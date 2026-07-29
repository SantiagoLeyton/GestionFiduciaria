import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from fiduciary.models import (
    DailyReportRow,
    FiduciaryAssignment,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportRowIssue,
    ImportedSheetResult,
    Payment,
)
from fiduciary.permissions import can_import_fiduciary
from fiduciary.services import create_payment
from fiduciary.utils import calculate_sha256

from .parser import DailyReportParser


class DailyReportDuplicateError(Exception):
    def __init__(self, imported_file: ImportedFile):
        self.imported_file = imported_file
        super().__init__("Este reporte diario ya fue cargado anteriormente.")


class DailyReportFinalizationError(Exception):
    pass


@dataclass(frozen=True)
class DailyReportAnalysisResult:
    batch: ImportBatch
    imported_file: ImportedFile
    rows_created: int


@dataclass(frozen=True)
class DailyReportFinalizationResult:
    batch_id: int
    imported_rows: int = 0
    duplicate_rows: int = 0


def analyze_daily_report_import(*, batch: ImportBatch, file_path) -> DailyReportAnalysisResult:
    path = Path(file_path)
    imported_file = _reserve_report_file(batch=batch, file_path=path)
    parsed = DailyReportParser(path).parse()
    _store_report_file(imported_file=imported_file, source_path=path)
    with transaction.atomic():
        sheet_results = _persist_sheets(imported_file, parsed)
        _persist_issues(imported_file, sheet_results, parsed)
        rows_created = _persist_rows(batch, imported_file, sheet_results, parsed)
        _update_counts(batch, imported_file)
    return DailyReportAnalysisResult(batch=batch, imported_file=imported_file, rows_created=rows_created)


def reanalyze_daily_report_import(*, batch: ImportBatch, user=None) -> int:
    updated = 0
    with transaction.atomic():
        for row in batch.daily_report_rows.select_for_update().filter(
            status__in=[DailyReportRow.Status.ASSIGNMENT_NOT_FOUND, DailyReportRow.Status.NEEDS_REVIEW]
        ):
            if row.assignment_id:
                row.status = _duplicate_or_valid(row)
                row.message = "Encargo resuelto manualmente." if row.status == DailyReportRow.Status.VALID else "Pago duplicado."
                row.save(update_fields=["status", "message", "updated_at"])
                updated += 1
                continue
            assignment = FiduciaryAssignment.objects.filter(assignment_number=row.normalized_assignment_number).first()
            if assignment:
                row.assignment = assignment
                row.status = _duplicate_or_valid(row)
                row.message = "" if row.status == DailyReportRow.Status.VALID else "Pago duplicado."
                row.save(update_fields=["assignment", "status", "message", "updated_at"])
                updated += 1
        _update_batch_status(batch)
    return updated


def resolve_daily_report_assignment(*, row: DailyReportRow, assignment: FiduciaryAssignment | None, user, note: str = "") -> None:
    if not can_import_fiduciary(user):
        raise PermissionDenied
    with transaction.atomic():
        locked = DailyReportRow.objects.select_for_update().get(pk=row.pk)
        locked.assignment = assignment
        locked.resolved_by = user
        locked.resolved_at = timezone.now()
        locked.resolution_note = note.strip()
        if assignment:
            locked.status = _duplicate_or_valid(locked)
            locked.message = "Encargo resuelto manualmente." if locked.status == DailyReportRow.Status.VALID else "Pago duplicado."
        else:
            locked.status = DailyReportRow.Status.ASSIGNMENT_NOT_FOUND
            locked.message = "Encargo pendiente de resolucion."
        locked.save()
        _update_batch_status(locked.batch)


