# Seguridad

## Principios

- **Solo lectura.** Cuadra no está hecha para mover dinero. El lector de bancos solo abre pantallas de consulta; las pocas acciones que realiza en cada banco (por ejemplo, abrir la lista de compras en tránsito) están escritas una por una en el código y se rechazan si el elemento no es único.
- **Mínimos permisos.** El acceso al correo es por IMAP con contraseña de aplicación; el respaldo usa una carpeta propia de Google Drive.
- **Claves fuera del código.** `credenciales.env.nosync`, `config.toml` y `secretos.json` viven con permiso `600` y están en `.gitignore`.

## Reportar un problema

Si encuentras una vulnerabilidad, abre un *issue* sin detalles explotables o usa el reporte privado de GitHub (**Security → Report a vulnerability**).
