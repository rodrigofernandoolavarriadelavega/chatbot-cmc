/* QR mínimo para el portal del paciente (sin CDN, sin red).
   Modo byte (UTF-8), corrección M (sube a L si no cabe), versiones 1-40,
   elección de máscara por puntaje ISO/IEC 18004. Basado en el algoritmo
   público de Nayuki (qrcodegen, MIT), reescrito compacto.
   Uso: CMC_QR.svg(texto, {px:4}) → string <svg>;  CMC_QR.matrix(texto) → {size, m:[[bool]]} */
window.CMC_QR = (function(){
'use strict';
const ECC = {L:[1,[-1,7,10,15,20,26,18,20,24,30,18,20,24,26,30,22,24,28,30,28,28,28,28,30,30,26,28,30,30,30,30,30,30,30,30,30,30,30,30,30,30],
                 [-1,1,1,1,1,1,2,2,2,2,4,4,4,4,4,6,6,6,6,7,8,8,9,9,10,12,12,12,13,14,15,16,17,18,19,19,20,21,22,24,25]],
             M:[0,[-1,10,16,26,18,24,16,18,22,22,26,30,22,22,24,24,28,28,26,26,26,26,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28],
                 [-1,1,1,1,2,2,4,4,4,5,5,5,8,9,9,10,10,11,13,14,16,17,17,18,20,21,23,25,26,28,29,31,33,35,37,38,40,43,45,47,49]]};

function rawModules(v){ let r=(16*v+128)*v+64; if(v>=2){ const n=Math.floor(v/7)+2; r-=(25*n-10)*n-55; if(v>=7) r-=36; } return r; }
function dataCodewords(v,ecl){ const e=ECC[ecl]; return Math.floor(rawModules(v)/8)-e[1][v]*e[2][v]; }
function gfMul(x,y){ let z=0; for(let i=7;i>=0;i--){ z=(z<<1)^((z>>>7)*0x11D); z^=((y>>>i)&1)*x; } return z&0xFF; }
function rsDivisor(deg){ const r=new Array(deg).fill(0); r[deg-1]=1; let root=1;
  for(let i=0;i<deg;i++){ for(let j=0;j<deg;j++){ r[j]=gfMul(r[j],root); if(j+1<deg) r[j]^=r[j+1]; } root=gfMul(root,2); } return r; }
function rsRemainder(data,div){ const r=div.map(()=>0); for(const b of data){ const f=b^r.shift(); r.push(0); div.forEach((c,i)=>{ r[i]^=gfMul(c,f); }); } return r; }
function alignPos(v,size){ if(v===1) return []; const n=Math.floor(v/7)+2; const step=v===32?26:Math.ceil((v*4+4)/(n*2-2))*2; const r=[6]; for(let p=size-7;r.length<n;p-=step) r.splice(1,0,p); return r; }

function encode(text){
  const bytes=Array.from(new TextEncoder().encode(String(text)));
  for (const ecl of ['M','L']) for (let v=1; v<=40; v++){
    const ccBits = v<=9?8:16; const need = 4+ccBits+bytes.length*8;
    if (need <= dataCodewords(v,ecl)*8) return build(bytes,v,ecl);
  }
  throw new Error('El texto es demasiado largo para un código QR.');
}

function build(bytes,v,ecl){
  const cap = dataCodewords(v,ecl)*8; const bits=[];
  const put=(val,n)=>{ for(let i=n-1;i>=0;i--) bits.push((val>>>i)&1); };
  put(4,4); put(bytes.length, v<=9?8:16); bytes.forEach(b=>put(b,8));
  put(0, Math.min(4, cap-bits.length)); put(0, (8-bits.length%8)%8);
  for (let p=0xEC; bits.length<cap; p^=0xEC^0x11) put(p,8);
  const data=[]; for(let i=0;i<bits.length;i+=8){ let b=0; for(let j=0;j<8;j++) b=(b<<1)|bits[i+j]; data.push(b); }
  // Bloques + Reed-Solomon + intercalado
  const e=ECC[ecl], nb=e[2][v], eccLen=e[1][v], raw=Math.floor(rawModules(v)/8);
  const nShort=nb-raw%nb, shortLen=Math.floor(raw/nb), div=rsDivisor(eccLen); const blocks=[];
  for(let i=0,k=0;i<nb;i++){ const dl=shortLen-eccLen+(i<nShort?0:1); const dat=data.slice(k,k+dl); k+=dl;
    const blk=dat.concat(rsRemainder(dat,div)); if(i<nShort) blk.splice(shortLen-eccLen,0,-1); blocks.push(blk); }
  const cw=[]; for(let i=0;i<blocks[0].length;i++) blocks.forEach((b,j)=>{ if(i!==shortLen-eccLen||j>=nShort) cw.push(b[i]); });
  // Matriz
  const size=v*4+17; const M=[...Array(size)].map(()=>Array(size).fill(false)); const F=[...Array(size)].map(()=>Array(size).fill(false));
  const set=(x,y,d)=>{ M[y][x]=d; F[y][x]=true; };
  for(let i=0;i<size;i++){ set(6,i,i%2===0); set(i,6,i%2===0); }
  const finder=(x,y)=>{ for(let dy=-4;dy<=4;dy++) for(let dx=-4;dx<=4;dx++){ const d=Math.max(Math.abs(dx),Math.abs(dy)), xx=x+dx, yy=y+dy; if(xx>=0&&xx<size&&yy>=0&&yy<size) set(xx,yy,d!==2&&d!==4); } };
  finder(3,3); finder(size-4,3); finder(3,size-4);
  const ap=alignPos(v,size), na=ap.length;
  for(let i=0;i<na;i++) for(let j=0;j<na;j++){ if((i===0&&j===0)||(i===0&&j===na-1)||(i===na-1&&j===0)) continue;
    for(let dy=-2;dy<=2;dy++) for(let dx=-2;dx<=2;dx++) set(ap[i]+dx,ap[j]+dy,Math.max(Math.abs(dx),Math.abs(dy))!==1); }
  const fmt=(mask)=>{ const d=(e[0]<<3)|mask; let r=d; for(let i=0;i<10;i++) r=(r<<1)^((r>>>9)*0x537); const b=((d<<10)|r)^0x5412; const g=i=>((b>>>i)&1)===1;
    for(let i=0;i<=5;i++) set(8,i,g(i)); set(8,7,g(6)); set(8,8,g(7)); set(7,8,g(8)); for(let i=9;i<15;i++) set(14-i,8,g(i));
    for(let i=0;i<8;i++) set(size-1-i,8,g(i)); for(let i=8;i<15;i++) set(8,size-15+i,g(i)); set(8,size-8,true); };
  fmt(0);
  if (v>=7){ let r=v; for(let i=0;i<12;i++) r=(r<<1)^((r>>>11)*0x1F25); const b=(v<<12)|r;
    for(let i=0;i<18;i++){ const bit=((b>>>i)&1)===1, a=size-11+i%3, c=Math.floor(i/3); set(a,c,bit); set(c,a,bit); } }
  let i=0; for(let right=size-1;right>=1;right-=2){ if(right===6) right=5;
    for(let vert=0;vert<size;vert++) for(let j=0;j<2;j++){ const x=right-j, up=((right+1)&2)===0, y=up?size-1-vert:vert;
      if(!F[y][x] && i<cw.length*8){ M[y][x]=((cw[i>>>3]>>>(7-(i&7)))&1)===1; i++; } } }
  const MASK=[(x,y)=>(x+y)%2===0,(x,y)=>y%2===0,(x,y)=>x%3===0,(x,y)=>(x+y)%3===0,(x,y)=>(Math.floor(x/3)+Math.floor(y/2))%2===0,
    (x,y)=>x*y%2+x*y%3===0,(x,y)=>(x*y%2+x*y%3)%2===0,(x,y)=>((x+y)%2+x*y%3)%2===0];
  const apply=k=>{ for(let y=0;y<size;y++) for(let x=0;x<size;x++) if(!F[y][x]&&MASK[k](x,y)) M[y][x]=!M[y][x]; };
  let best=0, bestP=Infinity;
  for(let k=0;k<8;k++){ apply(k); fmt(k); const p=penalty(M,size); if(p<bestP){ bestP=p; best=k; } apply(k); }
  apply(best); fmt(best);
  return {size, m:M, version:v, ecl};
}

function penalty(M,n){
  let p=0, dark=0;
  const line=get=>{ let run=1, prev=get(0); for(let i=1;i<=n;i++){ const c=i<n?get(i):!prev; if(i<n&&c===prev) run++; else { if(run>=5) p+=3+run-5; run=1; prev=c; } }
    const s=[]; for(let i=0;i<n;i++) s.push(get(i)?1:0); const str='0000'+s.join('')+'0000';
    for(const pat of ['00001011101','10111010000']){ let k=str.indexOf(pat); while(k>=0){ p+=40; k=str.indexOf(pat,k+1); } } };
  for(let y=0;y<n;y++) line(x=>M[y][x]);
  for(let x=0;x<n;x++) line(y=>M[y][x]);
  for(let y=0;y<n-1;y++) for(let x=0;x<n-1;x++){ const c=M[y][x]; if(c===M[y][x+1]&&c===M[y+1][x]&&c===M[y+1][x+1]) p+=3; }
  for(let y=0;y<n;y++) for(let x=0;x<n;x++) if(M[y][x]) dark++;
  p += Math.floor(Math.abs(dark*20-n*n*10)/(n*n))*10;
  return p;
}

function svg(text, o={}){
  const q=encode(text), quiet=4, n=q.size+quiet*2; let d='';
  for(let y=0;y<q.size;y++) for(let x=0;x<q.size;x++) if(q.m[y][x]) d+=`M${x+quiet} ${y+quiet}h1v1h-1z`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${n} ${n}" shape-rendering="crispEdges" role="img" aria-label="${o.label||'Código QR'}" ${o.px?`width="${n*o.px}" height="${n*o.px}"`:''}><rect width="${n}" height="${n}" fill="#fff"/><path d="${d}" fill="#000"/></svg>`;
}
return {matrix:encode, svg};
})();
