from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .models import Client, FiduciaryAssignment, FiduciaryAssignmentHolder, FiduciaryNovelty, UnitOwnership


NOVELTY_TYPE_CHOICES = FiduciaryNovelty.NoveltyType.choices


@dataclass(frozen=True)
class OwnershipChangeResult:
    previous_ownership: UnitOwnership
    new_ownership: UnitOwnership


@dataclass(frozen=True)
class AssignmentChangeResult:
    previous_assignment: FiduciaryAssignment
    new_assignment: FiduciaryAssignment


ASSIGNMENT_CHANGE_WITHOUT_NEW_ASSIGNMENT = {"withdrawal", "exclusion"}


def validate_unit_primary_available(*, unit, current_instance=None) -> None:
    current = (
        UnitOwnership.objects.filter(property_unit=unit, is_active=True, is_primary=True)
        .select_related("client")
        .first()
    )
    if current and (not current_instance or current.pk != current_instance.pk):
        raise ValidationError(
            {
                "is_primary": (
                    "La unidad seleccionada ya tiene un titular principal activo: "
                    f"{current.client.full_name}. Para asignar otro titular principal debe registrar "
                    "una cesion o finalizar la titularidad vigente."
                )
            }
        )


def validate_active_assignment_available(*, unit, current_instance=None) -> None:
    current = FiduciaryAssignment.objects.filter(property_unit=unit, is_active=True).first()
    if current and (not current_instance or current.pk != current_instance.pk):
        raise ValidationError(
            {
                "property_unit": (
                    "La unidad seleccionada ya tiene un encargo fiduciario activo: "
                    f"{current.assignment_number}. Para registrar uno nuevo debe realizar un cambio de encargo "
                    "y finalizar la vigencia del anterior."
                )
            }
        )


def finalize_ownership(*, ownership: UnitOwnership, end_date, reason: str, novelty_type: str) -> UnitOwnership:
    if not ownership.is_active:
        raise ValidationError("La titularidad ya se encuentra finalizada.")
    if ownership.is_primary:
        raise ValidationError(
            "No finalice directamente el titular principal. Use la accion Cambiar titular principal o Registrar cesion."
        )
    with transaction.atomic():
        ownership.is_active = False
        ownership.end_date = end_date
        ownership.last_change_reason = _reason(novelty_type, reason)
        ownership.full_clean()
        ownership.save(update_fields=["is_active", "end_date", "last_change_reason", "updated_at"])
    return ownership


def change_primary_ownership(
    *,
    unit,
    new_client: Client,
    effective_date,
    novelty_type: str,
    reason: str,
) -> OwnershipChangeResult:
    with transaction.atomic():
        previous = (
            UnitOwnership.objects.select_for_update()
            .filter(property_unit=unit, is_active=True, is_primary=True)
            .select_related("client")
            .first()
        )
        if not previous:
            raise ValidationError("La unidad no tiene titular principal vigente para reemplazar.")
        if previous.client_id == new_client.pk:
            raise ValidationError("El nuevo titular principal debe ser diferente al titular vigente.")
        previous.is_active = False
        previous.end_date = effective_date
        previous.last_change_reason = _reason(novelty_type, reason)
        previous.full_clean()
        previous.save(update_fields=["is_active", "end_date", "last_change_reason", "updated_at"])
        new_ownership = UnitOwnership(
            client=new_client,
            property_unit=unit,
            is_primary=True,
            start_date=effective_date,
            last_change_reason=_reason(novelty_type, reason),
        )
        new_ownership.full_clean()
        new_ownership.save()
        sync_active_assignment_primary_holder(
            unit=unit,
            client=new_client,
            effective_date=effective_date,
            reason=_reason(novelty_type, reason),
        )
    return OwnershipChangeResult(previous_ownership=previous, new_ownership=new_ownership)


def sync_active_assignment_primary_holder(*, unit, client: Client, effective_date, reason: str) -> None:
    assignment = FiduciaryAssignment.objects.filter(property_unit=unit, is_active=True).first()
    if not assignment:
        return
    active_primary = assignment.holders.filter(is_active=True, is_primary=True).select_related("client").first()
    if active_primary and active_primary.client_id == client.pk:
        return
    if active_primary:
        active_primary.is_active = False
        active_primary.end_date = effective_date
        active_primary.last_change_reason = reason
        active_primary.full_clean()
        active_primary.save(update_fields=["is_active", "end_date", "last_change_reason", "updated_at"])
    existing = assignment.holders.filter(client=client, is_active=True).first()
    if existing:
        existing.is_primary = True
        existing.last_change_reason = reason
        existing.full_clean()
        existing.save(update_fields=["is_primary", "last_change_reason", "updated_at"])
        return
    holder = FiduciaryAssignmentHolder(
        assignment=assignment,
        client=client,
        is_primary=True,
        start_date=effective_date,
        last_change_reason=reason,
    )
    holder.full_clean()
    holder.save()


