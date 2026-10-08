"""
Dr. Paz (81): si el paciente vio horas y no respondió, el reenganche ofrece
las alternativas con bono Fonasa (médico general / nutricionista) en vez del
genérico. No se avisa antes de mostrar horas (decisión del dueño 2026-10-08).
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_pazr_")) / "s.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session  # noqa: E402
session.DB_PATH = TMP_DB
import jobs  # noqa: E402
import flows  # noqa: E402

PHONE = "56955553333"


def _sesion(esp):
    with session.db() as c:
        c.execute("DELETE FROM sessions"); c.execute("DELETE FROM conversation_events")
        upd = (datetime.now(timezone.utc) - timedelta(minutes=40)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT OR REPLACE INTO sessions (phone, state, data, updated_at) VALUES (?,?,?,?)",
                  (PHONE, "WAIT_SLOT", json.dumps({"especialidad": esp, "nombre_conocido": "Leticia Soto"}), upd))
        c.commit()


def _run():
    enviados = []

    async def fake(phone, interactive):
        enviados.append(interactive)
    with mock.patch.object(jobs, "send_whatsapp_interactive", new=AsyncMock(side_effect=fake)), \
         mock.patch.object(jobs, "is_medilink_down", lambda: True), \
         mock.patch.object(jobs, "verificar_cita_externa", new=AsyncMock(return_value="sin_rut")):
        asyncio.run(jobs._enviar_reenganche())
    return enviados


def test_reenganche_paz_ofrece_alternativas_fonasa():
    _sesion("nutriología y diabetología")
    env = _run()
    assert len(env) == 1
    body = env[0]["body"]["text"]
    assert "Dr. Raúl Paz" in body and "Fonasa" in body and "$60.000" in body
    ids = [b["reply"]["id"] for b in env[0]["action"]["buttons"]]
    assert ids == ["xpaz_si", "xpaz_mg", "xpaz_nutri"]


def test_otra_especialidad_sigue_generico():
    _sesion("medicina general")
    env = _run()
    assert len(env) == 1 and "Dr. Raúl Paz" not in env[0]["body"]["text"]


def test_botones_llevan_a_cada_agenda(monkeypatch):
    ini = AsyncMock(return_value="OK")
    monkeypatch.setattr(flows, "_iniciar_agendar", ini)
    for tl, esp in (("xpaz_si", "nutriología y diabetología"),
                    ("xpaz_mg", "medicina general"), ("xpaz_nutri", "nutrición")):
        asyncio.run(flows.handle_message(PHONE, tl, {"state": "WAIT_SLOT", "data": {}}))
        assert ini.await_args.args[2] == esp
