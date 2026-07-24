from django.contrib import admin

from .models import GroupingType, Project, PropertyUnit, StructuralGroup


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    search_fields = ("code", "name")
    list_filter = ("is_active",)


@admin.register(GroupingType)
class GroupingTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    search_fields = ("code", "name")
    list_filter = ("is_active",)


@admin.register(StructuralGroup)
class StructuralGroupAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "project", "grouping_type", "parent", "is_active")
    search_fields = ("code", "name", "project__name", "grouping_type__name")
    list_filter = ("is_active", "project", "grouping_type")


@admin.register(PropertyUnit)
class PropertyUnitAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "project", "structural_group", "is_active")
    search_fields = ("code", "name", "project__name", "structural_group__name")
    list_filter = ("is_active", "project")
