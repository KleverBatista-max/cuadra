"""Genera el video de una cámara falsa: un recibo con un QR de la DGI (inventado) que se acomoda frente al lente.

Uso:  python camara_falsa.py --salida /tmp/cuadra-demo/camara.mjpeg
Chrome lo usa con --use-fake-device-for-media-stream --use-file-for-fake-video-capture=ARCHIVO
Requiere: pip install qrcode pillow
"""
import argparse
import math
import os

import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ap = argparse.ArgumentParser()
ap.add_argument("--salida", default="/tmp/cuadra-demo/camara.mjpeg")
arg = ap.parse_args()
URL="https://dgi-fep.mef.gob.pa/Consultas/FacturasPorCUFE?chFE=FE0110000000000000-1-000000-0000000000000000000000000000000000"
qr=qrcode.QRCode(border=1,box_size=8,error_correction=qrcode.constants.ERROR_CORRECT_M); qr.add_data(URL); qr.make()
Q=qr.make_image(fill_color="black",back_color="white").convert("RGB")
def font(s):
    for f in ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf","/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"]:
        try: return ImageFont.truetype(f,s)
        except Exception: pass
    return ImageFont.load_default()
# recibo (papel) 520x900
R=Image.new("RGB",(520,900),(252,252,250)); d=ImageDraw.Draw(R)
d.text((260,50),"SUPERMERCADO CENTRAL",fill=(30,30,30),font=font(26),anchor="mm")
d.text((260,86),"RUC 0000000-0-000000",fill=(90,90,90),font=font(18),anchor="mm")
y=130
for n,m in [("Pan de molde","2.10"),("Leche entera 1L","1.85"),("Café molido","6.40"),("Frutas","8.75"),("Arroz 5 lb","4.20"),("Pollo","12.90"),("Jabón","3.15"),("Agua 6 pack","3.40")]:
    d.text((36,y),n,fill=(40,40,40),font=font(20)); d.text((484,y),m,fill=(40,40,40),font=font(20),anchor="ra"); y+=34
d.line((36,y+6,484,y+6),fill=(120,120,120),width=2)
d.text((36,y+22),"TOTAL",fill=(0,0,0),font=font(26)); d.text((484,y+22),"$42.75",fill=(0,0,0),font=font(26),anchor="ra")
Qs=Q.resize((330,330)); R.paste(Qs,(95,y+80))
def frame(i,n=150):
    W,H=480,640
    bg=Image.new("RGB",(W,H),(86,92,96)); 
    t=min(1,i/70); t=t*t*(3-2*t)           # acomoda el recibo
    s=0.42+0.22*t; ang=(1-t)*9+math.sin(i/9)*0.6
    r=R.resize((int(520*s),int(900*s)),Image.LANCZOS).rotate(ang,expand=True,fillcolor=(86,92,96))
    cx=W/2+(1-t)*70+math.sin(i/11)*3; cy=H/2+(1-t)*60-t*70+math.cos(i/13)*3
    bg.paste(r,(int(cx-r.width/2),int(cy-r.height/2)))
    return bg.filter(ImageFilter.GaussianBlur(0.4))
import io
os.makedirs(os.path.dirname(os.path.abspath(arg.salida)), exist_ok=True)
with open(arg.salida, "wb") as out:      # un .mjpeg son JPEG pegados uno tras otro
    for i in range(150):
        b = io.BytesIO(); frame(i).save(b, "JPEG", quality=92); out.write(b.getvalue())
print("listo:", arg.salida)
