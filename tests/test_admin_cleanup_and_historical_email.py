from datetime import date
from pathlib import Path

import pytest
from django.contrib.admin.models import LogEntry
from django.urls import reverse

from fiduciary.admin_cleanup import (
    delete_assignment,
    delete_client_if_orphan,
    delete_project,
    unlink_structural_group,
)
from fiduciary.imports.historical.data import CellData, HistoricalClient
from fiduciary.imports.historical.finalize import _FinalizationContext
from fiduciary.imports.historical.parser import HistoricalWorkbookParser
from fiduciary.imports.historical.readers import RawSheet
from fiduciary.models import Client, FiduciaryAssignment, FiduciaryAssignmentHolder, ImportBatch, ImportedFile, UnitOwnership
from fiduciary.services import create_imported_client, normalize_valid_imported_email
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup


pytestmark = pytest.mark.django_db


def _client(document="1", email="persona@gmail.com"):
    result = create_imported_client(
        full_name="PEREZ PERSONA",
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number=document,
        phone="3001234567",
        email=email,
    )
    assert result.client
    return result.client


def _project_context(code="P1"):
    project = Project.objects.create(code=code, name=f"Proyecto {code}")
    grouping_type = GroupingType.objects.create(code=f"T{code}", name=f"Torre {code}")
    group = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="A", name="Agrupacion A")
    unit = PropertyUnit.objects.create(project=project, structural_group=group, code="101", name="101")
    return project, grouping_type, group, unit


def _assignment(unit, client, number="EF-1"):
    UnitOwnership.objects.create(
        client=client,
        property_unit=unit,
        is_primary=True,
        start_date=date(2026, 1, 1),
        last_change_reason="Prueba",
    )
    assignment = FiduciaryAssignment.objects.create(
        assignment_number=number,
        property_unit=unit,
        start_date=date(2026, 1, 1),
        last_change_reason="Prueba",
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=client,
        is_primary=True,
        start_date=date(2026, 1, 1),
        last_change_reason="Prueba",
    )
    return assignment


def test_delete_project_removes_project_scope_but_preserves_clients_and_other_project(accounting_admin_user):
    project_a, _, _, unit_a = _project_context("PA")
    project_b, _, _, unit_b = _project_context("PB")
    client = _client()
    _assignment(unit_a, client, "EF-A")
    _assignment(unit_b, client, "EF-B")

    delete_project(project_a, user=accounting_admin_user, reason="Carga incorrecta")

    assert not Project.objects.filter(pk=project_a.pk).exists()
    assert Project.objects.filter(pk=project_b.pk).exists()
    assert PropertyUnit.objects.filter(pk=unit_b.pk).exists()
    assert Client.objects.filter(pk=client.pk).exists()
    assert FiduciaryAssignment.objects.filter(assignment_number="EF-B").exists()
    assert LogEntry.objects.filter(change_message__icontains="ELIMINAR_PROYECTO").exists()


def test_unlink_structural_group_preserves_grouping_type_and_other_projects(accounting_admin_user):
    project_a, grouping_type_a, group_a, unit_a = _project_context("GA")
    project_b = Project.objects.create(code="GB", name="Proyecto GB")
    group_b = StructuralGroup.objects.create(project=project_b, grouping_type=grouping_type_a, code="B", name="Agrupacion B")
    PropertyUnit.objects.create(project=project_b, structural_group=group_b, code="201", name="201")
    client = _client("2")
    _assignment(unit_a, client, "EF-GA")

    unlink_structural_group(group_a, user=accounting_admin_user, reason="Agrupacion erronea")

    assert GroupingType.objects.filter(pk=grouping_type_a.pk).exists()
    assert not StructuralGroup.objects.filter(pk=group_a.pk).exists()
    assert StructuralGroup.objects.filter(pk=group_b.pk).exists()
    assert Project.objects.filter(pk=project_a.pk).exists()
    assert Project.objects.filter(pk=project_b.pk).exists()


def test_delete_assignment_removes_assignment_scope_but_preserves_client(accounting_admin_user):
    _, _, _, unit = _project_context("EA")
    client = _client("3")
    assignment = _assignment(unit, client, "EF-DEL")

    delete_assignment(assignment, user=accounting_admin_user, reason="Encargo duplicado")

    assert not FiduciaryAssignment.objects.filter(pk=assignment.pk).exists()
    assert Client.objects.filter(pk=client.pk).exists()
    assert LogEntry.objects.filter(change_message__icontains="ELIMINAR_ENCARGO").exists()


def test_delete_client_blocks_when_dependencies_exist_and_deletes_orphan(accounting_admin_user):
    _, _, _, unit = _project_context("CA")
    related = _client("4")
    _assignment(unit, related, "EF-CLIENT")

    related_summary = delete_client_if_orphan(related, user=accounting_admin_user, reason="No debe borrar")
    assert related_summary.assignment_holders == 1
    assert Client.objects.filter(pk=related.pk).exists()

    orphan = _client("5", "orphan@example.com")
    delete_client_if_orphan(orphan, user=accounting_admin_user, reason="Registro errado")
    assert not Client.objects.filter(pk=orphan.pk).exists()
    assert LogEntry.objects.filter(change_message__icontains="ELIMINAR_CLIENTE").exists()


