from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse

from fiduciary.imports.historical import analyze_historical_import, store_historical_import_file
from fiduciary.imports.historical.finalize import (
    HistoricalImportFinalizationError,
    _summary_detail_from_cells,
    finalize_historical_import,
)
from fiduciary.imports.historical.resolutions import auto_resolve_new_units, update_batch_resolution_state
from fiduciary.models import (
    Client,
    FiduciaryAssignment,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportedHistoricalObservation,
    ImportedHistoricalNovelty,
    OperationalNovelty,
    Payment,
)
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup


pytestmark = pytest.mark.django_db


SAMPLE = Path("samples/fiduciary/historical/LIBRO_Universo_7.xlsx")


def prepare_ready_historical_batch(user):
    project = Project.objects.create(code="Universo 7", name="Universo 7")
    grouping_type = GroupingType.objects.create(code="Sector", name="Sector")
    StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="RES", name="RES Residencial")
    StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="COM", name="COM Comercial")
    batch = ImportBatch.objects.create(
        initiated_by=user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    result = analyze_historical_import(batch=batch, file_path=SAMPLE, grouping_type_hint="Sector")
    store_historical_import_file(imported_file=result.imported_file, source_path=SAMPLE)
    auto_resolve_new_units(batch, user=user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.READY
    return batch


def test_finalize_historical_import_creates_definitive_entities(accounting_admin_user):
    batch = prepare_ready_historical_batch(accounting_admin_user)

    result = finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    batch.refresh_from_db()
    imported_file = batch.files.get()
    assert batch.status == ImportBatch.Status.COMPLETED
    assert batch.imported_by == accounting_admin_user
    assert batch.imported_at is not None
    assert imported_file.status == ImportedFile.Status.COMPLETED
    assert result.created_property_units == 200
    assert result.created_assignments == 201
    assert result.created_payments == 210
    assert PropertyUnit.objects.count() == 200
    assert FiduciaryAssignment.objects.count() == 201
    assert FiduciaryAssignment.objects.filter(is_active=False).count() == 1
    assert Payment.objects.count() == 210
    assert Client.objects.filter(source_origin=Client.SourceOrigin.HISTORICAL_IMPORT).exists()
    assert ImportedHistoricalNovelty.objects.filter(batch=batch, status=ImportedHistoricalNovelty.Status.READY).count() == 1
    assert OperationalNovelty.objects.filter(
        batch=batch,
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        novelty_type=OperationalNovelty.NoveltyType.HISTORICAL,
    ).count() == 1
    assert ImportAppliedRecord.objects.filter(batch=batch, entity_kind=ImportAppliedRecord.EntityKind.PAYMENT).count() == 210


def test_commercial_can_finalize_historical_import(commercial_user):
    batch = prepare_ready_historical_batch(commercial_user)

    finalize_historical_import(batch_id=batch.pk, user=commercial_user)

    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.COMPLETED


def test_anonymous_user_cannot_finalize(accounting_admin_user):
    batch = prepare_ready_historical_batch(accounting_admin_user)

    with pytest.raises(PermissionDenied):
        finalize_historical_import(batch_id=batch.pk, user=AnonymousUser())

    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.READY
    assert Payment.objects.count() == 0


def test_finalize_requires_ready_batch(accounting_admin_user):
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
    )

    with pytest.raises(HistoricalImportFinalizationError):
        finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.AWAITING_RESOLUTION


def test_assignment_number_is_globally_unique(accounting_admin_user):
    project = Project.objects.create(code="P1", name="Proyecto 1")
    unit = PropertyUnit.objects.create(project=project, code="101", name="101")
    other_unit = PropertyUnit.objects.create(project=project, code="102", name="102")
    FiduciaryAssignment.objects.create(assignment_number="EF-GLOBAL", property_unit=unit, start_date="2026-01-01")

    with pytest.raises(ValidationError):
        FiduciaryAssignment(
            assignment_number="EF-GLOBAL",
            property_unit=other_unit,
            start_date="2026-01-02",
        ).full_clean()

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            FiduciaryAssignment.objects.bulk_create(
                [FiduciaryAssignment(assignment_number="EF-GLOBAL", property_unit=other_unit, start_date="2026-01-02")]
            )


def test_existing_assignment_number_with_different_unit_rolls_back(accounting_admin_user):
    batch = prepare_ready_historical_batch(accounting_admin_user)
    project = Project.objects.get(code="Universo 7")
    conflicting_unit = PropertyUnit.objects.create(project=project, code="CONFLICT", name="CONFLICT")
    FiduciaryAssignment.objects.create(
        assignment_number="282387900794",
        property_unit=conflicting_unit,
        start_date="2026-01-01",
    )

    with pytest.raises(HistoricalImportFinalizationError):
        finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.FAILED
    assert Payment.objects.count() == 0
    assert PropertyUnit.objects.filter(code="RES-101").count() == 0
    assert ImportAppliedRecord.objects.filter(batch=batch).count() == 0


def test_finalize_view_uses_post_and_completes_batch(client, accounting_admin_user):
    batch = prepare_ready_historical_batch(accounting_admin_user)
    client.force_login(accounting_admin_user)
    url = reverse("fiduciary:historical_import_finalize", args=[batch.pk])

    response = client.get(url)
    assert response.status_code == 200
    assert b"Ejecutar importacion definitiva" in response.content

    response = client.post(url, follow=True)

    assert response.status_code == 200
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.COMPLETED


def test_property_unit_history_view_shows_imported_historical_novelties(client, accounting_admin_user):
    batch = prepare_ready_historical_batch(accounting_admin_user)
    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)
    novelty = OperationalNovelty.objects.filter(
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        property_unit__isnull=False,
    ).select_related("property_unit").first()
    assert novelty is not None
    client.force_login(accounting_admin_user)

    response = client.get(reverse("real_estate:property_unit_history", args=[novelty.property_unit_id]))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Novedades" in content
    assert novelty.summary in content
    assert novelty.get_origin_display() in content


def test_uploaded_analysis_stores_file_path(accounting_admin_user):
    batch = prepare_ready_historical_batch(accounting_admin_user)
    imported_file = batch.files.get()

    assert imported_file.stored_path
    assert (settings.MEDIA_ROOT / imported_file.stored_path).exists()


def test_historical_novelty_summary_keeps_descriptive_text_from_name_columns():
    summary, detail = _summary_detail_from_cells(
        [
            {"header": "NOMBRE CLIENTE", "value": "*TERMIN.SIN ABONOS", "formula": False},
            {"header": "OBSERVACIONES", "value": "Detalle historico", "formula": False},
            {"header": "RECIBO FIDUCIA MAR/2026", "value": "100000", "formula": False},
        ]
    )

    assert summary == "*TERMIN.SIN ABONOS"
    assert detail == "Detalle historico"
