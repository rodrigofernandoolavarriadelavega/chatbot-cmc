"""¿Cómo nos conociste? (2026-10-05): lista de 5 opciones que separa
Facebook/Instagram de Google. Cada respuesta (botón o texto libre) guarda el
tag correcto; el botón antiguo ref_rrss sigue funcionando."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import session  # noqa: E402

session.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c)
    _c.commit()
import flows  # noqa: E402

FALLAS = []
CASOS = [
    ("ref_amigo", "amigo"), ("ref_fbig", "facebook_instagram"), ("ref_google", "google"),
    ("ref_recurrente", "recurrente"), ("ref_calle", "calle"), ("ref_rrss", "rrss"),
    ("lo vi en facebook", "facebook_instagram"), ("por instagram", "facebook_instagram"),
    ("busqué en google", "google"), ("me lo recomendó mi vecina", "amigo"),
    ("vi el letrero", "calle"), ("por la radio", "radio"),
    ("los conocí por facebook", "facebook_instagram"),
]
for i, (txt, esperado) in enumerate(CASOS):
    tel = f"5691234{i:04d}"
    session.save_session(tel, "WAIT_REFERRAL_POST", {})
    asyncio.run(flows.handle_message(tel, txt, {"state": "WAIT_REFERRAL_POST", "data": {}}))
    with session.db() as c:
        tags = [r[0] for r in c.execute("SELECT tag FROM contact_tags WHERE phone=?", (tel,))]
    ok = f"referido:{esperado}" in tags
    print(("OK  " if ok else "FAIL") + f" {txt!r} → {tags}")
    if not ok:
        FALLAS.append(txt)
print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
