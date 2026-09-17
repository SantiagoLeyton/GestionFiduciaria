from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from fiduciary.imports.audit import create_import_audit_event
from fiduciary.models import (
    AssignmentCreditSubsidy,
    AssignmentInterest,
    AssignmentLegalDocumentation,
    CBRImportRow,
    FiduciaryAssignment,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportedSheetResult,
    ImportRowIssue,
)
from fiduciary.permissions import can_import_fiduciary
from fiduciary.utils import calculate_sha256

from .parser import CBRIssue, CBRParser


class CBRFinalizationError(Exception):
    pass


@dataclass(frozen=True)
class CBRAnalysisResult:
    batch: ImportBatch
    imported_file: ImportedFile
    rows_created: int


@dataclass(frozen=True)
class CBRFinalizationResult:
    batch_id: int
    affected_assignments: int = 0
    credit_subsidies_created: int = 0
    credit_subsidies_reused: int = 0
    interests_created: int = 0
    interests_reused: int = 0
    contract_fields_updated: int = 0
    legal_fields_updated: int = 0


def analyze_cbr_import(*, batch: ImportBatch, file_path) -> CBRAnalysisResult:
    path = Path(file_path)
    imported_file = _reserve_cbr_file(batch=batch, file_path=path)
    parsed = CBRParser(path).parse()
    _store_cbr_file(imported_file=imported_file, source_path=path)
    with transaction.atomic():
        sheet_results = _persist_sheets(imported_file, parsed)
        _persist_parser_issues(imported_file, sheet_results, parsed)
        rows_created = _persist_rows(batch, imported_file, sheet_results, parsed)
        _update_counts(batch, imported_file)
    return CBRAnalysisResult(batch=batch, imported_file=imported_file, rows_created=rows_created)


def finalize_cbr_import(*, batch_id: int, user) -> CBRFinalizationResult:
    if not can_import_fiduciary(user):
        raise PermissionDenied
    try:
        with transaction.atomic():
            batch = ImportBatch.objects.select_for_update().get(pk=batch_id)
            _validate_ready(batch)
            imported_file = ImportedFile.objects.select_for_update().get(batch=batch, file_type=ImportedFile.FileType.CBR)
            if not imported_file.stored_path or not (settings.MEDIA_ROOT / imported_file.stored_path).exists():
                raise CBRFinalizationError("El archivo original del CBR no esta disponible.")
            now = timezone.now()
            batch.status = ImportBatch.Status.PROCESSING
            batch.processing_started_at = now
            batch.save(update_fields=["status", "processing_started_at"])
            imported_file.status = ImportedFile.Status.PROCESSING
            imported_file.save(update_fields=["status"])

            result = CBRFinalizationResult(batch_id=batch.pk)
            affected_assignments = set()
            for row in batch.cbr_rows.select_related("assignment", "sheet_result").select_for_update().order_by("sheet_name", "row_number"):
                if row.status != CBRImportRow.Status.VALID or not row.assignment_id:
                    raise CBRFinalizationError("Todas las filas aplicables deben estar validas y con encargo resuelto.")
                row_summary = _apply_row(row)
                affected_assignments.add(row.assignment_id)
                result = _add_result(result, row_summary)
                row.status = CBRImportRow.Status.IMPORTED
                row.applied_summary = row_summary
                row.message = "Fila CBR aplicada."
                row.save(update_fields=["status", "applied_summary", "message", "updated_at"])
                ImportAppliedRecord.objects.create(
                    batch=batch,
                    imported_file=imported_file,
                    sheet_result=row.sheet_result,
                    entity_kind=ImportAppliedRecord.EntityKind.CBR_IMPORT_ROW,
                    entity_id=row.pk,
                    action=ImportAppliedRecord.Action.CREATED,
                    source_row=row.row_number,
                    summary=f"CBR fila {row.row_number}, encargo {row.normalized_assignment_number}.",
                )

            result = CBRFinalizationResult(
                batch_id=batch.pk,
                affected_assignments=len(affected_assignments),
                credit_subsidies_created=result.credit_subsidies_created,
                credit_subsidies_reused=result.credit_subsidies_reused,
                interests_created=result.interests_created,
                interests_reused=result.interests_reused,
                contract_fields_updated=result.contract_fields_updated,
                legal_fields_updated=result.legal_fields_updated,
            )
            summary = _final_summary(result)
            batch.status = ImportBatch.Status.COMPLETED
            batch.processing_finished_at = timezone.now()
            batch.imported_at = batch.processing_finished_at
            batch.imported_by = user
            batch.summary = json.dumps(summary, ensure_ascii=True)
            batch.save(update_fields=["status", "processing_finished_at", "imported_at", "imported_by", "summary"])
            imported_file.status = ImportedFile.Status.COMPLETED
            imported_file.processing_finished_at = batch.processing_finished_at
            imported_file.result_message = json.dumps(summary, ensure_ascii=True)
            imported_file.save(update_fields=["status", "processing_finished_at", "result_message"])
            create_import_audit_event(
                batch=batch,
                imported_file=imported_file,
                entity_kind=ImportAppliedRecord.EntityKind.CBR_IMPORT_ROW,
                action="Importado",
                entity="CBR",
                lines=[
                    "Descripcion: Importacion CBR completada.",
                    f"Archivo: {imported_file.original_name}",
                    f"Resultado: {batch.get_status_display()}",
                    f"Encargos afectados: {result.affected_assignments}",
                    f"Creditos/subsidios creados: {result.credit_subsidies_created}",
                    f"Creditos/subsidios existentes: {result.credit_subsidies_reused}",
                    f"Intereses creados: {result.interests_created}",
                    f"Intereses existentes: {result.interests_reused}",
                    f"Datos contractuales actualizados: {result.contract_fields_updated}",
                    f"Documentacion legal actualizada: {result.legal_fields_updated}",
                ],
            )
            return result
    except Exception as exc:
        _mark_failed(batch_id, exc)
        raise


