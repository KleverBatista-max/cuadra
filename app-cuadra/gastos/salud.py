"""La app se vigila sola (14 nov 2026, pedido de Titular: "que se haga su debugging sola").

El worker llama revisar() al final de cada vuelta (cada 5 min). Si algo se rompe, avisa UNA vez en "Por revisar"
(los avisos tienen clave, no se repiten). No arregla nada por su cuenta: dice qué pasó y dónde mirar.
"""
import shutil
from datetime import datetime, timedelta, timezone

from . import config as C
from .reglas import avisar
from .util import ahora_panama

BANCOS = {"global-": "Global", "bac-": "BAC", "bg-": "Banco General", "banesco-": "Banesco"}
HORAS_BANCO = 20          # se corre a las 10:00 y 18:30: más de 20 h sin leer = fallaron dos corridas seguidas
VUELTAS_FALLA = 3         # un paso que falla 3 vueltas seguidas (15 min) avisa
PASOS = ("Nota en Notes", "Nota de iCloud", "DGI", "Facturas sueltas")
DISCO_MAX = 0.85


def _fecha(s):
    try:
        return datetime.strptime(str(s)[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def bancos_frescos(db, ahora):
    ultimo = {}
    for s in db.all("saldos"):
        if s["id"] in C.SALDOS_INTOCABLES:
            continue
        banco = next((n for p, n in BANCOS.items() if s["id"].startswith(p)), None)
        f = _fecha(s.get("desde"))
        if banco and f and (banco not in ultimo or f > ultimo[banco]):
            ultimo[banco] = f
    for banco, f in ultimo.items():
        if ahora - f > timedelta(hours=HORAS_BANCO):
            avisar(db, f"No se ha podido leer {banco} desde el {f:%d/%m %H:%M} (fallaron las últimas corridas). "
                       f"Mira /root/bancos_out/cron.log en el servidor.", clave=f"salud-banco-{banco}-{f:%Y%m%d%H%M}")


def pasos(db, estado, log):
    """Cuenta vueltas seguidas con error por paso (y la vuelta completa caída)."""
    s = db.get("sistema", "salud") or {}
    cuenta = dict(s.get("fallas") or {})
    errores = {}
    for l in log:
        for p in PASOS:
            if l.startswith(p) and ("error" in l.lower() or "no se pudo" in l.lower()):
                errores[p] = l
    if not estado.get("ok"):
        errores["Vuelta completa"] = estado.get("error") or "error desconocido"
    for p in list(PASOS) + ["Vuelta completa"]:
        if p in errores:
            cuenta[p] = cuenta.get(p, 0) + 1
            if cuenta[p] == VUELTAS_FALLA:
                avisar(db, f"«{p}» falla desde hace {VUELTAS_FALLA} vueltas seguidas: {errores[p][:220]}",
                       clave=f"salud-paso-{p}-{ahora_panama():%Y%m%d%H%M}")
        else:
            cuenta[p] = 0
    db.set("sistema", "salud", {"fallas": cuenta, "revisado": ahora_panama().strftime("%Y-%m-%dT%H:%M")})


def dgi_trabada(db):
    ahora = datetime.now(timezone.utc)
    for f in db.all("dgi"):
        if f.get("estado") != "QUEUED" or not f.get("proximo"):
            continue
        try:
            prox = datetime.fromisoformat(f["proximo"])
        except ValueError:
            continue
        if ahora - prox > timedelta(days=1):
            avisar(db, f"Una factura de la DGI lleva más de un día esperando su reintento (CUFE …{f['id'][-10:]}). "
                       f"Revisa el worker.", clave=f"salud-dgi-{f['id']}-{prox:%Y%m%d}")


def respaldo(db, ahora):
    f = (db.get("sistema", "respaldo") or {}).get("fecha")
    if f and (ahora.date() - datetime.strptime(f, "%Y-%m-%d").date()).days >= 2:
        avisar(db, f"El respaldo diario en Google Drive no se hace desde el {f}.", clave=f"salud-respaldo-{f}")


def disco(db, ruta):
    u = shutil.disk_usage(ruta)
    uso = u.used / u.total
    if uso > DISCO_MAX:
        avisar(db, f"El disco del servidor está al {uso:.0%}. Hay que liberar espacio.",
               clave=f"salud-disco-{ahora_panama():%Y%m%d}")


def tipos_raros(db):
    """14 nov: el Uber de $1.89 de la Banesco 1107 quedó como "Yappy recibido"; así nunca se enlaza con el banco
    (se duplica y luego "cae"). Un Yappy solo puede ser de la cuenta Yappy (medio "Yappy" o "BG 1111"), y el medio
    "Yappy" solo lleva tipos Yappy. La BG 1111 también hace transferencias (ACH enviado): eso es normal."""
    for m in db.all("movs"):
        yappy_tipo = str(m.get("tipo") or "").startswith("Yappy")
        if (yappy_tipo and m.get("medio") not in ("Yappy", "BG 1111")) or (m.get("medio") == "Yappy" and not yappy_tipo):
            avisar(db, f"{m.get('comercio')} ${float(m.get('monto') or 0):.2f} del {m.get('fecha')} está como «{m.get('tipo')}» "
                       f"pero el medio es {m.get('medio')}. Así no se cruza con el banco: corrige el tipo en Movimientos.",
                   clave=f"salud-tipo-{m['id']}-{m.get('tipo')}")


def revisar(cfg, db, estado, log):
    ahora = ahora_panama().replace(tzinfo=None)
    for nombre, fn in (("bancos", lambda: bancos_frescos(db, ahora)), ("pasos", lambda: pasos(db, estado, log)),
                       ("dgi", lambda: dgi_trabada(db)), ("respaldo", lambda: respaldo(db, ahora)),
                       ("disco", lambda: disco(db, cfg.datos)), ("tipos", lambda: tipos_raros(db))):
        try:
            fn()
        except Exception as e:  # la vigilancia nunca tumba la vuelta
            log.append(f"Salud ({nombre}): {type(e).__name__}: {e}")
