import json
import os
import tempfile
import zipfile
from datetime import datetime, time
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import Group
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import BackupRecord, BackupSettings
from core.services import backups as backup_service
from core.services.backups import (
    CHECKSUM_NAME,
    DUMP_NAME,
    MANIFEST_NAME,
    BackupError,
    BackupResult,
    active_pre_restore,
    apply_retention_policy,
    create_backup,
    get_downloadable_backup_path,
    import_external_backup,
    record_backup_failure,
    restore_backup,
    revert_last_restore,
    run_automatic_backup_if_needed,
    sync_backup_to_drive,
    sync_pending_backups,
    validate_backup_record,
)
from core.services.google_drive_backups import DriveBackupError, DriveRemoteFile, DriveUnavailable


class Completed:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def successful_runner(content=b"dump-data", seen=None):
    def runner(command, env, capture_output, text, check):
        if seen is not None:
            seen["command"] = command
            seen["env"] = env
        dump_path = Path(command[command.index("--file") + 1])
        dump_path.write_bytes(content)
        return Completed()

    return runner


def failing_runner(command, env, capture_output, text, check):
    return Completed(returncode=1)


def restore_success_runner(command, env, capture_output, text, check):
    return Completed()


def replace_users_with(*user_specs):
    def runner(command, env, capture_output, text, check):
        User = get_user_model()
        User.objects.all().delete()
        for spec in user_specs:
            data = {
                "username": spec["username"],
                "email": spec["email"],
                "password": spec.get("password", "BackupPass123"),
                "first_name": spec.get("first_name", ""),
                "last_name": spec.get("last_name", ""),
                "role": spec.get("role", User.Role.COMMERCIAL),
                "is_active": spec.get("is_active", True),
                "is_staff": spec.get("is_staff", False),
                "is_superuser": spec.get("is_superuser", False),
                "is_deleted": spec.get("is_deleted", False),
                "deleted_at": spec.get("deleted_at"),
            }
            pk = spec.get("pk")
            if pk is not None:
                data["id"] = pk
            user = User.objects.create_user(**data)
            if "password_hash" in spec:
                user.password = spec["password_hash"]
                user.save(update_fields=["password"])
            for group_name in spec.get("groups", []):
                user.groups.add(Group.objects.get_or_create(name=group_name)[0])
        return Completed()

    return runner


class FakeDriveClient:
    def __init__(
        self,
        *,
        configured=True,
        online=True,
        existing=None,
        upload_failures=0,
        exists_after_upload_failures=False,
        exists_after_upload_calls=1,
        checksum_mismatch=False,
        delete_fails=False,
    ):
        self.configured = configured
        self.online = online
        self.existing = existing or {}
        self.upload_failures = upload_failures
        self.exists_after_upload_failures = exists_after_upload_failures
        self.exists_after_upload_calls = exists_after_upload_calls
        self.checksum_mismatch = checksum_mismatch
        self.delete_fails = delete_fails
        self.find_calls = 0
        self.upload_calls = 0
        self.delete_calls = []

    def has_connectivity(self):
        if not self.configured:
            from core.services.google_drive_backups import DriveNotConfigured

            raise DriveNotConfigured("Drive no configurado")
        if not self.online:
            raise DriveUnavailable("Sin conectividad con Google Drive")
        return True

    def find_backup(self, backup_id, checksum):
        self.find_calls += 1
        stored = self.existing.get(str(backup_id))
        if stored:
            if stored["checksum"] != checksum:
                raise DriveBackupError("Existe una copia remota con checksum diferente.")
            return DriveRemoteFile(file_id=stored["file_id"], checksum_sha256=checksum)
        if self.exists_after_upload_failures and self.upload_calls >= self.exists_after_upload_calls:
            self.existing[str(backup_id)] = {"file_id": f"drive-{backup_id}", "checksum": checksum}
            return DriveRemoteFile(file_id=f"drive-{backup_id}", checksum_sha256=checksum)
        if self.checksum_mismatch:
            raise DriveBackupError("Existe una copia remota con checksum diferente.")
        return None

    def upload_backup(self, *, file_path, file_name, backup_id, checksum):
        self.upload_calls += 1
        if self.upload_calls <= self.upload_failures:
            raise DriveUnavailable("Timeout al subir a Google Drive")
        self.existing[str(backup_id)] = {"file_id": f"drive-{backup_id}", "checksum": checksum}
        return DriveRemoteFile(file_id=f"drive-{backup_id}", checksum_sha256=checksum)

    def delete_file(self, file_id):
        self.delete_calls.append(file_id)
        if self.delete_fails:
            raise DriveUnavailable("No fue posible eliminar remoto")


def make_zip(path, *, dump=b"dump", manifest_overrides=None, checksum_override=None, extra=False):
    import hashlib

    checksum = hashlib.sha256(dump).hexdigest()
    manifest = {
        "backup_schema_version": settings.BACKUP_FORMAT_VERSION,
        "generated_at": timezone.now().isoformat(),
        "type": BackupRecord.BackupType.MANUAL,
        "application": "Gestión Fiduciaria",
        "application_version": settings.APP_VERSION,
        "database_name": "pagos_fiducia",
        "postgresql_version": "17",
        "dump_file": DUMP_NAME,
        "checksum_algorithm": "sha256",
        "dump_checksum_sha256": checksum,
        "dump_size": len(dump),
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(manifest))
        archive.writestr(DUMP_NAME, dump)
        archive.writestr(CHECKSUM_NAME, f"{checksum_override or checksum}  {DUMP_NAME}\n")
        if extra:
            archive.writestr("extra.txt", "no permitido")
    return checksum


def zip_bytes(*, dump=b"dump", manifest_overrides=None, checksum_override=None, extra=False):
    path = Path(tempfile.gettempdir()) / f"backup_test_{timezone.now().timestamp()}.zip"
    checksum = make_zip(
        path,
        dump=dump,
        manifest_overrides=manifest_overrides,
        checksum_override=checksum_override,
        extra=extra,
    )
    data = path.read_bytes()
    path.unlink()
    return data, checksum


def make_backup_record(
    tmp_path,
    *,
    status=None,
    backup_type=None,
    dump=b"dump",
    manifest_overrides=None,
    file_name="Backup_test.zip",
    drive_sync_status=None,
    drive_file_id="",
):
    zip_path = tmp_path / file_name
    checksum = make_zip(zip_path, dump=dump, manifest_overrides=manifest_overrides)
    return BackupRecord.objects.create(
        backup_type=backup_type or BackupRecord.BackupType.MANUAL,
        status=status or BackupRecord.Status.SUCCESS,
        file_path=str(zip_path),
        file_name=file_name,
        file_size=zip_path.stat().st_size,
        checksum_sha256=checksum,
        metadata={"application_version": settings.APP_VERSION},
        drive_sync_status=drive_sync_status or BackupRecord.DriveSyncStatus.NOT_APPLICABLE,
        drive_file_id=drive_file_id,
    )


def make_ordered_backup_record(
    tmp_path,
    *,
    name,
    days_old=0,
    status=None,
    backup_type=None,
    drive_sync_status=None,
    drive_file_id="",
):
    record = make_backup_record(
        tmp_path,
        status=status,
        backup_type=backup_type,
        file_name=name,
        drive_sync_status=drive_sync_status,
        drive_file_id=drive_file_id,
    )
    BackupRecord.objects.filter(pk=record.pk).update(created_at=timezone.now() - timezone.timedelta(days=days_old))
    record.refresh_from_db()
    return record


@pytest.mark.django_db
def test_accounting_can_access_backup_panel_and_commercial_is_blocked(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)
    response = client.get(reverse("backup_list"))
    assert response.status_code == 200
    assert "Copias de seguridad" in response.content.decode()

    client.force_login(commercial_user)
    response = client.get(reverse("backup_list"))
    assert response.status_code == 403


@pytest.mark.django_db
def test_backup_panel_does_not_expose_physical_path(client, accounting_admin_user, tmp_path):
    secret_path = tmp_path / "interno" / "Backup_2026-08-11_10-00.zip"
    secret_path.parent.mkdir()
    secret_path.write_bytes(b"zip")
    BackupRecord.objects.create(
        backup_type=BackupRecord.BackupType.MANUAL,
        status=BackupRecord.Status.SUCCESS,
        file_path=str(secret_path),
        file_name=secret_path.name,
        file_size=3,
        checksum_sha256="a" * 64,
        created_by=accounting_admin_user,
    )

    client.force_login(accounting_admin_user)
    content = client.get(reverse("backup_list")).content.decode()

    assert secret_path.name in content
    assert str(secret_path.parent) not in content


