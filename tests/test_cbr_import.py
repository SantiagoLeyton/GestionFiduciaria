from __future__ import annotations

from datetime import date
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from django.urls import reverse

from core.models import AuditEvent
from fiduciary.imports.cbr import analyze_cbr_import, finalize_cbr_import
from fiduciary.models import (
    AssignmentCreditSubsidy,
    AssignmentInterest,
    AssignmentLegalDocumentation,
    CBRImportRow,
    FiduciaryAssignment,
    ImportBatch,
    ImportRowIssue,
    Payment,
)
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup


HEADERS = [
    "ENCARGO",
    "FECHA PAGO CREDITO",
    "ENTIDAD CR",
    "VALOR CR",
    "FECHA SUBSIDIO CAJA",
    "ENTIDAD SB CAJA",
    "VALOR SB CAJA",
    "FECHA SUBSIDIO GOB",
    "ENTIDAD SB GOB",
    "VALOR SB GOB",
    "FECHA CONTRATO DE ADHESION",
    "FECHA DE PROMESA",
    "FECHA ENTREGA SEGÚN PROMESA",
    "ENTREGA REAL",
    "MATRICULA",
    "FECHA ESCRITURA",
    "ESCRITURA",
    "NOTARÍA",
    "FECHA C/TRADIC",
    "FACT. ELECTRÓNICA",
    "RECIBO INTERESES",
    "FECHA INTERESES",
    "VALOR INTERESES",
]

