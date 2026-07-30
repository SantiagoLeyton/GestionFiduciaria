from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from fiduciary.imports.historical.finalize import _observation_dedupe_key, _summary_detail_from_cells
from fiduciary.imports.historical.parser import HistoricalWorkbookParser
from fiduciary.models import (
    Client,
    FiduciaryAssignment,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportedHistoricalNovelty,
    ImportedHistoricalObservation,
    ImportedSheetResult,
    OperationalNovelty,
    Payment,
)
from real_estate.models import PropertyUnit
from fiduciary.services import create_payment, split_imported_full_name


class Command(BaseCommand):
    help = "Diagnostica y repara de forma idempotente datos reconstruibles desde importaciones historicas completadas."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Aplica cambios seguros. Por defecto solo simula.")
        parser.add_argument("--batch", type=int, help="Limita la reparacion a un lote.")
        parser.add_argument("--project", type=str, help="Limita por nombre de proyecto detectado en el libro.")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        batches = ImportBatch.objects.filter(
            import_type=ImportBatch.ImportType.HISTORICAL,
            status__in=[ImportBatch.Status.COMPLETED, ImportBatch.Status.COMPLETED_WITH_ISSUES],
        ).prefetch_related("files")
        if options.get("batch"):
            batches = batches.filter(pk=options["batch"])

        totals = {
            "contacts": 0,
            "documents": 0,
            "names": 0,
            "payments": 0,
            "observations": 0,
            "conflicts": 0,
            "missing_files": 0,
        }
        self.stdout.write("Modo: APLICAR" if apply_changes else "Modo: SIMULACION")
        for batch in batches.order_by("pk"):
            result = self._process_batch(batch, apply_changes=apply_changes, project_filter=options.get("project"))
            for key, value in result.items():
                totals[key] += value
            self.stdout.write(
                f"Lote #{batch.pk}: contactos {result['contacts']}, documentos {result['documents']}, nombres {result['names']}, "
                f"pagos {result['payments']}, observaciones {result['observations']}, conflictos {result['conflicts']}, "
                f"archivos faltantes {result['missing_files']}."
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Total: contactos {totals['contacts']}, documentos {totals['documents']}, nombres {totals['names']}, pagos {totals['payments']}, "
                f"observaciones {totals['observations']}, conflictos {totals['conflicts']}, archivos faltantes {totals['missing_files']}."
            )
        )

    def _process_batch(self, batch, *, apply_changes: bool, project_filter: str | None) -> dict:
        counts = {
            "contacts": 0,
            "documents": 0,
            "names": 0,
            "payments": 0,
            "observations": 0,
            "conflicts": 0,
            "missing_files": 0,
        }
        imported_file = batch.files.filter(file_type=ImportedFile.FileType.HISTORICAL).order_by("order").first()
        if not imported_file or not imported_file.stored_path:
            counts["missing_files"] += 1
            return counts
        path = settings.MEDIA_ROOT / imported_file.stored_path
        if not path.exists():
            counts["missing_files"] += 1
            return counts
        workbook = HistoricalWorkbookParser(path).parse()
        if project_filter and not any(project_filter.lower() in row.project.lower() for sheet in workbook.sheets for row in sheet.rows):
            return counts

        with transaction.atomic():
            for sheet in workbook.sheets:
                sheet_result = ImportedSheetResult.objects.filter(imported_file=imported_file, sheet_name=sheet.name).first()
                for row in sheet.rows:
                    if not row.assignment:
                        continue
                    assignment = FiduciaryAssignment.objects.filter(assignment_number=row.assignment.assignment_number).first()
                    if not assignment:
                        counts["conflicts"] += 1
                        continue
                    counts["contacts"] += self._repair_contacts(row, apply_changes=apply_changes)
                    counts["names"] += self._repair_names(row, apply_changes=apply_changes)
                    counts["documents"] += self._repair_documents(assignment, row, apply_changes=apply_changes)
                    counts["payments"] += self._repair_payments(
                        batch=batch,
                        imported_file=imported_file,
                        sheet_result=sheet_result,
                        assignment=assignment,
                        row=row,
                        apply_changes=apply_changes,
                    )
                    counts["observations"] += self._repair_main_observation(
                        batch=batch,
                        imported_file=imported_file,
                        sheet_result=sheet_result,
                        assignment=assignment,
                        row=row,
                        apply_changes=apply_changes,
                    )
            counts["observations"] += self._repair_historical_novelty_observations(
                batch=batch,
                imported_file=imported_file,
                apply_changes=apply_changes,
            )
            if not apply_changes:
                transaction.set_rollback(True)
        return counts

    def _repair_contacts(self, row, *, apply_changes: bool) -> int:
        changed = 0
        for historical_client in row.clients:
            if not historical_client.document_number:
                continue
            client = Client.objects.filter(document_number=historical_client.document_number).first()
            if not client:
                continue
            update_fields = []
            if historical_client.email and not client.email:
                client.email = historical_client.email
                update_fields.append("email")
            if historical_client.phone and not client.phone:
                client.phone = historical_client.phone
                update_fields.append("phone")
            if historical_client.contact_name and not client.address:
                client.address = historical_client.contact_name
                update_fields.append("address")
            if update_fields:
                changed += 1
                if apply_changes:
                    client.full_clean()
                    client.save(update_fields=update_fields + ["updated_at"])
        return changed

    def _repair_main_observation(self, *, batch, imported_file, sheet_result, assignment, row, apply_changes: bool) -> int:
        detail = (row.observation or "").strip()
        if not detail:
            return 0
        unit = assignment.property_unit
        client = assignment.holders.select_related("client").filter(is_active=True, is_primary=True).first()
        client_obj = client.client if client else None
        dedupe_key = _observation_dedupe_key(
            origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
            project=unit.project,
            unit=unit,
            client=client_obj,
            assignment=assignment,
            source_sheet=row.sheet_name,
            summary="",
            detail=detail,
            historical_section="",
        )
        existing = ImportedHistoricalObservation.objects.filter(
            origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
            property_unit=unit,
            client=client_obj,
            assignment=assignment,
            source_sheet=row.sheet_name,
            source_row=row.row_number,
        ).first()
        if existing and existing.detail == detail and existing.dedupe_key == dedupe_key:
            return 0
        if existing and apply_changes:
            existing.detail = detail
            existing.dedupe_key = dedupe_key
            existing.full_clean()
            existing.save(update_fields=["detail", "dedupe_key", "updated_at"])
            return 1
        if existing:
            return 1
        if ImportedHistoricalObservation.objects.filter(dedupe_key=dedupe_key).exists():
            return 0
        if apply_changes:
            ImportedHistoricalObservation.objects.create(
                batch=batch,
                imported_file=imported_file,
                sheet_result=sheet_result,
                project=unit.project,
                property_unit=unit,
                client=client_obj,
                assignment=assignment,
                origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
                status=ImportedHistoricalObservation.Status.IMPORTED,
                summary="",
                detail=detail,
                source_sheet=row.sheet_name,
                source_row=row.row_number,
                source_order=row.row_number,
                dedupe_key=dedupe_key,
                source_payload={"row_type": "main_table"},
                imported_by=batch.initiated_by,
            )
        return 1

    def _repair_historical_novelty_observations(self, *, batch, imported_file, apply_changes: bool) -> int:
        changed = 0
        novelties = ImportedHistoricalNovelty.objects.filter(batch=batch).select_related("sheet_result")
        for novelty in novelties:
            unit = self._find_unit_for_novelty(novelty)
            assignment = self._find_assignment_for_novelty(novelty)
            client = self._find_client_for_novelty(novelty)
            summary, detail = _summary_detail_from_cells(novelty.original_cells)
            if not unit or not any([assignment, client, summary, detail]):
                continue
            historical_section = self._payload_value(novelty.original_cells, "__historical_section__")
            existing = OperationalNovelty.objects.filter(
                origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
                property_unit=unit,
                historical_client=client,
                historical_assignment=assignment,
                source_sheet=novelty.sheet_result.sheet_name,
                source_row=novelty.row_number,
                historical_section=historical_section,
            ).first()
            if existing:
                if existing.summary == summary and existing.detail == detail:
                    continue
                changed += 1
                if apply_changes:
                    existing.summary = summary
                    existing.detail = detail
                    existing.full_clean()
                    existing.save(update_fields=["summary", "detail", "updated_at"])
                continue
            if OperationalNovelty.objects.filter(source_novelty=novelty).exists():
                continue
            changed += 1
            if apply_changes:
                OperationalNovelty.objects.create(
                    batch=batch,
                    imported_file=imported_file,
                    source_novelty=novelty,
                    project=unit.project,
                    property_unit=unit,
                    historical_client=client,
                    historical_assignment=assignment,
                    novelty_type=OperationalNovelty.NoveltyType.HISTORICAL,
                    origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
                    status=OperationalNovelty.Status.IMPORTED,
                    historical_section=historical_section,
                    historical_month=self._int_or_none(self._payload_value(novelty.original_cells, "__section_month__")),
                    historical_year=self._int_or_none(self._payload_value(novelty.original_cells, "__section_year__")),
                    summary=summary,
                    detail=detail,
                    source_sheet=novelty.sheet_result.sheet_name,
                    source_row=novelty.row_number,
                    source_payload={"cells": novelty.original_cells},
                    created_by=batch.initiated_by,
                )
        return changed

    def _find_unit_for_novelty(self, novelty) -> PropertyUnit | None:
        unit_value = novelty.unit_code or novelty.unit_name
        if not unit_value:
            return None
        query = PropertyUnit.objects.filter(project__name__iexact=novelty.project_name).filter(
            Q(code__iexact=unit_value) | Q(name__iexact=unit_value)
        )
        if novelty.grouping_name:
            query = query.filter(
                Q(structural_group__name__iexact=novelty.grouping_name)
                | Q(structural_group__code__iexact=novelty.grouping_name)
            )
        return query.first() if query.count() == 1 else None

    def _find_assignment_for_novelty(self, novelty) -> FiduciaryAssignment | None:
        number = (novelty.assignment_number or "").strip()
        if not number:
            return None
        return FiduciaryAssignment.objects.filter(assignment_number=number).first()

    def _find_client_for_novelty(self, novelty) -> Client | None:
        document = self._payload_value(novelty.original_cells, "CEDULA CLIENTE")
        if document:
            client = Client.objects.filter(document_number=str(document).strip()).first()
            if client:
                return client
        return None

    def _payload_value(self, cells, header):
        normalized_header = header.strip().lower()
        for cell in cells or []:
            if str(cell.get("header", "")).strip().lower() == normalized_header:
                value = cell.get("value")
                return str(value).strip() if value is not None else ""
        return ""

    def _int_or_none(self, value):
        try:
            return int(value) if value not in ("", None) else None
        except (TypeError, ValueError):
            return None

    def _repair_names(self, row, *, apply_changes: bool) -> int:
        changed = 0
        for historical_client in row.clients:
            if not historical_client.document_number:
                continue
            client = Client.objects.filter(document_number=historical_client.document_number, source_origin=Client.SourceOrigin.HISTORICAL_IMPORT).first()
            if not client or client.first_names:
                continue
            first_names, last_names = split_imported_full_name(client.last_names_or_company)
            if not first_names:
                continue
            changed += 1
            if apply_changes:
                client.first_names = first_names
                client.last_names_or_company = last_names
                client.full_clean()
                client.save(update_fields=["first_names", "last_names_or_company", "updated_at"])
        return changed

    def _repair_documents(self, assignment, row, *, apply_changes: bool) -> int:
        changed = 0
        holders = list(assignment.holders.select_related("client").filter(is_active=True).order_by("-is_primary", "start_date", "pk"))
        for index, historical_client in enumerate(row.clients):
            if index >= len(holders) or not historical_client.document_number:
                continue
            client = holders[index].client
            if client.document_number == historical_client.document_number:
                continue
            if client.document_number and "/" not in client.document_number:
                continue
            if Client.objects.filter(document_type=client.document_type, document_number=historical_client.document_number).exclude(pk=client.pk).exists():
                continue
            changed += 1
            if apply_changes:
                client.document_number = historical_client.document_number
                client.full_clean()
                client.save(update_fields=["document_number", "updated_at"])
        return changed

    def _repair_payments(self, *, batch, imported_file, sheet_result, assignment, row, apply_changes: bool) -> int:
        changed = 0
        for payment in row.payments:
            exists = Payment.objects.filter(
                assignment=assignment,
                date_precision=Payment.DatePrecision.MONTH,
                period_year=payment.year,
                period_month=payment.month,
                amount=payment.amount,
            ).exists()
            if exists:
                continue
            changed += 1
            if apply_changes:
                result = create_payment(
                    assignment=assignment,
                    amount=payment.amount,
                    movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
                    source_file=imported_file,
                    source_sheet=sheet_result.sheet_name if sheet_result else row.sheet_name,
                    source_row=payment.source_row,
                    source_column=payment.source_column,
                    source_header=payment.source_header,
                    source_had_formula=payment.has_formula,
                    date_precision=Payment.DatePrecision.MONTH,
                    period_year=payment.year,
                    period_month=payment.month,
                )
                if result.status == "created":
                    ImportAppliedRecord.objects.create(
                        batch=batch,
                        imported_file=imported_file,
                        sheet_result=sheet_result,
                        entity_kind=ImportAppliedRecord.EntityKind.PAYMENT,
                        entity_id=result.payment.pk,
                        action=ImportAppliedRecord.Action.CREATED,
                        source_row=payment.source_row,
                        source_column=payment.source_column,
                        summary="Pago historico reparado desde archivo original.",
                    )
        return changed
