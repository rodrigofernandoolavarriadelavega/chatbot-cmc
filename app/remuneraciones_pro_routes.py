"""Remuneraciones Pro — módulo Alma `/alma/remuneraciones` (key `remuneraciones`).

Contrato: docs/remuneraciones_pro_contrato.md. Reemplaza en uso diario al Nivel 4
del Panel del Día (`remuneraciones_routes`, que sigue intacto).

Qué hace:
  1. Concilia, a nivel LÍNEA = (fecha, paciente, profesional), tres fuentes:
       - Sistema  = espejo `bi_pagos_caja` (lo que usa hoy `ebitda_routes._ebitda_mes`).
       - Medilink = caja EN VIVO, leída día por día por un barrido en background
                    y guardada en `remun_medilink_pagos` (solo lectura del HIS).
       - Módulo   = `pagos_cmc` (lo que teclea recepción; solo copago → la
                    bonificación Fonasa se estima por arancel N3).
  2. Liquida a cada profesional con la MISMA regla de honorario que el EBITDA
     (`_regla_honorario`, `SIN_RETENCION`, `RETENCION_BOLETA`, fijo de Abarca),
     sobre la base que decide `_linea_cuenta_para_pago`.
  3. Ajustes manuales, marca de pagado con monto y texto de WhatsApp.

Reglas que NO se negocian (ver contrato):
  - `pct_honorario` es lo que se lleva el PROFESIONAL.
  - Medilink se consulta SIEMPRE día por día (pagina de a 50 sin ordenar), con
    ≥4 s entre llamadas y backoff en 429. Nunca se escribe en Medilink.
  - Un día que Medilink no respondió completo NO se marca verificado (fallo
    cerrado): sin eso, todos sus pagos aparecerían como anulados.
  - Medilink /pagos NO trae el profesional: el lado "Medilink" se atribuye con la
    misma regla del espejo (`bi_sync._resolver_profesional_pago`: corrección
    manual → registro de recepción → atención). `otro_profesional` significa
    entonces "hoy la atribución da otro profesional que el espejo".

Auth: SOLO el token del dueño (OLACORE_TOKEN), igual que Trayectoria y Campañas
Meta — este módulo muestra cuánto se lleva cada profesional; el token de
recepción (ADMIN_TOKEN) y la cookie `admin` (compartida) NO lo abren.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

log = logging.getLogger("remuneraciones_pro")
_CL = ZoneInfo("America/Santiago")
_TEMPLATE = Path(__file__).parent.parent / "templates" / "alma_remuneraciones.html"

router = APIRouter(prefix="/api/remuneraciones", tags=["remuneraciones"])

# Intervalo mínimo entre llamadas a Medilink del barrido (segundos). El bot y el
# sync del espejo comparten el mismo cupo de la API: no bajar de 4.
INTERVALO_S = 4.0
# Días seguidos con error antes de abortar el barrido (Medilink caído/suspendido).
MAX_ERRORES_SEGUIDOS = 3
# Tolerancia en pesos para decir que dos montos calzan (misma que el auditor).
TOLERANCIA = 1
# Repeticiones de la MISMA diferencia (esperado, caja) para tratarla como patrón.
MIN_PATRON = 5

TIPOS = ("ok", "ok_patron", "fantasma_sistema", "falta_en_sistema", "otro_profesional", "monto_distinto",
         "sin_registro_modulo", "solo_en_modulo", "dif_modulo", "prof_distinto_modulo")
# Orden de prioridad cuando una línea tiene más de un problema: primero lo que
# mueve la base de pago, después las alertas de recepción.
_PRIORIDAD = ("otro_profesional", "fantasma_sistema", "falta_en_sistema", "monto_distinto",
              "solo_en_modulo", "prof_distinto_modulo", "dif_modulo", "sin_registro_modulo")
_TIPOS_CAJA = {"otro_profesional", "fantasma_sistema", "falta_en_sistema", "monto_distinto"}
# Lo que enciende el badge rojo del profesional: mueve la base de pago o es un
# faltante real. `dif_modulo` puntual es alerta (n_alertas) y `ok_patron` (la
# misma diferencia repetida ≥MIN_PATRON veces: tarifa/tecleo sistemático) se
# lista solo agrupado en `patrones`.
_TIPOS_BADGE = _TIPOS_CAJA | {"sin_registro_modulo", "solo_en_modulo", "prof_distinto_modulo"}

_MES_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# ═════════════════════════════ Decisión del dueño ═══════════════════════════

def _linea_cuenta_para_pago(linea: dict) -> bool:
    """¿Esta línea entra a la base de pago del profesional?

    Default: cuenta si el pago EXISTE en Medilink en vivo (mes verificado) o, si
    el mes aún no se verificó, si existe en el sistema (espejo). Trade-off:
    `solo_en_modulo` (recepción cobró pero Medilink no tiene pago, atención sin
    cerrar) NO cuenta → no se paga plata que la caja no respalda, a costa de
    atrasar el pago al profesional hasta que recepción cierre la atención.
    Para pagarlo igual: `return True` cuando linea["tipo"] == "solo_en_modulo".
    Antes de la regla manda la decisión explícita del dueño para ESA línea
    (`linea["decision"]`, POST /linea-decision; hoy solo en solo_en_modulo).
    """
    if linea.get("decision") is not None:
        return bool(linea["decision"])
    if linea.get("verificado"):
        return linea.get("medilink") is not None
    return linea.get("sistema") is not None


def _monto_linea(linea: dict) -> int:
    """Monto con que la línea entra a la base si cuenta: manda Medilink en un mes
    verificado y el sistema si no; si la regla de arriba deja pasar una línea
    sin caja (p. ej. solo_en_modulo), se usa lo que esperaba recepción."""
    orden = ("medilink", "sistema") if linea.get("verificado") else ("sistema", "medilink")
    for k in orden:
        if linea.get(k):
            return int(linea[k]["monto"])
    m = linea.get("modulo")
    return int(m["esperado"]) if m else 0


# ═════════════════════════════════ Auth ═════════════════════════════════════

def _token_de(request: Request | None, token: str | None) -> str | None:
    if token:
        return token
    if request is not None:
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            return h.split(None, 1)[1].strip()
    return None


def _auth(request: Request | None, token: str | None) -> str:
    """Solo el token del dueño. No se usa `_is_admin_token` a propósito: acepta
    el token de recepción, y aquí se ve cuánto gana cada profesional."""
    from config import OLACORE_TOKEN
    tk = _token_de(request, token)
    if tk and OLACORE_TOKEN and hmac.compare_digest(tk, OLACORE_TOKEN):
        return tk
    raise HTTPException(403, "Solo el token del dueño abre Remuneraciones")


# ═════════════════════════════ Tablas propias ═══════════════════════════════

def _ensure_tablas(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS remun_medilink_pagos (
        mes TEXT NOT NULL, pago_id INTEGER PRIMARY KEY, fecha TEXT NOT NULL,
        id_profesional INTEGER, id_paciente INTEGER, nombre_paciente TEXT,
        monto INTEGER NOT NULL DEFAULT 0, fetched_at TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_remun_ml_mes ON remun_medilink_pagos(mes)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_remun_ml_fecha ON remun_medilink_pagos(fecha)")
    # Un día solo se considera verificado si Medilink respondió COMPLETO (ok=1).
    c.execute("""CREATE TABLE IF NOT EXISTS remun_medilink_dias (
        fecha TEXT PRIMARY KEY, mes TEXT NOT NULL, ok INTEGER NOT NULL DEFAULT 0,
        n INTEGER DEFAULT 0, monto INTEGER DEFAULT 0, error TEXT, fetched_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS remun_verificacion (
        mes TEXT PRIMARY KEY, estado TEXT, started_at TEXT, finished_at TEXT,
        dias_total INTEGER DEFAULT 0, dias_ok INTEGER DEFAULT 0, dias_error INTEGER DEFAULT 0,
        dia_actual TEXT, error TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS remun_ajustes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, id_profesional INTEGER NOT NULL,
        mes TEXT NOT NULL, monto INTEGER NOT NULL, motivo TEXT NOT NULL DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_remun_aj ON remun_ajustes(mes, id_profesional)")
    # Registro de envíos de la liquidación por WhatsApp (el envío real lo hace el
    # dueño en wa.me; aquí solo queda cuándo lo marcó).
    c.execute("""CREATE TABLE IF NOT EXISTS remun_envios (
        id INTEGER PRIMARY KEY AUTOINCREMENT, id_profesional INTEGER NOT NULL, mes TEXT NOT NULL,
        canal TEXT DEFAULT 'whatsapp', at TEXT DEFAULT (datetime('now')))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_remun_env ON remun_envios(mes, id_profesional)")
    # Decisión del dueño por línea (hoy: incluir/excluir un solo_en_modulo).
    c.execute("""CREATE TABLE IF NOT EXISTS remun_decisiones (
        mes TEXT NOT NULL, id_profesional INTEGER NOT NULL, clave_linea TEXT NOT NULL,
        incluir INTEGER NOT NULL, at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (mes, id_profesional, clave_linea))""")
    from remuneraciones_routes import _ensure_estado, _ensure_cols
    _ensure_estado(c)
    _ensure_cols(c)
    try:
        c.execute("ALTER TABLE remuneracion_estado ADD COLUMN monto_pagado INTEGER")
    except Exception:
        pass


# ═════════════════════════════ Utilidades ═══════════════════════════════════

def _validar_mes(mes: str | None) -> str:
    if not mes:
        return datetime.now(_CL).strftime("%Y-%m")
    if not _MES_RE.match(mes):
        raise HTTPException(400, "mes debe ser YYYY-MM")
    return mes


def _ahora_utc() -> str:
    from datetime import timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _hoy() -> date:
    return datetime.now(_CL).date()


def _dias_del_mes(mes: str) -> list[str]:
    """Días del mes hasta hoy (CLT). Un mes futuro devuelve []."""
    from ebitda_routes import _mes_bounds
    ini, fin = _mes_bounds(mes)
    d, f, hoy = date.fromisoformat(ini), date.fromisoformat(fin), _hoy()
    out = []
    while d < f and d <= hoy:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _clp(n) -> str:
    n = int(round(n or 0))
    s = "$" + f"{abs(n):,}".replace(",", ".")
    return ("−" + s) if n < 0 else s


def _ddmm(f: str) -> str:
    return f"{f[8:10]}/{f[5:7]}" if f and len(f) >= 10 else (f or "")


_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")


def _mes_largo(mes: str) -> str:
    return f"{_MESES[int(mes[5:7]) - 1]} {mes[:4]}"


def _nombres_prof(c) -> dict:
    """id → {nombre, especialidad, telefono} (PROFESIONALES del bot y equipo_cmc)."""
    from medilink import PROFESIONALES
    out = {pid: {"nombre": i.get("nombre") or f"Prof {pid}", "especialidad": i.get("especialidad", ""),
                 "telefono": ""} for pid, i in PROFESIONALES.items()}
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(equipo_cmc)").fetchall()}
        col_upd = "updated_at" if "updated_at" in cols else "NULL AS updated_at"
        for r in c.execute(f"SELECT id_medilink, nombre, especialidad, telefono, {col_upd} FROM equipo_cmc "
                           "WHERE id_medilink IS NOT NULL").fetchall():
            o = out.setdefault(r["id_medilink"], {"nombre": r["nombre"] or f"Prof {r['id_medilink']}",
                                                  "especialidad": r["especialidad"] or "", "telefono": ""})
            o["telefono"] = re.sub(r"\D", "", r["telefono"] or "")
            o["pct_actualizado_at"] = r["updated_at"]
            if not o.get("especialidad"):
                o["especialidad"] = r["especialidad"] or ""
    except Exception:
        pass
    return out


def _nombre_prof(info: dict, pid) -> str:
    if pid is None:
        return "sin profesional"
    return (info.get(pid) or {}).get("nombre") or f"Prof {pid}"


# ═════════════════════════════ Lectura de fuentes ═══════════════════════════

def _leer_sistema(c, ini: str, fin: str) -> list[dict]:
    rows = c.execute(
        """SELECT p.pago_id, p.fecha, p.id_profesional, p.id_paciente, p.monto,
                  COALESCE(NULLIF(TRIM(p.nombre_paciente),''),
                    (SELECT a.paciente_nombre FROM bi_atenciones a WHERE a.atencion_id = p.atencion_id),
                    (SELECT a.paciente_nombre FROM bi_atenciones a WHERE a.id_paciente = p.id_paciente
                       AND COALESCE(a.paciente_nombre,'') <> '' ORDER BY a.atencion_id DESC LIMIT 1),
                    '') AS nombre
           FROM bi_pagos_caja p WHERE p.fecha >= ? AND p.fecha < ?""", (ini, fin)).fetchall()
    return [{"pago_id": int(r["pago_id"]), "fecha": r["fecha"], "id_profesional": r["id_profesional"],
             "id_paciente": r["id_paciente"], "monto": int(r["monto"] or 0),
             "nombre": " ".join((r["nombre"] or "").split())} for r in rows]


def _leer_medilink(c, mes: str) -> tuple[list[dict], set]:
    """Pagos de la cache SOLO de días verificados completos + set de esos días."""
    dias_ok = {r["fecha"] for r in c.execute(
        "SELECT fecha FROM remun_medilink_dias WHERE mes=? AND ok=1", (mes,)).fetchall()}
    rows = c.execute("SELECT pago_id, fecha, id_profesional, id_paciente, nombre_paciente, monto "
                     "FROM remun_medilink_pagos WHERE mes=?", (mes,)).fetchall()
    return ([{"pago_id": int(r["pago_id"]), "fecha": r["fecha"], "id_profesional": r["id_profesional"],
              "id_paciente": r["id_paciente"], "monto": int(r["monto"] or 0),
              "nombre": " ".join((r["nombre_paciente"] or "").split())}
             for r in rows if r["fecha"] in dias_ok], dias_ok)


def _leer_modulo(c, ini: str, fin: str) -> list[dict]:
    """Cobros de recepción. La columna `bonificacion` está siempre en 0: la
    bonificación Fonasa se estima por arancel N3 (`_bonif_desde_arancel`).
    Filas sin cobro (copago 0 y sin medio = prellenadas) no entran."""
    from conciliacion_routes import _bonif_desde_arancel
    cols = {r[1] for r in c.execute("PRAGMA table_info(pagos_cmc)").fetchall()}
    col_idp = "id_paciente" if "id_paciente" in cols else "NULL AS id_paciente"
    rows = c.execute(
        f"""SELECT id, fecha, hora, paciente_nombre, rut, id_profesional, profesional, area,
                   prevision, copago, metodo_pago, {col_idp}
            FROM pagos_cmc WHERE fecha >= ? AND fecha < ? ORDER BY fecha, hora, id""",
        (ini, fin)).fetchall()
    out = []
    for r in rows:
        copago = int(r["copago"] or 0)
        metodo = (r["metodo_pago"] or "").strip()
        if copago <= 0 and not metodo:
            continue
        es_fonasa = (r["prevision"] or "").strip().lower() == "fonasa"
        bonif = int(_bonif_desde_arancel(r["area"] or "")) if es_fonasa else 0
        if copago + bonif <= 0:
            continue
        out.append({"id": r["id"], "fecha": r["fecha"], "nombre": " ".join((r["paciente_nombre"] or "").split()),
                    "rut": (r["rut"] or "").strip(), "id_profesional": r["id_profesional"],
                    "profesional": r["profesional"] or "", "prevision": (r["prevision"] or "").lower(),
                    "copago": copago, "bonif_est": bonif, "esperado": copago + bonif,
                    "metodo": metodo, "id_paciente": r["id_paciente"]})
    return out


# ═════════════════════════════ Motor de conciliación ════════════════════════

def _clave_pac(id_paciente, nombre: str) -> str:
    if id_paciente:
        return f"id:{id_paciente}"
    from conciliacion_routes import normalizar_texto
    return "n:" + normalizar_texto(nombre or "")


def _mismo_paciente(a_id, a_nom: str, b_id, b_nom: str) -> float:
    """1.0 si comparten id_paciente; si no, similitud robusta de nombres
    (`conciliacion_routes._sim_paciente`: tokens del nombre más corto, ≥2 en
    común salvo nombres de un token). 0 si los id existen y difieren."""
    if a_id and b_id:
        return 1.0 if int(a_id) == int(b_id) else 0.0
    from conciliacion_routes import _sim_paciente
    return _sim_paciente(a_nom, b_nom)


def _patron_id(esperado: int, caja: int) -> str:
    """Id estable de un patrón: depende solo de (esperado recepción, monto caja)."""
    return f"p{int(esperado)}-{int(caja)}"


def _conciliar(sistema: list[dict], medilink: list[dict], dias_ok: set, modulo: list[dict],
               mes_verificado: bool, nombres: dict, decisiones: dict | None = None) -> list[dict]:
    """Cruce puro de las tres fuentes → lista de líneas (ver contrato)."""
    # ── 1. Sistema ↔ Medilink por pago_id ─────────────────────────────────
    s_por_id = {p["pago_id"]: p for p in sistema}
    l_por_id = {p["pago_id"]: p for p in medilink}
    lineas: dict = {}

    def _linea(fecha, pid, id_pac, nombre):
        k = (fecha, pid, _clave_pac(id_pac, nombre))
        ln = lineas.get(k)
        if ln is None:
            ln = lineas[k] = {"fecha": fecha, "id_profesional": pid, "paciente": nombre or "",
                              "clave_linea": f"{fecha}|{pid}|{k[2]}",
                              "id_paciente": id_pac, "sistema": None, "medilink": None, "modulo": None,
                              "_tipos": set(), "_otro": set(), "_mod_fecha": None, "_mod_prof": None,
                              "dia_verificado": fecha in dias_ok}
        if nombre and len(nombre) > len(ln["paciente"]):
            ln["paciente"] = nombre
        if not ln["id_paciente"] and id_pac:
            ln["id_paciente"] = id_pac
        return ln

    def _sumar(ln, lado, p, **extra):
        cur = ln[lado] or {"monto": 0, "pago_ids": [], **extra}
        cur["monto"] += p["monto"]
        cur["pago_ids"].append(p["pago_id"])
        ln[lado] = cur

    for pago_id in sorted(set(s_por_id) | set(l_por_id)):
        s, l = s_por_id.get(pago_id), l_por_id.get(pago_id)
        fecha = (l or s)["fecha"]
        nombre = (l and l["nombre"]) or (s and s["nombre"]) or ""
        id_pac = (l and l["id_paciente"]) or (s and s["id_paciente"])
        if s and not (fecha in dias_ok or s["fecha"] in dias_ok):
            ln = _linea(s["fecha"], s["id_profesional"], id_pac, nombre)   # día sin verificar
            _sumar(ln, "sistema", s)
            continue
        if s and l:
            if s["id_profesional"] != l["id_profesional"]:
                sale = _linea(s["fecha"], s["id_profesional"], id_pac, nombre)
                _sumar(sale, "sistema", s)
                sale["_tipos"].add("otro_profesional")
                sale["_otro"].add(l["id_profesional"])
                entra = _linea(l["fecha"], l["id_profesional"], id_pac, nombre)
                _sumar(entra, "medilink", l, id_profesional=l["id_profesional"])
                entra["_tipos"].add("otro_profesional")
                entra["_otro"].add(s["id_profesional"])
                continue
            ln = _linea(fecha, l["id_profesional"], id_pac, nombre)
            _sumar(ln, "sistema", s)
            _sumar(ln, "medilink", l, id_profesional=l["id_profesional"])
            continue
        if s:
            ln = _linea(s["fecha"], s["id_profesional"], id_pac, nombre)
            _sumar(ln, "sistema", s)
            ln["_tipos"].add("fantasma_sistema")
            continue
        ln = _linea(l["fecha"], l["id_profesional"], id_pac, nombre)
        _sumar(ln, "medilink", l, id_profesional=l["id_profesional"])
        ln["_tipos"].add("falta_en_sistema")

    for ln in lineas.values():
        if ln["sistema"] and ln["medilink"] and abs(ln["sistema"]["monto"] - ln["medilink"]["monto"]) > TOLERANCIA:
            ln["_tipos"].add("monto_distinto")

    # ── 2. Módulo Pagos agrupado por paciente-día-profesional ────────────
    grupos: dict = {}
    for r in modulo:
        k = (r["fecha"], r["id_profesional"], r["id_paciente"] and f"id:{r['id_paciente']}"
             or r["rut"] or _clave_pac(None, r["nombre"]))
        g = grupos.get(k)
        if g is None:
            g = grupos[k] = {"fecha": r["fecha"], "id_profesional": r["id_profesional"],
                             "nombre": r["nombre"], "id_paciente": r["id_paciente"], "copago": 0,
                             "bonif_est": 0, "esperado": 0, "metodos": [], "ids": [],
                             "prevision": r["prevision"]}
        g["copago"] += r["copago"]
        g["bonif_est"] += r["bonif_est"]
        g["esperado"] += r["esperado"]
        if r["metodo"] and r["metodo"] not in g["metodos"]:
            g["metodos"].append(r["metodo"])
        g["ids"].append(r["id"])
    gl = list(grupos.values())
    caja = [ln for ln in lineas.values() if ln["sistema"] or ln["medilink"]]

    def _caja_ref(ln) -> int:
        if ln["dia_verificado"] and ln["medilink"]:
            return ln["medilink"]["monto"]
        return (ln["sistema"] or ln["medilink"])["monto"]

    por_fecha: dict = defaultdict(list)
    for j, ln in enumerate(caja):
        por_fecha[ln["fecha"]].append(j)
    usados_g, usados_c = set(), set()

    def _pasada(mismo_prof: bool, desfase: int):
        pares = []
        for i, g in enumerate(gl):
            if i in usados_g:
                continue
            f0 = date.fromisoformat(g["fecha"])
            fechas = [g["fecha"]] if desfase == 0 else [
                (f0 + timedelta(days=d)).isoformat() for d in (-desfase, desfase)]
            for f in fechas:
                for j in por_fecha.get(f, ()):
                    if j in usados_c:
                        continue
                    ln = caja[j]
                    if (ln["id_profesional"] == g["id_profesional"]) != mismo_prof:
                        continue
                    sim = _mismo_paciente(g["id_paciente"], g["nombre"], ln["id_paciente"], ln["paciente"])
                    if sim >= 0.66:
                        pares.append((-sim, abs(g["esperado"] - _caja_ref(ln)), i, j))
        pares.sort()
        for _s, _d, i, j in pares:
            if i in usados_g or j in usados_c:
                continue
            usados_g.add(i)
            usados_c.add(j)
            g, ln = gl[i], caja[j]
            ln["modulo"] = {"copago": g["copago"], "bonif_est": g["bonif_est"], "esperado": g["esperado"],
                            "metodo": ", ".join(g["metodos"]), "id": g["ids"][0], "ids": g["ids"],
                            "id_profesional": g["id_profesional"], "prevision": g["prevision"]}
            if g["fecha"] != ln["fecha"]:
                ln["_mod_fecha"] = g["fecha"]
            if not mismo_prof:
                ln["_tipos"].add("prof_distinto_modulo")
                ln["_mod_prof"] = g["id_profesional"]
            if abs(g["esperado"] - _caja_ref(ln)) > TOLERANCIA:
                ln["_tipos"].add("dif_modulo")

    _pasada(True, 0)    # mismo día, mismo profesional
    _pasada(False, 0)   # mismo día, recepción lo cargó a otro profesional
    _pasada(True, 1)    # ±1 día, mismo profesional (fecha mal tecleada)

    for j, ln in enumerate(caja):
        if j not in usados_c:
            ln["_tipos"].add("sin_registro_modulo")
    for i, g in enumerate(gl):
        if i in usados_g:
            continue
        if g["copago"] <= 0:
            # Placeholder: fila prellenada sin cobro del paciente y sin pago en
            # caja (solo bonificación estimada). Silenciosa: no es un faltante.
            continue
        ln = _linea(g["fecha"], g["id_profesional"], g["id_paciente"], g["nombre"])
        ln["modulo"] = {"copago": g["copago"], "bonif_est": g["bonif_est"], "esperado": g["esperado"],
                        "metodo": ", ".join(g["metodos"]), "id": g["ids"][0], "ids": g["ids"],
                        "id_profesional": g["id_profesional"], "prevision": g["prevision"]}
        ln["_tipos"].add("solo_en_modulo")

    # ── 3. Patrones de dif_modulo (tarifa distinta sistemática) ──────────
    frec = Counter((ln["modulo"]["esperado"], _caja_ref(ln)) for ln in lineas.values()
                   if "dif_modulo" in ln["_tipos"])

    # ── 4. Tipo, diferencia, explicación, cuenta_para_pago ───────────────
    out = []
    for ln in lineas.values():
        tipos = [t for t in _PRIORIDAD if t in ln["_tipos"]]
        e_m0 = ln["modulo"]["esperado"] if ln["modulo"] else 0
        patron_n = 0
        if "dif_modulo" in ln["_tipos"]:
            n = frec[(e_m0, _caja_ref(ln))]
            if n >= MIN_PATRON:
                patron_n = n
                tipos = [("ok_patron" if t == "dif_modulo" else t) for t in tipos]
        tipo = tipos[0] if tipos else "ok"
        ln["verificado"] = bool(mes_verificado and ln["dia_verificado"])
        s_m = ln["sistema"]["monto"] if ln["sistema"] else 0
        l_m = ln["medilink"]["monto"] if ln["medilink"] else 0
        e_m = ln["modulo"]["esperado"] if ln["modulo"] else 0
        if tipo in _TIPOS_CAJA:
            dif = l_m - s_m
        elif tipo == "solo_en_modulo":
            dif = e_m
        elif tipo in ("dif_modulo", "ok_patron", "prof_distinto_modulo"):
            dif = e_m - _caja_ref(ln)
        elif tipo == "sin_registro_modulo":
            dif = _caja_ref(ln)
        else:
            dif = 0
        linea = {
            "fecha": ln["fecha"], "paciente": ln["paciente"] or (
                f"Paciente #{ln['id_paciente']}" if ln["id_paciente"] else "Paciente sin nombre"),
            "id_paciente": ln["id_paciente"], "id_profesional": ln["id_profesional"],
            "sistema": ln["sistema"], "medilink": ln["medilink"], "modulo": ln["modulo"],
            "tipo": tipo, "tipos": tipos or ["ok"], "diferencia": int(dif),
            "verificado": ln["verificado"], "dia_verificado": ln["dia_verificado"],
            "otro_profesional_id": next(iter(ln["_otro"]), None) if ln["_otro"] else None,
            "clave_linea": ln["clave_linea"], "decision": None,
            "patron_id": _patron_id(e_m, _caja_ref(ln)) if "ok_patron" in tipos else None,
        }
        linea["patron"] = linea["patron_id"]          # alias que lee el front
        if tipo == "solo_en_modulo" and decisiones:
            d = decisiones.get((ln["id_profesional"], ln["clave_linea"]))
            linea["decision"] = None if d is None else bool(d)
        linea["explicacion"] = _explicar(linea, ln, nombres, patron_n)
        if linea["decision"] is not None:
            linea["explicacion"] += (" Decisión suya: se INCLUYE en el pago." if linea["decision"]
                                     else " Decisión suya: se EXCLUYE del pago.")
        linea["cuenta_para_pago"] = bool(_linea_cuenta_para_pago(linea))
        linea["monto_pago"] = _monto_linea(linea) if linea["cuenta_para_pago"] else 0
        # Efecto potencial sobre el pago respecto del sistema, como si el día
        # verificado ya mandara: sirve para "si confirmas Medilink cambia en $X".
        hip = dict(linea, verificado=ln["dia_verificado"])
        pago_hip = _monto_linea(hip) if _linea_cuenta_para_pago(hip) else 0
        linea["efecto_pago"] = int(pago_hip - s_m)
        out.append(linea)
    out.sort(key=lambda x: (x["fecha"], x["paciente"]))
    return out


def _explicar(linea: dict, ln: dict, nombres: dict, patron_n: int) -> str:
    t = linea["tipo"]
    s, l, m = linea["sistema"], linea["medilink"], linea["modulo"]
    ids = lambda x: ", ".join(f"#{i}" for i in x["pago_ids"])  # noqa: E731
    otro = _nombre_prof(nombres, linea["otro_profesional_id"]) if linea["otro_profesional_id"] else ""
    partes = []
    if t == "otro_profesional":
        if s and not l:
            partes.append(f"El sistema cargó este pago ({_clp(s['monto'])}, {ids(s)}) a este profesional, "
                          f"pero hoy la atribución lo asigna a {otro}: sale de su base.")
        else:
            partes.append(f"El sistema tenía este pago ({_clp(l['monto'])}, {ids(l)}) a nombre de {otro}; "
                          f"hoy la atribución lo asigna a este profesional: entra a su base.")
        partes.append("Medilink no informa el profesional en la caja; se atribuye con la misma regla del "
                      "sistema (corrección manual, registro de recepción, atención).")
    elif t == "fantasma_sistema":
        partes.append(f"El sistema tiene {_clp(s['monto'])} ({ids(s)}), pero Medilink en vivo ya no tiene "
                      f"ese pago: se anuló después de la última sincronización. No corresponde pagarlo.")
    elif t == "falta_en_sistema":
        partes.append(f"Medilink en vivo tiene {_clp(l['monto'])} ({ids(l)}) que el sistema aún no trae: "
                      f"la sincronización está atrasada. Sí corresponde pagarlo.")
    elif t == "monto_distinto":
        partes.append(f"El sistema dice {_clp(s['monto'])} y Medilink en vivo {_clp(l['monto'])} "
                      f"(diferencia {_clp(l['monto'] - s['monto'])}). Manda Medilink.")
    elif t == "solo_en_modulo":
        partes.append(f"Recepción registró un cobro de {_clp(m['esperado'])}"
                      + (f" ({m['metodo']})" if m.get("metodo") else "")
                      + ", pero la caja de Medilink no tiene pago de este paciente ese día (atención sin "
                        "cerrar en caja). Por defecto no se paga hasta que quede en Medilink.")
    elif t == "prof_distinto_modulo":
        partes.append(f"Recepción lo registró a nombre de {_nombre_prof(nombres, ln['_mod_prof'])}, pero la "
                      f"caja lo atribuye a este profesional. No cambia el pago; revisar el registro.")
    elif t in ("dif_modulo", "ok_patron"):
        caja = l["monto"] if (l and linea["dia_verificado"]) else (s or l)["monto"]
        partes.append(f"Recepción esperaba {_clp(m['esperado'])}"
                      + (f" (copago {_clp(m['copago'])} + bonificación Fonasa estimada {_clp(m['bonif_est'])})"
                         if m["bonif_est"] else "")
                      + f" y la caja tiene {_clp(caja)}: diferencia {_clp(m['esperado'] - caja)}. "
                        "No cambia el pago.")
        if patron_n:
            partes.append(f"Se repite igual en {patron_n} atenciones del mes: es un patrón ("
                          + _causa_patron(m["copago"], m["bonif_est"], m["esperado"], caja)
                          + "), no un error puntual. Se considera conciliada.")
    elif t == "sin_registro_modulo":
        caja = (l if (l and linea["dia_verificado"]) else (s or l))["monto"]
        partes.append(f"Está en caja ({_clp(caja)}) pero recepción no lo registró en el módulo Pagos. "
                      "No cambia el pago; es una alerta de caja.")
    else:
        monto = (l or s)["monto"]
        if linea["dia_verificado"]:
            partes.append(f"Calza en las tres fuentes: sistema, Medilink en vivo y módulo Pagos ({_clp(monto)}).")
        else:
            partes.append(f"Sistema y módulo Pagos calzan ({_clp(monto)}). Medilink en vivo aún no se "
                          "verificó para este día.")
    # Problemas secundarios (la línea tiene más de uno).
    sec = [x for x in linea["tipos"][1:] if x != "ok"]
    if sec:
        partes.append("También: " + ", ".join(_ETIQUETA[x] for x in sec) + ".")
    if ln.get("_mod_fecha"):
        partes.append(f"Recepción lo registró con fecha {_ddmm(ln['_mod_fecha'])}.")
    return " ".join(partes)


_ETIQUETA = {
    "ok": "calza", "fantasma_sistema": "anulado en Medilink", "falta_en_sistema": "falta en el sistema",
    "otro_profesional": "atribuido a otro profesional", "monto_distinto": "monto distinto en Medilink",
    "sin_registro_modulo": "sin registro de recepción", "solo_en_modulo": "solo en el módulo Pagos",
    "dif_modulo": "monto distinto en recepción", "ok_patron": "diferencia de patrón conocido", "prof_distinto_modulo": "recepción lo cargó a otro profesional",
}


def _causa_patron(copago: int, bonif: int, esperado: int, caja: int) -> str:
    """Explicación en palabras de una diferencia repetida módulo↔caja."""
    if bonif and copago == bonif:
        return "recepción teclea la bonificación como copago"
    if bonif and esperado < caja:
        return "posible tramo Fonasa o copago distinto al arancel"
    if bonif and esperado > caja:
        return "arancel del módulo Pagos distinto al cobrado en Medilink"
    return "tarifa distinta entre el módulo Pagos y Medilink"


def _patrones(lineas: list[dict]) -> list[dict]:
    """Agrupa diferencias módulo↔caja idénticas (p. ej. $630 en Fonasa MG)."""
    grupos: dict = defaultdict(lambda: {"n": 0, "prev": Counter(), "causa": Counter(), "f0": "9", "f1": "0"})
    for x in lineas:
        if "ok_patron" not in x["tipos"] or not x["modulo"]:
            continue
        caja = x["medilink"]["monto"] if (x["medilink"] and x["dia_verificado"]) else (
            x["sistema"] or x["medilink"])["monto"]
        g = grupos[(x["modulo"]["esperado"], caja)]
        g["n"] += 1
        g["causa"][_causa_patron(x["modulo"]["copago"], x["modulo"]["bonif_est"],
                                 x["modulo"]["esperado"], caja)] += 1
        g["prev"][x["modulo"].get("prevision") or "?"] += 1
        g["f0"], g["f1"] = min(g["f0"], x["fecha"]), max(g["f1"], x["fecha"])
    out = []
    # Las líneas ok_patron ya pasaron el umbral a nivel MES: aquí no se vuelve a
    # filtrar, así el detalle de un profesional muestra su parte del patrón.
    for (esp, caja), g in grupos.items():
        prev = g["prev"].most_common(1)[0][0]
        causa = g["causa"].most_common(1)[0][0]
        signo = "+" if esp >= caja else "−"
        out.append({
            "patron_id": _patron_id(esp, caja), "id": _patron_id(esp, caja),
            "descripcion": (f"{g['n']} atenciones ({prev}) con recepción {_clp(esp)} y caja {_clp(caja)}: "
                            f"{signo}{_clp(abs(esp - caja))} c/u entre el {_ddmm(g['f0'])} y el "
                            f"{_ddmm(g['f1'])}. Causa probable: {causa}. No cambia el pago."),
            "causa": causa, "n": g["n"], "monto": (esp - caja) * g["n"],
            "diferencia_unitaria": esp - caja, "esperado": esp, "caja": caja, "prevision": prev,
        })
    out.sort(key=lambda p: -abs(p["monto"]))
    return out


# ═════════════════════════════ Estado de verificación ══════════════════════

_TAREA: dict = {"task": None, "mes": None}
_LOCK = asyncio.Lock()


def _estado_verificacion(c, mes: str) -> dict:
    dias = _dias_del_mes(mes)
    ok = {r["fecha"] for r in c.execute(
        "SELECT fecha FROM remun_medilink_dias WHERE mes=? AND ok=1", (mes,)).fetchall()}
    err = {r["fecha"] for r in c.execute(
        "SELECT fecha FROM remun_medilink_dias WHERE mes=? AND ok=0", (mes,)).fetchall()}
    v = c.execute("SELECT * FROM remun_verificacion WHERE mes=?", (mes,)).fetchone()
    en_curso = bool(_TAREA["task"] and not _TAREA["task"].done() and _TAREA["mes"] == mes)
    dias_ok = len([d for d in dias if d in ok])
    if en_curso:
        estado = "en_curso"
    elif dias and dias_ok == len(dias):
        estado = "ok"
    elif dias_ok == 0 and not err:
        estado = "sin_verificar"
    else:
        # Parcial o interrumpido (reinicio a mitad de barrido): no se usa como base.
        estado = "parcial"
    return {"mes": mes, "estado": estado, "en_curso": en_curso,
            "at": (v["finished_at"] if v else None),
            "started_at": v["started_at"] if v else None,
            "dias_ok": dias_ok, "dias_total": len(dias),
            "dias_error": len([d for d in dias if d in err and d not in ok]),
            "dia_actual": (v["dia_actual"] if (v and en_curso) else None),
            "error": v["error"] if v else None}


# ═════════════════════════════ Barrido Medilink ═════════════════════════════

class _ClienteEspaciado:
    """Envuelve httpx.AsyncClient: ≥INTERVALO_S entre llamadas (incluidos los
    reintentos 429 de bi_sync) y recuerda el último status para saber si el día
    terminó completo o abortado (fallo cerrado)."""

    def __init__(self, cli, intervalo: float):
        self.cli, self.intervalo, self._ultima = cli, intervalo, 0.0
        self.ultimo_status: int | None = None
        self.n = self.n429 = 0

    async def get(self, *a, **k):
        espera = self.intervalo - (time.monotonic() - self._ultima)
        if espera > 0:
            await asyncio.sleep(espera)
        self._ultima = time.monotonic()
        self.n += 1
        try:
            r = await self.cli.get(*a, **k)
        except Exception:
            self.ultimo_status = None
            raise
        self.ultimo_status = r.status_code
        if r.status_code == 429:
            self.n429 += 1
        return r


async def _fetch_dia(cli: _ClienteEspaciado, fecha: str) -> tuple[list[dict], bool, str]:
    """(pagos, completo, error). Reusa `bi_sync._fetch_pagos_dia` (pagina por id,
    backoff 429). Completo solo si la última respuesta fue 200."""
    import bi_sync
    pagos: list[dict] = []
    cli.ultimo_status = None
    try:
        async for batch in bi_sync._fetch_pagos_dia(cli, fecha):
            pagos.extend(batch)
    except Exception as e:  # noqa: BLE001
        return pagos, False, f"excepción: {e}"[:200]
    if cli.ultimo_status != 200:
        return pagos, False, f"Medilink respondió {cli.ultimo_status or 'sin respuesta'}"
    return pagos, True, ""


def _guardar_dia(mes: str, fecha: str, pagos: list[dict], ok: bool, error: str) -> None:
    """Persiste un día. Solo con ok=True reemplaza la cache de ese día; un día
    fallido conserva lo anterior y queda marcado ok=0."""
    from session import db
    import bi_sync
    with db() as c:
        _ensure_tablas(c)
        if ok:
            c.execute("DELETE FROM remun_medilink_pagos WHERE fecha=?", (fecha,))
            ahora = _ahora_utc()
            vistos = set()
            for p in pagos:
                pid = p.get("id")
                if not pid or pid in vistos:
                    continue
                vistos.add(pid)
                idp = p.get("id_profesional")
                if idp is None:
                    try:
                        idp, _ = bi_sync._resolver_profesional_pago(c, p)
                    except Exception as e:  # noqa: BLE001
                        log.warning("remun: no se pudo atribuir pago %s: %s", pid, e)
                        idp = None
                f = (p.get("fecha_recepcion") or p.get("fecha") or fecha)[:10]
                c.execute("""INSERT INTO remun_medilink_pagos
                    (mes, pago_id, fecha, id_profesional, id_paciente, nombre_paciente, monto, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(pago_id) DO UPDATE SET mes=excluded.mes, fecha=excluded.fecha,
                      id_profesional=excluded.id_profesional, id_paciente=excluded.id_paciente,
                      nombre_paciente=excluded.nombre_paciente, monto=excluded.monto,
                      fetched_at=excluded.fetched_at""",
                          (f[:7], int(pid), f, idp, p.get("id_paciente"),
                           (p.get("nombre_paciente") or "").strip() or None,
                           int(p.get("monto_pago") or p.get("monto") or 0), ahora))
            c.execute("""INSERT INTO remun_medilink_dias (fecha, mes, ok, n, monto, error, fetched_at)
                VALUES (?,?,1,?,?,NULL,datetime('now'))
                ON CONFLICT(fecha) DO UPDATE SET mes=excluded.mes, ok=1, n=excluded.n,
                  monto=excluded.monto, error=NULL, fetched_at=excluded.fetched_at""",
                      (fecha, mes, len(vistos), sum(int(p.get("monto_pago") or p.get("monto") or 0)
                                                    for p in pagos if p.get("id"))))
        else:
            c.execute("""INSERT INTO remun_medilink_dias (fecha, mes, ok, error, fetched_at)
                VALUES (?,?,0,?,datetime('now'))
                ON CONFLICT(fecha) DO UPDATE SET ok=0, error=excluded.error,
                  fetched_at=excluded.fetched_at""", (fecha, mes, error))


def _marcar(mes: str, **campos) -> None:
    from session import db
    with db() as c:
        _ensure_tablas(c)
        c.execute("INSERT OR IGNORE INTO remun_verificacion (mes) VALUES (?)", (mes,))
        if campos:
            sets = ", ".join(f"{k}=?" for k in campos)
            c.execute(f"UPDATE remun_verificacion SET {sets} WHERE mes=?", (*campos.values(), mes))


def importar_pagos_medilink(mes: str, dias: dict[str, list[dict]]) -> None:
    """Puebla la cache con pagos ya leídos de Medilink (registro crudo de /pagos)
    por el mismo camino que el barrido. Lo usan los fixtures y los tests."""
    for fecha in sorted(dias):
        _guardar_dia(mes, fecha, dias[fecha] or [], True, "")
    _marcar(mes, estado="ok", finished_at=_ahora_utc(),
            dias_total=len(dias), dias_ok=len(dias), dias_error=0, dia_actual=None, error=None)


async def _barrido(mes: str, solo_pendientes: bool = False) -> None:
    """Recorre el mes día por día contra Medilink /pagos. Nunca escribe en Medilink."""
    import httpx
    import bi_sync
    dias = _dias_del_mes(mes)
    if solo_pendientes:
        from session import db
        with db() as c:
            _ensure_tablas(c)
            ok = {r["fecha"] for r in c.execute(
                "SELECT fecha FROM remun_medilink_dias WHERE mes=? AND ok=1", (mes,)).fetchall()}
        dias = [d for d in dias if d not in ok]
    await asyncio.to_thread(_marcar, mes, estado="en_curso",
                            started_at=_ahora_utc(),
                            finished_at=None, dias_total=len(_dias_del_mes(mes)), error=None)
    n_ok = n_err = seguidos = 0
    error_final = None
    try:
        async with httpx.AsyncClient(timeout=30) as raw:
            cli = _ClienteEspaciado(raw, INTERVALO_S)
            for fecha in dias:
                await asyncio.to_thread(_marcar, mes, dia_actual=fecha)
                # No pisar el sync del espejo: ambos usan el mismo cupo de Medilink.
                async with bi_sync.PAGOS_LOCK:
                    pagos, ok, err = await _fetch_dia(cli, fecha)
                await asyncio.to_thread(_guardar_dia, mes, fecha, pagos, ok, err)
                if ok:
                    n_ok += 1
                    seguidos = 0
                else:
                    n_err += 1
                    seguidos += 1
                    log.warning("remun barrido %s: día %s sin verificar (%s)", mes, fecha, err)
                    if seguidos >= MAX_ERRORES_SEGUIDOS:
                        error_final = f"Medilink no respondió {seguidos} días seguidos; barrido detenido en {fecha}."
                        break
            log.info("remun barrido %s: %d días ok, %d con error, %d llamadas, %d×429",
                     mes, n_ok, n_err, cli.n, cli.n429)
    except Exception as e:  # noqa: BLE001
        error_final = f"barrido interrumpido: {e}"[:300]
        log.exception("remun barrido %s falló", mes)
    finally:
        await asyncio.to_thread(_marcar, mes, estado=("error" if error_final else "fin"),
                                finished_at=_ahora_utc(),
                                dia_actual=None, error=error_final, dias_error=n_err)


async def iniciar_barrido(mes: str, solo_pendientes: bool = False) -> bool:
    """Lanza el barrido en background. False si ya hay uno corriendo (de
    cualquier mes): nunca dos a la vez contra Medilink."""
    async with _LOCK:
        t = _TAREA["task"]
        if t and not t.done():
            return False
        _TAREA["mes"] = mes
        _TAREA["task"] = asyncio.create_task(_barrido(mes, solo_pendientes))
        return True


# ═════════════════════════════ Liquidación ══════════════════════════════════

def _liquidar(pid: int, mes: str, base: int, pct_map: dict) -> dict:
    """Misma regla que `ebitda_routes._ebitda_mes` (fijo/pct/retención), sobre
    la base que se le pase."""
    from ebitda_routes import _regla_honorario, SIN_RETENCION, RETENCION_BOLETA
    pct, fijo = _regla_honorario(pid, mes, pct_map)
    bruto = fijo if fijo is not None else round(base * pct / 100)
    liquido = bruto if pid in SIN_RETENCION else round(bruto * (1 - RETENCION_BOLETA))
    return {"pct": pct, "fijo": fijo is not None, "monto_fijo": fijo, "bruto": int(bruto),
            "liquido": int(liquido), "retencion": int(bruto - liquido),
            "sin_retencion": pid in SIN_RETENCION}


def _calcular_mes(c, mes: str) -> dict:
    """Arma todo el mes: líneas por profesional, fuentes, conciliación y liquidación."""
    from ebitda_routes import _mes_bounds, _pct_map
    _ensure_tablas(c)
    ini, fin = _mes_bounds(mes)
    verif = _estado_verificacion(c, mes)
    mes_ok = verif["estado"] == "ok"
    nombres = _nombres_prof(c)
    sistema = _leer_sistema(c, ini, fin)
    medilink, dias_ok = _leer_medilink(c, mes)
    modulo = _leer_modulo(c, ini, fin)
    decisiones = {(r["id_profesional"], r["clave_linea"]): r["incluir"] for r in c.execute(
        "SELECT id_profesional, clave_linea, incluir FROM remun_decisiones WHERE mes=?", (mes,)).fetchall()}
    envios = {r["id_profesional"]: r["at"] for r in c.execute(
        "SELECT id_profesional, MAX(at) AS at FROM remun_envios WHERE mes=? GROUP BY id_profesional",
        (mes,)).fetchall()}
    lineas = _conciliar(sistema, medilink, dias_ok, modulo, mes_ok, nombres, decisiones)
    pct_map = _pct_map(c)

    estado_pago = {r["id_profesional"]: r for r in c.execute(
        "SELECT * FROM remuneracion_estado WHERE mes=?", (mes,)).fetchall()}
    ajustes: dict = defaultdict(list)
    for r in c.execute("SELECT id, id_profesional, monto, motivo FROM remun_ajustes WHERE mes=? ORDER BY id",
                       (mes,)).fetchall():
        ajustes[r["id_profesional"]].append({"id": r["id"], "monto": int(r["monto"]), "motivo": r["motivo"]})

    por_prof: dict = defaultdict(list)
    for x in lineas:
        por_prof[x["id_profesional"]].append(x)
    s_prof: dict = defaultdict(lambda: [0, 0])
    for p in sistema:
        s_prof[p["id_profesional"]][0] += p["monto"]
        s_prof[p["id_profesional"]][1] += 1
    l_prof: dict = defaultdict(lambda: [0, 0])
    for p in medilink:
        l_prof[p["id_profesional"]][0] += p["monto"]
        l_prof[p["id_profesional"]][1] += 1
    m_prof: dict = defaultdict(lambda: {"copago": 0, "bonif_est": 0, "esperado": 0, "n": 0})
    for r in modulo:
        m = m_prof[r["id_profesional"]]
        m["copago"] += r["copago"]
        m["bonif_est"] += r["bonif_est"]
        m["esperado"] += r["esperado"]
        m["n"] += 1

    ids = {pid for pid in (set(por_prof) | set(ajustes)) if pid is not None}
    profs = []
    for pid in ids:
        ls = por_prof.get(pid, [])
        base = sum(x["monto_pago"] for x in ls)
        tiene_ingreso = bool(base or s_prof[pid][0] or l_prof[pid][0])
        liq = _liquidar(pid, mes, base, pct_map)
        if not tiene_ingreso and not liq["fijo"] and not ajustes.get(pid):
            # Solo alertas (p. ej. recepción cargó algo a alguien sin caja): no se
            # liquida, pero sus líneas siguen visibles en el detalle.
            pass
        aj = ajustes.get(pid, [])
        tot_aj = sum(a["monto"] for a in aj)
        por_tipo: dict = {}
        for x in ls:
            if x["tipo"] in ("ok", "ok_patron"):
                continue
            t = por_tipo.setdefault(x["tipo"], {"n": 0, "monto": 0})
            t["n"] += 1
            t["monto"] += x["diferencia"]
        n_dif = sum(1 for x in ls if x["tipo"] in _TIPOS_BADGE)
        n_alertas = sum(1 for x in ls if x["tipo"] == "dif_modulo")
        n_patron = sum(1 for x in ls if x["tipo"] == "ok_patron")
        monto_dif = sum(x["efecto_pago"] for x in ls)
        info = nombres.get(pid, {})
        ep = estado_pago.get(pid)
        pagado = bool(ep and ep["pagado"])
        monto_pagado = None
        if ep is not None and "monto_pagado" in ep.keys():
            monto_pagado = ep["monto_pagado"]
        verif_any = any(x["dia_verificado"] for x in ls) or l_prof[pid][1] > 0
        profs.append({
            "id": pid, "nombre": _nombre_prof(nombres, pid), "especialidad": info.get("especialidad", ""),
            "pct": liq["pct"], "fijo": liq["fijo"], "monto_fijo": liq["monto_fijo"],
            "sin_retencion": liq["sin_retencion"], "telefono": info.get("telefono", ""),
            "pct_actualizado_at": info.get("pct_actualizado_at"),
            "whatsapp_enviado_at": envios.get(pid),
            "pagado": pagado, "pagado_at": ep["pagado_at"] if ep else None,
            "monto_pagado": monto_pagado,
            "fuentes": {
                "sistema": {"monto": s_prof[pid][0], "n": s_prof[pid][1]},
                "medilink": ({"monto": l_prof[pid][0], "n": l_prof[pid][1]} if dias_ok else None),
                "modulo": dict(m_prof[pid]) if pid in m_prof else
                {"copago": 0, "bonif_est": 0, "esperado": 0, "n": 0},
            },
            "conciliacion": {
                "estado": ("sin_verificar" if not mes_ok else ("diferencias" if n_dif else "ok")),
                "n_ok": len(ls) - n_dif - n_alertas, "n_dif": n_dif, "n_alertas": n_alertas,
                "n_ok_patron": n_patron, "monto_dif_pago": int(monto_dif),
                "por_tipo": por_tipo, "verificado_parcial": bool(verif_any and not mes_ok),
            },
            "liquidacion": {
                "base": int(base), "base_fuente": "medilink" if mes_ok else "sistema",
                "pct": liq["pct"], "bruto": liq["bruto"], "retencion": liq["retencion"],
                "liquido": liq["liquido"], "ajustes": aj, "total_ajustes": tot_aj,
                "a_transferir": liq["liquido"] + tot_aj,
            },
            "_lineas": ls,
            "_liquidar": tiene_ingreso or liq["fijo"] or bool(aj),
        })
    profs = [p for p in profs if p["_liquidar"]]
    profs.sort(key=lambda p: (p["pagado"], -p["liquidacion"]["base"]))
    return {"verif": verif, "profesionales": profs, "lineas": lineas, "sistema": sistema,
            "medilink": medilink, "modulo": modulo, "dias_ok": dias_ok, "mes_ok": mes_ok}


def _resumen_mes(c, mes: str) -> dict:
    calc = _calcular_mes(c, mes)
    profs = calc["profesionales"]
    pagado = sum((p["monto_pagado"] if p["monto_pagado"] is not None else p["liquidacion"]["a_transferir"])
                 for p in profs if p["pagado"])
    a_transferir = sum(p["liquidacion"]["a_transferir"] for p in profs)
    sin_prof = [x for x in calc["lineas"] if x["id_profesional"] is None]
    v = calc["verif"]
    totales = {
        "sistema": sum(p["monto"] for p in calc["sistema"]),
        "medilink": (sum(p["monto"] for p in calc["medilink"]) if calc["dias_ok"] else None),
        "modulo_esperado": sum(r["esperado"] for r in calc["modulo"]),
        "modulo_copago": sum(r["copago"] for r in calc["modulo"]),
        "modulo_bonif_est": sum(r["bonif_est"] for r in calc["modulo"]),
        "base_pago": sum(p["liquidacion"]["base"] for p in profs),
        "bruto": sum(p["liquidacion"]["bruto"] for p in profs),
        "liquido": sum(p["liquidacion"]["liquido"] for p in profs),
        "total_ajustes": sum(p["liquidacion"]["total_ajustes"] for p in profs),
        "a_transferir": a_transferir,
        "pagado": pagado,
        "por_pagar": sum(p["liquidacion"]["a_transferir"] for p in profs if not p["pagado"]),
        "n_dif": sum(p["conciliacion"]["n_dif"] for p in profs),
        "n_alertas": sum(p["conciliacion"]["n_alertas"] for p in profs),
        "n_ok_patron": sum(p["conciliacion"]["n_ok_patron"] for p in profs),
        "monto_dif_pago": sum(p["conciliacion"]["monto_dif_pago"] for p in profs),
        "sin_profesional": {"n": len(sin_prof), "monto": sum(_monto_linea(x) for x in sin_prof)},
        "n_pagados": sum(1 for p in profs if p["pagado"]), "n_profesionales": len(profs),
    }
    return {"mes": mes,
            "verificado_medilink": {"estado": v["estado"], "at": v["at"], "dias_ok": v["dias_ok"],
                                    "dias_total": v["dias_total"], "en_curso": v["en_curso"],
                                    "dias_error": v["dias_error"]},
            "totales": totales,
            "patrones": _patrones(calc["lineas"]),
            "profesionales": [{k: v for k, v in p.items() if not k.startswith("_")} for p in profs]}


# ═════════════════════════════ WhatsApp ═════════════════════════════════════

def _saludo(nombre: str) -> str:
    n = (nombre or "").strip()
    if n.lower().startswith("dra."):
        return f"Estimada {n}:"
    if n.lower().startswith("dr."):
        return f"Estimado {n}:"
    return f"Hola, {n}:"


WA_MAX = 1800   # sobre esto el front lo manda en dos mensajes (resumen + detalle)


def _texto_whatsapp(p: dict, lineas: list[dict], mes: str, con_detalle: bool = True) -> str:
    """Texto ÚNICO del mensaje (el front solo lo muestra). Ley 21.719: de cada
    atención va solo fecha, nombre del paciente y monto — nunca diagnóstico ni
    especialidad; el destinatario es el profesional que lo atendió."""
    liq = p["liquidacion"]
    L = [_saludo(p["nombre"]), "",
         f"Junto con saludar, le enviamos el detalle de su liquidación de honorarios de {_mes_largo(mes)}.", "",
         "*Resumen*"]
    if p["fijo"]:
        L.append(f"• Ingresos atribuidos del mes: {_clp(liq['base'])}")
        L.append(f"• Honorario fijo de contrato: {_clp(liq['bruto'])}")
    else:
        L.append(f"• Ingresos del mes: {_clp(liq['base'])}")
        L.append(f"• Su porcentaje: {liq['pct']}% → honorario {_clp(liq['bruto'])}")
    if p["sin_retencion"]:
        L.append(f"• Factura a emitir: {_clp(liq['bruto'])} (sin retención)")
    else:
        L.append(f"• Boleta de honorarios a emitir: {_clp(liq['bruto'])}")
        L.append(f"• Retención 15,25%: {_clp(-liq['retencion'])}")
        L.append(f"• Líquido: {_clp(liq['liquido'])}")
    if liq["ajustes"]:
        L.append("• Ajustes:")
        for a in liq["ajustes"]:
            signo = "+" if a["monto"] >= 0 else ""
            L.append(f"   {signo}{_clp(a['monto'])} — {a['motivo']}")
    L.append(f"• *Total a transferir: {_clp(liq['a_transferir'])}*")
    pagables = [x for x in lineas if x["cuenta_para_pago"]]
    if con_detalle and pagables:
        L += ["", f"*Detalle de atenciones ({len(pagables)})*"]
        for x in sorted(pagables, key=lambda x: (x["fecha"], x["paciente"])):
            L.append(f"{_ddmm(x['fecha'])} · {x['paciente']} · {_clp(x['monto_pago'])}")
    elif pagables:
        L += ["", f"El detalle de las {len(pagables)} atenciones del mes va en el mensaje siguiente."]
    L += ["", "Ante cualquier duda, quedamos atentos.", "Saludos cordiales,", "Centro Médico Carampangue"]
    return "\n".join(L)


def _texto_detalle_whatsapp(lineas: list[dict], mes: str) -> str:
    """Segundo mensaje: solo la lista de atenciones (fecha · paciente · monto)."""
    pagables = sorted((x for x in lineas if x["cuenta_para_pago"]), key=lambda x: (x["fecha"], x["paciente"]))
    L = [f"*Detalle de atenciones de {_mes_largo(mes)} ({len(pagables)})*"]
    L += [f"{_ddmm(x['fecha'])} · {x['paciente']} · {_clp(x['monto_pago'])}" for x in pagables]
    L += ["", "Centro Médico Carampangue"]
    return "\n".join(L)


# ═════════════════════════════ Endpoints ════════════════════════════════════

def _prof_del_mes(c, mes: str, prof: int) -> tuple[dict, dict]:
    calc = _calcular_mes(c, mes)
    p = next((x for x in calc["profesionales"] if x["id"] == prof), None)
    return calc, p


@router.get("/mes")
def api_mes(request: Request, mes: str | None = Query(None), token: str | None = Query(None)):
    _auth(request, token)
    mes = _validar_mes(mes)
    from session import db
    from remuneraciones_routes import _backfill_telefonos
    with db() as c:
        _ensure_tablas(c)
        try:
            _backfill_telefonos(c)
        except Exception:  # noqa: BLE001
            pass
        return _resumen_mes(c, mes)


@router.get("/detalle")
def api_detalle(request: Request, prof: int = Query(...), mes: str | None = Query(None),
                token: str | None = Query(None)):
    _auth(request, token)
    mes = _validar_mes(mes)
    from session import db
    with db() as c:
        calc = _calcular_mes(c, mes)
    ls = [x for x in calc["lineas"] if x["id_profesional"] == prof]
    p = next((x for x in calc["profesionales"] if x["id"] == prof), None)
    return {"prof": prof, "mes": mes, "nombre": p["nombre"] if p else None,
            "verificado_medilink": calc["verif"]["estado"],
            "lineas": ls, "patrones": _patrones(ls)}


@router.post("/verificar-medilink")
async def api_verificar(request: Request, body: dict = Body(...), token: str | None = Query(None)):
    _auth(request, token)
    mes = _validar_mes((body or {}).get("mes"))
    if mes > datetime.now(_CL).strftime("%Y-%m"):
        raise HTTPException(400, "mes futuro")
    if not await iniciar_barrido(mes, bool((body or {}).get("solo_pendientes"))):
        raise HTTPException(409, f"Ya hay una verificación en curso ({_TAREA['mes']}). Espere a que termine.")
    from session import db
    with db() as c:
        _ensure_tablas(c)
        return {"ok": True, **_estado_verificacion(c, mes)}


@router.get("/verificar-estado")
def api_verificar_estado(request: Request, mes: str | None = Query(None), token: str | None = Query(None)):
    _auth(request, token)
    mes = _validar_mes(mes)
    from session import db
    with db() as c:
        _ensure_tablas(c)
        return _estado_verificacion(c, mes)


@router.post("/ajuste")
def api_ajuste(request: Request, body: dict = Body(...), token: str | None = Query(None)):
    _auth(request, token)
    try:
        prof = int(body.get("prof"))
        monto = int(round(float(body.get("monto"))))
    except (TypeError, ValueError):
        raise HTTPException(400, "prof y monto son obligatorios y numéricos")
    mes = _validar_mes(body.get("mes"))
    motivo = (body.get("motivo") or "").strip()[:200]
    if not motivo:
        raise HTTPException(400, "El motivo es obligatorio")
    if monto == 0:
        raise HTTPException(400, "El monto no puede ser 0")
    from session import db
    with db() as c:
        _ensure_tablas(c)
        cur = c.execute("INSERT INTO remun_ajustes (id_profesional, mes, monto, motivo) VALUES (?,?,?,?)",
                        (prof, mes, monto, motivo))
        return {"ok": True, "id": cur.lastrowid}


@router.delete("/ajuste/{aid}")
def api_ajuste_borrar(aid: int, request: Request, token: str | None = Query(None)):
    _auth(request, token)
    from session import db
    with db() as c:
        _ensure_tablas(c)
        n = c.execute("DELETE FROM remun_ajustes WHERE id=?", (aid,)).rowcount
    if not n:
        raise HTTPException(404, "ajuste no existe")
    return {"ok": True}


@router.post("/pagado")
def api_pagado(request: Request, body: dict = Body(...), token: str | None = Query(None)):
    _auth(request, token)
    try:
        prof = int(body.get("prof"))
    except (TypeError, ValueError):
        raise HTTPException(400, "falta prof")
    mes = _validar_mes(body.get("mes"))
    pagado = 1 if body.get("pagado") else 0
    monto = body.get("monto_pagado")
    try:
        monto = int(round(float(monto))) if (pagado and monto not in (None, "")) else None
    except (TypeError, ValueError):
        raise HTTPException(400, "monto_pagado inválido")
    from session import db
    with db() as c:
        _ensure_tablas(c)
        c.execute("""INSERT INTO remuneracion_estado (id_profesional, mes, pagado, pagado_at, monto_pagado)
            VALUES (?,?,?,datetime('now'),?)
            ON CONFLICT(id_profesional, mes) DO UPDATE SET pagado=excluded.pagado,
              pagado_at=excluded.pagado_at, monto_pagado=excluded.monto_pagado""",
                  (prof, mes, pagado, monto))
    return {"ok": True, "pagado": bool(pagado), "monto_pagado": monto}


@router.post("/config")
def api_config(request: Request, body: dict = Body(...), token: str | None = Query(None)):
    """% de honorario y/o teléfono → equipo_cmc (igual que el N4)."""
    _auth(request, token)
    pid = body.get("id")
    if not pid:
        raise HTTPException(400, "falta id")
    sets, vals = [], []
    if body.get("pct") is not None:
        try:
            pct = int(body["pct"])
        except (TypeError, ValueError):
            raise HTTPException(400, "pct inválido")
        if not 0 <= pct <= 100:
            raise HTTPException(400, "pct debe estar entre 0 y 100")
        sets.append("pct_honorario=?")
        vals.append(pct)
    if "telefono" in body:
        sets.append("telefono=?")
        vals.append(re.sub(r"\D", "", str(body["telefono"] or "")))
    if not sets:
        return {"ok": True}
    from session import db
    with db() as c:
        n = c.execute(f"UPDATE equipo_cmc SET {', '.join(sets)}, updated_at=datetime('now') "
                      "WHERE id_medilink=?", (*vals, pid)).rowcount
    if not n:
        raise HTTPException(404, "profesional no está en equipo_cmc")
    return {"ok": True}


@router.get("/whatsapp")
def api_whatsapp(request: Request, prof: int = Query(...), mes: str | None = Query(None),
                 token: str | None = Query(None)):
    _auth(request, token)
    mes = _validar_mes(mes)
    from session import db
    with db() as c:
        calc, p = _prof_del_mes(c, mes, prof)
    if not p:
        raise HTTPException(404, "profesional sin liquidación este mes")
    texto = _texto_whatsapp(p, p["_lineas"], mes, True)
    return {"telefono": p["telefono"], "texto": texto,
            "texto_resumen": _texto_whatsapp(p, p["_lineas"], mes, False),
            "texto_detalle": _texto_detalle_whatsapp(p["_lineas"], mes),
            "largo": len(texto), "dos_mensajes": len(texto) > WA_MAX, "max_caracteres": WA_MAX,
            "base_fuente": p["liquidacion"]["base_fuente"],
            "whatsapp_enviado_at": p["whatsapp_enviado_at"]}


@router.post("/whatsapp-enviado")
def api_whatsapp_enviado(request: Request, body: dict = Body(...), token: str | None = Query(None)):
    """Marca que el dueño envió la liquidación (no envía nada: el envío es en wa.me)."""
    _auth(request, token)
    try:
        prof = int(body.get("prof"))
    except (TypeError, ValueError):
        raise HTTPException(400, "falta prof")
    mes = _validar_mes(body.get("mes"))
    from session import db
    with db() as c:
        _ensure_tablas(c)
        cur = c.execute("INSERT INTO remun_envios (id_profesional, mes) VALUES (?,?)", (prof, mes))
        at = c.execute("SELECT at FROM remun_envios WHERE id=?", (cur.lastrowid,)).fetchone()["at"]
    return {"ok": True, "whatsapp_enviado_at": at}


@router.post("/linea-decision")
def api_linea_decision(request: Request, body: dict = Body(...), token: str | None = Query(None)):
    """Incluye/excluye del pago una línea solo_en_modulo. `incluir: null` borra
    la decisión y vuelve a la regla default."""
    _auth(request, token)
    try:
        prof = int(body.get("prof"))
    except (TypeError, ValueError):
        raise HTTPException(400, "falta prof")
    mes = _validar_mes(body.get("mes"))
    clave = str(body.get("clave_linea") or "")
    if not clave or "incluir" not in body:
        raise HTTPException(400, "faltan clave_linea / incluir")
    incluir = body.get("incluir")
    from session import db
    with db() as c:
        calc = _calcular_mes(c, mes)
        ln = next((x for x in calc["lineas"] if x["id_profesional"] == prof and x["clave_linea"] == clave), None)
        if ln is None:
            raise HTTPException(404, "línea no existe en este mes")
        if ln["tipo"] != "solo_en_modulo":
            raise HTTPException(400, "solo se decide sobre líneas 'solo_en_modulo'")
        if incluir is None:
            c.execute("DELETE FROM remun_decisiones WHERE mes=? AND id_profesional=? AND clave_linea=?",
                      (mes, prof, clave))
        else:
            c.execute("""INSERT INTO remun_decisiones (mes, id_profesional, clave_linea, incluir, at)
                VALUES (?,?,?,?,datetime('now')) ON CONFLICT(mes, id_profesional, clave_linea)
                DO UPDATE SET incluir=excluded.incluir, at=excluded.at""", (mes, prof, clave, 1 if incluir else 0))
    return {"ok": True, "clave_linea": clave, "decision": None if incluir is None else bool(incluir)}


def register_remuneraciones_pro_routes(app) -> None:
    """Registra API + página. Llamar desde main.py."""
    app.include_router(router)

    @app.get("/alma/remuneraciones", response_class=HTMLResponse, include_in_schema=False)
    def alma_remuneraciones_page(request: Request, token: str | None = Query(None)):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host.endswith("centromedicocarampangue.cl"):
            raise HTTPException(404, "Not found")
        tk = _auth(request, token)
        if not _TEMPLATE.exists():
            raise HTTPException(404, "Remuneraciones no disponible")
        html = _TEMPLATE.read_text(encoding="utf-8").replace("__TOKEN__", tk)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})
