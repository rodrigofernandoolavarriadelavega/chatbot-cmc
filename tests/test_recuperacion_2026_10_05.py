"""Vista "Recuperar pacientes" (2026-10-05): segundo contacto manual de recepción.

DB temporal, datos SINTÉTICOS, envíos y Meta mockeados (no sale nada a la red).
Cubre: auth, exclusiones, ventana 24 h, plantilla aprobada/no aprobada,
registro de gestión, no enviar dos veces en 7 días, botón "No, gracias",
ausencia de datos de campañas/gasto en la respuesta y borradores de plantillas.
"""
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "scripts"))

import session  # noqa: E402

session.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c)
    _c.commit()

import config  # noqa: E402

config.OLACORE_TOKEN = "dueno_test"
config.ADMIN_TOKEN = "recepcion_test"
import admin_routes  # noqa: E402

admin_routes._ADMIN_TOKENS = ("recepcion_test", "dueno_test")
import alma_scope  # noqa: E402

alma_scope._ADMIN_TOKENS = ("recepcion_test", "dueno_test")
import ausentismo  # noqa: E402
import campanas_meta_routes as cm  # noqa: E402
import recuperacion as rec  # noqa: E402

ausentismo.ensure_ausentismo_table()
cm._crear_tablas_seguimiento()

FALLAS = []


def check(nombre, cond):
    print(("OK  " if cond else "FAIL") + " " + nombre)
    if not cond:
        FALLAS.append(nombre)


CL = ZoneInfo("America/Santiago")
AHORA = datetime.now(CL)
HOY = AHORA.date()


def ep(d):
    return int((AHORA - timedelta(days=d)).timestamp())


