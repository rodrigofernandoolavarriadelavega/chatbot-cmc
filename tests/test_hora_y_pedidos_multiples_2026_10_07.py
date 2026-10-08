"""Hallazgos de la simulación de agentes (7-oct-2026).

A) parse_hora no debe leer fechas ni "opción N" como hora, y las ventanas
   ("después de las 4 PM", "after 4 PM") no son una hora exacta.
B) Varios pedidos en un mensaje (especialidades o pacientes): se avisa, el resto
   queda en cola y se ofrece al confirmar; nunca se agenda en lote.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import flows  # noqa: E402
import franja  # noqa: E402
from time_parser import parse_hora  # noqa: E402


# ── A) parse_hora ────────────────────────────────────────────────────────────
def test_fecha_no_es_hora():
    assert parse_hora("viernes 9 de octubre a las 16:30") == (16, 30)
    assert parse_hora("viernes 9 de octubre de 2026 a las 16:30") == (16, 30)
    assert parse_hora("el 09/10 a las 15:00") == (15, 0)
    assert parse_hora("viernes 9 a las 10") == (10, 0)
    assert parse_hora("9 de octubre") is None
    assert parse_hora("viernes 9") is None
    assert parse_hora("09/10") is None


def test_opcion_n_no_es_hora():
    for t in ("opción 2", "Option 2", "opcion 3", "option 1"):
        assert parse_hora(t) is None, t


def test_ventana_no_es_hora_exacta():
    for t in ("jueves después de las 4 PM", "after 4 PM", "antes de las 12", "desde las 15"):
        assert parse_hora(t) is None, t


def test_casos_que_ya_funcionaban_siguen_igual():
    casos = {
        "10:30": (10, 30), "10-30": (10, 30), "10.30": (10, 30), "1030": (10, 30),
        "a las 5": (17, 0), "5 de la tarde": (17, 0), "diez y media": (10, 30),
        "las 16:45": (16, 45), "10 am": (10, 0), "mediodía": (12, 0),
        "viernes 16:30": (16, 30), "hoy a las 11": (11, 0),
    }
    for t, esperado in casos.items():
        assert parse_hora(t) == esperado, t
    assert parse_hora("el 15") is None
    assert parse_hora("mi hijo tiene 10 años") is None


def test_franja_entiende_after_y_before():
    assert franja.parse("Thursday after 4 PM") == (16, 23)
    assert franja.parse("jueves después de las 4 PM") == (16, 23)
    assert franja.parse("before 12") == (8, 12)


def test_hora_pedida_explicita_ignora_rut_y_fechas():
    d: dict = {}
    flows._stash_hora_pedida(
        "viernes 9 de octubre de 2026 a las 16:30 para Juan Pérez, RUT 12.345.678-5", d)
    assert d["hora_pedida"] == "16:30"
    d = {}
    flows._stash_hora_pedida("hora de kine, RUT 12.345.678-5", d)
    assert "hora_pedida" not in d
    d = {"hora_pedida": "10:00"}
    flows._stash_hora_pedida("después de las 16:00", d)
    assert "hora_pedida" not in d


# ── A) opción N como índice ──────────────────────────────────────────────────
SLOTS = [{"hora_inicio": f"{h}:00", "fecha": "2026-10-08"} for h in ("09", "10", "11", "16")]


def test_opcion_n_resuelve_indice():
    assert flows._parse_slot_selection("Option 2", SLOTS) == 1
    assert flows._parse_slot_selection("opción 3", SLOTS) == 2
    assert flows._parse_slot_selection("la primera opción", SLOTS) == 0
    assert flows._parse_slot_selection("the first available slot", SLOTS) == 0
    assert flows._parse_slot_selection("the second one", SLOTS) == 1
    assert flows._parse_slot_selection("Select option 1 please", SLOTS) == 0
    assert flows._parse_slot_selection("16:00", SLOTS) == 3


def test_ref_opcion_n():
    assert flows._ref_opcion_n("Option 2") == 2
    assert flows._ref_opcion_n("la segunda") == 2
    assert flows._ref_opcion_n("opción 1") == 1
    assert flows._ref_opcion_n("a las 16:30") is None


# ── B) detección de pedidos múltiples ────────────────────────────────────────
def test_multiples_especialidades_en_orden_de_aparicion():
    r = flows._detectar_pedidos_multiples("quiero hora de kine, ginecología y psicología")
    assert [p["esp"] for p in r] == ["kinesiología", "ginecología", "psicología"]


def test_multiples_pacientes():
    r = flows._detectar_pedidos_multiples("necesito hora para Juan y para María")
    assert [p["paciente"] for p in r] == ["Juan", "María"]
    r = flows._detectar_pedidos_multiples(
        "una para Juan Pérez, RUT 12.345.678-5, y otra para María Multi Citas, RUT 22.222.222-2")
    assert [p["paciente"] for p in r] == ["Juan Pérez", "María Multi Citas"]


def test_no_detecta_pedido_unico_ni_alternativas_ni_precios():
    assert flows._detectar_pedidos_multiples("quiero hora de kine") is None
    assert flows._detectar_pedidos_multiples("quiero hora con kine o médico general") is None
    assert flows._detectar_pedidos_multiples("hora de medicina general y el valor de ortodoncia") is None
    assert flows._detectar_pedidos_multiples("hora para mi hijo Pedro") is None
    assert flows._detectar_pedidos_multiples("hora para Kinesiología y para Psicología") is not None  # son especialidades
    assert flows._detectar_pedidos_multiples("") is None


def test_aviso_no_tiene_voseo_ni_emojis():
    txt = flows._aviso_pedidos_multiples([{"esp": "kinesiología"}, {"esp": "ginecología"}, {"esp": "psicología"}])
    assert "una hora a la vez" in txt and "Kinesiología" in txt
    assert "Ginecología" in txt and "Psicología" in txt
    assert not any(ord(c) > 0x2000 for c in txt)
    for voseo in ("querés", "podés", "sabés", "vos "):
        assert voseo not in txt


def test_aplicar_limpia_y_saca_de_la_cola():
    d: dict = {}
    esp, aviso, ped = flows._aplicar_pedidos_multiples("p", "kine y ginecología", d, "kinesiología")
    assert esp == "kinesiología" and ped and "una hora a la vez" in aviso
    assert d["cola_pedidos"] == [{"esp": "ginecología"}]
    # sin pedidos nuevos, pero con cola vigente: se agenda "ginecología" -> sale de la cola
    d2 = {"cola_pedidos": [{"esp": "ginecología"}, {"esp": "psicología"}], "cola_ts": time.time()}
    _, aviso2, ped2 = flows._aplicar_pedidos_multiples("p", "ginecología", d2, "ginecología")
    assert aviso2 == "" and ped2 is None
    assert d2["cola_pedidos"] == [{"esp": "psicología"}]
    # cola vencida o mensaje sin especialidad: se limpia
    d3 = {"cola_pedidos": [{"esp": "ginecología"}], "cola_ts": time.time() - 4000}
    flows._aplicar_pedidos_multiples("p", "hola", d3, None)
    assert "cola_pedidos" not in d3


# ── B) estado WAIT_SIGUIENTE_PEDIDO ──────────────────────────────────────────
def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_siguiente_pedido_si_abre_la_siguiente_especialidad(monkeypatch):
    llamadas = []

    async def fake_iniciar(phone, data, esp, saludo_prefix=None):
        llamadas.append((esp, dict(data)))
        return "OFERTA"

    monkeypatch.setattr(flows, "_iniciar_agendar", fake_iniciar)
    monkeypatch.setattr(flows, "reset_session", lambda *a, **k: None)
    data = {"cola_pedidos": [{"esp": "ginecología"}, {"esp": "psicología"}], "cola_ts": time.time()}
    r = _run(flows.handle_message("56900000001", "sig_si", {"state": "WAIT_SIGUIENTE_PEDIDO", "data": data}))
    assert r == "OFERTA"
    assert llamadas[0][0] == "ginecología"
    assert llamadas[0][1]["cola_pedidos"] == [{"esp": "psicología"}]


def test_siguiente_pedido_no_limpia_la_cola(monkeypatch):
    resets = []
    monkeypatch.setattr(flows, "reset_session", lambda phone, *a, **k: resets.append(phone))

    async def no_debe_llamarse(*a, **k):
        raise AssertionError("no debe agendar si dijo que no")

    monkeypatch.setattr(flows, "_iniciar_agendar", no_debe_llamarse)
    data = {"cola_pedidos": [{"esp": "ginecología"}], "cola_ts": time.time()}
    r = _run(flows.handle_message("56900000002", "sig_no", {"state": "WAIT_SIGUIENTE_PEDIDO", "data": data}))
    assert resets == ["56900000002"]
    assert "agendar" not in str(r).lower() or "necesitas otra hora" in str(r).lower()


def test_siguiente_pedido_vencido_se_descarta(monkeypatch):
    resets = []
    monkeypatch.setattr(flows, "reset_session", lambda phone, *a, **k: resets.append(phone))
    data = {"cola_pedidos": [{"esp": "ginecología"}], "cola_ts": time.time() - 99999}
    _run(flows.handle_message("56900000003", "sí", {"state": "WAIT_SIGUIENTE_PEDIDO", "data": data}))
    assert resets


# ── Ronda 2 ──────────────────────────────────────────────────────────────────
import itertools  # noqa: E402


def test_orden_de_la_cola_es_el_del_mensaje():
    pares = [("kine", "kinesiología"), ("gineco", "ginecología"),
             ("psico", "psicología"), ("cardiólogo", "cardiología")]
    for perm in itertools.islice(itertools.permutations(pares), 0, 24, 5):
        texto = "quiero hora de " + ", ".join(p[0] for p in perm[:-1]) + " y " + perm[-1][0]
        r = flows._detectar_pedidos_multiples(texto)
        assert [p["esp"] for p in r] == [p[1] for p in perm], texto
    r = flows._detectar_pedidos_multiples("necesito psicólogo, kinesiólogo y ginecólogo")
    assert [p["esp"] for p in r] == ["psicología", "kinesiología", "ginecología"]
    # aunque el diccionario liste kine antes que psicología
    r = flows._detectar_pedidos_multiples("psico y kine")
    assert [p["esp"] for p in r] == ["psicología", "kinesiología"]


def test_tres_pacientes_en_cola_y_aviso():
    r = flows._detectar_pedidos_multiples("hora para Juan, para María y para Pedro")
    assert [p["paciente"] for p in r] == ["Juan", "María", "Pedro"]
    aviso = flows._aviso_pedidos_multiples(r)
    assert "Juan" in aviso and "*María* y *Pedro*" in aviso


def _sesion_falsa(monkeypatch):
    guardado = {}
    monkeypatch.setattr(flows, "save_session", lambda ph, st, d: guardado.update(state=st, data=dict(d)))
    monkeypatch.setattr(flows, "reset_session", lambda *a, **k: guardado.clear())

    async def fake_slots(esp, fecha, **k):
        s = [{"hora_inicio": h, "fecha": fecha, "profesional": "Dr. X", "id_profesional": 1}
             for h in ("10:00", "10:15", "10:30")]
        return s, s

    monkeypatch.setattr(flows, "buscar_slots_dia", fake_slots)
    return guardado


def test_cola_de_pacientes_sobrevive_al_flujo_otra_persona(monkeypatch):
    g = _sesion_falsa(monkeypatch)
    lb = {"especialidad": "medicina general", "id_profesional": 1, "fecha": "2026-10-08",
          "fecha_display": "jue 8 oct", "hora_inicio": "10:15", "modalidad": "fonasa"}
    data = {"cola_pedidos": [{"paciente": "María"}, {"paciente": "Pedro"}],
            "cola_ts": time.time(), "last_booked": lb}
    # "Sí" -> abre el flujo de otra persona (María) y la cola de Pedro viaja en la sesión
    _run(flows.handle_message("56900000010", "sig_si", {"state": "WAIT_SIGUIENTE_PEDIDO", "data": data}))
    assert g["state"] == "WAIT_SLOT_OTRO"
    assert g["data"]["cola_pedidos"] == [{"paciente": "Pedro"}]
    # "Ver otro horario" reinicia la búsqueda y tampoco pierde la cola
    visto = {}

    async def fake_iniciar(phone, d, esp, saludo_prefix=None):
        visto.update(d)
        return "OFERTA"

    monkeypatch.setattr(flows, "_iniciar_agendar", fake_iniciar)
    _run(flows.handle_message("56900000010", "slot_otro_dia", {"state": "WAIT_SLOT_OTRO", "data": g["data"]}))
    assert visto["cola_pedidos"] == [{"paciente": "Pedro"}]
    # y al terminar María, el siguiente en ofrecerse es Pedro
    cola = flows._cola_vigente(visto)
    assert flows._etiqueta_pedido(cola[0]) == "Pedro"


def test_tres_pacientes_hasta_vaciar(monkeypatch):
    g = _sesion_falsa(monkeypatch)
    d = {"cola_pedidos": [{"paciente": "María"}, {"paciente": "Pedro"}], "cola_ts": time.time(),
         "last_booked": {"especialidad": "medicina general", "id_profesional": 1,
                         "fecha": "2026-10-08", "hora_inicio": "10:00"}}
    ofrecidos = []
    for _ in range(2):
        cola = flows._cola_vigente(d)
        ofrecidos.append(flows._etiqueta_pedido(cola[0]))
        _run(flows.handle_message("56900000011", "sig_si", {"state": "WAIT_SIGUIENTE_PEDIDO", "data": d}))
        # el flujo "otra persona" termina y el hook de confirmación guarda lo que queda
        d = {"cola_pedidos": g["data"].get("cola_pedidos", []), "cola_ts": time.time(),
             "last_booked": d["last_booked"]}
    assert ofrecidos == ["María", "Pedro"]
    assert flows._cola_vigente(d) == []


def test_option_1_y_2_en_modalidad():
    assert flows._leer_modalidad_atencion("Select option 1 please") == "PRESENCIAL"
    assert flows._leer_modalidad_atencion("option 2") == "TELEMEDICINA"
    assert flows._leer_modalidad_atencion("opción 2") == "TELEMEDICINA"
    assert flows._leer_modalidad_atencion("la primera") == "PRESENCIAL"
    assert flows._leer_modalidad_atencion("por video") == "TELEMEDICINA"
    assert flows._leer_modalidad_atencion("quiero la hora del martes") is None
    assert flows._es_respuesta_obvia_al_prompt("Option 1", "option 1", "WAIT_MODALIDAD", {})
    assert not flows._es_respuesta_obvia_al_prompt("option 1", "option 1", "WAIT_RUT_AGENDAR", {})


def test_lista_de_espera_ofrece_el_siguiente(monkeypatch):
    g = _sesion_falsa(monkeypatch)
    d = {"cola_pedidos": [{"esp": "ginecología"}, {"esp": "psicología"}], "cola_ts": time.time()}
    txt = "✅ Listo, quedaste inscrito en la lista de espera.\n\n_Escribe *menu* si necesitas algo más._"
    r = flows._con_siguiente_pedido("56900000012", d, txt)
    assert g["state"] == "WAIT_SIGUIENTE_PEDIDO"
    cuerpo = r["interactive"]["body"]["text"]
    assert "lista de espera" in cuerpo and "Ginecología" in cuerpo and "Psicología" in cuerpo
    assert "¿Seguimos con la hora de *Ginecología*?" in cuerpo
    # sin cola: el texto no cambia
    assert flows._con_siguiente_pedido("p", {}, txt) == txt
    # cola vencida: tampoco
    assert flows._con_siguiente_pedido("p", {"cola_pedidos": [{"esp": "x"}], "cola_ts": 1}, txt) == txt
