import re
from collections import Counter
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
    SheetData,
    WorkbookData,
)
from .normalize import MONTHS, clean_text, compact_normalized, normalize_text, parse_decimal, parse_document_type
from .readers import RawSheet, WorkbookReader


PAYMENT_HEADER_PATTERN = re.compile(r"^recibofiducia([a-z]{3,4})(\d{4})$")
IGNORED_HEADER_KEYS = {
    "vincxmes",
    "area",
    "prom",
    "#",
    "valorinmueble",
    "recibos",
    "recibosfidubogota",
    "fecha",
    "recibido",
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
    "telefono",
    "email",
    "e mail",
    "contacto",
}


class HistoricalWorkbookParser:
    def __init__(self, path, *, grouping_type_hint: str | None = None):
        self.path = Path(path)
        self.reader = WorkbookReader()
        self.grouping_type_hint = grouping_type_hint

    def parse(self) -> WorkbookData:
        raw_workbook = self.reader.read(self.path)
        sheets = [self._parse_sheet(sheet) for sheet in raw_workbook.sheets]
        issues = list(raw_workbook.issues)
        for sheet in sheets:
            issues.extend(sheet.issues)
        return WorkbookData(
            path=self.path,
            file_type=raw_workbook.file_type,
            sheets=sheets,
            issues=issues,
            statistics=self._build_statistics(sheets, issues),
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

    def _find_header_row(self, sheet: RawSheet) -> int | None:
        best_row = None
        best_score = 0
        for row in range(1, min(sheet.used_rows, 30) + 1):
            normalized_headers = [
                normalize_text(cell.value)
                for (cell_row, _), cell in sheet.cells.items()
                if cell_row == row and normalize_text(cell.value)
            ]
            score = self._header_score(normalized_headers)
            if score > best_score:
                best_row = row
                best_score = score
        return best_row if best_score >= 4 else None

    def _header_score(self, headers: list[str]) -> int:
        compact_headers = {header.replace(" ", "") for header in headers}
        score = 0
        if "encargofiduciario" in compact_headers:
            score += 2
        if any(header in compact_headers for header in {"apto", "local", "bodega", "unidad"}):
            score += 2
        if "cedulacliente" in compact_headers or "identificacion" in compact_headers:
            score += 1
        if "nombrecliente" in compact_headers:
            score += 1
        if any(header.startswith("recibofiducia") for header in compact_headers):
            score += 1
        return score

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

        for column in range(1, sheet.used_columns + 1):
            cell = sheet.cell(header_row, column)
            header = clean_text(cell.value if cell else None)
            if not header:
                continue
            normalized = normalize_text(header)
            compact = compact_normalized(header)
            letter = cell.letter if cell else ""
            payment_column = self._detect_payment_column(header, column, letter)
            if payment_column:
                payment_columns.append(payment_column)
                known_header_count += 1
                continue
            key = self._functional_key(compact)
            if key == "client_name":
                client_column = DetectedColumn(
                    key=f"client_name_{len(client_name_columns) + 1}",
                    header=header,
                    normalized_header=normalized,
                    index=column,
                    letter=letter,
                )
                client_name_columns.append(client_column)
                known_header_count += 1
            elif key:
                columns[key] = DetectedColumn(
                    key=key,
                    header=header,
                    normalized_header=normalized,
                    index=column,
                    letter=letter,
                )
                known_header_count += 1
            elif compact and compact not in IGNORED_HEADER_KEYS:
                issues.append(
                    ParserIssue(
                        code="UNKNOWN_HEADER",
                        severity="info",
                        message="Encabezado no requerido por el analizador historico.",
                        sheet_name=sheet.name,
                        row_number=header_row,
                        column_letter=letter,
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

    def _detect_payment_column(
        self,
        header: str,
        column: int,
        letter: str,
    ) -> DetectedPaymentColumn | None:
        normalized = normalize_text(header)
        match = PAYMENT_HEADER_PATTERN.match(compact_normalized(header))
        if not match:
            return None
        month_text, year_text = match.groups()
        month = MONTHS.get(month_text.upper())
        if not month:
            return None
        return DetectedPaymentColumn(
            key=f"payment_{year_text}_{month:02}",
            header=header,
            normalized_header=normalized,
            index=column,
            letter=letter,
            month=month,
            year=int(year_text),
        )

    def _functional_key(self, compact_header: str) -> str | None:
        if compact_header == "vendedor":
            return "seller"
        if compact_header == "encargofiduciario":
            return "assignment_number"
        if compact_header in {"apto", "apartamento", "local", "bodega", "unidad"}:
            return "unit"
        if compact_header == "vinc":
            return "assignment_status"
        if compact_header in {"cedulacliente", "documentocliente", "identificacioncliente", "identificacion"}:
            return "document_number"
        if compact_header == "nombrecliente":
            return "client_name"
        if compact_header in {"observaciones", "observacion"}:
            return "observations"
        if compact_header == "cesionestraslados":
            return "assignment_changes"
        return None

    def _required_column_issues(
        self,
        sheet_name: str,
        columns: dict[str, DetectedColumn],
        payment_columns: list[DetectedPaymentColumn],
    ) -> list[ParserIssue]:
        issues = []
        for key, label in {
            "assignment_number": "encargo fiduciario",
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
        if not payment_columns:
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
        return all(key in columns for key in ["assignment_number", "unit", "client_names"]) and bool(payment_columns)

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
        formula_cached_columns = set()
        project, grouping_code, grouping_name = self._extract_sheet_structure(sheet)
        in_novelty_section = False
        novelty_rows = 0
        for row_number in range(header_row + 1, sheet.used_rows + 1):
            if self._is_row_empty(sheet, row_number):
                ignored_rows += 1
                ignored_reasons["empty"] += 1
                continue
            if self._is_novelty_header_row(sheet, row_number):
                in_novelty_section = True
                ignored_rows += 1
                ignored_reasons["novelty_section"] += 1
                continue
            if in_novelty_section:
                novelty = self._extract_novelty(
                    sheet,
                    row_number,
                    project,
                    grouping_code,
                    grouping_name,
                    columns,
                    header_row,
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
            if row.assignment is None or not row.unit_code:
                issues.append(
                    ParserIssue(
                        code="INVALID_HISTORICAL_ROW",
                        severity="warning",
                        message="Fila con informacion parcial sin unidad o encargo suficiente.",
                        sheet_name=sheet.name,
                        row_number=row_number,
                    )
                )
                ignored_rows += 1
                ignored_reasons["invalid"] += 1
                continue
            rows.append(row)
            formula_issues, cached_columns = self._formula_issues_for_row(sheet, row_number, payment_columns)
            issues.extend(formula_issues)
            formula_cached_columns.update(cached_columns)
        for column_letter in sorted(formula_cached_columns):
            issues.append(
                ParserIssue(
                    code="FORMULA_WITH_CACHED_VALUE",
                    severity="warning",
                    message="Columna mensual relevante contiene formulas con valor calculado disponible.",
                    sheet_name=sheet.name,
                    column_letter=column_letter,
                )
            )
        return rows, novelties, ignored_rows, dict(ignored_reasons), issues

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
        assignment_number = self._value(sheet, row_number, columns.get("assignment_number"))
        document_number = self._value(sheet, row_number, columns.get("document_number"))
        clients = self._extract_clients(sheet, row_number, columns, document_number)
        payments = self._extract_payments(sheet, row_number, payment_columns)

        if not any([unit, assignment_number, document_number, clients, payments]):
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
            assignment=HistoricalAssignment(
                assignment_number=assignment_number,
                status=self._value(sheet, row_number, columns.get("assignment_status")),
            )
            if assignment_number
            else None,
            clients=clients,
            payments=payments,
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
        for index, column in enumerate(name_columns, start=1):
            name = self._value(sheet, row_number, column)
            if not name:
                continue
            clients.append(
                HistoricalClient(
                    order=index,
                    name=name,
                    document_number=document_number if index == 1 else None,
                    document_type=parse_document_type(columns.get("document_number").header if columns.get("document_number") else None),
                    is_primary=index == 1,
                )
            )
        return clients

    def _extract_payments(
        self,
        sheet: RawSheet,
        row_number: int,
        payment_columns: list[DetectedPaymentColumn],
    ) -> list[HistoricalMonthlyPayment]:
        payments = []
        for column in payment_columns:
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
                    has_formula=cell.has_formula if cell else False,
                    has_cached_value=cell.has_cached_value if cell else False,
                )
            )
        return payments

    def _extract_novelty(
        self,
        sheet: RawSheet,
        row_number: int,
        project: str,
        grouping_code: str,
        grouping_name: str,
        columns: dict[str, DetectedColumn],
        header_row: int,
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
        assignment_number = self._value(sheet, row_number, columns.get("assignment_number"))
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
            )
            if assignment_number
            else None,
            cells=cells,
        )

    def _formula_issues_for_row(
        self,
        sheet: RawSheet,
        row_number: int,
        payment_columns: list[DetectedPaymentColumn],
    ) -> tuple[list[ParserIssue], set[str]]:
        issues = []
        cached_columns = set()
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
                    )
                )
            elif cell and cell.has_formula and parse_decimal(cell.value) is not None:
                cached_columns.add(column.letter)
        return issues, cached_columns

    def _value(self, sheet: RawSheet, row_number: int, column: DetectedColumn | None) -> str | None:
        if column is None:
            return None
        cell = sheet.cell(row_number, column.index)
        return clean_text(cell.value if cell else None)

    def _is_row_empty(self, sheet: RawSheet, row_number: int) -> bool:
        return not any(cell.value not in ("", None) for (row, _), cell in sheet.cells.items() if row == row_number)

    def _is_decorative_or_total_row(self, sheet: RawSheet, row_number: int) -> bool:
        values = [
            normalize_text(cell.value)
            for (row, _), cell in sheet.cells.items()
            if row == row_number and normalize_text(cell.value)
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
            for (row, _), cell in sheet.cells.items()
            if row == row_number and normalize_text(cell.value)
        ]
        return len(values) <= 3 and any(value == "novedades" for value in values)

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
