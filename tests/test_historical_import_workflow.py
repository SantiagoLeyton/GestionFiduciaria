from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from fiduciary.imports.cancellation import cancel_import_batch
from fiduciary.imports.historical import DuplicateHistoricalImportError, HistoricalWorkbookParser, analyze_historical_import
from fiduciary.models import (
    Client,
    DetectedStructureElement,
    FiduciaryAssignment,
    ImportBatch,
    ImportNovelty,
    ImportedFile,
    ImportedHistoricalNovelty,
    ImportedSheetResult,
    ImportRowIssue,
    ImportResolution,
    Payment,
)
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup


HISTORICAL_FILE = Path("samples/fiduciary/historical/LIBRO Springfield.xlsx")


@pytest.fixture
def accounting_client(accounting_admin_user):
    from django.test import Client as TestClient

    client = TestClient()
    client.force_login(accounting_admin_user)
    return client


@pytest.fixture
def commercial_client(commercial_user):
    from django.test import Client as TestClient

    client = TestClient()
    client.force_login(commercial_user)
    return client


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


def uploaded_historical_file():
    return SimpleUploadedFile(
        HISTORICAL_FILE.name,
        HISTORICAL_FILE.read_bytes(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def uploaded_historical_file_named(name):
    return SimpleUploadedFile(
        name,
        HISTORICAL_FILE.read_bytes(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def create_preparatory_batch(user, *, status=ImportBatch.Status.AWAITING_RESOLUTION):
    batch = ImportBatch.objects.create(
        initiated_by=user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=status,
        total_files=1,
        issue_count=1,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="libro-cancelable.xlsx",
        extension=".xlsx",
        size_bytes=128,
        sha256="c" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
        status=ImportedFile.Status.READY,
    )
    sheet = ImportedSheetResult.objects.create(
        imported_file=imported_file,
        sheet_name="Hoja 1",
        sheet_index=1,
        classification=ImportedSheetResult.Classification.PROCESSABLE,
        status=ImportedSheetResult.Status.ANALYZED,
    )
    ImportRowIssue.objects.create(
        imported_file=imported_file,
        sheet_result=sheet,
        severity=ImportRowIssue.Severity.WARNING,
        code="TEST_ISSUE",
        message="Incidencia sanitizada.",
    )
    element = DetectedStructureElement.objects.create(
        batch=batch,
        imported_file=imported_file,
        raw_value="Spring Field",
        normalized_value="springfield",
        inferred_kind=DetectedStructureElement.InferredKind.PROJECT,
        occurrence_count=203,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    ImportResolution.objects.create(detected_element=element)
    ImportNovelty.objects.create(
        batch=batch,
        imported_file=imported_file,
        sheet_result=sheet,
        novelty_type=ImportNovelty.NoveltyType.INCOMPATIBLE_STRUCTURE,
        description="Novedad sanitizada.",
    )
    ImportedHistoricalNovelty.objects.create(
        batch=batch,
        imported_file=imported_file,
        sheet_result=sheet,
        row_number=162,
        project_name="Proyecto seguro",
        grouping_name="T2",
        unit_code="303",
        assignment_number="EF-NOV",
        original_cells=[{"coordinate": "C162", "header": "APTO", "value": "303"}],
        sanitized_summary="Novedad historica detectada.",
    )
    return batch


def create_detected_element(batch, *, kind, raw_value, context=None, status=None):
    element = DetectedStructureElement.objects.create(
        batch=batch,
        raw_value=raw_value,
        normalized_value=raw_value.lower(),
        inferred_kind=kind,
        structural_context=context or {},
        occurrence_count=1,
        status=status or DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    ImportResolution.objects.create(detected_element=element)
    return element


@pytest.mark.django_db
def test_historical_import_permissions(client, accounting_client, commercial_client, accounting_admin_user):
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )
    assert client.get(reverse("fiduciary:historical_import_list")).status_code == 302
    assert accounting_client.get(reverse("fiduciary:historical_import_create")).status_code == 200
    assert commercial_client.get(reverse("fiduciary:historical_import_list")).status_code == 200
    assert commercial_client.get(reverse("fiduciary:historical_import_preview", args=[batch.pk])).status_code == 200
    assert commercial_client.get(reverse("fiduciary:historical_import_create")).status_code == 200
    assert commercial_client.get(reverse("fiduciary:historical_import_pending", args=[batch.pk])).status_code == 200


@pytest.mark.django_db
def test_commercial_can_create_historical_import_batch(commercial_client):
    response = commercial_client.post(
        reverse("fiduciary:historical_import_create"),
        {"file": uploaded_historical_file()},
    )

    assert response.status_code == 302
    assert ImportBatch.objects.count() == 1
    assert ImportedFile.objects.count() == 1


@pytest.mark.django_db
def test_accounting_can_create_batch_upload_file_and_run_analysis(accounting_client, springfield_structure):
    before = {
        "projects": Project.objects.count(),
        "groups": StructuralGroup.objects.count(),
        "units": PropertyUnit.objects.count(),
        "clients": Client.objects.count(),
        "assignments": FiduciaryAssignment.objects.count(),
        "payments": Payment.objects.count(),
    }
    response = accounting_client.post(
        reverse("fiduciary:historical_import_create"),
        {"grouping_type_hint": "Sector", "file": uploaded_historical_file()},
        follow=True,
    )
    batch = ImportBatch.objects.get()
    imported_file = batch.files.get()

    assert response.status_code == 200
    assert imported_file.original_name == HISTORICAL_FILE.name
    assert batch.status == ImportBatch.Status.READY
    assert batch.processed_rows == 300
    assert batch.issue_count == 18
    assert ImportedHistoricalNovelty.objects.filter(batch=batch).count() == 3
    assert Project.objects.count() == before["projects"]
    assert StructuralGroup.objects.count() == before["groups"]
    assert PropertyUnit.objects.count() == before["units"]
    assert Client.objects.count() == before["clients"]
    assert FiduciaryAssignment.objects.count() == before["assignments"]
    assert Payment.objects.count() == before["payments"]


@pytest.mark.django_db
def test_duplicate_upload_redirects_to_existing_preview_without_reprocessing(monkeypatch, accounting_client, springfield_structure):
    first_response = accounting_client.post(
        reverse("fiduciary:historical_import_create"),
        {"grouping_type_hint": "Sector", "file": uploaded_historical_file()},
        follow=True,
    )
    batch = ImportBatch.objects.get()
    counts = {
        "batches": ImportBatch.objects.count(),
        "files": ImportedFile.objects.count(),
        "detected": DetectedStructureElement.objects.count(),
        "resolutions": ImportResolution.objects.count(),
    }

    def fail_parse(self):
        raise AssertionError("El parser no debe ejecutarse para duplicados.")

    monkeypatch.setattr("fiduciary.imports.historical.analyzer.HistoricalWorkbookParser.parse", fail_parse)
    second_response = accounting_client.post(
        reverse("fiduciary:historical_import_create"),
        {"grouping_type_hint": "Sector", "file": uploaded_historical_file_named("copia-del-libro.xlsx")},
        follow=True,
    )
    content = second_response.content.decode()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.redirect_chain[-1][0] == reverse("fiduciary:historical_import_preview", args=[batch.pk])
    assert "Este archivo ya fue cargado anteriormente y no se volvio a procesar." in content
    assert HISTORICAL_FILE.name in content
    assert f"Lote asociado: #{batch.pk}" in content
    assert batch.get_status_display() in content
    assert ImportBatch.objects.count() == counts["batches"]
    assert ImportedFile.objects.count() == counts["files"]
    assert DetectedStructureElement.objects.count() == counts["detected"]
    assert ImportResolution.objects.count() == counts["resolutions"]


@pytest.mark.django_db
def test_duplicate_detected_after_batch_creation_removes_empty_batch(monkeypatch, accounting_client, springfield_structure):
    accounting_client.post(
        reverse("fiduciary:historical_import_create"),
        {"grouping_type_hint": "Sector", "file": uploaded_historical_file()},
        follow=True,
    )
    existing_file = ImportedFile.objects.get()

    monkeypatch.setattr("fiduciary.views.find_existing_historical_import", lambda path: None)

    def raise_duplicate(*, batch, file_path, grouping_type_hint=None):
        raise DuplicateHistoricalImportError(existing_file)

    monkeypatch.setattr("fiduciary.views.analyze_historical_import", raise_duplicate)
    response = accounting_client.post(
        reverse("fiduciary:historical_import_create"),
        {"grouping_type_hint": "Sector", "file": uploaded_historical_file_named("carrera.xlsx")},
        follow=True,
    )

    assert response.status_code == 200
    assert ImportBatch.objects.count() == 1
    assert ImportedFile.objects.count() == 1
    assert response.redirect_chain[-1][0] == reverse("fiduciary:historical_import_preview", args=[existing_file.batch_id])


@pytest.mark.django_db
def test_preview_shows_aggregates_issues_and_disabled_import_button(accounting_client, accounting_admin_user, springfield_structure):
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )
    analyze_historical_import(batch=batch, file_path=HISTORICAL_FILE, grouping_type_hint="Sector")
    response = accounting_client.get(reverse("fiduciary:historical_import_preview", args=[batch.pk]))

    assert response.status_code == 200
    content = response.content.decode()
    assert HISTORICAL_FILE.name in content
    assert "Hojas procesadas" in content
    assert "Filas validas" in content
    assert "Titulares detectados" in content
    assert "Titulares detectados" in content
    assert "Encargos" in content
    assert "300" in content
    assert "Pagos" in content
    assert "306" in content
    assert "Novedades historicas" in content
    assert "Novedades historicas detectadas" in content
    assert "FORMULA_WITH_CACHED_VALUE" in content
    assert "INVALID_HISTORICAL_ROW" in content
    assert "Springfield" in content
    assert "Sector" in content
    assert "VS Viviendas" in content or "Agrupaciones detectadas" in content
    assert "Unidades existentes" in content
    assert "Unidades nuevas" in content
    assert "La importación definitiva se habilitará en la siguiente fase." in content
    assert "disabled" in content


@pytest.mark.django_db
def test_preview_does_not_expose_client_names_documents_or_row_payments(accounting_client, accounting_admin_user, springfield_structure):
    parsed = HistoricalWorkbookParser(HISTORICAL_FILE).parse()
    first_named_client = next(client.name for sheet in parsed.sheets for row in sheet.rows for client in row.clients if client.name)
    first_novelty_name = next(
        cell.value
        for sheet in parsed.sheets
        for novelty in sheet.novelties
        for cell in novelty.cells
        if cell.header == "NOMBRE CLIENTE"
    )
    first_novelty_document = next(
        cell.value
        for sheet in parsed.sheets
        for novelty in sheet.novelties
        for cell in novelty.cells
        if cell.header == "CEDULA CLIENTE"
    )
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )
    analyze_historical_import(batch=batch, file_path=HISTORICAL_FILE, grouping_type_hint="Sector")

    response = accounting_client.get(reverse("fiduciary:historical_import_preview", args=[batch.pk]))
    content = response.content.decode()

    assert first_named_client not in content
    assert str(first_novelty_name) not in content
    assert str(first_novelty_document) not in content
    assert "CEDULA CLIENTE" not in content
    assert "25.000.000" not in content


@pytest.mark.django_db
def test_pending_list_and_resolution_apply_to_equivalent_elements(accounting_client, accounting_admin_user):
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )
    first = DetectedStructureElement.objects.create(
        batch=batch,
        raw_value="Spring Field",
        normalized_value="springfield",
        inferred_kind=DetectedStructureElement.InferredKind.PROJECT,
        occurrence_count=4,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    second = DetectedStructureElement.objects.create(
        batch=batch,
        raw_value="springfield",
        normalized_value="springfield",
        inferred_kind=DetectedStructureElement.InferredKind.PROJECT,
        occurrence_count=2,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    ImportResolution.objects.create(detected_element=first)
    ImportResolution.objects.create(detected_element=second)
    project = Project.objects.create(code="SPR", name="Springfield")

    pending_response = accounting_client.get(reverse("fiduciary:historical_import_pending", args=[batch.pk]))
    assert pending_response.status_code == 200
    assert "Spring Field" in pending_response.content.decode()

    response = accounting_client.post(
        reverse("fiduciary:historical_import_resolve", args=[batch.pk, first.pk]),
        {
            "target_kind": DetectedStructureElement.InferredKind.PROJECT,
            "action": ImportResolution.Action.ASSOCIATE_EXISTING,
            "target_project": project.pk,
        },
        follow=True,
    )
    assert response.status_code == 200
    first.refresh_from_db()
    second.refresh_from_db()
    batch.refresh_from_db()
    assert first.status == DetectedStructureElement.Status.RESOLVED
    assert second.status == DetectedStructureElement.Status.RESOLVED
    assert first.resolution.target_project == project
    assert second.resolution.target_project == project
    assert batch.status == ImportBatch.Status.READY
    assert "El lote esta listo" in response.content.decode()


@pytest.mark.django_db
def test_lot_awaiting_resolution_when_context_is_missing(accounting_client, springfield_structure):
    response = accounting_client.post(
        reverse("fiduciary:historical_import_create"),
        {"file": uploaded_historical_file()},
        follow=True,
    )
    batch = ImportBatch.objects.get()
    assert response.status_code == 200
    assert batch.status == ImportBatch.Status.AWAITING_RESOLUTION
    assert batch.detected_elements.filter(status=DetectedStructureElement.Status.NEEDS_REVIEW).exists()


@pytest.mark.django_db
def test_auto_new_units_are_prepared_without_creating_real_units(accounting_client, springfield_structure):
    before_units = PropertyUnit.objects.count()
    response = accounting_client.post(
        reverse("fiduciary:historical_import_create"),
        {"grouping_type_hint": "Sector", "file": uploaded_historical_file()},
        follow=True,
    )
    batch = ImportBatch.objects.get()
    new_unit_resolutions = ImportResolution.objects.filter(
        detected_element__batch=batch,
        detected_element__inferred_kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        action=ImportResolution.Action.CREATE_NEW,
    )
    assert response.status_code == 200
    assert new_unit_resolutions.exists()
    assert PropertyUnit.objects.count() == before_units


@pytest.mark.django_db
def test_cancel_confirmation_is_visible_for_accounting_users(accounting_client, accounting_admin_user):
    batch = create_preparatory_batch(accounting_admin_user)

    list_response = accounting_client.get(reverse("fiduciary:historical_import_list"))
    preview_response = accounting_client.get(reverse("fiduciary:historical_import_preview", args=[batch.pk]))
    confirm_response = accounting_client.get(reverse("fiduciary:historical_import_cancel", args=[batch.pk]))

    assert "Cancelar importación" in list_response.content.decode()
    assert "Cancelar importación" in preview_response.content.decode()
    content = confirm_response.content.decode()
    assert confirm_response.status_code == 200
    assert "¿Cancelar este intento de importación?" in content
    assert "libro-cancelable.xlsx" in content
    assert "<strong>Pendientes</strong>" in content
    assert "<div>1</div>" in content
    assert "Se eliminarán el análisis, las incidencias y las resoluciones pendientes." in content
    assert "Sí, cancelar importación" in content


@pytest.mark.django_db
def test_commercial_can_view_and_cancel_import(commercial_client, accounting_admin_user):
    batch = create_preparatory_batch(accounting_admin_user)

    preview_response = commercial_client.get(reverse("fiduciary:historical_import_preview", args=[batch.pk]))
    get_response = commercial_client.get(reverse("fiduciary:historical_import_cancel", args=[batch.pk]))
    post_response = commercial_client.post(reverse("fiduciary:historical_import_cancel", args=[batch.pk]), follow=True)

    assert preview_response.status_code == 200
    assert reverse("fiduciary:historical_import_cancel", args=[batch.pk]) in preview_response.content.decode()
    assert get_response.status_code == 200
    assert post_response.status_code == 200
    assert not ImportBatch.objects.filter(pk=batch.pk).exists()


@pytest.mark.django_db
def test_cancel_import_requires_post(accounting_client, accounting_admin_user):
    batch = create_preparatory_batch(accounting_admin_user)

    response = accounting_client.get(reverse("fiduciary:historical_import_cancel", args=[batch.pk]))

    assert response.status_code == 200
    assert ImportBatch.objects.filter(pk=batch.pk).exists()
    assert ImportedFile.objects.filter(batch=batch).exists()


@pytest.mark.django_db
def test_cancel_import_deletes_only_preparatory_records_and_releases_hash(accounting_client, accounting_admin_user):
    project = Project.objects.create(code="SAFE", name="Proyecto seguro")
    unit = PropertyUnit.objects.create(project=project, code="101", name="Unidad 101")
    batch = create_preparatory_batch(accounting_admin_user)

    response = accounting_client.post(reverse("fiduciary:historical_import_cancel", args=[batch.pk]), follow=True)

    assert response.status_code == 200
    assert "El intento de importación fue cancelado y sus resultados temporales fueron eliminados." in response.content.decode()
    assert not ImportBatch.objects.filter(pk=batch.pk).exists()
    assert ImportedFile.objects.count() == 0
    assert ImportedSheetResult.objects.count() == 0
    assert ImportRowIssue.objects.count() == 0
    assert DetectedStructureElement.objects.count() == 0
    assert ImportResolution.objects.count() == 0
    assert ImportNovelty.objects.count() == 0
    assert ImportedHistoricalNovelty.objects.count() == 0
    assert Project.objects.filter(pk=project.pk).exists()
    assert PropertyUnit.objects.filter(pk=unit.pk).exists()

    new_batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )
    ImportedFile.objects.create(
        batch=new_batch,
        original_name="libro-cancelable.xlsx",
        extension=".xlsx",
        size_bytes=128,
        sha256="c" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    assert ImportedFile.objects.filter(batch=new_batch, sha256="c" * 64).exists()


@pytest.mark.django_db
def test_cancel_import_blocks_completed_batches(accounting_client, accounting_admin_user):
    batch = create_preparatory_batch(accounting_admin_user, status=ImportBatch.Status.COMPLETED)

    confirm_response = accounting_client.get(reverse("fiduciary:historical_import_cancel", args=[batch.pk]))
    post_response = accounting_client.post(reverse("fiduciary:historical_import_cancel", args=[batch.pk]), follow=True)

    assert "Este lote no se puede cancelar en su estado actual." in confirm_response.content.decode()
    assert ImportBatch.objects.filter(pk=batch.pk).exists()
    assert ImportedFile.objects.filter(batch=batch).exists()
    assert "Este lote no puede cancelarse en su estado actual." in post_response.content.decode()


@pytest.mark.django_db
def test_cancel_import_blocks_batches_with_definitive_payments(accounting_client, accounting_admin_user):
    batch = create_preparatory_batch(accounting_admin_user)
    source_file = batch.files.get()
    project = Project.objects.create(code="PAY", name="Proyecto Pago")
    unit = PropertyUnit.objects.create(project=project, code="101", name="Unidad 101")
    assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-PAY-001",
        property_unit=unit,
        start_date=date(2026, 1, 1),
        last_change_reason="Prueba",
    )
    Payment.objects.create(
        assignment=assignment,
        exact_date=date(2026, 7, 22),
        date_precision=Payment.DatePrecision.EXACT,
        amount=Decimal("1000.00"),
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=source_file,
        source_sheet="Hoja 1",
        source_row=10,
    )

    response = accounting_client.post(reverse("fiduciary:historical_import_cancel", args=[batch.pk]), follow=True)

    assert ImportBatch.objects.filter(pk=batch.pk).exists()
    assert Payment.objects.filter(assignment=assignment).exists()
    assert "No es seguro cancelar un lote que ya tiene pagos asociados." in response.content.decode()


@pytest.mark.django_db
def test_cancel_service_returns_deleted_counts(accounting_admin_user):
    batch = create_preparatory_batch(accounting_admin_user)

    result = cancel_import_batch(batch=batch, cancelled_by=accounting_admin_user)

    assert result.batch_id == batch.pk
    assert result.files_deleted == 1
    assert result.sheet_results_deleted == 1
    assert result.row_issues_deleted == 1
    assert result.detected_elements_deleted == 1
    assert result.resolutions_deleted == 1
    assert result.novelties_deleted == 1
    assert result.historical_novelties_deleted == 1


@pytest.mark.django_db
def test_resolve_new_structural_group_prepares_child_units_without_business_creation(accounting_client, accounting_admin_user):
    project = Project.objects.create(code="U7", name="Universo 7")
    grouping_type = GroupingType.objects.create(code="USO", name="Uso")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )
    group_element = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        raw_value="RES",
        context={"project_id": project.pk, "grouping_type_id": grouping_type.pk},
    )
    unit_101 = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        raw_value="101",
        context={"project_id": project.pk, "grouping_name": "RES"},
    )
    unit_102 = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        raw_value="102",
        context={"project_id": project.pk, "grouping_name": "RES"},
    )
    before_groups = StructuralGroup.objects.count()
    before_units = PropertyUnit.objects.count()

    response = accounting_client.post(
        reverse("fiduciary:historical_import_resolve_group", args=[batch.pk, group_element.pk]),
        {
            "action": ImportResolution.Action.CREATE_NEW,
            "project": project.pk,
            "grouping_type": grouping_type.pk,
            "new_group_name": "RES",
        },
        follow=True,
    )

    assert response.status_code == 200
    assert "La agrupación fue resuelta y se actualizaron automáticamente 2 unidades relacionadas." in response.content.decode()
    assert StructuralGroup.objects.count() == before_groups
    assert PropertyUnit.objects.count() == before_units
    group_element.refresh_from_db()
    unit_101.refresh_from_db()
    unit_102.refresh_from_db()
    assert group_element.status == DetectedStructureElement.Status.RESOLVED
    assert group_element.resolution.action == ImportResolution.Action.CREATE_NEW
    assert group_element.resolution.parent_project == project
    assert group_element.resolution.parent_grouping_type == grouping_type
    assert group_element.resolution.create_name == "RES"
    assert unit_101.status == DetectedStructureElement.Status.RESOLVED
    assert unit_102.status == DetectedStructureElement.Status.RESOLVED
    assert unit_101.resolution.action == ImportResolution.Action.CREATE_NEW
    assert unit_102.resolution.action == ImportResolution.Action.CREATE_NEW
    assert unit_101.structural_context["parent_group_resolution_id"] == group_element.resolution.pk
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.READY


