from django import forms

from core.models import BackupSettings


class BackupUploadForm(forms.Form):
    file = forms.FileField(
        label="Archivo ZIP",
        widget=forms.ClearableFileInput(attrs={"class": "form-control", "accept": ".zip,application/zip"}),
    )

    def clean_file(self):
        file = self.cleaned_data["file"]
        if not file.name.lower().endswith(".zip"):
            raise forms.ValidationError("Debe seleccionar un archivo ZIP.")
        return file


class BackupSettingsForm(forms.ModelForm):
    class Meta:
        model = BackupSettings
        fields = ["daily_check_time"]
        widgets = {
            "daily_check_time": forms.TimeInput(attrs={"class": "form-control", "type": "time"}),
        }
        labels = {
            "daily_check_time": "Hora diaria",
        }
