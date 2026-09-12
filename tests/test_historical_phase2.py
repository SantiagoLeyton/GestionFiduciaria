from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from fiduciary.imports.historical import HistoricalWorkbookParser, analyze_historical_import
from fiduciary.imports.historical.data import CellData, ReconstructedHistoricalPayment
from fiduciary.imports.historical.parser import (
    RECEIPT_CATEGORY_CREDIT,
    RECEIPT_CATEGORY_ORDINARY,
    RECEIPT_CATEGORY_SUBSIDY,
    RECEIPT_CATEGORY_TRANSFER,
    RECEIPT_CATEGORY_CESSION,
    classify_receipt,
    _historical_payment_date_year_month,
    _split_historical_date_values,
)
from fiduciary.imports.historical.readers import RawSheet, RawWorkbook
from fiduciary.imports.historical.readiness import can_finalize_historical_import_batch
from fiduciary.imports.historical.finalize import _FinalizationContext
from fiduciary.imports.historical.normalize import normalize_text
from fiduciary.imports.historical.resolutions import equivalent_pending_elements
from fiduciary.models import (
    Client,
    DetectedStructureElement,
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportBatch,
    ImportedFile,
    ImportedHistoricalNovelty,
    ImportedHistoricalObservation,
    ImportedSheetResult,
    ImportResolution,
    ImportRowIssue,
    OperationalNovelty,
    Payment,
    UnitOwnership,
)
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup


pytestmark = pytest.mark.django_db


@pytest.fixture
def accounting_client(client, accounting_admin_user):
    client.force_login(accounting_admin_user)
    return client


def phase2_sheet(
    name="T1",
    *,
    row=5,
    unit="101",
    assignment="EF-101",
    client_name="JUAN PEREZ",
    document="1",
    receipts="R1-R2",
    receipts_header="RECIBOS",
    fidubogota_receipts=None,
    fidubogota_receipts_header="RECIBOS FIDUBOGOTA",
    dates="ENE.1/26F-ENE.2/26F",
    credit_dates=None,
    subsidy_dates=None,
    payment=1000,
    credit_value=None,
    subsidy_compensation_value=None,
    subsidy_government_value=None,
    assignment_changes_value=None,
):
    cells = {
        (1, 1): CellData(1, 1, "A", "A1", f"Proyecto Prueba - {name}"),
        (4, 1): CellData(4, 1, "A", "A4", "ENCARGO FIDUCIARIO"),
        (4, 2): CellData(4, 2, "B", "B4", "APTO"),
        (4, 3): CellData(4, 3, "C", "C4", "CEDULA CLIENTE"),
        (4, 4): CellData(4, 4, "D", "D4", "NOMBRE CLIENTE"),
        (4, 6): CellData(4, 6, "F", "F4", "FECHA"),
        (4, 7): CellData(4, 7, "G", "G4", "RECIBO FIDUCIA ENE/2026"),
        (4, 8): CellData(4, 8, "H", "H4", "FECHA PAGO CREDITO"),
        (4, 9): CellData(4, 9, "I", "I4", "FECHA PAGO SUBSIDIO"),
        (row, 1): CellData(row, 1, "A", f"A{row}", assignment),
        (row, 2): CellData(row, 2, "B", f"B{row}", unit),
        (row, 3): CellData(row, 3, "C", f"C{row}", document),
        (row, 4): CellData(row, 4, "D", f"D{row}", client_name),
        (row, 6): CellData(row, 6, "F", f"F{row}", dates),
        (row, 7): CellData(row, 7, "G", f"G{row}", payment),
        (row, 8): CellData(row, 8, "H", f"H{row}", credit_dates),
        (row, 9): CellData(row, 9, "I", f"I{row}", subsidy_dates),
    }
    if receipts_header is not None:
        cells[(4, 5)] = CellData(4, 5, "E", "E4", receipts_header)
        cells[(row, 5)] = CellData(row, 5, "E", f"E{row}", receipts)
    if fidubogota_receipts is not None:
        cells[(4, 10)] = CellData(4, 10, "J", "J4", fidubogota_receipts_header)
        cells[(row, 10)] = CellData(row, 10, "J", f"J{row}", fidubogota_receipts)
    if credit_value is not None:
        cells[(4, 11)] = CellData(4, 11, "K", "K4", "ABONOS CR CONSTRUCTOR")
        cells[(row, 11)] = CellData(row, 11, "K", f"K{row}", credit_value)
    if subsidy_compensation_value is not None:
        cells[(4, 12)] = CellData(4, 12, "L", "L4", "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION")
        cells[(row, 12)] = CellData(row, 12, "L", f"L{row}", subsidy_compensation_value)
    if subsidy_government_value is not None:
        cells[(4, 13)] = CellData(4, 13, "M", "M4", "DESEMBOLSO SUBSIDIOS GOBIERNO")
        cells[(row, 13)] = CellData(row, 13, "M", f"M{row}", subsidy_government_value)
    if assignment_changes_value is not None:
        cells[(4, 14)] = CellData(4, 14, "N", "N4", "CESIONES/TRASLADOS")
        cells[(row, 14)] = CellData(row, 14, "N", f"N{row}", assignment_changes_value)
    return RawSheet(name, 1, "visible", "A1:N5", cells, set(), set())


def montecielo_payment_sheet(
    *,
    receipts="",
    fidubogota_receipts="",
    dates="",
    received=None,
    received_formula=None,
    fiduciary_value=None,
    fiduciary_formula=None,
    assignment_changes_value=None,
    credit_dates=None,
    credit_value=None,
    subsidy_dates=None,
    subsidy_compensation_value=None,
    subsidy_government_value=None,
):
    cells = {
        (1, 1): CellData(1, 1, "A", "A1", "Proyecto Montecielo - T2"),
        (4, 1): CellData(4, 1, "A", "A4", "ENCARGO FIDUCIARIO"),
        (4, 2): CellData(4, 2, "B", "B4", "APTO"),
        (4, 3): CellData(4, 3, "C", "C4", "CEDULA CLIENTE"),
        (4, 4): CellData(4, 4, "D", "D4", "NOMBRE CLIENTE"),
        (4, 5): CellData(4, 5, "E", "E4", "RECIBOS"),
        (4, 6): CellData(4, 6, "F", "F4", "RECIBOS FIDUBOGOTA"),
        (4, 7): CellData(4, 7, "G", "G4", "FECHA"),
        (4, 8): CellData(4, 8, "H", "H4", "RECIBIDO"),
        (4, 9): CellData(4, 9, "I", "I4", "CESIONES/TRASLADOS"),
        (4, 10): CellData(4, 10, "J", "J4", "RECIBO FIDUCIA ENE/2026"),
        (4, 11): CellData(4, 11, "K", "K4", "FECHA PAGO CREDITO"),
        (4, 12): CellData(4, 12, "L", "L4", "ABONOS CR CONSTRUCTOR"),
        (4, 13): CellData(4, 13, "M", "M4", "FECHA PAGO SUBSIDIO"),
        (4, 14): CellData(4, 14, "N", "N4", "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION"),
        (4, 15): CellData(4, 15, "O", "O4", "DESEMBOLSO SUBSIDIOS GOBIERNO"),
        (5, 1): CellData(5, 1, "A", "A5", "EF-101"),
        (5, 2): CellData(5, 2, "B", "B5", "101"),
        (5, 3): CellData(5, 3, "C", "C5", "1"),
        (5, 4): CellData(5, 4, "D", "D5", "JUAN PEREZ"),
        (5, 5): CellData(5, 5, "E", "E5", receipts),
        (5, 6): CellData(5, 6, "F", "F5", fidubogota_receipts),
        (5, 7): CellData(5, 7, "G", "G5", dates),
        (5, 8): CellData(5, 8, "H", "H5", received),
        (5, 9): CellData(5, 9, "I", "I5", assignment_changes_value),
        (5, 10): CellData(5, 10, "J", "J5", fiduciary_value),
        (5, 11): CellData(5, 11, "K", "K5", credit_dates),
        (5, 12): CellData(5, 12, "L", "L5", credit_value),
        (5, 13): CellData(5, 13, "M", "M5", subsidy_dates),
        (5, 14): CellData(5, 14, "N", "N5", subsidy_compensation_value),
        (5, 15): CellData(5, 15, "O", "O5", subsidy_government_value),
    }
    if received_formula:
        cells[(5, 8)] = CellData(5, 8, "H", "H5", received, formula=received_formula, has_cached_value=True)
    if fiduciary_formula:
        cells[(5, 10)] = CellData(5, 10, "J", "J5", fiduciary_value, formula=fiduciary_formula, has_cached_value=True)
    return RawSheet("T2", 1, "visible", "A1:O5", cells, set(), set())


def unit_only_sheet(name="T1", rows=None):
    rows = rows or [(5, "101", "55.20", "192172500")]
    cells = {
        (1, 1): CellData(1, 1, "A", "A1", f"Proyecto Montecielo - {name}"),
        (4, 1): CellData(4, 1, "A", "A4", "ENCARGO FIDUCIARIO"),
        (4, 2): CellData(4, 2, "B", "B4", "APTO"),
        (4, 3): CellData(4, 3, "C", "C4", "AREA"),
        (4, 4): CellData(4, 4, "D", "D4", "VALOR INMUEBLE"),
        (4, 5): CellData(4, 5, "E", "E4", "OBSERVACIONES"),
        (4, 6): CellData(4, 6, "F", "F4", "NOMBRE CLIENTE"),
        (4, 7): CellData(4, 7, "G", "G4", "CEDULA CLIENTE"),
        (4, 8): CellData(4, 8, "H", "H4", "FECHA"),
        (4, 9): CellData(4, 9, "I", "I4", "RECIBOS"),
    }
    for row, unit, area, property_value in rows:
        cells[(row, 2)] = CellData(row, 2, "B", f"B{row}", unit)
        cells[(row, 3)] = CellData(row, 3, "C", f"C{row}", area)
        cells[(row, 4)] = CellData(row, 4, "D", f"D{row}", property_value)
        cells[(row, 5)] = CellData(row, 5, "E", f"E{row}", "Observacion sin encargo")
    return RawSheet(name, 1, "visible", f"A1:I{max(row for row, *_ in rows)}", cells, set(), set())


def _with_fiduciary_reference(sheet, *, row=5, assignment="EF-REF", client_name="CLIENTE REFERENCIA", document="999"):
    cells = dict(sheet.cells)
    cells[(row, 1)] = CellData(row, 1, "A", f"A{row}", assignment)
    cells[(row, 6)] = CellData(row, 6, "F", f"F{row}", client_name)
    cells[(row, 7)] = CellData(row, 7, "G", f"G{row}", document)
    return RawSheet(sheet.name, sheet.index, sheet.visibility, sheet.dimension, cells, sheet.hidden_columns, sheet.hidden_rows)


