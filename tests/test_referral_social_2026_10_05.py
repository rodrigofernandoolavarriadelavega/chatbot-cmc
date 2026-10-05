"""Referral de anuncios Click-to-Messenger / Instagram Direct (2026-10-05).
DB temporal, datos sintéticos. Ejecutar: python tests/test_referral_social_2026_10_05.py"""
import json, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import session  # noqa: E402

session.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c)
    _c.commit()

import campanas_meta_routes as cm  # noqa: E402

F = []
def check(n, c):
    print(("OK  " if c else "FAIL") + " " + n)
    if not c: F.append(n)

ADS = {"source": "ADS", "type": "OPEN_THREAD", "ad_id": "6001", "ref": "promo",
       "ads_context_data": {"ad_title": "Chequeo", "photo_url": "http://x/p.jpg", "post_id": "9"}}
N = session.normalizar_referral_social

# normalización
ev = {"sender": {"id": "111"}, "referral": ADS}
n = N(ev, "messenger")
check("messenger referral", n and n["source_id"] == "6001" and n["headline"] == "Chequeo" and n["plataforma"] == "facebook")
n = N({"referral": ADS}, "instagram")
check("ig referral plataforma", n and n["plataforma"] == "instagram" and n["media_type"] == "image")
check("postback.referral", N({"postback": {"payload": "GET_STARTED", "referral": ADS}}, "messenger")["source_id"] == "6001")
check("message.referral", N({"message": {"text": "hola", "referral": ADS}}, "instagram")["source_id"] == "6001")
check("sin referral -> None", N({"message": {"text": "hola"}}, "messenger") is None)
check("shortlink no anuncio -> None", N({"referral": {"source": "SHORTLINK", "type": "OPEN_THREAD", "ref": "x"}}, "messenger") is None)

# guardado + atribución
check("captura fb", session.capturar_referral_social("fb_27203774389301304", ev, "messenger"))
check("dedup 10 min", not session.capturar_referral_social("fb_27203774389301304", ev, "messenger"))
check("captura ig", session.capturar_referral_social("ig_555", {"referral": ADS}, "instagram"))
with session.db() as c:
    r = c.execute("SELECT * FROM meta_referrals WHERE phone='fb_27203774389301304'").fetchone()
check("fila fb canal/plataforma/raw", r["canal"] == "messenger" and r["plataforma"] == "facebook"
      and json.loads(r["raw_json"])["ad_id"] == "6001")
a = session.ultimo_referral_antes_de("fb_27203774389301304", int(time.time()) + 5)
check("atribuye fb por id completo", a and a["source_id"] == "6001")
check("ig atribuye", session.ultimo_referral_antes_de("ig_555", int(time.time()) + 5)["plataforma"] == "instagram")
# colisión: WhatsApp con mismos últimos 9 dígitos que el id fb no debe heredar
check("WA con sufijo igual NO hereda", session.ultimo_referral_antes_de("+56389301304", int(time.time()) + 5) is None)
check("fb distinto no hereda", session.ultimo_referral_antes_de("fb_999", int(time.time()) + 5) is None)
check("get_meta_referral_fresh fb", (session.get_meta_referral_fresh("fb_27203774389301304") or {}).get("source_id") == "6001")

# WhatsApp intacto
session.save_meta_referral("56911112222", {"source_id": "w1", "headline": "H", "source_type": "ad",
                           "ctwa_clid": "c", "source_url": "https://fb.me/x"}, canal="whatsapp")
w = session.ultimo_referral_antes_de("+56 9 1111 2222", int(time.time()) + 5)
check("WA intacto", w and w["source_id"] == "w1" and w["plataforma"] == "facebook")
check("clave panel fb/ig tal cual", cm._clave("fb_27203774389301304") == "fb_27203774389301304" and cm._clave("ig_555") == "ig_555")
print("FALLAS:", F); sys.exit(1 if F else 0)
