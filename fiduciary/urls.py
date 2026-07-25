from django.urls import path

from .views import (
    AssignmentCloseView,
    AssignmentContextGroupsView,
    AssignmentContextHoldersView,
    AssignmentContextTypesView,
    AssignmentContextUnitsView,
    AssignmentCreateView,
    AssignmentDetailView,
    AssignmentHolderCreateView,
    AssignmentHolderFinalizeView,
    AssignmentListView,
    AssignmentUpdateView,
    ClientCreateView,
    ClientDetailView,
    ClientListView,
    ClientStatusView,
    ClientUpdateView,
    UnitOwnershipCreateView,
    UnitOwnershipFinalizeView,
    UnitOwnershipListView,
)


app_name = "fiduciary"

urlpatterns = [
    path("clients/", ClientListView.as_view(), name="client_list"),
    path("clients/new/", ClientCreateView.as_view(), name="client_create"),
    path("clients/<int:pk>/", ClientDetailView.as_view(), name="client_detail"),
    path("clients/<int:pk>/edit/", ClientUpdateView.as_view(), name="client_update"),
    path("clients/<int:pk>/<str:action>/", ClientStatusView.as_view(), name="client_status"),
    path("ownerships/", UnitOwnershipListView.as_view(), name="ownership_list"),
    path("ownerships/new/", UnitOwnershipCreateView.as_view(), name="ownership_create"),
    path("ownerships/<int:pk>/finalize/", UnitOwnershipFinalizeView.as_view(), name="ownership_finalize"),
    path("assignments/", AssignmentListView.as_view(), name="assignment_list"),
    path("assignments/new/", AssignmentCreateView.as_view(), name="assignment_create"),
    path("assignments/context/types/", AssignmentContextTypesView.as_view(), name="assignment_context_types"),
    path("assignments/context/groups/", AssignmentContextGroupsView.as_view(), name="assignment_context_groups"),
    path("assignments/context/units/", AssignmentContextUnitsView.as_view(), name="assignment_context_units"),
    path("assignments/context/holders/", AssignmentContextHoldersView.as_view(), name="assignment_context_holders"),
    path("assignments/<int:pk>/", AssignmentDetailView.as_view(), name="assignment_detail"),
    path("assignments/<int:pk>/edit/", AssignmentUpdateView.as_view(), name="assignment_update"),
    path("assignments/<int:pk>/close/", AssignmentCloseView.as_view(), name="assignment_close"),
    path("assignments/<int:assignment_pk>/holders/new/", AssignmentHolderCreateView.as_view(), name="holder_create"),
    path("assignment-holders/<int:pk>/finalize/", AssignmentHolderFinalizeView.as_view(), name="holder_finalize"),
]