def _col_letter(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _excel_serial(value: date) -> int:
    return (datetime(value.year, value.month, value.day) - datetime(1899, 12, 30)).days


def _write_xlsx(
    path: Path,
    data_rows: list[list[object]],
    *,
    sheet_target: str = "worksheets/sheet1.xml",
    date_style_columns: set[int] | None = None,
) -> None:
    date_style_columns = date_style_columns or set()
    rows = [["CBR"], [], HEADERS, *data_rows]
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row, start=1):
            if value is None:
                continue
            coord = f"{_col_letter(column_index)}{row_index}"
            style = ' s="1"' if column_index in date_style_columns else ""
            if isinstance(value, int | float | Decimal):
                cells.append(f'<c r="{coord}"{style}><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{coord}"{style} t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(sheet_rows)}</sheetData>"
        "</worksheet>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="CBR" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="{sheet_target}"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/styles.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs>'
            "</styleSheet>",
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def _row(
    assignment="60002006511016",
    credit_date="10/02/2026",
    credit_entity="BANCO X",
    credit_amount="100.000.000",
    box_date="15/02/2026",
    box_entity="COMFENALCO",
    box_amount="10.000.000",
    gov_date="20/02/2026",
    gov_entity="GOBIERNO",
    gov_amount="20.000.000",
    adhesion_date="01/01/2026",
    promise_date="02/01/2026",
    promised_delivery_date="03/01/2026",
    actual_delivery_date="04/01/2026",
    registration="50N-123",
    deed_date="05/01/2026",
    deed_number="777",
    notary="NOTARIA 1",
    tradition_date="06/01/2026",
    invoice="FE-001",
    interest_receipt="INT-1",
    interest_date="07/01/2026",
    interest_amount="500.000",
):
    return [
        assignment,
        credit_date,
        credit_entity,
        credit_amount,
        box_date,
        box_entity,
        box_amount,
        gov_date,
        gov_entity,
        gov_amount,
        adhesion_date,
        promise_date,
        promised_delivery_date,
        actual_delivery_date,
        registration,
        deed_date,
        deed_number,
        notary,
        tradition_date,
        invoice,
        interest_receipt,
        interest_date,
        interest_amount,
    ]


@pytest.fixture
def assignment(db):
    project = Project.objects.create(code="MTC", name="Montecielo")
    grouping_type = GroupingType.objects.create(code="TORRE", name="Torre")
    group = StructuralGroup.objects.create(code="T1", name="T1", project=project, grouping_type=grouping_type)
    unit = PropertyUnit.objects.create(
        code="101",
        name="101",
        project=project,
        structural_group=group,
        property_value=Decimal("200000000"),
    )
    return FiduciaryAssignment.objects.create(
        assignment_number="60002006511016",
        property_unit=unit,
        start_date=date(2026, 1, 1),
    )


def _batch(user):
    return ImportBatch.objects.create(
        import_type=ImportBatch.ImportType.CBR,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
        initiated_by=user,
    )


@pytest.mark.django_db
def test_cbr_analyzes_row_3_headers_and_starts_data_on_row_4(tmp_path, accounting_admin_user, assignment):
    path = tmp_path / "cbr.xlsx"
    _write_xlsx(path, [_row()])

    result = analyze_cbr_import(batch=_batch(accounting_admin_user), file_path=path)

    result.batch.refresh_from_db()
    assert result.batch.status == ImportBatch.Status.READY
    row = CBRImportRow.objects.get(batch=result.batch)
    assert row.row_number == 4
    assert row.normalized_assignment_number == "60002006511016"
    assert ImportRowIssue.objects.filter(imported_file=result.imported_file).count() == 0


@pytest.mark.django_db
def test_cbr_reads_workbooks_with_absolute_sheet_targets(tmp_path, accounting_admin_user, assignment):
    path = tmp_path / "cbr_absolute_target.xlsx"
    _write_xlsx(path, [_row()], sheet_target="/xl/worksheets/sheet1.xml")

    result = analyze_cbr_import(batch=_batch(accounting_admin_user), file_path=path)

    row = CBRImportRow.objects.get(batch=result.batch)
    assert result.rows_created == 1
    assert row.row_number == 4
    assert row.status == CBRImportRow.Status.VALID


@pytest.mark.django_db
def test_cbr_finalize_applies_data_without_creating_payments(tmp_path, accounting_admin_user, assignment):
    path = tmp_path / "cbr.xlsx"
    _write_xlsx(path, [_row()])
    batch = _batch(accounting_admin_user)

    analyze_cbr_import(batch=batch, file_path=path)
    result = finalize_cbr_import(batch_id=batch.pk, user=accounting_admin_user)

    assignment.refresh_from_db()
    assert result.credit_subsidies_created == 3
    assert result.interests_created == 1
    assert result.contract_fields_updated == 4
    assert result.legal_fields_updated == 6
    assert Payment.objects.count() == 0
    assert AssignmentCreditSubsidy.objects.filter(assignment=assignment).count() == 3
    assert AssignmentInterest.objects.filter(assignment=assignment, receipt="INT-1", interest="2026-01-07").exists()
    assert assignment.adhesion_contract_date == date(2026, 1, 1)
    legal = assignment.legal_documentation
    assert legal.registration_number == "50N-123"
    assert legal.electronic_invoice == "FE-001"
    assert AuditEvent.objects.filter(action="Importado", entity="CBR").count() == 1


@pytest.mark.django_db
def test_cbr_real_shape_preserves_assignment_zeroes_and_numeric_fields(tmp_path, accounting_admin_user, assignment):
    assignment.assignment_number = "002010388888"
    assignment.save(update_fields=["assignment_number", "updated_at"])
    path = tmp_path / "cbr_real_shape.xlsx"
    _write_xlsx(
        path,
        [
            _row(
                assignment="002010388888",
                credit_date=_excel_serial(date(2026, 4, 5)),
                credit_entity="BANCO CAJA SOCIAL",
                credit_amount=115585812,
                box_date=None,
                box_entity=None,
                box_amount=None,
                gov_date=_excel_serial(date(2026, 5, 6)),
                gov_entity="ENTIDAD GOB",
                gov_amount=14490102,
                adhesion_date=_excel_serial(date(2026, 1, 10)),
                promise_date=_excel_serial(date(2026, 2, 11)),
                promised_delivery_date=_excel_serial(date(2026, 3, 12)),
                actual_delivery_date=None,
                registration="50N-ABC",
                deed_date=_excel_serial(date(2026, 6, 7)),
                deed_number=5953,
                notary=3,
                tradition_date=_excel_serial(date(2026, 7, 8)),
                invoice="FE842220",
                interest_receipt="NCR498",
                interest_date="ABR.05/26",
                interest_amount=338915,
            )
        ],
        date_style_columns={23},
    )

    batch = _batch(accounting_admin_user)
    analyze_cbr_import(batch=batch, file_path=path)
    row = CBRImportRow.objects.get(batch=batch)
    result = finalize_cbr_import(batch_id=batch.pk, user=accounting_admin_user)
    assignment.refresh_from_db()

    assert row.row_number == 4
    assert row.normalized_assignment_number == "002010388888"
    assert result.credit_subsidies_created == 2
    assert result.interests_created == 1
    assert assignment.adhesion_contract_date == date(2026, 1, 10)
    assert assignment.promise_date == date(2026, 2, 11)
    assert assignment.promised_delivery_date == date(2026, 3, 12)
    legal = assignment.legal_documentation
    assert legal.deed_number == "5953"
    assert legal.notary == "3"
    assert legal.electronic_invoice == "FE842220"
    assert AssignmentInterest.objects.filter(assignment=assignment, receipt="NCR498", interest="2026-04-05", amount=Decimal("338915")).exists()


@pytest.mark.django_db
def test_cbr_interest_historical_text_date_october(tmp_path, accounting_admin_user, assignment):
    path = tmp_path / "cbr_interest_october.xlsx"
    _write_xlsx(
        path,
        [
            _row(
                credit_date=None,
                credit_entity=None,
                credit_amount=None,
                box_date=None,
                box_entity=None,
                box_amount=None,
                gov_date=None,
                gov_entity=None,
                gov_amount=None,
                adhesion_date=None,
                promise_date=None,
                promised_delivery_date=None,
                actual_delivery_date=None,
                registration=None,
                deed_date=None,
                deed_number=None,
                notary=None,
                tradition_date=None,
                invoice=None,
                interest_receipt="NCR-OCT",
                interest_date="OCT.22/24",
                interest_amount=1000,
            )
        ],
    )

    batch = _batch(accounting_admin_user)
    analyze_cbr_import(batch=batch, file_path=path)
    finalize_cbr_import(batch_id=batch.pk, user=accounting_admin_user)

    assert AssignmentInterest.objects.filter(assignment=assignment, receipt="NCR-OCT", interest="2024-10-22", amount=Decimal("1000")).exists()


@pytest.mark.django_db
def test_cbr_finalize_is_idempotent_and_updates_existing_type_record(tmp_path, accounting_admin_user, assignment):
    first = tmp_path / "cbr1.xlsx"
    _write_xlsx(first, [_row()])
    batch = _batch(accounting_admin_user)
    analyze_cbr_import(batch=batch, file_path=first)
    finalize_cbr_import(batch_id=batch.pk, user=accounting_admin_user)

    repeat_batch = _batch(accounting_admin_user)
    analyze_cbr_import(batch=repeat_batch, file_path=first)
    repeated = finalize_cbr_import(batch_id=repeat_batch.pk, user=accounting_admin_user)
    assert repeated.credit_subsidies_created == 0
    assert repeated.credit_subsidies_reused == 3
    assert repeated.interests_created == 0
    assert repeated.interests_reused == 1

    second = tmp_path / "cbr2.xlsx"
    _write_xlsx(second, [_row(credit_amount="101.000.000")])
    second_batch = _batch(accounting_admin_user)
    analyze_cbr_import(batch=second_batch, file_path=second)
    changed = finalize_cbr_import(batch_id=second_batch.pk, user=accounting_admin_user)
    assert changed.credit_subsidies_created == 0
    assert changed.credit_subsidies_reused == 3
    assert AssignmentCreditSubsidy.objects.filter(assignment=assignment).count() == 3
    assert AssignmentCreditSubsidy.objects.get(
        assignment=assignment,
        entry_type=AssignmentCreditSubsidy.EntryType.CREDIT,
    ).amount == Decimal("101000000")


@pytest.mark.django_db
def test_cbr_compound_partial_block_generates_blocking_issue(tmp_path, accounting_admin_user, assignment):
    path = tmp_path / "cbr_partial.xlsx"
    _write_xlsx(path, [_row(box_amount=None)])

    result = analyze_cbr_import(batch=_batch(accounting_admin_user), file_path=path)

    result.batch.refresh_from_db()
    assert result.batch.status == ImportBatch.Status.AWAITING_RESOLUTION
    issue = ImportRowIssue.objects.get(code="CBR_INCOMPLETE_BLOCK")
    assert "Subsidio de Caja" in issue.message
    assert CBRImportRow.objects.get(batch=result.batch).status == CBRImportRow.Status.BLOCKED


@pytest.mark.django_db
def test_cbr_unknown_assignment_is_blocking_and_does_not_create_assignment(tmp_path, accounting_admin_user):
    path = tmp_path / "cbr_unknown.xlsx"
    _write_xlsx(path, [_row(assignment="999")])

    result = analyze_cbr_import(batch=_batch(accounting_admin_user), file_path=path)

    result.batch.refresh_from_db()
    assert result.batch.status == ImportBatch.Status.AWAITING_RESOLUTION
    assert FiduciaryAssignment.objects.filter(assignment_number="999").count() == 0
    assert ImportRowIssue.objects.filter(code="CBR_ASSIGNMENT_NOT_FOUND").exists()


@pytest.mark.django_db
def test_cbr_empty_cells_do_not_clear_existing_data(tmp_path, accounting_admin_user, assignment):
    assignment.adhesion_contract_date = date(2026, 2, 1)
    assignment.save(update_fields=["adhesion_contract_date", "updated_at"])
    AssignmentLegalDocumentation.objects.create(
        assignment=assignment,
        registration_number="50N-OLD",
        electronic_invoice="FE-OLD",
    )
    path = tmp_path / "cbr_blank.xlsx"
    _write_xlsx(
        path,
        [
            _row(
                credit_date=None,
                credit_entity=None,
                credit_amount=None,
                box_date=None,
                box_entity=None,
                box_amount=None,
                gov_date=None,
                gov_entity=None,
                gov_amount=None,
                adhesion_date=None,
                registration=None,
                invoice="FE-NEW",
                interest_receipt=None,
                interest_date=None,
                interest_amount=None,
            )
        ],
    )

    batch = _batch(accounting_admin_user)
    analyze_cbr_import(batch=batch, file_path=path)
    finalize_cbr_import(batch_id=batch.pk, user=accounting_admin_user)

    assignment.refresh_from_db()
    legal = assignment.legal_documentation
    assert assignment.adhesion_contract_date == date(2026, 2, 1)
    assert legal.registration_number == "50N-OLD"
    assert legal.electronic_invoice == "FE-NEW"


@pytest.mark.django_db
def test_cbr_reuses_interest_already_imported_from_historical_source(tmp_path, accounting_admin_user, assignment):
    AssignmentInterest.objects.create(
        assignment=assignment,
        receipt="INT-1",
        interest="2026-01-07",
        amount=Decimal("500000"),
    )
    path = tmp_path / "cbr_interest_repeated.xlsx"
    _write_xlsx(path, [_row()])
    batch = _batch(accounting_admin_user)

    analyze_cbr_import(batch=batch, file_path=path)
    result = finalize_cbr_import(batch_id=batch.pk, user=accounting_admin_user)

    assert result.interests_created == 0
    assert result.interests_reused == 1
    assert AssignmentInterest.objects.filter(assignment=assignment, receipt="INT-1", interest="2026-01-07", amount=Decimal("500000")).count() == 1


@pytest.mark.django_db
def test_commercial_user_can_apply_cbr(tmp_path, commercial_user, accounting_admin_user, assignment):
    path = tmp_path / "cbr.xlsx"
    _write_xlsx(path, [_row()])
    batch = _batch(accounting_admin_user)
    analyze_cbr_import(batch=batch, file_path=path)

    result = finalize_cbr_import(batch_id=batch.pk, user=commercial_user)

    assert result.affected_assignments == 1


@pytest.mark.django_db
def test_cbr_upload_view_allows_commercial_permission(client, commercial_user):
    client.force_login(commercial_user)
    response = client.get(reverse("fiduciary:cbr_import_create"))
    assert response.status_code == 200
