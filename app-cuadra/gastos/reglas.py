"""Reglas de negocio: aplicar lo que sale de los correos a la base de datos."""
import hashlib
import re
from datetime import timedelta

from . import config as C
from .util import ahora_panama, centavos, dias_entre, fecha_de, limpiar_comercio, nombre_persona, norm

ENTRANTES = ("Yappy recibido", "ACH recibido")


def es_entrante(d):
    return d.get("tipo") in ENTRANTES


# ---------------- avisos ("Por revisar") ----------------
def avisar(db, texto, tipo="revisar", clave=None):
    clave = clave or texto
    id = "av-" + hashlib.sha1(clave.encode()).hexdigest()[:12]
    if db.exists("avisos", id):
        return
    db.set("avisos", id, {"fecha": ahora_panama().strftime("%Y-%m-%dT%H:%M"), "texto": texto, "tipo": tipo, "visto": False})


# ---------------- equivalencia de medios ----------------
def clave_medio(medio):
    m = (medio or "").strip()
    if m == "Yappy":
        return ("BG", "1111")
    banco = m.split(" ")[0] if m else ""
    ult = re.search(r"(\d{4})$", m)
    return (banco, ult.group(1) if ult else m)


def misma_tx(a, b):
    """Mismo monto, mismo medio y fecha cercana. Las tarjetas a veces se asientan 2-3 días después."""
    try:
        if clave_medio(a["medio"]) != clave_medio(b["medio"]) or es_entrante(a) != es_entrante(b):
            return False
        if abs(abs(float(a["monto"])) - abs(float(b["monto"]))) >= 0.005:
            return False
        tarjeta = a.get("tipo") in ("Compra", "Abono") and b.get("tipo") in ("Compra", "Abono")
        return dias_entre(a["fecha"], b["fecha"]) <= (3 if tarjeta else 1)
    except Exception:
        return False


def movs_cerca(db, fecha, dias=10):
    f0 = (fecha_de(fecha) - timedelta(days=dias)).isoformat()
    return db.where("movs", "fecha", ">=", f0)


# ---------------- movimientos que llegan por correo ----------------
# ---------------- Deuda de adicionales (Ana / Beatriz) ----------------
CXC_DE = {"Ana": "ana", "Beatriz": "beatriz"}   # quien -> id en cxc


def _cxc_sumar(db, pid, valor, nota, log):
    """Agrega una partida (+ deuda, - abono) a la cuenta por cobrar de pid."""
    p = db.get("cxc", pid)
    if not p:
        avisar(db, f"No existe la cuenta por cobrar '{pid}' para anotar ${valor:.2f}.", clave=f"cxc-falta-{pid}")
        return False
    partidas = list(p.get("partidas") or []) + [round(valor, 2)]
    db.merge("cxc", pid, {"partidas": partidas, "nota": ((p.get("nota") or "") + " " + nota).strip()})
    log.append(f"CxC {p.get('nombre')}: {'+' if valor > 0 else ''}{valor:.2f} ({nota})")
    return True


def _cxc_quitar(db, pid, valor):
    """Quita una partida igual a valor (si existe). Devuelve True si la quitó."""
    p = db.get("cxc", pid)
    if not p:
        return False
    partidas = list(p.get("partidas") or [])
    for i, x in enumerate(partidas):
        if abs(float(x) - valor) < 0.005:
            partidas.pop(i)
            db.merge("cxc", pid, {"partidas": partidas})
            return True
    return False


def deuda_adicional(db, id, mov, log):
    """Compra (o devolución) con tarjeta adicional de Ana/Beatriz desde el corte -> se suma (o resta) a lo que debe."""
    quien = mov.get("quien")
    pid = CXC_DE.get(quien)
    t = C.TARJETAS.get(mov.get("medio") or "")
    if not pid or not t or t[0] != quien or mov.get("cxcAgregado") or mov.get("reembolso"):
        return
    if (mov.get("fecha") or "") < C.CORTE or mov.get("tipo") not in ("Compra", "Abono"):
        return
    valor = float(mov.get("monto") or 0) * (1 if mov["tipo"] == "Compra" else -1)
    if _cxc_sumar(db, pid, valor, f"{mov['tipo']} {mov.get('comercio')} {mov['fecha']} ${abs(valor):.2f}.", log):
        db.merge("movs", id, {"cxcAgregado": True})


