import calendar
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .data import (
    DetectedColumn,
    DetectedPaymentColumn,
    HistoricalAssignment,
    HistoricalClient,
    HistoricalMonthlyPayment,
    HistoricalNovelty,
    HistoricalNoveltyCell,
    HistoricalRow,
    ParseStatistics,
    ParserIssue,
    ReconstructedHistoricalPayment,
    SheetData,
    WorkbookData,
)
from fiduciary.imports.header_resolver import (
    HeaderResolver,
    header_candidates_from_sheet,
)
from .normalize import MONTHS, clean_text, compact_normalized, normalize_text, parse_decimal, parse_document_type
from .readers import RawSheet, WorkbookReader


STRICT_HISTORICAL_DATE_MONTHS = {
    "ENE": 1,
    "FEB": 2,
    "MAR": 3,
    "ABR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AGO": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DIC": 12,
}
STRICT_HISTORICAL_MONTH_LABELS = {month: label for label, month in STRICT_HISTORICAL_DATE_MONTHS.items()}
STRICT_HISTORICAL_DATE_RE = re.compile(
    r"^(?P<month>ENE|FEB|MAR|ABR|MAY|JUN|JUL|AGO|SEP|OCT|NOV|DIC)\.(?P<day>\d{1,2})/(?P<year>\d{2})(?P<fiduciary>F)?$",
    re.IGNORECASE,
)
PAYMENT_HEADER_PATTERNS = (
    re.compile(r"^recibofiducia([a-z]{3,4})(\d{4})$"),
    re.compile(r"^recibidofidubogota([a-z]{3,4})(\d{4})$"),
    re.compile(r"^recibido([a-z]{3,4})(\d{2}|\d{4})$"),
)
IGNORED_HEADER_KEYS = {
    "vincxmes",
    "prom",
    "#",
    "valorinmueble",
    "recibos",
    "recibosfidubogota",
    "fecha",
    "bono",
    "totalrecibido",
    "saldoporcobrar",
    "recursospropios",
    "creditobancario",
    "cajahonor",
    "subsidiosmcy",
    "subsidioscaja",
    "entidad",
    "fechadepromesa",
    "fechacontratoadhesion",
    "entregaspromesa",
    "entregareal",
    "matricula",
    "fechaesc",
    "esc",
    "an",
    "not",
    "fechactradic",
    "fe",
    "intereses",
    "valor",
}

HISTORICAL_HEADER_EXPECTED = {
    "seller": "VENDEDOR",
    "assignment_number": "ENCARGO FIDUCIARIO",
    "legacy_assignment_number": "ENCARGO FIDUCIARIO ANTIGUO",
    "new_assignment_number": "NUEVOS ENCARGO FIDUCIARIO",
    "unit": "UNIDAD",
    "area": "AREA",
    "assignment_status": "VINC",
    "document_number": "CEDULA CLIENTE",
    "client_name": "NOMBRE CLIENTE",
    "financial_entity": "ENTIDAD FINANCIERA",
    "property_value": "VALOR INMUEBLE",
    "phone": "TELEFONO",
    "email": "E-MAIL",
    "contact_name": "CONTACTO",
    "observations": "OBSERVACIONES",
    "receipt_numbers": "RECIBOS",
    "fidubogota_receipt_numbers": "RECIBOS FIDUBOGOTA",
    "payment_dates": "FECHA",
    "received_values": "RECIBIDO",
    "credit_payment_dates": "FECHA PAGO CREDITO",
    "subsidy_payment_dates": "FECHA PAGO SUBSIDIO",
    "credit_constructor_values": "ABONOS CR CONSTRUCTOR",
    "subsidy_compensation_values": "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION",
    "subsidy_government_values": "DESEMBOLSO SUBSIDIOS GOBIERNO",
    "assignment_changes": "CESIONES/TRASLADOS",
    "adhesion_contract_date": "FECHA CONTRATO DE ADHESION",
    "promise_date": "FECHA DE PROMESA",
    "promised_delivery_date": "ENTREGA S/PROMESA",
    "actual_delivery_date": "ENTREGA REAL",
}

HISTORICAL_HEADER_ALIASES = {
    "unit": {"APTO", "APARTAMENTO", "LOCAL", "BODEGA"},
    "document_number": {"DOCUMENTO CLIENTE", "IDENTIFICACION CLIENTE", "IDENTIFICACION"},
    "client_name": {"NOMBRE CLIENTE2"},
    "phone": {"TELÉFONO", "TEL", "CELULAR"},
    "email": {"EMAIL", "CORREO", "CORREO ELECTRONICO"},
    "observations": {"OBSERVACION"},
}

HISTORICAL_HEADER_RESOLVER = HeaderResolver(
    expected_headers=HISTORICAL_HEADER_EXPECTED,
    aliases=HISTORICAL_HEADER_ALIASES,
)

RECEIPT_CATEGORY_ORDINARY = "ordinary"
RECEIPT_CATEGORY_CREDIT = "credit"
RECEIPT_CATEGORY_SUBSIDY = "subsidy"
RECEIPT_CATEGORY_TRANSFER = "transfer"
RECEIPT_CATEGORY_CESSION = "cession"
PAYMENT_DESTINATION_CONSTRUCTORA = "constructora"
PAYMENT_DESTINATION_FIDUCIARIA = "fiduciaria"
DETAIL_HEADER_MIN_SCORE = 4
SUMMARY_SHEET_MAX_DETAIL_SCORE = 2
SUMMARY_SHEET_HEADER_KEYS = {
    "torre",
    "und",
    "valorventas",
    "totalrecibido",
    "saldoporcobrar",
    "recursospropios",
}

SPECIAL_VALUE_COLUMN_KEYS = {
    RECEIPT_CATEGORY_CREDIT: ("credit_constructor_values",),
    RECEIPT_CATEGORY_SUBSIDY: ("subsidy_compensation_values", "subsidy_government_values"),
    RECEIPT_CATEGORY_TRANSFER: ("assignment_changes",),
    RECEIPT_CATEGORY_CESSION: ("assignment_changes",),
}


@dataclass(frozen=True)
class ReceiptToken:
    raw_value: str
    source: str
    source_header: str
    source_column: str
    source_order: int
    order: int
    category: str


def _has_minimal_unit_data(row: HistoricalRow) -> bool:
    return bool(row.unit_code and row.area is not None and row.property_value is not None)


