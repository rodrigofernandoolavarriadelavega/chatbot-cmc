"""Vista de auditoría en /admin — hallazgos de TODOS los checks.

Lee la tabla `audit_findings` (app/audit_store) que alimentan:
  • scripts/audit_runner.py            (precio, consent, agenda, leak, finanzas)
  • scripts/conversation_audit_swarm.py (conversacion)

Página /admin/auditoria bajo auth admin: agrupa fixes seguros vs revisión,
filtra por tipo de check, y permite marcar resuelto. No modifica datos del bot.
"""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse

from admin_routes import require_admin
from audit_store import fetch as _fetch, counts_by_check
from session import _conn

router = APIRouter(tags=["audit"])

_SEV_COLOR = {"high": "#c0392b", "medium": "#d68910", "low": "#5d6d7e"}
_CHECK_LABEL = {
    "conversacion": "Conversación", "precio": "Precio", "consent": "Consentimiento",
    "agenda": "Agenda", "leak": "Leak", "finanzas": "Finanzas",
}


@router.get("/admin/api/audit-findings")
def audit_findings_api(_: str = Depends(require_admin),
                       limit: int = Query(300, le=1000),
                       status: str | None = Query("open"),
                       check: str | None = Query(None)):
    return JSONResponse(_fetch(limit, status, check))


@router.post("/admin/api/audit-findings/{fid}/resolve")
def audit_resolve(fid: int, _: str = Depends(require_admin)):
    con = _conn()
    try:
        con.execute("UPDATE audit_findings SET status='resolved' WHERE id=?", (fid,))
        con.commit()
    finally:
        con.close()
    return {"ok": True}


def _card(f: dict) -> str:
    sev = (f.get("severity") or "low").lower()
    color = _SEV_COLOR.get(sev, "#5d6d7e")
    chk = f.get("check_name") or "conversacion"
    chk_label = _CHECK_LABEL.get(chk, chk)
    phone = html.escape(f.get("phone") or "")
    issue = html.escape(f.get("issue") or "")
    evidence = html.escape(f.get("evidence") or "")
    fix = html.escape(f.get("suggested_fix") or "")
    target = html.escape(f.get("target_hint") or "")
    cat = html.escape(f.get("category") or "")
    run = html.escape((f.get("run_ts") or "")[:16])
    fid = f.get("id")
    return f"""
    <div class="card" data-id="{fid}">
      <div class="row">
        <span class="sev" style="background:{color}">{sev.upper()}</span>
        <span class="chk">{html.escape(chk_label)}</span>
        <span class="cat">{cat}</span>
        {f'<span class="phone">{phone}</span>' if phone else ''}
        <span class="run">{run}</span>
        <button class="resolve" onclick="resolver({fid}, this)">✓ Resuelto</button>
      </div>
      <div class="issue">{issue}</div>
      {f'<div class="ev">“{evidence}”</div>' if evidence else ''}
      {f'<div class="fix"><b>Fix sugerido:</b> {fix}</div>' if fix else ''}
      {f'<div class="target"><code>{target}</code></div>' if target else ''}
    </div>"""


