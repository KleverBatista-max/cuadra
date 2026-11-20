# Cuadra (app) — instrucciones para Claude o cualquier persona

Esto es el código de la app **Cuadra** de Titular Demo (gastos, tarjetas, cuentas por cobrar, facturas). Corre en un VPS
(Vultr, Ubuntu 24.04) en `/home/gastos/gastos-app`, usuario linux `gastos`. **Lee primero `MANUAL.md`** (copia del manual
del servidor, `/root/MANUAL.md`): sección 0 (resumen), 4.5 (reglas), 4.5b (conciliación con bancos), 4.8 (pruebas) y 11
(reglas para cualquier IA). Habla con Titular en español, simple y directo.

## Reglas que no se rompen

- **Manda la banca en línea.** El correo es un aviso provisional; lo que lee el lector de bancos lo corrige (MANUAL 4.5b).
- **Una factura nunca crea una transacción.** El efectivo no se sigue.
- **Cuentas por cobrar = dos casillas:** la nota del iPhone (la app nunca la toca sola) + movimientos marcados (`mov.cxc`).
- **Nada de pagar/transferir en los bancos**: el lector solo lee (MANUAL 11.1).
- No leer ni imprimir `config.toml`, `secretos.json`, `rclone.conf` ni credenciales.
- El repo **público** `cuadra` solo se actualiza con `/opt/bancos/publico/publicar.sh` (copia saneada). Nunca subir ahí ramas.

## Pruebas (obligatorio)

```bash
cd /home/gastos/gastos-app && sudo -u gastos -H venv/bin/python -m pytest
```

- 85+ pruebas en `tests/`; `tests/test_conciliacion.py` tiene los casos reales (Yappy, flotantes, Global, cxc, QR, DGI).
- El commit **no pasa** si una prueba falla (`.githooks/pre-commit`). Si una regla cambia a propósito, se cambia su prueba.
- Las pruebas nunca tocan el Drive real (`tests/conftest.py`).

## Cómo cambiar algo

1. `git status` limpio. 2. Cambiar el código + agregar el caso a `tests/`. 3. `sudo -u gastos -H git commit` (corre las
pruebas) y `git push`. 4. `systemctl restart gastos-web` si cambió la página o `web.py` (el worker, cada 5 min, toma el
código nuevo solo). 5. Anotar el cambio en `/root/MANUAL.md` (sección 4 y 10) y correr `manual-sync`.
Los archivos deben quedar del usuario `gastos`.

## Mapa del código (`gastos/`)

| Archivo | Qué hace |
|---|---|
| `worker.py` | vuelta cada 5 min: correo, JSON de bancos (`entrada/`), nota de iCloud, DGI, facturas, respaldo, salud |
| `parsers.py` | lee los correos (Yappy, BAC, Banesco, Global, BG, +Móvil, facturas) |
| `reglas.py` | reglas de negocio: aplicar movimientos, conciliar con bancos, flotantes, cuadres, cxc, +Móvil |
| `config.py` | tarjetas, adicionales, personas, interruptores (`CXC_AUTOMATICA`, `CXC_MOVS_DESDE`) |
| `web.py` + `static/app.html` | la página (FastAPI + una sola página) |
| `dgi.py`, `facturas_dgi.py`, `qr_foto.py` | facturas por QR de la DGI (con reintentos) y lector de fotos |
| `sueltas.py` | facturas que llegan antes que su transacción |
| `salud.py` | la app se vigila sola y avisa en "Por revisar" |
| `db.py` | SQLite con documentos (colecciones `movs`, `saldos`, `cxc`, `avisos`, ...) |

El lector de bancos está en otro repo (`cuadra-bancos`, `/opt/bancos`): corre todos los días 10:00 y 18:30 y deja el JSON
en `gastos-datos/entrada/`.
