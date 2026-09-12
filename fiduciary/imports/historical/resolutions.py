from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
import re

from fiduciary.models import DetectedStructureElement, ImportAppliedRecord, ImportBatch, ImportResolution
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup

from .normalize import normalize_text
from .readiness import has_historical_finalization_blockers


READY_STATES = {
    DetectedStructureElement.Status.AUTO_MATCHED,
    DetectedStructureElement.Status.RESOLVED,
    DetectedStructureElement.Status.IGNORED,
}

BLOCKED_STATES = {DetectedStructureElement.Status.DETECTED}


class ImmediateResolutionError(ValueError):
    pass


def create_immediate_structure_from_resolution(resolution: ImportResolution, user, *, apply_equivalents: bool = False) -> str | None:
    detected = resolution.detected_element
    if resolution.action != ImportResolution.Action.CREATE_NEW:
        return None
    if resolution.target_kind == DetectedStructureElement.InferredKind.PROJECT:
        return _create_project_from_resolution(resolution, user, apply_equivalents=apply_equivalents)
    if resolution.target_kind == DetectedStructureElement.InferredKind.GROUPING_TYPE:
        return _create_grouping_type_from_resolution(resolution, user, apply_equivalents=apply_equivalents)
    return None


def _create_project_from_resolution(resolution: ImportResolution, user, *, apply_equivalents: bool) -> str:
    code = (resolution.create_code or "").strip()
    name = (resolution.create_name or "").strip()
    if not code or not name:
        raise ImmediateResolutionError("Para crear el proyecto debe registrar codigo y nombre.")
    if Project.objects.filter(code=code).exists():
        raise ImmediateResolutionError(
            "Ya existe un proyecto con ese codigo. Asocielo con el registro existente."
        )
    with transaction.atomic():
        project = Project(code=code, name=name, description="", is_active=True)
        try:
            project.save()
        except (ValidationError, IntegrityError) as exc:
            raise ImmediateResolutionError(_readable_model_error(exc)) from exc
        _convert_created_resolution_to_existing(
            resolution=resolution,
            target_kind=DetectedStructureElement.InferredKind.PROJECT,
            target_field="target_project",
            target=project,
            user=user,
        )
        _trace_immediate_creation(
            resolution,
            entity_kind=ImportAppliedRecord.EntityKind.PROJECT,
            entity_id=project.pk,
            code=project.code,
            name=project.name,
            user=user,
        )
        if apply_equivalents:
            apply_resolution_to_equivalent_elements(resolution, user)
        update_batch_resolution_state(resolution.detected_element.batch)
    return f"Proyecto creado correctamente: {project}."


def _create_grouping_type_from_resolution(resolution: ImportResolution, user, *, apply_equivalents: bool) -> str:
    code = (resolution.create_code or "").strip()
    name = (resolution.create_name or "").strip()
    if not code or not name:
        raise ImmediateResolutionError("Para crear el tipo de agrupacion debe registrar codigo y nombre.")
    if GroupingType.objects.filter(code=code).exists():
        raise ImmediateResolutionError(
            "Ya existe un tipo de agrupacion con ese codigo. Asocielo con el registro existente."
        )
    with transaction.atomic():
        grouping_type = GroupingType(code=code, name=name, description="", is_active=True)
        try:
            grouping_type.save()
        except (ValidationError, IntegrityError) as exc:
            raise ImmediateResolutionError(_readable_model_error(exc)) from exc
        _convert_created_resolution_to_existing(
            resolution=resolution,
            target_kind=DetectedStructureElement.InferredKind.GROUPING_TYPE,
            target_field="target_grouping_type",
            target=grouping_type,
            user=user,
        )
        _trace_immediate_creation(
            resolution,
            entity_kind=ImportAppliedRecord.EntityKind.GROUPING_TYPE,
            entity_id=grouping_type.pk,
            code=grouping_type.code,
            name=grouping_type.name,
            user=user,
        )
        if apply_equivalents:
            apply_resolution_to_equivalent_elements(resolution, user)
        update_batch_resolution_state(resolution.detected_element.batch)
    return f"Tipo de agrupacion creado correctamente: {grouping_type}."


