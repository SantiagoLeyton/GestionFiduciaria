from datetime import date, datetime
from decimal import Decimal
from io import BytesIO, StringIO
from types import SimpleNamespace
from xml.etree import ElementTree as ET
import zipfile

import pytest
from django.contrib.admin.models import LogEntry
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from fiduciary.exporters import export_historical_workbook
from fiduciary.imports.historical import HistoricalWorkbookParser
from fiduciary.imports.historical.readers import WorkbookReader
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
from fiduciary.forms import (
    AssignmentFilterForm,
    ClientFilterForm,
    DIRECT_UNITS_VALUE,
    FiduciaryAssignmentForm,
    ImportResolutionForm,
    NoveltyFilterForm,
    ObservationFilterForm,
    ObservationForm,
    OperationalNoveltyForm,
    PaymentFilterForm,
    UnitOwnershipForm,
)
from fiduciary.models import DetectedStructureElement, ImportResolution
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


def excel_column_name(index):
    name = ""
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def exported_row_for_unit(project, tmp_path, sheet_name, unit_code):
    exported = export_historical_workbook(project)
    export_path = tmp_path / f"export-{sheet_name}-{unit_code}.xlsx"
    export_path.write_bytes(exported.content)
    workbook = WorkbookReader().read(export_path)
    sheet = next(sheet for sheet in workbook.sheets if sheet.name == sheet_name)
    headers = [sheet.cell(4, index).value for index in range(1, sheet.used_columns + 1)]
    header_col = {header: index + 1 for index, header in enumerate(headers) if header}
    for row_index in range(5, sheet.used_rows + 1):
        if sheet.cell(row_index, header_col["APTO "]).value == unit_code:
            return {header: sheet.cell(row_index, index + 1).value for index, header in enumerate(headers) if header}
    raise AssertionError(f"No se encontro la unidad {unit_code} en la hoja {sheet_name}.")


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


def new_assignment_post_data(unit, primary_client, number="EF-FORM", secondary_clients=None, dates=True, confirm=False):
    secondary_clients = secondary_clients or []
    data = {
        "project": unit.project_id,
        "grouping_type": unit.structural_group.grouping_type_id if unit.structural_group_id else "",
        "structural_group": unit.structural_group_id if unit.structural_group_id else "",
        "property_unit": unit.pk,
        "assignment_number": number,
        "primary_client_id": primary_client.pk if primary_client else "",
        "secondary_client_ids": ",".join(str(client.pk) for client in secondary_clients),
    }
    if dates:
        data.update(
            {
                "adhesion_contract_date": "2026-01-10",
                "promise_date": "2026-02-10",
                "promised_delivery_date": "2026-03-10",
                "actual_delivery_date": "2026-04-10",
            }
        )
    if confirm:
        data["confirm_without_dates"] = "true"
    return data


def xlsx_text(content):
    with zipfile.ZipFile(BytesIO(content)) as archive:
        return "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".xml"))


def xlsx_cell_styles(content, sheet_index=1):
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(BytesIO(content)) as archive:
        styles_root = ET.fromstring(archive.read("xl/styles.xml"))
        fills = []
        for fill in styles_root.findall("main:fills/main:fill", ns):
            fg = fill.find("main:patternFill/main:fgColor", ns)
            fills.append(fg.attrib.get("rgb") if fg is not None else None)
        style_fills = []
        for style in styles_root.findall("main:cellXfs/main:xf", ns):
            style_fills.append(fills[int(style.attrib.get("fillId", "0"))])
        sheet_root = ET.fromstring(archive.read(f"xl/worksheets/sheet{sheet_index}.xml"))
        return {
            cell.attrib["r"]: style_fills[int(cell.attrib.get("s", "0"))]
            for cell in sheet_root.findall(".//main:c", ns)
        }


def xlsx_cell_formats(content, sheet_index=1):
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(BytesIO(content)) as archive:
        styles_root = ET.fromstring(archive.read("xl/styles.xml"))
        fills = []
        for fill in styles_root.findall("main:fills/main:fill", ns):
            fg = fill.find("main:patternFill/main:fgColor", ns)
            fills.append(fg.attrib.get("rgb") if fg is not None else None)
        borders = []
        for border in styles_root.findall("main:borders/main:border", ns):
            borders.append(
                tuple(
                    side.attrib.get("style")
                    for side in (
                        border.find("main:left", ns),
                        border.find("main:right", ns),
                        border.find("main:top", ns),
                        border.find("main:bottom", ns),
                    )
                )
            )
        style_formats = []
        for style in styles_root.findall("main:cellXfs/main:xf", ns):
            style_formats.append(
                {
                    "fill": fills[int(style.attrib.get("fillId", "0"))],
                    "border": borders[int(style.attrib.get("borderId", "0"))],
                }
            )
        sheet_root = ET.fromstring(archive.read(f"xl/worksheets/sheet{sheet_index}.xml"))
        cells = {
            cell.attrib["r"]: style_formats[int(cell.attrib.get("s", "0"))]
            for cell in sheet_root.findall(".//main:c", ns)
        }
        widths = {
            int(col.attrib["min"]): float(col.attrib["width"])
            for col in sheet_root.findall("main:cols/main:col", ns)
        }
        heights = {
            int(row.attrib["r"]): float(row.attrib["ht"])
            for row in sheet_root.findall("main:sheetData/main:row", ns)
            if "ht" in row.attrib
        }
        return {"cells": cells, "widths": widths, "heights": heights}


def xlsx_cell_types(content, sheet_index=1):
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(BytesIO(content)) as archive:
        sheet_root = ET.fromstring(archive.read(f"xl/worksheets/sheet{sheet_index}.xml"))
        result = {}
        for cell in sheet_root.findall(".//main:c", ns):
            result[cell.attrib["r"]] = {
                "type": cell.attrib.get("t"),
                "formula": cell.find("main:f", ns) is not None,
                "value": cell.findtext("main:v", default="", namespaces=ns),
            }
        return result


@pytest.mark.django_db
def test_historical_finalization_persists_unit_historical_values_and_detail_only(
    accounting_client, accounting_admin_user, project
):
    from fiduciary.imports.historical.data import HistoricalRow
    from fiduciary.imports.historical.finalize import _FinalizationContext

    _, imported_file = create_imported_file(accounting_admin_user)
    unit = PropertyUnit.objects.create(project=project, code="501", name="501")
    context = _FinalizationContext(batch=imported_file.batch, imported_file=imported_file, user=accounting_admin_user)
    context._remember_unit_context(unit, "501", "T1")
    row = HistoricalRow(
        sheet_name="T1",
        row_number=5,
        project=project.name,
        grouping_type=None,
        grouping_code="T1",
        grouping_name="T1",
        unit_code="501",
        unit_name="501",
        area=Decimal("65.50"),
        property_value=Decimal("192172500"),
        financial_entity="BBVA",
    )

    assert context._unit_for_row(row) == unit
    unit.refresh_from_db()
    history_response = accounting_client.get(reverse("real_estate:property_unit_history", args=[unit.pk]))
    list_response = accounting_client.get(reverse("real_estate:property_unit_list"))

    assert unit.area == Decimal("65.50")
    assert unit.property_value == Decimal("192172500")
    assert unit.financial_entity == "BBVA"
    history_content = history_response.content.decode()
    assert "65,50 m2" in history_content
    assert "192.172.500" in history_content
    assert "BBVA" not in history_content
    list_content = list_response.content.decode()
    assert "<th>Area</th>" not in list_content
    assert "<th>Valor inmueble</th>" not in list_content
    assert "<th>Entidad financiera</th>" not in list_content


@pytest.mark.django_db
def test_backfill_unit_areas_updates_only_safe_null_unit_values(
    monkeypatch, tmp_path, accounting_admin_user, project, grouping_type
):
    media_root = tmp_path / "media"
    media_root.mkdir()
    t1 = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    target = PropertyUnit.objects.create(project=project, structural_group=t1, code="101", name="101")
    existing = PropertyUnit.objects.create(
        project=project,
        structural_group=t1,
        code="102",
        name="102",
        area=Decimal("70.00"),
        property_value=Decimal("111000000"),
        financial_entity="EXISTENTE",
    )
    conflict = PropertyUnit.objects.create(project=project, structural_group=t1, code="103", name="103")

    stored_a = media_root / "a.xlsx"
    stored_b = media_root / "b.xlsx"
    stored_a.write_bytes(b"a")
    stored_b.write_bytes(b"b")
    def make_file(name, sha_char):
        batch = ImportBatch.objects.create(
            initiated_by=accounting_admin_user,
            imported_by=accounting_admin_user,
            import_type=ImportBatch.ImportType.HISTORICAL,
            load_mode=ImportBatch.LoadMode.SINGLE_FILE,
            status=ImportBatch.Status.COMPLETED,
            total_files=1,
            processed_files=1,
        )
        return ImportedFile.objects.create(
            batch=batch,
            original_name=name,
            extension=".xlsx",
            size_bytes=128,
            sha256=sha_char * 64,
            file_type=ImportedFile.FileType.HISTORICAL,
            status=ImportedFile.Status.COMPLETED,
            stored_path=name,
        )

    make_file("a.xlsx", "d")
    make_file("b.xlsx", "e")
    make_file("missing.xlsx", "f")

    def row(unit_code, area, property_value="192172500", financial_entity="BBVA"):
        return SimpleNamespace(
            project=project.name,
            grouping_code="T1",
            grouping_name="T1",
            sheet_name="T1",
            unit_code=unit_code,
            unit_name=unit_code,
            area=Decimal(area),
            property_value=Decimal(property_value),
            financial_entity=financial_entity,
            row_number=5,
        )

    class FakeParser:
        def __init__(self, path):
            self.path = path

        def parse(self):
            rows_by_name = {
                "a.xlsx": [row("101", "65.50"), row("102", "66.00"), row("999", "80.00"), row("103", "55.00")],
                "b.xlsx": [row("103", "56.00", "193000000", "BANCO CONFLICTO")],
            }
            return SimpleNamespace(sheets=[SimpleNamespace(name="T1", rows=rows_by_name[self.path.name])])

    dry_out = StringIO()
    with override_settings(MEDIA_ROOT=media_root):
        monkeypatch.setattr("fiduciary.management.commands.backfill_unit_areas.HistoricalWorkbookParser", FakeParser)
        call_command("backfill_unit_areas", "--dry-run", stdout=dry_out)

    target.refresh_from_db()
    assert target.area is None
    assert target.property_value is None
    assert target.financial_entity is None
    assert "area_actualizables=1" in dry_out.getvalue()

    out = StringIO()
    with override_settings(MEDIA_ROOT=media_root):
        call_command("backfill_unit_areas", stdout=out)

    target.refresh_from_db()
    existing.refresh_from_db()
    conflict.refresh_from_db()
    output = out.getvalue()

    assert target.area == Decimal("65.50")
    assert target.property_value == Decimal("192172500")
    assert target.financial_entity == "BBVA"
    assert existing.area == Decimal("70.00")
    assert existing.property_value == Decimal("111000000")
    assert existing.financial_entity == "EXISTENTE"
    assert conflict.area is None
    assert conflict.property_value is None
    assert conflict.financial_entity is None
    assert not PropertyUnit.objects.filter(code="999").exists()
    assert "Archivo no disponible: missing.xlsx" in output
    assert "Conflicto AREA" in output
    assert "Conflicto VALOR INMUEBLE" in output
    assert "Conflicto ENTIDAD FINANCIERA" in output
    assert "unidades_no_resueltas=1" in output
    assert "area_actualizadas=1" in output
    assert "valor_inmueble_actualizadas=1" in output
    assert "entidad_financiera_actualizadas=1" in output


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
def test_manual_client_form_requires_first_and_last_name():
    from fiduciary.forms import ClientForm

    form = ClientForm(
        data={
            "document_type": FiduciaryClient.DocumentType.CITIZENSHIP_ID,
            "document_number": "783",
            "first_names": " ",
            "last_names_or_company": "",
            "phone": "300123",
            "email": "",
            "address": "",
            "is_active": "on",
        }
    )

    assert not form.is_valid()
    assert "Registre el nombre del cliente" in str(form.errors)
    assert "Registre el apellido del cliente" in str(form.errors)


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
def test_client_search_endpoint_finds_full_display_name_partial_email_and_document(accounting_client):
    client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="777",
        first_names="MARIA JOSE",
        last_names_or_company="LEYTON GOMEZ",
        email="Maria.Leyton@Example.com",
        phone="300",
    )

    exact_name = accounting_client.get(
        reverse("fiduciary:client_search"),
        {"criterion": "name", "q": "MARIA JOSE LEYTON GOMEZ"},
    ).json()["results"]
    partial_name = accounting_client.get(
        reverse("fiduciary:client_search"),
        {"criterion": "name", "q": "jose leyton"},
    ).json()["results"]
    email = accounting_client.get(
        reverse("fiduciary:client_search"),
        {"criterion": "email", "q": "maria.leyton@example.com"},
    ).json()["results"]
    document = accounting_client.get(
        reverse("fiduciary:client_search"),
        {"criterion": "document", "q": "777"},
    ).json()["results"]

    assert exact_name[0]["id"] == client.pk
    assert partial_name[0]["id"] == client.pk
    assert email[0]["id"] == client.pk
    assert document[0]["id"] == client.pk


@pytest.mark.django_db
def test_client_search_endpoint_includes_inactive_historical_clients(accounting_client):
    client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="75686638",
        first_names="MARIA JOSE",
        last_names_or_company="LEYTON GOMEZ",
        email="majogol@msn.com",
        phone="300",
        is_active=False,
    )

    for criterion, query in (
        ("document", "75686638"),
        ("name", "MARIA JOSE LEYTON GOMEZ"),
        ("email", "majogol@msn.com"),
    ):
        response = accounting_client.get(
            reverse("fiduciary:client_search"),
            {"criterion": criterion, "q": query},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        payload = response.json()["results"]
        assert response.status_code == 200
        assert payload[0]["id"] == client.pk
        assert payload[0]["status"] == "Inactivo"


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
def test_retired_ownership_finalize_route_does_not_mutate(accounting_client, active_client, secondary_client, unit):
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
    assert response.url == reverse("fiduciary:assignment_list")
    assert ownership.is_active is True
    assert UnitOwnership.objects.filter(pk=ownership.pk).exists()


@pytest.mark.django_db
def test_retired_primary_ownership_change_route_does_not_mutate(accounting_client, active_client, secondary_client, unit):
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
    assert response.url == reverse("fiduciary:assignment_list")
    assert current.is_active is True
    assert UnitOwnership.objects.filter(property_unit=unit, is_primary=True, is_active=True).get().client == active_client
    assert UnitOwnership.objects.filter(property_unit=unit).count() == 1


@pytest.mark.django_db
def test_retired_ownership_create_route_redirects_to_new_assignment_without_creating(accounting_client, active_client, secondary_client, unit):
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
    assert response.url == reverse("fiduciary:assignment_create")
    assignment.refresh_from_db()
    assert assignment.is_active is True
    assert assignment.holders.filter(client=active_client, is_active=True).exists()
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-SYNC-NEW").exists()
    assert UnitOwnership.objects.get(client=active_client, property_unit=unit).is_active is True
    assert not UnitOwnership.objects.filter(client=secondary_client, property_unit=unit).exists()


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
def test_assignment_creation_screen_is_available_from_assignment_module(accounting_client):
    response = accounting_client.get(reverse("fiduciary:assignment_create"))

    assert response.status_code == 200
    assert b"Nuevo encargo fiduciario" in response.content
    list_response = accounting_client.get(reverse("fiduciary:assignment_list"))
    assert "Nuevo Encargo" in list_response.content.decode()
    assert "NUEVO ENCARGO" not in list_response.content.decode()


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
def test_assignment_context_units_support_direct_and_grouped_units(accounting_client, project, grouping_type, active_client):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    direct_unit = PropertyUnit.objects.create(project=project, code="D1", name="Directa")
    grouped_unit = PropertyUnit.objects.create(project=project, structural_group=tower, code="A101", name="Agrupada")
    create_ownership(active_client, grouped_unit, True)
    create_assignment(grouped_unit, active_client, "EF-BUSY")

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
    busy_row = grouped_response.json()["results"][0]
    assert busy_row["disabled"] is True
    assert "no disponible" in busy_row["text"]


@pytest.mark.django_db
def test_unit_selects_and_historical_export_use_natural_unit_order(accounting_client, project, grouping_type, tmp_path):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    expected = ["101", "102", "108", "201", "901", "1001", "1002", "1101"]
    for code in ["1001", "1002", "101", "102", "108", "201", "901", "1101"]:
        PropertyUnit.objects.create(project=project, structural_group=tower, code=code, name=code)

    filter_response = accounting_client.get(
        reverse("fiduciary:unit_context_units"),
        {"project": project.pk, "structural_group": tower.pk},
    )
    assignment_response = accounting_client.get(
        reverse("fiduciary:assignment_context_units"),
        {"project": project.pk, "structural_group": tower.pk},
    )
    exported = export_historical_workbook(project)
    export_path = tmp_path / "natural-order.xlsx"
    export_path.write_bytes(exported.content)
    workbook = WorkbookReader().read(export_path)
    sheet = next(sheet for sheet in workbook.sheets if sheet.name == "T1")
    headers = [sheet.cell(4, index).value for index in range(1, sheet.used_columns + 1)]
    apto_col = headers.index("APTO ") + 1
    exported_units = [sheet.cell(row_index, apto_col).value for row_index in range(5, 5 + len(expected))]

    filter_units = [row["text"].split(" - ")[-1].replace(" (no disponible)", "") for row in filter_response.json()["results"]]
    assignment_units = [
        row["text"].split(" - ")[-1].replace(" (no disponible)", "")
        for row in assignment_response.json()["results"]
    ]

    assert filter_response.status_code == 200
    assert assignment_response.status_code == 200
    assert filter_units == expected
    assert assignment_units == expected
    assert exported_units == expected


@pytest.mark.django_db
def test_assignment_context_and_post_allow_unit_with_assignment_without_active_primary(
    accounting_client, active_client, project, grouping_type
):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=tower, code="103", name="103")
    FiduciaryAssignment.objects.create(
        assignment_number="60007919073811",
        property_unit=unit,
        start_date="2025-01-01",
        last_change_reason="Encargo sin titular vigente",
    )

    units_response = accounting_client.get(
        reverse("fiduciary:assignment_context_units"),
        {"project": project.pk, "structural_group": tower.pk},
    )
    payload = units_response.json()["results"]
    assert payload == [{"id": unit.pk, "text": "T1 - 103", "available": True, "disabled": False}]

    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        new_assignment_post_data(unit, active_client, "EF-UNIT-103"),
    )

    assert response.status_code == 302
    assert FiduciaryAssignment.objects.get(assignment_number="60007919073811").is_active is False
    assert FiduciaryAssignment.objects.filter(assignment_number="EF-UNIT-103", property_unit=unit, is_active=True).exists()


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
def test_assignment_create_creates_assignment_holders_and_dates(accounting_client, active_client, secondary_client, project, grouping_type):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=tower, code="101", name="101")
    third_client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="789",
        first_names="Diana",
        last_names_or_company="Mora",
        phone="301",
    )

    response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        new_assignment_post_data(unit, active_client, "EF-NEW", [secondary_client, third_client]),
    )

    assignment = FiduciaryAssignment.objects.get(assignment_number="EF-NEW")
    assert response.status_code == 302
    assert response.url == reverse("fiduciary:assignment_detail", args=[assignment.pk])
    assert assignment.property_unit == unit
    assert assignment.adhesion_contract_date == date(2026, 1, 10)
    assert assignment.promise_date == date(2026, 2, 10)
    assert assignment.holders.filter(client=active_client, is_primary=True, is_active=True).exists()
    assert assignment.holders.filter(client=secondary_client, is_primary=False, is_active=True).exists()
    assert assignment.holders.filter(client=third_client, is_primary=False, is_active=True).exists()
    assert UnitOwnership.objects.filter(client=active_client, property_unit=unit, is_primary=True, is_active=True).exists()
    assert UnitOwnership.objects.filter(client=secondary_client, property_unit=unit, is_primary=False, is_active=True).exists()
    assert UnitOwnership.objects.filter(client=third_client, property_unit=unit, is_primary=False, is_active=True).exists()
    assert not OperationalNovelty.objects.filter(other_type="INCLUSION", new_assignment=assignment).exists()
    assert OperationalNovelty.objects.count() == 0


