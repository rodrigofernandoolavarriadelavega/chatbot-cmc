"""Cierre suave tras pregunta de precio (6-oct-2026).

Medido en prod (30 d): 322 preguntas de precio, 65% ice-breakers de Meta sin
especialidad; con hora concreta agendó 5%, con texto genérico 1%.
  (a) ice-breaker de consulta/evaluación médica → se asume Medicina General
  (b) precio sin pista → lista de especialidades con valor; al elegir → precio +
      próxima hora + botón `agendar_sugerido` (reusa el camino existente)
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import flows  # noqa: E402


# ── (a)/(b): clasificación de la pregunta sin especialidad ──────────────────
def test_icebreakers_meta_se_asumen_medicina_general():
    for t in ("¿Cuál es el costo de la evaluación médica?",
              "¿Cuál es el costo de la consulta?",
              "Valor de la consulta", "Buenas tardes , necesito saber el valor de la consulta",
              "cuanto cuesta la consulta?"):
        assert flows._clasificar_precio_sin_esp(t) == "mg", t


def test_precio_generico_va_a_lista():
    for t in ("¿Costo de los servicios?", "precios", "cuánto cuesta?", "hola, cuanto cobran?"):
        assert flows._clasificar_precio_sin_esp(t) == "lista", t


def test_precio_con_pista_sigue_camino_de_siempre():
    # especialidad, apellido o contenido concreto → None (FAQ/LLM como antes)
    for t in ("cuanto vale la eco abdominal", "precio consulta con el dr olavarria",
              "cuánto cuesta el examen de orina", "cuanto sale sacar una muela"):
        assert flows._clasificar_precio_sin_esp(t) is None, t


def test_lista_respeta_limites_whatsapp():
    msg = flows._lista_precios_msg()
    inter = msg["interactive"]
    assert inter["type"] == "list"
    rows = [r for s in inter["action"]["sections"] for r in s["rows"]]
    assert 1 <= len(rows) <= 10
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))
    for r in rows:
        assert len(r["title"]) <= 24, r
        assert len(r.get("description", "")) <= 72, r
    assert len(inter["body"]["text"]) <= 1024
    assert len(inter["action"]["button"]) <= 20
    desc_mg = next(r for r in rows if r["id"] == "precio_esp_mg")["description"]
    assert "7.880" in desc_mg and "25.000" in desc_mg


def test_fila_por_id_y_por_titulo_escrito():
    assert flows._fila_lista_precios("precio_esp_kine", {})[3] == "kinesiología"
    reciente = {"precio_lista_ts": datetime.now(timezone.utc).isoformat()}
    # IG/FB: el paciente escribe el título tal cual (con o sin tilde)
    assert flows._fila_lista_precios("Psicología", reciente)[0] == "precio_esp_psico"
    assert flows._fila_lista_precios("psicologia", reciente)[0] == "precio_esp_psico"
    # sin lista reciente, el título suelto NO se interpreta como fila
    assert flows._fila_lista_precios("psicología", {}) is None
    vieja = {"precio_lista_ts": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}
    assert flows._fila_lista_precios("psicología", vieja) is None
    assert flows._fila_lista_precios("hola", reciente) is None


# ── fila elegida → precio + próxima hora + botón agendar_sugerido ───────────
def _slot(prof="Dr. Pablo Abarca", pid=73):
    return {"fecha": "2026-10-08", "fecha_display": "jueves 8 de octubre", "hora_inicio": "09:30:00",
            "hora_fin": "09:45:00", "profesional": prof, "id_profesional": pid,
            "especialidad": "Medicina General"}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_cierre_con_hora_deja_sugerida_y_botones(monkeypatch):
    saved = {}
    monkeypatch.setattr(flows, "save_session", lambda ph, st, d: saved.update({"state": st, "data": dict(d)}))
    monkeypatch.setattr(flows, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(flows, "is_medilink_down", lambda: False)

    async def fake_primer_dia(esp, **kw):
        assert esp == "medicina general" and kw.get("solo_ids") == flows._MED_AO_IDS
        return [], [_slot()]
    monkeypatch.setattr(flows, "buscar_primer_dia", fake_primer_dia)

    data = {"precio_lista_ts": datetime.now(timezone.utc).isoformat()}
    fila = flows._fila_lista_precios("precio_esp_mg", data)
    resp = _run(flows._cierre_precio_con_hora("56900000000", data, fila))
    body = resp["interactive"]["body"]["text"]
    assert "Medicina general" in body and "7.880" in body and "25.000" in body
    assert "jueves 8 de octubre" in body and "09:30" in body and "¿Te la reservo?" in body
    ids = [b["reply"]["id"] for b in resp["interactive"]["action"]["buttons"]]
    assert ids == ["agendar_sugerido", "no_agendar"]
    assert saved["state"] == "IDLE"
    assert saved["data"]["especialidad_sugerida"] == "medicina general"
    assert "precio_lista_ts" not in saved["data"]


def test_cierre_psicologia_con_salas_no_dice_bono(monkeypatch):
    monkeypatch.setattr(flows, "save_session", lambda *a, **k: None)
    monkeypatch.setattr(flows, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(flows, "is_medilink_down", lambda: False)

    async def fake_primer_dia(esp, **kw):
        return [_slot("Ps. Jacquelinne Salas", 82)], []
    monkeypatch.setattr(flows, "buscar_primer_dia", fake_primer_dia)
    fila = flows._fila_lista_precios("precio_esp_psico", {})
    resp = _run(flows._cierre_precio_con_hora("56900000000", {}, fila))
    body = resp["interactive"]["body"]["text"].lower()
    # Salas (82) no emite bono MLE: jamás el $14.420 de Montalba/Rodríguez
    assert "14.420" not in body and "25.000" in body


def test_cierre_sin_slot_ofrece_agendar_igual(monkeypatch):
    monkeypatch.setattr(flows, "save_session", lambda *a, **k: None)
    monkeypatch.setattr(flows, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(flows, "is_medilink_down", lambda: True)   # Medilink caído: sin lookup
    fila = flows._fila_lista_precios("precio_esp_kine", {})
    resp = _run(flows._cierre_precio_con_hora("56900000000", {}, fila))
    body = resp["interactive"]["body"]["text"]
    assert "¿Te agendo en *Kinesiología*?" in body and "7.830" in body


def test_otra_especialidad_pregunta_sin_lista(monkeypatch):
    monkeypatch.setattr(flows, "save_session", lambda *a, **k: None)
    monkeypatch.setattr(flows, "log_event", lambda *a, **k: None)
    fila = flows._fila_lista_precios("precio_esp_otra", {})
    resp = _run(flows._cierre_precio_con_hora("56900000000", {}, fila))
    assert isinstance(resp, str) and "qué especialidad" in resp.lower()
