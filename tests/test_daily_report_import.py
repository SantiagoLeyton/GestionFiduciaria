from datetime import date
from decimal import Decimal
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from fiduciary.imports.cancellation import cancel_import_batch
from fiduciary.imports.daily import (
    DailyReportDuplicateError,
    DailyReportParser,
    analyze_daily_report_import,
    finalize_daily_report_import,
    reanalyze_daily_report_import,
    resolve_daily_report_assignment,
)
from fiduciary.imports.daily.services import DailyReportFinalizationError
import fiduciary.imports.daily.services as daily_services
from fiduciary.models import (
    Client,
    DailyReportRow,
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportRowIssue,
    Payment,
    UnitOwnership,
)
from real_estate.models import Project, PropertyUnit


pytestmark = pytest.mark.django_db

REPORT = Path("samples/fiduciary/reports/springfield/ReporteConsolidado_Springfield_VS_2026-07.xlsx")
REAL_REPORT_2026_07_01 = Path(
    r"C:\Users\ASUS\OneDrive\Escritorio\Practices\ConstructoraCentenarioSAS\Documents\Company\ProyectoFinal\ReporteConsolidado - 2026-07-02T142848.781.xls"
)
REAL_REPORT_2026_07_14 = Path(
    r"C:\Users\ASUS\OneDrive\Escritorio\Practices\ConstructoraCentenarioSAS\Documents\Company\ProyectoFinal\ReporteConsolidado - 2026-07-15T081136.793.xls"
)


def make_assignment(number="435251139471"):
    project = Project.objects.create(code=f"P-{number[-4:]}", name=f"Proyecto {number[-4:]}")
    unit = PropertyUnit.objects.create(project=project, code=f"U-{number[-4:]}", name=f"Unidad {number[-4:]}")
    client = Client.objects.create(
        document_type=Client.DocumentType.CITIZENSHIP_ID,
        document_number=f"10{number[-8:]}",
        last_names_or_company=f"Cliente {number[-4:]}",
        phone="3001234567",
    )
    UnitOwnership.objects.create(client=client, property_unit=unit, is_primary=True, start_date="2026-01-01")
    assignment = FiduciaryAssignment.objects.create(assignment_number=number, property_unit=unit, start_date="2026-01-01")
    FiduciaryAssignmentHolder.objects.create(assignment=assignment, client=client, is_primary=True, start_date="2026-01-01")
    return assignment


def make_batch(user):
    return ImportBatch.objects.create(
        initiated_by=user,
        import_type=ImportBatch.ImportType.REPORTS,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.ANALYZING,
        total_files=1,
    )


