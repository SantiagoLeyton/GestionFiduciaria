from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q
from core.audit import record_audit

from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup

from .models import (
    Client,
    DailyReportRow,
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportNovelty,
    ImportResolution,
    ImportedHistoricalObservation,
    OperationalNovelty,
    Payment,
    UnitOwnership,
)


@dataclass
class CleanupSummary:
    projects: int = 0
    structural_groups: int = 0
    property_units: int = 0
    assignments: int = 0
    assignment_holders: int = 0
    ownerships: int = 0
    payments: int = 0
    daily_report_rows: int = 0
    import_novelties: int = 0
    operational_novelties: int = 0
    observations: int = 0
    import_resolutions: int = 0
    clients: int = 0

    def as_dict(self):
        return {
            "proyectos": self.projects,
            "agrupaciones": self.structural_groups,
            "unidades": self.property_units,
            "encargos": self.assignments,
            "titulares_encargo": self.assignment_holders,
            "titularidades": self.ownerships,
            "pagos": self.payments,
            "filas_reporte_diario": self.daily_report_rows,
            "novedades_importacion": self.import_novelties,
            "novedades_operativas": self.operational_novelties,
            "observaciones": self.observations,
            "resoluciones_importacion": self.import_resolutions,
            "clientes": self.clients,
        }

    def text(self):
        return ", ".join(f"{label}: {count}" for label, count in self.as_dict().items() if count)


def project_cleanup_summary(project: Project) -> CleanupSummary:
    units = PropertyUnit.objects.filter(project=project)
    assignments = FiduciaryAssignment.objects.filter(property_unit__project=project)
    groups = StructuralGroup.objects.filter(project=project)
    return CleanupSummary(
        projects=1,
        structural_groups=groups.count(),
        property_units=units.count(),
        assignments=assignments.count(),
        assignment_holders=FiduciaryAssignmentHolder.objects.filter(assignment__in=assignments).count(),
        ownerships=UnitOwnership.objects.filter(property_unit__in=units).count(),
        payments=Payment.objects.filter(assignment__in=assignments).count(),
        daily_report_rows=DailyReportRow.objects.filter(Q(assignment__in=assignments) | Q(payment__assignment__in=assignments)).count(),
        import_novelties=ImportNovelty.objects.filter(
            Q(property_unit__in=units) | Q(assignment__in=assignments) | Q(payment__assignment__in=assignments)
        ).count(),
        operational_novelties=OperationalNovelty.objects.filter(Q(project=project) | Q(property_unit__in=units)).count(),
        observations=ImportedHistoricalObservation.objects.filter(Q(project=project) | Q(property_unit__in=units)).count(),
        import_resolutions=ImportResolution.objects.filter(
            Q(target_project=project)
            | Q(parent_project=project)
            | Q(target_structural_group__in=groups)
            | Q(parent_structural_group__in=groups)
            | Q(target_property_unit__in=units)
        ).count(),
    )


def structural_group_cleanup_summary(group: StructuralGroup) -> CleanupSummary:
    groups = _group_subtree(group)
    units = PropertyUnit.objects.filter(structural_group__in=groups)
    assignments = FiduciaryAssignment.objects.filter(property_unit__in=units)
    return CleanupSummary(
        structural_groups=groups.count(),
        property_units=units.count(),
        assignments=assignments.count(),
        assignment_holders=FiduciaryAssignmentHolder.objects.filter(assignment__in=assignments).count(),
        ownerships=UnitOwnership.objects.filter(property_unit__in=units).count(),
        payments=Payment.objects.filter(assignment__in=assignments).count(),
        daily_report_rows=DailyReportRow.objects.filter(Q(assignment__in=assignments) | Q(payment__assignment__in=assignments)).count(),
        import_novelties=ImportNovelty.objects.filter(
            Q(property_unit__in=units) | Q(assignment__in=assignments) | Q(payment__assignment__in=assignments)
        ).count(),
        operational_novelties=OperationalNovelty.objects.filter(property_unit__in=units).count(),
        observations=ImportedHistoricalObservation.objects.filter(property_unit__in=units).count(),
        import_resolutions=ImportResolution.objects.filter(
            Q(target_structural_group__in=groups) | Q(parent_structural_group__in=groups) | Q(target_property_unit__in=units)
        ).count(),
    )


