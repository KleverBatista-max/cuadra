"""Lectores de correos. Cada uno recibe un Correo y devuelve una lista de acciones.

Acciones posibles (tuplas):
  ("mov", {...})                 movimiento nuevo
  ("ignorar", motivo)            correo conocido que no genera nada
  ("pi_bancos", dict)            JSON del lector de bancos de la Raspberry
  ("nota_cxc", texto)            nota "Cuentas x Cobrar" del iPhone
  ("movil_factura", {...})       factura +Móvil disponible
  ("factura_correo", {...})      correo que parece factura de un comercio
  ("error", motivo)              correo de un remitente conocido que no se pudo leer
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import config as C
from .util import (MESES, a_panama, html_a_texto, limpiar_comercio, monto, nombre_persona, norm)


@dataclass
class Correo:
    msgid: str                 # id hex de Gmail (igual que el de la API) u otro único
    remitente: str
    asunto: str
    fecha: datetime            # UTC
    texto: str = ""
    html: str = ""
    adjuntos: list = field(default_factory=list)  # [(nombre, tipo, bytes)]
    autenticado: bool = True   # el remitente pasó SPF/DKIM (o es un correo enviado por Titular a sí mismo)

    @property
    def cuerpo(self):
        t = (self.texto or "").strip()
        if len(t) < 20 and self.html:
            return html_a_texto(self.html)
        return t

    @property
    def local(self):
        return a_panama(self.fecha)

    @property
    def url(self):
        if re.fullmatch(r"[0-9a-f]{16}", self.msgid or ""):
            return f"https://mail.google.com/mail/u/0/#all/{self.msgid}"
        return ""


def _base(c, **kw):
    d = dict(fecha=None, hora=None, comercio="", monto=None, medio="", tipo="Compra",
             quien="Titular", bolsa="Titular", factura="Pendiente", notas="", origen="correo")
    d.update(kw)
    if d["fecha"] is None:
        d["fecha"] = c.local.strftime("%Y-%m-%d")
        d["hora"] = d["hora"] or c.local.strftime("%H:%M")
    d["mes"] = d["fecha"][:7]
    return d


def quien_bolsa(medio):
    return C.TARJETAS.get(medio, ("Por confirmar", "Por confirmar"))


# ---------------- Yappy ----------------
_Y_MONTO = re.compile(r"(Te enviaron|Pagaste|Enviaste) con Yappy\W+\$?\s*([\d,]+\.\d{2})", re.I)


def yappy(c):
    t = re.sub(r"[|#]", " ", c.cuerpo)
    t = re.sub(r"[ \t]+", " ", t)
    m = _Y_MONTO.search(t)
    conf = re.search(r"Confirmaci[oó]n\s+([A-Z]{5}-\d{6,})", t)
    fecha = re.search(r"Fecha\s+(\d{1,2})\s+([a-zA-Z]{3})\w*\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?", t, re.I)
    if not (m and conf):
        return [("error", "Yappy sin monto o confirmación")]
    accion, valor = m.group(1).lower(), monto(m.group(2))
    if fecha:
        d, mes, y, hh, mm, ap = fecha.groups()
        hh = int(hh) % 12 + (12 if ap.lower() == "p" else 0)
        f = f"{int(y):04d}-{MESES[mes.lower()[:3]]:02d}-{int(d):02d}"
        h = f"{hh:02d}:{mm}"
    else:
        f, h = c.local.strftime("%Y-%m-%d"), c.local.strftime("%H:%M")
    msj = re.search(r"Mensaje\s+(.*?)\s+Confirmaci", t, re.S)
    mensaje = (msj.group(1).strip() if msj else "")
    id = "yp-" + conf.group(1)

    def persona(pat):
        p = re.search(pat + r"\s+(.+?)\s+(?:\d{8}|\*{4}-\d{4}|\n|Fecha)", t)
        nombre = p.group(1).strip() if p else ""
        return re.sub(r"\s+", " ", nombre)

    if accion == "te enviaron":
        nombre = persona(r"Enviado por")
        quien = nombre_persona(nombre, C.NOMBRES_PERSONA) or nombre or "Por confirmar"
        mov = _base(c, fecha=f, hora=h, comercio=nombre, monto=valor, medio="Yappy", tipo="Yappy recibido",
                    quien=quien, bolsa="Ingreso", factura="No aplica",
                    notas=f"Mensaje: {mensaje}" if mensaje else "")
    elif accion == "pagaste":
        nombre = persona(r"Pagado a")
        mov = _base(c, fecha=f, hora=h, comercio=limpiar_comercio(nombre) if nombre.isupper() else nombre,
                    monto=valor, medio="Yappy", tipo="Yappy pago", factura="Pendiente")
    else:
        nombre = persona(r"Enviado a")
        mov = _base(c, fecha=f, hora=h, comercio=nombre, monto=valor, medio="Yappy", tipo="Yappy enviado",
                    factura="No aplica")
    mov["id"] = id
    return [("mov", mov)]


# ---------------- BAC (tarjetas) ----------------
_BAC_MARCAS = {"AMEX": "BAC Amex", "MASTERCARD": "BAC MC", "VISA": "BAC Visa"}


def bac_tarjeta(c):
    t = c.cuerpo
    m = re.search(r"tarjeta\s+(adicional\s+)?(AMEX|MASTERCARD|VISA)\s+terminada en\s+(\d{4})(?:\s+a nombre de\s+([A-ZÁÉÍÓÚÑ ]+?)\s*\n)?", t, re.I)
    lineas = [l.strip() for l in t.splitlines() if l.strip()]
    try:
        i = lineas.index("Monto")
        comercio, montotxt = lineas[i + 1], lineas[i + 2]
    except (ValueError, IndexError):
        comercio = montotxt = None
    fh = re.search(r"(\d{4})/(\d{2})/(\d{2})-(\d{2}):(\d{2})", t)
    estado = re.search(r"\b(Aprobada|Rechazada|Denegada)\b", t)
    if not (m and comercio and montotxt and fh):
        return [("error", "Alerta BAC sin datos completos")]
    if estado and estado.group(1).lower() != "aprobada":
        return [("ignorar", f"BAC transacción {estado.group(1)}")]
    ult4 = m.group(3)
    if ult4 in C.TARJETAS_AJENAS:
        return [("ignorar", f"tarjeta ajena {ult4}")]
    medio = f"{_BAC_MARCAS[m.group(2).upper()]} {ult4}"
    quien, bolsa = quien_bolsa(medio)
    nombre_adic = (m.group(4) or "").strip()
    if m.group(1) and nombre_adic:
        p = nombre_persona(nombre_adic, {"beatriz": "Beatriz", "ana": "Ana"})
        if p == "Beatriz":
            quien, bolsa = "Beatriz", "Beatriz"
        elif medio not in C.TARJETAS:
            quien, bolsa = (p or nombre_adic.title()), "Titular"
    y, mo, d, hh, mi = fh.groups()
    mov = _base(c, fecha=f"{y}-{mo}-{d}", hora=f"{hh}:{mi}", comercio=limpiar_comercio(comercio),
                monto=monto(montotxt), medio=medio, quien=quien, bolsa=bolsa)
    mov["id"] = "gm-" + c.msgid
    return [("mov", mov)]


# ---------------- Banesco ----------------
def banesco(c):
    t = re.sub(r"\s+", " ", c.cuerpo)
    if "activado" in t.lower() or "Activada" in c.asunto:
        return [("ignorar", "aviso Banesco")]
    m = re.search(r"Transaccion de (\w+) con su tarjeta (.+?) con terminaci[oó]n \**(\d{4}) por un monto de ([\d,]+\.\d{2}) USD, en (.+?)(?: Si usted|$)", t)
    if not m:
        if "Alerta de Consumo" in c.asunto or "Abono" in c.asunto:
            return [("error", "Alerta Banesco sin datos")]
        return [("ignorar", "correo Banesco informativo")]
    clase, _, ult4, valor, comercio = m.groups()
    if ult4 in C.TARJETAS_AJENAS:
        return [("ignorar", f"tarjeta ajena {ult4}")]
    medio = f"Banesco Visa {ult4}"
    quien, bolsa = quien_bolsa(medio)
    abono = clase.lower() in ("credit", "credito", "crédito", "abono") or "Abono" in c.asunto
    v = monto(valor)
    mov = _base(c, comercio=("Abono de " if abono else "") + limpiar_comercio(comercio),
                monto=-v if abono else v, medio=medio, tipo="Abono" if abono else "Compra",
                quien=quien, bolsa=bolsa, factura="No aplica" if abono else "Pendiente")
    mov["id"] = "gm-" + c.msgid
    return [("mov", mov)]


# ---------------- Global Bank ----------------
def globalbank(c):
    t = re.sub(r"\s+", " ", c.cuerpo)
    m = re.search(r"compra de \$([\d,]+\.\d{2}) en (.+?) con tarjeta Global Bank terminaci[oó]n (\d{4})", t)
    if not m:
        if "CONFIRMACION DE TRANSACCION" in c.asunto.upper():
            return [("error", "Alerta Global sin datos")]
        return [("ignorar", "correo Global informativo")]
    valor, comercio, ult4 = m.groups()
    if ult4 in C.TARJETAS_AJENAS:
        return [("ignorar", f"tarjeta ajena {ult4}")]
    medio = f"Global {ult4}"
    quien, bolsa = quien_bolsa(medio)
    mov = _base(c, comercio=limpiar_comercio(comercio), monto=monto(valor), medio=medio, quien=quien, bolsa=bolsa)
    if re.search(r"SERVICIO FIJO MAS MOV|MAS MOVIL|\+M[OÓ]VIL", comercio, re.I):
        mov.update(comercio="+Móvil (factura del plan)", bolsa="Excluido", factura="No aplica",
                   notas="Factura +Móvil", movil=True)
    elif re.search(r"\bpuma\b", comercio, re.I) and float(valor.replace(",", "")) in (10.0, 20.0, 50.0, 85.0, 100.0):
        mov["notas"] = "Posible preautorización de gasolinera; se ajusta con el banco."
        mov["preaut"] = True
    mov["id"] = "gm-" + c.msgid
    return [("mov", mov)]


# ---------------- Banco General (ACH saliente) ----------------
def bgeneral(c):
    t = c.cuerpo
    if "transferencia" not in t.lower() or "realizada" not in t.lower():
        return [("ignorar", "correo Banco General informativo")]
    cta = re.search(r"debitada de la cuenta con terminaci[oó]n\s*(\d{4})?", t)
    a = re.search(r"\bA:\s*(.+)", t)
    banco = re.search(r"Banco:\s*(.+)", t)
    v = re.search(r"Monto:\s*US\$?\s*([\d,]+\.\d{2})", t)
    f = re.search(r"Fecha de [Tt]ransferencia:\s*(\d{2})-([a-z]{3})-(\d{2})", t)
    if not (a and v):
        return [("error", "Transferencia BG sin datos")]
    medio = f"BG {cta.group(1)}" if cta and cta.group(1) else "BG cuenta"
    fecha = None
    if f:
        d, mes, y = f.groups()
        fecha = f"20{y}-{MESES[mes.lower()]:02d}-{d}"
    destino = a.group(1).strip()
    mov = _base(c, comercio=destino, monto=monto(v.group(1)), medio=medio, tipo="ACH enviado",
                bolsa="Por confirmar", factura="No aplica",
                notas=f"Banco destino: {banco.group(1).strip()}" if banco else "")
    if fecha:
        mov["fecha"], mov["mes"] = fecha, fecha[:7]
        if fecha != c.local.strftime("%Y-%m-%d"):
            mov["hora"] = "12:00"
    if medio == "BG cuenta":
        mov["notas"] = (mov["notas"] + " · El correo no dice la cuenta debitada.").strip(" ·")
    if any(n in t for n in C.CUENTAS_PROPIAS):
        mov["bolsa"] = "Excluido"
    mov["id"] = "gm-" + c.msgid
    return [("mov", mov)]


# ---------------- BAC (transferencia de la cuenta de ahorros, llega por Outlook) ----------------
def bac_ach(c):
    t = re.sub(r"\s+", " ", c.cuerpo)
    m = re.search(r"Ha hecho una transferencia por USD ([\d,]+\.\d{2})", t)
    if not m:
        return [("ignorar", "aviso BAC")]
    cta = re.search(r"n[uú]mero \*+(\d{4})", t)
    f = re.search(r"Fecha (\d{2})/(\d{2})/(\d{4}) a las (\d{1,2}):(\d{1,2})", t)
    medio = f"BAC {cta.group(1)}" if cta else "BAC 1110"
    kw = {}
    if f:
        d, mo, y, hh, mi = f.groups()
        kw = dict(fecha=f"{y}-{mo}-{d}", hora=f"{int(hh):02d}:{int(mi):02d}")
    mov = _base(c, comercio="Transferencia desde ahorros BAC", monto=monto(m.group(1)), medio=medio,
                tipo="ACH enviado", bolsa="Por confirmar", factura="No aplica", **kw)
    mov["id"] = "ol-" + re.sub(r"[^A-Za-z0-9]", "", c.msgid)[-24:]
    return [("mov", mov)]


# ---------------- +Móvil (factura disponible) ----------------
def cw_factura(c):
    t = c.cuerpo
    cta = re.search(r"cuenta(?: para pagos)?\s*(\d{9,})", c.asunto + " " + t)
    vence = re.search(r"Fecha l[ií]mite de pago\s*(\d{2})/([A-Za-z]{3})/(\d{4})", t)
    emision = re.search(r"Fecha de emisi[oó]n\s*(\d{2})/([A-Za-z]{3})/(\d{4})", t)
    if not cta:
        return [("error", "Factura +Móvil sin número de cuenta")]
    if cta.group(1) != C.MOVIL_CUENTA:
        return [("ignorar", f"factura +Móvil de otra cuenta ({cta.group(1)})")]
    if emision:
        d, mo, y = emision.groups()
        mes = f"{y}-{MESES[mo.lower()]:02d}"
    else:
        mes = c.local.strftime("%Y-%m")
    v = None
    if vence:
        d, mo, y = vence.groups()
        v = f"{y}-{MESES[mo.lower()]:02d}-{d}"
    return [("movil_factura", {"mes": mes, "vence": v})]


# ---------------- Correos propios: PI-BANCOS y nota ----------------
def propio(c):
    asunto = c.asunto or ""
    if not c.autenticado:
        return [("error", "correo que dice venir de Titular pero no pasó la verificación del remitente")]
    if "PI-BANCOS" in asunto:
        for nombre, tipo, data in c.adjuntos:
            if nombre.endswith(".json"):
                try:
                    return [("pi_bancos", json.loads(data.decode("utf-8")))]
                except Exception:
                    return [("error", "PI-BANCOS con JSON dañado")]
        m = re.search(r"\{.*\"bancos\".*\}", c.texto or "", re.S)
        if m:
            try:
                return [("pi_bancos", json.loads(m.group(0)))]
            except Exception:
                pass
        return [("error", "PI-BANCOS sin JSON")]
    if "Cuentas x Cobrar" in asunto or "Copia de nota" in asunto:
        cuerpo = c.cuerpo
        if "Nombre Monto" not in cuerpo:
            return [("ignorar", "nota vacía")]
        return [("nota_cxc", cuerpo)]
    return None


# ---------------- Facturas que llegan por correo ----------------
_FACT = re.compile(r"factura|comprobante|recibo|invoice|receipt", re.I)


def posible_factura(c):
    if not _FACT.search(c.asunto or ""):
        return None
    texto = c.cuerpo
    for nombre, tipo, data in c.adjuntos:
        if nombre.lower().endswith(".pdf"):
            try:
                from .facturas import texto_pdf
                texto += "\n" + texto_pdf(data)
            except Exception:
                pass
    totales = []
    for m in re.finditer(r"(?:total(?:\s+a\s+pagar|\s+pagado)?|monto total|importe)\s*[:\-]?\s*(?:B/\.|US\$|\$|USD)?\s*([\d,]+\.\d{2})", texto, re.I):
        totales.append(monto(m.group(1)))
    if not totales:
        return None
    return [("factura_correo", {"asunto": c.asunto, "remitente": c.remitente, "url": c.url,
                                "fecha": c.local.strftime("%Y-%m-%d"), "totales": sorted(set(totales))})]


# ---------------- Enrutador ----------------
LECTORES = [
    (lambda c: "yappy.com.pa" in c.remitente, yappy),
    (lambda c: "pa.bac.net" in c.remitente, bac_tarjeta),
    (lambda c: "banesco.com.pa" in c.remitente, banesco),
    (lambda c: "globalbank.com.pa" in c.remitente, globalbank),
    (lambda c: c.remitente == "mensajesyalertas@bgeneral.com", bgeneral),
    (lambda c: "Movimiento Monetario" in (c.asunto or "") and "baccredomatic" in (c.remitente + c.cuerpo[:3000]).lower(), bac_ach),
    (lambda c: c.remitente == "facturacion@cwpanama.com" and "Factura" in (c.asunto or ""), cw_factura),
    (lambda c: c.remitente in C.REMITENTES_PROPIOS, propio),
]


def leer(c):
    c.remitente = (c.remitente or "").lower().strip()
    for cond, lector in LECTORES:
        try:
            if cond(c):
                r = lector(c)
                if r is not None:
                    return r
        except Exception as e:  # nunca tumbar el proceso por un correo raro
            return [("error", f"{lector.__name__}: {e}")]
    return posible_factura(c) or [("ignorar", "remitente no reconocido")]
