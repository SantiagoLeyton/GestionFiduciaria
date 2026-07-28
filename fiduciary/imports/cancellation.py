from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from fiduciary.models import (
    DetectedStructureElement,
    ImportBatch,
    ImportAppliedRecord,
    ImportedHistoricalNovelty,
    ImportNovelty,
    ImportResolution,
    ImportedFile,
    ImportedSheetResult,
    ImportRowIssue,
    Payment,
)


CANCELABLE_BATCH_STATUSES = {
    ImportBatch.Status.ANALYZING,
    ImportBatch.Status.AWAITING_RESOLUTION,
    ImportBatch.Status.READY,
    ImportBatch.Status.FAILED,
}


@dataclass(frozen=True)
class ImportCancellationResult:
    batch_id: int
    files_deleted: int
    sheet_results_deleted: int
    row_issues_deleted: int
    detected_elements_deleted: int
    resolutions_deleted: int
    novelties_deleted: int
    historical_novelties_deleted: int


def cancel_import_batch(*, batch: ImportBatch, cancelled_by) -> ImportCancellationResult:
    if not cancelled_by or not cancelled_by.is_authenticated:
        raise ValidationError("Debe autenticarse para cancelar una importacion.")

    with transaction.atomic():
        locked_batch = ImportBatch.objects.select_for_update().get(pk=batch.pk)
        if locked_batch.status not in CANCELABLE_BATCH_STATUSES:
            raise ValidationError("Este lote no puede cancelarse en su estado actual.")

        file_ids = list(
            ImportedFile.objects.select_for_update()
            .filter(batch=locked_batch)
            .values_list("id", flat=True)
        )
        _ensure_no_definitive_entities(locked_batch, file_ids)

        duplicate_elements = DetectedStructureElement.objects.filter(batch=locked_batch)
        resolutions_deleted = ImportResolution.objects.filter(
            detected_element_id__in=duplicate_elements.values("id")
        ).delete()[0]
        detected_elements_deleted = duplicate_elements.delete()[0]
        historical_novelties_deleted = ImportedHistoricalNovelty.objects.filter(batch=locked_batch).delete()[0]
        novelties_deleted = ImportNovelty.objects.filter(batch=locked_batch).delete()[0]
        row_issues_deleted = ImportRowIssue.objects.filter(imported_file_id__in=file_ids).delete()[0]
        sheet_results_deleted = ImportedSheetResult.objects.filter(imported_file_id__in=file_ids).delete()[0]
        files_deleted = ImportedFile.objects.filter(id__in=file_ids).delete()[0]
        batch_id = locked_batch.pk
        locked_batch.delete()

    return ImportCancellationResult(
        batch_id=batch_id,
        files_deleted=files_deleted,
        sheet_results_deleted=sheet_results_deleted,
        row_issues_deleted=row_issues_deleted,
        detected_elements_deleted=detected_elements_deleted,
        resolutions_deleted=resolutions_deleted,
        novelties_deleted=novelties_deleted,
        historical_novelties_deleted=historical_novelties_deleted,
    )


def _ensure_no_definitive_entities(batch: ImportBatch, file_ids: list[int]) -> None:
    if Payment.objects.filter(source_file_id__in=file_ids).exists():
        raise ValidationError("No es seguro cancelar un lote que ya tiene pagos asociados.")
    if ImportAppliedRecord.objects.filter(batch=batch).exists():
        raise ValidationError("No es seguro cancelar un lote que ya tiene trazabilidad definitiva asociada.")
    if ImportNovelty.objects.filter(batch=batch, payment__isnull=False).exists():
        raise ValidationError("No es seguro cancelar un lote que ya tiene novedades asociadas a pagos.")
