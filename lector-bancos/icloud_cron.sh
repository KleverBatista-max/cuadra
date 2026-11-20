#!/bin/bash
# Cada minuto: corre el lector de la nota de iCloud si la app lo pidió (archivo icloud-ahora) o cada 10 minutos.
T=/home/gastos/gastos-datos/entrada/icloud-ahora
M=$(date +%M)
if [ -f "$T" ] || [ $((10#$M % 10)) -eq 0 ]; then
  rm -f "$T"
  flock -n /tmp/icloud.cron.lock xvfb-run -a -s "-screen 0 1280x900x24" /opt/bancos-venv/bin/python /opt/bancos/icloud_nota.py ${ICLOUD_ARGS:-} >> /root/icloud.log 2>&1
fi
