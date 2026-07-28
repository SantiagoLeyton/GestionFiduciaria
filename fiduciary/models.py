from django.core.exceptions import ValidationError
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from real_estate.models import PropertyUnit


class TimestampedModel(models.Model):
    last_change_reason = models.TextField("ultimo motivo de modificacion", blank=True)
    created_at = models.DateTimeField("fecha de creacion", auto_now_add=True)
    updated_at = models.DateTimeField("fecha de actualizacion", auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Client(TimestampedModel):
    class DocumentType(models.TextChoices):
        CITIZENSHIP_ID = "cc", "Cedula de ciudadania"
        FOREIGN_ID = "ce", "Cedula de extranjeria"
        PASSPORT = "passport", "Pasaporte"
        TAX_ID = "nit", "NIT"
        UNKNOWN = "unknown", "Desconocido"

    class InformationStatus(models.TextChoices):
        COMPLETE = "complete", "Completo"
        INCOMPLETE = "incomplete", "Incompleto"

    class SourceOrigin(models.TextChoices):
        MANUAL = "manual", "Manual"
        HISTORICAL_IMPORT = "historical_import", "Importacion historica"
        REPORT_IMPORT = "report_import", "Importacion de reporte"

    document_type = models.CharField(
        "tipo de documento",
        max_length=16,
        choices=DocumentType.choices,
        default=DocumentType.UNKNOWN,
    )
    document_number = models.CharField("numero de documento", max_length=50, blank=True, null=True)
    first_names = models.CharField("nombres", max_length=150, blank=True)
    last_names_or_company = models.CharField("apellidos o razon social", max_length=180)
    phone = models.CharField("telefono", max_length=50, blank=True)
    email = models.EmailField("correo electronico", blank=True)
    address = models.CharField("direccion", max_length=250, blank=True)
    information_status = models.CharField(
        "estado de informacion",
        max_length=16,
        choices=InformationStatus.choices,
        default=InformationStatus.COMPLETE,
    )
    incomplete_reason = models.TextField("motivo de informacion incompleta", blank=True)
    source_origin = models.CharField(
        "origen del registro",
        max_length=24,
        choices=SourceOrigin.choices,
        default=SourceOrigin.MANUAL,
    )
    is_active = models.BooleanField("activo", default=True)

    class Meta:
        ordering = ("last_names_or_company", "first_names", "document_number")
        constraints = [
            models.UniqueConstraint(
                fields=["document_type", "document_number"],
                condition=Q(document_number__isnull=False) & ~Q(document_number=""),
                name="fiduciary_client_document_unique",
            ),
            models.CheckConstraint(
                condition=Q(information_status__in=["complete", "incomplete"]),
                name="fiduciary_client_information_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(source_origin__in=["manual", "historical_import", "report_import"]),
                name="fiduciary_client_source_origin_valid",
            ),
        ]

    @property
    def full_name(self):
        if self.first_names:
            return f"{self.first_names} {self.last_names_or_company}".strip()
        return self.last_names_or_company

    def clean(self):
        super().clean()
        self.document_number = self.document_number.strip() if self.document_number else None
        self.first_names = self.first_names.strip()
        self.last_names_or_company = self.last_names_or_company.strip()
        self.phone = self.phone.strip()
        self.email = self.email.strip().lower()
        self.address = self.address.strip()
        self.incomplete_reason = self.incomplete_reason.strip()
        if not self.last_names_or_company:
            raise ValidationError({"last_names_or_company": "Registre apellidos o razon social."})
        if self.source_origin == self.SourceOrigin.MANUAL:
            if not self.document_type or self.document_type == self.DocumentType.UNKNOWN or not self.document_number:
                raise ValidationError("Debe registrar tipo y numero de documento.")
        if self.information_status == self.InformationStatus.COMPLETE and not any([self.phone, self.email]):
            raise ValidationError("Debe registrar al menos un telefono o un correo electronico.")
        if self.source_origin != self.SourceOrigin.MANUAL and self.information_status == self.InformationStatus.INCOMPLETE:
            if not self.incomplete_reason:
                raise ValidationError({"incomplete_reason": "Registre el motivo de informacion incompleta."})

    def __str__(self):
        if self.document_number:
            return f"{self.full_name} ({self.get_document_type_display()} {self.document_number})"
        return f"{self.full_name} ({self.get_document_type_display()})"


class DatedActiveRelation(TimestampedModel):
    start_date = models.DateField("fecha de inicio")
    end_date = models.DateField("fecha de finalizacion", blank=True, null=True)
    is_active = models.BooleanField("vigente", default=True)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "La fecha de finalizacion no puede ser anterior a la inicial."})
        if self.is_active and self.end_date:
            raise ValidationError("Una relacion vigente no debe tener fecha de finalizacion.")
        if not self.is_active and not self.end_date:
            raise ValidationError("Una relacion finalizada debe tener fecha de finalizacion.")


