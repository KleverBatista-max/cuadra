"""Servidor web: la página de cada persona y la API que usa."""
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import config as C
from . import drive, facturas
from .db import DB
from pydantic import BaseModel
from . import facturas_dgi
from . import sueltas
from . import divisiones
from . import reglas

AQUI = os.path.dirname(__file__)
COLECCIONES = {"movs", "saldos", "cxc", "avisos", "sistema"}
CAMPOS_PERSONA = {"factura", "facturaId", "facturaTipo", "facturaNombre", "facturaDatos", "notas"}
DURACION = 30 * 24 * 3600  # la sesión dura 30 días


# ---------------- contraseñas ----------------
def hash_clave(clave):
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(clave.encode(), salt=sal, n=2 ** 14, r=8, p=1)
    return base64.b64encode(sal).decode() + "$" + base64.b64encode(h).decode()


def clave_ok(clave, guardada):
    try:
        sal, h = guardada.split("$")
        calc = hashlib.scrypt(clave.encode(), salt=base64.b64decode(sal), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except Exception:
        return False


def crear_app(cfg=None):
    cfg = cfg or C.cargar()
    db = DB(cfg.db_path)
    secreto = (cfg.secreto or (db.get("sistema", "secreto") or {}).get("v") or "").encode()
    if not secreto:
        s = secrets.token_hex(32)
        db.set("sistema", "secreto", {"v": s})
        secreto = s.encode()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    intentos = {}

    # ---------------- sesión ----------------
    def firmar(usuario):
        exp = str(int(time.time()) + DURACION)
        msg = f"{usuario}|{exp}"
        sig = hmac.new(secreto, msg.encode(), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(f"{msg}|{sig}".encode()).decode()

    def usuario_de(req):
        tok = req.cookies.get("sesion")
        if not tok:
            return None
        try:
            usuario, exp, sig = base64.urlsafe_b64decode(tok.encode()).decode().split("|")
        except Exception:
            return None
        good = hmac.new(secreto, f"{usuario}|{exp}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good) or int(exp) < time.time():
            return None
        u = db.get("usuarios", usuario)
        if not u:
            return None
        return {"id": usuario, "nombre": u["nombre"], "rol": u.get("rol", "persona")}

    def exigir(req, admin=False):
        u = usuario_de(req)
        if not u:
            raise HTTPException(401, "Inicia sesión")
        if admin and u["rol"] != "admin":
            raise HTTPException(403, "Solo Titular puede hacer esto")
        return u

    def plantilla_login(error=""):
        html = open(os.path.join(AQUI, "static", "login.html"), encoding="utf-8").read()
        return html.replace("__ERROR__", error)

    @app.get("/login", response_class=HTMLResponse)
    def login_form():
        return plantilla_login()

    @app.post("/login")
    async def login(req: Request):
        form = await req.form()
        usuario = (form.get("usuario") or "").strip().lower()
        clave = form.get("clave") or ""
        ip = req.client.host if req.client else "?"
        lst = [t for t in intentos.get(ip, []) if t > time.time() - 600]
        if len(lst) >= 10:
            return HTMLResponse(plantilla_login("Demasiados intentos. Espera 10 minutos."), status_code=429)
        u = db.get("usuarios", usuario)
        if not u or not clave_ok(clave, u.get("clave", "")):
            lst.append(time.time())
            intentos[ip] = lst
            return HTMLResponse(plantilla_login("Usuario o contraseña incorrectos."), status_code=401)
        r = RedirectResponse("/", status_code=303)
        r.set_cookie("sesion", firmar(usuario), max_age=DURACION, httponly=True, samesite="lax",
                     secure=req.url.scheme == "https" or req.headers.get("x-forwarded-proto") == "https")
        return r

    @app.get("/salir")
    def salir():
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie("sesion")
        return r

    @app.get("/", response_class=HTMLResponse)
    def inicio(req: Request):
        u = usuario_de(req)
        if not u:
            return RedirectResponse("/login", status_code=303)
        html = open(os.path.join(AQUI, "static", "app.html"), encoding="utf-8").read()
        dato = json.dumps({"nombre": u["nombre"], "rol": u["rol"]}).replace("<", "\\u003c")
        return HTMLResponse(html.replace("__USUARIO_JSON__", dato), headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(os.path.join(AQUI, "static", "logo.svg"), media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})

    @app.get("/static/{nombre}")
    def estatico(nombre: str):
        if not re.fullmatch(r"[a-z]+\.(js|css|svg|png|json|wasm)", nombre):
            raise HTTPException(404)
        return FileResponse(os.path.join(AQUI, "static", nombre), headers={"Cache-Control": "no-cache"},
                            media_type="application/wasm" if nombre.endswith(".wasm") else None)

    # ---------------- API ----------------
    CXC_DE = {"Ana": "ana", "Beatriz": "beatriz"}  # persona -> su cuenta por cobrar (solo lectura)

    def visible(u, coll, d):
        if u["rol"] == "admin":
            return True
        if coll == "cxc":
            return d.get("id") == CXC_DE.get(u["nombre"])
        return coll == "movs" and d.get("quien") == u["nombre"]

    @app.get("/api/version")
    def version(req: Request):
        exigir(req)
        return {"v": db.last_change()}

    @app.get("/api/c/{coll}")
    def listar(coll: str, req: Request, f: str = None, op: str = None, v: str = None):
        u = exigir(req)
        if coll not in COLECCIONES:
            raise HTTPException(404)
        if u["rol"] != "admin" and coll not in ("movs", "cxc"):
            return []
        if coll == "sistema":
            docs = [d for d in db.all("sistema") if d["id"] == "estado"]
        elif f and op:
            docs = db.where(coll, f, op, v)
        else:
            docs = db.all(coll)
        out = []
        for d in docs:
            if visible(u, coll, d):
                d = dict(d)
                id = d.pop("id")
                out.append({"id": id, "data": d})
        return out

    def limpiar_id(id):
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,80}", id):
            raise HTTPException(400, "Identificador no válido")
        return id

    @app.put("/api/d/{coll}/{id}")
    async def poner(coll: str, id: str, req: Request):
        u = exigir(req)
        limpiar_id(id)
        data = await req.json()
        if coll not in COLECCIONES - {"sistema"}:
            raise HTTPException(404)
        if u["rol"] != "admin":
            if coll != "movs" or db.exists(coll, id) or not id.startswith("man-"):
                raise HTTPException(403, "No permitido")
            data["quien"] = u["nombre"]
            data["bolsa"] = "Beatriz" if u["nombre"] == "Beatriz" else "Titular"
            data["origen"] = "manual"
            data["tipo"] = "Compra"
        db.set(coll, id, data)
        if coll in ("movs", "cxc"):
            reglas.recalcular_cxc(db)
        return {"ok": True}

    @app.patch("/api/d/{coll}/{id}")
    async def cambiar(coll: str, id: str, req: Request):
        u = exigir(req)
        limpiar_id(id)
        data = await req.json()
        actual = db.get(coll, id) if coll in COLECCIONES else None
        if actual is None:
            raise HTTPException(404, "No existe")
        if u["rol"] != "admin":
            if coll != "movs" or actual.get("quien") != u["nombre"]:
                raise HTTPException(403, "No permitido")
            if actual.get("origen") != "manual":
                data = {k: v for k, v in data.items() if k in CAMPOS_PERSONA}
            else:
                data.update(quien=u["nombre"], bolsa="Beatriz" if u["nombre"] == "Beatriz" else "Titular")
        if coll == "sistema":
            raise HTTPException(403)
        if coll == "movs" and actual.get("divDe"):
            raise HTTPException(409, "Esto es una parte de una división: se cambia desde la transacción principal.")
        if coll == "movs" and "division" in data:
            try:
                data["division"] = divisiones.validar(db, id, {**actual, **data}, data["division"])
            except divisiones.ErrorDivision as e:
                raise HTTPException(422, str(e))
        db.merge(coll, id, data)
        if coll in ("movs", "cxc"):
            reglas.recalcular_cxc(db)
        return {"ok": True}

    @app.delete("/api/d/{coll}/{id}")
    def borrar(coll: str, id: str, req: Request):
        u = exigir(req)
        limpiar_id(id)
        actual = db.get(coll, id) if coll in COLECCIONES else None
        if actual is None:
            return {"ok": True}
        if u["rol"] != "admin" and not (coll == "movs" and actual.get("quien") == u["nombre"] and actual.get("origen") == "manual"):
            raise HTTPException(403, "No permitido")
        if coll == "sistema":
            raise HTTPException(403)
        if coll == "movs" and actual.get("divDe"):
            raise HTTPException(409, "Esto es una parte de una división: quítala desde la transacción principal.")
        db.delete(coll, id)
        if coll == "movs":
            reglas.recalcular_cxc(db)
        return {"ok": True}

    # ---------------- facturas ----------------
    @app.post("/api/subir")
    async def subir(req: Request, archivo: UploadFile = File(...)):
        u = exigir(req)
        data = await archivo.read()
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(413, "El archivo pesa más de 25 MB")
        tipo = archivo.content_type or mimetypes.guess_type(archivo.filename or "")[0] or "application/octet-stream"
        if not (tipo.startswith("image/") or tipo == "application/pdf"):
            raise HTTPException(415, "Solo fotos o PDF")
        fid = "f-" + uuid.uuid4().hex[:16]
        ext = os.path.splitext(archivo.filename or "")[1].lower()[:6] or (".pdf" if tipo == "application/pdf" else ".jpg")
        local = os.path.join(cfg.pendientes, fid + ext)
        with open(local, "wb") as f:
            f.write(data)
        db.set("archivos", fid, {"nombre": archivo.filename or fid + ext, "tipo": tipo, "local": local,
                                 "persona": u["nombre"], "creado": time.time(), "subido": False})
        datos = None
        try:
            datos = facturas.leer(archivo.filename, tipo, data)
        except Exception:
            datos = None
        if datos:
            db.merge("archivos", fid, {"datos": datos})
        return {"id": fid, "contentType": tipo, "datos": datos}

    class Espera(BaseModel):
        fid: str

    @app.post("/api/factura/esperar")
    def factura_esperar(b: Espera, req: Request, tareas: BackgroundTasks):
        """Factura subida sin transacción: se pega ya si la hay; si no, espera a que llegue (nunca crea un movimiento)."""
        u = exigir(req)
        fid = limpiar_id(b.fid)
        a = db.get("archivos", fid)
        if not a:
            raise HTTPException(404, "No existe esa factura")
        if u["rol"] != "admin" and a.get("persona") != u["nombre"]:
            raise HTTPException(403, "No permitido")
        if (a.get("datos") or {}).get("total") is None:
            raise HTTPException(422, "No pude leer el total de la factura")
        if a.get("estado") != "pegada":
            db.merge("archivos", fid, {"estado": "espera", "esperaDesde": time.time()})
        a = db.get("archivos", fid); a["id"] = fid
        mov = sueltas.intentar(db, a, []) if a.get("estado") == "espera" else None
        if mov:
            from .worker import subir_facturas
            tareas.add_task(subir_facturas, cfg, db, [])
            return {"estado": "PEGADA", "comercio": mov.get("comercio"), "fecha": mov.get("fecha")}
        return {"estado": "ESPERA"}

    class QRDGI(BaseModel):
        mov: str | None = None
        qr_url: str

    # ---------- Atajo del iPhone: llave por persona que SOLO sirve para mandar facturas por QR ----------
    def _hash_llave(t):
        return hashlib.sha256(t.encode()).hexdigest()

    @app.get("/api/atajo")
    def atajo_estado(req: Request):
        u = exigir(req)
        a = db.get("atajos", u["id"]) or {}
        return {"tiene": bool(a.get("hash")), "creada": a.get("creada")}

    @app.post("/api/atajo")
    def atajo_crear(req: Request):
        """Crea (o cambia) la llave del Atajo de quien está conectado. Se muestra una sola vez."""
        u = exigir(req)
        t = "cq_" + secrets.token_urlsafe(24)
        db.set("atajos", u["id"], {"hash": _hash_llave(t), "nombre": u["nombre"], "creada": time.strftime("%Y-%m-%d %H:%M")})
        return {"llave": t}

    @app.delete("/api/atajo")
    def atajo_borrar(req: Request):
        u = exigir(req)
        db.delete("atajos", u["id"])
        return {"ok": True}

    @app.post("/api/atajo/factura")
    async def atajo_factura(req: Request):
        """Lo llama el Atajo del iPhone (Escanear QR -> enviar). Responde texto corto para "Mostrar resultado"."""
        from fastapi.responses import PlainTextResponse
        auth = req.headers.get("authorization", "")
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        ip = req.client.host if req.client else "?"
        lst = [t for t in intentos.get("atajo-" + ip, []) if t > time.time() - 600]
        if len(lst) >= 10:
            return PlainTextResponse("Demasiados intentos. Espera 10 minutos.", status_code=429)
        quien = next((a for a in db.all("atajos") if tok and hmac.compare_digest(a.get("hash", ""), _hash_llave(tok))), None)
        if not quien:
            intentos["atajo-" + ip] = lst + [time.time()]
            return PlainTextResponse("La llave del Atajo no es válida. Crea una nueva en Cuadra.", status_code=401)
        try:
            cuerpo = await req.json()
            qr = cuerpo.get("qr") if isinstance(cuerpo, dict) else str(cuerpo)
        except Exception:
            qr = (await req.body()).decode("utf-8", "ignore")
        qr = (qr or "").strip()
        try:
            f = facturas_dgi.encolar(db, qr, None, persona=quien.get("nombre") or "Titular")
        except ValueError as e:
            return PlainTextResponse(str(e), status_code=422)
        import asyncio
        f = await asyncio.to_thread(facturas_dgi.procesar, cfg, db, f["id"])
        d = f.get("datos") or {}
        pegada = db.get("movs", f["mov"]) if f.get("mov") else None
        if f.get("estado") == "OK":
            from .worker import subir_facturas
            asyncio.get_running_loop().run_in_executor(None, subir_facturas, cfg, db, [])
            total = f"${float(d['total']):.2f}" if d.get("total") is not None else ""
            donde = f'pegada a "{pegada.get("comercio")}"' if pegada else "se pega sola cuando llegue la transacción"
            return PlainTextResponse(f"✓ Factura de {d.get('emisor') or 'la DGI'} {total} guardada, {donde}.")
        if f.get("estado") == "QUEUED":
            return PlainTextResponse("La DGI todavía no publica esta factura. Quedó en espera: se busca sola en 2 horas, mañana, a los 3 días, a los 7 días y a fin de mes.")
        return PlainTextResponse(f.get("mensaje") or "No se pudo guardar esa factura.", status_code=422)

    @app.post("/api/dgi/foto")
    async def dgi_foto(req: Request, archivo: UploadFile = File(...)):
        """Plan B del lector: foto (cámara del iPhone) -> el servidor busca el QR de la DGI con OpenCV + zxing-cpp."""
        exigir(req)
        data = await archivo.read()
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(413, "La foto pesa más de 25 MB")
        from . import qr_foto
        import asyncio
        texto = await asyncio.to_thread(qr_foto.leer_qr, data)
        return {"qr": texto}

    @app.post("/api/dgi/qr")
    def dgi_qr(b: QRDGI, req: Request, tareas: BackgroundTasks):
        """QR de la factura -> se guarda, se consulta la DGI y, si ya existe, el PDF queda en el movimiento."""
        u = exigir(req)
        mov_id = None
        if b.mov:
            mov_id = limpiar_id(b.mov)
            mov = db.get("movs", mov_id)
            if not mov:
                raise HTTPException(404, "No existe ese movimiento")
            if u["rol"] != "admin" and mov.get("quien") != u["nombre"]:
                raise HTTPException(403, "No permitido")
        try:
            f = facturas_dgi.encolar(db, b.qr_url, mov_id, persona=u["nombre"])
        except ValueError as e:
            raise HTTPException(422, str(e))
        f = facturas_dgi.procesar(cfg, db, f["id"])
        if f.get("estado") == "OK":  # el PDF de la DGI sube a Drive en el momento
            from .worker import subir_facturas
            tareas.add_task(subir_facturas, cfg, db, [])
        estado = {"OK": "OK", "QUEUED": "ESPERA", "NEEDS_MANUAL": "MANUAL"}.get(f.get("estado"), "ERROR")
        d = f.get("datos") or {}
        pegada = db.get("movs", f["mov"]) if f.get("mov") else None
        return {"estado": estado, "mensaje": f.get("mensaje"), "emisor": d.get("emisor"), "total": d.get("total"),
                "proximo": f.get("proximo"), "pegada": (pegada or {}).get("comercio"), "fecha": (pegada or {}).get("fecha")}

    @app.post("/api/dgi/{cufe}/buscar")
    def dgi_buscar(cufe: str, req: Request, tareas: BackgroundTasks):
        """Botón "Buscar ahora": consulta la DGI en el momento sin esperar el próximo intento."""
        u = exigir(req)
        if not re.fullmatch(r"[A-Za-z0-9-]{20,120}", cufe):   # el CUFE real trae guiones (FE01...-1-457490-...); sin el guion el botón no hacía nada (16 nov)
            raise HTTPException(400, "Identificador no válido")
        f = db.get("dgi", cufe)
        if not f or f.get("estado") not in ("QUEUED", "NEEDS_MANUAL"):
            raise HTTPException(404, "Esa factura ya no está en espera")
        mov = db.get("movs", f["mov"]) if f.get("mov") else None
        dueno = (mov or {}).get("quien") or f.get("persona") or "Titular"
        if u["rol"] != "admin" and dueno != u["nombre"]:
            raise HTTPException(403, "No permitido")
        ult = f.get("ultimo") or ""
        if ult and ult > (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(timespec="seconds"):
            raise HTTPException(429, "Ya se buscó hace un momento. Espera unos segundos.")
        f = facturas_dgi.procesar(cfg, db, cufe, manual=True)
        if f.get("estado") == "OK":
            from .worker import subir_facturas
            tareas.add_task(subir_facturas, cfg, db, [])
        d = f.get("datos") or {}
        pegada = db.get("movs", f["mov"]) if f.get("mov") else None
        return {"estado": {"OK": "OK", "QUEUED": "ESPERA", "NEEDS_MANUAL": "MANUAL"}.get(f.get("estado"), "ERROR"),
                "mensaje": f.get("mensaje"), "emisor": d.get("emisor"), "total": d.get("total"),
                "proximo": f.get("proximo"), "pegada": (pegada or {}).get("comercio")}

    @app.get("/api/facturas/sueltas")
    def facturas_sueltas(req: Request):
        """Facturas guardadas que todavía no tienen transacción (esperando o sin transacción)."""
        u = exigir(req)
        out = []
        for a in db.all("archivos"):
            if a.get("estado") not in ("espera", "sin-transaccion"):
                continue
            if u["rol"] != "admin" and a.get("persona") != u["nombre"]:
                continue
            d = a.get("datos") or {}
            out.append({"id": a["id"], "estado": a["estado"], "persona": a.get("persona"), "emisor": d.get("emisor"),
                        "total": d.get("total"), "fecha": d.get("fecha"), "tipo": a.get("tipo"), "creado": a.get("creado")})
        # QR escaneados sin movimiento que la DGI todavía no publica (o nunca publicó)
        import calendar
        from datetime import datetime
        for f in db.all("dgi"):
            if f.get("mov") or f.get("estado") not in ("QUEUED", "NEEDS_MANUAL"):
                continue
            if u["rol"] != "admin" and (f.get("persona") or "Titular") != u["nombre"]:
                continue
            try:
                creado = calendar.timegm(datetime.fromisoformat(f.get("creado")).utctimetuple())
            except Exception:
                creado = 0
            out.append({"id": "dgi-" + f["id"], "estado": "dgi-espera" if f["estado"] == "QUEUED" else "dgi-manual",
                        "persona": f.get("persona") or "Titular", "emisor": None, "total": None, "fecha": None,
                        "proximo": f.get("proximo"), "creado": creado})
        return sorted(out, key=lambda x: -(x.get("creado") or 0))

    def _suelta_de(u, fid):
        a = db.get("archivos", limpiar_id(fid))
        if not a or a.get("estado") not in ("espera", "sin-transaccion"):
            raise HTTPException(404, "Esa factura ya no está pendiente")
        if u["rol"] != "admin" and a.get("persona") != u["nombre"]:
            raise HTTPException(403, "No permitido")
        a["id"] = fid
        return a

    @app.get("/api/facturas/sueltas/{fid}/candidatos")
    def suelta_candidatos(fid: str, req: Request):
        """Transacciones sin factura parecidas (monto y fecha) para pegarle la factura a mano."""
        u = exigir(req)
        a = _suelta_de(u, fid)
        d = a.get("datos") or {}
        dia = sueltas._fecha_factura(a)
        total = d.get("total")
        out = []
        for m in db.all("movs"):
            if m.get("facturaId") or m.get("tipo") in sueltas.ENTRANTES or m.get("bolsa") == "Excluido":
                continue
            if u["rol"] != "admin" and m.get("quien") != u["nombre"]:
                continue
            dm = sueltas._dia(m.get("fecha"))
            if not dm or abs((dm - dia).days) > 45:
                continue
            dif = abs(float(m.get("monto") or 0) - float(total)) if total is not None else 0
            out.append({"id": m["id"], "fecha": m.get("fecha"), "comercio": m.get("comercio"), "monto": m.get("monto"),
                        "medio": m.get("medio"), "quien": m.get("quien"), "_k": (dif, abs((dm - dia).days))})
        out.sort(key=lambda x: x.pop("_k"))
        return out[:25]

    class PegarSuelta(BaseModel):
        mov: str

    @app.post("/api/facturas/sueltas/{fid}/pegar")
    def suelta_pegar(fid: str, b: PegarSuelta, req: Request, tareas: BackgroundTasks):
        u = exigir(req)
        a = _suelta_de(u, fid)
        mov = db.get("movs", limpiar_id(b.mov))
        if not mov or mov.get("facturaId"):
            raise HTTPException(422, "Esa transacción no existe o ya tiene factura")
        if u["rol"] != "admin" and mov.get("quien") != u["nombre"]:
            raise HTTPException(403, "No permitido")
        mov["id"] = b.mov
        sueltas.pegar(db, a, mov, [])
        from .worker import subir_facturas
        tareas.add_task(subir_facturas, cfg, db, [])
        return {"ok": True, "comercio": mov.get("comercio")}

    @app.post("/api/facturas/sueltas/{fid}/aceptar")
    def suelta_aceptar(fid: str, req: Request):
        """La quita de la lista. La factura sigue guardada (y sube a Drive igual)."""
        u = exigir(req)
        if fid.startswith("dgi-"):  # QR que la DGI no publica: se deja de buscar
            cufe = fid[4:]
            f = db.get("dgi", cufe)
            if not f or f.get("mov") or f.get("estado") not in ("QUEUED", "NEEDS_MANUAL"):
                raise HTTPException(404, "Esa factura ya no está pendiente")
            if u["rol"] != "admin" and (f.get("persona") or "Titular") != u["nombre"]:
                raise HTTPException(403, "No permitido")
            db.merge("dgi", cufe, {"estado": "CANCELADA", "proximo": None, "mensaje": "Quitada por " + u["nombre"]})
            return {"ok": True}
        _suelta_de(u, fid)
        db.merge("archivos", fid, {"estado": "aceptada", "aceptada": time.strftime("%Y-%m-%d %H:%M")})
        return {"ok": True}

    @app.get("/_blob/{fid}")
    def ver_factura(fid: str, req: Request):
        u = exigir(req)
        limpiar_id(fid)
        a = db.get("archivos", fid)
        if not a:
            raise HTTPException(404)
        if u["rol"] != "admin":
            mov = next(iter(db.where("movs", "facturaId", "==", fid)), None)
            if a.get("persona") != u["nombre"] and not (mov and mov.get("quien") == u["nombre"]):
                raise HTTPException(403)
        if a.get("local") and os.path.exists(a["local"]):
            return FileResponse(a["local"], media_type=a.get("tipo"))
        if a.get("rel"):
            data = drive.bajar(cfg, a["rel"])
            if data is not None:
                return Response(data, media_type=a.get("tipo"))
        raise HTTPException(404, "No se encontró el archivo")

    # ---------------- respaldo ----------------
    @app.get("/api/respaldo")
    def respaldo(req: Request):
        exigir(req, admin=True)
        return JSONResponse(db.exportar())

    @app.post("/api/importar")
    async def importar(req: Request):
        exigir(req, admin=True)
        data = await req.json()
        if not isinstance(data, dict) or "movs" not in data:
            raise HTTPException(400, "Ese archivo no parece un respaldo de la app")
        n = db.importar(data, reemplazar=True)
        return {"ok": True, "documentos": n}

    return app
