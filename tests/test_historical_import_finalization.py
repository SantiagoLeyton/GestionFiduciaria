from pathlib import Path
from types import SimpleNamespace
from datetime import date
from decimal import Decimal
import shutil
import tempfile
import zipfile

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse

from fiduciary.exporters import export_historical_workbook
from fiduciary.imports.historical import DuplicateHistoricalImportError, analyze_historical_import, store_historical_import_file
from fiduciary.imports.historical.finalize import (
    HistoricalImportFinalizationError,
    _FinalizationContext,
    _historical_event_effective_date,
    _historical_event_fragments,
    _historical_payment_concept,
    _historical_novelty_type,
    _parse_historical_payment_date,
    _canonical_client_name_matches,
    _summary_detail_from_cells,
    _unique_client_matching_mention,
    finalize_historical_import,
)
from fiduciary.imports.historical.normalize import normalize_text
from fiduciary.imports.historical.resolutions import auto_resolve_new_units, auto_resolve_units_for_group_resolution, update_batch_resolution_state
from fiduciary.models import (
    Client,
    DetectedStructureElement,
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportedHistoricalObservation,
    ImportedHistoricalNovelty,
    ImportedSheetResult,
    ImportResolution,
    ImportRowIssue,
    OperationalNovelty,
    Payment,
    UnitOwnership,
)
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup


pytestmark = pytest.mark.django_db
REAL_MIRADOR_FILE = Path(
    r"C:\Users\ASUS\OneDrive\Escritorio\Practices\ConstructoraCentenarioSAS\Documents\Code\DatosFalsos\salida\LIBRO_MIRADOR DEL QUINDIO.xlsx"
)
REAL_KSMP_FILE = Path(
    r"C:\Users\ASUS\OneDrive\Escritorio\Practices\ConstructoraCentenarioSAS\Documents\Code\DatosFalsos\salida\LIBRO_KSMP.xlsx"
)
REAL_MONTECIELO_FILE = Path(
    r"C:\Users\ASUS\OneDrive\Escritorio\aja\aja x2\LIBRO MONTECIELO - copia.xlsx"
)
REAL_MONTECIELO_AVAILABLE_FILE = Path(
    r"C:\Users\ASUS\OneDrive\Escritorio\aja\aja x2\LIBRO MONTECIELO.xlsx"
)


def build_minimal_manzana_workbook(path: Path, *, duplicate_assignment: bool = False) -> Path:
    t2_first_assignment = "EF-T1-101" if duplicate_assignment else "EF-T2-101"
    sheets = [
        (
            "T1",
            "PROYECTO MANZANA - Torre 1",
            [
                ("101", "EF-T1-101", "1001", "Cliente T1 101", 100000),
                ("102", "EF-T1-102", "1002", "Cliente T1 102", 200000),
            ],
        ),
        (
            "T2",
            "PROYECTO MANZANA - Torre 2",
            [
                ("101", t2_first_assignment, "2001", "Cliente T2 101", 150000),
                ("102", "EF-T2-102", "2002", "Cliente T2 102", 250000),
            ],
        ),
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(len(sheets)))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook([sheet[0] for sheet in sheets]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(len(sheets)))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        headers = ["APTO", "ENCARGO FIDUCIARIO", "CEDULA CLIENTE", "NOMBRE CLIENTE", "RECIBIDO ENE/2026"]
        for index, (_, title, rows) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _xlsx_sheet_xml(title, headers, rows))
    return path


def build_client_identity_contact_workbook(path: Path) -> Path:
    headers = [
        "APTO",
        "ENCARGO FIDUCIARIO",
        "CEDULA CLIENTE",
        "NOMBRE CLIENTE",
        "CELULAR",
        "E-MAIL",
        "CONTACTO",
        "RECIBIDO ENE/2026",
    ]
    rows = [
        (
            "303",
            "60007919107424",
            "75692028",
            "WATSON CLEVELAND",
            "3166488834",
            "watson.cleveland.75692028@example.com",
            "SANTIAGO LEYTON CEL: 3009898588",
            100000,
        ),
        (
            "304",
            "60007919107425",
            "75692029",
            "TENTACLES LUCAS",
            "3166488835",
            "tentacles.lucas.75692029@example.com",
            "CONTACTO TENTACLES",
            100000,
        ),
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(1))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook(["T1"]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(1))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml("PROYECTO KSMP - Torre 1", headers, rows))
    return path


def build_secondary_holder_workbook(path: Path) -> Path:
    headers = [
        "APTO",
        "ENCARGO FIDUCIARIO",
        "CEDULA CLIENTE",
        "NOMBRE CLIENTE",
        "NOMBRE CLIENTE2",
        "CELULAR",
        "E-MAIL",
        "RECIBIDO ENE/2026",
    ]
    rows = [
        (
            "701",
            "EF-SEC-701",
            "7001/7002",
            "TITULAR PRINCIPAL",
            "TITULAR SECUNDARIO",
            "3007001000/3007002000",
            "principal@example.com,secundario@example.com",
            100000,
        )
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(1))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook(["T1"]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(1))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml("PROYECTO MANZANA - Torre 1", headers, rows))
    return path


def build_ksmp_unit_303_chain_workbook(path: Path) -> Path:
    first_event = "NC6383 MAR.4/22 CESION DE LEYTON MARIA A LUCAS TENTACLES *ANTIC $15.000.000 *ARRAS $4'640"
    second_event = "NC6383 MAR.4/23 CESION DE LUCAS TENTACLES A CLEVELAND WATSON *ANTIC $15.000.000 *ARRAS $4'640"
    headers = [
        "APTO",
        "ENCARGO FIDUCIARIO",
        "CEDULA CLIENTE",
        "NOMBRE CLIENTE",
        "TIPO NOVEDAD",
        "OBSERVACIONES",
        "RECIBIDO ENE/2026",
    ]
    row_entries = [
        (21, ("303", "60007919107424", "75692028", "WATSON CLEVELAND", "", second_event, 100000)),
        (60, ("NOVEDADES", "", "", "", "", "", "")),
        (61, ("CESIONES MAR/2022", "", "", "", "", "", "")),
        (63, ("303", "60007919155557", "75686638", "LEYTON GÃƒâ€œMEZ MARÃƒÂA JOSÃƒâ€°", "*CESION/ARRAS", first_event, "")),
        (
            70,
            (
                "303",
                "60007919107412",
                "75686638",
                "TENTACLES LUCAS",
                "*CESION/ARRAS",
                f"{first_event} | {second_event}",
                "",
            ),
        ),
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(1))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook(["T7"]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(1))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml_at_rows("PROYECTO KSMP - Torre 7", headers, row_entries))
    return path


def build_operational_dates_workbook(
    path: Path,
    *,
    project_name: str = "Manzana",
    assignment_number: str = "EF-DATES-101",
    include_promised_delivery: bool = True,
    promised_delivery_value: str = "15/03/2024",
) -> Path:
    headers = [
        "APTO",
        "ENCARGO FIDUCIARIO",
        "CEDULA CLIENTE",
        "NOMBRE CLIENTE",
        "FECHA CONTRATO DE ADHESION",
        "FECHA DE PROMESA",
    ]
    row = [
        "101",
        assignment_number,
        "8101",
        "Cliente Fechas",
        "10/01/2024",
        "20/02/2024",
    ]
    if include_promised_delivery:
        headers.append("ENTREGA S/PROMESA")
        row.append(promised_delivery_value)
    headers.extend(["ENTREGA REAL", "RECIBIDO ENE/2026"])
    row.extend(["01/04/2024", 100000])
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(1))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook(["T1"]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(1))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml(f"PROYECTO {project_name} - Torre 1", headers, [tuple(row)]))
    return path


def build_reconstructible_payments_workbook(path: Path) -> Path:
    headers = [
        "APTO",
        "ENCARGO FIDUCIARIO",
        "CEDULA CLIENTE",
        "NOMBRE CLIENTE",
        "RECIBOS",
        "FECHA",
        "FECHA PAGO CREDITO",
        "FECHA PAGO SUBSIDIO",
        "RECIBIDO ENE/2026",
    ]
    rows = [
        (
            "101",
            "EF-R-101",
            "9001",
            "Cliente Reconstruido",
            "NCR100-NCR101-NCR102-RCA704CR-NCR2051SUB",
            "01/01/2026-02/01/2026-03/01/2026",
            "04/01/2026",
            "05/01/2026",
            {"formula": "500000+700000+900000+1100000+1300000", "value": 4500000},
        )
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(1))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook(["T1"]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(1))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml("PROYECTO MANZANA - Torre 1", headers, rows))
    return path


def build_ambiguous_subsidies_workbook(path: Path) -> Path:
    headers = [
        "APTO",
        "ENCARGO FIDUCIARIO",
        "CEDULA CLIENTE",
        "NOMBRE CLIENTE",
        "RECIBOS",
        "FECHA",
        "FECHA PAGO SUBSIDIO",
        "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION",
        "DESEMBOLSO SUBSIDIOS GOBIERNO",
    ]
    rows = [
        (
            "101",
            "EF-SUB-AMB",
            "9001",
            "Cliente Subsidio",
            "NCR300SB-NCR301SUB",
            "",
            "10/03/2024 - 20/04/2024",
            15000000,
            20000000,
        )
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(1))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook(["T1"]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(1))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml("PROYECTO MANZANA - Torre 1", headers, rows))
    return path


def build_monetary_changes_workbook(path: Path) -> Path:
    headers = [
        "APTO",
        "ENCARGO FIDUCIARIO",
        "CEDULA CLIENTE",
        "NOMBRE CLIENTE",
        "RECIBOS",
        "FECHA",
        "CESIONES/TRASLADOS",
        "RECIBIDO MAY/2025",
    ]
    rows = [
        ("101", "EF-TR-101", "9101", "Cliente Traslado", "(NC14119TRASLADO)", "(MAY.9/25TRASLADO)", 20000000, ""),
        ("102", "EF-CE-102", "9102", "Cliente Cesion", "(NC14131CESION)", "(MAY.10/25CESION)", 30000000, ""),
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(1))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook(["T1"]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(1))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml("PROYECTO MANZANA - Torre 1", headers, rows))
    return path


def build_montecielo_transfer_date_workbook(path: Path) -> Path:
    headers = [
        "APTO",
        "ENCARGO FIDUCIARIO",
        "CEDULA CLIENTE",
        "NOMBRE CLIENTE",
        "RECIBOS",
        "RECIBOS FIDUBOGOTA",
        "FECHA",
        "CESIONES/TRASLADOS",
        "RECIBO FIDUCIA MAY/2026",
        "RECIBO FIDUCIA JUN/2026",
        "RECIBO FIDUCIA JUL/2026",
        "RECIBO FIDUCIA AGO/2026",
    ]
    rows = [
        (
            "101",
            "EF-MT-101",
            "9101",
            "Cliente Traslado",
            "NC26449TRASL.AP101MTCT1",
            "NCR48-NCR109-RC309-RC400",
            "(ABR.8/26TRASL)-MAY.4/26F-JUN.11/26F-JUL.14/26F-AGO.13/26F",
            {"formula": "2500000", "value": 2500000},
            1000000,
            2000000,
            1500000,
            1750000,
        )
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(1))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook(["T2"]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(1))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml("PROYECTO MONTECIELO - T2", headers, rows))
    return path


def build_montecielo_t1_804_observations_workbook(path: Path) -> Path:
    headers = [
        "ENCARGO FIDUCIARIO",
        "APTO",
        "AREA",
        "CEDULA CLIENTE",
        "NOMBRE CLIENTE",
        "NOMBRE CLIENTE",
        "VALOR INMUEBLE",
        "RECIBOS FIDUBOGOTA",
        "FECHA",
        "CESIONES/TRASLADOS",
        "RECIBO FIDUCIA JUL/2025",
        "RECIBO FIDUCIA AGO/2025",
        "RECIBO FIDUCIA OCT/2025",
        "RECIBO FIDUCIA DIC/2025",
        "RECIBO FIDUCIA MAR/2026",
        "OBSERVACIONES",
    ]
    main_observation = "NC26433 ABR.13/26 TERMINAC M/AC CAMILA CARDONA *DEVOL $12'687 SIN ARRAS | OBSERVACION DE TEST"
    historical_observation = "NC26433 ABR.13/26 TERMINAC M/AC CAMILA CARDONA *DEVOL $12'687 SIN ARRAS"
    rows = [
        (
            5,
            (
                "002011062714",
                "804",
                40,
                "1006321298",
                "MUNOZ OROZCO YESICA TATIANA",
                "",
                173000000,
                "NCR999",
                "MAR.13/26F",
                ".",
                "",
                "",
                "",
                "",
                12687000,
                main_observation,
            ),
        ),
        (6, ("NOVEDADES",)),
        (
            7,
            (
                "002010383530",
                "804",
                40,
                "1094960339",
                "CARDONA JARAMILLO CAMILA",
                "TERMIN/MUTUO AC SIN ARRAS",
                173000000,
                "NCR28-NCR44-NCR59-NCR152-NCR226-NCR404",
                "JUL.16/25F-AGO.4/25F-AGO.29/25F-OCT.31/25F-DIC.19/25F-MAR.4/26F",
                ".",
                8520000,
                540000,
                270000,
                540000,
                2817000,
                historical_observation,
            ),
        ),
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(1))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook(["T1"]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(1))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml_at_rows("PROYECTO MONTECIELO - Torre 1", headers, rows))
    return path


