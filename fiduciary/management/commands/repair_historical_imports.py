from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from fiduciary.imports.historical.parser import HistoricalWorkbookParser
from fiduciary.models import (
    Client,
    FiduciaryAssignment,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportedSheetResult,
    Payment,
)
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

        totals = {"contacts": 0, "documents": 0, "names": 0, "payments": 0, "conflicts": 0, "missing_files": 0}
        self.stdout.write("Modo: APLICAR" if apply_changes else "Modo: SIMULACION")
        for batch in batches.order_by("pk"):
            result = self._process_batch(batch, apply_changes=apply_changes, project_filter=options.get("project"))
            for key, value in result.items():
                totals[key] += value
            self.stdout.write(
                f"Lote #{batch.pk}: contactos {result['contacts']}, documentos {result['documents']}, nombres {result['names']}, "
                f"pagos {result['payments']}, conflictos {result['conflicts']}, archivos faltantes {result['missing_files']}."
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Total: contactos {totals['contacts']}, documentos {totals['documents']}, nombres {totals['names']}, pagos {totals['payments']}, "
                f"conflictos {totals['conflicts']}, archivos faltantes {totals['missing_files']}."
            )
        )

    def _process_batch(self, batch, *, apply_changes: bool, project_filter: str | None) -> dict:
        counts = {"contacts": 0, "documents": 0, "names": 0, "payments": 0, "conflicts": 0, "missing_files": 0}
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
