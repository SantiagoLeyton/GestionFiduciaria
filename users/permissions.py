from functools import wraps

from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied


def user_has_role(user, roles):
    return bool(user.is_authenticated and user.role in roles)


def user_can_manage_users(user):
    return bool(
        user.is_authenticated
        and (user.is_superuser or user.role == user.Role.ACCOUNTING_ADMIN)
    )


def role_required(*roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not user_has_role(request.user, roles):
                raise PermissionDenied
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


class RoleRequiredMixin(AccessMixin):
    allowed_roles = ()

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if self.allowed_roles and not user_has_role(request.user, self.allowed_roles):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class UserManagementRequiredMixin(AccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not user_can_manage_users(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class UserReadRequiredMixin(AccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        return super().dispatch(request, *args, **kwargs)