def build_multiple_clients_single_cell_workbook(path: Path) -> Path:
    headers = [
        "APTO",
        "ENCARGO FIDUCIARIO",
        "CEDULA CLIENTE",
        "NOMBRE CLIENTE",
        "TELEFONO",
        "E-MAIL",
        "RECIBIDO ENE/2026",
    ]
    rows = [
        (
            "101",
            "EF-MULTI-101",
            "7546909/41912336",
            "JORGE HERNAN LOAIZA ORTIZ/ GLORIA MILENA GIRALDO SANCHEZ",
            "3212000189-3148670777",
            "jorge@example.com/gloria@example.com",
            "",
        )
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(1))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook(["T1"]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(1))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml("PROYECTO CLIENTES - T1", headers, rows))
    return path


def build_ksmp_t7_303_observation_novelty_cross_workbook(path: Path) -> Path:
    headers = [
        "APTO",
        "ENCARGO FIDUCIARIO",
        "CEDULA CLIENTE",
        "NOMBRE CLIENTE",
        "NOMBRE CLIENTE",
        "OBSERVACIONES",
        "RECIBIDO ENE/2026",
    ]
    first = "NC6383 MAR.4/22 CESION DE LEYTON MARIA A LUCAS TENTACLES *ANTIC $15.000.000 *ARRAS $4'640"
    second = "NC6383 MAR.4/23 CESION DE LUCAS TENTACLES A CLEVELAND WATSON *ANTIC $15.000.000 *ARRAS $4'640"
    rows = [
        (5, ("303", "60007919107424", "75692028", "WATSON CLEVELAND", "", second, "")),
        (8, ("NOVEDADES",)),
        (9, ("303", "60007919155557", "75686638", "LEYTON GOMEZ MARIA JOSE", "*CESION/ARRAS", first, "")),
        (10, ("303", "60007919107412", "75686639", "TENTACLES LUCAS", "*CESION/ARRAS", first, "")),
        (11, ("303", "60007919107412", "75686639", "TENTACLES LUCAS", "*CESION/ARRAS", second, "")),
        (12, ("303", "60007919107424", "75692028", "WATSON CLEVELAND", "", second, "")),
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(1))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook(["T7"]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(1))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml_at_rows("PROYECTO KSMP - T7", headers, rows))
    return path


def build_historical_cession_integrity_workbook(
    path: Path,
    *,
    current_client_name: str = "COOPER LOIS",
    previous_client_name: str = "GREEN MAGGIE",
    observation: str = "NC1814 JUL.2/22 CESION DE GREEN MAGGIE A COOPER LOIS",
    effective_section: str = "CESIONES JUL/2022",
    current_document: str = "50164",
    previous_document: str = "19254619",
    current_email: str = "cooper.lois.50164@example.com",
    previous_email: str = "green.maggie.19254619@example.com",
    current_phone: str = "3005016400",
    previous_phone: str = "3001925461",
) -> Path:
    headers = [
        "APTO",
        "ENCARGO FIDUCIARIO",
        "CEDULA CLIENTE",
        "NOMBRE CLIENTE",
        "CELULAR",
        "E-MAIL",
        "OBSERVACIONES",
        "RECIBIDO ENE/2026",
    ]
    rows = [
        (
            "501",
            "60005086962864",
            current_document,
            current_client_name,
            current_phone,
            current_email,
            observation,
            100000,
        ),
        ("NOVEDADES", "", "", "", "", "", "", ""),
        (effective_section, "", "", "", "", "", "", ""),
        (
            "501",
            "60005086977777",
            previous_document,
            previous_client_name,
            previous_phone,
            previous_email,
            observation,
            "",
        ),
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(1))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook(["T1"]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(1))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            _xlsx_sheet_xml("PROYECTO MIRADOR DEL QUINDIO - Torre 1", headers, rows),
        )
    return path


def build_mirador_like_reconstructible_workbook(path: Path) -> Path:
    headers = [""] * 66
    headers[0] = "APTO"
    headers[1] = "ENCARGO FIDUCIARIO"
    headers[2] = "CEDULA CLIENTE"
    headers[3] = "NOMBRE CLIENTE"
    headers[4] = "RECIBOS"
    headers[5] = "FECHA"
    headers[19] = "RECIBIDO JUN/23"
    headers[22] = "RECIBIDO SEP/23"
    headers[23] = "RECIBIDO OCT/23"
    headers[25] = "RECIBIDO DIC/23"
    headers[26] = "RECIBIDO JUN/24"
    headers[49] = "RECIBIDO FIDUBOGOTA MAY/2023"
    headers[60] = "RECIBIDO FIDUBOGOTA ABR/2024"
    headers[64] = "RECIBIDO FIDUBOGOTA AGO/2024"
    headers[65] = "RECIBIDO FIDUBOGOTA SEP/2024"
    row = [""] * 66
    row[0] = "401"
    row[1] = "60003073393443"
    row[2] = "9443"
    row[3] = "CLIENTE MIRADOR"
    row[4] = "NCR4494-NCR4489-NCR4493-NCR4488-NCR4487-NCR4495-NCR4492-NCR4491-NCR4486-NCR4490"
    row[5] = "MAY.21/23F-JUN.10/23-SEP.9/23-SEP.11/23-OCT.13/23-DIC.13/23-ABR.5/24F-JUN.4/24-AGO.3/24F-SEP.5/24F"
    row[19] = 12876000
    row[22] = {"formula": "19025000+3075000", "value": 22100000}
    row[23] = 16335000
    row[25] = 2496500
    row[26] = 8648000
    row[49] = 28442000
    row[60] = 50734000
    row[64] = 26712000
    row[65] = 23829000
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types(1))
        archive.writestr("_rels/.rels", _xlsx_root_rels())
        archive.writestr("xl/workbook.xml", _xlsx_workbook(["T1"]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels(1))
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml("PROYECTO MIRADOR DEL QUINDIO - Torre 1", headers, [tuple(row)]))
    return path


def _xlsx_content_types(sheet_count: int) -> str:
    sheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{sheets}</Types>"
    )


def _xlsx_root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )


def _xlsx_workbook(sheet_names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets></workbook>"
    )


def _xlsx_workbook_rels(sheet_count: int) -> str:
    relationships = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{relationships}</Relationships>"
    )


def _xlsx_styles() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellXfs>'
        '</styleSheet>'
    )


def _xlsx_sheet_xml(title: str, headers: list[str], rows: list[tuple]) -> str:
    cells = [_inline_cell("A1", title)]
    columns = [_excel_column(index) for index in range(1, len(headers) + 1)]
    for column, header in zip(columns, headers):
        cells.append(_inline_cell(f"{column}4", header))
    for row_index, values in enumerate(rows, start=5):
        for column, value in zip(columns, values):
            if isinstance(value, dict) and "formula" in value:
                cells.append(_formula_cell(f"{column}{row_index}", value["formula"], value["value"]))
            elif isinstance(value, int):
                cells.append(_number_cell(f"{column}{row_index}", value))
            else:
                cells.append(_inline_cell(f"{column}{row_index}", value))
    row_xml = "".join(cells)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{columns[-1]}{len(rows) + 4}"/>'
        f"<sheetData>{row_xml}</sheetData>"
        '</worksheet>'
    )


def _xlsx_sheet_xml_at_rows(title: str, headers: list[str], row_entries: list[tuple[int, tuple]]) -> str:
    cells = [_inline_cell("A1", title)]
    columns = [_excel_column(index) for index in range(1, len(headers) + 1)]
    for column, header in zip(columns, headers):
        cells.append(_inline_cell(f"{column}4", header))
    max_row = 4
    for row_index, values in row_entries:
        max_row = max(max_row, row_index)
        for column, value in zip(columns, values):
            if isinstance(value, dict) and "formula" in value:
                cells.append(_formula_cell(f"{column}{row_index}", value["formula"], value["value"]))
            elif isinstance(value, int):
                cells.append(_number_cell(f"{column}{row_index}", value))
            else:
                cells.append(_inline_cell(f"{column}{row_index}", value))
    row_xml = "".join(cells)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{columns[-1]}{max_row}"/>'
        f"<sheetData>{row_xml}</sheetData>"
        '</worksheet>'
    )


def _inline_cell(reference: str, value: str) -> str:
    return f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>'


def _number_cell(reference: str, value: int) -> str:
    return f'<c r="{reference}"><v>{value}</v></c>'


def _formula_cell(reference: str, formula: str, value: int) -> str:
    return f'<c r="{reference}"><f>{formula}</f><v>{value}</v></c>'


def _excel_column(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def resolve_detected_groups_for_creation(batch: ImportBatch, user, project: Project, grouping_type: GroupingType) -> None:
    for element in batch.detected_elements.filter(
        inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        status=DetectedStructureElement.Status.NEEDS_REVIEW,
    ).select_related("resolution"):
        resolution = element.resolution
        resolution.action = ImportResolution.Action.CREATE_NEW
        resolution.target_kind = DetectedStructureElement.InferredKind.STRUCTURAL_GROUP
        resolution.parent_project = project
        resolution.parent_grouping_type = grouping_type
        resolution.create_code = element.raw_value
        resolution.create_name = element.raw_value
        resolution.resolved_by = user
        resolution.status = ImportResolution.Status.APPLIED
        resolution.save()
        element.status = DetectedStructureElement.Status.RESOLVED
        element.save(update_fields=["status", "updated_at"])


def resolve_detected_structure_to_existing(batch: ImportBatch, user, project: Project, grouping_type: GroupingType) -> None:
    for element in batch.detected_elements.filter(status=DetectedStructureElement.Status.NEEDS_REVIEW).select_related("resolution"):
        resolution = element.resolution
        resolution.action = ImportResolution.Action.ASSOCIATE_EXISTING
        resolution.resolved_by = user
        resolution.status = ImportResolution.Status.APPLIED
        if element.inferred_kind == DetectedStructureElement.InferredKind.PROJECT:
            resolution.target_kind = DetectedStructureElement.InferredKind.PROJECT
            resolution.target_project = project
        elif element.inferred_kind == DetectedStructureElement.InferredKind.GROUPING_TYPE:
            resolution.target_kind = DetectedStructureElement.InferredKind.GROUPING_TYPE
            resolution.target_grouping_type = grouping_type
        elif element.inferred_kind == DetectedStructureElement.InferredKind.STRUCTURAL_GROUP:
            group = StructuralGroup.objects.get(project=project, code=element.raw_value)
            resolution.target_kind = DetectedStructureElement.InferredKind.STRUCTURAL_GROUP
            resolution.target_structural_group = group
            resolution.parent_project = project
            resolution.parent_grouping_type = grouping_type
        else:
            continue
        resolution.save()
        element.status = DetectedStructureElement.Status.RESOLVED
        element.save(update_fields=["status", "updated_at"])


def prepare_ready_historical_batch(user):
    project = Project.objects.create(code="Manzana", name="Manzana")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        source_path = build_minimal_manzana_workbook(Path(temp_dir) / "LIBRO_Manzana.xlsx")
        result = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
        store_historical_import_file(imported_file=result.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, user, project, grouping_type)
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
    assert result.created_property_units == 4
    assert result.created_assignments == 4
    assert result.created_payments == 4
    assert PropertyUnit.objects.count() == 4
    assert FiduciaryAssignment.objects.count() == 4
    assert Payment.objects.count() == 4
    assert Client.objects.filter(source_origin=Client.SourceOrigin.HISTORICAL_IMPORT).exists()
    assert ImportAppliedRecord.objects.filter(batch=batch, entity_kind=ImportAppliedRecord.EntityKind.PAYMENT).count() == 4


def test_historical_import_materializes_secondary_client_relationships(
    client,
    tmp_path,
    accounting_admin_user,
):
    project = Project.objects.create(code="Manzana", name="Manzana")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    source_path = build_secondary_holder_workbook(tmp_path / "LIBRO_SECUNDARIO.xlsx")
    result = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)

    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    unit = PropertyUnit.objects.get(project=project, name="701")
    assignment = FiduciaryAssignment.objects.get(assignment_number="EF-SEC-701")
    primary = Client.objects.get(document_number="7001")
    secondary = Client.objects.get(document_number="7002")
    primary_ownership = UnitOwnership.objects.get(property_unit=unit, client=primary)
    secondary_ownership = UnitOwnership.objects.get(property_unit=unit, client=secondary)
    primary_holder = FiduciaryAssignmentHolder.objects.get(assignment=assignment, client=primary)
    secondary_holder = FiduciaryAssignmentHolder.objects.get(assignment=assignment, client=secondary)

    assert primary_ownership.is_active is True
    assert primary_ownership.is_primary is True
    assert secondary_ownership.is_active is True
    assert secondary_ownership.is_primary is False
    assert primary_holder.is_active is True
    assert primary_holder.is_primary is True
    assert secondary_holder.is_active is True
    assert secondary_holder.is_primary is False
    assert secondary.email == "secundario@example.com"
    assert secondary.phone == "3007002000"

    client.force_login(accounting_admin_user)
    content = client.get(reverse("fiduciary:client_detail", args=[secondary.pk])).content.decode()
    assert "EF-SEC-701" in content
    assert "701" in content
    assert "Secundario" in content
    assert "Vigente" in content


