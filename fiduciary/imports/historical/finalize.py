import hashlib
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from fiduciary.models import (
    Client,
    DetectedStructureElement,
    AssignmentInterest,
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportedHistoricalObservation,
    ImportedHistoricalNovelty,
    ImportResolution,
    OperationalNovelty,
    Payment,
    UnitOwnership,
)
from fiduciary.imports.audit import create_import_audit_event
from fiduciary.permissions import can_import_fiduciary
from fiduciary.services import create_imported_client, normalize_valid_imported_email
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup

from .normalize import MONTHS, clean_text, normalize_text
from .parser import (
    HistoricalWorkbookParser,
    RECEIPT_CATEGORY_CREDIT,
    RECEIPT_CATEGORY_ORDINARY,
    RECEIPT_CATEGORY_SUBSIDY,
    RECEIPT_CATEGORY_TRANSFER,
    RECEIPT_CATEGORY_CESSION,
    _split_client_name_values,
    _split_contact_values,
    _split_document_values,
    _split_phone_values,
)
from .readiness import (
    can_continue_historical_finalization,
    can_start_historical_finalization,
    has_blocked_dependencies,
    has_failed_historical_files,
    has_open_blocking_issues,
    has_unresolved_required_pendings,
)

logger = logging.getLogger(__name__)
PAYMENT_DATE_RELATION_AMBIGUOUS = "ambiguous"


class HistoricalImportFinalizationError(Exception):
    pass


@dataclass(frozen=True)
class HistoricalImportFinalizationResult:
    batch_id: int
    created_projects: int = 0
    created_grouping_types: int = 0
    created_structural_groups: int = 0
    created_property_units: int = 0
    created_clients: int = 0
    created_ownerships: int = 0
    created_assignments: int = 0
    created_assignment_holders: int = 0
    created_payments: int = 0
    duplicate_payments: int = 0
    created_interests: int = 0
    duplicate_interests: int = 0
    preserved_novelties: int = 0
    imported_observations: int = 0
    imported_novelties: int = 0


@dataclass(frozen=True)
class _PaymentResult:
    status: str
    payment: Payment | None = None
    errors: list[str] | None = None


@dataclass
class _HistoricalNoveltyState:
    novelty: ImportedHistoricalNovelty | None
    sheet_result: object | None
    row_number: int | None
    original_cells: list[dict]
    unit: PropertyUnit
    assignment: FiduciaryAssignment | None
    client: Client | None
    summary: str
    detail: str
    historical_section: str
    historical_month: int | None
    historical_year: int | None


@dataclass
class _HistoricalNoveltyEvent:
    source_state: _HistoricalNoveltyState
    detail: str
    summary: str
    effective_date: object
    novelty_type: str
    other_type: str
    previous_mention: str
    new_mention: str
    source_order: int
    event_key: str
    previous_state: _HistoricalNoveltyState | None = None
    new_state: _HistoricalNoveltyState | None = None


def store_historical_import_file(*, imported_file: ImportedFile, source_path) -> None:
    source = Path(source_path)
    target_dir = settings.MEDIA_ROOT / "imports" / "historical"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{imported_file.sha256}{source.suffix.lower()}"
    if not target.exists():
        shutil.copyfile(source, target)
    imported_file.stored_path = str(target.relative_to(settings.MEDIA_ROOT))
    imported_file.save(update_fields=["stored_path"])


def finalize_historical_import(*, batch_id: int, user, progress_callback=None) -> HistoricalImportFinalizationResult:
    if not can_import_fiduciary(user):
        raise PermissionDenied

    try:
        started = time.perf_counter()
        batch = ImportBatch.objects.get(pk=batch_id)
        if batch.status == ImportBatch.Status.PROCESSING:
            _validate_batch_continuing(batch)
        else:
            _validate_batch_ready(batch)
        files = list(
            ImportedFile.objects.filter(batch=batch, file_type=ImportedFile.FileType.HISTORICAL).order_by("order", "pk")
        )
        if len(files) != 1:
            raise HistoricalImportFinalizationError("La importacion historica definitiva requiere un unico archivo.")
        imported_file = files[0]
        path = _stored_file_path(imported_file)

        if batch.status != ImportBatch.Status.PROCESSING:
            batch.status = ImportBatch.Status.PROCESSING
            batch.processing_started_at = timezone.now()
            batch.summary = json.dumps(
                {"progress": {"phase": "preparing_final_import", "percent": 1, "processed_rows": 0, "total_rows": batch.total_rows}},
                ensure_ascii=True,
            )
            batch.save(update_fields=["status", "processing_started_at", "summary"])
            imported_file.status = ImportedFile.Status.PROCESSING
            imported_file.save(update_fields=["status"])

        parse_started = time.perf_counter()
        workbook = HistoricalWorkbookParser(path, progress_callback=progress_callback).parse()
        parse_seconds = time.perf_counter() - parse_started
        parse_blocking_issues = [issue for issue in workbook.issues if issue.severity == "blocking"]
        if parse_blocking_issues:
            first_issue = parse_blocking_issues[0]
            raise HistoricalImportFinalizationError(
                "El libro contiene incidencias bloqueantes detectadas durante la importacion definitiva: "
                f"{first_issue.code} en {first_issue.sheet_name or '-'}"
                f"{f' fila {first_issue.row_number}' if first_issue.row_number else ''}. "
                f"{first_issue.message}"
            )
        if has_open_blocking_issues(batch):
            raise HistoricalImportFinalizationError("El lote contiene incidencias bloqueantes abiertas.")

        with transaction.atomic():
            batch = ImportBatch.objects.select_for_update().get(pk=batch_id)
            if batch.status != ImportBatch.Status.PROCESSING:
                raise HistoricalImportFinalizationError("El lote no se encuentra en procesamiento.")
            files = list(
                ImportedFile.objects.select_for_update()
                .filter(batch=batch, file_type=ImportedFile.FileType.HISTORICAL)
                .order_by("order", "pk")
            )
            if len(files) != 1:
                raise HistoricalImportFinalizationError("La importacion historica definitiva requiere un unico archivo.")
            imported_file = files[0]

            if progress_callback:
                progress_callback(
                    {
                        "phase": "materializing",
                        "percent": 95,
                        "processed_rows": workbook.statistics.valid_rows,
                        "total_rows": workbook.statistics.valid_rows,
                    }
                )

            context = _FinalizationContext(batch=batch, imported_file=imported_file, user=user)
            materialize_started = time.perf_counter()
            context.materialize_structure()
            context.import_rows(workbook)
            context.flush_payments()
            context._time("novelties", context.preserve_historical_novelties)
            context._time("traces", context.flush_traces)
            materialize_seconds = time.perf_counter() - materialize_started

            now = timezone.now()
            batch.status = ImportBatch.Status.COMPLETED
            batch.processing_finished_at = now
            batch.imported_at = now
            batch.imported_by = user
            batch.summary = json.dumps(
                {
                    "message": context.summary(),
                    "progress": {
                        "phase": "completed",
                        "percent": 100,
                        "processed_rows": workbook.statistics.valid_rows,
                        "total_rows": workbook.statistics.valid_rows,
                    },
                },
                ensure_ascii=True,
            )
            batch.save(
                update_fields=[
                    "status",
                    "processing_finished_at",
                    "imported_at",
                    "imported_by",
                    "processed_rows",
                    "summary",
                ]
            )
            imported_file.status = ImportedFile.Status.COMPLETED
            imported_file.processing_finished_at = now
            imported_file.processed_rows = workbook.statistics.valid_rows
            imported_file.save(update_fields=["status", "processing_finished_at", "processed_rows"])
            result = context.result()
            created_projects = _created_record_count(batch, ImportAppliedRecord.EntityKind.PROJECT)
            created_grouping_types = _created_record_count(batch, ImportAppliedRecord.EntityKind.GROUPING_TYPE)
            created_structural_groups = _created_record_count(batch, ImportAppliedRecord.EntityKind.STRUCTURAL_GROUP)
            created_property_units = _created_record_count(batch, ImportAppliedRecord.EntityKind.PROPERTY_UNIT)
            create_import_audit_event(
                batch=batch,
                imported_file=imported_file,
                entity_kind=ImportAppliedRecord.EntityKind.PROJECT,
                action="Importado",
                entity="Libro historico",
                lines=[
                    "Descripcion: Importacion historica definitiva completada.",
                    f"Archivo: {imported_file.original_name}",
                    f"Resultado: {batch.get_status_display()}",
                    f"Proyectos creados: {created_projects}",
                    f"Tipos de agrupacion creados: {created_grouping_types}",
                    f"Agrupaciones creadas: {created_structural_groups}",
                    f"Unidades creadas: {created_property_units}",
                    f"Clientes creados: {result.created_clients}",
                    f"Titularidades creadas: {result.created_ownerships}",
                    f"Encargos fiduciarios creados: {result.created_assignments}",
                    f"Titulares de encargo creados: {result.created_assignment_holders}",
                    f"Pagos creados: {result.created_payments}",
                    f"Pagos existentes/omitidos: {result.duplicate_payments}",
                    f"Intereses creados: {result.created_interests}",
                    f"Intereses existentes/omitidos: {result.duplicate_interests}",
                    f"Novedades importadas: {result.imported_novelties}",
                    f"Observaciones importadas: {result.imported_observations}",
                ],
            )
        logger.info(
            "Historical import finalization completed for batch %s in %.2fs (parse %.2fs, materialize %.2fs, rows %s, timings %s).",
            batch_id,
            time.perf_counter() - started,
            parse_seconds,
            materialize_seconds,
            workbook.statistics.valid_rows,
            {key: round(value, 3) for key, value in sorted(context.timings.items())},
        )
        return result
    except Exception as exc:
        logger.exception("Historical import finalization failed for batch %s.", batch_id)
        _mark_batch_failed(batch_id, exc)
        raise


