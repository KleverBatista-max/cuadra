"""Reglas de negocio: aplicar lo que sale de los correos a la base de datos."""
import hashlib
import re
from datetime import datetime, timedelta
from itertools import combinations

from . import config as C
from .util import ahora_panama, centavos, dias_entre, fecha_de, limpiar_comercio, nombre_persona, norm

ENTRANTES = ("Yappy recibido", "ACH recibido")


def es_entrante(d):
    return d.get("tipo") in ENTRANTES


# ---------------- avisos ("Por revisar") ----------------
def nombre_tarjeta(s, sid):
    """Nombre para los avisos con el banco (hay dos Mastercard: BAC y Global): «Mastercard (BAC)»."""
    nom = (s or {}).get("nombre") or sid
    banco = (s or {}).get("banco")
    return f"{nom} ({banco})" if banco and banco not in nom else nom


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


def mismo_nombre(x, y):
    """Nombres compatibles: alguna palabra (3+ letras) de uno aparece en el otro, ignorando espacios.
    Si falta un nombre, se da por compatible. "LASGARNACHASDELCARNAL" ~ "Las Garnachas del Carnal"."""
    a, b = norm(x or ""), norm(y or "")
    if not a or not b:
        return True
    ja, jb = re.sub(r"[^a-z0-9]", "", a), re.sub(r"[^a-z0-9]", "", b)
    return any(len(w) >= 3 and w in jb for w in re.findall(r"[a-z0-9]+", a)) or \
        any(len(w) >= 3 and w in ja for w in re.findall(r"[a-z0-9]+", b))


_COMUNES = {"panama", "compra", "tienda", "store", "super", "inc", "corp", "plaza", "mall", "city", "the", "del", "los", "las"}


def mismo_comercio(x, y):
    """Comercio del correo vs. del banco: comparten una palabra propia (4+ letras, no genérica) o uno contiene al otro."""
    a, b = norm(x or ""), norm(y or "")
    if not a or not b:
        return False
    pa = {w for w in re.findall(r"[a-z0-9]+", a) if len(w) >= 4 and w not in _COMUNES}
    pb = {w for w in re.findall(r"[a-z0-9]+", b) if len(w) >= 4 and w not in _COMUNES}
    ja, jb = re.sub(r"[^a-z0-9]", "", a), re.sub(r"[^a-z0-9]", "", b)
    return bool(pa & pb) or any(w in jb for w in pa) or any(w in ja for w in pb)


def misma_tx(a, b, nombre=True):
    """Mismo monto, mismo medio y fecha cercana. Las tarjetas a veces se asientan 2-3 días después."""
    try:
        if clave_medio(a["medio"]) != clave_medio(b["medio"]) or es_entrante(a) != es_entrante(b):
            return False
        if abs(abs(float(a["monto"])) - abs(float(b["monto"]))) >= 0.005:
            return False
        tarjeta = a.get("tipo") in ("Compra", "Abono") and b.get("tipo") in ("Compra", "Abono")
        if dias_entre(a["fecha"], b["fecha"]) > (3 if tarjeta else 1):
            return False
        # Yappy/ACH: mismo monto a otra persona NO es el mismo (13 nov: $10 a Gisela se perdia por $10 a Nina)
        return tarjeta or not nombre or mismo_nombre(a.get("comercio"), b.get("comercio"))
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


def saldo_cxc(items, base=0.0):
    """Lo que la persona debe por sus movimientos, en orden de fecha (23 nov 2026, decisión de Titular): si un abono se pasa de lo que
    debía, el sobrante es de Titular y se descarta en ese momento (la cuenta queda en 0, sin saldo a favor); una deuda nueva después
    empieza desde cero. En la misma fecha las deudas cuentan antes que los abonos, para que un pago del mismo día no se pierda."""
    saldo = float(base)   # `base` = lo que ya dice la nota del iPhone (casilla 1): los abonos también se le restan
    for x in sorted(items, key=lambda x: (x["fecha"] or "", 0 if x["monto"] > 0 else 1, x["mov"])):
        saldo = max(0.0, round(saldo + x["monto"], 2))
    return round(saldo, 2)


def recalcular_cxc(db):
    """Casilla 2 de cada persona: suma de los movimientos marcados (mov.cxc). No toca las partidas de la nota."""
    from . import divisiones   # las partes de las transacciones divididas se igualan primero (ver divisiones.py)
    divisiones.sincronizar(db, avisar)
    por = {}
    for m in db.all("movs"):
        c = m.get("cxc")
        if not isinstance(c, dict) or not c.get("id") or c.get("signo") not in (1, -1):
            continue
        if m.get("caido"):   # compra que no se asentó (preautorización cancelada): no es gasto ni deuda de nadie
            continue
        v = round(c["signo"] * abs(float(m.get("monto") or 0)), 2)
        por.setdefault(c["id"], []).append({"mov": m["id"], "fecha": m.get("fecha"), "comercio": m.get("comercio"), "monto": v})
    for p in db.all("cxc"):
        items = sorted(por.get(p["id"], []), key=lambda x: (x["fecha"] or "", x["mov"]))
        base = round(sum(float(v) for v in (p.get("partidas") or [])), 2)
        tot = round(saldo_cxc(items, base) - base, 2)   # casilla 2 = lo que los movimientos suman o restan a la nota
        if p.get("deMovs") != tot or (p.get("detalleMovs") or []) != items:
            db.merge("cxc", p["id"], {"deMovs": tot, "detalleMovs": items})


def marcar_cxc(db, mov_id, pid, signo, log, texto):
    db.merge("movs", mov_id, {"cxc": {"id": pid, "signo": signo}})
    recalcular_cxc(db)
    log.append(f"Por cobrar ({pid}): {texto}")


def deuda_adicional(db, id, mov, log):
    """Compra (o devolución) con tarjeta adicional de Ana/Beatriz -> se marca para su casilla de movimientos."""
    quien = mov.get("quien")
    pid = CXC_DE.get(quien)
    t = C.TARJETAS.get(mov.get("medio") or "")
    if not t and mov.get("adicionalNueva") and pid:
        # adicional que no está en config.TARJETAS pero el correo del banco dice de quién es (13 nov: BAC 6791 de Beatriz)
        t = (quien, quien)
        avisar(db, f"Tarjeta adicional nueva {mov.get('medio')} a nombre de {quien}: sus compras salen como de {quien}. "
                   f"Agrégala en gastos/config.py (TARJETAS) para que quede fija.", clave="adic-" + (mov.get("medio") or ""))
    if not C.CXC_AUTOMATICA or not pid or not t or t[0] != quien or mov.get("cxc") or mov.get("reembolso"):
        return
    if (mov.get("fecha") or "") < C.CXC_MOVS_DESDE or mov.get("tipo") not in ("Compra", "Abono"):
        return
    signo = 1 if mov["tipo"] == "Compra" else -1
    marcar_cxc(db, id, pid, signo, log, f"{mov['tipo']} {mov.get('comercio')} {mov['fecha']} ${abs(float(mov['monto'])):.2f}")


