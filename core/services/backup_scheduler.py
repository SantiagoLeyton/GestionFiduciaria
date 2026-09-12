import logging
import os
import sys
import threading

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone
from django.utils.dateparse import parse_time

from core.models import BackupRecord, BackupSettings
from core.services.backups import BackupError, BackupResult, record_backup_failure, run_automatic_backup_if_needed


logger = logging.getLogger(__name__)
_scheduler_thread = None
_stop_event = threading.Event()


def scheduler_should_start(argv=None, environ=None):
    if not getattr(settings, "BACKUP_AUTO_SCHEDULER_ENABLED", True):
        return False
    argv = list(sys.argv if argv is None else argv)
    environ = os.environ if environ is None else environ
    command = argv[1] if len(argv) > 1 else ""
    executable = os.path.basename(argv[0] if argv else "").lower()
    if command in {"check", "makemigrations", "migrate", "collectstatic", "shell", "test"}:
        return False
    argv_text = " ".join(str(part).lower() for part in argv)
    if "pytest" in executable or "py.test" in executable or "pytest" in argv_text:
        return False
    if command == "runserver":
        return environ.get("RUN_MAIN") == "true" or "--noreload" in argv
    return True


def scheduler_is_running():
    return bool(_scheduler_thread and _scheduler_thread.is_alive())


def mark_scheduler_active():
    try:
        BackupSettings.objects.filter(pk=BackupSettings.get_solo().pk).update(
            automation_status=BackupSettings.AutomationStatus.ACTIVE
        )
    except Exception:
        logger.exception("AUTO_BACKUP_SCHEDULER_STATUS_UPDATE_FAILED")


def refresh_scheduler_status():
    settings_obj = BackupSettings.get_solo()
    target_status = (
        BackupSettings.AutomationStatus.ACTIVE
        if scheduler_is_running()
        else BackupSettings.AutomationStatus.PENDING
    )
    if settings_obj.automation_status != target_status:
        BackupSettings.objects.filter(pk=settings_obj.pk).update(automation_status=target_status)
        settings_obj.automation_status = target_status
    return settings_obj


def start_backup_scheduler(*, interval_seconds=None):
    global _scheduler_thread
    if scheduler_is_running():
        return _scheduler_thread
    _stop_event.clear()
    interval_seconds = interval_seconds or getattr(settings, "BACKUP_AUTO_SCHEDULER_INTERVAL_SECONDS", 60)
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        kwargs={"interval_seconds": interval_seconds},
        name="gf-auto-backup-scheduler",
        daemon=True,
    )
    _scheduler_thread.start()
    logger.info(
        "AUTO_BACKUP_SCHEDULER_STARTED: pid=%s argv=%s interval=%s",
        os.getpid(),
        sys.argv,
        interval_seconds,
    )
    return _scheduler_thread


def stop_backup_scheduler():
    _stop_event.set()


def _scheduler_loop(*, interval_seconds):
    mark_scheduler_active()
    while not _stop_event.is_set():
        try:
            close_old_connections()
            run_pending_backup_check_once()
        except Exception:
            logger.exception("AUTO_BACKUP_SCHEDULER_LOOP_FAILED")
        finally:
            close_old_connections()
        _stop_event.wait(interval_seconds)


def daily_backup_check_is_due(settings_obj, now=None):
    now = timezone.localtime(now or timezone.now())
    daily_check_time = settings_obj.daily_check_time
    if isinstance(daily_check_time, str):
        daily_check_time = parse_time(daily_check_time)
    if daily_check_time and now.time() < daily_check_time:
        return False, "Aun no corresponde ejecutar la comprobacion diaria de backups."
    if (
        settings_obj.last_auto_check_at
        and timezone.localtime(settings_obj.last_auto_check_at).date() == now.date()
        and settings_obj.last_auto_check_result != BackupSettings.LastCheckResult.FAILED
        and not _settings_changed_after_last_check(settings_obj, now)
    ):
        return False, "La comprobacion automatica ya fue realizada hoy."
    return True, "Corresponde ejecutar la comprobacion diaria de backups."


def _settings_changed_after_last_check(settings_obj, now):
    if not settings_obj.last_auto_check_at or not settings_obj.updated_at:
        return False
    updated_at = timezone.localtime(settings_obj.updated_at)
    return updated_at <= now and updated_at > timezone.localtime(settings_obj.last_auto_check_at)


def run_pending_backup_check_once(*, now=None, runner=None, drive_client=None):
    settings_obj = BackupSettings.get_solo()
    due, message = daily_backup_check_is_due(settings_obj, now=now)
    if not due:
        logger.debug("AUTO_BACKUP_CHECK_NOT_DUE: %s", message)
        return BackupResult(record=None, created=False, message=message)
    logger.info("AUTO_BACKUP_CHECK_STARTED: %s", message)
    try:
        result = run_automatic_backup_if_needed(runner=runner, drive_client=drive_client)
    except BackupError as exc:
        settings_obj = BackupSettings.get_solo()
        settings_obj.last_auto_check_at = timezone.now()
        settings_obj.last_auto_check_result = BackupSettings.LastCheckResult.FAILED
        settings_obj.save(update_fields=["last_auto_check_at", "last_auto_check_result"])
        record_backup_failure(backup_type=BackupRecord.BackupType.AUTOMATIC, message=str(exc))
        logger.exception("AUTO_BACKUP_FAILED: %s", exc)
        return BackupResult(record=None, created=False, message=str(exc))
    if result.created:
        logger.info("AUTO_BACKUP_CREATED: %s", result.message)
    else:
        logger.info("AUTO_BACKUP_SKIPPED_NO_CHANGES: %s", result.message)
    return result
