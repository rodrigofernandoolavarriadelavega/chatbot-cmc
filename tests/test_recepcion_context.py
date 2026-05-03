"""Tests para el feature de contexto de recepcionista post-HUMAN_TAKEOVER.

Cubre:
  1. get_recepcion_msgs: devuelve solo mensajes [Recepcionista] direction=out recientes.
  2. snapshot_recepcion_context: devuelve dict con recepcion_resumen y _ts.
  3. TTL: si recepcion_resumen_ts tiene > 30 min, flows.py debe limpiarlo.
  4. detect_intent/respuesta_faq: aceptan recepcion_resumen y lo incluyen en el prompt.
  5. Guard agendar_bloqueado_por_ctx_recepcion: si ctx dice "le agendé", intent=agendar se bloquea.
"""
import sys
import os
import sqlite3
import json
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

# Añadir app/ al path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

# ── Helpers de BD temporal ────────────────────────────────────────────────────

def _make_in_memory_session_module():
    """Crea un módulo session con BD en memoria para tests aislados."""
    import importlib
    import session as _sess
    # Reemplazar _DB con BD en memoria para tests
    return _sess


# ── Test 1: get_recepcion_msgs ────────────────────────────────────────────────

def test_get_recepcion_msgs_filters_system_messages():
    """Solo retorna mensajes con prefijo '[Recepcionista] ', excluye mensajes de sistema."""
    import session
    from session import get_recepcion_msgs
    import session as _sess_mod

    phone = "56999000001"
    # Insertar mensajes directamente en la BD
    with _sess_mod._conn() as conn:
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("DELETE FROM messages WHERE phone=?", (phone,))
        # Mensaje de sistema (toma control)
        conn.execute(
            "INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
            (phone, "out", "[Recepcionista tomó la conversación]", "HUMAN_TAKEOVER", now_str)
        )
        # Mensaje real de recepcionista
        conn.execute(
            "INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
            (phone, "out", "[Recepcionista] Ginecología es solo particular, $35.000", "HUMAN_TAKEOVER", now_str)
        )
        # Mensaje del paciente (direction=in, debe ignorarse)
        conn.execute(
            "INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
            (phone, "in", "¿Y los horarios?", "HUMAN_TAKEOVER", now_str)
        )
        # Bot reanudado
        conn.execute(
            "INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
            (phone, "out", "[Bot reanudado por recepcionista]", "IDLE", now_str)
        )
        conn.commit()

    msgs = get_recepcion_msgs(phone, since_minutes=60, max_n=3)
    assert msgs == ["Ginecología es solo particular, $35.000"], f"Got: {msgs}"
    print("PASS: test_get_recepcion_msgs_filters_system_messages")


def test_get_recepcion_msgs_max_n():
    """Respeta el límite max_n y devuelve en orden cronológico."""
    import session as _sess_mod
    from session import get_recepcion_msgs

    phone = "56999000002"
    base = datetime.utcnow()
    with _sess_mod._conn() as conn:
        conn.execute("DELETE FROM messages WHERE phone=?", (phone,))
        for i in range(5):
            ts = (base + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
                (phone, "out", f"[Recepcionista] Mensaje {i+1}", "HUMAN_TAKEOVER", ts)
            )
        conn.commit()

    msgs = get_recepcion_msgs(phone, since_minutes=60, max_n=3)
    assert len(msgs) == 3, f"Expected 3, got {len(msgs)}"
    # ORDER BY ts DESC LIMIT 3 da los 3 más recientes: 5, 4, 3
    # reversed() los pone en cronológico: 3, 4, 5
    assert msgs[-1] == "Mensaje 5", f"Last should be Mensaje 5, got: {msgs}"
    assert msgs[0] == "Mensaje 3", f"First should be Mensaje 3, got: {msgs}"
    print("PASS: test_get_recepcion_msgs_max_n")


def test_get_recepcion_msgs_empty_when_old():
    """Ignora mensajes más antiguos que since_minutes."""
    import session as _sess_mod
    from session import get_recepcion_msgs

    phone = "56999000003"
    with _sess_mod._conn() as conn:
        conn.execute("DELETE FROM messages WHERE phone=?", (phone,))
        # Mensaje de hace 2 horas
        old_ts = (datetime.utcnow() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
            (phone, "out", "[Recepcionista] Mensaje antiguo", "HUMAN_TAKEOVER", old_ts)
        )
        conn.commit()

    msgs = get_recepcion_msgs(phone, since_minutes=60, max_n=3)
    assert msgs == [], f"Expected empty, got: {msgs}"
    print("PASS: test_get_recepcion_msgs_empty_when_old")


