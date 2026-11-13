"""Google Drive a través de rclone (rclone guarda la autorización de la cuenta de Titular)."""
import os
import shutil
import subprocess


def disponible(cfg):
    if not shutil.which("rclone"):
        return False
    r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True)
    return f"{cfg.rclone_remoto}:" in r.stdout


def _ruta(cfg, rel):
    return f"{cfg.rclone_remoto}:{cfg.drive_carpeta}/{rel}"


def subir(cfg, local, rel):
    """Copia un archivo local a Drive. Devuelve True si quedó arriba."""
    r = subprocess.run(["rclone", "copyto", local, _ruta(cfg, rel)], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return False
    chk = subprocess.run(["rclone", "lsf", _ruta(cfg, rel)], capture_output=True, text=True, timeout=120)
    return chk.returncode == 0 and os.path.basename(rel) in chk.stdout


def bajar(cfg, rel):
    """Devuelve los bytes de un archivo en Drive (o None)."""
    r = subprocess.run(["rclone", "cat", _ruta(cfg, rel)], capture_output=True, timeout=300)
    return r.stdout if r.returncode == 0 else None


def limpiar_viejos(cfg, carpeta_rel, dias):
    subprocess.run(["rclone", "delete", "--min-age", f"{dias}d", _ruta(cfg, carpeta_rel)],
                   capture_output=True, timeout=600)