def test_historical_import_materializes_structured_client_name_and_contact(
    tmp_path,
    accounting_admin_user,
):
    project = Project.objects.create(code="KSMP", name="KSMP")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    source_path = build_client_identity_contact_workbook(tmp_path / "LIBRO_CLIENTES.xlsx")

    result = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)

    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    watson = Client.objects.get(document_number="75692028")
    assert watson.first_names == "CLEVELAND"
    assert watson.last_names_or_company == "WATSON"
    assert watson.full_name == "CLEVELAND WATSON"
    assert watson.phone == "3166488834"
    assert watson.address == "SANTIAGO LEYTON CEL: 3009898588"

    tentacles = Client.objects.get(document_number="75692029")
    assert tentacles.first_names == "LUCAS"
    assert tentacles.last_names_or_company == "TENTACLES"
    assert tentacles.full_name == "LUCAS TENTACLES"


def _finalize_operational_dates_workbook(source_path: Path, accounting_admin_user, project_name: str, assignment_number: str):
    project = Project.objects.create(code=project_name, name=project_name)
    grouping_type = GroupingType.objects.create(code=f"T-{project_name}", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    result = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()

    assert batch.status == ImportBatch.Status.READY
    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)
    return FiduciaryAssignment.objects.get(assignment_number=assignment_number)


def test_historical_import_persists_assignment_operational_dates(tmp_path, accounting_admin_user):
    source_path = build_operational_dates_workbook(
        tmp_path / "LIBRO_Fechas_Completas.xlsx",
        project_name="Manzana Fechas",
        assignment_number="EF-DATES-101",
    )

    assignment = _finalize_operational_dates_workbook(source_path, accounting_admin_user, "Manzana Fechas", "EF-DATES-101")

    assert assignment.adhesion_contract_date == date(2024, 1, 10)
    assert assignment.promise_date == date(2024, 2, 20)
    assert assignment.promised_delivery_date == date(2024, 3, 15)
    assert assignment.actual_delivery_date == date(2024, 4, 1)


def test_historical_import_accepts_missing_promised_delivery_header(tmp_path, accounting_admin_user):
    source_path = build_operational_dates_workbook(
        tmp_path / "LIBRO_Fechas_Sin_Entrega_Promesa.xlsx",
        project_name="Manzana Fechas",
        assignment_number="EF-DATES-102",
        include_promised_delivery=False,
    )

    assignment = _finalize_operational_dates_workbook(source_path, accounting_admin_user, "Manzana Fechas", "EF-DATES-102")

    assert assignment.adhesion_contract_date == date(2024, 1, 10)
    assert assignment.promise_date == date(2024, 2, 20)
    assert assignment.promised_delivery_date is None
    assert assignment.actual_delivery_date == date(2024, 4, 1)


def test_historical_import_accepts_blank_assignment_operational_date(tmp_path, accounting_admin_user):
    source_path = build_operational_dates_workbook(
        tmp_path / "LIBRO_Fechas_Entrega_Promesa_Vacia.xlsx",
        project_name="Manzana Fechas",
        assignment_number="EF-DATES-103",
        promised_delivery_value="",
    )

    assignment = _finalize_operational_dates_workbook(source_path, accounting_admin_user, "Manzana Fechas", "EF-DATES-103")

    assert assignment.adhesion_contract_date == date(2024, 1, 10)
    assert assignment.promise_date == date(2024, 2, 20)
    assert assignment.promised_delivery_date is None
    assert assignment.actual_delivery_date == date(2024, 4, 1)


def test_finalize_historical_import_materializes_reconstructed_payments_without_monthly_duplicate(
    client,
    tmp_path,
    accounting_admin_user,
):
    project = Project.objects.create(code="Manzana", name="Manzana")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    source_path = build_reconstructible_payments_workbook(tmp_path / "LIBRO_Reconstruible.xlsx")
    result = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()

    assert batch.status == ImportBatch.Status.READY
    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    payments = list(Payment.objects.order_by("exact_date", "amount"))
    assert Payment.objects.filter(date_precision=Payment.DatePrecision.MONTH).count() == 0
    assert len(payments) == 5
    assert [(payment.exact_date, payment.amount, payment.concept) for payment in payments] == [
        (date(2026, 1, 1), 500000, "ORDINARIO | Recibo NCR100"),
        (date(2026, 1, 2), 700000, "ORDINARIO | Recibo NCR101"),
        (date(2026, 1, 3), 900000, "ORDINARIO | Recibo NCR102"),
        (date(2026, 1, 4), 1100000, "CREDITO | Recibo RCA704CR"),
        (date(2026, 1, 5), 1300000, "SUBSIDIO | Recibo NCR2051SUB"),
    ]
    assert {payment.source_column for payment in payments} == {"I"}
    assert all(payment.source_had_formula for payment in payments)

    client.force_login(accounting_admin_user)
    response = client.get(reverse("fiduciary:payment_list"), {"assignment_number": "EF-R-101"})
    content = response.content.decode()
    assert response.status_code == 200
    assert "ORDINARIO | Recibo NCR100" in content
    assert "CREDITO | Recibo RCA704CR" in content
    assert "SUBSIDIO | Recibo NCR2051SUB" in content


def test_roundtrip_exported_workbook_with_separator_keeps_payments_idempotent(
    tmp_path,
    accounting_admin_user,
):
    project = Project.objects.create(code="RTPF", name="Roundtrip F")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    group = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=group, code="101", name="101")
    client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="900101",
        first_names="Cliente",
        last_names_or_company="Roundtrip",
        phone="3001234567",
    )
    UnitOwnership.objects.create(property_unit=unit, client=client, is_primary=True, start_date=date(2026, 1, 1))
    assignment = FiduciaryAssignment.objects.create(
        assignment_number="EF-ROUNDTRIP-101",
        property_unit=unit,
        start_date=date(2026, 1, 1),
        is_active=True,
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=client,
        is_primary=True,
        is_active=True,
        start_date=date(2026, 1, 1),
    )
    historical_batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        imported_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.COMPLETED,
        total_files=1,
        processed_files=1,
    )
    historical_file = ImportedFile.objects.create(
        batch=historical_batch,
        original_name="historico-roundtrip.xlsx",
        extension=".xlsx",
        size_bytes=128,
        sha256="c" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
        status=ImportedFile.Status.COMPLETED,
    )
    report_batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        imported_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.REPORTS,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.COMPLETED,
        total_files=1,
        processed_files=1,
    )
    report_file = ImportedFile.objects.create(
        batch=report_batch,
        original_name="reporte-roundtrip.xlsx",
        extension=".xlsx",
        size_bytes=128,
        sha256="d" * 64,
        file_type=ImportedFile.FileType.REPORT,
        status=ImportedFile.Status.COMPLETED,
    )
    payment_specs = [
        (date(2026, 1, 10), Decimal("1000000.00"), "ORDINARIO | Recibo HC001", Payment.Destination.CONSTRUCTORA, Payment.MovementType.HISTORICAL_PAYMENT, historical_file, "RECIBIDO"),
        (date(2026, 2, 10), Decimal("2000000.00"), "ORDINARIO | Recibo HF001", Payment.Destination.FIDUCIARIA, Payment.MovementType.HISTORICAL_PAYMENT, historical_file, "RECIBO FIDUCIA FEB/2026"),
        (date(2026, 3, 15), Decimal("3000000.00"), "Reporte consolidado", Payment.Destination.FIDUCIARIA, Payment.MovementType.ADDITION, report_file, "Reporte"),
        (date(2026, 4, 2), Decimal("4000000.00"), "Pago posterior GF", Payment.Destination.CONSTRUCTORA, Payment.MovementType.ADDITION, report_file, "Reporte"),
    ]
    for index, (exact_date, amount, concept, destination, movement_type, source_file, source_header) in enumerate(payment_specs, start=5):
        Payment.objects.create(
            assignment=assignment,
            exact_date=exact_date,
            date_precision=Payment.DatePrecision.EXACT,
            amount=amount,
            concept=concept,
            destination=destination,
            movement_type=movement_type,
            source_file=source_file,
            source_sheet="T1",
            source_row=index,
            source_column="I",
            source_header=source_header,
        )

    exported = export_historical_workbook(project)
    source_path = tmp_path / exported.filename
    source_path.write_bytes(exported.content)
    before = list(
        Payment.objects.filter(assignment=assignment).order_by("exact_date", "amount").values_list(
            "exact_date",
            "amount",
            "concept",
            "destination",
            "movement_type",
        )
    )

    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    analysis = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=analysis.imported_file, source_path=source_path)
    resolve_detected_structure_to_existing(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()

    assert batch.status == ImportBatch.Status.READY
    assert not ImportRowIssue.objects.filter(
        imported_file=analysis.imported_file,
        code__in={
            "HIST_PAYMENT_VALUE_COUNT_MISMATCH",
            "HIST_UNRECEIPTED_PAYMENT_VALUE_COUNT_MISMATCH",
            "HIST_PAYMENT_DATE_RECEIPT_MISMATCH",
        },
    ).exists()

    result = finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)
    assignment.refresh_from_db()
    after = list(
        Payment.objects.filter(assignment=assignment).order_by("exact_date", "amount").values_list(
            "exact_date",
            "amount",
            "concept",
            "destination",
            "movement_type",
        )
    )

    assert result.duplicate_payments == 2
    assert result.created_payments == 0
    assert after == before
    assert Payment.objects.filter(assignment=assignment).count() == 4

    second_batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    with pytest.raises(DuplicateHistoricalImportError):
        analyze_historical_import(batch=second_batch, file_path=source_path, grouping_type_hint="Torre")
    assert list(
        Payment.objects.filter(assignment=assignment).order_by("exact_date", "amount").values_list(
            "exact_date",
            "amount",
            "concept",
            "destination",
            "movement_type",
        )
    ) == before


def test_historical_flow_persists_ambiguous_compensation_and_government_subsidies(
    tmp_path,
    accounting_admin_user,
):
    project = Project.objects.create(code="Manzana", name="Manzana")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    source_path = build_ambiguous_subsidies_workbook(tmp_path / "LIBRO_Ambiguo.xlsx")
    result = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()

    assert batch.status == ImportBatch.Status.READY
    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    assignment = FiduciaryAssignment.objects.get(assignment_number="EF-SUB-AMB")
    payments = list(Payment.objects.filter(assignment=assignment).order_by("source_column"))

    assert [(payment.amount, payment.source_header, payment.concept) for payment in payments] == [
        (15000000, "DESEMBOLSO SUBSIDIO CAJAS DE COMPENSACION", "SUBSIDIO CAJA"),
        (20000000, "DESEMBOLSO SUBSIDIOS GOBIERNO", "SUBSIDIO GOBIERNO"),
    ]
    assert all(payment.date_precision == Payment.DatePrecision.AMBIGUOUS for payment in payments)
    assert all(payment.exact_date is None for payment in payments)
    assert all(payment.period_year is None and payment.period_month is None for payment in payments)
    assert all(payment.historical_date_values == ["10/03/2024", "20/04/2024"] for payment in payments)
    assert all(payment.historical_receipt_values == ["NCR300SB", "NCR301SUB"] for payment in payments)


def test_mirador_like_historical_flow_uses_reconstructed_payments_in_payment_views(
    client,
    tmp_path,
    accounting_admin_user,
):
    project = Project.objects.create(code="Mirador Del Quindio", name="Mirador Del Quindio")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    source_path = build_mirador_like_reconstructible_workbook(tmp_path / "LIBRO_MIRADOR_RECONSTRUIBLE.xlsx")
    result = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()

    assert batch.status == ImportBatch.Status.READY
    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    assignment = FiduciaryAssignment.objects.get(assignment_number="60003073393443")
    payments = list(Payment.objects.filter(assignment=assignment).order_by("exact_date", "amount"))
    assert len(payments) == 10
    assert Payment.objects.filter(assignment=assignment, date_precision=Payment.DatePrecision.MONTH).count() == 0
    assert [(payment.exact_date, payment.amount, payment.concept, payment.source_column) for payment in payments[:4]] == [
        (date(2023, 5, 21), 28442000, "ORDINARIO | Recibo NCR4494", "AX"),
        (date(2023, 6, 10), 12876000, "ORDINARIO | Recibo NCR4489", "T"),
        (date(2023, 9, 9), 19025000, "ORDINARIO | Recibo NCR4493", "W"),
        (date(2023, 9, 11), 3075000, "ORDINARIO | Recibo NCR4488", "W"),
    ]

    client.force_login(accounting_admin_user)
    payment_response = client.get(reverse("fiduciary:payment_list"), {"assignment_number": "60003073393443"})
    payment_content = payment_response.content.decode()
    assert payment_response.status_code == 200
    assert "ORDINARIO | Recibo NCR4493" in payment_content
    assert "ORDINARIO | Recibo NCR4488" in payment_content
    assert ">9/2023<" not in payment_content

    detail_response = client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk]))
    detail_content = detail_response.content.decode()
    assert detail_response.status_code == 200
    assert "ORDINARIO | Recibo NCR4493" in detail_content
    assert "ORDINARIO | Recibo NCR4488" in detail_content
    assert ">9/2023<" not in detail_content