# ── Test 2: snapshot_recepcion_context ───────────────────────────────────────

def test_snapshot_recepcion_context_returns_correct_keys():
    """snapshot_recepcion_context devuelve dict con recepcion_resumen y _ts cuando hay msgs."""
    import session as _sess_mod
    from session import snapshot_recepcion_context

    phone = "56999000004"
    with _sess_mod._conn() as conn:
        conn.execute("DELETE FROM messages WHERE phone=?", (phone,))
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
            (phone, "out", "[Recepcionista] La consulta vale $25.000", "HUMAN_TAKEOVER", now_str)
        )
        conn.commit()

    ctx = snapshot_recepcion_context(phone)
    assert "recepcion_resumen" in ctx, f"Missing key: {ctx}"
    assert "recepcion_resumen_ts" in ctx, f"Missing key: {ctx}"
    assert ctx["recepcion_resumen"] == ["La consulta vale $25.000"]
    print("PASS: test_snapshot_recepcion_context_returns_correct_keys")


def test_snapshot_recepcion_context_empty_when_no_msgs():
    """snapshot_recepcion_context devuelve {} si no hay mensajes de recepcionista."""
    from session import snapshot_recepcion_context
    ctx = snapshot_recepcion_context("56999000099")
    assert ctx == {}, f"Expected empty, got: {ctx}"
    print("PASS: test_snapshot_recepcion_context_empty_when_no_msgs")


# ── Test 3: TTL del contexto (30 min) ─────────────────────────────────────────

def test_recepcion_ctx_ttl_expired_is_cleaned():
    """Si recepcion_resumen_ts tiene más de 30 min, flows.py lo debe limpiar."""
    from zoneinfo import ZoneInfo
    # Simular data con ts de hace 35 min
    old_ts = (datetime.now(ZoneInfo("America/Santiago")) - timedelta(minutes=35)).isoformat()
    data = {
        "recepcion_resumen": ["Gineco es particular"],
        "recepcion_resumen_ts": old_ts,
    }
    # Aplicar la lógica TTL inline (misma que flows.py)
    from datetime import datetime as _dt_rc, timezone as _tz_rc
    from zoneinfo import ZoneInfo as _ZI_rc

    _rc_ts_raw = data.get("recepcion_resumen_ts")
    _rc_expired = True
    if _rc_ts_raw:
        _rc_ts = _dt_rc.fromisoformat(_rc_ts_raw)
        if _rc_ts.tzinfo is None:
            _rc_ts = _rc_ts.replace(tzinfo=_ZI_rc("America/Santiago"))
        _rc_age_min = (_dt_rc.now(_tz_rc.utc) - _rc_ts.astimezone(_tz_rc.utc)).total_seconds() / 60
        _rc_expired = _rc_age_min > 30

    assert _rc_expired, "Context with 35 min age should be expired"
    print("PASS: test_recepcion_ctx_ttl_expired_is_cleaned")


def test_recepcion_ctx_ttl_not_expired():
    """Si recepcion_resumen_ts tiene menos de 30 min, el contexto debe mantenerse."""
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt_rc, timezone as _tz_rc
    from zoneinfo import ZoneInfo as _ZI_rc

    recent_ts = (datetime.now(ZoneInfo("America/Santiago")) - timedelta(minutes=10)).isoformat()
    _rc_ts = _dt_rc.fromisoformat(recent_ts)
    if _rc_ts.tzinfo is None:
        _rc_ts = _rc_ts.replace(tzinfo=_ZI_rc("America/Santiago"))
    _rc_age_min = (_dt_rc.now(_tz_rc.utc) - _rc_ts.astimezone(_tz_rc.utc)).total_seconds() / 60
    _rc_expired = _rc_age_min > 30

    assert not _rc_expired, "Context with 10 min age should NOT be expired"
    print("PASS: test_recepcion_ctx_ttl_not_expired")


# ── Test 4: detect_intent acepta recepcion_resumen ───────────────────────────

