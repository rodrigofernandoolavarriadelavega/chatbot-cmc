"""Rutas del dashboard del autopilot. APIRouter para incluir en main.py con 1 línea.

- GET    /autopilot                 → dashboard HTML (estética panel v2, embebible en Alma)
- GET    /autopilot/api/snapshot    → JSON del último run (lee snapshot, no golpea Meta)
- POST   /autopilot/api/refresh     → fuerza un dry-run nuevo (read-only sobre Meta)
- GET    /autopilot/api/designs     → galería de diseños Canva (visor embebido)
- POST   /autopilot/api/designs     → agrega un diseño a la galería
- DELETE /autopilot/api/designs/{id}→ elimina un diseño

Auth: mismo token admin que el resto del panel (?token= o cookie).
"""
import asyncio
import base64
import json
import logging
import re
import time
import unicodedata
import uuid
from pathlib import Path

from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from config import ADMIN_TOKEN, OLACORE_TOKEN, ALMA_PROFILES

_ADMIN_TOKENS_AP: tuple[str, ...] = (ADMIN_TOKEN, OLACORE_TOKEN)
from .world_state import load_snapshot
from .designs import load_designs, add_design, delete_design, set_status, VALID_STATUS
from .ad_formats import AD_FORMATS, FORMAT_BY_KEY, channels
from .image_gen import build_prompt, generate_png, gpt_size_for, OPENAI_IMAGE_MODEL
from . import publishing

# Jobs de generación en memoria. La generación (gpt-image-2) tarda 60-120s — más
# que cualquier timeout de proxy razonable, así que corre en background y el
# dashboard consulta el estado por job_id en vez de bloquear la conexión HTTP.
_GEN_JOBS: dict[str, dict] = {}

_STATIC_DESIGNS = Path(__file__).parent.parent.parent / "static" / "ad_designs"

# Snapshot de atribución/CAC (generado por el cron _job_cac_snapshot vía cac_report.py).
_CAC_SNAPSHOT = Path(__file__).parent.parent.parent / "data" / "cac_snapshot.json"
_CAC_SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "cac_report.py"


