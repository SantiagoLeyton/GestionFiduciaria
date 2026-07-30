from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Client, FiduciaryAssignment, FiduciaryAssignmentHolder, OperationalNovelty, UnitOwnership


NOVELTY_TYPE_CHOICES = [
    ("cession", "Cesion"),
    ("exclusion", "Exclusion"),
    ("substitution", "Sustitucion"),
    ("administrative_correction", "Correccion administrativa"),
    ("withdrawal", "Retiro"),
    ("other", "Otro"),
]


@dataclass(frozen=True)
class OwnershipChangeResult:
    previous_ownership: UnitOwnership
    new_ownership: UnitOwnership


@dataclass(frozen=True)
class AssignmentChangeResult:
    previous_assignment: FiduciaryAssignment
    new_assignment: FiduciaryAssignment


ASSIGNMENT_CHANGE_WITHOUT_NEW_ASSIGNMENT = {"withdrawal", "exclusion"}


@dataclass(frozen=True)
class OperationalNoveltyResult:
    novelty: OperationalNovelty
    ownership: UnitOwnership | None = None
    assignment: FiduciaryAssignment | None = None


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


@dataclass(frozen=True)
class OwnershipAssignmentCreationResult:
    ownership: UnitOwnership
    assignment: FiduciaryAssignment
    novelty: OperationalNovelty


def create_primary_ownership_with_assignment(
    *,
    unit,
    primary_client: Client,
    assignment_number: str,
    effective_date,
    reason: str = "",
    secondary_clients=None,
    novelty_type: str = OperationalNovelty.NoveltyType.CESSION,
    other_type: str = "",
    created_by=None,
) -> OwnershipAssignmentCreationResult:
    secondary_clients = list(secondary_clients or [])
    assignment_number = (assignment_number or "").strip()
    reason = (reason or "Registro de nueva titularidad y encargo fiduciario.").strip()
    other_type = (other_type or "").strip()
    if novelty_type == OperationalNovelty.NoveltyType.OTHER and not other_type:
        raise ValidationError({"other_type": "Indique cual es la novedad."})
    if not assignment_number:
        raise ValidationError({"assignment_number": "Registre el numero del nuevo encargo fiduciario."})
    if primary_client in secondary_clients:
        raise ValidationError({"secondary_clients": "El titular principal no debe repetirse como secundario."})
    if FiduciaryAssignment.objects.filter(assignment_number=assignment_number).exists():
        raise ValidationError({"assignment_number": "El numero de encargo ya existe y no puede reutilizarse."})

    with transaction.atomic():
        locked_unit = unit.__class__.objects.select_for_update().get(pk=unit.pk)
        current_primary = (
            UnitOwnership.objects.select_for_update()
            .filter(property_unit=locked_unit, is_active=True, is_primary=True)
            .select_related("client")
            .first()
        )
        current_assignment = (
            FiduciaryAssignment.objects.select_for_update()
            .filter(property_unit=locked_unit, is_active=True)
            .prefetch_related("holders")
            .first()
        )
        if current_primary and current_primary.client_id == primary_client.pk:
            raise ValidationError({"client": "El cliente ya es titular principal vigente de la unidad."})
        previous_client = current_primary.client if current_primary else None
        previous_assignment = current_assignment

        if current_primary:
            current_primary.is_active = False
            current_primary.end_date = effective_date
            current_primary.last_change_reason = reason
            current_primary.full_clean()
            current_primary.save(update_fields=["is_active", "end_date", "last_change_reason", "updated_at"])

        if current_assignment:
            current_assignment.is_active = False
            current_assignment.end_date = effective_date
            current_assignment.last_change_reason = reason
            current_assignment.full_clean()
            current_assignment.save(update_fields=["is_active", "end_date", "last_change_reason", "updated_at"])
            current_assignment.holders.filter(is_active=True).update(
                is_active=False,
                end_date=effective_date,
                last_change_reason=reason,
                updated_at=timezone.now(),
            )

        ownership = UnitOwnership(
            client=primary_client,
            property_unit=locked_unit,
            is_primary=True,
            start_date=effective_date,
            last_change_reason=reason,
        )
        ownership.full_clean()
        ownership.save()

        assignment = FiduciaryAssignment(
            assignment_number=assignment_number,
            property_unit=locked_unit,
            start_date=effective_date,
            observations=reason,
            last_change_reason=reason,
        )
        assignment.full_clean()
        assignment.save()
        FiduciaryAssignmentHolder.objects.create(
            assignment=assignment,
            client=primary_client,
            is_primary=True,
            start_date=effective_date,
            last_change_reason=reason,
        )

        seen_secondary_ids = set()
        for client in secondary_clients:
            if client.pk in seen_secondary_ids:
                raise ValidationError({"secondary_clients": "No puede seleccionar el mismo cliente secundario mas de una vez."})
            seen_secondary_ids.add(client.pk)
            secondary_ownership, _ = UnitOwnership.objects.get_or_create(
                client=client,
                property_unit=locked_unit,
                is_active=True,
                defaults={
                    "is_primary": False,
                    "start_date": effective_date,
                    "last_change_reason": reason,
                },
            )
            if secondary_ownership.is_primary:
                raise ValidationError({"secondary_clients": "Un titular principal vigente no puede agregarse como secundario."})
            FiduciaryAssignmentHolder.objects.create(
                assignment=assignment,
                client=client,
                is_primary=False,
                start_date=effective_date,
                last_change_reason=reason,
            )
        novelty = OperationalNovelty(
            project=locked_unit.project,
            property_unit=locked_unit,
            novelty_type=novelty_type,
            other_type=other_type,
            origin=OperationalNovelty.Origin.AUTOMATIC,
            status=OperationalNovelty.Status.APPLIED,
            effective_date=effective_date,
            previous_client=previous_client,
            new_client=primary_client,
            previous_assignment=previous_assignment,
            new_assignment=assignment,
            summary=reason,
            detail=reason,
            created_by=created_by,
        )
        novelty.full_clean()
        novelty.save()
    return OwnershipAssignmentCreationResult(ownership=ownership, assignment=assignment, novelty=novelty)


