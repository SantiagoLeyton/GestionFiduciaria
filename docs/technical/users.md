# Usuarios y acceso funcional

## Alcance

La pantalla funcional de usuarios es exclusivamente de consulta.

No permite crear, editar, cambiar roles, activar, inactivar ni eliminar usuarios desde el panel empresarial. Las rutas historicas de gestion permanecen bloqueadas en servidor para evitar modificaciones por URL directa o solicitudes POST manipuladas.

La administracion tecnica de cuentas puede realizarse mediante Django Admin o Django Shell. No existen endpoints funcionales, API ni comandos de carga inicial para crear usuarios.

## Rol visible

El valor interno `accounting_admin` se conserva para no afectar permisos ni registros existentes.

La interfaz muestra este rol como `Contabilidad`.

## Username

`username` se conserva como dato tecnico interno del modelo actual.

La interfaz funcional:

- no lo muestra en la tabla de usuarios;
- no lo expone en formularios funcionales;
- no permite editarlo;
- permite autenticacion por correo y contrasena.

## Ultimo acceso

La tabla muestra `last_login` como `Ultimo acceso`.

- Si existe valor, se presenta fecha y hora local.
- Si es NULL, se muestra `Nunca`.

El inicio de sesion usa el mecanismo estandar de Django, por lo que actualiza `last_login`.

## Busqueda

El parametro de busqueda se normaliza con `strip()`. Una busqueda compuesta solo por espacios se trata como vacia y muestra el listado normal.
