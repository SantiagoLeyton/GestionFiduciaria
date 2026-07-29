from django import forms
from django.core.exceptions import ValidationError
from django.forms import formset_factory

from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup

from .domain_services import ASSIGNMENT_CHANGE_WITHOUT_NEW_ASSIGNMENT, NOVELTY_TYPE_CHOICES, validate_active_assignment_available, validate_unit_primary_available
from .models import (
    Client,
    DailyReportRow,
    DetectedStructureElement,
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportResolution,
    UnitOwnership,
)


DIRECT_UNITS_VALUE = "__direct__"
MAX_IMPORT_FILES = 25
MAX_IMPORT_FILE_SIZE_BYTES = 25 * 1024 * 1024


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
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    property_unit = forms.ModelChoiceField(
        label="Unidad",
        required=False,
        queryset=PropertyUnit.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.order_by("name")
        units = PropertyUnit.objects.select_related("project").order_by("project__name", "name", "code")
        project_id = self.data.get("project") if self.is_bound else None
        if project_id:
            units = units.filter(project_id=project_id)
        self.fields["property_unit"].queryset = units

    def clean_q(self):
        return self.cleaned_data["q"].strip()


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
    class Meta:
        model = UnitOwnership
        fields = ("client", "property_unit", "start_date")
        widgets = {
            "client": forms.Select(attrs={"class": "form-select"}),
            "property_unit": forms.Select(attrs={"class": "form-select"}),
            "start_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        client_id = self.data.get("client") if self.is_bound else self.initial.get("client")
        if client_id:
            self.fields["client"].queryset = Client.objects.filter(is_active=True, pk=client_id)
        else:
            self.fields["client"].queryset = Client.objects.none()
        self.fields["property_unit"].queryset = PropertyUnit.objects.filter(is_active=True).order_by(
            "project__name", "name", "code"
        )

    def clean(self):
        cleaned = super().clean()
        client = cleaned.get("client")
        unit = cleaned.get("property_unit")
        if unit:
            try:
                validate_unit_primary_available(unit=unit, current_instance=self.instance if self.instance.pk else None)
            except ValidationError as exc:
                self.add_error("property_unit", exc.message_dict.get("is_primary", exc.messages)[0])
        if client and unit:
            duplicate = UnitOwnership.objects.filter(client=client, property_unit=unit, is_active=True)
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error("client", "El cliente ya tiene una titularidad vigente sobre esta unidad.")
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
        self.fields["project"].queryset = Project.objects.order_by("name")
        self.fields["grouping_type"].queryset = GroupingType.objects.order_by("name")
        groups = StructuralGroup.objects.order_by("project__name", "name", "code")
        units = PropertyUnit.objects.order_by("project__name", "name", "code")
        if project_id:
            groups = groups.filter(project_id=project_id)
            units = units.filter(project_id=project_id)
        if grouping_type_id:
            groups = groups.filter(grouping_type_id=grouping_type_id)
            units = units.filter(structural_group__grouping_type_id=grouping_type_id)
        if group_id:
            units = units.filter(structural_group_id=group_id)
        self.fields["structural_group"].queryset = groups
        self.fields["property_unit"].queryset = units
        self.fields["client"].queryset = Client.objects.order_by("last_names_or_company", "first_names")

    def clean_q(self):
        return self.cleaned_data["q"].strip()


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
        self.fields["property_unit"].queryset = units.order_by("project__name", "name", "code")
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
        fields = ("assignment_number", "property_unit", "start_date", "observations", "is_active")
        widgets = {
            "assignment_number": forms.TextInput(attrs={"class": "form-control"}),
            "property_unit": forms.Select(attrs={"class": "form-select"}),
            "start_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "observations": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean(self):
        cleaned = super().clean()
        unit = cleaned.get("property_unit")
        if unit and cleaned.get("is_active"):
            try:
                validate_active_assignment_available(unit=unit, current_instance=self.instance)
            except ValidationError as exc:
                self.add_error("property_unit", exc.message_dict.get("property_unit", exc.messages)[0])
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        self.apply_reason(instance)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


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
        self.fields["target_property_unit"].queryset = PropertyUnit.objects.select_related(
            "project", "structural_group"
        ).order_by("project__name", "structural_group__name", "name", "code")
        self.fields["parent_project"].queryset = Project.objects.order_by("name", "code")
        self.fields["parent_grouping_type"].queryset = GroupingType.objects.order_by("name", "code")
        self.fields["parent_structural_group"].queryset = StructuralGroup.objects.select_related(
            "project", "grouping_type"
        ).order_by("project__name", "name", "code")
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
        if action == ImportResolution.Action.CREATE_NEW and not (
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
        queryset=GroupingType.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-structural-resolution": "grouping-type"}),
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
        existing_group = cleaned.get("existing_group")
        new_group_name = cleaned.get("new_group_name")
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
    class Meta:
        model = DailyReportRow
        fields = ("assignment", "resolution_note")
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

    def clean_resolution_note(self):
        return self.cleaned_data["resolution_note"].strip()
