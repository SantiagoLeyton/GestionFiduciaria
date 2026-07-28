from pathlib import Path

import pytest

from fiduciary.imports.historical import analyze_historical_import
from fiduciary.models import (
    Client,
    DetectedStructureElement,
    FiduciaryAssignment,
    ImportBatch,
    ImportedFile,
    ImportResolution,
    ImportedSheetResult,
    ImportRowIssue,
    Payment,
)
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup


HISTORICAL_FILE = Path("samples/fiduciary/historical/LIBRO Springfield.xlsx")


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
    assert ImportRowIssue.objects.count() == 18
    assert DetectedStructureElement.objects.count() > 0
    assert ImportResolution.objects.count() == DetectedStructureElement.objects.count()
    assert Project.objects.count() == before["projects"]
    assert GroupingType.objects.count() == before["grouping_types"]
    assert StructuralGroup.objects.count() == before["groups"]
    assert PropertyUnit.objects.count() == before["units"]
    assert Client.objects.count() == before["clients"]
    assert FiduciaryAssignment.objects.count() == before["assignments"]
    assert Payment.objects.count() == before["payments"]
    assert result.preview.row_count == 303
    assert result.preview.payment_entry_count == 306


@pytest.mark.django_db
def test_analyzer_auto_matches_safe_existing_structure(import_batch, springfield_structure):
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
    assert matched_groups["VS Viviendas"].status == "auto_matched"
    assert matched_groups["CM Comercio"].status == "auto_matched"
    assert matched_groups["ID Industrial"].status == "auto_matched"
    assert matched_groups["VS Viviendas"].candidates[0].object_id == groups["VS Viviendas"].pk


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
    assert imported_file.row_issues.filter(code="INVALID_HISTORICAL_ROW").count() == 6
    import_batch.refresh_from_db()
    assert import_batch.status == ImportBatch.Status.AWAITING_RESOLUTION
    assert import_batch.processed_rows == 303
