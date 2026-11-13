"""Corrida periódica: lee el correo, aplica reglas, sube facturas a Drive y hace el respaldo diario.

Se ejecuta cada 5 minutos con el temporizador de systemd (gastos-worker.timer).
"""
import fcntl
import gzip
import json
import os
import re
import shutil
import sys
import time
import traceback

from . import config as C
from . import drive, reglas
from .db import DB
from .parsers import leer
from .util import ahora_panama, limpiar_comercio


def procesar_correos(cfg, db, correos, log, gmail=None):
    """Aplica una lista de Correo. Separado de IMAP para poder probarlo."""
    notas = []
    for c in correos:
        acciones = leer(c)
        ok = True
        propio = False
        es_nota = False
        for tipo, dato in acciones:
            if tipo == "mov":
                reglas.aplicar_mov(db, dict(dato), log)
            elif tipo == "pi_bancos":
                propio = True
                reglas.aplicar_bancos(db, dato, c.local.strftime("%Y-%m-%dT%H:%M"), log)
            elif tipo == "nota_cxc":
                propio = True
                notas.append((c, dato))
                es_nota = True
            elif tipo == "movil_factura":
                if reglas.asegurar_mes_movil(db, dato["mes"], dato.get("vence")):
                    log.append(f"+Móvil: abierto el mes {dato['mes']}")
            elif tipo == "factura_correo":
                reglas.aplicar_factura_correo(db, dato, log)
            elif tipo == "error":
                ok = False
                reglas.avisar(db, f"No pude leer un correo de {c.remitente} ({c.asunto}, {c.local:%d/%m %H:%M}): {dato}.",
                              clave="err-" + c.msgid)
            elif tipo == "ignorar" and "nota vacía" in str(dato):
                ok = False  # no se procesa ni se borra; puede llegar otra con datos
        if ok and not es_nota:
            db.marcar_procesado(c.msgid, ",".join(a[0] for a in acciones))
            if propio and gmail and cfg.borrar_correos_raspberry:
                gmail.a_papelera(c.msgid)
    # De las notas, solo cuenta la más reciente
    if notas:
        notas.sort(key=lambda x: x[0].fecha)
        ultima, texto = notas[-1]
        if reglas.aplicar_nota(db, texto, ultima.local.strftime("%Y-%m-%d"), log):
            for c, _ in notas:
                db.marcar_procesado(c.msgid, "nota_cxc")
                if gmail and cfg.borrar_correos_raspberry:
                    gmail.a_papelera(c.msgid)


def subir_facturas(cfg, db, log):
    if not drive.disponible(cfg):
        return
    for a in db.all("archivos"):
        if a.get("subido"):
            continue
        local = a.get("local")
        if not local or not os.path.exists(local):
            continue
        mov = next((d for d in db.where("movs", "facturaId", "==", a["id"])), None)
        edad_h = (time.time() - a.get("creado", time.time())) / 3600
        if mov is None and edad_h < 24:
            continue  # darle 24 h a que llegue su transacción (sueltas.py); después se guarda igual
        if mov:
            # En Drive solo hay dos carpetas: Beatriz (declara aparte) y Titular (todo lo demás)
            persona = "Beatriz" if mov.get("bolsa") == "Beatriz" or mov.get("quien") == "Beatriz" else "Titular"
            fecha = mov.get("fecha") or ahora_panama().strftime("%Y-%m-%d")
            nombre = f"{fecha}_{limpiar_comercio(mov.get('comercio') or 'factura')}_{float(mov.get('monto') or 0):.2f}"
        else:
            persona = "Beatriz" if a.get("persona") == "Beatriz" else "Titular"
            dd = a.get("datos") or {}   # factura sin transacción: se guarda igual, con lo leído de la factura
            fecha = dd.get("fecha") or ahora_panama().strftime("%Y-%m-%d")
            nombre = (f"{fecha}_{limpiar_comercio(dd.get('emisor') or 'factura')}_{float(dd['total']):.2f}"
                      if dd.get("total") is not None else "sin-asignar")
        nombre = re.sub(r"[^A-Za-z0-9áéíóúñÁÉÍÓÚÑ._ -]", "", nombre).replace(" ", "-")
        ext = os.path.splitext(a.get("nombre") or "")[1].lower() or ".pdf"
        rel = f"Facturas/{persona} {fecha[:4]}/{nombre}_{a['id'][-6:]}{ext}"  # una carpeta por persona y año, sin subcarpetas
        if drive.subir(cfg, local, rel):
            db.merge("archivos", a["id"], {"subido": True, "rel": rel, "local": None})
            os.remove(local)
            log.append(f"Factura subida a Drive: {rel}")


