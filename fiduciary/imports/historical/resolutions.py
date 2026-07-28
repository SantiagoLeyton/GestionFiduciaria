from django.db import transaction
from django.utils import timezone

from fiduciary.models import DetectedStructureElement, ImportBatch, ImportResolution
from real_estate.models import PropertyUnit, StructuralGroup

from .normalize import normalize_text


READY_STATES = {
    DetectedStructureElement.Status.AUTO_MATCHED,
    DetectedStructureElement.Status.RESOLVED,
    DetectedStructureElement.Status.IGNORED,
}

BLOCKED_STATES = {DetectedStructureElement.Status.DETECTED}


def apply_resolution_to_equivalent_elements(resolution: ImportResolution, user) -> int:
    detected = resolution.detected_element
    equivalents = DetectedStructureElement.objects.filter(
        batch=detected.batch,
        inferred_kind=detected.inferred_kind,
        normalized_value=detected.normalized_value,
        structural_context=detected.structural_context,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    ).select_related("resolution")
    with transaction.atomic():
        for element in equivalents:
            item_resolution = element.resolution
            _copy_resolution(resolution, item_resolution, user)
            _mark_element_from_resolution(element, item_resolution)
        updated_units = auto_resolve_new_units(detected.batch, user=user)
        update_batch_resolution_state(detected.batch)
    return updated_units


def resolve_structural_group(
    *,
    resolution: ImportResolution,
    action,
    project,
    grouping_type,
    existing_group=None,
    new_group_name=None,
    resolved_by,
) -> int:
    detected = resolution.detected_element
    if detected.inferred_kind != DetectedStructureElement.InferredKind.STRUCTURAL_GROUP:
        raise ValueError("La resolucion estructural solo aplica a agrupaciones detectadas.")
    if action == ImportResolution.Action.ASSOCIATE_EXISTING and not existing_group:
        raise ValueError("Seleccione la agrupacion existente.")
    if action == ImportResolution.Action.CREATE_NEW and not (new_group_name or "").strip():
        raise ValueError("Registre el nombre de la nueva agrupacion.")
    if existing_group and (existing_group.project_id != project.pk or existing_group.grouping_type_id != grouping_type.pk):
        raise ValueError("La agrupacion existente no pertenece al proyecto y tipo seleccionados.")

    with transaction.atomic():
        resolution.target_kind = DetectedStructureElement.InferredKind.STRUCTURAL_GROUP
        resolution.action = action
        resolution.parent_project = project
        resolution.parent_grouping_type = grouping_type
        resolution.resolved_by = resolved_by
        resolution.resolved_at = timezone.now()
        resolution.status = ImportResolution.Status.APPLIED
        if action == ImportResolution.Action.ASSOCIATE_EXISTING:
            resolution.target_structural_group = existing_group
            resolution.create_code = ""
            resolution.create_name = ""
        else:
            resolution.target_structural_group = None
            resolution.create_code = detected.raw_value if detected.raw_value != "(sin valor)" else ""
            resolution.create_name = new_group_name.strip()
        resolution.save()
        _mark_element_from_resolution(detected, resolution)
        updated_units = apply_resolution_to_equivalent_elements(resolution, resolved_by)
        update_batch_resolution_state(detected.batch)
    return updated_units


def reanalyze_pending_resolutions(batch: ImportBatch, user=None) -> int:
    updated = 0
    with transaction.atomic():
        updated += _propagate_resolved_parent_context(batch)
        for element in batch.detected_elements.filter(
            status__in=[DetectedStructureElement.Status.NEEDS_REVIEW, DetectedStructureElement.Status.DETECTED],
            resolution__action=ImportResolution.Action.UNRESOLVED,
        ).select_related("resolution"):
            if element.inferred_kind == DetectedStructureElement.InferredKind.STRUCTURAL_GROUP:
                updated += _try_match_pending_group(element, user=user)
            elif element.inferred_kind == DetectedStructureElement.InferredKind.PROPERTY_UNIT:
                updated += _try_match_pending_unit(element, user=user)
        updated += auto_resolve_new_units(batch, user=user)
        update_batch_resolution_state(batch)
    return updated


