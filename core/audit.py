from __future__ import annotations

from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.contenttypes.models import ContentType

from core.models import AuditEvent


ACTION_FLAGS = {
    "creado": ADDITION,
    "created": ADDITION,
    "modificado": CHANGE,
    "updated": CHANGE,
    "eliminado": DELETION,
    "deleted": DELETION,
    "importado": ADDITION,
    "imported": ADDITION,
    "deshecho": CHANGE,
    "revertido": CHANGE,
    "reverted": CHANGE,
    "cargado": ADDITION,
    "uploaded": ADDITION,
    "descargado": CHANGE,
    "downloaded": CHANGE,
    "exportado": CHANGE,
    "exported": CHANGE,
    "restaurado": CHANGE,
    "restored": CHANGE,
}


def record_audit(
    *,
    user,
    action: str,
    entity: str,
    entity_id=None,
    entity_repr: str = "",
    obj=None,
    description: str = "",
    context=None,
    before=None,
    after=None,
    reason: str = "",
    summary=None,
    mirror_log_entry: bool = True,
):
    user_id = getattr(user, "pk", None)
    if not user_id:
        return None
    target = obj if obj is not None else None
    event = AuditEvent.objects.create(
        user=user,
        action=str(action or "").strip(),
        entity=str(entity or "").strip(),
        entity_id=str(entity_id if entity_id is not None else getattr(target, "pk", "") or ""),
        entity_repr=str(entity_repr or target or "")[:200],
        description=str(description or "").strip(),
        reason=str(reason or "").strip(),
        context=_clean_payload(context),
        before=_clean_payload(before),
        after=_clean_payload(after),
        summary=_clean_payload(summary),
    )
    if mirror_log_entry:
        _mirror_to_log_entry(event, target)
    return event


def audit_event(*, user, action: str, entity: str, obj=None, description: str = "", context=None, before=None, after=None, reason: str = "", summary=None):
    return record_audit(
        user=user,
        action=action,
        entity=entity,
        obj=obj,
        description=description,
        context=context,
        before=_snapshot_to_dict(before),
        after=_snapshot_to_dict(after),
        reason=reason,
        summary=summary,
    )


def audit_snapshot(obj, fields):
    return {field: _stringify(getattr(obj, field, None)) for field in fields}


def _snapshot_to_dict(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return _clean_payload(value)
    result = {}
    for chunk in str(value).replace(";", "|").split("|"):
        if ":" in chunk:
            key, raw = chunk.split(":", 1)
        elif "=" in chunk:
            key, raw = chunk.split("=", 1)
        else:
            continue
        result[key.strip()] = raw.strip().strip("'")
    return result


def _clean_payload(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(key): _stringify(item) for key, item in value.items() if item not in (None, "")}
    return {"Detalle": _stringify(value)}


def _stringify(value):
    if value is None:
        return ""
    return str(value)


def _mirror_to_log_entry(event: AuditEvent, target) -> None:
    content_type = ContentType.objects.get_for_model(target.__class__ if target is not None else AuditEvent)
    LogEntry.objects.create(
        user_id=event.user_id,
        content_type=content_type,
        object_id=str(getattr(target, "pk", "") or ""),
        object_repr=(event.entity_repr or str(target or event))[:200],
        action_flag=ACTION_FLAGS.get(event.action.casefold(), CHANGE),
        change_message=_event_message(event),
    )


def _event_message(event: AuditEvent) -> str:
    parts = [f"Accion: {event.action}", f"Entidad: {event.entity}"]
    if event.description:
        parts.append(f"Descripcion: {event.description}")
    if event.reason:
        parts.append(f"Motivo: {event.reason}")
    for key, value in event.context.items():
        parts.append(f"{key}: {value}")
    if event.before:
        parts.append("Antes: " + ", ".join(f"{key}={value}" for key, value in event.before.items()))
    if event.after:
        parts.append("Despues: " + ", ".join(f"{key}={value}" for key, value in event.after.items()))
    for key, value in event.summary.items():
        parts.append(f"{key}: {value}")
    return " | ".join(parts)
