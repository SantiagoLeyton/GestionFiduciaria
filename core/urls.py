from django.urls import path

from .views import (
    BackupCreateConfirmView,
    BackupCreateView,
    BackupDetailView,
    BackupDownloadView,
    BackupDriveRetryView,
    BackupListView,
    BackupRestoreConfirmView,
    BackupRestoreView,
    BackupRevertConfirmView,
    BackupRevertView,
    BackupSettingsUpdateView,
    BackupUploadView,
    HomeView,
)


urlpatterns = [
    path("backups/", BackupListView.as_view(), name="backup_list"),
    path("backups/new/", BackupCreateConfirmView.as_view(), name="backup_confirm"),
    path("backups/create/", BackupCreateView.as_view(), name="backup_create"),
    path("backups/settings/", BackupSettingsUpdateView.as_view(), name="backup_settings"),
    path("backups/upload/", BackupUploadView.as_view(), name="backup_upload"),
    path("backups/<int:pk>/", BackupDetailView.as_view(), name="backup_detail"),
    path("backups/<int:pk>/download/", BackupDownloadView.as_view(), name="backup_download"),
    path("backups/<int:pk>/drive/retry/", BackupDriveRetryView.as_view(), name="backup_drive_retry"),
    path("backups/<int:pk>/restore/", BackupRestoreConfirmView.as_view(), name="backup_restore_confirm"),
    path("backups/<int:pk>/restore/confirm/", BackupRestoreView.as_view(), name="backup_restore"),
    path("backups/revert/", BackupRevertConfirmView.as_view(), name="backup_revert_confirm"),
    path("backups/revert/confirm/", BackupRevertView.as_view(), name="backup_revert"),
    path("", HomeView.as_view(), name="home"),
]