class _FinalizationContext:
    def __init__(self, *, batch: ImportBatch, imported_file: ImportedFile, user):
        self.batch = batch
        self.imported_file = imported_file
        self.user = user
        self.today = timezone.localdate()
        self.projects: dict[int, Project] = {}
        self.grouping_types: dict[int, GroupingType] = {}
        self.groups: dict[int, StructuralGroup] = {}
        self.units: dict[int, PropertyUnit] = {}
        self.groups_by_name: dict[str, StructuralGroup] = {}
        self.units_by_context: dict[tuple[str, str], PropertyUnit] = {}
        self.created_projects = 0
        self.created_grouping_types = 0
        self.created_structural_groups = 0
        self.created_property_units = 0
        self.created_clients = 0
        self.created_ownerships = 0
        self.created_assignments = 0
        self.created_assignment_holders = 0
        self.created_payments = 0
        self.duplicate_payments = 0
        self.created_interests = 0
        self.duplicate_interests = 0
        self.preserved_novelties = 0
        self.imported_observations = 0
        self.imported_novelties = 0
        self._trace_buffer: list[ImportAppliedRecord] = []
        self._clients_by_document: dict[tuple[str, str], Client] = {}
        self._assignments_by_number: dict[str, FiduciaryAssignment] = {}
        self._ownerships_by_key: dict[tuple[int, int, bool], UnitOwnership] = {}
        self._holders_by_key: dict[tuple[int, int, bool], FiduciaryAssignmentHolder] = {}
        self._payment_keys_by_assignment: dict[int, set[tuple]] = {}
        self._active_primary_ownership_by_unit: dict[int, UnitOwnership] = {}
        self._active_primary_holder_by_assignment: dict[int, FiduciaryAssignmentHolder] = {}
        self._holder_preloaded_assignments: set[int] = set()
        self._payment_buffer: list[Payment] = []
        self._payment_trace_buffer: list[tuple[Payment, object, int, str | None, str]] = []
        self._historical_row_states_by_unit: dict[int, list[_HistoricalNoveltyState]] = {}
        self._historical_novelty_texts_by_sheet_unit: dict[tuple[str, str], set[str]] | None = None
        self.timings: dict[str, float] = {}

    def materialize_structure(self) -> None:
        self._time("structure.projects", lambda: self._materialize_kind(DetectedStructureElement.InferredKind.PROJECT))
        self._time("structure.grouping_types", lambda: self._materialize_kind(DetectedStructureElement.InferredKind.GROUPING_TYPE))
        self._time("structure.groups", lambda: self._materialize_kind(DetectedStructureElement.InferredKind.STRUCTURAL_GROUP))
        self._time("structure.units", lambda: self._materialize_kind(DetectedStructureElement.InferredKind.PROPERTY_UNIT))
        self._preload_active_ownerships()

    def import_rows(self, workbook) -> None:
        sheet_results = {sheet.sheet_name: sheet for sheet in self.imported_file.sheet_results.all()}
        for sheet in workbook.sheets:
            sheet_result = sheet_results.get(sheet.name)
            for row in sheet.rows:
                unit = self._time("rows.units", lambda: self._unit_for_row(row))
                historical_context = _is_historical_context_row(row)
                if not row.assignment or not row.assignment.assignment_number:
                    if historical_context:
                        continue
                    if not any([row.clients, row.payments, row.reconstructed_payments]):
                        continue
                    raise HistoricalImportFinalizationError(
                        f"La fila {row.row_number} de la hoja {row.sheet_name} no tiene numero de encargo."
                    )
                clients = self._time("rows.clients", lambda: [self._client_for_historical_client(client) for client in row.clients])
                assignment = self._time("rows.assignments", lambda: self._assignment_for_row(row, unit, historical=historical_context))
                if not historical_context:
                    for client, historical_client in zip(clients, row.clients, strict=False):
                        ownership = self._time(
                            "rows.ownerships",
                            lambda client=client, historical_client=historical_client: self._ownership_for_client(
                                unit, client, historical_client.is_primary
                            ),
                        )
                        self._time(
                            "rows.holders",
                            lambda client=client, historical_client=historical_client, ownership=ownership: self._assignment_holder_for_client(
                                assignment, client, historical_client.is_primary, ownership
                            ),
                        )
                self._remember_historical_row_state(row, unit, assignment, clients, sheet_result)
                if row.reconstructed_payments:
                    for payment in row.reconstructed_payments:
                        self._time("rows.payments", lambda payment=payment: self._reconstructed_payment_for_row(assignment, payment, sheet_result))
                else:
                    for payment in row.payments:
                        self._time("rows.payments", lambda payment=payment: self._payment_for_row(assignment, payment, sheet_result))
                for interest in row.interests:
                    self._time("rows.interests", lambda interest=interest: self._interest_for_row(assignment, interest, sheet_result))
                self._time("rows.observations", lambda: self._main_row_observation(row, unit, assignment, sheet_result))
        self.batch.processed_rows = workbook.statistics.valid_rows

    def preserve_historical_novelties(self) -> None:
        novelties_by_unit: dict[tuple[str, str, str, str], list[ImportedHistoricalNovelty]] = {}
        novelties = ImportedHistoricalNovelty.objects.filter(batch=self.batch).select_related("sheet_result").order_by(
            "sheet_result__sheet_name",
            "row_number",
            "pk",
        )
        for novelty in novelties:
            if novelty.status != ImportedHistoricalNovelty.Status.READY:
                novelty.status = ImportedHistoricalNovelty.Status.READY
                novelty.save(update_fields=["status", "updated_at"])
            self.preserved_novelties += 1
            self._trace(
                ImportAppliedRecord.EntityKind.HISTORICAL_NOVELTY,
                ImportAppliedRecord.Action.PRESERVED,
                entity_id=novelty.pk,
                sheet_result=novelty.sheet_result,
                source_row=novelty.row_number,
                summary="Novedad historica preservada para fases posteriores.",
            )
            key = (
                normalize_text(novelty.project_name),
                normalize_text(novelty.grouping_name or novelty.grouping_code),
                normalize_text(novelty.unit_code or novelty.unit_name),
                normalize_text(novelty.grouping_type_name),
            )
            novelties_by_unit.setdefault(key, []).append(novelty)
        for unit_novelties in novelties_by_unit.values():
            self._historical_novelty_observations_for_unit(unit_novelties)

    def result(self) -> HistoricalImportFinalizationResult:
        return HistoricalImportFinalizationResult(
            batch_id=self.batch.pk,
            created_projects=self.created_projects,
            created_grouping_types=self.created_grouping_types,
            created_structural_groups=self.created_structural_groups,
            created_property_units=self.created_property_units,
            created_clients=self.created_clients,
            created_ownerships=self.created_ownerships,
            created_assignments=self.created_assignments,
            created_assignment_holders=self.created_assignment_holders,
            created_payments=self.created_payments,
            duplicate_payments=self.duplicate_payments,
            created_interests=self.created_interests,
            duplicate_interests=self.duplicate_interests,
            preserved_novelties=self.preserved_novelties,
            imported_observations=self.imported_observations,
            imported_novelties=self.imported_novelties,
        )

    def flush_traces(self) -> None:
        if not self._trace_buffer:
            return
        ImportAppliedRecord.objects.bulk_create(self._trace_buffer, batch_size=1000)
        self._trace_buffer = []

    def flush_payments(self) -> None:
        if not self._payment_buffer:
            return
        Payment.objects.bulk_create(self._payment_buffer, batch_size=1000)
        for payment, sheet_result, source_row, source_column, summary in self._payment_trace_buffer:
            self._trace(
                ImportAppliedRecord.EntityKind.PAYMENT,
                ImportAppliedRecord.Action.CREATED,
                payment.pk,
                sheet_result=sheet_result,
                source_row=source_row,
                source_column=source_column,
                summary=summary,
            )
        self._payment_buffer = []
        self._payment_trace_buffer = []

    def _time(self, key: str, callback):
        started = time.perf_counter()
        try:
            return callback()
        finally:
            self.timings[key] = self.timings.get(key, 0.0) + (time.perf_counter() - started)

    def _preload_active_ownerships(self) -> None:
        unit_ids = [unit.pk for unit in self.units.values() if unit.pk]
        if not unit_ids:
            return
        for ownership in UnitOwnership.objects.filter(property_unit_id__in=unit_ids, is_active=True):
            key = (ownership.property_unit_id, ownership.client_id, ownership.is_primary)
            self._ownerships_by_key[key] = ownership
            if ownership.is_primary:
                self._active_primary_ownership_by_unit[ownership.property_unit_id] = ownership

    def _preload_active_assignment_holders(self, assignment: FiduciaryAssignment) -> None:
        if not assignment.pk or assignment.pk in self._holder_preloaded_assignments:
            return
        for holder in assignment.holders.filter(is_active=True):
            key = (holder.assignment_id, holder.client_id, holder.is_primary)
            self._holders_by_key[key] = holder
            if holder.is_primary:
                self._active_primary_holder_by_assignment[holder.assignment_id] = holder
        self._holder_preloaded_assignments.add(assignment.pk)

    def summary(self) -> str:
        return (
            "Importacion historica definitiva completada. "
            f"Proyectos creados: {self.created_projects}; tipos creados: {self.created_grouping_types}; "
            f"agrupaciones creadas: {self.created_structural_groups}; unidades creadas: {self.created_property_units}; "
            f"clientes creados: {self.created_clients}; encargos creados: {self.created_assignments}; "
            f"pagos creados: {self.created_payments}; pagos duplicados omitidos: {self.duplicate_payments}; "
            f"intereses creados: {self.created_interests}; intereses duplicados omitidos: {self.duplicate_interests}; "
            f"novedades preservadas: {self.preserved_novelties}; "
            f"observaciones historicas importadas: {self.imported_observations}."
        )

    def _materialize_kind(self, kind: str) -> None:
        elements = self.batch.detected_elements.filter(inferred_kind=kind).select_related(
            "resolution",
            "resolution__target_project",
            "resolution__target_grouping_type",
            "resolution__target_structural_group",
            "resolution__target_property_unit",
            "resolution__parent_project",
            "resolution__parent_grouping_type",
            "resolution__parent_structural_group",
        )
        for element in elements:
            if element.status == DetectedStructureElement.Status.IGNORED:
                continue
            resolution = element.resolution
            if resolution.action == ImportResolution.Action.UNRESOLVED:
                raise HistoricalImportFinalizationError(f"El elemento {element.raw_value} no esta resuelto.")
            if kind == DetectedStructureElement.InferredKind.PROJECT:
                self.projects[resolution.pk] = self._project_from_resolution(resolution)
            elif kind == DetectedStructureElement.InferredKind.GROUPING_TYPE:
                self.grouping_types[resolution.pk] = self._grouping_type_from_resolution(resolution)
            elif kind == DetectedStructureElement.InferredKind.STRUCTURAL_GROUP:
                group = self._group_from_resolution(resolution)
                self.groups[resolution.pk] = group
                self.groups_by_name[normalize_text(element.raw_value)] = group
            elif kind == DetectedStructureElement.InferredKind.PROPERTY_UNIT:
                unit = self._unit_from_resolution(resolution)
                self.units[resolution.pk] = unit
                grouping_name = element.structural_context.get("grouping_name", "")
                self._remember_unit_context(unit, element.raw_value, grouping_name)

    def _project_from_resolution(self, resolution: ImportResolution) -> Project:
        if resolution.action == ImportResolution.Action.ASSOCIATE_EXISTING and resolution.target_project:
            self._trace(ImportAppliedRecord.EntityKind.PROJECT, ImportAppliedRecord.Action.REUSED, resolution.target_project.pk)
            return resolution.target_project
        code, name = _code_and_name(resolution)
        project, created = Project.objects.get_or_create(
            code=code,
            defaults={"name": name, "last_change_reason": _reason(self.batch)},
        )
        if created:
            self.created_projects += 1
        self._trace(ImportAppliedRecord.EntityKind.PROJECT, _created_action(created), project.pk)
        return project

    def _grouping_type_from_resolution(self, resolution: ImportResolution) -> GroupingType:
        if resolution.action == ImportResolution.Action.ASSOCIATE_EXISTING and resolution.target_grouping_type:
            self._trace(
                ImportAppliedRecord.EntityKind.GROUPING_TYPE,
                ImportAppliedRecord.Action.REUSED,
                resolution.target_grouping_type.pk,
            )
            return resolution.target_grouping_type
        code, name = _code_and_name(resolution)
        grouping_type, created = GroupingType.objects.get_or_create(
            code=code,
            defaults={"name": name, "last_change_reason": _reason(self.batch)},
        )
        if created:
            self.created_grouping_types += 1
        self._trace(ImportAppliedRecord.EntityKind.GROUPING_TYPE, _created_action(created), grouping_type.pk)
        return grouping_type

    def _group_from_resolution(self, resolution: ImportResolution) -> StructuralGroup:
        if resolution.action == ImportResolution.Action.ASSOCIATE_EXISTING and resolution.target_structural_group:
            group = resolution.target_structural_group
            self._trace(ImportAppliedRecord.EntityKind.STRUCTURAL_GROUP, ImportAppliedRecord.Action.REUSED, group.pk)
            return group
        project = resolution.parent_project or self._single_project()
        grouping_type = resolution.parent_grouping_type or self._single_grouping_type()
        parent = resolution.parent_structural_group
        code = _non_placeholder(resolution.create_code)
        name = _non_placeholder(resolution.create_name) or _non_placeholder(resolution.detected_element.raw_value)
        group = _find_group(project, grouping_type, parent, code, name)
        created = False
        if not group:
            group = StructuralGroup.objects.create(
                project=project,
                grouping_type=grouping_type,
                parent=parent,
                code=code,
                name=name,
                last_change_reason=_reason(self.batch),
            )
            created = True
            self.created_structural_groups += 1
        self._trace(ImportAppliedRecord.EntityKind.STRUCTURAL_GROUP, _created_action(created), group.pk)
        return group

    def _unit_from_resolution(self, resolution: ImportResolution) -> PropertyUnit:
        if resolution.action == ImportResolution.Action.ASSOCIATE_EXISTING and resolution.target_property_unit:
            unit = resolution.target_property_unit
            self._trace(ImportAppliedRecord.EntityKind.PROPERTY_UNIT, ImportAppliedRecord.Action.REUSED, unit.pk)
            return unit
        project = resolution.parent_project or self._single_project()
        parent = resolution.parent_structural_group or self._parent_group_from_context(resolution)
        code = _non_placeholder(resolution.create_code)
        name = _non_placeholder(resolution.create_name) or _non_placeholder(resolution.detected_element.raw_value)
        if code and name and normalize_text(code) == normalize_text(name):
            code = ""
        unit = _find_unit(project, parent, code, name)
        created = False
        if not unit:
            unit = PropertyUnit.objects.create(
                project=project,
                structural_group=parent,
                code=code,
                name=name,
                last_change_reason=_reason(self.batch),
            )
            created = True
            self.created_property_units += 1
        self._trace(ImportAppliedRecord.EntityKind.PROPERTY_UNIT, _created_action(created), unit.pk)
        return unit

    def _unit_for_row(self, row) -> PropertyUnit:
        unit_value = row.unit_code or row.unit_name or ""
        unit = self._unit_from_context(row.grouping_name, unit_value) or self._unit_from_context(row.grouping_code, unit_value)
        if not unit:
            raise HistoricalImportFinalizationError(
                f"No se encontro unidad resuelta para la fila {row.row_number} de la hoja {row.sheet_name}."
            )
        self._update_unit_historical_data(unit, row.area, row.property_value, row.financial_entity)
        return unit

    def _update_unit_historical_data(self, unit: PropertyUnit, area, property_value, financial_entity) -> None:
        update_fields = []
        if area is not None and unit.area is None:
            unit.area = area
            update_fields.append("area")
        if property_value is not None and unit.property_value is None:
            unit.property_value = property_value
            update_fields.append("property_value")
        if financial_entity and not unit.financial_entity:
            unit.financial_entity = financial_entity
            update_fields.append("financial_entity")
        if update_fields:
            unit.save(update_fields=[*update_fields, "updated_at"])

    def _remember_unit_context(self, unit: PropertyUnit, unit_value: str, grouping_value: str = "") -> None:
        unit_keys = {normalize_text(unit_value), normalize_text(unit.code), normalize_text(unit.name)}
        grouping_keys = {normalize_text(grouping_value)}
        if unit.structural_group_id:
            grouping_keys.update(
                {
                    normalize_text(unit.structural_group.code),
                    normalize_text(unit.structural_group.name),
                    normalize_text(str(unit.structural_group)),
                }
            )
        for grouping_key in {key for key in grouping_keys if key}:
            for unit_key in {key for key in unit_keys if key}:
                self.units_by_context[(grouping_key, unit_key)] = unit

    def _unit_from_context(self, grouping_value: str, unit_value: str) -> PropertyUnit | None:
        return self.units_by_context.get((normalize_text(grouping_value), normalize_text(unit_value)))

    def _client_for_historical_client(self, historical_client) -> Client:
        document_type = _document_type(historical_client.document_type)
        document_number = (historical_client.document_number or "").strip()
        cache_key = (document_type, document_number) if document_number else None
        if cache_key and cache_key in self._clients_by_document:
            client = self._clients_by_document[cache_key]
            if not historical_client.name or _canonical_client_name_matches(client, historical_client.name):
                self._update_cached_client_contact(client, historical_client)
                self._trace(ImportAppliedRecord.EntityKind.CLIENT, ImportAppliedRecord.Action.REUSED, client.pk)
                return client
            document_number = ""
            cache_key = None
        if document_number:
            existing_with_document = Client.objects.filter(
                document_type=document_type,
                document_number=document_number,
            ).first()
            if existing_with_document and not _canonical_client_name_matches(existing_with_document, historical_client.name):
                document_number = ""
                cache_key = None
        existing_by_name = _client_by_canonical_name(historical_client.name)
        if existing_by_name:
            self._update_cached_client_contact(existing_by_name, historical_client)
            self._trace(ImportAppliedRecord.EntityKind.CLIENT, ImportAppliedRecord.Action.REUSED, existing_by_name.pk)
            if cache_key:
                self._clients_by_document[cache_key] = existing_by_name
            return existing_by_name
        result = create_imported_client(
            full_name=historical_client.name,
            document_type=document_type,
            document_number=document_number or None,
            source_origin=Client.SourceOrigin.HISTORICAL_IMPORT,
            phone=historical_client.phone or "",
            email=historical_client.email or "",
            contact_name=historical_client.contact_name or "",
        )
        if result.status == "invalid" or not result.client:
            raise HistoricalImportFinalizationError("; ".join(result.errors) or "No fue posible crear el cliente importado.")
        if result.status == "created":
            self.created_clients += 1
            action = ImportAppliedRecord.Action.CREATED
        else:
            action = ImportAppliedRecord.Action.REUSED
        if cache_key:
            self._clients_by_document[cache_key] = result.client
        self._trace(ImportAppliedRecord.EntityKind.CLIENT, action, result.client.pk)
        return result.client

    def _update_cached_client_contact(self, client: Client, historical_client) -> None:
        update_fields = []
        if historical_client.phone and not client.phone:
            client.phone = historical_client.phone
            update_fields.append("phone")
        email = normalize_valid_imported_email(historical_client.email)
        if email and not client.email:
            client.email = email
            update_fields.append("email")
        if historical_client.contact_name and not client.address:
            client.address = historical_client.contact_name
            update_fields.append("address")
        if (
            update_fields
            and client.information_status == Client.InformationStatus.INCOMPLETE
            and client.document_number
            and (client.phone or client.email)
        ):
            client.information_status = Client.InformationStatus.COMPLETE
            client.incomplete_reason = ""
            update_fields.extend(["information_status", "incomplete_reason"])
        if update_fields:
            client.full_clean()
            client.save(update_fields=update_fields + ["updated_at"])

    def _ownership_for_client(self, unit: PropertyUnit, client: Client, is_primary: bool) -> UnitOwnership:
        for existing_key in ((unit.pk, client.pk, True), (unit.pk, client.pk, False)):
            if existing_key in self._ownerships_by_key:
                ownership = self._ownerships_by_key[existing_key]
                self._trace(ImportAppliedRecord.EntityKind.UNIT_OWNERSHIP, ImportAppliedRecord.Action.REUSED, ownership.pk)
                return ownership
        if is_primary:
            primary = self._active_primary_ownership_by_unit.get(unit.pk)
            if primary and primary.client_id != client.pk:
                raise HistoricalImportFinalizationError("La unidad ya tiene un titular principal vigente diferente.")

        ownership = UnitOwnership(
            client=client,
            property_unit=unit,
            is_active=True,
            is_primary=is_primary,
            start_date=self.today,
            last_change_reason=_reason(self.batch),
        )
        try:
            ownership.save()
        except IntegrityError:
            ownership = UnitOwnership.objects.filter(client=client, property_unit=unit, is_active=True).first()
            if not ownership:
                raise
            self._ownerships_by_key[(unit.pk, client.pk, ownership.is_primary)] = ownership
            self._trace(ImportAppliedRecord.EntityKind.UNIT_OWNERSHIP, ImportAppliedRecord.Action.REUSED, ownership.pk)
            return ownership

        self.created_ownerships += 1
        self._ownerships_by_key[(unit.pk, client.pk, is_primary)] = ownership
        if is_primary:
            self._active_primary_ownership_by_unit[unit.pk] = ownership
        self._trace(ImportAppliedRecord.EntityKind.UNIT_OWNERSHIP, ImportAppliedRecord.Action.CREATED, ownership.pk)
        return ownership

    def _assignment_for_row(self, row, unit: PropertyUnit, *, historical: bool = False) -> FiduciaryAssignment:
        assignment_number = row.assignment.assignment_number.strip()
        imported_dates = _assignment_operational_dates(row.assignment)
        cache_key = normalize_text(assignment_number)
        if cache_key in self._assignments_by_number:
            assignment = self._assignments_by_number[cache_key]
            if assignment.property_unit_id != unit.pk:
                raise HistoricalImportFinalizationError(
                    f"El encargo {assignment_number} ya existe asociado a una unidad diferente."
                )
            self._fill_missing_assignment_operational_dates(assignment, imported_dates)
            self._trace(ImportAppliedRecord.EntityKind.FIDUCIARY_ASSIGNMENT, ImportAppliedRecord.Action.REUSED, assignment.pk)
            return assignment
        assignment = FiduciaryAssignment.objects.filter(assignment_number=assignment_number).first()
        if assignment:
            if assignment.property_unit_id != unit.pk:
                raise HistoricalImportFinalizationError(
                    f"El encargo {assignment_number} ya existe asociado a una unidad diferente."
                )
            self._assignments_by_number[cache_key] = assignment
            self._fill_missing_assignment_operational_dates(assignment, imported_dates)
            self._trace(ImportAppliedRecord.EntityKind.FIDUCIARY_ASSIGNMENT, ImportAppliedRecord.Action.REUSED, assignment.pk)
            return assignment
        assignment = FiduciaryAssignment.objects.create(
            assignment_number=assignment_number,
            property_unit=unit,
            start_date=self.today,
            end_date=self.today if historical else None,
            is_active=not historical,
            **imported_dates,
            observations="Encargo historico reconstruido desde contexto historico." if historical else "Creado desde importacion historica.",
            last_change_reason=_reason(self.batch),
        )
        self.created_assignments += 1
        self._assignments_by_number[cache_key] = assignment
        self._holder_preloaded_assignments.add(assignment.pk)
        self._payment_keys_by_assignment[assignment.pk] = set()
        self._trace(ImportAppliedRecord.EntityKind.FIDUCIARY_ASSIGNMENT, ImportAppliedRecord.Action.CREATED, assignment.pk)
        return assignment

    def _fill_missing_assignment_operational_dates(self, assignment: FiduciaryAssignment, imported_dates: dict[str, object]) -> None:
        changed_fields = []
        for field_name, imported_date in imported_dates.items():
            if imported_date and not getattr(assignment, field_name):
                setattr(assignment, field_name, imported_date)
                changed_fields.append(field_name)
        if changed_fields:
            assignment.last_change_reason = _reason(self.batch)
            assignment.save(update_fields=[*changed_fields, "last_change_reason", "updated_at"])

    def _assignment_holder_for_client(
        self,
        assignment: FiduciaryAssignment,
        client: Client,
        is_primary: bool,
        ownership: UnitOwnership,
    ) -> FiduciaryAssignmentHolder:
        self._preload_active_assignment_holders(assignment)
        for existing_key in ((assignment.pk, client.pk, True), (assignment.pk, client.pk, False)):
            if existing_key in self._holders_by_key:
                holder = self._holders_by_key[existing_key]
                self._trace(ImportAppliedRecord.EntityKind.ASSIGNMENT_HOLDER, ImportAppliedRecord.Action.REUSED, holder.pk)
                return holder
        if is_primary:
            primary = self._active_primary_holder_by_assignment.get(assignment.pk)
            if primary and primary.client_id != client.pk:
                raise HistoricalImportFinalizationError("El encargo ya tiene un titular principal vigente diferente.")

        holder = FiduciaryAssignmentHolder(
            assignment=assignment,
            client=client,
            is_active=True,
            is_primary=is_primary,
            start_date=ownership.start_date,
            last_change_reason=_reason(self.batch),
        )
        try:
            holder.save()
        except IntegrityError:
            holder = FiduciaryAssignmentHolder.objects.filter(assignment=assignment, client=client, is_active=True).first()
            if not holder:
                raise
            self._holders_by_key[(assignment.pk, client.pk, holder.is_primary)] = holder
            self._trace(ImportAppliedRecord.EntityKind.ASSIGNMENT_HOLDER, ImportAppliedRecord.Action.REUSED, holder.pk)
            return holder

        self.created_assignment_holders += 1
        self._holders_by_key[(assignment.pk, client.pk, is_primary)] = holder
        if is_primary:
            self._active_primary_holder_by_assignment[assignment.pk] = holder
        self._trace(ImportAppliedRecord.EntityKind.ASSIGNMENT_HOLDER, ImportAppliedRecord.Action.CREATED, holder.pk)
        return holder

    def _payment_for_row(self, assignment: FiduciaryAssignment, payment, sheet_result) -> None:
        result = self._create_historical_payment(
            assignment=assignment,
            amount=payment.amount,
            movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
            source_file=self.imported_file,
            source_sheet=sheet_result.sheet_name if sheet_result else "",
            source_row=payment.source_row,
            source_column=payment.source_column,
            source_header=payment.source_header,
            source_had_formula=payment.has_formula,
            destination=payment.destination,
            date_precision=Payment.DatePrecision.MONTH,
            period_year=payment.year,
            period_month=payment.month,
        )
        if result.status == "created" and result.payment:
            self.created_payments += 1
            self._payment_trace_buffer.append(
                (
                    result.payment,
                    sheet_result,
                    payment.source_row,
                    payment.source_column,
                    "",
                )
            )
        elif result.status == "duplicate":
            self.duplicate_payments += 1
            self._trace(
                ImportAppliedRecord.EntityKind.PAYMENT,
                ImportAppliedRecord.Action.SKIPPED,
                sheet_result=sheet_result,
                source_row=payment.source_row,
                source_column=payment.source_column,
                summary="Pago historico duplicado omitido.",
            )
        else:
            raise HistoricalImportFinalizationError("; ".join(result.errors) or "No fue posible crear el pago.")

    def _reconstructed_payment_for_row(self, assignment: FiduciaryAssignment, payment, sheet_result) -> None:
        exact_date = None
        date_precision = Payment.DatePrecision.EXACT
        historical_date_values = []
        historical_receipt_values = []
        if payment.date_relation_status == PAYMENT_DATE_RELATION_AMBIGUOUS:
            date_precision = Payment.DatePrecision.AMBIGUOUS
            historical_date_values = list(payment.ambiguous_date_values)
            historical_receipt_values = list(payment.ambiguous_receipt_values)
        else:
            exact_date = _parse_historical_payment_date(payment.date_value)
            if not exact_date:
                raise HistoricalImportFinalizationError(
                    f"No fue posible interpretar la fecha historica {payment.date_value!r} en la fila {payment.source_row}."
                )
        result = self._create_historical_payment(
            assignment=assignment,
            amount=payment.amount,
            movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
            source_file=self.imported_file,
            source_sheet=sheet_result.sheet_name if sheet_result else payment.sheet_name,
            source_row=payment.source_row,
            source_column=payment.value_source_column,
            source_header=payment.value_source_header,
            source_had_formula=payment.value_had_formula,
            destination=payment.destination,
            date_precision=date_precision,
            exact_date=exact_date,
            concept=_historical_payment_concept(payment.category, payment.receipt, payment.value_source_header),
            historical_date_values=historical_date_values,
            historical_receipt_values=historical_receipt_values,
        )
        if result.status == "created" and result.payment:
            self.created_payments += 1
            self._payment_trace_buffer.append(
                (
                    result.payment,
                    sheet_result,
                    payment.source_row,
                    payment.value_source_column,
                    f"Pago historico reconstruido: {payment.receipt or payment.value_source_header}.",
                )
            )
        elif result.status == "duplicate":
            self.duplicate_payments += 1
            self._trace(
                ImportAppliedRecord.EntityKind.PAYMENT,
                ImportAppliedRecord.Action.SKIPPED,
                sheet_result=sheet_result,
                source_row=payment.source_row,
                source_column=payment.value_source_column,
                summary=f"Pago historico reconstruido duplicado omitido: {payment.receipt or payment.value_source_header}.",
            )
        else:
            raise HistoricalImportFinalizationError("; ".join(result.errors) or "No fue posible crear el pago reconstruido.")

    def _interest_for_row(self, assignment: FiduciaryAssignment, interest, sheet_result) -> None:
        _, created = AssignmentInterest.objects.get_or_create(
            assignment=assignment,
            receipt=interest.receipt,
            interest=interest.date_iso,
            amount=interest.amount,
        )
        if created:
            self.created_interests += 1
            self._trace(
                ImportAppliedRecord.EntityKind.FIDUCIARY_ASSIGNMENT,
                ImportAppliedRecord.Action.CREATED,
                assignment.pk,
                sheet_result=sheet_result,
                source_row=interest.source_row,
                source_column=interest.source_column,
                summary=f"Interes historico importado: {interest.receipt}.",
            )
        else:
            self.duplicate_interests += 1

    def _create_historical_payment(
        self,
        *,
        assignment,
        amount,
        movement_type,
        source_file,
        source_sheet,
        source_row,
        date_precision,
        exact_date=None,
        period_year=None,
        period_month=None,
        concept=None,
        source_column=None,
        source_header=None,
        source_had_formula=False,
        destination=None,
        historical_date_values=None,
        historical_receipt_values=None,
    ):
        try:
            normalized_amount = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError):
            return _PaymentResult("invalid", errors=["El valor del pago no es valido."])
        if normalized_amount < 0:
            return _PaymentResult("invalid", errors=["El valor del pago no puede ser negativo."])

        key = self._payment_key(
            assignment=assignment,
            amount=normalized_amount,
            date_precision=date_precision,
            exact_date=exact_date,
            period_year=period_year,
            period_month=period_month,
            concept=concept,
            destination=destination,
            source_file=source_file,
            source_sheet=source_sheet,
            source_row=source_row,
            source_column=source_column,
            source_header=source_header,
        )
        keys = self._payment_keys_for_assignment(assignment.pk)
        if key in keys:
            return _PaymentResult("duplicate")

        payment = Payment(
            assignment=assignment,
            exact_date=exact_date,
            period_year=period_year,
            period_month=period_month,
            date_precision=date_precision,
            amount=normalized_amount,
            concept=(concept or "").strip() or None,
            destination=destination,
            movement_type=movement_type,
            source_file=source_file,
            source_sheet=source_sheet,
            source_row=source_row,
            source_column=source_column,
            source_header=source_header,
            source_had_formula=source_had_formula,
            historical_date_values=list(historical_date_values or []),
            historical_receipt_values=list(historical_receipt_values or []),
        )
        try:
            payment.clean()
        except ValidationError as exc:
            return _PaymentResult("invalid", errors=[str(exc)])
        keys.add(key)
        self._payment_buffer.append(payment)
        return _PaymentResult("created", payment=payment)

    def _payment_keys_for_assignment(self, assignment_id: int) -> set[tuple]:
        if assignment_id not in self._payment_keys_by_assignment:
            keys = set()
            for (
                exact_date,
                period_year,
                period_month,
                date_precision,
                amount,
                concept,
                destination,
                source_file_id,
                source_sheet,
                source_row,
                source_column,
                source_header,
            ) in Payment.objects.filter(
                assignment_id=assignment_id
            ).values_list(
                "exact_date",
                "period_year",
                "period_month",
                "date_precision",
                "amount",
                "concept",
                "destination",
                "source_file_id",
                "source_sheet",
                "source_row",
                "source_column",
                "source_header",
            ):
                if date_precision == Payment.DatePrecision.EXACT:
                    keys.add((Payment.DatePrecision.EXACT, exact_date, amount, concept or "", destination or ""))
                elif date_precision == Payment.DatePrecision.MONTH:
                    keys.add((Payment.DatePrecision.MONTH, period_year, period_month, amount))
                else:
                    keys.add(
                        (
                            Payment.DatePrecision.AMBIGUOUS,
                            source_file_id,
                            source_sheet,
                            source_row,
                            source_column,
                            source_header,
                            amount,
                        )
                    )
            self._payment_keys_by_assignment[assignment_id] = keys
        return self._payment_keys_by_assignment[assignment_id]

    def _payment_key(
        self,
        *,
        assignment,
        amount,
        date_precision,
        exact_date=None,
        period_year=None,
        period_month=None,
        concept=None,
        destination=None,
        source_file=None,
        source_sheet=None,
        source_row=None,
        source_column=None,
        source_header=None,
    ) -> tuple:
        if date_precision == Payment.DatePrecision.EXACT:
            return (Payment.DatePrecision.EXACT, exact_date, amount, (concept or "").strip(), destination or "")
        if date_precision == Payment.DatePrecision.MONTH:
            return (Payment.DatePrecision.MONTH, period_year, period_month, amount)
        if date_precision == Payment.DatePrecision.AMBIGUOUS:
            return (
                Payment.DatePrecision.AMBIGUOUS,
                source_file.pk if source_file else None,
                source_sheet,
                source_row,
                source_column,
                source_header,
                amount,
            )
        raise HistoricalImportFinalizationError("La precision de fecha no es valida.")

    def _main_row_observation(self, row, unit: PropertyUnit, assignment: FiduciaryAssignment, sheet_result) -> None:
        detail = (row.observation or "").strip()
        if not detail:
            return
        fragments = _historical_event_fragments(detail) or [detail]
        for index, fragment in enumerate(fragments, start=1):
            if self._main_observation_fragment_matches_historical_novelty(row, fragment):
                continue
            self._save_historical_observation(
                origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
                sheet_result=sheet_result,
                source_novelty=None,
                source_sheet=row.sheet_name,
                source_row=row.row_number,
                source_order=(row.row_number * 1000) + index,
                project=unit.project,
                unit=unit,
                client=None,
                assignment=assignment,
                summary="",
                detail=fragment,
                historical_section="",
                historical_month=None,
                historical_year=None,
                payload={"row_type": "main_table", "fragment_index": index},
            )

    def _main_observation_fragment_matches_historical_novelty(self, row, fragment: str) -> bool:
        text_key = _normalized_historical_text_key(fragment)
        if not text_key:
            return False
        index = self._historical_novelty_text_index()
        unit_value = getattr(row, "unit_code", "") or getattr(row, "unit_name", "")
        return text_key in index.get((normalize_text(row.sheet_name), normalize_text(unit_value)), set())

    def _historical_novelty_text_index(self) -> dict[tuple[str, str], set[str]]:
        if self._historical_novelty_texts_by_sheet_unit is not None:
            return self._historical_novelty_texts_by_sheet_unit
        index: dict[tuple[str, str], set[str]] = {}
        novelties = ImportedHistoricalNovelty.objects.filter(batch=self.batch).select_related("sheet_result")
        for novelty in novelties:
            unit_value = novelty.unit_code or novelty.unit_name
            if not unit_value:
                continue
            key = (normalize_text(novelty.sheet_result.sheet_name if novelty.sheet_result else ""), normalize_text(unit_value))
            summary, detail = _summary_detail_from_cells(novelty.original_cells or [])
            values = []
            if detail:
                values.extend(_historical_event_fragments(detail) or [detail])
            for value in values:
                text_key = _normalized_historical_text_key(value)
                if text_key:
                    index.setdefault(key, set()).add(text_key)
        self._historical_novelty_texts_by_sheet_unit = index
        return index

    def _historical_novelty_observation(self, novelty: ImportedHistoricalNovelty) -> None:
        self._historical_novelty_observations_for_unit([novelty])

    def _historical_novelty_observations_for_unit(self, novelties: list[ImportedHistoricalNovelty]) -> None:
        states = [
            state
            for novelty in novelties
            for state in self._historical_novelty_states(novelty)
        ]
        if not states:
            return
        source_states = list(states)
        unit_ids = {state.unit.pk for state in states}
        for unit_id in unit_ids:
            states.extend(self._historical_row_states_by_unit.get(unit_id, []))
        states = _dedupe_historical_states(states)
        state_candidates = self._historical_state_candidates(states)
        events = []
        for state in source_states:
            if not state.detail and not state.summary:
                continue
            fragments = _historical_event_fragments(state.detail) if state.detail else []
            if not fragments and state.detail:
                fragments = [state.detail]
            if not fragments and state.summary:
                fragments = [""]
            for index, fragment in enumerate(fragments, start=1):
                summary = _best_summary_from_row_cells(state.original_cells, fragment) or state.summary
                novelty_type, other_type = _historical_novelty_type(summary, fragment, state.historical_section)
                effective_date = _historical_event_effective_date(summary, fragment, state.original_cells)
                previous_mention, new_mention = _assignment_change_mentions(summary, fragment)
                source_order = (state.row_number or 0) * 100 + index
                event = _HistoricalNoveltyEvent(
                    source_state=state,
                    detail=fragment,
                    summary=summary,
                    effective_date=effective_date,
                    novelty_type=novelty_type,
                    other_type=other_type,
                    previous_mention=previous_mention,
                    new_mention=new_mention,
                    source_order=source_order,
                    event_key=_historical_event_key(
                        unit=state.unit,
                        effective_date=effective_date,
                        novelty_type=novelty_type,
                        other_type=other_type,
                        previous_mention=previous_mention,
                        new_mention=new_mention,
                        summary=summary,
                        detail=fragment,
                    ),
                )
                events.append(event)
        events.sort(key=lambda event: (event.effective_date or self.today, event.source_order, event.event_key))
        unique_events: dict[str, _HistoricalNoveltyEvent] = {}
        for event in events:
            existing = unique_events.get(event.event_key)
            if not existing or _historical_event_completeness_score(event) > _historical_event_completeness_score(existing):
                unique_events[event.event_key] = event
        for event in sorted(unique_events.values(), key=lambda item: (item.effective_date or self.today, item.source_order, item.event_key)):
            if _looks_like_assignment_change_text(event.summary, event.detail, event.source_state.historical_section):
                event.previous_state, event.new_state = _state_pair_for_historical_event(event, state_candidates, unit=event.source_state.unit)
            self._save_historical_novelty_event(event, state_candidates)

    def _historical_novelty_states(self, novelty: ImportedHistoricalNovelty) -> list[_HistoricalNoveltyState]:
        unit = self._unit_from_imported_novelty(novelty)
        if not unit:
            return []
        assignment = self._assignment_from_imported_novelty(novelty, unit)
        clients = self._clients_from_imported_novelty(novelty)
        summary, detail = _summary_detail_from_cells(novelty.original_cells)
        historical_section = _cell_payload_value(novelty.original_cells, "__historical_section__") or ""
        if not any([summary, detail, assignment]):
            return []
        if not clients:
            return [
                _HistoricalNoveltyState(
                    novelty=novelty,
                    sheet_result=novelty.sheet_result,
                    row_number=novelty.row_number,
                    original_cells=novelty.original_cells or [],
                    unit=unit,
                    assignment=assignment,
                    client=None,
                    summary=summary,
                    detail=detail,
                    historical_section=historical_section,
                    historical_month=_int_or_none(_cell_payload_value(novelty.original_cells, "__section_month__")),
                    historical_year=_int_or_none(_cell_payload_value(novelty.original_cells, "__section_year__")),
                )
            ]
        states = []
        for index, client in enumerate(clients):
            if assignment:
                self._ensure_inactive_historical_relations(
                    unit=unit,
                    assignment=assignment,
                    client=client,
                    effective_date=None,
                    is_primary=index == 0,
                )
            states.append(
                _HistoricalNoveltyState(
                    novelty=novelty,
                    sheet_result=novelty.sheet_result,
                    row_number=novelty.row_number,
                    original_cells=novelty.original_cells or [],
                    unit=unit,
                    assignment=assignment,
                    client=client,
                    summary=summary,
                    detail=detail,
                    historical_section=historical_section,
                    historical_month=_int_or_none(_cell_payload_value(novelty.original_cells, "__section_month__")),
                    historical_year=_int_or_none(_cell_payload_value(novelty.original_cells, "__section_year__")),
                )
            )
        return states

    def _remember_historical_row_state(self, row, unit: PropertyUnit, assignment: FiduciaryAssignment, clients: list[Client], sheet_result) -> None:
        if not row.observation:
            return
        primary_client = None
        for client, historical_client in zip(clients, row.clients, strict=False):
            if historical_client.is_primary:
                primary_client = client
                break
        if primary_client is None and clients:
            primary_client = clients[0]
        if not primary_client:
            return
        state = _HistoricalNoveltyState(
            novelty=None,
            sheet_result=sheet_result,
            row_number=row.row_number,
            original_cells=[],
            unit=unit,
            assignment=assignment,
            client=primary_client,
            summary="",
            detail=row.observation,
            historical_section="",
            historical_month=None,
            historical_year=None,
        )
        self._historical_row_states_by_unit.setdefault(unit.pk, []).append(state)

    def _historical_state_candidates(self, states: list[_HistoricalNoveltyState]) -> list[_HistoricalNoveltyState]:
        candidates = []
        seen = set()
        for state in states:
            key = (state.client.pk if state.client else None, state.assignment.pk if state.assignment else None)
            if key not in seen:
                candidates.append(state)
                seen.add(key)
        unit_ids = {state.unit.pk for state in states}
        holders = FiduciaryAssignmentHolder.objects.filter(
            assignment__property_unit_id__in=unit_ids,
        ).select_related("client", "assignment", "assignment__property_unit")
        for holder in holders:
            unit = holder.assignment.property_unit
            key = (holder.client_id, holder.assignment_id)
            if key in seen:
                continue
            candidates.append(
                _HistoricalNoveltyState(
                    novelty=None,
                    sheet_result=None,
                    row_number=None,
                    original_cells=[],
                    unit=unit,
                    assignment=holder.assignment,
                    client=holder.client,
                    summary="",
                    detail="",
                    historical_section="",
                    historical_month=None,
                    historical_year=None,
                )
            )
            seen.add(key)
        return candidates

    def _save_historical_novelty_event(
        self,
        event: _HistoricalNoveltyEvent,
        state_candidates: list[_HistoricalNoveltyState],
    ) -> None:
        unit = event.source_state.unit
        is_assignment_change = _looks_like_assignment_change_text(
            event.summary,
            event.detail,
            event.source_state.historical_section,
        )
        previous_state = None
        if is_assignment_change and event.previous_mention:
            previous_state = event.previous_state or _unique_state_matching_mention(event.previous_mention, state_candidates, unit=unit)
        if is_assignment_change and not previous_state and event.source_state.client and event.source_state.assignment:
            previous_state = event.source_state
        previous_client = previous_state.client if previous_state else (event.source_state.client if is_assignment_change else None)
        previous_assignment = previous_state.assignment if previous_state else (event.source_state.assignment if is_assignment_change else None)
        has_structured_chain = (
            len(
                {
                    (state.client.pk, state.assignment.pk)
                    for state in state_candidates
                    if state.unit.pk == unit.pk and state.client and state.assignment
                }
            )
            > 1
        )
        new_state = None
        if is_assignment_change and event.new_mention:
            new_state = event.new_state or _unique_state_matching_mention(event.new_mention, state_candidates, unit=unit)
        new_assignment = None
        new_client = None
        if is_assignment_change:
            if new_state:
                new_assignment = new_state.assignment
                new_client = new_state.client
            elif not has_structured_chain or not event.new_mention:
                new_assignment = self._new_assignment_for_historical_change(unit=unit, previous_assignment=previous_assignment)
                new_client = self._new_client_for_historical_change(
                    new_assignment=new_assignment,
                    previous_assignment=previous_assignment,
                    previous_client=previous_client,
                    new_mention=event.new_mention,
                    summary=event.summary,
                    detail=event.detail,
                )
        _validate_historical_event_state_pair(
            previous_state=previous_state,
            previous_client=previous_client,
            previous_assignment=previous_assignment,
            new_state=new_state,
            new_client=new_client,
            new_assignment=new_assignment,
        )
        if is_assignment_change:
            self._ensure_inactive_historical_relations(
                unit=unit,
                assignment=previous_assignment,
                client=previous_client,
                effective_date=event.effective_date,
            )
            self._ensure_inactive_historical_relations(
                unit=unit,
                assignment=new_assignment,
                client=new_client,
                effective_date=event.effective_date,
            )
        elif event.source_state.assignment and event.source_state.client:
            self._ensure_inactive_historical_relations(
                unit=unit,
                assignment=event.source_state.assignment,
                client=event.source_state.client,
                effective_date=event.effective_date,
            )
        existing = self._existing_historical_operational_novelty(event.event_key, unit)
        defaults = {
            "batch": self.batch,
            "imported_file": self.imported_file,
            "project": unit.project,
            "property_unit": unit,
            "novelty_type": event.novelty_type,
            "other_type": event.other_type,
            "origin": OperationalNovelty.Origin.HISTORICAL_IMPORT,
            "status": OperationalNovelty.Status.IMPORTED,
            "effective_date": event.effective_date,
            "previous_client": previous_client,
            "historical_client": previous_client or event.source_state.client,
            "new_client": new_client,
            "previous_assignment": previous_assignment,
            "new_assignment": new_assignment,
            "historical_assignment": previous_assignment or event.source_state.assignment,
            "summary": event.summary,
            "detail": event.detail,
            "source_sheet": event.source_state.sheet_result.sheet_name if event.source_state.sheet_result else "",
            "source_row": event.source_state.row_number,
            "historical_section": event.source_state.historical_section,
            "historical_month": event.source_state.historical_month,
            "historical_year": event.source_state.historical_year,
            "source_payload": {
                "cells": event.source_state.original_cells,
                "event_key": event.event_key,
                "matched_previous_state": _historical_state_payload(previous_state),
                "matched_new_state": _historical_state_payload(new_state),
            },
            "source_observation": None,
            "source_novelty": event.source_state.novelty,
            "created_by": self.user,
        }
        if existing:
            for field, value in defaults.items():
                setattr(existing, field, value)
            operational_novelty, created = existing, False
        else:
            operational_novelty = OperationalNovelty(**defaults)
            created = True
        operational_novelty.full_clean()
        operational_novelty.save()
        if created:
            self.imported_novelties += 1

    def _save_historical_event_observation(self, event: _HistoricalNoveltyEvent) -> ImportedHistoricalObservation:
        unit = event.source_state.unit
        return self._save_historical_observation(
            origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
            sheet_result=event.source_state.sheet_result,
            source_novelty=event.source_state.novelty,
            source_sheet=event.source_state.sheet_result.sheet_name if event.source_state.sheet_result else "",
            source_row=event.source_state.row_number,
            source_order=event.source_order,
            project=unit.project,
            unit=unit,
            client=event.source_state.client,
            assignment=event.source_state.assignment,
            summary=event.summary,
            detail=event.detail,
            historical_section=event.source_state.historical_section,
            historical_month=event.source_state.historical_month,
            historical_year=event.source_state.historical_year,
            payload={
                "cells": event.source_state.original_cells,
                "event_key": event.event_key,
                "observation_only": True,
            },
        )

    def _existing_historical_operational_novelty(self, event_key: str, unit: PropertyUnit) -> OperationalNovelty | None:
        for novelty in OperationalNovelty.objects.filter(
            batch=self.batch,
            property_unit=unit,
            origin=OperationalNovelty.Origin.HISTORICAL_IMPORT,
        ):
            if (novelty.source_payload or {}).get("event_key") == event_key:
                return novelty
        return None

    def _new_assignment_for_historical_change(
        self,
        *,
        unit: PropertyUnit | None,
        previous_assignment: FiduciaryAssignment | None,
    ) -> FiduciaryAssignment | None:
        if not unit:
            return None
        assignments = list(
            FiduciaryAssignment.objects.filter(property_unit=unit, is_active=True)
            .exclude(pk=previous_assignment.pk if previous_assignment else None)
            .order_by("pk")[:2]
        )
        return assignments[0] if len(assignments) == 1 else None

    def _new_client_for_historical_change(
        self,
        *,
        new_assignment: FiduciaryAssignment | None,
        previous_assignment: FiduciaryAssignment | None,
        previous_client: Client | None,
        new_mention: str,
        summary: str,
        detail: str,
    ) -> Client | None:
        if new_assignment:
            holders = list(
                new_assignment.holders.filter(is_active=True, is_primary=True)
                .select_related("client")
                .order_by("pk")[:2]
            )
            candidates = [holder.client for holder in holders if not previous_client or holder.client_id != previous_client.pk]
            if len(candidates) == 1:
                return candidates[0]
            if new_mention:
                return _unique_client_matching_mention(new_mention, candidates)
            return None
        if previous_assignment:
            holders = list(
                previous_assignment.holders.filter(is_active=True)
                .exclude(client=previous_client)
                .select_related("client")
                .order_by("-is_primary", "pk")
            )
            candidates = [holder.client for holder in holders]
            if new_mention:
                return _unique_client_matching_mention(new_mention, candidates)
            return candidates[0] if len(candidates) == 1 else None
        return None

    def _ensure_inactive_historical_relations(
        self,
        *,
        unit: PropertyUnit | None,
        assignment: FiduciaryAssignment | None,
        client: Client | None,
        effective_date,
        is_primary: bool = True,
    ) -> None:
        if not unit or not assignment or not client:
            return
        relation_date = effective_date or self.today
        ownership_key = (unit.pk, client.pk, is_primary)
        if ownership_key not in self._ownerships_by_key:
            ownership, created = UnitOwnership.objects.get_or_create(
                client=client,
                property_unit=unit,
                is_active=False,
                is_primary=is_primary,
                defaults={
                    "start_date": relation_date,
                    "end_date": relation_date,
                    "last_change_reason": _reason(self.batch),
                },
            )
            self._ownerships_by_key[ownership_key] = ownership
            if created:
                self.created_ownerships += 1
                self._trace(
                    ImportAppliedRecord.EntityKind.UNIT_OWNERSHIP,
                    ImportAppliedRecord.Action.CREATED,
                    ownership.pk,
                )
        holder_key = (assignment.pk, client.pk, is_primary)
        if holder_key not in self._holders_by_key:
            holder, created = FiduciaryAssignmentHolder.objects.get_or_create(
                assignment=assignment,
                client=client,
                is_active=False,
                is_primary=is_primary,
                defaults={
                    "start_date": relation_date,
                    "end_date": relation_date,
                    "last_change_reason": _reason(self.batch),
                },
            )
            self._holders_by_key[holder_key] = holder
            if created:
                self.created_assignment_holders += 1
                self._trace(
                    ImportAppliedRecord.EntityKind.ASSIGNMENT_HOLDER,
                    ImportAppliedRecord.Action.CREATED,
                    holder.pk,
                )

    def _project_from_imported_novelty(self, novelty: ImportedHistoricalNovelty) -> Project | None:
        project_name = (novelty.project_name or "").strip()
        if not project_name:
            return None
        query = Project.objects.filter(Q(code__iexact=project_name) | Q(name__iexact=project_name))
        matches = list(query[:2])
        return matches[0] if len(matches) == 1 else None

    def _unit_from_imported_novelty(self, novelty: ImportedHistoricalNovelty) -> PropertyUnit | None:
        unit_value = novelty.unit_code or novelty.unit_name
        if not unit_value:
            return None
        unit = (
            self._unit_from_context(novelty.grouping_name, unit_value)
            or self._unit_from_context(novelty.grouping_code, unit_value)
            or self._unit_from_context(novelty.grouping_name or novelty.grouping_code, unit_value)
        )
        if unit:
            return unit
        project = self._project_from_imported_novelty(novelty)
        if not project:
            return None
        query = PropertyUnit.objects.filter(project=project).filter(Q(code__iexact=unit_value) | Q(name__iexact=unit_value))
        matches = list(query[:2])
        return matches[0] if len(matches) == 1 else None

    def _assignment_from_imported_novelty(self, novelty: ImportedHistoricalNovelty, unit: PropertyUnit | None) -> FiduciaryAssignment | None:
        number = novelty.assignment_number.strip()
        if not number:
            return None
        cache_key = normalize_text(number)
        if cache_key in self._assignments_by_number:
            return self._assignments_by_number[cache_key]
        assignment = FiduciaryAssignment.objects.filter(assignment_number=number).first()
        if assignment:
            self._assignments_by_number[cache_key] = assignment
            return assignment
        if not unit:
            return None
        assignment = FiduciaryAssignment(
            assignment_number=number,
            property_unit=unit,
            start_date=self.today,
            end_date=self.today,
            is_active=False,
            observations="Encargo historico reconstruido desde seccion NOVEDADES.",
            last_change_reason=_reason(self.batch),
        )
        assignment.full_clean()
        assignment.save()
        self.created_assignments += 1
        self._assignments_by_number[cache_key] = assignment
        self._holder_preloaded_assignments.add(assignment.pk)
        self._payment_keys_by_assignment[assignment.pk] = set()
        self._trace(
            ImportAppliedRecord.EntityKind.FIDUCIARY_ASSIGNMENT,
            ImportAppliedRecord.Action.CREATED,
            assignment.pk,
            sheet_result=novelty.sheet_result,
            source_row=novelty.row_number,
        )
        return assignment

    def _client_from_imported_novelty(self, novelty: ImportedHistoricalNovelty) -> Client | None:
        clients = self._clients_from_imported_novelty(novelty)
        return clients[0] if clients else None

    def _clients_from_imported_novelty(self, novelty: ImportedHistoricalNovelty) -> list[Client]:
        document_cell = _first_cell_payload_by_header(
            novelty.original_cells,
            {"cedula cliente", "documento cliente", "identificacion", "identificacion cliente"},
        )
        document = document_cell.get("value") if document_cell else None
        document_type = _document_type(document_cell.get("header") if document_cell else "")
        raw_names = []
        for value in _cell_values_by_header(novelty.original_cells, {"nombre cliente"}):
            if _looks_descriptive_novelty_text(value):
                continue
            raw_names.extend(_split_client_name_values(value))
        names = [name for name in raw_names if name and not _looks_descriptive_novelty_text(name)]
        if not names:
            return []
        email = _first_cell_by_header(novelty.original_cells, {"e mail", "email", "correo", "correo electronico"})
        phone = _first_cell_by_header(novelty.original_cells, {"telefono", "tel", "celular"})
        contact_name = _first_cell_by_header(novelty.original_cells, {"contacto"})
        documents = _split_document_values(str(document).strip() if document else "")
        phones = _split_phone_values(str(phone).strip() if phone else "")
        emails = _split_contact_values(str(email).strip() if email else "", separators=("/", ";", ","))
        email = normalize_valid_imported_email(email)
        phone = str(phone).strip() if phone else ""
        contact_name = str(contact_name).strip() if contact_name else ""
        clients = []
        for index, name in enumerate(names, start=1):
            document = documents[index - 1] if index <= len(documents) else ""
            indexed_phone = phones[index - 1] if index <= len(phones) else phone
            indexed_email = emails[index - 1] if index <= len(emails) else email
            client = self._client_from_historical_novelty_values(
                name=name,
                document=document,
                document_type=document_type,
                phone=indexed_phone,
                email=indexed_email,
                contact_name=contact_name,
            )
            if client:
                clients.append(client)
        return clients

    def _client_from_historical_novelty_values(
        self,
        *,
        name: str,
        document: str,
        document_type: str,
        phone: str,
        email: str,
        contact_name: str,
    ) -> Client | None:
        document = str(document).strip() if document else ""
        name = str(name).strip() if name else ""
        phone = str(phone).strip() if phone else ""
        email = str(email).strip() if email else ""
        contact_name = str(contact_name).strip() if contact_name else ""
        if document:
            existing = Client.objects.filter(document_number=document).first()
            if existing:
                if not name or _canonical_client_name_matches(existing, name):
                    self._update_client_contact_from_values(
                        existing,
                        phone=phone,
                        email=email,
                        contact_name=contact_name,
                        document_type=document_type,
                    )
                    return existing
                document = ""
        existing_by_name = _client_by_canonical_name(name)
        if existing_by_name:
            self._update_client_contact_from_values(
                existing_by_name,
                phone=phone,
                email=email,
                contact_name=contact_name,
                document_type=document_type,
            )
            return existing_by_name
        result = create_imported_client(
            full_name=name,
            document_type=document_type,
            document_number=document or None,
            source_origin=Client.SourceOrigin.HISTORICAL_IMPORT,
            phone=phone,
            email=email,
            contact_name=contact_name,
            incomplete_reason="Cliente historico importado desde seccion NOVEDADES.",
        )
        if result.status == "invalid" or not result.client:
            return None
        if not UnitOwnership.objects.filter(client=result.client, is_active=True).exists():
            result.client.is_active = False
            result.client.last_change_reason = "Cliente historico sin titularidad vigente importado desde NOVEDADES."
            result.client.save(update_fields=["is_active", "last_change_reason", "updated_at"])
        if result.status == "created":
            self.created_clients += 1
        return result.client

    def _update_client_contact_from_values(
        self,
        client: Client,
        *,
        phone: str = "",
        email: str = "",
        contact_name: str = "",
        document_type: str | None = None,
    ) -> None:
        update_fields = []
        if document_type and document_type != Client.DocumentType.UNKNOWN and client.document_type == Client.DocumentType.UNKNOWN:
            client.document_type = document_type
            update_fields.append("document_type")
        if phone and not client.phone:
            client.phone = phone
            update_fields.append("phone")
        email = normalize_valid_imported_email(email)
        if email and not client.email:
            client.email = email
            update_fields.append("email")
        if contact_name and not client.address:
            client.address = contact_name
            update_fields.append("address")
        if (
            update_fields
            and client.information_status == Client.InformationStatus.INCOMPLETE
            and client.document_number
            and client.document_type != Client.DocumentType.UNKNOWN
            and (client.phone or client.email)
        ):
            client.information_status = Client.InformationStatus.COMPLETE
            client.incomplete_reason = ""
            update_fields.extend(["information_status", "incomplete_reason"])
        if update_fields:
            client.full_clean()
            client.save(update_fields=update_fields + ["updated_at"])

    def _save_historical_observation(
        self,
        *,
        origin,
        sheet_result,
        source_novelty,
        source_sheet,
        source_row,
        source_order,
        project,
        unit,
        client,
        assignment,
        summary,
        detail,
        historical_section,
        historical_month,
        historical_year,
        payload,
        status=ImportedHistoricalObservation.Status.IMPORTED,
    ) -> ImportedHistoricalObservation:
        dedupe_key = _observation_dedupe_key(
            origin=origin,
            project=project,
            unit=unit,
            client=client,
            assignment=assignment,
            source_sheet=source_sheet,
            summary=summary,
            detail=detail,
            historical_section=historical_section,
            event_key=(payload or {}).get("event_key"),
        )
        defaults = {
                "batch": self.batch,
                "imported_file": self.imported_file,
                "sheet_result": sheet_result,
                "source_novelty": source_novelty,
                "project": project,
                "property_unit": unit,
                "client": client,
                "assignment": assignment,
                "origin": origin,
                "status": status,
                "historical_section": historical_section,
                "historical_month": historical_month,
                "historical_year": historical_year,
                "summary": summary,
                "detail": detail,
                "source_sheet": source_sheet,
                "source_row": source_row,
                "source_order": source_order,
                "source_payload": payload,
                "imported_by": self.user,
            }
        natural_match = ImportedHistoricalObservation.objects.filter(
            origin=origin,
            property_unit=unit,
            client=client,
            assignment=assignment,
            source_novelty=source_novelty,
            source_sheet=source_sheet,
            source_row=source_row,
            source_order=source_order,
            historical_section=historical_section,
        ).first()
        if natural_match:
            for field, value in defaults.items():
                setattr(natural_match, field, value)
            natural_match.dedupe_key = dedupe_key
            natural_match.full_clean()
            natural_match.save()
            observation, created = natural_match, False
        else:
            observation, created = ImportedHistoricalObservation.objects.update_or_create(
                dedupe_key=dedupe_key,
                defaults=defaults,
            )
        if created:
            self.imported_observations += 1
        return observation

    def _single_project(self) -> Project:
        projects = list(self.projects.values())
        if len({project.pk for project in projects}) != 1:
            raise HistoricalImportFinalizationError("No existe un proyecto unico resuelto para la importacion.")
        return projects[0]

    def _single_grouping_type(self) -> GroupingType:
        grouping_types = list(self.grouping_types.values())
        if len({grouping_type.pk for grouping_type in grouping_types}) != 1:
            raise HistoricalImportFinalizationError("No existe un tipo de agrupacion unico resuelto para la importacion.")
        return grouping_types[0]

    def _parent_group_from_context(self, resolution: ImportResolution) -> StructuralGroup | None:
        parent_resolution_id = (resolution.detected_element.structural_context or {}).get("parent_group_resolution_id")
        if parent_resolution_id:
            parent = self.groups.get(int(parent_resolution_id))
            if parent:
                return parent
        grouping_name = (resolution.detected_element.structural_context or {}).get("grouping_name")
        if grouping_name:
            return self.groups_by_name.get(normalize_text(grouping_name))
        return None

    def _trace(
        self,
        entity_kind: str,
        action: str,
        entity_id: int | None = None,
        *,
        sheet_result=None,
        source_row=None,
        source_column="",
        summary="",
    ) -> None:
        self._trace_buffer.append(
            ImportAppliedRecord(
                batch=self.batch,
                imported_file=self.imported_file,
                sheet_result=sheet_result,
                entity_kind=entity_kind,
                entity_id=entity_id,
                action=action,
                source_row=source_row,
                source_column=source_column or "",
                summary=summary,
            )
        )