def aplicar_mov(db, mov, log):
    id = mov.pop("id")
    if db.exists("movs", id):
        return False
    for d in movs_cerca(db, mov["fecha"], 3):
        if d.get("origen") == "banco" and misma_tx(d, mov):
            log.append(f"Ya estaba por el banco: {mov['comercio']} ${mov['monto']}")
            return False
    if mov.get("movil"):
        asegurar_mes_movil(db, mov["mes"])
    mov.pop("movil", None)
    db.set("movs", id, mov)
    log.append(f"Nuevo: {mov['fecha']} {mov['comercio']} ${mov['monto']:.2f} ({mov['medio']})")
    if mov.get("medio", "").startswith(("BG cuenta",)):
        avisar(db, f"Transferencia de Banco General sin cuenta debitada: {mov['comercio']} ${mov['monto']:.2f} el {mov['fecha']}.")
    if mov["quien"] == "Por confirmar" and mov.get("tipo") == "Compra":
        avisar(db, f"Tarjeta desconocida {mov['medio']}: {mov['comercio']} ${mov['monto']:.2f} el {mov['fecha']}.")
    deuda_adicional(db, id, mov, log)
    if mov.get("tipo") == "Yappy recibido":
        cruzar_cobro(db, id, mov, log)
    return True


def cruzar_cobro(db, yid, y, log):
    """Yappy recibido -> reembolso de adicional, cobro +Móvil o cuenta por cobrar."""
    valor = round(float(y["monto"]), 2)
    # 1) reembolso exacto de una compra de su tarjeta adicional
    if y["quien"] in ("Ana", "Beatriz"):
        f = fecha_de(y["fecha"])
        cands = [d for d in movs_cerca(db, y["fecha"], 3)
                 if d.get("quien") == y["quien"] and not es_entrante(d) and not d.get("reembolso")
                 and abs(float(d.get("monto") or 0) - valor) < 0.005
                 and f - timedelta(days=3) <= fecha_de(d["fecha"]) <= f]
        if len(cands) == 1:
            d = cands[0]
            db.merge("movs", d["id"], {"reembolso": {"de": y["quien"], "monto": valor, "ref": yid}})
            db.merge("movs", yid, {"notas": f"Reembolso de {d['comercio']} (${valor:.2f})"})
            log.append(f"Reembolso: {y['quien']} pagó {d['comercio']} ${valor:.2f}")
            if d.get("cxcAgregado") and CXC_DE.get(y["quien"]):
                _cxc_quitar(db, CXC_DE[y["quien"]], valor)
            return
        if len(cands) > 1:
            avisar(db, f"Yappy de {y['quien']} por ${valor:.2f} el {y['fecha']} coincide con {len(cands)} compras; márcalo a mano.")
            return
    nombre = norm(y["comercio"])
    # 2) +Móvil
    for m in sorted(db.all("movil"), key=lambda x: x["id"]):
        cobros = m.get("cobros") or []
        for c in cobros:
            if not c.get("pagado") and c.get("alias") and norm(c["alias"]) == nombre and abs(float(c["monto"]) - valor) < 0.005:
                c["pagado"] = True
                db.merge("movil", m["id"], {"cobros": cobros})
                log.append(f"+Móvil {m['id']}: pagó {c['nombre']}")
                return
    # 3) cuentas por cobrar
    posibles = [p for p in db.all("cxc") if (p.get("alias") and norm(p["alias"]) == nombre)
                or p["id"] == CXC_DE.get(y.get("quien"))]
    for p in posibles:
        partidas = list(p.get("partidas") or [])
        for i, x in enumerate(partidas):
            if abs(float(x) - valor) < 0.005:
                partidas.pop(i)
                nota = ((p.get("nota") or "") + f" Cobrado ${valor:.2f} el {y['fecha']} por Yappy.").strip()
                db.merge("cxc", p["id"], {"partidas": partidas, "nota": nota})
                log.append(f"Cobro: {p['nombre']} ${valor:.2f}")
                return
    if len(posibles) == 1:
        # no cuadra con ninguna partida: se resta como abono
        p = posibles[0]
        if _cxc_sumar(db, p["id"], -valor, f"Abono ${valor:.2f} el {y['fecha']} por Yappy.", log):
            # puede no ser un pago de su deuda (plata de otra persona, otro asunto): Titular lo revisa
            avisar(db, f"Se restaron ${valor:.2f} a {p.get('nombre')} como abono (Yappy de {y['comercio']} el {y['fecha']}). "
                       f"Si no era un pago de su deuda, abre {p.get('nombre')} en Cuentas por cobrar y borra el -{valor:.2f}.",
                   clave="abono-" + yid)
    elif posibles:
        avisar(db, f"Posible cobro: {y['comercio']} mandó ${valor:.2f} el {y['fecha']} y coincide con varias personas. Ajústalo a mano.")