def auto_resolve_new_units(batch: ImportBatch, user=None) -> int:
    candidates = DetectedStructureElement.objects.filter(
        batch=batch,
        inferred_kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        status__in=[DetectedStructureElement.Status.NEEDS_REVIEW, DetectedStructureElement.Status.DETECTED],
        resolution__action=ImportResolution.Action.UNRESOLVED,
    ).select_related("resolution")
    updated = 0
    for element in candidates:
        context = element.structural_context or {}
        project_id = context.get("project_id")
        group_id = context.get("structural_group_id")
        grouping_name = context.get("grouping_name")
        if not project_id:
            continue
        parent_group_resolution = _resolved_group_for_unit(batch, grouping_name, project_id)
        if parent_group_resolution:
            updated += _resolve_unit_from_parent_group(element, parent_group_resolution, user=user)
            continue
        if not group_id:
            continue
        resolution = element.resolution
        resolution.action = ImportResolution.Action.CREATE_NEW
        resolution.target_kind = DetectedStructureElement.InferredKind.PROPERTY_UNIT
        resolution.parent_project_id = project_id
        resolution.parent_structural_group_id = group_id
        resolution.create_code = element.raw_value if element.raw_value != "(sin valor)" else ""
        resolution.create_name = element.raw_value if element.raw_value != "(sin valor)" else ""
        resolution.resolved_by = user
        resolution.resolved_at = timezone.now()
        resolution.status = ImportResolution.Status.APPLIED
        resolution.save()
        element.status = DetectedStructureElement.Status.RESOLVED
        element.save(update_fields=["status", "updated_at"])
        updated += 1
    return updated


def auto_resolve_units_for_group_resolution(group_resolution: ImportResolution, user=None) -> int:
    group_element = group_resolution.detected_element
    project = group_resolution.parent_project or (group_resolution.target_structural_group.project if group_resolution.target_structural_group else None)
    if not project:
        return 0
    units = DetectedStructureElement.objects.filter(
        batch=group_element.batch,
        inferred_kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        status__in=[DetectedStructureElement.Status.NEEDS_REVIEW, DetectedStructureElement.Status.DETECTED],
        resolution__action=ImportResolution.Action.UNRESOLVED,
        structural_context__grouping_name=group_element.raw_value,
    ).select_related("resolution")
    updated = 0
    for unit in units:
        updated += _resolve_unit_from_parent_group(unit, group_resolution, user=user)
    return updated


def update_batch_resolution_state(batch: ImportBatch) -> None:
    has_pending = batch.detected_elements.filter(status=DetectedStructureElement.Status.NEEDS_REVIEW).exists()
    has_blocked = batch.detected_elements.filter(status__in=BLOCKED_STATES, resolution__action=ImportResolution.Action.UNRESOLVED).exists()
    batch.status = ImportBatch.Status.AWAITING_RESOLUTION if has_pending or has_blocked else ImportBatch.Status.READY
    batch.save(update_fields=["status"])


def _propagate_resolved_parent_context(batch: ImportBatch) -> int:
    updated = 0
    project_id = _resolved_project_id(batch)
    grouping_type_id = _resolved_grouping_type_id(batch)
    for element in batch.detected_elements.filter(
        inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        resolution__action=ImportResolution.Action.UNRESOLVED,
    ):
        context = dict(element.structural_context or {})
        changed = False
        if project_id and not context.get("project_id"):
            context["project_id"] = project_id
            changed = True
        if grouping_type_id and not context.get("grouping_type_id"):
            context["grouping_type_id"] = grouping_type_id
            changed = True
        if changed:
            element.structural_context = context
            element.save(update_fields=["structural_context", "updated_at"])
            updated += 1
    for element in batch.detected_elements.filter(
        inferred_kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        resolution__action=ImportResolution.Action.UNRESOLVED,
    ):
        context = dict(element.structural_context or {})
        if project_id and not context.get("project_id"):
            context["project_id"] = project_id
            element.structural_context = context
            element.save(update_fields=["structural_context", "updated_at"])
            updated += 1
    return updated


