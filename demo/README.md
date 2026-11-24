# Demostración: datos inventados, capturas y videos

Estas herramientas arman una copia de Cuadra con **datos inventados** (nada de cuentas reales) y le sacan capturas
y videos. Así se hicieron las imágenes del README principal.

| Archivo | Qué hace |
|---|---|
| `datos_demo.py` | Crea una base de demostración: 3 tarjetas, 4 cuentas, un mes de compras, 3 personas que deben y un usuario `demo`. Siempre los mismos datos. |
| `camara_falsa.py` | Genera el "video de la cámara": un recibo con un código QR de la DGI (inventado) que se acomoda frente al lente. |
| `capturar.py` | Maneja la app con Playwright y guarda una captura por pantalla, incluyendo escanear una factura y dividir una cuenta. |
| `grabar_video.py` | Graba esos mismos pasos como video (cuadros a 30 por segundo, listos para `ffmpeg`). |

## Cómo se usa

```bash
cd app-cuadra
python3 -m venv venv && venv/bin/pip install -r requirements.txt
pip install playwright qrcode pillow            # para las capturas (en cualquier Python)

venv/bin/python ../demo/datos_demo.py           # crea /tmp/cuadra-demo
GASTOS_CONFIG=/tmp/cuadra-demo/config.toml venv/bin/python -m gastos servidor &   # la app en :18099

python ../demo/camara_falsa.py                  # /tmp/cuadra-demo/camara.mjpeg
python ../demo/capturar.py --salida capturas    # 11 imágenes
python ../demo/grabar_video.py --salida cuadros # cuadros del video
```

Si Chrome no es el que usa Playwright, se le pasa la ruta con `--chrome /usr/bin/google-chrome`.

## Qué es real y qué es simulado

- **Real:** la app, sus pantallas, el escáner de QR (lee el código con la cámara falsa), el pegado de la factura y la división de la cuenta.
- **Simulado:** la respuesta de la DGI. El QR es inventado y la DGI no tiene esa factura, así que la respuesta de `/api/dgi/qr` se reemplaza por una factura de ejemplo.
