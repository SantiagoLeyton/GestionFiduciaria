from dataclasses import is_dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from django.db import models

from fiduciary.imports.historical import HistoricalWorkbookParser
from fiduciary.imports.historical.data import (
    DetectedPaymentColumn,
    HistoricalClient,
    HistoricalMonthlyPayment,
    HistoricalRow,
    WorkbookData,
)
from fiduciary.imports.historical.readers import WorkbookReader


HISTORICAL_DIR = Path("samples/fiduciary/historical")
REAL_XLSX_FILES = sorted(HISTORICAL_DIR.glob("*.xlsx"))
REAL_XLS_FILES = sorted(HISTORICAL_DIR.glob("*.xls"))


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
    assert all(client.document_number is None for client in row.clients[1:])


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


def test_parser_statistics_from_real_workbook(parsed_workbook):
    stats = parsed_workbook.statistics

    assert stats.sheets_processed == 3
    assert stats.valid_rows == 303
    assert stats.ignored_rows == 21
    assert stats.client_appearances_found >= 260
    assert stats.distinct_assignments_found == 301
    assert stats.payment_entries_found >= 300
    assert stats.payment_columns_detected == 15
    assert stats.issues_found == 18
    assert stats.issues_found == len(parsed_workbook.issues)


def test_parser_returns_structured_issues(parsed_workbook):
    issue_codes = {issue.code for issue in parsed_workbook.issues}

    assert "UNKNOWN_HEADER" not in issue_codes
    assert "FORMULA_WITH_CACHED_VALUE" in issue_codes
    assert all(issue.severity in {"info", "warning", "error", "blocking"} for issue in parsed_workbook.issues)


def test_irrelevant_formula_columns_do_not_generate_issues(parsed_workbook):
    issue_columns = {issue.column_letter for issue in parsed_workbook.issues if issue.column_letter}

    assert issue_columns <= {"U", "V", "W", "X"}
    assert not {"Z", "AA", "AB", "AC"} & issue_columns


def test_ignored_rows_are_classified(parsed_workbook):
    for sheet in parsed_workbook.sheets:
        assert sheet.ignored_row_reasons == {
            "invalid": 2,
            "decorative_or_total": 1,
            "empty": 3,
            "empty_after_extraction": 1,
        }


def test_parser_result_uses_dataclasses_not_django_models(parsed_workbook):
    assert is_dataclass(parsed_workbook)
    for sheet in parsed_workbook.sheets:
        assert is_dataclass(sheet)
        for row in sheet.rows[:3]:
            assert is_dataclass(row)
            assert not isinstance(row, models.Model)


def test_parser_does_not_persist_data(parsed_workbook):
    assert parsed_workbook.statistics.valid_rows == 303


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
    assert workbook.sheets[0].cell(4, 20).column in workbook.sheets[0].hidden_columns
    assert not workbook.sheets[0].hidden_rows


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
