from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied


def _has_business_role(user):
    return bool(
        user.is_authenticated
        and (
            user.is_superuser
            or user.role in {user.Role.ACCOUNTING_ADMIN, user.Role.COMMERCIAL}
        )
    )


def can_create_fiduciary(user):
    return _has_business_role(user)


def can_import_fiduciary(user):
    return can_create_fiduciary(user)


def can_resolve_imports(user):
    return can_create_fiduciary(user)


def can_update_fiduciary(user):
    return bool(
        user.is_authenticated
        and (user.is_superuser or user.role == user.Role.ACCOUNTING_ADMIN)
    )


def can_manage_fiduciary(user):
    return can_update_fiduciary(user)


class FiduciaryReadRequiredMixin(AccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        return super().dispatch(request, *args, **kwargs)


class FiduciaryCreateRequiredMixin(AccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not can_create_fiduciary(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class FiduciaryImportRequiredMixin(FiduciaryCreateRequiredMixin):
    pass


class FiduciaryUpdateRequiredMixin(AccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not can_update_fiduciary(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class FiduciaryManagementRequiredMixin(FiduciaryUpdateRequiredMixin):
    pass