@pytest.mark.django_db
def test_resolve_existing_structural_group_scopes_unit_matching_to_parent_group(accounting_client, accounting_admin_user):
    project = Project.objects.create(code="U7", name="Universo 7")
    grouping_type = GroupingType.objects.create(code="USO", name="Uso")
    res_group = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="RES", name="Residencial")
    com_group = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="COM", name="Comercial")
    existing_res_unit = PropertyUnit.objects.create(project=project, structural_group=res_group, code="101", name="101")
    PropertyUnit.objects.create(project=project, structural_group=com_group, code="101", name="101")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )
    group_element = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        raw_value="RES",
        context={"project_id": project.pk, "grouping_type_id": grouping_type.pk},
    )
    unit_101 = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        raw_value="101",
        context={"project_id": project.pk, "grouping_name": "RES"},
    )
    unit_102 = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        raw_value="102",
        context={"project_id": project.pk, "grouping_name": "RES"},
    )

    response = accounting_client.post(
        reverse("fiduciary:historical_import_resolve_group", args=[batch.pk, group_element.pk]),
        {
            "action": ImportResolution.Action.ASSOCIATE_EXISTING,
            "project": project.pk,
            "grouping_type": grouping_type.pk,
            "existing_group": res_group.pk,
            "new_group_name": "",
        },
        follow=True,
    )

    assert response.status_code == 200
    unit_101.refresh_from_db()
    unit_102.refresh_from_db()
    assert unit_101.resolution.action == ImportResolution.Action.ASSOCIATE_EXISTING
    assert unit_101.resolution.target_property_unit == existing_res_unit
    assert unit_102.resolution.action == ImportResolution.Action.CREATE_NEW
    assert unit_102.resolution.parent_structural_group == res_group


