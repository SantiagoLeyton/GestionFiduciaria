from pathlib import Path
from decimal import Decimal

import pytest

from fiduciary.imports.header_resolver import (
    MATCH_ALIAS,
    MATCH_AMBIGUOUS,
    MATCH_EXACT,
    MATCH_NOT_FOUND,
    MATCH_SIMILAR,
    HeaderCandidate,
    HeaderResolver,
    header_candidates_from_sheet,
    normalize_header,
)
from fiduciary.imports.historical import HistoricalWorkbookParser
from fiduciary.imports.historical.data import CellData
from fiduciary.imports.historical.parser import HISTORICAL_HEADER_RESOLVER
from fiduciary.imports.historical.readers import RawSheet


MONTECIELO_FILE = Path(
    r"C:\Users\ASUS\OneDrive\Escritorio\Practices\ConstructoraCentenarioSAS\Documents\Company\ProyectoFinal\LIBRO MONTECIELO T2.xlsx"
)
LIBRO1_CANDIDATES = [
    Path(
        r"C:\Users\ASUS\OneDrive\Escritorio\Practices\ConstructoraCentenarioSAS\Documents\Company\ProyectoFinal\Libro1.xlsx"
    ),
    Path(r"C:\Users\ASUS\Downloads\Libro1.xlsx"),
]
LIBRO1_FILE = next((path for path in LIBRO1_CANDIDATES if path.exists()), LIBRO1_CANDIDATES[0])
MEDITERRANEO_FILE = Path(
    r"C:\Users\ASUS\OneDrive\Escritorio\Practices\ConstructoraCentenarioSAS\Documents\Company\ProyectoFinal\LIBRO MEDITERRANEO.xlsx"
)
MEDITERRANEO_COPY_FILE = Path(
    r"C:\Users\ASUS\OneDrive\Escritorio\Practices\ConstructoraCentenarioSAS\Documents\Company\ProyectoFinal\zLIBRO MEDITERRANEO - copia.xlsx"
)


def can_read_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def candidate(header, column=1, letter="A"):
    return HeaderCandidate(
        header=header,
        normalized_header=normalize_header(header),
        column_index=column,
        column_letter=letter,
    )


def resolver():
    return HeaderResolver(
        expected_headers={"client": "NOMBRE CLIENTE", "entity": "ENTIDAD", "amount": "VALOR"},
        aliases={"client": {"NOMBRE CLIENTE2"}},
    )


def test_header_resolver_exact_match():
    result = resolver().resolve("client", [candidate("NOMBRE CLIENTE")])

    assert result.found
    assert result.match_type == MATCH_EXACT
    assert result.actual_header == "NOMBRE CLIENTE"
    assert result.column_index == 1


def test_header_resolver_normalizes_spaces_newlines_and_case():
    sheet = RawSheet(
        "Datos",
        1,
        "visible",
        "C1:C1",
        {(1, 3): CellData(1, 3, "C", "C1", "  Nombre\n   Cliente\t")},
        set(),
        set(),
    )
    headers = header_candidates_from_sheet(sheet, 1)

    result = resolver().resolve("client", headers)

    assert result.match_type == MATCH_EXACT
    assert result.actual_header == "Nombre Cliente"
    assert result.column_letter == "C"


def test_header_resolver_known_alias():
    result = resolver().resolve("client", [candidate("NOMBRE CLIENTE2")])

    assert result.match_type == MATCH_ALIAS
    assert result.actual_header == "NOMBRE CLIENTE2"


def test_header_resolver_token_order_similarity():
    result = resolver().resolve("client", [candidate("CLIENTE NOMBRE")])

    assert result.match_type == MATCH_SIMILAR
    assert result.actual_header == "CLIENTE NOMBRE"


def test_header_resolver_preserves_accents_during_normalized_comparison():
    result = HeaderResolver(expected_headers={"phone": "TELEFONO"}).resolve(
        "phone",
        [candidate("TELÉFONO")],
    )

    assert result.match_type == MATCH_EXACT


def test_header_resolver_safe_similar_match():
    result = HeaderResolver(expected_headers={"assignment": "ENCARGO FIDUCIARIO"}).resolve(
        "assignment",
        [candidate("ENCARGO FIDUCARIO")],
    )

    assert result.match_type == MATCH_SIMILAR
    assert result.actual_header == "ENCARGO FIDUCARIO"


