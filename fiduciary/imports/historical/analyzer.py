import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.db import IntegrityError, transaction

from fiduciary.models import (
    DetectedStructureElement,
    ImportBatch,
    ImportedHistoricalNovelty,
    ImportedFile,
    ImportedSheetResult,
    ImportResolution,
    ImportRowIssue,
)
from fiduciary.utils import calculate_sha256
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup

from .data import HistoricalRow, WorkbookData
from .normalize import clean_text, normalize_text
from .parser import HistoricalWorkbookParser


@dataclass(frozen=True)
class MatchCandidate:
    model_name: str
    object_id: int
    label: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class StructurePreviewItem:
    kind: str
    raw_value: str
    normalized_value: str
    status: str
    occurrence_count: int
    candidates: list[MatchCandidate] = field(default_factory=list)
    detected_element_id: int | None = None
    resolution_id: int | None = None


@dataclass(frozen=True)
class HistoricalImportPreview:
    batch_id: int
    imported_file_id: int
    workbook: WorkbookData
    project: StructurePreviewItem | None
    grouping_type: StructurePreviewItem | None
    structural_groups: list[StructurePreviewItem]
    property_units: list[StructurePreviewItem]
    parser_issue_count: int
    pending_resolution_count: int
    automatic_match_count: int
    row_count: int
    client_appearance_count: int
    assignment_count: int
    payment_entry_count: int
    historical_novelty_count: int


@dataclass(frozen=True)
class HistoricalImportAnalysisResult:
    preview: HistoricalImportPreview
    imported_file: ImportedFile


class DuplicateHistoricalImportError(Exception):
    def __init__(self, imported_file: ImportedFile):
        self.imported_file = imported_file
        super().__init__("Este archivo historico ya fue cargado anteriormente.")


@dataclass(frozen=True)
class ExistingStructureIndex:
    projects: list[Project]
    grouping_types: list[GroupingType]
    structural_groups: list[StructuralGroup]
    property_units: list[PropertyUnit]


def analyze_historical_import(
    *,
    batch: ImportBatch,
    file_path,
    grouping_type_hint: str | None = None,
) -> HistoricalImportAnalysisResult:
    path = Path(file_path)
    imported_file = reserve_historical_import_file(batch=batch, file_path=path)
    workbook = HistoricalWorkbookParser(path, grouping_type_hint=grouping_type_hint).parse()
    with transaction.atomic():
        _update_imported_file_from_workbook(imported_file, workbook)
        _persist_sheet_results(imported_file, workbook)
        _persist_parser_issues(imported_file, workbook)
        _persist_historical_novelties(batch, imported_file, workbook)
        index = _build_existing_structure_index()
        project_preview = _analyze_project(batch, imported_file, workbook, index)
        grouping_type_preview = _analyze_grouping_type(batch, imported_file, workbook, index, grouping_type_hint)
        group_previews = _analyze_structural_groups(batch, imported_file, workbook, index, project_preview, grouping_type_preview)
        unit_previews = _analyze_property_units(batch, imported_file, workbook, index, project_preview, group_previews)
        _update_batch_and_file_counts(batch, imported_file, workbook)

    preview_items = [item for item in [project_preview, grouping_type_preview] if item] + group_previews + unit_previews
    preview = HistoricalImportPreview(
        batch_id=batch.pk,
        imported_file_id=imported_file.pk,
        workbook=workbook,
        project=project_preview,
        grouping_type=grouping_type_preview,
        structural_groups=group_previews,
        property_units=unit_previews,
        parser_issue_count=len(workbook.issues),
        pending_resolution_count=sum(1 for item in preview_items if item.status == "needs_review"),
        automatic_match_count=sum(1 for item in preview_items if item.status == "auto_matched"),
        row_count=workbook.statistics.valid_rows,
        client_appearance_count=workbook.statistics.client_appearances_found,
        assignment_count=workbook.statistics.distinct_assignments_found,
        payment_entry_count=workbook.statistics.payment_entries_found,
        historical_novelty_count=workbook.statistics.historical_novelties_found,
    )
    return HistoricalImportAnalysisResult(preview=preview, imported_file=imported_file)


def find_existing_historical_import(file_path) -> ImportedFile | None:
    sha256 = calculate_sha256(file_path)
    return _historical_files().filter(sha256=sha256).first()