# ---------------- +Móvil ----------------
def asegurar_mes_movil(db, mes, vence=None):
    if db.exists("movil", mes):
        if vence:
            cur = db.get("movil", mes)
            if not cur.get("vence"):
                db.merge("movil", mes, {"vence": vence})
        return False
    previos = sorted([m for m in db.all("movil") if m["id"] < mes], key=lambda x: x["id"])
    if not previos:
        avisar(db, f"No hay mes anterior de +Móvil para copiar las líneas de {mes}.")
        return False
    cobros = [{**c, "pagado": False} for c in previos[-1].get("cobros", [])]
    db.set("movil", mes, {"mes": mes, "vence": vence or "", "cobros": cobros})
    return True


# ---------------- Bancos (lector de la Raspberry) ----------------
def _nombre_yappy(c):
    m = re.search(r"YAPPY BG (?:DE|A)\s+(.+?)(?:\s+POR\b.*|\s+\(.*)?$", c)
    return m.group(1).strip() if m else c


def clasificar_banco(m):
    """Movimiento del JSON del banco -> documento movs (sin id)."""
    medio = C.ALIAS_MEDIOS.get(m["medio"], m["medio"])
    raw = (m.get("comercio") or "").strip()
    tipo = m.get("tipo") or "Compra"
    valor = round(float(m["monto"]), 2)
    d = dict(fecha=m["fecha"], hora=m.get("hora") or "00:01", mes=m["fecha"][:7], monto=valor, medio=medio,
             tipo=tipo, quien="Titular", bolsa="Titular", factura="Pendiente", notas="", origen="banco")
    up = raw.upper()
    propias = any(n in raw for n in C.CUENTAS_PROPIAS) or "TITULAR Demo" in up
    if "RETIRO" in up and ("ATM" in up or "CAJERO" in up or "CLAVE" in up):
        d.update(tipo="Retiro cajero", comercio="Retiro cajero ATM", bolsa="Por confirmar", factura="No aplica")
    elif "PLAN SALDOS" in up or "INTERES" in up or "CARGO POR" in up or "COMISION" in up:
        d.update(tipo="Cargo banco", comercio=limpiar_comercio(re.sub(r"\d{4}-\d{2}\*+.*$", "", raw)) or "Cargo del banco",
                 factura="No aplica")
        if "PLAN SALDOS" in up:
            d["comercio"] = "Plan Saldos Deudores"
    elif "YAPPY BG" in up:
        nombre = _nombre_yappy(raw).title()
        d["medio"] = "Yappy"
        if tipo == "Yappy recibido" or " DE " in up.split("YAPPY BG")[1][:4]:
            p = nombre_persona(nombre, C.NOMBRES_PERSONA)
            d.update(tipo="Yappy recibido", comercio=nombre, quien=p or nombre, bolsa="Ingreso", factura="No aplica")
        else:
            d.update(tipo="Yappy pago" if up.startswith("PAGO YAPPY") else "Yappy enviado", comercio=nombre,
                     bolsa="Por confirmar", factura="No aplica")
    elif tipo == "ACH recibido":
        nom = re.sub(r"^(TEF DE:|TRANSFERENCIA DE)\s*", "", raw, flags=re.I)
        d.update(comercio=raw.title() if propias else nom.title(), quien="Titular" if propias else nom.title(),
                 bolsa="Ingreso", factura="No aplica")
    elif tipo == "ACH enviado":
        com = re.sub(r"^BANCA MOVIL\s+", "", raw, flags=re.I)
        d.update(comercio=com.title()[:60], bolsa="Por confirmar", factura="No aplica")
    elif tipo == "Abono" or valor < 0:
        d.update(tipo="Abono", comercio="Abono " + limpiar_comercio(re.sub(r"^(Credit|Compra)\s+(USA|LUX|IRL)?\s*", "", raw)),
                 factura="No aplica", monto=-abs(valor))
    else:
        com = re.sub(r"^(Compra|COMPRA)\s*(\(RETAIL\))?\s*(USA|LUX|IRL|PAN)?\s*(\d{10}\s+)?", "", raw)
        com = re.sub(r"Amzn\.com/bill\s+", "", com)
        q, b = C.TARJETAS.get(medio, ("Por confirmar", "Por confirmar"))
        d.update(comercio=limpiar_comercio(com), quien=q, bolsa=b)
    if propias:
        d.update(bolsa="Excluido", factura="No aplica", quien="Titular")
    return d


