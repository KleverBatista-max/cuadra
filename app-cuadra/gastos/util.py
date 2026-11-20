import re
import unicodedata
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from html import unescape

PANAMA = timezone(timedelta(hours=-5))  # Panamá no cambia de horario


def sin_acentos(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def norm(s):
    return re.sub(r"\s+", " ", sin_acentos(s).lower()).strip()


def a_panama(dt_utc):
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(PANAMA)


def ahora_panama():
    return datetime.now(PANAMA)


def monto(txt):
    """'1,170.00' -> 1170.0 ; '$4.5' -> 4.5"""
    if txt is None:
        return None
    t = str(txt).replace("$", "").replace("US", "").replace("USD", "").replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return round(float(m.group(0)), 2) if m else None


def centavos(x):
    return int(round(abs(float(x)) * 100))


def fecha_de(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def dias_entre(a, b):
    return abs((fecha_de(a) - fecha_de(b)).days)


MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8,
         "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
         "jan": 1, "apr": 4, "aug": 8, "dec": 12}


class _Texto(HTMLParser):
    BLOQUES = {"p", "div", "br", "tr", "td", "th", "li", "h1", "h2", "h3", "h4", "table", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        if tag in self.BLOQUES:
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        if tag in self.BLOQUES:
            self.partes.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.partes.append(data)


def html_a_texto(html):
    p = _Texto()
    p.feed(html or "")
    t = unescape("".join(p.partes))
    t = t.replace("\xa0", " ")
    lineas = [re.sub(r"[ \t]+", " ", l).strip() for l in t.splitlines()]
    return "\n".join(l for l in lineas if l)


_PEQ = {"de", "del", "la", "las", "el", "los", "y", "en", "por", "a"}
_SIGLAS = {"BAC", "BG", "ATM", "ACH", "USA", "PS5", "UPS", "DHL", "BV", "SA", "CWP"}
_FIJOS = {"PAYPAL": "PayPal", "MCDONALDS": "McDonald's", "MC DONALDS": "McDonald's"}


def limpiar_comercio(s):
    """Nombre de comercio legible a partir de lo que manda el banco."""
    if not s:
        return ""
    t = unescape(s).strip()
    t = re.sub(r"[\\┴]", " ", t).strip()
    t = re.sub(r"_\d+$", "", t)                      # ZAZ FA PLAZA EDISON_48
    t = re.sub(r"\*+", " ", t)                       # PAYPAL *TEMU
    t = re.sub(r"\b(PANAM\.?\s+PAN|PANAMA\s+PAN|PAN)\.?\s*$", "", t, flags=re.I)
    t = re.sub(r"\b(SAN FRANCISCO|PANAMA|PANAM)\s*$", "", t, flags=re.I)
    t = re.sub(r"\b(?=\w*\d)(?=\w*[A-Za-z])\w{6,}\b", "", t)   # códigos tipo 3C8LC7YI3
    t = re.sub(r"\s+(EUR|USD)$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.-")
    palabras = []
    for i, w in enumerate(t.split(" ")):
        up = w.upper()
        if up in _FIJOS:
            palabras.append(_FIJOS[up])
        elif up in _SIGLAS:
            palabras.append(up)
        elif i > 0 and w.lower() in _PEQ:
            palabras.append(w.lower())
        elif w.isupper() or w.islower():
            palabras.append(w[:1].upper() + w[1:].lower())
        else:
            palabras.append(w)
    return " ".join(palabras)[:60]


def nombre_persona(nombre, mapa):
    n = norm(nombre)
    for k, v in mapa.items():
        if n == k or n.startswith(k):
            return v
    return None
