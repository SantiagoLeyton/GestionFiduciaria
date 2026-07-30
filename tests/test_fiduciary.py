from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from fiduciary.models import Client as FiduciaryClient
from fiduciary.models import (
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportedHistoricalObservation,
    OperationalNovelty,
    Payment,
    UnitOwnership,
)
from fiduciary.forms import DIRECT_UNITS_VALUE, UnitOwnershipForm
from fiduciary.services import split_imported_full_name
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup


@pytest.fixture
def project(db):
    return Project.objects.create(code="P3-001", name="Proyecto Fase 3")


@pytest.fixture
def second_project(db):
    return Project.objects.create(code="P3-002", name="Proyecto Externo")


@pytest.fixture
def grouping_type(db):
    return GroupingType.objects.create(code="T-F3", name="Torre")


@pytest.fixture
def unit(project):
    return PropertyUnit.objects.create(project=project, code="U-101", name="Unidad 101")


@pytest.fixture
def second_unit(project):
    return PropertyUnit.objects.create(project=project, code="U-102", name="Unidad 102")


@pytest.fixture
def external_unit(second_project):
    return PropertyUnit.objects.create(project=second_project, code="U-201", name="Unidad externa")


@pytest.fixture
def active_client(db):
    return FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="123",
        first_names="Ana",
        last_names_or_company="Silva",
        phone="3001234567",
    )


@pytest.fixture
def secondary_client(db):
    return FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="456",
        first_names="Carlos",
        last_names_or_company="Ruiz",
        email="carlos@example.com",
    )


@pytest.fixture
def accounting_client(accounting_admin_user):
    test_client = Client()
    test_client.force_login(accounting_admin_user)
    return test_client


@pytest.fixture
def commercial_client(commercial_user):
    test_client = Client()
    test_client.force_login(commercial_user)
    return test_client


def create_ownership(client, unit, is_primary=True, start_date="2026-01-01"):
    return UnitOwnership.objects.create(
        client=client,
        property_unit=unit,
        is_primary=is_primary,
        start_date=start_date,
        last_change_reason="Registro manual",
    )


def create_assignment(unit, client, number="EF-001", start_date="2026-01-01"):
    assignment = FiduciaryAssignment.objects.create(
        assignment_number=number,
        property_unit=unit,
        start_date=start_date,
        last_change_reason="Registro manual",
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=client,
        is_primary=True,
        start_date=start_date,
        last_change_reason="Registro manual",
    )
    return assignment


def create_imported_file(user, file_type=ImportedFile.FileType.HISTORICAL):
    batch = ImportBatch.objects.create(
        initiated_by=user,
        imported_by=user,
        import_type=ImportBatch.ImportType.HISTORICAL if file_type == ImportedFile.FileType.HISTORICAL else ImportBatch.ImportType.REPORTS,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.COMPLETED,
        total_files=1,
        processed_files=1,
        summary="Lote de prueba",
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="archivo-prueba.xlsx",
        extension=".xlsx",
        size_bytes=128,
        sha256=("a" if file_type == ImportedFile.FileType.HISTORICAL else "b") * 64,
        file_type=file_type,
        status=ImportedFile.Status.COMPLETED,
        order=1,
    )
    return batch, imported_file


def create_payment_for_assignment(assignment, user, amount="1000.00", period_year=2026, period_month=7):
    _, imported_file = create_imported_file(user)
    return Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.MONTH,
        period_year=period_year,
        period_month=period_month,
        amount=Decimal(amount),
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T2",
        source_row=5,
        source_column="AA",
        source_header="RECIBO FIDUCIA JUL/2026",
    )


def assignment_post_data(unit, primary_client, number="EF-FORM", secondary_clients=None, deleted_clients=None, blank_rows=0):
    secondary_clients = secondary_clients or []
    deleted_clients = deleted_clients or []
    rows = list(secondary_clients) + list(deleted_clients) + [None] * blank_rows
    data = {
        "project": unit.project_id,
        "grouping_type": unit.structural_group.grouping_type_id if unit.structural_group_id else "",
        "structural_group": unit.structural_group_id or DIRECT_UNITS_VALUE,
        "assignment_number": number,
        "property_unit": unit.pk,
        "start_date": "2026-01-01",
        "observations": "",
        "primary_client": primary_client.pk if primary_client else "",
        "change_reason": "Registro temporal",
        "holders-TOTAL_FORMS": str(max(1, len(rows))),
        "holders-INITIAL_FORMS": "0",
        "holders-MIN_NUM_FORMS": "0",
        "holders-MAX_NUM_FORMS": "1000",
    }
    if not rows:
        rows = [None]
    for index, client in enumerate(rows):
        data[f"holders-{index}-client"] = client.pk if client else ""
        data[f"holders-{index}-DELETE"] = "on" if client in deleted_clients else ""
    return data


@pytest.mark.django_db
def test_create_valid_natural_person():
    client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="  900  ",
        first_names="  Laura ",
        last_names_or_company=" Torres ",
        email="LAURA@EXAMPLE.COM",
    )

    assert client.document_number == "900"
    assert client.email == "laura@example.com"
    assert client.full_name == "Laura Torres"
    assert client.information_status == FiduciaryClient.InformationStatus.COMPLETE


@pytest.mark.django_db
def test_create_company_with_tax_id_and_empty_first_names():
    client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.TAX_ID,
        document_number="NIT-1",
        first_names="",
        last_names_or_company="Constructora Beta S.A.",
        phone="6010000000",
    )

    assert client.full_name == "Constructora Beta S.A."


@pytest.mark.django_db
def test_client_rejects_missing_identification():
    client = FiduciaryClient(
        document_type="",
        document_number="",
        last_names_or_company="Sin documento",
        phone="300",
    )

    with pytest.raises(ValidationError):
        client.full_clean()


@pytest.mark.django_db
def test_client_duplicate_document_type_and_number_is_rejected(active_client):
    duplicate = FiduciaryClient(
        document_type=active_client.document_type,
        document_number=active_client.document_number,
        last_names_or_company="Duplicado",
        phone="300",
    )

    with pytest.raises(ValidationError):
        duplicate.full_clean()


@pytest.mark.django_db
def test_same_document_number_with_different_type_is_allowed(active_client):
    client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.PASSPORT,
        document_number=active_client.document_number,
        last_names_or_company="Pasaporte",
        phone="300",
    )

    assert client.pk


@pytest.mark.django_db
def test_manual_complete_client_requires_contact():
    client = FiduciaryClient(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="999",
        first_names="Sin",
        last_names_or_company="Contacto",
    )

    with pytest.raises(ValidationError):
        client.full_clean()


@pytest.mark.django_db
def test_client_can_have_single_contact_method():
    client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="777",
        last_names_or_company="Solo Telefono",
        phone="300",
    )

    assert client.phone == "300"


@pytest.mark.django_db
def test_client_can_have_only_email():
    client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="778",
        last_names_or_company="Solo Correo",
        email="solo@example.com",
    )

    assert client.email == "solo@example.com"


@pytest.mark.django_db
def test_client_can_have_phone_email_and_contact():
    client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="779",
        last_names_or_company="Contacto Completo",
        phone="300",
        email="completo@example.com",
        address="Contacto administrativo",
    )

    assert client.address == "Contacto administrativo"


