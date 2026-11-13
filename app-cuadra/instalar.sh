#!/usr/bin/env bash
# Instalador de "Gastos y facturas" para la Raspberry Pi.
# Uso:  bash instalar.sh
set -e
cd "$(dirname "$0")"
YO="$(whoami)"
APP="$HOME/gastos-app"
DATOS="$HOME/gastos-datos"

echo "==> 1/5 Instalando programas del sistema (te puede pedir tu contraseña de la Raspberry)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip rclone tesseract-ocr tesseract-ocr-spa rsync >/dev/null

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
  echo "Tu Raspberry tiene un Python viejo ($(python3 --version)). Hace falta Raspberry Pi OS Bookworm o más nuevo."
  exit 1
fi

echo "==> 2/5 Copiando la app a $APP"
mkdir -p "$APP" "$DATOS" "$DATOS/entrada"
rsync -a --delete --exclude venv --exclude tests --exclude '__pycache__' ./ "$APP/"

echo "==> 3/5 Preparando Python (tarda unos minutos la primera vez)"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

if [ ! -f "$DATOS/config.toml" ]; then
  cp "$APP/deploy/config.ejemplo.toml" "$DATOS/config.toml"
  chmod 600 "$DATOS/config.toml"
  echo "    Creé $DATOS/config.toml (lo llenas en el paso 4 de la guía)."
fi

# comando corto: "gastos revisar", "gastos usuario ..."
printf '#!/bin/sh\nexport GASTOS_CONFIG="%s/config.toml"\ncd "%s" && exec "%s/venv/bin/python" -m gastos "$@"\n' "$DATOS" "$APP" "$APP" | sudo tee /usr/local/bin/gastos >/dev/null
sudo chmod +x /usr/local/bin/gastos

echo "==> 4/5 Dejando todo corriendo solo (también al reiniciar)"
for f in gastos-web.service gastos-worker.service gastos-worker.timer; do
  sed -e "s#__USUARIO__#$YO#g" -e "s#__APP__#$APP#g" -e "s#__DATOS__#$DATOS#g" "$APP/deploy/$f" | sudo tee "/etc/systemd/system/$f" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now gastos-web.service gastos-worker.timer

echo "==> 5/5 Publicando la página solo dentro de Tailscale"
if command -v tailscale >/dev/null; then
  sudo tailscale serve --bg 8080 || echo "    (No pude activar tailscale serve; ver la guía, paso 7)"
  tailscale serve status || true
else
  echo "    Tailscale no está instalado. Ver la guía, paso 7."
fi

echo
echo "Listo. Sigue con la guía desde el paso 3."