@pytest.mark.django_db
def test_new_assignment_real_search_selection_posts_selected_clients(accounting_client, project, grouping_type):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=tower, code="106", name="106")
    primary = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="75686638",
        first_names="MARIA JOSE",
        last_names_or_company="LEYTON GOMEZ",
        email="majogol@msn.com",
        phone="300",
        is_active=False,
    )
    secondary_a = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="801",
        first_names="LUCAS",
        last_names_or_company="TENTACLES",
        email="lucas@example.com",
        phone="301",
        is_active=False,
    )
    secondary_b = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="802",
        first_names="CLEVELAND",
        last_names_or_company="WATSON",
        email="cleveland@example.com",
        phone="302",
    )

    get_response = accounting_client.get(reverse("fiduciary:assignment_create"))
    search_response = accounting_client.get(
        reverse("fiduciary:client_search"),
        {"criterion": "name", "q": "MARIA JOSE LEYTON GOMEZ"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    secondary_response = accounting_client.get(
        reverse("fiduciary:client_search"),
        {"criterion": "email", "q": "lucas@example.com"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    post_response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        new_assignment_post_data(unit, primary, "EF-SEARCH-SELECT", [secondary_a, secondary_b]),
    )

    assignment = FiduciaryAssignment.objects.get(assignment_number="EF-SEARCH-SELECT")
    primary.refresh_from_db()
    secondary_a.refresh_from_db()
    assert get_response.status_code == 200
    assert 'data-client-picker data-target="id_primary_client_id"' in get_response.content.decode()
    assert search_response.json()["results"][0]["id"] == primary.pk
    assert secondary_response.json()["results"][0]["id"] == secondary_a.pk
    assert post_response.status_code == 302
    assert primary.is_active is True
    assert secondary_a.is_active is True
    assert assignment.holders.get(is_primary=True).client_id == primary.pk
    assert set(assignment.holders.filter(is_primary=False).values_list("client_id", flat=True)) == {
        secondary_a.pk,
        secondary_b.pk,
    }


@pytest.mark.django_db
def test_assignment_create_requires_confirmation_without_dates(accounting_client, active_client, project, grouping_type):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=tower, code="102", name="102")

    first_response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        new_assignment_post_data(unit, active_client, "EF-NODATES", dates=False),
    )
    assert first_response.status_code == 200
    assert "No has colocado ninguna fecha" in first_response.content.decode()
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-NODATES").exists()

    second_response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        new_assignment_post_data(unit, active_client, "EF-NODATES", dates=False, confirm=True),
    )
    assert second_response.status_code == 302
    assert FiduciaryAssignment.objects.filter(assignment_number="EF-NODATES").exists()


@pytest.mark.django_db
def test_assignment_create_rejects_unavailable_unit_and_duplicate_number(accounting_client, active_client, secondary_client, project, grouping_type):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=tower, code="103", name="103")
    create_ownership(active_client, unit, True)
    create_assignment(unit, active_client, "EF-EXISTS")

    unavailable_response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        new_assignment_post_data(unit, secondary_client, "EF-OTHER"),
    )
    duplicate_response = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        new_assignment_post_data(PropertyUnit.objects.create(project=project, structural_group=tower, code="104", name="104"), secondary_client, "EF-EXISTS"),
    )

    assert unavailable_response.status_code == 200
    assert "no esta disponible" in unavailable_response.content.decode()
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-OTHER").exists()
    assert duplicate_response.status_code == 200
    assert "Ya existe un encargo fiduciario" in duplicate_response.content.decode()


@pytest.mark.django_db
def test_assignment_create_validates_secondary_duplicates(accounting_client, active_client, secondary_client, project, grouping_type):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=tower, code="105", name="105")

    same_as_primary = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        new_assignment_post_data(unit, active_client, "EF-PRIMARY-DUP", [active_client]),
    )
    duplicate_secondary = accounting_client.post(
        reverse("fiduciary:assignment_create"),
        new_assignment_post_data(unit, active_client, "EF-SECONDARY-DUP", [secondary_client, secondary_client]),
    )

    assert same_as_primary.status_code == 200
    assert "principal no puede repetirse" in same_as_primary.content.decode()
    assert duplicate_secondary.status_code == 200
    assert "mismo cliente secundario" in duplicate_secondary.content.decode()


@pytest.mark.django_db
def test_assignment_add_secondary_creates_inclusion(accounting_client, active_client, secondary_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-ADD-SECONDARY")

    response = accounting_client.post(
        reverse("fiduciary:assignment_secondary_create", args=[assignment.pk]),
        {"client_id": secondary_client.pk},
    )

    assert response.status_code == 302
    assert FiduciaryAssignmentHolder.objects.filter(
        assignment=assignment,
        client=secondary_client,
        is_primary=False,
        is_active=True,
    ).exists()
    assert UnitOwnership.objects.filter(
        property_unit=unit,
        client=secondary_client,
        is_primary=False,
        is_active=True,
    ).exists()
    novelty = OperationalNovelty.objects.get(new_client=secondary_client, new_assignment=assignment)
    assert novelty.novelty_type == OperationalNovelty.NoveltyType.OTHER
    assert novelty.other_type == "INCLUSION"
    assert "INCLUSION" in novelty.detail
    assert ImportedHistoricalObservation.objects.filter(
        property_unit=unit,
        assignment=assignment,
        client=secondary_client,
        summary="INCLUSION",
    ).exists()

    duplicate_response = accounting_client.post(
        reverse("fiduciary:assignment_secondary_create", args=[assignment.pk]),
        {"client_id": secondary_client.pk},
    )
    assert duplicate_response.status_code == 200
    assert assignment.holders.filter(client=secondary_client, is_active=True).count() == 1
    assert OperationalNovelty.objects.filter(new_client=secondary_client, new_assignment=assignment).count() == 1


@pytest.mark.django_db
def test_ownership_module_is_not_visible_in_navigation(accounting_client):
    home_content = accounting_client.get(reverse("home")).content.decode()
    assignment_content = accounting_client.get(reverse("fiduciary:assignment_list")).content.decode()

    assert "Titularidades" not in home_content
    assert "ownerships/" not in home_content
    assert "Titularidades" not in assignment_content
    assert reverse("fiduciary:ownership_list") not in assignment_content
    assert "Encargos fiduciarios" in assignment_content


@pytest.mark.django_db
def test_retired_holder_create_redirects_to_add_secondary(accounting_client, active_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-HOLDER-LEGACY")

    response = accounting_client.get(reverse("fiduciary:holder_create", args=[assignment.pk]))

    assert response.status_code == 302
    assert response.url == reverse("fiduciary:assignment_secondary_create", args=[assignment.pk])


@pytest.mark.django_db
def test_retired_ownership_create_does_not_create_assignment_with_primary_and_secondaries(
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
    assert response.url == reverse("fiduciary:assignment_create")
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-JOINT").exists()
    assert not UnitOwnership.objects.filter(client=active_client, property_unit=unit).exists()
    assert not UnitOwnership.objects.filter(client=secondary_client, property_unit=unit).exists()


@pytest.mark.django_db
def test_retired_ownership_create_redirects_without_partial_records(
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

    assert response.status_code == 302
    assert response.url == reverse("fiduciary:assignment_create")
    assert FiduciaryAssignment.objects.filter(assignment_number="EF-DUP-JOINT").count() == 1
    assert not UnitOwnership.objects.filter(client=secondary_client, property_unit=unit).exists()


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
            "primary_client_id": "999999",
            "confirm_without_dates": "true",
        },
    )

    assert response.status_code == 200
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
    ownership_response = commercial_client.get(reverse("fiduciary:ownership_create"))
    assert ownership_response.status_code == 302
    assert ownership_response.url == reverse("fiduciary:assignment_create")
    assert accounting_client.get(reverse("fiduciary:assignment_create")).status_code == 200
    assert commercial_client.get(reverse("fiduciary:assignment_create")).status_code == 200
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
def test_delete_endpoints_require_confirmation_and_reason(accounting_client, active_client, unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client)

    assert accounting_client.get(reverse("fiduciary:client_delete", args=[active_client.pk])).status_code == 200
    assert accounting_client.get(reverse("fiduciary:assignment_delete", args=[assignment.pk])).status_code == 403
    assert accounting_client.post(reverse("fiduciary:client_delete", args=[active_client.pk]), {"confirm": "yes"}).status_code == 302
    assert accounting_client.post(reverse("fiduciary:assignment_delete", args=[assignment.pk]), {"confirm": "yes"}).status_code == 403
    assert FiduciaryClient.objects.filter(pk=active_client.pk).exists()
    assert FiduciaryAssignment.objects.filter(pk=assignment.pk).exists()


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
def test_assignment_detail_blocks_free_update_and_delete(accounting_client, active_client, secondary_client, unit, second_unit):
    create_ownership(active_client, unit, True)
    create_ownership(secondary_client, second_unit, True)
    assignment = create_assignment(unit, active_client)
    other_assignment = create_assignment(second_unit, secondary_client, "EF-OTHER")
    assignment.adhesion_contract_date = date(2024, 1, 10)
    assignment.promise_date = date(2024, 2, 20)
    assignment.promised_delivery_date = date(2024, 3, 15)
    assignment.actual_delivery_date = date(2024, 4, 1)
    assignment.save()

    detail_response = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk]))
    content = detail_response.content.decode()

    assert detail_response.status_code == 200
    assert "Contrato de adhesion" in content
    assert "10/01/2024" in content
    assert "Promesa" in content
    assert "20/02/2024" in content
    assert "Entrega segun promesa" in content
    assert "15/03/2024" in content
    assert "Entrega real" in content
    assert "01/04/2024" in content
    assert "Informacion contractual" in content
    assert "Periodo" not in content
    data_section = content.split("<h2>Datos del encargo</h2>", 1)[1].split("<h2>Informacion contractual</h2>", 1)[0]
    assert "Cerrar</button>" not in data_section
    assert "Agregar titular" not in data_section
    assert "Registrar cambio de encargo" not in data_section
    assert "Registrar novedad" in content
    assert "Registrar observacion" in content
    assert reverse("fiduciary:assignment_update", args=[assignment.pk]) not in content
    assert reverse("fiduciary:assignment_delete", args=[assignment.pk]) not in content
    assert reverse("fiduciary:novelty_create") in content
    assert reverse("fiduciary:observation_create") in content
    assert accounting_client.get(reverse("fiduciary:novelty_create"), {"project": unit.project_id, "property_unit": unit.pk}).status_code == 200
    assert accounting_client.get(
        reverse("fiduciary:observation_create"),
        {"project": unit.project_id, "property_unit": unit.pk, "assignment": assignment.pk},
    ).status_code == 200

    form_response = accounting_client.get(reverse("fiduciary:assignment_update", args=[assignment.pk]))
    assert form_response.status_code == 403
    delete_get_response = accounting_client.get(reverse("fiduciary:assignment_delete", args=[assignment.pk]))
    delete_post_response = accounting_client.post(
        reverse("fiduciary:assignment_delete", args=[assignment.pk]),
        {"confirm": "yes", "change_reason": "Intento directo"},
    )
    assert delete_get_response.status_code == 403
    assert delete_post_response.status_code == 403

    update_response = accounting_client.post(
        reverse("fiduciary:assignment_update", args=[assignment.pk]),
        {
            "adhesion_contract_date": "2024-01-11",
            "promise_date": "2024-02-21",
            "promised_delivery_date": "",
            "actual_delivery_date": "2024-04-02",
            "change_reason": "Actualizacion de fechas operativas",
        },
    )

    assert update_response.status_code == 403
    assignment.refresh_from_db()
    other_assignment.refresh_from_db()
    assert assignment.adhesion_contract_date == date(2024, 1, 10)
    assert assignment.promise_date == date(2024, 2, 20)
    assert assignment.promised_delivery_date == date(2024, 3, 15)
    assert assignment.actual_delivery_date == date(2024, 4, 1)
    assert assignment.assignment_number == "EF-001"
    assert assignment.property_unit_id == unit.pk
    assert assignment.start_date == date(2026, 1, 1)
    assert other_assignment.adhesion_contract_date is None
    assert other_assignment.promise_date is None
    assert other_assignment.promised_delivery_date is None
    assert other_assignment.actual_delivery_date is None


@pytest.mark.django_db
def test_assignment_related_views_do_not_show_assignment_period(accounting_client, active_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client)

    assignment_list = accounting_client.get(reverse("fiduciary:assignment_list"), {"project": unit.project_id}).content.decode()
    assignment_detail = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk])).content.decode()
    client_detail = accounting_client.get(reverse("fiduciary:client_detail", args=[active_client.pk])).content.decode()
    unit_history = accounting_client.get(reverse("real_estate:property_unit_history", args=[unit.pk])).content.decode()

    assert "Periodo" not in assignment_list
    assert "Actualizar informacion contractual" in assignment_list
    assert "Periodo" not in assignment_detail
    assert "Informacion contractual" in assignment_detail
    assert "Contrato de adhesion" in assignment_detail
    assert "&mdash;" in assignment_detail
    assert "Periodo" not in client_detail
    assert assignment.assignment_number in client_detail
    assert "Periodo" not in unit_history


@pytest.mark.django_db
def test_unit_hierarchy_filters_do_not_render_units_until_group_is_selected(accounting_client, project, grouping_type):
    group_t1 = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    group_t2 = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T2", name="Torre 2")
    PropertyUnit.objects.create(project=project, structural_group=group_t1, code="101", name="101")
    PropertyUnit.objects.create(project=project, structural_group=group_t2, code="101", name="101")

    content = accounting_client.get(reverse("fiduciary:novelty_list"), {"project": project.pk}).content.decode()

    assert "T1 - 101" not in content
    assert "T2 - 101" not in content
    assert 'data-unit-hierarchy-field="property-unit" disabled' in content


