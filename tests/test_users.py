import pytest
from django.contrib.auth import authenticate, get_user_model
from django.test import RequestFactory

from users.models import User
from users.permissions import RoleRequiredMixin, role_required


@pytest.mark.django_db
def test_user_model_required_fields():
    user = get_user_model().objects.create_user(
        username="jperez",
        email="jperez@centenario.com",
        password="StrongPass123",
        first_name="Juan",
        last_name="Perez",
        role=User.Role.ACCOUNTING_ADMIN,
    )

    assert user.email == "jperez@centenario.com"
    assert user.full_name == "Juan Perez"
    assert user.is_accounting_admin()
    assert user.is_active


@pytest.mark.django_db
def test_authenticate_by_username(commercial_user):
    user = authenticate(username="comercial", password="StrongPass123")
    assert user == commercial_user


@pytest.mark.django_db
def test_authenticate_by_email(commercial_user):
    user = authenticate(username="comercial@centenario.com", password="StrongPass123")
    assert user == commercial_user


@pytest.mark.django_db
def test_authenticate_by_email_is_case_insensitive(commercial_user):
    user = authenticate(username="COMERCIAL@CENTENARIO.COM", password="StrongPass123")
    assert user == commercial_user


@pytest.mark.django_db
def test_authenticate_strips_identifier_spaces(commercial_user):
    user = authenticate(username="  comercial  ", password="StrongPass123")
    assert user == commercial_user


@pytest.mark.django_db
def test_password_is_not_trimmed():
    user = get_user_model().objects.create_user(
        username="literalpass",
        email="literalpass@centenario.com",
        password="  StrongPass123  ",
        role=User.Role.COMMERCIAL,
    )

    assert authenticate(username="literalpass", password="  StrongPass123  ") == user
    assert authenticate(username="literalpass", password="StrongPass123") is None


@pytest.mark.django_db
def test_inactive_user_cannot_authenticate(commercial_user):
    commercial_user.is_active = False
    commercial_user.save()

    user = authenticate(username="comercial", password="StrongPass123")
    assert user is None


def test_role_required_blocks_users_without_permission(commercial_user):
    factory = RequestFactory()
    request = factory.get("/")
    request.user = commercial_user

    @role_required(User.Role.ACCOUNTING_ADMIN)
    def protected_view(request):
        return None

    with pytest.raises(Exception) as exc:
        protected_view(request)

    assert exc.type.__name__ == "PermissionDenied"


def test_role_required_mixin_allows_configured_role(accounting_admin_user):
    class DummyView(RoleRequiredMixin):
        allowed_roles = (User.Role.ACCOUNTING_ADMIN,)

    request = RequestFactory().get("/")
    request.user = accounting_admin_user

    assert DummyView().allowed_roles == (User.Role.ACCOUNTING_ADMIN,)


@pytest.mark.django_db
def test_create_user_requires_role():
    with pytest.raises(ValueError):
        get_user_model().objects.create_user(
            username="sinrol",
            email="sinrol@centenario.com",
            password="StrongPass123",
        )


@pytest.mark.django_db
def test_create_superuser_gets_accounting_admin_role():
    user = get_user_model().objects.create_superuser(
        username="admin",
        email="admin@centenario.com",
        password="StrongPass123",
    )

    assert user.is_superuser
    assert user.is_staff
    assert user.role == User.Role.ACCOUNTING_ADMIN


@pytest.mark.django_db
def test_email_is_normalized_on_create_user():
    user = get_user_model().objects.create_user(
        username="correo",
        email="Correo@CENTENARIO.COM",
        password="StrongPass123",
        role=User.Role.COMMERCIAL,
    )

    assert user.email == "correo@centenario.com"