@pytest.mark.django_db
def test_client_rejects_contact_without_phone_or_email():
    client = FiduciaryClient(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="780",
        last_names_or_company="Solo Contacto",
        address="Contacto administrativo",
    )

    with pytest.raises(ValidationError, match="telefono o un correo"):
        client.full_clean()


@pytest.mark.django_db
def test_client_rejects_space_only_phone_and_email():
    client = FiduciaryClient(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="781",
        last_names_or_company="Espacios",
        phone="   ",
        email="   ",
        address="Contacto administrativo",
    )

    with pytest.raises(ValidationError, match="telefono o un correo"):
        client.full_clean()


@pytest.mark.django_db
def test_client_form_rejects_contact_only():
    from fiduciary.forms import ClientForm

    form = ClientForm(
        data={
            "document_type": FiduciaryClient.DocumentType.CITIZENSHIP_ID,
            "document_number": "782",
            "first_names": "",
            "last_names_or_company": "Solo Contacto",
            "phone": "",
            "email": "",
            "address": "Contacto administrativo",
            "is_active": "on",
        }
    )

    assert not form.is_valid()
    assert "Debe registrar al menos un telefono o un correo electronico." in str(form.errors)


@pytest.mark.django_db
def test_client_activation_requires_reason(accounting_client, active_client):
    response = accounting_client.post(reverse("fiduciary:client_status", args=[active_client.pk, "deactivate"]))

    active_client.refresh_from_db()
    assert response.status_code == 302
    assert active_client.is_active is True


@pytest.mark.django_db
def test_client_inactivation_and_reactivation_do_not_delete(accounting_client, active_client):
    accounting_client.post(
        reverse("fiduciary:client_status", args=[active_client.pk, "deactivate"]),
        {"change_reason": "Inactivacion administrativa"},
    )
    active_client.refresh_from_db()
    assert active_client.is_active is False

    accounting_client.post(
        reverse("fiduciary:client_status", args=[active_client.pk, "activate"]),
        {"change_reason": "Reactivacion administrativa"},
    )
    active_client.refresh_from_db()
    assert active_client.is_active is True


@pytest.mark.django_db
def test_client_edit_requires_reason(accounting_client, active_client):
    response = accounting_client.post(
        reverse("fiduciary:client_update", args=[active_client.pk]),
        {
            "document_type": active_client.document_type,
            "document_number": active_client.document_number,
            "first_names": active_client.first_names,
            "last_names_or_company": "Silva Editada",
            "phone": active_client.phone,
            "email": active_client.email,
            "address": active_client.address,
            "is_active": "on",
        },
    )

    active_client.refresh_from_db()
    assert response.status_code == 200
    assert active_client.last_names_or_company == "Silva"


@pytest.mark.django_db
def test_client_search_blank_and_combined_filters(accounting_client, active_client, unit):
    create_ownership(active_client, unit)

    blank = accounting_client.get(reverse("fiduciary:client_list"), {"q": "     "})
    filtered = accounting_client.get(
        reverse("fiduciary:client_list"),
        {
            "q": "Ana",
            "document_type": active_client.document_type,
            "information_status": FiduciaryClient.InformationStatus.COMPLETE,
            "status": "active",
            "project": unit.project_id,
            "property_unit": unit.pk,
        },
    )

    assert active_client.full_name in blank.content.decode()
    assert active_client.full_name in filtered.content.decode()


@pytest.mark.django_db
def test_client_document_filter_normalizes_separators_and_combines_filters(accounting_client, active_client, unit):
    active_client.document_number = "1.234-567"
    active_client.save(update_fields=["document_number"])
    create_ownership(active_client, unit)

    response = accounting_client.get(
        reverse("fiduciary:client_list"),
        {
            "document": "234 5",
            "document_type": active_client.document_type,
            "project": unit.project_id,
        },
    )

    content = response.content.decode()
    assert active_client.full_name in content


@pytest.mark.django_db
def test_client_document_filter_returns_empty_for_unknown_document(accounting_client, active_client):
    response = accounting_client.get(reverse("fiduciary:client_list"), {"document": "999999"})

    assert active_client.full_name not in response.content.decode()


@pytest.mark.django_db
def test_client_pagination_preserves_filters(accounting_client, project):
    for index in range(18):
        FiduciaryClient.objects.create(
            document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
            document_number=f"CC-{index}",
            last_names_or_company=f"Cliente {index:02}",
            phone="300",
        )

    response = accounting_client.get(reverse("fiduciary:client_list"), {"q": "Cliente", "page": 2})
    content = response.content.decode()

    assert "Pagina 2 de 2" in content
    assert "q=Cliente" in content


@pytest.mark.django_db
def test_client_can_own_multiple_units(active_client, unit, second_unit):
    first = create_ownership(active_client, unit)
    second = create_ownership(active_client, second_unit)

    assert first.client == second.client


@pytest.mark.django_db
def test_unit_can_have_primary_and_secondary_owners(active_client, secondary_client, unit):
    primary = create_ownership(active_client, unit, True)
    secondary = create_ownership(secondary_client, unit, False)

    assert primary.is_primary
    assert not secondary.is_primary


@pytest.mark.django_db
def test_unit_rejects_two_active_primary_owners(active_client, secondary_client, unit):
    create_ownership(active_client, unit, True)
    ownership = UnitOwnership(client=secondary_client, property_unit=unit, is_primary=True, start_date="2026-01-02")

    with pytest.raises(ValidationError):
        ownership.full_clean()


@pytest.mark.django_db
def test_unit_rejects_duplicate_active_client_unit(active_client, unit):
    create_ownership(active_client, unit)
    duplicate = UnitOwnership(client=active_client, property_unit=unit, is_primary=False, start_date="2026-01-02")

    with pytest.raises(ValidationError):
        duplicate.full_clean()


@pytest.mark.django_db
def test_unit_allows_historical_ownership_after_previous_closed(active_client, unit):
    ownership = create_ownership(active_client, unit)
    ownership.is_active = False
    ownership.end_date = "2026-02-01"
    ownership.save()

    new_ownership = create_ownership(active_client, unit, start_date="2026-03-01")

    assert new_ownership.is_active


@pytest.mark.django_db
def test_ownership_rejects_invalid_dates(active_client, unit):
    ownership = UnitOwnership(
        client=active_client,
        property_unit=unit,
        is_primary=True,
        start_date="2026-02-01",
        end_date="2026-01-01",
        is_active=False,
    )

    with pytest.raises(ValidationError):
        ownership.full_clean()


@pytest.mark.django_db
def test_finalize_ownership_preserves_record(accounting_client, active_client, secondary_client, unit):
    create_ownership(active_client, unit, True)
    ownership = create_ownership(secondary_client, unit, False)

    response = accounting_client.post(
        reverse("fiduciary:ownership_finalize", args=[ownership.pk]),
        {
            "novelty_type": "exclusion",
            "reason": "Finalizacion",
            "end_date": "2026-02-01",
        },
    )

    ownership.refresh_from_db()
    assert response.status_code == 302
    assert ownership.is_active is False
    assert UnitOwnership.objects.filter(pk=ownership.pk).exists()


