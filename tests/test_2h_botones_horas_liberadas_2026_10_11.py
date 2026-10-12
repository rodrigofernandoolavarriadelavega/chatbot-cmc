"""Recordatorios 2h con botones + 48h bot por template + kine con rutina +
horas liberadas (2026-10-11). Sin red, sin Medilink.

Correr: PYTHONPATH=app:. venv/bin/python -m pytest tests/test_2h_botones_horas_liberadas_2026_10_11.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_2hb_")) / "s.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ["ALMA_OPERATIVA_VERIFICAR_LIBRE"] = "false"
os.environ.setdefault("SQLCIPHER_KEY", "")

import session  # noqa: E402
session.DB_PATH = TMP_DB
import reminders  # noqa: E402
from alma_brain import operativa  # noqa: E402

CL = ZoneInfo("America/Santiago")


def run(coro):
    return asyncio.run(coro)


# ── Plantilla 2h con botones ────────────────────────────────────────────────

def _aprobada(valor):
    async def f(name):
        return valor if name == "recordatorio_2h_botones" else False
    return f


def test_plantilla_2h_con_botones_si_aprobada(monkeypatch):
    import winback
    monkeypatch.setattr(winback, "is_template_approved", _aprobada(True))
    name, btns = run(reminders._plantilla_2h("123"))
    assert name == "recordatorio_2h_botones"
    assert btns == ["r2h_confirm:123", "r2h_cancelar:123"]


def test_plantilla_2h_sin_botones_si_no_aprobada(monkeypatch):
    import winback
    monkeypatch.setattr(winback, "is_template_approved", _aprobada(False))
    assert run(reminders._plantilla_2h("123")) == ("recordatorio_cita_2h", None)


def test_plantilla_2h_flag_apagado(monkeypatch):
    import winback
    monkeypatch.setattr(winback, "is_template_approved", _aprobada(True))
    monkeypatch.setenv("RECORDATORIO_2H_BOTONES", "0")
    assert run(reminders._plantilla_2h("123")) == ("recordatorio_cita_2h", None)


def test_plantilla_2h_error_verificando_cae_a_actual(monkeypatch):
    import winback

    async def boom(n):
        raise RuntimeError("meta caída")
    monkeypatch.setattr(winback, "is_template_approved", boom)
    assert run(reminders._plantilla_2h("1")) == ("recordatorio_cita_2h", None)


def _cita_2h(phone="56900002001"):
    f = datetime.now(CL) + timedelta(hours=2)
    return {"id": 1, "id_cita": "9001", "phone": phone, "fecha": f.strftime("%Y-%m-%d"),
            "hora": f.strftime("%H:%M:%S"), "especialidad": "Kinesiología",
            "profesional": "Luis Armijo", "paciente_nombre": "Ana Ruiz", "es_tercero": 0,
            "confirmation_status": None}


def _setup_2h(monkeypatch, citas, aprobada):
    import winback
    monkeypatch.setattr(winback, "is_template_approved", _aprobada(aprobada))
    monkeypatch.setattr(reminders, "get_citas_bot_para_2h_reminder", lambda *a, **k: citas)

    async def estado(*a, **k):
        return {"anulada": False, "id_paciente": None}
    monkeypatch.setattr(reminders, "_estado_pre_envio", estado)
    monkeypatch.setattr(reminders, "get_last_inbound_ts", lambda p: None)  # ventana cerrada
    monkeypatch.setattr(reminders, "USE_TEMPLATES", True)
    monkeypatch.setattr(reminders, "log_message", lambda *a, **k: None)
    marcados = []
    monkeypatch.setattr(reminders, "mark_reminder_2h_sent", lambda i: marcados.append(i))
    eventos = []
    monkeypatch.setattr(reminders, "log_event", lambda p, e, m=None: eventos.append((e, m)))

    async def nosleep(s):
        return None
    monkeypatch.setattr(reminders.asyncio, "sleep", nosleep)
    return marcados, eventos


def test_2h_bot_usa_plantilla_con_botones_y_no_manda_aviso_liberacion(monkeypatch):
    marcados, eventos = _setup_2h(monkeypatch, [_cita_2h()], True)
    tpl, textos = [], []

    async def send_tpl(phone, name, body_params=None, button_payloads=None, **k):
        tpl.append((name, body_params, button_payloads))
        return "wamid.1"

    async def send_text(phone, msg):
        textos.append(msg)
    run(reminders.enviar_recordatorios_2h(send_text, send_template_fn=send_tpl))
    assert tpl and tpl[0][0] == "recordatorio_2h_botones"
    assert tpl[0][2] == ["r2h_confirm:9001", "r2h_cancelar:9001"]
    assert len(tpl[0][1]) == 4   # mismas 4 variables
    assert marcados == [1]
    # el aviso "tu hora queda disponible en 30 minutos" ya no existe
    assert not any("queda disponible" in t for t in textos)
    assert not any(e == "aviso_liberacion_slot" for e, _ in eventos)


def test_2h_bot_sin_aprobacion_sigue_con_la_actual(monkeypatch):
    _setup_2h(monkeypatch, [_cita_2h()], False)
    tpl = []

    async def send_tpl(phone, name, body_params=None, button_payloads=None, **k):
        tpl.append((name, button_payloads))
        return "wamid.1"
    run(reminders.enviar_recordatorios_2h(lambda *a: None, send_template_fn=send_tpl))
    assert tpl == [("recordatorio_cita_2h", None)]


def test_no_queda_texto_que_prometa_liberar_la_hora():
    src = (ROOT / "app" / "reminders.py").read_text()
    assert "queda disponible en *30 minutos*" not in src
    assert "aviso_liberacion_slot" not in src


def test_main_enruta_botones_r2h():
    src = (ROOT / "app" / "main.py").read_text()
    assert "r2h_confirm:" in src and "recordatorio_2h_boton" in src


def test_recepcion_2h_usa_botones(monkeypatch):
    import winback
    monkeypatch.setattr(winback, "is_template_approved", _aprobada(True))
    f = datetime.now(CL) + timedelta(hours=2)
    cita = {"id": 5, "id_cita_medilink": "9100", "phone": "56900002002", "paciente_nombre": "Ana Ruiz",
            "especialidad": "Kinesiología", "profesional": "Luis Armijo", "hora": f.strftime("%H:%M"),
            "fecha": f.strftime("%Y-%m-%d"), "id_profesional": 77}
    monkeypatch.setattr(reminders, "_guard_recepcion", lambda n: True)
    monkeypatch.setattr(reminders, "get_citas_recepcion_pendientes_2h", lambda *a: [dict(cita)])

    async def estado(*a, **k):
        return {"anulada": False, "confirmada": False}
    monkeypatch.setattr(reminders, "_estado_pre_envio", estado)
    monkeypatch.setattr(reminders, "USE_TEMPLATES", True)
    monkeypatch.setattr(reminders, "get_last_inbound_ts", lambda p: None)
    monkeypatch.setattr(reminders, "log_message", lambda *a, **k: None)
    monkeypatch.setattr(reminders, "log_event", lambda *a, **k: None)
    marc = []
    monkeypatch.setattr(reminders, "mark_recepcion_reminder_2h_sent", lambda i: marc.append(i))

    async def nosleep(s):
        return None
    monkeypatch.setattr(reminders.asyncio, "sleep", nosleep)
    tpl = []

    async def send_tpl(phone, name, body_params=None, button_payloads=None, **k):
        tpl.append((name, button_payloads))
        return "wamid.2"
    n = run(reminders.enviar_recordatorios_recepcion_2h(lambda *a: None, send_template_fn=send_tpl))
    assert n == 1 and tpl == [("recordatorio_2h_botones", ["r2h_confirm:9100", "r2h_cancelar:9100"])]


# ── 48h bot: ventana cerrada → template ─────────────────────────────────────

def _setup_48(monkeypatch, ventana_abierta):
    manana = (datetime.now(CL) + timedelta(days=2)).strftime("%Y-%m-%d")
    cita = {"id": 7, "id_cita": "9200", "phone": "56900003001", "fecha": manana, "hora": "17:00:00",
            "especialidad": "Medicina General", "profesional": "Dr. Andrés Abarca",
            "paciente_nombre": "Ana Ruiz", "id_paciente_medilink": None}
    monkeypatch.setattr(reminders, "get_citas_bot_para_48h_reminder", lambda f: [dict(cita)])

    async def estado(*a, **k):
        return {"anulada": False, "confirmada": False, "id_paciente": None}
    monkeypatch.setattr(reminders, "_estado_pre_envio", estado)
    monkeypatch.setattr(reminders, "is_window_open", lambda p: ventana_abierta)
    monkeypatch.setattr(reminders, "USE_TEMPLATES", True)
    monkeypatch.setattr(reminders, "log_message", lambda *a, **k: None)
    ev = []
    monkeypatch.setattr(reminders, "log_event", lambda p, e, m=None: ev.append(e))
    marc = []
    monkeypatch.setattr(reminders, "mark_reminder_48h_sent", lambda i: marc.append(i))

    async def nosleep(s):
        return None
    monkeypatch.setattr(reminders.asyncio, "sleep", nosleep)
    return marc, ev


def test_48h_bot_ventana_cerrada_usa_template(monkeypatch):
    marc, ev = _setup_48(monkeypatch, False)
    tpl, inter = [], []

    async def send_tpl(phone, name, body_params=None, button_payloads=None, **k):
        tpl.append((name, body_params, button_payloads))
        return "wamid.3"

    async def send_inter(phone, m):
        inter.append(m)
    run(reminders.enviar_recordatorios_48h(lambda *a: None, send_interactive_fn=send_inter,
                                           send_template_fn=send_tpl))
    assert tpl and tpl[0][0] == "recordatorio_cita" and len(tpl[0][1]) == 6
    assert tpl[0][2][0] == "cita_confirm:9200" and not inter and marc == [7]


def test_48h_bot_template_rechazado_no_marca(monkeypatch):
    marc, ev = _setup_48(monkeypatch, False)

    async def send_tpl(*a, **k):
        return None
    run(reminders.enviar_recordatorios_48h(lambda *a: None, send_interactive_fn=lambda *a: None,
                                           send_template_fn=send_tpl))
    assert marc == [] and "template_fallido" in ev


def test_48h_bot_ventana_abierta_sigue_interactivo(monkeypatch):
    marc, ev = _setup_48(monkeypatch, True)
    inter = []

    async def send_inter(phone, m):
        inter.append(m)

    async def send_tpl(*a, **k):
        raise AssertionError("no debe usar template con ventana abierta")
    run(reminders.enviar_recordatorios_48h(lambda *a: None, send_interactive_fn=send_inter,
                                           send_template_fn=send_tpl))
    assert len(inter) == 1 and marc == [7]


# ── 48h recepción: kine con rutina ──────────────────────────────────────────

def _seed_ausentismo(id_pac, id_prof, fecha, id_estado=2, anul=0, id_cita=None):
    from ausentismo import ensure_ausentismo_table
    ensure_ausentismo_table()
    with session.db() as c:
        c.execute("INSERT OR REPLACE INTO ausentismo_citas (id_cita,id_profesional,id_paciente,fecha,id_estado,anulacion) "
                  "VALUES (?,?,?,?,?,?)", (id_cita or abs(hash((id_pac, fecha, id_prof))) % 10**8,
                                           id_prof, id_pac, fecha, id_estado, anul))
        c.commit()


def test_kine_con_rutina_omite_y_primera_sesion_envia():
    hoy = datetime.now(CL).date()
    _seed_ausentismo(501, 77, (hoy - timedelta(days=3)).isoformat(), id_estado=2)
    assert reminders._kine_con_rutina({"id_profesional": 77, "id_paciente": 501}) is True
    # primera sesión de una serie nueva: sin atención de kine en 14 días
    assert reminders._kine_con_rutina({"id_profesional": 77, "id_paciente": 502}) is False
    # sesión hace 20 días no cuenta
    _seed_ausentismo(503, 21, (hoy - timedelta(days=20)).isoformat(), id_estado=2)
    assert reminders._kine_con_rutina({"id_profesional": 21, "id_paciente": 503}) is False
    # anulada / no asiste / cambio de fecha no cuentan
    _seed_ausentismo(504, 77, (hoy - timedelta(days=2)).isoformat(), id_estado=1, anul=1)
    _seed_ausentismo(504, 77, (hoy - timedelta(days=4)).isoformat(), id_estado=8)
    _seed_ausentismo(504, 77, (hoy - timedelta(days=5)).isoformat(), id_estado=14)
    assert reminders._kine_con_rutina({"id_profesional": 77, "id_paciente": 504}) is False
    # no kine -> nunca omite
    assert reminders._kine_con_rutina({"id_profesional": 73, "id_paciente": 501}) is False
    # sin id_paciente -> fail-open
    assert reminders._kine_con_rutina({"id_profesional": 77}) is False


def test_recepcion_48h_kine_rutina_emite_evento_y_no_envia(monkeypatch):
    hoy = datetime.now(CL).date()
    _seed_ausentismo(601, 77, (hoy - timedelta(days=1)).isoformat(), id_estado=2)
    fecha = (hoy + timedelta(days=2)).isoformat()
    filas = [
        {"id": 11, "id_cita_medilink": "9301", "id_profesional": 77, "id_paciente": 601, "phone": "56900004001",
         "paciente_nombre": "Rutina", "especialidad": "Kinesiología", "profesional": "Luis Armijo",
         "fecha": fecha, "hora": "10:00"},
        {"id": 12, "id_cita_medilink": "9302", "id_profesional": 77, "id_paciente": 602, "phone": "56900004002",
         "paciente_nombre": "Nueva", "especialidad": "Kinesiología", "profesional": "Luis Armijo",
         "fecha": fecha, "hora": "11:00"},
    ]
    monkeypatch.setattr(reminders, "_guard_recepcion", lambda n: True)
    monkeypatch.setattr(reminders, "get_citas_recepcion_pendientes_48h", lambda f: [dict(x) for x in filas])

    async def estado(*a, **k):
        return {"anulada": False, "confirmada": False}
    monkeypatch.setattr(reminders, "_estado_pre_envio", estado)
    monkeypatch.setattr(reminders, "USE_TEMPLATES", True)
    monkeypatch.setattr(reminders, "get_last_inbound_ts", lambda p: None)
    monkeypatch.setattr(reminders, "log_message", lambda *a, **k: None)
    ev = []
    monkeypatch.setattr(reminders, "log_event", lambda p, e, m=None: ev.append((p, e)))
    marc = []
    monkeypatch.setattr(reminders, "mark_recepcion_reminder_48h_sent", lambda i: marc.append(i))

    async def nosleep(s):
        return None
    monkeypatch.setattr(reminders.asyncio, "sleep", nosleep)
    enviados = []

    async def send_tpl(phone, name, body_params=None, button_payloads=None, **k):
        enviados.append(phone)
        return "wamid.4"
    n = run(reminders.enviar_recordatorios_recepcion_48h(lambda *a: None, send_template_fn=send_tpl))
    assert enviados == ["56900004002"] and n == 1
    assert ("56900004001", "recordatorio_skip_kine_rutina") in ev


# ── Horas liberadas ─────────────────────────────────────────────────────────

@pytest.fixture
def operativa_on(monkeypatch):
    monkeypatch.setenv("ALMA_OPERATIVA_ENABLED", "true")
    monkeypatch.setattr(operativa, "_operativa_enabled", lambda: True)
    monkeypatch.setattr(operativa, "VERIFICAR_LIBRE", False)
    with session.db() as c:
        for t in ("waitlist", "waitlist_offers", "conversation_events", "citas_bot"):
            c.execute(f"DELETE FROM {t}")
        c.commit()
    # ventana abierta para todos salvo que el test diga otra cosa
    monkeypatch.setattr(session, "is_window_open", lambda p: True)
    yield


def _slot(esp="medicina general", horas=4):
    f = datetime.now(CL) + timedelta(hours=horas)
    return {"especialidad": esp, "id_prof": 73, "fecha": f.strftime("%Y-%m-%d"),
            "hora": f.strftime("%H:%M"), "phone_cancelador": "56911110000"}


def _espera(phone, esp="medicina general"):
    session.add_to_waitlist(phone, "11111111-1", "Paciente", esp)


def _pidio(phone, esp="medicina general", dias=1, evento="intent_agendar", extra=None):
    import json
    with session.db() as c:
        c.execute("INSERT INTO conversation_events (phone,event,meta,ts) VALUES (?,?,?,datetime('now',?))",
                  (phone, evento, json.dumps({"especialidad": esp, **(extra or {})}), f"-{dias} days"))
        c.commit()


def _mandar(monkeypatch):
    enviados = []

    async def send(phone, msg):
        enviados.append(phone)
    return enviados, send


def _eventos(nombre):
    with session.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT phone, meta FROM conversation_events WHERE event=?", (nombre,)).fetchall()]


def test_no_ofrece_si_faltan_menos_de_60_min(operativa_on, monkeypatch):
    _espera("56920000001")
    enviados, send = _mandar(monkeypatch)
    res = run(operativa.fill_freed_slot(_slot(horas=0.5), send_fn=send))
    assert res["invitados"] == 0 and not enviados
    assert _eventos("hora_liberada_sin_antelacion")


def test_tandas_2_luego_3_tope_5(operativa_on, monkeypatch):
    for i in range(8):
        _espera(f"5692000010{i}")
    enviados, send = _mandar(monkeypatch)
    slot = _slot()
    r1 = run(operativa.fill_freed_slot(slot, send_fn=send))
    assert r1["invitados"] == 2
    # idempotencia: segundo camino de detección no reenvía
    r1b = run(operativa.fill_freed_slot(slot, send_fn=send))
    assert r1b["invitados"] == 0 and len(enviados) == 2
    # tanda 2: 3 más
    r2 = run(operativa.fill_freed_slot(slot, send_fn=send, tanda=2))
    assert r2["invitados"] == 3 and len(enviados) == 5
    r3 = run(operativa.fill_freed_slot(slot, send_fn=send, tanda=2))
    assert r3["invitados"] == 0 and len(enviados) == 5
    assert len(set(enviados)) == 5
    ofr = _eventos("hora_liberada_ofrecida")
    assert len(ofr) == 5 and '"grupo": "a"' in ofr[0]["meta"] and '"ventana": "abierta"' in ofr[0]["meta"]


def test_continuar_tandas_a_los_10_min(operativa_on, monkeypatch):
    for i in range(6):
        _espera(f"5692000020{i}")
    enviados, send = _mandar(monkeypatch)
    run(operativa.fill_freed_slot(_slot(), send_fn=send))
    assert len(enviados) == 2
    assert run(operativa.continuar_tandas(send_fn=send)) == 0   # aún no pasan 10 min
    with session.db() as c:
        c.execute("UPDATE waitlist_offers SET created_at=datetime('now','-11 minutes')")
        c.commit()
    assert run(operativa.continuar_tandas(send_fn=send)) == 1
    assert len(enviados) == 5
    assert run(operativa.continuar_tandas(send_fn=send)) == 0   # sellada, no reintenta


def test_grupo_b_despues_de_a_y_excluye_con_cita(operativa_on, monkeypatch):
    _espera("56920000301")
    _pidio("56920000302")                       # pidió ayer, sin cita -> grupo b
    _pidio("56920000303")                       # pidió pero luego agendó
    with session.db() as c:
        c.execute("INSERT INTO conversation_events (phone,event,meta) VALUES ('56920000303','cita_creada','{}')")
        c.commit()
    _pidio("56920000304", dias=5)               # hace 5 días: fuera de los 3 días
    _pidio("56920000305", esp="ginecología")    # otra especialidad
    enviados, send = _mandar(monkeypatch)
    run(operativa.fill_freed_slot(_slot(), send_fn=send, top_n=5))
    assert enviados == ["56920000301", "56920000302"]
    grupos = [e["meta"] for e in _eventos("hora_liberada_ofrecida")]
    assert '"grupo": "a"' in grupos[0] and '"grupo": "b"' in grupos[1]


def test_grupo_b_prioriza_mismo_dia(operativa_on, monkeypatch):
    slot = _slot()
    _pidio("56920000401", dias=2)
    _pidio("56920000402", dias=1, evento="funnel_slot_ofrecido", extra={"fecha": slot["fecha"]})
    enviados, send = _mandar(monkeypatch)
    run(operativa.fill_freed_slot(slot, send_fn=send))
    assert enviados[0] == "56920000402"


def test_respeta_bajas_y_un_aviso_por_dia(operativa_on, monkeypatch):
    _espera("56920000501")
    _espera("56920000502")
    _espera("56920000503")
    session.save_tag("56920000501", "recuperacion_opt_out")
    session.log_event("56920000502", "hora_liberada_ofrecida", {"x": 1})   # ya avisado hoy
    enviados, send = _mandar(monkeypatch)
    run(operativa.fill_freed_slot(_slot(), send_fn=send))
    assert enviados == ["56920000503"]


def test_ventana_cerrada_a_template_oferta_cupo(operativa_on, monkeypatch):
    _espera("56920000601")
    monkeypatch.setattr(session, "is_window_open", lambda p: False)
    monkeypatch.setenv("USE_TEMPLATES", "true")
    import messaging
    llamadas = []

    async def tpl(to, name, body_params=None, **k):
        llamadas.append((to, name))
        return "wamid.X"
    monkeypatch.setattr(messaging, "send_whatsapp_template", tpl)
    monkeypatch.setattr(messaging, "render_template_body", lambda n, p=None: "x")
    # send_fn=None => canal real
    res = run(operativa.fill_freed_slot(_slot()))
    assert res["invitados"] == 1 and llamadas == [("56920000601", "oferta_cupo")]
    assert '"ventana": "cerrada"' in _eventos("hora_liberada_ofrecida")[0]["meta"]


def test_grupo_b_ventana_cerrada_exige_consent_marketing(operativa_on, monkeypatch):
    _pidio("56920000701")
    monkeypatch.setattr(session, "is_window_open", lambda p: False)
    monkeypatch.setenv("USE_TEMPLATES", "true")
    import messaging
    llamadas = []

    async def tpl(to, name, body_params=None, **k):
        llamadas.append(name)
        return "wamid.X"
    monkeypatch.setattr(messaging, "send_whatsapp_template", tpl)
    monkeypatch.setattr(messaging, "render_template_body", lambda n, p=None: "x")
    # sin consent -> nada
    monkeypatch.setattr(operativa, "_consent_marketing_ok", lambda p: False)
    assert run(operativa.fill_freed_slot(_slot(horas=26)))["invitados"] == 0
    assert llamadas == []
    # con consent y hora de MAÑANA -> horas_liberadas_v1
    with session.db() as c:
        c.execute("DELETE FROM waitlist_offers"); c.execute("DELETE FROM conversation_events WHERE event LIKE 'hora_liberada%'")
        c.commit()
    monkeypatch.setattr(operativa, "_consent_marketing_ok", lambda p: True)
    manana = (datetime.now(CL) + timedelta(days=1)).replace(hour=15, minute=0)
    slot = dict(_slot(), fecha=manana.strftime("%Y-%m-%d"), hora="15:00")
    assert run(operativa.fill_freed_slot(slot))["invitados"] == 1
    assert llamadas == ["horas_liberadas_v1"]
    # con consent pero hora de HOY y ventana cerrada -> sin template adecuado
    with session.db() as c:
        c.execute("DELETE FROM waitlist_offers"); c.execute("DELETE FROM conversation_events WHERE event LIKE 'hora_liberada%'")
        c.commit()
    llamadas.clear()
    hoy = dict(_slot(), fecha=(datetime.now(CL)).strftime("%Y-%m-%d"), hora="23:59")
    if _slot(horas=2)["fecha"] == datetime.now(CL).strftime("%Y-%m-%d"):
        assert run(operativa.fill_freed_slot(hoy))["invitados"] == 0 and llamadas == []


def test_hora_no_libre_no_ofrece(operativa_on, monkeypatch):
    _espera("56920000801")
    monkeypatch.setattr(operativa, "VERIFICAR_LIBRE", True)

    async def no_libre(*a):
        return False
    monkeypatch.setattr(operativa, "_hora_realmente_libre", no_libre)
    enviados, send = _mandar(monkeypatch)
    res = run(operativa.fill_freed_slot(_slot(), send_fn=send))
    assert res["invitados"] == 0 and not enviados and _eventos("hora_liberada_no_libre")


def test_primero_que_toma_gana_y_el_otro_recibe_ya_la_tomaron(operativa_on, monkeypatch):
    _espera("56920000901")
    _espera("56920000902")
    enviados, send = _mandar(monkeypatch)
    slot = _slot()
    run(operativa.fill_freed_slot(slot, send_fn=send))
    msgs = []

    async def send2(phone, msg):
        msgs.append((phone, msg))
    o1 = session.get_open_offer_for_phone("56920000901")
    o2 = session.get_open_offer_for_phone("56920000902")
    run(operativa.accept_offer(o1, send_fn=send2))
    r2 = run(operativa.accept_offer(o2, send_fn=send2))
    assert r2["reason"] == "slot_taken"
    assert any("ya la tomaron" in m for p, m in msgs if p == "56920000902")
    assert len(_eventos("hora_liberada_tomada")) == 1


def test_oferta_con_hora_pasada_no_se_toma(operativa_on, monkeypatch):
    _espera("56920001001")
    enviados, send = _mandar(monkeypatch)
    run(operativa.fill_freed_slot(_slot(), send_fn=send))
    with session.db() as c:
        c.execute("UPDATE waitlist_offers SET fecha=date('now','-1 day')")
        c.commit()
    o = session.get_open_offer_for_phone("56920001001")
    msgs = []

    async def send2(phone, msg):
        msgs.append(msg)
    r = run(operativa.accept_offer(o, send_fn=send2))
    assert r["reason"] == "hora_pasada" and msgs


def test_ofrecer_hora_liberada_excluye_al_que_anulo(operativa_on, monkeypatch):
    _espera("56920001101")
    _espera("56920001102")
    with session.db() as c:
        c.execute("INSERT INTO citas_bot (phone,id_cita,fecha,hora,especialidad,profesional) "
                  "VALUES ('56920001101','7777','2030-01-01','10:00','medicina general','x')")
        c.commit()
    enviados, send = _mandar(monkeypatch)
    f = datetime.now(CL) + timedelta(hours=4)
    res = run(operativa.ofrecer_hora_liberada(
        {"id": 7777, "id_profesional": 73, "fecha": f.strftime("%Y-%m-%d"),
         "hora_inicio": f.strftime("%H:%M:%S")}, origen="cancelar_cita", send_fn=send))
    assert enviados == ["56920001102"] and res["invitados"] == 1
    assert _eventos("hora_liberada_detectada")