class HistoricalWorkbookParser:
    def __init__(self, path, *, grouping_type_hint: str | None = None, progress_callback=None):
        self.path = Path(path)
        self.reader = WorkbookReader()
        self.grouping_type_hint = grouping_type_hint
        self.progress_callback = progress_callback
        self._progress_total_rows = 0
        self._progress_done_rows = 0
        self._receipt_tokens_cache = {}
        self._receipt_date_pairs_cache = {}
        self._payment_values_cache = {}
        self._historical_date_normalizations = []
        self._progress_sheet_index = 0
        self._progress_total_sheets = 0

    def parse(self) -> WorkbookData:
        raw_workbook = self.reader.read(self.path)
        self._progress_total_sheets = len(raw_workbook.sheets)
        self._progress_total_rows = sum(max(sheet.used_rows, 0) for sheet in raw_workbook.sheets) or 1
        self._emit_progress("preparing", sheet_name="", row_number=0)
        sheets = []
        for index, sheet in enumerate(raw_workbook.sheets, start=1):
            self._progress_sheet_index = index
            self._emit_progress("processing_sheet", sheet_name=sheet.name, row_number=0)
            sheets.append(self._parse_sheet(sheet))
        issues = list(raw_workbook.issues)
        for sheet in sheets:
            issues.extend(sheet.issues)
        issues.extend(self._duplicate_assignment_issues(sheets))
        self._progress_done_rows = self._progress_total_rows
        self._emit_progress("finalizing", sheet_name="", row_number=0)
        return WorkbookData(
            path=self.path,
            file_type=raw_workbook.file_type,
            sheets=sheets,
            issues=issues,
            statistics=self._build_statistics(sheets, issues),
        )

    def _emit_progress(self, phase: str, *, sheet_name: str, row_number: int) -> None:
        if not self.progress_callback:
            return
        percent = int(min(100, round((self._progress_done_rows / self._progress_total_rows) * 100)))
        self.progress_callback(
            {
                "phase": phase,
                "percent": percent,
                "sheet": sheet_name,
                "sheet_index": self._progress_sheet_index,
                "total_sheets": self._progress_total_sheets,
                "row": row_number,
                "processed_rows": min(self._progress_done_rows, self._progress_total_rows),
                "total_rows": self._progress_total_rows,
            }
        )

    def _parse_sheet(self, raw_sheet: RawSheet) -> SheetData:
        if not raw_sheet.cells:
            return SheetData(
                name=raw_sheet.name,
                index=raw_sheet.index,
                visibility=raw_sheet.visibility,
                used_rows=0,
                used_columns=0,
                classification="empty",
            )

        header_row = self._find_header_row(raw_sheet)
        if header_row is None:
            if self._looks_like_summary_sheet(raw_sheet):
                return SheetData(
                    name=raw_sheet.name,
                    index=raw_sheet.index,
                    visibility=raw_sheet.visibility,
                    used_rows=raw_sheet.used_rows,
                    used_columns=raw_sheet.used_columns,
                    classification="skipped_summary",
                    ignored_rows=raw_sheet.used_rows,
                    ignored_row_reasons={"summary_sheet": raw_sheet.used_rows},
                )
            return SheetData(
                name=raw_sheet.name,
                index=raw_sheet.index,
                visibility=raw_sheet.visibility,
                used_rows=raw_sheet.used_rows,
                used_columns=raw_sheet.used_columns,
                classification="unknown",
                issues=[
                    ParserIssue(
                        code="HEADER_ROW_NOT_FOUND",
                        severity="blocking",
                        message="No se encontro una fila de encabezados compatible con el libro historico.",
                        sheet_name=raw_sheet.name,
                    )
                ],
            )

        columns, payment_columns, header_issues = self._detect_columns(raw_sheet, header_row)
        classification = "processable" if self._has_required_columns(columns, payment_columns) else "unknown"
        rows, novelties, ignored_rows, ignored_row_reasons, row_issues = self._extract_rows(
            raw_sheet,
            header_row,
            columns,
            payment_columns,
        )
        if not self._sheet_has_fiduciary_data(rows, novelties):
            ignored_rows += len(rows)
            ignored_row_reasons = dict(Counter(ignored_row_reasons) + Counter({"sheet_without_fiduciary_data": len(rows)}))
            rows = []
            novelties = []
            classification = "skipped_without_fiduciary_data"
        return SheetData(
            name=raw_sheet.name,
            index=raw_sheet.index,
            visibility=raw_sheet.visibility,
            used_rows=raw_sheet.used_rows,
            used_columns=raw_sheet.used_columns,
            classification=classification,
            header_row=header_row,
            columns=columns,
            payment_columns=payment_columns,
            rows=rows,
            novelties=novelties,
            ignored_rows=ignored_rows,
            ignored_row_reasons=ignored_row_reasons,
            issues=header_issues + row_issues,
        )

    def _sheet_has_fiduciary_data(self, rows: list[HistoricalRow], novelties: list[HistoricalNovelty]) -> bool:
        for row in rows:
            if row.assignment and row.assignment.assignment_number:
                return True
            if row.clients:
                return True
        for novelty in novelties:
            if novelty.assignment and novelty.assignment.assignment_number:
                return True
            if self._novelty_has_client_data(novelty):
                return True
        return False

    def _novelty_has_client_data(self, novelty: HistoricalNovelty) -> bool:
        client_headers = {
            "cedula cliente",
            "documento cliente",
            "identificacion",
            "identificacion cliente",
            "nombre cliente",
        }
        for cell in novelty.cells:
            header = normalize_text(cell.header or "")
            if header in client_headers and clean_text(cell.value):
                return True
        return False

    def _find_header_row(self, sheet: RawSheet) -> int | None:
        best_row = None
        best_score = 0
        for row in range(1, min(sheet.used_rows, 30) + 1):
            score = self._header_score(header_candidates_from_sheet(sheet, row))
            if score > best_score:
                best_row = row
                best_score = score
        return best_row if best_score >= DETAIL_HEADER_MIN_SCORE else None

    def _looks_like_summary_sheet(self, sheet: RawSheet) -> bool:
        best_detail_score = 0
        summary_hits = 0
        for row in range(1, min(sheet.used_rows, 30) + 1):
            headers = header_candidates_from_sheet(sheet, row)
            best_detail_score = max(best_detail_score, self._header_score(headers))
            row_keys = {candidate.normalized_header.replace(" ", "") for candidate in headers}
            summary_hits = max(summary_hits, len(row_keys & SUMMARY_SHEET_HEADER_KEYS))
        return summary_hits >= 4 and best_detail_score <= SUMMARY_SHEET_MAX_DETAIL_SCORE

    def _header_score(self, headers) -> int:
        score = 0
        if self._has_any_assignment_header(headers):
            score += 2
        if HISTORICAL_HEADER_RESOLVER.resolve("unit", headers).found:
            score += 2
        if HISTORICAL_HEADER_RESOLVER.resolve("document_number", headers).found:
            score += 1
        if any(resolution.found for resolution in HISTORICAL_HEADER_RESOLVER.resolve_repeated("client_name", headers)):
            score += 1
        if any(self._detect_payment_column(header.header, header.column_index, header.column_letter) for header in headers):
            score += 1
        if HISTORICAL_HEADER_RESOLVER.resolve("received_values", headers).found:
            score += 1
        return score

    def _has_any_assignment_header(self, headers) -> bool:
        return any(
            HISTORICAL_HEADER_RESOLVER.resolve(key, headers).found
            for key in ("assignment_number", "new_assignment_number")
        )

    def _detect_columns(
        self,
        sheet: RawSheet,
        header_row: int,
    ) -> tuple[dict[str, DetectedColumn], list[DetectedPaymentColumn], list[ParserIssue]]:
        columns: dict[str, DetectedColumn] = {}
        client_name_columns: list[DetectedColumn] = []
        payment_columns: list[DetectedPaymentColumn] = []
        issues: list[ParserIssue] = []
        known_header_count = 0
        header_candidates = header_candidates_from_sheet(sheet, header_row)
        recognized_indexes = set()

        for candidate in header_candidates:
            compact = candidate.normalized_header.replace(" ", "")
            payment_column = self._detect_payment_column(candidate.header, candidate.column_index, candidate.column_letter)
            if payment_column:
                payment_columns.append(payment_column)
                recognized_indexes.add(candidate.column_index)
                known_header_count += 1
        for key in HISTORICAL_HEADER_EXPECTED:
            if key == "client_name":
                resolutions = HISTORICAL_HEADER_RESOLVER.resolve_repeated(key, header_candidates, sheet_name=sheet.name)
                if len(resolutions) == 1 and resolutions[0].ambiguous:
                    issues.append(self._header_resolution_issue(resolutions[0], header_row))
                    continue
                for resolution in resolutions:
                    if not resolution.found:
                        continue
                    recognized_indexes.add(resolution.column_index)
                    client_column = self._detected_column_from_resolution(
                        f"client_name_{len(client_name_columns) + 1}",
                        resolution,
                    )
                    client_name_columns.append(client_column)
                    known_header_count += 1
                continue
            resolution = HISTORICAL_HEADER_RESOLVER.resolve(key, header_candidates, sheet_name=sheet.name)
            if resolution.ambiguous:
                issues.append(self._header_resolution_issue(resolution, header_row))
                continue
            if resolution.found:
                recognized_indexes.add(resolution.column_index)
                columns[key] = self._detected_column_from_resolution(key, resolution)
                known_header_count += 1

        for candidate in header_candidates:
            compact = candidate.normalized_header.replace(" ", "")
            if candidate.column_index in recognized_indexes or compact in IGNORED_HEADER_KEYS:
                continue
            issues.append(
                ParserIssue(
                    code="UNKNOWN_HEADER",
                    severity="info",
                    message="Encabezado no requerido por el analizador historico.",
                    sheet_name=sheet.name,
                    row_number=header_row,
                    column_letter=candidate.column_letter,
                )
            )

        if client_name_columns:
            columns["client_names"] = client_name_columns[0]
            for column in client_name_columns:
                columns[column.key] = column
        payment_columns.sort(key=lambda item: (item.year, item.month, item.index))
        issues.extend(self._required_column_issues(sheet.name, columns, payment_columns))
        if known_header_count == 0:
            issues.append(
                ParserIssue(
                    code="NO_RECOGNIZED_HEADERS",
                    severity="blocking",
                    message="La hoja no contiene encabezados reconocidos para libro historico.",
                    sheet_name=sheet.name,
                    row_number=header_row,
                )
            )
        return columns, payment_columns, issues

    def _detected_column_from_resolution(self, key: str, resolution) -> DetectedColumn:
        return DetectedColumn(
            key=key,
            header=resolution.actual_header,
            normalized_header=resolution.normalized_actual,
            index=resolution.column_index,
            letter=resolution.column_letter,
            expected_header=resolution.expected_header,
            match_type=resolution.match_type,
        )

    def _header_resolution_issue(self, resolution, header_row: int) -> ParserIssue:
        candidates = ", ".join(candidate.header for candidate in resolution.candidates)
        return ParserIssue(
            code="AMBIGUOUS_HEADER",
            severity="error",
            message=f"Encabezado ambiguo para {resolution.expected_header}: {candidates}.",
            sheet_name=resolution.sheet_name,
            row_number=header_row,
        )

    def _detect_payment_column(
        self,
        header: str,
        column: int,
        letter: str,
    ) -> DetectedPaymentColumn | None:
        normalized = normalize_text(header)
        match = None
        for pattern in PAYMENT_HEADER_PATTERNS:
            match = pattern.match(compact_normalized(header))
            if match:
                break
        if not match:
            return None
        month_text, year_text = match.groups()
        month = MONTHS.get(month_text.upper())
        if not month:
            return None
        year = int(year_text)
        if len(year_text) == 2:
            year += 2000
        return DetectedPaymentColumn(
            key=f"payment_{year}_{month:02}",
            header=header,
            normalized_header=normalized,
            index=column,
            letter=letter,
            month=month,
            year=year,
        )

    def _required_column_issues(
        self,
        sheet_name: str,
        columns: dict[str, DetectedColumn],
        payment_columns: list[DetectedPaymentColumn],
    ) -> list[ParserIssue]:
        issues = []
        for key, label in {
            "unit": "unidad inmobiliaria",
            "document_number": "documento de cliente",
            "client_names": "nombre de cliente",
        }.items():
            if key not in columns:
                issues.append(
                    ParserIssue(
                        code="REQUIRED_COLUMN_MISSING",
                        severity="error",
                        message=f"Falta la columna obligatoria de {label}.",
                        sheet_name=sheet_name,
                    )
                )
        if "assignment_number" not in columns and "new_assignment_number" not in columns:
            issues.append(
                ParserIssue(
                    code="REQUIRED_COLUMN_MISSING",
                    severity="error",
                    message="Falta la columna obligatoria de encargo fiduciario.",
                    sheet_name=sheet_name,
                )
            )
        if not payment_columns and "received_values" not in columns:
            issues.append(
                ParserIssue(
                    code="PAYMENT_COLUMNS_MISSING",
                    severity="warning",
            message="No se detectaron columnas mensuales de recibos fiduciarios.",
            sheet_name=sheet_name,
        )
            )
        return issues

    def _has_required_columns(
        self,
        columns: dict[str, DetectedColumn],
        payment_columns: list[DetectedPaymentColumn],
    ) -> bool:
        return (
            ("assignment_number" in columns or "new_assignment_number" in columns)
            and all(key in columns for key in ["unit", "document_number", "client_names"])
            and (bool(payment_columns) or "received_values" in columns)
        )

    def _extract_rows(
        self,
        sheet: RawSheet,
        header_row: int,
        columns: dict[str, DetectedColumn],
        payment_columns: list[DetectedPaymentColumn],
    ) -> tuple[list[HistoricalRow], list[HistoricalNovelty], int, dict[str, int], list[ParserIssue]]:
        rows = []
        novelties = []
        ignored_rows = 0
        ignored_reasons = Counter()
        issues = []
        formula_cached_columns = {}
        project, grouping_code, grouping_name = self._extract_sheet_structure(sheet)
        in_novelty_section = False
        novelty_section = ""
        novelty_section_month = None
        novelty_section_year = None
        novelty_rows = 0
        for row_number in range(header_row + 1, sheet.used_rows + 1):
            self._progress_done_rows += 1
            if row_number == sheet.used_rows or self._progress_done_rows % 25 == 0:
                self._emit_progress("processing_rows", sheet_name=sheet.name, row_number=row_number)
            if self._is_row_empty(sheet, row_number):
                ignored_rows += 1
                ignored_reasons["empty"] += 1
                continue
            if self._is_novelty_header_row(sheet, row_number):
                in_novelty_section = True
                novelty_section = ""
                novelty_section_month = None
                novelty_section_year = None
                ignored_rows += 1
                ignored_reasons["novelty_section"] += 1
                continue
            if in_novelty_section:
                subtitle = self._novelty_subtitle(sheet, row_number, columns)
                if subtitle:
                    novelty_section = subtitle
                    novelty_section_month, novelty_section_year = _month_year_from_text(subtitle)
                    ignored_rows += 1
                    ignored_reasons["novelty_subtitle"] += 1
                    continue
                novelty = self._extract_novelty(
                    sheet,
                    row_number,
                    project,
                    grouping_code,
                    grouping_name,
                    columns,
                    header_row,
                    novelty_section,
                    novelty_section_month,
                    novelty_section_year,
                )
                if novelty:
                    novelties.append(novelty)
                ignored_rows += 1
                ignored_reasons["novelty"] += 1
                novelty_rows += 1
                continue
            if self._is_decorative_or_total_row(sheet, row_number):
                ignored_rows += 1
                ignored_reasons["decorative_or_total"] += 1
                continue
            row = self._extract_row(sheet, row_number, project, grouping_code, grouping_name, columns, payment_columns)
            if row is None:
                ignored_rows += 1
                ignored_reasons["empty_after_extraction"] += 1
                continue
            if not self._looks_like_main_table_attempt(row):
                ignored_rows += 1
                ignored_reasons["structural_or_auxiliary"] += 1
                continue
            if (row.assignment is None and not _has_minimal_unit_data(row)) or not row.unit_code:
                missing = []
                if not row.unit_code:
                    missing.append("unidad")
                if row.assignment is None:
                    missing.append("encargo fiduciario")
                issues.append(
                    ParserIssue(
                        code="INVALID_HISTORICAL_ROW",
                        severity="warning",
                        message="Fila con informacion parcial sin unidad o encargo suficiente.",
                        sheet_name=sheet.name,
                        row_number=row_number,
                        unit_code=row.unit_code or "",
                        field_name=", ".join(missing),
                        found_value=self._row_diagnostic_value(row),
                        cause=f"Falta {', '.join(missing)}.",
                    )
                )
                ignored_rows += 1
                ignored_reasons["invalid"] += 1
                continue
            date_format_issues = self._historical_date_format_issues(sheet, row_number, columns, row)
            issues.extend(date_format_issues)
            if date_format_issues:
                rows.append(row)
                continue
            issues.extend(self._receipt_date_issues(sheet, row_number, columns, row))
            issues.extend(self._payment_reconstruction_issues(sheet, row_number, columns, payment_columns, row))
            rows.append(row)
            formula_issues, cached_columns = self._formula_issues_for_row(sheet, row_number, payment_columns, row)
            issues.extend(formula_issues)
            formula_cached_columns.update(cached_columns)
        for column_letter, column_header in sorted(formula_cached_columns.items()):
            issues.append(
                ParserIssue(
                    code="FORMULA_WITH_CACHED_VALUE",
                    severity="warning",
                    message="Columna mensual relevante contiene formulas con valor calculado disponible.",
                    sheet_name=sheet.name,
                    column_letter=column_letter,
                    field_name=column_header,
                    found_value="Una o más fórmulas con valor calculado disponible.",
                    cause="La columna contiene fórmulas; en esta fase se conserva el valor calculado sin descomponer pagos.",
                    extra_data={"scope": "column", "header": column_header},
                )
            )
        return rows, novelties, ignored_rows, dict(ignored_reasons), issues

    def _looks_like_main_table_attempt(self, row: HistoricalRow) -> bool:
        if row.assignment:
            return True
        if not row.unit_code:
            return False
        return bool(_has_minimal_unit_data(row) or row.clients or row.payments)

    def _row_diagnostic_value(self, row: HistoricalRow) -> str:
        parts = []
        if row.unit_code:
            parts.append(f"Unidad: {row.unit_code}")
        if row.assignment and row.assignment.assignment_number:
            parts.append(f"Encargo: {row.assignment.assignment_number}")
        if row.clients:
            parts.append(f"Clientes: {len(row.clients)}")
        if row.payments:
            parts.append(f"Pagos mensuales: {len(row.payments)}")
        return "; ".join(parts)

    def _receipt_date_issues(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
        row: HistoricalRow,
    ) -> list[ParserIssue]:
        receipt_columns = _receipt_columns(columns)
        receipt_tokens = self._receipt_tokens(sheet, row_number, receipt_columns)
        classified_receipts = _classify_receipt_tokens(receipt_tokens)
        classified_payment_dates = _classified_historical_payment_dates(
            _split_historical_date_values(self._value(sheet, row_number, columns.get("payment_dates")))
        )
        for category, date_values in _embedded_special_date_markers(classified_receipts).items():
            classified_payment_dates[category].extend(date_values)
        classified_payment_dates = self._apply_special_receipt_date_fallbacks(classified_receipts, classified_payment_dates)

        issues = []
        dates_column = columns.get("payment_dates")
        ordinary_dates = classified_payment_dates[RECEIPT_CATEGORY_ORDINARY]
        ordinary_groups = self._ordinary_receipt_date_groups(classified_receipts, ordinary_dates, columns)
        for group in ordinary_groups:
            ordinary_receipts = group["receipts"]
            group_dates = group["dates"]
            if not (ordinary_receipts or group_dates) or len(ordinary_receipts) == len(group_dates):
                continue
            issues.append(
                ParserIssue(
                    code="HIST_PAYMENT_DATE_RECEIPT_MISMATCH",
                    severity="blocking",
                    message="La cantidad de fechas ordinarias no coincide con la cantidad de recibos ordinarios.",
                    sheet_name=sheet.name,
                    row_number=row_number,
                    column_letter=_issue_columns(dates_column, receipt_columns),
                    unit_code=row.unit_code or "",
                    field_name=_issue_field("FECHA", receipt_columns),
                    found_value=f"{len(group_dates)} fechas ordinarias / {len(ordinary_receipts)} recibos ordinarios",
                    cause="La cantidad de fechas ordinarias no coincide con la cantidad de recibos ordinarios.",
                    extra_data={
                        "date_count": len(group_dates),
                        "receipt_count": len(ordinary_receipts),
                        "ordinary_date_count": len(group_dates),
                        "ordinary_receipt_count": len(ordinary_receipts),
                        "credit_receipt_count": len(classified_receipts[RECEIPT_CATEGORY_CREDIT]),
                        "subsidy_receipt_count": len(classified_receipts[RECEIPT_CATEGORY_SUBSIDY]),
                        "destination": group["destination"],
                        "date_header": dates_column.header if dates_column else "",
                        "receipt_headers": [column.header for column in receipt_columns],
                        "grouping_name": row.grouping_name,
                        "scope": "cell",
                    },
                )
            )

        return issues

    def _ordinary_receipt_date_groups(
        self,
        classified_receipts: dict[str, list[ReceiptToken]],
        ordinary_dates: list[str],
        columns: dict[str, DetectedColumn],
    ) -> list[dict]:
        constructora_dates = [date for date in ordinary_dates if _payment_destination_from_date_value(date) == PAYMENT_DESTINATION_CONSTRUCTORA]
        fiducia_dates = [date for date in ordinary_dates if _payment_destination_from_date_value(date) == PAYMENT_DESTINATION_FIDUCIARIA]
        standard_receipts = [
            receipt for receipt in classified_receipts[RECEIPT_CATEGORY_ORDINARY] if receipt.source == "receipt_numbers"
        ]
        fiducia_receipts = [
            receipt for receipt in classified_receipts[RECEIPT_CATEGORY_ORDINARY] if receipt.source == "fidubogota_receipt_numbers"
        ]
        if "fidubogota_receipt_numbers" not in columns:
            return [
                {
                    "destination": "legacy",
                    "receipts": classified_receipts[RECEIPT_CATEGORY_ORDINARY],
                    "dates": ordinary_dates,
                }
            ]
        if fiducia_receipts and not standard_receipts:
            return [
                {
                    "destination": PAYMENT_DESTINATION_FIDUCIARIA,
                    "receipts": fiducia_receipts,
                    "dates": fiducia_dates,
                }
            ]
        return [
            {
                "destination": PAYMENT_DESTINATION_CONSTRUCTORA,
                "receipts": standard_receipts,
                "dates": constructora_dates,
            },
            {
                "destination": PAYMENT_DESTINATION_FIDUCIARIA,
                "receipts": fiducia_receipts,
                "dates": fiducia_dates,
            },
        ]

    def _apply_special_receipt_date_fallbacks(
        self,
        classified_receipts: dict[str, list[ReceiptToken]],
        classified_payment_dates: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        return classified_payment_dates

    def receipt_date_sequences(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
    ) -> dict[str, list[tuple[str, str]]]:
        receipt_columns = _receipt_columns(columns)
        if not receipt_columns:
            return {
                RECEIPT_CATEGORY_ORDINARY: [],
                RECEIPT_CATEGORY_CREDIT: [],
                RECEIPT_CATEGORY_SUBSIDY: [],
                RECEIPT_CATEGORY_TRANSFER: [],
                RECEIPT_CATEGORY_CESSION: [],
            }
        classified_receipts = _classify_receipt_tokens(self._receipt_tokens(sheet, row_number, receipt_columns))
        classified_payment_dates = _classified_historical_payment_dates(
            _split_historical_date_values(self._value(sheet, row_number, columns.get("payment_dates")))
        )
        for category, date_values in _embedded_special_date_markers(classified_receipts).items():
            classified_payment_dates[category].extend(date_values)
        classified_payment_dates = self._apply_special_receipt_date_fallbacks(classified_receipts, classified_payment_dates)
        pairs = self._receipt_date_token_pairs(sheet, row_number, columns)
        return {
            RECEIPT_CATEGORY_ORDINARY: [
                (receipt.raw_value, date_value)
                for category, receipt, date_value in pairs
                if category == RECEIPT_CATEGORY_ORDINARY
            ],
            RECEIPT_CATEGORY_CREDIT: [
                (receipt.raw_value, date_value)
                for category, receipt, date_value in pairs
                if category == RECEIPT_CATEGORY_CREDIT
            ],
            RECEIPT_CATEGORY_SUBSIDY: [],
            RECEIPT_CATEGORY_TRANSFER: [
                (receipt.raw_value, date_value)
                for category, receipt, date_value in pairs
                if category == RECEIPT_CATEGORY_TRANSFER
            ],
            RECEIPT_CATEGORY_CESSION: [
                (receipt.raw_value, date_value)
                for category, receipt, date_value in pairs
                if category == RECEIPT_CATEGORY_CESSION
            ],
        }

    def _receipt_tokens(
        self,
        sheet: RawSheet,
        row_number: int,
        receipt_columns: list[DetectedColumn],
    ) -> list[ReceiptToken]:
        cache = getattr(self, "_receipt_tokens_cache", None)
        cache_key = (id(sheet), row_number, tuple(column.index for column in receipt_columns))
        if cache is not None and cache_key in cache:
            return cache[cache_key]
        tokens = []
        global_order = 1
        for column in receipt_columns:
            for source_order, raw_value in enumerate(_split_receipt_values(self._value(sheet, row_number, column)), start=1):
                tokens.append(
                    ReceiptToken(
                        raw_value=raw_value,
                        source=column.key,
                        source_header=column.header,
                        source_column=column.letter,
                        source_order=source_order,
                        order=global_order,
                        category=classify_receipt(raw_value),
                    )
                )
                global_order += 1
        if cache is not None:
            cache[cache_key] = tokens
        return tokens

    def _extract_sheet_structure(self, sheet: RawSheet) -> tuple[str, str, str]:
        title = ""
        for (row, _), cell in sorted(sheet.cells.items()):
            if row > 3:
                break
            value = clean_text(cell.value)
            if value and ("proyecto" in normalize_text(value) or "conjunto" in normalize_text(value)):
                title = value
                break
        project = ""
        descriptor = sheet.name
        match = re.search(r"proyecto\s+(.+?)\s*-\s*(.+)$", title, flags=re.IGNORECASE)
        if match:
            project = match.group(1).strip().title()
            descriptor = match.group(2).strip().title()
        elif title:
            title_main = re.split(r"\s+-\s+", title, maxsplit=1)[0]
            normalized_title = normalize_text(title_main)
            sheet_name_normalized = normalize_text(sheet.name)
            title_without_group = re.sub(rf"\b{re.escape(sheet_name_normalized)}\b", "", normalized_title, flags=re.IGNORECASE)
            title_without_group = re.sub(r"\b(conjunto|cerrado|proyecto)\b", "", title_without_group, flags=re.IGNORECASE)
            title_without_group = re.sub(r"\b\d+\s+(apartamentos?|locales?|unidades?)\b.*$", "", title_without_group, flags=re.IGNORECASE)
            project = title_without_group.strip().title()
            descriptor = sheet.name
        if not project:
            project = self._project_from_filename()
        grouping_code = sheet.name.strip()
        descriptor_without_code = re.sub(rf"\b{re.escape(grouping_code)}\b", "", descriptor, flags=re.IGNORECASE).strip()
        grouping_name = f"{grouping_code} {descriptor_without_code}".strip()
        return project, grouping_code, grouping_name

    def _project_from_filename(self) -> str:
        name = re.sub(r"\.(xlsx|xls)$", "", self.path.name, flags=re.IGNORECASE)
        name = re.sub(r"^libro[_\s-]*", "", name, flags=re.IGNORECASE)
        name = re.sub(r"\([^)]*\)", "", name)
        name = name.replace("_", " ").strip()
        return re.sub(r"\s+", " ", name).title() or "Proyecto sin identificar"

    def _extract_row(
        self,
        sheet: RawSheet,
        row_number: int,
        project: str,
        grouping_code: str,
        grouping_name: str,
        columns: dict[str, DetectedColumn],
        payment_columns: list[DetectedPaymentColumn],
    ) -> HistoricalRow | None:
        unit = self._value(sheet, row_number, columns.get("unit"))
        area = parse_decimal(self._value(sheet, row_number, columns.get("area")))
        financial_entity = self._value(sheet, row_number, columns.get("financial_entity")) or ""
        property_value = parse_decimal(self._value(sheet, row_number, columns.get("property_value")))
        previous_assignment_number = self._previous_assignment_number(sheet, row_number, columns)
        new_assignment_number = self._value(sheet, row_number, columns.get("new_assignment_number"))
        assignment_number = new_assignment_number or previous_assignment_number
        document_number = self._value(sheet, row_number, columns.get("document_number"))
        observation = self._value(sheet, row_number, columns.get("observations")) or ""
        clients = self._extract_clients(sheet, row_number, columns, document_number)
        reconstructed_payments = self._reconstruct_payments(sheet, row_number, columns, payment_columns)
        has_receipt_tokens = bool(self._receipt_tokens(sheet, row_number, _receipt_columns(columns)))
        payments = [] if reconstructed_payments or has_receipt_tokens else self._extract_payments(sheet, row_number, columns, payment_columns)

        has_minimal_unit_data = bool(unit and area is not None and property_value is not None)
        if not any([has_minimal_unit_data, unit, assignment_number, document_number, clients, payments, reconstructed_payments]):
            return None

        return HistoricalRow(
            sheet_name=sheet.name,
            row_number=row_number,
            project=project,
            grouping_type=self.grouping_type_hint,
            grouping_code=grouping_code,
            grouping_name=grouping_name,
            unit_code=unit,
            unit_name=unit,
            area=area,
            property_value=property_value,
            financial_entity=financial_entity,
            assignment=HistoricalAssignment(
                assignment_number=assignment_number,
                status=self._value(sheet, row_number, columns.get("assignment_status")),
                previous_assignment_number=previous_assignment_number if new_assignment_number else None,
                adhesion_contract_date=self._value(sheet, row_number, columns.get("adhesion_contract_date")),
                promise_date=self._value(sheet, row_number, columns.get("promise_date")),
                promised_delivery_date=self._value(sheet, row_number, columns.get("promised_delivery_date")),
                actual_delivery_date=self._value(sheet, row_number, columns.get("actual_delivery_date")),
            )
            if assignment_number
            else None,
            observation=observation,
            clients=clients,
            payments=payments,
            reconstructed_payments=reconstructed_payments,
        )

    def _extract_clients(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
        document_number: str | None,
    ) -> list[HistoricalClient]:
        clients = []
        name_columns = [
            column for key, column in columns.items() if key.startswith("client_name_")
        ]
        name_columns.sort(key=lambda column: column.index)
        document_parts = _split_document_values(document_number)
        phone_parts = _split_phone_values(self._value(sheet, row_number, columns.get("phone")))
        email_parts = _split_contact_values(self._value(sheet, row_number, columns.get("email")), separators=("/", ";", ","))
        contact_value = self._value(sheet, row_number, columns.get("contact_name"))
        names = []
        for column in name_columns:
            names.extend(_split_client_name_values(self._value(sheet, row_number, column)))
        for index, name in enumerate(names, start=1):
            if not name:
                continue
            clients.append(
                HistoricalClient(
                    order=index,
                    name=name,
                    document_number=document_parts[index - 1] if index <= len(document_parts) else None,
                    document_type=parse_document_type(columns.get("document_number").header if columns.get("document_number") else None),
                    is_primary=index == 1,
                    phone=phone_parts[index - 1] if index <= len(phone_parts) else None,
                    email=email_parts[index - 1] if index <= len(email_parts) else None,
                    contact_name=contact_value,
                )
            )
        return clients

    def _extract_payments(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
        payment_columns: list[DetectedPaymentColumn],
    ) -> list[HistoricalMonthlyPayment]:
        payment_dates = self._value(sheet, row_number, columns.get("payment_dates"))
        payment_dates_text = clean_text(payment_dates) or ""
        if "||" in payment_dates_text and not _split_historical_date_values(payment_dates):
            return []
        payments = []
        for column in payment_columns:
            destination = _payment_destination_from_header(column.header)
            cell = sheet.cell(row_number, column.index)
            amount = parse_decimal(cell.value if cell else None)
            if amount is None:
                continue
            payments.append(
                HistoricalMonthlyPayment(
                    month=column.month,
                    year=column.year,
                    amount=amount,
                    source_row=row_number,
                    source_column=column.letter,
                    source_header=column.header,
                    destination=destination,
                    has_formula=cell.has_formula if cell else False,
                    has_cached_value=cell.has_cached_value if cell else False,
                )
            )
        return payments

    def _reconstruct_payments(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
        payment_columns: list[DetectedPaymentColumn],
    ) -> list[ReconstructedHistoricalPayment]:
        receipt_columns = _receipt_columns(columns)
        ordered_pairs = self._receipt_date_token_pairs(sheet, row_number, columns) if receipt_columns else []
        separator_payments = self._reconstruct_separator_payments(sheet, row_number, columns, payment_columns)
        special_value_columns = {
            category: _special_value_columns(columns, category)
            for category in (
                RECEIPT_CATEGORY_CREDIT,
                RECEIPT_CATEGORY_TRANSFER,
                RECEIPT_CATEGORY_CESSION,
            )
        }
        if not ordered_pairs and not any(special_value_columns.values()):
            return separator_payments
        if not any(special_value_columns.values()):
            ordinary_pairs = [pair for pair in ordered_pairs if pair[0] == RECEIPT_CATEGORY_ORDINARY]
            if not ordinary_pairs or (not payment_columns and "received_values" not in columns):
                return separator_payments
            return [
                *self._reconstruct_regular_payments(sheet, row_number, columns, ordinary_pairs, payment_columns),
                *separator_payments,
            ]

        reconstructed = []
        ordinary_pairs = [pair for pair in ordered_pairs if pair[0] == RECEIPT_CATEGORY_ORDINARY]
        if ordinary_pairs and (payment_columns or "received_values" in columns):
            reconstructed.extend(
                self._reconstruct_regular_payments(sheet, row_number, columns, ordinary_pairs, payment_columns)
            )

        value_source_order = len(reconstructed) + 1
        for category in (
            RECEIPT_CATEGORY_CREDIT,
            RECEIPT_CATEGORY_TRANSFER,
            RECEIPT_CATEGORY_CESSION,
        ):
            category_values = self._individual_special_payment_values(
                sheet,
                row_number,
                special_value_columns[category],
            )
            category_pairs = [pair for pair in ordered_pairs if pair[0] == category]
            if not category_pairs:
                continue
            if len(category_pairs) != len(category_values):
                continue
            for pair, value in zip(category_pairs, category_values, strict=False):
                reconstructed.append(
                    self._reconstructed_payment_from_pair_value(
                        pair,
                        value,
                        row_number=row_number,
                        sheet_name=sheet.name,
                        value_source_order=value_source_order,
                    )
                )
                value_source_order += 1
        reconstructed.extend(separator_payments)
        return sorted(reconstructed, key=lambda payment: (payment.receipt_order, payment.value_source_order))

    def _reconstruct_regular_payments(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
        ordered_pairs: list[tuple[str, ReceiptToken, str]],
        payment_columns: list[DetectedPaymentColumn],
    ) -> list[ReconstructedHistoricalPayment]:
        if "fidubogota_receipt_numbers" not in columns and "received_values" not in columns:
            return self._reconstruct_payments_from_monthly_values(sheet, row_number, ordered_pairs, payment_columns)
        constructora_pairs = [
            pair
            for pair in ordered_pairs
            if pair[0] == RECEIPT_CATEGORY_ORDINARY and _payment_destination_from_pair(pair) == PAYMENT_DESTINATION_CONSTRUCTORA
        ]
        fiducia_pairs = [
            pair
            for pair in ordered_pairs
            if pair[0] == RECEIPT_CATEGORY_ORDINARY and _payment_destination_from_pair(pair) == PAYMENT_DESTINATION_FIDUCIARIA
        ]
        legacy_pairs = [
            pair
            for pair in ordered_pairs
            if pair[0] == RECEIPT_CATEGORY_ORDINARY and _payment_destination_from_pair(pair) is None
        ]

        reconstructed = []
        if constructora_pairs:
            received_column = columns.get("received_values")
            if received_column:
                received_values = self._individual_special_payment_values(
                    sheet,
                    row_number,
                    [received_column],
                    ignore_literal_zero_padding=not _row_has_receipt_or_date_context(sheet, row_number, columns),
                )
                if len(received_values) == len(constructora_pairs):
                    for order, (pair, value) in enumerate(zip(constructora_pairs, received_values, strict=False), start=1):
                        reconstructed.append(
                            self._reconstructed_payment_from_pair_value(
                                pair,
                                value,
                                row_number=row_number,
                                sheet_name=sheet.name,
                                value_source_order=order,
                                destination=PAYMENT_DESTINATION_CONSTRUCTORA,
                            )
                        )
            else:
                reconstructed.extend(
                    self._reconstruct_payments_from_monthly_values(
                        sheet,
                        row_number,
                        constructora_pairs,
                        [column for column in payment_columns if _payment_destination_from_header(column.header) == PAYMENT_DESTINATION_CONSTRUCTORA],
                    )
                )
        if fiducia_pairs:
            reconstructed.extend(
                self._reconstruct_payments_from_monthly_values(
                    sheet,
                    row_number,
                    fiducia_pairs,
                    [column for column in payment_columns if _payment_destination_from_header(column.header) == PAYMENT_DESTINATION_FIDUCIARIA],
                )
            )
        if legacy_pairs:
            reconstructed.extend(self._reconstruct_payments_from_monthly_values(sheet, row_number, legacy_pairs, payment_columns))
        return sorted(reconstructed, key=lambda payment: (payment.receipt_order, payment.value_source_order))

    def _reconstruct_separator_payments(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
        payment_columns: list[DetectedPaymentColumn],
    ) -> list[ReconstructedHistoricalPayment]:
        return []

    def _reconstruct_payments_from_monthly_values(
        self,
        sheet: RawSheet,
        row_number: int,
        ordered_pairs: list[tuple[str, ReceiptToken, str]],
        payment_columns: list[DetectedPaymentColumn],
    ) -> list[ReconstructedHistoricalPayment]:
        values = self._individual_payment_values(sheet, row_number, payment_columns)
        if len(ordered_pairs) != len(values):
            return []
        ordered_pairs = self._normalize_positional_payment_pair_years(sheet, row_number, ordered_pairs, values)
        reconstructed = []
        for value_source_order, (pair, value) in enumerate(zip(ordered_pairs, values, strict=False), start=1):
            if not _payment_value_matches_pair_period(pair, value):
                continue
            reconstructed.append(
                self._reconstructed_payment_from_pair_value(
                    pair,
                    value,
                    row_number=row_number,
                    sheet_name=sheet.name,
                    value_source_order=value_source_order,
                )
            )
        return sorted(reconstructed, key=lambda payment: payment.receipt_order)

    def _reconstructed_payment_from_pair_value(
        self,
        pair: tuple[str, ReceiptToken, str],
        value: dict,
        *,
        row_number: int,
        sheet_name: str,
        value_source_order: int,
        destination: str | None = None,
    ) -> ReconstructedHistoricalPayment:
        category, token, date_value = pair
        column = value["column"]
        return ReconstructedHistoricalPayment(
            category=category,
            date_value=date_value,
            receipt=token.raw_value,
            receipt_source=token.source,
            receipt_source_header=token.source_header,
            receipt_source_column=token.source_column,
            receipt_source_order=token.source_order,
            receipt_order=token.order,
            amount=value["amount"],
            value_source_column=column.letter,
            value_source_header=column.header,
            destination=destination or _payment_destination_from_pair(pair) or _payment_destination_from_header(column.header),
            value_source_order=value_source_order,
            source_row=row_number,
            sheet_name=sheet_name,
            value_had_formula=value["has_formula"],
            value_formula=value["formula"],
        )

    def _payment_reconstruction_issues(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
        payment_columns: list[DetectedPaymentColumn],
        row: HistoricalRow,
    ) -> list[ParserIssue]:
        pairs = self._receipt_date_token_pairs(sheet, row_number, columns)
        issues = []
        issues.extend(self._separator_payment_reconstruction_issues(sheet, row_number, columns, payment_columns, row))
        unsupported_formula_columns = [
            column
            for column in payment_columns
            if (cell := sheet.cell(row_number, column.index))
            and cell.has_formula
            and _parse_simple_sum_formula(cell.formula) is None
        ]
        for column in unsupported_formula_columns:
            cell = sheet.cell(row_number, column.index)
            issues.append(
                ParserIssue(
                    code="HIST_PAYMENT_FORMULA_NOT_RECONSTRUCTIBLE",
                    severity="info",
                    message="Formula de pago historico no soportada para reconstruccion individual.",
                    sheet_name=sheet.name,
                    row_number=row_number,
                    column_letter=column.letter,
                    unit_code=row.unit_code or "",
                    field_name=column.header,
                    found_value=_formula_text(cell.formula)[:500] if cell else "",
                    cause="La formula contiene operaciones o referencias que no se pueden descomponer de forma segura sin interpretar Excel.",
                    extra_data={"scope": "cell", "header": column.header},
                )
            )
        special_value_columns = {
            category: _special_value_columns(columns, category)
            for category in (
                RECEIPT_CATEGORY_CREDIT,
                RECEIPT_CATEGORY_TRANSFER,
                RECEIPT_CATEGORY_CESSION,
            )
        }
        issues.extend(self._regular_payment_value_issues(sheet, row_number, columns, payment_columns, row, pairs))
        if any(special_value_columns.values()):
            issues.extend(
                self._category_payment_value_issues(
                    sheet,
                    row_number,
                    columns,
                    payment_columns,
                    row,
                    pairs,
                    special_value_columns,
                )
            )
        return issues

    def _regular_payment_value_issues(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
        payment_columns: list[DetectedPaymentColumn],
        row: HistoricalRow,
        pairs: list[tuple[str, ReceiptToken, str]],
    ) -> list[ParserIssue]:
        ordinary_pairs = [pair for pair in pairs if pair[0] == RECEIPT_CATEGORY_ORDINARY]
        if not ordinary_pairs:
            return []
        if "fidubogota_receipt_numbers" not in columns and "received_values" not in columns:
            return self._positional_payment_value_issues(
                sheet,
                row_number,
                payment_columns,
                row,
                ordinary_pairs,
                code="HIST_PAYMENT_VALUE_COUNT_MISMATCH",
                destination="legacy",
            )

        issues = []
        constructora_pairs = [
            pair for pair in ordinary_pairs if _payment_destination_from_pair(pair) == PAYMENT_DESTINATION_CONSTRUCTORA
        ]
        fiducia_pairs = [
            pair for pair in ordinary_pairs if _payment_destination_from_pair(pair) == PAYMENT_DESTINATION_FIDUCIARIA
        ]
        received_column = columns.get("received_values")
        received_values = (
            self._individual_special_payment_values(
                sheet,
                row_number,
                [received_column],
                ignore_literal_zero_padding=not _row_has_receipt_or_date_context(sheet, row_number, columns),
            )
            if received_column
            else []
        )
        if constructora_pairs or received_values:
            if len(received_values) != len(constructora_pairs):
                issues.append(
                    self._payment_value_count_issue(
                        sheet,
                        row_number,
                        [received_column] if received_column else [],
                        row,
                        len(received_values),
                        len(constructora_pairs),
                        destination=PAYMENT_DESTINATION_CONSTRUCTORA,
                    )
                )
        fiducia_columns = [
            column
            for column in payment_columns
            if _payment_destination_from_header(column.header) == PAYMENT_DESTINATION_FIDUCIARIA
        ]
        fiducia_receipt_count, fiducia_date_count = self._fiduciary_receipt_date_counts(sheet, row_number, columns)
        if fiducia_receipt_count == fiducia_date_count:
            issues.extend(
                self._positional_payment_value_issues(
                    sheet,
                    row_number,
                    fiducia_columns,
                    row,
                    fiducia_pairs,
                    code="HIST_PAYMENT_VALUE_COUNT_MISMATCH",
                    destination=PAYMENT_DESTINATION_FIDUCIARIA,
                )
            )
        return issues

    def _positional_payment_value_issues(
        self,
        sheet: RawSheet,
        row_number: int,
        payment_columns: list[DetectedPaymentColumn],
        row: HistoricalRow,
        pairs: list[tuple[str, ReceiptToken, str]],
        *,
        code: str,
        destination: str,
    ) -> list[ParserIssue]:
        values = self._individual_payment_values(sheet, row_number, payment_columns)
        if not pairs and not values:
            return []
        if len(values) != len(pairs):
            return [
                self._payment_value_count_issue(
                    sheet,
                    row_number,
                    [value["column"] for value in values] or payment_columns,
                    row,
                    len(values),
                    len(pairs),
                    code=code,
                    destination=destination,
                )
            ]

        pairs = self._normalize_positional_payment_pair_years(sheet, row_number, pairs, values)
        issues = []
        for pair, value in zip(pairs, values, strict=False):
            if _payment_value_matches_pair_period(pair, value):
                continue
            issues.append(
                self._payment_period_mismatch_issue(
                    sheet,
                    row_number,
                    row,
                    pair,
                    value,
                    code=code,
                    destination=destination,
                )
            )
        return issues

    def _fiduciary_receipt_date_counts(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
    ) -> tuple[int, int]:
        receipt_columns = _receipt_columns(columns)
        classified_receipts = _classify_receipt_tokens(self._receipt_tokens(sheet, row_number, receipt_columns))
        classified_payment_dates = _classified_historical_payment_dates(
            _split_historical_date_values(self._value(sheet, row_number, columns.get("payment_dates")))
        )
        ordinary_dates = classified_payment_dates[RECEIPT_CATEGORY_ORDINARY]
        fiducia_dates = [
            date for date in ordinary_dates if _payment_destination_from_date_value(date) == PAYMENT_DESTINATION_FIDUCIARIA
        ]
        fiducia_receipts = [
            receipt
            for receipt in classified_receipts[RECEIPT_CATEGORY_ORDINARY]
            if receipt.source == "fidubogota_receipt_numbers"
        ]
        return len(fiducia_receipts), len(fiducia_dates)

    def _normalize_positional_payment_pair_years(
        self,
        sheet: RawSheet,
        row_number: int,
        pairs: list[tuple[str, ReceiptToken, str]],
        values: list[dict],
    ) -> list[tuple[str, ReceiptToken, str]]:
        if not pairs or len(pairs) != len(values):
            return pairs
        normalized_pairs = list(pairs)
        for index, (pair, value) in enumerate(zip(pairs, values, strict=False)):
            category, token, date_value = pair
            if category != RECEIPT_CATEGORY_ORDINARY:
                continue
            if _payment_destination_from_pair(pair) != PAYMENT_DESTINATION_FIDUCIARIA:
                continue
            original_parts = _historical_payment_date_parts(date_value)
            if original_parts is None:
                continue
            original_year, month, day = original_parts
            column = value["column"]
            if month != column.month or original_year == column.year:
                continue
            if abs(column.year - original_year) != 1:
                continue
            candidate_value = _replace_historical_payment_date_year(date_value, column.year)
            if not candidate_value or not self._candidate_year_fits_sequence(normalized_pairs, index, candidate_value):
                continue
            normalized_pair = (category, token, candidate_value)
            normalized_pairs[index] = normalized_pair
            record = {
                "sheet": sheet.name,
                "row": row_number,
                "receipt": token.raw_value,
                "source_column": token.source_column,
                "original": date_value,
                "interpreted": candidate_value,
            }
            if record not in self._historical_date_normalizations:
                self._historical_date_normalizations.append(record)

        return normalized_pairs

    def _candidate_year_fits_sequence(
        self,
        pairs: list[tuple[str, ReceiptToken, str]],
        index: int,
        candidate_value: str,
    ) -> bool:
        candidate_date = _historical_payment_date_as_date(candidate_value)
        original_date = _historical_payment_date_as_date(pairs[index][2])
        if candidate_date is None or original_date is None:
            return False

        previous_date = None
        for previous_pair in reversed(pairs[:index]):
            if _payment_destination_from_pair(previous_pair) != PAYMENT_DESTINATION_FIDUCIARIA:
                continue
            previous_date = _historical_payment_date_as_date(previous_pair[2])
            if previous_date:
                break

        next_date = None
        for next_pair in pairs[index + 1 :]:
            if _payment_destination_from_pair(next_pair) != PAYMENT_DESTINATION_FIDUCIARIA:
                continue
            next_date = _historical_payment_date_as_date(next_pair[2])
            if next_date:
                break

        def violations(value):
            count = 0
            if previous_date and value <= previous_date:
                count += 1
            if next_date and value >= next_date:
                count += 1
            return count

        return violations(candidate_date) < violations(original_date)

    def _payment_value_count_issue(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: list[DetectedColumn],
        row: HistoricalRow,
        value_count: int,
        pair_count: int,
        *,
        code: str = "HIST_PAYMENT_VALUE_COUNT_MISMATCH",
        destination: str,
        year: int | None = None,
        month: int | None = None,
    ) -> ParserIssue:
        return ParserIssue(
            code=code,
            severity="blocking",
            message="La cantidad de valores no permite reconstruir pagos historicos individuales.",
            sheet_name=sheet.name,
            row_number=row_number,
            column_letter="/".join(column.letter for column in columns),
            unit_code=row.unit_code or "",
            field_name=" / ".join(column.header for column in columns) or "RECIBIDO",
            found_value=f"{value_count} valores / {pair_count} recibos con fecha",
            cause="La cantidad de fechas, recibos y valores no coincide para la fuente y periodo correspondiente.",
            extra_data={
                "scope": "cell",
                "payment_pair_count": pair_count,
                "value_count": value_count,
                "destination": destination,
                "year": year,
                "month": month,
            },
        )

    def _payment_period_mismatch_issue(
        self,
        sheet: RawSheet,
        row_number: int,
        row: HistoricalRow,
        pair: tuple[str, ReceiptToken, str],
        value: dict,
        *,
        code: str,
        destination: str,
    ) -> ParserIssue:
        column = value["column"]
        date_key = _historical_payment_date_year_month(pair[2])
        value_key = (column.year, column.month)
        return ParserIssue(
            code=code,
            severity="blocking",
            message="El periodo de la fecha historica no coincide con la columna de valor del movimiento.",
            sheet_name=sheet.name,
            row_number=row_number,
            column_letter=column.letter,
            unit_code=row.unit_code or "",
            field_name=column.header,
            found_value=(
                f"{pair[1].raw_value} / {pair[2]} -> "
                f"{column.header} ({value['amount']})"
            ),
            cause=(
                "El recibo, la fecha y el valor se pudieron emparejar por posicion, "
                "pero el mes/ano de la fecha no coincide con la columna donde esta el valor."
            ),
            extra_data={
                "scope": "cell",
                "payment_pair_count": 1,
                "value_count": 1,
                "destination": destination,
                "date_year": date_key[0] if date_key else None,
                "date_month": date_key[1] if date_key else None,
                "value_year": value_key[0],
                "value_month": value_key[1],
                "receipt": pair[1].raw_value,
                "date_value": pair[2],
                "value_column": column.header,
            },
        )

    def _separator_payment_reconstruction_issues(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
        payment_columns: list[DetectedPaymentColumn],
        row: HistoricalRow,
    ) -> list[ParserIssue]:
        return []

    def _historical_date_format_issues(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
        row: HistoricalRow,
    ) -> list[ParserIssue]:
        column = columns.get("payment_dates")
        if column is None:
            return []
        issues = []
        for date_value in _split_historical_date_values(self._value(sheet, row_number, column)):
            if not _looks_like_historical_text_date(date_value):
                continue
            if _is_valid_strict_historical_date(date_value):
                continue
            issues.append(
                ParserIssue(
                    code="HIST_INVALID_DATE_HEADER",
                    severity="blocking",
                    message=(
                        f"Fecha invalida en encabezado: '{date_value}'. "
                        "Formato esperado: MES.DD/AA o MES.DD/AAF."
                    ),
                    sheet_name=sheet.name,
                    row_number=row_number,
                    column_letter=column.letter,
                    unit_code=row.unit_code or "",
                    field_name=column.header,
                    found_value=date_value,
                    cause=(
                        "El valor parece una fecha historica, pero no cumple el formato exacto "
                        "MES.DD/AA o MES.DD/AAF con mes y dia validos."
                    ),
                    extra_data={"scope": "cell", "header": column.header},
                )
            )
        return issues

    def _category_payment_value_issues(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
        payment_columns: list[DetectedPaymentColumn],
        row: HistoricalRow,
        pairs: list[tuple[str, ReceiptToken, str]],
        special_value_columns: dict[str, list[DetectedColumn]],
    ) -> list[ParserIssue]:
        issues = []
        receipt_columns = _receipt_columns(columns)
        category_specs = {
            RECEIPT_CATEGORY_CREDIT: (
                "HIST_CREDIT_VALUE_RECEIPT_MISMATCH",
                "FECHA PAGO CREDITO",
                "credito",
                "CR",
            ),
            RECEIPT_CATEGORY_TRANSFER: (
                "HIST_TRANSFER_VALUE_RECEIPT_MISMATCH",
                "FECHA",
                "traslado",
                "TRASLADO",
            ),
            RECEIPT_CATEGORY_CESSION: (
                "HIST_CESSION_VALUE_RECEIPT_MISMATCH",
                "FECHA",
                "cesion",
                "CESION",
            ),
        }
        classified_receipts = _classify_receipt_tokens(self._receipt_tokens(sheet, row_number, receipt_columns))
        for category, (code, date_header, label, receipt_label) in category_specs.items():
            category_pairs = [pair for pair in pairs if pair[0] == category]
            category_receipts = classified_receipts[category]
            if category in {RECEIPT_CATEGORY_TRANSFER, RECEIPT_CATEGORY_CESSION}:
                category_receipts = [receipt for receipt in category_receipts if receipt.source == "receipt_numbers"]
            category_columns = special_value_columns[category]
            category_values = self._individual_special_payment_values(sheet, row_number, category_columns)
            if category in {RECEIPT_CATEGORY_TRANSFER, RECEIPT_CATEGORY_CESSION} and not category_pairs and not category_receipts:
                continue
            expected_pairs = len(category_receipts) if category in {RECEIPT_CATEGORY_TRANSFER, RECEIPT_CATEGORY_CESSION} else len(category_pairs)
            if len(category_pairs) == expected_pairs and len(category_pairs) == len(category_values):
                continue
            issue_columns = []
            date_column = columns.get("credit_payment_dates" if category == RECEIPT_CATEGORY_CREDIT else "payment_dates")
            if date_column:
                issue_columns.append(date_column.letter)
            issue_columns.extend(column.letter for column in receipt_columns)
            issue_columns.extend(column.letter for column in category_columns)
            issues.append(
                ParserIssue(
                    code=code,
                    severity="blocking" if category in {RECEIPT_CATEGORY_TRANSFER, RECEIPT_CATEGORY_CESSION} else "warning",
                    message=f"La cantidad de valores de {label} no permite reconstruir pagos historicos individuales.",
                    sheet_name=sheet.name,
                    row_number=row_number,
                    column_letter="/".join(issue_columns),
                    unit_code=row.unit_code or "",
                    field_name=" / ".join(
                        [date_header, *[column.header for column in receipt_columns], *[column.header for column in category_columns]]
                    ),
                    found_value=f"{len(category_values)} valores de {label} / {len(category_pairs)} recibos {receipt_label} con fecha",
                    cause=f"La cantidad de fechas, recibos y valores de {label} no coincide.",
                    extra_data={
                        "scope": "cell",
                        "receipt_category": category,
                        "payment_pair_count": len(category_pairs),
                        "receipt_count": len(category_receipts),
                        "value_count": len(category_values),
                    },
                )
            )
        return issues

    def _individual_payment_values(
        self,
        sheet: RawSheet,
        row_number: int,
        payment_columns: list[DetectedPaymentColumn],
        *,
        pair_groups: dict[tuple[int, int], list[tuple[str, ReceiptToken, str]]] | None = None,
    ) -> list[dict]:
        cache = getattr(self, "_payment_values_cache", None)
        paired_keys = tuple(sorted(pair_groups)) if pair_groups is not None else None
        cache_key = (id(sheet), row_number, tuple(column.index for column in payment_columns), paired_keys)
        if cache is not None and cache_key in cache:
            return cache[cache_key]
        values = []
        for column in payment_columns:
            cell = sheet.cell(row_number, column.index)
            if not cell:
                continue
            date_key = (column.year, column.month)
            if pair_groups is not None and not pair_groups.get(date_key) and _is_literal_zero_cell(cell):
                continue
            formula_values = _parse_simple_sum_formula(cell.formula)
            if formula_values is not None:
                values.extend(
                    {
                        "amount": amount,
                        "column": column,
                        "has_formula": True,
                        "formula": _formula_text(cell.formula),
                    }
                    for amount in formula_values
                )
                continue
            amount = parse_decimal(cell.value)
            if amount is None:
                continue
            values.append(
                {
                    "amount": amount,
                    "column": column,
                    "has_formula": bool(cell.has_formula),
                    "formula": _formula_text(cell.formula),
                }
            )
        if cache is not None:
            cache[cache_key] = values
        return values

    def _individual_special_payment_values(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: list[DetectedColumn],
        *,
        ignore_literal_zero_padding: bool = False,
    ) -> list[dict]:
        cache = getattr(self, "_payment_values_cache", None)
        cache_key = ("special", id(sheet), row_number, tuple(column.index for column in columns), ignore_literal_zero_padding)
        if cache is not None and cache_key in cache:
            return cache[cache_key]
        values = []
        for column in columns:
            cell = sheet.cell(row_number, column.index)
            if not cell:
                continue
            if ignore_literal_zero_padding and _is_received_literal_zero_padding(column, cell):
                continue
            formula_values = _parse_simple_sum_formula(cell.formula)
            if formula_values is not None:
                values.extend(
                    {
                        "amount": amount,
                        "column": column,
                        "has_formula": True,
                        "formula": _formula_text(cell.formula),
                    }
                    for amount in formula_values
                )
                continue
            amount = parse_decimal(cell.value)
            if amount is None:
                continue
            values.append(
                {
                    "amount": amount,
                    "column": column,
                    "has_formula": bool(cell.has_formula),
                    "formula": _formula_text(cell.formula),
                }
            )
        if cache is not None:
            cache[cache_key] = values
        return values

    def _receipt_date_token_pairs(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
    ) -> list[tuple[str, ReceiptToken, str]]:
        cache = getattr(self, "_receipt_date_pairs_cache", None)
        cache_key = (id(sheet), row_number)
        if cache is not None and cache_key in cache:
            return cache[cache_key]
        receipt_columns = _receipt_columns(columns)
        if not receipt_columns:
            return []
        classified_receipts = _classify_receipt_tokens(self._receipt_tokens(sheet, row_number, receipt_columns))
        classified_payment_dates = _classified_historical_payment_dates(
            _split_historical_date_values(self._value(sheet, row_number, columns.get("payment_dates")))
        )
        for category, date_values in _embedded_special_date_markers(classified_receipts).items():
            classified_payment_dates[category].extend(date_values)
        classified_payment_dates = self._apply_special_receipt_date_fallbacks(classified_receipts, classified_payment_dates)
        pairs = []
        ordinary_dates = classified_payment_dates[RECEIPT_CATEGORY_ORDINARY]
        for group in self._ordinary_receipt_date_groups(classified_receipts, ordinary_dates, columns):
            receipts = group["receipts"]
            dates = group["dates"]
            if len(dates) == len(receipts):
                pairs.extend((RECEIPT_CATEGORY_ORDINARY, receipt, date_value) for receipt, date_value in zip(receipts, dates, strict=False))

        for category, dates in (
            (RECEIPT_CATEGORY_CREDIT, _split_historical_date_values(self._value(sheet, row_number, columns.get("credit_payment_dates")))),
            (RECEIPT_CATEGORY_TRANSFER, classified_payment_dates[RECEIPT_CATEGORY_TRANSFER]),
            (RECEIPT_CATEGORY_CESSION, classified_payment_dates[RECEIPT_CATEGORY_CESSION]),
        ):
            receipts = classified_receipts[category]
            if category in {RECEIPT_CATEGORY_TRANSFER, RECEIPT_CATEGORY_CESSION}:
                receipts = [receipt for receipt in receipts if receipt.source == "receipt_numbers"]
            if len(dates) == len(receipts):
                pairs.extend((category, receipt, date_value) for receipt, date_value in zip(receipts, dates, strict=False))
        if cache is not None:
            cache[cache_key] = pairs
        return pairs

    def _extract_novelty(
        self,
        sheet: RawSheet,
        row_number: int,
        project: str,
        grouping_code: str,
        grouping_name: str,
        columns: dict[str, DetectedColumn],
        header_row: int,
        historical_section: str,
        section_month: int | None,
        section_year: int | None,
    ) -> HistoricalNovelty | None:
        cells = []
        for column in range(1, sheet.used_columns + 1):
            cell = sheet.cell(row_number, column)
            if not cell or cell.value in ("", None):
                continue
            header_cell = sheet.cell(header_row, column)
            header = clean_text(header_cell.value if header_cell else None)
            cells.append(
                HistoricalNoveltyCell(
                    coordinate=cell.coordinate,
                    column_letter=cell.letter,
                    column_index=cell.column,
                    header=header,
                    value=cell.value,
                    formula=cell.formula,
                    has_cached_value=cell.has_cached_value,
                    is_date=cell.is_date,
                )
            )
        if not cells:
            return None

        unit = self._value(sheet, row_number, columns.get("unit"))
        previous_assignment_number = self._previous_assignment_number(sheet, row_number, columns)
        new_assignment_number = self._value(sheet, row_number, columns.get("new_assignment_number"))
        assignment_number = new_assignment_number or previous_assignment_number
        client_name = self._first_client_name(sheet, row_number, columns)
        document_number = self._value(sheet, row_number, columns.get("document_number"))
        if not any([unit, assignment_number, client_name, document_number]):
            return None
        return HistoricalNovelty(
            sheet_name=sheet.name,
            row_number=row_number,
            project=project,
            grouping_type=self.grouping_type_hint,
            grouping_code=grouping_code,
            grouping_name=grouping_name,
            unit_code=unit,
            unit_name=unit,
            assignment=HistoricalAssignment(
                assignment_number=assignment_number,
                status=self._value(sheet, row_number, columns.get("assignment_status")),
                previous_assignment_number=previous_assignment_number if new_assignment_number else None,
            )
            if assignment_number
            else None,
            historical_section=historical_section,
            section_month=section_month,
            section_year=section_year,
            cells=cells,
        )

    def _novelty_subtitle(self, sheet: RawSheet, row_number: int, columns: dict[str, DetectedColumn]) -> str:
        values = [
            clean_text(cell.value)
            for cell in sheet.row_cells(row_number)
            if clean_text(cell.value)
        ]
        if not values or len(values) > 3:
            return ""
        if any(
            self._value(sheet, row_number, columns.get(key))
            for key in ["assignment_number", "legacy_assignment_number", "new_assignment_number", "document_number"]
        ):
            return ""
        if self._first_client_name(sheet, row_number, columns):
            return ""
        text = " ".join(values).strip()
        normalized = normalize_text(text)
        if normalized in {"ventas", "por vender", "total", "subtotal"}:
            return ""
        return text

    def _first_client_name(self, sheet: RawSheet, row_number: int, columns: dict[str, DetectedColumn]) -> str | None:
        name_columns = [column for key, column in columns.items() if key.startswith("client_name_")]
        name_columns.sort(key=lambda column: column.index)
        for column in name_columns:
            value = self._value(sheet, row_number, column)
            if value:
                return value
        return None

    def _formula_issues_for_row(
        self,
        sheet: RawSheet,
        row_number: int,
        payment_columns: list[DetectedPaymentColumn],
        row: HistoricalRow,
    ) -> tuple[list[ParserIssue], dict[str, str]]:
        issues = []
        cached_columns = {}
        for column in payment_columns:
            cell = sheet.cell(row_number, column.index)
            if cell and cell.has_formula and not cell.has_cached_value:
                issues.append(
                    ParserIssue(
                        code="FORMULA_WITHOUT_CACHED_VALUE",
                        severity="error",
                        message="Celda relevante con formula sin valor calculado disponible.",
                        sheet_name=sheet.name,
                        row_number=row_number,
                        column_letter=column.letter,
                        unit_code=row.unit_code,
                        field_name=column.header,
                        found_value=str(cell.formula or cell.value or "")[:500],
                        cause="La celda contiene una formula de Excel sin valor calculado disponible para importar.",
                        extra_data={"scope": "cell", "header": column.header},
                    )
                )
            elif cell and cell.has_formula and parse_decimal(cell.value) is not None:
                cached_columns[column.letter] = column.header
        return issues, cached_columns

    def _value(self, sheet: RawSheet, row_number: int, column: DetectedColumn | None) -> str | None:
        if column is None:
            return None
        cell = sheet.cell(row_number, column.index)
        return clean_text(cell.value if cell else None)

    def _previous_assignment_number(
        self,
        sheet: RawSheet,
        row_number: int,
        columns: dict[str, DetectedColumn],
    ) -> str | None:
        return self._value(sheet, row_number, columns.get("assignment_number")) or self._value(
            sheet,
            row_number,
            columns.get("legacy_assignment_number"),
        )

    def _is_row_empty(self, sheet: RawSheet, row_number: int) -> bool:
        return sheet.is_row_empty(row_number)

    def _is_decorative_or_total_row(self, sheet: RawSheet, row_number: int) -> bool:
        values = [
            normalize_text(cell.value)
            for cell in sheet.row_cells(row_number)
            if normalize_text(cell.value)
        ]
        if not values:
            return True
        joined = " ".join(values)
        if "total" in joined or "subtotal" in joined:
            return True
        if joined in {"ventas", "por vender", "novedades"}:
            return True
        if "novedades" in joined and len(values) <= 2:
            return True
        return False

    def _is_novelty_header_row(self, sheet: RawSheet, row_number: int) -> bool:
        values = [
            normalize_text(cell.value)
            for cell in sheet.row_cells(row_number)
            if normalize_text(cell.value)
        ]
        return len(values) <= 3 and any(value == "novedades" or "novedades" in value.split() for value in values)

    def _build_statistics(self, sheets: list[SheetData], issues: list[ParserIssue]) -> ParseStatistics:
        processed_sheets = [sheet for sheet in sheets if sheet.classification == "processable"]
        rows = [row for sheet in sheets for row in sheet.rows]
        novelties = [novelty for sheet in sheets for novelty in sheet.novelties]
        unique_clients = Counter(
            (row.sheet_name, row.row_number, client.order) for row in rows for client in row.clients
        )
        unique_assignments = {row.assignment.assignment_number for row in rows if row.assignment}
        return ParseStatistics(
            sheets_total=len(sheets),
            sheets_processed=len(processed_sheets),
            valid_rows=len(rows),
            ignored_rows=sum(sheet.ignored_rows for sheet in sheets),
            client_appearances_found=len(unique_clients),
            distinct_assignments_found=len(unique_assignments),
            payment_entries_found=sum(len(row.payments) for row in rows),
            payment_columns_detected=sum(len(sheet.payment_columns) for sheet in sheets),
            historical_novelties_found=len(novelties),
            issues_found=len(issues),
        )

    def _duplicate_assignment_issues(self, sheets: list[SheetData]) -> list[ParserIssue]:
        occurrences: dict[str, list[tuple[SheetData, HistoricalRow]]] = defaultdict(list)
        for sheet in sheets:
            for row in sheet.rows:
                if row.assignment and row.assignment.assignment_number:
                    occurrences[normalize_text(row.assignment.assignment_number)].append((sheet, row))

        issues: list[ParserIssue] = []
        for rows in occurrences.values():
            unit_keys = {
                (
                    normalize_text(row.project),
                    normalize_text(row.grouping_name or row.grouping_code),
                    normalize_text(row.unit_code or row.unit_name),
                )
                for _, row in rows
            }
            if len(unit_keys) <= 1:
                continue
            locations = ", ".join(
                f"{row.sheet_name} fila {row.row_number} unidad {row.unit_code or row.unit_name or '-'}"
                for _, row in rows[:4]
            )
            if len(rows) > 4:
                locations += f" y {len(rows) - 4} mas"
            for sheet, row in rows:
                column = self._assignment_issue_column(sheet, row)
                issues.append(
                    ParserIssue(
                        code="HIST_DUPLICATE_ASSIGNMENT_NUMBER",
                        severity="blocking",
                        message="El mismo encargo fiduciario aparece asociado a mas de una unidad.",
                        sheet_name=row.sheet_name,
                        row_number=row.row_number,
                        column_letter=column.letter if column else "",
                        unit_code=row.unit_code or "",
                        field_name=column.header if column else "ENCARGO FIDUCIARIO",
                        found_value=row.assignment.assignment_number,
                        cause=f"Encargo duplicado en unidades distintas: {locations}.",
                        extra_data={
                            "assignment_number": row.assignment.assignment_number,
                            "locations": [
                                {
                                    "sheet": duplicate_row.sheet_name,
                                    "row": duplicate_row.row_number,
                                    "unit": duplicate_row.unit_code or duplicate_row.unit_name or "",
                                }
                                for _, duplicate_row in rows
                            ],
                        },
                    )
                )
        return issues

    def _assignment_issue_column(self, sheet: SheetData, row: HistoricalRow) -> DetectedColumn | None:
        if row.assignment and row.assignment.previous_assignment_number and "new_assignment_number" in sheet.columns:
            return sheet.columns["new_assignment_number"]
        return sheet.columns.get("assignment_number") or sheet.columns.get("legacy_assignment_number") or sheet.columns.get(
            "new_assignment_number"
        )


def _split_document_values(value: str | None) -> list[str]:
    if value is None:
        return []
    parts = [part.strip() for part in str(value).split("/")]
    return [part for part in parts if part]


def _split_client_name_values(value: str | None) -> list[str]:
    text = clean_text(value)
    if not text:
        return []
    slash_parts = [part.strip() for part in re.split(r"\s*/\s*", text) if part.strip()]
    if len(slash_parts) > 1 and all(_looks_like_client_name_part(part) for part in slash_parts):
        return slash_parts
    return [text]


def _looks_like_client_name_part(value: str) -> bool:
    normalized = normalize_text(value)
    if not normalized or "@" in value:
        return False
    if _digit_count(value):
        return False
    return len(re.findall(r"[a-z]", normalized)) >= 3


def _split_contact_values(value: str | None, *, separators=("/",)) -> list[str]:
    if value is None:
        return []
    text = clean_text(value)
    if not text:
        return []
    pattern = "|".join(re.escape(separator) for separator in separators)
    return [_normalize_contact_value(part) for part in re.split(pattern, text) if part.strip()]


def _normalize_contact_value(value: str) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("mailto:"):
        text = text[7:].strip()
    return text.lower() if "@" in text else text


def _split_phone_values(value: str | None) -> list[str]:
    text = clean_text(value)
    if not text:
        return []
    slash_parts = _split_contact_values(text, separators=("/",))
    if len(slash_parts) > 1:
        return slash_parts
    hyphen_parts = [part.strip() for part in re.split(r"\s*-\s*", text) if part.strip()]
    if len(hyphen_parts) > 1 and all(_digit_count(part) >= 7 for part in hyphen_parts):
        return hyphen_parts
    return [text]


def _digit_count(value: str) -> int:
    return sum(1 for char in value if char.isdigit())


def _split_receipt_values(value: str | None) -> list[str]:
    text = clean_text(value)
    if not text:
        return []
    parts = []
    for hyphen_part in re.split(r"\s*-\s*", text):
        hyphen_part = hyphen_part.strip()
        if not hyphen_part:
            continue
        if _looks_like_embedded_historical_date_marker(hyphen_part):
            parts.append(hyphen_part)
            continue
        parts.extend(part.strip() for part in re.split(r"\s*/\s*", hyphen_part) if part.strip())
    return parts


def _looks_like_embedded_historical_date_marker(value: str | None) -> bool:
    text = clean_text(value) or ""
    compact = re.sub(r"\s+", "", text).upper()
    return bool(re.search(r"\(?[A-ZÁÉÍÓÚ]{3,}\.\d{1,2}/\d{2,4}(?:F|CESION|TRASL(?:ADO)?)?\)?", compact))


def _parse_simple_sum_formula(value: str | None):
    formula = _formula_text(value)
    if not formula:
        return None
    expression = formula[1:] if formula.startswith("=") else formula
    if not expression or re.search(r"[A-Za-z()*/^]", expression):
        return None
    if "-" in expression.lstrip("-"):
        tokens = re.findall(r"[+-]?\s*\d+(?:[.,]\d+)?", expression)
        if not tokens or "".join(token.replace(" ", "") for token in tokens) != expression.replace(" ", ""):
            return None
        amounts = []
        for token in tokens:
            try:
                amounts.append(Decimal(token.replace(" ", "").replace(",", ".")))
            except InvalidOperation:
                return None
        return [sum(amounts).quantize(Decimal("0.01"))]
    parts = [part.strip() for part in expression.split("+")]
    if len(parts) < 2 or any(not part for part in parts):
        return None
    amounts = [parse_decimal(part) for part in parts]
    if any(amount is None for amount in amounts):
        return None
    return amounts


def _formula_text(value: str | None) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return text if text.startswith("=") else f"={text}"


def classify_receipt(value: str | None) -> str:
    text = clean_text(value)
    if not text:
        return RECEIPT_CATEGORY_ORDINARY
    marker_text = text.upper()
    if re.search(r"\d\s*[\s._/-]*TRASL(?:ADO)?(?:[\s._/-]|$)", marker_text):
        return RECEIPT_CATEGORY_TRANSFER
    if re.search(r"\d\s*[\s._/-]*CESION(?:[\s._/-]|$)", marker_text):
        return RECEIPT_CATEGORY_CESSION
    compact = re.sub(r"[\s._/()\\-]+", "", text).upper()
    if re.search(r"\dTRASLADO$", compact):
        return RECEIPT_CATEGORY_TRANSFER
    if re.search(r"\dCESION$", compact):
        return RECEIPT_CATEGORY_CESSION
    if re.search(r"\d(?:SUB|SB)$", compact):
        return RECEIPT_CATEGORY_SUBSIDY
    if re.search(r"\dCR$", compact):
        return RECEIPT_CATEGORY_CREDIT
    return RECEIPT_CATEGORY_ORDINARY


def _classify_receipt_values(receipts: list[str]) -> dict[str, list[str]]:
    classified = {
        RECEIPT_CATEGORY_ORDINARY: [],
        RECEIPT_CATEGORY_CREDIT: [],
        RECEIPT_CATEGORY_SUBSIDY: [],
        RECEIPT_CATEGORY_TRANSFER: [],
        RECEIPT_CATEGORY_CESSION: [],
    }
    for receipt in receipts:
        classified[classify_receipt(receipt)].append(receipt)
    return classified


def _classify_receipt_tokens(receipts: list[ReceiptToken]) -> dict[str, list[ReceiptToken]]:
    classified = {
        RECEIPT_CATEGORY_ORDINARY: [],
        RECEIPT_CATEGORY_CREDIT: [],
        RECEIPT_CATEGORY_SUBSIDY: [],
        RECEIPT_CATEGORY_TRANSFER: [],
        RECEIPT_CATEGORY_CESSION: [],
    }
    for receipt in receipts:
        classified[receipt.category].append(receipt)
    return classified


def _embedded_special_date_markers(classified_receipts: dict[str, list[ReceiptToken]]) -> dict[str, list[str]]:
    markers = {
        RECEIPT_CATEGORY_TRANSFER: [],
        RECEIPT_CATEGORY_CESSION: [],
    }
    for category in markers:
        for receipt in classified_receipts[category]:
            if receipt.source == "fidubogota_receipt_numbers" and _looks_like_embedded_historical_date_marker(receipt.raw_value):
                markers[category].append(receipt.raw_value)
    return markers


def classify_historical_payment_date(value: str | None) -> str:
    text = clean_text(value)
    if not text:
        return RECEIPT_CATEGORY_ORDINARY
    compact = re.sub(r"[\s._/()\\-]+", "", text).upper()
    if re.search(r"\d{2,4}TRASL(?:ADO)?$", compact):
        return RECEIPT_CATEGORY_TRANSFER
    if re.search(r"\d{2,4}CESION$", compact):
        return RECEIPT_CATEGORY_CESSION
    return RECEIPT_CATEGORY_ORDINARY


def _classified_historical_payment_dates(dates: list[str]) -> dict[str, list[str]]:
    classified = {
        RECEIPT_CATEGORY_ORDINARY: [],
        RECEIPT_CATEGORY_TRANSFER: [],
        RECEIPT_CATEGORY_CESSION: [],
    }
    for date_value in dates:
        classified[classify_historical_payment_date(date_value)].append(date_value)
    return classified


def _receipt_columns(columns: dict[str, DetectedColumn]) -> list[DetectedColumn]:
    return sorted(
        [
            column
            for key in ("receipt_numbers", "fidubogota_receipt_numbers")
            if (column := columns.get(key)) is not None
        ],
        key=lambda column: column.index,
    )


def _row_has_receipt_or_date_context(sheet: RawSheet, row_number: int, columns: dict[str, DetectedColumn]) -> bool:
    for column in _receipt_columns(columns):
        cell = sheet.cell(row_number, column.index)
        if clean_text(cell.value if cell else None):
            return True
    date_column = columns.get("payment_dates")
    if date_column:
        cell = sheet.cell(row_number, date_column.index)
        if _split_historical_date_values(cell.value if cell else None):
            return True
    return False


def _is_received_literal_zero_padding(column: DetectedColumn, cell) -> bool:
    return column.key == "received_values" and _is_literal_zero_cell(cell)


def _is_literal_zero_cell(cell) -> bool:
    if cell.has_formula:
        return False
    value = cell.value
    if isinstance(value, bool):
        return False
    if isinstance(value, Decimal):
        return value == 0
    if isinstance(value, int | float):
        return Decimal(str(value)) == 0
    return False


def _payment_destination_from_header(header: str) -> str | None:
    normalized = compact_normalized(header)
    if normalized.startswith("recibidofidubogota") or normalized.startswith("recibofiducia"):
        return PAYMENT_DESTINATION_FIDUCIARIA
    if normalized.startswith("recibido"):
        return PAYMENT_DESTINATION_CONSTRUCTORA
    return None


def _payment_destination_from_date_value(value: str | None) -> str:
    text = clean_text(value)
    return PAYMENT_DESTINATION_FIDUCIARIA if text.rstrip().rstrip(")").rstrip().endswith(("F", "f")) else PAYMENT_DESTINATION_CONSTRUCTORA


def _payment_destination_from_pair(pair: tuple[str, ReceiptToken, str]) -> str | None:
    category, token, date_value = pair
    if category == RECEIPT_CATEGORY_CREDIT:
        return PAYMENT_DESTINATION_CONSTRUCTORA
    if category != RECEIPT_CATEGORY_ORDINARY:
        return None
    if token.source == "fidubogota_receipt_numbers":
        return PAYMENT_DESTINATION_FIDUCIARIA
    if token.source == "receipt_numbers":
        return PAYMENT_DESTINATION_CONSTRUCTORA if _payment_destination_from_date_value(date_value) == PAYMENT_DESTINATION_CONSTRUCTORA else None
    return _payment_destination_from_date_value(date_value)


def _payment_value_matches_pair_period(pair: tuple[str, ReceiptToken, str], value: dict) -> bool:
    date_key = _historical_payment_date_year_month(pair[2])
    if not date_key:
        return True
    column = value["column"]
    if column.year is None or column.month is None:
        return True
    return date_key == (column.year, column.month)


def _special_value_columns(columns: dict[str, DetectedColumn], category: str) -> list[DetectedColumn]:
    return sorted(
        [
            column
            for key in SPECIAL_VALUE_COLUMN_KEYS.get(category, ())
            if (column := columns.get(key)) is not None
        ],
        key=lambda column: column.index,
    )


def _issue_columns(date_column: DetectedColumn | None, receipt_columns: list[DetectedColumn]) -> str:
    letters = []
    if date_column:
        letters.append(date_column.letter)
    letters.extend(column.letter for column in receipt_columns)
    return "/".join(letters)


def _issue_field(date_header: str, receipt_columns: list[DetectedColumn]) -> str:
    receipt_headers = [column.header for column in receipt_columns]
    if not receipt_headers:
        receipt_headers = ["RECIBOS"]
    return " / ".join([date_header, *receipt_headers])


def _split_historical_date_values(value: str | None, *, after_separator: bool = False) -> list[str]:
    text = clean_text(value)
    if not text:
        return []
    before, separator, after = text.partition("||")
    if after_separator:
        if not separator:
            return []
        text = after
    else:
        text = before
    text = clean_text(text)
    if not text:
        return []
    if re.match(r"^\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?$", text):
        return [text]
    return [part.strip() for part in re.split(r"\s*-\s*", text) if part.strip()]


def _historical_text_date_base(value: str | None) -> str:
    text = clean_text(value) or ""
    text = text.strip().strip("()").strip()
    text = re.sub(r"(TRASLADO|TRASL|CESION)$", "", text, flags=re.IGNORECASE).strip()
    return text


def _looks_like_historical_text_date(value: str | None) -> bool:
    text = _historical_text_date_base(value)
    if not text:
        return False
    compact = re.sub(r"\s+", "", text).upper()
    if re.match(r"^\d{4}-\d{2}-\d{2}(?:\d{2}:\d{2}:\d{2})?$", compact):
        return False
    return bool(
        re.match(r"^[A-Z]{3,4}", compact)
        and any(char.isdigit() for char in compact)
        and any(marker in compact for marker in (".", "/", "-", "F"))
    )


def _is_valid_strict_historical_date(value: str | None) -> bool:
    text = _historical_text_date_base(value)
    match = STRICT_HISTORICAL_DATE_RE.match(text)
    if not match:
        return False
    month = STRICT_HISTORICAL_DATE_MONTHS.get(match.group("month").upper())
    if not month:
        return False
    day = int(match.group("day"))
    year = 2000 + int(match.group("year"))
    return 1 <= day <= calendar.monthrange(year, month)[1]


def _historical_payment_date_parts(value: str | None) -> tuple[int, int, int] | None:
    text = _historical_text_date_base(value)
    match = STRICT_HISTORICAL_DATE_RE.match(text)
    if not match:
        return None
    month = STRICT_HISTORICAL_DATE_MONTHS.get(match.group("month").upper())
    if not month:
        return None
    day = int(match.group("day"))
    year = 2000 + int(match.group("year"))
    if not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None
    return year, month, day


def _historical_payment_date_as_date(value: str | None) -> date | None:
    parts = _historical_payment_date_parts(value)
    if not parts:
        return None
    year, month, day = parts
    return date(year, month, day)


def _replace_historical_payment_date_year(value: str | None, year: int) -> str | None:
    text = _historical_text_date_base(value)
    match = STRICT_HISTORICAL_DATE_RE.match(text)
    if not match:
        return None
    month = STRICT_HISTORICAL_DATE_MONTHS.get(match.group("month").upper())
    if not month:
        return None
    day = int(match.group("day"))
    if not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None
    suffix = "F" if match.group("fiduciary") else ""
    return f"{STRICT_HISTORICAL_MONTH_LABELS[month]}.{day}/{year % 100:02d}{suffix}"


def _historical_payment_date_year_month(value: str | None) -> tuple[int, int] | None:
    text = clean_text(value)
    if not text:
        return None
    text = text.strip().strip("()").strip()
    text = re.sub(r"(TRASLADO|TRASL|CESION)$", "", text, flags=re.IGNORECASE).strip()
    text = text.rstrip("Ff").strip()
    if " " in text:
        text = text.split(" ", 1)[0]
    iso_match = re.match(r"^(?P<year>\d{4})-(?P<month>\d{2})-\d{2}$", text)
    if iso_match:
        return int(iso_match.group("year")), int(iso_match.group("month"))
    slash_match = re.match(r"^\d{1,2}/(?P<month>\d{1,2})/(?P<year>\d{2,4})$", text)
    if slash_match:
        year = int(slash_match.group("year"))
        if year < 100:
            year += 2000
        return year, int(slash_match.group("month"))
    if _looks_like_historical_text_date(value) and not _is_valid_strict_historical_date(value):
        return None
    month_pattern = "|".join(STRICT_HISTORICAL_DATE_MONTHS)
    text_match = re.match(rf"^(?P<month>{month_pattern})\.(?P<day>\d{{1,2}})/(?P<year>\d{{2}})$", text.upper())
    if text_match:
        month = STRICT_HISTORICAL_DATE_MONTHS.get(text_match.group("month"))
        if not month:
            return None
        year = int(text_match.group("year"))
        return year + 2000, month
    return None


def _month_year_from_text(value: str) -> tuple[int | None, int | None]:
    normalized = normalize_text(value).upper()
    compact = compact_normalized(value).upper()
    year_match = re.search(r"(20\d{2}|19\d{2})", compact)
    if not year_match:
        return None, None
    for month_text, month in MONTHS.items():
        if re.search(rf"\b{re.escape(month_text)}\b", normalized) or month_text in compact:
            return month, int(year_match.group(1))
    return None, None
