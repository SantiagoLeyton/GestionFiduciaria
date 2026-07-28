# Analizador de libro historico

## Alcance

La Fase 4B incorpora un analizador de solo lectura para el libro historico real ubicado en `samples/fiduciary/historical/`.

No persiste datos, no consulta la base de datos, no crea modelos Django, no realiza matching contra estructura existente y no implementa resolucion asistida.

## Arquitectura

El codigo se ubica en `fiduciary/imports/historical/`:

- `data.py`: dataclasses internas del resultado.
- `normalize.py`: normalizacion de texto, numeros, meses y documentos.
- `readers.py`: lectura de workbooks `xlsx` y ruta opcional para `xls`.
- `parser.py`: parser principal `HistoricalWorkbookParser`.

## API

```python
from fiduciary.imports.historical import HistoricalWorkbookParser

result = HistoricalWorkbookParser(path).parse()
```

El resultado es un `WorkbookData` en memoria con hojas, filas historicas, incidencias y estadisticas.

La Fase 4C agrega una capa de analisis contra la estructura inmobiliaria existente:

```python
from fiduciary.imports.historical import analyze_historical_import

analysis = analyze_historical_import(
    batch=batch,
    file_path=path,
    grouping_type_hint="Sector",
)
```

Esta capa ejecuta el parser, registra resultados preparatorios de importacion y devuelve una previsualizacion. No crea proyectos, tipos de agrupacion, agrupaciones, unidades, clientes, titularidades, encargos ni pagos.

## Deteccion de encabezados

El parser escanea las primeras filas y selecciona la fila con mayor puntaje de encabezados historicos reconocidos:

- `ENCARGO FIDUCIARIO`.
- columna de unidad (`APTO`, `LOCAL`, `BODEGA` o equivalente).
- `CEDULA CLIENTE`.
- `NOMBRE CLIENTE`.
- columnas `RECIBO FIDUCIA XXX/YYYY`.

Las columnas se detectan por encabezado normalizado, no por posicion fija.

## Columnas mensuales

Se detectan encabezados con patron:

```text
RECIBO FIDUCIA XXX/YYYY
```

Cada columna queda representada como `DetectedPaymentColumn` con letra, indice, encabezado original, mes y ano. La coleccion se ordena cronologicamente.

## Filas

El parser ignora filas vacias, separadores y totales. Cada fila valida se transforma en `HistoricalRow`, que contiene:

- proyecto.
- tipo de agrupacion como pista opcional entregada por el consumidor del parser. El parser no resuelve ni fuerza `Sector`.
- agrupacion.
- unidad.
- encargo.
- titulares en orden.
- pagos mensuales positivos.

Cuando una hoja contiene una seccion posterior de `NOVEDADES`, esa seccion no se interpreta como unidades principales. El encabezado se clasifica como separador y las filas posteriores se conservan en memoria como `HistoricalNovelty`, con su contexto estructural, unidad, encargo y celdas originales. Estas filas siguen contando como omitidas del bloque principal, pero ya no generan la incidencia generica `HISTORICAL_NOVELTY_SECTION_SKIPPED`.

## Formulas

El parser nunca ejecuta formulas. Si una celda relevante tiene formula:

- registra si hay formula.
- registra si existe valor cacheado.
- genera incidencia estructurada cuando corresponde.

## Limitaciones

El lector `xlsx` se implementa con XML/ZIP de solo lectura para conservar informacion de formulas sin agregar dependencias. La ruta `xls` queda preparada mediante `xlrd` si existe en el entorno; no hay archivo `.xls` real en los samples actuales para validacion automatica completa.

Las estadisticas distinguen apariciones de titulares, numeros de encargo distintos y entradas de pago detectadas. No deben interpretarse como clientes Django unicos ni como pagos persistidos.

## Analisis conservador

El analizador de Fase 4C compara los valores estructurales extraidos contra `Project`, `GroupingType`, `StructuralGroup` y `PropertyUnit`.

La asociacion automatica solo ocurre cuando existe un unico candidato por codigo o nombre normalizado dentro del contexto correcto. Si no hay candidato o aparecen varios candidatos posibles, se crea una resolucion pendiente para revision del usuario.

Para el libro real de Springfield, el parser extrae el proyecto y las agrupaciones del archivo. El tipo de agrupacion puede recibirse como pista del consumidor mediante `grouping_type_hint`; no queda acoplado globalmente a `Sector` y puede ser reemplazado por otros tipos configurados en la base de datos.

Los unicos registros que esta capa puede crear o actualizar son preparatorios: `ImportBatch`, `ImportedFile`, `ImportedSheetResult`, `ImportRowIssue`, `DetectedStructureElement`, `ImportResolution` e `ImportedHistoricalNovelty`.

## Novedades historicas temporales

La preparacion historica persiste las novedades detectadas en `ImportedHistoricalNovelty`. Este modelo conserva:

