# Modelo tecnico de clientes, titularidades y encargos

## App

La Fase 3 incorpora la app `fiduciary` para concentrar clientes, titularidades y encargos fiduciarios. Se mantiene una sola app porque estas entidades forman un nucleo funcional con relaciones directas entre si.

No incluye pagos, importaciones, cartera, novedades completas, auditoria funcional, API ni integraciones externas.

## Entidades

### Client

Representa una persona natural o empresa identificada como cliente o titular.

Campos principales:

- `document_type`: tipo de documento controlado por `TextChoices` (`cc`, `ce`, `passport`, `nit`).
- `document_number`: numero de documento normalizado con `strip()`.
- `first_names`: nombres, opcional para empresas.
- `last_names_or_company`: apellidos o razon social.
- `phone`, `email`, `address`: medios de contacto.
- `information_status`: `complete` o `incomplete`.
- `is_active`: activo/inactivo.
- `last_change_reason`, `created_at`, `updated_at`.

Reglas:

- La combinacion `document_type` + `document_number` identifica al cliente.
- Un cliente completo creado manualmente debe tener al menos telefono o correo electronico.
- La direccion puede almacenarse, pero no cuenta como medio de contacto suficiente para guardar un cliente manual.
- Los clientes manuales se crean como completos.
- Un cliente inactivo no se elimina ni pierde relaciones historicas.

### UnitOwnership

Relacion historica entre `Client` y `PropertyUnit`.

Campos principales:

- `client`.
- `property_unit`.
- `is_primary`.
- `start_date`.
- `end_date`.
- `is_active`.
- `last_change_reason`, `created_at`, `updated_at`.

Reglas:

- Una unidad puede tener varios titulares.
- Solo puede existir un titular principal vigente por unidad.
- Un cliente no puede tener dos titularidades vigentes simultaneas sobre la misma unidad.
- Las titularidades anteriores se conservan.
- Fecha final no puede ser anterior a fecha inicial.
- Vigente implica ausencia de fecha final.
- Finalizada implica fecha final.
- No se asignan clientes inactivos ni unidades inactivas a titularidades vigentes manuales.

### FiduciaryAssignment

Representa un encargo fiduciario asociado a una unica unidad inmobiliaria.

Campos principales:

- `assignment_number`: numero asignado por fiducia.
- `property_unit`.
- `start_date`.
- `end_date`.
- `is_active`.
- `observations`.
- `last_change_reason`, `created_at`, `updated_at`.

Reglas:

- Una unidad puede tener varios encargos historicos.
- Solo puede existir un encargo vigente por unidad.
- Los encargos anteriores se conservan.
- Un encargo vigente debe tener titular principal vigente.
- No se almacenan pagos ni valores temporales.
- La creacion manual de encargos existe temporalmente solo para validaciones de Fase 3. En el flujo funcional definitivo, los encargos provienen del libro inicial o de archivos/reportes suministrados por la fiduciaria.

### FiduciaryAssignmentHolder

Relacion explicita entre un encargo fiduciario y sus titulares.

Campos principales:

- `assignment`.
- `client`.
- `is_primary`.
- `start_date`.
- `end_date`.
- `is_active`.
- `last_change_reason`, `created_at`, `updated_at`.

Reglas:

- Un encargo vigente debe tener exactamente un titular principal vigente.
- Puede tener varios titulares secundarios vigentes.
- El titular del encargo debe tener titularidad vigente sobre la misma unidad.
- No puede haber dos relaciones vigentes identicas cliente-encargo.

## Cardinalidades

- `Client` 1 a N `UnitOwnership`.
- `PropertyUnit` 1 a N `UnitOwnership`.
- `PropertyUnit` 1 a N `FiduciaryAssignment`.
- `FiduciaryAssignment` 1 a N `FiduciaryAssignmentHolder`.
- `Client` 1 a N `FiduciaryAssignmentHolder`.

## Constraints de PostgreSQL

- `fiduciary_client_document_unique`: documento unico por tipo y numero.
- `fiduciary_client_information_status_valid`: estados de informacion validos.
- `fiduciary_unit_one_active_primary_owner`: maximo un titular principal vigente por unidad.
- `fiduciary_unit_active_client_unique`: maximo una titularidad vigente cliente-unidad.
- `fiduciary_assignment_one_active_per_unit`: maximo un encargo vigente por unidad.
- `fiduciary_assignment_one_active_primary_holder`: maximo un titular principal vigente por encargo.
- `fiduciary_assignment_active_client_unique`: maximo una relacion vigente cliente-encargo.

## Permisos

Contabilidad y superusuarios tecnicos:

- Consultar, crear, editar, activar/inactivar clientes.
- Consultar, crear y finalizar titularidades.
- Consultar, crear, editar y cerrar encargos.
- Administrar titulares asociados a encargos.

Comercial:

- Consultar clientes.
- Consultar titularidades.
- Consultar encargos fiduciarios.
- Consultar unidades.

Comercial no puede ejecutar operaciones manuales de escritura. Las restricciones se aplican en servidor.

## Operaciones atomicas

Se usa `transaction.atomic()` en operaciones que modifican una o varias entidades:

- Crear clientes.
- Editar clientes.
- Activar/inactivar clientes.
- Crear/finalizar titularidades.
- Crear encargos con titulares.
- Editar/cerrar encargos.
- Agregar/finalizar titulares de encargos.

## Preparacion para fases posteriores

El modelo conserva historicos de titularidades y encargos, permitiendo representar posteriormente cesiones, cambios de unidad, cambios de titular principal y cambios de encargo sin sobrescribir informacion previa.

Los pagos futuros deberan relacionarse con `FiduciaryAssignment`. Esta fase no crea modelo de pagos ni campos simulados de valor o fecha de pago.

El formulario manual de creacion de encargos queda marcado en la interfaz como herramienta temporal de validacion. Debe retirarse o bloquearse cuando se implemente la carga oficial de encargos desde archivos.

## Decision pendiente

La documentacion revisada no define si `assignment_number` debe ser unico globalmente, unico por proyecto o unico por unidad. Por esa razon la Fase 3 no implementa un constraint de unicidad sobre el numero de encargo.

## Diagrama textual

```mermaid
erDiagram
    PROPERTY_UNIT ||--o{ UNIT_OWNERSHIP : has
    CLIENT ||--o{ UNIT_OWNERSHIP : owns
    PROPERTY_UNIT ||--o{ FIDUCIARY_ASSIGNMENT : has
    FIDUCIARY_ASSIGNMENT ||--o{ FIDUCIARY_ASSIGNMENT_HOLDER : has
    CLIENT ||--o{ FIDUCIARY_ASSIGNMENT_HOLDER : holds
```

## Pruebas

Validacion oficial:

```powershell
.\.venv\Scripts\python manage.py check
.\.venv\Scripts\python manage.py makemigrations --check --dry-run
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\pytest --ds=config.settings
```