def aplicar_bancos(db, data, desde, log):
    """data = JSON de PI-BANCOS; desde = 'YYYY-MM-DDTHH:MM' (hora de recepción en Panamá)."""
    fecha_rep = data.get("fecha") or desde[:10]
    saldos_tocados = []
    for b in data.get("bancos", []):
        for x in (b.get("cuentas") or []) + (b.get("tarjetas") or []):
            sid = x.get("id")
            if not sid or sid in C.SALDOS_INTOCABLES or x.get("titular") or x.get("principal"):
                continue
            if not db.exists("saldos", sid):
                avisar(db, f"El lector de bancos trajo una cuenta que no existe en la app: {sid}.", clave="saldo-" + sid)
                continue
            patch = {k: x[k] for k in ("saldo", "deuda", "disponible", "corteDia", "pagoDia", "pagoMin", "saldoCorte", "transito") if k in x}
            patch["desde"] = desde
            db.merge("saldos", sid, patch)
            saldos_tocados.append(sid)
    log.append(f"Saldos actualizados: {len(saldos_tocados)}")

    # Movimientos
    usados = set()
    nuevos = 0
    todos = [clasificar_banco(m) for b in data.get("bancos", []) for m in (b.get("movimientos") or [])]
    if todos:
        f0 = min(m["fecha"] for m in todos)
        existentes = movs_cerca(db, f0, 2)
    else:
        existentes = []
    for m in sorted(todos, key=lambda x: (x["fecha"], x["monto"])):
        dup = None
        for e in existentes:
            if e["id"] not in usados and misma_tx(e, m):
                dup = e
                break
        if dup:
            usados.add(dup["id"])
            continue
        # gasolinera: la alerta trae la preautorización y el banco el monto final
        if "PUMA" in m["comercio"].upper():
            cand = [e for e in existentes if e["id"] not in usados and e.get("origen") == "correo"
                    and clave_medio(e["medio"]) == clave_medio(m["medio"]) and dias_entre(e["fecha"], m["fecha"]) <= 1
                    and "puma" in (e.get("comercio") or "").lower()]
            if len(cand) == 1:
                e = cand[0]
                db.merge("movs", e["id"], {"monto": m["monto"], "preaut": None,
                                            "notas": f"Alerta del banco por ${float(e['monto']):.2f} (preautorización); monto final según el banco."})
                usados.add(e["id"])
                log.append(f"Puma ajustado: ${e['monto']} -> ${m['monto']}")
                continue
        base = f"pi-{m['fecha'].replace('-', '')}-{centavos(m['monto'])}-"
        n = 1
        while db.exists("movs", base + str(n)):
            n += 1
        db.set("movs", base + str(n), m)
        existentes.append({**m, "id": base + str(n)})
        usados.add(base + str(n))
        nuevos += 1
        deuda_adicional(db, base + str(n), m, log)
        if m["tipo"] == "Yappy recibido":
            cruzar_cobro(db, base + str(n), m, log)
    log.append(f"Movimientos nuevos del banco: {nuevos}")

    # Transacciones en tránsito (flotantes)
    hoy = fecha_de(fecha_rep)
    for sid in saldos_tocados:
        s = db.get("saldos", sid)
        medios = s.get("medios") or []
        pend = []  # compras del correo que todavía no salen procesadas en el banco
        for d in db.where("movs", "fecha", ">=", (hoy - timedelta(days=14)).isoformat()):
            if d.get("origen") != "correo" or d.get("medio") not in medios:
                continue
            stamp = f"{d['fecha']}T{d.get('hora') or '23:59'}"
            if stamp > desde:
                continue
            if d["id"] in usados:
                if d.get("flotante") or d.get("enTransito"):
                    db.merge("movs", d["id"], {"flotante": None, "enTransito": None})
            else:
                pend.append(d)
        en_banco = emparejar_transito(pend, s.get("transito") or [])
        corte = ultimo_corte(hoy, s.get("corteDia")) if sid.startswith("bac-") else None
        for d in pend:
            edad = (hoy - fecha_de(d["fecha"])).days
            if d["id"] in en_banco:  # 1) el banco la muestra en tránsito/flotante: sigue en camino, sin aviso
                if not d.get("flotante") or not d.get("enTransito"):
                    db.merge("movs", d["id"], {"flotante": True, "enTransito": True})
            elif corte and fecha_de(d["fecha"]) <= corte:
                # 2) BAC solo muestra lo posterior al último corte. Si no está en tránsito ni en la lista, quedó en el
                #    estado de cuenta (incluye compras del mismo día del corte hechas después de la hora del corte).
                if d.get("flotante") or d.get("enTransito"):
                    db.merge("movs", d["id"], {"flotante": None, "enTransito": None})
            elif edad > 7:
                if d.get("flotante"):
                    db.merge("movs", d["id"], {"flotante": None})
                    avisar(db, f"{d['comercio']} ${float(d['monto']):.2f} del {d['fecha']} ({d['medio']}) nunca apareció en el banco. ¿Preautorización o cargo cancelado?")
            elif not d.get("flotante"):
                db.merge("movs", d["id"], {"flotante": True})


