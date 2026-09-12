import uuid
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.forms import formset_factory

from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup
from real_estate.querysets import with_natural_unit_order

from .domain_services import (
    ASSIGNMENT_CHANGE_WITHOUT_NEW_ASSIGNMENT,
    MANUAL_INCLUSION_TYPE,
    NOVELTY_TYPE_CHOICES,
    validate_active_assignment_available,
    validate_unit_primary_available,
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
    ImportResolution,
    OperationalNovelty,
    Payment,
    UnitOwnership,
)
from users.models import User


DIRECT_UNITS_VALUE = "__direct__"
MAX_IMPORT_FILES = 25
MAX_IMPORT_FILE_SIZE_BYTES = 25 * 1024 * 1024


def short_client_name(client: Client | None) -> str:
    if not client:
        return "Sin titular"
    first = (client.first_names or "").split()
    last = (client.last_names_or_company or "").split()
    parts = []
    if first:
        parts.append(first[0].title())
    if last:
        parts.append(last[0].title())
    return " ".join(parts) if parts else "Sin titular"


def assignment_choice_label(assignment: FiduciaryAssignment) -> str:
    holder = assignment.active_primary_holder
    return f"{assignment.assignment_number} — {short_client_name(holder.client if holder else None)}"


class AssignmentChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return assignment_choice_label(obj)


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleExcelFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        if not data:
            return []
        if not isinstance(data, (list, tuple)):
            data = [data]
        return list(data)


class ChangeReasonMixin(forms.Form):
    change_reason = forms.CharField(
        label="Motivo",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )

    def apply_reason(self, instance):
        instance.last_change_reason = self.cleaned_data["change_reason"].strip()