@pytest.mark.django_db
def test_primary_ownership_change_preserves_history(accounting_client, active_client, secondary_client, unit):
    current = create_ownership(active_client, unit, True)

    response = accounting_client.post(
        reverse("fiduciary:ownership_change_primary", args=[current.pk]),
        {
            "new_client": secondary_client.pk,
            "effective_date": "2026-03-01",
            "novelty_type": "cession",
            "reason": "Cesion validada",
        },
    )

    current.refresh_from_db()
    assert response.status_code == 302
    assert current.is_active is False
    assert UnitOwnership.objects.filter(property_unit=unit, is_primary=True, is_active=True).get().client == secondary_client
    assert UnitOwnership.objects.filter(property_unit=unit).count() == 2


@pytest.mark.django_db
def test_primary_ownership_creation_syncs_active_assignment(accounting_client, active_client, secondary_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-SYNC")

    response = accounting_client.post(
        reverse("fiduciary:ownership_create"),
        {
            "client": secondary_client.pk,
            "property_unit": unit.pk,
            "start_date": "2026-03-01",
            "assignment_number": "EF-SYNC-NEW",
            "novelty_type": OperationalNovelty.NoveltyType.CESSION,
            "change_reason": "Nuevo titular",
        },
    )

    assert response.status_code == 302
    assignment.refresh_from_db()
    assert assignment.is_active is False
    assert not assignment.holders.filter(client=active_client, is_active=True).exists()
    new_assignment = FiduciaryAssignment.objects.get(assignment_number="EF-SYNC-NEW")
    assert new_assignment.holders.filter(client=secondary_client, is_primary=True, is_active=True).exists()
    assert UnitOwnership.objects.get(client=active_client, property_unit=unit).is_active is False
    assert UnitOwnership.objects.get(client=secondary_client, property_unit=unit).is_primary is True
    novelty = OperationalNovelty.objects.get(new_assignment=new_assignment)
    assert novelty.previous_assignment == assignment
    assert novelty.previous_client == active_client
    assert novelty.new_client == secondary_client


@pytest.mark.django_db
def test_manual_ownership_form_always_creates_primary_and_ignores_manipulated_value(active_client, unit):
    form = UnitOwnershipForm(
        data={
            "client": active_client.pk,
            "property_unit": unit.pk,
            "is_primary": "",
            "start_date": "2026-01-01",
            "assignment_number": "EF-FORM-OWN",
            "novelty_type": OperationalNovelty.NoveltyType.CESSION,
            "change_reason": "Registro manual",
        }
    )

    assert "is_primary" not in form.fields
    assert form.is_valid(), form.errors
    ownership = form.save()
    assert ownership.is_primary is True


@pytest.mark.django_db
def test_manual_ownership_form_accepts_replacement_data_without_is_primary_field(active_client, secondary_client, unit):
    create_ownership(active_client, unit, True)
    form = UnitOwnershipForm(
        data={
            "client": secondary_client.pk,
            "property_unit": unit.pk,
            "start_date": "2026-01-02",
            "assignment_number": "EF-REPLACE",
            "novelty_type": OperationalNovelty.NoveltyType.CESSION,
            "change_reason": "Registro manual",
        }
    )

    assert "is_primary" not in form.fields
    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_inactive_client_or_unit_cannot_receive_new_ownership(active_client, unit):
    active_client.is_active = False
    active_client.save(update_fields=["is_active"])
    with pytest.raises(ValidationError):
        UnitOwnership(client=active_client, property_unit=unit, is_primary=True, start_date="2026-01-01").full_clean()

    active_client.is_active = True
    active_client.save(update_fields=["is_active"])
    unit.is_active = False
    unit.save(update_fields=["is_active"])
    with pytest.raises(ValidationError):
        UnitOwnership(client=active_client, property_unit=unit, is_primary=True, start_date="2026-01-01").full_clean()


@pytest.mark.django_db(transaction=True)
def test_ownership_primary_constraint_hits_postgresql(active_client, secondary_client, unit):
    create_ownership(active_client, unit, True)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            UnitOwnership.objects.bulk_create(
                [UnitOwnership(client=secondary_client, property_unit=unit, is_primary=True, start_date="2026-01-02")]
            )


@pytest.mark.django_db
def test_create_assignment_with_primary_and_secondary(active_client, secondary_client, unit):
    create_ownership(active_client, unit, True)
    create_ownership(secondary_client, unit, False)
    assignment = create_assignment(unit, active_client)
    secondary = FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=secondary_client,
        is_primary=False,
        start_date="2026-01-01",
        last_change_reason="Secundario",
    )

    assert assignment.property_unit == unit
    assert secondary.pk