def _convert_created_resolution_to_existing(*, resolution, target_kind, target_field, target, user) -> None:
    resolution.action = ImportResolution.Action.ASSOCIATE_EXISTING
    resolution.target_kind = target_kind
    resolution.target_project = None
    resolution.target_grouping_type = None
    resolution.target_structural_group = None
    resolution.target_property_unit = None
    setattr(resolution, target_field, target)
    resolution.parent_project = None
    resolution.parent_grouping_type = None
    resolution.parent_structural_group = None
    resolution.resolved_by = user
    resolution.resolved_at = timezone.now()
    resolution.status = ImportResolution.Status.APPLIED
    resolution.save()
    _mark_element_from_resolution(resolution.detected_element, resolution)


def _trace_immediate_creation(resolution, *, entity_kind, entity_id, code, name, user) -> None:
    detected = resolution.detected_element
    ImportAppliedRecord.objects.create(
        batch=detected.batch,
        entity_kind=entity_kind,
        entity_id=entity_id,
        action=ImportAppliedRecord.Action.CREATED,
        summary=(
            f"Creado durante resolucion de pendiente historico. "
            f"Codigo: {code}. Nombre: {name}. "
            f"Pendiente: {detected.pk}. Lote: {detected.batch_id}. "
            f"Responsable: {getattr(user, 'username', '')}."
        ),
    )


def _readable_model_error(exc) -> str:
    if isinstance(exc, ValidationError):
        if hasattr(exc, "message_dict"):
            return " ".join(str(message) for messages in exc.message_dict.values() for message in messages)
        return " ".join(str(message) for message in exc.messages)
    return "No fue posible crear el registro por una restriccion de integridad."


def apply_resolution_to_equivalent_elements(resolution: ImportResolution, user) -> int:
    detected = resolution.detected_element
    equivalents = equivalent_pending_elements(detected).select_related("resolution")
    with transaction.atomic():
        for element in equivalents:
            item_resolution = element.resolution
            _copy_resolution(resolution, item_resolution, user)
            _mark_element_from_resolution(element, item_resolution)
        updated_units = auto_resolve_new_units(detected.batch, user=user)
        update_batch_resolution_state(detected.batch)
    return updated_units


def apply_resolution_to_current_element(resolution: ImportResolution, user) -> None:
    with transaction.atomic():
        resolution.resolved_by = user
        resolution.resolved_at = timezone.now()
        resolution.status = ImportResolution.Status.APPLIED
        resolution.save()
        _mark_element_from_resolution(resolution.detected_element, resolution)


def equivalent_pending_elements(element: DetectedStructureElement):
    context = element.structural_context or {}
    queryset = DetectedStructureElement.objects.filter(
        batch=element.batch,
        inferred_kind=element.inferred_kind,
        normalized_value=element.normalized_value,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    ).exclude(pk=element.pk)
    if context.get("missing_grouping_type"):
        queryset = DetectedStructureElement.objects.filter(
            batch=element.batch,
            inferred_kind=element.inferred_kind,
            status=DetectedStructureElement.Status.NEEDS_REVIEW,
            structural_context__missing_grouping_type=True,
        ).exclude(pk=element.pk)
        project_id = context.get("project_id")
        project_name = context.get("project_name")
        if project_id:
            queryset = queryset.filter(structural_context__project_id=project_id)
        elif project_name:
            queryset = queryset.filter(structural_context__project_name=project_name)
        return queryset
    return queryset.filter(structural_context=context)


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