class UnitOwnership(DatedActiveRelation):
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="unit_ownerships")
    property_unit = models.ForeignKey(PropertyUnit, on_delete=models.PROTECT, related_name="ownerships")
    is_primary = models.BooleanField("titular principal", default=False)

    class Meta:
        ordering = ("property_unit__project__name", "property_unit__name", "-is_active", "-is_primary")
        constraints = [
            models.UniqueConstraint(
                fields=["property_unit"],
                condition=Q(is_primary=True, is_active=True),
                name="fiduciary_unit_one_active_primary_owner",
            ),
            models.UniqueConstraint(
                fields=["client", "property_unit"],
                condition=Q(is_active=True),
                name="fiduciary_unit_active_client_unique",
            ),
        ]

    def clean(self):
        super().clean()
        if self.is_active:
            if self.client_id and not self.client.is_active:
                raise ValidationError({"client": "No puede asignar un cliente inactivo a una titularidad vigente."})
            if self.property_unit_id and not self.property_unit.is_active:
                raise ValidationError({"property_unit": "No puede asignar una unidad inactiva a una titularidad vigente."})

    def __str__(self):
        role = "Principal" if self.is_primary else "Secundario"
        return f"{self.client.full_name} - {self.property_unit} ({role})"


class FiduciaryAssignment(DatedActiveRelation):
    assignment_number = models.CharField("numero de encargo fiduciario", max_length=80)
    property_unit = models.ForeignKey(PropertyUnit, on_delete=models.PROTECT, related_name="fiduciary_assignments")
    observations = models.TextField("observaciones", blank=True)

    class Meta:
        ordering = ("property_unit__project__name", "property_unit__name", "-start_date")
        constraints = [
            models.UniqueConstraint(
                fields=["property_unit"],
                condition=Q(is_active=True),
                name="fiduciary_assignment_one_active_per_unit",
            ),
        ]

    def clean(self):
        super().clean()
        self.assignment_number = self.assignment_number.strip()
        self.observations = self.observations.strip()
        if not self.assignment_number:
            raise ValidationError({"assignment_number": "Registre el numero de encargo fiduciario."})
        if self.is_active and self.property_unit_id and not self.property_unit.is_active:
            raise ValidationError({"property_unit": "No puede crear un encargo vigente sobre una unidad inactiva."})
        if self.pk and self.is_active and not self.holders.filter(is_active=True, is_primary=True).exists():
            raise ValidationError("Un encargo vigente debe tener un titular principal vigente.")

    @property
    def active_primary_holder(self):
        return self.holders.filter(is_active=True, is_primary=True).select_related("client").first()

    def __str__(self):
        return self.assignment_number


class FiduciaryAssignmentHolder(DatedActiveRelation):
    assignment = models.ForeignKey(FiduciaryAssignment, on_delete=models.PROTECT, related_name="holders")
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="fiduciary_assignment_holders")
    is_primary = models.BooleanField("titular principal", default=False)

    class Meta:
        ordering = ("assignment__assignment_number", "-is_active", "-is_primary", "client__last_names_or_company")
        constraints = [
            models.UniqueConstraint(
                fields=["assignment"],
                condition=Q(is_primary=True, is_active=True),
                name="fiduciary_assignment_one_active_primary_holder",
            ),
            models.UniqueConstraint(
                fields=["assignment", "client"],
                condition=Q(is_active=True),
                name="fiduciary_assignment_active_client_unique",
            ),
        ]

    def clean(self):
        super().clean()
        if self.is_active and self.client_id and not self.client.is_active:
            raise ValidationError({"client": "No puede asociar un cliente inactivo a un encargo vigente."})
        if self.is_active and self.assignment_id and self.client_id:
            has_valid_ownership = UnitOwnership.objects.filter(
                client=self.client,
                property_unit=self.assignment.property_unit,
                is_active=True,
            ).exists()
            if not has_valid_ownership:
                raise ValidationError(
                    {"client": "El titular del encargo debe tener titularidad vigente sobre la misma unidad."}
                )

    def __str__(self):
        role = "Principal" if self.is_primary else "Secundario"
        return f"{self.assignment.assignment_number} - {self.client.full_name} ({role})"


