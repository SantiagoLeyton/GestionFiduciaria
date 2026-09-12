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
    backup_id: str = ""
    name: str = ""
    created_time: str = ""
    size: int | None = None
    backup_type: str = ""


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
                .list(q=query, spaces="drive", fields="files(id,name,appProperties,createdTime,size)", pageSize=10)
                .execute(num_retries=0)
            )
        except Exception as exc:
            raise DriveUnavailable("No fue posible consultar Google Drive.") from exc
        files = response.get("files", [])
        for item in files:
            props = item.get("appProperties") or {}
            if props.get("checksum_sha256") == checksum:
                return _remote_file_from_item(item)
        if files:
            raise DriveBackupError("Existe una copia remota con el mismo identificador pero checksum diferente.")
        return None

    def upload_backup(self, *, file_path, file_name, backup_id, checksum, backup_type="ordinary"):
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
                "backup_type": str(backup_type),
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
        return DriveRemoteFile(
            file_id=response["id"],
            checksum_sha256=checksum,
            backup_id=str(backup_id),
            name=file_name,
            backup_type=str(backup_type),
        )

    def list_managed_backups(self):
        folder_id = self.ensure_ready()
        query = (
            "trashed = false and "
            f"'{_escape_drive_query(folder_id)}' in parents and "
            "mimeType != 'application/vnd.google-apps.folder'"
        )
        files = []
        page_token = None
        try:
            while True:
                response = (
                    self._service_obj()
                    .files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="nextPageToken,files(id,name,createdTime,size,mimeType,appProperties)",
                        pageSize=100,
                        pageToken=page_token,
                    )
                    .execute(num_retries=0)
                )
                files.extend(_remote_file_from_item(item) for item in response.get("files", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        except Exception as exc:
            raise DriveUnavailable("No fue posible listar copias de seguridad en Google Drive.") from exc
        return [
            item
            for item in files
            if _is_managed_ordinary_backup(item)
        ]

    def get_file_metadata(self, file_id):
        if not file_id:
            raise DriveBackupError("No hay ID de archivo remoto para consultar.")
        self.ensure_ready()
        try:
            item = (
                self._service_obj()
                .files()
                .get(fileId=file_id, fields="id,name,createdTime,size,appProperties,trashed")
                .execute(num_retries=0)
            )
        except Exception as exc:
            raise DriveUnavailable("No fue posible consultar la copia remota en Google Drive.") from exc
        if item.get("trashed"):
            raise DriveBackupError("La copia remota en Google Drive está en la papelera.")
        return _remote_file_from_item(item)

    def download_file(self, file_id, destination):
        if not file_id:
            raise DriveBackupError("No hay ID de archivo remoto para descargar.")
        self.ensure_ready()
        destination = Path(destination)
        try:
            from googleapiclient.http import MediaIoBaseDownload
        except ImportError as exc:
            raise DriveNotConfigured("Las dependencias oficiales de Google Drive no están instaladas.") from exc
        try:
            request = self._service_obj().files().get_media(fileId=file_id)
            with destination.open("wb") as file_obj:
                downloader = MediaIoBaseDownload(file_obj, request)
                done = False
                while not done:
                    _status, done = downloader.next_chunk(num_retries=0)
                file_obj.flush()
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise DriveUnavailable("No fue posible descargar la copia remota desde Google Drive.") from exc
        return destination

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


def _remote_file_from_item(item):
    props = item.get("appProperties") or {}
    size = item.get("size")
    try:
        size = int(size) if size not in {None, ""} else None
    except (TypeError, ValueError):
        size = None
    return DriveRemoteFile(
        file_id=item["id"],
        checksum_sha256=props.get("checksum_sha256", ""),
        backup_id=props.get("backup_id", ""),
        name=item.get("name", ""),
        created_time=item.get("createdTime", ""),
        size=size,
        backup_type=props.get("backup_type", ""),
    )


def _is_managed_ordinary_backup(item):
    if not item.backup_id or not item.checksum_sha256:
        return False
    if item.backup_type == "pre_restore":
        return False
    return item.backup_type in {"", "ordinary", "automatic", "manual"}
