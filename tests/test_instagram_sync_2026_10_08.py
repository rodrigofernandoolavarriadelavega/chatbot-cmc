"""Sincronización de la bandeja de Instagram al panel (8-oct-2026).

Cubre:
  1. Utilidades puras: hora de Meta -> formato de la tabla, in/out, contacto,
     adjuntos sin texto.
  2. planificar_conversacion: empareja con lo que ya entró por webhook (texto +
     hora), no duplica, inserta lo que falta (echoes de recepción), cubre los
     trozos de un 'out' largo, y marca como no entregado SOLO lo que Meta no
     tiene (y solo con historia completa y pasada la gracia).
  3. Integración con una DB temporal y Meta simulado: idempotencia (2ª corrida
     no inserta nada), phone `ig_<IGSID>`, state NULL, wamid `ig:<id>`, nada de
     push ni respuestas del bot, no leídos controlados, dry_run no escribe,
     nombre del perfil sin pisar el RUT.
  4. Panel: /admin/api/conversations lista IG con su canal, el chat sale en
     orden cronológico aunque la historia se haya importado después, y
     `delivery` llega al front.
  5. El job está registrado con misfire_grace_time y detrás de la bandera.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("SQLCIPHER_KEY", "")

import session as S  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="cmc_test_igsync_")) / "s.db"
S.DB_PATH = _TMP

import instagram_sync as IG  # noqa: E402

OWN = "17841428972140588"
OWN_USER = "centromedicocarampangue"
IGSID = "1407207674380928"
PHONE = f"ig_{IGSID}"
UTC = timezone.utc


def _dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _meta(mid, who, text, ts, **extra):
    """Mensaje crudo como lo devuelve la API."""
    frm = ({"id": OWN, "username": OWN_USER} if who == "out" else {"id": IGSID, "username": "paciente.ig"})
    m = {"id": mid, "from": frm, "created_time": _dt(ts).strftime("%Y-%m-%dT%H:%M:%S+0000"),
         "message": text}
    m.update(extra)
    return m


def _norm(*crudos):
    return [IG.normalizar_mensaje(m, {OWN}, OWN_USER) for m in crudos]


def _fila(id_, dir_, texto, ts, wamid=None, delivery=None):
    return {"id": id_, "dir": dir_, "ep": _dt(ts).timestamp(), "k": IG._clave(texto),
            "wamid": wamid, "delivery": delivery, "usada": False}


AHORA = _dt("2026-10-08 19:00:00")


# ── 1. Utilidades puras ─────────────────────────────────────────────────────

def test_hora_de_meta_queda_en_formato_de_la_tabla():
    dt = IG._parse_meta_ts("2026-10-08T18:56:18+0000")
    assert IG._ts_db(dt) == "2026-10-08 18:56:18"
    # con offset distinto se normaliza a UTC, como datetime('now')
    assert IG._ts_db(IG._parse_meta_ts("2026-10-08T15:56:18-0300")) == "2026-10-08 18:56:18"


def test_direccion_por_remitente():
    entra, sale = _norm(_meta("a", "in", "Hola", "2026-10-08 10:00:00"),
                        _meta("b", "out", "Buenas", "2026-10-08 10:01:00"))
    assert entra["direction"] == "in" and sale["direction"] == "out"


def test_contacto_es_el_otro_participante():
    conv = {"participants": {"data": [{"id": OWN, "username": OWN_USER},
                                      {"id": IGSID, "username": "paciente.ig"}]}}
    assert IG.contacto_de(conv, {OWN}, OWN_USER) == (IGSID, "paciente.ig")
    assert IG.contacto_de({"participants": {"data": [{"id": OWN}]}}, {OWN}, OWN_USER) is None


def test_adjuntos_sin_texto_quedan_con_marcador():
    img = IG.normalizar_mensaje(_meta("a", "in", "", "2026-10-08 10:00:00",
                                      attachments={"data": [{"image_data": {"url": "x"}}]}), {OWN}, OWN_USER)
    assert img["text"] == "[Imagen]"
    audio = IG.normalizar_mensaje(_meta("b", "in", "", "2026-10-08 10:00:00",
                                        attachments={"data": [{"mime_type": "audio/mp4"}]}), {OWN}, OWN_USER)
    assert audio["text"] == "[Nota de voz]"
    vacio = IG.normalizar_mensaje(_meta("c", "in", "", "2026-10-08 10:00:00"), {OWN}, OWN_USER)
    assert vacio is None   # reacción/borrado: nada que mostrar


# ── 2. Planificación ────────────────────────────────────────────────────────

def test_empareja_con_lo_que_ya_entro_por_webhook():
    filas = [_fila(1, "in", "Hola, quiero hora", "2026-10-08 10:00:20")]   # webhook llega 20 s después
    msgs = _norm(_meta("m1", "in", "Hola, quiero hora", "2026-10-08 10:00:00"))
    plan = IG.planificar_conversacion(filas, msgs, AHORA, False, None)
    assert plan["insertar"] == [] and plan["vincular"] == [(1, "m1")]


def test_fuera_de_la_ventana_no_empareja_y_se_inserta():
    filas = [_fila(1, "in", "Hola", "2026-10-08 10:30:00")]
    msgs = _norm(_meta("m1", "in", "Hola", "2026-10-08 10:00:00"))
    plan = IG.planificar_conversacion(filas, msgs, AHORA, False, None)
    assert len(plan["insertar"]) == 1 and plan["vincular"] == []


def test_dos_hola_iguales_se_emparejan_uno_a_uno():
    filas = [_fila(1, "in", "Hola", "2026-10-08 10:00:05")]
    msgs = _norm(_meta("m1", "in", "Hola", "2026-10-08 10:00:00"),
                 _meta("m2", "in", "Hola", "2026-10-08 10:00:30"))
    plan = IG.planificar_conversacion(filas, msgs, AHORA, False, None)
    assert len(plan["vincular"]) == 1 and len(plan["insertar"]) == 1


def test_lo_que_recepcion_contesto_desde_la_app_se_inserta():
    msgs = _norm(_meta("m1", "in", "Tienen hora?", "2026-10-08 10:00:00"),
                 _meta("m2", "out", "Sí, mañana a las 10", "2026-10-08 10:05:00"))
    filas = [_fila(1, "in", "Tienen hora?", "2026-10-08 10:00:02")]
    plan = IG.planificar_conversacion(filas, msgs, AHORA, False, None)
    assert [m["mid"] for m in plan["insertar"]] == ["m2"]


def test_ya_importado_no_se_repite():
    filas = [_fila(1, "out", "Sí, mañana", "2026-10-08 10:05:00", wamid="ig:m2")]
    msgs = _norm(_meta("m2", "out", "Sí, mañana", "2026-10-08 10:05:00"))
    plan = IG.planificar_conversacion(filas, msgs, AHORA, False, None)
    assert plan == {"insertar": [], "vincular": [], "fallidos": []}


def test_out_largo_partido_en_trozos_no_se_duplica():
    largo = ("Hola, soy el asistente del Centro Médico Carampangue. " * 20).strip()
    filas = [_fila(1, "out", largo, "2026-10-08 10:00:10")]
    k = largo.replace("*", "")
    msgs = _norm(_meta("t1", "out", k[:450], "2026-10-08 10:00:08"),
                 _meta("t2", "out", k[450:900], "2026-10-08 10:00:09"))
    plan = IG.planificar_conversacion(filas, msgs, AHORA, False, None)
    assert plan["insertar"] == []


def test_no_entregado_solo_lo_que_meta_no_tiene():
    desde = _dt("2026-06-14 00:00:00")
    filas = [
        _fila(1, "in", "Hola", "2026-09-01 10:00:00"),
        _fila(2, "out", "Respuesta que SÍ llegó", "2026-09-01 10:00:20"),
        _fila(3, "out", "Respuesta que NO llegó", "2026-09-01 10:05:00"),
        _fila(4, "out", "Antes del quiebre", "2026-05-01 10:00:00"),
    ]
    msgs = _norm(_meta("m1", "in", "Hola", "2026-09-01 10:00:00"),
                 _meta("m2", "out", "Respuesta que sí llegó", "2026-09-01 10:00:18"))
    plan = IG.planificar_conversacion(filas, msgs, AHORA, True, desde)
    assert plan["fallidos"] == [3]


def test_no_entregado_exige_historia_completa_y_gracia():
    desde = _dt("2026-06-14 00:00:00")
    filas = [_fila(1, "out", "Sin par", "2026-09-01 10:05:00"),
             _fila(2, "out", "Recién enviado", "2026-10-08 18:50:00")]
    msgs = _norm(_meta("m1", "in", "Hola", "2026-09-01 10:00:00"))
    # incremental (historia parcial): nunca marca
    assert IG.planificar_conversacion(filas, msgs, AHORA, False, desde)["fallidos"] == []
    # backfill: marca la vieja, no la de hace 10 minutos (Meta aún puede no reflejarla)
    assert IG.planificar_conversacion(filas, msgs, AHORA, True, desde)["fallidos"] == [1]


# ── 3. Integración con DB temporal y Meta simulado ──────────────────────────

class FakeMeta:
    """Reemplaza _Cliente.get: sirve /me, /me/conversations, /{conv}, /{igsid}."""
    def __init__(self, convs, perfil=None):
        self.convs = convs       # {conv_id: {"updated": ts, "msgs": [crudos]}}
        self.perfil = perfil or {}
        self.pedidos = []

    async def get(self, url, params=None):
        self.pedidos.append(url)
        if url.endswith("/me"):
            return {"user_id": OWN, "username": OWN_USER}
        if url.endswith("/me/conversations"):
            data = [{"id": cid, "updated_time": _dt(c["updated"]).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                     "participants": {"data": [{"id": OWN, "username": OWN_USER},
                                               {"id": c.get("igsid", IGSID), "username": "paciente.ig"}]}}
                    for cid, c in sorted(self.convs.items(), key=lambda x: x[1]["updated"], reverse=True)]
            return {"data": data}
        for cid, c in self.convs.items():
            if url.endswith("/" + cid):
                # Igual que Meta: paginación con cursor dentro del campo, hasta limit+1
                # por página, y un `paging.next` ENGAÑOSO (siempre presente).
                f = params["fields"]
                lim = int(re.search(r"limit\((\d+)\)", f).group(1))
                despues = re.search(r"after\((\d+)\)", f)
                orden = sorted(c["msgs"], key=lambda m: m["created_time"], reverse=True)
                ini = int(despues.group(1)) if despues else 0
                pag = orden[ini:ini + lim + 1]
                self.paginas = getattr(self, "paginas", 0) + 1
                return {"messages": {"data": pag, "paging": {
                    "cursors": {"after": str(ini + len(pag))}, "next": "https://engañoso"}}}
        return self.perfil


@pytest.fixture()
def db(monkeypatch):
    # Otros tests de la suite cambian S.DB_PATH o reemplazan sys.modules["session"]
    # por un doble: se fija aquí lo que ESTE test necesita y se restaura al salir.
    monkeypatch.setitem(sys.modules, "session", S)
    monkeypatch.setattr(S, "DB_PATH", _TMP)
    with S.db() as conn:
        for t in ("messages", "sessions", "contact_profiles", "admin_seen", "system_state"):
            conn.execute(f"DELETE FROM {t}")
    monkeypatch.setenv("META_PAGE_ACCESS_TOKEN", "tok")
    import config
    monkeypatch.setattr(config, "META_PAGE_ACCESS_TOKEN", "tok")
    # Nada de lo importado puede pasar por log_message (dispara push) ni por el bot.
    def _prohibido(*a, **k):
        raise AssertionError("instagram_sync no debe usar log_message")
    monkeypatch.setattr(S, "log_message", _prohibido)
    return S


def _correr(monkeypatch, meta, **kw):
    monkeypatch.setattr(IG._Cliente, "get", lambda self, url, params=None, **k: meta.get(url, params))
    monkeypatch.setattr(IG, "PAUSA_S", 0)
    return asyncio.run(IG.sincronizar(**kw))


def _msgs(phone=PHONE):
    with S.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM messages WHERE phone=? ORDER BY ts, id", (phone,)).fetchall()]


def _conv_ejemplo():
    ahora = datetime.now(UTC).replace(microsecond=0)
    t = lambda **k: (ahora - timedelta(**k)).strftime("%Y-%m-%d %H:%M:%S")
    return {"c1": {"updated": t(minutes=5), "msgs": [
        _meta("m1", "in", "Hola, precio de limpieza dental?", t(days=20)),
        _meta("m2", "out", "Hola! La limpieza cuesta $30.000", t(days=20, minutes=-3)),
        _meta("m3", "in", "Gracias, y tienen hora?", t(days=5)),
        _meta("m4", "in", "", t(minutes=6), attachments={"data": [{"image_data": {"url": "x"}}]}),
    ]}}


def test_backfill_importa_y_es_idempotente(db, monkeypatch):
    meta = FakeMeta(_conv_ejemplo(), perfil={"name": "María Pérez", "username": "paciente.ig"})
    r1 = _correr(monkeypatch, meta, modo="backfill")
    assert r1["ok"] and r1["insertados"] == 4 and r1["contactos_nuevos"] == 1
    filas = _msgs()
    assert len(filas) == 4
    assert all(f["canal"] == "instagram" and f["state"] is None and f["wamid"].startswith("ig:") for f in filas)
    assert {f["direction"] for f in filas} == {"in", "out"}
    assert any(f["text"] == "[Imagen]" for f in filas)
    # 2ª corrida: cero inserciones
    r2 = _correr(monkeypatch, meta, modo="backfill")
    assert r2["insertados"] == 0 and r2["vinculados"] == 0 and len(_msgs()) == 4


def test_no_duplica_lo_que_entro_por_webhook(db, monkeypatch):
    conv = _conv_ejemplo()
    # el webhook guardó m1 con la hora de llegada (4 s después) y sin wamid
    ts1 = _dt(IG._ts_db(IG._parse_meta_ts(conv["c1"]["msgs"][0]["created_time"]))) + timedelta(seconds=4)
    with S.db() as c:
        c.execute("INSERT INTO messages (phone, direction, text, state, ts, canal) VALUES (?,?,?,?,?,?)",
                  (PHONE, "in", "Hola, precio de limpieza dental?", "IDLE",
                   ts1.strftime("%Y-%m-%d %H:%M:%S"), "instagram"))
    r = _correr(monkeypatch, FakeMeta(conv), modo="backfill")
    assert r["insertados"] == 3 and r["vinculados"] == 1
    assert len(_msgs()) == 4
    assert sum(1 for f in _msgs() if f["text"].startswith("Hola, precio")) == 1


def test_dry_run_no_escribe(db, monkeypatch):
    r = _correr(monkeypatch, FakeMeta(_conv_ejemplo()), modo="backfill", dry_run=True)
    assert r["insertados"] == 4 and r["dry_run"]
    assert _msgs() == []
    assert S.system_state_get(IG.WATERMARK_KEY) is None


def test_no_leidos_historico_visto_y_reciente_sin_responder_pendiente(db, monkeypatch):
    _correr(monkeypatch, FakeMeta(_conv_ejemplo()), modo="backfill")
    sin_leer = S.get_unread_counts()
    # El entrante de hace 20 días (con respuesta) y el de hace 5 días (viejo) quedan vistos;
    # la foto de hace 6 minutos sin responder queda como no leída.
    assert sin_leer.get(PHONE) == 1


def test_nombre_del_perfil_sin_pisar_el_rut(db, monkeypatch):
    S.save_profile(PHONE, "12345678-5", "")
    _correr(monkeypatch, FakeMeta(_conv_ejemplo(), perfil={"name": "María Pérez"}), modo="backfill")
    p = S.get_profile(PHONE)
    assert p["nombre"] == "María Pérez" and p["rut"] == "12345678-5"


def test_nombre_real_existente_no_se_pisa(db, monkeypatch):
    S.save_profile(PHONE, "", "Ana Soto")
    _correr(monkeypatch, FakeMeta(_conv_ejemplo(), perfil={"name": "Otro Nombre"}), modo="backfill")
    assert S.get_profile(PHONE)["nombre"] == "Ana Soto"


def test_sin_nombre_real_queda_el_username_con_arroba(db, monkeypatch):
    _correr(monkeypatch, FakeMeta(_conv_ejemplo(), perfil={"username": "maria.marita"}), modo="backfill")
    assert S.get_profile(PHONE)["nombre"] == "@maria.marita"


def test_arroba_provisional_se_mejora_con_el_nombre_real_pero_lo_editado_a_mano_no(db, monkeypatch):
    S.save_profile(PHONE, "", "@maria.marita")
    _correr(monkeypatch, FakeMeta(_conv_ejemplo(), perfil={"name": "María Pérez"}), modo="backfill")
    assert S.get_profile(PHONE)["nombre"] == "María Pérez"
    S.save_profile(PHONE, "", "María P. (editado por recepción)")
    _correr(monkeypatch, FakeMeta(_conv_ejemplo(), perfil={"name": "Otra Cosa"}), modo="backfill")
    assert S.get_profile(PHONE)["nombre"] == "María P. (editado por recepción)"


def test_contacto_fuera_de_la_lista_de_meta_igual_recibe_nombre(db, monkeypatch):
    # 16 de los 254 contactos de la DB no salen en la bandeja (tope de 800): se les pide el perfil directo
    viejo = "ig_999000111"
    with S.db() as c:
        c.execute("INSERT INTO messages (phone, direction, text, state, ts, canal) VALUES (?,?,?,?,?,?)",
                  (viejo, "in", "Hola", "IDLE", "2026-05-01 10:00:00", "instagram"))
    _correr(monkeypatch, FakeMeta(_conv_ejemplo(), perfil={"name": "Pedro Soto"}), modo="backfill")
    assert S.get_profile(viejo)["nombre"] == "Pedro Soto"


def test_backfill_marca_no_entregadas_las_out_sin_par(db, monkeypatch):
    conv = _conv_ejemplo()
    ts_mala = (datetime.now(UTC) - timedelta(days=19)).strftime("%Y-%m-%d %H:%M:%S")
    with S.db() as c:
        c.execute("INSERT INTO messages (phone, direction, text, state, ts, canal) VALUES (?,?,?,?,?,?)",
                  (PHONE, "out", "Respuesta del bot que Meta jamás entregó", "IDLE", ts_mala, "instagram"))
    r = _correr(monkeypatch, FakeMeta(conv), modo="backfill",
                desde_no_entregado=datetime.now(UTC) - timedelta(days=60))
    assert r["no_entregados"] == 1
    malas = [f for f in _msgs() if f["delivery"] == "failed"]
    assert len(malas) == 1 and malas[0]["text"].startswith("Respuesta del bot")
    # ya marcada: la siguiente corrida no la cuenta otra vez
    assert _correr(monkeypatch, FakeMeta(conv), modo="backfill",
                   desde_no_entregado=datetime.now(UTC) - timedelta(days=60))["no_entregados"] == 0


def test_incremental_usa_marca_de_agua_y_no_baja_todo(db, monkeypatch):
    meta = FakeMeta(_conv_ejemplo())
    _correr(monkeypatch, meta, modo="backfill")
    assert S.system_state_get(IG.WATERMARK_KEY)
    r = _correr(monkeypatch, meta, modo="incremental")
    assert r["ok"] and r["insertados"] == 0


def test_abortado_por_meta_no_avanza_la_marca_de_agua(db, monkeypatch):
    class Roto(FakeMeta):
        async def get(self, url, params=None):
            if url.endswith("/me"):
                return await super().get(url, params)
            raise IG.SyncAbortado("rate limit de Meta")
    r = _correr(monkeypatch, Roto({}), modo="incremental")
    assert not r["ok"] and r["abortado"]
    assert S.system_state_get(IG.WATERMARK_KEY) is None


def test_conversacion_larga_se_pagina_con_el_cursor_y_no_con_next(db, monkeypatch):
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(days=30)
    msgs = [_meta(f"x{i}", "in" if i % 2 == 0 else "out", f"mensaje {i}",
                  (base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")) for i in range(250)]
    meta = FakeMeta({"c9": {"updated": (base + timedelta(minutes=250)).strftime("%Y-%m-%d %H:%M:%S"), "msgs": msgs}})
    r = _correr(monkeypatch, meta, modo="backfill")
    assert r["insertados"] == 250 and len(_msgs()) == 250
    assert meta.paginas == 3 and "https://engañoso" not in meta.pedidos


def test_tope_de_800_conversaciones_de_meta_no_aborta_la_corrida(db, monkeypatch):
    monkeypatch.setattr(IG, "TOPE_LISTA_META", 1)

    class ConTope(FakeMeta):
        async def get(self, url, params=None):
            if url.endswith("/me/conversations"):
                j = await super().get(url, params)
                j["paging"] = {"next": "https://graph.instagram.com/v22.0/pagina2"}
                return j
            if url.endswith("/pagina2"):
                raise IG.SyncAbortado("sin respuesta de Meta tras 4 intentos")
            return await super().get(url, params)
    r = _correr(monkeypatch, ConTope(_conv_ejemplo()), modo="backfill")
    assert r["ok"] and r["lista_truncada"] and r["insertados"] == 4


def test_lo_de_los_ultimos_minutos_se_deja_al_webhook(db, monkeypatch):
    ahora = datetime.now(UTC).replace(microsecond=0)
    t = lambda **k: (ahora - timedelta(**k)).strftime("%Y-%m-%d %H:%M:%S")
    conv = {"c1": {"updated": t(seconds=20), "msgs": [
        _meta("v", "in", "Hola de hace una hora", t(hours=1)),
        _meta("n", "in", "Hola de hace 20 segundos", t(seconds=20))]}}
    r = _correr(monkeypatch, FakeMeta(conv), modo="backfill")
    assert r["insertados"] == 1
    assert [f["text"] for f in _msgs()] == ["Hola de hace una hora"]


def test_job_respeta_la_bandera(monkeypatch):
    llamadas = []

    async def falso(*a, **k):
        llamadas.append(a)
    monkeypatch.setattr(IG, "sincronizar", falso)
    monkeypatch.delenv("INSTAGRAM_SYNC_ACTIVE", raising=False)
    asyncio.run(IG.job_instagram_sync())
    assert llamadas == []
    monkeypatch.setenv("INSTAGRAM_SYNC_ACTIVE", "true")
    asyncio.run(IG.job_instagram_sync())
    assert llamadas == [("incremental",)]


# ── 4. Panel ────────────────────────────────────────────────────────────────

def test_panel_lista_ig_ordena_cronologico_y_expone_delivery(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import admin_routes
    # WhatsApp "normal" y un IG con historia importada DESPUÉS (id mayor, ts menor)
    with S.db() as c:
        c.execute("INSERT INTO messages (phone, direction, text, state, ts, canal) VALUES "
                  "('56911112222','in','hola','IDLE','2026-10-08 12:00:00','whatsapp')")
        c.execute("INSERT INTO messages (phone, direction, text, state, ts, canal, wamid) VALUES "
                  "(?, 'in', 'mensaje reciente', 'IDLE', '2026-10-08 18:00:00', 'instagram', NULL)", (PHONE,))
        c.execute("INSERT INTO messages (phone, direction, text, state, ts, canal, wamid, delivery) VALUES "
                  "(?, 'out', 'respuesta vieja no entregada', NULL, '2026-06-20 10:00:00', 'instagram', 'ig:x', 'failed')",
                  (PHONE,))
        c.execute("INSERT INTO messages (phone, direction, text, state, ts, canal, wamid) VALUES "
                  "(?, 'in', 'mensaje antiguo', NULL, '2026-06-20 09:59:00', 'instagram', 'ig:y')", (PHONE,))
    import config
    app = FastAPI()
    app.include_router(admin_routes.router)
    cli = TestClient(app)
    h = {"Authorization": f"Bearer {config.ADMIN_TOKEN or 'x'}"}
    if not config.ADMIN_TOKEN:
        monkeypatch.setattr(admin_routes, "_is_admin_token", lambda t: t == "x")
    admin_routes._CONV_CACHE["body"] = None
    convs = cli.get("/admin/api/conversations", headers=h).json()
    ig = [c for c in convs if c["phone"] == PHONE]
    assert len(ig) == 1
    assert ig[0]["canal"] == "instagram" and ig[0]["last_text"] == "mensaje reciente"
    chat = cli.get(f"/admin/api/conversations/{PHONE}", headers=h).json()
    assert [m["text"] for m in chat] == ["mensaje antiguo", "respuesta vieja no entregada", "mensaje reciente"]
    assert [m["delivery"] for m in chat] == [None, "failed", None]


def test_template_v2_marca_los_no_entregados():
    html = (ROOT / "templates" / "admin_v2.html").read_text(encoding="utf-8")
    assert 'm.delivery === "failed"' in html and "No entregado" in html
    assert "channelOf(phone)" in html and 'startsWith("ig_")' in html


# ── 5. Registro del job y envío del bot ─────────────────────────────────────

def test_job_registrado_con_misfire_grace_time():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    i = main.index("job_instagram_sync,\n        \"interval\"")
    bloque = main[i:i + 260]
    assert 'id="instagram_sync"' in bloque and "misfire_grace_time" in bloque and "coalesce=True" in bloque


def test_bot_marca_failed_cuando_el_envio_ig_falla():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert 'delivery="failed" if _entregado is False else None' in main


def test_modulo_solo_usa_get():
    src = (ROOT / "app" / "instagram_sync.py").read_text(encoding="utf-8")
    assert not re.search(r"\.(post|put|delete|patch)\(", src)
    assert "log_message" not in re.sub(r'"""[\s\S]*?"""', "", src).replace("log_message`", "")
