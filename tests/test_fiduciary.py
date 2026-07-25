import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from fiduciary.models import Client as FiduciaryClient
from fiduciary.models import FiduciaryAssignment, FiduciaryAssignmentHolder, UnitOwnership
from fiduciary.forms import DIRECT_UNITS_VALUE
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
def test_client_can_have_phone_email_and_address():
    client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="779",
        last_names_or_company="Contacto Completo",
        phone="300",
        email="completo@example.com",
        address="Calle 1",
    )

    assert client.address == "Calle 1"


@pytest.mark.django_db
def test_client_rejects_address_without_phone_or_email():
    client = FiduciaryClient(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="780",
        last_names_or_company="Solo Direccion",
        address="Calle 1",
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
        address="Calle 1",
    )

    with pytest.raises(ValidationError, match="telefono o un correo"):
        client.full_clean()


@pytest.mark.django_db
def test_client_form_rejects_address_only():
    from fiduciary.forms import ClientForm

    form = ClientForm(
        data={
            "document_type": FiduciaryClient.DocumentType.CITIZENSHIP_ID,
            "document_number": "782",
            "first_names": "",
            "last_names_or_company": "Solo Direccion",
            "phone": "",
            "email": "",
            "address": "Calle 1",
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
def test_finalize_ownership_preserves_record(accounting_client, active_client, unit):
    ownership = create_ownership(active_client, unit)

    response = accounting_client.post(
        reverse("fiduciary:ownership_finalize", args=[ownership.pk]),
        {"change_reason": "Finalizacion", "end_date": "2026-02-01"},
    )

    ownership.refresh_from_db()
    assert response.status_code == 302
    assert ownership.is_active is False
    assert UnitOwnership.objects.filter(pk=ownership.pk).exists()


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
def test_temporary_assignment_form_notice_and_formset_html(accounting_client):
    response = accounting_client.get(reverse("fiduciary:assignment_create"))
    content = response.content.decode()

    assert response.status_code == 200
    assert "Herramienta temporal de validacion" in content
    assert "id_holders-TOTAL_FORMS" in content
    assert "empty-secondary-holder-template" in content
    assert "Agregar titular secundario" in content
    assert "remove-secondary-holder" in content
    assert "data-context-field=\"property-unit\" disabled" in content
    assert "data-holder-role=\"primary\" disabled" in content


@pytest.mark.django_db
def test_assignment_form_does_not_preload_all_units_or_holders(accounting_client, active_client, unit, external_unit):
    create_ownership(active_client, unit)

    response = accounting_client.get(reverse("fiduciary:assignment_create"))
    content = response.content.decode()

    assert response.status_code == 200
    assert str(unit) not in content
    assert str(external_unit) not in content
    assert active_client.full_name not in content


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
def test_create_assignment_form_with_primary_and_no_secondary(accounting_client, active_client, unit):
    create_ownership(active_client, unit)

    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        assignment_post_data(unit, active_client, "EF-NO-SEC", blank_rows=1),
    )

    assignment = FiduciaryAssignment.objects.get(assignment_number="EF-NO-SEC")
    assert response.status_code == 302
    assert assignment.holders.count() == 1
    assert assignment.holders.get().is_primary is True


@pytest.mark.django_db
def test_create_assignment_form_with_one_two_three_and_more_secondaries(accounting_client, active_client, unit):
    create_ownership(active_client, unit, True)
    secondary_clients = []
    for index in range(5):
        client = FiduciaryClient.objects.create(
            document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
            document_number=f"SEC-{index}",
            last_names_or_company=f"Secundario {index}",
            phone="300",
        )
        create_ownership(client, unit, False)
        secondary_clients.append(client)

    for count in [1, 2, 3, 5]:
        current_unit = PropertyUnit.objects.create(project=unit.project, code=f"UF-{count}", name=f"Unidad Form {count}")
        create_ownership(active_client, current_unit, True)
        for client in secondary_clients[:count]:
            create_ownership(client, current_unit, False)
        response = accounting_client.post(
            reverse("fiduciary:assignment_create"),
            assignment_post_data(current_unit, active_client, f"EF-SEC-{count}", secondary_clients[:count]),
        )
        assignment = FiduciaryAssignment.objects.get(assignment_number=f"EF-SEC-{count}")
        assert response.status_code == 302
        assert assignment.holders.filter(is_primary=False).count() == count


@pytest.mark.django_db
def test_assignment_form_empty_secondary_row_does_not_error(accounting_client, active_client, unit):
    create_ownership(active_client, unit)

    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        assignment_post_data(unit, active_client, "EF-EMPTY", blank_rows=1),
    )

    assert response.status_code == 302
    assert FiduciaryAssignment.objects.get(assignment_number="EF-EMPTY").holders.count() == 1


@pytest.mark.django_db
def test_assignment_form_deleted_secondary_row_is_ignored(accounting_client, active_client, secondary_client, unit):
    create_ownership(active_client, unit, True)
    create_ownership(secondary_client, unit, False)

    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        assignment_post_data(unit, active_client, "EF-DELETED", deleted_clients=[secondary_client]),
    )

    assignment = FiduciaryAssignment.objects.get(assignment_number="EF-DELETED")
    assert response.status_code == 302
    assert assignment.holders.filter(is_primary=False).count() == 0


