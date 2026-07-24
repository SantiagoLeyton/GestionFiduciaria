from django.urls import path

from .views import BlockedUserManagementView, LoginView, UserListView, logout_view


urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", logout_view, name="logout"),
    path("users/", UserListView.as_view(), name="user_list"),
    path("users/new/", BlockedUserManagementView.as_view(), name="user_create"),
    path("users/<int:pk>/edit/", BlockedUserManagementView.as_view(), name="user_update"),
    path("users/<int:pk>/<str:action>/", BlockedUserManagementView.as_view(), name="user_status"),
]