@pytest.mark.django_db
def test_primary_unit_selectors_use_grouping_labels(accounting_admin_user, project, grouping_type, active_client):
    group_t1 = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    group_t2 = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T2", name="Torre 2")
    unit_t1 = PropertyUnit.objects.create(project=project, structural_group=group_t1, code="101", name="101")
    unit_t2 = PropertyUnit.objects.create(project=project, structural_group=group_t2, code="101", name="101")
    create_ownership(active_client, unit_t1)
    create_ownership(active_client, unit_t2)
    create_assignment(unit_t1, active_client, "EF-T1-101")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
        total_files=1,
    )
    detected = DetectedStructureElement.objects.create(
        batch=batch,
        raw_value="101",
        normalized_value="101",
        inferred_kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        confidence="0.8000",
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    ImportResolution.objects.create(detected_element=detected)

    forms = [
        ClientFilterForm({"project": project.pk, "grouping_type": grouping_type.pk, "structural_group": group_t1.pk}),
        AssignmentFilterForm({"project": project.pk, "grouping_type": grouping_type.pk, "structural_group": group_t1.pk}),
        ObservationFilterForm({"project": project.pk, "grouping_type": grouping_type.pk, "structural_group": group_t1.pk}),
        PaymentFilterForm({"project": project.pk, "grouping_type": grouping_type.pk, "structural_group": group_t1.pk}),
        NoveltyFilterForm({"project": project.pk, "grouping_type": grouping_type.pk, "structural_group": group_t1.pk}),
        OperationalNoveltyForm(
            user=accounting_admin_user,
            data={"project": project.pk, "grouping_type": grouping_type.pk, "structural_group": group_t1.pk},
        ),
        ObservationForm(
            user=accounting_admin_user,
            data={"project": project.pk, "grouping_type": grouping_type.pk, "structural_group": group_t1.pk},
        ),
        UnitOwnershipForm(
            initial={"client": active_client.pk, "property_unit": unit_t1.pk},
        ),
        FiduciaryAssignmentForm(data={"project": project.pk, "grouping_type": grouping_type.pk, "structural_group": group_t1.pk}),
        ImportResolutionForm(detected_element=detected),
    ]

    for form in forms:
        rendered = form.as_p()
        assert "T1 - 101" in rendered
        if not isinstance(form, ImportResolutionForm):
            assert "T2 - 101" not in rendered


@pytest.mark.django_db
def test_unit_hierarchy_context_filters_project_type_group_and_units(accounting_client, project, second_project):
    tower = GroupingType.objects.create(code="TOR", name="Torre")
    building = GroupingType.objects.create(code="ED", name="Edificio")
    a_t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    a_t2 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T2", name="Torre 2")
    a_e1 = StructuralGroup.objects.create(project=project, grouping_type=building, code="E1", name="Edificio 1")
    b_t1 = StructuralGroup.objects.create(project=second_project, grouping_type=tower, code="T1", name="Torre 1")
    PropertyUnit.objects.create(project=project, structural_group=a_t1, code="101", name="101")
    unit_a_t2 = PropertyUnit.objects.create(project=project, structural_group=a_t2, code="101", name="101")
    unit_a_e1 = PropertyUnit.objects.create(project=project, structural_group=a_e1, code="101", name="101")
    PropertyUnit.objects.create(project=second_project, structural_group=b_t1, code="101", name="101")

    types = accounting_client.get(reverse("fiduciary:unit_context_types"), {"project": project.pk}).json()["results"]
    assert {row["id"] for row in types} == {tower.pk, building.pk}

    tower_groups = accounting_client.get(
        reverse("fiduciary:unit_context_groups"),
        {"project": project.pk, "grouping_type": tower.pk},
    ).json()["results"]
    assert {row["id"] for row in tower_groups} == {a_t1.pk, a_t2.pk}
    assert a_e1.pk not in {row["id"] for row in tower_groups}
    assert b_t1.pk not in {row["id"] for row in tower_groups}

    t2_units = accounting_client.get(
        reverse("fiduciary:unit_context_units"),
        {"project": project.pk, "structural_group": a_t2.pk},
    ).json()["results"]
    assert t2_units == [{"id": unit_a_t2.pk, "text": "T2 - 101"}]

    e1_units = accounting_client.get(
        reverse("fiduciary:unit_context_units"),
        {"project": project.pk, "structural_group": a_e1.pk},
    ).json()["results"]
    assert e1_units == [{"id": unit_a_e1.pk, "text": "E1 - 101"}]


@pytest.mark.django_db
def test_import_batch_lists_do_not_show_visible_ids(accounting_client, accounting_admin_user):
    historical_batch, historical_file = create_imported_file(accounting_admin_user, ImportedFile.FileType.HISTORICAL)
    report_batch, report_file = create_imported_file(accounting_admin_user, ImportedFile.FileType.REPORT)

    historical_content = accounting_client.get(reverse("fiduciary:historical_import_list")).content.decode()
    report_content = accounting_client.get(reverse("fiduciary:daily_report_list")).content.decode()

    assert "<th>ID</th>" not in historical_content
    assert f"#{historical_batch.pk}" not in historical_content
    assert "<th>Fecha</th>" in historical_content
    assert reverse("fiduciary:historical_import_preview", args=[historical_batch.pk]) in historical_content
    assert historical_file.original_name in historical_content or "Previsualizar" in historical_content

    assert "<th>ID</th>" not in report_content
    assert f"#{report_batch.pk}" not in report_content
    assert "<th>Fecha</th>" in report_content
    assert reverse("fiduciary:daily_report_preview", args=[report_batch.pk]) in report_content
    assert report_file.original_name in report_content or "Previsualizar" in report_content


@pytest.mark.django_db
def test_export_historical_workbook_uses_database_history_receipts_separator_and_group_sheets(
    accounting_client,
    accounting_admin_user,
    project,
    tmp_path,
):
    tower = GroupingType.objects.create(code="T", name="Torre")
    building = GroupingType.objects.create(code="ED", name="Edificio")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    ed1 = StructuralGroup.objects.create(project=project, grouping_type=building, code="ED1", name="Edificio 1")
    unit_t1 = PropertyUnit.objects.create(
        project=project,
        structural_group=t1,
        code="101",
        name="101",
        area=Decimal("65.50"),
        property_value=Decimal("192172500"),
        financial_entity="BBVA",
    )
    unit_ed1 = PropertyUnit.objects.create(project=project, structural_group=ed1, code="201", name="201")
    primary = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="900",
        first_names="MARIA JOSE",
        last_names_or_company="LEYTON GOMEZ",
        phone="316",
        email="maria@example.com",
        address="SANTIAGO LEYTON CEL: 3009898588",
    )
    secondary = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="901",
        first_names="LUCAS",
        last_names_or_company="TENTACLES",
        phone="317",
    )
    create_ownership(primary, unit_t1, True)
    create_ownership(secondary, unit_t1, False)
    historical_assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-HIST",
        property_unit=unit_t1,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        is_active=False,
        last_change_reason="Historico",
        observations="Encargo historico reconstruido desde seccion NOVEDADES.",
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=historical_assignment,
        client=secondary,
        is_primary=True,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        is_active=False,
        last_change_reason="Historico",
    )
    assignment = create_assignment(unit_t1, primary, "EF-EXPORT")
    assignment.adhesion_contract_date = date(2025, 1, 2)
    assignment.promise_date = date(2025, 2, 3)
    assignment.promised_delivery_date = date(2025, 3, 4)
    assignment.actual_delivery_date = date(2025, 4, 5)
    assignment.save()
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=secondary,
        is_primary=False,
        start_date="2026-01-01",
        last_change_reason="Secundario",
    )
    _, imported_file = create_imported_file(accounting_admin_user)
    _, report_file = create_imported_file(accounting_admin_user, ImportedFile.FileType.REPORT)
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2025, 1, 14),
        amount=Decimal("600000.00"),
        concept="ORDINARIO | Recibo NCR000",
        destination=Payment.Destination.CONSTRUCTORA,
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=5,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2025, 1, 15),
        amount=Decimal("500000.00"),
        concept="ORDINARIO | Recibo NCR001",
        destination=Payment.Destination.FIDUCIARIA,
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=5,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2025, 1, 16),
        amount=Decimal("250000.00"),
        concept="ORDINARIO | Recibo NCR004",
        destination=Payment.Destination.FIDUCIARIA,
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=5,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2025, 1, 20),
        amount=Decimal("300000.00"),
        concept="CREDITO | Recibo NCR002",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=5,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2025, 2, 25),
        amount=Decimal("400000.00"),
        concept="SUBSIDIO | Recibo NCR003",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=5,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2026, 3, 10),
        amount=Decimal("700000.00"),
        concept="ABONO",
        destination=Payment.Destination.FIDUCIARIA,
        movement_type=Payment.MovementType.ADDITION,
        source_file=report_file,
        source_sheet="Reporte",
        source_row=2,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2026, 3, 11),
        amount=Decimal("800000.00"),
        concept="ABONO",
        destination=Payment.Destination.CONSTRUCTORA,
        movement_type=Payment.MovementType.ADDITION,
        source_file=report_file,
        source_sheet="Reporte",
        source_row=3,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2025, 1, 30),
        amount=Decimal("100000.00"),
        concept="AJUSTE",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=5,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2025, 1, 31),
        amount=Decimal("588000.00"),
        concept="AJUSTE",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=5,
    )
    OperationalNovelty.objects.create(
        property_unit=unit_t1,
        previous_client=secondary,
        new_client=primary,
        previous_assignment=historical_assignment,
        new_assignment=assignment,
        historical_assignment=assignment,
        novelty_type=OperationalNovelty.NoveltyType.CESSION,
        origin=OperationalNovelty.Origin.MANUAL,
        status=OperationalNovelty.Status.APPLIED,
        effective_date=date(2024, 6, 1),
        summary="*CESION/ARRAS",
        detail="NC1 JUN.1/24 CESION DE LUCAS TENTACLES A MARIA JOSE LEYTON | Creado desde importacion historica. | Encargo historico reconstruido desde seccion NOVEDADES.",
        created_by=accounting_admin_user,
    )
    ImportedHistoricalObservation.objects.create(
        batch=imported_file.batch,
        imported_file=imported_file,
        project=project,
        property_unit=unit_t1,
        client=secondary,
        assignment=historical_assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        summary="*CESION/ARRAS",
        detail="NC1 JUN.1/24 CESION DE LUCAS TENTACLES A MARIA JOSE LEYTON",
        historical_section="NOVEDADES",
        source_sheet="T1",
        source_row=20,
        source_order=1,
        dedupe_key="obs-duplicate-novelty",
        imported_by=accounting_admin_user,
    )
    OperationalNovelty.objects.create(
        property_unit=unit_t1,
        historical_assignment=assignment,
        novelty_type=OperationalNovelty.NoveltyType.OTHER,
        other_type="INCLUSION",
        origin=OperationalNovelty.Origin.MANUAL,
        status=OperationalNovelty.Status.APPLIED,
        effective_date=date(2026, 3, 11),
        summary="INCLUSION",
        detail="INCLUSION LUCAS TENTACLES",
        created_by=accounting_admin_user,
    )
    ImportedHistoricalObservation.objects.create(
        batch=imported_file.batch,
        imported_file=imported_file,
        project=project,
        property_unit=unit_t1,
        client=primary,
        assignment=assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        summary="Observacion comercial",
        detail="NC2 OBSERVACION REAL SIN NOVEDAD",
        historical_year=2025,
        historical_month=5,
        source_sheet="T1",
        source_row=21,
        source_order=2,
        dedupe_key="obs-without-novelty",
        imported_by=accounting_admin_user,
    )
    ImportedHistoricalObservation.objects.create(
        batch=imported_file.batch,
        imported_file=imported_file,
        project=project,
        property_unit=unit_t1,
        client=primary,
        assignment=assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        summary="Estado vigente",
        detail="OBSERVACION VIGENTE DEL ENCARGO",
        source_sheet="T1",
        source_row=22,
        source_order=3,
        dedupe_key="obs-current-assignment",
        imported_by=accounting_admin_user,
    )
    PropertyUnit.objects.create(project=project, structural_group=ed1, code="202", name="202")
    create_ownership(primary, unit_ed1, True)
    assignment_ed1 = create_assignment(unit_ed1, primary, "EF-ED1")
    Payment.objects.create(
        assignment=assignment_ed1,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2025, 2, 5),
        amount=Decimal("123000.00"),
        concept="ORDINARIO | Recibo NCR-ED1",
        destination=Payment.Destination.FIDUCIARIA,
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="ED1",
        source_row=5,
    )

    response = accounting_client.post(reverse("fiduciary:export_historical_workbook"), {"project": project.pk})
    content = xlsx_text(response.content)
    export_path = tmp_path / "exported.xlsx"
    export_path.write_bytes(response.content)
    workbook = WorkbookReader().read(export_path)
    parsed = HistoricalWorkbookParser(export_path).parse()
    t1_sheet = next(sheet for sheet in workbook.sheets if sheet.name == "T1")
    ed1_sheet = next(sheet for sheet in workbook.sheets if sheet.name == "ED1")
    headers = [t1_sheet.cell(4, index).value for index in range(1, t1_sheet.used_columns + 1)]
    row = {header: t1_sheet.cell(5, index + 1).value for index, header in enumerate(headers)}
    header_col = {header: index + 1 for index, header in enumerate(headers) if header}
    total_sales_row = 6
    unsold_row = 7
    total_row = 8
    novelty_title_row = 10
    novelty_cession_row = 11
    novelty_inclusion_row = 13
    novelty_cession = {
        header: t1_sheet.cell(novelty_cession_row, index + 1).value for index, header in enumerate(headers) if header
    }
    novelty_inclusion = {
        header: t1_sheet.cell(novelty_inclusion_row, index + 1).value for index, header in enumerate(headers) if header
    }
    cell_styles = xlsx_cell_styles(response.content, sheet_index=t1_sheet.index)
    cell_formats = xlsx_cell_formats(response.content, sheet_index=t1_sheet.index)

    assert response.status_code == 200
    assert response["Content-Type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert set(sheet.name for sheet in workbook.sheets) == {"T1", "ED1"}
    assert t1_sheet.cell(1, 1).value == f"CONJUNTO CERRADO {project.name} T1"
    assert headers[:5] == ["ENCARGO FIDUCIARIO", "NUEVOS ENCARGO FIDUCIARIO", "APTO ", "AREA", "CEDULA CLIENTE"]
    assert "VENDEDOR" not in headers
    assert headers[headers.index("APTO ") + 1] == "AREA"
    assert not any(header in (None, "") or (isinstance(header, str) and not header.strip()) for header in headers)
    assert "TIPO NOVEDAD" not in headers
    assert "VINC" not in headers
    assert "CESIONES/TRASLADOS" in headers
    assert "RECIBIDO CESION" not in headers
    assert "OBSERVACIONES" in headers
    assert "RECIBOS FIDUBOGOTA" in headers
    assert "RECIBIDO" in headers
    assert "RECIBO FIDUCIA ENE/2025" in headers
    assert "RECIBO FIDUCIA FEB/2025" not in headers
    assert "RECIBO FIDUCIA MAR/2026" not in headers
    assert not any(str(header).startswith("RECIBIDO FIDUBOGOTA ") for header in headers)
    assert not any(str(header).startswith("RECIBIDO ENE/") for header in headers)
    expected_payment_order = [
        "RECIBOS",
        "RECIBOS FIDUBOGOTA",
        "FECHA",
        "RECIBIDO",
        "CESIONES/TRASLADOS",
        "FECHA PAGO CREDITO",
        "FECHA PAGO SUBSIDIO",
        "RECIBO FIDUCIA ENE/2025",
    ]
    assert [header for header in headers if header in expected_payment_order] == expected_payment_order
    assert row["APTO "] == "101"
    assert row["AREA"] == Decimal("65.50")
    assert row["ENTIDAD FINANCIERA"] == "BBVA"
    assert row["VALOR INMUEBLE"] == Decimal("192172500")
    assert row["ENCARGO FIDUCIARIO"] == "EF-EXPORT"
    main_assignment_values = [
        t1_sheet.cell(row_index, header_col["ENCARGO FIDUCIARIO"]).value
        for row_index in range(5, novelty_title_row)
    ]
    assert "EF-HIST" not in main_assignment_values
    assert row["NOMBRE CLIENTE"] == "LEYTON GOMEZ MARIA JOSE"
    assert row["NOMBRE CLIENTE2"] == "TENTACLES LUCAS"
    assert row["CONTACTO"] == "SANTIAGO LEYTON CEL: 3009898588"
    assert "OBSERVACION VIGENTE DEL ENCARGO" in row["OBSERVACIONES"]
    assert "NC1 JUN.1/24 CESION DE LUCAS TENTACLES A MARIA JOSE LEYTON" in row["OBSERVACIONES"]
    assert row["FECHA CONTRATO DE ADHESION"] == "2025-01-02"
    assert row["FECHA"] == "ENE.14/25 - ENE.15/25F - ENE.16/25F || MAR.10/26F - MAR.11/26"
    assert row["FECHA PAGO CREDITO"] == "ENE.20/25"
    assert row["FECHA PAGO SUBSIDIO"] == "FEB.25/25"
    assert row["RECIBOS"] == "NCR000 - NCR002 - NCR003"
    assert row["RECIBOS FIDUBOGOTA"] == "NCR001 - NCR004"
    assert row["RECIBIDO"] == Decimal("600000")
    assert row["ABONOS CR CONSTRUCTOR"] == Decimal("300000")
    assert row["DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION"] == Decimal("400000")
    assert t1_sheet.cell(5, header_col["AJUSTE"]).formula == "100000+588000"
    assert t1_sheet.cell(5, header_col["RECIBO FIDUCIA ENE/2025"]).formula == "500000+250000"
    assert "||" not in str(row["RECIBIDO"])
    assert "||" not in str(t1_sheet.cell(5, header_col["RECIBO FIDUCIA ENE/2025"]).formula)
    total_formula = t1_sheet.cell(5, header_col["TOTAL RECIBIDO"]).formula
    assert f"{excel_column_name(header_col['RECIBIDO'])}5" in total_formula
    assert f"{excel_column_name(header_col['RECIBO FIDUCIA ENE/2025'])}5" in total_formula
    assert f"{excel_column_name(header_col['ABONOS CR CONSTRUCTOR'])}5" in total_formula
    assert f"{excel_column_name(header_col['DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION'])}5" in total_formula
    assert f"{excel_column_name(header_col['AJUSTE'])}5" in total_formula
    assert f"{excel_column_name(header_col['RECIBOS'])}5" not in total_formula
    assert f"{excel_column_name(header_col['FECHA'])}5" not in total_formula
    assert t1_sheet.cell(5, header_col["SALDO POR COBRAR"]).formula == (
        f"{excel_column_name(header_col['VALOR INMUEBLE'])}5-"
        f"{excel_column_name(header_col['TOTAL RECIBIDO'])}5"
    )
    assert t1_sheet.cell(5, header_col["RECURSOS PROPIOS"]).formula == (
        f"{excel_column_name(header_col['SALDO POR COBRAR'])}5-"
        f"{excel_column_name(header_col['CREDITO BANCARIO'])}5-"
        f"{excel_column_name(header_col['CAJA HONOR'])}5-"
        f"{excel_column_name(header_col['SUBSIDIOS MCY'])}5-"
        f"{excel_column_name(header_col['SUBSIDIOS CAJA COMP'])}5"
    )
    assert t1_sheet.cell(total_sales_row, header_col["NOMBRE CLIENTE"]).value == "TOTAL VENTAS"
    assert t1_sheet.cell(unsold_row, header_col["NOMBRE CLIENTE"]).value == "POR VENDER"
    assert t1_sheet.cell(total_row, header_col["NOMBRE CLIENTE"]).value == "TOTAL"
    assert t1_sheet.cell(total_sales_row, header_col["TOTAL RECIBIDO"]).formula == (
        f"SUM({excel_column_name(header_col['TOTAL RECIBIDO'])}5:"
        f"{excel_column_name(header_col['TOTAL RECIBIDO'])}5)"
    )
    assert t1_sheet.cell(total_sales_row, header_col["AJUSTE"]).formula == (
        f"SUM({excel_column_name(header_col['AJUSTE'])}5:{excel_column_name(header_col['AJUSTE'])}5)"
    )
    assert t1_sheet.cell(unsold_row, header_col["AJUSTE"]).value is None
    assert t1_sheet.cell(total_row, header_col["AJUSTE"]).value is None
    assert t1_sheet.cell(total_row, header_col["SALDO POR COBRAR"]).formula == (
        f"SUM({excel_column_name(header_col['SALDO POR COBRAR'])}5:"
        f"{excel_column_name(header_col['SALDO POR COBRAR'])}5)"
    )
    assert t1_sheet.cell(novelty_title_row, 1).value == "NOVEDADES / OBSERVACIONES"
    assert t1_sheet.cell(novelty_title_row + 1, 1).value != "APTO"
    assert novelty_cession["APTO "] == "101"
    assert novelty_cession["AREA"] == Decimal("65.50")
    assert novelty_cession["ENTIDAD FINANCIERA"] == "BBVA"
    assert novelty_cession["VALOR INMUEBLE"] == Decimal("192172500")
    assert novelty_cession["FECHA"] == "2024-06-01"
    assert novelty_cession["ENCARGO FIDUCIARIO"] == "EF-HIST"
    assert novelty_cession["NUEVOS ENCARGO FIDUCIARIO"] == "EF-EXPORT"
    assert novelty_cession["NOMBRE CLIENTE"] == "TENTACLES LUCAS"
    assert novelty_cession["NOMBRE CLIENTE2"] == "*CESION/ARRAS"
    assert novelty_cession["OBSERVACIONES"] == "NC1 JUN.1/24 CESION DE LUCAS TENTACLES A MARIA JOSE LEYTON"
    novelty_total_formula = t1_sheet.cell(novelty_cession_row, header_col["TOTAL RECIBIDO"]).formula
    assert f"{excel_column_name(header_col['RECIBIDO'])}{novelty_cession_row}" in novelty_total_formula
    assert f"{excel_column_name(header_col['RECIBO FIDUCIA ENE/2025'])}{novelty_cession_row}" in novelty_total_formula
    assert novelty_inclusion["NOMBRE CLIENTE2"] == "INCLUSION"
    assert novelty_inclusion["OBSERVACIONES"] == "INCLUSION LUCAS TENTACLES"
    assert "NC2 OBSERVACION REAL SIN NOVEDAD" in row["OBSERVACIONES"]
    assert row["OBSERVACIONES"].count("NC1 JUN.1/24 CESION DE LUCAS TENTACLES A MARIA JOSE LEYTON") == 1
    assert novelty_cession["OBSERVACIONES"].count("NC1 JUN.1/24 CESION DE LUCAS TENTACLES A MARIA JOSE LEYTON") == 1
    assert novelty_cession["FECHA"] < novelty_inclusion["FECHA"]
    fiducia_ref = excel_column_name(header_col["RECIBO FIDUCIA ENE/2025"])
    assert cell_styles[f"{fiducia_ref}4"] == "FFCCCCFF"
    assert cell_styles[f"{fiducia_ref}5"] == "FFCCCCFF"
    assert cell_styles[f"{fiducia_ref}11"] == "FFCCCCFF"
    assert cell_styles["A4"] is None
    assert cell_styles[f"{excel_column_name(header_col['NOMBRE CLIENTE2'])}4"] is None
    assert cell_styles[f"{excel_column_name(header_col['FECHA PAGO CREDITO'])}4"] == "FFE2F0D9"
    assert cell_styles[f"{excel_column_name(header_col['FECHA PAGO SUBSIDIO'])}4"] == "FFE2F0D9"
    assert cell_styles[f"{excel_column_name(header_col['ABONOS CR CONSTRUCTOR'])}4"] == "FFE2F0D9"
    assert cell_styles[f"{excel_column_name(header_col['DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION'])}4"] == "FFE2F0D9"
    assert cell_styles[f"{excel_column_name(header_col['DESEMBOLSO SUBSIDIOS GOBIERNO'])}4"] == "FFE2F0D9"
    assert cell_styles[f"{excel_column_name(header_col['SUBSIDIOS MCY'])}4"] == "FF0070C0"
    assert cell_styles[f"{excel_column_name(header_col['SUBSIDIOS CAJA COMP'])}4"] == "FF0070C0"
    assert cell_styles[f"{excel_column_name(header_col['ENTIDAD'])}4"] == "FF0070C0"
    assert cell_styles[f"{excel_column_name(header_col['ENTREGA REAL'])}4"] == "FFFFFF00"
    assert cell_styles[f"{excel_column_name(header_col['MATRIC'])}4"] == "FF0070C0"
    assert cell_formats["widths"][header_col["CEDULA CLIENTE"]] == 10.28515625
    assert cell_formats["widths"][header_col["NOMBRE CLIENTE"]] == 31.0
    assert cell_formats["widths"][header_col["RECIBO FIDUCIA ENE/2025"]] == 13.0
    assert cell_formats["widths"][header_col["OBSERVACIONES"]] == 105.140625
    assert cell_formats["heights"][4] == 56.25
    assert cell_formats["heights"][5] == 12.75
    assert cell_formats["heights"][novelty_cession_row] == 13.7
    assert cell_formats["cells"]["A4"]["border"] == ("thin", "thin", "thin", "thin")
    assert cell_formats["cells"]["E5"]["border"] == ("thin", "thin", "thin", "thin")
    assert cell_formats["cells"][f"{excel_column_name(header_col['TOTAL RECIBIDO'])}5"]["border"] == ("thin", "thin", "thin", "thin")
    assert cell_formats["cells"][f"{excel_column_name(header_col['SALDO POR COBRAR'])}5"]["border"] == ("thin", "thin", "thin", "thin")
    assert cell_formats["cells"]["A11"]["border"] == ("thin", "thin", "thin", "thin")
    assert "Creado desde importacion historica" not in content
    assert "Encargo historico reconstruido desde seccion NOVEDADES" not in content
    ed1_headers = [ed1_sheet.cell(4, index).value for index in range(1, ed1_sheet.used_columns + 1)]
    ed1_apto_col = ed1_headers.index("APTO ") + 1
    ed1_exported_units = [ed1_sheet.cell(row_index, ed1_apto_col).value for row_index in range(5, ed1_sheet.used_rows + 1)]
    assert "202" in ed1_exported_units
    assert "RECIBO FIDUCIA FEB/2025" in ed1_headers
    assert "RECIBO FIDUCIA ENE/2025" not in ed1_headers
    assert parsed.statistics.sheets_processed == 2
    assert parsed.sheets[0].header_row == 4
    assert parsed.sheets[0].columns["assignment_number"].header == "ENCARGO FIDUCIARIO"
    assert parsed.sheets[0].columns["client_name_1"].header == "NOMBRE CLIENTE"
    assert parsed.sheets[0].payment_columns
    assert "RECIBIDO" in content


@pytest.mark.django_db
def test_export_main_row_uses_current_cession_observation_and_header(accounting_admin_user, project, tmp_path):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=t1, code="101", name="101")
    client_a = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="A",
        first_names="HOWARD",
        last_names_or_company="TENTACLES",
        phone="3000000001",
    )
    client_b = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="B",
        first_names="SANDY",
        last_names_or_company="PARKER",
        phone="3000000002",
    )
    client_c = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="C",
        first_names="BOB",
        last_names_or_company="STONE",
        phone="3000000003",
    )
    previous_assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-A",
        property_unit=unit,
        start_date=date(2022, 1, 1),
        end_date=date(2023, 5, 2),
        is_active=False,
    )
    intermediate_assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-B",
        property_unit=unit,
        start_date=date(2023, 5, 3),
        end_date=date(2024, 2, 1),
        is_active=False,
    )
    current_assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-C",
        property_unit=unit,
        start_date=date(2024, 2, 2),
        is_active=True,
    )
    for client, start, end, active in (
        (client_a, date(2022, 1, 1), date(2023, 5, 2), False),
        (client_b, date(2023, 5, 3), date(2024, 2, 1), False),
        (client_c, date(2024, 2, 2), None, True),
    ):
        UnitOwnership.objects.create(
            client=client,
            property_unit=unit,
            is_primary=True,
            start_date=start,
            end_date=end,
            is_active=active,
        )
    for assignment, client, active in (
        (previous_assignment, client_a, False),
        (intermediate_assignment, client_b, False),
        (current_assignment, client_c, True),
    ):
        FiduciaryAssignmentHolder.objects.create(
            assignment=assignment,
            client=client,
            is_primary=True,
            start_date=assignment.start_date,
            end_date=assignment.end_date,
            is_active=active,
        )
    _, imported_file = create_imported_file(accounting_admin_user)
    Payment.objects.create(
        assignment=current_assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2024, 2, 2),
        amount=Decimal("15000000.00"),
        concept="CESION | Recibo NC3082",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=5,
    )
    OperationalNovelty.objects.create(
        project=project,
        property_unit=unit,
        previous_client=client_a,
        new_client=client_b,
        previous_assignment=previous_assignment,
        new_assignment=intermediate_assignment,
        novelty_type=OperationalNovelty.NoveltyType.CESSION,
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.IMPORTED,
        effective_date=date(2023, 5, 3),
        summary="*CESION/ARRAS",
        detail="NC111 MAY.3/23 CESION DE HOWARD TENTACLES A SANDY PARKER",
        created_by=accounting_admin_user,
    )
    OperationalNovelty.objects.create(
        project=project,
        property_unit=unit,
        previous_client=client_b,
        new_client=client_c,
        previous_assignment=intermediate_assignment,
        new_assignment=current_assignment,
        novelty_type=OperationalNovelty.NoveltyType.CESSION,
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.IMPORTED,
        effective_date=date(2024, 2, 2),
        summary="*CESION/ARRAS",
        detail="NC3082 MAY.3/23 CESION DE HOWARD TENTACLES A SANDY PARKER *ANTIC $15.000.000 *ARRAS $4'640",
        created_by=accounting_admin_user,
    )

    row = exported_row_for_unit(project, tmp_path, "T1", "101")

    assert row["CESIONES/TRASLADOS"] == Decimal("15000000")
    assert "RECIBIDO CESION" not in row
    assert "CESION DE HOWARD TENTACLES A SANDY PARKER" in row["OBSERVACIONES"]
    assert "NC3082 MAY.3/23" in row["OBSERVACIONES"]
    assert "*ANTIC $15.000.000" in row["OBSERVACIONES"]
    assert "*ARRAS $4'640" in row["OBSERVACIONES"]
    assert "NC111 MAY.3/23" not in row["OBSERVACIONES"]


