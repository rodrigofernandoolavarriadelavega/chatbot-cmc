"""Nombre canónico de profesional — fuente única de verdad.

Problema que resuelve: en pagos_cmc (y otros lados) el mismo profesional
aparece con strings distintos según cómo lo tipeó recepción:
  id=55 → "Dra. Javiera Burgos" / "Javiera Burgos Godoy"
  id=67 → "Sarai Gómez" / "Sarai Goméz Miere"
  id=21 → "Leonardo Etcheverry" / "Leonardo Etcheverry Rebolledo"
  id=56 → "Andrea Guevara" / "Andrea Guevara Andrea Guevara"  (duplicado literal)

Como las filas SÍ traen id_profesional correcto, la canonicalización es por id:
el nombre canónico sale de medilink.PROFESIONALES[id]["nombre"].

Si el id es desconocido (no está en el dict), se conserva el string crudo
limpiado (colapsa espacios + quita duplicación literal "X X").
"""
import re

# Alias extra para ids ausentes del dict (ej. profesionales históricos).
# Se mapea por nombre normalizado → nombre canónico deseado.
_ALIAS_SIN_ID: dict[str, str] = {}


def _clean(raw: str) -> str:
    """Limpia un nombre crudo: colapsa espacios y des-duplica 'Nombre Nombre'."""
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s:
        return s
    # "Andrea Guevara Andrea Guevara" → "Andrea Guevara"
    mid = len(s) // 2
    if len(s) % 2 == 1 and s[mid] == " " and s[:mid] == s[mid + 1:]:
        s = s[:mid]
    return s


def load_canonical_map() -> dict[int, dict]:
    """Carga el mapa id→{nombre,...} de medilink. LANZA si falla.

    Pensado para procesos batch (backfill) que NO deben degradar en silencio:
    si el mapa no carga, un backfill se volvería no-op y "consolidaría" 0 filas
    sin avisar. Mejor que reviente. El runtime (canonical_name) sí degrada suave.
    """
    from medilink import PROFESIONALES  # ImportError/etc. se propaga a propósito
    return PROFESIONALES


def canonical_name(id_prof: int | None, raw_name: str = "") -> str:
    """Devuelve el nombre canónico del profesional.

    Prioridad: medilink.PROFESIONALES[id] → alias sin id → string crudo limpiado.
    Nunca lanza: si todo falla, retorna el crudo limpiado (o '').
    """
    try:
        from medilink import PROFESIONALES
    except Exception:
        PROFESIONALES = {}

    if id_prof and id_prof in PROFESIONALES:
        return PROFESIONALES[id_prof]["nombre"]

    cleaned = _clean(raw_name)
    key = cleaned.lower()
    if key in _ALIAS_SIN_ID:
        return _ALIAS_SIN_ID[key]
    return cleaned
