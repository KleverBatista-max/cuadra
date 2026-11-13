#!/usr/bin/env python3
"""Revisa Global, BAC, Banco General y Banesco una vez al día y manda un correo PI-BANCOS con el resultado.

Uso:
  python3 bancos.py                  # todos los bancos, guarda JSON y manda correo
  python3 bancos.py --solo global    # un banco (global|bac|bg|banesco)
  python3 bancos.py --sin-correo     # no manda correo, solo guarda JSON
  python3 bancos.py --headless       # sin ventana (después de que todo funcione)
  python3 bancos.py --solo global --prueba   # prueba: sin correo, guarda texto+captura de cada paso en ~/bancos_out/debug

Archivo credenciales.env.nosync en esta misma carpeta (".nosync" = iCloud no lo sube; no lo compartas):
  GLOBAL_USER= GLOBAL_PASS= BAC_USER= BAC_PASS= BG_USER= BG_PASS= BANESCO_USER= BANESCO_PASS=
  GMAIL_USER=titular@example.com  GMAIL_APP_PASS=<contraseña de aplicación>
Nunca hace clic en Pagar / Transferir / menús de configuración: solo lee.
"""
import argparse, datetime as dt, json, os, re, smtplib, sys, time
from email.message import EmailMessage
from pathlib import Path

import parsers as P

HOME = Path.home()
OUT = HOME / "bancos_out"
PERFIL = HOME / ".bancos_perfiles"
DIAS = 10  # cuántos días hacia atrás se reportan movimientos
HOY = dt.date.today()
DESDE = (HOY - dt.timedelta(days=DIAS)).isoformat()
DEBUG = OUT / "debug"
PRUEBA = False
_paso = [0]


# ---------- config ----------
CARPETA = Path(__file__).resolve().parent


def cargar_env():
    # credenciales.env.nosync en la misma carpeta del script (antes estaba en ~/.bancos.env; se acepta igual)
    f = CARPETA / "credenciales.env.nosync"  # .nosync: iCloud Drive no lo sube (el Escritorio se sincroniza)
    if not f.exists():
        f = HOME / ".bancos.env"
    env = dict(os.environ)
    if f.exists():
        for l in f.read_text().splitlines():
            if "=" in l and not l.strip().startswith("#"):
                k, v = l.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if v:  # una línea vacía no borra un valor puesto antes
                    env[k.strip()] = v
    return env


# ---------- tipos de movimiento ----------
def tipo_cuenta(desc, monto):
    y = "YAPPY" in desc.upper()
    if monto > 0:
        return "Yappy recibido" if y else "ACH recibido"
    return "Yappy pago" if y else "ACH enviado"


def mov_cuenta(m, medio):
    if m["fecha"] < DESDE:
        return None
    return {"fecha": m["fecha"], "hora": "00:01", "comercio": m["desc"][:80], "monto": abs(m["monto"]),
            "medio": medio, "tipo": tipo_cuenta(m["desc"], m["monto"])}


def mov_tarjeta(m, medio):
    if m["fecha"] < DESDE:
        return None
    monto = -m["monto"]  # compra positiva, abono negativo
    return {"fecha": m["fecha"], "hora": "00:01", "comercio": m["desc"][:80], "monto": monto,
            "medio": medio, "tipo": "Compra" if monto > 0 else "Abono"}


def limpio(d):
    return {k: v for k, v in d.items() if v is not None}


# ---------- helpers de navegador ----------
def texto(page):
    return page.inner_text("body").replace("\xa0", " ")


def dbg(page, banco, nombre):
    """En --prueba guarda el texto visible y una captura de la página actual."""
    if not PRUEBA:
        return
    _paso[0] += 1
    base = DEBUG / ("%s-%02d-%s" % (banco, _paso[0], re.sub(r"\W+", "_", nombre)))
    try:
        base.with_suffix(".txt").write_text(page.url + "\n\n" + texto(page))
    except Exception as e:
        base.with_suffix(".txt").write_text("sin texto: %s" % e)
    try:
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        pass
    print("  [debug]", base.name)