class ImportBatch(models.Model):
    class ImportType(models.TextChoices):
        HISTORICAL = "historical", "Historico"
        REPORTS = "reports", "Reportes"

    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        ANALYZING = "analyzing", "Analizando"
        AWAITING_RESOLUTION = "awaiting_resolution", "Pendiente de resolucion"
        READY = "ready", "Listo"
        PROCESSING = "processing", "Procesando"
        COMPLETED = "completed", "Completado"
        COMPLETED_WITH_ISSUES = "completed_with_issues", "Completado con incidencias"
        FAILED = "failed", "Fallido"
        CANCELLED = "cancelled", "Cancelado"

    class LoadMode(models.TextChoices):
        SINGLE_FILE = "single_file", "Archivo individual"
        FOLDER = "folder", "Carpeta"

    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="import_batches",
    )
    import_type = models.CharField("tipo de importacion", max_length=16, choices=ImportType.choices)
    load_mode = models.CharField("modo de carga", max_length=16, choices=LoadMode.choices, default=LoadMode.SINGLE_FILE)
    status = models.CharField("estado", max_length=32, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField("fecha de creacion", auto_now_add=True)
    processing_started_at = models.DateTimeField("inicio de procesamiento", blank=True, null=True)
    processing_finished_at = models.DateTimeField("fin de procesamiento", blank=True, null=True)
    total_files = models.PositiveIntegerField("archivos totales", default=0)
    processed_files = models.PositiveIntegerField("archivos procesados", default=0)
    total_rows = models.PositiveIntegerField("filas totales", default=0)
    processed_rows = models.PositiveIntegerField("filas procesadas", default=0)
    issue_count = models.PositiveIntegerField("incidencias", default=0)
    summary = models.TextField("resumen sanitizado", blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=Q(import_type__in=["historical", "reports"]),
                name="fiduciary_import_batch_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(load_mode__in=["single_file", "folder"]),
                name="fiduciary_import_batch_load_mode_valid",
            ),
        ]

    def __str__(self):
        return f"{self.get_import_type_display()} - {self.created_at:%Y-%m-%d %H:%M}"


class ImportedFile(models.Model):
    class FileType(models.TextChoices):
        HISTORICAL = "historical", "Historico"
        REPORT = "report", "Reporte"
        UNKNOWN = "unknown", "Desconocido"

    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        ANALYZING = "analyzing", "Analizando"
        READY = "ready", "Listo"
        PROCESSING = "processing", "Procesando"
        COMPLETED = "completed", "Completado"
        COMPLETED_WITH_ISSUES = "completed_with_issues", "Completado con incidencias"
        FAILED = "failed", "Fallido"
        DUPLICATE = "duplicate", "Duplicado"
        CANCELLED = "cancelled", "Cancelado"

    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="files")
    original_name = models.CharField("nombre original", max_length=255)
    extension = models.CharField("extension", max_length=16)
    size_bytes = models.PositiveBigIntegerField("tamano en bytes")
    sha256 = models.CharField("sha-256", max_length=64)
    file_type = models.CharField("tipo de archivo", max_length=16, choices=FileType.choices, default=FileType.UNKNOWN)
    status = models.CharField("estado", max_length=32, choices=Status.choices, default=Status.PENDING)
    order = models.PositiveIntegerField("orden", default=0)
    processing_started_at = models.DateTimeField("inicio de procesamiento", blank=True, null=True)
    processing_finished_at = models.DateTimeField("fin de procesamiento", blank=True, null=True)
    total_rows = models.PositiveIntegerField("filas totales", default=0)
    processed_rows = models.PositiveIntegerField("filas procesadas", default=0)
    skipped_rows = models.PositiveIntegerField("filas omitidas", default=0)
    error_count = models.PositiveIntegerField("errores", default=0)
    warning_count = models.PositiveIntegerField("advertencias", default=0)
    result_message = models.TextField("mensaje sanitizado", blank=True)
    created_at = models.DateTimeField("fecha de creacion", auto_now_add=True)

    class Meta:
        ordering = ("batch", "order", "original_name")
        indexes = [
            models.Index(fields=["sha256"], name="fiduciary_file_sha_idx"),
            models.Index(fields=["status"], name="fiduciary_file_status_idx"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["batch", "sha256"], name="fiduciary_imported_file_batch_sha_unique"),
            models.CheckConstraint(
                condition=Q(file_type__in=["historical", "report", "unknown"]),
                name="fiduciary_imported_file_type_valid",
            ),
        ]

    def clean(self):
        super().clean()
        self.original_name = self.original_name.strip()
        self.extension = self.extension.strip().lower()
        self.sha256 = self.sha256.strip().lower()
        self.result_message = self.result_message.strip()
        if len(self.sha256) != 64:
            raise ValidationError({"sha256": "El hash SHA-256 debe tener 64 caracteres hexadecimales."})

    def __str__(self):
        return self.original_name