@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        ("NCR1631", RECEIPT_CATEGORY_ORDINARY),
        ("RCA704CR", RECEIPT_CATEGORY_CREDIT),
        ("NCR2051SUB", RECEIPT_CATEGORY_SUBSIDY),
        ("NCR101SB", RECEIPT_CATEGORY_SUBSIDY),
        ("rca704cr", RECEIPT_CATEGORY_CREDIT),
        ("RCA704 CR", RECEIPT_CATEGORY_CREDIT),
        ("RCA704-CR", RECEIPT_CATEGORY_CREDIT),
        ("NC14119TRASLADO", RECEIPT_CATEGORY_TRANSFER),
        ("NC14131CESION", RECEIPT_CATEGORY_CESSION),
        ("ACREDITADO123", RECEIPT_CATEGORY_ORDINARY),
        ("SUBASTA123", RECEIPT_CATEGORY_ORDINARY),
    ],
)
def test_receipt_classification_is_strict_and_normalized(receipt, expected):
    assert classify_receipt(receipt) == expected


def test_receipts_column_sources_are_optional_and_can_be_combined():
    only_standard = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR1-NCR2", dates="ENE.1/26F-ENE.2/26F")
    )
    only_fidubogota = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(
            receipts_header=None,
            fidubogota_receipts="NCR1-NCR2",
            fidubogota_receipts_header="RECIBOS FIDUBOGOTÁ",
            dates="ENE.1/26F-ENE.2/26F",
        )
    )
    combined = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR1", fidubogota_receipts="NCR2", dates="ENE.1/26-ENE.2/26F")
    )

    assert "receipt_numbers" in only_standard.columns
    assert "fidubogota_receipt_numbers" in only_fidubogota.columns
    assert only_fidubogota.columns["fidubogota_receipt_numbers"].header == "RECIBOS FIDUBOGOTÁ"
    assert "HIST_PAYMENT_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in only_standard.issues}
    assert "HIST_PAYMENT_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in only_fidubogota.issues}
    assert "HIST_PAYMENT_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in combined.issues}


def test_receipt_tokens_preserve_source_column_and_order():
    parser = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))
    sheet = phase2_sheet(receipts="NCR1-NCR2CR", fidubogota_receipts="NCR3SB")
    parsed = parser._parse_sheet(sheet)

    tokens = parser._receipt_tokens(sheet, 5, [parsed.columns["receipt_numbers"], parsed.columns["fidubogota_receipt_numbers"]])

    assert [(token.raw_value, token.source, token.source_column, token.source_order, token.order, token.category) for token in tokens] == [
        ("NCR1", "receipt_numbers", "E", 1, 1, RECEIPT_CATEGORY_ORDINARY),
        ("NCR2CR", "receipt_numbers", "E", 2, 2, RECEIPT_CATEGORY_CREDIT),
        ("NCR3SB", "fidubogota_receipt_numbers", "J", 1, 3, RECEIPT_CATEGORY_SUBSIDY),
    ]


def test_missing_receipt_columns_with_dates_creates_ordinary_mismatch():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts_header=None, dates="ENE.1/26F")
    )

    issue = next(issue for issue in parsed.issues if issue.code == "HIST_PAYMENT_DATE_RECEIPT_MISMATCH")
    assert issue.severity == "blocking"
    assert issue.found_value == "1 fechas ordinarias / 0 recibos ordinarios"


def test_receipts_and_dates_with_same_count_do_not_create_issue():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(phase2_sheet())

    assert parsed.classification == "processable"
    assert not any(issue.code == "HIST_PAYMENT_DATE_RECEIPT_MISMATCH" for issue in parsed.issues)


def test_receipts_and_dates_with_different_count_create_blocking_issue():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(unit="1503", receipts="R1-R2-R3", dates="ENE.1/26F-ENE.2/26F")
    )

    issue = next(issue for issue in parsed.issues if issue.code == "HIST_PAYMENT_DATE_RECEIPT_MISMATCH")
    assert issue.severity == "blocking"
    assert issue.unit_code == "1503"
    assert issue.field_name == "FECHA / RECIBOS"
    assert issue.extra_data["date_count"] == 2
    assert issue.extra_data["receipt_count"] == 3
    assert issue.extra_data["scope"] == "cell"
    assert issue.found_value == "2 fechas ordinarias / 3 recibos ordinarios"


def test_special_receipts_do_not_create_false_ordinary_mismatch():
    dates = "-".join(f"ENE.{day}/26F" for day in range(1, 16))
    receipts = "-".join([*(f"NCR{day:04}" for day in range(1, 16)), "RCA704CR", "NCR2051SUB"])
    parsed = HistoricalWorkbookParser(Path("LIBRO Mediterraneo.xlsx"))._parse_sheet(
        phase2_sheet(unit="1503", receipts=receipts, dates=dates, credit_dates="FEB.1/26F", subsidy_dates="MAR.1/26F")
    )

    codes = {issue.code for issue in parsed.issues}
    assert "HIST_PAYMENT_DATE_RECEIPT_MISMATCH" not in codes
    assert "HIST_CREDIT_DATE_RECEIPT_MISMATCH" not in codes
    assert "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH" not in codes


def test_credit_and_subsidy_receipts_are_excluded_from_ordinary_count():
    sheet = phase2_sheet(
        receipts="NCR1001-NCR1002-NCR1003CR-NCR1004SB",
        dates="ENE.1/26F-ENE.2/26F",
        credit_dates="ENE.3/26F",
        subsidy_dates="ENE.4/26F",
        payment=300,
    )
    sheet.cells[(5, 7)] = CellData(5, 7, "G", "G5", 300, formula="=100+200", has_cached_value=True)

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    codes = {issue.code for issue in parsed.issues}
    assert "HIST_PAYMENT_DATE_RECEIPT_MISMATCH" not in codes
    assert "HIST_CREDIT_DATE_RECEIPT_MISMATCH" not in codes
    assert "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH" not in codes
    assert [(payment.receipt, payment.amount) for payment in parsed.rows[0].reconstructed_payments] == [
        ("NCR1001", 100),
        ("NCR1002", 200),
    ]


def test_extra_ordinary_receipt_still_creates_ordinary_mismatch_when_credit_is_excluded():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(
            receipts="NCR1001-NCR1002-NCR1003-NCR1004CR",
            dates="ENE.1/26F-ENE.2/26F",
            credit_dates="ENE.4/26F",
        )
    )

    issue = next(issue for issue in parsed.issues if issue.code == "HIST_PAYMENT_DATE_RECEIPT_MISMATCH")
    assert issue.severity == "blocking"
    assert issue.found_value == "2 fechas ordinarias / 3 recibos ordinarios"


def test_only_final_credit_and_subsidy_suffixes_are_excluded_from_ordinary_count():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(
            receipts="NCR1631-NCR3184CR-NCR3185SB-NCR2051SUB",
            dates="ENE.1/26F",
            credit_dates="ENE.2/26F",
            subsidy_dates="ENE.3/26F-ENE.4/26F",
            payment=100,
        )
    )

    codes = {issue.code for issue in parsed.issues}
    assert classify_receipt("NCR1631") == RECEIPT_CATEGORY_ORDINARY
    assert classify_receipt("NCR3184CR") == RECEIPT_CATEGORY_CREDIT
    assert classify_receipt("NCR3185SB") == RECEIPT_CATEGORY_SUBSIDY
    assert classify_receipt("NCR2051SUB") == RECEIPT_CATEGORY_SUBSIDY
    assert "HIST_PAYMENT_DATE_RECEIPT_MISMATCH" not in codes
    assert [payment.receipt for payment in parsed.rows[0].reconstructed_payments] == ["NCR1631"]


def test_credit_receipts_do_not_validate_against_credit_payment_date():
    valid = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR100-RCA704CR", dates="ENE.1/26F", credit_dates="FEB.1/26F")
    )
    assert "HIST_CREDIT_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in valid.issues}

    missing = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR100-RCA704CR", dates="ENE.1/26F", credit_dates="")
    )
    assert "HIST_CREDIT_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in missing.issues}

    mismatch = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="RCA704CR-RCA705CR", dates="", credit_dates="FEB.1/26F")
    )
    assert "HIST_CREDIT_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in mismatch.issues}

    extra_date = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR100", dates="ENE.1/26F", credit_dates="FEB.1/26F")
    )
    assert "HIST_CREDIT_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in extra_date.issues}

    fidubogota_credit = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(
            receipts_header=None,
            fidubogota_receipts="RCA704CR",
            dates="",
            credit_dates="FEB.1/26F",
        )
    )
    assert "HIST_CREDIT_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in fidubogota_credit.issues}


def test_subsidy_receipts_do_not_validate_against_subsidy_payment_date():
    valid_sub = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR100-NCR2051SUB", dates="ENE.1/26F", subsidy_dates="MAR.1/26F")
    )
    assert "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in valid_sub.issues}

    valid_sb = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR100-NCR2051SB", dates="ENE.1/26F", subsidy_dates="MAR.1/26F")
    )
    assert "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in valid_sb.issues}

    two_subsidies = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR100SUB-NCR101SB", dates="", subsidy_dates="01/03/2025 - 15/04/2025")
    )
    assert "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in two_subsidies.issues}

    mismatch = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR100SUB-NCR101SB", dates="", subsidy_dates="01/03/2025")
    )
    assert "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in mismatch.issues}

    extra_date = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR100", dates="ENE.1/26F", subsidy_dates="MAR.1/26F")
    )
    assert "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in extra_date.issues}

    fidubogota_subsidy = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(
            receipts_header=None,
            fidubogota_receipts="NCR2051SUB",
            dates="",
            subsidy_dates="MAR.1/26F",
        )
    )
    assert "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in fidubogota_subsidy.issues}


def test_excel_iso_date_value_counts_as_single_special_date():
    assert _split_historical_date_values("2025-07-18 00:00:00") == ["2025-07-18 00:00:00"]

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR100-RCA704CR", dates="ENE.1/26F", credit_dates="2025-07-18 00:00:00")
    )

    assert "HIST_CREDIT_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in parsed.issues}


