import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from django.db.models import Prefetch

from real_estate.models import Project, PropertyUnit, StructuralGroup
from real_estate.querysets import with_natural_unit_order

from .models import (
    Client,
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportedFile,
    ImportedHistoricalObservation,
    OperationalNovelty,
    Payment,
)


MAIN_HISTORICAL_HEADERS = [
    "ENCARGO FIDUCIARIO",
    "NUEVOS ENCARGO FIDUCIARIO",
    "APTO ",
    "AREA",
    "CEDULA CLIENTE",
    "NOMBRE CLIENTE",
    "NOMBRE CLIENTE2",
    "ENTIDAD FINANCIERA",
    "VALOR INMUEBLE",
    "RECIBOS",
    "RECIBOS FIDUBOGOTA",
    "FECHA",
    "RECIBIDO",
    "CESIONES/TRASLADOS",
    "FECHA PAGO CREDITO",
    "FECHA PAGO SUBSIDIO",
]

MAIN_TRAILING_HEADERS = [
    "ABONOS CR CONSTRUCTOR",
    "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION",
    "DESEMBOLSO SUBSIDIOS GOBIERNO",
    "AJUSTE",
    "TOTAL RECIBIDO",
    "SALDO POR COBRAR",
    "RECURSOS PROPIOS",
    "CREDITO BANCARIO",
    "CAJA HONOR",
    "SUBSIDIOS MCY",
    "SUBSIDIOS CAJA COMP",
    "ENTIDAD",
    "FECHA CONTRATO DE ADHESION",
    "FECHA DE PROMESA",
    "ENTREGA REAL",
    "MATRIC",
    "FECHA ESC",
    "ESC",
    "NOT",
    "FECHA C/TRADIC",
    "FE",
    "INTERESES",
    "VALOR",
    "TELEFONO",
    "E-MAIL",
    "CONTACTO",
    "OBSERVACIONES",
]

GREEN_HEADER_LABELS = {
    "FECHA PAGO CREDITO",
    "FECHA PAGO SUBSIDIO",
    "ABONOS CR CONSTRUCTOR",
    "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION",
    "DESEMBOLSO SUBSIDIOS GOBIERNO",
}

BLUE_HEADER_LABELS = {
    "SUBSIDIOS MCY",
    "SUBSIDIOS CAJA COMP",
    "ENTIDAD",
    "MATRIC",
    "FECHA ESC",
    "ESC",
    "NOT",
    "FECHA C/TRADIC",
    "FE",
}

YELLOW_HEADER_LABELS = {"ENTREGA REAL"}
HISTORICAL_SECTION_TITLE = "NOVEDADES / OBSERVACIONES"

REFERENCE_COLUMN_WIDTHS = {
    1: 17.42578125,
    2: 17.85546875,
    3: 13.0,
    4: 8.28515625,
    5: 7.7109375,
    6: 6.7109375,
    7: 16.42578125,
    8: 31.0,
    9: 23.85546875,
    10: 19.42578125,
    11: 16.0,
    12: 15.0,
    13: 11.42578125,
    14: 13.0,
    15: 13.0,
    16: 14.140625,
}

REFERENCE_TRAILING_WIDTHS = {
    "ABONOS CR CONSTRUCTOR": 12.42578125,
    "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION": 15.5703125,
    "DESEMBOLSO SUBSIDIOS GOBIERNO": 15.28515625,
    "AJUSTE": 14.140625,
    "TOTAL RECIBIDO": 15.28515625,
    "SALDO POR COBRAR": 11.5703125,
    "RECURSOS PROPIOS": 13.0,
    "CREDITO BANCARIO": 13.0,
    "CAJA HONOR": 13.0,
    "SUBSIDIOS MCY": 13.0,
    "SUBSIDIOS CAJA COMP": 13.0,
    "ENTIDAD": 13.0,
    "FECHA CONTRATO DE ADHESION": 13.0,
    "FECHA DE PROMESA": 13.0,
    "ENTREGA REAL": 13.0,
    "MATRIC": 8.140625,
    "FECHA ESC": 11.42578125,
    "ESC": 11.42578125,
    "NOT": 11.42578125,
    "FECHA C/TRADIC": 11.42578125,
    "FE": 11.42578125,
    "INTERESES": 11.42578125,
    "VALOR": 11.42578125,
    "TELEFONO": 13.0,
    "E-MAIL": 20.0,
    "CONTACTO": 24.0,
    "OBSERVACIONES": 105.140625,
}

HEADER_ROW_HEIGHT = 56.25
DATA_ROW_HEIGHT = 12.75
SUMMARY_ROW_HEIGHT = 13.15
NOVELTIES_TITLE_HEIGHT = 15.0
NOVELTY_ROW_HEIGHT = 13.7

INTERNAL_EXPORT_METADATA_PATTERNS = [
    re.compile(r"(?:^|\|\s*)Creado desde importacion historica\.?", flags=re.IGNORECASE),
    re.compile(
        r"(?:^|\|\s*)Encargo historico reconstruido desde seccion NOVEDADES\.?",
        flags=re.IGNORECASE,
    ),
]

MONTH_NAMES = {
    1: "ENE",
    2: "FEB",
    3: "MAR",
    4: "ABR",
    5: "MAY",
    6: "JUN",
    7: "JUL",
    8: "AGO",
    9: "SEP",
    10: "OCT",
    11: "NOV",
    12: "DIC",
}

# Separates payments with real imported receipts from later payments that do not
# have a receipt number in Gestion Fiduciaria.
HISTORICAL_RECEIPT_SEPARATOR = " || "


@dataclass(frozen=True)
class ExportedWorkbook:
    filename: str
    content: bytes


@dataclass(frozen=True)
class PaymentExportItem:
    payment: Payment
    date_text: str
    receipt: str
    amount_text: str
    category: str
    has_receipt: bool
    period_key: tuple[int, int] | None
    destination: str | None
    is_historical: bool
    is_report_payment: bool


@dataclass(frozen=True)
class HistoricalExportItem:
    sort_date: date | None
    historical_year: int | None
    historical_month: int | None
    source_order: int
    source_row: int | None
    pk: int
    kind: str
    obj: object


@dataclass(frozen=True)
class ClientNoveltyExport:
    unit: PropertyUnit
    client: Client | None
    assignment: FiduciaryAssignment | None
    novelties: tuple[OperationalNovelty, ...]


