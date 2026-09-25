"""test_capital_demo_2026_09_24.py — desvío DEMO Capital Travel (whitelist).

Cubre el contrato pedido para la demo de venta:
  1. Teléfono en whitelist + fecha vigente → va al asistente de Capital,
     NUNCA toca session.get_session/save_session ni flows.handle_message.
  2. Teléfono fuera de whitelist → no intercepta (flujo normal intacto).
  3. Fecha vencida (CAPITAL_DEMO_HASTA en el pasado) → no intercepta.
  4. Mensaje exacto "CMC" (cualquier capitalización) → no intercepta (escape).
  5. Mensaje de audio/imagen → responde una línea pidiendo escribir, sin
     tocar Claude ni la API de Capital.
  6. API de Capital caída → responde disculpa breve, nunca truena.
  7. Sin CAPITAL_DEMO_PHONES/CAPITAL_DEMO_HASTA seteadas → módulo inerte.
  8. Primer mensaje de un teléfono whitelisted → flyer (cta_url) + botón
     "PLOMO", ANTES que cualquier respuesta del asistente (salvo pregunta
     concreta). Segundo mensaje en adelante → solo el asistente. Un
     reinicio simulado (borrar el estado en memoria, dejar el archivo) NO
     vuelve a mandar el flyer.

Mockea Anthropic (capital_demo.client) y httpx (capital_demo._fetch_contexto
vía httpx.AsyncClient) — no pega a servicios reales.
"""
from __future__ import annotations

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

PHONE_DEMO = "56912345678"
PHONE_OTRO = "56999999999"


def _msg_texto(body: str) -> dict:
    return {"type": "text", "text": {"body": body}}


def _msg_audio() -> dict:
    return {"type": "audio", "audio": {"id": "abc123"}}


def _msg_boton(payload: str, title: str = "") -> dict:
    return {
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": payload, "title": title or payload},
        },
    }


class _ClaudeRespFake:
    def __init__(self, text: str):
        self.content = [MagicMock(text=text)]


class CapitalDemoBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._env_bak = dict(os.environ)
        os.environ["CAPITAL_DEMO_PHONES"] = PHONE_DEMO
        os.environ["CAPITAL_DEMO_HASTA"] = "2099-01-01"
        # Historial en memoria no debe filtrarse entre tests.
        capital_demo._historial.clear()
        capital_demo._historial_ts.clear()
        capital_demo._cache_ctx.update(ts=0.0, texto=None, whatsapp=capital_demo._WHATSAPP_FALLBACK)
        # Archivo de "ya recibió el flyer" aislado por test (tmp), y estado
        # en memoria reseteado — cada test empieza como si nadie hubiese
        # escrito antes.
        self._tmpdir = tempfile.TemporaryDirectory()
        self._enviados_path_bak = capital_demo._ENVIADOS_PATH
        capital_demo._ENVIADOS_PATH = Path(self._tmpdir.name) / "capital_demo_enviados.json"
        capital_demo._enviados_mem = None

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_bak)
        capital_demo._ENVIADOS_PATH = self._enviados_path_bak
        capital_demo._enviados_mem = None
        self._tmpdir.cleanup()

    def _marcar_ya_recibido(self, phone: str):
        """Atajo para los tests que quieren simular que el teléfono YA pasó
        por su primer contacto (así se prueba el camino 'solo asistente')."""
        capital_demo._marcar_enviado(phone)


class TestActivacionWhitelist(CapitalDemoBase):
    def test_telefono_en_whitelist_y_fecha_vigente_activo(self):
        self.assertTrue(capital_demo.activo(PHONE_DEMO))

    def test_telefono_fuera_de_whitelist_no_activo(self):
        self.assertFalse(capital_demo.activo(PHONE_OTRO))

    def test_fecha_vencida_no_activo(self):
        os.environ["CAPITAL_DEMO_HASTA"] = "2020-01-01"
        self.assertFalse(capital_demo.activo(PHONE_DEMO))

    def test_sin_env_vars_no_activo(self):
        os.environ.pop("CAPITAL_DEMO_PHONES", None)
        os.environ.pop("CAPITAL_DEMO_HASTA", None)
        self.assertFalse(capital_demo.activo(PHONE_DEMO))

    def test_solo_phones_sin_hasta_no_activo(self):
        os.environ.pop("CAPITAL_DEMO_HASTA", None)
        self.assertFalse(capital_demo.activo(PHONE_DEMO))

    def test_acepta_telefono_con_mas(self):
        self.assertTrue(capital_demo.activo("+" + PHONE_DEMO))


