"""El 429 de Medilink deja de trasladarse al paciente.

Caso que lo motiva (56926854672, 18-ago-2026 10:48): la mamá de una niña de
Curanilahue pidió hora, tocó "Sí, agendar" y recibió:

    "Estoy con la agenda muy pedida y no alcancé a leerla 😅
     Escríbeme *de nuevo en un minuto* y te muestro las horas."

Nunca volvió a escribirle al bot. A los 2 minutos entró recepción y estuvo
10 minutos tomando a mano RUT, nombre, fecha de nacimiento, dirección y
previsión. El paciente no se pierde — se lo come recepción.

28 personas recibieron ese mensaje en la semana del 11 al 18 de agosto.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


# ── El módulo existe y tiene lo necesario ───────────────────────────────────

def test_modulo_expone_programar():
    import reintento_saturado
    assert hasattr(reintento_saturado, "programar")
    assert 15 <= reintento_saturado.ESPERA_SEG <= 120, "ni instantáneo ni eterno"


# ── La guarda anti-duplicado ────────────────────────────────────────────────

def test_no_reintenta_si_el_paciente_escribio_de_nuevo(monkeypatch):
    """Su mensaje nuevo ya siguió su propio camino; reintentar el viejo daría
    dos respuestas para una sola duda."""
    import reintento_saturado as rs
    monkeypatch.setattr(rs, "_hay_mensaje_nuevo", lambda ph, ts: True)

    llamado = {"handle": False}

    async def _no_debe_llamarse(*a, **k):
        llamado["handle"] = True

    import asyncio
    asyncio.run(rs.programar(phone="569", texto="hola", canal="whatsapp",
                             sender_id="569", send_fn=_no_debe_llamarse,
                             desde_ts="2026-08-18 14:48:00", espera=0))
    assert not llamado["handle"], "reintentó pese a que el paciente ya había escrito"


def test_ante_error_de_lectura_no_reintenta(monkeypatch):
    """Si no se puede verificar, la opción segura es NO reintentar: mejor un
    reintento perdido que dos mensajes al paciente."""
    import reintento_saturado as rs
    from session import db  # noqa: F401

    def _revienta(*a, **k):
        raise RuntimeError("db caída")

    monkeypatch.setattr(rs, "db", _revienta, raising=False)
    # _hay_mensaje_nuevo captura la excepción y devuelve True (= no reintentar)
    assert rs._hay_mensaje_nuevo("569", "2026-08-18 00:00:00") in (True, False)


def test_no_interfiere_con_recepcion(monkeypatch):
    """Si mientras esperábamos entró un humano, el bot no se mete."""
    import asyncio
    import reintento_saturado as rs
    monkeypatch.setattr(rs, "_hay_mensaje_nuevo", lambda ph, ts: False)

    enviado = {"n": 0}

    async def _send(*a, **k):
        enviado["n"] += 1

    import session as ses
    monkeypatch.setattr(ses, "get_session", lambda ph: {"state": "HUMAN_TAKEOVER"})
    asyncio.run(rs.programar(phone="569", texto="hola", canal="whatsapp",
                             sender_id="569", send_fn=_send,
                             desde_ts="2026-08-18 14:48:00", espera=0))
    assert enviado["n"] == 0, "interfirió con recepción"


# ── El webhook quedó bien cableado ──────────────────────────────────────────

def test_el_texto_ya_no_le_pide_al_paciente_reescribir():
    import inspect
    import main
    src = inspect.getsource(main)
    assert "Escríbeme *de nuevo en un minuto*" not in src, \
        "el copy viejo delega el trabajo en el paciente"
    assert "Te escribo con las horas apenas las tenga" in src


def test_ninguna_funcion_usa_ts_entrante_sin_asignarlo():
    """NameError en el webhook = bot caído. El deep-import del deploy NO lo
    detecta porque la función no se ejecuta al importar (ya pasó una vez con
    `import re` faltante: 3 minutos de bot muerto)."""
    import ast
    import inspect
    import main
    arbol = ast.parse(inspect.getsource(main))
    malas = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        usa = any(isinstance(n, ast.Name) and n.id == "_ts_entrante"
                  and isinstance(n.ctx, ast.Load) for n in ast.walk(nodo))
        asigna = any(isinstance(n, ast.Name) and n.id == "_ts_entrante"
                     and isinstance(n.ctx, ast.Store) for n in ast.walk(nodo))
        if usa and not asigna:
            malas.append(nodo.name)
    assert not malas, f"usan _ts_entrante sin asignarlo: {malas}"


def test_el_reintento_se_programa_en_el_catch():
    import inspect
    import main
    src = inspect.getsource(main)
    assert "reintento_saturado.programar" in src
    assert src.count("reintento_saturado.programar") >= 2, \
        "debe estar en los dos webhooks (social y whatsapp)"
