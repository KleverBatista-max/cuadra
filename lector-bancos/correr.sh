#!/bin/bash
# Corre los 4 bancos y se lo pasa directo a la app de gastos (SIN correo PI-BANCOS desde 12 nov 2026). Cron lun-vie 10:00 Panamá.
cd /opt/bancos
mkdir -p /root/bancos_out
exec >> /root/bancos_out/cron.log 2>&1
echo "===== $(date "+%F %T") ====="
flock -n /tmp/bancos.lock xvfb-run -a -s "-screen 0 1366x900x24" /opt/bancos-venv/bin/python bancos.py --reintentos 2 --espera 30 --sin-correo   # sin correo PI-BANCOS: el JSON va directo a la app (abajo)
HOY=$(date +%F)
if [ -f /root/bancos_out/$HOY.json ] && [ -d /home/gastos/gastos-datos/entrada ]; then
  # la app de gastos lo toma en su próxima revisión (cada 5 min) y lo borra
  install -o gastos -g gastos -m 600 /root/bancos_out/$HOY.json /home/gastos/gastos-datos/entrada/bancos-$HOY.json
  echo "Entregado a la app de gastos: entrada/bancos-$HOY.json"
  rm -f /root/bancos_out/*.json   # la app ya lo tiene; no se acumulan archivos diarios
fi