class ClientFilterForm(forms.Form):
    q = forms.CharField(label="Buscar", required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    document = forms.CharField(
        label="Documento",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Numero completo o parcial"}),
    )
    document_type = forms.ChoiceField(
        label="Tipo de documento",
        required=False,
        choices=[("", "Todos")] + list(Client.DocumentType.choices),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    information_status = forms.ChoiceField(
        label="Estado de informacion",
        required=False,
        choices=[("", "Todos")] + list(Client.InformationStatus.choices),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    status = forms.ChoiceField(
        label="Estado",
        required=False,
        choices=[("", "Todos"), ("active", "Activos"), ("inactive", "Inactivos")],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    project = forms.ModelChoiceField(
        label="Proyecto",
        required=False,
        queryset=Project.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "project"}),
    )
    grouping_type = forms.ModelChoiceField(
        label="Tipo de agrupacion",
        required=False,
        queryset=GroupingType.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "grouping-type"}),
    )
    structural_group = forms.ModelChoiceField(
        label="Agrupacion",
        required=False,
        queryset=StructuralGroup.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "structural-group"}),
    )
    property_unit = forms.ModelChoiceField(
        label="Unidad",
        required=False,
        queryset=PropertyUnit.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "property-unit"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configure_unit_hierarchy_fields(self)

    def clean_q(self):
        return self.cleaned_data["q"].strip()

    def clean_document(self):
        return normalize_document_query(self.cleaned_data.get("document", ""))


def normalize_document_query(value: str) -> str:
    cleaned = (value or "").strip()
    for separator in (" ", ".", ",", "-"):
        cleaned = cleaned.replace(separator, "")
    return cleaned


def property_unit_choice_label(unit: PropertyUnit) -> str:
    unit_label = unit.name or unit.code
    group = getattr(unit, "structural_group", None)
    if group:
        group_label = group.code or group.name
        return f"{group_label} - {unit_label}"
    return unit_label


def use_property_unit_choice_labels(field) -> None:
    field.label_from_instance = property_unit_choice_label


def configure_unit_hierarchy_fields(form, *, active_only: bool = False, initial_unit: PropertyUnit | None = None) -> None:
    data = form.data if form.is_bound else None
    unit = initial_unit
    if not unit:
        unit_id = data.get("property_unit") if data else form.initial.get("property_unit")
        if unit_id:
            unit = PropertyUnit.objects.filter(pk=unit_id).select_related(
                "project", "structural_group", "structural_group__grouping_type"
            ).first()

    project_id = data.get("project") if data else (unit.project_id if unit else None)
    grouping_type_id = data.get("grouping_type") if data else (
        unit.structural_group.grouping_type_id if unit and unit.structural_group_id else None
    )
    structural_group_id = data.get("structural_group") if data else (unit.structural_group_id if unit else None)

    if "project" in form.fields:
        projects = Project.objects.order_by("name")
        if active_only:
            projects = projects.filter(is_active=True)
        form.fields["project"].queryset = projects
    if "grouping_type" in form.fields:
        types = GroupingType.objects.none()
        if project_id:
            types = GroupingType.objects.filter(structural_groups__project_id=project_id).distinct().order_by("name")
            if active_only:
                types = types.filter(is_active=True, structural_groups__is_active=True)
        form.fields["grouping_type"].queryset = types
    if "structural_group" in form.fields and isinstance(form.fields["structural_group"], forms.ModelChoiceField):
        groups = StructuralGroup.objects.none()
        if project_id and grouping_type_id:
            groups = StructuralGroup.objects.filter(project_id=project_id, grouping_type_id=grouping_type_id).select_related(
                "project", "grouping_type"
            ).order_by("name", "code")
            if active_only:
                groups = groups.filter(is_active=True)
        form.fields["structural_group"].queryset = groups
    if "property_unit" in form.fields:
        units = PropertyUnit.objects.none()
        if project_id and structural_group_id:
            units = PropertyUnit.objects.filter(project_id=project_id, structural_group_id=structural_group_id).select_related(
                "project", "structural_group"
            )
            if active_only:
                units = units.filter(is_active=True)
        elif unit:
            units = PropertyUnit.objects.filter(pk=unit.pk).select_related("project", "structural_group")
        form.fields["property_unit"].queryset = with_natural_unit_order(units)
        use_property_unit_choice_labels(form.fields["property_unit"])
        if not project_id:
            form.fields["property_unit"].widget.attrs["disabled"] = "disabled"
    if "grouping_type" in form.fields and not project_id:
        form.fields["grouping_type"].widget.attrs["disabled"] = "disabled"
    if "structural_group" in form.fields and not grouping_type_id:
        form.fields["structural_group"].widget.attrs["disabled"] = "disabled"
    if "property_unit" in form.fields and not structural_group_id:
        form.fields["property_unit"].widget.attrs["disabled"] = "disabled"


def unit_can_receive_new_assignment(unit: PropertyUnit) -> bool:
    if not unit or not unit.is_active:
        return False
    has_active_primary = UnitOwnership.objects.filter(
        property_unit=unit,
        is_active=True,
        is_primary=True,
        end_date__isnull=True,
    ).exists()
    return not has_active_primary


def assignment_has_current_holder(assignment: FiduciaryAssignment | None) -> bool:
    if not assignment or not assignment.pk:
        return False
    return assignment.holders.filter(is_active=True, end_date__isnull=True).exists()


def assignment_can_receive_payment(assignment: FiduciaryAssignment | None) -> bool:
    return bool(assignment and assignment.is_active and assignment_has_current_holder(assignment))


class NewFiduciaryAssignmentForm(forms.Form):
    project = forms.ModelChoiceField(
        label="Proyecto",
        queryset=Project.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "project"}),
    )
    grouping_type = forms.ModelChoiceField(
        label="Tipo de agrupacion",
        required=False,
        queryset=GroupingType.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "grouping-type"}),
    )
    structural_group = forms.ModelChoiceField(
        label="Agrupacion",
        required=False,
        queryset=StructuralGroup.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "structural-group"}),
    )
    property_unit = forms.ModelChoiceField(
        label="Unidad",
        queryset=PropertyUnit.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "property-unit"}),
    )
    assignment_number = forms.CharField(
        label="Numero del nuevo encargo fiduciario",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    primary_client_id = forms.IntegerField(widget=forms.HiddenInput())
    secondary_client_ids = forms.CharField(required=False, widget=forms.HiddenInput())
    adhesion_contract_date = forms.DateField(
        label="Contrato de adhesion",
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    promise_date = forms.DateField(
        label="Promesa",
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    promised_delivery_date = forms.DateField(
        label="Entrega segun promesa",
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    actual_delivery_date = forms.DateField(
        label="Entrega real",
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    confirm_without_dates = forms.BooleanField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, **kwargs):
        self.requires_date_confirmation = False
        super().__init__(*args, **kwargs)
        configure_unit_hierarchy_fields(self, active_only=True)

    def clean_assignment_number(self):
        value = (self.cleaned_data.get("assignment_number") or "").strip()
        if not value:
            raise ValidationError("Registre el numero del nuevo encargo fiduciario.")
        if FiduciaryAssignment.objects.filter(assignment_number=value).exists():
            raise ValidationError("Ya existe un encargo fiduciario con ese numero.")
        return value

    def clean_primary_client_id(self):
        client_id = self.cleaned_data.get("primary_client_id")
        client = Client.objects.filter(pk=client_id).first()
        if not client:
            raise ValidationError("Seleccione un cliente principal valido.")
        return client

    def clean_secondary_client_ids(self):
        raw = (self.cleaned_data.get("secondary_client_ids") or "").strip()
        if not raw:
            return []
        ids = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if not item.isdigit():
                raise ValidationError("La seleccion de secundarios no es valida.")
            ids.append(int(item))
        clients = list(Client.objects.filter(pk__in=ids))
        clients_by_id = {client.pk: client for client in clients}
        if len(clients_by_id) != len(set(ids)):
            raise ValidationError("Todos los clientes secundarios deben existir.")
        ordered = []
        seen = set()
        for client_id in ids:
            if client_id in seen:
                raise ValidationError("No puede seleccionar el mismo cliente secundario mas de una vez.")
            seen.add(client_id)
            ordered.append(clients_by_id[client_id])
        return ordered

    def clean(self):
        cleaned = super().clean()
        unit = cleaned.get("property_unit")
        project = cleaned.get("project")
        grouping_type = cleaned.get("grouping_type")
        group = cleaned.get("structural_group")
        primary = cleaned.get("primary_client_id")
        secondaries = cleaned.get("secondary_client_ids") or []
        if unit and project and unit.project_id != project.pk:
            self.add_error("property_unit", "La unidad no pertenece al proyecto seleccionado.")
        if unit and group and unit.structural_group_id != group.pk:
            self.add_error("property_unit", "La unidad no pertenece a la agrupacion seleccionada.")
        if unit and grouping_type and unit.structural_group_id and unit.structural_group.grouping_type_id != grouping_type.pk:
            self.add_error("property_unit", "La unidad no corresponde al tipo de agrupacion indicado.")
        if unit and not unit_can_receive_new_assignment(unit):
            self.add_error("property_unit", "La unidad seleccionada no esta disponible para un nuevo encargo activo.")
        if primary and primary in secondaries:
            self.add_error("secondary_client_ids", "El cliente principal no puede repetirse como secundario.")
        has_any_date = any(
            cleaned.get(field)
            for field in (
                "adhesion_contract_date",
                "promise_date",
                "promised_delivery_date",
                "actual_delivery_date",
            )
        )
        if not has_any_date and not cleaned.get("confirm_without_dates"):
            self.requires_date_confirmation = True
            self.add_error(None, "No has colocado ninguna fecha. ¿Aun asi quieres guardar?")
        return cleaned


class AddSecondaryAssignmentHolderForm(forms.Form):
    client_id = forms.IntegerField(widget=forms.HiddenInput())

    def __init__(self, *args, assignment=None, **kwargs):
        self.assignment = assignment
        super().__init__(*args, **kwargs)

    def clean_client_id(self):
        client_id = self.cleaned_data.get("client_id")
        client = Client.objects.filter(pk=client_id).first()
        if not client:
            raise ValidationError("Seleccione un cliente secundario valido.")
        return client

    def clean(self):
        cleaned = super().clean()
        client = cleaned.get("client_id")
        if not self.assignment or not client:
            return cleaned
        if self.assignment.holders.filter(client=client, is_active=True).exists():
            self.add_error("client_id", "El cliente ya esta asociado como titular vigente de este encargo.")
        if self.assignment.holders.filter(client=client, is_active=True, is_primary=True).exists():
            self.add_error("client_id", "El titular principal no puede agregarse como secundario.")
        active_primary = UnitOwnership.objects.filter(
            property_unit=self.assignment.property_unit,
            client=client,
            is_active=True,
            is_primary=True,
            end_date__isnull=True,
        ).exists()
        if active_primary:
            self.add_error("client_id", "Un titular principal vigente no puede agregarse como secundario.")
        return cleaned


class ExportHistoricalWorkbookForm(forms.Form):
    project = forms.ModelChoiceField(
        label="Proyecto",
        queryset=Project.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.order_by("name")


class ExportDocumentFilterForm(forms.Form):
    file_type = forms.ChoiceField(
        label="Tipo",
        required=False,
        choices=[
            ("", "Todos"),
            (ImportedFile.FileType.HISTORICAL, "Libros historicos"),
            (ImportedFile.FileType.REPORT, "Reportes fiduciarios"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    filename = forms.CharField(
        label="Nombre del archivo",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "KSMP"}),
    )
    date_from = forms.DateField(
        label="Desde",
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    date_to = forms.DateField(
        label="Hasta",
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )

    def clean_filename(self):
        return (self.cleaned_data.get("filename") or "").strip()


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = (
            "document_type",
            "document_number",
            "first_names",
            "last_names_or_company",
            "phone",
            "email",
            "address",
            "is_active",
        )
        widgets = {
            "document_type": forms.Select(attrs={"class": "form-select"}),
            "document_number": forms.TextInput(attrs={"class": "form-control"}),
            "first_names": forms.TextInput(attrs={"class": "form-control"}),
            "last_names_or_company": forms.TextInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "address": forms.TextInput(attrs={"class": "form-control"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        labels = {
            "document_type": "Tipo de documento",
            "document_number": "Numero de documento",
            "first_names": "Nombres",
            "last_names_or_company": "Apellidos o razon social",
            "phone": "Telefono",
            "email": "Correo electronico",
            "address": "Contacto",
            "is_active": "Activo",
        }

    def clean(self):
        cleaned = super().clean()
        cleaned["information_status"] = Client.InformationStatus.COMPLETE
        cleaned["source_origin"] = Client.SourceOrigin.MANUAL
        if not (cleaned.get("first_names") or "").strip():
            self.add_error("first_names", "Registre el nombre del cliente.")
        if not (cleaned.get("last_names_or_company") or "").strip():
            self.add_error("last_names_or_company", "Registre el apellido del cliente.")
        if cleaned.get("document_type") == Client.DocumentType.UNKNOWN:
            self.add_error("document_type", "Seleccione un tipo de documento valido.")
        if not (cleaned.get("document_number") or "").strip():
            self.add_error("document_number", "Registre el numero de documento.")
        phone = (cleaned.get("phone") or "").strip()
        email = (cleaned.get("email") or "").strip()
        if not phone and not email:
            raise ValidationError("Debe registrar al menos un telefono o un correo electronico.")
        return cleaned

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["document_type"].choices = [
            choice for choice in Client.DocumentType.choices if choice[0] != Client.DocumentType.UNKNOWN
        ]
        self.fields["document_number"].required = True
        self.fields["first_names"].required = True
        self.fields["last_names_or_company"].required = True

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.information_status = Client.InformationStatus.COMPLETE
        instance.source_origin = Client.SourceOrigin.MANUAL
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class ClientUpdateForm(ChangeReasonMixin, ClientForm):
    pass


class StatusReasonForm(forms.Form):
    change_reason = forms.CharField(label="Motivo", widget=forms.TextInput(attrs={"class": "form-control"}))
    end_date = forms.DateField(
        label="Fecha de finalizacion",
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )

    def clean_change_reason(self):
        return self.cleaned_data["change_reason"].strip()


class UnitOwnershipForm(ChangeReasonMixin, forms.ModelForm):
    project = forms.ModelChoiceField(
        label="Proyecto",
        required=False,
        queryset=Project.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "project"}),
    )
    grouping_type = forms.ModelChoiceField(
        label="Tipo de agrupacion",
        required=False,
        queryset=GroupingType.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "grouping-type"}),
    )
    structural_group = forms.ModelChoiceField(
        label="Agrupacion",
        required=False,
        queryset=StructuralGroup.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "structural-group"}),
    )
    novelty_type = forms.ChoiceField(
        label="Tipo de novedad",
        choices=[
            (OperationalNovelty.NoveltyType.CESSION, "Cesion"),
            (OperationalNovelty.NoveltyType.SUBSTITUTION, "Sustitucion"),
            (OperationalNovelty.NoveltyType.ADMINISTRATIVE_CORRECTION, "Correccion administrativa"),
            (OperationalNovelty.NoveltyType.OTHER, "Otro"),
        ],
        widget=forms.Select(attrs={"class": "form-select", "data-novelty-type": "true"}),
    )
    other_type = forms.CharField(
        label="Cual",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "data-other-type": "true"}),
    )
    change_reason = forms.CharField(
        label="Observacion o motivo",
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )
    assignment_number = forms.CharField(
        label="Numero del nuevo encargo fiduciario",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    secondary_clients = forms.ModelMultipleChoiceField(
        label="Clientes secundarios asociados",
        required=False,
        queryset=Client.objects.none(),
        widget=forms.SelectMultiple(attrs={"class": "form-select"}),
    )

    class Meta:
        model = UnitOwnership
        fields = ("client", "project", "grouping_type", "structural_group", "property_unit", "start_date")
        widgets = {
            "client": forms.Select(attrs={"class": "form-select"}),
            "property_unit": forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "property-unit"}),
            "start_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        client_id = self.data.get("client") if self.is_bound else self.initial.get("client")
        if client_id:
            self.fields["client"].queryset = Client.objects.filter(is_active=True, pk=client_id)
        else:
            self.fields["client"].queryset = Client.objects.none()
        initial_unit = None
        if self.instance.pk:
            initial_unit = self.instance.property_unit
        configure_unit_hierarchy_fields(self, active_only=True, initial_unit=initial_unit)
        self.fields["secondary_clients"].queryset = Client.objects.filter(is_active=True).order_by(
            "last_names_or_company", "first_names"
        )

    def clean(self):
        cleaned = super().clean()
        client = cleaned.get("client")
        unit = cleaned.get("property_unit")
        assignment_number = (cleaned.get("assignment_number") or "").strip()
        cleaned["assignment_number"] = assignment_number
        secondary_clients = list(cleaned.get("secondary_clients") or [])
        other_type = (cleaned.get("other_type") or "").strip()
        cleaned["other_type"] = other_type
        if cleaned.get("novelty_type") == OperationalNovelty.NoveltyType.OTHER and not other_type:
            self.add_error("other_type", "Indique cual es la novedad.")
        if assignment_number and FiduciaryAssignment.objects.filter(assignment_number=assignment_number).exists():
            self.add_error("assignment_number", "El numero de encargo ya existe y no puede reutilizarse.")
        if client and client in secondary_clients:
            self.add_error("secondary_clients", "El titular principal no debe repetirse como secundario.")
        if client and unit:
            duplicate = UnitOwnership.objects.filter(client=client, property_unit=unit, is_active=True)
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error("client", "El cliente ya tiene una titularidad vigente sobre esta unidad.")
        if unit:
            project = cleaned.get("project")
            grouping_type = cleaned.get("grouping_type")
            structural_group = cleaned.get("structural_group")
            if project and unit.project_id != project.pk:
                self.add_error("property_unit", "La unidad no pertenece al proyecto seleccionado.")
            if structural_group and unit.structural_group_id != structural_group.pk:
                self.add_error("property_unit", "La unidad no pertenece a la agrupacion seleccionada.")
            if grouping_type and unit.structural_group_id and unit.structural_group.grouping_type_id != grouping_type.pk:
                self.add_error("property_unit", "La unidad no corresponde al tipo de agrupacion seleccionado.")
        if len({client.pk for client in secondary_clients}) != len(secondary_clients):
            self.add_error("secondary_clients", "No puede seleccionar el mismo cliente secundario mas de una vez.")
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.is_primary = True
        self.apply_reason(instance)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class AssignmentFilterForm(forms.Form):
    q = forms.CharField(label="Buscar", required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    project = forms.ModelChoiceField(
        label="Proyecto",
        required=False,
        queryset=Project.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    grouping_type = forms.ModelChoiceField(
        label="Tipo",
        required=False,
        queryset=GroupingType.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    structural_group = forms.ModelChoiceField(
        label="Agrupacion",
        required=False,
        queryset=StructuralGroup.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    property_unit = forms.ModelChoiceField(
        label="Unidad",
        required=False,
        queryset=PropertyUnit.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    client = forms.ModelChoiceField(
        label="Cliente",
        required=False,
        queryset=Client.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    status = forms.ChoiceField(
        label="Estado",
        required=False,
        choices=[("", "Todos"), ("active", "Vigentes"), ("inactive", "Inactivos")],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    start_from = forms.DateField(
        label="Inicio desde",
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    start_to = forms.DateField(
        label="Inicio hasta",
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        project_id = self.data.get("project") if self.is_bound else None
        grouping_type_id = self.data.get("grouping_type") if self.is_bound else None
        group_id = self.data.get("structural_group") if self.is_bound else None
        for field_name, role in (
            ("project", "project"),
            ("grouping_type", "grouping-type"),
            ("structural_group", "structural-group"),
            ("property_unit", "property-unit"),
        ):
            self.fields[field_name].widget.attrs["data-unit-hierarchy-field"] = role
        configure_unit_hierarchy_fields(self)
        self.fields["client"].queryset = Client.objects.order_by("last_names_or_company", "first_names")

    def clean_q(self):
        return self.cleaned_data["q"].strip()


class ObservationFilterForm(forms.Form):
    project = forms.ModelChoiceField(
        label="Proyecto",
        required=False,
        queryset=Project.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "project"}),
    )
    grouping_type = forms.ModelChoiceField(
        label="Tipo de agrupacion",
        required=False,
        queryset=GroupingType.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "grouping-type"}),
    )
    structural_group = forms.ModelChoiceField(
        label="Agrupacion",
        required=False,
        queryset=StructuralGroup.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "structural-group"}),
    )
    property_unit = forms.ModelChoiceField(
        label="Unidad",
        required=False,
        queryset=PropertyUnit.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "property-unit"}),
    )
    client = forms.ModelChoiceField(
        label="Cliente",
        required=False,
        queryset=Client.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    document = forms.CharField(label="Documento", required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    assignment_number = forms.CharField(label="Encargo", required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    origin = forms.ChoiceField(
        label="Origen",
        required=False,
        choices=[("", "Todos")] + list(ImportedHistoricalObservation.Origin.choices),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    date_from = forms.DateField(
        label="Desde",
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    date_to = forms.DateField(
        label="Hasta",
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configure_unit_hierarchy_fields(self)
        self.fields["client"].queryset = Client.objects.order_by("last_names_or_company", "first_names", "document_number")

    def clean_document(self):
        return normalize_document_query(self.cleaned_data.get("document", ""))

    def clean_assignment_number(self):
        return self.cleaned_data.get("assignment_number", "").strip()


class ObservationForm(forms.ModelForm):
    change_reason = forms.CharField(
        label="Motivo",
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )
    project = forms.ModelChoiceField(
        label="Proyecto",
        queryset=Project.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "project"}),
    )
    grouping_type = forms.ModelChoiceField(
        label="Tipo de agrupacion",
        required=False,
        queryset=GroupingType.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "grouping-type"}),
    )
    structural_group = forms.ModelChoiceField(
        label="Agrupacion",
        required=False,
        queryset=StructuralGroup.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "structural-group"}),
    )
    assignment = AssignmentChoiceField(
        label="Encargo relacionado",
        queryset=FiduciaryAssignment.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = ImportedHistoricalObservation
        fields = ("project", "grouping_type", "structural_group", "property_unit", "assignment", "summary", "detail")
        widgets = {
            "property_unit": forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "property-unit"}),
            "assignment": forms.Select(attrs={"class": "form-select"}),
            "summary": forms.TextInput(attrs={"class": "form-control"}),
            "detail": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
        }
        labels = {
            "property_unit": "Unidad",
            "assignment": "Encargo relacionado",
            "summary": "Resumen",
            "detail": "Detalle",
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        self.require_change_reason = kwargs.pop("require_change_reason", False)
        super().__init__(*args, **kwargs)
        data = self.data if self.is_bound else None
        initial_unit = self.instance.property_unit if self.instance.pk else None
        project_id = data.get("project") if data else getattr(initial_unit, "project_id", None)
        unit_id = data.get("property_unit") if data else self.instance.property_unit_id
        configure_unit_hierarchy_fields(self, active_only=True, initial_unit=initial_unit)
        self.fields["assignment"].queryset = FiduciaryAssignment.objects.none()
        if unit_id:
            self.fields["assignment"].queryset = (
                FiduciaryAssignment.objects.filter(property_unit_id=unit_id)
                .prefetch_related("holders__client")
                .order_by("-is_active", "assignment_number")
            )
        if self.instance.pk and self.instance.origin != ImportedHistoricalObservation.Origin.MANUAL:
            for field in self.fields.values():
                field.disabled = True
        if not self.require_change_reason:
            self.fields.pop("change_reason")
        else:
            self.fields["change_reason"].required = True

    def clean_detail(self):
        return self.cleaned_data["detail"].strip()

    def clean_summary(self):
        return (self.cleaned_data.get("summary") or "").strip()

    def clean_change_reason(self):
        reason = (self.cleaned_data.get("change_reason") or "").strip()
        if self.require_change_reason and not reason:
            raise ValidationError("Registre el motivo de la modificacion.")
        return reason

    def clean(self):
        cleaned = super().clean()
        project = cleaned.get("project")
        unit = cleaned.get("property_unit")
        assignment = cleaned.get("assignment")
        if unit and project and unit.project_id != project.pk:
            self.add_error("property_unit", "La unidad no pertenece al proyecto seleccionado.")
        if assignment and unit and assignment.property_unit_id != unit.pk:
            self.add_error("assignment", "El encargo no pertenece a la unidad seleccionada.")
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.origin = ImportedHistoricalObservation.Origin.MANUAL
        instance.status = ImportedHistoricalObservation.Status.IMPORTED
        instance.project = instance.property_unit.project
        instance.client = None
        if self.user and not instance.pk:
            instance.imported_by = self.user
        if self.user and instance.pk:
            instance.updated_by = self.user
        if not instance.dedupe_key:
            instance.dedupe_key = uuid.uuid4().hex
        if commit:
            instance.full_clean()
            instance.save()
            self.save_m2m()
        return instance


class NoveltyFilterForm(forms.Form):
    project = forms.ModelChoiceField(label="Proyecto", required=False, queryset=Project.objects.none(), widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "project"}))
    grouping_type = forms.ModelChoiceField(label="Tipo de agrupacion", required=False, queryset=GroupingType.objects.none(), widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "grouping-type"}))
    structural_group = forms.ModelChoiceField(label="Agrupacion", required=False, queryset=StructuralGroup.objects.none(), widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "structural-group"}))
    property_unit = forms.ModelChoiceField(label="Unidad", required=False, queryset=PropertyUnit.objects.none(), widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "property-unit"}))
    novelty_type = forms.ChoiceField(
        label="Tipo",
        required=False,
        choices=[("", "Todos")] + list(OperationalNovelty.NoveltyType.choices),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    client = forms.ModelChoiceField(label="Cliente", required=False, queryset=Client.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    document = forms.CharField(label="Documento", required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    assignment_number = forms.CharField(label="Encargo", required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    origin = forms.ChoiceField(
        label="Origen",
        required=False,
        choices=[("", "Todos")] + list(OperationalNovelty.Origin.choices),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    date_from = forms.DateField(label="Desde", required=False, widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    date_to = forms.DateField(label="Hasta", required=False, widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configure_unit_hierarchy_fields(self)
        self.fields["client"].queryset = Client.objects.order_by("last_names_or_company", "first_names", "document_number")

    def clean_document(self):
        return normalize_document_query(self.cleaned_data.get("document", ""))

    def clean_assignment_number(self):
        return self.cleaned_data.get("assignment_number", "").strip()


class PaymentFilterForm(forms.Form):
    project = forms.ModelChoiceField(label="Proyecto", required=False, queryset=Project.objects.none(), widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "project"}))
    grouping_type = forms.ModelChoiceField(label="Tipo de agrupacion", required=False, queryset=GroupingType.objects.none(), widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "grouping-type"}))
    structural_group = forms.ModelChoiceField(label="Agrupacion", required=False, queryset=StructuralGroup.objects.none(), widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "structural-group"}))
    property_unit = forms.ModelChoiceField(label="Unidad", required=False, queryset=PropertyUnit.objects.none(), widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "property-unit"}))
    client = forms.ModelChoiceField(label="Cliente", required=False, queryset=Client.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    document = forms.CharField(label="Documento", required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    assignment_number = forms.CharField(label="Encargo fiduciario", required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    date_from = forms.DateField(label="Fecha inicial", required=False, widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    date_to = forms.DateField(label="Fecha final", required=False, widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configure_unit_hierarchy_fields(self)
        self.fields["client"].queryset = Client.objects.order_by("last_names_or_company", "first_names", "document_number")

    def has_criteria(self):
        if not self.is_valid():
            return False
        return any(self.cleaned_data.get(name) for name in self.fields)

    def clean_document(self):
        return normalize_document_query(self.cleaned_data.get("document", ""))

    def clean_assignment_number(self):
        return self.cleaned_data.get("assignment_number", "").strip()


class ManualPaymentForm(forms.Form):
    exact_date = forms.DateField(
        label="Fecha",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    amount = forms.DecimalField(
        label="Valor",
        min_value=Decimal("0.01"),
        max_digits=18,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
    )
    concept = forms.CharField(
        label="Concepto",
        max_length=180,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    destination = forms.ChoiceField(
        label="Recibido por",
        choices=Payment.Destination.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def clean_concept(self):
        return " ".join((self.cleaned_data.get("concept") or "").split())


class GlobalManualPaymentForm(ManualPaymentForm):
    assignment_number = forms.CharField(
        label="Numero de encargo fiduciario",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Numero exacto del encargo"}),
    )
    project = forms.ModelChoiceField(
        label="Proyecto",
        required=False,
        queryset=Project.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "project"}),
    )
    grouping_type = forms.ModelChoiceField(
        label="Tipo de agrupacion",
        required=False,
        queryset=GroupingType.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "grouping-type"}),
    )
    structural_group = forms.ModelChoiceField(
        label="Agrupacion",
        required=False,
        queryset=StructuralGroup.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "structural-group"}),
    )
    property_unit = forms.ModelChoiceField(
        label="Unidad",
        required=False,
        queryset=PropertyUnit.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "property-unit"}),
    )

    field_order = [
        "assignment_number",
        "project",
        "grouping_type",
        "structural_group",
        "property_unit",
        "exact_date",
        "amount",
        "concept",
        "destination",
    ]

    def __init__(self, *args, **kwargs):
        self.resolved_assignment = None
        super().__init__(*args, **kwargs)
        configure_unit_hierarchy_fields(self, active_only=True)
        self.order_fields(self.field_order)

    def clean_assignment_number(self):
        return " ".join((self.cleaned_data.get("assignment_number") or "").split())

    def clean(self):
        cleaned = super().clean()
        assignment_number = cleaned.get("assignment_number")
        unit = cleaned.get("property_unit")
        project = cleaned.get("project")
        grouping_type = cleaned.get("grouping_type")
        group = cleaned.get("structural_group")
        assignment = None

        if assignment_number:
            matches = list(FiduciaryAssignment.objects.filter(assignment_number__iexact=assignment_number).order_by("pk")[:2])
            if not matches:
                self.add_error("assignment_number", "No existe un encargo fiduciario con ese numero.")
            elif len(matches) > 1:
                self.add_error("assignment_number", "El numero de encargo no permite una seleccion inequivoca.")
            elif not assignment_can_receive_payment(matches[0]):
                self.add_error(
                    "assignment_number",
                    "No se puede registrar el pago porque el encargo no tiene titulares vigentes.",
                )
            else:
                assignment = matches[0]

        if unit and project and unit.project_id != project.pk:
            self.add_error("property_unit", "La unidad no pertenece al proyecto seleccionado.")
        if unit and group and unit.structural_group_id != group.pk:
            self.add_error("property_unit", "La unidad no pertenece a la agrupacion seleccionada.")
        if unit and grouping_type and unit.structural_group_id and unit.structural_group.grouping_type_id != grouping_type.pk:
            self.add_error("property_unit", "La unidad no corresponde al tipo de agrupacion indicado.")
        if unit:
            active_assignments = list(
                FiduciaryAssignment.objects.filter(property_unit=unit, is_active=True).order_by("-start_date", "-pk")
            )
            payable_assignments = [item for item in active_assignments if assignment_can_receive_payment(item)]
            if not payable_assignments:
                self.add_error(
                    "property_unit",
                    "La unidad seleccionada no tiene un encargo fiduciario activo con titulares vigentes.",
                )
            elif len(payable_assignments) > 1:
                self.add_error("property_unit", "La unidad seleccionada tiene mas de un encargo activo.")
            elif assignment and assignment.pk != payable_assignments[0].pk:
                self.add_error("property_unit", "La unidad seleccionada no corresponde al encargo indicado.")
            else:
                assignment = payable_assignments[0]

        if not assignment_number and not unit:
            raise ValidationError("Busque el encargo por numero o seleccione una unidad.")
        if assignment:
            self.resolved_assignment = assignment
            cleaned["assignment"] = assignment
        return cleaned