@pytest.mark.django_db
def test_manual_backup_creates_valid_zip_manifest_checksum_and_record(tmp_path, accounting_admin_user, monkeypatch):
    seen = {}
    drive_client = FakeDriveClient()
    monkeypatch.setitem(settings.DATABASES["default"], "PASSWORD", "super-secret")
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        result = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"pg-dump-content", seen),
            drive_client=drive_client,
        )

    record = result.record
    zip_path = Path(record.file_path)
    assert result.created is True
    assert record.created_by == accounting_admin_user
    assert zip_path.exists()
    assert "super-secret" not in seen["command"]
    assert seen["env"]["PGPASSWORD"] == "super-secret"

    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        assert {MANIFEST_NAME, DUMP_NAME, CHECKSUM_NAME}.issubset(names)
        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        checksum = archive.read(CHECKSUM_NAME).decode("utf-8").split()[0]
        dump = archive.read(DUMP_NAME)

    assert manifest["type"] == BackupRecord.BackupType.MANUAL
    assert manifest["backup_id"] == str(record.backup_uid)
    assert manifest["dump_file"] == DUMP_NAME
    assert manifest["checksum_algorithm"] == "sha256"
    assert manifest["dump_checksum_sha256"] == checksum == record.checksum_sha256
    assert manifest["dump_size"] == len(dump)
    assert "super-secret" not in json.dumps(manifest)
    assert record.drive_sync_status == BackupRecord.DriveSyncStatus.SYNCED
    assert record.drive_file_id
    assert LogEntry.objects.filter(object_id=str(record.pk), change_message__icontains="Respaldo generado").exists()


@pytest.mark.django_db
def test_pg_dump_failure_does_not_delete_existing_backups(tmp_path, accounting_admin_user):
    existing_path = tmp_path / "Backup_existente.zip"
    existing_path.write_bytes(b"existing")
    existing = BackupRecord.objects.create(
        backup_type=BackupRecord.BackupType.AUTOMATIC,
        status=BackupRecord.Status.SUCCESS,
        file_path=str(existing_path),
        file_name=existing_path.name,
        file_size=existing_path.stat().st_size,
        checksum_sha256="b" * 64,
    )

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        with pytest.raises(BackupError):
            create_backup(backup_type=BackupRecord.BackupType.MANUAL, user=accounting_admin_user, runner=failing_runner)

    existing.refresh_from_db()
    assert existing.status == BackupRecord.Status.SUCCESS
    assert existing_path.exists()

    failed = record_backup_failure(
        backup_type=BackupRecord.BackupType.MANUAL,
        user=accounting_admin_user,
        message="No fue posible generar el volcado de PostgreSQL.",
    )
    assert failed.status == BackupRecord.Status.FAILED


@pytest.mark.django_db
def test_backup_local_success_when_drive_has_no_connectivity(tmp_path, accounting_admin_user):
    drive_client = FakeDriveClient(online=False)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        result = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"local-only"),
            drive_client=drive_client,
        )

    record = result.record
    assert record.status == BackupRecord.Status.SUCCESS
    assert Path(record.file_path).exists()
    assert record.drive_sync_status == BackupRecord.DriveSyncStatus.PENDING
    assert drive_client.upload_calls == 0


@pytest.mark.django_db
def test_drive_not_configured_keeps_local_backup_pending(tmp_path, accounting_admin_user):
    drive_client = FakeDriveClient(configured=False)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        result = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"drive-not-configured"),
            drive_client=drive_client,
        )

    assert result.record.status == BackupRecord.Status.SUCCESS
    assert result.record.drive_sync_status == BackupRecord.DriveSyncStatus.PENDING
    assert result.record.drive_last_error


@pytest.mark.django_db
def test_drive_sync_checks_existing_before_retries_and_avoids_duplicate_upload(tmp_path, accounting_admin_user):
    drive_client = FakeDriveClient(upload_failures=1, exists_after_upload_failures=True)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"retry-drive"),
            drive_client=drive_client,
        ).record

    assert record.drive_sync_status == BackupRecord.DriveSyncStatus.SYNCED
    assert drive_client.upload_calls == 1
    assert drive_client.find_calls == 2


@pytest.mark.django_db
def test_drive_sync_final_check_after_third_error_can_mark_synced(tmp_path, accounting_admin_user):
    drive_client = FakeDriveClient(upload_failures=3, exists_after_upload_failures=True, exists_after_upload_calls=3)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"final-check"),
            drive_client=drive_client,
        ).record

    assert record.drive_sync_status == BackupRecord.DriveSyncStatus.SYNCED
    assert drive_client.upload_calls == 3


@pytest.mark.django_db
def test_drive_sync_checks_drive_before_second_and_third_attempt(tmp_path, accounting_admin_user):
    drive_client = FakeDriveClient(upload_failures=2)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"third-attempt"),
            drive_client=drive_client,
        ).record

    assert record.drive_sync_status == BackupRecord.DriveSyncStatus.SYNCED
    assert drive_client.find_calls == 3
    assert drive_client.upload_calls == 3


@pytest.mark.django_db
def test_drive_sync_three_failures_keep_local_success_failed_remote(tmp_path, accounting_admin_user):
    drive_client = FakeDriveClient(upload_failures=3)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"failed-drive"),
            drive_client=drive_client,
        ).record

    assert record.status == BackupRecord.Status.SUCCESS
    assert record.drive_sync_status == BackupRecord.DriveSyncStatus.FAILED
    assert drive_client.upload_calls == 3


@pytest.mark.django_db
def test_drive_sync_existing_backup_zero_uploads_and_checksum_mismatch_fails(tmp_path, accounting_admin_user):
    existing_client = FakeDriveClient()
    mismatch_client = FakeDriveClient(checksum_mismatch=True)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = make_backup_record(
            tmp_path,
            backup_type=BackupRecord.BackupType.MANUAL,
            drive_sync_status=BackupRecord.DriveSyncStatus.PENDING,
        )
        existing_client.existing[str(record.backup_uid)] = {
            "file_id": "remote-existing",
            "checksum": record.checksum_sha256,
        }
        result = sync_backup_to_drive(record, client=existing_client, user=accounting_admin_user)

        mismatch = make_backup_record(
            tmp_path,
            file_name="mismatch.zip",
            backup_type=BackupRecord.BackupType.MANUAL,
            drive_sync_status=BackupRecord.DriveSyncStatus.PENDING,
        )
        mismatch_result = sync_backup_to_drive(mismatch, client=mismatch_client, user=accounting_admin_user)

    record.refresh_from_db()
    mismatch.refresh_from_db()
    assert result.synced is True
    assert existing_client.upload_calls == 0
    assert record.drive_file_id == "remote-existing"
    assert mismatch_result.synced is False
    assert mismatch.drive_sync_status == BackupRecord.DriveSyncStatus.FAILED


@pytest.mark.django_db
def test_drive_tokens_are_not_written_to_manifest(tmp_path, accounting_admin_user):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path), GOOGLE_DRIVE_TOKEN_FILE="C:/secret/token.json"):
        record = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"manifest-security"),
            drive_client=FakeDriveClient(),
        ).record

    manifest = record.metadata
    manifest_text = json.dumps(manifest)
    assert manifest["backup_id"] == str(record.backup_uid)
    assert "token.json" not in manifest_text
    assert "GOOGLE_DRIVE" not in manifest_text


@pytest.mark.django_db
def test_retention_keeps_four_successful_ordinary_backups_after_new_valid_backup(tmp_path, accounting_admin_user):
    now = timezone.now()
    old_records = []
    for index in range(4):
        path = tmp_path / f"old_{index}.zip"
        path.write_bytes(b"old")
        record = BackupRecord.objects.create(
            backup_type=BackupRecord.BackupType.AUTOMATIC if index % 2 else BackupRecord.BackupType.MANUAL,
            status=BackupRecord.Status.SUCCESS,
            file_path=str(path),
            file_name=path.name,
            file_size=path.stat().st_size,
            checksum_sha256=str(index) * 64,
            created_by=accounting_admin_user if index % 2 == 0 else None,
            drive_sync_status=BackupRecord.DriveSyncStatus.SYNCED,
            drive_file_id=f"old-drive-{index}",
        )
        BackupRecord.objects.filter(pk=record.pk).update(created_at=now - timezone.timedelta(days=5 - index))
        old_records.append(record)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path), BACKUP_RETENTION_ORDINARY=4):
        create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"new-dump"),
            drive_client=FakeDriveClient(),
        )

    assert BackupRecord.objects.filter(status=BackupRecord.Status.SUCCESS).count() == 4
    oldest = BackupRecord.objects.get(pk=old_records[0].pk)
    assert oldest.status == BackupRecord.Status.PRUNED
    assert not Path(oldest.file_path).exists()


