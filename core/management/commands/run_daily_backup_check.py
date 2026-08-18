from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_time
from django.utils import timezone

from core.models import BackupRecord, BackupSettings
from core.services.backups import BackupError, record_backup_failure, run_automatic_backup_if_needed


def daily_check_is_due(settings_obj, now=None):
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
    ):
        return False, "La comprobacion automatica ya fue realizada hoy."
    return True, "Corresponde ejecutar la comprobacion diaria de backups."


class Command(BaseCommand):
    help = "Verifica si corresponde generar una copia de seguridad automatica diaria."

    def handle(self, *args, **options):
        settings_obj = BackupSettings.get_solo()
        due, message = daily_check_is_due(settings_obj)
        if not due:
            self.stdout.write(self.style.WARNING(message))
            return
        try:
            result = run_automatic_backup_if_needed()
        except BackupError as exc:
            settings_obj = BackupSettings.get_solo()
            settings_obj.last_auto_check_at = timezone.now()
            settings_obj.last_auto_check_result = BackupSettings.LastCheckResult.FAILED
            settings_obj.save(update_fields=["last_auto_check_at", "last_auto_check_result", "updated_at"])
            record_backup_failure(backup_type=BackupRecord.BackupType.AUTOMATIC, message=str(exc))
            raise CommandError(str(exc)) from exc

        style = self.style.SUCCESS if result.created else self.style.WARNING
        self.stdout.write(style(result.message))
