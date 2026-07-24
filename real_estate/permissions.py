from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied


def can_manage_real_estate(user):
    return bool(
        user.is_authenticated
        and (user.is_superuser or user.role == user.Role.ACCOUNTING_ADMIN)
    )


class RealEstateReadRequiredMixin(AccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        return super().dispatch(request, *args, **kwargs)


class RealEstateManagementRequiredMixin(AccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not can_manage_real_estate(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)
