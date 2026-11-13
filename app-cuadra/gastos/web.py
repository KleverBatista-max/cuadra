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

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import config as C
from . import drive, facturas
from .db import DB
from pydantic import BaseModel
from . import facturas_dgi
from . import conexiones
from . import sueltas

AQUI = os.path.dirname(__file__)
COLECCIONES = {"movs", "saldos", "cxc", "movil", "avisos", "sistema"}
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
        if not re.fullmatch(r"[a-z]+\.(js|css|svg|png|json)", nombre):
            raise HTTPException(404)
        return FileResponse(os.path.join(AQUI, "static", nombre), headers={"Cache-Control": "no-cache"})

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
        db.merge(coll, id, data)
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
        db.delete(coll, id)
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
        """Factura subida sin transacción: se pega ya si la hay; si no, espera (24 h, luego Efectivo)."""
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

    class Conexion(BaseModel):
        servicio: str
        usuario: str
        clave: str

    @app.get("/api/conexiones")
    def ver_conexiones(req: Request):
        exigir(req, admin=True)
        return conexiones.resumen(cfg)

    @app.post("/api/conexiones")
    def poner_conexion(b: Conexion, req: Request):
        """Titular pone una clave desde la app; se guarda solo en el VPS y se prueba al momento."""
        exigir(req, admin=True)
        if b.servicio == "icloud_web":  # inicio de sesión normal en icloud.com (lo usa el lector de la nota)
            if not b.usuario.strip() or not b.clave.strip():
                raise HTTPException(422, "Escribe el correo y la contraseña")
            conexiones.guardar(cfg, "icloud_web", {"usuario": b.usuario.strip(), "clave": b.clave,
                                                   "guardada": time.strftime("%Y-%m-%d %H:%M"), "codigo": None})
            conexiones.pedir_ahora(cfg)
            return {"ok": True, "mensaje": "Guardado. Entrando a iCloud… si Apple pide un código, aparece aquí abajo."}
        if b.servicio != "icloud":
            raise HTTPException(422, "Servicio no soportado")
        if not b.usuario.strip() or not b.clave.strip():
            raise HTTPException(422, "Escribe el correo y la contraseña")
        prueba = conexiones.probar_icloud(b.usuario, b.clave)
        conexiones.guardar(cfg, "icloud", {"usuario": b.usuario.strip(), "clave": b.clave.strip(),
                                           "guardada": time.strftime("%Y-%m-%d %H:%M"),
                                           "prueba": {"ok": prueba.get("ok"), "mensaje": prueba.get("mensaje")}})
        return prueba

    class Codigo(BaseModel):
        codigo: str

    @app.post("/api/conexiones/codigo")
    def poner_codigo(b: Codigo, req: Request):
        exigir(req, admin=True)
        cod = re.sub(r"\D", "", b.codigo or "")
        if len(cod) != 6:
            raise HTTPException(422, "El código tiene 6 dígitos")
        conexiones.guardar(cfg, "icloud_web", {"codigo": cod, "codigo_ts": time.time()})
        return {"ok": True, "mensaje": "Código enviado. Revisando…"}

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
