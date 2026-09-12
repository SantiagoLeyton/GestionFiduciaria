from pathlib import Path

import pytest
from django.db import connection
from django.db import IntegrityError
from django.db.migrations.executor import MigrationExecutor

from fiduciary.imports.historical import DuplicateHistoricalImportError, analyze_historical_import
from fiduciary.imports.historical.analyzer import reserve_historical_import_file
from fiduciary.imports.historical.data import HistoricalAssignment, HistoricalClient, HistoricalRow, ParseStatistics, SheetData, WorkbookData
from fiduciary.imports.historical.resolutions import reanalyze_pending_resolutions, update_batch_resolution_state
from fiduciary.imports.historical.normalize import normalize_text
from fiduciary.models import (
    Client,
    DetectedStructureElement,
    FiduciaryAssignment,
    ImportBatch,
    ImportedHistoricalNovelty,
    ImportedFile,
    ImportResolution,
    ImportedSheetResult,
    ImportRowIssue,
    Payment,
)
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup


HISTORICAL_FILE = Path("samples/fiduciary/historical/LIBRO Springfield.xlsx")
UNIVERSO_FILE = Path("samples/fiduciary/historical/LIBRO_Universo_7.xlsx")


def _workbook_with_groups(path: Path, *, project_name: str, group_names: list[str]) -> WorkbookData:
    sheets = []
    for index, group_name in enumerate(group_names, start=1):
        rows = [
            HistoricalRow(
                sheet_name=group_name,
                row_number=5,
                project=project_name,
                grouping_type=None,
                grouping_code=group_name,
                grouping_name=group_name,
                unit_code="101",
                unit_name="101",
                assignment=HistoricalAssignment(f"EF-{group_name}-101"),
                clients=[
                    HistoricalClient(
                        order=1,
                        name=f"CLIENTE {group_name}",
                        document_number=f"900{index}",
                        is_primary=True,
                    )
                ],
            )
        ]
        sheets.append(
            SheetData(
                name=group_name,
                index=index,
                visibility="visible",
                used_rows=5,
                used_columns=8,
                classification="processable",
                header_row=4,
                rows=rows,
            )
        )
    return WorkbookData(
        path=path,
        file_type="xlsx",
        sheets=sheets,
        statistics=ParseStatistics(
            sheets_total=len(sheets),
            sheets_processed=len(sheets),
            valid_rows=len(sheets),
            client_appearances_found=len(sheets),
            distinct_assignments_found=len(sheets),
        ),
    )


def _patch_workbook_parser(monkeypatch, workbook: WorkbookData) -> None:
    class FakeHistoricalWorkbookParser:
        def __init__(self, *args, **kwargs):
            pass

        def parse(self):
            return workbook

    monkeypatch.setattr(
        "fiduciary.imports.historical.analyzer.HistoricalWorkbookParser",
        FakeHistoricalWorkbookParser,
    )


@pytest.fixture
def import_batch(db, accounting_admin_user):
    return ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )


@pytest.fixture
def springfield_structure(db):
    project = Project.objects.create(code="SPR", name="Springfield")
    grouping_type = GroupingType.objects.create(code="SEC", name="Sector")
    groups = {
        "VS Viviendas": StructuralGroup.objects.create(
            project=project,
            grouping_type=grouping_type,
            code="VS",
            name="VS Viviendas",
        ),
        "CM Comercio": StructuralGroup.objects.create(
            project=project,
            grouping_type=grouping_type,
            code="CM",
            name="CM Comercio",
        ),
        "ID Industrial": StructuralGroup.objects.create(
            project=project,
            grouping_type=grouping_type,
            code="ID",
            name="ID Industrial",
        ),
    }
    PropertyUnit.objects.create(project=project, structural_group=groups["VS Viviendas"], code="101", name="101")
    return project, grouping_type, groups