def export_historical_workbook(project: Project) -> ExportedWorkbook:
    workbook = _build_xlsx(project, _project_sheet_payloads(project))
    today = date.today().isoformat()
    return ExportedWorkbook(
        filename=f"LIBRO_{_safe_filename(project.code or project.name)}_{today}.xlsx",
        content=workbook,
    )


def _project_sheet_payloads(project: Project) -> list[tuple[str, dict]]:
    units = list(_project_units(project))
    groups = {
        group.pk: group
        for group in StructuralGroup.objects.filter(project=project).select_related("grouping_type").order_by(
            "grouping_type__name", "code", "name"
        )
    }
    units_by_group: dict[int | None, list[PropertyUnit]] = {}
    for unit in units:
        units_by_group.setdefault(unit.structural_group_id, []).append(unit)

    payloads = []
    used_sheet_names: set[str] = set()
    for group_id, group_units in units_by_group.items():
        group = groups.get(group_id)
        title = _sheet_name(group.code or group.name if group else "UNIDADES", used_sheet_names)
        month_keys = _fiduciary_historical_month_keys_for_units(group_units)
        main_headers = [
            *MAIN_HISTORICAL_HEADERS,
            *[_fiduciary_received_header(year, month) for year, month in month_keys],
            *MAIN_TRAILING_HEADERS,
        ]
        main_rows = []
        for unit in group_units:
            assignment = _current_assignment(list(unit.fiduciary_assignments.all()))
            if assignment:
                row_number = 5 + len(main_rows)
                main_rows.append(_assignment_row(unit, assignment, main_headers, month_keys, row_number=row_number))
            else:
                main_rows.append(_empty_unit_row(unit, main_headers))
        payloads.append(
            (
                title,
                {
                    "main_headers": main_headers,
                    "main_rows": main_rows,
                    "novelty_rows": _novelty_rows_for_units(group_units, main_headers, month_keys, len(main_rows)),
                },
            )
        )
    if not payloads:
        current = date.today()
        headers = [
            *MAIN_HISTORICAL_HEADERS,
            *MAIN_TRAILING_HEADERS,
        ]
        payloads.append(
            (
                "SIN DATOS",
                {
                    "main_headers": headers,
                    "main_rows": [],
                    "novelty_rows": [],
                },
            )
        )
    return payloads


def _project_units(project: Project):
    queryset = (
        PropertyUnit.objects.filter(project=project)
        .select_related("project", "structural_group", "structural_group__grouping_type")
        .prefetch_related(
            Prefetch(
                "fiduciary_assignments",
                queryset=FiduciaryAssignment.objects.prefetch_related(
                    Prefetch(
                        "holders",
                        queryset=FiduciaryAssignmentHolder.objects.select_related("client").order_by(
                            "-is_primary", "-is_active", "start_date", "pk"
                        ),
                    ),
                    Prefetch(
                        "payments",
                        queryset=Payment.objects.select_related("source_file").order_by(
                            "exact_date", "period_year", "period_month", "pk"
                        ),
                    ),
                    Prefetch(
                        "historical_observations",
                        queryset=ImportedHistoricalObservation.objects.order_by(
                            "historical_year", "historical_month", "source_order", "source_row", "pk"
                        ),
                    ),
                ).order_by("start_date", "pk"),
            ),
            Prefetch(
        "historical_observations",
        queryset=ImportedHistoricalObservation.objects.select_related("assignment", "client", "operational_novelty").order_by(
            "historical_year", "historical_month", "source_order", "source_row", "pk"
        ),
            ),
            Prefetch(
                "operational_novelties",
                queryset=OperationalNovelty.objects.select_related(
                    "previous_client",
                    "new_client",
                    "historical_client",
                    "previous_assignment",
                    "new_assignment",
                    "historical_assignment",
                    "source_observation",
                ).order_by("effective_date", "historical_year", "historical_month", "source_row", "pk"),
            ),
        )
    )
    return with_natural_unit_order(queryset)


def _fiduciary_historical_month_keys_for_units(units: list[PropertyUnit]) -> list[tuple[int, int]]:
    keys = set()
    for unit in units:
        for assignment in unit.fiduciary_assignments.all():
            for payment in assignment.payments.all():
                item = _payment_export_item(payment)
                if not _is_fiduciary_ordinary_payment(item):
                    continue
                key = _payment_period_key(payment)
                if key:
                    keys.add(key)
    return sorted(keys)


def _is_fiduciary_ordinary_payment(item: PaymentExportItem) -> bool:
    return (
        item.category in {"ordinary", "other"}
        and item.destination == Payment.Destination.FIDUCIARIA
        and item.amount_text
    )


def _current_assignment(assignments: list[FiduciaryAssignment]) -> FiduciaryAssignment | None:
    candidates = [
        assignment
        for assignment in assignments
        if assignment.is_active and _current_primary_holder(assignment) is not None
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda assignment: (assignment.start_date, assignment.pk or 0), reverse=True)[0]


def _current_primary_holder(assignment: FiduciaryAssignment) -> FiduciaryAssignmentHolder | None:
    holders = list(assignment.holders.all())
    return next((holder for holder in holders if holder.is_active and holder.is_primary), None)