def finalize_daily_report_import(*, batch_id: int, user) -> DailyReportFinalizationResult:
    if not can_import_fiduciary(user):
        raise PermissionDenied
    try:
        with transaction.atomic():
            batch = ImportBatch.objects.select_for_update().get(pk=batch_id)
            _validate_ready(batch)
            imported_file = ImportedFile.objects.select_for_update().get(batch=batch, file_type=ImportedFile.FileType.REPORT)
            if not imported_file.stored_path or not (settings.MEDIA_ROOT / imported_file.stored_path).exists():
                raise DailyReportFinalizationError("El archivo original del reporte no esta disponible.")
            batch.status = ImportBatch.Status.PROCESSING
            batch.processing_started_at = timezone.now()
            batch.save(update_fields=["status", "processing_started_at"])
            imported_file.status = ImportedFile.Status.PROCESSING
            imported_file.save(update_fields=["status"])

            imported_rows = 0
            duplicate_rows = 0
            rows = (
                batch.daily_report_rows.select_related("assignment", "sheet_result")
                .select_for_update(of=("self",))
                .order_by("sheet_name", "row_number")
            )
            for row in rows:
                if row.status == DailyReportRow.Status.DUPLICATE:
                    duplicate_rows += 1
                    _trace(batch, imported_file, row, ImportAppliedRecord.Action.SKIPPED, summary="Pago duplicado omitido.")
                    continue
                if row.status != DailyReportRow.Status.VALID or not row.assignment_id:
                    raise DailyReportFinalizationError("Todas las filas aplicables deben estar validas y con encargo resuelto.")
                result = create_payment(
                    assignment=row.assignment,
                    amount=row.amount,
                    movement_type=row.movement_type,
                    source_file=imported_file,
                    source_sheet=row.sheet_name,
                    source_row=row.row_number,
                    date_precision=Payment.DatePrecision.EXACT,
                    exact_date=row.payment_date,
                    concept=row.concept,
                )
                if result.status == "created":
                    row.payment = result.payment
                    row.status = DailyReportRow.Status.IMPORTED
                    row.message = "Pago importado."
                    row.save(update_fields=["payment", "status", "message", "updated_at"])
                    imported_rows += 1
                    _trace(batch, imported_file, row, ImportAppliedRecord.Action.CREATED, payment=result.payment)
                elif result.status == "duplicate":
                    row.status = DailyReportRow.Status.DUPLICATE
                    row.message = "Pago duplicado omitido."
                    row.save(update_fields=["status", "message", "updated_at"])
                    duplicate_rows += 1
                    _trace(batch, imported_file, row, ImportAppliedRecord.Action.SKIPPED, summary="Pago duplicado omitido.")
                else:
                    raise DailyReportFinalizationError("; ".join(result.errors) or "No fue posible crear el pago.")
            now = timezone.now()
            batch.status = ImportBatch.Status.COMPLETED
            batch.processing_finished_at = now
            batch.imported_at = now
            batch.imported_by = user
            batch.summary = f"Reporte diario importado. Pagos creados: {imported_rows}; duplicados omitidos: {duplicate_rows}."
            batch.save(update_fields=["status", "processing_finished_at", "imported_at", "imported_by", "summary"])
            imported_file.status = ImportedFile.Status.COMPLETED
            imported_file.processing_finished_at = now
            imported_file.result_message = batch.summary
            imported_file.save(update_fields=["status", "processing_finished_at", "result_message"])
            return DailyReportFinalizationResult(batch_id=batch.pk, imported_rows=imported_rows, duplicate_rows=duplicate_rows)
    except Exception as exc:
        _mark_failed(batch_id, exc)
        raise


def find_existing_daily_report(file_path) -> ImportedFile | None:
    sha256 = calculate_sha256(file_path)
    return ImportedFile.objects.filter(file_type=ImportedFile.FileType.REPORT, sha256=sha256).first()


def _reserve_report_file(*, batch: ImportBatch, file_path) -> ImportedFile:
    path = Path(file_path)
    sha256 = calculate_sha256(path)
    existing = ImportedFile.objects.filter(file_type=ImportedFile.FileType.REPORT, sha256=sha256).first()
    if existing:
        raise DailyReportDuplicateError(existing)
    try:
        return ImportedFile.objects.create(
            batch=batch,
            original_name=path.name,
            extension=path.suffix.lower(),
            size_bytes=path.stat().st_size,
            sha256=sha256,
            file_type=ImportedFile.FileType.REPORT,
            status=ImportedFile.Status.ANALYZING,
            order=1,
            result_message="Analisis de reporte diario en curso.",
        )
    except IntegrityError as exc:
        existing = ImportedFile.objects.filter(file_type=ImportedFile.FileType.REPORT, sha256=sha256).first()
        if existing:
            raise DailyReportDuplicateError(existing) from exc
        raise