@pytest.mark.django_db
def test_analyze_historical_import_persists_only_preparation_records(import_batch, springfield_structure):
    before = {
        "projects": Project.objects.count(),
        "grouping_types": GroupingType.objects.count(),
        "groups": StructuralGroup.objects.count(),
        "units": PropertyUnit.objects.count(),
        "clients": Client.objects.count(),
        "assignments": FiduciaryAssignment.objects.count(),
        "payments": Payment.objects.count(),
    }

    result = analyze_historical_import(
        batch=import_batch,
        file_path=HISTORICAL_FILE,
        grouping_type_hint="Sector",
    )

    assert ImportedFile.objects.filter(batch=import_batch).count() == 1
    assert ImportedSheetResult.objects.count() == 3
    assert ImportRowIssue.objects.count() == 159
    assert ImportRowIssue.objects.filter(code="FORMULA_WITH_CACHED_VALUE").count() == 12
    assert ImportRowIssue.objects.filter(code="HIST_PAYMENT_DATE_RECEIPT_MISMATCH").count() == 143
    assert ImportRowIssue.objects.filter(code="HIST_PAYMENT_VALUE_COUNT_MISMATCH", severity=ImportRowIssue.Severity.INFO).count() == 4
    assert ImportRowIssue.objects.filter(code="HIST_PAYMENT_VALUE_COUNT_MISMATCH", field_name__endswith="...").exists()
    assert ImportedHistoricalNovelty.objects.count() == 3
    assert DetectedStructureElement.objects.count() > 0
    assert ImportResolution.objects.count() == DetectedStructureElement.objects.count()
    assert Project.objects.count() == before["projects"]
    assert GroupingType.objects.count() == before["grouping_types"]
    assert StructuralGroup.objects.count() == before["groups"]
    assert PropertyUnit.objects.count() == before["units"]
    assert Client.objects.count() == before["clients"]
    assert FiduciaryAssignment.objects.count() == before["assignments"]
    assert Payment.objects.count() == before["payments"]
    assert result.preview.row_count == 300
    assert result.preview.payment_entry_count == 306
    assert result.preview.historical_novelty_count == 3


@pytest.mark.django_db
def test_analyzer_detects_existing_groups_but_leaves_grouping_type_resolution_explicit(import_batch, springfield_structure):
    project, grouping_type, groups = springfield_structure

    result = analyze_historical_import(
        batch=import_batch,
        file_path=HISTORICAL_FILE,
        grouping_type_hint="Sector",
    )

    assert result.preview.project.status == "auto_matched"
    assert result.preview.project.candidates[0].object_id == project.pk
    assert result.preview.grouping_type.status == "auto_matched"
    assert result.preview.grouping_type.candidates[0].object_id == grouping_type.pk
    matched_groups = {item.raw_value: item for item in result.preview.structural_groups}
    assert matched_groups["VS Viviendas"].status == "needs_review"
    assert matched_groups["CM Comercio"].status == "needs_review"
    assert matched_groups["ID Industrial"].status == "needs_review"
    assert matched_groups["VS Viviendas"].candidates == []


@pytest.mark.django_db
def test_analyzer_detects_multiple_grouping_patterns_without_deducing_grouping_type(
    import_batch,
    tmp_path,
    monkeypatch,
):
    project = Project.objects.create(code="KSMP", name="KSMP")
    torre = GroupingType.objects.create(code="T", name="Torre")
    edificacion = GroupingType.objects.create(code="ED", name="Edificacion")
    groups = {
        "T1": StructuralGroup.objects.create(project=project, grouping_type=torre, code="T1", name="T1"),
        "T2": StructuralGroup.objects.create(project=project, grouping_type=torre, code="T2", name="T2"),
        "ED1": StructuralGroup.objects.create(project=project, grouping_type=edificacion, code="ED1", name="ED1"),
        "ED2": StructuralGroup.objects.create(project=project, grouping_type=edificacion, code="ED2", name="ED2"),
    }
    for group in groups.values():
        PropertyUnit.objects.create(project=project, structural_group=group, code="101", name="101")
    path = tmp_path / "LIBRO_KSMP_MULTITIPO.xlsx"
    path.write_bytes(b"historical")
    _patch_workbook_parser(monkeypatch, _workbook_with_groups(path, project_name="KSMP", group_names=["T1", "T2", "ED1", "ED2"]))

    result = analyze_historical_import(batch=import_batch, file_path=path, grouping_type_hint="Torre")

    matched = {item.raw_value: item for item in result.preview.structural_groups}
    assert set(matched) == {"T1", "T2", "ED1", "ED2"}
    for raw_value, group in groups.items():
        assert matched[raw_value].status == "needs_review"
        assert matched[raw_value].candidates == []
        element = DetectedStructureElement.objects.get(pk=matched[raw_value].detected_element_id)
        assert element.structural_context["project_id"] == project.pk
        assert element.structural_context["grouping_name"] == raw_value
        assert "grouping_type_id" not in element.structural_context
        assert element.resolution.action == ImportResolution.Action.UNRESOLVED
    assert result.preview.pending_resolution_count == 4