def _resolved_project_id(batch: ImportBatch) -> int | None:
    elements = batch.detected_elements.filter(
        inferred_kind=DetectedStructureElement.InferredKind.PROJECT,
        status__in=[DetectedStructureElement.Status.AUTO_MATCHED, DetectedStructureElement.Status.RESOLVED],
        resolution__action=ImportResolution.Action.ASSOCIATE_EXISTING,
        resolution__target_project__isnull=False,
    ).select_related("resolution")
    ids = {element.resolution.target_project_id for element in elements}
    return next(iter(ids)) if len(ids) == 1 else None


def _resolved_grouping_type_id(batch: ImportBatch) -> int | None:
    elements = batch.detected_elements.filter(
        inferred_kind=DetectedStructureElement.InferredKind.GROUPING_TYPE,
        status__in=[DetectedStructureElement.Status.AUTO_MATCHED, DetectedStructureElement.Status.RESOLVED],
        resolution__action=ImportResolution.Action.ASSOCIATE_EXISTING,
        resolution__target_grouping_type__isnull=False,
    ).select_related("resolution")
    ids = {element.resolution.target_grouping_type_id for element in elements}
    return next(iter(ids)) if len(ids) == 1 else None


def _try_match_pending_group(element: DetectedStructureElement, user=None) -> int:
    resolution = element.resolution
    context = element.structural_context or {}
    project_id = context.get("project_id") or getattr(resolution.parent_project, "pk", None)
    grouping_type_id = context.get("grouping_type_id") or getattr(resolution.parent_grouping_type, "pk", None)
    if not project_id or not grouping_type_id:
        return 0
    candidates = _groups_by_normalized_value(element.raw_value, project_id, grouping_type_id)
    if len(candidates) != 1:
        return 0
    group = candidates[0]
    resolution.action = ImportResolution.Action.ASSOCIATE_EXISTING
    resolution.target_kind = DetectedStructureElement.InferredKind.STRUCTURAL_GROUP
    resolution.target_structural_group = group
    resolution.parent_project_id = group.project_id
    resolution.parent_grouping_type_id = group.grouping_type_id
    resolution.resolved_by = user
    resolution.resolved_at = timezone.now()
    resolution.status = ImportResolution.Status.APPLIED
    resolution.save()
    _mark_element_from_resolution(element, resolution)
    return 1


def _try_match_pending_unit(element: DetectedStructureElement, user=None) -> int:
    context = element.structural_context or {}
    project_id = context.get("project_id")
    grouping_name = context.get("grouping_name")
    parent_group_resolution = _resolved_group_for_unit(element.batch, grouping_name, project_id)
    if not parent_group_resolution:
        if element.status == DetectedStructureElement.Status.NEEDS_REVIEW:
            element.status = DetectedStructureElement.Status.DETECTED
            element.save(update_fields=["status", "updated_at"])
            return 1
        return 0
    return _resolve_unit_from_parent_group(element, parent_group_resolution, user=user)