def _assignment_row(
    unit: PropertyUnit,
    assignment: FiduciaryAssignment,
    headers: list[str],
    month_keys: list[tuple[int, int]],
    *,
    row_number: int,
) -> list[object]:
    holders = list(assignment.holders.all())
    primary_holder = next((holder for holder in holders if holder.is_active and holder.is_primary), None)
    primary = primary_holder.client if primary_holder else None
    secondary_clients = [holder.client for holder in holders if holder.is_active and not holder.is_primary]
    payments = [_payment_export_item(payment) for payment in assignment.payments.all()]
    row_by_header = {
        "APTO ": unit.name or unit.code,
        "AREA": unit.area if unit.area is not None else "",
        "ENTIDAD FINANCIERA": unit.financial_entity or "",
        "VALOR INMUEBLE": unit.property_value if unit.property_value is not None else "",
        "ENCARGO FIDUCIARIO": assignment.assignment_number,
        "NUEVOS ENCARGO FIDUCIARIO": _new_assignment_numbers(unit, assignment),
        "CEDULA CLIENTE": primary.document_number if primary else "",
        "NOMBRE CLIENTE": _client_excel_name(primary),
        "NOMBRE CLIENTE2": " | ".join(_client_excel_name(client) for client in secondary_clients),
        "TELEFONO": primary.phone if primary else "",
        "CONTACTO": primary.address if primary else "",
        "E-MAIL": primary.email if primary else "",
        "FECHA CONTRATO DE ADHESION": assignment.adhesion_contract_date,
        "FECHA DE PROMESA": assignment.promise_date,
        "ENTREGA REAL": assignment.actual_delivery_date,
        "FECHA": _main_date_sequence(payments),
        "FECHA PAGO CREDITO": _date_sequence(payments, {"credit"}, include_separator=False),
        "FECHA PAGO SUBSIDIO": _date_sequence(payments, {"subsidy"}, include_separator=False),
        "RECIBOS": _receipt_sequence(payments, destination=Payment.Destination.CONSTRUCTORA),
        "RECIBOS FIDUBOGOTA": _receipt_sequence(payments, destination=Payment.Destination.FIDUCIARIA),
        "RECIBIDO": _received_amount_sequence(payments),
        "ABONOS CR CONSTRUCTOR": _amount_sequence(payments, {"credit"}, include_separator=False),
        "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION": _amount_sequence(payments, {"subsidy"}, include_separator=False),
        "DESEMBOLSO SUBSIDIOS GOBIERNO": "",
        "AJUSTE": _amount_sequence(payments, {"adjustment"}, include_separator=False),
        "CESIONES/TRASLADOS": _amount_sequence(payments, {"cession", "transfer"}, include_separator=False),
        "OBSERVACIONES": _assignment_observations(unit, assignment),
    }
    for year, month in month_keys:
        row_by_header[_fiduciary_received_header(year, month)] = _monthly_amount_sequence(
            payments,
            year,
            month,
            Payment.Destination.FIDUCIARIA,
        )
    _apply_financial_formulas(row_by_header, headers, row_number)
    return _row_from_header_map(headers, row_by_header)


def _empty_unit_row(unit: PropertyUnit, headers: list[str]) -> list[object]:
    return _row_from_header_map(
        headers,
        {
            "APTO ": unit.name or unit.code,
            "AREA": unit.area if unit.area is not None else "",
            "ENTIDAD FINANCIERA": unit.financial_entity or "",
            "VALOR INMUEBLE": unit.property_value if unit.property_value is not None else "",
        },
    )


def _new_assignment_numbers(unit: PropertyUnit, assignment: FiduciaryAssignment) -> str:
    return ""


def _assignment_observations(unit: PropertyUnit, assignment: FiduciaryAssignment) -> str:
    values = [_clean_business_text(assignment.observations)]
    values.extend(
        _clean_business_text(observation.detail)
        for observation in unit.historical_observations.all()
        if not _is_observation_backed_by_novelty(observation)
    )
    values.extend(_current_transition_observations(unit, assignment))
    return _join_unique(values)


def _current_transition_observations(unit: PropertyUnit, assignment: FiduciaryAssignment) -> list[str]:
    values = []
    for novelty in unit.operational_novelties.all():
        if novelty.new_assignment_id != assignment.pk or not _is_cession_or_transfer(novelty):
            continue
        source_text = ""
        if novelty.source_observation_id:
            source_text = novelty.source_observation.detail
        values.append(_clean_business_text(source_text or novelty.detail))
    return values


def _is_cession_or_transfer(novelty: OperationalNovelty) -> bool:
    if novelty.novelty_type == OperationalNovelty.NoveltyType.CESSION:
        return True
    summary = _novelty_summary(novelty).casefold()
    detail = (novelty.detail or "").casefold()
    return any(token in summary or token in detail for token in ("cesion", "cesión", "traslado"))


def _novelty_rows_for_units(
    units: list[PropertyUnit],
    headers: list[str],
    month_keys: list[tuple[int, int]],
    main_row_count: int,
) -> list[list[object]]:
    items: list[HistoricalExportItem] = []
    for unit in units:
        for group in _client_novelty_exports(unit):
            first = group.novelties[0]
            items.append(
                HistoricalExportItem(
                    sort_date=first.effective_date,
                    historical_year=first.historical_year,
                    historical_month=first.historical_month,
                    source_order=0,
                    source_row=first.source_row,
                    pk=first.pk or 0,
                    kind="client_novelties",
                    obj=group,
                )
            )
    items = sorted(items, key=_historical_item_sort_key)
    first_novelty_row = 10 + main_row_count
    return [
        _historical_item_row(item, headers, month_keys, row_number=first_novelty_row + index)
        for index, item in enumerate(items)
    ]


def _historical_item_row(
    item: HistoricalExportItem,
    headers: list[str],
    month_keys: list[tuple[int, int]],
    *,
    row_number: int,
) -> list[object]:
    if item.kind == "client_novelties":
        return _client_novelties_row(item.obj, headers, month_keys, row_number=row_number)
    if item.kind == "observation":
        return _observation_row(item.obj, headers, month_keys, row_number=row_number)
    return _novelty_row(item.obj, headers, month_keys, row_number=row_number)


def _client_novelty_exports(unit: PropertyUnit) -> list[ClientNoveltyExport]:
    grouped: dict[tuple[int | None, int | None], dict[str, object]] = {}
    for novelty in unit.operational_novelties.all():
        relations = _novelty_client_relations(novelty)
        if not relations:
            relations = [(None, novelty.previous_assignment or novelty.historical_assignment or novelty.new_assignment)]
        for client, assignment in relations:
            key = (client.pk if client else None, assignment.pk if assignment else None)
            if key not in grouped:
                grouped[key] = {"client": client, "assignment": assignment, "novelties": []}
            grouped[key]["novelties"].append(novelty)
    exports = [
        ClientNoveltyExport(
            unit=unit,
            client=data["client"],
            assignment=data["assignment"],
            novelties=tuple(sorted(data["novelties"], key=_novelty_sort_key)),
        )
        for data in grouped.values()
    ]
    return sorted(exports, key=lambda item: _novelty_sort_key(item.novelties[0]))


def _novelty_client_relations(novelty: OperationalNovelty) -> list[tuple[Client, FiduciaryAssignment | None]]:
    relations: list[tuple[Client, FiduciaryAssignment | None]] = []
    for client, assignment in (
        (novelty.previous_client, novelty.previous_assignment),
        (novelty.historical_client, novelty.historical_assignment),
        (novelty.new_client, novelty.new_assignment),
    ):
        if client and not any(existing.pk == client.pk for existing, _ in relations):
            relations.append((client, assignment))
    return relations


def _novelty_sort_key(novelty: OperationalNovelty) -> tuple:
    return (
        novelty.effective_date or date.max,
        novelty.historical_year or 9999,
        novelty.historical_month or 99,
        novelty.source_row or 0,
        novelty.pk or 0,
    )


