from dataclasses import dataclass

from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from real_estate.models import Project, PropertyUnit, StructuralGroup

from fiduciary.models import (
    Client,
    DailyReportRow,
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportedHistoricalObservation,
    ImportNovelty,
    OperationalNovelty,
    Payment,
    UnitOwnership,
)
from fiduciary.imports.audit import create_import_audit_event


@dataclass
class ImportReversionSummary:
    payments: int = 0
    observations: int = 0
    novelties: int = 0
    assignment_holders: int = 0
    ownerships: int = 0
    assignments: int = 0
    units: int = 0
    groups: int = 0
    projects: int = 0
    preserved_clients: int = 0
    preserved_shared: int = 0

    def as_dict(self):
        return {
            "pagos": self.payments,
            "observaciones": self.observations,
            "novedades": self.novelties,
            "titulares_encargo": self.assignment_holders,
            "titularidades": self.ownerships,
            "encargos": self.assignments,
            "unidades": self.units,
            "agrupaciones": self.groups,
            "proyectos": self.projects,
            "clientes_conservados": self.preserved_clients,
            "registros_compartidos_conservados": self.preserved_shared,
        }

    def text(self):
        return ", ".join(f"{label}: {count}" for label, count in self.as_dict().items() if count) or "sin eliminaciones"


REVERSIBLE_STATUSES = {
    ImportBatch.Status.COMPLETED,
    ImportBatch.Status.COMPLETED_WITH_ISSUES,
}


def import_reversion_summary(batch: ImportBatch) -> ImportReversionSummary:
    created_payments = _created_ids(batch, ImportAppliedRecord.EntityKind.PAYMENT)
    created_clients = _created_ids(batch, ImportAppliedRecord.EntityKind.CLIENT)
    return ImportReversionSummary(
        payments=Payment.objects.filter(pk__in=created_payments).count(),
        observations=ImportedHistoricalObservation.objects.filter(batch=batch).count(),
        novelties=OperationalNovelty.objects.filter(batch=batch).count(),
        assignment_holders=_exclusive_count(batch, ImportAppliedRecord.EntityKind.ASSIGNMENT_HOLDER),
        ownerships=_exclusive_count(batch, ImportAppliedRecord.EntityKind.UNIT_OWNERSHIP),
        assignments=_exclusive_count(batch, ImportAppliedRecord.EntityKind.FIDUCIARY_ASSIGNMENT),
        units=_exclusive_count(batch, ImportAppliedRecord.EntityKind.PROPERTY_UNIT),
        groups=_exclusive_count(batch, ImportAppliedRecord.EntityKind.STRUCTURAL_GROUP),
        projects=_exclusive_count(batch, ImportAppliedRecord.EntityKind.PROJECT),
        preserved_clients=Client.objects.filter(pk__in=created_clients).count(),
    )