@pytest.mark.django_db
def test_analyzer_keeps_group_pending_even_when_code_exists_until_type_is_selected(
    import_batch,
    tmp_path,
    monkeypatch,
):
    project = Project.objects.create(code="AMB", name="Ambiguo")
    torre = GroupingType.objects.create(code="T", name="Torre")
    edificacion = GroupingType.objects.create(code="ED", name="Edificacion")
    StructuralGroup.objects.create(project=project, grouping_type=torre, code="X1", name="X1 Torre")
    StructuralGroup.objects.create(project=project, grouping_type=edificacion, code="ED1", name="X1")
    path = tmp_path / "LIBRO_AMBIGUO.xlsx"
    path.write_bytes(b"historical")
    _patch_workbook_parser(monkeypatch, _workbook_with_groups(path, project_name="Ambiguo", group_names=["X1"]))

    result = analyze_historical_import(batch=import_batch, file_path=path)

    group = result.preview.structural_groups[0]
    assert group.raw_value == "X1"
    assert group.status == "needs_review"
    assert group.candidates == []
    element = DetectedStructureElement.objects.get(pk=group.detected_element_id)
    assert element.status == DetectedStructureElement.Status.NEEDS_REVIEW
    assert element.resolution.action == ImportResolution.Action.UNRESOLVED


@pytest.mark.django_db
def test_analyzer_marks_missing_units_as_pending_resolution(import_batch, springfield_structure):
    result = analyze_historical_import(
        batch=import_batch,
        file_path=HISTORICAL_FILE,
        grouping_type_hint="Sector",
    )

    unit_101 = next(item for item in result.preview.property_units if item.raw_value == "101")
    missing_units = [item for item in result.preview.property_units if item.raw_value != "101"]

    assert unit_101.status == "auto_matched"
    assert missing_units
    assert all(item.status == "needs_review" for item in missing_units)
    assert result.preview.pending_resolution_count == len(missing_units)


@pytest.mark.django_db
def test_unresolved_groups_block_child_units_instead_of_actionable_pending(import_batch):
    Project.objects.create(code="U7", name="Universo 7")
    GroupingType.objects.create(code="SEC", name="Sector")

    result = analyze_historical_import(
        batch=import_batch,
        file_path=UNIVERSO_FILE,
        grouping_type_hint="Sector",
    )
    elements = DetectedStructureElement.objects.filter(batch=import_batch)

    assert result.preview.row_count == 200
    assert {item.raw_value for item in result.preview.structural_groups} == {"RES Residencial", "COM Comercial"}
    assert elements.filter(
        inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    ).count() == 2
    assert elements.filter(
        inferred_kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        status=DetectedStructureElement.Status.DETECTED,
        resolution__action=ImportResolution.Action.UNRESOLVED,
    ).count() == 200
    assert elements.filter(status=DetectedStructureElement.Status.NEEDS_REVIEW).count() == 2
    assert result.preview.pending_resolution_count == 2


@pytest.mark.django_db
def test_analyzer_without_grouping_type_hint_does_not_force_sector(import_batch, springfield_structure):
    result = analyze_historical_import(batch=import_batch, file_path=HISTORICAL_FILE)

    assert result.preview.grouping_type.status == "needs_review"
    assert result.preview.grouping_type.raw_value == ""
    assert all(item.status == "needs_review" for item in result.preview.structural_groups)


@pytest.mark.django_db
def test_analyzer_creates_unresolved_resolution_for_ambiguous_project(import_batch):
    Project.objects.create(code="A", name="Springfield")
    Project.objects.create(code="B", name="Springfield")

    result = analyze_historical_import(batch=import_batch, file_path=HISTORICAL_FILE, grouping_type_hint="Sector")

    assert result.preview.project.status == "needs_review"
    assert len(result.preview.project.candidates) == 2
    resolution = ImportResolution.objects.get(pk=result.preview.project.resolution_id)
    assert resolution.action == ImportResolution.Action.UNRESOLVED


@pytest.mark.django_db
def test_analyzer_records_sheet_results_and_parser_issues(import_batch, springfield_structure):
    result = analyze_historical_import(
        batch=import_batch,
        file_path=HISTORICAL_FILE,
        grouping_type_hint="Sector",
    )

    imported_file = result.imported_file
    assert imported_file.original_name == HISTORICAL_FILE.name
    assert imported_file.sha256
    assert set(imported_file.sheet_results.values_list("sheet_name", flat=True)) == {"VS", "CM", "ID"}
    assert imported_file.row_issues.filter(code="FORMULA_WITH_CACHED_VALUE").count() == 12
    assert imported_file.row_issues.filter(code="INVALID_HISTORICAL_ROW").count() == 0
    assert imported_file.row_issues.filter(code="HISTORICAL_NOVELTY_SECTION_SKIPPED").count() == 0
    assert imported_file.historical_novelties.count() == 3
    novelty = imported_file.historical_novelties.order_by("sheet_result__sheet_index", "row_number").first()
    assert novelty.project_name == "Springfield"
    assert novelty.grouping_name in {"VS Viviendas", "CM Comercio", "ID Industrial"}
    assert novelty.unit_code
    assert novelty.assignment_number
    assert novelty.original_cells
    assert all("coordinate" in cell and "value" in cell for cell in novelty.original_cells)
    import_batch.refresh_from_db()
    assert import_batch.status == ImportBatch.Status.AWAITING_RESOLUTION
    assert import_batch.processed_rows == 300