def test_receipt_date_sequences_keep_positional_order_by_category():
    parser = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))
    sheet = phase2_sheet(
        receipts="NCR100-NCR101-RCA704CR-NCR102-NCR2051SUB",
        dates="01/01-02/01-03/01",
        credit_dates="04/01",
        subsidy_dates="05/01",
    )
    parsed = parser._parse_sheet(sheet)

    sequences = parser.receipt_date_sequences(sheet, 5, parsed.columns)

    assert sequences[RECEIPT_CATEGORY_ORDINARY] == [
        ("NCR100", "01/01"),
        ("NCR101", "02/01"),
        ("NCR102", "03/01"),
    ]
    assert sequences[RECEIPT_CATEGORY_CREDIT] == [("RCA704CR", "04/01")]
    assert sequences[RECEIPT_CATEGORY_SUBSIDY] == []


def test_reconstructs_ordinary_payment_from_date_receipt_and_value():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR100", dates="01/01", payment=500)
    )

    payment = parsed.rows[0].reconstructed_payments[0]
    assert isinstance(payment, ReconstructedHistoricalPayment)
    assert payment.category == RECEIPT_CATEGORY_ORDINARY
    assert payment.receipt == "NCR100"
    assert payment.date_value == "01/01"
    assert payment.amount == 500
    assert payment.value_source_column == "G"


def test_montecielo_constructora_payments_use_receipts_dates_without_f_and_received_values():
    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(
        montecielo_payment_sheet(
            receipts="NCR113-NCR152",
            dates="ENE.5/26-FEB.8/26",
            received=3500000,
            received_formula="=1000000+2500000",
        )
    )

    payments = parsed.rows[0].reconstructed_payments

    assert [(payment.receipt, payment.date_value, payment.amount, payment.destination, payment.value_source_header) for payment in payments] == [
        ("NCR113", "ENE.5/26", 1000000, "constructora", "RECIBIDO"),
        ("NCR152", "FEB.8/26", 2500000, "constructora", "RECIBIDO"),
    ]


def test_montecielo_fiduciary_payments_use_fidubogota_receipts_f_dates_and_monthly_values():
    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(
        montecielo_payment_sheet(
            fidubogota_receipts="NCR48-NCR109-RC309",
            dates="ENE.4/26F-ENE.11/26F-ENE.14/26F",
            fiduciary_value=4500000,
            fiduciary_formula="=1000000+2000000+1500000",
        )
    )

    payments = parsed.rows[0].reconstructed_payments

    assert [(payment.receipt, payment.date_value, payment.amount, payment.destination, payment.value_source_header) for payment in payments] == [
        ("NCR48", "ENE.4/26F", 1000000, "fiduciaria", "RECIBO FIDUCIA ENE/2026"),
        ("NCR109", "ENE.11/26F", 2000000, "fiduciaria", "RECIBO FIDUCIA ENE/2026"),
        ("RC309", "ENE.14/26F", 1500000, "fiduciaria", "RECIBO FIDUCIA ENE/2026"),
    ]


def test_unit_rows_with_area_and_property_value_are_parsed_without_client_or_assignment():
    t1 = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(
        _with_fiduciary_reference(
            unit_only_sheet("T1", [(5, "999", "50.00", "100000000"), (6, "101", "55.20", "192172500"), (7, "908", "40.70", "173000000")])
        )
    )
    t2 = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(
        _with_fiduciary_reference(unit_only_sheet("T2", [(5, "999", "50.00", "100000000"), (6, "108", "65.50", "192172500")]))
    )

    rows = [row for row in [*t1.rows, *t2.rows] if row.assignment is None]

    assert [(row.sheet_name, row.unit_code, row.area, row.property_value) for row in rows] == [
        ("T1", "101", Decimal("55.20"), Decimal("192172500.00")),
        ("T1", "908", Decimal("40.70"), Decimal("173000000.00")),
        ("T2", "108", Decimal("65.50"), Decimal("192172500.00")),
    ]
    assert all(row.assignment is None and row.clients == [] for row in rows)


def test_sheet_without_any_client_or_assignment_discards_unit_only_rows():
    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(
        unit_only_sheet("T3", [(5, "101", "55.20", "192172500"), (6, "908", "40.70", "173000000")])
    )

    assert parsed.classification == "skipped_without_fiduciary_data"
    assert parsed.rows == []
    assert parsed.novelties == []
    assert parsed.ignored_row_reasons["sheet_without_fiduciary_data"] == 2


def test_montecielo_fiduciary_same_month_counts_dates_receipts_and_values():
    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(
        montecielo_payment_sheet(
            fidubogota_receipts="NCR1-NCR2",
            dates="ENE.2/26F-ENE.29/26F",
            fiduciary_value=1500000,
            fiduciary_formula="=500000+1000000",
        )
    )

    payments = parsed.rows[0].reconstructed_payments

    assert not any(issue.severity == "blocking" for issue in parsed.issues)
    assert [(payment.receipt, payment.date_value, payment.amount, payment.destination) for payment in payments] == [
        ("NCR1", "ENE.2/26F", 500000, "fiduciaria"),
        ("NCR2", "ENE.29/26F", 1000000, "fiduciaria"),
    ]


@pytest.mark.parametrize("date_value", ["ENE.26/26F", "ENE26/26F"])
def test_montecielo_fiduciary_text_date_normalizes_to_month_year(date_value):
    assert _historical_payment_date_year_month(date_value) == (2026, 1)


def test_montecielo_does_not_mix_standard_and_fidubogota_receipt_sources():
    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(
        montecielo_payment_sheet(
            receipts="NCR113",
            fidubogota_receipts="NCR48",
            dates="ENE.5/26-ENE.4/26F",
            received=1000000,
            fiduciary_value=2000000,
        )
    )

    assert [(payment.receipt, payment.destination, payment.amount) for payment in parsed.rows[0].reconstructed_payments] == [
        ("NCR113", "constructora", 1000000),
        ("NCR48", "fiduciaria", 2000000),
    ]


def test_montecielo_transfer_preserves_receipt_context_and_uses_assignment_changes_value():
    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(
        montecielo_payment_sheet(
            receipts="NC26449TRASL.AP101MTCT1",
            dates="(ABR.8/26TRASL)",
            assignment_changes_value=2500000,
        )
    )

    payment = parsed.rows[0].reconstructed_payments[0]
    assert payment.category == RECEIPT_CATEGORY_TRANSFER
    assert payment.receipt == "NC26449TRASL.AP101MTCT1"
    assert payment.date_value == "(ABR.8/26TRASL)"
    assert payment.amount == 2500000


def test_montecielo_transfer_receipt_without_transfer_date_is_blocking():
    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(
            montecielo_payment_sheet(
                receipts="NC26300TRASL",
                fidubogota_receipts="NCR251-NCR303",
                dates="NOV.12/25-ENE.2/26F-ENE.3/26F",
                fiduciary_value=1700000,
                fiduciary_formula="=800000+900000",
                assignment_changes_value=23950000,
            )
    )

    issue = next(issue for issue in parsed.issues if issue.code == "HIST_TRANSFER_VALUE_RECEIPT_MISMATCH")
    assert issue.severity == "blocking"
    assert issue.extra_data["receipt_count"] == 1
    assert issue.extra_data["payment_pair_count"] == 0
    assert issue.extra_data["value_count"] == 1


def test_montecielo_fiduciary_month_requires_matching_individual_values():
    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(
        montecielo_payment_sheet(
            fidubogota_receipts="NCR1-NCR2",
            dates="ENE.2/26F-ENE.29/26F",
            fiduciary_value=1000,
        )
    )

    issue = next(issue for issue in parsed.issues if issue.code == "HIST_PAYMENT_VALUE_COUNT_MISMATCH")
    assert issue.severity == "blocking"
    assert issue.extra_data["destination"] == "fiduciaria"
    assert issue.extra_data["year"] == 2026
    assert issue.extra_data["month"] == 1
    assert issue.found_value == "1 valores / 2 recibos con fecha"


def test_montecielo_fiduciary_date_does_not_use_value_from_other_month():
    sheet = montecielo_payment_sheet(
        fidubogota_receipts="NCR1",
        dates="ENE.2/26F",
        fiduciary_value=1000,
    )
    sheet.cells[(4, 10)] = CellData(4, 10, "J", "J4", "RECIBO FIDUCIA FEB/2026")

    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(sheet)

    issues = [issue for issue in parsed.issues if issue.code == "HIST_PAYMENT_VALUE_COUNT_MISMATCH"]
    assert any(issue.severity == "blocking" and issue.extra_data["month"] == 1 for issue in issues)
    assert any(issue.severity == "blocking" and issue.extra_data["month"] == 2 for issue in issues)


def test_montecielo_constructora_payment_value_mismatch_is_blocking():
    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(
        montecielo_payment_sheet(
            receipts="NCR1-NCR2",
            dates="ENE.2/26-ENE.29/26",
            received=1000,
        )
    )

    issue = next(issue for issue in parsed.issues if issue.code == "HIST_PAYMENT_VALUE_COUNT_MISMATCH")
    assert issue.severity == "blocking"
    assert issue.extra_data["destination"] == "constructora"
    assert issue.found_value == "1 valores / 2 recibos con fecha"


def test_montecielo_embedded_cession_marker_in_fidubogota_receipts_is_not_ordinary():
    parsed_sheet = montecielo_payment_sheet(
        receipts="NC26448CESION",
        fidubogota_receipts="(ABR.14/26CESION)-NCR513-NCR537",
        dates="ENE.22/26F-ENE.30/26F",
        fiduciary_value=6749907,
        fiduciary_formula="=5749907+1000000",
        assignment_changes_value=0,
    )
    parsed_sheet.cells[(5, 9)] = CellData(5, 9, "I", "I5", 0, formula="=5625000-5625000", has_cached_value=True)
    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(parsed_sheet)

    assert not any(issue.severity == "blocking" for issue in parsed.issues)
    cession = next(payment for payment in parsed.rows[0].reconstructed_payments if payment.category == RECEIPT_CATEGORY_CESSION)
    assert cession.receipt == "NC26448CESION"
    assert cession.date_value == "(ABR.14/26CESION)"
    assert cession.amount == 0
    assert [payment.receipt for payment in parsed.rows[0].reconstructed_payments if payment.destination == "fiduciaria"] == [
        "NCR513",
        "NCR537",
    ]


def test_montecielo_credit_uses_credit_date_and_constructor_credit_value():
    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(
        montecielo_payment_sheet(
            receipts="NCR3184CR",
            dates="",
            credit_dates="MAY.3/26",
            credit_value=8000000,
        )
    )

    payment = parsed.rows[0].reconstructed_payments[0]
    assert payment.category == RECEIPT_CATEGORY_CREDIT
    assert payment.receipt == "NCR3184CR"
    assert payment.date_value == "MAY.3/26"
    assert payment.amount == 8000000
    assert payment.destination == "constructora"


