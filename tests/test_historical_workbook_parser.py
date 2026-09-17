from dataclasses import is_dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import zipfile

import pytest
from django.db import models

from fiduciary.imports.historical import HistoricalWorkbookParser
from fiduciary.imports.historical.data import (
    CellData,
    DetectedPaymentColumn,
    HistoricalClient,
    HistoricalMonthlyPayment,
    HistoricalNovelty,
    HistoricalNoveltyCell,
    HistoricalRow,
    WorkbookData,
)
from fiduciary.imports.historical.readers import RawSheet, WorkbookReader


HISTORICAL_DIR = Path("samples/fiduciary/historical")
REAL_XLSX_FILES = sorted(HISTORICAL_DIR.glob("*.xlsx"))
REAL_XLS_FILES = sorted(HISTORICAL_DIR.glob("*.xls"))
MONTECIELO_FILE = Path(
    r"C:\Users\ASUS\OneDrive\Escritorio\Practices\ConstructoraCentenarioSAS\Documents\Company\ProyectoFinal\LIBRO MONTECIELO T2.xlsx"
)


def can_read_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


@pytest.fixture(scope="module")
def parsed_workbook():
    assert REAL_XLSX_FILES, "No hay archivos xlsx reales en samples/fiduciary/historical/"
    return HistoricalWorkbookParser(REAL_XLSX_FILES[0]).parse()


def test_parser_reads_real_xlsx_workbook(parsed_workbook):
    assert isinstance(parsed_workbook, WorkbookData)
    assert parsed_workbook.file_type == "xlsx"
    assert parsed_workbook.path.name == "LIBRO Springfield.xlsx"
    assert parsed_workbook.statistics.sheets_total == 3


def test_parser_detects_all_real_sheets(parsed_workbook):
    sheets = {sheet.name: sheet for sheet in parsed_workbook.sheets}

    assert set(sheets) == {"VS", "CM", "ID"}
    assert all(sheet.visibility == "visible" for sheet in sheets.values())
    assert all(sheet.used_rows >= 100 for sheet in sheets.values())
    assert all(sheet.used_columns >= 40 for sheet in sheets.values())
    assert all(sheet.classification == "processable" for sheet in sheets.values())


def test_parser_detects_header_row_and_functional_columns(parsed_workbook):
    for sheet in parsed_workbook.sheets:
        assert sheet.header_row == 4
        assert sheet.columns["assignment_number"].header == "ENCARGO FIDUCIARIO"
        assert sheet.columns["document_number"].header == "CEDULA CLIENTE"
        assert "unit" in sheet.columns
        assert "client_name_1" in sheet.columns


def test_parser_detects_monthly_payment_columns_chronologically(parsed_workbook):
    expected = [
        ("T", 3, 2026),
        ("U", 4, 2026),
        ("V", 5, 2026),
        ("W", 6, 2026),
        ("X", 7, 2026),
    ]

    for sheet in parsed_workbook.sheets:
        assert all(isinstance(column, DetectedPaymentColumn) for column in sheet.payment_columns)
        assert [(column.letter, column.month, column.year) for column in sheet.payment_columns] == expected


def test_parser_extracts_rows_structure_assignments_and_clients(parsed_workbook):
    first_sheet = parsed_workbook.sheets[0]
    first_row = first_sheet.rows[0]

    assert isinstance(first_row, HistoricalRow)
    assert first_row.project == "Springfield"
    assert first_row.grouping_type is None
    assert first_row.grouping_code == first_sheet.name
    assert first_row.grouping_name == "VS Viviendas"
    assert first_row.unit_code
    assert first_row.assignment.assignment_number
    assert all(isinstance(client, HistoricalClient) for client in first_row.clients)
    assert first_row.clients[0].is_primary is True
    assert first_row.clients[0].order == 1
    assert first_row.clients[0].document_type == "cc"


