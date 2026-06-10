"""Tests para Items 21, 29 y 30 — deploy 2026-06-10.

Item 21: Separación de finalidad en privacy_consents (Ley 21.719 Art. 9).
Item 29: WAIT_ESPECIALIDAD intercepta preguntas de estado antes del normalizador.
Item 30: Paquete UX flows.py (consent msg, dead-ends, IDs semánticos, post-cancel,
         QR CTA, Bienvenido neutro, _msg_medilink_transient).

Ejecución:
    pytest tests/test_items_21_29_30.py -v
    python tests/test_items_21_29_30.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

_TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_items_")) / "sessions.db"
os.environ["SESSIONS_DB"] = str(_TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")
os.environ.setdefault("MEDILINK_TOKEN", "fake")
os.environ.setdefault("META_ACCESS_TOKEN", "fake")
os.environ.setdefault("META_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")

import session as _session_mod
_session_mod.DB_PATH = _TMP_DB

# ── helpers ───────────────────────────────────────────────────────────────────

def _init_db():
    """Trigger DDL by calling _conn (runs DDL on first use per DB_PATH)."""
    import session as s
    # _conn() triggers _run_ddl_inline on first call per path
    with s._conn() as _:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# ITEM 21: privacy_consents purpose separation
# ══════════════════════════════════════════════════════════════════════════════

def test_21_save_and_get_marketing_consent():
    """save_privacy_consent con purpose='marketing' (default) y recuperación."""
    _init_db()
    from session import save_privacy_consent, has_privacy_consent, get_privacy_consent
    phone = "56911111101"
    save_privacy_consent(phone, "accepted", method="whatsapp")
    assert has_privacy_consent(phone, purpose="marketing") is True
    row = get_privacy_consent(phone, purpose="marketing")
    assert row is not None
    assert row["status"] == "accepted"


def test_21_has_consent_default_is_marketing():
    """has_privacy_consent sin argumento mantiene comportamiento previo (marketing)."""
    _init_db()
    from session import save_privacy_consent, has_privacy_consent
    phone = "56911111102"
    # sin consent → False
    assert has_privacy_consent(phone) is False
    save_privacy_consent(phone, "accepted")
    # con consent → True
    assert has_privacy_consent(phone) is True


def test_21_assistential_always_true():
    """purpose='assistential' retorna True sin importar si hay consent de marketing."""
    _init_db()
    from session import has_privacy_consent
    phone_sin_consent = "56911111103"
    # Sin ningun consent — flujo asistencial debe pasar igual
    assert has_privacy_consent(phone_sin_consent, purpose="assistential") is True


def test_21_assistential_true_even_when_marketing_declined():
    """Paciente que rechazó marketing igual puede recibir recordatorios."""
    _init_db()
    from session import save_privacy_consent, has_privacy_consent
    phone = "56911111104"
    save_privacy_consent(phone, "declined")
    # marketing → False
    assert has_privacy_consent(phone, purpose="marketing") is False
    # assistential → True (base contractual)
    assert has_privacy_consent(phone, purpose="assistential") is True


def test_21_revoke_only_revokes_marketing():
    """revoke_privacy_consent no afecta flujos asistenciales."""
    _init_db()
    from session import save_privacy_consent, has_privacy_consent, revoke_privacy_consent
    phone = "56911111105"
    save_privacy_consent(phone, "accepted")
    assert has_privacy_consent(phone, purpose="marketing") is True
    revoke_privacy_consent(phone, purpose="marketing")
    assert has_privacy_consent(phone, purpose="marketing") is False
    # assistential sigue True
    assert has_privacy_consent(phone, purpose="assistential") is True


def test_21_ddl_idempotent():
    """DDL puede correrse múltiples veces sin error (ALTER TABLE idempotente)."""
    import session as s
    # Resetear flag para forzar re-ejecución del DDL
    s._DDL_DONE = False
    with s._conn() as _:
        pass
    s._DDL_DONE = False
    with s._conn() as _:
        pass  # segunda vez — no debe lanzar excepción


def test_21_purpose_column_exists():
    """La columna purpose existe en la tabla privacy_consents."""
    _init_db()
    import sqlite3
    con = sqlite3.connect(str(_TMP_DB))
    cols = [r[1] for r in con.execute("PRAGMA table_info(privacy_consents)").fetchall()]
    con.close()
    assert "purpose" in cols, f"purpose column missing. Columns: {cols}"


# ══════════════════════════════════════════════════════════════════════════════
# ITEM 29: WAIT_ESPECIALIDAD — preguntas de estado interceptadas
# ══════════════════════════════════════════════════════════════════════════════

def test_29_regex_patterns():
    """El regex de estado cubre los patrones reportados (13 casos/sem)."""
    import re as _re
    _RE = _re.compile(
        r"qued[oó]\s*(agend|confirm|reserv|list)"
        r"|confirm[oa]d[ao]"
        r"|mi\s+reserva"
        r"|mi\s+cit[ao]"
        r"|mi\s+hora"
        r"|ya\s+est[aá]\s*(agend|confirm|list|reserv)"
        r"|est[aá]\s+list[ao]"
        r"|est[aá]\s+confirm"
        r"|\bse\s+agend[oó]\b"
        r"|\bse\s+confirm[oó]\b"
        r"|\bquiero\s+(ver|saber)\s+(mis?\s+)?(cit[ao]|reserva|hora)"
        r"|\bver\s+mis?\s+(cit[ao]|reserva|hora)",
        _re.IGNORECASE,
    )
    positivos = [
        "¿quedó agendado?",
        "ya está confirmada mi reserva",
        "¿está lista mi cita?",
        "¿se confirmó mi hora?",
        "mi reserva está confirmada",
        "quiero ver mis citas",
        "ver mi hora",
        "mi hora quedó reservada?",
        "confirmada",
        "se agendó?",
    ]
    for p in positivos:
        assert _RE.search(p), f"Expected match for: {p!r}"

    negativos = [
        "medicina general",
        "kine",
        "psicología",
        "hola",
        "quiero agendar",
        "1",
    ]
    for n in negativos:
        assert not _RE.search(n), f"Expected NO match for: {n!r}"


def test_29_status_query_present_in_flows():
    """El guard de status query está en el código de WAIT_ESPECIALIDAD."""
    flows_src = (ROOT / "app" / "flows.py").read_text()
    assert "ITEM-29" in flows_src, "ITEM-29 guard not found in flows.py"
    assert "_RE_STATUS_Q" in flows_src, "_RE_STATUS_Q not defined"
    assert "_iniciar_ver(phone, data, txt=txt)" in flows_src, "redirect to _iniciar_ver missing"


# ══════════════════════════════════════════════════════════════════════════════
# ITEM 30: UX fixes en flows.py
# ══════════════════════════════════════════════════════════════════════════════

def test_30a_consent_msg_contains_opt_out_instruction():
    """Todos los mensajes de consent aceptado explican cómo salir."""
    flows_src = (ROOT / "app" / "flows.py").read_text()
    assert "no quiero mensajes" in flows_src, "opt-out instruction missing from consent msg"
    assert "quedaste registrado" in flows_src, "confirmation text missing"


def test_30a_no_old_consent_msg():
    """El mensaje viejo vacío 'Listo, queda registrado. Pronto recibirás...' no existe."""
    flows_src = (ROOT / "app" / "flows.py").read_text()
    assert "Pronto recibirás recordatorios de salud." not in flows_src, \
        "Old empty consent msg still present"


def test_30b_dead_ends_use_btn_msg():
    """Los dead-ends sin citas usan _btn_msg (no return simple)."""
    flows_src = (ROOT / "app" / "flows.py").read_text()
    # Verify dead-end messages are wrapped in _btn_msg
    # Pattern: _btn_msg( right before or near "No tienes citas" / "No encontré citas"
    # Count occurrences of "No encontré citas futuras" — all should be inside _btn_msg
    dead_end_raw_pattern = re.compile(
        r'return\s*\(\s*\n\s*f?"No (tienes|encontré) citas futuras'
    )
    matches = dead_end_raw_pattern.findall(flows_src)
    assert len(matches) == 0, \
        f"Found raw return dead-ends without _btn_msg: {matches}"


def test_30c_semantic_ids_in_submenu():
    """El sub-menú accion_cambiar usa IDs semánticos accion_reagendar/accion_cancelar."""
    flows_src = (ROOT / "app" / "flows.py").read_text()
    assert '"accion_reagendar"' in flows_src, "accion_reagendar ID missing"
    assert '"accion_cancelar"' in flows_src, "accion_cancelar ID missing"


def test_30c_backward_compat_handlers():
    """Los handlers numéricos '2'/'3' siguen existiendo (backward compat)."""
    flows_src = (ROOT / "app" / "flows.py").read_text()
    assert 'if txt == "2": return await _iniciar_reagendar' in flows_src
    assert 'if txt == "3": return await _iniciar_cancelar' in flows_src
    assert 'tl == "accion_reagendar"' in flows_src
    assert 'tl == "accion_cancelar"' in flows_src


def test_30d_post_cancel_no_redundant_text():
    """El mensaje post-cancelación no tiene texto redundante '¿Quieres ahora agendar'."""
    flows_src = (ROOT / "app" / "flows.py").read_text()
    assert "Cancelé tu cita. ¿Quieres ahora agendar" not in flows_src, \
        "Redundant post-cancel text still present"
    assert "📅 Agendar nueva hora" in flows_src, "Agendar nueva hora button missing"


def test_30e_qr_reject_has_cta():
    """El rechazo de opt-in QR tiene CTA de agendar."""
    flows_src = (ROOT / "app" / "flows.py").read_text()
    assert "Igual puedo ayudarte a agendar horas" in flows_src, \
        "QR rejection CTA missing"


def test_30f_no_bienvenido_slash():
    """No quedan ocurrencias de 'Bienvenido/a' en flows.py."""
    flows_src = (ROOT / "app" / "flows.py").read_text()
    assert "Bienvenido/a" not in flows_src, "Bienvenido/a still present in flows.py"


def test_30g_transient_no_thinking_emoji():
    """_msg_medilink_transient no usa el emoji 🤔."""
    flows_src = (ROOT / "app" / "flows.py").read_text()
    # Find the function body
    idx = flows_src.find("def _msg_medilink_transient")
    assert idx != -1
    snippet = flows_src[idx:idx+400]
    assert "\U0001f914" not in snippet, "🤔 emoji still in _msg_medilink_transient"
    assert "Una recepcionista te ayudará directamente por acá" in snippet, \
        "New recepcionista phrase missing"


def test_30g_transient_has_phone():
    """_msg_medilink_transient mantiene el teléfono."""
    flows_src = (ROOT / "app" / "flows.py").read_text()
    idx = flows_src.find("def _msg_medilink_transient")
    snippet = flows_src[idx:idx+500]
    assert "CMC_TELEFONO" in snippet, "Phone reference missing from _msg_medilink_transient"


# ── runner ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
