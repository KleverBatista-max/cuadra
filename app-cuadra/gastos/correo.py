"""Lectura de Gmail por IMAP (solo lectura; no marca como leído)."""
import email
import imaplib
import re
from email.policy import default as POLICY
from email.utils import parsedate_to_datetime
from datetime import timezone

from .parsers import Correo

REMITENTES_BANCO = [
    "notificaciones@yappy.com.pa", "notificacion_pa@pa.bac.net", "Informacion_Banesco_PA@banesco.com.pa",
    "contactenos@globalbank.com.pa", "mensajesyalertas@bgeneral.com", "alerta@baccredomatic.com",
    "facturacion@cwpanama.com",
]


def _carpeta(imap, bandera):
    typ, data = imap.list()
    for linea in data or []:
        l = linea.decode("utf-8", "ignore")
        if bandera in l:
            m = re.search(r'"([^"]+)"\s*$', l) or re.search(r"\s(\S+)$", l)
            if m:
                return m.group(1)
    return None


def _a_correo(gid, raw):
    msg = email.message_from_bytes(raw, policy=POLICY)
    remitente = email.utils.parseaddr(msg.get("From", ""))[1]
    try:
        fecha = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc)
    except Exception:
        from datetime import datetime
        fecha = datetime.now(timezone.utc)
    texto = html = ""
    adjuntos = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        nombre = part.get_filename()
        try:
            if nombre:
                adjuntos.append((nombre, ctype, part.get_payload(decode=True) or b""))
            elif ctype == "text/plain" and not texto:
                texto = part.get_content()
            elif ctype == "text/html" and not html:
                html = part.get_content()
        except Exception:
            pass
    auth = " ".join(str(h) for h in (msg.get_all("Authentication-Results") or [])).lower()
    autenticado = (not auth) or "dkim=pass" in auth or "spf=pass" in auth
    return Correo(msgid=gid, remitente=remitente, asunto=str(msg.get("Subject", "")), fecha=fecha,
                  texto=texto, html=html, adjuntos=adjuntos, autenticado=autenticado)


class Gmail:
    def __init__(self, usuario, clave):
        self.imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        self.imap.login(usuario, clave)
        self.todos = _carpeta(self.imap, "\\All") or "[Gmail]/All Mail"
        self.papelera = _carpeta(self.imap, "\\Trash") or "[Gmail]/Trash"

    def _buscar(self, carpeta, consulta, ya_visto):
        self.imap.select(f'"{carpeta}"', readonly=True)
        typ, data = self.imap.uid("SEARCH", "X-GM-RAW", f'"{consulta}"')
        uids = (data[0] or b"").split()
        if not uids:
            return []
        # primero solo los ids, para no bajar de nuevo correos ya procesados
        typ, d = self.imap.uid("FETCH", b",".join(uids).decode(), "(X-GM-MSGID)")
        nuevos = []
        for linea in d or []:
            linea = linea[0] if isinstance(linea, tuple) else linea
            m = re.search(rb"X-GM-MSGID (\d+)", linea or b"")
            u = re.search(rb"UID (\d+)", linea or b"")
            if m and u:
                gid = format(int(m.group(1)), "x")
                if not ya_visto(gid):
                    nuevos.append((u.group(1), gid))
        out = []
        for uid, gid in nuevos:
            typ, d = self.imap.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
            if d and isinstance(d[0], tuple):
                out.append((uid, gid, d[0][1]))
        return out

    def recientes(self, dias, ya_visto):
        """Devuelve [Correo] de bancos, Yappy, facturas y correos propios, sin repetir los ya procesados."""
        de = " OR ".join(f"from:{r}" for r in REMITENTES_BANCO)
        consultas = [
            (self.todos, f"newer_than:{dias}d ({de})"),
            (self.todos, f"newer_than:{dias}d (subject:PI-BANCOS OR subject:(Cuentas x Cobrar) OR subject:(Copia de nota))"),
            (self.papelera, f"newer_than:{dias}d (subject:PI-BANCOS OR subject:(Cuentas x Cobrar))"),
            (self.todos, f"newer_than:{max(dias, 10)}d (subject:factura OR subject:comprobante OR subject:recibo) -from:cwpanama.com"),
        ]
        vistos, out = set(), []
        for carpeta, q in consultas:
            for uid, gid, raw in self._buscar(carpeta, q, lambda g: g in vistos or ya_visto(g)):
                if gid in vistos:
                    continue
                vistos.add(gid)
                out.append(_a_correo(gid, raw))
        out.sort(key=lambda c: c.fecha)
        return out

    def a_papelera(self, gid):
        self.imap.select(f'"{self.todos}"', readonly=False)
        typ, data = self.imap.uid("SEARCH", "X-GM-MSGID", str(int(gid, 16)))
        for uid in (data[0] or b"").split():
            self.imap.uid("STORE", uid, "+X-GM-LABELS", "\\Trash")

    def cerrar(self):
        try:
            self.imap.logout()
        except Exception:
            pass