def _validate_batch_ready(batch: ImportBatch) -> None:
    if batch.import_type != ImportBatch.ImportType.HISTORICAL:
        raise HistoricalImportFinalizationError("Solo los lotes historicos pueden finalizarse con este servicio.")
    if not can_start_historical_finalization(batch):
        raise HistoricalImportFinalizationError("Solo un lote listo puede importarse definitivamente.")


def _validate_batch_continuing(batch: ImportBatch) -> None:
    if batch.import_type != ImportBatch.ImportType.HISTORICAL:
        raise HistoricalImportFinalizationError("Solo los lotes historicos pueden finalizarse con este servicio.")
    if not can_continue_historical_finalization(batch):
        raise HistoricalImportFinalizationError("El lote en procesamiento no cumple los requisitos para continuar.")


def _validate_batch_dependencies(batch: ImportBatch) -> None:
    if has_unresolved_required_pendings(batch):
        raise HistoricalImportFinalizationError("El lote aun tiene pendientes accionables.")
    if has_blocked_dependencies(batch):
        raise HistoricalImportFinalizationError("El lote aun tiene elementos bloqueados por dependencia.")
    if has_open_blocking_issues(batch):
        raise HistoricalImportFinalizationError("El lote contiene incidencias bloqueantes abiertas.")
    if has_failed_historical_files(batch):
        raise HistoricalImportFinalizationError("El lote contiene archivos fallidos.")


