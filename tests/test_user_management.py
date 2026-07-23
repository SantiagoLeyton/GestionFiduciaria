import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

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
def test_user_management_requires_authentication(client):
    response = client.get(reverse("user_list"))

    assert response.status_code == 302
    assert reverse("login") in response["Location"]


@pytest.mark.django_db
def test_accounting_admin_can_access_user_management(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    response = client.get(reverse("user_list"))

    assert response.status_code == 200
    assert "Gestion de Usuarios" in response.content.decode()


@pytest.mark.django_db
def test_technical_superuser_can_access_user_management(client, technical_superuser):
    client.force_login(technical_superuser)

    response = client.get(reverse("user_list"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_commercial_user_is_rejected_from_user_management(client, commercial_user):
    client.force_login(commercial_user)

    response = client.get(reverse("user_list"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_create_user_from_management(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_create"),
        {
            "first_name": "Ana",
            "last_name": "Silva",
            "username": "asilva",
            "email": "ASILVA@CENTENARIO.COM",
            "role": User.Role.COMMERCIAL,
            "is_active": "on",
            "password1": "StrongPass123",
            "password2": "StrongPass123",
        },
    )

    assert response.status_code == 302
    user = User.objects.get(username="asilva")
    assert user.email == "asilva@centenario.com"
    assert user.role == User.Role.COMMERCIAL
    assert user.is_active is True
    assert user.check_password("StrongPass123")
    assert user.password != "StrongPass123"


@pytest.mark.django_db
def test_create_user_rejects_duplicate_username(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_create"),
        {
            "first_name": "Otro",
            "last_name": "Usuario",
            "username": commercial_user.username.upper(),
            "email": "otro@centenario.com",
            "role": User.Role.COMMERCIAL,
            "is_active": "on",
            "password1": "StrongPass123",
            "password2": "StrongPass123",
        },
    )

    assert response.status_code == 200
    assert "Ya existe un usuario" in response.content.decode()


@pytest.mark.django_db
def test_create_user_rejects_duplicate_email_case_insensitive(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_create"),
        {
            "first_name": "Otro",
            "last_name": "Usuario",
            "username": "otro",
            "email": commercial_user.email.upper(),
            "role": User.Role.COMMERCIAL,
            "is_active": "on",
            "password1": "StrongPass123",
            "password2": "StrongPass123",
        },
    )

    assert response.status_code == 200
    assert "Ya existe un usuario con este correo" in response.content.decode()


@pytest.mark.django_db
def test_create_user_rejects_invalid_role(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_create"),
        {
            "first_name": "Rol",
            "last_name": "Invalido",
            "username": "rolinvalido",
            "email": "rolinvalido@centenario.com",
            "role": "auditor",
            "is_active": "on",
            "password1": "StrongPass123",
            "password2": "StrongPass123",
        },
    )

    assert response.status_code == 200
    assert not User.objects.filter(username="rolinvalido").exists()


@pytest.mark.django_db
def test_create_user_uses_password_validators(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_create"),
        {
            "first_name": "Clave",
            "last_name": "Debil",
            "username": "debil",
            "email": "debil@centenario.com",
            "role": User.Role.COMMERCIAL,
            "is_active": "on",
            "password1": "123",
            "password2": "123",
        },
    )

    assert response.status_code == 200
    assert not User.objects.filter(username="debil").exists()


@pytest.mark.django_db
def test_update_user_authorized_fields(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_update", args=[commercial_user.pk]),
        {
            "first_name": "Carlos",
            "last_name": "Actualizado",
            "username": "comercial2",
            "email": "comercial2@centenario.com",
            "role": User.Role.ACCOUNTING_ADMIN,
            "is_active": "on",
        },
    )

    commercial_user.refresh_from_db()
    assert response.status_code == 302
    assert commercial_user.username == "comercial2"
    assert commercial_user.role == User.Role.ACCOUNTING_ADMIN


@pytest.mark.django_db
def test_commercial_user_cannot_update_user_by_direct_url(client, commercial_user, accounting_admin_user):
    client.force_login(commercial_user)

    response = client.post(
        reverse("user_update", args=[accounting_admin_user.pk]),
        {
            "first_name": "Marta",
            "last_name": "Editada",
            "username": accounting_admin_user.username,
            "email": accounting_admin_user.email,
            "role": User.Role.COMMERCIAL,
            "is_active": "",
        },
    )

    accounting_admin_user.refresh_from_db()
    assert response.status_code == 403
    assert accounting_admin_user.role == User.Role.ACCOUNTING_ADMIN
    assert accounting_admin_user.is_active is True


@pytest.mark.django_db
def test_deactivate_user(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(reverse("user_status", args=[commercial_user.pk, "deactivate"]))

    commercial_user.refresh_from_db()
    assert response.status_code == 302
    assert commercial_user.is_active is False


@pytest.mark.django_db
def test_activate_user(client, accounting_admin_user, commercial_user):
    commercial_user.is_active = False
    commercial_user.save(update_fields=["is_active"])
    client.force_login(accounting_admin_user)

    response = client.post(reverse("user_status", args=[commercial_user.pk, "activate"]))

    commercial_user.refresh_from_db()
    assert response.status_code == 302
    assert commercial_user.is_active is True


@pytest.mark.django_db
def test_admin_cannot_deactivate_own_account(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    response = client.post(reverse("user_status", args=[accounting_admin_user.pk, "deactivate"]))

    accounting_admin_user.refresh_from_db()
    assert response.status_code == 302
    assert accounting_admin_user.is_active is True


@pytest.mark.django_db
def test_admin_cannot_change_own_role_or_status_by_post_manipulation(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_update", args=[accounting_admin_user.pk]),
        {
            "first_name": accounting_admin_user.first_name,
            "last_name": accounting_admin_user.last_name,
            "username": accounting_admin_user.username,
            "email": accounting_admin_user.email,
            "role": User.Role.COMMERCIAL,
            "is_active": "",
        },
    )

    accounting_admin_user.refresh_from_db()
    assert response.status_code == 302
    assert accounting_admin_user.role == User.Role.ACCOUNTING_ADMIN
    assert accounting_admin_user.is_active is True


@pytest.mark.django_db
def test_user_deletion_endpoint_does_not_exist(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(f"/accounts/users/{commercial_user.pk}/delete/")

    assert response.status_code == 404
    assert get_user_model().objects.filter(pk=commercial_user.pk).exists()


@pytest.mark.django_db
def test_user_management_navigation_by_role(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)
    admin_response = client.get(reverse("home"))
    assert reverse("user_list") in admin_response.content.decode()

    client.force_login(commercial_user)
    commercial_response = client.get(reverse("home"))
    assert reverse("user_list") not in commercial_response.content.decode()


@pytest.mark.django_db
def test_user_list_filters_by_role_and_status(client, accounting_admin_user, commercial_user):
    commercial_user.is_active = False
    commercial_user.save(update_fields=["is_active"])
    client.force_login(accounting_admin_user)

    response = client.get(
        reverse("user_list"),
        {"role": User.Role.COMMERCIAL, "status": "inactive", "q": "comercial"},
    )

    content = response.content.decode()
    assert response.status_code == 200
    assert commercial_user.username in content
    assert accounting_admin_user.username not in content