def _slug(text: str) -> str:
    """Slug ascii para nombre de archivo/id."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:40] or "diseno"

log = logging.getLogger("bot")
router = APIRouter()

_TEMPLATE = Path(__file__).parent.parent.parent / "templates" / "autopilot_dashboard.html"


def _check_token(token: str | None, request: Request | None) -> None:
    """Valida el token admin (query o cookie). Coherente con el resto del panel."""
    if token and token in _ADMIN_TOKENS_AP:
        return
    cookie = request.cookies.get("admin_token") if request else None
    if cookie and cookie in _ADMIN_TOKENS_AP:
        return
    raise HTTPException(status_code=403, detail="Token inválido")


@router.get("/autopilot", response_class=HTMLResponse)
def autopilot_dashboard(token: str | None = Query(None), request: Request = None):
    _check_token(token, request)
    if not _TEMPLATE.exists():
        raise HTTPException(404, "Dashboard no disponible")
    # Secciones (tabs) visibles según el perfil del token. El perfil resuelve
    # `secciones["autopilot"]` = lista de tabs permitidos; None/ausente = todos.
    eff_token = token if (token in _ADMIN_TOKENS_AP) else (request.cookies.get("admin_token") if request else None)
    secciones = (ALMA_PROFILES.get(eff_token, {}) or {}).get("secciones", {}) or {}
    allowed = secciones.get("autopilot")  # lista o None (acceso total)
    html = _TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("__ALLOWED_SECTIONS__", json.dumps(allowed) if allowed is not None else "null")
    return HTMLResponse(html)


@router.get("/autopilot/api/snapshot")
def autopilot_snapshot(token: str | None = Query(None), request: Request = None):
    _check_token(token, request)
    snap = load_snapshot()
    if snap is None:
        return JSONResponse({"empty": True, "message": "Aún no hay corridas del autopilot."})
    return JSONResponse(snap)


@router.post("/autopilot/api/refresh")
async def autopilot_refresh(window: int = Query(7), token: str | None = Query(None),
                            request: Request = None):
    """Fuerza un dry-run nuevo. Read-only sobre Meta (solo lee insights)."""
    _check_token(token, request)
    try:
        from .engine import run_dry_run
        run = await run_dry_run(window_days=window)
        return JSONResponse({"ok": True, "n_actions": len(run.actions)})
    except Exception as e:  # noqa: BLE001
        log.error("autopilot refresh falló: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Atribución / CAC (pestaña embebida) ─────────────────────────────────────

@router.get("/autopilot/api/atribucion")
def autopilot_atribucion(token: str | None = Query(None), request: Request = None):
    """Snapshot de atribución/CAC cacheado (cruce ad → cita → pago real).

    Lo genera el cron diario (`_job_cac_snapshot`) porque calcularlo golpea la
    Meta Marketing API (~60s). Acá solo se lee el JSON — instantáneo.
    """
    _check_token(token, request)
    if not _CAC_SNAPSHOT.exists():
        return JSONResponse({"empty": True,
                             "message": "Aún no hay snapshot de CAC. Genéralo con el botón Actualizar."})
    try:
        return JSONResponse(json.loads(_CAC_SNAPSHOT.read_text(encoding="utf-8")))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"empty": True, "message": f"Snapshot inválido: {e}"})


@router.post("/autopilot/api/atribucion/refresh")
async def autopilot_atribucion_refresh(token: str | None = Query(None), request: Request = None):
    """Regenera el snapshot CAC en background (tarda ~60s por la Meta API).
    Devuelve de inmediato; el dashboard recarga el GET en ~1 min."""
    _check_token(token, request)

    async def _run():
        try:
            import sys as _sys
            root = _CAC_SCRIPT.parent.parent
            proc = await asyncio.create_subprocess_exec(
                _sys.executable, str(_CAC_SCRIPT), "--mode", "rolling",
                "--json", str(_CAC_SNAPSHOT), cwd=str(root),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=240)
        except Exception as e:  # noqa: BLE001
            log.warning("atribucion refresh background falló: %s", e)

    asyncio.create_task(_run())
    return JSONResponse({"ok": True, "message": "Generando snapshot… recarga en ~1 min."})


# ── Galería de diseños (Canva) ──────────────────────────────────────────────

@router.get("/autopilot/api/designs")
def autopilot_designs(token: str | None = Query(None), request: Request = None):
    """Lista los diseños de la galería (más reciente primero)."""
    _check_token(token, request)
    return JSONResponse({"designs": load_designs()})


@router.post("/autopilot/api/designs")
async def autopilot_designs_add(request: Request, token: str | None = Query(None)):
    """Agrega/actualiza un diseño. Body = registro (ver designs.py)."""
    _check_token(token, request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    if not isinstance(body, dict) or not body.get("title"):
        raise HTTPException(400, "Falta 'title'")
    return JSONResponse({"ok": True, "design": add_design(body)})


@router.delete("/autopilot/api/designs/{rid}")
def autopilot_designs_del(rid: str, token: str | None = Query(None), request: Request = None):
    """Elimina un diseño por id."""
    _check_token(token, request)
    return JSONResponse({"ok": delete_design(rid)})


@router.post("/autopilot/api/designs/{rid}/status")
async def autopilot_designs_status(rid: str, request: Request, token: str | None = Query(None)):
    """Cambia el estado de un diseño (borrador → aprobado → publicado)."""
    _check_token(token, request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    status = (body.get("status") or "").strip().lower()
    if status not in VALID_STATUS:
        raise HTTPException(400, f"Estado inválido (usa: {', '.join(VALID_STATUS)})")
    return JSONResponse({"ok": set_status(rid, status), "status": status})


@router.get("/autopilot/api/formats")
def autopilot_formats(token: str | None = Query(None), request: Request = None):
    """Catálogo de formatos publicitarios disponibles (para el dashboard y el asistente)."""
    _check_token(token, request)
    return JSONResponse({"channels": channels(), "formats": AD_FORMATS})


async def _run_generation(job_id: str, *, fmt: dict, title: str, subtitle: str,
                          specialty: str, brief: str, cta: str, quality: str) -> None:
    """Corre la generación en background y deja el resultado en _GEN_JOBS[job_id]."""
    try:
        prompt = build_prompt(fmt, title=title, subtitle=subtitle, specialty=specialty,
                              brief=brief, cta=cta)
        png = await generate_png(prompt, size=gpt_size_for(fmt), quality=quality)
        rid = f"{_slug(specialty or title)}-{int(time.time())}"
        _STATIC_DESIGNS.mkdir(parents=True, exist_ok=True)
        (_STATIC_DESIGNS / f"{rid}.png").write_bytes(png)
        rec = add_design({
            "id": rid, "title": title, "specialty": specialty, "format": fmt["key"],
            "image_url": f"/static/ad_designs/{rid}.png", "status": "borrador",
            "source": OPENAI_IMAGE_MODEL,
        })
        _GEN_JOBS[job_id] = {"status": "done", "design": rec}
    except Exception as e:  # noqa: BLE001
        log.error("generación falló (%s): %s", job_id, e)
        _GEN_JOBS[job_id] = {"status": "error", "error": str(e)}


@router.post("/autopilot/api/designs/generate")
async def autopilot_designs_generate(request: Request, token: str | None = Query(None)):
    """Inicia la generación de una pieza (async). Devuelve un job_id para consultar.

    Body: {title, subtitle?, specialty?, format?, brief?, cta?, quality?}
    """
    _check_token(token, request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Falta 'title'")
    fmt = FORMAT_BY_KEY.get(body.get("format") or "instagram_post")
    if not fmt:
        raise HTTPException(400, f"Formato desconocido: {body.get('format')}")

    job_id = uuid.uuid4().hex[:12]
    _GEN_JOBS[job_id] = {"status": "running"}
    asyncio.create_task(_run_generation(
        job_id, fmt=fmt, title=title,
        subtitle=(body.get("subtitle") or "").strip(),
        specialty=(body.get("specialty") or "").strip(),
        brief=(body.get("brief") or "").strip(),
        cta=(body.get("cta") or "").strip(),
        quality=body.get("quality") or "high",
    ))
    return JSONResponse({"ok": True, "job_id": job_id, "status": "running"})


@router.get("/autopilot/api/designs/generate/status/{job_id}")
def autopilot_designs_generate_status(job_id: str, token: str | None = Query(None),
                                      request: Request = None):
    """Estado de un job de generación: running | done (+design) | error (+error)."""
    _check_token(token, request)
    return JSONResponse(_GEN_JOBS.get(job_id, {"status": "unknown"}))


@router.post("/autopilot/api/designs/save-edit")
async def autopilot_designs_save_edit(request: Request, token: str | None = Query(None)):
    """Guarda una imagen editada (texto sobrepuesto + tamaño/formato) como diseño nuevo.

    Body: {image_b64 (dataURL o base64 PNG), title?, specialty?, format?}
    Devuelve el registro creado para insertarlo en la galería sin recargar.
    """
    _check_token(token, request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    raw = (body.get("image_b64") or "").strip()
    if not raw:
        raise HTTPException(400, "Falta 'image_b64'")
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        png = base64.b64decode(raw)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "image_b64 inválido")
    if len(png) > 12 * 1024 * 1024:
        raise HTTPException(413, "Imagen demasiado grande")
    title = (body.get("title") or "Diseño editado").strip()
    specialty = (body.get("specialty") or "").strip()
    fmt = (body.get("format") or "instagram_post").strip()
    rid = f"{_slug(specialty or title)}-edit-{int(time.time())}"
    _STATIC_DESIGNS.mkdir(parents=True, exist_ok=True)
    (_STATIC_DESIGNS / f"{rid}.png").write_bytes(png)
    rec = add_design({
        "id": rid, "title": title, "specialty": specialty, "format": fmt,
        "image_url": f"/static/ad_designs/{rid}.png", "status": "borrador",
        "source": "editor",
    })
    return JSONResponse({"ok": True, "design": rec})


@router.post("/autopilot/api/designs/cutout")
async def autopilot_designs_cutout(request: Request, token: str | None = Query(None)):
    """Recorte automático con IA (quitar fondo) vía remove.bg.

    Requiere REMOVEBG_API_KEY en el entorno. Si no está, devuelve ok=false /
    reason=no_key (HTTP 200) para que el editor caiga al modo manual sin error.
    Body: {image_b64 (dataURL o base64 PNG)}.
    """
    _check_token(token, request)
    import os
    key = os.getenv("REMOVEBG_API_KEY", "").strip()
    if not key:
        return JSONResponse({"ok": False, "reason": "no_key",
                             "message": "Falta REMOVEBG_API_KEY en el .env del servidor."})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    raw = (body.get("image_b64") or "").strip()
    if not raw:
        raise HTTPException(400, "Falta 'image_b64'")
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        png = base64.b64decode(raw)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "image_b64 inválido")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                "https://api.remove.bg/v1.0/removebg",
                headers={"X-Api-Key": key},
                data={"size": "auto", "format": "png"},
                files={"image_file": ("image.png", png, "image/png")},
            )
        if r.status_code != 200:
            return JSONResponse({"ok": False, "reason": "api_error",
                                 "message": f"remove.bg {r.status_code}: {r.text[:200]}"})
        out = base64.b64encode(r.content).decode()
        return JSONResponse({"ok": True, "image_b64": "data:image/png;base64," + out})
    except Exception as e:  # noqa: BLE001
        log.warning("cutout remove.bg falló: %s", e)
        return JSONResponse({"ok": False, "reason": "exception", "message": str(e)})


# ── Email marketing (segmentos) ──────────────────────────────────────────────

@router.get("/autopilot/api/email/segments")
def email_segments_list(token: str | None = Query(None), request: Request = None):
    """Segmentos guardados + plantillas best-practice + estado del canal de envío."""
    _check_token(token, request)
    from .email_segments import load_segments, SEGMENT_TEMPLATES
    from .email_render import sending_status
    return JSONResponse({
        "segments": load_segments(),
        "templates": SEGMENT_TEMPLATES,
        "sending": sending_status(),
    })


@router.get("/autopilot/api/email/options")
def email_options(token: str | None = Query(None), request: Request = None):
    """Opciones para el constructor: lifecycles + especialidades disponibles en BI."""
    _check_token(token, request)
    from .email_segments import LIFECYCLE_BANDS, specialty_options
    return JSONResponse({
        "lifecycles": [{"key": k, "label": lbl} for k, lbl, _ in LIFECYCLE_BANDS],
        "especialidades": specialty_options(),
    })


@router.post("/autopilot/api/email/segments")
async def email_segments_save(request: Request, token: str | None = Query(None)):
    """Crea/actualiza un segmento. Body = registro de segmento (ver email_segments.py)."""
    _check_token(token, request)
    from .email_segments import add_segment
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    if not isinstance(body, dict) or not (body.get("name") or "").strip():
        raise HTTPException(400, "Falta 'name'")
    return JSONResponse({"ok": True, "segment": add_segment(body)})


@router.delete("/autopilot/api/email/segments/{rid}")
def email_segments_del(rid: str, token: str | None = Query(None), request: Request = None):
    """Elimina un segmento por id."""
    _check_token(token, request)
    from .email_segments import delete_segment
    return JSONResponse({"ok": delete_segment(rid)})


@router.post("/autopilot/api/email/preview")
async def email_preview(request: Request, token: str | None = Query(None)):
    """Dado un segmento (no necesita estar guardado), devuelve:
    audiencia por capas, score best-practice, asunto renderizado y HTML de preview.
    """
    _check_token(token, request)
    from .email_segments import resolve_audience, score_segment
    from .email_render import render_email, render_subject
    try:
        seg = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    if not isinstance(seg, dict):
        raise HTTPException(400, "Segmento inválido")
    audience = resolve_audience(seg)
    score = score_segment(seg, audience)
    return JSONResponse({
        "audience": audience,
        "score": score,
        "subject_preview": render_subject(seg),
        "html_preview": render_email(seg, preview=True),
    })


# ── Doble opt-in / baja / tracking de email (endpoints PÚBLICOS, sin token) ────
# Los abre el paciente desde su correo: confirmación (paso 2 del doble opt-in),
# baja one-click (List-Unsubscribe), pixel de apertura y redirect de clic.

_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _consent_page(title: str, msg: str, ok: bool = True) -> HTMLResponse:
    color = "#1172AB" if ok else "#b42318"
    icon = "✓" if ok else "•"
    return HTMLResponse(f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title></head>
<body style="margin:0;font-family:Arial,Helvetica,sans-serif;background:#f4f7fa;color:#13202e">
<div style="max-width:480px;margin:8vh auto;background:#fff;border-radius:16px;padding:38px 30px;
            box-shadow:0 4px 18px rgba(15,63,104,.08);text-align:center">
  <div style="width:54px;height:54px;border-radius:50%;background:{color};color:#fff;font-size:28px;
              line-height:54px;margin:0 auto 18px">{icon}</div>
  <h1 style="margin:0 0 10px;font-size:21px;color:#0F3F68">{title}</h1>
  <p style="margin:0;font-size:15px;line-height:1.6;color:#64798c">{msg}</p>
  <p style="margin:22px 0 0;font-size:13px"><a href="https://centromedicocarampangue.cl"
     style="color:#1172AB">centromedicocarampangue.cl</a></p>
</div></body></html>""")