def test_accounting_ui_shows_administrative_delete_actions(client, accounting_admin_user):
    project, _, group, unit = _project_context("UI")
    fiduciary_client = _client("UI-1")
    assignment = _assignment(unit, fiduciary_client, "EF-UI")
    client.force_login(accounting_admin_user)

    project_list = client.get(reverse("real_estate:project_list")).content.decode()
    group_list = client.get(reverse("real_estate:structural_group_list")).content.decode()
    client_detail = client.get(reverse("fiduciary:client_detail", args=[fiduciary_client.pk])).content.decode()
    assignment_detail = client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk])).content.decode()

    assert reverse("real_estate:project_delete", args=[project.pk]) in project_list
    assert "Eliminar proyecto" in project_list
    assert reverse("real_estate:structural_group_delete", args=[group.pk]) in group_list
    assert "Desvincular" in group_list
    assert reverse("fiduciary:client_delete", args=[fiduciary_client.pk]) in client_detail
    assert "Eliminar cliente" in client_detail
    assert reverse("fiduciary:assignment_delete", args=[assignment.pk]) not in assignment_detail
    assert "Eliminar encargo" not in assignment_detail


@pytest.mark.parametrize(
    ("raw_email", "expected"),
    [
        ("persona@gmail.com", "persona@gmail.com"),
        ("persona@constructoracentenario.com", "persona@constructoracentenario.com"),
        ("persona@empresa.com.co", "persona@empresa.com.co"),
        ("PERSONA@EMPRESA.COM", "persona@empresa.com"),
        (" Persona@Empresa.com ", "persona@empresa.com"),
        ("mailto:persona@empresa.com", "persona@empresa.com"),
    ],
)
def test_historical_import_accepts_valid_email_domains_and_normalizes(raw_email, expected):
    client = _client(document=f"D{abs(hash(raw_email))}", email=raw_email)
    assert client.email == expected


@pytest.mark.parametrize("raw_email", ["persona", "persona@", "@empresa.com", "persona empresa@empresa.com"])
def test_historical_import_rejects_really_invalid_email(raw_email):
    client = _client(document=f"I{abs(hash(raw_email))}", email=raw_email)
    assert client.email == ""


def test_parser_normalizes_visible_email_value_without_requiring_hyperlink():
    sheet = RawSheet(
        "T1",
        1,
        "visible",
        "A1:E5",
        {
            (1, 1): CellData(1, 1, "A", "A1", "Proyecto Email - T1"),
            (4, 1): CellData(4, 1, "A", "A4", "APTO"),
            (4, 2): CellData(4, 2, "B", "B4", "CEDULA CLIENTE"),
            (4, 3): CellData(4, 3, "C", "C4", "NOMBRE CLIENTE"),
            (4, 4): CellData(4, 4, "D", "D4", "TELEFONO"),
            (4, 5): CellData(4, 5, "E", "E4", "E-MAIL"),
            (4, 6): CellData(4, 6, "F", "F4", "ENCARGO FIDUCIARIO"),
            (5, 1): CellData(5, 1, "A", "A5", "101"),
            (5, 2): CellData(5, 2, "B", "B5", "123"),
            (5, 3): CellData(5, 3, "C", "C5", "PEREZ PERSONA"),
            (5, 4): CellData(5, 4, "D", "D5", "3001234567"),
            (5, 5): CellData(5, 5, "E", "E5", "mailto:Persona@Empresa.com"),
            (5, 6): CellData(5, 6, "F", "F5", "EF-EMAIL"),
        },
        set(),
        set(),
    )

    parsed = HistoricalWorkbookParser(Path("LIBRO Email.xlsx"))._parse_sheet(sheet)

    assert parsed.rows[0].clients[0].email == "persona@empresa.com"


@pytest.mark.parametrize(
    ("raw_email", "expected"),
    [
        ("sleyton_644@unihumboldt.edu.co", "sleyton_644@unihumboldt.edu.co"),
        ("SLEYTON_644@UNIHUMBOLDT.EDU.CO", "sleyton_644@unihumboldt.edu.co"),
        ("mailto:sleyton_644@unihumboldt.edu.co", "sleyton_644@unihumboldt.edu.co"),
    ],
)
def test_institutional_email_normalization_used_by_historical_updates(raw_email, expected, accounting_admin_user):
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="historico.xlsx",
        extension=".xlsx",
        size_bytes=1,
        sha256=f"{abs(hash(raw_email)):064x}"[-64:],
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number=f"U{abs(hash(raw_email))}",
        first_names="SANTIAGO",
        last_names_or_company="LEYTON",
        phone="3001234567",
        source_origin=Client.SourceOrigin.HISTORICAL_IMPORT,
    )
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)

    context._update_cached_client_contact(
        client,
        HistoricalClient(order=1, name="LEYTON SANTIAGO", email=raw_email),
    )

    client.refresh_from_db()
    assert client.email == expected


def test_invalid_historical_email_is_not_allowed_to_raise_model_validation(accounting_admin_user):
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="historico.xlsx",
        extension=".xlsx",
        size_bytes=1,
        sha256="e" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="INVALID-EMAIL",
        first_names="CLIENTE",
        last_names_or_company="INVALIDO",
        phone="3001234567",
        source_origin=Client.SourceOrigin.HISTORICAL_IMPORT,
    )
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)

    context._update_cached_client_contact(
        client,
        HistoricalClient(order=1, name="CLIENTE INVALIDO", email="persona empresa@empresa.com"),
    )

    client.refresh_from_db()
    assert client.email == ""
    assert normalize_valid_imported_email("persona empresa@empresa.com") == ""
