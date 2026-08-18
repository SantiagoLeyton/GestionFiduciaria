from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError


User = get_user_model()


ROLE_FILTER_CHOICES = [
    ("", "Todos los roles"),
    (User.Role.COMMERCIAL, "Comercial"),
    (User.Role.ACCOUNTING_ADMIN, "Contabilidad"),
]


class UserSearchForm(forms.Form):
    q = forms.CharField(
        label="Busqueda de usuarios",
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Nombre, usuario o correo electronico",
            }
        ),
    )
    role = forms.ChoiceField(
        label="Rol del sistema",
        required=False,
        choices=ROLE_FILTER_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def clean_q(self):
        return self.cleaned_data["q"].strip()
    status = forms.ChoiceField(
        label="Estado",
        required=False,
        choices=[("", "Todos"), ("active", "Activos"), ("inactive", "Inactivos"), ("deleted", "Eliminados")],
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class BaseManagedUserForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "role", "is_active")
        widgets = {
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "role": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        labels = {
            "first_name": "Nombres",
            "last_name": "Apellidos",
            "email": "Correo electronico",
            "role": "Rol",
            "is_active": "Usuario activo",
        }

    def __init__(self, *args, actor=None, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        if self.actor and self.instance.pk and self.instance.pk == self.actor.pk:
            self.fields["role"].disabled = True
            self.fields["is_active"].disabled = True

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        queryset = User.objects.filter(email__iexact=email)
        if self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError("Ya existe un usuario con este correo electronico.")
        return email

    def clean_role(self):
        role = self.cleaned_data["role"]
        valid_roles = {choice[0] for choice in User.Role.choices}
        if role not in valid_roles:
            raise ValidationError("Seleccione un rol valido.")
        return role


class ManagedUserCreateForm(BaseManagedUserForm):
    pass


class ManagedUserUpdateForm(BaseManagedUserForm):
    def clean(self):
        cleaned_data = super().clean()
        if self.actor and self.instance.pk == self.actor.pk:
            original = User.objects.get(pk=self.instance.pk)
            cleaned_data["role"] = original.role
            cleaned_data["is_active"] = original.is_active
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.actor and self.instance.pk == self.actor.pk:
            original = User.objects.get(pk=self.instance.pk)
            user.role = original.role
            user.is_active = original.is_active
        if commit:
            user.save()
            self.save_m2m()
        return user
