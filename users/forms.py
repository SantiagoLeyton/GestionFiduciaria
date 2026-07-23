from django import forms
from django.contrib.auth import authenticate


class LoginForm(forms.Form):
    username = forms.CharField(
        label="Usuario o correo electronico",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "username",
                "placeholder": "ejemplo@centenario.com",
            }
        ),
    )
    password = forms.CharField(
        label="Contrasena",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "autocomplete": "current-password",
                "placeholder": "Ingrese su contrasena",
            }
        ),
    )
    remember_me = forms.BooleanField(
        label="Mantener sesion iniciada",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    error_messages = {
        "invalid_login": "Usuario, correo o contrasena invalidos.",
        "inactive": "La cuenta se encuentra inactiva. Contacte al administrador.",
    }

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        username = cleaned_data.get("username")
        password = cleaned_data.get("password")

        if username and password:
            self.user_cache = authenticate(self.request, username=username, password=password)
            if self.user_cache is None:
                raise forms.ValidationError(self.error_messages["invalid_login"], code="invalid_login")
            if not self.user_cache.is_active:
                raise forms.ValidationError(self.error_messages["inactive"], code="inactive")
        return cleaned_data

    def clean_username(self):
        return self.cleaned_data["username"].strip()

    def get_user(self):
        return self.user_cache
