"""Saca las capturas de pantalla de la app de demostración (una por pantalla) con Playwright.

Pasos:
  1. venv/bin/python ../demo/datos_demo.py        y arrancar la app con ese config (ver datos_demo.py)
  2. python ../demo/camara_falsa.py               (genera /tmp/cuadra-demo/camara.mjpeg)
  3. python ../demo/capturar.py --url http://127.0.0.1:18099 --salida capturas
Requiere: pip install playwright   (y Chrome o Chromium instalado; con --chrome se indica su ruta)

El QR de la DGI es inventado y la DGI no tiene esa factura, así que la respuesta de /api/dgi/qr se simula
(y la factura se marca como recibida en el movimiento). Todo lo demás es la app real.
"""
import argparse
import json
import os

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:18099")
ap.add_argument("--usuario", default="demo")
ap.add_argument("--clave", default="demo1234")
ap.add_argument("--salida", default="capturas")
ap.add_argument("--camara", default="/tmp/cuadra-demo/camara.mjpeg")
ap.add_argument("--chrome", default=None, help="ruta de Chrome/Chromium (si no, el de Playwright)")
ap.add_argument("--oscuro", action="store_true")
a = ap.parse_args()
os.makedirs(a.salida, exist_ok=True)

# el campo de notas libres no se enseña en las capturas
CSS = "#eNotas,label[for=eNotas]{display:none!important}"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=a.chrome, args=["--no-sandbox", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                                          f"--use-file-for-fake-video-capture={a.camara}"])
    c = b.new_context(viewport={"width": 393, "height": 852}, device_scale_factor=2, permissions=["camera"],
                      color_scheme="dark" if a.oscuro else "light")
    pg = c.new_page()
    pg.goto(a.url + "/login")
    pg.fill("input[name=usuario]", a.usuario)
    pg.fill("input[name=clave]", a.clave)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(2500)
    pg.add_style_tag(content=CSS)

    def dgi(route):  # simula la respuesta de la DGI para el QR inventado
        c.request.patch(f"{a.url}/api/d/movs/d99", data={"factura": "Recibida"})
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"estado": "OK", "emisor": "Supermercado Rey", "total": 42.75}))
    pg.route("**/api/dgi/qr", dgi)

    def foto(n):
        pg.wait_for_timeout(700)
        pg.screenshot(path=f"{a.salida}/{n}.png")

    def pestana(t):
        pg.locator("nav >> text=" + t).first.click()

    foto("01-inicio")
    pestana("Tarjetas"); foto("02-tarjetas")
    pestana("Cuentas"); foto("03-cuentas")
    pestana("Movimientos"); foto("04-movimientos")
    pestana("Por cobrar"); foto("05-por-cobrar")
    pestana("Movimientos"); pg.wait_for_timeout(500)
    pg.locator("button:visible:has-text('Supermercado Rey'):has-text('42.75')").first.click(); foto("06-editar-movimiento")
    pg.click("#eQR"); pg.wait_for_timeout(900); pg.screenshot(path=f"{a.salida}/07-escanear-qr.png")
    pg.wait_for_function("document.body.innerText.includes('guardada en')", timeout=15000)
    pg.wait_for_timeout(400); pg.screenshot(path=f"{a.salida}/08-factura-guardada.png")
    pg.wait_for_timeout(4500)
    pg.locator("button:visible:has-text('Restaurante La Casona')").first.click(); pg.wait_for_timeout(700)
    pg.get_by_role("button", name="Dividir", exact=True).click(); pg.wait_for_timeout(500)
    for nombre in ("Ana", "Lu"):
        i = pg.locator("input[placeholder='Buscar o agregar persona']")
        i.click(); pg.keyboard.type(nombre, delay=60); pg.wait_for_timeout(400)
        pg.locator("#divBox .divacc").first.click(); pg.wait_for_timeout(500)
    foto("09-dividir")
    pg.locator("#eSave").click(); pg.wait_for_timeout(1500)
    pestana("Por cobrar"); foto("10-por-cobrar-despues")
    pestana("Inicio"); pg.wait_for_timeout(500)
    pg.locator("#bell").click(); foto("11-avisos")
print("listo:", sorted(os.listdir(a.salida)))
