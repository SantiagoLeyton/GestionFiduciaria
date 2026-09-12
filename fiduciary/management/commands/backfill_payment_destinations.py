from django.core.management.base import BaseCommand

from fiduciary.models import ImportedFile, Payment
from fiduciary.imports.historical.normalize import compact_normalized


def destination_from_source_header(header: str) -> str | None:
    normalized = compact_normalized(header or "")
    if normalized.startswith("recibidofidubogota") or normalized.startswith("recibofiducia"):
        return Payment.Destination.FIDUCIARIA
    if normalized.startswith("recibido"):
        return Payment.Destination.CONSTRUCTORA
    return None


def destination_from_payment(payment: Payment) -> str | None:
    if payment.source_file_id and payment.source_file.file_type == ImportedFile.FileType.REPORT:
        return Payment.Destination.FIDUCIARIA
    return destination_from_source_header(payment.source_header or "")


class Command(BaseCommand):
    help = "Completa Payment.destination desde reportes consolidados o encabezados historicos mensuales."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Reporta cambios sin escribir en la base de datos.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        updated = 0
        skipped = 0
        candidates = (
            Payment.objects.filter(destination__isnull=True)
            .select_related("source_file")
            .only("pk", "source_header", "destination", "source_file__file_type")
            .order_by("pk")
        )
        for payment in candidates.iterator():
            destination = destination_from_payment(payment)
            if not destination:
                skipped += 1
                continue
            if not dry_run:
                Payment.objects.filter(pk=payment.pk, destination__isnull=True).update(destination=destination)
            updated += 1
        action = "detectados" if dry_run else "actualizados"
        self.stdout.write(
            self.style.SUCCESS(
                f"Destinos {action}: {updated}. Pagos omitidos sin origen de reporte o encabezado mensual inequivoco: {skipped}."
            )
        )
