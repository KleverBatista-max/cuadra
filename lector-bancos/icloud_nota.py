"""Lector de la nota "Cuentas x Cobrar" en icloud.com (root, cron cada minuto vía icloud_cron.sh).

Inicia sesión con la cuenta de iCloud dedicada (secretos.json -> icloud_web), pide el código de 6 dígitos por la app
(Más -> Conexiones) cuando Apple lo exige, marca el navegador como de confianza y guarda la sesión en /root/.icloud_perfil.
Estado visible en la app: /home/gastos/gastos-datos/icloud-estado.json. Solo lectura en iCloud.
"""
import fcntl, json, os, pwd, re, sys, time
from playwright.sync_api import sync_playwright

DATOS = "/home/gastos/gastos-datos"
SEC = DATOS + "/secretos.json"
EST = DATOS + "/icloud-estado.json"
PERFIL = "/root/.icloud_perfil"
DBG = "/root/icloud_debug"
EXPLORAR = "--explorar" in sys.argv
G = pwd.getpwnam("gastos")


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


def estado(e, texto):
    d = {"estado": e, "texto": texto, "hora": time.strftime("%d/%m %H:%M")}
    with open(EST + ".tmp", "w") as f:
        json.dump(d, f)
    os.chown(EST + ".tmp", G.pw_uid, G.pw_gid); os.chmod(EST + ".tmp", 0o640); os.replace(EST + ".tmp", EST)
    log("estado:", e, texto)


def sec():
    try:
        return json.load(open(SEC)).get("icloud_web") or {}
    except Exception:
        return {}


def foto(pg, n):
    if EXPLORAR:
        os.makedirs(DBG, exist_ok=True); pg.screenshot(path=f"{DBG}/{n}.png")


def visible(loc, ms=0):
    try:
        if ms: loc.first.wait_for(state="visible", timeout=ms)
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def clic_texto(sc, textos, ms=0):
    for t in textos:
        exacto = re.compile(r"^\s*" + t + r"\s*$", re.I)   # exacto: "Trust" no debe agarrar "Don't Trust"
        for loc in (sc.get_by_role("button", name=exacto), sc.get_by_text(exacto)):
            if visible(loc, ms):
                loc.first.click(); return True
    return False


def login(pg, s):
    estado("entrando", "Entrando a iCloud…")
    clic_texto(pg, ["Iniciar sesión", "Sign In"], 8000); pg.wait_for_timeout(3000)
    fl = pg.frame_locator("iframe#aid-auth-widget-iFrame")
    cuenta = fl.locator("#account_name_text_field")
    if not visible(cuenta, 20000):
        foto(pg, "e1-sin-formulario"); estado("error", "No encontré el formulario de inicio de sesión de Apple"); return False
    cuenta.fill(s["usuario"]); cuenta.press("Enter"); pg.wait_for_timeout(3000)
    clic_texto(fl, ["Continuar con contraseña", "Continue with Password", "Usar contraseña", "Use Password"], 3000)
    clave = fl.locator("#password_text_field")
    if not visible(clave, 15000):
        foto(pg, "e2-sin-clave"); estado("error", "Apple no mostró el campo de contraseña"); return False
    rec = fl.locator("#remember-me, input[type=checkbox]")
    try:
        if visible(rec) and not rec.first.is_checked():
            rec.first.check(force=True); log("marcado: mantener sesión iniciada")
    except Exception:
        pass
    clave.fill(s["clave"]); clave.press("Enter"); pg.wait_for_timeout(6000); foto(pg, "e3-tras-clave")
    txt = (fl.locator("body").inner_text(timeout=5000) if visible(fl.locator("body")) else "")
    if re.search(r"incorrect|incorrecta|no es correcta|bloquead|locked", txt, re.I):
        estado("error", "Apple rechazó el correo o la contraseña"); return False
    codigo = fl.locator("input.form-security-code-input, input[autocomplete='one-time-code'], input[type='tel']")
    if visible(codigo, 8000):
        pg.wait_for_timeout(3000)
        tx2 = fl.locator("body").inner_text(timeout=5000)
        if re.search(r"can.t be sent|no se pueden enviar|try again later|inténtalo más tarde|intenta de nuevo más tarde", tx2, re.I):
            foto(pg, "e3b-limite")
            estado("espera", "Apple no está mandando códigos ahora (demasiados intentos). Se reintenta solo más tarde.")
            return False
        estado("codigo", "Apple pidió el código de 6 dígitos: escríbelo en Más → Conexiones")
        t0 = time.time()
        while time.time() - t0 < 8 * 60:
            s2 = sec()
            if s2.get("codigo") and float(s2.get("codigo_ts") or 0) > t0 - 120:
                codigo.first.click(); pg.keyboard.type(s2["codigo"], delay=120); pg.wait_for_timeout(6000)
                foto(pg, "e4-tras-codigo")
                ok = clic_texto(fl, ["Confiar", "Trust"], 10000); log("confiar en el navegador:", ok); foto(pg, "e5-tras-confiar"); pg.wait_for_timeout(6000)
                break
            time.sleep(3)
        else:
            estado("error", "No llegó el código a tiempo (8 min). Vuelve a guardar para pedir otro."); return False
    pg.wait_for_timeout(5000)
    return True