def _resolve_unit_from_parent_group(element: DetectedStructureElement, group_resolution: ImportResolution, user=None) -> int:
    project = group_resolution.parent_project or (
        group_resolution.target_structural_group.project if group_resolution.target_structural_group else None
    )
    if not project:
        return 0
    resolution = element.resolution
    matches = []
    if group_resolution.action == ImportResolution.Action.ASSOCIATE_EXISTING and group_resolution.target_structural_group_id:
        matches = _units_by_normalized_value(element.raw_value, project.pk, group_resolution.target_structural_group_id)
    if len(matches) > 1:
        return 0
    resolution.target_kind = DetectedStructureElement.InferredKind.PROPERTY_UNIT
    resolution.parent_project = project
    resolution.resolved_by = user
    resolution.resolved_at = timezone.now()
    resolution.status = ImportResolution.Status.APPLIED
    if len(matches) == 1:
        resolution.action = ImportResolution.Action.ASSOCIATE_EXISTING
        resolution.target_property_unit = matches[0]
        resolution.parent_structural_group = matches[0].structural_group
        resolution.create_code = ""
        resolution.create_name = ""
    else:
        resolution.action = ImportResolution.Action.CREATE_NEW
        resolution.target_property_unit = None
        resolution.parent_structural_group = group_resolution.target_structural_group
        resolution.create_code = element.raw_value if element.raw_value != "(sin valor)" else ""
        resolution.create_name = element.raw_value if element.raw_value != "(sin valor)" else ""
        context = element.structural_context or {}
        context["parent_group_resolution_id"] = group_resolution.pk
        context["parent_group_name"] = group_resolution.create_name or group_resolution.detected_element.raw_value
        element.structural_context = context
    resolution.save()
    _mark_element_from_resolution(element, resolution)
    return 1


def _resolved_group_for_unit(batch: ImportBatch, grouping_name, project_id=None) -> ImportResolution | None:
    if not grouping_name:
        return None
    group_elements = DetectedStructureElement.objects.filter(
        batch=batch,
        inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        normalized_value=normalize_text(grouping_name),
        status__in=[DetectedStructureElement.Status.AUTO_MATCHED, DetectedStructureElement.Status.RESOLVED],
    ).select_related("resolution", "resolution__parent_project", "resolution__target_structural_group")
    if project_id:
        group_elements = group_elements.filter(
            resolution__parent_project_id=project_id
        ) | group_elements.filter(resolution__target_structural_group__project_id=project_id)
    resolutions = [element.resolution for element in group_elements if element.resolution.action != ImportResolution.Action.UNRESOLVED]
    return resolutions[0] if len(resolutions) == 1 else None


def _groups_by_normalized_value(raw_value, project_id, grouping_type_id):
    normalized = normalize_text(raw_value)
    groups = StructuralGroup.objects.filter(project_id=project_id, grouping_type_id=grouping_type_id)
    matches = [
        group
        for group in groups
        if normalize_text(group.code) == normalized or normalize_text(group.name) == normalized or normalize_text(str(group)) == normalized
    ]
    return matches


def _units_by_normalized_value(raw_value, project_id, group_id):
    normalized = normalize_text(raw_value)
    units = PropertyUnit.objects.filter(project_id=project_id, structural_group_id=group_id)
    return [unit for unit in units if normalize_text(unit.code) == normalized or normalize_text(unit.name) == normalized]


def _copy_resolution(source: ImportResolution, target: ImportResolution, user) -> None:
    target.action = source.action
    target.target_kind = source.target_kind
    target.target_project = source.target_project
    target.target_grouping_type = source.target_grouping_type
    target.target_structural_group = source.target_structural_group
    target.target_property_unit = source.target_property_unit
    target.parent_project = source.parent_project
    target.parent_grouping_type = source.parent_grouping_type
    target.parent_structural_group = source.parent_structural_group
    target.create_code = source.create_code.strip()
    target.create_name = source.create_name.strip()
    target.resolved_by = user
    target.resolved_at = timezone.now()
    target.status = ImportResolution.Status.APPLIED
    target.save()


def _mark_element_from_resolution(element: DetectedStructureElement, resolution: ImportResolution) -> None:
    element.inferred_kind = resolution.target_kind
    if resolution.action == ImportResolution.Action.IGNORE:
        element.status = DetectedStructureElement.Status.IGNORED
    else:
        element.status = DetectedStructureElement.Status.RESOLVED
    element.save(update_fields=["inferred_kind", "status", "structural_context", "updated_at"])