class TestExtraccionTexto(unittest.TestCase):
    def test_texto_plano(self):
        self.assertEqual(capital_demo.texto_de_mensaje(_msg_texto("hola"), "text"), "hola")

    def test_boton_interactive_usa_title(self):
        m = _msg_boton("payload_x", title="Sí, agendar")
        self.assertEqual(capital_demo.texto_de_mensaje(m, "interactive"), "Sí, agendar")

    def test_audio_no_produce_texto(self):
        self.assertIsNone(capital_demo.texto_de_mensaje(_msg_audio(), "audio"))

    def test_imagen_no_produce_texto(self):
        self.assertIsNone(capital_demo.texto_de_mensaje({"type": "image"}, "image"))


class TestManejarWebhookWA(CapitalDemoBase):
    async def test_fuera_de_whitelist_no_intercepta(self):
        with patch("session.get_session") as gs, patch("flows.handle_message") as hm:
            intercepto = await capital_demo.manejar_webhook_wa(
                PHONE_OTRO, _msg_texto("hola, quiero un tour"), "text"
            )
        self.assertFalse(intercepto)
        gs.assert_not_called()
        hm.assert_not_called()

    async def test_fecha_vencida_no_intercepta(self):
        os.environ["CAPITAL_DEMO_HASTA"] = "2020-01-01"
        with patch("session.get_session") as gs, patch("flows.handle_message") as hm:
            intercepto = await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, _msg_texto("hola"), "text"
            )
        self.assertFalse(intercepto)
        gs.assert_not_called()
        hm.assert_not_called()

    async def test_palabra_escape_cmc_no_intercepta(self):
        for variante in ("CMC", "cmc", "Cmc", "  cmc  "):
            with patch("session.get_session") as gs, patch("flows.handle_message") as hm:
                intercepto = await capital_demo.manejar_webhook_wa(
                    PHONE_DEMO, _msg_texto(variante), "text"
                )
            self.assertFalse(intercepto, f"debió escapar con {variante!r}")
            gs.assert_not_called()
            hm.assert_not_called()

    async def test_whitelist_activa_va_al_asistente_sin_tocar_sesion_cmc(self):
        # No es el primer contacto: así se aísla el camino "solo asistente"
        # del camino "primer contacto → flyer" (cubierto en su propia clase).
        self._marcar_ya_recibido(PHONE_DEMO)
        contexto_fake = ("TOURS DISPONIBLES:\n- [id 1] Trekking Farellones", "56900000000")
        with patch("session.get_session") as gs, \
             patch("session.save_session") as ss, \
             patch("flows.handle_message") as hm, \
             patch("session.log_message") as lm, \
             patch("messaging.send_whatsapp", new_callable=AsyncMock) as sw, \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock(return_value=contexto_fake)), \
             patch.object(capital_demo.client.messages, "create",
                          new=AsyncMock(return_value=_ClaudeRespFake("¡Hola! Tenemos Trekking Farellones."))):
            intercepto = await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, _msg_texto("hola, qué tours tienen"), "text"
            )
        self.assertTrue(intercepto)
        gs.assert_not_called()
        ss.assert_not_called()
        hm.assert_not_called()
        sw.assert_awaited_once()
        # El log de mensajes SÍ se usa (para verlo en el panel), marcado capital_demo
        self.assertGreaterEqual(lm.call_count, 2)
        for call in lm.call_args_list:
            self.assertEqual(call.kwargs.get("canal"), "capital_demo")

    async def test_boton_plomo_se_expande_a_consulta_completa(self):
        self._marcar_ya_recibido(PHONE_DEMO)
        contexto_fake = ("TOURS DISPONIBLES:\n- [id 7] Expedición Cerro El Plomo", "56900000000")
        with patch("session.log_message") as lm, \
             patch("messaging.send_whatsapp", new_callable=AsyncMock), \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock(return_value=contexto_fake)), \
             patch.object(capital_demo.client.messages, "create",
                          new=AsyncMock(return_value=_ClaudeRespFake("info del Plomo"))) as create:
            intercepto = await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, _msg_texto("PLOMO"), "text"
            )
        self.assertTrue(intercepto)
        # El log del panel muestra el botón tal cual lo mandó Meta
        primera_llamada_in = lm.call_args_list[0]
        self.assertEqual(primera_llamada_in.args[2], "PLOMO")
        # Pero a Claude se le manda la consulta expandida, no la palabra suelta
        mensajes = create.call_args.kwargs["messages"]
        self.assertIn("Cerro El Plomo", mensajes[-1]["content"])
        self.assertIn("cupos", mensajes[-1]["content"])

    async def test_boton_plomo_button_type_tambien_expande(self):
        self._marcar_ya_recibido(PHONE_DEMO)
        contexto_fake = ("TOURS DISPONIBLES:\n- [id 7] Expedición Cerro El Plomo", "56900000000")
        msg_btn = {"type": "button", "button": {"text": "PLOMO", "payload": "PLOMO"}}
        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock), \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock(return_value=contexto_fake)), \
             patch.object(capital_demo.client.messages, "create",
                          new=AsyncMock(return_value=_ClaudeRespFake("info del Plomo"))) as create:
            intercepto = await capital_demo.manejar_webhook_wa(PHONE_DEMO, msg_btn, "button")
        self.assertTrue(intercepto)
        mensajes = create.call_args.kwargs["messages"]
        self.assertIn("Cerro El Plomo", mensajes[-1]["content"])

    async def test_whitelist_multiple_telefonos_separados_por_coma(self):
        os.environ["CAPITAL_DEMO_PHONES"] = "56983129274,56987834148"
        self.assertTrue(capital_demo.activo("56983129274"))
        self.assertTrue(capital_demo.activo("56987834148"))
        self.assertFalse(capital_demo.activo(PHONE_OTRO))

    async def test_historial_se_pasa_a_claude_en_el_segundo_turno(self):
        # Historial de conversación, no de primer-contacto: se aísla marcando
        # el teléfono como ya recibido.
        self._marcar_ya_recibido(PHONE_DEMO)
        contexto_fake = ("TOURS DISPONIBLES:\n- [id 1] Trekking Farellones", "56900000000")
        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock), \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock(return_value=contexto_fake)), \
             patch.object(capital_demo.client.messages, "create",
                          new=AsyncMock(return_value=_ClaudeRespFake("respuesta 1"))) as create1:
            await capital_demo.manejar_webhook_wa(PHONE_DEMO, _msg_texto("hola"), "text")

        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock), \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock(return_value=contexto_fake)), \
             patch.object(capital_demo.client.messages, "create",
                          new=AsyncMock(return_value=_ClaudeRespFake("respuesta 2"))) as create2:
            await capital_demo.manejar_webhook_wa(PHONE_DEMO, _msg_texto("cuánto cuesta"), "text")

        segundo_call_kwargs = create2.call_args.kwargs
        mensajes = segundo_call_kwargs["messages"]
        # Debe incluir el turno anterior (user+assistant) antes del nuevo mensaje
        self.assertGreaterEqual(len(mensajes), 3)
        self.assertEqual(mensajes[0]["content"], "hola")
        self.assertEqual(mensajes[-1]["content"], "cuánto cuesta")

    async def test_audio_responde_una_linea_pidiendo_escribir(self):
        with patch("session.log_message") as lm, \
             patch("messaging.send_whatsapp", new_callable=AsyncMock) as sw, \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock()) as fc, \
             patch.object(capital_demo.client.messages, "create", new=AsyncMock()) as cc:
            intercepto = await capital_demo.manejar_webhook_wa(PHONE_DEMO, _msg_audio(), "audio")
        self.assertTrue(intercepto)
        fc.assert_not_called()
        cc.assert_not_called()
        sw.assert_awaited_once()
        texto_enviado = sw.call_args.args[1]
        self.assertIn("texto", texto_enviado.lower())
        self.assertEqual(texto_enviado.count("\n"), 0)  # una sola línea

    async def test_imagen_responde_una_linea_pidiendo_escribir(self):
        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock) as sw, \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock()) as fc:
            intercepto = await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, {"type": "image", "image": {"id": "x"}}, "image"
            )
        self.assertTrue(intercepto)
        fc.assert_not_called()
        sw.assert_awaited_once()

    async def test_api_capital_caida_responde_disculpa(self):
        self._marcar_ya_recibido(PHONE_DEMO)
        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock) as sw, \
             patch.object(capital_demo, "_fetch_contexto",
                          new=AsyncMock(side_effect=Exception("connection refused"))):
            intercepto = await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, _msg_texto("hola, quiero reservar"), "text"
            )
        self.assertTrue(intercepto)
        sw.assert_awaited_once()
        texto_enviado = sw.call_args.args[1]
        self.assertIn("problema técnico", texto_enviado)
        self.assertIn(capital_demo._WHATSAPP_FALLBACK, texto_enviado)

    async def test_claude_falla_responde_disculpa_con_whatsapp_agencia(self):
        self._marcar_ya_recibido(PHONE_DEMO)
        contexto_fake = ("TOURS DISPONIBLES:\n- [id 1] X", "56911112222")
        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock) as sw, \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock(return_value=contexto_fake)), \
             patch.object(capital_demo.client.messages, "create",
                          new=AsyncMock(side_effect=Exception("rate limited"))):
            intercepto = await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, _msg_texto("hola"), "text"
            )
        self.assertTrue(intercepto)
        texto_enviado = sw.call_args.args[1]
        self.assertIn("56911112222", texto_enviado)