@pytest.mark.django_db
def test_failed_fifth_backup_does_not_prune_existing_four(tmp_path, accounting_admin_user):
    for index in range(4):
        path = tmp_path / f"kept_{index}.zip"
        path.write_bytes(b"kept")
        BackupRecord.objects.create(
            backup_type=BackupRecord.BackupType.MANUAL,
            status=BackupRecord.Status.SUCCESS,
            file_path=str(path),
            file_name=path.name,
            file_size=path.stat().st_size,
            checksum_sha256=str(index) * 64,
            created_by=accounting_admin_user,
        )

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path), BACKUP_RETENTION_ORDINARY=4):
        with pytest.raises(BackupError):
            create_backup(backup_type=BackupRecord.BackupType.MANUAL, user=accounting_admin_user, runner=failing_runner)

    assert BackupRecord.objects.filter(status=BackupRecord.Status.SUCCESS).count() == 4
    assert all(Path(record.file_path).exists() for record in BackupRecord.objects.filter(status=BackupRecord.Status.SUCCESS))


@pytest.mark.django_db
def test_automatic_backup_runs_once_per_day_and_only_when_changes_exist(tmp_path, monkeypatch):
    marker = timezone.now()
    monkeypatch.setattr(backup_service, "current_change_marker", lambda: marker)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        first = run_automatic_backup_if_needed(runner=successful_runner(b"auto-dump"))
        second = run_automatic_backup_if_needed(runner=successful_runner(b"auto-dump-2"))

    assert first.created is True
    assert second.created is False
    assert BackupRecord.objects.filter(backup_type=BackupRecord.BackupType.AUTOMATIC, status=BackupRecord.Status.SUCCESS).count() == 1


@pytest.mark.django_db
def test_automatic_backup_skips_when_no_changes_since_latest_success(tmp_path, monkeypatch):
    marker = timezone.now()
    BackupRecord.objects.create(
        backup_type=BackupRecord.BackupType.MANUAL,
        status=BackupRecord.Status.SUCCESS,
        file_path=str(tmp_path / "baseline.zip"),
        file_name="baseline.zip",
        file_size=1,
        checksum_sha256="c" * 64,
        last_change_at=marker,
    )
    monkeypatch.setattr(backup_service, "current_change_marker", lambda: marker)

    result = run_automatic_backup_if_needed(runner=successful_runner(b"unused"))

    assert result.created is False
    assert BackupRecord.objects.filter(backup_type=BackupRecord.BackupType.AUTOMATIC).count() == 0


@pytest.mark.django_db
def test_automatic_check_retries_pending_drive_sync_without_new_backup(tmp_path, monkeypatch):
    marker = timezone.now()
    pending = make_backup_record(
        tmp_path,
        backup_type=BackupRecord.BackupType.MANUAL,
        drive_sync_status=BackupRecord.DriveSyncStatus.PENDING,
    )
    pending.last_change_at = marker
    pending.save(update_fields=["last_change_at"])
    monkeypatch.setattr(backup_service, "current_change_marker", lambda: marker)
    drive_client = FakeDriveClient()

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        result = run_automatic_backup_if_needed(runner=successful_runner(b"unused"), drive_client=drive_client)

    pending.refresh_from_db()
    assert result.created is False
    assert pending.drive_sync_status == BackupRecord.DriveSyncStatus.SYNCED
    assert BackupRecord.objects.filter(backup_type=BackupRecord.BackupType.AUTOMATIC).count() == 0


@pytest.mark.django_db
def test_manual_backup_runs_even_without_detected_changes(tmp_path, accounting_admin_user, monkeypatch):
    monkeypatch.setattr(backup_service, "current_change_marker", lambda: None)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        result = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"manual-dump"),
        )

    assert result.created is True


@pytest.mark.django_db
def test_daily_backup_command_reports_result(tmp_path, monkeypatch):
    from core.management.commands import run_daily_backup_check as command_module

    settings_obj = BackupSettings.get_solo()
    settings_obj.daily_check_time = time(0, 0)
    settings_obj.save(update_fields=["daily_check_time"])
    monkeypatch.setattr(
        command_module,
        "run_automatic_backup_if_needed",
        lambda: BackupResult(record=None, created=True, message="Copia automática creada."),
    )

    call_command("run_daily_backup_check")


@pytest.mark.django_db
def test_backup_confirmation_view_uses_service_and_post(client, accounting_admin_user, monkeypatch):
    calls = []

    def fake_create_backup(*, backup_type, user):
        calls.append((backup_type, user.pk))

    monkeypatch.setattr("core.views.create_backup", fake_create_backup)
    client.force_login(accounting_admin_user)

    assert client.get(reverse("backup_confirm")).status_code == 200
    response = client.post(reverse("backup_create"))

    assert response.status_code == 302
    assert calls == [(BackupRecord.BackupType.MANUAL, accounting_admin_user.pk)]


@pytest.mark.django_db
def test_password_reset_templates_reuse_login_visual_asset(client):
    for url_name in ["password_reset", "password_reset_done", "password_reset_complete"]:
        response = client.get(reverse(url_name))
        content = response.content.decode()
        assert response.status_code == 200
        assert "login-visual-media" in content
        assert "assets/login" in content


@pytest.mark.django_db
def test_backup_validation_accepts_valid_zip(tmp_path):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = make_backup_record(tmp_path)
        result = validate_backup_record(record)

    assert result.valid is True
    assert result.manifest["checksum_algorithm"] == "sha256"


@pytest.mark.django_db
def test_backup_validation_rejects_checksum_manifest_incomplete_empty_missing_pruned_and_path_traversal(tmp_path):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        bad_checksum = make_backup_record(tmp_path, file_name="bad_checksum.zip")
        bad_checksum.checksum_sha256 = "0" * 64
        bad_checksum.save(update_fields=["checksum_sha256"])
        assert validate_backup_record(bad_checksum).valid is False

        bad_manifest = make_backup_record(
            tmp_path,
            file_name="bad_manifest.zip",
            manifest_overrides={"checksum_algorithm": "md5"},
        )
        assert validate_backup_record(bad_manifest).valid is False

        incomplete_path = tmp_path / "incomplete.zip"
        with zipfile.ZipFile(incomplete_path, "w") as archive:
            archive.writestr(MANIFEST_NAME, "{}")
        incomplete = BackupRecord.objects.create(
            backup_type=BackupRecord.BackupType.MANUAL,
            status=BackupRecord.Status.SUCCESS,
            file_path=str(incomplete_path),
            file_name=incomplete_path.name,
            file_size=incomplete_path.stat().st_size,
            checksum_sha256="a" * 64,
        )
        assert validate_backup_record(incomplete).valid is False

        empty_dump = make_backup_record(tmp_path, file_name="empty.zip", dump=b"")
        assert validate_backup_record(empty_dump).valid is False

        missing = make_backup_record(tmp_path, file_name="missing.zip")
        Path(missing.file_path).unlink()
        assert validate_backup_record(missing).valid is False

        pruned = make_backup_record(tmp_path, file_name="pruned.zip", status=BackupRecord.Status.PRUNED)
        assert validate_backup_record(pruned).valid is False

    outside = tmp_path.parent / "outside.zip"
    checksum = make_zip(outside)
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        traversal = BackupRecord.objects.create(
            backup_type=BackupRecord.BackupType.MANUAL,
            status=BackupRecord.Status.SUCCESS,
            file_path=str(outside),
            file_name=outside.name,
            file_size=outside.stat().st_size,
            checksum_sha256=checksum,
        )
        assert validate_backup_record(traversal).valid is False


@pytest.mark.django_db
def test_restore_generates_pre_restore_and_runs_pg_restore(tmp_path, accounting_admin_user):
    seen = {}

    def restore_runner(command, env, capture_output, text, check):
        seen["command"] = command
        return Completed()

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = make_backup_record(tmp_path)
        result = restore_backup(
            record=record,
            user=accounting_admin_user,
            dump_runner=successful_runner(b"before-restore"),
            restore_runner=restore_runner,
            post_check=lambda: None,
        )

    assert result.restored is True
    assert result.pre_restore.backup_type == BackupRecord.BackupType.PRE_RESTORE
    assert active_pre_restore() == result.pre_restore
    assert "--single-transaction" in seen["command"]
    assert "--clean" in seen["command"]
    assert "--if-exists" in seen["command"]
    assert LogEntry.objects.filter(change_message__icontains="Restauración exitosa").exists()