def test_historical_flow_materializes_monetary_cession_and_transfer_with_historical_dates(
    client,
    tmp_path,
    accounting_admin_user,
):
    project = Project.objects.create(code="Manzana", name="Manzana")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    source_path = build_monetary_changes_workbook(tmp_path / "LIBRO_CAMBIOS_MONETARIOS.xlsx")
    result = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()

    assert batch.status == ImportBatch.Status.READY
    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    transfer_assignment = FiduciaryAssignment.objects.get(assignment_number="EF-TR-101")
    cession_assignment = FiduciaryAssignment.objects.get(assignment_number="EF-CE-102")
    transfer_payment = Payment.objects.get(assignment=transfer_assignment)
    cession_payment = Payment.objects.get(assignment=cession_assignment)
    assert (transfer_payment.exact_date, transfer_payment.amount, transfer_payment.concept, transfer_payment.source_column) == (
        date(2025, 5, 9),
        20000000,
        "TRASLADO | Recibo NC14119",
        "G",
    )
    assert (cession_payment.exact_date, cession_payment.amount, cession_payment.concept, cession_payment.source_column) == (
        date(2025, 5, 10),
        30000000,
        "CESION | Recibo NC14131",
        "G",
    )
    assert Payment.objects.filter(date_precision=Payment.DatePrecision.MONTH).count() == 0

    client.force_login(accounting_admin_user)
    payment_response = client.get(reverse("fiduciary:payment_list"), {"assignment_number": "EF-TR-101"})
    payment_content = payment_response.content.decode()
    assert payment_response.status_code == 200
    assert "TRASLADO | Recibo NC14119" in payment_content
    assert "NC14119TRASLADO" not in payment_content

    detail_response = client.get(reverse("fiduciary:assignment_detail", args=[cession_assignment.pk]))
    detail_content = detail_response.content.decode()
    assert detail_response.status_code == 200
    assert "CESION | Recibo NC14131" in detail_content
    assert "2025" in detail_content
    assert "NC14131CESION" not in detail_content


def test_montecielo_transfer_date_marker_materializes_exact_payment_without_invalid_date_issue(
    tmp_path,
    accounting_admin_user,
):
    project = Project.objects.create(code="Montecielo", name="Montecielo")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    source_path = build_montecielo_transfer_date_workbook(tmp_path / "LIBRO_MONTECIELO_T2_148.xlsx")
    result = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")

    assert not result.imported_file.row_issues.filter(
        message__icontains="No fue posible interpretar la fecha historica",
    ).exists()

    store_historical_import_file(imported_file=result.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()

    assert batch.status == ImportBatch.Status.READY
    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    assignment = FiduciaryAssignment.objects.get(assignment_number="EF-MT-101")
    payment = Payment.objects.get(assignment=assignment, concept__startswith="TRASLADO")
    assert payment.exact_date == date(2026, 4, 8)
    assert payment.amount == 2500000
    assert payment.concept == "TRASLADO | TRASL AP101 MTCT1 | Recibo NC26449"


def test_historical_cession_reconstructs_previous_and_new_relations_from_real_flow(
    client,
    tmp_path,
    accounting_admin_user,
):
    project = Project.objects.create(code="Mirador Del Quindio", name="Mirador Del Quindio")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    source_path = build_historical_cession_integrity_workbook(tmp_path / "LIBRO_MIRADOR_CESION.xlsx")
    result = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()

    assert batch.status == ImportBatch.Status.READY
    assert ImportedHistoricalNovelty.objects.filter(batch=batch).count() == 1
    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    unit = PropertyUnit.objects.get(project=project, name="501")
    previous_client = Client.objects.get(document_number="19254619")
    new_client = Client.objects.get(document_number="50164")
    previous_assignment = FiduciaryAssignment.objects.get(assignment_number="60005086977777")
    new_assignment = FiduciaryAssignment.objects.get(assignment_number="60005086962864")
    novelty = OperationalNovelty.objects.get(property_unit=unit)

    assert previous_client.full_name == "GREEN MAGGIE"
    assert previous_client.email == "green.maggie.19254619@example.com"
    assert previous_client.phone == "3001925461"
    assert previous_client.information_status == Client.InformationStatus.COMPLETE
    assert new_client.full_name == "COOPER LOIS"
    assert new_client.email == "cooper.lois.50164@example.com"
    assert new_client.phone == "3005016400"
    assert new_client.information_status == Client.InformationStatus.COMPLETE
    assert previous_assignment.is_active is False
    assert new_assignment.is_active is True
    previous_ownership = UnitOwnership.objects.get(property_unit=unit, client=previous_client)
    new_ownership = UnitOwnership.objects.get(property_unit=unit, client=new_client)
    previous_holder = FiduciaryAssignmentHolder.objects.get(assignment=previous_assignment, client=previous_client)
    new_holder = FiduciaryAssignmentHolder.objects.get(assignment=new_assignment, client=new_client)
    assert previous_ownership.is_active is False
    assert previous_ownership.is_primary is True
    assert new_ownership.is_active is True
    assert new_ownership.is_primary is True
    assert previous_holder.is_active is False
    assert previous_holder.is_primary is True
    assert new_holder.is_active is True
    assert new_holder.is_primary is True
    assert novelty.previous_client == previous_client
    assert novelty.historical_client == previous_client
    assert novelty.new_client == new_client
    assert novelty.previous_assignment == previous_assignment
    assert novelty.historical_assignment == previous_assignment
    assert novelty.new_assignment == new_assignment
    assert novelty.effective_date == date(2022, 7, 2)

    assert ImportedHistoricalObservation.objects.filter(
        assignment=new_assignment,
        detail__icontains="NC1814 JUL.2/22 CESION DE GREEN MAGGIE A COOPER LOIS",
    ).exists()

    client.force_login(accounting_admin_user)
    novelty_response = client.get(reverse("fiduciary:novelty_detail", args=[novelty.pk]))
    novelty_content = novelty_response.content.decode()
    assert novelty_response.status_code == 200
    assert "GREEN MAGGIE" in novelty_content
    assert "COOPER LOIS" in novelty_content
    assert "60005086977777" in novelty_content
    assert "60005086962864" in novelty_content
    assert "Observaciones relacionadas" in novelty_content
    assert "No hay observaciones relacionadas." not in novelty_content

    previous_client_response = client.get(reverse("fiduciary:client_detail", args=[previous_client.pk]))
    new_client_response = client.get(reverse("fiduciary:client_detail", args=[new_client.pk]))
    unit_history_response = client.get(reverse("real_estate:property_unit_history", args=[unit.pk]))
    current_assignment_response = client.get(reverse("fiduciary:assignment_detail", args=[new_assignment.pk]))
    assert previous_client_response.status_code == 200
    assert new_client_response.status_code == 200
    assert unit_history_response.status_code == 200
    assert current_assignment_response.status_code == 200
    previous_client_content = previous_client_response.content.decode()
    new_client_content = new_client_response.content.decode()
    unit_history_content = unit_history_response.content.decode()
    assert "60005086977777" in previous_client_content
    assert "Finalizado" in previous_client_content
    assert "501" in previous_client_content
    assert "60005086962864" in new_client_content
    assert "Vigente" in new_client_content
    assert "501" in new_client_content
    assert "GREEN MAGGIE" in unit_history_content
    assert "COOPER LOIS" in unit_history_content
    assert "60005086977777" in unit_history_content
    assert "60005086962864" in unit_history_content
    assert "NC1814 JUL.2/22 CESION" in current_assignment_response.content.decode()


def test_historical_cession_resolves_abbreviated_mentions_against_structured_clients(
    client,
    tmp_path,
    accounting_admin_user,
):
    project = Project.objects.create(code="Mirador Del Quindio", name="Mirador Del Quindio")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    observation_text = "NC25245 MAR.13/23 CESION DE HUGO RODRIGUEZ A DIANA RODRIGUEZ"
    source_path = build_historical_cession_integrity_workbook(
        tmp_path / "LIBRO_MIRADOR_CESION_REALISTA.xlsx",
        current_client_name="RODRIGUEZ ESPITIA DIANA MARCELA",
        previous_client_name="RODRIGUEZ ACOSTA HUGO ALFONSO",
        observation=observation_text,
        effective_section="CESIONES MAR/2023",
        current_document="52888111",
        previous_document="19254619",
        current_email="diana.rodriguez.52888111@example.com",
        previous_email="hugo.rodriguez.19254619@example.com",
    )
    result = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()

    assert batch.status == ImportBatch.Status.READY
    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    previous_client = Client.objects.get(document_number="19254619")
    new_client = Client.objects.get(document_number="52888111")
    previous_assignment = FiduciaryAssignment.objects.get(assignment_number="60005086977777")
    new_assignment = FiduciaryAssignment.objects.get(assignment_number="60005086962864")
    novelty = OperationalNovelty.objects.get()

    assert previous_client.full_name == "RODRIGUEZ ACOSTA HUGO ALFONSO"
    assert new_client.full_name == "RODRIGUEZ ESPITIA DIANA MARCELA"
    assert not Client.objects.filter(first_names="HUGO RODRIGUEZ").exists()
    assert not Client.objects.filter(first_names="DIANA RODRIGUEZ").exists()
    assert previous_client.email == "hugo.rodriguez.19254619@example.com"
    assert new_client.email == "diana.rodriguez.52888111@example.com"
    assert novelty.previous_client == previous_client
    assert novelty.new_client == new_client
    assert novelty.previous_assignment == previous_assignment
    assert novelty.new_assignment == new_assignment
    assert novelty.effective_date == date(2023, 3, 13)
    assert FiduciaryAssignmentHolder.objects.filter(assignment=previous_assignment, client=previous_client, is_active=False).exists()
    assert FiduciaryAssignmentHolder.objects.filter(assignment=new_assignment, client=new_client, is_active=True, is_primary=True).exists()

    client.force_login(accounting_admin_user)
    novelty_content = client.get(reverse("fiduciary:novelty_detail", args=[novelty.pk])).content.decode()
    assignment_content = client.get(reverse("fiduciary:assignment_detail", args=[new_assignment.pk])).content.decode()
    assert "RODRIGUEZ ACOSTA HUGO ALFONSO" in novelty_content
    assert "RODRIGUEZ ESPITIA DIANA MARCELA" in novelty_content
    assert observation_text in assignment_content


def test_historical_cession_keeps_structural_new_client_when_mention_has_extra_detail(
    tmp_path,
    accounting_admin_user,
):
    project = Project.objects.create(code="Mirador Del Quindio", name="Mirador Del Quindio")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    observation_text = (
        "NC3497 FEB.18/22 CESION DE SAMWISE MORNINGSTAR A BETH GRIFFIN "
        "*ANTIC $18.000.000 *ARRAS $4'640"
    )
    source_path = build_historical_cession_integrity_workbook(
        tmp_path / "LIBRO_MIRADOR_CESION_SAMWISE.xlsx",
        current_client_name="GRIFFIN BETH",
        previous_client_name="MORNINGSTAR SAMWISE",
        observation=observation_text,
        effective_section="*CESION/ARRAS",
        current_document="52888112",
        previous_document="19254620",
        current_email="beth.griffin.52888112@example.com",
        previous_email="samwise.morningstar.19254620@example.com",
    )
    result = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)

    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    previous_client = Client.objects.get(document_number="19254620")
    new_client = Client.objects.get(document_number="52888112")
    previous_assignment = FiduciaryAssignment.objects.get(assignment_number="60005086977777")
    new_assignment = FiduciaryAssignment.objects.get(assignment_number="60005086962864")
    novelty = OperationalNovelty.objects.get()

    assert previous_client.full_name == "MORNINGSTAR SAMWISE"
    assert new_client.full_name == "GRIFFIN BETH"
    assert novelty.previous_client == previous_client
    assert novelty.new_client == new_client
    assert novelty.previous_assignment == previous_assignment
    assert novelty.new_assignment == new_assignment
    assert not Client.objects.filter(first_names="SAMWISE MORNINGSTAR").exists()
    assert not Client.objects.filter(first_names="BETH GRIFFIN").exists()


