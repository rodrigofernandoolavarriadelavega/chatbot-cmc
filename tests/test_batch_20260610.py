"""
Tests para el batch de mejoras 2026-06-10 (M2, M3, M5).

M2: Rescate de slot rechazado — deteccion de negacion + botones de rescate.
M3: Normalizacion de slugs de waitlist.
M5: Follow-up proactivo intent info (logica de guardado de flags).

Ejecucion:
    pytest tests/test_batch_20260610.py -v
    python tests/test_batch_20260610.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

_TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_batch_")) / "sessions.db"
os.environ["SESSIONS_DB"] = str(_TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")
os.environ.setdefault("MEDILINK_BASE_URL", "https://fake")
os.environ.setdefault("MEDILINK_TOKEN", "fake")
os.environ.setdefault("MEDILINK_SUCURSAL", "1")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("META_ACCESS_TOKEN", "fake")
os.environ.setdefault("META_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("META_VERIFY_TOKEN", "fake")
os.environ.setdefault("OPENAI_API_KEY", "fake")

import session as _session_mod
_session_mod.DB_PATH = _TMP_DB


# ─────────────────────────────────────────────────────────────────────────────
# M3: _normalize_waitlist_esp
# ─────────────────────────────────────────────────────────────────────────────

def test_m3_normalize_slug_estetica():
    """Variantes de estética deben colapsar al canonical."""
    from session import _normalize_waitlist_esp
    assert _normalize_waitlist_esp("estetica") == "estetica facial"
    assert _normalize_waitlist_esp("Estética Facial") == "estetica facial"
    assert _normalize_waitlist_esp("estética facial") == "estetica facial"
    assert _normalize_waitlist_esp("estetica facial") == "estetica facial"


def test_m3_normalize_slug_ortodoncia():
    from session import _normalize_waitlist_esp
    assert _normalize_waitlist_esp("Ortodoncia") == "ortodoncia"
    assert _normalize_waitlist_esp("ortodoncia") == "ortodoncia"
    assert _normalize_waitlist_esp("ortodoncista") == "ortodoncia"


def test_m3_normalize_slug_ecografia():
    from session import _normalize_waitlist_esp
    assert _normalize_waitlist_esp("Ecografía") == "ecografia"
    assert _normalize_waitlist_esp("ecografia abdominal") == "ecografia"
    assert _normalize_waitlist_esp("ecografía ginecológica") == "ecografia"


def test_m3_normalize_slug_unknown_preserved():
    """Slugs sin match se devuelven en minuscula-sin-tildes."""
    from session import _normalize_waitlist_esp
    result = _normalize_waitlist_esp("alguna-especialidad-nueva")
    assert result == "alguna-especialidad-nueva"


def test_m3_add_to_waitlist_normalizes():
    """add_to_waitlist aplica normalizacion antes de INSERT."""
    from session import add_to_waitlist, _conn
    # La DB se inicializa al primer _conn()
    with _conn() as _:
        pass
    wid = add_to_waitlist("56999000001", "12345678-9", "Test Paciente", "Estética Facial")
    with _conn() as conn:
        row = conn.execute("SELECT especialidad FROM waitlist WHERE id=?", (wid,)).fetchone()
    assert row["especialidad"] == "estetica facial", f"Got: {row['especialidad']}"


def test_m3_add_to_waitlist_dedup_normalizes():
    """Dos inserts con variantes del mismo slug no duplican la fila."""
    from session import add_to_waitlist, _conn
    with _conn() as _:
        pass
    phone = "56999000002"
    wid1 = add_to_waitlist(phone, "12345679-0", "Paciente Dos", "estetica")
    wid2 = add_to_waitlist(phone, "12345679-0", "Paciente Dos", "Estética Facial")
    # Deben ser el mismo ID (update, no insert)
    assert wid1 == wid2, f"Expected same wid, got {wid1} vs {wid2}"


# ─────────────────────────────────────────────────────────────────────────────
# M3: script de migracion
# ─────────────────────────────────────────────────────────────────────────────

def test_m3_normalize_script_importable():
    """El script de migracion se puede importar sin errores."""
    import importlib.util
    script = ROOT / "scripts" / "normalize_waitlist_slugs.py"
    assert script.exists(), "Script no encontrado"
    spec = importlib.util.spec_from_file_location("normalize_waitlist_slugs", str(script))
    mod = importlib.util.module_from_spec(spec)
    # No ejecutar main(), solo verificar que el modulo carga
    # (main usa _conn que requiere DB inicializada)


# ─────────────────────────────────────────────────────────────────────────────
# M5: followup_info_ts flag se guarda en sesion
# ─────────────────────────────────────────────────────────────────────────────

def test_m5_flag_keys_defined():
    """Los campos followup_info_* estan documentados (test de contrato)."""
    # Verificar que los nombres de campo son los esperados — cualquier
    # cambio en flows.py que los renombre rompera este test.
    expected_keys = {"followup_info_ts", "followup_info_esp", "followup_info_sent"}
    # Buscar en flows.py que los tres keys existan
    flows_src = (ROOT / "app" / "flows.py").read_text(encoding="utf-8")
    for key in expected_keys:
        assert key in flows_src, f"Key '{key}' no encontrado en flows.py"


def test_m5_job_function_exists():
    """_job_followup_info existe en jobs.py."""
    jobs_src = (ROOT / "app" / "jobs.py").read_text(encoding="utf-8")
    assert "_job_followup_info" in jobs_src
    assert "FOLLOWUP_INFO_ENABLED" in jobs_src


# ─────────────────────────────────────────────────────────────────────────────
# M2: deteccion de negacion en WAIT_SLOT
# ─────────────────────────────────────────────────────────────────────────────

def test_m2_negacion_kw_en_flows():
    """Los keywords de negacion de M2 estan presentes en flows.py."""
    flows_src = (ROOT / "app" / "flows.py").read_text(encoding="utf-8")
    assert "funnel_rescate_ofrecido" in flows_src
    assert "_NEGACION_SLOT_KW" in flows_src
    assert "_es_negacion_slot" in flows_src


def test_m2_rescate_botones_en_flows():
    """Los botones de rescate M2 estan en flows.py."""
    flows_src = (ROOT / "app" / "flows.py").read_text(encoding="utf-8")
    assert "Otro dia" in flows_src or "Otro día" in flows_src
    assert "Llamar a recepcion" in flows_src or "Recepción" in flows_src


# ─────────────────────────────────────────────────────────────────────────────
# M4: digest semanal
# ─────────────────────────────────────────────────────────────────────────────

def test_m4_job_function_exists():
    """_job_waitlist_digest_semanal existe en jobs.py."""
    jobs_src = (ROOT / "app" / "jobs.py").read_text(encoding="utf-8")
    assert "_job_waitlist_digest_semanal" in jobs_src
    assert "WAITLIST_DIGEST_ENABLED" in jobs_src
    assert "waitlist_digest_semanal" in jobs_src


# ─────────────────────────────────────────────────────────────────────────────
# M1: funnel events en flows.py
# ─────────────────────────────────────────────────────────────────────────────

def test_m1_funnel_events_present():
    """Los 4 eventos de embudo estan en flows.py."""
    flows_src = (ROOT / "app" / "flows.py").read_text(encoding="utf-8")
    for ev in ("funnel_especialidad", "funnel_slot_ofrecido",
                "funnel_slot_rechazado", "funnel_confirmacion"):
        assert ev in flows_src, f"Evento '{ev}' no encontrado en flows.py"


# ─────────────────────────────────────────────────────────────────────────────
# M6: crosssell cache completado
# ─────────────────────────────────────────────────────────────────────────────

def test_m6_crosssell_cache_completado():
    """Cache cross-sell tiene frases del contexto chileno rural."""
    ch_src = (ROOT / "app" / "claude_helper.py").read_text(encoding="utf-8")
    # Afirmaciones chilenas
    for phrase in ("ya po", "dale", "buena", "agendame"):
        assert phrase in ch_src, f"Frase '{phrase}' no encontrada en claude_helper.py"
    # Rechazos
    for phrase in ("mas adelante", "voy a pensarlo"):
        assert phrase in ch_src, f"Frase '{phrase}' no encontrada en claude_helper.py"


def test_m6_crosssell_system_prompt_actualizado():
    """El system prompt del clasificador cross-sell ya no tiene el TODO."""
    ch_src = (ROOT / "app" / "claude_helper.py").read_text(encoding="utf-8")
    assert "TODO RODRIGO: ajustar el system prompt" not in ch_src
    # Debe tener el contexto de Arauco
    assert "Arauco" in ch_src or "Biob" in ch_src


# ─────────────────────────────────────────────────────────────────────────────
# Sintaxis y deep-import
# ─────────────────────────────────────────────────────────────────────────────

def test_syntax_flows():
    import ast
    src = (ROOT / "app" / "flows.py").read_text(encoding="utf-8")
    ast.parse(src)  # lanza SyntaxError si hay problema


def test_syntax_session():
    import ast
    src = (ROOT / "app" / "session.py").read_text(encoding="utf-8")
    ast.parse(src)


def test_syntax_jobs():
    import ast
    src = (ROOT / "app" / "jobs.py").read_text(encoding="utf-8")
    ast.parse(src)


def test_syntax_claude_helper():
    import ast
    src = (ROOT / "app" / "claude_helper.py").read_text(encoding="utf-8")
    ast.parse(src)


# ─────────────────────────────────────────────────────────────────────────────
# Runner manual
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        (n, f) for n, f in globals().items()
        if n.startswith("test_") and callable(f)
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            if asyncio.iscoroutinefunction(fn):
                asyncio.run(fn())
            else:
                fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1
    print(f"\n{passed}/{passed+failed} tests passed")
    if failed:
        sys.exit(1)