def utc(d):
    return (AHORA - timedelta(days=d)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fecha(n):
    return (HOY + timedelta(days=n)).isoformat()


H = 1 / 24


def ph(n):
    return f"5695550{n:04d}"


with session.db() as c:
    def ref(p, dias, ad="ADX"):
        c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
                  (p, ad, "Titular secreto", ep(dias), "facebook"))

    def msg(p, dias, d="in"):
        c.execute("INSERT INTO messages (phone, direction, text, ts) VALUES (?,?,?,?)", (p, d, "hola", utc(dias)))

    def ev(p, e, dias, meta=None):
        c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
                  (p, e, json.dumps(meta or {}), utc(dias)))

    def slots(p, dias, esp="Medicina General"):
        ev(p, "funnel_slot_ofrecido", dias, {"esp": esp})

    def cita(p, idc, f, creada, esp="Medicina General", pid=None, cancel=None):
        c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, created_at, "
                  "cancel_detected_at, ad_source_id, id_paciente_medilink) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (p, idc, esp, "Dr", fecha(f), "10:00", utc(creada), cancel, "ADX", pid))

    def nombre(p, n):
        c.execute("INSERT INTO contact_profiles (phone, nombre) VALUES (?,?)", (p, n))

    def tag(p, t):
        c.execute("INSERT INTO contact_tags (phone, tag) VALUES (?,?)", (p, t))

    # P1 no asistió
    ref(ph(1), 8); msg(ph(1), 8); cita(ph(1), "7101", -3, 7.9, pid=9100001); nombre(ph(1), "Nora Noshow")
    c.execute("INSERT INTO ausentismo_citas (id_cita, id_profesional, id_paciente, fecha, hora, id_estado, estado_cita, "
              "anulacion) VALUES (?,?,?,?,?,?,?,?)", (7101, 1, 9100001, fecha(-3), "10:00", 8, "", 0))
    # P2 vio horas hace 3 días (ventana cerrada)
    ref(ph(2), 3); slots(ph(2), 2.9, "Kinesiología"); msg(ph(2), 2.9); nombre(ph(2), "Vicente Vio")
    # P3 vio horas hace 5 h (ventana abierta)
    ref(ph(3), 5 * H); slots(ph(3), 4.9 * H, "Psiquiatría"); msg(ph(3), 4.9 * H); nombre(ph(3), "Valeria Reciente")
    # P4 con cita futura creada tras el clic (kanban: Agendado)
    ref(ph(4), 4); msg(ph(4), 3.9); cita(ph(4), "F4", 3, 3.8)
    # P4b vio horas pero tiene cita futura creada ANTES del clic
    ref(ph(14), 2); slots(ph(14), 1.9); msg(ph(14), 1.9); cita(ph(14), "F14", 5, 9)
    # P5/P6/P7/P8 exclusiones de consentimiento / número
    for n, t in ((5, "marketing_opt_out"), (6, rec.TAG_OPT_OUT), (7, "posible_numero_equivocado")):
        ref(ph(n), 3); slots(ph(n), 2.9); msg(ph(n), 2.9); tag(ph(n), t)
    ref(ph(8), 3); slots(ph(8), 2.9); msg(ph(8), 2.9)
    # P9 recordatorio hace 2 días (excluido) · P19 hace 10 días (incluido)
    ref(ph(9), 4); slots(ph(9), 3.9); msg(ph(9), 3.9); ev(ph(9), "recuperacion_enviado", 2)
    ref(ph(19), 12); slots(ph(19), 11.9); msg(ph(19), 11.9); ev(ph(19), "recuperacion_enviado", 10)
    # P10 sin respuesta > 7 días
    ref(ph(10), 20); msg(ph(10), 15); nombre(ph(10), "Pablo Perdido")
    # P11 escribió, sin ver horas, hace 20 h (ventana abierta)
    ref(ph(11), 1); msg(ph(11), 20 * H)
    # P12 en conversación activa (30 min)
    ref(ph(12), 1); slots(ph(12), 0.5 * H); msg(ph(12), 0.5 * H)
    # P13 gestión cerrada · P18 volver a llamar
    ref(ph(13), 3); slots(ph(13), 2.9); msg(ph(13), 2.9)
    ref(ph(18), 3); slots(ph(18), 2.9); msg(ph(18), 2.9)
    # P15 llegó por la web
    ev(ph(15), "web_origen", 4, {"pagina": "blog", "articulo": "eco", "boton": "flotante", "texto": "hola"})
    msg(ph(15), 3.9); slots(ph(15), 3.8)
    # P16 Instagram
    ref("ig_999", 3); msg("ig_999", 2.9)
    # P17 recepción le respondió hace 3 h
    ref(ph(17), 3); slots(ph(17), 2.9); msg(ph(17), 2.9); ev(ph(17), "recepcionista_respondio", 3 * H)
    c.commit()

cm.guardar_seguimiento(cm._clave(ph(13)), "no_interesa", "No quiere", None, origen="recepción", ahora=AHORA)
cm.guardar_seguimiento(cm._clave(ph(18)), "volver_llamar", "Llamar el jueves", fecha(2), origen="dueño", ahora=AHORA)

# Mocks de red / BI
BI = {"optouts": {rec._k(ph(8))}}
rec._bi_optouts = lambda: BI["optouts"]
EST = {n: "PENDING" for n in set(rec.PLANTILLAS.values())}


async def _meta(nombres):
    return {n: EST[n] for n in nombres if n in EST}


rec._consultar_meta = _meta
CITA_EXT = {"v": "libre"}


async def _cext(phone):
    return CITA_EXT["v"]


rec._cita_externa = _cext
ENV = {"texto": [], "plantilla": []}


async def _resp(phone, texto, exigir_entrega=False):
    ENV["texto"].append((phone, texto, exigir_entrega))
    return {"ok": True}


async def _tpl(to, name, body_params=None, button_payloads=None, **kw):
    ENV["plantilla"].append((to, name, body_params, button_payloads))
    return "wamid.TEST"


admin_routes.responder_como_recepcion = _resp
import messaging  # noqa: E402

messaging.send_whatsapp_template = _tpl


def run(co):
    rec._locks.clear()
    return asyncio.run(co)


