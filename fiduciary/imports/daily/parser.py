from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fiduciary.imports.historical.normalize import normalize_text
from fiduciary.imports.historical.readers import WorkbookReader


@dataclass(frozen=True)
class DailyReportIssue:
    code: str
    message: str
    sheet_name: str | None = None
    row_number: int | None = None


@dataclass(frozen=True)
class ParsedDailyReportRow:
    sheet_name: str
    row_number: int
    original_assignment_number: str
    normalized_assignment_number: str
    payment_date: date | None
    amount: Decimal | None
    movement_type: str
    payer_name: str = ""
    payer_document: str = ""
    concept: str = ""
    original_data: dict[str, str] = field(default_factory=dict)
    status_hint: str = "valid"
    message: str = ""


@dataclass(frozen=True)
class ParsedDailyReportSheet:
    name: str
    index: int
    header_row: int | None
    rows: list[ParsedDailyReportRow] = field(default_factory=list)
    issues: list[DailyReportIssue] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedDailyReport:
    path: Path
    sheets: list[ParsedDailyReportSheet]
    issues: list[DailyReportIssue]

    @property
    def rows(self) -> list[ParsedDailyReportRow]:
        return [row for sheet in self.sheets for row in sheet.rows]


class DailyReportParser:
    REQUIRED_FIELDS = {"assignment_number", "payment_date", "amount"}
    HEADER_ALIASES = {
        "assignment_number": {
            "n encargo",
            "no encargo",
            "numero encargo",
            "numero de encargo",
            "encargo",
            "encargo fiduciario",
            "n encargo fiduciario",
        },
        "payment_date": {"fecha mov", "fecha movimiento", "fecha de movimiento", "fecha pago", "fecha de pago"},
        "amount": {"adicion", "adicion pago", "valor", "valor pago", "valor recibido"},
        "withdrawal": {"retiro", "valor retiro"},
        "payer_name": {"adquiriente", "pagador", "cliente", "nombre cliente", "nombre pagador"},
        "payer_document": {"identificacion", "documento", "documento cliente", "identificacion pagador"},
        "concept": {"concepto", "descripcion", "detalle"},
    }

    def __init__(self, path):
        self.path = Path(path)

    def parse(self) -> ParsedDailyReport:
        workbook = WorkbookReader().read(self.path)
        issues = [
            DailyReportIssue(code=issue.code, message=issue.message, sheet_name=issue.sheet_name, row_number=issue.row_number)
            for issue in workbook.issues
        ]
        parsed_sheets = []
        for sheet in workbook.sheets:
            parsed_sheets.append(self._parse_sheet(sheet))
        issues.extend(issue for sheet in parsed_sheets for issue in sheet.issues)
        return ParsedDailyReport(path=self.path, sheets=parsed_sheets, issues=issues)

    def _parse_sheet(self, sheet) -> ParsedDailyReportSheet:
        header_row, columns, issues = self._detect_headers(sheet)
        if not header_row:
            return ParsedDailyReportSheet(name=sheet.name, index=sheet.index, header_row=None, issues=issues)
        rows = []
        for row_number in range(header_row + 1, sheet.used_rows + 1):
            if self._is_empty_row(sheet, row_number):
                continue
            parsed = self._parse_row(sheet, row_number, columns)
            if parsed:
                rows.append(parsed)
        return ParsedDailyReportSheet(name=sheet.name, index=sheet.index, header_row=header_row, rows=rows, issues=issues)

    def _detect_headers(self, sheet):
        best = None
        best_columns = {}
        issues: list[DailyReportIssue] = []
        max_row = min(sheet.used_rows, 20)
        for row_number in range(1, max_row + 1):
            matches: dict[str, list[int]] = {}
            for column in range(1, sheet.used_columns + 1):
                cell = sheet.cell(row_number, column)
                normalized = normalize_text(cell.value if cell else "")
                if not normalized:
                    continue
                field = self._field_for_header(normalized)
                if field:
                    matches.setdefault(field, []).append(column)
            score = len(self.REQUIRED_FIELDS.intersection(matches))
            if best is None or score > best[0]:
                best = (score, row_number)
                best_columns = matches
        if not best or best[0] < len(self.REQUIRED_FIELDS):
            issues.append(DailyReportIssue("MISSING_REQUIRED_HEADERS", "No se identificaron los encabezados obligatorios.", sheet.name))
            return None, {}, issues
        ambiguous = [field for field, columns in best_columns.items() if len(columns) > 1 and field in self.REQUIRED_FIELDS]
        if ambiguous:
            issues.append(DailyReportIssue("AMBIGUOUS_REQUIRED_HEADERS", "Existen encabezados obligatorios ambiguos.", sheet.name, best[1]))
            return None, {}, issues
        columns = {field: values[0] for field, values in best_columns.items()}
        missing = self.REQUIRED_FIELDS.difference(columns)
        if missing:
            issues.append(DailyReportIssue("MISSING_REQUIRED_HEADERS", "Faltan encabezados obligatorios.", sheet.name, best[1]))
            return None, {}, issues
        return best[1], columns, issues

    def _field_for_header(self, normalized: str) -> str | None:
        for field, aliases in self.HEADER_ALIASES.items():
            if normalized in aliases:
                return field
        return None

    def _parse_row(self, sheet, row_number: int, columns: dict[str, int]) -> ParsedDailyReportRow | None:
        original_assignment = self._text(sheet, row_number, columns["assignment_number"])
        normalized_assignment = original_assignment.strip()
        payment_date, date_error = self._date(sheet.cell(row_number, columns["payment_date"]))
        amount, movement_type, amount_error = self._amount(
            sheet.cell(row_number, columns["amount"]),
            sheet.cell(row_number, columns["withdrawal"]) if columns.get("withdrawal") else None,
        )
        payer_name = self._text(sheet, row_number, columns.get("payer_name"))
        payer_document = self._text(sheet, row_number, columns.get("payer_document"))
        concept = self._text(sheet, row_number, columns.get("concept"))
        original_data = {
            "assignment_number": original_assignment,
            "payment_date": str(payment_date) if payment_date else "",
            "amount": str(amount) if amount is not None else "",
            "payer_name": payer_name,
            "payer_document": payer_document,
            "concept": concept,
        }
        if not any(original_data.values()):
            return None
        status = "valid"
        message = ""
        if not normalized_assignment:
            status, message = "invalid_assignment", "La fila no tiene numero de encargo."
        elif date_error:
            status, message = "invalid_date", date_error
        elif amount_error:
            status, message = "invalid_amount", amount_error
        return ParsedDailyReportRow(
            sheet_name=sheet.name,
            row_number=row_number,
            original_assignment_number=original_assignment,
            normalized_assignment_number=normalized_assignment,
            payment_date=payment_date,
            amount=amount,
            movement_type=movement_type,
            payer_name=payer_name,
            payer_document=payer_document,
            concept=concept,
            original_data=original_data,
            status_hint=status,
            message=message,
        )

    def _is_empty_row(self, sheet, row_number: int) -> bool:
        return not any(self._text(sheet, row_number, column) for column in range(1, sheet.used_columns + 1))

    def _text(self, sheet, row_number: int, column: int | None) -> str:
        if not column:
            return ""
        cell = sheet.cell(row_number, column)
        value = cell.value if cell else ""
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).strip()

    def _date(self, cell) -> tuple[date | None, str]:
        value = cell.value if cell else None
        if isinstance(value, datetime):
            return value.date(), ""
        if isinstance(value, date):
            return value, ""
        if isinstance(value, int | float):
            return (datetime(1899, 12, 30) + timedelta(days=float(value))).date(), ""
        text = str(value).strip() if value is not None else ""
        if not text:
            return None, "La fecha esta vacia."
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(text, fmt).date(), ""
            except ValueError:
                continue
        return None, "La fecha no tiene un formato valido."

    def _amount(self, addition_cell, withdrawal_cell=None) -> tuple[Decimal | None, str, str]:
        addition = self._decimal(addition_cell.value if addition_cell else None)
        withdrawal = self._decimal(withdrawal_cell.value if withdrawal_cell else None)
        if addition and addition > 0:
            return addition, "addition", ""
        if withdrawal and withdrawal > 0:
            return withdrawal, "withdrawal", ""
        value = addition if addition is not None else withdrawal
        if value is None:
            return None, "addition", "El valor esta vacio."
        return value, "addition", "El valor debe ser mayor que cero."

    def _decimal(self, value) -> Decimal | None:
        if value in (None, ""):
            return None
        if isinstance(value, Decimal):
            return value
        text = str(value).strip().replace("$", "").replace(" ", "")
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", ".")
        try:
            return Decimal(text)
        except (InvalidOperation, ValueError):
            return None
