from django.urls import path

from .views import (
    GroupingTypeCreateView,
    GroupingTypeListView,
    GroupingTypeStatusView,
    GroupingTypeUpdateView,
    ProjectCreateView,
    ProjectListView,
    ProjectStatusView,
    ProjectUpdateView,
    PropertyUnitCreateView,
    PropertyUnitHistoryView,
    PropertyUnitListView,
    PropertyUnitStatusView,
    PropertyUnitUpdateView,
    StructuralGroupCreateView,
    StructuralGroupListView,
    StructuralGroupStatusView,
    StructuralGroupUpdateView,
)


app_name = "real_estate"

urlpatterns = [
    path("projects/", ProjectListView.as_view(), name="project_list"),
    path("projects/new/", ProjectCreateView.as_view(), name="project_create"),
    path("projects/<int:pk>/edit/", ProjectUpdateView.as_view(), name="project_update"),
    path("projects/<int:pk>/<str:action>/", ProjectStatusView.as_view(), name="project_status"),
    path("grouping-types/", GroupingTypeListView.as_view(), name="grouping_type_list"),
    path("grouping-types/new/", GroupingTypeCreateView.as_view(), name="grouping_type_create"),
    path("grouping-types/<int:pk>/edit/", GroupingTypeUpdateView.as_view(), name="grouping_type_update"),
    path("grouping-types/<int:pk>/<str:action>/", GroupingTypeStatusView.as_view(), name="grouping_type_status"),
    path("structural-groups/", StructuralGroupListView.as_view(), name="structural_group_list"),
    path("structural-groups/new/", StructuralGroupCreateView.as_view(), name="structural_group_create"),
    path("structural-groups/<int:pk>/edit/", StructuralGroupUpdateView.as_view(), name="structural_group_update"),
    path("structural-groups/<int:pk>/<str:action>/", StructuralGroupStatusView.as_view(), name="structural_group_status"),
    path("property-units/", PropertyUnitListView.as_view(), name="property_unit_list"),
    path("property-units/new/", PropertyUnitCreateView.as_view(), name="property_unit_create"),
    path("property-units/<int:pk>/history/", PropertyUnitHistoryView.as_view(), name="property_unit_history"),
    path("property-units/<int:pk>/edit/", PropertyUnitUpdateView.as_view(), name="property_unit_update"),
    path("property-units/<int:pk>/<str:action>/", PropertyUnitStatusView.as_view(), name="property_unit_status"),
]
