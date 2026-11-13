"""Base de datos: SQLite con documentos JSON por colección (mismo modelo que la app actual).

Colecciones: movs, saldos, cxc, movil, avisos, sistema.
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

_lock = threading.RLock()


class DB:
    def __init__(self, path):
        self.path = path
        self.con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS docs (coll TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,"
            " version INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL, PRIMARY KEY (coll, id))"
        )
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS procesados (msgid TEXT PRIMARY KEY, cuando REAL NOT NULL, resultado TEXT)"
        )

    # ---- transacciones ----
    @contextmanager
    def tx(self):
        with _lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self.con.execute("COMMIT")
            except BaseException:
                self.con.execute("ROLLBACK")
                raise

    # ---- lectura ----
    def get(self, coll, id):
        r = self.con.execute("SELECT data FROM docs WHERE coll=? AND id=?", (coll, id)).fetchone()
        return json.loads(r[0]) if r else None

    def all(self, coll):
        rows = self.con.execute("SELECT id, data FROM docs WHERE coll=? ORDER BY id", (coll,)).fetchall()
        out = []
        for id, data in rows:
            d = json.loads(data)
            d["id"] = id
            out.append(d)
        return out

    def where(self, coll, field, op, value):
        res = []
        for d in self.all(coll):
            v = d.get(field)
            if v is None:
                continue
            if op == "==" and v == value:
                res.append(d)
            elif op == ">=" and str(v) >= str(value):
                res.append(d)
            elif op == "<=" and str(v) <= str(value):
                res.append(d)
        return res

    def exists(self, coll, id):
        return self.con.execute("SELECT 1 FROM docs WHERE coll=? AND id=?", (coll, id)).fetchone() is not None

    # ---- escritura ----
    def set(self, coll, id, data):
        data = {k: v for k, v in data.items() if k != "id"}
        with _lock:
            self.con.execute(
                "INSERT INTO docs(coll,id,data,version,updated) VALUES(?,?,?,1,?) "
                "ON CONFLICT(coll,id) DO UPDATE SET data=excluded.data, version=docs.version+1, updated=excluded.updated",
                (coll, id, json.dumps(data, ensure_ascii=False), time.time()),
            )

    def merge(self, coll, id, patch):
        """Actualiza solo los campos dados. Un valor None borra el campo."""
        with _lock:
            cur = self.get(coll, id)
            if cur is None:
                raise KeyError(f"{coll}/{id} no existe")
            for k, v in patch.items():
                if k == "id":
                    continue
                if v is None:
                    cur.pop(k, None)
                else:
                    cur[k] = v
            self.set(coll, id, cur)
            return cur

    def delete(self, coll, id):
        with _lock:
            self.con.execute("DELETE FROM docs WHERE coll=? AND id=?", (coll, id))

    def last_change(self):
        r = self.con.execute("SELECT MAX(updated) FROM docs").fetchone()
        return r[0] or 0

    # ---- correos ya procesados ----
    def ya_procesado(self, msgid):
        return self.con.execute("SELECT 1 FROM procesados WHERE msgid=?", (msgid,)).fetchone() is not None

    def marcar_procesado(self, msgid, resultado=""):
        with _lock:
            self.con.execute(
                "INSERT OR REPLACE INTO procesados(msgid,cuando,resultado) VALUES(?,?,?)",
                (msgid, time.time(), resultado[:500]),
            )

    # ---- respaldo / importación ----
    def exportar(self):
        out = {"fecha": time.strftime("%Y-%m-%dT%H:%M:%S")}
        for c in ("movs", "saldos", "cxc", "movil"):
            lst = []
            for d in self.all(c):
                d = dict(d)
                d["_id"] = d.pop("id")
                lst.append(d)
            out[c] = lst
        return out

    def importar(self, data, reemplazar=True):
        n = 0
        with self.tx():
            for c in ("movs", "saldos", "cxc", "movil"):
                if c not in data:
                    continue
                if reemplazar:
                    self.con.execute("DELETE FROM docs WHERE coll=?", (c,))
                for d in data[c]:
                    d = dict(d)
                    id = d.pop("_id", None) or d.pop("id", None)
                    if not id:
                        continue
                    self.set(c, id, d)
                    n += 1
        return n

    def respaldo_sqlite(self, destino):
        dst = sqlite3.connect(destino)
        with _lock:
            self.con.backup(dst)
        dst.close()