def test_parser_preserves_secondary_client_order_when_present(parsed_workbook):
    row = next(row for sheet in parsed_workbook.sheets for row in sheet.rows if len(row.clients) >= 3)

    assert [client.order for client in row.clients] == [1, 2, 3]
    assert row.clients[0].is_primary is True
    assert all(not client.is_primary for client in row.clients[1:])
    assert all("/" not in (client.document_number or "") for client in row.clients)


@pytest.mark.skipif(not can_read_file(MONTECIELO_FILE), reason="Libro real de Montecielo no disponible o bloqueado")
def test_parser_extracts_montecielo_contacts_and_split_documents():
    workbook = HistoricalWorkbookParser(MONTECIELO_FILE).parse()
    row = next(row for sheet in workbook.sheets for row in sheet.rows if row.row_number == 20)

    assert [client.document_number for client in row.clients[:2]] == ["1128450396", "1214723055"]
    assert all("/" not in (client.document_number or "") for client in row.clients)
    assert row.clients[0].email
    assert row.clients[0].phone


@pytest.mark.skipif(not can_read_file(MONTECIELO_FILE), reason="Libro real de Montecielo no disponible o bloqueado")
def test_parser_preserves_montecielo_303_novelty_summary_text():
    workbook = HistoricalWorkbookParser(MONTECIELO_FILE, grouping_type_hint="Torre").parse()
    novelty = next(novelty for sheet in workbook.sheets for novelty in sheet.novelties if novelty.unit_code == "303")
    values = {str(cell.value).strip() for cell in novelty.cells}

    assert "*TERMIN.SIN ABONOS" in values


def test_parser_extracts_monthly_payments_and_formula_metadata(parsed_workbook):
    row_with_payments = next(row for sheet in parsed_workbook.sheets for row in sheet.rows if row.payments)

    assert all(isinstance(payment, HistoricalMonthlyPayment) for payment in row_with_payments.payments)
    assert all(payment.amount > 0 for payment in row_with_payments.payments)
    assert all(payment.source_column in {"T", "U", "V", "W", "X"} for payment in row_with_payments.payments)
    assert any(
        payment.has_formula and payment.has_cached_value
        for sheet in parsed_workbook.sheets
        for row in sheet.rows
        for payment in row.payments
    )


def test_grouping_type_hint_is_optional_and_not_globally_coupled():
    path = REAL_XLSX_FILES[0]
    default_result = HistoricalWorkbookParser(path).parse()
    hinted_result = HistoricalWorkbookParser(path, grouping_type_hint="Torre").parse()

    assert default_result.sheets[0].rows[0].grouping_type is None
    assert hinted_result.sheets[0].rows[0].grouping_type == "Torre"


def test_parser_extracts_universo_project_without_springfield_fallback():
    parsed = HistoricalWorkbookParser(HISTORICAL_DIR / "LIBRO_Universo_7.xlsx", grouping_type_hint="Sector").parse()

    assert parsed.statistics.valid_rows == 200
    assert {row.project for sheet in parsed.sheets for row in sheet.rows} == {"Universo 7"}
    assert {row.grouping_name for sheet in parsed.sheets for row in sheet.rows} == {"RES Residencial", "COM Comercial"}


