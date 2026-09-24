"""
Regresión — portaviones consolidado 2026-09-24, problema #15 (parte
ortodoncia "sin necesidad de viajar a la ciudad").

Casos reales: 56946271011 y fb_29308667732068666 (ambos "Quiero más
información" llegados desde un anuncio Meta de ortodoncia con headline
"Brackets sin viajar $120K", confirmado en logs con
`META_REFERRAL WA capturado ... headline='Brackets sin viajar $120K'`).
El bot respondió con el pitch de ortodoncia usando la frase "todo aquí en
Carampangue — sin necesidad de viajar a la ciudad".

Root cause: NO es un template fijo en el código (no hay match de
"sin necesidad de viajar"/"brackets sin viajar" en app/*.py) — el mecanismo
real es que `detect_intent`/`respuesta_faq` (app/claude_helper.py) inyectan
el headline del anuncio TEXTUAL en el prompt de Claude
(`meta_referral['headline']`), y Claude lo repite/expande en su respuesta.
El headline es copy publicitario (Meta Ads), no un hecho verificado por el
bot — no debería citarse literal en una respuesta al paciente.

NOTA: el mensaje de derivación "Hospital de Arauco / clínicas de
Concepción" para servicios que el CMC no tiene (ej. cirugía vascular,
ejemplo fb_29308667732068666) es DISEÑO DELIBERADO (commit 45907e4,
decisión del dueño) — no se toca.

Fix: agregar una instrucción explícita en el bloque de contexto del
referral (los dos puntos donde se inyecta `meta_referral['headline']`) para
que Claude NO cite/repita el eslogan textual del anuncio.

Como la respuesta real depende del LLM (no determinística), este test
verifica el MECANISMO: que el prompt que efectivamente se envía a Claude
cuando hay un referral con headline contiene la instrucción anti-parroteo.
No hace una llamada real a la API (mockeada).

Ejecución:
    python tests/test_portaviones_2026_09_24_referral_no_parrot.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import claude_helper  # noqa: E402

FAILS = 0


def check_true(label: str, cond: bool):
    global FAILS
    print(f"{'OK  ' if cond else 'FAIL'} {label}")
    if not cond:
        FAILS += 1


class _FakeContentBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeResp:
    def __init__(self, text: str):
        self.content = [_FakeContentBlock(text)]
        self.stop_reason = "end_turn"
        self.usage = None


GUARD_PHRASE = "NO lo cites ni repitas"
HEADLINE = "Brackets sin viajar $120K"


async def _run():
    captured = {}

    async def _fake_claude_create(**kwargs):
        captured["messages"] = kwargs.get("messages")
        # detect_intent hace prefill con "{" y espera JSON válido
        return _FakeResp('"intent": "info", "respuesta_directa": "ok"}')

    with patch.object(claude_helper, "_claude_create", side_effect=_fake_claude_create), \
         patch.object(claude_helper, "_horarios_vivos", new=AsyncMock(return_value="")):

        # ── detect_intent ──
        captured.clear()
        await claude_helper.detect_intent(
            "¡Hola! Quiero más información",
            meta_referral={"headline": HEADLINE},
        )
        _sent = captured.get("messages")
        check_true("detect_intent_llamo_a_claude", _sent is not None)
        _user_content = _sent[0]["content"] if _sent else ""
        check_true(
            "detect_intent_incluye_headline",
            HEADLINE in _user_content,
        )
        check_true(
            "detect_intent_incluye_guardrail_anti_parroteo",
            GUARD_PHRASE in _user_content,
        )

        # ── respuesta_faq ──
        captured.clear()

        async def _fake_claude_create_faq(**kwargs):
            captured["messages"] = kwargs.get("messages")
            return _FakeResp('{"respuesta_directa": "ok"}')

        with patch.object(claude_helper, "_claude_create", side_effect=_fake_claude_create_faq):
            await claude_helper.respuesta_faq(
                "cuéntame más sobre eso porfa",
                meta_referral={"headline": HEADLINE},
            )
        _sent2 = captured.get("messages")
        check_true("respuesta_faq_llamo_a_claude", _sent2 is not None)
        _user_content2 = _sent2[0]["content"] if _sent2 else ""
        check_true(
            "respuesta_faq_incluye_headline",
            HEADLINE in _user_content2,
        )
        check_true(
            "respuesta_faq_incluye_guardrail_anti_parroteo",
            GUARD_PHRASE in _user_content2,
        )


asyncio.run(_run())

print()
if FAILS:
    print(f"{FAILS} fallo(s)")
    sys.exit(1)
print("Todos los casos OK")
