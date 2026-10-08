"""Atribución Google Ads (2026-10-08), pasos 1-4:
- POST /api/gclick (TestClient): validación, idempotencia, rate-limit, solo escritura.
- Bot: marcador "g-<code>" -> tag referral_source:google_ads + vínculo phone<->clic,
  marcador limpio antes de clasificar, el marcador (web: ...) de siempre sigue igual.
- save_cita_bot: cita queda con el clic (ventana 90 días).
- Conversión offline: cola idempotente, apagada por flag, payload (hora Santiago con offset,
  order_id = id cita), errores parciales.
Se corre como script: python tests/test_google_ads_2026_10_08.py"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("MEDILINK_TOKEN", "x")

import session  # noqa: E402

session.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c)
    _c.commit()

import config  # noqa: E402
import flows  # noqa: E402
import google_ads as ga  # noqa: E402

FALLAS = []


def check(nombre, cond):
    print(("OK  " if cond else "FAIL") + " " + nombre)
    if not cond:
        FALLAS.append(nombre)


def q1(sql, *a):
    with session.db() as c:
        return c.execute(sql, a).fetchone()


# ───────────────────────── Paso 1: endpoint ─────────────────────────
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

cli = TestClient(main.app)
GCL = "Cj0KCQjw_TEST-gclid_abcdefghijklmnop123456"
OK_BODY = {"code": "K7M2X", "gclid": GCL, "landing": "/ecografia", "ts": int(time.time())}

r = cli.post("/api/gclick", json=OK_BODY)
check("POST valido -> 200 ok", r.status_code == 200 and r.json() == {"ok": True})
row = q1("SELECT * FROM google_clicks WHERE code='K7M2X'")
check("fila guardada con tipo, id y landing", row and row["click_type"] == "gclid"
      and row["click_id"] == GCL and row["landing"] == "/ecografia" and row["phone"] is None)
check("reintento identico es idempotente (200)", cli.post("/api/gclick", json=OK_BODY).status_code == 200)
check("mismo code con OTRO gclid -> 409 y no pisa",
      cli.post("/api/gclick", json={**OK_BODY, "gclid": "OtroGclidDistinto_123456"}).status_code == 409
      and q1("SELECT click_id FROM google_clicks WHERE code='K7M2X'")["click_id"] == GCL)
r = cli.post("/api/gclick", json={"code": "ABCD2", "gbraid": "GbraidTest_123", "landing": "/odontologia-general"})
check("gbraid y wbraid aceptados", r.status_code == 200
      and cli.post("/api/gclick", json={"code": "ABCD3", "wbraid": "WbraidTest_123"}).status_code == 200
      and q1("SELECT click_type FROM google_clicks WHERE code='ABCD2'")["click_type"] == "gbraid")
for nombre, body in {
    "code con 0/O (ambiguo)": {**OK_BODY, "code": "K0M2X"},
    "code corto": {**OK_BODY, "code": "K7M"},
    "code largo": {**OK_BODY, "code": "K7M2XQ9"},
    "sin click id": {"code": "ZZZZ2", "landing": "/x"},
    "dos click id": {"code": "ZZZZ3", "gclid": GCL, "gbraid": GCL},
    "gclid con caracteres raros": {"code": "ZZZZ4", "gclid": "a b;DROP TABLE"},
    "landing con query/script": {"code": "ZZZZ5", "gclid": GCL, "landing": "http://evil/<script>"},
    "body no objeto": [1, 2, 3],
}.items():
    check(f"400: {nombre}", cli.post("/api/gclick", json=body).status_code == 400)
check("400: JSON roto", cli.post("/api/gclick", content=b"{no es json").status_code == 400)
check("413: cuerpo enorme", cli.post("/api/gclick", content=b"x" * 5000).status_code == 413)
check("403: Origin ajeno", cli.post("/api/gclick", json={**OK_BODY, "code": "QQQQ2"},
                                    headers={"Origin": "https://evil.example"}).status_code == 403)
check("200: Origin propio", cli.post("/api/gclick", json={**OK_BODY, "code": "QQQQ3"},
                                     headers={"Origin": "https://agentecmc.cl"}).status_code == 200)
check("solo escritura: GET -> 405, sin ruta de lectura",
      cli.get("/api/gclick").status_code == 405 and cli.get("/api/gclick/K7M2X").status_code in (404, 405))
check("la respuesta no filtra el gclid", GCL not in cli.post("/api/gclick", json=OK_BODY).text)
check("tabla sin cuerpos basura tras los 400", q1("SELECT COUNT(*) n FROM google_clicks")["n"] == 4)

main._gclick_buckets.clear()
codes = [429 if False else cli.post("/api/gclick", json={**OK_BODY, "code": "RRRR2"},
                                    headers={"CF-Connecting-IP": "9.9.9.9"}).status_code for _ in range(25)]
check("rate-limit por IP: pasa 20 y luego 429", codes.count(200) == 20 and codes[-1] == 429)
check("otra IP sigue pasando", cli.post("/api/gclick", json=OK_BODY, headers={"CF-Connecting-IP": "8.8.8.8"}).status_code == 200)
main._gclick_buckets.clear()

# ───────────────────────── Paso 2: bot ─────────────────────────
vistos = []
_orig_norm = flows.normalizar_texto_paciente


def _spy(t):
    vistos.append(t)
    return _orig_norm(t)


flows.normalizar_texto_paciente = _spy


def llega(tel, texto):
    vistos.clear()
    session.save_privacy_consent(tel, "accepted", method="test")
    try:
        asyncio.run(flows.handle_message(tel, texto, {"state": "IDLE", "data": {}}))
    except Exception as e:           # lo que pase DESPUÉS del marcador no es de este test
        print("   (flujo posterior lanzó:", type(e).__name__, ")")


def tags(tel):
    return session.get_tags(tel)


T1 = "56911110001"
llega(T1, "Hola, quiero agendar una ecografía. (web: landing_ecografia · ecografia · hero · g-k7m2x)")
check("g- en marcador extendido: tag google_ads", "referral_source:google_ads" in tags(T1))
check("sigue el tag web y el slug de la pagina (no se pierde nada de lo de antes)",
      "referral_source:web" in tags(T1) and "referral_source:web_landing_ecografia" in tags(T1))
check("no se creo tag web_g-...", not any(t.startswith("referral_source:web_g") for t in tags(T1)))
lk = q1("SELECT phone, linked_ts FROM google_clicks WHERE code='K7M2X'")
check("phone vinculado al clic", lk["phone"] == T1 and lk["linked_ts"])
ev = q1("SELECT meta FROM conversation_events WHERE phone=? AND event='web_origen'", T1)
m = json.loads(ev["meta"])
check("web_origen: pagina/articulo/boton intactos", (m["pagina"], m["articulo"], m["boton"]) ==
      ("landing_ecografia", "ecografia", "hero"))
check("marcador limpio antes de clasificar (sin 'g-' ni '(web')",
      vistos and all("g-k7m2x" not in v.lower() and "(web" not in v.lower() for v in vistos))
check("evento gads_vinculo", q1("SELECT 1 FROM conversation_events WHERE phone=? AND event='gads_vinculo'", T1))

T2 = "56911110002"
llega(T2, "Hola, quiero agendar. (web: g-abcd2)")
check("marcador simple (web: g-code): tag y vinculo",
      "referral_source:google_ads" in tags(T2) and q1("SELECT phone FROM google_clicks WHERE code='ABCD2'")["phone"] == T2)

T3 = "56911110003"
llega(T3, "Hola, quiero agendar. (web: g-zzzz9)")
check("codigo desconocido: no tagea google_ads, igual deja el web",
      "referral_source:google_ads" not in tags(T3) and "referral_source:web" in tags(T3)
      and q1("SELECT 1 FROM conversation_events WHERE phone=? AND event='gads_codigo_sin_clic'", T3))

T4 = "56911110004"
llega(T4, "Hola, quiero agendar. (web: g-k7m2x)")
check("link compartido: otro telefono NO roba el clic ya vinculado",
      "referral_source:google_ads" not in tags(T4) and q1("SELECT phone FROM google_clicks WHERE code='K7M2X'")["phone"] == T1)

T5 = "56911110005"
llega(T5, "Hola, quiero agendar una hora. (web: home)")
check("marcador web de siempre: sin cambios (sin tag google)",
      "referral_source:web_home" in tags(T5) and "referral_source:google_ads" not in tags(T5))
llega("56911110006", "Hola, quiero agendar. (web)")
check("(web) pelado sigue funcionando", "referral_source:web" in tags("56911110006"))

# ───────────────────────── Paso 3: cita <-> clic (90 días) ─────────────────────────
session.save_cita_bot(T1, "9001", "Ecografía", "David Pardo", "2026-10-09", "10:00", "presencial", id_paciente_medilink=5)
c1 = q1("SELECT gads_code, gads_click_type, gads_click_id FROM citas_bot WHERE id_cita='9001'")
check("cita queda asociada al gclid del telefono", (c1["gads_code"], c1["gads_click_type"], c1["gads_click_id"]) == ("K7M2X", "gclid", GCL))
session.save_cita_bot("56999999999", "9002", "Ecografía", "David Pardo", "2026-10-09", "11:00", "presencial")
check("telefono sin clic: cita sin gclid", q1("SELECT gads_click_id FROM citas_bot WHERE id_cita='9002'")["gads_click_id"] is None)
with session.db() as c:
    c.execute("INSERT INTO google_clicks (code,click_type,click_id,landing,ts,phone,linked_ts) VALUES (?,?,?,?,?,?,?)",
              ("OLDC2", "gclid", "ViejoGclid_123456", "/x", int(time.time()) - 91 * 86400, "56922220001", int(time.time())))
session.save_cita_bot("56922220001", "9003", "Ecografía", "David Pardo", "2026-10-09", "12:00", "presencial")
check("clic de hace 91 dias NO se asocia (ventana 90)", q1("SELECT gads_click_id FROM citas_bot WHERE id_cita='9003'")["gads_click_id"] is None)
with session.db() as c:
    c.execute("INSERT INTO google_clicks (code,click_type,click_id,landing,ts,phone,linked_ts) VALUES (?,?,?,?,?,?,?)",
              ("NEWC2", "gbraid", "GbraidNuevo_123", "/x", int(time.time()) - 89 * 86400, "+56 9 3333 0001", int(time.time())))
session.save_cita_bot("56933330001", "9004", "Ecografía", "David Pardo", "2026-10-09", "12:30", "presencial")
check("clic de hace 89 dias si; telefono con formato distinto casa por 9 digitos",
      q1("SELECT gads_click_type FROM citas_bot WHERE id_cita='9004'")["gads_click_type"] == "gbraid")

# ───────────────────────── Paso 4: conversión offline ─────────────────────────
check("formato fecha: verano Chile -03:00", ga.formato_fecha_google(
    int(__import__("datetime").datetime(2026, 10, 8, 14, 30, tzinfo=ga._CLT).timestamp())) == "2026-10-08 14:30:00-03:00")
check("formato fecha: invierno Chile -04:00", ga.formato_fecha_google(
    int(__import__("datetime").datetime(2026, 7, 8, 14, 30, tzinfo=ga._CLT).timestamp())) == "2026-07-08 14:30:00-04:00")

item = {"id_cita": "9001", "phone": T1, "fecha": "2026-10-09", "hora": "10:00", "value": 45000.0}
check("encolar cita con gclid", ga.encolar_conversion(item) is True)
check("encolar es idempotente (1 fila)", ga.encolar_conversion(item) and q1("SELECT COUNT(*) n FROM google_ads_uploads")["n"] == 1)
check("cita sin gclid no se encola", ga.encolar_conversion({**item, "id_cita": "9002", "phone": "56999999999"}) is False)

llamadas = []


class _Resp:
    def __init__(self, code, data):
        self.status_code, self._d, self.text = code, data, json.dumps(data)

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Cli:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, **kw):
        llamadas.append((url, kw))
        if "oauth2" in url:
            return _Resp(200, {"access_token": "tok", "expires_in": 3600})
        return _Resp(200, _Cli.respuesta)

    respuesta = {"results": [{}]}


import httpx  # noqa: E402
_httpx_orig = httpx.AsyncClient
httpx.AsyncClient = _Cli

res = asyncio.run(ga.subir_pendientes())
check("flag apagado (default): no hace nada ni llama a Google", res["habilitado"] is False and not llamadas
      and q1("SELECT estado FROM google_ads_uploads")["estado"] == "pendiente")
config.GOOGLE_ADS_OFFLINE_ENABLED = True
res = asyncio.run(ga.subir_pendientes())
check("flag ON sin credenciales: no sube, no rompe", not llamadas and res["subidas"] == 0
      and q1("SELECT estado FROM google_ads_uploads")["estado"] == "pendiente")

config.GOOGLE_ADS_DEVELOPER_TOKEN, config.GOOGLE_ADS_CLIENT_ID = "dev", "cid"
config.GOOGLE_ADS_CLIENT_SECRET, config.GOOGLE_ADS_REFRESH_TOKEN = "sec", "ref"
config.GOOGLE_ADS_CUSTOMER_ID, config.GOOGLE_ADS_CONVERSION_ACTION_ID = "123-456-7890", "555"
res = asyncio.run(ga.subir_pendientes())
url, kw = llamadas[-1]
conv = kw["json"]["conversions"][0]
check("URL uploadClickConversions del cliente (sin guiones)", ":uploadClickConversions" in url and "/customers/1234567890:" in url)
check("headers: developer-token y bearer", kw["headers"]["developer-token"] == "dev" and kw["headers"]["Authorization"] == "Bearer tok")
check("payload: gclid, orderId = id cita, valor CLP, accion",
      conv["gclid"] == GCL and conv["orderId"] == "9001" and conv["conversionValue"] == 45000.0
      and conv["currencyCode"] == "CLP" and conv["conversionAction"] == "customers/1234567890/conversionActions/555")
check("payload: hora de la atencion en Santiago con offset", conv["conversionDateTime"] == "2026-10-09 10:00:00-03:00")
check("partialFailure activo", kw["json"]["partialFailure"] is True)
check("estado sent tras subir", res["subidas"] == 1 and q1("SELECT estado, sent_at FROM google_ads_uploads")["estado"] == "sent")
n = len(llamadas)
asyncio.run(ga.subir_pendientes())
check("ya enviada: no se vuelve a subir (idempotente)", len(llamadas) == n)

# gbraid va en su campo; error parcial cuenta intento; DUPLICATE_ORDER_ID = ya estaba
ga.encolar_conversion({"id_cita": "9004", "phone": "56933330001", "fecha": "2026-10-09", "hora": "12:30", "value": 30000.0})
_Cli.respuesta = {"results": [{}], "partialFailureError": {"details": [{"errors": [{
    "errorCode": {"conversionUploadError": "CLICK_NOT_FOUND"}, "message": "x",
    "location": {"fieldPathElements": [{"fieldName": "conversions", "index": 0}]}}]}]}}
asyncio.run(ga.subir_pendientes())
conv = llamadas[-1][1]["json"]["conversions"][0]
f = q1("SELECT estado, intentos, ultimo_error FROM google_ads_uploads WHERE id_cita='9004'")
check("gbraid viaja como gbraid", "gbraid" in conv and "gclid" not in conv)
check("error parcial: sigue pendiente, intento+1, error guardado",
      f["estado"] == "pendiente" and f["intentos"] == 1 and "CLICK_NOT_FOUND" in f["ultimo_error"])
_Cli.respuesta = {"results": [{}], "partialFailureError": {"details": [{"errors": [{
    "errorCode": {"conversionUploadError": "DUPLICATE_ORDER_ID"}, "message": "dup",
    "location": {"fieldPathElements": [{"fieldName": "conversions", "index": 0}]}}]}]}}
asyncio.run(ga.subir_pendientes())
check("DUPLICATE_ORDER_ID = ya subida (sent)", q1("SELECT estado FROM google_ads_uploads WHERE id_cita='9004'")["estado"] == "sent")

with session.db() as c:
    c.execute("INSERT INTO google_ads_uploads (id_cita,phone,click_type,click_id,click_ts,conv_ts,value) VALUES "
              "('9100','x','gclid','Viejo_123456',?,?,1000)", (int(time.time()) - 95 * 86400, int(time.time())))
n = len(llamadas)
asyncio.run(ga.subir_pendientes())
check("clic >90 dias: expirada y no se envia", q1("SELECT estado FROM google_ads_uploads WHERE id_cita='9100'")["estado"] == "expirada"
      and len(llamadas) == n)


async def _caida(*a, **k):
    raise httpx.ConnectError("sin red")
_Cli.post = _caida
ga.encolar_conversion({"id_cita": "9001", "phone": T1, "fecha": "2026-10-09", "hora": "10:00", "value": 1.0})
with session.db() as c:
    c.execute("INSERT INTO google_ads_uploads (id_cita,phone,click_type,click_id,click_ts,conv_ts,value) VALUES "
              "('9101','x','gclid','Ok_1234567',?,?,1000)", (int(time.time()), int(time.time())))
_tok = ga._token_cache
_tok.update(tok="", exp=0)
res = asyncio.run(ga.subir_pendientes())
check("caida de red: no lanza y no quema intentos", res["errores"] == 1
      and q1("SELECT intentos FROM google_ads_uploads WHERE id_cita='9101'")["intentos"] == 0)
httpx.AsyncClient = _httpx_orig

# ───────────────────────── Landings + JS ─────────────────────────
for pag in ("ecografia", "odontologia-general", "ortodoncia", "implantologia"):
    html = cli.get("/" + pag).text
    check(f"/{pag}: marcador (web: landing_...) intacto y cmc-wa.js antes de cmc-gads.js",
          "(web%3A%20landing_" in html and html.find("/static/cmc-wa.js") < html.find("/static/cmc-gads.js") != -1)
check("cmc-gads.js se sirve", cli.get("/static/cmc-gads.js").status_code == 200)

node = shutil.which("node")
if node:
    js = (ROOT / "static" / "cmc-gads.js").read_text(encoding="utf-8")
    harness = r"""