def aplicar_mov(db, mov, log):
    id = mov.pop("id")
    if db.exists("movs", id):
        return False
    # Yappy: el código de confirmación (id yp-XXXXX-NNNN) lo identifica. El banco (BG) no lo trae, así que cada
    # fila del banco se queda con UN solo código; otro Yappy igual (mismo monto y persona) con otro código es nuevo.
    conf = id[3:] if id.startswith("yp-") else None
    for d in movs_cerca(db, mov["fecha"], 3):
        if d.get("origen") == "banco" and misma_tx(d, mov):
            if conf and d.get("confirmacion") and d["confirmacion"] != conf:
                continue
            if conf:
                extra = {"confirmacion": conf}
                if d.get("hora") in (None, "", "00:01") and mov.get("hora"):
                    extra["hora"] = mov["hora"]
                db.merge("movs", d["id"], extra)
            log.append(f"Ya estaba por el banco: {mov['comercio']} ${mov['monto']}")
            return False
    if es_entrante(mov):
        # Yappy/ACH recibido: el banco pone el titular de la cuenta, el correo el nombre de Yappy (13 nov: el Yappy
        # de Ana sale en BG como "Carla Yulieth Rivera Gomez"). Si hay UNA sola fila del banco sin cruzar con ese
        # monto y fecha, es la misma (el banco nunca repite).
        sueltas = [d for d in movs_cerca(db, mov["fecha"], 3) if d.get("origen") == "banco" and not d.get("confirmacion")
                   and misma_tx(d, mov, nombre=False)]
        if len(sueltas) == 1:
            if conf:
                db.merge("movs", sueltas[0]["id"], {"confirmacion": conf})
            log.append(f"Ya estaba por el banco (otro nombre): {mov['comercio']} ${mov['monto']}")
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
    """Yappy recibido -> reembolso de adicional, cobro +Móvil o abono en la casilla de movimientos de la persona.
    Nunca toca las partidas de la nota. Titular puede cambiar la marca en el movimiento."""
    valor = round(float(y["monto"]), 2)
    # la casilla de movimientos solo cuenta desde CXC_MOVS_DESDE; +Móvil y reembolsos funcionan siempre (14 nov)
    marca_cxc = C.CXC_AUTOMATICA and (y.get("fecha") or "") >= C.CXC_MOVS_DESDE
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
            c = d.get("cxc")
            if marca_cxc and isinstance(c, dict) and c.get("signo") == 1:   # la compra estaba sumada: el reembolso la compensa
                marcar_cxc(db, yid, c["id"], -1, log, f"reembolso {d['comercio']} ${valor:.2f}")
            return
        if len(cands) > 1:
            avisar(db, f"Yappy de {y['quien']} por ${valor:.2f} el {y['fecha']} coincide con {len(cands)} compras; márcalo a mano.")
            return
    nombre = norm(y["comercio"])
    # 2) (23 nov 2026) +Móvil ya no se cruza con Yappys: es una lista de chequeo; lo que cada quien debe del plan es una deuda
    #    normal en Cuentas por cobrar (`cargar_movil`) y el Yappy entra aquí abajo como abono, igual que cualquier otro.
    # 3) abono en la casilla de movimientos (no toca la nota)
    if not marca_cxc:
        return
    posibles = [p for p in db.all("cxc") if (p.get("alias") and norm(p["alias"]) == nombre)
                or p["id"] == CXC_DE.get(y.get("quien"))]
    if len(posibles) == 1:
        p = posibles[0]
        marcar_cxc(db, yid, p["id"], -1, log, f"abono Yappy ${valor:.2f} {y['fecha']}")
        exacto = any(abs(float(x) - valor) < 0.005 for x in (p.get("partidas") or []))
        if not exacto:
            avisar(db, f"Se restaron ${valor:.2f} a {p.get('nombre')} como abono (Yappy de {y['comercio']} el {y['fecha']}). "
                       f"Si no era un pago de su deuda, abre ese movimiento y en «Por cobrar» elige «No cuenta».",
                   clave="abono-" + yid)
    elif posibles:
        avisar(db, f"Posible cobro: {y['comercio']} mandó ${valor:.2f} el {y['fecha']} y coincide con varias personas. "
                   f"Abre el movimiento y elige a quién en «Por cobrar».")


# ---------------- +Móvil ----------------
MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def cargar_movil(db, mes, mov_id=None):
    """Pasa lo que debe cada línea del mes a Cuentas por cobrar (23 nov 2026, idea de Titular): una deuda normal por persona
    («Laura debe: +Móvil diciembre»), igual que `asistente debe`; con `mov_id` queda ligada al cargo del banco. No incluye la
    línea de Titular ni las ya marcadas pagadas, y no repite: guarda en `cargados` qué línea ya pasó. El checklist de +Móvil no
    cambia. Personas que no existen en Por cobrar (Buki, Ñeñe) se crean."""
    from .asistente import Asistente, ErrorAsistente
    m = db.get("movil", mes)
    if not m:
        raise ErrorAsistente(f"No existe el mes {mes} de +Móvil.")
    a = Asistente(db)
    cargados = dict(m.get("cargados") or {})
    nombre_mes = MESES_ES[int(mes[5:7]) - 1]
    hechos = []
    for c in m.get("cobros") or []:
        n = c.get("nombre") or ""
        if not n or n == "Titular" or c.get("pagado") or n in cargados:
            continue
        try:
            p = a.persona(n)
        except ErrorAsistente:
            a.persona_nueva(n, c.get("alias") or "")
            p = a.persona(n)
        _, mid, _ = a.debe(p["id"], c["monto"], f"+Móvil {nombre_mes}", mov=mov_id)
        cargados[n] = mid
        hechos.append(n)
    db.merge("movil", mes, {"cargados": cargados})
    return hechos


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
    # la línea de Titular (la que él mismo paga) nace «pagada»: cuenta para la ganancia pero no es por cobrar
    cobros = [{**c, "pagado": c.get("nombre") == "Titular"} for c in previos[-1].get("cobros", [])]
    db.set("movil", mes, {"mes": mes, "vence": vence or "", "cobros": cobros})
    return True