def test_historical_novelties_reconstruct_chronological_chain_for_unit_303(
    client,
    tmp_path,
    accounting_admin_user,
):
    project = Project.objects.create(code="KSMP", name="KSMP")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    source_path = build_ksmp_unit_303_chain_workbook(tmp_path / "LIBRO_KSMP.xlsx")
    result = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)

    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    unit = PropertyUnit.objects.get(project=project, name="303")
    leyton = next(client for client in Client.objects.all() if "leyton" in normalize_text(client.full_name))
    tentacles = Client.objects.get(first_names="LUCAS", last_names_or_company="TENTACLES")
    watson = Client.objects.get(first_names="CLEVELAND", last_names_or_company="WATSON")
    leyton_assignment = FiduciaryAssignment.objects.get(assignment_number="60007919155557")
    tentacles_assignment = FiduciaryAssignment.objects.get(assignment_number="60007919107412")
    watson_assignment = FiduciaryAssignment.objects.get(assignment_number="60007919107424")
    novelties = list(OperationalNovelty.objects.filter(property_unit=unit).order_by("effective_date", "pk"))

    assert len(novelties) == 2
    first, second = novelties
    assert first.effective_date == date(2022, 3, 4)
    assert first.novelty_type == OperationalNovelty.NoveltyType.CESSION
    assert first.display_type == "Cesion"
    assert first.summary == "*CESION/ARRAS"
    assert first.detail == "NC6383 MAR.4/22 CESION DE LEYTON MARIA A LUCAS TENTACLES *ANTIC $15.000.000 *ARRAS $4'640"
    assert "|" not in first.detail
    assert first.previous_client == leyton
    assert first.new_client == tentacles
    assert first.previous_assignment == leyton_assignment
    assert first.new_assignment == tentacles_assignment
    assert normalize_text(first.previous_client.full_name) == normalize_text(leyton.full_name)
    assert first.new_client.full_name == "LUCAS TENTACLES"
    assert first.previous_assignment.assignment_number == "60007919155557"
    assert first.new_assignment.assignment_number == "60007919107412"
    assert first.source_payload["matched_previous_state"]["row"] == 63
    assert normalize_text(first.source_payload["matched_previous_state"]["client"]) == normalize_text(leyton.full_name)
    assert first.source_payload["matched_previous_state"]["assignment"] == "60007919155557"
    assert first.source_payload["matched_new_state"] == {
        "row": 70,
        "client": "LUCAS TENTACLES",
        "assignment": "60007919107412",
    }

    assert second.effective_date == date(2023, 3, 4)
    assert second.novelty_type == OperationalNovelty.NoveltyType.CESSION
    assert second.display_type == "Cesion"
    assert second.summary == "*CESION/ARRAS"
    assert second.detail == "NC6383 MAR.4/23 CESION DE LUCAS TENTACLES A CLEVELAND WATSON *ANTIC $15.000.000 *ARRAS $4'640"
    assert "|" not in second.detail
    assert second.previous_client == tentacles
    assert second.new_client == watson
    assert second.previous_assignment == tentacles_assignment
    assert second.new_assignment == watson_assignment
    assert second.previous_client.full_name == "LUCAS TENTACLES"
    assert second.new_client.full_name == "CLEVELAND WATSON"
    assert second.previous_assignment.assignment_number == "60007919107412"
    assert second.new_assignment.assignment_number == "60007919107424"
    assert second.source_payload["matched_previous_state"] == {
        "row": 70,
        "client": "LUCAS TENTACLES",
        "assignment": "60007919107412",
    }
    assert second.source_payload["matched_new_state"] == {
        "row": 21,
        "client": "CLEVELAND WATSON",
        "assignment": "60007919107424",
    }
    assert first.new_client == second.previous_client
    assert first.new_assignment == second.previous_assignment

    assert Client.objects.filter(first_names="TENTACLES LUCAS").count() == 0
    assert Client.objects.filter(first_names="WATSON CLEVELAND").count() == 0
    assert tentacles.full_name == "LUCAS TENTACLES"
    assert watson.full_name == "CLEVELAND WATSON"
    assert UnitOwnership.objects.filter(property_unit=unit, client=leyton, is_active=False).exists()
    assert UnitOwnership.objects.filter(property_unit=unit, client=tentacles, is_active=False).exists()
    assert UnitOwnership.objects.filter(property_unit=unit, client=watson, is_active=True).exists()
    assert FiduciaryAssignmentHolder.objects.filter(assignment=leyton_assignment, client=leyton, is_active=False).exists()
    assert FiduciaryAssignmentHolder.objects.filter(assignment=tentacles_assignment, client=tentacles, is_active=False).exists()
    assert FiduciaryAssignmentHolder.objects.filter(assignment=watson_assignment, client=watson, is_active=True).exists()
    assert ImportedHistoricalObservation.objects.filter(property_unit=unit, detail=first.detail).count() == 1
    assert ImportedHistoricalObservation.objects.filter(property_unit=unit, detail=second.detail).count() == 1
    assert ImportedHistoricalObservation.objects.filter(property_unit=unit).count() == 2

    client.force_login(accounting_admin_user)
    content = client.get(reverse("fiduciary:novelty_list")).content.decode()
    assert "HistÃƒÂ³rica importada" not in content
    assert "Cesion" in content
    assert "*CESION/ARRAS" in content
    assert leyton.full_name in content
    assert "LUCAS TENTACLES" in content
    assert "CLEVELAND WATSON" in content
    assert "60007919155557" in content
    assert "60007919107412" in content
    assert "60007919107424" in content
    detail_content = client.get(reverse("fiduciary:novelty_detail", args=[second.pk])).content.decode()
    assert "LUCAS TENTACLES" in detail_content
    assert "CLEVELAND WATSON" in detail_content
    assert "60007919107412" in detail_content
    assert "60007919107424" in detail_content
    assert "NC6383 MAR.4/23 CESION DE LUCAS TENTACLES A CLEVELAND WATSON" in detail_content
    assert "NC6383 MAR.4/22 CESION DE LEYTON MARIA A LUCAS TENTACLES" not in detail_content
    first_detail_content = client.get(reverse("fiduciary:novelty_detail", args=[first.pk])).content.decode()
    assert leyton.full_name in first_detail_content
    assert "LUCAS TENTACLES" in first_detail_content
    assert "60007919155557" in first_detail_content
    assert "60007919107412" in first_detail_content
    assert "NC6383 MAR.4/22 CESION DE LEYTON MARIA A LUCAS TENTACLES" in first_detail_content
    assert "NC6383 MAR.4/23 CESION DE LUCAS TENTACLES A CLEVELAND WATSON" not in first_detail_content
    tentacles_detail = client.get(reverse("fiduciary:client_detail", args=[tentacles.pk])).content.decode()
    watson_detail = client.get(reverse("fiduciary:client_detail", args=[watson.pk])).content.decode()
    assert "LUCAS TENTACLES" in tentacles_detail
    assert "60007919107412" in tentacles_detail
    assert "CLEVELAND WATSON" in watson_detail
    assert "60007919107424" in watson_detail


def test_real_ksmp_t7_303_materializes_chained_cessions_from_states(
    client,
    accounting_admin_user,
):
    if not REAL_KSMP_FILE.exists():
        pytest.skip("LIBRO_KSMP.xlsx no esta disponible en este entorno.")
    project = Project.objects.create(code="KSMP", name="Ksmp")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )

    result = analyze_historical_import(batch=batch, file_path=REAL_KSMP_FILE, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=REAL_KSMP_FILE)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.READY

    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    unit = PropertyUnit.objects.get(project=project, structural_group__code="T7", name="303")
    novelties = list(
        OperationalNovelty.objects.filter(property_unit=unit, effective_date__in=[date(2022, 3, 4), date(2023, 3, 4)])
        .select_related("previous_client", "new_client", "previous_assignment", "new_assignment")
        .order_by("effective_date", "pk")
    )
    assert len(novelties) == 2
    first, second = novelties

    assert normalize_text(first.previous_client.first_names) == normalize_text("MARIA JOSE")
    assert normalize_text(first.previous_client.last_names_or_company) == normalize_text("LEYTON GOMEZ")
    assert normalize_text(first.previous_client.full_name) == normalize_text("MARIA JOSE LEYTON GOMEZ")
    assert first.previous_assignment.assignment_number == "60007919155557"
    assert first.new_client.first_names == "LUCAS"
    assert first.new_client.last_names_or_company == "TENTACLES"
    assert first.new_client.full_name == "LUCAS TENTACLES"
    assert first.new_assignment.assignment_number == "60007919107412"
    assert first.source_payload["matched_previous_state"]["row"] == 63
    assert first.source_payload["matched_new_state"]["row"] == 70

    assert second.previous_client.full_name == "LUCAS TENTACLES"
    assert second.previous_assignment.assignment_number == "60007919107412"
    assert second.new_client.first_names == "CLEVELAND"
    assert second.new_client.last_names_or_company == "WATSON"
    assert second.new_client.full_name == "CLEVELAND WATSON"
    assert second.new_assignment.assignment_number == "60007919107424"
    assert second.source_payload["matched_previous_state"]["row"] == 70
    assert second.source_payload["matched_new_state"]["row"] == 21
    assert first.new_client_id == second.previous_client_id
    assert first.new_assignment_id == second.previous_assignment_id

    assert Client.objects.filter(first_names="LUCAS TENTACLES").count() == 0
    assert Client.objects.filter(first_names="CLEVELAND WATSON").count() == 0

    client.force_login(accounting_admin_user)
    list_content = client.get(reverse("fiduciary:novelty_list"), {"project": project.pk, "property_unit": unit.pk}).content.decode()
    assert "LUCAS TENTACLES" in list_content
    assert "CLEVELAND WATSON" in list_content
    assert "60007919107412" in list_content
    assert "60007919107424" in list_content
    first_detail = client.get(reverse("fiduciary:novelty_detail", args=[first.pk])).content.decode()
    second_detail = client.get(reverse("fiduciary:novelty_detail", args=[second.pk])).content.decode()
    assert "LUCAS TENTACLES" in first_detail
    assert "60007919107412" in first_detail
    assert "LUCAS TENTACLES" in second_detail
    assert "CLEVELAND WATSON" in second_detail
    assert "60007919107412" in second_detail
    assert "60007919107424" in second_detail


def test_abbreviated_name_resolution_does_not_choose_ambiguous_candidates():
    first = Client(first_names="RODRIGUEZ ACOSTA HUGO ALFONSO", last_names_or_company="")
    second = Client(first_names="RODRIGUEZ PEREZ HUGO ANDRES", last_names_or_company="")

    assert _unique_client_matching_mention("HUGO RODRIGUEZ", [first, second]) is None


def test_natural_name_mentions_resolve_to_structured_historical_client_order():
    tentacles = Client(first_names="LUCAS", last_names_or_company="TENTACLES")
    watson = Client(first_names="CLEVELAND", last_names_or_company="WATSON")

    assert _unique_client_matching_mention("LUCAS TENTACLES", [tentacles]) is tentacles
    assert _unique_client_matching_mention("CLEVELAND WATSON", [watson]) is watson
    assert _unique_client_matching_mention("TENTACLES LUCAS", [tentacles]) is tentacles
    assert _unique_client_matching_mention("WATSON CLEVELAND", [watson]) is watson
    assert _canonical_client_name_matches(tentacles, "TENTACLES LUCAS")
    assert _canonical_client_name_matches(watson, "WATSON CLEVELAND")


def test_historical_event_effective_date_uses_event_date_not_import_date():
    assert _historical_event_effective_date("NC14119 MAY.9/25TRASLADO", "", []) == date(2025, 5, 9)
    assert _historical_event_effective_date("", "", [{"value": "FEB.3/25INCLUSION DE TITULAR"}]) == date(2025, 2, 3)
    assert _historical_event_effective_date("Observacion con fecha 02/09/2026 sin evento", "", []) is None


def test_historical_payment_date_strips_cession_transfer_markers_before_parsing():
    assert _parse_historical_payment_date("(ABR.8/26TRASL)") == date(2026, 4, 8)
    assert _parse_historical_payment_date("ABR.8/26CESION") == date(2026, 4, 8)
    assert _parse_historical_payment_date("MAY.4/26F") == date(2026, 5, 4)


def test_historical_finalization_materializes_zero_amount_payment(accounting_admin_user):
    project = Project.objects.create(code="P0", name="Proyecto Cero")
    unit = PropertyUnit.objects.create(project=project, code="101", name="101")
    assignment = FiduciaryAssignment.objects.create(
        property_unit=unit,
        assignment_number="EF-ZERO",
        start_date="2026-01-01",
    )
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.READY,
        total_files=1,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="historico.xlsx",
        extension=".xlsx",
        size_bytes=1,
        sha256="z" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)

    result = context._create_historical_payment(
        assignment=assignment,
        amount=0,
        movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
        source_file=imported_file,
        source_sheet="T1",
        source_row=121,
        date_precision=Payment.DatePrecision.EXACT,
        exact_date=date(2026, 4, 14),
        concept="CESION | Recibo NC26448CESION",
        source_column="R",
        source_header="CESIONES/TRASLADOS",
    )
    context.flush_payments()

    assert result.status == "created"
    payment = Payment.objects.get(assignment=assignment)
    assert payment.amount == 0
    assert payment.concept == "CESION | Recibo NC26448CESION"


def test_historical_finalization_keeps_unit_only_row_without_orphan_relations(accounting_admin_user):
    project = Project.objects.create(code="P-UNIT", name="Proyecto Unidad")
    group = StructuralGroup.objects.create(
        project=project,
        grouping_type=GroupingType.objects.create(code="T", name="Torre"),
        code="T1",
        name="T1",
    )
    unit = PropertyUnit.objects.create(project=project, structural_group=group, code="101", name="101")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.READY,
        total_files=1,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="historico.xlsx",
        extension=".xlsx",
        size_bytes=1,
        sha256="u" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    imported_file.sheet_results.create(sheet_name="T1", sheet_index=1)
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)
    context._remember_unit_context(unit, "101", "T1")
    row = SimpleNamespace(
        sheet_name="T1",
        row_number=5,
        grouping_name="T1",
        grouping_code="T1",
        unit_code="101",
        unit_name="101",
        area=Decimal("55.20"),
        property_value=Decimal("192172500"),
        financial_entity="",
        assignment=None,
        clients=[],
        payments=[],
        reconstructed_payments=[],
        observation="Observacion sin encargo",
    )
    workbook = SimpleNamespace(sheets=[SimpleNamespace(name="T1", rows=[row])], statistics=SimpleNamespace(valid_rows=1))

    context.import_rows(workbook)

    unit.refresh_from_db()
    assert unit.area == Decimal("55.20")
    assert unit.property_value == Decimal("192172500")
    assert Client.objects.count() == 0
    assert FiduciaryAssignment.objects.count() == 0
    assert ImportedHistoricalObservation.objects.count() == 0


