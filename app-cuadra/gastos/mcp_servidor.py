"""Conector MCP de Cuadra: deja que Claude (app del teléfono / claude.ai) use `gastos asistente` desde un chat.

Solo expone los comandos fijos del asistente (nada de ejecutar cosas libres). Autenticación: OAuth 2.0 (el que exige claude.ai);
al conectar, Titular escribe UNA vez su clave de Cuadra (usuario `titular`, solo si es admin). Tokens guardados con hash en
gastos-datos/mcp-oauth.json (0600). Tras 5 claves malas seguidas el login se bloquea 15 minutos.

  python -m gastos mcp          (lo corre systemd: cuadra-mcp.service, 127.0.0.1:9100, detrás de Tailscale Funnel)
"""
import functools
import hashlib
import html
import json
import os
import secrets
import time

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken, TokenError,
                                      construct_redirect_uri)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from .asistente import Asistente, ErrorAsistente, _fmt_mov, crear_asistente
from .web import clave_ok

BASE = os.environ.get("CUADRA_MCP_BASE", "https://mi-servidor.example.ts.net")
PUERTO = int(os.environ.get("CUADRA_MCP_PUERTO", "9100"))
ACCESO_SEG, REFRESCO_SEG, CODIGO_SEG, PENDIENTE_SEG = 12 * 3600, 90 * 86400, 300, 600
MAX_CLIENTES, MAX_FALLOS, BLOQUEO_SEG = 50, 5, 900


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Almacen:
    """Clientes registrados y tokens (con hash) en un JSON; los códigos de autorización viven solo en memoria."""

    def __init__(self, ruta):
        self.ruta = ruta
        self.d = {"clientes": {}, "tokens": {}}
        try:
            self.d = json.load(open(ruta, encoding="utf-8"))
        except (OSError, ValueError):
            pass

    def guardar(self):
        tmp = self.ruta + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.d, f)
        os.replace(tmp, self.ruta)