# ---------------- Bancos (lector de la Raspberry) ----------------
def _nombre_yappy(c):
    m = re.search(r"YAPPY BG (?:DE|A)\s+(.+?)(?:\s+POR\b.*|\s+\(.*)?$", c)
    return m.group(1).strip() if m else c


GASOLINERA = re.compile(r"\b(puma|texaco|delta|terpel|shell)\b", re.I)


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
        k = re.match(r"KUARA DE:?\s*(.+?)(?:\s+POR\b.*)?$", raw, re.I)
        if k and not propias:   # Kuara (18 nov 2026): "KUARA DE: beatriz demo POR PAGO DE HOY" -> Kuara de Beatriz
            p = nombre_persona(k.group(1), C.NOMBRES_PERSONA)
            d.update(comercio="Kuara de " + (p or k.group(1).title()), quien=p or k.group(1).title())
            if raw.upper() != "KUARA DE: " + k.group(1).upper():
                d["notas"] = raw.title()
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
    con_transito = set()   # tarjetas cuya lista "en proceso" se leyó en esta corrida
    linea_mala = set()     # tarjetas cuya lectura no cuadró la línea: en esa lectura no se mata ninguna compra
    for b in data.get("bancos", []):
        for x in (b.get("cuentas") or []) + (b.get("tarjetas") or []):
            sid = x.get("id")
            if not sid or sid in C.SALDOS_INTOCABLES or x.get("titular") or x.get("principal"):
                continue
            if not db.exists("saldos", sid):
                avisar(db, f"El lector de bancos trajo una cuenta que no existe en la app: {sid}.", clave="saldo-" + sid)
                continue
            if isinstance(x.get("planes"), list) and x.get("financiado") is not None:
                # 19 nov 2026 (Titular): el «saldo faltante» del plan trae el interés del día (14,000 -> 14,001.56), que no se
                # cobra si se cancela antes del corte. La deuda cuenta el CAPITAL: plan sin cuotas pagadas = saldo inicial.
                # Con cuotas ya cobradas no se sabe el capital exacto: se usa el saldo faltante. Lo del banco queda aparte.
                x = dict(x, financiadoBanco=x["financiado"],
                         financiado=round(sum(float(pl.get("inicial") or 0) if not pl.get("pagadas")
                                              else float(pl.get("faltante") or 0) for pl in x["planes"]), 2))
            patch = {k: x[k] for k in ("saldo", "deuda", "disponible", "corteDia", "pagoDia", "pagoMin", "saldoCorte", "transito", "cuadre", "financiado", "financiadoBanco", "planes") if k in x}
            if x.get("al"):
                patch["disponibleAl"] = x["al"]   # Global: fecha hasta la que calculó su disponible
            patch["desde"] = desde
            db.merge("saldos", sid, patch)
            saldos_tocados.append(sid)
            if isinstance(x.get("transito"), list):   # None = el lector no pudo leer "en proceso": no se mata nada
                con_transito.add(sid)
            # Cuadre 2 (BAC y Banesco): adeudado + disponible + en tránsito = línea de la tarjeta, siempre igual.
            # La primera vez se aprende (limiteCuadre); después, si cambia, se avisa.
            if sid.startswith(("bac-", "banesco-")) and "transito" in x and x.get("deuda") is not None \
                    and x.get("disponible") is not None:
                # 19 nov 2026: + el saldo de los planes de cuotas de BAC («Financiamientos»), que baja el disponible.
                # Ese saldo trae el interés del día (14,000 -> 14,001.56) y el disponible no: se tolera hasta 1 %.
                fin = float((x.get("financiado") if "financiado" in x else db.get("saldos", sid).get("financiado")) or 0)
                linea = round(float(x["deuda"]) + float(x["disponible"]) + fin
                              + sum(abs(float(t.get("monto") or 0)) for t in (x.get("transito") or [])), 2)
                antes = db.get("saldos", sid).get("limiteCuadre")
                if antes is None:
                    db.merge("saldos", sid, {"limiteCuadre": linea})
                elif abs(linea - float(antes)) > max(0.01, fin * 0.01):
                    linea_mala.add(sid)
                    nom = nombre_tarjeta(db.get("saldos", sid), sid)
                    avisar(db, f"{nom} no cuadra: adeudado + disponible + en tránsito = ${linea:,.2f}, y siempre daba "
                               f"${float(antes):,.2f} (diferencia ${linea - float(antes):,.2f}). Puede faltar un movimiento "
                               f"o el banco cambió el límite.", clave=f"linea-{sid}-{fecha_rep}-{linea}")
            if x.get("cuadre") is not None and abs(float(x["cuadre"])) > 0.01:
                nom = nombre_tarjeta(db.get("saldos", sid), sid)
                avisar(db, f"{nom} no cuadra por ${float(x['cuadre']):.2f}: la deuda del banco no es igual al pago de contado "
                           f"más los movimientos después del corte. Puede faltar o sobrar un movimiento.",
                       clave=f"cuadre-{sid}-{fecha_rep}")
    log.append(f"Saldos actualizados: {len(saldos_tocados)}")
    for f in data.get("fallidos") or []:   # el lector no pudo con ese banco ni reintentando (14 nov)
        avisar(db, f"El lector de bancos no pudo leer {f} en la corrida del {desde[:16].replace('T', ' ')} (ya se reintentó). "
                   f"Sus saldos quedaron como en la lectura anterior.", clave=f"fallido-{f}-{desde[:13]}")

    # Movimientos (rediseño 14 nov 2026, pedido de Titular): LO QUE MANDA ES LA BANCA EN LÍNEA.
    # El correo es un aviso provisional. Ciclo de una compra con tarjeta:
    #   aviso (correo) -> en tránsito (el banco la muestra en proceso) -> asentada (el banco la lista).
    # Al asentarse, el banco sobrescribe el monto (preautorización de gasolinera, propina, hotel...). Del correo quedan
    # el día de la compra, la hora, el comercio legible y la tarjeta. Sin plazo fijo: un aviso abierto se enlaza aunque
    # el banco tarde semanas (se busca en los últimos 35 días). Lo asentado guarda mov["banco"] = lo que dijo el banco.
    usados = set()
    nuevos = 0
    todos = [clasificar_banco(m) for b in data.get("bancos", []) for m in (b.get("movimientos") or [])]
    if todos:
        f0 = min(m["fecha"] for m in todos)
        existentes = movs_cerca(db, f0, VENTANA_AVISOS)
    else:
        existentes = []
    es_tarjeta = lambda m: m.get("tipo") in ("Compra", "Abono")

    def ya_visto(e, m):
        """La misma fila del banco en una corrida anterior (guardada como pi-, o ya enlazada a un aviso del correo)."""
        if clave_medio(e.get("medio")) != clave_medio(m["medio"]):
            return False
        # 19 nov 2026: la plata que SALE no es la misma fila que la que ENTRA (Global Pagos: entraron $1,380 del 2x3 y
        # salieron $1,380 a TAP Max el mismo día; la salida se tomó por la entrada ya vista y la entrada se repitió).
        if es_entrante(e) != es_entrante(m):
            return False
        mismo = lambda f, v: f == m["fecha"] and abs(abs(float(v or 0)) - abs(float(m["monto"]))) < 0.005
        b = e.get("banco")
        if isinstance(b, dict) and b.get("fecha"):
            return mismo(b["fecha"], b.get("monto"))
        return e.get("origen") == "banco" and mismo(e.get("fecha"), e.get("monto"))

    def abiertos(m):
        """Avisos del correo de la misma tarjeta que el banco todavía no asentó (desde 1 día después hasta 35 antes)."""
        fm = fecha_de(m["fecha"])
        return [e for e in existentes if e["id"] not in usados and e.get("origen") == "correo" and not e.get("banco")
                and e.get("tipo") == m.get("tipo") and clave_medio(e.get("medio")) == clave_medio(m["medio"])
                and -1 <= (fm - fecha_de(e["fecha"])).days <= VENTANA_AVISOS]

    # Pista de los flotantes (idea de Titular, 14 nov 2026): un aviso que la lectura anterior vio «en proceso» y que en esta
    # ya no está en esa lista "desapareció" justo cuando el banco asentó algo: es la misma compra con su monto final.
    # Solo vale en tarjetas cuya lista «en proceso» se leyó bien en esta corrida (BAC y Banesco; Global no tiene lista).
    saldo_de = {}
    for sid in con_transito - linea_mala:
        for md in (db.get("saldos", sid) or {}).get("medios") or []:
            saldo_de[clave_medio(md)] = sid
    idos_de = {}

    def desaparecidos(m):
        sid = saldo_de.get(clave_medio(m["medio"]))
        if not sid:
            return set()
        if sid not in idos_de:
            s = db.get("saldos", sid) or {}
            ab = [e for e in existentes if e.get("origen") == "correo" and not e.get("banco") and not e.get("caido")
                  and e.get("medio") in (s.get("medios") or []) and e["id"] not in usados]
            siguen = emparejar_transito(ab, s.get("transito") or [])
            idos_de[sid] = {e["id"] for e in ab if e.get("enTransito") and e["id"] not in siguen}
        return idos_de[sid] - usados

    proporcion = lambda m, e: 0.3 <= float(m["monto"]) / max(float(e.get("monto") or 0), 0.01) <= 3

    sin_par = []
    for m in sorted(todos, key=lambda x: (x["fecha"], x["monto"])):
        dup = next((e for e in existentes if e["id"] not in usados and ya_visto(e, m)), None)
        if dup:
            usados.add(dup["id"])
        else:
            sin_par.append(m)
    # Banesco (17 y 19 nov 2026) a veces vuelve a listar una compra ya vista con OTRA fecha: Amazon $72.93 que dijo
    # del 10 nov salió el 19 como del 11 y entró repetida. Si una fila no tuvo par exacto, y hay UNA compra ya asentada
    # de la misma tarjeta, mismo monto, mismo comercio y a 5 días o menos, es la misma fila (Temu 7 -> 10 nov: 3 días; con fin de semana pueden ser más). (Va después de la pasada
    # exacta: si el banco lista las dos, la vieja se queda con su par y la otra entra como compra nueva.)
    def misma_fila_otra_fecha(e, m):
        if not es_tarjeta(m) or clave_medio(e.get("medio")) != clave_medio(m["medio"]) or e.get("tipo") != m.get("tipo"):
            return False
        b = e.get("banco")
        if isinstance(b, dict) and b.get("fecha"):
            f, v, c = b["fecha"], b.get("monto"), b.get("comercio") or e.get("comercio")
        elif e.get("origen") == "banco":
            f, v, c = e.get("fecha"), e.get("monto"), e.get("comercio")
        else:
            return False
        return abs(abs(float(v or 0)) - abs(float(m["monto"]))) < 0.005 and dias_entre(f, m["fecha"]) <= 5 \
            and mismo_comercio(c, m.get("comercio"))
    resto = []
    for m in sin_par:
        c = [e for e in existentes if e["id"] not in usados and misma_fila_otra_fecha(e, m)]
        if len(c) == 1:
            usados.add(c[0]["id"])
            log.append(f"Ya estaba con otra fecha: {m.get('comercio')} ${m['monto']} ({m['fecha']}) = {c[0]['id']}")
        else:
            resto.append(m)
    sin_par = resto
    # Tarjetas, pasada 1: mismo monto (el aviso más cercano en fecha)
    resto = []
    for m in sin_par:
        if es_tarjeta(m):
            c = [e for e in abiertos(m) if abs(float(e.get("monto") or 0) - float(m["monto"])) < 0.005]
            if c:
                asentar(db, min(c, key=lambda e: abs((fecha_de(m["fecha"]) - fecha_de(e["fecha"])).days)), m, usados, log)
                continue
        resto.append(m)
    # Tarjetas, pasada 2: otro monto, mismo comercio (o gasolinera en ambos), UNA sola opción -> el banco manda
    sin_par, resto = resto, []
    for m in sin_par:
        if es_tarjeta(m) and float(m["monto"]) > 0:
            gas = bool(GASOLINERA.search(m.get("comercio") or ""))
            c = [e for e in abiertos(m)
                 if 0.3 <= float(m["monto"]) / max(float(e.get("monto") or 0), 0.01) <= 3
                 and (mismo_comercio(e.get("comercio"), m.get("comercio"))
                      or (gas and GASOLINERA.search(e.get("comercio") or "")))]
            if len(c) == 1:
                asentar(db, c[0], m, usados, log)
                continue
            # Sin opción única por comercio: decide la pista de los flotantes, y solo si hay UNA opción de cada lado.
            idos = desaparecidos(m)
            if len(c) > 1:      # varios avisos parecidos: el que desapareció de «en proceso» en esta lectura
                d = [e for e in c if e["id"] in idos]
            else:               # el banco escribe el comercio de otra forma: el único aviso que desapareció y calza
                fm = fecha_de(m["fecha"])
                d = [e for e in abiertos(m) if e["id"] in idos and proporcion(m, e)
                     and -1 <= (fm - fecha_de(e["fecha"])).days <= 10]
                if len(d) == 1 and any(x is not m and es_tarjeta(x) and float(x["monto"]) > 0 and proporcion(x, d[0])
                                       and clave_medio(x["medio"]) == clave_medio(m["medio"]) for x in sin_par):
                    d = []      # hay otra fila nueva del banco que también le calzaría: no se adivina
            if len(d) == 1:
                asentar(db, d[0], m, usados, log, pista=True)
                continue
            if len(c) > 1:
                avisar(db, f"El banco asentó {m.get('comercio')} por ${float(m['monto']):.2f} el {m['fecha']} y hay {len(c)} avisos "
                           f"del correo parecidos con otro monto ({', '.join('$%.2f' % float(e['monto']) for e in c[:4])}). "
                           f"Revisa en Movimientos cuál es y borra el aviso que sobre.",
                       clave=f"monto-{m['fecha']}-{m['monto']}-{m.get('comercio')}")
        resto.append(m)
    # Yappy, ACH y lo demás: como antes (mismo monto y nombre; recibidos con otro nombre uno a uno)
    sin_par, quedan = [], []
    for m in resto:
        dup = None if es_tarjeta(m) else next((e for e in existentes if e["id"] not in usados and misma_tx(e, m)), None)
        if dup:
            usados.add(dup["id"])
        else:
            sin_par.append(m)
    for m in sin_par:
        if es_entrante(m):
            sueltos = [e for e in existentes if e["id"] not in usados and e.get("origen") != "banco"
                       and misma_tx(e, m, nombre=False)]
            if len(sueltos) == 1:
                usados.add(sueltos[0]["id"])
                db.merge("movs", sueltos[0]["id"], {"nombreBanco": m.get("comercio")})
                log.append(f"Cruzado con otro nombre: {sueltos[0].get('comercio')} = {m.get('comercio')} ${m['monto']}")
                continue
            if len(sueltos) > 1:
                avisar(db, f"El banco trae un {m['tipo']} de {m.get('comercio')} por ${float(m['monto']):.2f} el {m['fecha']} y hay "
                           f"{len(sueltos)} del correo con ese monto ({', '.join(e.get('comercio') or '?' for e in sueltos)}). "
                           f"Puede estar repetido: revisa en Movimientos y borra el que sobre.",
                       clave=f"ambiguo-{m['fecha']}-{m['monto']}-{m.get('comercio')}")
        quedan.append(m)
    # Lo que el banco tiene y el correo no: movimiento nuevo (ya asentado)
    for m in quedan:
        base = f"pi-{m['fecha'].replace('-', '')}-{centavos(m['monto'])}-"
        n = 1
        while db.exists("movs", base + str(n)):
            n += 1
        db.set("movs", base + str(n), m)
        existentes.append({**m, "id": base + str(n)})
        usados.add(base + str(n))
        nuevos += 1
        deuda_adicional(db, base + str(n), m, log)
        if m["tipo"] == "Yappy recibido" or (m["tipo"] == "ACH recibido" and m.get("quien") in CXC_DE):
            cruzar_cobro(db, base + str(n), m, log)   # ACH/Kuara de Beatriz o Ana = abono, igual que un Yappy (18 nov)
    log.append(f"Movimientos nuevos del banco: {nuevos}")
    cuadre_yappy(db, todos, fecha_rep, log)

    # Planes de cuotas de BAC (19 nov 2026): la compra que se pasó a un plan («Cc Banesco 4%» $14,000 de la Amex) sale de
    # tránsito y no se lista nunca como movimiento; su saldo vive en «Financiamientos» (saldo.financiado, que ya se
    # suma a la deuda). Se enlaza el aviso del correo con el plan (misma tarjeta, mismo monto que el saldo inicial,
    # hasta 5 días): queda asentado y deja de sumarse aparte, así no se cuenta dos veces. Un plan sin aviso no crea nada.
    for b in data.get("bancos", []):
        for x in b.get("tarjetas") or []:
            if not x.get("planes") or not db.exists("saldos", x.get("id") or ""):
                continue
            medios = (db.get("saldos", x["id"]) or {}).get("medios") or []
            for pl in x["planes"]:
                cerca = movs_cerca(db, pl["fecha"], VENTANA_AVISOS)
                ya = [e for e in cerca if e.get("medio") in medios and isinstance(e.get("banco"), dict)
                      and e["banco"].get("plan") and e["banco"].get("fecha") == pl.get("fecha")
                      and abs(float(e["banco"].get("monto") or 0) - float(pl.get("inicial") or 0)) < 0.005]
                if ya:
                    continue
                c = [e for e in cerca if e["id"] not in usados and e.get("origen") == "correo" and not e.get("banco")
                     and e.get("medio") in medios and e.get("tipo") == "Compra"
                     and abs(float(e.get("monto") or 0) - float(pl.get("inicial") or 0)) < 0.005
                     and dias_entre(e["fecha"], pl["fecha"]) <= 5]
                if len(c) == 1:
                    asentar(db, c[0], {"fecha": pl["fecha"], "monto": float(pl["inicial"]), "comercio": pl.get("concepto")},
                            usados, log)
                    db.merge("movs", c[0]["id"], {"banco": {"fecha": pl["fecha"], "monto": float(pl["inicial"]),
                                                            "comercio": pl.get("concepto"), "plan": True}})
                    log.append(f"Plan de cuotas enlazado: {c[0].get('comercio')} ${pl['inicial']}")
                else:
                    avisar(db, f"BAC tiene un plan de cuotas «{pl.get('concepto')}» por ${float(pl.get('inicial') or 0):,.2f} del "
                               f"{pl.get('fecha')} y no encontré {'su compra' if not c else 'una sola compra'} en la app. Su saldo "
                               f"(${float(pl.get('faltante') or 0):,.2f}) ya cuenta en la deuda de la tarjeta.",
                           clave=f"plan-{x['id']}-{pl.get('fecha')}-{pl.get('inicial')}")

    # Avisos del correo que el banco todavía no asentó: en tránsito, o se cayeron (preautorización cancelada)
    hoy = fecha_de(fecha_rep)
    for sid in saldos_tocados:
        s = db.get("saldos", sid)
        medios = s.get("medios") or []
        if s.get("tipo") != "tarjeta":
            # 21 nov 2026: BG llegó con 0 movimientos y TODOS los Yappy del correo quedaron «flotantes» (la página los sumó
            # otra vez encima del saldo: +$248.44). Una cuenta sin NINGUNA fila del banco en esta lectura no sirve para
            # decidir qué asentó el banco: no se toca lo pendiente y se avisa (antes cuadre_yappy callaba).
            claves = {clave_medio(m) for m in medios}
            if not any(clave_medio(m.get("medio")) in claves for m in todos):
                # solo importan los del CORREO: son los únicos que esta lectura podría marcar flotantes por error. Una cuenta
                # quieta (sin filas hoy) que solo tiene movimientos del banco no es una lectura mala (21 nov, «No la uses»).
                esp = [d for d in db.where("movs", "fecha", ">=", (hoy - timedelta(days=5)).isoformat())
                       if d.get("origen") == "correo" and clave_medio(d.get("medio")) in claves]
                if esp:
                    avisar(db, f"{nombre_tarjeta(s, sid)}: el lector no trajo ningún movimiento del banco, pero la app tiene "
                               f"{len(esp)} del correo en los últimos 5 días. Esa lectura quedó incompleta: no se tocó nada de lo pendiente. "
                               f"Si pasa otra vez, avísame.", clave=f"sinmovs-{sid}-{desde[:13]}")
                    log.append(f"{sid}: lectura sin movimientos del banco, se ignora para lo pendiente")
                continue
        pend = []
        for d in db.where("movs", "fecha", ">=", (hoy - timedelta(days=VENTANA_AVISOS)).isoformat()):
            if d.get("origen") != "correo" or d.get("medio") not in medios:
                continue
            if f"{d['fecha']}T{d.get('hora') or '23:59'}" > desde:
                continue
            if d["id"] in usados or d.get("banco"):
                if d.get("flotante") or d.get("enTransito"):
                    db.merge("movs", d["id"], {"flotante": None, "enTransito": None})
                continue
            pend.append(d)
        en_banco = emparejar_transito(pend, s.get("transito") or [])
        corte = ultimo_corte(hoy, s.get("corteDia")) if sid.startswith("bac-") else None
        # 14 nov: BAC mostró "20 USD" sin centavos, el lector no lo leyó y la app mató la gasolina de $20. Si la línea
        # (adeudado + disponible + en tránsito) no cuadra, a la lista "en proceso" le falta algo: no se confía en ella.
        leido = sid in con_transito and sid not in linea_mala
        limite = (datetime.strptime(desde[:16], "%Y-%m-%dT%H:%M") - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        for d in pend:
            if d["id"] in en_banco:  # 1) el banco la muestra en proceso: sigue en camino (aunque pasen semanas)
                if not d.get("flotante") or not d.get("enTransito") or d.get("fueraTransito") or d.get("caido"):
                    db.merge("movs", d["id"], {"flotante": True, "enTransito": True, "fueraTransito": None, "caido": None})
            elif corte and fecha_de(d["fecha"]) <= corte:
                # 2) BAC solo lista lo posterior al último corte: si no está en proceso, quedó en el estado de cuenta.
                #    (Si fuera falso, el cuadre de BAC —contado + movimientos = deuda— no da y avisa.)
                db.merge("movs", d["id"], {"flotante": None, "enTransito": None, "caido": None,
                                            "banco": {"corte": corte.isoformat()}})
            elif d.get("caido"):
                continue
            elif leido and f"{d['fecha']}T{d.get('hora') or '00:00'}" <= limite:
                # 3) (pedido de Titular, 14 nov) el banco ya no la muestra ni en proceso ni asentada: se murió en esta
                #    misma lectura. Solo si esta corrida sí leyó la lista "en proceso" de la tarjeta, y con 2 h de margen.
                db.merge("movs", d["id"], {"flotante": None, "enTransito": None, "fueraTransito": None, "caido": True})
                avisar(db, f"{d['comercio']} ${float(d['monto']):.2f} del {d['fecha']} ({d['medio']}) ya no sale en el banco "
                           f"(ni en proceso ni asentada): preautorización cancelada. Ya no se cuenta como deuda. "
                           f"Si la ves cobrada en el banco, avísame.", clave="caido-" + d["id"])
            else:
                # 4) Global (sin lista "en proceso") o aviso muy reciente: queda flotante, sin plazo. En Global lo flotante
                #    está dentro del disponible; solo muere si el disponible sube justo su monto (cuadre_disponible).
                if not d.get("flotante"):
                    db.merge("movs", d["id"], {"flotante": True})
    for sid in saldos_tocados:
        cuadre_disponible(db, sid, desde, log)
        cuadre_cuenta(db, sid, desde, log)
    # 14 nov (Puma de Beatriz): el banco manda también en lo que se cobra. Si esta lectura cambió el monto de una compra
    # marcada «Por cobrar» ($20 preautorizados -> $14.31) o la dio por no asentada, el total de la persona se rehace ya.
    recalcular_cxc(db)


def cuadre_disponible(db, sid, desde, log):
    """Cuadre del disponible (idea de Titular, 14 nov 2026), igual para todos los bancos:
         disponible + (suma de los movimientos de la tarjeta desde ANCLA_DISP) = constante K
    Cada compra baja el disponible y sube la suma en lo mismo; un pago o abono al revés; una preautorización que se
    asienta con otro monto mueve ambos igual. Si K cambia entre lecturas, falta o sobra un movimiento -> aviso.
    Global calcula su disponible en lote y dice hasta cuándo ("Disponible al 14 nov", ese día incluido): ahí se cuentan
    los movimientos hasta esa fecha (sin el dato: los de días anteriores, y sábado/domingo no se revisa)."""
    s = db.get("saldos", sid) or {}
    if s.get("tipo") != "tarjeta" or s.get("disponible") is None:
        return
    global_ = sid.startswith("global-")
    dia = desde[:10]
    # Global dice hasta cuándo calculó ("Disponible al 14 nov" = lo de antes del 14). Sin ese dato: las del día no
    # cuentan y el fin de semana no se revisa.
    corte_g = s.get("disponibleAl") if global_ and s.get("disponibleAl") else None
    if global_ and not corte_g and fecha_de(dia).weekday() >= 5:
        return
    medios = set(s.get("medios") or [])
    t = 0.0
    for m in db.where("movs", "fecha", ">=", ANCLA_DISP):
        if m.get("medio") not in medios or m.get("caido") or m.get("tipo") not in ("Compra", "Abono", "Cargo banco"):
            continue
        if global_:
            # 16 nov 2026: "Disponible al 15 nov" INCLUYE el 15 (ese día el disponible bajó $126.02 = las seis compras del 15,
            # al centavo; antes se suponía lo contrario y dio un aviso falso por $102.85). Sin ese dato, lo del día no cuenta.
            # La fecha de la compra es la más temprana entre el correo y el banco (Worldwide Medical: banco 13, correo 14).
            f = min(m["fecha"], (m.get("banco") or {}).get("fecha") or m["fecha"])
            if (f > corte_g) if corte_g else (f >= dia):
                continue
        elif f"{m['fecha']}T{m.get('hora') or '00:01'}" > desde:
            continue
        t += float(m.get("monto") or 0)
    k = round(float(s["disponible"]) + t, 2)
    antes = s.get("cuadreDisp") or {}
    if antes.get("k") is not None and k - float(antes["k"]) > 0.01:
        # el disponible subió sin movimiento que lo explique: si es justo el monto de UNA compra flotante, se liberó
        dif = round(k - float(antes["k"]), 2)
        flot = [m for m in db.where("movs", "fecha", ">=", ANCLA_DISP) if m.get("medio") in medios and m.get("flotante")
                and not m.get("banco") and not m.get("caido") and abs(float(m.get("monto") or 0) - dif) < 0.01]
        if len(flot) == 1:
            m = flot[0]
            db.merge("movs", m["id"], {"flotante": None, "enTransito": None, "caido": True})
            avisar(db, f"{m.get('comercio')} ${float(m['monto']):.2f} del {m['fecha']} ({m['medio']}): el banco devolvió ese monto "
                       f"al disponible sin cobrarlo. Preautorización cancelada; ya no se cuenta como deuda.",
                   clave="caido-" + m["id"])
            log.append(f"Liberada por disponible: {m.get('comercio')} ${m['monto']}")
            k = round(k - float(m["monto"]), 2)
    if antes.get("k") is not None and abs(k - float(antes["k"])) > 0.01:
        dif = round(k - float(antes["k"]), 2)
        avisar(db, f"{nombre_tarjeta(s, sid)}: el disponible no cuadra con los movimientos por ${abs(dif):,.2f} desde la lectura "
                   f"del {str(antes.get('desde', ''))[:16].replace('T', ' ')}. "
                   f"{'Sobra disponible: puede sobrar un movimiento en la app o faltar un pago.' if dif > 0 else 'Falta disponible: puede faltar una compra en la app.'}",
               clave=f"disp-{sid}-{desde}")
        log.append(f"Disponible {sid} no cuadra: {dif:+.2f}")
    db.merge("saldos", sid, {"cuadreDisp": {"k": k, "desde": desde}})


def cuadre_cuenta(db, sid, desde, log):
    """Cuadre del saldo de las cuentas (19 nov 2026, pedido de Titular, como red de seguridad):
         saldo del banco - (entradas - salidas de la app desde ANCLA_DISP) = constante
    Si un movimiento se repite o falta en la app, el número cambia. Como a veces el banco anota algo un día después, solo
    avisa si la MISMA diferencia sigue en dos lecturas seguidas; un atraso se arregla solo en la siguiente lectura.
    La cuenta Yappy ya tiene su cuadre día por día (cuadre_yappy): aquí no se repite."""
    s = db.get("saldos", sid) or {}
    if s.get("tipo") == "tarjeta" or s.get("saldo") is None or not s.get("medios") or "Yappy" in (s.get("medios") or []):
        return
    medios = set(s["medios"])
    t = 0.0
    for m in db.where("movs", "fecha", ">=", ANCLA_DISP):
        if m.get("medio") not in medios or m.get("caido"):
            continue
        # 22 nov 2026: lo que trae la lectura del banco (origen banco) YA está en su saldo aunque el banco lo anote con fecha
        # de mañana (el Kuara de $58 de la noche del 22 salió como del 23): solo se deja fuera lo posterior que NO vino del banco.
        if m.get("origen") != "banco" and f"{m['fecha']}T{m.get('hora') or '00:01'}" > desde:
            continue
        v = abs(float(m.get("monto") or 0))
        t += v if es_entrante(m) else -v
    k = round(float(s["saldo"]) - t, 2)
    antes = s.get("cuadreCuenta") or {}
    if antes.get("k") is None:
        db.merge("saldos", sid, {"cuadreCuenta": {"k": k, "desde": desde, "pend": None}})
        return
    dif = round(k - float(antes["k"]), 2)
    if abs(dif) <= 0.01:
        db.merge("saldos", sid, {"cuadreCuenta": {"k": k, "desde": desde, "pend": None}})
        return
    pend = antes.get("pend")
    if pend is not None and abs(float(pend) - dif) <= 0.01:
        nom = nombre_tarjeta(s, sid)
        avisar(db, f"{nom}: el saldo del banco no cuadra con los movimientos de la app por ${abs(dif):,.2f} (dos lecturas "
                   f"seguidas, desde la del {str(antes.get('desde', ''))[:16].replace('T', ' ')}). "
                   f"{'Puede faltar una entrada en la app o sobrar una salida.' if dif > 0 else 'Puede sobrar una entrada repetida en la app o faltar una salida.'}",
               clave=f"cuenta-{sid}-{desde}")
        log.append(f"Cuenta {sid} no cuadra: {dif:+.2f}")
        db.merge("saldos", sid, {"cuadreCuenta": {"k": k, "desde": desde, "pend": None}})   # avisa una vez
    else:
        log.append(f"Cuenta {sid}: diferencia {dif:+.2f}, se espera a la próxima lectura")
        db.merge("saldos", sid, {"cuadreCuenta": {"k": antes["k"], "desde": antes.get("desde"), "pend": dif}})


ANCLA_DISP = "2026-11-11"   # desde aquí se suman los movimientos para el cuadre del disponible

VENTANA_AVISOS = 35        # días hacia atrás en que un aviso del correo puede enlazarse con lo que asienta el banco
def asentar(db, e, m, usados, log, pista=False):
    """El banco asentó el aviso e del correo: el banco manda en el monto. Si el monto cambió, se busca de nuevo su
    factura con el monto real (aunque hayan pasado los 7 días de búsqueda)."""
    usados.add(e["id"])
    upd = {"banco": {"fecha": m["fecha"], "monto": m["monto"], "comercio": m.get("comercio")},
           "flotante": None, "enTransito": None, "fueraTransito": None, "preaut": None, "caido": None}
    cambio = abs(float(e.get("monto") or 0) - float(m["monto"])) >= 0.005
    if cambio:
        upd["monto"] = m["monto"]
        upd["notas"] = ((e.get("notas") or "") + " " if e.get("notas") else "") + \
            f"Aviso del correo por ${float(e['monto']):.2f}; monto final según el banco ${float(m['monto']):.2f}."
        log.append(f"Asentada con otro monto: {e.get('comercio')} ${e['monto']} -> ${m['monto']}")
    if pista:   # enlazada por la pista de los flotantes: queda escrito por qué, para poder revisarlo
        upd["notas"] = ((upd.get("notas") or e.get("notas") or "") + " ").lstrip() + \
            f"Enlazada con «{m.get('comercio')}» del banco porque salió de «en proceso» en la misma lectura en que se asentó."
        log.append(f"Enlazada por flotante desaparecido: {e.get('comercio')} = {m.get('comercio')} ${m['monto']}")
    db.merge("movs", e["id"], upd)
    if cambio and not e.get("facturaId"):
        try:
            from . import sueltas
            for a in db.all("archivos"):
                t = (a.get("datos") or {}).get("total")
                if a.get("estado") in ("espera", "sin-transaccion") and t is not None and abs(float(t) - float(m["monto"])) < 0.005:
                    if sueltas.intentar(db, a, log):
                        break
        except Exception as ex:
            log.append(f"Factura tras asentar: {type(ex).__name__}: {ex}")

def cuadre_yappy(db, todos, fecha_rep, log):
    """Revisión final (pedido de Titular, 13 nov 2026): por cada día, lo que dice el banco de la cuenta Yappy (BG 1111)
    debe ser igual a lo que tiene la app. Yappy y el banco nunca repiten, así que si no da igual hay uno repetido o
    uno que falta. El día del reporte no se revisa (pueden faltar correos); se ve en la corrida siguiente."""
    yappy = ("BG", "1111")
    banco = [m for m in todos if clave_medio(m["medio"]) == yappy and m["fecha"] < fecha_rep]
    if not banco:
        return
    f0 = min(m["fecha"] for m in banco)
    signo = lambda d: (1 if es_entrante(d) else -1) * abs(float(d.get("monto") or 0))
    app = [d for d in db.where("movs", "fecha", ">=", f0)
           if clave_medio(d.get("medio")) == yappy and d["fecha"] < fecha_rep]
    dias = sorted({m["fecha"] for m in banco} | {d["fecha"] for d in app})
    suma = {f: (round(sum(signo(m) for m in banco if m["fecha"] == f), 2),
                round(sum(signo(d) for d in app if d["fecha"] == f), 2)) for f in dias}
    dif = {f: round(a - b, 2) for f, (b, a) in suma.items()}
    # 14 nov: un Yappy de noche (Laura 8 nov 21:36, Luis 11 nov 22:01) BG lo anota al día siguiente: la app tiene de
    # más un día justo lo que le falta el siguiente. Si en la app hay un Yappy de noche (desde las 18:00) por ese
    # monto, es el mismo movimiento con otra fecha: no se avisa.
    # 15 nov (Xalek $9 del 14 a las 23:20): si el día siguiente es el de la lectura, ese día no se cuadra (pueden faltar
    # correos), pero el banco YA trae ahí el Yappy corrido: se mira si esta misma lectura lo trae con esa fecha.
    # Pueden ser VARIOS Yappy de noche (dos o tres que entre todos suman la diferencia): se busca el grupo que la explica.
    banco_hoy = [signo(m) for m in todos if clave_medio(m["medio"]) == yappy and m["fecha"] == fecha_rep]

    def estan_en(montos, lista):
        """¿Cada monto aparece en la lista, uno por uno (sin usar dos veces la misma fila del banco)?"""
        libres = list(lista)
        for v in montos:
            i = next((k for k, w in enumerate(libres) if abs(w - v) < 0.005), None)
            if i is None:
                return False
            libres.pop(i)
        return True

    corridos = set()
    # 22 nov 2026: el banco solo trae 10 días; el primer día de la ventana no tiene a su víspera para emparejar el Yappy
    # de noche (Luis 11 nov 22:01, BG lo anotó el 12). Si al primer día le sobra en el banco lo que la app tiene de noche
    # la víspera, es el mismo movimiento corrido: no es descuadre.
    prev = (fecha_de(f0) - timedelta(days=1)).isoformat()
    d0 = dif.get(f0, 0)
    if d0 < -0.005:
        noche0 = [signo(x) for x in db.where("movs", "fecha", "==", prev)
                  if clave_medio(x.get("medio")) == yappy and (x.get("hora") or "") >= "18:00"][:12]
        if any(abs(sum(g) + d0) < 0.005 for n in range(1, len(noche0) + 1) for g in combinations(noche0, n)):
            corridos.add(f0)
            log.append(f"Yappy {prev}: ${-d0:,.2f} de noche que el banco anotó el {f0}")
    for f in dias:
        sig = (fecha_de(f) + timedelta(days=1)).isoformat()
        d = dif.get(f, 0)
        if abs(d) <= 0.005:
            continue
        noche = [signo(x) for x in app if x["fecha"] == f and (x.get("hora") or "") >= "18:00"][:12]
        grupos = [g for n in range(1, len(noche) + 1) for g in combinations(noche, n) if abs(sum(g) - d) < 0.005]
        if not grupos:
            continue
        if sig in dif and abs(d + dif[sig]) < 0.005:
            corridos |= {f, sig}
        elif sig == fecha_rep and any(estan_en(g, banco_hoy) for g in grupos):
            corridos.add(f)
        else:
            continue
        log.append(f"Yappy {f}: ${d:,.2f} de noche que el banco anotó el {sig}")
    for f in dias:
        b, a = suma[f]
        if abs(a - b) > 0.005 and f not in corridos:
            nb = sum(1 for m in banco if m["fecha"] == f); na = sum(1 for d in app if d["fecha"] == f)
            avisar(db, f"Cuenta Yappy del {f} no cuadra: el banco suma ${b:,.2f} ({nb} movimientos) y la app ${a:,.2f} "
                       f"({na}). Diferencia ${a - b:,.2f}: puede haber uno repetido o faltar uno. Revisa ese día en Movimientos.",
                   clave=f"cuadre-yappy-{f}-{a}-{b}")
            log.append(f"Yappy {f} no cuadra: banco {b} app {a}")


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
    if not C.NOTA_IPHONE:   # en pausa (18 nov 2026): se da por leída y no se toca nada
        log.append("Nota del iPhone en pausa: no se aplica")
        return True
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
    recalcular_cxc(db)   # la casilla 2 depende de lo que diga la nota (los abonos no bajan de 0)
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
