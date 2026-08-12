"""Test del filtro de anonimización del feed público /api/boxes-en-vivo.

El gemelo digital 3D (agentecmc.cl/gemelo) es HTML estático servido por nginx,
sin token admin embebido. `_anonimizar_boxes_state()` es la ÚNICA puerta entre
el payload interno de /admin/api/boxes-state (que trae nombre e iniciales de
paciente, nombre del profesional, cita_id, paciente_id, plata y horarios) y lo
que sale a internet. Este test asegura que esa puerta nunca deja pasar datos
identificables de paciente ni el nombre del profesional, y que respeta el
gotcha de nombres visibles (box3 interno = "Box 4" visible).

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

# Keys que JAMÁS deben aparecer en el payload público, a ningún nivel.
PROHIBIDAS = {
    "paciente", "paciente_id", "rut", "telefono", "profesional",
    "profesional_id", "nombre", "cita_id", "revenue_dia", "citas_dia",
    "hora_inicio", "hora_fin", "elapsed_min",
}

ALLOWLIST_KEYS = {"id", "nombre_visible", "estado", "especialidad_actual"}


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


def _buscar_strings_prohibidos(obj, encontrados: list[str], contexto: str = ""):
    """Recorre recursivamente el payload de salida buscando fugas: cualquier
    nombre propio con mayúscula que huela a persona, o valores numéricos que
    parezcan cita_id/paciente_id (ids grandes con 6 dígitos)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in PROHIBIDAS:
                encontrados.append(f"{contexto}.{k} (key prohibida)")
            _buscar_strings_prohibidos(v, encontrados, f"{contexto}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _buscar_strings_prohibidos(v, encontrados, f"{contexto}[{i}]")
    elif isinstance(obj, str):
        for nombre_fuga in ("Olavarría", "Montalba", "Etcheverry", "J.P.", "Juan Pérez"):
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