const src = process.argv[2], url = process.argv[3], fixed = process.argv[4] === 'ok';
const fs = require('fs'); const code = fs.readFileSync(src, 'utf8');
let store = {}; let posts = [];
const links = [
  {h: 'https://wa.me/56966610737?text=Hola%2C%20quiero%20agendar%20(web%3A%20landing_ecografia%20%C2%B7%20ecografia%20%C2%B7%20hero)&utm_source=x', skip: false},
  {h: 'https://wa.me/56966610737?text=Hola', skip: false},
  {h: 'https://example.com/otra', skip: false}];
const els = links.map(l => ({ _h: l.h, getAttribute(k){return k==='href'?this._h:null}, setAttribute(k,v){ if(k==='href') this._h=v }, hasAttribute(){return false}}));
const g = globalThis;
g.location = new URL(url); g.localStorage = {getItem:k=>store[k]||null,setItem:(k,v)=>{store[k]=v}};
g.document = {querySelectorAll: () => els.filter(e => e._h.indexOf('wa.me/56966610737') >= 0), addEventListener(){}};
g.window = g; g.addEventListener = ()=>{};
g.fetch = (u, o) => { posts.push(JSON.parse(o.body)); return Promise.resolve({ok: fixed, status: fixed ? 200 : 500}); };
require('vm').runInThisContext(code);  // JS propio del repo, en un contexto con DOM simulado
setTimeout(() => console.log(JSON.stringify({posts, hrefs: els.map(e => e._h), store})), 50);
"""
    hp = Path(tempfile.mkdtemp()) / "h.js"
    hp.write_text(harness)

    def corre(url, ok="ok"):
        o = subprocess.run([node, str(hp), str(ROOT / "static" / "cmc-gads.js"), url, ok],
                           capture_output=True, text=True, timeout=20)
        return json.loads(o.stdout) if o.stdout.strip() else {"err": o.stderr}

    a = corre("https://agentecmc.cl/ecografia")
    check("JS sin gclid: no postea ni toca los links", a.get("posts") == [] and "g-" not in "".join(a["hrefs"]) and a["store"] == {})
    b = corre("https://agentecmc.cl/ecografia?gclid=TEST1234&utm_source=google")
    cod = b["posts"][0]["code"] if b.get("posts") else ""
    check("JS con gclid: POST {code,gclid,landing,ts}", len(b.get("posts", [])) == 1 and ga.RE_CODE.match(cod)
          and b["posts"][0]["gclid"] == "TEST1234" and b["posts"][0]["landing"] == "/ecografia" and b["posts"][0]["ts"])
    import urllib.parse as up
    t0 = up.parse_qs(up.urlparse(b["hrefs"][0]).query)["text"][0]
    check("JS: marcador extendido + ' · g-CODE' (y conserva utm)", t0.endswith(f"hero · g-{cod})") and t0.startswith("Hola, quiero agendar (web: landing_ecografia")
          and "utm_source=x" in b["hrefs"][0])
    t1 = up.parse_qs(up.urlparse(b["hrefs"][1]).query)["text"][0]
    check("JS: link sin marcador recibe '(web: g-CODE)'", t1 == f"Hola (web: g-{cod})")
    check("JS: links que no son del bot no se tocan", b["hrefs"][2] == "https://example.com/otra")
    c = corre("https://agentecmc.cl/ecografia?gclid=TEST1234", ok="no")
    check("JS: si el POST falla, los links quedan como hoy", "g-" not in "".join(c["hrefs"]))
    check("JS: el gclid de tipo wbraid tambien", corre("https://agentecmc.cl/x?wbraid=WB123456")["posts"][0].get("wbraid") == "WB123456")
else:
    print("SKIP JS (node no disponible)")

print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