class ImportedSheetResult(models.Model):
    class Visibility(models.TextChoices):
        VISIBLE = "visible", "Visible"
        HIDDEN = "hidden", "Oculta"
        VERY_HIDDEN = "very_hidden", "Muy oculta"

    class Classification(models.TextChoices):
        PROCESSABLE = "processable", "Procesable"
        AUXILIARY = "auxiliary", "Auxiliar"
        SUMMARY = "summary", "Resumen"
        EMPTY = "empty", "Vacia"
        UNKNOWN = "unknown", "Desconocida"

    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        ANALYZED = "analyzed", "Analizada"
        PROCESSED = "processed", "Procesada"
        SKIPPED = "skipped", "Omitida"
        FAILED = "failed", "Fallida"

    imported_file = models.ForeignKey(ImportedFile, on_delete=models.PROTECT, related_name="sheet_results")
    sheet_name = models.CharField("nombre de hoja", max_length=150)
    sheet_index = models.PositiveIntegerField("indice")
    visibility = models.CharField("visibilidad", max_length=16, choices=Visibility.choices, default=Visibility.VISIBLE)
    classification = models.CharField(
        "clasificacion",
        max_length=16,
        choices=Classification.choices,
        default=Classification.UNKNOWN,
    )
    header_row = models.PositiveIntegerField("fila de encabezado", blank=True, null=True)
    detected_dimension = models.CharField("dimension detectada", max_length=50, blank=True)
    analyzed_rows = models.PositiveIntegerField("filas analizadas", default=0)
    processed_rows = models.PositiveIntegerField("filas procesadas", default=0)
    skipped_rows = models.PositiveIntegerField("filas omitidas", default=0)
    error_count = models.PositiveIntegerField("errores", default=0)
    warning_count = models.PositiveIntegerField("advertencias", default=0)
    status = models.CharField("estado", max_length=16, choices=Status.choices, default=Status.PENDING)
    summary = models.TextField("resumen sanitizado", blank=True)
    created_at = models.DateTimeField("fecha de creacion", auto_now_add=True)
    updated_at = models.DateTimeField("fecha de actualizacion", auto_now=True)

    class Meta:
        ordering = ("imported_file", "sheet_index")
        constraints = [
            models.UniqueConstraint(fields=["imported_file", "sheet_name"], name="fiduciary_sheet_file_name_unique"),
        ]

    def __str__(self):
        return f"{self.imported_file} - {self.sheet_name}"


