"""Invariantes de seguridad de `confirmar_sugerencia` — la única función del
bloque de conciliación que escribe sobre `pagos_cmc`.

Por qué merece tests propios: `pagos_cmc.metodo_pago` es la fuente del medio de
pago en el Libro de la Verdad del centro. Un emparejamiento mal aplicado no es
un bug cosmético — deja la caja diciendo que una atención se pagó por
transferencia cuando se pagó en efectivo, y eso se arrastra a todos los informes
hacia atrás.

Los cuatro candados que se fijan acá:
  1. El `pago_cmc_id` tiene que ser uno de los candidatos de ESA sugerencia
     (si no, el endpoint sería un "escribe en cualquier fila que me pidas").
  2. No se confirma dos veces.
  3. No se pisa una fila bloqueada.
  4. No se pisa una fila que ya tiene copago (alguien cobró en efectivo entre
     que se sugirió y se confirmó) — y en ese caso la sugerencia se descarta
     sola en vez de quedar colgada.

TRAMPA encontrada escribiendo estos tests: `pagos_cmc.metodo_pago` tiene
DEFAULT 'efectivo'. Una fila que nadie tocó NO dice NULL, dice "efectivo" —
así que "no se escribió nada" se verifica contra 'transferencia' y contra el
copago, nunca contra NULL.

Correr: venv/bin/python3 tests/test_confirmar_sugerencia_seguridad.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import session  # noqa: E402
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
session.DB_PATH = Path(_tmp.name)

import pagos_transferencia_sugeridos as pts  # noqa: E402
from pagos_routes import ensure_pagos_table  # noqa: E402

_FALLOS: list[str] = []


def _ok(cond, msg):
    print(("OK    " if cond else "FALLO ") + msg)
    if not cond:
        _FALLOS.append(msg)


def _crear_pago(bloqueado=0, copago=None):
    """Inserta una fila de pagos_cmc como la deja el prellenado de agenda."""
    ensure_pagos_table()
    with session.db() as c:
        cur = c.execute(
            "INSERT INTO pagos_cmc (fecha, hora, paciente_nombre, bloqueado, copago) "
            "VALUES (?,?,?,?,?)",
            ("2026-07-28", "10:30", "Paciente Prueba", bloqueado, copago),
        )
        return cur.lastrowid


def _crear_sugerencia(uid, candidatos, monto=15000):
    pts.ensure_pagos_sugeridos_table()
    with session.db() as c:
        cur = c.execute(
            "INSERT INTO pagos_sugeridos (uid_email, banco, nombre_transfiere, monto, "
            "fecha, hora, num_operacion, candidatos_json, estado) "
            "VALUES (?,?,?,?,?,?,?,?,'pendiente')",
            (uid, "bancoestado", "Juan Perez", monto, "2026-07-28", "10:00",
             "123456", json.dumps(candidatos)),
        )
        return cur.lastrowid


def _leer_pago(pago_id):
    with session.db() as c:
        r = c.execute("SELECT * FROM pagos_cmc WHERE id=?", (pago_id,)).fetchone()
        return dict(r) if r else None


def test_solo_escribe_en_un_candidato_declarado():
    """El candado principal: no se puede apuntar a una fila arbitraria."""
    pago_legitimo = _crear_pago()
    pago_ajeno = _crear_pago()
    sug = _crear_sugerencia(9001, [{"id": pago_legitimo}])

    r = pts.confirmar_sugerencia(sug, pago_ajeno, "test")
    _ok(r["ok"] is False, "confirmar contra un pago que NO es candidato falla")
    _ok("candidatos" in r.get("error", ""), "el error dice explícitamente por qué")

    ajeno = _leer_pago(pago_ajeno)
    # OJO: pagos_cmc.metodo_pago trae DEFAULT 'efectivo', así que "intacta" NO
    # es NULL — es que siga en efectivo y sin copago.
    _ok(ajeno["metodo_pago"] != "transferencia" and not ajeno["copago"],
        "la fila ajena quedó intacta — no se escribió nada en ella")


def test_camino_feliz_escribe_lo_esperado():
    pago = _crear_pago()
    sug = _crear_sugerencia(9002, [{"id": pago}], monto=23000)

    r = pts.confirmar_sugerencia(sug, pago, "test:abc123")
    _ok(r["ok"] is True, "confirmar un candidato válido funciona")

    fila = _leer_pago(pago)
    _ok(fila["metodo_pago"] == "transferencia", "queda marcada como transferencia")
    _ok(fila["copago"] == 23000, "el copago queda con el monto del correo del banco")
    _ok(fila["match_confianza"] == "transferencia_sugerida",
        "queda trazado CÓMO se decidió (no se confunde con un cobro manual)")

    with session.db() as c:
        s = dict(c.execute("SELECT * FROM pagos_sugeridos WHERE id=?", (sug,)).fetchone())
    _ok(s["estado"] == "confirmado", "la sugerencia queda confirmada")
    _ok(s["resuelto_por"] == "test:abc123", "queda registrado QUIÉN lo confirmó")
    _ok(s["elegido_pago_cmc_id"] == pago, "queda registrado SOBRE QUÉ fila se aplicó")


def test_no_se_confirma_dos_veces():
    pago = _crear_pago()
    sug = _crear_sugerencia(9003, [{"id": pago}])

    pts.confirmar_sugerencia(sug, pago, "test")
    r2 = pts.confirmar_sugerencia(sug, pago, "test")
    _ok(r2["ok"] is False, "el segundo intento sobre la misma sugerencia falla")
    _ok("ya estaba en estado" in r2.get("error", ""),
        "el error explica que ya estaba resuelta (doble clic en el panel)")


def test_no_pisa_fila_bloqueada():
    pago = _crear_pago(bloqueado=1)
    sug = _crear_sugerencia(9004, [{"id": pago}])

    r = pts.confirmar_sugerencia(sug, pago, "test")
    _ok(r["ok"] is False, "una fila bloqueada no se toca")
    fila = _leer_pago(pago)
    _ok(fila["metodo_pago"] != "transferencia" and not fila["copago"],
        "la fila bloqueada quedó intacta")


def test_no_pisa_cobro_hecho_entre_medio():
    """Carrera real: recepción cobró en efectivo mientras la sugerencia esperaba."""
    pago = _crear_pago(copago=15000)
    sug = _crear_sugerencia(9005, [{"id": pago}])

    r = pts.confirmar_sugerencia(sug, pago, "test")
    _ok(r["ok"] is False, "no se pisa una atención ya cobrada por otro medio")

    fila = _leer_pago(pago)
    _ok(fila["copago"] == 15000, "el cobro que ya existía se mantiene")

    with session.db() as c:
        s = dict(c.execute("SELECT * FROM pagos_sugeridos WHERE id=?", (sug,)).fetchone())
    _ok(s["estado"] == "descartado",
        "la sugerencia se descarta sola en vez de quedar colgada para siempre")


def test_nada_aplica_sugerencias_sin_humano():
    """La regla de negocio: el automatismo propone, la persona dispone."""
    import subprocess
    raiz = os.path.join(os.path.dirname(__file__), "..", "app")
    salida = subprocess.run(
        ["grep", "-rn", "confirmar_sugerencia", raiz],
        capture_output=True, text=True).stdout
    llamadas = [l for l in salida.splitlines()
                if "confirmar_sugerencia(" in l
                and "def confirmar_sugerencia" not in l
                and not l.split(":", 2)[2].strip().startswith("#")
                and "- Confirmar" not in l]
    _ok(len(llamadas) == 1,
        f"existe exactamente UN llamador (el endpoint con auth), no un cron. Encontrados: {len(llamadas)}")
    _ok(all("routes" in l.split(":")[0] for l in llamadas),
        "ese único llamador vive en una ruta HTTP, o sea siempre hay una persona detrás")


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
    print("\n" + ("── TODO OK ──" if not _FALLOS else f"── FALLARON {len(_FALLOS)} ──"))
    sys.exit(1 if _FALLOS else 0)