- lote, archivo y hoja.
- fila de origen.
- proyecto, tipo de agrupacion, agrupacion, unidad y encargo detectados.
- celdas originales de la fila, incluyendo coordenada, encabezado, valor, formula y metadata de cache.
- resumen sanitizado para interfaz.
- estado temporal de preparacion.

La previsualizacion muestra conteos y una muestra contextual de hoja, fila, proyecto, agrupacion, unidad y encargo. No expone nombres completos, documentos ni valores de celdas originales en masa.

`ImportedHistoricalNovelty` no aplica novedades sobre clientes, encargos, unidades ni pagos. La importacion definitiva sera responsable de interpretar y aplicar esas novedades segun las reglas funcionales futuras.

## Previsualizacion y resolucion asistida

La interfaz de carga historica permite a Contabilidad y Comercial crear un lote, cargar un archivo `.xlsx` o `.xls`, ejecutar el analisis conservador y revisar una previsualizacion agregada. Ambos roles pueden resolver pendientes preparatorios. Comercial mantiene restriccion de no modificar registros de negocio existentes.

La resolucion asistida permite clasificar cada elemento pendiente como proyecto, tipo de agrupacion, agrupacion o unidad, y decidir entre asociar una entidad existente, preparar una creacion futura o ignorar el valor. La decision se guarda en `ImportResolution` y se aplica a las apariciones equivalentes del lote sin crear entidades definitivas.

Cuando una unidad nueva ya tiene proyecto y agrupacion resueltos, queda preparada automaticamente como `create_new`. El lote pasa a `ready` solo cuando no quedan elementos pendientes. El boton de importacion definitiva permanece deshabilitado hasta la siguiente fase.

Las agrupaciones pendientes pueden resolverse desde el propio flujo mediante una pantalla guiada. Contabilidad puede asociar la agrupacion detectada con una agrupacion existente filtrada por proyecto y tipo, o preparar la creacion futura de una agrupacion nueva indicando proyecto, tipo y nombre. Esta accion no crea `StructuralGroup`; solo actualiza `ImportResolution`.

Al resolver una agrupacion, el backend vuelve a procesar masivamente las unidades hijas del mismo contexto estructural. Si la agrupacion existe, las unidades se buscan de forma conservadora dentro de esa agrupacion padre; si no existe, las unidades quedan preparadas como `create_new` y conservan en el contexto la resolucion preparatoria de su agrupacion. La pantalla de pendientes tambien permite volver a analizar elementos pendientes contra la estructura inmobiliaria actual sin recargar el archivo ni crear otro lote.

Los conteos distinguen pendientes accionables de elementos bloqueados por dependencia. Una unidad cuyo proyecto, tipo o agrupacion padre aun no esta resuelto se mantiene como `DetectedStructureElement.Status.DETECTED` con resolucion `unresolved`; no se presenta como decision individual ni cuenta como pendiente accionable. Cuando el padre queda resuelto, el servicio prepara sus unidades hijas automaticamente.

Al resolver proyecto o tipo de agrupacion desde el formulario generico, el backend vuelve a analizar los pendientes y propaga el contexto resuelto a agrupaciones y unidades hijas. Esto permite que una agrupacion como `T2` pueda resolverse contra la estructura existente o quedar como creacion futura sin que sus unidades aparezcan como decisiones independientes.

## Duplicados historicos

La carga historica bloquea duplicados globales por SHA-256 antes de ejecutar el parser. La regla aplica solo a `ImportedFile.file_type = historical`, por lo que un archivo futuro de reportes no queda bloqueado unicamente por compartir hash.

La proteccion existe en dos niveles: el servicio reserva el archivo antes de analizar y lanza `DuplicateHistoricalImportError` si ya existe, y la base de datos mantiene la restriccion condicional `fiduciary_imported_file_historical_sha_unique`. La migracion `0003` limpia duplicados preparatorios previos de forma deterministica antes de crear la restriccion: conserva el archivo con referencias de negocio si existiera, luego el lote en estado mas avanzado y finalmente el mas antiguo.

## Cancelacion de intentos

Los lotes historicos en estados preparatorios (`analyzing`, `awaiting_resolution`, `ready` y `failed`) pueden cancelarse antes de ejecutar una importacion definitiva. La cancelacion se realiza mediante `cancel_import_batch`, dentro de una transaccion y con bloqueo del lote.

El servicio valida que no existan pagos u otras referencias definitivas asociadas al archivo antes de eliminar datos. Si el lote solo contiene informacion preparatoria, elimina resoluciones, elementos detectados, novedades historicas temporales, novedades funcionales preparatorias, incidencias, resultados de hojas, archivos importados y el propio lote. Al eliminar `ImportedFile`, el SHA-256 queda disponible para volver a cargar el archivo corregido.

Por ahora no se conserva el `ImportBatch` cancelado porque mantener el archivo preparatorio bloquearia la proteccion global por SHA-256. Esta decision debe revisarse cuando exista auditoria funcional completa.
