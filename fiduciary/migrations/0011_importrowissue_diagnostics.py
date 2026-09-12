from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fiduciary", "0010_alter_importedhistoricalobservation_origin_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="importrowissue",
            name="cause",
            field=models.TextField(blank=True, verbose_name="causa"),
        ),
        migrations.AddField(
            model_name="importrowissue",
            name="extra_data",
            field=models.JSONField(blank=True, default=dict, verbose_name="datos adicionales"),
        ),
        migrations.AddField(
            model_name="importrowissue",
            name="field_name",
            field=models.CharField(blank=True, max_length=120, verbose_name="campo"),
        ),
        migrations.AddField(
            model_name="importrowissue",
            name="found_value",
            field=models.TextField(blank=True, verbose_name="valor encontrado"),
        ),
        migrations.AddField(
            model_name="importrowissue",
            name="unit_code",
            field=models.CharField(blank=True, max_length=80, verbose_name="unidad"),
        ),
    ]
