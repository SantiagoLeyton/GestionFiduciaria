import json
import logging
import threading
import tempfile
import unicodedata
import uuid
from datetime import date
from pathlib import Path

from django.contrib import messages
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, transaction
from django.db.models import Case, Count, IntegerField, Min, Prefetch, Q, Sum, When
from django.db.models import Value
from django.db.models.functions import Replace
from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.text import slugify
from django.views.generic import CreateView, DetailView, FormView, ListView, TemplateView, UpdateView, View

from core.models import BackupRecord
from core.audit import audit_event, record_audit
from core.models import AuditEvent
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup
from real_estate.querysets import with_natural_unit_order

from .forms import (
    AssignmentFilterForm,
    AssignmentFinancialEntityForm,
    AssignmentHolderForm,
    AssignmentChangeForm,
    AddSecondaryAssignmentHolderForm,
    ClientFilterForm,
    ClientForm,
    ClientUpdateForm,
    DailyReportAssignmentResolutionForm,
    DailyReportUploadForm,
    DIRECT_UNITS_VALUE,
    ExportDocumentFilterForm,
    ExportHistoricalWorkbookForm,
    FiduciaryAssignmentForm,
    FiduciaryAssignmentUpdateForm,
    GlobalManualPaymentForm,
    HistoricalImportUploadForm,
    ImportResolutionForm,
    ManualPaymentForm,
    MAX_IMPORT_FILE_SIZE_BYTES,
    AuditFilterForm,
    NoveltyFilterForm,
    NewFiduciaryAssignmentForm,
    assignment_choice_label,
    assignment_can_receive_payment,
    normalize_document_query,
    OperationalNoveltyForm,
    OperationalNoveltyEditForm,
    ObservationFilterForm,
    ObservationForm,
    PaymentEditForm,
    PaymentFilterForm,
    OwnershipFinalizeForm,
    PrimaryOwnershipChangeForm,
    StructuralGroupResolutionForm,
    SecondaryAssignmentHolderFormSet,
    StatusReasonForm,
    UnitOwnershipForm,
    eligible_assignment_clients,
    property_unit_choice_label,
    unit_can_receive_new_assignment,
    validate_assignment_holder_formset,
)
from .domain_services import (
    change_assignment,
    change_primary_ownership,
    apply_operational_novelty,
    create_primary_ownership_with_assignment,
    finalize_ownership,
    save_form_object_safely,
    sync_active_assignment_primary_holder,
)
from .exporters import export_historical_workbook
from .services import create_payment
from .utils import calculate_sha256
from .imports.historical import (
    analyze_historical_import,
    finalize_historical_import,
    store_historical_import_file,
)
from .imports.historical.readiness import (
    has_available_stored_historical_file,
    can_finalize_historical_import_batch,
    has_blocked_dependencies,
    has_open_blocking_issues,
    has_unresolved_required_pendings,
)
from .imports.cancellation import CANCELABLE_BATCH_STATUSES, cancel_import_batch
from .imports.audit import AUDIT_SOURCE_COLUMN
from .imports.reversion import import_reversion_summary, revert_import_batch
from .imports.daily import analyze_daily_report_import, finalize_daily_report_import, reanalyze_daily_report_import, resolve_daily_report_assignment
from .imports.historical.resolutions import (
    ImmediateResolutionError,
    apply_resolution_to_current_element,
    apply_resolution_to_equivalent_elements,
    auto_resolve_new_units,
    create_immediate_structure_from_resolution,
    equivalent_pending_elements,
    reanalyze_pending_resolutions,
    resolve_structural_group,
    structural_group_pattern_elements,
    structural_group_pattern_label,
    structural_group_pattern_suggestions,
    apply_structural_group_pattern_suggestions,
    update_batch_resolution_state,
)
from .models import (
    Client,
    DailyReportRow,
    DetectedStructureElement,
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportedHistoricalObservation,
    ImportNovelty,
    ImportRowIssue,
    ImportResolution,
    OperationalNovelty,
    Payment,
    UnitOwnership,
)
from .permissions import (
    FiduciaryCreateRequiredMixin,
    FiduciaryImportRequiredMixin,
    FiduciaryManagementRequiredMixin,
    FiduciaryReadRequiredMixin,
    FiduciaryUpdateRequiredMixin,
    can_create_fiduciary,
    can_import_fiduciary,
    can_update_fiduciary,
)

logger = logging.getLogger(__name__)
MANUAL_PAYMENT_SOURCE_SHA = "9" * 64


def _payment_movement_type_label(payment: Payment) -> str:
    if payment.movement_type == Payment.MovementType.ADDITION:
        return "Abono"
    return payment.get_movement_type_display()


def _payment_movement_sort_key(payment: Payment):
    if payment.exact_date:
        return payment.exact_date
    if payment.period_year and payment.period_month:
        return date(payment.period_year, payment.period_month, 1)
    return date.min


def _assignment_movement_rows(payments: list[Payment]) -> list[dict]:
    movements = [
        {
            "kind": "payment",
            "date": _payment_movement_sort_key(payment),
            "payment": payment,
            "amount": payment.amount,
            "concept": payment.concept or "-",
            "type_label": _payment_movement_type_label(payment),
            "source": f"{payment.source_file.original_name} | {payment.source_sheet} fila {payment.source_row}"
            + (f" col. {payment.source_column}" if payment.source_column else ""),
        }
        for payment in payments
    ]
    return sorted(movements, key=lambda movement: (movement["date"], movement["kind"]))


class QueryStringMixin:
    def add_common_context(self, context):
        query_params = self.request.GET.copy()
        query_params.pop("page", None)
        context["page_querystring"] = query_params.urlencode()
        context["can_create"] = can_create_fiduciary(self.request.user)
        context["can_update"] = can_update_fiduciary(self.request.user)
        context["can_manage"] = context["can_update"]
        return context


def historical_batches():
    return ImportBatch.objects.filter(import_type=ImportBatch.ImportType.HISTORICAL).select_related("initiated_by").order_by("-created_at", "-pk")


def daily_report_batches():
    return ImportBatch.objects.filter(import_type=ImportBatch.ImportType.REPORTS).select_related("initiated_by").order_by("-created_at", "-pk")


def _validation_error_text(exc: ValidationError) -> str:
    if hasattr(exc, "messages"):
        return " ".join(str(message) for message in exc.messages)
    return str(exc)


def _add_validation_errors_to_form(form, exc: ValidationError) -> None:
    if hasattr(exc, "message_dict"):
        for field_name, messages_for_field in exc.message_dict.items():
            target = field_name if field_name in form.fields else None
            for message in messages_for_field:
                form.add_error(target, message)
        return
    for message in getattr(exc, "messages", [str(exc)]):
        form.add_error(None, message)


def _normalize_search_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(value.casefold().split())


def _activate_clients_for_assignment(clients, reason: str) -> None:
    for client in clients:
        if client.is_active:
            continue
        client.is_active = True
        client.last_change_reason = reason
        client.full_clean()
        client.save(update_fields=["is_active", "last_change_reason", "updated_at"])


def _create_assignment_without_novelty(*, unit, primary_client, assignment_number: str, secondary_clients, reason: str):
    secondary_clients = list(secondary_clients or [])
    if primary_client in secondary_clients:
        raise ValidationError({"secondary_client_ids": "El cliente principal no puede repetirse como secundario."})
    assignment_number = (assignment_number or "").strip()
    locked_unit = PropertyUnit.objects.select_for_update().get(pk=unit.pk)
    if FiduciaryAssignment.objects.filter(property_unit=locked_unit, is_active=True).exists():
        raise ValidationError({"property_unit": "La unidad seleccionada ya tiene un encargo fiduciario activo."})
    if UnitOwnership.objects.filter(property_unit=locked_unit, is_active=True, is_primary=True, end_date__isnull=True).exists():
        raise ValidationError({"property_unit": "La unidad seleccionada ya tiene un titular principal vigente."})

    primary_ownership = UnitOwnership(
        client=primary_client,
        property_unit=locked_unit,
        is_primary=True,
        start_date=timezone.localdate(),
        last_change_reason=reason,
    )
    primary_ownership.full_clean()
    primary_ownership.save()

    assignment = FiduciaryAssignment(
        assignment_number=assignment_number,
        property_unit=locked_unit,
        start_date=timezone.localdate(),
        observations=reason,
        last_change_reason=reason,
    )
    assignment.full_clean()
    assignment.save()
    FiduciaryAssignmentHolder.objects.create(
        assignment=assignment,
        client=primary_client,
        is_primary=True,
        start_date=assignment.start_date,
        last_change_reason=reason,
    )

    seen_secondary_ids = set()
    for client in secondary_clients:
        if client.pk in seen_secondary_ids:
            raise ValidationError({"secondary_client_ids": "No puede seleccionar el mismo cliente secundario mas de una vez."})
        seen_secondary_ids.add(client.pk)
        secondary_ownership, _ = UnitOwnership.objects.get_or_create(
            client=client,
            property_unit=locked_unit,
            is_active=True,
            defaults={
                "is_primary": False,
                "start_date": assignment.start_date,
                "last_change_reason": reason,
            },
        )
        if secondary_ownership.is_primary:
            raise ValidationError({"secondary_client_ids": "Un titular principal vigente no puede agregarse como secundario."})
        FiduciaryAssignmentHolder.objects.create(
            assignment=assignment,
            client=client,
            is_primary=False,
            start_date=assignment.start_date,
            last_change_reason=reason,
        )
    return assignment


def _audit_snapshot(obj, fields: tuple[str, ...]) -> str:
    parts = []
    for field in fields:
        value = getattr(obj, field, None)
        parts.append(f"{field}={value!r}")
    return "; ".join(parts)


def _log_object_action(user, obj, action: str, flag: int, message: str) -> None:
    record_audit(
        user=user,
        action=_human_action_from_code(action, flag),
        entity=_human_entity_from_object(obj),
        obj=obj,
        description=message,
        context={"Codigo interno": action},
    )


def _log_observation_change(user, observation: ImportedHistoricalObservation, reason: str, before: str = "") -> None:
    after = _audit_snapshot(observation, ("summary", "detail", "property_unit_id", "assignment_id"))
    record_audit(
        user=user,
        action="Modificado",
        entity="Observacion",
        obj=observation,
        description="MODIFICAR_OBSERVACION",
        reason=reason,
        before=before,
        after=after,
    )


def _human_action_from_code(action: str, flag: int) -> str:
    if flag == DELETION or action.startswith("ELIMINAR"):
        return "Eliminado"
    if flag == ADDITION or action.startswith("CREAR"):
        return "Creado"
    return "Modificado"


def _human_entity_from_object(obj) -> str:
    labels = {
        ImportedHistoricalObservation: "Observacion",
        OperationalNovelty: "Novedad",
        Payment: "Pago",
    }
    return labels.get(obj.__class__, obj._meta.verbose_name.title())


def _novelty_has_structural_effect(novelty: OperationalNovelty) -> bool:
    structural_types = {
        OperationalNovelty.NoveltyType.CESSION,
        OperationalNovelty.NoveltyType.WITHDRAWAL,
        OperationalNovelty.NoveltyType.EXCLUSION,
        OperationalNovelty.NoveltyType.SUBSTITUTION,
    }
    if novelty.novelty_type in structural_types:
        return True
    return novelty.novelty_type == OperationalNovelty.NoveltyType.OTHER and novelty.other_type.strip().casefold() == "inclusion"


def _unlink_payment_dependents(payment: Payment) -> None:
    ImportNovelty.objects.filter(payment=payment).update(payment=None)
    DailyReportRow.objects.filter(payment=payment).update(
        payment=None,
        status=DailyReportRow.Status.VALID,
        message="Pago eliminado manualmente por Contabilidad.",
    )
    ImportedHistoricalObservation.related_payments.through.objects.filter(payment_id=payment.pk).delete()


def _audit_manual_payment_created(user, payment: Payment) -> None:
    audit_event(
        user=user,
        action="Creado",
        entity="Pago",
        obj=payment,
        context={
            "Proyecto": payment.assignment.property_unit.project,
            "Unidad": payment.assignment.property_unit,
            "Encargo": payment.assignment.assignment_number,
        },
        after=_audit_snapshot(payment, ("exact_date", "period_year", "period_month", "amount", "concept", "destination")),
    )


class HistoricalImportBatchListView(FiduciaryReadRequiredMixin, QueryStringMixin, ListView):
    model = ImportBatch
    template_name = "fiduciary/import_batch_list.html"
    context_object_name = "batches"
    paginate_by = 10

    def get_queryset(self):
        return historical_batches().annotate(
            files_count=Count("files", distinct=True),
            pending_count=Count(
                "detected_elements",
                filter=Q(detected_elements__status=DetectedStructureElement.Status.NEEDS_REVIEW),
                distinct=True,
            ),
        )

    def get_context_data(self, **kwargs):
        context = self.add_common_context(super().get_context_data(**kwargs))
        context["cancelable_statuses"] = CANCELABLE_BATCH_STATUSES
        return context


class HistoricalImportCreateView(FiduciaryImportRequiredMixin, FormView):
    form_class = HistoricalImportUploadForm
    template_name = "fiduciary/import_batch_form.html"

    def form_valid(self, form):
        uploaded_files = self.request.FILES.getlist("file") or form.cleaned_data["file"]
        summary = _process_historical_uploads(
            request=self.request,
            uploaded_files=uploaded_files,
            grouping_type_hint=form.cleaned_data.get("grouping_type_hint"),
        )
        if summary.get("counts", {}).get("processing") == 1 and len(summary.get("items", [])) == 1:
            return redirect(summary["items"][0]["preview_url"])
        self.request.session["fiduciary_upload_summary"] = summary
        return redirect("fiduciary:import_upload_summary")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Importar libro historico"
        context["back_url"] = "fiduciary:historical_import_list"
        context["file_list_target"] = "historical-files"
        context["submit_label"] = "Crear lotes y analizar"
        return context


class ImportUploadSummaryView(FiduciaryReadRequiredMixin, TemplateView):
    template_name = "fiduciary/import_upload_summary.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        summary = self.request.session.get("fiduciary_upload_summary") or {}
        context["summary"] = summary
        return context


def _process_historical_uploads(*, request, uploaded_files, grouping_type_hint=None) -> dict:
    items = []
    seen_hashes = {}
    with tempfile.TemporaryDirectory() as temp_dir:
        for order, uploaded_file in enumerate(uploaded_files, start=1):
            item = _base_upload_item(uploaded_file, "Historico")
            validation_error = _validate_excel_upload(uploaded_file)
            if validation_error:
                item.update(result="invalid", message=validation_error)
                items.append(item)
                continue
            path = _copy_upload_to_temp(uploaded_file, temp_dir, order)
            sha256 = calculate_sha256(path)
            item["sha256"] = sha256
            if sha256 in seen_hashes:
                item.update(result="duplicate", message=f"Archivo repetido en la seleccion. Coincide con {seen_hashes[sha256]}.")
                items.append(item)
                continue
            seen_hashes[sha256] = uploaded_file.name
            batch = ImportBatch.objects.create(
                initiated_by=request.user,
                import_type=ImportBatch.ImportType.HISTORICAL,
                load_mode=ImportBatch.LoadMode.SINGLE_FILE,
                status=ImportBatch.Status.ANALYZING,
                total_files=1,
            )
            item["batch_id"] = batch.pk
            try:
                analysis_result = analyze_historical_import(
                    batch=batch,
                    file_path=path,
                    grouping_type_hint=grouping_type_hint,
                )
                store_historical_import_file(imported_file=analysis_result.imported_file, source_path=path)
                auto_resolve_new_units(batch, user=request.user)
                update_batch_resolution_state(batch)
                batch.refresh_from_db()
                if batch.status == ImportBatch.Status.READY:
                    _finalize_historical_import_synchronously(batch_id=batch.pk, user=request.user)
                    item.update(
                        result="auto_finalized",
                        message="Importacion historica definitiva completada.",
                        preview_url=reverse("fiduciary:historical_import_preview", args=[batch.pk]),
                    )
                    items.append(item)
                    continue
                item.update(
                    result="with_pendings" if batch.status == ImportBatch.Status.AWAITING_RESOLUTION else "processed",
                    message=batch.get_status_display(),
                    preview_url=reverse("fiduciary:historical_import_preview", args=[batch.pk]),
                )
            except Exception:
                logger.exception("Historical import analysis failed for batch %s and file %s.", batch.pk, path.name)
                batch.status = ImportBatch.Status.FAILED
                batch.summary = "No fue posible analizar el archivo historico cargado."
                batch.save(update_fields=["status", "summary"])
                item.update(
                    result="failed",
                    message="No fue posible analizar el archivo historico.",
                    preview_url=reverse("fiduciary:historical_import_preview", args=[batch.pk]),
                )
            items.append(item)
    return _build_upload_summary("Carga historica", "historical", items)


