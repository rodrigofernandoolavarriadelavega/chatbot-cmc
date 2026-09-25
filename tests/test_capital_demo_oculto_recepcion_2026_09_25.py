"""test_capital_demo_oculto_recepcion_2026_09_25.py — los teléfonos activos en
el desvío DEMO Capital Travel (`capital_demo.activo(phone)`) NO son pacientes
del CMC y no deben aparecer listados en:
  - `session.get_conversations()` (fuente única de /admin/api/conversations
    Y de recepcion_kanban_routes.build_board() → board + cola)
  - `session.get_metricas()` → desglose "Canales" (agregación por canal)

Sí deben seguir accesibles por acceso DIRECTO (`session.get_messages(phone)`,
que alimenta GET /admin/api/conversations/{phone}) — el dueño lo usa.

Al vencer la demo (fecha pasada / teléfono fuera de whitelist), el teléfono
vuelve a aparecer en los listados normalmente.

Correr: PYTHONPATH=app:. venv/bin/python tests/test_capital_demo_oculto_recepcion_2026_09_25.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

os.environ.setdefault("SQLCIPHER_KEY", "")

import session  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
session.DB_PATH = Path(_tmp.name)

import capital_demo  # noqa: E402
import recepcion_kanban_routes as kanban  # noqa: E402

PHONE_DEMO = "56912345678"
PHONE_NORMAL = "56999999999"


class CapitalDemoOcultoBase(unittest.TestCase):
    def setUp(self):
        self._env_bak = dict(os.environ)
        os.environ["CAPITAL_DEMO_PHONES"] = PHONE_DEMO
        os.environ["CAPITAL_DEMO_HASTA"] = "2099-01-01"

        # DB limpia por test — mismo patrón que otros tests de session.py.
        with session.db() as conn:
            for tabla in ("messages", "sessions", "contact_profiles"):
                conn.execute(f"DELETE FROM {tabla}")

        # Un mensaje entrante de cada teléfono, como llegaría por el webhook.
        session.log_message(PHONE_DEMO, "in", "hola, quiero un tour", "CAPITAL_DEMO",
                            canal="capital_demo")
        session.log_message(PHONE_NORMAL, "in", "quiero una hora con medicina general",
                            "IDLE", canal="whatsapp")
        session.save_session(PHONE_NORMAL, "WAIT_ESPECIALIDAD", {})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_bak)


class TestGetConversations(CapitalDemoOcultoBase):
    def test_telefono_en_desvio_no_aparece_en_la_lista(self):
        phones = [c["phone"] for c in session.get_conversations()]
        self.assertNotIn(PHONE_DEMO, phones)

    def test_telefono_normal_si_aparece(self):
        phones = [c["phone"] for c in session.get_conversations()]
        self.assertIn(PHONE_NORMAL, phones)

    def test_acceso_directo_por_telefono_sigue_funcionando(self):
        """GET /admin/api/conversations/{phone} usa get_messages, no
        get_conversations — el dueño sigue pudiendo abrir la conversación
        directo aunque no esté listada."""
        msgs = session.get_messages(PHONE_DEMO)
        self.assertTrue(any(m["text"] == "hola, quiero un tour" for m in msgs))

    def test_al_vencer_la_demo_vuelve_a_aparecer(self):
        os.environ["CAPITAL_DEMO_HASTA"] = "2020-01-01"  # vencida
        phones = [c["phone"] for c in session.get_conversations()]
        self.assertIn(PHONE_DEMO, phones)

    def test_fuera_de_whitelist_tambien_vuelve_a_aparecer(self):
        os.environ["CAPITAL_DEMO_PHONES"] = "56900000000"  # ya no incluye PHONE_DEMO
        phones = [c["phone"] for c in session.get_conversations()]
        self.assertIn(PHONE_DEMO, phones)

    def test_sin_env_capital_demo_todos_aparecen(self):
        os.environ.pop("CAPITAL_DEMO_PHONES", None)
        os.environ.pop("CAPITAL_DEMO_HASTA", None)
        phones = [c["phone"] for c in session.get_conversations()]
        self.assertIn(PHONE_DEMO, phones)
        self.assertIn(PHONE_NORMAL, phones)

    def test_filtro_fail_open_si_capital_demo_no_importa(self):
        """Si el import de capital_demo fallara por cualquier motivo, la
        lista de conversaciones NO debe desaparecer — fail-open."""
        import builtins
        real_import = builtins.__import__

        def _import_que_falla(name, *a, **kw):
            if name == "capital_demo":
                raise RuntimeError("simulado")
            return real_import(name, *a, **kw)

        builtins.__import__ = _import_que_falla
        try:
            phones = [c["phone"] for c in session.get_conversations()]
        finally:
            builtins.__import__ = real_import
        self.assertIn(PHONE_DEMO, phones)
        self.assertIn(PHONE_NORMAL, phones)


class TestKanbanBoardYCola(CapitalDemoOcultoBase):
    def test_board_no_incluye_telefono_del_desvio(self):
        board = kanban.build_board(None, None)
        phones = [c["phone"] for c in board["cards"]]
        self.assertNotIn(PHONE_DEMO, phones)
        self.assertIn(PHONE_NORMAL, phones)

    def test_cola_v2_tampoco_lo_incluye(self):
        import recepcion_kanban_v2 as v2
        board = kanban.build_board(None, None)
        cola = v2.construir(board["cards"])
        # Recorre todas las columnas/carriles de la cola buscando el phone.
        raw = str(cola)
        self.assertNotIn(PHONE_DEMO, raw)


class TestMetricasPorCanal(CapitalDemoOcultoBase):
    """get_case_study_report() es la agregación por canal (GROUP BY canal)
    que alimenta el reporte de KPIs — canal='capital_demo' no debe figurar."""

    def test_canales_excluye_capital_demo(self):
        reporte = session.get_case_study_report(dias=30)
        self.assertNotIn("capital_demo", reporte.get("canales", {}))

    def test_canales_incluye_whatsapp_normal(self):
        reporte = session.get_case_study_report(dias=30)
        self.assertIn("whatsapp", reporte.get("canales", {}))


if __name__ == "__main__":
    unittest.main()


def test_busqueda_del_panel_no_devuelve_chats_del_desvio(monkeypatch):
    import admin_routes
    monkeypatch.setenv("CAPITAL_DEMO_PHONES", "51955058247")
    monkeypatch.setenv("CAPITAL_DEMO_HASTA", "2099-01-01")
    monkeypatch.setattr(admin_routes, "search_messages", lambda q: [
        {"phone": "51955058247", "text": "Quiero Estética"},
        {"phone": "56911112222", "text": "Quiero Estética"}])
    r = admin_routes.admin_search_messages("Estética", _="x")
    assert [x["phone"] for x in r["results"]] == ["56911112222"]


def test_numeros_visibles_en_cmc_no_se_ocultan(monkeypatch):
    import capital_demo
    monkeypatch.setenv("CAPITAL_DEMO_PHONES", "56983129274,56987834148,51955058247")
    monkeypatch.setenv("CAPITAL_DEMO_HASTA", "2099-01-01")
    monkeypatch.setenv("CAPITAL_DEMO_VISIBLE_CMC", "56987834148,51955058247")
    assert capital_demo.oculto_en_cmc("56983129274") is True      # cliente externo: oculto
    assert capital_demo.oculto_en_cmc("56987834148") is False     # dueño: visible en ambos
    assert capital_demo.oculto_en_cmc("+51955058247") is False
    assert capital_demo.oculto_en_cmc("56911112222") is False     # paciente normal
