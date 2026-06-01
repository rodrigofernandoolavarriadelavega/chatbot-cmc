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
import json
import logging
import re
import time
import unicodedata
import uuid
from pathlib import Path

from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from config import ADMIN_TOKEN
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
    if token and token == ADMIN_TOKEN:
        return
    cookie = request.cookies.get("admin_token") if request else None
    if cookie and cookie == ADMIN_TOKEN:
        return
    raise HTTPException(status_code=403, detail="Token inválido")


@router.get("/autopilot", response_class=HTMLResponse)
def autopilot_dashboard(token: str | None = Query(None), request: Request = None):
    _check_token(token, request)
    if not _TEMPLATE.exists():
        raise HTTPException(404, "Dashboard no disponible")
    return HTMLResponse(_TEMPLATE.read_text(encoding="utf-8"))


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
