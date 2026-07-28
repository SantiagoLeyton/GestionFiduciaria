import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .data import CellData, ParserIssue
from .normalize import excel_column_index, excel_column_name


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"main": MAIN_NS, "rel": REL_NS, "pkgrel": PKG_REL_NS}


@dataclass(frozen=True)
class RawSheet:
    name: str
    index: int
    visibility: str
    dimension: str | None
    cells: dict[tuple[int, int], CellData]
    hidden_columns: set[int]
    hidden_rows: set[int]

    @property
    def used_rows(self) -> int:
        if not self.cells:
            return 0
        return max(row for row, _ in self.cells)

    @property
    def used_columns(self) -> int:
        if not self.cells:
            return 0
        return max(column for _, column in self.cells)

    def cell(self, row: int, column: int) -> CellData | None:
        return self.cells.get((row, column))


@dataclass(frozen=True)
class RawWorkbook:
    file_type: str
    sheets: list[RawSheet]
    issues: list[ParserIssue]


class WorkbookReader:
    def read(self, path: Path) -> RawWorkbook:
        extension = path.suffix.lower()
        if extension == ".xlsx":
            return XlsxWorkbookReader().read(path)
        if extension == ".xls":
            return XlsWorkbookReader().read(path)
        return RawWorkbook(
            file_type=extension.lstrip("."),
            sheets=[],
            issues=[
                ParserIssue(
                    code="UNSUPPORTED_FILE_TYPE",
                    severity="blocking",
                    message="El tipo de archivo no esta soportado para libro historico.",
                )
            ],
        )


