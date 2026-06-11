"""
Regresión F004 (auditoría 2026-06-10): lookup case-sensitive en PRECIOS_SLOT.

Bug: data["especialidad"] viaja en minúsculas por el funnel de agendamiento
(_iniciar_agendar guarda especialidad_lower, flows.py:13503), pero las claves
de PRECIOS_SLOT son Title Case. _precio_line("kinesiología") devolvía "" y el
bot derivaba a recepción un precio que SÍ está en la tabla ("Para confirmarte
el valor exacto de *kinesiología*, te paso con recepción").

Ejecución:
    venv/bin/python -m pytest tests/test_precio_line_case.py -q
    python tests/test_precio_line_case.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

_TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_precio_case_")) / "sessions.db"
os.environ["SESSIONS_DB"] = str(_TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session as _session_mod
_session_mod.DB_PATH = _TMP_DB

import flows


def test_precio_line_minusculas_kinesiologia():
    # Camino real: pre-router -> _preguntar_precio_respuesta -> _precio_line(data["especialidad"])
    linea = flows._precio_line("kinesiología")
    assert linea, "kinesiología (minúsculas) debe resolver precio de PRECIOS_SLOT"
    assert "7.830" in linea
    assert "Fonasa" in linea


def test_precio_line_minusculas_ginecologia():
    # F034: precio ginecología actualizado de $30.000 → $35.000 (precio real)
    linea = flows._precio_line("ginecología")
    assert linea, "ginecología (minúsculas) debe resolver precio"
    assert "35.000" in linea


def test_precio_line_title_case_sigue_funcionando():
    assert "7.830" in flows._precio_line("Kinesiología")
    # F034: ginecología $35.000 (no $30.000)
    assert "35.000" in flows._precio_line("Ginecología")


def test_precio_line_kine_tiene_precio_particular():
    # F035: kinesiología es "ambas" → particular $20.000 disponible (no "solo Fonasa")
    linea = flows._precio_line("kinesiología", modalidad_override="particular")
    assert "20.000" in linea, "kine particular debe mostrar $20.000"
    assert "solo con valor Fonasa" not in linea, "no debe decir solo Fonasa"


def test_precio_line_marquez_override_intacto():
    # Dr. Márquez (id 13) en MG -> tarifa Medicina Familiar ($7.880 / $30.000).
    linea = flows._precio_line("medicina general", id_profesional=13)
    assert "30.000" in linea


def test_preguntar_precio_respuesta_no_deriva_con_precio_conocido():
    # Reproduce el síntoma end-to-end: especialidad en minúsculas en data
    # (como la guarda _iniciar_agendar) y SIN slot_elegido (WAIT_SLOT antes
    # de elegir hora). Antes del fix derivaba a recepción.
    resp = flows._preguntar_precio_respuesta({"especialidad": "kinesiología"}, txt="¿cuánto vale?")
    assert "te paso con recepción" not in resp
    assert "7.830" in resp


def test_precio_line_especialidad_desconocida_sigue_vacia():
    # No inventar precios: lo que no está en la tabla sigue retornando "".
    assert flows._precio_line("acupuntura") == ""


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