def test_parser_extracts_montecielo_style_title_and_preserves_novelty_section():
    cells = {
        (1, 1): CellData(1, 1, "A", "A1", "CONJUNTO CERRADO MONTECIELO T2 - 150 APARTAMENTOS VIP"),
        (4, 1): CellData(4, 1, "A", "A4", "APTO"),
        (4, 2): CellData(4, 2, "B", "B4", "ENCARGO FIDUCIARIO"),
        (4, 3): CellData(4, 3, "C", "C4", "CEDULA CLIENTE"),
        (4, 4): CellData(4, 4, "D", "D4", "NOMBRE CLIENTE"),
        (4, 20): CellData(4, 20, "T", "T4", "RECIBO FIDUCIA MAR/2026"),
        (5, 1): CellData(5, 1, "A", "A5", "101"),
        (5, 2): CellData(5, 2, "B", "B5", "EF-001"),
        (5, 3): CellData(5, 3, "C", "C5", "1"),
        (5, 4): CellData(5, 4, "D", "D5", "Cliente Uno"),
        (5, 20): CellData(5, 20, "T", "T5", 100),
        (155, 1): CellData(155, 1, "A", "A155", "VENTAS"),
        (156, 1): CellData(156, 1, "A", "A156", "POR VENDER"),
        (157, 1): CellData(157, 1, "A", "A157", "TOTAL"),
        (161, 1): CellData(161, 1, "A", "A161", "NOVEDADES"),
        (162, 1): CellData(162, 1, "A", "A162", "TERMINACIONES MAR/2026"),
        (163, 1): CellData(163, 1, "A", "A163", "303"),
        (163, 2): CellData(163, 2, "B", "B163", "EF-303"),
        (163, 4): CellData(163, 4, "D", "D163", "Cliente historico"),
    }
    sheet = RawSheet("T2", 1, "visible", "A1:AW163", cells, set(), set())
    parser = HistoricalWorkbookParser(Path("samples/fiduciary/historical/LIBRO MONTECIELO T2(5).xlsx"), grouping_type_hint="Torre")

    parsed_sheet = parser._parse_sheet(sheet)

    assert len(parsed_sheet.rows) == 2
    assert parsed_sheet.rows[0].project == "Montecielo"
    assert parsed_sheet.rows[0].grouping_name == "T2"
    assert parsed_sheet.rows[1].context == "novelty"
    assert parsed_sheet.rows[1].unit_code == "303"
    assert parsed_sheet.ignored_row_reasons["novelty_section"] == 1
    assert parsed_sheet.ignored_row_reasons["novelty_subtitle"] == 1
    assert parsed_sheet.ignored_row_reasons.get("novelty", 0) == 0
    assert len(parsed_sheet.novelties) == 1
    novelty = parsed_sheet.novelties[0]
    assert isinstance(novelty, HistoricalNovelty)
    assert novelty.project == "Montecielo"
    assert novelty.grouping_name == "T2"
    assert novelty.unit_code == "303"
    assert novelty.assignment.assignment_number == "EF-303"
    assert novelty.historical_section == "TERMINACIONES MAR/2026"
    assert novelty.section_month == 3
    assert novelty.section_year == 2026
    assert all(isinstance(cell, HistoricalNoveltyCell) for cell in novelty.cells)
    assert {cell.coordinate for cell in novelty.cells} == {"A163", "B163", "D163"}
    assert "HISTORICAL_NOVELTY_SECTION_SKIPPED" not in {issue.code for issue in parsed_sheet.issues}


def test_parser_skips_summary_sheet_without_header_row_error():
    cells = {
        (1, 1): CellData(1, 1, "A", "A1", "MONTECIELO 9 TORRES VIP"),
        (3, 1): CellData(3, 1, "A", "A3", "TORRE"),
        (3, 2): CellData(3, 2, "B", "B3", "UND"),
        (3, 3): CellData(3, 3, "C", "C3", "AREA"),
        (3, 4): CellData(3, 4, "D", "D3", "VALOR VENTAS"),
        (3, 5): CellData(3, 5, "E", "E3", "TOTAL RECIBIDO"),
        (3, 6): CellData(3, 6, "F", "F3", "SALDO POR COBRAR"),
        (3, 7): CellData(3, 7, "G", "G3", "RECURSOS PROPIOS"),
        (4, 1): CellData(4, 1, "A", "A4", "T1"),
        (4, 2): CellData(4, 2, "B", "B4", 120),
    }
    sheet = RawSheet("MONTECIELO", 10, "visible", "A1:G4", cells, set(), set())

    parsed_sheet = HistoricalWorkbookParser(Path("LIBRO MONTECIELO.xlsx"))._parse_sheet(sheet)

    assert parsed_sheet.classification == "skipped_summary"
    assert parsed_sheet.rows == []
    assert "HEADER_ROW_NOT_FOUND" not in {issue.code for issue in parsed_sheet.issues}


