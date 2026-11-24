"""Capa del asistente: cambios a mano, pero con comandos fijos, probados y con bitácora.

  gastos asistente buscar [texto] [--fecha AAAA-MM-DD] [--monto N]
  gastos asistente debe PERSONA MONTO "motivo" [--mov ID] [--fecha AAAA-MM-DD]
  gastos asistente abono PERSONA MONTO "motivo" [--fecha AAAA-MM-DD]
  gastos asistente marcar MOV_ID PERSONA|ninguna [abono]
  gastos asistente dividir MOV_ID "Beatriz, Laura" [--montos "Beatriz=15, Laura=10"]    (o «ninguna» para quitar la división)
  gastos asistente cuenta PERSONA
  gastos asistente persona-nueva NOMBRE [--alias "nombre en Yappy"] [--nota "texto"]
  gastos asistente bitacora [N]
  gastos asistente deshacer ID_BITACORA [--forzar]

Cada cambio guarda en la bitácora (colección `bitacora` + archivo bitacora-asistente.jsonl): cuándo, qué se pidió, y el
documento antes y después. `deshacer` usa eso. Respeta las reglas de Cuadra: nada de excluir/inventar reglas; lo por cobrar son
movimientos marcados `cxc` (como «Deuda anterior de X»).
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

from . import divisiones, reglas

PANAMA = timezone(timedelta(hours=-5))


class ErrorAsistente(Exception):
    pass


def _ahora():
    return datetime.now(PANAMA)


class Asistente:
    def __init__(self, db, archivo_log=None, respaldar=None):
        self.db = db
        self.archivo_log = archivo_log
        self.respaldar = respaldar   # función(ruta) opcional; en la CLI hace copia de la base antes de cambiar

    # ---------- utilidades ----------
    def persona(self, texto):
        t = reglas.norm(texto or "")
        for p in self.db.all("cxc"):
            if t in {reglas.norm(p["id"]), reglas.norm(p.get("nombre") or ""), reglas.norm(p.get("alias") or "")} - {""}:
                return p
        for nombre, pid in reglas.CXC_DE.items():
            if t == reglas.norm(nombre):
                p = self.db.get("cxc", pid)
                if p:
                    return {"id": pid, **p}
        raise ErrorAsistente(f"No conozco a '{texto}' en Por cobrar. Conocidos: "
                             + ", ".join(sorted(p["id"] for p in self.db.all("cxc"))) + ". Para agregar a alguien nuevo: persona-nueva.")

    @staticmethod
    def _monto(v):
        try:
            m = round(abs(float(str(v).replace("$", "").replace(",", ""))), 2)
        except ValueError:
            raise ErrorAsistente(f"Monto inválido: {v}")
        if m <= 0:
            raise ErrorAsistente("El monto debe ser mayor que 0.")
        return m

    @staticmethod
    def _fecha(f):
        if not f:
            return _ahora().strftime("%Y-%m-%d")
        try:
            datetime.strptime(f, "%Y-%m-%d")
        except ValueError:
            raise ErrorAsistente(f"Fecha inválida (usa AAAA-MM-DD): {f}")
        return f

    def _id_libre(self, coll, base):
        """Nunca repite un id (dos comandos en el mismo segundo)."""
        i, cand = 1, base
        while self.db.exists(coll, cand):
            i += 1
            cand = f"{base}-{i}"
        return cand

    def _guardar(self, comando, pedido, cambios):
        """cambios = [(coll, id, antes, despues)]. Registra y devuelve el id de bitácora."""
        bid = self._id_libre("bitacora", _ahora().strftime("as-%Y%m%d-%H%M%S"))
        reg = {"cuando": _ahora().isoformat(timespec="seconds"), "comando": comando, "pedido": pedido,
               "cambios": [{"coll": c, "id": i, "antes": a, "despues": d} for c, i, a, d in cambios]}
        self.db.set("bitacora", bid, reg)
        if self.archivo_log:
            with open(self.archivo_log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": bid, **reg}, ensure_ascii=False) + "\n")
        return bid

    def _aplicar(self, comando, pedido, cambios_planeados):
        """cambios_planeados = [(coll, id, doc_nuevo)] (doc_nuevo None = borrar). Hace el respaldo, aplica y registra."""
        if self.respaldar:
            self.respaldar()
        cambios = []
        for coll, i, nuevo in cambios_planeados:
            antes = self.db.get(coll, i)
            if nuevo is None:
                self.db.delete(coll, i)
            else:
                self.db.set(coll, i, nuevo)
            cambios.append((coll, i, antes, nuevo))
        reglas.recalcular_cxc(self.db)
        return self._guardar(comando, pedido, cambios)

    # ---------- comandos ----------
    def buscar(self, texto="", fecha=None, monto=None, limite=15):
        t = reglas.norm(texto)
        m = self._monto(monto) if monto else None
        out = []
        for x in self.db.all("movs"):
            if t and t not in reglas.norm(f"{x.get('comercio','')} {x.get('notas','')} {x.get('medio','')}"):
                continue
            if fecha and x.get("fecha") != fecha:
                continue
            if m is not None and abs(abs(float(x.get("monto") or 0)) - m) > 0.005:
                continue
            out.append(x)
        out.sort(key=lambda x: (x.get("fecha") or "", x.get("hora") or ""), reverse=True)
        return out[:limite]

    def _mov_manual(self, pid, signo, monto, motivo, fecha, mov_ref):
        ahora = _ahora()
        nombre = pid["nombre"] if pid.get("nombre") else pid["id"]
        mid = self._id_libre("movs", ahora.strftime("as-%Y%m%d-%H%M%S") + f"-{pid['id']}")
        nota = motivo.strip()
        if mov_ref:
            nota += f" (parte del movimiento {mov_ref})"
        return mid, {
            "fecha": fecha, "hora": ahora.strftime("%H:%M"), "mes": fecha[:7], "monto": monto, "medio": "Por cobrar",
            "tipo": "Compra" if signo == 1 else "Abono",
            "comercio": f"{nombre} debe: {motivo.strip()}" if signo == 1 else f"Abono de {nombre}: {motivo.strip()}",
            "quien": "Titular", "bolsa": "Excluido", "factura": "No aplica", "notas": nota + " [asistente]",
            "origen": "manual", "cxc": {"id": pid["id"], "signo": signo},
        }

    def debe(self, persona, monto, motivo, mov=None, fecha=None):
        p, m = self.persona(persona), self._monto(monto)
        if not (motivo or "").strip():
            raise ErrorAsistente("Falta el motivo (ej. «almuerzo en Mi Ranchito»).")
        if mov:
            orig = self.db.get("movs", mov)
            if not orig:
                raise ErrorAsistente(f"No existe el movimiento {mov}.")
            if m > abs(float(orig.get("monto") or 0)) + 0.005:
                raise ErrorAsistente(f"{m:.2f} es más que el movimiento ({abs(float(orig['monto'])):.2f}).")
            fecha = fecha or orig.get("fecha")
        mid, doc = self._mov_manual(p, 1, m, motivo, self._fecha(fecha), mov)
        bid = self._aplicar("debe", f"{p['id']} debe {m:.2f}: {motivo}" + (f" (mov {mov})" if mov else ""), [("movs", mid, doc)])
        return bid, mid, doc

    def abono(self, persona, monto, motivo="pago", fecha=None):
        p, m = self.persona(persona), self._monto(monto)
        mid, doc = self._mov_manual(p, -1, m, motivo or "pago", self._fecha(fecha), None)
        bid = self._aplicar("abono", f"{p['id']} abonó {m:.2f}: {motivo}", [("movs", mid, doc)])
        return bid, mid, doc

    def marcar(self, mov, persona, abono=False):
        orig = self.db.get("movs", mov)
        if not orig:
            raise ErrorAsistente(f"No existe el movimiento {mov}.")
        if reglas.norm(persona) in ("ninguna", "nadie", "no cuenta"):
            nuevo = {k: v for k, v in orig.items() if k not in ("cxc", "id")}
            texto = f"quitar de Por cobrar {mov}"
        else:
            p = self.persona(persona)
            nuevo = {**{k: v for k, v in orig.items() if k != "id"}, "cxc": {"id": p["id"], "signo": -1 if abono else 1}}
            texto = f"marcar {mov} como {'abono de' if abono else 'deuda de'} {p['id']}"
        bid = self._aplicar("marcar", texto, [("movs", mov, nuevo)])
        return bid, mov, nuevo

    def dividir(self, mov, personas, montos=None):
        """Divide una compra entre personas con la MISMA función «Dividir» de la página (`divisiones.py`): guarda `division` en la
        compra y las partes de cada persona salen solas (sincronizar). Titular cuenta como un participante más: «Beatriz, Laura» = tercios.
        `montos` opcional = montos fijos en dólares, ej. «Beatriz=15, Laura=10»; «titular» o «yo» fija lo de Titular; el resto se reparte parejo.
        personas='ninguna' quita la división. Reemplaza cualquier división anterior de esa compra."""
        orig = self.db.get("movs", mov)
        if not orig:
            raise ErrorAsistente(f"No existe el movimiento {mov}.")
        quitar = reglas.norm(personas if isinstance(personas, str) else "") in ("ninguna", "nadie", "quitar")
        if quitar:
            if not orig.get("division"):
                raise ErrorAsistente("Esa compra no está dividida.")
            nuevo = {k: v for k, v in orig.items() if k not in ("division", "id")}
            bid = self._aplicar("dividir", f"quitar la división de {mov}", [("movs", mov, nuevo)])
            return bid, mov, nuevo
        nombres = [x.strip() for x in (personas.replace(";", ",").split(",") if isinstance(personas, str) else personas) if str(x).strip()]
        if not nombres:
            raise ErrorAsistente("Falta con quién dividir (ej. «Beatriz, Laura»).")
        ids = []
        for n in nombres:
            pid = self.persona(n)["id"]
            if pid in ids:
                raise ErrorAsistente(f"{n} aparece dos veces.")
            ids.append(pid)
        total = abs(float(orig.get("monto") or 0))
        if total <= 0:
            raise ErrorAsistente("Esa compra no tiene monto.")
        fijos, titular = {}, None
        for trozo in [t for t in str(montos or "").replace(";", ",").split(",") if t.strip()]:
            if "=" not in trozo:
                raise ErrorAsistente(f"Monto mal escrito: «{trozo.strip()}» (usa Persona=monto, ej. Beatriz=15).")
            quien, m = trozo.split("=", 1)
            pct = round(self._monto(m) / total * 100, 4)
            if reglas.norm(quien) in ("titular", "yo", "tu"):
                titular = pct
            else:
                pid = self.persona(quien)["id"]
                if pid not in ids:
                    raise ErrorAsistente(f"{quien.strip()} tiene monto pero no está entre las personas.")
                fijos[pid] = pct
        try:
            pcts = divisiones.repartir(ids, fijos, titular)
            div = divisiones.validar(self.db, mov, orig, {"partes": [{"id": i, "pct": pcts[i]} for i in ids]})
        except divisiones.ErrorDivision as e:
            raise ErrorAsistente(str(e))
        nuevo = {**{k: v for k, v in orig.items() if k != "id"}, "division": div}
        bid = self._aplicar("dividir", f"dividir {mov} entre {', '.join(ids)}", [("movs", mov, nuevo)])
        return bid, mov, nuevo

    def division_texto(self, mov):
        """Cómo quedó repartida una compra (para confirmar al usuario)."""
        m = self.db.get("movs", mov) or {}
        partes = (m.get("division") or {}).get("partes") or []
        if not partes:
            return f"«{m.get('comercio')}» ya no está dividida."
        nombres = {p["id"]: (p.get("nombre") or p["id"]) for p in self.db.all("cxc")}
        v = divisiones.montos(m.get("monto"), partes)
        total = abs(float(m.get("monto") or 0))
        resto = round(total - sum(v.values()), 2)
        return (f"«{m.get('comercio')}» ${total:.2f}: " + ", ".join(f"{nombres.get(i, i)} ${x:.2f}" for i, x in v.items())
                + f", Titular ${resto:.2f}")

    def persona_nueva(self, nombre, alias="", nota=""):
        """Agrega una persona a Por cobrar (igual que «Nueva persona» de la página). No repite nombres ni alias."""
        nombre = (nombre or "").strip()
        if not nombre:
            raise ErrorAsistente("Falta el nombre.")
        claves = {reglas.norm(nombre)} | ({reglas.norm(alias)} if (alias or "").strip() else set())
        for p in self.db.all("cxc"):
            if claves & ({reglas.norm(p["id"]), reglas.norm(p.get("nombre") or ""), reglas.norm(p.get("alias") or "")} - {""}):
                raise ErrorAsistente(f"Ya existe «{p.get('nombre') or p['id']}» en Por cobrar.")
        base = "".join(c for c in reglas.norm(nombre) if c.isalnum()) or "persona"
        pid = self._id_libre("cxc", base)
        doc = {"nombre": nombre, "alias": (alias or "").strip(), "nota": (nota or "").strip(), "partidas": []}
        bid = self._aplicar("persona-nueva", f"agregar a {nombre} en Por cobrar", [("cxc", pid, doc)])
        return bid, pid, doc

    def cuenta(self, persona):
        p = self.persona(persona)
        return p

    def bitacora(self, n=10):
        regs = [{"id": r["id"], **{k: v for k, v in r.items() if k != "id"}} for r in self.db.all("bitacora")]
        regs.sort(key=lambda r: r["id"])
        return regs[-n:]

    def deshacer(self, bid, forzar=False):
        reg = self.db.get("bitacora", bid)
        if not reg:
            raise ErrorAsistente(f"No existe la entrada {bid} en la bitácora.")
        if reg.get("comando") == "deshacer":
            raise ErrorAsistente("Eso ya fue un «deshacer»; para repetirlo haz el cambio de nuevo.")
        if reg.get("deshecho_por"):
            raise ErrorAsistente(f"{bid} ya se deshizo ({reg['deshecho_por']}).")
        for c in reg["cambios"]:
            actual = self.db.get(c["coll"], c["id"])
            if not forzar and actual != c["despues"]:
                raise ErrorAsistente(f"{c['coll']}/{c['id']} cambió después de {bid}; no lo deshago solo (usa --forzar si estás seguro).")
        plan = [(c["coll"], c["id"], c["antes"]) for c in reversed(reg["cambios"])]
        nueva = self._aplicar("deshacer", f"deshacer {bid}", plan)
        self.db.merge("bitacora", bid, {"deshecho_por": nueva})
        return nueva


# ---------- línea de comandos ----------
def _fmt_mov(x):
    c = x.get("cxc")
    return (f"{x['id']}  {x.get('fecha')} {x.get('hora','')}  ${abs(float(x.get('monto') or 0)):.2f}  {x.get('comercio')}  "
            f"[{x.get('medio')}]" + (f"  cxc={c['id']}{'+' if c['signo']==1 else '-'}" if isinstance(c, dict) else ""))


def crear_asistente(cfg, db=None):
    """Asistente listo para usar: con copia de la base antes de cada cambio y bitácora en gastos-datos."""
    from .db import DB
    db = db or DB(cfg.db_path)
    datos = os.path.dirname(os.path.abspath(cfg.db_path))

    def respaldar():
        db.respaldo_sqlite(os.path.join(datos, time.strftime("antes-asistente-%Y-%m-%d-%H%M%S.db")))

    return Asistente(db, os.path.join(datos, "bitacora-asistente.jsonl"), respaldar)


def cli(cfg, args):
    a = crear_asistente(cfg)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, r = args[0], list(args[1:])

    def opcion(nombre):
        if nombre in r:
            i = r.index(nombre)
            v = r[i + 1] if i + 1 < len(r) else None
            del r[i:i + 2]
            return v
        return None
    try:
        if cmd == "buscar":
            fecha, monto = opcion("--fecha"), opcion("--monto")
            for x in a.buscar(" ".join(r), fecha, monto):
                print(_fmt_mov(x))
        elif cmd in ("debe", "abono"):
            mov, fecha = opcion("--mov"), opcion("--fecha")
            if len(r) < 2 or (cmd == "debe" and len(r) < 3):
                print(__doc__)
                return 2
            if cmd == "debe":
                bid, mid, doc = a.debe(r[0], r[1], " ".join(r[2:]), mov, fecha)
            else:
                bid, mid, doc = a.abono(r[0], r[1], " ".join(r[2:]) or "pago", fecha)
            print(f"Listo ({bid}): {_fmt_mov({'id': mid, **doc})}")
        elif cmd == "marcar":
            abono = "abono" in r
            r = [x for x in r if x != "abono"]
            bid, mid, doc = a.marcar(r[0], r[1], abono)
            print(f"Listo ({bid}): {_fmt_mov({'id': mid, **doc})}")
        elif cmd == "dividir":
            montos = opcion("--montos")
            if len(r) < 2:
                print(__doc__)
                return 2
            bid, mid, doc = a.dividir(r[0], " ".join(r[1:]), montos)
            print(f"Listo ({bid}): {a.division_texto(mid)}")
        elif cmd == "persona-nueva":
            alias, nota = opcion("--alias"), opcion("--nota")
            if not r:
                print(__doc__)
                return 2
            bid, pid, doc = a.persona_nueva(" ".join(r), alias or "", nota or "")
            print(f"Listo ({bid}): {doc['nombre']} agregada a Por cobrar (id {pid})")
        elif cmd == "cuenta":
            p = a.cuenta(" ".join(r))
            print(f"{p.get('nombre') or p['id']}: movimientos ${p.get('deMovs', 0):.2f}; partidas {p.get('partidas')}")
            for d in p.get("detalleMovs") or []:
                print(f"  {d['fecha']}  {d['monto']:>8.2f}  {d['comercio']}  ({d['mov']})")
        elif cmd == "bitacora":
            for g in a.bitacora(int(r[0]) if r else 10):
                print(f"{g['id']}  {g['cuando']}  {g['comando']}: {g['pedido']}" + (f"  [deshecho por {g['deshecho_por']}]" if g.get("deshecho_por") else ""))
        elif cmd == "deshacer":
            forzar = "--forzar" in r
            print(f"Listo ({a.deshacer(r[0], forzar)}): deshice {r[0]}")
        else:
            print(__doc__)
            return 2
    except ErrorAsistente as e:
        print("No lo hice:", e)
        return 1
    return 0
