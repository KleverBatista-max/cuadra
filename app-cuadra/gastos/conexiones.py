"""Conexiones (12 nov 2026): claves que Titular pone desde la app (Más → Conexiones), guardadas solo en el VPS.

Archivo: <datos>/secretos.json (permiso 600). Nunca se devuelven las claves a la página.
iCloud: se prueba por IMAP (imap.mail.me.com) con contraseña de aplicación de Apple y se mira si hay carpeta de notas
y si aparece la nota "Cuentas x Cobrar". Solo lectura.
"""
import imaplib
import json
import os
import re
import time
from email import message_from_bytes
from email.header import decode_header, make_header

def _ruta(cfg):
    return os.path.join(cfg.datos, "secretos.json")


def leer(cfg):
    try:
        with open(_ruta(cfg)) as f:
            return json.load(f)
    except Exception:
        return {}


def guardar(cfg, servicio, datos):
    s = leer(cfg)
    s[servicio] = {**(s.get(servicio) or {}), **datos}
    p = _ruta(cfg)
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(s, f)
    os.replace(p + ".tmp", p)


def resumen(cfg):
    """Lo que la página puede ver: sin claves."""
    out = {}
    for k, v in leer(cfg).items():
        out[k] = {"usuario": v.get("usuario"), "guardada": v.get("guardada"), "prueba": v.get("prueba")}
    out["icloud_web_estado"] = estado_icloud(cfg)
    return out


def _carpetas(m):
    typ, data = m.list()
    out = []
    for l in data or []:
        l = l.decode("utf-8", "ignore")
        mm = re.search(r'"([^"]+)"\s*$', l) or re.search(r"\s(\S+)$", l)
        if mm:
            out.append(mm.group(1))
    return out


def probar_icloud(usuario, clave):
    usuario, clave = (usuario or "").strip(), re.sub(r"\s", "", clave or "")
    m = None
    for u in [usuario] + ([usuario.split("@")[0]] if "@" in usuario else []):
        try:
            m = imaplib.IMAP4_SSL("imap.mail.me.com", 993, timeout=25)
            m.login(u, clave)
            break
        except imaplib.IMAP4.error:
            m = None
        except Exception as e:
            return {"ok": False, "mensaje": f"No pude conectar con iCloud ({type(e).__name__})."}
    if m is None:
        return {"ok": False, "mensaje": "iCloud rechazó el correo o la contraseña de aplicación. Revisa que sea una contraseña de aplicación (xxxx-xxxx-xxxx-xxxx) creada en account.apple.com."}
    try:
        carpetas = _carpetas(m)
        nc = [c for c in carpetas if c.lower() in ("notes", "notas")]
        res = {"ok": True, "carpetas": carpetas[:30], "carpeta_notas": nc[0] if nc else None, "notas": 0, "titulos": [], "nota_cobrar": False}
        if nc:
            m.select(f'"{nc[0]}"', readonly=True)
            typ, d = m.uid("SEARCH", None, "ALL")
            uids = (d[0] or b"").split()
            res["notas"] = len(uids)
            for u in uids[-30:]:
                typ, h = m.uid("FETCH", u, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
                if h and isinstance(h[0], tuple):
                    t = str(make_header(decode_header(message_from_bytes(h[0][1]).get("Subject", ""))))
                    res["titulos"].append(t[:40])
                    if "cobrar" in t.lower():
                        res["nota_cobrar"] = True
        if res["nota_cobrar"]:
            res["mensaje"] = "Conectado. La nota Cuentas x Cobrar SÍ se ve por la contraseña de aplicación."
        elif nc:
            res["mensaje"] = f"Conectado. Hay carpeta de notas con {res['notas']} nota(s), pero no veo la de Cuentas x Cobrar (las notas compartidas o actualizadas de iCloud no salen por aquí)."
        else:
            res["mensaje"] = "Conectado al correo de iCloud, pero esa cuenta no expone notas por IMAP (las notas de iCloud actuales no salen con contraseña de aplicación)."
        return res
    finally:
        try:
            m.logout()
        except Exception:
            pass


def estado_icloud(cfg):
    try:
        with open(os.path.join(cfg.datos, "icloud-estado.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def pedir_ahora(cfg):
    """Avisa al lector de iCloud (root, cron cada minuto) que corra ya."""
    with open(os.path.join(cfg.datos, "entrada", "icloud-ahora"), "w") as f:
        f.write(str(time.time()))