def ultimo_corte(hoy, dia):
    """Fecha del último corte (dia del mes) en o antes de hoy; None si no se sabe."""
    try:
        dia = int(dia)
    except (TypeError, ValueError):
        return None
    import calendar
    y, m = hoy.year, hoy.month
    if hoy.day < dia:
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return hoy.replace(year=y, month=m, day=min(dia, calendar.monthrange(y, m)[1]))


def emparejar_transito(movs, transito):
    """Empareja compras del correo con las 'en tránsito' que muestra el banco (BAC/Banesco).
    Primero monto exacto; luego el más cercano (el monto cambia mientras está en proceso: 8.61 -> 8.27),
    hasta 15% o $2 de diferencia, con fecha a ±3 días. Cada tránsito se usa una sola vez. Devuelve ids."""
    libres = [dict(t, _i=i) for i, t in enumerate(transito)]
    hechos = set()
    for exacto in (True, False):
        for d in sorted(movs, key=lambda x: x["id"]):
            if d["id"] in hechos:
                continue
            v = abs(float(d.get("monto") or 0))
            cands = []
            for t in libres:
                tv = abs(float(t.get("monto") or 0))
                try:
                    dias = abs((fecha_de(t["fecha"]) - fecha_de(d["fecha"])).days)
                except Exception:
                    continue
                dif = abs(tv - v)
                if dias <= 3 and (dif < 0.005 if exacto else dif <= max(2.0, 0.15 * v)):
                    cands.append((dif, dias, t))
            if cands:
                cands.sort(key=lambda c: (c[0], c[1]))
                libres.remove(cands[0][2])
                hechos.add(d["id"])
    return hechos


# ---------------- Nota "Cuentas x Cobrar" ----------------
_NUM = re.compile(r"^\$?-?[\d,]+(?:\.\d+)?\$?$")  # 100 / 1,000.00 / $3 / 3$


BORRAR_MAX = 3  # personas que la nota puede borrar de una sola vez


def leer_nota(texto):
    """Devuelve {nombre: [montos]} del bloque 'Nombre Monto'. Ignora todo lo demás."""
    lineas = [l.replace("\ufffc", "").replace("\u00a0", " ").strip() for l in texto.splitlines()]
    try:
        i = next(k for k, l in enumerate(lineas) if norm(l) == "nombre monto")
    except StopIteration:
        return None
    out = {}
    for l in lineas[i + 1:]:
        if not l:
            continue
        if norm(l).startswith("fecha monto") or norm(l).startswith("dinero"):
            break
        partes = l.split()
        nombre, montos = [], []
        for p in partes:
            if _NUM.match(p) and nombre:
                montos.append(round(float(p.replace("$", "").replace(",", "")), 2))
            elif not montos:
                nombre.append(p)
            else:
                break  # texto después de los montos = etiqueta de la sección siguiente
        if nombre and montos:
            out[" ".join(nombre)] = montos
        if montos and len(partes) > len(nombre) + len(montos):
            resto = " ".join(partes[len(nombre) + len(montos):])
            if norm(resto).startswith("dinero"):
                break
    return out


