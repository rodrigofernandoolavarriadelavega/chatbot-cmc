/* Portal del paciente (portal_app) — Árbol familiar ("Agenda familiar").
   Idea de portal_v4 (genograma): cuadrado = hombre, círculo = mujer, anillo
   verde = tiene hora, relleno aqua = la persona que está viendo ahora.
   Se dibuja con viewBox (vectorial nítido en cualquier celular, lección v4).
   Cada nodo es un botón accesible (Enter/Espacio) que llama onTap(rut).
   Se carga a pedido solo cuando la persona abre "Familia". */
window.CMC_GENO = (function(){
'use strict';
const esc = s => String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const norm = r => String(r||'').replace(/[^0-9kK]/g,'').toUpperCase();
function row(m){
  const r=(m.relation||'').toLowerCase();
  if (r==='titular') return 'yo';
  if (/madre|padre|abuel|suegr/.test(r)) return 'arriba';
  if (/hij|niet|tutel/.test(r)) return 'abajo';
  if (/c[oó]nyuge|conyuge|pareja|espos/.test(r)) return 'pareja';
  if (/herman/.test(r)) return 'lado';
  return 'cercano';
}
function build(members, activeRut){
  const W=360, R=27, LB=48, Y={arriba:64, medio:226, abajo:388};   // LB = alto de las etiquetas bajo cada nodo
  const yo = members.find(m=>(m.relation||'').toLowerCase()==='titular') || members[0];
  const g = {arriba:[], medio:[], abajo:[]};
  const pareja=[], lado=[], cerc=[];
  for (const m of members){ if(m===yo) continue; const k=row(m);
    if(k==='arriba') g.arriba.push(m); else if(k==='abajo') g.abajo.push(m); else if(k==='pareja') pareja.push(m); else if(k==='lado') lado.push(m); else cerc.push(m); }
  // Fila del medio: hermanos · YO · pareja · cercanos (cercanos a la derecha, línea punteada)
  g.medio = lado.concat([yo], pareja, cerc);
  const pos = new Map();
  for (const k of ['arriba','medio','abajo']){
    const arr=g[k]; const n=arr.length; if(!n) continue;
    const step = Math.min(130, (W-90)/Math.max(1,n-1));
    const total = step*(n-1); const x0 = W/2 - total/2;
    arr.forEach((m,i)=>pos.set(m, {x: n===1 ? W/2 : x0+i*step, y: Y[k]}));
  }
  // si arriba/abajo vacíos, compactar alto
  const hasTop=g.arriba.length>0, hasBot=g.abajo.length>0;
  const H = (hasBot?Y.abajo:Y.medio) + 78; const top = hasTop?0:(Y.medio-R-40);
  const P = m => pos.get(m);
  let lines='';
  const py=P(yo);
  // Las líneas salen BAJO las etiquetas (nunca cruzan texto: lección v4)
  if (hasTop){ const xs=g.arriba.map(m=>P(m).x); const y0=Y.arriba+R+LB, midY=Y.medio-R-30;
    lines+=`<path d="${xs.map(x=>`M${x} ${y0} V${midY}`).join(' ')} M${Math.min(...xs,py.x)} ${midY} H${Math.max(...xs,py.x)} M${py.x} ${midY} V${py.y-R}" class="ln"/>`; }
  for (const m of pareja){ const p=P(m); lines+=`<path d="M${py.x+R} ${py.y} H${p.x-R}" class="ln"/>`; }
  for (const m of lado){ const p=P(m); lines+=`<path d="M${p.x} ${p.y-R} V${py.y-R-34} H${py.x}" class="ln"/>`; }
  for (const m of cerc){ const p=P(m); lines+=`<path d="M${py.x+R*0.7} ${py.y-R*0.7} Q${(py.x+p.x)/2} ${py.y-R-46} ${p.x-R*0.7} ${p.y-R*0.7}" class="ln dash"/>`; }
  if (hasBot){ const xs=g.abajo.map(m=>P(m).x); const from = pareja.length ? (py.x+P(pareja[0]).x)/2 : py.x; const midY=Y.abajo-R-26;
    lines+=`<path d="M${from} ${pareja.length?py.y:py.y+R+LB} V${midY} M${Math.min(...xs,from)} ${midY} H${Math.max(...xs,from)}" class="ln"/>`;
    for (const x of xs) lines+=`<path d="M${x} ${midY} V${Y.abajo-R}" class="ln"/>`; }
  let nodes='';
  for (const m of members){ const p=P(m); if(!p) continue;
    const act = norm(m.rut)===norm(activeRut); const hora=!!m.proxima; const hombre=String(m.sexo||'').toUpperCase().startsWith('M');
    const ini = esc((String(m.nombre||'?').trim()[0]||'?').toUpperCase());
    const shape = hombre ? `<rect x="${p.x-R}" y="${p.y-R}" width="${2*R}" height="${2*R}" rx="10" class="nd${act?' act':''}"/>` : `<circle cx="${p.x}" cy="${p.y}" r="${R}" class="nd${act?' act':''}"/>`;
    const ring = hora ? (hombre ? `<rect x="${p.x-R-6}" y="${p.y-R-6}" width="${2*R+12}" height="${2*R+12}" rx="14" class="ring"/>` : `<circle cx="${p.x}" cy="${p.y}" r="${R+6}" class="ring"/>`) : '';
    const nombre = esc(String(m.nombre||'').trim().split(/\s+/)[0]);
    const rel = (m.relation||'').toLowerCase()==='titular' ? 'usted' : esc(m.relation||'');
    const label = `${nombre}${m.edad!=null?' ('+m.edad+')':''}`;
    nodes += `<g class="gnode" role="button" tabindex="0" data-rut="${esc(m.rut)}" aria-label="${label}, ${rel}${hora?', tiene hora':''}${act?', viendo ahora':''}">
      <rect x="${p.x-50}" y="${p.y-R-10}" width="100" height="${2*R+56}" fill="transparent"/>
      ${ring}${shape}<text x="${p.x}" y="${p.y+7}" class="ini${act?' act':''}">${ini}</text>
      <text x="${p.x}" y="${p.y+R+22}" class="nm">${label}</text>
      <text x="${p.x}" y="${p.y+R+40}" class="rl">${rel}</text>
      ${act?`<rect x="${p.x-30}" y="${p.y-R-24}" width="60" height="20" rx="10" class="vw"/><text x="${p.x}" y="${p.y-R-9.5}" class="vwt">VIENDO</text>`:''}
    </g>`;
  }
  return `<svg viewBox="0 ${top} ${W} ${H-top}" width="100%" role="group" aria-label="Árbol familiar" style="display:block;max-width:520px;margin:0 auto">
    <style>.ln{fill:none;stroke:#9DB3C4;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}.ln.dash{stroke-dasharray:5 6}
    .nd{fill:#fff;stroke:#0F3F68;stroke-width:2.5}.nd.act{fill:#4FBECE;stroke:#0F3F68}.ring{fill:none;stroke:#11734B;stroke-width:3}
    .ini{font:700 22px Montserrat,system-ui,sans-serif;fill:#0F3F68;text-anchor:middle}.ini.act{fill:#06303F}
    .nm{font:700 17px Inter,system-ui,sans-serif;fill:#14212D;text-anchor:middle;paint-order:stroke;stroke:#F6F3EE;stroke-width:4px}
    .rl{font:600 15px Inter,system-ui,sans-serif;fill:#55636F;text-anchor:middle;paint-order:stroke;stroke:#F6F3EE;stroke-width:4px}
    .vw{fill:#11734B}.vwt{font:800 10.5px Inter,system-ui,sans-serif;fill:#fff;text-anchor:middle;letter-spacing:.06em}
    .gnode{cursor:pointer;outline:none}.gnode:focus-visible .nd{stroke:#1172AB;stroke-width:5}</style>
    ${lines}${nodes}</svg>`;
}
function wire(root, onTap){
  root.querySelectorAll('.gnode').forEach(n=>{
    n.addEventListener('click', ()=>onTap(n.dataset.rut));
    n.addEventListener('keydown', e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); onTap(n.dataset.rut); } });
  });
}
return {build, wire};
})();