def reserve_historical_import_file(*, batch: ImportBatch, file_path) -> ImportedFile:
    path = Path(file_path)
    sha256 = calculate_sha256(path)
    existing_file = _historical_files().filter(sha256=sha256).first()
    if existing_file:
        raise DuplicateHistoricalImportError(existing_file)

    try:
        with transaction.atomic():
            return ImportedFile.objects.create(
                batch=batch,
                original_name=path.name,
                extension=path.suffix.lower(),
                size_bytes=path.stat().st_size,
                sha256=sha256,
                file_type=ImportedFile.FileType.HISTORICAL,
                status=ImportedFile.Status.ANALYZING,
                order=1,
                result_message="Analisis historico en curso.",
            )
    except IntegrityError as exc:
        existing_file = _historical_files().filter(sha256=sha256).first()
        if existing_file:
            raise DuplicateHistoricalImportError(existing_file) from exc
        raise


def _historical_files():
    return (
        ImportedFile.objects.filter(file_type=ImportedFile.FileType.HISTORICAL)
        .select_related("batch", "batch__initiated_by")
        .order_by("created_at", "pk")
    )


def _update_imported_file_from_workbook(imported_file: ImportedFile, workbook: WorkbookData) -> None:
    summary = _sanitized_summary(workbook)
    imported_file.status = ImportedFile.Status.READY if not _has_blocking_issues(workbook) else ImportedFile.Status.FAILED
    imported_file.total_rows = workbook.statistics.valid_rows + workbook.statistics.ignored_rows
    imported_file.processed_rows = workbook.statistics.valid_rows
    imported_file.skipped_rows = workbook.statistics.ignored_rows
    imported_file.error_count = sum(1 for issue in workbook.issues if issue.severity in {"error", "blocking"})
    imported_file.warning_count = sum(1 for issue in workbook.issues if issue.severity == "warning")
    imported_file.result_message = json.dumps(summary, ensure_ascii=True)
    imported_file.save(
        update_fields=[
            "status",
            "total_rows",
            "processed_rows",
            "skipped_rows",
            "error_count",
            "warning_count",
            "result_message",
        ]
    )


def _persist_sheet_results(imported_file: ImportedFile, workbook: WorkbookData) -> None:
    for sheet in workbook.sheets:
        ImportedSheetResult.objects.update_or_create(
            imported_file=imported_file,
            sheet_name=sheet.name,
            defaults={
                "sheet_index": sheet.index,
                "visibility": _sheet_visibility(sheet.visibility),
                "classification": _sheet_classification(sheet.classification),
                "header_row": sheet.header_row,
                "detected_dimension": f"{sheet.used_rows}x{sheet.used_columns}" if sheet.used_rows else "",
                "analyzed_rows": sheet.used_rows,
                "processed_rows": len(sheet.rows),
                "skipped_rows": sheet.ignored_rows,
                "error_count": sum(1 for issue in sheet.issues if issue.severity in {"error", "blocking"}),
                "warning_count": sum(1 for issue in sheet.issues if issue.severity == "warning"),
                "status": ImportedSheetResult.Status.ANALYZED,
                "summary": "Hoja historica analizada sin persistencia definitiva.",
            },
        )


def _persist_parser_issues(imported_file: ImportedFile, workbook: WorkbookData) -> None:
    sheet_results = {sheet.sheet_name: sheet for sheet in imported_file.sheet_results.all()}
    for issue in workbook.issues:
        ImportRowIssue.objects.get_or_create(
            imported_file=imported_file,
            sheet_result=sheet_results.get(issue.sheet_name),
            row_number=issue.row_number,
            column_letter=issue.column_letter or "",
            severity=_issue_severity(issue.severity),
            code=issue.code,
            defaults={
                "message": issue.message,
                "status": ImportRowIssue.Status.OPEN,
            },
        )


def _persist_historical_novelties(batch: ImportBatch, imported_file: ImportedFile, workbook: WorkbookData) -> None:
    sheet_results = {sheet.sheet_name: sheet for sheet in imported_file.sheet_results.all()}
    for sheet in workbook.sheets:
        sheet_result = sheet_results.get(sheet.name)
        if not sheet_result:
            continue
        for novelty in sheet.novelties:
            ImportedHistoricalNovelty.objects.update_or_create(
                imported_file=imported_file,
                sheet_result=sheet_result,
                row_number=novelty.row_number,
                defaults={
                    "batch": batch,
                    "project_name": novelty.project,
                    "grouping_type_name": novelty.grouping_type or "",
                    "grouping_code": novelty.grouping_code,
                    "grouping_name": novelty.grouping_name,
                    "unit_code": novelty.unit_code or "",
                    "unit_name": novelty.unit_name or "",
                    "assignment_number": novelty.assignment.assignment_number if novelty.assignment else "",
                    "assignment_status": novelty.assignment.status if novelty.assignment and novelty.assignment.status else "",
                    "original_cells": [_novelty_cell_payload(cell) for cell in novelty.cells],
                    "sanitized_summary": _novelty_summary(novelty),
                    "status": ImportedHistoricalNovelty.Status.DETECTED,
                },
            )


