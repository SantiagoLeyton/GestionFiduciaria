from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError

from .models import Client, Payment


@dataclass
class ImportedClientResult:
    status: str
    client: Client | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class PaymentCreationResult:
    status: str
    payment: Payment | None = None
    errors: list[str] = field(default_factory=list)


def create_imported_client(
    *,
    full_name: str,
    document_type: str | None = None,
    document_number: str | None = None,
    source_origin: str = Client.SourceOrigin.HISTORICAL_IMPORT,
    incomplete_reason: str = "Registro creado automaticamente desde importacion con informacion incompleta.",
    phone: str = "",
    email: str = "",
) -> ImportedClientResult:
    name = (full_name or "").strip()
    doc_type = document_type or Client.DocumentType.UNKNOWN
    doc_number = (document_number or "").strip() or None
    phone = (phone or "").strip()
    email = (email or "").strip()

    if not name:
        return ImportedClientResult(status="invalid", errors=["Registre el nombre del cliente importado."])

    if doc_number:
        existing = Client.objects.filter(document_type=doc_type, document_number=doc_number).first()
        if existing:
            return ImportedClientResult(status="existing", client=existing)

    is_incomplete = not doc_number or doc_type == Client.DocumentType.UNKNOWN or not (phone or email)
    client = Client(
        document_type=doc_type,
        document_number=doc_number,
        first_names="",
        last_names_or_company=name,
        phone=phone,
        email=email,
        information_status=(
            Client.InformationStatus.INCOMPLETE if is_incomplete else Client.InformationStatus.COMPLETE
        ),
        incomplete_reason=incomplete_reason if is_incomplete else "",
        source_origin=source_origin,
    )

    try:
        client.full_clean()
        client.save()
    except (ValidationError, IntegrityError) as exc:
        return ImportedClientResult(status="invalid", errors=[str(exc)])
    return ImportedClientResult(status="created", client=client)


def create_payment(
    *,
    assignment,
    amount,
    movement_type,
    source_file,
    source_sheet,
    source_row,
    date_precision,
    exact_date=None,
    period_year=None,
    period_month=None,
    concept=None,
    source_column=None,
    source_header=None,
    source_had_formula=False,
) -> PaymentCreationResult:
    try:
        normalized_amount = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return PaymentCreationResult(status="invalid", errors=["El valor del pago no es valido."])

    if normalized_amount <= 0:
        return PaymentCreationResult(status="invalid", errors=["El valor del pago debe ser mayor que cero."])

    duplicate_query = Payment.objects.filter(assignment=assignment, amount=normalized_amount)
    if date_precision == Payment.DatePrecision.EXACT:
        duplicate_query = duplicate_query.filter(date_precision=date_precision, exact_date=exact_date)
    elif date_precision == Payment.DatePrecision.MONTH:
        duplicate_query = duplicate_query.filter(
            date_precision=date_precision,
            period_year=period_year,
            period_month=period_month,
        )
    else:
        return PaymentCreationResult(status="invalid", errors=["La precision de fecha no es valida."])

    if duplicate_query.exists():
        return PaymentCreationResult(status="duplicate")

    payment = Payment(
        assignment=assignment,
        exact_date=exact_date,
        period_year=period_year,
        period_month=period_month,
        date_precision=date_precision,
        amount=normalized_amount,
        concept=(concept or "").strip() or None,
        movement_type=movement_type,
        source_file=source_file,
        source_sheet=source_sheet,
        source_row=source_row,
        source_column=source_column,
        source_header=source_header,
        source_had_formula=source_had_formula,
    )

    try:
        payment.full_clean()
        payment.save()
    except IntegrityError:
        return PaymentCreationResult(status="duplicate")
    except ValidationError as exc:
        return PaymentCreationResult(status="invalid", errors=[str(exc)])
    return PaymentCreationResult(status="created", payment=payment)
