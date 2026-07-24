from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, ListView, UpdateView, View

from .forms import (
    GroupingTypeForm,
    GroupingTypeUpdateForm,
    ProjectForm,
    ProjectUpdateForm,
    PropertyUnitForm,
    PropertyUnitUpdateForm,
    SearchForm,
    StructuralGroupForm,
    StructuralGroupUpdateForm,
)
from .models import GroupingType, Project, PropertyUnit, StructuralGroup
from .permissions import RealEstateManagementRequiredMixin, RealEstateReadRequiredMixin, can_manage_real_estate


class EntityListView(RealEstateReadRequiredMixin, ListView):
    paginate_by = 10
    search_fields = ("code", "name")
    status_field = "is_active"

    def get_queryset(self):
        queryset = super().get_queryset()
        self.search_form = SearchForm(self.request.GET)
        if self.search_form.is_valid():
            query = self.search_form.cleaned_data.get("q")
            status = self.search_form.cleaned_data.get("status")
            if query:
                filters = Q()
                for field in self.search_fields:
                    filters |= Q(**{f"{field}__icontains": query})
                queryset = queryset.filter(filters)
            if status == "active":
                queryset = queryset.filter(is_active=True)
            elif status == "inactive":
                queryset = queryset.filter(is_active=False)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_form"] = getattr(self, "search_form", SearchForm(self.request.GET))
        context["can_manage"] = can_manage_real_estate(self.request.user)
        context["create_url_name"] = self.create_url_name
        context["update_url_name"] = self.update_url_name
        context["status_url_name"] = self.status_url_name
        context["entity_label"] = self.entity_label
        context["entity_label_plural"] = self.entity_label_plural
        return context


class EntityCreateView(RealEstateManagementRequiredMixin, CreateView):
    template_name = "real_estate/entity_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entity_label"] = self.entity_label
        context["list_url_name"] = self.list_url_name
        return context

    def form_valid(self, form):
        with transaction.atomic():
            self.object = form.save()
        messages.success(self.request, f"{self.entity_label} creado correctamente.")
        return redirect(self.success_url)

    def form_invalid(self, form):
        messages.error(self.request, "No fue posible guardar la informacion. Revise los datos ingresados.")
        return super().form_invalid(form)


class EntityUpdateView(RealEstateManagementRequiredMixin, UpdateView):
    template_name = "real_estate/entity_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entity_label"] = self.entity_label
        context["list_url_name"] = self.list_url_name
        return context

    def form_valid(self, form):
        with transaction.atomic():
            self.object = form.save()
        messages.success(self.request, f"{self.entity_label} actualizado correctamente.")
        return redirect(self.success_url)

    def form_invalid(self, form):
        messages.error(self.request, "No fue posible actualizar la informacion. Revise los datos ingresados.")
        return super().form_invalid(form)


class EntityStatusView(RealEstateManagementRequiredMixin, View):
    model = None
    list_url_name = None

    def post(self, request, pk, action):
        if action not in {"activate", "deactivate"}:
            raise Http404("Accion no disponible.")
        reason = request.POST.get("change_reason", "").strip()
        if not reason:
            messages.error(request, "Debe registrar el motivo de la modificacion.")
            return redirect(self.list_url_name)
        instance = get_object_or_404(self.model, pk=pk)
        instance.is_active = action == "activate"
        instance.last_change_reason = reason
        instance.save(update_fields=["is_active", "last_change_reason", "updated_at"])
        message = "Registro activado correctamente." if instance.is_active else "Registro inactivado correctamente."
        messages.success(request, message)
        return redirect(self.list_url_name)


class ProjectListView(EntityListView):
    model = Project
    template_name = "real_estate/project_list.html"
    context_object_name = "projects"
    create_url_name = "real_estate:project_create"
    update_url_name = "real_estate:project_update"
    status_url_name = "real_estate:project_status"
    entity_label = "Proyecto"
    entity_label_plural = "Proyectos"


class ProjectCreateView(EntityCreateView):
    model = Project
    form_class = ProjectForm
    success_url = reverse_lazy("real_estate:project_list")
    list_url_name = "real_estate:project_list"
    entity_label = "Proyecto"