def esperar(page, ms=2500):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def llenar(page, etiquetas, valor, espera=60):
    """Busca el campo visible por etiqueta/placeholder; reintenta hasta `espera` segundos (páginas lentas)."""
    fin = time.time() + espera
    while True:
        for e in etiquetas:
            for loc in (page.get_by_label(e, exact=False), page.get_by_placeholder(e)):
                try:
                    for i in range(loc.count()):
                        if loc.nth(i).is_visible() and loc.nth(i).is_editable():
                            loc.nth(i).fill(valor)
                            return
                except Exception:
                    pass
        if time.time() > fin:
            raise RuntimeError("no encontré el campo " + "/".join(etiquetas))
        page.wait_for_timeout(1000)


def clic_texto(page, nombres, rol="button"):
    for n in nombres:
        for loc in (page.get_by_role(rol, name=re.compile(n, re.I)), page.get_by_text(re.compile("^" + n + "$", re.I))):
            try:
                if loc.count():
                    loc.first.click()
                    return True
            except Exception:
                pass
    return False


def abrir(pw, banco, headless):
    ctx = pw.chromium.launch_persistent_context(str(PERFIL / banco), headless=headless,
                                                viewport={"width": 1366, "height": 900}, locale="es-PA")
    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())


# ---------- Global Bank ----------
G_CUENTAS = {"00000000001": ("global-tapmax", "Global Tap Max"),
             "00000000004": ("global-nolauses", "Global NoLaUses"),
             "00000000002": ("global-menos5000", "Global Menos5000"),
             "00000000003": ("global-pagos", "Global Pagos")}  # 00000000001 (Ana y Carla) se ignora


def banco_global(page, env):
    page.goto("https://ebanking.globalbank.com.pa/auth/login"); esperar(page); dbg(page, "global", "login")
    llenar(page, ["Usuario"], env["GLOBAL_USER"]); clic_texto(page, ["Continuar"]); esperar(page)
    dbg(page, "global", "usuario_enviado")
    llenar(page, ["Contraseña"], env["GLOBAL_PASS"]); clic_texto(page, ["Ingresar"])
    page.wait_for_url("**/app/dashboard", timeout=180000); esperar(page, 4000)  # 3 min por si pide código
    dbg(page, "global", "dashboard")
    dash = texto(page)
    cuentas_d, tarjetas_d = P.global_dashboard(dash)
    res = {"banco": "Global Bank", "cuentas": [], "tarjetas": [], "movimientos": []}
    for c in cuentas_d:
        if c["numero"] not in G_CUENTAS:
            continue
        id_, medio = G_CUENTAS[c["numero"]]
        res["cuentas"].append({"id": id_, "saldo": c["saldo"]})
        try:
            page.goto("https://ebanking.globalbank.com.pa/app/dashboard"); esperar(page)
            page.get_by_text(c["nombre"], exact=True).first.click(); esperar(page, 3500)
            dbg(page, "global", "cuenta_" + c["numero"])
            for m in P.global_cuenta_movs(texto(page)):
                x = mov_cuenta(m, medio)
                if x: res["movimientos"].append(x)
        except Exception as e:
            print("  Global cuenta", c["numero"], "sin movimientos:", e)
    for t in tarjetas_d:
        try:
            page.goto("https://ebanking.globalbank.com.pa/app/dashboard"); esperar(page)
            page.get_by_text(t["nombre"], exact=True).first.click(); esperar(page, 3500)
            dbg(page, "global", "tarjeta_" + t["ultimos4"])
            d = P.global_tarjeta(texto(page))
            res["tarjetas"].append(limpio({"id": "global-mc-" + t["ultimos4"], "deuda": d.get("deuda"),
                                           "disponible": d.get("disponible", t["disponible"]),
                                           "pagoDia": d.get("pagoDia"), "pagoMin": d.get("pagoMin")}))
            for m in d["movs"]:
                x = mov_tarjeta(m, "Global MC " + t["ultimos4"])
                if x: res["movimientos"].append(x)
        except Exception as e:
            print("  Global tarjeta", t["ultimos4"], "falló:", e)
    return res