# ── Lista: quién entra, quién no, orden ─────────────────────────────────────
d = rec.lista(30, AHORA)
por = {p["_phone"]: p for p in d["personas"]}
ex = d["excluidos"]
check("entra: no asistió", ph(1) in por and por[ph(1)]["situacion"] == "no_asistio" and por[ph(1)]["prioridad"] == "alta")
check("entra: vio horas hace 3 días (ventana cerrada)", ph(2) in por and por[ph(2)]["situacion"] == "vio_horas"
      and por[ph(2)]["ventana_abierta"] is False)
check("entra: vio horas hace 5 h con ventana ABIERTA", ph(3) in por and por[ph(3)]["ventana_abierta"] is True
      and 18 < por[ph(3)]["ventana_cierra_en_h"] < 20)
check("entra: sin respuesta > 7 días", ph(10) in por and por[ph(10)]["situacion"] == "sin_respuesta"
      and por[ph(10)]["prioridad"] == "baja")
check("entra: escribió y no siguió (ventana abierta)", ph(11) in por and por[ph(11)]["situacion"] == "escribio"
      and por[ph(11)]["ventana_abierta"])
check("entra: llegó por la web", ph(15) in por and por[ph(15)]["origen"] == "Página web")
check("entra: recordatorio de hace 10 días ya no bloquea", ph(19) in por)
check("entra: volver a llamar sigue en la lista", ph(18) in por and por[ph(18)]["gestion"]["estado"] == "volver_llamar"
      and por[ph(18)]["gestion"]["nota"] == "Llamar el jueves")
check("sale: ya agendado (cita futura del bot)", ph(4) not in por)
check("sale: cita futura creada antes del clic", ph(14) not in por and ex["cita_futura"] == 1)
check("sale: marketing_opt_out", ph(5) not in por)
check("sale: opt-out del botón de la plantilla", ph(6) not in por)
check("sale: número equivocado", ph(7) not in por and ex["numero_equivocado"] == 1)
check("sale: opt-out en BI", ph(8) not in por and ex["opt_out"] == 3)
check("sale: recordatorio en los últimos 7 días", ph(9) not in por and ex["recordatorio"] == 1)
check("sale: conversación activa (<2 h)", ph(12) not in por)
check("sale: recepción le escribió hace poco", ph(17) not in por and ex["conversacion_activa"] == 2)
check("sale: gestión 'No le interesa'", ph(13) not in por and ex["gestion_cerrada"] == 1)
check("sale: Instagram/Messenger", "ig_999" not in por and ex["otro_canal"] == 1)
orden = [p["_phone"] for p in d["personas"]]
check("prioridad: no-show primero, luego vio horas recientes, sin respuesta al final",
      orden[0] == ph(1) and orden.index(ph(3)) < orden.index(ph(2)) and orden[-1] == ph(10))
check("telefono completo y nombre", por[ph(1)]["nombre"] == "Nora Noshow" and "+56 9" in por[ph(1)]["telefono"])
check("texto sugerido sin especialidad sensible (psiquiatría)", "siquiatr" not in por[ph(3)]["texto_sugerido"].lower()
      and "psiquiatr" not in por[ph(3)]["texto_sugerido"].lower())
check("texto sugerido con especialidad no sensible", "kinesiología" in por[ph(2)]["texto_sugerido"].lower())
chilenos = " ".join(p["texto_sugerido"] + p["plantilla_texto"] for p in d["personas"]).lower()
check("sin voseo ni emojis en los textos", not any(w in chilenos for w in ("querés", "tenés", "podés", "sabés", "vos ", "dale"))
      and all(ord(ch) < 0x2600 for ch in chilenos))
dj = json.dumps(rec.publica(d), ensure_ascii=False).lower()
check("no filtra campañas/gasto/anuncio (claves ni textos del anuncio)",
      all(k not in dj for k in ("campana", "campaign", "ad_id", "gasto", "spend", "plataforma", "titular secreto", "adx")))
check("filtro por gestión", all(p["gestion"]["estado"] == "volver_llamar" for p in rec.lista(30, AHORA, "volver_llamar")["personas"]))

# ── Auth HTTP ───────────────────────────────────────────────────────────────
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import recuperacion_routes  # noqa: E402

