from decimal import Decimal
from io import BytesIO

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from fiduciary.forms import ClientForm
from fiduciary.models import (
    Client,
    DetectedStructureElement,
    FiduciaryAssignment,
    ImportBatch,
    ImportedFile,
    ImportNovelty,
    ImportResolution,
    ImportedSheetResult,
    ImportRowIssue,
    Payment,
)
from fiduciary.services import create_imported_client, create_payment
from fiduciary.utils import calculate_sha256
from real_estate.models import Project, PropertyUnit


@pytest.fixture
def import_batch(db, accounting_admin_user):
    return ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
    )


@pytest.fixture
def imported_file(import_batch):
    return ImportedFile.objects.create(
        batch=import_batch,
        original_name="libro.xlsx",
        extension=".xlsx",
        size_bytes=128,
        sha256="a" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )


@pytest.fixture
def assignment(db):
    project = Project.objects.create(code="IMP", name="Proyecto Importacion")
    unit = PropertyUnit.objects.create(project=project, code="101", name="Unidad 101")
    return FiduciaryAssignment.objects.create(
        assignment_number="EF-IMP-001",
        property_unit=unit,
        start_date="2026-01-01",
        last_change_reason="Prueba",
    )


def create_source_file(batch, suffix):
    return ImportedFile.objects.create(
        batch=batch,
        original_name=f"archivo-{suffix}.xlsx",
        extension=".xlsx",
        size_bytes=128,
        sha256=(suffix * 64)[:64],
        file_type=ImportedFile.FileType.HISTORICAL,
    )


@pytest.mark.django_db
def test_manual_client_form_still_requires_document():
    form = ClientForm(
        data={
            "document_type": Client.DocumentType.CITIZENSHIP_ID,
            "document_number": "",
            "first_names": "",
            "last_names_or_company": "Cliente Manual",
            "phone": "300",
            "email": "",
            "address": "",
            "is_active": "on",
        }
    )

    assert not form.is_valid()
    assert "document_number" in form.errors


@pytest.mark.django_db
def test_manual_client_form_does_not_allow_unknown_document_type():
    form = ClientForm(
        data={
            "document_type": Client.DocumentType.UNKNOWN,
            "document_number": "123",
            "first_names": "",
            "last_names_or_company": "Cliente Manual",
            "phone": "300",
            "email": "",
            "address": "",
            "is_active": "on",
        }
    )

    assert not form.is_valid()
    assert "document_type" in form.errors


@pytest.mark.django_db
def test_imported_client_with_document_is_reused_by_document():
    first = create_imported_client(
        full_name="Cliente Importado",
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="123",
        phone="300",
    )
    second = create_imported_client(
        full_name="Otro Nombre",
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="123",
        phone="300",
    )

    assert first.status == "created"
    assert second.status == "existing"
    assert second.client == first.client


@pytest.mark.django_db
def test_imported_client_without_document_is_incomplete_and_unknown():
    result = create_imported_client(full_name="Titular Sin Documento")

    assert result.status == "created"
    assert result.client.document_type == Client.DocumentType.UNKNOWN
    assert result.client.document_number is None
    assert result.client.information_status == Client.InformationStatus.INCOMPLETE
    assert result.client.incomplete_reason


@pytest.mark.django_db
def test_multiple_incomplete_clients_without_document_are_allowed_and_not_merged_by_name():
    first = create_imported_client(full_name="Nombre Repetido")
    second = create_imported_client(full_name="Nombre Repetido")

    assert first.status == "created"
    assert second.status == "created"
    assert first.client.pk != second.client.pk


@pytest.mark.django_db
def test_duplicate_document_is_rejected_by_model_validation():
    Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="999",
        last_names_or_company="Original",
        phone="300",
    )
    duplicate = Client(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="999",
        last_names_or_company="Duplicado",
        phone="300",
    )

    with pytest.raises(ValidationError):
        duplicate.full_clean()


@pytest.mark.django_db
def test_completing_imported_client_respects_document_uniqueness():
    Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="777",
        last_names_or_company="Identificado",
        phone="300",
    )
    result = create_imported_client(full_name="Incompleto")
    client = result.client
    client.document_type = Client.DocumentType.CITIZENSHIP_ID
    client.document_number = "777"
    client.phone = "301"
    client.information_status = Client.InformationStatus.COMPLETE
    client.incomplete_reason = ""

    with pytest.raises(ValidationError):
        client.full_clean()


