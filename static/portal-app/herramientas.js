/* Portal del paciente (portal_app) — HERRAMIENTAS de uso diario (ronda 3, 8-oct-2026).
   Ronda 4: las marcas "lo tomé" viven en el servidor (las ve toda la familia autorizada,
   con quién marcó y a qué hora) y los recordatorios no prometen que "suena".
   1) Mis remedios (pastillero + recordatorios .ics / suscripción)  2) Ficha de emergencia (tarjeta, billetera, QR)
   3) Qué me toca este año (guía preventiva del bot por edad y sexo).
   Se carga a pedido. Usa las utilidades globales del template (S, VIEWS, api, ic, esc…).
   Nunca da consejos de dosis ni interpreta resultados. */
(function(){
'use strict';
const H = window.CMC_H = { rem:{}, ficha:{}, guia:null, form:null, fform:null, tocaRut:null, tomas:{}, cal:{} };
const AVISOS_WA = false;   // Interruptor futuro (avisos por WhatsApp). Apagado: no hay UI ni envío.
const DIAS_L = ['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo'];   // índice 0 = lunes
const DIAS_S = ['Lun','Mar','Mié','Jue','Vie','Sáb','Dom'];
const HORAS_RAPIDAS = ['08:00','13:00','20:00','22:00'];
const wd = d => (d.getDay()+6)%7;   // lunes=0 … domingo=6
const SIGA = 'Siga siempre la indicación de su médico. El portal no da consejos de dosis.';

/* ── Modo ejemplo: datos inventados, viven solo en la memoria del navegador ── */
const DEMO_REM = {
  '50000000-7':[
    {id:1,uid:'d1',nombre:'Losartán 50 mg',dosis:'1 comprimido',horarios:['08:00','20:00'],dias:[],quedan:12,por_toma:1,quedan_fecha:iso(today())},
    {id:2,uid:'d2',nombre:'Metformina 850 mg',dosis:'1 comprimido con el almuerzo',horarios:['13:00'],dias:[],quedan:40,por_toma:1,quedan_fecha:iso(today())},
    {id:3,uid:'d3',nombre:'Atorvastatina 20 mg',dosis:'1 comprimido',horarios:['22:00'],dias:[],quedan:null,por_toma:1,quedan_fecha:null}],
  '50000001-5':[{id:4,uid:'d4',nombre:'Salbutamol inhalador',dosis:'Según le indicó su médico',horarios:[],dias:[],quedan:null,por_toma:null,quedan_fecha:null}],
  '50000003-1':[{id:5,uid:'d5',nombre:'Metformina 850 mg',dosis:'1 comprimido',horarios:['08:00','20:00'],dias:[],quedan:null,por_toma:1,quedan_fecha:null},
                {id:6,uid:'d6',nombre:'Vitamina D',dosis:'1 cápsula',horarios:['09:00'],dias:[0,3],quedan:null,por_toma:1,quedan_fecha:null}],
};
const DEMO_FICHA = {
  '50000000-7':{ficha:{alergias:'Penicilina',enfermedades:'Presión alta (hipertensión); Diabetes tipo 2',grupo_sanguineo:'O+',contacto_nombre:'Juan Ejemplo (esposo)',contacto_telefono:'+56 9 8765 4321',notas:'Usa lentes para leer',incluir_remedios:true},updated_at:iso(dAdd(-20))},
};
/* Marcas de ejemplo: una propia y una hecha por otra persona (para ver "marcado por"). */
const DEMO_TOMAS = { '500000007':{'1|08:00':{propio:true,hhmm:'08:04'}}, '500000031':{'5|08:00':{propio:false,por:'Rosa',hhmm:'08:05'}} };
const rk = () => rutClean(S.d && S.d.rut);
function demoKey(r){ return Object.keys(DEMO_P).find(k=>rutClean(k)===rutClean(r)) || DEMO_OWNER; }

/* ════════════════ Datos ════════════════ */
async function loadRem(force){
  const r = rk(); if (!r) return [];
  if (!force && Array.isArray(H.rem[r])) return H.rem[r];
  if (DEMO){ H.rem[r] = (DEMO_REM[demoKey(r)] = DEMO_REM[demoKey(r)] || []); return H.rem[r]; }
  try{ const x = await api('/portal/api/herramientas/remedios'); H.rem[r] = x.remedios||[]; store('cmc_rem_'+r, H.rem[r]); }
  catch(e){ if(e.kind==='auth') { toLogin(); return []; } const c=load('cmc_rem_'+r); H.rem[r] = c ? c : {error:e}; if(c) H.rem[r]._offline=true; }
  return H.rem[r];
}
async function loadFicha(force){
  const r = rk(); if (!force && H.ficha[r]!==undefined) return H.ficha[r];
  if (DEMO){ H.ficha[r] = DEMO_FICHA[demoKey(r)] || null; return H.ficha[r]; }
  try{ const x = await api('/portal/api/herramientas/ficha'); H.ficha[r] = x.ficha ? {ficha:x.ficha, updated_at:x.updated_at} : null; }
  catch(e){ if(e.kind==='auth'){ toLogin(); return null; } H.ficha[r] = {error:e}; }
  return H.ficha[r];
}
async function loadGuia(){
  if (H.guia && !H.guia.error) return H.guia;
  // Guía pública (sin datos de nadie): se lee igual en el modo ejemplo.
  try{ const r = await fetch('/portal/api/herramientas/guia-preventiva',{credentials:'same-origin'}); if(!r.ok) throw new Error();
    H.guia = await r.json(); store('cmc_guia', H.guia); }
  catch(e){ const c = load('cmc_guia'); H.guia = c || {error:new NetErr('offline','No pudimos cargar la guía. Revise su señal e intente de nuevo.')}; }
  return H.guia;
}
const rerender = (...names) => () => { if (names.includes(route().name)) render(); };

/* ════════════════ Cálculos del pastillero (aritmética, no clínica) ════════════════ */
function tomasDe(list, d){ const out=[]; (list||[]).forEach(m=>{ if(m.dias&&m.dias.length&&!m.dias.includes(wd(d))) return; (m.horarios||[]).forEach(h=>out.push({h,m})); }); return out.sort((a,b)=>a.h.localeCompare(b.h)); }
/* ── Tomas marcadas: en el servidor (compartidas), con copia en el teléfono para cuando no hay señal ── */
const tomKey = () => 'cmc_tomas_'+rk()+'_'+iso(today());
const TQ = 'cmc_tomas_q';   // marcas hechas sin señal, se envían solas después
function tomasHoy(){ const t=H.tomas[rk()]; if (t && t.fecha===iso(today()) && t.map) return t.map; const c=load(tomKey()); return (c && !Array.isArray(c)) ? c : {}; }
const tomadas = () => new Set(Object.keys(tomasHoy()));
async function loadTomas(force){
  const r = rk(); if (!r) return {}; const f = iso(today());
  if (!force && H.tomas[r] && H.tomas[r].fecha===f) return H.tomas[r].map;
  if (DEMO){ H.tomas[r] = {fecha:f, map:(DEMO_TOMAS[r] = DEMO_TOMAS[r] || {})}; return H.tomas[r].map; }
  await flushTomasQ();
  try{ const x = await api('/portal/api/herramientas/tomas?fecha='+f); const map={};
    (x.tomas||[]).forEach(t=>{ map[t.remedio_id+'|'+t.hora] = {propio:!!t.propio, por:t.marcado_por||'', hhmm:t.marcado_hhmm||''}; });
    (load(TQ)||[]).filter(q=>q.rut===r && q.fecha===f).forEach(q=>{ const k=q.remedio_id+'|'+q.hora; if(q.tomado) map[k]=map[k]||{propio:true,hhmm:q.hhmm,pend:true}; else delete map[k]; });
    H.tomas[r] = {fecha:f, map}; store(tomKey(), map); }
  catch(e){ if(e.kind==='auth'){ toLogin(); return {}; } H.tomas[r] = {fecha:f, map:tomasHoy(), offline:true}; }
  return H.tomas[r].map;
}
async function flushTomasQ(){
  if (DEMO) return; const q = load(TQ)||[]; if (!q.length) return; const rest=[];
  for (const it of q){ if (it.rut!==rk()){ rest.push(it); continue; }   // la API marca en el perfil ACTIVO: solo se envían las suyas
    try{ await api('/portal/api/herramientas/tomas',{method:'POST',body:JSON.stringify({remedio_id:it.remedio_id,hora:it.hora,fecha:it.fecha,tomado:it.tomado})}); }
    catch(e){ if (['offline','timeout','server','busy'].includes(e.kind)) rest.push(it); } }
  store(TQ, rest.length?rest:undefined);
}
window.addEventListener('online', ()=>{ flushTomasQ().then(()=>{ if(route().name==='remedios'||route().name==='inicio') loadTomas(true).then(rerender('remedios','inicio')); }); });
function marcaTxt(e){ if(!e) return ''; const quien = e.propio ? 'usted' : (e.por || 'un familiar'); return e.pend ? 'Marcado en este teléfono · se enviará cuando tenga señal' : 'Marcado por '+quien+(e.hhmm?', '+e.hhmm:''); }
function porDia(m){ const n=(m.horarios||[]).length; if(!n) return 0; return n*((m.dias&&m.dias.length)?m.dias.length/7:1)*(m.por_toma||1); }
/* Días que alcanzan: lo que la persona anotó, menos lo que debió tomar desde ese día. */
function diasQuedan(m){
  if (m.quedan==null || !m.quedan_fecha) return null; const pd=porDia(m); if(!pd) return null;
  const desde=pDate(m.quedan_fecha); const pasados=desde?Math.max(0,Math.round((today()-desde)/864e5)):0;
  const resto=Math.max(0, m.quedan - pasados*pd); return {dias:Math.floor(resto/pd), resto:Math.round(resto)};
}
function horasTxt(m){ const h=m.horarios||[]; if(!h.length) return 'Sin hora fija'; return (h.length===1?'A las ':'')+h.join(h.length===2?' y ':', ')+' hrs'; }
function diasTxt(m){ const d=m.dias||[]; if(!d.length) return 'Todos los días'; if(d.length===5&&d.every(x=>x<5)) return 'De lunes a viernes'; return d.map(x=>DIAS_L[x].toLowerCase()).join(', ').replace(/^./,c=>c.toUpperCase()); }

/* ════════════════ Tarjeta pequeña de Inicio ════════════════ */
function homeCard(){
  if (!S.d || restringido()) return '';
  const r = rk(); const list = H.rem[r];
  if (Array.isArray(list) && list.length){
    const hoy = tomasDe(list, today()); const done = tomadas();
    if (hoy.length){
      const now = new Date(); const ahora = String(now.getHours()).padStart(2,'0')+':'+String(now.getMinutes()).padStart(2,'0');
      const pend = hoy.filter(t=>!done.has(t.m.id+'|'+t.h)); const prox = pend.find(t=>t.h>=ahora) || pend[0];
      const bajo = list.find(m=>{ const q=diasQuedan(m); return q && q.dias<=7; });
      return `<a class="tool-teaser" href="#remedios"><span class="ic">${ic('pill')}</span><span class="tx"><b>Sus remedios de hoy</b>
        <span>${prox?`Próxima toma: <strong>${esc(prox.h)}</strong> · ${esc(prox.m.nombre)}`:'Ya marcó todas las tomas de hoy'}</span>
        ${bajo?`<span class="warn-t">${ic('warn','style="width:18px;height:18px;vertical-align:-3px"')} Al ${esc(bajo.nombre.split(' ')[0])} le queda poco</span>`:''}</span>${chev()}</a>`;
    }
  }
  if (list===undefined) return '';   // aún cargando: no mostrar nada que luego salte
  const yo = personaActiva(rk()); const n = H.guia && !H.guia.error ? itemsPara(yo).filter(x=>!hecho(x,yo)).length : null;
  return `<a class="tool-teaser" href="#toca"><span class="ic">${ic('listcheck')}</span><span class="tx"><b>Qué le toca este año</b>
    <span>${n?n+(n===1?' control o vacuna':' controles y vacunas')+' según su edad':'Controles y vacunas según su edad'}</span></span>${chev()}</a>`;
}

/* ════════════════ 1 · MIS REMEDIOS ════════════════ */
function deQuien(){ return S.d.is_dependent ? `<p class="lead">De <b>${esc(S.d.nombre)}</b>. <a href="#" onclick="openPeople();return false">Cambiar de persona</a></p>` : ''; }
VIEWS.remedios = () => {
  const r = rk(); const list = H.rem[r];
  if (list===undefined || list===null) return `<h1>Mis remedios</h1>${skel(3,72)}`;
  if (list.error) return `<h1>Mis remedios</h1>${errorBlock(list.error,"CMC_H.rem['"+jsq(r)+"']=undefined;render()")}`;
  if (!list.length) return `<h1>Mis remedios</h1>${deQuien()}
    <div class="card" style="margin-top:16px;text-align:left"><div class="big-ic">${ic('pill')}</div>
      <h3 style="margin-top:10px">Anote sus remedios y agréguelos a su calendario</h3>
      <p style="margin-top:6px">Su calendario le avisa a la hora de cada toma, según cómo lo tenga configurado. También le avisamos aquí cuando le queden pocos.</p></div>
    <button class="btn aqua" style="margin-top:16px" onclick="CMC_H.nuevo()">${ic('plus')}Anotar mi primer remedio</button>
    <p class="muted small" style="margin-top:14px">${SIGA}</p>`;
  const hoy = tomasDe(list, today()); const map = tomasHoy(); const done = new Set(Object.keys(map));
  const bajos = list.map(m=>({m,q:diasQuedan(m)})).filter(x=>x.q && x.q.dias<=7);
  const sinHora = list.some(m=>!(m.horarios||[]).length);
  return `<h1>Mis remedios</h1>${deQuien()}
    ${list._offline?`<div class="note info" style="margin-top:10px">${ic('info')}<div>Sin señal: le mostramos lo último guardado en este teléfono.</div></div>`:''}
    ${bajos.map(({m,q})=>`<div class="card low" style="margin-top:14px"><p class="low-t">${ic('warn','style="width:22px;height:22px;vertical-align:-5px"')} ${q.dias===0?'Ya no le quedarían':'Le quedan para unos '+q.dias+(q.dias===1?' día':' días')}</p>
      <h3 style="margin-top:4px">${esc(m.nombre)}</h3><p style="margin-top:4px">Pida su control para la receta antes de que se acabe.</p>
      <button class="btn pri" style="margin-top:12px" onclick="CMC_H.receta(${+m.id})">${ic('cal')}Pedir hora para la receta</button>
      <button class="btn link" style="color:var(--blue)" onclick="go('remedio/${+m.id}')">Ya compré más: actualizar</button></div>`).join('')}
    <h2>Hoy, ${esc(fCorta(iso(today())))}</h2>
    ${H.tomas[rk()]&&H.tomas[rk()].offline?`<p class="muted small" style="margin:-4px 0 8px">Sin señal: las marcas que haga se envían solas cuando vuelva la conexión.</p>`:''}
    ${hoy.length?`<div class="list" role="group" aria-label="Tomas de hoy">${hoy.map(t=>{ const k=t.m.id+'|'+t.h, on=done.has(k), e=map[k];
      return `<button class="li toma" aria-pressed="${on}" onclick="CMC_H.marcar('${jsq(k)}')"><span class="hr">${esc(t.h)}</span><span class="tx"><b>${esc(t.m.nombre)}</b>${t.m.dosis?`<span>${esc(t.m.dosis)}</span>`:''}${on?`<span class="by">${ic('check','style="width:16px;height:16px;vertical-align:-2px"')} ${esc(marcaTxt(e))}</span>`:''}</span><span class="tick" aria-hidden="true">${on?ic('check'):''}</span><span class="sr">${on?'Tomado. Toque para desmarcar':'Toque para marcar como tomado'}</span></button>`; }).join('')}</div>
      <p class="muted small" style="margin-top:8px">Toque una toma cuando la haya tomado. La marca también la ven los familiares autorizados en su portal.</p>`
      :`<div class="card"><p>Hoy no tiene tomas con hora.</p></div>`}
    <button class="btn pri" style="margin-top:18px" onclick="go('alarmas')" ${list.some(m=>(m.horarios||[]).length)?'':'disabled'}>${ic('cal')}Agregar recordatorios al calendario</button>
    <h2>Todos sus remedios</h2>
    <div class="list">${list.map(m=>{ const q=diasQuedan(m);
      return `<a class="li" href="#remedio/${+m.id}"><span class="ic">${ic('pill')}</span><span class="tx"><b>${esc(m.nombre)}</b><span>${esc(m.dosis||'')}${m.dosis?' · ':''}${esc(horasTxt(m))}</span><span>${esc(diasTxt(m))}${q?' · le quedan para unos '+q.dias+(q.dias===1?' día':' días'):''}</span></span>${chev()}</a>`; }).join('')}</div>
    <button class="btn aqua" style="margin-top:14px" onclick="CMC_H.nuevo()">${ic('plus')}Anotar otro remedio</button>
    ${sinHora?`<p class="muted small" style="margin-top:10px">Los remedios sin hora fija no van al calendario, pero sí aparecen en su ficha de emergencia.</p>`:''}
    <div class="note info" style="margin-top:18px">${ic('info')}<div>${SIGA}</div></div>
    ${helpBlock('Hola, tengo una consulta sobre mis remedios.')}`;
};
VIEWS.remedios.after = () => { const r=rk(); if (H.rem[r]===undefined) loadRem().then(rerender('remedios')); if (!H.tomas[r] || H.tomas[r].fecha!==iso(today())) loadTomas().then(rerender('remedios')); };
H.marcar = async k => {
  const r = rk(), f = iso(today()); const [rid, hora] = k.split('|'); const map = {...tomasHoy()}; const prev = map[k];
  if (prev && !prev.propio && prev.por && !confirm('Esta toma la marcó '+prev.por+'. ¿Quitar la marca?')) return;
  const tomado = !prev; const now = new Date(); const hhmm = String(now.getHours()).padStart(2,'0')+':'+String(now.getMinutes()).padStart(2,'0');
  if (tomado) map[k] = {propio:true, hhmm, pend:!DEMO}; else delete map[k];
  H.tomas[r] = {...(H.tomas[r]||{}), fecha:f, map}; store(tomKey(), map); try{ localStorage.removeItem('cmc_tomas_'+r+'_'+iso(dAdd(-2))); }catch(e){}
  say(tomado?'Marcado como tomado.':'Desmarcado.'); render();
  if (DEMO){ if(tomado) map[k].pend=false; DEMO_TOMAS[r]=map; return; }
  try{
    const x = await api('/portal/api/herramientas/tomas',{method:'POST',body:JSON.stringify({remedio_id:+rid,hora,fecha:f,tomado})});
    const m2 = {...tomasHoy()}; if (x.toma) m2[k] = {propio:!!x.toma.propio, por:x.toma.marcado_por||'', hhmm:x.toma.marcado_hhmm||''}; else delete m2[k];
    H.tomas[r] = {fecha:f, map:m2}; store(tomKey(), m2);
  }catch(e){
    if (e.kind==='auth') return toLogin();
    if (['offline','timeout','server','busy'].includes(e.kind)){ const q=(load(TQ)||[]).filter(x=>!(x.rut===r&&x.fecha===f&&x.remedio_id===+rid&&x.hora===hora)); q.push({rut:r,fecha:f,remedio_id:+rid,hora,tomado,hhmm}); store(TQ,q.slice(-60)); toast('Sin señal: la marca quedó en este teléfono y se enviará sola.'); }
    else { const m3={...tomasHoy()}; if(prev) m3[k]=prev; else delete m3[k]; H.tomas[r]={fecha:f,map:m3}; store(tomKey(),m3); toast(e.message||'No se pudo guardar la marca.'); }
  }
  if (route().name==='remedios' || route().name==='inicio') render();
};
H.nuevo = () => { H.form = null; go('remedio/nuevo'); };
H.receta = id => { const m=(H.rem[rk()]||[]).find(x=>+x.id===+id); startAgendar({rut:S.d.rut, esp:'Medicina General', motivo:'Control y receta: '+(m?m.nombre:''), skipQuien:true}); };

/* ── Formulario de un remedio ── */
VIEWS.remedio = (r) => {
  const list = H.rem[rk()]; if (!Array.isArray(list)){ setTimeout(()=>loadRem().then(rerender('remedio')),0); return `<h1>Remedio</h1>${skel(4,60)}`; }
  const edit = r.id!=='nuevo' ? list.find(m=>String(m.id)===String(r.id)) : null;
  if (r.id!=='nuevo' && !edit) return `<h1>No encontramos ese remedio</h1><button class="btn pri" style="margin-top:16px" onclick="go('remedios')">Ver mis remedios</button>`;
  const key = edit ? 'e'+edit.id : 'nuevo';
  if (!H.form || H.form.key!==key) H.form = edit ? {key, id:edit.id, nombre:edit.nombre, dosis:edit.dosis||'', horarios:[...(edit.horarios||[])], sinHora:!(edit.horarios||[]).length, dias:[...(edit.dias||[])], algunos:!!(edit.dias||[]).length, quedan:edit.quedan==null?'':String(edit.quedan).replace('.',','), por_toma:edit.por_toma==null?'1':String(edit.por_toma).replace('.',','), err:''}
                                              : {key, nombre:'', dosis:'', horarios:[], sinHora:false, dias:[], algunos:false, quedan:'', por_toma:'1', err:''};
  const f = H.form;
  const horasSel = [...new Set([...HORAS_RAPIDAS, ...f.horarios])].sort();
  return `<span class="step">${edit?'Corregir remedio':'Anotar un remedio'}${S.d.is_dependent?' de '+esc(first(S.d.nombre)):''}</span><h1>${edit?esc(edit.nombre):'¿Qué remedio toma?'}</h1>
    <form onsubmit="CMC_H.guardar(event)" novalidate>
      <label class="lbl" for="rn">Nombre del remedio</label>
      <input id="rn" class="inp sm" autocomplete="off" autocapitalize="sentences" maxlength="80" value="${esc(f.nombre)}" placeholder="Ej.: Losartán 50 mg" aria-describedby="rnH" oninput="CMC_H.form.nombre=this.value">
      <p class="hint" id="rnH">Como sale en la caja o en la receta.</p>
      <label class="lbl" for="rd">¿Cuánto toma cada vez? <span class="muted">(opcional)</span></label>
      <input id="rd" class="inp sm" autocomplete="off" maxlength="80" value="${esc(f.dosis)}" placeholder="Ej.: 1 comprimido" oninput="CMC_H.form.dosis=this.value">
      <p class="hint">Escríbalo tal como se lo indicó su médico.</p>

      <fieldset class="fs"><legend class="lbl">¿A qué hora lo toma?</legend>
        <div class="chips" role="group" aria-label="Horas">${horasSel.map(h=>`<button type="button" class="chip" aria-pressed="${f.horarios.includes(h)}" ${f.sinHora?'disabled':''} onclick="CMC_H.hora('${h}')">${h}</button>`).join('')}</div>
        <div class="otra"><label class="lbl" for="rh" style="margin-top:12px">Otra hora</label>
          <div style="display:flex;gap:10px;flex-wrap:wrap"><input id="rh" class="inp sm" type="time" step="300" style="flex:1 1 140px;min-width:0" ${f.sinHora?'disabled':''}><button type="button" class="btn sec" style="width:auto;min-height:60px;padding:8px 18px" ${f.sinHora?'disabled':''} onclick="CMC_H.otraHora()">Agregar</button></div></div>
        <label class="check" style="margin-top:12px"><input type="checkbox" ${f.sinHora?'checked':''} onchange="CMC_H.form.sinHora=this.checked;render()"><span>No tiene hora fija (lo toma solo cuando lo necesita)</span></label>
      </fieldset>

      ${f.sinHora?'':`<fieldset class="fs"><legend class="lbl">¿Qué días?</legend>
        <div class="seg"><button type="button" class="choice" style="justify-content:center" aria-pressed="${!f.algunos}" onclick="CMC_H.form.algunos=false;CMC_H.form.dias=[];render()"><b style="font-size:1.05rem">Todos los días</b></button>
          <button type="button" class="choice" style="justify-content:center" aria-pressed="${f.algunos}" onclick="CMC_H.form.algunos=true;render()"><b style="font-size:1.05rem">Algunos días</b></button></div>
        ${f.algunos?`<div class="chips days" role="group" aria-label="Días de la semana" style="margin-top:12px">${DIAS_S.map((d,i)=>`<button type="button" class="chip" aria-pressed="${f.dias.includes(i)}" aria-label="${DIAS_L[i]}" onclick="CMC_H.dia(${i})">${d}</button>`).join('')}</div>`:''}
      </fieldset>`}

      <fieldset class="fs"><legend class="lbl">¿Cuántos le quedan? <span class="muted">(opcional)</span></legend>
        <p class="hint" style="margin-top:0">Así le avisamos aquí cuando falte poco y pueda pedir su receta a tiempo.</p>
        <div class="f2"><div><label class="lbl" for="rq" style="font-weight:600">Le quedan</label><input id="rq" class="inp" inputmode="decimal" maxlength="5" value="${esc(f.quedan)}" placeholder="30" oninput="CMC_H.form.quedan=this.value"></div>
          <div><label class="lbl" for="rp" style="font-weight:600">Toma cada vez</label><input id="rp" class="inp" inputmode="decimal" maxlength="4" value="${esc(f.por_toma)}" placeholder="1" oninput="CMC_H.form.por_toma=this.value"></div></div>
        <p class="hint">En comprimidos, cápsulas o la unidad que use. Si toma media pastilla, escriba 0,5.</p>
      </fieldset>
      <div id="rErr">${f.err?`<p class="err" role="alert">${ic('warn','style="width:22px;height:22px;flex:none"')}<span>${esc(f.err)}</span></p>`:''}</div>
      <div class="btns" style="margin-top:22px"><button class="btn pri" id="rBtn" type="submit">Guardar remedio</button>
        <button class="btn sec" type="button" onclick="goBack()">Cancelar</button>
        ${edit?`<button class="btn link" type="button" onclick="CMC_H.borrar(${+edit.id})">Borrar este remedio</button>`:''}</div>
      <p class="muted small" style="margin-top:12px">${SIGA}</p>
    </form>`;
};
H.hora = h => { const f=H.form; const i=f.horarios.indexOf(h); i>=0?f.horarios.splice(i,1):f.horarios.push(h); f.horarios.sort(); f.err=''; render(); };
H.otraHora = () => { const v=($('#rh').value||'').slice(0,5); if(!/^\d\d:\d\d$/.test(v)){ $('#rh').focus(); return; } const f=H.form; if(!f.horarios.includes(v)) f.horarios.push(v); f.horarios.sort(); render(); };
H.dia = i => { const f=H.form; const k=f.dias.indexOf(i); k>=0?f.dias.splice(k,1):f.dias.push(i); f.dias.sort(); render(); };
const numOk = s => { if(String(s).trim()==='') return null; const n=parseFloat(String(s).replace(',','.')); return isNaN(n)?NaN:n; };
H.guardar = async (ev) => {
  ev.preventDefault(); const f=H.form; const err=m=>{ f.err=m; render(); const e=$('#rErr'); e&&e.scrollIntoView({block:'center'}); };
  f.nombre=($('#rn').value||'').trim(); f.dosis=($('#rd').value||'').trim();
  if (!f.nombre) return err('Escriba el nombre del remedio.');
  if (!f.sinHora && !f.horarios.length) return err('Elija al menos una hora, o marque «No tiene hora fija».');
  if (f.algunos && !f.dias.length) return err('Elija al menos un día de la semana.');
  const q=numOk(f.quedan), p=numOk(f.por_toma);
  if (Number.isNaN(q) || (q!=null&&(q<0||q>5000))) return err('Revise cuántos le quedan: escriba solo el número.');
  if (Number.isNaN(p) || (p!=null&&(p<0.25||p>50))) return err('Revise cuántos toma cada vez: escriba solo el número (por ejemplo 1 o 0,5).');
  const body = {id:f.id||null, nombre:f.nombre, dosis:f.dosis, horarios:f.sinHora?[]:f.horarios, dias:(f.sinHora||!f.algunos)?[]:f.dias, quedan:q, por_toma:p==null?1:p};
  const b=$('#rBtn'); b.disabled=true; b.textContent='Guardando…';
  try{
    if (DEMO){ await sleep(300); const L=DEMO_REM[demoKey(rk())]=DEMO_REM[demoKey(rk())]||[]; const prev=L.find(m=>m.id===f.id);
      const row={...body, uid:prev?prev.uid:'n'+Date.now(), id:prev?prev.id:Date.now()%1e6, quedan_fecha: q==null?null:(prev&&prev.quedan===q?prev.quedan_fecha:iso(today()))};
      if(prev) Object.assign(prev,row); else L.push(row); H.rem[rk()]=L; }
    else { await api('/portal/api/herramientas/remedios',{method:'POST',body:JSON.stringify(body)}); await loadRem(true); }
    H.form=null; toast(DEMO?'Ejemplo: remedio anotado (no se guarda).':'Remedio guardado.'); location.replace('#remedios');
  }catch(e){ if(e.kind==='auth') return toLogin(); b.disabled=false; b.textContent='Guardar remedio';
    if (e.kind==='offline'||e.kind==='timeout') return err('Se cortó la conexión. Lo que escribió sigue aquí: intente de nuevo cuando tenga señal.'); err(e.message); }
};
H.borrar = async id => {
  if (!confirm('¿Borrar este remedio? Si lo agregó a su calendario con el archivo, borre también esos recordatorios en el calendario.')) return;
  try{ if(DEMO){ const k=demoKey(rk()); DEMO_REM[k]=(DEMO_REM[k]||[]).filter(m=>m.id!==id); H.rem[rk()]=DEMO_REM[k]; }
       else { await api('/portal/api/herramientas/remedios/'+id,{method:'DELETE'}); await loadRem(true); }
       H.form=null; toast('Remedio borrado.'); location.replace('#remedios'); }
  catch(e){ if(e.kind==='auth') return toLogin(); toast(e.message); }
};

/* ── Recordatorios: UN archivo .ics por persona (UID estables + SEQUENCE) y, opcional,
      un link privado de suscripción que mantiene el calendario al día solo. ── */
const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent);
VIEWS.alarmas = () => {
  if (H.rem[rk()]===undefined){ loadRem().then(rerender('alarmas')); return `<h1>Recordatorios en su calendario</h1>${skel(3,72)}`; }
  const list = (H.rem[rk()]||[]); const con = Array.isArray(list) ? list.filter(m=>(m.horarios||[]).length) : [];
  if (!con.length){ setTimeout(()=>go('remedios'),0); return ''; }
  const ios = isIOS(); const n = con.reduce((a,m)=>a+m.horarios.length,0); const de = S.d.is_dependent?' de '+esc(first(S.d.nombre)):'';
  return `<span class="step">Mis remedios</span><h1>Recordatorios en su calendario</h1>
    <p class="lead">Ponemos cada toma en el calendario de su teléfono. Le avisa su calendario, según cómo lo tenga configurado.</p>
    <div class="card" style="margin-top:14px"><p><b>${n} ${n===1?'recordatorio':'recordatorios'}</b> para ${con.length===1?'1 remedio':con.length+' remedios'}${de}:</p>
      <ul class="stack" style="padding-left:20px;margin-top:8px">${con.map(m=>`<li>${esc(m.nombre)}: ${esc(horasTxt(m).replace(/^A las/,"a las"))}, ${esc(diasTxt(m).toLowerCase())}</li>`).join('')}</ul></div>
    <h2>Cómo se hace</h2>
    <ol class="steps"><li>Toque el botón azul de abajo.</li>
      <li>${ios?'El teléfono le muestra los recordatorios. Toque <b>Agregar todo</b>.':'Abra el archivo que se descargó (<b>mis-remedios.ics</b>) y elija su <b>Calendario</b>. Toque <b>Importar</b> o <b>Guardar</b>.'}</li>
      <li>Listo. A la hora de cada toma, su calendario le muestra un aviso.</li></ol>
    <a class="btn pri" style="margin-top:18px" ${DEMO?`href="#" onclick="CMC_H.icsDemo();return false"`:`href="/portal/api/herramientas/remedios.ics" download="mis-remedios.ics" onclick="track('remedios_ics')"`}>${ic('cal')}Agregar recordatorios al calendario</a>
    <div class="note info" style="margin-top:14px">${ic('info')}<div>Si cambia un remedio, vuelva a tocar el botón; si después ve un aviso repetido, bórrelo en el calendario.</div></div>
    ${!ios?'<p class="muted small" style="margin-top:10px">Si no encuentra el archivo, búsquelo en la carpeta <b>Descargas</b> o en las notificaciones del teléfono.</p>':''}
    <details class="acc" style="margin-top:18px" ${H.cal.abierto?'open':''} ontoggle="CMC_H.cal.abierto=this.open;if(this.open)CMC_H.calEstado()"><summary><b>Que se actualice solo<small>Opcional: un link privado para su calendario</small></b>${ic('chev','class="chev"')}</summary>
      <div class="body" id="calBox">${calBox()}</div></details>
    ${helpBlock('Hola, necesito ayuda para agregar los recordatorios de mis remedios al calendario del celular.')}`;
};
function calBox(){
  const c = H.cal[rk()] || {};
  const intro = `<p>Con un link privado, su calendario se pone al día solo cuando usted cambia un remedio: no tiene que repetir los pasos. El link muestra solo el nombre del remedio y la hora.</p>`;
  if (c.cargando) return intro + skel(1,58);
  if (c.error) return intro + errorBlock(c.error, 'CMC_H.calEstado(true)', 'Hola, necesito ayuda con el link de calendario de mis remedios.');
  if (c.url){
    const g = 'https://calendar.google.com/calendar/r?cid='+encodeURIComponent(c.webcal);
    return `<div class="note ok" role="status">${ic('check')}<div><b>Su link privado está listo.</b> Agréguelo una sola vez en este teléfono.</div></div>
      <div class="btns" style="margin-top:12px">
        ${isIOS()?`<a class="btn pri" href="${esc(c.webcal)}">${ic('cal')}Agregar al calendario del iPhone</a><a class="btn sec" href="${esc(g)}" target="_blank" rel="noopener">Usar Google Calendar</a>`
                 :`<a class="btn pri" href="${esc(g)}" target="_blank" rel="noopener">${ic('cal')}Agregar a Google Calendar</a><a class="btn sec" href="${esc(c.webcal)}">Otro calendario</a>`}
        <button class="btn sec" onclick="CMC_H.calCopiar()">Copiar el link</button></div>
      <p class="muted small" style="margin-top:10px">Los cambios pueden tardar unas horas en aparecer en su calendario. No comparta este link: quien lo tenga ve los nombres de sus remedios.</p>
      <button class="btn link" onclick="CMC_H.calRevocar()">Desactivar el link</button>`;
  }
  if (c.activa) return intro + `<div class="note info">${ic('info')}<div>Ya tiene un link activo${c.creada?' (creado el '+esc(fMes(String(c.creada).slice(0,10)))+')':''}. Si lo quiere agregar en otro teléfono, cree uno nuevo: el anterior deja de funcionar.</div></div>
      <div class="btns" style="margin-top:12px"><button class="btn sec" onclick="CMC_H.calCrear()">Crear un link nuevo</button><button class="btn link" onclick="CMC_H.calRevocar()">Desactivar el link</button></div>`;
  return intro + `<button class="btn sec" style="margin-top:12px" onclick="CMC_H.calCrear()">Crear mi link privado</button>
    ${DEMO?'<p class="muted small" style="margin-top:8px">Ejemplo: no se crea ningún link de verdad.</p>':''}`;
}
const calPaint = () => { const b=$('#calBox'); if(b) b.innerHTML = calBox(); };
H.calEstado = async (force) => {
  const r = rk(); if (!force && H.cal[r] && !H.cal[r].error) return calPaint();
  if (DEMO){ H.cal[r] = H.cal[r] || {}; return calPaint(); }
  H.cal[r] = {cargando:true}; calPaint();
  try{ const x = await api('/portal/api/herramientas/calendario'); H.cal[r] = {activa:!!x.activa, creada:x.creada}; }
  catch(e){ if(e.kind==='auth') return toLogin(); H.cal[r] = {error:e}; }
  calPaint();
};
H.calCrear = async () => {
  const r = rk(); if (H.cal[r] && H.cal[r].activa && !confirm('El link anterior dejará de funcionar. ¿Crear uno nuevo?')) return;
  if (DEMO){ H.cal[r] = {url:location.origin+'/portal/cal/ejemplo.ics', webcal:'webcal://'+location.host+'/portal/cal/ejemplo.ics', demo:true}; calPaint(); toast('Ejemplo: este link no funciona de verdad.'); return; }
  H.cal[r] = {cargando:true}; calPaint();
  try{ const x = await api('/portal/api/herramientas/calendario',{method:'POST',body:'{}'}); H.cal[r] = {url:x.url, webcal:x.webcal, activa:true}; track('remedios_cal_link'); say('Su link privado está listo.'); }
  catch(e){ if(e.kind==='auth') return toLogin(); H.cal[r] = {error:e}; }
  calPaint();
};
H.calCopiar = async () => { const c=H.cal[rk()]||{}; try{ await navigator.clipboard.writeText(c.url); toast('Link copiado.'); }catch(e){ prompt('Copie este link:', c.url); } };
H.calRevocar = async () => {
  if (!confirm('¿Desactivar el link? Su calendario dejará de recibir cambios. Los avisos ya agregados se borran desde el calendario.')) return;
  const r = rk();
  try{ if (!DEMO) await api('/portal/api/herramientas/calendario',{method:'DELETE'}); H.cal[r] = {activa:false}; toast('Link desactivado.'); calPaint(); }
  catch(e){ if(e.kind==='auth') return toLogin(); toast(e.message); }
};

/* Generador .ics del modo ejemplo (el real lo arma el servidor; mismo formato RFC 5545). */
function icsEsc(s){ return String(s).replace(/\\/g,'\\\\').replace(/;/g,'\\;').replace(/,/g,'\\,').replace(/\r?\n/g,'\\n'); }
function fold(l){ const out=[]; let cur='', n=0; for(const ch of l){ const w=new TextEncoder().encode(ch).length, lim=out.length?74:75; if(n+w>lim){ out.push(cur); cur=''; n=0; } cur+=ch; n+=w; } out.push(cur); return out.join('\r\n '); }
function icsTexto(rut, list){
  const now=new Date(); const p2=x=>String(x).padStart(2,'0');
  const stamp=now.getUTCFullYear()+p2(now.getUTCMonth()+1)+p2(now.getUTCDate())+'T'+p2(now.getUTCHours())+p2(now.getUTCMinutes())+p2(now.getUTCSeconds())+'Z';
  const L=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//Centro Medico Carampangue//Portal del Paciente//ES','CALSCALE:GREGORIAN','METHOD:PUBLISH','X-WR-CALNAME:Mis remedios','X-WR-TIMEZONE:America/Santiago',
    'BEGIN:VTIMEZONE','TZID:America/Santiago','BEGIN:STANDARD','DTSTART:19700405T000000','TZOFFSETFROM:-0300','TZOFFSETTO:-0400','TZNAME:-04','RRULE:FREQ=YEARLY;BYMONTH=4;BYDAY=SU;BYMONTHDAY=2,3,4,5,6,7,8','END:STANDARD',
    'BEGIN:DAYLIGHT','DTSTART:19700906T000000','TZOFFSETFROM:-0400','TZOFFSETTO:-0300','TZNAME:-03','RRULE:FREQ=YEARLY;BYMONTH=9;BYDAY=SU;BYMONTHDAY=2,3,4,5,6,7,8','END:DAYLIGHT','END:VTIMEZONE'];
  const DI=['MO','TU','WE','TH','FR','SA','SU'];
  list.forEach(m=>(m.horarios||[]).forEach(h=>{
    let d=today(); if(m.dias&&m.dias.length){ for(let i=0;i<7&&!m.dias.includes(wd(d));i++) d.setDate(d.getDate()+1); }
    const t='Tomar '+m.nombre+(m.dosis?' ('+m.dosis+')':'');
    L.push('BEGIN:VEVENT','UID:cmc-remedio-ej-'+m.uid+'-'+h.replace(':','')+'@centromedicocarampangue.cl','SEQUENCE:'+(m.seq||0),'DTSTAMP:'+stamp,
      'DTSTART;TZID=America/Santiago:'+d.getFullYear()+p2(d.getMonth()+1)+p2(d.getDate())+'T'+h.replace(':','')+'00','DURATION:PT5M',
      (m.dias&&m.dias.length)?'RRULE:FREQ=WEEKLY;BYDAY='+m.dias.map(i=>DI[i]).join(','):'RRULE:FREQ=DAILY',
      'SUMMARY:'+icsEsc(t),'DESCRIPTION:'+icsEsc('Recordatorio que usted anotó en su portal. Siga siempre la indicación de su médico. Centro Médico Carampangue.'),'TRANSP:TRANSPARENT',
      'BEGIN:VALARM','ACTION:DISPLAY','TRIGGER:PT0S','DESCRIPTION:'+icsEsc(t),'END:VALARM','END:VEVENT'); }));
  L.push('END:VCALENDAR'); return L.map(fold).join('\r\n')+'\r\n';
}
H.icsTexto = icsTexto;
H.icsDemo = () => { const txt=icsTexto(S.d.rut, (H.rem[rk()]||[]).filter(m=>(m.horarios||[]).length));
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([txt],{type:'text/calendar;charset=utf-8'})); a.download='mis-remedios.ics'; document.body.appendChild(a); a.click(); a.remove();
  toast('Ejemplo: se descargó un archivo de recordatorios con remedios inventados.'); };

