"""El flag CONCILIACION_TRANSFERENCIAS_ACTIVE tiene que significar INERTE.

Antes de este test (2026-07-28) el bloque de conciliación tenía dos caminos hacia
el Gmail del centro y solo uno se podía apagar:

  - el cron `conciliacion_transferencias_poll` se registraba SIEMPRE, así que
    deployar el bloque bastaba para empezar a leer el buzón cada 10 minutos;
  - el endpoint `POST /api/conciliacion-transferencias/backfill` recorría el
    buzón COMPLETO sin mirar ningún flag.

"Apagado" tiene que querer decir que el sistema no sale a buscar correo. Lo que
solo LEE tablas locales (`conciliar`, `estado_backfill`) sigue disponible a
propósito: sirve para revisar lo ya conciliado con el sistema apagado.

Correr: venv/bin/python3 tests/test_conciliacion_gating.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import session  # noqa: E402
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
session.DB_PATH = Path(_tmp.name)

import config  # noqa: E402
from fastapi import HTTPException  # noqa: E402

_FALLOS: list[str] = []


def _ok(cond, msg):
    print(("OK    " if cond else "FALLO ") + msg)
    if not cond:
        _FALLOS.append(msg)


def test_flag_existe_y_viene_apagado():
    _ok(hasattr(config, "CONCILIACION_TRANSFERENCIAS_ACTIVE"),
        "el flag CONCILIACION_TRANSFERENCIAS_ACTIVE existe en config")
    _ok(config.CONCILIACION_TRANSFERENCIAS_ACTIVE is False,
        "viene apagado por defecto (leer correo es una decisión explícita)")


def test_backfill_bloqueado_con_flag_apagado():
    import conciliacion_transferencias_routes as rutas
    original = config.CONCILIACION_TRANSFERENCIAS_ACTIVE
    config.CONCILIACION_TRANSFERENCIAS_ACTIVE = False
    try:
        try:
            rutas._requiere_flag()
            salio = "no bloqueó"
        except HTTPException as e:
            salio = e.status_code
    finally:
        config.CONCILIACION_TRANSFERENCIAS_ACTIVE = original
    _ok(salio == 503,
        f"con el flag apagado el backfill responde 503, no abre IMAP (salió: {salio})")


def test_backfill_permitido_con_flag_encendido():
    import conciliacion_transferencias_routes as rutas
    original = config.CONCILIACION_TRANSFERENCIAS_ACTIVE
    config.CONCILIACION_TRANSFERENCIAS_ACTIVE = True
    try:
        try:
            rutas._requiere_flag()
            paso = True
        except HTTPException:
            paso = False
    finally:
        config.CONCILIACION_TRANSFERENCIAS_ACTIVE = original
    _ok(paso, "con el flag encendido el backfill sí procede")


def test_lectura_local_no_pasa_por_el_flag():
    """`conciliar` y `estado_backfill` solo tocan tablas locales."""
    import inspect

    import conciliacion_transferencias as ct
    fuente_conciliar = inspect.getsource(ct.conciliar)
    fuente_estado = inspect.getsource(ct.estado_backfill)
    for nombre, fuente in (("conciliar", fuente_conciliar), ("estado_backfill", fuente_estado)):
        _ok("imap" not in fuente.lower() and "_connect" not in fuente,
            f"{nombre}() no abre IMAP — puede consultarse con el sistema apagado")


def test_el_cron_solo_se_registra_con_el_flag():
    """Verifica en el código fuente de main que el add_job está dentro del if."""
    import inspect

    import main
    fuente = inspect.getsource(main)
    i_flag = fuente.find("CONCILIACION_TRANSFERENCIAS_ACTIVE as _CONCIL_ACTIVE")
    i_job = fuente.find('id="conciliacion_transferencias_poll"')
    _ok(i_flag != -1, "main.py lee el flag antes de registrar el cron")
    _ok(i_job != -1 and i_flag < i_job,
        "el add_job del poll está DESPUÉS de la comprobación del flag")
    entre = fuente[i_flag:i_job] if (i_flag != -1 and i_job != -1) else ""
    _ok("if _CONCIL_ACTIVE:" in entre,
        "el add_job está dentro del `if _CONCIL_ACTIVE:`")


def test_hay_guarda_de_concurrencia_en_backfill():
    import inspect

    import conciliacion_transferencias_routes as rutas
    fuente = inspect.getsource(rutas.register_conciliacion_transferencias_routes)
    _ok("_BACKFILL_TASK" in fuente and "409" in fuente,
        "dos backfills simultáneos se rechazan con 409 (antes lanzaba dos barridos)")
    _ok("add_done_callback" in fuente,
        "la task guarda referencia y reporta su excepción en vez de morir en silencio")


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
    print("\n" + ("── TODO OK ──" if not _FALLOS else f"── FALLARON {len(_FALLOS)} ──"))
    sys.exit(1 if _FALLOS else 0)
