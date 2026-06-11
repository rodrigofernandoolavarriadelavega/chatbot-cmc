"""Notificaciones automáticas a profesionales vía WhatsApp.

Best practices implementadas:
- Ventana 24h obligatoria (regla del dueño: nunca templates pagados).
- Quiet hours 22:00-07:00 CLT (no spammear de noche).
- Frequency cap: máx 5 push/hora por profesional para evitar fatiga.
- Idempotencia: misma (event_type, cita_id) no se envía dos veces.
- Logging: `prof_notif_sent` / `prof_notif_skipped` con razón.
- Permisos: consulta `profesionales_permisos.json` antes de enviar.

Mapeo id_profesional (Medilink) → phone (WhatsApp).
Fuente: cruce entre `medilink.PROFESIONALES` y `STAFF_PHONES` del .env.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from session import log_event
from session import db as _conn
from messaging import send_whatsapp

log = logging.getLogger("bot.prof_notif")

# ── Mapeo id_profesional Medilink → phone WhatsApp ──────────────────────────
# Cruce manual entre medilink.PROFESIONALES (id) y STAFF_PHONES env (phone).
# Si un profesional no tiene WA registrado, se omite silenciosamente.
PROF_ID_TO_PHONE: dict[int, str] = {
     1: "56987834148",   # Dr. Rodrigo Olavarría
    73: "56974559977",   # Dr. Andrés Abarca
    23: "56941745865",   # Dr. Manuel Borrego
    60: "56936193005",   # Dr. Miguel Millán
    64: "56967273744",   # Dr. Claudio Barraza
    61: "56978255191",   # Dr. Tirso Rejón
    65: "56975580967",   # Dr. Nicolás Quijano
    55: "56938738734",   # Dra. Javiera Burgos
    72: "56984378319",   # Dr. Carlos Jiménez
    66: "56969176902",   # Dra. Daniela Castillo
    75: "56987384044",   # Dr. Fernando Fredes
    74: "56993140124",   # Jorge Montalba
    49: "56941529674",   # Juan Pablo Rodríguez
    68: "56992201931",   # David Pardo
    # Profesionales sin WA registrado (kine, masoterapia, nutrición, etc.):
    # 13, 69, 76, 59, 77, 21, 52, 70, 67, 56 — pendientes de agregar a STAFF_PHONES.
}

TZ_CL = ZoneInfo("America/Santiago")

# Quiet hours: no enviar push entre 22:00 y 07:00 CLT.
QUIET_START_H = 22
QUIET_END_H = 7

# Frequency cap: máx N push por profesional en VENTANA_MIN minutos.
FREQ_CAP_VENTANA_MIN = 60
FREQ_CAP_MAX = 5


def get_phone(id_prof: int) -> str | None:
    """Retorna phone del profesional o None si no está mapeado."""
    return PROF_ID_TO_PHONE.get(id_prof)


def _dentro_ventana_24h(phone: str) -> bool:
    """True si el profesional escribió al bot en las últimas 24h.
    Meta cobra mensajes fuera de esta ventana (templates) — la regla del
    dueño es NUNCA enviar fuera de ventana."""
    try:
        with _conn() as c:
            r = c.execute(
                "SELECT ts FROM messages WHERE phone=? AND direction='in' "
                "ORDER BY ts DESC LIMIT 1",
                (phone,)
            ).fetchone()
            if not r:
                return False
            # ts guardado como UTC string ISO. Comparamos con now UTC.
            from datetime import timezone
            ts_str = r["ts"]
            # SQLite a veces devuelve sin TZ — asumir UTC.
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                return False
            ahora = datetime.now(timezone.utc)
            return (ahora - ts) < timedelta(hours=24)
    except Exception as e:
        log.warning("ventana_24h check error phone=%s: %s", phone, e)
        return False


def _en_quiet_hours() -> bool:
    """True si la hora actual CLT está en quiet hours (22:00-07:00)."""
    now = datetime.now(TZ_CL)
    h = now.hour
    return h >= QUIET_START_H or h < QUIET_END_H


def _excede_freq_cap(phone: str) -> bool:
    """True si el profesional ya recibió >=FREQ_CAP_MAX push en últimos
    FREQ_CAP_VENTANA_MIN minutos."""
    try:
        with _conn() as c:
            r = c.execute(
                "SELECT COUNT(*) as n FROM conversation_events "
                "WHERE phone=? AND event='prof_notif_sent' "
                "AND ts > datetime('now', ?)",
                (phone, f"-{FREQ_CAP_VENTANA_MIN} minutes")
            ).fetchone()
            return (r["n"] if r else 0) >= FREQ_CAP_MAX
    except Exception as e:
        log.warning("freq_cap check error phone=%s: %s", phone, e)
        return False


def _ya_enviado(phone: str, event_type: str, dedup_key: str) -> bool:
    """Idempotencia: True si ya se envió este event_type con este dedup_key.
    dedup_key típicamente = id_cita."""
    try:
        with _conn() as c:
            # FIX F042: la columna es 'meta', no 'data' (conversation_events schema)
            r = c.execute(
                "SELECT 1 FROM conversation_events "
                "WHERE phone=? AND event='prof_notif_sent' "
                "AND meta LIKE ? "
                "AND ts > datetime('now', '-7 days') LIMIT 1",
                (phone, f'%"event_type":"{event_type}"%"dedup_key":"{dedup_key}"%')
            ).fetchone()
            return bool(r)
    except Exception as e:
        log.warning("idempotencia check error phone=%s key=%s: %s",
                    phone, dedup_key, e)
        return False


def _tiene_permiso(phone: str, feature: str) -> bool:
    """Wrapper sobre admin_routes.get_permiso."""
    try:
        from admin_routes import get_permiso
        return get_permiso(phone, feature, default=False)
    except Exception as e:
        log.warning("get_permiso error phone=%s feature=%s: %s",
                    phone, feature, e)
        return False


def _primer_nombre(nombre_completo: str) -> str:
    """'Dr. Rodrigo Olavarría' → 'Rodrigo'. 'Jorge Montalba' → 'Jorge'."""
    if not nombre_completo:
        return ""
    tokens = nombre_completo.split()
    for t in tokens:
        tl = t.lower().rstrip(".")
        if tl not in ("dr", "dra", "ps", "t.m", "tm", "klgo", "klga",
                      "nut", "fga", "ftra", "tcm", "tens"):
            return t
    return tokens[0] if tokens else ""


async def notify_prof(
    id_prof: int,
    feature: str,
    body: str,
    event_type: str,
    dedup_key: str = "",
    *,
    ignore_quiet_hours: bool = False,
    ignore_freq_cap: bool = False,
) -> tuple[bool, str]:
    """Envía notificación a un profesional aplicando todas las reglas:
    1. Profesional debe tener WA registrado (PROF_ID_TO_PHONE).
    2. Permiso `feature` activo en /profesionalescmc.
    3. Dentro de ventana 24h (regla costo $0).
    4. Fuera de quiet hours (salvo ignore=True).
    5. No exceder freq cap (salvo ignore=True).
    6. Idempotencia: misma (event_type, dedup_key) no se reenvía.

    Retorna (enviado, razón_skip_si_no_enviado).
    Loggea conversation_event en ambos casos para auditoría.
    """
    phone = get_phone(id_prof)
    if not phone:
        return False, "sin_phone"

    # Permiso
    if not _tiene_permiso(phone, feature):
        log_event(phone, "prof_notif_skipped", {
            "event_type": event_type, "razon": "sin_permiso",
            "feature": feature, "id_prof": id_prof,
        })
        return False, "sin_permiso"

    # Ventana 24h (la regla más estricta)
    if not _dentro_ventana_24h(phone):
        log_event(phone, "prof_notif_skipped", {
            "event_type": event_type, "razon": "fuera_ventana_24h",
            "feature": feature, "id_prof": id_prof,
        })
        return False, "fuera_ventana_24h"

    # Quiet hours
    if not ignore_quiet_hours and _en_quiet_hours():
        # Opt-in estricto: si el prof tiene quiet_hours_strict, respetar siempre
        # (ya está respetado al estar acá). Si no tiene, opt-in default es respetar.
        log_event(phone, "prof_notif_skipped", {
            "event_type": event_type, "razon": "quiet_hours",
            "feature": feature, "id_prof": id_prof,
        })
        return False, "quiet_hours"

    # Frequency cap
    if not ignore_freq_cap and _excede_freq_cap(phone):
        log_event(phone, "prof_notif_skipped", {
            "event_type": event_type, "razon": "freq_cap_excedido",
            "feature": feature, "id_prof": id_prof,
        })
        return False, "freq_cap"

    # Idempotencia
    if dedup_key and _ya_enviado(phone, event_type, dedup_key):
        log_event(phone, "prof_notif_skipped", {
            "event_type": event_type, "razon": "duplicado",
            "feature": feature, "id_prof": id_prof, "dedup_key": dedup_key,
        })
        return False, "duplicado"

    # Envío
    try:
        await send_whatsapp(phone, body)
        log_event(phone, "prof_notif_sent", {
            "event_type": event_type,
            "feature": feature,
            "id_prof": id_prof,
            "dedup_key": dedup_key,
            "preview": body[:120],
        })
        log.info("prof_notif sent id_prof=%d feature=%s event=%s",
                 id_prof, feature, event_type)
        return True, "ok"
    except Exception as e:
        log.error("prof_notif send error id_prof=%d: %s", id_prof, e)
        log_event(phone, "prof_notif_skipped", {
            "event_type": event_type, "razon": "error_envio",
            "feature": feature, "id_prof": id_prof, "error": str(e)[:200],
        })
        return False, "error_envio"


# ── Funciones de alto nivel para cada tipo de evento ────────────────────────

def _fecha_humana(fecha_iso: str) -> str:
    """'2026-05-15' → 'jueves 15 de mayo'."""
    try:
        d = datetime.strptime(fecha_iso[:10], "%Y-%m-%d")
        dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        meses = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio",
                 "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
        return f"{dias[d.weekday()]} {d.day} de {meses[d.month]}"
    except Exception:
        return fecha_iso


async def notify_nueva_cita(
    id_prof: int,
    profesional_nombre: str,
    paciente_nombre: str,
    fecha: str,
    hora: str,
    modalidad: str = "particular",
    id_cita: str = "",
) -> tuple[bool, str]:
    """Push WA al profesional cuando bot agenda paciente nuevo."""
    nombre_corto = _primer_nombre(profesional_nombre)
    pac_corto = _primer_nombre(paciente_nombre) if paciente_nombre else "Paciente"
    fecha_h = _fecha_humana(fecha)
    mod_label = "Fonasa" if "fonasa" in modalidad.lower() else "Particular"
    body = (
        f"📅 *Nueva cita agendada* — {nombre_corto}\n\n"
        f"👤 {paciente_nombre or pac_corto}\n"
        f"🗓 {fecha_h} · {hora}\n"
        f"💳 {mod_label}\n\n"
        f"_Te avisé porque el paciente lo agendó por WhatsApp. "
        f"Responde *agenda* para ver tu día._"
    )
    return await notify_prof(
        id_prof, "notif_nueva_cita", body,
        event_type="nueva_cita", dedup_key=id_cita,
    )


async def notify_cancelacion(
    id_prof: int,
    profesional_nombre: str,
    paciente_nombre: str,
    fecha: str,
    hora: str,
    id_cita: str = "",
) -> tuple[bool, str]:
    """Push WA al profesional cuando paciente cancela."""
    nombre_corto = _primer_nombre(profesional_nombre)
    fecha_h = _fecha_humana(fecha)
    body = (
        f"❌ *Paciente canceló* — {nombre_corto}\n\n"
        f"👤 {paciente_nombre or 'Paciente'}\n"
        f"🗓 Tenía hora el {fecha_h} · {hora}\n\n"
        f"_Tu slot quedó libre. Alma lo está ofreciendo automáticamente a la "
        f"lista de espera; si alguien lo toma, recepción confirma._"
    )
    return await notify_prof(
        id_prof, "notif_cancelacion", body,
        event_type="cancelacion", dedup_key=id_cita,
    )


async def notify_reagenda(
    id_prof: int,
    profesional_nombre: str,
    paciente_nombre: str,
    fecha_old: str,
    hora_old: str,
    fecha_new: str,
    hora_new: str,
    id_cita_old: str = "",
    id_cita_new: str = "",
) -> tuple[bool, str]:
    """Push WA al profesional cuando paciente cambia su hora."""
    nombre_corto = _primer_nombre(profesional_nombre)
    f_old = _fecha_humana(fecha_old)
    f_new = _fecha_humana(fecha_new)
    body = (
        f"🔄 *Paciente reagendó* — {nombre_corto}\n\n"
        f"👤 {paciente_nombre or 'Paciente'}\n"
        f"   Antes: {f_old} · {hora_old}\n"
        f"   Ahora: {f_new} · {hora_new}"
    )
    return await notify_prof(
        id_prof, "notif_reagenda", body,
        event_type="reagenda",
        dedup_key=f"{id_cita_old}->{id_cita_new}",
    )


async def notify_no_show(
    id_prof: int,
    profesional_nombre: str,
    paciente_nombre: str,
    hora: str,
    id_cita: str = "",
) -> tuple[bool, str]:
    """Push WA al profesional cuando paciente no se presenta pasada hora + 30min."""
    nombre_corto = _primer_nombre(profesional_nombre)
    body = (
        f"⚠️ *Posible no-show* — {nombre_corto}\n\n"
        f"👤 {paciente_nombre or 'Paciente'}\n"
        f"🕐 Tenía hora a las {hora} y aún no aparece en sistema como atendido.\n\n"
        f"_Si quieres marcarlo como atendido o no-show, díselo a recepción._"
    )
    return await notify_prof(
        id_prof, "notif_no_show", body,
        event_type="no_show", dedup_key=id_cita,
    )


async def notify_paciente_peor(
    id_prof: int,
    profesional_nombre: str,
    paciente_nombre: str,
    paciente_phone: str,
    especialidad: str,
) -> tuple[bool, str]:
    """Push WA al profesional cuando paciente reportó sentirse 'peor' en
    seguimiento post-consulta. Ignora quiet hours (urgencia clínica)."""
    nombre_corto = _primer_nombre(profesional_nombre)
    body = (
        f"🩺 *Alerta clínica* — {nombre_corto}\n\n"
        f"👤 {paciente_nombre or 'Paciente'} reportó sentirse *peor* tras "
        f"su consulta de {especialidad}.\n"
        f"📱 {paciente_phone}\n\n"
        f"_Considera contactarlo o pedirle que vuelva a control._"
    )
    return await notify_prof(
        id_prof, "notif_paciente_peor", body,
        event_type="paciente_peor",
        dedup_key=f"{paciente_phone}:{especialidad}",
        ignore_quiet_hours=True,  # urgencia clínica supera quiet hours
    )
