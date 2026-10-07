# -*- coding: utf-8 -*-
"""Portal del Profesional — `/portal-profesional` (HTML) + `/portal-profesional/api/*`.

QUÉ ES
------
La vista de trabajo donde profesionales y recepción del CMC ven lo que el
PACIENTE subió voluntariamente desde su Portal del Paciente:

  - Mediciones (presión, glicemia, peso, temperatura) → `patient_vitals`
  - Exámenes publicados en su portal → `portal_examenes` (publicado=1; los
    carga y publica el centro desde /portal/examenes-admin)
  - Datos que el paciente informó → `contact_profiles` (fila del MISMO RUT)
  - Familia y representación → `family_links`
  - Actividad en el portal (ingresos, check-in "voy en camino", agendó/anuló
    desde el portal, vio exámenes…) → `conversation_events` cuyo meta trae
    el RUT del paciente.

REGLAS (Ley 21.719 — datos de salud sensibles)
----------------------------------------------
1. SOLO LECTURA. Nada de este router escribe datos del paciente.
2. Cada acceso queda en `portal_pro_accesos` (quién, qué paciente, qué acción,
   cuándo, desde qué IP). Si la auditoría no se puede escribir, NO se
   entregan datos (fail-closed, 503).
3. Sin exportes masivos: no hay CSV ni "descargar todo". La única salida es
   el resumen imprimible de UN paciente, y queda auditado.
4. Sin datos de terceros que compartan teléfono: todo se busca por RUT, nunca
   por teléfono (un celular puede ser de toda una familia). Los eventos del
   bot que solo traen teléfono (p.ej. confirmaciones por WhatsApp) NO se
   muestran aquí por eso.
5. Sin diagnósticos ni semáforos automáticos: se muestran valores crudos con
   fecha. La única marca permitida es "fuera del rango de referencia
   registrado" en exámenes, y solo cuando el examen trae ese rango.
6. Tope de fichas por hora por usuario (anti-scraping).

AUTH Y ALCANCE
--------------
`alma_scope.resolve(..., "portal_pro")`:
  - OLACORE_TOKEN (dueño) y ADMIN_TOKEN / cookie admin (recepción): ven todo.
  - Perfil con `profesional_id` y "portal_pro" en su `modulos`: ve SOLO a sus
    pacientes = RUT con cita o atención con él en los últimos 12 meses o con
    cita en los próximos 90 días (BI: fact_citas + fact_atenciones). Si la BI
    no responde, no ve a nadie (fail-closed) y la pantalla lo dice.
  - Hoy NINGÚN perfil de profesional trae "portal_pro" en `modulos`: el dueño
    decide a quién habilitar (config.ALMA_PROFILES). Sin eso, 403.

DEMO
----
`?demo=1` en la página y en la API → datos FICTICIOS generados en memoria
(RUT 50.xxx.xxx), sin auth, sin tocar ninguna base. Sirve para mostrar el
producto sin exponer pacientes reales.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

import alma_scope
import config
from session import db

log = logging.getLogger("portal_profesional")

router = APIRouter(tags=["portal_profesional"])

MODULO = "portal_pro"
_CL = ZoneInfo("America/Santiago")
_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "portal_profesional.html"
_NO_STORE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}

FICHAS_POR_HORA = 120          # tope anti-scraping por usuario
VENTANA_PROPIOS_PASADO = 365   # días hacia atrás para "mis pacientes"
VENTANA_PROPIOS_FUTURO = 90    # días hacia adelante (citas agendadas)
_TIPOS_VITAL = ("presion", "glicemia", "peso", "temperatura")

# Eventos del portal que se muestran como actividad. Todos llevan el RUT en el
# meta (rut / owner / dependent). Se listan explícitos para usar el índice
# (event, ts) de conversation_events.
try:
    from portal_routes import _EVENTOS_PORTAL as _EV_CLIENTE
except Exception:  # pragma: no cover — import tardío / tests aislados
    _EV_CLIENTE = {"wiz_abre", "wiz_esp", "wiz_slot", "wiz_exito", "wiz_error",
                   "cita_cambia_inicia", "cita_cambia_exito", "cita_anula",
                   "checkin", "fz", "examenes_ver", "offline", "magic_link_pide"}

EVENTO_LABEL = {
    "portal_login": "Entró a su portal",
    "portal_login_magic_link": "Entró a su portal (enlace por WhatsApp)",
    "portal_checkin_confirmado": "Confirmó que viene a su hora de hoy",
    "portal_perfil_actualizado": "Actualizó sus datos",
    "portal_family_add_minor": "Vinculó a un menor a su cargo",
    "portal_family_add_adult": "Vinculó a un familiar adulto (con código)",
    "portal_family_revoke": "Quitó un vínculo familiar",
    "portal_family_switch": "Cambió de perfil familiar",
    "portal_examen_cargado": "El centro publicó exámenes en su portal",
    "portal_wiz_exito": "Agendó una hora desde el portal",
    "portal_cita_cambia_exito": "Cambió una hora desde el portal",
    "portal_cita_anula": "Anuló una hora desde el portal",
    "portal_examenes_ver": "Revisó sus exámenes",
    "portal_checkin": "Abrió el check-in del día",
}
# Eventos que cuentan como "novedad" (información nueva para el profesional).
# Ingresos y navegación son actividad, no novedad.
_EV_NOVEDAD_CHECKIN = {"portal_checkin_confirmado"}
_EV_NOVEDAD_PERFIL = {"portal_perfil_actualizado"}
_EV_ACTIVIDAD = sorted(set(EVENTO_LABEL) | {f"portal_{e}" for e in _EV_CLIENTE})

METODO_LABEL = {
    "otp": ("Verificado con código", "ok"),
    "tutor_declaration": ("Declaración de tutor (menor de edad)", "info"),
    "declared": ("Declarado al agendar por WhatsApp · sin verificar", "warn"),
}


# ══ Utilidades ═══════════════════════════════════════════════════════════════

def _key(rut: str | None) -> str:
    """RUT canónico para comparar: sin puntos ni guion, en mayúscula."""
    return "".join(ch for ch in (rut or "").upper() if ch.isdigit() or ch == "K")


def _sql_key(col: str) -> str:
    return f"upper(replace(replace({col},'.',''),'-',''))"


def _fmt_rut(k: str) -> str:
    k = _key(k)
    if len(k) < 2:
        return k
    cuerpo, dv = k[:-1], k[-1]
    partes = []
    while len(cuerpo) > 3:
        partes.insert(0, cuerpo[-3:])
        cuerpo = cuerpo[:-3]
    partes.insert(0, cuerpo)
    return ".".join(partes) + "-" + dv


def _iso(s: str | None) -> str | None:
    """Normaliza timestamps de SQLite (UTC 'YYYY-MM-DD HH:MM:SS') a ISO con Z."""
    if not s:
        return None
    s = str(s).strip()
    if len(s) == 10:
        return s
    if "T" not in s:
        s = s.replace(" ", "T", 1)
    if not (s.endswith("Z") or "+" in s[10:] or s[10:].count("-") > 0):
        s += "Z"
    return s


def _utc_sql(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _edad(fnac: str | None) -> int | None:
    if not fnac:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            d = datetime.strptime(fnac.strip()[:10], fmt).date()
            hoy = date.today()
            return hoy.year - d.year - ((hoy.month, hoy.day) < (d.month, d.day))
        except ValueError:
            continue
    return None


def _json(data, status: int = 200):
    return JSONResponse(data, status_code=status, headers=_NO_STORE)


def _meta(raw) -> dict:
    try:
        m = json.loads(raw or "{}")
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


# ══ Auth y contexto ══════════════════════════════════════════════════════════

def _ctx(request: Request, token: str | None, cmc_session: str | None) -> dict:
    """Resuelve quién pide. Lanza 401/403 vía alma_scope."""
    tk, scope = alma_scope.resolve(request, token, cmc_session, MODULO)
    prof = config.ALMA_PROFILES.get(tk) or {}
    own = getattr(config, "OLACORE_TOKEN", "") or ""
    adm = getattr(config, "ADMIN_TOKEN", "") or ""
    if own and hmac.compare_digest(tk, own):
        rol, actor = "dueno", "Dueño (Adkun)"
    elif scope:
        rol, actor = "profesional", prof.get("variante") or f"Profesional {scope}"
    elif adm and hmac.compare_digest(tk, adm):
        rol, actor = "recepcion", "Recepción"
    else:
        # Otros perfiles Alma con el módulo habilitado NO tienen alcance definido:
        # antes caían como "perfil" con scope None = veían TODOS los pacientes.
        raise HTTPException(403, "Este perfil no tiene acceso al Portal del Profesional.")
    # El primer elemento de X-Forwarded-For lo controla el cliente (falsificable);
    # X-Real-IP lo fija nginx, y el ÚLTIMO de XFF es el que agregó nuestro proxy.
    real = (request.headers.get("x-real-ip") or "").strip()
    xff = [x.strip() for x in (request.headers.get("x-forwarded-for") or "").split(",") if x.strip()]
    ip = real or (xff[-1] if xff else "") or (request.client.host if request.client else "")
    return {
        "rol": rol, "actor": actor, "scope": scope,
        "actor_hash": hashlib.sha256(f"pp:{tk}".encode()).hexdigest()[:12],
        "ip": ip[:64], "ua": (request.headers.get("user-agent") or "")[:160],
    }


def ensure_tabla_accesos() -> None:
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS portal_pro_accesos (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             TEXT DEFAULT (datetime('now')),
                actor          TEXT,
                actor_hash     TEXT,
                rol            TEXT,
                profesional_id INTEGER,
                accion         TEXT NOT NULL,
                rut            TEXT,
                detalle        TEXT DEFAULT '{}',
                resultado      TEXT DEFAULT 'ok',
                ip             TEXT,
                ua             TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ppa_ts ON portal_pro_accesos(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ppa_rut ON portal_pro_accesos(rut, ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ppa_actor ON portal_pro_accesos(actor_hash, ts)")


_TABLA_OK = False


def auditar(ctx: dict, accion: str, rut: str | None = None,
            detalle: dict | None = None, resultado: str = "ok") -> None:
    """Escribe el registro de acceso. Si falla, NO se entregan datos (503)."""
    global _TABLA_OK
    try:
        if not _TABLA_OK:
            ensure_tabla_accesos()
            _TABLA_OK = True
        with db() as c:
            c.execute(
                """INSERT INTO portal_pro_accesos
                   (actor, actor_hash, rol, profesional_id, accion, rut, detalle, resultado, ip, ua)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (ctx["actor"], ctx["actor_hash"], ctx["rol"], ctx["scope"], accion,
                 _key(rut) if rut else None,
                 json.dumps(detalle or {}, ensure_ascii=False)[:600], resultado,
                 ctx["ip"], ctx["ua"]))
    except Exception as e:
        log.error("auditoría portal_pro no se pudo escribir: %s", e)
        raise HTTPException(503, "No se pudo registrar el acceso. Por seguridad no se muestran datos.")


