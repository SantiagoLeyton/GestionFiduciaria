from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from fiduciary.imports.historical import HistoricalWorkbookParser
from fiduciary.imports.historical.normalize import normalize_text
from fiduciary.models import ImportedFile
from real_estate.models import Project, PropertyUnit


@dataclass(frozen=True)
class UnitFieldCandidate:
    unit: PropertyUnit
    field: str
    value: object
    normalized_value: object
    source: str
    sheet: str
    row: int


BACKFILL_FIELDS = {
    "area": "AREA",
    "property_value": "VALOR INMUEBLE",
    "financial_entity": "ENTIDAD FINANCIERA",
}


class Command(BaseCommand):
    help = (
        "Completa datos historicos faltantes de PropertyUnit desde libros almacenados "
        "sin modificar otros datos."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Reporta cambios sin guardar datos.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        media_root = Path(settings.MEDIA_ROOT).resolve()
        files = ImportedFile.objects.filter(
            file_type=ImportedFile.FileType.HISTORICAL,
        ).exclude(stored_path="")
        project_index = _project_index()
        unit_index = _unit_index()
        candidates_by_unit_field: dict[tuple[int, str], list[UnitFieldCandidate]] = defaultdict(list)
        found = defaultdict(int)
        updated = defaultdict(int)
        skipped_existing = defaultdict(int)
        conflicts = defaultdict(int)
        missing_files = unresolved = rows_without_values = 0

        for imported_file in files.order_by("created_at", "pk"):
            path = _safe_stored_path(media_root, imported_file.stored_path)
            if not path or not path.exists():
                missing_files += 1
                self.stdout.write(f"Archivo no disponible: {imported_file.original_name}")
                continue
            workbook = HistoricalWorkbookParser(path).parse()
            for sheet in workbook.sheets:
                for row in sheet.rows:
                    values = {
                        field: _candidate_value(getattr(row, field, None), field)
                        for field in BACKFILL_FIELDS
                    }
                    values = {field: value for field, value in values.items() if value not in (None, "")}
                    if not values:
                        rows_without_values += 1
                        continue
                    unit = _resolve_unit(row, project_index, unit_index)
                    if not unit:
                        unresolved += 1
                        self.stdout.write(
                            f"Unidad no resuelta: proyecto={row.project} agrupacion={row.grouping_code or row.grouping_name} "
                            f"unidad={row.unit_code or row.unit_name} archivo={imported_file.original_name} fila={row.row_number}"
                        )
                        continue
                    for field, value in values.items():
                        found[field] += 1
                        candidates_by_unit_field[(unit.pk, field)].append(
                            UnitFieldCandidate(
                                unit=unit,
                                field=field,
                                value=value,
                                normalized_value=_normalized_candidate_value(value, field),
                                source=imported_file.original_name,
                                sheet=sheet.name,
                                row=row.row_number,
                            )
                        )

        for (_, field), candidates in candidates_by_unit_field.items():
            unit = candidates[0].unit
            if not _field_is_missing(unit, field):
                skipped_existing[field] += 1
                continue
            distinct = {candidate.normalized_value for candidate in candidates}
            if len(distinct) > 1:
                conflicts[field] += 1
                values = ", ".join(str(candidate.value) for candidate in candidates)
                self.stdout.write(
                    f"Conflicto {BACKFILL_FIELDS[field]}: proyecto={unit.project} "
                    f"agrupacion={unit.structural_group or '-'} unidad={unit.name or unit.code} valores={values}"
                )
                continue
            if not dry_run:
                setattr(unit, field, candidates[0].value)
                unit.save(update_fields=[field, "updated_at"])
            updated[field] += 1

        updated_label = "actualizables" if dry_run else "actualizadas"
        metrics = []
        for field, label in BACKFILL_FIELDS.items():
            metric_key = _metric_key(label)
            metrics.append(f"{metric_key}_encontradas={found[field]}")
            metrics.append(f"{metric_key}_{updated_label}={updated[field]}")
            metrics.append(f"{metric_key}_existentes_omitidas={skipped_existing[field]}")
            metrics.append(f"{metric_key}_conflictos={conflicts[field]}")
        self.stdout.write(
            self.style.SUCCESS(
                "Backfill de datos historicos de unidad terminado: "
                f"{'; '.join(metrics)}; unidades_no_resueltas={unresolved}; "
                f"archivos_no_disponibles={missing_files}; filas_sin_datos={rows_without_values}; dry_run={dry_run}."
            )
        )


def _safe_stored_path(media_root: Path, stored_path: str) -> Path | None:
    try:
        path = (media_root / stored_path).resolve()
    except (OSError, RuntimeError):
        return None
    if media_root == path or media_root in path.parents:
        return path
    return None


def _project_index() -> dict[str, Project]:
    index = {}
    for project in Project.objects.all():
        for value in {project.code, project.name, str(project)}:
            normalized = normalize_text(value)
            if normalized:
                index[normalized] = project
    return index


def _unit_index() -> dict[tuple[int, str, str], list[PropertyUnit]]:
    index: dict[tuple[int, str, str], list[PropertyUnit]] = defaultdict(list)
    units = PropertyUnit.objects.select_related("project", "structural_group")
    for unit in units:
        unit_keys = {normalize_text(unit.code), normalize_text(unit.name)}
        group_keys = {""}
        if unit.structural_group_id:
            group_keys.update(
                {
                    normalize_text(unit.structural_group.code),
                    normalize_text(unit.structural_group.name),
                    normalize_text(str(unit.structural_group)),
                }
            )
        for group_key in {key for key in group_keys if key}:
            for unit_key in {key for key in unit_keys if key}:
                index[(unit.project_id, group_key, unit_key)].append(unit)
    return index


def _resolve_unit(row, project_index: dict[str, Project], unit_index: dict[tuple[int, str, str], list[PropertyUnit]]) -> PropertyUnit | None:
    project = project_index.get(normalize_text(row.project))
    if not project:
        return None
    grouping_keys = {
        normalize_text(row.grouping_code),
        normalize_text(row.grouping_name),
        normalize_text(row.sheet_name),
    }
    unit_keys = {normalize_text(row.unit_code), normalize_text(row.unit_name)}
    matches = []
    seen = set()
    for grouping_key in {key for key in grouping_keys if key}:
        for unit_key in {key for key in unit_keys if key}:
            for unit in unit_index.get((project.pk, grouping_key, unit_key), []):
                if unit.pk not in seen:
                    seen.add(unit.pk)
                    matches.append(unit)
    return matches[0] if len(matches) == 1 else None


def _candidate_value(value, field: str):
    if field == "financial_entity":
        return " ".join(str(value or "").split())
    return value


def _normalized_candidate_value(value, field: str):
    if field == "financial_entity":
        return normalize_text(value)
    return value


def _field_is_missing(unit: PropertyUnit, field: str) -> bool:
    value = getattr(unit, field)
    if field == "financial_entity":
        return not value
    return value is None


def _metric_key(label: str) -> str:
    return normalize_text(label).replace(" ", "_")
