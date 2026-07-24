from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class ActiveEntity(models.Model):
    code = models.CharField("codigo", max_length=50)
    name = models.CharField("nombre", max_length=150)
    description = models.TextField("descripcion", blank=True)
    is_active = models.BooleanField("activo", default=True)
    last_change_reason = models.TextField("ultimo motivo de modificacion", blank=True)
    created_at = models.DateTimeField("fecha de creacion", auto_now_add=True)
    updated_at = models.DateTimeField("fecha de actualizacion", auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} - {self.name}"


class Project(ActiveEntity):
    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=["code"], name="real_estate_project_code_unique"),
        ]
        verbose_name = "project"
        verbose_name_plural = "projects"


class GroupingType(ActiveEntity):
    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=["code"], name="real_estate_grouping_type_code_unique"),
        ]
        verbose_name = "grouping type"
        verbose_name_plural = "grouping types"


class StructuralGroup(ActiveEntity):
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="structural_groups")
    grouping_type = models.ForeignKey(GroupingType, on_delete=models.PROTECT, related_name="structural_groups")
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="children",
    )

    class Meta:
        ordering = ("project__name", "parent__name", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["project", "code"],
                condition=Q(parent__isnull=True),
                name="real_estate_structural_group_root_code_unique",
            ),
            models.UniqueConstraint(
                fields=["parent", "code"],
                condition=Q(parent__isnull=False),
                name="real_estate_structural_group_parent_code_unique",
            ),
        ]
        verbose_name = "structural group"
        verbose_name_plural = "structural groups"

    def clean(self):
        super().clean()
        if self.parent and self.parent.project_id != self.project_id:
            raise ValidationError({"parent": "La agrupacion padre debe pertenecer al mismo proyecto."})

        ancestor = self.parent
        while ancestor:
            if self.pk and ancestor.pk == self.pk:
                raise ValidationError({"parent": "La jerarquia no puede contener ciclos."})
            ancestor = ancestor.parent


class PropertyUnit(ActiveEntity):
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="property_units")
    structural_group = models.ForeignKey(
        StructuralGroup,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="property_units",
    )

    class Meta:
        ordering = ("project__name", "structural_group__name", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["project", "code"],
                condition=Q(structural_group__isnull=True),
                name="real_estate_property_unit_project_code_unique",
            ),
            models.UniqueConstraint(
                fields=["structural_group", "code"],
                condition=Q(structural_group__isnull=False),
                name="real_estate_property_unit_group_code_unique",
            ),
        ]
        verbose_name = "property unit"
        verbose_name_plural = "property units"

    def clean(self):
        super().clean()
        if self.structural_group and self.structural_group.project_id != self.project_id:
            raise ValidationError(
                {"structural_group": "La unidad no puede asociarse a una agrupacion de otro proyecto."}
            )