# ---------- BAC ----------
def banco_bac(page, env):
    pg = page
    pg.goto("https://www.sucursalelectronica.com/redir/showLogin.go"); esperar(pg, 3000)
    dbg(pg, "bac", "login")
    llenar(pg, ["Usuario"], env["BAC_USER"]); llenar(pg, ["Contraseña"], env["BAC_PASS"])
    pg.wait_for_timeout(500)
    clic_texto(pg, ["Ingresar"])
    fin = time.time() + 180  # hasta 3 min por si pide token/código
    while not re.search(r"Cuentas bancarias|Tarjetas de cr[eé]dito|CUENTA DE AHORROS", texto(pg)):
        if "SESIONES ACTIVAS" in texto(pg).upper():  # sesión anterior sin cerrar: abrir una nueva
            clic_texto(pg, ["CONTINUAR"]); esperar(pg, 3000); continue
        if time.time() > fin:
            raise RuntimeError("BAC no mostró el resumen (url: %s)" % pg.url)
        pg.wait_for_timeout(2000)
    esperar(pg, 3000)
    dbg(pg, "bac", "resumen")
    res = {"banco": "BAC", "cuentas": [], "tarjetas": [], "movimientos": []}
    resumen = texto(pg)
    cuentas, tarjetas = P.bac_resumen(resumen)
    for c in cuentas:
        if c["numero"] == "100000001":
            res["cuentas"].append({"id": "bac-ahorros", "saldo": c["saldo"]})
    ids = {"1101": ("bac-amex-1101", "BAC Amex 1101"), "1102": ("bac-mc-1102", "BAC MC 1102")}
    for t in tarjetas:
        if t["ultimos4"] in ids:
            res["tarjetas"].append({"id": ids[t["ultimos4"]][0], "deuda": t["saldo"]})
    # Detalle de cada producto: SOLO la celda "Ver" (td id=tdAccountBalanceItem...).
    # NUNCA los botones de Transferir / Pagar de la misma fila.
    resumen_url = pg.url
    ver = "td[id^='tdAccountBalanceItem']"
    for i in range(pg.locator(ver).count()):
        try:
            if i:
                pg.goto(resumen_url); esperar(pg, 3000)
            celda = pg.locator(ver).nth(i)
            fila = celda.locator("xpath=..").inner_text().replace("\xa0", " ")
            celda.locator("button").first.click(); esperar(pg, 4000)
            if "AHORROS" in fila:
                dbg(pg, "bac", "ahorros")
                if "100000001" in fila:
                    for m in P.bac_cuenta_movs(texto(pg)):
                        x = mov_cuenta(m, "BAC 1110")
                        if x: res["movimientos"].append(x)
                continue
            u4 = re.search(r"(\d{4})\s", fila)
            u4 = u4.group(1) if u4 else "????"
            dbg(pg, "bac", "tarjeta_" + u4)
            if u4 not in ids:
                continue
            d = P.bac_tarjeta(texto(pg))
            for m in d["movs"]:
                x = mov_tarjeta(m, ids[u4][1])
                if x: res["movimientos"].append(x)
            transito = None
            try:  # flotantes: SOLO el enlace "Consultar" del recuadro "En tránsito o flotantes" (es una consulta)
                # hay otro "Consultar" en el menú de la izquierda: buscar SOLO dentro del recuadro de flotantes
                caja = pg.locator("div, p, span").filter(has_text=re.compile(r"presione\s+Consultar", re.I)).last
                c = caja.get_by_text(re.compile(r"^\s*Consultar\s*\.?\s*$"))
                if c.count() == 1:
                    c.click(); esperar(pg, 3000)
                    dbg(pg, "bac", "transito_" + u4)
                    transito = P.bac_transito(texto(pg))
                else:
                    print("  BAC %s: no encontré un único enlace 'Consultar' (flotantes)" % u4)
            except Exception as e:
                print("  BAC %s flotantes no leídas:" % u4, e)
            # Pago mínimo: SOLO el botón azul "Pagar tarjeta" del detalle (autorizado por Titular, 13 nov 2026).
            # Abre el paso 1 "Selección"; se LEE y se sale. NUNCA marcar monto, elegir cuenta ni CONTINUAR.
            pago = {}
            try:
                b = pg.get_by_role("button", name=re.compile(r"^\s*Pagar tarjeta\s*$", re.I))
                if b.count() != 1:
                    b = pg.get_by_role("link", name=re.compile(r"^\s*Pagar tarjeta\s*$", re.I))
                if b.count() == 1:
                    b.click(); esperar(pg, 4000)
                    dbg(pg, "bac", "pago_" + u4)
                    t = texto(pg)
                    if re.search(r"PAGO DE TARJETA DE CR[EÉ]DITO", t, re.I) and u4 in t:
                        pago = P.bac_pago(t)
                else:
                    print("  BAC %s: no encontré un único botón 'Pagar tarjeta'" % u4)
            except Exception as e:
                print("  BAC %s pago mínimo no leído:" % u4, e)
            for r in res["tarjetas"]:
                if r["id"] == ids[u4][0]:
                    r.update(limpio({"disponible": d.get("disponible"), "pagoDia": d.get("pagoDia"),
                                     "pagoMin": pago.get("pagoMin"), "saldoCorte": pago.get("pagoContado"),
                                     "transito": transito}))
        except Exception as e:
            print("  BAC detalle %d no leído:" % i, e)
    try:  # cerrar sesión para que la próxima vez no salga "sesiones activas"
        clic_texto(pg, ["Salir"], "link") or clic_texto(pg, ["Salir"]); esperar(pg, 2000)
    except Exception:
        pass
    return res