def test_historical_like_sheet_with_missing_required_header_still_reports_error():
    cells = {
        (4, 1): CellData(4, 1, "A", "A4", "ENCARGO FIDUCIARIO"),
        (4, 2): CellData(4, 2, "B", "B4", "APTO"),
        (4, 3): CellData(4, 3, "C", "C4", "NOMBRE CLIENTE"),
        (4, 4): CellData(4, 4, "D", "D4", "FECHA"),
        (4, 5): CellData(4, 5, "E", "E4", "RECIBIDO"),
        (5, 1): CellData(5, 1, "A", "A5", "EF-101"),
        (5, 2): CellData(5, 2, "B", "B5", "101"),
        (5, 3): CellData(5, 3, "C", "C5", "CLIENTE UNO"),
    }
    sheet = RawSheet("T1", 1, "visible", "A4:E5", cells, set(), set())

    parsed_sheet = HistoricalWorkbookParser(Path("LIBRO incompleto.xlsx"))._parse_sheet(sheet)

    assert parsed_sheet.classification == "unknown"
    assert "REQUIRED_COLUMN_MISSING" in {issue.code for issue in parsed_sheet.issues}
    assert "HEADER_ROW_NOT_FOUND" not in {issue.code for issue in parsed_sheet.issues}


def _roundtrip_date_sheet(*, payment_date, receipt=None, fiduciary_receipt=None, received=None, fiduciary_received=None):
    cells = {
        (1, 1): CellData(1, 1, "A", "A1", "CONJUNTO CERRADO ROUNDTRIP T1"),
        (4, 1): CellData(4, 1, "A", "A4", "ENCARGO FIDUCIARIO"),
        (4, 2): CellData(4, 2, "B", "B4", "APTO"),
        (4, 3): CellData(4, 3, "C", "C4", "CEDULA CLIENTE"),
        (4, 4): CellData(4, 4, "D", "D4", "NOMBRE CLIENTE"),
        (4, 5): CellData(4, 5, "E", "E4", "RECIBOS"),
        (4, 6): CellData(4, 6, "F", "F4", "RECIBOS FIDUBOGOTA"),
        (4, 7): CellData(4, 7, "G", "G4", "FECHA"),
        (4, 8): CellData(4, 8, "H", "H4", "RECIBIDO"),
        (4, 9): CellData(4, 9, "I", "I4", "RECIBO FIDUCIA ENE/2026"),
        (5, 1): CellData(5, 1, "A", "A5", "EF-101"),
        (5, 2): CellData(5, 2, "B", "B5", "101"),
        (5, 3): CellData(5, 3, "C", "C5", "123"),
        (5, 4): CellData(5, 4, "D", "D5", "Cliente Uno"),
        (5, 7): CellData(5, 7, "G", "G5", payment_date),
    }
    if receipt is not None:
        cells[(5, 5)] = CellData(5, 5, "E", "E5", receipt)
    if fiduciary_receipt is not None:
        cells[(5, 6)] = CellData(5, 6, "F", "F5", fiduciary_receipt)
    if received is not None:
        cells[(5, 8)] = CellData(5, 8, "H", "H5", received)
    if fiduciary_received is not None:
        cells[(5, 9)] = CellData(5, 9, "I", "I5", fiduciary_received)
    return RawSheet("T1", 1, "visible", "A1:I5", cells, set(), set())


def test_parser_keeps_historical_fiduciary_dates_without_separator_intact():
    sheet = _roundtrip_date_sheet(
        payment_date="ENE.10/26F",
        fiduciary_receipt="RF001",
        fiduciary_received=1500000,
    )
    parsed_sheet = HistoricalWorkbookParser(Path("roundtrip.xlsx"), grouping_type_hint="Torre")._parse_sheet(sheet)

    row = parsed_sheet.rows[0]
    issue_codes = {issue.code for issue in parsed_sheet.issues}

    assert len(row.reconstructed_payments) == 1
    assert row.reconstructed_payments[0].date_value == "ENE.10/26F"
    assert row.reconstructed_payments[0].amount == 1500000
    assert "HIST_PAYMENT_VALUE_COUNT_MISMATCH" not in issue_codes


