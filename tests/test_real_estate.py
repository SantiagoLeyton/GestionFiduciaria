import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup
from users.models import User


@pytest.fixture
def project(db):
    return Project.objects.create(code="PRJ-001", name="Monte Cielo")


@pytest.fixture
def second_project(db):
    return Project.objects.create(code="PRJ-002", name="Altos del Rio")


@pytest.fixture
def grouping_type(db):
    return GroupingType.objects.create(code="TORRE", name="Torre")


@pytest.fixture
def block_type(db):
    return GroupingType.objects.create(code="BLOQUE", name="Bloque")


@pytest.fixture
def admin_client(accounting_admin_user):
    test_client = Client()
    test_client.force_login(accounting_admin_user)
    return test_client


@pytest.fixture
def commercial_client(commercial_user):
    test_client = Client()
    test_client.force_login(commercial_user)
    return test_client


@pytest.mark.django_db
def test_project_creation():
    project = Project.objects.create(code="P-100", name="Reserva Montecielo")

    assert project.is_active is True
    assert project.description == ""


@pytest.mark.django_db
def test_project_duplicate_code_is_rejected(project):
    duplicate = Project(code=project.code, name="Otro proyecto")

    with pytest.raises(ValidationError):
        duplicate.full_clean()


@pytest.mark.django_db
def test_project_edit_requires_reason(admin_client, project):
    response = admin_client.post(
        reverse("real_estate:project_update", args=[project.pk]),
        {"code": project.code, "name": "Monte Cielo Norte", "description": "", "is_active": "on"},
    )

    assert response.status_code == 200
    project.refresh_from_db()
    assert project.name == "Monte Cielo"


@pytest.mark.django_db
def test_project_edit_with_reason(admin_client, project):
    response = admin_client.post(
        reverse("real_estate:project_update", args=[project.pk]),
        {
            "code": project.code,
            "name": "Monte Cielo Norte",
            "description": "Actualizacion administrativa",
            "is_active": "on",
            "change_reason": "Correccion de nombre",
        },
    )

    project.refresh_from_db()
    assert response.status_code == 302
    assert project.name == "Monte Cielo Norte"
    assert project.last_change_reason == "Correccion de nombre"


@pytest.mark.django_db
def test_project_activation_and_inactivation(admin_client, project):
    response = admin_client.post(
        reverse("real_estate:project_status", args=[project.pk, "deactivate"]),
        {"change_reason": "Proyecto cerrado administrativamente"},
    )
    project.refresh_from_db()
    assert response.status_code == 302
    assert project.is_active is False

    response = admin_client.post(
        reverse("real_estate:project_status", args=[project.pk, "activate"]),
        {"change_reason": "Reactivacion autorizada"},
    )
    project.refresh_from_db()
    assert response.status_code == 302
    assert project.is_active is True


@pytest.mark.django_db
def test_project_permissions(client, commercial_client, admin_client, project):
    assert client.get(reverse("real_estate:project_list")).status_code == 302
    assert commercial_client.get(reverse("real_estate:project_list")).status_code == 200
    assert commercial_client.get(reverse("real_estate:project_create")).status_code == 403
    assert admin_client.get(reverse("real_estate:project_create")).status_code == 200


@pytest.mark.django_db
def test_grouping_type_creation_and_edit(admin_client):
    response = admin_client.post(
        reverse("real_estate:grouping_type_create"),
        {"code": "ETAPA", "name": "Etapa", "description": "", "is_active": "on"},
    )
    grouping_type = GroupingType.objects.get(code="ETAPA")
    assert response.status_code == 302

    response = admin_client.post(
        reverse("real_estate:grouping_type_update", args=[grouping_type.pk]),
        {
            "code": "ETAPA",
            "name": "Etapa constructiva",
            "description": "",
            "is_active": "on",
            "change_reason": "Ajuste de nombre",
        },
    )
    grouping_type.refresh_from_db()
    assert response.status_code == 302
    assert grouping_type.name == "Etapa constructiva"


@pytest.mark.django_db
def test_grouping_type_activation_inactivation_and_permissions(admin_client, commercial_client, grouping_type):
    assert commercial_client.get(reverse("real_estate:grouping_type_create")).status_code == 403

    admin_client.post(
        reverse("real_estate:grouping_type_status", args=[grouping_type.pk, "deactivate"]),
        {"change_reason": "No usado temporalmente"},
    )
    grouping_type.refresh_from_db()
    assert grouping_type.is_active is False

    admin_client.post(
        reverse("real_estate:grouping_type_status", args=[grouping_type.pk, "activate"]),
        {"change_reason": "Tipo habilitado"},
    )
    grouping_type.refresh_from_db()
    assert grouping_type.is_active is True


@pytest.mark.django_db
def test_structural_group_creation_and_valid_hierarchy(project, grouping_type, block_type):
    tower = StructuralGroup.objects.create(
        project=project, grouping_type=grouping_type, code="T1", name="Torre 1"
    )
    block = StructuralGroup.objects.create(
        project=project, grouping_type=block_type, parent=tower, code="B1", name="Bloque 1"
    )

    assert block.parent == tower


@pytest.mark.django_db
def test_structural_group_multiple_levels(project, grouping_type, block_type):
    sector = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="S1", name="Sector 1")
    stage = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, parent=sector, code="E1", name="Etapa 1")
    block = StructuralGroup.objects.create(project=project, grouping_type=block_type, parent=stage, code="B1", name="Bloque 1")

    assert block.parent.parent == sector