class ProjectUpdateView(EntityUpdateView):
    model = Project
    form_class = ProjectUpdateForm
    success_url = reverse_lazy("real_estate:project_list")
    list_url_name = "real_estate:project_list"
    entity_label = "Proyecto"


class ProjectStatusView(EntityStatusView):
    model = Project
    list_url_name = "real_estate:project_list"


class GroupingTypeListView(EntityListView):
    model = GroupingType
    template_name = "real_estate/grouping_type_list.html"
    context_object_name = "grouping_types"
    create_url_name = "real_estate:grouping_type_create"
    update_url_name = "real_estate:grouping_type_update"
    status_url_name = "real_estate:grouping_type_status"
    entity_label = "Tipo de agrupacion"
    entity_label_plural = "Tipos de agrupacion"


class GroupingTypeCreateView(EntityCreateView):
    model = GroupingType
    form_class = GroupingTypeForm
    success_url = reverse_lazy("real_estate:grouping_type_list")
    list_url_name = "real_estate:grouping_type_list"
    entity_label = "Tipo de agrupacion"


class GroupingTypeUpdateView(EntityUpdateView):
    model = GroupingType
    form_class = GroupingTypeUpdateForm
    success_url = reverse_lazy("real_estate:grouping_type_list")
    list_url_name = "real_estate:grouping_type_list"
    entity_label = "Tipo de agrupacion"


class GroupingTypeStatusView(EntityStatusView):
    model = GroupingType
    list_url_name = "real_estate:grouping_type_list"


class StructuralGroupListView(EntityListView):
    model = StructuralGroup
    template_name = "real_estate/structural_group_list.html"
    context_object_name = "structural_groups"
    create_url_name = "real_estate:structural_group_create"
    update_url_name = "real_estate:structural_group_update"
    status_url_name = "real_estate:structural_group_status"
    entity_label = "Agrupacion"
    entity_label_plural = "Agrupaciones"
    search_fields = ("code", "name", "project__name", "grouping_type__name")

    def get_queryset(self):
        return super().get_queryset().select_related("project", "grouping_type", "parent")


class StructuralGroupCreateView(EntityCreateView):
    model = StructuralGroup
    form_class = StructuralGroupForm
    success_url = reverse_lazy("real_estate:structural_group_list")
    list_url_name = "real_estate:structural_group_list"
    entity_label = "Agrupacion"


class StructuralGroupUpdateView(EntityUpdateView):
    model = StructuralGroup
    form_class = StructuralGroupUpdateForm
    success_url = reverse_lazy("real_estate:structural_group_list")
    list_url_name = "real_estate:structural_group_list"
    entity_label = "Agrupacion"


class StructuralGroupStatusView(EntityStatusView):
    model = StructuralGroup
    list_url_name = "real_estate:structural_group_list"


class PropertyUnitListView(EntityListView):
    model = PropertyUnit
    template_name = "real_estate/property_unit_list.html"
    context_object_name = "property_units"
    create_url_name = "real_estate:property_unit_create"
    update_url_name = "real_estate:property_unit_update"
    status_url_name = "real_estate:property_unit_status"
    entity_label = "Unidad inmobiliaria"
    entity_label_plural = "Unidades inmobiliarias"
    search_fields = ("code", "name", "project__name", "structural_group__name")

    def get_queryset(self):
        return super().get_queryset().select_related("project", "structural_group")


class PropertyUnitCreateView(EntityCreateView):
    model = PropertyUnit
    form_class = PropertyUnitForm
    success_url = reverse_lazy("real_estate:property_unit_list")
    list_url_name = "real_estate:property_unit_list"
    entity_label = "Unidad inmobiliaria"


class PropertyUnitUpdateView(EntityUpdateView):
    model = PropertyUnit
    form_class = PropertyUnitUpdateForm
    success_url = reverse_lazy("real_estate:property_unit_list")
    list_url_name = "real_estate:property_unit_list"
    entity_label = "Unidad inmobiliaria"


class PropertyUnitStatusView(EntityStatusView):
    model = PropertyUnit
    list_url_name = "real_estate:property_unit_list"