class XlsxWorkbookReader:
    def read(self, path: Path) -> RawWorkbook:
        issues: list[ParserIssue] = []
        with zipfile.ZipFile(path) as archive:
            shared_strings = self._read_shared_strings(archive)
            date_style_ids = self._read_date_style_ids(archive)
            sheet_refs = self._read_sheet_refs(archive)
            sheets = [
                self._read_sheet(archive, sheet_name, index, visibility, sheet_path, shared_strings, date_style_ids)
                for index, (sheet_name, visibility, sheet_path) in enumerate(sheet_refs, start=1)
            ]
        return RawWorkbook(file_type="xlsx", sheets=sheets, issues=issues)

    def _read_shared_strings(self, archive: zipfile.ZipFile) -> list[str]:
        try:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        except KeyError:
            return []
        values = []
        for item in root.findall("main:si", NS):
            values.append("".join(text.text or "" for text in item.findall(".//main:t", NS)))
        return values

    def _read_date_style_ids(self, archive: zipfile.ZipFile) -> set[int]:
        try:
            root = ET.fromstring(archive.read("xl/styles.xml"))
        except KeyError:
            return set()
        custom_formats = {}
        for item in root.findall("main:numFmts/main:numFmt", NS):
            custom_formats[int(item.attrib["numFmtId"])] = item.attrib.get("formatCode", "")
        builtin_date_ids = set(range(14, 23)) | {45, 46, 47}
        date_style_ids = set()
        cell_formats = root.find("main:cellXfs", NS)
        if cell_formats is None:
            return date_style_ids
        for index, cell_format in enumerate(cell_formats.findall("main:xf", NS)):
            format_id = int(cell_format.attrib.get("numFmtId", "0"))
            format_code = custom_formats.get(format_id, "")
            if format_id in builtin_date_ids or re.search(r"(?<!\\)[dmyhs]", format_code, re.IGNORECASE):
                date_style_ids.add(index)
        return date_style_ids

    def _read_sheet_refs(self, archive: zipfile.ZipFile) -> list[tuple[str, str, str]]:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        target_by_id = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall("pkgrel:Relationship", NS)}
        refs = []
        for sheet in workbook.findall("main:sheets/main:sheet", NS):
            relationship_id = sheet.attrib[f"{{{REL_NS}}}id"]
            target = target_by_id[relationship_id]
            if not target.startswith("xl/"):
                target = "xl/" + target
            refs.append((sheet.attrib["name"], sheet.attrib.get("state", "visible"), target))
        return refs

    def _read_sheet(
        self,
        archive: zipfile.ZipFile,
        sheet_name: str,
        index: int,
        visibility: str,
        sheet_path: str,
        shared_strings: list[str],
        date_style_ids: set[int],
    ) -> RawSheet:
        root = ET.fromstring(archive.read(sheet_path))
        dimension_node = root.find("main:dimension", NS)
        dimension = dimension_node.attrib.get("ref") if dimension_node is not None else None
        hidden_columns = self._hidden_columns(root)
        hidden_rows = self._hidden_rows(root)
        cells = {}
        for cell_node in root.findall(".//main:c", NS):
            reference = cell_node.attrib.get("r", "")
            match = re.match(r"([A-Z]+)(\d+)", reference)
            if not match:
                continue
            column_letter, row_text = match.groups()
            row = int(row_text)
            column = excel_column_index(column_letter)
            formula_node = cell_node.find("main:f", NS)
            value_node = cell_node.find("main:v", NS)
            formula = formula_node.text if formula_node is not None else None
            style_id = int(cell_node.attrib.get("s", "0")) if cell_node.attrib.get("s", "0").isdigit() else 0
            is_date = style_id in date_style_ids
            value = self._cell_value(cell_node, value_node, shared_strings, is_date)
            cells[(row, column)] = CellData(
                row=row,
                column=column,
                letter=excel_column_name(column),
                coordinate=reference,
                value=value,
                formula=formula,
                has_cached_value=value_node is not None and value_node.text not in (None, ""),
                is_date=is_date,
            )
        return RawSheet(
            name=sheet_name,
            index=index,
            visibility=visibility,
            dimension=dimension,
            cells=cells,
            hidden_columns=hidden_columns,
            hidden_rows=hidden_rows,
        )

    def _hidden_columns(self, root: ET.Element) -> set[int]:
        hidden = set()
        for column in root.findall("main:cols/main:col", NS):
            if column.attrib.get("hidden") != "1":
                continue
            start = int(float(column.attrib["min"]))
            end = int(float(column.attrib["max"]))
            hidden.update(range(start, end + 1))
        return hidden

    def _hidden_rows(self, root: ET.Element) -> set[int]:
        hidden = set()
        for row in root.findall("main:sheetData/main:row", NS):
            if row.attrib.get("hidden") == "1" and row.attrib.get("r"):
                hidden.add(int(row.attrib["r"]))
        return hidden

    def _cell_value(
        self,
        cell_node: ET.Element,
        value_node: ET.Element | None,
        shared_strings: list[str],
        is_date: bool = False,
    ) -> Any:
        cell_type = cell_node.attrib.get("t")
        if cell_type == "inlineStr":
            return "".join(text.text or "" for text in cell_node.findall(".//main:t", NS))
        if value_node is None:
            return None
        raw_value = value_node.text
        if cell_type == "s":
            try:
                return shared_strings[int(raw_value)]
            except (ValueError, IndexError, TypeError):
                return raw_value
        if cell_type == "b":
            return raw_value == "1"
        if cell_type == "str":
            return raw_value
        if is_date and raw_value not in (None, ""):
            return self._excel_serial_date(float(raw_value))
        try:
            if raw_value is not None and "." not in raw_value:
                return int(raw_value)
            return float(raw_value) if raw_value is not None else None
        except ValueError:
            return raw_value

    def _excel_serial_date(self, serial: float):
        # Excel incorrectly treats 1900 as leap year; this matches the common workbook date system.
        base = datetime(1899, 12, 30)
        return base + timedelta(days=serial)


class XlsWorkbookReader:
    def read(self, path: Path) -> RawWorkbook:
        try:
            import xlrd
        except ImportError:
            return RawWorkbook(
                file_type="xls",
                sheets=[],
                issues=[
                    ParserIssue(
                        code="XLS_READER_UNAVAILABLE",
                        severity="blocking",
                        message="No hay lector XLS disponible en el entorno.",
                    )
                ],
            )

        workbook = xlrd.open_workbook(path, formatting_info=False)
        sheets = []
        for index, sheet in enumerate(workbook.sheets(), start=1):
            cells = {}
            for row_index in range(sheet.nrows):
                for column_index in range(sheet.ncols):
                    value = sheet.cell_value(row_index, column_index)
                    if value in ("", None):
                        continue
                    row = row_index + 1
                    column = column_index + 1
                    letter = excel_column_name(column)
                    cells[(row, column)] = CellData(
                        row=row,
                        column=column,
                        letter=letter,
                        coordinate=f"{letter}{row}",
                        value=value,
                    )
            sheets.append(
                RawSheet(
                    name=sheet.name,
                    index=index,
                    visibility="visible",
                    dimension=f"A1:{excel_column_name(sheet.ncols)}{sheet.nrows}",
                    cells=cells,
                    hidden_columns=set(),
                    hidden_rows=set(),
                )
            )
        return RawWorkbook(file_type="xls", sheets=sheets, issues=[])
