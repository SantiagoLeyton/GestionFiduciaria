import pytest
from django.test import Client
from django.urls import reverse

from users.models import User


@pytest.mark.django_db
def test_home_requires_authentication(client):
    response = client.get(reverse("home"))
    assert response.status_code == 302
    assert reverse("login") in response["Location"]


@pytest.mark.django_db
def test_login_by_username(client, commercial_user):
    response = client.post(
        reverse("login"),
        {"username": "comercial", "password": "StrongPass123"},
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("home")


@pytest.mark.django_db
def test_login_by_email(client, accounting_admin_user):
    response = client.post(
        reverse("login"),
        {"username": "contabilidad@centenario.com", "password": "StrongPass123"},
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("home")


@pytest.mark.django_db
def test_authenticated_user_visiting_login_is_redirected_home(client, commercial_user):
    client.force_login(commercial_user)
    response = client.get(reverse("login"))

    assert response.status_code == 302
    assert response["Location"] == reverse("home")


@pytest.mark.django_db
def test_login_rejects_invalid_credentials(client):
    response = client.post(
        reverse("login"),
        {"username": "nadie", "password": "incorrecta"},
    )

    assert response.status_code == 400
    assert "No fue posible iniciar sesion" in response.content.decode()
    assert "nadie" not in response.content.decode()


@pytest.mark.django_db
def test_login_rejects_wrong_password_with_generic_message(client, commercial_user):
    response = client.post(
        reverse("login"),
        {"username": "comercial", "password": "WrongPass123"},
    )

    assert response.status_code == 400
    assert "No fue posible iniciar sesion" in response.content.decode()


@pytest.mark.django_db
def test_login_post_requires_csrf_when_enforced(commercial_user):
    csrf_client = Client(enforce_csrf_checks=True)
    response = csrf_client.post(
        reverse("login"),
        {"username": "comercial", "password": "StrongPass123"},
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_inactive_user_login_is_rejected(client, commercial_user):
    commercial_user.is_active = False
    commercial_user.save()

    response = client.post(
        reverse("login"),
        {"username": "comercial", "password": "StrongPass123"},
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_next_parameter_allows_local_path(client, commercial_user):
    response = client.post(
        f"{reverse('login')}?next=/",
        {"username": "comercial", "password": "StrongPass123"},
    )

    assert response.status_code == 302
    assert response["Location"] == "/"


@pytest.mark.django_db
def test_next_parameter_rejects_external_url(client, commercial_user):
    response = client.post(
        f"{reverse('login')}?next=https://example.com/phishing",
        {"username": "comercial", "password": "StrongPass123"},
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("home")


@pytest.mark.django_db
def test_logout_requires_post(client, commercial_user):
    client.force_login(commercial_user)
    response = client.get(reverse("logout"))
    assert response.status_code == 405


@pytest.mark.django_db
def test_logout_by_post(client, commercial_user):
    client.force_login(commercial_user)
    response = client.post(reverse("logout"))
    assert response.status_code == 302
    assert response["Location"] == reverse("login")


@pytest.mark.django_db
def test_private_route_is_blocked_after_logout(client, commercial_user):
    client.force_login(commercial_user)
    client.post(reverse("logout"))

    response = client.get(reverse("home"))

    assert response.status_code == 302
    assert reverse("login") in response["Location"]


@pytest.mark.django_db
def test_login_without_remember_me_expires_on_browser_close(client, commercial_user):
    response = client.post(
        reverse("login"),
        {"username": "comercial", "password": "StrongPass123"},
    )

    assert response.status_code == 302
    assert client.session.get_expire_at_browser_close() is True


@pytest.mark.django_db
def test_login_with_remember_me_uses_normal_session_duration(client, commercial_user):
    response = client.post(
        reverse("login"),
        {"username": "comercial", "password": "StrongPass123", "remember_me": "on"},
    )

    assert response.status_code == 302
    assert client.session.get_expire_at_browser_close() is False


@pytest.mark.django_db
def test_home_renders_private_page(client, commercial_user):
    client.force_login(commercial_user)
    response = client.get(reverse("home"))

    assert response.status_code == 200
    assert "Fase 1 activa" in response.content.decode()


@pytest.mark.django_db
def test_navigation_for_commercial_does_not_show_admin_future_items(client, commercial_user):
    client.force_login(commercial_user)
    response = client.get(reverse("home"))
    content = response.content.decode()

    assert "Importar libro historico" in content
    assert "Auditoria" not in content
    assert "Usuarios" not in content


@pytest.mark.django_db
def test_navigation_for_accounting_admin_shows_admin_future_items(client, accounting_admin_user):
    client.force_login(accounting_admin_user)
    response = client.get(reverse("home"))
    content = response.content.decode()

    assert "Auditoria" in content
    assert "Usuarios" in content
    assert accounting_admin_user.get_role_display() in content