@pytest.mark.django_db
def test_assignment_form_deleted_incompatible_secondary_row_is_ignored(accounting_client, active_client, secondary_client, unit, second_unit):
    create_ownership(active_client, unit, True)
    create_ownership(secondary_client, second_unit, True)

    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        assignment_post_data(unit, active_client, "EF-DELETED-INCOMPATIBLE", deleted_clients=[secondary_client]),
    )

    assignment = FiduciaryAssignment.objects.get(assignment_number="EF-DELETED-INCOMPATIBLE")
    assert response.status_code == 302
    assert assignment.holders.filter(is_primary=False).count() == 0


@pytest.mark.django_db
def test_assignment_form_rejects_duplicate_secondary(accounting_client, active_client, secondary_client, unit):
    create_ownership(active_client, unit, True)
    create_ownership(secondary_client, unit, False)

    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        assignment_post_data(unit, active_client, "EF-DUP", [secondary_client, secondary_client]),
    )

    assert response.status_code == 200
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-DUP").exists()
    assert "No puede seleccionar el mismo titular secundario" in response.content.decode()


@pytest.mark.django_db
def test_assignment_form_rejects_primary_as_secondary(accounting_client, active_client, unit):
    create_ownership(active_client, unit, True)

    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        assignment_post_data(unit, active_client, "EF-PRIMARY-DUP", [active_client]),
    )

    assert response.status_code == 200
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-PRIMARY-DUP").exists()
    assert "titular principal no debe repetirse" in response.content.decode()


@pytest.mark.django_db
def test_assignment_form_requires_exactly_one_primary(accounting_client, active_client, unit):
    create_ownership(active_client, unit)

    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        assignment_post_data(unit, None, "EF-NO-PRIMARY"),
    )

    assert response.status_code == 200
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-NO-PRIMARY").exists()


@pytest.mark.django_db
def test_assignment_form_rejects_client_without_ownership_or_other_unit(accounting_client, active_client, secondary_client, unit, second_unit):
    create_ownership(active_client, unit)
    create_ownership(secondary_client, second_unit)

    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        assignment_post_data(unit, active_client, "EF-WRONG-UNIT", [secondary_client]),
    )

    assert response.status_code == 200
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-WRONG-UNIT").exists()


@pytest.mark.django_db
def test_assignment_form_rejects_finalized_inactive_ownership_and_inactive_client(accounting_client, active_client, secondary_client, unit):
    create_ownership(active_client, unit)
    inactive_ownership = create_ownership(secondary_client, unit, False)
    inactive_ownership.is_active = False
    inactive_ownership.end_date = "2026-02-01"
    inactive_ownership.save()

    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        assignment_post_data(unit, active_client, "EF-FINALIZED", [secondary_client]),
    )
    assert response.status_code == 200
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-FINALIZED").exists()

    secondary_client.is_active = False
    secondary_client.save(update_fields=["is_active"])
    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        assignment_post_data(unit, active_client, "EF-INACTIVE-CLIENT", [secondary_client]),
    )
    assert response.status_code == 200
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-INACTIVE-CLIENT").exists()


@pytest.mark.django_db
def test_assignment_form_unit_without_active_holders_shows_clear_message(accounting_client, active_client, unit):
    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        assignment_post_data(unit, active_client, "EF-NO-HOLDERS"),
    )

    assert response.status_code == 200
    assert "La unidad seleccionada no tiene titulares vigentes" in response.content.decode()


@pytest.mark.django_db
def test_assignment_form_does_not_leave_partial_assignment_when_formset_fails(accounting_client, active_client, secondary_client, unit):
    create_ownership(active_client, unit)

    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        assignment_post_data(unit, active_client, "EF-PARTIAL", [secondary_client]),
    )

    assert response.status_code == 200
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-PARTIAL").exists()
    assert not FiduciaryAssignmentHolder.objects.filter(assignment__assignment_number="EF-PARTIAL").exists()


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

    assert response.status_code == 200
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-BAD").exists()


@pytest.mark.django_db
def test_permissions_for_fiduciary_views(accounting_client, commercial_client, client, active_client):
    assert client.get(reverse("fiduciary:client_list")).status_code == 302
    assert accounting_client.get(reverse("fiduciary:client_create")).status_code == 200
    assert commercial_client.get(reverse("fiduciary:client_list")).status_code == 200
    assert commercial_client.get(reverse("fiduciary:assignment_list")).status_code == 200
    assert commercial_client.get(reverse("fiduciary:client_create")).status_code == 403
    assert commercial_client.get(reverse("fiduciary:ownership_create")).status_code == 403
    assert commercial_client.get(reverse("fiduciary:assignment_create")).status_code == 403
    assert commercial_client.post(reverse("fiduciary:client_status", args=[active_client.pk, "deactivate"])).status_code == 403


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
def test_property_unit_view_shows_empty_messages(accounting_client, unit):
    response = accounting_client.get(reverse("real_estate:property_unit_list"), {"project": unit.project_id})
    content = response.content.decode()

    assert "Sin titular" in content
    assert "Sin encargo fiduciario" in content


@pytest.mark.django_db
def test_commercial_templates_do_not_show_write_or_delete_actions(commercial_client, active_client, unit):
    create_ownership(active_client, unit)
    create_assignment(unit, active_client)

    clients = commercial_client.get(reverse("fiduciary:client_list")).content.decode()
    assignments = commercial_client.get(reverse("fiduciary:assignment_list")).content.decode()

    assert "Nuevo cliente" not in clients
    assert "Editar" not in clients
    assert "Nuevo encargo" not in assignments
    assert "Eliminar" not in clients + assignments


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
