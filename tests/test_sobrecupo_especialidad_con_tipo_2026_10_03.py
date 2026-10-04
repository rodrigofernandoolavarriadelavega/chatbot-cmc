"""Sobrecupo debe aplicar aunque la especialidad traiga el tipo de examen pegado.

Caso real 2-oct 21:01 (…8327): "Necesito una abdominal y otra pélvica" → el flujo
guardó especialidad "ecografía abdominal y pélvica"; generar_slots la comparaba
exacto contra la allowlist → [] → sin capas 2/3 del martes 6 → oferta del 13 →
"otro día" → lista de espera.
"""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ["SOBRECUPO_ENABLED"] = "true"
os.environ.pop("SOBRECUPO_ESPECIALIDADES", None)

import medilink as M
import sobrecupo as sc


def test_especialidad_aplica_por_profesional():
    for e in ["ecografía", "Ecografía", "ecografia", "ecografía abdominal y pélvica",
              "ecografía de pared abdominal", "ecografía mamaria"]:
        assert sc.especialidad_aplica(e), e
    for e in ["ginecología", "medicina general", "cardiología", "", "xyz"]:
        assert not sc.especialidad_aplica(e), e


def test_generar_slots_con_tipo_pegado(monkeypatch, tmp_path):
    async def fake_ocupadas(client, id_prof, fecha):
        return ["10:00", "10:15", "10:30"] if id_prof == 68 else []
    monkeypatch.setattr(M, "_get_horas_ocupadas", fake_ocupadas)
    monkeypatch.setattr(M, "_get_shared_client", lambda: None)
    monkeypatch.setattr(sc, "count_dia", lambda *a, **k: 0)
    monkeypatch.setattr(sc, "_ocupados", lambda *a, **k: set())
    base = asyncio.run(sc.generar_slots("ecografía"))
    con_tipo = asyncio.run(sc.generar_slots("ecografía abdominal y pélvica"))
    assert base and con_tipo
    assert [(s["fecha"], s["hora_inicio"]) for s in con_tipo] == \
           [(s["fecha"], s["hora_inicio"]) for s in base]
    assert asyncio.run(sc.generar_slots("ginecología")) == []


def test_flag_off_sigue_inerte(monkeypatch):
    monkeypatch.setenv("SOBRECUPO_ENABLED", "false")
    assert asyncio.run(sc.generar_slots("ecografía abdominal y pélvica")) == []
