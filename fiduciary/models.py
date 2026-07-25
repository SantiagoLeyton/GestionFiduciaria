from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

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

    class InformationStatus(models.TextChoices):
        COMPLETE = "complete", "Completo"
        INCOMPLETE = "incomplete", "Incompleto"

    document_type = models.CharField("tipo de documento", max_length=16, choices=DocumentType.choices)
    document_number = models.CharField("numero de documento", max_length=50)
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
    is_active = models.BooleanField("activo", default=True)

    class Meta:
        ordering = ("last_names_or_company", "first_names", "document_number")
        constraints = [
            models.UniqueConstraint(
                fields=["document_type", "document_number"],
                name="fiduciary_client_document_unique",
            ),
            models.CheckConstraint(
                condition=Q(information_status__in=["complete", "incomplete"]),
                name="fiduciary_client_information_status_valid",
            ),
        ]

    @property
    def full_name(self):
        if self.first_names:
            return f"{self.first_names} {self.last_names_or_company}".strip()
        return self.last_names_or_company

    def clean(self):
        super().clean()
        self.document_number = self.document_number.strip()
        self.first_names = self.first_names.strip()
        self.last_names_or_company = self.last_names_or_company.strip()
        self.phone = self.phone.strip()
        self.email = self.email.strip().lower()
        self.address = self.address.strip()
        if not self.document_type or not self.document_number:
            raise ValidationError("Debe registrar tipo y numero de documento.")
        if not self.last_names_or_company:
            raise ValidationError({"last_names_or_company": "Registre apellidos o razon social."})
        if self.information_status == self.InformationStatus.COMPLETE and not any([self.phone, self.email]):
            raise ValidationError("Debe registrar al menos un telefono o un correo electronico.")

    def __str__(self):
        return f"{self.full_name} ({self.get_document_type_display()} {self.document_number})"


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