@pytest.mark.django_db
def test_structural_group_same_code_allowed_in_different_parents(project, grouping_type, block_type):
    tower_a = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    tower_b = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T2", name="Torre 2")
    group_a = StructuralGroup.objects.create(project=project, grouping_type=block_type, parent=tower_a, code="B1", name="Bloque 1")
    group_b = StructuralGroup.objects.create(project=project, grouping_type=block_type, parent=tower_b, code="B1", name="Bloque 1")

    assert group_a.code == group_b.code


@pytest.mark.django_db
def test_structural_group_duplicate_code_in_same_parent_is_rejected(project, grouping_type, block_type):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    StructuralGroup.objects.create(project=project, grouping_type=block_type, parent=tower, code="B1", name="Bloque 1")
    duplicate = StructuralGroup(project=project, grouping_type=block_type, parent=tower, code="B1", name="Bloque duplicado")

    with pytest.raises(ValidationError):
        duplicate.full_clean()


@pytest.mark.django_db
def test_structural_group_rejects_cycle(project, grouping_type, block_type):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    block = StructuralGroup.objects.create(project=project, grouping_type=block_type, parent=tower, code="B1", name="Bloque 1")
    tower.parent = block

    with pytest.raises(ValidationError):
        tower.full_clean()


@pytest.mark.django_db
def test_structural_group_rejects_parent_from_another_project(project, second_project, grouping_type, block_type):
    parent = StructuralGroup.objects.create(project=second_project, grouping_type=grouping_type, code="T1", name="Torre 1")
    group = StructuralGroup(project=project, grouping_type=block_type, parent=parent, code="B1", name="Bloque 1")

    with pytest.raises(ValidationError):
        group.full_clean()


@pytest.mark.django_db
def test_structural_group_permissions(admin_client, commercial_client):
    assert commercial_client.get(reverse("real_estate:structural_group_create")).status_code == 403
    assert admin_client.get(reverse("real_estate:structural_group_create")).status_code == 200


@pytest.mark.django_db
def test_property_unit_creation_direct_project(project):
    unit = PropertyUnit.objects.create(project=project, code="A101", name="Apartamento 101")

    assert unit.structural_group is None


@pytest.mark.django_db
def test_property_unit_creation_under_group(project, grouping_type):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    unit = PropertyUnit.objects.create(project=project, structural_group=tower, code="A101", name="Apartamento 101")

    assert unit.structural_group == tower


@pytest.mark.django_db
def test_property_unit_same_code_allowed_in_different_groups(project, grouping_type):
    tower_a = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    tower_b = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T2", name="Torre 2")
    unit_a = PropertyUnit.objects.create(project=project, structural_group=tower_a, code="A101", name="Apartamento 101")
    unit_b = PropertyUnit.objects.create(project=project, structural_group=tower_b, code="A101", name="Apartamento 101")

    assert unit_a.code == unit_b.code


@pytest.mark.django_db
def test_property_unit_duplicate_code_same_group_is_rejected(project, grouping_type):
    tower = StructuralGroup.objects.create(project=project, grouping_type=grouping_type, code="T1", name="Torre 1")
    PropertyUnit.objects.create(project=project, structural_group=tower, code="A101", name="Apartamento 101")
    duplicate = PropertyUnit(project=project, structural_group=tower, code="A101", name="Apartamento duplicado")

    with pytest.raises(ValidationError):
        duplicate.full_clean()


@pytest.mark.django_db
def test_property_unit_duplicate_code_direct_project_is_rejected(project):
    PropertyUnit.objects.create(project=project, code="A101", name="Apartamento 101")
    duplicate = PropertyUnit(project=project, code="A101", name="Apartamento duplicado")

    with pytest.raises(ValidationError):
        duplicate.full_clean()


@pytest.mark.django_db
def test_property_unit_rejects_group_from_another_project(project, second_project, grouping_type):
    group = StructuralGroup.objects.create(project=second_project, grouping_type=grouping_type, code="T1", name="Torre 1")
    unit = PropertyUnit(project=project, structural_group=group, code="A101", name="Apartamento 101")

    with pytest.raises(ValidationError):
        unit.full_clean()


@pytest.mark.django_db
def test_property_unit_activation_inactivation_and_permissions(admin_client, commercial_client, project):
    unit = PropertyUnit.objects.create(project=project, code="A101", name="Apartamento 101")
    assert commercial_client.get(reverse("real_estate:property_unit_create")).status_code == 403

    admin_client.post(
        reverse("real_estate:property_unit_status", args=[unit.pk, "deactivate"]),
        {"change_reason": "Unidad no disponible"},
    )
    unit.refresh_from_db()
    assert unit.is_active is False

    admin_client.post(
        reverse("real_estate:property_unit_status", args=[unit.pk, "activate"]),
        {"change_reason": "Unidad disponible"},
    )
    unit.refresh_from_db()
    assert unit.is_active is True


@pytest.mark.django_db
def test_status_change_requires_reason(admin_client, project):
    response = admin_client.post(reverse("real_estate:project_status", args=[project.pk, "deactivate"]))

    project.refresh_from_db()
    assert response.status_code == 302
    assert project.is_active is True


@pytest.mark.django_db
def test_no_delete_endpoint_for_real_estate(admin_client, project):
    response = admin_client.post(f"/real-estate/projects/{project.pk}/delete/")

    assert response.status_code == 404
    assert Project.objects.filter(pk=project.pk).exists()


@pytest.mark.django_db
def test_sidebar_links_for_real_estate(client, commercial_user):
    client.force_login(commercial_user)
    response = client.get(reverse("home"))
    content = response.content.decode()

    assert reverse("real_estate:project_list") in content
    assert reverse("real_estate:property_unit_list") in content