@pytest.mark.django_db
def test_independent_assignment_creation_screen_is_blocked(accounting_client):
    response = accounting_client.get(reverse("fiduciary:assignment_create"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_assignment_context_types_are_limited_to_selected_project(accounting_client, project, second_project, grouping_type):
    other_type = GroupingType.objects.create(code="B-F3", name="Bloque")
    StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    StructuralGroup.objects.create(project=second_project, grouping_type=other_type, code="B1", name="Bloque 1")

    response = accounting_client.get(reverse("fiduciary:assignment_context_types"), {"project": project.pk})
    payload = response.json()["results"]

    assert response.status_code == 200
    assert {row["id"] for row in payload} == {grouping_type.pk}


@pytest.mark.django_db
def test_assignment_context_groups_include_direct_option_and_filter_by_type(accounting_client, project, grouping_type):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    block_type = GroupingType.objects.create(code="B-F3", name="Bloque")
    StructuralGroup.objects.create(project=project, grouping_type=block_type, code="B1", name="Bloque 1")

    response = accounting_client.get(
        reverse("fiduciary:assignment_context_groups"),
        {"project": project.pk, "grouping_type": grouping_type.pk},
    )
    payload = response.json()["results"]

    assert response.status_code == 200
    assert {"id": DIRECT_UNITS_VALUE, "text": "Unidades directas del proyecto"} in payload
    assert {str(row["id"]) for row in payload if row["id"] != DIRECT_UNITS_VALUE} == {str(tower.pk)}


@pytest.mark.django_db
def test_assignment_context_units_support_direct_and_grouped_units(accounting_client, project, grouping_type):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    direct_unit = PropertyUnit.objects.create(project=project, code="D1", name="Directa")
    grouped_unit = PropertyUnit.objects.create(project=project, structural_group=tower, code="A101", name="Agrupada")

    direct_response = accounting_client.get(
        reverse("fiduciary:assignment_context_units"),
        {"project": project.pk, "structural_group": DIRECT_UNITS_VALUE},
    )
    grouped_response = accounting_client.get(
        reverse("fiduciary:assignment_context_units"),
        {"project": project.pk, "structural_group": tower.pk},
    )

    assert {row["id"] for row in direct_response.json()["results"]} == {direct_unit.pk}
    assert {row["id"] for row in grouped_response.json()["results"]} == {grouped_unit.pk}


@pytest.mark.django_db
def test_assignment_context_holders_loads_only_current_unit_holders(
    accounting_client, active_client, secondary_client, unit, second_unit
):
    create_ownership(active_client, unit, True)
    create_ownership(secondary_client, second_unit, True)

    response = accounting_client.get(reverse("fiduciary:assignment_context_holders"), {"unit": unit.pk})
    payload = response.json()["results"]

    assert response.status_code == 200
    assert {row["id"] for row in payload} == {active_client.pk}


@pytest.mark.django_db
def test_independent_assignment_post_does_not_create_assignment(accounting_client, active_client, unit):
    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        assignment_post_data(unit, active_client, "EF-BLOCKED"),
    )

    assert response.status_code == 403
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-BLOCKED").exists()


@pytest.mark.django_db
def test_ownership_create_creates_assignment_with_primary_and_secondaries(
    accounting_client, active_client, secondary_client, unit
):
    response = accounting_client.post(
        reverse("fiduciary:ownership_create"),
        {
            "client": active_client.pk,
            "property_unit": unit.pk,
            "start_date": "2026-01-01",
            "assignment_number": "EF-JOINT",
            "novelty_type": OperationalNovelty.NoveltyType.CESSION,
            "secondary_clients": [secondary_client.pk],
            "change_reason": "Registro conjunto",
        },
    )

    assert response.status_code == 302
    assignment = FiduciaryAssignment.objects.get(assignment_number="EF-JOINT")
    assert UnitOwnership.objects.get(client=active_client, property_unit=unit).is_primary is True
    assert UnitOwnership.objects.get(client=secondary_client, property_unit=unit).is_primary is False
    assert assignment.holders.get(client=active_client).is_primary is True
    assert assignment.holders.get(client=secondary_client).is_primary is False
    assert OperationalNovelty.objects.filter(property_unit=unit, new_assignment=assignment, new_client=active_client).exists()


@pytest.mark.django_db
def test_ownership_create_rejects_duplicate_assignment_number_without_partial_records(
    accounting_client, active_client, secondary_client, unit, second_unit
):
    create_ownership(active_client, second_unit)
    create_assignment(second_unit, active_client, "EF-DUP-JOINT")

    response = accounting_client.post(
        reverse("fiduciary:ownership_create"),
        {
            "client": secondary_client.pk,
            "property_unit": unit.pk,
            "start_date": "2026-01-01",
            "assignment_number": "EF-DUP-JOINT",
            "novelty_type": OperationalNovelty.NoveltyType.CESSION,
            "change_reason": "Registro conjunto",
        },
    )

    assert response.status_code == 200
    assert FiduciaryAssignment.objects.filter(assignment_number="EF-DUP-JOINT").count() == 1
    assert not UnitOwnership.objects.filter(client=secondary_client, property_unit=unit).exists()
    assert "numero de encargo ya existe" in response.content.decode().lower()


@pytest.mark.django_db
def test_unit_can_have_historical_assignments_after_closing(active_client, unit):
    create_ownership(active_client, unit)
    first = create_assignment(unit, active_client, "EF-OLD")
    first.is_active = False
    first.end_date = "2026-02-01"
    first.save()
    first.holders.update(is_active=False, end_date="2026-02-01")

    second = create_assignment(unit, active_client, "EF-NEW", "2026-03-01")

    assert second.is_active


@pytest.mark.django_db
def test_reject_two_active_assignments_same_unit(active_client, unit):
    create_ownership(active_client, unit)
    create_assignment(unit, active_client, "EF-1")
    assignment = FiduciaryAssignment(
        assignment_number="EF-2",
        property_unit=unit,
        start_date="2026-02-01",
        last_change_reason="Duplicado",
    )

    with pytest.raises(ValidationError):
        assignment.full_clean()


@pytest.mark.django_db
def test_assignment_rejects_invalid_dates(unit):
    assignment = FiduciaryAssignment(
        assignment_number="EF-1",
        property_unit=unit,
        start_date="2026-02-01",
        end_date="2026-01-01",
        is_active=False,
    )

    with pytest.raises(ValidationError):
        assignment.full_clean()


@pytest.mark.django_db
def test_assignment_rejects_two_active_primary_holders(active_client, secondary_client, unit):
    create_ownership(active_client, unit, True)
    create_ownership(secondary_client, unit, False)
    assignment = create_assignment(unit, active_client)
    holder = FiduciaryAssignmentHolder(
        assignment=assignment,
        client=secondary_client,
        is_primary=True,
        start_date="2026-01-01",
    )

    with pytest.raises(ValidationError):
        holder.full_clean()


@pytest.mark.django_db
def test_assignment_rejects_holder_without_valid_unit_ownership(active_client, second_unit):
    assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-1",
        property_unit=second_unit,
        start_date="2026-01-01",
        last_change_reason="Registro",
    )
    holder = FiduciaryAssignmentHolder(assignment=assignment, client=active_client, is_primary=True, start_date="2026-01-01")

    with pytest.raises(ValidationError):
        holder.full_clean()


@pytest.mark.django_db
def test_close_assignment_preserves_it_and_closes_holders(accounting_client, active_client, unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client)

    response = accounting_client.post(
        reverse("fiduciary:assignment_close", args=[assignment.pk]),
        {"change_reason": "Cierre", "end_date": "2026-02-01"},
    )

    assignment.refresh_from_db()
    assert response.status_code == 302
    assert assignment.is_active is False
    assert assignment.holders.filter(is_active=True).count() == 0
    assert FiduciaryAssignment.objects.filter(pk=assignment.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_assignment_active_constraint_hits_postgresql(active_client, unit):
    create_ownership(active_client, unit)
    create_assignment(unit, active_client, "EF-1")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            FiduciaryAssignment.objects.bulk_create(
                [FiduciaryAssignment(assignment_number="EF-2", property_unit=unit, start_date="2026-02-01")]
            )


@pytest.mark.django_db
def test_assignment_creation_is_atomic_when_holder_invalid(accounting_client, active_client, unit):
    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        {
            "assignment_number": "EF-BAD",
            "property_unit": unit.pk,
            "start_date": "2026-01-01",
            "observations": "",
            "primary_client": active_client.pk,
            "change_reason": "Registro",
        },
    )

    assert response.status_code == 403
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-BAD").exists()


@pytest.mark.django_db
def test_permissions_for_fiduciary_views(accounting_client, commercial_client, client, active_client, unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client)

    assert client.get(reverse("fiduciary:client_list")).status_code == 302
    assert accounting_client.get(reverse("fiduciary:client_create")).status_code == 200
    assert commercial_client.get(reverse("fiduciary:client_list")).status_code == 200
    assert commercial_client.get(reverse("fiduciary:assignment_list")).status_code == 200
    assert commercial_client.get(reverse("fiduciary:client_create")).status_code == 200
    assert commercial_client.get(reverse("fiduciary:ownership_create")).status_code == 200
    assert commercial_client.get(reverse("fiduciary:assignment_create")).status_code == 403
    create_response = commercial_client.post(
        reverse("fiduciary:client_create"),
        {
            "document_type": FiduciaryClient.DocumentType.CITIZENSHIP_ID,
            "document_number": "COM-001",
            "first_names": "Cliente",
            "last_names_or_company": "Comercial",
            "phone": "3000000000",
            "email": "",
            "address": "",
            "is_active": "on",
        },
    )
    assert create_response.status_code == 302
    assert FiduciaryClient.objects.filter(document_number="COM-001").exists()
    assert commercial_client.get(reverse("fiduciary:client_update", args=[active_client.pk])).status_code == 403
    assert commercial_client.get(reverse("fiduciary:assignment_update", args=[assignment.pk])).status_code == 403
    assert commercial_client.post(reverse("fiduciary:client_status", args=[active_client.pk, "deactivate"])).status_code == 403
    assert commercial_client.post(reverse("fiduciary:assignment_close", args=[assignment.pk])).status_code == 403


