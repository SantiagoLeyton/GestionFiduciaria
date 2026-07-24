from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from .managers import CustomUserManager


class User(AbstractUser):
    class Role(models.TextChoices):
        COMMERCIAL = "commercial", "Comercial"
        ACCOUNTING_ADMIN = "accounting_admin", "Administrador de Contabilidad"

    email = models.EmailField("correo electronico", unique=True)
    role = models.CharField("rol", max_length=32, choices=Role.choices)
    objects = CustomUserManager()
    REQUIRED_FIELDS = ["email"]

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"
        constraints = [
            models.CheckConstraint(
                condition=Q(role__in=["commercial", "accounting_admin"]),
                name="users_user_role_official_values",
            ),
            models.UniqueConstraint(Lower("email"), name="users_user_email_ci_unique"),
        ]

    @property
    def full_name(self):
        return self.get_full_name() or self.username

    @property
    def role_label(self):
        if self.role == self.Role.ACCOUNTING_ADMIN:
            return "Contabilidad"
        return self.get_role_display()

    def is_accounting_admin(self):
        return self.role == self.Role.ACCOUNTING_ADMIN

    def is_commercial(self):
        return self.role == self.Role.COMMERCIAL
