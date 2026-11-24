"""Graba un video de la app de demostración: escanear el QR de una factura y dividir una cuenta.

Usa el screencast de Chrome (CDP) a 2x y guarda los cuadros a 30 por segundo como JPG numerados.
Uso:   python grabar_video.py --url http://127.0.0.1:18099 --salida cuadros
Luego: ffmpeg -framerate 30 -i cuadros/f%05d.jpg -c:v libx264 -pix_fmt yuv420p video.mp4
Requiere: pip install playwright. El QR y la respuesta de la DGI son simulados (ver capturar.py).
"""
import argparse
import base64
import json
import os
import shutil

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:18099")
ap.add_argument("--usuario", default="demo")
ap.add_argument("--clave", default="demo1234")
ap.add_argument("--salida", default="cuadros")
ap.add_argument("--camara", default="/tmp/cuadra-demo/camara.mjpeg")
ap.add_argument("--chrome", default=None)
a = ap.parse_args()
shutil.rmtree(a.salida, ignore_errors=True)
os.makedirs(a.salida)

# círculo que marca cada toque + sin campo de notas libres
JS = """(()=>{
const s=document.createElement('style');
s.textContent='@keyframes tp{from{transform:translate(-50%,-50%) scale(.3);opacity:.9}to{transform:translate(-50%,-50%) scale(1.6);opacity:0}}'
 +'.tapr{position:fixed;width:64px;height:64px;border-radius:50%;background:rgba(15,107,85,.38);border:2px solid rgba(15,107,85,.7);pointer-events:none;z-index:99999;animation:tp .5s ease-out forwards}'
 +'#eNotas,label[for=eNotas]{display:none!important}';
document.head.appendChild(s);
window.__tap=(x,y)=>{const d=document.createElement('div');d.className='tapr';d.style.left=x+'px';d.style.top=y+'px';document.body.appendChild(d);setTimeout(()=>d.remove(),600)};
})()"""

cuadros = []  # (marca de tiempo, bytes JPEG)
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=a.chrome, args=["--no-sandbox", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                                          f"--use-file-for-fake-video-capture={a.camara}"])
    c = b.new_context(viewport={"width": 393, "height": 852}, device_scale_factor=2, color_scheme="light", permissions=["camera"])
    pg = c.new_page()
    pg.goto(a.url + "/login")
    pg.fill("input[name=usuario]", a.usuario)
    pg.fill("input[name=clave]", a.clave)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(2500)

    def dgi(route):
        c.request.patch(f"{a.url}/api/d/movs/d99", data={"factura": "Recibida"})
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"estado": "OK", "emisor": "Supermercado Central", "total": 42.75}))
    pg.route("**/api/dgi/qr", dgi)
    pg.evaluate(JS)

    cdp = c.new_cdp_session(pg)

    def al_cuadro(ev):
        cuadros.append((ev["metadata"]["timestamp"], base64.b64decode(ev["data"])))
        try:
            cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
        except Exception:
            pass
    cdp.on("Page.screencastFrame", al_cuadro)

    def toca(loc, espera=350):
        loc.scroll_into_view_if_needed()
        bb = loc.bounding_box()
        pg.evaluate(f"window.__tap({bb['x'] + bb['width'] / 2},{bb['y'] + bb['height'] / 2})")
        pg.wait_for_timeout(espera)
        loc.click()

    def espera(ms):
        pg.wait_for_timeout(ms)

    cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": 786, "maxHeight": 1704, "everyNthFrame": 1})
    espera(1200)
    # A. escanear la factura de una compra
    toca(pg.locator("nav >> text=Movimientos").first); espera(1300)
    toca(pg.locator("button:visible:has-text('Supermercado Central'):has-text('42.75')").first); espera(1500)
    toca(pg.locator("#eQR"))
    pg.wait_for_selector("#qrOv.ok", timeout=15000); espera(900)
    pg.wait_for_function("document.body.innerText.includes('guardada en')", timeout=15000); espera(2600)
    # B. dividir una cuenta entre tres
    toca(pg.locator("button:visible:has-text('Restaurante La Casona')").first); espera(1300)
    toca(pg.get_by_role("button", name="Dividir", exact=True)); espera(900)
    for nombre in ("Ana", "Lu"):
        entrada = pg.locator("input[placeholder='Buscar o agregar persona']")
        toca(entrada, 200); pg.keyboard.type(nombre, delay=140); espera(500)
        toca(pg.locator("#divBox .divacc").first); espera(900 if nombre == "Ana" else 2000)
    toca(pg.locator("#eSave")); espera(1500)
    # C. el resultado en Por cobrar
    toca(pg.locator("nav >> text=Por cobrar").first); espera(3000)
    cdp.send("Page.stopScreencast")

# del screencast (solo manda cuadros cuando algo cambia) a 30 cuadros por segundo constantes
t0, fps, j = cuadros[0][0], 30, 0
n = int((cuadros[-1][0] - t0) * fps) + 1
for i in range(n):
    t = t0 + i / fps
    while j + 1 < len(cuadros) and cuadros[j + 1][0] <= t:
        j += 1
    with open(f"{a.salida}/f{i:05d}.jpg", "wb") as f:
        f.write(cuadros[j][1])
print(f"listo: {n} cuadros ({n / fps:.1f} s) en {a.salida}")