@pytest.mark.django_db
def test_same_name_and_same_content_is_blocked(import_batch, accounting_admin_user, springfield_structure):
    first = analyze_historical_import(batch=import_batch, file_path=HISTORICAL_FILE, grouping_type_hint="Sector")
    second_batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )

    with pytest.raises(DuplicateHistoricalImportError) as exc_info:
        analyze_historical_import(batch=second_batch, file_path=HISTORICAL_FILE, grouping_type_hint="Sector")

    assert exc_info.value.imported_file == first.imported_file
    assert ImportedFile.objects.count() == 1


@pytest.mark.django_db
def test_different_name_and_same_content_is_blocked(tmp_path, import_batch, accounting_admin_user, springfield_structure):
    first = analyze_historical_import(batch=import_batch, file_path=HISTORICAL_FILE, grouping_type_hint="Sector")
    duplicate_path = tmp_path / "otro-nombre.xlsx"
    duplicate_path.write_bytes(HISTORICAL_FILE.read_bytes())
    second_batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )

    with pytest.raises(DuplicateHistoricalImportError) as exc_info:
        analyze_historical_import(batch=second_batch, file_path=duplicate_path, grouping_type_hint="Sector")

    assert exc_info.value.imported_file == first.imported_file
    assert ImportedFile.objects.count() == 1


@pytest.mark.django_db
def test_same_name_and_different_content_is_allowed(tmp_path, import_batch, accounting_admin_user, springfield_structure):
    analyze_historical_import(batch=import_batch, file_path=HISTORICAL_FILE, grouping_type_hint="Sector")
    changed_path = tmp_path / HISTORICAL_FILE.name
    changed_path.write_bytes(HISTORICAL_FILE.read_bytes() + b"\n")
    second_batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )

    result = analyze_historical_import(batch=second_batch, file_path=changed_path, grouping_type_hint="Sector")

    assert result.imported_file.original_name == HISTORICAL_FILE.name
    assert ImportedFile.objects.count() == 2


@pytest.mark.django_db
def test_duplicate_inside_same_batch_is_blocked(import_batch, springfield_structure):
    first = analyze_historical_import(batch=import_batch, file_path=HISTORICAL_FILE, grouping_type_hint="Sector")

    with pytest.raises(DuplicateHistoricalImportError) as exc_info:
        analyze_historical_import(batch=import_batch, file_path=HISTORICAL_FILE, grouping_type_hint="Sector")

    assert exc_info.value.imported_file == first.imported_file
    assert ImportedFile.objects.count() == 1


@pytest.mark.django_db
def test_duplicate_does_not_run_parser_or_create_derived_records(monkeypatch, import_batch, accounting_admin_user, springfield_structure):
    analyze_historical_import(batch=import_batch, file_path=HISTORICAL_FILE, grouping_type_hint="Sector")
    counts = {
        "files": ImportedFile.objects.count(),
        "sheets": ImportedSheetResult.objects.count(),
        "issues": ImportRowIssue.objects.count(),
        "historical_novelties": ImportedHistoricalNovelty.objects.count(),
        "detected": DetectedStructureElement.objects.count(),
        "resolutions": ImportResolution.objects.count(),
    }

    def fail_parse(self):
        raise AssertionError("El parser no debe ejecutarse para duplicados.")

    monkeypatch.setattr("fiduciary.imports.historical.analyzer.HistoricalWorkbookParser.parse", fail_parse)
    second_batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )

    with pytest.raises(DuplicateHistoricalImportError):
        analyze_historical_import(batch=second_batch, file_path=HISTORICAL_FILE, grouping_type_hint="Sector")

    assert ImportedFile.objects.count() == counts["files"]
    assert ImportedSheetResult.objects.count() == counts["sheets"]
    assert ImportRowIssue.objects.count() == counts["issues"]
    assert ImportedHistoricalNovelty.objects.count() == counts["historical_novelties"]
    assert DetectedStructureElement.objects.count() == counts["detected"]
    assert ImportResolution.objects.count() == counts["resolutions"]