class ImportRowIssue(models.Model):
    class Severity(models.TextChoices):
        INFO = "info", "Informacion"
        WARNING = "warning", "Advertencia"
        ERROR = "error", "Error"
        BLOCKING = "blocking", "Bloqueante"

    class Status(models.TextChoices):
        OPEN = "open", "Abierta"
        RESOLVED = "resolved", "Resuelta"
        IGNORED = "ignored", "Ignorada"

    imported_file = models.ForeignKey(ImportedFile, on_delete=models.PROTECT, related_name="row_issues")
    sheet_result = models.ForeignKey(
        ImportedSheetResult,
        on_delete=models.PROTECT,
        related_name="row_issues",
        blank=True,
        null=True,
    )
    row_number = models.PositiveIntegerField("fila", blank=True, null=True)
    column_letter = models.CharField("columna", max_length=10, blank=True)
    severity = models.CharField("severidad", max_length=16, choices=Severity.choices)
    code = models.CharField("codigo", max_length=80)
    message = models.TextField("mensaje sanitizado")
    status = models.CharField("estado", max_length=16, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField("fecha de creacion", auto_now_add=True)

    class Meta:
        ordering = ("imported_file", "sheet_result", "row_number", "column_letter", "created_at")
        indexes = [
            models.Index(fields=["severity", "status"], name="fiduciary_issue_sev_status_idx"),
            models.Index(fields=["code"], name="fiduciary_issue_code_idx"),
        ]

    def clean(self):
        super().clean()
        self.column_letter = self.column_letter.strip().upper()
        self.code = self.code.strip()
        self.message = self.message.strip()

    def __str__(self):
        return f"{self.severity}: {self.code}"


class DetectedStructureElement(models.Model):
    class InferredKind(models.TextChoices):
        PROJECT = "project", "Proyecto"
        GROUPING_TYPE = "grouping_type", "Tipo de agrupacion"
        STRUCTURAL_GROUP = "structural_group", "Agrupacion"
        PROPERTY_UNIT = "property_unit", "Unidad"
        UNKNOWN = "unknown", "Desconocido"

    class Status(models.TextChoices):
        DETECTED = "detected", "Detectado"
        AUTO_MATCHED = "auto_matched", "Asociado automaticamente"
        NEEDS_REVIEW = "needs_review", "Requiere revision"
        RESOLVED = "resolved", "Resuelto"
        IGNORED = "ignored", "Ignorado"

    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="detected_elements")
    imported_file = models.ForeignKey(
        ImportedFile,
        on_delete=models.PROTECT,
        related_name="detected_elements",
        blank=True,
        null=True,
    )
    raw_value = models.CharField("valor detectado", max_length=255)
    normalized_value = models.CharField("valor normalizado", max_length=255)
    inferred_kind = models.CharField("tipo inferido", max_length=32, choices=InferredKind.choices, default=InferredKind.UNKNOWN)
    structural_context = models.JSONField("contexto estructural sanitizado", default=dict, blank=True)
    occurrence_count = models.PositiveIntegerField("ocurrencias", default=1)
    confidence = models.DecimalField("confianza", max_digits=5, decimal_places=4, blank=True, null=True)
    status = models.CharField("estado", max_length=24, choices=Status.choices, default=Status.DETECTED)
    created_at = models.DateTimeField("fecha de creacion", auto_now_add=True)
    updated_at = models.DateTimeField("fecha de actualizacion", auto_now=True)

    class Meta:
        ordering = ("batch", "inferred_kind", "normalized_value")
        indexes = [
            models.Index(fields=["batch", "normalized_value"], name="fiduciary_detected_norm_idx"),
            models.Index(fields=["status"], name="fiduciary_detected_status_idx"),
        ]

    def clean(self):
        super().clean()
        self.raw_value = self.raw_value.strip()
        self.normalized_value = self.normalized_value.strip()
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValidationError({"confidence": "La confianza debe estar entre 0 y 1."})

    def __str__(self):
        return f"{self.get_inferred_kind_display()}: {self.normalized_value}"