@pytest.mark.django_db
def test_restore_reconciles_current_and_backup_users_without_duplicates(tmp_path, accounting_admin_user):
    User = get_user_model()
    current_group = Group.objects.get_or_create(name="accounting_admin")[0]
    accounting_admin_user.groups.add(current_group)
    current_b = User.objects.create_user(
        username="usuario-b",
        email="b@centenario.com",
        password="CurrentB123",
        first_name="Beatriz",
        role=User.Role.COMMERCIAL,
    )
    current_c = User.objects.create_user(
        username="usuario-c",
        email="c@centenario.com",
        password="CurrentC123",
        first_name="Camila",
        role=User.Role.COMMERCIAL,
    )

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = make_backup_record(tmp_path)
        restore_backup(
            record=record,
            user=accounting_admin_user,
            dump_runner=successful_runner(b"before-restore"),
            restore_runner=replace_users_with(
                {
                    "username": "contabilidad-historica",
                    "email": accounting_admin_user.email,
                    "password": "BackupPass123",
                    "first_name": "Historica",
                    "role": User.Role.COMMERCIAL,
                },
                {
                    "username": "usuario-b-historico",
                    "email": current_b.email,
                    "password": "BackupPass123",
                    "first_name": "Backup B",
                    "role": User.Role.ACCOUNTING_ADMIN,
                },
                {
                    "username": "usuario-d",
                    "email": "d@centenario.com",
                    "password": "BackupD123",
                    "first_name": "Diana",
                    "role": User.Role.COMMERCIAL,
                },
            ),
            post_check=lambda: None,
        )

    emails = set(User.objects.values_list("email", flat=True))
    expected = {accounting_admin_user.email, current_b.email, current_c.email, "d@centenario.com"}
    assert expected.issubset(emails)
    assert User.objects.filter(email__in=expected).count() == 4

    restored_a = User.objects.get(email=accounting_admin_user.email)
    restored_b = User.objects.get(email=current_b.email)
    restored_c = User.objects.get(email=current_c.email)
    restored_d = User.objects.get(email="d@centenario.com")

    assert restored_a.first_name == accounting_admin_user.first_name
    assert restored_a.role == User.Role.ACCOUNTING_ADMIN
    assert restored_a.check_password("StrongPass123")
    assert list(restored_a.groups.values_list("name", flat=True)) == ["accounting_admin"]
    assert restored_b.first_name == "Beatriz"
    assert restored_b.role == User.Role.COMMERCIAL
    assert restored_b.check_password("CurrentB123")
    assert restored_c.check_password("CurrentC123")
    assert restored_d.first_name == "Diana"
    assert authenticate(username=current_c.email, password="CurrentC123") == restored_c
    assert LogEntry.objects.filter(change_message__icontains="Reconciliación de cuentas").exists()


@pytest.mark.django_db
def test_restore_current_user_administrative_state_prevales_over_backup(tmp_path, accounting_admin_user):
    User = get_user_model()
    current = User.objects.create_user(
        username="estado-actual",
        email="estado@centenario.com",
        password="CurrentState123",
        first_name="Nombre actual",
        last_name="Apellido actual",
        role=User.Role.ACCOUNTING_ADMIN,
        is_active=False,
        is_deleted=True,
        deleted_at=timezone.now(),
    )
    current_password = current.password

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = make_backup_record(tmp_path)
        restore_backup(
            record=record,
            user=accounting_admin_user,
            dump_runner=successful_runner(b"before-restore"),
            restore_runner=replace_users_with(
                {
                    "username": "estado-backup",
                    "email": current.email,
                    "password": "BackupState123",
                    "first_name": "Nombre backup",
                    "last_name": "Apellido backup",
                    "role": User.Role.COMMERCIAL,
                    "is_active": True,
                    "is_deleted": False,
                }
            ),
            post_check=lambda: None,
        )

    restored = User.objects.get(email=current.email)
    assert restored.first_name == "Nombre actual"
    assert restored.last_name == "Apellido actual"
    assert restored.role == User.Role.ACCOUNTING_ADMIN
    assert restored.password == current_password
    assert restored.is_active is False
    assert restored.is_deleted is True
    assert restored.deleted_at is not None


@pytest.mark.django_db
def test_restore_resolves_user_pk_conflict_and_preserves_historical_references(tmp_path, accounting_admin_user):
    User = get_user_model()
    current = User.objects.create_user(
        username="carlos",
        email="carlos@centenario.com",
        password="CarlosActual123",
        first_name="Carlos",
        role=User.Role.COMMERCIAL,
    )
    conflicting_pk = current.pk

    def restore_runner(command, env, capture_output, text, check):
        User.objects.all().delete()
        historic = User.objects.create_user(
            id=conflicting_pk,
            username="fabio",
            email="fabio@centenario.com",
            password="FabioBackup123",
            first_name="Fabio",
            role=User.Role.ACCOUNTING_ADMIN,
        )
        content_type = ContentType.objects.get_for_model(BackupRecord)
        LogEntry.objects.create(
            user=historic,
            content_type=content_type,
            object_id="historico",
            object_repr="Registro historico",
            action_flag=CHANGE,
            change_message="Auditoria historica del backup.",
        )
        return Completed()

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = make_backup_record(tmp_path)
        restore_backup(
            record=record,
            user=accounting_admin_user,
            dump_runner=successful_runner(b"before-restore"),
            restore_runner=restore_runner,
            post_check=lambda: None,
        )

    historic = User.objects.get(email="fabio@centenario.com")
    recreated = User.objects.get(email=current.email)
    assert historic.pk == conflicting_pk
    assert recreated.pk != conflicting_pk
    assert recreated.check_password("CarlosActual123")
    assert LogEntry.objects.get(change_message="Auditoria historica del backup.").user == historic


@pytest.mark.django_db
def test_revert_pre_restore_does_not_reconcile_current_users(tmp_path, accounting_admin_user):
    User = get_user_model()
    current_only = User.objects.create_user(
        username="posterior",
        email="posterior@centenario.com",
        password="Posterior123",
        role=User.Role.COMMERCIAL,
    )

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        pre_restore = make_backup_record(tmp_path, backup_type=BackupRecord.BackupType.PRE_RESTORE)
        result = revert_last_restore(
            user=accounting_admin_user,
            restore_runner=replace_users_with(
                {
                    "username": "solo-pre-restore",
                    "email": "solo_pre_restore@centenario.com",
                    "password": "PreRestore123",
                    "role": User.Role.ACCOUNTING_ADMIN,
                }
            ),
            post_check=lambda: None,
        )

    assert result.restored is True
    assert User.objects.filter(email=current_only.email).exists() is False
    assert User.objects.filter(email="solo_pre_restore@centenario.com").exists() is True
    assert BackupRecord.objects.get(pk=pre_restore.pk).status == BackupRecord.Status.CONSUMED


@pytest.mark.django_db
def test_restore_does_not_run_when_pre_restore_fails(tmp_path, accounting_admin_user):
    calls = []

    def restore_runner(command, env, capture_output, text, check):
        calls.append(command)
        return Completed()

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = make_backup_record(tmp_path)
        with pytest.raises(BackupError):
            restore_backup(
                record=record,
                user=accounting_admin_user,
                dump_runner=failing_runner,
                restore_runner=restore_runner,
                post_check=lambda: None,
            )

    assert calls == []


@pytest.mark.django_db
def test_new_pre_restore_replaces_previous_only_after_new_one_is_valid(tmp_path, accounting_admin_user):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        old = make_backup_record(
            tmp_path,
            backup_type=BackupRecord.BackupType.PRE_RESTORE,
            file_name="old_pre_restore.zip",
        )
        record = make_backup_record(tmp_path, file_name="restore.zip")
        restore_backup(
            record=record,
            user=accounting_admin_user,
            dump_runner=successful_runner(b"new-pre-restore"),
            restore_runner=restore_success_runner,
            post_check=lambda: None,
        )

    old.refresh_from_db()
    assert old.status == BackupRecord.Status.REPLACED
    assert not Path(old.file_path).exists()
    assert active_pre_restore().file_name != old.file_name


