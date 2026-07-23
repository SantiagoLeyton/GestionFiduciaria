import pytest

from users.models import User


@pytest.fixture
def commercial_user(db):
    return User.objects.create_user(
        username="comercial",
        email="comercial@centenario.com",
        password="StrongPass123",
        first_name="Carlos",
        last_name="Ramirez",
        role=User.Role.COMMERCIAL,
    )


@pytest.fixture
def accounting_admin_user(db):
    return User.objects.create_user(
        username="contabilidad",
        email="contabilidad@centenario.com",
        password="StrongPass123",
        first_name="Marta",
        last_name="Gomez",
        role=User.Role.ACCOUNTING_ADMIN,
    )