class Proveedor:
    def __init__(self, db, almacen):
        self.db, self.alm = db, almacen
        self.codigos, self.pendientes = {}, {}
        self.fallos, self.bloqueado_hasta = [], 0.0

    # --- clientes (registro dinámico) ---
    async def get_client(self, client_id):
        c = self.alm.d["clientes"].get(client_id)
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def register_client(self, client_info):
        cl = self.alm.d["clientes"]
        cl[client_info.client_id] = client_info.model_dump(mode="json")
        while len(cl) > MAX_CLIENTES:
            cl.pop(next(iter(cl)))
        self.alm.guardar()

    # --- autorización: manda a la página de clave ---
    async def authorize(self, client, params: AuthorizationParams):
        rid = secrets.token_urlsafe(24)
        self.pendientes[rid] = (client, params, time.time() + PENDIENTE_SEG)
        return f"{BASE}/cuadra-login?req={rid}"

    def pendiente(self, rid):
        p = self.pendientes.get(rid)
        if not p or p[2] < time.time():
            self.pendientes.pop(rid, None)
            return None
        return p

    def clave_valida(self, clave):
        ahora = time.time()
        if ahora < self.bloqueado_hasta:
            return None
        u = self.db.get("usuarios", "titular")
        ok = bool(u and u.get("rol") == "admin" and clave_ok(clave or "", u.get("clave", "")))
        if ok:
            self.fallos = []
            return True
        self.fallos = [t for t in self.fallos if ahora - t < BLOQUEO_SEG] + [ahora]
        if len(self.fallos) >= MAX_FALLOS:
            self.bloqueado_hasta, self.fallos = ahora + BLOQUEO_SEG, []
        return False

    def emitir_codigo(self, rid):
        client, p, _ = self.pendientes.pop(rid)
        code = secrets.token_urlsafe(32)
        self.codigos[code] = AuthorizationCode(
            code=code, scopes=p.scopes or [], expires_at=time.time() + CODIGO_SEG, client_id=client.client_id,
            code_challenge=p.code_challenge, redirect_uri=p.redirect_uri,
            redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly, resource=p.resource, subject="titular")
        return construct_redirect_uri(str(p.redirect_uri), code=code, state=p.state)

    async def load_authorization_code(self, client, authorization_code):
        c = self.codigos.get(authorization_code)
        if not c or c.client_id != client.client_id or c.expires_at < time.time():
            return None
        return c

    # --- tokens ---
    def _emitir(self, client_id, scopes, resource):
        acc, ref = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        ahora = int(time.time())
        t = self.alm.d["tokens"]
        for k in [k for k, v in t.items() if v["exp"] < ahora]:
            del t[k]
        t[_h(acc)] = {"tipo": "acceso", "cliente": client_id, "scopes": scopes, "exp": ahora + ACCESO_SEG, "res": resource}
        t[_h(ref)] = {"tipo": "refresco", "cliente": client_id, "scopes": scopes, "exp": ahora + REFRESCO_SEG, "res": resource}
        self.alm.guardar()
        return OAuthToken(access_token=acc, expires_in=ACCESO_SEG, scope=" ".join(scopes) or None, refresh_token=ref)

    async def exchange_authorization_code(self, client, authorization_code):
        self.codigos.pop(authorization_code.code, None)
        return self._emitir(client.client_id, authorization_code.scopes, authorization_code.resource)

    def _buscar(self, token, tipo):
        v = self.alm.d["tokens"].get(_h(token))
        if not v or v["tipo"] != tipo or v["exp"] < time.time():
            return None
        return v

    async def load_refresh_token(self, client, refresh_token):
        v = self._buscar(refresh_token, "refresco")
        if not v or v["cliente"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=v["cliente"], scopes=v["scopes"], expires_at=v["exp"],
                            resource=v["res"], subject="titular")

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.alm.d["tokens"].pop(_h(refresh_token.token), None)   # rota el refresco
        nuevos = scopes or refresh_token.scopes
        if not set(nuevos) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope")
        return self._emitir(client.client_id, nuevos, refresh_token.resource)

    async def load_access_token(self, token):
        v = self._buscar(token, "acceso")
        if not v:
            return None
        return AccessToken(token=token, client_id=v["cliente"], scopes=v["scopes"], expires_at=v["exp"],
                           resource=v["res"], subject="titular")

    async def revoke_token(self, token):
        self.alm.d["tokens"].pop(_h(token.token), None)
        self.alm.guardar()


PAGINA = """<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Conectar Claude a Cuadra</title><body style="font-family:system-ui;max-width:420px;margin:12vh auto;padding:0 20px;background:#0f1412;color:#e8efe9">
<h2>Conectar Claude a Cuadra</h2><p>«{cliente}» quiere usar el asistente de Cuadra (buscar, registrar «me debe», abonos y deshacer).
Escribe tu clave de Cuadra para permitirlo.</p>{aviso}
<form method="post"><input type="hidden" name="req" value="{rid}">
<input name="clave" type="password" autofocus placeholder="Clave de Cuadra" style="width:100%;padding:12px;font-size:16px;margin:8px 0">
<button style="width:100%;padding:12px;font-size:16px">Permitir</button></form></body></html>"""