@pytest.mark.parametrize(
    ("sheet_name", "row_number", "unit_code", "assignment_number", "summary"),
    [
        ("T1", 135, "135", "EF-135", "TERMIN/MUTUO AC SIN $$ ENE/2026"),
        ("T1", 136, "136", "EF-136", "TERMIN/MUTUO AC SIN $$ ENE/2026"),
        ("T2", 162, "162", "EF-162", "TERMIN/MUTUO AC SIN $$ ENE/2026"),
    ],
)
def test_summary_only_historical_novelty_preserves_relation_without_fake_detail(
    accounting_admin_user,
    sheet_name,
    row_number,
    unit_code,
    assignment_number,
    summary,
):
    project = Project.objects.create(code=f"P-SUM-{row_number}", name=f"Proyecto Resumen {row_number}")
    group = StructuralGroup.objects.create(
        project=project,
        grouping_type=GroupingType.objects.create(code="T", name="Torre"),
        code=sheet_name,
        name=sheet_name,
    )
    unit = PropertyUnit.objects.create(project=project, structural_group=group, code=unit_code, name=unit_code)
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.READY,
        total_files=1,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="historico.xlsx",
        extension=".xlsx",
        size_bytes=1,
        sha256="s" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    sheet_result = ImportedSheetResult.objects.create(imported_file=imported_file, sheet_name=sheet_name, sheet_index=1)
    novelty = ImportedHistoricalNovelty.objects.create(
        batch=batch,
        imported_file=imported_file,
        sheet_result=sheet_result,
        row_number=row_number,
        project_name=project.name,
        grouping_name=group.name,
        grouping_code=group.code,
        grouping_type_name=group.grouping_type.name,
        unit_code=unit.code,
        assignment_number=assignment_number,
        original_cells=[
            {"header": "ENCARGO FIDUCIARIO", "value": assignment_number, "formula": False},
            {"header": "APTO", "value": unit_code, "formula": False},
            {"header": "CEDULA CLIENTE", "value": f"{row_number}{row_number}", "formula": False},
            {"header": "NOMBRE CLIENTE", "value": "SANTIAGO LEYTON", "formula": False},
            {"header": "TIPO NOVEDAD", "value": summary, "formula": False},
        ],
    )
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)
    context.units_by_context[(normalize_text(group.name), normalize_text(unit.code))] = unit

    context._historical_novelty_observation(novelty)

    assignment = FiduciaryAssignment.objects.get(assignment_number=assignment_number)
    client = Client.objects.get(document_number=f"{row_number}{row_number}")
    operational = OperationalNovelty.objects.get(source_novelty=novelty)
    observation = ImportedHistoricalObservation.objects.get(source_novelty=novelty)

    assert FiduciaryAssignmentHolder.objects.filter(assignment=assignment, client=client, is_active=False).exists()
    assert UnitOwnership.objects.filter(property_unit=unit, client=client, is_active=False).exists()
    assert operational.summary == summary
    assert operational.detail == ""
    assert observation.summary == summary
    assert observation.detail == ""


def test_generic_main_observation_does_not_create_historical_novelty_when_real_novelty_exists(
    accounting_admin_user,
):
    project = Project.objects.create(code="P-OBS-NOV", name="Proyecto Observacion")
    group = StructuralGroup.objects.create(
        project=project,
        grouping_type=GroupingType.objects.create(code="T", name="Torre"),
        code="T1",
        name="T1",
    )
    unit = PropertyUnit.objects.create(project=project, structural_group=group, code="804", name="804")
    assignment = FiduciaryAssignment.objects.create(
        property_unit=unit,
        assignment_number="EF-804",
        start_date="2026-01-01",
    )
    client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="804",
        first_names="SANDY",
        last_names_or_company="PARKER",
        phone="3000000000",
    )
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=client,
        is_primary=True,
        is_active=False,
        start_date="2026-01-01",
        end_date="2026-01-31",
    )
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.READY,
        total_files=1,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="historico.xlsx",
        extension=".xlsx",
        size_bytes=1,
        sha256="o" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    sheet_result = ImportedSheetResult.objects.create(imported_file=imported_file, sheet_name="T1", sheet_index=1)
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)
    row = SimpleNamespace(
        observation="OBSERVACION DE TEST",
        sheet_name="T1",
        row_number=804,
        clients=[SimpleNamespace(is_primary=True)],
    )
    context._main_row_observation(row, unit, assignment, sheet_result)
    context._remember_historical_row_state(
        row,
        unit,
        assignment,
        [client],
        sheet_result,
    )
    novelty = ImportedHistoricalNovelty.objects.create(
        batch=batch,
        imported_file=imported_file,
        sheet_result=sheet_result,
        row_number=135,
        project_name=project.name,
        grouping_name=group.name,
        grouping_code=group.code,
        grouping_type_name=group.grouping_type.name,
        unit_code=unit.code,
        assignment_number=assignment.assignment_number,
        original_cells=[
            {"header": "ENCARGO FIDUCIARIO", "value": assignment.assignment_number, "formula": False},
            {"header": "APTO", "value": unit.code, "formula": False},
            {"header": "CEDULA CLIENTE", "value": client.document_number, "formula": False},
            {"header": "NOMBRE CLIENTE", "value": client.full_name, "formula": False},
            {"header": "TIPO NOVEDAD", "value": "TERMIN/MUTUO AC SIN $$ ENE/2026", "formula": False},
        ],
    )
    context.units_by_context[(normalize_text(group.name), normalize_text(unit.code))] = unit

    context._historical_novelty_observation(novelty)

    assert OperationalNovelty.objects.count() == 1
    assert OperationalNovelty.objects.get().other_type == "TERMINACION"
    assert not OperationalNovelty.objects.filter(summary="OBSERVACION DE TEST").exists()
    assert ImportedHistoricalObservation.objects.filter(detail="OBSERVACION DE TEST").count() == 1


def test_real_montecielo_t1_804_observations_and_termination_survive_full_import(
    tmp_path,
    accounting_admin_user,
):
    source_path = build_montecielo_t1_804_observations_workbook(tmp_path / "LIBRO_MONTECIELO_T1_804.xlsx")
    project = Project.objects.create(code="MONTECIELO", name="Montecielo")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )

    analysis = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    parsed_row = next(row for sheet in analysis.preview.workbook.sheets for row in sheet.rows if row.unit_code == "804")
    assert "OBSERVACION DE TEST" in parsed_row.observation
    assert any(novelty.unit_code == "804" and novelty.row_number == 7 for sheet in analysis.preview.workbook.sheets for novelty in sheet.novelties)
    store_historical_import_file(imported_file=analysis.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    for resolution in ImportResolution.objects.filter(
        detected_element__batch=batch,
        detected_element__inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        status=ImportResolution.Status.APPLIED,
    ):
        auto_resolve_units_for_group_resolution(resolution, user=accounting_admin_user)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.READY

    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    unit = PropertyUnit.objects.get(project=project, name="804")
    observations = list(
        ImportedHistoricalObservation.objects.filter(property_unit=unit)
        .exclude(origin="historical_novelty")
        .order_by("source_order", "pk")
    )
    novelties = list(OperationalNovelty.objects.filter(property_unit=unit).order_by("source_row", "pk"))

    assert len(observations) == 1
    assert [observation.detail for observation in observations] == [
        "OBSERVACION DE TEST",
    ]
    assert len(novelties) == 1
    assert novelties[0].other_type == "TERMINACION"
    assert novelties[0].summary == "TERMIN/MUTUO AC SIN ARRAS"
    assert "CAMILA CARDONA" in novelties[0].detail
    assert not OperationalNovelty.objects.filter(property_unit=unit, summary="OBSERVACION DE TEST").exists()


@pytest.mark.skipif(not REAL_MONTECIELO_FILE.exists(), reason="Libro real de Montecielo no disponible.")
def test_real_montecielo_workbook_t1_804_materializes_observations_and_termination(
    tmp_path,
    accounting_admin_user,
):
    source_path = tmp_path / REAL_MONTECIELO_FILE.name
    try:
        shutil.copyfile(REAL_MONTECIELO_FILE, source_path)
    except PermissionError:
        pytest.skip("Libro real de Montecielo bloqueado por el sistema.")
    project = Project.objects.create(code="MONTECIELO", name="Montecielo")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )

    analysis = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    parsed_rows = [
        row
        for sheet in analysis.preview.workbook.sheets
        for row in sheet.rows
        if sheet.name == "T1" and str(row.unit_code).strip() == "804"
    ]
    parsed_novelties = [
        novelty
        for sheet in analysis.preview.workbook.sheets
        for novelty in sheet.novelties
        if sheet.name == "T1" and str(novelty.unit_code).strip() == "804"
    ]
    assert any(row.row_number == 64 and "TEST" in row.observation for row in parsed_rows)
    assert any(novelty.row_number == 146 for novelty in parsed_novelties)

    store_historical_import_file(imported_file=analysis.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.READY

    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    unit = PropertyUnit.objects.get(project=project, structural_group__code="T1", name="804")
    observations = list(ImportedHistoricalObservation.objects.filter(property_unit=unit).order_by("source_row", "source_order", "pk"))
    novelties = list(OperationalNovelty.objects.filter(property_unit=unit).order_by("source_row", "pk"))

    assert len(observations) == 1
    assert any("TEST" in observation.detail for observation in observations)
    assert len(novelties) == 1
    assert novelties[0].other_type == "TERMINACION"
    assert "CAMILA CARDONA" in novelties[0].detail
    assert not OperationalNovelty.objects.filter(property_unit=unit, summary__icontains="TEST").exists()


def test_historical_import_splits_multiple_clients_from_single_cell(tmp_path, accounting_admin_user):
    source_path = build_multiple_clients_single_cell_workbook(tmp_path / "LIBRO_CLIENTES_MULTIPLES.xlsx")

    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )

    analysis = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    row = next(row for sheet in analysis.preview.workbook.sheets for row in sheet.rows if row.unit_code == "101")

    assert [(client.name, client.document_number, client.phone, client.email) for client in row.clients] == [
        ("JORGE HERNAN LOAIZA ORTIZ", "7546909", "3212000189", "jorge@example.com"),
        ("GLORIA MILENA GIRALDO SANCHEZ", "41912336", "3148670777", "gloria@example.com"),
    ]

    context = _FinalizationContext(batch=batch, imported_file=analysis.imported_file, user=accounting_admin_user)
    persisted_clients = [context._client_for_historical_client(client) for client in row.clients]

    assert not Client.objects.filter(first_names__icontains="/").exists()
    first, second = persisted_clients
    assert [client.document_number for client in persisted_clients] == ["7546909", "41912336"]
    assert [(client.phone, client.email) for client in persisted_clients] == [
        ("3212000189", "jorge@example.com"),
        ("3148670777", "gloria@example.com"),
    ]
    assert "/" not in first.full_name
    assert "/" not in second.full_name


