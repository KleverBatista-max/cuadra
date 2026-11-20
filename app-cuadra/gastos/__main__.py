"""Comandos:  gastos <comando>

  servidor                      arranca la página web (lo hace systemd solo)
  revisar                       lee el correo una vez ahora y muestra qué hizo
  usuario <usuario> <Nombre> [admin]   crea o cambia la contraseña de alguien
  importar <respaldo.json>      carga un respaldo de la app (reemplaza todo)
  probar-gmail                  comprueba que la contraseña de aplicación funciona
  probar-drive                  comprueba que Google Drive está conectado
"""
import getpass
import json
import sys

from . import config as C


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "ayuda"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    cfg = C.cargar()

    if cmd == "servidor":
        import uvicorn
        from .web import crear_app
        uvicorn.run(crear_app(cfg), host="127.0.0.1", port=cfg.puerto, proxy_headers=True, log_level="warning")
        return 0

    if cmd == "revisar":
        from .worker import correr
        est = correr(cfg)
        print("Listo." if est["ok"] else f"Hubo un error: {est.get('error')}")
        return 0 if est["ok"] else 1

    if cmd == "usuario":
        if len(args) < 2:
            print("Uso: gastos usuario beatriz Beatriz      (o: usuario titular Titular admin)")
            return 2
        from .db import DB
        from .web import hash_clave
        uid, nombre = args[0].lower(), args[1]
        rol = "admin" if len(args) > 2 and args[2] == "admin" else "persona"
        if rol == "persona" and nombre not in C.PERSONAS:
            print(f"Ojo: '{nombre}' debe escribirse igual que en la app: {', '.join(C.PERSONAS)}")
            return 2
        c1 = getpass.getpass(f"Contraseña nueva para {nombre}: ")
        c2 = getpass.getpass("Repítela: ")
        if c1 != c2 or len(c1) < 6:
            print("No coinciden o tiene menos de 6 caracteres. No cambié nada.")
            return 1
        DB(cfg.db_path).set("usuarios", uid, {"nombre": nombre, "rol": rol, "clave": hash_clave(c1)})
        print(f"Listo: {nombre} entra con el usuario '{uid}'.")
        return 0

    if cmd == "importar":
        from .db import DB
        data = json.load(open(args[0], encoding="utf-8"))
        n = DB(cfg.db_path).importar(data)
        print(f"Importados {n} documentos.")
        return 0

    if cmd == "probar-gmail":
        from .correo import Gmail
        try:
            g = Gmail(cfg.gmail_usuario, cfg.gmail_clave_app)
        except Exception as e:
            print(f"No pude entrar a Gmail: {e}")
            return 1
        print(f"Gmail OK. Carpetas: {g.todos} y {g.papelera}")
        cs = g.recientes(2, lambda x: False)
        print(f"Correos de bancos de los últimos 2 días: {len(cs)}")
        for c in cs[-5:]:
            print(f"  {c.local:%d/%m %H:%M}  {c.remitente}  {c.asunto[:50]}")
        g.cerrar()
        return 0

    if cmd == "probar-drive":
        from . import drive
        if not drive.disponible(cfg):
            print(f"rclone no tiene el remoto '{cfg.rclone_remoto}'. Revisa el paso de Google Drive en la guía.")
            return 1
        import os
        import tempfile
        p = os.path.join(tempfile.gettempdir(), "prueba-gastos.txt")
        open(p, "w").write("prueba")
        ok = drive.subir(cfg, p, "prueba-conexion.txt")
        print("Drive OK: se creó 'prueba-conexion.txt' en tu carpeta." if ok else "No se pudo subir a Drive.")
        return 0 if ok else 1

    print(f"No conozco el comando '{cmd}'.")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
