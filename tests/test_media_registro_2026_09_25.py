"""test_media_registro_2026_09_25.py — pedido del dueño: el panel de recepción
(CMC y Capital Travel) debe MOSTRAR las imágenes de las campañas, no solo el
texto. Cubre el mecanismo de registro (`messages.media_url`/`media_tipo`) en
los puntos donde se envía/recibe una imagen:

  1. Migración idempotente (columnas nuevas + tabla `admin_sent_media`).
  2. `log_message`/`get_messages` guardan y devuelven media_url/media_tipo.
  3. Imagen saliente vía `messaging.send_whatsapp_image(log=True)`.
  4. Plantilla con header IMAGE (caso real: flyer dental winback) — un
     call site end a end con los guardas bypaseados.
  5. Interactivo con header IMAGE — capital_demo (flyer "PLOMO"), local +
     espejo a Alma Capital.
  6. Endpoint `/admin/api/file/{id}` (fotos entrantes) y
     `/admin/api/sent-media/{id}` (envíos ad-hoc del panel): auth requerida,
     404 si no existe, 200 + bytes correctos si existe.

Correr: PYTHONPATH=app:. venv/bin/python tests/test_media_registro_2026_09_25.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

os.environ.setdefault("SQLCIPHER_KEY", "")

import session  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
session.DB_PATH = Path(_tmp.name)

import capital_demo  # noqa: E402
import messaging  # noqa: E402
import admin_routes  # noqa: E402

PHONE = "56911112222"
PHONE_DEMO = "56912345678"


class TestMigracionYColumnas(unittest.TestCase):
    def test_columnas_media_existen_en_messages(self):
        with session.db() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        self.assertIn("media_url", cols)
        self.assertIn("media_tipo", cols)

    def test_tabla_admin_sent_media_existe(self):
        with session.db() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='admin_sent_media'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_migracion_es_idempotente(self):
        """Correr la DDL dos veces no debe explotar (ALTER TABLE repetido)."""
        with session.db() as conn:
            session._run_ddl_inline(conn)
            session._run_ddl_inline(conn)  # segunda vez, no debe lanzar


class TestLogMessageMedia(unittest.TestCase):
    def test_log_message_persiste_media_url_y_tipo(self):
        session.log_message(PHONE, "out", "[imagen] promo", "IDLE",
                            media_url="https://agentecmc.cl/static/promos/x.jpg",
                            media_tipo="image")
        msgs = session.get_messages(PHONE)
        ultimo = msgs[-1]
        self.assertEqual(ultimo["media_url"], "https://agentecmc.cl/static/promos/x.jpg")
        self.assertEqual(ultimo["media_tipo"], "image")

    def test_log_message_sin_media_queda_null(self):
        session.log_message(PHONE, "in", "hola", "IDLE")
        msgs = session.get_messages(PHONE)
        ultimo = msgs[-1]
        self.assertIsNone(ultimo["media_url"])
        self.assertIsNone(ultimo["media_tipo"])


class TestImagenSaliente(unittest.IsolatedAsyncioTestCase):
    async def test_send_whatsapp_image_log_true_persiste_media_url(self):
        with patch.object(messaging, "_post_meta", new=AsyncMock(return_value="wamid-img-1")):
            wamid = await messaging.send_whatsapp_image(
                PHONE, "https://agentecmc.cl/static/promos/flyer.jpg",
                caption="Promo", log=True, log_state="IDLE",
            )
        self.assertEqual(wamid, "wamid-img-1")
        ultimo = session.get_messages(PHONE)[-1]
        self.assertEqual(ultimo["media_url"], "https://agentecmc.cl/static/promos/flyer.jpg")
        self.assertEqual(ultimo["media_tipo"], "image")
        self.assertEqual(ultimo["wamid"], "wamid-img-1")
        # El texto ya NO trae la URL pegada (ahora vive en su propia columna).
        self.assertNotIn("https://", ultimo["text"])


class TestPlantillaConImagenDentalWinback(unittest.IsolatedAsyncioTestCase):
    """Caso real: dental_winback.send_dental_winback con DENTAL_PROMO_FLYER_ACTIVE
    manda el flyer (template header IMAGE) en vez de texto plano. Verifica que
    el log queda con media_url=DENTAL_PROMO_FLYER_IMG — la plantilla real que
    usa `_lm(..., media_url=..., media_tipo=...)`."""

    async def test_envio_dental_con_flyer_registra_media_url(self):
        import dental_winback as dw
        import config as cfg

        candidato = {
            "telefono": PHONE, "paciente_id": 1, "nombre": "Paciente Test",
            "ultima_especialidad": "odontologia general", "ultimo_profesional": "Dra. Burgos",
            "subcohorte": "dental_odonto_general_180d",
        }

        # dental_winback.py hace `import config as _cfg_wb` DENTRO de la función
        # (import perezoso) — parcheamos el atributo en el módulo `config` real,
        # que es lo que ese import fresco va a leer al llamar send_dental_winback.
        with patch.object(cfg, "DENTAL_PROMO_FLYER_ACTIVE", True), \
             patch.object(dw, "ya_enviado_dental_winback_hoy", return_value=False), \
             patch.object(dw, "phone_in_dental_opt_out", return_value=False), \
             patch.object(dw, "has_dental_consent", return_value=True), \
             patch.object(dw, "_registrar_envio_dental", return_value=None), \
             patch("contact_budget.can_contact", return_value=(True, "ok")), \
             patch("contact_budget.record_contact", return_value=None), \
             patch("messaging.send_whatsapp_template", new=AsyncMock(return_value="wamid-tmpl")), \
             patch("session.save_campana_envio", return_value=None), \
             patch("session.log_message") as lm:
            ok = await dw.send_dental_winback(candidato)

        self.assertTrue(ok)
        lm.assert_called_once()
        args, kwargs = lm.call_args
        self.assertEqual(args[0], PHONE)
        self.assertEqual(args[1], "out")
        self.assertEqual(kwargs.get("media_url"), cfg.DENTAL_PROMO_FLYER_IMG)
        self.assertEqual(kwargs.get("media_tipo"), "image")


class TestInteractivoConImagenCapitalDemo(unittest.IsolatedAsyncioTestCase):
    """El flyer de primer contacto (capital_demo) es un interactivo con
    header IMAGE. Debe quedar registrado local Y espejado con media_url."""

    def setUp(self):
        self._env_bak = dict(os.environ)
        os.environ["CAPITAL_DEMO_PHONES"] = PHONE_DEMO
        os.environ["CAPITAL_DEMO_HASTA"] = "2099-01-01"
        os.environ["CAPITAL_WA_SECRET"] = "secreto-test"
        capital_demo._historial.clear()
        capital_demo._historial_ts.clear()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._enviados_bak = capital_demo._ENVIADOS_PATH
        capital_demo._ENVIADOS_PATH = Path(self._tmpdir.name) / "enviados.json"
        capital_demo._enviados_mem = None
        self._humano_bak = capital_demo._HUMANO_PATH
        capital_demo._HUMANO_PATH = Path(self._tmpdir.name) / "humano.json"
        capital_demo._humano_mem = None

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_bak)
        capital_demo._ENVIADOS_PATH = self._enviados_bak
        capital_demo._enviados_mem = None
        capital_demo._HUMANO_PATH = self._humano_bak
        capital_demo._humano_mem = None
        self._tmpdir.cleanup()

    def test_extrae_media_url_del_header_de_imagen(self):
        msg = capital_demo._msg_flyer_plomo("Juan")
        url = capital_demo._media_url_de_interactivo(msg)
        self.assertEqual(url, capital_demo._FLYER_IMG_URL)

    def test_interactivo_sin_header_imagen_no_da_media_url(self):
        msg = capital_demo._msg_boton_plomo()
        self.assertIsNone(capital_demo._media_url_de_interactivo(msg))

    async def test_primer_contacto_registra_y_espeja_media_url_del_flyer(self):
        with patch("session.log_message") as lm, \
             patch("messaging.send_whatsapp_interactive", new=AsyncMock()), \
             patch.object(capital_demo, "_espejo_post", new=AsyncMock()) as ep:
            await capital_demo.manejar_webhook_wa(
                PHONE_DEMO, {"type": "text", "text": {"body": "hola"}}, "text"
            )
            import asyncio
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        # session.log_message: la llamada del flyer trae media_url.
        flyer_calls = [c for c in lm.call_args_list
                      if c.kwargs.get("media_url") == capital_demo._FLYER_IMG_URL]
        self.assertTrue(flyer_calls, "ninguna llamada a log_message trajo el media_url del flyer")

        # Espejo: el payload del flyer también trae media_url.
        flyer_espejo = [c for c in ep.await_args_list
                        if c.args[0].get("media_url") == capital_demo._FLYER_IMG_URL]
        self.assertTrue(flyer_espejo, "el espejo del flyer no incluyó media_url")


class MediaEndpointsBase(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(admin_routes.router)
        self.client = TestClient(app)
        from config import ADMIN_TOKEN
        self.token = ADMIN_TOKEN

        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmpdir.cleanup()
        import shutil
        shutil.rmtree(ROOT / "data" / "uploads" / PHONE, ignore_errors=True)


class TestEndpointFile(MediaEndpointsBase):
    def test_sin_token_da_401(self):
        r = self.client.get("/admin/api/file/1")
        self.assertEqual(r.status_code, 401)

    def test_id_inexistente_da_404(self):
        r = self.client.get("/admin/api/file/999999", params={"token": self.token})
        self.assertEqual(r.status_code, 404)

    def test_archivo_existente_sirve_bytes_correctos(self):
        fpath = Path(self._tmpdir.name)
        (ROOT / "data" / "uploads" / PHONE).mkdir(parents=True, exist_ok=True)
        real_path = ROOT / "data" / "uploads" / PHONE / "test_file_endpoint.jpg"
        real_path.write_bytes(b"\xff\xd8\xff fake-jpeg-bytes")
        try:
            file_id = session.save_patient_file(
                PHONE, "test_file_endpoint.jpg", "image", "image/jpeg",
                f"data/uploads/{PHONE}/test_file_endpoint.jpg", real_path.stat().st_size,
            )
            r = self.client.get(f"/admin/api/file/{file_id}", params={"token": self.token})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.content, b"\xff\xd8\xff fake-jpeg-bytes")
            self.assertEqual(r.headers["content-type"], "image/jpeg")
        finally:
            real_path.unlink(missing_ok=True)


class TestEndpointSentMedia(MediaEndpointsBase):
    def test_sin_token_da_401(self):
        r = self.client.get("/admin/api/sent-media/1")
        self.assertEqual(r.status_code, 401)

    def test_id_inexistente_da_404(self):
        r = self.client.get("/admin/api/sent-media/999999", params={"token": self.token})
        self.assertEqual(r.status_code, 404)

    def test_archivo_existente_sirve_bytes_correctos(self):
        (ROOT / "data" / "uploads" / PHONE).mkdir(parents=True, exist_ok=True)
        real_path = ROOT / "data" / "uploads" / PHONE / "test_sent_media.png"
        real_path.write_bytes(b"\x89PNG fake-png-bytes")
        try:
            sent_id = session.save_admin_sent_media(
                PHONE, "test_sent_media.png", "image/png",
                f"data/uploads/{PHONE}/test_sent_media.png", real_path.stat().st_size,
            )
            r = self.client.get(f"/admin/api/sent-media/{sent_id}", params={"token": self.token})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.content, b"\x89PNG fake-png-bytes")
            self.assertEqual(r.headers["content-type"], "image/png")
        finally:
            real_path.unlink(missing_ok=True)


class TestSendDocumentRegistraMedia(unittest.TestCase):
    """POST /admin/api/send-document con una imagen: sube a Meta, guarda copia
    local y registra media_url apuntando a /admin/api/sent-media/{id}."""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(admin_routes.router)
        self.client = TestClient(app)
        from config import ADMIN_TOKEN
        self.token = ADMIN_TOKEN

    def tearDown(self):
        import shutil
        shutil.rmtree(ROOT / "data" / "uploads" / PHONE, ignore_errors=True)

    def test_envio_imagen_registra_media_url_apuntando_a_sent_media(self):
        with patch("messaging.upload_media_to_whatsapp", new=AsyncMock(return_value="meta-media-1")), \
             patch("messaging.send_whatsapp_image_by_id", new=AsyncMock(return_value="wamid-doc-1")), \
             patch.object(admin_routes, "log_message") as lm:
            r = self.client.post(
                "/admin/api/send-document",
                params={"token": self.token},
                data={"phone": PHONE, "caption": "foto de control"},
                files={"file": ("foto.jpg", b"\xff\xd8\xff fake", "image/jpeg")},
            )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        lm.assert_called_once()
        args, kwargs = lm.call_args
        self.assertEqual(args[0], PHONE)
        self.assertTrue(kwargs.get("media_url", "").startswith("/admin/api/sent-media/"))
        self.assertEqual(kwargs.get("media_tipo"), "image")

        # El endpoint de esa media_url sirve el archivo real.
        sent_url = kwargs["media_url"]
        r2 = self.client.get(sent_url, params={"token": self.token})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.content, b"\xff\xd8\xff fake")


if __name__ == "__main__":
    unittest.main()