def _apply_row(row: CBRImportRow) -> dict[str, int]:
    assignment = row.assignment
    data = row.parsed_data or {}
    summary = {
        "credit_subsidies_created": 0,
        "credit_subsidies_reused": 0,
        "interests_created": 0,
        "interests_reused": 0,
        "contract_fields_updated": 0,
        "legal_fields_updated": 0,
    }
    for item in data.get("credit_subsidies") or []:
        entry_type = _credit_type(item["kind"])
        defaults = {
            "date": _parse_iso_date(item["date"]),
            "amount": Decimal(str(item["amount"])),
            "entity": item.get("entity", ""),
        }
        record, created = AssignmentCreditSubsidy.objects.get_or_create(
            assignment=assignment,
            entry_type=entry_type,
            defaults=defaults,
        )
        if created:
            summary["credit_subsidies_created"] += 1
        else:
            _update_fields(record, defaults)
            summary["credit_subsidies_reused"] += 1
    summary["contract_fields_updated"] += _update_fields(
        assignment,
        {
            field: _parse_iso_date(value)
            for field, value in (data.get("contract") or {}).items()
            if value
        },
    )
    legal_data = data.get("legal") or {}
    if legal_data:
        legal, _ = AssignmentLegalDocumentation.objects.get_or_create(assignment=assignment)
        legal_updates = {}
        for field, value in legal_data.items():
            if field in {"deed_date", "tradition_certificate_date"}:
                legal_updates[field] = _parse_iso_date(value)
            else:
                legal_updates[field] = value
        summary["legal_fields_updated"] += _update_fields(legal, legal_updates)
    interest = data.get("interest")
    if interest:
        _, created = AssignmentInterest.objects.get_or_create(
            assignment=assignment,
            receipt=interest.get("receipt", ""),
            interest=interest.get("date", ""),
            amount=Decimal(str(interest["amount"])),
        )
        if created:
            summary["interests_created"] += 1
        else:
            summary["interests_reused"] += 1
    return summary


def _update_fields(instance, values: dict) -> int:
    changed = []
    for field, value in values.items():
        if value in (None, ""):
            continue
        if getattr(instance, field) != value:
            setattr(instance, field, value)
            changed.append(field)
    if changed:
        instance.save(update_fields=[*changed, "updated_at"] if hasattr(instance, "updated_at") else changed)
    return len(changed)


def _credit_type(kind: str) -> str:
    return {
        "credit": AssignmentCreditSubsidy.EntryType.CREDIT,
        "box_subsidy": AssignmentCreditSubsidy.EntryType.BOX_SUBSIDY,
        "government_subsidy": AssignmentCreditSubsidy.EntryType.GOVERNMENT_SUBSIDY,
    }[kind]


def _parse_iso_date(value: str) -> date:
    return date.fromisoformat(value)


