# Modelo tecnico de estructura inmobiliaria

## Alcance

Este documento registra el modelo implementado en la Fase 2B para representar la estructura inmobiliaria configurable del sistema Centenario Gestion Fiduciaria.

No incluye clientes, encargos fiduciarios, pagos, importaciones, novedades, auditoria funcional, historial completo, API ni integraciones externas.

## Entidades

### Project

Representa un proyecto inmobiliario.

Campos principales:

- `code`: codigo obligatorio y unico globalmente.
- `name`: nombre obligatorio.
- `description`: descripcion opcional.
- `is_active`: estado activo/inactivo.
- `last_change_reason`: ultimo motivo registrado en una modificacion o cambio de estado.

### GroupingType

Catalogo configurable de tipos de agrupacion estructural, por ejemplo Torre, Bloque, Manzana, Sector, Etapa o Edificio.

Campos principales:

- `code`: codigo obligatorio y unico globalmente.
- `name`: nombre obligatorio.
- `description`: descripcion opcional.
- `is_active`: estado activo/inactivo.
- `last_change_reason`: ultimo motivo registrado en una modificacion o cambio de estado.

### StructuralGroup

Representa una agrupacion estructural dentro de un proyecto. Puede depender directamente del proyecto o de otra agrupacion del mismo proyecto.

Campos principales:

- `project`: proyecto propietario.
- `grouping_type`: tipo configurable de la agrupacion.
- `parent`: agrupacion padre opcional.
- `code`: codigo opcional.
- `name`: nombre opcional.
- `description`: descripcion opcional.
- `is_active`: estado activo/inactivo.
- `last_change_reason`: ultimo motivo registrado en una modificacion o cambio de estado.

Reglas:

- Si no tiene padre, el codigo es unico dentro del proyecto.
- Si tiene padre, el codigo es unico dentro del padre inmediato.
- La unicidad del codigo aplica solo cuando el codigo tiene un valor real.
- Debe existir codigo, nombre o ambos.
- La agrupacion padre debe pertenecer al mismo proyecto.
- La jerarquia no puede contener ciclos.

### PropertyUnit

Representa la unidad inmobiliaria final. Puede depender directamente de un proyecto o de cualquier agrupacion estructural.

Campos principales:

- `project`: proyecto propietario.
- `structural_group`: agrupacion estructural padre opcional.
- `code`: codigo opcional.
- `name`: nombre opcional.
- `description`: descripcion opcional.
- `is_active`: estado activo/inactivo.
- `last_change_reason`: ultimo motivo registrado en una modificacion o cambio de estado.

Reglas:

- Si no tiene agrupacion padre, el codigo es unico dentro del proyecto.
- Si tiene agrupacion padre, el codigo es unico dentro de esa agrupacion.
- La unicidad del codigo aplica solo cuando el codigo tiene un valor real.
- Debe existir codigo, nombre o ambos.
- La agrupacion padre debe pertenecer al mismo proyecto de la unidad.

## Relaciones

- `Project` 1 a N `StructuralGroup`.
- `Project` 1 a N `PropertyUnit`.
- `GroupingType` 1 a N `StructuralGroup`.
- `StructuralGroup` 1 a N `StructuralGroup` mediante `parent`, permitiendo jerarquia configurable.
- `StructuralGroup` 1 a N `PropertyUnit`.

Todas las relaciones usan integridad referencial con `PROTECT`; no se implementa eliminacion fisica.

## Constraints de base de datos

- `real_estate_project_code_unique`: codigo unico global para proyectos.
- `real_estate_grouping_type_code_unique`: codigo unico global para tipos de agrupacion.
- `real_estate_structural_group_root_code_unique`: codigo unico no vacio por proyecto para agrupaciones sin padre.
- `real_estate_structural_group_parent_code_unique`: codigo unico no vacio por padre inmediato para agrupaciones con padre.
- `real_estate_property_unit_project_code_unique`: codigo unico no vacio por proyecto para unidades sin agrupacion.
- `real_estate_property_unit_group_code_unique`: codigo unico no vacio por agrupacion para unidades con agrupacion.

## Consultas contextuales

La consulta de unidades se realiza por contexto:

- Proyecto.
- Tipo de agrupacion.
- Agrupacion concreta.
- Opcion explicita para unidades asociadas directamente al proyecto.

La pantalla no muestra indiscriminadamente unidades de todos los proyectos al ingresar sin contexto.

## Permisos

- Administrador de Contabilidad y superusuarios tecnicos: consultar, crear, editar, activar e inactivar.
- Comercial: consultar.

Las restricciones se aplican en servidor mediante permisos reutilizables. La interfaz oculta acciones no permitidas, pero no se usa como unica barrera de seguridad.

## Modificaciones y motivos

Toda edicion o cambio de estado requiere un motivo. En esta fase se almacena unicamente el ultimo motivo en la entidad afectada. La auditoria funcional y la visualizacion historica quedan fuera de esta fase.
