from decimal import Decimal
import uuid

import pytest
from django.contrib.admin.models import LogEntry
from django.urls import reverse
from django.utils import timezone

from fiduciary.models import (
    Client,
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportedHistoricalObservation,
    OperationalNovelty,
    Payment,
    UnitOwnership,
)
from fiduciary.imports.reversion import revert_import_batch
from real_estate.models import Project, PropertyUnit


def _assignment(project, client, code="101", number="EF-CRUD"):
    unit = PropertyUnit.objects.create(project=project, code=code, name=code)
    assignment = FiduciaryAssignment.objects.create(
        assignment_number=number,
        property_unit=unit,
        start_date=timezone.localdate(),
        last_change_reason="test",
    )
    UnitOwnership.objects.create(
        client=client,
        property_unit=unit,
        is_primary=True,
        start_date=timezone.localdate(),
        last_change_reason="test",
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=client,
        is_primary=True,
        start_date=timezone.localdate(),
        last_change_reason="test",
    )
    return unit, assignment


def _source_file(user, sha):
    batch = ImportBatch.objects.create(
        initiated_by=user,
        import_type=ImportBatch.ImportType.REPORTS,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.COMPLETED,
        total_files=1,
    )
    return ImportedFile.objects.create(
        batch=batch,
        original_name=f"{sha}.xlsx",
        extension=".xlsx",
        size_bytes=1,
        sha256=sha * 64,
        file_type=ImportedFile.FileType.REPORT,
    )


@pytest.fixture
def crud_project():
    return Project.objects.create(code=uuid.uuid4().hex[:8], name="Proyecto CRUD")


@pytest.fixture
def accounting_session(client, accounting_admin_user):
    client.force_login(accounting_admin_user)
    return client


@pytest.mark.django_db
def test_accounting_can_edit_and_delete_imported_observation_with_reason(accounting_session, accounting_admin_user, crud_project):
    client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="901",
        first_names="Ana",
        last_names_or_company="Prueba",
        phone="300",
    )
    unit, assignment = _assignment(crud_project, client)
    other_unit, other_assignment = _assignment(crud_project, client, code="999", number="EF-OTHER-OBS")
    observation = ImportedHistoricalObservation.objects.create(
        project=crud_project,
        property_unit=unit,
        client=client,
        assignment=assignment,
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        summary="Anterior",
        detail="Detalle anterior",
        dedupe_key=uuid.uuid4().hex,
        imported_by=accounting_admin_user,
    )

    response = accounting_session.post(
        reverse("fiduciary:observation_update", args=[observation.pk]),
        {
            "project": crud_project.pk,
            "property_unit": other_unit.pk,
            "assignment": other_assignment.pk,
            "summary": "Nuevo",
            "detail": "Detalle nuevo",
            "change_reason": "Correccion contable",
        },
    )

    assert response.status_code == 302
    observation.refresh_from_db()
    assert observation.origin == ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION
    assert observation.summary == "Nuevo"
    assert observation.property_unit_id == unit.pk
    assert observation.assignment_id == assignment.pk
    assert LogEntry.objects.filter(change_message__icontains="MODIFICAR_OBSERVACION").exists()

    response = accounting_session.post(
        reverse("fiduciary:observation_delete", args=[observation.pk]),
        {"confirm": "yes", "change_reason": "Dato duplicado"},
    )

    assert response.status_code == 302
    assert not ImportedHistoricalObservation.objects.filter(pk=observation.pk).exists()
    assert LogEntry.objects.filter(change_message__icontains="ELIMINAR_OBSERVACION").exists()


@pytest.mark.django_db
def test_accounting_can_edit_and_delete_payment_without_deleting_assignment(accounting_session, accounting_admin_user, crud_project):
    client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="902",
        first_names="Pago",
        last_names_or_company="Prueba",
        phone="300",
    )
    _, assignment = _assignment(crud_project, client, code="102", number="EF-PAY-CRUD")
    source_file = _source_file(accounting_admin_user, "b")
    payment = Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=timezone.localdate(),
        amount=Decimal("1000"),
        concept="Inicial",
        destination=Payment.Destination.CONSTRUCTORA,
        movement_type=Payment.MovementType.ADDITION,
        source_file=source_file,
        source_sheet="Manual",
        source_row=1,
    )

    response = accounting_session.post(
        reverse("fiduciary:payment_update", args=[payment.pk]),
        {
            "date_precision": Payment.DatePrecision.EXACT,
            "exact_date": timezone.localdate().isoformat(),
            "amount": "1500",
            "concept": "Corregido",
            "destination": Payment.Destination.FIDUCIARIA,
            "change_reason": "Correccion recibo",
        },
    )

    assert response.status_code == 302
    payment.refresh_from_db()
    assert payment.amount == Decimal("1500.00")
    assert payment.destination == Payment.Destination.FIDUCIARIA
    assert LogEntry.objects.filter(change_message__icontains="MODIFICAR_PAGO").exists()

    response = accounting_session.post(
        reverse("fiduciary:payment_delete", args=[payment.pk]),
        {"confirm": "yes", "change_reason": "Pago errado"},
    )

    assert response.status_code == 302
    assert not Payment.objects.filter(pk=payment.pk).exists()
    assert FiduciaryAssignment.objects.filter(pk=assignment.pk).exists()
    assert LogEntry.objects.filter(change_message__icontains="ELIMINAR_PAGO").exists()


