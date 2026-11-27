"""Lectores de texto de cada banco (sin navegador, se pueden probar con texto copiado)."""
import re

MESES = {"ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AGO": 8,
         "SEP": 9, "SET": 9, "OCT": 10, "NOV": 11, "DIC": 12}
MESES_LARGOS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
                "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}


def money(s):
    """'-$1,150.00' -> -1150.0 ; '+ 150.00' -> 150.0 ; '836.07 USD' -> 836.07"""
    s = s.replace("USD", "").replace("$", "").replace(",", "").replace(" ", "").replace("+", "")
    return float(s)


def iso(d, m, y):
    return "%04d-%02d-%02d" % (int(y), int(m), int(d))


# ---------------- Global Bank ----------------
def global_dashboard(text):
    cuentas, tarjetas = [], []
    for m in re.finditer(r"^(.+)\n(\d{8,12})\n\$([\d,\.]+)\nDisponible\s*$", text, re.M):
        cuentas.append({"nombre": m.group(1).strip(), "numero": m.group(2), "saldo": money(m.group(3))})
    for m in re.finditer(r"^(.+)\nXXXX-(\d{4})\n\$([\d,\.]+)\nDisponible al (\d+ \w+)", text, re.M):
        tarjetas.append({"nombre": m.group(1).strip(), "ultimos4": m.group(2), "disponible": money(m.group(3)),
                         "al": m.group(4)})
    return cuentas, tarjetas


def global_cuenta_movs(text):
    out = []
    for m in re.finditer(r"(\d{2}) ([A-Z]{3}) (\d{4})\t(\d+)\t(.+?)\t(-?\$[\d,\.]+)\t(-?\$[\d,\.]+)", text):
        out.append({"fecha": iso(m.group(1), MESES[m.group(2)], m.group(3)), "ref": m.group(4),
                    "desc": m.group(5).strip(), "monto": money(m.group(6))})
    return out


def global_tarjeta(text):
    d = {}
    m = re.search(r"Límite de la tarjeta:\s*\$([\d,\.]+)", text)
    if m: d["limite"] = money(m.group(1))
    m = re.search(r"\$([\d,\.]+)\n\$([\d,\.]+)\nSaldo utilizado\nDisponible al (\d+ \w+)", text)
    if m:
        d["deuda"] = money(m.group(1)); d["disponible"] = money(m.group(2)); d["al"] = m.group(3)
    m = re.search(r"Pago mínimo\n\$([\d,\.]+)", text)
    if m: d["pagoMin"] = money(m.group(1))
    m = re.search(r"Próximo pago\n(\d{2}) ([A-Z]{3}) (\d{4})", text)
    if m: d["pagoDia"] = int(m.group(1))
    movs = []
    for m in re.finditer(r"(\d{2}) ([A-Z]{3}) (\d{4})\t(.+?)\t(-?\$[\d,\.]+)\t", text):
        movs.append({"fecha": iso(m.group(1), MESES[m.group(2)], m.group(3)), "desc": m.group(4).strip(),
                     "monto": money(m.group(5))})
    d["movs"] = movs
    return d


def global_estado(text):
    """«Estado de cuenta» de la tarjeta de Global (27 nov 2026). Después del corte la pestaña «Movimientos» queda VACÍA y todo
    pasa al estado de cuenta (compras, pagos y el CASH BACK, que nunca sale como aviso del correo). Trae el detalle de la
    principal y, debajo, el de cada adicional («Detalle de compras y pagos - Adicional - NOMBRE - XXXX-XXXX-XXXX-1105»).
    Las compras de una adicional salen también en la lista de la principal con el mismo número de referencia: se quedan
    con la adicional. Devuelve [{"fecha","desc","monto","tarjeta"}]; tarjeta = últimos 4 de la adicional o None (principal);
    monto con el mismo signo que la pestaña Movimientos (compra negativa, pago/crédito positivo)."""
    i = text.find("Detalle de compras y pagos")
    if i < 0:
        return []
    filas, ref_adic = [], set()
    cortes = [(m.start(), m.group(1)) for m in re.finditer(r"Detalle de compras y pagos(?: - Adicional - .*? - XXXX-XXXX-XXXX-(\d{4}))?\n", text[i:])]
    for k, (ini, ult) in enumerate(cortes):
        fin = cortes[k + 1][0] if k + 1 < len(cortes) else len(text) - i
        for m in re.finditer(r"^(\d{2}) ([A-Z]{3}) (\d{4})\t([^\t]*)\t(.+?)\t(-?\$[\d,\.]+|-)\t(-?\$[\d,\.]+|-)\s*$", text[i + ini:i + fin], re.M):
            compra, pago = m.group(6), m.group(7)
            monto = money(compra) if compra != "-" else (abs(money(pago)) if pago != "-" else 0.0)
            ref = m.group(4).strip()
            if ult and ref:
                ref_adic.add(ref)
            filas.append({"fecha": iso(m.group(1), MESES[m.group(2)], m.group(3)), "ref": ref, "desc": m.group(5).strip(),
                          "monto": monto, "tarjeta": ult})
    out = []
    for f in filas:
        if f["tarjeta"] is None and f["ref"] and f["ref"] in ref_adic:
            continue   # compra de una adicional repetida en la lista de la principal
        out.append({"fecha": f["fecha"], "desc": f["desc"], "monto": f["monto"], "tarjeta": f["tarjeta"]})
    return out