@pytest.mark.django_db
def test_export_main_row_uses_current_transfer_observation(accounting_admin_user, project, tmp_path):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=t1, code="102", name="102")
    client_a = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="TA",
        first_names="ANA",
        last_names_or_company="DIAZ",
        phone="3000000004",
    )
    client_b = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="TB",
        first_names="LUIS",
        last_names_or_company="RUIZ",
        phone="3000000005",
    )
    previous_assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-OLD",
        property_unit=unit,
        start_date=date(2023, 1, 1),
        end_date=date(2023, 8, 2),
        is_active=False,
    )
    current_assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-NEW",
        property_unit=unit,
        start_date=date(2023, 8, 3),
        is_active=True,
    )
    UnitOwnership.objects.create(
        client=client_a,
        property_unit=unit,
        is_primary=True,
        start_date=previous_assignment.start_date,
        end_date=previous_assignment.end_date,
        is_active=False,
    )
    UnitOwnership.objects.create(
        client=client_b,
        property_unit=unit,
        is_primary=True,
        start_date=current_assignment.start_date,
        is_active=True,
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=previous_assignment,
        client=client_a,
        is_primary=True,
        start_date=previous_assignment.start_date,
        end_date=previous_assignment.end_date,
        is_active=False,
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=current_assignment,
        client=client_b,
        is_primary=True,
        start_date=current_assignment.start_date,
    )
    detail = "NC500 AGO.3/23 TRASLADO DE ANA DIAZ A LUIS RUIZ"
    OperationalNovelty.objects.create(
        project=project,
        property_unit=unit,
        previous_client=client_a,
        new_client=client_b,
        previous_assignment=previous_assignment,
        new_assignment=current_assignment,
        novelty_type=OperationalNovelty.NoveltyType.OTHER,
        other_type="TRASLADO",
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.IMPORTED,
        effective_date=date(2023, 8, 3),
        summary="*TRASLADO",
        detail=detail,
        created_by=accounting_admin_user,
    )

    row = exported_row_for_unit(project, tmp_path, "T1", "102")

    assert detail in row["OBSERVACIONES"]


@pytest.mark.django_db
def test_export_main_row_does_not_promote_unrelated_historical_observations(
    accounting_admin_user, project, tmp_path
):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=t1, code="103", name="103")
    client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="N1",
        first_names="CARLA",
        last_names_or_company="MORA",
        phone="3000000006",
    )
    create_ownership(client, unit)
    assignment = create_assignment(unit, client, "EF-NO-CESION")
    ImportedHistoricalObservation.objects.create(
        project=project,
        property_unit=unit,
        client=client,
        assignment=assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        summary="Nota historica",
        detail="OBSERVACION HISTORICA NO RELACIONADA",
        historical_year=2024,
        historical_month=5,
        dedupe_key="unrelated-historical-observation",
        imported_by=accounting_admin_user,
    )
    OperationalNovelty.objects.create(
        project=project,
        property_unit=unit,
        new_client=client,
        new_assignment=assignment,
        novelty_type=OperationalNovelty.NoveltyType.OTHER,
        other_type="INCLUSION",
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.IMPORTED,
        effective_date=date(2024, 5, 2),
        summary="INCLUSION",
        detail="INCLUSION CARLA MORA",
        created_by=accounting_admin_user,
    )

    row = exported_row_for_unit(project, tmp_path, "T1", "103")

    assert row["OBSERVACIONES"] in (None, "")


@pytest.mark.django_db
def test_export_main_row_does_not_use_novelty_summary_as_observation_detail(accounting_admin_user, project, tmp_path):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=t1, code="104", name="104")
    previous_client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="S1",
        first_names="ANA",
        last_names_or_company="SOTO",
        phone="3000000007",
    )
    current_client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="S2",
        first_names="LUIS",
        last_names_or_company="SOTO",
        phone="3000000008",
    )
    previous_assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-SUM-OLD",
        property_unit=unit,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 5, 1),
        is_active=False,
    )
    current_assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-SUM-NEW",
        property_unit=unit,
        start_date=date(2024, 5, 2),
        is_active=True,
    )
    UnitOwnership.objects.create(
        client=current_client,
        property_unit=unit,
        is_primary=True,
        start_date=current_assignment.start_date,
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=current_assignment,
        client=current_client,
        is_primary=True,
        start_date=current_assignment.start_date,
    )
    OperationalNovelty.objects.create(
        project=project,
        property_unit=unit,
        previous_client=previous_client,
        new_client=current_client,
        previous_assignment=previous_assignment,
        new_assignment=current_assignment,
        novelty_type=OperationalNovelty.NoveltyType.CESSION,
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.IMPORTED,
        effective_date=date(2024, 5, 2),
        summary="RESUMEN SIN DETALLE",
        detail="",
        created_by=accounting_admin_user,
    )

    row = exported_row_for_unit(project, tmp_path, "T1", "104")

    assert row["OBSERVACIONES"] in (None, "")
    assert "RESUMEN SIN DETALLE" not in str(row["OBSERVACIONES"])


@pytest.mark.django_db
def test_export_observations_use_detail_without_summary_fallback(accounting_admin_user, project, active_client, tmp_path):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=t1, code="101", name="101")
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-OBS-DETAIL")
    _, imported_file = create_imported_file(accounting_admin_user)
    ImportedHistoricalObservation.objects.create(
        batch=imported_file.batch,
        imported_file=imported_file,
        project=project,
        property_unit=unit,
        client=active_client,
        assignment=assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        summary="RESUMEN NO EXPORTABLE",
        detail="DETALLE EMPRESARIAL",
        source_sheet="T1",
        source_row=5,
        imported_by=accounting_admin_user,
    )
    ImportedHistoricalObservation.objects.create(
        batch=imported_file.batch,
        imported_file=imported_file,
        project=project,
        property_unit=unit,
        client=active_client,
        assignment=assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        summary="RESUMEN SIN DETALLE",
        detail="",
        source_sheet="T1",
        source_row=6,
        imported_by=accounting_admin_user,
    )

    row = exported_row_for_unit(project, tmp_path, "T1", "101")

    assert row["OBSERVACIONES"] == "DETALLE EMPRESARIAL"
    assert "RESUMEN NO EXPORTABLE" not in row["OBSERVACIONES"]
    assert "RESUMEN SIN DETALLE" not in row["OBSERVACIONES"]