def test_montecielo_subsidy_sources_do_not_invent_ambiguous_payment_values():
    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(
        montecielo_payment_sheet(
            receipts="NCR3185SB-NCR2051SUB",
            dates="",
            subsidy_dates="MAY.4/26-JUN.5/26",
            subsidy_compensation_value=15000000,
            subsidy_government_value=20000000,
        )
    )

    assert parsed.classification == "processable"
    assert not any(payment.category == RECEIPT_CATEGORY_SUBSIDY for payment in parsed.rows[0].reconstructed_payments)
    assert "HIST_SUBSIDY_DATE_SOURCE_AMBIGUOUS" not in {issue.code for issue in parsed.issues}


def test_reconstructs_ordinary_and_credit_payments():
    sheet = phase2_sheet(receipts="NCR100-RCA704CR", dates="01/01", credit_dates="04/01", payment=300)
    sheet.cells[(5, 7)] = CellData(5, 7, "G", "G5", 300, formula="=100+200", has_cached_value=True)

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert parsed.rows[0].reconstructed_payments == []
    assert "HIST_CREDIT_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in parsed.issues}


def test_does_not_reconstruct_subsidy_payment_with_sb_suffix():
    sheet = phase2_sheet(receipts="NCR100-NCR11771SB", dates="01/01", subsidy_dates="05/01", payment=300)
    sheet.cells[(5, 7)] = CellData(5, 7, "G", "G5", 300, formula="=100+200", has_cached_value=True)

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert parsed.rows[0].reconstructed_payments == []
    assert "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in parsed.issues}


def test_does_not_reconstruct_subsidy_payment_with_sub_suffix():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR11962SUB", dates="", subsidy_dates="05/01", payment=200)
    )

    assert parsed.rows[0].reconstructed_payments == []
    assert "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH" not in {issue.code for issue in parsed.issues}


def test_does_not_reconstruct_credit_and_subsidy_in_same_row():
    sheet = phase2_sheet(
        receipts="NCR100-RCA704CR-NCR2051SUB",
        dates="01/01",
        credit_dates="04/01",
        subsidy_dates="05/01",
        payment=600,
    )
    sheet.cells[(5, 7)] = CellData(5, 7, "G", "G5", 600, formula="=100+200+300", has_cached_value=True)

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert parsed.rows[0].reconstructed_payments == []
    codes = {issue.code for issue in parsed.issues}
    assert "HIST_CREDIT_DATE_RECEIPT_MISMATCH" not in codes
    assert "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH" not in codes


def test_reconstructs_categories_from_separate_transactional_value_sources():
    sheet = phase2_sheet(
        receipts="NCR5863-NCR5862-NCR5864-NCR5861-NCR5865CR-NCR5866SB",
        dates="NOV.24/24F-NOV.25/24F-NOV.26/24F-NOV.28/24F",
        credit_dates="24/07/2024",
        subsidy_dates="29/11/2024",
        payment=97172500,
        credit_value=80000000,
        subsidy_compensation_value=15000000,
    )
    sheet.cells[(4, 7)] = CellData(4, 7, "G", "G4", "RECIBIDO FIDUBOGOTA NOV/2024")
    sheet.cells[(5, 7)] = CellData(5, 7, "G", "G5", 97172500, formula="=583000+18171000+11952500+66466000", has_cached_value=True)

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert [
        (payment.category, payment.receipt, payment.date_value, payment.amount, payment.value_source_column)
        for payment in parsed.rows[0].reconstructed_payments
    ] == [
        (RECEIPT_CATEGORY_ORDINARY, "NCR5863", "NOV.24/24F", 583000, "G"),
        (RECEIPT_CATEGORY_ORDINARY, "NCR5862", "NOV.25/24F", 18171000, "G"),
        (RECEIPT_CATEGORY_ORDINARY, "NCR5864", "NOV.26/24F", 11952500, "G"),
        (RECEIPT_CATEGORY_ORDINARY, "NCR5861", "NOV.28/24F", 66466000, "G"),
        (RECEIPT_CATEGORY_CREDIT, "NCR5865CR", "24/07/2024", 80000000, "K"),
    ]
    assert not any(issue.code.startswith("HIST_SUBSIDY") and issue.code.endswith("_MISMATCH") for issue in parsed.issues)


def test_reconstructs_real_t5_equivalent_with_sub_suffix_and_transactional_values():
    sheet = phase2_sheet(
        receipts="NCR10515-NCR10513-NCR10514-NCR10512-NCR10516CR-NCR10517SUB",
        dates="MAY.3/23F-MAY.7/23F-MAY.11/23F-MAY.28/23F",
        credit_dates="30/12/2022",
        subsidy_dates="07/05/2023",
        payment=45228000,
        credit_value=95000000,
        subsidy_compensation_value=20000000,
    )
    sheet.cells[(4, 7)] = CellData(4, 7, "G", "G4", "RECIBIDO FIDUBOGOTA MAY/2023")
    sheet.cells[(5, 7)] = CellData(5, 7, "G", "G5", 45228000, formula="=10402000+7508000+19855000+7463000", has_cached_value=True)

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert [(payment.category, payment.receipt, payment.amount) for payment in parsed.rows[0].reconstructed_payments] == [
        (RECEIPT_CATEGORY_ORDINARY, "NCR10515", 10402000),
        (RECEIPT_CATEGORY_ORDINARY, "NCR10513", 7508000),
        (RECEIPT_CATEGORY_ORDINARY, "NCR10514", 19855000),
        (RECEIPT_CATEGORY_ORDINARY, "NCR10512", 7463000),
        (RECEIPT_CATEGORY_CREDIT, "NCR10516CR", 95000000),
    ]
    assert not any(issue.code.startswith("HIST_SUBSIDY") and issue.code.endswith("_MISMATCH") for issue in parsed.issues)


def test_reconstructs_compensation_subsidy_with_single_source_and_date():
    sheet = phase2_sheet(
        receipts="NCR100-NCR200SB",
        dates="ENE.1/26F",
        subsidy_dates="13/05/2025",
        payment=500000,
        subsidy_compensation_value=15000000,
    )

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert not any(payment.category == RECEIPT_CATEGORY_SUBSIDY for payment in parsed.rows[0].reconstructed_payments)
    codes = {issue.code for issue in parsed.issues}
    assert "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH" not in codes
    assert "HIST_SUBSIDY_VALUE_RECEIPT_MISMATCH" not in codes


def test_reconstructs_government_subsidy_with_single_source_and_date():
    sheet = phase2_sheet(
        receipts="NCR100-NCR201SUB",
        dates="ENE.1/26F",
        subsidy_dates="14/06/2025",
        payment=500000,
        subsidy_government_value=20000000,
    )

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert not any(payment.category == RECEIPT_CATEGORY_SUBSIDY for payment in parsed.rows[0].reconstructed_payments)
    codes = {issue.code for issue in parsed.issues}
    assert "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH" not in codes
    assert "HIST_SUBSIDY_VALUE_RECEIPT_MISMATCH" not in codes


def test_reconstructs_ambiguous_compensation_and_government_subsidies_without_inventing_dates():
    sheet = phase2_sheet(
        receipts="NCR300SB-NCR301SUB",
        dates="",
        subsidy_dates="10/03/2024 - 20/04/2024",
        payment=None,
        subsidy_compensation_value=15000000,
        subsidy_government_value=20000000,
    )

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert not any(payment.category == RECEIPT_CATEGORY_SUBSIDY for payment in parsed.rows[0].reconstructed_payments)
    assert "HIST_SUBSIDY_DATE_SOURCE_AMBIGUOUS" not in {issue.code for issue in parsed.issues}
    assert not any(issue.severity == "blocking" for issue in parsed.issues)


@pytest.mark.parametrize(
    (
        "unit",
        "receipts",
        "credit_dates",
        "subsidy_dates",
        "subsidy_compensation_value",
        "subsidy_government_value",
    ),
    [
        ("104", "NCR3137CR-NCR3138SB", "07/11/2022", "08/11/2022 - 10/12/2022", 26000000, 10000000),
        ("105", "", None, "10/12/2022", None, 10000000),
        ("201", "NCR3185SB", None, "10/03/2024 - 20/04/2024", 26000000, 63422500),
        ("202", "NCR2051SUB", None, "10/03/2024 - 20/04/2024", 20000000, 10000000),
    ],
)
def test_ksmp_subsidy_date_receipt_mismatch_cases_are_processable(
    unit,
    receipts,
    credit_dates,
    subsidy_dates,
    subsidy_compensation_value,
    subsidy_government_value,
):
    sheet = phase2_sheet(
        unit=unit,
        receipts=receipts,
        dates="",
        credit_dates=credit_dates,
        subsidy_dates=subsidy_dates,
        payment=None,
        credit_value=1 if credit_dates else None,
        subsidy_compensation_value=subsidy_compensation_value,
        subsidy_government_value=subsidy_government_value,
    )

    parsed = HistoricalWorkbookParser(Path("LIBRO KSMP.xlsx"))._parse_sheet(sheet)
    subsidy_payments = [payment for payment in parsed.rows[0].reconstructed_payments if payment.category == RECEIPT_CATEGORY_SUBSIDY]

    assert parsed.classification == "processable"
    codes = {issue.code for issue in parsed.issues}
    assert not any(
        issue.severity == "blocking" and issue.code == "HIST_SUBSIDY_DATE_RECEIPT_MISMATCH"
        for issue in parsed.issues
    )
    assert "HIST_SUBSIDY_DATE_SOURCE_AMBIGUOUS" not in codes
    assert "HIST_SUBSIDY_VALUE_RECEIPT_MISMATCH" not in codes
    assert subsidy_payments == []


def test_credit_receipt_with_date_but_no_credit_value_source_is_not_materialized():
    sheet = phase2_sheet(
        receipts="NCR100-RCA704CR",
        dates="NOV.24/24F",
        credit_dates="24/07/2024",
        payment=583000,
        credit_value=None,
        subsidy_compensation_value=0,
    )
    sheet.cells[(4, 7)] = CellData(4, 7, "G", "G4", "RECIBIDO FIDUBOGOTA NOV/2024")

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert [(payment.category, payment.receipt) for payment in parsed.rows[0].reconstructed_payments] == [
        (RECEIPT_CATEGORY_ORDINARY, "NCR100")
    ]
    assert "HIST_CREDIT_VALUE_RECEIPT_MISMATCH" not in {issue.code for issue in parsed.issues}


def test_reconstructs_monetary_transfer_from_assignment_changes_value():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(
            receipts="(NC14119TRASLADO)",
            dates="(MAY.9/25TRASLADO)",
            payment=None,
            assignment_changes_value=20000000,
        )
    )

    payment = parsed.rows[0].reconstructed_payments[0]
    assert payment.category == RECEIPT_CATEGORY_TRANSFER
    assert payment.receipt == "(NC14119TRASLADO)"
    assert payment.date_value == "(MAY.9/25TRASLADO)"
    assert payment.amount == 20000000
    assert payment.value_source_column == "N"