@pytest.mark.django_db
def test_write_post_requires_csrf(accounting_admin_user):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(accounting_admin_user)

    response = csrf_client.post(reverse("fiduciary:client_create"), {})

    assert response.status_code == 403


@pytest.mark.django_db
def test_delete_endpoints_do_not_exist(accounting_client, active_client, unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client)

    assert accounting_client.post(f"/fiduciary/clients/{active_client.pk}/delete/").status_code == 404
    assert accounting_client.post(f"/fiduciary/assignments/{assignment.pk}/delete/").status_code == 404
    assert FiduciaryClient.objects.filter(pk=active_client.pk).exists()


@pytest.mark.django_db
def test_client_detail_and_assignment_list_show_related_information(accounting_client, active_client, secondary_client, unit):
    create_ownership(active_client, unit, True)
    create_ownership(secondary_client, unit, False)
    assignment = create_assignment(unit, active_client)
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=secondary_client,
        is_primary=False,
        start_date="2026-01-01",
        last_change_reason="Registro",
    )

    client_detail = accounting_client.get(reverse("fiduciary:client_detail", args=[active_client.pk])).content.decode()
    assignment_list = accounting_client.get(reverse("fiduciary:assignment_list"), {"project": unit.project_id}).content.decode()

    assert unit.name in client_detail
    assert assignment.assignment_number in client_detail
    assert active_client.full_name in assignment_list
    assert secondary_client.full_name in assignment_list


@pytest.mark.django_db
def test_assignment_detail_shows_real_payments(accounting_client, accounting_admin_user, active_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client)
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        status=ImportBatch.Status.COMPLETED,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="LIBRO.xlsx",
        extension=".xlsx",
        size_bytes=100,
        sha256="a" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
        status=ImportedFile.Status.COMPLETED,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.MONTH,
        period_year=2026,
        period_month=7,
        amount="1500000.00",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T2",
        source_row=5,
        source_column="T",
    )

    content = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk])).content.decode()

    assert "Pagos registrados" in content
    assert "LIBRO.xlsx" in content
    assert "T2 fila 5" in content
    assert "No se han realizado pagos." not in content


