import json
import tempfile
from pathlib import Path

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, FormView, ListView, UpdateView, View

from real_estate.models import GroupingType, PropertyUnit, StructuralGroup

from .forms import (
    AssignmentFilterForm,
    AssignmentHolderForm,
    ClientFilterForm,
    ClientForm,
    ClientUpdateForm,
    DailyReportAssignmentResolutionForm,
    DailyReportUploadForm,
    DIRECT_UNITS_VALUE,
    FiduciaryAssignmentForm,
    FiduciaryAssignmentUpdateForm,
    HistoricalImportUploadForm,
    ImportResolutionForm,
    StructuralGroupResolutionForm,
    SecondaryAssignmentHolderFormSet,
    StatusReasonForm,
    UnitOwnershipForm,
    eligible_assignment_clients,
    validate_assignment_holder_formset,
)
from .imports.historical import (
    DuplicateHistoricalImportError,
    analyze_historical_import,
    finalize_historical_import,
    find_existing_historical_import,
    store_historical_import_file,
)
from .imports.cancellation import CANCELABLE_BATCH_STATUSES, cancel_import_batch
from .imports.daily import (
    DailyReportDuplicateError,
    analyze_daily_report_import,
    finalize_daily_report_import,
    reanalyze_daily_report_import,
    resolve_daily_report_assignment,
)
from .imports.historical.resolutions import (
    apply_resolution_to_equivalent_elements,
    auto_resolve_new_units,
    reanalyze_pending_resolutions,
    resolve_structural_group,
    update_batch_resolution_state,
)
from .models import (
    Client,
    DailyReportRow,
    DetectedStructureElement,
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportBatch,
    ImportRowIssue,
    ImportResolution,
    UnitOwnership,
)
from .permissions import (
    FiduciaryCreateRequiredMixin,
    FiduciaryImportRequiredMixin,
    FiduciaryManagementRequiredMixin,
    FiduciaryReadRequiredMixin,
    can_create_fiduciary,
    can_import_fiduciary,
    can_update_fiduciary,
)


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
        uploaded_file = form.cleaned_data["file"]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / uploaded_file.name
            with path.open("wb") as target:
                for chunk in uploaded_file.chunks():
                    target.write(chunk)
            existing_file = find_existing_historical_import(path)
            if existing_file:
                _add_duplicate_historical_import_message(self.request, existing_file)
                return redirect("fiduciary:historical_import_preview", pk=existing_file.batch_id)
            batch = ImportBatch.objects.create(
                initiated_by=self.request.user,
                import_type=ImportBatch.ImportType.HISTORICAL,
                load_mode=ImportBatch.LoadMode.SINGLE_FILE,
                status=ImportBatch.Status.ANALYZING,
                total_files=1,
            )
            try:
                analysis_result = analyze_historical_import(
                    batch=batch,
                    file_path=path,
                    grouping_type_hint=form.cleaned_data.get("grouping_type_hint"),
                )
                store_historical_import_file(imported_file=analysis_result.imported_file, source_path=path)
                auto_resolve_new_units(batch, user=self.request.user)
                update_batch_resolution_state(batch)
                messages.success(self.request, "Archivo historico analizado correctamente.")
            except DuplicateHistoricalImportError as exc:
                batch.delete()
                _add_duplicate_historical_import_message(self.request, exc.imported_file)
                return redirect("fiduciary:historical_import_preview", pk=exc.imported_file.batch_id)
            except Exception:
                batch.status = ImportBatch.Status.FAILED
                batch.summary = "No fue posible analizar el archivo historico cargado."
                batch.save(update_fields=["status", "summary"])
                messages.error(self.request, "No fue posible analizar el archivo historico. Revise el archivo e intente nuevamente.")
        return redirect("fiduciary:historical_import_preview", pk=batch.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Importar libro historico"
        context["back_url"] = "fiduciary:historical_import_list"
        return context


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
        context["summary"] = _load_import_summary(batch.summary or (imported_file.result_message if imported_file else ""))
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
        context["can_finalize"] = can_import_fiduciary(self.request.user) and batch.status == ImportBatch.Status.READY
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
            result = finalize_historical_import(batch_id=self.object.pk, user=request.user)
        except PermissionDenied:
            raise
        except Exception as exc:
            messages.error(request, str(exc))
            return redirect("fiduciary:historical_import_preview", pk=self.object.pk)
        messages.success(
            request,
            (
                "Importacion historica definitiva completada. "
                f"Unidades creadas: {result.created_property_units}. "
                f"Clientes creados: {result.created_clients}. "
                f"Pagos creados: {result.created_payments}."
            ),
        )
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
        context["can_finalize"] = batch.status == ImportBatch.Status.READY
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
            "El intento de importación fue cancelado y sus resultados temporales fueron eliminados.",
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
        ).select_related("resolution").order_by("inferred_kind", "normalized_value")

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
        return context


