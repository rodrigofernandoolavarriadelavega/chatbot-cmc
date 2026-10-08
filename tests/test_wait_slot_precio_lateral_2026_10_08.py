"""Pregunta lateral de PRECIO / cobertura en WAIT_SLOT (2026-10-08).

Medido en prod (90 días, sessions.db solo lectura): ~160 hilos escribieron
"cuánto sale", "valor", "es por fonasa?", "atiende con fonasa" con horas en
pantalla. Dos fallas: (1) el pre-router LLM los mandaba a la ficha de dirección
y teléfono (31 casos); (2) la respuesta de precio no resolvía "odontología"
(20 casos: "te paso con recepción" teniendo el valor en PRECIOS_SLOT).

Esperado: precio/cobertura de lo ofrecido + las MISMAS horas, estado
WAIT_SLOT, sin reintentos ni reset, sin llamar al LLM, sin robar la selección.

Offline (fakes de harness_50). Correr:
    MEDILINK_TOKEN=x PYTHONPATH=app:. python -m pytest tests/test_wait_slot_precio_lateral_2026_10_08.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
import harness_50 as H  # noqa: E402
import flows  # noqa: E402
import medilink  # noqa: E402
from session import get_session, reset_session, save_privacy_consent, save_session  # noqa: E402


@pytest.fixture(autouse=True)
def _fakes_del_harness(monkeypatch):
    """Otros módulos de tests reemplazan estas funciones en flows/medilink y el
    harness las instala solo al importarse: se reinstalan por test para que el
    resultado no dependa del orden de la suite."""
    for mod in (medilink, flows):
        for nombre, fake in (
            ("buscar_primer_dia", H.fake_buscar_primer_dia),
            ("buscar_slots_dia", H.fake_buscar_slots_dia),
            ("buscar_slots_dia_por_ids", H.fake_buscar_slots_dia_por_ids),
            ("buscar_paciente", H.fake_buscar_paciente),
            ("listar_citas_paciente", H.fake_listar_citas_paciente),
            ("consultar_proxima_fecha", H.fake_consultar_proxima_fecha),
            ("verificar_slot_disponible", H.fake_verificar_slot_disponible),
        ):
            monkeypatch.setattr(mod, nombre, fake, raising=False)
    monkeypatch.setattr(flows, "detect_intent", H.fake_detect_intent)
    monkeypatch.setattr(flows, "respuesta_faq", H.fake_respuesta_faq)
    monkeypatch.setattr(flows, "classify_with_context", H.fake_classify_with_context)
    monkeypatch.setattr(flows, "is_medilink_down", lambda: False)
    monkeypatch.setattr(flows, "send_whatsapp", H.fake_send_whatsapp)

# Frases reales del corpus (WAIT_SLOT, 90 días), sin datos personales.
PRECIO_REAL = [
    "cuánto sale", "Valor", "Precio?", "Cual el el valor", "Qero saber el valor",
    "Cuánto vale la consulta", "y valores", "Valor total?", "Cuanto es el valor",
    "Cuál es el valor total de la consulta?", "Primero quiero saber el valor",
    "Con médico que valor tiene", "cuanto seria el total de todo",
]
COBERTURA_REAL = [
    "es por fonasa?", "Fonasa", "Fonasa?", "Por Fonasa", "Y por Fonasa ?",
    "Atiende ppr fonasa", "Atiende  con fonasa", "Atienden con fonasa V",
    "Pensé que atendia por fonasa", "El doctor Rodrigo Olavarría atiende con Fonasa",
    "Dónde se compra el bono", "Tiene para el bono directo en la clínica por Fonasa",
    "Pensé que trabajaba con Fonasa",
]
DENTAL_REAL = [
    "Cuál es el valor de una endodoncia?", "Valor del destartraje",
    "Y cuanto es el valor  en sacarme las 2 muelas?",
    "Necesito saber cuánto sale una limpieza dental",
]
# NO son preguntas laterales: elegir hora, otro día, reservar, otra persona...
SELECCION_REAL = [
    "sí", "la primera", "a las 10", "otro día", "10:00", "1", "Cuanto antes",
    "Dr. Andrés Abarca 17:15 por fonasa",
    "Viernes a las 12:30 por favor con fonasa",
    "Por Fonasa si está bien reservame la",
    "Otro profesional por fonasa", "Quiero para otra persona",
    "Necesito un doctor que atienda en la mañana con Fonasa mañana",
]


def _data_con_slots(esp="Medicina General", prof_id=73, nombre="Dr. Andrés Abarca"):
    slots = H._fake_slots(esp, prof_id, nombre)
    return {"especialidad": esp.lower(), "slots": slots[:5], "todos_slots": slots}


def test_detector_acepta_corpus_real():
    d = _data_con_slots()
    for t in PRECIO_REAL + COBERTURA_REAL:
        assert flows._es_pregunta_lateral_precio(t, d), t
    dd = _data_con_slots("Odontología General", 55, "Dra. Javiera Burgos")
    for t in DENTAL_REAL:
        assert flows._es_pregunta_lateral_precio(t, dd), t


def test_detector_no_roba_seleccion():
    d = _data_con_slots()
    for t in SELECCION_REAL:
        assert not flows._es_pregunta_lateral_precio(t, d), t


def test_detector_requiere_horas_en_pantalla_y_no_otra_persona():
    assert not flows._es_pregunta_lateral_precio("cuánto sale", {})
    d = _data_con_slots()
    d["booking_for_other"] = True
    assert not flows._es_pregunta_lateral_precio("Fonasa", d)


def test_detector_otra_especialidad_no_se_responde_con_la_actual():
    d = _data_con_slots()
    assert not flows._es_pregunta_lateral_precio("Cuánto sale una eco abdominal con fonasa", d)
    assert not flows._es_pregunta_lateral_precio("Y cuando sale la ortodoncia", d)


def test_precio_line_resuelve_nombre_generico():
    assert "15.000" in flows._precio_line("odontología")
    assert "14.420" in flows._precio_line("psicología")
    assert "60.000" in flows._precio_line("nutriología")
    assert flows._precio_line("medicina general") == flows._precio_line("Medicina General")
    # sin entrada: sigue vacío
    assert flows._precio_line("especialidad inventada") == ""


# ── E2E por handle_message ──────────────────────────────────────────────────
_n = [0]


_ESP_FAKE = {
    "medicina general": ("Medicina General", 73, "Dr. Andrés Abarca"),
    "odontología": ("Odontología General", 55, "Dra. Javiera Burgos"),
}


def _arrancar(esp_texto: str) -> str:
    """Deja la sesión directo en WAIT_SLOT con 5 horas ofrecidas (sin pasar por
    detect_intent: así no depende del resto de la suite)."""
    _n[0] += 1
    phone = f"569000{_n[0]:05d}"
    reset_session(phone)
    save_privacy_consent(phone, "accepted", method="test")
    save_session(phone, "WAIT_SLOT", _data_con_slots(*_ESP_FAKE[esp_texto]))
    return phone


def _correr(coro):
    # asyncio.run() deja sin loop actual y rompe a los tests que usan get_event_loop()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _decir(phone: str, txt: str):
    return _correr(flows.handle_message(phone, txt, get_session(phone)))


def _sin_llm(monkeypatch):
    llamadas = []

    async def _boom(*a, **k):
        llamadas.append(a)
        return {"action": "escape", "intent": "preguntar_info", "args": {}}
    monkeypatch.setattr(flows, "classify_with_context", _boom)
    return llamadas


def _estado_comparable(d: dict) -> dict:
    return {k: v for k, v in d.items() if not str(k).startswith("_ts")}


def test_e2e_precio_y_cobertura_mg_vuelven_a_mostrar_las_horas(monkeypatch):
    llamadas = _sin_llm(monkeypatch)
    for t in ["cuánto sale", "Valor", "es por fonasa?", "Atiende ppr fonasa", "Fonasa?"]:
        phone = _arrancar("medicina general")
        antes = get_session(phone)["data"]
        resp = _decir(phone, t)
        txt = H._normalize(resp)
        ses = get_session(phone)
        assert ses["state"] == "WAIT_SLOT", (t, ses["state"])
        assert ses["data"]["slots"] == antes["slots"], t
        assert {k for k in ses["data"] if "reint" in k or "intento" in k} == \
               {k for k in antes if "reint" in k or "intento" in k}, t
        assert "7.880" in txt and "25.000" in txt, (t, txt)
        # las mismas horas otra vez (lista interactiva con ids 1..5)
        assert isinstance(resp, dict), t
        ids = [r["id"] for s in resp["interactive"]["action"]["sections"] for r in s["rows"]]
        assert ids[:5] == ["1", "2", "3", "4", "5"], (t, ids)
        assert "no te entendí" not in txt.lower() and "recepción" not in txt.lower(), (t, txt)
        assert "Monsalve" not in txt, (t, txt)
    assert llamadas == []  # el LLM ni se consultó


def test_e2e_odontologia_precio_y_fonasa_sin_derivar(monkeypatch):
    _sin_llm(monkeypatch)
    phone = _arrancar("odontología")
    txt = H._normalize(_decir(phone, "cuánto sale"))
    assert "15.000" in txt and "te paso con recepción" not in txt
    assert get_session(phone)["state"] == "WAIT_SLOT"
    phone = _arrancar("odontología")
    txt = H._normalize(_decir(phone, "atiende con fonasa?"))
    assert "solo Particular" in txt and "15.000" in txt
    assert get_session(phone)["state"] == "WAIT_SLOT"


def test_e2e_dental_procedimiento_usa_faq_y_no_lee_la_hora(monkeypatch):
    _sin_llm(monkeypatch)
    pedidos = []

    async def _faq(msg, *a, **k):
        pedidos.append(msg)
        return "La limpieza dental cuesta desde $30.000."
    monkeypatch.setattr(flows, "respuesta_faq", _faq)
    phone = _arrancar("odontología")
    resp = _decir(phone, "Y cuanto es el valor  en sacarme las 2 muelas?")
    txt = H._normalize(resp)
    assert pedidos and "$30.000" in txt, txt
    assert "No tengo exactamente" not in txt        # "las 2" ya no se lee como 14:00
    assert get_session(phone)["state"] == "WAIT_SLOT"


def test_e2e_seleccion_sigue_igual(monkeypatch):
    _sin_llm(monkeypatch)
    for t, hora in [("sí", "09:00"), ("la primera", "09:00"), ("1", "09:00"),
                    ("a las 10", "10:00"), ("10:00", "10:00")]:
        phone = _arrancar("medicina general")
        txt = H._normalize(_decir(phone, t))
        assert "¿Tu atención será Fonasa o Particular?" in txt, (t, txt)
        assert hora in txt, (t, txt)
    phone = _arrancar("medicina general")
    _decir(phone, "otro día")
    assert get_session(phone)["state"] == "WAIT_SLOT"


def test_e2e_cuanto_antes_no_es_precio(monkeypatch):
    _sin_llm(monkeypatch)
    phone = _arrancar("medicina general")
    txt = H._normalize(_decir(phone, "Cuanto antes"))
    assert "7.880" not in txt or "Pago" not in txt


def test_monto_con_punto_de_miles_no_es_hora():
    d = _data_con_slots()
    for t in ("El valor de 60.000 es el total de la consulta?",
              "Pero cuál es el monto total de la atención. Abono 60 + ?"):
        assert flows._es_pregunta_lateral_precio(t, d), t