/* ════════════════ 2 · FICHA DE EMERGENCIA ════════════════ */
function edadTxt(){ const e=edadDe(S.d.fecha_nacimiento); return e!=null ? e+' años' : ''; }
function remediosFicha(f){ if (f && f.incluir_remedios===false) return []; const l=H.rem[rk()]; return Array.isArray(l)?l:[]; }
function qrTexto(f){
  const rems = remediosFicha(f).map(m=>m.nombre+(m.dosis?' ('+m.dosis+')':'')+((m.horarios||[]).length?' '+m.horarios.join(' y '):''));
  const L = ['FICHA DE EMERGENCIA MEDICA', 'Nombre: '+S.d.nombre];
  if (edadTxt()) L.push('Edad: '+edadTxt()+(S.d.fecha_nacimiento?' (nac. '+fMes(S.d.fecha_nacimiento)+')':''));
  L.push('ALERGIAS: '+(f.alergias||'No anotadas'));
  if (f.enfermedades) L.push('Enfermedades: '+f.enfermedades);
  if (rems.length) L.push('Remedios: '+rems.join('; '));
  if (f.grupo_sanguineo) L.push('Grupo sanguineo: '+f.grupo_sanguineo);
  if (f.notas) L.push('Notas: '+f.notas);
  if (f.contacto_nombre||f.contacto_telefono) L.push('Contacto de emergencia: '+[f.contacto_nombre,f.contacto_telefono].filter(Boolean).join(' '));
  L.push('Datos escritos por el paciente. Centro Medico Carampangue (44) 296 5226');
  return L.join('\n');
}
H.qrTexto = () => { const x=H.ficha[rk()]; return x&&x.ficha ? qrTexto(x.ficha) : ''; };
function telHref(t){ const d=String(t||'').replace(/[^\d+]/g,''); return d.length>=8 ? 'tel:'+d : ''; }
function tarjeta(f){
  const rems = remediosFicha(f);
  return `<article class="ecard" aria-label="Ficha de emergencia de ${esc(S.d.nombre)}">
    <div class="ecard-h">${ic('siren')}<span>Ficha de emergencia médica</span></div>
    <div class="ecard-b">
      <p class="en">${esc(S.d.nombre)}</p><p class="muted">${esc(edadTxt())}${f.grupo_sanguineo?` · Grupo sanguíneo <b class="gs">${esc(f.grupo_sanguineo)}</b>`:''}</p>
      <div class="alg"><span>Alergias</span><b>${esc(f.alergias||'No anotadas')}</b></div>
      ${f.enfermedades?`<dl><div class="kv"><dt>Enfermedades</dt><dd>${esc(f.enfermedades)}</dd></div></dl>`:''}
      ${rems.length?`<dl><div class="kv"><dt>Remedios</dt><dd>${rems.map(m=>esc(m.nombre)+(m.dosis?` <span class="muted">(${esc(m.dosis)})</span>`:'')).join('<br>')}</dd></div></dl>`:''}
      ${f.notas?`<dl><div class="kv"><dt>Importante</dt><dd>${esc(f.notas)}</dd></div></dl>`:''}
      ${(f.contacto_nombre||f.contacto_telefono)?`<div class="ctc"><span class="muted">En caso de emergencia, llamar a</span><b>${esc(f.contacto_nombre||'')}</b>${telHref(f.contacto_telefono)?`<a class="btn ok" href="${telHref(f.contacto_telefono)}">${ic('phone')}Llamar ${esc(f.contacto_telefono)}</a>`:esc(f.contacto_telefono||'')}</div>`:''}
    </div></article>`;
}
VIEWS.emergencia = () => {
  const x = H.ficha[rk()];
  if (x===undefined) return `<h1>Ficha de emergencia</h1>${skel(1,320)}`;
  if (x && x.error) return `<h1>Ficha de emergencia</h1>${errorBlock(x.error,"CMC_H.ficha['"+jsq(rk())+"']=undefined;render()")}`;
  if (!x) return `<h1>Ficha de emergencia</h1>${deQuien()}
    <p class="lead" style="margin-top:8px">Si le pasa algo en la calle o en la micro, quien le ayude puede ver en su celular sus alergias, enfermedades, remedios y a quién llamar.</p>
    <div class="card" style="margin-top:16px"><ul class="stack" style="padding-left:20px"><li>Una tarjeta grande para mostrar en el celular.</li><li>Una versión para imprimir del tamaño de su cédula, para la billetera.</li><li>Un código QR con el mismo texto, que se lee sin internet.</li></ul></div>
    <button class="btn aqua" style="margin-top:16px" onclick="go('fichaedit')">${ic('plus')}Crear mi ficha</button>
    <p class="muted small" style="margin-top:12px">Toma 2 minutos. Usted decide qué anotar y puede borrarla cuando quiera.</p>`;
  const f = x.ficha;
  return `<h1>Ficha de emergencia</h1>${deQuien()}
    <div style="margin-top:14px">${tarjeta(f)}</div>
    <p class="muted small" style="margin-top:8px">${x.updated_at?'Actualizada el '+esc(fMes(String(x.updated_at).slice(0,10)))+'. ':''}Datos escritos por usted; no los revisa un médico.</p>
    <div class="btns" style="margin-top:16px"><button class="btn pri" onclick="CMC_H.verQR()">${ic('qr')}Mostrar el código QR</button>
      <button class="btn sec" onclick="CMC_H.imprimir()">${ic('print')}Imprimir para la billetera</button>
      <button class="btn sec" onclick="go('fichaedit')">${ic('edit')}Corregir mi ficha</button>
      <button class="btn link" onclick="CMC_H.borrarFicha()">Borrar mi ficha</button></div>
    <div class="note info" style="margin-top:14px">${ic('info')}<div>El código QR guarda el texto de la ficha. No es un link: nadie la puede ver por internet.</div></div>`;
};
VIEWS.emergencia.after = () => { const r=rk(); if(H.ficha[r]===undefined) loadFicha().then(rerender('emergencia')); if(H.rem[r]===undefined) loadRem().then(rerender('emergencia')); };
H.verQR = async () => {
  try{ await loadScript(STATIC+'qr.js'); }catch(e){ return toast(e.message); }
  let svg; try{ svg = CMC_QR.svg(H.qrTexto(), {label:'Código QR con la ficha de emergencia'}); }catch(e){ return toast(e.message); }
  openSheet(`<h2 id="shT" style="margin:6px 0 4px">Código de su ficha</h2><p class="muted small">Quien le ayude lo puede leer con la cámara del teléfono. No necesita internet.</p>
    <div class="qrbox" style="margin-top:12px">${svg}</div>
    <button class="btn sec" style="margin-top:14px" onclick="closeSheet()">Cerrar</button>`);
  track('ficha_qr');
};
H.imprimir = async () => {
  const x=H.ficha[rk()]; if(!x||!x.ficha) return; const f=x.ficha;
  try{ await loadScript(STATIC+'qr.js'); }catch(e){ return toast(e.message); }
  const w = window.open('','_blank'); if(!w){ toast('Permita las ventanas emergentes para imprimir.'); return; }
  const rems = remediosFicha(f).map(m=>esc(m.nombre)+(m.dosis?' ('+esc(m.dosis)+')':'')).join('; ');
  w.document.write(`<!DOCTYPE html><html lang="es-CL"><head><meta charset="utf-8"><title>Ficha de emergencia — ${esc(S.d.nombre)}</title><style>
    @page{size:A4;margin:12mm}*{box-sizing:border-box}body{font-family:Arial,Helvetica,sans-serif;color:#000;margin:0;padding:10mm}
    .c{width:85.6mm;height:54mm;border:1px solid #000;border-radius:3mm;overflow:hidden;display:inline-block;vertical-align:top;margin:0 6mm 6mm 0;position:relative;font-size:7.6pt;line-height:1.25}
    .h{background:#B3261E;color:#fff;font-weight:700;font-size:8.5pt;padding:1.6mm 3mm;letter-spacing:.02em}.b{padding:2mm 3mm}
    .n{font-size:10.5pt;font-weight:700}.a{border:1px solid #B3261E;background:#FCEBE8;padding:1mm 2mm;margin:1.2mm 0;font-weight:700}
    .q{display:flex;gap:3mm;padding:2.5mm 3mm}.q svg{width:36mm;height:36mm;flex:none}.cut{font-size:9pt;color:#555;margin:0 0 4mm}
    p{margin:.6mm 0}button{font-size:14pt;padding:10px 18px;margin-bottom:8mm}@media print{button,.cut{display:none}}</style></head><body>
    <button onclick="window.print()">Imprimir</button><p class="cut">Recorte por el borde de cada tarjeta, dóblela al medio y guárdela junto a su cédula.</p>
    <div class="c"><div class="h">FICHA DE EMERGENCIA MÉDICA</div><div class="b"><p class="n">${esc(S.d.nombre)}</p>
      <p>${esc(edadTxt())}${f.grupo_sanguineo?' · Grupo sanguíneo <b>'+esc(f.grupo_sanguineo)+'</b>':''}</p>
      <p class="a">ALERGIAS: ${esc(f.alergias||'No anotadas')}</p>${f.enfermedades?'<p><b>Enfermedades:</b> '+esc(f.enfermedades)+'</p>':''}
      ${(f.contacto_nombre||f.contacto_telefono)?'<p><b>Llamar a:</b> '+esc([f.contacto_nombre,f.contacto_telefono].filter(Boolean).join(' · '))+'</p>':''}</div></div>
    <div class="c"><div class="h">REMEDIOS Y CÓDIGO QR</div><div class="q">${CMC_QR.svg(qrTexto(f))}<div>
      ${rems?'<p><b>Remedios:</b> '+rems+'</p>':''}${f.notas?'<p><b>Importante:</b> '+esc(f.notas)+'</p>':''}
      <p style="margin-top:2mm;color:#333">El código tiene el mismo texto. Se lee con la cámara, sin internet.</p><p style="color:#333">Datos escritos por el paciente.</p></div></div></div>
    <script>setTimeout(function(){window.print()},300)<\/script></body></html>`);
  w.document.close(); track('ficha_imprime');
};
H.borrarFicha = async () => {
  if (!confirm('¿Borrar su ficha de emergencia? Se borra de su portal. Si la imprimió, bote también el papel.')) return;
  try{ if(DEMO){ delete DEMO_FICHA[demoKey(rk())]; } else await api('/portal/api/herramientas/ficha',{method:'DELETE'});
    H.ficha[rk()]=null; toast('Ficha borrada.'); render(); }
  catch(e){ if(e.kind==='auth') return toLogin(); toast(e.message); }
};