@pytest.mark.django_db
def test_restore_failure_keeps_pre_restore_available(tmp_path, accounting_admin_user):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = make_backup_record(tmp_path)
        with pytest.raises(BackupError):
            restore_backup(
                record=record,
                user=accounting_admin_user,
                dump_runner=successful_runner(b"before-failure"),
                restore_runner=failing_runner,
                post_check=lambda: None,
            )

    assert active_pre_restore() is not None
    assert Path(active_pre_restore().file_path).exists()


@pytest.mark.django_db
def test_restore_post_check_failure_is_controlled_and_keeps_pre_restore(tmp_path, accounting_admin_user):
    def failing_check():
        raise BackupError("validación posterior fallida")

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = make_backup_record(tmp_path)
        with pytest.raises(BackupError):
            restore_backup(
                record=record,
                user=accounting_admin_user,
                dump_runner=successful_runner(b"before-failure"),
                restore_runner=restore_success_runner,
                post_check=failing_check,
            )

    assert active_pre_restore() is not None


@pytest.mark.django_db
def test_revert_uses_pre_restore_once_and_consumes_it(tmp_path, accounting_admin_user):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        pre_restore = make_backup_record(
            tmp_path,
            backup_type=BackupRecord.BackupType.PRE_RESTORE,
            file_name="pre_restore.zip",
        )
        result = revert_last_restore(
            user=accounting_admin_user,
            restore_runner=restore_success_runner,
            post_check=lambda: None,
        )

    pre_restore.refresh_from_db()
    assert result.restored is True
    assert pre_restore.status == BackupRecord.Status.CONSUMED
    assert not Path(pre_restore.file_path).exists()
    assert active_pre_restore() is None
    with pytest.raises(BackupError):
        revert_last_restore(user=accounting_admin_user, restore_runner=restore_success_runner, post_check=lambda: None)


@pytest.mark.django_db
def test_successful_ordinary_backup_clears_pre_restore_and_failed_one_keeps_it(tmp_path, accounting_admin_user):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        pre_restore = make_backup_record(
            tmp_path,
            backup_type=BackupRecord.BackupType.PRE_RESTORE,
            file_name="pre_restore.zip",
        )
        with pytest.raises(BackupError):
            create_backup(backup_type=BackupRecord.BackupType.MANUAL, user=accounting_admin_user, runner=failing_runner)
        pre_restore.refresh_from_db()
        assert pre_restore.status == BackupRecord.Status.SUCCESS
        assert Path(pre_restore.file_path).exists()

        create_backup(
            backup_type=BackupRecord.BackupType.AUTOMATIC,
            runner=successful_runner(b"ordinary"),
        )
        pre_restore.refresh_from_db()

    assert pre_restore.status == BackupRecord.Status.REPLACED
    assert not Path(pre_restore.file_path).exists()


@pytest.mark.django_db
def test_pre_restore_does_not_participate_in_ordinary_retention(tmp_path, accounting_admin_user):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path), BACKUP_RETENTION_ORDINARY=4):
        pre_restore = make_backup_record(
            tmp_path,
            backup_type=BackupRecord.BackupType.PRE_RESTORE,
            file_name="pre_restore.zip",
        )
        for index in range(4):
            make_backup_record(
                tmp_path,
                file_name=f"ordinary_{index}.zip",
                drive_sync_status=BackupRecord.DriveSyncStatus.SYNCED,
                drive_file_id=f"drive-ordinary-{index}",
            )
        create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"new"),
            cleanup_pre_restore=False,
            drive_client=FakeDriveClient(),
        )

    pre_restore.refresh_from_db()
    assert pre_restore.status == BackupRecord.Status.SUCCESS
    assert BackupRecord.objects.filter(backup_type__in=[BackupRecord.BackupType.MANUAL, BackupRecord.BackupType.AUTOMATIC], status=BackupRecord.Status.SUCCESS).count() == 4


@pytest.mark.django_db
def test_downloadable_backup_path_rejects_pre_restore_and_invalid_path(tmp_path):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        pre_restore = make_backup_record(
            tmp_path,
            backup_type=BackupRecord.BackupType.PRE_RESTORE,
            file_name="pre_restore.zip",
        )
        with pytest.raises(BackupError):
            get_downloadable_backup_path(pre_restore)

        record = make_backup_record(tmp_path, file_name="download.zip")
        assert get_downloadable_backup_path(record).name == "download.zip"


@pytest.mark.django_db
def test_backup_detail_restore_download_and_revert_views_permissions(client, accounting_admin_user, commercial_user, tmp_path, monkeypatch):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = make_backup_record(tmp_path, file_name="web.zip")
        pre_restore = make_backup_record(
            tmp_path,
            backup_type=BackupRecord.BackupType.PRE_RESTORE,
            file_name="web_pre_restore.zip",
        )

        client.force_login(accounting_admin_user)
        detail = client.get(reverse("backup_detail", args=[record.pk]))
        assert detail.status_code == 200
        assert "Integridad válida" in detail.content.decode()
        assert str(tmp_path) not in detail.content.decode()
        assert client.get(reverse("backup_restore_confirm", args=[record.pk])).status_code == 200
        assert client.get(reverse("backup_download", args=[record.pk])).status_code == 200
        assert client.get(reverse("backup_revert_confirm")).status_code == 200

        monkeypatch.setattr("core.views.restore_backup", lambda record, user: None)
        monkeypatch.setattr("core.views.revert_last_restore", lambda user: None)
        assert client.post(reverse("backup_restore", args=[record.pk])).status_code == 302
        assert client.post(reverse("backup_revert")).status_code == 302

        client.force_login(commercial_user)
        assert client.get(reverse("backup_detail", args=[record.pk])).status_code == 403
        assert client.get(reverse("backup_restore_confirm", args=[record.pk])).status_code == 403
        assert client.get(reverse("backup_download", args=[record.pk])).status_code == 403
        assert client.post(reverse("backup_restore", args=[record.pk])).status_code == 403
        assert client.get(reverse("backup_revert_confirm")).status_code == 403
        assert client.post(reverse("backup_revert")).status_code == 403


@pytest.mark.django_db
def test_manual_drive_retry_view_uses_existing_backup_and_permissions(client, accounting_admin_user, commercial_user, tmp_path, monkeypatch):
    record = make_backup_record(
        tmp_path,
        backup_type=BackupRecord.BackupType.MANUAL,
        drive_sync_status=BackupRecord.DriveSyncStatus.FAILED,
    )
    calls = []

    def fake_sync(backup, user):
        calls.append((backup.pk, user.pk))
        backup.drive_sync_status = BackupRecord.DriveSyncStatus.SYNCED
        backup.drive_file_id = "drive-retry"
        backup.save(update_fields=["drive_sync_status", "drive_file_id"])
        return backup_service.DriveSyncResult(True, BackupRecord.DriveSyncStatus.SYNCED, "Sincronizado")

    monkeypatch.setattr("core.views.sync_backup_to_drive", fake_sync)

    client.force_login(accounting_admin_user)
    response = client.post(reverse("backup_drive_retry", args=[record.pk]))
    assert response.status_code == 302
    assert calls == [(record.pk, accounting_admin_user.pk)]
    assert BackupRecord.objects.count() == 1

    client.force_login(commercial_user)
    assert client.post(reverse("backup_drive_retry", args=[record.pk])).status_code == 403


@pytest.mark.django_db
def test_external_backup_upload_valid_zip_is_registered_without_pruning_ordinary_backups(tmp_path, accounting_admin_user):
    for index in range(4):
        make_backup_record(tmp_path, file_name=f"ordinary_existing_{index}.zip")
    data, checksum = zip_bytes(dump=b"external-dump")
    uploaded = SimpleUploadedFile("descargado.zip", data, content_type="application/zip")

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        result = import_external_backup(uploaded_file=uploaded, user=accounting_admin_user)

    record = result.record
    assert record.backup_type == BackupRecord.BackupType.UPLOADED
    assert record.checksum_sha256 == checksum
    assert Path(record.file_path).exists()
    assert BackupRecord.objects.filter(
        backup_type=BackupRecord.BackupType.MANUAL,
        status=BackupRecord.Status.SUCCESS,
    ).count() == 4
    with zipfile.ZipFile(record.file_path) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {MANIFEST_NAME, DUMP_NAME, CHECKSUM_NAME}


