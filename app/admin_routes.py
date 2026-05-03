"""Admin panel API routes — all /admin/api/* endpoints."""
import asyncio
import hashlib
import hmac
import logging
import time
from datetime import date, timedelta
from collections import defaultdict, deque

from fastapi import APIRouter, Request, Query, HTTPException, Header, Depends, Cookie, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse

from config import ADMIN_TOKEN, ORTODONCIA_TOKEN, COOKIE_SECRET, STAFF_PHONES
from messaging import send_whatsapp, send_instagram, send_messenger, edit_whatsapp_message
from session import (get_session, reset_session, save_session, get_metricas,
                     log_message, get_messages, get_conversations, log_event,
                     update_message_text_by_wamid, get_message_by_wamid,
                     get_tags, save_tag, delete_tag, get_tags_summary, search_messages,
                     get_kine_tracking_all, save_kine_tracking,
                     get_ortodoncia_pacientes, set_ortodoncia_tipo, get_ortodoncia_sync_max_fecha,
                     get_waitlist_all, cancel_waitlist,
                     get_confirmaciones_dia, get_citas_cache_todos,
                     get_metricas_fidelizacion, get_nps_por_profesional,
                     get_notes, save_notes, get_patient_context, get_registration_stats,
                     get_referral_stats, get_case_study_report,
                     get_patient_files, get_media_stats, get_demanda_no_disponible,
                     get_conversion_funnel_by_especialidad,
                     mark_admin_seen, get_unread_counts,
                     save_profile, get_profile, get_phone_by_rut,
                     delete_patient_data, get_privacy_consent, save_privacy_consent,
                     get_next_cita_bot_by_phone, mark_reminder_sent,
                     _conn)
from medilink import (buscar_paciente, crear_paciente, buscar_primer_dia,
                      buscar_slots_dia, crear_cita, listar_citas_paciente,
                      cancelar_cita, get_citas_seguimiento_mes, sync_citas_dia,
                      sync_ortodoncia_rango,
                      SEGUIMIENTO_ESPECIALIDADES, PROFESIONALES)

log = logging.getLogger("bot")

router = APIRouter(tags=["admin"])


# ── Cookie signing ───────────────────────────────────────────────────────────

_COOKIE_NAME = "cmc_session"
_COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days


def _cookie_key() -> bytes:
    """Derive a signing key from COOKIE_SECRET or ADMIN_TOKEN."""
    secret = COOKIE_SECRET or ADMIN_TOKEN
    return hashlib.sha256(f"cmc-cookie-sign:{secret}".encode()).digest()


