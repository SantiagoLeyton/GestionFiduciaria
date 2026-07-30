import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from fiduciary.models import (
    Client,
    DetectedStructureElement,
    FiduciaryAssignment,
    FiduciaryAssignmentHolder,
    ImportAppliedRecord,
    ImportBatch,
    ImportedFile,
    ImportedHistoricalObservation,
    ImportedHistoricalNovelty,
    ImportResolution,
    ImportRowIssue,
    OperationalNovelty,
    Payment,
    UnitOwnership,
)
from fiduciary.permissions import can_import_fiduciary
from fiduciary.services import create_imported_client, create_payment
from real_estate.models import GroupingType, Project, PropertyUnit, StructuralGroup

from .normalize import normalize_text
from .parser import HistoricalWorkbookParser


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
    preserved_novelties: int = 0
    imported_observations: int = 0
    imported_novelties: int = 0


def store_historical_import_file(*, imported_file: ImportedFile, source_path) -> None:
    source = Path(source_path)
    target_dir = settings.MEDIA_ROOT / "imports" / "historical"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{imported_file.sha256}{source.suffix.lower()}"
    if not target.exists():
        shutil.copyfile(source, target)
    imported_file.stored_path = str(target.relative_to(settings.MEDIA_ROOT))
    imported_file.save(update_fields=["stored_path"])