def change_assignment(
    *,
    current_assignment: FiduciaryAssignment,
    new_assignment_number: str,
    effective_date,
    novelty_type: str,
    reason: str,
    primary_client: Client | None,
    secondary_clients,
    other_description: str = "",
) -> AssignmentChangeResult:
    secondary_clients = list(secondary_clients)
    new_assignment_number = (new_assignment_number or "").strip()
    description = (other_description or "").strip()
    if novelty_type == "other" and not description:
        raise ValidationError({"other_description": "Describa la novedad."})
    if novelty_type not in ASSIGNMENT_CHANGE_WITHOUT_NEW_ASSIGNMENT and not primary_client:
        raise ValidationError({"primary_client": "Seleccione el titular principal del encargo."})
    if new_assignment_number and FiduciaryAssignment.objects.filter(assignment_number=new_assignment_number).exclude(pk=current_assignment.pk).exists():
        raise ValidationError({"new_assignment_number": "Ya existe un encargo fiduciario con ese numero."})
    with transaction.atomic():
        current = FiduciaryAssignment.objects.select_for_update().get(pk=current_assignment.pk)
        if not current.is_active:
            raise ValidationError("El encargo actual ya se encuentra finalizado.")
        operation_reason = _reason(novelty_type, description or reason)
        active_ownership_client_ids = set(
            UnitOwnership.objects.filter(property_unit=current.property_unit, is_active=True).values_list("client_id", flat=True)
        )
        requested_clients = ([primary_client] if primary_client else []) + secondary_clients
        missing = [client.full_name for client in requested_clients if client.pk not in active_ownership_client_ids]
        if missing:
            raise ValidationError(
                "Todos los titulares del nuevo encargo deben tener titularidad vigente sobre la unidad. "
                f"Sin titularidad vigente: {', '.join(missing)}."
            )

        if novelty_type in ASSIGNMENT_CHANGE_WITHOUT_NEW_ASSIGNMENT:
            current.holders.filter(is_active=True).update(
                is_active=False,
                end_date=effective_date,
                last_change_reason=operation_reason,
            )
            UnitOwnership.objects.filter(property_unit=current.property_unit, is_active=True).update(
                is_active=False,
                end_date=effective_date,
                last_change_reason=operation_reason,
            )
            current.last_change_reason = operation_reason
            current.full_clean()
            current.save(update_fields=["last_change_reason", "updated_at"])
            return AssignmentChangeResult(previous_assignment=current, new_assignment=current)

        if not new_assignment_number or new_assignment_number == current.assignment_number:
            current.holders.filter(is_active=True).update(
                is_active=False,
                end_date=effective_date,
                last_change_reason=operation_reason,
            )
            target_assignment = current
            target_assignment.last_change_reason = operation_reason
            target_assignment.save(update_fields=["last_change_reason", "updated_at"])
        else:
            current.is_active = False
            current.end_date = effective_date
            current.last_change_reason = operation_reason
            current.full_clean()
            current.save(update_fields=["is_active", "end_date", "last_change_reason", "updated_at"])
            current.holders.filter(is_active=True).update(
                is_active=False,
                end_date=effective_date,
                last_change_reason=operation_reason,
            )
            target_assignment = FiduciaryAssignment(
                assignment_number=new_assignment_number,
                property_unit=current.property_unit,
                start_date=effective_date,
                observations=f"Cambio de encargo desde {current.assignment_number}.",
                last_change_reason=operation_reason,
            )
            target_assignment.full_clean()
            target_assignment.save()

        for index, client in enumerate([primary_client] + secondary_clients):
            holder = FiduciaryAssignmentHolder(
                assignment=target_assignment,
                client=client,
                is_primary=index == 0,
                start_date=effective_date,
                last_change_reason=operation_reason,
            )
            holder.full_clean()
            holder.save()
    return AssignmentChangeResult(previous_assignment=current, new_assignment=target_assignment)