def _base_upload_item(uploaded_file, import_type: str) -> dict:
    return {
        "name": uploaded_file.name,
        "import_type": import_type,
        "result": "",
        "message": "",
        "batch_id": None,
        "preview_url": "",
    }


def _validate_excel_upload(uploaded_file) -> str:
    extension = Path(uploaded_file.name).suffix.lower()
    if extension not in {".xlsx", ".xls"}:
        return "Extension no permitida. Use .xlsx o .xls."
    if uploaded_file.size <= 0:
        return "El archivo esta vacio."
    if uploaded_file.size > MAX_IMPORT_FILE_SIZE_BYTES:
        return "El archivo supera el tamano maximo permitido de 25 MB."
    return ""


def _copy_upload_to_temp(uploaded_file, temp_dir, order: int) -> Path:
    target_dir = Path(temp_dir) / str(order)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / Path(uploaded_file.name).name
    with path.open("wb") as target:
        for chunk in uploaded_file.chunks():
            target.write(chunk)
    return path


def _start_historical_finalization_thread(*, batch_id: int, user_id: int) -> None:
    _mark_historical_finalization_started(batch_id)
    thread = threading.Thread(
        target=_run_historical_finalization_background,
        kwargs={
            "batch_id": batch_id,
            "user_id": user_id,
        },
        daemon=True,
        name=f"historical-finalization-{batch_id}",
    )
    thread.start()


def _finalize_historical_import_synchronously(*, batch_id: int, user) -> None:
    batch = ImportBatch.objects.get(pk=batch_id)
    _validate_historical_finalization_can_start(batch)
    finalize_historical_import(batch_id=batch_id, user=user)


def _mark_historical_finalization_started(batch_id: int) -> None:
    batch = ImportBatch.objects.get(pk=batch_id)
    _validate_historical_finalization_can_start(batch)
    ImportBatch.objects.filter(pk=batch_id, status=ImportBatch.Status.READY).update(
        status=ImportBatch.Status.PROCESSING,
        processing_started_at=timezone.now(),
        summary=json.dumps(
            {
                "progress": {
                    "phase": "preparing_final_import",
                    "percent": 1,
                    "processed_rows": 0,
                    "total_rows": batch.total_rows,
                }
            },
            ensure_ascii=True,
        ),
    )
    ImportedFile.objects.filter(batch_id=batch_id, file_type=ImportedFile.FileType.HISTORICAL).update(
        status=ImportedFile.Status.PROCESSING
    )


def _run_historical_finalization_background(*, batch_id: int, user_id: int) -> None:
    close_old_connections()
    try:
        user = get_user_model().objects.get(pk=user_id)
        finalize_historical_import(
            batch_id=batch_id,
            user=user,
            progress_callback=_batch_finalization_progress_callback(batch_id),
        )
    except Exception as exc:
        logger.exception("Historical import finalization worker failed for batch %s.", batch_id)
        try:
            _store_background_failure(batch_id, f"No fue posible completar el procesamiento del libro: {exc}")
        except Exception:
            logger.exception("Could not mark historical import batch %s as failed after worker error.", batch_id)
    finally:
        try:
            close_old_connections()
        except Exception:
            logger.exception("Could not close old database connections after historical finalization worker.")


def _batch_finalization_progress_callback(batch_id: int):
    def callback(progress: dict) -> None:
        ImportBatch.objects.filter(pk=batch_id).update(
            processed_rows=progress.get("processed_rows") or 0,
            total_rows=progress.get("total_rows") or 0,
            summary=json.dumps({"progress": progress}, ensure_ascii=True),
        )

    return callback


def _store_background_failure(batch_id: int, message: str) -> None:
    ImportBatch.objects.filter(pk=batch_id).update(
        status=ImportBatch.Status.FAILED,
        processing_finished_at=timezone.now(),
        summary=json.dumps({"progress": {"phase": "failed", "percent": 100, "error": message}}, ensure_ascii=True),
    )
    ImportedFile.objects.filter(batch_id=batch_id, file_type=ImportedFile.FileType.HISTORICAL).update(
        status=ImportedFile.Status.FAILED,
        result_message=message,
        processing_finished_at=timezone.now(),
    )


def _validate_historical_finalization_can_start(batch: ImportBatch) -> None:
    if batch.status != ImportBatch.Status.READY:
        raise ValidationError("Solo un lote listo puede importarse definitivamente.")
    if has_unresolved_required_pendings(batch):
        raise ValidationError("El lote aun tiene pendientes accionables.")
    if has_blocked_dependencies(batch):
        raise ValidationError("El lote aun tiene elementos bloqueados por dependencia.")
    if has_open_blocking_issues(batch):
        raise ValidationError("El lote contiene incidencias bloqueantes abiertas.")
    if not has_available_stored_historical_file(batch):
        raise ValidationError("El archivo original no esta conservado para importacion definitiva.")


def _build_upload_summary(title: str, import_type: str, items: list[dict]) -> dict:
    counts = {
        "total": len(items),
        "processed": sum(1 for item in items if item["result"] == "processed"),
        "auto_finalized": sum(1 for item in items if item["result"] == "auto_finalized"),
        "with_pendings": sum(1 for item in items if item["result"] == "with_pendings"),
        "processing": sum(1 for item in items if item["result"] == "processing"),
        "duplicates": sum(1 for item in items if item["result"] == "duplicate"),
        "invalid": sum(1 for item in items if item["result"] == "invalid"),
        "failed": sum(1 for item in items if item["result"] == "failed"),
    }
    return {"title": title, "import_type": import_type, "counts": counts, "items": items}

def _add_duplicate_historical_import_message(request, imported_file) -> None:
    uploaded_at = imported_file.created_at.strftime("%Y-%m-%d %H:%M")
    messages.warning(
        request,
        (
            "Este archivo ya fue cargado anteriormente y no se volvio a procesar. "
            f"Archivo original: {imported_file.original_name}. "
            f"Fecha de carga: {uploaded_at}. "
            f"Lote asociado: #{imported_file.batch_id}. "
            f"Estado del lote: {imported_file.batch.get_status_display()}."
        ),
    )


