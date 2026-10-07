"""Regresión — mensaje perdido por dedup tras falla (disco lleno 2026-10-05).

Caso real: 12:15 un paciente pidió licencia por fallecimiento de un familiar.
El primer intento marcó el wamid en processed_msgs, reventó en log_message
("database or disk is full") → 500. Meta reenvió 1 s después y el bot lo
descartó como "MSG duplicado ignorado": nadie lo vio, ni el bot ni recepción.

Uso:
  PYTHONPATH=app:. venv/bin/python tests/test_dedup_reintento_2026_10_05.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
TMP_DB = Path(tempfile.mkdtemp()) / "test_dedup_reintento.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import main  # noqa: E402
import session  # noqa: E402
from messaging import _respuesta_enviada_var  # noqa: E402


def _wamid(nombre: str) -> str:
    # session.py no respeta SESSIONS_DB (usa data/sessions.db): ids únicos por
    # corrida para que una corrida anterior no los deje "ya procesados".
    return f"wamid.test.{nombre}.{uuid.uuid4().hex}"


class DedupReintento(unittest.TestCase):
    def _webhook_con(self, procesar):
        orig = main._webhook_procesar
        main._webhook_procesar = procesar
        try:
            return asyncio.run(main.webhook(None))
        finally:
            main._webhook_procesar = orig

    def test_falla_sin_responder_permite_reintento(self):
        wamid = _wamid("caso_licencia_duelo")

        async def revienta(_req):
            assert session.is_duplicate(wamid) is False
            raise RuntimeError("database or disk is full")

        with self.assertRaises(RuntimeError):
            self._webhook_con(revienta)
        # El reenvío de Meta tiene que procesarse, no caer en "duplicado".
        self.assertFalse(session.is_duplicate(wamid))
        # Y un tercer envío ya sí es duplicado (idempotencia intacta).
        self.assertTrue(session.is_duplicate(wamid))

    def test_falla_despues_de_responder_no_reprocesa(self):
        wamid = _wamid("ya_contestado")

        async def responde_y_revienta(_req):
            assert session.is_duplicate(wamid) is False
            _respuesta_enviada_var.get()["enviado"] = True  # lo que hace _post_meta
            raise RuntimeError("falla después de contestar")

        with self.assertRaises(RuntimeError):
            self._webhook_con(responde_y_revienta)
        self.assertTrue(session.is_duplicate(wamid))

    def test_exito_no_desmarca(self):
        wamid = _wamid("normal")

        async def ok(_req):
            session.is_duplicate(wamid)
            return "ok"

        self.assertEqual(self._webhook_con(ok), "ok")
        self.assertTrue(session.is_duplicate(wamid))

    def test_delete_falla_igual_permite_reintento_en_memoria(self):
        wamid = _wamid("disco_lleno_total")
        session.is_duplicate(wamid)
        orig_db = session.db

        def db_rota():
            raise RuntimeError("database or disk is full")

        session.db = db_rota
        try:
            session.desmarcar_procesados([wamid])  # no debe lanzar
        finally:
            session.db = orig_db
        self.assertFalse(session.is_duplicate(wamid))


if __name__ == "__main__":
    unittest.main(verbosity=2)
