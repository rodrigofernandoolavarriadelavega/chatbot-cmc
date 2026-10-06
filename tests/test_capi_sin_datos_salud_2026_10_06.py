"""CAPI: lo que sale hacia Meta nunca lleva especialidad ni categoría médica (6-oct-2026)."""
import asyncio, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import session
session.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c); _c.commit()
import meta_capi, httpx

FALLAS = []
def check(n, c):
    print(("OK  " if c else "FAIL") + " " + n)
    if not c: FALLAS.append(n)

ENVIADO = []
class _R:
    status_code = 200
    text = '{"events_received":1}'
    def json(self): return {"events_received": 1}
class _C:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def post(self, url, *a, **k):
        ENVIADO.append(k.get("json") or json.loads(k.get("data") or "{}")); return _R()
httpx.AsyncClient = _C
meta_capi._cfg = lambda: ("PIXEL", "TOKEN", None)

asyncio.run(meta_capi.send_event("Purchase", phone="56911111111", value=6000.0, currency="CLP",
    rut="11111111-1", first_name="Ana", ctwa_clid="CLID", event_id="purchase_cita_1",
    custom_data={"content_name": "Psiquiatría", "content_category": "medical_appointment",
                 "venta_total": 20000, "currency": "CLP", "especialidad": "Psiquiatría"}))
txt = json.dumps(ENVIADO, ensure_ascii=False)
check("se envió un evento", len(ENVIADO) >= 1)
check("sin especialidad en el payload", "Psiquiatr" not in txt and "content_name" not in txt)
check("sin categoría médica", "medical" not in txt and "content_category" not in txt)
cd = ENVIADO[0]["data"][0]["custom_data"] if ENVIADO else {}
check("conserva valor, moneda y venta_total", cd.get("value") == 6000.0 and cd.get("currency") == "CLP" and cd.get("venta_total") == 20000)
check("conserva el clic del anuncio", "CLID" in txt)
print("\n%d fallas" % len(FALLAS)); sys.exit(1 if FALLAS else 0)