app = FastAPI()
recuperacion_routes.register_recuperacion_routes(app)
cli = TestClient(app)
check("auth: sin token 401", cli.get("/api/recuperar/lista").status_code == 401)
check("auth: token malo 403", cli.get("/api/recuperar/lista?token=otro").status_code == 403)
check("auth: envío sin token 401", cli.post(f"/api/recuperar/{rec._k(ph(2))}/enviar", json={}).status_code == 401)
check("auth: gestión sin token 401", cli.post(f"/api/recuperar/{rec._k(ph(2))}/gestion", json={}).status_code == 401)
r = cli.get("/api/recuperar/lista?token=recepcion_test")
check("auth: recepción 200 y trae la lista", r.status_code == 200 and r.json()["total"] == d["total"])
check("auth: Bearer del dueño 200", cli.get("/api/recuperar/lista", headers={"Authorization": "Bearer dueno_test"}).status_code == 200)
check("api: no expone campos internos", all(not k.startswith("_") for p in r.json()["personas"] for k in p))
check("api: plantillas sin aprobar", all(not v["aprobada"] for v in r.json()["plantillas"].values()))
check("página: sin credencial redirige al login", cli.get("/alma/recuperar", follow_redirects=False).status_code == 302)
pg = cli.get("/alma/recuperar?token=recepcion_test")
check("página: con token 200 y sin placeholder", pg.status_code == 200 and "__TOKEN__" not in pg.text
      and "recepcion_test" in pg.text)
check("registro: módulo en Alma y en perfil Recepción", "recuperar" in config.ALMA_MODULE_REGISTRY
      and config.ALMA_MODULE_REGISTRY["recuperar"]["src"] == "/alma/recuperar")
src_cfg = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
check("registro: está en la lista de módulos de Recepción", '"ausentismo", "recuperar", "pagos"' in src_cfg)

# ── Envío ───────────────────────────────────────────────────────────────────
k2, k3, k1, k10 = (rec._k(ph(n)) for n in (2, 3, 1, 10))


def enviar(k, modo, mensaje=None, confirmar=True):
    try:
        return run(rec.enviar(k, modo, mensaje, confirmar, AHORA)), None
    except rec.NoEnviable as e:
        return None, e


_, e = enviar(k3, "texto", "hola", confirmar=False)
check("envío: sin confirmación no sale (400)", e and e.status == 400 and not ENV["texto"])
_, e = enviar(k2, "texto", "hola")
check("ventana cerrada: texto libre rechazado", e and e.status == 409 and not ENV["texto"])
_, e = enviar(k2, "plantilla")
check("ventana cerrada + plantilla sin aprobar: bloqueado con aviso de Meta",
      e and "aprobación de Meta" in str(e) and not ENV["plantilla"])
_, e = enviar(k3, "plantilla")
check("ventana abierta: la plantilla no se usa", e and e.status == 409 and not ENV["plantilla"])
_, e = enviar(k3, "texto", "   ")
check("mensaje vacío rechazado", e and e.status == 400)
_, e = enviar(k3, "texto", "x" * 1001)
check("mensaje demasiado largo rechazado", e and e.status == 400)
CITA_EXT["v"] = "tiene"
_, e = enviar(k3, "texto", "Hola")
check("cita externa en Medilink: no se envía", e and "Medilink" in str(e) and not ENV["texto"])
CITA_EXT["v"] = "libre"
BI["optouts"] = None
_, e = enviar(k3, "texto", "Hola")
check("BI caído: envío bloqueado (falla cerrada)", e and e.status == 503 and not ENV["texto"])
BI["optouts"] = {rec._k(ph(8))}

msg_ok = "Hola Valeria, te escribimos del Centro Médico Carampangue. ¿Sigues necesitando la hora?"
r_, e = enviar(k3, "texto", msg_ok)
check("ventana abierta: texto libre sale por responder_como_recepcion",
      r_ and r_["ok"] and ENV["texto"] == [(ph(3), msg_ok, True)])
