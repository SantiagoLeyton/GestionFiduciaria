from django.conf import settings

from fiduciary.models import DetectedStructureElement, ImportBatch, ImportedFile, ImportResolution, ImportRowIssue


def has_unresolved_required_pendings(batch: ImportBatch) -> bool:
    return batch.detected_elements.filter(status=DetectedStructureElement.Status.NEEDS_REVIEW).exists()


def has_blocked_dependencies(batch: ImportBatch) -> bool:
    return batch.detected_elements.filter(
        status=DetectedStructureElement.Status.DETECTED,
        resolution__action=ImportResolution.Action.UNRESOLVED,
    ).exists()


def has_open_blocking_issues(batch: ImportBatch) -> bool:
    return batch.files.filter(
        row_issues__severity=ImportRowIssue.Severity.BLOCKING,
        row_issues__status=ImportRowIssue.Status.OPEN,
    ).exists()


def has_failed_historical_files(batch: ImportBatch) -> bool:
    return batch.files.filter(
        file_type=ImportedFile.FileType.HISTORICAL,
        status=ImportedFile.Status.FAILED,
    ).exists()


def has_stored_historical_file(batch: ImportBatch) -> bool:
    return batch.files.filter(
        file_type=ImportedFile.FileType.HISTORICAL,
    ).exclude(stored_path="").exists()


def has_available_stored_historical_file(batch: ImportBatch) -> bool:
    for imported_file in batch.files.filter(file_type=ImportedFile.FileType.HISTORICAL).exclude(stored_path=""):
        if (settings.MEDIA_ROOT / imported_file.stored_path).exists():
            return True
    return False


def has_historical_finalization_blockers(batch: ImportBatch) -> bool:
    return (
        has_unresolved_required_pendings(batch)
        or has_blocked_dependencies(batch)
        or has_open_blocking_issues(batch)
        or has_failed_historical_files(batch)
    )


def has_historical_finalization_prerequisites(batch: ImportBatch, *, require_stored_file: bool = True) -> bool:
    if batch.import_type != ImportBatch.ImportType.HISTORICAL:
        return False
    if has_historical_finalization_blockers(batch):
        return False
    if require_stored_file and not has_available_stored_historical_file(batch):
        return False
    return True


def can_start_historical_finalization(batch: ImportBatch, *, require_stored_file: bool = True) -> bool:
    return batch.status == ImportBatch.Status.READY and has_historical_finalization_prerequisites(
        batch,
        require_stored_file=require_stored_file,
    )


def can_continue_historical_finalization(batch: ImportBatch, *, require_stored_file: bool = True) -> bool:
    return batch.status == ImportBatch.Status.PROCESSING and has_historical_finalization_prerequisites(
        batch,
        require_stored_file=require_stored_file,
    )


def can_finalize_historical_import_batch(batch: ImportBatch, *, require_stored_file: bool = True) -> bool:
    return can_start_historical_finalization(batch, require_stored_file=require_stored_file)