def _novelty_cell_payload(cell) -> dict[str, Any]:
    return {
        "coordinate": cell.coordinate,
        "column_letter": cell.column_letter,
        "column_index": cell.column_index,
        "header": cell.header or "",
        "value": _json_safe_value(cell.value),
        "formula": cell.formula or "",
        "has_formula": bool(cell.formula),
        "has_cached_value": cell.has_cached_value,
        "is_date": cell.is_date,
    }


def _json_safe_value(value):
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _novelty_summary(novelty) -> str:
    unit = novelty.unit_code or "sin unidad"
    assignment = novelty.assignment.assignment_number if novelty.assignment else "sin encargo"
    return f"Novedad historica detectada en hoja {novelty.sheet_name}, fila {novelty.row_number}, unidad {unit}, encargo {assignment}."


def _build_existing_structure_index() -> ExistingStructureIndex:
    return ExistingStructureIndex(
        projects=list(Project.objects.all()),
        grouping_types=list(GroupingType.objects.all()),
        structural_groups=list(StructuralGroup.objects.select_related("project", "grouping_type", "parent")),
        property_units=list(PropertyUnit.objects.select_related("project", "structural_group")),
    )


def _analyze_project(
    batch: ImportBatch,
    imported_file: ImportedFile,
    workbook: WorkbookData,
    index: ExistingStructureIndex,
) -> StructurePreviewItem | None:
    values = _count_values(row.project for row in _rows(workbook))
    if not values:
        return None
    raw_value = values.most_common(1)[0][0]
    return _persist_detected_item(
        batch=batch,
        imported_file=imported_file,
        kind=DetectedStructureElement.InferredKind.PROJECT,
        raw_value=raw_value,
        occurrence_count=sum(values.values()),
        candidates=_match_by_code_or_name(raw_value, index.projects),
    )


def _analyze_grouping_type(
    batch: ImportBatch,
    imported_file: ImportedFile,
    workbook: WorkbookData,
    index: ExistingStructureIndex,
    grouping_type_hint: str | None,
) -> StructurePreviewItem | None:
    value = clean_text(grouping_type_hint) or _single_non_empty(row.grouping_type for row in _rows(workbook))
    if not value:
        return _persist_detected_item(
            batch=batch,
            imported_file=imported_file,
            kind=DetectedStructureElement.InferredKind.GROUPING_TYPE,
            raw_value="",
            occurrence_count=0,
            candidates=[],
            status_override="needs_review",
        )
    return _persist_detected_item(
        batch=batch,
        imported_file=imported_file,
        kind=DetectedStructureElement.InferredKind.GROUPING_TYPE,
        raw_value=value,
        occurrence_count=len(_rows(workbook)),
        candidates=_match_by_code_or_name(value, index.grouping_types),
    )


def _analyze_structural_groups(
    batch: ImportBatch,
    imported_file: ImportedFile,
    workbook: WorkbookData,
    index: ExistingStructureIndex,
    project_preview: StructurePreviewItem | None,
    grouping_type_preview: StructurePreviewItem | None,
) -> list[StructurePreviewItem]:
    project_id = _single_candidate_id(project_preview)
    grouping_type_id = _single_candidate_id(grouping_type_preview)
    groups = []
    for raw_value, count in _count_values(row.grouping_name for row in _rows(workbook)).items():
        candidates = []
        if project_id and grouping_type_id:
            candidates = _match_structural_group(raw_value, project_id, grouping_type_id, index.structural_groups)
        groups.append(
            _persist_detected_item(
                batch=batch,
                imported_file=imported_file,
                kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
                raw_value=raw_value,
                occurrence_count=count,
                candidates=candidates,
                context={"project_id": project_id, "grouping_type_id": grouping_type_id},
            )
        )
    return groups