class HistoricalImportReanalyzePendingView(FiduciaryImportRequiredMixin, View):
    def post(self, request, pk):
        batch = get_object_or_404(historical_batches(), pk=pk)
        updated = reanalyze_pending_resolutions(batch, user=request.user)
        messages.success(request, f"Se volvieron a analizar los pendientes. Elementos actualizados: {updated}.")
        return redirect("fiduciary:historical_import_pending", pk=batch.pk)


class HistoricalImportResolutionView(FiduciaryImportRequiredMixin, FormView):
    form_class = ImportResolutionForm
    template_name = "fiduciary/import_resolution_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.batch = get_object_or_404(historical_batches(), pk=kwargs["pk"])
        self.element = get_object_or_404(
            self.batch.detected_elements.select_related("resolution"),
            pk=kwargs["element_pk"],
        )
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
        resolution.save()
        apply_resolution_to_equivalent_elements(resolution, self.request.user)
        reanalyze_pending_resolutions(self.batch, user=self.request.user)
        self.batch.refresh_from_db()
        if self.batch.status == ImportBatch.Status.READY:
            messages.success(self.request, "Todas las resoluciones estan completas. El lote queda listo para la siguiente fase.")
            return redirect("fiduciary:historical_import_preview", pk=self.batch.pk)
        messages.success(self.request, "Resolucion aplicada a las apariciones equivalentes.")
        return redirect("fiduciary:historical_import_pending", pk=self.batch.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["batch"] = self.batch
        context["element"] = self.element
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
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["detected_element"] = self.element
        return kwargs

    def form_valid(self, form):
        try:
            updated_units = resolve_structural_group(
                resolution=self.element.resolution,
                action=form.cleaned_data["action"],
                project=form.cleaned_data["project"],
                grouping_type=form.cleaned_data["grouping_type"],
                existing_group=form.cleaned_data.get("existing_group"),
                new_group_name=form.cleaned_data.get("new_group_name"),
                resolved_by=self.request.user,
            )
        except ValueError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(
            self.request,
            f"La agrupación fue resuelta y se actualizaron automáticamente {updated_units} unidades relacionadas.",
        )
        return redirect("fiduciary:historical_import_preview", pk=self.batch.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["batch"] = self.batch
        context["element"] = self.element
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
        uploaded_file = form.cleaned_data["file"]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / uploaded_file.name
            with path.open("wb") as target:
                for chunk in uploaded_file.chunks():
                    target.write(chunk)
            batch = ImportBatch.objects.create(
                initiated_by=self.request.user,
                import_type=ImportBatch.ImportType.REPORTS,
                load_mode=ImportBatch.LoadMode.SINGLE_FILE,
                status=ImportBatch.Status.ANALYZING,
                total_files=1,
            )
            try:
                analyze_daily_report_import(batch=batch, file_path=path)
                messages.success(self.request, "Reporte diario analizado correctamente.")
            except DailyReportDuplicateError as exc:
                batch.delete()
                messages.warning(self.request, "Este reporte diario ya fue cargado anteriormente.")
                return redirect("fiduciary:daily_report_preview", pk=exc.imported_file.batch_id)
            except Exception:
                batch.status = ImportBatch.Status.FAILED
                batch.summary = "No fue posible analizar el reporte diario cargado."
                batch.save(update_fields=["status", "summary"])
                messages.error(self.request, "No fue posible analizar el reporte diario. Revise el archivo e intente nuevamente.")
        return redirect("fiduciary:daily_report_preview", pk=batch.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Importar reporte diario"
        context["back_url"] = "fiduciary:daily_report_list"
        return context


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
        resolve_daily_report_assignment(
            row=self.row,
            assignment=form.cleaned_data.get("assignment"),
            user=self.request.user,
            note=form.cleaned_data.get("resolution_note", ""),
        )
        messages.success(self.request, "Resolucion del encargo aplicada.")
        return redirect("fiduciary:daily_report_preview", pk=self.batch.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["batch"] = self.batch
        context["row"] = self.row
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
            document_type = self.filter_form.cleaned_data.get("document_type")
            information_status = self.filter_form.cleaned_data.get("information_status")
            status = self.filter_form.cleaned_data.get("status")
            project = self.filter_form.cleaned_data.get("project")
            property_unit = self.filter_form.cleaned_data.get("property_unit")
            if q:
                queryset = queryset.filter(
                    Q(first_names__icontains=q)
                    | Q(last_names_or_company__icontains=q)
                    | Q(document_number__icontains=q)
                    | Q(phone__icontains=q)
                    | Q(email__icontains=q)
                )
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
                queryset=UnitOwnership.objects.select_related("property_unit", "property_unit__project").order_by("-is_active", "-start_date"),
            ),
            Prefetch(
                "fiduciary_assignment_holders",
                queryset=FiduciaryAssignmentHolder.objects.select_related(
                    "assignment", "assignment__property_unit", "assignment__property_unit__project"
                ).order_by("-is_active", "-start_date"),
            ),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_create"] = can_create_fiduciary(self.request.user)
        context["can_update"] = can_update_fiduciary(self.request.user)
        context["can_manage"] = context["can_update"]
        return context


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
            self.object = form.save()
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
            client.is_active = action == "activate"
            client.last_change_reason = form.cleaned_data["change_reason"]
            client.save(update_fields=["is_active", "last_change_reason", "updated_at"])
        messages.success(request, "Cliente actualizado correctamente.")
        return redirect("fiduciary:client_list")


class UnitOwnershipListView(FiduciaryReadRequiredMixin, QueryStringMixin, ListView):
    model = UnitOwnership
    template_name = "fiduciary/ownership_list.html"
    context_object_name = "ownerships"
    paginate_by = 10

    def get_queryset(self):
        return UnitOwnership.objects.select_related("client", "property_unit", "property_unit__project")

    def get_context_data(self, **kwargs):
        return self.add_common_context(super().get_context_data(**kwargs))


class UnitOwnershipCreateView(FiduciaryCreateRequiredMixin, CreateView):
    model = UnitOwnership
    form_class = UnitOwnershipForm
    template_name = "fiduciary/form.html"
    success_url = reverse_lazy("fiduciary:ownership_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Nueva titularidad"
        context["back_url"] = "fiduciary:ownership_list"
        return context

    def form_valid(self, form):
        with transaction.atomic():
            self.object = form.save()
        messages.success(self.request, "Titularidad creada correctamente.")
        return redirect(self.success_url)


class UnitOwnershipFinalizeView(FiduciaryManagementRequiredMixin, View):
    def post(self, request, pk):
        ownership = get_object_or_404(UnitOwnership, pk=pk)
        form = StatusReasonForm(request.POST)
        if not form.is_valid() or not form.cleaned_data.get("end_date"):
            messages.error(request, "Debe registrar motivo y fecha de finalizacion.")
            return redirect("fiduciary:ownership_list")
        with transaction.atomic():
            ownership.is_active = False
            ownership.end_date = form.cleaned_data["end_date"]
            ownership.last_change_reason = form.cleaned_data["change_reason"]
            ownership.save()
        messages.success(request, "Titularidad finalizada correctamente.")
        return redirect("fiduciary:ownership_list")


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
            if status == "active":
                queryset = queryset.filter(is_active=True)
            elif status == "inactive":
                queryset = queryset.filter(is_active=False)
            if start_from:
                queryset = queryset.filter(start_date__gte=start_from)
            if start_to:
                queryset = queryset.filter(start_date__lte=start_to)
        return queryset.distinct()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter_form"] = getattr(self, "filter_form", AssignmentFilterForm(self.request.GET))
        return self.add_common_context(context)


class AssignmentDetailView(FiduciaryReadRequiredMixin, DetailView):
    model = FiduciaryAssignment
    template_name = "fiduciary/assignment_detail.html"
    context_object_name = "assignment"

    def get_queryset(self):
        return FiduciaryAssignment.objects.select_related(
            "property_unit", "property_unit__project", "property_unit__structural_group"
        ).prefetch_related(Prefetch("holders", queryset=FiduciaryAssignmentHolder.objects.select_related("client")))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_create"] = can_create_fiduciary(self.request.user)
        context["can_update"] = can_update_fiduciary(self.request.user)
        context["can_manage"] = context["can_update"]
        return context


class AssignmentCreateView(FiduciaryCreateRequiredMixin, CreateView):
    model = FiduciaryAssignment
    form_class = FiduciaryAssignmentForm
    template_name = "fiduciary/assignment_form.html"
    success_url = reverse_lazy("fiduciary:assignment_list")

    def get_holder_formset(self, unit_id=None):
        eligible_clients = eligible_assignment_clients(unit_id)
        if self.request.method == "POST":
            return SecondaryAssignmentHolderFormSet(
                self.request.POST,
                prefix="holders",
                form_kwargs={"eligible_clients": eligible_clients},
            )
        return SecondaryAssignmentHolderFormSet(prefix="holders", form_kwargs={"eligible_clients": eligible_clients})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Nuevo encargo fiduciario"
        context["back_url"] = "fiduciary:assignment_list"
        unit_id = self.request.POST.get("property_unit") if self.request.method == "POST" else None
        form = context.get("form")
        if form and form.is_bound and form.data.get("property_unit"):
            unit_id = form.data.get("property_unit")
        context["holder_formset"] = context.get("holder_formset") or self.get_holder_formset(unit_id)
        context["temporary_assignment_notice"] = (
            "Herramienta temporal de validacion. En el flujo definitivo los encargos fiduciarios "
            "seran registrados mediante importacion de archivos."
        )
        context["direct_units_value"] = DIRECT_UNITS_VALUE
        return context

    def form_valid(self, form):
        holder_formset = self.get_holder_formset(form.cleaned_data.get("property_unit").pk)
        if not holder_formset.is_valid():
            return self.form_invalid(form, holder_formset)
        try:
            secondary_clients = validate_assignment_holder_formset(
                holder_formset,
                form.cleaned_data.get("property_unit"),
                form.cleaned_data.get("primary_client"),
            )
        except Exception as exc:
            holder_formset.non_form_errors_value = str(exc)
            form.add_error(None, exc)
            return self.form_invalid(form, holder_formset)
        with transaction.atomic():
            self.object = form.save(commit=False)
            form.apply_reason(self.object)
            self.object.save()
            FiduciaryAssignmentHolder.objects.create(
                assignment=self.object,
                client=form.cleaned_data["primary_client"],
                is_primary=True,
                start_date=self.object.start_date,
                last_change_reason=self.object.last_change_reason,
            )
            for client in secondary_clients:
                FiduciaryAssignmentHolder.objects.create(
                    assignment=self.object,
                    client=client,
                    is_primary=False,
                    start_date=self.object.start_date,
                    last_change_reason=self.object.last_change_reason,
                )
        messages.success(self.request, "Encargo fiduciario creado correctamente.")
        return redirect(self.success_url)

    def form_invalid(self, form, holder_formset=None):
        if holder_formset is None:
            holder_formset = self.get_holder_formset(self.request.POST.get("property_unit"))
        messages.error(self.request, "No fue posible crear el encargo. Revise los titulares seleccionados.")
        return self.render_to_response(self.get_context_data(form=form, holder_formset=holder_formset))


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
        return JsonResponse({"results": [{"id": item.pk, "text": str(item)} for item in units.order_by("name", "code")]})


class AssignmentContextHoldersView(FiduciaryCreateRequiredMixin, View):
    def get(self, request):
        unit_id = request.GET.get("unit")
        holders = eligible_assignment_clients(unit_id)
        return JsonResponse({"results": [{"id": item.pk, "text": item.full_name, "label": item.full_name} for item in holders]})


class AssignmentUpdateView(FiduciaryManagementRequiredMixin, UpdateView):
    model = FiduciaryAssignment
    form_class = FiduciaryAssignmentUpdateForm
    template_name = "fiduciary/form.html"
    success_url = reverse_lazy("fiduciary:assignment_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Editar encargo fiduciario"
        context["back_url"] = "fiduciary:assignment_list"
        return context

    def form_valid(self, form):
        with transaction.atomic():
            self.object = form.save()
        messages.success(self.request, "Encargo fiduciario actualizado correctamente.")
        return redirect(self.success_url)


class AssignmentCloseView(FiduciaryManagementRequiredMixin, View):
    def post(self, request, pk):
        assignment = get_object_or_404(FiduciaryAssignment, pk=pk)
        form = StatusReasonForm(request.POST)
        if not form.is_valid() or not form.cleaned_data.get("end_date"):
            messages.error(request, "Debe registrar motivo y fecha de cierre.")
            return redirect("fiduciary:assignment_detail", pk=assignment.pk)
        with transaction.atomic():
            assignment.is_active = False
            assignment.end_date = form.cleaned_data["end_date"]
            assignment.last_change_reason = form.cleaned_data["change_reason"]
            assignment.save()
            assignment.holders.filter(is_active=True).update(
                is_active=False,
                end_date=assignment.end_date,
                last_change_reason=assignment.last_change_reason,
            )
        messages.success(request, "Encargo fiduciario cerrado correctamente.")
        return redirect("fiduciary:assignment_detail", pk=assignment.pk)


class AssignmentHolderCreateView(FiduciaryManagementRequiredMixin, CreateView):
    model = FiduciaryAssignmentHolder
    form_class = AssignmentHolderForm
    template_name = "fiduciary/form.html"

    def dispatch(self, request, *args, **kwargs):
        self.assignment = get_object_or_404(FiduciaryAssignment, pk=kwargs["assignment_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["assignment"] = self.assignment
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Agregar titular al encargo"
        context["back_url"] = "fiduciary:assignment_list"
        return context

    def form_valid(self, form):
        with transaction.atomic():
            self.object = form.save()
        messages.success(self.request, "Titular agregado correctamente.")
        return redirect("fiduciary:assignment_detail", pk=self.assignment.pk)


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
            holder.is_active = False
            holder.end_date = form.cleaned_data["end_date"]
            holder.last_change_reason = form.cleaned_data["change_reason"]
            holder.save()
        messages.success(request, "Titular finalizado correctamente.")
        return redirect("fiduciary:assignment_detail", pk=holder.assignment_id)
