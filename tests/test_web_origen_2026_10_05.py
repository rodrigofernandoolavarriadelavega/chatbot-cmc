"""Marcador web extendido (2026-10-05): "(web: página · artículo · botón)".
Compatibilidad con "(web)", "(web: home)" y marcador al inicio; cada llegada
registra web_origen; el marcador se limpia del texto."""
import asyncio
import json
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

capt = {}
_orig = flows.handle_message


FALLAS = []
CASOS = [
    ("Hola, quiero agendar una hora. (web: home)", "home", "", ""),
    ("Hola, quiero agendar una Ecografía. (web: blog · eco-abdominal · flotante)", "blog", "eco-abdominal", "flotante"),
    ("Hola, quiero agendar. (web: blog / dolor-lumbar / cuerpo-2)", "blog", "dolor-lumbar", "cuerpo-2"),
    ("(web)Hola, quiero agendar Otorrino para hoy.", "", "", ""),
    ("Hola, soy de Curanilahue. (web)", "", "", ""),
]
for i, (txt, pag, art, btn) in enumerate(CASOS):
    tel = f"5698765{i:04d}"
    asyncio.run(flows.handle_message(tel, txt, {"state": "IDLE", "data": {}}))
    with session.db() as c:
        ev = c.execute("SELECT meta FROM conversation_events WHERE phone=? AND event='web_origen'", (tel,)).fetchone()
        tags = [r[0] for r in c.execute("SELECT tag FROM contact_tags WHERE phone=?", (tel,))]
        ent = c.execute("SELECT text FROM messages WHERE phone=? AND direction='in' ORDER BY ts LIMIT 1", (tel,)).fetchone()
    m = json.loads(ev[0]) if ev else {}
    ok = (bool(ev) and m.get("pagina") == pag and m.get("articulo") == art and m.get("boton") == btn
          and "referral_source:web" in tags and (not pag or f"referral_source:web_{pag}" in tags))
    print(("OK  " if ok else "FAIL") + f" {txt[:60]!r} → {m} tags={tags}")
    if not ok:
        FALLAS.append(txt)
# segunda llegada del mismo teléfono: se registra otra vez web_origen
tel = "56987650001"
asyncio.run(flows.handle_message(tel, "Hola. (web: blog · cardiologia · flotante)", {"state": "IDLE", "data": {}}))
with session.db() as c:
    n = c.execute("SELECT COUNT(*) FROM conversation_events WHERE phone=? AND event='web_origen'", (tel,)).fetchone()[0]
ok = n == 2
print(("OK  " if ok else "FAIL") + f" segunda llegada registrada (eventos={n})")
if not ok:
    FALLAS.append("segunda")
print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