class HistoricalImportPreviewView(FiduciaryReadRequiredMixin, DetailView):
    model = ImportBatch
    template_name = "fiduciary/import_preview.html"
    context_object_name = "batch"

    def get_queryset(self):
        return historical_batches().prefetch_related(
            "files",
            "files__sheet_results",
            "files__row_issues",
            "historical_novelties",
            "detected_elements",
            "detected_elements__resolution",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        batch = self.object
        imported_file = batch.files.order_by("order", "original_name").first()
        detected = batch.detected_elements.select_related(
            "resolution",
            "resolution__target_project",
            "resolution__target_grouping_type",
            "resolution__target_structural_group",
            "resolution__target_property_unit",
        )
        context["can_create"] = can_create_fiduciary(self.request.user)
        context["can_update"] = can_update_fiduciary(self.request.user)
        context["can_import"] = can_import_fiduciary(self.request.user)
        context["can_manage"] = context["can_update"]
        context["imported_file"] = imported_file
        context["summary"] = _historical_content_summary(batch, imported_file)
        context["sheets"] = imported_file.sheet_results.all() if imported_file else []
        context["issue_groups"] = (
            imported_file.row_issues.values("code", "severity", "sheet_result__sheet_name").annotate(total=Count("id")).order_by("code")
            if imported_file
            else []
        )
        context["project_element"] = detected.filter(inferred_kind=DetectedStructureElement.InferredKind.PROJECT).first()
        context["grouping_type_element"] = detected.filter(inferred_kind=DetectedStructureElement.InferredKind.GROUPING_TYPE).first()
        context["groups"] = detected.filter(inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP).order_by(
            "normalized_value"
        )
        units = detected.filter(inferred_kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT).order_by("normalized_value")
        context["existing_units_count"] = units.filter(resolution__target_property_unit__isnull=False).count()
        context["new_units_count"] = units.filter(resolution__action=ImportResolution.Action.CREATE_NEW).count()
        context["unknown_units_count"] = units.filter(status=DetectedStructureElement.Status.NEEDS_REVIEW).count()
        context["unit_samples"] = units[:12]
        context["automatic_matches_count"] = detected.filter(status=DetectedStructureElement.Status.AUTO_MATCHED).count()
        context["historical_novelties_count"] = batch.historical_novelties.count()
        context["historical_novelty_samples"] = batch.historical_novelties.select_related(
            "sheet_result"
        ).order_by("sheet_result__sheet_index", "row_number")[:8]
        context["pending_count"] = detected.filter(status=DetectedStructureElement.Status.NEEDS_REVIEW).count()
        context["blocked_dependency_count"] = detected.filter(
            status=DetectedStructureElement.Status.DETECTED,
            resolution__action=ImportResolution.Action.UNRESOLVED,
        ).count()
        context["prepared_creation_count"] = detected.filter(resolution__action=ImportResolution.Action.CREATE_NEW).count()
        context["existing_resolution_count"] = detected.filter(resolution__action=ImportResolution.Action.ASSOCIATE_EXISTING).count()
        context["unknown_count"] = detected.filter(resolution__action=ImportResolution.Action.UNRESOLVED).count()
        context["is_ready"] = batch.status == ImportBatch.Status.READY
        context["can_cancel"] = can_import_fiduciary(self.request.user) and batch.status in CANCELABLE_BATCH_STATUSES
        context["can_finalize"] = can_import_fiduciary(self.request.user) and can_finalize_historical_import_batch(batch)
        return context


class HistoricalImportProgressView(FiduciaryReadRequiredMixin, View):
    def get(self, request, pk):
        batch = get_object_or_404(historical_batches(), pk=pk)
        summary = _load_import_summary(batch.summary)
        progress = summary.get("progress") if isinstance(summary, dict) else None
        return JsonResponse(
            {
                "batch_id": batch.pk,
                "status": batch.status,
                "progress": progress
                or {
                    "phase": batch.get_status_display(),
                    "percent": 100 if batch.status in {ImportBatch.Status.READY, ImportBatch.Status.COMPLETED} else 0,
                    "processed_rows": batch.processed_rows,
                    "total_rows": batch.total_rows,
                },
            }
        )


class HistoricalImportProgressPageView(FiduciaryReadRequiredMixin, QueryStringMixin, DetailView):
    model = ImportBatch
    template_name = "fiduciary/import_progress.html"
    context_object_name = "batch"

    def get_queryset(self):
        return historical_batches()

    def get_context_data(self, **kwargs):
        context = self.add_common_context(super().get_context_data(**kwargs))
        context["progress_url"] = reverse("fiduciary:historical_import_progress", args=[self.object.pk])
        context["preview_url"] = reverse("fiduciary:historical_import_preview", args=[self.object.pk])
        return context


class HistoricalImportIssueDetailView(FiduciaryReadRequiredMixin, QueryStringMixin, ListView):
    model = ImportRowIssue
    template_name = "fiduciary/import_issue_detail.html"
    context_object_name = "issues"
    paginate_by = 25

    def dispatch(self, request, *args, **kwargs):
        self.batch = get_object_or_404(historical_batches(), pk=kwargs["pk"])
        self.imported_file = self.batch.files.order_by("order", "original_name").first()
        if not self.imported_file:
            raise Http404("Archivo importado no encontrado.")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = ImportRowIssue.objects.filter(imported_file=self.imported_file).select_related("sheet_result")
        self.code = self.request.GET.get("code", "")
        self.severity = self.request.GET.get("severity", "")
        self.sheet_name = self.request.GET.get("sheet", "")
        if self.code:
            queryset = queryset.filter(code=self.code)
        if self.severity:
            queryset = queryset.filter(severity=self.severity)
        if self.sheet_name:
            queryset = queryset.filter(sheet_result__sheet_name=self.sheet_name)
        return queryset.order_by("sheet_result__sheet_index", "row_number", "column_letter", "pk")

    def get_context_data(self, **kwargs):
        context = self.add_common_context(super().get_context_data(**kwargs))
        context["batch"] = self.batch
        context["imported_file"] = self.imported_file
        context["code"] = self.code
        context["severity"] = self.severity
        context["sheet_name"] = self.sheet_name
        return context


class HistoricalImportFinalizeView(FiduciaryImportRequiredMixin, DetailView):
    model = ImportBatch
    template_name = "fiduciary/import_finalize_confirm.html"
    context_object_name = "batch"

    def get_queryset(self):
        return historical_batches().prefetch_related("files", "detected_elements", "historical_novelties")

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        try:
            _finalize_historical_import_synchronously(batch_id=self.object.pk, user=request.user)
        except PermissionDenied:
            raise
        except Exception as exc:
            messages.error(request, str(exc))
            return redirect("fiduciary:historical_import_preview", pk=self.object.pk)
        messages.success(request, "Importacion historica definitiva completada.")
        return redirect("fiduciary:historical_import_preview", pk=self.object.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        batch = self.object
        context["imported_file"] = batch.files.order_by("order", "original_name").first()
        context["pending_count"] = batch.detected_elements.filter(
            status=DetectedStructureElement.Status.NEEDS_REVIEW
        ).count()
        context["blocked_count"] = batch.detected_elements.filter(
            status=DetectedStructureElement.Status.DETECTED,
            resolution__action=ImportResolution.Action.UNRESOLVED,
        ).count()
        context["historical_novelties_count"] = batch.historical_novelties.count()
        context["can_finalize"] = can_finalize_historical_import_batch(batch)
        return context


class HistoricalImportCancelView(FiduciaryImportRequiredMixin, DetailView):
    model = ImportBatch
    template_name = "fiduciary/import_cancel_confirm.html"
    context_object_name = "batch"

    def get_queryset(self):
        return historical_batches().prefetch_related("files", "detected_elements")

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        try:
            cancel_import_batch(batch=self.object, cancelled_by=request.user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("fiduciary:historical_import_preview", pk=self.object.pk)
        messages.success(
            request,
            "El intento de importaciÃ³n fue cancelado y sus resultados temporales fueron eliminados.",
        )
        return redirect("fiduciary:historical_import_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        batch = self.object
        context["imported_file"] = batch.files.order_by("order", "original_name").first()
        context["pending_count"] = batch.detected_elements.filter(
            status=DetectedStructureElement.Status.NEEDS_REVIEW
        ).count()
        context["can_cancel"] = batch.status in CANCELABLE_BATCH_STATUSES
        return context


class ImportRevertView(FiduciaryImportRequiredMixin, DetailView):
    template_name = "fiduciary/import_revert_confirm.html"
    context_object_name = "batch"
    list_url_name = ""
    preview_url_name = ""

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        reason = request.POST.get("change_reason", "").strip()
        if not reason:
            messages.error(request, "Debe registrar el motivo para deshacer la importacion.")
            return redirect(self.preview_url_name, pk=self.object.pk)
        try:
            summary = revert_import_batch(batch=self.object, user=request.user, reason=reason)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect(self.preview_url_name, pk=self.object.pk)
        messages.success(request, f"Importacion deshecha. {summary.text()}.")
        return redirect(self.list_url_name)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        summary = import_reversion_summary(self.object)
        context["imported_file"] = self.object.files.order_by("order", "original_name").first()
        context["summary"] = summary
        context["summary_items"] = summary.as_dict().items()
        context["can_revert"] = self.object.status in {
            ImportBatch.Status.COMPLETED,
            ImportBatch.Status.COMPLETED_WITH_ISSUES,
        }
        context["preview_url_name"] = self.preview_url_name
        return context


class HistoricalImportRevertView(ImportRevertView):
    list_url_name = "fiduciary:historical_import_list"
    preview_url_name = "fiduciary:historical_import_preview"

    def get_queryset(self):
        return historical_batches().prefetch_related("files", "applied_records")


class HistoricalImportPendingListView(FiduciaryImportRequiredMixin, QueryStringMixin, ListView):
    model = DetectedStructureElement
    template_name = "fiduciary/import_pending_list.html"
    context_object_name = "elements"
    paginate_by = 20

    def dispatch(self, request, *args, **kwargs):
        self.batch = get_object_or_404(historical_batches(), pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return self.batch.detected_elements.filter(
            status=DetectedStructureElement.Status.NEEDS_REVIEW
        ).select_related("resolution").annotate(
            pending_order=Case(
                When(inferred_kind=DetectedStructureElement.InferredKind.PROJECT, then=0),
                When(inferred_kind=DetectedStructureElement.InferredKind.GROUPING_TYPE, then=1),
                When(inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP, then=2),
                When(inferred_kind=DetectedStructureElement.InferredKind.PROPERTY_UNIT, then=3),
                default=9,
                output_field=IntegerField(),
            )
        ).order_by("pending_order", "normalized_value")

    def get_context_data(self, **kwargs):
        context = self.add_common_context(super().get_context_data(**kwargs))
        context["batch"] = self.batch
        context["blocked_elements"] = self.batch.detected_elements.filter(
            status=DetectedStructureElement.Status.DETECTED,
            resolution__action=ImportResolution.Action.UNRESOLVED,
        ).order_by("inferred_kind", "raw_value")[:15]
        context["blocked_count"] = self.batch.detected_elements.filter(
            status=DetectedStructureElement.Status.DETECTED,
            resolution__action=ImportResolution.Action.UNRESOLVED,
        ).count()
        context["prepared_creation_count"] = self.batch.detected_elements.filter(
            resolution__action=ImportResolution.Action.CREATE_NEW,
        ).count()
        for element in context["elements"]:
            element.block_reason = _pending_dependency_block_reason(self.batch, element)
        return context


class HistoricalImportReanalyzePendingView(FiduciaryImportRequiredMixin, View):
    def post(self, request, pk):
        batch = get_object_or_404(historical_batches(), pk=pk)
        updated = reanalyze_pending_resolutions(batch, user=request.user)
        messages.success(request, f"Se volvieron a analizar los pendientes. Elementos actualizados: {updated}.")
        return redirect("fiduciary:historical_import_pending", pk=batch.pk)


def _pending_dependency_block_reason(batch: ImportBatch, element: DetectedStructureElement) -> str:
    unresolved_filter = Q(status=DetectedStructureElement.Status.NEEDS_REVIEW) | Q(
        status=DetectedStructureElement.Status.DETECTED,
        resolution__action=ImportResolution.Action.UNRESOLVED,
    )
    if element.inferred_kind in {
        DetectedStructureElement.InferredKind.GROUPING_TYPE,
        DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        DetectedStructureElement.InferredKind.PROPERTY_UNIT,
    } and batch.detected_elements.filter(unresolved_filter, inferred_kind=DetectedStructureElement.InferredKind.PROJECT).exclude(
        pk=element.pk
    ).exists():
        return "Resuelva primero el proyecto."
    if element.inferred_kind in {
        DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        DetectedStructureElement.InferredKind.PROPERTY_UNIT,
    } and batch.detected_elements.filter(unresolved_filter, inferred_kind=DetectedStructureElement.InferredKind.GROUPING_TYPE).exclude(
        pk=element.pk
    ).exists():
        return "Resuelva primero el tipo de agrupacion."
    if element.inferred_kind == DetectedStructureElement.InferredKind.PROPERTY_UNIT and batch.detected_elements.filter(
        unresolved_filter,
        inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
    ).exclude(pk=element.pk).exists():
        return "Resuelva primero la agrupacion."
    return ""


class HistoricalImportResolutionView(FiduciaryImportRequiredMixin, FormView):
    form_class = ImportResolutionForm
    template_name = "fiduciary/import_resolution_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.batch = get_object_or_404(historical_batches(), pk=kwargs["pk"])
        self.element = get_object_or_404(
            self.batch.detected_elements.select_related("resolution"),
            pk=kwargs["element_pk"],
        )
        block_reason = _pending_dependency_block_reason(self.batch, self.element)
        if block_reason:
            messages.error(request, block_reason)
            return redirect("fiduciary:historical_import_pending", pk=self.batch.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.element.resolution
        kwargs["detected_element"] = self.element
        return kwargs

    def form_valid(self, form):
        resolution = form.save(commit=False)
        _clear_resolution_targets(resolution)
        resolution.resolved_by = self.request.user
        resolution.status = ImportResolution.Status.APPLIED
        apply_equivalents = self.request.POST.get("apply_equivalents") == "1"
        try:
            immediate_message = create_immediate_structure_from_resolution(
                resolution,
                self.request.user,
                apply_equivalents=apply_equivalents,
            )
        except ImmediateResolutionError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        if immediate_message:
            messages.success(self.request, immediate_message)
        else:
            apply_resolution_to_current_element(resolution, self.request.user)
            equivalent_count = 0
            if apply_equivalents:
                equivalent_count = equivalent_pending_elements(self.element).count()
                apply_resolution_to_equivalent_elements(resolution, self.request.user)
        reanalyze_pending_resolutions(self.batch, user=self.request.user)
        self.batch.refresh_from_db()
        if can_finalize_historical_import_batch(self.batch):
            messages.success(self.request, "Todas las resoluciones estan completas. El lote esta listo para importacion definitiva.")
            return redirect("fiduciary:historical_import_preview", pk=self.batch.pk)
        if not immediate_message:
            if self.request.POST.get("apply_equivalents") == "1":
                messages.success(self.request, f"Resolucion aplicada al pendiente actual y a {equivalent_count} pendiente(s) similar(es).")
            else:
                messages.success(self.request, "Resolucion aplicada al pendiente actual.")
        return redirect("fiduciary:historical_import_pending", pk=self.batch.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["batch"] = self.batch
        context["element"] = self.element
        context["creates_immediately"] = self.element.inferred_kind in {
            DetectedStructureElement.InferredKind.PROJECT,
            DetectedStructureElement.InferredKind.GROUPING_TYPE,
        }
        context["equivalent_elements"] = list(equivalent_pending_elements(self.element)[:25])
        context["equivalent_count"] = equivalent_pending_elements(self.element).count()
        return context


class HistoricalImportStructuralGroupResolutionView(FiduciaryImportRequiredMixin, FormView):
    form_class = StructuralGroupResolutionForm
    template_name = "fiduciary/import_structural_group_resolution_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.batch = get_object_or_404(historical_batches(), pk=kwargs["pk"])
        self.element = get_object_or_404(
            self.batch.detected_elements.select_related("resolution"),
            pk=kwargs["element_pk"],
            inferred_kind=DetectedStructureElement.InferredKind.STRUCTURAL_GROUP,
        )
        block_reason = _pending_dependency_block_reason(self.batch, self.element)
        if block_reason:
            messages.error(request, block_reason)
            return redirect("fiduciary:historical_import_pending", pk=self.batch.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["detected_element"] = self.element
        return kwargs

    def form_valid(self, form):
        created_grouping_type = None
        try:
            with transaction.atomic():
                grouping_type = form.cleaned_data["grouping_type"]
                if form.cleaned_data.get("create_grouping_type"):
                    grouping_type = GroupingType(
                        code=form.cleaned_data["new_grouping_type_code"].strip(),
                        name=form.cleaned_data["new_grouping_type_name"].strip(),
                        description="",
                        is_active=True,
                    )
                    grouping_type.save()
                    created_grouping_type = grouping_type
                suggestions = []
                if form.cleaned_data["action"] == ImportResolution.Action.ASSOCIATE_EXISTING:
                    suggestions = structural_group_pattern_suggestions(
                        source_element=self.element,
                        project=form.cleaned_data["project"],
                        grouping_type=grouping_type,
                        selected_group=form.cleaned_data.get("existing_group"),
                    )
                elif form.cleaned_data["action"] == ImportResolution.Action.CREATE_NEW:
                    suggestions = structural_group_pattern_suggestions(
                        source_element=self.element,
                        project=form.cleaned_data["project"],
                        grouping_type=grouping_type,
                        selected_group=None,
                        new_group_name=form.cleaned_data.get("new_group_name"),
                    )
                updated_units = resolve_structural_group(
                    resolution=self.element.resolution,
                    action=form.cleaned_data["action"],
                    project=form.cleaned_data["project"],
                    grouping_type=grouping_type,
                    existing_group=form.cleaned_data.get("existing_group"),
                    new_group_name=form.cleaned_data.get("new_group_name"),
                    resolved_by=self.request.user,
                )
                if suggestions:
                    applied = apply_structural_group_pattern_suggestions(suggestions, self.request.user)
                    updated_units += applied
        except (ValueError, ValidationError, IntegrityError) as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        if created_grouping_type:
            messages.success(self.request, f"Tipo de agrupacion creado: {created_grouping_type}.")
        messages.success(
            self.request,
            f"La agrupacion fue resuelta y se actualizaron automaticamente {updated_units} unidades relacionadas.",
        )
        self.batch.refresh_from_db()
        if can_finalize_historical_import_batch(self.batch):
            messages.success(self.request, "Todas las resoluciones estan completas. El lote esta listo para importacion definitiva.")
            return redirect("fiduciary:historical_import_preview", pk=self.batch.pk)
        return redirect("fiduciary:historical_import_pending", pk=self.batch.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["batch"] = self.batch
        context["element"] = self.element
        project_id = (self.element.structural_context or {}).get("project_id")
        context["suggested_project"] = Project.objects.filter(pk=project_id).first() if project_id else None
        context["pattern_label"] = structural_group_pattern_label(self.element)
        context["pattern_elements"] = structural_group_pattern_elements(self.element)
        return context


class HistoricalImportStructuralGroupChoicesView(FiduciaryImportRequiredMixin, View):
    def get(self, request):
        project_id = request.GET.get("project")
        grouping_type_id = request.GET.get("grouping_type")
        groups = StructuralGroup.objects.none()
        if project_id and grouping_type_id:
            groups = StructuralGroup.objects.filter(
                is_active=True,
                project_id=project_id,
                grouping_type_id=grouping_type_id,
            ).order_by("name", "code")
        return JsonResponse({"results": [{"id": group.pk, "text": str(group)} for group in groups]})


def _clear_resolution_targets(resolution: ImportResolution) -> None:
    if resolution.action != ImportResolution.Action.ASSOCIATE_EXISTING:
        resolution.target_project = None
        resolution.target_grouping_type = None
        resolution.target_structural_group = None
        resolution.target_property_unit = None
    elif resolution.target_kind == DetectedStructureElement.InferredKind.PROJECT:
        resolution.target_grouping_type = None
        resolution.target_structural_group = None
        resolution.target_property_unit = None
    elif resolution.target_kind == DetectedStructureElement.InferredKind.GROUPING_TYPE:
        resolution.target_project = None
        resolution.target_structural_group = None
        resolution.target_property_unit = None
    elif resolution.target_kind == DetectedStructureElement.InferredKind.STRUCTURAL_GROUP:
        resolution.target_project = None
        resolution.target_grouping_type = None
        resolution.target_property_unit = None
    elif resolution.target_kind == DetectedStructureElement.InferredKind.PROPERTY_UNIT:
        resolution.target_project = None
        resolution.target_grouping_type = None
        resolution.target_structural_group = None
    if resolution.action != ImportResolution.Action.CREATE_NEW:
        resolution.parent_project = None
        resolution.parent_grouping_type = None
        resolution.parent_structural_group = None
        resolution.create_code = ""
        resolution.create_name = ""


def _load_import_summary(value: str) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _historical_content_summary(batch: ImportBatch, imported_file: ImportedFile | None) -> dict:
    file_summary = _load_import_summary(imported_file.result_message if imported_file else "")
    batch_summary = _load_import_summary(batch.summary)
    content_keys = {
        "valid_rows",
        "ignored_rows",
        "client_appearances",
        "distinct_assignments",
        "payment_entries",
        "payment_columns",
        "historical_novelties",
        "issues",
    }
    summary = dict(file_summary)
    for key, value in batch_summary.items():
        if key in content_keys or key not in summary:
            summary[key] = value
    return summary


class DailyReportBatchListView(FiduciaryReadRequiredMixin, QueryStringMixin, ListView):
    model = ImportBatch
    template_name = "fiduciary/daily_report_batch_list.html"
    context_object_name = "batches"
    paginate_by = 10

    def get_queryset(self):
        return daily_report_batches().annotate(
            files_count=Count("files", distinct=True),
            pending_count=Count(
                "daily_report_rows",
                filter=Q(
                    daily_report_rows__status__in=[
                        DailyReportRow.Status.ASSIGNMENT_NOT_FOUND,
                        DailyReportRow.Status.INVALID_ASSIGNMENT,
                        DailyReportRow.Status.INVALID_DATE,
                        DailyReportRow.Status.INVALID_AMOUNT,
                        DailyReportRow.Status.NEEDS_REVIEW,
                        DailyReportRow.Status.FAILED,
                    ]
                ),
                distinct=True,
            ),
        )

    def get_context_data(self, **kwargs):
        context = self.add_common_context(super().get_context_data(**kwargs))
        context["cancelable_statuses"] = CANCELABLE_BATCH_STATUSES
        return context


class DailyReportCreateView(FiduciaryImportRequiredMixin, FormView):
    form_class = DailyReportUploadForm
    template_name = "fiduciary/daily_report_form.html"

    def form_valid(self, form):
        uploaded_files = self.request.FILES.getlist("file") or form.cleaned_data["file"]
        summary = _process_daily_report_uploads(request=self.request, uploaded_files=uploaded_files)
        self.request.session["fiduciary_upload_summary"] = summary
        return redirect("fiduciary:import_upload_summary")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Importar reporte diario"
        context["back_url"] = "fiduciary:daily_report_list"
        context["file_list_target"] = "daily-files"
        context["submit_label"] = "Crear lotes y analizar"
        return context


def _process_daily_report_uploads(*, request, uploaded_files) -> dict:
    items = []
    seen_hashes = {}
    with tempfile.TemporaryDirectory() as temp_dir:
        for order, uploaded_file in enumerate(uploaded_files, start=1):
            item = _base_upload_item(uploaded_file, "Reporte diario")
            validation_error = _validate_excel_upload(uploaded_file)
            if validation_error:
                item.update(result="invalid", message=validation_error)
                items.append(item)
                continue
            path = _copy_upload_to_temp(uploaded_file, temp_dir, order)
            sha256 = calculate_sha256(path)
            item["sha256"] = sha256
            if sha256 in seen_hashes:
                item.update(result="duplicate", message=f"Archivo repetido en la seleccion. Coincide con {seen_hashes[sha256]}.")
                items.append(item)
                continue
            seen_hashes[sha256] = uploaded_file.name
            batch = ImportBatch.objects.create(
                initiated_by=request.user,
                import_type=ImportBatch.ImportType.REPORTS,
                load_mode=ImportBatch.LoadMode.SINGLE_FILE,
                status=ImportBatch.Status.ANALYZING,
                total_files=1,
            )
            item["batch_id"] = batch.pk
            try:
                analyze_daily_report_import(batch=batch, file_path=path)
                batch.refresh_from_db()
                if batch.status == ImportBatch.Status.READY:
                    finalization = finalize_daily_report_import(batch_id=batch.pk, user=request.user)
                    batch.refresh_from_db()
                    item.update(
                        result="auto_finalized",
                        message=(
                            "Finalizado automaticamente. "
                            f"Pagos creados: {finalization.imported_rows}. Duplicados omitidos: {finalization.duplicate_rows}."
                        ),
                        preview_url=reverse("fiduciary:daily_report_preview", args=[batch.pk]),
                    )
                    items.append(item)
                    continue
                item.update(
                    result="with_pendings" if batch.status == ImportBatch.Status.AWAITING_RESOLUTION else "processed",
                    message=batch.get_status_display(),
                    preview_url=reverse("fiduciary:daily_report_preview", args=[batch.pk]),
                )
            except Exception:
                batch.status = ImportBatch.Status.FAILED
                batch.summary = "No fue posible analizar el reporte diario cargado."
                batch.save(update_fields=["status", "summary"])
                item.update(
                    result="failed",
                    message="No fue posible analizar el reporte diario.",
                    preview_url=reverse("fiduciary:daily_report_preview", args=[batch.pk]),
                )
            items.append(item)
    return _build_upload_summary("Carga de reportes diarios", "daily", items)

class DailyReportPreviewView(FiduciaryReadRequiredMixin, DetailView):
    model = ImportBatch
    template_name = "fiduciary/daily_report_preview.html"
    context_object_name = "batch"

    def get_queryset(self):
        return daily_report_batches().prefetch_related("files", "daily_report_rows")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        batch = self.object
        imported_file = batch.files.order_by("order", "original_name").first()
        rows = batch.daily_report_rows.select_related("assignment", "payment").order_by("sheet_name", "row_number")
        blocking_statuses = [
            DailyReportRow.Status.ASSIGNMENT_NOT_FOUND,
            DailyReportRow.Status.INVALID_ASSIGNMENT,
            DailyReportRow.Status.INVALID_DATE,
            DailyReportRow.Status.INVALID_AMOUNT,
            DailyReportRow.Status.NEEDS_REVIEW,
            DailyReportRow.Status.FAILED,
        ]
        context["can_create"] = can_create_fiduciary(self.request.user)
        context["can_import"] = can_import_fiduciary(self.request.user)
        context["imported_file"] = imported_file
        context["rows"] = rows[:50]
        context["summary"] = _load_import_summary(batch.summary)
        context["valid_count"] = rows.filter(status=DailyReportRow.Status.VALID).count()
        context["duplicate_count"] = rows.filter(status=DailyReportRow.Status.DUPLICATE).count()
        context["assignment_not_found_count"] = rows.filter(status=DailyReportRow.Status.ASSIGNMENT_NOT_FOUND).count()
        context["invalid_date_count"] = rows.filter(status=DailyReportRow.Status.INVALID_DATE).count()
        context["invalid_amount_count"] = rows.filter(status=DailyReportRow.Status.INVALID_AMOUNT).count()
        context["blocking_count"] = rows.filter(status__in=blocking_statuses).count()
        context["issue_groups"] = (
            ImportRowIssue.objects.filter(imported_file=imported_file)
            .values("code", "severity", "sheet_result__sheet_name")
            .annotate(total=Count("id"))
            .order_by("severity", "code", "sheet_result__sheet_name")
            if imported_file
            else []
        )
        context["can_finalize"] = can_import_fiduciary(self.request.user) and batch.status == ImportBatch.Status.READY
        context["can_cancel"] = can_import_fiduciary(self.request.user) and batch.status in CANCELABLE_BATCH_STATUSES
        return context


class DailyReportReanalyzeView(FiduciaryImportRequiredMixin, View):
    def post(self, request, pk):
        batch = get_object_or_404(daily_report_batches(), pk=pk)
        updated = reanalyze_daily_report_import(batch=batch, user=request.user)
        batch.refresh_from_db()
        if batch.status == ImportBatch.Status.READY:
            try:
                result = finalize_daily_report_import(batch_id=batch.pk, user=request.user)
                messages.success(request, f"Reporte reanalizado y finalizado automaticamente. Pagos creados: {result.imported_rows}.")
                return redirect("fiduciary:daily_report_preview", pk=batch.pk)
            except Exception as exc:
                messages.error(request, str(exc))
        messages.success(request, f"Reporte reanalizado. Filas actualizadas: {updated}.")
        return redirect("fiduciary:daily_report_preview", pk=batch.pk)


class DailyReportResolveAssignmentView(FiduciaryImportRequiredMixin, FormView):
    form_class = DailyReportAssignmentResolutionForm
    template_name = "fiduciary/daily_report_resolve.html"

    def dispatch(self, request, *args, **kwargs):
        self.batch = get_object_or_404(daily_report_batches(), pk=kwargs["pk"])
        self.row = get_object_or_404(self.batch.daily_report_rows, pk=kwargs["row_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.row
        return kwargs

    def form_valid(self, form):
        assignment = form.cleaned_data.get("assignment")
        financial_entity = form.cleaned_data.get("financial_entity", "")
        payment_destination = form.cleaned_data.get("payment_destination") or None
        if assignment and financial_entity:
            unit = assignment.property_unit
            if unit.financial_entity != financial_entity:
                unit.financial_entity = financial_entity
                unit.save(update_fields=["financial_entity", "updated_at"])
        resolve_daily_report_assignment(
            row=self.row,
            assignment=assignment,
            user=self.request.user,
            note=form.cleaned_data.get("resolution_note", ""),
            payment_destination=payment_destination,
        )
        self.batch.refresh_from_db()
        if self.batch.status == ImportBatch.Status.READY:
            try:
                result = finalize_daily_report_import(batch_id=self.batch.pk, user=self.request.user)
                messages.success(self.request, f"Resolucion aplicada. Reporte finalizado automaticamente. Pagos creados: {result.imported_rows}.")
                return redirect("fiduciary:daily_report_preview", pk=self.batch.pk)
            except Exception as exc:
                messages.error(self.request, str(exc))
        messages.success(self.request, "Resolucion del encargo aplicada.")
        return redirect("fiduciary:daily_report_preview", pk=self.batch.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["batch"] = self.batch
        context["row"] = self.row
        assignments = FiduciaryAssignment.objects.select_related("property_unit").order_by("assignment_number")
        context["assignment_financial_entities"] = {
            str(assignment.pk): assignment.property_unit.financial_entity or "" for assignment in assignments
        }
        return context


class DailyReportCancelView(FiduciaryImportRequiredMixin, DetailView):
    model = ImportBatch
    template_name = "fiduciary/daily_report_cancel_confirm.html"
    context_object_name = "batch"

    def get_queryset(self):
        return daily_report_batches().prefetch_related("files", "daily_report_rows")

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        try:
            cancel_import_batch(batch=self.object, cancelled_by=request.user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("fiduciary:daily_report_preview", pk=self.object.pk)
        messages.success(request, "El intento de importacion del reporte fue cancelado.")
        return redirect("fiduciary:daily_report_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["imported_file"] = self.object.files.order_by("order", "original_name").first()
        context["row_count"] = self.object.daily_report_rows.count()
        context["can_cancel"] = self.object.status in CANCELABLE_BATCH_STATUSES
        return context


class DailyReportRevertView(ImportRevertView):
    list_url_name = "fiduciary:daily_report_list"
    preview_url_name = "fiduciary:daily_report_preview"

    def get_queryset(self):
        return daily_report_batches().prefetch_related("files", "applied_records", "daily_report_rows")


class DailyReportFinalizeView(FiduciaryImportRequiredMixin, DetailView):
    model = ImportBatch
    template_name = "fiduciary/daily_report_finalize_confirm.html"
    context_object_name = "batch"

    def get_queryset(self):
        return daily_report_batches().prefetch_related("files", "daily_report_rows")

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        try:
            result = finalize_daily_report_import(batch_id=self.object.pk, user=request.user)
        except PermissionDenied:
            raise
        except Exception as exc:
            messages.error(request, str(exc))
            return redirect("fiduciary:daily_report_preview", pk=self.object.pk)
        messages.success(
            request,
            f"Reporte diario aplicado. Pagos creados: {result.imported_rows}. Duplicados omitidos: {result.duplicate_rows}.",
        )
        return redirect("fiduciary:daily_report_preview", pk=self.object.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["imported_file"] = self.object.files.order_by("order", "original_name").first()
        context["can_finalize"] = self.object.status == ImportBatch.Status.READY
        return context


class ClientListView(FiduciaryReadRequiredMixin, QueryStringMixin, ListView):
    model = Client
    template_name = "fiduciary/client_list.html"
    context_object_name = "clients"
    paginate_by = 10

    def get_queryset(self):
        queryset = Client.objects.annotate(
            current_units_count=Count("unit_ownerships__property_unit", filter=Q(unit_ownerships__is_active=True), distinct=True),
            assignments_count=Count("fiduciary_assignment_holders__assignment", distinct=True),
        )
        self.filter_form = ClientFilterForm(self.request.GET)
        if self.filter_form.is_valid():
            q = self.filter_form.cleaned_data.get("q")
            document = self.filter_form.cleaned_data.get("document")
            document_type = self.filter_form.cleaned_data.get("document_type")
            information_status = self.filter_form.cleaned_data.get("information_status")
            status = self.filter_form.cleaned_data.get("status")
            project = self.filter_form.cleaned_data.get("project")
            grouping_type = self.filter_form.cleaned_data.get("grouping_type")
            structural_group = self.filter_form.cleaned_data.get("structural_group")
            property_unit = self.filter_form.cleaned_data.get("property_unit")
            if q:
                queryset = queryset.filter(
                    Q(first_names__icontains=q)
                    | Q(last_names_or_company__icontains=q)
                    | Q(document_number__icontains=q)
                    | Q(phone__icontains=q)
                    | Q(email__icontains=q)
                )
            if document:
                queryset = queryset.annotate(
                    normalized_document_number=Replace(
                        Replace(
                            Replace(
                                Replace("document_number", Value(" "), Value("")),
                                Value("."),
                                Value(""),
                            ),
                            Value(","),
                            Value(""),
                        ),
                        Value("-"),
                        Value(""),
                    )
                ).filter(normalized_document_number__icontains=document)
            if document_type:
                queryset = queryset.filter(document_type=document_type)
            if information_status:
                queryset = queryset.filter(information_status=information_status)
            if status == "active":
                queryset = queryset.filter(is_active=True)
            elif status == "inactive":
                queryset = queryset.filter(is_active=False)
            if project:
                queryset = queryset.filter(unit_ownerships__property_unit__project=project)
            if grouping_type:
                queryset = queryset.filter(unit_ownerships__property_unit__structural_group__grouping_type=grouping_type)
            if structural_group:
                queryset = queryset.filter(unit_ownerships__property_unit__structural_group=structural_group)
            if property_unit:
                queryset = queryset.filter(unit_ownerships__property_unit=property_unit)
        return queryset.distinct().order_by("last_names_or_company", "first_names", "document_number")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter_form"] = getattr(self, "filter_form", ClientFilterForm(self.request.GET))
        return self.add_common_context(context)


class ClientDetailView(FiduciaryReadRequiredMixin, DetailView):
    model = Client
    template_name = "fiduciary/client_detail.html"
    context_object_name = "client_obj"

    def get_queryset(self):
        return Client.objects.prefetch_related(
            Prefetch(
                "unit_ownerships",
                queryset=UnitOwnership.objects.select_related(
                    "property_unit",
                    "property_unit__project",
                    "property_unit__structural_group",
                ).order_by("-is_active", "-start_date"),
            ),
            Prefetch(
                "fiduciary_assignment_holders",
                queryset=FiduciaryAssignmentHolder.objects.select_related(
                    "assignment",
                    "assignment__property_unit",
                    "assignment__property_unit__project",
                    "assignment__property_unit__structural_group",
                ).order_by("-is_active", "-start_date"),
            ),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_create"] = can_create_fiduciary(self.request.user)
        context["can_update"] = can_update_fiduciary(self.request.user)
        context["can_manage"] = context["can_update"]
        context["novelties"] = OperationalNovelty.objects.filter(
            Q(previous_client=self.object) | Q(new_client=self.object) | Q(historical_client=self.object)
        ).select_related(
            "property_unit",
            "property_unit__structural_group",
            "previous_assignment",
            "new_assignment",
            "historical_assignment",
            "created_by",
        ).order_by("-created_at", "-pk")
        novelty_unit_ids = []
        novelty_assignment_ids = []
        for novelty in context["novelties"]:
            if novelty.property_unit_id:
                novelty_unit_ids.append(novelty.property_unit_id)
            novelty_assignment_ids.extend(
                assignment_id
                for assignment_id in (
                    novelty.previous_assignment_id,
                    novelty.new_assignment_id,
                    novelty.historical_assignment_id,
                )
                if assignment_id
            )
        observation_filters = (
            Q(assignment__holders__client=self.object)
            | Q(client=self.object)
            | Q(operational_novelty__previous_client=self.object)
            | Q(operational_novelty__new_client=self.object)
            | Q(operational_novelty__historical_client=self.object)
            | Q(source_novelty__operational_novelties__previous_client=self.object)
            | Q(source_novelty__operational_novelties__new_client=self.object)
            | Q(source_novelty__operational_novelties__historical_client=self.object)
        )
        if novelty_unit_ids and novelty_assignment_ids:
            observation_filters |= Q(property_unit_id__in=novelty_unit_ids, assignment_id__in=novelty_assignment_ids)
        context["related_observations"] = (
            ImportedHistoricalObservation.objects.exclude(origin="historical_novelty")
            .filter(operational_novelty__isnull=True)
            .filter(observation_filters)
            .select_related("property_unit", "property_unit__structural_group", "assignment")
            .distinct()
            .order_by("-created_at", "-pk")
        )
        return context


class AdministrativeDeleteConfirmView(FiduciaryManagementRequiredMixin, TemplateView):
    template_name = "fiduciary/admin_delete_confirm.html"
    model = None
    success_url = None
    action_label = ""
    warning = ""

    def dispatch(self, request, *args, **kwargs):
        self.object = get_object_or_404(self.model, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_summary(self):
        raise NotImplementedError

    def perform_delete(self, reason: str):
        raise NotImplementedError

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        summary = self.get_summary()
        context.update(
            {
                "object": self.object,
                "action_label": self.action_label,
                "warning": self.warning,
                "summary": summary,
                "summary_items": summary.as_dict().items(),
                "cancel_url": self.success_url,
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        if request.POST.get("confirm") != "yes":
            messages.error(request, "Debe confirmar la eliminacion.")
            return redirect(self.success_url)
        reason = request.POST.get("change_reason", "").strip()
        if not reason:
            messages.error(request, "Debe registrar el motivo de la eliminacion.")
            return redirect(request.path)
        try:
            result = self.perform_delete(reason)
        except ValidationError as exc:
            messages.error(request, _validation_error_text(exc))
            return redirect(request.path)
        if result is False:
            return redirect(request.path)
        messages.success(request, "Eliminacion administrativa ejecutada correctamente.")
        return redirect(self.success_url)


class ClientDeleteView(AdministrativeDeleteConfirmView):
    model = Client
    success_url = reverse_lazy("fiduciary:client_list")
    action_label = "Eliminar cliente"
    warning = (
        "El cliente solo se eliminara si no tiene titularidades, encargos, pagos, "
        "novedades u observaciones relacionadas. No se ejecuta cascada destructiva."
    )

    def get_summary(self):
        from .admin_cleanup import client_dependency_summary

        return client_dependency_summary(self.object)

    def perform_delete(self, reason: str):
        from .admin_cleanup import client_dependency_summary, delete_client_if_orphan

        summary = client_dependency_summary(self.object)
        has_dependencies = any(count for key, count in summary.as_dict().items() if key != "clientes")
        if has_dependencies:
            messages.error(self.request, f"No se elimino el cliente porque tiene dependencias: {summary.text()}.")
            return False
        delete_client_if_orphan(self.object, user=self.request.user, reason=reason)
        return True


class ClientCreateView(FiduciaryCreateRequiredMixin, CreateView):
    model = Client
    form_class = ClientForm
    template_name = "fiduciary/form.html"
    success_url = reverse_lazy("fiduciary:client_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Nuevo cliente"
        context["back_url"] = "fiduciary:client_list"
        return context

    def form_valid(self, form):
        with transaction.atomic():
            self.object = form.save()
            audit_event(
                user=self.request.user,
                action="Creado",
                entity="Cliente",
                obj=self.object,
                context={
                    "Cliente": self.object.full_name,
                    "Documento": self.object.document_number,
                    "Correo": self.object.email,
                },
            )
        messages.success(self.request, "Cliente creado correctamente.")
        return redirect(self.success_url)


class ClientUpdateView(FiduciaryManagementRequiredMixin, UpdateView):
    model = Client
    form_class = ClientUpdateForm
    template_name = "fiduciary/form.html"
    success_url = reverse_lazy("fiduciary:client_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Editar cliente"
        context["back_url"] = "fiduciary:client_list"
        return context

    def form_valid(self, form):
        with transaction.atomic():
            before = _audit_snapshot(
                self.object,
                ("first_names", "last_names_or_company", "document_number", "email", "phone", "is_active"),
            )
            self.object = form.save()
            after = _audit_snapshot(
                self.object,
                ("first_names", "last_names_or_company", "document_number", "email", "phone", "is_active"),
            )
            audit_event(
                user=self.request.user,
                action="Modificado",
                entity="Cliente",
                obj=self.object,
                context={"Cliente": self.object.full_name, "Documento": self.object.document_number},
                before=before,
                after=after,
            )
        messages.success(self.request, "Cliente actualizado correctamente.")
        return redirect(self.success_url)


class ClientStatusView(FiduciaryManagementRequiredMixin, View):
    def post(self, request, pk, action):
        if action not in {"activate", "deactivate"}:
            raise Http404("Accion no disponible.")
        client = get_object_or_404(Client, pk=pk)
        form = StatusReasonForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Debe registrar el motivo.")
            return redirect("fiduciary:client_list")
        with transaction.atomic():
            before = f"is_active: {client.is_active}"
            client.is_active = action == "activate"
            client.last_change_reason = form.cleaned_data["change_reason"]
            client.save(update_fields=["is_active", "last_change_reason", "updated_at"])
            audit_event(
                user=request.user,
                action="Modificado",
                entity="Cliente",
                obj=client,
                description="Cliente activado." if client.is_active else "Cliente inactivado.",
                context={"Cliente": client.full_name, "Documento": client.document_number, "Motivo": client.last_change_reason},
                before=before,
                after=f"is_active: {client.is_active}",
            )
        messages.success(request, "Cliente actualizado correctamente.")
        return redirect("fiduciary:client_list")


class ObservationListView(FiduciaryReadRequiredMixin, QueryStringMixin, ListView):
    model = ImportedHistoricalObservation
    template_name = "fiduciary/observation_list.html"
    context_object_name = "observations"
    paginate_by = 10

    def get_queryset(self):
        queryset = ImportedHistoricalObservation.objects.exclude(origin="historical_novelty").select_related(
            "project",
            "property_unit",
            "property_unit__structural_group",
            "assignment",
            "imported_by",
        ).order_by("-created_at", "-pk")
        self.filter_form = ObservationFilterForm(self.request.GET)
        if self.filter_form.is_valid():
            project = self.filter_form.cleaned_data.get("project")
            grouping_type = self.filter_form.cleaned_data.get("grouping_type")
            structural_group = self.filter_form.cleaned_data.get("structural_group")
            unit = self.filter_form.cleaned_data.get("property_unit")
            client = self.filter_form.cleaned_data.get("client")
            document = self.filter_form.cleaned_data.get("document")
            assignment_number = self.filter_form.cleaned_data.get("assignment_number")
            origin = self.filter_form.cleaned_data.get("origin")
            date_from = self.filter_form.cleaned_data.get("date_from")
            date_to = self.filter_form.cleaned_data.get("date_to")
            if project:
                queryset = queryset.filter(property_unit__project=project)
            if grouping_type:
                queryset = queryset.filter(property_unit__structural_group__grouping_type=grouping_type)
            if structural_group:
                queryset = queryset.filter(property_unit__structural_group=structural_group)
            if unit:
                queryset = queryset.filter(property_unit=unit)
            if client:
                queryset = queryset.filter(
                    Q(assignment__holders__client=client)
                    | Q(client=client)
                )
            if document:
                normalized = document
                queryset = queryset.annotate(
                    normalized_document_number=Replace(
                        Replace(
                            Replace(
                                Replace("assignment__holders__client__document_number", Value(" "), Value("")),
                                Value("."),
                                Value(""),
                            ),
                            Value(","),
                            Value(""),
                        ),
                        Value("-"),
                        Value(""),
                    )
                ).filter(
                    Q(normalized_document_number__icontains=normalized)
                    | Q(client__document_number__icontains=normalized)
                )
            if assignment_number:
                queryset = queryset.filter(assignment__assignment_number__icontains=assignment_number)
            if origin:
                queryset = queryset.filter(origin=origin)
            if date_from:
                queryset = queryset.filter(created_at__date__gte=date_from)
            if date_to:
                queryset = queryset.filter(created_at__date__lte=date_to)
        return queryset.distinct()

    def get_context_data(self, **kwargs):
        context = self.add_common_context(super().get_context_data(**kwargs))
        context["filter_form"] = getattr(self, "filter_form", ObservationFilterForm(self.request.GET))
        return context


class ObservationCreateView(FiduciaryManagementRequiredMixin, CreateView):
    model = ImportedHistoricalObservation
    form_class = ObservationForm
    template_name = "fiduciary/observation_form.html"
    success_url = reverse_lazy("fiduciary:observation_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        try:
            with transaction.atomic():
                self.object = form.save()
                audit_event(
                    user=self.request.user,
                    action="Creado",
                    entity="Observacion",
                    obj=self.object,
                    context={
                        "Proyecto": self.object.project,
                        "Unidad": self.object.property_unit,
                        "Cliente": self.object.client,
                        "Encargo": self.object.assignment,
                    },
                    after=_audit_snapshot(self.object, ("summary", "detail", "origin")),
                )
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)
        messages.success(self.request, "Observacion creada correctamente.")
        return redirect(self.success_url)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Nueva observacion"
        context["back_url"] = "fiduciary:observation_list"
        return context


class ObservationUpdateView(FiduciaryManagementRequiredMixin, UpdateView):
    model = ImportedHistoricalObservation
    form_class = ObservationForm
    template_name = "fiduciary/observation_form.html"
    success_url = reverse_lazy("fiduciary:observation_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["require_change_reason"] = True
        kwargs["lock_context"] = True
        return kwargs

    def form_valid(self, form):
        try:
            with transaction.atomic():
                before = _audit_snapshot(self.object, ("summary", "detail", "property_unit_id", "assignment_id"))
                self.object = form.save()
                _log_observation_change(self.request.user, self.object, form.cleaned_data["change_reason"], before)
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)
        messages.success(self.request, "Observacion actualizada correctamente.")
        return redirect(self.success_url)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Editar observacion"
        context["back_url"] = "fiduciary:observation_list"
        observation = self.object
        unit = observation.property_unit
        group = unit.structural_group if unit else None
        context["readonly_context"] = {
            "Proyecto": unit.project if unit else observation.project,
            "Tipo de agrupacion": group.grouping_type if group else None,
            "Agrupacion": group,
            "Unidad": unit,
            "Encargo fiduciario": observation.assignment,
        }
        return context


class ObservationDeleteView(AdministrativeDeleteConfirmView):
    model = ImportedHistoricalObservation
    success_url = reverse_lazy("fiduciary:observation_list")
    action_label = "Eliminar observacion"
    warning = "Esta accion eliminara la observacion seleccionada. No elimina clientes, unidades, encargos ni pagos."

    def dispatch(self, request, *args, **kwargs):
        self.object = get_object_or_404(
            ImportedHistoricalObservation.objects.exclude(origin="historical_novelty"),
            pk=kwargs["pk"],
        )
        return TemplateView.dispatch(self, request, *args, **kwargs)

    def get_summary(self):
        class Summary:
            def as_dict(self):
                return {"Observaciones": 1}

        return Summary()

    def perform_delete(self, reason: str):
        with transaction.atomic():
            before = _audit_snapshot(self.object, ("summary", "detail", "property_unit_id", "assignment_id", "origin"))
            _log_object_action(
                self.request.user,
                self.object,
                "ELIMINAR_OBSERVACION",
                DELETION,
                f"ELIMINAR_OBSERVACION | Antes: {before} | Motivo: {reason}",
            )
            self.object.related_payments.clear()
            self.object.delete()
        return True


class ObservationDetailView(FiduciaryReadRequiredMixin, QueryStringMixin, DetailView):
    model = ImportedHistoricalObservation
    template_name = "fiduciary/observation_detail.html"
    context_object_name = "observation"

    def get_queryset(self):
        return ImportedHistoricalObservation.objects.exclude(origin="historical_novelty").select_related(
            "project",
            "property_unit",
            "client",
            "assignment",
            "imported_by",
            "imported_file",
            "batch",
        )

    def get_context_data(self, **kwargs):
        return self.add_common_context(super().get_context_data(**kwargs))


class ObservationContextView(FiduciaryReadRequiredMixin, View):
    def get(self, request):
        project_id = request.GET.get("project")
        unit_id = request.GET.get("unit")
        payload = {"units": [], "clients": [], "assignments": []}
        if project_id:
            units = with_natural_unit_order(
                PropertyUnit.objects.filter(project_id=project_id, is_active=True).select_related("structural_group")
            )
            payload["units"] = [{"id": unit.pk, "text": property_unit_choice_label(unit)} for unit in units]
        if not unit_id:
            return JsonResponse(payload)
        clients = (
            Client.objects.filter(
                Q(unit_ownerships__property_unit_id=unit_id)
                | Q(fiduciary_assignment_holders__assignment__property_unit_id=unit_id)
                | Q(historical_observations__property_unit_id=unit_id)
                | Q(novelties_as_previous_client__property_unit_id=unit_id)
                | Q(novelties_as_new_client__property_unit_id=unit_id)
                | Q(historical_novelties__property_unit_id=unit_id)
            )
            .distinct()
            .order_by("last_names_or_company", "first_names", "document_number")
        )
        assignments = (
            FiduciaryAssignment.objects.filter(property_unit_id=unit_id)
            .prefetch_related("holders__client")
            .order_by("-is_active", "assignment_number")
        )
        payload["clients"] = [{"id": client.pk, "text": f"{client.full_name} - {client.document_number or 'Sin documento'}"} for client in clients]
        payload["assignments"] = [{"id": assignment.pk, "text": assignment_choice_label(assignment)} for assignment in assignments]
        return JsonResponse(payload)


class UnitHierarchyTypesView(FiduciaryReadRequiredMixin, View):
    def get(self, request):
        project_id = request.GET.get("project")
        types = GroupingType.objects.none()
        if project_id:
            types = (
                GroupingType.objects.filter(
                    is_active=True,
                    structural_groups__project_id=project_id,
                    structural_groups__is_active=True,
                )
                .distinct()
                .order_by("name")
            )
        return JsonResponse({"results": [{"id": item.pk, "text": item.name} for item in types]})


class UnitHierarchyGroupsView(FiduciaryReadRequiredMixin, View):
    def get(self, request):
        project_id = request.GET.get("project")
        grouping_type_id = request.GET.get("grouping_type")
        groups = StructuralGroup.objects.none()
        if project_id and grouping_type_id:
            groups = StructuralGroup.objects.filter(
                is_active=True,
                project_id=project_id,
                grouping_type_id=grouping_type_id,
            ).order_by("name", "code")
        return JsonResponse({"results": [{"id": item.pk, "text": str(item)} for item in groups]})


class UnitHierarchyUnitsView(FiduciaryReadRequiredMixin, View):
    def get(self, request):
        project_id = request.GET.get("project")
        group_id = request.GET.get("structural_group")
        units = PropertyUnit.objects.none()
        if project_id and group_id:
            units = with_natural_unit_order(
                PropertyUnit.objects.filter(
                    is_active=True,
                    project_id=project_id,
                    structural_group_id=group_id,
                ).select_related("structural_group")
            )
        return JsonResponse({"results": [{"id": item.pk, "text": property_unit_choice_label(item)} for item in units]})


class NoveltyListView(FiduciaryReadRequiredMixin, QueryStringMixin, ListView):
    model = OperationalNovelty
    template_name = "fiduciary/novelty_list.html"
    context_object_name = "novelties"
    paginate_by = 10

    def get_queryset(self):
        queryset = OperationalNovelty.objects.select_related(
            "project",
            "property_unit",
            "property_unit__structural_group",
            "previous_client",
            "new_client",
            "historical_client",
            "previous_assignment",
            "new_assignment",
            "historical_assignment",
            "created_by",
        ).order_by("-created_at", "-pk")
        self.filter_form = NoveltyFilterForm(self.request.GET)
        if self.filter_form.is_valid():
            project = self.filter_form.cleaned_data.get("project")
            grouping_type = self.filter_form.cleaned_data.get("grouping_type")
            structural_group = self.filter_form.cleaned_data.get("structural_group")
            unit = self.filter_form.cleaned_data.get("property_unit")
            novelty_type = self.filter_form.cleaned_data.get("novelty_type")
            client = self.filter_form.cleaned_data.get("client")
            document = self.filter_form.cleaned_data.get("document")
            assignment_number = self.filter_form.cleaned_data.get("assignment_number")
            origin = self.filter_form.cleaned_data.get("origin")
            date_from = self.filter_form.cleaned_data.get("date_from")
            date_to = self.filter_form.cleaned_data.get("date_to")
            if project:
                queryset = queryset.filter(property_unit__project=project)
            if grouping_type:
                queryset = queryset.filter(property_unit__structural_group__grouping_type=grouping_type)
            if structural_group:
                queryset = queryset.filter(property_unit__structural_group=structural_group)
            if unit:
                queryset = queryset.filter(property_unit=unit)
            if novelty_type:
                queryset = queryset.filter(novelty_type=novelty_type)
            if client:
                queryset = queryset.filter(Q(previous_client=client) | Q(new_client=client) | Q(historical_client=client))
            if document:
                queryset = queryset.filter(
                    Q(previous_client__document_number__icontains=document)
                    | Q(new_client__document_number__icontains=document)
                    | Q(historical_client__document_number__icontains=document)
                )
            if assignment_number:
                queryset = queryset.filter(
                    Q(previous_assignment__assignment_number__icontains=assignment_number)
                    | Q(new_assignment__assignment_number__icontains=assignment_number)
                    | Q(historical_assignment__assignment_number__icontains=assignment_number)
                )
            if origin:
                queryset = queryset.filter(origin=origin)
            if date_from:
                queryset = queryset.filter(created_at__date__gte=date_from)
            if date_to:
                queryset = queryset.filter(created_at__date__lte=date_to)
        return queryset

    def get_context_data(self, **kwargs):
        context = self.add_common_context(super().get_context_data(**kwargs))
        context["filter_form"] = getattr(self, "filter_form", NoveltyFilterForm(self.request.GET))
        return context


class NoveltyDetailView(FiduciaryReadRequiredMixin, QueryStringMixin, DetailView):
    model = OperationalNovelty
    template_name = "fiduciary/novelty_detail.html"
    context_object_name = "novelty"

    def get_queryset(self):
        return OperationalNovelty.objects.select_related(
            "project",
            "property_unit",
            "previous_client",
            "new_client",
            "historical_client",
            "previous_assignment",
            "new_assignment",
            "historical_assignment",
            "created_by",
            "imported_file",
            "batch",
            "source_observation",
        )

    def get_context_data(self, **kwargs):
        context = self.add_common_context(super().get_context_data(**kwargs))
        novelty = self.object
        related_observations = ImportedHistoricalObservation.objects.exclude(origin="historical_novelty").filter(
            property_unit=novelty.property_unit,
        )
        client_ids = [
            client_id
            for client_id in (novelty.previous_client_id, novelty.new_client_id, novelty.historical_client_id)
            if client_id
        ]
        assignment_ids = [
            assignment_id
            for assignment_id in (
                novelty.previous_assignment_id,
                novelty.new_assignment_id,
                novelty.historical_assignment_id,
            )
            if assignment_id
        ]
        filters = Q(pk=novelty.source_observation_id) if novelty.source_observation_id else Q()
        if not novelty.source_observation_id and client_ids:
            filters |= Q(client_id__in=client_ids)
        if not novelty.source_observation_id and assignment_ids:
            filters |= Q(assignment_id__in=assignment_ids)
        context["related_observations"] = (
            related_observations.filter(filters).select_related("client", "assignment", "imported_by").distinct()
            if filters
            else related_observations.none()
        )
        return context


class NoveltyUpdateView(FiduciaryManagementRequiredMixin, UpdateView):
    model = OperationalNovelty
    form_class = OperationalNoveltyEditForm
    template_name = "fiduciary/novelty_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["structural_locked"] = _novelty_has_structural_effect(self.object)
        return kwargs

    def form_valid(self, form):
        reason = self.request.POST.get("change_reason", "").strip()
        if not reason:
            form.add_error(None, "Registre el motivo de la modificacion.")
            return self.form_invalid(form)
        try:
            with transaction.atomic():
                before = _audit_snapshot(self.object, ("effective_date", "summary", "detail", "other_type"))
                self.object = form.save()
                after = _audit_snapshot(self.object, ("effective_date", "summary", "detail", "other_type"))
                _log_object_action(
                    self.request.user,
                    self.object,
                    "MODIFICAR_NOVEDAD",
                    CHANGE,
                    f"MODIFICAR_NOVEDAD | Antes: {before} | Despues: {after} | Motivo: {reason}",
                )
        except ValidationError as exc:
            _add_validation_errors_to_form(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, "Novedad actualizada correctamente.")
        return redirect("fiduciary:novelty_detail", pk=self.object.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Editar novedad"
        context["edit_mode"] = True
        context["cancel_url"] = reverse("fiduciary:novelty_detail", args=[self.object.pk])
        context["client_search_url"] = reverse("fiduciary:novelty_client_search")
        context["assignment_clients_url"] = reverse("fiduciary:novelty_assignment_clients")
        selected_clients = {}
        for client in (self.object.previous_client, self.object.historical_client, self.object.new_client):
            if client:
                selected_clients[str(client.pk)] = {"text": client.full_name}
        context["selected_clients_json"] = json.dumps(selected_clients)
        return context


class NoveltyDeleteView(AdministrativeDeleteConfirmView):
    model = OperationalNovelty
    success_url = reverse_lazy("fiduciary:novelty_list")
    action_label = "Eliminar novedad"
    warning = (
        "Las novedades hacen parte de la trazabilidad historica y no se pueden eliminar manualmente."
    )

    def dispatch(self, request, *args, **kwargs):
        raise PermissionDenied("La eliminacion manual de novedades no esta permitida.")

    def get_summary(self):
        class Summary:
            def as_dict(self):
                return {"Novedades": 1}

        return Summary()

    def perform_delete(self, reason: str):
        raise PermissionDenied("La eliminacion manual de novedades no esta permitida.")


class NoveltyCreateView(FiduciaryManagementRequiredMixin, QueryStringMixin, FormView):
    form_class = OperationalNoveltyForm
    template_name = "fiduciary/novelty_form.html"
    success_url = reverse_lazy("fiduciary:novelty_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        try:
            result = apply_operational_novelty(
                unit=form.cleaned_data["property_unit"],
                novelty_type=form.cleaned_data["novelty_type"],
                effective_date=form.cleaned_data["effective_date"],
                summary=form.cleaned_data.get("summary", ""),
                detail=form.cleaned_data.get("detail", ""),
                user=self.request.user,
                new_client=form.cleaned_data.get("new_client"),
                current_client=form.cleaned_data.get("current_client"),
                current_assignment=form.cleaned_data.get("current_assignment"),
                new_assignment_number=form.cleaned_data.get("new_assignment_number", ""),
                secondary_clients=form.cleaned_data.get("secondary_clients"),
                other_type=form.cleaned_data.get("other_type", ""),
            )
        except ValidationError as exc:
            _add_validation_errors_to_form(form, exc)
            return self.form_invalid(form)
        audit_event(
            user=self.request.user,
            action="Creado",
            entity="Novedad",
            obj=result.novelty,
            context={
                "Proyecto": result.novelty.project,
                "Unidad": result.novelty.property_unit,
                "Tipo": result.novelty.get_novelty_type_display(),
                "Cliente actual": result.novelty.previous_client or result.novelty.historical_client,
                "Cliente nuevo": result.novelty.new_client,
                "Encargo": result.novelty.new_assignment or result.novelty.previous_assignment or result.novelty.historical_assignment,
            },
            after=_audit_snapshot(result.novelty, ("summary", "detail", "origin", "status")),
        )
        messages.success(self.request, "Novedad registrada correctamente.")
        return redirect("fiduciary:novelty_detail", pk=result.novelty.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Nueva novedad"
        context["client_search_url"] = reverse("fiduciary:novelty_client_search")
        context["assignment_clients_url"] = reverse("fiduciary:novelty_assignment_clients")
        form = context.get("form")
        selected_ids = []
        if form and form.is_bound:
            selected_ids.extend([form.data.get("new_client"), *form.data.getlist("secondary_clients")])
            selected_ids.extend([form.data.get("current_client")])
        selected_clients = Client.objects.filter(pk__in=[item for item in selected_ids if str(item).isdigit()])
        context["selected_clients_json"] = json.dumps(
            {
                str(client.pk): {
                    "id": client.pk,
                    "text": f"{client.document_number or 'Sin documento'} | {client.full_name} | {client.email or 'Sin correo'}",
                }
                for client in selected_clients
            }
        )
        return context


class PaymentListView(FiduciaryReadRequiredMixin, QueryStringMixin, ListView):
    model = Payment
    template_name = "fiduciary/payment_list.html"
    context_object_name = "payments"
    paginate_by = 10

    def get_queryset(self):
        self.filter_form = PaymentFilterForm(self.request.GET)
        self.search_performed = self.filter_form.has_criteria()
        queryset = Payment.objects.select_related(
            "assignment",
            "assignment__property_unit",
            "assignment__property_unit__project",
            "assignment__property_unit__structural_group",
            "assignment__property_unit__structural_group__grouping_type",
            "source_file",
            "source_file__batch",
        ).prefetch_related(
            Prefetch(
                "assignment__holders",
                queryset=FiduciaryAssignmentHolder.objects.select_related("client").order_by("-is_primary", "client__last_names_or_company"),
                to_attr="prefetched_holders",
            )
        ).order_by("-exact_date", "-period_year", "-period_month", "-created_at", "-pk")
        if not self.search_performed:
            return queryset.none()
        if self.filter_form.is_valid():
            project = self.filter_form.cleaned_data.get("project")
            grouping_type = self.filter_form.cleaned_data.get("grouping_type")
            structural_group = self.filter_form.cleaned_data.get("structural_group")
            unit = self.filter_form.cleaned_data.get("property_unit")
            client = self.filter_form.cleaned_data.get("client")
            document = self.filter_form.cleaned_data.get("document")
            assignment_number = self.filter_form.cleaned_data.get("assignment_number")
            date_from = self.filter_form.cleaned_data.get("date_from")
            date_to = self.filter_form.cleaned_data.get("date_to")
            if project:
                queryset = queryset.filter(assignment__property_unit__project=project)
            if grouping_type:
                queryset = queryset.filter(assignment__property_unit__structural_group__grouping_type=grouping_type)
            if structural_group:
                queryset = queryset.filter(assignment__property_unit__structural_group=structural_group)
            if unit:
                queryset = queryset.filter(assignment__property_unit=unit)
            if client:
                queryset = queryset.filter(assignment__holders__client=client)
            if document:
                queryset = queryset.annotate(
                    normalized_holder_document=Replace(
                        Replace(
                            Replace(
                                Replace("assignment__holders__client__document_number", Value(" "), Value("")),
                                Value("."),
                                Value(""),
                            ),
                            Value(","),
                            Value(""),
                        ),
                        Value("-"),
                        Value(""),
                    )
                ).filter(normalized_holder_document__icontains=document)
            if assignment_number:
                queryset = queryset.filter(assignment__assignment_number__icontains=assignment_number)
            if date_from:
                queryset = queryset.filter(
                    Q(exact_date__gte=date_from)
                    | Q(period_year__gt=date_from.year)
                    | Q(period_year=date_from.year, period_month__gte=date_from.month)
                )
            if date_to:
                queryset = queryset.filter(
                    Q(exact_date__lte=date_to)
                    | Q(period_year__lt=date_to.year)
                    | Q(period_year=date_to.year, period_month__lte=date_to.month)
                )
        return queryset.distinct()

    def get_context_data(self, **kwargs):
        context = self.add_common_context(super().get_context_data(**kwargs))
        for payment in context["payments"]:
            holders = getattr(payment.assignment, "prefetched_holders", [])
            payment.display_client = next((holder.client for holder in holders if holder.is_primary), None) or (
                holders[0].client if holders else None
            )
            payment.display_movement_type = _payment_movement_type_label(payment)
        context["filter_form"] = getattr(self, "filter_form", PaymentFilterForm(self.request.GET))
        context["search_performed"] = getattr(self, "search_performed", False)
        return context


class PaymentCreateView(FiduciaryCreateRequiredMixin, QueryStringMixin, FormView):
    form_class = GlobalManualPaymentForm
    template_name = "fiduciary/payment_form.html"

    def form_valid(self, form):
        assignment = form.cleaned_data["assignment"]
        try:
            payment = _create_manual_payment_for_assignment(assignment, form.cleaned_data, self.request.user)
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)
        _audit_manual_payment_created(self.request.user, payment)
        messages.success(self.request, "Pago registrado correctamente.")
        return redirect(f"{reverse('fiduciary:payment_list')}?assignment_number={assignment.assignment_number}")

    def get_context_data(self, **kwargs):
        context = self.add_common_context(super().get_context_data(**kwargs))
        context["title"] = "Registrar pago"
        context["global_payment"] = True
        context["unit_assignment_url"] = reverse("fiduciary:payment_unit_assignment")
        return context


class PaymentUpdateView(FiduciaryManagementRequiredMixin, UpdateView):
    model = Payment
    form_class = PaymentEditForm
    template_name = "fiduciary/payment_edit_form.html"

    def get_queryset(self):
        return Payment.objects.select_related("assignment", "assignment__property_unit", "source_file")

    def form_valid(self, form):
        reason = self.request.POST.get("change_reason", "").strip()
        if not reason:
            form.add_error(None, "Registre el motivo de la modificacion.")
            return self.form_invalid(form)
        try:
            with transaction.atomic():
                before = _audit_snapshot(
                    self.object,
                    ("date_precision", "exact_date", "period_year", "period_month", "amount", "concept", "destination"),
                )
                self.object = form.save(commit=False)
                self.object.full_clean()
                self.object.save()
                after = _audit_snapshot(
                    self.object,
                    ("date_precision", "exact_date", "period_year", "period_month", "amount", "concept", "destination"),
                )
                _log_object_action(
                    self.request.user,
                    self.object,
                    "MODIFICAR_PAGO",
                    CHANGE,
                    f"MODIFICAR_PAGO | Antes: {before} | Despues: {after} | Motivo: {reason}",
                )
        except ValidationError as exc:
            _add_validation_errors_to_form(form, exc)
            return self.form_invalid(form)
        except IntegrityError:
            form.add_error(None, "Ya existe un pago con la misma identidad para este encargo.")
            return self.form_invalid(form)
        messages.success(self.request, "Pago actualizado correctamente.")
        return redirect(f"{reverse('fiduciary:payment_list')}?assignment_number={self.object.assignment.assignment_number}")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Editar pago"
        context["payment"] = self.object
        return context


class PaymentDeleteView(AdministrativeDeleteConfirmView):
    model = Payment
    success_url = reverse_lazy("fiduciary:payment_list")
    action_label = "Eliminar pago"
    warning = "Esta accion eliminara solo el pago seleccionado. No elimina cliente, unidad, proyecto ni encargo."

    def get_summary(self):
        class Summary:
            def as_dict(self):
                return {"Pagos": 1}

        return Summary()

    def perform_delete(self, reason: str):
        with transaction.atomic():
            before = _audit_snapshot(
                self.object,
                (
                    "assignment_id",
                    "date_precision",
                    "exact_date",
                    "period_year",
                    "period_month",
                    "amount",
                    "concept",
                    "destination",
                    "source_file_id",
                ),
            )
            _log_object_action(
                self.request.user,
                self.object,
                "ELIMINAR_PAGO",
                DELETION,
                f"ELIMINAR_PAGO | Antes: {before} | Motivo: {reason}",
            )
            _unlink_payment_dependents(self.object)
            self.object.delete()
        return True


class PaymentUnitAssignmentView(FiduciaryCreateRequiredMixin, View):
    def get(self, request):
        unit_id = request.GET.get("unit")
        if not unit_id:
            return JsonResponse({"assignment": None, "message": "Seleccione una unidad."})
        assignments = list(
            FiduciaryAssignment.objects.filter(property_unit_id=unit_id, is_active=True)
            .select_related("property_unit", "property_unit__project", "property_unit__structural_group")
            .order_by("-start_date", "-pk")
        )
        payable_assignments = [assignment for assignment in assignments if assignment_can_receive_payment(assignment)]
        if not payable_assignments:
            return JsonResponse(
                {
                    "assignment": None,
                    "message": "La unidad seleccionada no tiene un encargo fiduciario activo con titulares vigentes.",
                }
            )
        if len(payable_assignments) > 1:
            return JsonResponse({"assignment": None, "message": "La unidad seleccionada tiene mas de un encargo activo."})
        assignment = payable_assignments[0]
        unit = assignment.property_unit
        group = f"{unit.structural_group} | " if unit.structural_group_id else ""
        return JsonResponse(
            {
                "assignment": {
                    "id": assignment.pk,
                    "number": assignment.assignment_number,
                    "text": f"{assignment.assignment_number} | {unit.project.name} | {group}{unit.name or unit.code}",
                }
            }
        )


class AuditListView(FiduciaryManagementRequiredMixin, QueryStringMixin, ListView):
    model = AuditEvent
    template_name = "fiduciary/audit_list.html"
    context_object_name = "records"
    paginate_by = 10

    def get_queryset(self):
        queryset = AuditEvent.objects.select_related("user").order_by("-created_at", "-pk")
        self.filter_form = AuditFilterForm(self.request.GET)
        if self.filter_form.is_valid():
            responsible = self.filter_form.cleaned_data.get("responsible")
            date_from = self.filter_form.cleaned_data.get("date_from")
            date_to = self.filter_form.cleaned_data.get("date_to")
            action = self.filter_form.cleaned_data.get("action")
            entity_kind = self.filter_form.cleaned_data.get("entity_kind")
            reason = self.filter_form.cleaned_data.get("reason")
            batch = self.filter_form.cleaned_data.get("batch")
            imported_file = self.filter_form.cleaned_data.get("imported_file")
            if responsible:
                queryset = queryset.filter(user=responsible)
            if date_from:
                queryset = queryset.filter(created_at__date__gte=date_from)
            if date_to:
                queryset = queryset.filter(created_at__date__lte=date_to)
            if action:
                queryset = queryset.filter(action=action)
            if entity_kind:
                queryset = queryset.filter(entity=entity_kind)
            if reason:
                queryset = queryset.filter(
                    Q(description__icontains=reason)
                    | Q(reason__icontains=reason)
                    | Q(entity_repr__icontains=reason)
                    | Q(context__icontains=reason)
                    | Q(summary__icontains=reason)
                )
            if batch:
                queryset = queryset.filter(Q(context__Lote=str(batch.pk)) | Q(context__Lote=batch.pk))
            if imported_file:
                queryset = queryset.filter(context__Archivo=imported_file.original_name)
        return queryset

    def get_context_data(self, **kwargs):
        context = self.add_common_context(super().get_context_data(**kwargs))
        context["filter_form"] = getattr(self, "filter_form", AuditFilterForm(self.request.GET))
        return context


class AuditDetailView(FiduciaryManagementRequiredMixin, QueryStringMixin, DetailView):
    model = AuditEvent
    template_name = "fiduciary/audit_detail.html"
    context_object_name = "record"

    def get_queryset(self):
        return AuditEvent.objects.select_related("user")

    def get_context_data(self, **kwargs):
        context = self.add_common_context(super().get_context_data(**kwargs))
        context["responsible"] = self.object.user
        context["context_items"] = self.object.context.items()
        context["before_items"] = self.object.before.items()
        context["after_items"] = self.object.after.items()
        keys = list(dict.fromkeys([*self.object.before.keys(), *self.object.after.keys()]))
        context["change_rows"] = [
            {"field": key, "before": self.object.before.get(key, ""), "after": self.object.after.get(key, "")}
            for key in keys
        ]
        context["summary_items"] = self.object.summary.items()
        return context


class ExportHomeView(FiduciaryReadRequiredMixin, QueryStringMixin, TemplateView):
    template_name = "fiduciary/export_home.html"

    def get_context_data(self, **kwargs):
        context = self.add_common_context(super().get_context_data(**kwargs))
        document_form = ExportDocumentFilterForm(self.request.GET)
        documents = ImportedFile.objects.select_related("batch", "batch__initiated_by").exclude(stored_path="").order_by("-created_at", "-pk")
        if document_form.is_valid():
            file_type = document_form.cleaned_data.get("file_type")
            filename = document_form.cleaned_data.get("filename")
            date_from = document_form.cleaned_data.get("date_from")
            date_to = document_form.cleaned_data.get("date_to")
            if file_type:
                documents = documents.filter(file_type=file_type)
            if filename:
                documents = documents.filter(original_name__icontains=filename)
            if date_from:
                documents = documents.filter(created_at__date__gte=date_from)
            if date_to:
                documents = documents.filter(created_at__date__lte=date_to)
        document_rows = []
        for item in documents[:50]:
            path = _stored_import_file_path(item)
            document_rows.append({"file": item, "available": bool(path and path.exists())})
        context["export_form"] = ExportHistoricalWorkbookForm()
        context["document_filter_form"] = document_form
        context["document_rows"] = document_rows
        return context


class HistoricalWorkbookExportView(FiduciaryReadRequiredMixin, View):
    def post(self, request):
        form = ExportHistoricalWorkbookForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Seleccione un proyecto valido para exportar.")
            return redirect("fiduciary:export_home")
        exported = export_historical_workbook(form.cleaned_data["project"])
        audit_event(
            user=request.user,
            action="Exportado",
            entity="Libro historico",
            obj=form.cleaned_data["project"],
            context={"Proyecto": form.cleaned_data["project"], "Archivo": exported.filename},
        )
        response = HttpResponse(
            exported.content,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{exported.filename}"'
        return response


class UploadedDocumentDownloadView(FiduciaryReadRequiredMixin, View):
    def get(self, request, pk):
        imported_file = get_object_or_404(ImportedFile.objects.select_related("batch"), pk=pk)
        path = _stored_import_file_path(imported_file)
        if not path or not path.exists():
            messages.error(request, "Archivo no disponible.")
            return redirect("fiduciary:export_home")
        audit_event(
            user=request.user,
            action="Descargado",
            entity="Archivo importado",
            obj=imported_file,
            context={"Archivo": imported_file.original_name, "Tipo": imported_file.get_file_type_display()},
        )
        return FileResponse(path.open("rb"), as_attachment=True, filename=imported_file.original_name)


def _stored_import_file_path(imported_file: ImportedFile) -> Path | None:
    if not imported_file.stored_path:
        return None
    media_root = Path(settings.MEDIA_ROOT).resolve()
    path = (media_root / imported_file.stored_path).resolve()
    try:
        path.relative_to(media_root)
    except ValueError:
        return None
    return path


def _redirect_retired_ownership_module(request, target="fiduciary:assignment_list"):
    messages.info(request, "La gestion operativa se realiza desde Encargos fiduciarios.")
    return redirect(target)


class UnitOwnershipListView(FiduciaryReadRequiredMixin, View):
    def get(self, request):
        return _redirect_retired_ownership_module(request)


class UnitOwnershipCreateView(FiduciaryCreateRequiredMixin, View):
    def get(self, request):
        return _redirect_retired_ownership_module(request, "fiduciary:assignment_create")

    def post(self, request):
        return _redirect_retired_ownership_module(request, "fiduciary:assignment_create")


class UnitOwnershipFinalizeView(FiduciaryManagementRequiredMixin, View):
    def get(self, request, pk):
        return _redirect_retired_ownership_module(request)

    def post(self, request, pk):
        return _redirect_retired_ownership_module(request)


class PrimaryOwnershipChangeView(FiduciaryManagementRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        return _redirect_retired_ownership_module(request)


class AssignmentListView(FiduciaryReadRequiredMixin, QueryStringMixin, ListView):
    model = FiduciaryAssignment
    template_name = "fiduciary/assignment_list.html"
    context_object_name = "assignments"
    paginate_by = 10

    def get_queryset(self):
        queryset = FiduciaryAssignment.objects.select_related(
            "property_unit", "property_unit__project", "property_unit__structural_group"
        ).prefetch_related(Prefetch("holders", queryset=FiduciaryAssignmentHolder.objects.select_related("client")))
        self.filter_form = AssignmentFilterForm(self.request.GET)
        if self.filter_form.is_valid():
            q = self.filter_form.cleaned_data.get("q")
            project = self.filter_form.cleaned_data.get("project")
            grouping_type = self.filter_form.cleaned_data.get("grouping_type")
            group = self.filter_form.cleaned_data.get("structural_group")
            unit = self.filter_form.cleaned_data.get("property_unit")
            client = self.filter_form.cleaned_data.get("client")
            status = self.filter_form.cleaned_data.get("status")
            start_from = self.filter_form.cleaned_data.get("start_from")
            start_to = self.filter_form.cleaned_data.get("start_to")
            if q:
                queryset = queryset.filter(
                    Q(assignment_number__icontains=q)
                    | Q(property_unit__code__icontains=q)
                    | Q(property_unit__name__icontains=q)
                    | Q(holders__client__document_number__icontains=q)
                    | Q(holders__client__last_names_or_company__icontains=q)
                )
            if project:
                queryset = queryset.filter(property_unit__project=project)
            if grouping_type:
                queryset = queryset.filter(property_unit__structural_group__grouping_type=grouping_type)
            if group:
                queryset = queryset.filter(property_unit__structural_group=group)
            if unit:
                queryset = queryset.filter(property_unit=unit)
            if client:
                queryset = queryset.filter(holders__client=client)
            queryset = queryset.annotate(
                current_holder_count=Count(
                    "holders",
                    filter=Q(holders__is_active=True, holders__end_date__isnull=True),
                    distinct=True,
                )
            )
            if status == "active":
                queryset = queryset.filter(is_active=True, current_holder_count__gt=0)
            elif status == "inactive":
                queryset = queryset.filter(Q(is_active=False) | Q(current_holder_count=0))
            if start_from:
                queryset = queryset.filter(start_date__gte=start_from)
            if start_to:
                queryset = queryset.filter(start_date__lte=start_to)
        return queryset.distinct()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter_form"] = getattr(self, "filter_form", AssignmentFilterForm(self.request.GET))
        for assignment in context["assignments"]:
            assignment.operationally_active = assignment_can_receive_payment(assignment)
        return self.add_common_context(context)


class AssignmentDetailView(FiduciaryReadRequiredMixin, DetailView):
    model = FiduciaryAssignment
    template_name = "fiduciary/assignment_detail.html"
    context_object_name = "assignment"

    def get_queryset(self):
        return FiduciaryAssignment.objects.select_related(
            "property_unit", "property_unit__project", "property_unit__structural_group"
        ).prefetch_related(
            Prefetch("holders", queryset=FiduciaryAssignmentHolder.objects.select_related("client")),
            Prefetch(
                "payments",
                queryset=Payment.objects.select_related("source_file", "source_file__batch").prefetch_related("daily_report_rows").order_by(
                    "exact_date", "period_year", "period_month", "source_row", "pk"
                ),
            ),
            Prefetch(
                "historical_observations",
                queryset=ImportedHistoricalObservation.objects.exclude(origin="historical_novelty").select_related("property_unit").order_by("-created_at", "-pk"),
            ),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payments = list(self.object.payments.all())
        stats = self.object.payments.aggregate(count=Count("id"), total=Sum("amount"))
        novelties = list(
            OperationalNovelty.objects.filter(
                Q(previous_assignment=self.object) | Q(new_assignment=self.object) | Q(historical_assignment=self.object)
            )
            .select_related("property_unit", "previous_client", "new_client", "historical_client", "created_by", "imported_file")
            .order_by("-created_at", "-pk")
        )
        context["can_create"] = can_create_fiduciary(self.request.user)
        context["can_update"] = can_update_fiduciary(self.request.user)
        context["can_manage"] = context["can_update"]
        context["assignment_operationally_active"] = assignment_can_receive_payment(self.object)
        context["payments"] = payments
        context["movements"] = _assignment_movement_rows(payments)
        context["payment_count"] = stats["count"] or 0
        context["payment_total"] = stats["total"] or 0
        context["first_payment"] = payments[0] if payments else None
        context["last_payment"] = payments[-1] if payments else None
        context["financial_entity_form"] = AssignmentFinancialEntityForm(
            initial={"financial_entity": self.object.property_unit.financial_entity or ""}
        )
        context["novelties"] = novelties
        return context


class AssignmentDeleteView(AdministrativeDeleteConfirmView):
    model = FiduciaryAssignment
    success_url = reverse_lazy("fiduciary:assignment_list")
    action_label = "Eliminar encargo fiduciario"
    warning = (
        "Esta accion eliminara el encargo y la informacion dependiente del encargo, "
        "incluyendo titulares del encargo, pagos, novedades y observaciones asociadas. "
        "El cliente no se eliminara."
    )

    def dispatch(self, request, *args, **kwargs):
        raise PermissionDenied("La eliminacion manual directa de encargos fiduciarios no esta permitida.")

    def get_summary(self):
        from .admin_cleanup import assignment_cleanup_summary

        return assignment_cleanup_summary(self.object)

    def perform_delete(self, reason: str):
        from .admin_cleanup import delete_assignment

        delete_assignment(self.object, user=self.request.user, reason=reason)
        return True


class AssignmentFinancialEntityUpdateView(FiduciaryUpdateRequiredMixin, View):
    def post(self, request, pk):
        assignment = get_object_or_404(
            FiduciaryAssignment.objects.select_related("property_unit"),
            pk=pk,
        )
        form = AssignmentFinancialEntityForm(request.POST)
        if not form.is_valid():
            messages.error(request, "No fue posible actualizar la entidad financiera.")
            return redirect("fiduciary:assignment_detail", pk=assignment.pk)
        financial_entity = form.cleaned_data["financial_entity"]
        unit = assignment.property_unit
        if financial_entity and unit.financial_entity != financial_entity:
            before = f"financial_entity: {unit.financial_entity}"
            unit.financial_entity = financial_entity
            unit.save(update_fields=["financial_entity", "updated_at"])
            audit_event(
                user=request.user,
                action="Modificado",
                entity="Unidad inmobiliaria",
                obj=unit,
                description="Entidad financiera actualizada desde el encargo.",
                context={"Proyecto": unit.project, "Unidad": unit, "Encargo": assignment.assignment_number},
                before=before,
                after=f"financial_entity: {unit.financial_entity}",
            )
            messages.success(request, "Entidad financiera actualizada correctamente.")
        elif financial_entity:
            messages.info(request, "La entidad financiera no tuvo cambios.")
        else:
            messages.info(request, "La entidad financiera se conserva sin cambios.")
        return redirect("fiduciary:assignment_detail", pk=assignment.pk)


class AssignmentPaymentCreateView(FiduciaryCreateRequiredMixin, FormView):
    form_class = ManualPaymentForm
    template_name = "fiduciary/payment_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.assignment = get_object_or_404(
            FiduciaryAssignment.objects.select_related(
                "property_unit",
                "property_unit__project",
                "property_unit__structural_group",
            ),
            pk=kwargs["pk"],
        )
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        try:
            payment = _create_manual_payment_for_assignment(self.assignment, form.cleaned_data, self.request.user)
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)
        _audit_manual_payment_created(self.request.user, payment)
        messages.success(self.request, "Pago registrado correctamente.")
        return redirect("fiduciary:assignment_detail", pk=self.assignment.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["assignment"] = self.assignment
        context["title"] = "Registrar pago"
        return context


def _create_manual_payment_for_assignment(assignment: FiduciaryAssignment, data: dict, user) -> Payment:
    if not assignment_can_receive_payment(assignment):
        raise ValidationError("No se puede registrar el pago porque el encargo no tiene titulares vigentes.")
    with transaction.atomic():
        source_file = _manual_payment_source_file(user)
        result = create_payment(
            assignment=assignment,
            amount=data["amount"],
            movement_type=Payment.MovementType.ADDITION,
            source_file=source_file,
            source_sheet="Manual",
            source_row=1,
            date_precision=Payment.DatePrecision.EXACT,
            exact_date=data["exact_date"],
            concept=data["concept"],
            source_header="PAGO MANUAL",
            destination=data["destination"],
        )
        if result.status == "duplicate":
            raise ValidationError("Ya existe un pago con la misma fecha y valor para este encargo.")
        if result.status != "created":
            raise ValidationError(result.errors or ["No fue posible registrar el pago."])
        return result.payment


def _manual_payment_source_file(user) -> ImportedFile:
    existing = ImportedFile.objects.filter(
        file_type=ImportedFile.FileType.REPORT,
        sha256=MANUAL_PAYMENT_SOURCE_SHA,
    ).first()
    if existing:
        return existing
    now = timezone.now()
    batch = ImportBatch.objects.create(
        initiated_by=user,
        imported_by=user,
        import_type=ImportBatch.ImportType.REPORTS,
        load_mode=ImportBatch.LoadMode.SINGLE_FILE,
        status=ImportBatch.Status.COMPLETED,
        imported_at=now,
        total_files=1,
        processed_files=1,
        total_rows=1,
        processed_rows=1,
        summary="Fuente tecnica para pagos registrados manualmente desde Encargos fiduciarios.",
    )
    return ImportedFile.objects.create(
        batch=batch,
        original_name="Pagos manuales",
        extension=".manual",
        size_bytes=0,
        sha256=MANUAL_PAYMENT_SOURCE_SHA,
        file_type=ImportedFile.FileType.REPORT,
        status=ImportedFile.Status.COMPLETED,
        order=1,
        processing_started_at=now,
        processing_finished_at=now,
        total_rows=1,
        processed_rows=1,
        result_message="Pago manual registrado.",
    )


class AssignmentCreateView(FiduciaryCreateRequiredMixin, FormView):
    form_class = NewFiduciaryAssignmentForm
    template_name = "fiduciary/assignment_create.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Nuevo encargo fiduciario"
        context["back_url"] = "fiduciary:assignment_list"
        context["client_search_url"] = reverse("fiduciary:client_search")
        context["client_create_url"] = reverse("fiduciary:client_create")
        form = context.get("form")
        context["requires_date_confirmation"] = bool(getattr(form, "requires_date_confirmation", False))
        primary_id = form.data.get("primary_client_id") if form and form.is_bound else ""
        secondary_ids = (form.data.get("secondary_client_ids") if form and form.is_bound else "") or ""
        selected_ids = [item for item in [primary_id, *secondary_ids.split(",")] if str(item).strip().isdigit()]
        selected_clients = Client.objects.filter(pk__in=selected_ids).order_by("last_names_or_company", "first_names")
        context["selected_clients_json"] = json.dumps(
            {
                str(client.pk): {
                    "id": client.pk,
                    "text": f"{client.full_name} | {client.document_number or 'Sin documento'} | {client.email or 'Sin correo'}",
                }
                for client in selected_clients
            }
        )
        return context

    def form_valid(self, form):
        try:
            with transaction.atomic():
                selected_clients = [form.cleaned_data["primary_client_id"], *form.cleaned_data["secondary_client_ids"]]
                _activate_clients_for_assignment(selected_clients, "Reactivacion por nuevo encargo fiduciario.")
                assignment = _create_assignment_without_novelty(
                    unit=form.cleaned_data["property_unit"],
                    primary_client=form.cleaned_data["primary_client_id"],
                    assignment_number=form.cleaned_data["assignment_number"],
                    secondary_clients=form.cleaned_data["secondary_client_ids"],
                    reason="Registro de nuevo encargo fiduciario.",
                )
                assignment.adhesion_contract_date = form.cleaned_data.get("adhesion_contract_date")
                assignment.promise_date = form.cleaned_data.get("promise_date")
                assignment.promised_delivery_date = form.cleaned_data.get("promised_delivery_date")
                assignment.actual_delivery_date = form.cleaned_data.get("actual_delivery_date")
                assignment.full_clean()
                assignment.save(
                    update_fields=[
                        "adhesion_contract_date",
                        "promise_date",
                        "promised_delivery_date",
                        "actual_delivery_date",
                        "updated_at",
                    ]
                )
                audit_event(
                    user=self.request.user,
                    action="Creado",
                    entity="Encargo fiduciario",
                    obj=assignment,
                    context={
                        "Proyecto": assignment.property_unit.project,
                        "Unidad": assignment.property_unit,
                        "Encargo": assignment.assignment_number,
                        "Cliente principal": form.cleaned_data["primary_client_id"],
                    },
                    after=_audit_snapshot(
                        assignment,
                        ("assignment_number", "start_date", "adhesion_contract_date", "promise_date", "promised_delivery_date", "actual_delivery_date"),
                    ),
                )
        except ValidationError as exc:
            _add_validation_errors_to_form(form, exc)
            return self.form_invalid(form)
        except IntegrityError as exc:
            form.add_error(None, "La operacion no pudo completarse porque viola una regla de negocio vigente.")
            return self.form_invalid(form)
        messages.success(self.request, "Encargo fiduciario creado correctamente.")
        return redirect("fiduciary:assignment_detail", pk=assignment.pk)


class AssignmentContextTypesView(FiduciaryCreateRequiredMixin, View):
    def get(self, request):
        project_id = request.GET.get("project")
        types = GroupingType.objects.none()
        if project_id:
            types = (
                GroupingType.objects.filter(
                    is_active=True,
                    structural_groups__project_id=project_id,
                    structural_groups__is_active=True,
                )
                .distinct()
                .order_by("name")
            )
        return JsonResponse({"results": [{"id": item.pk, "text": item.name} for item in types]})


class AssignmentContextGroupsView(FiduciaryCreateRequiredMixin, View):
    def get(self, request):
        project_id = request.GET.get("project")
        grouping_type_id = request.GET.get("grouping_type")
        results = []
        if project_id:
            results.append({"id": DIRECT_UNITS_VALUE, "text": "Unidades directas del proyecto"})
            groups = StructuralGroup.objects.filter(is_active=True, project_id=project_id).select_related(
                "grouping_type"
            )
            if grouping_type_id:
                groups = groups.filter(grouping_type_id=grouping_type_id)
            results.extend({"id": item.pk, "text": str(item)} for item in groups.order_by("name", "code"))
        return JsonResponse({"results": results})


class AssignmentContextUnitsView(FiduciaryCreateRequiredMixin, View):
    def get(self, request):
        project_id = request.GET.get("project")
        group_id = request.GET.get("structural_group")
        units = PropertyUnit.objects.none()
        if project_id and group_id == DIRECT_UNITS_VALUE:
            units = PropertyUnit.objects.filter(
                is_active=True,
                project_id=project_id,
                structural_group__isnull=True,
            )
        elif project_id and group_id:
            units = PropertyUnit.objects.filter(
                is_active=True,
                project_id=project_id,
                structural_group_id=group_id,
            )
        unit_rows = []
        for item in with_natural_unit_order(units.select_related("project", "structural_group")):
            available = unit_can_receive_new_assignment(item)
            label = property_unit_choice_label(item)
            unit_rows.append(
                {
                    "id": item.pk,
                    "text": label if available else f"{label} (no disponible)",
                    "available": available,
                    "disabled": not available,
                }
            )
        return JsonResponse({"results": unit_rows})


class AssignmentContextHoldersView(FiduciaryCreateRequiredMixin, View):
    def get(self, request):
        unit_id = request.GET.get("unit")
        holders = eligible_assignment_clients(unit_id)
        return JsonResponse({"results": [{"id": item.pk, "text": item.full_name, "label": item.full_name} for item in holders]})


class ClientSearchView(FiduciaryCreateRequiredMixin, View):
    def get(self, request):
        criterion = request.GET.get("criterion")
        query = (request.GET.get("q") or "").strip()
        exclude_ids = {
            int(item)
            for item in request.GET.getlist("exclude")
            if str(item).strip().isdigit()
        }
        clients = []
        if query:
            base = Client.objects.all()
            if exclude_ids:
                base = base.exclude(pk__in=exclude_ids)
            if criterion == "document":
                normalized_query = normalize_document_query(query)
                clients = [
                    client
                    for client in base.order_by("last_names_or_company", "first_names")[:500]
                    if normalized_query in normalize_document_query(client.document_number or "")
                ][:10]
            elif criterion == "email":
                email_query = query.strip().lower()
                clients = list(base.filter(email__icontains=email_query).order_by("last_names_or_company", "first_names")[:10])
            else:
                normalized_tokens = _normalize_search_text(query).split()
                normalized_query = _normalize_search_text(query)
                clients = [
                    client
                    for client in base.order_by("last_names_or_company", "first_names")[:1000]
                    if normalized_query in _normalize_search_text(client.full_name)
                    or all(token in _normalize_search_text(client.full_name) for token in normalized_tokens)
                ][:10]
        results = [
            {
                "id": client.pk,
                "text": f"{client.full_name} | {client.document_number or 'Sin documento'} | {client.email or 'Sin correo'}",
                "is_active": client.is_active,
                "status": "Activo" if client.is_active else "Inactivo",
            }
            for client in clients
        ]
        return JsonResponse({"results": results})


def _assignment_holder_rows(assignment):
    holders = assignment.holders.filter(is_active=True).select_related("client").order_by("-is_primary", "client__last_names_or_company", "client__first_names")
    return [
        {
            "id": holder.client_id,
            "text": f"{holder.client.document_number or 'Sin documento'} | {holder.client.full_name} | {holder.client.email or 'Sin correo'}",
            "role": "primary" if holder.is_primary else "secondary",
            "role_label": "Principal" if holder.is_primary else "Secundario",
        }
        for holder in holders
    ]


def _search_clients_for_picker(*, criterion, query, exclude_ids=None, base_queryset=None):
    exclude_ids = set(exclude_ids or [])
    query = (query or "").strip()
    if not query:
        return []
    base = base_queryset if base_queryset is not None else Client.objects.filter(is_active=True)
    if exclude_ids:
        base = base.exclude(pk__in=exclude_ids)
    if criterion == "document":
        normalized_query = normalize_document_query(query)
        return [
            client
            for client in base.order_by("last_names_or_company", "first_names")[:500]
            if normalized_query in normalize_document_query(client.document_number or "")
        ][:10]
    if criterion == "email":
        return list(base.filter(email__icontains=query.strip().lower()).order_by("last_names_or_company", "first_names")[:10])
    normalized_tokens = _normalize_search_text(query).split()
    normalized_query = _normalize_search_text(query)
    return [
        client
        for client in base.order_by("last_names_or_company", "first_names")[:1000]
        if normalized_query in _normalize_search_text(client.full_name)
        or all(token in _normalize_search_text(client.full_name) for token in normalized_tokens)
    ][:10]


class NoveltyAssignmentClientsView(FiduciaryCreateRequiredMixin, View):
    def get(self, request):
        assignment_id = request.GET.get("assignment")
        assignment = FiduciaryAssignment.objects.filter(pk=assignment_id, is_active=True).prefetch_related("holders__client").first()
        if not assignment:
            return JsonResponse({"results": []})
        return JsonResponse({"results": _assignment_holder_rows(assignment)})


class NoveltyClientSearchView(FiduciaryCreateRequiredMixin, View):
    def get(self, request):
        assignment_id = request.GET.get("assignment")
        criterion = request.GET.get("criterion")
        query = (request.GET.get("q") or "").strip()
        scope = request.GET.get("scope") or "current"
        exclude_ids = {
            int(item)
            for item in request.GET.getlist("exclude")
            if str(item).strip().isdigit()
        }
        assignment = FiduciaryAssignment.objects.filter(pk=assignment_id, is_active=True).prefetch_related("holders__client").first()
        if scope in {"new_holder", "new_secondary"}:
            if scope == "new_secondary" and assignment:
                exclude_ids.update(assignment.holders.filter(is_active=True).values_list("client_id", flat=True))
            clients = _search_clients_for_picker(criterion=criterion, query=query, exclude_ids=exclude_ids)
            results = [
                {
                    "id": client.pk,
                    "text": f"{client.full_name} | {client.document_number or 'Sin documento'} | {client.email or 'Sin correo'}",
                    "is_active": client.is_active,
                    "status": "Activo" if client.is_active else "Inactivo",
                }
                for client in clients
            ]
            return JsonResponse({"results": results})
        if not assignment or not query:
            return JsonResponse({"results": []})
        rows = []
        normalized_query = _normalize_search_text(query)
        normalized_document = normalize_document_query(query)
        for row in _assignment_holder_rows(assignment):
            if row["id"] in exclude_ids:
                continue
            client_text = _normalize_search_text(row["text"])
            client = Client.objects.get(pk=row["id"])
            if criterion == "document":
                matches = normalized_document in normalize_document_query(client.document_number or "")
            elif criterion == "email":
                matches = query.lower() in (client.email or "").lower()
            else:
                tokens = normalized_query.split()
                matches = normalized_query in client_text or all(token in client_text for token in tokens)
            if matches:
                rows.append({**row, "status": row["role_label"]})
            if len(rows) >= 10:
                break
        return JsonResponse({"results": rows})


class AssignmentUpdateView(FiduciaryManagementRequiredMixin, UpdateView):
    model = FiduciaryAssignment
    form_class = FiduciaryAssignmentUpdateForm
    template_name = "fiduciary/form.html"
    success_url = reverse_lazy("fiduciary:assignment_list")

    def dispatch(self, request, *args, **kwargs):
        raise PermissionDenied("La edicion manual directa de encargos fiduciarios no esta permitida.")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Actualizar informacion contractual"
        context["back_url"] = "fiduciary:assignment_list"
        return context

    def form_valid(self, form):
        try:
            before = _audit_snapshot(
                self.object,
                ("assignment_number", "adhesion_contract_date", "promise_date", "promised_delivery_date", "actual_delivery_date", "observations"),
            )
            self.object = save_form_object_safely(form)
            audit_event(
                user=self.request.user,
                action="Modificado",
                entity="Encargo fiduciario",
                obj=self.object,
                context={
                    "Proyecto": self.object.property_unit.project,
                    "Unidad": self.object.property_unit,
                    "Encargo": self.object.assignment_number,
                },
                before=before,
                after=_audit_snapshot(
                    self.object,
                    ("assignment_number", "adhesion_contract_date", "promise_date", "promised_delivery_date", "actual_delivery_date", "observations"),
                ),
            )
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)
        messages.success(self.request, "Informacion contractual actualizada correctamente.")
        return redirect("fiduciary:assignment_detail", pk=self.object.pk)


class AssignmentCloseView(FiduciaryManagementRequiredMixin, View):
    def post(self, request, pk):
        assignment = get_object_or_404(FiduciaryAssignment, pk=pk)
        form = StatusReasonForm(request.POST)
        if not form.is_valid() or not form.cleaned_data.get("end_date"):
            messages.error(request, "Debe registrar motivo y fecha de cierre.")
            return redirect("fiduciary:assignment_detail", pk=assignment.pk)
        with transaction.atomic():
            before = _audit_snapshot(assignment, ("is_active", "end_date", "last_change_reason"))
            assignment.is_active = False
            assignment.end_date = form.cleaned_data["end_date"]
            assignment.last_change_reason = form.cleaned_data["change_reason"]
            assignment.save()
            assignment.holders.filter(is_active=True).update(
                is_active=False,
                end_date=assignment.end_date,
                last_change_reason=assignment.last_change_reason,
            )
            audit_event(
                user=request.user,
                action="Modificado",
                entity="Encargo fiduciario",
                obj=assignment,
                description="Encargo cerrado.",
                context={
                    "Proyecto": assignment.property_unit.project,
                    "Unidad": assignment.property_unit,
                    "Encargo": assignment.assignment_number,
                    "Motivo": assignment.last_change_reason,
                },
                before=before,
                after=_audit_snapshot(assignment, ("is_active", "end_date", "last_change_reason")),
            )
        messages.success(request, "Encargo fiduciario cerrado correctamente.")
        return redirect("fiduciary:assignment_detail", pk=assignment.pk)


class AssignmentChangeView(FiduciaryManagementRequiredMixin, FormView):
    form_class = AssignmentChangeForm
    template_name = "fiduciary/form.html"

    def dispatch(self, request, *args, **kwargs):
        self.assignment = get_object_or_404(
            FiduciaryAssignment.objects.select_related("property_unit"),
            pk=kwargs["pk"],
            is_active=True,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["assignment"] = self.assignment
        return kwargs

    def form_valid(self, form):
        try:
            result = change_assignment(
                current_assignment=self.assignment,
                new_assignment_number=form.cleaned_data["new_assignment_number"],
                effective_date=form.cleaned_data["effective_date"],
                novelty_type=form.cleaned_data["novelty_type"],
                reason=form.cleaned_data["reason"],
                primary_client=form.cleaned_data.get("primary_client"),
                secondary_clients=form.cleaned_data.get("secondary_clients"),
                other_description=form.cleaned_data.get("other_description", ""),
            )
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)
        audit_event(
            user=self.request.user,
            action="Creado",
            entity="Cambio de encargo",
            obj=result.new_assignment,
            context={
                "Proyecto": result.new_assignment.property_unit.project,
                "Unidad": result.new_assignment.property_unit,
                "Encargo anterior": self.assignment.assignment_number,
                "Encargo nuevo": result.new_assignment.assignment_number,
                "Motivo": form.cleaned_data["reason"],
            },
        )
        messages.success(self.request, "Cambio de encargo registrado correctamente.")
        return redirect("fiduciary:assignment_detail", pk=result.new_assignment.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Registrar cambio de encargo"
        context["back_url"] = "fiduciary:assignment_list"
        return context


class AssignmentSecondaryCreateView(FiduciaryCreateRequiredMixin, FormView):
    form_class = AddSecondaryAssignmentHolderForm
    template_name = "fiduciary/assignment_add_secondary.html"

    def dispatch(self, request, *args, **kwargs):
        self.assignment = get_object_or_404(
            FiduciaryAssignment.objects.select_related("property_unit", "property_unit__project"),
            pk=kwargs["assignment_pk"],
            is_active=True,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["assignment"] = self.assignment
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["assignment"] = self.assignment
        context["title"] = "Añadir cliente secundario"
        context["back_url"] = "fiduciary:assignment_detail"
        context["client_search_url"] = reverse("fiduciary:client_search")
        context["client_create_url"] = reverse("fiduciary:client_create")
        return context

    def form_valid(self, form):
        client = form.cleaned_data["client_id"]
        reason = f"INCLUSION {client.full_name}"
        effective_date = timezone.localdate()
        try:
            with transaction.atomic():
                _activate_clients_for_assignment([client], "Reactivacion por inclusion como cliente secundario.")
                unit = PropertyUnit.objects.select_for_update().get(pk=self.assignment.property_unit_id)
                ownership, _ = UnitOwnership.objects.get_or_create(
                    client=client,
                    property_unit=unit,
                    is_active=True,
                    defaults={
                        "is_primary": False,
                        "start_date": effective_date,
                        "last_change_reason": reason,
                    },
                )
                if ownership.is_primary:
                    raise ValidationError("Un titular principal vigente no puede agregarse como secundario.")
                FiduciaryAssignmentHolder.objects.create(
                    assignment=self.assignment,
                    client=client,
                    is_primary=False,
                    start_date=effective_date,
                    last_change_reason=reason,
                )
                novelty = OperationalNovelty(
                    project=unit.project,
                    property_unit=unit,
                    novelty_type=OperationalNovelty.NoveltyType.OTHER,
                    other_type="INCLUSION",
                    origin=OperationalNovelty.Origin.MANUAL,
                    status=OperationalNovelty.Status.APPLIED,
                    effective_date=effective_date,
                    new_client=client,
                    new_assignment=self.assignment,
                    historical_assignment=self.assignment,
                    summary="INCLUSION",
                    detail=reason,
                    created_by=self.request.user,
                )
                novelty.full_clean()
                novelty.save()
                observation = ImportedHistoricalObservation(
                    project=unit.project,
                    property_unit=unit,
                    client=client,
                    assignment=self.assignment,
                    origin=ImportedHistoricalObservation.Origin.MANUAL,
                    status=ImportedHistoricalObservation.Status.IMPORTED,
                    summary="INCLUSION",
                    detail=reason,
                    dedupe_key=uuid.uuid4().hex,
                    imported_by=self.request.user,
                )
                observation.full_clean()
                observation.save()
                audit_event(
                    user=self.request.user,
                    action="Creado",
                    entity="Cliente secundario de encargo",
                    obj=self.assignment,
                    context={
                        "Proyecto": unit.project,
                        "Unidad": unit,
                        "Encargo": self.assignment.assignment_number,
                        "Cliente": client,
                    },
                    description="Cliente secundario añadido con novedad de inclusion.",
                )
        except ValidationError as exc:
            form.add_error(None, _validation_error_text(exc))
            return self.form_invalid(form)
        except IntegrityError:
            form.add_error(None, "La operacion no pudo completarse porque viola una regla de negocio vigente.")
            return self.form_invalid(form)
        messages.success(self.request, "Cliente secundario añadido con novedad de INCLUSION.")
        return redirect("fiduciary:assignment_detail", pk=self.assignment.pk)


class AssignmentHolderCreateView(FiduciaryCreateRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        assignment = get_object_or_404(FiduciaryAssignment, pk=kwargs["assignment_pk"])
        messages.info(request, "Use la accion Añadir cliente secundario desde el encargo.")
        return redirect("fiduciary:assignment_secondary_create", assignment_pk=assignment.pk)


class AssignmentHolderFinalizeView(FiduciaryManagementRequiredMixin, View):
    def post(self, request, pk):
        holder = get_object_or_404(FiduciaryAssignmentHolder, pk=pk)
        form = StatusReasonForm(request.POST)
        if not form.is_valid() or not form.cleaned_data.get("end_date"):
            messages.error(request, "Debe registrar motivo y fecha de finalizacion.")
            return redirect("fiduciary:assignment_detail", pk=holder.assignment_id)
        if holder.assignment.is_active and holder.is_primary:
            messages.error(request, "No puede finalizar el titular principal mientras el encargo siga vigente.")
            return redirect("fiduciary:assignment_detail", pk=holder.assignment_id)
        with transaction.atomic():
            before = _audit_snapshot(holder, ("is_active", "end_date", "last_change_reason"))
            holder.is_active = False
            holder.end_date = form.cleaned_data["end_date"]
            holder.last_change_reason = form.cleaned_data["change_reason"]
            holder.save()
            audit_event(
                user=request.user,
                action="Modificado",
                entity="Titular de encargo",
                obj=holder,
                description="Titular finalizado.",
                context={"Cliente": holder.client, "Encargo": holder.assignment.assignment_number, "Motivo": holder.last_change_reason},
                before=before,
                after=_audit_snapshot(holder, ("is_active", "end_date", "last_change_reason")),
            )
        messages.success(request, "Titular finalizado correctamente.")
        return redirect("fiduciary:assignment_detail", pk=holder.assignment_id)