def test_reconstructs_monetary_cession_from_assignment_changes_value():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(
            receipts="(NC14131CESION)",
            dates="(MAY.9/25CESION)",
            payment=None,
            assignment_changes_value=30000000,
        )
    )

    payment = parsed.rows[0].reconstructed_payments[0]
    assert payment.category == RECEIPT_CATEGORY_CESSION
    assert payment.receipt == "(NC14131CESION)"
    assert payment.date_value == "(MAY.9/25CESION)"
    assert payment.amount == 30000000


def test_transfer_with_date_and_receipt_but_missing_value_is_diagnosed():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(
            receipts="NC14119TRASLADO",
            dates="MAY.9/25TRASLADO",
            payment=None,
            assignment_changes_value=0,
        )
    )

    assert parsed.rows[0].reconstructed_payments == []
    issue = next(issue for issue in parsed.issues if issue.code == "HIST_TRANSFER_VALUE_RECEIPT_MISMATCH")
    assert issue.found_value == "0 valores de traslado / 1 recibos TRASLADO con fecha"


def test_reconstructs_receipts_from_standard_and_fidubogota_columns():
    sheet = montecielo_payment_sheet(
        receipts="NCR100",
        fidubogota_receipts="NCR101",
        dates="01/01-02/01F",
        received=100,
        fiduciary_value=200,
    )

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert [(payment.receipt, payment.receipt_source, payment.receipt_source_column, payment.amount, payment.destination) for payment in parsed.rows[0].reconstructed_payments] == [
        ("NCR100", "receipt_numbers", "E", 100, "constructora"),
        ("NCR101", "fidubogota_receipt_numbers", "F", 200, "fiduciaria"),
    ]


def test_simple_sum_formula_is_split_into_individual_values():
    sheet = phase2_sheet(receipts="NCR100-NCR101-NCR102", dates="01/01-02/01-03/01", payment=60)
    sheet.cells[(5, 7)] = CellData(5, 7, "G", "G5", 60, formula="=10+20+30", has_cached_value=True)

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert [payment.amount for payment in parsed.rows[0].reconstructed_payments] == [10, 20, 30]
    assert all(payment.value_had_formula for payment in parsed.rows[0].reconstructed_payments)


def test_unreceipted_separator_dates_require_individual_monthly_values():
    sheet = phase2_sheet(receipts="", dates=" || ENE.1/26F-ENE.2/26F", payment=1200)

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert parsed.rows[0].payments == []
    assert parsed.rows[0].reconstructed_payments == []
    issue = next(issue for issue in parsed.issues if issue.code == "HIST_UNRECEIPTED_PAYMENT_VALUE_COUNT_MISMATCH")
    assert issue.found_value == "1 valores / 2 fechas sin recibo"


def test_incompatible_value_count_is_diagnosed_without_reconstruction():
    sheet = phase2_sheet(receipts="NCR100-NCR101-NCR102", dates="01/01-02/01-03/01", payment=30)
    sheet.cells[(5, 7)] = CellData(5, 7, "G", "G5", 30, formula="=10+20", has_cached_value=True)

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert parsed.rows[0].reconstructed_payments == []
    issue = next(issue for issue in parsed.issues if issue.code == "HIST_PAYMENT_VALUE_COUNT_MISMATCH")
    assert issue.found_value == "2 valores / 3 recibos con fecha"
    assert issue.severity == "info"


def test_aggregate_received_value_for_multiple_payments_is_not_distributed():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR100-NCR101", dates="01/01-02/01", payment=300)
    )

    assert parsed.rows[0].reconstructed_payments == []
    issue = next(issue for issue in parsed.issues if issue.code == "HIST_PAYMENT_AGGREGATE_NOT_DISTRIBUTABLE")
    assert issue.found_value == "300.00"
    assert "no se puede distribuir" in issue.cause
    assert issue.severity == "info"


def test_unsupported_formula_is_diagnosed_without_eval():
    sheet = phase2_sheet(receipts="NCR100", dates="01/01", payment=300)
    sheet.cells[(5, 7)] = CellData(5, 7, "G", "G5", 300, formula="=SUM(100,200)", has_cached_value=True)

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    issue = next(issue for issue in parsed.issues if issue.code == "HIST_PAYMENT_FORMULA_NOT_RECONSTRUCTIBLE")
    assert issue.found_value == "=SUM(100,200)"
    assert issue.severity == "info"


def test_non_reconstructible_payment_does_not_invalidate_structurally_valid_row():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR100-NCR101", dates="01/01-02/01", payment=300)
    )

    assert parsed.classification == "processable"
    assert len(parsed.rows) == 1
    assert parsed.rows[0].unit_code == "101"
    assert parsed.rows[0].assignment.assignment_number == "EF-101"
    assert parsed.rows[0].reconstructed_payments == []
    codes = {issue.code for issue in parsed.issues}
    assert "HIST_PAYMENT_AGGREGATE_NOT_DISTRIBUTABLE" in codes
    assert "INVALID_HISTORICAL_ROW" not in codes
    assert not any(issue.severity == "blocking" for issue in parsed.issues)


def test_fully_reconstructible_payment_row_remains_valid():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(
        phase2_sheet(receipts="NCR100", dates="01/01", payment=300)
    )

    assert parsed.classification == "processable"
    assert len(parsed.rows) == 1
    assert parsed.rows[0].reconstructed_payments[0].receipt == "NCR100"
    assert "INVALID_HISTORICAL_ROW" not in {issue.code for issue in parsed.issues}


def test_non_reconstructible_payment_diagnostic_does_not_block_import_readiness(
    monkeypatch,
    tmp_path,
    accounting_admin_user,
):
    project = Project.objects.create(code="Prueba", name="Prueba")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    group = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="T1")
    PropertyUnit.objects.create(project=project, structural_group=group, code="101", name="101")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )
    file_path = tmp_path / "LIBRO Prueba.xlsx"
    file_path.write_bytes(b"placeholder")
    workbook = RawWorkbook(
        "xlsx",
        [phase2_sheet(receipts="NCR100-NCR101", dates="01/01-02/01", payment=300)],
        [],
    )
    monkeypatch.setattr("fiduciary.imports.historical.analyzer.calculate_sha256", lambda path: "f" * 64)
    original_parse = HistoricalWorkbookParser.parse

    def fake_parse(self):
        self.reader.read = lambda path: workbook
        return original_parse(self)

    monkeypatch.setattr("fiduciary.imports.historical.analyzer.HistoricalWorkbookParser.parse", fake_parse)

    result = analyze_historical_import(batch=batch, file_path=file_path, grouping_type_hint="Torre")
    batch.refresh_from_db()

    issue = result.imported_file.row_issues.get(code="HIST_PAYMENT_AGGREGATE_NOT_DISTRIBUTABLE")
    assert issue.severity == ImportRowIssue.Severity.INFO
    assert batch.processed_rows == 1
    assert batch.status == ImportBatch.Status.READY
    assert can_finalize_historical_import_batch(batch, require_stored_file=False)


def test_partial_row_keeps_diagnostic_context():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(phase2_sheet(unit="", assignment="EF-101"))

    issue = next(issue for issue in parsed.issues if issue.code == "INVALID_HISTORICAL_ROW")
    assert issue.row_number == 5
    assert "unidad" in issue.field_name
    assert "Falta unidad" in issue.cause


def test_multi_sheet_workbook_keeps_independent_sheet_results(monkeypatch):
    parser = HistoricalWorkbookParser(Path("LIBRO Multi.xlsx"))
    workbook = RawWorkbook(
        "xlsx",
        [
            phase2_sheet("T1"),
            phase2_sheet("T2", receipts="R1", dates="ENE.1/26F"),
            phase2_sheet("T3", unit="1503", receipts="R1-R2-R3", dates="ENE.1/26F-ENE.2/26F"),
            RawSheet("Resumen", 4, "visible", "A1:A1", {(1, 1): CellData(1, 1, "A", "A1", "Resumen")}, set(), set()),
        ],
        [],
    )
    monkeypatch.setattr(parser.reader, "read", lambda path: workbook)

    parsed = parser.parse()
    by_name = {sheet.name: sheet for sheet in parsed.sheets}

    assert by_name["T1"].classification == "processable"
    assert by_name["T2"].classification == "processable"
    assert any(issue.severity == "blocking" for issue in by_name["T3"].issues)
    assert by_name["Resumen"].classification == "unknown"
    assert parsed.statistics.sheets_total == 4
    assert parsed.statistics.sheets_processed == 3


def test_analyzer_persists_issue_detail_fields(monkeypatch, tmp_path, accounting_admin_user):
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )
    file_path = tmp_path / "LIBRO Multi.xlsx"
    file_path.write_bytes(b"placeholder")
    workbook = RawWorkbook("xlsx", [phase2_sheet("T1", unit="1503", receipts="R1-R2-R3", dates="ENE.1/26F-ENE.2/26F")], [])
    monkeypatch.setattr("fiduciary.imports.historical.analyzer.calculate_sha256", lambda path: "b" * 64)
    original_parse = HistoricalWorkbookParser.parse

    def fake_parse(self):
        self.reader.read = lambda path: workbook
        return original_parse(self)

    monkeypatch.setattr("fiduciary.imports.historical.analyzer.HistoricalWorkbookParser.parse", fake_parse)

    result = analyze_historical_import(batch=batch, file_path=file_path, grouping_type_hint="Torre")
    issue = result.imported_file.row_issues.get(code="HIST_PAYMENT_DATE_RECEIPT_MISMATCH")

    assert issue.unit_code == "1503"
    assert issue.field_name == "FECHA / RECIBOS"
    assert issue.found_value == "2 fechas ordinarias / 3 recibos ordinarios"
    assert issue.extra_data["receipt_count"] == 3