def crear_servidor(cfg):
    from .db import DB
    db = DB(cfg.db_path)
    alm = Almacen(os.path.join(os.path.dirname(os.path.abspath(cfg.db_path)), "mcp-oauth.json"))
    prov = Proveedor(db, alm)
    asis = crear_asistente(cfg, db)
    host = BASE.split("://", 1)[1]
    mcp = MCPServer(
        "Cuadra",
        instructions=("Asistente de Cuadra (gastos de Titular). Para registrar algo sobre un movimiento: primero `buscar` para sacar "
                      "su id; si falta un dato (¿me deben o ya pagó?, ¿a quién?, ¿cuánto?) pregunta antes de escribir. "
                      "Después de un cambio, confirma con `cuenta`. Habla en español, simple y directo."),
        auth_server_provider=prov,
        auth=AuthSettings(issuer_url=BASE, resource_server_url=f"{BASE}/mcp",
                          client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=["cuadra"],
                                                                                default_scopes=["cuadra"]),
                          required_scopes=["cuadra"], validate_token_resource=False))

    @mcp.custom_route("/cuadra-login", methods=["GET", "POST"])
    async def login(request: Request):
        if request.method == "GET":
            rid, clave = request.query_params.get("req", ""), None
        else:
            f = await request.form()
            rid, clave = str(f.get("req", "")), str(f.get("clave", ""))
        p = prov.pendiente(rid)
        if not p:
            return HTMLResponse("<p style='font-family:system-ui'>Este enlace venció. Vuelve a conectar desde Claude.</p>", 400)
        pagina = lambda aviso="": HTMLResponse(PAGINA.format(cliente=html.escape(p[0].client_name or "Claude"), rid=html.escape(rid), aviso=aviso))
        if clave is None:
            return pagina()
        r = prov.clave_valida(clave)
        if r is None:
            return pagina("<p style='color:#ffb4a8'>Demasiados intentos. Espera 15 minutos.</p>")
        if not r:
            return pagina("<p style='color:#ffb4a8'>Clave incorrecta.</p>")
        return RedirectResponse(prov.emitir_codigo(rid), 302)

    def seguro(fn):
        @functools.wraps(fn)   # conserva la firma: de ahí sale el esquema de parámetros de la herramienta
        def f(*a, **k):
            try:
                return fn(*a, **k)
            except ErrorAsistente as e:
                return f"No lo hice: {e}"
        return f

    L = {"readOnlyHint": True}

    @mcp.tool(annotations=L)
    @seguro
    def buscar(texto: str = "", fecha: str = "", monto: str = "") -> str:
        """Busca movimientos por texto (comercio/nota/medio), fecha AAAA-MM-DD o monto. Devuelve id, fecha, monto y comercio."""
        r = asis.buscar(texto, fecha or None, monto or None)
        return "\n".join(_fmt_mov(x) for x in r) or "Sin resultados."

    @mcp.tool()
    @seguro
    def debe(persona: str, monto: str, motivo: str, mov: str = "", fecha: str = "") -> str:
        """Registra que PERSONA le debe MONTO a Titular como deuda suelta (si la cuenta se DIVIDIÓ entre varias personas usa `dividir`, no esto). `mov` = id del movimiento original (opcional
        pero recomendado; el gasto original no se toca). Crea un movimiento manual «Por cobrar»."""
        bid, mid, doc = asis.debe(persona, monto, motivo, mov or None, fecha or None)
        return f"Listo ({bid}): {_fmt_mov({'id': mid, **doc})}"

    @mcp.tool()
    @seguro
    def abono(persona: str, monto: str, motivo: str = "pago", fecha: str = "") -> str:
        """Registra que PERSONA le pagó (abonó) MONTO a Titular, restándolo de lo que debe."""
        bid, mid, doc = asis.abono(persona, monto, motivo, fecha or None)
        return f"Listo ({bid}): {_fmt_mov({'id': mid, **doc})}"

    @mcp.tool()
    @seguro
    def marcar(mov: str, persona: str, abono: bool = False) -> str:
        """Marca un movimiento existente como deuda de PERSONA (o como abono si abono=true). persona='ninguna' lo quita de Por cobrar."""
        bid, mid, doc = asis.marcar(mov, persona, abono)
        return f"Listo ({bid}): {_fmt_mov({'id': mid, **doc})}"

    @mcp.tool()
    @seguro
    def dividir(mov: str, personas: str, montos: str = "") -> str:
        """Divide una COMPRA entre varias personas, con la misma función «Dividir» de la página (Titular cuenta como un participante más:
        «Beatriz, Laura» = tercios; cada parte sale sola en Por cobrar). `mov` = id de la compra (sácalo con `buscar`). `personas` =
        nombres separados por coma. `montos` opcional = montos fijos en dólares, ej. «Beatriz=15, Laura=10» (quien no tenga monto
        se reparte parejo; «yo=10» fija lo de Titular). personas='ninguna' quita la división. Reemplaza una división anterior.
        Usa esto para «dividimos la cuenta entre X y Y»; `debe` es solo para una deuda suelta que no es una división de esa compra."""
        bid, mid, _ = asis.dividir(mov, personas, montos or None)
        return f"Listo ({bid}): {asis.division_texto(mid)}"

    @mcp.tool()
    @seguro
    def persona_nueva(nombre: str, alias: str = "", nota: str = "") -> str:
        """Agrega una PERSONA NUEVA a Por cobrar (para poder usar debe/abono/marcar con ella). `alias` = nombre con que aparece en
        su Yappy (opcional). Se niega si ya existe alguien con ese nombre o alias."""
        bid, pid, doc = asis.persona_nueva(nombre, alias, nota)
        return f"Listo ({bid}): {doc['nombre']} agregada a Por cobrar (id {pid})."

    @mcp.tool(annotations=L)
    @seguro
    def cuenta(persona: str) -> str:
        """Muestra cuánto debe PERSONA (movimientos marcados + partidas) y el detalle."""
        p = asis.cuenta(persona)
        det = "\n".join(f"  {d['fecha']}  {d['monto']:.2f}  {d['comercio']}  ({d['mov']})" for d in p.get("detalleMovs") or [])
        return f"{p.get('nombre') or p['id']}: movimientos ${p.get('deMovs', 0):.2f}; partidas {p.get('partidas')}\n{det}"

    @mcp.tool(annotations=L)
    @seguro
    def resumen() -> str:
        """Los números de la pantalla de inicio de Cuadra: Neto, Liquidez, Por cobrar (y quién debe), Debo en tarjetas y Gasto del mes."""
        from . import resumen as R
        return R.texto_resumen(R.calcular(asis.db))

    @mcp.tool(annotations=L)
    @seguro
    def tarjetas(filtro: str = "") -> str:
        """Tarjetas de crédito: cuánto se debe, límite y disponible, fecha de corte y de pago (cuántos días faltan). `filtro` opcional:
        banco, nombre o últimos 4 dígitos (ej. «bac», «1107»). Sirve para «cuál toca pagar / cortar primero»."""
        from . import resumen as R
        return R.texto_tarjetas(R.calcular(asis.db), filtro)

    @mcp.tool(annotations=L)
    @seguro
    def cuentas_banco(filtro: str = "") -> str:
        """Cuentas (ahorros, Yappy, etc.): saldo disponible de cada una y cuáles cuentan en la liquidez. `filtro` opcional: banco, nombre o número."""
        from . import resumen as R
        return R.texto_cuentas(R.calcular(asis.db), filtro)

    @mcp.tool(annotations=L)
    @seguro
    def bitacora(n: int = 10) -> str:
        """Últimos cambios hechos con el asistente (id, cuándo, qué se pidió)."""
        return "\n".join(f"{g['id']}  {g['cuando']}  {g['comando']}: {g['pedido']}"
                         + (f"  [deshecho por {g['deshecho_por']}]" if g.get("deshecho_por") else "") for g in asis.bitacora(n)) or "Vacía."

    @mcp.tool()
    @seguro
    def deshacer(id_bitacora: str, forzar: bool = False) -> str:
        """Deshace un cambio de la bitácora (devuelve el documento a como estaba). Se niega si cambió después, salvo forzar=true."""
        return f"Listo ({asis.deshacer(id_bitacora, forzar)}): deshice {id_bitacora}"

    ts = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[host, f"{host}:443", f"127.0.0.1:{PUERTO}", f"localhost:{PUERTO}"],
                                   allowed_origins=["https://claude.ai", "https://claude.com", BASE])
    return mcp, prov, ts


def servir(cfg):
    import uvicorn
    mcp, _, ts = crear_servidor(cfg)
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, transport_security=ts)
    uvicorn.run(app, host="127.0.0.1", port=PUERTO, proxy_headers=True, forwarded_allow_ips="127.0.0.1", log_level="warning")