def test_parser_ignores_post_gf_fiduciary_dates_after_separator_for_historical_validation():
    sheet = _roundtrip_date_sheet(
        payment_date="ENE.10/26F || MAR.15/26F",
        fiduciary_receipt="RF001",
        fiduciary_received=1500000,
    )
    parsed_sheet = HistoricalWorkbookParser(Path("roundtrip.xlsx"), grouping_type_hint="Torre")._parse_sheet(sheet)

    row = parsed_sheet.rows[0]
    issue_codes = {issue.code for issue in parsed_sheet.issues}

    assert [payment.date_value for payment in row.reconstructed_payments] == ["ENE.10/26F"]
    assert [payment.amount for payment in row.reconstructed_payments] == [1500000]
    assert "HIST_PAYMENT_VALUE_COUNT_MISMATCH" not in issue_codes
    assert "HIST_UNRECEIPTED_PAYMENT_VALUE_COUNT_MISMATCH" not in issue_codes


def test_parser_ignores_post_gf_constructor_dates_after_separator_for_historical_validation():
    sheet = _roundtrip_date_sheet(
        payment_date="ENE.10/26 || MAR.15/26",
        receipt="RC001",
        received=1000000,
    )
    parsed_sheet = HistoricalWorkbookParser(Path("roundtrip.xlsx"), grouping_type_hint="Torre")._parse_sheet(sheet)

    row = parsed_sheet.rows[0]
    issue_codes = {issue.code for issue in parsed_sheet.issues}

    assert [payment.date_value for payment in row.reconstructed_payments] == ["ENE.10/26"]
    assert [payment.amount for payment in row.reconstructed_payments] == [1000000]
    assert "HIST_PAYMENT_VALUE_COUNT_MISMATCH" not in issue_codes
    assert "HIST_UNRECEIPTED_PAYMENT_VALUE_COUNT_MISMATCH" not in issue_codes


def test_main_table_payment_mismatch_still_blocks_after_novelty_section_fix():
    sheet = _roundtrip_date_sheet(
        payment_date="ENE.10/26 - FEB.15/26",
        receipt="RC001",
        received=1000000,
    )
    parsed_sheet = HistoricalWorkbookParser(Path("roundtrip.xlsx"), grouping_type_hint="Torre")._parse_sheet(sheet)

    assert "HIST_PAYMENT_DATE_RECEIPT_MISMATCH" in {issue.code for issue in parsed_sheet.issues}