def _historical_item_sort_key(item: HistoricalExportItem) -> tuple:
    return (
        item.sort_date or date.max,
        item.historical_year or 9999,
        item.historical_month or 99,
        item.source_order,
        item.source_row or 0,
        item.pk,
    )


def _historical_text_key(
    unit: PropertyUnit,
    text: str,
    historical_year: int | None,
    historical_month: int | None,
) -> tuple | None:
    cleaned = _clean_business_text(text)
    normalized = re.sub(r"[^a-z0-9]+", " ", cleaned.casefold()).strip()
    if not normalized:
        return None
    return (unit.pk, historical_year or 0, historical_month or 0, normalized)


def _is_linked_to_operational_novelty(observation: ImportedHistoricalObservation) -> bool:
    return hasattr(observation, "operational_novelty")


def _is_observation_backed_by_novelty(observation: ImportedHistoricalObservation) -> bool:
    return _is_linked_to_operational_novelty(observation) or bool(observation.source_novelty_id)


def _is_historical_observation(observation: ImportedHistoricalObservation) -> bool:
    return bool(observation.historical_year or observation.historical_month or observation.historical_section or observation.source_novelty_id)


def _client_novelties_row(
    export: ClientNoveltyExport,
    headers: list[str],
    month_keys: list[tuple[int, int]],
    *,
    row_number: int,
) -> list[object]:
    assignment = export.assignment
    primary_client = export.client
    payments = [_payment_export_item(payment) for payment in assignment.payments.all()] if assignment else []
    summaries = [_clean_business_text(_novelty_summary(novelty)) for novelty in export.novelties]
    details = [_clean_business_text(novelty.detail) for novelty in export.novelties]
    row_by_header = {
        "APTO ": export.unit.name or export.unit.code,
        "AREA": export.unit.area if export.unit.area is not None else "",
        "ENTIDAD FINANCIERA": export.unit.financial_entity or "",
        "VALOR INMUEBLE": export.unit.property_value if export.unit.property_value is not None else "",
        "ENCARGO FIDUCIARIO": assignment.assignment_number if assignment else "",
        "NUEVOS ENCARGO FIDUCIARIO": _join_text_values(
            [
                novelty.new_assignment.assignment_number
                for novelty in export.novelties
                if novelty.new_assignment_id and novelty.new_assignment_id != (assignment.pk if assignment else None)
            ]
        ),
        "CEDULA CLIENTE": primary_client.document_number if primary_client else "",
        "NOMBRE CLIENTE": _client_excel_name(primary_client),
        "NOMBRE CLIENTE2": _join_text_values(summaries),
        "TELEFONO": primary_client.phone if primary_client else "",
        "CONTACTO": primary_client.address if primary_client else "",
        "E-MAIL": primary_client.email if primary_client else "",
        "FECHA CONTRATO DE ADHESION": assignment.adhesion_contract_date if assignment else "",
        "FECHA DE PROMESA": assignment.promise_date if assignment else "",
        "ENTREGA REAL": assignment.actual_delivery_date if assignment else "",
        "FECHA": _join_text_values([_novelty_date_text(novelty) for novelty in export.novelties]),
        "RECIBOS": _receipt_sequence(payments, destination=Payment.Destination.CONSTRUCTORA),
        "RECIBOS FIDUBOGOTA": _receipt_sequence(payments, destination=Payment.Destination.FIDUCIARIA),
        "RECIBIDO": _received_amount_sequence(payments),
        "FECHA PAGO CREDITO": _date_sequence(payments, {"credit"}, include_separator=False),
        "FECHA PAGO SUBSIDIO": _date_sequence(payments, {"subsidy"}, include_separator=False),
        "ABONOS CR CONSTRUCTOR": _amount_sequence(payments, {"credit"}, include_separator=False),
        "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION": _amount_sequence(payments, {"subsidy"}, include_separator=False),
        "DESEMBOLSO SUBSIDIOS GOBIERNO": "",
        "AJUSTE": _amount_sequence(payments, {"adjustment"}, include_separator=False),
        "CESIONES/TRASLADOS": _amount_sequence(payments, {"cession", "transfer"}, include_separator=False),
        "OBSERVACIONES": _join_text_values(details),
    }
    for year, month in month_keys:
        row_by_header[_fiduciary_received_header(year, month)] = _monthly_amount_sequence(
            payments,
            year,
            month,
            Payment.Destination.FIDUCIARIA,
        )
    _apply_financial_formulas(row_by_header, headers, row_number)
    return _row_from_header_map(headers, row_by_header)


def _novelty_row(
    novelty: OperationalNovelty,
    headers: list[str],
    month_keys: list[tuple[int, int]],
    *,
    row_number: int,
) -> list[object]:
    assignment = novelty.previous_assignment or novelty.historical_assignment or novelty.new_assignment
    primary_client = novelty.previous_client or novelty.historical_client or novelty.new_client
    payments = [_payment_export_item(payment) for payment in assignment.payments.all()] if assignment else []
    row_by_header = {
        "APTO ": novelty.property_unit.name or novelty.property_unit.code,
        "AREA": novelty.property_unit.area if novelty.property_unit.area is not None else "",
        "ENTIDAD FINANCIERA": novelty.property_unit.financial_entity or "",
        "VALOR INMUEBLE": novelty.property_unit.property_value if novelty.property_unit.property_value is not None else "",
        "ENCARGO FIDUCIARIO": assignment.assignment_number if assignment else "",
        "NUEVOS ENCARGO FIDUCIARIO": novelty.new_assignment.assignment_number if novelty.new_assignment_id else "",
        "CEDULA CLIENTE": primary_client.document_number if primary_client else "",
        "NOMBRE CLIENTE": _client_excel_name(primary_client),
        "NOMBRE CLIENTE2": _clean_business_text(_novelty_summary(novelty)),
        "TELEFONO": primary_client.phone if primary_client else "",
        "CONTACTO": primary_client.address if primary_client else "",
        "E-MAIL": primary_client.email if primary_client else "",
        "FECHA CONTRATO DE ADHESION": assignment.adhesion_contract_date if assignment else "",
        "FECHA DE PROMESA": assignment.promise_date if assignment else "",
        "ENTREGA REAL": assignment.actual_delivery_date if assignment else "",
        "FECHA": novelty.effective_date or _historical_period_text(novelty),
        "RECIBOS": _receipt_sequence(payments, destination=Payment.Destination.CONSTRUCTORA),
        "RECIBOS FIDUBOGOTA": _receipt_sequence(payments, destination=Payment.Destination.FIDUCIARIA),
        "RECIBIDO": _received_amount_sequence(payments),
        "FECHA PAGO CREDITO": _date_sequence(payments, {"credit"}, include_separator=False),
        "FECHA PAGO SUBSIDIO": _date_sequence(payments, {"subsidy"}, include_separator=False),
        "ABONOS CR CONSTRUCTOR": _amount_sequence(payments, {"credit"}, include_separator=False),
        "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION": _amount_sequence(payments, {"subsidy"}, include_separator=False),
        "DESEMBOLSO SUBSIDIOS GOBIERNO": "",
        "AJUSTE": _amount_sequence(payments, {"adjustment"}, include_separator=False),
        "CESIONES/TRASLADOS": _amount_sequence(payments, {"cession", "transfer"}, include_separator=False),
        "OBSERVACIONES": _clean_business_text(novelty.detail),
    }
    for year, month in month_keys:
        row_by_header[_fiduciary_received_header(year, month)] = _monthly_amount_sequence(
            payments,
            year,
            month,
            Payment.Destination.FIDUCIARIA,
        )
    _apply_financial_formulas(row_by_header, headers, row_number)
    return _row_from_header_map(headers, row_by_header)


