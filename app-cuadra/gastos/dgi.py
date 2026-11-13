"""
Facturas electrónicas de la DGI Panamá (dgi-fep.mef.gob.pa), solo HTTP (sin navegador).

  * La página del QR trae <input type=hidden name=facturaXML>.
  * POST /Consultas/DescargarFacturaPDF con facturaXML devuelve el PDF.
  * Si la factura aún no está subida, la página no trae facturaXML -> PENDING (no es error).
  * El buscador por CUFE usa reCAPTCHA: NO se automatiza. Solo se usan el link del QR
    y la ruta pública /Consultas/FacturasPorCUFE/<cufe> que usa el propio sitio.

Basado en el dgi_scraper.py que mandó Titular (12 nov 2026), adaptado al paquete gastos.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

HOST = "dgi-fep.mef.gob.pa"
BASE = "https://" + HOST
CUFE_PAGE = "/Consultas/FacturasPorCUFE"
PDF_POST = "/Consultas/DescargarFacturaPDF"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
CUFE_RE = re.compile(r"FE[0-9A-Za-z\-]{40,}")

OK, PENDING, INVALID, ERROR = "OK", "PENDING", "INVALID", "ERROR"


@dataclass
class InvoiceResult:
    status: str
    cufe: Optional[str] = None
    message: str = ""
    pdf_bytes: Optional[bytes] = None
    pdf_filename: Optional[str] = None
    data: dict = field(default_factory=dict)


def parse_qr_url(qr_text: str) -> Optional[dict]:
    """Texto leído del QR -> {'cufe','url'} solo si es un link https de la DGI."""
    qr_text = (qr_text or "").strip()
    u = urlparse(qr_text)
    if u.scheme == "https" and u.hostname == HOST:
        cufe = (parse_qs(u.query).get("chFE") or [None])[0]
        if cufe and CUFE_RE.fullmatch(cufe):
            return {"cufe": cufe, "url": qr_text}
    return None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "es-PA,es;q=0.9"})
    return s


def _lines(soup):
    return [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]


def _after(lines, label, offset=1):
    for i, l in enumerate(lines):
        if l.upper() == label.upper():
            return lines[i + offset] if i + offset < len(lines) else None
    return None


def _num(s):
    try:
        return float(s.replace(",", "")) if s else None
    except ValueError:
        return None


def parse_invoice_html(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    L = _lines(soup)
    d = {}
    d["numero"] = next((re.sub(r"^No\.\s*", "", l) for l in L if re.match(r"^No\.\s*\d+", l)), None)
    d["fecha_emision"] = next((l for l in L if re.fullmatch(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}", l)), None)
    d["cufe"] = _after(L, "CÓDIGO ÚNICO DE FACTURA ELECTRÓNICA [CUFE]")
    d["protocolo_autorizacion"] = _after(L, "PROTOCOLO DE AUTORIZACIÓN")
    i_em = L.index("EMISOR") if "EMISOR" in L else 0
    i_re = L.index("RECEPTOR") if "RECEPTOR" in L else len(L)
    emisor = L[i_em:i_re]
    d["emisor_ruc"] = _after(emisor, "RUC")
    d["emisor_dv"] = _after(emisor, "DV")
    d["emisor_nombre"] = _after(emisor, "NOMBRE")
    d["receptor_nombre"] = _after(L[i_re:], "NOMBRE")
    d["total"] = _num(_after(L, "Valor Total:"))
    d["itbms"] = _num(_after(L, "ITBMS Total:"))
    d["total_pagado"] = _num(_after(L, "TOTAL PAGADO:"))
    return d


def _xml_field(html: str) -> Optional[str]:
    inp = BeautifulSoup(html, "html.parser").find("input", {"name": "facturaXML"})
    return (inp.get("value") if inp else None) or None


def _download_pdf(s, xml_field, referer):
    r = s.post(BASE + PDF_POST, data={"facturaXML": xml_field},
               headers={"Referer": referer, "Origin": BASE}, timeout=30)
    if r.status_code != 200 or "pdf" not in r.headers.get("content-type", "").lower() or not r.content.startswith(b"%PDF"):
        return None, None
    m = re.search(r'filename="?([^";]+)"?', r.headers.get("content-disposition", ""))
    return r.content, (m.group(1) if m else "factura.pdf")


def fetch_invoice(qr_url: Optional[str] = None, cufe: Optional[str] = None) -> InvoiceResult:
    s = _session()
    try:
        if qr_url:
            p = parse_qr_url(qr_url)
            if not p:
                return InvoiceResult(INVALID, message="El QR no es un link válido de la DGI")
            cufe, referer = p["cufe"], p["url"]
        elif cufe:
            cufe = cufe.strip()
            if not CUFE_RE.fullmatch(cufe):
                return InvoiceResult(INVALID, cufe=cufe, message="CUFE con formato inválido")
            referer = f"{BASE}{CUFE_PAGE}/{cufe}"
        else:
            return InvoiceResult(INVALID, message="Falta el QR")
        r = s.get(referer, timeout=30)
    except requests.RequestException as e:
        return InvoiceResult(ERROR, cufe=cufe, message=f"Red: {e}")
    if r.status_code >= 500:
        return InvoiceResult(ERROR, cufe=cufe, message=f"La DGI respondió {r.status_code}")
    xml = _xml_field(r.text)
    if not xml and qr_url:
        try:  # respaldo: la ruta pública por CUFE
            referer = f"{BASE}{CUFE_PAGE}/{cufe}"
            r = s.get(referer, timeout=30)
            xml = _xml_field(r.text)
        except requests.RequestException as e:
            return InvoiceResult(ERROR, cufe=cufe, message=f"Red: {e}")
    if not xml:
        return InvoiceResult(PENDING, cufe=cufe, message="La DGI aún no tiene esta factura")
    data = parse_invoice_html(r.text)
    try:
        pdf, fname = _download_pdf(s, xml, referer)
    except requests.RequestException as e:
        return InvoiceResult(ERROR, cufe=cufe, data=data, message=f"Red al bajar el PDF: {e}")
    if not pdf:
        return InvoiceResult(ERROR, cufe=cufe, data=data, message="La DGI no devolvió un PDF válido")
    return InvoiceResult(OK, cufe=data.get("cufe") or cufe, pdf_bytes=pdf, pdf_filename=fname, data=data, message="OK")


if __name__ == "__main__":
    import json, sys
    a = sys.argv[1] if len(sys.argv) > 1 else ""
    res = fetch_invoice(qr_url=a) if a.startswith("http") else fetch_invoice(cufe=a)
    print(res.status, res.message)
    print(json.dumps(res.data, ensure_ascii=False, indent=1))
    if res.pdf_bytes:
        print("PDF:", res.pdf_filename, len(res.pdf_bytes), "bytes, empieza con", res.pdf_bytes[:4])
