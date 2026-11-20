/* Preprocesado para QR muy densos en papel térmico (facturas DGI). Gris, desenfoque 3x3 opcional, umbral adaptativo
   por media (imagen integral) y borde blanco. Sin esto ZXing no lee esos QR (probado 14 nov 2026 con un ticket real). */
(function(){
function qrPrep(src, sx, sy, sw, sh, o){
  var s=o.escala||1, W=Math.max(1,Math.round(sw*s)), H=Math.max(1,Math.round(sh*s)), B=o.borde||40;
  var c=document.createElement('canvas'); c.width=W; c.height=H; var x=c.getContext('2d',{willReadFrequently:true});
  x.imageSmoothingQuality='high'; x.drawImage(src,sx,sy,sw,sh,0,0,W,H);
  var d=x.getImageData(0,0,W,H).data, g=new Float32Array(W*H), i, j, y, xx;
  for(i=0,j=0;i<g.length;i++,j+=4)g[i]=0.299*d[j]+0.587*d[j+1]+0.114*d[j+2];
  var v=g;
  if(o.blur){v=new Float32Array(W*H);for(y=0;y<H;y++)for(xx=0;xx<W;xx++){var s2=0,n=0;for(var dy=-1;dy<=1;dy++)for(var dx=-1;dx<=1;dx++){var a=y+dy,b=xx+dx;if(a>=0&&a<H&&b>=0&&b<W){s2+=g[a*W+b];n++;}}v[y*W+xx]=s2/n;}}
  var I=new Float64Array((W+1)*(H+1));
  for(y=0;y<H;y++){var r=0;for(xx=0;xx<W;xx++){r+=v[y*W+xx];I[(y+1)*(W+1)+xx+1]=I[y*(W+1)+xx+1]+r;}}
  var R=(o.bloque|1)>>1, C=o.C, OW=W+2*B, OH=H+2*B, out=new ImageData(OW,OH); out.data.fill(255);
  for(y=0;y<H;y++){var y0=Math.max(0,y-R),y1=Math.min(H-1,y+R);for(xx=0;xx<W;xx++){var x0=Math.max(0,xx-R),x1=Math.min(W-1,xx+R);
    var m=(I[(y1+1)*(W+1)+x1+1]-I[y0*(W+1)+x1+1]-I[(y1+1)*(W+1)+x0]+I[y0*(W+1)+x0])/((y1-y0+1)*(x1-x0+1));
    var k=((y+B)*OW+xx+B)*4, bb=v[y*W+xx]>m-C?255:0; out.data[k]=out.data[k+1]=out.data[k+2]=bb;}}
  var oc=document.createElement('canvas'); oc.width=OW; oc.height=OH; oc.getContext('2d').putImageData(out,0,0); return oc;
}
window.qrPrep=qrPrep;
// Se alterna una por cuadro en vivo; en una foto se prueban todas. Las dos primeras leyeron el ticket de prueba.
window.QR_VARIANTES=[{bloque:21,C:5,blur:1},{bloque:35,C:5,escala:1.5,blur:1},{bloque:21,C:5},{bloque:35,C:10,blur:1},
  {bloque:51,C:10},{bloque:91,C:5},{bloque:21,C:10,escala:0.75},{bloque:51,C:5,escala:1.5,blur:1}];
})();
