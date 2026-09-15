import uuid

from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="audit_events",
        blank=True,
        null=True,
    )
    action = models.CharField("accion", max_length=80)
    entity = models.CharField("entidad", max_length=120)
    entity_id = models.CharField("id de entidad", max_length=80, blank=True)
    entity_repr = models.CharField("representacion de entidad", max_length=200, blank=True)
    description = models.TextField("descripcion", blank=True)
    reason = models.TextField("motivo", blank=True)
    context = models.JSONField("contexto", default=dict, blank=True)
    before = models.JSONField("antes", default=dict, blank=True)
    after = models.JSONField("despues", default=dict, blank=True)
    summary = models.JSONField("resumen", default=dict, blank=True)
    created_at = models.DateTimeField("fecha de creacion", auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        indexes = [
            models.Index(fields=["action", "-created_at"], name="core_audit_action_date_idx"),
            models.Index(fields=["entity", "-created_at"], name="core_audit_entity_date_idx"),
        ]
        verbose_name = "audit event"
        verbose_name_plural = "audit events"

    def __str__(self):
        return f"{self.action} - {self.entity}"

    @property
    def visible_description(self):
        return self.description or self.reason or self.entity_repr or ""


class BackupRecord(models.Model):
    class BackupType(models.TextChoices):
        AUTOMATIC = "automatic", "Automático"
        MANUAL = "manual", "Manual"
        PRE_RESTORE = "pre_restore", "Previo a restauración"
        UPLOADED = "uploaded", "Cargado externo"

    class Status(models.TextChoices):
        SUCCESS = "success", "Exitoso"
        FAILED = "failed", "Fallido"
        PRUNED = "pruned", "Retenido fuera de línea"
        REPLACED = "replaced", "Reemplazado"
        CONSUMED = "consumed", "Consumido"

    class DriveSyncStatus(models.TextChoices):
        NOT_APPLICABLE = "not_applicable", "No aplica"
        PENDING = "pending", "Pendiente"
        SYNCING = "syncing", "Sincronizando"
        SYNCED = "synced", "Sincronizado"
        FAILED = "failed", "Error"

    backup_uid = models.UUIDField("identificador lógico", default=uuid.uuid4, editable=False, db_index=True)
    backup_type = models.CharField("tipo", max_length=16, choices=BackupType.choices)
    status = models.CharField("estado", max_length=16, choices=Status.choices, default=Status.SUCCESS)
    created_at = models.DateTimeField("fecha de creación", auto_now_add=True)
    file_path = models.TextField("ruta física", blank=True)
    file_name = models.CharField("archivo", max_length=180, blank=True)
    file_size = models.PositiveBigIntegerField("tamaño", default=0)
    checksum_sha256 = models.CharField("checksum SHA-256", max_length=64, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="backup_records",
        blank=True,
        null=True,
    )
    last_change_at = models.DateTimeField("último cambio detectado", blank=True, null=True)
    metadata = models.JSONField("metadatos", default=dict, blank=True)
    message = models.TextField("mensaje", blank=True)
    drive_sync_status = models.CharField(
        "estado Google Drive",
        max_length=20,
        choices=DriveSyncStatus.choices,
        default=DriveSyncStatus.NOT_APPLICABLE,
        db_index=True,
    )
    drive_file_id = models.CharField("ID archivo Google Drive", max_length=128, blank=True)
    drive_synced_at = models.DateTimeField("fecha sincronización Google Drive", blank=True, null=True)
    drive_last_attempt_at = models.DateTimeField("último intento Google Drive", blank=True, null=True)
    drive_last_error = models.TextField("último error Google Drive", blank=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        indexes = [
            models.Index(fields=["backup_type", "status", "-created_at"], name="core_backup_type_status_idx"),
            models.Index(fields=["status", "-created_at"], name="core_backup_status_date_idx"),
            models.Index(fields=["backup_type", "drive_sync_status"], name="core_backup_drive_status_idx"),
        ]
        verbose_name = "backup record"
        verbose_name_plural = "backup records"

    def __str__(self):
        return f"{self.get_backup_type_display()} - {self.created_at:%Y-%m-%d %H:%M}"

    @property
    def is_available(self):
        return self.status == self.Status.SUCCESS and bool(self.file_path)

    @property
    def is_ordinary(self):
        return self.backup_type in {self.BackupType.AUTOMATIC, self.BackupType.MANUAL}

    @property
    def is_user_downloadable(self):
        return self.backup_type in {self.BackupType.AUTOMATIC, self.BackupType.MANUAL, self.BackupType.UPLOADED}

    @property
    def requires_drive_sync(self):
        return self.backup_type in {self.BackupType.AUTOMATIC, self.BackupType.MANUAL}


class BackupSettings(models.Model):
    class LastCheckResult(models.TextChoices):
        NOT_RUN = "not_run", "Sin comprobaciones"
        BACKUP_CREATED = "backup_created", "Backup creado"
        NO_CHANGES = "no_changes", "Sin cambios"
        FAILED = "failed", "Fallo"

    class AutomationStatus(models.TextChoices):
        PENDING = "pending", "Pendiente de configuración"
        ACTIVE = "active", "Activa"

    daily_check_time = models.TimeField("hora diaria", default="18:00")
    last_auto_check_at = models.DateTimeField("última comprobación automática", blank=True, null=True)
    last_auto_check_result = models.CharField(
        "resultado de última comprobación",
        max_length=24,
        choices=LastCheckResult.choices,
        default=LastCheckResult.NOT_RUN,
    )
    automation_status = models.CharField(
        "estado de automatización",
        max_length=24,
        choices=AutomationStatus.choices,
        default=AutomationStatus.PENDING,
    )
    updated_at = models.DateTimeField("fecha de actualización", auto_now=True)

    class Meta:
        verbose_name = "backup settings"
        verbose_name_plural = "backup settings"

    def __str__(self):
        return f"Copias de seguridad - {self.daily_check_time:%H:%M}"

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
