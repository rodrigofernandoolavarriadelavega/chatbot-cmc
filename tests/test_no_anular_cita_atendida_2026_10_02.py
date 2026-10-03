"""El bot anulaba citas YA ATENDIDAS (caso 2026-09-28, Martín con Abarca 14:15,
anulada 22:09): el seguimiento posconsulta ofrecía "reagendar" (id "2") y el
flujo listaba la cita del mismo día ya atendida.

Uso: PYTHONPATH=app:. python tests/test_no_anular_cita_atendida_2026_10_02.py
"""
import asyncio, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import AsyncMock
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app")); sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="cmc_no_anular_")) / "s.db"
os.environ["SESSIONS_DB"] = str(TMP); os.environ.setdefault("SQLCIPHER_KEY", "")
import session  # noqa: E402
session.DB_PATH = TMP
import medilink  # noqa: E402


class _R:
    def __init__(self, code, js=None, text=""):
        self.status_code, self._js, self.text = code, js or {}, text
    def json(self):
        return self._js


class _Cli:
    def __init__(self, estado):
        self.estado, self.puts = estado, []
    async def put(self, url, json=None, headers=None):
        self.puts.append(url); return _R(200)


class TestGuardCancelar(unittest.TestCase):
    def _run(self, id_estado, estado_txt):
        cli = _Cli(id_estado)
        medilink._get_shared_client = lambda: cli
        async def fake_get(client, url, **kw):
            return _R(200, {"data": {"id": 1, "id_estado": id_estado, "estado_cita": estado_txt}})
        medilink._get = fake_get
        medilink._mark_cancelada_por_sistema = lambda *_a, **_k: None
        ok, motivo = asyncio.run(medilink.cancelar_cita_con_motivo(1))
        return ok, motivo, cli.puts

    def test_no_anula_atendida_ni_en_sala(self):
        for est, txt in ((2, "Atendido"), (5, "En sala de espera"), (6, "Atendiéndose")):
            ok, motivo, puts = self._run(est, txt)
            self.assertFalse(ok, txt); self.assertFalse(puts, txt); self.assertIn("atendida", motivo)

    def test_anula_cita_pendiente(self):
        ok, _m, puts = self._run(7, "No confirmado")
        self.assertTrue(ok); self.assertEqual(len(puts), 1)


class TestListaSinAtendidas(unittest.TestCase):
    def test_helper(self):
        self.assertTrue(medilink._cita_ya_avanzo({"id_estado": 2}))
        self.assertTrue(medilink._cita_ya_avanzo({"estado_cita": "En sala de espera"}))
        self.assertFalse(medilink._cita_ya_avanzo({"id_estado": 7, "estado_cita": "No confirmado"}))
        self.assertFalse(medilink._cita_ya_avanzo({"id_estado": 3, "estado_cita": "Confirmado por teléfono"}))


class TestSeguimientoOfreceCitaNueva(unittest.IsolatedAsyncioTestCase):
    async def test_boton_es_agendar_no_reagendar(self):
        import messaging
        messaging.send_whatsapp = AsyncMock(return_value="wamid.T")
        import flows
        flows._iniciar_agendar = AsyncMock(return_value="slots")
        flows._iniciar_reagendar = AsyncMock(return_value="reagendar")
        p = "56900007001"
        session.save_fidelizacion_respuesta if hasattr(session, "save_fidelizacion_respuesta") else None
        # Simula el handler del botón: data guardada por la oferta del seguimiento
        r = await flows.handle_message(p, "seg_control",
                                       {"state": "IDLE", "data": {"seg_control_esp": "medicina general"}})
        self.assertEqual(r, "slots")
        flows._iniciar_agendar.assert_awaited()
        self.assertEqual(flows._iniciar_agendar.await_args.args[2], "medicina general")
        flows._iniciar_reagendar.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
