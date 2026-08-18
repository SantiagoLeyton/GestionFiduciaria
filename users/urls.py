from django.urls import path, reverse_lazy

from .views import (
    LoginView,
    ManagedUserCreateView,
    ManagedUserDeleteView,
    ManagedUserStatusView,
    ManagedUserUpdateView,
    UserListView,
    logout_view,
)
from django.contrib.auth import views as auth_views
from .forms import AccountPasswordResetForm, AccountSetPasswordForm


urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", logout_view, name="logout"),
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            form_class=AccountPasswordResetForm,
            template_name="users/password_reset_form.html",
            email_template_name="users/password_reset_email.html",
            subject_template_name="users/password_reset_subject.txt",
            success_url=reverse_lazy("password_reset_done"),
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(template_name="users/password_reset_done.html"),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            form_class=AccountSetPasswordForm,
            template_name="users/password_reset_confirm.html",
            success_url=reverse_lazy("password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(template_name="users/password_reset_complete.html"),
        name="password_reset_complete",
    ),
    path("users/", UserListView.as_view(), name="user_list"),
    path("users/new/", ManagedUserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/edit/", ManagedUserUpdateView.as_view(), name="user_update"),
    path("users/<int:pk>/delete/", ManagedUserDeleteView.as_view(), name="user_delete"),
    path("users/<int:pk>/<str:action>/", ManagedUserStatusView.as_view(), name="user_status"),
]
