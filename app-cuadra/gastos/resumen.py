"""Lo que Titular ve en la página (Neto, Liquidez, Por cobrar, tarjetas, cuentas), para el conector (22 nov 2026).

NO calcula nada: el cálculo vive en UN solo lugar, `static/calculo.js`, el mismo archivo que usa la página. Aquí solo se leen los
datos de la base, se le pasan a `node` (`calculo_cli.js`) y se da formato al texto. Solo lee: no escribe en la base.
"""
import json
import os
import subprocess
import unicodedata
from datetime import date, datetime
from zoneinfo import ZoneInfo

AQUI = os.path.dirname(os.path.abspath(__file__))


def calcular(db, hoy=None):
    hoy = hoy or datetime.now(ZoneInfo("America/Panama")).date()
    datos = {"saldos": db.all("saldos"), "movs": db.all("movs"), "cxc": db.all("cxc"), "movil": db.all("movil"), "hoy": hoy.isoformat()}
    p = subprocess.run(["node", os.path.join(AQUI, "calculo_cli.js")], input=json.dumps(datos), capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError("No pude calcular el resumen: " + (p.stderr.strip().splitlines() or ["error de node"])[-1])
    r = json.loads(p.stdout)
    r["hoy"] = date.fromisoformat(r["hoy"])
    for t in r["tarjetas"]:
        for k in ("corte", "pago"):
            if t[k]:
                t[k] = {"fecha": date.fromisoformat(t[k]["fecha"]), "n": t[k]["n"]}
    r["por_cobrar_personas"] = [tuple(x) for x in r["por_cobrar_personas"]]
    return r


def _m(v):
    if v is None:
        return "sin dato"
    return ("−" if v < -0.004 else "") + f"${abs(v):,.2f}"


def _norm(t):
    return unicodedata.normalize("NFD", str(t or "").lower()).encode("ascii", "ignore").decode().strip()


def _coincide(filtro, *campos):
    f = _norm(filtro)
    return not f or f in _norm(" ".join(str(c) for c in campos))


def _cuando(o, hoy):
    if not o:
        return "sin dato"
    n = o["n"]
    return f"{o['fecha'].day} {['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'][o['fecha'].month-1]} ({'hoy' if n == 0 else 'mañana' if n == 1 else f'en {n} días'})"


def texto_resumen(r):
    l = [f"Resumen al {r['hoy'].isoformat()} (los mismos números de la pantalla de inicio):",
         f"Neto: {_m(r['neto'])}  (Liquidez + Por cobrar − Debo en tarjetas)",
         f"Liquidez (plata en cuentas que cuentan): {_m(r['liquidez'])}",
         f"Por cobrar: {_m(r['por_cobrar'])}" + (f"  (incluye {_m(r['movil'])} de +Móvil sin pagar)" if r["movil"] else ""),
         f"Debo en tarjetas: {_m(r['deuda_tarjetas'])}",
         f"Gasto del mes ({r['mes']}, bolsa Titular, sin pagos a tarjetas): {_m(r['gasto_mes'])}"]
    if r["por_cobrar_personas"]:
        l.append("Quién debe: " + "; ".join(f"{n} {_m(v)}" for n, v in r["por_cobrar_personas"]))
    return "\n".join(l)


def texto_tarjetas(r, filtro=""):
    ts = [t for t in r["tarjetas"] if _coincide(filtro, t["banco"], t["nombre"], t["ultimos4"], " ".join(t["medios"]))]
    if not ts:
        return "No encontré esa tarjeta."
    ts.sort(key=lambda t: (t["pago"]["n"] if t["pago"] else 9999))
    out = []
    for t in ts:
        l = [f"{t['banco']} {t['nombre']} ••{t['ultimos4']}: debes {_m(t['deuda'])}"]
        if t["limite"]:
            if t["compartida"] and t["grupo_deuda"] is not None:
                l.append(f"  Límite compartido {t['grupo']} {_m(t['limite'])} · usado por el grupo {_m(t['grupo_deuda'])} · disponible {_m(t['limite'] - t['grupo_deuda'])}")
            elif t["deuda"] is not None:
                l.append(f"  Límite {_m(t['limite'])} · disponible {_m(t['limite'] - t['deuda'])} · usado {round(t['deuda'] / t['limite'] * 100)}%")
        l.append(f"  Corte: {_cuando(t['corte'], r['hoy'])}")
        l.append("  Pago: " + ("sin pago este corte" if t["sin_pago"] else _cuando(t["pago"], r["hoy"])))
        if t["saldo_corte"] is not None:
            l.append(f"  Pago de contado del último corte: {_m(t['saldo_corte'])}" + (f" · mínimo {_m(t['pago_minimo'])}" if t["pago_minimo"] is not None else ""))
        l.append(f"  Actualizado: {t['actualizado'].replace('T', ' ')}")
        out.append("\n".join(l))
    return "\n".join(out)


def texto_cuentas(r, filtro=""):
    cs = [c for c in r["cuentas"] if _coincide(filtro, c["banco"], c["nombre"], " ".join(c["medios"]))]
    if not cs:
        return "No encontré esa cuenta."
    out, banco = [], None
    for c in cs:
        if c["banco"] != banco:
            banco = c["banco"]
            out.append(f"{banco}:")
        nota = "" if c["cuenta"] else f"  (NO cuenta en la liquidez{': ' + c['nota'] if c['nota'] else ''})"
        out.append(f"  {c['nombre']} ({', '.join(c['medios'])}): {_m(c['saldo'])}{nota}")
    out.append(f"Total que cuenta en liquidez: {_m(sum(c['saldo'] for c in r['cuentas'] if c['cuenta']))}")
    return "\n".join(out)
