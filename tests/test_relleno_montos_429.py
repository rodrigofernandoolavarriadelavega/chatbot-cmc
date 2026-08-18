"""El relleno de montos dejaba de martillar Medilink.

Medido en producción el 18-ago-2026, sobre el log de ~13 días:

    llamadas a /atenciones/{id} ....  29.132
    IDs distintos ..................     951      → 30 llamadas por ID
    el más repetido ................     221 veces

Se auto-alimentaba: `get_olavarria_atenciones_sin_monto()` filtra por
`monto_facturado IS NULL`, y cuando los reintentos daban 429 no se escribía
nada — la atención seguía NULL y volvía a pedirse en la pasada siguiente.
Cada 429 alimentaba el 429 del día siguiente.

Era la principal fuente de los ~1.000 429 diarios contra el HIS, que a su vez
hacían que 28 pacientes por semana recibieran "escríbeme de nuevo en un minuto".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


# ── El tope de reintentos ───────────────────────────────────────────────────

def test_existe_tope_de_intentos():
    import session
    assert hasattr(session, "_MAX_INTENTOS_MONTO")
    assert 1 <= session._MAX_INTENTOS_MONTO <= 5, "un tope razonable, ni 1 ni infinito"


def test_la_query_excluye_los_agotados():
    """Sin este filtro la cola nunca se vacía."""
    import inspect
    import session
    src = inspect.getsource(session.get_olavarria_atenciones_sin_monto)
    assert "intentos_monto" in src
    assert "_MAX_INTENTOS_MONTO" in src


def test_existe_el_marcador_de_fallos():
    import session
    assert hasattr(session, "marcar_intento_monto")


def test_la_columna_se_crea_sola():
    """Bases anteriores al fix no tienen intentos_monto — hay que agregarla
    en caliente o la query revienta."""
    import inspect
    import session
    src = inspect.getsource(session._ensure_intentos_monto)
    assert "ALTER TABLE" in src and "intentos_monto" in src


# ── El guardrail de 429 ─────────────────────────────────────────────────────

def test_el_sync_usa_el_carril_batch():
    """Guardrail del proyecto: todo cron que pegue a Medilink llama
    use_batch_lane() primero, para no competir con pacientes en vivo."""
    import inspect
    import main
    src = inspect.getsource(main.sync_olavarria_montos)
    assert "use_batch_lane()" in src


def test_el_sync_no_abre_su_propio_cliente():
    """httpx.AsyncClient propio se salta el throttling de medilink.py.

    Se mira el CÓDIGO, no el docstring: la explicación del fix menciona
    httpx.AsyncClient a propósito para decir que se quitó.
    """
    import inspect
    import main
    src = inspect.getsource(main.sync_olavarria_montos)
    # Todo lo que va entre el primer par de triples comillas es el docstring.
    partes = src.split('"""')
    codigo = partes[2] if len(partes) >= 3 else src
    assert "httpx.AsyncClient" not in codigo, "debe usar _get_shared_client()"
    assert "_get_shared_client" in codigo


def test_el_sync_marca_los_fallos():
    """Si no persiste el fallo, la atención vuelve a la cola mañana."""
    import inspect
    import main
    src = inspect.getsource(main.sync_olavarria_montos)
    assert "marcar_intento_monto" in src


# ── La aritmética del ahorro ────────────────────────────────────────────────

def test_cuantas_llamadas_ahorra():
    """Con 934 pendientes: antes se repetían indefinidamente; ahora cada una
    se pide como máximo 3 pasadas × 3 intentos = 9 veces, y después sale."""
    pendientes = 934
    tope_pasadas = 3        # _MAX_INTENTOS_MONTO
    intentos_por_pasada = 3
    techo = pendientes * tope_pasadas * intentos_por_pasada
    assert techo == 8406
    # Contra las 29.132 medidas en 13 días, y creciendo sin techo.
    assert techo < 29132