_fichas: dict[str, deque] = defaultdict(deque)


def _tope_fichas(ctx: dict) -> None:
    ahora = time.time()
    dq = _fichas[ctx["actor_hash"]]
    while dq and dq[0] < ahora - 3600:
        dq.popleft()
    if len(dq) >= FICHAS_POR_HORA:
        auditar(ctx, "tope_fichas", resultado="bloqueado")
        raise HTTPException(429, "Alcanzó el máximo de fichas por hora. Intente más tarde.")
    dq.append(ahora)


# ══ Alcance del profesional ══════════════════════════════════════════════════

def _pacientes_de_profesional(prof_id: int, keys: list[str]) -> set[str] | None:
    """De `keys` (RUT canónicos), cuáles son pacientes de `prof_id`: cita o
    atención con él en los últimos 12 meses, o cita en los próximos 90 días.
    None = la BI no respondió (el llamador debe fallar cerrado)."""
    if not keys:
        return set()
    from bi_helper import bi_query
    rows, status = bi_query(
        f"""SELECT DISTINCT {_sql_key('p.rut')} AS k
              FROM bi.dim_paciente p
             WHERE {_sql_key('p.rut')} = ANY(%s)
               AND (EXISTS (SELECT 1 FROM bi.fact_citas c
                             WHERE c.paciente_id = p.paciente_id AND c.profesional_id = %s
                               AND c.fecha BETWEEN CURRENT_DATE - %s::int AND CURRENT_DATE + %s::int)
                    OR EXISTS (SELECT 1 FROM bi.fact_atenciones a
                             WHERE a.paciente_id = p.paciente_id AND a.profesional_id = %s
                               AND a.fecha >= CURRENT_DATE - %s::int))""",
        (list(keys), prof_id, VENTANA_PROPIOS_PASADO, VENTANA_PROPIOS_FUTURO,
         prof_id, VENTANA_PROPIOS_PASADO))
    if status != "ok":
        return None
    return {r["k"] for r in rows}


