from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import BackupRecord, BackupSettings
from core.services.backups import BackupError, record_backup_failure, run_automatic_backup_if_needed
from core.services.backup_scheduler import daily_backup_check_is_due


def daily_check_is_due(settings_obj, now=None):
    return daily_backup_check_is_due(settings_obj, now=now or timezone.now())


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
            settings_obj.save(update_fields=["last_auto_check_at", "last_auto_check_result"])
            record_backup_failure(backup_type=BackupRecord.BackupType.AUTOMATIC, message=str(exc))
            raise CommandError(str(exc)) from exc

        style = self.style.SUCCESS if result.created else self.style.WARNING
        self.stdout.write(style(result.message))
