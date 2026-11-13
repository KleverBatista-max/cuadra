"""Facturas por QR de la DGI: cola con reintentos, pegada a un movimiento.

Colección "dgi" (un documento por CUFE, así no hay duplicados):
  {qr_url, mov, estado: QUEUED|OK|FAILED|NEEDS_MANUAL, intentos, proximo (ISO UTC), mensaje, creado}

En el movimiento se guarda lo que ve la página:
  dgi = {estado: "OK"|"ESPERA"|"MANUAL"|"ERROR", cufe, proximo, mensaje}
  y si salió bien: facturaId, facturaTipo, facturaNombre, factura="Recibida", facturaDatos.
El PDF va a "archivos" igual que una subida a mano, y el worker lo sube a Drive (Titular AAAA / Beatriz AAAA).
"""
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import dgi

# Calendario (pedido de Titular, 12 nov 2026): al escanear, luego 24 h, 48 h y 72 h después de cada intento,
# y una última pasada a fin de mes. Si no aparece, queda para subir el PDF a mano.
ESPERAS_H = [24, 48, 72]
ERROR_RETRY_MINUTES = 30   # la DGI no respondió: no gasta intento
PAUSA_S = 1.5              # entre consultas a la DGI
INTENTOS_TOTALES = len(ESPERAS_H) + 2  # escaneo + 3 esperas + fin de mes


def _get(db, coll, id):
    """db.get no trae el id; aquí sí."""
    d = db.get(coll, id) if id else None
    if d is not None:
        d["id"] = id
    return d


def _ahora():
    return datetime.now(timezone.utc)


def _iso(d):
    return d.isoformat(timespec="seconds")


def encolar(db, qr_url, mov_id, persona="Titular"):
    """Guarda el QR para un movimiento. Devuelve el documento de la cola (con 'id' = CUFE)."""
    p = dgi.parse_qr_url(qr_url)
    if not p:
        raise ValueError("Ese QR no es de una factura de la DGI.")
    cufe = p["cufe"]
    f = _get(db, "dgi", cufe)
    if f:
        if f.get("estado") == "OK" and (not mov_id or (f.get("mov") and f["mov"] != mov_id)):
            raise ValueError("Esa factura ya está guardada" + (" en otro movimiento." if mov_id else "."))
        db.merge("dgi", cufe, {"mov": mov_id or f.get("mov"), "qr_url": f.get("qr_url") or p["url"]})
        if f.get("estado") in ("FAILED", "NEEDS_MANUAL"):  # escanearla otra vez = volver a intentar
            db.merge("dgi", cufe, {"estado": "QUEUED", "intentos": 0, "proximo": _iso(_ahora())})
        return _get(db, "dgi", cufe)
    db.set("dgi", cufe, {"qr_url": p["url"], "mov": mov_id, "persona": persona, "estado": "QUEUED", "intentos": 0,
                         "proximo": _iso(_ahora()), "mensaje": "", "creado": _iso(_ahora())})
    return _get(db, "dgi", cufe)


def fin_de_mes(ahora):
    """Último día del mes a las 8 p. m. de Panamá (01:00 UTC del día 1). Si falta menos de 12 h, el del mes siguiente."""
    y, m = ahora.year, ahora.month
    for _ in range(2):
        y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
        t = datetime(y2, m2, 1, 1, 0, tzinfo=timezone.utc)
        if t - ahora >= timedelta(hours=12):
            return t
        y, m = y2, m2
    return t


def proximo_intento(intentos, ahora):
    """intentos = cuántos se han hecho. None = ya no hay más."""
    if intentos <= len(ESPERAS_H):
        return ahora + timedelta(hours=ESPERAS_H[intentos - 1])
    if intentos == len(ESPERAS_H) + 1:
        return fin_de_mes(ahora)
    return None


def _fecha_iso(f):
    try:
        return datetime.strptime(f, "%d/%m/%Y %H:%M:%S").strftime("%Y-%m-%d")
    except Exception:
        return ""


