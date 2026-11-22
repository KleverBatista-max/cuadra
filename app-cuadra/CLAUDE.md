# Cuadra (app) — instrucciones para Claude o cualquier persona

Esto es el código de la app **Cuadra** de Titular Demo (gastos, tarjetas, cuentas por cobrar, facturas). Corre en un VPS
(Vultr, Ubuntu 24.04) en `/home/gastos/gastos-app`, usuario linux `gastos`. **Lee primero `MANUAL.md`** (copia del manual
del servidor, `/root/MANUAL.md`): sección 0 (resumen), 4.5 (reglas), 4.5b (conciliación con bancos), 4.8 (pruebas) y 11
(reglas para cualquier IA). Habla con Titular en español, simple y directo.

## Lista de cierre: TODO cambio termina así (pedido de Titular, 22 nov 2026)

1. Pruebas verdes (`venv/bin/python -m pytest`) y commit + `git push` de este repo (privado `cuadra-app`).
2. Documentado: `/root/MANUAL.md` (sección 4 y 10) y luego `manual-sync`.
3. **Listo para respaldo y mudanza:** cualquier archivo, servicio, programa (apt/pip/node) o ruta de Tailscale NUEVO debe quedar en
   `/opt/respaldo/respaldo.sh`, `/opt/bancos/restaurar.sh` e `instalar.sh` (+ `deploy/`). Si no, la mudanza lo pierde.
4. Versión pública: correr `/opt/bancos/publico/publicar.sh` (y subir `/opt/bancos` a GitHub) y revisar que diga «publicado» o «sin cambios».
5. Un solo lugar para cada regla de cálculo (saldos/Neto: `gastos/static/calculo.js`); no copiar fórmulas.

## Reglas que no se rompen

- **Manda la banca en línea.** El correo es un aviso provisional; lo que lee el lector de bancos lo corrige (MANUAL 4.5b).
- **Una factura nunca crea una transacción.** El efectivo no se sigue.
- **Cuentas por cobrar = dos casillas:** la nota del iPhone (la app nunca la toca sola) + movimientos marcados (`mov.cxc`).
- **Nada de pagar/transferir en los bancos**: el lector solo lee (MANUAL 11.1).
- No leer ni imprimir `config.toml`, `secretos.json`, `rclone.conf` ni credenciales.
- El repo **público** `cuadra` solo se actualiza con `/opt/bancos/publico/publicar.sh` (copia saneada). Nunca subir ahí ramas.

## Cuando Titular pide algo sobre sus DATOS («de los $30, $15 son de Beatriz», «Ana ya pagó», «ese no cuenta»)

**No es un cambio de código y no se inventan reglas.** La estructura de la app ya alcanza: se juega con lo que existe
(movimientos, la marca `cxc`, notas). Usa **`gastos asistente`** (comandos fijos, probados, con copia, bitácora y deshacer):

```bash
cd /home/gastos/gastos-app && sudo -u gastos -H venv/bin/python -m gastos asistente --help
```

1. `buscar` para sacar el id del movimiento (texto, `--fecha`, `--monto`). 2. `debe PERSONA MONTO "motivo" --mov ID`
(deuda suelta), `dividir MOV "Beatriz, Laura" [--montos "Beatriz=15"]` (cuando la cuenta se DIVIDIÓ entre varias personas: usa la misma función «Dividir» de la página; `ninguna` la quita), `abono` (ya pagó) o `marcar MOV PERSONA|ninguna`. 3. `cuenta PERSONA` para comprobar el resultado y decírselo a
Titular. 4. Si se equivocó o se arrepiente: `deshacer ID_BITACORA`.

- Si falta un dato (¿me deben o ya pagó? ¿a quién?), **pregunta**; no adivines.
- No edites la base ni `/api/d/...` a pulso y no excluyas movimientos propios (decisión de Titular, 18 nov).
- Desde el teléfono Titular puede usar los mismos comandos por el conector `cuadra-mcp` (chat de claude.ai); es la misma capa:
  mismas reglas, misma bitácora. Código en `gastos/mcp_servidor.py`; si agregas un comando al asistente, agrégalo ahí también.
- Si lo que pide no cabe en estos comandos, primero entiende y explica; solo si Titular lo autoriza se agrega un comando nuevo
  (con su prueba en `tests/test_asistente.py`). Detalle: MANUAL sección 10, «22 nov 2026 (noche)».

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