def global_adicionales(text, principal=""):
    """Pestaña "Tarjetas adicionales" de la tarjeta de Global (14 nov 2026): lista NOMBRE / XXXX-1234 / $ / Disponible al.
    Devuelve [{"nombre", "ultimos4"}] sin la principal. El disponible de esa lista va atrasado: no se usa."""
    i = text.rfind("Tarjetas adicionales")
    out = []
    if i < 0:
        return out
    for m in re.finditer(r"^(.+)\nXXXX-(\d{4})\s*$", text[i:], re.M):
        if m.group(2) != principal and m.group(2) not in [x["ultimos4"] for x in out]:
            out.append({"nombre": m.group(1).strip(), "ultimos4": m.group(2)})
    return out


# ---------------- BAC ----------------
def bac_resumen(text):
    cuentas = [{"numero": m.group(1), "saldo": money(m.group(2))}
               for m in re.finditer(r"PA - CUENTA DE AHORROS\s+(\d+)\s+(-?[\d,\.]+) USD", text)]
    tarjetas = [{"nombre": m.group(1), "ultimos4": m.group(2), "saldo": money(m.group(3))}
                for m in re.finditer(r"PA - (AMERICANEXPRESS|MASTERCARD)\s+[\d\*\-]+(\d{4})\s+(-?[\d,\.]+) USD", text)]
    return cuentas, tarjetas


def bac_cuenta_movs(text):
    out = []
    for m in re.finditer(r"(\d{2})/(\d{2})/(\d{4})\t(\d+)\t(.+?)\t([\d,\.]+)\t\+? ?([\d,\.]+)\t([\d,\.]+)", text):
        deb, cred = money(m.group(6)), money(m.group(7))
        out.append({"fecha": iso(m.group(1), m.group(2), m.group(3)), "ref": m.group(4),
                    "desc": m.group(5).strip(), "monto": cred - deb})
    return out


def bac_tarjeta(text):
    d = {}
    m = re.search(r"Fecha de pago de contado:\s*(\d{1,2}) (\w+)", text)
    if m: d["pagoDia"] = int(m.group(1))
    m = re.search(r"Saldo en dolares:\s*(-?[\d,\.]+) USD", text)
    if m: d["deuda"] = money(m.group(1))
    m = re.search(r"Disponible para Compras\s*(-?[\d,\.]+)\s+USD", text)
    if m: d["disponible"] = money(m.group(1))
    movs = []
    # solo la tabla de movimientos (arriba hay "Fecha : dd/mm/aaaa" y "Saldo en dolares" que no son compras)
    i = max(text.rfind("Monto dólares"), text.rfind("Movimientos recientes"))
    tabla = text[i:] if i >= 0 else text
    # BAC agrupa por tarjeta: una línea "5184-43**-****-6791" y debajo las compras de ESA tarjeta (principal o
    # adicional). Cada compra lleva "tarjeta" = últimos 4 de su grupo (14 nov 2026: adicionales de Beatriz y Ana).
    grupos = [(g.start(), g.group(1)) for g in re.finditer(r"^\s*\d{4}-\d{2}\*\*-\*\*\*\*-(\d{3,4})\s*$", tabla, re.M)]  # Amex: solo 3 dígitos ("-754")
    for m in re.finditer(r"(\d{2})/(\d{2})/(\d{4})\s+((?:(?!\d{2}/\d{2}/\d{4}|USD).)+?)\s+(-?[\d,\.]+) USD", tabla, re.S):
        mov = {"fecha": iso(m.group(1), m.group(2), m.group(3)), "desc": " ".join(m.group(4).split()),
               "monto": -money(m.group(5))}
        g = [u4 for pos, u4 in grupos if pos < m.start()]
        if g:
            mov["tarjeta"] = g[-1]
        movs.append(mov)
    d["movs"] = movs
    return d