@pytest.mark.django_db
def test_external_backup_upload_rejects_invalid_checksum_manifest_incomplete_and_cleans_file(tmp_path, accounting_admin_user):
    cases = [
        SimpleUploadedFile("invalid.zip", b"no-es-zip", content_type="application/zip"),
        SimpleUploadedFile("checksum.zip", zip_bytes(checksum_override="0" * 64)[0], content_type="application/zip"),
        SimpleUploadedFile(
            "manifest.zip",
            zip_bytes(manifest_overrides={"backup_schema_version": "99.0"})[0],
            content_type="application/zip",
        ),
    ]
    incomplete = tmp_path / "incomplete_upload.zip"
    with zipfile.ZipFile(incomplete, "w") as archive:
        archive.writestr(MANIFEST_NAME, "{}")
    cases.append(SimpleUploadedFile("incomplete.zip", incomplete.read_bytes(), content_type="application/zip"))

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        for uploaded in cases:
            with pytest.raises(BackupError):
                import_external_backup(uploaded_file=uploaded, user=accounting_admin_user)

    assert BackupRecord.objects.filter(backup_type=BackupRecord.BackupType.UPLOADED).count() == 0
    assert not list(tmp_path.glob("Backup_externo_*.zip"))


@pytest.mark.django_db
def test_external_backup_upload_view_permissions_and_path_traversal_name(client, accounting_admin_user, commercial_user, tmp_path):
    data, _ = zip_bytes()
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        client.force_login(accounting_admin_user)
        response = client.post(
            reverse("backup_upload"),
            {"file": SimpleUploadedFile("../backup.zip", data, content_type="application/zip")},
        )
        assert response.status_code == 302
        record = BackupRecord.objects.get(backup_type=BackupRecord.BackupType.UPLOADED)
        assert ".." not in record.file_name
        assert Path(record.file_path).resolve().is_relative_to(tmp_path.resolve())

        client.force_login(commercial_user)
        assert client.get(reverse("backup_upload")).status_code == 403
        assert client.post(reverse("backup_upload"), {}).status_code == 403


@pytest.mark.django_db
def test_pg_restore_absolute_path_and_missing_tool_error_is_controlled(tmp_path, accounting_admin_user):
    seen = {}
    executable = tmp_path / "pg_restore.exe"
    executable.write_text("fake", encoding="utf-8")

    def restore_runner(command, env, capture_output, text, check):
        seen["command"] = command
        return Completed()

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path), BACKUP_PG_RESTORE_PATH=str(executable)):
        record = make_backup_record(tmp_path)
        restore_backup(
            record=record,
            user=accounting_admin_user,
            dump_runner=successful_runner(b"before"),
            restore_runner=restore_runner,
            post_check=lambda: None,
        )
    assert seen["command"][0] == str(executable)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path), BACKUP_PG_RESTORE_PATH=str(tmp_path / "missing.exe")):
        record = make_backup_record(tmp_path, file_name="restore_missing_tool.zip")
        with pytest.raises(BackupError, match="herramienta"):
            restore_backup(
                record=record,
                user=accounting_admin_user,
                dump_runner=successful_runner(b"before"),
                restore_runner=None,
                post_check=lambda: None,
            )


@pytest.mark.django_db
def test_pg_restore_missing_tool_does_not_return_500_in_view(client, accounting_admin_user, tmp_path, monkeypatch):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = make_backup_record(tmp_path)

    def fake_restore(record, user):
        raise BackupError("La herramienta de restauración de PostgreSQL no está disponible o no está configurada.")

    monkeypatch.setattr("core.views.restore_backup", fake_restore)
    client.force_login(accounting_admin_user)
    response = client.post(reverse("backup_restore", args=[record.pk]))
    assert response.status_code == 302


@pytest.mark.django_db
def test_final_generated_backup_file_is_standard_zip_and_same_file_validated(tmp_path, accounting_admin_user):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        result = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"standard-zip-dump"),
        )

        final_path = Path(result.record.file_path)
        assert final_path.read_bytes()[:4] == b"PK\x03\x04"
        with zipfile.ZipFile(final_path) as archive:
            assert archive.testzip() is None
            assert set(archive.namelist()) == {MANIFEST_NAME, DUMP_NAME, CHECKSUM_NAME}
            manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        assert validate_backup_record(result.record).manifest == manifest


@pytest.mark.django_db
def test_pruned_backup_is_shown_as_unavailable_without_actions(client, accounting_admin_user, tmp_path):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        record = make_backup_record(tmp_path, status=BackupRecord.Status.PRUNED)
        client.force_login(accounting_admin_user)
        content = client.get(reverse("backup_list")).content.decode()

    assert record.file_name in content
    assert "No disponible físicamente para acciones" in content
    assert reverse("backup_restore_confirm", args=[record.pk]) not in content
    assert reverse("backup_download", args=[record.pk]) not in content


@pytest.mark.django_db
def test_backup_settings_time_persistence_and_permissions(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)
    response = client.post(reverse("backup_settings"), {"daily_check_time": "19:30"})
    assert response.status_code == 302
    settings_obj = BackupSettings.get_solo()
    assert settings_obj.daily_check_time.strftime("%H:%M") == "19:30"

    content = client.get(reverse("backup_list")).content.decode()
    assert "19:30" in content
    assert "Pendiente de configuración" in content
    assert "La tarea diaria del servidor debe configurarse externamente." in content

    client.force_login(commercial_user)
    assert client.post(reverse("backup_settings"), {"daily_check_time": "20:00"}).status_code == 403


@pytest.mark.django_db
def test_daily_backup_command_records_last_check_created_no_changes_and_failed(tmp_path, monkeypatch):
    marker = timezone.now()
    monkeypatch.setattr(backup_service, "current_change_marker", lambda: marker)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        run_automatic_backup_if_needed(runner=successful_runner(b"auto"))
        settings_obj = BackupSettings.get_solo()
        assert settings_obj.last_auto_check_result == BackupSettings.LastCheckResult.BACKUP_CREATED
        assert settings_obj.last_auto_check_at is not None

        run_automatic_backup_if_needed(runner=successful_runner(b"ignored"))
        settings_obj.refresh_from_db()
        assert settings_obj.last_auto_check_result == BackupSettings.LastCheckResult.NO_CHANGES

    settings_obj.last_auto_check_result = BackupSettings.LastCheckResult.FAILED
    settings_obj.save(update_fields=["last_auto_check_result"])
    settings_obj.refresh_from_db()
    assert settings_obj.last_auto_check_result == BackupSettings.LastCheckResult.FAILED


@pytest.mark.django_db
def test_daily_backup_command_waits_until_configured_time(monkeypatch):
    from core.management.commands import run_daily_backup_check as command_module

    settings_obj = BackupSettings.get_solo()
    settings_obj.daily_check_time = time(18, 0)
    settings_obj.save(update_fields=["daily_check_time"])
    monkeypatch.setattr(command_module.timezone, "now", lambda: timezone.make_aware(datetime(2026, 8, 18, 17, 0)))
    monkeypatch.setattr(
        command_module,
        "run_automatic_backup_if_needed",
        lambda: pytest.fail("No debe ejecutar la comprobacion antes de la hora configurada."),
    )

    call_command("run_daily_backup_check")

    settings_obj.refresh_from_db()
    assert settings_obj.last_auto_check_at is None


@pytest.mark.django_db
def test_daily_backup_command_runs_at_or_after_configured_time(monkeypatch):
    from core.management.commands import run_daily_backup_check as command_module

    settings_obj = BackupSettings.get_solo()
    settings_obj.daily_check_time = time(18, 0)
    settings_obj.save(update_fields=["daily_check_time"])
    calls = []
    monkeypatch.setattr(command_module.timezone, "now", lambda: timezone.make_aware(datetime(2026, 8, 18, 18, 0)))
    monkeypatch.setattr(
        command_module,
        "run_automatic_backup_if_needed",
        lambda: calls.append("run") or BackupResult(record=None, created=False, message="Sin cambios."),
    )

    call_command("run_daily_backup_check")

    assert calls == ["run"]


@pytest.mark.django_db
def test_daily_backup_command_tolerates_hourly_scheduler_after_half_hour(monkeypatch):
    from core.management.commands import run_daily_backup_check as command_module

    settings_obj = BackupSettings.get_solo()
    settings_obj.daily_check_time = time(18, 30)
    settings_obj.save(update_fields=["daily_check_time"])
    calls = []
    monkeypatch.setattr(
        command_module,
        "run_automatic_backup_if_needed",
        lambda: calls.append("run") or BackupResult(record=None, created=False, message="Sin cambios."),
    )

    monkeypatch.setattr(command_module.timezone, "now", lambda: timezone.make_aware(datetime(2026, 8, 18, 18, 0)))
    call_command("run_daily_backup_check")
    assert calls == []

    monkeypatch.setattr(command_module.timezone, "now", lambda: timezone.make_aware(datetime(2026, 8, 18, 19, 0)))
    call_command("run_daily_backup_check")
    assert calls == ["run"]