def apply_operational_novelty(
    *,
    unit,
    novelty_type: str,
    effective_date,
    summary: str,
    detail: str,
    user,
    new_client: Client | None = None,
    new_assignment_number: str = "",
    secondary_clients=None,
    other_type: str = "",
) -> OperationalNoveltyResult:
    summary = (summary or "").strip()
    detail = (detail or "").strip()
    other_type = (other_type or "").strip()
    if novelty_type == OperationalNovelty.NoveltyType.OTHER and not other_type:
        raise ValidationError({"other_type": "Indique cual es la novedad."})
    if novelty_type in {OperationalNovelty.NoveltyType.CESSION, OperationalNovelty.NoveltyType.SUBSTITUTION}:
        result = create_primary_ownership_with_assignment(
            unit=unit,
            primary_client=new_client,
            assignment_number=new_assignment_number,
            effective_date=effective_date,
            reason=summary or detail or "Novedad operativa.",
            secondary_clients=secondary_clients,
            novelty_type=novelty_type,
            other_type=other_type,
            created_by=user,
        )
        if detail:
            result.novelty.detail = detail
            result.novelty.full_clean()
            result.novelty.save(update_fields=["detail", "updated_at"])
        return OperationalNoveltyResult(novelty=result.novelty, ownership=result.ownership, assignment=result.assignment)

    with transaction.atomic():
        locked_unit = unit.__class__.objects.select_for_update().get(pk=unit.pk)
        current_primary = (
            UnitOwnership.objects.select_for_update()
            .filter(property_unit=locked_unit, is_active=True, is_primary=True)
            .select_related("client")
            .first()
        )
        current_assignment = (
            FiduciaryAssignment.objects.select_for_update()
            .filter(property_unit=locked_unit, is_active=True)
            .first()
        )
        operation_reason = summary or detail or dict(NOVELTY_TYPE_CHOICES).get(novelty_type, novelty_type)
        if novelty_type in {OperationalNovelty.NoveltyType.WITHDRAWAL, OperationalNovelty.NoveltyType.EXCLUSION}:
            if current_primary:
                current_primary.is_active = False
                current_primary.end_date = effective_date
                current_primary.last_change_reason = operation_reason
                current_primary.full_clean()
                current_primary.save(update_fields=["is_active", "end_date", "last_change_reason", "updated_at"])
            if current_assignment:
                current_assignment.holders.filter(is_active=True, is_primary=True).update(
                    is_active=False,
                    end_date=effective_date,
                    last_change_reason=operation_reason,
                    updated_at=timezone.now(),
                )
                current_assignment.last_change_reason = operation_reason
                current_assignment.full_clean()
                current_assignment.save(update_fields=["last_change_reason", "updated_at"])
        novelty = OperationalNovelty(
            project=locked_unit.project,
            property_unit=locked_unit,
            novelty_type=novelty_type,
            other_type=other_type,
            origin=OperationalNovelty.Origin.MANUAL,
            status=OperationalNovelty.Status.APPLIED if novelty_type != OperationalNovelty.NoveltyType.OTHER else OperationalNovelty.Status.DESCRIPTIVE,
            effective_date=effective_date,
            previous_client=current_primary.client if current_primary else None,
            new_client=new_client if novelty_type not in {OperationalNovelty.NoveltyType.WITHDRAWAL, OperationalNovelty.NoveltyType.EXCLUSION} else None,
            previous_assignment=current_assignment,
            summary=summary,
            detail=detail,
            created_by=user,
        )
        novelty.full_clean()
        novelty.save()
    return OperationalNoveltyResult(novelty=novelty)


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