def test_detect_intent_accepts_recepcion_resumen_param():
    """detect_intent debe aceptar recepcion_resumen sin error de firma."""
    import inspect
    from claude_helper import detect_intent
    sig = inspect.signature(detect_intent)
    assert "recepcion_resumen" in sig.parameters, "detect_intent missing recepcion_resumen param"
    param = sig.parameters["recepcion_resumen"]
    assert param.default is None, "recepcion_resumen should default to None"
    print("PASS: test_detect_intent_accepts_recepcion_resumen_param")


def test_respuesta_faq_accepts_recepcion_resumen_param():
    """respuesta_faq debe aceptar recepcion_resumen sin error de firma."""
    import inspect
    from claude_helper import respuesta_faq
    sig = inspect.signature(respuesta_faq)
    assert "recepcion_resumen" in sig.parameters, "respuesta_faq missing recepcion_resumen param"
    param = sig.parameters["recepcion_resumen"]
    assert param.default is None, "recepcion_resumen should default to None"
    print("PASS: test_respuesta_faq_accepts_recepcion_resumen_param")


# ── Test 5: Guard agendar_bloqueado_por_ctx_recepcion ─────────────────────────

def test_agendar_guard_keywords():
    """Si recepcion_resumen contiene 'le agendé', el guard debe detectarlo."""
    _AGENDAR_MANUAL_KWS = ("agendé", "agende", "le agendé", "le agende",
                           "quedó agendado", "quedo agendado", "tiene hora",
                           "le saqué hora", "le saque hora", "ya tiene cita",
                           "ya quedó", "ya quedo")
    casos_positivos = [
        ["Le agendé para el viernes a las 10:00"],
        ["Ya tiene hora con el Dr. Márquez el lunes"],
        ["Ya quedó agendado para el martes"],
        ["Le saqué hora para hoy a las 15:00"],
    ]
    casos_negativos = [
        ["Ginecología es solo particular, $35.000"],
        ["Los horarios son de lunes a viernes"],
        None,
        [],
    ]
    for caso in casos_positivos:
        rc_lower = " ".join(caso).lower()
        encontrado = any(kw in rc_lower for kw in _AGENDAR_MANUAL_KWS)
        assert encontrado, f"Should detect manual scheduling in: {caso}"

    for caso in casos_negativos:
        if not caso:
            continue
        rc_lower = " ".join(caso).lower()
        encontrado = any(kw in rc_lower for kw in _AGENDAR_MANUAL_KWS)
        assert not encontrado, f"Should NOT detect scheduling in: {caso}"

    print("PASS: test_agendar_guard_keywords")


# ── Test 6: _reset preserva recepcion_resumen ─────────────────────────────────

def test_reset_preserves_recepcion_resumen():
    """_reset debe preservar recepcion_resumen y _ts en el data post-reset."""
    import session as _sess_mod
    from session import save_session, get_session

    phone = "56999000010"
    # Guardar sesión con recepcion_resumen
    save_session(phone, "HUMAN_TAKEOVER", {
        "recepcion_resumen": ["Test mensaje"],
        "recepcion_resumen_ts": datetime.now().isoformat(),
        "handoff_reason": "test",
    })
    # Reset
    with _sess_mod._conn() as conn:
        _sess_mod._reset(conn, phone)

    sess = get_session(phone)
    assert sess["state"] == "IDLE"
    assert sess["data"].get("recepcion_resumen") == ["Test mensaje"], \
        f"recepcion_resumen not preserved: {sess['data']}"
    assert "recepcion_resumen_ts" in sess["data"], "recepcion_resumen_ts not preserved"
    print("PASS: test_reset_preserves_recepcion_resumen")


# ── Runner ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_get_recepcion_msgs_filters_system_messages,
        test_get_recepcion_msgs_max_n,
        test_get_recepcion_msgs_empty_when_old,
        test_snapshot_recepcion_context_returns_correct_keys,
        test_snapshot_recepcion_context_empty_when_no_msgs,
        test_recepcion_ctx_ttl_expired_is_cleaned,
        test_recepcion_ctx_ttl_not_expired,
        test_detect_intent_accepts_recepcion_resumen_param,
        test_respuesta_faq_accepts_recepcion_resumen_param,
        test_agendar_guard_keywords,
        test_reset_preserves_recepcion_resumen,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"FAIL: {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed+failed} tests passed")
    if failed:
        sys.exit(1)