@pytest.mark.django_db
def test_group_resolution_filters_existing_groups_by_project_and_type(accounting_client, accounting_admin_user):
    project = Project.objects.create(code="U7", name="Universo 7")
    other_project = Project.objects.create(code="OTR", name="Otro")
    grouping_type = GroupingType.objects.create(code="USO", name="Uso")
    other_type = GroupingType.objects.create(code="TOR", name="Torre")
    valid_group = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="RES", name="Residencial")
    StructuralGroup.objects.create(project=other_project, grouping_type=grouping_type, code="RES", name="Residencial otro")
    StructuralGroup.objects.create(project=project, grouping_type=other_type, code="TOR", name="Residencial torre")

    response = accounting_client.get(
        reverse("fiduciary:historical_import_context_groups"),
        {"project": project.pk, "grouping_type": grouping_type.pk},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"] == [{"id": valid_group.pk, "text": str(valid_group)}]


@pytest.mark.django_db
def test_reanalyze_pending_uses_newly_created_structure_without_new_batch_or_file(accounting_client, accounting_admin_user):
    project = Project.objects.create(code="U7", name="Universo 7")
    grouping_type = GroupingType.objects.create(code="USO", name="Uso")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )
    ImportedFile.objects.create(
        batch=batch,
        original_name="universo.xlsx",
        extension=".xlsx",
        size_bytes=128,
        sha256="d" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    group_element = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        raw_value="RES",
        context={"project_id": project.pk, "grouping_type_id": grouping_type.pk},
    )
    unit_element = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        raw_value="101",
        context={"project_id": project.pk, "grouping_name": "RES"},
    )
    StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="RES", name="RES")

    response = accounting_client.post(reverse("fiduciary:historical_import_reanalyze_pending", args=[batch.pk]), follow=True)

    assert response.status_code == 200
    assert ImportBatch.objects.count() == 1
    assert ImportedFile.objects.count() == 1
    group_element.refresh_from_db()
    unit_element.refresh_from_db()
    assert group_element.resolution.action == ImportResolution.Action.ASSOCIATE_EXISTING
    assert unit_element.resolution.action == ImportResolution.Action.CREATE_NEW
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.READY