/* ── Crear / corregir la ficha (con consentimiento explícito) ── */
VIEWS.fichaedit = () => {
  const x = H.ficha[rk()]; if (x===undefined){ setTimeout(()=>loadFicha().then(rerender('fichaedit')),0); return `<h1>Ficha de emergencia</h1>${skel(4,60)}`; }
  const prev = x && x.ficha ? x.ficha : null; const p = S.perfil||{};
  if (!H.fform || H.fform.rut!==rk()) H.fform = {rut:rk(), ...(prev||{alergias:'',enfermedades:'',grupo_sanguineo:'',contacto_nombre:p.contacto_emerg_nombre||'',contacto_telefono:p.contacto_emerg_telefono||'',notas:'',incluir_remedios:true}), consent:!!prev, err:''};
  const f = H.fform; const dxs = (S.d.diagnosticos||[]).map(dxNombre).filter(Boolean); const nm = S.d.is_dependent ? first(S.d.nombre) : '';
  const t = (id,lbl,hint,ph,rows) => `<label class="lbl" for="fe_${id}">${lbl}</label><textarea id="fe_${id}" class="inp sm ta" rows="${rows||2}" maxlength="400" placeholder="${ph}" oninput="CMC_H.fform.${id}=this.value">${esc(f[id]||'')}</textarea>${hint?`<p class="hint">${hint}</p>`:''}`;
  return `<span class="step">Ficha de emergencia${nm?' de '+esc(nm):''}</span><h1>${prev?'Corregir la ficha':'Crear la ficha'}</h1>
    <p class="lead">Escriba solo lo que quiera que vea quien le ayude en una emergencia.</p>
    <form onsubmit="CMC_H.guardarFicha(event)" novalidate>
      ${t('alergias','Alergias','Si no tiene, escriba «Ninguna conocida».','Ej.: penicilina, mariscos')}
      ${t('enfermedades','Enfermedades','','Ej.: presión alta, diabetes')}
      ${dxs.length?`<button type="button" class="btn sec" style="margin-top:8px;min-height:52px;font-size:1rem" onclick="CMC_H.usarDx()">Usar las que el centro tiene anotadas</button>`:''}
      <label class="lbl" for="fe_gs">Grupo sanguíneo <span class="muted">(opcional)</span></label>
      <select id="fe_gs" class="inp sm" onchange="CMC_H.fform.grupo_sanguineo=this.value">${['','A+','A-','B+','B-','AB+','AB-','O+','O-'].map(g=>`<option value="${g}" ${g===(f.grupo_sanguineo||'')?'selected':''}>${g||'No lo sé'}</option>`).join('')}</select>
      <label class="lbl" for="fe_cn">Contacto de emergencia: nombre</label><input id="fe_cn" class="inp sm" maxlength="60" value="${esc(f.contacto_nombre||'')}" placeholder="Ej.: Juan (esposo)" oninput="CMC_H.fform.contacto_nombre=this.value">
      <label class="lbl" for="fe_ct">Contacto de emergencia: teléfono</label><input id="fe_ct" class="inp sm" type="tel" inputmode="tel" maxlength="20" value="${esc(f.contacto_telefono||'')}" placeholder="+56 9 1234 5678" oninput="CMC_H.fform.contacto_telefono=this.value">
      ${t('notas','Algo importante que deban saber <span class="muted">(opcional)</span>','','Ej.: usa marcapasos, usa audífonos')}
      <label class="check" style="margin-top:16px"><input type="checkbox" ${f.incluir_remedios!==false?'checked':''} onchange="CMC_H.fform.incluir_remedios=this.checked"><span>Mostrar los remedios que anoté en «Mis remedios»</span></label>
      <label class="check consent" style="margin-top:12px"><input type="checkbox" id="fe_ok" ${f.consent?'checked':''} onchange="CMC_H.fform.consent=this.checked"><span><b>Acepto que el centro guarde estos datos de salud</b>${nm?' de '+esc(nm):''} para mostrarlos en esta ficha. Solo se ven en este portal y puedo borrarlos cuando quiera (<a href="/privacidad" target="_blank" rel="noopener">Ley 21.719</a>).</span></label>
      <div id="feErr">${f.err?`<p class="err" role="alert">${ic('warn','style="width:22px;height:22px;flex:none"')}<span>${esc(f.err)}</span></p>`:''}</div>
      <div class="btns" style="margin-top:22px"><button class="btn pri" id="feBtn" type="submit">Guardar mi ficha</button><button class="btn sec" type="button" onclick="goBack()">Cancelar</button></div>
    </form>`;
};
VIEWS.fichaedit.after = () => { if(H.rem[rk()]===undefined) loadRem(); };
H.usarDx = () => { const dxs=(S.d.diagnosticos||[]).map(dxNombre).filter(Boolean); const f=H.fform; const cur=(f.enfermedades||'').trim(); f.enfermedades = cur ? cur+'; '+dxs.filter(d=>!cur.includes(d)).join('; ') : dxs.join('; '); f.enfermedades=f.enfermedades.replace(/;\s*$/,''); render(); };
H.guardarFicha = async (ev) => {
  ev.preventDefault(); const f=H.fform; const err=m=>{ f.err=m; render(); const e=$('#feErr'); e&&e.scrollIntoView({block:'center'}); };
  if (!f.consent) return err('Para guardar, marque la casilla de aceptación. Son datos de salud y necesitamos su permiso.');
  if (!String(f.alergias||'').trim() && !String(f.enfermedades||'').trim() && !String(f.contacto_telefono||'').trim()) return err('Escriba al menos sus alergias, una enfermedad o un contacto.');
  const body = {consent:true, alergias:f.alergias, enfermedades:f.enfermedades, grupo_sanguineo:f.grupo_sanguineo||'', contacto_nombre:f.contacto_nombre, contacto_telefono:f.contacto_telefono, notas:f.notas, incluir_remedios:f.incluir_remedios!==false};
  const b=$('#feBtn'); b.disabled=true; b.textContent='Guardando…';
  try{
    if (DEMO){ await sleep(300); const {consent, ...ficha}=body; DEMO_FICHA[demoKey(rk())] = {ficha, updated_at:iso(today())}; H.ficha[rk()] = DEMO_FICHA[demoKey(rk())]; }
    else { await api('/portal/api/herramientas/ficha',{method:'POST',body:JSON.stringify(body)}); await loadFicha(true); }
    H.fform=null; toast(DEMO?'Ejemplo: ficha guardada (no se guarda de verdad).':'Ficha guardada.'); location.replace('#emergencia');
  }catch(e){ if(e.kind==='auth') return toLogin(); b.disabled=false; b.textContent='Guardar mi ficha';
    if (e.kind==='offline'||e.kind==='timeout') return err('Se cortó la conexión. Lo que escribió sigue aquí: intente de nuevo cuando tenga señal.'); err(e.message); }
};