@pytest.mark.django_db
def test_export_includes_all_valid_observations_without_turning_them_into_novelties(
    accounting_admin_user, project, active_client, tmp_path
):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=t1, code="804", name="804")
    create_ownership(active_client, unit, True)
    current_assignment = create_assignment(unit, active_client, "EF-804-ACTUAL")
    historical_assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-804-HIST",
        property_unit=unit,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        is_active=False,
        last_change_reason="Historico",
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=historical_assignment,
        client=active_client,
        is_primary=True,
        is_active=False,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        last_change_reason="Historico",
    )
    _, imported_file = create_imported_file(accounting_admin_user)
    ImportedHistoricalObservation.objects.create(
        batch=imported_file.batch,
        imported_file=imported_file,
        project=project,
        property_unit=unit,
        client=active_client,
        assignment=current_assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        summary="RESUMEN NO EXPORTABLE",
        detail="OBSERVACION DE TEST",
        source_sheet="T1",
        source_row=804,
        source_order=1,
        dedupe_key="obs-test-804",
        imported_by=accounting_admin_user,
    )
    ImportedHistoricalObservation.objects.create(
        batch=imported_file.batch,
        imported_file=imported_file,
        project=project,
        property_unit=unit,
        client=active_client,
        assignment=historical_assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        summary="HISTORICA",
        detail="DETALLE HISTORICO REAL 804",
        historical_year=2025,
        historical_month=5,
        source_sheet="T1",
        source_row=135,
        source_order=2,
        dedupe_key="obs-hist-804",
        imported_by=accounting_admin_user,
    )
    OperationalNovelty.objects.create(
        property_unit=unit,
        historical_client=active_client,
        historical_assignment=historical_assignment,
        novelty_type=OperationalNovelty.NoveltyType.OTHER,
        other_type="TERMINACION",
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.IMPORTED,
        effective_date=date(2026, 1, 15),
        summary="TERMIN/MUTUO AC SIN $$ ENE/2026",
        detail="",
        created_by=accounting_admin_user,
    )

    exported = export_historical_workbook(project)
    export_path = tmp_path / "observations-804.xlsx"
    export_path.write_bytes(exported.content)
    workbook = WorkbookReader().read(export_path)
    sheet = next(sheet for sheet in workbook.sheets if sheet.name == "T1")
    headers = [sheet.cell(4, index).value for index in range(1, sheet.used_columns + 1)]
    observation_col = headers.index("OBSERVACIONES") + 1
    section_row = next(
        row_index
        for row_index in range(5, sheet.used_rows + 1)
        if sheet.cell(row_index, 1).value == "NOVEDADES / OBSERVACIONES"
    )
    exported_novelty_details = [
        sheet.cell(row_index, observation_col).value
        for row_index in range(section_row + 1, sheet.used_rows + 1)
        if sheet.cell(row_index, observation_col).value
    ]
    main_row = exported_row_for_unit(project, tmp_path, "T1", "804")

    assert "VINC" not in headers
    assert main_row["OBSERVACIONES"] == "OBSERVACION DE TEST | DETALLE HISTORICO REAL 804"
    assert "OBSERVACION DE TEST" not in exported_novelty_details
    assert "DETALLE HISTORICO REAL 804" not in exported_novelty_details
    assert "RESUMEN NO EXPORTABLE" not in " ".join(exported_novelty_details)
    assert OperationalNovelty.objects.filter(property_unit=unit).count() == 1
    assert OperationalNovelty.objects.get(property_unit=unit).other_type == "TERMINACION"


@pytest.mark.django_db
def test_export_keeps_main_rows_for_units_without_current_client(accounting_admin_user, project, active_client, tmp_path):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit_without_holder = PropertyUnit.objects.create(project=project, structural_group=t1, code="101", name="101")
    unit_with_holder = PropertyUnit.objects.create(project=project, structural_group=t1, code="102", name="102")
    FiduciaryAssignment.objects.create(
        assignment_number="EF-NO-HOLDER",
        property_unit=unit_without_holder,
        start_date=date(2026, 1, 1),
        is_active=True,
    )
    create_ownership(active_client, unit_with_holder, True)
    create_assignment(unit_with_holder, active_client, "EF-ACTIVE")

    exported = export_historical_workbook(project)
    export_path = tmp_path / "keep-without-current-client.xlsx"
    export_path.write_bytes(exported.content)
    workbook = WorkbookReader().read(export_path)
    sheet = next(sheet for sheet in workbook.sheets if sheet.name == "T1")
    headers = [sheet.cell(4, index).value for index in range(1, sheet.used_columns + 1)]
    apto_col = headers.index("APTO ") + 1
    exported_main_units = []
    rows_by_unit = {}
    for row_index in range(5, sheet.used_rows + 1):
        unit_code = sheet.cell(row_index, apto_col).value
        name_value = sheet.cell(row_index, headers.index("NOMBRE CLIENTE") + 1).value
        if name_value in {"TOTAL VENTAS", "POR VENDER", "TOTAL"}:
            continue
        exported_main_units.append(unit_code)
        rows_by_unit[unit_code] = row_index

    assert "101" in exported_main_units
    assert "102" in exported_main_units
    row_101 = rows_by_unit["101"]
    assert sheet.cell(row_101, headers.index("ENCARGO FIDUCIARIO") + 1).value is None
    assert sheet.cell(row_101, headers.index("CEDULA CLIENTE") + 1).value is None
    assert sheet.cell(row_101, headers.index("NOMBRE CLIENTE") + 1).value is None


@pytest.mark.django_db
def test_export_novelties_includes_chained_cessions_once_per_fact(
    accounting_admin_user, project, tmp_path
):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t7 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T7", name="Torre 7")
    unit = PropertyUnit.objects.create(project=project, structural_group=t7, code="303", name="303")
    leyton = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="75686638",
        first_names="MARIA JOSE",
        last_names_or_company="LEYTON GOMEZ",
        phone="3000000001",
    )
    tentacles = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="75600000",
        first_names="LUCAS",
        last_names_or_company="TENTACLES",
        phone="3000000002",
    )
    watson = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="75692028",
        first_names="CLEVELAND",
        last_names_or_company="WATSON",
        phone="3000000003",
    )
    assignment_leyton = FiduciaryAssignment.objects.create(
        assignment_number="EF-LEYTON",
        property_unit=unit,
        start_date=date(2022, 1, 1),
        end_date=date(2022, 3, 4),
        is_active=False,
    )
    assignment_tentacles = FiduciaryAssignment.objects.create(
        assignment_number="EF-TENTACLES",
        property_unit=unit,
        start_date=date(2022, 3, 4),
        end_date=date(2023, 3, 4),
        is_active=False,
    )
    create_ownership(watson, unit, True)
    assignment_watson = create_assignment(unit, watson, "EF-WATSON")
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment_leyton,
        client=leyton,
        is_primary=True,
        is_active=False,
        start_date=date(2022, 1, 1),
        end_date=date(2022, 3, 4),
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment_tentacles,
        client=tentacles,
        is_primary=True,
        is_active=False,
        start_date=date(2022, 3, 4),
        end_date=date(2023, 3, 4),
    )
    first_detail = "NC6383 MAR.4/22 CESION DE LEYTON MARIA A LUCAS TENTACLES *ANTIC $15.000.000 *ARRAS $4'640"
    second_detail = "NC6383 MAR.4/23 CESION DE LUCAS TENTACLES A CLEVELAND WATSON *ANTIC $15.000.000 *ARRAS $4'640"
    OperationalNovelty.objects.create(
        property_unit=unit,
        previous_client=leyton,
        new_client=tentacles,
        previous_assignment=assignment_leyton,
        new_assignment=assignment_tentacles,
        historical_assignment=assignment_leyton,
        novelty_type=OperationalNovelty.NoveltyType.CESSION,
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.IMPORTED,
        effective_date=date(2022, 3, 4),
        summary="*CESION/ARRAS",
        detail=first_detail,
        created_by=accounting_admin_user,
    )
    OperationalNovelty.objects.create(
        property_unit=unit,
        previous_client=tentacles,
        new_client=watson,
        previous_assignment=assignment_tentacles,
        new_assignment=assignment_watson,
        historical_assignment=assignment_tentacles,
        novelty_type=OperationalNovelty.NoveltyType.CESSION,
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.IMPORTED,
        effective_date=date(2023, 3, 4),
        summary="*CESION/ARRAS",
        detail=second_detail,
        created_by=accounting_admin_user,
    )

    exported = export_historical_workbook(project)
    export_path = tmp_path / "ksmp-t7-303-chained-cessions.xlsx"
    export_path.write_bytes(exported.content)
    workbook = WorkbookReader().read(export_path)
    sheet = next(sheet for sheet in workbook.sheets if sheet.name == "T7")
    headers = [sheet.cell(4, index).value for index in range(1, sheet.used_columns + 1)]
    observation_col = headers.index("OBSERVACIONES") + 1
    name_col = headers.index("NOMBRE CLIENTE") + 1
    section_row = next(
        row_index
        for row_index in range(5, sheet.used_rows + 1)
        if sheet.cell(row_index, 1).value == "NOVEDADES / OBSERVACIONES"
    )
    novelty_details = [
        sheet.cell(row_index, observation_col).value
        for row_index in range(section_row + 1, sheet.used_rows + 1)
        if sheet.cell(row_index, observation_col).value
    ]
    novelty_clients = [
        sheet.cell(row_index, name_col).value
        for row_index in range(section_row + 1, sheet.used_rows + 1)
        if sheet.cell(row_index, observation_col).value
    ]

    assert novelty_details == [first_detail, f"{first_detail} - {second_detail}", second_detail]
    assert novelty_clients == ["LEYTON GOMEZ MARIA JOSE", "TENTACLES LUCAS", "WATSON CLEVELAND"]
    assert novelty_details[1].count("CESION") == 2
    assert "LEYTON MARIA A LUCAS TENTACLES" in novelty_details[1]
    assert "LUCAS TENTACLES A CLEVELAND WATSON" in novelty_details[1]


@pytest.mark.django_db
def test_export_documents_download_stored_file_and_handles_missing_file(
    accounting_client,
    client,
    accounting_admin_user,
    tmp_path,
):
    media_root = tmp_path / "media"
    stored_dir = media_root / "imports"
    stored_dir.mkdir(parents=True)
    stored_file = stored_dir / "archivo.xlsx"
    stored_file.write_bytes(b"contenido fuente")
    with override_settings(MEDIA_ROOT=media_root):
        _, imported_file = create_imported_file(accounting_admin_user)
        imported_file.stored_path = "imports/archivo.xlsx"
        imported_file.save(update_fields=["stored_path"])
        _, missing_file = create_imported_file(accounting_admin_user, ImportedFile.FileType.REPORT)
        missing_file.stored_path = "imports/no-existe.xlsx"
        missing_file.save(update_fields=["stored_path"])
        _, unsafe_file = create_imported_file(accounting_admin_user, ImportedFile.FileType.UNKNOWN)
        unsafe_file.stored_path = "../fuera.xlsx"
        unsafe_file.save(update_fields=["stored_path"])

        home_response = accounting_client.get(reverse("fiduciary:export_home"))
        download_response = accounting_client.get(reverse("fiduciary:export_document_download", args=[imported_file.pk]))
        missing_response = accounting_client.get(reverse("fiduciary:export_document_download", args=[missing_file.pk]))
        unsafe_response = accounting_client.get(reverse("fiduciary:export_document_download", args=[unsafe_file.pk]))
        anonymous_response = client.get(reverse("fiduciary:export_document_download", args=[imported_file.pk]))

    assert home_response.status_code == 200
    assert "archivo-prueba.xlsx" in home_response.content.decode()
    assert "Archivo no disponible" in home_response.content.decode()
    assert download_response.status_code == 200
    assert b"".join(download_response.streaming_content) == b"contenido fuente"
    assert missing_response.status_code == 302
    assert unsafe_response.status_code == 302
    assert anonymous_response.status_code == 302


@pytest.mark.django_db
def test_historical_payments_persist_destination_from_received_headers(accounting_admin_user, unit, active_client):
    from fiduciary.imports.historical.data import HistoricalMonthlyPayment
    from fiduciary.imports.historical.finalize import _FinalizationContext
    from fiduciary.imports.historical.parser import _payment_destination_from_header

    _, imported_file = create_imported_file(accounting_admin_user)
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-DEST-HIST")
    context = _FinalizationContext(batch=imported_file.batch, imported_file=imported_file, user=accounting_admin_user)
    constructora_payment = HistoricalMonthlyPayment(
        month=1,
        year=2026,
        amount=Decimal("1000000"),
        source_row=5,
        source_column="Q",
        source_header="RECIBIDO ENE/2026",
        destination=_payment_destination_from_header("RECIBIDO ENE/2026"),
    )
    fiduciaria_payment = HistoricalMonthlyPayment(
        month=1,
        year=2026,
        amount=Decimal("1800000"),
        source_row=5,
        source_column="T",
        source_header="RECIBIDO FIDUBOGOTA ENE/2026",
        destination=_payment_destination_from_header("RECIBIDO FIDUBOGOTA ENE/2026"),
    )

    context._payment_for_row(assignment, constructora_payment, None)
    context._payment_for_row(assignment, fiduciaria_payment, None)
    context.flush_payments()

    assert Payment.objects.get(amount=Decimal("1000000")).destination == Payment.Destination.CONSTRUCTORA
    assert Payment.objects.get(amount=Decimal("1800000")).destination == Payment.Destination.FIDUCIARIA


@pytest.mark.django_db
def test_backfill_payment_destinations_updates_only_monthly_headers(accounting_admin_user, unit, active_client):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-BACKFILL-DEST")
    _, imported_file = create_imported_file(accounting_admin_user)
    constructora = Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.MONTH,
        period_year=2026,
        period_month=1,
        amount=Decimal("1000000"),
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=5,
        source_header="RECIBIDO ENE/2026",
    )
    fiduciaria = Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.MONTH,
        period_year=2026,
        period_month=2,
        amount=Decimal("1800000"),
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=5,
        source_header="RECIBIDO FIDUBOGOTA FEB/2026",
    )
    unknown = Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2026, 3, 1),
        amount=Decimal("500000"),
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=5,
        source_header="ABONOS CR CONSTRUCTOR",
    )
    _, report_file = create_imported_file(accounting_admin_user, ImportedFile.FileType.REPORT)
    report_payment = Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2026, 4, 1),
        amount=Decimal("900000"),
        movement_type=Payment.MovementType.ADDITION,
        source_file=report_file,
        source_sheet="Reporte",
        source_row=8,
    )

    call_command("backfill_payment_destinations")
    constructora.refresh_from_db()
    fiduciaria.refresh_from_db()
    unknown.refresh_from_db()
    report_payment.refresh_from_db()

    assert constructora.destination == Payment.Destination.CONSTRUCTORA
    assert fiduciaria.destination == Payment.Destination.FIDUCIARIA
    assert unknown.destination is None
    assert report_payment.destination == Payment.Destination.FIDUCIARIA


@pytest.mark.django_db
def test_export_routes_constructor_and_fiduciary_payments_to_separate_monthly_columns(
    accounting_admin_user, project, active_client, tmp_path
):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=t1, code="101", name="101")
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-DEST-EXPORT")
    _, imported_file = create_imported_file(accounting_admin_user)
    _, report_file = create_imported_file(accounting_admin_user, ImportedFile.FileType.REPORT)
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2026, 1, 10),
        amount=Decimal("1000000"),
        concept="ORDINARIO | Recibo NCR-C1",
        destination=Payment.Destination.CONSTRUCTORA,
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=1,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2026, 1, 11),
        amount=Decimal("1800000"),
        concept="ORDINARIO | Recibo NCR-F1",
        destination=Payment.Destination.FIDUCIARIA,
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=2,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2026, 1, 12),
        amount=Decimal("200000"),
        concept="ORDINARIO | Recibo NCR-F2",
        destination=Payment.Destination.FIDUCIARIA,
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=3,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2026, 1, 13),
        amount=Decimal("2330000"),
        concept="APORTES INVERSIONISTAS",
        destination=Payment.Destination.FIDUCIARIA,
        movement_type=Payment.MovementType.ADDITION,
        source_file=report_file,
        source_sheet="Reporte",
        source_row=4,
    )

    exported = export_historical_workbook(project)
    export_path = tmp_path / "destinations.xlsx"
    export_path.write_bytes(exported.content)
    workbook = WorkbookReader().read(export_path)
    sheet = next(sheet for sheet in workbook.sheets if sheet.name == "T1")
    headers = [sheet.cell(4, index).value for index in range(1, sheet.used_columns + 1)]
    header_col = {header: index + 1 for index, header in enumerate(headers) if header}
    row = {header: sheet.cell(5, index + 1).value for index, header in enumerate(headers) if header}

    assert row["FECHA"] == "ENE.10/26 - ENE.11/26F - ENE.12/26F || ENE.13/26F"
    assert row["RECIBOS"] == "NCR-C1"
    assert row["RECIBOS FIDUBOGOTA"] == "NCR-F1 - NCR-F2"
    assert row["RECIBIDO"] == Decimal("1000000")
    assert sheet.cell(5, header_col["RECIBO FIDUCIA ENE/2026"]).formula == "1800000+200000+2330000"
    assert "RECIBIDO ENE/2026" not in headers
    assert "RECIBIDO FIDUBOGOTA ENE/2026" not in headers
    assert "||" not in str(row["RECIBIDO"])
    assert "||" not in str(sheet.cell(5, header_col["RECIBO FIDUCIA ENE/2026"]).formula)
    total_formula = sheet.cell(5, header_col["TOTAL RECIBIDO"]).formula
    assert f"{excel_column_name(header_col['RECIBIDO'])}5" in total_formula
    assert f"{excel_column_name(header_col['RECIBO FIDUCIA ENE/2026'])}5" in total_formula
    assert f"{excel_column_name(header_col['RECIBOS'])}5" not in total_formula
    assert f"{excel_column_name(header_col['FECHA'])}5" not in total_formula