def test_header_resolver_not_found():
    result = resolver().resolve("client", [candidate("CONTACTO")])

    assert result.match_type == MATCH_NOT_FOUND
    assert not result.found


def test_header_resolver_rejects_ambiguous_similar_match():
    result = HeaderResolver(expected_headers={"assignment": "ENCARGO FIDUCIARIO"}).resolve(
        "assignment",
        [
            candidate("ENCARGO FIDUCARIO", column=1, letter="A"),
            candidate("ENCARGO FIDUCIARIOO", column=2, letter="B"),
        ],
    )

    assert result.match_type == MATCH_AMBIGUOUS
    assert {item.header for item in result.candidates} == {"ENCARGO FIDUCARIO", "ENCARGO FIDUCIARIOO"}


def test_header_resolver_prevents_entity_false_positive():
    result = resolver().resolve("entity", [candidate("ENTIDAD FINANCIERA")])

    assert result.match_type == MATCH_NOT_FOUND


def test_header_resolver_prevents_amount_false_positive_when_both_exist():
    result = resolver().resolve(
        "amount",
        [candidate("VALOR INTERES", column=1, letter="A"), candidate("VALOR", column=8, letter="H")],
    )

    assert result.match_type == MATCH_EXACT
    assert result.actual_header == "VALOR"
    assert result.column_letter == "H"


def test_header_resolver_prevents_amount_false_positive_when_only_interest_exists():
    result = resolver().resolve("amount", [candidate("VALOR INTERES")])

    assert result.match_type == MATCH_NOT_FOUND


def test_header_resolver_prevents_date_false_positive():
    result = HeaderResolver(expected_headers={"date": "FECHA"}).resolve(
        "date",
        [candidate("FECHA PAGO CREDITO")],
    )

    assert result.match_type == MATCH_NOT_FOUND


def test_header_resolver_prevents_assignment_false_positive():
    result = HeaderResolver(expected_headers={"assignment": "ENCARGO FIDUCIARIO"}).resolve(
        "assignment",
        [candidate("NUEVOS ENCARGO FIDUCIARIO")],
    )

    assert result.match_type == MATCH_NOT_FOUND


def test_header_candidates_are_independent_from_physical_position():
    sheet = RawSheet(
        "Datos",
        1,
        "visible",
        "A1:Z1",
        {
            (1, 7): CellData(1, 7, "G", "G1", "  NOMBRE\nCLIENTE2 "),
            (1, 20): CellData(1, 20, "T", "T1", "ENCARGO FIDUCIARIO"),
        },
        set(),
        set(),
    )

    headers = header_candidates_from_sheet(sheet, 1)
    result = HISTORICAL_HEADER_RESOLVER.resolve("client_name", headers, sheet_name=sheet.name)

    assert result.match_type == MATCH_ALIAS
    assert result.actual_header == "NOMBRE CLIENTE2"
    assert result.column_index == 7
    assert result.column_letter == "G"


def test_historical_parser_resolves_alias_and_reordered_headers_without_fixed_letters():
    cells = {
        (1, 1): CellData(1, 1, "A", "A1", "Proyecto Prueba - Torre A"),
        (4, 5): CellData(4, 5, "E", "E4", "NOMBRE CLIENTE2"),
        (4, 8): CellData(4, 8, "H", "H4", "RECIBO FIDUCIA MAR/2026"),
        (4, 11): CellData(4, 11, "K", "K4", "CEDULA CLIENTE"),
        (4, 15): CellData(4, 15, "O", "O4", "APTO"),
        (4, 21): CellData(4, 21, "U", "U4", "ENCARGO FIDUCIARIO"),
        (5, 5): CellData(5, 5, "E", "E5", "Cliente Uno"),
        (5, 8): CellData(5, 8, "H", "H5", 1000),
        (5, 11): CellData(5, 11, "K", "K5", "123"),
        (5, 15): CellData(5, 15, "O", "O5", "101"),
        (5, 21): CellData(5, 21, "U", "U5", "EF-001"),
    }
    sheet = RawSheet("A", 1, "visible", "A1:U5", cells, set(), set())

    parsed = HistoricalWorkbookParser(Path("LIBRO Prueba.xlsx"))._parse_sheet(sheet)

    assert parsed.classification == "processable"
    assert parsed.columns["client_name_1"].match_type == MATCH_ALIAS
    assert parsed.columns["client_name_1"].index == 5
    assert parsed.columns["unit"].index == 15
    assert parsed.columns["assignment_number"].index == 21
    assert parsed.rows[0].clients[0].name == "Cliente Uno"
    assert parsed.rows[0].payments[0].source_column == "H"