@pytest.mark.django_db
def test_payment_list_starts_empty_until_filter_is_selected(accounting_client, accounting_admin_user, active_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-PAY-EMPTY")
    create_payment_for_assignment(assignment, accounting_admin_user, "2500000.00")

    response = accounting_client.get(reverse("fiduciary:payment_list"))
    content = response.content.decode()

    assert response.status_code == 200
    assert "Seleccione al menos un criterio de busqueda" in content
    assert "2500000.00" not in content


@pytest.mark.django_db
def test_payment_list_filters_and_links_related_entities(accounting_client, accounting_admin_user, active_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-PAY-FILTER")
    create_payment_for_assignment(assignment, accounting_admin_user, "3500000.00")

    response = accounting_client.get(
        reverse("fiduciary:payment_list"),
        {"project": unit.project_id, "document": "123", "assignment_number": "PAY-FILTER"},
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert "3.500.000" in content
    assert reverse("fiduciary:client_detail", args=[active_client.pk]) in content
    assert reverse("fiduciary:assignment_detail", args=[assignment.pk]) in content
    assert reverse("real_estate:property_unit_history", args=[unit.pk]) in content
    assert reverse("real_estate:project_list") in content


@pytest.mark.django_db
def test_audit_list_filters_and_detail_show_import_trace(accounting_client, accounting_admin_user):
    batch, imported_file = create_imported_file(accounting_admin_user)
    record = ImportAppliedRecord.objects.create(
        batch=batch,
        imported_file=imported_file,
        entity_kind=ImportAppliedRecord.EntityKind.PAYMENT,
        entity_id=99,
        action=ImportAppliedRecord.Action.CREATED,
        source_row=7,
        source_column="AA",
        summary="Pago creado desde prueba",
    )

    response = accounting_client.get(
        reverse("fiduciary:audit_list"),
        {"responsible": accounting_admin_user.pk, "action": ImportAppliedRecord.Action.CREATED, "entity_kind": ImportAppliedRecord.EntityKind.PAYMENT, "reason": "Pago creado"},
    )
    content = response.content.decode()
    detail_response = accounting_client.get(reverse("fiduciary:audit_detail", args=[record.pk]))
    detail_content = detail_response.content.decode()

    assert response.status_code == 200
    assert "Pago creado desde prueba" in content
    assert reverse("fiduciary:audit_detail", args=[record.pk]) in content
    assert detail_response.status_code == 200
    assert "Pago creado desde prueba" in detail_content
    assert "AA" in detail_content
    assert "99" in detail_content


@pytest.mark.django_db
def test_sidebar_marks_current_module_and_removes_consultas(accounting_client, accounting_admin_user, active_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-SIDEBAR")
    create_payment_for_assignment(assignment, accounting_admin_user)

    payment_content = accounting_client.get(reverse("fiduciary:payment_list"), {"project": unit.project_id}).content.decode()
    audit_content = accounting_client.get(reverse("fiduciary:audit_list")).content.decode()

    assert 'href="/fiduciary/payments/"' in payment_content
    assert 'nav-link active" href="/fiduciary/payments/"' in payment_content
    assert 'nav-link active" href="/fiduciary/audit/"' in audit_content
    assert "Consultas" not in payment_content
    assert "Consultas" not in audit_content


@pytest.mark.django_db
def test_assignment_change_preserves_previous_payments(accounting_client, accounting_admin_user, active_client, secondary_client, unit):
    create_ownership(active_client, unit, True)
    create_ownership(secondary_client, unit, False)
    assignment = create_assignment(unit, active_client, "EF-OLD")
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=secondary_client,
        is_primary=False,
        start_date="2026-01-01",
        last_change_reason="Registro manual",
    )
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        status=ImportBatch.Status.COMPLETED,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="LIBRO.xlsx",
        extension=".xlsx",
        size_bytes=100,
        sha256="b" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
        status=ImportedFile.Status.COMPLETED,
    )
    payment = Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.MONTH,
        period_year=2026,
        period_month=7,
        amount="1500000.00",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T2",
        source_row=5,
        source_column="T",
    )

    response = accounting_client.post(
        reverse("fiduciary:assignment_change", args=[assignment.pk]),
        {
            "new_assignment_number": "EF-NEW",
            "effective_date": "2026-08-01",
            "novelty_type": "substitution",
            "reason": "Cambio contractual",
            "primary_client": str(active_client.pk),
            "secondary_clients": [str(secondary_client.pk)],
        },
    )

    assignment.refresh_from_db()
    payment.refresh_from_db()
    new_assignment = FiduciaryAssignment.objects.get(assignment_number="EF-NEW")
    assert response.status_code == 302
    assert assignment.is_active is False
    assert new_assignment.is_active is True
    assert payment.assignment == assignment
    assert new_assignment.holders.filter(client=active_client, is_active=True).exists()
    assert new_assignment.holders.filter(client=secondary_client, is_active=True).exists()
    assert FiduciaryAssignment.objects.filter(property_unit=unit, is_active=True).count() == 1


@pytest.mark.django_db
def test_assignment_withdrawal_leaves_unit_without_active_holders(accounting_client, active_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-RET")

    response = accounting_client.post(
        reverse("fiduciary:assignment_change", args=[assignment.pk]),
        {
            "new_assignment_number": "",
            "effective_date": "2026-08-01",
            "novelty_type": "withdrawal",
            "reason": "Retiro del titular",
            "primary_client": "",
            "secondary_clients": [],
        },
    )

    assignment.refresh_from_db()
    assert response.status_code == 302
    assert assignment.is_active is True
    assert assignment.holders.filter(is_active=True).count() == 0
    assert UnitOwnership.objects.filter(property_unit=unit, is_active=True).count() == 0


@pytest.mark.django_db
def test_assignment_other_requires_description(accounting_client, active_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-OTHER")

    response = accounting_client.post(
        reverse("fiduciary:assignment_change", args=[assignment.pk]),
        {
            "new_assignment_number": "",
            "effective_date": "2026-08-01",
            "novelty_type": "other",
            "reason": "Otro",
            "other_description": "",
            "primary_client": active_client.pk,
            "secondary_clients": [],
        },
    )

    assert response.status_code == 200
    assert "Describa la novedad" in response.content.decode()


@pytest.mark.django_db
def test_commercial_cannot_change_assignment(commercial_client, active_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-COM")

    response = commercial_client.get(reverse("fiduciary:assignment_change", args=[assignment.pk]))

    assert response.status_code == 403


@pytest.mark.django_db
def test_assignment_filters_do_not_mix_projects(accounting_client, active_client, unit, external_unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client, "EF-IN")
    external_client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="EXT",
        last_names_or_company="Externo",
        phone="300",
    )
    create_ownership(external_client, external_unit)
    create_assignment(external_unit, external_client, "EF-OUT")

    response = accounting_client.get(reverse("fiduciary:assignment_list"), {"project": unit.project_id})
    content = response.content.decode()

    assert assignment.assignment_number in content
    assert "EF-OUT" not in content


@pytest.mark.parametrize(
    ("raw_name", "expected_first", "expected_last"),
    [
        ("RINCON CARDONA LUZ ADRIANA", "LUZ ADRIANA", "RINCON CARDONA"),
        ("CAMARGO TORRES DELASCAR", "DELASCAR", "CAMARGO TORRES"),
        ("PEREZ JUAN DAVID", "DAVID", "PEREZ JUAN"),
        ("JUAN PEREZ", "PEREZ", "JUAN"),
        ("MARIA", "MARIA", ""),
        ("DE LA CRUZ MARIA CAMILA SOFIA", "CRUZ MARIA CAMILA SOFIA", "DE LA"),
        ("  RUIZ   ANA  MARIA  ", "MARIA", "RUIZ ANA"),
    ],
)
def test_split_imported_full_name(raw_name, expected_first, expected_last):
    assert split_imported_full_name(raw_name) == (expected_first, expected_last)


@pytest.mark.django_db
def test_property_unit_view_shows_real_holders_and_assignment(accounting_client, active_client, secondary_client, unit):
    create_ownership(active_client, unit, True)
    create_ownership(secondary_client, unit, False)
    assignment = create_assignment(unit, active_client)

    response = accounting_client.get(reverse("real_estate:property_unit_list"), {"project": unit.project_id})
    content = response.content.decode()

    assert active_client.full_name in content
    assert "Principal" in content
    assert assignment.assignment_number in content
    assert "No se han realizado pagos aun" in content


@pytest.mark.django_db
def test_property_unit_view_shows_last_payment(accounting_client, accounting_admin_user, active_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client)
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        status=ImportBatch.Status.COMPLETED,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="LIBRO.xlsx",
        extension=".xlsx",
        size_bytes=100,
        sha256="c" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
        status=ImportedFile.Status.COMPLETED,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.MONTH,
        period_year=2026,
        period_month=7,
        amount="1500000.00",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T2",
        source_row=5,
        source_column="T",
    )

    response = accounting_client.get(reverse("real_estate:property_unit_list"), {"project": unit.project_id})
    content = response.content.decode()

    assert "7/2026" in content
    assert "1.500.000" in content
    assert "No se han realizado pagos aun" not in content


@pytest.mark.django_db
def test_client_form_and_detail_show_contact_label(accounting_client, active_client):
    form_response = accounting_client.get(reverse("fiduciary:client_create"))
    detail_response = accounting_client.get(reverse("fiduciary:client_detail", args=[active_client.pk]))

    assert "Contacto" in form_response.content.decode()
    assert "Contacto" in detail_response.content.decode()
    assert "Direccion" not in form_response.content.decode() + detail_response.content.decode()


@pytest.mark.django_db
def test_property_unit_view_shows_empty_messages(accounting_client, unit):
    response = accounting_client.get(reverse("real_estate:property_unit_list"), {"project": unit.project_id})
    content = response.content.decode()

    assert "Sin titular" in content
    assert "Sin encargo fiduciario" in content


@pytest.mark.django_db
def test_commercial_templates_show_create_but_not_update_or_delete_actions(commercial_client, active_client, unit):
    create_ownership(active_client, unit)
    create_assignment(unit, active_client)

    clients = commercial_client.get(reverse("fiduciary:client_list")).content.decode()
    assignments = commercial_client.get(reverse("fiduciary:assignment_list")).content.decode()

    assert "Nuevo cliente" in clients
    assert "Editar" not in clients
    assert "Nuevo encargo" not in assignments
    assert "Eliminar" not in clients + assignments


@pytest.mark.django_db
def test_accounting_can_create_manual_observation(accounting_client, accounting_admin_user, active_client, unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client, "EF-OBS")

    response = accounting_client.post(
        reverse("fiduciary:observation_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "client": active_client.pk,
            "assignment": assignment.pk,
            "summary": "Seguimiento",
            "detail": "Observacion manual de seguimiento.",
        },
    )

    assert response.status_code == 302
    observation = ImportedHistoricalObservation.objects.get(summary="Seguimiento")
    assert observation.origin == ImportedHistoricalObservation.Origin.MANUAL
    assert observation.imported_by == accounting_admin_user
    assert observation.property_unit == unit
    assert observation.assignment == assignment


@pytest.mark.django_db
def test_commercial_cannot_create_or_edit_observations(commercial_client, active_client, unit, accounting_admin_user):
    create_ownership(active_client, unit)
    observation = ImportedHistoricalObservation.objects.create(
        project=unit.project,
        property_unit=unit,
        client=active_client,
        origin=ImportedHistoricalObservation.Origin.MANUAL,
        summary="Manual",
        detail="Solo lectura para comercial.",
        imported_by=accounting_admin_user,
    )

    create_response = commercial_client.get(reverse("fiduciary:observation_create"))
    edit_response = commercial_client.get(reverse("fiduciary:observation_update", args=[observation.pk]))

    assert create_response.status_code == 403
    assert edit_response.status_code == 403


@pytest.mark.django_db
def test_imported_observations_are_read_only_for_accounting(accounting_client, active_client, unit, accounting_admin_user):
    create_ownership(active_client, unit)
    observation = ImportedHistoricalObservation.objects.create(
        project=unit.project,
        property_unit=unit,
        client=active_client,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        summary="Importada",
        detail="No editable.",
        imported_by=accounting_admin_user,
    )

    response = accounting_client.get(reverse("fiduciary:observation_update", args=[observation.pk]))

    assert response.status_code == 403


@pytest.mark.django_db
def test_observation_filters_by_unit_document_and_assignment(accounting_client, active_client, secondary_client, unit, second_unit, accounting_admin_user):
    create_ownership(active_client, unit)
    create_ownership(secondary_client, second_unit)
    assignment = create_assignment(unit, active_client, "EF-FILTER")
    other_assignment = create_assignment(second_unit, secondary_client, "EF-OTHER")
    ImportedHistoricalObservation.objects.create(
        project=unit.project,
        property_unit=unit,
        client=active_client,
        assignment=assignment,
        origin=ImportedHistoricalObservation.Origin.MANUAL,
        summary="Incluida",
        detail="Coincide con filtros.",
        imported_by=accounting_admin_user,
    )
    ImportedHistoricalObservation.objects.create(
        project=second_unit.project,
        property_unit=second_unit,
        client=secondary_client,
        assignment=other_assignment,
        origin=ImportedHistoricalObservation.Origin.MANUAL,
        summary="Excluida",
        detail="No coincide.",
        imported_by=accounting_admin_user,
    )

    response = accounting_client.get(
        reverse("fiduciary:observation_list"),
        {
            "property_unit": unit.pk,
            "document": "123",
            "assignment_number": "FILTER",
        },
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert "Incluida" in content
    assert "Excluida" not in content


@pytest.mark.django_db
def test_observations_are_visible_from_unit_history(accounting_client, active_client, unit, accounting_admin_user):
    create_ownership(active_client, unit)
    ImportedHistoricalObservation.objects.create(
        project=unit.project,
        property_unit=unit,
        client=active_client,
        origin=ImportedHistoricalObservation.Origin.MANUAL,
        summary="Linea temporal",
        detail="Visible en historial de unidad.",
        imported_by=accounting_admin_user,
    )

    response = accounting_client.get(reverse("real_estate:property_unit_history", args=[unit.pk]))
    content = response.content.decode()

    assert response.status_code == 200
    assert "Visible en historial de unidad." in content


@pytest.mark.django_db
def test_observation_list_shows_summary_detail_and_detail_page_with_long_text(
    accounting_client, active_client, unit, accounting_admin_user
):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client, "EF-OBS-LONG")
    long_summary = "Resumen largo " + ("con contexto " * 20)
    long_detail = "Detalle largo " + ("sin recortes en el detalle " * 25)
    observation = ImportedHistoricalObservation.objects.create(
        project=unit.project,
        property_unit=unit,
        client=active_client,
        assignment=assignment,
        origin=ImportedHistoricalObservation.Origin.MANUAL,
        summary=long_summary,
        detail=long_detail,
        imported_by=accounting_admin_user,
    )

    list_response = accounting_client.get(reverse("fiduciary:observation_list"))
    list_content = list_response.content.decode()
    detail_response = accounting_client.get(reverse("fiduciary:observation_detail", args=[observation.pk]))
    detail_content = detail_response.content.decode()

    assert list_response.status_code == 200
    assert "<th>Resumen</th><th>Detalle</th>" in list_content
    assert "Ver detalle" in list_content
    assert reverse("fiduciary:observation_detail", args=[observation.pk]) in list_content
    assert detail_response.status_code == 200
    assert long_summary in detail_content
    assert long_detail in detail_content
    assert reverse("fiduciary:client_detail", args=[active_client.pk]) in detail_content
    assert reverse("fiduciary:assignment_detail", args=[assignment.pk]) in detail_content


@pytest.mark.django_db
def test_observation_context_limits_clients_and_assignments_to_selected_unit(
    accounting_client, active_client, secondary_client, unit, second_unit
):
    create_ownership(active_client, unit)
    create_ownership(secondary_client, second_unit)
    assignment = create_assignment(unit, active_client, "EF-CTX")
    create_assignment(second_unit, secondary_client, "EF-CTX-OTHER")

    response = accounting_client.get(reverse("fiduciary:observation_context"), {"project": unit.project_id, "unit": unit.pk})
    payload = response.json()

    assert response.status_code == 200
    assert {row["id"] for row in payload["units"]} == {unit.pk, second_unit.pk}
    assert {row["id"] for row in payload["clients"]} == {active_client.pk}
    assert {row["id"] for row in payload["assignments"]} == {assignment.pk}


@pytest.mark.django_db
def test_unit_history_separates_observations_from_operational_novelties(
    accounting_client, accounting_admin_user, active_client, secondary_client, unit
):
    create_ownership(active_client, unit)
    create_ownership(secondary_client, unit, False)
    assignment = create_assignment(unit, active_client, "EF-HIST")
    ImportedHistoricalObservation.objects.create(
        project=unit.project,
        property_unit=unit,
        client=active_client,
        assignment=assignment,
        origin=ImportedHistoricalObservation.Origin.MANUAL,
        summary="Observacion separada",
        detail="Debe aparecer solo como observacion.",
        imported_by=accounting_admin_user,
    )
    OperationalNovelty.objects.create(
        project=unit.project,
        property_unit=unit,
        novelty_type=OperationalNovelty.NoveltyType.HISTORICAL,
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.IMPORTED,
        historical_client=secondary_client,
        historical_assignment=assignment,
        summary="*TERMIN.SIN ABONOS",
        detail="Novedad historica importada.",
        created_by=accounting_admin_user,
    )

    response = accounting_client.get(reverse("real_estate:property_unit_history", args=[unit.pk]))
    content = response.content.decode()

    assert response.status_code == 200
    assert "Observaciones" in content
    assert "Novedades" in content
    assert "Debe aparecer solo como observacion." in content
    assert "*TERMIN.SIN ABONOS" in content
    assert "Importacion historica" in content


@pytest.mark.django_db
def test_operational_novelty_withdrawal_leaves_unit_without_active_primary_holder(
    accounting_client, active_client, unit, accounting_admin_user
):
    ownership = create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-WITHDRAW")

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "novelty_type": OperationalNovelty.NoveltyType.WITHDRAWAL,
            "effective_date": "2026-04-01",
            "summary": "Retiro voluntario",
            "detail": "Se conserva el encargo sin titular principal.",
        },
    )

    assert response.status_code == 302
    ownership.refresh_from_db()
    assignment.refresh_from_db()
    assert ownership.is_active is False
    assert assignment.is_active is True
    assert not assignment.holders.filter(is_primary=True, is_active=True).exists()
    assert OperationalNovelty.objects.filter(
        property_unit=unit,
        previous_client=active_client,
        previous_assignment=assignment,
        novelty_type=OperationalNovelty.NoveltyType.WITHDRAWAL,
        created_by=accounting_admin_user,
    ).exists()