def respaldo_diario(cfg, db, log):
    hoy = ahora_panama().strftime("%Y-%m-%d")
    est = db.get("sistema", "respaldo") or {}
    if est.get("fecha") == hoy or ahora_panama().hour < 2:
        return
    tmp = os.path.join(cfg.datos, "respaldo-tmp.db")
    db.respaldo_sqlite(tmp)
    gz = os.path.join(cfg.datos, f"gastos-{hoy}.db.gz")
    with open(tmp, "rb") as f, gzip.open(gz, "wb") as g:
        shutil.copyfileobj(f, g)
    os.remove(tmp)
    js = os.path.join(cfg.datos, f"gastos-{hoy}.json")
    with open(js, "w") as f:
        json.dump(db.exportar(), f, ensure_ascii=False)
    ok = True
    if drive.disponible(cfg):
        ok = drive.subir(cfg, gz, f"Respaldo/{os.path.basename(gz)}") and drive.subir(cfg, js, f"Respaldo/{os.path.basename(js)}")
        drive.limpiar_viejos(cfg, "Respaldo", 60)
    else:
        ok = False
    # se guardan las últimas 7 copias locales por si Drive falla
    locales = sorted(f for f in os.listdir(cfg.datos) if f.startswith("gastos-20"))
    for f in locales[:-14]:
        os.remove(os.path.join(cfg.datos, f))
    if ok:
        db.set("sistema", "respaldo", {"fecha": hoy})
        log.append("Respaldo diario subido a Drive")
    else:
        reglas.avisar(db, "El respaldo de hoy no se pudo subir a Google Drive (quedó una copia en la Raspberry).",
                      clave="resp-" + hoy)


def correr(cfg):
    db = DB(cfg.db_path)
    log = []
    estado = {"inicio": ahora_panama().strftime("%Y-%m-%dT%H:%M:%S"), "ok": True}
    try:
        if cfg.gmail_usuario and cfg.gmail_clave_app:
            from .correo import Gmail
            g = Gmail(cfg.gmail_usuario, cfg.gmail_clave_app)
            try:
                correos = g.recientes(cfg.dias_atras, db.ya_procesado)
                procesar_correos(cfg, db, correos, log, gmail=g)
                try:  # nota "Cuentas x Cobrar" directo de la carpeta Notes de Gmail (solo si cambió)
                    from . import notas_gmail
                    notas_gmail.revisar(db, g, log)
                except Exception as e:
                    log.append(f"Nota en Notes: no se pudo leer ({type(e).__name__}: {e})")
            finally:
                g.cerrar()
        # Archivos de bancos dejados en la carpeta "entrada" (por si el lector de bancos corre aquí mismo)
        entrada = os.path.join(cfg.datos, "entrada")
        if os.path.isdir(entrada):
            for f in sorted(os.listdir(entrada)):
                if f.endswith(".json"):
                    p = os.path.join(entrada, f)
                    with open(p) as fh:
                        data = json.load(fh)
                    reglas.aplicar_bancos(db, data, ahora_panama().strftime("%Y-%m-%dT%H:%M"), log)
                    os.remove(p)  # ya aplicado: no se guardan archivos diarios
        try:  # nota "Cuentas x Cobrar" que el lector de iCloud dejó en entrada/ (solo si cambió)
            from . import notas_gmail
            notas_gmail.revisar_archivo(cfg, db, log)
        except Exception as e:
            log.append(f"Nota de iCloud: no se pudo aplicar ({type(e).__name__}: {e})")
        try:  # facturas por QR de la DGI que quedaron en espera (reintento cada 12 h)
            from . import facturas_dgi
            facturas_dgi.procesar_vencidos(cfg, db, log)
        except Exception as e:
            log.append(f"DGI: no se pudo reintentar ({type(e).__name__}: {e})")
        try:  # facturas subidas antes que su transacción: pegar, o a las 24 h dejarlas como Efectivo
            from . import sueltas
            sueltas.revisar(db, log)
        except Exception as e:
            log.append(f"Facturas sueltas: error ({type(e).__name__}: {e})")
        subir_facturas(cfg, db, log)
        respaldo_diario(cfg, db, log)
    except Exception as e:
        estado["ok"] = False
        estado["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        if "AUTHENTICATIONFAILED" in str(e) or "Invalid credentials" in str(e):
            reglas.avisar(db, "Gmail rechazó la contraseña de aplicación. Revisa config.toml.", clave="gmail-auth")
    estado["fin"] = ahora_panama().strftime("%Y-%m-%dT%H:%M:%S")
    estado["log"] = log[-50:]
    db.set("sistema", "estado", estado)
    for l in log:
        print(l)
    return estado


def main():
    cfg = C.cargar()
    lock = open(os.path.join(cfg.datos, ".worker.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Ya hay una corrida en curso.")
        return 0
    est = correr(cfg)
    return 0 if est["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