@router.get("/email/confirmar", response_class=HTMLResponse)
def email_confirmar(t: str | None = Query(None)):
    """Paso 2 del doble opt-in: el paciente confirma su correo (Ley 21.719)."""
    from . import email_optin
    rec = email_optin.confirm_email_optin(t or "")
    if not rec:
        return _consent_page("Enlace no válido",
                             "Este enlace expiró o ya no es válido. Si quieres recibir "
                             "nuestros correos, vuelve a pedirlo por WhatsApp.", ok=False)
    return _consent_page("¡Suscripción confirmada!",
                         "Listo, vas a recibir recordatorios y novedades del Centro Médico "
                         "Carampangue. Puedes darte de baja cuando quieras desde cualquier correo.")


@router.get("/email/baja", response_class=HTMLResponse)
def email_baja(t: str | None = Query(None), p: str | None = Query(None)):
    """Baja one-click del canal email (List-Unsubscribe). Acepta token o teléfono."""
    from . import email_optin
    email_optin.revoke_email_optin(token=t, phone=p)
    return _consent_page("Te diste de baja",
                         "No volverás a recibir correos de marketing del Centro Médico "
                         "Carampangue. Esto no afecta los recordatorios de tus citas.")


@router.post("/email/baja", response_class=HTMLResponse)
def email_baja_post(t: str | None = Query(None), p: str | None = Query(None)):
    """List-Unsubscribe-Post=One-Click: los clientes de correo hacen POST."""
    return email_baja(t=t, p=p)