def bac_pago(text):
    """Ventana de 'Pagar tarjeta' (paso 1, Selección): PAGO DE CONTADO y PAGO MÍNIMO con su fecha límite.
    Solo se lee; nunca se elige monto ni cuenta."""
    d = {}
    m = re.search(r"PAGO DE CONTADO\s+(\d{2})/(\d{2})/(\d{4})\s+(-?[\d,\.]+)", text, re.I)
    if m: d["pagoContado"] = money(m.group(4)); d["pagoFecha"] = iso(m.group(1), m.group(2), m.group(3))
    m = re.search(r"PAGO M[IÍ]NIMO\s+(\d{2})/(\d{2})/(\d{4})\s+(-?[\d,\.]+)", text, re.I)
    if m: d["pagoMin"] = money(m.group(4))
    return d


def bac_transito(text):
    """Tabla 'En tránsito o flotantes' de BAC (tras pulsar Consultar): fecha + 'Compra en Proceso' + monto."""
    i = text.find("En tránsito o flotantes")
    j = text.find("Procesados", i + 1)
    if i < 0:
        return []
    zona = text[i:j if j > i else len(text)]
    # BAC a veces escribe el monto sin centavos ("20 USD"): el 14 nov se perdió una gasolina de $20 por exigir ".00"
    out = [{"fecha": iso(m.group(1), m.group(2), m.group(3)), "desc": " ".join(m.group(4).split()),
            "monto": money(m.group(5))}
           for m in re.finditer(r"(\d{2})/(\d{2})/(\d{4})\s+(.+?)\s+(-?[\d,]+(?:\.\d+)?)\s*USD", zona)]
    # BAC dice cuántos hay ("Tiene 2 movimientos en tránsito"). Si no se leyeron todos, la lista NO se da por leída
    # (None): así la app no mata una compra por una lectura incompleta.
    n = re.search(r"Tiene\s+(\d+)\s+movimientos?\s+en\s+tr[aá]nsito", zona, re.I)
    if n and int(n.group(1)) != len(out):
        print("  BAC tránsito: el banco dice %s y se leyeron %d: lista no confiable" % (n.group(1), len(out)))
        return None
    return out


def bac_financiamientos(text):
    """Pestaña «Financiamientos» de una tarjeta BAC (19 nov 2026): planes de cuotas (traslado de saldo, minicuotas).
    Ese saldo NO está en el «Saldo en dólares» ni en tránsito, pero sí baja el disponible y es deuda.
    Devuelve {"total": saldo faltante de todos los planes, "planes": [...]}, o None si la página no se entendió."""
    i = text.find("Saldo faltante")
    if i < 0:
        return None
    zona = text[i:]
    j = zona.find("Atrás")
    zona = zona[:j] if j > 0 else zona
    if re.search(r"No hay movimientos", zona, re.I):
        return {"total": 0.0, "planes": []}
    lineas = [l.strip() for l in zona.splitlines() if l.strip()]
    fecha_re = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
    din = re.compile(r"^-?[\d,]+(?:\.\d+)?\s*USD$")
    planes, k = [], 0
    while k < len(lineas):
        f = fecha_re.match(lineas[k])
        if not f:
            k += 1
            continue
        j = k + 1
        bloque = []
        while j < len(lineas) and not fecha_re.match(lineas[j]) and not lineas[j].startswith("Total"):
            bloque.append(lineas[j]); j += 1
        montos = [money(x) for x in bloque if din.match(x)]
        cuotas = next((re.match(r"^(\d{1,3})/(\d{1,3})$", x) for x in bloque if re.match(r"^\d{1,3}/\d{1,3}$", x)), None)
        if len(montos) >= 3 and bloque:
            planes.append({"fecha": iso(f.group(1), f.group(2), f.group(3)), "concepto": " ".join(bloque[0].split()),
                           "pagadas": int(cuotas.group(1)) if cuotas else None, "plazo": int(cuotas.group(2)) if cuotas else None,
                           "cuota": montos[-3], "inicial": montos[-2], "faltante": montos[-1]})
        k = j
    t = re.search(r"Total\s+(-?[\d,]+\.\d{2})\s*USD", zona)
    total = money(t.group(1)) if t else None
    if total is None or abs(total - sum(x["faltante"] for x in planes)) > 0.01:
        print("  BAC financiamientos: no cuadra la suma de los planes con el total: no se usa")
        return None
    return {"total": total, "planes": planes}