def _filtrar_alcance(ctx: dict, keys: list[str]) -> tuple[list[str], bool]:
    """Devuelve (keys permitidos, alcance_verificado)."""
    if not ctx["scope"]:
        return keys, True
    ok = _pacientes_de_profesional(ctx["scope"], keys)
    if ok is None:
        return [], False
    return [k for k in keys if k in ok], True


def _exigir_alcance(ctx: dict, k: str, accion: str) -> None:
    permitidos, verificado = _filtrar_alcance(ctx, [k])
    if not verificado:
        auditar(ctx, accion, k, {"motivo": "bi_no_disponible"}, resultado="denegado")
        raise HTTPException(503, "No pudimos verificar si es su paciente. Intente en unos minutos.")
    if k not in permitidos:
        auditar(ctx, accion, k, {"motivo": "fuera_de_alcance"}, resultado="denegado")
        raise HTTPException(403, "Este paciente no tiene citas con usted en los últimos 12 meses.")


# ══ Lectura de datos (solo por RUT) ══════════════════════════════════════════

def _nombres(keys: list[str]) -> dict[str, str]:
    """Nombre por RUT desde perfiles y vínculos familiares (nunca por teléfono)."""
    if not keys:
        return {}
    out: dict[str, str] = {}
    marks = ",".join("?" * len(keys))
    with db() as c:
        for r in c.execute(
                f"""SELECT {_sql_key('rut')} AS k, nombre FROM contact_profiles
                     WHERE {_sql_key('rut')} IN ({marks}) AND COALESCE(nombre,'') <> ''
                     ORDER BY updated_at ASC""", keys).fetchall():
            out[r["k"]] = r["nombre"]
        for r in c.execute(
                f"""SELECT {_sql_key('dependent_rut')} AS k, dependent_nombre FROM family_links
                     WHERE {_sql_key('dependent_rut')} IN ({marks})
                       AND COALESCE(dependent_nombre,'') <> ''""", keys).fetchall():
            if r["k"] not in out and _key(r["dependent_nombre"]) != r["k"]:
                out[r["k"]] = r["dependent_nombre"]
    return out


def _novedades_reales(dias: int) -> dict[str, dict]:
    """Por RUT: qué subió/cambió en la ventana. Solo lectura de SQLite local."""
    desde = _utc_sql(datetime.now(timezone.utc) - timedelta(days=dias))
    pac: dict[str, dict] = {}

    def item(k):
        return pac.setdefault(k, {"k": k, "ultimo": None, "mediciones": 0, "tipos": set(),
                                  "examenes": 0, "perfil": 0, "familia": 0, "checkin": 0})

    def toca(p, ts):
        ts = _iso(ts)
        if ts and (p["ultimo"] is None or ts > p["ultimo"]):
            p["ultimo"] = ts

    with db() as c:
        for r in c.execute(
                """SELECT rut, tipo, COUNT(*) n, MAX(created_at) u FROM patient_vitals
                    WHERE created_at >= ? GROUP BY rut, tipo""", (desde,)).fetchall():
            p = item(_key(r["rut"]))
            p["mediciones"] += r["n"]
            p["tipos"].add(r["tipo"])
            toca(p, r["u"])
        try:
            for r in c.execute(
                    """SELECT rut, COUNT(*) n, MAX(created_at) u FROM portal_examenes
                        WHERE publicado = 1 AND created_at >= ? GROUP BY rut""", (desde,)).fetchall():
                p = item(_key(r["rut"]))
                p["examenes"] += r["n"]
                toca(p, r["u"])
        except Exception:
            pass  # tabla aún no creada (nadie ha cargado exámenes)
        for r in c.execute(
                """SELECT owner_rut, dependent_rut, created_at FROM family_links
                    WHERE created_at >= ? AND revoked_at IS NULL""", (desde,)).fetchall():
            for rr in (r["owner_rut"], r["dependent_rut"]):
                p = item(_key(rr))
                p["familia"] += 1
                toca(p, r["created_at"])
        evs = sorted(_EV_NOVEDAD_CHECKIN | _EV_NOVEDAD_PERFIL)
        for r in c.execute(
                f"""SELECT event, meta, ts FROM conversation_events
                     WHERE event IN ({','.join('?' * len(evs))}) AND ts >= ?""",
                (*evs, desde)).fetchall():
            m = _meta(r["meta"])
            k = _key(m.get("rut"))
            if not k or m.get("demo"):
                continue
            p = item(k)
            if r["event"] in _EV_NOVEDAD_CHECKIN:
                p["checkin"] += 1
            else:
                p["perfil"] += 1
            toca(p, r["ts"])
    for p in pac.values():
        p["tipos"] = sorted(p["tipos"])
    # RUT de la demo del portal del paciente (50.000.00x) nunca cuentan como reales.
    return {k: v for k, v in pac.items() if len(k) >= 7 and not k.startswith("50000")}


