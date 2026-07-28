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


def can_create_real_estate(user):
    return _has_business_role(user)


def can_update_real_estate(user):
    return bool(
        user.is_authenticated
        and (user.is_superuser or user.role == user.Role.ACCOUNTING_ADMIN)
    )


def can_manage_real_estate(user):
    return can_update_real_estate(user)


class RealEstateReadRequiredMixin(AccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        return super().dispatch(request, *args, **kwargs)


class RealEstateCreateRequiredMixin(AccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not can_create_real_estate(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class RealEstateUpdateRequiredMixin(AccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not can_update_real_estate(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class RealEstateManagementRequiredMixin(RealEstateUpdateRequiredMixin):
    pass