class ImportResolution(models.Model):
    class Action(models.TextChoices):
        ASSOCIATE_EXISTING = "associate_existing", "Asociar existente"
        CREATE_NEW = "create_new", "Crear nuevo"
        IGNORE = "ignore", "Ignorar"
        UNRESOLVED = "unresolved", "Sin resolver"

    class Status(models.TextChoices):
        DRAFT = "draft", "Borrador"
        APPLIED = "applied", "Aplicada"
        REJECTED = "rejected", "Rechazada"

    detected_element = models.OneToOneField(
        DetectedStructureElement,
        on_delete=models.PROTECT,
        related_name="resolution",
    )
    action = models.CharField("accion", max_length=24, choices=Action.choices, default=Action.UNRESOLVED)
    target_kind = models.CharField(
        "tipo destino",
        max_length=32,
        choices=DetectedStructureElement.InferredKind.choices,
        default=DetectedStructureElement.InferredKind.UNKNOWN,
    )
    target_project = models.ForeignKey(
        "real_estate.Project",
        on_delete=models.PROTECT,
        related_name="import_target_resolutions",
        blank=True,
        null=True,
    )
    target_grouping_type = models.ForeignKey(
        "real_estate.GroupingType",
        on_delete=models.PROTECT,
        related_name="import_target_resolutions",
        blank=True,
        null=True,
    )
    target_structural_group = models.ForeignKey(
        "real_estate.StructuralGroup",
        on_delete=models.PROTECT,
        related_name="import_target_resolutions",
        blank=True,
        null=True,
    )
    target_property_unit = models.ForeignKey(
        "real_estate.PropertyUnit",
        on_delete=models.PROTECT,
        related_name="import_target_resolutions",
        blank=True,
        null=True,
    )
    parent_project = models.ForeignKey(
        "real_estate.Project",
        on_delete=models.PROTECT,
        related_name="import_parent_resolutions",
        blank=True,
        null=True,
    )
    parent_grouping_type = models.ForeignKey(
        "real_estate.GroupingType",
        on_delete=models.PROTECT,
        related_name="import_parent_resolutions",
        blank=True,
        null=True,
    )
    parent_structural_group = models.ForeignKey(
        "real_estate.StructuralGroup",
        on_delete=models.PROTECT,
        related_name="import_parent_resolutions",
        blank=True,
        null=True,
    )
    create_code = models.CharField("codigo a crear", max_length=80, blank=True)
    create_name = models.CharField("nombre a crear", max_length=180, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="import_resolutions",
        blank=True,
        null=True,
    )
    resolved_at = models.DateTimeField("fecha de resolucion", blank=True, null=True)
    status = models.CharField("estado", max_length=16, choices=Status.choices, default=Status.DRAFT)

    class Meta:
        ordering = ("detected_element",)

    def clean(self):
        super().clean()
        self.create_code = self.create_code.strip()
        self.create_name = self.create_name.strip()
        if self.action == self.Action.CREATE_NEW and not (self.create_code or self.create_name):
            raise ValidationError("Registre codigo, nombre o ambos para crear el elemento.")
        if self.status == self.Status.APPLIED and not self.resolved_at:
            self.resolved_at = timezone.now()

    def __str__(self):
        return f"{self.detected_element} - {self.get_action_display()}"


class Payment(TimestampedModel):
    class DatePrecision(models.TextChoices):
        EXACT = "exact", "Fecha exacta"
        MONTH = "month", "Periodo mensual"

    class MovementType(models.TextChoices):
        HISTORICAL_PAYMENT = "historical_payment", "Pago historico"
        ADDITION = "addition", "Adicion"
        WITHDRAWAL = "withdrawal", "Retiro"

    assignment = models.ForeignKey(FiduciaryAssignment, on_delete=models.PROTECT, related_name="payments")
    exact_date = models.DateField("fecha exacta", blank=True, null=True)
    period_year = models.PositiveSmallIntegerField("ano del periodo", blank=True, null=True)
    period_month = models.PositiveSmallIntegerField("mes del periodo", blank=True, null=True)
    date_precision = models.CharField("precision de fecha", max_length=8, choices=DatePrecision.choices)
    amount = models.DecimalField("valor", max_digits=18, decimal_places=2)
    concept = models.CharField("concepto", max_length=180, blank=True, null=True)
    movement_type = models.CharField("tipo de movimiento", max_length=24, choices=MovementType.choices)
    source_file = models.ForeignKey(ImportedFile, on_delete=models.PROTECT, related_name="payments")
    source_sheet = models.CharField("hoja origen", max_length=150)
    source_row = models.PositiveIntegerField("fila origen")
    source_column = models.CharField("columna origen", max_length=10, blank=True, null=True)
    source_header = models.CharField("encabezado origen", max_length=180, blank=True, null=True)
    source_had_formula = models.BooleanField("origen con formula", default=False)
    imported_at = models.DateTimeField("fecha de importacion", auto_now_add=True)

    class Meta:
        ordering = ("assignment", "-exact_date", "-period_year", "-period_month", "-created_at")
        indexes = [
            models.Index(fields=["assignment", "exact_date", "amount"], name="fiduciary_payment_exact_idx"),
            models.Index(fields=["assignment", "period_year", "period_month", "amount"], name="fiduciary_payment_month_idx"),
            models.Index(fields=["source_file", "source_sheet", "source_row", "source_column"], name="fiduciary_payment_source_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="fiduciary_payment_amount_positive"),
            models.CheckConstraint(
                condition=Q(date_precision__in=["exact", "month"]),
                name="fiduciary_payment_date_precision_valid",
            ),
            models.CheckConstraint(
                condition=Q(movement_type__in=["historical_payment", "addition", "withdrawal"]),
                name="fiduciary_payment_movement_type_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(date_precision="exact", exact_date__isnull=False, period_year__isnull=True, period_month__isnull=True)
                    | Q(date_precision="month", exact_date__isnull=True, period_year__isnull=False, period_month__isnull=False)
                ),
                name="fiduciary_payment_one_date_mode",
            ),
            models.CheckConstraint(
                condition=Q(period_month__isnull=True) | Q(period_month__gte=1, period_month__lte=12),
                name="fiduciary_payment_period_month_range",
            ),
            models.UniqueConstraint(
                fields=["assignment", "exact_date", "amount"],
                condition=Q(date_precision="exact"),
                name="fiduciary_payment_exact_unique",
            ),
            models.UniqueConstraint(
                fields=["assignment", "period_year", "period_month", "amount"],
                condition=Q(date_precision="month"),
                name="fiduciary_payment_month_unique",
            ),
        ]

    def clean(self):
        super().clean()
        self.concept = self.concept.strip() if self.concept else None
        self.source_sheet = self.source_sheet.strip()
        self.source_column = self.source_column.strip().upper() if self.source_column else None
        self.source_header = self.source_header.strip() if self.source_header else None
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "El valor debe ser mayor que cero."})
        if self.date_precision == self.DatePrecision.EXACT:
            if not self.exact_date:
                raise ValidationError({"exact_date": "Registre la fecha exacta del pago."})
            if self.period_year or self.period_month:
                raise ValidationError("Un pago con fecha exacta no debe tener periodo mensual.")
        elif self.date_precision == self.DatePrecision.MONTH:
            if self.exact_date:
                raise ValidationError("Un pago mensual historico no debe tener fecha exacta.")
            if not self.period_year or not self.period_month:
                raise ValidationError("Registre ano y mes del periodo.")
            if not 1 <= self.period_month <= 12:
                raise ValidationError({"period_month": "El mes debe estar entre 1 y 12."})
        else:
            raise ValidationError({"date_precision": "Seleccione una precision de fecha valida."})

    def __str__(self):
        return f"{self.assignment} - {self.amount}"