def leer_nota(pg):
    """iCloud dibuja la nota en lienzos (no hay texto en la pagina): se abre, se selecciona todo y se copia.
    Ojo: las TABLAS de Notas salen vacias al copiar; la parte "Nombre Monto" tiene que ser texto, no tabla."""
    fr = next((f for f in pg.frames if "/applications/notes" in f.url), None)
    if fr is None:
        return None
    item = fr.locator(".note-list-item-title", has_text=re.compile(r"cuentas\s*x\s*cobrar", re.I))
    if not visible(item, 15000):
        return None
    item.first.click(); pg.wait_for_timeout(8000)
    ed = fr.locator(".notes-note-ed, .editor-container").first
    if not visible(ed, 10000):
        return None
    bb = ed.bounding_box()
    pg.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + 120); pg.wait_for_timeout(800)
    pg.keyboard.press("Control+A"); pg.wait_for_timeout(400); pg.keyboard.press("Control+C"); pg.wait_for_timeout(800)
    pg.keyboard.press("ArrowDown")   # quita la seleccion; no se escribe nada en la nota
    try:
        return pg.evaluate("navigator.clipboard.readText()")
    except Exception:
        return None


INTENTO = "/root/.icloud_intento"
MANUAL = "--ahora" in sys.argv


def puede_intentar():
    """Entrar pide un código por SMS: no más de un intento cada 6 h, y solo de 8 a. m. a 9 p. m. (salvo --ahora)."""
    if MANUAL:
        return True
    h = int(time.strftime("%H"))
    if h < 8 or h >= 21:
        return False
    try:
        return time.time() - os.path.getmtime(INTENTO) >= 6 * 3600
    except OSError:
        return True


def marcar_intento():
    open(INTENTO, "w").write(time.strftime("%F %T"))


def dentro(pg):
    return "/notes" in pg.url and not visible(pg.locator("iframe#aid-auth-widget-iFrame")) and not visible(pg.get_by_role("button", name=re.compile("^(Iniciar sesión|Sign In)$", re.I)))


def main():
    s = sec()
    if not s.get("usuario") or not s.get("clave"):
        return
    lk = open("/tmp/icloud.lock", "w"); fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(PERFIL, headless=False, viewport={"width": 1280, "height": 900}, locale="es-PA", permissions=["clipboard-read", "clipboard-write"])
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            pg.goto("https://www.icloud.com/notes/", wait_until="domcontentloaded", timeout=60000); pg.wait_for_timeout(8000)
            foto(pg, "e0-inicio")
            if not dentro(pg):
                if not puede_intentar():
                    estado("espera", "La sesión de iCloud venció. Se vuelve a intentar entre 8 a. m. y 9 p. m.; cuando llegue el código por SMS, escríbelo en Configuración.")
                    return
                marcar_intento()
                if not login(pg, s):
                    return
                pg.goto("https://www.icloud.com/notes/", wait_until="domcontentloaded", timeout=60000); pg.wait_for_timeout(10000)
            if not dentro(pg):
                foto(pg, "e5-no-entro"); estado("error", "No pude entrar a Notas de iCloud"); return
            estado("ok", "Sesión de iCloud activa")
            texto = leer_nota(pg)
            if texto is None:
                estado("error", "Entré a iCloud pero no encontré la nota Cuentas x Cobrar")
            elif not re.search(r"(?mi)^\s*nombre\s+monto\s*$", texto):
                estado("error", "Abrí la nota pero no encuentro la línea Nombre Monto: escríbela sola, justo arriba de la lista de deudas (si es una tabla, conviértela en texto)")
            else:
                f = DATOS + "/entrada/nota-icloud.txt"
                with open(f + ".tmp", "w") as fh:
                    fh.write(texto)
                os.chown(f + ".tmp", G.pw_uid, G.pw_gid); os.chmod(f + ".tmp", 0o600); os.replace(f + ".tmp", f)
                estado("ok", "Nota leída de iCloud")
                if EXPLORAR:
                    L = [l for l in texto.splitlines()]
                    log("   nota: líneas", len(L), "| línea 'Nombre Monto' en posición", next((i for i, l in enumerate(L) if re.fullmatch(r"\s*nombre\s+monto\s*", l, re.I)), None))
            if EXPLORAR:  # estructura de la página (sin contenido de notas)
                for f in pg.frames:
                    log("FRAME", f.url[:90])
                    try:
                        n = f.evaluate("""() => { const c = {}; document.querySelectorAll('*').forEach(e => { c[e.tagName] = (c[e.tagName]||0)+1 }); return c }""")
                        log("   etiquetas:", {k: v for k, v in n.items() if k in ("IFRAME","DIV","LI","CANVAS","ARTICLE","SPAN","BUTTON","INPUT")})
                        hits = f.evaluate("""() => [...document.querySelectorAll('*')].filter(e => e.children.length===0 && /cobrar/i.test(e.textContent||'')).slice(0,5).map(e => e.tagName+'.'+String(e.className).slice(0,60)+' role='+e.getAttribute('role')+' aria='+(e.getAttribute('aria-label')||'').slice(0,40))""")
                        log("   elementos con 'cobrar':", hits)
                        ed = f.evaluate("""() => [...document.querySelectorAll('[contenteditable=true], canvas')].slice(0,5).map(e => e.tagName+'.'+String(e.className).slice(0,60))""")
                        log("   editores/canvas:", ed)
                    except Exception as e:
                        log("   (no se pudo leer:", type(e).__name__, ")")
        except Exception as e:
            foto(pg, "e9-error"); estado("error", f"Fallo leyendo iCloud ({type(e).__name__})"); log(repr(e)[:300])
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
