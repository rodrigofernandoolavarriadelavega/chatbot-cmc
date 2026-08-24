"""
Regresión — consolidado 2026-08-24, hallazgo #10: parser de hora en
WAIT_SLOT/WAIT_QUICK_BOOK interpreta mal números sueltos y expresiones de
fecha libre.

1. "3" solo (sin ":") en WAIT_SLOT, cuando no hay 3+ slots mostrados para
   interpretarlo como índice de lista, ya NO se busca como "03:00" (el CMC
   no atiende de madrugada) — se pregunta si se refiere a las 15:00 (si hay
   cupos esa hora) con botones de confirmación.
2. "para el 26" ya no se interpreta como hora "26:00" (inválida, 24h no
   llega a 26) — se descarta como intento de hora y cae al flujo normal.
3. "mañana"/"el lunes"/"miércoles 26" en WAIT_QUICK_BOOK ahora se reconocen
   como preferencia de FECHA (reusa `_detectar_fecha_pedida_idle`) en vez de
   perderse en el clasificador genérico de intent.

Uso:
    PYTHONPATH=app:. python tests/test_parser_hora_slot_2026_08_24.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_parser_hora_")) / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session  # noqa: E402
session.DB_PATH = TMP_DB

import medilink as medilink_mod  # noqa: E402
medilink_mod.buscar_primer_dia = AsyncMock(return_value=([], []))
medilink_mod.buscar_slots_dia = AsyncMock(return_value=[])
medilink_mod.buscar_slots_dia_por_ids = AsyncMock(return_value=[])
medilink_mod.buscar_paciente = AsyncMock(return_value=None)
medilink_mod.listar_citas_paciente = AsyncMock(return_value=[])
medilink_mod.consultar_proxima_fecha = AsyncMock(return_value=None)
medilink_mod.verificar_slot_disponible = AsyncMock(return_value=True)
import messaging as messaging_mod  # noqa: E402
messaging_mod.send_whatsapp = AsyncMock(return_value="wamid.TEST")

import claude_helper as claude_helper_mod  # noqa: E402


async def _fake_classify_with_context(mensaje, state, session_data):
    # Igual que harness_50.py: "continue" = deja decidir al handler del
    # estado, sin llamar a la API real de Claude (evita flaky + costo).
    return {"action": "continue"}


claude_helper_mod.classify_with_context = _fake_classify_with_context

import flows as flows_mod  # noqa: E402
flows_mod.classify_with_context = _fake_classify_with_context
flows_mod.buscar_slots_dia_por_ids = medilink_mod.buscar_slots_dia_por_ids
flows_mod.verificar_slot_disponible = medilink_mod.verificar_slot_disponible

_CHILE_TZ = ZoneInfo("America/Santiago")


def _fecha_futura(dias=3):
    return (datetime.now(_CHILE_TZ) + timedelta(days=dias)).strftime("%Y-%m-%d")


def _slot(hora, fecha=None, especialidad="Psicología Adulto",
          profesional="Jorge Montalba", id_profesional=74):
    return {
        "fecha": fecha or _fecha_futura(),
        "fecha_display": fecha or _fecha_futura(),
        "hora_inicio": hora,
        "especialidad": especialidad,
        "profesional": profesional,
        "id_profesional": id_profesional,
        "id": 900,
    }


def _wait_slot_data(un_solo_slot=True, incluir_15=True):
    """1 slot mostrado (para forzar que 'idx = int(txt)-1' NO alcance a
    seleccionar por índice) pero varios en el pool total (todos_slots),
    incluyendo uno a las 15:00 para que la aclaración PM tenga sentido."""
    mostrado = [_slot("11:00:00")]
    pool = [_slot("11:00:00"), _slot("12:00:00")]
    if incluir_15:
        pool.append(_slot("15:00:00"))
    return {"slots": mostrado, "todos_slots": pool, "especialidad": "psicología adulto"}


def _body_text(resp):
    if isinstance(resp, dict):
        inter = resp.get("interactive", {})
        if inter.get("type") == "button":
            return inter.get("body", {}).get("text", "")
        if inter.get("type") == "list":
            return inter.get("body", {}).get("text", "")
    return resp


class TestNumeroSueltoNoEsHoraMadrugada(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with session.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM conversation_events")
            conn.commit()

    async def test_tres_pide_confirmacion_pm_si_hay_cupo_15(self):
        data = _wait_slot_data(incluir_15=True)
        resp = await flows_mod.handle_message(
            "56900000401", "3", {"state": "WAIT_SLOT", "data": data})
        texto = _body_text(resp)
        self.assertIn("15:00", texto)
        self.assertNotIn("03:00", texto)

    async def test_tres_no_ofrece_pm_si_no_hay_cupo_15(self):
        """Sin cupos a las 15:00, no tiene sentido preguntar por esa hora —
        pero TAMPOCO debe presentar '03:00' como intento de búsqueda real."""
        data = _wait_slot_data(incluir_15=False)
        resp = await flows_mod.handle_message(
            "56900000402", "3", {"state": "WAIT_SLOT", "data": data})
        texto = _body_text(resp)
        self.assertNotIn("03:00", texto)

    async def test_confirmar_pm_busca_la_hora_correcta(self):
        data = _wait_slot_data(incluir_15=True)
        resp1 = await flows_mod.handle_message(
            "56900000403", "3", {"state": "WAIT_SLOT", "data": data})
        self.assertIsInstance(resp1, dict)
        sess = session.get_session("56900000403")
        resp2 = await flows_mod.handle_message(
            "56900000403", "hora_amb_si:15:00", sess)
        texto2 = _body_text(resp2)
        self.assertIn("15:00", texto2)

    async def test_para_el_26_no_muestra_hora_invalida(self):
        data = _wait_slot_data(incluir_15=False)
        resp = await flows_mod.handle_message(
            "56900000404", "para el 26", {"state": "WAIT_SLOT", "data": data})
        texto = _body_text(resp) or ""
        self.assertNotIn("26:00", texto)


class TestQuickBookFechaPedida(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with session.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM conversation_events")
            conn.commit()

    async def _run(self, phone, txt):
        data = {"quick_esp": "psicología adulto", "quick_prof": "Jorge Montalba"}
        return await flows_mod.handle_message(
            phone, txt, {"state": "WAIT_QUICK_BOOK", "data": data})

    async def test_manana_dispara_agendar_no_menu_generico(self):
        with patch.object(flows_mod, "_iniciar_agendar",
                           new=AsyncMock(return_value="ok")) as mock_ag:
            resp = await self._run("56900000501", "mañana")
        mock_ag.assert_called_once()
        _args, _kwargs = mock_ag.call_args
        # especialidad (2do posicional) debe seguir siendo la de quick_esp
        self.assertEqual(_args[2], "psicología adulto")
        self.assertEqual(resp, "ok")

    async def test_el_lunes_dispara_agendar(self):
        with patch.object(flows_mod, "_iniciar_agendar",
                           new=AsyncMock(return_value="ok")) as mock_ag:
            await self._run("56900000502", "el lunes")
        mock_ag.assert_called_once()

    async def test_miercoles_26_dispara_agendar(self):
        with patch.object(flows_mod, "_iniciar_agendar",
                           new=AsyncMock(return_value="ok")) as mock_ag:
            await self._run("56900000503", "miércoles 26")
        mock_ag.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
