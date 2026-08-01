"""Rutas de la cola de comprobantes WhatsApp — /alma/comprobantes.

Panel liviano para recepción: cada comprobante llega con la foto, los datos
extraídos por visión y las validaciones pre-cruzadas (cuenta destino, N° de
operación duplicado, paciente + cita). "Registrar pago" reutiliza el endpoint
existente POST /alma/api/pagos (misma atribución/validaciones de siempre) y
después marca el comprobante como confirmado. La plata nunca se registra sola.

Auth: mismo esquema del panel de pagos (token query o cookie de sesión).
Cero CDN (regla CSP del CMC): CSS/JS inline.
"""
from __future__ import annotations

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(tags=["comprobantes"])


def _auth(request: Request, token: str | None, cmc_session: str | None) -> None:
    from pagos_routes import _require_admin_dep
    _require_admin_dep(request, token=token, cmc_session=cmc_session)


@router.get("/alma/api/comprobantes")
async def api_listar(request: Request,
                     estado: str | None = Query(None),
                     token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    from comprobantes_pagos import listar_comprobantes
    return JSONResponse({"comprobantes": listar_comprobantes(estado=estado)})


@router.post("/alma/api/comprobantes/{comp_id}/estado")
async def api_marcar(comp_id: int, request: Request,
                     token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    estado = (body.get("estado") or "").strip()
    pago_id = body.get("pago_id")
    from comprobantes_pagos import marcar_comprobante
    if not marcar_comprobante(comp_id, estado, pago_id):
        raise HTTPException(400, "estado inválido")
    return JSONResponse({"ok": True})


_PAGE = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Comprobantes WhatsApp — CMC</title>
<style>
:root{--aqua:#4FBECE;--azul:#1172AB;--navy:#0F3F68;--bg:#f4f7f9;--ok:#1a7f4e;--bad:#c0392b;--warn:#b7791f}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:#1c2b36;padding:16px}
h1{font-size:1.15rem;color:var(--navy);margin-bottom:2px}
.sub{color:#5a7184;font-size:.82rem;margin-bottom:14px}
.filtros{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.filtros button{border:1px solid #cfdde6;background:#fff;border-radius:20px;padding:6px 14px;font-size:.82rem;cursor:pointer}
.filtros button.on{background:var(--azul);color:#fff;border-color:var(--azul)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.card{background:#fff;border-radius:12px;box-shadow:0 2px 8px rgba(15,63,104,.08);padding:14px;display:flex;flex-direction:column;gap:10px}
.top{display:flex;gap:12px}
.thumb{width:86px;height:110px;object-fit:cover;border-radius:8px;border:1px solid #e3ecf1;cursor:pointer;background:#eef3f6}
.monto{font-size:1.35rem;font-weight:700;color:var(--navy)}
.dato{font-size:.8rem;color:#42586b;line-height:1.45}
.badges{display:flex;gap:6px;flex-wrap:wrap}
.badge{font-size:.72rem;padding:3px 9px;border-radius:12px;font-weight:600}
.b-ok{background:#e2f5ea;color:var(--ok)} .b-bad{background:#fde8e4;color:var(--bad)}
.b-warn{background:#fdf3dd;color:var(--warn)} .b-neutro{background:#eef3f6;color:#5a7184}
.acciones{display:flex;gap:8px;margin-top:2px}
.acciones button{flex:1;border:none;border-radius:8px;padding:9px 0;font-size:.85rem;font-weight:600;cursor:pointer}
.b-reg{background:var(--azul);color:#fff} .b-desc{background:#eef3f6;color:#5a7184}
.estado-chip{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.4px}
.vacio{color:#8aa0b0;padding:40px;text-align:center;grid-column:1/-1}
dialog{border:none;border-radius:12px;max-width:92vw;max-height:92vh;padding:0}
dialog img{max-width:90vw;max-height:88vh;display:block}
</style></head><body>
<h1>Comprobantes de transferencia — WhatsApp</h1>
<div class="sub">Leídos automáticamente por visión · la cuenta destino y el N° de operación ya vienen verificados · registrar pago usa el formulario de pagos de siempre</div>
<div class="filtros">
  <button data-f="pendiente" class="on">Pendientes</button>
  <button data-f="confirmado">Confirmados</button>
  <button data-f="descartado">Descartados</button>
  <button data-f="">Todos</button>
</div>
<div class="grid" id="grid"><div class="vacio">Cargando…</div></div>
<dialog id="dlg"><img id="dlgimg" alt="comprobante"></dialog>
<script>
const TOKEN=new URLSearchParams(location.search).get('token')||'';
const $=s=>document.querySelector(s);
let filtro='pendiente';
const fmt=n=>'$'+(n||0).toLocaleString('es-CL');
async function cargar(){
  const r=await fetch(`/alma/api/comprobantes?token=${TOKEN}`+(filtro?`&estado=${filtro}`:''));
  if(!r.ok){$('#grid').innerHTML='<div class="vacio">Error de autenticación — abre con ?token=…</div>';return}
  const d=await r.json();render(d.comprobantes||[]);
}
function badge(c){
  const b=[];
  if(c.destinatario_ok===1)b.push('<span class="badge b-ok">✓ cuenta CMC</span>');
  else if(c.destinatario_ok===0)b.push('<span class="badge b-bad">✗ CUENTA NO ES DEL CMC</span>');
  else b.push('<span class="badge b-neutro">destino no legible</span>');
  if(c.duplicado_de)b.push(`<span class="badge b-bad">⚠ N° operación repetido (#${c.duplicado_de})</span>`);
  if(c.cita_especialidad)b.push(`<span class="badge b-ok">cita: ${c.cita_especialidad} ${c.cita_fecha} ${c.cita_hora}</span>`);
  else b.push('<span class="badge b-warn">sin cita próxima en el bot</span>');
  if(c.confianza==='baja')b.push('<span class="badge b-warn">lectura dudosa — mirar foto</span>');
  return b.join('');
}
function render(items){
  if(!items.length){$('#grid').innerHTML='<div class="vacio">Nada por aquí ✨</div>';return}
  $('#grid').innerHTML=items.map(c=>`
  <div class="card" id="c${c.id}">
    <div class="top">
      ${c.file_id?`<img class="thumb" src="/admin/api/file/${c.file_id}?token=${TOKEN}" onclick="ver(this.src)">`:'<div class="thumb"></div>'}
      <div>
        <div class="monto">${fmt(c.monto)}</div>
        <div class="dato">${c.paciente_nombre||'(tel. '+c.phone.slice(-8)+')'}${c.paciente_rut?' · '+c.paciente_rut:''}</div>
        <div class="dato">${c.banco||''} ${c.fecha_transf||''} ${c.hora_transf||''}</div>
        <div class="dato">Op: ${c.num_operacion||'—'} · <span class="estado-chip">${c.estado}</span></div>
      </div>
    </div>
    <div class="badges">${badge(c)}</div>
    ${c.estado==='pendiente'?`<div class="acciones">
      <button class="b-reg" onclick="registrar(${c.id})">Registrar pago</button>
      <button class="b-desc" onclick="marcar(${c.id},'descartado')">Descartar</button>
    </div>`:''}
  </div>`).join('');
}
function ver(src){$('#dlgimg').src=src;$('#dlg').showModal();$('#dlg').onclick=()=>$('#dlg').close()}
async function registrar(id){
  const r=await fetch(`/alma/api/comprobantes?token=${TOKEN}`);
  const c=(await r.json()).comprobantes.find(x=>x.id===id);
  if(!c)return;
  if(c.destinatario_ok===0&&!confirm('OJO: la cuenta destino NO es del CMC. ¿Registrar igual?'))return;
  if(c.duplicado_de&&!confirm('OJO: N° de operación repetido. ¿Registrar igual?'))return;
  const body={
    paciente_nombre:c.paciente_nombre||'(comprobante WhatsApp)',
    rut:c.paciente_rut||'',
    copago:c.monto||0,
    metodo_pago:'transferencia',
    codigo_transferencia:c.num_operacion||'',
    area:c.cita_especialidad||'',
    origen:'chat',canal:'bot',
    creado_por:'comprobante_whatsapp',
  };
  const rp=await fetch(`/alma/api/pagos?token=${TOKEN}`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!rp.ok){alert('Error registrando el pago: '+await rp.text());return}
  const pago=await rp.json();
  await fetch(`/alma/api/comprobantes/${id}/estado?token=${TOKEN}`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({estado:'confirmado',pago_id:pago.id||null})});
  cargar();
}
async function marcar(id,estado){
  await fetch(`/alma/api/comprobantes/${id}/estado?token=${TOKEN}`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({estado})});
  cargar();
}
document.querySelectorAll('.filtros button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.filtros button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');filtro=b.dataset.f;cargar();
});
cargar();setInterval(cargar,30000);
</script></body></html>"""


@router.get("/alma/comprobantes", response_class=HTMLResponse)
async def page_comprobantes(request: Request,
                            token: str | None = Query(None),
                            cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    return HTMLResponse(_PAGE)
