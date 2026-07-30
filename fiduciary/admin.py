from django.contrib import admin

from .models import (
    Client,
    DailyReportRow,
    DetectedStructureElement,
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportedHistoricalObservation,
    ImportedHistoricalNovelty,
    ImportNovelty,
    ImportResolution,
    ImportedSheetResult,
    ImportRowIssue,
    OperationalNovelty,
    Payment,
    UnitOwnership,
)


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


@admin.register(ImportBatch)
class ImportBatchAdmin(NoDeleteAdmin):
    list_display = ("id", "import_type", "load_mode", "status", "initiated_by", "created_at", "processed_files")
    list_filter = ("import_type", "load_mode", "status", "created_at")
    search_fields = ("summary",)


@admin.register(ImportedFile)
class ImportedFileAdmin(NoDeleteAdmin):
    list_display = ("original_name", "file_type", "status", "batch", "order", "sha256")
    list_filter = ("file_type", "status")
    search_fields = ("original_name", "sha256", "result_message")


@admin.register(ImportedSheetResult)
class ImportedSheetResultAdmin(NoDeleteAdmin):
    list_display = ("sheet_name", "imported_file", "visibility", "classification", "status", "processed_rows")
    list_filter = ("visibility", "classification", "status")
    search_fields = ("sheet_name", "summary")


@admin.register(ImportRowIssue)
class ImportRowIssueAdmin(NoDeleteAdmin):
    list_display = ("code", "severity", "status", "imported_file", "sheet_result", "row_number", "column_letter")
    list_filter = ("severity", "status", "code")
    search_fields = ("code", "message")


@admin.register(DetectedStructureElement)
class DetectedStructureElementAdmin(NoDeleteAdmin):
    list_display = ("raw_value", "inferred_kind", "confidence", "status", "batch", "occurrence_count")
    list_filter = ("inferred_kind", "status")
    search_fields = ("raw_value", "normalized_value")


@admin.register(ImportResolution)
class ImportResolutionAdmin(NoDeleteAdmin):
    list_display = ("detected_element", "action", "target_kind", "status", "resolved_by", "resolved_at")
    list_filter = ("action", "target_kind", "status")
    search_fields = ("create_code", "create_name", "detected_element__raw_value")


@admin.register(Payment)
class PaymentAdmin(NoDeleteAdmin):
    list_display = ("assignment", "date_precision", "exact_date", "period_year", "period_month", "amount", "movement_type")
    list_filter = ("date_precision", "movement_type", "source_had_formula")
    search_fields = ("assignment__assignment_number", "concept", "source_sheet", "source_header")


@admin.register(ImportNovelty)
class ImportNoveltyAdmin(NoDeleteAdmin):
    list_display = ("novelty_type", "status", "batch", "imported_file", "created_at")
    list_filter = ("novelty_type", "status")
    search_fields = ("description",)


@admin.register(ImportAppliedRecord)
class ImportAppliedRecordAdmin(NoDeleteAdmin):
    list_display = ("batch", "entity_kind", "action", "entity_id", "imported_file", "source_row", "created_at")
    list_filter = ("entity_kind", "action", "created_at")
    search_fields = ("summary", "source_column")


@admin.register(ImportedHistoricalNovelty)
class ImportedHistoricalNoveltyAdmin(NoDeleteAdmin):
    list_display = ("imported_file", "sheet_result", "row_number", "unit_code", "assignment_number", "status")
    list_filter = ("status", "sheet_result")
    search_fields = ("unit_code", "assignment_number", "project_name", "grouping_name", "sanitized_summary")


@admin.register(ImportedHistoricalObservation)
class ImportedHistoricalObservationAdmin(NoDeleteAdmin):
    list_display = ("origin", "status", "property_unit", "client", "assignment", "source_sheet", "source_row", "created_at")
    list_filter = ("origin", "status", "historical_month", "historical_year")
    search_fields = (
        "summary",
        "detail",
        "historical_section",
        "property_unit__code",
        "property_unit__name",
        "client__document_number",
        "assignment__assignment_number",
    )


@admin.register(OperationalNovelty)
class OperationalNoveltyAdmin(NoDeleteAdmin):
    list_display = ("novelty_type", "origin", "status", "property_unit", "previous_client", "new_client", "created_at")
    list_filter = ("novelty_type", "origin", "status")
    search_fields = (
        "summary",
        "detail",
        "other_type",
        "property_unit__code",
        "property_unit__name",
        "previous_client__document_number",
        "new_client__document_number",
        "historical_client__document_number",
        "previous_assignment__assignment_number",
        "new_assignment__assignment_number",
        "historical_assignment__assignment_number",
    )


@admin.register(DailyReportRow)
class DailyReportRowAdmin(NoDeleteAdmin):
    list_display = ("imported_file", "sheet_name", "row_number", "normalized_assignment_number", "payment_date", "amount", "status")
    list_filter = ("status", "payment_date")
    search_fields = ("normalized_assignment_number", "payer_name", "concept", "message")