@pytest.mark.django_db
def test_daily_backup_command_does_not_repeat_after_same_day_check(monkeypatch):
    from core.management.commands import run_daily_backup_check as command_module

    settings_obj = BackupSettings.get_solo()
    settings_obj.daily_check_time = time(18, 0)
    settings_obj.last_auto_check_at = timezone.make_aware(datetime(2026, 8, 18, 18, 5))
    settings_obj.last_auto_check_result = BackupSettings.LastCheckResult.NO_CHANGES
    settings_obj.save(update_fields=["daily_check_time", "last_auto_check_at", "last_auto_check_result"])
    monkeypatch.setattr(command_module.timezone, "now", lambda: timezone.make_aware(datetime(2026, 8, 18, 20, 0)))
    monkeypatch.setattr(
        command_module,
        "run_automatic_backup_if_needed",
        lambda: pytest.fail("No debe repetir la comprobacion el mismo dia."),
    )

    call_command("run_daily_backup_check")


@pytest.mark.django_db
def test_daily_backup_command_time_change_applies_next_day(monkeypatch):
    from core.management.commands import run_daily_backup_check as command_module

    settings_obj = BackupSettings.get_solo()
    settings_obj.daily_check_time = time(20, 0)
    settings_obj.last_auto_check_at = timezone.make_aware(datetime(2026, 8, 18, 18, 5))
    settings_obj.last_auto_check_result = BackupSettings.LastCheckResult.BACKUP_CREATED
    settings_obj.save(update_fields=["daily_check_time", "last_auto_check_at", "last_auto_check_result"])
    calls = []
    monkeypatch.setattr(
        command_module,
        "run_automatic_backup_if_needed",
        lambda: calls.append("run") or BackupResult(record=None, created=False, message="Sin cambios."),
    )

    monkeypatch.setattr(command_module.timezone, "now", lambda: timezone.make_aware(datetime(2026, 8, 18, 20, 0)))
    call_command("run_daily_backup_check")
    assert calls == []

    monkeypatch.setattr(command_module.timezone, "now", lambda: timezone.make_aware(datetime(2026, 8, 19, 20, 0)))
    call_command("run_daily_backup_check")
    assert calls == ["run"]


@pytest.mark.django_db
def test_daily_backup_command_retries_after_failed_check(monkeypatch):
    from core.management.commands import run_daily_backup_check as command_module

    settings_obj = BackupSettings.get_solo()
    settings_obj.daily_check_time = time(18, 0)
    settings_obj.last_auto_check_at = timezone.make_aware(datetime(2026, 8, 18, 18, 5))
    settings_obj.last_auto_check_result = BackupSettings.LastCheckResult.FAILED
    settings_obj.save(update_fields=["daily_check_time", "last_auto_check_at", "last_auto_check_result"])
    calls = []
    monkeypatch.setattr(command_module.timezone, "now", lambda: timezone.make_aware(datetime(2026, 8, 18, 19, 0)))
    monkeypatch.setattr(
        command_module,
        "run_automatic_backup_if_needed",
        lambda: calls.append("run") or BackupResult(record=None, created=False, message="Sin cambios."),
    )

    call_command("run_daily_backup_check")

    assert calls == ["run"]


@pytest.mark.django_db
def test_backup_creation_does_not_call_chmod_and_final_file_is_created_in_storage(
    tmp_path, accounting_admin_user, monkeypatch
):
    def forbidden_chmod(*args, **kwargs):
        raise AssertionError("El módulo de backups no debe modificar permisos manualmente.")

    monkeypatch.setattr(os, "chmod", forbidden_chmod)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        result = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"acl-inheritance"),
        )

    final_path = Path(result.record.file_path)
    assert final_path.parent == tmp_path
    with zipfile.ZipFile(final_path) as archive:
        assert archive.testzip() is None


@pytest.mark.django_db
def test_external_upload_does_not_call_chmod(tmp_path, accounting_admin_user, monkeypatch):
    def forbidden_chmod(*args, **kwargs):
        raise AssertionError("El módulo de backups no debe modificar permisos manualmente.")

    monkeypatch.setattr(os, "chmod", forbidden_chmod)
    data, _ = zip_bytes(dump=b"uploaded-acl")

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        result = import_external_backup(
            uploaded_file=SimpleUploadedFile("externo.zip", data, content_type="application/zip"),
            user=accounting_admin_user,
        )

    assert Path(result.record.file_path).parent == tmp_path


@pytest.mark.django_db
def test_permission_error_when_writing_final_backup_is_controlled_and_keeps_existing_backups(
    tmp_path, accounting_admin_user, monkeypatch
):
    existing_path = tmp_path / "Backup_existente.zip"
    existing_path.write_bytes(b"existing")
    existing = BackupRecord.objects.create(
        backup_type=BackupRecord.BackupType.MANUAL,
        status=BackupRecord.Status.SUCCESS,
        file_path=str(existing_path),
        file_name=existing_path.name,
        file_size=existing_path.stat().st_size,
        checksum_sha256="d" * 64,
        created_by=accounting_admin_user,
    )

    def denied_copy(source, destination):
        raise BackupError("No hay permisos suficientes para escribir la copia de seguridad en la carpeta configurada.")

    monkeypatch.setattr(backup_service, "_copy_into_storage_with_inheritance", denied_copy)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        with pytest.raises(BackupError, match="permisos suficientes"):
            create_backup(
                backup_type=BackupRecord.BackupType.MANUAL,
                user=accounting_admin_user,
                runner=successful_runner(b"new-content"),
            )

    existing.refresh_from_db()
    assert existing.status == BackupRecord.Status.SUCCESS
    assert existing_path.exists()
    assert BackupRecord.objects.filter(status=BackupRecord.Status.SUCCESS).count() == 1


def test_copy_into_storage_permission_error_is_converted_to_backup_error(tmp_path, monkeypatch):
    source = tmp_path / "source.zip"
    destination = tmp_path / "final.zip"
    source.write_bytes(b"data")
    original_open = Path.open

    def guarded_open(self, *args, **kwargs):
        if self == destination:
            raise PermissionError("denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    with pytest.raises(BackupError, match="permisos suficientes"):
        backup_service._copy_into_storage_with_inheritance(source, destination)

    assert not destination.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_retention_does_not_unlink_ordinary_backups_when_count_is_four_or_less(tmp_path, monkeypatch, count):
    for index in range(count):
        make_ordered_backup_record(
            tmp_path,
            name=f"ordinary_{index}.zip",
            days_old=count - index,
            drive_sync_status=BackupRecord.DriveSyncStatus.SYNCED,
            drive_file_id=f"drive-{index}",
        )
    unlinked = []

    def spy_unlink(self, *args, **kwargs):
        unlinked.append(Path(self).name)

    monkeypatch.setattr(Path, "unlink", spy_unlink)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path), BACKUP_RETENTION_ORDINARY=4):
        apply_retention_policy()

    assert unlinked == []


@pytest.mark.django_db
def test_retention_with_fifth_ordinary_unlinks_only_oldest_actionable_backup(tmp_path, monkeypatch):
    records = [
        make_ordered_backup_record(
            tmp_path,
            name=f"ordinary_{index}.zip",
            days_old=5 - index,
            drive_sync_status=BackupRecord.DriveSyncStatus.SYNCED,
            drive_file_id=f"drive-{index}",
        )
        for index in range(5)
    ]
    unlinked = []
    original_unlink = Path.unlink

    def spy_unlink(self, *args, **kwargs):
        unlinked.append(Path(self).name)
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", spy_unlink)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path), BACKUP_RETENTION_ORDINARY=4):
        apply_retention_policy(drive_client=FakeDriveClient())

    records[0].refresh_from_db()
    assert unlinked == [records[0].file_name]
    assert records[0].status == BackupRecord.Status.PRUNED
    assert BackupRecord.objects.filter(status=BackupRecord.Status.SUCCESS, backup_type=BackupRecord.BackupType.MANUAL).count() == 4