def _stored_file_path(imported_file: ImportedFile) -> Path:
    if not imported_file.stored_path:
        raise HistoricalImportFinalizationError("El archivo original no esta conservado para importacion definitiva.")
    path = settings.MEDIA_ROOT / imported_file.stored_path
    if not path.exists():
        raise HistoricalImportFinalizationError("No se encontro el archivo original conservado.")
    return path


def _mark_batch_failed(batch_id: int, exc: Exception) -> None:
    ImportBatch.objects.filter(
        pk=batch_id,
        status__in=[ImportBatch.Status.READY, ImportBatch.Status.PROCESSING],
    ).update(
        status=ImportBatch.Status.FAILED,
        processing_finished_at=timezone.now(),
        summary=json.dumps(
            {
                "progress": {
                    "phase": "failed",
                    "percent": 100,
                    "error": f"No fue posible completar la importacion historica definitiva: {_safe_error(exc)}",
                }
            },
            ensure_ascii=True,
        ),
    )
    ImportedFile.objects.filter(batch_id=batch_id, status=ImportedFile.Status.PROCESSING).update(
        status=ImportedFile.Status.FAILED,
        processing_finished_at=timezone.now(),
        result_message=f"No fue posible completar la importacion historica definitiva: {_safe_error(exc)}",
    )


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, (HistoricalImportFinalizationError, ValidationError)):
        return str(exc)
    return "error interno durante la importacion."