@pytest.mark.django_db
def test_accounting_can_edit_novelty_with_full_panel_but_cannot_delete_it(accounting_session, accounting_admin_user, crud_project):
    client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="903",
        first_names="Novedad",
        last_names_or_company="Prueba",
        phone="300",
    )
    unit, _ = _assignment(crud_project, client, code="103", number="EF-NOV-CRUD")
    novelty = OperationalNovelty.objects.create(
        project=crud_project,
        property_unit=unit,
        novelty_type=OperationalNovelty.NoveltyType.OTHER,
        other_type="OTRO",
        origin=OperationalNovelty.Origin.MANUAL,
        status=OperationalNovelty.Status.DESCRIPTIVE,
        summary="Anterior",
        detail="Detalle",
        created_by=accounting_admin_user,
    )

    detail_response = accounting_session.get(reverse("fiduciary:novelty_detail", args=[novelty.pk]))
    assert detail_response.status_code == 200
    assert "Editar" in detail_response.content.decode()
    assert "Eliminar" not in detail_response.content.decode()

    edit_response = accounting_session.get(reverse("fiduciary:novelty_update", args=[novelty.pk]))
    edit_content = edit_response.content.decode()
    assert edit_response.status_code == 200
    assert "Registre eventos operativos" in edit_content
    assert "Unidad" in edit_content
    assert "Encargo fiduciario" in edit_content
    assert "Tipo de novedad" in edit_content

    other_client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="9039",
        first_names="Otra",
        last_names_or_company="Persona",
        phone="300",
    )
    other_unit, other_assignment = _assignment(crud_project, other_client, code="103B", number="EF-NOV-OTHER")
    response = accounting_session.post(
        reverse("fiduciary:novelty_update", args=[novelty.pk]),
        {
            "project": crud_project.pk,
            "property_unit": other_unit.pk,
            "current_assignment": other_assignment.pk,
            "effective_date": "",
            "summary": "Nuevo",
            "detail": "Detalle nuevo",
            "other_type": "OTRO",
            "change_reason": "Correccion",
        },
    )

    assert response.status_code == 302
    novelty.refresh_from_db()
    assert novelty.summary == "Nuevo"
    assert novelty.detail == "Detalle nuevo"
    assert novelty.property_unit_id == unit.pk
    assert LogEntry.objects.filter(change_message__icontains="MODIFICAR_NOVEDAD").exists()

    delete_get = accounting_session.get(reverse("fiduciary:novelty_delete", args=[novelty.pk]))
    assert delete_get.status_code == 403

    delete_response = accounting_session.post(
        reverse("fiduciary:novelty_delete", args=[novelty.pk]),
        {"confirm": "yes", "change_reason": "Correccion de registro"},
    )

    assert delete_response.status_code == 403
    assert OperationalNovelty.objects.filter(pk=novelty.pk).exists()
    assert not LogEntry.objects.filter(change_message__icontains="ELIMINAR_NOVEDAD").exists()