def _sign_cookie(role: str) -> str:
    """Create a signed cookie value: role:expires:signature."""
    expires = int(time.time()) + _COOKIE_MAX_AGE
    payload = f"{role}:{expires}"
    sig = hmac.new(_cookie_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_cookie(value: str) -> str | None:
    """Verify a signed cookie. Returns role ('admin'|'ortodoncia') or None."""
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    role, expires_str, sig = parts
    if role not in ("admin", "ortodoncia"):
        return None
    try:
        expires = int(expires_str)
    except ValueError:
        return None
    if time.time() > expires:
        return None
    payload = f"{role}:{expires_str}"
    expected = hmac.new(_cookie_key(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return role


def _set_session_cookie(response: Response, role: str, is_https: bool) -> None:
    """Set the signed httpOnly session cookie on a response."""
    response.set_cookie(
        key=_COOKIE_NAME,
        value=_sign_cookie(role),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    """Delete the session cookie."""
    response.delete_cookie(key=_COOKIE_NAME, path="/")


# ── Auth helpers ─────────────────────────────────────────────────────────────

def _extract_token(query_token: str | None, auth_header: str | None) -> str:
    """Obtiene el token desde Authorization: Bearer ... o, como fallback,
    desde el query param ?token=... (para mantener compatibilidad con el panel HTML).
    """
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header.split(None, 1)[1].strip()
    return query_token or ""


def require_admin(request: Request,
                  token: str | None = Query(None),
                  authorization: str | None = Header(None),
                  cmc_session: str | None = Cookie(None)) -> str:
    """Dependency FastAPI que valida token admin.
    Prioridad: Bearer header > cookie > query param.
    Retorna el token validado."""
    # 1. Bearer header
    if authorization and authorization.lower().startswith("bearer "):
        tk = authorization.split(None, 1)[1].strip()
        if hmac.compare_digest(tk or '', ADMIN_TOKEN):
            return tk
    # 2. Cookie
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role == "admin":
            return ADMIN_TOKEN
    # 3. Query param (backwards compat)
    if token and hmac.compare_digest(token or '', ADMIN_TOKEN):
        return token
    raise HTTPException(status_code=401, detail="Token inválido")


def require_ortodoncia(request: Request,
                       token: str | None = Query(None),
                       authorization: str | None = Header(None),
                       cmc_session: str | None = Cookie(None)) -> str:
    """Dependency FastAPI que valida token de ortodoncia o admin.
    Prioridad: Bearer header > cookie > query param."""
    # 1. Bearer header
    if authorization and authorization.lower().startswith("bearer "):
        tk = authorization.split(None, 1)[1].strip()
        if hmac.compare_digest(tk, ADMIN_TOKEN) or hmac.compare_digest(tk, ORTODONCIA_TOKEN):
            return tk
    # 2. Cookie
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return ORTODONCIA_TOKEN if role == "ortodoncia" else ADMIN_TOKEN
    # 3. Query param (backwards compat)
    if token and (hmac.compare_digest(token, ADMIN_TOKEN) or hmac.compare_digest(token, ORTODONCIA_TOKEN)):
        return token
    raise HTTPException(status_code=403, detail="Acceso denegado")


# ── Login page & auth endpoints ──────────────────────────────────────────────

_LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<title>Iniciar sesion — Panel CMC</title>
<style>
:root {
  --bg: #f1f5f9; --surface: #ffffff; --border: #e2e8f0;
  --text: #1e293b; --text-2: #475569; --text-3: #94a3b8;
  --primary: #1172AB; --primary-hover: #0e5f8f;
  --red: #ef4444; --red-soft: #fef2f2;
  --radius: 10px;
  --shadow: 0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.06);
  --shadow-md: 0 4px 6px rgba(0,0,0,.07),0 2px 4px rgba(0,0,0,.06);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: var(--bg); color: var(--text);
  font-family: "Inter", system-ui, sans-serif;
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
}
.login-card {
  background: var(--surface); border-radius: 14px;
  box-shadow: var(--shadow-md); padding: 36px 32px 32px;
  width: 360px; max-width: 92vw;
}
.login-logo {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 24px; justify-content: center;
}
.login-logo img { height: 40px; object-fit: contain; }
.login-logo h1 { font-size: 15px; font-weight: 700; color: var(--text); }
.login-subtitle {
  text-align: center; font-size: 13px; color: var(--text-2);
  margin-bottom: 20px;
}
label {
  display: block; font-size: 12px; font-weight: 600;
  color: var(--text-2); margin-bottom: 6px;
}
input[type="password"] {
  width: 100%; padding: 10px 14px; border: 1px solid var(--border);
  border-radius: 8px; font-family: inherit; font-size: 14px;
  color: var(--text); background: var(--bg);
  transition: border-color .15s;
}
input[type="password"]:focus {
  outline: none; border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(17,114,171,.12);
}
.btn-login {
  width: 100%; padding: 11px; border: none; border-radius: 8px;
  background: var(--primary); color: #fff;
  font-family: inherit; font-size: 14px; font-weight: 600;
  cursor: pointer; margin-top: 16px; transition: background .15s;
}
.btn-login:hover { background: var(--primary-hover); }
.btn-login:disabled { opacity: .6; cursor: not-allowed; }
.error-msg {
  margin-top: 12px; padding: 8px 12px; border-radius: 8px;
  background: var(--red-soft); color: var(--red);
  font-size: 12px; font-weight: 500; text-align: center;
  display: none;
}
</style>
</head>
<body>
<div class="login-card">
  <div class="login-logo">
    <img src="/static/logo.png" alt="CMC">
    <h1>Panel de Recepcion</h1>
  </div>
  <p class="login-subtitle">Centro Medico Carampangue</p>
  <form id="login-form" method="POST" action="/admin/login">
    <label for="password">Contrasena</label>
    <input type="password" id="password" name="password"
           placeholder="Ingresa la contrasena" autocomplete="current-password" required autofocus>
    <button type="submit" class="btn-login" id="btn-submit">Iniciar sesion</button>
    <div class="error-msg" id="error-msg"></div>
  </form>
</div>
<script>
document.getElementById("login-form").addEventListener("submit", async function(e) {
  e.preventDefault();
  const btn = document.getElementById("btn-submit");
  const errDiv = document.getElementById("error-msg");
  btn.disabled = true;
  errDiv.style.display = "none";
  try {
    const r = await fetch("/admin/login", {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: "password=" + encodeURIComponent(document.getElementById("password").value),
      redirect: "follow",
    });
    if (r.redirected) {
      window.location.href = r.url;
      return;
    }
    const data = await r.json().catch(() => null);
    errDiv.textContent = (data && data.detail) || "Contrasena incorrecta";
    errDiv.style.display = "block";
  } catch (err) {
    errDiv.textContent = "Error de conexion";
    errDiv.style.display = "block";
  }
  btn.disabled = false;
});
</script>
</body>
</html>"""


@router.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(cmc_session: str | None = Cookie(None)):
    """Muestra la pagina de login. Si ya hay cookie valida, redirige al panel."""
    role = _verify_cookie(cmc_session) if cmc_session else None
    if role == "admin":
        return RedirectResponse(url="/admin", status_code=302)
    return HTMLResponse(content=_LOGIN_HTML)


_LOGIN_ATTEMPTS: dict[str, deque] = {}
_LOGIN_RATE_WINDOW = 60   # segundos
_LOGIN_RATE_MAX = 5       # intentos


def _login_rate_limited(ip: str) -> bool:
    """True si la IP excedió el límite de intentos en la ventana actual."""
    import time as _t_lr
    now = _t_lr.time()
    bucket = _LOGIN_ATTEMPTS.setdefault(ip, deque())
    # Drop old entries
    while bucket and (now - bucket[0]) > _LOGIN_RATE_WINDOW:
        bucket.popleft()
    if len(bucket) >= _LOGIN_RATE_MAX:
        return True
    bucket.append(now)
    return False


@router.post("/admin/login")
def admin_login(request: Request, password: str = Form(...)):
    """Valida la contrasena y setea la cookie de sesion."""
    ip = request.client.host if request.client else "?"
    if _login_rate_limited(ip):
        log.warning("Admin login rate-limited ip=%s", ip)
        raise HTTPException(status_code=429, detail="Demasiados intentos. Intenta en 1 minuto.")
    is_https = (request.url.scheme == "https"
                or request.headers.get("x-forwarded-proto") == "https")

    if hmac.compare_digest(password, ADMIN_TOKEN):
        response = RedirectResponse(url="/admin", status_code=302)
        _set_session_cookie(response, "admin", is_https)
        log.info("Admin login OK (cookie set) ip=%s", ip)
        return response

    if hmac.compare_digest(password, ORTODONCIA_TOKEN):
        response = RedirectResponse(url="/admin", status_code=302)
        _set_session_cookie(response, "ortodoncia", is_https)
        log.info("Ortodoncia login OK (cookie set) ip=%s", ip)
        return response

    log.warning("Admin login FAIL ip=%s", ip)
    raise HTTPException(status_code=401, detail="Contrasena incorrecta")


@router.post("/admin/logout")
def admin_logout():
    """Borra la cookie de sesion y redirige al login."""
    response = RedirectResponse(url="/admin/login", status_code=302)
    _clear_session_cookie(response)
    return response


# ── Conversations & metrics ──────────────────────────────────────────────────

@router.get("/admin/api/conversations")
def admin_conversations(_: str = Depends(require_admin)):
    convs = get_conversations()
    for c in convs:
        role = STAFF_PHONES.get(c.get("phone", ""), "")
        if role:
            c["is_staff"] = True
            c["staff_role"] = role
    return convs


@router.get("/admin/api/conversations/{phone}")
def admin_conversation_detail(phone: str, _: str = Depends(require_admin)):
    return get_messages(phone)


@router.get("/admin/api/staff-phones")
def admin_staff_phones(_: str = Depends(require_admin)):
    return STAFF_PHONES


@router.get("/admin/api/contacts/bad-names")
def admin_bad_names(_: str = Depends(require_admin)):
    """Lista contactos con nombres que parecen respuestas accidentales
    en vez del nombre real del paciente (ej. 'Si Primera Vez').
    Para corrección masiva desde el panel admin.
    """
    from session import get_contactos_con_nombre_sospechoso
    return {"contactos": get_contactos_con_nombre_sospechoso()}


@router.patch("/admin/api/contact/{phone}/name")
async def admin_update_contact_name(phone: str, request: Request,
                                     _: str = Depends(require_admin)):
    """Actualiza el nombre de un contacto. Body: {\"nombre\": \"...\"}.
    Retorna {\"ok\": bool}."""
    from session import update_contact_nombre
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "JSON inválido"}
    nuevo = (body.get("nombre") or "").strip()
    if not nuevo or len(nuevo) < 2:
        return {"ok": False, "error": "nombre requerido (mín 2 chars)"}
    if len(nuevo) > 100:
        return {"ok": False, "error": "nombre demasiado largo (max 100)"}
    ok = update_contact_nombre(phone, nuevo)
    return {"ok": ok, "phone": phone, "nombre": nuevo}


# ── Permisos del bot profesional por profesional ────────────────────────────
# Persistencia en JSON plano (solo lo edita el director del CMC desde el
# dashboard /profesionalescmc; no requiere DB).
import json as _json_perm
from pathlib import Path as _Path_perm
_PERMS_FILE = _Path_perm(__file__).parent.parent / "data" / "profesionales_permisos.json"


def _load_perms() -> dict:
    try:
        if _PERMS_FILE.exists():
            return _json_perm.loads(_PERMS_FILE.read_text(encoding="utf-8"))
    except Exception as _e_load:
        log.warning("profesionales_permisos: falla al leer %s: %s", _PERMS_FILE, _e_load)
    return {}


def _save_perms(data: dict) -> None:
    _PERMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PERMS_FILE.write_text(_json_perm.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@router.get("/admin/api/profesionales-permisos")
def admin_profesionales_permisos(_: str = Depends(require_admin)):
    """Retorna dict {phone: {feature_id: bool}} con permisos actuales."""
    return _load_perms()


@router.post("/admin/api/profesionales-permisos")
async def admin_profesionales_permisos_set(request: Request, _: str = Depends(require_admin)):
    """Guarda el dict completo de permisos. Body: {phone: {feature: bool}}."""
    body = await request.json()
    if not isinstance(body, dict):
        return {"ok": False, "error": "body debe ser dict"}
    _save_perms(body)
    log.info("profesionales_permisos actualizado: %d profesionales", len(body))
    return {"ok": True, "profesionales": len(body)}


def get_permiso(phone: str, feature: str, default: bool = False) -> bool:
    """Helper para que otros módulos (flows, doctor_alerts) consulten permisos.
    Uso: `if get_permiso("56987834148", "asistente_clinico"): ...`
    """
    perms = _load_perms()
    return bool(perms.get(phone, {}).get(feature, default))


@router.get("/admin/api/metrics")
def admin_metrics(_: str = Depends(require_admin)):
    return get_metricas(dias=30)


# ── Waitlist ─────────────────────────────────────────────────────────────────

@router.get("/admin/api/waitlist")
def admin_waitlist(_: str = Depends(require_admin)):
    """Lista de espera completa (activas + notificadas + canceladas)."""
    return get_waitlist_all()


@router.post("/admin/api/waitlist/{wl_id}/cancel")
def admin_waitlist_cancel(wl_id: int, _: str = Depends(require_admin)):
    """Marca una entrada de waitlist como cancelada (por recepción)."""
    cancel_waitlist(wl_id)
    return {"ok": True}


# ── Confirmaciones ───────────────────────────────────────────────────────────

@router.get("/admin/api/confirmaciones")
def admin_confirmaciones(fecha: str = None, _: str = Depends(require_admin)):
    """Estado de confirmación de las citas del bot para una fecha (default: mañana)."""
    if not fecha:
        fecha = (date.today() + timedelta(days=1)).isoformat()
    filas = get_confirmaciones_dia(fecha)
    resumen = {"confirmed": 0, "reagendar": 0, "cancelar": 0, "pendiente": 0}
    for f in filas:
        estado = f.get("confirmation_status") or "pendiente"
        if estado in resumen:
            resumen[estado] += 1
    return {"fecha": fecha, "total": len(filas), "resumen": resumen, "citas": filas}


# ── Métricas fidelización ────────────────────────────────────────────────────

@router.get("/admin/api/metricas-fidelizacion")
def admin_metricas_fidelizacion(dias: int | None = None,
                                _: str = Depends(require_admin)):
    """Métricas de campañas de fidelización.
    ?dias=7 → última semana, ?dias=30 → último mes, sin param → todo."""
    if dias is not None and dias not in (7, 30):
        dias = 30  # fallback a 30 si mandan algo raro
    return get_metricas_fidelizacion(dias)


# ── NPS por profesional ──────────────────────────────────────────────────────

@router.get("/admin/api/nps")
def admin_nps(dias: int | None = None, _: str = Depends(require_admin)):
    """NPS por profesional basado en respuestas post-consulta (mejor/igual/peor).
    ?dias=30 → último mes, sin param → todo el histórico."""
    return get_nps_por_profesional(dias)


# ── Takeover, reply, resume ──────────────────────────────────────────────────

@router.post("/admin/api/takeover/{phone}")
async def admin_takeover(phone: str, _: str = Depends(require_admin)):
    """Recepcionista toma control manual de una conversación."""
    save_session(phone, "HUMAN_TAKEOVER", {"hold_sent": True, "msgs_sin_respuesta": 0,
                                            "handoff_reason": "manual (recepcionista)"})
    log_event(phone, "derivado_humano", {"razon": "takeover manual desde panel"})
    await send_whatsapp(phone,
        "Hola 👋 Te está atendiendo una recepcionista del Centro Médico Carampangue.\n"
        "¿En qué te podemos ayudar?")
    log_message(phone, "out", "[Recepcionista tomó la conversación]", "HUMAN_TAKEOVER")
    return {"ok": True}


@router.post("/admin/api/reply")
async def admin_reply(request: Request, _: str = Depends(require_admin)):
    """Recepcionista envía un mensaje al paciente desde el panel (WhatsApp, Instagram o Messenger)."""
    body = await request.json()
    phone = body.get("phone", "").strip()
    message = body.get("message", "").strip()
    if not phone or not message:
        raise HTTPException(status_code=400, detail="phone y message son requeridos")

    wamid = None
    if phone.startswith("ig_"):
        igsid = phone[3:]
        await send_instagram(igsid, message)
        canal = "instagram"
    elif phone.startswith("fb_"):
        psid = phone[3:]
        await send_messenger(psid, message)
        canal = "messenger"
    else:
        wamid = await send_whatsapp(phone, message)
        canal = "whatsapp"

    # Auto-takeover: cuando la recepcionista escribe, el paciente está
    # conversando con ella — el bot NO debe seguir procesando intents de
    # los mensajes del paciente en paralelo. Forzamos HUMAN_TAKEOVER si
    # no estaba ya. Salir requiere explícitamente "devolver al bot".
    _sess = get_session(phone)
    state = _sess.get("state", "HUMAN_TAKEOVER")
    from session import save_session as _save_sess_for_takeover
    _data = _sess.get("data", {}) or {}
    # Excepción sistémica: si el paciente está en un estado TRANSACCIONAL
    # (agendando, completando registro, confirmando cita), un mensaje de la
    # recepcionista en paralelo NO debe interrumpir el flow. Auditoría
    # 2026-04-28: 55 takeovers desde WAIT_SLOT/7d, solo 1 terminó en cita.
    _ESTADOS_TRANSACCIONALES = {
        "WAIT_SLOT", "CONFIRMING_CITA", "WAIT_QUICK_BOOK",
        "WAIT_RUT_AGENDAR", "WAIT_RUT_CANCELAR", "WAIT_RUT_REAGENDAR", "WAIT_RUT_VER",
        "WAIT_DATOS_NUEVO", "WAIT_NOMBRE_NUEVO", "WAIT_FECHA_NAC", "WAIT_SEXO",
        "WAIT_COMUNA", "WAIT_EMAIL", "WAIT_REFERRAL", "WAIT_REFERRAL_POST",
        "WAIT_REFERRAL_CODE",
        "WAIT_BOOKING_FOR", "WAIT_PHONE_OWNER_NAME",
        "WAIT_MODALIDAD", "WAIT_CITA_CANCELAR", "WAIT_CITA_REAGENDAR",
        "CONFIRMING_CANCEL", "CONFIRMING_REAGENDAR",
    }
    if state in _ESTADOS_TRANSACCIONALES:
        log_event(phone, "recep_msg_durante_flow", {"state": state})
        # NO cambiamos el estado — el paciente sigue en el flow transaccional.
        # La recepcionista puede contestarle algo en paralelo sin interrumpir.
    elif state != "HUMAN_TAKEOVER":
        _data["handoff_reason"] = "recepcionista_respondio"
        log_event(phone, "auto_takeover_recep_reply", {"from_state": state})
        state = "HUMAN_TAKEOVER"
    # Reset contador msgs_sin_respuesta: la recepcionista YA respondió, así que
    # los próximos mensajes del paciente no deben gatillar "Recibido 🙏" /
    # "Seguimos atentos" automáticos. Sin esto el bot mandaba auto-replies
    # mientras la recepcionista ya estaba en la conversación.
    _data["msgs_sin_respuesta"] = 0
    # Marca que la recepcionista YA habló al menos una vez en esta sesión.
    # Sin esto el contador se reseteaba a 0 y el siguiente msg del paciente
    # volvía a disparar "Recibido 🙏" después de cada respuesta humana.
    # Ver caso real 56975932459 (2026-04-23): 10 acks repetidos.
    _data["human_replied"] = True
    _save_sess_for_takeover(phone, "HUMAN_TAKEOVER", _data)
    log_message(phone, "out", f"[Recepcionista] {message}", state, canal=canal, wamid=wamid)
    log_event(phone, "recepcionista_respondio", {"mensaje": message[:200]})
    try:
        from session import try_autocapture_rut_name
        try_autocapture_rut_name(phone, message)
    except Exception as e:
        log.warning("try_autocapture_rut_name falló phone=%s: %s", phone, e)
    return {"ok": True, "wamid": wamid}


@router.get("/admin/api/scheduler-info")
async def admin_scheduler_info(_: str = Depends(require_admin)):
    """Lista jobs del APScheduler con próximo run. Útil para diagnóstico."""
    import sys
    # uvicorn puede registrar el módulo como "app.main" o "main" según cómo se
    # arrancó. Buscamos en ambos para no crear un scheduler nuevo por import.
    mod = sys.modules.get("app.main") or sys.modules.get("main")
    if mod is None or not hasattr(mod, "scheduler"):
        return {"error": "scheduler_module_not_found", "running": False, "jobs": []}
    scheduler = mod.scheduler
    jobs = []
    for j in scheduler.get_jobs():
        jobs.append({
            "id": j.id,
            "name": j.name,
            "trigger": str(j.trigger),
            "next_run": str(j.next_run_time) if j.next_run_time else None,
        })
    return {"count": len(jobs), "jobs": jobs, "running": scheduler.running}


@router.get("/admin/api/medilink-stats")
async def admin_medilink_stats(_: str = Depends(require_admin)):
    """Stats rápidas de Medilink: contador 429, tamaño cache proxima."""
    from medilink import get_stats_429, _proxima_cache, _PROXIMA_CACHE_TTL
    return {
        "stats_429": get_stats_429(),
        "proxima_cache_entries": len(_proxima_cache),
        "proxima_cache_ttl_seconds": _PROXIMA_CACHE_TTL,
    }


@router.post("/admin/api/edit-message")
async def admin_edit_message(request: Request, _: str = Depends(require_admin)):
    """Edita un mensaje WhatsApp ya enviado. Meta impone ventana de 15 min y sólo texto."""
    from datetime import datetime, timezone
    body = await request.json()
    phone = (body.get("phone") or "").strip()
    wamid = (body.get("wamid") or "").strip()
    new_text = (body.get("text") or "").strip()
    if not phone or not wamid or not new_text:
        raise HTTPException(status_code=400, detail="phone, wamid y text son requeridos")

    msg = get_message_by_wamid(wamid)
    if not msg:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")
    if msg.get("phone") != phone or msg.get("direction") != "out":
        raise HTTPException(status_code=400, detail="Mensaje no pertenece a esta conversación")
    if msg.get("canal") not in (None, "whatsapp"):
        raise HTTPException(status_code=400, detail="Sólo se pueden editar mensajes de WhatsApp")

    # Ventana 15 min
    try:
        ts = datetime.fromisoformat(msg["ts"].replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        age = 0
    if age > 900:
        raise HTTPException(status_code=400, detail="Ventana de 15 min expiró")

    ok, err = await edit_whatsapp_message(phone, wamid, new_text)
    if not ok:
        raise HTTPException(status_code=502, detail=f"Meta rechazó la edición: {err}")

    # Actualizar DB conservando prefijo [Recepcionista] si existía
    orig = msg.get("text") or ""
    prefix = "[Recepcionista] " if orig.startswith("[Recepcionista]") else ""
    update_message_text_by_wamid(wamid, f"{prefix}{new_text}")
    log_event(phone, "mensaje_editado", {"wamid": wamid, "nuevo": new_text[:200]})
    return {"ok": True}


@router.post("/admin/api/resume/{phone}")
async def admin_resume(phone: str, _: str = Depends(require_admin)):
    """Devuelve el control al bot y notifica al paciente."""
    reset_session(phone)
    log_event(phone, "bot_reanudado")
    await send_whatsapp(phone,
        "Continuamos con el asistente automático 😊\n"
        "Escribe *menu* cuando quieras.")
    log_message(phone, "out", "[Bot reanudado por recepcionista]", "IDLE")
    return {"ok": True}


# ── Paciente & citas ─────────────────────────────────────────────────────────

@router.get("/admin/api/paciente")
async def admin_buscar_paciente(rut: str, _: str = Depends(require_admin)):
    """Busca un paciente en Medilink por RUT."""
    paciente = await buscar_paciente(rut)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    return paciente


@router.get("/admin/api/slots")
async def admin_slots(especialidad: str, _: str = Depends(require_admin)):
    """Retorna la próxima fecha disponible y sus slots para una especialidad."""
    fecha = await buscar_primer_dia(especialidad)
    if not fecha:
        raise HTTPException(status_code=404, detail="Sin disponibilidad")
    slots, _ = await buscar_slots_dia(especialidad, fecha)
    # Incluir nombre del profesional en cada slot
    for s in slots:
        pid = s.get("id_profesional")
        s["profesional_nombre"] = PROFESIONALES.get(pid, {}).get("nombre", f"Prof. {pid}")
    return {"fecha": fecha, "slots": slots}


@router.post("/admin/api/agendar")
async def admin_agendar(request: Request, _: str = Depends(require_admin)):
    """Crea una cita desde el panel de recepción."""
    _AGENDA_DIA_CACHE.clear()  # invalidar caché: la nueva cita debe aparecer al próximo GET
    body = await request.json()
    rut        = body.get("rut", "").strip()
    nombre     = body.get("nombre", "").strip()
    apellidos  = body.get("apellidos", "").strip()
    id_prof    = int(body.get("id_profesional"))
    fecha      = body.get("fecha", "").strip()
    hora_ini   = body.get("hora_inicio", "").strip()
    hora_fin   = body.get("hora_fin", "").strip()
    duracion   = int(body.get("duracion", 30))

    # Buscar o crear paciente
    paciente = await buscar_paciente(rut)
    if not paciente:
        extra = {}
        for k in ("celular", "email", "fecha_nacimiento", "sexo", "comuna"):
            v = body.get(k, "").strip() if isinstance(body.get(k), str) else ""
            if v:
                extra[k] = v
        paciente = await crear_paciente(rut, nombre, apellidos, **extra)
        if not paciente:
            raise HTTPException(status_code=400, detail="No se pudo crear el paciente")

    cita = await crear_cita(paciente["id"], id_prof, fecha, hora_ini, hora_fin)
    if not cita:
        raise HTTPException(status_code=400, detail="No se pudo crear la cita en Medilink")

    log_event("admin", "cita_creada_panel", {
        "rut": rut, "id_profesional": id_prof,
        "fecha": fecha, "hora": hora_ini
    })
    return {"ok": True, "cita": cita}


@router.get("/admin/api/especialidades")
def admin_especialidades(_: str = Depends(require_admin)):
    """Retorna la lista de especialidades únicas disponibles."""
    esp = sorted({v["especialidad"] for v in PROFESIONALES.values()})
    return {"especialidades": esp}


# ── Tags ─────────────────────────────────────────────────────────────────────

@router.get("/admin/api/tags/summary")
def admin_tags_summary(_: str = Depends(require_admin)):
    """Agregado de tags en uso: counts por tag + mapa phone→tags.

    Alimenta los pills dinámicos de filtro en la bandeja admin_v2.
    Debe ir antes de /tags/{phone} para que FastAPI no capture 'summary' como phone.
    """
    return get_tags_summary()


@router.get("/admin/api/tags/{phone}")
def admin_get_tags(phone: str, _: str = Depends(require_admin)):
    return {"tags": get_tags(phone)}


@router.post("/admin/api/tags/{phone}")
async def admin_add_tag(phone: str, request: Request, _: str = Depends(require_admin)):
    body = await request.json()
    tag = body.get("tag", "").strip()
    if not tag:
        raise HTTPException(status_code=400, detail="tag requerido")
    save_tag(phone, tag)
    return {"tags": get_tags(phone)}


@router.delete("/admin/api/tags/{phone}/{tag}")
def admin_delete_tag(phone: str, tag: str, _: str = Depends(require_admin)):
    delete_tag(phone, tag)
    return {"tags": get_tags(phone)}


# ── Search ───────────────────────────────────────────────────────────────────

@router.get("/admin/api/search")
def admin_search_messages(q: str, _: str = Depends(require_admin)):
    """Busca texto en todos los mensajes de todas las conversaciones."""
    if len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Mínimo 2 caracteres")
    results = search_messages(q.strip())
    return {"q": q, "results": results}


# ── Citas paciente & anular ──────────────────────────────────────────────────

@router.get("/admin/api/citas-paciente")
async def admin_citas_paciente(rut: str, _: str = Depends(require_admin)):
    """Retorna las citas futuras de un paciente buscado por RUT."""
    paciente = await buscar_paciente(rut)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    citas = await listar_citas_paciente(paciente["id"], rut=paciente.get("rut"))
    return {"paciente": paciente, "citas": citas}


@router.post("/admin/api/anular")
async def admin_anular_cita(request: Request, _: str = Depends(require_admin)):
    """Anula una cita por su ID de Medilink."""
    _AGENDA_DIA_CACHE.clear()  # invalidar caché tras anulación
    body = await request.json()
    id_cita = int(body.get("id_cita"))
    ok = await cancelar_cita(id_cita)
    if not ok:
        raise HTTPException(status_code=400, detail="No se pudo anular la cita en Medilink")
    log_event("admin", "cita_anulada_panel", {"id_cita": id_cita})
    return {"ok": True}


# ── Kinesiología / Pacientes en Control ──────────────────────────────────────

@router.get("/admin/api/kine")
async def admin_kine(mes: str = None, especialidad: str = "kinesiologia",
                     _: str = Depends(require_admin)):
    """Retorna citas de una especialidad recurrente.
    mes=YYYY-MM → mes específico | mes=YYYY → año completo | mes=todos → todo el histórico"""
    import calendar as cal_mod

    cfg = SEGUIMIENTO_ESPECIALIDADES.get(especialidad, {})
    tracking = {(t["id_paciente"], t["id_prof"]): t for t in get_kine_tracking_all()}

    def _enrich(citas):
        for p in citas:
            t = tracking.get((p["id_paciente"], p["id_prof"]), {})
            p["total_sesiones"]    = t.get("total_sesiones", 0)
            p["modalidad"]         = t.get("modalidad", "fonasa")
            p["notas"]             = t.get("notas", "")
            p["precio_fonasa"]     = cfg.get("precio_fonasa")
            p["precio_particular"] = cfg.get("precio_particular")
        return citas

    # Modo "todos" — histórico completo desde caché
    if mes == "todos":
        ids_prof = cfg.get("ids", [])
        raw = get_citas_cache_todos(ids_prof)
        grupos: dict = defaultdict(list)
        for c in raw:
            key = (c["id_paciente"], c["id_prof"])
            grupos[key].append(c)
        citas = []
        for (id_pac, id_prof), items in grupos.items():
            items_sorted = sorted(items, key=lambda x: x["fecha"])
            citas.append({
                "id_paciente":     id_pac,
                "id_prof":         id_prof,
                "prof_nombre":     cfg.get("ids") and PROFESIONALES.get(id_prof, {}).get("nombre", ""),
                "paciente_nombre": items_sorted[0]["paciente_nombre"],
                "sesiones_mes":    len(items_sorted),
                "fechas":          [c["fecha"] for c in items_sorted],
                "primera_fecha":   items_sorted[0]["fecha"],
                "ultima_fecha":    items_sorted[-1]["fecha"],
            })
        citas = sorted(citas, key=lambda x: x["primera_fecha"])
        return {"mode": "todos", "especialidad": especialidad,
                "especialidad_label": cfg.get("label", especialidad),
                "pacientes": _enrich(citas)}

    # Modo "año" — YYYY sin mes
    if mes and len(mes) == 4 and mes.isdigit():
        year = int(mes)
        all_citas = []
        for month in range(1, 13):
            mc = await get_citas_seguimiento_mes(year, month, especialidad)
            all_citas.extend(mc)
        # Reagrupar por paciente+prof sumando sesiones
        grupos: dict = defaultdict(list)
        for c in all_citas:
            key = (c["id_paciente"], c["id_prof"])
            grupos[key].append(c)
        citas = []
        for (id_pac, id_prof), items in grupos.items():
            fechas = sorted({f for i in items for f in i.get("fechas", [i.get("primera_fecha","")])})
            citas.append({
                "id_paciente":     id_pac, "id_prof": id_prof,
                "prof_nombre":     items[0].get("prof_nombre",""),
                "paciente_nombre": items[0]["paciente_nombre"],
                "sesiones_mes":    len(fechas),
                "fechas":          fechas,
                "primera_fecha":   fechas[0] if fechas else "",
                "ultima_fecha":    fechas[-1] if fechas else "",
            })
        citas = sorted(citas, key=lambda x: x["primera_fecha"])
        return {"mode": "anio", "year": year, "especialidad": especialidad,
                "especialidad_label": cfg.get("label", especialidad),
                "pacientes": _enrich(citas)}

    # Modo mes (default)
    if mes:
        try:
            year, month = int(mes.split("-")[0]), int(mes.split("-")[1])
        except (ValueError, IndexError):
            raise HTTPException(status_code=400, detail="mes debe ser YYYY-MM, YYYY, o 'todos'")
    else:
        hoy = date.today()
        year, month = hoy.year, hoy.month
    citas = await get_citas_seguimiento_mes(year, month, especialidad)
    return {"year": year, "month": month, "mode": "mes", "especialidad": especialidad,
            "especialidad_label": cfg.get("label", especialidad), "pacientes": _enrich(citas)}


@router.get("/admin/api/kine/especialidades")
def admin_kine_especialidades(_: str = Depends(require_admin)):
    return {"especialidades": [
        {"id": k, "label": v["label"]} for k, v in SEGUIMIENTO_ESPECIALIDADES.items()
    ]}


@router.post("/admin/api/kine/sync")
async def admin_kine_sync(fecha: str = None, _: str = Depends(require_admin)):
    """Fuerza sincronización del caché de citas para una fecha (default: hoy)."""
    if not fecha:
        fecha = date.today().strftime("%Y-%m-%d")
    ids_todos = list({i for cfg in SEGUIMIENTO_ESPECIALIDADES.values() for i in cfg["ids"]})
    await sync_citas_dia(fecha, ids_todos)
    return {"ok": True, "fecha": fecha, "ids": ids_todos}


@router.put("/admin/api/kine/{id_paciente}/{id_prof}")
async def admin_kine_update(id_paciente: int, id_prof: int, request: Request,
                            _: str = Depends(require_admin)):
    """Actualiza el tracking de sesiones de un paciente en control."""
    body = await request.json()
    save_kine_tracking(
        id_paciente, id_prof,
        int(body.get("total_sesiones", 0)),
        body.get("modalidad", "fonasa"),
        body.get("notas", ""),
    )
    return {"ok": True}


# ── Ortodoncia ───────────────────────────────────────────────────────────────

@router.get("/admin/api/ortodoncia")
def admin_ortodoncia_pacientes(_: str = Depends(require_ortodoncia)):
    pacientes = get_ortodoncia_pacientes()
    ultima_sync = get_ortodoncia_sync_max_fecha()
    return {"pacientes": pacientes, "ultima_sync": ultima_sync}


@router.put("/admin/api/ortodoncia/{id_atencion}")
async def admin_ortodoncia_tipo(id_atencion: int, request: Request,
                                _: str = Depends(require_ortodoncia)):
    body = await request.json()
    tipo = body.get("tipo")
    if tipo not in ("instalacion", "control", "pendiente"):
        raise HTTPException(status_code=400, detail="tipo debe ser instalacion, control o pendiente")
    set_ortodoncia_tipo(id_atencion, tipo)
    return {"ok": True}


@router.get("/admin/api/whatsapp-quality")
async def api_whatsapp_quality(_=Depends(require_admin)):
    """Retorna quality rating y messaging limits del número WhatsApp."""
    from messaging import get_whatsapp_quality_rating
    data = await get_whatsapp_quality_rating()
    if data is None:
        raise HTTPException(status_code=502, detail="No se pudo obtener quality rating de Meta")
    return data


@router.get("/admin/api/message-statuses")
def api_message_statuses(phone: str = Query(...), _=Depends(require_admin)):
    """Resumen de estados de entrega de mensajes salientes (últimas 24h)."""
    from session import get_message_status_summary
    return get_message_status_summary(phone)


@router.post("/admin/api/send-document")
async def api_send_document(
    phone: str = Form(...),
    caption: str = Form(""),
    file: UploadFile = File(...),
    _=Depends(require_admin),
):
    """Sube un archivo y lo envía al paciente por WhatsApp."""
    from messaging import upload_media_to_whatsapp, send_whatsapp_document_by_id, send_whatsapp_image_by_id

    content = await file.read()
    if len(content) > 16 * 1024 * 1024:  # 16 MB limit de Meta
        raise HTTPException(status_code=413, detail="Archivo excede 16 MB")

    mime = file.content_type or "application/octet-stream"
    fname = file.filename or "archivo"

    media_id = await upload_media_to_whatsapp(content, mime, fname)
    if not media_id:
        raise HTTPException(status_code=502, detail="Error subiendo archivo a Meta")

    is_image = mime.startswith("image/")
    if is_image:
        await send_whatsapp_image_by_id(phone, media_id, caption=caption)
        log_text = f"[imagen] {caption}" if caption else "[imagen]"
    else:
        await send_whatsapp_document_by_id(phone, media_id, filename=fname, caption=caption)
        log_text = f"[documento: {fname}] {caption}" if caption else f"[documento: {fname}]"

    log_message(phone, "out", log_text, "HUMAN_TAKEOVER", canal="whatsapp")
    return {"ok": True, "media_id": media_id, "type": "image" if is_image else "document"}


@router.post("/admin/api/send-template")
async def api_send_template(
    phone: str = Form(...),
    template_name: str = Form(...),
    params: str = Form("[]"),
    _=Depends(require_admin),
):
    """Envía un Message Template aprobado al paciente (para mensajes fuera de ventana 24h)."""
    import json as _json
    from messaging import send_whatsapp_template

    try:
        body_params = _json.loads(params)
    except _json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="params debe ser un JSON array")

    await send_whatsapp_template(phone, template_name, body_params=body_params)
    log_message(phone, "out", f"[template: {template_name}] {', '.join(body_params)}", "HUMAN_TAKEOVER", canal="whatsapp")
    return {"ok": True}


@router.post("/admin/api/send-confirmation/{phone}")
async def api_send_confirmation(phone: str, _=Depends(require_admin)):
    """Dispara manualmente la plantilla `recordatorio_cita` para la próxima cita
    agendada por el bot. Marca reminder_sent=1 para evitar duplicado del cron 24h."""
    from messaging import send_whatsapp_template
    from reminders import _fmt_fecha_display, _fmt_hora, _nombre_corto

    cita = get_next_cita_bot_by_phone(phone)
    if not cita:
        raise HTTPException(404, "No hay cita futura agendada por el bot para este número")

    nombre = _nombre_corto(cita.get("paciente_nombre")) or "paciente"
    fecha_display = _fmt_fecha_display(cita["fecha"])
    hora = _fmt_hora(cita["hora"])
    modalidad = (cita.get("modalidad") or "particular").capitalize()
    id_cita = cita["id_cita"]

    await send_whatsapp_template(
        phone,
        "recordatorio_cita",
        body_params=[nombre, cita["especialidad"], cita["profesional"],
                     fecha_display, hora, modalidad],
        button_payloads=[f"cita_confirm:{id_cita}",
                         f"cita_reagendar:{id_cita}",
                         f"cita_cancelar:{id_cita}"],
    )
    log_message(phone, "out",
                f"[Recordatorio manual] {cita['especialidad']} con {cita['profesional']} — {fecha_display} a las {hora}",
                "HUMAN_TAKEOVER", canal="whatsapp")
    mark_reminder_sent(cita["id"])
    log_event(phone, "recordatorio_manual", {"id_cita": id_cita})
    return {"ok": True, "cita": {
        "especialidad": cita["especialidad"], "profesional": cita["profesional"],
        "fecha": fecha_display, "hora": hora, "nombre": nombre,
    }}


# ── Notas internas ───────────────────────────────────────────────────────────

@router.get("/admin/api/notes/{phone}")
def admin_get_notes(phone: str, _: str = Depends(require_admin)):
    return {"notes": get_notes(phone)}


@router.put("/admin/api/notes/{phone}")
async def admin_save_notes(phone: str, request: Request, _: str = Depends(require_admin)):
    body = await request.json()
    save_notes(phone, body.get("notes", ""))
    return {"ok": True}


@router.put("/admin/api/profile/{phone}/name")
async def admin_set_display_name(phone: str, request: Request, _: str = Depends(require_admin)):
    """Establece o actualiza el nombre visible de un contacto."""
    body = await request.json()
    nombre = body.get("nombre", "").strip()
    if not nombre:
        raise HTTPException(400, "nombre es requerido")
    save_profile(phone, "", nombre)
    return {"ok": True, "nombre": nombre}


# ── Contexto del paciente ────────────────────────────────────────────────────

@router.get("/admin/api/patient-context/{phone}")
def admin_patient_context(phone: str, _: str = Depends(require_admin)):
    return get_patient_context(phone)


# ── Estadísticas de registro ─────────────────────────────────────────────────

@router.get("/admin/api/registration-stats")
def admin_registration_stats(dias: int = 30, _: str = Depends(require_admin)):
    return get_registration_stats(dias)


@router.get("/admin/api/referral-stats")
def admin_referral_stats(dias: int = 30, _: str = Depends(require_admin)):
    """Estadísticas de cómo nos conocieron los pacientes nuevos."""
    return get_referral_stats(dias)


@router.get("/admin/api/case-study")
def admin_case_study(dias: int = 30, _: str = Depends(require_admin)):
    """Reporte consolidado de KPIs para caso de éxito."""
    return get_case_study_report(dias)


@router.post("/admin/api/ortodoncia/sync")
async def admin_ortodoncia_sync(desde: str = "2025-01-01", hasta: str = None,
                                _: str = Depends(require_ortodoncia)):
    fin = hasta or date.today().isoformat()
    from resilience import spawn_task
    spawn_task(sync_ortodoncia_rango(desde, fin))
    return {"ok": True, "desde": desde, "hasta": fin}


# ── "Agendar por ella" — slots del flujo activo del paciente ────────────────

@router.get("/admin/api/flow-slots/{phone}")
def admin_flow_slots(phone: str, _: str = Depends(require_admin)):
    """Retorna los slots actuales en la sesión del paciente (si está en WAIT_SLOT)."""
    sess = get_session(phone)
    state = sess.get("state", "IDLE")
    data = sess.get("data", {})
    if state != "WAIT_SLOT":
        raise HTTPException(status_code=409,
                            detail=f"El paciente no está eligiendo horario (estado: {state})")
    slots = data.get("slots", [])
    todos = data.get("todos_slots", slots)
    return {
        "state": state,
        "especialidad": data.get("especialidad", ""),
        "slots": slots,
        "todos_slots": todos,
    }


@router.post("/admin/api/select-slot/{phone}")
async def admin_select_slot(phone: str, request: Request, _: str = Depends(require_admin)):
    """Recepcionista selecciona un slot por el paciente: avanza el flujo a WAIT_RUT o confirma."""
    body = await request.json()
    slot_idx = body.get("slot_index")
    if slot_idx is None:
        raise HTTPException(status_code=400, detail="slot_index requerido")

    sess = get_session(phone)
    state = sess.get("state", "IDLE")
    data = sess.get("data", {})

    if state != "WAIT_SLOT":
        raise HTTPException(status_code=409,
                            detail=f"El paciente no está eligiendo horario (estado: {state})")

    todos = data.get("todos_slots", data.get("slots", []))
    if slot_idx < 0 or slot_idx >= len(todos):
        raise HTTPException(status_code=400, detail="Índice de slot inválido")

    slot = todos[slot_idx]
    data["slot_elegido"] = slot
    data["fecha_display"] = slot.get("fecha_display", "")
    data["hora_inicio"] = slot.get("hora_inicio", "")
    data["profesional"] = slot.get("profesional", "")

    # Si ya tenemos RUT del paciente, saltar a confirmar directo
    rut = data.get("rut")
    if rut:
        save_session(phone, "CONFIRMING_CITA", data)
        await send_whatsapp(phone,
            f"La recepcionista te agendó hora 📋\n\n"
            f"🏥 *{slot.get('especialidad','')}* — {slot.get('profesional','')}\n"
            f"📅 *{slot.get('fecha_display','')}*\n"
            f"🕐 *{slot.get('hora_inicio','')[:5]}*\n\n"
            "¿Confirmas? Responde *Sí* o *No*")
        log_message(phone, "out", "[Recepcionista seleccionó slot]", "CONFIRMING_CITA")
    else:
        save_session(phone, "WAIT_RUT_AGENDAR", data)
        await send_whatsapp(phone,
            f"Te busqué hora 📋\n\n"
            f"🏥 *{slot.get('especialidad','')}* — {slot.get('profesional','')}\n"
            f"📅 *{slot.get('fecha_display','')}*\n"
            f"🕐 *{slot.get('hora_inicio','')[:5]}*\n\n"
            "Para confirmar necesito tu RUT (ej: 12345678-9)")
        log_message(phone, "out", "[Recepcionista seleccionó slot, esperando RUT]", "WAIT_RUT_AGENDAR")

    log_event(phone, "slot_seleccionado_panel", {
        "profesional": slot.get("profesional", ""),
        "hora": slot.get("hora_inicio", ""),
        "fecha": slot.get("fecha_display", ""),
    })
    return {"ok": True, "new_state": "CONFIRMING_CITA" if rut else "WAIT_RUT_AGENDAR"}


# ── Timeline / Agenda del día ──────────────────────────────────────────────

@router.get("/admin/api/agenda-dia")
async def admin_agenda_dia(fecha: str = None, nocache: int = 0, _: str = Depends(require_admin)):
    # Desactivado: hacía fan-out de ~20 requests a Medilink y saturaba el
    # rate limit. Recepción ya ve la agenda directa en Medilink. Para
    # reactivar, restaurar desde git el cuerpo previo a commit cfe53c6.
    return {"fecha": fecha, "profesionales": [], "disabled": True,
            "reason": "Consultar la agenda directamente en Medilink."}


# ── Campañas estacionales ────────────────────────────────────────────────────

@router.get("/admin/api/campanas")
def admin_campanas(_: str = Depends(require_admin)):
    """Lista todas las campañas estacionales disponibles con stats de envío."""
    from fidelizacion import CAMPANAS_ESTACIONALES
    from session import get_campana_envio_stats
    stats = {s["campana_id"]: s for s in get_campana_envio_stats()}
    result = []
    for cid, camp in CAMPANAS_ESTACIONALES.items():
        s = stats.get(cid, {})
        result.append({
            "id": cid,
            "nombre": camp["nombre"],
            "temporada": camp["temporada"],
            "icono": camp["icono"],
            "descripcion": camp["descripcion"],
            "meses_sugeridos": camp["meses_sugeridos"],
            "segmento": camp.get("segmento", {}),
            "enviados": s.get("enviados", 0),
            "ultimo_envio": s.get("ultimo_envio"),
        })
    return {"campanas": result}


@router.post("/admin/api/campanas/enviar")
async def admin_enviar_campana(request: Request, _: str = Depends(require_admin)):
    """Dispara una campaña estacional manualmente.
    Body: {campana_id, tags?: [...], dias_sin_visita?: int}"""
    from fidelizacion import enviar_campana_estacional, CAMPANAS_ESTACIONALES
    from session import get_segmented_phones

    body = await request.json()
    campana_id = body.get("campana_id", "")

    if campana_id not in CAMPANAS_ESTACIONALES:
        raise HTTPException(status_code=400,
                            detail=f"Campaña no encontrada: {campana_id}")

    camp = CAMPANAS_ESTACIONALES[campana_id]
    seg = camp.get("segmento", {})
    tags = body.get("tags") or seg.get("tags")
    dias_sin_visita = body.get("dias_sin_visita") or seg.get("dias_sin_visita")

    pacientes = get_segmented_phones(tags=tags, dias_sin_visita=dias_sin_visita)

    if not pacientes:
        return {"ok": True, "enviados": 0, "errores": 0, "audiencia": 0,
                "mensaje": "Sin pacientes que cumplan los criterios"}

    enviados, errores = await enviar_campana_estacional(
        campana_id, pacientes, send_whatsapp
    )
    return {"ok": True, "enviados": enviados, "errores": errores,
            "audiencia": len(pacientes), "campana": camp["nombre"]}


@router.get("/admin/api/campanas/preview")
def admin_campana_preview(campana_id: str, _: str = Depends(require_admin)):
    """Preview de audiencia de una campaña sin enviarla."""
    from fidelizacion import CAMPANAS_ESTACIONALES
    from session import get_segmented_phones

    if campana_id not in CAMPANAS_ESTACIONALES:
        raise HTTPException(status_code=400, detail="Campaña no encontrada")

    camp = CAMPANAS_ESTACIONALES[campana_id]
    seg = camp.get("segmento", {})
    pacientes = get_segmented_phones(
        tags=seg.get("tags"), dias_sin_visita=seg.get("dias_sin_visita"))

    return {"campana": camp["nombre"], "audiencia": len(pacientes),
            "ejemplo_mensaje": camp["mensaje"].format(saludo="Hola *Juan* \U0001f44b ")}


# ── Programa de referidos ────────────────────────────────────────────────────

@router.post("/admin/api/referral-code/{phone}")
def admin_generate_referral_code(phone: str, _: str = Depends(require_admin)):
    """Genera un código de referido para un paciente."""
    from session import generate_referral_code
    code = generate_referral_code(phone)
    return {"code": code, "phone": phone}


@router.get("/admin/api/referral-code/{phone}")
def admin_get_referral_code(phone: str, _: str = Depends(require_admin)):
    """Retorna el código de referido de un paciente."""
    from session import get_referral_code
    code = get_referral_code(phone)
    return {"code": code, "phone": phone}


@router.get("/admin/api/referral-code-stats")
def admin_referral_code_stats(dias: int = 30, _: str = Depends(require_admin)):
    """Estadísticas del programa de referidos (códigos)."""
    from session import get_referral_code_stats
    return get_referral_code_stats(dias)


# ── Métricas fidelización enhanced ───────────────────────────────────────────

@router.get("/admin/api/fidelizacion-trends")
def admin_fidelizacion_trends(semanas: int = 4,
                               _: str = Depends(require_admin)):
    """Tendencias semanales de campañas de fidelización."""
    from session import get_fidelizacion_trends
    return get_fidelizacion_trends(semanas)


# ── Google Analytics Data API ────────────────────────────────────────────────

@router.get("/admin/api/analytics")
def admin_analytics(dias: int = 30, _: str = Depends(require_admin)):
    """Métricas web desde GA4 Data API."""
    from config import GA4_PROPERTY_ID, GA4_CREDENTIALS_PATH
    if not GA4_CREDENTIALS_PATH:
        raise HTTPException(503, "GA4_CREDENTIALS_PATH no configurado en .env")

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            RunReportRequest, DateRange, Metric, Dimension, OrderBy,
            RunRealtimeReportRequest,
        )
        import os
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GA4_CREDENTIALS_PATH

        client = BetaAnalyticsDataClient()
        prop = f"properties/{GA4_PROPERTY_ID}"

        # ── 1. Métricas generales (últimos N días) ──
        general = client.run_report(RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date=f"{dias}daysAgo", end_date="today")],
            metrics=[
                Metric(name="activeUsers"),
                Metric(name="sessions"),
                Metric(name="screenPageViews"),
                Metric(name="averageSessionDuration"),
                Metric(name="bounceRate"),
            ],
        ))
        g_row = general.rows[0] if general.rows else None
        resumen = {
            "usuarios": int(g_row.metric_values[0].value) if g_row else 0,
            "sesiones": int(g_row.metric_values[1].value) if g_row else 0,
            "paginas_vistas": int(g_row.metric_values[2].value) if g_row else 0,
            "duracion_promedio_seg": round(float(g_row.metric_values[3].value), 1) if g_row else 0,
            "tasa_rebote": round(float(g_row.metric_values[4].value) * 100, 1) if g_row else 0,
        }

        # ── 2. Páginas más visitadas ──
        pages_rpt = client.run_report(RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date=f"{dias}daysAgo", end_date="today")],
            dimensions=[Dimension(name="pagePath")],
            metrics=[Metric(name="screenPageViews"), Metric(name="activeUsers")],
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)],
            limit=10,
        ))
        paginas = [
            {"pagina": r.dimension_values[0].value,
             "vistas": int(r.metric_values[0].value),
             "usuarios": int(r.metric_values[1].value)}
            for r in pages_rpt.rows
        ]

        # ── 3. Fuentes de tráfico ──
        src_rpt = client.run_report(RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date=f"{dias}daysAgo", end_date="today")],
            dimensions=[Dimension(name="sessionSource")],
            metrics=[Metric(name="sessions")],
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
            limit=10,
        ))
        fuentes = [
            {"fuente": r.dimension_values[0].value or "(directo)",
             "sesiones": int(r.metric_values[0].value)}
            for r in src_rpt.rows
        ]

        # ── 4. Dispositivos ──
        dev_rpt = client.run_report(RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date=f"{dias}daysAgo", end_date="today")],
            dimensions=[Dimension(name="deviceCategory")],
            metrics=[Metric(name="sessions")],
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        ))
        dispositivos = [
            {"dispositivo": r.dimension_values[0].value,
             "sesiones": int(r.metric_values[0].value)}
            for r in dev_rpt.rows
        ]

        # ── 5. Tendencia diaria (últimos N días) ──
        trend_rpt = client.run_report(RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date=f"{dias}daysAgo", end_date="today")],
            dimensions=[Dimension(name="date")],
            metrics=[Metric(name="activeUsers"), Metric(name="sessions")],
            order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))],
        ))
        tendencia = [
            {"fecha": r.dimension_values[0].value,
             "usuarios": int(r.metric_values[0].value),
             "sesiones": int(r.metric_values[1].value)}
            for r in trend_rpt.rows
        ]

        # ── 6. Usuarios en tiempo real ──
        try:
            rt = client.run_realtime_report(RunRealtimeReportRequest(
                property=prop,
                metrics=[Metric(name="activeUsers")],
            ))
            realtime = int(rt.rows[0].metric_values[0].value) if rt.rows else 0
        except Exception:
            realtime = None

        return {
            "dias": dias,
            "resumen": resumen,
            "paginas_top": paginas,
            "fuentes": fuentes,
            "dispositivos": dispositivos,
            "tendencia": tendencia,
            "realtime": realtime,
        }

    except ImportError:
        raise HTTPException(503, "google-analytics-data no instalado. Ejecutar: pip install google-analytics-data")
    except Exception as e:
        log.error("GA4 API error: %s", e)
        raise HTTPException(502, f"Error consultando GA4: {e}")