@pytest.mark.django_db
def test_reanalyze_converts_child_units_with_unresolved_parent_to_blocked(accounting_client, accounting_admin_user):
    project = Project.objects.create(code="U7", name="Universo 7")
    grouping_type = GroupingType.objects.create(code="USO", name="Uso")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )
    create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        raw_value="COM Comercial",
        context={"project_id": project.pk, "grouping_type_id": grouping_type.pk},
    )
    unit = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        raw_value="L101",
        context={"project_id": project.pk, "grouping_name": "COM Comercial"},
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )

    response = accounting_client.post(reverse("fiduciary:historical_import_reanalyze_pending", args=[batch.pk]), follow=True)

    assert response.status_code == 200
    unit.refresh_from_db()
    assert unit.status == DetectedStructureElement.Status.DETECTED
    assert unit.resolution.action == ImportResolution.Action.UNRESOLVED
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.AWAITING_RESOLUTION


@pytest.mark.django_db
def test_resolving_project_and_type_propagates_context_and_resolves_group_children(accounting_client, accounting_admin_user):
    project = Project.objects.create(code="MON", name="Montecielo")
    grouping_type = GroupingType.objects.create(code="TOR", name="Torre")
    group = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T2", name="T2")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )
    project_element = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.PROJECT,
        raw_value="Montecielo",
    )
    type_element = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.GROUPING_TYPE,
        raw_value="Torre",
    )
    group_element = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        raw_value="T2",
        context={"project_id": None, "grouping_type_id": None},
    )
    unit_element = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        raw_value="101",
        context={"project_id": None, "grouping_name": "T2"},
        status=DetectedStructureElement.Status.DETECTED,
    )

    accounting_client.post(
        reverse("fiduciary:historical_import_resolve", args=[batch.pk, project_element.pk]),
        {
            "target_kind": DetectedStructureElement.InferredKind.PROJECT,
            "action": ImportResolution.Action.ASSOCIATE_EXISTING,
            "target_project": project.pk,
        },
        follow=True,
    )
    accounting_client.post(
        reverse("fiduciary:historical_import_resolve", args=[batch.pk, type_element.pk]),
        {
            "target_kind": DetectedStructureElement.InferredKind.GROUPING_TYPE,
            "action": ImportResolution.Action.ASSOCIATE_EXISTING,
            "target_grouping_type": grouping_type.pk,
        },
        follow=True,
    )

    group_element.refresh_from_db()
    unit_element.refresh_from_db()
    assert group_element.structural_context["project_id"] == project.pk
    assert group_element.structural_context["grouping_type_id"] == grouping_type.pk
    assert group_element.status == DetectedStructureElement.Status.RESOLVED
    assert group_element.resolution.target_structural_group == group
    assert unit_element.status == DetectedStructureElement.Status.RESOLVED
    assert unit_element.resolution.action == ImportResolution.Action.CREATE_NEW
    assert unit_element.resolution.parent_structural_group == group