def structural_group_pattern_suggestions(
    *,
    source_element: DetectedStructureElement,
    project: Project,
    grouping_type: GroupingType,
    selected_group: StructuralGroup | None,
    new_group_name: str = "",
) -> list[dict]:
    source_patterns = _group_pattern(source_element.raw_value)
    source_token = _group_pattern_token(source_element.raw_value)
    source_prefixes = {pattern["prefix"] for pattern in source_patterns if pattern["token"] == source_token}
    if not source_token or not source_prefixes:
        return []

    create_template = None
    selected_prefixes: set[str] = set()
    if selected_group:
        selected_pattern = _group_pattern(selected_group.code, selected_group.name, str(selected_group))
        selected_tokens = {pattern["token"] for pattern in selected_pattern if pattern["token"]}
        selected_prefixes = {pattern["prefix"] for pattern in selected_pattern if pattern["token"] == source_token}
        selected_prefixes.discard("")
        if source_token not in selected_tokens:
            return []
    else:
        create_template = _create_group_pattern_template(new_group_name)
        if not create_template or create_template["token"] != source_token:
            return []

    groups = list(StructuralGroup.objects.filter(project=project, grouping_type=grouping_type).order_by("name", "code", "pk"))
    pending = DetectedStructureElement.objects.filter(
        batch=source_element.batch,
        inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    ).exclude(pk=source_element.pk)
    suggestions = []
    for element in pending:
        context = element.structural_context or {}
        context_project_id = context.get("project_id")
        context_grouping_type_id = context.get("grouping_type_id")
        if context_project_id and int(context_project_id) != project.pk:
            continue
        if context_grouping_type_id and int(context_grouping_type_id) != grouping_type.pk:
            continue
        token = _group_pattern_token(element.raw_value)
        if not token:
            continue
        if not _element_matches_source_pattern(element, source_prefixes):
            continue
        if create_template:
            suggestions.append(
                {
                    "element": element,
                    "action": ImportResolution.Action.CREATE_NEW,
                    "project": project,
                    "grouping_type": grouping_type,
                    "create_name": _format_created_group_name(create_template, element.raw_value),
                }
            )
            continue
        matches = [
            group
            for group in groups
            if _group_matches_learned_pattern(group, token, selected_prefixes)
        ]
        if len(matches) == 1:
            suggestions.append(
                {
                    "element": element,
                    "action": ImportResolution.Action.ASSOCIATE_EXISTING,
                    "target": matches[0],
                }
            )
    return suggestions


def structural_group_pattern_elements(source_element: DetectedStructureElement) -> list[DetectedStructureElement]:
    source_prefixes = {
        pattern["prefix"]
        for pattern in _group_pattern(source_element.raw_value)
        if pattern["prefix"]
    }
    if not source_prefixes:
        return []
    source_context = source_element.structural_context or {}
    project_id = source_context.get("project_id")
    queryset = DetectedStructureElement.objects.filter(
        batch=source_element.batch,
        inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    ).select_related("resolution")
    if project_id:
        queryset = queryset.filter(structural_context__project_id=project_id)
    return [
        element
        for element in queryset.order_by("normalized_value", "pk")
        if _element_matches_source_pattern(element, source_prefixes)
    ]


def structural_group_pattern_label(source_element: DetectedStructureElement) -> str:
    prefixes = [
        pattern["prefix"].upper()
        for pattern in _group_pattern(source_element.raw_value)
        if pattern["prefix"]
    ]
    return f"{prefixes[0]}*" if prefixes else source_element.raw_value


