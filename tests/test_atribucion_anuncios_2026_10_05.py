"""Atribución de anuncios Meta (2026-10-05):
- CAPI manda ctwa_clid en su campo propio + WABA (no disfrazado de fbc).
- meta_referrals guarda source_url → plataforma y el referral completo.
- citas_bot queda con el anuncio que la trajo (último clic ≤90d).
- meta_insights_snapshot guarda los desgloses diarios.
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import session  # noqa: E402

session.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c)
    _c.commit()

import meta_capi  # noqa: E402
import meta_insights_snapshot as mis  # noqa: E402

FALLAS = []


def check(nombre, cond):
    print(("OK  " if cond else "FAIL") + " " + nombre)
    if not cond:
        FALLAS.append(nombre)


# ── plataforma desde source_url ─────────────────────────────────────────────
P = session._plataforma_desde_url
check("fb.me → facebook", P("https://fb.me/2abc") == "facebook")
check("instagram → instagram", P("https://www.instagram.com/p/XYZ/") == "instagram")
check("vacío → ''", P("") == "")
check("dominio raro → ''", P("https://example.com") == "")

# ── referral completo + plataforma ──────────────────────────────────────────
TEL = "56911111111"
session.save_meta_referral(TEL, {"source_url": "https://www.instagram.com/p/X/",
                                 "source_id": "AD1", "headline": "No dejes",
                                 "ctwa_clid": "ARclid", "campo_nuevo": 7})
with session.db() as c:
    r = c.execute("SELECT plataforma, raw_json FROM meta_referrals WHERE phone=?",
                  (TEL,)).fetchone()
check("referral guarda plataforma", r["plataforma"] == "instagram")
check("referral guarda JSON completo", json.loads(r["raw_json"])["campo_nuevo"] == 7)

# ── cita queda atribuida al anuncio ─────────────────────────────────────────
session.save_cita_bot(TEL, "C1", "medicina general", "Dr X", "2026-10-10",
                      "10:00", "presencial")
session.save_cita_bot("56922222222", "C2", "medicina general", "Dr X",
                      "2026-10-10", "11:00", "presencial")
with session.db() as c:
    c1 = c.execute("SELECT * FROM citas_bot WHERE id_cita='C1'").fetchone()
    c2 = c.execute("SELECT * FROM citas_bot WHERE id_cita='C2'").fetchone()
check("cita con anuncio → ad_source_id", c1["ad_source_id"] == "AD1")
check("cita con anuncio → plataforma", c1["ad_plataforma"] == "instagram")
check("cita sin anuncio → NULL", c2["ad_source_id"] is None)
check("clic >90d no atribuye",
      session.ultimo_referral_antes_de(TEL, int(time.time()) + 91 * 86400) is None)

# ── CAPI: ctwa_clid en su campo ─────────────────────────────────────────────
enviados = []


class _R:
    status_code = 200

    def json(self):
        return {"events_received": 1}


class _C:
    async def post(self, url, json):
        enviados.append(json)
        return _R()


meta_capi._get_client = lambda: _C()
meta_capi._cfg = lambda: ("PIX", "TOK", "")
meta_capi._waba_id = lambda: "WABA"
asyncio.run(meta_capi.send_event("Purchase", "56933333333", ctwa_clid="ARz", value=1))
asyncio.run(meta_capi.send_event("Lead", "56933333333", fbclid="IwAR",
                                 action_source="website"))
ud = enviados[0]["data"][0]["user_data"]
check("CAPI ctwa_clid propio", ud.get("ctwa_clid") == "ARz")
check("CAPI WABA", ud.get("whatsapp_business_account_id") == "WABA")
check("CAPI sin fbc falso", "fbc" not in ud)
check("CAPI web conserva fbc",
      enviados[1]["data"][0]["user_data"].get("fbc", "").endswith(".IwAR"))

# ── snapshot de insights ────────────────────────────────────────────────────


class _HR:
    status_code = 200

    def __init__(self, bd):
        fila = {"ad_id": "AD1", "campaign_name": "No dejes", "spend": "1000",
                "impressions": "500", "reach": "300", "frequency": "1.6",
                "clicks": "20", "actions": [
                    {"action_type": mis._CONV, "value": "4"}]}
        if "publisher_platform" in bd:
            fila["publisher_platform"] = "instagram"
        if "platform_position" in bd:
            fila["platform_position"] = "reels"
        self._b = {"data": [fila]}

    def json(self):
        return self._b


class _HC:
    def get(self, url, params=None, headers=None, timeout=None):
        check_token = "access_token" not in (params or {})
        assert check_token, "token no debe ir en la URL"
        return _HR((params or {}).get("breakdowns", ""))


mis._cfg = lambda: ("TOK", "act_1")
n = mis.snapshot_dia("2026-10-01", client=_HC())
with session.db() as c:
    fila = c.execute("""SELECT * FROM meta_insights_diario
                        WHERE desglose='ubicacion'""").fetchone()
check("snapshot: 1 fila por desglose", n == len(mis.DESGLOSES))
check("snapshot: ubicación", fila["valor"] == "instagram|reels")
check("snapshot: conversaciones", fila["conversaciones"] == 4)
mis.snapshot_dia("2026-10-01", client=_HC())
with session.db() as c:
    tot = c.execute("SELECT COUNT(*) FROM meta_insights_diario").fetchone()[0]
check("snapshot: re-correr no duplica", tot == len(mis.DESGLOSES))

print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
