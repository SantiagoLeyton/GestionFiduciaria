import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth import authenticate
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.test import override_settings
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
    assert "Gestion de cuentas" in response.content.decode()


@pytest.mark.django_db
def test_technical_superuser_without_accounting_role_cannot_consult_users(client, technical_superuser):
    client.force_login(technical_superuser)

    response = client.get(reverse("user_list"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_commercial_user_can_consult_users_without_management_actions(client, commercial_user):
    client.force_login(commercial_user)

    response = client.get(reverse("user_list"))

    assert response.status_code == 403


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
def test_accounting_admin_can_create_user(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_create"),
        {
            "first_name": "Ana",
            "last_name": "Silva",
            "email": "asilva@centenario.com",
            "role": User.Role.COMMERCIAL,
            "is_active": "on",
        },
    )

    assert response.status_code == 302
    created = get_user_model().objects.get(email="asilva@centenario.com")
    assert created.role == User.Role.COMMERCIAL
    assert created.username
    assert created.has_usable_password()


@pytest.mark.django_db
def test_managed_user_create_requires_first_and_last_name(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_create"),
        {
            "first_name": "   ",
            "last_name": "",
            "email": "sin-nombre@centenario.com",
            "role": User.Role.COMMERCIAL,
            "is_active": "on",
        },
    )

    content = response.content.decode()
    assert response.status_code == 200
    assert not get_user_model().objects.filter(email="sin-nombre@centenario.com").exists()
    assert "Registre el nombre del usuario" in content
    assert "Registre el apellido del usuario" in content


@pytest.mark.django_db
def test_create_user_reuses_logically_deleted_email_without_old_access(client, accounting_admin_user):
    client.force_login(accounting_admin_user)
    deleted = User.objects.create_user(
        username="eliminado",
        email="reuso@centenario.com",
        password="OldPass123",
        first_name="Nombre",
        last_name="Anterior",
        role=User.Role.ACCOUNTING_ADMIN,
    )
    group = Group.objects.create(name="Grupo anterior")
    permission = Permission.objects.first()
    deleted.groups.add(group)
    if permission:
        deleted.user_permissions.add(permission)
    deleted_pk = deleted.pk
    delete_response = client.post(reverse("user_delete", args=[deleted.pk]), {"reason": "Cuenta anterior cerrada"})
    assert delete_response.status_code == 302

    response = client.post(
        reverse("user_create"),
        {
            "first_name": "Nombre Nuevo",
            "last_name": "Apellido Nuevo",
            "email": "reuso@centenario.com",
            "role": User.Role.COMMERCIAL,
            "is_active": "on",
        },
    )

    reused = User.objects.get(pk=deleted_pk)
    assert response.status_code == 302
    assert User.objects.filter(email="reuso@centenario.com").count() == 1
    assert reused.is_deleted is False
    assert reused.is_active is True
    assert reused.first_name == "Nombre Nuevo"
    assert reused.last_name == "Apellido Nuevo"
    assert reused.role == User.Role.COMMERCIAL
    assert reused.groups.count() == 0
    assert reused.user_permissions.count() == 0
    assert authenticate(username="reuso@centenario.com", password="OldPass123") is None
    assert LogEntry.objects.filter(
        object_id=str(reused.pk),
        change_message__icontains="reutilizando correo de usuario eliminado",
    ).exists()
    assert LogEntry.objects.filter(object_id=str(reused.pk), change_message__icontains="ELIMINAR_USUARIO").exists()


@pytest.mark.django_db
def test_create_user_still_rejects_active_or_inactive_existing_email(client, accounting_admin_user):
    client.force_login(accounting_admin_user)
    active = User.objects.create_user(
        username="activo-email",
        email="duplicado@centenario.com",
        password="StrongPass123",
        first_name="Activo",
        last_name="Correo",
        role=User.Role.COMMERCIAL,
    )

    active_response = client.post(
        reverse("user_create"),
        {
            "first_name": "Otro",
            "last_name": "Usuario",
            "email": active.email,
            "role": User.Role.COMMERCIAL,
            "is_active": "on",
        },
    )
    assert active_response.status_code == 200
    assert User.objects.filter(email=active.email).count() == 1

    active.is_active = False
    active.save(update_fields=["is_active"])
    inactive_response = client.post(
        reverse("user_create"),
        {
            "first_name": "Otro",
            "last_name": "Usuario",
            "email": active.email,
            "role": User.Role.COMMERCIAL,
            "is_active": "on",
        },
    )
    assert inactive_response.status_code == 200
    assert User.objects.filter(email=active.email).count() == 1


@pytest.mark.django_db
def test_accounting_admin_can_update_allowed_user_fields(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_update", args=[commercial_user.pk]),
        {
            "first_name": "Carlos",
            "last_name": "Actualizado",
            "email": "nuevo@centenario.com",
            "role": User.Role.ACCOUNTING_ADMIN,
            "is_active": "on",
        },
    )

    commercial_user.refresh_from_db()
    assert response.status_code == 302
    assert commercial_user.email == "nuevo@centenario.com"
    assert commercial_user.role == User.Role.ACCOUNTING_ADMIN
    assert commercial_user.is_active is True


@pytest.mark.django_db
def test_accounting_admin_can_deactivate_user(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(reverse("user_status", args=[commercial_user.pk, "deactivate"]), {"reason": "Cambio de cargo"})

    commercial_user.refresh_from_db()
    assert response.status_code == 302
    assert commercial_user.is_active is False
    assert LogEntry.objects.filter(
        object_id=str(commercial_user.pk),
        change_message__icontains="INACTIVAR_USUARIO",
    ).filter(change_message__icontains="Cambio de cargo").exists()


@pytest.mark.django_db
def test_deactivate_user_without_reason_is_rejected(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(reverse("user_status", args=[commercial_user.pk, "deactivate"]), {"reason": "   "})

    commercial_user.refresh_from_db()
    assert response.status_code == 400
    assert commercial_user.is_active is True
    assert not LogEntry.objects.filter(object_id=str(commercial_user.pk), change_message__icontains="INACTIVAR_USUARIO").exists()


@pytest.mark.django_db
def test_user_delete_is_logical(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(reverse("user_delete", args=[commercial_user.pk]), {"reason": "Retiro confirmado"})

    commercial_user.refresh_from_db()
    assert response.status_code == 302
    assert get_user_model().objects.filter(pk=commercial_user.pk).exists()
    assert commercial_user.is_deleted is True
    assert commercial_user.is_active is False
    assert LogEntry.objects.filter(
        object_id=str(commercial_user.pk),
        change_message__icontains="ELIMINAR_USUARIO",
    ).filter(change_message__icontains="Retiro confirmado").exists()


@pytest.mark.django_db
def test_delete_user_without_reason_is_rejected(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(reverse("user_delete", args=[commercial_user.pk]), {"reason": ""})

    commercial_user.refresh_from_db()
    assert response.status_code == 400
    assert commercial_user.is_deleted is False
    assert commercial_user.is_active is True
    assert not LogEntry.objects.filter(object_id=str(commercial_user.pk), change_message__icontains="ELIMINAR_USUARIO").exists()


@pytest.mark.django_db
def test_user_destructive_actions_show_confirmation_before_changes(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    deactivate_response = client.get(reverse("user_status", args=[commercial_user.pk, "deactivate"]))
    delete_response = client.get(reverse("user_delete", args=[commercial_user.pk]))

    commercial_user.refresh_from_db()
    assert deactivate_response.status_code == 200
    assert "Inactivar usuario" in deactivate_response.content.decode()
    assert "Motivo" in deactivate_response.content.decode()
    assert delete_response.status_code == 200
    assert "Eliminar usuario" in delete_response.content.decode()
    assert "La cuenta sera eliminada logicamente" in delete_response.content.decode()
    assert commercial_user.is_active is True
    assert commercial_user.is_deleted is False


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
    assert timezone.localtime(accounting_admin_user.last_login).strftime("%d/%m/%Y") in content
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


@pytest.mark.django_db
def test_duplicate_email_is_rejected_in_account_management(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_create"),
        {
            "first_name": "Duplicado",
            "last_name": "Correo",
            "email": commercial_user.email.upper(),
            "role": User.Role.COMMERCIAL,
            "is_active": "on",
        },
    )

    assert response.status_code == 200
    assert "Ya existe un usuario con este correo electronico" in response.content.decode()
    assert get_user_model().objects.filter(email__iexact=commercial_user.email).count() == 1


@pytest.mark.django_db
def test_password_is_not_editable_in_account_management(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.get(reverse("user_update", args=[commercial_user.pk]))
    content = response.content.decode()

    assert response.status_code == 200
    assert "password" not in content.lower()
    assert "contrasena" not in content.lower()


@pytest.mark.django_db
def test_inactive_and_reactivated_account_authentication(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    client.post(reverse("user_status", args=[commercial_user.pk, "deactivate"]), {"reason": "Suspension temporal"})
    commercial_user.refresh_from_db()
    assert commercial_user.is_active is False
    assert authenticate(username=commercial_user.email, password="StrongPass123") is None

    client.post(reverse("user_status", args=[commercial_user.pk, "activate"]))
    commercial_user.refresh_from_db()
    assert commercial_user.is_active is True
    assert authenticate(username=commercial_user.email, password="StrongPass123") == commercial_user


@pytest.mark.django_db
def test_logically_deleted_account_cannot_authenticate_and_keeps_audit(client, accounting_admin_user, commercial_user):
    client.force_login(accounting_admin_user)

    response = client.post(reverse("user_delete", args=[commercial_user.pk]), {"reason": "Retiro definitivo"})

    commercial_user.refresh_from_db()
    assert response.status_code == 302
    assert commercial_user.is_deleted is True
    assert authenticate(username=commercial_user.email, password="StrongPass123") is None
    assert LogEntry.objects.filter(object_id=str(commercial_user.pk), change_message__icontains="eliminada").exists()


@pytest.mark.django_db
def test_account_management_actions_create_audit_entries(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    response = client.post(
        reverse("user_create"),
        {
            "first_name": "Laura",
            "last_name": "Prueba",
            "email": "laura@centenario.com",
            "role": User.Role.COMMERCIAL,
            "is_active": "on",
        },
    )

    assert response.status_code == 302
    created = get_user_model().objects.get(email="laura@centenario.com")
    assert LogEntry.objects.filter(object_id=str(created.pk), change_message__icontains="creada").exists()


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_password_reset_request_uses_generic_response(client, commercial_user):
    existing = client.post(reverse("password_reset"), {"email": commercial_user.email})
    missing = client.post(reverse("password_reset"), {"email": "nadie@centenario.com"})

    assert existing.status_code == 302
    assert missing.status_code == 302
    assert existing["Location"] == reverse("password_reset_done")
    assert missing["Location"] == reverse("password_reset_done")
    assert len(mail.outbox) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("host", ["GF-SERVER:8000", "OTRO-SERVIDOR:8000"])
@override_settings(
    ALLOWED_HOSTS=["GF-SERVER", "OTRO-SERVIDOR"],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
def test_password_reset_email_uses_request_host(client, commercial_user, host):
    mail.outbox.clear()

    client.post(reverse("password_reset"), {"email": commercial_user.email}, HTTP_HOST=host)

    body = mail.outbox[0].body
    assert f"http://{host}/accounts/reset/" in body
    assert "127.0.0.1" not in body
    assert "localhost" not in body


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_valid_account_can_reset_password_and_token_cannot_be_reused(client, commercial_user):
    client.post(reverse("password_reset"), {"email": commercial_user.email})
    body = mail.outbox[0].body
    reset_path = body.split("/accounts/reset/", 1)[1].split()[0]
    reset_url = f"/accounts/reset/{reset_path}"

    response = client.get(reset_url, follow=True)
    confirm_url = response.redirect_chain[-1][0]
    response = client.post(
        confirm_url,
        {"new_password1": "NewStrongPass123", "new_password2": "NewStrongPass123"},
    )

    assert response.status_code == 302
    assert authenticate(username=commercial_user.email, password="StrongPass123") is None
    assert authenticate(username=commercial_user.email, password="NewStrongPass123") == commercial_user
    reused = client.get(reset_url, follow=True)
    assert "Enlace no valido" in reused.content.decode()


@pytest.mark.django_db
def test_invalid_password_reset_link_is_rejected(client, commercial_user):
    from django.utils.http import urlsafe_base64_encode
    from django.utils.encoding import force_bytes

    uid = urlsafe_base64_encode(force_bytes(commercial_user.pk))
    response = client.get(reverse("password_reset_confirm", args=[uid, "token-invalido"]), follow=True)

    assert response.status_code == 200
    assert "Enlace no valido" in response.content.decode()
