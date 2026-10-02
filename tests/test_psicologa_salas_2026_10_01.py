"""Ps. Jacquelinne Salas (Medilink 82) — incorporación al bot (2026-10-01).

Verifica OFFLINE (Medilink y Anthropic mockeados; nunca llama a APIs reales):
  1. Está en PROFESIONALES (intervalo 45, presencial: sin telemedicina_dias) y
     en AMBOS pools de psicología (infantil y adulto) de ESPECIALIDADES_MAP.
  2. Un pedido de psicología INFANTIL (y uno genérico con un niño) y uno de
     psicología ADULTO la ofrecen, con precio $20.000 (Fonasa sin bono) /
     $25.000 (particular).
  3. El bot jamás le promete bono Fonasa ni le muestra el bono $14.420 de
     Montalba/Rodríguez para ella; Montalba/Rodríguez conservan su bono.
  4. Los $20.000 / $25.000 pasan el validador de precios del FAQ; su nombre no
     lo borra el scrub de profesionales.

Uso:
    PYTHONPATH=app:. ./venv/bin/python tests/test_psicologa_salas_2026_10_01.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_20261001_")) / "s.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")
import session  # noqa: E402
session.DB_PATH = TMP_DB

import medilink as _ml  # noqa: E402

_F = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
_ids_reales = _ml._ids_para_especialidad   # la tabla REAL de ruteo, sin mockear
LLAMADAS: list[str] = []


def _slots_de(pid: int, hora: str) -> list[dict]:
    prof = _ml.PROFESIONALES[pid]
    h, m = int(hora[:2]), int(hora[3:5])
    fin = h * 60 + m + prof["intervalo"]
    return [{"fecha": _F, "fecha_display": "Miércoles 7 de octubre",
             "hora_inicio": hora + ":00", "hora_fin": f"{fin // 60:02d}:{fin % 60:02d}:00",
             "profesional": prof["nombre"], "especialidad": prof["especialidad"],
             "id_profesional": pid, "id_recurso": 1, "duracion": prof["intervalo"]}]


async def _fake_primer_dia(especialidad, *a, solo_ids=None, **k):
    """Un cupo por profesional del pool REAL de esa especialidad. Salas lo
    tiene más temprano (15:30) para que la oferta sea ella."""
    LLAMADAS.append(especialidad)
    ids = list(solo_ids) if solo_ids else _ids_reales(especialidad)
    horas = {82: "15:30", 74: "18:00", 49: "17:00"}
    todos = []
    for pid in ids:
        todos.extend(_slots_de(pid, horas.get(pid, "16:00")))
    todos.sort(key=lambda s: s["hora_inicio"])
    return todos[:5], todos


_ml.buscar_primer_dia = _fake_primer_dia
_ml.buscar_slots_dia = AsyncMock(side_effect=lambda e, f, **k: _fake_primer_dia(e))
_ml.buscar_paciente = AsyncMock(return_value=None)
_ml.listar_citas_paciente = AsyncMock(return_value=[])
_ml.consultar_proxima_fecha = AsyncMock(return_value=None)
import messaging as _msg  # noqa: E402
_msg.send_whatsapp = AsyncMock(return_value="wamid.TEST")

import flows as flows_mod  # noqa: E402
import claude_helper  # noqa: E402


async def _continue(mensaje, state, session_data):
    return {"action": "continue"}


flows_mod.classify_with_context = _continue
flows_mod.respuesta_faq = AsyncMock(return_value="ok")


def _intent(esp):
    m = AsyncMock(return_value={"intent": "agendar", "especialidad": esp, "respuesta_directa": None})
    flows_mod.detect_intent = m
    claude_helper.detect_intent = m


def _clean():
    LLAMADAS.clear()
    with session.db() as c:
        for t in ("sessions", "citas_bot", "conversation_events"):
            c.execute(f"DELETE FROM {t}")
        c.commit()


def _txt(resp):
    return str(resp) if isinstance(resp, dict) else (resp or "")


def _ids_sesion(phone):
    d = session.get_session(phone).get("data", {})
    return {s.get("id_profesional") for s in (d.get("todos_slots") or d.get("slots") or [])}


class TestRoster(unittest.TestCase):
    def test_profesionales(self):
        p = _ml.PROFESIONALES[82]
        self.assertEqual(p["nombre"], "Ps. Jacquelinne Salas")
        self.assertEqual(p["intervalo"], 45)
        self.assertNotIn("telemedicina", p)
        self.assertNotIn("telemedicina_dias", p)   # presencial; online = excepción vía recepción

    def test_ambos_pools(self):
        for k in ("psicología infantil", "psicólogo infantil", "psicóloga infantil",
                  "psicología adulto", "psicólogo adulto", "psicología", "psicóloga"):
            self.assertIn(82, _ml._ids_para_especialidad(k), k)
        # Montalba/Rodríguez NO se pierden
        self.assertEqual(set(_ml._ids_para_especialidad("psicología adulto")), {74, 49, 82})
        self.assertEqual(set(_ml._ids_para_especialidad("psicología infantil")), {74, 82})
        self.assertEqual(_ml._ids_para_especialidad("jacquelinne salas"), [82])
        self.assertEqual(_ml.ESPECIALIDADES_ID["psicología infantil"], 5)

    def test_apellido_detectado(self):
        for t in ("quiero hora con la psicologa Jacquelinne Salas", "hora con jacquelinne",
                  "con la dra salas"):
            self.assertEqual(flows_mod._detectar_apellido_profesional(t), "jacquelinne salas", t)
        # "salas" suelto NO la dispara (salas de espera)
        self.assertIsNone(flows_mod._detectar_apellido_profesional("hay salas de espera?"))


class TestPrecio(unittest.TestCase):
    SLOT = _slots_de(82, "15:30")[0]

    def test_ambos_precios_sin_bono(self):
        for esp in ("Psicología Infantil", "Psicología Adulto"):
            t = flows_mod._precio_line(esp, dict(self.SLOT, especialidad=esp))
            self.assertIn("$20.000", t)
            self.assertIn("$25.000", t)
            self.assertIn("sin bono", t.lower())
            self.assertNotIn("14.420", t)

    def test_por_id_sin_slot(self):
        t = flows_mod._precio_line("psicología infantil", id_profesional=82)
        self.assertIn("$20.000", t)
        self.assertIn("$25.000", t)

    def test_override_modalidad(self):
        f = flows_mod._precio_line("Psicología Adulto", self.SLOT, modalidad_override="fonasa")
        p = flows_mod._precio_line("Psicología Adulto", self.SLOT, modalidad_override="particular")
        self.assertIn("$20.000", f)
        self.assertNotIn("$25.000", f)
        self.assertIn("sin bono", f.lower())
        self.assertIn("$25.000", p)
        self.assertNotIn("$20.000", p)

    def test_montalba_rodriguez_conservan_bono(self):
        for pid in (74, 49):
            t = flows_mod._precio_line("Psicología Adulto", _slots_de(pid, "18:00")[0])
            self.assertIn("$14.420", t)
            self.assertIn("$20.000", t)
            self.assertNotIn("$25.000", t)

    def test_nunca_promete_bono(self):
        import re
        for mod in (None, "fonasa", "particular"):
            t = flows_mod._precio_line("Psicología Infantil", self.SLOT, modalidad_override=mod)
            for m in re.finditer(r"bono", t.lower()):
                self.assertTrue(t.lower()[max(0, m.start() - 4):m.start()] == "sin ", t)

    def test_web_agendador_label(self):
        import agendador_routes as ar
        self.assertEqual(ar._precio_label("Psicología Adulto", 82),
                         "Fonasa (sin bono) $20.000 · Particular $25.000")
        self.assertIn("14.420", ar._precio_label("Psicología Adulto", 74))
        cat = ar._build_catalogo()
        infantil = [e for g in cat for e in g["especialidades"] if e["especialidad"] == "Psicología Infantil"][0]
        self.assertIn(82, {p["id"] for p in infantil["profesionales"]})
        adulto = [e for g in cat for e in g["especialidades"] if e["especialidad"] == "Psicología Adulto"][0]
        self.assertIn(82, {p["id"] for p in adulto["profesionales"]})


class TestValidadorFAQ(unittest.TestCase):
    def test_precios_en_whitelist(self):
        self.assertIn("$20.000", claude_helper._PRECIOS_CONOCIDOS)
        self.assertIn("$25.000", claude_helper._PRECIOS_CONOCIDOS)
        t = claude_helper._validar_respuesta_faq(
            "Con la psicóloga Jacquelinne: Fonasa $20.000 directo (sin bono) o particular $25.000.")
        self.assertIn("$20.000", t)
        self.assertIn("$25.000", t)
        self.assertNotIn("consultar en recepción", t)

    def test_nombre_no_se_borra(self):
        t = claude_helper._validar_respuesta_faq("Te atiende la psicóloga Salas, presencial.")
        self.assertIn("Salas", t)
        self.assertNotIn("del CMC", t)

    def test_prompt_dice_sin_bono_y_presencial(self):
        sp = claude_helper.SYSTEM_PROMPT
        self.assertIn("Jacquelinne Salas", sp)
        self.assertIn("SIN bono", sp)
        self.assertIn("PRESENCIAL", sp)
        self.assertIn("recepción", sp)


class TestFlujo(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clean()

    async def test_infantil_la_ofrece_con_precio(self):
        _intent("psicología infantil")
        p = "56900001001"
        r = await flows_mod.handle_message(p, "quiero hora de psicologia infantil", {"state": "IDLE", "data": {}})
        t = _txt(r)
        self.assertIn(82, _ids_sesion(p), t)
        self.assertIn("Salas", t)
        self.assertIn("$20.000", t)
        self.assertIn("$25.000", t)
        self.assertNotIn("14.420", t)
        self.assertIn("psicología infantil", [e.lower() for e in LLAMADAS])
        # Montalba también sigue en el pool infantil
        self.assertEqual(_ids_sesion(p) - {82}, {74})

    async def test_nino_con_psicologia_generica_va_a_infantil(self):
        _intent("psicología")
        p = "56900001002"
        r = await flows_mod.handle_message(
            p, "necesito psicologa para mi hijo de 8 años", {"state": "IDLE", "data": {}})
        t = _txt(r)
        self.assertIn(82, _ids_sesion(p), t)
        self.assertNotIn(49, _ids_sesion(p))      # Rodríguez es solo adultos
        self.assertNotIn("atiende principalmente adultos", t)

    async def test_adulto_la_ofrece_con_precio(self):
        _intent("psicología adulto")
        p = "56900001003"
        r = await flows_mod.handle_message(p, "quiero hora con psicologo adulto", {"state": "IDLE", "data": {}})
        t = _txt(r)
        self.assertEqual(_ids_sesion(p), {74, 49, 82})
        self.assertIn("Salas", t)
        self.assertIn("$20.000", t)
        self.assertIn("$25.000", t)
        self.assertNotIn("14.420", t)

    async def test_pregunta_fonasa_en_wait_slot_no_dice_bono(self):
        p = "56900001004"
        slot = _slots_de(82, "15:30")[0]
        slot["especialidad"] = "Psicología Infantil"
        data = {"especialidad": "psicología infantil", "slots": [slot], "todos_slots": [slot],
                "slot_sugerido": slot, "fechas_vistas": [_F]}
        r = await flows_mod.handle_message(p, "atiende con fonasa?", {"state": "WAIT_SLOT", "data": data})
        t = _txt(r).lower()
        self.assertIn("sin bono", t)
        self.assertIn("$20.000", t)
        self.assertNotIn("bono mle", t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