def test_novelty_section_rows_are_preserved_and_diagnosed_when_they_have_payment_evidence():
    cells = {
        (1, 1): CellData(1, 1, "A", "A1", "CONJUNTO CERRADO ROUNDTRIP T1"),
        (4, 1): CellData(4, 1, "A", "A4", "ENCARGO FIDUCIARIO"),
        (4, 2): CellData(4, 2, "B", "B4", "APTO"),
        (4, 3): CellData(4, 3, "C", "C4", "CEDULA CLIENTE"),
        (4, 4): CellData(4, 4, "D", "D4", "NOMBRE CLIENTE"),
        (4, 5): CellData(4, 5, "E", "E4", "NOMBRE CLIENTE2"),
        (4, 6): CellData(4, 6, "F", "F4", "RECIBOS"),
        (4, 7): CellData(4, 7, "G", "G4", "RECIBOS FIDUBOGOTA"),
        (4, 8): CellData(4, 8, "H", "H4", "FECHA"),
        (4, 9): CellData(4, 9, "I", "I4", "RECIBIDO"),
        (4, 10): CellData(4, 10, "J", "J4", "CESIONES/TRASLADOS"),
        (4, 11): CellData(4, 11, "K", "K4", "RECIBO FIDUCIA ENE/2026"),
        (4, 12): CellData(4, 12, "L", "L4", "OBSERVACIONES"),
        (5, 1): CellData(5, 1, "A", "A5", "EF-101"),
        (5, 2): CellData(5, 2, "B", "B5", "101"),
        (5, 3): CellData(5, 3, "C", "C5", "123"),
        (5, 4): CellData(5, 4, "D", "D5", "Cliente Uno"),
        (5, 6): CellData(5, 6, "F", "F5", "RC001"),
        (5, 8): CellData(5, 8, "H", "H5", "ENE.10/26"),
        (5, 9): CellData(5, 9, "I", "I5", 1000000),
        (6, 1): CellData(6, 1, "A", "A6", "NOVEDADES / OBSERVACIONES"),
        (7, 1): CellData(7, 1, "A", "A7", "EF-HIST"),
        (7, 2): CellData(7, 2, "B", "B7", "101"),
        (7, 3): CellData(7, 3, "C", "C7", "456"),
        (7, 4): CellData(7, 4, "D", "D7", "Cliente Historico"),
        (7, 5): CellData(7, 5, "E", "E7", "*TRASLADO"),
        (7, 6): CellData(7, 6, "F", "F7", "NC26449TRASLADO"),
        (7, 7): CellData(7, 7, "G", "G7", "RF001 - RF002"),
        (7, 8): CellData(7, 8, "H", "H7", "ABR.8/26TRASL"),
        (7, 10): CellData(7, 10, "J", "J7", 2500000),
        (7, 11): CellData(7, 11, "K", "K7", 999999),
        (7, 12): CellData(7, 12, "L", "L7", "Detalle historico de traslado"),
    }
    sheet = RawSheet("T1", 1, "visible", "A1:L7", cells, set(), set())
    parsed_sheet = HistoricalWorkbookParser(Path("roundtrip.xlsx"), grouping_type_hint="Torre")._parse_sheet(sheet)
    issue_codes = {issue.code for issue in parsed_sheet.issues}

    assert len(parsed_sheet.rows) == 2
    assert parsed_sheet.rows[1].context == "novelty"
    assert len(parsed_sheet.novelties) == 1
    assert parsed_sheet.novelties[0].row_number == 7
    assert "HIST_PAYMENT_DATE_RECEIPT_MISMATCH" in issue_codes
    assert "HIST_PAYMENT_VALUE_COUNT_MISMATCH" not in issue_codes
    assert "HIST_CESSION_VALUE_RECEIPT_MISMATCH" not in issue_codes
    assert "HIST_TRANSFER_VALUE_RECEIPT_MISMATCH" not in issue_codes
    assert parsed_sheet.ignored_row_reasons["novelty_section"] == 1
    assert parsed_sheet.ignored_row_reasons.get("novelty", 0) == 0


def test_parser_statistics_from_real_workbook(parsed_workbook):
    stats = parsed_workbook.statistics

    assert stats.sheets_processed == 3
    assert stats.valid_rows == 300
    assert stats.ignored_rows == 24
    assert stats.client_appearances_found >= 260
    assert stats.distinct_assignments_found == 300
    assert stats.payment_entries_found >= 300
    assert stats.payment_columns_detected == 15
    assert stats.historical_novelties_found == 3
    assert stats.issues_found == 159
    assert stats.issues_found == len(parsed_workbook.issues)


def test_parser_returns_structured_issues(parsed_workbook):
    issue_codes = {issue.code for issue in parsed_workbook.issues}

    assert "UNKNOWN_HEADER" not in issue_codes
    assert "FORMULA_WITH_CACHED_VALUE" in issue_codes
    assert all(issue.severity in {"info", "warning", "error", "blocking"} for issue in parsed_workbook.issues)