def _created_action(created: bool) -> str:
    return ImportAppliedRecord.Action.CREATED if created else ImportAppliedRecord.Action.REUSED


def _created_record_count(batch: ImportBatch, entity_kind: str) -> int:
    return (
        ImportAppliedRecord.objects.filter(
            batch=batch,
            entity_kind=entity_kind,
            action=ImportAppliedRecord.Action.CREATED,
        )
        .exclude(source_column="__AUDIT__")
        .count()
    )


def _summary_detail_from_cells(cells: list[dict]) -> tuple[str, str]:
    detail_parts = []
    for cell in cells:
        header = normalize_text(cell.get("header") or "")
        value = str(cell.get("value") or "").strip()
        if not value or cell.get("formula"):
            continue
        if header in {"observaciones", "observacion"}:
            detail_parts.append(value)
    detail = "\n".join(detail_parts)
    summary = _best_summary_from_row_cells(cells, detail)
    return summary, detail


def _best_summary_from_row_cells(cells: list[dict], detail: str) -> str:
    candidates = []
    for index, cell in enumerate(cells):
        value = str(cell.get("value") or "").strip()
        if not value or cell.get("formula"):
            continue
        score = _summary_candidate_score(cell, value, detail)
        if score:
            candidates.append((score, len(value), index, value))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    best = candidates[0]
    if len(candidates) > 1 and candidates[1][0] == best[0] and candidates[1][1] == best[1]:
        return ""
    return best[3]


