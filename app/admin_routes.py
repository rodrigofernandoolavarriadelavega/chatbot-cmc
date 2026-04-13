"""Admin panel API routes — all /admin/api/* endpoints."""
import asyncio
import hashlib
import hmac
import logging
import time
from datetime import date, timedelta
from collections import defaultdict

from fastapi import APIRouter, Request, Query, HTTPException, Header, Depends, Cookie, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from config import ADMIN_TOKEN, ORTODONCIA_TOKEN, COOKIE_SECRET
from messaging import send_whatsapp, send_instagram, send_messenger
from session import (get_session, reset_session, save_session, get_metricas,
                     log_message, get_messages, get_conversations, log_event,
                     get_tags, save_tag, delete_tag, search_messages,
                     get_kine_tracking_all, save_kine_tracking,
                     get_ortodoncia_pacientes, set_ortodoncia_tipo, get_ortodoncia_sync_max_fecha,
                     get_waitlist_all, cancel_waitlist,
                     get_confirmaciones_dia, get_citas_cache_todos,
                     get_metricas_fidelizacion)
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
        if tk == ADMIN_TOKEN:
            return tk
    # 2. Cookie
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role == "admin":
            return ADMIN_TOKEN
    # 3. Query param (backwards compat)
    if token and token == ADMIN_TOKEN:
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
        if tk in (ORTODONCIA_TOKEN, ADMIN_TOKEN):
            return tk
    # 2. Cookie
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return ORTODONCIA_TOKEN if role == "ortodoncia" else ADMIN_TOKEN
    # 3. Query param (backwards compat)
    if token and token in (ORTODONCIA_TOKEN, ADMIN_TOKEN):
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


@router.post("/admin/login")
def admin_login(request: Request, password: str = Form(...)):
    """Valida la contrasena y setea la cookie de sesion."""
    is_https = (request.url.scheme == "https"
                or request.headers.get("x-forwarded-proto") == "https")

    if password == ADMIN_TOKEN:
        response = RedirectResponse(url="/admin", status_code=302)
        _set_session_cookie(response, "admin", is_https)
        log.info("Admin login OK (cookie set) ip=%s",
                 request.client.host if request.client else "?")
        return response

    if password == ORTODONCIA_TOKEN:
        response = RedirectResponse(url="/admin", status_code=302)
        _set_session_cookie(response, "ortodoncia", is_https)
        log.info("Ortodoncia login OK (cookie set) ip=%s",
                 request.client.host if request.client else "?")
        return response

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
    return get_conversations()


@router.get("/admin/api/conversations/{phone}")
def admin_conversation_detail(phone: str, _: str = Depends(require_admin)):
    return get_messages(phone)


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

    if phone.startswith("ig_"):
        igsid = phone[3:]
        await send_instagram(igsid, message)
        canal = "instagram"
    elif phone.startswith("fb_"):
        psid = phone[3:]
        await send_messenger(psid, message)
        canal = "messenger"
    else:
        await send_whatsapp(phone, message)
        canal = "whatsapp"

    state = get_session(phone).get("state", "HUMAN_TAKEOVER")
    log_message(phone, "out", f"[Recepcionista] {message}", state, canal=canal)
    log_event(phone, "recepcionista_respondio", {"mensaje": message[:200]})
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
        paciente = await crear_paciente(rut, nombre, apellidos)
        if not paciente:
            raise HTTPException(status_code=400, detail="No se pudo crear el paciente")

    cita = await crear_cita(paciente["id"], id_prof, fecha, hora_ini, hora_fin, duracion)
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
    citas = await listar_citas_paciente(paciente["id"])
    return {"paciente": paciente, "citas": citas}


@router.post("/admin/api/anular")
async def admin_anular_cita(request: Request, _: str = Depends(require_admin)):
    """Anula una cita por su ID de Medilink."""
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


@router.post("/admin/api/ortodoncia/sync")
async def admin_ortodoncia_sync(desde: str = "2025-01-01", hasta: str = None,
                                _: str = Depends(require_ortodoncia)):
    fin = hasta or date.today().isoformat()
    asyncio.create_task(sync_ortodoncia_rango(desde, fin))
    return {"ok": True, "desde": desde, "hasta": fin}