class ImportNovelty(models.Model):
    class NoveltyType(models.TextChoices):
        ASSIGNMENT_CHANGE = "assignment_change", "Cambio de encargo"
        TRANSFER = "transfer", "Cesion"
        RELOCATION = "relocation", "Traslado"
        WITHDRAWAL = "withdrawal", "Retiro"
        AMBIGUOUS_HOLDER = "ambiguous_holder", "Titular ambiguo"
        INCOMPATIBLE_STRUCTURE = "incompatible_structure", "Estructura incompatible"

    class Status(models.TextChoices):
        DETECTED = "detected", "Detectada"
        PENDING_REVIEW = "pending_review", "Pendiente de revision"
        RESOLVED = "resolved", "Resuelta"
        DISMISSED = "dismissed", "Descartada"

    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="novelties")
    imported_file = models.ForeignKey(
        ImportedFile,
        on_delete=models.PROTECT,
        related_name="novelties",
        blank=True,
        null=True,
    )
    sheet_result = models.ForeignKey(
        ImportedSheetResult,
        on_delete=models.PROTECT,
        related_name="novelties",
        blank=True,
        null=True,
    )
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="import_novelties", blank=True, null=True)
    property_unit = models.ForeignKey(
        PropertyUnit,
        on_delete=models.PROTECT,
        related_name="import_novelties",
        blank=True,
        null=True,
    )
    assignment = models.ForeignKey(
        FiduciaryAssignment,
        on_delete=models.PROTECT,
        related_name="import_novelties",
        blank=True,
        null=True,
    )
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="import_novelties", blank=True, null=True)
    novelty_type = models.CharField("tipo de novedad", max_length=32, choices=NoveltyType.choices)
    status = models.CharField("estado", max_length=24, choices=Status.choices, default=Status.DETECTED)
    description = models.TextField("descripcion sanitizada", blank=True)
    created_at = models.DateTimeField("fecha de creacion", auto_now_add=True)
    updated_at = models.DateTimeField("fecha de actualizacion", auto_now=True)

    class Meta:
        ordering = ("batch", "created_at")
        indexes = [
            models.Index(fields=["novelty_type", "status"], name="fiduciary_novelty_status_idx"),
        ]

    def __str__(self):
        return f"{self.get_novelty_type_display()} - {self.get_status_display()}"
