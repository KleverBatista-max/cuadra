/* Cálculos de Cuadra que se usan en DOS lados: la página (app.html) y el conector del teléfono (resumen.py, vía calculo_cli.js).
   UNA sola copia (22 nov 2026, Titular): si cambia una regla de saldos/deuda/Neto, se cambia AQUÍ y los dos lados la reciben.
   saldoCalc y stamp son el código de la página tal cual estaba, solo con `since` (movimientos desde la última lectura) como parámetro. */
(function (root) {
  function isIn(d){return d.tipo==='Yappy recibido'||d.tipo==='ACH recibido';}
  function sum(a){return (a||[]).reduce(function(t,x){return t+(Number(x)||0);},0);}
  function stamp(d){return String(d.fecha||'')+'T'+(d.hora||'23:59');}
  function saldoCalc(x,since){
    var ms=x.medios||[],out=0,inn=0;
    since.forEach(function(d){if(ms.indexOf(d.medio)<0||(stamp(d)<=String(x.desde||'')&&d.flotante!==true))return;var m=Number(d.monto)||0;if(isIn(d))inn+=m;else out+=m;});
    if(x.tipo==='tarjeta'){
      var outN=0,innN=0;
      // Regla para todas (Titular, 16 nov 2026): lo que llega por correo se suma a la deuda y se resta del disponible en el momento.
      // BAC y Banesco dan el disponible al instante: falta solo lo posterior a la lectura. Global lo da atrasado y dice hasta
      // qué día ("Disponible al 15 nov", ese día incluido): falta todo lo de fecha POSTERIOR a ese día, aunque sea de antes
      // de la lectura (fin de semana, feriados). La fecha de una compra es la más temprana entre el correo y el banco.
      var al=x.disponibleAl?String(x.disponibleAl):'';
      since.forEach(function(d){if(ms.indexOf(d.medio)<0||d.caido)return;
        var f=String(d.fecha||''),fb=d.banco&&d.banco.fecha?String(d.banco.fecha):f;if(fb<f)f=fb;
        if(al?(f<=al):(stamp(d)<=String(x.desde||'')))return;
        var m=Number(d.monto)||0;if(isIn(d))innN+=m;else outN+=m;});
      var disp=x.disponible!=null?Number(x.disponible)-outN+innN:null;
      if(x.dispIncluyeTransito&&x.limite!=null&&disp!=null)return {deuda:Number(x.limite)-disp,disp:disp};
      // BAC (19 nov 2026): el saldo de los planes de cuotas («Financiamientos») es deuda aunque no salga en el saldo.
      return {deuda:x.deuda!=null?Number(x.deuda)+(Number(x.financiado)||0)+out-inn:null,disp:disp};
    }
    return {saldo:(Number(x.saldo)||0)-out+inn};
  }
  // 23 nov 2026 (Titular): una cuenta que se pasa de pagada queda en cero; el sobrante es de Titular, no saldo a favor de la persona.
  function cxcTotal(p){return Math.max(0,sum(p.partidas)+(Number(p.deMovs)||0));}
  // 23 nov 2026: +Móvil es solo una lista de chequeo; lo que debe cada quien vive en Cuentas por cobrar (no se cuenta dos veces).
  function movilPend(movil){return 0;}
  // movimientos desde la lectura más vieja de los saldos (lo que la página llama S.since)
  function sinceDe(saldos,movs){
    var min=null;(saldos||[]).forEach(function(x){var d=String(x.desde||'').slice(0,10);if(d&&(!min||d<min))min=d;});
    return min?(movs||[]).filter(function(m){return String(m.fecha||'')>=min;}):[];
  }
  // Liquidez (cuentas que cuentan) y deuda de tarjetas: los cuadros del inicio
  function liquidez(saldos,since){
    var liq={c:0,d:0};(saldos||[]).forEach(function(x){var c=saldoCalc(x,since);
      if(x.tipo==='tarjeta'){if(c.deuda!=null)liq.d+=c.deuda;}else if(x.contar!==false)liq.c+=c.saldo;});
    return liq;
  }
  function porCobrar(cxc,movil){return (cxc||[]).reduce(function(t,p){return t+cxcTotal(p);},0)+movilPend(movil);}
  function nextDay(day,hoy){var d=new Date(hoy.getFullYear(),hoy.getMonth(),day);if(d<hoy)d=new Date(hoy.getFullYear(),hoy.getMonth()+1,day);return d;}
  function proxDia(day,hoy){if(!day)return null;var t=new Date(hoy.getFullYear(),hoy.getMonth(),hoy.getDate());var d=nextDay(Number(day),t);return {d:d,n:Math.round((d-t)/864e5)};}
  function sinPagoDe(x){return x.saldoCorte===0||x.pagoMin===0;}
  // Gasto del mes: compras y salidas; NO cuenta entradas (Yappy/ACH recibidos), pagos a tarjetas (tipo Abono) ni compras caídas.
  // `todas`=false: solo la bolsa Titular (cuadro de Titular); true: todo lo que se le pase (vista de Beatriz/Ana, que ya viene filtrada).
  function gastoMes(docs,todas){return (docs||[]).filter(function(d){return (todas||d.bolsa==='Titular')&&!isIn(d)&&d.tipo!=='Abono'&&!d.caido;})
    .reduce(function(t,d){return t+(Number(d.monto)||0);},0);}

  // ---- Todo junto para el conector (herramientas resumen / tarjetas / cuentas_banco). hoyStr = 'AAAA-MM-DD' (hora de Panamá).
  function iso(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');}
  function resumen(datos,hoyStr){
    var q=hoyStr.split('-'),hoy=new Date(+q[0],+q[1]-1,+q[2]);
    var saldos=(datos.saldos||[]).slice().sort(function(a,b){return (a.orden||0)-(b.orden||0);});
    var since=sinceDe(saldos,datos.movs),calc={};saldos.forEach(function(x){calc[x.id]=saldoCalc(x,since);});
    var liq=liquidez(saldos,since),pc=porCobrar(datos.cxc,datos.movil),mv=movilPend(datos.movil);
    var personas=(datos.cxc||[]).filter(function(p){return !(Math.abs(cxcTotal(p))<0.005&&(p.detalleMovs||[]).length>0);})
      .map(function(p){return [p.nombre||p.id,Math.round(cxcTotal(p)*100)/100];}).sort(function(a,b){return b[1]-a[1];});
    var mes=hoyStr.slice(0,7);
    var gasto=gastoMes((datos.movs||[]).filter(function(m){return m.mes===mes;}),false);
    var tarjetas=saldos.filter(function(x){return x.tipo==='tarjeta';}).map(function(x){
      var c=calc[x.id],k=proxDia(x.corteDia,hoy),p=proxDia(x.pagoDia,hoy);
      var m=String((x.medios||[''])[0]).match(/(\d{4})\s*$/);
      var lim=x.limite!=null?x.limite:(c.deuda!=null&&c.disp!=null?c.deuda+c.disp:null);
      return {id:x.id,banco:x.banco,nombre:x.nombre,ultimos4:m?m[1]:'',medios:x.medios||[],grupo:x.grupo||null,deuda:c.deuda,disponible:c.disp,limite:lim,
        corte:k?{fecha:iso(k.d),n:k.n}:null,pago:p?{fecha:iso(p.d),n:p.n}:null,
        sin_pago:sinPagoDe(x)||!(c.deuda>0),saldo_corte:x.saldoCorte!=null?x.saldoCorte:null,pago_minimo:x.pagoMin!=null?x.pagoMin:null,actualizado:String(x.desde||'')};});
    tarjetas.forEach(function(t){var gr=t.grupo?tarjetas.filter(function(o){return o.grupo===t.grupo;}):[t];
      t.compartida=!!t.grupo;t.grupo_deuda=gr.every(function(o){return o.deuda!=null;})?gr.reduce(function(s,o){return s+o.deuda;},0):null;});
    var cuentas=saldos.filter(function(x){return x.tipo!=='tarjeta'&&x.medios&&x.medios.length;}).map(function(x){
      return {id:x.id,banco:x.banco,nombre:x.nombre,medios:x.medios,saldo:calc[x.id].saldo,cuenta:x.contar!==false,nota:x.nota||'',actualizado:String(x.desde||'')};});
    return {hoy:hoyStr,neto:liq.c+pc-liq.d,liquidez:liq.c,por_cobrar:pc,movil:mv,por_cobrar_personas:personas,deuda_tarjetas:liq.d,gasto_mes:gasto,mes:mes,tarjetas:tarjetas,cuentas:cuentas};
  }

  var API={isIn:isIn,sum:sum,stamp:stamp,saldoCalc:saldoCalc,cxcTotal:cxcTotal,movilPend:movilPend,sinceDe:sinceDe,liquidez:liquidez,porCobrar:porCobrar,
    nextDay:nextDay,proxDia:proxDia,sinPagoDe:sinPagoDe,gastoMes:gastoMes,resumen:resumen};
  if (typeof module !== 'undefined' && module.exports) module.exports = API; else root.Calculo = API;
})(typeof window !== 'undefined' ? window : globalThis);