def _observation_row(
    observation: ImportedHistoricalObservation,
    headers: list[str],
    month_keys: list[tuple[int, int]],
    *,
    row_number: int,
) -> list[object]:
    assignment = observation.assignment
    primary_client = observation.client
    payments = [_payment_export_item(payment) for payment in assignment.payments.all()] if assignment else []
    row_by_header = {
        "APTO ": observation.property_unit.name or observation.property_unit.code,
        "AREA": observation.property_unit.area if observation.property_unit.area is not None else "",
        "ENTIDAD FINANCIERA": observation.property_unit.financial_entity or "",
        "VALOR INMUEBLE": observation.property_unit.property_value if observation.property_unit.property_value is not None else "",
        "ENCARGO FIDUCIARIO": assignment.assignment_number if assignment else "",
        "CEDULA CLIENTE": primary_client.document_number if primary_client else "",
        "NOMBRE CLIENTE": _client_excel_name(primary_client),
        "NOMBRE CLIENTE2": _clean_business_text(observation.summary),
        "TELEFONO": primary_client.phone if primary_client else "",
        "CONTACTO": primary_client.address if primary_client else "",
        "E-MAIL": primary_client.email if primary_client else "",
        "FECHA CONTRATO DE ADHESION": assignment.adhesion_contract_date if assignment else "",
        "FECHA DE PROMESA": assignment.promise_date if assignment else "",
        "ENTREGA REAL": assignment.actual_delivery_date if assignment else "",
        "FECHA": _observation_period_text(observation),
        "RECIBOS": _receipt_sequence(payments, destination=Payment.Destination.CONSTRUCTORA),
        "RECIBOS FIDUBOGOTA": _receipt_sequence(payments, destination=Payment.Destination.FIDUCIARIA),
        "RECIBIDO": _received_amount_sequence(payments),
        "FECHA PAGO CREDITO": _date_sequence(payments, {"credit"}, include_separator=False),
        "FECHA PAGO SUBSIDIO": _date_sequence(payments, {"subsidy"}, include_separator=False),
        "ABONOS CR CONSTRUCTOR": _amount_sequence(payments, {"credit"}, include_separator=False),
        "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION": _amount_sequence(payments, {"subsidy"}, include_separator=False),
        "DESEMBOLSO SUBSIDIOS GOBIERNO": "",
        "AJUSTE": _amount_sequence(payments, {"adjustment"}, include_separator=False),
        "CESIONES/TRASLADOS": _amount_sequence(payments, {"cession", "transfer"}, include_separator=False),
        "OBSERVACIONES": _clean_business_text(observation.detail),
    }
    for year, month in month_keys:
        row_by_header[_fiduciary_received_header(year, month)] = _monthly_amount_sequence(
            payments,
            year,
            month,
            Payment.Destination.FIDUCIARIA,
        )
    _apply_financial_formulas(row_by_header, headers, row_number)
    return _row_from_header_map(headers, row_by_header)


def _historical_period_text(novelty: OperationalNovelty) -> str:
    if novelty.historical_year and novelty.historical_month:
        return f"{novelty.historical_month:02d}/{novelty.historical_year}"
    if novelty.historical_year:
        return str(novelty.historical_year)
    return ""


def _novelty_date_text(novelty: OperationalNovelty) -> str:
    if novelty.effective_date:
        return novelty.effective_date.isoformat()
    return _historical_period_text(novelty)


def _observation_period_text(observation: ImportedHistoricalObservation) -> str:
    if observation.historical_year and observation.historical_month:
        return f"{observation.historical_month:02d}/{observation.historical_year}"
    if observation.historical_year:
        return str(observation.historical_year)
    return ""


def _row_from_header_map(headers: list[str], values: dict[str, object]) -> list[object]:
    row = []
    blank_position = 0
    for index, header in enumerate(headers, start=1):
        if header == "":
            blank_position += 1
            row.append(1 if blank_position == 2 else "")
            continue
        row.append(values.get(header, ""))
    return row


def _apply_financial_formulas(values: dict[str, object], headers: list[str], row_number: int) -> None:
    total_col = _header_index(headers, "TOTAL RECIBIDO")
    property_value_col = _header_index(headers, "VALOR INMUEBLE")
    balance_col = _header_index(headers, "SALDO POR COBRAR")
    own_resources_col = _header_index(headers, "RECURSOS PROPIOS")
    credit_col = _header_index(headers, "CREDITO BANCARIO")
    honor_col = _header_index(headers, "CAJA HONOR")
    subsidy_mcy_col = _header_index(headers, "SUBSIDIOS MCY")
    subsidy_comp_col = _header_index(headers, "SUBSIDIOS CAJA COMP")
    monetary_refs = [_cell_ref(column, row_number) for column in _monetary_total_columns(headers)]
    if monetary_refs:
        values["TOTAL RECIBIDO"] = f"=SUM({','.join(monetary_refs)})"
    if property_value_col and total_col:
        values["SALDO POR COBRAR"] = f"={_cell_ref(property_value_col, row_number)}-{_cell_ref(total_col, row_number)}"
    if all([balance_col, credit_col, honor_col, subsidy_mcy_col, subsidy_comp_col]):
        values["RECURSOS PROPIOS"] = (
            f"={_cell_ref(balance_col, row_number)}-{_cell_ref(credit_col, row_number)}"
            f"-{_cell_ref(honor_col, row_number)}-{_cell_ref(subsidy_mcy_col, row_number)}"
            f"-{_cell_ref(subsidy_comp_col, row_number)}"
        )