# ── Mapa dinámico (datos filtrados por fecha) ─────────────────────────────

@router.get("/admin/api/map-data")
def admin_map_data(desde: str = Query(None), hasta: str = Query(None),
                   _: str = Depends(require_admin)):
    """Devuelve datos de comunas, localidades y direcciones filtrados por rango de fechas.

    Parámetros:
        desde: fecha inicio YYYY-MM-DD (default: todo)
        hasta: fecha fin YYYY-MM-DD (default: hoy)
    """
    import sqlite3 as _sqlite3
    from pathlib import Path
    from collections import Counter
    from random import uniform

    db_path = Path(__file__).parent.parent / "data" / "heatmap_cache.db"
    if not db_path.exists():
        raise HTTPException(404, "heatmap_cache.db no encontrado. Ejecutar scripts/heatmap_comunas.py download")

    # heatmap_cache.db es plaintext (agregados geográficos, no PII sensible).
    conn = _sqlite3.connect(str(db_path))
    conn.row_factory = _sqlite3.Row

    # ── Rango de fechas disponible ──
    rango = conn.execute("SELECT MIN(fecha) as mn, MAX(fecha) as mx FROM citas_heatmap").fetchone()
    fecha_min = rango["mn"] or "2026-01-01"
    fecha_max = rango["mx"] or date.today().isoformat()

    f_desde = desde or fecha_min
    f_hasta = hasta or fecha_max

    # ── Citas en rango ──
    citas_rango = conn.execute("""
        SELECT c.id, c.id_paciente, c.id_profesional, c.nombre_profesional, c.fecha, c.hora_inicio
        FROM citas_heatmap c
        WHERE c.fecha BETWEEN ? AND ?
    """, (f_desde, f_hasta)).fetchall()

    pac_ids_en_rango = {c["id_paciente"] for c in citas_rango if c["id_paciente"]}

    # ── Pacientes con datos ──
    if not pac_ids_en_rango:
        conn.close()
        return {
            "fecha_min": fecha_min, "fecha_max": fecha_max,
            "filtro": {"desde": f_desde, "hasta": f_hasta},
            "total_citas": 0, "pacientes_unicos": 0,
            "comunas": [], "localidades": [], "direcciones": [],
        }

    placeholders = ",".join("?" * len(pac_ids_en_rango))
    pacs = conn.execute(f"""
        SELECT id, nombre, apellidos, comuna, ciudad, direccion
        FROM pacientes_heatmap WHERE id IN ({placeholders})
    """, list(pac_ids_en_rango)).fetchall()
    pac_map = {p["id"]: dict(p) for p in pacs}

    # ── Indexar atenciones por paciente (solo en rango) ──
    atenciones_por_pac = {}
    for c in citas_rango:
        pid = c["id_paciente"]
        if not pid:
            continue
        if pid not in atenciones_por_pac:
            atenciones_por_pac[pid] = []
        atenciones_por_pac[pid].append({
            "prof": (c["nombre_profesional"] or "").strip(),
            "fecha": c["fecha"],
            "hora": c["hora_inicio"][:5] if c["hora_inicio"] else "",
        })

    # ── Normalización de comunas ──
    COMUNA_NORMALIZE = {
        "ARAUVO": "ARAUCO", "LARAQUETE": "ARAUCO", "CARAMPANGUE": "ARAUCO",
        "CONUMO": "ARAUCO", "HORCONES": "ARAUCO", "RAMADILLAS": "ARAUCO",
        "TUBUL": "ARAUCO", "LLICO": "ARAUCO", "PICHILO": "ARAUCO",
        "COLICO": "ARAUCO", "SAN JOSÉ DE COLICO": "ARAUCO",
        "SAN JOSE DE COLICO": "ARAUCO",
    }
    COMUNA_COORDS = {
        "ARAUCO": (-37.2467, -73.3178), "CURANILAHUE": (-37.4744, -73.3481),
        "LOS ALAMOS": (-37.62, -73.47), "CAÑETE": (-37.8009, -73.3967),
        "LEBU": (-37.6083, -73.65), "CORONEL": (-37.0167, -73.15),
        "LOTA": (-37.0833, -73.15), "CONTULMO": (-38.0131, -73.2292),
        "TIRUA": (-38.3333, -73.5), "CONCEPCION": (-36.8201, -73.0444),
    }
    LOC_CARAMPANGUE = {"CONUMO","MONSALVE","MANUEL LUENGO","LOS MAITENES","CRUCE NORTE",
                       "LOS SILOS","LOS BOLDOS","LA MESETA","CHILLANCITO","DUARTE","PRAT 1"}
    LOC_LARAQUETE = {"EL PINAR","VILLA BOSQUE","GONZALO ROJAS","PABLO NERUDA","LOS LINGUES",
                     "LOS MAÑIOS","VISTA HERMOSA","PLAYA NORTE","EL BOLDO","SAN PEDRO","COPIHUE"}
    LOC_RAMADILLAS = {"LOS ARTESANOS","MOLINO DEL SOL","IGNACIO CARRERA","ARTURO PEREZ","JULIO MONTT"}
    LOC_URBANO = {"VILLA PEHUEN","VILLA DON CARLOS","PORTAL DEL VALLE","VILLA EL MIRADOR",
                  "VILLA LAS ARAUCARIAS","VILLA LOS TRONCOS","VILLA RADIATA","VOLCÁN","VOLCAN",
                  "LAS AMAPOLAS","LOS CANELOS","COVADONGA","CAUPOLICAN","FRESIA","SERRANO",
                  "SAN MARTIN","PEDRO AGUIRRE","PUNTA CARAMPANGUE","AV PRAT","CALIFORNIA",
                  "TUCAPEL","BLANCO","SCHNIER","ARRAYAN","LAS PEÑAS","ALTO LOS PADRES"}
    LOC_COORDS = {
        "CARAMPANGUE": (-37.265, -73.28), "LARAQUETE": (-37.17, -73.1833),
        "RAMADILLAS": (-37.307, -73.258), "ARAUCO URBANO": (-37.2467, -73.3178),
        "TUBUL": (-37.23, -73.44), "LLICO": (-37.195, -73.565), "COLICO": (-37.3833, -73.25),
    }

    def norm_comuna(c):
        c = (c or "").strip().upper()
        if not c or c.isdigit() or len(c) < 3:
            return ""
        return COMUNA_NORMALIZE.get(c, c)

    def detect_loc(dir_str, comuna_norm):
        if comuna_norm != "ARAUCO":
            return None
        d = (dir_str or "").upper()
        for kw in LOC_CARAMPANGUE:
            if kw in d:
                return "CARAMPANGUE"
        for kw in LOC_LARAQUETE:
            if kw in d:
                return "LARAQUETE"
        for kw in LOC_RAMADILLAS:
            if kw in d:
                return "RAMADILLAS"
        for kw in LOC_URBANO:
            if kw in d:
                return "ARAUCO URBANO"
        if "TUBUL" in d:
            return "TUBUL"
        if "LLICO" in d:
            return "LLICO"
        if "COLICO" in d:
            return "COLICO"
        return None

    # ── Contar por comuna y localidad ──
    comuna_counter = Counter()
    comuna_citas = Counter()
    loc_counter = Counter()
    for pid in pac_ids_en_rango:
        p = pac_map.get(pid)
        if not p:
            continue
        cu = norm_comuna(p["comuna"])
        if cu:
            comuna_counter[cu] += 1
            comuna_citas[cu] += len(atenciones_por_pac.get(pid, []))
            loc = detect_loc(p.get("direccion", ""), cu)
            if loc:
                loc_counter[loc] += 1

    total_con_comuna = sum(comuna_counter.values()) or 1
    comunas_out = []
    for cu, cnt in comuna_counter.most_common():
        coords = COMUNA_COORDS.get(cu)
        if coords:
            comunas_out.append({
                "comuna": cu, "pacientes": cnt,
                "citas": comuna_citas[cu],
                "porcentaje": round(cnt / total_con_comuna * 100, 1),
                "lat": coords[0], "lng": coords[1],
            })

    arauco_total = comuna_counter.get("ARAUCO", 1)
    locs_out = []
    for loc, cnt in loc_counter.most_common():
        coords = LOC_COORDS.get(loc)
        if coords:
            locs_out.append({
                "localidad": loc, "pacientes": cnt,
                "porcentaje": round(cnt / arauco_total * 100, 1),
                "lat": coords[0], "lng": coords[1],
            })

    # ── Direcciones geocodificadas ──
    dir_groups = {}
    for pid in pac_ids_en_rango:
        p = pac_map.get(pid)
        if not p or not p.get("direccion") or not p["direccion"].strip():
            continue
        key = p["direccion"].strip().upper()
        if key not in dir_groups:
            dir_groups[key] = {"dir": p["direccion"], "comuna": p.get("comuna", ""), "pacs": []}
        nombre = f"{p.get('nombre', '')} {p.get('apellidos', '')}".strip()
        ats = atenciones_por_pac.get(pid, [])[:5]
        dir_groups[key]["pacs"].append({"nombre": nombre, "citas": len(ats), "ats": ats})

    # Leer geocode_cache
    geo_cache = {}
    try:
        for row in conn.execute("SELECT direccion_key, lat, lng FROM geocode_cache").fetchall():
            geo_cache[row["direccion_key"]] = (row["lat"], row["lng"])
    except Exception:
        pass

    dirs_out = []
    for key, info in dir_groups.items():
        coords = geo_cache.get(key)
        if not coords:
            continue
        lat, lng = coords
        if not (-39.0 < lat < -36.0 and -74.0 < lng < -71.0):
            continue
        total_citas_dir = sum(p["citas"] for p in info["pacs"])
        detalle = []
        for p in info["pacs"][:8]:
            det = {"n": p["nombre"], "c": p["citas"], "a": []}
            for at in p["ats"][:5]:
                f = at["fecha"]
                if f and len(f) >= 10:
                    f = f[8:10] + "/" + f[5:7]
                prof = at["prof"].split()
                prof_short = " ".join(prof[:2]) if len(prof) >= 2 else at["prof"]
                det["a"].append(f"{f} {at['hora']} — {prof_short}")
            detalle.append(det)
        dirs_out.append({
            "lat": round(lat, 4), "lng": round(lng, 4),
            "d": info["dir"].strip(), "p": len(info["pacs"]),
            "c": total_citas_dir, "det": detalle,
        })

    conn.close()

    return {
        "fecha_min": fecha_min, "fecha_max": fecha_max,
        "filtro": {"desde": f_desde, "hasta": f_hasta},
        "total_citas": len(citas_rango),
        "pacientes_unicos": len(pac_ids_en_rango),
        "comunas": comunas_out,
        "localidades": locs_out,
        "direcciones": dirs_out,
    }