def _store_report_file(*, imported_file: ImportedFile, source_path) -> None:
    source = Path(source_path)
    target_dir = settings.MEDIA_ROOT / "imports" / "reports"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{imported_file.sha256}{source.suffix.lower()}"
    if not target.exists():
        shutil.copyfile(source, target)
    imported_file.stored_path = str(target.relative_to(settings.MEDIA_ROOT))
    imported_file.save(update_fields=["stored_path"])


def _persist_sheets(imported_file, parsed):
    sheet_results = {}
    for sheet in parsed.sheets:
        sheet_result, _ = ImportedSheetResult.objects.update_or_create(
            imported_file=imported_file,
            sheet_name=sheet.name,
            defaults={
                "sheet_index": sheet.index,
                "classification": ImportedSheetResult.Classification.PROCESSABLE if sheet.rows else ImportedSheetResult.Classification.UNKNOWN,
                "header_row": sheet.header_row,
                "processed_rows": len(sheet.rows),
                "error_count": len(sheet.issues),
                "status": ImportedSheetResult.Status.ANALYZED,
                "summary": "Reporte diario analizado.",
            },
        )
        sheet_results[sheet.name] = sheet_result
    return sheet_results


def _persist_issues(imported_file, sheet_results, parsed) -> int:
    created = 0
    for issue in parsed.issues:
        ImportRowIssue.objects.create(
            imported_file=imported_file,
            sheet_result=sheet_results.get(issue.sheet_name or ""),
            row_number=issue.row_number,
            severity=ImportRowIssue.Severity.BLOCKING,
            code=issue.code,
            message=issue.message,
        )
        created += 1
    return created


def _persist_rows(batch, imported_file, sheet_results, parsed) -> int:
    created = 0
    for parsed_row in parsed.rows:
        assignment = None
        status = parsed_row.status_hint
        message = parsed_row.message
        if status == DailyReportRow.Status.VALID:
            assignment = FiduciaryAssignment.objects.filter(assignment_number=parsed_row.normalized_assignment_number).first()
            if not assignment:
                status = DailyReportRow.Status.ASSIGNMENT_NOT_FOUND
                message = "El encargo no existe."
            elif Payment.objects.filter(
                assignment=assignment,
                date_precision=Payment.DatePrecision.EXACT,
                exact_date=parsed_row.payment_date,
                amount=parsed_row.amount,
            ).exists():
                status = DailyReportRow.Status.DUPLICATE
                message = "Pago duplicado."
        DailyReportRow.objects.create(
            batch=batch,
            imported_file=imported_file,
            sheet_result=sheet_results.get(parsed_row.sheet_name),
            sheet_name=parsed_row.sheet_name,
            row_number=parsed_row.row_number,
            original_assignment_number=parsed_row.original_assignment_number,
            normalized_assignment_number=parsed_row.normalized_assignment_number,
            payment_date=parsed_row.payment_date,
            amount=parsed_row.amount,
            movement_type=parsed_row.movement_type,
            payer_name=parsed_row.payer_name,
            payer_document=parsed_row.payer_document,
            concept=parsed_row.concept,
            original_data=parsed_row.original_data,
            status=status,
            message=message,
            assignment=assignment,
        )
        created += 1
    return created


def _update_counts(batch, imported_file):
    rows = batch.daily_report_rows.all()
    parser_issue_count = imported_file.row_issues.filter(severity=ImportRowIssue.Severity.BLOCKING).count()
    blocking = rows.filter(
        status__in=[
            DailyReportRow.Status.ASSIGNMENT_NOT_FOUND,
            DailyReportRow.Status.INVALID_ASSIGNMENT,
            DailyReportRow.Status.INVALID_DATE,
            DailyReportRow.Status.INVALID_AMOUNT,
            DailyReportRow.Status.NEEDS_REVIEW,
            DailyReportRow.Status.FAILED,
        ]
    ).count()
    batch.total_files = 1
    batch.processed_files = 1
    batch.total_rows = rows.count()
    batch.processed_rows = rows.exclude(status__in=[DailyReportRow.Status.FAILED]).count()
    batch.issue_count = blocking + parser_issue_count
    if parser_issue_count:
        batch.status = ImportBatch.Status.FAILED
    else:
        batch.status = ImportBatch.Status.AWAITING_RESOLUTION if blocking else ImportBatch.Status.READY
    batch.summary = json.dumps(_summary(rows), ensure_ascii=True)
    batch.save(update_fields=["total_files", "processed_files", "total_rows", "processed_rows", "issue_count", "status", "summary"])
    imported_file.total_rows = batch.total_rows
    imported_file.processed_rows = batch.processed_rows
    imported_file.error_count = blocking + parser_issue_count
    imported_file.status = ImportedFile.Status.FAILED if parser_issue_count else ImportedFile.Status.READY
    imported_file.result_message = batch.summary
    imported_file.save(update_fields=["total_rows", "processed_rows", "error_count", "status", "result_message"])


