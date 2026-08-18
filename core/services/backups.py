import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, connection, transaction
from django.db.utils import OperationalError
from django.utils import timezone

from core.models import BackupRecord, BackupSettings
from core.services.google_drive_backups import (
    DriveBackupError,
    DriveNotConfigured,
    DriveUnavailable,
    GoogleDriveBackupClient,
)


DUMP_NAME = "pagos_fiducia.dump"
MANIFEST_NAME = "manifest.json"
CHECKSUM_NAME = "checksum.sha256"
ORDINARY_TYPES = {BackupRecord.BackupType.AUTOMATIC, BackupRecord.BackupType.MANUAL}
RESTORABLE_TYPES = {
    BackupRecord.BackupType.AUTOMATIC,
    BackupRecord.BackupType.MANUAL,
    BackupRecord.BackupType.PRE_RESTORE,
    BackupRecord.BackupType.UPLOADED,
}


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupResult:
    record: BackupRecord | None
    created: bool
    message: str


@dataclass(frozen=True)
class DriveSyncResult:
    synced: bool
    status: str
    message: str
    remote_file_id: str = ""


@dataclass(frozen=True)
class BackupValidationResult:
    valid: bool
    message: str
    manifest: dict | None = None


@dataclass(frozen=True)
class RestoreResult:
    restored: bool
    message: str
    pre_restore: BackupRecord | None = None


@dataclass(frozen=True)
class UserSnapshot:
    fields: dict
    groups: tuple[str, ...]
    permissions: tuple[tuple[str, str, str], ...]


