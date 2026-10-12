"""Medicina General: hora temprana alternativa (umbral 2 h) y re-búsqueda
mid-conversación en WAIT_SLOT (2026-10-11, prueba del martes 13-oct).

Offline: agenda falsa por profesional y fecha. Correr:
    MEDILINK_TOKEN=x PYTHONPATH=app:. python -m pytest tests/test_mg_hora_temprana_2026_10_11.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
import harness_50 as H  # noqa: E402
import flows  # noqa: E402
import medilink  # noqa: E402
from session import db, get_session, reset_session, save_privacy_consent, save_session  # noqa: E402

_TZ = ZoneInfo("America/Santiago")
HOY = datetime.now(_TZ).date()
HOY_S = HOY.strftime("%Y-%m-%d")
MANANA_S = (HOY + timedelta(days=1)).strftime("%Y-%m-%d")
NOMBRES = {73: "Dr. Andrés Abarca", 1: "Dr. Rodrigo Olavarría", 13: "Dr. Alonso Márquez"}


def _slot(prof, fecha, hora):
    mm = int(hora[:2]) * 60 + int(hora[3:]) + 15
    return {"profesional": NOMBRES[prof], "especialidad": "Medicina General", "fecha": fecha,
            "fecha_display": "hoy" if fecha == HOY_S else "mañana", "hora_inicio": hora,
            "hora_fin": f"{mm // 60:02d}:{mm % 60:02d}", "id_profesional": prof,
            "id_recurso": 1, "duracion": 15}


# ── 1. Función pura del umbral ──────────────────────────────────────────────
def test_umbral_abarca_tarde_vs_olavarria_temprano_muestra_ambos():
    alt = medilink.elegir_hora_temprana([_slot(73, HOY_S, "17:45")], [_slot(1, HOY_S, "10:30")])
    assert alt and alt["hora_inicio"] == "10:30" and alt["id_profesional"] == 1
    assert alt["alt_temprana"] is True


def test_umbral_abarca_11_vs_olavarria_1030_solo_abarca():
    assert medilink.elegir_hora_temprana([_slot(73, HOY_S, "11:00")], [_slot(1, HOY_S, "10:30")]) is None


def test_umbral_borde_120_no_dispara_121_si():
    base = [_slot(73, HOY_S, "12:30")]
    assert medilink.elegir_hora_temprana(base, [_slot(1, HOY_S, "10:30")]) is None       # 120
    assert medilink.elegir_hora_temprana(base, [_slot(1, HOY_S, "10:29")]) is not None   # 121


def test_umbral_configurable_y_empate_prefiere_olavarria():
    base = [_slot(73, HOY_S, "12:30")]
    otros = [_slot(13, HOY_S, "10:00"), _slot(1, HOY_S, "10:00")]
    assert medilink.elegir_hora_temprana(base, otros)["id_profesional"] == 1
    assert medilink.elegir_hora_temprana(base, otros, umbral_min=180) is None
    assert medilink.MG_UMBRAL_TEMPRANO_MIN == 120


def test_sin_otros_o_sin_base():
    assert medilink.elegir_hora_temprana([_slot(73, HOY_S, "17:45")], []) is None
    assert medilink.elegir_hora_temprana([], [_slot(1, HOY_S, "10:30")]) is None


# ── 2. _slots_para_fecha (camino prioridad) con agenda falsa ────────────────
def _lunes_futuro():
    d = HOY + timedelta(days=8)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _bloques(desde, hasta):
    out, cur = set(), int(desde[:2]) * 60 + int(desde[3:])
    fin = int(hasta[:2]) * 60 + int(hasta[3:])
    while cur < fin:
        out.add(f"{cur // 60:02d}:{cur % 60:02d}")
        cur += 5
    return out


def _correr(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _slots_con_agenda(monkeypatch, ocupadas: dict, horario: dict):
    """horario: {id: (hi, hf, intervalo)}; ocupadas: {id: set de bloques de 5 min}."""
    async def _esp(client, id_prof):
        return []
    async def _bloq(client, id_prof, fecha):
        return []
    async def _ocu(client, id_prof, fecha):
        return set(ocupadas.get(id_prof, set()))
    monkeypatch.setattr(medilink, "_get_horarios_especiales", _esp)
    monkeypatch.setattr(medilink, "_get_bloqueos", _bloq)
    monkeypatch.setattr(medilink, "_get_horas_ocupadas", _ocu)
    fecha = _lunes_futuro()
    wd = datetime.strptime(fecha, "%Y-%m-%d").weekday()
    horarios = {i: {"dias": [wd], "horario_dia": {wd: (hi, hf, None)}, "intervalo": iv}
                for i, (hi, hf, iv) in horario.items()}
    ids = list(horario)
    return _correr(medilink._slots_para_fecha(None, ids, horarios, fecha, prioridad=True)), fecha


HORARIO = {73: ("08:00", "18:00", 15), 1: ("10:30", "14:00", 15), 13: ("15:00", "20:00", 20)}


def test_camino_prioridad_abarca_1745_y_olavarria_1030_muestra_ambos(monkeypatch):
    (smart, todos), _ = _slots_con_agenda(
        monkeypatch, {73: _bloques("08:00", "17:45")}, HORARIO)
    assert todos[0]["id_profesional"] == 73 and todos[0]["hora_inicio"] == "17:45"
    alt = [s for s in todos if s.get("alt_temprana")]
    assert len(alt) == 1 and alt[0]["hora_inicio"] == "10:30" and alt[0]["id_profesional"] == 1
    assert smart[-1] is alt[0] or smart[-1] == alt[0]
    assert {s["id_profesional"] for s in todos} == {73, 1}      # Márquez 15:00 no es más temprano


def test_camino_prioridad_abarca_1100_no_agrega_otro(monkeypatch):
    (smart, todos), _ = _slots_con_agenda(
        monkeypatch, {73: _bloques("08:00", "11:00")}, HORARIO)
    assert {s["id_profesional"] for s in todos} == {73}
    assert not any(s.get("alt_temprana") for s in todos + smart)


def test_camino_prioridad_abarca_lleno_cae_a_olavarria(monkeypatch):
    (smart, todos), _ = _slots_con_agenda(
        monkeypatch, {73: _bloques("08:00", "18:00")}, HORARIO)
    assert todos and {s["id_profesional"] for s in todos} == {1}
    assert not any(s.get("alt_temprana") for s in todos)


# ── 3. Mid-conversación en WAIT_SLOT ────────────────────────────────────────
@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
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


AGENDA: dict = {}


def _instalar_agenda(monkeypatch, agenda):
    """agenda: {(prof, fecha): [horas]}"""
    def _slots(ids, fecha):
        out = []
        for pid in ids:
            out += [_slot(pid, fecha, h) for h in agenda.get((pid, fecha), [])]
        return out

    async def por_ids(ids, fecha, **kw):
        s = _slots(ids, fecha)
        return s[:5], s

    async def dia(esp, fecha, **kw):
        for pid in (73, 1, 13):
            s = _slots([pid], fecha)
            if s:
                return s[:5], s
        return [], []

    async def primer_dia(esp, dias_adelante=60, excluir=None, solo_ids=None, **kw):
        for d in range(0, 20):
            f = (HOY + timedelta(days=d)).strftime("%Y-%m-%d")
            if f in (excluir or []):
                continue
            s = _slots(solo_ids or [73, 1, 13], f)
            if s:
                return s[:5], s
        return [], []

    for mod in (medilink, flows):
        monkeypatch.setattr(mod, "buscar_slots_dia_por_ids", por_ids, raising=False)
        monkeypatch.setattr(mod, "buscar_slots_dia", dia, raising=False)
        monkeypatch.setattr(mod, "buscar_primer_dia", primer_dia, raising=False)


_n = [0]


def _arrancar(slots_pantalla):
    _n[0] += 1
    phone = f"569010{_n[0]:05d}"
    reset_session(phone)
    save_privacy_consent(phone, "accepted", method="test")
    save_session(phone, "WAIT_SLOT", {
        "especialidad": "medicina general", "slots": slots_pantalla[:5],
        "todos_slots": slots_pantalla, "fechas_vistas": [slots_pantalla[0]["fecha"]],
        "expansion_stage": 0, "prof_sugerido_id": slots_pantalla[0]["id_profesional"],
        "slot_sugerido": slots_pantalla[0]})
    return phone


def _decir(phone, txt):
    return _correr(flows.handle_message(phone, txt, get_session(phone)))


def _texto(resp):
    if isinstance(resp, dict):
        i = resp["interactive"]
        partes = [i["body"]["text"]]
        for sec in i.get("action", {}).get("sections", []):
            partes += [r["title"] for r in sec["rows"]]
        for b in i.get("action", {}).get("buttons", []):
            partes.append(b["reply"]["title"])
        return "\n".join(partes)
    return resp


def _eventos(phone, evento):
    with db() as c:
        rows = c.execute("SELECT meta FROM conversation_events WHERE phone=? AND event=?",
                         (phone, evento)).fetchall()
    return [json.loads(r[0]) for r in rows]


def test_mid_con_marquez_rebusca_y_muestra_sus_horas(monkeypatch):
    _instalar_agenda(monkeypatch, {(73, MANANA_S): ["09:00", "09:15"], (13, MANANA_S): ["16:00", "16:20"]})
    phone = _arrancar([_slot(73, MANANA_S, "09:00"), _slot(73, MANANA_S, "09:15")])
    resp = _decir(phone, "con Márquez")
    txt = _texto(resp)
    assert "16:00" in txt and "Márquez" in txt, txt
    assert get_session(phone)["state"] == "WAIT_SLOT"
    assert {s["id_profesional"] for s in get_session(phone)["data"]["slots"]} == {13}
    ev = _eventos(phone, "mg_rebusca_mid")
    assert ev and ev[0]["motivo"] == "medico" and ev[0]["resultado"] == "lista"


def test_mid_en_la_tarde_busca_en_todos_los_medicos(monkeypatch):
    _instalar_agenda(monkeypatch, {(73, MANANA_S): ["09:00", "10:00"], (1, MANANA_S): ["16:30"]})
    phone = _arrancar([_slot(73, MANANA_S, "09:00"), _slot(73, MANANA_S, "10:00")])
    txt = _texto(_decir(phone, "tienen en la tarde?"))
    assert "16:30" in txt and "Olavarría" in txt, txt
    assert _eventos(phone, "mg_rebusca_mid")[0]["motivo"] == "franja"


def test_mid_y_hoy_sin_horas_hoy_dice_la_verdad_y_ofrece_lo_cercano(monkeypatch):
    _instalar_agenda(monkeypatch, {(73, MANANA_S): ["09:00", "09:15"]})
    phone = _arrancar([_slot(73, MANANA_S, "09:00"), _slot(73, MANANA_S, "09:15")])
    resp = _decir(phone, "¿y hoy?")
    txt = _texto(resp)
    assert "No tengo horas" in txt and "hoy" in txt, txt
    assert "próximas disponibles" not in txt
    assert "09:00" in txt                       # lo más cercano: mañana 09:00
    ids = [b["reply"]["id"] for b in resp["interactive"]["action"]["buttons"]]
    assert ids == ["confirmar_sugerido", "waitlist_antes_si"]   # reutiliza lista de espera existente
    assert _eventos(phone, "mg_rebusca_mid")[0]["resultado"] == "mas_cercano"


def test_mid_hoy_sin_horas_en_ningun_dia_ofrece_lista_de_espera(monkeypatch):
    _instalar_agenda(monkeypatch, {})
    phone = _arrancar([_slot(73, MANANA_S, "09:00")])
    resp = _decir(phone, "¿y hoy?")
    ids = [b["reply"]["id"] for b in resp["interactive"]["action"]["buttons"]]
    assert ids == ["waitlist_si", "waitlist_no"]
    assert get_session(phone)["state"] == "WAIT_WAITLIST_CONFIRM"


def test_mid_hoy_con_horas_hoy_las_muestra_con_hora_temprana(monkeypatch):
    h = (datetime.now(_TZ) + timedelta(hours=4)).hour
    if h > 19:
        pytest.skip("muy tarde para fabricar horas de hoy")
    tarde = f"{min(h + 3, 20):02d}:00"
    temprano = f"{h:02d}:15" if h >= 8 else "09:00"
    _instalar_agenda(monkeypatch, {(73, HOY_S): [tarde], (1, HOY_S): [temprano]})
    # a la hora de la oferta Abarca ya mostró mañana (no hay de hoy en pantalla)
    phone = _arrancar([_slot(73, MANANA_S, "09:00")])
    # buscar_slots_dia falso devuelve solo el primero con cupo; el camino real agrega
    # la hora temprana, probado arriba. Acá: se re-buscó hoy y hay lista.
    txt = _texto(_decir(phone, "y para hoy?"))
    assert tarde in txt or temprano in txt, txt
    assert _eventos(phone, "mg_rebusca_mid")[0]["resultado"] == "lista"


def test_pedido_cubierto_por_pantalla_no_rebusca(monkeypatch):
    _instalar_agenda(monkeypatch, {})
    phone = _arrancar([_slot(73, MANANA_S, "09:00"), _slot(1, MANANA_S, "16:30")])
    _decir(phone, "con Olavarría")      # ya está en pantalla: lo resuelve el handler existente
    assert _eventos(phone, "mg_rebusca_mid") == []


def test_otras_especialidades_no_pasan_por_el_rebuscador(monkeypatch):
    _instalar_agenda(monkeypatch, {})
    phone = _arrancar([_slot(73, MANANA_S, "09:00")])
    d = get_session(phone)["data"]
    d["especialidad"] = "kinesiología"
    save_session(phone, "WAIT_SLOT", d)
    _decir(phone, "en la tarde")
    assert _eventos(phone, "mg_rebusca_mid") == []


def test_slot_elegido_marcado_se_registra(monkeypatch):
    _instalar_agenda(monkeypatch, {(13, MANANA_S): ["16:00"]})
    phone = _arrancar([_slot(73, MANANA_S, "09:00")])
    _decir(phone, "con Márquez")
    _decir(phone, "1")
    ev = _eventos(phone, "mg_slot_elegido")
    assert ev and ev[0]["origen"] == "rebusca_medico" and ev[0]["id_profesional"] == 13


# ── 4. Tarjeta de primera oferta con hora temprana ──────────────────────────
def test_tarjeta_primera_oferta_muestra_ambas_y_boton_confirma_la_temprana(monkeypatch):
    _instalar_agenda(monkeypatch, {(73, MANANA_S): ["17:45", "18:00"]})
    alt = {**_slot(1, MANANA_S, "10:30"), "alt_temprana": True}

    async def _alt(fecha, base):
        assert base and base[0]["id_profesional"] == 73
        return dict(alt)
    monkeypatch.setattr(flows, "hora_temprana_mg", _alt)
    _n[0] += 1
    phone = f"569020{_n[0]:05d}"
    reset_session(phone)
    save_privacy_consent(phone, "accepted", method="test")
    resp = _correr(flows._iniciar_agendar(phone, {}, "medicina general"))
    txt = _texto(resp)
    assert "17:45" in txt and "10:30" in txt and "Olavarría" in txt, txt
    ids = [b["reply"]["id"] for b in resp["interactive"]["action"]["buttons"]]
    assert ids == ["confirmar_sugerido", "confirmar_temprano", "ver_otros"]
    assert all(len(b["reply"]["title"]) <= 20 for b in resp["interactive"]["action"]["buttons"])
    assert _eventos(phone, "mg_hora_temprana_mostrada")[0]["alt"] == "10:30"
    assert get_session(phone)["state"] == "WAIT_SLOT"
    # el paciente toca "Olavarría 10:30"
    _decir(phone, "confirmar_temprano")
    ev = _eventos(phone, "mg_slot_elegido")
    assert ev and ev[0]["alt_temprana"] is True and ev[0]["id_profesional"] == 1


def test_tarjeta_sin_alternativa_queda_igual(monkeypatch):
    _instalar_agenda(monkeypatch, {(73, MANANA_S): ["10:00", "10:15"]})

    async def _nada(fecha, base):
        return None
    monkeypatch.setattr(flows, "hora_temprana_mg", _nada)
    _n[0] += 1
    phone = f"569021{_n[0]:05d}"
    reset_session(phone)
    save_privacy_consent(phone, "accepted", method="test")
    resp = _correr(flows._iniciar_agendar(phone, {}, "medicina general"))
    ids = [b["reply"]["id"] for b in resp["interactive"]["action"]["buttons"]]
    assert ids == ["confirmar_sugerido", "ver_otros", "otro_prof"]
    assert _eventos(phone, "mg_hora_temprana_mostrada") == []


# ── 5. "Sí" + hoy/hora que coincide con lo ofrecido confirma; no re-busca ───
def _pantalla(slots):
    _n[0] += 1
    phone = f"569030{_n[0]:05d}"
    reset_session(phone)
    save_privacy_consent(phone, "accepted", method="test")
    save_session(phone, "WAIT_SLOT", {
        "especialidad": "medicina general", "slots": slots, "todos_slots": slots,
        "fechas_vistas": [slots[0]["fecha"]], "expansion_stage": 0,
        "prof_sugerido_id": slots[0]["id_profesional"], "slot_sugerido": slots[0]})
    return phone


def _confirmo(phone):
    return bool(_eventos(phone, "funnel_slot_elegido"))


def _hoy_ok():
    return datetime.now(_TZ).hour < 22


@pytest.mark.parametrize("texto", ["sí, para hoy", "si para hoy", "ya, la de hoy", "Sii para hoy", "dale, hoy"])
def test_si_para_hoy_con_hora_de_hoy_en_pantalla_confirma(monkeypatch, texto):
    if not _hoy_ok():
        pytest.skip("muy tarde")
    _instalar_agenda(monkeypatch, {})
    phone = _pantalla([_slot(73, HOY_S, "23:40")])
    _decir(phone, texto)
    assert _confirmo(phone), texto
    assert _eventos(phone, "mg_rebusca_mid") == []


@pytest.mark.parametrize("texto,hora,prof", [
    ("sí, la de las 10:30", "10:30", 1), ("sí, a las 10:30", "10:30", 1),
    ("sí la de las 17:45", "17:45", 73), ("ok a las 17:45", "17:45", 73)])
def test_si_con_hora_que_coincide_confirma_esa_hora(monkeypatch, texto, hora, prof):
    _instalar_agenda(monkeypatch, {})
    alt = {**_slot(1, MANANA_S, "10:30"), "alt_temprana": True}
    phone = _pantalla([_slot(73, MANANA_S, "17:45"), alt])
    _decir(phone, texto)
    assert _confirmo(phone), texto
    assert _eventos(phone, "mg_rebusca_mid") == []
    sel = get_session(phone)["data"].get("slot_elegido") or {}
    assert sel.get("hora_inicio", "")[:5] == hora and sel.get("id_profesional") == prof, sel


def test_si_para_hoy_con_dos_medicos_en_pantalla_sigue_ambiguo(monkeypatch):
    if not _hoy_ok():
        pytest.skip("muy tarde")
    _instalar_agenda(monkeypatch, {})
    alt = {**_slot(1, HOY_S, "23:00"), "alt_temprana": True}
    phone = _pantalla([_slot(73, HOY_S, "23:40"), alt])
    _decir(phone, "sí, para hoy")
    assert not _confirmo(phone)                       # no inventa cuál
    assert _eventos(phone, "mg_rebusca_mid") == []    # ni re-busca
    assert get_session(phone)["state"] == "WAIT_SLOT"
    resp = _decir(phone, "sí, para hoy")
    assert _texto(resp).startswith("¿Cuál te acomoda?"), _texto(resp)
    assert [b["reply"]["id"] for b in resp["interactive"]["action"]["buttons"]] == \
        ["confirmar_sugerido", "confirmar_temprano"]
    assert "próximas disponibles" not in _texto(resp)


def test_si_pero_en_la_tarde_rebusca(monkeypatch):
    _instalar_agenda(monkeypatch, {(1, MANANA_S): ["16:30"]})
    phone = _pantalla([_slot(73, MANANA_S, "09:00")])
    _decir(phone, "sí pero en la tarde")
    assert not _confirmo(phone)
    assert _eventos(phone, "mg_rebusca_mid")[0]["motivo"] == "franja"


def test_si_pero_con_marquez_rebusca(monkeypatch):
    _instalar_agenda(monkeypatch, {(13, MANANA_S): ["16:00"]})
    phone = _pantalla([_slot(73, MANANA_S, "09:00")])
    _decir(phone, "sí pero con Márquez")
    assert not _confirmo(phone)
    assert _eventos(phone, "mg_rebusca_mid")[0]["motivo"].startswith("medico")


def test_si_a_las_15_no_ofrecida_rebusca(monkeypatch):
    _instalar_agenda(monkeypatch, {(1, MANANA_S): ["15:00"]})
    phone = _pantalla([_slot(73, MANANA_S, "09:00")])
    _decir(phone, "sí, a las 15")
    assert not _confirmo(phone)
    assert _eventos(phone, "mg_rebusca_mid")[0]["motivo"] == "hora"


def test_si_para_hoy_con_oferta_de_manana_rebusca_hoy(monkeypatch):
    _instalar_agenda(monkeypatch, {})
    phone = _pantalla([_slot(73, MANANA_S, "09:00")])
    _decir(phone, "sí, para hoy")
    assert not _confirmo(phone)
    assert _eventos(phone, "mg_rebusca_mid")[0]["motivo"] == "hoy"


# ── 6. Franja: siempre re-busca en los tres médicos, ordenada por hora ──────
def _filas(resp):
    return [r["title"] for s in resp["interactive"]["action"]["sections"] for r in s["rows"]
            if r["id"].isdigit()]


def test_franja_tarde_abarca_primero_y_otros_no_entran_si_no_superan_umbral(monkeypatch):
    _instalar_agenda(monkeypatch, {
        (73, MANANA_S): ["15:00", "15:15", "17:45"], (1, MANANA_S): ["10:30", "14:00"],
        (13, MANANA_S): ["16:00"]})
    phone = _pantalla([_slot(73, MANANA_S, "09:00")])
    resp = _decir(phone, "en la tarde")
    assert _filas(resp) == ["15:00", "15:15", "17:45"], _filas(resp)   # Olavarría 14:00 = 60 min antes
    ev = _eventos(phone, "mg_rebusca_mid")[0]
    assert ev["motivo"] == "franja" and ev["resultado"] == "lista"


def test_franja_tarde_agrega_solo_la_temprana_del_otro_al_final(monkeypatch):
    _instalar_agenda(monkeypatch, {
        (73, MANANA_S): ["17:45", "18:00"], (1, MANANA_S): ["13:30", "13:45"],
        (13, MANANA_S): ["16:00", "16:20"]})
    phone = _pantalla([_slot(73, MANANA_S, "09:00")])
    resp = _decir(phone, "en la tarde")
    assert _filas(resp) == ["17:45 · Abarca", "18:00 · Abarca", "13:30 · Olavarría"], _filas(resp)


def test_franja_abarca_sin_nada_en_la_franja_muestra_los_otros_por_hora(monkeypatch):
    _instalar_agenda(monkeypatch, {
        (73, MANANA_S): ["09:00"], (1, MANANA_S): ["16:30"], (13, MANANA_S): ["16:00", "16:20"]})
    phone = _pantalla([_slot(73, MANANA_S, "09:00")])
    assert _filas(_decir(phone, "en la tarde")) == ["16:00 · Márquez", "16:20 · Márquez", "16:30 · Olavarría"]


def test_franja_despues_de_las_x_rebusca_hoy(monkeypatch):
    _instalar_agenda(monkeypatch, {(1, HOY_S): ["19:30"], (73, HOY_S): ["09:00"]})
    phone = _pantalla([_slot(73, HOY_S, "09:00")])
    _decir(phone, "después de las 7")
    assert _eventos(phone, "mg_rebusca_mid")[0]["fechas"] == [HOY_S]