def mediterraneo_like_sheet(
    *,
    old_assignment_header="ENCARGO FIDUCIARIO",
    old_assignment="",
    new_assignment="",
    first_name_header="NOMBRE CLIENTE",
    second_name_header="NOMBRE CLIENTE2",
    third_name_header=None,
    third_name="",
    documents="25120628/4565078",
    area="65.50",
    financial_entity="BBVA",
    property_value="192172500",
    phone="",
    contact="",
    observation="",
):
    cells = {
        (1, 1): CellData(1, 1, "A", "A1", "Proyecto Mediterraneo - Torre 4"),
        (4, 2): CellData(4, 2, "B", "B4", old_assignment_header),
        (4, 3): CellData(4, 3, "C", "C4", "NUEVOS ENCARGO FIDUCIARIO"),
        (4, 4): CellData(4, 4, "D", "D4", "APTO"),
        (4, 6): CellData(4, 6, "F", "F4", "AREA"),
        (4, 8): CellData(4, 8, "H", "H4", "CEDULA CLIENTE"),
        (4, 9): CellData(4, 9, "I", "I4", first_name_header),
        (4, 10): CellData(4, 10, "J", "J4", second_name_header),
        (4, 11): CellData(4, 11, "K", "K4", "ENTIDAD FINANCIERA"),
        (4, 13): CellData(4, 13, "M", "M4", "VALOR INMUEBLE"),
        (4, 24): CellData(4, 24, "X", "X4", "TELEFONO"),
        (4, 25): CellData(4, 25, "Y", "Y4", "CONTACTO"),
        (4, 26): CellData(4, 26, "Z", "Z4", "OBSERVACIONES"),
        (4, 28): CellData(4, 28, "AB", "AB4", "RECIBIDO FIDUBOGOTA AGO/2021"),
        (5, 2): CellData(5, 2, "B", "B5", old_assignment),
        (5, 3): CellData(5, 3, "C", "C5", new_assignment),
        (5, 4): CellData(5, 4, "D", "D5", "S105"),
        (5, 6): CellData(5, 6, "F", "F5", area),
        (5, 8): CellData(5, 8, "H", "H5", documents),
        (5, 9): CellData(5, 9, "I", "I5", "ARANGO RODRIGUEZ CARMENZA"),
        (5, 10): CellData(5, 10, "J", "J5", "CORREA ROJAS SILVIO"),
        (5, 11): CellData(5, 11, "K", "K5", financial_entity),
        (5, 13): CellData(5, 13, "M", "M5", property_value),
        (5, 24): CellData(5, 24, "X", "X5", phone),
        (5, 25): CellData(5, 25, "Y", "Y5", contact),
        (5, 26): CellData(5, 26, "Z", "Z5", observation),
        (5, 28): CellData(5, 28, "AB", "AB5", 150000),
        (6, 2): CellData(6, 2, "B", "B6", "EF-S101"),
        (6, 4): CellData(6, 4, "D", "D6", "S101"),
        (6, 8): CellData(6, 8, "H", "H6", "100"),
        (6, 9): CellData(6, 9, "I", "I6", "CLIENTE S101"),
        (6, 28): CellData(6, 28, "AB", "AB6", 100000),
    }
    if third_name_header:
        cells[(4, 11)] = CellData(4, 11, "K", "K4", third_name_header)
        cells[(5, 11)] = CellData(5, 11, "K", "K5", third_name)
    return RawSheet("T4", 1, "visible", "A1:AB6", cells, set(), set())