def test_issue_group_detail_view_lists_individual_cases(accounting_client, accounting_admin_user):
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
        sha256="c" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    sheet = ImportedSheetResult.objects.create(imported_file=imported_file, sheet_name="T4", sheet_index=1)
    ImportRowIssue.objects.create(
        imported_file=imported_file,
        sheet_result=sheet,
        row_number=127,
        column_letter="F/E",
        unit_code="1503",
        field_name="FECHA / RECIBOS",
        found_value="FECHA: 20 | RECIBOS: 21",
        cause="20 fecha(s) y 21 recibo(s).",
        severity=ImportRowIssue.Severity.BLOCKING,
        code="HIST_PAYMENT_DATE_RECEIPT_MISMATCH",
        message="La cantidad de fechas no coincide con la cantidad de recibos.",
        extra_data={"grouping_name": "Torre 4", "scope": "cell"},
    )

    response = accounting_client.get(
        reverse("fiduciary:historical_import_issues", args=[batch.pk]),
        {"code": "HIST_PAYMENT_DATE_RECEIPT_MISMATCH", "severity": "blocking", "sheet": "T4"},
    )

    assert response.status_code == 200
    content = response.content.decode()
    assert "<th>Codigo</th>" in content
    assert "<th>Severidad</th>" in content
    assert "<th>Hoja</th>" in content
    assert "<th>Fila</th>" in content
    assert "<th>Columna</th>" in content
    assert "<th>Valor encontrado</th>" in content
    assert "<th>Causa</th>" in content
    assert "<th>Descripcion</th>" in content
    assert "<th>Agrupacion</th>" not in content
    assert "<th>Alcance</th>" not in content
    assert "<th>Unidad</th>" not in content
    assert "FECHA: 20 | RECIBOS: 21" in content
    assert "F/E" in content
    assert "FECHA / RECIBOS" in content
    assert "20 fecha(s) y 21 recibo(s)." in content


def test_issue_group_detail_view_renders_formula_column_issue(accounting_client, accounting_admin_user):
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
        sha256="1" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    sheet = ImportedSheetResult.objects.create(imported_file=imported_file, sheet_name="T1", sheet_index=1)
    ImportRowIssue.objects.create(
        imported_file=imported_file,
        sheet_result=sheet,
        column_letter="AA",
        field_name="RECIBIDO FIDUBOGOTA FEB/2022",
        found_value="Una o más fórmulas con valor calculado disponible.",
        cause="La columna contiene fórmulas; en esta fase se conserva el valor calculado sin descomponer pagos.",
        severity=ImportRowIssue.Severity.WARNING,
        code="FORMULA_WITH_CACHED_VALUE",
        message="Columna mensual relevante contiene formulas con valor calculado disponible.",
        extra_data={"scope": "column"},
    )

    response = accounting_client.get(
        reverse("fiduciary:historical_import_issues", args=[batch.pk]),
        {"code": "FORMULA_WITH_CACHED_VALUE", "severity": "warning", "sheet": "T1"},
    )

    content = response.content.decode()
    assert response.status_code == 200
    assert "FORMULA_WITH_CACHED_VALUE" in content
    assert "Advertencia" in content
    assert "T1" in content
    assert "No aplica" in content
    assert "AA" in content
    assert "RECIBIDO FIDUBOGOTA FEB/2022" in content
    assert "Una o más fórmulas con valor calculado disponible." in content
    assert "La columna contiene fórmulas" in content


def _assignment_context():
    project = Project.objects.create(code="P1", name="Proyecto 1")
    grouping_type = GroupingType.objects.create(code="TOR", name="Torre")
    group = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="T1")
    unit = PropertyUnit.objects.create(project=project, structural_group=group, code="101", name="101")
    assignment = FiduciaryAssignment.objects.create(property_unit=unit, assignment_number="EF-101", start_date="2026-01-01")
    first = Client.objects.create(
        first_names="JUAN",
        last_names_or_company="PEREZ",
        document_number="1",
        phone="3001111111",
        source_origin=Client.SourceOrigin.HISTORICAL_IMPORT,
    )
    second = Client.objects.create(
        first_names="MARIA",
        last_names_or_company="LOPEZ",
        document_number="2",
        phone="3002222222",
        source_origin=Client.SourceOrigin.HISTORICAL_IMPORT,
    )
    for index, client in enumerate([first, second], start=1):
        ownership = UnitOwnership.objects.create(
            property_unit=unit,
            client=client,
            is_primary=index == 1,
            start_date="2026-01-01",
        )
        FiduciaryAssignmentHolder.objects.create(
            assignment=assignment,
            client=client,
            is_primary=index == 1,
            start_date=ownership.start_date,
        )
    return unit, assignment, first, second


def test_assignment_observation_is_created_once_and_visible_from_both_clients(accounting_client, accounting_admin_user):
    unit, assignment, first, second = _assignment_context()
    observation = ImportedHistoricalObservation.objects.create(
        property_unit=unit,
        assignment=assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        detail="PENDIENTE DOCUMENTACION",
        imported_by=accounting_admin_user,
    )

    assignment_response = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk]))
    first_response = accounting_client.get(reverse("fiduciary:client_detail", args=[first.pk]))
    second_response = accounting_client.get(reverse("fiduciary:client_detail", args=[second.pk]))

    assert ImportedHistoricalObservation.objects.filter(assignment=assignment, detail=observation.detail).count() == 1
    assert "PENDIENTE DOCUMENTACION" in assignment_response.content.decode()
    assert "PENDIENTE DOCUMENTACION" in first_response.content.decode()
    assert "PENDIENTE DOCUMENTACION" in second_response.content.decode()


def test_observation_tables_do_not_show_removed_redundant_columns(accounting_client, accounting_admin_user):
    unit, assignment, *_ = _assignment_context()
    ImportedHistoricalObservation.objects.create(
        property_unit=unit,
        assignment=assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        detail="OBSERVACION DEL ENCARGO",
        imported_by=accounting_admin_user,
    )

    list_content = accounting_client.get(reverse("fiduciary:observation_list")).content.decode()
    assignment_content = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk])).content.decode()

    assert "<th>Cliente</th>" not in list_content
    assert "<th>Usuario</th>" not in list_content
    observations_section = assignment_content.split("<h2>Observaciones relacionadas</h2>", 1)[1].split(
        "<h2>Novedades relacionadas</h2>",
        1,
    )[0]
    assert "<th>Cliente</th>" not in observations_section
    assert "<th>Encargo</th>" not in observations_section


def test_observation_does_not_move_to_new_holder_without_assignment_relation(accounting_client, accounting_admin_user):
    unit, old_assignment, old_client, _ = _assignment_context()
    new_client = Client.objects.create(
        first_names="NUEVO",
        last_names_or_company="TITULAR",
        document_number="3",
        phone="3003333333",
        source_origin=Client.SourceOrigin.HISTORICAL_IMPORT,
    )
    UnitOwnership.objects.filter(property_unit=unit, is_primary=True, is_active=True).update(
        is_active=False,
        end_date="2026-01-31",
    )
    old_assignment.is_active = False
    old_assignment.end_date = "2026-01-31"
    old_assignment.save(update_fields=["is_active", "end_date", "updated_at"])
    UnitOwnership.objects.create(property_unit=unit, client=new_client, is_primary=True, start_date="2026-02-01")
    new_assignment = FiduciaryAssignment.objects.create(
        property_unit=unit,
        assignment_number="EF-102",
        start_date="2026-02-01",
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=new_assignment,
        client=new_client,
        is_primary=True,
        start_date="2026-02-01",
    )
    ImportedHistoricalObservation.objects.create(
        property_unit=unit,
        assignment=old_assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        detail="OBSERVACION DEL ENCARGO ANTERIOR",
        imported_by=accounting_admin_user,
    )

    old_content = accounting_client.get(reverse("fiduciary:client_detail", args=[old_client.pk])).content.decode()
    new_content = accounting_client.get(reverse("fiduciary:client_detail", args=[new_client.pk])).content.decode()

    assert "OBSERVACION DEL ENCARGO ANTERIOR" in old_content
    assert "OBSERVACION DEL ENCARGO ANTERIOR" not in new_content


def test_same_observation_text_remains_independent_by_assignment(accounting_admin_user):
    first_unit, first_assignment, *_ = _assignment_context()
    second_unit = PropertyUnit.objects.create(project=first_unit.project, structural_group=first_unit.structural_group, code="102", name="102")
    second_assignment = FiduciaryAssignment.objects.create(property_unit=second_unit, assignment_number="EF-102", start_date="2026-01-01")

    ImportedHistoricalObservation.objects.create(
        property_unit=first_unit,
        assignment=first_assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        detail="MISMO TEXTO",
        imported_by=accounting_admin_user,
    )
    ImportedHistoricalObservation.objects.create(
        property_unit=second_unit,
        assignment=second_assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        detail="MISMO TEXTO",
        imported_by=accounting_admin_user,
    )

    assert ImportedHistoricalObservation.objects.filter(detail="MISMO TEXTO").count() == 2


def test_structural_row_with_only_payment_like_values_does_not_create_invalid_issue():
    sheet = phase2_sheet(unit="", assignment="", client_name="", document="", receipts="", dates="", payment=250000)
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert not any(issue.code == "INVALID_HISTORICAL_ROW" for issue in parsed.issues)
    assert parsed.ignored_row_reasons["structural_or_auxiliary"] == 1


def test_structural_row_with_only_client_data_does_not_create_invalid_issue():
    sheet = phase2_sheet(unit="", assignment="", client_name="TITULO CESIONES", document="", receipts="", dates="", payment=None)
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert not any(issue.code == "INVALID_HISTORICAL_ROW" for issue in parsed.issues)
    assert parsed.ignored_row_reasons["structural_or_auxiliary"] == 1


def test_structural_row_with_only_residual_unit_value_does_not_create_invalid_issue():
    sheet = phase2_sheet(unit="2", assignment="", client_name="", document="", receipts="", dates="", payment=None)
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert not any(issue.code == "INVALID_HISTORICAL_ROW" for issue in parsed.issues)
    assert parsed.ignored_row_reasons["structural_or_auxiliary"] == 1


def test_incomplete_main_row_still_creates_invalid_issue():
    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(phase2_sheet(unit="1501", assignment=""))

    issue = next(issue for issue in parsed.issues if issue.code == "INVALID_HISTORICAL_ROW")
    assert issue.unit_code == "1501"
    assert "encargo" in issue.field_name


def test_formula_with_cached_value_records_column_scope():
    sheet = phase2_sheet()
    sheet.cells[(5, 7)] = CellData(5, 7, "G", "G5", 1000, formula="=500+500", has_cached_value=True)

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)
    issue = next(issue for issue in parsed.issues if issue.code == "FORMULA_WITH_CACHED_VALUE")

    assert issue.row_number is None
    assert issue.field_name == "RECIBO FIDUCIA ENE/2026"
    assert issue.extra_data["scope"] == "column"
    assert "valor calculado" in issue.cause