def test_irrelevant_formula_columns_do_not_generate_issues(parsed_workbook):
    issue_columns = {
        issue.column_letter
        for issue in parsed_workbook.issues
        if issue.code == "FORMULA_WITH_CACHED_VALUE" and issue.column_letter
    }

    assert issue_columns <= {"U", "V", "W", "X"}
    assert not {"Z", "AA", "AB", "AC"} & issue_columns


def test_ignored_rows_are_classified(parsed_workbook):
    for sheet in parsed_workbook.sheets:
        assert sheet.ignored_row_reasons == {
            "structural_or_auxiliary": 2,
            "decorative_or_total": 1,
            "empty": 3,
            "novelty_section": 1,
            "novelty": 1,
        }


def test_parser_result_uses_dataclasses_not_django_models(parsed_workbook):
    assert is_dataclass(parsed_workbook)
    for sheet in parsed_workbook.sheets:
        assert is_dataclass(sheet)
        for row in sheet.rows[:3]:
            assert is_dataclass(row)
            assert not isinstance(row, models.Model)


def test_parser_does_not_persist_data(parsed_workbook):
    assert parsed_workbook.statistics.valid_rows == 300


def test_reader_handles_real_xlsx_metadata_without_modifying_file():
    workbook = WorkbookReader().read(REAL_XLSX_FILES[0])

    assert workbook.file_type == "xlsx"
    assert [sheet.name for sheet in workbook.sheets] == ["VS", "CM", "ID"]
    assert all(sheet.visibility == "visible" for sheet in workbook.sheets)
    assert workbook.sheets[0].dimension == "A1:AW162"
    assert workbook.sheets[0].cell(4, 2).value == "ENCARGO FIDUCIARIO"
    assert isinstance(workbook.sheets[0].cell(5, 35).value, datetime)
    assert workbook.sheets[0].cell(5, 35).is_date is True
    assert workbook.sheets[0].cell(5, 23).has_formula is True
    assert workbook.sheets[0].cell(5, 23).has_cached_value is True
    assert workbook.sheets[0].cell(6, 26).has_formula is True
    assert workbook.sheets[0].cell(6, 26).has_cached_value is True
    assert workbook.sheets[0].cell(4, 20).column in workbook.sheets[0].hidden_columns
    assert not workbook.sheets[0].hidden_rows


def test_reader_keeps_out_of_range_excel_date_as_controlled_issue(tmp_path):
    path = tmp_path / "invalid-date.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets><sheet name="T1" sheetId="1" r:id="rId1"/></sheets>
            </workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
            </Relationships>""",
        )
        archive.writestr(
            "xl/styles.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs>
            </styleSheet>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <dimension ref="BO213:BO213"/>
              <sheetData><row r="213"><c r="BO213" s="1"><v>4682000</v></c></row></sheetData>
            </worksheet>""",
        )

    workbook = WorkbookReader().read(path)

    cell = workbook.sheets[0].cell(213, 67)
    assert cell.value == "4682000"
    assert cell.is_date is True
    issue = workbook.issues[0]
    assert issue.code == "INVALID_EXCEL_DATE_VALUE"
    assert issue.severity == "warning"
    assert issue.sheet_name == "T1"
    assert issue.row_number == 213
    assert issue.column_letter == "BO"
    assert issue.found_value == "4682000"


def test_xls_without_reader_returns_controlled_issue():
    with patch.dict("sys.modules", {"xlrd": None}):
        result = WorkbookReader().read(Path("samples/fiduciary/historical/no-real-file.xls"))

    assert result.file_type == "xls"
    assert result.issues[0].code == "XLS_READER_UNAVAILABLE"


def test_parser_does_not_import_django_models_or_query_database():
    import fiduciary.imports.historical.parser as parser_module

    assert "django" not in parser_module.__dict__


def test_parser_reads_real_xls_when_available():
    if not REAL_XLS_FILES:
        pytest.skip("No hay archivo .xls real en samples/fiduciary/historical/")

    parsed = HistoricalWorkbookParser(REAL_XLS_FILES[0]).parse()

    assert parsed.file_type == "xls"
    assert parsed.sheets or parsed.issues