@pytest.mark.django_db
def test_import_batch_and_related_file_models(import_batch, imported_file):
    sheet = ImportedSheetResult.objects.create(
        imported_file=imported_file,
        sheet_name="VS",
        sheet_index=1,
        visibility=ImportedSheetResult.Visibility.VISIBLE,
        classification=ImportedSheetResult.Classification.PROCESSABLE,
        header_row=4,
        detected_dimension="A1:AW162",
    )
    issue = ImportRowIssue.objects.create(
        imported_file=imported_file,
        sheet_result=sheet,
        row_number=10,
        column_letter="T",
        severity=ImportRowIssue.Severity.WARNING,
        code="FORMULA_VALUE",
        message="Celda relevante con formula y valor calculado disponible.",
    )

    assert import_batch.files.get() == imported_file
    assert imported_file.sheet_results.get() == sheet
    assert issue.message == "Celda relevante con formula y valor calculado disponible."


@pytest.mark.django_db
def test_detected_structure_resolution_and_novelty(import_batch, imported_file, accounting_admin_user, assignment):
    detected = DetectedStructureElement.objects.create(
        batch=import_batch,
        imported_file=imported_file,
        raw_value="Spring Field",
        normalized_value="springfield",
        inferred_kind=DetectedStructureElement.InferredKind.PROJECT,
        confidence=Decimal("0.9100"),
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    )
    resolution = ImportResolution.objects.create(
        detected_element=detected,
        action=ImportResolution.Action.CREATE_NEW,
        target_kind=DetectedStructureElement.InferredKind.PROJECT,
        create_code="SPR",
        create_name="Springfield",
        resolved_by=accounting_admin_user,
    )
    novelty = ImportNovelty.objects.create(
        batch=import_batch,
        imported_file=imported_file,
        assignment=assignment,
        novelty_type=ImportNovelty.NoveltyType.TRANSFER,
        description="Novedad funcional sanitizada.",
    )

    assert resolution.detected_element == detected
    assert novelty.status == ImportNovelty.Status.DETECTED


@pytest.mark.django_db
def test_payment_with_exact_date_is_valid(assignment, imported_file):
    result = create_payment(
        assignment=assignment,
        amount="1000.50",
        movement_type=Payment.MovementType.ADDITION,
        source_file=imported_file,
        source_sheet="Page 1",
        source_row=6,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date="2026-07-22",
        concept="Pago mensual",
    )

    assert result.status == "created"
    assert result.payment.exact_date.isoformat() == "2026-07-22"
    assert result.payment.concept == "Pago mensual"


@pytest.mark.django_db
def test_payment_with_month_period_is_valid(assignment, imported_file):
    result = create_payment(
        assignment=assignment,
        amount="2500",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="VS",
        source_row=5,
        source_column="X",
        source_header="RECIBO FIDUCIA JUL/2026",
        source_had_formula=True,
        date_precision=Payment.DatePrecision.MONTH,
        period_year=2026,
        period_month=7,
    )

    assert result.status == "created"
    assert result.payment.period_year == 2026
    assert result.payment.period_month == 7
    assert result.payment.source_had_formula is True


@pytest.mark.django_db
def test_payment_rejects_invalid_date_modes_and_missing_date(assignment, imported_file):
    both_modes = create_payment(
        assignment=assignment,
        amount="100",
        movement_type=Payment.MovementType.ADDITION,
        source_file=imported_file,
        source_sheet="Page 1",
        source_row=6,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date="2026-07-22",
        period_year=2026,
        period_month=7,
    )
    no_date = create_payment(
        assignment=assignment,
        amount="100",
        movement_type=Payment.MovementType.ADDITION,
        source_file=imported_file,
        source_sheet="Page 1",
        source_row=7,
        date_precision=Payment.DatePrecision.MONTH,
    )

    assert both_modes.status == "invalid"
    assert no_date.status == "invalid"


@pytest.mark.django_db
def test_payment_rejects_month_out_of_range_zero_and_negative(assignment, imported_file):
    bad_month = create_payment(
        assignment=assignment,
        amount="100",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="VS",
        source_row=5,
        date_precision=Payment.DatePrecision.MONTH,
        period_year=2026,
        period_month=13,
    )
    zero = create_payment(
        assignment=assignment,
        amount="0",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="VS",
        source_row=6,
        date_precision=Payment.DatePrecision.MONTH,
        period_year=2026,
        period_month=7,
    )
    negative = create_payment(
        assignment=assignment,
        amount="-1",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="VS",
        source_row=7,
        date_precision=Payment.DatePrecision.MONTH,
        period_year=2026,
        period_month=7,
    )

    assert bad_month.status == "invalid"
    assert zero.status == "invalid"
    assert negative.status == "invalid"


