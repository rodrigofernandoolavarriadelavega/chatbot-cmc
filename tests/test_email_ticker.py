"""Tests del parser/cruce de app/email_ticker.py — offline, sin IMAP real.

Fixtures en tests/fixtures/email_ticker/ (correos reales de producción con
nombres de pacientes anonimizados, estructura y campos intactos).
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import email_ticker as et  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "email_ticker"
_CL = ZoneInfo("America/Santiago")


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── parse_email_body: los 3 tipos reales ────────────────────────────────────

def test_parse_cita_agendada():
    body = _load("cita_agendada.txt")
    dt = datetime(2026, 7, 13, 22, 53, 51, tzinfo=_CL)  # correo real: mismo mes que la cita
    r = et.parse_email_body("Cita agendada", body, dt)
    assert r is not None
    assert r["tipo"] == "agendada"
    assert r["paciente_nombre"] == "Paciente De Prueba Uno"
    assert r["profesional_nombre"] == "Gisela del Pilar Pinto Tirapeguy"
    assert r["fecha_cita"] == "2026-08-17"
    assert r["hora_cita"] == "18:30"


def test_parse_cita_anulada():
    body = _load("cita_anulada.txt")
    dt = datetime(2026, 7, 13, 21, 4, 18, tzinfo=_CL)
    r = et.parse_email_body("Cita anulada", body, dt)
    assert r is not None
    assert r["tipo"] == "anulada"
    assert r["paciente_nombre"] == "Paciente De Prueba Dos"
    assert r["profesional_nombre"] == "Rodrigo Olavarría de la Vega"
    assert r["fecha_cita"] == "2026-07-13"
    assert r["hora_cita"] == "17:00"


def test_parse_cita_reagendada():
    body = _load("cita_reagendada.txt")
    dt = datetime(2026, 7, 13, 19, 40, 25, tzinfo=_CL)
    r = et.parse_email_body("Cita reagendada", body, dt)
    assert r is not None
    assert r["tipo"] == "reagendada"
    assert r["paciente_nombre"] == "Paciente De Prueba Tres"
    assert r["profesional_nombre"] == "Miguel Millan"
    assert r["fecha_cita"] == "2026-07-25"
    assert r["hora_cita"] == "11:20"


def test_parse_fallback_html():
    """Si no hay parte text/plain (cambio de template), _strip_html debe
    dejar el cuerpo parseable igual."""
    html = _load("cita_agendada.html")
    stripped = et._strip_html(html)
    dt = datetime(2026, 8, 20, 10, 0, 0, tzinfo=_CL)
    r = et.parse_email_body("Cita agendada", stripped, dt)
    assert r is not None
    assert r["paciente_nombre"] == "Paciente De Prueba Cuatro"
    assert r["profesional_nombre"] == "Andres Abarca Rojas"
    assert r["fecha_cita"] == "2026-09-03"
    assert r["hora_cita"] == "09:15"


def test_parse_asunto_no_reconocido_devuelve_none():
    body = _load("cita_agendada.txt")
    dt = datetime(2026, 7, 13, 22, 0, 0, tzinfo=_CL)
    assert et.parse_email_body("Confirma tu cita", body, dt) is None


def test_parse_cuerpo_incompleto_devuelve_none():
    dt = datetime(2026, 7, 13, 22, 0, 0, tzinfo=_CL)
    assert et.parse_email_body("Cita agendada", "texto sin campos reconocibles", dt) is None


# ── Inferencia de año (sin año en el correo, cuidado con dic→ene) ──────────

def test_infer_year_mismo_mes():
    dt = datetime(2026, 7, 13, tzinfo=_CL)
    assert et._infer_fecha_iso(17, "julio", dt) == "2026-07-17"


def test_infer_year_mes_siguiente_cercano():
    dt = datetime(2026, 7, 13, tzinfo=_CL)
    assert et._infer_fecha_iso(3, "agosto", dt) == "2026-08-03"


def test_infer_year_salto_diciembre_a_enero():
    # correo llega en diciembre, la cita es en enero → año SIGUIENTE
    dt = datetime(2026, 12, 30, tzinfo=_CL)
    assert et._infer_fecha_iso(5, "enero", dt) == "2027-01-05"


def test_infer_year_salto_enero_a_diciembre():
    # correo llega en enero, la cita referenciada es de diciembre → año ANTERIOR
    dt = datetime(2027, 1, 3, tzinfo=_CL)
    assert et._infer_fecha_iso(28, "diciembre", dt) == "2026-12-28"


def test_infer_year_mes_invalido():
    dt = datetime(2026, 7, 13, tzinfo=_CL)
    assert et._infer_fecha_iso(17, "mesinventado", dt) is None


def test_infer_year_dia_invalido_31_febrero():
    dt = datetime(2026, 2, 1, tzinfo=_CL)
    assert et._infer_fecha_iso(31, "febrero", dt) is None


# ── Normalización / match de nombres ────────────────────────────────────────

def test_nombres_coinciden_exacto_con_tildes():
    assert et._nombres_coinciden("Cristina Reyes Henríquez", "cristina reyes henriquez")


def test_nombres_coinciden_contencion():
    assert et._nombres_coinciden("Gabriela", "Gabriela Soto Muñoz")


def test_nombres_no_coinciden():
    assert not et._nombres_coinciden("Cristina Reyes", "Andrea Guevara")


def test_nombres_coinciden_vacio_es_falso():
    assert not et._nombres_coinciden("", "Cristina Reyes")


# ── Fecha Medilink (DD/MM/YYYY) → ISO ───────────────────────────────────────

def test_fecha_medilink_ddmmyyyy():
    assert et._fecha_medilink_a_iso("17/08/2026") == "2026-08-17"


def test_fecha_medilink_iso_passthrough():
    assert et._fecha_medilink_a_iso("2026-08-17") == "2026-08-17"


def test_fecha_medilink_invalida():
    assert et._fecha_medilink_a_iso("no-es-fecha") is None


def test_fecha_medilink_vacia():
    assert et._fecha_medilink_a_iso("") is None


# ── Cruce contra agenda_ticker (con DB temporal, aislado de prod) ──────────

def _setup_temp_db(tmp_path, monkeypatch):
    import session as _session
    db_path = tmp_path / "test_email_ticker.db"
    monkeypatch.setattr(_session, "DB_PATH", db_path)
    monkeypatch.setattr(_session, "_DDL_DONE", False)
    monkeypatch.setattr(_session, "_DDL_DONE_PATH", None)
    from agenda_ticker import ensure_agenda_ticker_table
    ensure_agenda_ticker_table()
    return _session


def test_cruce_exitoso_un_solo_candidato(tmp_path, monkeypatch):
    _session = _setup_temp_db(tmp_path, monkeypatch)
    with _session.db() as conn:
        conn.execute("""
            INSERT INTO agenda_ticker (id_cita, profesional, paciente_nombre, fecha_cita, hora_inicio)
            VALUES (9001, 'Rodrigo Olavarría de la Vega', 'Cristina Reyes Henríquez', '13/07/2026', '20:00')
        """)
        conn.commit()
    id_cita, status = et.cruzar_con_agenda_ticker(
        "Cristina Reyes Henríquez", "Rodrigo Olavarría de la Vega", "2026-07-13", "20:00"
    )
    assert id_cita == 9001
    assert status == "cruzado"


def test_cruce_sin_candidatos_no_adivina(tmp_path, monkeypatch):
    _setup_temp_db(tmp_path, monkeypatch)
    id_cita, status = et.cruzar_con_agenda_ticker(
        "Paciente Que No Existe", "Dr. Nadie", "2026-07-13", "20:00"
    )
    assert id_cita is None
    assert status == "no_cruzado"


def test_cruce_ambiguo_no_adivina(tmp_path, monkeypatch):
    """2 citas a la misma hora con el mismo paciente/profesional (dato sucio
    o duplicado) → no se adivina cuál es, se marca no_cruzado."""
    _session = _setup_temp_db(tmp_path, monkeypatch)
    with _session.db() as conn:
        conn.execute("""
            INSERT INTO agenda_ticker (id_cita, profesional, paciente_nombre, fecha_cita, hora_inicio)
            VALUES (9002, 'Rodrigo Olavarría de la Vega', 'Cristina Reyes Henríquez', '13/07/2026', '20:00')
        """)
        conn.execute("""
            INSERT INTO agenda_ticker (id_cita, profesional, paciente_nombre, fecha_cita, hora_inicio)
            VALUES (9003, 'Rodrigo Olavarría de la Vega', 'Cristina Reyes Henríquez', '13/07/2026', '20:00')
        """)
        conn.commit()
    id_cita, status = et.cruzar_con_agenda_ticker(
        "Cristina Reyes Henríquez", "Rodrigo Olavarría de la Vega", "2026-07-13", "20:00"
    )
    assert id_cita is None
    assert status == "no_cruzado"


def test_ensure_table_es_idempotente(tmp_path, monkeypatch):
    _setup_temp_db(tmp_path, monkeypatch)
    et.ensure_email_ticker_table()
    et.ensure_email_ticker_table()  # segunda vez no debe reventar


# ── Respaldo Medilink en vivo (una consulta por fecha, no por correo) ──────

def test_match_en_citas_medilink_un_candidato():
    citas = [
        {"id": 111, "nombre_paciente": "Cristina Reyes Henríquez",
         "nombre_profesional": "Rodrigo Olavarría de la Vega", "hora_inicio": "20:00:00"},
        {"id": 222, "nombre_paciente": "Otra Persona",
         "nombre_profesional": "Andrés Abarca Rojas", "hora_inicio": "20:00:00"},
    ]
    id_cita = et._match_en_citas_medilink("Cristina Reyes Henríquez", "Rodrigo Olavarría de la Vega", "20:00", citas)
    assert id_cita == 111


def test_match_en_citas_medilink_sin_candidatos():
    citas = [{"id": 111, "nombre_paciente": "Otra Persona", "nombre_profesional": "Alguien", "hora_inicio": "20:00:00"}]
    assert et._match_en_citas_medilink("Cristina Reyes", "Rodrigo Olavarría", "20:00", citas) is None


def test_match_en_citas_medilink_ambiguo():
    citas = [
        {"id": 111, "nombre_paciente": "Cristina Reyes Henríquez",
         "nombre_profesional": "Rodrigo Olavarría de la Vega", "hora_inicio": "20:00:00"},
        {"id": 112, "nombre_paciente": "Cristina Reyes Henríquez",
         "nombre_profesional": "Rodrigo Olavarría de la Vega", "hora_inicio": "20:00:00"},
    ]
    assert et._match_en_citas_medilink("Cristina Reyes Henríquez", "Rodrigo Olavarría de la Vega", "20:00", citas) is None


def test_resolver_cruces_pendientes_usa_una_sola_consulta_por_fecha(monkeypatch):
    """2 correos no cruzados localmente, MISMA fecha → debe consultar
    Medilink UNA sola vez para esa fecha (no una por correo)."""
    import asyncio

    llamadas = []

    async def fake_fetch(client, headers, fecha_iso, max_paginas=8):
        llamadas.append(fecha_iso)
        if fecha_iso == "2026-07-13":
            return [
                {"id": 501, "nombre_paciente": "Paciente Uno", "nombre_profesional": "Dr. Uno", "hora_inicio": "10:00:00"},
                {"id": 502, "nombre_paciente": "Paciente Dos", "nombre_profesional": "Dr. Dos", "hora_inicio": "11:00:00"},
            ]
        return []

    monkeypatch.setattr(et, "_fetch_citas_medilink_por_fecha", fake_fetch)

    class _FakeClient:
        pass

    import medilink
    monkeypatch.setattr(medilink, "_get_shared_client", lambda: _FakeClient())
    monkeypatch.setattr(medilink, "HEADERS", {})

    registros = [
        {"m": {"uid": 1}, "parsed": {"paciente_nombre": "Paciente Uno", "profesional_nombre": "Dr. Uno",
                                      "fecha_cita": "2026-07-13", "hora_cita": "10:00"},
         "id_cita": None, "cruce_status": "no_cruzado"},
        {"m": {"uid": 2}, "parsed": {"paciente_nombre": "Paciente Dos", "profesional_nombre": "Dr. Dos",
                                      "fecha_cita": "2026-07-13", "hora_cita": "11:00"},
         "id_cita": None, "cruce_status": "no_cruzado"},
    ]
    asyncio.run(et.resolver_cruces_pendientes(registros))

    assert llamadas == ["2026-07-13"]  # UNA sola consulta para las 2 fechas iguales
    assert registros[0]["id_cita"] == 501
    assert registros[0]["cruce_status"] == "cruzado"
    assert registros[1]["id_cita"] == 502
    assert registros[1]["cruce_status"] == "cruzado"


def test_resolver_cruces_pendientes_no_toca_ya_cruzados(monkeypatch):
    """Un registro que YA cruzó local no debe disparar consulta a Medilink."""
    import asyncio

    async def fake_fetch(*a, **k):
        raise AssertionError("no debería llamarse: no hay pendientes")

    monkeypatch.setattr(et, "_fetch_citas_medilink_por_fecha", fake_fetch)

    registros = [
        {"m": {"uid": 1}, "parsed": {"paciente_nombre": "X", "profesional_nombre": "Y",
                                      "fecha_cita": "2026-07-13", "hora_cita": "10:00"},
         "id_cita": 999, "cruce_status": "cruzado"},
    ]
    asyncio.run(et.resolver_cruces_pendientes(registros))
    assert registros[0]["id_cita"] == 999  # sin cambios
