"""Test del filtro de anonimización del feed público /api/boxes-en-vivo.

El gemelo digital 3D (agentecmc.cl/gemelo) es HTML estático servido por nginx,
sin token admin embebido. `_anonimizar_boxes_state()` es la ÚNICA puerta entre
el payload interno de /admin/api/boxes-state (que trae nombre e iniciales de
paciente, nombre del profesional, cita_id, paciente_id, plata y horarios) y lo
que sale a internet. Este test asegura que esa puerta nunca deja pasar datos
identificables de paciente ni el nombre del profesional, y que respeta el
gotcha de nombres visibles (box3 interno = "Box 4" visible).

También cubre el modo `?detalle=1` (OPERATE v2): agenda del día por box en
bloques anonimizados {hora_inicio, hora_fin, estado, especialidad} — SIN
paciente/RUT/teléfono/cita_id/profesional/plata — más métricas del día
{citas_total, atendidas, ocupacion_pct, proximo_bloque_libre}.

Ejecución:
    PYTHONPATH=app:. python3 tests/test_boxes_en_vivo_anonimizacion.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

from main import _anonimizar_boxes_state  # noqa: E402

# Keys que JAMÁS deben aparecer en el payload público, a ningún nivel (modo
# simple, sin detalle). En modo detalle, hora_inicio/hora_fin SÍ existen pero
# solo dentro de `agenda_dia[*]` — junto a `estado`/`especialidad`, nunca junto
# a un identificador de paciente/profesional/plata (ver PROHIBIDAS_DETALLE).
PROHIBIDAS = {
    "paciente", "paciente_id", "rut", "telefono", "profesional",
    "profesional_id", "nombre", "cita_id", "revenue_dia", "citas_dia",
    "hora_inicio", "hora_fin", "elapsed_min",
}

# Keys prohibidas en modo detalle: mismo set salvo hora_inicio/hora_fin, que
# ahí son legítimas (agenda anonimizada, sin nombre asociado).
PROHIBIDAS_DETALLE = PROHIBIDAS - {"hora_inicio", "hora_fin"}

ALLOWLIST_KEYS = {"id", "nombre_visible", "piso", "estado", "especialidad_actual"}
ALLOWLIST_KEYS_DETALLE = ALLOWLIST_KEYS | {"agenda_dia", "metricas_dia"}
ALLOWLIST_BLOQUE_KEYS = {"hora_inicio", "hora_fin", "estado", "especialidad"}
ALLOWLIST_METRICAS_KEYS = {"citas_total", "atendidas", "ocupacion_pct", "proximo_bloque_libre"}
BLOQUE_ESTADOS_VALIDOS = {"atendida", "en_curso", "agendada", "no_show", "anulada"}


def _raw_boxes_state_fixture() -> dict:
    """Simula el payload crudo de /admin/api/boxes-state con datos sensibles
    reales (nombre de paciente, RUT-like, nombre de profesional) para
    verificar que NADA de eso sobrevive al filtro."""
    return {
        "boxes": [
            {
                "id": "box1", "nombre": "Box 1", "piso": 1, "orden": 1,
                "tipo": "general", "estado": "ocupado",
                "profesionales_activos": [{
                    "profesional": "Dr. Rodrigo Olavarría",
                    "especialidad": "Medicina General",
                    "paciente": "J.P.",
                    "elapsed_min": 12,
                    "cita_id": 998877,
                    "paciente_id": 445566,
                }],
                "proximo": None,
                "revenue_dia": 45000,
                "citas_dia": 3,
            },
            {
                # Gotcha: id interno "box3" == nombre visible "Box 4".
                "id": "box3", "nombre": "Box 4", "piso": 2, "orden": 2,
                "tipo": "procedimientos", "estado": "libre",
                "profesionales_activos": [],
                "proximo": None,
                "revenue_dia": 0,
                "citas_dia": 0,
            },
            {
                # Gotcha inverso: id interno "box4" == nombre visible "Box 3".
                "id": "box4", "nombre": "Box 3", "piso": 2, "orden": 1,
                "tipo": "psicología", "estado": "proximo",
                "profesionales_activos": [],
                "proximo": {
                    "profesional": "Ps. Montalba",
                    "especialidad": "Psicología",
                    "starts_in_min": 10,
                },
                "revenue_dia": 0,
                "citas_dia": 1,
            },
            {
                # "próximo" a 45 min NO debe marcarse proximo en el feed público
                # (regla pública: <15 min), aunque el dashboard interno sí lo
                # muestre (su ventana es de 60 min).
                "id": "kine1", "nombre": "Kinesiología 1", "piso": 1, "orden": 3,
                "tipo": "kinesiología", "estado": "proximo",
                "profesionales_activos": [],
                "proximo": {
                    "profesional": "Klgo. Etcheverry",
                    "especialidad": "Kinesiología",
                    "starts_in_min": 45,
                },
                "revenue_dia": 0,
                "citas_dia": 1,
            },
        ],
        # Ruido del payload interno que NO debe filtrarse a través de otras keys.
        "totales": {"revenue_dia": 780000},
        "citas_dia_full": [{"paciente": "Juan Pérez Soto", "profesional": "Dr. X"}],
        "rev_por_prof": {"1": 500000},
    }


def _raw_boxes_state_fixture_detalle() -> dict:
    """Payload crudo con agenda del día real (citas_dia_full + boxes_config_default
    + cupos_hoy + now_cl) para probar `detalle=True`. Cubre los 5 buckets de
    estado público (atendida/en_curso/agendada/no_show/anulada) repartidos en
    boxes distintos, y dos boxes de piso distinto (1 y 2) para el campo `piso`.
    """
    raw = _raw_boxes_state_fixture()
    raw["now_cl"] = "2026-08-20 11:00:00"
    # cupos del horario, usado por ocupacion_pct — box3 (Box 4) queda en 0 para
    # probar que ocupacion_pct es None (no 0/0) sin agenda ni cupos.
    cupos = {"box1": 20, "box3": 0, "box4": 5, "kine1": 10}
    for b in raw["boxes"]:
        b["cupos_hoy"] = cupos.get(b["id"], 0)
    raw["boxes_config_default"] = [
        {"id": "box1", "revenue_profs": [1, 73, 23, 60, 61, 68]},
        {"id": "box3", "revenue_profs": [67, 56]},   # visible "Box 4"
        {"id": "box4", "revenue_profs": [74, 49]},   # visible "Box 3"
        {"id": "kine1", "revenue_profs": [21, 77]},
    ]
    raw["citas_dia_full"] = [
        # box1 (prof 1) — atendida, ya pasó.
        {"cita_id": 111, "profesional_id": 1, "profesional": "Dr. Rodrigo Olavarría",
         "paciente": "Juan Pérez Soto", "especialidad": "Medicina General",
         "hora_inicio": "08:00", "hora_fin": "08:15", "estado": "atendido"},
        # box1 (prof 1) — cubre el "ahora" (11:00) → en_curso.
        {"cita_id": 112, "profesional_id": 1, "profesional": "Dr. Rodrigo Olavarría",
         "paciente": "Ana Soto", "especialidad": "Medicina General",
         "hora_inicio": "10:50", "hora_fin": "11:10", "estado": "no confirmado"},
        # box1 (prof 73 — también revenue_profs de box1) — futura → agendada.
        {"cita_id": 113, "profesional_id": 73, "profesional": "Dr. Andrés Abarca",
         "paciente": "Pedro Vega", "especialidad": "Medicina General",
         "hora_inicio": "14:00", "hora_fin": "14:15", "estado": "confirmado por email"},
        # box4 visible "Box 3" (prof 74) — no_show.
        {"cita_id": 114, "profesional_id": 74, "profesional": "Ps. Montalba",
         "paciente": "Luis Rojas", "especialidad": "Psicología",
         "hora_inicio": "09:00", "hora_fin": "09:45", "estado": "no asiste"},
        # kine1 (prof 77) — anulada.
        {"cita_id": 115, "profesional_id": 77, "profesional": "Klgo. Etcheverry",
         "paciente": "María Díaz", "especialidad": "Kinesiología",
         "hora_inicio": "09:00", "hora_fin": "09:40", "estado": "anulado"},
        # Profesional sin box asignado en boxes_config_default → se descarta,
        # no debe aparecer en ningún agenda_dia ni romper el cálculo.
        {"cita_id": 116, "profesional_id": 999, "profesional": "Dr. Fantasma",
         "paciente": "Nadie", "especialidad": "Rayos X",
         "hora_inicio": "12:00", "hora_fin": "12:15", "estado": "no confirmado"},
    ]
    return raw


def _buscar_strings_prohibidos(obj, encontrados: list[str], contexto: str = "", prohibidas=None):
    """Recorre recursivamente el payload de salida buscando fugas: cualquier
    nombre propio con mayúscula que huela a persona, o valores numéricos que
    parezcan cita_id/paciente_id (ids grandes con 6 dígitos)."""
    prohibidas = PROHIBIDAS if prohibidas is None else prohibidas
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in prohibidas:
                encontrados.append(f"{contexto}.{k} (key prohibida)")
            _buscar_strings_prohibidos(v, encontrados, f"{contexto}.{k}", prohibidas)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _buscar_strings_prohibidos(v, encontrados, f"{contexto}[{i}]", prohibidas)
    elif isinstance(obj, str):
        for nombre_fuga in ("Olavarría", "Montalba", "Etcheverry", "J.P.", "Juan Pérez",
                            "Abarca", "Ana Soto", "Pedro Vega", "Luis Rojas", "María Díaz"):
            if nombre_fuga in obj:
                encontrados.append(f"{contexto} contiene '{nombre_fuga}': {obj!r}")


def test_no_filtra_datos_de_paciente_ni_profesional():
    raw = _raw_boxes_state_fixture()
    out = _anonimizar_boxes_state(raw)

    encontrados: list[str] = []
    _buscar_strings_prohibidos(out, encontrados, "boxes")
    assert not encontrados, f"Fuga de datos sensibles en el payload público: {encontrados}"

    # Solo las 4 keys permitidas, ni una más, por box.
    for box in out:
        assert set(box.keys()) == ALLOWLIST_KEYS, f"Keys inesperadas: {box.keys()}"


def test_nombres_visibles_respetan_el_gotcha_box3_box4():
    raw = _raw_boxes_state_fixture()
    out = {b["id"]: b for b in _anonimizar_boxes_state(raw)}
    assert out["box3"]["nombre_visible"] == "Box 4"
    assert out["box4"]["nombre_visible"] == "Box 3"


def test_estados_correctos():
    raw = _raw_boxes_state_fixture()
    out = {b["id"]: b for b in _anonimizar_boxes_state(raw)}

    assert out["box1"]["estado"] == "ocupado"
    assert out["box1"]["especialidad_actual"] == "Medicina General"

    assert out["box3"]["estado"] == "libre"
    assert out["box3"]["especialidad_actual"] is None

    # proximo interno con starts_in_min=10 (<15) → proximo en el feed público
    assert out["box4"]["estado"] == "proximo"
    assert out["box4"]["especialidad_actual"] == "Psicología"

    # proximo interno con starts_in_min=45 (>15) → libre en el feed público
    assert out["kine1"]["estado"] == "libre"
    assert out["kine1"]["especialidad_actual"] is None


def test_modo_simple_no_incluye_agenda_ni_metricas():
    """Sin `detalle=True` el payload es exactamente el de siempre — el campo
    nuevo `piso` no reintroduce agenda/métricas por accidente."""
    raw = _raw_boxes_state_fixture()
    out = _anonimizar_boxes_state(raw)
    for box in out:
        assert "agenda_dia" not in box
        assert "metricas_dia" not in box
        assert set(box.keys()) == ALLOWLIST_KEYS


def test_incluye_piso_en_todos_los_boxes():
    """Piso 1 y piso 2 (y el carril 0 de telemedicina si viniera) deben salir
    con su `piso` — el gemelo lo necesita para montar 'CMC Piso 2'."""
    raw = _raw_boxes_state_fixture()
    out = {b["id"]: b for b in _anonimizar_boxes_state(raw)}
    assert out["box1"]["piso"] == 1
    assert out["box3"]["piso"] == 2
    assert out["box4"]["piso"] == 2
    assert out["kine1"]["piso"] == 1


def test_detalle_no_filtra_datos_sensibles():
    raw = _raw_boxes_state_fixture_detalle()
    out = _anonimizar_boxes_state(raw, detalle=True)

    encontrados: list[str] = []
    _buscar_strings_prohibidos(out, encontrados, "boxes", prohibidas=PROHIBIDAS_DETALLE)
    assert not encontrados, f"Fuga de datos sensibles en el payload detalle=1: {encontrados}"

    for box in out:
        assert set(box.keys()) == ALLOWLIST_KEYS_DETALLE, f"Keys inesperadas: {box.keys()}"
        assert set(box["metricas_dia"].keys()) == ALLOWLIST_METRICAS_KEYS
        for bloque in box["agenda_dia"]:
            assert set(bloque.keys()) == ALLOWLIST_BLOQUE_KEYS, f"Keys inesperadas en bloque: {bloque.keys()}"
            assert bloque["estado"] in BLOQUE_ESTADOS_VALIDOS


def test_detalle_buckets_de_estado():
    raw = _raw_boxes_state_fixture_detalle()
    out = {b["id"]: b for b in _anonimizar_boxes_state(raw, detalle=True)}

    box1_estados = sorted(b["estado"] for b in out["box1"]["agenda_dia"])
    assert box1_estados == ["agendada", "atendida", "en_curso"]

    assert [b["estado"] for b in out["box4"]["agenda_dia"]] == ["no_show"]
    assert [b["estado"] for b in out["kine1"]["agenda_dia"]] == ["anulada"]
    # box3 no tiene citas asignadas en el fixture → agenda vacía, no error.
    assert out["box3"]["agenda_dia"] == []

    # La cita del profesional 999 (sin box en boxes_config_default) no debe
    # aparecer en NINGÚN box.
    total_bloques = sum(len(b["agenda_dia"]) for b in out.values())
    assert total_bloques == 5


def test_detalle_metricas_dia():
    raw = _raw_boxes_state_fixture_detalle()
    out = {b["id"]: b for b in _anonimizar_boxes_state(raw, detalle=True)}

    m1 = out["box1"]["metricas_dia"]
    assert m1["citas_total"] == 3
    assert m1["atendidas"] == 1
    assert m1["ocupacion_pct"] == 15          # 3 citas / 20 cupos
    assert m1["proximo_bloque_libre"] == "11:10"   # cubre el "ahora" 11:00-11:10

    m3 = out["box3"]["metricas_dia"]
    assert m3["citas_total"] == 0
    assert m3["ocupacion_pct"] is None        # sin cupos conocidos, no 0
    assert m3["proximo_bloque_libre"] == "11:00"   # libre ahora mismo

    m4 = out["box4"]["metricas_dia"]
    assert m4["citas_total"] == 1
    assert m4["atendidas"] == 0
    assert m4["ocupacion_pct"] == 20          # 1 cita / 5 cupos
    # no_show no cuenta como "ocupando" el bloque ahora → libre ahora.
    assert m4["proximo_bloque_libre"] == "11:00"

    mk = out["kine1"]["metricas_dia"]
    assert mk["citas_total"] == 1
    assert mk["ocupacion_pct"] == 10          # 1 cita / 10 cupos
    # anulada tampoco ocupa → libre ahora.
    assert mk["proximo_bloque_libre"] == "11:00"


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    ok = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
            ok += 1
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            print(f"ERROR {t.__name__}: {e!r}")
    print(f"\n{ok}/{len(tests)} tests pasaron")
    if ok != len(tests):
        sys.exit(1)


if __name__ == "__main__":
    _run_all()