def _analyze_property_units(
    batch: ImportBatch,
    imported_file: ImportedFile,
    workbook: WorkbookData,
    index: ExistingStructureIndex,
    project_preview: StructurePreviewItem | None,
    group_previews: list[StructurePreviewItem],
) -> list[StructurePreviewItem]:
    project_id = _single_candidate_id(project_preview)
    group_by_name = {item.raw_value: item for item in group_previews}
    unit_counts: dict[tuple[str, str], int] = {}
    for row in _rows(workbook):
        if not row.unit_code:
            continue
        key = (row.grouping_name, row.unit_code)
        unit_counts[key] = unit_counts.get(key, 0) + 1

    previews = []
    for (group_name, unit_code), count in unit_counts.items():
        group_preview = group_by_name.get(group_name)
        group_id = _single_candidate_id(group_preview)
        candidates = []
        status_override = None
        if project_id:
            if group_preview and group_preview.status == "auto_matched":
                candidates = _match_property_unit(unit_code, project_id, group_id, index.property_units)
            else:
                status_override = "blocked"
        else:
            status_override = "blocked"
        previews.append(
            _persist_detected_item(
                batch=batch,
                imported_file=imported_file,
                kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
                raw_value=unit_code,
                occurrence_count=count,
                candidates=candidates,
                context={"project_id": project_id, "structural_group_id": group_id, "grouping_name": group_name},
                status_override=status_override,
            )
        )
    return previews


def _persist_detected_item(
    *,
    batch: ImportBatch,
    imported_file: ImportedFile,
    kind: str,
    raw_value: str,
    occurrence_count: int,
    candidates: list[MatchCandidate],
    context: dict[str, Any] | None = None,
    status_override: str | None = None,
) -> StructurePreviewItem:
    normalized_value = normalize_text(raw_value)
    status = status_override or ("auto_matched" if len(candidates) == 1 and candidates[0].confidence >= 1 else "needs_review")
    model_status = {
        "auto_matched": DetectedStructureElement.Status.AUTO_MATCHED,
        "blocked": DetectedStructureElement.Status.DETECTED,
    }.get(status, DetectedStructureElement.Status.NEEDS_REVIEW)
    detected = DetectedStructureElement.objects.create(
        batch=batch,
        imported_file=imported_file,
        raw_value=raw_value or "(sin valor)",
        normalized_value=normalized_value,
        inferred_kind=kind,
        structural_context=context or {},
        occurrence_count=occurrence_count,
        confidence=candidates[0].confidence if len(candidates) == 1 else None,
        status=model_status,
    )
    resolution = _create_resolution(detected, kind, candidates, status)
    return StructurePreviewItem(
        kind=kind,
        raw_value=raw_value,
        normalized_value=normalized_value,
        status=status,
        occurrence_count=occurrence_count,
        candidates=candidates,
        detected_element_id=detected.pk,
        resolution_id=resolution.pk,
    )


def _create_resolution(
    detected: DetectedStructureElement,
    kind: str,
    candidates: list[MatchCandidate],
    status: str,
) -> ImportResolution:
    defaults = {
        "target_kind": kind,
        "action": ImportResolution.Action.UNRESOLVED,
        "status": ImportResolution.Status.DRAFT,
    }
    if status == "auto_matched" and len(candidates) == 1:
        defaults["action"] = ImportResolution.Action.ASSOCIATE_EXISTING
        candidate = candidates[0]
        if kind == DetectedStructureElement.InferredKind.PROJECT:
            defaults["target_project_id"] = candidate.object_id
        elif kind == DetectedStructureElement.InferredKind.GROUPING_TYPE:
            defaults["target_grouping_type_id"] = candidate.object_id
        elif kind == DetectedStructureElement.InferredKind.STRUCTURAL_GROUP:
            defaults["target_structural_group_id"] = candidate.object_id
        elif kind == DetectedStructureElement.InferredKind.PROPERTY_UNIT:
            defaults["target_property_unit_id"] = candidate.object_id
    return ImportResolution.objects.create(detected_element=detected, **defaults)


def _match_by_code_or_name(raw_value: str, objects) -> list[MatchCandidate]:
    normalized = normalize_text(raw_value)
    exact_code = [obj for obj in objects if normalize_text(obj.code) == normalized]
    if len(exact_code) == 1:
        return [_candidate(exact_code[0], "codigo_normalizado")]
    exact_name = [obj for obj in objects if normalize_text(obj.name) == normalized]
    if len(exact_name) == 1:
        return [_candidate(exact_name[0], "nombre_normalizado")]
    matches = exact_code + [obj for obj in exact_name if obj not in exact_code]
    return [_candidate(obj, "coincidencia_multiple") for obj in matches]


def _match_structural_group(
    raw_value: str,
    project_id: int,
    grouping_type_id: int,
    groups: list[StructuralGroup],
) -> list[MatchCandidate]:
    normalized = normalize_text(raw_value)
    scoped = [item for item in groups if item.project_id == project_id and item.grouping_type_id == grouping_type_id]
    code_matches = [item for item in scoped if normalize_text(item.code) == normalized]
    name_matches = [item for item in scoped if normalize_text(item.name) == normalized or normalize_text(str(item)) == normalized]
    matches = code_matches + [item for item in name_matches if item not in code_matches]
    return [_candidate(item, "agrupacion_en_contexto") for item in matches]