class TestPrimerContactoFlyerPlomo(CapitalDemoBase):
    """Requisito del dueño: primer mensaje de un teléfono whitelisted → flyer
    (cta_url, imagen+link) + botón único 'PLOMO', ANTES de cualquier respuesta
    del asistente (salvo pregunta concreta). Persistencia memoria+disco para
    que un reinicio no lo repita."""

    async def test_primer_mensaje_envia_flyer_y_boton_sin_asistente(self):
        with patch("session.log_message") as lm, \
             patch("messaging.send_whatsapp", new_callable=AsyncMock) as sw, \
             patch("messaging.send_whatsapp_interactive", new_callable=AsyncMock) as swi, \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock()) as fc, \
             patch.object(capital_demo.client.messages, "create", new=AsyncMock()) as cc:
            intercepto = await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, _msg_texto("hola"), "text"
            )
        self.assertTrue(intercepto)
        # Saludo corto, sin "?" y ≤6 palabras → NO es pregunta concreta:
        # solo flyer + botón, el asistente no se llama.
        fc.assert_not_called()
        cc.assert_not_called()
        sw.assert_not_awaited()
        self.assertEqual(swi.await_count, 2)

        primer_envio = swi.await_args_list[0].args[1]
        self.assertEqual(primer_envio["type"], "cta_url")
        self.assertEqual(primer_envio["header"]["type"], "image")
        self.assertEqual(primer_envio["header"]["image"]["link"], capital_demo._FLYER_IMG_URL)
        self.assertIn("ALGÚN DÍA VAS A CONTAR ESTA HISTORIA", primer_envio["body"]["text"])
        self.assertEqual(primer_envio["footer"]["text"], "Responde BAJA para no recibir más salidas")
        self.assertEqual(primer_envio["action"]["name"], "cta_url")
        self.assertEqual(primer_envio["action"]["parameters"]["display_text"], "Reservar mi cupo")
        self.assertIn("tour=7", primer_envio["action"]["parameters"]["url"])

        segundo_envio = swi.await_args_list[1].args[1]
        self.assertEqual(segundo_envio["type"], "button")
        botones = segundo_envio["action"]["buttons"]
        self.assertEqual(len(botones), 1)
        self.assertEqual(botones[0]["reply"]["id"], "PLOMO")
        self.assertEqual(botones[0]["reply"]["title"], "PLOMO")

    async def test_primer_mensaje_con_pregunta_concreta_tambien_responde_asistente(self):
        contexto_fake = ("TOURS DISPONIBLES:\n- [id 7] Expedición Cerro El Plomo", "56900000000")
        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock) as sw, \
             patch("messaging.send_whatsapp_interactive", new_callable=AsyncMock) as swi, \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock(return_value=contexto_fake)), \
             patch.object(capital_demo.client.messages, "create",
                          new=AsyncMock(return_value=_ClaudeRespFake("info del Plomo"))) as create:
            intercepto = await capital_demo.manejar_webhook_wa(
                PHONE_DEMO,
                _msg_texto("¿Cuánto cuesta la expedición al Plomo y qué necesito llevar?"),
                "text",
            )
        self.assertTrue(intercepto)
        self.assertEqual(swi.await_count, 2)  # flyer + botón igual se mandan
        create.assert_awaited_once()
        sw.assert_awaited_once()  # y además la respuesta del asistente

    async def test_segundo_mensaje_solo_asistente_sin_flyer(self):
        contexto_fake = ("TOURS DISPONIBLES:\n- [id 1] Trekking Farellones", "56900000000")
        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock), \
             patch("messaging.send_whatsapp_interactive", new_callable=AsyncMock), \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock(return_value=contexto_fake)), \
             patch.object(capital_demo.client.messages, "create",
                          new=AsyncMock(return_value=_ClaudeRespFake("resp"))):
            await capital_demo.manejar_webhook_wa(PHONE_DEMO, _msg_texto("hola"), "text")

        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock) as sw2, \
             patch("messaging.send_whatsapp_interactive", new_callable=AsyncMock) as swi2, \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock(return_value=contexto_fake)), \
             patch.object(capital_demo.client.messages, "create",
                          new=AsyncMock(return_value=_ClaudeRespFake("resp 2"))):
            intercepto2 = await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, _msg_texto("cuánto cuesta"), "text"
            )
        self.assertTrue(intercepto2)
        swi2.assert_not_awaited()
        sw2.assert_awaited_once()

    async def test_reinicio_simulado_no_reenvia_flyer(self):
        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock), \
             patch("messaging.send_whatsapp_interactive", new_callable=AsyncMock) as swi1:
            await capital_demo.manejar_webhook_wa(PHONE_DEMO, _msg_texto("hola"), "text")
        self.assertEqual(swi1.await_count, 2)
        self.assertTrue(capital_demo._ENVIADOS_PATH.exists())

        # "Reinicio": se borra el estado en memoria del proceso (como pasaría
        # con un restart del servicio); el archivo en disco sigue ahí.
        capital_demo._enviados_mem = None

        contexto_fake = ("TOURS DISPONIBLES:\n- [id 1] X", "56900000000")
        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock) as sw2, \
             patch("messaging.send_whatsapp_interactive", new_callable=AsyncMock) as swi2, \
             patch.object(capital_demo, "_fetch_contexto", new=AsyncMock(return_value=contexto_fake)), \
             patch.object(capital_demo.client.messages, "create",
                          new=AsyncMock(return_value=_ClaudeRespFake("resp"))):
            await capital_demo.manejar_webhook_wa(PHONE_DEMO, _msg_texto("hola de nuevo"), "text")

        swi2.assert_not_awaited()
        sw2.assert_awaited_once()

    async def test_nombre_flyer_usa_perfil_whatsapp_si_viene(self):
        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock), \
             patch("messaging.send_whatsapp_interactive", new_callable=AsyncMock) as swi:
            await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, _msg_texto("hola"), "text", wa_profile_name="Fernanda Soto"
            )
        primer_envio = swi.await_args_list[0].args[1]
        self.assertIn("¡Hola Fernanda!", primer_envio["body"]["text"])

    async def test_nombre_flyer_usa_fallback_por_telefono_si_no_hay_perfil(self):
        os.environ["CAPITAL_DEMO_PHONES"] = "56983129274,56987834148"
        with patch("session.log_message"), \
             patch("messaging.send_whatsapp", new_callable=AsyncMock), \
             patch("messaging.send_whatsapp_interactive", new_callable=AsyncMock) as swi:
            await capital_demo.manejar_webhook_wa(
                "56983129274", _msg_texto("hola"), "text", wa_profile_name=""
            )
        primer_envio = swi.await_args_list[0].args[1]
        self.assertIn("¡Hola Juan Carlos!", primer_envio["body"]["text"])

    async def test_link_reserva_incluye_src(self):
        os.environ["CAPITAL_DEMO_SRC"] = "dif-1"
        try:
            with patch("session.log_message"), \
                 patch("messaging.send_whatsapp", new_callable=AsyncMock), \
                 patch("messaging.send_whatsapp_interactive", new_callable=AsyncMock) as swi:
                await capital_demo.manejar_webhook_wa(PHONE_DEMO, _msg_texto("hola"), "text")
            primer_envio = swi.await_args_list[0].args[1]
            self.assertIn("src=dif-1", primer_envio["action"]["parameters"]["url"])
        finally:
            os.environ.pop("CAPITAL_DEMO_SRC", None)


