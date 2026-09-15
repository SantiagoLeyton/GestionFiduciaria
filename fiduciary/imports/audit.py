from __future__ import annotations

from core.audit import record_audit
from fiduciary.models import ImportAppliedRecord, ImportBatch, ImportedFile


AUDIT_SOURCE_COLUMN = "__AUDIT__"


def create_import_audit_event(
    *,
    batch: ImportBatch,
    imported_file: ImportedFile | None,
    entity_kind: str,
    action: str,
    entity: str,
    lines: list[str],
) -> ImportAppliedRecord:
    summary_lines = [f"Accion: {action}", f"Entidad: {entity}", *[line for line in lines if line]]
    record_audit(
        user=batch.imported_by or batch.initiated_by,
        action=action,
        entity=entity,
        obj=batch,
        description="\n".join(line for line in lines if line),
        context={
            "Archivo": imported_file.original_name if imported_file else "",
            "Lote": batch.pk,
        },
        summary=_lines_to_summary(lines),
        mirror_log_entry=True,
    )
    return ImportAppliedRecord.objects.create(
        batch=batch,
        imported_file=imported_file,
        entity_kind=entity_kind,
        action=ImportAppliedRecord.Action.CREATED,
        source_column=AUDIT_SOURCE_COLUMN,
        summary="\n".join(summary_lines),
    )


def is_import_audit_record(record: ImportAppliedRecord) -> bool:
    return record.source_column == AUDIT_SOURCE_COLUMN


def _lines_to_summary(lines: list[str]) -> dict:
    summary = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        summary[key.strip()] = value.strip()
    return summary
