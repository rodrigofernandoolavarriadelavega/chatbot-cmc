"""
FIX-3: Staff whitelist — números internos del CMC que NO deben pasar por el
flujo de pacientes. Si un miembro del staff escribe al canal público, el bot
los deriva directo a HUMAN_TAKEOVER con un saludo corto.

La lista combina:
1. STAFF_PHONES de config.py (viene del .env, configurable por servidor).
2. _EXTRA_PHONES: lista hardcodeada para casos conocidos que no están en .env.

Para agregar vía panel admin usar el endpoint POST /admin/api/staff/add.
Para agregar permanentemente, editar STAFF_PHONES en .env del servidor.

Phones en formato canónico sin '+' (ej: "56938738734").
"""
import logging

log = logging.getLogger("bot.staff")

# Números conocidos hardcodeados aquí (respaldo por si .env no está actualizado).
# ADD_STAFF_HERE: agregar según aparezcan en logs:
#   SELECT phone, COUNT(*) as n FROM conversation_events
#   WHERE event='derivado_humano' AND ts > datetime('now','-30 days')
#   GROUP BY phone ORDER BY n DESC LIMIT 20;
_EXTRA_PHONES: dict[str, str] = {
    # BUG-K: Dra. Javiera Burgos (dentista), 71 msg basura/semana — auditoría may 2026.
    # Este número fue reportado en el audit como el de Burgos. Si el número de Rejón
    # es distinto, agregarlo por separado cuando se confirme.
    "56938738734": "Dra. Javiera Burgos",
}

# Runtime additions (via endpoint /admin/api/staff/add, volátil — se pierde al reiniciar)
_runtime_phones: dict[str, str] = {}


def _merged() -> dict[str, str]:
    """Combina .env + hardcoded + runtime additions."""
    try:
        from config import STAFF_PHONES as _cfg
    except Exception:
        _cfg = {}
    merged = {**_EXTRA_PHONES, **_cfg, **_runtime_phones}
    return merged


def is_staff(phone: str) -> bool:
    """Retorna True si el phone pertenece a un miembro del staff."""
    return phone in _merged()


def get_staff_name(phone: str) -> str:
    """Retorna nombre del staff o string vacío si no está en la lista."""
    return _merged().get(phone, "")


def get_all_staff() -> dict[str, str]:
    """Retorna copia del mapa completo (para el panel admin)."""
    return dict(_merged())


def add_staff_runtime(phone: str, nombre: str) -> None:
    """Agrega un número al whitelist en runtime (volátil). Para persistir, usar .env."""
    _runtime_phones[phone] = nombre
    log.info("staff_whitelist: agregado %s (%s) en runtime", phone, nombre)


def remove_staff_runtime(phone: str) -> bool:
    """Elimina un número del runtime whitelist. Retorna True si existía."""
    if phone in _runtime_phones:
        del _runtime_phones[phone]
        return True
    return False