@pytest.mark.django_db
def test_assignment_detail_registers_manual_constructor_and_fiduciary_payments(
    accounting_client, project, active_client, tmp_path
):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=t1, code="101", name="101")
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-MANUAL-PAY")

    detail = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk]))
    assert reverse("fiduciary:assignment_payment_create", args=[assignment.pk]) in detail.content.decode()
    assert "Registrar pago" in detail.content.decode()

    constructora_response = accounting_client.post(
        reverse("fiduciary:assignment_payment_create", args=[assignment.pk]),
        {
            "exact_date": "2026-01-10",
            "amount": "1000000",
            "concept": "ABONO MANUAL",
            "destination": Payment.Destination.CONSTRUCTORA,
        },
    )
    fiduciaria_response = accounting_client.post(
        reverse("fiduciary:assignment_payment_create", args=[assignment.pk]),
        {
            "exact_date": "2026-01-11",
            "amount": "1800000",
            "concept": "ABONO MANUAL",
            "destination": Payment.Destination.FIDUCIARIA,
        },
    )

    assert constructora_response.status_code == 302
    assert fiduciaria_response.status_code == 302
    payments = list(assignment.payments.order_by("exact_date"))
    assert [payment.destination for payment in payments] == [
        Payment.Destination.CONSTRUCTORA,
        Payment.Destination.FIDUCIARIA,
    ]
    assert all(payment.movement_type == Payment.MovementType.ADDITION for payment in payments)
    assert all("Recibo" not in (payment.concept or "") for payment in payments)

    updated_detail = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk]))
    detail_content = updated_detail.content.decode()
    assert "ABONO MANUAL" in detail_content
    assert "Constructora" in detail_content
    assert "Fiduciaria" in detail_content

    row = exported_row_for_unit(project, tmp_path, "T1", "101")
    assert row["FECHA"] == "ENE.10/26 - ENE.11/26F"
    assert not row["RECIBOS"]
    assert row["RECIBOS FIDUBOGOTA"] in ("", None)
    assert row["RECIBIDO"] == Decimal("1000000")
    assert row["RECIBO FIDUCIA ENE/2026"] == Decimal("1800000")
    assert "||" not in str(row["RECIBIDO"])
    assert "||" not in str(row["RECIBO FIDUCIA ENE/2026"])


@pytest.mark.django_db
def test_export_manual_report_fiduciary_payment_creates_month_column_from_payment(
    accounting_admin_user, project, grouping_type, active_client, tmp_path
):
    group = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="test", name="agrupacion")
    unit = PropertyUnit.objects.create(project=project, structural_group=group, code="unidad 2", name="unidad 2")
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "123654789012")
    _, report_file = create_imported_file(accounting_admin_user, ImportedFile.FileType.REPORT)
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2026, 9, 15),
        amount=Decimal("10000000"),
        concept="NCR782",
        destination=Payment.Destination.FIDUCIARIA,
        movement_type=Payment.MovementType.ADDITION,
        source_file=report_file,
        source_sheet="Pagos manuales",
        source_row=1,
    )

    exported = export_historical_workbook(project)
    export_path = tmp_path / "libro-test.xlsx"
    export_path.write_bytes(exported.content)
    workbook = WorkbookReader().read(export_path)
    sheet = next(sheet for sheet in workbook.sheets if sheet.name == "test")
    headers = [sheet.cell(4, index).value for index in range(1, sheet.used_columns + 1)]
    header_col = {header: index + 1 for index, header in enumerate(headers) if header}
    row = {header: sheet.cell(5, index + 1).value for index, header in enumerate(headers) if header}

    assert "RECIBO FIDUCIA SEP/2026" in headers
    assert row["RECIBOS FIDUBOGOTA"] == "NCR782"
    assert row["FECHA"] == "SEP.15/26F"
    assert sheet.cell(5, header_col["RECIBO FIDUCIA SEP/2026"]).value == Decimal("10000000")


@pytest.mark.django_db
def test_payment_list_shows_global_register_payment_action(accounting_client):
    response = accounting_client.get(reverse("fiduciary:payment_list"))
    content = response.content.decode()

    assert response.status_code == 200
    assert "Registrar pago" in content
    assert reverse("fiduciary:payment_create") in content


@pytest.mark.django_db
def test_global_payment_create_by_assignment_number(accounting_client, active_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-GLOBAL-NUM")

    get_response = accounting_client.get(reverse("fiduciary:payment_create"))
    assert get_response.status_code == 200
    assert "Numero de encargo fiduciario" in get_response.content.decode()

    response = accounting_client.post(
        reverse("fiduciary:payment_create"),
        {
            "assignment_number": "EF-GLOBAL-NUM",
            "exact_date": "2026-03-10",
            "amount": "1230000",
            "concept": "CUOTA MANUAL",
            "destination": Payment.Destination.CONSTRUCTORA,
        },
    )
    payment = Payment.objects.get(assignment=assignment)

    assert response.status_code == 302
    assert response.url == f"{reverse('fiduciary:payment_list')}?assignment_number=EF-GLOBAL-NUM"
    assert payment.amount == Decimal("1230000")
    assert payment.concept == "CUOTA MANUAL"
    assert payment.destination == Payment.Destination.CONSTRUCTORA
    assert payment.movement_type == Payment.MovementType.ADDITION

    list_response = accounting_client.get(response.url)
    list_content = list_response.content.decode()
    assert "CUOTA MANUAL" in list_content
    assert "Abono" in list_content
    assert "Adicion" not in list_content


@pytest.mark.django_db
def test_global_payment_create_by_unit_hierarchy(accounting_client, project, grouping_type, active_client):
    group = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=group, code="202", name="202")
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-GLOBAL-UNIT")

    context_response = accounting_client.get(reverse("fiduciary:payment_unit_assignment"), {"unit": unit.pk})
    assert context_response.status_code == 200
    assert context_response.json()["assignment"]["number"] == "EF-GLOBAL-UNIT"

    response = accounting_client.post(
        reverse("fiduciary:payment_create"),
        {
            "project": project.pk,
            "grouping_type": grouping_type.pk,
            "structural_group": group.pk,
            "property_unit": unit.pk,
            "exact_date": "2026-04-11",
            "amount": "2500000",
            "concept": "PAGO POR JERARQUIA",
            "destination": Payment.Destination.FIDUCIARIA,
        },
    )
    payment = Payment.objects.get(assignment=assignment)

    assert response.status_code == 302
    assert payment.amount == Decimal("2500000")
    assert payment.concept == "PAGO POR JERARQUIA"
    assert payment.destination == Payment.Destination.FIDUCIARIA


@pytest.mark.django_db
def test_assignment_with_current_holder_is_active_and_allows_manual_payment(accounting_client, active_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-ACTIVE-HOLDER")

    detail = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk]))
    content = detail.content.decode()
    assert "Estado: Vigente" in content
    assert reverse("fiduciary:assignment_payment_create", args=[assignment.pk]) in content

    response = accounting_client.post(
        reverse("fiduciary:assignment_payment_create", args=[assignment.pk]),
        {
            "exact_date": "2026-06-01",
            "amount": "900000",
            "concept": "PAGO ACTIVO",
            "destination": Payment.Destination.CONSTRUCTORA,
        },
    )

    assert response.status_code == 302
    assert assignment.payments.filter(concept="PAGO ACTIVO").exists()


@pytest.mark.django_db
def test_assignment_without_holders_is_inactive_and_blocks_direct_payment(accounting_client, unit):
    assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-NO-HOLDERS",
        property_unit=unit,
        start_date=date(2026, 1, 1),
        last_change_reason="Registro manual",
    )

    detail = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk]))
    content = detail.content.decode()
    assert "Estado: Inactivo" in content
    assert reverse("fiduciary:assignment_payment_create", args=[assignment.pk]) not in content
    list_response = accounting_client.get(reverse("fiduciary:assignment_list"), {"q": "EF-NO-HOLDERS"})
    assert "status-pill status-inactive" in list_response.content.decode()

    response = accounting_client.post(
        reverse("fiduciary:assignment_payment_create", args=[assignment.pk]),
        {
            "exact_date": "2026-06-02",
            "amount": "900000",
            "concept": "PAGO BLOQUEADO",
            "destination": Payment.Destination.CONSTRUCTORA,
        },
    )

    assert response.status_code == 200
    assert "No se puede registrar el pago porque el encargo no tiene titulares vigentes." in response.content.decode()
    assert assignment.payments.count() == 0


@pytest.mark.django_db
def test_assignment_with_only_historical_holder_is_inactive_and_blocks_payment(accounting_client, active_client, unit):
    assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-HISTORICAL-HOLDER",
        property_unit=unit,
        start_date=date(2025, 1, 1),
        last_change_reason="Registro manual",
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=active_client,
        is_primary=True,
        is_active=False,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        last_change_reason="Finalizado",
    )

    detail = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk]))
    assert "Estado: Inactivo" in detail.content.decode()

    response = accounting_client.post(
        reverse("fiduciary:payment_create"),
        {
            "assignment_number": "EF-HISTORICAL-HOLDER",
            "exact_date": "2026-06-03",
            "amount": "900000",
            "concept": "PAGO BLOQUEADO",
            "destination": Payment.Destination.CONSTRUCTORA,
        },
    )

    assert response.status_code == 200
    assert "No se puede registrar el pago porque el encargo no tiene titulares vigentes." in response.content.decode()
    assert assignment.payments.count() == 0


@pytest.mark.django_db
def test_global_payment_assignment_number_rejects_assignment_without_current_holder(accounting_client, unit):
    assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-GLOBAL-NO-HOLDER",
        property_unit=unit,
        start_date=date(2026, 1, 1),
        last_change_reason="Registro manual",
    )

    response = accounting_client.post(
        reverse("fiduciary:payment_create"),
        {
            "assignment_number": "EF-GLOBAL-NO-HOLDER",
            "exact_date": "2026-06-04",
            "amount": "1000000",
            "concept": "PAGO MANUAL",
            "destination": Payment.Destination.FIDUCIARIA,
        },
    )

    assert response.status_code == 200
    assert "No se puede registrar el pago porque el encargo no tiene titulares vigentes." in response.content.decode()
    assert assignment.payments.count() == 0


@pytest.mark.django_db
def test_payment_unit_assignment_endpoint_rejects_assignment_without_current_holder(accounting_client, unit):
    FiduciaryAssignment.objects.create(
        assignment_number="EF-UNIT-NO-HOLDER",
        property_unit=unit,
        start_date=date(2026, 1, 1),
        last_change_reason="Registro manual",
    )

    response = accounting_client.get(reverse("fiduciary:payment_unit_assignment"), {"unit": unit.pk})

    assert response.status_code == 200
    assert response.json()["assignment"] is None
    assert "titulares vigentes" in response.json()["message"]


@pytest.mark.django_db
def test_global_payment_create_rejects_unknown_assignment(accounting_client):
    response = accounting_client.post(
        reverse("fiduciary:payment_create"),
        {
            "assignment_number": "EF-NO-EXISTE",
            "exact_date": "2026-05-12",
            "amount": "1000000",
            "concept": "PAGO MANUAL",
            "destination": Payment.Destination.CONSTRUCTORA,
        },
    )

    assert response.status_code == 200
    assert "No existe un encargo fiduciario con ese numero." in response.content.decode()
    assert Payment.objects.count() == 0


@pytest.mark.django_db
def test_export_formats_cession_and_transfer_dates_with_historical_markers(
    accounting_admin_user, project, active_client, tmp_path
):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=t1, code="102", name="102")
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-CESSION-DATES")
    _, imported_file = create_imported_file(accounting_admin_user)
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2023, 5, 3),
        amount=Decimal("15000000"),
        concept="CESION | Recibo NC3082",
        destination=Payment.Destination.CONSTRUCTORA,
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=1,
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2025, 5, 9),
        amount=Decimal("12000000"),
        concept="TRASLADO | Recibo NC500",
        destination=Payment.Destination.CONSTRUCTORA,
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=2,
    )

    row = exported_row_for_unit(project, tmp_path, "T1", "102")

    assert row["FECHA"] == "(MAY.3/23CESION) - (MAY.9/25TRASLADO)"


@pytest.mark.django_db
def test_export_marks_cession_transfer_receipts_and_preserves_zero_amount(
    accounting_admin_user, project, active_client, tmp_path
):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit_101 = PropertyUnit.objects.create(project=project, structural_group=t1, code="101", name="101")
    unit_102 = PropertyUnit.objects.create(project=project, structural_group=t1, code="102", name="102")
    unit_103 = PropertyUnit.objects.create(project=project, structural_group=t1, code="103", name="103")
    create_ownership(active_client, unit_101, True)
    create_ownership(active_client, unit_102, True)
    create_ownership(active_client, unit_103, True)
    assignment_101 = create_assignment(unit_101, active_client, "EF-CESSION-ZERO")
    assignment_102 = create_assignment(unit_102, active_client, "EF-TRANSFER")
    create_assignment(unit_103, active_client, "EF-NO-CESSION")
    _, imported_file = create_imported_file(accounting_admin_user)

    Payment.objects.create(
        assignment=assignment_101,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2026, 4, 14),
        amount=Decimal("0"),
        concept="CESION | Recibo NC26448",
        destination=Payment.Destination.CONSTRUCTORA,
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=1,
    )
    Payment.objects.create(
        assignment=assignment_102,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2026, 4, 8),
        amount=Decimal("2500000"),
        concept="TRASLADO | TRASL AP101 MTCT1 | Recibo NC26449",
        destination=Payment.Destination.CONSTRUCTORA,
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=2,
    )

    exported = export_historical_workbook(project)
    export_path = tmp_path / "cession-transfer-zero.xlsx"
    export_path.write_bytes(exported.content)
    workbook = WorkbookReader().read(export_path)
    sheet = next(sheet for sheet in workbook.sheets if sheet.name == "T1")
    headers = [sheet.cell(4, index).value for index in range(1, sheet.used_columns + 1)]
    header_col = {header: index + 1 for index, header in enumerate(headers) if header}
    rows = {
        sheet.cell(row_index, header_col["APTO "]).value: {
            header: sheet.cell(row_index, index + 1).value for index, header in enumerate(headers) if header
        }
        for row_index in range(5, 8)
    }
    cell_types = xlsx_cell_types(exported.content, sheet_index=sheet.index)
    zero_ref = f"{excel_column_name(header_col['CESIONES/TRASLADOS'])}5"

    assert rows["101"]["RECIBOS"] == "NC26448CESION"
    assert rows["101"]["FECHA"] == "(ABR.14/26CESION)"
    assert rows["101"]["CESIONES/TRASLADOS"] == Decimal("0")
    assert cell_types[zero_ref]["type"] is None
    assert cell_types[zero_ref]["value"] == "0"
    assert rows["102"]["RECIBOS"] == "NC26449TRASLADO"
    assert rows["102"]["FECHA"] == "(ABR.8/26TRASLADO)"
    assert rows["102"]["CESIONES/TRASLADOS"] == Decimal("2500000")
    assert rows["103"]["CESIONES/TRASLADOS"] is None
    assert headers[headers.index("CESIONES/TRASLADOS") + 1] == "FECHA PAGO CREDITO"
    assert headers[headers.index("FECHA PAGO CREDITO") + 1] == "FECHA PAGO SUBSIDIO"
    with pytest.raises(ValidationError):
        Payment.objects.create(
            assignment=assignment_101,
            date_precision=Payment.DatePrecision.EXACT,
            exact_date=date(2026, 4, 15),
            amount=Decimal("-1"),
            concept="CESION | Recibo NC26450",
            destination=Payment.Destination.CONSTRUCTORA,
            movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
            source_file=imported_file,
            source_sheet="T1",
            source_row=3,
        )


@pytest.mark.django_db
def test_exported_unreceipted_separator_dates_reimport_as_individual_payments(
    accounting_admin_user, project, active_client, tmp_path
):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=t1, code="103", name="103")
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-ROUNDTRIP")
    _, report_file = create_imported_file(accounting_admin_user, ImportedFile.FileType.REPORT)
    payment_specs = [
        (date(2026, 1, 10), Decimal("500000"), Payment.Destination.CONSTRUCTORA),
        (date(2026, 1, 20), Decimal("700000"), Payment.Destination.CONSTRUCTORA),
        (date(2026, 2, 5), Decimal("900000"), Payment.Destination.FIDUCIARIA),
    ]
    for row_number, (payment_date, amount, destination) in enumerate(payment_specs, start=1):
        Payment.objects.create(
            assignment=assignment,
            date_precision=Payment.DatePrecision.EXACT,
            exact_date=payment_date,
            amount=amount,
            concept="ABONO MANUAL",
            destination=destination,
            movement_type=Payment.MovementType.ADDITION,
            source_file=report_file,
            source_sheet="Manual",
            source_row=row_number,
        )
    exported = export_historical_workbook(project)
    export_path = tmp_path / "roundtrip-unreceipted.xlsx"
    export_path.write_bytes(exported.content)

    parsed = HistoricalWorkbookParser(export_path).parse()
    parsed_row = next(row for sheet in parsed.sheets for row in sheet.rows if row.unit_code == "103")
    reconstructed = sorted(parsed_row.reconstructed_payments, key=lambda payment: payment.date_value)

    assert parsed_row.payments == []
    assert [(payment.date_value, payment.amount, payment.destination, payment.receipt) for payment in reconstructed] == [
        ("ENE.10/26", Decimal("500000"), Payment.Destination.CONSTRUCTORA, ""),
        ("ENE.20/26", Decimal("700000"), Payment.Destination.CONSTRUCTORA, ""),
        ("FEB.5/26F", Decimal("900000"), Payment.Destination.FIDUCIARIA, ""),
    ]