def register_fiduciary_novelty(
    *,
    project,
    property_unit,
    novelty_type: str,
    effective_date,
    reason: str,
    created_by,
    ip_address: str | None = None,
    new_primary_client: Client | None = None,
    secondary_clients=None,
    new_assignment_number: str = "",
    other_description: str = "",
) -> FiduciaryNovelty:
    secondary_clients = list(secondary_clients or [])
    with transaction.atomic():
        unit = property_unit.__class__.objects.select_for_update().get(pk=property_unit.pk)
        previous_primary = (
            UnitOwnership.objects.select_for_update()
            .filter(property_unit=unit, is_active=True, is_primary=True)
            .select_related("client")
            .first()
        )
        previous_assignment = (
            FiduciaryAssignment.objects.select_for_update()
            .filter(property_unit=unit, is_active=True)
            .prefetch_related("holders", "payments")
            .first()
        )
        before = _unit_state(unit)
        affected_payments = previous_assignment.payments.count() if previous_assignment else 0

        if novelty_type in ASSIGNMENT_CHANGE_WITHOUT_NEW_ASSIGNMENT:
            if previous_assignment:
                result = change_assignment(
                    current_assignment=previous_assignment,
                    new_assignment_number="",
                    effective_date=effective_date,
                    novelty_type=novelty_type,
                    reason=reason,
                    primary_client=None,
                    secondary_clients=[],
                    other_description=other_description,
                )
                new_assignment = result.new_assignment
            else:
                UnitOwnership.objects.filter(property_unit=unit, is_active=True).update(
                    is_active=False,
                    end_date=effective_date,
                    last_change_reason=_reason(novelty_type, other_description or reason),
                )
                new_assignment = None
        elif previous_assignment:
            result = change_assignment(
                current_assignment=previous_assignment,
                new_assignment_number=new_assignment_number,
                effective_date=effective_date,
                novelty_type=novelty_type,
                reason=reason,
                primary_client=new_primary_client,
                secondary_clients=secondary_clients,
                other_description=other_description,
            )
            new_assignment = result.new_assignment
        else:
            if not new_primary_client:
                raise ValidationError({"new_primary_client": "Seleccione el titular principal."})
            change_primary_ownership(
                unit=unit,
                new_client=new_primary_client,
                effective_date=effective_date,
                novelty_type=novelty_type,
                reason=other_description or reason,
            )
            new_assignment = None

        after = _unit_state(unit)
        novelty = FiduciaryNovelty(
            project=project,
            property_unit=unit,
            novelty_type=novelty_type,
            effective_date=effective_date,
            reason=reason,
            other_description=other_description,
            previous_primary_client=previous_primary.client if previous_primary else None,
            new_primary_client=new_primary_client,
            previous_assignment=previous_assignment,
            new_assignment=new_assignment,
            created_by=created_by,
            ip_address=ip_address or None,
            affected_payments_count=affected_payments,
            before_data=before,
            after_data=after,
            result_message="Novedad aplicada correctamente.",
        )
        novelty.full_clean()
        novelty.save()
    return novelty


def _unit_state(unit) -> dict:
    active_ownerships = [
        {
            "client_id": ownership.client_id,
            "client": ownership.client.full_name,
            "is_primary": ownership.is_primary,
        }
        for ownership in UnitOwnership.objects.filter(property_unit=unit, is_active=True)
        .select_related("client")
        .order_by("-is_primary", "pk")
    ]
    assignment = (
        FiduciaryAssignment.objects.filter(property_unit=unit, is_active=True)
        .prefetch_related("holders", "payments")
        .first()
    )
    return {
        "unit_id": unit.pk,
        "unit": str(unit),
        "ownerships": active_ownerships,
        "assignment_id": assignment.pk if assignment else None,
        "assignment_number": assignment.assignment_number if assignment else "",
        "holders": [
            {"client_id": holder.client_id, "client": holder.client.full_name, "is_primary": holder.is_primary}
            for holder in assignment.holders.select_related("client").filter(is_active=True).order_by("-is_primary", "pk")
        ]
        if assignment
        else [],
        "payments_count": assignment.payments.count() if assignment else 0,
    }


def save_form_object_safely(form):
    try:
        with transaction.atomic():
            return form.save()
    except ValidationError:
        raise
    except IntegrityError as exc:
        raise ValidationError("La operacion no pudo completarse porque viola una regla de negocio vigente.") from exc


def _reason(novelty_type: str, reason: str) -> str:
    label = dict(NOVELTY_TYPE_CHOICES).get(novelty_type, novelty_type)
    return f"{label}: {reason.strip()}"