@pytest.mark.django_db
def test_operational_novelty_cession_creates_new_assignment_and_secondary_holders(
    accounting_client, active_client, secondary_client, unit
):
    other_client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="789",
        first_names="Marta",
        last_names_or_company="Gomez",
        phone="300789",
    )
    create_ownership(active_client, unit)
    current_assignment = create_assignment(unit, active_client, "EF-CESSION-OLD")

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "novelty_type": OperationalNovelty.NoveltyType.CESSION,
            "effective_date": "2026-05-01",
            "new_client": secondary_client.pk,
            "new_assignment_number": "EF-CESSION-NEW",
            "secondary_clients": [other_client.pk],
            "summary": "Cesion",
            "detail": "Cambio de titular principal.",
        },
    )

    assert response.status_code == 302
    current_assignment.refresh_from_db()
    assert current_assignment.is_active is False
    new_assignment = FiduciaryAssignment.objects.get(assignment_number="EF-CESSION-NEW")
    assert new_assignment.holders.get(client=secondary_client).is_primary is True
    assert new_assignment.holders.get(client=other_client).is_primary is False
    assert UnitOwnership.objects.get(client=secondary_client, property_unit=unit).is_primary is True
    assert OperationalNovelty.objects.filter(
        property_unit=unit,
        previous_assignment=current_assignment,
        new_assignment=new_assignment,
        previous_client=active_client,
        new_client=secondary_client,
    ).exists()