# ---------- Banco General ----------
BG_CUENTAS = {"00-00-00-000000-0": ("bg-yappy", "BG 1111"),
              "00-00-00-000000-0": ("bg-nolauses", "BG 1112"),
              "00-00-00-000000-0": ("bg-pagomovil", "BG 1113")}


# Preguntas de seguridad de BG: palabras que identifican la pregunta -> clave en credenciales.env.nosync.
# Cualquier otra BG_RESP_<PALABRA> también sirve si <palabra> aparece en la pregunta.
BG_PREGUNTAS = {"BG_RESP_MASCOTA": ["mascota"], "BG_RESP_CIUDAD": ["ciudad"]}


def _sin_tildes(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def bg_pregunta(page, env):
    """Si la página muestra una pregunta de seguridad conocida, la responde. Devuelve True si respondió."""
    preguntas = [q for q in re.findall(r"¿[^?\n]{3,120}\?", texto(page))
                 if not re.search(r"ayuda|olvidaste|reconoces", q, re.I)]  # textos de ayuda, no preguntas
    if not preguntas:
        return False
    mapa = dict(BG_PREGUNTAS)
    for k in env:
        if k.startswith("BG_RESP_") and k not in mapa:
            mapa[k] = [_sin_tildes(k[len("BG_RESP_"):])]
    for preg in preguntas:
        p = _sin_tildes(preg)
        for clave, palabras in mapa.items():
            if any(w in p for w in palabras):
                if not env.get(clave):
                    raise RuntimeError("BG pregunta '%s' y falta %s en credenciales.env.nosync" % (preg, clave))
                dbg(page, "bg", "pregunta")
                campo = None
                try:
                    loc = page.get_by_label(preg.strip("¿?"), exact=False)
                    if loc.count(): campo = loc.first
                except Exception:
                    pass
                if campo is None:
                    vis = page.locator("input[type=text]:visible, input[type=password]:visible, input:not([type]):visible")
                    for i in range(vis.count()):
                        if not vis.nth(i).input_value():
                            campo = vis.nth(i); break
                if campo is None:
                    raise RuntimeError("BG pregunta '%s' pero no encontré dónde escribir" % preg)
                campo.fill(env[clave])
                clic_texto(page, ["Continuar", "Ingresar", "Enviar", "Aceptar", "Validar"])
                print("  BG pregunta de seguridad respondida:", preg)
                esperar(page, 3000)
                return True
    if tuple(preguntas) not in _avisadas:
        _avisadas.add(tuple(preguntas))
        print("  BG muestra una pregunta que no conozco:", preguntas, "- respóndela en la ventana (3 min)")
    return False


_avisadas = set()


def banco_bg(page, env):
    # el login de bgeneral.com es un iframe de bgsegura.bgeneral.com: se abre directo
    page.goto("https://bgsegura.bgeneral.com/", wait_until="domcontentloaded", timeout=60000); esperar(page, 2000); dbg(page, "bg", "login")
    llenar(page, ["usuario", "Usuario"], env["BG_USER"])
    with page.context.expect_page(timeout=30000) as nueva:  # el botón (imagen sin texto) abre zonasegura en otra pestaña
        page.locator("button.btningresar").click()
    page = nueva.value
    page.wait_for_load_state(); esperar(page, 4000)
    dbg(page, "bg", "usuario_enviado")
    bg_pregunta(page, env)  # a veces la pregunta sale antes de la contraseña
    fin = time.time() + 180  # esperar la pantalla de contraseña (puede venir después de la pregunta)
    while "/login/password" not in page.url and time.time() < fin:
        bg_pregunta(page, env); page.wait_for_timeout(1500)
    dbg(page, "bg", "contrasena")
    llenar(page, ["contraseña", "Contraseña", "clave"], env["BG_PASS"]); clic_texto(page, ["Entrar", "Ingresar"])
    fin = time.time() + 180  # hasta 3 min: preguntas de seguridad o código manual
    while "/group/guest/dashboard" not in page.url:
        if time.time() > fin:
            raise RuntimeError("BG no llegó al dashboard (url: %s)" % page.url)
        esperar(page, 2000)
        if "/group/guest/dashboard" not in page.url:
            bg_pregunta(page, env)
    esperar(page, 4000)
    dbg(page, "bg", "dashboard")
    res = {"banco": "Banco General", "cuentas": [], "tarjetas": [], "movimientos": []}
    for c in P.bg_dashboard(texto(page)):
        if c["numero"] not in BG_CUENTAS:
            continue
        id_, medio = BG_CUENTAS[c["numero"]]
        res["cuentas"].append({"id": id_, "saldo": c["saldo"]})
        try:
            page.goto("https://zonasegura.bgeneral.com/group/guest/dashboard"); esperar(page, 3000)
            page.get_by_text(c["nombre"], exact=True).first.click(); esperar(page, 4000)  # carga lenta
            for _ in range(15):  # "Ver más" (solo carga más filas) hasta pasar la fecha DESDE
                movs = P.bg_movs(texto(page))
                if not movs or min(m["fecha"] for m in movs) < DESDE:
                    break
                if not clic_texto(page, ["Ver más"]):
                    break
                esperar(page, 2500)
            dbg(page, "bg", "cuenta_" + c["numero"])
            for m in P.bg_movs(texto(page)):
                x = mov_cuenta(m, medio)
                if x: res["movimientos"].append(x)
        except Exception as e:
            print("  BG cuenta", c["numero"], "sin movimientos:", e)
    return res


# ---------- Banesco ----------
def banesco_transito(pg, nombre):
    """Pestaña 'Movimientos en tránsito' de la tarjeta abierta. Solo la pestaña: nunca Pagar ni el interruptor.
    Ojo: desde esta pestaña Banesco da error si se abre otra tarjeta; leerla al final de cada tarjeta."""
    try:
        tab = pg.get_by_text(re.compile(r"^\s*Movimientos en tr[aá]nsito"))
        if tab.count() >= 1:
            tab.first.click(); esperar(pg, 3000)
            dbg(pg, "banesco", "transito_" + nombre)
            return P.banesco_transito(texto(pg))
    except Exception as e:
        print("  Banesco tránsito %s no leído:" % nombre, e)
    return None


def banco_banesco(page, env):
    pg = page  # directo a la banca digital (la portada www.banesco.com.pa a veces pone un reto de Cloudflare)
    pg.goto("https://bancadigital.banesco.com.pa/", wait_until="domcontentloaded", timeout=60000)
    esperar(pg); dbg(pg, "banesco", "login")
    llenar(pg, ["Usuario", "User"], env["BANESCO_USER"]); clic_texto(pg, ["Continue", "Continuar"]); esperar(pg, 2500)
    llenar(pg, ["Contraseña", "Password"], env["BANESCO_PASS"]); clic_texto(pg, ["Continue", "Continuar"])
    pg.wait_for_url("**/desktop*", timeout=180000); esperar(pg, 4000)
    dbg(pg, "banesco", "desktop")
    res = {"banco": "Banesco", "cuentas": [], "tarjetas": [], "movimientos": []}
    # Las opciones (Ver movimientos / Ver datos / Más opciones) son una capa .card-options que sale con el mouse.
    # Solo "Ver movimientos". NUNCA clic en la tarjeta en sí: tiene el interruptor "Activa".
    opciones = pg.locator(".card-options").filter(has_text="Ver movimientos")
    op = opciones.first
    op.hover(); pg.wait_for_timeout(1500)
    op.get_by_text("Ver movimientos", exact=True).first.click(); esperar(pg, 5000)
    dbg(pg, "banesco", "tarjeta_1107")
    for _ in range(15):  # "Ver más movimientos" (solo carga más filas) hasta pasar la fecha DESDE
        movs = P.banesco_tarjeta(texto(pg))["movs"]
        if not movs or min(m["fecha"] for m in movs) < DESDE:
            break
        if not clic_texto(pg, ["Ver más movimientos"], "link") and not clic_texto(pg, ["Ver más movimientos"]):
            break
        esperar(pg, 3000)
    dbg(pg, "banesco", "tarjeta_1107_completa")
    if "1107" not in texto(pg):
        raise RuntimeError("Banesco abrió movimientos de otra tarjeta (url: %s)" % pg.url)
    d = P.banesco_tarjeta(texto(pg))
    res["tarjetas"].append(limpio({"id": "banesco-1107", "deuda": d.get("deuda"), "disponible": d.get("disponible"),
                                   "corteDia": d.get("corteDia"), "pagoDia": d.get("pagoDia"),
                                   "pagoMin": d.get("pagoMin"), "saldoCorte": d.get("saldoCorte")}))
    for m in d["movs"]:
        x = mov_tarjeta(m, "Banesco Visa 1107")
        if x: res["movimientos"].append(x)
    try:  # adicional 1108 (Beatriz). Solo se pulsa el texto "Visa **** 1108", nunca el interruptor "Activa".
        clic_texto(pg, ["Ver adicionales"], "link") or clic_texto(pg, ["Ver adicionales"]); esperar(pg, 4000)
        dbg(pg, "banesco", "adicionales")
        pg.get_by_text(re.compile(r"Visa \*{4} 1108")).first.click(); esperar(pg, 4000)
        dbg(pg, "banesco", "adicional_1108")
        if "1108" in texto(pg):
            a = P.banesco_tarjeta(texto(pg))
            res["tarjetas"].append(limpio({"id": "banesco-1108", "titular": "Beatriz", "principal": "banesco-1107",
                                           "disponible": a.get("disponible")}))
            for m in a["movs"]:
                x = mov_tarjeta(m, "Banesco Visa 1108 (Beatriz)")
                if x: res["movimientos"].append(x)
            res["tarjetas"][-1]["transito"] = banesco_transito(pg, "1108")
    except Exception as e:
        print("  Banesco adicionales no leídas:", e)
    try:  # volver a la principal 1107 (mismo camino de arriba: solo "Ver movimientos") y leer su tránsito
        pg.goto("https://bancadigital.banesco.com.pa/desktop", wait_until="domcontentloaded", timeout=60000); esperar(pg, 5000)
        op = pg.locator(".card-options").filter(has_text="Ver movimientos").first
        op.hover(); pg.wait_for_timeout(1500)
        op.get_by_text("Ver movimientos", exact=True).first.click(); esperar(pg, 5000)
        if "1107" in texto(pg):
            res["tarjetas"][0]["transito"] = banesco_transito(pg, "1107")
    except Exception as e:
        print("  Banesco tránsito 1107 no leído:", e)
    return res


BANCOS = {"global": banco_global, "bac": banco_bac, "bg": banco_bg, "banesco": banco_banesco}


# ---------- salida ----------
NOMBRES = {"global-tapmax": "Tap Max 1114", "global-nolauses": "No La Uses 1115", "global-menos5000": "Menos de 5000 1116",
           "global-pagos": "Pagos 1117", "global-mc-1104": "Mastercard Black 1104", "bac-ahorros": "Ahorros 1110",
           "bac-amex-1101": "Amex 1101", "bac-mc-1102": "Mastercard 1102", "bg-yappy": "Yappy 1111",
           "bg-nolauses": "No La Uses 1112", "bg-pagomovil": "Pago Móvil 1113", "banesco-1107": "Visa 1107",
           "banesco-1108": "Visa 1108 (adicional Beatriz)"}
ENTRA = ("ACH recibido", "Yappy recibido", "Abono")


def usd(x):
    return ("-$%s" if x < 0 else "$%s") % format(abs(x), ",.2f")


def texto_correo(payload, fallidos):
    L = ["Bancos al %s (movimientos desde %s)" % (payload["fecha"], DESDE), ""]
    tot_c = tot_d = 0.0
    for b in payload["bancos"]:
        L.append("=== %s ===" % b["banco"].upper())
        for c in b["cuentas"]:
            tot_c += c["saldo"]
            L.append("  %-28s %14s" % (NOMBRES.get(c["id"], c["id"]), usd(c["saldo"])))
        for t in b["tarjetas"]:
            tot_d += t.get("deuda") or 0
            extra = []
            if t.get("deuda") is not None: extra.append("debe " + usd(t["deuda"]))
            if t.get("disponible") is not None: extra.append("disponible " + usd(t["disponible"]))
            if t.get("pagoMin") is not None: extra.append("mínimo " + usd(t["pagoMin"]))
            if t.get("pagoDia"): extra.append("paga el día %d" % t["pagoDia"])
            L.append("  %-28s %s" % (NOMBRES.get(t["id"], t["id"]), " · ".join(extra)))
        movs = sorted(b["movimientos"], key=lambda m: m["fecha"], reverse=True)
        if movs:
            L.append("  Movimientos:")
            for m in movs:
                if m["tipo"] in ("Compra", "Abono"):  # tarjeta: monto>0 compra, monto<0 abono
                    signo = -m["monto"]
                else:  # cuenta: monto siempre positivo, el tipo dice si entra o sale
                    signo = m["monto"] if m["tipo"] in ENTRA else -m["monto"]
                L.append("    %s  %11s  %-22s %s" % (m["fecha"][5:].replace("-", "/"), ("+" if signo > 0 else "") + usd(signo),
                                                    m["medio"][:22], m["comercio"][:45]))
        L.append("")
    L.append("Total en cuentas: %s" % usd(tot_c))
    L.append("Total deuda tarjetas: %s" % usd(tot_d))
    if fallidos:
        L += ["", "NO SE PUDIERON LEER: " + ", ".join(fallidos)]
    return "\n".join(L)


def enviar(env, payload, asunto, fallidos):
    if not env.get("GMAIL_USER") or not env.get("GMAIL_APP_PASS"):
        print("Falta GMAIL_USER / GMAIL_APP_PASS: no se mandó el correo."); return
    m = EmailMessage()
    m["From"] = env["GMAIL_USER"]
    m["To"] = env.get("CORREO_PARA") or env["GMAIL_USER"]  # CORREO_PARA opcional; si no, se manda a ti mismo
    m["Subject"] = asunto
    m.set_content(texto_correo(payload, fallidos))
    m.add_attachment(json.dumps(payload, ensure_ascii=False, indent=1).encode(), maintype="application",
                     subtype="json", filename="bancos-%s.json" % payload["fecha"])
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(env["GMAIL_USER"], env["GMAIL_APP_PASS"].replace(" ", "")); s.send_message(m)
    print("Correo enviado:", asunto)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo", choices=list(BANCOS)); ap.add_argument("--sin-correo", action="store_true")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--prueba", action="store_true", help="sin correo, guarda debug en ~/bancos_out/debug")
    ap.add_argument("--reintentos", type=int, default=0, help="veces que se reintenta un banco que falló")
    ap.add_argument("--espera", type=int, default=30, help="minutos entre reintentos")
    ap.add_argument("--correo-prueba", action="store_true", help="manda el correo con el último JSON guardado, sin entrar a los bancos")
    a = ap.parse_args()
    global PRUEBA
    if a.prueba:
        PRUEBA = True; a.sin_correo = True  # con --headless también sirve (servidor sin pantalla)
    env = cargar_env(); OUT.mkdir(exist_ok=True); PERFIL.mkdir(exist_ok=True)
    if a.correo_prueba:
        ult = sorted(OUT.glob("20??-??-??.json"))
        if not ult: sys.exit("No hay JSON guardado todavía.")
        enviar(env, json.loads(ult[-1].read_text()), "PI-BANCOS (prueba) " + ult[-1].stem, []); return
    if PRUEBA: DEBUG.mkdir(exist_ok=True)
    claves = {"global": "GLOBAL", "bac": "BAC", "bg": "BG", "banesco": "BANESCO"}
    faltan = [claves[b] + s for b in ([a.solo] if a.solo else BANCOS) for s in ("_USER", "_PASS") if not env.get(claves[b] + s)]
    if faltan:
        sys.exit("Faltan en credenciales.env.nosync: " + ", ".join(faltan))
    from playwright.sync_api import sync_playwright
    payload = {"fecha": HOY.isoformat(), "bancos": []}
    pendientes = [a.solo] if a.solo else list(BANCOS)
    for intento in range(a.reintentos + 1):
        if intento:
            print("Reintento %d en %d min: %s" % (intento, a.espera, ", ".join(pendientes)), flush=True)
            time.sleep(a.espera * 60)
        fallaron = []
        with sync_playwright() as pw:
            for nombre in pendientes:
                print("→", nombre, flush=True)
                ctx, page = abrir(pw, nombre, a.headless)
                try:
                    r = BANCOS[nombre](page, env)
                    payload["bancos"].append(r)
                    print("  ok: %d cuentas, %d tarjetas, %d movimientos" % (len(r["cuentas"]), len(r["tarjetas"]), len(r["movimientos"])), flush=True)
                except Exception as e:
                    fallaron.append(nombre)
                    print("  FALLÓ", nombre, ":", str(e).splitlines()[0][:300], flush=True)
                    for pg_ in ctx.pages:  # la página activa puede ser otra pestaña (BAC, Banesco)
                        try: pg_.screenshot(path=str(OUT / ("error-%s.png" % nombre)))
                        except Exception: pass
                        if PRUEBA: dbg(pg_, nombre, "ERROR")
                finally:
                    ctx.close()
        pendientes = fallaron
        if not pendientes:
            break
    orden = {"Global Bank": 0, "BAC": 1, "Banco General": 2, "Banesco": 3}
    payload["bancos"].sort(key=lambda b: orden.get(b["banco"], 9))
    payload["fallidos"] = pendientes
    f = OUT / ("%s%s.json" % (HOY.isoformat(), "-" + a.solo if a.solo else ""))
    f.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print("Guardado:", f)
    if not a.sin_correo and not a.solo:
        enviar(env, payload, "PI-BANCOS " + HOY.isoformat() + (" (faltó %s)" % ", ".join(pendientes) if pendientes else ""), pendientes)


if __name__ == "__main__":
    main()
