"""Nota "Cuentas x Cobrar" leída directo de la carpeta "Notes" de Gmail (12 nov 2026).

El iPhone sincroniza las notas de la cuenta de Gmail en la carpeta IMAP "Notes". Se lee con la misma
contraseña de aplicación de Google de la app: sin iCloud, sin códigos, sin correo del Atajo.
La nota se aplica SOLO cuando su contenido cambia (huella guardada en sistema/nota_notes), para no borrar
lo que la app suma sola entre una edición y otra. Nunca se borra ni se modifica nada en esa carpeta.
"""
import hashlib

from . import reglas
from .correo import _a_correo
from .util import norm

CARPETA = "Notes"
TITULO = "cuentas x cobrar"


def revisar(db, gmail, log):
    imap = gmail.imap
    typ, _ = imap.select(f'"{CARPETA}"', readonly=True)
    if typ != "OK":
        return False
    typ, d = imap.uid("SEARCH", None, "ALL")
    mejor = None
    for uid in (d[0] or b"").split():
        typ, r = imap.uid("FETCH", uid, "(BODY.PEEK[])")
        if not r or not isinstance(r[0], tuple):
            continue
        c = _a_correo("nota-" + uid.decode(), r[0][1])
        if TITULO in norm(c.asunto or "") and (mejor is None or c.fecha > mejor.fecha):
            mejor = c
    if mejor is None:
        return False
    texto = mejor.cuerpo or ""
    huella = hashlib.sha1(texto.encode("utf-8", "ignore")).hexdigest()
    est = db.get("sistema", "nota_notes") or {}
    if est.get("huella") == huella:
        return False
    if reglas.aplicar_nota(db, texto, mejor.local.strftime("%Y-%m-%d"), log):
        db.set("sistema", "nota_notes", {"huella": huella, "aplicada": mejor.local.strftime("%Y-%m-%dT%H:%M")})
        log.append("Nota Cuentas x Cobrar (carpeta Notes de Gmail) aplicada")
        return True
    return False


def revisar_archivo(cfg, db, log):
    """Nota de iCloud que deja el lector (root) en entrada/nota-icloud.txt: se aplica solo si cambió."""
    import os
    from .util import ahora_panama
    f = os.path.join(cfg.datos, "entrada", "nota-icloud.txt")
    if not os.path.exists(f):
        return False
    texto = open(f, encoding="utf-8", errors="ignore").read()
    huella = hashlib.sha1(texto.encode("utf-8", "ignore")).hexdigest()
    est = db.get("sistema", "nota_icloud") or {}
    if est.get("huella") == huella:
        return False
    hoy = ahora_panama().strftime("%Y-%m-%d")
    if reglas.aplicar_nota(db, texto, hoy, log):
        db.set("sistema", "nota_icloud", {"huella": huella, "aplicada": ahora_panama().strftime("%Y-%m-%dT%H:%M")})
        log.append("Nota Cuentas x Cobrar (iCloud) aplicada")
        return True
    return False