@pytest.mark.django_db
def test_export_writes_single_monetary_amounts_as_numbers_and_additive_amounts_as_formulas(
    accounting_admin_user, project, active_client, tmp_path
):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=t1, code="101", name="101")
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-NUMERIC-EXPORT")
    _, imported_file = create_imported_file(accounting_admin_user)
    payments = [
        ("CESION | Recibo NC3082", Decimal("12000000"), date(2026, 1, 5), Payment.Destination.CONSTRUCTORA),
        ("ABONO", Decimal("10076000"), date(2026, 6, 10), Payment.Destination.FIDUCIARIA),
        ("SUBSIDIO | Recibo SB001", Decimal("15000000"), date(2026, 2, 15), Payment.Destination.FIDUCIARIA),
        ("ABONO", Decimal("24113500"), date(2026, 7, 1), Payment.Destination.FIDUCIARIA),
        ("ABONO", Decimal("17178000"), date(2026, 7, 2), Payment.Destination.FIDUCIARIA),
        ("ABONO", Decimal("7102000"), date(2026, 7, 3), Payment.Destination.FIDUCIARIA),
    ]
    for row_number, (concept, amount, payment_date, destination) in enumerate(payments, start=1):
        Payment.objects.create(
            assignment=assignment,
            date_precision=Payment.DatePrecision.EXACT,
            exact_date=payment_date,
            amount=amount,
            concept=concept,
            destination=destination,
            movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
            source_file=imported_file,
            source_sheet="T1",
            source_row=row_number,
        )

    exported = export_historical_workbook(project)
    export_path = tmp_path / "numeric-amounts.xlsx"
    export_path.write_bytes(exported.content)
    workbook = WorkbookReader().read(export_path)
    sheet = next(sheet for sheet in workbook.sheets if sheet.name == "T1")
    headers = [sheet.cell(4, index).value for index in range(1, sheet.used_columns + 1)]
    header_col = {header: index + 1 for index, header in enumerate(headers) if header}
    cell_types = xlsx_cell_types(exported.content, sheet_index=sheet.index)

    numeric_headers = [
        "CESIONES/TRASLADOS",
        "RECIBO FIDUCIA JUN/2026",
        "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION",
    ]
    for header in numeric_headers:
        ref = f"{excel_column_name(header_col[header])}5"
        assert cell_types[ref]["type"] is None
        assert cell_types[ref]["formula"] is False
        assert cell_types[ref]["value"]

    additive_ref = f"{excel_column_name(header_col['RECIBO FIDUCIA JUL/2026'])}5"
    assert cell_types[additive_ref]["type"] is None
    assert cell_types[additive_ref]["formula"] is True
    assert sheet.cell(5, header_col["RECIBO FIDUCIA JUL/2026"]).formula == "24113500+17178000+7102000"

    total_formula = sheet.cell(5, header_col["TOTAL RECIBIDO"]).formula
    for header in [*numeric_headers, "RECIBO FIDUCIA JUL/2026"]:
        assert f"{excel_column_name(header_col[header])}5" in total_formula
    assert f"{excel_column_name(header_col['RECIBOS'])}5" not in total_formula
    assert f"{excel_column_name(header_col['FECHA'])}5" not in total_formula


@pytest.mark.django_db
def test_export_preserves_historical_receipt_category_suffixes(accounting_admin_user, project, active_client, tmp_path):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=t1, code="201", name="201")
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-RECEIPTS")
    _, imported_file = create_imported_file(accounting_admin_user)
    _, report_file = create_imported_file(accounting_admin_user, ImportedFile.FileType.REPORT)
    payment_specs = [
        ("ORDINARIO | Recibo NCR1631", Decimal("1000000"), date(2026, 1, 1), imported_file),
        ("CREDITO | Recibo NCR3184CR", Decimal("2000000"), date(2026, 1, 2), imported_file),
        ("SUBSIDIO | Recibo NCR3185SB", Decimal("3000000"), date(2026, 1, 3), imported_file),
        ("SUBSIDIO | Recibo NCR2051SUB", Decimal("4000000"), date(2026, 1, 4), imported_file),
        ("ABONO SIN RECIBO", Decimal("5000000"), date(2026, 1, 5), report_file),
    ]
    for row_number, (concept, amount, payment_date, source_file) in enumerate(payment_specs, start=1):
        Payment.objects.create(
            assignment=assignment,
            date_precision=Payment.DatePrecision.EXACT,
            exact_date=payment_date,
            amount=amount,
            concept=concept,
            destination=Payment.Destination.CONSTRUCTORA,
            movement_type=(
                Payment.MovementType.ADDITION
                if source_file.file_type == ImportedFile.FileType.REPORT
                else Payment.MovementType.HISTORICAL_PAYMENT
            ),
            source_file=source_file,
            source_sheet="T1",
            source_row=row_number,
        )

    row = exported_row_for_unit(project, tmp_path, "T1", "201")

    assert row["RECIBOS"] == "NCR1631 - NCR3184CR - NCR3185SB - NCR2051SUB"
    assert "NCR1631CR" not in row["RECIBOS"]
    assert "ABONO SIN RECIBO" not in row["RECIBOS"]


@pytest.mark.django_db
def test_export_orders_historical_receipts_by_business_category(accounting_admin_user, project, active_client, tmp_path):
    tower = GroupingType.objects.create(code="T", name="Torre")
    t1 = StructuralGroup.objects.create(project=project, grouping_type=tower, code="T1", name="Torre 1")
    unit_307 = PropertyUnit.objects.create(project=project, structural_group=t1, code="307", name="307")
    unit_308 = PropertyUnit.objects.create(project=project, structural_group=t1, code="308", name="308")
    create_ownership(active_client, unit_307, True)
    create_ownership(active_client, unit_308, True)
    assignment_307 = create_assignment(unit_307, active_client, "EF-RECEIPT-ORDER-SB")
    assignment_308 = create_assignment(unit_308, active_client, "EF-RECEIPT-ORDER-SUB")
    _, imported_file = create_imported_file(accounting_admin_user)

    receipts_307 = [
        ("ORDINARIO | Recibo NCR3313", date(2026, 1, 1)),
        ("ORDINARIO | Recibo NCR3320", date(2026, 1, 2)),
        ("ORDINARIO | Recibo NCR3323", date(2026, 1, 3)),
        ("ORDINARIO | Recibo NCR3311", date(2026, 1, 4)),
        ("ORDINARIO | Recibo NCR3322", date(2026, 1, 5)),
        ("ORDINARIO | Recibo NCR3319", date(2026, 1, 6)),
        ("ORDINARIO | Recibo NCR3315", date(2026, 1, 7)),
        ("ORDINARIO | Recibo NCR3312", date(2026, 1, 8)),
        ("ORDINARIO | Recibo NCR3321", date(2026, 1, 9)),
        ("ORDINARIO | Recibo NCR3318", date(2026, 1, 10)),
        ("ORDINARIO | Recibo NCR3317", date(2026, 1, 11)),
        ("ORDINARIO | Recibo NCR3316", date(2026, 1, 12)),
        ("CREDITO | Recibo NCR3324CR", date(2026, 1, 13)),
        ("SUBSIDIO | Recibo NCR3325SB", date(2026, 1, 14)),
        ("ORDINARIO | Recibo NCR3314", date(2026, 1, 15)),
    ]
    receipts_308 = [
        ("ORDINARIO | Recibo NCR2001", date(2026, 2, 1)),
        ("CREDITO | Recibo NCR2002CR", date(2026, 2, 2)),
        ("SUBSIDIO | Recibo NCR2003SUB", date(2026, 2, 3)),
        ("ORDINARIO | Recibo NCR2004", date(2026, 2, 4)),
    ]
    for row_number, (assignment, specs) in enumerate(
        ((assignment_307, receipts_307), (assignment_308, receipts_308)),
        start=1,
    ):
        for offset, (concept, payment_date) in enumerate(specs):
            Payment.objects.create(
                assignment=assignment,
                date_precision=Payment.DatePrecision.EXACT,
                exact_date=payment_date,
                amount=Decimal("1000000"),
                concept=concept,
                destination=Payment.Destination.CONSTRUCTORA,
                movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
                source_file=imported_file,
                source_sheet="T1",
                source_row=(row_number * 100) + offset,
            )

    row_307 = exported_row_for_unit(project, tmp_path, "T1", "307")
    row_308 = exported_row_for_unit(project, tmp_path, "T1", "308")

    assert row_307["RECIBOS"].endswith("NCR3316 - NCR3314 - NCR3324CR - NCR3325SB")
    assert "NCR3316 - NCR3324CR - NCR3325SB - NCR3314" not in row_307["RECIBOS"]
    assert row_308["RECIBOS"] == "NCR2001 - NCR2004 - NCR2002CR - NCR2003SUB"


@pytest.mark.django_db
def test_payment_destination_is_visible_in_payment_list_and_assignment_detail(
    accounting_client, accounting_admin_user, active_client, unit
):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-DEST-UI")
    payment = create_payment_for_assignment(assignment, accounting_admin_user, "2500000.00")
    payment.destination = Payment.Destination.FIDUCIARIA
    payment.save(update_fields=["destination"])

    list_response = accounting_client.get(reverse("fiduciary:payment_list"), {"assignment_number": "EF-DEST-UI"})
    detail_response = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk]))

    assert "Recibido por" in list_response.content.decode()
    assert "Fiduciaria" in list_response.content.decode()
    assert "Recibido por" in detail_response.content.decode()
    assert "Fiduciaria" in detail_response.content.decode()


@pytest.mark.django_db
def test_export_documents_filters_by_type_filename_and_date(accounting_client, accounting_admin_user, tmp_path):
    media_root = tmp_path / "media"
    imports_dir = media_root / "imports"
    imports_dir.mkdir(parents=True)
    (imports_dir / "LIBRO_KSMP.xlsx").write_bytes(b"historico")
    (imports_dir / "consolidado_KSMP.xlsx").write_bytes(b"reporte")
    (imports_dir / "consolidado_OTRO.xlsx").write_bytes(b"otro")
    historical_batch, historical_file = create_imported_file(accounting_admin_user, ImportedFile.FileType.HISTORICAL)
    report_batch, report_file = create_imported_file(accounting_admin_user, ImportedFile.FileType.REPORT)
    other_batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        imported_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.REPORTS,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.COMPLETED,
        total_files=1,
        processed_files=1,
        summary="Lote de prueba",
    )
    other_file = ImportedFile.objects.create(
        batch=other_batch,
        original_name="archivo-prueba.xlsx",
        extension=".xlsx",
        size_bytes=128,
        sha256="c" * 64,
        file_type=ImportedFile.FileType.REPORT,
        status=ImportedFile.Status.COMPLETED,
        order=1,
    )
    historical_file.original_name = "LIBRO_KSMP.xlsx"
    historical_file.stored_path = "imports/LIBRO_KSMP.xlsx"
    historical_file.save(update_fields=["original_name", "stored_path"])
    report_file.original_name = "consolidado_KSMP.xlsx"
    report_file.stored_path = "imports/consolidado_KSMP.xlsx"
    report_file.save(update_fields=["original_name", "stored_path"])
    other_file.original_name = "consolidado_OTRO.xlsx"
    other_file.stored_path = "imports/consolidado_OTRO.xlsx"
    other_file.save(update_fields=["original_name", "stored_path"])
    ImportedFile.objects.filter(pk=historical_file.pk).update(created_at=timezone.make_aware(datetime(2026, 9, 6, 10, 0)))
    ImportedFile.objects.filter(pk=report_file.pk).update(created_at=timezone.make_aware(datetime(2026, 9, 7, 10, 0)))
    ImportedFile.objects.filter(pk=other_file.pk).update(created_at=timezone.make_aware(datetime(2026, 9, 8, 10, 0)))

    with override_settings(MEDIA_ROOT=media_root):
        filtered = accounting_client.get(
            reverse("fiduciary:export_home"),
            {
                "file_type": ImportedFile.FileType.REPORT,
                "filename": "ksmp",
                "date_from": "2026-09-07",
                "date_to": "2026-09-07",
            },
        )
        filtered_content = filtered.content.decode()
        cleared = accounting_client.get(reverse("fiduciary:export_home"))
        cleared_content = cleared.content.decode()

    assert filtered.status_code == 200
    assert "consolidado_KSMP.xlsx" in filtered_content
    assert "LIBRO_KSMP.xlsx" not in filtered_content
    assert "consolidado_OTRO.xlsx" not in filtered_content
    assert reverse("fiduciary:export_home") in filtered_content
    assert "Descargar" in filtered_content
    assert cleared.status_code == 200
    assert "LIBRO_KSMP.xlsx" in cleared_content
    assert "consolidado_KSMP.xlsx" in cleared_content
    assert "consolidado_OTRO.xlsx" in cleared_content


@pytest.mark.django_db
def test_assignment_detail_edits_financial_entity_in_payment_context(accounting_client, accounting_admin_user, active_client, unit):
    unit.financial_entity = "BANCO CAJA SOCIAL"
    unit.save(update_fields=["financial_entity"])
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-FIN")
    create_payment_for_assignment(assignment, accounting_admin_user, "2500000.00")

    detail_response = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk]))
    detail_content = detail_response.content.decode()
    unit_response = accounting_client.get(reverse("real_estate:property_unit_history", args=[unit.pk]))

    assert detail_response.status_code == 200
    assert "Pagos registrados" in detail_content
    assert "Entidad financiera" in detail_content
    assert "BANCO CAJA SOCIAL" in detail_content
    assert "BANCO CAJA SOCIAL" not in unit_response.content.decode()

    update_response = accounting_client.post(
        reverse("fiduciary:assignment_financial_entity_update", args=[assignment.pk]),
        {"financial_entity": "BBVA"},
    )
    unit.refresh_from_db()

    assert update_response.status_code == 302
    assert unit.financial_entity == "BBVA"


@pytest.mark.django_db
def test_assignment_financial_entity_blank_post_does_not_delete_existing_value(accounting_client, active_client, unit):
    unit.financial_entity = "BANCO DE BOGOTA"
    unit.save(update_fields=["financial_entity"])
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-FIN-BLANK")

    response = accounting_client.post(
        reverse("fiduciary:assignment_financial_entity_update", args=[assignment.pk]),
        {"financial_entity": ""},
    )
    unit.refresh_from_db()

    assert response.status_code == 302
    assert unit.financial_entity == "BANCO DE BOGOTA"


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
def test_report_payments_are_displayed_as_abono_in_payment_list(accounting_client, accounting_admin_user, active_client, unit):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-ABONO")
    _, imported_file = create_imported_file(accounting_admin_user, ImportedFile.FileType.REPORT)
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2026, 7, 25),
        amount=Decimal("1500000.00"),
        movement_type=Payment.MovementType.ADDITION,
        source_file=imported_file,
        source_sheet="Reporte",
        source_row=2,
    )

    response = accounting_client.get(reverse("fiduciary:payment_list"), {"assignment_number": "EF-ABONO"})
    content = response.content.decode()

    assert response.status_code == 200
    assert "<th>Tipo</th>" not in content
    assert "Adicion" not in content


@pytest.mark.django_db
def test_assignment_detail_shows_only_monetary_cession_transfer_in_payments_table(
    accounting_client,
    accounting_admin_user,
    active_client,
    unit,
):
    create_ownership(active_client, unit, True)
    assignment = create_assignment(unit, active_client, "EF-MOV")
    _, imported_file = create_imported_file(accounting_admin_user)
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2025, 9, 17),
        amount=Decimal("25000000.00"),
        concept="TRASLADO | NC5726",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T5",
        source_row=32,
        source_column="G",
    )
    Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2024, 10, 8),
        amount=Decimal("92136000.00"),
        concept="CREDITO | Recibo NCR5727",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T5",
        source_row=32,
        source_column="CD",
    )
    OperationalNovelty.objects.create(
        property_unit=unit,
        historical_assignment=assignment,
        novelty_type=OperationalNovelty.NoveltyType.HISTORICAL,
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.DESCRIPTIVE,
        effective_date=date(2025, 9, 17),
        summary="*TRASLADO SIN VALOR",
        detail="Fila de novedades T5 fila 202 sin valor monetario.",
        created_by=accounting_admin_user,
    )

    response = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk]))
    content = response.content.decode()
    payments_section = content.split("<h2>Pagos registrados</h2>", 1)[1].split("<h2>Titulares del encargo</h2>", 1)[0]
    novelties_section = content.split("<h2>Novedades relacionadas</h2>", 1)[1]

    assert response.status_code == 200
    assert "TRASLADO | NC5726" in payments_section
    assert "CREDITO | Recibo NCR5727" in payments_section
    assert "*TRASLADO SIN VALOR" not in payments_section
    assert "*TRASLADO SIN VALOR" in novelties_section
    assert "<strong>2</strong><br><span class=\"text-muted\">Cantidad</span>" in payments_section
    assert "117.136.000" in payments_section


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
        ("TENTACLES LUCAS", "LUCAS", "TENTACLES"),
        ("WATSON CLEVELAND", "CLEVELAND", "WATSON"),
        ("LEYTON GÓMEZ MARÍA JOSÉ", "MARÍA JOSÉ", "LEYTON GÓMEZ"),
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
def test_manual_observation_requires_assignment(accounting_client, active_client, unit):
    create_ownership(active_client, unit)

    response = accounting_client.post(
        reverse("fiduciary:observation_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "assignment": "",
            "summary": "Seguimiento",
            "detail": "Observacion sin encargo.",
        },
    )

    assert response.status_code == 200
    assert ImportedHistoricalObservation.objects.filter(summary="Seguimiento").count() == 0
    assert "Este campo es obligatorio" in response.content.decode()


