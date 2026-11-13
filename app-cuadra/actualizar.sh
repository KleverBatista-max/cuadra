#!/usr/bin/env bash
# Para instalar una versión nueva del código sin tocar tus datos ni tu config.
set -e
cd "$(dirname "$0")"
rsync -a --delete --exclude venv --exclude tests --exclude '__pycache__' ./ "$HOME/gastos-app/"
"$HOME/gastos-app/venv/bin/pip" install -q -r "$HOME/gastos-app/requirements.txt"
sudo systemctl restart gastos-web.service
echo "Actualizado."