def _persist_sheets(imported_file, parsed):
    sheet_results = {}
    for sheet in parsed.sheets:
        sheet_result, _ = ImportedSheetResult.objects.update_or_create(
            imported_file=imported_file,
            sheet_name=sheet.name,
            defaults={
                "sheet_index": sheet.index,
                "classification": ImportedSheetResult.Classification.PROCESSABLE,
                "header_row": sheet.header_row,
                "processed_rows": len(sheet.rows),
                "error_count": len(sheet.issues),
                "status": ImportedSheetResult.Status.ANALYZED,
                "summary": "CBR analizado.",
            },
        )
        sheet_results[sheet.name] = sheet_result
    return sheet_results


def _persist_parser_issues(imported_file, sheet_results, parsed) -> int:
    created = 0
    for issue in parsed.issues:
        _create_issue(imported_file, sheet_results.get(issue.sheet_name or ""), issue)
        created += 1
    return created


def _persist_rows(batch, imported_file, sheet_results, parsed) -> int:
    created = 0
    for parsed_row in parsed.rows:
        assignment = _resolve_assignment(parsed_row.normalized_assignment_number)
        status = CBRImportRow.Status.VALID
        message = ""
        issues = list(parsed_row.issues)
        if parsed_row.normalized_assignment_number and not assignment:
            issues.append(
                CBRIssue(
                    code="CBR_ASSIGNMENT_NOT_FOUND",
                    message=f"No se encontro el encargo fiduciario {parsed_row.normalized_assignment_number} en Gestion Fiduciaria.",
                    sheet_name=parsed_row.sheet_name,
                    row_number=parsed_row.row_number,
                    field_name="ENCARGO",
                    found_value=parsed_row.normalized_assignment_number,
                )
            )
        if issues:
            status = CBRImportRow.Status.BLOCKED
            message = "La fila tiene incidencias bloqueantes."
        row = CBRImportRow.objects.create(
            batch=batch,
            imported_file=imported_file,
            sheet_result=sheet_results.get(parsed_row.sheet_name),
            sheet_name=parsed_row.sheet_name,
            row_number=parsed_row.row_number,
            original_assignment_number=parsed_row.original_assignment_number,
            normalized_assignment_number=parsed_row.normalized_assignment_number,
            assignment=assignment,
            parsed_data=parsed_row.parsed_data,
            original_data=parsed_row.original_data,
            status=status,
            message=message,
        )
        for issue in issues:
            _create_issue(imported_file, sheet_results.get(parsed_row.sheet_name), issue, assignment_number=parsed_row.normalized_assignment_number)
        created += 1
    return created


def _create_issue(imported_file, sheet_result, issue, *, assignment_number: str = "") -> None:
    ImportRowIssue.objects.create(
        imported_file=imported_file,
        sheet_result=sheet_result,
        row_number=issue.row_number,
        column_letter=getattr(issue, "column_letter", ""),
        unit_code=assignment_number,
        field_name=getattr(issue, "field_name", ""),
        found_value=getattr(issue, "found_value", ""),
        cause=issue.message,
        severity=ImportRowIssue.Severity.BLOCKING,
        code=issue.code,
        message=issue.message,
    )


def _resolve_assignment(number: str) -> FiduciaryAssignment | None:
    if not number:
        return None
    assignment = FiduciaryAssignment.objects.filter(assignment_number=number).first()
    if assignment:
        return assignment
    stripped = number.lstrip("0")
    if not stripped or stripped == number:
        return None
    for candidate in FiduciaryAssignment.objects.filter(assignment_number__endswith=stripped):
        if candidate.assignment_number.lstrip("0") == stripped:
            return candidate
    return None


def _update_counts(batch, imported_file):
    rows = batch.cbr_rows.all()
    parser_issue_count = imported_file.row_issues.filter(severity=ImportRowIssue.Severity.BLOCKING).count()
    blocked = rows.filter(status=CBRImportRow.Status.BLOCKED).count()
    batch.total_files = 1
    batch.processed_files = 1
    batch.total_rows = rows.count()
    batch.processed_rows = rows.exclude(status=CBRImportRow.Status.FAILED).count()
    batch.issue_count = parser_issue_count
    batch.status = ImportBatch.Status.AWAITING_RESOLUTION if (blocked or parser_issue_count) else ImportBatch.Status.READY
    batch.summary = json.dumps(_analysis_summary(rows), ensure_ascii=True)
    batch.save(update_fields=["total_files", "processed_files", "total_rows", "processed_rows", "issue_count", "status", "summary"])
    imported_file.total_rows = batch.total_rows
    imported_file.processed_rows = batch.processed_rows
    imported_file.error_count = parser_issue_count
    imported_file.status = ImportedFile.Status.COMPLETED_WITH_ISSUES if parser_issue_count else ImportedFile.Status.READY
    imported_file.result_message = batch.summary
    imported_file.save(update_fields=["total_rows", "processed_rows", "error_count", "status", "result_message"])