/* ════════════════ 3 · QUÉ ME TOCA ESTE AÑO ════════════════ */
function personas(){ return members().filter(m=>m.acceso!=='solo_horas_agendadas'); }
function personaActiva(rr){ const r=rr||H.tocaRut||rk(); const m=memberByRut(r)||{}; const act=rutClean(r)===rk();
  const fn = act ? S.d.fecha_nacimiento : null;
  return {rut:r, nombre:m.nombre||(act?S.d.nombre:''), sexo:(m.sexo||(act?S.d.sexo:'')||'').toUpperCase().slice(0,1), edad:m.edad!=null?m.edad:edadDe(fn), meses:fn?mesesDe(fn):null, relation:m.relation}; }
function mesesDe(fn){ const d=pDate(fn); if(!d) return null; const t=new Date(); let m=(t.getFullYear()-d.getFullYear())*12+(t.getMonth()-d.getMonth()); if(t.getDate()<d.getDate()) m--; return Math.max(0,m); }
const hechoKey = (p,it) => 'cmc_toca_'+rutClean(p.rut)+'_'+it.id+'_'+new Date().getFullYear();
const hecho = (it,p) => !!load(hechoKey(p||personaActiva(), it));
function itemsPara(p){
  const g=H.guia; if(!g||g.error||p.edad==null) return [];
  let out = g.items.filter(it=>p.edad>=it.edad_min && p.edad<=it.edad_max && (!it.sexo || it.sexo===p.sexo));
  if (out.some(it=>it.id==='mamografia_ges')) out = out.filter(it=>it.id!=='mamografia');
  if (p.edad<15){
    const lo = p.meses!=null ? p.meses : p.edad*12, hi = p.meses!=null ? p.meses : p.edad*12+11;
    const vs = g.vacunas.filter(v=>v.desde_meses<=hi && v.hasta_meses>lo);
    const grp = (donde) => vs.filter(v=>v.donde===donde);
    const mk = (id,titulo,list,donde,txt) => ({id, titulo, que:list.map(v=>v.vacuna).join(' · '), frecuencia:'Según el calendario de vacunas', donde, donde_txt:txt, vacunas:list});
    if (grp('vacunatorio').length) out.push(mk('vac_edad','Vacunas que le tocan a su edad',grp('vacunatorio'),'vacunatorio','Gratis en un vacunatorio público, con el carnet de vacunas. El centro no pone vacunas.'));
    if (grp('colegio').length) out.push(mk('vac_cole','Vacunas en el colegio',grp('colegio'),'colegio','Se ponen en el colegio, según el curso. Si faltó ese día, en un vacunatorio público.'));
  }
  return out;
}
function proximaVacuna(p){ const g=H.guia; if(!g||g.error||p.edad==null||p.edad>=15) return null; const m = p.meses!=null?p.meses:p.edad*12+11;
  const nx = g.vacunas.filter(v=>v.desde_meses>m).sort((a,b)=>a.desde_meses-b.desde_meses); if(!nx.length) return null;
  const at = nx[0].desde_meses; return {cuando: at<24 ? 'a los '+at+' meses' : 'a los '+Math.floor(at/12)+' años', vac:nx.filter(v=>v.desde_meses===at).map(v=>v.vacuna)}; }
