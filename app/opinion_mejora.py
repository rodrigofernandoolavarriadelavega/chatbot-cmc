"""Pregunta opcional "¿algo que podamos mejorar?" tras la encuesta postconsulta.

Flujo: el paciente responde la encuesta (mejor/igual/peor) -> el bot agrega UNA
pregunta opcional con botón "Nada por ahora". El siguiente texto libre dentro de
6 h que NO sea un intent claro se guarda en `opinion_libre`; si es intent
(agendar, cancelar, humano...) se procesa normal y no se guarda. No hay estado
de sesión bloqueante: solo `data["opinion_pendiente"]` con vencimiento.

Alerta a recepción (mismo canal que meta_alertas: Telegram, respaldo WhatsApp)
si la encuesta fue 'peor' o el texto contiene una queja.
Flag OPINION_MEJORA_ACTIVE (default true) apaga todo.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

log = logging.getLogger("opinion_mejora")

VENTANA_HORAS = 6
LARGO_MIN = 3

PREGUNTA = ("¿Hay algo que podamos mejorar de tu atención? Puedes escribirlo aquí, "
            "o tocar *Nada por ahora*.")
GRACIAS = "Gracias, lo vamos a revisar. 🙏"
GRACIAS_NADA = "Gracias por tu respuesta 😊\n_Escribe *menu* si necesitas algo más._"
BOTON_NADA = {"id": "opinion_nada", "title": "Nada por ahora"}

_QUEJAS = (
    "no se conecto", "no se pudo conectar", "no llego", "no llegaron", "espere", "esperamos",
    "esperando", "esperar", "nadie contesto", "nadie respondio", "nadie me", "no contestaron",
    "no me contestaron", "pesim", "horrible", "terrible", "reclamo", "queja", "demor",
    "tardo", "tardaron", "tardanza", "atrasad", "atraso", "no me atendieron", "no me atendio",
    "no funciono", "se corto", "se cayo", "mala atencion", "falta de respeto",
)


def activo() -> bool:
    return os.getenv("OPINION_MEJORA_ACTIVE", "true").strip().lower() in ("1", "true", "yes")


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFD", (t or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


_RE_QUEJA = re.compile(r"\b(?:" + "|".join(re.escape(q) for q in _QUEJAS) + r")|\b(?:mal|mala|malo|malos|malas)\b")


def es_queja(texto: str) -> bool:
    return bool(_RE_QUEJA.search(_norm(texto)))


def marcar_pendiente(data: dict, seg: dict | None, categoria: str) -> bool:
    """Deja la pregunta pendiente en la sesión. Devuelve False si el flag está apagado."""
    if not activo():
        return False
    seg = seg or {}
    data["opinion_pendiente"] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "cita_id": str(seg.get("cita_id") or ""),
        "profesional": seg.get("profesional") or "",
        "especialidad": seg.get("especialidad") or "",
        "respuesta": categoria,
    }
    return True


def pendiente_vigente(data: dict) -> dict | None:
    if not activo():
        return None
    p = (data or {}).get("opinion_pendiente")
    if not isinstance(p, dict):
        return None
    try:
        ts = datetime.fromisoformat(p["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        data.pop("opinion_pendiente", None)
        return None
    if datetime.now(timezone.utc) - ts > timedelta(hours=VENTANA_HORAS):
        data.pop("opinion_pendiente", None)
        return None
    return p


def _nombre(phone: str) -> str:
    try:
        from session import get_profile
        pf = get_profile(phone)
        if pf and pf.get("nombre"):
            return pf["nombre"]
    except Exception:
        pass
    return phone


def _alertar(phone: str, p: dict, texto: str | None) -> None:
    motivo = []
    if p.get("respuesta") == "peor":
        motivo.append("respondió *peor* en la encuesta")
    if texto and es_queja(texto):
        motivo.append("el comentario contiene una queja")
    cuerpo = (
        "⚠️ *Opinión de paciente: llamar hoy*\n\n"
        f"Paciente *{_nombre(phone)}* ({phone})\n"
        f"Profesional: {p.get('profesional') or '-'}\n"
        f"Especialidad: {p.get('especialidad') or '-'}\n"
        f"Motivo: {' y '.join(motivo)}\n"
        f"Comentario: {texto if texto else '(sin comentario)'}"
    )
    try:
        from resilience import spawn_task
        import meta_alertas
        spawn_task(meta_alertas.enviar_al_dueno(cuerpo), name="opinion_mejora_alerta")
    except Exception as e:
        log.warning("opinion_mejora: no se pudo enviar alerta: %s", e)


def _debe_alertar(p: dict, texto: str | None) -> bool:
    return p.get("respuesta") == "peor" or bool(texto and es_queja(texto))


def guardar(phone: str, data: dict, texto: str) -> str:
    """Guarda el texto libre como opinión, consume el pendiente y devuelve el agradecimiento."""
    p = data.pop("opinion_pendiente")
    texto = texto.strip()
    from session import db
    with db() as conn:
        conn.execute(
            "INSERT INTO opinion_libre(phone,cita_id,profesional,especialidad,respuesta_encuesta,texto) "
            "VALUES(?,?,?,?,?,?)",
            (phone, p.get("cita_id"), p.get("profesional"), p.get("especialidad"),
             p.get("respuesta"), texto))
        conn.commit()
    try:
        from session import log_event
        log_event(phone, "opinion_libre_guardada", {"respuesta": p.get("respuesta"), "queja": es_queja(texto)})
    except Exception:
        pass
    if _debe_alertar(p, texto):
        _alertar(phone, p, texto)
    return GRACIAS


def declinar(phone: str, data: dict) -> str:
    """Botón 'Nada por ahora'. Consume el pendiente; alerta si la encuesta fue 'peor'."""
    p = data.pop("opinion_pendiente", None)
    if p:
        try:
            from session import log_event
            log_event(phone, "opinion_libre_nada", {"respuesta": p.get("respuesta")})
        except Exception:
            pass
        if _debe_alertar(p, None):
            _alertar(phone, p, None)
    return GRACIAS_NADA
