"""test_capital_demo_recepcion_2026_09_25.py — bandeja de recepción de Alma
Capital sobre el desvío DEMO Capital Travel: espejo, modo humano, y los 4
endpoints internos (/internal/capital-demo/*).

Cubre:
  1. El espejo se dispara con el formato pedido (telefono/direccion/autor/
     texto/tipo/wamid/ts) y no rompe la demo si Alma Capital está caído.
  2. Modo humano: el bot registra y espeja lo entrante pero NO responde.
  3. Los 4 endpoints internos rechazan (404) sin secreto, con secreto
     equivocado, y con X-Forwarded-For/X-Real-IP (señal de tráfico por
     nginx desde internet) — nunca delatan con 401/403.
  4. /enviar: rechaza (403) teléfono fuera de la whitelist; éxito registra
     + espeja con autor=operador; ventana cerrada (código 131047 vía
     messaging.ventana_cerrada_ultimo_envio) → 409 sin loguear.
  5. /modo, /estado, /historial funcionan con el secreto correcto.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

os.environ.setdefault("SQLCIPHER_KEY", "")

import capital_demo  # noqa: E402
import capital_demo_routes  # noqa: E402

PHONE_DEMO = "56912345678"
PHONE_OTRO = "56999999999"
SECRETO = "capital-secreto-test"


def _msg_texto(body: str) -> dict:
    return {"type": "text", "text": {"body": body}}


async def _drenar_tasks(veces: int = 3):
    """Deja correr las Tasks de fondo (espejo fire-and-forget) antes de
    assertar. asyncio.sleep(0) cede el control al loop N veces."""
    for _ in range(veces):
        await asyncio.sleep(0)


class CapitalDemoRecepcionBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._env_bak = dict(os.environ)
        os.environ["CAPITAL_DEMO_PHONES"] = PHONE_DEMO
        os.environ["CAPITAL_DEMO_HASTA"] = "2099-01-01"
        os.environ["CAPITAL_WA_SECRET"] = SECRETO

        capital_demo._historial.clear()
        capital_demo._historial_ts.clear()
        capital_demo._cache_ctx.update(ts=0.0, texto=None, whatsapp=capital_demo._WHATSAPP_FALLBACK)

        self._tmpdir = tempfile.TemporaryDirectory()
        self._enviados_path_bak = capital_demo._ENVIADOS_PATH
        capital_demo._ENVIADOS_PATH = Path(self._tmpdir.name) / "capital_demo_enviados.json"
        capital_demo._enviados_mem = None

        self._humano_path_bak = capital_demo._HUMANO_PATH
        capital_demo._HUMANO_PATH = Path(self._tmpdir.name) / "capital_demo_humano.json"
        capital_demo._humano_mem = None

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_bak)
        capital_demo._ENVIADOS_PATH = self._enviados_path_bak
        capital_demo._enviados_mem = None
        capital_demo._HUMANO_PATH = self._humano_path_bak
        capital_demo._humano_mem = None
        self._tmpdir.cleanup()

    def _marcar_ya_recibido(self, phone: str):
        capital_demo._marcar_enviado(phone)


class TestEspejo(CapitalDemoRecepcionBase):
    async def test_espejo_dispara_con_formato_correcto_inbound_y_outbound(self):
        self._marcar_ya_recibido(PHONE_DEMO)
        contexto_fake = ("TOURS DISPONIBLES:\n- [id 1] Trekking Farellones", "56900000000")

        class _ClaudeRespFake:
            def __init__(self, text):
                self.content = [MagicMock(text=text)]

        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock, return_value="wamid-bot-1"), \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock(return_value=contexto_fake)), \
             patch.object(capital_demo.client.messages, "create",
                          new=AsyncMock(return_value=_ClaudeRespFake("¡Hola! Tenemos tours."))), \
             patch.object(capital_demo, "_espejo_post", new=AsyncMock()) as ep:
            intercepto = await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, _msg_texto("hola, qué tours tienen"), "text", wa_profile_name="Juan"
            )
            await _drenar_tasks()

        self.assertTrue(intercepto)
        self.assertGreaterEqual(ep.await_count, 2)  # 1 inbound + 1 outbound (al menos)

        payload_in = ep.await_args_list[0].args[0]
        self.assertEqual(payload_in["telefono"], PHONE_DEMO)
        self.assertEqual(payload_in["direccion"], "in")
        self.assertEqual(payload_in["autor"], "cliente")
        self.assertEqual(payload_in["texto"], "hola, qué tours tienen")
        self.assertEqual(payload_in["tipo"], "text")
        self.assertEqual(payload_in["nombre"], "Juan")
        self.assertIn("ts", payload_in)
        # ISO8601 con zona: debe parsear con fromisoformat
        from datetime import datetime as _dt
        _dt.fromisoformat(payload_in["ts"])

        payload_out = ep.await_args_list[-1].args[0]
        self.assertEqual(payload_out["direccion"], "out")
        self.assertEqual(payload_out["autor"], "bot")
        self.assertEqual(payload_out["wamid"], "wamid-bot-1")

    async def test_espejo_no_dispara_sin_capital_wa_secret(self):
        os.environ.pop("CAPITAL_WA_SECRET", None)
        with patch("resilience.spawn_task") as st:
            capital_demo._espejo(PHONE_DEMO, "in", "cliente", "hola")
        st.assert_not_called()

    async def test_espejo_no_rompe_si_alma_capital_esta_caido(self):
        """_espejo_post nunca debe propagar la excepción — Alma Capital caído
        no puede tumbar ni frenar la demo de Capital Travel."""
        import httpx as _httpx

        class _ClienteQueFalla:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                raise _httpx.ConnectError("connection refused")

        with patch.object(_httpx, "AsyncClient", return_value=_ClienteQueFalla()):
            # No debe lanzar excepción.
            await capital_demo._espejo_post({
                "telefono": PHONE_DEMO, "direccion": "in", "autor": "cliente",
                "texto": "hola", "tipo": "text", "wamid": None, "ts": "2026-09-25T00:00:00-03:00",
            })

    async def test_espejo_fire_and_forget_no_bloquea_el_envio_al_paciente(self):
        """Aunque Alma Capital esté caído (httpx falla dentro de _espejo_post,
        que ya lo atrapa), el paciente igual recibe su respuesta — el espejo
        corre en background, no en el camino crítico."""
        import httpx as _httpx

        self._marcar_ya_recibido(PHONE_DEMO)
        contexto_fake = ("TOURS DISPONIBLES:\n- [id 1] X", "56900000000")

        class _ClaudeRespFake:
            def __init__(self, text):
                self.content = [MagicMock(text=text)]

        class _ClienteQueFalla:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                raise _httpx.ConnectError("connection refused")

        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock) as sw, \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock(return_value=contexto_fake)), \
             patch.object(capital_demo.client.messages, "create",
                          new=AsyncMock(return_value=_ClaudeRespFake("hola"))), \
             patch.object(_httpx, "AsyncClient", return_value=_ClienteQueFalla()):
            intercepto = await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, _msg_texto("hola"), "text"
            )
            await _drenar_tasks()
        self.assertTrue(intercepto)
        sw.assert_awaited_once()  # el paciente igual recibió su respuesta


class TestModoHumano(CapitalDemoRecepcionBase):
    async def test_modo_humano_silencia_bot_pero_registra_y_espeja(self):
        self._marcar_ya_recibido(PHONE_DEMO)
        capital_demo.set_modo(PHONE_DEMO, "humano")
        self.assertTrue(capital_demo.es_modo_humano(PHONE_DEMO))

        with patch("session.log_message") as lm, \
             patch("messaging.send_whatsapp", new_callable=AsyncMock) as sw, \
             patch.object(capital_demo, "_espejo_post", new=AsyncMock()) as ep:
            intercepto = await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, _msg_texto("hola, alguien ahí?"), "text"
            )
            await _drenar_tasks()

        self.assertTrue(intercepto)
        sw.assert_not_awaited()  # el bot NO responde
        lm.assert_called_once()  # solo el registro del entrante
        ep.assert_awaited_once()  # pero sí se espeja
        payload = ep.await_args_list[0].args[0]
        self.assertEqual(payload["direccion"], "in")
        self.assertEqual(payload["autor"], "cliente")

    async def test_modo_humano_no_reenvia_flyer_de_primer_contacto(self):
        """Si un teléfono NUEVO (sin flyer aún) entra en modo humano antes de
        escribir, el bot tampoco manda el flyer de bienvenida."""
        capital_demo.set_modo(PHONE_DEMO, "humano")
        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock) as sw, \
             patch("messaging.send_whatsapp_interactive", new_callable=AsyncMock) as swi, \
             patch.object(capital_demo, "_espejo_post", new=AsyncMock()):
            intercepto = await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, _msg_texto("hola"), "text"
            )
            await _drenar_tasks()
        self.assertTrue(intercepto)
        sw.assert_not_awaited()
        swi.assert_not_awaited()

    async def test_set_modo_bot_reactiva_respuestas(self):
        self._marcar_ya_recibido(PHONE_DEMO)
        capital_demo.set_modo(PHONE_DEMO, "humano")
        capital_demo.set_modo(PHONE_DEMO, "bot")
        self.assertFalse(capital_demo.es_modo_humano(PHONE_DEMO))

        contexto_fake = ("TOURS DISPONIBLES:\n- [id 1] X", "56900000000")

        class _ClaudeRespFake:
            def __init__(self, text):
                self.content = [MagicMock(text=text)]

        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock) as sw, \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock(return_value=contexto_fake)), \
             patch.object(capital_demo.client.messages, "create",
                          new=AsyncMock(return_value=_ClaudeRespFake("hola"))), \
             patch.object(capital_demo, "_espejo_post", new=AsyncMock()):
            await capital_demo.manejar_webhook_wa(PHONE_DEMO, _msg_texto("hola de nuevo"), "text")
        sw.assert_awaited_once()

    async def test_modo_humano_persiste_via_archivo_atomico(self):
        capital_demo.set_modo(PHONE_DEMO, "humano")
        self.assertTrue(capital_demo._HUMANO_PATH.exists())
        data = json.loads(capital_demo._HUMANO_PATH.read_text(encoding="utf-8"))
        self.assertIn(PHONE_DEMO, data)
        # Simula reinicio: memoria se pierde, el archivo sobrevive.
        capital_demo._humano_mem = None
        self.assertTrue(capital_demo.es_modo_humano(PHONE_DEMO))


class EndpointsBase(CapitalDemoRecepcionBase):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(capital_demo_routes.router)
        self.client = TestClient(app)

    def _headers(self, secreto: str | None = SECRETO, extra: dict | None = None) -> dict:
        h = {}
        if secreto is not None:
            h["X-Alma-Secret"] = secreto
        if extra:
            h.update(extra)
        return h


class TestEndpointsSeguridad(EndpointsBase):
    def test_sin_capital_wa_secret_env_da_404(self):
        os.environ.pop("CAPITAL_WA_SECRET", None)
        r = self.client.get("/internal/capital-demo/estado", params={"telefono": PHONE_DEMO},
                             headers=self._headers(secreto="cualquiera"))
        self.assertEqual(r.status_code, 404)

    def test_sin_header_secreto_da_404(self):
        r = self.client.get("/internal/capital-demo/estado", params={"telefono": PHONE_DEMO},
                             headers=self._headers(secreto=None))
        self.assertEqual(r.status_code, 404)

    def test_header_secreto_equivocado_da_404(self):
        r = self.client.get("/internal/capital-demo/estado", params={"telefono": PHONE_DEMO},
                             headers=self._headers(secreto="secreto-incorrecto"))
        self.assertEqual(r.status_code, 404)

    def test_x_forwarded_for_da_404_aunque_el_secreto_sea_correcto(self):
        r = self.client.get(
            "/internal/capital-demo/estado", params={"telefono": PHONE_DEMO},
            headers=self._headers(extra={"X-Forwarded-For": "203.0.113.7"}),
        )
        self.assertEqual(r.status_code, 404)

    def test_x_real_ip_da_404_aunque_el_secreto_sea_correcto(self):
        r = self.client.get(
            "/internal/capital-demo/estado", params={"telefono": PHONE_DEMO},
            headers=self._headers(extra={"X-Real-Ip": "203.0.113.7"}),
        )
        self.assertEqual(r.status_code, 404)

    def test_llamada_local_legitima_pasa_el_guard(self):
        r = self.client.get("/internal/capital-demo/estado", params={"telefono": PHONE_DEMO},
                             headers=self._headers())
        self.assertEqual(r.status_code, 200)

    def test_guard_aplica_a_las_4_rutas(self):
        # Sin secreto correcto, las 4 rutas deben dar 404 (no 401/403/422).
        headers = self._headers(secreto="malo")
        r1 = self.client.post("/internal/capital-demo/enviar",
                               json={"telefono": PHONE_DEMO, "texto": "hola"}, headers=headers)
        r2 = self.client.post("/internal/capital-demo/modo",
                               json={"telefono": PHONE_DEMO, "modo": "humano"}, headers=headers)
        r3 = self.client.get("/internal/capital-demo/estado",
                              params={"telefono": PHONE_DEMO}, headers=headers)
        r4 = self.client.get("/internal/capital-demo/historial",
                              params={"telefono": PHONE_DEMO}, headers=headers)
        for r in (r1, r2, r3, r4):
            self.assertEqual(r.status_code, 404)


class TestEndpointEnviar(EndpointsBase):
    def test_telefono_fuera_de_whitelist_da_403(self):
        r = self.client.post("/internal/capital-demo/enviar",
                              json={"telefono": PHONE_OTRO, "texto": "hola"},
                              headers=self._headers())
        self.assertEqual(r.status_code, 403)

    def test_enviar_exitoso_registra_y_espeja(self):
        with patch.object(capital_demo_routes, "send_whatsapp",
                           new=AsyncMock(return_value="wamid-op-1")), \
             patch.object(capital_demo_routes, "ventana_cerrada_ultimo_envio", return_value=False), \
             patch.object(capital_demo, "_log", new=AsyncMock()) as log_mock:
            r = self.client.post("/internal/capital-demo/enviar",
                                  json={"telefono": PHONE_DEMO, "texto": "mensaje del operador"},
                                  headers=self._headers())
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["wamid"], "wamid-op-1")
        log_mock.assert_awaited_once()
        _, kwargs = log_mock.await_args
        args = log_mock.await_args.args
        self.assertEqual(args[0], PHONE_DEMO)
        self.assertEqual(args[1], "out")
        self.assertEqual(args[2], "mensaje del operador")
        self.assertEqual(kwargs.get("autor"), "operador")

    def test_ventana_cerrada_da_409_y_no_registra(self):
        with patch.object(capital_demo_routes, "send_whatsapp",
                           new=AsyncMock(return_value=None)), \
             patch.object(capital_demo_routes, "ventana_cerrada_ultimo_envio", return_value=True), \
             patch.object(capital_demo, "_log", new=AsyncMock()) as log_mock:
            r = self.client.post("/internal/capital-demo/enviar",
                                  json={"telefono": PHONE_DEMO, "texto": "hola"},
                                  headers=self._headers())
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json(), {"error": "ventana_cerrada"})
        log_mock.assert_not_awaited()

    def test_envio_fallido_da_502_y_no_registra(self):
        with patch.object(capital_demo_routes, "send_whatsapp",
                           new=AsyncMock(return_value=None)), \
             patch.object(capital_demo_routes, "ventana_cerrada_ultimo_envio", return_value=False), \
             patch.object(capital_demo, "_log", new=AsyncMock()) as log_mock:
            r = self.client.post("/internal/capital-demo/enviar",
                                  json={"telefono": PHONE_DEMO, "texto": "hola"},
                                  headers=self._headers())
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json(), {"error": "envio_fallido"})
        log_mock.assert_not_awaited()

    def test_faltan_campos_da_400(self):
        r = self.client.post("/internal/capital-demo/enviar", json={"telefono": PHONE_DEMO},
                              headers=self._headers())
        self.assertEqual(r.status_code, 400)


class TestEndpointModoEstadoHistorial(EndpointsBase):
    def test_modo_actualiza_el_set(self):
        r = self.client.post("/internal/capital-demo/modo",
                              json={"telefono": PHONE_DEMO, "modo": "humano"},
                              headers=self._headers())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(capital_demo.es_modo_humano(PHONE_DEMO))

        r2 = self.client.post("/internal/capital-demo/modo",
                               json={"telefono": PHONE_DEMO, "modo": "bot"},
                               headers=self._headers())
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(capital_demo.es_modo_humano(PHONE_DEMO))

    def test_modo_invalido_da_400(self):
        r = self.client.post("/internal/capital-demo/modo",
                              json={"telefono": PHONE_DEMO, "modo": "lo-que-sea"},
                              headers=self._headers())
        self.assertEqual(r.status_code, 400)

    def test_estado_refleja_modo_y_flyer(self):
        capital_demo._marcar_enviado(PHONE_DEMO)
        capital_demo.set_modo(PHONE_DEMO, "humano")
        r = self.client.get("/internal/capital-demo/estado", params={"telefono": PHONE_DEMO},
                             headers=self._headers())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"modo": "humano", "flyer_enviado": True})

    def test_historial_filtra_canal_capital_demo_y_mapea_autor(self):
        filas = [
            {"direction": "in", "text": "hola", "state": "CAPITAL_DEMO",
             "canal": "capital_demo", "wamid": None, "ts": "2026-09-25 10:00:00"},
            {"direction": "out", "text": "respuesta bot", "state": "CAPITAL_DEMO",
             "canal": "capital_demo", "wamid": "w1", "ts": "2026-09-25 10:00:05"},
            {"direction": "out", "text": "respuesta operador", "state": "CAPITAL_DEMO_OPERADOR",
             "canal": "capital_demo", "wamid": "w2", "ts": "2026-09-25 10:01:00"},
            {"direction": "in", "text": "mensaje de un paciente CMC", "state": "IDLE",
             "canal": "whatsapp", "wamid": None, "ts": "2026-09-25 10:02:00"},
        ]
        with patch.object(capital_demo_routes, "get_messages", return_value=filas):
            r = self.client.get("/internal/capital-demo/historial", params={"telefono": PHONE_DEMO},
                                 headers=self._headers())
        self.assertEqual(r.status_code, 200)
        data = r.json()
        mensajes = data["mensajes"]
        self.assertEqual(len(mensajes), 3)  # el de canal=whatsapp queda afuera
        self.assertEqual(mensajes[0]["autor"], "cliente")
        self.assertEqual(mensajes[1]["autor"], "bot")
        self.assertEqual(mensajes[2]["autor"], "operador")
        self.assertEqual(mensajes[1]["wamid"], "w1")
        self.assertIn("T10:00:00", mensajes[0]["ts"])


if __name__ == "__main__":
    unittest.main()
