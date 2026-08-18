from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import DetailView, ListView, TemplateView, View

from core.forms import BackupSettingsForm, BackupUploadForm
from core.models import BackupRecord, BackupSettings
from core.services.backups import (
    BackupError,
    active_pre_restore,
    create_backup,
    get_downloadable_backup_path,
    import_external_backup,
    record_backup_failure,
    restore_backup,
    revert_last_restore,
    sync_backup_to_drive,
    validate_backup_record,
)
from users.permissions import user_can_manage_users


class HomeView(LoginRequiredMixin, TemplateView):
    template_name = "core/home.html"


class AccountingOnlyMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if not user_can_manage_users(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class BackupListView(AccountingOnlyMixin, ListView):
    model = BackupRecord
    template_name = "core/backup_list.html"
    context_object_name = "backups"
    paginate_by = 10

    def get_queryset(self):
        return BackupRecord.objects.select_related("created_by").order_by("-created_at", "-pk")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_pre_restore"] = active_pre_restore()
        backup_settings = BackupSettings.get_solo()
        context["backup_settings"] = backup_settings
        context["settings_form"] = BackupSettingsForm(instance=backup_settings)
        context["available_backup_ids"] = {
            backup.pk for backup in context["backups"] if validate_backup_record(backup).valid and backup.is_user_downloadable
        }
        return context


class BackupCreateConfirmView(AccountingOnlyMixin, TemplateView):
    template_name = "core/backup_confirm.html"


class BackupCreateView(AccountingOnlyMixin, View):
    success_url = reverse_lazy("backup_list")

    def post(self, request):
        try:
            create_backup(backup_type=BackupRecord.BackupType.MANUAL, user=request.user)
        except BackupError as exc:
            record_backup_failure(backup_type=BackupRecord.BackupType.MANUAL, user=request.user, message=str(exc))
            messages.error(request, str(exc))
        else:
            messages.success(request, "Copia de seguridad creada y validada correctamente.")
        return redirect(self.success_url)


class BackupDriveRetryView(AccountingOnlyMixin, View):
    def post(self, request, pk):
        backup = get_object_or_404(BackupRecord, pk=pk)
        try:
            result = sync_backup_to_drive(backup, user=request.user)
        except BackupError as exc:
            messages.error(request, str(exc))
            return redirect("backup_detail", pk=backup.pk)
        if result.synced:
            messages.success(request, result.message)
        else:
            messages.warning(request, result.message)
        return redirect("backup_detail", pk=backup.pk)


class BackupSettingsUpdateView(AccountingOnlyMixin, View):
    success_url = reverse_lazy("backup_list")

    def post(self, request):
        backup_settings = BackupSettings.get_solo()
        form = BackupSettingsForm(request.POST, instance=backup_settings)
        if form.is_valid():
            form.save()
            messages.success(request, "Configuración automática actualizada correctamente.")
        else:
            messages.error(request, "Revise la hora configurada para la comprobación diaria.")
        return redirect(self.success_url)


class BackupUploadView(AccountingOnlyMixin, TemplateView):
    template_name = "core/backup_upload.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = BackupUploadForm()
        return context

    def post(self, request):
        form = BackupUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return self.render_to_response({"form": form})
        try:
            result = import_external_backup(uploaded_file=form.cleaned_data["file"], user=request.user)
        except BackupError as exc:
            messages.error(request, str(exc))
            return self.render_to_response({"form": form})
        messages.success(request, result.message)
        return redirect("backup_detail", pk=result.record.pk)


class BackupDetailView(AccountingOnlyMixin, DetailView):
    model = BackupRecord
    template_name = "core/backup_detail.html"
    context_object_name = "backup"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        validation = validate_backup_record(self.object)
        context["validation"] = validation
        context["manifest"] = validation.manifest or self.object.metadata or {}
        return context


class BackupDownloadView(AccountingOnlyMixin, View):
    def get(self, request, pk):
        backup = get_object_or_404(BackupRecord, pk=pk)
        try:
            path = get_downloadable_backup_path(backup)
        except BackupError as exc:
            messages.error(request, str(exc))
            return redirect("backup_detail", pk=backup.pk)
        return FileResponse(path.open("rb"), as_attachment=True, filename=backup.file_name)


class BackupRestoreConfirmView(AccountingOnlyMixin, DetailView):
    model = BackupRecord
    template_name = "core/backup_restore_confirm.html"
    context_object_name = "backup"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["validation"] = validate_backup_record(self.object)
        return context


class BackupRestoreView(AccountingOnlyMixin, View):
    def post(self, request, pk):
        backup = get_object_or_404(BackupRecord, pk=pk)
        try:
            restore_backup(record=backup, user=request.user)
        except BackupError as exc:
            messages.error(request, str(exc))
            return redirect("backup_restore_confirm", pk=backup.pk)
        messages.success(request, "Restauración completada correctamente. La reversión preventiva quedó disponible.")
        return redirect("backup_list")


class BackupRevertConfirmView(AccountingOnlyMixin, TemplateView):
    template_name = "core/backup_revert_confirm.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["pre_restore"] = active_pre_restore()
        return context


class BackupRevertView(AccountingOnlyMixin, View):
    def post(self, request):
        try:
            revert_last_restore(user=request.user)
        except BackupError as exc:
            messages.error(request, str(exc))
            return redirect("backup_revert_confirm")
        messages.success(request, "La restauración fue revertida correctamente.")
        return redirect("backup_list")
