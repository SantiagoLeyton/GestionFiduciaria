from django.contrib import admin

from .models import Client, FiduciaryAssignment, FiduciaryAssignmentHolder, UnitOwnership


class NoDeleteAdmin(admin.ModelAdmin):
    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Client)
class ClientAdmin(NoDeleteAdmin):
    list_display = ("document_type", "document_number", "full_name", "email", "phone", "information_status", "is_active")
    list_filter = ("document_type", "information_status", "is_active")
    search_fields = ("document_number", "first_names", "last_names_or_company", "email", "phone")


@admin.register(UnitOwnership)
class UnitOwnershipAdmin(NoDeleteAdmin):
    list_display = ("client", "property_unit", "is_primary", "start_date", "end_date", "is_active")
    list_filter = ("is_primary", "is_active", "start_date")
    search_fields = ("client__document_number", "client__last_names_or_company", "property_unit__code", "property_unit__name")


class FiduciaryAssignmentHolderInline(admin.TabularInline):
    model = FiduciaryAssignmentHolder
    extra = 0
    can_delete = False


@admin.register(FiduciaryAssignment)
class FiduciaryAssignmentAdmin(NoDeleteAdmin):
    list_display = ("assignment_number", "property_unit", "start_date", "end_date", "is_active")
    list_filter = ("is_active", "start_date")
    search_fields = ("assignment_number", "property_unit__code", "property_unit__name")
    inlines = [FiduciaryAssignmentHolderInline]


@admin.register(FiduciaryAssignmentHolder)
class FiduciaryAssignmentHolderAdmin(NoDeleteAdmin):
    list_display = ("assignment", "client", "is_primary", "start_date", "end_date", "is_active")
    list_filter = ("is_primary", "is_active", "start_date")
    search_fields = ("assignment__assignment_number", "client__document_number", "client__last_names_or_company")