def _analysis_summary(rows):
    return {
        "total": rows.count(),
        "valid": rows.filter(status=CBRImportRow.Status.VALID).count(),
        "blocked": rows.filter(status=CBRImportRow.Status.BLOCKED).count(),
        "assignments": rows.filter(assignment__isnull=False).values("assignment_id").distinct().count(),
    }


def _final_summary(result: CBRFinalizationResult) -> dict:
    return {
        "affected_assignments": result.affected_assignments,
        "credit_subsidies_created": result.credit_subsidies_created,
        "credit_subsidies_reused": result.credit_subsidies_reused,
        "interests_created": result.interests_created,
        "interests_reused": result.interests_reused,
        "contract_fields_updated": result.contract_fields_updated,
        "legal_fields_updated": result.legal_fields_updated,
    }


def _add_result(result: CBRFinalizationResult, summary: dict[str, int]) -> CBRFinalizationResult:
    return CBRFinalizationResult(
        batch_id=result.batch_id,
        affected_assignments=result.affected_assignments,
        credit_subsidies_created=result.credit_subsidies_created + summary.get("credit_subsidies_created", 0),
        credit_subsidies_reused=result.credit_subsidies_reused + summary.get("credit_subsidies_reused", 0),
        interests_created=result.interests_created + summary.get("interests_created", 0),
        interests_reused=result.interests_reused + summary.get("interests_reused", 0),
        contract_fields_updated=result.contract_fields_updated + summary.get("contract_fields_updated", 0),
        legal_fields_updated=result.legal_fields_updated + summary.get("legal_fields_updated", 0),
    )


def _reserve_cbr_file(*, batch: ImportBatch, file_path) -> ImportedFile:
    path = Path(file_path)
    return ImportedFile.objects.create(
        batch=batch,
        original_name=path.name,
        extension=path.suffix.lower(),
        size_bytes=path.stat().st_size,
        sha256=calculate_sha256(path),
        file_type=ImportedFile.FileType.CBR,
        status=ImportedFile.Status.ANALYZING,
        order=1,
        result_message="Analisis CBR en curso.",
    )


def _store_cbr_file(*, imported_file: ImportedFile, source_path) -> None:
    source = Path(source_path)
    target_dir = settings.MEDIA_ROOT / "imports" / "cbr"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{imported_file.sha256}{source.suffix.lower()}"
    if not target.exists():
        shutil.copyfile(source, target)
    imported_file.stored_path = str(target.relative_to(settings.MEDIA_ROOT))
    imported_file.save(update_fields=["stored_path"])


def _validate_ready(batch):
    if batch.import_type != ImportBatch.ImportType.CBR:
        raise CBRFinalizationError("Solo los lotes CBR pueden aplicarse con este servicio.")
    if batch.status != ImportBatch.Status.READY:
        raise CBRFinalizationError("Solo un lote CBR listo puede aplicarse.")
    if batch.cbr_rows.exclude(status=CBRImportRow.Status.VALID).exists() or batch.files.filter(row_issues__severity=ImportRowIssue.Severity.BLOCKING).exists():
        raise CBRFinalizationError("El lote CBR tiene incidencias bloqueantes.")


def _mark_failed(batch_id, exc):
    ImportBatch.objects.filter(pk=batch_id, status__in=[ImportBatch.Status.READY, ImportBatch.Status.PROCESSING]).update(
        status=ImportBatch.Status.FAILED,
        processing_finished_at=timezone.now(),
        summary=f"No fue posible aplicar el CBR: {_safe_error(exc)}",
    )
    ImportedFile.objects.filter(batch_id=batch_id, status=ImportedFile.Status.PROCESSING).update(
        status=ImportedFile.Status.FAILED,
        processing_finished_at=timezone.now(),
        result_message="No fue posible aplicar el CBR.",
    )


def _safe_error(exc):
    if isinstance(exc, CBRFinalizationError):
        return str(exc)
    return "error interno durante la aplicacion."
