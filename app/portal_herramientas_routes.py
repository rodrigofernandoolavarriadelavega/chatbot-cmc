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
                         quedan=?, por_toma=?, quedan_fecha=?, updated_at=datetime('now')
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
                  ahora: datetime | None = None) -> str:
    ahora = ahora or datetime.now(_TZ)
    stamp = ahora.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    hoy = ahora.date()
    cal = "Mis remedios" + (f" · {nombre_persona}" if nombre_persona else "")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0",
         "PRODID:-//Centro Medico Carampangue//Portal del Paciente//ES",
         "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
         f"X-WR-CALNAME:{_ics_esc(cal)}", "X-WR-TIMEZONE:America/Santiago"]
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
            titulo = f"Tomar {r['nombre']}" + (f" ({r['dosis']})" if r.get("dosis") else "")
            rrule = ("RRULE:FREQ=WEEKLY;BYDAY=" + ",".join(_DIAS_ICS[i] for i in dias)
                     if dias else "RRULE:FREQ=DAILY")
            desc = ("Recordatorio que usted anotó en su portal. "
                    "Siga siempre la indicación de su médico. Centro Médico Carampangue.")
            L += ["BEGIN:VEVENT",
                  f"UID:{uid_toma(rut, r['uid'], hora)}",
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
