from django.urls import path

from .views import LoginView, UserCreateView, UserListView, UserStatusView, UserUpdateView, logout_view


urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", logout_view, name="logout"),
    path("users/", UserListView.as_view(), name="user_list"),
    path("users/new/", UserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/edit/", UserUpdateView.as_view(), name="user_update"),
    path("users/<int:pk>/<str:action>/", UserStatusView.as_view(), name="user_status"),
]
