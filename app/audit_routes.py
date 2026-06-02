"""Vista de auditoría en /admin — hallazgos del enjambre horario.

Lee la tabla `audit_findings` que escribe scripts/conversation_audit_swarm.py
(cron `0 * * * *`) y la muestra en una página simple bajo auth admin.

Router propio (patrón inventario_routes/kine_routes): se incluye en main.py.
No modifica datos del bot; solo lee findings y permite marcar resueltos.
"""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse

from admin_routes import require_admin
from session import _conn

router = APIRouter(tags=["audit"])

_SEV_COLOR = {"high": "#c0392b", "medium": "#d68910", "low": "#5d6d7e"}


def _fetch(limit: int = 200, status: str | None = "open") -> list[dict]:
    con = _conn()
    try:
        # La tabla la crea el script de auditoría; si aún no corrió, no existe.
        exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_findings'"
        ).fetchone()
        if not exists:
            return []
        q = ("SELECT id, run_ts, phone, severity, category, issue, evidence, "
             "fix_type, suggested_fix, target_hint, status FROM audit_findings")
        args: list = []
        if status and status != "all":
            q += " WHERE status = ?"
            args.append(status)
        q += " ORDER BY run_ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in con.execute(q, args).fetchall()]
    finally:
        con.close()


@router.get("/admin/api/audit-findings")
def audit_findings_api(_: str = Depends(require_admin),
                       limit: int = Query(200, le=1000),
                       status: str | None = Query("open")):
    return JSONResponse(_fetch(limit, status))


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
        <span class="cat">{cat}</span>
        <span class="phone">{phone}</span>
        <span class="run">{run}</span>
        <button class="resolve" onclick="resolver({fid}, this)">✓ Resuelto</button>
      </div>
      <div class="issue">{issue}</div>
      {f'<div class="ev">“{evidence}”</div>' if evidence else ''}
      <div class="fix"><b>Fix sugerido:</b> {fix}</div>
      {f'<div class="target"><code>{target}</code></div>' if target else ''}
    </div>"""


@router.get("/admin/auditoria", response_class=HTMLResponse)
def audit_page(_: str = Depends(require_admin)):
    rows = _fetch(300, "open")
    safe = [r for r in rows if r.get("fix_type") == "data_safe"]
    logic = [r for r in rows if r.get("fix_type") != "data_safe"]
    n_high = sum(1 for r in rows if (r.get("severity") or "").lower() == "high")

    def section(titulo, grupo, sub):
        cards = "".join(_card(f) for f in grupo) or '<p class="empty">Sin hallazgos abiertos.</p>'
        return f'<h2>{titulo} <span class="count">{len(grupo)}</span></h2><p class="sub">{sub}</p>{cards}'

    body = section(
        "Fixes seguros (estaged)", safe,
        "Datos de bajo riesgo. Revísalos y aplícalos tú — el enjambre NO los aplica solo.",
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
  .kpis {{ display:flex; gap:12px; padding:16px 22px; flex-wrap:wrap; }}
  .kpi {{ background:#fff; border-radius:14px; padding:12px 18px; box-shadow:0 2px 10px rgba(15,63,104,.08); }}
  .kpi b {{ font-size:22px; color:var(--navy); display:block; }}
  .kpi span {{ font-size:11px; color:#5d6d7e; text-transform:uppercase; letter-spacing:.08em; }}
  main {{ padding:6px 22px 60px; max-width:900px; }}
  h2 {{ font-size:15px; margin:26px 0 2px; color:var(--navy); }}
  h2 .count {{ background:var(--aqua); color:var(--navy); border-radius:20px; padding:1px 9px; font-size:12px; margin-left:6px; }}
  .sub {{ font-size:12px; color:#5d6d7e; margin:0 0 10px; }}
  .card {{ background:#fff; border-radius:14px; padding:14px 16px; margin:10px 0; box-shadow:0 2px 10px rgba(15,63,104,.06); border-left:4px solid var(--aqua); }}
  .row {{ display:flex; align-items:center; gap:9px; flex-wrap:wrap; margin-bottom:8px; }}
  .sev {{ color:#fff; font-size:10px; font-weight:700; padding:2px 8px; border-radius:6px; letter-spacing:.05em; }}
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
  <div><h1>Auditoría del chatbot</h1><div class="sub">CENTRO MÉDICO CARAMPANGUE · enjambre horario</div></div>
</header>
<div class="kpis">
  <div class="kpi"><b>{len(rows)}</b><span>abiertos</span></div>
  <div class="kpi"><b>{n_high}</b><span>severidad alta</span></div>
  <div class="kpi"><b>{len(safe)}</b><span>fixes seguros</span></div>
  <div class="kpi"><b>{len(logic)}</b><span>requieren revisión</span></div>
</div>
<main>{body}</main>
<script>
  const TOKEN = new URLSearchParams(location.search).get('token') || '';
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