@pytest.mark.django_db
def test_novelty_edit_panel_preserves_type_specific_context(accounting_session, accounting_admin_user, crud_project):
    current_client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="913",
        first_names="Actual",
        last_names_or_company="Novedad",
        phone="300",
    )
    new_client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="914",
        first_names="Nuevo",
        last_names_or_company="Novedad",
        phone="300",
    )
    secondary_client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="915",
        first_names="Secundario",
        last_names_or_company="Novedad",
        phone="300",
    )
    unit, assignment = _assignment(crud_project, current_client, code="113", number="EF-NOV-TYPES")
    UnitOwnership.objects.create(
        client=secondary_client,
        property_unit=unit,
        is_primary=False,
        start_date=timezone.localdate(),
        last_change_reason="test",
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=secondary_client,
        is_primary=False,
        start_date=timezone.localdate(),
        last_change_reason="test",
    )
    new_assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-NOV-TYPES-NEW",
        property_unit=unit,
        is_active=False,
        start_date=timezone.localdate(),
        end_date=timezone.localdate(),
        last_change_reason="test",
    )

    novelties = [
        OperationalNovelty.objects.create(
            project=crud_project,
            property_unit=unit,
            novelty_type=OperationalNovelty.NoveltyType.CESSION,
            origin=OperationalNovelty.Origin.MANUAL,
            status=OperationalNovelty.Status.APPLIED,
            previous_client=current_client,
            new_client=new_client,
            previous_assignment=assignment,
            new_assignment=new_assignment,
            summary="Cesion",
            detail="Cesion",
            created_by=accounting_admin_user,
        ),
        OperationalNovelty.objects.create(
            project=crud_project,
            property_unit=unit,
            novelty_type=OperationalNovelty.NoveltyType.OTHER,
            other_type="INCLUSION",
            origin=OperationalNovelty.Origin.MANUAL,
            status=OperationalNovelty.Status.APPLIED,
            previous_assignment=assignment,
            new_assignment=assignment,
            new_client=secondary_client,
            summary="Inclusion",
            detail="Inclusion",
            created_by=accounting_admin_user,
        ),
        OperationalNovelty.objects.create(
            project=crud_project,
            property_unit=unit,
            novelty_type=OperationalNovelty.NoveltyType.ADMINISTRATIVE_CORRECTION,
            origin=OperationalNovelty.Origin.MANUAL,
            status=OperationalNovelty.Status.DESCRIPTIVE,
            previous_client=current_client,
            previous_assignment=assignment,
            summary="Correccion",
            detail="Correccion",
            created_by=accounting_admin_user,
        ),
    ]

    for novelty in novelties:
        response = accounting_session.get(reverse("fiduciary:novelty_update", args=[novelty.pk]))
        content = response.content.decode()
        assert response.status_code == 200
        assert "Tipo de novedad" in content
        assert "Unidad" in content
        assert "Encargo fiduciario" in content
        assert "Solo lectura durante la edicion." in content
        assert unit.code in content
        assert assignment.assignment_number in content or new_assignment.assignment_number in content

    cession_content = accounting_session.get(reverse("fiduciary:novelty_update", args=[novelties[0].pk])).content.decode()
    assert "Nuevo titular principal" in cession_content
    assert "Nuevo numero de encargo" in cession_content

    inclusion_content = accounting_session.get(reverse("fiduciary:novelty_update", args=[novelties[1].pk])).content.decode()
    assert "Clientes secundarios asociados" in inclusion_content

    response = accounting_session.post(
        reverse("fiduciary:novelty_update", args=[novelties[1].pk]),
        {"summary": "Inclusion corregida", "detail": "Detalle corregido", "change_reason": "Correccion descriptiva"},
    )
    assert response.status_code == 302
    novelties[1].refresh_from_db()
    assert novelties[1].novelty_type == OperationalNovelty.NoveltyType.OTHER
    assert novelties[1].other_type == "INCLUSION"
    assert novelties[1].property_unit_id == unit.pk
    assert novelties[1].previous_assignment_id == assignment.pk


@pytest.mark.django_db
def test_revert_daily_report_batch_removes_only_created_payment(accounting_admin_user, crud_project):
    client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="904",
        first_names="Reporte",
        last_names_or_company="Prueba",
        phone="300",
    )
    _, assignment = _assignment(crud_project, client, code="104", number="EF-REPORT-UNDO")
    source_file = _source_file(accounting_admin_user, "c")
    batch = source_file.batch
    payment = Payment.objects.create(
        assignment=assignment,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=timezone.localdate(),
        amount=Decimal("2000"),
        concept="Reporte",
        destination=Payment.Destination.FIDUCIARIA,
        movement_type=Payment.MovementType.ADDITION,
        source_file=source_file,
        source_sheet="Reporte",
        source_row=1,
    )
    ImportAppliedRecord.objects.create(
        batch=batch,
        imported_file=source_file,
        entity_kind=ImportAppliedRecord.EntityKind.PAYMENT,
        entity_id=str(payment.pk),
        action=ImportAppliedRecord.Action.CREATED,
        summary="Pago reporte",
    )

    summary = revert_import_batch(batch=batch, user=accounting_admin_user, reason="Reporte errado")

    batch.refresh_from_db()
    assert summary.payments == 1
    assert batch.status == ImportBatch.Status.REVERTED
    assert not Payment.objects.filter(pk=payment.pk).exists()
    assert Client.objects.filter(pk=client.pk).exists()
    assert FiduciaryAssignment.objects.filter(pk=assignment.pk).exists()
