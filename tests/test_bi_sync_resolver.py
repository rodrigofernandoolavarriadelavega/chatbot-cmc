"""
Test de regresión para `app/bi_sync.py::_resolver_profesional_pago` y las
funciones que orquesta (`_resolver_profesional_pagos_cmc`, `_resolver_atencion_id`).

Contexto (docs/LIBRO_DE_LA_VERDAD.md sección 3): el commit b090e9c0
(2026-06-12) agregó un "NIVEL 0.5" que resolvía el `id_profesional` cruzando
contra `pagos_cmc` y retornaba de inmediato con `atencion_id=None`, cortando
la ejecución ANTES de llegar a la cascada heurística que busca el
`atencion_id` en `bi_atenciones`. Efecto medido en producción: la tasa de
pagos sin `atencion_id` subió de 0% (abril 2026) a 87-93% (junio/julio 2026).

Este archivo verifica dos cosas a la vez, porque son las dos mitades del
mismo contrato:
  1. El NIVEL 0.5 ya NO corta la búsqueda de `atencion_id` cuando SÍ hay una
     atención real que matchea.
  2. La atribución del PROFESIONAL sigue siendo correcta incluso cuando el
     paciente tiene atenciones de varios profesionales el mismo día (el bug
     ORIGINAL que b090e9c0 corrigió — no debe reabrirse).

Ejecución:
    PYTHONPATH=app:. venv/bin/python tests/test_bi_sync_resolver.py

No toca producción, no requiere SQLCIPHER_KEY, no llama a Medilink.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import bi_sync  # noqa: E402


def _mkdb() -> sqlite3.Connection:
    """DB en memoria con el subconjunto de esquema que el resolver necesita."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE bi_atenciones (
            atencion_id INTEGER PRIMARY KEY, fecha TEXT, id_paciente INTEGER,
            id_profesional INTEGER, total INTEGER, abonado INTEGER, deuda INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE pagos_cmc (
            id INTEGER PRIMARY KEY, fecha TEXT, paciente_nombre TEXT,
            id_profesional INTEGER, copago INTEGER, bonificacion INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE bi_pago_overrides (
            pago_id INTEGER PRIMARY KEY, id_profesional INTEGER, atencion_id INTEGER
        )
    """)
    return c


PASS = 0
FAIL = 0


def check(nombre: str, cond: bool, detalle: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {nombre}")
    else:
        FAIL += 1
        print(f"  FAIL {nombre}  {detalle}")


def test_nivel_0_5_no_corta_busqueda_de_atencion_id():
    """Caso central del bug: pagos_cmc resuelve un único profesional (Abarca),
    y existe una atención real de ESE profesional que matchea por monto+fecha.
    Antes del fix: (73, None). Después: (73, <atencion_id real>)."""
    c = _mkdb()
    c.execute("INSERT INTO pagos_cmc (fecha, paciente_nombre, id_profesional, copago, bonificacion) "
              "VALUES ('2026-06-10', 'Juan Perez', 73, 15000, 0)")
    c.execute("INSERT INTO bi_atenciones (atencion_id, fecha, id_paciente, id_profesional, "
              "total, abonado, deuda) VALUES (9001, '2026-06-10', 55, 73, 15000, 15000, 0)")
    pago = {"id": 1, "id_paciente": 55, "fecha_recepcion": "2026-06-10",
            "monto_pago": 15000, "nombre_paciente": "Juan Perez"}
    prof, aten = bi_sync._resolver_profesional_pago(c, pago)
    check("nivel 0.5 resuelve profesional Y atencion_id (no None)",
          prof == 73 and aten == 9001, f"got prof={prof} aten={aten}")


def test_atribucion_profesional_no_se_rompe_multiples_profesionales_mismo_dia():
    """El bug ORIGINAL que b090e9c0 corrigió: paciente con atenciones de DOS
    profesionales el mismo día (Márquez y Quijano), monto ambiguo entre ambas
    atenciones. pagos_cmc (recepción) dice que el pago es de Quijano — eso
    debe ganar, y el atencion_id encontrado debe ser el de Quijano, NUNCA el
    de Márquez aunque su atención matchee igual de bien por monto."""
    c = _mkdb()
    c.execute("INSERT INTO pagos_cmc (fecha, paciente_nombre, id_profesional, copago, bonificacion) "
              "VALUES ('2026-06-11', 'Maria Soto', 65, 35000, 0)")  # Quijano
    # Atención de Márquez (13) el mismo día, mismo monto — candidata ambigua
    # si no se filtrara por profesional.
    c.execute("INSERT INTO bi_atenciones (atencion_id, fecha, id_paciente, id_profesional, "
              "total, abonado, deuda) VALUES (9101, '2026-06-11', 88, 13, 35000, 35000, 0)")
    # Atención real de Quijano (65) el mismo día, mismo monto.
    c.execute("INSERT INTO bi_atenciones (atencion_id, fecha, id_paciente, id_profesional, "
              "total, abonado, deuda) VALUES (9102, '2026-06-11', 88, 65, 35000, 35000, 0)")
    pago = {"id": 2, "id_paciente": 88, "fecha_recepcion": "2026-06-11",
            "monto_pago": 35000, "nombre_paciente": "Maria Soto"}
    prof, aten = bi_sync._resolver_profesional_pago(c, pago)
    check("profesional correcto (Quijano=65, no Márquez=13)", prof == 65, f"got prof={prof}")
    check("atencion_id es la de Quijano (9102, no 9101 de Márquez)",
          aten == 9102, f"got aten={aten}")


def test_nivel_0_5_sin_atencion_matcheable_no_inventa_nada():
    """Profesional resuelto por 0.5, pero el paciente no tiene NINGUNA
    atención registrada (huérfano genuino, no por atajo de código). Debe
    devolver (profesional, None), no reventar."""
    c = _mkdb()
    c.execute("INSERT INTO pagos_cmc (fecha, paciente_nombre, id_profesional, copago, bonificacion) "
              "VALUES ('2026-07-01', 'Pedro Diaz', 1, 5000, 0)")
    pago = {"id": 3, "id_paciente": 200, "fecha_recepcion": "2026-07-01",
            "monto_pago": 5000, "nombre_paciente": "Pedro Diaz"}
    prof, aten = bi_sync._resolver_profesional_pago(c, pago)
    check("profesional resuelto igual (huérfano genuino)", prof == 1, f"got prof={prof}")
    check("atencion_id None genuino (no hay atenciones)", aten is None, f"got aten={aten}")


def test_override_nivel_0_sigue_siendo_autoridad_absoluta():
    """Un override manual debe ganarle a todo, incluyendo pagos_cmc."""
    c = _mkdb()
    c.execute("INSERT INTO bi_pago_overrides (pago_id, id_profesional, atencion_id) "
              "VALUES (4, 999, 7777)")
    c.execute("INSERT INTO pagos_cmc (fecha, paciente_nombre, id_profesional, copago, bonificacion) "
              "VALUES ('2026-07-02', 'Ana Rios', 1, 5000, 0)")
    pago = {"id": 4, "id_paciente": 300, "fecha_recepcion": "2026-07-02",
            "monto_pago": 5000, "nombre_paciente": "Ana Rios"}
    prof, aten = bi_sync._resolver_profesional_pago(c, pago)
    check("override gana por sobre pagos_cmc", (prof, aten) == (999, 7777), f"got {(prof, aten)}")


def test_sin_match_en_pagos_cmc_cascada_infiere_ambos():
    """Sin match en pagos_cmc (nombre distinto), la cascada histórica sigue
    infiriendo profesional Y atencion_id desde bi_atenciones (comportamiento
    pre-existente, no debe romperse)."""
    c = _mkdb()
    c.execute("INSERT INTO bi_atenciones (atencion_id, fecha, id_paciente, id_profesional, "
              "total, abonado, deuda) VALUES (9201, '2026-07-03', 400, 60, 20000, 20000, 0)")
    pago = {"id": 5, "id_paciente": 400, "fecha_recepcion": "2026-07-03",
            "monto_pago": 20000, "nombre_paciente": "Nombre Que No Esta En Pagos Cmc"}
    prof, aten = bi_sync._resolver_profesional_pago(c, pago)
    check("cascada infiere profesional sin pagos_cmc", prof == 60, f"got prof={prof}")
    check("cascada infiere atencion_id sin pagos_cmc", aten == 9201, f"got aten={aten}")


def test_nivel_0_5_no_hace_return_temprano_con_none():
    """Regresión directa contra la forma del bug: llamar a
    `_resolver_profesional_pagos_cmc` (nivel 0.5 aislado) debe devolver SOLO
    un id_profesional (int|None) — nunca una tupla. Si algún día alguien
    vuelve a fusionar nivel 0.5 con la búsqueda de atencion_id y reintroduce
    un `return X, None` temprano en `_resolver_profesional_pago` para el caso
    de match único, el primer test de este archivo
    (test_nivel_0_5_no_corta_busqueda_de_atencion_id) ya lo detecta. Este test
    fija el contrato de tipos de la función separada."""
    c = _mkdb()
    c.execute("INSERT INTO pagos_cmc (fecha, paciente_nombre, id_profesional, copago, bonificacion) "
              "VALUES ('2026-06-10', 'Juan Perez', 73, 15000, 0)")
    resultado = bi_sync._resolver_profesional_pagos_cmc(c, "2026-06-10", "Juan Perez", 15000)
    check("nivel 0.5 aislado retorna int, no tupla",
          isinstance(resultado, int), f"got {type(resultado)}={resultado!r}")


TESTS = [
    test_nivel_0_5_no_corta_busqueda_de_atencion_id,
    test_atribucion_profesional_no_se_rompe_multiples_profesionales_mismo_dia,
    test_nivel_0_5_sin_atencion_matcheable_no_inventa_nada,
    test_override_nivel_0_sigue_siendo_autoridad_absoluta,
    test_sin_match_en_pagos_cmc_cascada_infiere_ambos,
    test_nivel_0_5_no_hace_return_temprano_con_none,
]


def main():
    print(f"=== test_bi_sync_resolver.py ({len(TESTS)} casos) ===")
    for t in TESTS:
        print(f"\n{t.__name__}")
        t()
    print(f"\n{'='*60}")
    print(f"PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
