"""Tests offline del chasis de orquestadores de Alma — sin red.

Fijan: registro completo, dry-run seguro con todo apagado, propose() real con
datos sembrados, y el gating de modos (OFF no encola; ON encola).

Correr: `python3 tests/test_alma_orchestrators.py`
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import session  # noqa: E402
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _tmp.close()
session.DB_PATH = Path(_tmp.name)
# Cola de propuestas a archivo temporal (no tocar la real).
_props = tempfile.NamedTemporaryFile(suffix=".json", delete=False); _props.close()
os.environ["ALMA_BRAIN_PROPOSALS_PATH"] = _props.name

from alma_brain import orchestrators  # noqa: E402
from alma_brain import tools  # noqa: E402


def _ok(cond, msg):
    print(("OK   " if cond else "FALLO ") + msg)
    assert cond, msg


def test_registry_completo_y_off():
    _ok(len(orchestrators.REGISTRY) == 23, f"23 orquestadores registrados ({len(orchestrators.REGISTRY)})")
    cat = orchestrators.catalog()
    _ok(all(not c["enabled"] for c in cat), "todos OFF por defecto")
    _ok(all(c["flag"].startswith("ALMA_ORQ_") for c in cat), "cada uno con su flag dedicado")


def test_dry_no_persiste_aunque_haya_datos():
    session.add_resultado_pendiente("56900000001", "Hemograma", nombre="Ana")
    r = asyncio.run(orchestrators.run_one("resultados_examenes", mode="dry"))
    _ok(len(r["proposals"]) == 1, f"dry propone 1 worklist de resultados ({len(r['proposals'])})")
    _ok(r["proposals"][0]["params"]["worklist"][0]["examen"] == "Hemograma", "worklist trae el examen")
    _ok(len(tools.load_proposals()) == 0, "dry NO encola nada en la cola real")


def test_confirmaciones_detecta_sin_confirmar():
    manana = (date.today() + timedelta(days=1)).isoformat()
    with session._conn() as c:
        c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora) "
                  "VALUES (?,?,?,?,?,?)",
                  ("56911112222", "CITA-X", "Medicina General", "Dr. Olavarría", manana, "10:00"))
        c.commit()
    r = asyncio.run(orchestrators.run_one("confirmaciones", mode="dry"))
    _ok(len(r["proposals"]) == 1, f"propone confirmar la cita de mañana ({len(r['proposals'])})")
    wl = r["proposals"][0]["params"]["worklist"]
    _ok(wl and wl[0]["telefono"] == "56911112222", "worklist trae la cita sin confirmar")


def test_modo_propose_gateado_off_no_encola():
    os.environ.pop("ALMA_ORQ_RESULTADOS_EXAMENES_ENABLED", None)
    before = len(tools.load_proposals())
    r = asyncio.run(orchestrators.run_one("resultados_examenes", mode="propose"))
    _ok("gateado OFF" in r["notes"], "modo propose con flag OFF → no actúa")
    _ok(len(tools.load_proposals()) == before, "no encoló nada con el flag apagado")


def test_modo_propose_on_encola():
    os.environ["ALMA_ORQ_RESULTADOS_EXAMENES_ENABLED"] = "true"
    before = len(tools.load_proposals())
    r = asyncio.run(orchestrators.run_one("resultados_examenes", mode="propose"))
    _ok(len(tools.load_proposals()) == before + 1, "con el flag ON encola 1 propuesta")
    _ok(r["enabled"], "reporta enabled=True")
    os.environ.pop("ALMA_ORQ_RESULTADOS_EXAMENES_ENABLED", None)


def test_ges_backlog_detecta_sin_agendar():
    # Uno con síntoma GES urgente que NO agendó; otro que SÍ agendó después.
    session.log_event("56933330001", "triage_ges_match", {"especialidad": "cardiología", "urgency": True})
    session.log_event("56933330002", "triage_ges_match", {"especialidad": "nutrición", "urgency": False})
    with session._conn() as c:
        c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, fecha, hora, created_at) "
                  "VALUES (?,?,?,?,?, datetime('now','+1 second'))",
                  ("56933330002", "C-GES", "Nutrición", "2030-01-01", "10:00"))
        c.commit()
    r = asyncio.run(orchestrators.run_one("ges_backlog", mode="dry"))
    _ok(len(r["proposals"]) == 1, f"propone seguir el backlog GES ({len(r['proposals'])})")
    phones = [w["telefono"] for w in r["proposals"][0]["params"]["worklist"]]
    _ok("56933330001" in phones, "el que no agendó está en la worklist")
    _ok("56933330002" not in phones, "el que agendó después queda fuera")


def test_agenda_salud_degrada_sin_bi():
    # Sin BI (entorno de test) → sense degrada, no crashea, no propone.
    r = asyncio.run(orchestrators.run_one("agenda_salud", mode="dry"))
    _ok(r["proposals"] == [], "sin BI no propone")
    _ok("error" not in r, "no propaga error")


def test_pagina_referencia_endpoints():
    from pathlib import Path
    html = (Path(__file__).parent.parent / "templates" / "alma_orquestadores.html").read_text(encoding="utf-8")
    for ep in ("/admin/api/orquestadores", "/admin/api/orquestadores/propuestas",
               "/admin/api/orquestadores/snapshot", "/admin/api/orquestadores/briefing"):
        _ok(ep in html, f"la página referencia {ep}")
    _ok("__TOKEN__" in html, "placeholder de token presente en la página")


def test_snapshot_persiste_y_lee():
    from alma_brain.orchestrators import snapshot
    snapf = tempfile.NamedTemporaryFile(suffix=".json", delete=False); snapf.close()
    snapshot.SNAPSHOT_PATH = snapf.name
    snap = asyncio.run(snapshot.build_and_save())
    _ok(snap["n_orquestadores"] == 23, f"snapshot cubre los 23 ({snap['n_orquestadores']})")
    loaded = snapshot.load_snapshot()
    _ok(loaded is not None and loaded["n_orquestadores"] == 23, "snapshot se relee del disco")
    _ok("generated_at" in loaded and "resultados" in loaded, "snapshot tiene estructura esperada")
    try: os.unlink(snapf.name)
    except OSError: pass


def test_briefing_prioriza():
    from alma_brain.orchestrators import briefing, snapshot
    snapf = tempfile.NamedTemporaryFile(suffix=".json", delete=False); snapf.close()
    snapshot.SNAPSHOT_PATH = snapf.name
    # A esta altura el DB temporal ya tiene un resultado pendiente y eventos GES sembrados.
    b = asyncio.run(briefing.build_briefing(refresh=True))
    _ok(b["n_acciones"] >= 1, f"el briefing junta acciones ({b['n_acciones']})")
    names = [it["orquestador"] for it in b["items"]]
    _ok("ges_backlog" in names or "resultados_examenes" in names, "incluye orquestadores con datos sembrados")
    # Orden: si hay alguna alta prioridad, va primero.
    if any(it["alta_prioridad"] for it in b["items"]):
        _ok(b["items"][0]["alta_prioridad"], "una acción de alta prioridad encabeza el briefing")
    _ok("por_dominio" in b and "n_personas" in b, "briefing tiene agregados por dominio y personas")
    try: os.unlink(snapf.name)
    except OSError: pass


def test_control_cronico_detecta_sin_control():
    session.save_tag("56944440001", "dx:hta")  # crónico SIN cita → candidato
    session.save_tag("56944440002", "dx:dm2")  # crónico con cita reciente → excluido
    with session._conn() as c:
        c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, fecha, hora) VALUES (?,?,?,?,?)",
                  ("56944440002", "C-CRON", "Medicina General", date.today().isoformat(), "09:00"))
        c.commit()
    r = asyncio.run(orchestrators.run_one("control_cronico", mode="dry"))
    _ok(len(r["proposals"]) == 1, f"propone control crónico ({len(r['proposals'])})")
    phones = [w["telefono"] for w in r["proposals"][0]["params"]["worklist"]]
    _ok("56944440001" in phones, "el crónico sin control reciente está")
    _ok("56944440002" not in phones, "el crónico con cita reciente queda fuera")


def test_ficha_incompleta_degrada_sin_bi():
    r = asyncio.run(orchestrators.run_one("ficha_incompleta", mode="dry"))
    _ok(r["proposals"] == [], "sin BI no propone")
    _ok("error" not in r, "no propaga error")


def test_ads_anomalia_no_crashea():
    # Según haya o no snapshot del autopilot en el entorno, propone 0..N; nunca crashea.
    r = asyncio.run(orchestrators.run_one("ads_anomalia", mode="dry"))
    _ok(isinstance(r["proposals"], list), "devuelve lista de propuestas (0..N)")
    _ok("error" not in r, "no propaga error")
    st = r["signals"].get("source_status")
    _ok(st in ("ok", "unavailable"), f"source_status sano ({st})")


def test_referral_sin_cerrar_detecta():
    import time
    now = int(time.time())
    with session._conn() as c:
        # Lead de Meta hace 1 día que NO agendó → candidato.
        c.execute("INSERT INTO meta_referrals (phone, source_type, headline, ts) VALUES (?,?,?,?)",
                  ("56955550001", "ad", "Eco mamaria $25.000", now - 86400))
        # Lead que SÍ agendó después → excluido.
        c.execute("INSERT INTO meta_referrals (phone, source_type, headline, ts) VALUES (?,?,?,?)",
                  ("56955550002", "ad", "Cardiología", now - 86400))
        c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, fecha, hora, created_at) "
                  "VALUES (?,?,?,?,?, datetime('now'))",
                  ("56955550002", "C-REF", "Cardiología", "2030-02-02", "10:00"))
        c.commit()
    r = asyncio.run(orchestrators.run_one("referral_sin_cerrar", mode="dry"))
    _ok(len(r["proposals"]) == 1, f"propone cerrar leads ({len(r['proposals'])})")
    phones = [w["telefono"] for w in r["proposals"][0]["params"]["worklist"]]
    _ok("56955550001" in phones, "el lead sin agendar está")
    _ok("56955550002" not in phones, "el lead que agendó queda fuera")


def test_conversacion_parada_detecta():
    with session._conn() as c:
        # Último mensaje del paciente hace 3h, sin respuesta → candidato.
        c.execute("INSERT INTO messages (phone, direction, text, ts) VALUES (?,?,?, datetime('now','-3 hours'))",
                  ("56966660001", "in", "Hola, quiero una hora"))
        # Conversación con respuesta posterior del bot → excluida.
        c.execute("INSERT INTO messages (phone, direction, text, ts) VALUES (?,?,?, datetime('now','-4 hours'))",
                  ("56966660002", "in", "Buenas"))
        c.execute("INSERT INTO messages (phone, direction, text, ts) VALUES (?,?,?, datetime('now','-3 hours'))",
                  ("56966660002", "out", "Hola! Te ayudo"))
        c.commit()
    r = asyncio.run(orchestrators.run_one("conversacion_parada", mode="dry"))
    _ok(len(r["proposals"]) == 1, f"propone responder conversaciones paradas ({len(r['proposals'])})")
    phones = [w["telefono"] for w in r["proposals"][0]["params"]["worklist"]]
    _ok("56966660001" in phones, "la parada sin respuesta está")
    _ok("56966660002" not in phones, "la que tuvo respuesta del bot queda fuera")


def test_metrics_endpoint():
    import admin_routes
    m = admin_routes.admin_orq_metrics("token")
    _ok(m["total"] == 23, f"métricas: total 23 ({m['total']})")
    _ok(m["apagados"] == 23 and m["encendidos"] == 0, "todos apagados en métricas")
    _ok(sum(m["por_dominio"].values()) == m["total"], "por_dominio suma el total")


def test_pagina_tiene_filtro_dominio():
    html = (Path(__file__).parent.parent / "templates" / "alma_orquestadores.html").read_text(encoding="utf-8")
    for token in ("filterDom", 'id="chips"', "data-dom"):
        _ok(token in html, f"la página tiene el filtro por dominio: {token}")


def test_run_all_dry_no_crashea():
    res = asyncio.run(orchestrators.run_all(mode="dry"))
    _ok(len(res) == 23, f"run_all devuelve los 23 ({len(res)})")
    _ok(all("error" not in x or x.get("name") for x in res), "ninguno tumba el barrido")


if __name__ == "__main__":
    tests = [
        test_registry_completo_y_off,
        test_dry_no_persiste_aunque_haya_datos,
        test_confirmaciones_detecta_sin_confirmar,
        test_modo_propose_gateado_off_no_encola,
        test_modo_propose_on_encola,
        test_ges_backlog_detecta_sin_agendar,
        test_agenda_salud_degrada_sin_bi,
        test_pagina_referencia_endpoints,
        test_snapshot_persiste_y_lee,
        test_briefing_prioriza,
        test_control_cronico_detecta_sin_control,
        test_ficha_incompleta_degrada_sin_bi,
        test_ads_anomalia_no_crashea,
        test_referral_sin_cerrar_detecta,
        test_conversacion_parada_detecta,
        test_metrics_endpoint,
        test_pagina_tiene_filtro_dominio,
        test_run_all_dry_no_crashea,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)}/{len(tests)} tests OK — chasis de orquestadores")
    for p in (_tmp.name, _props.name):
        try: os.unlink(p)
        except OSError: pass