def aplicar_nota(db, texto, hoy, log):
    bloque = leer_nota(texto)
    if not bloque:
        avisar(db, "Llegó la nota de Cuentas x Cobrar pero no encontré el bloque 'Nombre Monto'.", clave="nota-" + hoy)
        return False
    actuales = db.all("cxc")
    ayer = (fecha_de(hoy) - timedelta(days=1)).isoformat()
    vistos = set()
    for nombre, montos in bloque.items():
        n = norm(nombre)
        p = next((x for x in actuales if norm(x.get("nombre")) == n or (x.get("alias") and norm(x["alias"]) == n)), None)
        if p is None:
            pid = re.sub(r"[^a-z0-9]+", "-", n).strip("-") or "persona"
            db.set("cxc", pid, {"nombre": nombre, "partidas": montos, "alias": "", "nota": "", "origen": "nota"})
            log.append(f"CxC nueva: {nombre} {montos}")
            vistos.add(pid)
            continue
        vistos.add(p["id"])
        if p.get("origen") != "nota":  # esta en la nota: desde ahora la nota manda sobre esta persona
            db.merge("cxc", p["id"], {"origen": "nota"})
        # no reponer una partida cobrada ayer u hoy (la nota puede ir atrasada)
        cobradas = re.findall(r"Cobrado \$([\d.]+) el (\d{4}-\d{2}-\d{2})", p.get("nota") or "")
        recientes = [round(float(v), 2) for v, f in cobradas if f in (hoy, ayer)]
        nuevas = list(montos)
        for v in recientes:
            if v in nuevas and v not in (p.get("partidas") or []):
                nuevas.remove(v)
        if sorted(nuevas) != sorted([round(float(x), 2) for x in (p.get("partidas") or [])]):
            db.merge("cxc", p["id"], {"partidas": nuevas})
            log.append(f"CxC {p['nombre']}: {p.get('partidas')} -> {nuevas}")
    faltan = [x for x in actuales if x["id"] not in vistos]
    borrar = [x for x in faltan if x.get("origen") == "nota"]
    if len(borrar) > BORRAR_MAX:  # demasiados de golpe: puede ser una nota mal leida; no borrar nada
        avisar(db, f"Tu nota ya no tiene a {len(borrar)} personas ({', '.join(x['nombre'] for x in borrar)}). "
                   "Son muchas a la vez y no borré ninguna: bórralas a mano si es correcto.",
               clave="nota-muchos-" + "-".join(sorted(x["id"] for x in borrar)))
        borrar = []
    for x in borrar:
        db.delete("cxc", x["id"])
        log.append(f"CxC borrada (ya no está en la nota): {x['nombre']} {x.get('partidas')}")
    for x in faltan:
        if x not in borrar and sum(x.get("partidas") or []) > 0:
            avisar(db, f"{x['nombre']} está en la app con ${sum(x['partidas']):.2f} pero ya no aparece en tu nota.",
                   clave=f"nota-falta-{x['id']}-{sum(x['partidas'])}")
    return True


# ---------------- Facturas que llegan por correo ----------------
def aplicar_factura_correo(db, info, log):
    for total in info["totales"]:
        cands = [d for d in movs_cerca(db, info["fecha"], 3)
                 if d.get("factura") == "Pendiente" and not d.get("facturaCorreo") and not d.get("facturaId")
                 and abs(float(d.get("monto") or 0) - total) < 0.005 and dias_entre(d["fecha"], info["fecha"]) <= 2]
        if len(cands) == 1:
            db.merge("movs", cands[0]["id"], {"facturaCorreo": f"{info['asunto']} ({info['remitente']})",
                                                "facturaCorreoUrl": info["url"]})
            log.append(f"Posible factura por correo para {cands[0]['comercio']} ${total:.2f}")