def test_parser_reports_real_progress_by_sheet_and_rows(monkeypatch):
    events = []
    parser = HistoricalWorkbookParser(Path("LIBRO Multi.xlsx"), progress_callback=events.append)
    workbook = RawWorkbook("xlsx", [phase2_sheet("T1"), phase2_sheet("T2", row=6)], [])
    monkeypatch.setattr(parser.reader, "read", lambda path: workbook)

    parser.parse()

    assert events[0]["phase"] == "preparing"
    assert any(event["phase"] == "processing_sheet" and event["sheet"] == "T1" for event in events)
    assert any(event["phase"] == "processing_rows" and event["row"] >= 5 for event in events)
    assert events[-1]["percent"] == 100


def test_progress_endpoint_returns_batch_progress(accounting_client, accounting_admin_user):
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_rows=100,
        processed_rows=40,
        summary='{"progress":{"phase":"processing_rows","percent":40,"sheet":"T4","row":50,"processed_rows":40,"total_rows":100}}',
    )

    response = accounting_client.get(reverse("fiduciary:historical_import_progress", args=[batch.pk]))

    assert response.status_code == 200
    assert response.json()["progress"]["percent"] == 40
    assert response.json()["progress"]["sheet"] == "T4"


def test_pending_resolution_requires_confirmation_to_apply_equivalents(accounting_client, accounting_admin_user):
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )
    first = DetectedStructureElement.objects.create(
        batch=batch,
        raw_value="Spring Field",
        normalized_value="springfield",
        inferred_kind=DetectedStructureElement.InferredKind.PROJECT,
        occurrence_count=4,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    second = DetectedStructureElement.objects.create(
        batch=batch,
        raw_value="springfield",
        normalized_value="springfield",
        inferred_kind=DetectedStructureElement.InferredKind.PROJECT,
        occurrence_count=2,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    ImportResolution.objects.create(detected_element=first)
    ImportResolution.objects.create(detected_element=second)
    project = Project.objects.create(code="SPR", name="Springfield")

    assert equivalent_pending_elements(first).count() == 1
    response = accounting_client.post(
        reverse("fiduciary:historical_import_resolve", args=[batch.pk, first.pk]),
        {
            "target_kind": DetectedStructureElement.InferredKind.PROJECT,
            "action": ImportResolution.Action.ASSOCIATE_EXISTING,
            "target_project": project.pk,
        },
        follow=True,
    )

    assert response.status_code == 200
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == DetectedStructureElement.Status.RESOLVED
    assert second.status == DetectedStructureElement.Status.NEEDS_REVIEW


def test_pending_resolution_applies_equivalents_only_with_confirmation(accounting_client, accounting_admin_user):
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )
    first = DetectedStructureElement.objects.create(
        batch=batch,
        raw_value="Spring Field",
        normalized_value="springfield",
        inferred_kind=DetectedStructureElement.InferredKind.PROJECT,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    second = DetectedStructureElement.objects.create(
        batch=batch,
        raw_value="springfield",
        normalized_value="springfield",
        inferred_kind=DetectedStructureElement.InferredKind.PROJECT,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    third = DetectedStructureElement.objects.create(
        batch=batch,
        raw_value="Other",
        normalized_value="other",
        inferred_kind=DetectedStructureElement.InferredKind.PROJECT,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    ImportResolution.objects.create(detected_element=first)
    ImportResolution.objects.create(detected_element=second)
    ImportResolution.objects.create(detected_element=third)
    project = Project.objects.create(code="SPR", name="Springfield")

    response = accounting_client.post(
        reverse("fiduciary:historical_import_resolve", args=[batch.pk, first.pk]),
        {
            "target_kind": DetectedStructureElement.InferredKind.PROJECT,
            "action": ImportResolution.Action.ASSOCIATE_EXISTING,
            "target_project": project.pk,
            "apply_equivalents": "1",
        },
        follow=True,
    )

    assert response.status_code == 200
    first.refresh_from_db()
    second.refresh_from_db()
    third.refresh_from_db()
    assert first.status == DetectedStructureElement.Status.RESOLVED
    assert second.status == DetectedStructureElement.Status.RESOLVED
    assert third.status == DetectedStructureElement.Status.NEEDS_REVIEW


def _create_structural_group_pending(batch, raw_value, project, grouping_type, *, include_context=True):
    element = DetectedStructureElement.objects.create(
        batch=batch,
        raw_value=raw_value,
        normalized_value=normalize_text(raw_value),
        inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        structural_context={
            "project_id": project.pk,
            "project_name": project.name,
            "grouping_type_id": grouping_type.pk,
            "grouping_type_name": grouping_type.name,
            "grouping_name": raw_value,
        }
        if include_context
        else {},
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    ImportResolution.objects.create(detected_element=element)
    return element



def test_structural_group_resolution_applies_t1_to_t12_pattern_for_current_project_only(accounting_client, accounting_admin_user):
    project = Project.objects.create(code="P12", name="Proyecto Doce Torres")
    other_project = Project.objects.create(code="POT", name="Otro Proyecto")
    grouping_type = GroupingType.objects.create(code="TOR", name="Torre")
    groups = [
        StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code=f"T{i}", name=f"Torre {i}")
        for i in range(1, 13)
    ]
    StructuralGroup.objects.create(project=other_project, grouping_type=grouping_type, code="T2", name="Torre 2")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )
    pending_by_name = {
        f"T{index}": _create_structural_group_pending(batch, f"T{index}", project, grouping_type, include_context=False)
        for index in range(1, 13)
    }
    outside = _create_structural_group_pending(batch, "T2", other_project, grouping_type)

    response = accounting_client.post(
        reverse("fiduciary:historical_import_resolve_group", args=[batch.pk, pending_by_name["T1"].pk]),
        {
            "action": ImportResolution.Action.ASSOCIATE_EXISTING,
            "project": project.pk,
            "grouping_type": grouping_type.pk,
            "existing_group": groups[0].pk,
        },
        follow=True,
    )

    assert response.status_code == 200
    for index in range(1, 13):
        element = pending_by_name[f"T{index}"]
        element.refresh_from_db()
        assert element.status == DetectedStructureElement.Status.RESOLVED
        assert element.resolution.target_structural_group == groups[index - 1]
    outside.refresh_from_db()
    assert outside.status == DetectedStructureElement.Status.NEEDS_REVIEW


def test_resolve_pending_groups_applies_types_by_pattern_and_creates_missing_type_inline(accounting_client, accounting_admin_user):
    project = Project.objects.create(code="PMT", name="Proyecto Multitipo")
    torre = GroupingType.objects.create(code="T", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )
    project_element = DetectedStructureElement.objects.create(
        batch=batch,
        raw_value="Proyecto Multitipo",
        normalized_value=normalize_text("Proyecto Multitipo"),
        inferred_kind=DetectedStructureElement.InferredKind.PROJECT,
        status=DetectedStructureElement.Status.AUTO_MATCHED,
    )
    ImportResolution.objects.create(
        detected_element=project_element,
        target_kind=DetectedStructureElement.InferredKind.PROJECT,
        action=ImportResolution.Action.ASSOCIATE_EXISTING,
        status=ImportResolution.Status.APPLIED,
        target_project=project,
    )
    elements = {}
    for raw_value in ["T1", "T2", "ED1", "ED2"]:
        element = _create_structural_group_pending(batch, raw_value, project, torre, include_context=False)
        element.structural_context = {
            "project_id": project.pk,
            "project_name": project.name,
            "grouping_name": raw_value,
        }
        element.save(update_fields=["structural_context"])
        elements[raw_value] = element

    response = accounting_client.post(
        reverse("fiduciary:historical_import_resolve_group", args=[batch.pk, elements["T1"].pk]),
        {
            "action": ImportResolution.Action.CREATE_NEW,
            "project": project.pk,
            "grouping_type": torre.pk,
            "new_group_name": "T1",
        },
        follow=True,
    )

    assert response.status_code == 200
    for raw_value in ["T1", "T2"]:
        elements[raw_value].refresh_from_db()
        assert elements[raw_value].status == DetectedStructureElement.Status.RESOLVED
        assert elements[raw_value].resolution.action == ImportResolution.Action.CREATE_NEW
        assert elements[raw_value].resolution.parent_grouping_type == torre
        assert elements[raw_value].resolution.create_name == raw_value
    for raw_value in ["ED1", "ED2"]:
        elements[raw_value].refresh_from_db()
        assert elements[raw_value].status == DetectedStructureElement.Status.NEEDS_REVIEW

    response = accounting_client.post(
        reverse("fiduciary:historical_import_resolve_group", args=[batch.pk, elements["ED1"].pk]),
        {
            "action": ImportResolution.Action.CREATE_NEW,
            "project": project.pk,
            "create_grouping_type": "on",
            "new_grouping_type_code": "ED",
            "new_grouping_type_name": "Edificacion",
            "new_group_name": "ED1",
        },
        follow=True,
    )

    assert response.status_code == 200
    edificacion = GroupingType.objects.get(code="ED", name="Edificacion")
    for raw_value in ["ED1", "ED2"]:
        elements[raw_value].refresh_from_db()
        assert elements[raw_value].status == DetectedStructureElement.Status.RESOLVED
        assert elements[raw_value].resolution.action == ImportResolution.Action.CREATE_NEW
        assert elements[raw_value].resolution.parent_grouping_type == edificacion
        assert elements[raw_value].resolution.create_name == raw_value


def test_structural_group_ambiguous_pattern_keeps_remaining_pendings(accounting_client, accounting_admin_user):
    project = Project.objects.create(code="PAMB", name="Proyecto Ambiguo Patron")
    grouping_type = GroupingType.objects.create(code="TOR", name="Torre")
    group_1 = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="TN", name="Torre Norte")
    StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="TS", name="Torre Sur")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )
    source = _create_structural_group_pending(batch, "T1", project, grouping_type)
    second = _create_structural_group_pending(batch, "T2", project, grouping_type)

    response = accounting_client.post(
        reverse("fiduciary:historical_import_resolve_group", args=[batch.pk, source.pk]),
        {
            "action": ImportResolution.Action.ASSOCIATE_EXISTING,
            "project": project.pk,
            "grouping_type": grouping_type.pk,
            "existing_group": group_1.pk,
        },
        follow=True,
    )

    assert response.status_code == 200
    source.refresh_from_db()
    second.refresh_from_db()
    assert source.status == DetectedStructureElement.Status.RESOLVED
    assert second.status == DetectedStructureElement.Status.NEEDS_REVIEW