@transaction.atomic
def revert_import_batch(*, batch: ImportBatch, user, reason: str = "") -> ImportReversionSummary:
    locked = ImportBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.status not in REVERSIBLE_STATUSES:
        raise ValidationError("Solo se pueden deshacer importaciones definitivas completadas.")

    summary = import_reversion_summary(locked)

    payment_ids = list(_created_ids(locked, ImportAppliedRecord.EntityKind.PAYMENT))
    ImportNovelty.objects.filter(payment_id__in=payment_ids).delete()
    DailyReportRow.objects.filter(payment_id__in=payment_ids).update(
        payment=None,
        status=DailyReportRow.Status.VALID,
        message="Pago eliminado al deshacer la importacion.",
    )
    ImportedHistoricalObservation.related_payments.through.objects.filter(payment_id__in=payment_ids).delete()
    summary.payments = Payment.objects.filter(pk__in=payment_ids).delete()[0]

    summary.observations = ImportedHistoricalObservation.objects.filter(batch=locked).delete()[0]
    summary.novelties = OperationalNovelty.objects.filter(batch=locked).delete()[0]

    summary.assignment_holders = _delete_exclusive(
        locked,
        ImportAppliedRecord.EntityKind.ASSIGNMENT_HOLDER,
        FiduciaryAssignmentHolder,
    )
    summary.ownerships = _delete_exclusive(locked, ImportAppliedRecord.EntityKind.UNIT_OWNERSHIP, UnitOwnership)
    summary.assignments = _delete_exclusive(locked, ImportAppliedRecord.EntityKind.FIDUCIARY_ASSIGNMENT, FiduciaryAssignment)
    summary.units = _delete_exclusive(locked, ImportAppliedRecord.EntityKind.PROPERTY_UNIT, PropertyUnit)
    summary.groups = _delete_exclusive(locked, ImportAppliedRecord.EntityKind.STRUCTURAL_GROUP, StructuralGroup)
    summary.projects = _delete_exclusive(locked, ImportAppliedRecord.EntityKind.PROJECT, Project)

    now = timezone.now()
    locked.status = ImportBatch.Status.REVERTED
    locked.reverted_at = now
    locked.reverted_by = user
    locked.summary = f"Importacion deshecha. Eliminado: {summary.text()}."
    locked.save(update_fields=["status", "reverted_at", "reverted_by", "summary"])
    ImportedFile.objects.filter(batch=locked).update(status=ImportedFile.Status.REVERTED, result_message=locked.summary)
    imported_file = locked.files.order_by("order", "pk").first()
    entity = "Libro historico" if locked.import_type == ImportBatch.ImportType.HISTORICAL else "Reporte fiduciario"
    entity_kind = ImportAppliedRecord.EntityKind.PROJECT if locked.import_type == ImportBatch.ImportType.HISTORICAL else ImportAppliedRecord.EntityKind.PAYMENT
    create_import_audit_event(
        batch=locked,
        imported_file=imported_file,
        entity_kind=entity_kind,
        action="Deshecho",
        entity=entity,
        lines=[
            "Descripcion: Importacion definitiva deshecha.",
            f"Archivo: {imported_file.original_name if imported_file else '-'}",
            f"Resultado: {locked.get_status_display()}",
            f"Pagos eliminados: {summary.payments}",
            f"Novedades eliminadas: {summary.novelties}",
            f"Observaciones eliminadas: {summary.observations}",
            f"Encargos eliminados: {summary.assignments}",
            f"Unidades eliminadas: {summary.units}",
            f"Agrupaciones eliminadas: {summary.groups}",
            f"Proyectos eliminados: {summary.projects}",
            f"Clientes conservados: {summary.preserved_clients}",
            f"Motivo: {reason.strip()}" if reason else "",
        ],
    )
    _log_reversion(user, locked, summary, reason)
    return summary


def _created_ids(batch: ImportBatch, kind: str) -> set[int]:
    return set(
        ImportAppliedRecord.objects.filter(
            batch=batch,
            entity_kind=kind,
            action=ImportAppliedRecord.Action.CREATED,
            entity_id__isnull=False,
        ).values_list("entity_id", flat=True)
    )


def _exclusive_count(batch: ImportBatch, kind: str) -> int:
    ids = _created_ids(batch, kind)
    return sum(1 for entity_id in ids if not _has_other_trace(batch, kind, entity_id))


def _delete_exclusive(batch: ImportBatch, kind: str, model) -> int:
    deleted = 0
    for entity_id in sorted(_created_ids(batch, kind), reverse=True):
        if _has_other_trace(batch, kind, entity_id):
            continue
        try:
            obj = model.objects.get(pk=entity_id)
        except model.DoesNotExist:
            continue
        try:
            obj.delete()
            deleted += 1
        except ProtectedError:
            continue
    return deleted


def _has_other_trace(batch: ImportBatch, kind: str, entity_id: int) -> bool:
    return ImportAppliedRecord.objects.exclude(batch=batch).filter(entity_kind=kind, entity_id=entity_id).exists()


def _log_reversion(user, batch: ImportBatch, summary: ImportReversionSummary, reason: str = "") -> None:
    content_type = ContentType.objects.get_for_model(LogEntry)
    message = f"DESHACER_IMPORTACION | Lote {batch.pk} | Afectado: {summary.text()}"
    if reason:
        message = f"{message} | Motivo: {reason.strip()}"
    LogEntry.objects.create(
        user_id=user.pk,
        content_type=content_type,
        object_id=str(batch.pk),
        object_repr=f"Importacion {batch.pk}",
        action_flag=DELETION,
        change_message=message,
    )
