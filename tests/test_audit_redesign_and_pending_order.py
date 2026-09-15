import pytest
from django.contrib.admin.models import LogEntry
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from fiduciary.admin_cleanup import delete_grouping_type_if_unused, delete_property_unit
from fiduciary.imports.audit import AUDIT_SOURCE_COLUMN
from fiduciary.models import DetectedStructureElement, ImportAppliedRecord, ImportBatch, ImportedFile, ImportResolution
from tests.test_historical_import_finalization import (
    _analyze_and_finalize_incremental_workbook,
    build_incremental_payments_workbook,
)
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup


def _batch(user, *, summary=""):
    return ImportBatch.objects.create(
        initiated_by=user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.AWAITING_RESOLUTION,
        total_files=1,
        summary=summary,
    )


def _file(batch):
    return ImportedFile.objects.create(
        batch=batch,
        original_name="historico.xlsx",
        extension=".xlsx",
        size_bytes=1,
        sha256="d" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )


def _pending(batch, kind, value):
    element = DetectedStructureElement.objects.create(
        batch=batch,
        raw_value=value,
        normalized_value=value.casefold(),
        inferred_kind=kind,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    ImportResolution.objects.create(detected_element=element, target_kind=kind)
    return element


@pytest.fixture
def accounting_session(client, accounting_admin_user):
    client.force_login(accounting_admin_user)
    return client


@pytest.mark.django_db
def test_audit_list_shows_only_import_audit_events_and_detail_hides_raw_json(accounting_session, accounting_admin_user):
    batch = _batch(accounting_admin_user, summary='{"valid_rows":270,"ignored_rows":9694}')
    imported_file = _file(batch)
    ImportAppliedRecord.objects.create(
        batch=batch,
        imported_file=imported_file,
        entity_kind=ImportAppliedRecord.EntityKind.CLIENT,
        entity_id=10,
        action=ImportAppliedRecord.Action.CREATED,
        summary="Cliente interno que no debe aparecer",
    )
    ImportAppliedRecord.objects.create(
        batch=batch,
        imported_file=imported_file,
        entity_kind=ImportAppliedRecord.EntityKind.PROJECT,
        action=ImportAppliedRecord.Action.CREATED,
        source_column=AUDIT_SOURCE_COLUMN,
        summary="\n".join(
            [
                "Accion: Importado",
                "Entidad: Libro historico",
                "Archivo: historico.xlsx",
                "Clientes creados: 10",
            ]
        ),
    )
    audit = AuditEvent.objects.create(
        user=accounting_admin_user,
        action="Importado",
        entity="Libro historico",
        description="Importacion historica definitiva completada.",
        context={"Archivo": "historico.xlsx", "Lote": batch.pk},
        summary={"Clientes creados": "10"},
    )

    content = accounting_session.get(reverse("fiduciary:audit_list")).content.decode()
    detail = accounting_session.get(reverse("fiduciary:audit_detail", args=[audit.pk])).content.decode()

    assert "Libro historico" in content
    assert "Cliente interno que no debe aparecer" not in content
    assert "Clientes creados" in detail
    assert "10" in detail
    assert '{"valid_rows"' not in detail


@pytest.mark.django_db
def test_historical_import_creates_single_visible_audit_event(tmp_path, accounting_admin_user):
    project = Project.objects.create(code="Incremental", name="Incremental")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    path = build_incremental_payments_workbook(tmp_path / "LIBRO_AuditImport.xlsx", payment_count=3)

    batch, _ = _analyze_and_finalize_incremental_workbook(path, accounting_admin_user, project, grouping_type)

    audit_records = AuditEvent.objects.filter(action="Importado", entity="Libro historico", context__Lote=str(batch.pk))
    assert audit_records.count() == 1
    assert ImportAppliedRecord.objects.filter(batch=batch).exclude(source_column=AUDIT_SOURCE_COLUMN).count() > 1
    assert audit_records.get().summary["Pagos creados"] == "3"


@pytest.mark.django_db
def test_pending_resolution_order_and_dependency_backend(accounting_session, accounting_admin_user):
    batch = _batch(accounting_admin_user)
    project = _pending(batch, DetectedStructureElement.InferredKind.PROJECT, "Montecielo")
    grouping_type = _pending(batch, DetectedStructureElement.InferredKind.GROUPING_TYPE, "TORRE")
    group = _pending(batch, DetectedStructureElement.InferredKind.STRUCTURAL_GROUP, "T1")

    response = accounting_session.get(reverse("fiduciary:historical_import_pending", args=[batch.pk]))
    content = response.content.decode()

    assert content.index("Montecielo") < content.index("TORRE") < content.index("T1")
    assert "Resuelva primero el proyecto" in content
    blocked_response = accounting_session.get(reverse("fiduciary:historical_import_resolve", args=[batch.pk, grouping_type.pk]), follow=True)
    assert "Resuelva primero el proyecto" in blocked_response.content.decode()

    project.status = DetectedStructureElement.Status.RESOLVED
    project.resolution.status = ImportResolution.Status.APPLIED
    project.resolution.resolved_by = accounting_admin_user
    project.resolution.resolved_at = timezone.now()
    project.resolution.save()
    project.save()

    response = accounting_session.get(reverse("fiduciary:historical_import_pending", args=[batch.pk]))
    content = response.content.decode()
    assert "Resuelva primero el proyecto" not in content
    assert "Resuelva primero el tipo de agrupacion" in content

    blocked_group = accounting_session.get(reverse("fiduciary:historical_import_resolve_group", args=[batch.pk, group.pk]), follow=True)
    assert "Resuelva primero el tipo de agrupacion" in blocked_group.content.decode()


@pytest.mark.django_db
def test_real_estate_manual_create_update_delete_actions_are_audited(accounting_session, accounting_admin_user):
    start = AuditEvent.objects.count()

    response = accounting_session.post(
        reverse("real_estate:project_create"),
        {"code": "AUD", "name": "Auditoria", "description": "", "is_active": "on"},
    )
    assert response.status_code == 302
    project = Project.objects.get(code="AUD")

    response = accounting_session.post(
        reverse("real_estate:project_update", args=[project.pk]),
        {
            "code": "AUD",
            "name": "Auditoria Actualizada",
            "description": "",
            "is_active": "on",
            "change_reason": "Prueba de auditoria",
        },
    )
    assert response.status_code == 302

    grouping_type = GroupingType.objects.create(code="SINUSO", name="Sin uso")
    delete_grouping_type_if_unused(grouping_type, user=accounting_admin_user, reason="Prueba de auditoria")

    assert AuditEvent.objects.count() == start + 3
    content = accounting_session.get(reverse("fiduciary:audit_list")).content.decode()
    assert "Creado" in content and "Proyecto" in content
    assert "Modificado" in content and "Proyecto" in content
    assert "ELIMINAR_TIPO_AGRUPACION" in content


@pytest.mark.django_db
def test_property_unit_delete_is_single_audit_event_and_clients_are_not_touched(accounting_admin_user):
    project = Project.objects.create(code="PU", name="Proyecto Unidad")
    grouping_type = GroupingType.objects.create(code="TORPU", name="Torre")
    group = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(
        project=project,
        structural_group=group,
        code="101",
        name="101",
        area="55.20",
        property_value="100000000",
    )
    start = AuditEvent.objects.count()

    delete_property_unit(unit, user=accounting_admin_user, reason="Carga equivocada")

    assert not PropertyUnit.objects.filter(pk=unit.pk).exists()
    assert AuditEvent.objects.count() == start + 1
    assert AuditEvent.objects.filter(description__icontains="ELIMINAR_UNIDAD").exists()


@pytest.mark.django_db
def test_audit_list_includes_administrative_log_entries(accounting_session, accounting_admin_user):
    grouping_type = GroupingType.objects.create(code="AUDDEL", name="Auditable")
    delete_grouping_type_if_unused(grouping_type, user=accounting_admin_user, reason="Visible en auditoria")

    content = accounting_session.get(reverse("fiduciary:audit_list")).content.decode()

    assert "ELIMINAR_TIPO_AGRUPACION" in content
    assert "Auditable" in content
    event = AuditEvent.objects.get(description__icontains="ELIMINAR_TIPO_AGRUPACION")
    detail = accounting_session.get(reverse("fiduciary:audit_detail", args=[event.pk])).content.decode()
    assert "Visible en auditoria" in detail