@pytest.mark.django_db
def test_resolving_two_unknown_groups_prepares_all_blocked_units(accounting_client, accounting_admin_user):
    project = Project.objects.create(code="U7", name="Universo 7")
    grouping_type = GroupingType.objects.create(code="USO", name="Uso")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )
    res_group = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        raw_value="RES Residencial",
        context={"project_id": project.pk, "grouping_type_id": grouping_type.pk},
    )
    com_group = create_detected_element(
        batch,
        kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        raw_value="COM Comercial",
        context={"project_id": project.pk, "grouping_type_id": grouping_type.pk},
    )
    for index in range(1, 102):
        create_detected_element(
            batch,
            kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
            raw_value=f"{index}",
            context={"project_id": project.pk, "grouping_name": "RES Residencial"},
            status=DetectedStructureElement.Status.DETECTED,
        )
    for index in range(1, 101):
        create_detected_element(
            batch,
            kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
            raw_value=f"L{index}",
            context={"project_id": project.pk, "grouping_name": "COM Comercial"},
            status=DetectedStructureElement.Status.DETECTED,
        )

    first_response = accounting_client.post(
        reverse("fiduciary:historical_import_resolve_group", args=[batch.pk, res_group.pk]),
        {
            "action": ImportResolution.Action.CREATE_NEW,
            "project": project.pk,
            "grouping_type": grouping_type.pk,
            "new_group_name": "RES Residencial",
        },
        follow=True,
    )
    assert "se actualizaron autom" in first_response.content.decode()
    assert DetectedStructureElement.objects.filter(
        batch=batch,
        inferred_kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        status=DetectedStructureElement.Status.DETECTED,
    ).count() == 100
    assert DetectedStructureElement.objects.filter(
        batch=batch,
        inferred_kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        resolution__action=ImportResolution.Action.CREATE_NEW,
    ).count() == 101

    accounting_client.post(
        reverse("fiduciary:historical_import_resolve_group", args=[batch.pk, com_group.pk]),
        {
            "action": ImportResolution.Action.CREATE_NEW,
            "project": project.pk,
            "grouping_type": grouping_type.pk,
            "new_group_name": "COM Comercial",
        },
        follow=True,
    )

    assert DetectedStructureElement.objects.filter(
        batch=batch,
        inferred_kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        status=DetectedStructureElement.Status.DETECTED,
    ).count() == 0
    assert DetectedStructureElement.objects.filter(
        batch=batch,
        inferred_kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT,
        resolution__action=ImportResolution.Action.CREATE_NEW,
    ).count() == 201
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.READY
