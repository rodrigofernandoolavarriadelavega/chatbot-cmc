"""email_ticker.py — Lector de correos de Medilink (push, con hora exacta).

Qué resuelve: `agenda_ticker.py` infiere el ORDEN de agendamiento por el `id`
incremental de la cita porque la API de Medilink no entrega fecha de
creación. Medilink SÍ manda un correo al Gmail del centro cada vez que se
agenda, anula o reagenda una cita — y la cabecera `Date` de ese correo es la
hora EXACTA del evento. Este módulo lee ese buzón (solo lectura, IMAP), lo
parsea y usa esa hora real para complementar (no reemplazar) al ticker
existente.

Remitentes: `notificaciones@dentalink.cl` y `notificaciones@softwaremedilink.com`
(ambos HealthAtom). Asuntos que procesamos: "Cita agendada", "Cita anulada",
"Cita reagendada". Se ignoran otros asuntos que llegan al mismo buzón
("Confirma tu cita", avisos de transferencia bancaria — ver README para la
oportunidad NO implementada de verificación automática de abonos).

Diseño (mismos principios que agenda_ticker.py):
  1. Cursor persistido en `system_state` (`email_ticker_last_uid` +
     `email_ticker_uidvalidity`) = mayor UID de IMAP ya procesado. Arranque en
     frío: se siembra con el UID más alto existente SIN leer los ~17.000
     correos históricos del buzón (ver UIDNEXT).
  2. Cada pasada (job del scheduler, 60 s): `UID SEARCH cursor+1:*`, trae solo
     los correos nuevos. El fetch/parseo corre en threadpool
     (`asyncio.to_thread`) porque `imaplib` es bloqueante — nunca toca el
     event loop.
  3. Conexión SIEMPRE `readonly=True` (`SELECT ... readonly`) — jamás se
     marca como leído ni se borra nada del buzón del centro.
  4. El correo NO trae el `id_cita` de Medilink. Se cruza por
     paciente + profesional + fecha + hora contra la tabla local
     `agenda_ticker` (espejo de `/citas`, ver agenda_ticker.py). Si no hay
     exactamente un candidato, se marca `no_cruzado` — nunca se adivina.
  5. Si el cruce fue exitoso, se alimenta `agenda_ticker.fecha_creacion_real`
     (hora exacta del correo, en vez de `fecha_actualizacion` de Medilink que
     es un proxy). El ticker existente prioriza esa columna al mostrar "hace
     cuánto entró" — ver `agenda_ticker.get_feed`.
  6. Degradación: cualquier fallo (Gmail no responde, cambia el formato del
     correo, credenciales no configuradas) se loguea y la pasada retorna sin
     lanzar excepción. Un correo mal parseado no tumba el sistema de
     agendamiento — se salta ESE correo, no el resto del lote ni el cursor.
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import re
import ssl
import unicodedata
from datetime import date, datetime
from email.header import decode_header
from zoneinfo import ZoneInfo

log = logging.getLogger("email_ticker")

_CHILE_TZ = ZoneInfo("America/Santiago")

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
IMAP_MAILBOX = "INBOX"

# HealthAtom manda las notificaciones de Medilink desde estos 2 dominios
# (dentalink.cl y softwaremedilink.com son la misma plataforma).
_REMITENTES_MEDILINK = ("notificaciones@dentalink.cl", "notificaciones@softwaremedilink.com")

# Asuntos que procesamos → tipo de evento. "Confirma tu cita" (recordatorio,
# no agendamiento) y cualquier otro asunto se ignoran a propósito.
_SUBJECT_TIPOS = {
    "Cita agendada":   "agendada",
    "Cita anulada":    "anulada",
    "Cita reagendada": "reagendada",
}

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def ensure_email_ticker_table() -> None:
    """Crea la tabla del lector de correos si no existe. Idempotente."""
    from session import db
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_ticker (
                uid                 INTEGER PRIMARY KEY,
                tipo                TEXT DEFAULT '',
                email_ts            TEXT DEFAULT '',
                remitente           TEXT DEFAULT '',
                asunto              TEXT DEFAULT '',
                paciente_nombre     TEXT DEFAULT '',
                profesional_nombre  TEXT DEFAULT '',
                fecha_cita          TEXT DEFAULT '',
                hora_cita           TEXT DEFAULT '',
                id_cita             INTEGER,
                cruce_status        TEXT DEFAULT 'no_cruzado',
                canal               TEXT DEFAULT '',
                canal_detalle       TEXT DEFAULT '',
                procesado_at        TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_ticker_ts ON email_ticker(email_ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_ticker_cita ON email_ticker(id_cita)")
        conn.commit()


# ── Parseo del cuerpo del correo ────────────────────────────────────────────

def _decode_subject(raw_subject: str | None) -> str:
    if not raw_subject:
        return ""
    parts = decode_header(raw_subject)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def _strip_html(html: str) -> str:
    """Fallback si el correo no trae parte text/plain (hoy los 3 tipos que
    procesamos SÍ la traen — ver README — pero si HealthAtom cambia el
    template no queremos que el parser se rompa en silencio)."""
    import html as _html_mod
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", "\n", text)
    text = _html_mod.unescape(text)
    return text


def _get_body_text(msg: email.message.Message) -> str:
    """Prefiere text/plain (más limpio y estable); si no existe, limpia el
    text/html."""
    plain, htmlpart = None, None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            if ctype == "text/plain" and plain is None:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                try:
                    plain = payload.decode(charset, errors="replace")
                except Exception:
                    plain = None
            elif ctype == "text/html" and htmlpart is None:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                try:
                    htmlpart = payload.decode(charset, errors="replace")
                except Exception:
                    htmlpart = None
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace") if payload else ""
        except Exception:
            text = ""
        if msg.get_content_type() == "text/html":
            htmlpart = text
        else:
            plain = text
    if plain:
        return plain
    if htmlpart:
        return _strip_html(htmlpart)
    return ""


def _clean_lines(text: str) -> list[str]:
    lines = [l.strip() for l in text.splitlines()]
    return [l for l in lines if l]


def _value_after(lines: list[str], label: str) -> str | None:
    """Devuelve la línea siguiente a `label` (los correos de Medilink traen
    'Fecha' / 'Hora' / 'Paciente' / 'Profesional' como etiqueta en su propia
    línea y el valor en la línea de abajo)."""
    try:
        idx = lines.index(label)
    except ValueError:
        return None
    if idx + 1 < len(lines):
        return lines[idx + 1]
    return None


def _infer_fecha_iso(dia: int, mes_nombre: str, email_dt_local: datetime) -> str | None:
    """El correo dice '13 de julio' SIN año. Se infiere del año/mes en que
    llegó el correo (`email_dt_local`), con corrección de salto de año
    cuando el mes de la cita y el mes del correo quedan a más de 6 meses de
    distancia (diciembre→enero y viceversa)."""
    mes = _MESES.get((mes_nombre or "").strip().lower())
    if not mes or not (1 <= dia <= 31):
        return None
    year = email_dt_local.year
    diff = mes - email_dt_local.month
    if diff <= -6:
        year += 1
    elif diff >= 6:
        year -= 1
    try:
        return date(year, mes, dia).isoformat()
    except ValueError:
        return None


def parse_email_body(subject: str, body_text: str, email_dt_local: datetime) -> dict | None:
    """Parsea el cuerpo de un correo de Medilink ya identificado (asunto +
    remitente correctos). Devuelve None si falta algún campo obligatorio —
    NO se adivina, se descarta el correo y se loguea."""
    tipo = _SUBJECT_TIPOS.get(subject.strip())
    if not tipo:
        return None
    lines = _clean_lines(body_text)

    fecha_raw = _value_after(lines, "Fecha")
    hora_raw = _value_after(lines, "Hora")
    paciente = _value_after(lines, "Paciente")
    profesional = _value_after(lines, "Profesional")

    if not (fecha_raw and hora_raw and paciente and profesional):
        return None

    m_fecha = re.match(r"(\d{1,2})\s+de\s+(\w+)", fecha_raw, re.IGNORECASE)
    m_hora = re.match(r"(\d{1,2}):(\d{2})", hora_raw)
    if not m_fecha or not m_hora:
        return None

    fecha_iso = _infer_fecha_iso(int(m_fecha.group(1)), m_fecha.group(2), email_dt_local)
    if not fecha_iso:
        return None
    hora_hhmm = f"{int(m_hora.group(1)):02d}:{m_hora.group(2)}"

    return {
        "tipo": tipo,
        "paciente_nombre": paciente.strip(),
        "profesional_nombre": profesional.strip(),
        "fecha_cita": fecha_iso,
        "hora_cita": hora_hhmm,
    }


# ── Cruce contra Medilink local (agenda_ticker) ─────────────────────────────

def _normalizar_nombre(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _nombres_coinciden(a: str, b: str) -> bool:
    a, b = _normalizar_nombre(a), _normalizar_nombre(b)
    if not a or not b:
        return False
    if a == b:
        return True
    # Contención: el correo o Medilink a veces truncan el nombre (ej. solo
    # el primer nombre). El match ya viene acotado por profesional+fecha+hora,
    # así que la colisión por contención es de riesgo bajo.
    return a in b or b in a


def _fecha_medilink_a_iso(fecha_raw: str) -> str | None:
    """`agenda_ticker.fecha_cita` guarda tal cual lo que devuelve Medilink
    (DD/MM/YYYY según docs/medilink_gotchas.md §5), pero por robustez se
    acepta también YYYY-MM-DD por si cambia el formato de origen."""
    fecha_raw = (fecha_raw or "").strip()
    if not fecha_raw:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(fecha_raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def cruzar_con_agenda_ticker(paciente: str, profesional: str, fecha_iso: str, hora: str) -> tuple[int | None, str]:
    """Busca en la tabla local `agenda_ticker` (espejo de `/citas`) la cita
    que corresponde a este correo, cruzando por paciente + profesional +
    fecha + hora. Si hay 0 o más de 1 candidato, NO se adivina: se devuelve
    (None, 'no_cruzado')."""
    from session import db
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT id_cita, paciente_nombre, profesional, fecha_cita FROM agenda_ticker WHERE hora_inicio = ?",
                (hora,),
            ).fetchall()
    except Exception as e:
        log.warning("cruzar_con_agenda_ticker: error de lectura: %s", e)
        return None, "no_cruzado"

    candidatos = []
    for r in rows:
        f_iso = _fecha_medilink_a_iso(r["fecha_cita"])
        if f_iso != fecha_iso:
            continue
        if not _nombres_coinciden(paciente, r["paciente_nombre"]):
            continue
        if not _nombres_coinciden(profesional, r["profesional"]):
            continue
        candidatos.append(r["id_cita"])

    if len(candidatos) == 1:
        return candidatos[0], "cruzado"
    return None, "no_cruzado"


# ── IMAP (bloqueante — SIEMPRE llamado vía asyncio.to_thread) ──────────────

def _connect_imap() -> imaplib.IMAP4_SSL:
    """Conecta por SSL. En Mac de desarrollo la verificación de certificados
    puede fallar por falta de `certifi` en el bundle de Python — en
    producción (Linux) normalmente no hace falta este fallback, pero se deja
    por robustez. Nunca se desactiva la verificación de certificados."""
    try:
        ctx = ssl.create_default_context()
        return imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)
    except ssl.SSLCertVerificationError:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        return imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)


def _fetch_new_emails_sync(cursor_uid: int, max_emails: int = 200) -> tuple[list[dict], int]:
    """Función bloqueante: login, SELECT readonly, UID SEARCH de correos
    nuevos, fetch de cada uno. Devuelve (lista de correos crudos, uid_max
    visto). Solo lectura — jamás marca como leído ni borra. Nunca lanza: en
    caso de error retorna ([], cursor_uid) y loguea."""
    from config import GMAIL_CMC_USER, GMAIL_CMC_APP_PASSWORD

    if not GMAIL_CMC_USER or not GMAIL_CMC_APP_PASSWORD:
        log.warning("_fetch_new_emails_sync: GMAIL_CMC_USER/GMAIL_CMC_APP_PASSWORD no configurados")
        return [], cursor_uid

    M = None
    try:
        M = _connect_imap()
        M.login(GMAIL_CMC_USER, GMAIL_CMC_APP_PASSWORD)
        M.select(IMAP_MAILBOX, readonly=True)

        typ, data = M.uid("search", None, f"UID {cursor_uid + 1}:*")
        if typ != "OK":
            log.warning("_fetch_new_emails_sync: UID SEARCH falló: %s", typ)
            return [], cursor_uid
        uids = [int(u) for u in data[0].split() if u.isdigit()]
        # UID SEARCH "N:*" con buzón sin correos >= N a veces devuelve el UID
        # más alto existente (comportamiento documentado de IMAP) — filtramos
        # cualquier UID <= cursor para no reprocesar.
        uids = sorted(u for u in uids if u > cursor_uid)
        if not uids:
            return [], cursor_uid
        if len(uids) > max_emails:
            log.warning(
                "_fetch_new_emails_sync: %d correos nuevos en una pasada (> %d) — "
                "se procesan los primeros %d, el resto queda para la próxima pasada",
                len(uids), max_emails, max_emails,
            )
            uids = uids[:max_emails]

        out = []
        new_max = cursor_uid
        for u in uids:
            new_max = max(new_max, u)
            try:
                typ, msgdata = M.uid("fetch", str(u), "(RFC822)")
                if typ != "OK" or not msgdata or msgdata[0] is None:
                    continue
                raw = msgdata[0][1]
                msg = email.message_from_bytes(raw)
                out.append({
                    "uid": u,
                    "subject": _decode_subject(msg.get("Subject")),
                    "from": msg.get("From", ""),
                    "date_header": msg.get("Date", ""),
                    "body": _get_body_text(msg),
                })
            except Exception as e:
                log.warning("_fetch_new_emails_sync: fallo parseando UID %d: %s", u, e)
                continue
        return out, new_max
    except imaplib.IMAP4.error as e:
        log.error("_fetch_new_emails_sync: error IMAP: %s", e)
        return [], cursor_uid
    except Exception as e:
        log.error("_fetch_new_emails_sync: fallo inesperado: %s", e)
        return [], cursor_uid
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass


def _seed_cursor_sync() -> int | None:
    """Arranque en frío: UID más alto ya existente en el buzón (UIDNEXT - 1),
    SIN leer los correos históricos. Igual estrategia que
    agenda_ticker._fetch_max_cita_id."""
    from config import GMAIL_CMC_USER, GMAIL_CMC_APP_PASSWORD
    if not GMAIL_CMC_USER or not GMAIL_CMC_APP_PASSWORD:
        return None
    M = None
    try:
        M = _connect_imap()
        M.login(GMAIL_CMC_USER, GMAIL_CMC_APP_PASSWORD)
        typ, data = M.status(IMAP_MAILBOX, "(UIDNEXT)")
        if typ != "OK" or not data:
            return None
        m = re.search(r"UIDNEXT (\d+)", data[0].decode() if isinstance(data[0], bytes) else data[0])
        if not m:
            return None
        return max(int(m.group(1)) - 1, 0)
    except Exception as e:
        log.error("_seed_cursor_sync: no se pudo sembrar cursor inicial: %s", e)
        return None
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass


# ── Job del scheduler ────────────────────────────────────────────────────

async def poll_email_ticker() -> dict:
    """Job del scheduler (cada 60s). Todo el trabajo de red bloqueante corre
    en threadpool. Degrada con gracia: cualquier excepción se loguea y se
    retorna sin propagar — un correo mal parseado o Gmail caído nunca puede
    tumbar el sistema de agendamiento."""
    import asyncio
    from session import system_state_get, system_state_set, db
    from config import GMAIL_CMC_USER, GMAIL_CMC_APP_PASSWORD

    if not GMAIL_CMC_USER or not GMAIL_CMC_APP_PASSWORD:
        return {"ok": False, "error": "GMAIL_CMC_USER/GMAIL_CMC_APP_PASSWORD no configurados"}

    try:
        ensure_email_ticker_table()

        cursor_raw = system_state_get("email_ticker_last_uid")
        if cursor_raw is None:
            seed = await asyncio.to_thread(_seed_cursor_sync)
            if seed is None:
                return {"ok": False, "error": "no se pudo sembrar cursor inicial"}
            system_state_set("email_ticker_last_uid", str(seed))
            log.info("poll_email_ticker: arranque en frío, cursor=%d (sin backfill)", seed)
            return {"ok": True, "nuevos": 0, "cursor": seed, "cold_start": True}

        cursor = int(cursor_raw)
        raw_emails, new_max = await asyncio.to_thread(_fetch_new_emails_sync, cursor)

        if new_max > cursor:
            system_state_set("email_ticker_last_uid", str(new_max))

        if not raw_emails:
            return {"ok": True, "nuevos": 0, "cursor": new_max}

        procesados = 0
        for m in raw_emails:
            try:
                if not any(rem.lower() in (m["from"] or "").lower() for rem in _REMITENTES_MEDILINK):
                    continue
                if m["subject"].strip() not in _SUBJECT_TIPOS:
                    continue
                try:
                    dt = email.utils.parsedate_to_datetime(m["date_header"])
                except Exception:
                    dt = None
                if dt is None:
                    log.warning("poll_email_ticker: UID %d sin Date parseable, se descarta", m["uid"])
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                dt_local = dt.astimezone(_CHILE_TZ)

                parsed = parse_email_body(m["subject"], m["body"], dt_local)
                if not parsed:
                    log.warning("poll_email_ticker: UID %d no se pudo parsear el cuerpo (asunto=%r)", m["uid"], m["subject"])
                    continue

                id_cita, cruce_status = cruzar_con_agenda_ticker(
                    parsed["paciente_nombre"], parsed["profesional_nombre"],
                    parsed["fecha_cita"], parsed["hora_cita"],
                )

                canal, canal_detalle = "", ""
                if id_cita is not None:
                    try:
                        from agenda_ticker import _clasificar_canal
                        canal, canal_detalle = _clasificar_canal(id_cita)
                    except Exception as e:
                        log.warning("poll_email_ticker: fallo clasificando canal id_cita=%s: %s", id_cita, e)

                email_ts_str = dt_local.strftime("%Y-%m-%d %H:%M:%S")

                with db() as conn:
                    conn.execute("""
                        INSERT OR IGNORE INTO email_ticker
                            (uid, tipo, email_ts, remitente, asunto, paciente_nombre,
                             profesional_nombre, fecha_cita, hora_cita, id_cita,
                             cruce_status, canal, canal_detalle)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        m["uid"], parsed["tipo"], email_ts_str, m["from"], m["subject"],
                        parsed["paciente_nombre"], parsed["profesional_nombre"],
                        parsed["fecha_cita"], parsed["hora_cita"], id_cita,
                        cruce_status, canal, canal_detalle,
                    ))
                    conn.commit()

                # Alimenta el ticker existente con la hora real de creación.
                # Solo para 'agendada' — es el único tipo donde la fecha/hora
                # del correo coincide con lo que agenda_ticker capturó al
                # crear la fila (una 'reagendada' cambia fecha/hora y NO
                # actualiza la fila ya insertada en agenda_ticker, ver
                # docstring del módulo).
                if id_cita is not None and parsed["tipo"] == "agendada":
                    try:
                        from agenda_ticker import set_fecha_creacion_real
                        set_fecha_creacion_real(id_cita, email_ts_str)
                    except Exception as e:
                        log.warning("poll_email_ticker: no se pudo alimentar agenda_ticker id_cita=%s: %s", id_cita, e)

                procesados += 1
            except Exception as e:
                # Un correo individual mal formado no debe frenar el resto del lote.
                log.error("poll_email_ticker: fallo procesando UID %s: %s", m.get("uid"), e)
                continue

        log.info("poll_email_ticker: %d correo(s) relevante(s) procesado(s) de %d nuevo(s), cursor=%d",
                  procesados, len(raw_emails), new_max)
        return {"ok": True, "nuevos": procesados, "vistos": len(raw_emails), "cursor": new_max}
    except Exception as e:
        log.error("poll_email_ticker: fallo inesperado, no se propaga: %s", e)
        return {"ok": False, "error": str(e)}


def get_stats() -> dict:
    """Resumen de cruce para el panel/reporte: cuántos correos por tipo,
    cuántos se lograron cruzar contra una cita real."""
    from session import db
    ensure_email_ticker_table()
    with db() as conn:
        rows = conn.execute("""
            SELECT tipo, cruce_status, COUNT(*) n FROM email_ticker GROUP BY tipo, cruce_status
        """).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM email_ticker").fetchone()[0]
        cruzados = conn.execute("SELECT COUNT(*) FROM email_ticker WHERE cruce_status='cruzado'").fetchone()[0]
    return {
        "total": total,
        "cruzados": cruzados,
        "pct_cruzado": round(100 * cruzados / total, 1) if total else 0.0,
        "detalle": [dict(r) for r in rows],
    }