def test_ksmp_t7_303_observation_fragments_that_exist_as_novelties_are_not_extra_observations(
    tmp_path,
    accounting_admin_user,
):
    source_path = build_ksmp_t7_303_observation_novelty_cross_workbook(tmp_path / "LIBRO_KSMP_T7_303.xlsx")
    project = Project.objects.create(code="KSMP", name="KSMP")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )

    analysis = analyze_historical_import(batch=batch, file_path=source_path, grouping_type_hint="Torre")
    assert analysis.preview.workbook.statistics.historical_novelties_found == 4
    store_historical_import_file(imported_file=analysis.imported_file, source_path=source_path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.READY

    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    unit = PropertyUnit.objects.get(project=project, name="303")
    leyton = Client.objects.get(document_number="75686638")
    tentacles = Client.objects.get(document_number="75686639")
    watson = Client.objects.get(document_number="75692028")
    novelties = OperationalNovelty.objects.filter(property_unit=unit, novelty_type=OperationalNovelty.NoveltyType.CESSION)

    assert novelties.count() == 2
    assert novelties.filter(previous_client=leyton, new_client=tentacles).count() == 1
    assert novelties.filter(previous_client=tentacles, new_client=watson).count() == 1
    assert not ImportedHistoricalObservation.objects.filter(
        property_unit=unit,
        source_novelty__isnull=True,
        client__in=[leyton, tentacles, watson],
    ).exists()
    assert not ImportedHistoricalObservation.objects.filter(
        property_unit=unit,
        source_novelty__isnull=True,
        detail__icontains="NC6383",
    ).exists()


def test_real_montecielo_ui_upload_keeps_t1_147_clients_separate(client, accounting_admin_user):
    assert REAL_MONTECIELO_AVAILABLE_FILE.exists()
    project = Project.objects.create(code="Montecielo", name="Montecielo")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    client.force_login(accounting_admin_user)

    with REAL_MONTECIELO_AVAILABLE_FILE.open("rb") as handle:
        uploaded = SimpleUploadedFile(
            "LIBRO MONTECIELO.xlsx",
            handle.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    response = client.post(
        reverse("fiduciary:historical_import_create"),
        {"file": [uploaded], "grouping_type_hint": "Torre"},
        follow=True,
    )

    assert response.status_code == 200
    batch = ImportBatch.objects.latest("pk")
    imported_file = batch.files.get()
    novelty = imported_file.historical_novelties.get(sheet_result__sheet_name="T1", row_number=147)
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)

    clients = context._clients_from_imported_novelty(novelty)

    assert [(item.document_number, item.phone, item.email) for item in clients] == [
        ("7546909", "3212000189", "jorgegiraldo1505@gmail.com"),
        ("41912336", "3148670777", "milenaloaiza1505@gmail.com"),
    ]
    assert not Client.objects.filter(document_number__contains="/").exists()
    assert not Client.objects.filter(Q(first_names__contains="/") | Q(last_names_or_company__contains="/")).exists()

    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)
    context.materialize_structure()
    context._historical_novelty_observation(novelty)

    unit = PropertyUnit.objects.get(project=project, structural_group__code="T1", name="908")
    assignment = FiduciaryAssignment.objects.get(assignment_number="002010383656")
    jorge = Client.objects.get(document_number="7546909")
    gloria = Client.objects.get(document_number="41912336")

    assert assignment.property_unit == unit
    assert FiduciaryAssignmentHolder.objects.filter(assignment=assignment, client=jorge, is_primary=True).exists()
    assert FiduciaryAssignmentHolder.objects.filter(assignment=assignment, client=gloria, is_primary=False).exists()
    assert UnitOwnership.objects.filter(property_unit=unit, client=jorge, is_primary=True).exists()
    assert UnitOwnership.objects.filter(property_unit=unit, client=gloria, is_primary=False).exists()
    assert OperationalNovelty.objects.filter(property_unit=unit, historical_client=jorge).count() == 1
    assert not OperationalNovelty.objects.filter(
        Q(historical_client=gloria) | Q(previous_client=gloria) | Q(new_client=gloria),
        property_unit=unit,
    ).exists()
    assert not ImportedHistoricalObservation.objects.filter(property_unit=unit).exists()


def test_real_ksmp_t7_303_client_views_show_novelties_without_duplicate_observations(
    client,
    accounting_admin_user,
):
    assert REAL_KSMP_FILE.exists()
    project = Project.objects.create(code="KSMP", name="KSMP")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )

    analysis = analyze_historical_import(batch=batch, file_path=REAL_KSMP_FILE, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=analysis.imported_file, source_path=REAL_KSMP_FILE)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.READY

    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    unit = PropertyUnit.objects.get(project=project, structural_group__code="T7", name="303")
    leyton = Client.objects.get(document_number="75686638")
    tentacles = Client.objects.get(first_names="LUCAS", last_names_or_company="TENTACLES", document_number__isnull=True)
    watson = Client.objects.get(document_number="75692028")

    assert OperationalNovelty.objects.filter(property_unit=unit, previous_client=leyton, new_client=tentacles).count() == 1
    assert OperationalNovelty.objects.filter(property_unit=unit, previous_client=tentacles, new_client=watson).count() == 1
    assert not ImportedHistoricalObservation.objects.filter(property_unit=unit, source_novelty__isnull=False).exists()
    assert not ImportedHistoricalObservation.objects.filter(property_unit=unit, operational_novelty__isnull=False).exists()

    client.force_login(accounting_admin_user)
    expected_counts = [(leyton, 1), (tentacles, 2), (watson, 1)]
    for target_client, novelty_count in expected_counts:
        response = client.get(reverse("fiduciary:client_detail", args=[target_client.pk]))
        assert response.status_code == 200
        assert response.context["novelties"].filter(property_unit=unit).count() == novelty_count
        assert response.context["related_observations"].filter(property_unit=unit).count() == 0


def test_historical_section_text_creates_operational_novelty_without_observation(
    accounting_admin_user,
):
    project = Project.objects.create(code="P-OBS-ONLY", name="Proyecto Observacion Historica")
    group = StructuralGroup.objects.create(
        project=project,
        grouping_type=GroupingType.objects.create(code="T", name="Torre"),
        code="T1",
        name="T1",
    )
    unit = PropertyUnit.objects.create(project=project, structural_group=group, code="804", name="804")
    assignment = FiduciaryAssignment.objects.create(
        property_unit=unit,
        assignment_number="EF-804",
        start_date="2026-01-01",
    )
    client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number="805",
        first_names="SANDY",
        last_names_or_company="PARKER",
        phone="3000000000",
    )
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.READY,
        total_files=1,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="historico.xlsx",
        extension=".xlsx",
        size_bytes=1,
        sha256="p" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    sheet_result = ImportedSheetResult.objects.create(imported_file=imported_file, sheet_name="T1", sheet_index=1)
    novelty = ImportedHistoricalNovelty.objects.create(
        batch=batch,
        imported_file=imported_file,
        sheet_result=sheet_result,
        row_number=140,
        project_name=project.name,
        grouping_name=group.name,
        grouping_code=group.code,
        grouping_type_name=group.grouping_type.name,
        unit_code=unit.code,
        assignment_number=assignment.assignment_number,
        original_cells=[
            {"header": "ENCARGO FIDUCIARIO", "value": assignment.assignment_number, "formula": False},
            {"header": "APTO", "value": unit.code, "formula": False},
            {"header": "CEDULA CLIENTE", "value": client.document_number, "formula": False},
            {"header": "NOMBRE CLIENTE", "value": client.full_name, "formula": False},
            {"header": "OBSERVACIONES", "value": "OBSERVACION HISTORICA REAL", "formula": False},
            {"header": "__historical_section__", "value": "NOVEDADES", "formula": False},
        ],
    )
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)
    context.units_by_context[(normalize_text(group.name), normalize_text(unit.code))] = unit

    context._historical_novelty_observation(novelty)

    operational = OperationalNovelty.objects.get(property_unit=unit)
    assert operational.detail == "OBSERVACION HISTORICA REAL"
    assert operational.source_observation is None
    assert ImportedHistoricalObservation.objects.count() == 0


def test_historical_event_fragments_split_only_complete_events():
    first = "NC1 MAR.4/22 CESION DE A A B"
    second = "NC2 MAR.4/23 CESION DE B A C"

    assert _historical_event_fragments(f"{first} | {second}") == [first, second]
    assert _historical_event_fragments(f"{first} - {second}") == [first, second]
    assert _historical_event_fragments("NC1 MAR.4/22 CESION DE A A B - NCR1-NCR2-NCR3") == [
        "NC1 MAR.4/22 CESION DE A A B - NCR1-NCR2-NCR3"
    ]


@pytest.mark.parametrize(
    ("text", "expected_type", "expected_other"),
    [
        ("*CESION/ARRAS", OperationalNovelty.NoveltyType.CESSION, ""),
        ("*TRASLADO", OperationalNovelty.NoveltyType.OTHER, "TRASLADO"),
        ("FEB.3/25 INCLUSION DE TITULAR", OperationalNovelty.NoveltyType.OTHER, "INCLUSION"),
        ("EXCLUSION DE TITULAR", OperationalNovelty.NoveltyType.EXCLUSION, ""),
        ("*DESISTIM/ARRAS", OperationalNovelty.NoveltyType.OTHER, "DESISTIMIENTO"),
    ],
)
def test_historical_novelty_type_uses_real_event_family(text, expected_type, expected_other):
    assert _historical_novelty_type(text) == (expected_type, expected_other)


def test_real_mirador_workbook_materializes_reconstructed_payments_from_partial_month_groups(
    client,
    accounting_admin_user,
):
    if not REAL_MIRADOR_FILE.exists():
        pytest.skip("LIBRO_MIRADOR DEL QUINDIO.xlsx no esta disponible en este entorno.")
    try:
        with REAL_MIRADOR_FILE.open("rb") as stream:
            stream.read(1)
    except PermissionError:
        pytest.skip("LIBRO_MIRADOR DEL_QUINDIO.xlsx esta bloqueado por el sistema.")
    project = Project.objects.create(code="Mirador Del Quindio", name="Mirador Del Quindio")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    result = analyze_historical_import(batch=batch, file_path=REAL_MIRADOR_FILE, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=REAL_MIRADOR_FILE)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()

    assert batch.status == ImportBatch.Status.READY
    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    assignment = FiduciaryAssignment.objects.get(assignment_number="60003073391659")
    payments = list(Payment.objects.filter(assignment=assignment).order_by("exact_date", "amount"))
    assert [(payment.exact_date, payment.amount, payment.concept, payment.source_column) for payment in payments] == [
        (date(2024, 2, 3), 66888500, "ORDINARIO | Recibo NCR4242", "BG"),
        (date(2024, 2, 11), 4848000, "ORDINARIO | Recibo NCR4239", "BG"),
        (date(2024, 2, 18), 4684000, "ORDINARIO | Recibo NCR4240", "BG"),
        (date(2024, 2, 19), 5752000, "ORDINARIO | Recibo NCR4241", "BG"),
        (date(2024, 7, 10), 110000000, "CREDITO | Recibo NCR4243", "CD"),
    ]
    assert Payment.objects.filter(assignment=assignment, date_precision=Payment.DatePrecision.MONTH).count() == 0

    assignment_t2 = FiduciaryAssignment.objects.get(assignment_number="60003073403429")
    t2_payments = list(Payment.objects.filter(assignment=assignment_t2).order_by("exact_date", "amount"))
    assert [(payment.exact_date, payment.amount, payment.concept, payment.source_column) for payment in t2_payments] == [
        (date(2024, 7, 24), 80000000, "CREDITO | Recibo NCR5865CR", "CD"),
        (date(2024, 11, 24), 583000, "ORDINARIO | Recibo NCR5863", "BP"),
        (date(2024, 11, 25), 18171000, "ORDINARIO | Recibo NCR5862", "BP"),
        (date(2024, 11, 26), 11952500, "ORDINARIO | Recibo NCR5864", "BP"),
        (date(2024, 11, 28), 66466000, "ORDINARIO | Recibo NCR5861", "BP"),
        (date(2024, 11, 29), 15000000, "SUBSIDIO | Recibo NCR5866SB", "CE"),
    ]
    assert Payment.objects.filter(assignment=assignment_t2, date_precision=Payment.DatePrecision.MONTH).count() == 0
    assert sum(payment.amount for payment in t2_payments) == 192172500

    assignment_t5 = FiduciaryAssignment.objects.get(assignment_number="60003073442270")
    t5_payments = list(Payment.objects.filter(assignment=assignment_t5).order_by("exact_date", "amount"))
    assert [(payment.exact_date, payment.amount, payment.concept, payment.source_column) for payment in t5_payments] == [
        (date(2022, 12, 30), 95000000, "CREDITO | Recibo NCR10516CR", "CD"),
        (date(2023, 5, 3), 10402000, "ORDINARIO | Recibo NCR10515", "AX"),
        (date(2023, 5, 7), 7508000, "ORDINARIO | Recibo NCR10513", "AX"),
        (date(2023, 5, 7), 20000000, "SUBSIDIO | Recibo NCR10517SUB", "CE"),
        (date(2023, 5, 11), 19855000, "ORDINARIO | Recibo NCR10514", "AX"),
        (date(2023, 5, 28), 7463000, "ORDINARIO | Recibo NCR10512", "AX"),
    ]
    assert Payment.objects.filter(assignment=assignment_t5, date_precision=Payment.DatePrecision.MONTH).count() == 0
    assert sum(payment.amount for payment in t5_payments) == 160228000

    client.force_login(accounting_admin_user)
    payment_response = client.get(reverse("fiduciary:payment_list"), {"assignment_number": "60003073391659"})
    payment_content = payment_response.content.decode()
    assert payment_response.status_code == 200
    assert "ORDINARIO | Recibo NCR4242" in payment_content
    assert "3/02/2024" in payment_content

    detail_response = client.get(reverse("fiduciary:assignment_detail", args=[assignment.pk]))
    detail_content = detail_response.content.decode()
    assert detail_response.status_code == 200
    assert "ORDINARIO | Recibo NCR4242" in detail_content
    assert "2024" in detail_content
    assert ">2/2024<" not in detail_content


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
    project = Project.objects.get(code="Manzana")
    conflicting_unit = PropertyUnit.objects.create(project=project, code="CONFLICT", name="CONFLICT")
    FiduciaryAssignment.objects.create(
        assignment_number="EF-T1-101",
        property_unit=conflicting_unit,
        start_date="2026-01-01",
    )

    with pytest.raises(HistoricalImportFinalizationError):
        finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.FAILED
    assert Payment.objects.count() == 0
    assert PropertyUnit.objects.filter(code="T1-101").count() == 0
    assert ImportAppliedRecord.objects.filter(batch=batch).count() == 0


