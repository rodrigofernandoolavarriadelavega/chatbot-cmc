"""Portal del Paciente — herramientas de uso diario (2026-10-08, ronda 3).

1. Pastillero: remedios con horarios + archivo .ics con alarmas (RFC 5545).
2. Ficha de emergencia: alergias, enfermedades, contacto (datos sensibles,
   con consentimiento explícito antes de guardar).
3. "Qué me toca este año": guía preventiva por edad y sexo, construida desde
   las MISMAS tablas que usa el bot (autocuidado, doctor_alerts, pni).

Auth: mismo patrón que portal_routes — cookie portal_session + perfil activo.
Todo lo que es del paciente pasa por `_resolve_completo` (Ley 21.719: un
adulto representado sin verificar da 403). Los datos se guardan por RUT del
paciente activo, NUNCA por teléfono: dos pacientes que comparten celular no
ven lo del otro.

Ronda 4 (2026-10-08):
4. Tomas compartidas: "lo tomé" se guarda en el servidor (portal_tomas), con
   quién marcó y a qué hora, para que la hija y la mamá vean lo mismo.
5. Recordatorios honestos: un solo .ics por persona (UID estable + SEQUENCE)
   y suscripción de calendario por token secreto, revocable y de solo
   lectura (portal_cal_tokens; se guarda solo el hash del token).

Nada aquí da consejos de dosis ni interpreta resultados. Los avisos por
WhatsApp NO existen en esta ronda (interruptor PORTAL_REMEDIOS_AVISOS_WA,
apagado y sin uso).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import JSONResponse, Response

import session as _session
from portal_routes import _resolve_completo, DEMO_RUT, DEMO_FAMILY

log = logging.getLogger("bot.portal_herramientas")
router = APIRouter(tags=["portal-herramientas"])

_TZ = ZoneInfo("America/Santiago")
# Interruptor preparado para el futuro (avisos por WhatsApp). APAGADO: hoy no
# se lee en ningún flujo; el aviso vive solo en el calendario del teléfono.
PORTAL_REMEDIOS_AVISOS_WA = os.getenv("PORTAL_REMEDIOS_AVISOS_WA", "false").lower() == "true"

_MAX_REMEDIOS = 30
_MAX_HORARIOS = 6
_HORA_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DIAS_ICS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]   # índice = date.weekday()


# ═══ Tablas (aditivas, en sessions.db) ═════════════════════════════════════
_DDL_PATH: str | None = None


def _ensure_tablas() -> None:
    global _DDL_PATH
    actual = str(_session.DB_PATH)
    if _DDL_PATH == actual:
        return
    with _session.db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS portal_remedios (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                rut         TEXT NOT NULL,
                uid         TEXT NOT NULL UNIQUE,
                nombre      TEXT NOT NULL,
                dosis       TEXT,
                horarios    TEXT NOT NULL DEFAULT '[]',
                dias        TEXT NOT NULL DEFAULT '[]',
                quedan      REAL,
                por_toma    REAL,
                quedan_fecha TEXT,
                creado_por  TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_portal_remedios_rut ON portal_remedios(rut)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS portal_ficha_emergencia (
                rut          TEXT PRIMARY KEY,
                data_json    TEXT NOT NULL,
                consent_at   TEXT NOT NULL,
                consent_por  TEXT NOT NULL,
                updated_at   TEXT DEFAULT (datetime('now'))
            )""")
        # SEQUENCE del .ics: sube cada vez que el remedio cambia (RFC 5545 §3.8.7.4)
        try:
            c.execute("ALTER TABLE portal_remedios ADD COLUMN seq INTEGER DEFAULT 0")
        except Exception:
            pass   # ya existe
        # Tomas marcadas ("lo tomé"), compartidas entre quienes tienen acceso
        # COMPLETO al perfil. rut = paciente; marcado_por_rut = titular de la
        # sesión que marcó (puede ser el mismo paciente o un familiar).
        c.execute("""
            CREATE TABLE IF NOT EXISTS portal_tomas (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                rut                TEXT NOT NULL,
                remedio_id         INTEGER NOT NULL,
                fecha              TEXT NOT NULL,
                hora               TEXT NOT NULL,
                marcado_por_rut    TEXT NOT NULL DEFAULT '',
                marcado_por_nombre TEXT NOT NULL DEFAULT '',
                marcado_ts         TEXT NOT NULL,
                UNIQUE(rut, remedio_id, fecha, hora)
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_portal_tomas_rut ON portal_tomas(rut, fecha)")
        # Suscripción de calendario: solo el HASH del token (el token viaja una
        # vez al navegador). Revocable; solo lectura; sin RUT en la URL.
        c.execute("""
            CREATE TABLE IF NOT EXISTS portal_cal_tokens (
                token_hash   TEXT PRIMARY KEY,
                rut          TEXT NOT NULL,
                creado_por   TEXT NOT NULL,
                created_at   TEXT DEFAULT (datetime('now')),
                revoked_at   TEXT,
                last_access  TEXT,
                n_access     INTEGER DEFAULT 0
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_portal_cal_tokens_rut ON portal_cal_tokens(rut)")
        try:   # teléfono de quien creó el link: para re-verificar acceso COMPLETO en cada lectura
            c.execute("ALTER TABLE portal_cal_tokens ADD COLUMN creado_por_tel TEXT DEFAULT ''")
        except Exception:
            pass   # ya existe
    _DDL_PATH = actual


# ═══ Helpers de validación ═════════════════════════════════════════════════
def _txt(v, n: int) -> str:
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(v or "")).strip()
    return re.sub(r"\s{2,}", " ", s)[:n]


def _horarios(v) -> list[str]:
    if not isinstance(v, list):
        raise HTTPException(400, "Horarios inválidos")
    out = []
    for h in v:
        h = str(h or "").strip()[:5]
        if not _HORA_RE.match(h):
            raise HTTPException(400, "Hay una hora mal escrita. Use el formato 08:00.")
        if h not in out:
            out.append(h)
    if len(out) > _MAX_HORARIOS:
        raise HTTPException(400, f"Máximo {_MAX_HORARIOS} horarios por remedio.")
    return sorted(out)


def _dias(v) -> list[int]:
    """[] = todos los días. Si no, índices 0=lunes … 6=domingo."""
    if v in (None, "", "todos"):
        return []
    if not isinstance(v, list):
        raise HTTPException(400, "Días inválidos")
    out = sorted({int(d) for d in v if str(d).isdigit() and 0 <= int(d) <= 6})
    return [] if len(out) == 7 else out


def _num(v, lo: float, hi: float) -> float | None:
    if v in (None, ""):
        return None
    try:
        f = float(str(v).replace(",", "."))
    except ValueError:
        raise HTTPException(400, "Número inválido")
    if not (lo <= f <= hi):
        raise HTTPException(400, "Número fuera de rango")
    return f


def _fila_remedio(r) -> dict:
    d = dict(r)
    d["horarios"] = json.loads(d.get("horarios") or "[]")
    d["dias"] = json.loads(d.get("dias") or "[]")
    d.pop("rut", None)
    return d


def _es_demo(rut: str) -> bool:
    return rut == DEMO_RUT or rut in DEMO_FAMILY


# ═══ Pastillero ════════════════════════════════════════════════════════════
def listar_remedios(rut: str) -> list[dict]:
    _ensure_tablas()
    with _session.db() as c:
        rows = c.execute(
            "SELECT * FROM portal_remedios WHERE rut=? ORDER BY nombre COLLATE NOCASE", (rut,)
        ).fetchall()
    return [_fila_remedio(r) for r in rows]


@router.get("/portal/api/herramientas/remedios")
async def remedios_listar(portal_session: str | None = Cookie(None),
                          portal_active: str | None = Cookie(None)):
    _o, _op, rut, _p = await _resolve_completo(portal_session, portal_active)
    if _es_demo(rut):
        return {"ok": True, "demo": True, "remedios": []}
    return {"ok": True, "remedios": listar_remedios(rut)}


@router.post("/portal/api/herramientas/remedios")
async def remedios_guardar(request: Request,
                           portal_session: str | None = Cookie(None),
                           portal_active: str | None = Cookie(None)):
    owner_rut, owner_phone, rut, _p = await _resolve_completo(portal_session, portal_active)
    b = await request.json()
    nombre = _txt(b.get("nombre"), 80)
    if not nombre:
        raise HTTPException(400, "Escriba el nombre del remedio.")
    dosis = _txt(b.get("dosis"), 80)
    horarios = _horarios(b.get("horarios") or [])
    dias = _dias(b.get("dias"))
    quedan = _num(b.get("quedan"), 0, 5000)
    por_toma = _num(b.get("por_toma"), 0.25, 50)
    hoy = datetime.now(_TZ).strftime("%Y-%m-%d")
    if _es_demo(rut):
        return {"ok": True, "demo": True}
    _ensure_tablas()
    rid = b.get("id")
    with _session.db() as c:
        if rid:
            prev = c.execute("SELECT * FROM portal_remedios WHERE id=? AND rut=?",
                             (int(rid), rut)).fetchone()
            if not prev:
                raise HTTPException(404, "No encontramos ese remedio.")
            # "quedan" se re-fecha solo si la persona cambió el número
            qf = prev["quedan_fecha"] if (quedan == prev["quedan"]) else (hoy if quedan is not None else None)
            c.execute("""UPDATE portal_remedios SET nombre=?, dosis=?, horarios=?, dias=?,
                         quedan=?, por_toma=?, quedan_fecha=?, updated_at=datetime('now'),
                         seq=COALESCE(seq,0)+1
                         WHERE id=? AND rut=?""",
                      (nombre, dosis, json.dumps(horarios), json.dumps(dias), quedan,
                       por_toma, qf, int(rid), rut))
            new_id = int(rid)
        else:
            n = c.execute("SELECT COUNT(*) FROM portal_remedios WHERE rut=?", (rut,)).fetchone()[0]
            if n >= _MAX_REMEDIOS:
                raise HTTPException(400, f"Máximo {_MAX_REMEDIOS} remedios por persona.")
            cur = c.execute("""INSERT INTO portal_remedios
                    (rut, uid, nombre, dosis, horarios, dias, quedan, por_toma, quedan_fecha, creado_por)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (rut, secrets.token_hex(8), nombre, dosis, json.dumps(horarios),
                     json.dumps(dias), quedan, por_toma, hoy if quedan is not None else None,
                     owner_rut))
            new_id = cur.lastrowid
    try:
        _session.log_event(owner_phone, "portal_remedio_guardado",
                           {"propio": owner_rut == rut, "n_horarios": len(horarios)})
    except Exception:
        pass
    return {"ok": True, "id": new_id}


@router.delete("/portal/api/herramientas/remedios/{rid}")
async def remedios_borrar(rid: int, portal_session: str | None = Cookie(None),
                          portal_active: str | None = Cookie(None)):
    _o, _op, rut, _p = await _resolve_completo(portal_session, portal_active)
    if _es_demo(rut):
        return {"ok": True, "demo": True}
    _ensure_tablas()
    with _session.db() as c:
        cur = c.execute("DELETE FROM portal_remedios WHERE id=? AND rut=?", (rid, rut))
        if cur.rowcount:
            c.execute("DELETE FROM portal_tomas WHERE remedio_id=? AND rut=?", (rid, rut))
    if not cur.rowcount:
        raise HTTPException(404, "No encontramos ese remedio.")
    return {"ok": True}


# ── .ics (RFC 5545) ──────────────────────────────────────────────────────────
_VTIMEZONE = [
    # Chile continental desde 2023 (tzdata: Rule Chile 2023 max):
    # verano empieza el 1er domingo de sept. >= día 2, termina el 1er domingo de abril >= día 2.
    "BEGIN:VTIMEZONE", "TZID:America/Santiago",
    "BEGIN:STANDARD", "DTSTART:19700405T000000", "TZOFFSETFROM:-0300", "TZOFFSETTO:-0400",
    "TZNAME:-04", "RRULE:FREQ=YEARLY;BYMONTH=4;BYDAY=SU;BYMONTHDAY=2,3,4,5,6,7,8", "END:STANDARD",
    "BEGIN:DAYLIGHT", "DTSTART:19700906T000000", "TZOFFSETFROM:-0400", "TZOFFSETTO:-0300",
    "TZNAME:-03", "RRULE:FREQ=YEARLY;BYMONTH=9;BYDAY=SU;BYMONTHDAY=2,3,4,5,6,7,8", "END:DAYLIGHT",
    "END:VTIMEZONE",
]


def _ics_esc(s: str) -> str:
    return (str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """Plegado RFC 5545 §3.1: líneas de máx. 75 octetos, sin partir un carácter UTF-8."""
    out, cur, n = [], "", 0
    for ch in line:
        w = len(ch.encode("utf-8"))
        lim = 75 if not out else 74          # las de continuación llevan un espacio
        if n + w > lim:
            out.append(cur)
            cur, n = "", 0
        cur += ch
        n += w
    out.append(cur)
    return "\r\n ".join(out)


def uid_toma(rut: str, remedio_uid: str, hora: str) -> str:
    """UID estable por (paciente, remedio, hora): re-descargar actualiza en vez de duplicar
    (en calendarios que respetan UID, p. ej. Google Calendar al importar)."""
    h = hashlib.sha256(f"{rut}|{remedio_uid}|{hora}".encode()).hexdigest()[:20]
    return f"cmc-remedio-{h}@centromedicocarampangue.cl"


def construir_ics(rut: str, remedios: list[dict], nombre_persona: str = "",
                  ahora: datetime | None = None, publico: bool = False) -> str:
    """publico=True: versión para la suscripción por link (la puede leer
    cualquiera que tenga la URL) → SOLO nombre del remedio y hora: sin dosis,
    sin nombre de la persona, sin RUT, sin diagnósticos."""
    ahora = ahora or datetime.now(_TZ)
    stamp = ahora.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    hoy = ahora.date()
    cal = "Mis remedios" + (f" · {nombre_persona}" if nombre_persona and not publico else "")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0",
         "PRODID:-//Centro Medico Carampangue//Portal del Paciente//ES",
         "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
         f"X-WR-CALNAME:{_ics_esc(cal)}", "X-WR-TIMEZONE:America/Santiago"]
    if publico:
        # Sugerencia de refresco para calendarios suscritos (cada cliente decide)
        L += ["REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    L += _VTIMEZONE
    for r in remedios:
        dias = r.get("dias") or []
        for hora in r.get("horarios") or []:
            hh, mm = hora.split(":")
            # Primera toma: hoy si coincide con los días; si no, el próximo día válido
            d = hoy
            if dias:
                for _ in range(7):
                    if d.weekday() in dias:
                        break
                    d += timedelta(days=1)
            dt = f"{d.strftime('%Y%m%d')}T{hh}{mm}00"
            titulo = f"Tomar {r['nombre']}" + (f" ({r['dosis']})" if r.get("dosis") and not publico else "")
            rrule = ("RRULE:FREQ=WEEKLY;BYDAY=" + ",".join(_DIAS_ICS[i] for i in dias)
                     if dias else "RRULE:FREQ=DAILY")
            desc = ("Recordatorio que usted anotó en su portal. "
                    "Siga siempre la indicación de su médico. Centro Médico Carampangue.")
            L += ["BEGIN:VEVENT",
                  f"UID:{uid_toma(rut, r['uid'], hora)}",
                  f"SEQUENCE:{int(r.get('seq') or 0)}",
                  f"DTSTAMP:{stamp}",
                  f"DTSTART;TZID=America/Santiago:{dt}",
                  "DURATION:PT5M",
                  rrule,
                  f"SUMMARY:{_ics_esc(titulo)}",
                  f"DESCRIPTION:{_ics_esc(desc)}",
                  "TRANSP:TRANSPARENT",
                  "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:PT0S",
                  f"DESCRIPTION:{_ics_esc(titulo)}",
                  "END:VALARM",
                  "END:VEVENT"]
    L.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in L) + "\r\n"


@router.get("/portal/api/herramientas/remedios.ics")
async def remedios_ics(portal_session: str | None = Cookie(None),
                       portal_active: str | None = Cookie(None)):
    owner_rut, owner_phone, rut, _p = await _resolve_completo(portal_session, portal_active)
    rems = [r for r in ([] if _es_demo(rut) else listar_remedios(rut)) if r.get("horarios")]
    if not rems:
        raise HTTPException(404, "No hay remedios con horario para poner alarmas.")
    try:
        _session.log_event(owner_phone, "portal_remedios_ics", {"n": len(rems)})
    except Exception:
        pass
    return Response(construir_ics(rut, rems), media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="mis-remedios.ics"',
                             "Cache-Control": "no-store"})


# ── Suscripción de calendario (se actualiza sola, sin reimportar) ───────────
_CAL_RATE: dict[str, list[float]] = {}


def _cal_rate_ok(key: str, limit: int = 30, window_s: int = 3600) -> bool:
    import time as _t
    now = _t.monotonic()
    xs = [t for t in _CAL_RATE.get(key, []) if now - t < window_s]
    if len(xs) >= limit:
        _CAL_RATE[key] = xs
        return False
    xs.append(now)
    _CAL_RATE[key] = xs
    return True


def _hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def _base_url(request: Request) -> tuple[str, str]:
    """(https://host, host) respetando el proxy (nginx/Cloudflare)."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{host}", host


def _suscripcion_estado(rut: str) -> dict:
    _ensure_tablas()
    with _session.db() as c:
        r = c.execute("""SELECT created_at, last_access FROM portal_cal_tokens
                         WHERE rut=? AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1""",
                      (rut,)).fetchone()
    return {"activa": bool(r), "creada": r["created_at"] if r else None,
            "ultimo_uso": r["last_access"] if r else None}


@router.get("/portal/api/herramientas/calendario")
async def calendario_estado(portal_session: str | None = Cookie(None),
                            portal_active: str | None = Cookie(None)):
    _o, _op, rut, _p = await _resolve_completo(portal_session, portal_active)
    if _es_demo(rut):
        return {"ok": True, "demo": True, "activa": False}
    return {"ok": True, **_suscripcion_estado(rut)}


@router.post("/portal/api/herramientas/calendario")
async def calendario_crear(request: Request, portal_session: str | None = Cookie(None),
                           portal_active: str | None = Cookie(None)):
    """Crea un link secreto de suscripción para el perfil activo. Revoca los
    anteriores de ESTE perfil (un solo link vigente por persona): el link solo
    se muestra una vez, en el servidor queda el hash."""
    owner_rut, owner_phone, rut, _p = await _resolve_completo(portal_session, portal_active)
    if _es_demo(rut):
        return {"ok": True, "demo": True}
    _ensure_tablas()
    tok = secrets.token_urlsafe(32)          # 256 bits, 43 caracteres
    with _session.db() as c:
        c.execute("UPDATE portal_cal_tokens SET revoked_at=datetime('now') "
                  "WHERE rut=? AND revoked_at IS NULL", (rut,))
        c.execute("INSERT INTO portal_cal_tokens (token_hash, rut, creado_por, creado_por_tel) VALUES (?,?,?,?)",
                  (_hash_token(tok), rut, owner_rut, owner_phone or ""))
    base, host = _base_url(request)
    try:
        _session.log_event(owner_phone, "portal_calendario_suscripcion", {"propio": owner_rut == rut})
    except Exception:
        pass
    path = f"/portal/cal/{tok}.ics"
    return {"ok": True, "url": base + path, "webcal": f"webcal://{host}{path}"}


@router.delete("/portal/api/herramientas/calendario")
async def calendario_revocar(portal_session: str | None = Cookie(None),
                             portal_active: str | None = Cookie(None)):
    owner_rut, owner_phone, rut, _p = await _resolve_completo(portal_session, portal_active)
    if _es_demo(rut):
        return {"ok": True, "demo": True}
    _ensure_tablas()
    with _session.db() as c:
        cur = c.execute("UPDATE portal_cal_tokens SET revoked_at=datetime('now') "
                        "WHERE rut=? AND revoked_at IS NULL", (rut,))
    return {"ok": True, "revocados": cur.rowcount}


@router.get("/portal/cal/{token}.ics")
async def calendario_feed(token: str, request: Request):
    """Feed de solo lectura para el calendario del teléfono. Sin cookie: la
    llave es el token (256 bits). Devuelve SOLO nombre del remedio y hora.
    404 genérico ante token inválido, revocado o vínculo ya quitado."""
    # IP que puso nginx (X-Real-IP) o el ÚLTIMO salto de XFF: el primero lo
    # controla el cliente y permitiría saltarse el límite.
    xff = (request.headers.get("x-forwarded-for") or "").split(",")
    ip = ((request.headers.get("x-real-ip") or "").strip() or xff[-1].strip()
          or (request.client.host if request.client else ""))
    if not _cal_rate_ok("ip:" + ip, limit=120):
        raise HTTPException(429, "Demasiadas consultas")
    if not re.fullmatch(r"[A-Za-z0-9_-]{30,80}", token or ""):
        raise HTTPException(404, "No encontrado")
    h = _hash_token(token)
    if not _cal_rate_ok("tok:" + h, limit=30):
        raise HTTPException(429, "Demasiadas consultas")
    _ensure_tablas()
    with _session.db() as c:
        row = c.execute("SELECT rut, creado_por, creado_por_tel FROM portal_cal_tokens "
                        "WHERE token_hash=? AND revoked_at IS NULL", (h,)).fetchone()
    if not row:
        raise HTTPException(404, "No encontrado")
    rut, creador = row["rut"], row["creado_por"]
    if creador != rut:
        # Link creado por un familiar: en CADA lectura se exige que siga con
        # acceso COMPLETO (vínculo quitado o rebajado a solo-horas → 404).
        # Fallo cerrado ante cualquier error de verificación.
        from portal_routes import _acceso_vinculo, ACCESO_COMPLETO
        try:
            acc = await _acceso_vinculo(creador, row["creado_por_tel"] or "", rut)
        except Exception:
            acc = {}
        if acc.get("acceso") != ACCESO_COMPLETO:
            raise HTTPException(404, "No encontrado")
    with _session.db() as c:
        c.execute("UPDATE portal_cal_tokens SET last_access=datetime('now'), "
                  "n_access=COALESCE(n_access,0)+1 WHERE token_hash=?", (h,))
    rems = [r for r in listar_remedios(rut) if r.get("horarios")]
    return Response(construir_ics(rut, rems, publico=True),
                    media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "private, max-age=900",
                             "X-Robots-Tag": "noindex, nofollow",
                             "Content-Disposition": 'inline; filename="remedios.ics"'})


# ── Tomas compartidas ("lo tomé") ────────────────────────────────────────────
def _hoy() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _fecha_toma(v) -> str:
    """Solo hoy o ayer (Chile): no se marca el futuro ni se reescribe el pasado."""
    f = str(v or "").strip()[:10] or _hoy()
    try:
        d = datetime.strptime(f, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Fecha inválida")
    hoy = datetime.now(_TZ).date()
    if not (hoy - timedelta(days=1) <= d <= hoy):
        raise HTTPException(400, "Solo se puede marcar hoy o ayer.")
    return f


def _nombre_marcador(owner_rut: str, owner_phone: str) -> str:
    """Primer nombre de quien marca, SOLO si el perfil del teléfono es de ese
    mismo RUT (teléfonos compartidos: jamás el nombre de otra persona)."""
    try:
        p = _session.get_profile(owner_phone) or {}
    except Exception:
        p = {}
    if (p.get("rut") or "").replace(".", "").upper() == owner_rut.replace(".", "").upper():
        w = (p.get("nombre") or "").strip().split(" ")[0]
        return w[:1].upper() + w[1:].lower() if w else ""
    return ""


def _fila_toma(r, owner_rut: str) -> dict:
    ts = r["marcado_ts"] or ""
    try:
        hhmm = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).astimezone(_TZ).strftime("%H:%M")
    except ValueError:
        hhmm = ""
    propio = r["marcado_por_rut"] == owner_rut
    return {"remedio_id": r["remedio_id"], "hora": r["hora"], "fecha": r["fecha"],
            "propio": propio, "marcado_por": "" if propio else (r["marcado_por_nombre"] or ""),
            "marcado_hhmm": hhmm}


@router.get("/portal/api/herramientas/tomas")
async def tomas_listar(fecha: str = "", portal_session: str | None = Cookie(None),
                       portal_active: str | None = Cookie(None)):
    owner_rut, _op, rut, _p = await _resolve_completo(portal_session, portal_active)
    f = _fecha_toma(fecha)
    if _es_demo(rut):
        return {"ok": True, "demo": True, "fecha": f, "tomas": []}
    _ensure_tablas()
    with _session.db() as c:
        rows = c.execute("SELECT * FROM portal_tomas WHERE rut=? AND fecha=? ORDER BY hora",
                         (rut, f)).fetchall()
    return {"ok": True, "fecha": f, "tomas": [_fila_toma(r, owner_rut) for r in rows]}


@router.post("/portal/api/herramientas/tomas")
async def tomas_marcar(request: Request, portal_session: str | None = Cookie(None),
                       portal_active: str | None = Cookie(None)):
    """{remedio_id, hora, fecha?, tomado: bool}. Requiere acceso COMPLETO al
    perfil. Si otra persona ya la marcó, se respeta su marca (quién y cuándo)."""
    owner_rut, owner_phone, rut, _p = await _resolve_completo(portal_session, portal_active)
    b = await request.json()
    try:
        rid = int(b.get("remedio_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Remedio inválido")
    hora = str(b.get("hora") or "")[:5]
    if not _HORA_RE.match(hora):
        raise HTTPException(400, "Hora inválida")
    f = _fecha_toma(b.get("fecha"))
    tomado = b.get("tomado") is not False
    if _es_demo(rut):
        return {"ok": True, "demo": True}
    _ensure_tablas()
    with _session.db() as c:
        rem = c.execute("SELECT horarios FROM portal_remedios WHERE id=? AND rut=?", (rid, rut)).fetchone()
        if not rem:
            raise HTTPException(404, "No encontramos ese remedio.")
        if hora not in json.loads(rem["horarios"] or "[]"):
            raise HTTPException(400, "Ese remedio no tiene toma a esa hora.")
        if tomado:
            c.execute("""INSERT OR IGNORE INTO portal_tomas
                         (rut, remedio_id, fecha, hora, marcado_por_rut, marcado_por_nombre, marcado_ts)
                         VALUES (?,?,?,?,?,?,?)""",
                      (rut, rid, f, hora, owner_rut, _nombre_marcador(owner_rut, owner_phone),
                       datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
        else:
            c.execute("DELETE FROM portal_tomas WHERE rut=? AND remedio_id=? AND fecha=? AND hora=?",
                      (rut, rid, f, hora))
        row = c.execute("SELECT * FROM portal_tomas WHERE rut=? AND remedio_id=? AND fecha=? AND hora=?",
                        (rut, rid, f, hora)).fetchone()
    try:
        _session.log_event(owner_phone, "portal_toma_marcada", {"propio": owner_rut == rut, "tomado": tomado})
    except Exception:
        pass
    return {"ok": True, "toma": _fila_toma(row, owner_rut) if row else None}


# ═══ Ficha de emergencia ═══════════════════════════════════════════════════
_FICHA_CAMPOS = {"alergias": 400, "enfermedades": 400, "grupo_sanguineo": 8,
                 "contacto_nombre": 60, "contacto_telefono": 20, "notas": 300}
_GRUPOS = {"", "A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"}


def leer_ficha(rut: str) -> dict | None:
    _ensure_tablas()
    with _session.db() as c:
        r = c.execute("SELECT * FROM portal_ficha_emergencia WHERE rut=?", (rut,)).fetchone()
    if not r:
        return None
    return {"ficha": json.loads(r["data_json"]), "consent_at": r["consent_at"],
            "updated_at": r["updated_at"]}


@router.get("/portal/api/herramientas/ficha")
async def ficha_leer(portal_session: str | None = Cookie(None),
                     portal_active: str | None = Cookie(None)):
    _o, _op, rut, _p = await _resolve_completo(portal_session, portal_active)
    if _es_demo(rut):
        return {"ok": True, "demo": True, "ficha": None}
    f = leer_ficha(rut)
    return {"ok": True, **(f or {"ficha": None})}


@router.post("/portal/api/herramientas/ficha")
async def ficha_guardar(request: Request, portal_session: str | None = Cookie(None),
                        portal_active: str | None = Cookie(None)):
    owner_rut, owner_phone, rut, _p = await _resolve_completo(portal_session, portal_active)
    b = await request.json()
    if b.get("consent") is not True:
        raise HTTPException(400, "Para guardar la ficha, marque la casilla de aceptación.")
    data = {k: _txt(b.get(k), n) for k, n in _FICHA_CAMPOS.items()}
    if data["grupo_sanguineo"] not in _GRUPOS:
        raise HTTPException(400, "Grupo sanguíneo inválido")
    data["incluir_remedios"] = bool(b.get("incluir_remedios", True))
    if _es_demo(rut):
        return {"ok": True, "demo": True}
    _ensure_tablas()
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _session.db() as c:
        c.execute("""INSERT INTO portal_ficha_emergencia (rut, data_json, consent_at, consent_por, updated_at)
                     VALUES (?,?,?,?,datetime('now'))
                     ON CONFLICT(rut) DO UPDATE SET data_json=excluded.data_json,
                       updated_at=datetime('now')""",
                  (rut, json.dumps(data, ensure_ascii=False), ahora, owner_rut))
    try:
        # Rastro sin valores (nunca el contenido clínico en eventos)
        _session.log_event(owner_phone, "portal_ficha_emergencia_guardada",
                           {"propia": owner_rut == rut})
    except Exception:
        pass
    return {"ok": True}


@router.delete("/portal/api/herramientas/ficha")
async def ficha_borrar(portal_session: str | None = Cookie(None),
                       portal_active: str | None = Cookie(None)):
    _o, _op, rut, _p = await _resolve_completo(portal_session, portal_active)
    if _es_demo(rut):
        return {"ok": True, "demo": True}
    _ensure_tablas()
    with _session.db() as c:
        c.execute("DELETE FROM portal_ficha_emergencia WHERE rut=?", (rut,))
    return {"ok": True}


# ═══ "Qué me toca este año" — desde las tablas del bot ═════════════════════
# Presentación para el paciente de cada fila de autocuidado._EXAMENES_PREVENTIVOS
# y doctor_alerts._PREVENTIVOS. Los RANGOS de edad y sexo salen de esas tablas
# (fuente única); aquí solo se decide cómo decirlo y dónde se hace. Si el bot
# agrega una fila que no calza con ninguna clave, el test lo detecta.
_PRESENTACION = [
    # (clave regex sobre el texto de la fila, id, presentación)
    (r"\bPAP\b", "pap", {
        "titulo": "PAP (Papanicolau)",
        "que": "Examen que detecta a tiempo el cáncer de cuello del útero.",
        "frecuencia": "Cada 3 años", "donde": "cmc", "esp": "Matrona", "motivo": "PAP",
        "donde_txt": "Aquí en el centro, con la matrona."}),
    (r"Mamograf[ií]a GES", "mamografia_ges", {
        "titulo": "Mamografía",
        "que": "Radiografía de las mamas para detectar a tiempo el cáncer de mama. Entre los 50 y 69 años tiene garantía GES.",
        "frecuencia": "Cada 2 años", "donde": "fuera", "esp": "Medicina General",
        "motivo": "Orden de mamografía",
        "donde_txt": "Se toma en un centro de imágenes con orden médica. La orden se la damos aquí, en medicina general. Con GES se pide en su CESFAM."}),
    (r"Mamograf[ií]a", "mamografia", {
        "titulo": "Mamografía",
        "que": "Radiografía de las mamas. Desde los 40 años, converse con su médico cada cuánto hacerla.",
        "frecuencia": "Según le indique su médico", "donde": "fuera", "esp": "Medicina General",
        "motivo": "Orden de mamografía",
        "donde_txt": "Se toma en un centro de imágenes con orden médica. La orden se la damos aquí, en medicina general."}),
    (r"pr[oó]stata", "prostata", {
        "titulo": "Control de próstata",
        "que": "Conversación con el médico sobre el examen de próstata (examen de sangre y tacto).",
        "frecuencia": "Desde los 50 años", "donde": "cmc", "esp": "Medicina General",
        "motivo": "Control de próstata",
        "donde_txt": "Aquí en el centro, en medicina general."}),
    (r"lip[ií]dico", "colesterol", {
        "titulo": "Colesterol y azúcar en la sangre",
        "que": "Examen de sangre para revisar el colesterol y la glicemia.",
        "frecuencia": "Periódicamente, según su médico", "donde": "cmc", "esp": "Medicina General",
        "motivo": "Orden de exámenes de colesterol y glicemia",
        "donde_txt": "La orden del examen se la damos aquí, en medicina general."}),
    (r"Control PA", "presion", {
        "titulo": "Control de presión",
        "que": "Tomarse la presión en un control. También puede anotarla en casa en Mis mediciones.",
        "frecuencia": "Al menos 1 vez al año", "donde": "cmc", "esp": "Medicina General",
        "motivo": "Control de presión",
        "donde_txt": "Aquí en el centro, en medicina general."}),
    (r"colorrectal|TSOH", "colon", {
        "titulo": "Examen de cáncer de colon",
        "que": "Un examen simple de sangre oculta en las deposiciones. Converse con su médico.",
        "frecuencia": "Desde los 45 años", "donde": "cmc", "esp": "Medicina General",
        "motivo": "Examen de sangre oculta en deposiciones",
        "donde_txt": "La orden se la damos aquí, en medicina general."}),
    (r"EMPAM", "empam", {
        "titulo": "Examen preventivo del adulto mayor (EMPAM)",
        "que": "Revisión completa de su salud, una vez al año.",
        "frecuencia": "1 vez al año", "donde": "cesfam", "esp": "Medicina Familiar",
        "motivo": "Chequeo preventivo",
        "donde_txt": "Gratis en su CESFAM. Si prefiere, puede hacerse un chequeo aquí con medicina familiar."}),
    (r"Influenza", "vacunas_mayor", {
        "titulo": "Vacunas de influenza y neumococo",
        "que": "La de la influenza es cada año, en otoño. Pregunte también por la del neumococo.",
        "frecuencia": "Influenza: cada año", "donde": "vacunatorio", "esp": "", "motivo": "",
        "donde_txt": "Gratis en el vacunatorio de su CESFAM. El centro no pone vacunas."}),
    (r"\(EMP\)|\bEMP\b", "emp", {
        "titulo": "Examen preventivo (EMP)",
        "que": "Revisión de presión, peso y azúcar, con consejos para cuidarse.",
        "frecuencia": "Periódicamente", "donde": "cmc", "esp": "Medicina General",
        "motivo": "Chequeo preventivo",
        "donde_txt": "Aquí en el centro, en medicina general (o gratis en su CESFAM)."}),
]
# Filas del bot que NO se muestran al paciente (son para el médico).
_SOLO_MEDICO = (r"Screening neonatal",)


def _presentar(texto: str) -> tuple[str, dict] | None:
    for pat, pid, pres in _PRESENTACION:
        if re.search(pat, texto, re.IGNORECASE):
            return pid, pres
    return None


def guia_preventiva() -> dict:
    """Reglas para 'Qué me toca este año' con la MISMA fuente que el bot."""
    from autocuidado import _EXAMENES_PREVENTIVOS
    from doctor_alerts import _PREVENTIVOS
    from pni import _PNI_CALENDARIO

    items: dict[str, dict] = {}
    sin_mapa: list[str] = []
    for fuente, filas in (("autocuidado", _EXAMENES_PREVENTIVOS), ("doctor_alerts", _PREVENTIVOS)):
        for e_min, e_max, sexo, texto in filas:
            if any(re.search(p, texto) for p in _SOLO_MEDICO):
                continue
            m = _presentar(texto)
            if not m:
                sin_mapa.append(texto)
                continue
            pid, pres = m
            if pid in items:      # misma recomendación en ambas tablas: unir rangos
                it = items[pid]
                it["edad_min"] = min(it["edad_min"], e_min)
                it["edad_max"] = max(it["edad_max"], e_max)
                it["fuente"].append(fuente)
                continue
            items[pid] = {"id": pid, "edad_min": e_min, "edad_max": e_max,
                          "sexo": sexo, "fuente": [fuente], **pres}
    vacunas = [{"desde_meses": a, "hasta_meses": b, "vacuna": v, "que": d,
                "donde": "colegio" if esc else "vacunatorio"}
               for a, b, v, d, esc in _PNI_CALENDARIO]
    ninos = {"id": "control_sano", "titulo": "Control sano",
             "que": "Control de crecimiento y desarrollo de su hijo o hija.",
             "frecuencia": "Según su edad", "donde": "cmc", "esp": "Medicina General",
             "motivo": "Control sano", "edad_min": 0, "edad_max": 14, "sexo": None,
             "donde_txt": "Aquí en el centro: atendemos niños en medicina general y medicina familiar.",
             "fuente": ["claude_helper: Control del niño sano → Medicina General"]}
    return {"ok": True, "items": [ninos] + list(items.values()), "vacunas": vacunas,
            "sin_mapa": sin_mapa,
            "aviso": "Es una guía general por edad. Su médico define lo que necesita usted."}


@router.get("/portal/api/herramientas/guia-preventiva")
async def guia_preventiva_endpoint():
    """Pública: no contiene datos de nadie, solo la guía general por edad."""
    return JSONResponse(guia_preventiva(), headers={"Cache-Control": "public, max-age=3600"})


# ═══ Lista de espera (la MISMA del bot) ════════════════════════════════════
# Solo para "no hay horas libres": inscribe en session.waitlist y el aviso lo
# manda el cron existente (jobs._job_waitlist_check, plantilla lista_espera_cupo)
# al WhatsApp del titular. NO sirve para "avíseme si hay una hora ANTES de la que
# ya tengo": ese cron marca como notificada sin avisar a quien ya tiene hora de
# esa especialidad. Para eso el portal abre WhatsApp con el texto escrito.
@router.post("/portal/api/herramientas/lista-espera")
async def lista_espera_inscribir(request: Request, portal_session: str | None = Cookie(None),
                                 portal_active: str | None = Cookie(None)):
    from portal_routes import _resolve_context
    owner_rut, owner_phone, rut, _p = _resolve_context(portal_session, portal_active)
    b = await request.json()
    esp = _txt(b.get("especialidad"), 60)
    if not esp:
        raise HTTPException(400, "Falta la especialidad.")
    id_prof = b.get("id_profesional")
    try:
        id_prof = int(id_prof) if id_prof not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "Profesional inválido")
    nombre = _txt(b.get("nombre"), 80)
    if _es_demo(rut):
        return {"ok": True, "demo": True}
    wid = _session.add_to_waitlist(owner_phone, rut, nombre, esp, id_prof_pref=id_prof,
                                   notas="portal")
    try:
        _session.log_event(owner_phone, "portal_lista_espera", {"waitlist_id": wid, "especialidad": esp,
                                                                "propio": owner_rut == rut})
    except Exception:
        pass
    return {"ok": True, "id": wid}