def grouping_type_cleanup_summary(grouping_type: GroupingType) -> CleanupSummary:
    groups = StructuralGroup.objects.filter(grouping_type=grouping_type)
    return CleanupSummary(structural_groups=groups.count())


def property_unit_cleanup_summary(unit: PropertyUnit) -> CleanupSummary:
    assignments = FiduciaryAssignment.objects.filter(property_unit=unit)
    payments = Payment.objects.filter(assignment__in=assignments)
    return CleanupSummary(
        property_units=1,
        assignments=assignments.count(),
        assignment_holders=FiduciaryAssignmentHolder.objects.filter(assignment__in=assignments).count(),
        ownerships=UnitOwnership.objects.filter(property_unit=unit).count(),
        payments=payments.count(),
        daily_report_rows=DailyReportRow.objects.filter(Q(assignment__in=assignments) | Q(payment__in=payments)).count(),
        import_novelties=ImportNovelty.objects.filter(Q(property_unit=unit) | Q(assignment__in=assignments) | Q(payment__in=payments)).count(),
        operational_novelties=OperationalNovelty.objects.filter(property_unit=unit).count(),
        observations=ImportedHistoricalObservation.objects.filter(property_unit=unit).count(),
        import_resolutions=ImportResolution.objects.filter(target_property_unit=unit).count(),
    )


def assignment_cleanup_summary(assignment: FiduciaryAssignment) -> CleanupSummary:
    payments = Payment.objects.filter(assignment=assignment)
    return CleanupSummary(
        assignments=1,
        assignment_holders=assignment.holders.count(),
        payments=payments.count(),
        daily_report_rows=DailyReportRow.objects.filter(Q(assignment=assignment) | Q(payment__in=payments)).count(),
        import_novelties=ImportNovelty.objects.filter(Q(assignment=assignment) | Q(payment__in=payments)).count(),
        operational_novelties=OperationalNovelty.objects.filter(
            Q(previous_assignment=assignment) | Q(new_assignment=assignment) | Q(historical_assignment=assignment)
        ).count(),
        observations=ImportedHistoricalObservation.objects.filter(Q(assignment=assignment) | Q(related_payments__in=payments)).distinct().count(),
    )


def client_dependency_summary(client: Client) -> CleanupSummary:
    return CleanupSummary(
        clients=1,
        ownerships=client.unit_ownerships.count(),
        assignment_holders=client.fiduciary_assignment_holders.count(),
        import_novelties=client.import_novelties.count(),
        operational_novelties=OperationalNovelty.objects.filter(
            Q(previous_client=client) | Q(new_client=client) | Q(historical_client=client)
        ).count(),
        observations=client.historical_observations.count(),
    )


@transaction.atomic
def delete_project(project: Project, *, user, reason: str = "") -> CleanupSummary:
    summary = project_cleanup_summary(project)
    label = str(project)
    _delete_project_scope(PropertyUnit.objects.filter(project=project), StructuralGroup.objects.filter(project=project), project=project)
    project.delete()
    _log_deletion(user, "ELIMINAR_PROYECTO", label, summary, reason)
    return summary


@transaction.atomic
def delete_grouping_type_if_unused(grouping_type: GroupingType, *, user, reason: str = "") -> CleanupSummary:
    summary = grouping_type_cleanup_summary(grouping_type)
    if summary.structural_groups:
        return summary
    label = str(grouping_type)
    grouping_type.delete()
    _log_deletion(user, "ELIMINAR_TIPO_AGRUPACION", label, summary, reason)
    return summary


@transaction.atomic
def delete_property_unit(unit: PropertyUnit, *, user, reason: str = "") -> CleanupSummary:
    summary = property_unit_cleanup_summary(unit)
    label = f"{unit.project} / {unit}"
    _delete_project_scope(PropertyUnit.objects.filter(pk=unit.pk), StructuralGroup.objects.none())
    _log_deletion(user, "ELIMINAR_UNIDAD", label, summary, reason)
    return summary


