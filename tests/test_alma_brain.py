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
    bajo = policy.OfferContext(paciente_conocido=True, rut_valido=True,
                               esp_coincide=True, horas_hasta=48, desde_waitlist=True)
    auto, _ = policy.should_auto_confirm(bajo)
    _ok(auto, "paciente conocido + esp coincide + >24h → auto-confirma")

    riesgo = policy.OfferContext(paciente_conocido=False, rut_valido=True,
                                 esp_coincide=True, horas_hasta=48, desde_waitlist=True)
    auto2, _ = policy.should_auto_confirm(riesgo)
    _ok(not auto2, "paciente desconocido → recepción")

    apurado = policy.OfferContext(paciente_conocido=True, rut_valido=True,
                                  esp_coincide=True, horas_hasta=3, desde_waitlist=True)
    auto3, _ = policy.should_auto_confirm(apurado)
    _ok(not auto3, "cupo a menos de 24h → recepción")


def test_autoconfirm_margen_por_especialidad_escasa():
    """Especialista escaso (cardio/gastro) exige >48h; el resto >24h."""
    os.environ["ALMA_OPERATIVA_AUTOCONFIRM"] = "true"
    base = dict(paciente_conocido=True, rut_valido=True, esp_coincide=True, desde_waitlist=True)

    # 30h de margen: alcanza para una general, NO para cardiología.
    general = policy.OfferContext(horas_hasta=30, especialidad="medicina general", **base)
    cardio  = policy.OfferContext(horas_hasta=30, especialidad="cardiología", **base)
    _ok(policy.should_auto_confirm(general)[0], "general a 30h → auto (margen 24h)")
    _ok(not policy.should_auto_confirm(cardio)[0], "cardiología a 30h → recepción (exige 48h)")

    # 50h: cardiología ya pasa el margen ampliado.
    cardio_ok = policy.OfferContext(horas_hasta=50, especialidad="cardiología", **base)
    _ok(policy.should_auto_confirm(cardio_ok)[0], "cardiología a 50h → auto (supera 48h)")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests OK")