@router.get("/e/o/{token}.png")
def email_open_pixel(token: str):
    """Pixel de apertura 1x1. Devuelve PNG transparente siempre (no filtra errores)."""
    try:
        from . import email_tracking
        email_tracking.record_open(token)
    except Exception:  # noqa: BLE001
        pass
    return Response(content=_PIXEL_PNG, media_type="image/png",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@router.get("/e/c/{token}")
def email_click(token: str):
    """Redirect de clic: registra el clic y manda a wa.me con marcador de atribución."""
    from . import email_tracking
    rec = email_tracking.record_click(token)
    dest = (rec or {}).get("cta_url") or f"https://wa.me/{email_tracking.WA_NUMBER}"
    return RedirectResponse(dest, status_code=302)


# ── Email — opt-in stats, audiencias unificadas y envío (admin) ───────────────

@router.get("/autopilot/api/email/optin")
def email_optin_stats_ep(token: str | None = Query(None), request: Request = None):
    """Embudo de doble opt-in + métricas de campaña (aperturas/clics)."""
    _check_token(token, request)
    from . import email_optin, email_tracking
    return JSONResponse({"optin": email_optin.optin_stats(),
                         "campaign": email_tracking.campaign_stats()})


@router.post("/autopilot/api/audiences/overview")
async def audiences_overview_ep(request: Request, token: str | None = Query(None)):
    """Alcance de una audiencia (criterios RFM) lado a lado por canal WhatsApp/Email/Meta."""
    _check_token(token, request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    from . import audiences
    crit = (body or {}).get("criteria") or {}
    return JSONResponse(audiences.audience_overview(crit))


@router.post("/autopilot/api/email/send")
async def email_send_ep(request: Request, token: str | None = Query(None)):
    """Envía (o simula) un segmento. dry_run=true por defecto: NO envía, solo cuenta.
    El envío real exige además EMAIL_SENDING_ENABLED en el server (doble gate)."""
    _check_token(token, request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    seg = (body or {}).get("segment") or {}
    if not isinstance(seg, dict) or not seg.get("criteria"):
        raise HTTPException(400, "Falta 'segment.criteria'")
    dry_run = bool((body or {}).get("dry_run", True))
    limit = int((body or {}).get("limit", 200))
    from . import email_tracking
    summary = await email_tracking.send_segment(seg, limit=limit, dry_run=dry_run)
    return JSONResponse(summary)


# ── Optimizador de Políticas (nivel 5) — bandeja SOLO-PROPUESTA, read-only ─────

@router.get("/autopilot/api/optimizer/proposals")
def optimizer_proposals(token: str | None = Query(None), request: Request = None):
    """Bandeja de revisión de políticas: cruza outcomes reales (margen con honorario,
    conversión de win-back) vs las reglas vigentes y PROPONE cambios con evidencia.
    READ-ONLY: no aplica nada. La aplicación (champion/challenger) es fase posterior."""
    _check_token(token, request)
    from . import optimizer
    return JSONResponse(optimizer.run_analysis())


@router.get("/autopilot/api/experiments")
def experiments_ledger(token: str | None = Query(None), request: Request = None):
    """Champion/Challenger: ledger de experimentos de política + veredictos con
    estadística honesta sobre data real. READ-ONLY: evalúa y recomienda; promover el
    ganador a la política viva es una acción humana, gateada."""
    _check_token(token, request)
    from . import experiments
    return JSONResponse(experiments.run_evaluations())


@router.get("/autopilot/api/impact")
def impact_pnl(days: int = Query(90), token: str | None = Query(None), request: Request = None):
    """P&L agéntico: qué hizo la capa autónoma (win-back/email/ads) y cuánto rindió,
    por canal + totales. READ-ONLY. Cifras = piso atribuible (no contabilidad oficial)."""
    _check_token(token, request)
    from . import impact
    return JSONResponse(impact.pnl(days=max(1, min(int(days), 365))))


# ── Publicación orgánica (segmento Instagram · Facebook · WhatsApp) ───────────

@router.get("/autopilot/api/publish/status")
def publish_status(token: str | None = Query(None), request: Request = None):
    """Estado de conexión por canal + kill-switch. Lo consume la consola para
    decidir 'conectado' vs 'conecta tu cuenta', sin exponer tokens."""
    _check_token(token, request)
    return JSONResponse(publishing.connection_status())


@router.get("/autopilot/api/publish/queue")
def publish_queue(token: str | None = Query(None), request: Request = None):
    """Cola de publicación completa (más reciente primero)."""
    _check_token(token, request)
    return JSONResponse({"items": publishing.load_queue()})


@router.post("/autopilot/api/publish/queue")
async def publish_enqueue(request: Request, token: str | None = Query(None)):
    """Agrega una pieza a la cola. Body: {design_id?, title, image_url, caption?,
    channels:[...], scheduled_at?}. Entra en estado 'cola' (pendiente de aprobar)."""
    _check_token(token, request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    if not isinstance(body, dict) or not body.get("image_url"):
        raise HTTPException(400, "Falta 'image_url'")
    if not body.get("channels"):
        raise HTTPException(400, "Selecciona al menos un canal")
    return JSONResponse({"ok": True, "item": publishing.enqueue(body)})


@router.post("/autopilot/api/publish/queue/{rid}/approve")
async def publish_approve(rid: str, request: Request, token: str | None = Query(None)):
    """Aprueba una pieza → el scheduler la publica a su hora (o ASAP si no hay)."""
    _check_token(token, request)
    scheduled_at = None
    try:
        body = await request.json()
        scheduled_at = (body or {}).get("scheduled_at")
    except Exception:  # noqa: BLE001 — body opcional
        pass
    item = publishing.approve(rid, scheduled_at)
    if not item:
        raise HTTPException(404, "Pieza no encontrada en la cola")
    return JSONResponse({"ok": True, "item": item})


@router.post("/autopilot/api/publish/queue/{rid}/schedule")
async def publish_reschedule(rid: str, request: Request, token: str | None = Query(None)):
    """Cambia/limpia la hora programada de una pieza sin alterar su estado.
    Body: {scheduled_at} (ISO) o {scheduled_at:null} para volver a ASAP."""
    _check_token(token, request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    item = publishing.reschedule(rid, (body or {}).get("scheduled_at"))
    if not item:
        raise HTTPException(404, "Pieza no encontrada en la cola")
    return JSONResponse({"ok": True, "item": item})


@router.post("/autopilot/api/publish/queue/{rid}/publish-now")
async def publish_now(rid: str, token: str | None = Query(None), request: Request = None):
    """Publica una pieza inmediatamente (sigue gateada por ORGANIC_PUBLISH_EXECUTE)."""
    _check_token(token, request)
    item = await publishing.publish_item(rid)
    if not item:
        raise HTTPException(404, "Pieza no encontrada en la cola")
    return JSONResponse({"ok": True, "item": item})


@router.delete("/autopilot/api/publish/queue/{rid}")
def publish_delete(rid: str, token: str | None = Query(None), request: Request = None):
    """Saca una pieza de la cola."""
    _check_token(token, request)
    return JSONResponse({"ok": publishing.delete(rid)})


# ── SEO (auditoría on-page + técnica + local) ────────────────────────────────

# Jobs de auditoría en memoria: el fetch en vivo de ~15 URLs tarda más que un
# timeout de proxy razonable, así que corre en background (igual que la
# generación de diseños) y el dashboard consulta por job_id.
_SEO_JOBS: dict[str, dict] = {}


@router.get("/autopilot/api/seo/snapshot")
def seo_snapshot(token: str | None = Query(None), request: Request = None):
    """Último snapshot de auditoría SEO (no golpea el sitio). Si no hay corrida,
    devuelve igual la coverage matrix + checklist local + oportunidades para que
    el dashboard tenga algo útil que mostrar."""
    _check_token(token, request)
    from . import seo_audit
    snap = seo_audit.load_snapshot()
    if snap is None:
        return JSONResponse({
            "empty": True,
            "message": "Aún no hay auditorías. Pulsa Auditar para correr la primera.",
            "coverage": seo_audit.coverage_matrix(),
            "local_checklist": seo_audit.local_seo_checklist(),
            "opportunities": seo_audit.OPPORTUNITY_TEMPLATES,
        })
    # coverage/checklist/oportunidades son cálculos puros (baratos): recalcular
    # fresco para que un cambio de pesos se refleje sin re-auditar el sitio.
    # Solo las páginas (requieren fetch de red) quedan cacheadas en el snapshot.
    snap["coverage"] = seo_audit.coverage_matrix()
    snap["local_checklist"] = seo_audit.local_seo_checklist()
    snap["opportunities"] = seo_audit.OPPORTUNITY_TEMPLATES
    return JSONResponse(snap)


async def _run_seo_audit(job_id: str) -> None:
    """Corre la auditoría en background y deja el snapshot en _SEO_JOBS[job_id]."""
    try:
        from . import seo_audit
        snap = await seo_audit.fetch_and_audit()
        _SEO_JOBS[job_id] = {"status": "done", "kpis": snap.get("kpis", {})}
    except Exception as e:  # noqa: BLE001
        log.error("seo audit falló (%s): %s", job_id, e)
        _SEO_JOBS[job_id] = {"status": "error", "error": str(e)}


@router.post("/autopilot/api/seo/audit")
async def seo_audit_run(token: str | None = Query(None), request: Request = None):
    """Inicia una auditoría en vivo (async, read-only sobre el sitio). Devuelve job_id."""
    _check_token(token, request)
    job_id = uuid.uuid4().hex[:12]
    _SEO_JOBS[job_id] = {"status": "running"}
    asyncio.create_task(_run_seo_audit(job_id))
    return JSONResponse({"ok": True, "job_id": job_id, "status": "running"})


@router.get("/autopilot/api/seo/audit/status/{job_id}")
def seo_audit_status(job_id: str, token: str | None = Query(None), request: Request = None):
    """Estado de una auditoría: running | done (+kpis) | error (+error)."""
    _check_token(token, request)
    return JSONResponse(_SEO_JOBS.get(job_id, {"status": "unknown"}))


@router.get("/autopilot/api/seo/targets")
def seo_targets(token: str | None = Query(None), request: Request = None):
    """URLs default (siempre auditadas) + targets extra del usuario."""
    _check_token(token, request)
    from . import seo_audit
    return JSONResponse({"default": seo_audit.default_targets(),
                         "custom": seo_audit.load_targets()})


@router.post("/autopilot/api/seo/targets")
async def seo_targets_add(request: Request, token: str | None = Query(None)):
    """Agrega una URL a auditar. Body: {url, label?, role?}."""
    _check_token(token, request)
    from . import seo_audit
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    if not isinstance(body, dict) or not (body.get("url") or "").strip():
        raise HTTPException(400, "Falta 'url'")
    return JSONResponse({"ok": True, "target": seo_audit.add_target(body)})


@router.delete("/autopilot/api/seo/targets")
def seo_targets_del(url: str = Query(...), token: str | None = Query(None),
                    request: Request = None):
    """Elimina un target de usuario por URL."""
    _check_token(token, request)
    from . import seo_audit
    return JSONResponse({"ok": seo_audit.delete_target(url)})


# ── Fase 2: decisiones de presupuesto pendientes de aprobación ───────────────

@router.get("/autopilot/api/pending")
def pending_list(token: str | None = Query(None), request: Request = None):
    """Acciones de presupuesto esperando OK del dueño + historial reciente."""
    _check_token(token, request)
    from . import approvals
    from .flags import flag_on
    from . import tenants as _tn
    _t = _tn.active()
    return JSONResponse({
        "pending": approvals.list_pending(),
        "recent": approvals.list_recent(30),
        "enabled": flag_on("AUTOPILOT_ENABLED"),
        "execute": flag_on("AUTOPILOT_EXECUTE"),
        "product": "Kintu",
        "tenant": {"id": _t.id, "label": _t.label, "currency": _t.currency,
                   "margin_profile": _t.margin_profile},
    })


@router.get("/autopilot/api/tenants")
def tenants_list(token: str | None = Query(None), request: Request = None):
    """Kintu — inquilinos (marcas) registrados en el motor. CMC activo; las demás
    quedan registradas pero apagadas hasta tener cuenta de ads + atribución."""
    _check_token(token, request)
    from . import tenants as _tn
    act = _tn.active().id
    out = []
    for tid, t in _tn.all_tenants().items():
        out.append({
            "id": t.id, "label": t.label, "ad_account": t.ad_account or None,
            "currency": t.currency, "margin_profile": t.margin_profile,
            "attribution": t.attribution, "enabled": t.enabled,
            "active": t.id == act,
            "ready": bool(t.ad_account) and t.attribution != "none",
        })
    return JSONResponse({"product": "Kintu", "active": act, "tenants": out})


@router.post("/autopilot/api/pending/{pid}/approve")
async def pending_approve(pid: str, token: str | None = Query(None), request: Request = None):
    """Aprueba y APLICA la acción (re-valida límites; escritura real gateada por
    AUTOPILOT_EXECUTE). Devuelve el item con su resultado de aplicación."""
    _check_token(token, request)
    from . import approvals
    item = await approvals.approve(pid, by="dashboard")
    if not item:
        raise HTTPException(404, "no encontrada")
    return JSONResponse({"ok": item["status"] in ("approved",), "item": item})


@router.post("/autopilot/api/pending/{pid}/reject")
def pending_reject(pid: str, token: str | None = Query(None), request: Request = None):
    """Rechaza una acción pendiente (no se aplica)."""
    _check_token(token, request)
    from . import approvals
    item = approvals.reject(pid, by="dashboard")
    if not item:
        raise HTTPException(404, "no encontrada")
    return JSONResponse({"ok": True, "item": item})


# ── Loop de medición de creatividades — ranking de anuncios por rendimiento ──

@router.get("/autopilot/api/creatives")
def creatives_get(token: str | None = Query(None), request: Request = None):
    """Último ranking de creatividades (lee snapshot; no golpea Meta)."""
    _check_token(token, request)
    from . import creatives
    data = creatives.load_snapshot()
    if not data:
        return JSONResponse({"empty": True, "message": "Sin ranking todavía. Pulsa Actualizar."})
    return JSONResponse(data)


@router.post("/autopilot/api/creatives/refresh")
async def creatives_refresh(window: int = Query(30), token: str | None = Query(None),
                            request: Request = None):
    """Recalcula el ranking de creatividades desde Meta (read-only) y lo cachea."""
    _check_token(token, request)
    from . import creatives
    try:
        data = await creatives.rank_creatives(window_days=window)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"No se pudo medir creatividades: {e}")
    creatives.save_snapshot(data)
    # #5 — si el toggle `autogen` está ON, genera una variante del ángulo ganador.
    try:
        from . import settings as _ap_settings
        if _ap_settings.get("autogen"):
            created = await creatives.autogen_winner(data, n=1)
            data["autogen_created"] = [c.get("id") for c in created]
    except Exception as _e_ag:  # noqa: BLE001
        logging.getLogger("bot").warning("autogen en refresh falló: %s", _e_ag)
    return JSONResponse(data)


# ── Digest + ajustes (toggle autogen) ────────────────────────────────────────

@router.get("/autopilot/api/digest")
def autopilot_digest(days: int = Query(7), token: str | None = Query(None),
                     request: Request = None):
    """Resumen de los últimos N días (qué aplicó/propuso/aprobó + creatividad)."""
    _check_token(token, request)
    from . import digest
    d = digest.build_digest(days=days)
    d["text"] = digest.render_text(d)
    return JSONResponse(d)


@router.get("/autopilot/api/settings")
def autopilot_settings_get(token: str | None = Query(None), request: Request = None):
    """Ajustes runtime toggleables (hoy: autogen)."""
    _check_token(token, request)
    from . import settings as _ap_settings
    return JSONResponse(_ap_settings.get_settings())


@router.post("/autopilot/api/settings")
async def autopilot_settings_set(request: Request, token: str | None = Query(None)):
    """Prende/apaga un ajuste. Body: {key, value}."""
    _check_token(token, request)
    from . import settings as _ap_settings
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    key = (body or {}).get("key")
    if key not in ("autogen",):
        raise HTTPException(400, "ajuste no permitido")
    return JSONResponse(_ap_settings.set_setting(key, bool(body.get("value"))))


@router.post("/autopilot/api/creatives/autogen")
async def autopilot_creatives_autogen(n: int = Query(1), token: str | None = Query(None),
                                      request: Request = None):
    """Genera AHORA n variante(s) del ángulo ganador (manual). Cuesta ~$75 CLP/imagen.
    Devuelve los borradores creados."""
    _check_token(token, request)
    from . import creatives
    try:
        created = await creatives.autogen_winner(n=max(1, min(int(n), 3)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"No se pudo generar: {e}")
    return JSONResponse({"created": created})


# ── #1 — Capacidad por especialidad (sobrecupos: default seguir llenando) ────

@router.get("/autopilot/api/capacity")
def autopilot_capacity_get(token: str | None = Query(None), request: Request = None):
    """Estado de capacidad por especialidad (info Medilink) + overrides del dueño."""
    _check_token(token, request)
    from . import capacity, settings as _s
    return JSONResponse({
        "snapshot": (capacity.load_snapshot() or {}).get("data", {}),
        "override": _s.get("capacity_override") or {},
    })


@router.post("/autopilot/api/capacity")
async def autopilot_capacity_set(request: Request, token: str | None = Query(None)):
    """Marca una especialidad. Body: {especialidad, status:'fill'|'lleno', target?}.
    Default 'fill' = seguir llenando (sobrecupos). 'lleno' = no escalar más."""
    _check_token(token, request)
    from . import settings as _s
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    esp = (body or {}).get("especialidad")
    if not esp:
        raise HTTPException(400, "Falta 'especialidad'")
    s = _s.set_capacity(esp, status=(body.get("status") or "fill"), target=body.get("target"))
    return JSONResponse({"ok": True, "capacity_override": s.get("capacity_override", {})})


# ── CAC real por anuncio — atribución LOCAL (source_id × agendó/pagó × spend) ─

@router.get("/autopilot/api/cac-local")
async def autopilot_cac_local(window: int = Query(30), token: str | None = Query(None),
                              request: Request = None):
    """CAC real por anuncio calculado localmente (no depende del CAPI de Meta).
    Cruza meta_referrals.source_id (qué ad) × agendó/pagó × spend del ad."""
    _check_token(token, request)
    from . import cac_local
    try:
        return JSONResponse(await cac_local.cac_by_ad(window_days=window))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"No se pudo calcular CAC local: {e}")