@pytest.mark.django_db
def test_new_local_backup_with_drive_failure_does_not_prune_existing_pairs(tmp_path, accounting_admin_user):
    for index in range(4):
        make_ordered_backup_record(
            tmp_path,
            name=f"ordinary_existing_{index}.zip",
            days_old=4 - index,
            drive_sync_status=BackupRecord.DriveSyncStatus.SYNCED,
            drive_file_id=f"drive-existing-{index}",
        )

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path), BACKUP_RETENTION_ORDINARY=4):
        create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"new-local-only"),
            drive_client=FakeDriveClient(upload_failures=3),
        )

    assert BackupRecord.objects.filter(status=BackupRecord.Status.SUCCESS, backup_type=BackupRecord.BackupType.MANUAL).count() == 5
    assert BackupRecord.objects.filter(status=BackupRecord.Status.PRUNED).count() == 0
    assert all(Path(record.file_path).exists() for record in BackupRecord.objects.filter(status=BackupRecord.Status.SUCCESS))


@pytest.mark.django_db
def test_remote_retention_failure_does_not_delete_local_backup(tmp_path):
    records = [
        make_ordered_backup_record(
            tmp_path,
            name=f"ordinary_pair_{index}.zip",
            days_old=5 - index,
            drive_sync_status=BackupRecord.DriveSyncStatus.SYNCED,
            drive_file_id=f"drive-pair-{index}",
        )
        for index in range(5)
    ]

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path), BACKUP_RETENTION_ORDINARY=4):
        apply_retention_policy(drive_client=FakeDriveClient(delete_fails=True))

    records[0].refresh_from_db()
    assert records[0].status == BackupRecord.Status.SUCCESS
    assert Path(records[0].file_path).exists()
    assert "remoto" in records[0].message


@pytest.mark.django_db
def test_retention_ignores_uploaded_pre_restore_and_non_success_statuses(tmp_path, monkeypatch):
    for index in range(4):
        make_ordered_backup_record(
            tmp_path,
            name=f"ordinary_{index}.zip",
            days_old=4 - index,
            drive_sync_status=BackupRecord.DriveSyncStatus.SYNCED,
            drive_file_id=f"drive-{index}",
        )
    make_ordered_backup_record(tmp_path, name="uploaded.zip", backup_type=BackupRecord.BackupType.UPLOADED)
    make_ordered_backup_record(tmp_path, name="pre_restore.zip", backup_type=BackupRecord.BackupType.PRE_RESTORE)
    make_ordered_backup_record(tmp_path, name="failed.zip", status=BackupRecord.Status.FAILED)
    make_ordered_backup_record(tmp_path, name="pruned.zip", status=BackupRecord.Status.PRUNED)
    make_ordered_backup_record(tmp_path, name="replaced.zip", status=BackupRecord.Status.REPLACED)
    make_ordered_backup_record(tmp_path, name="consumed.zip", status=BackupRecord.Status.CONSUMED)
    unlinked = []

    def spy_unlink(self, *args, **kwargs):
        unlinked.append(Path(self).name)

    monkeypatch.setattr(Path, "unlink", spy_unlink)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path), BACKUP_RETENTION_ORDINARY=4):
        apply_retention_policy()

    assert unlinked == []


@pytest.mark.django_db
def test_retention_ignores_historical_success_records_that_are_not_actionable(tmp_path, monkeypatch):
    missing = BackupRecord.objects.create(
        backup_type=BackupRecord.BackupType.MANUAL,
        status=BackupRecord.Status.SUCCESS,
        file_path=str(tmp_path / "Backup_2026-08-11_15-28.zip"),
        file_name="Backup_2026-08-11_15-28.zip",
        file_size=10,
        checksum_sha256="e" * 64,
    )
    for index in range(4):
        make_ordered_backup_record(
            tmp_path,
            name=f"ordinary_{index}.zip",
            days_old=4 - index,
            drive_sync_status=BackupRecord.DriveSyncStatus.SYNCED,
            drive_file_id=f"drive-{index}",
        )
    unlinked = []

    def spy_unlink(self, *args, **kwargs):
        unlinked.append(Path(self).name)

    monkeypatch.setattr(Path, "unlink", spy_unlink)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path), BACKUP_RETENTION_ORDINARY=4):
        apply_retention_policy()

    missing.refresh_from_db()
    assert unlinked == []
    assert missing.status == BackupRecord.Status.SUCCESS


@pytest.mark.django_db
def test_create_pre_restore_does_not_execute_ordinary_retention(tmp_path, monkeypatch):
    def forbidden_retention(*args, **kwargs):
        raise AssertionError("PRE_RESTORE no debe ejecutar retención ordinaria.")

    monkeypatch.setattr(backup_service, "apply_retention_policy", forbidden_retention)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        result = create_backup(
            backup_type=BackupRecord.BackupType.PRE_RESTORE,
            runner=successful_runner(b"pre-restore"),
        )

    assert result.record.backup_type == BackupRecord.BackupType.PRE_RESTORE
    assert result.record.drive_sync_status == BackupRecord.DriveSyncStatus.NOT_APPLICABLE


@pytest.mark.django_db
def test_pre_restore_and_uploaded_are_not_synced_to_drive(tmp_path, accounting_admin_user):
    drive_client = FakeDriveClient()
    data, _ = zip_bytes()

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        pre_restore = create_backup(
            backup_type=BackupRecord.BackupType.PRE_RESTORE,
            runner=successful_runner(b"pre-no-drive"),
            drive_client=drive_client,
        ).record
        uploaded = import_external_backup(
            uploaded_file=SimpleUploadedFile("externo.zip", data, content_type="application/zip"),
            user=accounting_admin_user,
        ).record
        pre_result = sync_backup_to_drive(pre_restore, client=drive_client, user=accounting_admin_user)
        upload_result = sync_backup_to_drive(uploaded, client=drive_client, user=accounting_admin_user)

    assert pre_result.status == BackupRecord.DriveSyncStatus.NOT_APPLICABLE
    assert upload_result.status == BackupRecord.DriveSyncStatus.NOT_APPLICABLE
    assert drive_client.upload_calls == 0


@pytest.mark.django_db
def test_restore_does_not_unlink_ordinary_backups(tmp_path, accounting_admin_user, monkeypatch):
    ordinary = make_ordered_backup_record(tmp_path, name="ordinary_old.zip", days_old=5)
    target = make_ordered_backup_record(tmp_path, name="ordinary_restore.zip")
    unlinked = []
    original_unlink = Path.unlink

    def spy_unlink(self, *args, **kwargs):
        path = Path(self)
        if path.parent == tmp_path:
            unlinked.append(path.name)
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", spy_unlink)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        restore_backup(
            record=target,
            user=accounting_admin_user,
            dump_runner=successful_runner(b"pre-restore"),
            restore_runner=restore_success_runner,
            post_check=lambda: None,
        )

    assert ordinary.file_name not in unlinked
    assert target.file_name not in unlinked


@pytest.mark.django_db
def test_replacing_pre_restore_only_unlinks_previous_pre_restore(tmp_path, accounting_admin_user, monkeypatch):
    ordinary = make_ordered_backup_record(tmp_path, name="ordinary_old.zip", days_old=5)
    old_pre = make_ordered_backup_record(
        tmp_path,
        name="pre_restore_old.zip",
        backup_type=BackupRecord.BackupType.PRE_RESTORE,
        days_old=4,
    )
    target = make_ordered_backup_record(tmp_path, name="ordinary_restore.zip")
    unlinked = []
    original_unlink = Path.unlink

    def spy_unlink(self, *args, **kwargs):
        path = Path(self)
        if path.parent == tmp_path:
            unlinked.append(path.name)
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", spy_unlink)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        restore_backup(
            record=target,
            user=accounting_admin_user,
            dump_runner=successful_runner(b"pre-restore"),
            restore_runner=restore_success_runner,
            post_check=lambda: None,
        )

    assert old_pre.file_name in unlinked
    assert ordinary.file_name not in unlinked
    assert target.file_name not in unlinked


@pytest.mark.django_db
def test_backup_names_are_unique_without_reusing_or_deleting_existing_file(tmp_path, accounting_admin_user):
    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        first = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"first"),
        ).record
        second = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"second"),
        ).record

    assert first.file_name != second.file_name
    assert Path(first.file_path).exists()
    assert Path(second.file_path).exists()


@pytest.mark.django_db
def test_temporary_cleanup_source_is_not_final_zip(tmp_path, accounting_admin_user, monkeypatch):
    unlinked = []
    original_unlink = Path.unlink

    def spy_unlink(self, *args, **kwargs):
        unlinked.append(Path(self))
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", spy_unlink)

    with override_settings(BACKUP_STORAGE_PATH=str(tmp_path)):
        result = create_backup(
            backup_type=BackupRecord.BackupType.MANUAL,
            user=accounting_admin_user,
            runner=successful_runner(b"cleanup"),
        )

    final_path = Path(result.record.file_path)
    assert final_path.exists()
    assert final_path not in unlinked
