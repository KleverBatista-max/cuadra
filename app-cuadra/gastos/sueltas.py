"""Facturas subidas antes de que llegue su transacción (12 nov 2026, pedido de Titular).

Una factura "suelta" es un documento de "archivos" con estado "espera" y datos leídos (total, fecha, emisor).
El worker (cada 5 min) y los endpoints llaman a revisar():
  1. Pega cada factura suelta a un movimiento con el MISMO monto y fecha a ±3 días
     (sin factura todavía; Beatriz/Ana solo en sus propios movimientos). El nombre solo desempata.
     Si hay varias opciones y el nombre no decide, avisa en "Por revisar" y no adivina.
  2. Si no aparece ninguna transacción NO se crea nada (el efectivo no se sigue: solo bancos, tarjetas y deudas).
     La factura igual se guarda: a las 24 h el worker la sube a Drive con fecha_comercio_monto (worker.subir_facturas),
     y se sigue intentando pegar hasta 7 días por si la transacción llega tarde.
"""
import re
import time
from datetime import datetime, timedelta, timezone

from .reglas import avisar
from .util import norm

ESPERA_H = 24          # a partir de aquí la factura sube a Drive aunque no tenga transacción
BUSCAR_DIAS = 7        # se sigue buscando su transacción hasta 7 días
VENTANA_DIAS = 3
ENTRANTES = ("Yappy recibido", "ACH recibido")
PANAMA = timezone(timedelta(hours=-5))


def _dia(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _fecha_factura(a):
    d = _dia((a.get("datos") or {}).get("fecha"))
    if d:
        return d
    return datetime.fromtimestamp(a.get("creado") or time.time(), PANAMA).date()


def _palabras(s):
    return {w for w in re.findall(r"[a-z0-9]{3,}", norm(s or "")) if w not in ("los", "las", "del", "s.a", "inc", "corp")}


def _parecido(emisor, comercio):
    return len(_palabras(emisor) & _palabras(comercio))


def _candidatos(movs, total, dia, persona, excluir=None):
    out = []
    for m in movs:
        if m["id"] == excluir or m.get("facturaId") or m.get("factura") != "Pendiente" or m.get("tipo") in ENTRANTES:
            continue
        if persona in ("Beatriz", "Ana") and m.get("quien") != persona:
            continue
        try:
            if abs(float(m.get("monto") or 0) - float(total)) > 0.005:
                continue
        except (TypeError, ValueError):
            continue
        dm = _dia(m.get("fecha"))
        if not dm or abs((dm - dia).days) > VENTANA_DIAS:
            continue
        out.append(m)
    return out


def _elegir(cands, emisor):
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        puntos = sorted(((_parecido(emisor, c.get("comercio")), c) for c in cands), key=lambda x: -x[0])
        if puntos[0][0] > 0 and puntos[0][0] > puntos[1][0]:
            return puntos[0][1]
    return None


def _campos_factura(a):
    return {"facturaId": a["id"], "facturaTipo": a.get("tipo") or "", "facturaNombre": a.get("nombre") or "",
            "factura": "Recibida", "facturaDatos": a.get("datos") or {}}


def pegar(db, a, mov, log):
    db.merge("movs", mov["id"], _campos_factura(a))
    db.merge("archivos", a["id"], {"estado": "pegada", "mov": mov["id"]})
    log.append(f"Factura {(a.get('datos') or {}).get('emisor') or a.get('nombre')} pegada a {mov.get('comercio')} {mov.get('fecha')}")
    return mov


def intentar(db, a, log, movs=None):
    """Intenta pegar una factura suelta. Devuelve el movimiento o None."""
    datos = a.get("datos") or {}
    if datos.get("total") is None:
        return None
    movs = movs if movs is not None else db.all("movs")
    cands = _candidatos(movs, datos["total"], _fecha_factura(a), a.get("persona"))
    mov = _elegir(cands, datos.get("emisor"))
    if mov:
        return pegar(db, a, mov, log)
    if len(cands) > 1:
        avisar(db, f"La factura de {datos.get('emisor') or 'un comercio'} por ${float(datos['total']):.2f} coincide con "
                   f"{len(cands)} movimientos ({', '.join(c.get('comercio') or '' for c in cands[:3])}). Ábrela y pégala al correcto.",
               clave="suelta-varias-" + a["id"])
    return None


def revisar(db, log):
    """Lo llama el worker cada 5 min (y los endpoints al recibir una factura)."""
    movs = db.all("movs")
    ahora = time.time()
    for a in db.all("archivos"):
        if a.get("estado") != "espera":
            continue
        if intentar(db, a, log, movs):
            movs = db.all("movs")
        elif ahora - float(a.get("esperaDesde") or a.get("creado") or ahora) >= BUSCAR_DIAS * 86400:
            db.merge("archivos", a["id"], {"estado": "sin-transaccion"})  # queda guardada (en Drive) sin movimiento
