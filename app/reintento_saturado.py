"""Reintento automático cuando Medilink está saturado (429).

## El problema

Cuando Medilink devuelve 429 sostenido, `medilink._agotado()` lanza
`MedilinkRateLimited` y el webhook respondía al paciente:

    "Estoy con la agenda muy pedida y no alcancé a leerla 😅
     Escríbeme *de nuevo en un minuto* y te muestro las horas."

Le pasa al paciente el trabajo que le toca al bot. Y el paciente no vuelve.

Caso real (56926854672, 18-ago-2026 10:48): mamá de una niña de Curanilahue
pidió hora, tocó "Sí, agendar", recibió ese mensaje y **nunca volvió a
escribirle al bot**. A los 2 minutos entró recepción y estuvo 10 minutos
tomando a mano RUT, nombre, fecha de nacimiento, dirección y previsión.

El paciente no se pierde — se lo come recepción. En la semana del 11 al 18 de
agosto, 28 personas recibieron ese mensaje: ~4,7 horas/mes de trabajo humano
causadas por un texto que delega en el paciente.

## Qué hace este módulo

En vez de pedirle al paciente que reescriba, el bot **reintenta solo** a los
`ESPERA_SEG` segundos y le manda las horas cuando salgan. Un 429 dura
segundos: para cuando el paciente termina de leer el mensaje, ya se resolvió.

## Por qué no duplica respuestas

Antes de reintentar comprueba que el paciente NO haya escrito nada nuevo desde
el mensaje que falló. Si escribió, ese mensaje ya siguió su propio camino por
el webhook y este reintento se descarta en silencio.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("reintento_saturado")

# Un 429 de Medilink se resuelve en segundos. 45 s da margen de sobra sin que
# el paciente sienta que lo dejaron colgado.
ESPERA_SEG = 45


def _hay_mensaje_nuevo(phone: str, desde_ts: str) -> bool:
    """True si el paciente escribió algo después del mensaje que falló.

    En ese caso su mensaje nuevo ya fue procesado por el webhook con su propio
    flujo, y reintentar el viejo produciría dos respuestas para una sola duda.
    """
    try:
        from session import db
        with db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE phone=? AND direction='in' AND ts > ?",
                (phone, desde_ts),
            ).fetchone()
        return bool(row and row[0])
    except Exception as e:  # noqa: BLE001 — ante la duda, NO reintentar
        log.warning("reintento_saturado: no se pudo verificar mensajes nuevos: %s", e)
        return True


async def programar(phone: str, texto: str, canal: str, sender_id: str,
                    send_fn, desde_ts: str, espera: int = ESPERA_SEG) -> None:
    """Espera y reprocesa el mensaje del paciente. Best-effort: si vuelve a
    fallar, recién ahí se le avisa que llame a recepción.

    `desde_ts` es el timestamp del mensaje entrante que gatilló el 429 — se usa
    para detectar si el paciente escribió mientras esperábamos.
    """
    try:
        await asyncio.sleep(espera)

        if _hay_mensaje_nuevo(phone, desde_ts):
            log.info("reintento_saturado: %s escribió de nuevo, se descarta el reintento", phone)
            return

        from flows import handle_message
        from session import get_session, log_message

        session = get_session(phone)
        # Si mientras esperábamos entró recepción, no interferir.
        if session.get("state") == "HUMAN_TAKEOVER":
            log.info("reintento_saturado: %s quedó en HUMAN_TAKEOVER, no se reintenta", phone)
            return

        respuesta = await handle_message(phone, texto, session)
        if not respuesta:
            return

        if isinstance(respuesta, dict) and respuesta.get("type") == "interactive":
            from main import _interactive_to_text
            resp_text = _interactive_to_text(respuesta, include_promo=True)
        else:
            resp_text = str(respuesta)

        if not resp_text.strip():
            return

        estado = get_session(phone).get("state", "IDLE")
        await send_fn(sender_id, resp_text)
        log_message(phone, "out", resp_text, estado, canal=canal)
        log.info("reintento_saturado OK para %s → estado=%s", phone, estado)

    except Exception as e:  # noqa: BLE001 — nunca romper por un reintento
        log.warning("reintento_saturado falló para %s: %s", phone, e)
