#!/bin/bash
# Mudanza: instala TODO (bancos + app Cuadra + respaldos) en una máquina nueva a partir del respaldo de Drive.
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
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Córrelo con sudo:  sudo bash $0 ARCHIVO.tar.gz"; exit 1; }
[ -f "${1:-}" ] || { echo "Falta el respaldo. Uso: sudo bash $0 vps-proyecto-ultimo.tar.gz"; exit 1; }
TAR=$(readlink -f "$1")
PLAYWRIGHT=1.65.1

echo "==> 1/7 Programas del sistema"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rclone tesseract-ocr tesseract-ocr-spa rsync xvfb git curl >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' || { echo "Python muy viejo: usa Raspberry Pi OS Bookworm o Ubuntu 24.04"; exit 1; }
timedatectl set-timezone America/Panama || true
if [ ! -f /swapfile ] && [ "$(awk '/MemTotal/{print $2}' /proc/meminfo)" -lt 3000000 ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> 2/7 Usuario gastos"
id gastos >/dev/null 2>&1 || useradd -m -s /bin/bash gastos

echo "==> 3/7 Archivos del respaldo"
X=$(mktemp -d); trap 'rm -rf "$X"' EXIT
tar -xzf "$TAR" -C "$X"
for d in home/gastos opt/bancos opt/respaldo root/bancos_out root/.config/rclone; do
  [ -e "$X/$d" ] && mkdir -p "/$d" && rsync -a "$X/$d/" "/$d/"
done
cp "$X/root/MANUAL.md" /root/MANUAL.md
mkdir -p /root/.ssh && chmod 700 /root/.ssh
for f in config github_cuadra_bancos github_cuadra_bancos.pub; do
  [ -f "$X/root/.ssh/$f" ] && install -m 600 "$X/root/.ssh/$f" "/root/.ssh/$f"
done
ssh-keyscan -t ed25519 github.com 2>/dev/null >> /root/.ssh/known_hosts || true
for f in gastos-web.service gastos-worker.service gastos-worker.timer; do cp "$X/etc/systemd/system/$f" /etc/systemd/system/; done
[ -f "$X/etc/update-motd.d/99-manual" ] && install -m 755 "$X/etc/update-motd.d/99-manual" /etc/update-motd.d/99-manual
# base de datos: la copia consistente que hizo el respaldo
rm -f /home/gastos/gastos-datos/gastos.db-wal /home/gastos/gastos-datos/gastos.db-shm
install -m 600 "$X/gastos.db" /home/gastos/gastos-datos/gastos.db
chown -R gastos:gastos /home/gastos
mkdir -p /root/icloud_debug /root/respaldos; chmod 700 /root/respaldos

echo "==> 4/7 App Cuadra (Python, tarda unos minutos)"
rm -rf /home/gastos/gastos-app/venv
sudo -u gastos -H python3 -m venv /home/gastos/gastos-app/venv
sudo -u gastos -H /home/gastos/gastos-app/venv/bin/pip install -q --upgrade pip
sudo -u gastos -H /home/gastos/gastos-app/venv/bin/pip install -q -r /home/gastos/gastos-app/requirements.txt
printf '#!/bin/sh\nexport GASTOS_CONFIG="/home/gastos/gastos-datos/config.toml"\ncd "/home/gastos/gastos-app" && exec "/home/gastos/gastos-app/venv/bin/python" -m gastos "$@"\n' > /usr/local/bin/gastos
chmod +x /usr/local/bin/gastos

echo "==> 5/7 Lector de bancos (navegador Chromium, tarda unos minutos)"
rm -rf /opt/bancos-venv
python3 -m venv /opt/bancos-venv
/opt/bancos-venv/bin/pip install -q "playwright==$PLAYWRIGHT"
/opt/bancos-venv/bin/playwright install --with-deps chromium >/dev/null

echo "==> 6/7 Servicios y horarios"
systemctl daemon-reload
systemctl enable --now gastos-web.service gastos-worker.timer
crontab "$X/crontab-root.txt"
git config --global --get-all safe.directory 2>/dev/null | grep -qx /home/gastos/gastos-app || git config --global --add safe.directory /home/gastos/gastos-app

echo "==> 7/7 Tailscale (la página https://mi-servidor...)"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
echo "    Si aparece un enlace, ábrelo y entra con tu cuenta de Tailscale."
echo "    (Antes borra o apaga el servidor viejo en tailscale.com/admin para que el nombre mi-servidor quede libre.)"
tailscale up --hostname=mi-servidor
tailscale serve --bg 8080
tailscale funnel --bg 8080

echo
echo "Listo. Pruebas rápidas:"
echo "  sudo -u gastos -H gastos probar-gmail"
echo "  sudo -u gastos -H gastos probar-drive"
echo "  Página: $(tailscale serve status 2>/dev/null | grep -o 'https://[^ ]*' | head -1)"
echo "iCloud pedirá el código SMS en la próxima revisión: escríbelo en Cuadra → engranaje."