def _match_property_unit(
    raw_value: str,
    project_id: int,
    group_id: int | None,
    units: list[PropertyUnit],
) -> list[MatchCandidate]:
    normalized = normalize_text(raw_value)
    scoped = [item for item in units if item.project_id == project_id and item.structural_group_id == group_id]
    code_matches = [item for item in scoped if normalize_text(item.code) == normalized]
    name_matches = [item for item in scoped if normalize_text(item.name) == normalized]
    matches = code_matches + [item for item in name_matches if item not in code_matches]
    return [_candidate(item, "unidad_en_contexto") for item in matches]


def _candidate(obj, reason: str) -> MatchCandidate:
    return MatchCandidate(
        model_name=obj.__class__.__name__,
        object_id=obj.pk,
        label=str(obj),
        confidence=1.0,
        reason=reason,
    )


def _update_batch_and_file_counts(batch: ImportBatch, imported_file: ImportedFile, workbook: WorkbookData) -> None:
    summary = _sanitized_summary(workbook)
    batch.status = ImportBatch.Status.AWAITING_RESOLUTION
    batch.total_files = max(batch.total_files, 1)
    batch.processed_files = max(batch.processed_files, 1)
    batch.total_rows = workbook.statistics.valid_rows + workbook.statistics.ignored_rows
    batch.processed_rows = workbook.statistics.valid_rows
    batch.issue_count = len(workbook.issues)
    batch.summary = json.dumps(summary, ensure_ascii=True)
    batch.save(update_fields=["status", "total_files", "processed_files", "total_rows", "processed_rows", "issue_count", "summary"])

    imported_file.status = ImportedFile.Status.READY if not _has_blocking_issues(workbook) else ImportedFile.Status.FAILED
    imported_file.save(update_fields=["status"])


def _rows(workbook: WorkbookData) -> list[HistoricalRow]:
    return [row for sheet in workbook.sheets for row in sheet.rows]


def _count_values(values) -> Any:
    counter = {}
    for value in values:
        cleaned = clean_text(value)
        if cleaned:
            counter[cleaned] = counter.get(cleaned, 0) + 1
    return _CounterView(counter)


def _single_non_empty(values) -> str | None:
    found = {clean_text(value) for value in values if clean_text(value)}
    return next(iter(found)) if len(found) == 1 else None


def _single_candidate_id(item: StructurePreviewItem | None) -> int | None:
    if item and item.status == "auto_matched" and len(item.candidates) == 1:
        return item.candidates[0].object_id
    return None


def _has_blocking_issues(workbook: WorkbookData) -> bool:
    return any(issue.severity == "blocking" for issue in workbook.issues)


def _sheet_visibility(value: str) -> str:
    if value == "veryHidden":
        return ImportedSheetResult.Visibility.VERY_HIDDEN
    if value == "hidden":
        return ImportedSheetResult.Visibility.HIDDEN
    return ImportedSheetResult.Visibility.VISIBLE


def _sheet_classification(value: str) -> str:
    mapping = {
        "processable": ImportedSheetResult.Classification.PROCESSABLE,
        "empty": ImportedSheetResult.Classification.EMPTY,
        "auxiliary": ImportedSheetResult.Classification.AUXILIARY,
        "summary": ImportedSheetResult.Classification.SUMMARY,
    }
    return mapping.get(value, ImportedSheetResult.Classification.UNKNOWN)


def _issue_severity(value: str) -> str:
    if value == "warning":
        return ImportRowIssue.Severity.WARNING
    if value == "error":
        return ImportRowIssue.Severity.ERROR
    if value == "blocking":
        return ImportRowIssue.Severity.BLOCKING
    return ImportRowIssue.Severity.INFO


class _CounterView(dict):
    def most_common(self, amount: int):
        return sorted(self.items(), key=lambda item: item[1], reverse=True)[:amount]


def _sanitized_summary(workbook: WorkbookData) -> dict[str, int]:
    return {
        "valid_rows": workbook.statistics.valid_rows,
        "ignored_rows": workbook.statistics.ignored_rows,
        "client_appearances": workbook.statistics.client_appearances_found,
        "distinct_assignments": workbook.statistics.distinct_assignments_found,
        "payment_entries": workbook.statistics.payment_entries_found,
        "payment_columns": workbook.statistics.payment_columns_detected,
        "historical_novelties": workbook.statistics.historical_novelties_found,
        "issues": workbook.statistics.issues_found,
    }