@pytest.mark.django_db
def test_manual_observation_update_requires_reason_and_audits(
    accounting_client, accounting_admin_user, active_client, unit
):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client, "EF-OBS-EDIT")
    observation = ImportedHistoricalObservation.objects.create(
        project=unit.project,
        property_unit=unit,
        assignment=assignment,
        origin=ImportedHistoricalObservation.Origin.MANUAL,
        summary="Seguimiento",
        detail="Detalle inicial.",
        imported_by=accounting_admin_user,
    )

    missing = accounting_client.post(
        reverse("fiduciary:observation_update", args=[observation.pk]),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "assignment": assignment.pk,
            "summary": "Seguimiento",
            "detail": "Detalle editado.",
            "change_reason": "   ",
        },
    )
    observation.refresh_from_db()
    assert missing.status_code == 200
    assert observation.detail == "Detalle inicial."

    saved = accounting_client.post(
        reverse("fiduciary:observation_update", args=[observation.pk]),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "assignment": assignment.pk,
            "summary": "Seguimiento",
            "detail": "Detalle editado.",
            "change_reason": "Correccion solicitada",
        },
    )

    observation.refresh_from_db()
    assert saved.status_code == 302
    assert observation.detail == "Detalle editado."
    assert LogEntry.objects.filter(
        object_id=str(observation.pk),
        change_message__icontains="Correccion solicitada",
    ).exists()


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
    assert "Coincide con filtros." in content
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
    assert "<th>Detalle</th>" in list_content
    assert "<th>Resumen</th>" not in list_content
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
def test_observation_context_assignment_labels_include_short_primary_holder_or_without_holder(
    accounting_client,
    unit,
):
    client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="789",
        first_names="Santiago Jose",
        last_names_or_company="Leyton Gomez",
        phone="3000000000",
    )
    create_ownership(client, unit)
    assignment = create_assignment(unit, client, "002011062714")
    without_holder = FiduciaryAssignment.objects.create(
        assignment_number="002011062715",
        property_unit=unit,
        is_active=False,
        start_date="2026-01-01",
        end_date="2026-01-02",
        last_change_reason="Registro manual",
    )

    response = accounting_client.get(reverse("fiduciary:observation_context"), {"project": unit.project_id, "unit": unit.pk})
    payload = response.json()

    assert response.status_code == 200
    labels = {row["id"]: row["text"] for row in payload["assignments"]}
    assert labels[assignment.pk] == "002011062714 — Santiago Leyton"
    assert labels[without_holder.pk] == "002011062715 — Sin titular"


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
            "current_assignment": assignment.pk,
            "current_client": active_client.pk,
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
            "current_assignment": current_assignment.pk,
            "current_client": active_client.pk,
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
def test_manual_novelty_requires_client_and_assignment(accounting_client, active_client, unit):
    create_ownership(active_client, unit)
    create_assignment(unit, active_client, "EF-NOV-CTX")

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "novelty_type": OperationalNovelty.NoveltyType.WITHDRAWAL,
            "effective_date": "2026-05-01",
            "summary": "Retiro",
            "detail": "Sin contexto obligatorio.",
        },
    )

    content = response.content.decode()
    assert response.status_code == 200
    assert OperationalNovelty.objects.count() == 0
    assert "Seleccione el encargo fiduciario" in content
    assert "Seleccione el cliente de la novedad" in content


@pytest.mark.django_db
def test_cession_without_new_assignment_returns_form_error_not_500(accounting_client, active_client, secondary_client, unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client, "EF-NOV-MISSING")

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "current_assignment": assignment.pk,
            "current_client": active_client.pk,
            "novelty_type": OperationalNovelty.NoveltyType.CESSION,
            "effective_date": "2026-05-01",
            "new_client": secondary_client.pk,
            "new_assignment_number": "",
            "summary": "Cesion",
            "detail": "Falta encargo nuevo.",
        },
    )

    assert response.status_code == 200
    assert "Registre el nuevo numero de encargo" in response.content.decode()
    assert OperationalNovelty.objects.count() == 0


@pytest.mark.django_db
def test_cession_rejects_current_holder_as_new_client_without_500(accounting_client, active_client, unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client, "EF-NOV-SAME")

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "current_assignment": assignment.pk,
            "current_client": active_client.pk,
            "novelty_type": OperationalNovelty.NoveltyType.CESSION,
            "effective_date": "2026-05-01",
            "new_client": active_client.pk,
            "new_assignment_number": "EF-NOV-SAME-NEW",
            "summary": "Cesion",
            "detail": "Mismo titular.",
        },
    )

    assert response.status_code == 200
    assert "El nuevo titular debe ser diferente al titular actual" in response.content.decode()
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-NOV-SAME-NEW").exists()


@pytest.mark.django_db
def test_client_search_finds_by_partial_document_name_and_email(accounting_client):
    client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="41912336",
        first_names="Gloria Milena",
        last_names_or_company="Loaiza Ortiz",
        email="milena@example.com",
        phone="3148670777",
    )

    by_document = accounting_client.get(reverse("fiduciary:client_search"), {"criterion": "document", "q": "9123"}).json()
    by_name = accounting_client.get(reverse("fiduciary:client_search"), {"criterion": "name", "q": "gloria loaiza"}).json()
    by_email = accounting_client.get(reverse("fiduciary:client_search"), {"criterion": "email", "q": "milena@"}).json()
    excluded = accounting_client.get(
        reverse("fiduciary:client_search"),
        {"criterion": "document", "q": "41912336", "exclude": [client.pk]},
    ).json()

    assert {row["id"] for row in by_document["results"]} == {client.pk}
    assert {row["id"] for row in by_name["results"]} == {client.pk}
    assert {row["id"] for row in by_email["results"]} == {client.pk}
    assert excluded["results"] == []


@pytest.mark.django_db
def test_novelty_assignment_client_search_is_limited_to_selected_assignment(
    accounting_client, active_client, secondary_client, unit, second_unit
):
    other_client = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="900",
        first_names="Cliente",
        last_names_or_company="Externo",
        email="externo@example.com",
        phone="300900",
    )
    create_ownership(active_client, unit)
    create_ownership(secondary_client, unit, False)
    create_ownership(other_client, second_unit)
    assignment = create_assignment(unit, active_client, "EF-NOV-FILTER")
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=secondary_client,
        is_primary=False,
        start_date="2026-01-01",
        last_change_reason="Registro manual",
    )
    create_assignment(second_unit, other_client, "EF-NOV-OTHER")

    response = accounting_client.get(
        reverse("fiduciary:novelty_client_search"),
        {"assignment": assignment.pk, "criterion": "name", "q": "Cliente"},
    )
    rows = response.json()["results"]

    assert response.status_code == 200
    assert {row["id"] for row in rows} == set()

    holder_response = accounting_client.get(
        reverse("fiduciary:novelty_client_search"),
        {"assignment": assignment.pk, "criterion": "document", "q": secondary_client.document_number},
    )
    holder_rows = holder_response.json()["results"]
    assert {row["id"] for row in holder_rows} == {secondary_client.pk}
    assert holder_rows[0]["role"] == "secondary"


@pytest.mark.django_db
def test_novelty_rejects_client_not_related_to_assignment(accounting_client, active_client, secondary_client, unit, second_unit):
    create_ownership(active_client, unit)
    create_ownership(secondary_client, second_unit)
    assignment = create_assignment(unit, active_client, "EF-NOV-REL")

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "current_assignment": assignment.pk,
            "current_client": secondary_client.pk,
            "novelty_type": OperationalNovelty.NoveltyType.WITHDRAWAL,
            "effective_date": "2026-05-01",
            "detail": "Cliente ajeno al encargo.",
        },
    )

    assert response.status_code == 200
    assert "El cliente seleccionado no pertenece al encargo actual" in response.content.decode()
    assert OperationalNovelty.objects.count() == 0


@pytest.mark.django_db
def test_secondary_holder_cannot_use_cession(accounting_client, active_client, secondary_client, unit):
    create_ownership(active_client, unit)
    create_ownership(secondary_client, unit, False)
    assignment = create_assignment(unit, active_client, "EF-NOV-ROLE")
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=secondary_client,
        is_primary=False,
        start_date="2026-01-01",
        last_change_reason="Registro manual",
    )
    replacement = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="901",
        first_names="Nuevo",
        last_names_or_company="Titular",
        phone="300901",
    )

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "current_assignment": assignment.pk,
            "current_client": secondary_client.pk,
            "novelty_type": OperationalNovelty.NoveltyType.CESSION,
            "effective_date": "2026-05-01",
            "new_client": replacement.pk,
            "new_assignment_number": "EF-NOV-ROLE-NEW",
            "detail": "Cesion forzada de secundario.",
        },
    )

    assert response.status_code == 200
    assert "La cesion solo esta permitida para el titular principal" in response.content.decode()
    assert not FiduciaryAssignment.objects.filter(assignment_number="EF-NOV-ROLE-NEW").exists()


@pytest.mark.django_db
def test_principal_holder_cannot_use_exclusion(accounting_client, active_client, unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client, "EF-NOV-PRINCIPAL")

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "current_assignment": assignment.pk,
            "current_client": active_client.pk,
            "novelty_type": OperationalNovelty.NoveltyType.EXCLUSION,
            "effective_date": "2026-05-01",
            "detail": "Exclusion forzada del principal.",
        },
    )

    assert response.status_code == 200
    assert "no esta permitido para el titular principal" in response.content.decode()
    assert OperationalNovelty.objects.count() == 0


@pytest.mark.django_db
def test_manual_historical_import_type_is_rejected(accounting_client, active_client, unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client, "EF-NOV-HIST-MAN")

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "current_assignment": assignment.pk,
            "current_client": active_client.pk,
            "novelty_type": OperationalNovelty.NoveltyType.HISTORICAL,
            "effective_date": "2026-05-01",
            "detail": "Historico manipulado.",
        },
    )

    assert response.status_code == 200
    assert OperationalNovelty.objects.count() == 0


@pytest.mark.django_db
def test_inclusion_adds_new_secondary_without_requiring_existing_secondary(accounting_client, active_client, unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client, "EF-NOV-INCLUSION")
    new_secondary = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="777001",
        first_names="Gloria",
        last_names_or_company="Giraldo",
        email="gloria.inclusion@example.com",
    )

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "current_assignment": assignment.pk,
            "current_client": active_client.pk,
            "novelty_type": "inclusion",
            "effective_date": "2026-05-01",
            "secondary_clients": [new_secondary.pk],
            "detail": "Incluir cliente secundario.",
        },
    )

    assert response.status_code == 302
    assert assignment.holders.filter(client=new_secondary, is_primary=False, is_active=True).exists()
    assert UnitOwnership.objects.filter(client=new_secondary, property_unit=unit, is_primary=False, is_active=True).exists()
    novelty = OperationalNovelty.objects.get(other_type="INCLUSION")
    assert novelty.novelty_type == OperationalNovelty.NoveltyType.OTHER
    assert novelty.previous_client == active_client
    assert novelty.new_client == new_secondary
    assert novelty.new_assignment == assignment


@pytest.mark.django_db
def test_inclusion_rejects_current_primary_and_existing_secondary(accounting_client, active_client, secondary_client, unit):
    create_ownership(active_client, unit)
    create_ownership(secondary_client, unit, False)
    assignment = create_assignment(unit, active_client, "EF-NOV-INCLUSION-DUP")
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=secondary_client,
        is_primary=False,
        start_date="2026-01-01",
        last_change_reason="Registro manual",
    )

    primary_response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "current_assignment": assignment.pk,
            "current_client": active_client.pk,
            "novelty_type": "inclusion",
            "effective_date": "2026-05-01",
            "secondary_clients": [active_client.pk],
            "detail": "Primario repetido.",
        },
    )
    existing_response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "current_assignment": assignment.pk,
            "current_client": active_client.pk,
            "novelty_type": "inclusion",
            "effective_date": "2026-05-01",
            "secondary_clients": [secondary_client.pk],
            "detail": "Secundario repetido.",
        },
    )

    assert primary_response.status_code == 200
    assert "titular principal no puede agregarse como secundario" in primary_response.content.decode()
    assert existing_response.status_code == 200
    assert "ya esta asociado como titular vigente" in existing_response.content.decode()
    assert OperationalNovelty.objects.count() == 0


@pytest.mark.django_db
def test_new_holder_search_is_global_and_excludes_current_holder(accounting_client, active_client, unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client, "EF-NOV-SEARCH-HOLDER")
    external = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="41998736",
        first_names="Gloria Milena",
        last_names_or_company="Loaiza Ortiz",
        email="milena.nueva@example.com",
    )

    by_document = accounting_client.get(
        reverse("fiduciary:novelty_client_search"),
        {"scope": "new_holder", "assignment": assignment.pk, "criterion": "document", "q": "9987", "exclude": [active_client.pk]},
    ).json()
    by_name = accounting_client.get(
        reverse("fiduciary:novelty_client_search"),
        {"scope": "new_holder", "assignment": assignment.pk, "criterion": "name", "q": "gloria loaiza", "exclude": [active_client.pk]},
    ).json()
    by_email = accounting_client.get(
        reverse("fiduciary:novelty_client_search"),
        {"scope": "new_holder", "assignment": assignment.pk, "criterion": "email", "q": "milena.nueva", "exclude": [active_client.pk]},
    ).json()
    current = accounting_client.get(
        reverse("fiduciary:novelty_client_search"),
        {"scope": "new_holder", "assignment": assignment.pk, "criterion": "document", "q": active_client.document_number, "exclude": [active_client.pk]},
    ).json()

    assert {row["id"] for row in by_document["results"]} == {external.pk}
    assert {row["id"] for row in by_name["results"]} == {external.pk}
    assert {row["id"] for row in by_email["results"]} == {external.pk}
    assert current["results"] == []


@pytest.mark.django_db
def test_new_secondary_search_is_global_but_excludes_assignment_holders(
    accounting_client, active_client, secondary_client, unit
):
    create_ownership(active_client, unit)
    create_ownership(secondary_client, unit, False)
    assignment = create_assignment(unit, active_client, "EF-NOV-SEARCH-SEC")
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=secondary_client,
        is_primary=False,
        start_date="2026-01-01",
        last_change_reason="Registro manual",
    )
    candidate = FiduciaryClient.objects.create(
        document_type=FiduciaryClient.DocumentType.CITIZENSHIP_ID,
        document_number="880044",
        first_names="Nuevo",
        last_names_or_company="Secundario",
        email="nuevo.secundario@example.com",
    )

    by_document = accounting_client.get(
        reverse("fiduciary:novelty_client_search"),
        {"scope": "new_secondary", "assignment": assignment.pk, "criterion": "document", "q": "8800"},
    ).json()
    existing = accounting_client.get(
        reverse("fiduciary:novelty_client_search"),
        {"scope": "new_secondary", "assignment": assignment.pk, "criterion": "document", "q": secondary_client.document_number},
    ).json()

    assert {row["id"] for row in by_document["results"]} == {candidate.pk}
    assert existing["results"] == []


@pytest.mark.django_db
def test_novelty_form_javascript_contains_role_matrix_and_cleanup(accounting_client, active_client, unit):
    create_ownership(active_client, unit)
    create_assignment(unit, active_client, "EF-NOV-JS")

    response = accounting_client.get(reverse("fiduciary:novelty_create"))
    content = response.content.decode()

    assert 'role === "primary" && item.value === "exclusion"' in content
    assert 'role === "secondary" && (item.value === "cession" || item.value === "inclusion")' in content
    assert 'scope: "new_holder"' in content
    assert 'scope: "new_secondary"' in content
    assert "clearClientPicker" in content
    assert 'event.key === "Enter"' in content
    assert "event.preventDefault()" in content


@pytest.mark.django_db
def test_manual_novelty_requires_detail_and_allows_blank_summary(accounting_client, active_client, unit):
    create_ownership(active_client, unit)
    assignment = create_assignment(unit, active_client, "EF-NOV-DETAIL-REQ")

    missing = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "current_assignment": assignment.pk,
            "current_client": active_client.pk,
            "novelty_type": OperationalNovelty.NoveltyType.OTHER,
            "other_type": "ACLARACION",
            "summary": "Resumen sin detalle",
            "detail": "   ",
        },
    )
    assert missing.status_code == 200
    assert "Registre el detalle de la novedad" in missing.content.decode()
    assert OperationalNovelty.objects.count() == 0

    valid = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "current_assignment": assignment.pk,
            "current_client": active_client.pk,
            "novelty_type": OperationalNovelty.NoveltyType.OTHER,
            "other_type": "ACLARACION",
            "summary": "",
            "detail": "Detalle suficiente.",
        },
    )

    novelty = OperationalNovelty.objects.get()
    assert valid.status_code == 302
    assert novelty.summary == ""
    assert novelty.detail == "Detalle suficiente."


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
    assignment = create_assignment(unit, active_client, "EF-NOV-OLD")

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "current_assignment": assignment.pk,
            "current_client": active_client.pk,
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
    assignment = create_assignment(unit, active_client, "EF-OTHER")

    response = accounting_client.post(
        reverse("fiduciary:novelty_create"),
        {
            "project": unit.project_id,
            "property_unit": unit.pk,
            "current_assignment": assignment.pk,
            "current_client": active_client.pk,
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