def _monetary_total_columns(headers: list[str]) -> list[int]:
    monetary_headers = {
        "RECIBIDO",
        "CESIONES/TRASLADOS",
        "ABONOS CR CONSTRUCTOR",
        "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION",
        "DESEMBOLSO SUBSIDIOS GOBIERNO",
        "AJUSTE",
    }
    return [
        index
        for index, header in enumerate(headers, start=1)
        if header in monetary_headers or _is_fiduciary_received_header(header)
    ]


def _summary_row(headers: list[str], label: str, data_start: int, data_end: int) -> list[object]:
    values: dict[str, object] = {"NOMBRE CLIENTE": label}
    if data_end < data_start:
        return _row_from_header_map(headers, values)
    formula_headers = {
        "AREA",
        "VALOR INMUEBLE",
        "CESIONES/TRASLADOS",
        "ABONOS CR CONSTRUCTOR",
        "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION",
        "DESEMBOLSO SUBSIDIOS GOBIERNO",
        "AJUSTE",
        "TOTAL RECIBIDO",
        "SALDO POR COBRAR",
        "RECURSOS PROPIOS",
        "CREDITO BANCARIO",
        "CAJA HONOR",
        "SUBSIDIOS MCY",
        "SUBSIDIOS CAJA COMP",
        "INTERESES",
        "VALOR",
    }
    for header in headers:
        if header in formula_headers or _is_fiduciary_received_header(header):
            if header == "AJUSTE" and label != "TOTAL VENTAS":
                continue
            col = _header_index(headers, header)
            if col:
                values[header] = f"=SUM({_cell_ref(col, data_start)}:{_cell_ref(col, data_end)})"
    return _row_from_header_map(headers, values)


def _por_vender_row(headers: list[str]) -> list[object]:
    return _row_from_header_map(headers, {"NOMBRE CLIENTE": "POR VENDER"})


def _header_index(headers: list[str], target: str) -> int | None:
    for index, header in enumerate(headers, start=1):
        if header == target:
            return index
    return None


def _cell_ref(column: int, row: int) -> str:
    return f"{_column_name(column)}{row}"


def _novelty_summary(novelty: OperationalNovelty) -> str:
    return novelty.summary or novelty.display_type


def _payment_export_item(payment: Payment) -> PaymentExportItem:
    category = _payment_category(payment)
    receipt = _export_receipt(payment.concept or "", category)
    return PaymentExportItem(
        payment=payment,
        date_text=_payment_date(payment, category),
        receipt=receipt,
        amount_text=_decimal_text(payment.amount),
        category=category,
        has_receipt=bool(receipt),
        period_key=_payment_period_key(payment),
        destination=payment.destination,
        is_historical=payment.movement_type == Payment.MovementType.HISTORICAL_PAYMENT,
        is_report_payment=bool(
            payment.source_file_id and payment.source_file.file_type == ImportedFile.FileType.REPORT
        ),
    )


def _payment_category(payment: Payment) -> str:
    concept = (payment.concept or "").casefold()
    if "credito" in concept or "crédito" in concept:
        return "credit"
    if "subsidio" in concept:
        return "subsidy"
    if "ajuste" in concept:
        return "adjustment"
    if "traslado" in concept:
        return "transfer"
    if "cesion" in concept or "cesión" in concept:
        return "cession"
    if payment.movement_type == Payment.MovementType.ADDITION:
        return "other"
    return "ordinary"


def _export_receipt(concept: str, category: str) -> str:
    receipt = _receipt_from_concept(concept)
    if category == "cession":
        return _receipt_with_movement_marker(receipt, "CESION")
    if category == "transfer":
        return _receipt_with_movement_marker(receipt, "TRASLADO")
    return receipt


def _receipt_with_movement_marker(receipt: str, marker: str) -> str:
    if not receipt:
        return ""
    if marker == "TRASLADO":
        base = re.sub(r"(?:TRASLADO|TRASL)$", "", receipt, flags=re.IGNORECASE)
        return f"{base}TRASLADO"
    base = re.sub(rf"{re.escape(marker)}$", "", receipt, flags=re.IGNORECASE)
    return f"{base}{marker}"


def _date_sequence(payments: list[PaymentExportItem], categories: set[str], *, include_separator: bool = True) -> str:
    return _sequence(payments, categories, "date_text", include_separator=include_separator)


def _main_date_sequence(payments: list[PaymentExportItem]) -> str:
    historical = [
        item
        for item in payments
        if item.is_historical and item.category in {"ordinary", "cession", "transfer", "other"} and item.date_text
    ]
    later = [item for item in payments if item.is_report_payment and item.category in {"ordinary", "other"} and item.date_text]
    historical_text = _join_values([item.date_text for item in historical])
    later_text = _join_values([item.date_text for item in later])
    if historical_text and later_text:
        return f"{historical_text}{HISTORICAL_RECEIPT_SEPARATOR}{later_text}"
    return historical_text or later_text


def _receipt_sequence(payments: list[PaymentExportItem], *, destination: str | None = None) -> str:
    receipt_items = []
    for index, item in enumerate(payments):
        if not item.has_receipt:
            continue
        if destination == Payment.Destination.FIDUCIARIA:
            if item.category not in {"ordinary", "other"} or item.destination != Payment.Destination.FIDUCIARIA:
                continue
        elif destination == Payment.Destination.CONSTRUCTORA:
            if item.category in {"ordinary", "other"} and item.destination == Payment.Destination.FIDUCIARIA:
                continue
        elif destination is not None and item.destination != destination:
            continue
        receipt_items.append((index, item))
    receipt_items.sort(key=lambda pair: (_receipt_category_rank(pair[1]), pair[0]))
    return " - ".join(item.receipt for _, item in receipt_items)


def _receipt_category_rank(item: PaymentExportItem) -> int:
    if item.category == "credit":
        return 1
    if item.category == "subsidy":
        return 2
    return 0


def _amount_sequence(payments: list[PaymentExportItem], categories: set[str], *, include_separator: bool = True) -> object:
    filtered = [
        item.amount_text
        for item in payments
        if item.is_historical and item.category in categories and item.amount_text != ""
    ]
    return _join_amount_values(filtered)


def _received_amount_sequence(payments: list[PaymentExportItem]) -> object:
    filtered = [
        item.amount_text
        for item in payments
        if item.category in {"ordinary", "other"}
        and item.destination == Payment.Destination.CONSTRUCTORA
        and item.amount_text != ""
    ]
    return _join_amount_values(filtered)