# ── Patient files (media recibido) ───────────────────────────────────────────

@router.get("/admin/api/patient-files/{phone}")
def api_patient_files(phone: str, _=Depends(require_admin)):
    """Lista archivos recibidos de un paciente."""
    return get_patient_files(phone)


@router.get("/admin/api/file/{file_id}")
def api_serve_file(file_id: int, _=Depends(require_admin)):
    """Sirve un archivo almacenado por ID."""
    from session import _conn
    from pathlib import Path
    with _conn() as conn:
        row = conn.execute(
            "SELECT file_path, mime_type, filename FROM patient_files WHERE id=?",
            (file_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Archivo no encontrado")
    fpath = Path(__file__).parent.parent / row["file_path"]
    if not fpath.exists():
        raise HTTPException(404, "Archivo eliminado del disco")
    content = fpath.read_bytes()
    mime = row["mime_type"] or "application/octet-stream"
    headers = {"Content-Disposition": f'inline; filename="{row["filename"]}"'}
    return Response(content=content, media_type=mime, headers=headers)


# ── Media stats (image counter) ──────────────────────────────────────────────

@router.get("/admin/api/media-stats")
def api_media_stats(_=Depends(require_admin)):
    """Estadísticas de archivos media recibidos — historial completo."""
    return get_media_stats()


# ── Demanda no disponible ────────────────────────────────────────────────────

@router.get("/admin/api/demanda-no-disponible")
def api_demanda_no_disponible(dias: int = Query(90, ge=1, le=365),
                               _=Depends(require_admin)):
    """Lista demanda de especialistas/exámenes que no tenemos."""
    return get_demanda_no_disponible(dias)


# ── Conversion funnel por especialidad ──────────────────────────────────────

@router.get("/admin/api/conversion-funnel")
def api_conversion_funnel(dias: int = Query(30, ge=1, le=365),
                          _=Depends(require_admin)):
    """Tasa de conversión agendar→confirmar por especialidad.

    Úsalo para decidir dónde invertir marketing (alta demanda + baja conversión
    = problema de UX o disponibilidad) y detectar especialidades que sangran.
    """
    return get_conversion_funnel_by_especialidad(dias)


# ── Reagendar 1-click tras cancelación del doctor ───────────────────────────

# ── Marcar conversación como vista por el admin ────────────────────────────

@router.post("/admin/api/conversation/{phone}/mark-seen")
async def api_mark_seen(phone: str, _=Depends(require_admin)):
    """Marca la conversación como vista ahora. Limpia badges de no leídos."""
    mark_admin_seen(phone, seen_by="admin")
    return {"ok": True, "phone": phone}


@router.get("/admin/api/conversation/{phone}/last-seen")
def api_last_seen(phone: str, _=Depends(require_admin)):
    """Retorna el timestamp en que se marcó por última vez como visto.
    Si nunca se marcó, retorna None. Útil para renderizar separador
    '↓ Mensajes nuevos ↓' justo antes del primer msg posterior."""
    from session import _conn
    with _conn() as c:
        row = c.execute("SELECT seen_at FROM admin_seen WHERE phone=?", (phone,)).fetchone()
        return {"phone": phone, "last_seen_at": row["seen_at"] if row else None}


@router.get("/admin/api/unread-counts")
def api_unread_counts(_=Depends(require_admin)):
    """Retorna mapa {phone: cantidad} de mensajes inbound no leídos por phone."""
    return get_unread_counts()


# ── Web Push (notificaciones nativas a la PWA admin) ───────────────────────

@router.get("/admin/api/push/vapid-key")
def api_push_vapid_key(_=Depends(require_admin)):
    """Retorna la clave pública VAPID para que el browser se suscriba."""
    import push as _push
    return {"public_key": _push.VAPID_PUBLIC_KEY}


@router.post("/admin/api/push/subscribe")
async def api_push_subscribe(request: Request, _=Depends(require_admin)):
    """Guarda una suscripción del browser. Body: {subscription:{...}, label:"..."}"""
    import push as _push
    body = await request.json()
    sub = body.get("subscription") or {}
    label = (body.get("label") or "").strip()[:80]
    user_agent = request.headers.get("user-agent", "")[:200]
    try:
        sid = _push.save_subscription(sub, role="admin", label=label, user_agent=user_agent)
        return {"ok": True, "id": sid}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.post("/admin/api/push/unsubscribe")
async def api_push_unsubscribe(request: Request, _=Depends(require_admin)):
    """Elimina una suscripción. Body: {endpoint:"..."}"""
    import push as _push
    body = await request.json()
    endpoint = (body.get("endpoint") or "").strip()
    if not endpoint:
        return JSONResponse({"ok": False, "error": "endpoint requerido"}, status_code=400)
    removed = _push.delete_subscription(endpoint)
    return {"ok": True, "removed": removed}


@router.post("/admin/api/push/test")
async def api_push_test(_=Depends(require_admin)):
    """Envía un push de prueba a todas las suscripciones admin."""
    import push as _push
    stats = _push.send_to_role(
        "admin",
        title="Test CMC",
        body="Si ves esto, las notificaciones funcionan correctamente.",
        url="/admin/v2",
        badge=_push.count_unread_conversations(),
        tag="cmc-test",
    )
    return stats


# ── Marcar paciente como agendado manualmente ──────────────────────────────

@router.post("/admin/api/patient/{phone}/mark-booked")
async def api_mark_booked(phone: str, request: Request, _=Depends(require_admin)):
    """Registra cita agendada manualmente (por teléfono o presencial).

    Body: {"especialidad":..., "profesional":..., "fecha":"YYYY-MM-DD",
           "hora":"HH:MM", "modalidad":"fonasa|particular"}

    Guarda cita en citas_bot + state=COMPLETED + tag "agendado-manual".
    El bot deja de perseguir al paciente con reenganche.
    """
    body = await request.json()
    esp = (body.get("especialidad") or "").strip()
    prof = (body.get("profesional") or "").strip()
    fecha = (body.get("fecha") or "").strip()
    hora = (body.get("hora") or "").strip()
    rut = (body.get("rut") or "").strip()
    nombre = (body.get("nombre") or "").strip()
    if not esp or not fecha or not hora:
        raise HTTPException(status_code=400, detail="especialidad, fecha y hora son obligatorios")
    from session import save_cita_bot, save_session, save_tag, log_event, save_profile, get_profile
    import time as _time
    id_cita = body.get("id_cita") or f"manual-{phone}-{int(_time.time())}"
    save_cita_bot(
        phone=phone, id_cita=id_cita, especialidad=esp, profesional=prof,
        fecha=fecha, hora=hora, modalidad=(body.get("modalidad") or "manual"),
    )
    # Asociar RUT + nombre al teléfono si vinieron en el body
    if rut or nombre:
        existing = get_profile(phone) or {}
        rut_final = rut or (existing.get("rut") or "")
        nombre_final = nombre or (existing.get("nombre") or "")
        save_profile(phone, rut_final, nombre_final)
    save_session(phone, "COMPLETED", {})
    save_tag(phone, "agendado-manual")
    log_event(phone, "agendado_manual", {
        "especialidad": esp, "profesional": prof, "fecha": fecha, "hora": hora,
        "id_cita": id_cita, "rut": rut, "nombre": nombre,
    })
    return {"ok": True, "phone": phone, "id_cita": id_cita,
            "cita": {"especialidad": esp, "profesional": prof, "fecha": fecha, "hora": hora}}


@router.post("/admin/api/cita/{id_cita}/cancel-doctor")
async def api_cancel_by_doctor(id_cita: str, _=Depends(require_admin)):
    """Notifica al paciente que su cita fue cancelada por el profesional y
    le envía 3 slots alternativos pre-cargados en WhatsApp (1-click reagendar).

    Uso: recepción llama este endpoint tras cancelar la cita en Medilink
    (cuando la causa es el profesional, no el paciente).
    """
    from jobs import enviar_reagendar_por_cancelacion
    result = await enviar_reagendar_por_cancelacion(id_cita, motivo="doctor_cancel")
    if not result.get("ok"):
        reason = result.get("reason", "error")
        if reason == "cita_no_encontrada":
            raise HTTPException(status_code=404, detail="Cita no encontrada en citas_bot")
        if reason == "ya_notificado":
            raise HTTPException(status_code=409, detail="Paciente ya fue notificado")
        raise HTTPException(status_code=400, detail=reason)
    return result


# ── Ley 19.628: consent + derecho al olvido ──────────────────────────────────

@router.get("/admin/api/privacy/consent/{phone}")
def api_get_consent(phone: str, _=Depends(require_admin)):
    """Retorna el registro de consentimiento del paciente (para auditoría)."""
    phone_clean = phone.lstrip("+").strip()
    rec = get_privacy_consent(phone_clean)
    return {"phone": phone_clean, "consent": rec}


@router.post("/admin/api/privacy/consent/{phone}")
def api_set_consent(phone: str, status: str = Query(..., pattern="^(accepted|declined|pending)$"),
                    _=Depends(require_admin)):
    """Registra manualmente un consent (por ej. recibido por WhatsApp tradicional
    o por teléfono). `method=admin` queda en el registro."""
    phone_clean = phone.lstrip("+").strip()
    save_privacy_consent(phone_clean, status=status, method="admin")
    log_event(phone_clean, "privacy_consent_admin_set", {"status": status})
    return {"phone": phone_clean, "status": status}


@router.delete("/admin/api/patient")
async def api_delete_patient(rut: str | None = Query(None),
                             phone: str | None = Query(None),
                             id_paciente_medilink: int | None = Query(None),
                             _=Depends(require_admin)):
    """Derecho al olvido (Ley 19.628 art. 12). Borra en cascada todos los
    datos del paciente en nuestras tablas + archivos físicos. Registra el
    evento en `gdpr_deletions` (inmutable).

    Requiere uno de: `rut`, `phone`. Si provees `id_paciente_medilink`
    también borra caches de citas/ortodoncia/kine.

    **Atención**: este borrado NO afecta Medilink. Para borrar datos clínicos
    allí, debes contactar al proveedor (healthatom).
    """
    if not rut and not phone:
        raise HTTPException(400, "Debes proveer rut o phone.")
    phone_clean = phone.lstrip("+").strip() if phone else None
    rut_clean = rut.strip().replace(".", "").lower() if rut else None

    # Si solo tenemos rut, intentamos resolver id_paciente en Medilink
    # (best-effort; no falla si Medilink está caído).
    if rut_clean and not id_paciente_medilink:
        try:
            pac = await buscar_paciente(rut_clean)
            if pac and pac.get("id"):
                id_paciente_medilink = int(pac["id"])
        except Exception as e:
            log.warning("No pude resolver id_paciente Medilink para rut=%s: %s",
                        rut_clean, e)

    try:
        summary = delete_patient_data(
            phone=phone_clean,
            rut=rut_clean,
            id_paciente_medilink=id_paciente_medilink,
            deleted_by="admin",
        )
    except Exception as e:
        log.exception("Error en delete_patient_data")
        raise HTTPException(500, f"Error borrando datos: {e}")

    return {
        "ok": True,
        "rut": rut_clean,
        "phone": phone_clean,
        "id_paciente_medilink": id_paciente_medilink,
        "summary": summary,
        "nota": "Datos en Medilink NO fueron borrados. Contacta healthatom por separado.",
    }


@router.get("/admin/api/privacy/deletions")
def api_list_deletions(limit: int = Query(100, ge=1, le=500),
                       _=Depends(require_admin)):
    """Audit log de borrados ejecutados (tabla `gdpr_deletions`, inmutable)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, rut, phone, deleted_at, deleted_by, summary "
            "FROM gdpr_deletions ORDER BY deleted_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Dashboard de ahorros (savings tracker) ──────────────────────────────────

_PRICE_CLAUDE_INTENT_USD     = 0.0011
_PRICE_TEMPLATE_UTILITY_USD  = 0.0075
_PRICE_WHISPER_SHORT_USD     = 0.0003
_USD_TO_CLP = 950

_SAVINGS_EVENTS = {
    "savings:intent_cache_hit":                    _PRICE_CLAUDE_INTENT_USD,
    "savings:skip_reminder_2h_service_window":     _PRICE_TEMPLATE_UTILITY_USD,
    "savings:skip_reminder_2h_medilink_confirmed": _PRICE_TEMPLATE_UTILITY_USD,
    "savings:skip_whisper_short_audio":            _PRICE_WHISPER_SHORT_USD,
}


@router.get("/admin/api/savings")
def api_savings(period: str = Query("today", pattern="^(today|7d|30d)$"),
                _=Depends(require_admin)):
    """Agrega eventos 'savings:*' en conversation_events y calcula ahorro estimado.
    Period: today | 7d | 30d."""
    where_ts = {
        "today": "ts >= datetime('now', 'start of day')",
        "7d":    "ts >= datetime('now', '-7 days')",
        "30d":   "ts >= datetime('now', '-30 days')",
    }[period]

    counts = {e.split(":")[1]: 0 for e in _SAVINGS_EVENTS}
    usd_by_category = {k: 0.0 for k in counts}

    with _conn() as conn:
        qmarks = ",".join("?" for _ in _SAVINGS_EVENTS)
        rows = conn.execute(
            f"SELECT event, COUNT(*) AS n FROM conversation_events "
            f"WHERE {where_ts} AND event IN ({qmarks}) "
            f"GROUP BY event",
            list(_SAVINGS_EVENTS.keys())
        ).fetchall()

    total_usd = 0.0
    for r in rows:
        ev = r["event"]
        n = int(r["n"] or 0)
        key = ev.split(":")[1]
        counts[key] = n
        subtotal = n * _SAVINGS_EVENTS.get(ev, 0.0)
        usd_by_category[key] = round(subtotal, 4)
        total_usd += subtotal

    return {
        "period":          period,
        "counts":          counts,
        "usd_by_category": usd_by_category,
        "total_usd":       round(total_usd, 3),
        "total_clp":       int(round(total_usd * _USD_TO_CLP)),
        "prices_usd": {
            "claude_intent":    _PRICE_CLAUDE_INTENT_USD,
            "template_utility": _PRICE_TEMPLATE_UTILITY_USD,
            "whisper_short":    _PRICE_WHISPER_SHORT_USD,
        },
    }


# ── AGENDA HOME: waiting room + estado local + undo ──────────────────────────
import json as _json


def _agenda_conn():
    """Usa el mismo helper que session.py para compatibilidad con SQLCipher."""
    from session import _conn as _session_conn  # type: ignore
    return _session_conn()


_AGENDA_TABLES_READY = False


def _ensure_agenda_tables():
    global _AGENDA_TABLES_READY
    if _AGENDA_TABLES_READY:
        return
    conn = _agenda_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cita_estados_local (
                id_cita INTEGER PRIMARY KEY,
                estado TEXT,
                ts INTEGER,
                operador TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS waiting_room (
                id_cita INTEGER PRIMARY KEY,
                phone TEXT,
                nombre TEXT,
                especialidad TEXT,
                profesional TEXT,
                llegada_ts INTEGER,
                prioridad INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS undo_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                op TEXT,
                payload TEXT,
                ts INTEGER,
                expires_ts INTEGER,
                applied INTEGER DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_undo_expires ON undo_snapshots(expires_ts)")
        conn.commit()
        _AGENDA_TABLES_READY = True
    finally:
        conn.close()


_ESTADO_MAP = {
    "": "pendiente",
    "pendiente": "pendiente",
    "solicitada": "pendiente",
    "confirmada": "confirmada",
    "confirmado": "confirmada",
    "asistira": "confirmada",
    "asistirá": "confirmada",
    "asiste": "en_consulta",
    "en consulta": "en_consulta",
    "en_consulta": "en_consulta",
    "en sala": "en_consulta",
    "atendida": "atendida",
    "atendido": "atendida",
    "asistio": "atendida",
    "asistió": "atendida",
    "no asistio": "no_asistio",
    "no asistió": "no_asistio",
    "no_asistio": "no_asistio",
    "ausente": "no_asistio",
    "anulada": "cancelada",
    "anulado": "cancelada",
    "cancelada": "cancelada",
    "cancelado": "cancelada",
}


def _normalize_estado(raw: str) -> str:
    if not raw:
        return "pendiente"
    k = str(raw).strip().lower()
    return _ESTADO_MAP.get(k, "pendiente")


def _bulk_estados_locales(ids):
    if not ids:
        return {}
    _ensure_agenda_tables()
    conn = _agenda_conn()
    try:
        placeholders = ",".join(["?"] * len(ids))
        rows = conn.execute(
            f"SELECT id_cita, estado FROM cita_estados_local WHERE id_cita IN ({placeholders})",
            ids
        ).fetchall()
        return {int(r["id_cita"]): r["estado"] for r in rows}
    finally:
        conn.close()


def _get_wa_status(phone: str) -> str:
    if not phone:
        return "no_enviado"
    try:
        conn = _agenda_conn()
        try:
            cutoff = int(time.time()) - 48 * 3600
            r = conn.execute(
                "SELECT status, ts FROM message_statuses WHERE phone=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                (phone, cutoff)
            ).fetchone()
            if not r:
                return "no_enviado"
            status = (r["status"] or "").lower()
            last_status_ts = int(r["ts"] or 0)
            try:
                ri = conn.execute(
                    "SELECT ts FROM messages WHERE phone=? AND direction='in' ORDER BY ts DESC LIMIT 1",
                    (phone,)
                ).fetchone()
                if ri and int(ri["ts"] or 0) > last_status_ts:
                    return "respondido"
            except Exception:
                pass
            if status == "read":
                return "leido"
            if status == "delivered":
                return "entregado"
            if status == "sent":
                return "enviado"
            if status == "failed":
                return "no_enviado"
            return "enviado"
        finally:
            conn.close()
    except Exception as e:
        log.debug("_get_wa_status err: %s", e)
        return "no_enviado"


def _create_undo_snapshot(op: str, payload: dict, ttl_s: int = 15) -> int:
    _ensure_agenda_tables()
    conn = _agenda_conn()
    try:
        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO undo_snapshots (op, payload, ts, expires_ts) VALUES (?, ?, ?, ?)",
            (op, _json.dumps(payload, default=str), now, now + int(ttl_s))
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


@router.post("/admin/api/cita/{id_cita}/estado")
async def admin_cita_set_estado(id_cita: int, request: Request, _: str = Depends(require_admin)):
    """Guarda un override local del estado de la cita (no se propaga a Medilink)."""
    body = await request.json()
    nuevo = (body.get("estado") or "").strip().lower()
    if nuevo not in ("pendiente", "confirmada", "en_consulta", "atendida", "no_asistio", "cancelada"):
        raise HTTPException(status_code=400, detail="estado inválido")
    operador = body.get("operador", "admin")
    _ensure_agenda_tables()
    conn = _agenda_conn()
    try:
        conn.execute(
            "INSERT INTO cita_estados_local (id_cita, estado, ts, operador) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id_cita) DO UPDATE SET estado=excluded.estado, ts=excluded.ts, operador=excluded.operador",
            (int(id_cita), nuevo, int(time.time()), operador)
        )
        conn.commit()
    finally:
        conn.close()
    log_event("admin", "cita_estado_local", {"id_cita": id_cita, "estado": nuevo})
    return {"ok": True, "id_cita": id_cita, "estado": nuevo}


@router.get("/admin/api/waiting-room")
def admin_waiting_room(_: str = Depends(require_admin)):
    """Lista pacientes en sala de espera."""
    _ensure_agenda_tables()
    conn = _agenda_conn()
    try:
        rows = conn.execute(
            "SELECT id_cita, phone, nombre, especialidad, profesional, llegada_ts, prioridad "
            "FROM waiting_room ORDER BY prioridad DESC, llegada_ts ASC"
        ).fetchall()
        now = int(time.time())
        items = []
        for r in rows:
            llegada = int(r["llegada_ts"] or 0)
            espera_min = max(0, (now - llegada) // 60) if llegada else 0
            items.append({
                "id_cita": r["id_cita"],
                "phone": r["phone"] or "",
                "nombre": r["nombre"] or "",
                "especialidad": r["especialidad"] or "",
                "profesional": r["profesional"] or "",
                "llegada_ts": llegada,
                "prioridad": int(r["prioridad"] or 0),
                "espera_min": int(espera_min),
            })
        return {"items": items}
    finally:
        conn.close()


@router.post("/admin/api/waiting-room/arrive")
async def admin_waiting_room_arrive(request: Request, _: str = Depends(require_admin)):
    """Agrega o actualiza un paciente en la sala de espera."""
    body = await request.json()
    id_cita = int(body.get("id_cita") or 0)
    if not id_cita:
        raise HTTPException(status_code=400, detail="id_cita requerido")
    _ensure_agenda_tables()
    conn = _agenda_conn()
    try:
        conn.execute(
            "INSERT INTO waiting_room (id_cita, phone, nombre, especialidad, profesional, llegada_ts, prioridad) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id_cita) DO UPDATE SET phone=excluded.phone, nombre=excluded.nombre, "
            "especialidad=excluded.especialidad, profesional=excluded.profesional, "
            "prioridad=excluded.prioridad",
            (
                id_cita,
                body.get("phone", "") or "",
                body.get("nombre", "") or "",
                body.get("especialidad", "") or "",
                body.get("profesional", "") or "",
                int(time.time()),
                int(body.get("prioridad") or 0),
            )
        )
        conn.commit()
    finally:
        conn.close()
    log_event("admin", "waiting_room_arrive", {"id_cita": id_cita})
    return {"ok": True, "id_cita": id_cita}


@router.delete("/admin/api/waiting-room/{id_cita}")
def admin_waiting_room_remove(id_cita: int, _: str = Depends(require_admin)):
    """Retira un paciente de la sala de espera con snapshot undo."""
    _ensure_agenda_tables()
    conn = _agenda_conn()
    try:
        row = conn.execute(
            "SELECT id_cita, phone, nombre, especialidad, profesional, llegada_ts, prioridad "
            "FROM waiting_room WHERE id_cita=?",
            (int(id_cita),)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No está en sala")
        snapshot_id = _create_undo_snapshot("waiting_remove", dict(row), ttl_s=30)
        conn.execute("DELETE FROM waiting_room WHERE id_cita=?", (int(id_cita),))
        conn.commit()
    finally:
        conn.close()
    log_event("admin", "waiting_room_remove", {"id_cita": id_cita, "snapshot": snapshot_id})
    return {"ok": True, "snapshot_id": snapshot_id}


@router.post("/admin/api/undo/{snapshot_id}")
def admin_undo(snapshot_id: int, _: str = Depends(require_admin)):
    """Restaura una acción desde snapshot (soporta waiting_remove)."""
    _ensure_agenda_tables()
    conn = _agenda_conn()
    try:
        row = conn.execute(
            "SELECT id, op, payload, expires_ts, applied FROM undo_snapshots WHERE id=?",
            (int(snapshot_id),)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="snapshot no encontrado")
        if int(row["applied"] or 0) == 1:
            raise HTTPException(status_code=410, detail="snapshot ya aplicado")
        if int(row["expires_ts"] or 0) < int(time.time()):
            raise HTTPException(status_code=410, detail="snapshot expirado")
        payload = _json.loads(row["payload"] or "{}")
        op = row["op"]
        if op == "waiting_remove":
            conn.execute(
                "INSERT INTO waiting_room (id_cita, phone, nombre, especialidad, profesional, llegada_ts, prioridad) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id_cita) DO UPDATE SET phone=excluded.phone, nombre=excluded.nombre",
                (
                    int(payload.get("id_cita") or 0),
                    payload.get("phone", "") or "",
                    payload.get("nombre", "") or "",
                    payload.get("especialidad", "") or "",
                    payload.get("profesional", "") or "",
                    int(payload.get("llegada_ts") or time.time()),
                    int(payload.get("prioridad") or 0),
                )
            )
        else:
            raise HTTPException(status_code=400, detail=f"op {op} no soportada")
        conn.execute("UPDATE undo_snapshots SET applied=1 WHERE id=?", (int(snapshot_id),))
        conn.commit()
    finally:
        conn.close()
    log_event("admin", "undo_applied", {"snapshot_id": snapshot_id, "op": op})
    return {"ok": True, "snapshot_id": snapshot_id, "op": op}


try:
    _ensure_agenda_tables()
except Exception as _e:
    log.warning("No se pudieron inicializar tablas admin: %s", _e)


# ---------------------------------------------------------------------------
# Pediátrico: PNI + hitos del desarrollo (consumido desde GES)
# ---------------------------------------------------------------------------
from pni import get_vaccine_reminder
from hitos_desarrollo import get_milestones_reminder


@router.post("/api/send-pediatric-info")
async def admin_send_pediatric_info(request: Request, _: str = Depends(require_admin)):
    """Envía recordatorio PNI y/o hitos del desarrollo a un teléfono.
    Body: {phone, birthdate, name?, include: 'pni'|'milestones'|'both'}.
    Llamado desde la app GES."""
    payload = await request.json()
    phone = (payload.get("phone") or "").strip()
    birthdate = (payload.get("birthdate") or "").strip()
    name = (payload.get("name") or "").strip()
    include = (payload.get("include") or "both").lower()

    if not phone or not birthdate:
        raise HTTPException(400, "phone y birthdate son requeridos")
    if include not in ("pni", "milestones", "both"):
        raise HTTPException(400, "include debe ser 'pni', 'milestones' o 'both'")

    partes = []
    if include in ("pni", "both"):
        msg = get_vaccine_reminder(birthdate, name)
        if msg:
            partes.append(msg)
    if include in ("milestones", "both"):
        msg = get_milestones_reminder(birthdate, name)
        if msg:
            partes.append(msg)

    if not partes:
        return {"sent": False, "reason": "Paciente fuera del rango pediátrico o sin contenido aplicable"}

    texto = "\n\n".join(partes)
    try:
        wamid = await send_whatsapp(phone, texto)
    except Exception as e:
        log.error("send-pediatric-info falló para %s: %s", phone, e)
        raise HTTPException(502, f"No se pudo enviar WhatsApp: {e}")

    log_event(phone, "pediatric_info_sent", {"include": include, "source": "ges"})
    return {"sent": True, "wamid": wamid, "include": include, "chars": len(texto)}


# ── FIX-3: Staff whitelist endpoints ────────────────────────────────────────

@router.get("/admin/api/staff")
async def api_get_staff(_auth=Depends(require_admin)):
    """Lista los números en la whitelist de staff."""
    from staff_whitelist import get_all_staff
    return {"staff": get_all_staff()}


@router.post("/admin/api/staff/add")
async def api_add_staff(request: Request, _auth=Depends(require_admin)):
    """Agrega un número al whitelist de staff (runtime, volátil).
    Para persistir, agregar a STAFF_PHONES en .env del servidor.
    Body: {"phone": "56XXXXXXXXX", "nombre": "Dr. Nombre"}
    """
    body = await request.json()
    phone_raw = str(body.get("phone", "")).strip().lstrip("+")
    nombre = str(body.get("nombre", "")).strip()
    if not phone_raw or not nombre:
        raise HTTPException(400, "Se requieren 'phone' y 'nombre'")
    from staff_whitelist import add_staff_runtime
    add_staff_runtime(phone_raw, nombre)
    return {"ok": True, "phone": phone_raw, "nombre": nombre}


@router.delete("/admin/api/staff/{phone}")
async def api_remove_staff(phone: str, _auth=Depends(require_admin)):
    """Elimina un número del whitelist runtime."""
    from staff_whitelist import remove_staff_runtime
    removed = remove_staff_runtime(phone.lstrip("+"))
    return {"ok": True, "removed": removed}


# ── Meta CAPI endpoints ────────────────────────────────────────────────────────

@router.post("/admin/api/capi/test")
async def capi_test(_auth=Depends(require_admin)):
    """Envía un evento de prueba a Meta CAPI.
    Retorna {"skipped": "no_pixel_id"} si META_PIXEL_ID no está configurado.
    """
    import meta_capi
    result = await meta_capi.send_event(
        "Schedule",
        phone="+56966610737",
        first_name="Test",
        last_name="CAPI",
        email="test@cmc.cl",
        custom_data={"test": True, "content_name": "test_endpoint"},
    )
    return result


@router.get("/admin/api/capi/stats")
def capi_stats(dias: int = 7, _auth=Depends(require_admin)):
    """Estadísticas de eventos CAPI enviados.

    Filtra conversation_events buscando eventos cuyo nombre empieza con 'capi_'
    o es exactamente 'fbclid_captured'.
    Devuelve conteos por evento y los últimos 20 registros.
    """
    from session import _conn
    import json as _json_capi
    try:
        con = _conn()
        cur = con.cursor()
        # Conteos por tipo de evento en los últimos N días
        cur.execute(
            """
            SELECT event, COUNT(*) as total
            FROM conversation_events
            WHERE (event LIKE 'capi_%' OR event = 'fbclid_captured')
              AND ts >= datetime('now', ? || ' days')
            GROUP BY event
            ORDER BY total DESC
            """,
            (f"-{dias}",),
        )
        conteos = {row[0]: row[1] for row in cur.fetchall()}

        # Últimos 20 eventos
        cur.execute(
            """
            SELECT phone, event, meta, ts
            FROM conversation_events
            WHERE (event LIKE 'capi_%' OR event = 'fbclid_captured')
              AND ts >= datetime('now', ? || ' days')
            ORDER BY ts DESC
            LIMIT 20
            """,
            (f"-{dias}",),
        )
        rows = cur.fetchall()
        ultimos = []
        for row in rows:
            try:
                d = _json_capi.loads(row[2]) if row[2] else {}
            except Exception:
                d = {"raw": str(row[2])[:100]}
            ultimos.append({
                "phone": row[0][:6] + "XXXXX",  # ofuscar
                "event": row[1],
                "data": d,
                "ts": row[3],
            })

        from config import META_PIXEL_ID
        return {
            "dias": dias,
            "pixel_configurado": bool(META_PIXEL_ID),
            "conteos": conteos,
            "total_eventos": sum(conteos.values()),
            "ultimos": ultimos,
        }
    except Exception as e:
        return {"error": str(e), "dias": dias}