@pytest.mark.django_db
def test_novelty_detail_opens_with_links_and_related_observations(
    accounting_client, accounting_admin_user, active_client, unit
):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client, "EF-NOV-DETAIL")
    observation = ImportedHistoricalObservation.objects.create(
        project=unit.project,
        property_unit=unit,
        client=active_client,
        assignment=assignment,
        origin=ImportedHistoricalObservation.Origin.MANUAL,
        summary="Resumen relacionado",
        detail="Detalle relacionado completo.",
        imported_by=accounting_admin_user,
    )
    novelty = OperationalNovelty.objects.create(
        project=unit.project,
        property_unit=unit,
        novelty_type=OperationalNovelty.NoveltyType.HISTORICAL,
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.IMPORTED,
        historical_client=active_client,
        historical_assignment=assignment,
        source_observation=observation,
        summary="Novedad detalle",
        detail="Detalle de novedad completo.",
        created_by=accounting_admin_user,
    )

    response = accounting_client.get(reverse("fiduciary:novelty_detail", args=[novelty.pk]))
    content = response.content.decode()

    assert response.status_code == 200
    assert "Novedad detalle" in content
    assert reverse("fiduciary:client_detail", args=[active_client.pk]) in content
    assert reverse("fiduciary:assignment_detail", args=[assignment.pk]) in content
    assert "Resumen relacionado" in content
    assert reverse("fiduciary:observation_detail", args=[observation.pk]) in content


@pytest.mark.django_db
def test_novelty_list_links_client_unit_and_assignment(accounting_client, accounting_admin_user, active_client, unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client, "EF-NOV-LINK")
    OperationalNovelty.objects.create(
        project=unit.project,
        property_unit=unit,
        novelty_type=OperationalNovelty.NoveltyType.HISTORICAL,
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.IMPORTED,
        historical_client=active_client,
        historical_assignment=assignment,
        summary="Novedad enlazada",
        created_by=accounting_admin_user,
    )

    response = accounting_client.get(reverse("fiduciary:novelty_list"))
    content = response.content.decode()

    assert response.status_code == 200
    assert reverse("fiduciary:client_detail", args=[active_client.pk]) in content
    assert reverse("fiduciary:assignment_detail", args=[assignment.pk]) in content
    assert reverse("real_estate:property_unit_history", args=[unit.pk]) in content


@pytest.mark.django_db
def test_novelty_create_redirects_to_created_detail(accounting_client, active_client, secondary_client, unit):
    create_ownership(active_client, unit)
    create_assignment(unit, active_client, "EF-NOV-OLD")

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "novelty_type": OperationalNovelty.NoveltyType.CESSION,
            "effective_date": "2026-05-01",
            "new_client": secondary_client.pk,
            "new_assignment_number": "EF-NOV-NEW",
            "summary": "Cesion con redireccion",
            "detail": "Debe abrir el detalle creado.",
        },
    )

    novelty = OperationalNovelty.objects.get(summary="Cesion con redireccion")
    assert response.status_code == 302
    assert response.url == reverse("fiduciary:novelty_detail", args=[novelty.pk])
    detail_response = accounting_client.get(response.url)
    assert detail_response.status_code == 200
    assert "Cesion con redireccion" in detail_response.content.decode()


@pytest.mark.django_db
def test_operational_novelty_other_requires_custom_type(accounting_client, active_client, unit):
    create_ownership(active_client, unit)
    create_assignment(unit, active_client, "EF-OTHER")

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "novelty_type": OperationalNovelty.NoveltyType.OTHER,
            "summary": "Otro",
            "detail": "Descripcion sin clasificacion.",
        },
    )

    assert response.status_code == 200
    assert "Indique cual es la novedad" in response.content.decode()
    assert OperationalNovelty.objects.count() == 0


@pytest.mark.django_db
def test_commercial_can_read_but_cannot_create_operational_novelties(
    commercial_client, accounting_admin_user, active_client, unit
):
    create_ownership(active_client, unit)
    OperationalNovelty.objects.create(
        project=unit.project,
        property_unit=unit,
        novelty_type=OperationalNovelty.NoveltyType.HISTORICAL,
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.IMPORTED,
        historical_client=active_client,
        summary="Novedad consultable",
        created_by=accounting_admin_user,
    )

    list_response = commercial_client.get(reverse("fiduciary:novelty_list"))
    create_response = commercial_client.get(reverse("fiduciary:novelty_create"))

    assert list_response.status_code == 200
    assert "Novedad consultable" in list_response.content.decode()
    assert create_response.status_code == 403


@pytest.mark.django_db
def test_assignment_pagination_preserves_filters(accounting_client, active_client, unit):
    create_ownership(active_client, unit)
    for index in range(18):
        assignment = FiduciaryAssignment.objects.create(
            assignment_number=f"EF-{index:02}",
            property_unit=unit,
            start_date=f"2026-01-{index + 1:02}",
            is_active=False,
            end_date=f"2026-02-{index + 1:02}",
            last_change_reason="Historico",
        )
        FiduciaryAssignmentHolder.objects.create(
            assignment=assignment,
            client=active_client,
            is_primary=True,
            start_date=assignment.start_date,
            is_active=False,
            end_date=assignment.end_date,
            last_change_reason="Historico",
        )

    response = accounting_client.get(reverse("fiduciary:assignment_list"), {"q": "EF", "page": 2})
    content = response.content.decode()

    assert "Pagina 2 de 2" in content
    assert "q=EF" in content
