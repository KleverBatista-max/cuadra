#!/bin/bash
# Mudanza: instala TODO (bancos + app Cuadra + Paquetes Demo + respaldos) en una máquina nueva a partir del respaldo de Drive.
#
# Sirve para: Raspberry Pi (Raspberry Pi OS 64 bits "Bookworm" o más nuevo) o cualquier Ubuntu 24.04 / Debian 12.
# Uso (en la máquina nueva):
#   1. Baja de Google Drive "Gastos Demo/Respaldo VPS/vps-proyecto-ultimo.tar.gz" a la máquina nueva.
#   2. Saca este archivo del respaldo:   tar -xzf vps-proyecto-ultimo.tar.gz opt/bancos/restaurar.sh
#   3. Córrelo:                          sudo bash opt/bancos/restaurar.sh vps-proyecto-ultimo.tar.gz
#   4. Abre el enlace de Tailscale que aparece al final y entra con tu cuenta.
#   5. Apaga el servidor viejo. La primera vez iCloud pedirá el código SMS (Cuadra → engranaje).
#
# No toca la configuración de SSH de la máquina nueva (para no dejarte afuera).
#
# MODO PRUEBA (14 nov 2026):  sudo bash restaurar.sh --prueba ARCHIVO.tar.gz
#   Instala todo igual pero NO prende la revisión automática (Gmail), NO instala los horarios (bancos, iCloud, respaldo
#   que pisaría el Drive y GitHub reales) y NO toca Tailscale. Sirve para probar una mudanza sin afectar al servidor real.
set -euo pipefail
PRUEBA=0
if [ "${1:-}" = "--prueba" ]; then PRUEBA=1; shift; fi
[ "$(id -u)" = 0 ] || { echo "Córrelo con sudo:  sudo bash $0 ARCHIVO.tar.gz"; exit 1; }
[ -f "${1:-}" ] || { echo "Falta el respaldo. Uso: sudo bash $0 [--prueba] vps-proyecto-ultimo.tar.gz"; exit 1; }
TAR=$(readlink -f "$1")
# Ubuntu nuevo (26.10) monta /tmp en memoria (~475 MB): Chromium y el respaldo no caben. Temporales en el disco (14 nov).
export TMPDIR=/var/tmp
PLAYWRIGHT=1.65.1

