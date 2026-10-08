"""instagram_sync.py — importa la bandeja de Instagram al panel de recepción.

POR QUÉ EXISTE (8-oct-2026)
  El panel lee la tabla `messages`, y a `messages` solo llega lo que pasa por el
  webhook: los DM de texto del paciente y las respuestas que manda el propio bot
  o el panel. Quedaban fuera (a) todo lo que recepción contesta directo desde la
  app de Instagram (llega como "echo" y el webhook lo descarta), (b) los DM que
  no son texto (fotos, notas de voz, historias, reels), y (c) cualquier mensaje
  que el webhook no alcanzó a entregar. Además, entre el 14-jun y el 8-oct el
  token de envío estaba vencido: las respuestas quedaron guardadas como 'out'
  aunque Meta nunca las entregó.

  Este módulo lee la bandeja con la API "Instagram Login"
  (graph.instagram.com/{v}/me/conversations) y deja en `messages`:
    * lo que falta (INSERT directo, sin pasar por `log_message`), y
    * la marca delivery='failed' en las 'out' que Meta nunca tuvo.

SOLO VISIBILIDAD
  Se inserta con SQL directo: NO dispara push, NO toca `sessions`, NO llama a
  `handle_message`, NO envía nada. El único endpoint de Meta que se usa es GET.
  Lo importado queda con state NULL (los análisis por estado ya filtran
  `state IS NOT NULL`) y wamid = "ig:<id de mensaje de Meta>" (dedupe).

CONTACTOS
  El phone es `ig_<IGSID>`, igual que el webhook (`phone = f"ig_{sender_id}"`);
  el IGSID es el id del OTRO participante de la conversación.

NO LEÍDOS
  Un histórico importado no puede encender cientos de globos de "no leído": los
  entrantes que ya tienen una respuesta posterior, o que tienen más de
  UNREAD_VENTANA_H horas, se dan por vistos (admin_seen). Lo reciente sin
  respuesta queda como no leído: es justo lo que recepción necesita ver.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("bot.ig_sync")

GRAPH_IG = "https://graph.instagram.com/v22.0"
WAMID_PREFIX = "ig:"
WATERMARK_KEY = "ig_sync_watermark"

# Cuánto se retrocede respecto de la marca de agua en cada corrida incremental
# (el dedupe hace inocuo el solape).
MARGEN_INCREMENTAL = timedelta(minutes=30)
# Sin marca de agua (primera corrida del job) solo se mira lo de los últimos días;
# el histórico completo es el backfill explícito.
VENTANA_INICIAL = timedelta(days=3)
# Ventana para emparejar un mensaje de Meta con una fila ya guardada por webhook.
# La fila del webhook se escribe DESPUÉS del created_time de Meta (llegada del
# webhook / fin del turno del bot): tolera 60 s hacia atrás y 5 min hacia adelante.
EMPAREJA_ANTES_S = 60
EMPAREJA_DESPUES_S = 300
# No se importa lo de los últimos minutos: el webhook (y el turno del bot) lo está
# guardando justo ahora y importarlo antes duplicaría la fila. La corrida siguiente
# lo toma (el solape del incremental es de 30 min).
GRACIA_WEBHOOK = timedelta(minutes=3)
# Entrantes más viejos que esto se dan por vistos al importar.
UNREAD_VENTANA_H = 48
# Una 'out' sin par en Meta solo se declara no entregada si ya pasó este rato
# (evita marcar un envío que Meta aún no refleja en la bandeja).
NO_ENTREGADO_GRACIA = timedelta(minutes=30)
# Tope de llamadas por corrida incremental (el backfill no tiene tope, tiene pausa).
MAX_LLAMADAS_INCREMENTAL = 300
# La Conversations API de Instagram admite ~2 llamadas/s por cuenta: más rápido
# devuelve "Application request limit reached" (código 4). Medido 8-oct-2026.
PAUSA_S = 0.6
# Meta solo lista las 800 conversaciones más recientes: la página 17 devuelve
# error 500 (probado con limit=50 y limit=100). Es el tope de la API, no un fallo.
TOPE_LISTA_META = 800
# Códigos de error de Graph que significan "baja la velocidad".
_THROTTLE_CODES = {4, 17, 32, 613, 80002, 80006}

_lock = asyncio.Lock()


class SyncAbortado(Exception):
    """Meta pidió frenar (rate limit) o el token no sirve: se corta la corrida."""


def sync_activo() -> bool:
    """Flag INSTAGRAM_SYNC_ACTIVE (default false). Se lee en cada llamada."""
    return os.getenv("INSTAGRAM_SYNC_ACTIVE", "false").strip().lower() in ("true", "1", "yes")


# ── Utilidades puras ────────────────────────────────────────────────────────

def _parse_meta_ts(s: str) -> datetime:
    """'2026-10-08T18:56:18+0000' → datetime UTC aware."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(s, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"created_time inválido: {s!r}")


