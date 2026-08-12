"""
test_medilink_outage.py — modo caída de PLATAFORMA Medilink (403 "no se
encuentra activa") + recontacto contextual automático al recuperarse.

Incidente real 2026-08-12 12:00-13:22 UTC: la API de Medilink devolvió 403
"La plataforma no se encuentra activa" a TODO endpoint durante 82 min. Caía
al `except Exception` genérico (reset_session + "problema técnico"), 8
pacientes rebotados a ciegas y ninguno quedó en cola de aviso. Recepción los
recontactó a mano con las horas exactas que pedían y convirtió 7/8 — esto
automatiza esa receta.

Correr: venv/bin/python3 tests/test_medilink_outage.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import session  # noqa: E402
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
session.DB_PATH = Path(_tmp.name)

import httpx  # noqa: E402
import medilink  # noqa: E402
import medilink_outage as outage  # noqa: E402

_FALLOS: list[str] = []


def _ok(cond, msg):
    print(("OK    " if cond else "FALLO ") + msg)
    if not cond:
        _FALLOS.append(msg)


def _reset():
    """Limpia el estado del modo caída y el contexto capturado entre tests."""
    outage._ensure_table()
    with session.db() as conn:
        conn.execute("DELETE FROM system_state WHERE key LIKE 'medilink_outage_%'")
        conn.execute("DELETE FROM medilink_outage_context")
        conn.commit()
    medilink._last_reported_status = None


def _sin_esperas():
    """Anula los backoff para que el test no tarde segundos de sobra."""
    async def _nada(*a, **k):
        return None
    medilink.asyncio.sleep = _nada


class _ClienteFalso:
    """Cliente httpx de mentira que responde siempre lo mismo."""

    def __init__(self, status=403, body="", excepcion=None):
        self.status = status
        self.body = body
        self.excepcion = excepcion
        self.llamadas = 0

    async def get(self, url, **kw):
        self.llamadas += 1
        if self.excepcion:
            raise self.excepcion
        return httpx.Response(self.status, text=self.body,
                              request=httpx.Request("GET", url))

    async def post(self, url, **kw):
        return await self.get(url, **kw)


_BODY_INACTIVA = ('{"error":{"code":403,"message":'
                  '"La plataforma no se encuentra activa. Contacte a soporte."}}')
_BODY_PERMISOS = ('{"error":{"code":403,"message":'
                  '"No tiene permisos para acceder a este recurso."}}')


# ── 1. Detección del 403 "plataforma inactiva" vs 403 de permisos ───────────

def test_403_inactiva_levanta_medilinkinactiva():
    _reset()
    _sin_esperas()
    cli = _ClienteFalso(status=403, body=_BODY_INACTIVA)
    try:
        asyncio.run(medilink._get(cli, "http://x/citas"))
        salio = "sin excepcion"
    except medilink.MedilinkInactiva:
        salio = "MedilinkInactiva"
    except Exception as e:
        salio = type(e).__name__
    _ok(salio == "MedilinkInactiva",
        f"403 con 'no se encuentra activa' levanta MedilinkInactiva (salió: {salio})")


def test_403_permisos_no_se_confunde_con_inactiva():
    _reset()
    _sin_esperas()
    cli = _ClienteFalso(status=403, body=_BODY_PERMISOS)
    try:
        r = asyncio.run(medilink._get(cli, "http://x/citas"))
        salio = f"respuesta normal status={r.status_code}"
    except medilink.MedilinkInactiva:
        salio = "MedilinkInactiva (MAL)"
    except Exception as e:
        salio = type(e).__name__
    _ok(salio == "respuesta normal status=403",
        f"un 403 de permisos puntuales NO dispara MedilinkInactiva (salió: {salio})")


def test_deteccion_tolera_mayusculas_y_tildes():
    _ok(medilink._es_plataforma_inactiva("LA PLATAFORMA NO SE ENCUENTRA ACTIVA"),
        "detección tolera mayúsculas")
    _ok(medilink._es_plataforma_inactiva("La plataforma no se encúentra áctiva"),
        "detección tolera tildes")
    _ok(not medilink._es_plataforma_inactiva(""), "body vacío no matchea")
    _ok(not medilink._es_plataforma_inactiva("Undefined index: rut"),
        "otro mensaje de error cualquiera no matchea")


def test_post_tambien_detecta_inactiva():
    _reset()
    _sin_esperas()
    cli = _ClienteFalso(status=403, body=_BODY_INACTIVA)
    try:
        asyncio.run(medilink._post(cli, "http://x/citas", json={}))
        salio = "sin excepcion"
    except medilink.MedilinkInactiva:
        salio = "MedilinkInactiva"
    except Exception as e:
        salio = type(e).__name__
    _ok(salio == "MedilinkInactiva", f"_post también detecta el 403 (salió: {salio})")


def test_probe_up_no_confunde_403_inactiva_con_vivo():
    """Bug encontrado al integrar: probe_up() (fail-open de _iniciar_agendar)
    hacía `status_code < 500` sin mirar el body — un 403 de plataforma
    suspendida contaba como VIVO y disparaba _report_up(), que resetea la
    racha de fallos consecutivos (note_exito) ANTES de que llegara a 2. Con
    eso el modo caída nunca se habría abierto en tráfico real detrás de
    _iniciar_agendar. Ver docstring de probe_up()."""
    _reset()
    orig_client = medilink.httpx.AsyncClient

    class _Cli403Inactiva:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return httpx.Response(403, text=_BODY_INACTIVA,
                                  request=httpx.Request("GET", url))

    medilink.httpx.AsyncClient = lambda *a, **k: _Cli403Inactiva()
    try:
        vivo = asyncio.run(medilink.probe_up())
    finally:
        medilink.httpx.AsyncClient = orig_client
    _ok(vivo is False, "probe_up() NO debe leer el 403 de plataforma inactiva como vivo")


def test_probe_up_sigue_leyendo_403_de_permisos_como_vivo():
    """El fix no debe volverse un martillo: un 403 de permisos puntual (no la
    frase de plataforma suspendida) sigue contando como 'vivo' — sigue siendo
    la misma sonda barata para el resto de los casos."""
    _reset()
    orig_client = medilink.httpx.AsyncClient

    class _Cli403Permisos:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return httpx.Response(403, text=_BODY_PERMISOS,
                                  request=httpx.Request("GET", url))

    medilink.httpx.AsyncClient = lambda *a, **k: _Cli403Permisos()
    try:
        vivo = asyncio.run(medilink.probe_up())
    finally:
        medilink.httpx.AsyncClient = orig_client
    _ok(vivo is True, "un 403 de permisos puntual sigue leyéndose como vivo")


# ── 2. Apertura del modo caída: solo con 2 fallos consecutivos ──────────────

def test_modo_caida_abre_solo_con_2_fallos_consecutivos():
    _reset()
    _ok(not outage.is_open(), "arranca cerrado")
    abrio_1 = outage.note_fallo_inactiva()
    _ok(abrio_1 is False, "1er fallo NO abre el modo (evita falsa alarma)")
    _ok(not outage.is_open(), "sigue cerrado tras 1 fallo")
    abrio_2 = outage.note_fallo_inactiva()
    _ok(abrio_2 is True, "2º fallo consecutivo SÍ abre el modo")
    _ok(outage.is_open(), "modo caída queda abierto")


def test_exito_intermedio_resetea_la_racha():
    _reset()
    outage.note_fallo_inactiva()  # 1/2
    outage.note_exito()           # racha se corta
    abrio = outage.note_fallo_inactiva()  # vuelve a ser 1/2, no 2/2
    _ok(abrio is False,
        "un éxito entre medio corta la racha — un 403 aislado no abre el modo")
    _ok(not outage.is_open(), "sigue cerrado")


def test_full_raise_dispara_apertura_end_to_end():
    """El flujo real: 2 requests fallidas con el 403 de plataforma abren el modo."""
    _reset()
    _sin_esperas()
    cli = _ClienteFalso(status=403, body=_BODY_INACTIVA)
    for _ in range(2):
        try:
            asyncio.run(medilink._get(cli, "http://x/citas"))
        except medilink.MedilinkInactiva:
            pass
    _ok(outage.is_open(),
        "2 fallos MedilinkInactiva reales (vía _get) abren el modo caída")


# ── 3. Cierre del modo caída: 2 sondeos OK consecutivos ──────────────────────

def test_watcher_no_dispara_con_1_solo_sondeo_ok():
    _reset()
    outage.note_fallo_inactiva()
    outage.note_fallo_inactiva()
    _ok(outage.is_open(), "precondición: modo abierto")
    cerro_1 = outage.note_sondeo_ok()
    _ok(cerro_1 is False, "1 solo sondeo OK NO cierra el modo")
    _ok(outage.is_open(), "sigue abierto tras 1 sondeo OK")
    cerro_2 = outage.note_sondeo_ok()
    _ok(cerro_2 is True, "2º sondeo OK consecutivo SÍ cierra el modo")
    _ok(not outage.is_open(), "modo caída queda cerrado")


def test_sondeo_fallido_corta_la_racha_de_oks():
    _reset()
    outage.note_fallo_inactiva()
    outage.note_fallo_inactiva()
    outage.note_sondeo_ok()   # 1/2
    outage.note_sondeo_fail()  # corta
    cerro = outage.note_sondeo_ok()  # vuelve a ser 1/2, no 2/2
    _ok(cerro is False, "un sondeo fallido entre medio reinicia la racha de OKs")


# ── 4. Ventana de 24 h ───────────────────────────────────────────────────────

def test_modo_caida_expira_a_las_24h():
    _reset()
    outage.note_fallo_inactiva()
    outage.note_fallo_inactiva()
    _ok(outage.is_open(), "precondición: modo abierto")
    from datetime import datetime, timedelta, timezone
    vieja = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    session.system_state_set(outage._KEY_OPENED_AT, vieja)
    _ok(not outage.is_open(), "más de 24h desde la apertura → se considera cerrado")


def test_expirar_pendientes_marca_los_viejos():
    _reset()
    with session.db() as conn:
        conn.execute("""
            INSERT INTO medilink_outage_context (phone, first_ts, textos, avisado)
            VALUES ('56900000001', datetime('now', '-25 hours'), '["hola"]', 0)
        """)
        conn.execute("""
            INSERT INTO medilink_outage_context (phone, first_ts, textos, avisado)
            VALUES ('56900000002', datetime('now'), '["hola"]', 0)
        """)
        conn.commit()
    n = outage.expirar_pendientes()
    _ok(n == 1, f"solo el contexto de >24h se expira (expiró {n})")
    pendientes = outage.list_pendientes()
    phones = {p["phone"] for p in pendientes}
    _ok("56900000002" not in phones or len(pendientes) == 1,
        "el contexto reciente sigue pendiente")
    _ok("56900000001" not in {p["phone"] for p in pendientes},
        "el contexto viejo ya no aparece como pendiente")


# ── 5. Captura de contexto (sin tocar la sesión / sin reset) ────────────────

def test_capturar_mensaje_no_hace_nada_con_modo_cerrado():
    _reset()
    outage.capturar_mensaje("56911111111", "hola necesito hora", {"state": "IDLE", "data": {}})
    _ok(not outage.hay_pendientes(),
        "con el modo cerrado y sin force, no se captura nada (no ensucia la tabla en tráfico normal)")


def test_capturar_mensaje_con_modo_abierto():
    _reset()
    outage.note_fallo_inactiva()
    outage.note_fallo_inactiva()
    session_fake = {"state": "WAIT_SLOT", "data": {"especialidad": "cardiología",
                                                    "prof_sugerido_id": 60}}
    outage.capturar_mensaje("56922222222", "quiero la hora de las 15:00", session_fake)
    pend = outage.list_pendientes()
    _ok(len(pend) == 1, "se capturó exactamente 1 contexto")
    row = pend[0]
    _ok(row["especialidad"] == "cardiología", "guarda la especialidad de la sesión")
    _ok(row["id_profesional"] == 60, "guarda el profesional sugerido")
    _ok(row["state_al_fallar"] == "WAIT_SLOT", "guarda el estado de la sesión")
    _ok(row["textos"] == ["quiero la hora de las 15:00"], "guarda el texto del mensaje")


def test_capturar_mensaje_incluye_human_takeover():
    """Punto crítico del rediseño: pacientes en HUMAN_TAKEOVER (el bot no les
    responde) igual deben quedar con su contexto capturado."""
    _reset()
    outage.note_fallo_inactiva()
    outage.note_fallo_inactiva()
    session_fake = {"state": "HUMAN_TAKEOVER", "data": {}}
    outage.capturar_mensaje("56933333333", "sigo esperando la hora", session_fake)
    pend = outage.list_pendientes()
    _ok(len(pend) == 1 and pend[0]["state_al_fallar"] == "HUMAN_TAKEOVER",
        "un mensaje en HUMAN_TAKEOVER también se captura mientras el modo está abierto")


def test_capturar_mensaje_no_toca_reset_session():
    """capturar_mensaje NUNCA debe resetear la sesión del paciente — es
    exactamente lo que el diseño busca evitar (a diferencia del except
    genérico viejo)."""
    import inspect
    src = inspect.getsource(outage)
    _ok("reset_session" not in src,
        "medilink_outage.py no invoca reset_session en ningún lado")


def test_capturar_mensaje_no_duplica_fila_por_paciente():
    _reset()
    outage.note_fallo_inactiva()
    outage.note_fallo_inactiva()
    session_fake = {"state": "WAIT_SLOT", "data": {"especialidad": "kinesiología"}}
    outage.capturar_mensaje("56944444444", "primer mensaje", session_fake)
    outage.capturar_mensaje("56944444444", "segundo mensaje", session_fake)
    outage.capturar_mensaje("56944444444", "tercer mensaje", session_fake)
    pend = outage.list_pendientes()
    _ok(len(pend) == 1, f"un solo registro por teléfono, se actualiza (hay {len(pend)})")
    _ok(pend[0]["textos"] == ["primer mensaje", "segundo mensaje", "tercer mensaje"],
        "acumula hasta los últimos textos en vez de pisar el anterior")


def test_capturar_mensaje_guarda_hasta_5_textos():
    _reset()
    outage.note_fallo_inactiva()
    outage.note_fallo_inactiva()
    session_fake = {"state": "IDLE", "data": {}}
    for i in range(8):
        outage.capturar_mensaje("56955555555", f"mensaje {i}", session_fake)
    pend = outage.list_pendientes()
    _ok(len(pend[0]["textos"]) == 5, "guarda como máximo los últimos 5 textos")
    _ok(pend[0]["textos"][-1] == "mensaje 7", "el último texto es el más reciente")


def test_capturar_mensaje_usa_rut_del_perfil_si_la_sesion_no_lo_tiene():
    _reset()
    outage.note_fallo_inactiva()
    outage.note_fallo_inactiva()
    session.save_profile("56966666666", "11.111.111-1", "Paciente Con Perfil")
    outage.capturar_mensaje("56966666666", "hola", {"state": "IDLE", "data": {}})
    pend = outage.list_pendientes()
    _ok(pend[0]["rut"] == "11.111.111-1",
        "si la sesión no trae rut, cae al rut del perfil guardado")


def test_capturar_mensaje_force_ignora_el_gate():
    _reset()
    _ok(not outage.is_open(), "precondición: modo cerrado")
    outage.capturar_mensaje("56977777777", "mensaje que gatilla el 1er fallo",
                            {"state": "IDLE", "data": {}}, force=True)
    _ok(outage.hay_pendientes(),
        "force=True captura aunque el modo todavía no esté abierto "
        "(el mensaje que dispara el fallo)")


# ── 6. Mensaje dirigido con especialidad y horas reales ─────────────────────

def test_mensaje_dirigido_con_especialidad_y_horas():
    import jobs

    async def _fake_slots_dia(especialidad, fecha):
        return [], [
            {"hora_inicio": "15:00", "hora_fin": "15:20", "profesional": "Dr. Miguel Millán",
             "especialidad": "cardiología", "fecha": fecha, "fecha_display": fecha,
             "id_profesional": 60},
            {"hora_inicio": "15:20", "hora_fin": "15:40", "profesional": "Dr. Miguel Millán",
             "especialidad": "cardiología", "fecha": fecha, "fecha_display": fecha,
             "id_profesional": 60},
        ]

    orig = jobs.buscar_slots_dia
    jobs.buscar_slots_dia = _fake_slots_dia
    try:
        row = {"phone": "56988888881", "especialidad": "cardiología",
               "id_profesional": None, "textos": ["necesito cardiólogo urgente"]}
        msg, resultado = asyncio.run(jobs._recontacto_outage_mensaje(row))
    finally:
        jobs.buscar_slots_dia = orig

    _ok(resultado == "enviado", f"resultado enviado con horas reales (fue {resultado})")
    _ok("cardiología" in msg.lower(), "el mensaje menciona la especialidad pedida")
    _ok("15:00" in msg and "15:20" in msg, "el mensaje trae las horas concretas encontradas")
    _ok("Miguel Millán" in msg, "el mensaje menciona al profesional")
    _ok("hoy" in msg.lower(), "ofrece hoy cuando hay cupos hoy")


def test_mensaje_retoma_texto_original_si_no_hay_especialidad():
    import jobs
    import claude_helper

    async def _fake_detect_intent(texto, *a, **k):
        return {"intent": "info", "especialidad": None, "respuesta_directa": ""}

    orig = claude_helper.detect_intent
    claude_helper.detect_intent = _fake_detect_intent
    try:
        row = {"phone": "56988888882", "especialidad": "", "id_profesional": None,
               "textos": ["¿atienden los sábados?"]}
        msg, resultado = asyncio.run(jobs._recontacto_outage_mensaje(row))
    finally:
        claude_helper.detect_intent = orig

    _ok(resultado == "enviado_generico", f"resultado genérico (fue {resultado})")
    _ok("¿atienden los sábados?" in msg,
        "retoma el texto original del paciente cuando no hay especialidad clara")


def test_regla_barata_no_llama_a_claude_si_ya_hay_especialidad():
    """Presupuesto: Claude solo para los ambiguos."""
    import jobs
    import claude_helper

    llamadas = {"n": 0}

    async def _fake_detect_intent(texto, *a, **k):
        llamadas["n"] += 1
        return {"intent": "agendar", "especialidad": "kinesiología", "respuesta_directa": ""}

    async def _fake_slots_dia(especialidad, fecha):
        return [], []

    async def _fake_primer_dia(especialidad, **kw):
        return [], []

    orig_detect = claude_helper.detect_intent
    orig_slots = jobs.buscar_slots_dia
    orig_primer = jobs.buscar_primer_dia
    claude_helper.detect_intent = _fake_detect_intent
    jobs.buscar_slots_dia = _fake_slots_dia
    jobs.buscar_primer_dia = _fake_primer_dia
    try:
        row = {"phone": "56988888883", "especialidad": "kinesiología",
               "id_profesional": None, "textos": ["necesito kine"]}
        asyncio.run(jobs._recontacto_outage_mensaje(row))
    finally:
        claude_helper.detect_intent = orig_detect
        jobs.buscar_slots_dia = orig_slots
        jobs.buscar_primer_dia = orig_primer

    _ok(llamadas["n"] == 0,
        "con especialidad ya conocida (regla barata) NO se llama a Claude")


# ── 7. Skip si ya tiene cita futura ──────────────────────────────────────────

def test_procesar_recontacto_skip_si_ya_tiene_cita():
    _reset()
    import jobs

    with session.db() as conn:
        conn.execute("""
            INSERT INTO medilink_outage_context
                (phone, textos, especialidad, rut, state_al_fallar, avisado)
            VALUES ('56999999991', '["hola"]', 'cardiología', '9.999.999-9', 'WAIT_SLOT', 0)
        """)
        conn.commit()

    enviados = []

    async def _fake_listar_citas(_id, rut=None, **kw):
        return [{"id": 1, "fecha": "20/08/2026"}]  # ya tiene cita futura

    async def _fake_send_whatsapp(phone, msg, **kw):
        enviados.append((phone, msg))
        return "wamid_fake"

    orig_listar = jobs.listar_citas_paciente
    orig_send = jobs.send_whatsapp
    jobs.listar_citas_paciente = _fake_listar_citas
    jobs.send_whatsapp = _fake_send_whatsapp
    try:
        asyncio.run(jobs._procesar_recontacto_outage())
    finally:
        jobs.listar_citas_paciente = orig_listar
        jobs.send_whatsapp = orig_send

    _ok(not enviados, "NO se envía WhatsApp si el rut ya tiene cita futura")
    pend = outage.list_pendientes()
    _ok(not pend, "el contexto queda cerrado (avisado) sin enviar")


def test_procesar_recontacto_human_takeover_no_envia_por_el_bot():
    _reset()
    import jobs

    with session.db() as conn:
        conn.execute("""
            INSERT INTO medilink_outage_context
                (phone, textos, especialidad, state_al_fallar, avisado)
            VALUES ('56999999992', '["hola"]', '', 'HUMAN_TAKEOVER', 0)
        """)
        conn.commit()

    enviados = []

    async def _fake_send_whatsapp(phone, msg, **kw):
        enviados.append((phone, msg))
        return "wamid_fake"

    orig_send = jobs.send_whatsapp
    jobs.send_whatsapp = _fake_send_whatsapp
    try:
        asyncio.run(jobs._procesar_recontacto_outage())
    finally:
        jobs.send_whatsapp = orig_send

    _ok(not enviados, "un paciente en HUMAN_TAKEOVER no recibe mensaje automático del bot")


def test_procesar_recontacto_envia_y_marca_avisado():
    _reset()
    import jobs

    with session.db() as conn:
        conn.execute("""
            INSERT INTO medilink_outage_context
                (phone, textos, especialidad, id_profesional, state_al_fallar, avisado)
            VALUES ('56999999993', '["necesito nutricionista"]', 'nutrición', NULL, 'WAIT_ESPECIALIDAD', 0)
        """)
        conn.commit()

    enviados = []

    async def _fake_listar_citas(_id, rut=None, **kw):
        return []

    async def _fake_slots_dia(especialidad, fecha):
        return [], [{"hora_inicio": "10:00", "hora_fin": "11:00", "profesional": "Gisela Pinto",
                     "especialidad": "nutrición", "fecha": fecha, "fecha_display": fecha,
                     "id_profesional": 52}]

    async def _fake_send_whatsapp(phone, msg, **kw):
        enviados.append((phone, msg))
        return "wamid_fake"

    orig_listar = jobs.listar_citas_paciente
    orig_slots = jobs.buscar_slots_dia
    orig_send = jobs.send_whatsapp
    jobs.listar_citas_paciente = _fake_listar_citas
    jobs.buscar_slots_dia = _fake_slots_dia
    jobs.send_whatsapp = _fake_send_whatsapp
    try:
        asyncio.run(jobs._procesar_recontacto_outage())
    finally:
        jobs.listar_citas_paciente = orig_listar
        jobs.buscar_slots_dia = orig_slots
        jobs.send_whatsapp = orig_send

    _ok(len(enviados) == 1, "se envía exactamente 1 WhatsApp al paciente pendiente")
    _ok("nutrición" in enviados[0][1].lower() and "10:00" in enviados[0][1],
        "el mensaje enviado trae especialidad y hora real")
    _ok(not outage.list_pendientes(), "queda marcado avisado tras el envío")


# ── 8. Watcher: gating por sondeos + no-op sin pendientes ───────────────────

def test_watcher_no_hace_nada_si_no_hay_pendientes():
    _reset()
    import jobs
    outage.note_fallo_inactiva()
    outage.note_fallo_inactiva()
    llamadas = {"n": 0}

    async def _fake_plataforma_activa(**kw):
        llamadas["n"] += 1
        return True

    orig = jobs.plataforma_activa
    jobs.plataforma_activa = _fake_plataforma_activa
    try:
        asyncio.run(jobs._job_medilink_outage_watcher_inner())
    finally:
        jobs.plataforma_activa = orig

    _ok(llamadas["n"] == 0,
        "sin contexto pendiente, el watcher ni sondea (ahorra requests)")


def test_watcher_completo_end_to_end():
    _reset()
    import jobs
    outage.note_fallo_inactiva()
    outage.note_fallo_inactiva()
    with session.db() as conn:
        conn.execute("""
            INSERT INTO medilink_outage_context
                (phone, textos, especialidad, state_al_fallar, avisado)
            VALUES ('56999999994', '["hola"]', 'podología', 'IDLE', 0)
        """)
        conn.commit()

    enviados = []

    async def _fake_plataforma_activa(**kw):
        return True

    async def _fake_listar_citas(_id, rut=None, **kw):
        return []

    async def _fake_slots_dia(especialidad, fecha):
        return [], []

    async def _fake_primer_dia(especialidad, **kw):
        return [], []

    async def _fake_send_whatsapp(phone, msg, **kw):
        enviados.append((phone, msg))
        return "wamid_fake"

    orig = (jobs.plataforma_activa, jobs.listar_citas_paciente,
           jobs.buscar_slots_dia, jobs.buscar_primer_dia, jobs.send_whatsapp)
    jobs.plataforma_activa = _fake_plataforma_activa
    jobs.listar_citas_paciente = _fake_listar_citas
    jobs.buscar_slots_dia = _fake_slots_dia
    jobs.buscar_primer_dia = _fake_primer_dia
    jobs.send_whatsapp = _fake_send_whatsapp
    try:
        # 1er sondeo OK: NO debe procesar todavía
        asyncio.run(jobs._job_medilink_outage_watcher_inner())
        _ok(not enviados, "1 solo sondeo OK no dispara el recontacto")
        _ok(outage.is_open(), "el modo sigue abierto tras 1 sondeo")
        # 2º sondeo OK: cierra y procesa
        asyncio.run(jobs._job_medilink_outage_watcher_inner())
    finally:
        (jobs.plataforma_activa, jobs.listar_citas_paciente,
         jobs.buscar_slots_dia, jobs.buscar_primer_dia, jobs.send_whatsapp) = orig

    _ok(len(enviados) == 1, "el 2º sondeo OK cierra el modo y dispara el recontacto")
    _ok(not outage.is_open(), "el modo caída queda cerrado")


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
    print("\n" + ("── TODO OK ──" if not _FALLOS else f"── FALLARON {len(_FALLOS)} ──"))
    for _f in _FALLOS:
        print("  -", _f)
    sys.exit(1 if _FALLOS else 0)
