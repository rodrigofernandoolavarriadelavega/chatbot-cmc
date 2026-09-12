"""Montalba (74): modalidad que depende del DÍA — lun-vie online, sábado presencial.

Cambio del dueño 2026-09-11: Jorge Montalba pasa a lun-vie 18:00-20:30 ONLINE y
sábado 09:00-14:00 PRESENCIAL. Es el primer profesional del CMC cuya modalidad
NO es constante, y el modelo previo (`telemedicina: True/False` por profesional)
no podía expresarlo.

Por qué importa tanto acertar: el paciente ve la modalidad ANTES de confirmar.
  · Decirle "videollamada" a alguien con hora el SÁBADO lo deja en su casa
    esperando un link que no llega — PIERDE la hora.
  · Decirle "presencial" a alguien con hora el LUNES lo manda a la clínica,
    donde recepción todavía puede resolverlo.
Es el mismo agujero del caso Bryan (2026-08-04), que pagó un abono de psiquiatría
sin saber que era teleconsulta.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from medilink import PROFESIONALES


# Réplica exacta de flows._es_teleconsulta, sin arrastrar todo el módulo flows
# (que exige env de WhatsApp/Medilink). Si la lógica de allá cambia, este test
# deja de representarla — por eso se comparan además los datos de PROFESIONALES.
from datetime import date


def _weekday_slot(slot):
    f = (slot.get("fecha") or "").strip()[:10]
    try:
        return date.fromisoformat(f).weekday()
    except (ValueError, TypeError):
        return None


def _es_teleconsulta(slot):
    try:
        pid = int(slot.get("id_profesional") or 0)
    except (TypeError, ValueError):
        pid = 0
    cfg = PROFESIONALES.get(pid, {})
    dias = cfg.get("telemedicina_dias")
    if dias is not None:
        wd = _weekday_slot(slot)
        if wd is None:
            return False
        return wd in dias
    if cfg.get("telemedicina"):
        return True
    esp = (slot.get("especialidad") or "").lower()
    return ("psiquiatr" in esp or "neurolog" in esp
            or "nutriolog" in esp or "diabetolog" in esp)


def _slot(fecha, pid=74, esp="Psicología Adulto"):
    return {"fecha": fecha, "id_profesional": pid, "especialidad": esp}


# 2026-09-14 es LUNES; 2026-09-19 es SÁBADO; 2026-09-20 DOMINGO.
def test_lunes_a_viernes_es_online():
    for f, dia in (("2026-09-14", "lunes"), ("2026-09-15", "martes"),
                   ("2026-09-16", "miércoles"), ("2026-09-17", "jueves"),
                   ("2026-09-18", "viernes")):
        assert _es_teleconsulta(_slot(f)) is True, f"{dia} {f} debería ser online"


def test_sabado_es_presencial():
    """El caso que rompe un booleano plano."""
    assert _es_teleconsulta(_slot("2026-09-19")) is False
    assert _es_teleconsulta(_slot("2026-09-26")) is False


def test_sin_fecha_no_promete_videollamada():
    """Ante la duda, presencial: el error recuperable."""
    assert _es_teleconsulta({"id_profesional": 74, "especialidad": "Psicología Adulto"}) is False
    assert _es_teleconsulta(_slot("basura")) is False


def test_no_rompe_a_los_que_ya_eran_teleconsulta():
    """Psiquiatría (78), Neurología (79) y Paz (81) siguen siendo online SIEMPRE,
    incluido el sábado."""
    for pid, esp in ((78, "Psiquiatría"), (79, "Neurología"),
                     (81, "Nutriología y Diabetología")):
        assert _es_teleconsulta(_slot("2026-09-19", pid, esp)) is True, f"prof {pid}"


def test_el_otro_psicologo_sigue_presencial():
    """Juan Pablo Rodríguez (49) NO cambió: presencial todos los días."""
    for f in ("2026-09-14", "2026-09-19"):
        assert _es_teleconsulta(_slot(f, 49, "Psicología Adulto")) is False


def test_config_de_montalba_es_la_acordada():
    cfg = PROFESIONALES[74]
    assert cfg.get("telemedicina_dias") == [0, 1, 2, 3, 4]
    assert cfg.get("telemedicina") is None, "no debe tener el booleano plano"
    assert cfg["intervalo"] == 45