const DONDE = {cmc:['Aquí en el centro','ok'], fuera:['Fuera, con orden del centro','info'], cesfam:['En su CESFAM','info'], vacunatorio:['En el vacunatorio','info'], colegio:['En el colegio','info']};
VIEWS.toca = () => {
  const g = H.guia; const ps = personas(); const p = personaActiva();
  if (!g) return `<h1>Qué le toca este año</h1>${skel(3,120)}`;
  if (g.error) return `<h1>Qué le toca este año</h1>${errorBlock(g.error,'CMC_H.guia=null;render()')}`;
  const restr = members().filter(m=>m.acceso==='solo_horas_agendadas');
  const items = itemsPara(p); const yo = rutClean(p.rut)===ownerRut(); const nm = yo ? '' : first(p.nombre);
  const prox = proximaVacuna(p);
  const card = it => { const done = hecho(it,p); const dd = DONDE[it.donde]||DONDE.cmc;
    return `<div class="card toca${done?' done':''}"><div class="toca-h"><h3>${esc(it.titulo)}</h3><span class="pill ${dd[1]}">${esc(dd[0])}</span></div>
      <p style="margin-top:6px">${esc(it.que)}</p><p class="muted small" style="margin-top:4px">${esc(it.frecuencia)}${it.edad_min!=null&&it.id!=='control_sano'&&!it.vacunas?' · de '+it.edad_min+(it.edad_max>=99?' años en adelante':' a '+it.edad_max+' años'):''}</p>
      <p class="donde">${ic(it.donde==='cmc'?'building':'pin','style="width:20px;height:20px;flex:none;margin-top:2px"')}<span>${esc(it.donde_txt)}</span></p>
      ${it.esp && !done?`<button class="btn ${it.donde==='cmc'?'pri':'sec'}" style="margin-top:12px" onclick="CMC_H.pedir('${jsq(it.id)}')">${it.donde==='cmc'?'Pedir hora de '+esc(it.esp.toLowerCase())+(nm?' para '+esc(nm):''):it.donde==='fuera'?'Pedir la orden aquí':'Prefiero un chequeo aquí'}</button>`:''}
      <button class="mark" aria-pressed="${done}" onclick="CMC_H.hecho('${jsq(it.id)}')">${done?ic('check')+'Ya está hecho este año':'Marcar como hecho este año'}</button></div>`; };
  const pend = items.filter(it=>!hecho(it,p)), listos = items.filter(it=>hecho(it,p));
  return `<h1>Qué le toca este año</h1><p class="lead">Controles y vacunas según la edad, con la misma guía que usa el centro.</p>
    ${ps.length>1?`<div class="chips who-chips" role="group" aria-label="Persona" style="margin-top:14px">${ps.map(m=>`<button class="chip" aria-pressed="${rutClean(m.rut)===rutClean(p.rut)}" onclick="CMC_H.tocaRut='${jsq(m.rut)}';render()">${esc(first(m.nombre))}${rutClean(m.rut)===ownerRut()?' (usted)':''}</button>`).join('')}</div>`:''}
    <h2>${nm?esc(nm)+', ':''}${p.edad!=null?(p.edad<2&&p.meses!=null?p.meses+' meses':p.edad+' años'):''}</h2>
    ${p.edad==null?`<div class="note info">${ic('info')}<div>No tenemos la fecha de nacimiento${nm?' de '+esc(nm):''}. Agréguela en <a href="#datos">Mis datos</a> para ver qué le toca.</div></div>`:''}
    ${p.edad!=null&&!p.sexo?`<div class="note info" style="margin-bottom:12px">${ic('info')}<div>Falta el sexo en la ficha; por eso no mostramos los controles solo de mujeres o de hombres.</div></div>`:''}
    ${p.edad!=null&&!items.length?`<div class="card"><p>Para esta edad no hay controles fijos en la guía.</p><p class="muted small" style="margin-top:6px">Si algo le preocupa, pida una hora y lo conversamos.</p></div>`:''}
    <div class="stack">${pend.map(card).join('')}</div>
    ${prox?`<div class="note info" style="margin-top:12px">${ic('cal')}<div><b>Lo próximo:</b> ${esc(prox.cuando)}, ${esc(prox.vac.join(', '))}.</div></div>`:''}
    ${listos.length?`<h2>Ya hecho este año</h2><div class="stack">${listos.map(card).join('')}</div>`:''}
    ${restr.length?`<p class="muted small" style="margin-top:14px">De ${restr.map(m=>esc(first(m.nombre))).join(', ')} no mostramos esto porque el vínculo no está verificado.</p>`:''}
    <div class="note info" style="margin-top:18px">${ic('info')}<div>${esc(g.aviso)} El portal no interpreta resultados de exámenes.</div></div>
    ${helpBlock('Hola, quiero saber qué controles me tocan este año.')}`;
};
VIEWS.toca.after = () => { if(!H.guia) loadGuia().then(rerender('toca')); };
H.hecho = id => { const p=personaActiva(); const it=itemsPara(p).find(x=>x.id===id); if(!it) return; const k=hechoKey(p,it); load(k)?store(k):store(k,1); say(load(k)?'Marcado como hecho.':'Desmarcado.'); render(); };
H.pedir = id => { const p=personaActiva(); const it=itemsPara(p).find(x=>x.id===id); if(!it) return; track('toca_pedir',{id}); startAgendar({rut:p.rut, esp:it.esp, motivo:it.motivo, skipQuien:true}); };