@transaction.atomic
def unlink_structural_group(group: StructuralGroup, *, user, reason: str = "") -> CleanupSummary:
    summary = structural_group_cleanup_summary(group)
    label = f"{group.project} / {group}"
    groups = _group_subtree(group)
    units = PropertyUnit.objects.filter(structural_group__in=groups)
    _delete_project_scope(units, groups)
    _log_deletion(user, "DESVINCULAR_AGRUPACION_PROYECTO", label, summary, reason)
    return summary


@transaction.atomic
def delete_assignment(assignment: FiduciaryAssignment, *, user, reason: str = "") -> CleanupSummary:
    summary = assignment_cleanup_summary(assignment)
    label = str(assignment)
    _delete_assignment_scope(FiduciaryAssignment.objects.filter(pk=assignment.pk))
    _log_deletion(user, "ELIMINAR_ENCARGO", label, summary, reason)
    return summary


@transaction.atomic
def delete_client_if_orphan(client: Client, *, user, reason: str = "") -> CleanupSummary:
    summary = client_dependency_summary(client)
    has_dependencies = any(count for key, count in summary.as_dict().items() if key != "clientes")
    if has_dependencies:
        return summary
    label = str(client)
    client.delete()
    _log_deletion(user, "ELIMINAR_CLIENTE", label, summary, reason)
    return summary


def _delete_project_scope(units, groups, *, project: Project | None = None):
    assignments = FiduciaryAssignment.objects.filter(property_unit__in=units)
    resolution_filter = Q(target_structural_group__in=groups) | Q(parent_structural_group__in=groups) | Q(target_property_unit__in=units)
    if project is not None:
        resolution_filter |= Q(target_project=project) | Q(parent_project=project)
    ImportResolution.objects.filter(resolution_filter).delete()
    _delete_assignment_scope(assignments)
    novelty_filter = Q(property_unit__in=units)
    observation_filter = Q(property_unit__in=units)
    if project is not None:
        novelty_filter |= Q(project=project)
        observation_filter |= Q(project=project)
    OperationalNovelty.objects.filter(novelty_filter).delete()
    ImportedHistoricalObservation.objects.filter(observation_filter).delete()
    UnitOwnership.objects.filter(property_unit__in=units).delete()
    units.delete()
    for group in groups.order_by("-pk"):
        group.delete()


def _delete_assignment_scope(assignments):
    payments = Payment.objects.filter(assignment__in=assignments)
    OperationalNovelty.objects.filter(
        Q(previous_assignment__in=assignments) | Q(new_assignment__in=assignments) | Q(historical_assignment__in=assignments)
    ).delete()
    ImportedHistoricalObservation.objects.filter(Q(assignment__in=assignments) | Q(related_payments__in=payments)).distinct().delete()
    ImportNovelty.objects.filter(Q(assignment__in=assignments) | Q(payment__in=payments)).delete()
    DailyReportRow.objects.filter(Q(assignment__in=assignments) | Q(payment__in=payments)).delete()
    FiduciaryAssignmentHolder.objects.filter(assignment__in=assignments).delete()
    payments.delete()
    assignments.delete()


def _group_subtree(group: StructuralGroup):
    ids = {group.pk}
    added = True
    while added:
        children = set(StructuralGroup.objects.filter(parent_id__in=ids).values_list("pk", flat=True))
        added = bool(children - ids)
        ids |= children
    return StructuralGroup.objects.filter(pk__in=ids)


def _log_deletion(user, action: str, object_label: str, summary: CleanupSummary, reason: str = "") -> None:
    entity = {
        "ELIMINAR_PROYECTO": "Proyecto",
        "DESVINCULAR_AGRUPACION_PROYECTO": "Agrupacion del proyecto",
        "ELIMINAR_ENCARGO": "Encargo fiduciario",
        "ELIMINAR_CLIENTE": "Cliente",
        "ELIMINAR_TIPO_AGRUPACION": "Tipo de agrupacion",
        "ELIMINAR_UNIDAD": "Unidad inmobiliaria",
    }.get(action, "Registro")
    record_audit(
        user=user,
        action="Eliminado",
        entity=entity,
        entity_repr=object_label,
        description=f"{action}: {object_label}",
        reason=reason,
        summary=summary.as_dict(),
    )
