from django import forms
from django.core.exceptions import ValidationError

from .models import GroupingType, Project, PropertyUnit, StructuralGroup


class SearchForm(forms.Form):
    q = forms.CharField(
        label="Buscar",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Codigo o nombre"}),
    )
    status = forms.ChoiceField(
        label="Estado",
        required=False,
        choices=[("", "Todos"), ("active", "Activos"), ("inactive", "Inactivos")],
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def clean_q(self):
        return self.cleaned_data["q"].strip()


class StructuralGroupFilterForm(SearchForm):
    project = forms.ModelChoiceField(
        label="Proyecto",
        required=False,
        queryset=Project.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    grouping_type = forms.ModelChoiceField(
        label="Tipo de agrupacion",
        required=False,
        queryset=GroupingType.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    parent = forms.ModelChoiceField(
        label="Agrupacion padre",
        required=False,
        queryset=StructuralGroup.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.order_by("name")
        self.fields["grouping_type"].queryset = GroupingType.objects.order_by("name")
        parent_queryset = StructuralGroup.objects.select_related("project", "grouping_type").order_by(
            "project__name", "name", "code"
        )
        project_id = self.data.get("project") if self.is_bound else None
        if project_id:
            parent_queryset = parent_queryset.filter(project_id=project_id)
        self.fields["parent"].queryset = parent_queryset


class PropertyUnitFilterForm(SearchForm):
    DIRECT_VALUE = "__direct__"

    project = forms.ModelChoiceField(
        label="Proyecto",
        required=False,
        queryset=Project.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    grouping_type = forms.ModelChoiceField(
        label="Tipo de agrupacion",
        required=False,
        queryset=GroupingType.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    structural_group = forms.ChoiceField(
        label="Agrupacion",
        required=False,
        choices=(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.order_by("name")
        self.fields["grouping_type"].queryset = GroupingType.objects.order_by("name")
        groups = StructuralGroup.objects.select_related("project", "grouping_type").order_by(
            "project__name", "name", "code"
        )
        project_id = self.data.get("project") if self.is_bound else None
        grouping_type_id = self.data.get("grouping_type") if self.is_bound else None
        if project_id:
            groups = groups.filter(project_id=project_id)
        if grouping_type_id:
            groups = groups.filter(grouping_type_id=grouping_type_id)
        choices = [("", "Seleccione una agrupacion"), (self.DIRECT_VALUE, "Directamente al proyecto")]
        choices.extend((str(group.pk), str(group)) for group in groups)
        self.fields["structural_group"].choices = choices

    def clean_structural_group(self):
        value = self.cleaned_data["structural_group"]
        if value in {"", self.DIRECT_VALUE}:
            return value
        try:
            return StructuralGroup.objects.get(pk=value)
        except StructuralGroup.DoesNotExist as exc:
            raise ValidationError("Seleccione una agrupacion valida.") from exc


class ChangeReasonMixin(forms.Form):
    change_reason = forms.CharField(
        label="Motivo de la modificacion",
        required=True,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.last_change_reason = self.cleaned_data["change_reason"].strip()
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class BaseEntityForm(forms.ModelForm):
    class Meta:
        fields = ("code", "name", "description", "is_active")
        widgets = {
            "code": forms.TextInput(attrs={"class": "form-control"}),
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        labels = {
            "code": "Codigo",
            "name": "Nombre",
            "description": "Descripcion",
            "is_active": "Activo",
        }

    def clean_code(self):
        return self.cleaned_data["code"].strip()

    def clean_name(self):
        return self.cleaned_data["name"].strip()


class ProjectForm(BaseEntityForm):
    class Meta(BaseEntityForm.Meta):
        model = Project


class ProjectUpdateForm(ChangeReasonMixin, ProjectForm):
    pass


class GroupingTypeForm(BaseEntityForm):
    class Meta(BaseEntityForm.Meta):
        model = GroupingType


class GroupingTypeUpdateForm(ChangeReasonMixin, GroupingTypeForm):
    pass


class StructuralGroupForm(BaseEntityForm):
    class Meta(BaseEntityForm.Meta):
        model = StructuralGroup
        fields = ("project", "grouping_type", "parent") + BaseEntityForm.Meta.fields
        widgets = {
            "project": forms.Select(attrs={"class": "form-select"}),
            "grouping_type": forms.Select(attrs={"class": "form-select"}),
            "parent": forms.Select(attrs={"class": "form-select"}),
            **BaseEntityForm.Meta.widgets,
        }
        labels = {
            "project": "Proyecto",
            "grouping_type": "Tipo",
            "parent": "Agrupacion padre",
            **BaseEntityForm.Meta.labels,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].required = False
        self.fields["name"].required = False
        self.fields["parent"].required = False
        queryset = StructuralGroup.objects.select_related("project", "grouping_type").order_by("project__name", "name")
        if self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = queryset

    def clean(self):
        cleaned_data = super().clean()
        project = cleaned_data.get("project")
        parent = cleaned_data.get("parent")
        code = cleaned_data.get("code")
        name = cleaned_data.get("name")
        if not code and not name:
            raise ValidationError("Debe registrar codigo, nombre o ambos.")
        if project and parent and parent.project_id != project.id:
            self.add_error("parent", "La agrupacion padre debe pertenecer al mismo proyecto.")
        if project and code:
            queryset = StructuralGroup.objects.filter(project=project, code=code)
            if self.instance.pk:
                queryset = queryset.exclude(pk=self.instance.pk)
            if parent:
                duplicate = queryset.filter(parent=parent).exists()
            else:
                duplicate = queryset.filter(parent__isnull=True).exists()
            if duplicate:
                raise ValidationError("Ya existe una agrupacion con este codigo en el mismo nivel.")
        return cleaned_data


class StructuralGroupUpdateForm(ChangeReasonMixin, StructuralGroupForm):
    pass


class PropertyUnitForm(BaseEntityForm):
    class Meta(BaseEntityForm.Meta):
        model = PropertyUnit
        fields = ("project", "structural_group") + BaseEntityForm.Meta.fields
        widgets = {
            "project": forms.Select(attrs={"class": "form-select"}),
            "structural_group": forms.Select(attrs={"class": "form-select"}),
            **BaseEntityForm.Meta.widgets,
        }
        labels = {
            "project": "Proyecto",
            "structural_group": "Agrupacion padre",
            **BaseEntityForm.Meta.labels,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].required = False
        self.fields["name"].required = False
        self.fields["structural_group"].required = False
        self.fields["structural_group"].queryset = StructuralGroup.objects.select_related("project").order_by(
            "project__name", "name"
        )

    def clean(self):
        cleaned_data = super().clean()
        project = cleaned_data.get("project")
        structural_group = cleaned_data.get("structural_group")
        code = cleaned_data.get("code")
        name = cleaned_data.get("name")
        if not code and not name:
            raise ValidationError("Debe registrar codigo, nombre o ambos.")
        if project and structural_group and structural_group.project_id != project.id:
            self.add_error("structural_group", "La agrupacion debe pertenecer al mismo proyecto.")
        if project and code:
            queryset = PropertyUnit.objects.filter(project=project, code=code)
            if self.instance.pk:
                queryset = queryset.exclude(pk=self.instance.pk)
            if structural_group:
                duplicate = queryset.filter(structural_group=structural_group).exists()
            else:
                duplicate = queryset.filter(structural_group__isnull=True).exists()
            if duplicate:
                raise ValidationError("Ya existe una unidad con este codigo en el mismo nivel.")
        return cleaned_data


class PropertyUnitUpdateForm(ChangeReasonMixin, PropertyUnitForm):
    pass