def test_finalize_view_post_materializes_synchronously_without_background(client, accounting_admin_user):
    batch = prepare_ready_historical_batch(accounting_admin_user)
    before = {
        "groups": StructuralGroup.objects.count(),
        "units": PropertyUnit.objects.count(),
        "clients": Client.objects.count(),
        "assignments": FiduciaryAssignment.objects.count(),
        "ownerships": UnitOwnership.objects.count(),
        "holders": FiduciaryAssignmentHolder.objects.count(),
    }
    client.force_login(accounting_admin_user)
    url = reverse("fiduciary:historical_import_finalize", args=[batch.pk])

    response = client.get(url)
    assert response.status_code == 200
    assert b"Ejecutar importacion definitiva" in response.content

    response = client.post(url, follow=True)

    assert response.status_code == 200
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.COMPLETED
    assert StructuralGroup.objects.count() > before["groups"]
    assert PropertyUnit.objects.count() > before["units"]
    assert Client.objects.count() > before["clients"]
    assert FiduciaryAssignment.objects.count() > before["assignments"]
    assert UnitOwnership.objects.count() > before["ownerships"]
    assert FiduciaryAssignmentHolder.objects.count() > before["holders"]
    assert "data-progress-url" not in response.content.decode()


def test_finalize_view_post_materializes_manzana_t1_t2_without_background(client, tmp_path, accounting_admin_user):
    path = build_minimal_manzana_workbook(tmp_path / "LIBRO_Manzana.xlsx")
    project = Project.objects.create(code="Manzana", name="Manzana")
    grouping_type = GroupingType.objects.create(code="Torre", name="Torre")
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )
    result = analyze_historical_import(batch=batch, file_path=path, grouping_type_hint="Torre")
    store_historical_import_file(imported_file=result.imported_file, source_path=path)
    resolve_detected_groups_for_creation(batch, accounting_admin_user, project, grouping_type)
    auto_resolve_new_units(batch, user=accounting_admin_user)
    update_batch_resolution_state(batch)
    batch.refresh_from_db()
    imported_file = batch.files.get()
    stored_path = settings.MEDIA_ROOT / imported_file.stored_path
    assert batch.status == ImportBatch.Status.READY
    assert stored_path.exists()
    assert stored_path.is_file()
    assert stored_path.stat().st_size > 0

    before = {
        "groups": StructuralGroup.objects.count(),
        "units": PropertyUnit.objects.count(),
        "clients": Client.objects.count(),
        "assignments": FiduciaryAssignment.objects.count(),
        "ownerships": UnitOwnership.objects.count(),
        "holders": FiduciaryAssignmentHolder.objects.count(),
    }
    client.force_login(accounting_admin_user)

    response = client.post(reverse("fiduciary:historical_import_finalize", args=[batch.pk]), follow=True)

    assert response.status_code == 200
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.COMPLETED
    assert StructuralGroup.objects.count() == before["groups"] + 2
    assert PropertyUnit.objects.count() == before["units"] + 4
    assert Client.objects.count() == before["clients"] + 4
    assert FiduciaryAssignment.objects.count() == before["assignments"] + 4
    assert UnitOwnership.objects.count() == before["ownerships"] + 4
    assert FiduciaryAssignmentHolder.objects.count() == before["holders"] + 4
    group_t1 = StructuralGroup.objects.get(project=project, name="T1 Torre 1")
    group_t2 = StructuralGroup.objects.get(project=project, name="T2 Torre 2")
    assert PropertyUnit.objects.filter(project=project, structural_group=group_t1, name="101").exists()
    assert PropertyUnit.objects.filter(project=project, structural_group=group_t2, name="101").exists()
    assert "data-progress-url" not in response.content.decode()


def test_finalize_historical_import_accepts_authorized_processing_batch_with_warning_info(accounting_admin_user):
    batch = prepare_ready_historical_batch(accounting_admin_user)
    imported_file = batch.files.get()
    sheet = imported_file.sheet_results.first()
    ImportRowIssue.objects.create(
        imported_file=imported_file,
        sheet_result=sheet,
        severity=ImportRowIssue.Severity.WARNING,
        code="FORMULA_WITH_CACHED_VALUE",
        message="Columna mensual relevante contiene formulas con valor calculado disponible.",
    )
    ImportRowIssue.objects.create(
        imported_file=imported_file,
        sheet_result=sheet,
        severity=ImportRowIssue.Severity.INFO,
        code="UNKNOWN_HEADER",
        message="Encabezado no requerido por el analizador historico.",
    )
    batch.status = ImportBatch.Status.PROCESSING
    batch.save(update_fields=["status"])
    imported_file.status = ImportedFile.Status.PROCESSING
    imported_file.save(update_fields=["status"])

    result = finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.COMPLETED
    assert result.created_property_units == 4
    assert Client.objects.filter(source_origin=Client.SourceOrigin.HISTORICAL_IMPORT).exists()
    assert FiduciaryAssignment.objects.exists()
    assert Payment.objects.exists()


def test_finalize_historical_import_processing_batch_does_not_self_block_for_not_ready(accounting_admin_user):
    batch = prepare_ready_historical_batch(accounting_admin_user)
    imported_file = batch.files.get()
    batch.status = ImportBatch.Status.PROCESSING
    batch.save(update_fields=["status"])
    imported_file.status = ImportedFile.Status.PROCESSING
    imported_file.save(update_fields=["status"])

    finalize_historical_import(batch_id=batch.pk, user=accounting_admin_user)

    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.COMPLETED


def test_property_unit_history_view_shows_imported_historical_novelties(client, accounting_admin_user):
    project = Project.objects.create(code="P1", name="Proyecto 1")
    group = StructuralGroup.objects.create(
        project=project,
        grouping_type=GroupingType.objects.create(code="TOR", name="Torre"),
        code="T1",
        name="Torre 1",
    )
    unit = PropertyUnit.objects.create(project=project, structural_group=group, code="101", name="101")
    novelty = OperationalNovelty.objects.create(
        project=project,
        property_unit=unit,
        novelty_type=OperationalNovelty.NoveltyType.HISTORICAL,
        origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        status=OperationalNovelty.Status.DESCRIPTIVE,
        summary="Novedad historica de prueba",
        detail="Detalle historico",
        created_by=accounting_admin_user,
    )
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


def test_historical_novelty_summary_ignores_non_descriptive_row_values():
    detail_text = "NC3464 FEB.20/23 CESION DE WALTER GREEN A JANE MALFOY *ANTIC $25.000.000 *ARRAS $4'640"
    summary, detail = _summary_detail_from_cells(
        [
            {"header": "TIPO NOVEDAD", "value": "*CESION/ARRAS", "formula": False},
            {"header": "NOMBRE CLIENTE", "value": "GREEN WALTER", "formula": False},
            {"header": "RECIBOS", "value": "NCR3504-NCR3509-NCR3510", "formula": False},
            {"header": "FECHA", "value": "ENE.10/22-FEB.18/22", "formula": False},
            {"header": "E-MAIL", "value": "walter.green@example.com", "formula": False},
            {"header": "CEDULA CLIENTE", "value": "19254620", "formula": False},
            {"header": "ENCARGO FIDUCIARIO", "value": "60002828877770", "formula": False},
            {"header": "RECIBIDO FEB/2023", "value": "29640000", "formula": False},
            {"header": "__historical_section__", "value": "ENE/22", "formula": False},
            {"header": "OBSERVACIONES", "value": detail_text, "formula": False},
        ]
    )

    assert summary == "*CESION/ARRAS"
    assert detail == detail_text


def test_historical_novelty_summary_detects_short_value_outside_fixed_columns():
    detail_text = "NC14119 MAY.9/25 TRASLADO DE TORRE 1 A TORRE 2"
    summary, detail = _summary_detail_from_cells(
        [
            {"header": "APTO", "value": "501", "formula": False},
            {"header": "NOMBRE CLIENTE", "value": "CLIENTE REAL", "formula": False},
            {"header": "COLUMNA AUXILIAR", "value": "*TRASLADO", "formula": False},
            {"header": "RECIBOS", "value": "NC14119TRASLADO", "formula": False},
            {"header": "FECHA", "value": "MAY.9/25TRASLADO", "formula": False},
            {"header": "OBSERVACIONES", "value": detail_text, "formula": False},
        ]
    )

    assert summary == "*TRASLADO"
    assert detail == detail_text


def test_main_table_observations_are_created_once_per_assignment(accounting_admin_user):
    project = Project.objects.create(code="P1", name="Proyecto 1")
    unit = PropertyUnit.objects.create(project=project, code="101", name="101")
    assignment = FiduciaryAssignment.objects.create(
        property_unit=unit,
        assignment_number="EF-101",
        start_date="2026-01-01",
    )
    first_client = Client.objects.create(
        first_names="JUAN",
        last_names_or_company="PEREZ",
        document_number="1",
        phone="3001111111",
        source_origin=Client.SourceOrigin.HISTORICAL_IMPORT,
    )
    second_client = Client.objects.create(
        first_names="MARIA",
        last_names_or_company="LOPEZ",
        document_number="2",
        phone="3002222222",
        source_origin=Client.SourceOrigin.HISTORICAL_IMPORT,
    )
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.READY,
        total_files=1,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="historico.xlsx",
        extension=".xlsx",
        size_bytes=1,
        sha256="a" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    sheet_result = imported_file.sheet_results.create(sheet_name="T1", sheet_index=1)
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)
    row = SimpleNamespace(observation="PENDIENTE DOCUMENTACION", sheet_name="T1", row_number=5)

    context._main_row_observation(row, unit, assignment, sheet_result)

    observations = ImportedHistoricalObservation.objects.filter(
        origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
        property_unit=unit,
        assignment=assignment,
        detail="PENDIENTE DOCUMENTACION",
    )
    assert observations.count() == 1
    assert observations.get().client is None


def test_main_table_observation_cell_with_multiple_events_creates_multiple_observations(accounting_admin_user):
    project = Project.objects.create(code="P2", name="Proyecto 2")
    unit = PropertyUnit.objects.create(project=project, code="303", name="303")
    assignment = FiduciaryAssignment.objects.create(
        property_unit=unit,
        assignment_number="EF-303",
        start_date="2026-01-01",
    )
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.READY,
        total_files=1,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name="historico.xlsx",
        extension=".xlsx",
        size_bytes=1,
        sha256="b" * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    sheet_result = imported_file.sheet_results.create(sheet_name="T1", sheet_index=1)
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)
    first = "NC6383 MAR.4/22 CESION DE LEYTON MARIA A LUCAS TENTACLES *ANTIC $15.000.000 *ARRAS $4'640"
    second = "NC6383 MAR.4/23 CESION DE LUCAS TENTACLES A CLEVELAND WATSON *ANTIC $15.000.000 *ARRAS $4'640"
    row = SimpleNamespace(observation=f"{first} - {second}", sheet_name="T1", row_number=5)

    context._main_row_observation(row, unit, assignment, sheet_result)

    assert list(
        ImportedHistoricalObservation.objects.filter(
            origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
            property_unit=unit,
            assignment=assignment,
        )
        .order_by("source_order")
        .values_list("detail", flat=True)
    ) == [first, second]


@pytest.mark.parametrize(
    ("label", "separator", "expected"),
    [
        ("dash", " - ", ["OBSERVACION 1", "OBSERVACION 2"]),
        ("pipe", " | ", ["OBSERVACION 1", "OBSERVACION 2"]),
    ],
)
def test_main_table_observation_cell_with_generic_multiple_observations_creates_multiple_records(
    accounting_admin_user,
    label,
    separator,
    expected,
):
    project = Project.objects.create(code=f"P-OBS-{label}", name="Proyecto Observaciones")
    unit = PropertyUnit.objects.create(project=project, code="101", name="101")
    assignment = FiduciaryAssignment.objects.create(
        property_unit=unit,
        assignment_number=f"EF-OBS-{label}",
        start_date="2026-01-01",
    )
    batch = ImportBatch.objects.create(
        initiated_by=accounting_admin_user,
        import_type=ImportBatch.ImportType.HISTORICAL,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.READY,
        total_files=1,
    )
    imported_file = ImportedFile.objects.create(
        batch=batch,
        original_name=f"historico-{label}.xlsx",
        extension=".xlsx",
        size_bytes=1,
        sha256=label[:1] * 64,
        file_type=ImportedFile.FileType.HISTORICAL,
    )
    sheet_result = imported_file.sheet_results.create(sheet_name="T1", sheet_index=1)
    context = _FinalizationContext(batch=batch, imported_file=imported_file, user=accounting_admin_user)
    row = SimpleNamespace(observation=separator.join(expected), sheet_name="T1", row_number=5)

    context._main_row_observation(row, unit, assignment, sheet_result)

    assert list(
        ImportedHistoricalObservation.objects.filter(
            origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
            property_unit=unit,
            assignment=assignment,
        )
        .order_by("source_order")
        .values_list("detail", flat=True)
    ) == expected


def test_transfer_payment_concept_preserves_receipt_context_annotation():
    concept = _historical_payment_concept("transfer", "NC26449TRASL.AP101MTCT1", "CESIONES/TRASLADOS")

    assert concept == "TRASLADO | TRASL AP101 MTCT1 | Recibo NC26449"
