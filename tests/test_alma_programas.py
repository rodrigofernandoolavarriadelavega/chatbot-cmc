"""Test del motor genérico de Programas Clínicos (adherencia + control). Sin BI."""
import sys, os
from datetime import date, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import programas as P

TODAY = date(2026, 6, 2)
P._today = lambda: TODAY
P._planes = lambda prog: {}


def _mk(pids):
    rows = []
    for pid, nombre, tel, offs in pids:
        for o in offs:
            rows.append({"paciente_id": pid, "paciente": nombre, "telefono": tel,
                         "lugar": "Arauco", "fecha": TODAY - timedelta(days=o),
                         "profesional": "Prof", "monto": 12000})
    return rows, "ok"


def test_adherencia():
    P._bi_rows = lambda eids, meses: _mk([
        (1, "Ana", "912345678", [40, 20, 5]),    # en_curso
        (2, "Bea", "922222222", [60, 40, 30]),   # riesgo
        (3, "Cris", "933333333", [80, 70, 60]),  # abandono
    ])
    d = P._compute("nutricion", 6)
    by = {p["paciente"]: p for p in d["pacientes"]}
    assert by["Ana"]["estado"] == "en_curso"
    assert by["Bea"]["estado"] == "riesgo"
    assert by["Cris"]["estado"] == "abandono"
    assert d["kpis"]["tipo"] == "adherencia" and d["kpis"]["urgentes"] == 1
    print("test_adherencia OK")


def test_control():
    P._bi_rows = lambda eids, meses: _mk([
        (10, "Don", "944444444", [100]),   # al_dia
        (11, "Eme", "955555555", [200]),   # pronto
        (12, "Efe", "966666666", [300]),   # vencido
    ])
    d = P._compute("cardiologia", 6)
    by = {p["paciente"]: p for p in d["pacientes"]}
    assert by["Don"]["estado"] == "al_dia"
    assert by["Eme"]["estado"] == "pronto"
    assert by["Efe"]["estado"] == "vencido"
    assert d["kpis"]["tipo"] == "control" and d["kpis"]["urgentes"] == 1
    print("test_control OK")


def test_wa_link():
    link = P._wa_link("966666666", "hola")
    assert link.startswith("https://wa.me/56966666666?text="), link
    assert P._wa_link("", "x") is None
    print("test_wa_link OK")


def test_configs_validas():
    for k, c in P.PROGRAMAS.items():
        assert {"especialidad_ids", "tipo", "wa", "objetivo"} <= set(c), k
        if c["tipo"] == "adherencia":
            assert {"en_curso_max", "riesgo_max", "abandono_max", "gap_nuevo"} <= set(c), k
        else:
            assert {"control_ok", "control_due"} <= set(c), k
    print(f"test_configs_validas OK ({len(P.PROGRAMAS)} programas)")


def test_tamizaje():
    def fake_bi(sql, params):
        return ([
            {"paciente_id": 1, "paciente": "Marta Soto", "edad": 52, "telefono": "912345678",
             "lugar": "Arauco", "ultima": TODAY - timedelta(days=1200)},
            {"paciente_id": 2, "paciente": "Rosa Díaz", "edad": 40, "telefono": "998887777",
             "lugar": "Curanilahue", "ultima": None},
        ], "ok")
    P.bi_query = fake_bi
    d = P._compute_tamizaje("pap", 6)
    by = {p["paciente"]: p for p in d["pacientes"]}
    assert by["Marta Soto"]["estado"] == "lapsed"
    assert by["Rosa Díaz"]["estado"] == "nunca"
    assert d["kpis"]["tipo"] == "tamizaje" and d["kpis"]["n_lapsed"] == 1 and d["kpis"]["n_nunca"] == 1
    assert d["pacientes"][0]["estado"] == "lapsed"   # lapsed primero (alta confianza)
    assert d["kpis"]["metric_val"] == 2              # ambas con teléfono
    assert P._dispatch("pap", 6)["kpis"]["tipo"] == "tamizaje"
    print(f"test_tamizaje OK ({len(P.TAMIZAJE)} cohortes)")


def test_configs_tamizaje():
    for k, c in P.TAMIZAJE.items():
        assert {"genero", "edad_min", "edad_max", "esp_recall", "dias", "wa", "objetivo"} <= set(c), k
    print(f"test_configs_tamizaje OK ({len(P.TAMIZAJE)} cohortes)")


def test_bordes():
    P._planes = lambda prog: {}
    # 1) Paciente de una sola sesión reciente → en_curso (no rompe el split de episodios)
    P._bi_rows = lambda e, m: _mk([(1, "Uno", "911111111", [2])])
    by = {p["paciente"]: p for p in P._compute("nutricion", 6)["pacientes"]}
    assert by["Uno"]["estado"] == "en_curso" and by["Uno"]["sesiones"] == 1

    # 2) Umbral exacto: nutricion en_curso_max=21 → 21 días sigue en_curso (≤)
    P._bi_rows = lambda e, m: _mk([(2, "Borde", "922222222", [40, 21])])
    by = {p["paciente"]: p for p in P._compute("nutricion", 6)["pacientes"]}
    assert by["Borde"]["estado"] == "en_curso", by["Borde"]["estado"]

    # 3) Estado manual no_contactar manda sobre todo
    P._planes = lambda prog: {3: {"paciente_id": 3, "sesiones_plan": 0, "estado_manual": "no_contactar", "notas": ""}}
    P._bi_rows = lambda e, m: _mk([(3, "Nc", "933333333", [60, 40, 30])])  # sería riesgo
    by = {p["paciente"]: p for p in P._compute("nutricion", 6)["pacientes"]}
    assert by["Nc"]["estado"] == "no_contactar", by["Nc"]["estado"]
    P._planes = lambda prog: {}

    # 4) Sin datos → KPIs en cero, sin crash
    P._bi_rows = lambda e, m: ([], "ok")
    d = P._compute("cardiologia", 6)
    assert d["kpis"]["n_pacientes"] == 0 and d["kpis"]["accionables"] == 0

    # 5) Tamizaje vacío → no crash
    P.bi_query = lambda sql, params: ([], "ok")
    dt = P._compute_tamizaje("empam", 6)
    assert dt["kpis"]["n_pacientes"] == 0
    print("test_bordes OK")


if __name__ == "__main__":
    test_adherencia(); test_control(); test_wa_link(); test_configs_validas()
    test_tamizaje(); test_configs_tamizaje(); test_bordes()
    print("ALL OK")
