# Centenario Gestion Fiduciaria

Sistema web local para la gestion de informacion fiduciaria de Constructora Centenario S.A.S.

## Alcance implementado

El proyecto cuenta actualmente con las Fases 1, 2A, 2B y 3 implementadas:

- Proyecto Django.
- Aplicaciones `core`, `users`, `real_estate` y `fiduciary`.
- Usuario personalizado configurado como `AUTH_USER_MODEL`.
- Login y logout.
- Autenticacion por usuario o correo electronico.
- Control de acceso reutilizable por roles oficiales.
- Consulta funcional de usuarios sin administracion de cuentas desde el panel empresarial.
- Estructura inmobiliaria configurable: proyectos, tipos de agrupacion, agrupaciones jerarquicas y unidades inmobiliarias.
- Clientes, titularidades historicas y encargos fiduciarios.
- Formulario temporal de validacion para encargos fiduciarios; el flujo definitivo vendra de importaciones.
- Plantillas base para Login, Inicio, Gestion de Usuarios y Estructura Inmobiliaria.
- Listados y detalles de clientes, titularidades y encargos fiduciarios.
- Configuracion PostgreSQL por variables de entorno.
- Archivos estaticos, logging tecnico inicial y pruebas con pytest.

No se implementan todavia pagos, novedades completas, importaciones, auditoria funcional, API, Docker ni integraciones externas.

## Requisitos

- Python 3.13 como version de referencia del proyecto.
- PostgreSQL 17 como base de datos de operacion.
- Microsoft Edge como navegador soportado.

## Configuracion local

1. Crear entorno virtual.
2. Instalar dependencias desde `requirements.txt`.
3. Crear un archivo `.env` basado en `.env.example`.
4. Crear la base de datos PostgreSQL indicada en las variables `DB_*`.
5. Ejecutar migraciones.
6. Crear un superusuario.
7. Levantar el servidor local.

## Comandos

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py createsuperuser
.\.venv\Scripts\python manage.py runserver
```

## Pruebas

```powershell
.\.venv\Scripts\pytest
```

Las pruebas pueden usar una configuracion aislada en `config.test_settings` para validar sin depender de datos reales.

Esta ejecucion aislada usa SQLite porque el entorno local puede no tener PostgreSQL disponible. No reemplaza la validacion oficial contra PostgreSQL 17.

La validacion oficial del proyecto se ejecuta contra PostgreSQL configurado mediante las variables `DB_*`:

```powershell
.\.venv\Scripts\python manage.py check
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py makemigrations --check --dry-run
.\.venv\Scripts\pytest --ds=config.settings
```
