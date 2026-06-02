"""
Tests del algoritmo de los módulos Alma Kine + Ortodoncia.

No tocan la BI: inyectan filas sintéticas en _bi_rows y fijan _today, así
verifican la lógica pura de detección de episodios, clasificación de estado,
override por plan y plan de pago. Correr: python tests/test_alma_kine_ortodoncia.py
"""
import sys, os
from datetime import date, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import kine_routes as K
import ortodoncia_routes as O

TODAY = date(2026, 6, 2)
K._today = lambda: TODAY
O._today = lambda: TODAY
K._planes = lambda: {}
O._planes = lambda: {}


def _kine_rows():
    rows = []
    def add(pid, nombre, tel, fecha, monto=10000, prof="Leonardo Etcheverry"):
        rows.append({"paciente_id": pid, "paciente": nombre, "telefono": tel,
                     "lugar": "Arauco", "fecha": fecha, "profesional_id": 21,
                     "profesional": prof, "monto": monto})
    for off in [21, 14, 7, 3]:  add(1, "Ana A", "912345678", TODAY - timedelta(days=off))   # en_curso
    for off in [35, 28, 21, 14]: add(2, "Bea B", "922222222", TODAY - timedelta(days=off))   # riesgo
    for off in [60, 52, 44, 30]: add(3, "Carla C", "933333333", TODAY - timedelta(days=off)) # abandono
    add(4, "Dora D", "944444444", TODAY - timedelta(days=200))
    add(4, "Dora D", "944444444", TODAY - timedelta(days=193))   # episodio viejo
    add(4, "Dora D", "944444444", TODAY - timedelta(days=95))
    add(4, "Dora D", "944444444", TODAY - timedelta(days=90))    # episodio actual cerrado
    return rows, "ok"


def test_kine():
    K._planes = lambda: {}
    K._bi_rows = lambda meses: _kine_rows()
    res = K._compute(6)
    by = {p["paciente"]: p for p in res["pacientes"]}
    assert by["Ana A"]["estado"] == "en_curso"
    assert by["Bea B"]["estado"] == "riesgo"
    assert by["Carla C"]["estado"] == "abandono"
    assert by["Dora D"]["estado"] == "cerrado"
    assert by["Dora D"]["n_episodios"] == 2
    assert by["Dora D"]["sesiones_episodio"] == 2
    assert res["kpis"]["activos"] == 2
    assert res["kpis"]["riesgo"] == 1
    assert [p["paciente"] for p in res["pacientes"]][0] == "Bea B"  # riesgo primero

    # plan completado override
    K._planes = lambda: {2: {"paciente_id": 2, "sesiones_plan": 4, "estado_manual": "", "notas": ""}}
    by2 = {p["paciente"]: p for p in K._compute(6)["pacientes"]}
    assert by2["Bea B"]["estado"] == "completado"
    assert by2["Bea B"]["avance_pct"] == 100
    print("test_kine OK")


def _orto_rows():
    rows = []
    def add(pid, nombre, tel, fecha, monto):
        rows.append({"paciente_id": pid, "paciente": nombre, "telefono": tel,
                     "lugar": "Curanilahue", "fecha": fecha, "monto": monto})
    add(10, "Eva E", "955555555", TODAY - timedelta(days=200), 120000)
    for off in [120, 80, 20]: add(10, "Eva E", "955555555", TODAY - timedelta(days=off), 30000)   # al_dia
    add(11, "Fran F", "966666666", TODAY - timedelta(days=300), 120000)
    for off in [200, 120, 70]: add(11, "Fran F", "966666666", TODAY - timedelta(days=off), 30000) # vencido
    add(12, "Gabi G", "977777777", TODAY - timedelta(days=120), 120000)
    add(12, "Gabi G", "977777777", TODAY - timedelta(days=40), 30000)                              # pronto
    return rows, "ok"


def test_ortodoncia():
    O._planes = lambda: {}
    O._bi_rows = lambda meses: _orto_rows()
    ro = O._compute(6)
    by = {p["paciente"]: p for p in ro["pacientes"]}
    assert by["Eva E"]["estado"] == "al_dia"
    assert by["Fran F"]["estado"] == "vencido"
    assert by["Gabi G"]["estado"] == "pronto"
    assert by["Eva E"]["n_instalacion"] == 1 and by["Eva E"]["n_controles"] == 3
    assert ro["kpis"]["vencidos"] == 1 and ro["kpis"]["pronto"] == 1 and ro["kpis"]["activos"] == 3

    # plan de pago -> saldo + cartera
    O._planes = lambda: {11: {"paciente_id": 11, "valor_total": 900000, "abonado": 300000,
                              "cuota_mensual": 50000, "estado_manual": "", "notas": ""}}
    ro2 = O._compute(6)
    by2 = {p["paciente"]: p for p in ro2["pacientes"]}
    assert by2["Fran F"]["saldo"] == 600000
    assert ro2["kpis"]["valor_cartera"] == 600000
    print("test_ortodoncia OK")


if __name__ == "__main__":
    test_kine()
    test_ortodoncia()
    print("ALL OK")
