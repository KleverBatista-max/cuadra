# Cuadra

App personal de finanzas que **se cuadra sola** contra los bancos panameños: lee los avisos del correo (Yappy, BAC,
Banesco, Global, Banco General), la banca en línea (lector de bancos aparte),
y muestra tarjetas, cuentas, movimientos, cuentas por cobrar y facturas (DGI).

- **Empieza por [`CLAUDE.md`](CLAUDE.md)** (reglas, pruebas, cómo cambiar algo) y **[`MANUAL.md`](MANUAL.md)** (todo el sistema).
- `GUIA.md` es histórica: la instalación original pensada para una Raspberry. Hoy corre en un VPS.
- Pruebas: `sudo -u gastos -H venv/bin/python -m pytest` (el commit no pasa si fallan).