def write_xlsx(path, rows):
    shared = []
    shared_index = {}

    def shared_id(value):
        text = str(value)
        if text not in shared_index:
            shared_index[text] = len(shared)
            shared.append(text)
        return shared_index[text]

    def col_name(index):
        name = ""
        while index:
            index, rem = divmod(index - 1, 26)
            name = chr(65 + rem) + name
        return name

    sheet_rows = []
    for r, values in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(values, start=1):
            if value is None:
                continue
            ref = f"{col_name(c)}{r}"
            if isinstance(value, int | float):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="s"><v>{shared_id(value)}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    shared_xml = "".join(f"<si><t>{value}</t></si>" for value in shared)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>""")
        archive.writestr("_rels/.rels", """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>""")
        archive.writestr("xl/workbook.xml", """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Reporte" sheetId="1" r:id="rId1"/></sheets></workbook>""")
        archive.writestr("xl/_rels/workbook.xml.rels", """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>""")
        archive.writestr("xl/sharedStrings.xml", f"""<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared)}" uniqueCount="{len(shared)}">{shared_xml}</sst>""")
        archive.writestr("xl/worksheets/sheet1.xml", f"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:J{len(rows)}"/><sheetData>{''.join(sheet_rows)}</sheetData></worksheet>""")


def test_daily_report_parser_reads_real_report_and_headers():
    parsed = DailyReportParser(REPORT).parse()

    assert parsed.rows
    first = parsed.rows[0]
    assert first.normalized_assignment_number == "435251139471"
    assert first.payment_date == date(2026, 7, 25)
    assert first.amount > 0
    assert first.concept


def test_daily_report_parser_reads_real_montecielo_xls_reports():
    first_report = DailyReportParser(REAL_REPORT_2026_07_01).parse()
    second_report = DailyReportParser(REAL_REPORT_2026_07_14).parse()

    assert first_report.issues == []
    assert second_report.issues == []
    assert [(sheet.name, sheet.header_row, len(sheet.rows)) for sheet in first_report.sheets] == [("Page 1", 5, 10)]
    assert [(sheet.name, sheet.header_row, len(sheet.rows)) for sheet in second_report.sheets] == [("Page 1", 5, 3)]
    assert first_report.rows[0].normalized_assignment_number == "002010979825"
    assert first_report.rows[0].payment_date == date(2026, 7, 1)
    assert first_report.rows[0].amount == Decimal("1101000.0")
    assert first_report.rows[-1].normalized_assignment_number == "002010980225"
    assert first_report.rows[-1].row_number == 15
    assert second_report.rows[0].normalized_assignment_number == "002010980323"
    assert second_report.rows[0].payment_date == date(2026, 7, 14)
    assert sum((row.amount for row in first_report.rows), Decimal("0")) == Decimal("11042000.0")
    assert sum((row.amount for row in second_report.rows), Decimal("0")) == Decimal("2770000.0")


def test_parser_preserves_leading_zeroes_and_column_order(tmp_path):
    path = tmp_path / "custom.xlsx"
    write_xlsx(
        path,
        [
            ["Concepto", "Valor", "Fecha Mov", "N° Encargo"],
            ["Pago", 1000, "25/07/2026", "00123"],
        ],
    )

    row = DailyReportParser(path).parse().rows[0]

    assert row.normalized_assignment_number == "00123"
    assert row.amount == 1000


def test_parser_reports_missing_and_ambiguous_headers(tmp_path):
    missing = tmp_path / "missing.xlsx"
    ambiguous = tmp_path / "ambiguous.xlsx"
    write_xlsx(missing, [["Fecha Mov", "Valor"], ["25/07/2026", 100]])
    write_xlsx(ambiguous, [["N° Encargo", "Encargo", "Fecha Mov", "Valor"], ["1", "1", "25/07/2026", 100]])

    assert DailyReportParser(missing).parse().issues[0].code == "MISSING_REQUIRED_HEADERS"
    assert DailyReportParser(ambiguous).parse().issues[0].code == "AMBIGUOUS_REQUIRED_HEADERS"


def test_analyze_daily_report_classifies_existing_missing_invalid_and_duplicate(tmp_path, accounting_admin_user):
    assignment = make_assignment("00123")
    batch = make_batch(accounting_admin_user)
    path = tmp_path / "mixed.xlsx"
    write_xlsx(
        path,
        [
            ["N° Encargo", "Fecha Mov", "Adicion", "Concepto"],
            ["00123", "25/07/2026", 1000, "Pago"],
            ["99999", "25/07/2026", 1000, "Pago"],
            ["00123", "fecha mala", 1000, "Pago"],
            ["00123", "25/07/2026", 0, "Pago"],
        ],
    )
    Payment.objects.create(
        assignment=assignment,
        exact_date="2026-07-25",
        date_precision=Payment.DatePrecision.EXACT,
        amount=1000,
        movement_type=Payment.MovementType.ADDITION,
        source_file=ImportedFile.objects.create(batch=batch, original_name="prev.xlsx", extension=".xlsx", size_bytes=1, sha256="a" * 64, file_type=ImportedFile.FileType.UNKNOWN),
        source_sheet="Prev",
        source_row=1,
    )

    analyze_daily_report_import(batch=batch, file_path=path)

    statuses = list(batch.daily_report_rows.order_by("row_number").values_list("status", flat=True))
    assert statuses == [
        DailyReportRow.Status.DUPLICATE,
        DailyReportRow.Status.ASSIGNMENT_NOT_FOUND,
        DailyReportRow.Status.INVALID_DATE,
        DailyReportRow.Status.INVALID_AMOUNT,
    ]


def test_duplicate_sha_is_rejected(tmp_path, accounting_admin_user):
    make_assignment("00123")
    path = tmp_path / "report.xlsx"
    write_xlsx(path, [["N° Encargo", "Fecha Mov", "Adicion"], ["00123", "25/07/2026", 1000]])
    analyze_daily_report_import(batch=make_batch(accounting_admin_user), file_path=path)

    with pytest.raises(DailyReportDuplicateError):
        analyze_daily_report_import(batch=make_batch(accounting_admin_user), file_path=path)


def test_analysis_persists_structural_issues(tmp_path, accounting_admin_user):
    path = tmp_path / "bad.xlsx"
    write_xlsx(path, [["Fecha Mov", "Valor"], ["25/07/2026", 1000]])
    batch = make_batch(accounting_admin_user)

    analyze_daily_report_import(batch=batch, file_path=path)

    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.FAILED
    assert ImportRowIssue.objects.filter(imported_file__batch=batch, code="MISSING_REQUIRED_HEADERS").exists()


def test_real_montecielo_xls_full_flow_and_sha_idempotence(accounting_admin_user):
    parsed = DailyReportParser(REAL_REPORT_2026_07_14).parse()
    for row in parsed.rows:
        make_assignment(row.normalized_assignment_number)
    batch = make_batch(accounting_admin_user)

    analyze_daily_report_import(batch=batch, file_path=REAL_REPORT_2026_07_14)
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.READY
    assert list(batch.daily_report_rows.values_list("status", flat=True)) == [DailyReportRow.Status.VALID] * 3

    result = finalize_daily_report_import(batch_id=batch.pk, user=accounting_admin_user)

    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.COMPLETED
    assert result.imported_rows == 3
    assert Payment.objects.filter(source_file__batch=batch).count() == 3
    first_payment = Payment.objects.get(source_row=6)
    assert first_payment.assignment.assignment_number == "002010980323"
    assert first_payment.exact_date == date(2026, 7, 14)
    assert first_payment.amount == Decimal("770000.00")
    assert first_payment.concept == "APORTES INVERSIONISTAS"
    assert ImportAppliedRecord.objects.filter(batch=batch, entity_kind=ImportAppliedRecord.EntityKind.PAYMENT).count() == 3
    with pytest.raises(DailyReportDuplicateError):
        analyze_daily_report_import(batch=make_batch(accounting_admin_user), file_path=REAL_REPORT_2026_07_14)
    with pytest.raises(DailyReportFinalizationError):
        finalize_daily_report_import(batch_id=batch.pk, user=accounting_admin_user)
    assert Payment.objects.filter(source_file__batch=batch).count() == 3


def test_daily_report_finalization_rolls_back_partial_payments(tmp_path, accounting_admin_user, monkeypatch):
    first = make_assignment("00123")
    make_assignment("00456")
    path = tmp_path / "rollback.xlsx"
    write_xlsx(
        path,
        [
            ["NÂ° Encargo", "Fecha Mov", "Adicion"],
            ["00123", "25/07/2026", 1000],
            ["00456", "25/07/2026", 2000],
        ],
    )
    batch = make_batch(accounting_admin_user)
    analyze_daily_report_import(batch=batch, file_path=path)
    original_create_payment = daily_services.create_payment
    calls = {"count": 0}

    def fail_after_first_payment(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return original_create_payment(**kwargs)
        raise DailyReportFinalizationError("Fallo controlado de prueba.")

    monkeypatch.setattr(daily_services, "create_payment", fail_after_first_payment)

    with pytest.raises(DailyReportFinalizationError):
        finalize_daily_report_import(batch_id=batch.pk, user=accounting_admin_user)

    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.FAILED
    assert Payment.objects.filter(assignment=first).count() == 0
    assert ImportAppliedRecord.objects.filter(batch=batch).count() == 0
    assert not batch.daily_report_rows.filter(payment__isnull=False).exists()


def test_manual_resolution_reanalyze_and_finalization(accounting_admin_user):
    assignment = make_assignment("435251139471")
    batch = make_batch(accounting_admin_user)
    analyze_daily_report_import(batch=batch, file_path=REPORT)
    row = batch.daily_report_rows.filter(status=DailyReportRow.Status.ASSIGNMENT_NOT_FOUND).first()
    if row:
        resolve_daily_report_assignment(row=row, assignment=assignment, user=accounting_admin_user, note="Validado")
        reanalyze_daily_report_import(batch=batch, user=accounting_admin_user)
    batch.refresh_from_db()
    if batch.status != ImportBatch.Status.READY:
        for pending in batch.daily_report_rows.filter(status=DailyReportRow.Status.ASSIGNMENT_NOT_FOUND):
            resolve_daily_report_assignment(row=pending, assignment=assignment, user=accounting_admin_user, note="Prueba")
        batch.refresh_from_db()

    result = finalize_daily_report_import(batch_id=batch.pk, user=accounting_admin_user)

    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.COMPLETED
    assert result.imported_rows >= 1
    assert Payment.objects.filter(assignment=assignment, exact_date__isnull=False).exists()
    assert ImportAppliedRecord.objects.filter(batch=batch, entity_kind=ImportAppliedRecord.EntityKind.PAYMENT).exists()


def test_finalization_blocks_when_pending_and_completed_retry(accounting_admin_user):
    batch = make_batch(accounting_admin_user)
    analyze_daily_report_import(batch=batch, file_path=REPORT)

    with pytest.raises(DailyReportFinalizationError):
        finalize_daily_report_import(batch_id=batch.pk, user=accounting_admin_user)

    batch.status = ImportBatch.Status.COMPLETED
    batch.save(update_fields=["status"])
    with pytest.raises(DailyReportFinalizationError):
        finalize_daily_report_import(batch_id=batch.pk, user=accounting_admin_user)


def test_cancel_before_apply_and_block_after_apply(tmp_path, accounting_admin_user):
    make_assignment("00123")
    path = tmp_path / "cancel.xlsx"
    write_xlsx(path, [["N° Encargo", "Fecha Mov", "Adicion"], ["00123", "25/07/2026", 1000]])
    batch = make_batch(accounting_admin_user)
    analyze_daily_report_import(batch=batch, file_path=path)
    cancel_import_batch(batch=batch, cancelled_by=accounting_admin_user)
    assert not ImportBatch.objects.filter(pk=batch.pk).exists()


def test_permissions_and_no_business_entities_created(tmp_path, accounting_admin_user, commercial_user):
    assignment = make_assignment("00123")
    before_clients = Client.objects.count()
    before_units = PropertyUnit.objects.count()
    path = tmp_path / "ok.xlsx"
    write_xlsx(path, [["N° Encargo", "Fecha Mov", "Adicion"], ["00123", "25/07/2026", 1000]])
    batch = make_batch(commercial_user)
    analyze_daily_report_import(batch=batch, file_path=path)
    finalize_daily_report_import(batch_id=batch.pk, user=commercial_user)

    assert Client.objects.count() == before_clients
    assert PropertyUnit.objects.count() == before_units
    assert Payment.objects.filter(assignment=assignment).count() == 1
    with pytest.raises(PermissionDenied):
        finalize_daily_report_import(batch_id=batch.pk, user=AnonymousUser())


def test_views_for_daily_report_flow(client, accounting_admin_user, tmp_path):
    make_assignment("00123")
    path = tmp_path / "upload.xlsx"
    write_xlsx(path, [["N° Encargo", "Fecha Mov", "Adicion"], ["00123", "25/07/2026", 1000]])
    client.force_login(accounting_admin_user)
    with path.open("rb") as handle:
        response = client.post(reverse("fiduciary:daily_report_create"), {"file": handle}, follow=True)
    assert response.status_code == 200
    batch = ImportBatch.objects.get(import_type=ImportBatch.ImportType.REPORTS)
    assert "Previsualizacion de reporte diario" in response.content.decode()
    response = client.get(reverse("fiduciary:daily_report_finalize", args=[batch.pk]))
    assert response.status_code == 200


def test_view_uploads_real_montecielo_xls_report(client, accounting_admin_user):
    for row in DailyReportParser(REAL_REPORT_2026_07_14).parse().rows:
        make_assignment(row.normalized_assignment_number)
    client.force_login(accounting_admin_user)

    with REAL_REPORT_2026_07_14.open("rb") as handle:
        response = client.post(reverse("fiduciary:daily_report_create"), {"file": handle}, follow=True)

    assert response.status_code == 200
    content = response.content.decode()
    assert "Previsualizacion de reporte diario" in content
    assert "002010980323" in content
    assert DailyReportRow.objects.count() == 3
