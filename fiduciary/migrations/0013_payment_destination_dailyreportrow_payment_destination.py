from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fiduciary", "0012_fiduciaryassignment_actual_delivery_date_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="destination",
            field=models.CharField(
                blank=True,
                choices=[("constructora", "Constructora"), ("fiduciaria", "Fiduciaria")],
                max_length=16,
                null=True,
                verbose_name="recibido por",
            ),
        ),
        migrations.AddField(
            model_name="dailyreportrow",
            name="payment_destination",
            field=models.CharField(
                blank=True,
                choices=[("constructora", "Constructora"), ("fiduciaria", "Fiduciaria")],
                max_length=16,
                null=True,
                verbose_name="recibido por",
            ),
        ),
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(fields=["destination"], name="fiduciary_payment_dest_idx"),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(("destination__isnull", True), ("destination__in", ["constructora", "fiduciaria"]), _connector="OR"),
                name="fiduciary_payment_destination_valid",
            ),
        ),
    ]
