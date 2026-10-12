"""Migración Haiku 4.5 -> 5.5: compatibilidad de _claude_create (offline, sin red).

    MEDILINK_TOKEN=x PYTHONPATH=app:. python -m pytest tests/test_claude_modelo_2026_10_11.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import claude_helper as ch  # noqa: E402


def _resp(blocks, stop="end_turn", details=None):
    return NS(content=blocks, stop_reason=stop, stop_details=details,
              usage=NS(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                       cache_creation_input_tokens=0))


class _Cli:
    def __init__(self, resp):
        self.resp, self.kw = resp, None
        self.messages = self

    async def create(self, **kw):
        self.kw = kw
        return self.resp


def _run(model, resp, **kw):
    cli = _Cli(resp)
    old = (ch.client, ch.CLAUDE_MODEL)
    ch.client, ch.CLAUDE_MODEL = cli, model
    try:
        r = asyncio.run(ch._claude_create(model=model, max_tokens=50,
                                          messages=[{"role": "user", "content": "hola"}], **kw))
    finally:
        ch.client, ch.CLAUDE_MODEL = old
    return cli.kw, r


TXT = NS(type="text", text='{"a": 1}')
THK = NS(type="thinking", thinking="...")
SCH = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"],
       "additionalProperties": False}


def test_55_sin_prefill_con_output_config():
    kw, _ = _run("claude-haiku-5-5", _resp([TXT]), schema=SCH, prefill="{")
    assert len(kw["messages"]) == 1 and kw["messages"][-1]["role"] == "user"
    oc = kw["extra_body"]["output_config"]
    assert oc["effort"] == ch.CLAUDE_EFFORT
    assert oc["format"] == {"type": "json_schema", "schema": SCH}
    assert kw["extra_body"]["thinking"] == {"type": "disabled"}
    for prohibido in ("temperature", "top_p", "top_k", "thinking"):
        assert prohibido not in kw


def test_45_conserva_prefill_y_no_manda_output_config():
    kw, _ = _run("claude-haiku-4-5-20251001", _resp([TXT]), schema=SCH, prefill="{")
    assert kw["messages"][-1] == {"role": "assistant", "content": "{"}
    assert "extra_body" not in kw and "schema" not in kw and "prefill" not in kw


def test_resp_text_salta_bloque_thinking():
    assert ch._resp_text(_resp([THK, TXT])) == '{"a": 1}'


def test_resp_text_sin_texto_levanta():
    with pytest.raises(ValueError):
        ch._resp_text(_resp([THK], stop="max_tokens"))


def test_refusal_levanta_claude_refusal():
    with pytest.raises(ch.ClaudeRefusal):
        _run("claude-haiku-5-5", _resp([], stop="refusal", details={"category": "cyber"}))


def test_detect_intent_refusal_cae_al_fallback(monkeypatch):
    cli = _Cli(_resp([], stop="refusal", details={"category": "cyber"}))
    monkeypatch.setattr(ch, "client", cli)
    monkeypatch.setattr(ch, "CLAUDE_MODEL", "claude-haiku-5-5")

    async def _h():
        return ""
    monkeypatch.setattr(ch, "_horarios_vivos", _h)
    r = asyncio.run(ch.detect_intent("necesito saber algo raro que no esta en cache zzqx"))
    assert r["intent"] in ("menu", "info")


def test_detect_intent_parsea_55_sin_prefill(monkeypatch):
    body = NS(type="text", text='{"intent": "agendar", "especialidad": "kinesiología", "respuesta_directa": null}')
    cli = _Cli(_resp([THK, body]))
    monkeypatch.setattr(ch, "client", cli)
    monkeypatch.setattr(ch, "CLAUDE_MODEL", "claude-haiku-5-5")

    async def _h():
        return ""
    monkeypatch.setattr(ch, "_horarios_vivos", _h)
    r = asyncio.run(ch.detect_intent("zzqx quiero algo muy particular con la rodilla derecha mañana"))
    assert r["intent"] == "agendar" and r["especialidad"] == "kinesiología"
    assert cli.kw["extra_body"]["output_config"]["format"]["schema"] == ch._SCHEMA_DETECT_INTENT
