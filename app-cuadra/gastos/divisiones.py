"""Dividir una transacción entre personas (23 nov 2026, idea de Titular).

Capa 1 (la verdad): la transacción principal guarda `division = {"partes": [{"id": "beatriz", "pct": 33.33}, ...]}`.
   Los porcentajes son de la transacción; lo que no se reparte (100 - suma) es de Titular y no crea movimiento.
Capa 2 (esto): `sincronizar` crea/actualiza/borra UN movimiento por persona con el mismo formato de siempre
   (medio «Por cobrar», bolsa «Excluido», `cxc` hacia la persona, igual que `asistente debe --mov`). Corre dentro de
   `reglas.recalcular_cxc`, que ya se llama en cada lectura del banco, en cada guardado de la página y en el asistente:
   si el banco cambia el monto (preautorización $20 -> $14.31) o la compra «no se asentó», las partes la siguen solas.
   El resto de la app (Por cobrar, Neto, conciliación) solo ve movimientos normales y no sabe que existen divisiones.
Capa 3: el botón «Dividir» de la página (solo pantalla; guarda `division`).

Las partes llevan `divDe` = id de la principal. No se editan a mano (web.py lo impide): se cambian desde la principal.
"""

ID_PREFIJO = "dv-"


class ErrorDivision(Exception):
    pass


def id_parte(mov_id, pid):
    return f"{ID_PREFIJO}{mov_id}-{pid}"


def repartir(personas, fijos=None, titular=None):
    """Porcentajes de cada persona (espejo de divRepartir en la página). `personas` = ids que participan; `fijos` = {id: pct}
    que alguien fijó; `titular` = pct que Titular fijó para sí (None = lo que sobre). Titular cuenta como un participante más: con 3 en
    total (Titular + 2) toca 33.33 % a cada uno. Lo fijado se respeta y el resto se reparte en partes iguales entre los demás."""
    fijos = {k: float(v) for k, v in (fijos or {}).items() if k in personas}
    libres = [p for p in personas if p not in fijos]
    n_libres = len(libres) + (1 if titular is None else 0)
    resto = 100.0 - sum(fijos.values()) - (0.0 if titular is None else float(titular))
    if n_libres == 0:
        if abs(resto) > 0.01:
            raise ErrorDivision("Los porcentajes no suman 100 %.")
    elif resto < -0.005:
        raise ErrorDivision("Los porcentajes fijados pasan de 100 %.")
    cada = max(resto, 0.0) / n_libres if n_libres else 0.0
    return {p: round(fijos[p] if p in fijos else cada, 4) for p in personas}


def validar(db, mov_id, mov, division):
    """Revisa una `division` antes de guardarla. Devuelve la división limpia (o None para quitarla)."""
    if not division or not division.get("partes"):
        return None
    if mov.get("cxc"):
        raise ErrorDivision("Esta transacción ya está marcada «Por cobrar» entera; quítalo antes de dividirla (si no, se cuenta doble).")
    if mov.get("tipo") != "Compra":
        raise ErrorDivision("Solo se dividen las compras.")
    if mov.get("divDe"):
        raise ErrorDivision("Esto ya es una parte de otra división.")
    vistos, partes, suma = set(), [], 0.0
    for p in division["partes"]:
        pid = p.get("id")
        try:
            pct = round(float(p.get("pct")), 4)
        except (TypeError, ValueError):
            raise ErrorDivision("Porcentaje inválido.")
        if not db.exists("cxc", pid):
            raise ErrorDivision(f"No conozco a «{pid}» en Por cobrar.")
        if pid in vistos:
            raise ErrorDivision("Una persona aparece dos veces.")
        if pct <= 0:
            raise ErrorDivision("Cada porcentaje debe ser mayor que 0.")
        vistos.add(pid)
        suma += pct
        partes.append({"id": pid, "pct": pct})
    if suma > 100.005:
        raise ErrorDivision(f"Los porcentajes suman {suma:.2f} %, más de 100.")
    return {"partes": partes}


def montos(monto, partes):
    """{id: monto en dólares}. Los centavos que sobran por redondeo van a la última persona, así lo repartido es exacto."""
    total = abs(float(monto or 0))
    suma_pct = sum(p["pct"] for p in partes)
    objetivo = round(total * suma_pct / 100, 2)
    out, acum = {}, 0.0
    for i, p in enumerate(partes):
        v = objetivo - acum if i == len(partes) - 1 else round(total * p["pct"] / 100, 2)
        v = round(v, 2)
        out[p["id"]] = v
        acum = round(acum + v, 2)
    return out


def _parte(padre_id, padre, pid, nombre, pct, monto):
    return {
        "fecha": padre.get("fecha"), "hora": padre.get("hora") or "00:00", "mes": (padre.get("fecha") or "")[:7],
        "monto": monto, "medio": "Por cobrar", "tipo": "Compra",
        "comercio": f"{nombre} debe: {padre.get('comercio') or ''}".strip(),
        "quien": "Titular", "bolsa": "Excluido", "factura": "No aplica",
        "notas": f"{pct:g} % del movimiento {padre_id} [división]",
        "origen": "division", "cxc": {"id": pid, "signo": 1}, "divDe": padre_id,
        **({"caido": True} if padre.get("caido") else {}),
    }


def sincronizar(db, avisar=None):
    """Deja las partes iguales a lo que dicen las transacciones principales. Solo escribe si algo cambió."""
    movs = {m["id"]: m for m in db.all("movs")}
    nombres = {p["id"]: (p.get("nombre") or p["id"]) for p in db.all("cxc")}
    deseadas = {}
    for mid, m in movs.items():
        div = m.get("division")
        if not div or not div.get("partes") or m.get("divDe"):
            continue
        if m.get("cxc"):   # se marcó «Por cobrar» entera después de dividir: no se cuenta doble
            if avisar:
                avisar(db, f"«{m.get('comercio')}» está dividida y también marcada «Por cobrar» entera. "
                           f"Se pausaron sus partes; quita una de las dos.", clave="div-cxc-" + mid)
            continue
        partes = [p for p in div["partes"] if p.get("id") in nombres and isinstance(p.get("pct"), (int, float)) and p["pct"] > 0]
        if not partes:
            continue
        for pid, v in montos(m.get("monto"), partes).items():
            pct = next(p["pct"] for p in partes if p["id"] == pid)
            deseadas[id_parte(mid, pid)] = _parte(mid, m, pid, nombres[pid], pct, v)
    # existentes que sobran (principal borrada, sin división, o persona quitada)
    for mid, m in movs.items():
        if m.get("divDe") and mid not in deseadas:
            db.delete("movs", mid)
    for mid, doc in deseadas.items():
        actual = movs.get(mid)
        if actual is None:
            db.set("movs", mid, doc)
        else:
            cambios = {k: v for k, v in doc.items() if actual.get(k) != v}
            if "caido" not in doc and actual.get("caido"):
                cambios["caido"] = None   # la principal revivió
            if cambios:
                db.merge("movs", mid, cambios)