@pytest.mark.django_db
def test_historical_hash_unique_constraint_is_controlled(import_batch, accounting_admin_user):
    first = reserve_historical_import_file(batch=import_batch, file_path=HISTORICAL_FILE)
    second_batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )

    with pytest.raises(DuplicateHistoricalImportError) as exc_info:
        reserve_historical_import_file(batch=second_batch, file_path=HISTORICAL_FILE)

    assert exc_info.value.imported_file == first


@pytest.mark.django_db(transaction=True)
def test_database_blocks_historical_duplicate_sha(import_batch, accounting_admin_user):
    first = reserve_historical_import_file(batch=import_batch, file_path=HISTORICAL_FILE)
    second_batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )

    with pytest.raises(IntegrityError):
        ImportedFile.objects.create(
            batch=second_batch,
            original_name="otro.xlsx",
            extension=".xlsx",
            size_bytes=first.size_bytes,
            sha256=first.sha256,
            file_type=ImportedFile.FileType.HISTORICAL,
        )


@pytest.mark.django_db(transaction=True)
def test_migration_cleans_previous_preparation_duplicates(accounting_admin_user):
    executor = MigrationExecutor(connection)
    executor.migrate([("fiduciary", "0002_detectedstructureelement_importbatch_importedfile_and_more")])
    old_apps = executor.loader.project_state(
        [("fiduciary", "0002_detectedstructureelement_importbatch_importedfile_and_more")]
    ).apps
    OldImportBatch = old_apps.get_model("fiduciary", "ImportBatch")
    OldImportedFile = old_apps.get_model("fiduciary", "ImportedFile")
    OldImportedSheetResult = old_apps.get_model("fiduciary", "ImportedSheetResult")
    OldImportRowIssue = old_apps.get_model("fiduciary", "ImportRowIssue")
    OldDetectedStructureElement = old_apps.get_model("fiduciary", "DetectedStructureElement")
    OldImportResolution = old_apps.get_model("fiduciary", "ImportResolution")

    canonical_batch = OldImportBatch.objects.create(
        initiated_by_id=accounting_admin_user.pk,
        import_type="historical",
        load_mode="single_file",
        status="ready",
    )
    duplicate_batch = OldImportBatch.objects.create(
        initiated_by_id=accounting_admin_user.pk,
        import_type="historical",
        load_mode="single_file",
        status="awaiting_resolution",
    )
    canonical_file = OldImportedFile.objects.create(
        batch=canonical_batch,
        original_name="canonico.xlsx",
        extension=".xlsx",
        size_bytes=10,
        sha256="a" * 64,
        file_type="historical",
    )
    duplicate_file = OldImportedFile.objects.create(
        batch=duplicate_batch,
        original_name="duplicado.xlsx",
        extension=".xlsx",
        size_bytes=10,
        sha256="a" * 64,
        file_type="historical",
    )
    duplicate_sheet = OldImportedSheetResult.objects.create(
        imported_file=duplicate_file,
        sheet_name="VS",
        sheet_index=1,
    )
    OldImportRowIssue.objects.create(
        imported_file=duplicate_file,
        sheet_result=duplicate_sheet,
        severity="warning",
        code="TEST_DUPLICATE",
        message="Incidencia sanitizada.",
    )
    duplicate_element = OldDetectedStructureElement.objects.create(
        batch=duplicate_batch,
        imported_file=duplicate_file,
        raw_value="Duplicado",
        normalized_value="duplicado",
        inferred_kind="project",
        status="needs_review",
    )
    OldImportResolution.objects.create(detected_element=duplicate_element)

    try:
        executor = MigrationExecutor(connection)
        executor.migrate([("fiduciary", "0003_importedfile_fiduciary_imported_file_historical_sha_unique")])
        migrated_apps = executor.loader.project_state(
            [("fiduciary", "0003_importedfile_fiduciary_imported_file_historical_sha_unique")]
        ).apps
        MigratedImportBatch = migrated_apps.get_model("fiduciary", "ImportBatch")
        MigratedImportedFile = migrated_apps.get_model("fiduciary", "ImportedFile")

        assert MigratedImportedFile.objects.filter(sha256="a" * 64, file_type="historical").count() == 1
        assert MigratedImportedFile.objects.get(sha256="a" * 64).original_name == canonical_file.original_name
        assert not MigratedImportBatch.objects.filter(pk=duplicate_batch.pk).exists()
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate([("fiduciary", "0005_importbatch_imported_at_importbatch_imported_by_and_more")])
