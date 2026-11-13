"""Lectura de facturas subidas (PDF con texto; fotos si la Raspberry tiene tesseract)."""
import io
import re
import shutil
import subprocess
import tempfile

from .util import monto


def texto_pdf(data):
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    partes = []
    for p in r.pages[:3]:
        try:
            partes.append(p.extract_text() or "")
        except Exception:
            pass
    return "\n".join(partes)


def texto_imagen(data):
    """OCR opcional. Si no está instalado tesseract, devuelve ''."""
    if not shutil.which("tesseract"):
        return ""
    with tempfile.NamedTemporaryFile(suffix=".img") as f:
        f.write(data)
        f.flush()
        try:
            out = subprocess.run(["tesseract", f.name, "-", "-l", "spa"], capture_output=True, timeout=60)
            return out.stdout.decode("utf-8", "ignore")
        except Exception:
            return ""


def _fecha(t):
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", t)
    if m:
        d, mo, y = m.groups()
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{y}-{int(mo):02d}-{int(d):02d}"
    return ""


def interpretar(t):
    """Saca emisor, ruc, número, fecha, total e ITBMS del texto de una factura panameña."""
    if not t or len(t.strip()) < 20:
        return None
    lineas = [l.strip() for l in t.splitlines() if l.strip()]
    total = None
    for pat in (r"total\s+(?:a\s+)?pagad[oa]\s*[:\s]\s*(?:B/\.|\$|USD)?\s*([\d,]+\.\d{2})",
                r"total\s+a\s+pagar\s*[:\s]\s*(?:B/\.|\$|USD)?\s*([\d,]+\.\d{2})",
                r"valor\s+total\s*[:\s]\s*(?:B/\.|\$|USD)?\s*([\d,]+\.\d{2})",
                r"\btotal\b\s*[:\s]\s*(?:B/\.|\$|USD)?\s*([\d,]+\.\d{2})"):
        ms = re.findall(pat, t, re.I)
        if ms:
            total = monto(ms[-1])
            break
    itbms = None
    m = re.search(r"(?:total\s+)?itbms\s*(?:\(?7%\)?)?\s*[:\s]\s*(?:B/\.|\$)?\s*([\d,]+\.\d{2})", t, re.I)
    if m:
        itbms = monto(m.group(1))
    ruc = ""
    m = re.search(r"R\.?U\.?C\.?\s*[:#]?\s*([0-9A-Z][0-9A-Z\-]{4,30})", t)
    if m:
        ruc = m.group(1).strip("-")
    numero = ""
    m = re.search(r"(?:factura|n[uú]mero de documento|no\.?\s*documento)\s*(?:n[°º.o]*|#)?\s*[:\s]\s*([0-9][0-9\-]{3,})", t, re.I)
    if m:
        numero = m.group(1)
    emisor = ""
    for l in lineas[:8]:
        if re.search(r"[A-Za-z]{3}", l) and not re.search(r"factura|comprobante|ruc|fecha|dgi|cufe|auxiliar", l, re.I):
            emisor = l[:80]
            break
    if total is None and not emisor:
        return None
    return {"emisor": emisor, "ruc": ruc, "numero": numero, "fecha": _fecha(t), "total": total, "itbms": itbms}


def leer(nombre, tipo, data):
    es_pdf = (tipo or "").endswith("pdf") or (nombre or "").lower().endswith(".pdf")
    try:
        t = texto_pdf(data) if es_pdf else texto_imagen(data)
    except Exception:
        t = ""
    return interpretar(t)