def finalize_historical_import(*, batch_id: int, user) -> HistoricalImportFinalizationResult:
    if not can_import_fiduciary(user):
        raise PermissionDenied

    try:
        with transaction.atomic():
            batch = ImportBatch.objects.select_for_update().get(pk=batch_id)
            _validate_batch_ready(batch)
            files = list(
                ImportedFile.objects.select_for_update()
                .filter(batch=batch, file_type=ImportedFile.FileType.HISTORICAL)
                .order_by("order", "pk")
            )
            if len(files) != 1:
                raise HistoricalImportFinalizationError("La importacion historica definitiva requiere un unico archivo.")
            imported_file = files[0]
            path = _stored_file_path(imported_file)

            batch.status = ImportBatch.Status.PROCESSING
            batch.processing_started_at = timezone.now()
            batch.summary = "Importacion historica definitiva en ejecucion."
            batch.save(update_fields=["status", "processing_started_at", "summary"])
            imported_file.status = ImportedFile.Status.PROCESSING
            imported_file.save(update_fields=["status"])

            workbook = HistoricalWorkbookParser(path).parse()
            if _has_blocking_issues(batch):
                raise HistoricalImportFinalizationError("El lote contiene incidencias bloqueantes abiertas.")

            context = _FinalizationContext(batch=batch, imported_file=imported_file, user=user)
            context.materialize_structure()
            context.import_rows(workbook)
            context.preserve_historical_novelties()

            now = timezone.now()
            batch.status = ImportBatch.Status.COMPLETED
            batch.processing_finished_at = now
            batch.imported_at = now
            batch.imported_by = user
            batch.summary = context.summary()
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
            imported_file.result_message = "Importacion historica definitiva completada."
            imported_file.save(update_fields=["status", "processing_finished_at", "processed_rows", "result_message"])
            return context.result()
    except Exception as exc:
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
        self.preserved_novelties = 0
        self.imported_observations = 0
        self.imported_novelties = 0

    def materialize_structure(self) -> None:
        self._materialize_kind(DetectedStructureElement.InferredKind.PROJECT)
        self._materialize_kind(DetectedStructureElement.InferredKind.GROUPING_TYPE)
        self._materialize_kind(DetectedStructureElement.InferredKind.STRUCTURAL_GROUP)
        self._materialize_kind(DetectedStructureElement.InferredKind.PROPERTY_UNIT)

    def import_rows(self, workbook) -> None:
        sheet_results = {sheet.sheet_name: sheet for sheet in self.imported_file.sheet_results.all()}
        for sheet in workbook.sheets:
            sheet_result = sheet_results.get(sheet.name)
            for row in sheet.rows:
                unit = self._unit_for_row(row)
                clients = [self._client_for_historical_client(client) for client in row.clients]
                if not row.assignment or not row.assignment.assignment_number:
                    raise HistoricalImportFinalizationError(
                        f"La fila {row.row_number} de la hoja {row.sheet_name} no tiene numero de encargo."
                    )
                assignment = self._assignment_for_row(row, unit)
                for client, historical_client in zip(clients, row.clients, strict=False):
                    ownership = self._ownership_for_client(unit, client, historical_client.is_primary)
                    self._assignment_holder_for_client(assignment, client, historical_client.is_primary, ownership)
                for payment in row.payments:
                    self._payment_for_row(assignment, payment, sheet_result)
                self._main_row_observation(row, unit, assignment, clients[0] if clients else None, sheet_result)
        self.batch.processed_rows = workbook.statistics.valid_rows

    def preserve_historical_novelties(self) -> None:
        for novelty in ImportedHistoricalNovelty.objects.filter(batch=self.batch).select_related("sheet_result"):
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
            self._historical_novelty_observation(novelty)

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
            preserved_novelties=self.preserved_novelties,
            imported_observations=self.imported_observations,
            imported_novelties=self.imported_novelties,
        )

    def summary(self) -> str:
        return (
            "Importacion historica definitiva completada. "
            f"Proyectos creados: {self.created_projects}; tipos creados: {self.created_grouping_types}; "
            f"agrupaciones creadas: {self.created_structural_groups}; unidades creadas: {self.created_property_units}; "
            f"clientes creados: {self.created_clients}; encargos creados: {self.created_assignments}; "
            f"pagos creados: {self.created_payments}; pagos duplicados omitidos: {self.duplicate_payments}; "
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
                grouping_name = normalize_text(element.structural_context.get("grouping_name", ""))
                self.units_by_context[(grouping_name, normalize_text(element.raw_value))] = unit

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
        key = (normalize_text(row.grouping_name), normalize_text(row.unit_code or row.unit_name or ""))
        unit = self.units_by_context.get(key)
        if not unit:
            raise HistoricalImportFinalizationError(
                f"No se encontro unidad resuelta para la fila {row.row_number} de la hoja {row.sheet_name}."
            )
        return unit

    def _client_for_historical_client(self, historical_client) -> Client:
        document_type = _document_type(historical_client.document_type)
        result = create_imported_client(
            full_name=historical_client.name,
            document_type=document_type,
            document_number=historical_client.document_number,
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
        self._trace(ImportAppliedRecord.EntityKind.CLIENT, action, result.client.pk)
        return result.client

    def _ownership_for_client(self, unit: PropertyUnit, client: Client, is_primary: bool) -> UnitOwnership:
        if is_primary:
            primary = UnitOwnership.objects.filter(property_unit=unit, is_active=True, is_primary=True).first()
            if primary and primary.client_id != client.pk:
                raise HistoricalImportFinalizationError("La unidad ya tiene un titular principal vigente diferente.")
        ownership, created = UnitOwnership.objects.get_or_create(
            client=client,
            property_unit=unit,
            is_active=True,
            defaults={"is_primary": is_primary, "start_date": self.today, "last_change_reason": _reason(self.batch)},
        )
        if created:
            self.created_ownerships += 1
        self._trace(ImportAppliedRecord.EntityKind.UNIT_OWNERSHIP, _created_action(created), ownership.pk)
        return ownership

    def _assignment_for_row(self, row, unit: PropertyUnit) -> FiduciaryAssignment:
        assignment_number = row.assignment.assignment_number.strip()
        assignment = FiduciaryAssignment.objects.filter(assignment_number=assignment_number).first()
        if assignment:
            if assignment.property_unit_id != unit.pk:
                raise HistoricalImportFinalizationError(
                    f"El encargo {assignment_number} ya existe asociado a una unidad diferente."
                )
            self._trace(ImportAppliedRecord.EntityKind.FIDUCIARY_ASSIGNMENT, ImportAppliedRecord.Action.REUSED, assignment.pk)
            return assignment
        assignment = FiduciaryAssignment.objects.create(
            assignment_number=assignment_number,
            property_unit=unit,
            start_date=self.today,
            observations="Creado desde importacion historica.",
            last_change_reason=_reason(self.batch),
        )
        self.created_assignments += 1
        self._trace(ImportAppliedRecord.EntityKind.FIDUCIARY_ASSIGNMENT, ImportAppliedRecord.Action.CREATED, assignment.pk)
        return assignment

    def _assignment_holder_for_client(
        self,
        assignment: FiduciaryAssignment,
        client: Client,
        is_primary: bool,
        ownership: UnitOwnership,
    ) -> FiduciaryAssignmentHolder:
        if is_primary:
            primary = assignment.holders.filter(is_active=True, is_primary=True).first()
            if primary and primary.client_id != client.pk:
                raise HistoricalImportFinalizationError("El encargo ya tiene un titular principal vigente diferente.")
        holder, created = FiduciaryAssignmentHolder.objects.get_or_create(
            assignment=assignment,
            client=client,
            is_active=True,
            defaults={"is_primary": is_primary, "start_date": ownership.start_date, "last_change_reason": _reason(self.batch)},
        )
        if created:
            self.created_assignment_holders += 1
        self._trace(ImportAppliedRecord.EntityKind.ASSIGNMENT_HOLDER, _created_action(created), holder.pk)
        return holder

    def _payment_for_row(self, assignment: FiduciaryAssignment, payment, sheet_result) -> None:
        result = create_payment(
            assignment=assignment,
            amount=payment.amount,
            movement_type=Payment.MovementType.HISTORICAL_PAYMENT,
            source_file=self.imported_file,
            source_sheet=sheet_result.sheet_name if sheet_result else "",
            source_row=payment.source_row,
            source_column=payment.source_column,
            source_header=payment.source_header,
            source_had_formula=payment.has_formula,
            date_precision=Payment.DatePrecision.MONTH,
            period_year=payment.year,
            period_month=payment.month,
        )
        if result.status == "created" and result.payment:
            self.created_payments += 1
            self._trace(
                ImportAppliedRecord.EntityKind.PAYMENT,
                ImportAppliedRecord.Action.CREATED,
                result.payment.pk,
                sheet_result=sheet_result,
                source_row=payment.source_row,
                source_column=payment.source_column,
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

    def _main_row_observation(self, row, unit: PropertyUnit, assignment: FiduciaryAssignment, client: Client | None, sheet_result) -> None:
        detail = (row.observation or "").strip()
        if not detail:
            return
        self._save_historical_observation(
            origin=ImportedHistoricalObservation.Origin.MAIN_TABLE_OBSERVATION,
            sheet_result=sheet_result,
            source_novelty=None,
            source_sheet=row.sheet_name,
            source_row=row.row_number,
            source_order=row.row_number,
            project=unit.project,
            unit=unit,
            client=client,
            assignment=assignment,
            summary="",
            detail=detail,
            historical_section="",
            historical_month=None,
            historical_year=None,
            payload={"row_type": "main_table"},
        )

    def _historical_novelty_observation(self, novelty: ImportedHistoricalNovelty) -> None:
        unit = self._unit_from_imported_novelty(novelty)
        assignment = self._assignment_from_imported_novelty(novelty, unit)
        client = self._client_from_imported_novelty(novelty)
        summary, detail = _summary_detail_from_cells(novelty.original_cells)
        if not any([summary, detail, client, unit, assignment]):
            return
        if not unit:
            return
        operational_novelty, created = OperationalNovelty.objects.update_or_create(
            source_novelty=novelty,
            defaults={
                "batch": self.batch,
                "imported_file": self.imported_file,
                "project": unit.project if unit else self._project_from_imported_novelty(novelty),
                "property_unit": unit,
                "novelty_type": OperationalNovelty.NoveltyType.HISTORICAL,
                "origin": OperationalNovelty.Origin.HISTORICAL_IMPORT,
                "status": OperationalNovelty.Status.IMPORTED if unit else OperationalNovelty.Status.DESCRIPTIVE,
                "historical_client": client,
                "historical_assignment": assignment,
                "summary": summary,
                "detail": detail,
                "source_sheet": novelty.sheet_result.sheet_name,
                "source_row": novelty.row_number,
                "historical_section": _cell_payload_value(novelty.original_cells, "__historical_section__") or "",
                "historical_month": _int_or_none(_cell_payload_value(novelty.original_cells, "__section_month__")),
                "historical_year": _int_or_none(_cell_payload_value(novelty.original_cells, "__section_year__")),
                "source_payload": {"cells": novelty.original_cells},
                "created_by": self.user,
            },
        )
        operational_novelty.full_clean()
        operational_novelty.save()
        if created:
            self.imported_novelties += 1
        if assignment:
            payments = assignment.payments.filter(source_file=novelty.imported_file, source_row=novelty.row_number)
            if payments.exists():
                pass

    def _project_from_imported_novelty(self, novelty: ImportedHistoricalNovelty) -> Project | None:
        project_name = (novelty.project_name or "").strip()
        if not project_name:
            return None
        query = Project.objects.filter(Q(code__iexact=project_name) | Q(name__iexact=project_name))
        return query.first() if query.count() == 1 else None

    def _unit_from_imported_novelty(self, novelty: ImportedHistoricalNovelty) -> PropertyUnit | None:
        unit_value = novelty.unit_code or novelty.unit_name
        if not unit_value:
            return None
        key = (normalize_text(novelty.grouping_name), normalize_text(unit_value))
        unit = self.units_by_context.get(key)
        if unit:
            return unit
        project = self._project_from_imported_novelty(novelty)
        if not project:
            return None
        query = PropertyUnit.objects.filter(project=project).filter(Q(code__iexact=unit_value) | Q(name__iexact=unit_value))
        return query.first() if query.count() == 1 else None

    def _assignment_from_imported_novelty(self, novelty: ImportedHistoricalNovelty, unit: PropertyUnit | None) -> FiduciaryAssignment | None:
        number = novelty.assignment_number.strip()
        if not number:
            return None
        assignment = FiduciaryAssignment.objects.filter(assignment_number=number).first()
        if assignment:
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
        self._trace(
            ImportAppliedRecord.EntityKind.FIDUCIARY_ASSIGNMENT,
            ImportAppliedRecord.Action.CREATED,
            assignment.pk,
            sheet_result=novelty.sheet_result,
            source_row=novelty.row_number,
        )
        return assignment

    def _client_from_imported_novelty(self, novelty: ImportedHistoricalNovelty) -> Client | None:
        document = _first_cell_by_header(novelty.original_cells, {"cedula cliente", "documento cliente", "identificacion", "identificacion cliente"})
        name = _first_cell_by_header(novelty.original_cells, {"nombre cliente"})
        document = str(document).strip() if document else ""
        name = str(name).strip() if name else ""
        if document:
            existing = Client.objects.filter(document_number=document).first()
            if existing:
                return existing
        if not name:
            return None
        result = create_imported_client(
            full_name=name,
            document_number=document or None,
            source_origin=Client.SourceOrigin.HISTORICAL_IMPORT,
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
            source_sheet=source_sheet,
            source_row=source_row,
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
        ImportAppliedRecord.objects.create(
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


def _validate_batch_ready(batch: ImportBatch) -> None:
    if batch.import_type != ImportBatch.ImportType.HISTORICAL:
        raise HistoricalImportFinalizationError("Solo los lotes historicos pueden finalizarse con este servicio.")
    if batch.status != ImportBatch.Status.READY:
        raise HistoricalImportFinalizationError("Solo un lote listo puede importarse definitivamente.")
    if batch.detected_elements.filter(status=DetectedStructureElement.Status.NEEDS_REVIEW).exists():
        raise HistoricalImportFinalizationError("El lote aun tiene pendientes accionables.")
    if batch.detected_elements.filter(
        status=DetectedStructureElement.Status.DETECTED,
        resolution__action=ImportResolution.Action.UNRESOLVED,
    ).exists():
        raise HistoricalImportFinalizationError("El lote aun tiene elementos bloqueados por dependencia.")
    if batch.files.filter(status=ImportedFile.Status.FAILED).exists():
        raise HistoricalImportFinalizationError("El lote contiene archivos fallidos.")


def _stored_file_path(imported_file: ImportedFile) -> Path:
    if not imported_file.stored_path:
        raise HistoricalImportFinalizationError("El archivo original no esta conservado para importacion definitiva.")
    path = settings.MEDIA_ROOT / imported_file.stored_path
    if not path.exists():
        raise HistoricalImportFinalizationError("No se encontro el archivo original conservado.")
    return path


def _has_blocking_issues(batch: ImportBatch) -> bool:
    return ImportRowIssue.objects.filter(
        imported_file__batch=batch,
        severity=ImportRowIssue.Severity.BLOCKING,
        status=ImportRowIssue.Status.OPEN,
    ).exists()


def _mark_batch_failed(batch_id: int, exc: Exception) -> None:
    ImportBatch.objects.filter(
        pk=batch_id,
        status__in=[ImportBatch.Status.READY, ImportBatch.Status.PROCESSING],
    ).update(
        status=ImportBatch.Status.FAILED,
        processing_finished_at=timezone.now(),
        summary=f"No fue posible completar la importacion historica definitiva: {_safe_error(exc)}",
    )
    ImportedFile.objects.filter(batch_id=batch_id, status=ImportedFile.Status.PROCESSING).update(
        status=ImportedFile.Status.FAILED,
        processing_finished_at=timezone.now(),
        result_message="No fue posible completar la importacion historica definitiva.",
    )


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, (HistoricalImportFinalizationError, ValidationError)):
        return str(exc)
    return "error interno durante la importacion."


def _created_action(created: bool) -> str:
    return ImportAppliedRecord.Action.CREATED if created else ImportAppliedRecord.Action.REUSED


def _summary_detail_from_cells(cells: list[dict]) -> tuple[str, str]:
    detail_parts = []
    summary_parts = []
    seen = set()
    for cell in cells:
        header = normalize_text(cell.get("header") or "")
        value = str(cell.get("value") or "").strip()
        if not value or cell.get("formula"):
            continue
        if header in {"observaciones", "observacion"}:
            detail_parts.append(value)
            continue
        if _cell_is_summary_candidate(cell, value):
            key = normalize_text(value)
            if key not in seen:
                summary_parts.append(value)
                seen.add(key)
    return " | ".join(summary_parts), "\n".join(detail_parts)


def _cell_is_summary_candidate(cell: dict, value: str) -> bool:
    header = normalize_text(cell.get("header") or "")
    if header == "nombre cliente":
        return _looks_descriptive_novelty_text(value)
    ignored_headers = {
        "#",
        "vendedor",
        "encargo fiduciario",
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
        "nombre cliente",
        "telefono",
        "email",
        "correo",
        "contacto",
    }
    if header in ignored_headers or header.startswith("recibo fiducia"):
        return False
    normalized = normalize_text(value)
    if not normalized or normalized in {"0", "1", "#"}:
        return False
    if _looks_numeric_only(value) or "%" in value:
        return False
    return any(ch.isalpha() for ch in value)


def _looks_descriptive_novelty_text(value: str) -> bool:
    normalized = normalize_text(value)
    compact = normalized.replace(" ", "")
    if value.strip().startswith("*"):
        return True
    keywords = {"termin", "terminacion", "retiro", "retirado", "cesion", "exclusion", "sustitucion", "anulacion", "cambio"}
    return any(keyword in compact for keyword in keywords)


def _looks_numeric_only(value: str) -> bool:
    text = value.replace(".", "").replace(",", "").replace("$", "").replace(" ", "").strip()
    return bool(text) and text.replace("-", "").isdigit()


def _first_cell_by_header(cells: list[dict], headers: set[str]):
    for cell in cells:
        if normalize_text(cell.get("header") or "") in headers:
            value = cell.get("value")
            if value not in ("", None):
                return value
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


def _observation_dedupe_key(*, origin, project, unit, client, assignment, source_sheet, summary, detail, historical_section) -> str:
    parts = [
        str(origin),
        str(project.pk if project else ""),
        str(unit.pk if unit else ""),
        str(client.document_number if client and client.document_number else client.pk if client else ""),
        str(assignment.assignment_number if assignment else ""),
        normalize_text(source_sheet),
        normalize_text(historical_section),
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
        "cedula de ciudadania": Client.DocumentType.CITIZENSHIP_ID,
        "ce": Client.DocumentType.FOREIGN_ID,
        "nit": Client.DocumentType.TAX_ID,
        "pasaporte": Client.DocumentType.PASSPORT,
        "passport": Client.DocumentType.PASSPORT,
    }
    return mapping.get(normalized, Client.DocumentType.UNKNOWN)
