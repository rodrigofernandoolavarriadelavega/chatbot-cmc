"""FIX G (consolidado mensual #14): enmascarar RUT en los logs de ARCHIVO
(/var/log/cmc-bot.log). NO toca log_message (va a sessions.db, cifrada con
SQLCipher, y la usa el panel de recepción — necesita el RUT en claro).

`session._scrub_pii` ya existía (FIX-18) pero solo se usaba en un sitio de
main.py; el resto de los `log.info(...texto...)` de IG/Messenger/audio/batch
mandaban el texto del paciente crudo al log de archivo. Un paciente que
escribe "mi rut es 12.345.678-9, agenda para mañana" quedaba con el RUT en
texto plano en /var/log/cmc-bot.log.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

from session import _scrub_pii  # noqa: E402

MAIN_PY = (ROOT / "app" / "main.py").read_text()


# ── Unit: el helper enmascara RUT de verdad ──────────────────────────────────

def test_scrub_pii_enmascara_rut_con_puntos():
    out = _scrub_pii("mi rut es 12.345.678-9, agenda para mañana")
    assert "12.345.678-9" not in out
    assert "***" in out
    assert out.endswith("-9") or "-9" in out  # conserva el DV para diagnóstico


def test_scrub_pii_enmascara_rut_sin_puntos():
    out = _scrub_pii("rut 12345678-9 agendame")
    assert "12345678-9" not in out


def test_scrub_pii_no_toca_texto_sin_rut():
    texto = "quiero hora para kinesiología mañana en la tarde"
    assert _scrub_pii(texto) == texto


# ── Estructural: los call sites reales de main.py pasan por _scrub_pii ──────
# (no basta con que el helper funcione — hay que evitar que un log nuevo se
# agregue crudo al lado sin pasar por acá, o que un fix se revierta sin querer)

def test_import_scrub_pii_en_main():
    assert "_scrub_pii" in MAIN_PY
    assert "from session import" in MAIN_PY


def _linea_de(marcador: str) -> str:
    idx = MAIN_PY.index(marcador)
    inicio = MAIN_PY.rfind("\n", 0, idx) + 1
    fin = MAIN_PY.find("\n", idx)
    return MAIN_PY[inicio:fin]


def test_log_instagram_usa_scrub_pii():
    linea = _linea_de('log.info("INSTAGRAM from=%s name=%r text=%r sender=%s",')
    siguiente = MAIN_PY[MAIN_PY.index(linea):MAIN_PY.index(linea) + 200]
    assert "_scrub_pii(texto" in siguiente


def test_log_messenger_usa_scrub_pii():
    linea = _linea_de('log.info("MESSENGER from=%s sender=%s text=%r",')
    siguiente = MAIN_PY[MAIN_PY.index(linea):MAIN_PY.index(linea) + 200]
    assert "_scrub_pii(texto" in siguiente


def test_log_audio_transcrito_usa_scrub_pii():
    assert '_scrub_pii(texto[:120])' in MAIN_PY


def test_log_msg_whatsapp_principal_usa_scrub_pii():
    assert '_scrub_pii(texto[:100])' in MAIN_PY


def test_log_batch_extra_usa_scrub_pii():
    assert '_scrub_pii(_xtxt[:80])' in MAIN_PY


def test_log_takeover_rescate_usa_scrub_pii():
    assert '_scrub_pii((texto or "")[:40])' in MAIN_PY


def test_logs_de_bot_reply_tambien_enmascarados():
    # Defensa extra: la respuesta del bot en teoría no debería llevar RUT,
    # pero si algún flujo lo hiciera, el log de archivo no debe exponerlo.
    assert MAIN_PY.count("_scrub_pii(resp_text[:80])") >= 2


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
