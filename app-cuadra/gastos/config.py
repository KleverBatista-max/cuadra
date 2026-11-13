"""Configuración: credenciales vienen de config.toml; las reglas fijas viven aquí."""
import os
import tomllib
from dataclasses import dataclass

BASE = os.environ.get("GASTOS_HOME", os.path.expanduser("~/gastos-datos"))

# ---------------- Reglas fijas acordadas con Titular ----------------

# medio -> (quien, bolsa)
TARJETAS = {
    "Banesco Visa 1107": ("Titular", "Titular"),
    "BAC MC 1102": ("Titular", "Titular"),
    "BAC Amex 1101": ("Titular", "Titular"),
    "Global 1104": ("Titular", "Titular"),
    "BAC MC 1103": ("Ana", "Titular"),   # adicional de Ana
    "Global 1105": ("Ana", "Titular"),   # adicional de Ana
    "Global 1106": ("Beatriz", "Beatriz"),       # adicional de Beatriz, declara aparte
    "Banesco Visa 1108": ("Beatriz", "Beatriz"), # adicional de Beatriz en Banesco
}

# Tarjetas que llegan al correo pero NO son de Titular: se ignoran por completo.
TARJETAS_AJENAS = {"1109"}

# Cuentas propias de Titular (transferencias entre ellas se excluyen)
CUENTAS_PROPIAS = ["00000000001", "00000000002", "00000000003", "00000000004"]

# Cómo nombra el lector de bancos cada medio -> nombre en la app
ALIAS_MEDIOS = {
    "Global MC 1104": "Global 1104",
    "Global MC 1105": "Global 1105",
    "Global MC 1106": "Global 1106",
    "Global Menos5000": "Global 1116",
    "Global Tap Max": "Global 1114",
    "Global TapMax": "Global 1114",
    "Global Pagos": "Global 1117",
    "Global No La Uses": "Global 1115",
    "Global NoLaUses": "Global 1115",
    "Global Ana y Carla": "Global 1118",
    "Banesco 1108": "Banesco Visa 1108",
}

# Personas que se muestran en la app
PERSONAS = ["Titular", "Beatriz", "Ana", "Mamá"]

# Nombre (como aparece en Yappy o en el banco) -> persona de la app
NOMBRES_PERSONA = {
    "ana demo": "Ana",
    "beatriz demo": "Beatriz",
    "beatriz demo": "Beatriz",
    "beatriz demo": "Beatriz",
}

# Remitentes válidos para los correos de la Raspberry y la nota del iPhone
REMITENTES_PROPIOS = {
    "titular@example.com",
    "titular@example.net",
    "envios@example.com",
}

# Fecha del corte en cero
CORTE = "2026-11-11"

# Cuenta +Móvil cuya factura abre el mes de cobros
MOVIL_CUENTA = "000000000000"

# Saldos que nunca se tocan
SALDOS_INTOCABLES = {"global-anacarla"}


@dataclass
class Config:
    gmail_usuario: str = ""
    gmail_clave_app: str = ""
    rclone_remoto: str = "drive"           # nombre del remoto en rclone
    drive_carpeta: str = "Gastos Demo"      # carpeta raíz en Google Drive
    secreto: str = ""                      # firma de sesiones
    borrar_correos_raspberry: bool = False # mandar a la papelera PI-BANCOS y la nota ya leídos
    dias_atras: int = 3
    puerto: int = 8080
    datos: str = BASE

    @property
    def db_path(self):
        return os.path.join(self.datos, "gastos.db")

    @property
    def pendientes(self):
        return os.path.join(self.datos, "pendientes")


def cargar(path=None):
    path = path or os.environ.get("GASTOS_CONFIG", os.path.join(BASE, "config.toml"))
    cfg = Config()
    if os.path.exists(path):
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        g = raw.get("gmail", {})
        cfg.gmail_usuario = g.get("usuario", "")
        cfg.gmail_clave_app = g.get("clave_app", "").replace(" ", "")
        d = raw.get("drive", {})
        cfg.rclone_remoto = d.get("remoto", cfg.rclone_remoto)
        cfg.drive_carpeta = d.get("carpeta", cfg.drive_carpeta)
        a = raw.get("app", {})
        cfg.secreto = a.get("secreto", "")
        cfg.borrar_correos_raspberry = bool(a.get("borrar_correos_raspberry", False))
        cfg.dias_atras = int(a.get("dias_atras", 3))
        cfg.puerto = int(a.get("puerto", 8080))
        cfg.datos = a.get("datos", os.path.dirname(os.path.abspath(path)))
    os.makedirs(cfg.pendientes, exist_ok=True)
    return cfg