def _ficha_real(k: str) -> dict:
    hace_1a = _utc_sql(datetime.now(timezone.utc) - timedelta(days=400))
    with db() as c:
        vit = [dict(r) for r in c.execute(
            f"""SELECT id, tipo, valor, valor2, contexto, nota, ts, created_at FROM patient_vitals
                 WHERE {_sql_key('rut')} = ? AND ts >= ? ORDER BY ts DESC LIMIT 1000""",
            (k, hace_1a.replace(" ", "T"))).fetchall()]
        try:
            exs = [dict(r) for r in c.execute(
                f"""SELECT id, nombre, fecha, valor, unidad, rango_min, rango_max, created_at
                      FROM portal_examenes WHERE {_sql_key('rut')} = ? AND publicado = 1
                     ORDER BY COALESCE(fecha, created_at) DESC, id DESC LIMIT 200""", (k,)).fetchall()]
        except Exception:
            exs = []
        perfiles = [dict(r) for r in c.execute(
            f"""SELECT nombre, fecha_nacimiento, sexo, email, comuna, direccion, prevision,
                       contacto_emerg_nombre, contacto_emerg_telefono, updated_at
                  FROM contact_profiles WHERE {_sql_key('rut')} = ?
                 ORDER BY updated_at DESC LIMIT 5""", (k,)).fetchall()]
        repres = [dict(r) for r in c.execute(
            f"""SELECT owner_rut, relation, verification_method, created_at FROM family_links
                 WHERE {_sql_key('dependent_rut')} = ? AND revoked_at IS NULL
                 ORDER BY created_at DESC LIMIT 20""", (k,)).fetchall()]
        gestiona = [dict(r) for r in c.execute(
            f"""SELECT dependent_rut, dependent_nombre, relation, verification_method, created_at
                  FROM family_links WHERE {_sql_key('owner_rut')} = ? AND revoked_at IS NULL
                 ORDER BY created_at DESC LIMIT 20""", (k,)).fetchall()]
        desde_ev = _utc_sql(datetime.now(timezone.utc) - timedelta(days=365))
        evs = []
        for r in c.execute(
                f"""SELECT event, meta, ts FROM conversation_events
                     WHERE event IN ({','.join('?' * len(_EV_ACTIVIDAD))}) AND ts >= ?
                     ORDER BY ts DESC LIMIT 4000""", (*_EV_ACTIVIDAD, desde_ev)).fetchall():
            m = _meta(r["meta"])
            if m.get("demo"):
                continue
            if k not in (_key(m.get("rut")), _key(m.get("owner")), _key(m.get("dependent")),
                         _key(m.get("to"))):
                continue
            evs.append((r["event"], m, r["ts"]))
            if len(evs) >= 150:
                break

    # Perfil: se toma la fila del MISMO RUT más reciente, campo a campo (nunca
    # la del teléfono: puede ser de otra persona de la familia).
    datos: dict = {}
    for p in reversed(perfiles):
        for kk, v in p.items():
            if v not in (None, ""):
                datos[kk] = v
    actualizado = perfiles[0]["updated_at"] if perfiles else None

    nombres = _nombres([_key(r["owner_rut"]) for r in repres] + [k])
    actividad = []
    for ev, m, ts in evs:
        det = ""
        if ev == "portal_checkin_confirmado":
            det = " · ".join(x for x in (m.get("especialidad") or "", (m.get("hora") or "") and f"{m.get('hora')} hrs") if x)
        elif ev == "portal_perfil_actualizado":
            det = ", ".join(m.get("campos") or [])
        elif ev in ("portal_family_add_minor", "portal_family_add_adult", "portal_family_revoke"):
            det = m.get("relation") or ""
        elif ev == "portal_examen_cargado":
            det = f"{m.get('n', '')} resultado(s)"
        actividad.append({"ts": _iso(ts), "evento": ev,
                          "label": EVENTO_LABEL.get(ev, ev.replace("portal_", "").replace("_", " ")),
                          "detalle": det,
                          "novedad": ev in (_EV_NOVEDAD_CHECKIN | _EV_NOVEDAD_PERFIL)})

    return {
        "paciente": {"rut": _fmt_rut(k), "nombre": datos.get("nombre") or nombres.get(k) or "",
                     "edad": _edad(datos.get("fecha_nacimiento")), "sexo": datos.get("sexo") or ""},
        "mediciones": [{**v, "ts": _iso(v["ts"]), "created_at": _iso(v["created_at"])} for v in vit],
        "examenes": [_examen_crudo(e) for e in exs],
        "datos": {kk: datos.get(kk) for kk in ("fecha_nacimiento", "sexo", "email", "comuna", "direccion",
                                               "prevision", "contacto_emerg_nombre",
                                               "contacto_emerg_telefono")},
        "datos_actualizado": _iso(actualizado),
        "datos_cambios": [a for a in actividad if a["evento"] == "portal_perfil_actualizado"][:10],
        # Terceros: nombre, relación y método. Sin RUT (minimización de datos).
        "representantes": [{"nombre": nombres.get(_key(r["owner_rut"])) or "",
                            "relacion": r["relation"] or "",
                            "metodo": r["verification_method"], "desde": _iso(r["created_at"])}
                           for r in repres],
        "gestiona": [{"nombre": r["dependent_nombre"] or "",
                      "relacion": r["relation"] or "", "metodo": r["verification_method"],
                      "desde": _iso(r["created_at"])} for r in gestiona],
        "actividad": actividad,
    }


def _examen_crudo(e: dict) -> dict:
    """Examen SIN la interpretación pensada para el paciente (nivel, etiqueta,
    conclusión, qué hacer). Solo valor, unidad y rango; `fuera_rango` solo si
    el examen trae un rango utilizable."""
    rmin, rmax, val = e.get("rango_min"), e.get("rango_max"), e.get("valor")
    tiene_rango = rmin is not None and rmax is not None and rmax > rmin
    fuera = None
    if tiene_rango and val is not None:
        fuera = val < rmin or val > rmax
    return {"id": e.get("id"), "nombre": e.get("nombre"), "fecha": e.get("fecha"),
            "valor": val, "unidad": e.get("unidad") or "",
            "rango_min": rmin if tiene_rango else None, "rango_max": rmax if tiene_rango else None,
            "fuera_rango": fuera, "publicado": _iso(e.get("created_at"))}


