// Lo corre resumen.py: lee {saldos,movs,cxc,hoy} por stdin y escribe el resumen en JSON. El cálculo está en static/calculo.js (el mismo de la página).
const C = require('./static/calculo.js');
let t = ''; process.stdin.on('data', d => t += d).on('end', () => { const d = JSON.parse(t); process.stdout.write(JSON.stringify(C.resumen(d, d.hoy))); });