def banesco_transito(text):
    """Pestaña 'Movimientos en tránsito' de Banesco (versión computadora):
    Fecha transacción / No. de autorización / Descripción / Monto, una línea cada uno (negativo = compra)."""
    i = text.find("Son las transacciones")
    zona = text[i:] if i >= 0 else ""
    # montos con o sin centavos (14 nov: BAC escribió "20 USD" y se perdió una compra)
    out = [{"fecha": iso(m.group(1), m.group(2), m.group(3)), "desc": m.group(5).strip(), "monto": money(m.group(6))}
           for m in re.finditer(r"^[ \t]*(\d{2})/(\d{2})/(\d{4})[ \t]*\n+[ \t]*(\d+)[ \t]*\n+[ \t]*(\S.*?)[ \t]*\n+[ \t]*(-?[\d,]+(?:\.\d{1,2})?)[ \t]*$",
                                zona, re.M)]
    # la pestaña dice cuántos hay: "Movimientos en tránsito (1)". Si no se leyeron todos -> None (no confiable)
    n = re.search(r"Movimientos en tr[aá]nsito\s*\((\d+)\)", text)
    if n and int(n.group(1)) != len(out):
        print("  Banesco tránsito: el banco dice %s y se leyeron %d: lista no confiable" % (n.group(1), len(out)))
        return None
    return out


# ---------------- Banco General ----------------
def bg_dashboard(text):
    return [{"nombre": m.group(1).strip(), "numero": m.group(2), "saldo": money(m.group(3))}
            for m in re.finditer(r"^(.+)\n(\d{2}-\d{2}-\d{2}-\d{6}-\d)\n\$([\d,\.]+)\n\$[\d,\.]+\s*$", text, re.M)]


def bg_movs(text):
    """Filas: fecha (dd-mmm-aaaa) / referencia opcional / descripción (1+ líneas) / monto / saldo."""
    meses = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9,
             "oct": 10, "nov": 11, "dic": 12}
    lineas = [l.strip() for l in text.splitlines() if l.strip()]
    fecha_re = re.compile(r"^(\d{2})-([a-z]{3})-(\d{4})$")
    din = re.compile(r"^-?\$[\d,]+\.\d{2}$")
    out, i = [], 0
    while i < len(lineas):
        f = fecha_re.match(lineas[i])
        if not f or f.group(2) not in meses:
            i += 1; continue
        j, ref, desc = i + 1, None, []
        if j < len(lineas) and re.fullmatch(r"\d{5,}", lineas[j]):
            ref = lineas[j]; j += 1
        while j < len(lineas) and not din.match(lineas[j]) and not fecha_re.match(lineas[j]):
            desc.append(lineas[j]); j += 1
        if j + 1 < len(lineas) and din.match(lineas[j]) and din.match(lineas[j + 1]) and desc:
            out.append({"fecha": iso(f.group(1), meses[f.group(2)], f.group(3)), "ref": ref,
                        "desc": " ".join(" ".join(desc).split()), "monto": money(lineas[j])})
            i = j + 2
        else:
            i += 1
    return out


# ---------------- Banesco ----------------
def banesco_tarjeta(text):
    def g(label):
        m = re.search(label + r"\s+USD\s+(-?[\d,\.]+)", text)
        return money(m.group(1)) if m else None
    d = {"saldoCorte": g("Saldo al corte"), "deuda": g("Adeudado a la fecha"), "pagoMin": g("Pago mínimo"),
         "disponible": g("Disponible para compras")}
    m = re.search(r"Fecha de Corte\s+(\d{2})/(\d{2})/(\d{4})", text)
    if m: d["corteDia"] = int(m.group(1))
    m = re.search(r"Fecha límite de pago\s+(\d{2})/(\d{2})/(\d{4})", text)
    if m: d["pagoDia"] = int(m.group(1))
    movs = []
    # tabla real: Fecha / Descripción / Monto (después del encabezado "Fecha transacción")
    i = text.find("Fecha transacción")
    tabla = text[i:] if i >= 0 else text
    for m in re.finditer(r"^(\d{2})/(\d{2})/(\d{4})[ \t]*\n+[ \t]*(\S.*?)[ \t]*\n+[ \t]*(-?[\d,]+\.\d{2})[ \t]*$", tabla, re.M):
        movs.append({"fecha": iso(m.group(1), m.group(2), m.group(3)), "desc": m.group(4).strip(),
                     "monto": money(m.group(5))})
    d["movs"] = movs
    return d
