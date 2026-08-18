from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from django.conf import settings


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"


class DriveBackupError(RuntimeError):
    pass


class DriveNotConfigured(DriveBackupError):
    pass


class DriveUnavailable(DriveBackupError):
    pass


@dataclass(frozen=True)
class DriveRemoteFile:
    file_id: str
    checksum_sha256: str


class GoogleDriveBackupClient:
    def __init__(self):
        self._service = None
        self._folder_id = None

    @property
    def configured(self):
        return bool(settings.GOOGLE_DRIVE_BACKUP_ENABLED and settings.GOOGLE_DRIVE_TOKEN_FILE)

    def ensure_ready(self):
        if not self.configured:
            raise DriveNotConfigured("Google Drive no está configurado para copias externas.")
        self._folder_id = self._folder_id or settings.GOOGLE_DRIVE_BACKUP_FOLDER_ID or self._ensure_folder()
        return self._folder_id

    def has_connectivity(self):
        if not self.configured:
            raise DriveNotConfigured("Google Drive no está configurado para copias externas.")
        try:
            self._service_obj().about().get(fields="user").execute(num_retries=0)
        except DriveBackupError:
            raise
        except Exception as exc:
            raise DriveUnavailable("No fue posible comunicarse con Google Drive.") from exc
        return True

    def find_backup(self, backup_id, checksum):
        folder_id = self.ensure_ready()
        query = (
            "trashed = false and "
            f"'{_escape_drive_query(folder_id)}' in parents and "
            f"appProperties has {{ key='backup_id' and value='{_escape_drive_query(backup_id)}' }}"
        )
        try:
            response = (
                self._service_obj()
                .files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="files(id,name,appProperties)",
                    pageSize=10,
                )
                .execute(num_retries=0)
            )
        except Exception as exc:
            raise DriveUnavailable("No fue posible consultar Google Drive.") from exc
        files = response.get("files", [])
        for item in files:
            props = item.get("appProperties") or {}
            if props.get("checksum_sha256") == checksum:
                return DriveRemoteFile(file_id=item["id"], checksum_sha256=checksum)
        if files:
            raise DriveBackupError("Existe una copia remota con el mismo identificador pero checksum diferente.")
        return None

    def upload_backup(self, *, file_path, file_name, backup_id, checksum):
        folder_id = self.ensure_ready()
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise DriveNotConfigured("Las dependencias oficiales de Google Drive no están instaladas.") from exc
        media = MediaFileUpload(str(file_path), mimetype="application/zip", resumable=False)
        body = {
            "name": file_name,
            "parents": [folder_id],
            "appProperties": {
                "backup_id": str(backup_id),
                "checksum_sha256": checksum,
                "application": "Gestion Fiduciaria",
            },
        }
        try:
            response = (
                self._service_obj()
                .files()
                .create(body=body, media_body=media, fields="id,appProperties")
                .execute(num_retries=0)
            )
        except Exception as exc:
            raise DriveUnavailable("No fue posible subir la copia de seguridad a Google Drive.") from exc
        props = response.get("appProperties") or {}
        if props.get("backup_id") != str(backup_id) or props.get("checksum_sha256") != checksum:
            raise DriveBackupError("Google Drive no confirmó los metadatos esperados del respaldo.")
        return DriveRemoteFile(file_id=response["id"], checksum_sha256=checksum)

    def delete_file(self, file_id):
        if not file_id:
            return
        self.ensure_ready()
        try:
            self._service_obj().files().delete(fileId=file_id).execute(num_retries=0)
        except Exception as exc:
            raise DriveUnavailable("No fue posible eliminar la copia remota en Google Drive.") from exc

    def _ensure_folder(self):
        service = self._service_obj()
        name = settings.GOOGLE_DRIVE_BACKUP_FOLDER_NAME
        query = (
            "trashed = false and mimeType = 'application/vnd.google-apps.folder' and "
            f"name = '{_escape_drive_query(name)}'"
        )
        try:
            response = (
                service.files()
                .list(q=query, spaces="drive", fields="files(id,name)", pageSize=1)
                .execute(num_retries=0)
            )
            files = response.get("files", [])
            if files:
                return files[0]["id"]
            created = (
                service.files()
                .create(
                    body={"name": name, "mimeType": "application/vnd.google-apps.folder"},
                    fields="id",
                )
                .execute(num_retries=0)
            )
        except Exception as exc:
            raise DriveUnavailable("No fue posible preparar la carpeta de Google Drive.") from exc
        return created["id"]

    def _service_obj(self):
        if self._service is not None:
            return self._service
        try:
            import google_auth_httplib2
            import httplib2
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise DriveNotConfigured("Las dependencias oficiales de Google Drive no están instaladas.") from exc

        token_path = Path(settings.GOOGLE_DRIVE_TOKEN_FILE)
        if not token_path.exists():
            raise DriveNotConfigured("El archivo de autorización de Google Drive no existe.")
        credentials = Credentials.from_authorized_user_file(str(token_path), scopes=[DRIVE_SCOPE])
        http = google_auth_httplib2.AuthorizedHttp(
            credentials,
            http=httplib2.Http(timeout=settings.GOOGLE_DRIVE_TIMEOUT_SECONDS),
        )
        self._service = build("drive", "v3", http=http, cache_discovery=False)
        return self._service


def _escape_drive_query(value):
    return str(value).replace("\\", "\\\\").replace("'", "\\'")