def _guardar_pdf(cfg, db, f, res, log):
    mov = _get(db, "movs", f["mov"]) if f.get("mov") else None
    fid = "f-" + uuid.uuid4().hex[:16]
    local = os.path.join(cfg.pendientes, fid + ".pdf")
    with open(local, "wb") as fh:
        fh.write(res.pdf_bytes)
    nombre = res.pdf_filename or (fid + ".pdf")
    db.set("archivos", fid, {"nombre": nombre, "tipo": "application/pdf", "local": local,
                             "persona": (mov or {}).get("quien") or f.get("persona") or "Titular", "creado": time.time(),
                             "subido": False, "cufe": f["id"]})
    d = res.data
    datos = {"emisor": d.get("emisor_nombre") or "", "ruc": d.get("emisor_ruc") or "", "numero": d.get("numero") or "",
             "fecha": _fecha_iso(d.get("fecha_emision") or ""), "total": d.get("total"), "itbms": d.get("itbms"),
             "cufe": f["id"]}
    if mov:
        patch = {"facturaId": fid, "facturaTipo": "application/pdf", "facturaNombre": nombre,
                 "factura": "Recibida", "facturaDatos": datos,
                 "dgi": {"estado": "OK", "cufe": f["id"], "mensaje": "Factura de la DGI guardada"}}
        if datos["total"] is not None and abs(float(datos["total"]) - float(mov.get("monto") or 0)) > 0.005:
            patch["notas"] = ((mov.get("notas") or "") + f" La factura dice ${float(datos['total']):.2f}.").strip()
        db.merge("movs", mov["id"], patch)
        log.append(f"Factura DGI guardada: {datos['emisor']} ${datos['total']} -> {mov.get('comercio')}")
    else:  # QR escaneado sin movimiento: la factura espera su transacción (sueltas.py)
        from . import sueltas
        db.merge("archivos", fid, {"datos": datos, "estado": "espera", "esperaDesde": time.time()})
        a = _get(db, "archivos", fid)
        pegada = sueltas.intentar(db, a, log)
        if pegada:
            db.merge("dgi", f["id"], {"mov": pegada["id"]})
    return fid, datos


def procesar(cfg, db, cufe, log=None):
    """Un intento. Devuelve el documento de la cola actualizado."""
    log = log if log is not None else []
    f = _get(db, "dgi", cufe)
    if not f or f.get("estado") in ("OK", "FAILED"):
        return f
    res = dgi.fetch_invoice(qr_url=f.get("qr_url")) if f.get("qr_url") else dgi.fetch_invoice(cufe=cufe)
    ahora = _ahora()
    intentos = int(f.get("intentos") or 0) + (0 if res.status == dgi.ERROR else 1)
    upd = {"intentos": intentos, "mensaje": res.message, "ultimo": _iso(ahora)}
    estado_mov = None
    if res.status == dgi.OK:
        fid, datos = _guardar_pdf(cfg, db, f, res, log)
        upd.update(estado="OK", proximo=None, archivo=fid, datos=datos)
    elif res.status == dgi.PENDING:
        prox = proximo_intento(intentos, ahora)
        if prox is None:
            upd.update(estado="NEEDS_MANUAL", proximo=None,
                       mensaje=f"No apareció en la DGI después de {intentos} intentos")
            estado_mov = {"estado": "MANUAL", "cufe": cufe, "mensaje": "La DGI no la publicó: sube el PDF o la foto"}
            from .reglas import avisar
            mov = _get(db, "movs", f.get("mov")) if f.get("mov") else None
            avisar(db, f"La DGI no publicó la factura de {(mov or {}).get('comercio', 'un movimiento')} "
                       f"({(mov or {}).get('fecha', '')}) después de {intentos} intentos. Súbela con foto o PDF.",
                   clave="dgi-manual-" + cufe[-24:])
        else:
            upd.update(estado="QUEUED", proximo=_iso(prox))
            estado_mov = {"estado": "ESPERA", "cufe": cufe, "proximo": _iso(prox),
                          "mensaje": "La DGI aún no la publica; se reintenta sola"}
    elif res.status == dgi.ERROR:
        prox = ahora + timedelta(minutes=ERROR_RETRY_MINUTES)
        upd.update(estado="QUEUED", proximo=_iso(prox), errores=int(f.get("errores") or 0) + 1)
        estado_mov = {"estado": "ESPERA", "cufe": cufe, "proximo": _iso(prox),
                      "mensaje": "La DGI no respondió; se reintenta sola"}
    else:  # INVALID
        upd.update(estado="FAILED", proximo=None)
        estado_mov = {"estado": "ERROR", "cufe": cufe, "mensaje": res.message}
    db.merge("dgi", cufe, upd)
    if estado_mov and f.get("mov") and db.exists("movs", f["mov"]):
        db.merge("movs", f["mov"], {"dgi": estado_mov})
    log.append(f"DGI {cufe[-10:]}: {upd.get('estado')} ({res.message})")
    return _get(db, "dgi", cufe)


def procesar_vencidos(cfg, db, log):
    """Lo llama el worker (cada 5 min): reintenta lo que ya venció."""
    ahora = _iso(_ahora())
    pendientes = [f for f in db.all("dgi") if f.get("estado") == "QUEUED" and (f.get("proximo") or "") <= ahora]
    for i, f in enumerate(pendientes):
        if i:
            time.sleep(PAUSA_S)
        procesar(cfg, db, f["id"], log)
    return len(pendientes)
