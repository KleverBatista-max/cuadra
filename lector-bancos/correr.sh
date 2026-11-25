#!/bin/bash
# Corre los 4 bancos y se lo pasa directo a la app de gastos (SIN correo PI-BANCOS desde 12 nov 2026). Cron todos los días 10:00 a.m. y 6:30 p.m. Panamá, los 4 bancos (--sin queda disponible para saltar alguno).
cd /opt/bancos
mkdir -p /root/bancos_out
exec >> /root/bancos_out/cron.log 2>&1
echo "===== $(date "+%F %T") ====="
flock -n /tmp/bancos.lock xvfb-run -a -s "-screen 0 1366x900x24" /opt/bancos-venv/bin/python bancos.py --reintentos 2 --espera 30 --sin-correo "$@"   # sin correo PI-BANCOS: el JSON va directo a la app (abajo)
HOY=$(date +%F)
for F in /root/bancos_out/$HOY.json /root/bancos_out/$HOY-*.json; do   # corrida completa o de un solo banco (--solo, 24 nov 2026)
  [ -f "$F" ] || continue
  if [ -d /home/gastos/gastos-datos/entrada ]; then
    # la app de gastos lo toma en su próxima revisión (cada 5 min) y lo borra
    N=$(basename "$F")
    install -o gastos -g gastos -m 600 "$F" /home/gastos/gastos-datos/entrada/bancos-$N
    echo "Entregado a la app de gastos: entrada/bancos-$N"
    rm -f "$F"   # la app ya lo tiene; no se acumulan archivos diarios
  fi
done
