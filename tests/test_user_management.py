import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from users.models import User


@pytest.fixture
def technical_superuser(db):
    user = User.objects.create_user(
        username="technical",
        email="technical@centenario.com",
        password="StrongPass123",
        first_name="Soporte",
        last_name="Tecnico",
        role=User.Role.COMMERCIAL,
    )
    user.is_staff = True
    user.is_superuser = True
    user.save(update_fields=["is_staff", "is_superuser"])
    return user


@pytest.mark.django_db
def test_user_list_requires_authentication(client):
    response = client.get(reverse("user_list"))

    assert response.status_code == 302
    assert reverse("login") in response["Location"]


@pytest.mark.django_db
def test_accounting_admin_can_consult_users(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    response = client.get(reverse("user_list"))

    assert response.status_code == 200
    assert "Usuarios" in response.content.decode()


@pytest.mark.django_db
def test_technical_superuser_can_consult_users(client, technical_superuser):
    client.force_login(technical_superuser)

    response = client.get(reverse("user_list"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_commercial_user_can_consult_users_without_management_actions(client, commercial_user):
    client.force_login(commercial_user)

    response = client.get(reverse("user_list"))
    content = response.content.decode()

    assert response.status_code == 200
    assert "Nuevo usuario" not in content
    assert "Editar" not in content
    assert "Activar" not in content
    assert "Desactivar" not in content


@pytest.mark.django_db
def test_user_list_shows_contabilidad_label(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    response = client.get(reverse("user_list"))
    content = response.content.decode()

    assert "Contabilidad" in content
    assert "Administrador de Contabilidad" not in content


@pytest.mark.django_db
def test_user_list_does_not_show_username_column_or_values(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.get(reverse("user_list"))
    content = response.content.decode()

    assert "<th>Usuario</th>" not in content
    assert f"<td>{commercial_user.username}</td>" not in content


@pytest.mark.django_db
def test_old_create_url_is_blocked(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_create"),
        {
            "first_name": "Ana",
            "last_name": "Silva",
            "username": "asilva",
            "email": "asilva@centenario.com",
            "role": User.Role.COMMERCIAL,
            "is_active": "on",
            "password1": "StrongPass123",
            "password2": "StrongPass123",
        },
    )

    assert response.status_code == 403
    assert not get_user_model().objects.filter(email="asilva@centenario.com").exists()


@pytest.mark.django_db
def test_old_update_url_is_blocked(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_update", args=[commercial_user.pk]),
        {
            "first_name": "Carlos",
            "last_name": "Actualizado",
            "email": "nuevo@centenario.com",
            "role": User.Role.ACCOUNTING_ADMIN,
            "is_active": "",
        },
    )

    commercial_user.refresh_from_db()
    assert response.status_code == 403
    assert commercial_user.email == "comercial@centenario.com"
    assert commercial_user.role == User.Role.COMMERCIAL
    assert commercial_user.is_active is True


@pytest.mark.django_db
def test_old_status_url_is_blocked(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(reverse("user_status", args=[commercial_user.pk, "deactivate"]))

    commercial_user.refresh_from_db()
    assert response.status_code == 403
    assert commercial_user.is_active is True


@pytest.mark.django_db
def test_user_deletion_endpoint_does_not_exist(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(f"/accounts/users/{commercial_user.pk}/delete/")

    assert response.status_code == 403
    assert get_user_model().objects.filter(pk=commercial_user.pk).exists()


@pytest.mark.django_db
def test_user_list_shows_last_login_and_never(client, accounting_admin_user, commercial_user):
    accounting_admin_user.last_login = timezone.localtime(timezone.now())
    accounting_admin_user.save(update_fields=["last_login"])
    commercial_user.last_login = None
    commercial_user.save(update_fields=["last_login"])
    client.force_login(accounting_admin_user)

    response = client.get(reverse("user_list"))
    content = response.content.decode()

    assert "Ultimo acceso" in content
    assert accounting_admin_user.last_login.strftime("%d/%m/%Y") in content
    assert "Nunca" in content


@pytest.mark.django_db
def test_login_updates_last_login(client, commercial_user):
    assert commercial_user.last_login is None

    response = client.post(reverse("login"), {"username": commercial_user.email, "password": "StrongPass123"})

    commercial_user.refresh_from_db()
    assert response.status_code == 302
    assert commercial_user.last_login is not None


@pytest.mark.django_db
def test_blank_space_search_is_treated_as_empty(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.get(reverse("user_list"), {"q": "     "})
    content = response.content.decode()

    assert response.status_code == 200
    assert accounting_admin_user.email in content
    assert commercial_user.email in content
    assert "errorlist" not in content


@pytest.mark.django_db
def test_login_by_email_still_works(client, commercial_user):
    response = client.post(reverse("login"), {"username": commercial_user.email, "password": "StrongPass123"})

    assert response.status_code == 302
    assert response["Location"] == reverse("home")