def apply_structural_group_pattern_suggestions(suggestions: list[dict], user) -> int:
    updated = 0
    with transaction.atomic():
        for suggestion in suggestions:
            element = suggestion["element"]
            resolution = element.resolution
            resolution.target_kind = DetectedStructureElement.InferredKind.STRUCTURAL_GROUP
            resolution.action = suggestion["action"]
            if suggestion["action"] == ImportResolution.Action.ASSOCIATE_EXISTING:
                target = suggestion["target"]
                resolution.target_structural_group = target
                resolution.parent_project = target.project
                resolution.parent_grouping_type = target.grouping_type
                resolution.create_code = ""
                resolution.create_name = ""
            else:
                resolution.target_structural_group = None
                resolution.parent_project = suggestion["project"]
                resolution.parent_grouping_type = suggestion["grouping_type"]
                resolution.create_code = element.raw_value if element.raw_value != "(sin valor)" else ""
                resolution.create_name = suggestion["create_name"]
            resolution.resolved_by = user
            resolution.resolved_at = timezone.now()
            resolution.status = ImportResolution.Status.APPLIED
            resolution.save()
            _mark_element_from_resolution(element, resolution)
            updated += 1
        if suggestions:
            auto_resolve_new_units(suggestions[0]["element"].batch, user=user)
            update_batch_resolution_state(suggestions[0]["element"].batch)
    return updated


def _group_pattern_token(value) -> str:
    normalized = normalize_text(value)
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    match = re.search(r"(\d+|[a-z])$", compact)
    if not match:
        return ""
    token = match.group(1)
    if token.isdigit():
        return str(int(token))
    return token


def _group_pattern(*values) -> list[dict[str, str]]:
    patterns = []
    seen = set()
    for value in values:
        normalized = normalize_text(value)
        compact = re.sub(r"[^a-z0-9]+", "", normalized)
        match = re.search(r"^(?P<prefix>[a-z]*)(?P<token>\d+|[a-z])$", compact)
        if not match:
            token = _group_pattern_token(value)
            prefix = ""
        else:
            prefix = match.group("prefix")
            token = match.group("token")
            if token.isdigit():
                token = str(int(token))
        if not token:
            continue
        key = (prefix, token)
        if key in seen:
            continue
        seen.add(key)
        patterns.append({"prefix": prefix, "token": token})
    return patterns


def _group_matches_learned_pattern(group: StructuralGroup, token: str, selected_prefixes: set[str]) -> bool:
    patterns = _group_pattern(group.code, group.name, str(group))
    if not selected_prefixes:
        return any(pattern["token"] == token for pattern in patterns)
    return any(pattern["token"] == token and pattern["prefix"] in selected_prefixes for pattern in patterns)


def _element_matches_source_pattern(element: DetectedStructureElement, source_prefixes: set[str]) -> bool:
    if not source_prefixes:
        return False
    return any(pattern["prefix"] in source_prefixes for pattern in _group_pattern(element.raw_value))


def _create_group_pattern_template(value: str) -> dict[str, str] | None:
    text = (value or "").strip()
    match = re.search(r"^(?P<prefix>.*?)(?P<token>\d+|[A-Za-z])\s*$", text)
    if not match:
        return None
    token = match.group("token")
    normalized_token = str(int(token)) if token.isdigit() else normalize_text(token)
    return {"prefix": match.group("prefix"), "token": normalized_token}


def _format_created_group_name(template: dict[str, str], raw_value: str) -> str:
    match = re.search(r"(?P<token>\d+|[A-Za-z])\s*$", (raw_value or "").strip())
    token = match.group("token") if match else raw_value
    return f"{template['prefix']}{token}".strip()


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
        resolution.create_code = ""
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
    batch.status = (
        ImportBatch.Status.AWAITING_RESOLUTION
        if has_historical_finalization_blockers(batch)
        else ImportBatch.Status.READY
    )
    batch.save(update_fields=["status"])


def _propagate_resolved_parent_context(batch: ImportBatch) -> int:
    updated = 0
    project_id = _resolved_project_id(batch)
    for element in batch.detected_elements.filter(
        inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        resolution__action=ImportResolution.Action.UNRESOLVED,
    ):
        context = dict(element.structural_context or {})
        changed = False
        if project_id and not context.get("project_id"):
            context["project_id"] = project_id
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
        resolution.create_code = ""
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