with session.db() as c:
    evs = c.execute("SELECT meta FROM conversation_events WHERE phone=? AND event='recuperacion_enviado'", (ph(3),)).fetchall()
    pc = c.execute("SELECT COUNT(*) FROM conversation_events WHERE phone=? AND event='proactive_contact'", (ph(3),)).fetchone()[0]
    sg = c.execute("SELECT estado, nota FROM campanas_seguimiento WHERE clave=?", (k3,)).fetchone()
    hs = c.execute("SELECT estado, nota, origen FROM campanas_seguimiento_hist WHERE clave=?", (k3,)).fetchall()
check("registro: evento recuperacion_enviado", len(evs) == 1 and json.loads(evs[0][0])["modo"] == "texto")
check("registro: cuenta en el presupuesto de contacto", pc == 1)
check("registro: estado Contactado y línea de historial",
      sg and sg[0] == "contactado" and len(hs) == 1 and "texto libre" in hs[0][1] and "recepción" in hs[0][2])
_, e = enviar(k3, "texto", msg_ok)
check("no se envía dos veces en 7 días (texto)", e and e.status == 409 and len(ENV["texto"]) == 1)
check("ya no aparece en la lista", ph(3) not in {p["_phone"] for p in rec.lista(30, AHORA)["personas"]})

# La ficha del dueño ve lo que marcó recepción (misma tabla)
fi = cm.persona_data(k3, ahora=AHORA)
check("el dueño ve el Contactado en su ficha", fi["seguimiento"]["estado"] == "contactado"
      and any("Recordatorio enviado" in h["nota"] for h in fi["seguimiento"]["historial"]))

# Plantilla aprobada, ventana cerrada
for n in EST:
    EST[n] = "APPROVED"
rec._tpl_cache = None
r_, e = enviar(k2, "plantilla")
check("ventana cerrada + plantilla APROBADA: sale con nombre y botones",
      r_ and ENV["plantilla"] == [(ph(2), "recuperar_hora_pendiente_v1", ["Vicente"], ["recup_agendar", "recup_no_gracias"])])
with session.db() as c:
    out = c.execute("SELECT text FROM messages WHERE phone=? AND direction='out'", (ph(2),)).fetchone()
    sg = c.execute("SELECT estado FROM campanas_seguimiento WHERE clave=?", (k2,)).fetchone()
check("plantilla: queda en el historial del chat y Contactado", out and "Vicente" in out[0] and sg and sg[0] == "contactado")
_, e = enviar(k2, "plantilla")
check("no se envía dos veces en 7 días (plantilla)", e and e.status == 409 and len(ENV["plantilla"]) == 1)
r_, e = enviar(k1, "plantilla")
check("no asistió usa la plantilla de hora no concretada", r_ and ENV["plantilla"][-1][1] == "recuperar_hora_no_concretada_v1")
r_, e = enviar(k10, "plantilla")
check("sin respuesta usa la plantilla de consulta abierta", r_ and ENV["plantilla"][-1][1] == "recuperar_consulta_abierta_v1")
_, e = enviar("999999999", "texto", "hola")
check("persona fuera de la lista no se puede enviar", e and e.status == 409)

# HTTP: envío y mapeo de errores
r = cli.post(f"/api/recuperar/{rec._k(ph(11))}/enviar?token=recepcion_test",
             json={"modo": "texto", "mensaje": "Hola, te escribimos del Centro Médico Carampangue.", "confirmar": True})
check("HTTP: envío 200", r.status_code == 200 and r.json()["estado_gestion"] == "contactado")
r = cli.post(f"/api/recuperar/{rec._k(ph(11))}/enviar?token=recepcion_test",
             json={"modo": "texto", "mensaje": "otra vez", "confirmar": True})
check("HTTP: segundo envío 409", r.status_code == 409)
r = cli.post(f"/api/recuperar/{rec._k(ph(18))}/enviar?token=recepcion_test", json={"modo": "plantilla"})
check("HTTP: sin confirmar 400", r.status_code == 400)
r = cli.post("/api/recuperar/abc/enviar?token=recepcion_test", json={})
check("HTTP: clave inválida 404", r.status_code == 404)
r = cli.post(f"/api/recuperar/{rec._k(ph(18))}/gestion?token=recepcion_test",
             json={"estado": "contactado", "nota": "Llamé, no contestó", "proximo": ""})