def _citas_bi(k: str, prof_id: int | None) -> dict:
    """Última atención y próxima cita (BI, best-effort). Si la pide un
    profesional, la última atención es CON ÉL (base del 'preparar control')."""
    from bi_helper import bi_query
    filtro = "AND a.profesional_id = %s" if prof_id else ""
    params = [k] + ([prof_id] if prof_id else [])
    ult, st = bi_query(
        f"""SELECT a.fecha, COALESCE(dp.nombre,'') AS profesional
              FROM bi.fact_atenciones a
              JOIN bi.dim_paciente p ON p.paciente_id = a.paciente_id
              LEFT JOIN bi.dim_profesional dp ON dp.profesional_id = a.profesional_id
             WHERE {_sql_key('p.rut')} = %s AND a.fecha <= CURRENT_DATE {filtro}
             ORDER BY a.fecha DESC LIMIT 1""", tuple(params))
    prox, st2 = bi_query(
        f"""SELECT c.fecha, c.hora_inicio, COALESCE(dp.nombre,'') AS profesional
              FROM bi.fact_citas c
              JOIN bi.dim_paciente p ON p.paciente_id = c.paciente_id
              LEFT JOIN bi.dim_profesional dp ON dp.profesional_id = c.profesional_id
             WHERE {_sql_key('p.rut')} = %s AND c.fecha >= CURRENT_DATE
               AND c.estado IN ('agendada','confirmada')
             ORDER BY c.fecha, c.hora_inicio LIMIT 1""", (k,))

    def f(v):
        return v.isoformat() if hasattr(v, "isoformat") else (str(v)[:10] if v else None)

    def h(v):
        return str(v)[:5] if v else ""
    return {
        "disponible": st == "ok",
        "ultima": ({"fecha": f(ult[0]["fecha"]), "profesional": ult[0]["profesional"]} if ult else None),
        "proxima": ({"fecha": f(prox[0]["fecha"]), "hora": h(prox[0].get("hora_inicio")),
                     "profesional": prox[0]["profesional"]} if prox and st2 == "ok" else None),
    }


def _resumen_control(ficha: dict, desde: str) -> dict:
    """Resumen de lo subido DESDE `desde` (YYYY-MM-DD, hora Chile). Solo
    estadística descriptiva de valores crudos; ninguna conclusión."""
    d0 = datetime.strptime(desde, "%Y-%m-%d").replace(tzinfo=_CL).astimezone(timezone.utc)
    corte = d0.strftime("%Y-%m-%dT%H:%M:%S")

    def despues(ts):
        return bool(ts) and ts.replace("Z", "")[:19] >= corte

    por_tipo = {}
    for t in _TIPOS_VITAL:
        xs = [v for v in ficha["mediciones"] if v["tipo"] == t and despues(v["ts"])]
        if not xs:
            continue
        vals = [v["valor"] for v in xs]
        st = {"n": len(xs), "primero": xs[-1]["ts"], "ultimo": xs[0]["ts"],
              "ultimo_valor": xs[0]["valor"], "ultimo_valor2": xs[0].get("valor2"),
              "min": min(vals), "max": max(vals), "promedio": round(sum(vals) / len(vals), 1)}
        if t == "presion":
            v2 = [v["valor2"] for v in xs if v.get("valor2") is not None]
            if v2:
                st.update({"min2": min(v2), "max2": max(v2), "promedio2": round(sum(v2) / len(v2), 1)})
        por_tipo[t] = st
    return {
        "desde": desde,
        "mediciones": por_tipo,
        "lecturas": [v for v in ficha["mediciones"] if despues(v["ts"])][:60],
        "examenes": [e for e in ficha["examenes"] if despues(e.get("publicado"))
                     or (e.get("fecha") and e["fecha"] >= desde)],
        "cambios": [a for a in ficha["actividad"] if a["novedad"] and despues(a["ts"])
                    or (a["evento"].startswith("portal_family_add") and despues(a["ts"]))],
        "familia_nueva": [r for r in ficha["representantes"] + ficha["gestiona"] if despues(r.get("desde"))],
    }


# ══ Demo (todo inventado, en memoria) ════════════════════════════════════════

_DEMO_PACIENTES = [
    # rut, nombre, sexo, fnac, perfil de datos
    ("50000101-9", "María Ejemplo Soto", "F", "1961-04-12", "hta"),
    ("50000102-7", "Juan Ejemplo Riquelme", "M", "1954-09-30", "dm"),
    ("50000103-5", "Rosa Ejemplo Fuentes", "F", "1948-01-22", "hta_dm"),
    ("50000104-3", "Pedro Ejemplo Lagos", "M", "1979-11-05", "peso"),
    ("50000105-1", "Camila Ejemplo Neira", "F", "1992-06-18", "examen"),
    ("50000106-K", "Tomás Ejemplo Neira", "M", "2017-03-02", "menor"),
    ("50000107-8", "Gloria Ejemplo Sáez", "F", "1957-08-14", "checkin"),
    ("50000108-6", "Héctor Ejemplo Vidal", "M", "1966-12-01", "hta"),
    ("50000109-4", "Elena Ejemplo Mella", "F", "1983-02-27", "datos"),
]