echo "==> 1/8 Programas del sistema"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rclone tesseract-ocr tesseract-ocr-spa rsync xvfb git curl qrencode >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' || { echo "Python muy viejo: usa Raspberry Pi OS Bookworm o Ubuntu 24.04"; exit 1; }
timedatectl set-timezone America/Panama || true
if [ ! -f /swapfile ] && [ "$(awk '/MemTotal/{print $2}' /proc/meminfo)" -lt 3000000 ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> 2/8 Usuarios gastos y paquetes"
id gastos >/dev/null 2>&1 || useradd -m -s /bin/bash gastos
id paquetes >/dev/null 2>&1 || useradd -m -s /usr/sbin/nologin paquetes
id reclamos >/dev/null 2>&1 || useradd -m -s /usr/sbin/nologin reclamos   # Armador de Reclamos BCBS
id facturas >/dev/null 2>&1 || useradd -m -s /usr/sbin/nologin facturas   # Facturador Demo (PDF)
id recetas >/dev/null 2>&1 || useradd -m -s /usr/sbin/nologin recetas   # Recetario

echo "==> 3/8 Archivos del respaldo"
X=$(mktemp -d); trap 'rm -rf "$X"' EXIT
tar -xzf "$TAR" -C "$X"
for d in home/gastos home/paquetes home/reclamos home/facturas home/recetas opt/bancos opt/respaldo root/bancos_out root/.config/rclone usr/local/sbin; do
  [ -e "$X/$d" ] && mkdir -p "/$d" && rsync -a "$X/$d/" "/$d/"
done
cp "$X/root/MANUAL.md" /root/MANUAL.md
mkdir -p /root/.ssh && chmod 700 /root/.ssh
for f in config github_cuadra_bancos github_cuadra_bancos.pub github_cuadra_publico github_cuadra_publico.pub; do
  [ -f "$X/root/.ssh/$f" ] && install -m 600 "$X/root/.ssh/$f" "/root/.ssh/$f"
done
ssh-keyscan -t ed25519 github.com 2>/dev/null >> /root/.ssh/known_hosts || true
for f in gastos-web.service gastos-worker.service gastos-worker.timer; do cp "$X/etc/systemd/system/$f" /etc/systemd/system/; done
[ -f "$X/etc/systemd/system/cuadra-mcp.service" ] && cp "$X/etc/systemd/system/cuadra-mcp.service" /etc/systemd/system/   # conector del teléfono (22 nov 2026)
[ -f "$X/etc/systemd/system/paquetes-web.service" ] && cp "$X/etc/systemd/system/paquetes-web.service" /etc/systemd/system/
[ -f "$X/etc/systemd/system/reclamos.service" ] && cp "$X/etc/systemd/system/reclamos.service" /etc/systemd/system/
[ -f "$X/etc/systemd/system/facturas.service" ] && cp "$X/etc/systemd/system/facturas.service" /etc/systemd/system/
[ -f "$X/etc/systemd/system/recetas.service" ] && cp "$X/etc/systemd/system/recetas.service" /etc/systemd/system/
[ -f "$X/etc/systemd/system/escaner.service" ] && cp "$X/etc/systemd/system/escaner.service" /etc/systemd/system/
[ -f "$X/usr/local/bin/paquetes" ] && install -m 755 "$X/usr/local/bin/paquetes" /usr/local/bin/paquetes
[ -f "$X/usr/local/bin/recetario" ] && install -m 755 "$X/usr/local/bin/recetario" /usr/local/bin/recetario
[ -f "$X/etc/update-motd.d/99-manual" ] && install -m 755 "$X/etc/update-motd.d/99-manual" /etc/update-motd.d/99-manual
# base de datos: la copia consistente que hizo el respaldo
rm -f /home/gastos/gastos-datos/gastos.db-wal /home/gastos/gastos-datos/gastos.db-shm
install -m 600 "$X/gastos.db" /home/gastos/gastos-datos/gastos.db
chown -R gastos:gastos /home/gastos
[ -d /home/reclamos ] && chown -R reclamos:reclamos /home/reclamos && chmod 600 /home/reclamos/config.json /home/reclamos/secreto /home/reclamos/env 2>/dev/null || true
[ -d /home/facturas ] && chown -R facturas:facturas /home/facturas && chmod 600 /home/facturas/env /home/facturas/secreto 2>/dev/null || true
# Recetario: su base (copia consistente del respaldo)
if [ -f "$X/recetas.db" ] && [ -d /home/recetas ]; then
  rm -f /home/recetas/recetas.db-wal /home/recetas/recetas.db-shm
  install -m 600 "$X/recetas.db" /home/recetas/recetas.db
fi
[ -d /home/recetas ] && chown -R recetas:recetas /home/recetas && chmod 600 /home/recetas/env /home/recetas/secreto /home/recetas/recetas.db /home/recetas/sello.png 2>/dev/null || true
# Paquetes Demo: su base (copia consistente del respaldo)
if [ -f "$X/cjk.db" ]; then
  mkdir -p /home/paquetes/datos
  rm -f /home/paquetes/datos/cjk.db-wal /home/paquetes/datos/cjk.db-shm
  install -m 600 "$X/cjk.db" /home/paquetes/datos/cjk.db
fi
chown -R paquetes:paquetes /home/paquetes; chmod 700 /home/paquetes/datos 2>/dev/null || true
mkdir -p /root/icloud_debug /root/respaldos; chmod 700 /root/respaldos

echo "==> 4/8 App Cuadra (Python, tarda unos minutos)"
rm -rf /home/gastos/gastos-app/venv
sudo -u gastos -H python3 -m venv /home/gastos/gastos-app/venv
sudo -u gastos -H /home/gastos/gastos-app/venv/bin/pip install -q --upgrade pip
sudo -u gastos -H /home/gastos/gastos-app/venv/bin/pip install -q -r /home/gastos/gastos-app/requirements.txt
printf '#!/bin/sh\nexport GASTOS_CONFIG="/home/gastos/gastos-datos/config.toml"\ncd "/home/gastos/gastos-app" && exec "/home/gastos/gastos-app/venv/bin/python" -m gastos "$@"\n' > /usr/local/bin/gastos
chmod +x /usr/local/bin/gastos

echo "==> 5/8 Node 22 (Paquetes Demo y el conector de Cuadra: gastos/static/calculo.js)"
need=1
if command -v node >/dev/null; then
  [ "$(node -e 'const [a,b]=process.versions.node.split(".").map(Number); console.log((a>22||(a===22&&b>=13))?1:0)')" = 1 ] && need=0
fi
if [ $need = 1 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi
node --version
# WhatsApp automático de Paquetes (14 nov 2026): wacli, versión fija y con su suma de control. La sesión del número de Demo
# viene en el respaldo (/home/paquetes/datos/wacli/session.db); si WhatsApp la da por vencida: engranaje -> Vincular (QR).
if ! command -v wacli >/dev/null; then
  WV=0.26.0; WA=$(dpkg --print-architecture); WD=$(mktemp -d)
  ( cd "$WD" && curl -fsSL -O "https://github.com/openclaw/wacli/releases/download/v$WV/wacli_${WV}_linux_${WA}.tar.gz" \
      -O "https://github.com/openclaw/wacli/releases/download/v$WV/checksums.txt" \
    && grep "linux_${WA}.tar.gz" checksums.txt | sha256sum -c - >/dev/null && tar -xzf "wacli_${WV}_linux_${WA}.tar.gz" \
    && install -m 755 wacli /usr/local/bin/wacli ) || echo "    OJO: no se pudo instalar wacli (el WhatsApp automático queda sin funcionar; lo demás sí)"
  rm -rf "$WD"
fi
command -v wacli >/dev/null && wacli --version | head -1

echo "==> 6/8 Lector de bancos (navegador Chromium, tarda unos minutos)"
rm -rf /opt/bancos-venv
python3 -m venv /opt/bancos-venv
/opt/bancos-venv/bin/pip install -q "playwright==$PLAYWRIGHT"
/opt/bancos-venv/bin/playwright install --with-deps chromium >/dev/null

echo "==> 7/8 Servicios y horarios"
systemctl daemon-reload
# Paquetes Demo solo escucha en 127.0.0.1:8090 (en prueba también: solo lee Panacarga)
[ -f /etc/systemd/system/paquetes-web.service ] && systemctl enable --now paquetes-web.service
[ -f /etc/systemd/system/reclamos.service ] && systemctl enable --now reclamos.service   # solo 127.0.0.1:8095
[ -f /etc/systemd/system/facturas.service ] && systemctl enable --now facturas.service   # solo 127.0.0.1:8096
[ -f /etc/systemd/system/recetas.service ] && systemctl enable --now recetas.service   # solo 127.0.0.1:8098
[ -f /etc/systemd/system/escaner.service ] && systemctl enable --now escaner.service   # solo 127.0.0.1:8097
if [ "$PRUEBA" = 1 ]; then
  systemctl enable --now gastos-web.service   # la página solo escucha en 127.0.0.1
  install -m 600 "$X/crontab-root.txt" /root/crontab-del-respaldo.txt
  echo "    MODO PRUEBA: revisión automática apagada y horarios NO instalados (quedaron en /root/crontab-del-respaldo.txt)."
else
  systemctl enable --now gastos-web.service gastos-worker.timer
  [ -f /etc/systemd/system/cuadra-mcp.service ] && systemctl enable --now cuadra-mcp.service   # conector del teléfono: solo 127.0.0.1:9100 (en prueba no: chocaría con el real)
  crontab "$X/crontab-root.txt"
fi
git config --global --get-all safe.directory 2>/dev/null | grep -qx /home/gastos/gastos-app || git config --global --add safe.directory /home/gastos/gastos-app

if [ "$PRUEBA" = 1 ]; then
  echo "==> 8/8 Tailscale: omitido (modo prueba)"
  echo; echo "Listo (modo prueba). Revisar: curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/login"
  echo "                     y Paquetes: curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/login"
  exit 0
fi
echo "==> 8/8 Tailscale (la página https://mi-servidor...)"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
echo "    Si aparece un enlace, ábrelo y entra con tu cuenta de Tailscale."
echo "    (Antes borra o apaga el servidor viejo en tailscale.com/admin para que el nombre mi-servidor quede libre.)"
tailscale up --hostname=mi-servidor
tailscale serve --bg 8080
tailscale funnel --bg 8080
[ -f /etc/systemd/system/cuadra-mcp.service ] && tailscale funnel --bg --set-path=/mcp http://127.0.0.1:9100/mcp   # conector del teléfono (el Funnel de la página sirve también /mcp)
[ -f /etc/systemd/system/paquetes-web.service ] && tailscale funnel --bg --https=8443 http://127.0.0.1:8090
[ -f /etc/systemd/system/reclamos.service ] && tailscale funnel --bg --https=10000 http://127.0.0.1:8095
[ -f /etc/systemd/system/facturas.service ] && tailscale funnel --bg --https=8443 --set-path=/factura http://127.0.0.1:8096
[ -f /etc/systemd/system/recetas.service ] && tailscale funnel --bg --https=8443 --set-path=/recetas http://127.0.0.1:8098
[ -f /etc/systemd/system/escaner.service ] && tailscale funnel --bg --https=8443 --set-path=/escaner http://127.0.0.1:8097

echo
echo "Listo. Pruebas rápidas:"
echo "  sudo -u gastos -H gastos probar-gmail"
echo "  sudo -u gastos -H gastos probar-drive"
echo "  Paquetes: https://mi-servidor...:8443  (sudo -u paquetes -H paquetes usuarios)"
echo "  Página: $(tailscale serve status 2>/dev/null | grep -o 'https://[^ ]*' | head -1)"
echo "iCloud pedirá el código SMS en la próxima revisión: escríbelo en Cuadra → engranaje."