def create_backup(
    *,
    backup_type,
    user=None,
    runner=None,
    apply_retention=True,
    cleanup_pre_restore=True,
    drive_client=None,
):
    if backup_type not in RESTORABLE_TYPES:
        raise BackupError("Tipo de respaldo no soportado en esta fase.")
    if backup_type == BackupRecord.BackupType.MANUAL and user is None:
        raise BackupError("El respaldo manual requiere usuario responsable.")

    storage_dir = _prepare_storage_dir()
    timestamp = timezone.localtime(timezone.now()).strftime("%Y-%m-%d_%H-%M-%S")
    file_name = _unique_backup_name(storage_dir, timestamp)
    final_zip = storage_dir / file_name
    backup_uid = uuid.uuid4()

    with tempfile.TemporaryDirectory(prefix="pagosfiducia_backup_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        dump_path = temp_dir / DUMP_NAME
        _run_pg_dump(dump_path, runner=runner)
        checksum = _sha256_file(dump_path)
        manifest = _build_manifest(
            backup_type=backup_type,
            dump_path=dump_path,
            checksum=checksum,
            backup_id=str(backup_uid),
        )
        checksum_text = f"{checksum}  {DUMP_NAME}\n"
        temp_zip = temp_dir / file_name
        _write_zip(temp_zip, dump_path=dump_path, manifest=manifest, checksum_text=checksum_text)
        _validate_zip(temp_zip, expected_checksum=checksum, strict=True)
        _copy_into_storage_with_inheritance(temp_zip, final_zip)
        _validate_zip(final_zip, expected_checksum=checksum, strict=True)

    with transaction.atomic():
        record = BackupRecord.objects.create(
            backup_uid=backup_uid,
            backup_type=backup_type,
            status=BackupRecord.Status.SUCCESS,
            file_path=str(final_zip),
            file_name=file_name,
            file_size=final_zip.stat().st_size,
            checksum_sha256=checksum,
            created_by=user if backup_type == BackupRecord.BackupType.MANUAL else None,
            last_change_at=current_change_marker(),
            metadata=manifest,
            message="Respaldo generado y validado correctamente.",
            drive_sync_status=_initial_drive_status(backup_type),
        )
        _audit(record, ADDITION, "Respaldo generado y validado correctamente.")

    drive_result = None
    if backup_type in ORDINARY_TYPES:
        drive_result = sync_backup_to_drive(record, client=drive_client)
    if apply_retention and backup_type in ORDINARY_TYPES and _backup_remote_copy_is_safe(record):
        apply_retention_policy(drive_client=drive_client)
    if cleanup_pre_restore and backup_type in ORDINARY_TYPES:
        clear_active_pre_restore(reason="Reversión deshabilitada por nuevo respaldo ordinario válido.")
    return BackupResult(record=record, created=True, message="Copia de seguridad creada correctamente.")


def run_automatic_backup_if_needed(*, runner=None, drive_client=None):
    settings_obj = BackupSettings.get_solo()
    settings_obj.last_auto_check_at = timezone.now()
    today = timezone.localdate()
    if BackupRecord.objects.filter(
        backup_type=BackupRecord.BackupType.AUTOMATIC,
        status=BackupRecord.Status.SUCCESS,
        created_at__date=today,
    ).exists():
        sync_pending_backups(client=drive_client)
        settings_obj.last_auto_check_result = BackupSettings.LastCheckResult.NO_CHANGES
        settings_obj.save(update_fields=["last_auto_check_at", "last_auto_check_result", "updated_at"])
        return BackupResult(record=None, created=False, message="Ya existe una copia automática para hoy.")

    latest_success = latest_successful_ordinary_backup()
    marker = current_change_marker()
    if latest_success and marker and latest_success.last_change_at and marker <= latest_success.last_change_at:
        sync_pending_backups(client=drive_client)
        settings_obj.last_auto_check_result = BackupSettings.LastCheckResult.NO_CHANGES
        settings_obj.save(update_fields=["last_auto_check_at", "last_auto_check_result", "updated_at"])
        return BackupResult(record=latest_success, created=False, message="No se detectaron cambios desde el último respaldo.")
    if latest_success and marker is None:
        sync_pending_backups(client=drive_client)
        settings_obj.last_auto_check_result = BackupSettings.LastCheckResult.NO_CHANGES
        settings_obj.save(update_fields=["last_auto_check_at", "last_auto_check_result", "updated_at"])
        return BackupResult(record=latest_success, created=False, message="No se detectaron cambios persistentes.")

    result = create_backup(backup_type=BackupRecord.BackupType.AUTOMATIC, runner=runner, drive_client=drive_client)
    settings_obj.last_auto_check_result = BackupSettings.LastCheckResult.BACKUP_CREATED
    settings_obj.save(update_fields=["last_auto_check_at", "last_auto_check_result", "updated_at"])
    return result


def record_backup_failure(*, backup_type, message, user=None):
    safe_message = str(message)[:500] or "No fue posible generar la copia de seguridad."
    try:
        with transaction.atomic():
            record = BackupRecord.objects.create(
                backup_type=backup_type,
                status=BackupRecord.Status.FAILED,
                created_by=user if backup_type == BackupRecord.BackupType.MANUAL else None,
                message=safe_message,
            )
            _audit(record, ADDITION, safe_message)
    except Exception:
        return None
    return record


def validate_backup_record(record):
    try:
        _ensure_restorable_record(record)
        zip_path = _safe_backup_file_path(record)
        manifest = _validate_zip(zip_path, expected_checksum=record.checksum_sha256, strict=True)
    except BackupError as exc:
        return BackupValidationResult(valid=False, message=str(exc))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return BackupValidationResult(valid=False, message=f"No fue posible leer el respaldo: {exc.__class__.__name__}.")
    return BackupValidationResult(valid=True, message="Integridad válida.", manifest=manifest)


def get_downloadable_backup_path(record):
    if not record.is_user_downloadable:
        raise BackupError("Este tipo de respaldo no está disponible para descarga.")
    validation = validate_backup_record(record)
    if not validation.valid:
        raise BackupError(validation.message)
    return _safe_backup_file_path(record)


def import_external_backup(*, uploaded_file, user):
    storage_dir = _prepare_storage_dir()
    timestamp = timezone.localtime(timezone.now()).strftime("%Y-%m-%d_%H-%M-%S")
    file_name = _unique_named_backup(storage_dir, f"Backup_externo_{timestamp}", ".zip")
    final_zip = storage_dir / file_name

    with tempfile.TemporaryDirectory(prefix="pagosfiducia_upload_backup_") as temp_dir_name:
        temp_zip = Path(temp_dir_name) / file_name
        with temp_zip.open("wb") as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        try:
            manifest, checksum = _validate_zip_details(temp_zip, strict=True)
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BackupError(f"No fue posible validar el ZIP cargado: {exc.__class__.__name__}.") from exc
        _copy_into_storage_with_inheritance(temp_zip, final_zip)
        try:
            final_manifest, final_checksum = _validate_zip_details(final_zip, strict=True)
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
            _safe_unlink(final_zip)
            raise BackupError(f"No fue posible validar el ZIP almacenado: {exc.__class__.__name__}.") from exc
        if final_checksum != checksum or final_manifest != manifest:
            _safe_unlink(final_zip)
            raise BackupError("La copia final almacenada no coincide con el archivo validado.")

    with transaction.atomic():
        backup_uid = _manifest_backup_uid(manifest)
        record = BackupRecord.objects.create(
            backup_uid=backup_uid,
            backup_type=BackupRecord.BackupType.UPLOADED,
            status=BackupRecord.Status.SUCCESS,
            file_path=str(final_zip),
            file_name=file_name,
            file_size=final_zip.stat().st_size,
            checksum_sha256=checksum,
            created_by=user,
            last_change_at=current_change_marker(),
            metadata={**manifest, "uploaded_original_name": Path(uploaded_file.name or "").name},
            message="Respaldo externo cargado y validado correctamente.",
            drive_sync_status=BackupRecord.DriveSyncStatus.NOT_APPLICABLE,
        )
        _audit(record, ADDITION, "Respaldo externo cargado y validado correctamente.")
    return BackupResult(record=record, created=True, message="Copia de seguridad cargada correctamente.")


def sync_backup_to_drive(record, *, client=None, attempts=3, user=None):
    if not record.requires_drive_sync or record.status != BackupRecord.Status.SUCCESS:
        _mark_drive_not_applicable(record)
        return DriveSyncResult(
            synced=False,
            status=BackupRecord.DriveSyncStatus.NOT_APPLICABLE,
            message="Este tipo de respaldo no se sincroniza con Google Drive.",
        )
    try:
        zip_path = _safe_backup_file_path(record)
    except BackupError as exc:
        _mark_drive_failed(record, str(exc), audit_user=user)
        return DriveSyncResult(False, BackupRecord.DriveSyncStatus.FAILED, str(exc))

    client = client or GoogleDriveBackupClient()
    backup_id = str(record.backup_uid)
    checksum = record.checksum_sha256
    try:
        client.has_connectivity()
    except DriveNotConfigured as exc:
        _mark_drive_pending(record, str(exc), audit_user=user)
        return DriveSyncResult(False, BackupRecord.DriveSyncStatus.PENDING, str(exc))
    except DriveUnavailable as exc:
        _mark_drive_pending(record, str(exc), audit_user=user)
        return DriveSyncResult(False, BackupRecord.DriveSyncStatus.PENDING, str(exc))
    except DriveBackupError as exc:
        _mark_drive_failed(record, str(exc), audit_user=user)
        return DriveSyncResult(False, BackupRecord.DriveSyncStatus.FAILED, str(exc))

    last_error = ""
    for _attempt in range(1, attempts + 1):
        existing = _find_drive_backup(record, client, audit_user=user)
        if existing.synced or existing.status == BackupRecord.DriveSyncStatus.FAILED:
            return existing
        _mark_drive_syncing(record)
        try:
            uploaded = client.upload_backup(
                file_path=zip_path,
                file_name=record.file_name,
                backup_id=backup_id,
                checksum=checksum,
            )
        except (DriveUnavailable, DriveBackupError) as exc:
            last_error = str(exc)
            continue
        _mark_drive_synced(record, uploaded.file_id, audit_user=user, message="Copia sincronizada con Google Drive.")
        return DriveSyncResult(True, BackupRecord.DriveSyncStatus.SYNCED, "Copia sincronizada con Google Drive.", uploaded.file_id)

    final_check = _find_drive_backup(record, client, audit_user=user)
    if final_check.synced or final_check.status == BackupRecord.DriveSyncStatus.FAILED:
        return final_check
    message = last_error or "No fue posible sincronizar la copia con Google Drive."
    _mark_drive_failed(record, message, audit_user=user)
    return DriveSyncResult(False, BackupRecord.DriveSyncStatus.FAILED, message)


def sync_pending_backups(*, client=None, user=None):
    records = BackupRecord.objects.filter(
        backup_type__in=ORDINARY_TYPES,
        status=BackupRecord.Status.SUCCESS,
        drive_sync_status__in=[
            BackupRecord.DriveSyncStatus.PENDING,
            BackupRecord.DriveSyncStatus.FAILED,
        ],
    ).order_by("created_at", "pk")
    results = [sync_backup_to_drive(record, client=client, user=user) for record in records]
    if any(result.synced for result in results):
        apply_retention_policy(drive_client=client)
    return results


def restore_backup(*, record, user, dump_runner=None, restore_runner=None, post_check=None):
    validation = validate_backup_record(record)
    if not validation.valid:
        raise BackupError(validation.message)

    current_users = _capture_current_users()
    old_pre_restore_ids = list(_active_pre_restore_queryset().values_list("pk", flat=True))
    pre_restore_result = create_backup(
        backup_type=BackupRecord.BackupType.PRE_RESTORE,
        user=None,
        runner=dump_runner,
        apply_retention=False,
        cleanup_pre_restore=False,
    )
    pre_restore = pre_restore_result.record
    pre_validation = validate_backup_record(pre_restore)
    if not pre_validation.valid:
        raise BackupError(f"No fue posible validar el respaldo preventivo: {pre_validation.message}")
    _replace_previous_pre_restores(exclude_pk=pre_restore.pk, previous_ids=old_pre_restore_ids)

    try:
        _run_pg_restore(_safe_backup_file_path(record), runner=restore_runner)
        reconciliation_message = _reconcile_current_users(current_users)
        _post_restore_check(checker=post_check)
    except BackupError as exc:
        record_backup_failure(
            backup_type=BackupRecord.BackupType.PRE_RESTORE,
            user=user,
            message=f"Restauración fallida. Preventivo conservado: {exc}",
        )
        _audit_record_message(
            user=user,
            action_flag=CHANGE,
            object_id=record.pk,
            object_repr=record.file_name,
            message=f"Restauración fallida. PRE_RESTORE conservado: {pre_restore.file_name}",
        )
        raise

    restored_pre_restore = _ensure_pre_restore_record(pre_restore)
    _audit(restored_pre_restore, ADDITION, f"Restauración ejecutada desde {record.file_name}.")
    _audit_record_message(
        user=user,
        action_flag=CHANGE,
        object_id=record.pk,
        object_repr=record.file_name,
        message=f"Restauración exitosa. PRE_RESTORE disponible: {restored_pre_restore.file_name}",
    )
    _audit_record_message(
        user=user,
        action_flag=CHANGE,
        object_id=record.pk,
        object_repr=record.file_name,
        message=reconciliation_message,
    )
    return RestoreResult(restored=True, message="Restauración completada correctamente.", pre_restore=restored_pre_restore)


def revert_last_restore(*, user, restore_runner=None, post_check=None):
    pre_restore = active_pre_restore()
    if not pre_restore:
        raise BackupError("No existe un respaldo preventivo disponible para reversión.")
    validation = validate_backup_record(pre_restore)
    if not validation.valid:
        raise BackupError(validation.message)

    _run_pg_restore(_safe_backup_file_path(pre_restore), runner=restore_runner)
    _post_restore_check(checker=post_check)

    _delete_backup_file(pre_restore)
    consumed = _ensure_pre_restore_record(pre_restore, status=BackupRecord.Status.CONSUMED)
    consumed.message = "PRE_RESTORE utilizado para revertir la última restauración."
    consumed.save(update_fields=["message"])
    _audit(consumed, CHANGE, "Reversión de restauración completada. PRE_RESTORE consumido.")
    _audit_record_message(
        user=user,
        action_flag=CHANGE,
        object_id=consumed.pk,
        object_repr=consumed.file_name,
        message="Reversión de restauración completada.",
    )
    return RestoreResult(restored=True, message="Reversión completada correctamente.", pre_restore=consumed)


def active_pre_restore():
    return _active_pre_restore_queryset().order_by("-created_at", "-pk").first()


def clear_active_pre_restore(*, reason):
    for record in _active_pre_restore_queryset():
        try:
            _delete_backup_file(record)
        except BackupError as exc:
            record.message = str(exc)
            record.save(update_fields=["message"])
            _audit(record, CHANGE, f"No fue posible retirar PRE_RESTORE: {exc}")
            continue
        record.status = BackupRecord.Status.REPLACED
        record.message = reason
        record.save(update_fields=["status", "message"])
        _audit(record, CHANGE, reason)


def latest_successful_ordinary_backup():
    return (
        BackupRecord.objects.filter(backup_type__in=ORDINARY_TYPES, status=BackupRecord.Status.SUCCESS)
        .order_by("-created_at", "-pk")
        .first()
    )


def current_change_marker():
    max_value = None
    for model in _tracked_models():
        field_name = "updated_at" if any(field.name == "updated_at" for field in model._meta.fields) else "created_at"
        value = model.objects.order_by(f"-{field_name}").values_list(field_name, flat=True).first()
        if value and (max_value is None or value > max_value):
            max_value = value
    log_value = _latest_non_backup_log_entry()
    if log_value and (max_value is None or log_value > max_value):
        max_value = log_value
    return max_value


def apply_retention_policy(limit=None, drive_client=None):
    limit = limit or settings.BACKUP_RETENTION_ORDINARY
    candidates = list(
        BackupRecord.objects.filter(backup_type__in=ORDINARY_TYPES, status=BackupRecord.Status.SUCCESS).order_by(
            "-created_at", "-pk"
        )
    )
    actionable = [record for record in candidates if _is_actionable_ordinary_backup(record)]
    if len(actionable) <= limit:
        return
    for record in actionable[limit:]:
        try:
            _delete_drive_backup_file(record, client=drive_client)
        except BackupError as exc:
            record.message = str(exc)
            record.save(update_fields=["message"])
            _audit(record, CHANGE, f"No fue posible retirar respaldo remoto por política de retención: {exc}")
            continue
        try:
            _delete_backup_file(record)
        except BackupError as exc:
            record.message = str(exc)
            record.save(update_fields=["message"])
            _audit(record, CHANGE, f"No fue posible retirar respaldo por política de retención: {exc}")
            continue
        record.status = BackupRecord.Status.PRUNED
        record.message = "Archivo eliminado por política de retención."
        record.save(update_fields=["status", "message"])
        _audit(record, DELETION, "Respaldo retirado por política de retención.")


def _tracked_models():
    from django.apps import apps

    labels = {"users", "real_estate", "fiduciary"}
    excluded = {("core", "BackupRecord")}
    models = []
    for model in apps.get_models():
        if model._meta.app_label in labels and (model._meta.app_label, model.__name__) not in excluded:
            if any(field.name in {"created_at", "updated_at"} for field in model._meta.fields):
                models.append(model)
    return models


def _safe_storage_dir():
    configured = Path(settings.BACKUP_STORAGE_PATH).expanduser()
    return configured.resolve()


def _initial_drive_status(backup_type):
    if backup_type in ORDINARY_TYPES:
        return BackupRecord.DriveSyncStatus.PENDING
    return BackupRecord.DriveSyncStatus.NOT_APPLICABLE


def _manifest_backup_uid(manifest):
    try:
        return uuid.UUID(str(manifest.get("backup_id") or uuid.uuid4()))
    except (TypeError, ValueError):
        return uuid.uuid4()


def _backup_remote_copy_is_safe(record):
    record.refresh_from_db(fields=["drive_sync_status", "drive_file_id"])
    return record.drive_sync_status == BackupRecord.DriveSyncStatus.SYNCED and bool(record.drive_file_id)


def _find_drive_backup(record, client, *, audit_user=None):
    try:
        remote = client.find_backup(str(record.backup_uid), record.checksum_sha256)
    except DriveBackupError as exc:
        _mark_drive_failed(record, str(exc), audit_user=audit_user)
        return DriveSyncResult(False, BackupRecord.DriveSyncStatus.FAILED, str(exc))
    if remote:
        _mark_drive_synced(record, remote.file_id, audit_user=audit_user, message="Copia ya existente en Google Drive.")
        return DriveSyncResult(True, BackupRecord.DriveSyncStatus.SYNCED, "Copia ya existente en Google Drive.", remote.file_id)
    return DriveSyncResult(False, BackupRecord.DriveSyncStatus.PENDING, "No se encontró copia remota.")


def _mark_drive_not_applicable(record):
    record.drive_sync_status = BackupRecord.DriveSyncStatus.NOT_APPLICABLE
    record.drive_last_error = ""
    record.save(update_fields=["drive_sync_status", "drive_last_error"])


def _mark_drive_pending(record, message, *, audit_user=None):
    record.drive_sync_status = BackupRecord.DriveSyncStatus.PENDING
    record.drive_last_attempt_at = timezone.now()
    record.drive_last_error = str(message)[:500]
    record.save(update_fields=["drive_sync_status", "drive_last_attempt_at", "drive_last_error"])
    _audit_record_message(
        user=audit_user or record.created_by,
        action_flag=CHANGE,
        object_id=record.pk,
        object_repr=record.file_name,
        message=f"Sincronización Google Drive pendiente: {record.drive_last_error}",
    )


def _mark_drive_syncing(record):
    record.drive_sync_status = BackupRecord.DriveSyncStatus.SYNCING
    record.drive_last_attempt_at = timezone.now()
    record.save(update_fields=["drive_sync_status", "drive_last_attempt_at"])


def _mark_drive_synced(record, file_id, *, audit_user=None, message):
    record.drive_sync_status = BackupRecord.DriveSyncStatus.SYNCED
    record.drive_file_id = file_id
    record.drive_synced_at = timezone.now()
    record.drive_last_attempt_at = record.drive_synced_at
    record.drive_last_error = ""
    record.save(
        update_fields=[
            "drive_sync_status",
            "drive_file_id",
            "drive_synced_at",
            "drive_last_attempt_at",
            "drive_last_error",
        ]
    )
    _audit_record_message(
        user=audit_user or record.created_by,
        action_flag=CHANGE,
        object_id=record.pk,
        object_repr=record.file_name,
        message=message,
    )


def _mark_drive_failed(record, message, *, audit_user=None):
    record.drive_sync_status = BackupRecord.DriveSyncStatus.FAILED
    record.drive_last_attempt_at = timezone.now()
    record.drive_last_error = str(message)[:500]
    record.save(update_fields=["drive_sync_status", "drive_last_attempt_at", "drive_last_error"])
    _audit_record_message(
        user=audit_user or record.created_by,
        action_flag=CHANGE,
        object_id=record.pk,
        object_repr=record.file_name,
        message=f"Sincronización Google Drive fallida: {record.drive_last_error}",
    )


def _prepare_storage_dir():
    storage_dir = _safe_storage_dir()
    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise BackupError("No hay permisos suficientes para crear o acceder a la carpeta de copias de seguridad.") from exc
    except OSError as exc:
        raise BackupError("No fue posible preparar la carpeta de copias de seguridad.") from exc
    if not storage_dir.is_dir():
        raise BackupError("La ruta de copias de seguridad no corresponde a una carpeta válida.")
    if not os.access(storage_dir, os.R_OK | os.W_OK):
        raise BackupError("El proceso actual no tiene permisos de lectura y escritura sobre la carpeta de copias de seguridad.")
    return storage_dir


def _safe_backup_file_path(record):
    if not record.file_path:
        raise BackupError("El respaldo no tiene archivo físico asociado.")
    storage_dir = _safe_storage_dir()
    path = Path(record.file_path).expanduser().resolve()
    try:
        path.relative_to(storage_dir)
    except ValueError as exc:
        raise BackupError("La ruta del respaldo no pertenece al almacenamiento administrado.") from exc
    if not path.exists() or not path.is_file():
        raise BackupError("El archivo físico del respaldo no existe.")
    return path


def _ensure_restorable_record(record):
    if record.status != BackupRecord.Status.SUCCESS:
        raise BackupError("El estado del respaldo no permite restauración.")
    if record.backup_type not in RESTORABLE_TYPES:
        raise BackupError("El tipo de respaldo no permite restauración.")


def _unique_backup_name(storage_dir, timestamp):
    return _unique_named_backup(storage_dir, f"Backup_{timestamp}_{uuid.uuid4().hex[:8]}", ".zip")


def _run_pg_dump(dump_path, runner=None):
    database = settings.DATABASES["default"]
    command = [
        settings.BACKUP_PG_DUMP_PATH,
        "--format=custom",
        "--file",
        str(dump_path),
        "--host",
        str(database.get("HOST") or "localhost"),
        "--port",
        str(database.get("PORT") or "5432"),
        "--username",
        str(database.get("USER") or ""),
        str(database.get("NAME") or ""),
    ]
    env = os.environ.copy()
    password = database.get("PASSWORD") or ""
    if password:
        env["PGPASSWORD"] = password
    runner = runner or subprocess.run
    result = runner(command, env=env, capture_output=True, text=True, check=False)
    if getattr(result, "returncode", 0) != 0:
        raise BackupError("No fue posible generar el volcado de PostgreSQL.")
    if not dump_path.exists() or dump_path.stat().st_size == 0:
        raise BackupError("El volcado de PostgreSQL no fue generado correctamente.")


def _run_pg_restore(zip_path, runner=None):
    database = settings.DATABASES["default"]
    with tempfile.TemporaryDirectory(prefix="pagosfiducia_restore_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        with zipfile.ZipFile(zip_path, "r") as archive:
            archive.extract(DUMP_NAME, path=temp_dir)
        dump_path = temp_dir / DUMP_NAME
        executable = settings.BACKUP_PG_RESTORE_PATH
        if runner is None:
            executable = _resolve_executable(executable, "pg_restore")
        command = [
            executable,
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--single-transaction",
            "--exit-on-error",
            "--dbname",
            str(database.get("NAME") or ""),
            "--host",
            str(database.get("HOST") or "localhost"),
            "--port",
            str(database.get("PORT") or "5432"),
            "--username",
            str(database.get("USER") or ""),
            str(dump_path),
        ]
        env = os.environ.copy()
        password = database.get("PASSWORD") or ""
        if password:
            env["PGPASSWORD"] = password
        runner = runner or subprocess.run
        should_close_connection = runner is subprocess.run
        if should_close_connection:
            connection.close()
        try:
            result = runner(command, env=env, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise BackupError("La herramienta de restauración de PostgreSQL no está disponible o no está configurada.") from exc
    if should_close_connection:
        connection.close()
    if getattr(result, "returncode", 0) != 0:
        raise BackupError("No fue posible restaurar el respaldo con pg_restore.")


def _build_manifest(*, backup_type, dump_path, checksum, backup_id):
    database = settings.DATABASES["default"]
    return {
        "backup_schema_version": settings.BACKUP_FORMAT_VERSION,
        "generated_at": timezone.now().isoformat(),
        "backup_id": backup_id,
        "type": backup_type,
        "application": "Gestión Fiduciaria",
        "application_version": settings.APP_VERSION,
        "database_name": database.get("NAME", ""),
        "postgresql_version": _postgresql_version(),
        "dump_file": DUMP_NAME,
        "checksum_algorithm": "sha256",
        "dump_checksum_sha256": checksum,
        "dump_size": dump_path.stat().st_size,
    }


def _postgresql_version():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version")
            return cursor.fetchone()[0]
    except Exception:
        return ""


def _write_zip(zip_path, *, dump_path, manifest, checksum_text):
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.write(dump_path, DUMP_NAME)
        archive.writestr(CHECKSUM_NAME, checksum_text)


def _validate_zip(zip_path, *, expected_checksum, strict=False):
    manifest, _ = _validate_zip_details(zip_path, expected_checksum=expected_checksum, strict=strict)
    return manifest


def _validate_zip_details(zip_path, *, expected_checksum=None, strict=False):
    with zipfile.ZipFile(zip_path, "r") as archive:
        if archive.testzip() is not None:
            raise BackupError("El archivo ZIP está corrupto.")
        names = set(archive.namelist())
        required = {MANIFEST_NAME, DUMP_NAME, CHECKSUM_NAME}
        if strict and names != required:
            raise BackupError("El archivo ZIP no contiene exactamente la estructura requerida.")
        if not required.issubset(names):
            raise BackupError("El archivo ZIP no contiene la estructura requerida.")
        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        checksum_text = archive.read(CHECKSUM_NAME).decode("utf-8").strip()
        dump_bytes = archive.read(DUMP_NAME)
    if not dump_bytes:
        raise BackupError("El dump del respaldo está vacío.")
    if manifest.get("backup_schema_version") != settings.BACKUP_FORMAT_VERSION:
        raise BackupError("La versión del formato de respaldo no es compatible.")
    if manifest.get("dump_file") != DUMP_NAME:
        raise BackupError("El manifest no corresponde al archivo dump esperado.")
    if manifest.get("checksum_algorithm") != "sha256":
        raise BackupError("El algoritmo de checksum no es compatible.")
    if not manifest.get("database_name"):
        raise BackupError("El manifest no contiene la base de datos de origen.")
    if not manifest.get("generated_at") or manifest.get("type") not in RESTORABLE_TYPES:
        raise BackupError("El manifest no contiene metadatos esenciales.")
    actual = hashlib.sha256(dump_bytes).hexdigest()
    if expected_checksum and actual != expected_checksum:
        raise BackupError("La validación del checksum del respaldo falló.")
    if manifest.get("dump_checksum_sha256") != actual or not checksum_text.startswith(actual):
        raise BackupError("La validación del checksum del respaldo falló.")
    return manifest, actual


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audit(record, action_flag, message):
    user_id = record.created_by_id or _system_user_id()
    if not user_id:
        return
    LogEntry.objects.log_actions(
        user_id=user_id,
        queryset=BackupRecord.objects.filter(pk=record.pk),
        action_flag=action_flag,
        change_message=message,
        single_object=True,
    )


def _audit_record_message(*, user, action_flag, object_id, object_repr, message):
    user_id = _existing_user_id(user) or _system_user_id()
    if not user_id:
        return
    content_type = ContentType.objects.get_for_model(BackupRecord)
    LogEntry.objects.create(
        user_id=user_id,
        content_type_id=content_type.pk,
        object_id=str(object_id),
        object_repr=str(object_repr)[:200],
        action_flag=action_flag,
        change_message=message,
    )


def _system_user_id():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.filter(role=User.Role.ACCOUNTING_ADMIN, is_active=True, is_deleted=False).order_by("pk").first()
    return user.pk if user else None


def _existing_user_id(user):
    if not user:
        return None
    from django.contrib.auth import get_user_model

    User = get_user_model()
    if User.objects.filter(pk=user.pk, email__iexact=getattr(user, "email", "")).exists():
        return user.pk
    matched = User.objects.filter(email__iexact=getattr(user, "email", "")).first()
    return matched.pk if matched else None


def _capture_current_users():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    snapshots = []
    queryset = User.objects.prefetch_related("groups", "user_permissions__content_type").order_by("pk")
    for user in queryset:
        fields = {
            "pk": user.pk,
            "password": user.password,
            "last_login": user.last_login,
            "is_superuser": user.is_superuser,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
            "is_staff": user.is_staff,
            "is_active": user.is_active,
            "date_joined": user.date_joined,
            "role": user.role,
            "is_deleted": user.is_deleted,
            "deleted_at": user.deleted_at,
        }
        permissions = tuple(
            user.user_permissions.order_by("content_type__app_label", "content_type__model", "codename").values_list(
                "content_type__app_label",
                "content_type__model",
                "codename",
            )
        )
        snapshots.append(
            UserSnapshot(
                fields=fields,
                groups=tuple(user.groups.order_by("name").values_list("name", flat=True)),
                permissions=permissions,
            )
        )
    return tuple(snapshots)


def _reconcile_current_users(snapshots):
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group, Permission

    User = get_user_model()
    current_emails = {str(snapshot.fields.get("email") or "").strip().lower() for snapshot in snapshots}
    current_emails.discard("")
    backup_only_before = User.objects.exclude(email__iexact="").exclude(email__in=current_emails).count()
    matched = 0
    recreated = 0
    pk_conflicts = 0

    try:
        with transaction.atomic():
            _reset_user_pk_sequence(User)
            for snapshot in snapshots:
                fields = dict(snapshot.fields)
                original_pk = fields.pop("pk")
                email = str(fields.get("email") or "").strip()
                user = User.objects.filter(email__iexact=email).first() if email else None
                if user:
                    matched += 1
                else:
                    if User.objects.filter(pk=original_pk).exists():
                        pk_conflicts += 1
                    user = User()
                    recreated += 1
                _apply_current_user_fields(user, fields, User)

                groups = [Group.objects.get_or_create(name=name)[0] for name in snapshot.groups]
                user.groups.set(groups)

                permissions = []
                for app_label, model, codename in snapshot.permissions:
                    permission = Permission.objects.filter(
                        content_type__app_label=app_label,
                        content_type__model=model,
                        codename=codename,
                    ).first()
                    if permission:
                        permissions.append(permission)
                user.user_permissions.set(permissions)
            _reset_user_pk_sequence(User)
    except IntegrityError as exc:
        raise BackupError("No fue posible reconciliar las cuentas de usuario después de la restauración.") from exc

    return (
        "Reconciliación de cuentas: "
        f"cuentas históricas recuperadas {backup_only_before}; "
        f"cuentas actuales conservadas {recreated}; "
        f"coincidencias reconciliadas {matched}; "
        f"conflictos de PK resueltos {pk_conflicts}."
    )


def _apply_current_user_fields(user, fields, User):
    fields["email"] = str(fields.get("email") or "").strip().lower()
    fields["username"] = _unique_username(User, fields.get("username") or fields["email"], current_pk=user.pk)
    for field, value in fields.items():
        setattr(user, field, value)
    user.save()


def _unique_username(User, username, *, current_pk=None):
    base = str(username or "usuario").strip() or "usuario"
    base = base[:140]
    candidate = base
    counter = 2
    query = User.objects.filter(username__iexact=candidate)
    if current_pk:
        query = query.exclude(pk=current_pk)
    while query.exists():
        suffix = f"_{counter}"
        candidate = f"{base[:150 - len(suffix)]}{suffix}"
        counter += 1
        query = User.objects.filter(username__iexact=candidate)
        if current_pk:
            query = query.exclude(pk=current_pk)
    return candidate


def _reset_user_pk_sequence(User):
    table_name = User._meta.db_table
    pk_column = User._meta.pk.column
    quoted_table = connection.ops.quote_name(table_name)
    quoted_pk = connection.ops.quote_name(pk_column)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT setval(pg_get_serial_sequence(%s, %s), COALESCE(MAX({quoted_pk}), 1), true) FROM {quoted_table}",
                [table_name, pk_column],
            )
    except Exception:
        return


def _latest_non_backup_log_entry():
    content_type = ContentType.objects.get_for_model(BackupRecord)
    return (
        LogEntry.objects.exclude(content_type=content_type)
        .order_by("-action_time")
        .values_list("action_time", flat=True)
        .first()
    )


def _active_pre_restore_queryset():
    return BackupRecord.objects.filter(
        backup_type=BackupRecord.BackupType.PRE_RESTORE,
        status=BackupRecord.Status.SUCCESS,
    )


def _unique_named_backup(storage_dir, base, suffix):
    candidate = f"{base}{suffix}"
    counter = 2
    while (storage_dir / candidate).exists():
        candidate = f"{base}_{counter}{suffix}"
        counter += 1
    return candidate


def _is_actionable_ordinary_backup(record):
    if record.backup_type not in ORDINARY_TYPES or record.status != BackupRecord.Status.SUCCESS:
        return False
    if record.drive_sync_status != BackupRecord.DriveSyncStatus.SYNCED or not record.drive_file_id:
        return False
    try:
        _safe_backup_file_path(record)
    except BackupError:
        return False
    return True


def _delete_backup_file(record):
    try:
        path = _safe_backup_file_path(record)
    except BackupError as exc:
        raise BackupError(f"El respaldo no está disponible físicamente para retiro: {exc}") from exc
    try:
        path.unlink()
    except PermissionError as exc:
        raise BackupError("No hay permisos suficientes para retirar el archivo de respaldo.") from exc
    except OSError as exc:
        raise BackupError("No fue posible retirar el archivo de respaldo.") from exc


def _delete_drive_backup_file(record, client=None):
    if record.backup_type not in ORDINARY_TYPES:
        return
    if record.drive_sync_status != BackupRecord.DriveSyncStatus.SYNCED or not record.drive_file_id:
        raise BackupError("El respaldo no tiene una copia remota sincronizada para retirar.")
    try:
        (client or GoogleDriveBackupClient()).delete_file(record.drive_file_id)
    except DriveNotConfigured as exc:
        raise BackupError("Google Drive no está configurado para retirar la copia remota.") from exc
    except DriveBackupError as exc:
        raise BackupError(str(exc)) from exc
    _audit(record, DELETION, "Copia remota retirada por política de retención.")


def _resolve_executable(configured, tool_name):
    if not configured:
        raise BackupError(f"La herramienta {tool_name} de PostgreSQL no está configurada.")
    configured_path = Path(str(configured))
    has_path_separator = "\\" in str(configured) or "/" in str(configured)
    if configured_path.is_absolute() or has_path_separator:
        if configured_path.exists() and configured_path.is_file():
            return str(configured_path)
        raise BackupError(f"La herramienta {tool_name} de PostgreSQL no existe en la ruta configurada.")
    resolved = shutil.which(str(configured))
    if resolved:
        return resolved
    raise BackupError(f"La herramienta {tool_name} de PostgreSQL no está disponible en PATH.")


def _copy_into_storage_with_inheritance(source, destination):
    try:
        with source.open("rb") as src, destination.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        source.unlink(missing_ok=True)
    except PermissionError as exc:
        _safe_unlink(destination)
        raise BackupError("No hay permisos suficientes para escribir la copia de seguridad en la carpeta configurada.") from exc
    except FileExistsError as exc:
        raise BackupError("Ya existe un archivo de copia de seguridad con el nombre generado.") from exc
    except OSError as exc:
        _safe_unlink(destination)
        raise BackupError("No fue posible guardar la copia de seguridad en la carpeta configurada.") from exc


def _safe_unlink(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _replace_previous_pre_restores(*, exclude_pk, previous_ids):
    for record in BackupRecord.objects.filter(pk__in=previous_ids).exclude(pk=exclude_pk):
        try:
            _delete_backup_file(record)
        except BackupError as exc:
            record.message = str(exc)
            record.save(update_fields=["message"])
            _audit(record, CHANGE, f"No fue posible reemplazar PRE_RESTORE anterior: {exc}")
            continue
        record.status = BackupRecord.Status.REPLACED
        record.message = "Reemplazado por un nuevo PRE_RESTORE válido."
        record.save(update_fields=["status", "message"])
        _audit(record, CHANGE, record.message)


def _ensure_pre_restore_record(source, status=BackupRecord.Status.SUCCESS):
    record, _ = BackupRecord.objects.update_or_create(
        file_path=source.file_path,
        defaults={
            "backup_uid": source.backup_uid,
            "backup_type": BackupRecord.BackupType.PRE_RESTORE,
            "status": status,
            "file_name": source.file_name,
            "file_size": source.file_size,
            "checksum_sha256": source.checksum_sha256,
            "last_change_at": source.last_change_at,
            "metadata": source.metadata,
            "message": source.message,
            "drive_sync_status": BackupRecord.DriveSyncStatus.NOT_APPLICABLE,
            "drive_file_id": "",
            "drive_synced_at": None,
            "drive_last_attempt_at": None,
            "drive_last_error": "",
        },
    )
    return record


def _post_restore_check(checker=None):
    if checker:
        checker()
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.execute("SELECT to_regclass('public.django_migrations')")
            if cursor.fetchone()[0] is None:
                raise BackupError("No se encontró la tabla de migraciones.")
            cursor.execute("SELECT to_regclass('public.users_user')")
            if cursor.fetchone()[0] is None:
                raise BackupError("No se encontró la tabla de usuarios.")
            cursor.execute("SELECT COUNT(*) FROM users_user")
            if cursor.fetchone()[0] < 1:
                raise BackupError("No se encontraron usuarios después de la restauración.")
    except OperationalError as exc:
        raise BackupError("No fue posible conectar con PostgreSQL después de la restauración.") from exc