def _monthly_amount_sequence(
    payments: list[PaymentExportItem],
    year: int,
    month: int,
    destination: str,
) -> object:
    filtered = [
        item.amount_text
        for item in payments
        if item.period_key == (year, month)
        and item.destination == destination
        and item.category in {"ordinary", "other"}
        and item.amount_text != ""
    ]
    return _join_amount_values(filtered)


def _sequence(payments: list[PaymentExportItem], categories: set[str], attr: str, *, include_separator: bool) -> str:
    filtered = [item for item in payments if item.category in categories and getattr(item, attr)]
    with_receipt = [getattr(item, attr) for item in filtered if item.has_receipt]
    without_receipt = [getattr(item, attr) for item in filtered if not item.has_receipt]
    if include_separator and with_receipt and without_receipt:
        return f"{_join_values(with_receipt)}{HISTORICAL_RECEIPT_SEPARATOR}{_join_values(without_receipt)}"
    if include_separator and without_receipt and not with_receipt:
        return f"{HISTORICAL_RECEIPT_SEPARATOR}{_join_values(without_receipt)}"
    return _join_values([*with_receipt, *without_receipt])


def _join_values(values: list[str]) -> str:
    values = [value for value in values if value]
    if not values:
        return ""
    if all(_is_number_text(value) for value in values) and len(values) > 1:
        formula = values[0]
        for value in values[1:]:
            formula += value if value.startswith("-") else f"+{value}"
        return f"={formula}"
    return " - ".join(values)


def _join_text_values(values: list[str]) -> str:
    return " - ".join(value for value in values if value)


def _join_amount_values(values: list[str]) -> object:
    values = [value for value in values if value != ""]
    if not values:
        return ""
    if all(_is_number_text(value) for value in values):
        if len(values) == 1:
            return Decimal(values[0])
        formula = values[0]
        for value in values[1:]:
            formula += value if value.startswith("-") else f"+{value}"
        return f"={formula}"
    return _join_values(values)


def _is_monetary_header(header: str) -> bool:
    return header in {
        "CESIONES/TRASLADOS",
        "ABONOS CR CONSTRUCTOR",
        "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION",
        "DESEMBOLSO SUBSIDIOS GOBIERNO",
        "AJUSTE",
        "TOTAL RECIBIDO",
        "SALDO POR COBRAR",
        "RECURSOS PROPIOS",
        "CREDITO BANCARIO",
        "CAJA HONOR",
        "SUBSIDIOS MCY",
        "SUBSIDIOS CAJA COMP",
        "INTERESES",
        "VALOR",
        "VALOR INMUEBLE",
        "AREA",
        "RECIBIDO",
    } or _is_fiduciary_received_header(header)


def _is_number_text(value: str) -> bool:
    return bool(re.fullmatch(r"-?\d+(?:\.\d+)?", value))


def _receipt_from_concept(concept: str) -> str:
    cleaned = concept.strip()
    if re.match(r"^[A-Z]{1,8}\d+[A-Z0-9-]*$", cleaned, flags=re.IGNORECASE):
        return _clean_receipt(cleaned)
    match = re.search(r"\bRecibo\s+([A-Z0-9-]+)", concept, flags=re.IGNORECASE)
    if match:
        return _clean_receipt(match.group(1))
    if "|" in concept:
        candidate = concept.split("|", 1)[1].strip()
        if re.match(r"^[A-Z]{1,8}\d+[A-Z0-9-]*$", candidate, flags=re.IGNORECASE):
            return _clean_receipt(candidate)
    return ""


def _clean_receipt(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().upper())


def _payment_period_key(payment: Payment) -> tuple[int, int] | None:
    if payment.exact_date:
        return payment.exact_date.year, payment.exact_date.month
    if payment.period_year and payment.period_month:
        return payment.period_year, payment.period_month
    return None


def _payment_date(payment: Payment, category: str | None = None) -> str:
    if category in {"cession", "transfer"}:
        marker = "CESION" if category == "cession" else "TRASLADO"
        if payment.exact_date:
            return f"({MONTH_NAMES[payment.exact_date.month]}.{payment.exact_date.day}/{payment.exact_date.year % 100:02d}{marker})"
        if payment.period_year and payment.period_month:
            return f"({MONTH_NAMES[payment.period_month]}/{payment.period_year}{marker})"
        return ""
    suffix = "F" if payment.destination == Payment.Destination.FIDUCIARIA else ""
    if payment.exact_date:
        return f"{MONTH_NAMES[payment.exact_date.month]}.{payment.exact_date.day}/{payment.exact_date.year % 100:02d}{suffix}"
    if payment.period_year and payment.period_month:
        return f"{MONTH_NAMES[payment.period_month]}/{payment.period_year}{suffix}"
    return ""


def _decimal_text(value: Decimal) -> str:
    return str(value.quantize(Decimal("1"))) if value == value.quantize(Decimal("1")) else str(value.normalize())


def _client_excel_name(client: Client | None) -> str:
    if not client:
        return ""
    return " ".join(part for part in [client.last_names_or_company, client.first_names] if part).strip()


def _clean_business_text(value: str) -> str:
    cleaned = " ".join((value or "").split())
    for pattern in INTERNAL_EXPORT_METADATA_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned.strip(" |")


def _fiduciary_received_header(year: int, month: int) -> str:
    return f"RECIBO FIDUCIA {MONTH_NAMES[month]}/{year}"


def _is_received_header(header: str) -> bool:
    return header == "RECIBIDO"


def _is_fiduciary_received_header(header: str) -> bool:
    return isinstance(header, str) and header.startswith("RECIBO FIDUCIA ")


def _is_fidubogota_header(header: str) -> bool:
    return _is_fiduciary_received_header(header)


def _join_unique(values: list[str]) -> str:
    unique = []
    seen = set()
    for value in values:
        normalized = " ".join((value or "").split())
        if not normalized or normalized.casefold() in seen:
            continue
        seen.add(normalized.casefold())
        unique.append(normalized)
    return " | ".join(unique)


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value or "PROYECTO").strip("_")
    return cleaned or "PROYECTO"


def _sheet_name(value: str, used: set[str]) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", " ", value or "Hoja").strip()[:31] or "Hoja"
    candidate = cleaned
    counter = 2
    while candidate in used:
        suffix = f" {counter}"
        candidate = f"{cleaned[:31 - len(suffix)]}{suffix}"
        counter += 1
    used.add(candidate)
    return candidate