def _summary_candidate_score(cell: dict, value: str, detail: str) -> int:
    header = normalize_text(cell.get("header") or "")
    if header in {"observaciones", "observacion"} or header.startswith("__"):
        return 0
    if header in {
        "#",
        "vendedor",
        "encargo fiduciario",
        "nuevo encargo fiduciario",
        "apto",
        "apartamento",
        "local",
        "bodega",
        "unidad",
        "vinc",
        "cedula cliente",
        "documento cliente",
        "identificacion cliente",
        "identificacion",
        "telefono",
        "email",
        "e mail",
        "correo",
        "contacto",
    }:
        return 0
    if _looks_like_non_summary_value(value):
        return 0
    tokens = _summary_tokens(value)
    if not tokens:
        return 0
    detail_tokens = set(_summary_tokens(detail))
    has_event_shape = _looks_descriptive_novelty_text(value)
    if detail_tokens:
        overlap = set(tokens) & detail_tokens
        if has_event_shape and overlap:
            return 100 + len(overlap)
        if len(tokens) <= 3 and len(overlap) == len(set(tokens)):
            return 80 + len(overlap)
        if has_event_shape and not _looks_descriptive_novelty_text(detail):
            return 60
        return 0
    return 50 if has_event_shape else 0


def _summary_tokens(value: str) -> list[str]:
    ignored = {"de", "del", "la", "las", "los", "el", "a", "y", "sin", "con"}
    return [
        token
        for token in re.findall(r"[a-z]+", normalize_text(value))
        if len(token) > 2 and token not in ignored
    ]


