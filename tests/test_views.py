from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse


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
    content = response.content.decode()
    assert "module-card" in content
    assert reverse("fiduciary:historical_import_list") in content
    assert reverse("fiduciary:payment_list") in content
    assert "sidebar" not in content
    assert "© 2026 Constructora Centenario. Todos los derechos reservados." in content


@pytest.mark.django_db
def test_navigation_for_commercial_does_not_show_admin_future_items(client, commercial_user):
    client.force_login(commercial_user)
    response = client.get(reverse("home"))
    content = response.content.decode()

    assert reverse("fiduciary:historical_import_list") in content
    assert "Auditoría" not in content
    assert "Usuarios" not in content


@pytest.mark.django_db
def test_navigation_for_accounting_admin_shows_admin_future_items(client, accounting_admin_user):
    client.force_login(accounting_admin_user)
    response = client.get(reverse("home"))
    content = response.content.decode()

    assert reverse("fiduciary:audit_list") in content
    assert "Usuarios" not in content
    assert accounting_admin_user.role_label in content
    assert "Administrador de Contabilidad" not in content


@pytest.mark.django_db
def test_internal_pages_keep_sidebar_and_remove_static_header_search(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    response = client.get(reverse("fiduciary:historical_import_list"))
    content = response.content.decode()

    assert response.status_code == 200
    assert 'class="sidebar"' in content
    assert "assets/logo.png" in content
    assert "data-theme-toggle" in content
    assert "Buscar proyectos" not in content
    assert "search-placeholder" not in content
    assert "Usuarios" not in content
    assert "© 2026 Constructora Centenario. Todos los derechos reservados." in content


@pytest.mark.django_db
def test_login_uses_static_visual_asset_and_footer_text(client):
    response = client.get(reverse("login"))
    content = response.content.decode()

    assert response.status_code == 200
    assert "login-visual-media" in content
    assert "assets/login.gif" in content
    assert "Contraseña" in content
    assert "Mantener sesión iniciada" in content
    assert "Iniciar sesión" in content
    assert "Todo acceso no autorizado" not in content
    assert "© 2026 Constructora Centenario. Todos los derechos reservados." in content


@pytest.mark.django_db
def test_theme_script_and_persistence_hook_are_present(client, commercial_user):
    client.force_login(commercial_user)

    response = client.get(reverse("home"))
    content = response.content.decode()

    assert "js/theme.js" in content
    assert "pagosfiducia-theme" in content
    assert "data-theme-toggle" in content
    assert "data-theme-label" in content
    assert "aria-label=\"Cambiar a modo" in content


def test_local_bootstrap_css_is_not_empty_and_contains_rules():
    path = Path("static/vendor/bootstrap/bootstrap.min.css")
    content = path.read_text(encoding="utf-8")

    assert path.exists()
    assert path.stat().st_size > 100_000
    assert ".container" in content
    assert ".btn" in content
    assert ".row" in content


def test_base_loads_bootstrap_before_app_css():
    content = Path("templates/base.html").read_text(encoding="utf-8")

    bootstrap_index = content.index("vendor/bootstrap/bootstrap.min.css")
    app_index = content.index("css/app.css")
    assert bootstrap_index < app_index


@pytest.mark.django_db
def test_home_cards_are_independent_blocks_with_logo(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    content = client.get(reverse("home")).content.decode()

    assert "home-brand" in content
    assert "assets/logo.png" in content
    assert content.count('class="module-card"') == 13
    assert content.count('class="module-icon"') == 13
    assert "module-grid" in content


@pytest.mark.django_db
def test_topbar_theme_button_is_next_to_logout(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    content = client.get(reverse("fiduciary:audit_list")).content.decode()

    theme_index = content.index("data-theme-toggle")
    divider_index = content.index("topbar-divider")
    logout_index = content.index("Cerrar sesión")
    assert theme_index < divider_index < logout_index


@pytest.mark.django_db
def test_sidebar_logo_uses_controlled_size_class(client, accounting_admin_user):
    client.force_login(accounting_admin_user)

    content = client.get(reverse("fiduciary:audit_list")).content.decode()

    assert "sidebar-logo-img" in content
    assert "assets/logo.png" in content
