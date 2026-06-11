"""
app/patient_source_routes.py — captura y reporte de la fuente declarada del paciente.

Endpoints:
  POST /api/fuente                 → guarda la fuente (check-in self-service o recepción)
  GET  /api/fuente/{rut}           → ¿ya sabemos la fuente de este RUT?
  GET  /api/canal/stats            → mezcla de canales + cruce con ad spend (auth)
  GET  /alma/canal                 → vista de reporte (auth) con captura de recepción
  GET  /fuente                     → página autoservicio (RUT + botones) para QR/link
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

import patient_source as ps

log = logging.getLogger("bot.patient_source_routes")
router = APIRouter()

_CAC_SNAPSHOT = Path(__file__).parent.parent / "data" / "cac_snapshot.json"

# Rate limit simple por IP (set público).
_RL: dict[str, list[float]] = {}


def _rate_ok(key: str, limit: int, window: int) -> bool:
    now = time.time()
    xs = [t for t in _RL.get(key, []) if now - t < window]
    if len(xs) >= limit:
        _RL[key] = xs
        return False
    xs.append(now)
    _RL[key] = xs
    return True


def _client_ip(req: Request | None) -> str:
    if not req:
        return "?"
    return (req.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (req.client.host if req.client else "?"))


def _is_admin(token, cmc_session) -> bool:
    try:
        from admin_routes import _verify_cookie, _is_admin_token
        if token and _is_admin_token(token):
            return True
        if cmc_session and _verify_cookie(cmc_session) in ("admin", "ortodoncia"):
            return True
    except Exception:
        pass
    return False


def _ad_spend() -> float:
    try:
        d = json.loads(_CAC_SNAPSHOT.read_text(encoding="utf-8"))
        return sum(float(a.get("spend", 0) or 0) for a in d.get("ads", []))
    except Exception:
        return 0.0


# ── Captura ──────────────────────────────────────────────────────────────────
def _recent_checkin(rut: str | None) -> bool:
    """¿Este RUT tiene un check-in real reciente (<4h)? Prueba de paciente legítimo
    para que un atacante no pueda envenenar la fuente de RUTs arbitrarios sin auth."""
    r = re.sub(r"[^0-9kK]", "", (rut or "")).upper()
    if not r:
        return False
    try:
        from session import db as _conn
        with _conn() as c:
            row = c.execute(
                "SELECT 1 FROM checkin_cmc "
                "WHERE replace(replace(replace(upper(rut),'.',''),'-',''),' ','')=? "
                "  AND llegada_at >= datetime('now','-4 hours') LIMIT 1",
                (r,),
            ).fetchone()
        return bool(row)
    except Exception:
        return False


@router.post("/api/fuente")
async def set_fuente(request: Request,
                     token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None)):
    if not _rate_ok(f"src:{_client_ip(request)}", 20, 60):
        raise HTTPException(429, "Demasiadas solicitudes")
    body = await request.json()
    is_admin = _is_admin(token, cmc_session)
    rut = body.get("rut")
    # El SERVIDOR decide `via` según el contexto de auth — no se confía en el body.
    if is_admin:
        via = "checkin" if body.get("via") == "checkin" else "recepcion"
    else:
        # Escritura pública (autoservicio/check-in): exige prueba de un check-in
        # REAL reciente del mismo RUT → nadie puede envenenar fuentes a ciegas.
        via = "checkin"
        if not _recent_checkin(rut):
            raise HTTPException(403, "Sin check-in reciente")
    res = ps.set_source(
        rut=rut,
        fuente=body.get("fuente"),
        via=via,
        id_paciente=body.get("id_paciente") if is_admin else None,
        nombre=body.get("nombre"),
        detalle=body.get("detalle"),
        overwrite=bool(body.get("overwrite")) and is_admin,
    )
    if res.get("error"):
        raise HTTPException(400, res["error"])
    return res


@router.get("/api/fuente/{rut}")
async def get_fuente(rut: str):
    s = ps.get_source(rut)
    return {"conocida": bool(s), "fuente": (s or {}).get("fuente")}


# ── Reporte ──────────────────────────────────────────────────────────────────
@router.get("/api/canal/stats")
async def canal_stats(desde: str = Query(...), hasta: str = Query(...),
                      token: str | None = Query(None),
                      cmc_session: str | None = Cookie(None)):
    if not _is_admin(token, cmc_session):
        raise HTTPException(403, "Token inválido")
    st = ps.stats(desde, hasta)
    spend = _ad_spend()
    st["ad_spend"] = round(spend)
    st["cac_declarado_redes"] = round(spend / st["de_ad"]) if st["de_ad"] else None
    return JSONResponse(st)


@router.get("/alma/canal", response_class=HTMLResponse)
async def canal_page(token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None)):
    if not _is_admin(token, cmc_session):
        raise HTTPException(403, "Token inválido")
    botones = "".join(
        f'<button class="src" data-f="{k}">{lbl}</button>' for k, lbl in ps.FUENTES
    )
    # Reflejar SOLO un token admin VALIDADO, JSON-encoded (anti-XSS). Si la auth vino
    # por cookie, TK queda "" y los fetch usan la cookie (same-origin).
    try:
        from admin_routes import _is_admin_token
        eff = token if (token and _is_admin_token(token)) else ""
    except Exception:
        eff = ""
    return HTMLResponse(_REPORT_HTML.replace("{{BOTONES}}", botones)
                        .replace("{{TOKEN}}", json.dumps(eff)))


@router.get("/fuente", response_class=HTMLResponse)
async def fuente_selfservice():
    # Sin parámetros del URL → cero superficie de XSS reflejado. El paciente teclea su RUT.
    botones = "".join(
        f'<button class="src" data-f="{k}">{lbl}</button>' for k, lbl in ps.FUENTES
    )
    return HTMLResponse(_SELF_HTML.replace("{{BOTONES}}", botones))


# ── HTML (estilo Alma: navy/aqua, Montserrat) ────────────────────────────────
_BASE_CSS = """
*{margin:0;box-sizing:border-box;font-family:'Montserrat',system-ui,sans-serif}
body{background:linear-gradient(160deg,#0f2a47,#14365a);color:#eaf6fa;min-height:100vh;padding:24px}
.card{background:#fff;color:#0f2a47;border-radius:20px;padding:22px;max-width:560px;margin:0 auto 18px;box-shadow:0 20px 50px rgba(8,23,40,.4)}
h1{font-size:22px;margin-bottom:4px}.sub{color:#5b7a93;font-size:13px;margin-bottom:16px}
.src{display:block;width:100%;text-align:left;background:#f1f7fb;border:1.5px solid #d7e6f0;color:#0f2a47;
 font-weight:700;font-size:17px;padding:16px 18px;border-radius:14px;margin:8px 0;cursor:pointer}
.src:hover{background:#e3f0f7;border-color:#7BBDCC}
.src.sel{background:#7BBDCC;border-color:#4a9fb5;color:#0f2a47}
input{width:100%;padding:14px;border:1.5px solid #d7e6f0;border-radius:12px;font-size:17px;margin-bottom:10px}
.ok{background:#1f9d55;color:#fff;padding:16px;border-radius:14px;text-align:center;font-weight:700;font-size:18px}
.bar{height:22px;border-radius:6px;background:#7BBDCC}.barwrap{display:flex;align-items:center;gap:10px;margin:6px 0}
.barwrap .lbl{width:160px;font-size:13px;font-weight:700}.barwrap .n{font-weight:800;width:36px;text-align:right}
.kpi{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0}
.kpi div{flex:1;min-width:130px;background:#f1f7fb;border-radius:12px;padding:12px}
.kpi b{display:block;font-size:24px;color:#0f2a47}.kpi span{font-size:12px;color:#5b7a93}
"""

_SELF_HTML = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>¿Cómo nos conoció?</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@500;700;800&display=swap" rel="stylesheet">
<style>""" + _BASE_CSS + """</style></head><body>
<div class="card">
  <h1>¿Cómo nos conoció? 👋</h1>
  <div class="sub">Tu respuesta nos ayuda a mejorar. Es solo un toque.</div>
  <input id="rut" placeholder="Tu RUT (sin puntos ni guión)" value="">
  <div id="botones">{{BOTONES}}</div>
  <div id="done" style="display:none" class="ok">¡Gracias! 🙌</div>
</div>
<script>
let sel=null;
document.querySelectorAll('.src').forEach(b=>b.onclick=async()=>{
  const rut=document.getElementById('rut').value.trim();
  if(!rut){document.getElementById('rut').focus();return;}
  document.querySelectorAll('.src').forEach(x=>x.classList.remove('sel'));
  b.classList.add('sel');
  await fetch('/api/fuente',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({rut,fuente:b.dataset.f,via:'checkin'})});
  document.getElementById('botones').style.display='none';
  document.getElementById('done').style.display='block';
});
</script></body></html>"""

_REPORT_HTML = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Canal declarado</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@500;700;800&display=swap" rel="stylesheet">
<style>""" + _BASE_CSS + """</style></head><body>
<div class="card">
  <h1>Canal declarado</h1>
  <div class="sub">De dónde dicen los pacientes que nos conocieron (declarado, no inferido)</div>
  <div style="display:flex;gap:8px;margin-bottom:12px">
    <input type="date" id="d1" style="margin:0"><input type="date" id="d2" style="margin:0">
    <button class="src" style="width:auto;margin:0;padding:10px 16px" onclick="load()">Ver</button>
  </div>
  <div class="kpi" id="kpi"></div>
  <div id="barras"></div>
</div>
<div class="card">
  <h1>Registrar fuente de un paciente</h1>
  <div class="sub">Recepción: para los que llamaron o llegaron sin check-in</div>
  <input id="rut" placeholder="RUT del paciente">
  <div id="botones">{{BOTONES}}</div>
  <div id="done" style="display:none" class="ok">Guardado ✓</div>
</div>
<script>
const TK={{TOKEN}};
function isoAgo(d){const x=new Date();x.setDate(x.getDate()-d);return x.toISOString().slice(0,10);}
document.getElementById('d1').value=isoAgo(30);
document.getElementById('d2').value=isoAgo(0);
async function load(){
  const d1=document.getElementById('d1').value,d2=document.getElementById('d2').value;
  const r=await fetch(`/api/canal/stats?desde=${d1}&hasta=${d2}&token=${TK}`);
  const s=await r.json();
  const cac=s.cac_declarado_redes?('$'+s.cac_declarado_redes.toLocaleString('es-CL')):'—';
  document.getElementById('kpi').innerHTML=
    `<div><b>${s.total}</b><span>respuestas</span></div>
     <div><b>${s.pct_ad}%</b><span>de redes/ads (IG·FB·Google)</span></div>
     <div><b>${cac}</b><span>CAC declarado redes (spend/${s.de_ad})</span></div>`;
  const max=Math.max(1,...s.por_fuente.map(f=>f.n));
  document.getElementById('barras').innerHTML=s.por_fuente.map(f=>
    `<div class="barwrap"><div class="lbl">${f.label}</div>
     <div class="bar" style="width:${Math.round(120*f.n/max)+20}px;${f.es_ad?'':'background:#cdd9e0'}"></div>
     <div class="n">${f.n}</div></div>`).join('') || '<div class="sub">Sin datos en el rango.</div>';
}
let sel=null;
document.querySelectorAll('.src').forEach(b=>b.onclick=async()=>{
  const rut=document.getElementById('rut').value.trim();
  if(!rut){document.getElementById('rut').focus();return;}
  document.querySelectorAll('.src').forEach(x=>x.classList.remove('sel'));b.classList.add('sel');
  await fetch('/api/fuente?token='+TK,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({rut,fuente:b.dataset.f,via:'recepcion',overwrite:true})});
  document.getElementById('done').style.display='block';
  setTimeout(()=>{document.getElementById('done').style.display='none';
    document.getElementById('rut').value='';document.querySelectorAll('.src').forEach(x=>x.classList.remove('sel'));},1500);
});
load();
</script></body></html>"""
