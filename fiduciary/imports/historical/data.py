from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CellData:
    row: int
    column: int
    letter: str
    coordinate: str
    value: Any = None
    formula: str | None = None
    has_cached_value: bool = False
    is_date: bool = False

    @property
    def has_formula(self) -> bool:
        return bool(self.formula)


@dataclass(frozen=True)
class ParserIssue:
    code: str
    severity: str
    message: str
    sheet_name: str | None = None
    row_number: int | None = None
    column_letter: str | None = None


@dataclass(frozen=True)
class DetectedColumn:
    key: str
    header: str
    normalized_header: str
    index: int
    letter: str


@dataclass(frozen=True)
class DetectedPaymentColumn(DetectedColumn):
    month: int = 0
    year: int = 0


@dataclass(frozen=True)
class HistoricalClient:
    order: int
    name: str
    document_number: str | None = None
    document_type: str | None = None
    is_primary: bool = False


@dataclass(frozen=True)
class HistoricalAssignment:
    assignment_number: str
    status: str | None = None


@dataclass(frozen=True)
class HistoricalMonthlyPayment:
    month: int
    year: int
    amount: Decimal
    source_row: int
    source_column: str
    source_header: str
    has_formula: bool = False
    has_cached_value: bool = False


@dataclass(frozen=True)
class HistoricalRow:
    sheet_name: str
    row_number: int
    project: str
    grouping_type: str | None
    grouping_code: str
    grouping_name: str
    unit_code: str | None
    unit_name: str | None
    assignment: HistoricalAssignment | None
    clients: list[HistoricalClient] = field(default_factory=list)
    payments: list[HistoricalMonthlyPayment] = field(default_factory=list)


@dataclass(frozen=True)
class SheetData:
    name: str
    index: int
    visibility: str
    used_rows: int
    used_columns: int
    classification: str
    header_row: int | None = None
    columns: dict[str, DetectedColumn] = field(default_factory=dict)
    payment_columns: list[DetectedPaymentColumn] = field(default_factory=list)
    rows: list[HistoricalRow] = field(default_factory=list)
    ignored_rows: int = 0
    ignored_row_reasons: dict[str, int] = field(default_factory=dict)
    issues: list[ParserIssue] = field(default_factory=list)


@dataclass(frozen=True)
class ParseStatistics:
    sheets_total: int = 0
    sheets_processed: int = 0
    valid_rows: int = 0
    ignored_rows: int = 0
    client_appearances_found: int = 0
    distinct_assignments_found: int = 0
    payment_entries_found: int = 0
    payment_columns_detected: int = 0
    issues_found: int = 0


@dataclass(frozen=True)
class WorkbookData:
    path: Path
    file_type: str
    sheets: list[SheetData] = field(default_factory=list)
    issues: list[ParserIssue] = field(default_factory=list)
    statistics: ParseStatistics = field(default_factory=ParseStatistics)