def _looks_like_non_summary_value(value: str) -> bool:
    text = value.strip()
    normalized = normalize_text(text)
    compact = normalized.replace(" ", "")
    if len(text) > 80:
        return True
    if "@" in text:
        return True
    if _looks_numeric_only(text) or "%" in text:
        return True
    if "$" in text and not _looks_descriptive_novelty_text(text):
        return True
    if re.search(r"\b[A-Z]{1,4}\d{3,}", text, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"[0-9 .,'/-]+", text):
        return True
    if re.search(r"\b[a-z]{3,4}[./-]\d{1,2}/\d{2,4}\b", normalized):
        return True
    if re.search(r"\b[a-z]{3,4}[./-]\d{1,2}/\d{2,4}[a-z]+", normalized):
        return True
    if re.search(r"\b[a-z]{3,4}\s+\d{1,2}\s+\d{2,4}[a-z]+", normalized):
        return True
    if compact.startswith("ncr") or compact.startswith("nc") and any(ch.isdigit() for ch in compact):
        return True
    return False


def _looks_descriptive_novelty_text(value: str) -> bool:
    normalized = normalize_text(value)
    compact = normalized.replace(" ", "")
    if value.strip().startswith("*"):
        return True
    keywords = {
        "termin",
        "terminacion",
        "retiro",
        "retirado",
        "cesion",
        "exclusion",
        "sustitucion",
        "anulacion",
        "cambio",
        "traslado",
        "inclusion",
        "desist",
        "desistimiento",
    }
    return any(keyword in compact for keyword in keywords)


def _historical_event_fragments(detail: str) -> list[str]:
    text = (detail or "").strip()
    if not text:
        return []
    pipe_parts = [part.strip() for part in re.split(r"\s*\|\s*", text) if part.strip()]
    if len(pipe_parts) > 1 and all(_looks_like_observation_fragment(part) for part in pipe_parts):
        return pipe_parts
    dash_parts = [part.strip() for part in re.split(r"\s+-\s+", text) if part.strip()]
    if len(dash_parts) > 1 and all(_looks_like_observation_fragment(part) for part in dash_parts):
        return dash_parts
    return [text]


def _normalized_historical_text_key(value: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(value or "")).strip()


def _looks_like_observation_fragment(value: str) -> bool:
    if _looks_like_historical_event_fragment(value):
        return True
    text = clean_text(value) or ""
    if len(text) < 5:
        return False
    if re.fullmatch(r"[A-Z]{1,4}\d+(?:[A-Z]{1,4})?", text.strip(), flags=re.IGNORECASE):
        return False
    if re.fullmatch(r"[A-Z]{1,4}\d+(?:[A-Z]{1,4})?(?:\s*-\s*[A-Z]{1,4}\d+(?:[A-Z]{1,4})?)+", text.strip(), flags=re.IGNORECASE):
        return False
    return bool(re.search(r"[A-Za-zÁÉÍÓÚáéíóú]", text))


def _looks_like_historical_event_fragment(value: str) -> bool:
    if not value:
        return False
    normalized = normalize_text(value)
    compact = normalized.replace(" ", "")
    has_event_word = any(
        keyword in compact
        for keyword in (
            "cesion",
            "traslado",
            "inclusion",
            "exclusion",
            "desist",
            "termin",
            "sustitucion",
        )
    )
    return has_event_word and _historical_event_effective_date("", value, []) is not None


def _historical_novelty_type(*values: str) -> tuple[str, str]:
    compact = " ".join(normalize_text(value).replace(" ", "") for value in values if value)
    if "cesion" in compact or "sustitucion" in compact:
        return OperationalNovelty.NoveltyType.CESSION, ""
    if "traslado" in compact:
        return OperationalNovelty.NoveltyType.OTHER, "TRASLADO"
    if "inclusion" in compact:
        return OperationalNovelty.NoveltyType.OTHER, "INCLUSION"
    if "exclusion" in compact:
        return OperationalNovelty.NoveltyType.EXCLUSION, ""
    if "desist" in compact:
        return OperationalNovelty.NoveltyType.OTHER, "DESISTIMIENTO"
    if "termin" in compact:
        return OperationalNovelty.NoveltyType.OTHER, "TERMINACION"
    return OperationalNovelty.NoveltyType.HISTORICAL, ""


def _is_exportable_historical_novelty_type(novelty_type: str, other_type: str) -> bool:
    if novelty_type != OperationalNovelty.NoveltyType.HISTORICAL:
        return True
    return bool(other_type)


def _is_historical_context_row(row) -> bool:
    return getattr(row, "context", "main_table") != "main_table"


