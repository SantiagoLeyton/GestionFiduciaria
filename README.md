<div align="center">

# Gestión Fiduciaria

</div>

<p align="center">
  <img src="static/assets/banner.png" alt="Gestión Fiduciaria" width="100%">
</p>

Aplicación web para la administración y consulta de información fiduciaria.

<div align="center">

![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=flat-square&logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django-5.2-092E20?style=flat-square&logo=django&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17-4169E1?style=flat-square&logo=postgresql&logoColor=white)
![Estado](https://img.shields.io/badge/Estado-Finalizado-2f7d45?style=flat-square)
![Licencia](https://img.shields.io/badge/Licencia-Todos_los_derechos_reservados-lightgrey?style=flat-square)

</div>

---

## Aviso

Este proyecto fue desarrollado como parte de las prácticas empresariales del programa de Ingeniería de Software de la Corporación Universitaria Empresarial Alexander von Humboldt.

El repositorio documenta únicamente el software desarrollado. No contiene información confidencial ni procesos internos de Constructora Centenario.

---

## Descripción

**Gestión Fiduciaria** es una aplicación web desarrollada para centralizar la gestión, consulta y trazabilidad de información fiduciaria mediante una plataforma organizada, segura y accesible desde navegador.

El sistema integra módulos para administración de información estructural, consulta de registros fiduciarios, importación de archivos, seguimiento de pagos, observaciones, novedades y auditoría operativa.

---

## Objetivo del Proyecto

El propósito del software es proporcionar una herramienta web que permita:

- Centralizar información fiduciaria en una única plataforma.
- Automatizar cargas y consultas que antes podían depender de archivos dispersos.
- Mantener trazabilidad de operaciones relevantes.
- Disminuir procesos manuales repetitivos.
- Facilitar la consulta organizada de información.
- Proveer una base técnica mantenible y extensible.

---

## Características Principales

- Autenticación de usuarios.
- Control de acceso por roles.
- Importación de libro histórico.
- Importación de reportes fiduciarios.
- Gestión y consulta de proyectos.
- Gestión de tipos de agrupación.
- Gestión de agrupaciones estructurales.
- Gestión y consulta de unidades inmobiliarias.
- Gestión y consulta de clientes.
- Registro y consulta de titularidades.
- Consulta de encargos fiduciarios.
- Registro y consulta de observaciones.
- Registro y consulta de novedades.
- Consulta de pagos.
- Auditoría de operaciones.
- Interfaz con modo claro y modo oscuro.

---

## Tecnologías

| Tecnología | Uso en el proyecto |
|---|---|
| Python | Lenguaje principal del backend |
| Django | Framework web principal |
| PostgreSQL | Base de datos relacional |
| Bootstrap | Base de estilos e interfaz responsive |
| HTML | Estructura de plantillas |
| CSS | Personalización visual del sistema |
| JavaScript | Interacciones de interfaz |
| OpenPyXL | Referencia técnica para trabajo con archivos Excel |
| Pandas | Referencia técnica para procesamiento tabular |
| xlrd | Lectura de archivos Excel en formato `.xls` |
| Git | Control de versiones |
| GitHub | Alojamiento del repositorio |

> Nota: las dependencias instalables del proyecto se encuentran definidas en `requirements.txt`.

---

## Arquitectura General

El sistema sigue una arquitectura monolítica modular basada en Django.

La estructura separa responsabilidades entre:

- **Presentación:** plantillas Django, HTML, CSS, Bootstrap y JavaScript.
- **Lógica de negocio:** vistas, formularios, servicios y validaciones.
- **Persistencia:** modelos Django, migraciones y base de datos PostgreSQL.

Esta organización permite mantener módulos independientes dentro de una misma aplicación web, facilitando pruebas, mantenimiento y evolución del sistema.

La arquitectura definitiva de despliegue centraliza la instalación en un servidor Windows perteneciente a Constructora Centenario. En este servidor se alojan la aplicación Django, PostgreSQL, los archivos estáticos, los archivos cargados, la configuración, los registros del sistema y las copias de seguridad.

Los usuarios no instalan el sistema en sus equipos. El acceso se realiza exclusivamente desde un navegador web mediante una dirección interna de la red corporativa, disponible para los computadores conectados a la misma red LAN o Wi-Fi.

La arquitectura de despliegue centraliza la ejecución en el servidor Windows. Desde allí se administra el servidor de la aplicación, la base de datos PostgreSQL y los archivos requeridos por el sistema.

```text
Servidor Windows
    |
    v
Servidor Django
    |
    v
PostgreSQL
    |
    v
Red corporativa
    |
    v
Usuarios mediante navegador
```

---

## Estructura del Proyecto

```text
PagosFiducia/
|-- config/                 # Configuración principal de Django
|-- core/                   # Vistas generales del sistema
|-- users/                  # Autenticación y modelo de usuario
|-- real_estate/            # Estructura inmobiliaria
|-- fiduciary/              # Módulos fiduciarios e importaciones
|-- templates/              # Plantillas HTML
|-- static/                 # Archivos estáticos
|   |-- assets/             # Imágenes institucionales
|   |-- css/                # Estilos propios
|   |-- js/                 # JavaScript propio
|   `-- vendor/             # Dependencias estáticas locales
|-- tests/                  # Pruebas automatizadas
|-- docs/                   # Documentación técnica del proyecto
|-- manage.py
|-- requirements.txt
|-- pytest.ini
`-- README.md
```

---

## Instalación

La instalación del sistema se realiza únicamente en el servidor Windows designado. Los equipos cliente no requieren instalación local; los usuarios finales solo necesitan un navegador moderno compatible y acceso a la red corporativa.

### 1. Clonar el repositorio

```powershell
git clone <URL_DEL_REPOSITORIO>
cd PagosFiducia
```

### 2. Crear y activar un entorno virtual

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Instalar dependencias

```powershell
pip install -r requirements.txt
```

### 4. Configurar variables de entorno

Crear un archivo `.env` a partir de `.env.example`.

```powershell
copy .env.example .env
```

### 5. Ejecutar migraciones manualmente

```powershell
python manage.py migrate
```

### 6. Crear usuario administrador

```powershell
python manage.py createsuperuser
```

---

## Variables de Entorno

La configuración del sistema debe realizarse mediante variables de entorno.

Variables definidas en `.env.example`:

| Variable | Propósito |
|---|---|
| `DJANGO_SECRET_KEY` | Clave secreta de Django |
| `DJANGO_DEBUG` | Activación del modo de depuración |
| `DJANGO_ALLOWED_HOSTS` | Hosts permitidos por la aplicación |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Orígenes confiables para CSRF |
| `DB_NAME` | Nombre de la base de datos |
| `DB_USER` | Usuario de base de datos |
| `DB_PASSWORD` | Contraseña de base de datos |
| `DB_HOST` | Host de base de datos |
| `DB_PORT` | Puerto de base de datos |
| `DB_CONNECT_TIMEOUT` | Tiempo máximo de conexión |
| `SESSION_COOKIE_SECURE` | Configuración de seguridad para cookie de sesión |
| `CSRF_COOKIE_SECURE` | Configuración de seguridad para cookie CSRF |

No se deben versionar secretos reales en el repositorio.

---

## Ejecución

La ejecución operativa del sistema se realiza en el servidor Windows configurado para alojar la aplicación web.

Los usuarios finales acceden al sistema desde el navegador mediante la URL interna definida por la empresa.

El acceso de usuarios debe realizarse mediante la dirección interna configurada en la red corporativa:

```text
http://<direccion-interna-del-servidor>/
```

---

## Pruebas

El proyecto utiliza `pytest` y `pytest-django` para la ejecución de pruebas automatizadas.

Ejecutar la suite completa:

```powershell
pytest --ds=config.settings
```

Validaciones recomendadas:

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
pytest --ds=config.settings
```

---

## Seguridad

El sistema incorpora mecanismos orientados a proteger el acceso y la trazabilidad:

- Autenticación de usuarios.
- Control de acceso por roles.
- Protección CSRF.
- Manejo de sesiones.
- Auditoría de operaciones.
- Validaciones en servidor.
- Separación de secretos mediante variables de entorno.

---

## Estado del Proyecto

| Campo | Estado |
|---|---|
| Versión | 1.0.0 |
| Estado | Proyecto finalizado |
| Tipo | Aplicación web |

La solución está compuesta por la aplicación web **Gestión Fiduciaria** y los componentes necesarios para su ejecución en el servidor Windows definido para el sistema.

---

## Autor

**Santiago Leyton**  

Estudiante de Ingeniería de Software  
Corporación Universitaria Empresarial Alexander von Humboldt

Proyecto desarrollado durante las prácticas empresariales en Constructora Centenario.

---

## Empresa

**Constructora Centenario**  

líder en construcción de vivienda en Armenia y el Eje Cafetero desde 1984.

Más de 8200 hogares entregados.

---

## Universidad

**Corporación Universitaria Empresarial Alexander von Humboldt**  

Programa de Ingeniería de Software.

Proyecto desarrollado como parte de las prácticas empresariales.

---

## Licencia

Todos los derechos reservados por Constructora Centenario S.A.S.

Uso interno.
