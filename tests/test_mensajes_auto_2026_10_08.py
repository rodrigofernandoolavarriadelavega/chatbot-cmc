"""Resultados de mensajes automáticos para Campañas Meta (2026-10-08)."""
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_ma_")) / "s.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")
try:
    import winback  # noqa: F401
except Exception:
    sys.modules.setdefault("winback", types.ModuleType("winback"))
import winback  # noqa: E402

import session  # noqa: E402
session.DB_PATH = TMP_DB
import mensajes_auto  # noqa: E402


def test_mide_llegada_respuesta_y_agenda(monkeypatch):
    def _bi_caido():
        raise RuntimeError("sin BI")
    monkeypatch.setattr(winback, "bi_conn", _bi_caido, raising=False)
    with session.db() as c:
        for t in ("fidelizacion_msgs", "messages", "message_statuses", "citas_bot", "conversation_events"):
            c.execute(f"DELETE FROM {t}")
        c.execute("INSERT INTO fidelizacion_msgs (phone, tipo, enviado_en) VALUES ('56911111111','crosssell_kine', datetime('now','-2 days'))")
        c.execute("INSERT INTO fidelizacion_msgs (phone, tipo, enviado_en) VALUES ('56922222222','crosssell_kine', datetime('now','-2 days'))")
        c.execute("INSERT INTO message_statuses (wamid, phone, status, ts) VALUES ('w1','56911111111','read', datetime('now','-2 days','+1 minutes'))")
        c.execute("INSERT INTO message_statuses (wamid, phone, status, ts, error_code) VALUES ('w2','56922222222','failed', datetime('now','-2 days','+1 minutes'), '131047')")
        c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES ('56911111111','in','sí', 'IDLE', datetime('now','-2 days','+2 hours'))")
        c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, created_at) VALUES ('56911111111','1','Kinesiología','Leo','2026-10-10','10:00', datetime('now','-1 days'))")
        c.commit()
    mensajes_auto._CACHE.clear()
    r = mensajes_auto.resumen(30)
    f = next(x for x in r["filas"] if x["key"] == "crosssell_kine")
    assert (f["enviados"], f["llegaron"], f["fallaron"], f["respondieron"], f["agendaron"]) == (2, 1, 1, 1, 1)
    assert f["error_top"] == "131047" and r["bi_error"] is True


def test_oferta_limpieza_lleva_imagen_que_el_panel_registra():
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert 'media_url=_hdr_img, media_tipo="image" if _hdr_img else None' in src