def _update_batch_status(batch):
    _update_counts(batch, batch.files.get(file_type=ImportedFile.FileType.REPORT))


def _summary(rows):
    return {
        "total": rows.count(),
        "valid": rows.filter(status=DailyReportRow.Status.VALID).count(),
        "duplicate": rows.filter(status=DailyReportRow.Status.DUPLICATE).count(),
        "assignment_not_found": rows.filter(status=DailyReportRow.Status.ASSIGNMENT_NOT_FOUND).count(),
        "invalid_date": rows.filter(status=DailyReportRow.Status.INVALID_DATE).count(),
        "invalid_amount": rows.filter(status=DailyReportRow.Status.INVALID_AMOUNT).count(),
        "needs_review": rows.filter(status=DailyReportRow.Status.NEEDS_REVIEW).count(),
        "valid_amount": str(sum((row.amount for row in rows.filter(status=DailyReportRow.Status.VALID) if row.amount), start=0)),
    }


def _duplicate_or_valid(row: DailyReportRow) -> str:
    if row.assignment_id and row.payment_date and row.amount and Payment.objects.filter(
        assignment=row.assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=row.payment_date,
        amount=row.amount,
    ).exists():
        return DailyReportRow.Status.DUPLICATE
    return DailyReportRow.Status.VALID


def _validate_ready(batch):
    if batch.import_type != ImportBatch.ImportType.REPORTS:
        raise DailyReportFinalizationError("Solo los lotes de reportes pueden importarse con este servicio.")
    if batch.status != ImportBatch.Status.READY:
        raise DailyReportFinalizationError("Solo un lote listo puede aplicarse.")
    if batch.daily_report_rows.filter(
        status__in=[
            DailyReportRow.Status.ASSIGNMENT_NOT_FOUND,
            DailyReportRow.Status.INVALID_ASSIGNMENT,
            DailyReportRow.Status.INVALID_DATE,
            DailyReportRow.Status.INVALID_AMOUNT,
            DailyReportRow.Status.NEEDS_REVIEW,
            DailyReportRow.Status.FAILED,
        ]
    ).exists():
        raise DailyReportFinalizationError("El lote tiene errores bloqueantes.")


def _trace(batch, imported_file, row, action, payment=None, summary=""):
    ImportAppliedRecord.objects.create(
        batch=batch,
        imported_file=imported_file,
        sheet_result=row.sheet_result,
        entity_kind=ImportAppliedRecord.EntityKind.PAYMENT if payment else ImportAppliedRecord.EntityKind.DAILY_REPORT_ROW,
        entity_id=payment.pk if payment else row.pk,
        action=action,
        source_row=row.row_number,
        summary=summary or f"Reporte diario fila {row.row_number}, encargo {row.normalized_assignment_number}.",
    )


def _mark_failed(batch_id, exc):
    ImportBatch.objects.filter(pk=batch_id, status__in=[ImportBatch.Status.READY, ImportBatch.Status.PROCESSING]).update(
        status=ImportBatch.Status.FAILED,
        processing_finished_at=timezone.now(),
        summary=f"No fue posible aplicar el reporte diario: {_safe_error(exc)}",
    )
    ImportedFile.objects.filter(batch_id=batch_id, status=ImportedFile.Status.PROCESSING).update(
        status=ImportedFile.Status.FAILED,
        processing_finished_at=timezone.now(),
        result_message="No fue posible aplicar el reporte diario.",
    )


def _safe_error(exc):
    if isinstance(exc, (DailyReportFinalizationError, ValidationError)):
        return str(exc)
    return "error interno durante la aplicacion."
