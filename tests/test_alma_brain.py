"""Tests offline del cerebro de Alma — sin API ni BD.

Fijan el contrato de los límites duros (policy) y de la regla pura de
auto-confirmación. Corren sin red: `python3 tests/test_alma_brain.py`.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from alma_brain import policy  # noqa: E402


def _ok(cond, msg):
    print(("OK  " if cond else "FALLO ") + msg)
    assert cond, msg


def test_execute_disabled_blocks_everything():
    os.environ.pop("ALMA_BRAIN_EXECUTE", None)
    lim = policy.HardLimits.from_env()
    ok, _ = policy.check("ads_dryrun_refresh", {}, lim)
    _ok(not ok, "con EXECUTE=false ninguna acción se ejecuta")


def test_contacto_masivo_exige_consent():
    os.environ["ALMA_BRAIN_EXECUTE"] = "true"
    os.environ["ALMA_BRAIN_REQUIRE_CONSENT"] = "true"
    lim = policy.HardLimits.from_env()
    ok, reason = policy.check("winback_blast", {"n_destinatarios": 10}, lim)
    _ok(not ok and "consent" in reason.lower(), "blast masivo bloqueado por Ley 21.719")


def test_medilink_writes_bloqueadas_por_defecto():
    os.environ["ALMA_BRAIN_EXECUTE"] = "true"
    os.environ.pop("ALMA_BRAIN_ALLOW_MEDILINK_WRITES", None)
    lim = policy.HardLimits.from_env()
    ok, _ = policy.check("agendar_cita", {}, lim)
    _ok(not ok, "escrituras a Medilink bloqueadas salvo flag explícito")


def test_ads_step_tope():
    os.environ["ALMA_BRAIN_EXECUTE"] = "true"
    os.environ["ALMA_BRAIN_ADS_MAX_STEP"] = "0.20"
    lim = policy.HardLimits.from_env()
    ok, _ = policy.check("ads_budget_change", {"step_pct": 0.50}, lim)
    _ok(not ok, "paso de presupuesto sobre el tope se rechaza")
    ok2, _ = policy.check("ads_budget_change", {"step_pct": 0.10}, lim)
    _ok(ok2, "paso dentro del tope pasa")


def test_autoconfirm_off_por_defecto():
    os.environ.pop("ALMA_OPERATIVA_AUTOCONFIRM", None)
    ctx = policy.OfferContext(paciente_conocido=True, rut_valido=True,
                              esp_coincide=True, horas_hasta=48, desde_waitlist=True)
    auto, motivo = policy.should_auto_confirm(ctx)
    _ok(not auto and "desactivada" in motivo, "sin flag, todo cupo va a recepción")


def test_autoconfirm_bajo_riesgo():
    os.environ["ALMA_OPERATIVA_AUTOCONFIRM"] = "true"
    base = dict(rut_valido=True, esp_coincide=True, desde_waitlist=True,
                especialidad="medicina general")
    bajo = policy.OfferContext(paciente_conocido=True, horas_hasta=3, **base)
    _ok(policy.should_auto_confirm(bajo)[0], "MG, paciente conocido, cupo a 3h → auto")

    riesgo = policy.OfferContext(paciente_conocido=False, horas_hasta=3, **base)
    _ok(not policy.should_auto_confirm(riesgo)[0], "paciente desconocido → recepción")

    apurado = policy.OfferContext(paciente_conocido=True, horas_hasta=0.5, **base)
    _ok(not policy.should_auto_confirm(apurado)[0], "MG a <1h → recepción")

    sin_wl = policy.OfferContext(paciente_conocido=True, rut_valido=True, esp_coincide=True,
                                 desde_waitlist=False, horas_hasta=3, especialidad="medicina general")
    _ok(not policy.should_auto_confirm(sin_wl)[0], "sin inscripción en lista de espera → recepción")


def test_autoconfirm_solo_mg_y_familiar():
    """Decisión 11-oct-2026: solo MG / Medicina Familiar se auto-confirma (margen 1 h)."""
    os.environ["ALMA_OPERATIVA_AUTOCONFIRM"] = "true"
    base = dict(paciente_conocido=True, rut_valido=True, esp_coincide=True, desde_waitlist=True)
    for esp in ("medicina general", "Medicina Familiar", "médico general"):
        ctx = policy.OfferContext(horas_hasta=2, especialidad=esp, **base)
        _ok(policy.should_auto_confirm(ctx)[0], f"{esp} a 2h → auto")
    for esp in ("cardiología", "kinesiología", "odontología general", "ecografía", ""):
        ctx = policy.OfferContext(horas_hasta=100, especialidad=esp, **base)
        _ok(not policy.should_auto_confirm(ctx)[0], f"{esp or 'sin esp'} → recepción aunque falten 100h")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests OK")
