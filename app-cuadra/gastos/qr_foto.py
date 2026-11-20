"""Plan B del lector de QR: una foto (cámara del iPhone, 12 MP) se descifra en el servidor con OpenCV + zxing-cpp.
Recorta el QR solo y prueba umbrales adaptativos; los QR de la DGI en papel térmico no se leen sin eso."""
import itertools
import time

import cv2
import numpy as np
import zxingcpp


def _recortes(g):
    yield g
    e = cv2.dilate(cv2.Canny(g, 50, 150), np.ones((25, 25), np.uint8))
    cs, _ = cv2.findContours(e, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cajas = sorted((cv2.boundingRect(c) for c in cs), key=lambda r: -r[2] * r[3])
    for x, y, w, h in cajas[:3]:
        if w > 80 and 0.6 < w / max(h, 1) < 1.6:
            p = int(0.06 * w)
            yield g[max(0, y - p):y + h + p, max(0, x - p):x + w + p]


def _leer(img):
    r = zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode, try_rotate=True, try_invert=True, try_downscale=True)
    from .dgi import parse_qr_url  # DGI o proveedor con el CUFE adentro (14 nov 2026)
    return next((x.text for x in r if parse_qr_url(x.text)), None)


def leer_qr(data: bytes, limite_s: float = 12):
    im = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if im is None:
        return None
    g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    if max(g.shape) > 2600:
        k = 2600 / max(g.shape)
        g = cv2.resize(g, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    t0 = time.time()
    for c in _recortes(g):
        t = _leer(c)
        if t:
            return t
        for s, blur, (bs, C) in itertools.product((1, 1.5, 0.75, 2), (3, 0), ((21, 5), (35, 5), (35, 10), (51, 10), (91, 5), (21, 10))):
            if time.time() - t0 > limite_s:
                return None
            z = cv2.resize(c, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC) if s != 1 else c
            if blur:
                z = cv2.GaussianBlur(z, (blur, blur), 0)
            b = cv2.adaptiveThreshold(z, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, bs, C)
            t = _leer(cv2.copyMakeBorder(b, 60, 60, 60, 60, cv2.BORDER_CONSTANT, value=255))
            if t:
                return t
    return None
