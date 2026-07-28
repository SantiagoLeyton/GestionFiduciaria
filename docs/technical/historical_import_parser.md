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

Los unicos registros que esta capa puede crear o actualizar son preparatorios: `ImportBatch`, `ImportedFile`, `ImportedSheetResult`, `ImportRowIssue`, `DetectedStructureElement` e `ImportResolution`.