def _historical_event_key(
    *,
    unit: PropertyUnit,
    effective_date,
    novelty_type: str,
    other_type: str,
    previous_mention: str,
    new_mention: str,
    summary: str,
    detail: str,
) -> str:
    reference_match = re.search(r"\bNC\w+\b", normalize_text(detail or ""), flags=re.IGNORECASE)
    reference = reference_match.group(0) if reference_match else ""
    normalized_detail = normalize_text(detail)
    parts = [
        str(unit.pk),
        str(effective_date or ""),
        novelty_type,
        normalize_text(other_type),
        normalize_text(previous_mention),
        normalize_text(new_mention),
        reference,
        "" if normalized_detail else normalize_text(summary),
        normalized_detail,
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _dedupe_historical_states(states: list[_HistoricalNoveltyState]) -> list[_HistoricalNoveltyState]:
    deduped = []
    seen = set()
    for state in states:
        key = (
            state.unit.pk,
            state.client.pk if state.client else None,
            state.assignment.pk if state.assignment else None,
            normalize_text(state.summary),
            normalize_text(state.detail),
        )
        if key in seen:
            continue
        deduped.append(state)
        seen.add(key)
    return deduped


def _historical_event_completeness_score(event: _HistoricalNoveltyEvent) -> tuple[int, int, int]:
    return (
        1 if event.summary else 0,
        1 if event.source_state.novelty else 0,
        len(event.source_state.original_cells or []),
    )


def _state_pair_for_historical_event(
    event: _HistoricalNoveltyEvent,
    candidates: list[_HistoricalNoveltyState],
    *,
    unit: PropertyUnit,
) -> tuple[_HistoricalNoveltyState | None, _HistoricalNoveltyState | None]:
    states_with_event = [
        state
        for state in candidates
        if state.unit.pk == unit.pk and state.client and state.assignment and _historical_state_contains_event(state, event)
    ]
    if len(states_with_event) < 2:
        return None, None
    previous_state = _unique_state_matching_mention(event.previous_mention, states_with_event, unit=unit)
    new_state = _unique_state_matching_mention(event.new_mention, states_with_event, unit=unit)
    if previous_state and new_state and previous_state.assignment.pk != new_state.assignment.pk:
        return previous_state, new_state
    return None, None


def _historical_state_contains_event(state: _HistoricalNoveltyState, event: _HistoricalNoveltyEvent) -> bool:
    fragments = _historical_event_fragments(state.detail)
    if not fragments and state.detail:
        fragments = [state.detail]
    for fragment in fragments:
        summary = _best_summary_from_row_cells(state.original_cells, fragment) or state.summary
        novelty_type, other_type = _historical_novelty_type(summary, fragment, state.historical_section)
        effective_date = _historical_event_effective_date(summary, fragment, state.original_cells)
        previous_mention, new_mention = _assignment_change_mentions(summary, fragment)
        key = _historical_event_key(
            unit=state.unit,
            effective_date=effective_date,
            novelty_type=novelty_type,
            other_type=other_type,
            previous_mention=previous_mention,
            new_mention=new_mention,
            summary=summary,
            detail=fragment,
        )
        if key == event.event_key:
            return True
    return False


def _historical_state_payload(state: _HistoricalNoveltyState | None) -> dict:
    if not state:
        return {}
    return {
        "row": state.row_number,
        "client": state.client.full_name if state.client else "",
        "assignment": state.assignment.assignment_number if state.assignment else "",
    }


def _validate_historical_event_state_pair(
    *,
    previous_state: _HistoricalNoveltyState | None,
    previous_client: Client | None,
    previous_assignment: FiduciaryAssignment | None,
    new_state: _HistoricalNoveltyState | None,
    new_client: Client | None,
    new_assignment: FiduciaryAssignment | None,
) -> None:
    if previous_state and (
        previous_client != previous_state.client or previous_assignment != previous_state.assignment
    ):
        raise HistoricalImportFinalizationError("La novedad historica mezcla titular o encargo anterior de estados diferentes.")
    if new_state and (new_client != new_state.client or new_assignment != new_state.assignment):
        raise HistoricalImportFinalizationError("La novedad historica mezcla titular o encargo nuevo de estados diferentes.")


def _unique_state_matching_mention(
    mention: str,
    candidates: list[_HistoricalNoveltyState],
    *,
    unit: PropertyUnit,
) -> _HistoricalNoveltyState | None:
    matches = [
        candidate
        for candidate in candidates
        if candidate.unit.pk == unit.pk and candidate.client and _name_mention_matches_client(mention, candidate.client)
    ]
    client_ids = {match.client.pk for match in matches if match.client}
    if len(client_ids) != 1:
        return None
    same_client_matches = [match for match in matches if match.client and match.client.pk in client_ids]
    assignment_ids = {match.assignment.pk for match in same_client_matches if match.assignment}
    if len(assignment_ids) > 1:
        return None
    return same_client_matches[0] if same_client_matches else None


def _looks_like_cession_text(*values: str) -> bool:
    compact = " ".join(normalize_text(value).replace(" ", "") for value in values if value)
    return "cesion" in compact or "sustitucion" in compact


def _looks_like_assignment_change_text(*values: str) -> bool:
    compact = " ".join(normalize_text(value).replace(" ", "") for value in values if value)
    return any(keyword in compact for keyword in ("cesion", "traslado", "cesionarras", "trasladoarras"))


def _assignment_change_mentions(*values: str) -> tuple[str, str]:
    text = " ".join(str(value or "") for value in values if value).strip()
    if not text:
        return "", ""
    normalized = normalize_text(text)
    match = re.search(
        r"(?:cesion|traslado)(?:\s*/?\s*arras)?\s+de\s+(?P<previous>.+?)\s+a\s+(?P<new>.+)",
        normalized,
    )
    if not match:
        return "", ""
    previous = _clean_assignment_change_mention(match.group("previous"))
    new = _clean_assignment_change_mention(match.group("new"))
    return previous, new


def _clean_assignment_change_mention(value: str) -> str:
    text = re.split(
        r"\s+(?:por|segun|según|mediante|valor|antic|arras|apto|apartamento|unidad|identificacion|identificación|cedula|cédula|cc|nit)\b",
        value,
        maxsplit=1,
    )[0]
    text = re.sub(r"\bnc\w+\b", " ", text)
    text = re.sub(r"\b[a-z]{3,4}[./-]\d{1,2}/\d{2,4}\b", " ", text)
    return " ".join(_significant_name_tokens(text))


def _unique_client_matching_mention(mention: str, candidates: list[Client]) -> Client | None:
    matches = [candidate for candidate in candidates if _name_mention_matches_client(mention, candidate)]
    return matches[0] if len(matches) == 1 else None


def _canonical_client_name_matches(client: Client, name: str) -> bool:
    return normalize_text(client.full_name) == normalize_text(name) or _name_mention_matches_client(name, client)


def _client_by_canonical_name(name: str) -> Client | None:
    matches = [
        client
        for client in Client.objects.filter(source_origin=Client.SourceOrigin.HISTORICAL_IMPORT)
        if _canonical_client_name_matches(client, name)
    ]
    return matches[0] if len(matches) == 1 else None


def _name_mention_matches_client(mention: str, client: Client) -> bool:
    mention_tokens = set(_significant_name_tokens(mention))
    if len(mention_tokens) < 2:
        return False
    client_tokens = set(_significant_name_tokens(client.full_name))
    return bool(client_tokens) and mention_tokens.issubset(client_tokens)


def _significant_name_tokens(value: str) -> list[str]:
    ignored = {"de", "del", "la", "las", "los", "el", "a", "y"}
    return [
        token
        for token in re.findall(r"[a-z0-9]+", normalize_text(value))
        if len(token) > 1 and token not in ignored and not token.isdigit()
    ]


def _looks_numeric_only(value: str) -> bool:
    text = value.replace(".", "").replace(",", "").replace("$", "").replace(" ", "").strip()
    return bool(text) and text.replace("-", "").isdigit()


def _first_cell_by_header(cells: list[dict], headers: set[str]):
    cell = _first_cell_payload_by_header(cells, headers)
    return cell.get("value") if cell else None


def _cell_values_by_header(cells: list[dict], headers: set[str]) -> list[str]:
    values = []
    for cell in cells:
        if normalize_text(cell.get("header") or "") not in headers:
            continue
        value = cell.get("value")
        if value not in ("", None):
            values.append(str(value).strip())
    return values


def _first_cell_payload_by_header(cells: list[dict], headers: set[str]):
    for cell in cells:
        if normalize_text(cell.get("header") or "") in headers:
            value = cell.get("value")
            if value not in ("", None):
                return cell
    return None


def _cell_payload_value(cells: list[dict], pseudo_header: str):
    for cell in cells:
        if cell.get("header") == pseudo_header:
            return cell.get("value")
    return None


def _int_or_none(value):
    try:
        return int(value) if value not in ("", None) else None
    except (TypeError, ValueError):
        return None


def _observation_dedupe_key(
    *,
    origin,
    project,
    unit,
    client,
    assignment,
    source_sheet,
    summary,
    detail,
    historical_section,
    event_key=None,
) -> str:
    parts = [
        str(origin),
        str(project.pk if project else ""),
        str(unit.pk if unit else ""),
        str(assignment.assignment_number if assignment else ""),
        normalize_text(str(event_key or "")),
        normalize_text(detail),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _reason(batch: ImportBatch) -> str:
    return f"Importacion historica definitiva lote #{batch.pk}."


def _code_and_name(resolution: ImportResolution) -> tuple[str, str]:
    raw_value = _non_placeholder(resolution.detected_element.raw_value)
    code = _non_placeholder(resolution.create_code) or raw_value
    name = _non_placeholder(resolution.create_name) or raw_value or code
    if not code or not name:
        raise HistoricalImportFinalizationError("La resolucion para crear entidad requiere codigo y nombre.")
    return code, name


def _non_placeholder(value) -> str:
    value = (value or "").strip()
    return "" if value == "(sin valor)" else value


def _find_group(project, grouping_type, parent, code, name):
    queryset = StructuralGroup.objects.filter(project=project, grouping_type=grouping_type, parent=parent)
    if code:
        found = queryset.filter(code=code).first()
        if found:
            return found
    if name:
        return next((group for group in queryset if normalize_text(group.name) == normalize_text(name)), None)
    return None


def _find_unit(project, parent, code, name):
    queryset = PropertyUnit.objects.filter(project=project, structural_group=parent)
    if code:
        found = queryset.filter(code=code).first()
        if found:
            return found
    if name:
        return next((unit for unit in queryset if normalize_text(unit.name) == normalize_text(name)), None)
    return None


def _document_type(value) -> str:
    normalized = normalize_text(value or "")
    mapping = {
        "cc": Client.DocumentType.CITIZENSHIP_ID,
        "cedula": Client.DocumentType.CITIZENSHIP_ID,
        "cedula cliente": Client.DocumentType.CITIZENSHIP_ID,
        "cedula de ciudadania": Client.DocumentType.CITIZENSHIP_ID,
        "documento cliente": Client.DocumentType.CITIZENSHIP_ID,
        "identificacion": Client.DocumentType.CITIZENSHIP_ID,
        "identificacion cliente": Client.DocumentType.CITIZENSHIP_ID,
        "ce": Client.DocumentType.FOREIGN_ID,
        "nit": Client.DocumentType.TAX_ID,
        "pasaporte": Client.DocumentType.PASSPORT,
        "passport": Client.DocumentType.PASSPORT,
    }
    return mapping.get(normalized, Client.DocumentType.UNKNOWN)


def _assignment_operational_dates(assignment) -> dict[str, object]:
    return {
        "adhesion_contract_date": _parse_historical_payment_date(getattr(assignment, "adhesion_contract_date", None)),
        "promise_date": _parse_historical_payment_date(getattr(assignment, "promise_date", None)),
        "promised_delivery_date": _parse_historical_payment_date(getattr(assignment, "promised_delivery_date", None)),
        "actual_delivery_date": _parse_historical_payment_date(getattr(assignment, "actual_delivery_date", None)),
    }


def _parse_historical_payment_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _historical_payment_date_text(value)
    if not text:
        return None
    if " " in text:
        text = text.split(" ", 1)[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    match = re.match(r"^(?P<month>[A-Za-zÁÉÍÓÚáéíóú]{3,4})[./-](?P<day>\d{1,2})/(?P<year>\d{2,4})$", text)
    if match:
        month = MONTHS.get(normalize_text(match.group("month")).upper())
        if not month:
            return None
        year = int(match.group("year"))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, int(match.group("day"))).date()
        except ValueError:
            return None
    return None


def _historical_payment_date_text(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.strip("()").strip()
    text = re.sub(r"(TRASLADO|TRASL|CESION|INCLUSION|EXCLUSION|SUSTITUCION)$", "", text, flags=re.IGNORECASE).strip()
    text = text.strip("()").strip()
    text = text.rstrip("Ff").strip()
    return text


def _historical_event_effective_date(summary: str, detail: str, cells: list[dict]):
    texts = [summary or "", detail or ""]
    texts.extend(str(cell.get("value") or "") for cell in cells if cell.get("value"))
    event_words = r"(CESION|TRASLADO|INCLUSION|EXCLUSION|SUSTITUCION)"
    date_pattern = r"[A-Za-zÃÃ‰ÃÃ“ÃšÃ¡Ã©Ã­Ã³Ãº]{3,4}[./-]\d{1,2}/\d{2,4}"
    for text in texts:
        if not re.search(event_words, text, flags=re.IGNORECASE):
            continue
        for match in re.finditer(rf"{date_pattern}\s*{event_words}?", text, flags=re.IGNORECASE):
            parsed = _parse_historical_payment_date(match.group(0))
            if parsed:
                return parsed
    return None


def _historical_payment_concept(category: str, receipt: str, value_source_header: str = "") -> str:
    labels = {
        RECEIPT_CATEGORY_ORDINARY: "ORDINARIO",
        RECEIPT_CATEGORY_CREDIT: "CREDITO",
        RECEIPT_CATEGORY_SUBSIDY: "SUBSIDIO",
        RECEIPT_CATEGORY_TRANSFER: "TRASLADO",
        RECEIPT_CATEGORY_CESSION: "CESION",
    }
    label = labels.get(category, category.upper())
    if category == RECEIPT_CATEGORY_SUBSIDY and not receipt:
        normalized_header = normalize_text(value_source_header)
        if "gobierno" in normalized_header:
            label = "SUBSIDIO GOBIERNO"
        elif "caja" in normalized_header or "compensacion" in normalized_header:
            label = "SUBSIDIO CAJA"
    clean_receipt = _historical_payment_receipt_for_display(receipt, category)
    if not clean_receipt:
        return label
    receipt_label = "Recibo " if category in {RECEIPT_CATEGORY_ORDINARY, RECEIPT_CATEGORY_CREDIT, RECEIPT_CATEGORY_SUBSIDY} else ""
    return f"{label} | {receipt_label}{clean_receipt}".strip()


def _historical_payment_receipt_for_display(receipt: str, category: str) -> str:
    text = str(receipt or "").strip().strip("()").strip()
    if category in {RECEIPT_CATEGORY_TRANSFER, RECEIPT_CATEGORY_CESSION}:
        match = re.match(r"^(?P<number>[A-Z]+\d+)(?P<context>.*)$", text, flags=re.IGNORECASE)
        if match:
            number = match.group("number").strip()
            context = match.group("context").strip()
            context = re.sub(r"^[\s._/-]*(TRASLADO|TRASL|CESION)[\s._/-]*", "", context, flags=re.IGNORECASE).strip()
            context = re.sub(r"[\s._/-]+", " ", context).strip()
            context = re.sub(r"(?<=\d)(?=[A-ZÁÉÍÓÚ])", " ", context, flags=re.IGNORECASE).strip()
            marker = "TRASL" if category == RECEIPT_CATEGORY_TRANSFER else "CESION"
            if context:
                return f"{marker} {context} | Recibo {number}"
            return f"Recibo {number}"
    return text
