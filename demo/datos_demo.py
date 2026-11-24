"""Crea una base de DEMOSTRACIÓN de Cuadra con datos inventados.

Sirve para sacar capturas y videos sin enseñar cuentas reales. Uso (desde la carpeta app-cuadra/):

    venv/bin/python ../demo/datos_demo.py            # crea /tmp/cuadra-demo con usuario demo / demo1234
    GASTOS_CONFIG=/tmp/cuadra-demo/config.toml venv/bin/python -m gastos servidor   # la sirve en :18099
"""
import argparse
import os
import random
import sys

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--app", default=os.getcwd(), help="carpeta app-cuadra (donde está el paquete gastos)")
ap.add_argument("--carpeta", default="/tmp/cuadra-demo", help="dónde crear la base y el config.toml")
ap.add_argument("--puerto", type=int, default=18099)
ap.add_argument("--clave", default="demo1234")
a = ap.parse_args()

sys.path.insert(0, a.app)
from gastos.db import DB          # noqa: E402
from gastos.web import hash_clave  # noqa: E402

random.seed(7)  # siempre los mismos datos
os.makedirs(a.carpeta, exist_ok=True)
with open(os.path.join(a.carpeta, "config.toml"), "w") as f:
    f.write(f'[app]\nsecreto = "demo-demo-demo-demo"\npuerto = {a.puerto}\ndatos = "{a.carpeta}"\n')
ruta = os.path.join(a.carpeta, "gastos.db")
for ext in ("", "-wal", "-shm"):
    if os.path.exists(ruta + ext):
        os.remove(ruta + ext)
db = DB(ruta)
db.set("usuarios", "demo", {"nombre": "Demo", "rol": "admin", "clave": hash_clave(a.clave)})

AHORA = "2026-11-23T19:42"
saldos = {
    "bg-yappy": {"banco": "Banco General", "desde": AHORA, "medios": ["Yappy", "BG 1111"], "nombre": "Cuenta Yappy", "orden": 1, "saldo": 412.50, "tipo": "cuenta"},
    "bg-ahorro": {"banco": "Banco General", "contar": True, "desde": AHORA, "medios": ["BG 1113"], "nombre": "Ahorros", "orden": 3, "saldo": 2850.00, "tipo": "cuenta"},
    "bac-ahorros": {"banco": "BAC", "contar": True, "desde": AHORA, "medios": ["BAC 1110"], "nombre": "Cuenta de ahorros", "orden": 10, "saldo": 1320.40, "tipo": "cuenta"},
    "global-principal": {"banco": "Global Bank", "desde": AHORA, "medios": ["Global 1114"], "nombre": "Cuenta principal", "orden": 30, "saldo": 5400.00, "tipo": "cuenta"},
    "bac-mc-1102": {"banco": "BAC", "corteDia": 9, "desde": AHORA, "deuda": 642.18, "disponible": 9357.82, "grupo": "BAC", "limite": 10000,
                    "medios": ["BAC MC 1102"], "nombre": "Mastercard", "orden": 12, "pagoDia": 25, "tipo": "tarjeta", "pagoMin": 35.0, "saldoCorte": 410.0,
                    "transito": [{"fecha": "2026-11-22", "desc": "Compra en Proceso", "monto": 35.0}], "financiado": 0, "planes": []},
    "banesco-1107": {"banco": "Banesco", "corteDia": 16, "desde": AHORA, "deuda": 1180.77, "disponible": 8819.23, "limite": 10000,
                     "medios": ["Banesco Visa 1107"], "nombre": "Visa", "orden": 20, "pagoDia": 12, "pagoMin": 50.0, "saldoCorte": 960.0, "tipo": "tarjeta", "transito": []},
    "global-mc-1104": {"banco": "Global Bank", "corteDia": 25, "desde": AHORA, "deuda": 318.60, "disponible": 6681.40, "limite": 7000,
                       "medios": ["Global 1104"], "nombre": "Mastercard", "orden": 35, "pagoDia": 20, "pagoMin": 25.0, "tipo": "tarjeta"},
}
for k, v in saldos.items():
    db.set("saldos", k, v)

comercios = ["Supermercado Rey", "Farmacia Arrocha", "Gasolinera", "Café Unido", "Cine", "Restaurante La Casona", "Netflix", "Uber", "Tienda de ropa", "Ferretería"]
medios = ["BAC MC 1102", "Banesco Visa 1107", "Global 1104"]


def mov(id, fecha, hora, monto, medio, tipo, comercio, **extra):
    d = {"fecha": fecha, "hora": hora, "mes": fecha[:7], "monto": monto, "medio": medio, "tipo": tipo, "comercio": comercio,
         "quien": "Titular", "bolsa": "Titular", "factura": "No aplica", "notas": "", "origen": "correo"}
    d.update(extra)
    db.set("movs", id, d)


n = 0
for dia in range(1, 24):
    for _ in range(random.choice([1, 2, 2, 3])):
        n += 1
        mov(f"d{n}", f"2026-11-{dia:02d}", f"{random.randint(8, 21):02d}:{random.randint(0, 59):02d}", round(random.uniform(4, 95), 2),
            random.choice(medios), "Compra", random.choice(comercios))
# una compra con factura pendiente: la que escanea el video (su total coincide con el recibo de camara_falsa.py)
mov("d99", "2026-11-23", "18:42", 42.75, "Banesco Visa 1107", "Compra", "Supermercado Rey", factura="Pendiente")
mov("y1", "2026-11-20", "12:10", 25.0, "Yappy", "Yappy recibido", "Yappy de Ana", origen="banco")

deudas = [("ana", "Ana", "x1", "2026-11-18", "Ana debe: mitad de la cena", 28.0), ("ana", "Ana", "x2", "2026-11-21", "Ana debe: entradas del cine", 17.5),
          ("luis", "Luis", "x3", "2026-11-19", "Luis debe: gasolina del viaje", 30.0), ("carla", "Carla", "x4", "2026-11-22", "Carla debe: 1/3 del taxi", 12.0)]
for pid, nombre, mid, fecha, texto, monto in deudas:
    mov(mid, fecha, "10:00", monto, "Por cobrar", "Compra", texto, bolsa="Excluido", origen="manual", cxc={"id": pid, "signo": 1})
for pid, nombre in (("ana", "Ana"), ("luis", "Luis"), ("carla", "Carla")):
    det = [{"mov": m, "fecha": f, "comercio": t, "monto": mo} for p, _, m, f, t, mo in deudas if p == pid]
    db.set("cxc", pid, {"nombre": nombre, "alias": "", "nota": "", "partidas": [], "deMovs": sum(d["monto"] for d in det), "detalleMovs": det})

db.set("avisos", "av1", {"fecha": "2026-11-23", "texto": "Compra de $35.00 en tránsito en BAC Mastercard. Confirma que llegue mañana.", "tipo": "revisar", "visto": False})
db.set("avisos", "av2", {"fecha": "2026-11-23", "texto": "Falta la factura de Supermercado Rey por $42.75.", "tipo": "revisar", "visto": False})
print(f"listo: {a.carpeta} ({len(db.all('movs'))} movimientos). Usuario demo / {a.clave}")