def _demo_ficha(k: str) -> dict | None:
    meta = next((p for p in _DEMO_PACIENTES if _key(p[0]) == k), None)
    if not meta:
        return None
    rut, nombre, sexo, fnac, tipo = meta
    rnd = random.Random(k)
    ahora = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    seed_h = {"hta": 2, "dm": 5, "hta_dm": 1, "peso": 26, "examen": 30, "menor": 7,
              "checkin": 3, "datos": 20}[tipo]
    med = []
    nid = 1

    ahora_cl = datetime.now(_CL)

    def add(tipo_v, valor, valor2, ctxv, dia, hora, nota=None):
        """Registro `dia` días atrás a la `hora` local de Chile (nunca en el futuro)."""
        nonlocal nid
        loc = (ahora_cl - timedelta(days=dia)).replace(hour=hora, minute=rnd.choice([4, 12, 18, 26, 37, 45, 52]),
                                                      second=0, microsecond=0)
        if loc > ahora_cl - timedelta(minutes=20):
            loc -= timedelta(days=1)
        t = loc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        med.append({"id": nid, "tipo": tipo_v, "valor": valor, "valor2": valor2, "contexto": ctxv,
                    "nota": nota, "ts": t, "created_at": t})
        nid += 1

    d0 = 0 if seed_h < 12 else 1   # quien subió "hace poco" tiene registro de hoy
    if tipo in ("hta", "hta_dm", "checkin"):
        base_s = 146 if tipo != "checkin" else 128
        for dia in range(0, 62, 2):
            s = base_s + dia * 0.32 + rnd.uniform(-7, 7)   # más alta hace 2 meses
            d = s * 0.6 + rnd.uniform(-4, 4)
            add("presion", round(s), round(d), "Mañana", d0 + dia, 8,
                "Después de caminar" if dia == 0 else None)
            if dia % 4 == 2:
                add("presion", round(s - 6 + rnd.uniform(-5, 5)), round(d - 3), "Noche", d0 + dia, 21)
    if tipo in ("dm", "hta_dm"):
        for dia in range(0, 60, 3):
            add("glicemia", round(128 + dia * 0.6 + rnd.uniform(-14, 14)), None, "Ayunas", d0 + dia, 7)
            if dia % 9 == 3:
                add("glicemia", round(188 + rnd.uniform(-20, 20)), None, "2 h después de comer", d0 + dia, 15)
    if tipo in ("peso", "dm"):
        for sem in range(0, 12):
            add("peso", round(91.0 + sem * 0.45 + rnd.uniform(-.4, .4), 1), None, None, 1 + sem * 7, 9)
    if tipo == "menor":
        add("temperatura", 37.1, None, "Mañana", 0, 8, "Sin fiebre desde ayer")
        add("temperatura", 37.9, None, "Noche", 1, 22)
        add("temperatura", 38.4, None, "Tarde", 1, 17, "Con tos")
        add("peso", 24.6, None, None, 30, 10)
    med.sort(key=lambda v: v["ts"], reverse=True)

    exs = []
    if tipo in ("examen", "hta_dm", "dm"):
        f = (date.today() - timedelta(days=4 if tipo == "examen" else 18)).isoformat()
        pub = (ahora - timedelta(hours=30 if tipo == "examen" else 400)).strftime("%Y-%m-%dT%H:%M:%SZ")
        base = [("Glicemia en ayunas", 118 if tipo != "examen" else 92, "mg/dL", 70, 100),
                ("Hemoglobina glicosilada (HbA1c)", 7.4 if tipo != "examen" else 5.4, "%", 4.0, 6.0),
                ("Colesterol total", 232, "mg/dL", 0, 200),
                ("Hemoglobina", 13.8, "g/dL", 12, 16),
                ("TSH", 2.1, "uUI/mL", None, None)]
        for i, (n, v, u, a, b) in enumerate(base):
            exs.append(_examen_crudo({"id": i + 1, "nombre": n, "fecha": f, "valor": v, "unidad": u,
                                      "rango_min": a, "rango_max": b, "created_at": pub}))

    def hace(h):
        return (ahora - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")

    actividad = [
        {"ts": hace(seed_h + 0.2), "evento": "portal_login", "label": EVENTO_LABEL["portal_login"],
         "detalle": "", "novedad": False},
        {"ts": hace(seed_h + 240), "evento": "portal_login", "label": EVENTO_LABEL["portal_login"],
         "detalle": "", "novedad": False},
        {"ts": hace(seed_h + 600), "evento": "portal_wiz_exito", "label": EVENTO_LABEL["portal_wiz_exito"],
         "detalle": "", "novedad": False},
    ]
    if tipo in ("checkin", "hta"):
        actividad.insert(0, {"ts": hace(1.5 if tipo == "checkin" else 26), "evento": "portal_checkin_confirmado",
                             "label": EVENTO_LABEL["portal_checkin_confirmado"],
                             "detalle": "Medicina General · 17:30 hrs", "novedad": True})
    if tipo in ("datos", "hta_dm"):
        actividad.insert(0, {"ts": hace(seed_h - 1 if seed_h > 2 else 1), "evento": "portal_perfil_actualizado",
                             "label": EVENTO_LABEL["portal_perfil_actualizado"],
                             "detalle": "direccion, contacto_emerg_telefono", "novedad": True})
    if tipo == "examen":
        actividad.insert(0, {"ts": hace(29), "evento": "portal_examenes_ver",
                             "label": EVENTO_LABEL["portal_examenes_ver"], "detalle": "", "novedad": False})
    actividad.sort(key=lambda a: a["ts"], reverse=True)

    repres, gestiona = [], []
    if tipo == "menor":
        repres.append({"nombre": "Camila Ejemplo Neira", "relacion": "hijo/a",
                       "metodo": "tutor_declaration", "desde": hace(900)})
    if tipo == "examen":
        gestiona.append({"nombre": "Tomás Ejemplo Neira", "relacion": "hijo/a",
                         "metodo": "tutor_declaration", "desde": hace(900)})
        gestiona.append({"nombre": "Luis Ejemplo Neira", "relacion": "padre/madre",
                         "metodo": "declared", "desde": hace(12)})
    if tipo == "hta_dm":
        repres.append({"nombre": "Ana Ejemplo Fuentes", "relacion": "padre/madre",
                       "metodo": "otp", "desde": hace(seed_h + 3)})
    datos = {"fecha_nacimiento": fnac, "sexo": sexo, "email": "" if tipo in ("menor", "hta") else "ejemplo@correo.cl",
             "comuna": rnd.choice(["Arauco", "Carampangue", "Curanilahue", "Laraquete"]),
             "direccion": "Calle Ficticia " + str(rnd.randint(100, 999)),
             "prevision": rnd.choice(["Fonasa B", "Fonasa C", "Fonasa D", "Isapre"]),
             "contacto_emerg_nombre": "" if tipo == "menor" else "Contacto Ejemplo",
             "contacto_emerg_telefono": "" if tipo == "menor" else "+56 9 1234 5678"}
    return {
        "paciente": {"rut": _fmt_rut(rut), "nombre": nombre, "edad": _edad(fnac), "sexo": sexo},
        "mediciones": med, "examenes": exs, "datos": datos,
        "datos_actualizado": hace(seed_h - 1 if tipo == "datos" else 2000),
        "datos_cambios": [a for a in actividad if a["evento"] == "portal_perfil_actualizado"],
        "representantes": repres, "gestiona": gestiona, "actividad": actividad,
        "citas": {"disponible": True,
                  "ultima": {"fecha": (date.today() - timedelta(days=rnd.choice([28, 35, 42, 63]))).isoformat(),
                             "profesional": "Dr. Ejemplo Demo"},
                  "proxima": ({"fecha": date.today().isoformat(), "hora": "17:30", "profesional": "Dr. Ejemplo Demo"}
                              if tipo in ("checkin", "hta") else
                              {"fecha": (date.today() + timedelta(days=rnd.randint(2, 12))).isoformat(),
                               "hora": rnd.choice(["09:30", "11:00", "15:45"]), "profesional": "Dr. Ejemplo Demo"})},
        "demo": True,
    }


def _demo_novedades(dias: int) -> list[dict]:
    corte = datetime.now(timezone.utc) - timedelta(days=dias)
    out = []
    for rut, nombre, *_ in _DEMO_PACIENTES:
        f = _demo_ficha(_key(rut))
        nuevos = [v for v in f["mediciones"] if v["created_at"] >= corte.strftime("%Y-%m-%dT%H:%M:%SZ")]
        exs = [e for e in f["examenes"] if (e["publicado"] or "") >= corte.strftime("%Y-%m-%dT%H:%M:%SZ")]
        nov = [a for a in f["actividad"] if a["novedad"] and a["ts"] >= corte.strftime("%Y-%m-%dT%H:%M:%SZ")]
        fam = [r for r in f["representantes"] + f["gestiona"]
               if r["desde"] >= corte.strftime("%Y-%m-%dT%H:%M:%SZ")]
        ts = [v["created_at"] for v in nuevos] + [e["publicado"] for e in exs] + [a["ts"] for a in nov] \
            + [r["desde"] for r in fam]
        if not ts:
            continue
        out.append({"rut": _fmt_rut(rut), "nombre": nombre, "ultimo": max(ts), "edad": f["paciente"]["edad"],
                    "mediciones": len(nuevos), "tipos": sorted({v["tipo"] for v in nuevos}),
                    "examenes": len(exs), "familia": len(fam),
                    "perfil": sum(1 for a in nov if a["evento"] == "portal_perfil_actualizado"),
                    "checkin": sum(1 for a in nov if a["evento"] == "portal_checkin_confirmado"),
                    "proxima_hoy": (f["citas"]["proxima"] or {}).get("fecha") == date.today().isoformat()})
    out.sort(key=lambda p: p["ultimo"], reverse=True)
    return out


_DEMO_ACCESOS = [
    ("Dr. Ejemplo Demo", "profesional", "ver_ficha", "50000101-9", "ok", 0.3),
    ("Dr. Ejemplo Demo", "profesional", "preparar_control", "50000101-9", "ok", 0.25),
    ("Recepción", "recepcion", "buscar", None, "ok", 2),
    ("Dr. Ejemplo Demo", "profesional", "ver_ficha", "50000108-6", "denegado", 5),
    ("Dueño (Adkun)", "dueno", "lista_novedades", None, "ok", 7),
]


# ══ Endpoints ════════════════════════════════════════════════════════════════

@router.get("/portal-profesional/api/yo")
def api_yo(request: Request, token: str | None = Query(None), demo: str = "",
           cmc_session: str | None = Cookie(None)):
    if demo == "1":
        return _json({"demo": True, "rol": "profesional", "actor": "Dr. Ejemplo Demo · Medicina General",
                      "alcance": "propios", "ve_auditoria": True})
    ctx = _ctx(request, token, cmc_session)
    return _json({"demo": False, "rol": ctx["rol"], "actor": ctx["actor"],
                  "alcance": "propios" if ctx["scope"] else "todos",
                  "ve_auditoria": ctx["rol"] == "dueno"})


@router.get("/portal-profesional/api/novedades")
def api_novedades(request: Request, dias: int = Query(7, ge=1, le=90),
                  token: str | None = Query(None), demo: str = "",
                  cmc_session: str | None = Cookie(None)):
    """Pacientes con información nueva en la ventana, del más reciente al más antiguo."""
    if demo == "1":
        return _json({"demo": True, "dias": dias, "alcance_verificado": True,
                      "pacientes": _demo_novedades(dias)})
    ctx = _ctx(request, token, cmc_session)
    pac = _novedades_reales(dias)
    keys, verificado = _filtrar_alcance(ctx, list(pac))
    auditar(ctx, "lista_novedades", None, {"dias": dias, "n": len(keys)},
            resultado="ok" if verificado else "sin_alcance")
    nombres = _nombres(keys)
    out = []
    for k in keys:
        p = pac[k]
        out.append({"rut": _fmt_rut(k), "nombre": nombres.get(k, ""), "ultimo": p["ultimo"],
                    "mediciones": p["mediciones"], "tipos": p["tipos"], "examenes": p["examenes"],
                    "familia": p["familia"], "perfil": p["perfil"], "checkin": p["checkin"]})
    out.sort(key=lambda x: x["ultimo"] or "", reverse=True)
    return _json({"demo": False, "dias": dias, "alcance_verificado": verificado, "pacientes": out[:300]})


@router.get("/portal-profesional/api/buscar")
def api_buscar(request: Request, q: str = Query("", max_length=60),
               token: str | None = Query(None), demo: str = "",
               cmc_session: str | None = Cookie(None)):
    """Busca por RUT o nombre SOLO entre pacientes que tienen algo en el portal."""
    q = (q or "").strip()
    qk = _key(q)
    es_rut = len(qk) >= 7 and qk[:-1].isdigit()
    if not es_rut and len(q) < 3:
        return _json({"pacientes": [], "motivo": "corto"})
    if demo == "1":
        res = []
        for rut, nombre, *_ in _DEMO_PACIENTES:
            if (es_rut and _key(rut).startswith(qk)) or (not es_rut and q.lower() in nombre.lower()):
                res.append({"rut": _fmt_rut(rut), "nombre": nombre})
        return _json({"pacientes": res[:15]})
    ctx = _ctx(request, token, cmc_session)
    _tope_fichas(ctx)   # buscar por RUT también expone pacientes: mismo tope
    cands: list[str] = []
    with db() as c:
        if es_rut:
            for t, col in (("patient_vitals", "rut"), ("family_links", "dependent_rut"),
                           ("family_links", "owner_rut"), ("contact_profiles", "rut")):
                if c.execute(f"SELECT 1 FROM {t} WHERE {_sql_key(col)} = ? LIMIT 1", (qk,)).fetchone():
                    cands.append(qk)
                    break
            try:
                if not cands and c.execute(
                        f"SELECT 1 FROM portal_examenes WHERE {_sql_key('rut')} = ? AND publicado=1 LIMIT 1",
                        (qk,)).fetchone():
                    cands.append(qk)
            except Exception:
                pass
        else:
            like = f"%{q}%"
            rows = c.execute(
                f"""SELECT DISTINCT {_sql_key('rut')} AS k FROM contact_profiles
                     WHERE nombre LIKE ? AND rut IS NOT NULL AND rut <> '' LIMIT 60""", (like,)).fetchall()
            rows += c.execute(
                f"""SELECT DISTINCT {_sql_key('dependent_rut')} AS k FROM family_links
                     WHERE dependent_nombre LIKE ? AND revoked_at IS NULL LIMIT 60""", (like,)).fetchall()
            cands = list(dict.fromkeys(r["k"] for r in rows if r["k"]))
    keys, verificado = _filtrar_alcance(ctx, cands)
    auditar(ctx, "buscar", None, {"tipo": "rut" if es_rut else "nombre", "n": len(keys)})
    nombres = _nombres(keys)
    return _json({"pacientes": [{"rut": _fmt_rut(k), "nombre": nombres.get(k, "")} for k in keys[:15]],
                  "alcance_verificado": verificado})


@router.get("/portal-profesional/api/paciente/{rut}")
def api_paciente(rut: str, request: Request, token: str | None = Query(None), demo: str = "",
                 cmc_session: str | None = Cookie(None)):
    k = _key(rut)
    if len(k) < 7:
        raise HTTPException(400, "RUT inválido")
    if demo == "1":
        f = _demo_ficha(k)
        if not f:
            raise HTTPException(404, "Paciente de ejemplo no encontrado")
        return _json(f)
    ctx = _ctx(request, token, cmc_session)
    _exigir_alcance(ctx, k, "ver_ficha")
    _tope_fichas(ctx)
    auditar(ctx, "ver_ficha", k)
    f = _ficha_real(k)
    f["citas"] = _citas_bi(k, ctx["scope"])
    f["demo"] = False
    return _json(f)


@router.get("/portal-profesional/api/paciente/{rut}/preparar")
def api_preparar(rut: str, request: Request, desde: str = Query(""),
                 token: str | None = Query(None), demo: str = "",
                 cmc_session: str | None = Cookie(None)):
    """Resumen imprimible de lo que el paciente subió desde su última atención."""
    k = _key(rut)
    if len(k) < 7:
        raise HTTPException(400, "RUT inválido")
    if demo == "1":
        f = _demo_ficha(k)
        if not f:
            raise HTTPException(404, "Paciente de ejemplo no encontrado")
        ctx = None
    else:
        ctx = _ctx(request, token, cmc_session)
        _exigir_alcance(ctx, k, "preparar_control")
        _tope_fichas(ctx)
        f = _ficha_real(k)
        f["citas"] = _citas_bi(k, ctx["scope"])
    base = "ultima_atencion"
    if not desde:
        ult = (f.get("citas") or {}).get("ultima")
        if ult and ult.get("fecha"):
            desde = ult["fecha"]
        else:
            desde = (date.today() - timedelta(days=90)).isoformat()
            base = "90_dias"
    else:
        base = "manual"
    try:
        datetime.strptime(desde, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Fecha inválida")
    if ctx:
        auditar(ctx, "preparar_control", k, {"desde": desde})
    res = _resumen_control(f, desde)
    res.update({"paciente": f["paciente"], "citas": f.get("citas"), "base": base,
                "generado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "generado_por": ctx["actor"] if ctx else "Dr. Ejemplo Demo (demo)",
                "demo": demo == "1"})
    return _json(res)


@router.get("/portal-profesional/api/accesos")
def api_accesos(request: Request, rut: str = "", token: str | None = Query(None), demo: str = "",
                cmc_session: str | None = Cookie(None)):
    """Registro de accesos (Ley 21.719). Solo el dueño."""
    if demo == "1":
        ahora = datetime.now(timezone.utc)
        return _json({"accesos": [
            {"ts": (ahora - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"), "actor": a, "rol": r,
             "accion": ac, "rut": _fmt_rut(rr) if rr else "", "resultado": res, "ip": "190.xx.xx.xx"}
            for a, r, ac, rr, res, h in sorted(_DEMO_ACCESOS, key=lambda x: x[5])]})
    ctx = _ctx(request, token, cmc_session)
    if ctx["rol"] != "dueno":
        raise HTTPException(403, "Solo el dueño ve el registro de accesos")
    ensure_tabla_accesos()
    with db() as c:
        if rut:
            rows = c.execute("""SELECT * FROM portal_pro_accesos WHERE rut = ?
                                ORDER BY id DESC LIMIT 300""", (_key(rut),)).fetchall()
        else:
            rows = c.execute("SELECT * FROM portal_pro_accesos ORDER BY id DESC LIMIT 300").fetchall()
    auditar(ctx, "ver_auditoria", rut or None)
    return _json({"accesos": [{"ts": _iso(r["ts"]), "actor": r["actor"], "rol": r["rol"],
                               "accion": r["accion"], "rut": _fmt_rut(r["rut"]) if r["rut"] else "",
                               "resultado": r["resultado"], "ip": r["ip"] or ""} for r in rows]})


@router.get("/portal-profesional", response_class=HTMLResponse, include_in_schema=False)
def pagina(request: Request, token: str | None = Query(None), demo: str = "",
           cmc_session: str | None = Cookie(None)):
    """Página. 404 en el dominio clínico público. Sin sesión válida (y sin
    demo) responde 401: la página nunca se sirve a un anónimo."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host.endswith("centromedicocarampangue.cl"):
        raise HTTPException(404, "Not found")
    if demo != "1" and not alma_scope.page_token(token, cmc_session, MODULO):
        return HTMLResponse(_SIN_ACCESO, status_code=401, headers=_NO_STORE)
    return HTMLResponse(_TEMPLATE.read_text(encoding="utf-8"), headers=_NO_STORE)


_SIN_ACCESO = """<!doctype html><html lang="es"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Portal del Profesional — sin acceso</title>
<body style="margin:0;min-height:100dvh;display:grid;place-items:center;background:#EEF3F8;
font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#12283A;padding:24px">
<div style="max-width:420px;background:#fff;border-radius:20px;padding:28px;box-shadow:0 12px 32px rgba(15,63,104,.12)">
<h1 style="font-size:21px;margin:0 0 8px;color:#0F3F68">Sin acceso al Portal del Profesional</h1>
<p style="font-size:15px;line-height:1.55;margin:0 0 16px;color:#3d5568">Esta vista muestra datos de salud
que los pacientes suben desde su portal. Para entrar necesita su enlace personal de Alma o iniciar sesión
en el panel.</p>
<a href="/admin/login" style="display:inline-block;background:#0F3F68;color:#fff;text-decoration:none;
padding:12px 18px;border-radius:12px;font-weight:600;font-size:15px">Iniciar sesión</a></div></body></html>"""