def test_sheet_classification_accepts_original_client_header():
    parsed = HistoricalWorkbookParser(Path("LIBRO MEDITERRANEO.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(old_assignment="60002010466030")
    )

    assert parsed.classification == "processable"
    assert parsed.rows[0].assignment.assignment_number == "60002010466030"
    assert parsed.rows[0].area == Decimal("65.50")
    assert parsed.rows[0].financial_entity == "BBVA"
    assert parsed.rows[0].property_value == Decimal("192172500")


def test_sheet_classification_accepts_client_name2_alias():
    parsed = HistoricalWorkbookParser(Path("zLIBRO MEDITERRANEO - copia.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(
            old_assignment_header="ENCARGO FIDUCIARIO ANTIGUO",
            first_name_header="CLIENTE NOMBRE",
            second_name_header="NOMBRE CLIENTE2",
            new_assignment="60002010466031",
        )
    )

    assert parsed.classification == "processable"
    assert parsed.columns["client_name_1"].header == "CLIENTE NOMBRE"
    assert parsed.columns["client_name_1"].match_type == MATCH_SIMILAR
    assert parsed.columns["client_name_2"].header == "NOMBRE CLIENTE2"
    assert parsed.columns["client_name_2"].match_type == MATCH_ALIAS


def test_new_assignment_number_is_valid_when_old_assignment_is_empty():
    parsed = HistoricalWorkbookParser(Path("LIBRO MEDITERRANEO.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(new_assignment="60002010466031")
    )

    s105 = next(row for row in parsed.rows if row.unit_code == "S105")
    assert s105.assignment.assignment_number == "60002010466031"
    assert s105.assignment.previous_assignment_number is None
    assert [client.document_number for client in s105.clients] == ["25120628", "4565078"]


def test_new_assignment_number_takes_precedence_over_old_assignment():
    parsed = HistoricalWorkbookParser(Path("LIBRO MEDITERRANEO.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(old_assignment="OLD-001", new_assignment="NEW-001")
    )

    s105 = next(row for row in parsed.rows if row.unit_code == "S105")
    assert s105.assignment.assignment_number == "NEW-001"
    assert s105.assignment.previous_assignment_number == "OLD-001"


def test_old_assignment_number_keeps_historical_behavior_when_new_assignment_is_empty():
    parsed = HistoricalWorkbookParser(Path("LIBRO MEDITERRANEO.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(old_assignment="OLD-ONLY")
    )

    s105 = next(row for row in parsed.rows if row.unit_code == "S105")
    assert s105.assignment.assignment_number == "OLD-ONLY"


def test_hyphen_separated_phones_are_assigned_by_client_order():
    parsed = HistoricalWorkbookParser(Path("LIBRO MEDITERRANEO.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(old_assignment="EF-001", phone="3001111111-3002222222")
    )

    s105 = next(row for row in parsed.rows if row.unit_code == "S105")
    assert [client.phone for client in s105.clients[:2]] == ["3001111111", "3002222222"]


def test_slash_separated_phones_keep_montecielo_behavior():
    parsed = HistoricalWorkbookParser(Path("LIBRO MONTECIELO T2.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(old_assignment="EF-001", phone="3001111111/3002222222")
    )

    s105 = next(row for row in parsed.rows if row.unit_code == "S105")
    assert [client.phone for client in s105.clients[:2]] == ["3001111111", "3002222222"]


def test_single_phone_is_not_duplicated_for_multiple_clients():
    parsed = HistoricalWorkbookParser(Path("LIBRO MEDITERRANEO.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(old_assignment="EF-001", phone="3001111111")
    )

    s105 = next(row for row in parsed.rows if row.unit_code == "S105")
    assert [client.phone for client in s105.clients[:2]] == ["3001111111", None]


def test_contact_is_shared_while_phone_remains_individual():
    parsed = HistoricalWorkbookParser(Path("LIBRO MEDITERRANEO.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(
            old_assignment="EF-001",
            phone="3001111111-3002222222",
            contact="LLAMAR DESPUES DE LAS 5 PM",
        )
    )

    s105 = next(row for row in parsed.rows if row.unit_code == "S105")
    assert [client.phone for client in s105.clients[:2]] == ["3001111111", "3002222222"]
    assert [client.contact_name for client in s105.clients[:2]] == [
        "LLAMAR DESPUES DE LAS 5 PM",
        "LLAMAR DESPUES DE LAS 5 PM",
    ]


def test_observation_is_shared_at_row_level_while_phone_remains_individual():
    parsed = HistoricalWorkbookParser(Path("LIBRO MEDITERRANEO.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(
            old_assignment="EF-001",
            phone="3001111111-3002222222",
            observation="PENDIENTE DOCUMENTACION",
        )
    )

    s105 = next(row for row in parsed.rows if row.unit_code == "S105")
    assert [client.phone for client in s105.clients[:2]] == ["3001111111", "3002222222"]
    assert s105.observation == "PENDIENTE DOCUMENTACION"


def test_single_client_keeps_phone_contact_and_observation():
    parsed = HistoricalWorkbookParser(Path("LIBRO MEDITERRANEO.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(
            old_assignment="EF-001",
            second_name_header="SIN USO",
            documents="25120628",
            phone="3001111111",
            contact="CONTACTO UNICO",
            observation="OBSERVACION UNICA",
        )
    )

    s105 = next(row for row in parsed.rows if row.unit_code == "S105")
    assert len(s105.clients) == 1
    assert s105.clients[0].phone == "3001111111"
    assert s105.clients[0].contact_name == "CONTACTO UNICO"
    assert s105.observation == "OBSERVACION UNICA"


def test_more_than_two_clients_receive_sequential_phones():
    parsed = HistoricalWorkbookParser(Path("LIBRO MEDITERRANEO.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(
            old_assignment="EF-001",
            third_name_header="CLIENTE NOMBRE",
            third_name="TERCER CLIENTE",
            documents="1/2/3",
            phone="3001111111-3002222222-3003333333",
        )
    )

    s105 = next(row for row in parsed.rows if row.unit_code == "S105")
    assert [client.name for client in s105.clients] == [
        "ARANGO RODRIGUEZ CARMENZA",
        "CORREA ROJAS SILVIO",
        "TERCER CLIENTE",
    ]
    assert [client.phone for client in s105.clients] == ["3001111111", "3002222222", "3003333333"]


def test_internal_hyphens_are_not_split_as_multiple_phones():
    parsed = HistoricalWorkbookParser(Path("LIBRO MEDITERRANEO.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(old_assignment="EF-001", phone="300-111-1111")
    )

    s105 = next(row for row in parsed.rows if row.unit_code == "S105")
    assert [client.phone for client in s105.clients[:2]] == ["300-111-1111", None]


def test_missing_old_and_new_assignment_keeps_invalid_row_diagnostic():
    parsed = HistoricalWorkbookParser(Path("LIBRO MEDITERRANEO.xlsx"))._parse_sheet(mediterraneo_like_sheet())

    assert "S105" not in {row.unit_code for row in parsed.rows}
    assert any(issue.code == "INVALID_HISTORICAL_ROW" and issue.row_number == 5 for issue in parsed.issues)


def test_mediterraneo_payment_headers_are_detected_without_fixed_letters():
    parsed = HistoricalWorkbookParser(Path("LIBRO MEDITERRANEO.xlsx"))._parse_sheet(
        mediterraneo_like_sheet(old_assignment="60002010466030")
    )

    payment = parsed.payment_columns[0]
    assert payment.header == "RECIBIDO FIDUBOGOTA AGO/2021"
    assert payment.month == 8
    assert payment.year == 2021
    assert payment.letter == "AB"
    assert parsed.rows[0].payments[0].amount == 150000


@pytest.mark.skipif(not can_read_file(MEDITERRANEO_FILE), reason="LIBRO MEDITERRANEO.xlsx no disponible o bloqueado")
def test_real_mediterraneo_t4_and_s105_are_processable():
    parsed = HistoricalWorkbookParser(MEDITERRANEO_FILE, grouping_type_hint="Torre").parse()
    sheet = parsed.sheets[0]
    s105 = next(row for row in sheet.rows if row.unit_code == "S105")

    assert sheet.name == "T4"
    assert sheet.classification == "processable"
    assert sheet.header_row == 4
    assert s105.assignment.assignment_number == "60002010466031"
    assert s105.assignment.previous_assignment_number is None
    assert [client.document_number for client in s105.clients[:2]] == ["25120628", "4565078"]
    assert [client.name for client in s105.clients[:2]] == ["ARANGO RODRIGUEZ CARMENZA", "CORREA ROJAS SILVIO"]
    assert [client.phone for client in s105.clients[:2]] == ["3217569613", "3183229925"]


@pytest.mark.skipif(not can_read_file(MEDITERRANEO_COPY_FILE), reason="zLIBRO MEDITERRANEO - copia.xlsx no disponible o bloqueado")
def test_real_mediterraneo_copy_uses_client_alias_and_new_assignment():
    parsed = HistoricalWorkbookParser(MEDITERRANEO_COPY_FILE, grouping_type_hint="Torre").parse()
    sheet = parsed.sheets[0]
    s105 = next(row for row in sheet.rows if row.unit_code == "S105")

    assert sheet.name == "T4"
    assert sheet.classification == "processable"
    assert sheet.header_row == 4
    assert sheet.columns["client_name_1"].header in {"CLIENTE NOMBRE", "NOMBRE CLIENTE"}
    if sheet.columns["client_name_1"].header == "CLIENTE NOMBRE":
        assert sheet.columns["client_name_1"].match_type == MATCH_SIMILAR
    else:
        assert sheet.columns["client_name_1"].match_type == MATCH_EXACT
    assert sheet.columns["client_name_2"].header == "NOMBRE CLIENTE2"
    assert sheet.columns["client_name_2"].match_type == MATCH_ALIAS
    assert sheet.columns["new_assignment_number"].header == "NUEVOS ENCARGO FIDUCIARIO"
    assert s105.assignment.assignment_number == "60002010466031"
    assert [client.phone for client in s105.clients[:2]] == ["3217569613", "3183229925"]


def test_client_name_is_not_added_as_explicit_alias():
    assert "CLIENTE NOMBRE" not in {
        alias.upper()
        for alias in HISTORICAL_HEADER_RESOLVER.aliases["client_name"]
    }


@pytest.mark.skipif(not can_read_file(MONTECIELO_FILE), reason="Libro real de Montecielo no disponible o bloqueado")
def test_header_resolver_works_with_real_montecielo_headers():
    parsed = HistoricalWorkbookParser(MONTECIELO_FILE).parse()
    sheet = parsed.sheets[0]

    assert sheet.classification == "processable"
    assert sheet.columns["assignment_number"].match_type == MATCH_EXACT
    assert sheet.columns["unit"].match_type == MATCH_ALIAS
    assert sheet.columns["unit"].header == "APTO"
    assert sheet.columns["client_name_1"].match_type == MATCH_EXACT
    assert sheet.columns["phone"].header == "TELEFONO"
    assert sheet.columns["email"].header == "E-MAIL"
    assert sheet.columns["contact_name"].header == "CONTACTO"


@pytest.mark.skipif(not can_read_file(MONTECIELO_FILE), reason="Libro real de Montecielo no disponible o bloqueado")
def test_real_montecielo_slash_phones_remain_assigned_by_client_order():
    parsed = HistoricalWorkbookParser(MONTECIELO_FILE).parse()
    sheet = parsed.sheets[0]
    row_308 = next(row for row in sheet.rows if row.unit_code == "308")

    assert [client.phone for client in row_308.clients[:2]] == ["624072792", "627607786"]


@pytest.mark.skipif(not LIBRO1_FILE.exists(), reason="Libro1.xlsx no disponible en el entorno")
def test_header_resolver_can_analyze_libro1_when_available():
    parsed = HistoricalWorkbookParser(LIBRO1_FILE).parse()
    headers_by_sheet = {
        sheet.name: {
            column.header
            for key, column in sheet.columns.items()
            if not key.startswith("client_names")
        }
        for sheet in parsed.sheets
        if sheet.header_row
    }

    assert headers_by_sheet
    assert any(
        "NOMBRE CLIENTE2" in headers or "NOMBRE CLIENTE" in headers
        for headers in headers_by_sheet.values()
    )


def test_future_new_assignment_header_can_be_resolved_without_business_logic():
    result = HeaderResolver(expected_headers={"new_assignment": "NUEVOS ENCARGO FIDUCIARIO"}).resolve(
        "new_assignment",
        [candidate("NUEVOS ENCARGO FIDUCIARIO", column=4, letter="D")],
    )

    assert result.match_type == MATCH_EXACT
    assert result.column_letter == "D"