def _build_xlsx(project: Project, payloads: list[tuple[str, dict]]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types(len(payloads)))
        archive.writestr("_rels/.rels", _root_rels())
        archive.writestr("xl/workbook.xml", _workbook_xml(payloads))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(payloads)))
        archive.writestr("xl/styles.xml", _styles_xml())
        for index, (sheet_name, payload) in enumerate(payloads, start=1):
            rows = _worksheet_rows(project, sheet_name, payload)
            header_rows = {4}
            max_columns = len(payload["main_headers"])
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                _worksheet_xml(rows, max_columns, header_rows=header_rows, main_headers=payload["main_headers"]),
            )
    return output.getvalue()


def _worksheet_rows(project: Project, sheet_name: str, payload: dict) -> list[list[object]]:
    main_headers = payload["main_headers"]
    data_start = 5
    data_end = data_start + len(payload["main_rows"]) - 1
    rows: list[list[object]] = [
        [f"CONJUNTO CERRADO {project.name} {sheet_name}"],
        [],
        [],
        main_headers,
        *payload["main_rows"],
        _summary_row(main_headers, "TOTAL VENTAS", data_start, data_end),
        _por_vender_row(main_headers),
        _summary_row(main_headers, "TOTAL", data_start, data_end),
    ]
    if payload["novelty_rows"]:
        rows.extend(
            [
                [],
                [HISTORICAL_SECTION_TITLE],
                *payload["novelty_rows"],
            ]
        )
    return rows


def _content_types(sheet_count: int) -> str:
    sheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{sheets}</Types>"
    )


def _root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )


def _workbook_xml(payloads: list[tuple[str, dict]]) -> str:
    sheet_nodes = "".join(
        f'<sheet name="{_attr(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _) in enumerate(payloads, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheet_nodes}</sheets></workbook>"
    )


def _workbook_rels(sheet_count: int) -> str:
    sheet_rels = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{sheet_rels}"
        f'<Relationship Id="rId{sheet_count + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="3">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="13"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        '</fonts>'
        '<fills count="6"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFE2F0D9"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFCCCCFF"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF0070C0"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFF00"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border><left style="thin"><color auto="1"/></left><right style="thin"><color auto="1"/></right>'
        '<top style="thin"><color auto="1"/></top><bottom style="thin"><color auto="1"/></bottom><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="11">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        '<xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1"/>'
        '<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="2" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="2" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>'
        '<xf numFmtId="3" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
        '<xf numFmtId="3" fontId="0" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1"/>'
        '</cellXfs></styleSheet>'
    )


def _worksheet_xml(
    rows: list[list[object]],
    header_count: int,
    *,
    header_rows: set[int],
    main_headers: list[str],
) -> str:
    row_nodes = []
    novelty_title_index = next((index for index, row in enumerate(rows, start=1) if row == [HISTORICAL_SECTION_TITLE]), None)
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(
            _cell_xml(
                row_index,
                col_index,
                row[col_index - 1] if col_index <= len(row) else "",
                style_id=_style_id(row_index, col_index, row, header_rows, main_headers),
            )
            for col_index in range(1, header_count + 1)
        )
        height = _row_height(row_index, row, header_rows, novelty_title_index)
        attrs = f' ht="{height}" customHeight="1"' if height else ""
        row_nodes.append(f'<row r="{row_index}"{attrs}>{cells}</row>')
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{_column_width(index, header_count, main_headers)}" customWidth="1"/>'
        for index in range(1, header_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"<cols>{columns}</cols>"
        f'<sheetData>{"".join(row_nodes)}</sheetData>'
        "</worksheet>"
    )


def _style_id(row_index: int, col_index: int, row: list[object], header_rows: set[int], headers: list[str]) -> int:
    header = headers[col_index - 1] if col_index <= len(headers) else ""
    if row_index in header_rows:
        if _is_fidubogota_header(header):
            return 5
        if header in GREEN_HEADER_LABELS:
            return 4
        if header in BLUE_HEADER_LABELS:
            return 6
        if header in YELLOW_HEADER_LABELS:
            return 7
        return 2
    if row_index == 1 or row == [HISTORICAL_SECTION_TITLE]:
        return 1
    if not row:
        return 0
    if _is_fidubogota_header(header):
        return 10
    if _is_monetary_header(header):
        return 9
    if row_index >= 4:
        return 8
    return 0


def _row_height(row_index: int, row: list[object], header_rows: set[int], novelty_title_index: int | None) -> float | None:
    if row_index in header_rows:
        return HEADER_ROW_HEIGHT
    if row == [HISTORICAL_SECTION_TITLE]:
        return NOVELTIES_TITLE_HEIGHT
    if any(value in {"TOTAL VENTAS", "POR VENDER", "TOTAL"} for value in row):
        return SUMMARY_ROW_HEIGHT
    if row_index >= 5:
        if novelty_title_index and row_index > novelty_title_index and row:
            return NOVELTY_ROW_HEIGHT
        if row:
            return DATA_ROW_HEIGHT
    return None


def _cell_xml(row: int, col: int, value: object, *, style_id: int = 0) -> str:
    ref = f"{_column_name(col)}{row}"
    style = f' s="{style_id}"' if style_id else ""
    if value is None or value == "":
        return f'<c r="{ref}"{style}/>'
    if isinstance(value, (int, float, Decimal)):
        return f'<c r="{ref}"{style}><v>{value}</v></c>'
    if isinstance(value, str) and value.startswith("="):
        return f'<c r="{ref}"{style}><f>{escape(value[1:])}</f></c>'
    if isinstance(value, (date, datetime)):
        value = value.date() if isinstance(value, datetime) else value
        value = value.isoformat()
    return f'<c r="{ref}" t="inlineStr"{style}><is><t>{escape(str(value))}</t></is></c>'


def _column_width(index: int, header_count: int, headers: list[str] | None = None) -> float:
    if index > header_count:
        return 15
    header = headers[index - 1] if headers and index <= len(headers) else ""
    if header == "CEDULA CLIENTE":
        return 10.28515625
    if header == "NOMBRE CLIENTE":
        return 31.0
    if index in REFERENCE_COLUMN_WIDTHS:
        return REFERENCE_COLUMN_WIDTHS[index]
    if _is_fidubogota_header(header) or _is_received_header(header):
        return 13.0
    if header in REFERENCE_TRAILING_WIDTHS:
        return REFERENCE_TRAILING_WIDTHS[header]
    return 13.0


def _attr(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name