@router.get("/admin/auditoria", response_class=HTMLResponse)
def audit_page(_: str = Depends(require_admin), check: str | None = Query(None)):
    rows = _fetch(400, "open", check)
    safe = [r for r in rows if r.get("fix_type") == "data_safe"]
    logic = [r for r in rows if r.get("fix_type") != "data_safe"]
    n_high = sum(1 for r in rows if (r.get("severity") or "").lower() == "high")
    counts = counts_by_check("open")
    total_open = sum(counts.values())

    def chip(key, label, n):
        active = (check == key) or (key == "all" and not check)
        cls = "chip active" if active else "chip"
        href = "/admin/auditoria" if key == "all" else f"/admin/auditoria?check={key}"
        return f'<a class="{cls}" href="{href}{{TK}}">{label} <b>{n}</b></a>'

    chips = chip("all", "Todos", total_open) + "".join(
        chip(k, _CHECK_LABEL.get(k, k), counts.get(k, 0))
        for k in ["conversacion", "precio", "consent", "agenda", "leak", "finanzas"]
        if counts.get(k, 0) > 0
    )

    def section(titulo, grupo, sub):
        cards = "".join(_card(f) for f in grupo) or '<p class="empty">Sin hallazgos abiertos.</p>'
        return f'<h2>{titulo} <span class="count">{len(grupo)}</span></h2><p class="sub">{sub}</p>{cards}'

    body = section(
        "Fixes seguros (estaged)", safe,
        "Datos de bajo riesgo. Revísalos y aplícalos tú — el motor NO los aplica solo.",
    ) + section(
        "Requieren tu revisión", logic,
        "Tocan lógica o flujo. Necesitan criterio humano.",
    )

    return HTMLResponse(f"""<!doctype html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Auditoría · CMC</title>
<style>
  :root {{ --navy:#0F3F68; --aqua:#4FBECE; --blue:#1172AB; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:'Montserrat',system-ui,sans-serif; margin:0; background:#f4f7f9; color:#16323f; }}
  header {{ background:linear-gradient(120deg,var(--navy),var(--blue)); color:#fff; padding:18px 22px; display:flex; align-items:center; gap:14px; }}
  header .cross {{ width:34px;height:34px;border-radius:9px;background:var(--aqua);display:grid;place-items:center;font-weight:800;color:var(--navy); }}
  header h1 {{ font-size:17px; margin:0; line-height:1.1; }}
  header .sub {{ font-size:11px; opacity:.85; letter-spacing:.12em; }}
  .kpis {{ display:flex; gap:12px; padding:16px 22px 4px; flex-wrap:wrap; }}
  .kpi {{ background:#fff; border-radius:14px; padding:12px 18px; box-shadow:0 2px 10px rgba(15,63,104,.08); }}
  .kpi b {{ font-size:22px; color:var(--navy); display:block; }}
  .kpi span {{ font-size:11px; color:#5d6d7e; text-transform:uppercase; letter-spacing:.08em; }}
  .chips {{ display:flex; gap:8px; padding:10px 22px; flex-wrap:wrap; }}
  .chip {{ text-decoration:none; font-size:12px; color:#16323f; background:#fff; border:1px solid #e0e6ea; border-radius:20px; padding:5px 12px; }}
  .chip b {{ color:var(--blue); }}
  .chip.active {{ background:var(--navy); color:#fff; border-color:var(--navy); }}
  .chip.active b {{ color:var(--aqua); }}
  main {{ padding:6px 22px 60px; max-width:900px; }}
  h2 {{ font-size:15px; margin:26px 0 2px; color:var(--navy); }}
  h2 .count {{ background:var(--aqua); color:var(--navy); border-radius:20px; padding:1px 9px; font-size:12px; margin-left:6px; }}
  .sub {{ font-size:12px; color:#5d6d7e; margin:0 0 10px; }}
  .card {{ background:#fff; border-radius:14px; padding:14px 16px; margin:10px 0; box-shadow:0 2px 10px rgba(15,63,104,.06); border-left:4px solid var(--aqua); }}
  .row {{ display:flex; align-items:center; gap:9px; flex-wrap:wrap; margin-bottom:8px; }}
  .sev {{ color:#fff; font-size:10px; font-weight:700; padding:2px 8px; border-radius:6px; letter-spacing:.05em; }}
  .chk {{ font-size:10px; font-weight:700; background:var(--navy); color:#fff; padding:2px 8px; border-radius:6px; letter-spacing:.04em; }}
  .cat {{ font-size:11px; background:#eef3f6; color:#16323f; padding:2px 8px; border-radius:6px; }}
  .phone {{ font-size:12px; color:var(--blue); font-weight:600; }}
  .run {{ font-size:11px; color:#9aa7b0; margin-left:auto; }}
  .resolve {{ font-size:11px; border:1px solid #d4dde2; background:#fff; color:#16323f; border-radius:7px; padding:3px 9px; cursor:pointer; }}
  .resolve:hover {{ background:#eafaf1; border-color:#2ecc71; color:#1e8449; }}
  .issue {{ font-size:14px; line-height:1.4; margin-bottom:6px; }}
  .ev {{ font-size:12.5px; color:#5d6d7e; font-style:italic; border-left:2px solid #e0e6ea; padding-left:9px; margin-bottom:6px; }}
  .fix {{ font-size:13px; line-height:1.4; }}
  .target {{ margin-top:5px; }}
  .target code {{ font-size:11.5px; background:#f0f3f5; padding:2px 6px; border-radius:5px; color:#34495e; }}
  .empty {{ color:#9aa7b0; font-size:13px; }}
</style></head><body>
<header>
  <div class="cross">✚</div>
  <div><h1>Auditoría del chatbot</h1><div class="sub">CENTRO MÉDICO CARAMPANGUE · motor de auditoría</div></div>
</header>
<div class="kpis">
  <div class="kpi"><b>{len(rows)}</b><span>{'en filtro' if check else 'abiertos'}</span></div>
  <div class="kpi"><b>{n_high}</b><span>severidad alta</span></div>
  <div class="kpi"><b>{len(safe)}</b><span>fixes seguros</span></div>
  <div class="kpi"><b>{len(logic)}</b><span>requieren revisión</span></div>
</div>
<div class="chips">{chips}</div>
<main>{body}</main>
<script>
  const TOKEN = new URLSearchParams(location.search).get('token') || '';
  // Propagar el token en los chips de filtro
  document.querySelectorAll('a.chip').forEach(a => {{
    a.href = a.href.replace('{{TK}}', TOKEN ? (a.href.includes('?') ? '&token=' : '?token=') + encodeURIComponent(TOKEN) : '');
  }});
  async function resolver(id, btn) {{
    btn.disabled = true; btn.textContent = '…';
    try {{
      const r = await fetch(`/admin/api/audit-findings/${{id}}/resolve?token=${{encodeURIComponent(TOKEN)}}`, {{method:'POST'}});
      if (!r.ok) throw new Error(r.status);
      const card = btn.closest('.card'); card.style.opacity = .35; card.querySelector('.resolve').textContent = '✓ resuelto';
    }} catch(e) {{ btn.disabled = false; btn.textContent = '✓ Resuelto'; alert('Error: ' + e); }}
  }}
</script>
</body></html>""")