def _ts_db(dt: datetime) -> str:
    """Mismo formato que `datetime('now')` de SQLite: UTC, 'YYYY-MM-DD HH:MM:SS'."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _epoch_db(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


_KEY_RE = re.compile(r"[^a-z0-9áéíóúüñ]")


def _clave(texto: str) -> str:
    """Texto comparable: minúsculas, sin espacios, signos, emojis ni markdown.
    El bot normaliza markdown y parte los textos largos en trozos de 900, así que
    la igualdad exacta contra lo que Meta guarda no sirve."""
    return _KEY_RE.sub("", (texto or "").lower())


def _textos_calzan(clave_db: str, clave_meta: str) -> bool:
    if not clave_meta:
        # Mensaje sin texto en Meta (foto, nota de voz): se empareja solo por hora.
        return True
    return (clave_db.startswith(clave_meta[:30]) or clave_meta.startswith(clave_db[:30])
            or clave_meta in clave_db)


def _texto_de_mensaje(m: dict) -> str:
    """Texto a guardar. Los adjuntos sin texto quedan como marcador legible: la
    URL del CDN de Meta vence y no se descarga nada (Ley 21.719: mínimo dato)."""
    texto = (m.get("message") or "").strip()
    if texto:
        return texto
    adj = ((m.get("attachments") or {}).get("data")) or []
    if adj:
        a = adj[0]
        mime = (a.get("mime_type") or "").lower()
        if a.get("image_data") or mime.startswith("image"):
            return "[Imagen]"
        if a.get("video_data") or mime.startswith("video"):
            return "[Video]"
        if mime.startswith("audio"):
            return "[Nota de voz]"
        return "[Archivo adjunto]"
    if m.get("is_unsupported"):
        return "[Mensaje de Instagram no compatible con la API (historia, reel o similar)]"
    return ""


def normalizar_mensaje(m: dict, own_ids: set, own_username: str) -> dict | None:
    """Mensaje crudo de la API → {mid, direction, text, dt, ts}. None si no sirve."""
    try:
        dt = _parse_meta_ts(m.get("created_time", ""))
    except ValueError:
        return None
    mid = m.get("id")
    if not mid:
        return None
    frm = m.get("from") or {}
    es_nuestro = (str(frm.get("id", "")) in own_ids) or (
        own_username and frm.get("username") == own_username)
    texto = _texto_de_mensaje(m)
    if not texto:
        return None   # reacciones y mensajes borrados llegan vacíos: nada que mostrar
    return {"mid": str(mid), "direction": "out" if es_nuestro else "in",
            "text": texto[:2000], "dt": dt, "ts": _ts_db(dt)}


def contacto_de(conv: dict, own_ids: set, own_username: str) -> tuple[str, str] | None:
    """(igsid, username) del otro participante; None si la conversación es rara."""
    otros = [p for p in ((conv.get("participants") or {}).get("data") or [])
             if str(p.get("id", "")) not in own_ids
             and not (own_username and p.get("username") == own_username)]
    if len(otros) != 1 or not otros[0].get("id"):
        return None
    return str(otros[0]["id"]), otros[0].get("username") or ""


# ── Capa HTTP (solo GET) ────────────────────────────────────────────────────

class _Cliente:
    def __init__(self, token: str, max_llamadas: int | None = None):
        self._c = httpx.AsyncClient(timeout=30)
        self._h = {"Authorization": f"Bearer {token}"}
        self.llamadas = 0
        self.max_llamadas = max_llamadas

    async def aclose(self):
        await self._c.aclose()

    async def get(self, url: str, params: dict | None = None, intentos: int = 4) -> dict:
        for intento in range(intentos):
            if self.max_llamadas is not None and self.llamadas >= self.max_llamadas:
                raise SyncAbortado("tope de llamadas por corrida")
            self.llamadas += 1
            await asyncio.sleep(PAUSA_S)
            try:
                r = await self._c.get(url, params=params, headers=self._h)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                log.warning("ig_sync red intento %d: %s", intento + 1, e)
                await asyncio.sleep(2 * (intento + 1))
                continue
            if r.status_code == 200:
                return r.json()
            err = {}
            try:
                err = (r.json() or {}).get("error") or {}
            except Exception:
                pass
            if r.status_code == 429 or err.get("code") in _THROTTLE_CODES:
                espera = 30 * (intento + 1)
                log.warning("ig_sync throttle (%s/%s): espero %ss", r.status_code, err.get("code"), espera)
                if intento == intentos - 1:
                    raise SyncAbortado("rate limit de Meta")
                await asyncio.sleep(espera)
                continue
            if r.status_code in (400, 401, 403):
                raise SyncAbortado(f"{r.status_code} {err.get('message', '')[:120]}")
            if intentos > 1:
                await asyncio.sleep(2 * (intento + 1))
        raise SyncAbortado(f"sin respuesta de Meta tras {intentos} intentos")


async def _conversaciones(cli: _Cliente, desde: datetime | None, estado: dict):
    """Conversaciones de la bandeja, la más reciente primero. Con `desde`, corta
    al llegar a una cuyo updated_time es anterior. Si Meta falla DESPUÉS de haber
    entregado TOPE_LISTA_META conversaciones, es su tope de paginación: se da por
    terminada la lista (estado["lista_truncada"]) en vez de abortar la corrida."""
    url = f"{GRAPH_IG}/me/conversations"
    params = {"platform": "instagram", "fields": "id,updated_time,participants", "limit": 50}
    vistas = 0
    while url:
        try:
            j = await cli.get(url, params)
        except SyncAbortado:
            if vistas >= TOPE_LISTA_META:
                estado["lista_truncada"] = True
                return
            raise
        for conv in j.get("data", []):
            vistas += 1
            if desde and conv.get("updated_time"):
                if _parse_meta_ts(conv["updated_time"]) < desde:
                    return
            yield conv
        url = (j.get("paging") or {}).get("next")
        params = None


POR_PAGINA = 100


async def _mensajes(cli: _Cliente, conv_id: str, desde: datetime | None) -> list[dict]:
    """Mensajes crudos de la conversación (todos, o hasta `desde`), el más nuevo primero.

    Se pagina con el cursor DENTRO del campo (`messages.limit(N).after(C){...}`).
    El `paging.next` que trae la respuesta NO sirve: apunta al borde
    /{conversación}/messages, que responde 403 "rate-limits on the node" siempre
    (probado 8-oct-2026) y además viene aunque la conversación ya esté completa.
    Ojo: la API devuelve hasta limit+1 mensajes, por eso se sigue solo si la
    página vino llena (>= POR_PAGINA)."""
    campos = "{id,from,created_time,message,attachments,is_unsupported}"
    out: list[dict] = []
    cursor = None
    while True:
        pag = f"limit({POR_PAGINA})" + (f".after({cursor})" if cursor else "")
        j = await cli.get(f"{GRAPH_IG}/{conv_id}", {"fields": f"messages.{pag}{campos}"})
        bloque = j.get("messages") or {}
        datos = bloque.get("data", [])
        out.extend(datos)
        cursor = ((bloque.get("paging") or {}).get("cursors") or {}).get("after")
        if len(datos) < POR_PAGINA or not cursor:
            break
        if desde:
            try:
                if _parse_meta_ts(datos[-1]["created_time"]) < desde:
                    break
            except (ValueError, KeyError):
                pass
    return out


async def _identidad(cli: _Cliente) -> tuple[set, str]:
    """Ids y username propios (para decidir in/out y quién es el contacto)."""
    j = await cli.get(f"{GRAPH_IG}/me", {"fields": "user_id,username"})
    ids = {str(j.get(k)) for k in ("user_id", "id") if j.get(k)}
    try:
        from config import INSTAGRAM_USER_ID
        if INSTAGRAM_USER_ID:
            ids.add(str(INSTAGRAM_USER_ID))
    except Exception:
        pass
    return ids, j.get("username") or ""


# ── Capa DB ─────────────────────────────────────────────────────────────────

def _filas_db(conn, phone: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, direction, text, ts, wamid, delivery FROM messages "
        "WHERE phone=? ORDER BY ts, id", (phone,)).fetchall()
    out = []
    for r in rows:
        try:
            ep = _epoch_db(r["ts"])
        except (ValueError, TypeError):
            continue
        out.append({"id": r["id"], "dir": r["direction"], "ep": ep, "k": _clave(r["text"]),
                    "wamid": r["wamid"], "delivery": r["delivery"], "usada": False})
    return out


def planificar_conversacion(filas: list[dict], msgs: list[dict], ahora: datetime,
                            historia_completa: bool, desde_no_entregado: datetime | None) -> dict:
    """Decide qué hacer con una conversación. Pura (sin DB): testeable.

    filas : mensajes que ya hay en `messages` para el phone (de `_filas_db`).
    msgs  : mensajes de Meta ya normalizados.
    Devuelve {insertar:[msg], vincular:[(id_fila, mid)], fallidos:[id_fila]}.
    """
    por_wamid = {f["wamid"]: f for f in filas if f["wamid"]}
    insertar, vincular, pendientes = [], [], []

    # 1) Por id de Meta (importados antes).
    for m in msgs:
        f = por_wamid.get(WAMID_PREFIX + m["mid"])
        if f:
            f["usada"] = True
        else:
            pendientes.append(m)

    # 2) Texto + hora, 1 a 1, contra filas del webhook sin id.
    sin_par = []
    for m in sorted(pendientes, key=lambda x: x["dt"]):
        ep = m["dt"].timestamp()
        km = _clave(m["text"])
        hit = None
        for f in filas:
            if f["usada"] or f["wamid"] or f["dir"] != m["direction"]:
                continue
            if -EMPAREJA_ANTES_S <= f["ep"] - ep <= EMPAREJA_DESPUES_S and _textos_calzan(f["k"], km):
                hit = f
                break
        if hit:
            hit["usada"] = True
            vincular.append((hit["id"], m["mid"]))
        else:
            sin_par.append(m)

    # 3) Trozos de un 'out' largo (el bot parte en 900): ya están en la fila completa.
    for m in sin_par:
        ep = m["dt"].timestamp()
        km = _clave(m["text"])
        cubierto = False
        if m["direction"] == "out" and len(km) >= 20:
            for f in filas:
                if (f["dir"] == "out" and not f["wamid"]
                        and -EMPAREJA_ANTES_S <= f["ep"] - ep <= EMPAREJA_DESPUES_S + 60
                        and km in f["k"]):
                    f["usada"] = True
                    cubierto = True
                    break
        if not cubierto:
            insertar.append(m)

    # 4) 'out' del bot/panel que Meta nunca tuvo. Solo con la historia completa de
    #    la conversación (si no, "no está" podría ser "no lo traje") y pasada la gracia.
    fallidos = []
    if historia_completa and desde_no_entregado is not None and msgs:
        primero = min(m["dt"] for m in msgs).timestamp()
        limite = (ahora - NO_ENTREGADO_GRACIA).timestamp()
        for f in filas:
            if (f["dir"] == "out" and not f["usada"] and not f["wamid"]
                    and f["delivery"] != "failed"
                    and f["ep"] >= desde_no_entregado.timestamp()
                    and primero - EMPAREJA_ANTES_S <= f["ep"] <= limite):
                fallidos.append(f["id"])
    return {"insertar": insertar, "vincular": vincular, "fallidos": fallidos}


def _aplicar(conn, phone: str, plan: dict) -> None:
    for fila_id, mid in plan["vincular"]:
        conn.execute("UPDATE messages SET wamid=? WHERE id=? AND wamid IS NULL",
                     (WAMID_PREFIX + mid, fila_id))
    for m in sorted(plan["insertar"], key=lambda x: x["dt"]):
        conn.execute(
            "INSERT INTO messages (phone, direction, text, state, ts, canal, wamid) "
            "VALUES (?, ?, ?, NULL, ?, 'instagram', ?)",
            (phone, m["direction"], m["text"], m["ts"], WAMID_PREFIX + m["mid"]))
    for fila_id in plan["fallidos"]:
        conn.execute("UPDATE messages SET delivery='failed' WHERE id=?", (fila_id,))


def _dar_por_vistos(conn, phone: str, msgs: list[dict], ahora: datetime) -> bool:
    """Marca como vistos los entrantes con respuesta posterior o de más de
    UNREAD_VENTANA_H horas. Devuelve True si movió admin_seen."""
    ins = [m for m in msgs if m["direction"] == "in"]
    if not ins:
        return False
    ultimo_out = max((m["dt"] for m in msgs if m["direction"] == "out"), default=None)
    limite = ahora - timedelta(hours=UNREAD_VENTANA_H)
    vistos = [m["dt"] for m in ins if m["dt"] < limite or (ultimo_out and m["dt"] < ultimo_out)]
    if not vistos:
        return False
    corte = _ts_db(max(vistos))
    fila = conn.execute("SELECT seen_at FROM admin_seen WHERE phone=?", (phone,)).fetchone()
    if fila and fila["seen_at"] and fila["seen_at"] >= corte:
        return False
    conn.execute(
        "INSERT INTO admin_seen (phone, seen_at, seen_by) VALUES (?, ?, 'ig_sync') "
        "ON CONFLICT(phone) DO UPDATE SET seen_at=excluded.seen_at, seen_by=excluded.seen_by",
        (phone, corte))
    return True


def _necesita_nombre(conn, phone: str) -> tuple[bool, str]:
    """Misma regla que `_profile_needs_name` del webhook. Devuelve (necesita, rut)."""
    r = conn.execute("SELECT nombre, rut FROM contact_profiles WHERE phone=?", (phone,)).fetchone()
    if not r:
        return True, ""
    n = (r["nombre"] or "").strip()
    # "@usuario" es un nombre provisional (no había nombre real): se puede mejorar.
    # Un nombre que recepción editó a mano nunca cae en estas reglas: no se pisa.
    return (not n) or n.startswith(("ig_", "fb_", "@")), (r["rut"] or "")


async def _nombre_de_perfil(cli: _Cliente, igsid: str, username: str) -> str:
    """Nombre del perfil: name > @username > '' (queda el código ig_...)."""
    def _arroba(u):
        u = (u or "").strip().lstrip("@")
        return f"@{u}" if u else ""
    try:
        j = await cli.get(f"{GRAPH_IG}/{igsid}", {"fields": "name,username"}, intentos=2)
        return (j.get("name") or "").strip() or _arroba(j.get("username") or username)
    except SyncAbortado as e:
        # 400/403 acá es "perfil no disponible" (cuenta borrada/restringida), no un
        # motivo para abortar la corrida: se usa el username que ya trae la conversación.
        if "tope" in str(e) or "rate limit" in str(e):
            raise
        return _arroba(username)


# ── Orquestación ────────────────────────────────────────────────────────────

async def sincronizar(modo: str = "incremental", dry_run: bool = False,
                      desde_no_entregado: datetime | None = None,
                      solo_igsid: str | None = None) -> dict:
    """Corre una sincronización.

    modo='incremental': conversaciones/mensajes desde la marca de agua.
    modo='backfill'   : TODA la bandeja; además marca las 'out' sin par en Meta
                        como no entregadas desde `desde_no_entregado`.
    dry_run=True      : cuenta y reporta, no escribe nada.
    """
    if modo not in ("incremental", "backfill"):
        raise ValueError("modo debe ser 'incremental' o 'backfill'")
    from config import META_PAGE_ACCESS_TOKEN
    if not META_PAGE_ACCESS_TOKEN:
        return {"ok": False, "error": "sin META_PAGE_ACCESS_TOKEN"}
    if _lock.locked():
        return {"ok": False, "error": "ya hay una sincronización en curso"}
    async with _lock:
        return await _sincronizar(META_PAGE_ACCESS_TOKEN, modo, dry_run,
                                  desde_no_entregado, solo_igsid)


async def _sincronizar(token: str, modo: str, dry_run: bool,
                       desde_no_entregado: datetime | None, solo_igsid: str | None) -> dict:
    import session
    ahora = datetime.now(timezone.utc)
    backfill = modo == "backfill"
    cli = _Cliente(token, None if backfill else MAX_LLAMADAS_INCREMENTAL)
    st = {"ok": True, "modo": modo, "dry_run": dry_run, "conversaciones": 0,
          "mensajes_meta": 0, "insertados": 0, "insertados_in": 0, "insertados_out": 0,
          "vinculados": 0, "no_entregados": 0, "contactos_nuevos": 0,
          "nombres_guardados": 0, "vistos": 0, "omitidas": 0, "abortado": None}
    estado_lista: dict = {}
    visitados: set = set()
    desde = None
    if not backfill:
        mark = session.system_state_get(WATERMARK_KEY)
        try:
            desde = _parse_meta_ts(mark) - MARGEN_INCREMENTAL if mark else ahora - VENTANA_INICIAL
        except ValueError:
            desde = ahora - VENTANA_INICIAL
    try:
        own_ids, own_user = await _identidad(cli)
        if not own_ids:
            raise SyncAbortado("no se pudo determinar la cuenta propia")
        async for conv in _conversaciones(cli, desde, estado_lista):
            c = contacto_de(conv, own_ids, own_user)
            if not c:
                st["omitidas"] += 1
                continue
            igsid, username = c
            if solo_igsid and igsid != solo_igsid:
                continue
            phone = f"ig_{igsid}"
            visitados.add(phone)
            crudos = await _mensajes(cli, conv["id"], desde)
            msgs = [n for n in (normalizar_mensaje(m, own_ids, own_user) for m in crudos) if n]
            msgs = [m for m in msgs if m["dt"] <= ahora - GRACIA_WEBHOOK]
            if desde:
                msgs = [m for m in msgs if m["dt"] >= desde]
            st["conversaciones"] += 1
            st["mensajes_meta"] += len(msgs)
            if not msgs:
                continue
            with session.db() as conn:
                filas = _filas_db(conn, phone)
                plan = planificar_conversacion(filas, msgs, ahora, backfill, desde_no_entregado)
                nuevo = not filas
                st["insertados"] += len(plan["insertar"])
                st["insertados_in"] += sum(1 for m in plan["insertar"] if m["direction"] == "in")
                st["insertados_out"] += sum(1 for m in plan["insertar"] if m["direction"] == "out")
                st["vinculados"] += len(plan["vincular"])
                st["no_entregados"] += len(plan["fallidos"])
                st["contactos_nuevos"] += 1 if (nuevo and plan["insertar"]) else 0
                necesita, rut = _necesita_nombre(conn, phone)
                if dry_run:
                    continue
                _aplicar(conn, phone, plan)
                if _dar_por_vistos(conn, phone, msgs, ahora):
                    st["vistos"] += 1
            if necesita and not dry_run:
                nombre = await _nombre_de_perfil(cli, igsid, username)
                if nombre and nombre != igsid:
                    session.save_profile(phone, rut, nombre)
                    st["nombres_guardados"] += 1
        # Solo en backfill: en incremental re-pedía el perfil de los ~300 contactos con
        # @username cada 10 min (el perfil falla ~40%), agotaba MAX_LLAMADAS y abortaba
        # sin avanzar el watermark. Los contactos tocados ya resuelven nombre arriba.
        if backfill and not dry_run and not solo_igsid:
            await _nombres_pendientes(cli, session, st, visitados)
    except SyncAbortado as e:
        st["abortado"] = str(e)
        log.warning("ig_sync abortado: %s", e)
    finally:
        await cli.aclose()
    st["llamadas"] = cli.llamadas
    st["lista_truncada"] = bool(estado_lista.get("lista_truncada"))
    if not dry_run and not st["abortado"] and not solo_igsid:
        session.system_state_set(WATERMARK_KEY, ahora.strftime("%Y-%m-%dT%H:%M:%S+0000"))
    st["ok"] = st["abortado"] is None
    log.info("ig_sync %s: %s", modo, {k: v for k, v in st.items() if v})
    return st


async def _nombres_pendientes(cli: _Cliente, session, st: dict, visitados: set) -> None:
    """Contactos ig_* ya existentes sin nombre que la bandeja no devolvió (la lista
    de Meta corta en 800 conversaciones): se les pide el perfil directo."""
    with session.db() as conn:
        phones = [r["phone"] for r in conn.execute(
            "SELECT DISTINCT m.phone FROM messages m LEFT JOIN contact_profiles p ON p.phone=m.phone "
            "WHERE m.phone LIKE 'ig\\_%' ESCAPE '\\' AND (p.nombre IS NULL OR trim(p.nombre)='' "
            "OR p.nombre LIKE 'ig\\_%' ESCAPE '\\' OR p.nombre LIKE '@%')").fetchall()]
    for phone in phones:
        if phone in visitados:
            continue
        with session.db() as conn:
            necesita, rut = _necesita_nombre(conn, phone)
        if not necesita:
            continue
        nombre = await _nombre_de_perfil(cli, phone[3:], "")
        if nombre:
            session.save_profile(phone, rut, nombre)
            st["nombres_guardados"] += 1


async def job_instagram_sync() -> None:
    """Job APScheduler (cada 10 min). No-op con INSTAGRAM_SYNC_ACTIVE=false."""
    if not sync_activo():
        log.debug("job_instagram_sync: INSTAGRAM_SYNC_ACTIVE=false — skip")
        return
    try:
        await sincronizar("incremental")
    except Exception as e:  # noqa: BLE001
        log.error("job_instagram_sync fallo: %s", e)
