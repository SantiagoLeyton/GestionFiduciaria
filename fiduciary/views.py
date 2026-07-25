from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View

from real_estate.models import GroupingType, PropertyUnit, StructuralGroup

from .forms import (
    AssignmentFilterForm,
    AssignmentHolderForm,
    ClientFilterForm,
    ClientForm,
    ClientUpdateForm,
    DIRECT_UNITS_VALUE,
    FiduciaryAssignmentForm,
    FiduciaryAssignmentUpdateForm,
    SecondaryAssignmentHolderFormSet,
    StatusReasonForm,
    UnitOwnershipForm,
    eligible_assignment_clients,
    validate_assignment_holder_formset,
)
from .models import Client, FiduciaryAssignment, FiduciaryAssignmentHolder, UnitOwnership
from .permissions import FiduciaryManagementRequiredMixin, FiduciaryReadRequiredMixin, can_manage_fiduciary


class QueryStringMixin:
    def add_common_context(self, context):
        query_params = self.request.GET.copy()
        query_params.pop("page", None)
        context["page_querystring"] = query_params.urlencode()
        context["can_manage"] = can_manage_fiduciary(self.request.user)
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
        context["can_manage"] = can_manage_fiduciary(self.request.user)
        return context


class ClientCreateView(FiduciaryManagementRequiredMixin, CreateView):
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


class UnitOwnershipCreateView(FiduciaryManagementRequiredMixin, CreateView):
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
        context["can_manage"] = can_manage_fiduciary(self.request.user)
        return context


class AssignmentCreateView(FiduciaryManagementRequiredMixin, CreateView):
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


class AssignmentContextTypesView(FiduciaryManagementRequiredMixin, View):
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


class AssignmentContextGroupsView(FiduciaryManagementRequiredMixin, View):
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


class AssignmentContextUnitsView(FiduciaryManagementRequiredMixin, View):
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


class AssignmentContextHoldersView(FiduciaryManagementRequiredMixin, View):
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
