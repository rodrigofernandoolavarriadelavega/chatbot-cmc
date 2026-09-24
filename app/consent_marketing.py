"""Respuesta del paciente a la plantilla consent_marketing_v2 (Ley 21.719).

Decide si un mensaje entrante ES la respuesta al consentimiento de marketing
mirando A QUÉ está contestando el paciente, no en qué estado está la sesión.

Antes (flows.py, bloque consent_marketing_v1) solo se registraba si la sesión
estaba en IDLE y había una fila 'pending' de ≤7 días. Auditoría 2026-09-24
(706 plantillas en 30 días) encontró los huecos:
  - con recepción en HUMAN_TAKEOVER, main.py silenciaba el mensaje antes de
    llegar al flujo → el botón se perdía sin registro;
  - quien antes dijo "no" y ahora "Sí, actívenlos" quedaba 'declined' (la fila
    ya no estaba 'pending') → su voluntad actual no quedaba registrada;
  - "Sii"/"Siii" no se reconocían.

Reglas:
  - "Sí, actívenlos" es el texto del botón y solo existe en esta plantilla →
    cuenta si se le envió la plantilla en los últimos 7 días.
  - "No por ahora" lo usan muchos otros botones, y un "sí" escrito es ambiguo →
    cuentan SOLO si el último mensaje que le mandamos fue la plantilla.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("bot")

_TEMPLATE_PREFIJOS = ("[template: consent_marketing_v2]", "[template: consent_marketing_v1]")
VENTANA_DIAS = 7

# Solo textos EXCLUSIVOS de consent_marketing_v2 ("Sí, acepto" también es botón
# del consentimiento dental y del de privacidad → va en _SI_ESCRITO).
_BOTON_SI = {"sí, actívenlos", "si, activenlos", "sí, activenlos", "si, actívenlos",
             "actívenlos", "activenlos"}
_SI_ESCRITO = {"si", "sí", "si acepto", "sí acepto", "sí, acepto", "si, acepto",
               "acepto", "si activenlos",
               "sí actívenlos", "si porfavor", "si por favor", "sí por favor"}
_NO = {"no por ahora", "no", "no gracias", "no, gracias", "no acepto"}


def _norm(txt: str) -> str:
    t = (txt or "").strip().lower()
    t = re.sub(r"[!¡.?¿✅❌]+", "", t).strip()
    t = re.sub(r"([aeiouáéíóú])\1+", r"\1", t)   # "siii" → "si"
    return re.sub(r"\s+", " ", t)


def _ultimo_saliente_y_plantilla(phone: str) -> tuple[bool, bool]:
    """(el último mensaje saliente es la plantilla, hubo plantilla en la ventana)."""
    from session import db
    with db() as conn:
        ult = conn.execute(
            "SELECT text FROM messages WHERE phone=? AND direction='out' "
            "ORDER BY id DESC LIMIT 1", (phone,)).fetchone()
        hubo = conn.execute(
            "SELECT 1 FROM messages WHERE phone=? AND direction='out' "
            "AND (text LIKE ? OR text LIKE ?) AND ts >= datetime('now', ?) LIMIT 1",
            (phone, _TEMPLATE_PREFIJOS[0] + "%", _TEMPLATE_PREFIJOS[1] + "%",
             f"-{VENTANA_DIAS} days")).fetchone()
    es_ultimo = bool(ult and (ult[0] or "").startswith(_TEMPLATE_PREFIJOS))
    return es_ultimo and bool(hubo), bool(hubo)


def detectar(phone: str, texto: str) -> str | None:
    """'accepted' | 'declined' si el mensaje responde a la plantilla; si no, None."""
    t = _norm(texto)
    if not t or len(t) > 40:
        return None
    es_boton_si = t in {_norm(x) for x in _BOTON_SI}
    es_si = es_boton_si or t in {_norm(x) for x in _SI_ESCRITO}
    es_no = t in {_norm(x) for x in _NO}
    if not (es_si or es_no):
        return None
    try:
        ultimo_es_plantilla, hubo_plantilla = _ultimo_saliente_y_plantilla(phone)
    except Exception as e:  # noqa: BLE001 — sin DB no se puede afirmar nada
        log.warning("consent_marketing.detectar error phone=...%s: %s", phone[-4:], e)
        return None
    if es_boton_si and hubo_plantilla:
        return "accepted"
    if ultimo_es_plantilla:
        return "accepted" if es_si else "declined"
    return None


def registrar(phone: str, status: str, texto: str, via: str) -> None:
    """Guarda la respuesta en bi.marketing_consent. Un "sí" posterior a un "no"
    reemplaza la negativa y saca al paciente de opt_outs_marketing (es su
    voluntad vigente); un "no" lo excluye de marketing, como antes."""
    from winback import (registrar_consent_respuesta, remover_opt_out_marketing,
                         registrar_opt_out_marketing)
    from session import log_event
    registrar_consent_respuesta(phone, status, method="reply")
    try:
        if status == "accepted":
            remover_opt_out_marketing(phone)
        else:
            registrar_opt_out_marketing(phone, source="consent_marketing_v2",
                                        reason="declined_marketing")
    except Exception as e:  # noqa: BLE001
        log.warning("consent_marketing opt-out sync error phone=...%s: %s", phone[-4:], e)
    log_event(phone, "marketing_consent_respuesta",
              {"status": status, "raw": (texto or "")[:120], "via": via})