def test_resolve_group_last_pending_leaves_batch_ready_without_finalizing(accounting_client, accounting_admin_user):
    project = Project.objects.create(code="PNF", name="Proyecto No Finaliza")
    grouping_type = GroupingType.objects.create(code="TOR", name="Torre")
    group = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )
    stored_dir = settings.MEDIA_ROOT / "imports" / "historical"
    stored_dir.mkdir(parents=True, exist_ok=True)
    stored_path = stored_dir / "resolve-group-no-finalize.xlsx"
    stored_path.write_bytes(b"not a real workbook")
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="historico.xlsx",
        extension=".xlsx",
        size_bytes=stored_path.stat().st_size,
        sha256="b" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
        stored_path="imports/historical/resolve-group-no-finalize.xlsx",
    )
    source = DetectedStructureElement.objects.create(
        batch=batch,
        imported_file=imported_file,
        raw_value="T1",
        normalized_value=normalize_text("T1"),
        inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        structural_context={"project_id": project.pk, "grouping_type_id": grouping_type.pk},
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    ImportResolution.objects.create(detected_element=source)

    response = accounting_client.post(
        reverse("fiduciary:historical_import_resolve_group", args=[batch.pk, source.pk]),
        {
            "action": ImportResolution.Action.ASSOCIATE_EXISTING,
            "project": project.pk,
            "grouping_type": grouping_type.pk,
            "existing_group": group.pk,
        },
        follow=True,
    )

    assert response.status_code == 200
    batch.refresh_from_db()
    source.refresh_from_db()
    assert source.status == DetectedStructureElement.Status.RESOLVED
    assert batch.status == ImportBatch.Status.READY


def test_historical_cession_sets_new_holder_when_assignment_state_is_unambiguous(accounting_admin_user):
    unit, assignment, previous_client, new_client = _assignment_context()
    FiduciaryAssignmentHolder.objects.filter(assignment=assignment, client=previous_client).update(is_active=False)
    UnitOwnership.objects.filter(property_unit=unit, client=previous_client).update(is_active=False)
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
        sha256="d" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    sheet_result = ImportedSheetResult.objects.create(imported_file=imported_file, sheet_name="T1", sheet_index=1)
    novelty = ImportedHistoricalNovelty.objects.create(
        batch=batch,
        imported_file=imported_file,
        sheet_result=sheet_result,
        row_number=20,
        project_name=unit.project.name,
        grouping_name=unit.structural_group.name,
        unit_code=unit.code,
        assignment_number=assignment.assignment_number,
        original_cells=[
            {"header": "CEDULA CLIENTE", "value": previous_client.document_number},
            {"header": "NOMBRE CLIENTE", "value": previous_client.full_name},
            {"header": "OBSERVACIONES", "value": "CESION DE JUAN PEREZ A MARIA LOPEZ"},
        ],
    )
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)
    context.units_by_context[(normalize_text(unit.structural_group.name), normalize_text(unit.code))] = unit

    context._historical_novelty_observation(novelty)

    operational = OperationalNovelty.objects.get(source_novelty=novelty)
    assert operational.historical_client == previous_client
    assert operational.new_client == new_client


def test_historical_cession_leaves_new_holder_empty_when_ambiguous(accounting_admin_user):
    unit, assignment, previous_client, _ = _assignment_context()
    extra = Client.objects.create(
        first_names="OTRO",
        last_names_or_company="CANDIDATO",
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="9",
        phone="3009999999",
        source_origin=Client.SourceOrigin.HISTORICAL_IMPORT,
    )
    UnitOwnership.objects.create(property_unit=unit, client=extra, is_primary=False, start_date="2026-01-01")
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=extra,
        is_primary=False,
        start_date="2026-01-01",
    )
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
    sheet_result = ImportedSheetResult.objects.create(imported_file=imported_file, sheet_name="T1", sheet_index=1)
    novelty = ImportedHistoricalNovelty.objects.create(
        batch=batch,
        imported_file=imported_file,
        sheet_result=sheet_result,
        row_number=20,
        project_name=unit.project.name,
        grouping_name=unit.structural_group.name,
        unit_code=unit.code,
        assignment_number=assignment.assignment_number,
        original_cells=[
            {"header": "CEDULA CLIENTE", "value": previous_client.document_number},
            {"header": "NOMBRE CLIENTE", "value": previous_client.full_name},
            {"header": "OBSERVACIONES", "value": "CESION DE JUAN PEREZ"},
        ],
    )
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)
    context.units_by_context[(normalize_text(unit.structural_group.name), normalize_text(unit.code))] = unit

    context._historical_novelty_observation(novelty)

    operational = OperationalNovelty.objects.get(source_novelty=novelty)
    assert operational.new_client is None


@pytest.mark.parametrize("novelty_text", ["EXCLUSION DE KRABS LISA", "CESION DE KRABS LISA A OTRO TITULAR"])
def test_novelty_person_name_does_not_contaminate_structured_client_identity(accounting_admin_user, novelty_text):
    unit, assignment, structured_client, _ = _assignment_context()
    structured_client.first_names = "GREEN CLEVELAND"
    structured_client.last_names_or_company = ""
    structured_client.document_number = "15808784"
    structured_client.save(update_fields=["first_names", "last_names_or_company", "document_number", "updated_at"])
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
        sha256="f" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    sheet_result = ImportedSheetResult.objects.create(imported_file=imported_file, sheet_name="T1", sheet_index=1)
    novelty = ImportedHistoricalNovelty.objects.create(
        batch=batch,
        imported_file=imported_file,
        sheet_result=sheet_result,
        row_number=20,
        project_name=unit.project.name,
        grouping_name=unit.structural_group.name,
        unit_code=unit.code,
        assignment_number=assignment.assignment_number,
        original_cells=[
            {"header": "NOMBRE CLIENTE", "value": novelty_text},
            {"header": "OBSERVACIONES", "value": f"NC3348 MAY.26/22 {novelty_text}"},
        ],
    )
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)
    context.units_by_context[(normalize_text(unit.structural_group.name), normalize_text(unit.code))] = unit

    context._historical_novelty_observation(novelty)

    structured_client.refresh_from_db()
    operational = OperationalNovelty.objects.get(source_novelty=novelty)
    assert structured_client.full_name == "GREEN CLEVELAND"
    assert not Client.objects.filter(first_names__icontains="KRABS", last_names_or_company__icontains="GREEN").exists()
    assert not Client.objects.filter(first_names__icontains="GREEN", last_names_or_company__icontains="KRABS").exists()
    assert novelty_text in operational.summary or novelty_text in operational.detail


def test_global_tables_show_grouping_for_repeated_unit_code(accounting_client, accounting_admin_user):
    project = Project.objects.create(code="P408", name="Proyecto 408")
    grouping_type = GroupingType.objects.create(code="TOR", name="Torre")
    group_t1 = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="T1")
    group_t2 = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T2", name="T2")
    unit_t1 = PropertyUnit.objects.create(project=project, structural_group=group_t1, code="408", name="408")
    unit_t2 = PropertyUnit.objects.create(project=project, structural_group=group_t2, code="408", name="408")
    client = Client.objects.create(
        first_names="ANA",
        last_names_or_company="UNO",
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="4081",
        phone="3004084081",
    )
    assignment_1 = FiduciaryAssignment.objects.create(property_unit=unit_t1, assignment_number="EF-T1-408", start_date="2026-01-01")
    assignment_2 = FiduciaryAssignment.objects.create(property_unit=unit_t2, assignment_number="EF-T2-408", start_date="2026-01-01")
    UnitOwnership.objects.create(property_unit=unit_t1, client=client, is_primary=True, start_date="2026-01-01")
    UnitOwnership.objects.create(property_unit=unit_t2, client=client, is_primary=True, start_date="2026-01-01")
    FiduciaryAssignmentHolder.objects.create(assignment=assignment_1, client=client, is_primary=True, start_date="2026-01-01")
    FiduciaryAssignmentHolder.objects.create(assignment=assignment_2, client=client, is_primary=True, start_date="2026-01-01")
    ImportedHistoricalObservation.objects.create(
        property_unit=unit_t1,
        assignment=assignment_1,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        detail="OBS T1",
        imported_by=accounting_admin_user,
    )
    ImportedHistoricalObservation.objects.create(
        property_unit=unit_t2,
        assignment=assignment_2,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        detail="OBS T2",
        imported_by=accounting_admin_user,
    )
    OperationalNovelty.objects.create(
        project=project,
        property_unit=unit_t2,
        novelty_type=OperationalNovelty.NoveltyType.HISTORICAL,
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.DESCRIPTIVE,
        detail="NOVEDAD T2",
        created_by=accounting_admin_user,
    )
    imported_file = ImportedFile.objects.create(
        batch=ImportBatch.objects.create(
            initiated_by=accounting_admin_user,
            import_type=ImportBatch.ImportType.HISTORICAL,
            load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        ),
        original_name="historico.xlsx",
        extension=".xlsx",
        size_bytes=1,
        sha256="f" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    Payment.objects.create(
        assignment=assignment_2,
        amount=1000,
        period_year=2026,
        period_month=1,
        date_precision=Payment.DatePrecision.MONTH,
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T2",
        source_row=10,
    )

    assignment_content = accounting_client.get(reverse("fiduciary:assignment_list"), {"project": project.pk}).content.decode()
    ownership_response = accounting_client.get(reverse("fiduciary:ownership_list"))
    observation_content = accounting_client.get(reverse("fiduciary:observation_list"), {"project": project.pk}).content.decode()
    novelty_content = accounting_client.get(reverse("fiduciary:novelty_list"), {"project": project.pk}).content.decode()
    payment_content = accounting_client.get(reverse("fiduciary:payment_list"), {"project": project.pk}).content.decode()
    client_content = accounting_client.get(reverse("fiduciary:client_detail", args=[client.pk])).content.decode()
    assignment_detail_content = accounting_client.get(reverse("fiduciary:assignment_detail", args=[assignment_2.pk])).content.decode()
    unit_history_content = accounting_client.get(reverse("real_estate:property_unit_history", args=[unit_t2.pk])).content.decode()

    assert "<th>Agrupacion</th>" in assignment_content
    assert "T1" in assignment_content and "T2" in assignment_content
    assert ownership_response.status_code == 302
    assert ownership_response.url == reverse("fiduciary:assignment_list")
    assert "<th>Agrupacion</th>" in observation_content
    assert "OBS T1" in observation_content and "OBS T2" in observation_content and "T1" in observation_content and "T2" in observation_content
    assert "<th>Agrupacion</th>" in novelty_content
    assert "NOVEDAD T2" in novelty_content and "T2" in novelty_content
    assert "<th>Agrupacion</th>" in payment_content
    assert "EF-T2-408" in payment_content and "T2" in payment_content
    assert "<th>Agrupacion</th>" in client_content
    assert "T1" in client_content and "T2" in client_content
    assert "T2 - 408" in assignment_detail_content or "T2 | 408" in assignment_detail_content
    assert "Proyecto 408" in unit_history_content
    assert "T2" in unit_history_content
    assert "408" in unit_history_content