class TestContexto(unittest.TestCase):
    def test_render_contexto_incluye_id_y_salidas(self):
        tours = [{
            "id": 7, "nombre": "Trekking Farellones", "categoria": "trekking",
            "dificultad": "media", "altitud_m": 2200, "duracion_dias": 1,
            "destino": "Farellones", "precio_persona": 45000,
        }]
        salidas = {7: [{"fecha": "12/10/2026", "hora": "08:00", "disponibles": 4, "cupo": 10}]}
        condiciones = [{"titulo": "Nieve", "mensaje": "Cordón cerrado por nieve", "estado": "alerta"}]
        texto = capital_demo._render_contexto(tours, salidas, condiciones)
        self.assertIn("[id 7]", texto)
        self.assertIn("12/10/2026", texto)
        self.assertIn("Cordón cerrado por nieve", texto)

    def test_render_contexto_sin_salida_lo_dice_explicito(self):
        tours = [{"id": 9, "nombre": "Tour X", "precio_persona": 1000}]
        texto = capital_demo._render_contexto(tours, {}, [])
        self.assertIn("sin salida programada", texto)


class TestLinkYPromptSinPacientesCMC(unittest.TestCase):
    def test_prompt_instruye_no_mencionar_cmc(self):
        prompt = capital_demo._system_prompt("contexto de prueba", "56900000000")
        self.assertIn("Nunca menciones al Centro Médico Carampangue", prompt)

    def test_prompt_incluye_link_agendar_con_src(self):
        os.environ["CAPITAL_DEMO_SRC"] = "dif-3"
        try:
            prompt = capital_demo._system_prompt("ctx", "5690")
            self.assertIn("src=dif-3", prompt)
        finally:
            os.environ.pop("CAPITAL_DEMO_SRC", None)

    def test_prompt_sin_src_no_agrega_parametro_vacio(self):
        os.environ.pop("CAPITAL_DEMO_SRC", None)
        prompt = capital_demo._system_prompt("ctx", "5690")
        self.assertNotIn("&src=", prompt)


if __name__ == "__main__":
    unittest.main()


def test_sin_voseo_corrige_formas_argentinas():
    import capital_demo as c
    assert c.sin_voseo("Dormís en carpa y tenés que llevar ropa.") == "Duermes en carpa y tienes que llevar ropa."
    assert c.sin_voseo("Contame cuándo querés ir. Mirá el cerro.") == "Cuéntame cuándo quieres ir. Mira el cerro."
    # no toca tuteo ni palabras que solo contienen la forma
    assert c.sin_voseo("Tienes que ver el mirador") == "Tienes que ver el mirador"