class AuditFilterForm(forms.Form):
    responsible = forms.ModelChoiceField(label="Responsable", required=False, queryset=User.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    date_from = forms.DateField(label="Fecha inicial", required=False, widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    date_to = forms.DateField(label="Fecha final", required=False, widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    action = forms.ChoiceField(label="Accion", required=False, choices=[("", "Todas")] + list(ImportAppliedRecord.Action.choices), widget=forms.Select(attrs={"class": "form-select"}))
    entity_kind = forms.ChoiceField(label="Modulo o entidad", required=False, choices=[("", "Todos")] + list(ImportAppliedRecord.EntityKind.choices), widget=forms.Select(attrs={"class": "form-select"}))
    reason = forms.CharField(label="Motivo", required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    batch = forms.ModelChoiceField(label="Lote", required=False, queryset=ImportBatch.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    imported_file = forms.ModelChoiceField(label="Archivo", required=False, queryset=ImportedFile.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["responsible"].queryset = User.objects.order_by("email", "username")
        self.fields["batch"].queryset = ImportBatch.objects.select_related("initiated_by").order_by("-created_at")
        self.fields["imported_file"].queryset = ImportedFile.objects.order_by("-created_at", "original_name")

    def clean_reason(self):
        return self.cleaned_data.get("reason", "").strip()


class OperationalNoveltyForm(forms.ModelForm):
    MANUAL_NOVELTY_TYPE_CHOICES = NOVELTY_TYPE_CHOICES + [(MANUAL_INCLUSION_TYPE, "Inclusion")]
    PRINCIPAL_ALLOWED_TYPES = {
        OperationalNovelty.NoveltyType.CESSION,
        OperationalNovelty.NoveltyType.WITHDRAWAL,
        OperationalNovelty.NoveltyType.SUBSTITUTION,
        OperationalNovelty.NoveltyType.ADMINISTRATIVE_CORRECTION,
        OperationalNovelty.NoveltyType.OTHER,
        MANUAL_INCLUSION_TYPE,
    }
    SECONDARY_ALLOWED_TYPES = {
        OperationalNovelty.NoveltyType.EXCLUSION,
        OperationalNovelty.NoveltyType.WITHDRAWAL,
        OperationalNovelty.NoveltyType.SUBSTITUTION,
        OperationalNovelty.NoveltyType.ADMINISTRATIVE_CORRECTION,
        OperationalNovelty.NoveltyType.OTHER,
    }
    project = forms.ModelChoiceField(label="Proyecto", queryset=Project.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    grouping_type = forms.ModelChoiceField(
        label="Tipo de agrupacion",
        required=False,
        queryset=GroupingType.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "grouping-type"}),
    )
    structural_group = forms.ModelChoiceField(
        label="Agrupacion",
        required=False,
        queryset=StructuralGroup.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "structural-group"}),
    )
    new_assignment_number = forms.CharField(
        label="Nuevo numero de encargo",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    current_assignment = AssignmentChoiceField(
        label="Encargo actual",
        required=False,
        queryset=FiduciaryAssignment.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    current_client = forms.ModelChoiceField(
        label="Cliente de la novedad",
        required=False,
        queryset=Client.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    novelty_type = forms.ChoiceField(
        label="Tipo de novedad",
        choices=MANUAL_NOVELTY_TYPE_CHOICES,
        widget=forms.Select(attrs={"class": "form-select", "data-novelty-type": "true"}),
    )
    secondary_clients = forms.ModelMultipleChoiceField(
        label="Clientes secundarios asociados",
        required=False,
        queryset=Client.objects.none(),
        widget=forms.SelectMultiple(attrs={"class": "form-select", "data-secondary-client-select": "true"}),
    )

    class Meta:
        model = OperationalNovelty
        fields = (
            "project",
            "grouping_type",
            "structural_group",
            "property_unit",
            "current_assignment",
            "current_client",
            "novelty_type",
            "other_type",
            "effective_date",
            "new_client",
            "summary",
            "detail",
        )
        widgets = {
            "property_unit": forms.Select(attrs={"class": "form-select", "data-unit-hierarchy-field": "property-unit"}),
            "novelty_type": forms.Select(attrs={"class": "form-select", "data-novelty-type": "true"}),
            "other_type": forms.TextInput(attrs={"class": "form-control", "data-other-type": "true"}),
            "effective_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "new_client": forms.Select(attrs={"class": "form-select"}),
            "summary": forms.TextInput(attrs={"class": "form-control"}),
            "detail": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
        }
        labels = {
            "property_unit": "Unidad",
            "novelty_type": "Tipo de novedad",
            "other_type": "Cual",
            "effective_date": "Fecha efectiva",
            "new_client": "Nuevo titular principal",
            "summary": "Resumen",
            "detail": "Detalle",
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        data = self.data if self.is_bound else None
        unit_id = data.get("property_unit") if data else self.initial.get("property_unit")
        self.fields["project"].widget.attrs["data-unit-hierarchy-field"] = "project"
        configure_unit_hierarchy_fields(self, active_only=True)
        self.fields["novelty_type"].choices = self.MANUAL_NOVELTY_TYPE_CHOICES
        self.fields["new_client"].required = False
        selected_new_client = data.get("new_client") if data else self.initial.get("new_client")
        selected_assignment = data.get("current_assignment") if data else self.initial.get("current_assignment")
        selected_current_client = data.get("current_client") if data else self.initial.get("current_client")
        if data and hasattr(data, "getlist"):
            selected_secondary_clients = data.getlist("secondary_clients")
        elif data:
            raw_secondary = data.get("secondary_clients", [])
            selected_secondary_clients = raw_secondary if isinstance(raw_secondary, (list, tuple)) else [raw_secondary]
        else:
            selected_secondary_clients = self.initial.get("secondary_clients", [])
        self.fields["new_client"].queryset = (
            Client.objects.filter(is_active=True, pk=selected_new_client).order_by("last_names_or_company", "first_names")
            if selected_new_client
            else Client.objects.none()
        )
        self.fields["secondary_clients"].queryset = (
            Client.objects.filter(is_active=True, pk__in=selected_secondary_clients).order_by("last_names_or_company", "first_names")
            if selected_secondary_clients
            else Client.objects.none()
        )
        if unit_id:
            assignments = FiduciaryAssignment.objects.filter(property_unit_id=unit_id, is_active=True).prefetch_related("holders__client")
            self.fields["current_assignment"].queryset = assignments.order_by("-start_date", "-pk")
        if selected_assignment:
            current_client_filter = Q(
                fiduciary_assignment_holders__assignment_id=selected_assignment,
                fiduciary_assignment_holders__is_active=True,
            )
            if str(selected_current_client or "").isdigit():
                current_client_filter |= Q(pk=selected_current_client)
            current_clients = Client.objects.filter(current_client_filter, is_active=True).distinct().order_by(
                "last_names_or_company", "first_names"
            )
            self.fields["current_client"].queryset = current_clients
        elif selected_current_client:
            self.fields["current_client"].queryset = Client.objects.filter(pk=selected_current_client)

    def clean_detail(self):
        detail = (self.cleaned_data.get("detail") or "").strip()
        if not detail:
            raise ValidationError("Registre el detalle de la novedad.")
        return detail

    def _post_clean(self):
        requested_type = getattr(self, "cleaned_data", {}).get("novelty_type")
        if requested_type == MANUAL_INCLUSION_TYPE:
            self.cleaned_data["novelty_type"] = OperationalNovelty.NoveltyType.OTHER
            self.cleaned_data["other_type"] = "INCLUSION"
            super()._post_clean()
            self.cleaned_data["novelty_type"] = MANUAL_INCLUSION_TYPE
            self.cleaned_data["other_type"] = "INCLUSION"
            self.instance.novelty_type = OperationalNovelty.NoveltyType.OTHER
            self.instance.other_type = "INCLUSION"
            return
        super()._post_clean()

    def clean(self):
        cleaned = super().clean()
        project = cleaned.get("project")
        unit = cleaned.get("property_unit")
        novelty_type = cleaned.get("novelty_type")
        other_type = (cleaned.get("other_type") or "").strip()
        cleaned["other_type"] = other_type
        new_client = cleaned.get("new_client")
        new_assignment = (cleaned.get("new_assignment_number") or "").strip()
        cleaned["new_assignment_number"] = new_assignment
        secondary_clients = list(cleaned.get("secondary_clients") or [])
        current_assignment = cleaned.get("current_assignment")
        current_client = cleaned.get("current_client")
        if unit and project and unit.project_id != project.pk:
            self.add_error("property_unit", "La unidad no pertenece al proyecto seleccionado.")
        if not current_assignment:
            self.add_error("current_assignment", "Seleccione el encargo fiduciario.")
        elif unit and current_assignment.property_unit_id != unit.pk:
            self.add_error("current_assignment", "El encargo no pertenece a la unidad seleccionada.")
        current_holder = None
        if current_assignment and current_client:
            current_holder = current_assignment.holders.filter(client=current_client, is_active=True).first()
            if not current_holder:
                self.add_error("current_client", "El cliente seleccionado no pertenece al encargo actual.")

        if not current_client:
            self.add_error("current_client", "Seleccione el cliente de la novedad.")
        elif current_holder is None and unit and not has_active_unit_ownership(current_client, unit):
            self.add_error("current_client", "El cliente no es titular vigente de la unidad seleccionada.")
        if novelty_type == OperationalNovelty.NoveltyType.HISTORICAL:
            self.add_error("novelty_type", "Importacion historica no es una novedad manual.")
        if current_holder:
            if novelty_type == OperationalNovelty.NoveltyType.CESSION and not current_holder.is_primary:
                self.add_error("novelty_type", "La cesion solo esta permitida para el titular principal del encargo.")
            elif novelty_type == OperationalNovelty.NoveltyType.EXCLUSION and current_holder.is_primary:
                self.add_error("novelty_type", "La exclusion solo esta permitida para clientes secundarios.")
            if current_holder.is_primary and novelty_type not in self.PRINCIPAL_ALLOWED_TYPES:
                self.add_error("novelty_type", "El tipo seleccionado no esta permitido para el titular principal.")
            if not current_holder.is_primary and novelty_type not in self.SECONDARY_ALLOWED_TYPES:
                self.add_error("novelty_type", "El tipo seleccionado no esta permitido para un cliente secundario.")
        if novelty_type in {
            OperationalNovelty.NoveltyType.CESSION,
            OperationalNovelty.NoveltyType.WITHDRAWAL,
            OperationalNovelty.NoveltyType.EXCLUSION,
            OperationalNovelty.NoveltyType.SUBSTITUTION,
            OperationalNovelty.NoveltyType.ADMINISTRATIVE_CORRECTION,
            MANUAL_INCLUSION_TYPE,
        } and not cleaned.get("effective_date"):
            self.add_error("effective_date", "Indique la fecha efectiva de la novedad.")
        if novelty_type == OperationalNovelty.NoveltyType.OTHER and not other_type:
            self.add_error("other_type", "Indique cual es la novedad.")
        if novelty_type in {OperationalNovelty.NoveltyType.CESSION, OperationalNovelty.NoveltyType.SUBSTITUTION}:
            if not new_client:
                self.add_error("new_client", "Seleccione el nuevo titular principal.")
            if not new_assignment:
                self.add_error("new_assignment_number", "Registre el nuevo numero de encargo.")
            if new_client and current_client and new_client.pk == current_client.pk:
                self.add_error("new_client", "El nuevo titular debe ser diferente al titular actual.")
        if novelty_type == MANUAL_INCLUSION_TYPE:
            primary_holder = (
                current_assignment.holders.filter(is_active=True, is_primary=True).select_related("client").first()
                if current_assignment
                else None
            )
            if not primary_holder:
                self.add_error("current_assignment", "El encargo no tiene titular principal vigente.")
            if not secondary_clients:
                self.add_error("secondary_clients", "Seleccione el nuevo cliente secundario.")
            for secondary_client in secondary_clients:
                if primary_holder and secondary_client.pk == primary_holder.client_id:
                    self.add_error("secondary_clients", "El titular principal no puede agregarse como secundario.")
                    break
                if current_assignment and current_assignment.holders.filter(client=secondary_client, is_active=True).exists():
                    self.add_error("secondary_clients", "El cliente ya esta asociado como titular vigente de este encargo.")
                    break
        if new_client and new_client in secondary_clients:
            self.add_error("secondary_clients", "El titular principal no debe repetirse como secundario.")
        if current_client and current_client in secondary_clients:
            self.add_error("secondary_clients", "El titular actual no debe repetirse como secundario.")
        if len({client.pk for client in secondary_clients}) != len(secondary_clients):
            self.add_error("secondary_clients", "No puede seleccionar el mismo cliente secundario mas de una vez.")
        if new_assignment and FiduciaryAssignment.objects.filter(assignment_number=new_assignment).exists():
            self.add_error("new_assignment_number", "Ya existe un encargo fiduciario con ese numero.")
        return cleaned


class FiduciaryAssignmentForm(ChangeReasonMixin, forms.ModelForm):
    project = forms.ModelChoiceField(
        label="Proyecto",
        required=False,
        queryset=Project.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-context-field": "project"}),
    )
    grouping_type = forms.ModelChoiceField(
        label="Tipo de agrupacion",
        required=False,
        queryset=GroupingType.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-context-field": "grouping-type"}),
    )
    structural_group = forms.ChoiceField(
        label="Agrupacion",
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-context-field": "structural-group"}),
    )
    primary_client = forms.ModelChoiceField(
        label="Titular principal",
        queryset=Client.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-holder-role": "primary"}),
    )

    class Meta:
        model = FiduciaryAssignment
        fields = (
            "project",
            "grouping_type",
            "structural_group",
            "assignment_number",
            "property_unit",
            "start_date",
            "observations",
            "primary_client",
        )
        widgets = {
            "assignment_number": forms.TextInput(attrs={"class": "form-control"}),
            "property_unit": forms.Select(attrs={"class": "form-select", "data-context-field": "property-unit"}),
            "start_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "observations": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        data = self.data if self.is_bound else None
        project_id = data.get("project") if data else None
        grouping_type_id = data.get("grouping_type") if data else None
        structural_group_id = data.get("structural_group") if data else None
        unit_id = data.get("property_unit") if data else self.initial.get("property_unit")

        self.fields["project"].queryset = Project.objects.filter(is_active=True).order_by("name")
        self.fields["grouping_type"].queryset = GroupingType.objects.none()
        self.fields["property_unit"].queryset = PropertyUnit.objects.none()
        self.fields["structural_group"].choices = [("", "---------")]

        groups = StructuralGroup.objects.none()
        units = PropertyUnit.objects.none()
        if project_id:
            self.fields["grouping_type"].queryset = (
                GroupingType.objects.filter(
                    is_active=True,
                    structural_groups__project_id=project_id,
                    structural_groups__is_active=True,
                )
                .distinct()
                .order_by("name")
            )
            groups = StructuralGroup.objects.filter(is_active=True, project_id=project_id).select_related(
                "project", "grouping_type"
            )
            group_choices = [("", "---------"), (DIRECT_UNITS_VALUE, "Unidades directas del proyecto")]
            if grouping_type_id:
                groups = groups.filter(grouping_type_id=grouping_type_id)
            group_choices.extend((str(group.pk), str(group)) for group in groups.order_by("name", "code"))
            self.fields["structural_group"].choices = group_choices
        if project_id and structural_group_id == DIRECT_UNITS_VALUE:
            units = PropertyUnit.objects.filter(
                is_active=True,
                project_id=project_id,
                structural_group__isnull=True,
            ).select_related("project", "structural_group")
        elif project_id and structural_group_id:
            units = PropertyUnit.objects.filter(
                is_active=True,
                project_id=project_id,
                structural_group_id=structural_group_id,
            ).select_related("project", "structural_group")
        elif unit_id and self.is_bound:
            units = PropertyUnit.objects.filter(is_active=True, pk=unit_id).select_related("project", "structural_group")

        if not project_id:
            self.fields["grouping_type"].widget.attrs["disabled"] = "disabled"
            self.fields["structural_group"].widget.attrs["disabled"] = "disabled"
        if not structural_group_id:
            self.fields["property_unit"].widget.attrs["disabled"] = "disabled"
        if not unit_id:
            self.fields["primary_client"].widget.attrs["disabled"] = "disabled"
        self.fields["property_unit"].queryset = with_natural_unit_order(units)
        use_property_unit_choice_labels(self.fields["property_unit"])
        self.fields["primary_client"].queryset = eligible_assignment_clients(unit_id)

    def clean(self):
        cleaned = super().clean()
        project = cleaned.get("project")
        grouping_type = cleaned.get("grouping_type")
        structural_group = cleaned.get("structural_group")
        unit = cleaned.get("property_unit")
        primary = cleaned.get("primary_client")
        if unit and project and unit.project_id != project.pk:
            raise ValidationError("La unidad seleccionada no pertenece al proyecto indicado.")
        if unit and not structural_group:
            raise ValidationError("Seleccione la agrupacion o la opcion de unidades directas del proyecto antes de elegir la unidad.")
        if unit and structural_group == DIRECT_UNITS_VALUE and unit.structural_group_id is not None:
            raise ValidationError("La unidad seleccionada no es una unidad directa del proyecto.")
        if unit and structural_group and structural_group != DIRECT_UNITS_VALUE:
            if str(unit.structural_group_id) != str(structural_group):
                raise ValidationError("La unidad seleccionada no pertenece a la agrupacion indicada.")
        if unit and grouping_type and unit.structural_group_id:
            if unit.structural_group.grouping_type_id != grouping_type.pk:
                raise ValidationError("La unidad seleccionada no corresponde al tipo de agrupacion indicado.")
        if unit and not UnitOwnership.objects.filter(property_unit=unit, is_active=True, end_date__isnull=True).exists():
            raise ValidationError("La unidad seleccionada no tiene titulares vigentes. Registre primero la titularidad de los clientes.")
        if unit and primary and not has_active_unit_ownership(primary, unit):
            raise ValidationError("El titular principal debe tener titularidad vigente sobre la unidad seleccionada.")
        if unit:
            try:
                validate_active_assignment_available(unit=unit, current_instance=self.instance if self.instance.pk else None)
            except ValidationError as exc:
                self.add_error("property_unit", exc.message_dict.get("property_unit", exc.messages)[0])
        return cleaned


def has_active_unit_ownership(client, unit):
    return UnitOwnership.objects.filter(
        client=client,
        property_unit=unit,
        is_active=True,
        end_date__isnull=True,
    ).exists()


def eligible_assignment_clients(unit_id):
    queryset = Client.objects.none()
    if unit_id:
        queryset = Client.objects.filter(
            is_active=True,
            unit_ownerships__property_unit_id=unit_id,
            unit_ownerships__is_active=True,
            unit_ownerships__end_date__isnull=True,
        ).distinct()
    return queryset.order_by("last_names_or_company", "first_names")


class SecondaryAssignmentHolderForm(forms.Form):
    client = forms.ModelChoiceField(
        label="Titular secundario",
        required=False,
        queryset=Client.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-holder-role": "secondary"}),
    )
    DELETE = forms.BooleanField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, eligible_clients=None, **kwargs):
        super().__init__(*args, **kwargs)
        if self.is_bound and self.data.get(f"{self.prefix}-DELETE") == "on":
            self.fields["client"].queryset = Client.objects.all()
        else:
            self.fields["client"].queryset = eligible_clients or Client.objects.none()
        if not self.fields["client"].queryset.exists():
            self.fields["client"].widget.attrs["disabled"] = "disabled"


SecondaryAssignmentHolderFormSet = formset_factory(
    SecondaryAssignmentHolderForm,
    extra=1,
)


def validate_assignment_holder_formset(formset, unit, primary_client):
    if not unit:
        raise ValidationError("Seleccione una unidad antes de registrar titulares.")
    if not primary_client:
        raise ValidationError("Debe seleccionar exactamente un titular principal.")
    if not has_active_unit_ownership(primary_client, unit):
        raise ValidationError("El titular principal debe tener titularidad vigente sobre la unidad seleccionada.")

    secondary_clients = []
    for form in formset.forms:
        if not hasattr(form, "cleaned_data"):
            continue
        if form.cleaned_data.get("DELETE"):
            continue
        client = form.cleaned_data.get("client")
        if not client:
            continue
        if client == primary_client:
            raise ValidationError("El titular principal no debe repetirse como secundario.")
        if client in secondary_clients:
            raise ValidationError("No puede seleccionar el mismo titular secundario mas de una vez.")
        if not has_active_unit_ownership(client, unit):
            raise ValidationError("Todos los titulares del encargo deben tener titularidad vigente sobre la unidad.")
        secondary_clients.append(client)
    return secondary_clients


class FiduciaryAssignmentUpdateForm(ChangeReasonMixin, forms.ModelForm):
    class Meta:
        model = FiduciaryAssignment
        fields = (
            "adhesion_contract_date",
            "promise_date",
            "promised_delivery_date",
            "actual_delivery_date",
        )
        widgets = {
            "adhesion_contract_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "promise_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "promised_delivery_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "actual_delivery_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        }

    def save(self, commit=True):
        instance = super().save(commit=False)
        self.apply_reason(instance)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class AssignmentFinancialEntityForm(forms.Form):
    financial_entity = forms.CharField(
        label="Entidad financiera",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "BANCO CAJA SOCIAL"}),
    )

    def clean_financial_entity(self):
        return " ".join((self.cleaned_data.get("financial_entity") or "").split())


class OwnershipFinalizeForm(forms.Form):
    novelty_type = forms.ChoiceField(label="Tipo de novedad", choices=NOVELTY_TYPE_CHOICES, widget=forms.Select(attrs={"class": "form-select"}))
    end_date = forms.DateField(label="Fecha efectiva", widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    reason = forms.CharField(label="Motivo u observacion", widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}))

    def clean_reason(self):
        return self.cleaned_data["reason"].strip()


class PrimaryOwnershipChangeForm(forms.Form):
    new_client = forms.ModelChoiceField(label="Nuevo titular principal", queryset=Client.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    effective_date = forms.DateField(label="Fecha efectiva", widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    novelty_type = forms.ChoiceField(label="Tipo de novedad", choices=NOVELTY_TYPE_CHOICES, widget=forms.Select(attrs={"class": "form-select"}))
    reason = forms.CharField(label="Motivo u observacion", widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}))

    def __init__(self, *args, unit=None, **kwargs):
        self.unit = unit
        super().__init__(*args, **kwargs)
        self.fields["new_client"].queryset = Client.objects.filter(is_active=True).order_by("last_names_or_company", "first_names")

    def clean_reason(self):
        return self.cleaned_data["reason"].strip()


class AssignmentChangeForm(forms.Form):
    new_assignment_number = forms.CharField(label="Nuevo numero de encargo", required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    effective_date = forms.DateField(label="Fecha efectiva", widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    novelty_type = forms.ChoiceField(label="Tipo de novedad", choices=NOVELTY_TYPE_CHOICES, widget=forms.Select(attrs={"class": "form-select"}))
    reason = forms.CharField(label="Motivo u observacion", widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}))
    other_description = forms.CharField(label="Descripcion de la novedad", required=False, widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}))
    primary_client = forms.ModelChoiceField(
        label="Titular principal del nuevo encargo",
        queryset=Client.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    secondary_clients = forms.ModelMultipleChoiceField(
        label="Clientes secundarios asociados",
        queryset=Client.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, assignment=None, **kwargs):
        self.assignment = assignment
        super().__init__(*args, **kwargs)
        if assignment:
            clients = Client.objects.filter(
                is_active=True,
                unit_ownerships__property_unit=assignment.property_unit,
                unit_ownerships__is_active=True,
            ).distinct().order_by("last_names_or_company", "first_names")
            self.fields["primary_client"].queryset = clients
            self.fields["secondary_clients"].queryset = clients
            if not self.is_bound:
                primary = assignment.holders.filter(is_active=True, is_primary=True).first()
                if primary:
                    self.initial["primary_client"] = primary.client_id
                self.initial["secondary_clients"] = list(
                    assignment.holders.filter(is_active=True, is_primary=False).order_by("pk").values_list("client_id", flat=True)
                )

    def clean_new_assignment_number(self):
        return (self.cleaned_data.get("new_assignment_number") or "").strip()

    def clean_reason(self):
        return self.cleaned_data["reason"].strip()

    def clean_other_description(self):
        return (self.cleaned_data.get("other_description") or "").strip()

    def clean(self):
        cleaned = super().clean()
        novelty_type = cleaned.get("novelty_type")
        if novelty_type == "other" and not cleaned.get("other_description"):
            self.add_error("other_description", "Describa la novedad.")
        if novelty_type not in ASSIGNMENT_CHANGE_WITHOUT_NEW_ASSIGNMENT:
            if not cleaned.get("primary_client"):
                self.add_error("primary_client", "Seleccione un titular principal.")
        secondary_clients = list(cleaned.get("secondary_clients") or [])
        primary_client = cleaned.get("primary_client")
        if primary_client and primary_client in secondary_clients:
            self.add_error("secondary_clients", "El titular principal no debe repetirse como secundario.")
        return cleaned


class AssignmentHolderForm(ChangeReasonMixin, forms.ModelForm):
    class Meta:
        model = FiduciaryAssignmentHolder
        fields = ("client", "is_primary", "start_date")
        widgets = {
            "client": forms.Select(attrs={"class": "form-select"}),
            "is_primary": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "start_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        }

    def __init__(self, *args, assignment=None, **kwargs):
        self.assignment = assignment
        super().__init__(*args, **kwargs)
        client_id = self.data.get("client") if self.is_bound else self.initial.get("client")
        if client_id:
            self.fields["client"].queryset = Client.objects.filter(is_active=True, pk=client_id)
        else:
            self.fields["client"].queryset = Client.objects.none()

    def clean(self):
        cleaned = super().clean()
        client = cleaned.get("client")
        is_primary = cleaned.get("is_primary")
        if self.assignment and client:
            duplicate = self.assignment.holders.filter(client=client, is_active=True)
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error("client", "El cliente ya es titular activo de este encargo.")
            if is_primary:
                primary = self.assignment.holders.filter(is_primary=True, is_active=True)
                if self.instance.pk:
                    primary = primary.exclude(pk=self.instance.pk)
                if primary.exists():
                    self.add_error("is_primary", "El encargo ya tiene un titular principal activo.")
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.assignment = self.assignment
        self.apply_reason(instance)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class HistoricalImportUploadForm(forms.Form):
    file = MultipleExcelFileField(
        label="Archivos historicos",
        required=False,
        widget=MultipleFileInput(attrs={"class": "form-control", "accept": ".xlsx,.xls", "multiple": True, "data-file-list": "historical-files"}),
    )
    grouping_type_hint = forms.CharField(
        label="Tipo de agrupacion sugerido",
        required=False,
        help_text="Use este campo solo cuando el formato del archivo no indique el tipo de agrupacion.",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Ej. Sector"}),
    )

    def clean_file(self):
        files = self.cleaned_data["file"]
        if not files:
            raise ValidationError("Seleccione al menos un archivo .xlsx o .xls.")
        if len(files) > MAX_IMPORT_FILES:
            raise ValidationError("Seleccione maximo 25 archivos por carga.")
        return files

    def clean_grouping_type_hint(self):
        return self.cleaned_data["grouping_type_hint"].strip()


class ImportResolutionForm(forms.ModelForm):
    target_kind = forms.ChoiceField(
        label="Clasificacion",
        choices=[
            (DetectedStructureElement.InferredKind.PROJECT, "Proyecto"),
            (DetectedStructureElement.InferredKind.GROUPING_TYPE, "Tipo de agrupacion"),
            (DetectedStructureElement.InferredKind.STRUCTURAL_GROUP, "Agrupacion"),
            (DetectedStructureElement.InferredKind.PROPERTY_UNIT, "Unidad"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    action = forms.ChoiceField(
        label="Decision",
        choices=[
            (ImportResolution.Action.ASSOCIATE_EXISTING, "Asociar con entidad existente"),
            (ImportResolution.Action.CREATE_NEW, "Crear nuevo en la importacion futura"),
            (ImportResolution.Action.IGNORE, "Ignorar"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = ImportResolution
        fields = (
            "target_kind",
            "action",
            "target_project",
            "target_grouping_type",
            "target_structural_group",
            "target_property_unit",
            "parent_project",
            "parent_grouping_type",
            "parent_structural_group",
            "create_code",
            "create_name",
        )
        widgets = {
            "target_project": forms.Select(attrs={"class": "form-select"}),
            "target_grouping_type": forms.Select(attrs={"class": "form-select"}),
            "target_structural_group": forms.Select(attrs={"class": "form-select"}),
            "target_property_unit": forms.Select(attrs={"class": "form-select"}),
            "parent_project": forms.Select(attrs={"class": "form-select"}),
            "parent_grouping_type": forms.Select(attrs={"class": "form-select"}),
            "parent_structural_group": forms.Select(attrs={"class": "form-select"}),
            "create_code": forms.TextInput(attrs={"class": "form-control"}),
            "create_name": forms.TextInput(attrs={"class": "form-control"}),
        }
        labels = {
            "target_project": "Proyecto existente",
            "target_grouping_type": "Tipo existente",
            "target_structural_group": "Agrupacion existente",
            "target_property_unit": "Unidad existente",
            "parent_project": "Proyecto padre",
            "parent_grouping_type": "Tipo padre",
            "parent_structural_group": "Agrupacion padre",
            "create_code": "Codigo para crear",
            "create_name": "Nombre para crear",
        }

    def __init__(self, *args, detected_element=None, **kwargs):
        self.detected_element = detected_element
        super().__init__(*args, **kwargs)
        self.fields["target_project"].queryset = Project.objects.order_by("name", "code")
        self.fields["target_grouping_type"].queryset = GroupingType.objects.order_by("name", "code")
        self.fields["target_structural_group"].queryset = StructuralGroup.objects.select_related(
            "project", "grouping_type"
        ).order_by("project__name", "name", "code")
        self.fields["target_property_unit"].queryset = with_natural_unit_order(
            PropertyUnit.objects.select_related("project", "structural_group")
        )
        use_property_unit_choice_labels(self.fields["target_property_unit"])
        self.fields["parent_project"].queryset = Project.objects.order_by("name", "code")
        self.fields["parent_grouping_type"].queryset = GroupingType.objects.order_by("name", "code")
        self.fields["parent_structural_group"].queryset = StructuralGroup.objects.select_related(
            "project", "grouping_type"
        ).order_by("project__name", "name", "code")
        if detected_element and detected_element.inferred_kind in {
            DetectedStructureElement.InferredKind.PROJECT,
            DetectedStructureElement.InferredKind.GROUPING_TYPE,
        }:
            self.fields["action"].choices = [
                (ImportResolution.Action.ASSOCIATE_EXISTING, "Asociar con entidad existente"),
                (ImportResolution.Action.CREATE_NEW, "Crear nuevo ahora"),
                (ImportResolution.Action.IGNORE, "Ignorar"),
            ]
        if detected_element and not self.is_bound:
            self.initial.setdefault("target_kind", detected_element.inferred_kind)
            value = detected_element.raw_value if detected_element.raw_value != "(sin valor)" else ""
            if detected_element.inferred_kind == DetectedStructureElement.InferredKind.PROPERTY_UNIT:
                self.initial.setdefault("create_code", "")
                self.initial.setdefault("create_name", value)
            else:
                self.initial.setdefault("create_code", value)
                self.initial.setdefault("create_name", value)

    def clean(self):
        cleaned = super().clean()
        action = cleaned.get("action")
        kind = cleaned.get("target_kind")
        if action == ImportResolution.Action.ASSOCIATE_EXISTING:
            required_by_kind = {
                DetectedStructureElement.InferredKind.PROJECT: "target_project",
                DetectedStructureElement.InferredKind.GROUPING_TYPE: "target_grouping_type",
                DetectedStructureElement.InferredKind.STRUCTURAL_GROUP: "target_structural_group",
                DetectedStructureElement.InferredKind.PROPERTY_UNIT: "target_property_unit",
            }
            field_name = required_by_kind.get(kind)
            if field_name and not cleaned.get(field_name):
                self.add_error(field_name, "Seleccione la entidad existente.")
        if action == ImportResolution.Action.CREATE_NEW and kind in {
            DetectedStructureElement.InferredKind.PROJECT,
            DetectedStructureElement.InferredKind.GROUPING_TYPE,
        }:
            if not (cleaned.get("create_code") or "").strip():
                self.add_error("create_code", "Registre el codigo.")
            if not (cleaned.get("create_name") or "").strip():
                self.add_error("create_name", "Registre el nombre.")
        elif action == ImportResolution.Action.CREATE_NEW and not (
            (cleaned.get("create_code") or "").strip() or (cleaned.get("create_name") or "").strip()
        ):
            raise ValidationError("Registre codigo, nombre o ambos para crear el elemento en la importacion futura.")
        if action == ImportResolution.Action.CREATE_NEW and kind == DetectedStructureElement.InferredKind.STRUCTURAL_GROUP:
            if not cleaned.get("parent_project") or not cleaned.get("parent_grouping_type"):
                raise ValidationError("Para crear una agrupacion debe indicar proyecto y tipo padre.")
        if action == ImportResolution.Action.CREATE_NEW and kind == DetectedStructureElement.InferredKind.PROPERTY_UNIT:
            if not cleaned.get("parent_project"):
                raise ValidationError("Para crear una unidad debe indicar el proyecto padre.")
        return cleaned


class StructuralGroupResolutionForm(forms.Form):
    action = forms.ChoiceField(
        label="Decision",
        choices=[
            (ImportResolution.Action.CREATE_NEW, "Crear nueva agrupacion"),
            (ImportResolution.Action.ASSOCIATE_EXISTING, "Relacionar con agrupacion existente"),
        ],
        widget=forms.Select(attrs={"class": "form-select", "data-structural-resolution": "action"}),
    )
    project = forms.ModelChoiceField(
        label="Proyecto",
        queryset=Project.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-structural-resolution": "project"}),
    )
    grouping_type = forms.ModelChoiceField(
        label="Tipo de agrupacion",
        required=False,
        queryset=GroupingType.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-structural-resolution": "grouping-type"}),
    )
    create_grouping_type = forms.BooleanField(
        label="Crear tipo de agrupacion",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input", "data-structural-resolution": "create-grouping-type"}),
    )
    new_grouping_type_code = forms.CharField(
        label="Codigo del tipo",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "ED"}),
    )
    new_grouping_type_name = forms.CharField(
        label="Nombre del tipo",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Edificacion"}),
    )
    existing_group = forms.ModelChoiceField(
        label="Agrupacion existente",
        required=False,
        queryset=StructuralGroup.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-structural-resolution": "existing-group"}),
    )
    new_group_name = forms.CharField(
        label="Nombre de la nueva agrupacion",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, detected_element=None, **kwargs):
        self.detected_element = detected_element
        super().__init__(*args, **kwargs)
        context = detected_element.structural_context if detected_element else {}
        project_id = self.data.get("project") if self.is_bound else context.get("project_id")
        grouping_type_id = self.data.get("grouping_type") if self.is_bound else context.get("grouping_type_id")
        self.fields["project"].queryset = Project.objects.filter(is_active=True).order_by("name", "code")
        self.fields["grouping_type"].queryset = GroupingType.objects.filter(is_active=True).order_by("name", "code")
        groups = StructuralGroup.objects.filter(is_active=True).select_related("project", "grouping_type")
        if project_id:
            groups = groups.filter(project_id=project_id)
            self.initial.setdefault("project", project_id)
        else:
            groups = groups.none()
        if grouping_type_id:
            groups = groups.filter(grouping_type_id=grouping_type_id)
            self.initial.setdefault("grouping_type", grouping_type_id)
        else:
            groups = groups.none()
        self.fields["existing_group"].queryset = groups.order_by("project__name", "grouping_type__name", "name", "code")
        if detected_element and not self.is_bound:
            value = detected_element.raw_value if detected_element.raw_value != "(sin valor)" else ""
            self.initial.setdefault("new_group_name", value)
            self.initial.setdefault("action", ImportResolution.Action.CREATE_NEW)

    def clean_new_group_name(self):
        return self.cleaned_data["new_group_name"].strip()

    def clean(self):
        cleaned = super().clean()
        action = cleaned.get("action")
        project = cleaned.get("project")
        grouping_type = cleaned.get("grouping_type")
        create_grouping_type = cleaned.get("create_grouping_type")
        new_grouping_type_code = (cleaned.get("new_grouping_type_code") or "").strip()
        new_grouping_type_name = (cleaned.get("new_grouping_type_name") or "").strip()
        existing_group = cleaned.get("existing_group")
        new_group_name = cleaned.get("new_group_name")
        if create_grouping_type:
            if not new_grouping_type_code:
                self.add_error("new_grouping_type_code", "Registre el codigo del tipo.")
            if not new_grouping_type_name:
                self.add_error("new_grouping_type_name", "Registre el nombre del tipo.")
            if action == ImportResolution.Action.ASSOCIATE_EXISTING:
                self.add_error("action", "Para crear un tipo nuevo debe crear tambien la agrupacion detectada.")
        elif not grouping_type:
            self.add_error("grouping_type", "Seleccione el tipo de agrupacion.")
        if action == ImportResolution.Action.ASSOCIATE_EXISTING:
            if not existing_group:
                self.add_error("existing_group", "Seleccione la agrupacion existente.")
            elif project and grouping_type and (
                existing_group.project_id != project.pk or existing_group.grouping_type_id != grouping_type.pk
            ):
                self.add_error("existing_group", "La agrupacion no pertenece al proyecto y tipo seleccionados.")
        if action == ImportResolution.Action.CREATE_NEW and not new_group_name:
            self.add_error("new_group_name", "Registre el nombre de la agrupacion.")
        return cleaned


class DailyReportUploadForm(forms.Form):
    file = MultipleExcelFileField(
        label="Reportes diarios",
        required=False,
        widget=MultipleFileInput(attrs={"class": "form-control", "accept": ".xlsx,.xls", "multiple": True, "data-file-list": "daily-files"}),
    )

    def clean_file(self):
        files = self.cleaned_data["file"]
        if not files:
            raise ValidationError("Seleccione al menos un archivo Excel .xlsx o .xls.")
        if len(files) > MAX_IMPORT_FILES:
            raise ValidationError("Seleccione maximo 25 archivos por carga.")
        return files


class DailyReportAssignmentResolutionForm(forms.ModelForm):
    payment_destination = forms.ChoiceField(
        label="Recibido por",
        required=False,
        choices=[(Payment.Destination.FIDUCIARIA, Payment.Destination.FIDUCIARIA.label)],
        initial=Payment.Destination.FIDUCIARIA,
        disabled=True,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    financial_entity = forms.CharField(
        label="Entidad financiera",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    class Meta:
        model = DailyReportRow
        fields = ("assignment", "payment_destination", "resolution_note")
        widgets = {
            "assignment": forms.Select(attrs={"class": "form-select"}),
            "resolution_note": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }
        labels = {
            "assignment": "Encargo fiduciario",
            "resolution_note": "Nota",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignment"].queryset = FiduciaryAssignment.objects.select_related(
            "property_unit", "property_unit__project"
        ).order_by("assignment_number")
        self.fields["assignment"].required = False
        assignment = self.instance.assignment if self.instance and self.instance.assignment_id else None
        if assignment and not self.is_bound:
            self.fields["financial_entity"].initial = assignment.property_unit.financial_entity or ""

    def clean_payment_destination(self):
        return Payment.Destination.FIDUCIARIA

    def clean_resolution_note(self):
        return self.cleaned_data["resolution_note"].strip()

    def clean_financial_entity(self):
        return " ".join((self.cleaned_data.get("financial_entity") or "").split())