check("HTTP: gestión guarda en la tabla compartida", r.status_code == 200 and r.json()["estado"] == "contactado")

# ── Botones de la plantilla ─────────────────────────────────────────────────
t_ag = rec.procesar_boton(ph(19), rec.PAYLOAD_AGENDAR)
check("botón Agendar hora sigue como 'menu' y deja evento", t_ag == "menu"
      and session.has_recent_event(ph(19), "recuperacion_acepto", 1))
t_no = rec.procesar_boton(ph(19), rec.PAYLOAD_NO_GRACIAS)
with session.db() as c:
    tg = c.execute("SELECT 1 FROM contact_tags WHERE phone=? AND tag=?", (ph(19), rec.TAG_OPT_OUT)).fetchone()
    sg = c.execute("SELECT estado FROM campanas_seguimiento WHERE clave=?", (rec._k(ph(19)),)).fetchone()
check("botón No, gracias: tag propio, evento y gestión cerrada", t_no == rec.PAYLOAD_NO_GRACIAS and tg
      and session.has_recent_event(ph(19), "recuperacion_optout", 1) and sg and sg[0] == "no_interesa")
check("botón No, gracias: sale de la lista", ph(19) not in {p["_phone"] for p in rec.lista(30, AHORA)["personas"]})
mk = session.get_tags(ph(19))
check("botón No, gracias: NO activa el opt-out general de marketing", "marketing_opt_out" not in mk)

# ── El webhook y el flujo conocen los botones ───────────────────────────────
main_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
flows_src = (ROOT / "app" / "flows.py").read_text(encoding="utf-8")
check("webhook: procesa payloads recup_*", "_recup.procesar_boton(phone, _btn_payload)" in main_src
      and "register_recuperacion_routes(app)" in main_src)
check("flujo: responde a recup_no_gracias sin escalar", 'tl == "recup_no_gracias"' in flows_src)

# ── Plantillas (borradores) ─────────────────────────────────────────────────
import upload_templates_to_meta as up  # noqa: E402

tdir = ROOT / "templates" / "whatsapp_templates"
for n in sorted(set(rec.PLANTILLAS.values())):
    f = tdir / f"{n}.DRAFT.json"
    t = json.loads(f.read_text(encoding="utf-8"))
    body = next(c["text"] for c in t["components"] if c["type"] == "BODY")
    btns = next(c for c in t["components"] if c["type"] == "BUTTONS")["buttons"]
    check(f"plantilla {n}: válida para el uploader", up.validate_template(t, f.name) == [])
    check(f"plantilla {n}: MARKETING es_CL, sin claves internas",
          t["category"] == "MARKETING" and t["language"] == "es_CL" and not any(k.startswith("_") for k in t))
    check(f"plantilla {n}: botones Agendar hora / No, gracias", [b["text"] for b in btns] == ["Agendar hora", "No, gracias"])
    low = body.lower()
    check(f"plantilla {n}: sin datos de salud, voseo ni emojis, una sola variable",
          not any(w in low for w in ("psiquiatr", "psicolog", "salud mental", "diagn", "tratamiento", "querés", "tenés"))
          and body.count("{{") == 1 and all(ord(ch) < 0x2600 for ch in body))
    check(f"plantilla {n}: la vista la usa", n in rec.PLANTILLAS.values())
sin_draft = [p.name for p, _ in up.load_templates(None)]
check("uploader: una corrida masiva no toma borradores", not any(n.endswith(".DRAFT.json") for n in sin_draft))
check("uploader: con --file sí se puede subir un borrador",
      up.load_templates("recuperar_hora_pendiente_v1.DRAFT.json")[0][1]["name"] == "recuperar_hora_pendiente_v1")
check("carril automático sigue apagado (flag sin tocar)", "PERSISTENCIA_ACTIVE" in (ROOT / "app" / "persistencia.py").read_text()
      and 'os.getenv("PERSISTENCIA_ACTIVE", "false")' in (ROOT / "app" / "persistencia.py").read_text())

print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