@pytest.mark.django_db
def test_payment_allows_null_concept(assignment, imported_file):
    result = create_payment(
        assignment=assignment,
        amount="100",
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="VS",
        source_row=5,
        date_precision=Payment.DatePrecision.MONTH,
        period_year=2026,
        period_month=7,
    )

    assert result.status == "created"
    assert result.payment.concept is None


@pytest.mark.django_db
def test_payment_duplicate_exact_and_monthly_are_detected(assignment, imported_file):
    exact = {
        "assignment": assignment,
        "amount": "100",
        "movement_type": Payment.MovementType.ADDITION,
        "source_file": imported_file,
        "source_sheet": "Page 1",
        "source_row": 6,
        "date_precision": Payment.DatePrecision.EXACT,
        "exact_date": "2026-07-22",
    }
    monthly = {
        "assignment": assignment,
        "amount": "200",
        "movement_type": Payment.MovementType.HISTORICAL_PAYMENT,
        "source_file": imported_file,
        "source_sheet": "VS",
        "source_row": 5,
        "date_precision": Payment.DatePrecision.MONTH,
        "period_year": 2026,
        "period_month": 7,
    }

    assert create_payment(**exact).status == "created"
    assert create_payment(**exact).status == "duplicate"
    assert create_payment(**monthly).status == "created"
    assert create_payment(**monthly).status == "duplicate"


@pytest.mark.django_db
def test_payment_same_assignment_and_amount_with_different_dates_is_allowed(assignment, imported_file, import_batch):
    second_file = create_source_file(import_batch, "b")
    first = create_payment(
        assignment=assignment,
        amount="100",
        movement_type=Payment.MovementType.ADDITION,
        source_file=imported_file,
        source_sheet="Page 1",
        source_row=6,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date="2026-07-22",
    )
    second = create_payment(
        assignment=assignment,
        amount="100",
        movement_type=Payment.MovementType.ADDITION,
        source_file=second_file,
        source_sheet="Page 1",
        source_row=7,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date="2026-07-23",
    )

    assert first.status == "created"
    assert second.status == "created"


@pytest.mark.django_db(transaction=True)
def test_payment_unique_constraint_protects_exact_duplicates(assignment, imported_file):
    Payment.objects.create(
        assignment=assignment,
        amount=Decimal("100.00"),
        movement_type=Payment.MovementType.ADDITION,
        source_file=imported_file,
        source_sheet="Page 1",
        source_row=6,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date="2026-07-22",
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Payment.objects.bulk_create(
                [
                    Payment(
                        assignment=assignment,
                        amount=Decimal("100.00"),
                        movement_type=Payment.MovementType.ADDITION,
                        source_file=imported_file,
                        source_sheet="Page 1",
                        source_row=7,
                        date_precision=Payment.DatePrecision.EXACT,
                        exact_date="2026-07-22",
                    )
                ]
            )


@pytest.mark.django_db(transaction=True)
def test_payment_unique_constraint_protects_monthly_duplicates(assignment, imported_file):
    Payment.objects.create(
        assignment=assignment,
        amount=Decimal("100.00"),
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="VS",
        source_row=5,
        date_precision=Payment.DatePrecision.MONTH,
        period_year=2026,
        period_month=7,
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Payment.objects.bulk_create(
                [
                    Payment(
                        assignment=assignment,
                        amount=Decimal("100.00"),
                        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
                        source_file=imported_file,
                        source_sheet="VS",
                        source_row=6,
                        date_precision=Payment.DatePrecision.MONTH,
                        period_year=2026,
                        period_month=7,
                    )
                ]
            )


def test_sha256_same_content_produces_same_hash_and_preserves_stream_position():
    first = BytesIO(b"contenido")
    second = BytesIO(b"contenido")
    first.seek(3)

    assert calculate_sha256(first, chunk_size=3) == calculate_sha256(second, chunk_size=2)
    assert first.tell() == 3


def test_sha256_different_content_produces_different_hash():
    assert calculate_sha256(BytesIO(b"uno")) != calculate_sha256(BytesIO(b"dos"))
