from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fiduciary.imports.historical.normalize import MONTHS, normalize_text
from fiduciary.imports.historical.readers import WorkbookReader


HEADER_ROW = 3
DATA_START_ROW = 4


@dataclass(frozen=True)
class CBRIssue:
    code: str
    message: str
    sheet_name: str | None = None
    row_number: int | None = None
    column_letter: str = ""
    field_name: str = ""
    found_value: str = ""


@dataclass(frozen=True)
class ParsedCBRRow:
    sheet_name: str
    row_number: int
    original_assignment_number: str
    normalized_assignment_number: str
    original_data: dict[str, str]
    parsed_data: dict[str, Any]
    issues: list[CBRIssue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class ParsedCBRSheet:
    name: str
    index: int
    header_row: int | None
    rows: list[ParsedCBRRow] = field(default_factory=list)
    issues: list[CBRIssue] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedCBRWorkbook:
    path: Path
    sheets: list[ParsedCBRSheet]
    issues: list[CBRIssue]

    @property
    def rows(self) -> list[ParsedCBRRow]:
        return [row for sheet in self.sheets for row in sheet.rows]


class CBRParser:
    FIELDS = {
        "assignment": "ENCARGO",
        "credit_date": "FECHA PAGO CREDITO",
        "credit_entity": "ENTIDAD CR",
        "credit_amount": "VALOR CR",
        "box_subsidy_date": "FECHA SUBSIDIO CAJA",
        "box_subsidy_entity": "ENTIDAD SB CAJA",
        "box_subsidy_amount": "VALOR SB CAJA",
        "government_subsidy_date": "FECHA SUBSIDIO GOB",
        "government_subsidy_entity": "ENTIDAD SB GOB",
        "government_subsidy_amount": "VALOR SB GOB",
        "adhesion_contract_date": "FECHA CONTRATO DE ADHESION",
        "promise_date": "FECHA DE PROMESA",
        "promised_delivery_date": "FECHA ENTREGA SEGUN PROMESA",
        "actual_delivery_date": "ENTREGA REAL",
        "registration_number": "MATRICULA",
        "deed_date": "FECHA ESCRITURA",
        "deed_number": "ESCRITURA",
        "notary": "NOTARIA",
        "tradition_certificate_date": "FECHA C/TRADIC",
        "electronic_invoice": "FACT. ELECTRONICA",
        "interest_receipt": "RECIBO INTERESES",
        "interest_date": "FECHA INTERESES",
        "interest_amount": "VALOR INTERESES",
    }
    REQUIRED_HEADERS = set(FIELDS)

    CREDIT_BLOCKS = {
        "credit": {
            "label": "credito",
            "display": "El credito",
            "date": "credit_date",
            "entity": "credit_entity",
            "amount": "credit_amount",
        },
        "box_subsidy": {
            "label": "subsidio_caja",
            "display": "El Subsidio de Caja",
            "date": "box_subsidy_date",
            "entity": "box_subsidy_entity",
            "amount": "box_subsidy_amount",
        },
        "government_subsidy": {
            "label": "subsidio_gobierno",
            "display": "El Subsidio de Gobierno",
            "date": "government_subsidy_date",
            "entity": "government_subsidy_entity",
            "amount": "government_subsidy_amount",
        },
    }
    CONTRACT_FIELDS = {
        "adhesion_contract_date": "adhesion_contract_date",
        "promise_date": "promise_date",
        "promised_delivery_date": "promised_delivery_date",
        "actual_delivery_date": "actual_delivery_date",
    }
    LEGAL_FIELDS = {
        "registration_number": "registration_number",
        "deed_date": "deed_date",
        "deed_number": "deed_number",
        "notary": "notary",
        "tradition_certificate_date": "tradition_certificate_date",
        "electronic_invoice": "electronic_invoice",
    }

    def __init__(self, path):
        self.path = Path(path)

    def parse(self) -> ParsedCBRWorkbook:
        workbook = WorkbookReader().read(self.path)
        issues = [
            CBRIssue(
                code=issue.code,
                message=issue.message,
                sheet_name=issue.sheet_name,
                row_number=issue.row_number,
                column_letter=issue.column_letter,
                field_name=issue.field_name,
                found_value=issue.found_value,
            )
            for issue in workbook.issues
        ]
        sheets = [self._parse_sheet(sheet) for sheet in workbook.sheets]
        issues.extend(issue for sheet in sheets for issue in sheet.issues)
        return ParsedCBRWorkbook(path=self.path, sheets=sheets, issues=issues)

    def _parse_sheet(self, sheet) -> ParsedCBRSheet:
        columns, issues = self._headers(sheet)
        if issues:
            return ParsedCBRSheet(name=sheet.name, index=sheet.index, header_row=HEADER_ROW, issues=issues)
        rows = []
        for row_number in range(DATA_START_ROW, sheet.used_rows + 1):
            if self._is_empty_row(sheet, row_number):
                continue
            rows.append(self._parse_row(sheet, row_number, columns))
        return ParsedCBRSheet(name=sheet.name, index=sheet.index, header_row=HEADER_ROW, rows=rows)

    def _headers(self, sheet) -> tuple[dict[str, int], list[CBRIssue]]:
        headers = {}
        by_normalized = {}
        for column in range(1, sheet.used_columns + 1):
            cell = sheet.cell(HEADER_ROW, column)
            value = cell.value if cell else None
            normalized = normalize_text(value)
            if normalized:
                by_normalized[normalized] = column
        issues = []
        for field, header in self.FIELDS.items():
            column = self._find_header(by_normalized, header)
            if column:
                headers[field] = column
            elif field in self.REQUIRED_HEADERS:
                issues.append(
                    CBRIssue(
                        code="CBR_MISSING_HEADER",
                        message=f"El archivo no contiene la columna {header}.",
                        sheet_name=sheet.name,
                        row_number=HEADER_ROW,
                        field_name=header,
                    )
                )
        return headers, issues

    def _find_header(self, headers: dict[str, int], expected: str) -> int | None:
        normalized_expected = normalize_text(expected)
        if normalized_expected in headers:
            return headers[normalized_expected]
        compact_expected = normalized_expected.replace(" ", "")
        for header, column in headers.items():
            compact_header = header.replace(" ", "")
            if compact_header == compact_expected:
                return column
            if expected == "FECHA ENTREGA SEGUN PROMESA" and header.startswith("fecha entrega seg"):
                return column
            if expected == "NOTARIA" and header.startswith("notar"):
                return column
            if expected == "FACT. ELECTRONICA" and "fact" in header and "electr" in header:
                return column
        return None

    def _parse_row(self, sheet, row_number: int, columns: dict[str, int]) -> ParsedCBRRow:
        original_data = {field: self._display_value(sheet, row_number, column) for field, column in columns.items()}
        issues: list[CBRIssue] = []
        original_assignment = original_data.get("assignment", "")
        normalized_assignment = self._normalize_assignment(sheet.cell(row_number, columns["assignment"]))
        parsed_data = {"credit_subsidies": [], "contract": {}, "legal": {}, "interest": None}
        if not normalized_assignment:
            issues.append(
                CBRIssue(
                    "CBR_ASSIGNMENT_MISSING",
                    "El registro no contiene un encargo fiduciario.",
                    sheet.name,
                    row_number,
                    self._letter(sheet, row_number, columns["assignment"]),
                    "ENCARGO",
                    original_assignment,
                )
            )

        for block_key, config in self.CREDIT_BLOCKS.items():
            block = self._parse_compound_block(sheet, row_number, columns, config, issues)
            if block:
                block["kind"] = block_key
                parsed_data["credit_subsidies"].append(block)

        for field, target in self.CONTRACT_FIELDS.items():
            value = self._raw(sheet, row_number, columns[field])
            if self._blank(value):
                continue
            parsed, error = self._date(value)
            if error:
                issues.append(self._field_issue(sheet, row_number, columns[field], field, "CBR_INVALID_DATE", error, value))
            else:
                parsed_data["contract"][target] = parsed.isoformat()

        for field, target in self.LEGAL_FIELDS.items():
            value = self._raw(sheet, row_number, columns[field])
            if self._blank(value):
                continue
            if field in {"deed_date", "tradition_certificate_date"}:
                parsed, error = self._date(value)
                if error:
                    issues.append(self._field_issue(sheet, row_number, columns[field], field, "CBR_INVALID_DATE", error, value))
                else:
                    parsed_data["legal"][target] = parsed.isoformat()
            else:
                parsed_data["legal"][target] = self._display_value(sheet, row_number, columns[field])

        interest = self._parse_interest_block(sheet, row_number, columns, issues)
        if interest:
            parsed_data["interest"] = interest

        return ParsedCBRRow(
            sheet_name=sheet.name,
            row_number=row_number,
            original_assignment_number=original_assignment,
            normalized_assignment_number=normalized_assignment,
            original_data=original_data,
            parsed_data=parsed_data,
            issues=issues,
        )

    def _parse_compound_block(self, sheet, row_number, columns, config, issues):
        raw_date = self._raw(sheet, row_number, columns[config["date"]])
        raw_entity = self._raw(sheet, row_number, columns[config["entity"]])
        raw_amount = self._raw_amount(sheet, row_number, columns[config["amount"]])
        present = {
            "fecha": not self._blank(raw_date),
            "entidad": not self._blank(raw_entity),
            "valor": not self._blank(raw_amount),
        }
        if not any(present.values()):
            return None
        if not all(present.values()):
            missing = self._missing_text([key for key, is_present in present.items() if not is_present])
            issues.append(
                CBRIssue(
                    "CBR_INCOMPLETE_BLOCK",
                    f"{config['display']} tiene informacion incompleta. Faltan {missing}.",
                    sheet.name,
                    row_number,
                    field_name=config["display"],
                    found_value=self._join_values(raw_date, raw_entity, raw_amount),
                )
            )
            return None
        parsed_date, date_error = self._date(raw_date)
        amount = self._decimal(raw_amount)
        if date_error:
            issues.append(self._field_issue(sheet, row_number, columns[config["date"]], config["date"], "CBR_INVALID_DATE", date_error, raw_date))
        if amount is None:
            issues.append(
                self._field_issue(
                    sheet,
                    row_number,
                    columns[config["amount"]],
                    config["amount"],
                    "CBR_INVALID_AMOUNT",
                    f"{config['display']} tiene un valor no interpretable.",
                    raw_amount,
                )
            )
        if date_error or amount is None:
            return None
        return {"date": parsed_date.isoformat(), "entity": str(raw_entity).strip(), "amount": str(amount)}

    def _parse_interest_block(self, sheet, row_number, columns, issues):
        raw_receipt = self._raw(sheet, row_number, columns["interest_receipt"])
        raw_date = self._raw(sheet, row_number, columns["interest_date"])
        raw_amount = self._raw_amount(sheet, row_number, columns["interest_amount"])
        present = {
            "recibo": not self._blank(raw_receipt),
            "fecha": not self._blank(raw_date),
            "valor": not self._blank(raw_amount),
        }
        if not any(present.values()):
            return None
        if not all(present.values()):
            missing = self._missing_text([key for key, is_present in present.items() if not is_present])
            issues.append(
                CBRIssue(
                    "CBR_INCOMPLETE_BLOCK",
                    f"Intereses tiene informacion incompleta. Faltan {missing}.",
                    sheet.name,
                    row_number,
                    field_name="Intereses",
                    found_value=self._join_values(raw_receipt, raw_date, raw_amount),
                )
            )
            return None
        parsed_date, date_error = self._date(raw_date)
        amount = self._decimal(raw_amount)
        if date_error:
            issues.append(self._field_issue(sheet, row_number, columns["interest_date"], "interest_date", "CBR_INVALID_DATE", date_error, raw_date))
        if amount is None:
            issues.append(
                self._field_issue(
                    sheet,
                    row_number,
                    columns["interest_amount"],
                    "interest_amount",
                    "CBR_INVALID_AMOUNT",
                    "Intereses tiene un valor no interpretable.",
                    raw_amount,
                )
            )
        if date_error or amount is None:
            return None
        return {"receipt": str(raw_receipt).strip(), "date": parsed_date.isoformat(), "amount": str(amount)}

    def _date(self, value) -> tuple[date | None, str]:
        if isinstance(value, datetime):
            return value.date(), ""
        if isinstance(value, date):
            return value, ""
        if isinstance(value, Decimal):
            value = float(value)
        if isinstance(value, int | float):
            try:
                return (datetime(1899, 12, 30) + timedelta(days=float(value))).date(), ""
            except (OverflowError, ValueError):
                return None, "La fecha no tiene un valor valido."
        text = str(value).strip() if value is not None else ""
        if not text:
            return None, "La fecha esta vacia."
        for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y"):
            try:
                return datetime.strptime(text, fmt).date(), ""
            except ValueError:
                pass
        normalized = text.upper().replace(" ", "").rstrip("F")
        if "." in normalized and "/" in normalized:
            month_text, rest = normalized.split(".", 1)
            day_text, year_text = rest.split("/", 1)
            month = MONTHS.get(month_text)
            if month and day_text.isdigit() and year_text.isdigit() and len(year_text) == 2:
                year = 2000 + int(year_text)
                try:
                    return date(year, month, int(day_text)), ""
                except ValueError:
                    pass
        return None, "La fecha no tiene un formato valido."

    def _decimal(self, value) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value if value > 0 else None
        if isinstance(value, int):
            return Decimal(value) if value > 0 else None
        if isinstance(value, float):
            return Decimal(str(value)) if value > 0 else None

        text = str(value).strip().replace("$", "").replace(" ", "")
        if not text:
            return None
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        elif "." in text:
            parts = text.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and all(part.isdigit() for part in parts)):
                text = "".join(parts)
        try:
            amount = Decimal(text)
        except InvalidOperation:
            return None
        return amount if amount > 0 else None

    def _field_issue(self, sheet, row_number, column, field, code, message, value):
        return CBRIssue(
            code,
            message,
            sheet.name,
            row_number,
            self._letter(sheet, row_number, column),
            self.FIELDS.get(field, field),
            self._safe_text(value),
        )

    def _is_empty_row(self, sheet, row_number: int) -> bool:
        for column in range(1, sheet.used_columns + 1):
            if not self._blank(self._raw(sheet, row_number, column)):
                return False
        return True

    def _normalize_assignment(self, cell) -> str:
        value = cell.value if cell else None
        if self._blank(value):
            return ""
        if isinstance(value, Decimal):
            return str(int(value)) if value == value.to_integral_value() else str(value).strip()
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return str(int(value)) if value.is_integer() else str(value).strip()
        return str(value).strip()

    def _display_value(self, sheet, row_number: int, column: int) -> str:
        return self._safe_text(self._raw(sheet, row_number, column))

    def _raw(self, sheet, row_number: int, column: int):
        cell = sheet.cell(row_number, column)
        return cell.value if cell else None

    def _raw_amount(self, sheet, row_number: int, column: int):
        cell = sheet.cell(row_number, column)
        if not cell:
            return None
        if cell.is_date and cell.raw_value not in (None, ""):
            return cell.raw_value
        return cell.value

    def _letter(self, sheet, row_number: int, column: int) -> str:
        cell = sheet.cell(row_number, column)
        return cell.letter if cell else ""

    def _blank(self, value) -> bool:
        return value is None or str(value).strip() == ""

    def _safe_text(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).strip()

    def _join_values(self, *values) -> str:
        return " | ".join(self._safe_text(value) for value in values if not self._blank(value))

    def _missing_text(self, values: list[str]) -> str:
        if len(values) == 1:
            return f"la {values[0]}" if values[0] in {"fecha", "entidad"} else f"el {values[0]}"
        return " y ".join(values)
