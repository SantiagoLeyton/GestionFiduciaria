from django.contrib.auth.models import UserManager


class CustomUserManager(UserManager):
    def _create_user(self, username, email, password, **extra_fields):
        if not extra_fields.get("role"):
            raise ValueError("El rol del usuario es obligatorio.")
        if email:
            email = self.normalize_email(email).lower()
        return super()._create_user(username, email, password, **extra_fields)

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        from .models import User

        extra_fields.setdefault("role", User.Role.ACCOUNTING_ADMIN)
        return super().create_superuser(username, email, password, **extra_fields)