/* ════════════════ Precarga para Inicio ════════════════ */
H.homeCard = homeCard;
/* Dato vivo para el acceso "Mis remedios" del Inicio (ronda 5). null = aún cargando. */
H.resumen = () => {
  if (!S.d || restringido()) return null; const list = H.rem[rk()];
  if (list===undefined || list===null) return null; if (!Array.isArray(list)) return {};
  if (!list.length) return {vacio:true};
  const hoy = tomasDe(list, today()); const done = tomadas(); const now = new Date();
  const ahora = String(now.getHours()).padStart(2,'0')+':'+String(now.getMinutes()).padStart(2,'0');
  const pend = hoy.filter(t=>!done.has(t.m.id+'|'+t.h)); const p = pend.find(t=>t.h>=ahora) || pend[0];
  const bajo = list.find(m=>{ const q=diasQuedan(m); return q && q.dias<=7; });
  return {total:hoy.length, prox: p ? {h:p.h, nombre:p.m.nombre} : null, bajo: bajo ? bajo.nombre.split(' ')[0] : ''};
};
H.precargar = async () => {
  if (!S.d || restringido()) return;
  const r = rk();
  await Promise.all([H.rem[r]===undefined?loadRem():null, H.guia?null:loadGuia(), loadTomas()]);
  if (route().name==='inicio') render();
};
})();
