"""
Wrapper para la API de Medilink 2 (healthatom).
Base URL: https://api.medilink2.healthatom.com/api/v5
"""
import asyncio
import json
import re
import logging
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

_CHILE_TZ = ZoneInfo("America/Santiago")

import httpx

from config import MEDILINK_BASE_URL, MEDILINK_TOKEN, MEDILINK_SUCURSAL

log = logging.getLogger("medilink")


def _safe_json(r, default=None):
    """Parse response JSON tolerando no-JSON (HTML de error, body vacío).
    Retorna default o {} si falla, loggeando status y snippet del body."""
    try:
        return r.json()
    except (ValueError, json.JSONDecodeError):
        body = ""
        try:
            body = r.text[:200]
        except Exception:
            pass
        log.warning(f"medilink non-JSON response status={r.status_code} body={body!r}")
        return default if default is not None else {}


HEADERS = {"Authorization": f"Token {MEDILINK_TOKEN}"}

# Caché de horarios por profesional: id_prof → {intervalo, dias (set de weekdays Python), _ts}
_horarios_cache: dict = {}
_HORARIO_CACHE_TTL = 3600  # 1 hora — si cambian horarios en Medilink, se refrescan automáticamente

# Semáforo global para limitar requests concurrentes a Medilink.
# Medilink devuelve 429 con fan-out (ej: panel admin carga citas de 20 profesionales en paralelo).
# Con N=4 las requests se serializan lo suficiente para no saturar y el circuit breaker
# no oscila. Si se necesita más throughput, subir con cuidado probando rate limit real.
_MEDILINK_SEM = asyncio.Semaphore(4)

# Caché de pacientes por RUT — ~200 pacientes activos, datos casi inmutables.
# Paciente se re-consulta 2-5 veces por flujo de agendar/cancelar/reagendar.
_paciente_cache: dict = {}
_paciente_id_cache: dict = {}
_PAC_ID_TTL = 600  # 10 min
_PACIENTE_CACHE_TTL = 600  # 10 min

# Caché de primera fecha disponible por especialidad.
# `/especialidades/{id}/proxima` se consulta en cada intento de agendar; los
# slots reales cambian cada pocos minutos pero el "próximo día disponible"
# cambia con menos frecuencia. TTL corto evita info stale.
_proxima_cache: dict = {}
_PROXIMA_CACHE_TTL = 900  # 15 min — aguanta picos de carga sin saturar Medilink

# Contador simple de 429s (para diagnóstico rápido)
_STATS_429 = {"total": 0, "last_ts": 0.0, "last_url": ""}


def record_429(url: str) -> None:
    import time as _t
    _STATS_429["total"] += 1
    _STATS_429["last_ts"] = _t.time()
    _STATS_429["last_url"] = url[:200]
    log.error("MEDILINK_429 %s total=%d", url, _STATS_429["total"])


def get_stats_429() -> dict:
    return dict(_STATS_429)

# Medilink usa dia 1=Lun..6=Sáb, 7=Dom → Python weekday 0=Lun..5=Sáb, 6=Dom
_MEDILINK_DIA_TO_WEEKDAY = {1:0, 2:1, 3:2, 4:3, 5:4, 6:5, 7:6}

# Profesionales habilitados en el CMC (id → info)
PROFESIONALES = {
     1: {"nombre": "Dr. Rodrigo Olavarría",    "especialidad": "Medicina General",      "intervalo": 15},
    73: {"nombre": "Dr. Andrés Abarca",        "especialidad": "Medicina General",      "intervalo": 15},
    13: {"nombre": "Dr. Alonso Márquez",       "especialidad": "Medicina General",      "intervalo": 20},
    23: {"nombre": "Dr. Manuel Borrego",       "especialidad": "Otorrinolaringología",  "intervalo": 20},
    60: {"nombre": "Dr. Miguel Millán",        "especialidad": "Cardiología",           "intervalo": 20, "dias": [5]},
    # 64: Dr. Claudio Barraza — Traumatología — temporalmente deshabilitado
    61: {"nombre": "Dr. Tirso Rejón",          "especialidad": "Ginecología",           "intervalo": 20},
    65: {"nombre": "Dr. Nicolás Quijano",      "especialidad": "Gastroenterología",     "intervalo": 20},
    55: {"nombre": "Dra. Javiera Burgos",      "especialidad": "Odontología General",   "intervalo": 60},
    72: {"nombre": "Dr. Carlos Jiménez",       "especialidad": "Odontología General",   "intervalo": 30},
    66: {"nombre": "Dra. Daniela Castillo",    "especialidad": "Ortodoncia",            "intervalo": 30},
    75: {"nombre": "Dr. Fernando Fredes",      "especialidad": "Endodoncia",            "intervalo": 30},
    69: {"nombre": "Dra. Aurora Valdés",       "especialidad": "Implantología",         "intervalo": 30},
    76: {"nombre": "Dra. Valentina Fuentealba","especialidad": "Estética Facial",       "intervalo": 30},
    59: {"nombre": "Paola Acosta",             "especialidad": "Masoterapia",           "intervalo": 20},
    77: {"nombre": "Luis Armijo",              "especialidad": "Kinesiología",          "intervalo": 40},
    21: {"nombre": "Leonardo Etcheverry",      "especialidad": "Kinesiología",          "intervalo": 40},
    52: {"nombre": "Gisela Pinto",             "especialidad": "Nutrición",             "intervalo": 60},
    74: {"nombre": "Jorge Montalba",           "especialidad": "Psicología Adulto",     "intervalo": 45},
    49: {"nombre": "Juan Pablo Rodríguez",     "especialidad": "Psicología Adulto",     "intervalo": 45},
    70: {"nombre": "Juana Arratia",            "especialidad": "Fonoaudiología",        "intervalo": 30},
    67: {"nombre": "Sarai Gómez",              "especialidad": "Matrona",               "intervalo": 30},
    56: {"nombre": "Andrea Guevara",           "especialidad": "Podología",             "intervalo": 60},
    68: {"nombre": "David Pardo",              "especialidad": "Ecografía",             "intervalo": 15},
}

# Mapa de palabras clave → IDs de profesionales
ESPECIALIDADES_MAP = {
    "kinesiología": [77, 21], "kinesiólogo": [77, 21], "kinesiologa": [77, 21], "kine": [77, 21],
    "masoterapia": [59], "masaje": [59], "masajes": [59], "masoterapeuta": [59],
    "medicina general": [73, 1, 13], "medico general": [73, 1, 13],
    "medicina familiar": [13], "médico familiar": [13],
    "otorrinolaringología": [23], "otorrino": [23], "orl": [23],
    # ── Keys únicas por profesional (usadas cuando el paciente pide a uno específico) ──
    # Bypass de _ESP_MED_GENERAL: "marquez" NO resuelve a "medicina familiar"
    # para que _iniciar_agendar NO active la lógica stage Abarca/Olavarría.
    "olavarría": [1], "olavarria": [1],
    "abarca": [73],
    "marquez": [13], "márquez": [13], "alonso márquez": [13], "dr marquez": [13], "dr márquez": [13],
    "etcheverry": [21], "leo": [21], "leonardo": [21],
    "armijo": [77], "luis armijo": [77],
    "paola acosta": [59], "paola": [59],
    "burgos": [55], "javiera burgos": [55], "dra burgos": [55],
    "jimenez": [72], "jiménez": [72], "carlos jimenez": [72], "dr jimenez": [72],
    "montalba": [74], "jorge montalba": [74],
    "rodriguez": [49], "rodríguez": [49], "juan pablo": [49], "juan pablo rodriguez": [49],
    # ── Especialidades genéricas (cuando el paciente no nombra a nadie) ──
    "odontología": [72, 55], "dentista": [72, 55], "odontólogo": [72, 55],
    "endodoncia": [75], "endodoncista": [75],
    "estética facial": [76], "estetica facial": [76], "estética": [76],
    "fonoaudiología": [70], "fonoaudiólogo": [70], "fonoaudiologa": [70],
    "implantología": [69], "implantes": [69],
    "matrona": [67], "ginecología": [61], "ginecólogo": [61],
    # traumatología deshabilitada → redirigir a medicina general
    "traumatología": [73, 1], "traumatólogo": [73, 1],
    "cardiología": [60], "cardiólogo": [60],
    "gastroenterología": [65], "gastroenterólogo": [65],
    "psicología adulto": [74, 49], "psicólogo adulto": [74, 49],
    "psicología infantil": [74], "psicólogo infantil": [74],
    "psicología": [74, 49], "psicólogo": [74, 49], "psicóloga": [74, 49],
    "nutrición": [52], "nutricionista": [52],
    "podología": [56], "podólogo": [56],
    "ortodoncia": [66], "ortodoncista": [66],
    "ecografía": [68], "ecografista": [68], "tecnólogo": [68],
}

# Mapa de palabras clave → ID de especialidad Medilink (para /especialidades/{id}/proxima)
ESPECIALIDADES_ID = {
    "kinesiología": 3, "kinesiólogo": 3, "kinesiologa": 3, "kine": 3,
    "medicina general": 10, "medico general": 10,
    "medicina familiar": 10, "médico familiar": 10,
    "odontología": 9, "dentista": 9, "odontólogo": 9,
    "fonoaudiología": 8, "fonoaudiólogo": 8, "fonoaudiologa": 8,
    "implantología": 20, "implantes": 20,
    "matrona": 11,
    "ginecología": 14, "ginecólogo": 14,
    "traumatología": 10, "traumatólogo": 10,  # redirigido a medicina general (ID 10)
    "cardiología": 16, "cardiólogo": 16,
    "gastroenterología": 18, "gastroenterólogo": 18,
    "psicología": 5, "psicólogo": 5, "psicóloga": 5,
    "psicología adulto": 5, "psicología infantil": 5,
    "nutrición": 4, "nutricionista": 4,
    "podología": 12, "podólogo": 12,
    "ortodoncia": 19, "ortodoncista": 19,
    "ecografía": 13, "ecografista": 13, "tecnólogo": 13,
    "otorrinolaringología": 6, "otorrino": 6,
    # ── Keys únicas por profesional (mismo id de especialidad base en Medilink) ──
    "olavarría": 10, "olavarria": 10, "abarca": 10,
    "marquez": 10, "márquez": 10, "alonso márquez": 10, "dr marquez": 10, "dr márquez": 10,
    "etcheverry": 3, "leo": 3, "leonardo": 3,
    "armijo": 3, "luis armijo": 3,
    "paola acosta": 3, "paola": 3,
    "burgos": 9, "javiera burgos": 9, "dra burgos": 9,
    "jimenez": 9, "jiménez": 9, "carlos jimenez": 9, "dr jimenez": 9,
    "montalba": 5, "jorge montalba": 5,
    "rodriguez": 5, "rodríguez": 5, "juan pablo": 5, "juan pablo rodriguez": 5,
}


def _q(params: dict) -> str:
    # Medilink rechaza JSON con espacios — separadores compactos.
    # NO URL-encodear acá: httpx lo hace vía params={}. Doble encoding rompe la query.
    return json.dumps(params, separators=(",", ":"))


# ── Reporte de estado a resilience.py ────────────────────────────────────────
# Cache in-process para evitar escribir en system_state en cada request.
# Source of truth sigue siendo session_state (para que cron de recuperación
# y /health vean lo mismo entre procesos), pero solo escribimos en cambios.
_last_reported_status: Optional[str] = None  # None | "up" | "down"


def _report_up() -> None:
    global _last_reported_status
    if _last_reported_status == "up":
        return
    try:
        from resilience import mark_medilink_up
        mark_medilink_up()
    except Exception as e:  # pragma: no cover — nunca debe romper un call
        log.error("resilience.mark_medilink_up falló: %s", e)
    _last_reported_status = "up"


def _report_down(reason: str) -> None:
    global _last_reported_status
    if _last_reported_status == "down":
        return
    try:
        from resilience import mark_medilink_down
        mark_medilink_down(reason)
    except Exception as e:  # pragma: no cover
        log.error("resilience.mark_medilink_down falló: %s", e)
    _last_reported_status = "down"


async def _get(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """GET con 2 reintentos ante errores de red, 5xx o 429 (rate limit).

    Serializado por _MEDILINK_SEM para evitar saturar Medilink con fan-out
    concurrente (Medilink rate-limita agresivamente cuando llegan >5 requests
    simultáneas, lo que tumbaba el circuit breaker).
    """
    async with _MEDILINK_SEM:
        for attempt in range(3):
            try:
                r = await client.get(url, **kwargs)
                if r.status_code == 429:
                    record_429(url)
                    wait = 3.0 * (2 ** attempt)  # 3s, 6s, 12s
                    log.warning("Medilink GET %s → 429 rate limit, esperando %.0fs (intento %d/3)", url, wait, attempt + 1)
                    await asyncio.sleep(wait)
                    continue
                if r.status_code < 500:
                    _report_up()
                    return r
                log.warning("Medilink GET %s → %s (intento %d/3)", url, r.status_code, attempt + 1)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                log.warning("Medilink GET %s error red: %s (intento %d/3)", url, e, attempt + 1)
            if attempt < 2:
                await asyncio.sleep(1.5 ** attempt)
        _report_down(f"GET {url} sin respuesta tras 3 intentos")
        raise httpx.RequestError(f"Medilink no respondió tras 3 intentos: {url}")


async def _post(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """POST con 1 reintento ante errores de red, 5xx o 429 (rate limit).
    Serializado por _MEDILINK_SEM (ver _get)."""
    async with _MEDILINK_SEM:
        for attempt in range(2):
            try:
                r = await client.post(url, **kwargs)
                if r.status_code == 429:
                    record_429(url)
                    wait = 3.0 * (2 ** attempt)  # 3s, 6s
                    log.warning("Medilink POST %s → 429 rate limit, esperando %.0fs (intento %d/2)", url, wait, attempt + 1)
                    await asyncio.sleep(wait)
                    continue
                if r.status_code < 500:
                    _report_up()
                    return r
                log.warning("Medilink POST %s → %s (intento %d/2)", url, r.status_code, attempt + 1)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                log.warning("Medilink POST %s error red: %s (intento %d/2)", url, e, attempt + 1)
            if attempt < 1:
                await asyncio.sleep(1.5)
        _report_down(f"POST {url} sin respuesta tras 2 intentos")
        raise httpx.RequestError(f"Medilink no respondió tras 2 intentos: {url}")


async def _get_horario(client: httpx.AsyncClient, id_prof: int) -> dict:
    """Obtiene intervalo, días de trabajo y horarios por día desde la API (con caché 1h).
    Retorna: {intervalo, dias: set(weekdays), horario_dia: {weekday: (hi, hf, break_t)}}
    break_t es None o (break_hi, break_hf) para excluir slots de pausa/almuerzo.

    Solo cachea resultados con datos válidos. Si Medilink falla (429/5xx/red),
    retorna el dict stale del caché si existe, o un fallback SIN cachearlo —
    así el próximo call reintenta en vez de servir vacío por 1 hora.
    """
    cached = _horarios_cache.get(id_prof)
    if cached and (time.monotonic() - cached.get("_ts", 0)) < _HORARIO_CACHE_TTL:
        return cached

    try:
        r = await _get(client, f"{MEDILINK_BASE_URL}/profesionales/{id_prof}/horarios", headers=HEADERS)
    except httpx.RequestError as e:
        log.error("No se pudo obtener horario del profesional %d: %s", id_prof, e)
        # Devolver stale si existe (aunque haya expirado) en vez de vacío
        if cached and cached.get("horario_dia"):
            log.warning("Usando horario stale para prof %d (fetch falló)", id_prof)
            return cached
        return {"intervalo": PROFESIONALES[id_prof]["intervalo"], "dias": set(range(5)), "horario_dia": {}}

    horario = {"intervalo": PROFESIONALES[id_prof]["intervalo"], "dias": set(range(6)), "horario_dia": {}}
    if r.status_code == 200:
        data = _safe_json(r).get("data", [])
        sucursal_data = next((x for x in data if x.get("id_sucursal") == int(MEDILINK_SUCURSAL)), None)
        if sucursal_data:
            dias_activos = set()
            horario_dia = {}
            for d in sucursal_data.get("dias", []):
                hi = d.get("hora_inicio", "")
                hf = d.get("hora_fin", "")
                # Break/almuerzo: si hora_inicio_break != hora_fin_break, se debe excluir.
                # Muchos profesionales tienen break 13:00-14:00. Ignorarlo causaba que
                # el bot ofreciera slots dentro del break y Medilink rechazara el crear_cita
                # con "Profesional no tiene horario para la fecha y duración solicitadas".
                bhi = d.get("hora_inicio_break", "") or ""
                bhf = d.get("hora_fin_break", "") or ""
                if hi and hf and hi != hf:
                    wd = _MEDILINK_DIA_TO_WEEKDAY.get(d["dia"])
                    if wd is not None:
                        dias_activos.add(wd)
                        break_t = None
                        if bhi and bhf and bhi != bhf:
                            break_t = (bhi[:5], bhf[:5])
                        horario_dia[wd] = (hi[:5], hf[:5], break_t)
            horario = {
                "intervalo":   PROFESIONALES[id_prof]["intervalo"],
                "dias":        dias_activos if dias_activos else set(range(5)),
                "horario_dia": horario_dia,
            }

    # Solo cachear si obtuvimos horario_dia real. Sin esto, un 429 que deja
    # horario_dia={} se cacheaba 1h y tumbaba a ese profesional para todos.
    if horario.get("horario_dia"):
        horario["_ts"] = time.monotonic()
        _horarios_cache[id_prof] = horario
    else:
        log.debug("Horario vacío para prof %d (status %s) — no se cachea", id_prof, r.status_code)
        if cached and cached.get("horario_dia"):
            return cached  # fallback a stale si existe
    return horario


async def _get_bloqueos(client: httpx.AsyncClient, id_prof: int, fecha: str) -> list:
    """Retorna lista de rangos bloqueados (hora_inicio, hora_fin) para ese profesional y fecha.
    La API solo filtra por id_sucursal y fecha — filtramos id_profesional en código.
    Incluye bloqueos sin id_profesional (aplican a toda la sucursal).
    """
    params = {
        "id_sucursal": {"eq": MEDILINK_SUCURSAL},
        "fecha":       {"eq": fecha},
    }
    try:
        r = await _get(client, f"{MEDILINK_BASE_URL}/horariosbloqueados",
                       params={"q": _q(params)}, headers=HEADERS)
    except httpx.RequestError as e:
        log.error("No se pudo obtener bloqueos para prof %d fecha %s: %s", id_prof, fecha, e)
        return []
    if r.status_code != 200:
        return []
    bloqueos = []
    for b in _safe_json(r).get("data", []):
        b_prof = b.get("id_profesional")
        # Incluir bloqueos del profesional O sin profesional (bloqueo de sucursal)
        if b_prof == id_prof or b_prof is None or b_prof == 0:
            bloqueos.append((b["hora_inicio"][:5], b["hora_fin"][:5]))
    if bloqueos:
        log.debug("Bloqueos prof %d fecha %s: %s", id_prof, fecha, bloqueos)
    return bloqueos


def _slot_bloqueado(hora_inicio: str, hora_fin: str, bloqueos: list) -> bool:
    """Retorna True si el slot se solapa con algún bloqueo."""
    for b_ini, b_fin in bloqueos:
        if hora_inicio[:5] < b_fin and hora_fin[:5] > b_ini:
            return True
    return False


def _h_to_min(h: str) -> int:
    """'14:15' → 855"""
    hh, mm = h[:5].split(":")
    return int(hh) * 60 + int(mm)


def smart_select(slots_libres: list, horas_ocupadas: set, intervalo: int, n: int = 5) -> list:
    """
    Elige los n mejores slots para compactar la agenda del profesional.
    Prioriza slots adyacentes a citas ya tomadas; si no hay citas, los primeros del día.
    """
    if not horas_ocupadas:
        return slots_libres[:n]

    def score(slot):
        m = _h_to_min(slot["hora_inicio"])
        puntos = 0
        for oc in horas_ocupadas:
            diff = abs(m - _h_to_min(oc))
            if diff <= intervalo:          # contiguo
                puntos += 10
            elif diff <= intervalo * 3:    # muy cerca
                puntos += 4
            elif diff <= intervalo * 6:    # cerca
                puntos += 1
        return puntos

    return sorted(slots_libres, key=score, reverse=True)[:n]


def _ids_para_especialidad(especialidad: str) -> list:
    ids = ESPECIALIDADES_MAP.get(especialidad.lower(), [])
    if not ids:
        for key, prof_ids in ESPECIALIDADES_MAP.items():
            if especialidad.lower() in key or key in especialidad.lower():
                return prof_ids
    return ids


async def _get_horas_ocupadas(client: httpx.AsyncClient, id_prof: int, fecha: str) -> set:
    """Retorna set de hora_inicio ocupadas según /citas (fuente de verdad real).
    Expande citas largas: una cita 11:00-12:00 bloquea 11:00, 11:15, 11:30, 11:45.
    """
    params = {
        "id_sucursal":      {"eq": MEDILINK_SUCURSAL},
        "id_profesional":   {"eq": id_prof},
        "fecha":            {"eq": fecha},
        "estado_anulacion": {"eq": 0},
    }
    try:
        r = await _get(client, f"{MEDILINK_BASE_URL}/citas",
                       params={"q": _q(params)}, headers=HEADERS)
    except httpx.RequestError as e:
        log.error("No se pudo obtener horas ocupadas prof %d fecha %s: %s", id_prof, fecha, e)
        raise  # Propagar error — no asumir que todo está libre
    if r.status_code != 200:
        raise httpx.RequestError(f"GET horas_ocupadas prof={id_prof} fecha={fecha} → {r.status_code}")
    ocupadas = set()
    for c in _safe_json(r).get("data", []):
        hi = c.get("hora_inicio", "")[:5]
        hf = c.get("hora_fin", "")[:5]
        if not hi:
            continue
        ocupadas.add(hi)
        # Expandir: una cita larga bloquea todos los bloques de 5 min internos
        if hf and hf > hi:
            cur = _h_to_min(hi) + 5
            fin = _h_to_min(hf)
            while cur < fin:
                ocupadas.add(f"{cur // 60:02d}:{cur % 60:02d}")
                cur += 5
    log.debug("Horas ocupadas prof %d fecha %s: %s", id_prof, fecha, sorted(ocupadas))
    return ocupadas


def _slot_libre_vs_ocupadas(hi: str, hf: str, ocupadas: set) -> bool:
    """True si el rango [hi, hf) no se solapa con ninguna hora en ocupadas.
    `ocupadas` es un set de strings 'HH:MM' en bloques de 5 min (ya expandido
    por _get_horas_ocupadas). Un slot de 40 min como 18:40-19:20 debe chequear
    18:40, 18:45, ..., 19:15 contra ocupadas. Si alguna está, choca.
    Fix del bug 2026-04-19: antes solo se chequeaba hi ∈ ocupadas, y el slot
    18:40-19:20 pasaba como libre aunque 19:10-19:50 ya estuviera agendado."""
    cur = _h_to_min(hi)
    fin = _h_to_min(hf)
    while cur < fin:
        if f"{cur // 60:02d}:{cur % 60:02d}" in ocupadas:
            return False
        cur += 5
    return True


def _generar_slots_horario(hora_inicio: str, hora_fin: str, intervalo: int,
                           break_t: tuple[str, str] | None = None) -> list:
    """Genera lista de (hi, hf) en 'HH:MM' desde hora_inicio hasta hora_fin,
    excluyendo los slots que se solapen total o parcialmente con el break."""
    slots = []
    cur = _h_to_min(hora_inicio)
    fin = _h_to_min(hora_fin)
    bi = bf = None
    if break_t:
        bi = _h_to_min(break_t[0])
        bf = _h_to_min(break_t[1])
    while cur + intervalo <= fin:
        hf_min = cur + intervalo
        # Solapamiento con break: el slot [cur, hf_min] choca si
        # cur < bf AND hf_min > bi (intersección no vacía).
        if bi is not None and cur < bf and hf_min > bi:
            # Saltar al final del break (alineado al siguiente múltiplo del intervalo)
            cur = bf
            continue
        hi = f"{cur // 60:02d}:{cur % 60:02d}"
        hf = f"{hf_min // 60:02d}:{hf_min % 60:02d}"
        slots.append((hi, hf))
        cur += intervalo
    return slots


async def _slots_para_fecha(client: httpx.AsyncClient, ids: list, horarios: dict,
                            fecha: str, prioridad: bool = False) -> tuple[list, list]:
    """
    Retorna (smart_5, todos_libres) para la fecha dada.
    Si prioridad=True, itera los IDs en orden y retorna al primer profesional con disponibilidad.
    """
    if prioridad:
        for id_prof in ids:
            smart, todos = await _slots_para_fecha(client, [id_prof], horarios, fecha, prioridad=False)
            if todos:
                return smart, todos
        return [], []

    fecha_dt = datetime.strptime(fecha, "%Y-%m-%d").date()
    weekday = fecha_dt.weekday()
    todos_libres = []
    horas_ocupadas = set()
    intervalo = 30  # fallback

    for id_prof in ids:
        h = horarios[id_prof]
        if weekday not in h["dias"]:
            continue
        # Obtener hora inicio/fin para este día específico
        horario_dia = h.get("horario_dia", {})
        if weekday in horario_dia:
            _tup = horario_dia[weekday]
            # Compat: entradas antiguas (hi, hf) o nuevas (hi, hf, break_t)
            hi_dia, hf_dia = _tup[0], _tup[1]
            break_t = _tup[2] if len(_tup) >= 3 else None
        else:
            # Sin info de horas específicas para este día, saltamos
            log.debug("Sin horario_dia para prof %d weekday %d", id_prof, weekday)
            continue

        intervalo       = h["intervalo"]
        bloqueos        = await _get_bloqueos(client, id_prof, fecha)
        ocupadas_citas  = await _get_horas_ocupadas(client, id_prof, fecha)
        horas_ocupadas |= ocupadas_citas
        horas_vistas    = {s["hora_inicio"] for s in todos_libres}

        log.info("Slots prof %d (%s) fecha %s: horario %s-%s, intervalo %d min, "
                 "ocupadas %d, bloqueos %d",
                 id_prof, PROFESIONALES[id_prof]["nombre"], fecha,
                 hi_dia, hf_dia, intervalo, len(ocupadas_citas), len(bloqueos))

        _ahora_cl = datetime.now(_CHILE_TZ)
        ahora_min = _h_to_min(_ahora_cl.strftime("%H:%M")) if fecha == _ahora_cl.date().strftime("%Y-%m-%d") else None
        # Buffer 60 min: el paciente no alcanza a llegar a un slot que empiece
        # en menos de 1h. También cubre conversaciones que quedan abiertas y
        # los slots se obsoletan antes de confirmar.
        BUFFER_MIN = 60
        libres_prof = 0
        for hi, hf in _generar_slots_horario(hi_dia, hf_dia, intervalo, break_t):
            if ahora_min is not None and _h_to_min(hi) <= (ahora_min + BUFFER_MIN):
                continue  # slot ya pasó o muy cerca de ahora
            # Chequear solape con TODO el rango del slot, no solo hora_inicio
            if not _slot_libre_vs_ocupadas(hi, hf, ocupadas_citas):
                horas_ocupadas.add(hi)
            elif not _slot_bloqueado(hi, hf, bloqueos) and hi not in horas_vistas:
                horas_vistas.add(hi)
                libres_prof += 1
                todos_libres.append({
                    "profesional":    PROFESIONALES[id_prof]["nombre"],
                    "especialidad":   PROFESIONALES[id_prof]["especialidad"],
                    "fecha":          fecha,
                    "fecha_display":  _fmt_fecha(fecha),
                    "hora_inicio":    hi,
                    "hora_fin":       hf,
                    "id_profesional": id_prof,
                    "id_recurso":     1,
                })
        log.info("Slots libres prof %d fecha %s: %d", id_prof, fecha, libres_prof)

    smart_5 = sorted(smart_select(todos_libres, horas_ocupadas, intervalo),
                     key=lambda x: x["hora_inicio"])
    return smart_5, todos_libres


def _id_especialidad(especialidad: str) -> Optional[int]:
    """Retorna el ID de especialidad Medilink para una palabra clave, o None."""
    key = especialidad.lower()
    if key in ESPECIALIDADES_ID:
        return ESPECIALIDADES_ID[key]
    for k, v in ESPECIALIDADES_ID.items():
        if key in k or k in key:
            return v
    return None


# Especialidades donde los profesionales se llenan en orden de prioridad (no mezclados)
_ESPECIALIDADES_PRIORIDAD = {"medicina general", "medicina familiar"}


async def buscar_primer_dia(especialidad: str, dias_adelante: int = 60,
                            excluir: list = None,
                            intervalo_override: dict = None,
                            solo_ids: list = None) -> tuple[list, list]:
    """
    Retorna (smart_5, todos_libres) del día disponible más próximo.
    Usa /especialidades/{id}/proxima para descubrir la primera fecha disponible,
    luego obtiene todos los slots reales de ese día cruzando con /citas.
    intervalo_override: {id_prof: minutos} para sobreescribir el intervalo de un profesional.
    solo_ids: si se pasa, restringe la búsqueda a esos IDs (ignora ESPECIALIDADES_MAP).
    """
    ids = solo_ids if solo_ids else _ids_para_especialidad(especialidad)
    if not ids:
        return [], []

    # Con solo_ids no usar prioridad (ya viene filtrado)
    usar_prioridad = (not solo_ids) and (especialidad.lower() in _ESPECIALIDADES_PRIORIDAD)
    excluir_set = set(excluir or [])
    id_esp = _id_especialidad(especialidad)

    async with httpx.AsyncClient(timeout=15) as client:
        # N+1 → paralelizado con asyncio.gather para reducir latencia
        import asyncio as _asyncio_horarios
        _horarios_list = await _asyncio_horarios.gather(*(_get_horario(client, i) for i in ids))
        horarios = dict(zip(ids, _horarios_list))
        if intervalo_override:
            for id_prof, mins in intervalo_override.items():
                if id_prof in horarios:
                    horarios[id_prof] = {**horarios[id_prof], "intervalo": mins}

        # Intentar con /proxima para descubrir la fecha rápidamente
        # Cacheado 3 min: reduce llamadas repetidas cuando varios pacientes
        # piden la misma especialidad en rápida sucesión.
        primera_fecha = None
        if id_esp:
            _px_cached = _proxima_cache.get(id_esp)
            if _px_cached and (time.monotonic() - _px_cached["_ts"]) < _PROXIMA_CACHE_TTL:
                primera_fecha = _px_cached.get("fecha")
                r = None
            else:
                try:
                    r = await _get(
                        client, f"{MEDILINK_BASE_URL}/especialidades/{id_esp}/proxima",
                        params={"q": _q({"id_sucursal": {"eq": int(MEDILINK_SUCURSAL)}})},
                        headers=HEADERS,
                    )
                except httpx.RequestError as e:
                    log.warning("No se pudo consultar proxima fecha especialidad %d: %s", id_esp, e)
                    r = None
            if r and r.status_code == 200:
                for slot in _safe_json(r).get("data", []):
                    # fecha viene en formato DD/MM/YYYY
                    raw = slot.get("fecha", "")
                    try:
                        fecha_dt = datetime.strptime(raw, "%d/%m/%Y").strftime("%Y-%m-%d")
                    except ValueError:
                        continue
                    if fecha_dt not in excluir_set:
                        primera_fecha = fecha_dt
                        break
                if primera_fecha:
                    _proxima_cache[id_esp] = {"fecha": primera_fecha, "_ts": time.monotonic()}

        # Siempre escanear desde hoy, usando primera_fecha como límite superior o fallback
        hoy = datetime.now(_CHILE_TZ).date()
        limite = datetime.strptime(primera_fecha, "%Y-%m-%d").date() if primera_fecha else (hoy + timedelta(days=dias_adelante))

        for delta in range(0, (limite - hoy).days + 1):
            fecha = (hoy + timedelta(days=delta)).strftime("%Y-%m-%d")
            if fecha in excluir_set:
                continue
            smart, todos = await _slots_para_fecha(client, ids, horarios, fecha, prioridad=usar_prioridad)
            if todos:
                return smart, todos

        # Si no hubo slots antes de primera_fecha, intentar exactamente esa fecha
        if primera_fecha and primera_fecha not in excluir_set:
            smart, todos = await _slots_para_fecha(client, ids, horarios, primera_fecha, prioridad=usar_prioridad)
            if todos:
                return smart, todos

        # Continuar día por día más allá
        for delta in range((limite - hoy).days + 1, dias_adelante + 1):
            fecha = (hoy + timedelta(days=delta)).strftime("%Y-%m-%d")
            if fecha in excluir_set:
                continue
            smart, todos = await _slots_para_fecha(client, ids, horarios, fecha, prioridad=usar_prioridad)
            if todos:
                return smart, todos

    return [], []


async def consultar_proxima_fecha(especialidad: str) -> str | None:
    """Retorna la fecha display del próximo día disponible para una especialidad, o None.
    Usa solo /especialidades/{id}/proxima — funciona aunque no conozcamos el prof ID."""
    id_esp = _id_especialidad(especialidad)
    if not id_esp:
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{MEDILINK_BASE_URL}/especialidades/{id_esp}/proxima",
            params={"q": _q({"id_sucursal": {"eq": int(MEDILINK_SUCURSAL)}})},
            headers=HEADERS,
        )
        if r.status_code == 200:
            for slot in _safe_json(r).get("data", []):
                raw = slot.get("fecha", "")
                try:
                    fecha_fmt = datetime.strptime(raw, "%d/%m/%Y").strftime("%Y-%m-%d")
                    return _fmt_fecha(fecha_fmt)
                except ValueError:
                    continue
    return None


async def buscar_slots_dia(especialidad: str, fecha: str,
                           intervalo_override: dict = None) -> tuple[list, list]:
    """Retorna (smart_5, todos_libres) para una fecha específica."""
    ids = _ids_para_especialidad(especialidad)
    if not ids:
        return [], []
    usar_prioridad = especialidad.lower() in _ESPECIALIDADES_PRIORIDAD
    async with httpx.AsyncClient(timeout=15) as client:
        # Paralelizar _get_horario (era N+1 secuencial)
        import asyncio as _aio_h
        _hl = await _aio_h.gather(*(_get_horario(client, i) for i in ids))
        horarios = dict(zip(ids, _hl))
        if intervalo_override:
            for id_prof, mins in intervalo_override.items():
                if id_prof in horarios:
                    horarios[id_prof] = {**horarios[id_prof], "intervalo": mins}
        return await _slots_para_fecha(client, ids, horarios, fecha, prioridad=usar_prioridad)


async def buscar_slots_dia_por_ids(ids: list, fecha: str,
                                   intervalo_override: dict = None) -> tuple[list, list]:
    """Retorna (smart_5, todos_libres) para una fecha y lista explícita de IDs de profesional."""
    if not ids:
        return [], []
    async with httpx.AsyncClient(timeout=15) as client:
        # Paralelizar _get_horario (era N+1 secuencial)
        import asyncio as _aio_h
        _hl = await _aio_h.gather(*(_get_horario(client, i) for i in ids))
        horarios = dict(zip(ids, _hl))
        if intervalo_override:
            for id_prof, mins in intervalo_override.items():
                if id_prof in horarios:
                    horarios[id_prof] = {**horarios[id_prof], "intervalo": mins}
        return await _slots_para_fecha(client, ids, horarios, fecha)


async def crear_paciente(rut: str, nombre: str, apellidos: str, **kwargs) -> Optional[dict]:
    """Crea un nuevo paciente en Medilink. Retorna dict con id, nombre, rut o None.
    kwargs opcionales: fecha_nacimiento, sexo, celular, telefono, email, comuna, direccion, ciudad.
    NOTA: Medilink POST /pacientes ignora campos opcionales, así que se hace un PUT después.
    """
    body: dict = {"rut": rut, "nombre": nombre, "apellidos": apellidos}
    _CAMPOS_OPCIONALES = ("fecha_nacimiento", "sexo", "celular", "telefono", "email",
                          "comuna", "direccion", "ciudad")
    extras = {}
    for campo in _CAMPOS_OPCIONALES:
        val = kwargs.get(campo)
        if val:
            extras[campo] = val
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await _post(client, f"{MEDILINK_BASE_URL}/pacientes",
                            json=body, headers=HEADERS)
        except httpx.RequestError as e:
            log.error("No se pudo crear paciente rut=%s: %s", rut, e)
            return None
        if r.status_code not in (200, 201):
            log.error("Error crear paciente rut=%s: %s %s", rut, r.status_code, r.text[:200])
            return None
        p = _safe_json(r).get("data", {})
        pac_id = p.get("id")
        if not pac_id:
            log.error("crear_paciente: Medilink response sin id: %s", str(p)[:200])
            return None
        result = {
            "id":     pac_id,
            "nombre": _fmt_nombre_apellidos(p.get('nombre'), p.get('apellidos')),
            "rut":    p.get("rut", ""),
        }
        # PUT para guardar campos opcionales (Medilink POST los ignora)
        if extras:
            try:
                r2 = await client.put(
                    f"{MEDILINK_BASE_URL}/pacientes/{pac_id}",
                    json=extras, headers=HEADERS, timeout=10,
                )
                if r2.status_code not in (200, 201):
                    log.warning("PUT extras paciente %s falló: %s %s",
                                pac_id, r2.status_code, r2.text[:200])
            except httpx.RequestError as e:
                log.warning("PUT extras paciente %s error: %s", pac_id, e)
        return result


async def buscar_paciente(rut: str) -> Optional[dict]:
    """Busca paciente por RUT. Devuelve dict con id, nombre, rut o None.

    Cacheado por 10 min: el mismo RUT se consulta 2-5 veces en un flujo
    (WAIT_RUT_AGENDAR → confirmaciones → CONFIRMING_CITA). Reduce carga
    Medilink y latencia de la conversación.
    """
    # Normalización robusta: remover puntos, guiones, underscores, espacios,
    # tabs y cualquier separador raro que pacientes rurales usan (p. ej.
    # "20_997_207_7", "20 997 207 7", "20.997.207 7"). Solo dejar alfanumérico.
    rut_clean = "".join(c for c in (rut or "").upper() if c.isalnum())
    # El dígito verificador va después del guión
    if len(rut_clean) > 1:
        rut_fmt = rut_clean[:-1] + "-" + rut_clean[-1]
    else:
        return None

    _cached = _paciente_cache.get(rut_clean)
    if _cached and (time.monotonic() - _cached["_ts"]) < _PACIENTE_CACHE_TTL:
        return _cached["data"]

    # Timeout reducido a 5s: si Medilink tarda más, caemos a fallback antes
    # de que Meta retire el webhook (límite 20s).
    async with httpx.AsyncClient(timeout=5) as client:
        params = {"rut": {"lk": rut_clean[:-1]}}
        try:
            r = await _get(client, f"{MEDILINK_BASE_URL}/pacientes",
                           params={"q": _q(params)}, headers=HEADERS)
        except httpx.RequestError as e:
            log.error("No se pudo buscar paciente rut=%s: %s", rut_clean, e)
            return None
        if r.status_code != 200:
            return None
        data = _safe_json(r).get("data", [])
        if not data:
            return None
        p = data[0]
        if not p.get("id"):
            log.error("buscar_paciente: registro sin id para rut=%s: %s", rut_clean, p)
            return None
        result = {
            "id":     p["id"],
            "nombre": _fmt_nombre_apellidos(p.get('nombre'), p.get('apellidos')),
            "rut":    p.get("rut", ""),
        }
        if p.get("fecha_nacimiento"):
            result["fecha_nacimiento"] = p["fecha_nacimiento"]
        if p.get("sexo"):
            result["sexo"] = p["sexo"]
        _paciente_cache[rut_clean] = {"data": result, "_ts": time.monotonic()}
        return result


async def buscar_paciente_por_nombre(nombre: str) -> list[dict]:
    """Busca pacientes por nombre/apellido en Medilink. Devuelve hasta 10 resultados."""
    async with httpx.AsyncClient(timeout=10) as client:
        params = {"nombre": {"lk": nombre}}
        try:
            r = await _get(client, f"{MEDILINK_BASE_URL}/pacientes",
                           params={"q": _q(params)}, headers=HEADERS)
        except httpx.RequestError as e:
            log.error("buscar_paciente_por_nombre '%s': %s", nombre, e)
            return []
        if r.status_code != 200:
            return []
        data = _safe_json(r).get("data", [])
        results = []
        for p in data[:10]:
            nombre_full = _fmt_nombre_apellidos(p.get('nombre'), p.get('apellidos'))
            results.append({
                "id": p["id"],
                "nombre": nombre_full,
                "rut": p.get("rut", ""),
            })
        return results


async def verificar_slot_disponible(id_profesional: int, fecha: str,
                                    hora_inicio: str, hora_fin: str) -> bool:
    """Verifica en tiempo real que el slot sigue libre antes de crear la cita.
    Consulta /citas y /horariosbloqueados frescos.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            ocupadas = await _get_horas_ocupadas(client, id_profesional, fecha)
            bloqueos = await _get_bloqueos(client, id_profesional, fecha)
        except (httpx.RequestError, Exception) as e:
            log.warning("verificar_slot: error consultando prof %d fecha %s: %s",
                        id_profesional, fecha, e)
            return False  # ante duda, decir que no está disponible

        # Verificar que hora_inicio no esté en el set expandido de horas ocupadas
        if hora_inicio[:5] in ocupadas:
            log.warning("verificar_slot: %s ya ocupado para prof %d fecha %s (ocupadas: %s)",
                        hora_inicio, id_profesional, fecha, sorted(ocupadas))
            return False

        # Verificar que no esté bloqueado
        if _slot_bloqueado(hora_inicio, hora_fin, bloqueos):
            log.warning("verificar_slot: %s bloqueado para prof %d fecha %s",
                        hora_inicio, id_profesional, fecha)
            return False

    return True


async def crear_cita(id_paciente: int, id_profesional: int, fecha: str,
                     hora_inicio: str, hora_fin: str, id_recurso: int = 1) -> Optional[dict]:
    """Crea una cita en Medilink. Devuelve dict con id de la cita o None si falla."""
    duracion = _h_to_min(hora_fin) - _h_to_min(hora_inicio)
    body = {
        "id_paciente":    id_paciente,
        "id_profesional": id_profesional,
        "id_sucursal":    MEDILINK_SUCURSAL,
        "id_sillon":      id_recurso,
        "fecha":          fecha,
        "hora_inicio":    hora_inicio,
        "hora_fin":       hora_fin,
        "duracion":       duracion,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await _post(client, f"{MEDILINK_BASE_URL}/citas", json=body, headers=HEADERS)
        except httpx.RequestError as e:
            log.error("No se pudo crear cita paciente=%d prof=%d fecha=%s: %s",
                      id_paciente, id_profesional, fecha, e)
            return None
        if r.status_code in (200, 201):
            try:
                data = _safe_json(r)
            except Exception as e:
                log.error("crear_cita: respuesta no-JSON de Medilink (%s): %s",
                          e, r.text[:300])
                return None
            cita = data.get("data", data)
            if isinstance(cita, list) and cita:
                cita = cita[0]
            cita_id = cita.get("id") if isinstance(cita, dict) else None
            if not cita_id:
                log.error("crear_cita: respuesta sin id — %s", data)
                return None
            # Invalidar cache de próxima fecha tras booking: el slot ya no está
            # disponible. Sin esto, con TTL 15 min el bot puede ofrecer el mismo
            # slot a otros pacientes y provocar doble-booking.
            _proxima_cache.clear()
            return {"id": cita_id, "confirmado": True}
        log.error("crear_cita falló: %s %s", r.status_code, r.text[:500])
        # Invalidar caché de horario si Medilink se queja de horario/duración:
        # asegura que el siguiente buscar_primer_dia consulte fresco.
        if r.status_code == 400 and ("horario" in r.text.lower() or "duraci" in r.text.lower()):
            _horarios_cache.pop(id_profesional, None)
            _proxima_cache.clear()
            log.info("crear_cita 400 horario → caché invalidada para prof %s", id_profesional)
        return None


async def listar_citas_paciente(id_paciente: int, rut: str | None = None) -> list:
    """Lista citas futuras de un paciente.

    Medilink devuelve HTTP 400 ('No puede buscar por id_paciente') al filtrar
    /citas por id_paciente. Si se provee `rut`, consultamos directamente por
    RUT. Dejamos `id_paciente` como intento inicial por compatibilidad, pero
    el camino real es por RUT.
    """
    hoy = datetime.now(_CHILE_TZ).date().strftime("%Y-%m-%d")
    async with httpx.AsyncClient(timeout=10) as client:
        data = []
        # Intento por rut (Medilink no soporta id_paciente en /citas)
        if rut:
            params = {
                "rut": {"eq": rut},
                "fecha": {"gte": hoy},
                "estado_anulacion": {"eq": 0},
            }
            try:
                r = await _get(client, f"{MEDILINK_BASE_URL}/citas",
                               params={"q": _q(params)}, headers=HEADERS)
                if r.status_code == 200:
                    data = _safe_json(r).get("data", [])
            except httpx.RequestError as e:
                log.error("listar_citas_paciente rut=%s: %s", rut, e)
                return []
        else:
            # Fallback: sin rut. Intenta id_paciente (fallará 400 hoy, pero
            # mantenemos por si la API se arregla).
            params = {
                "id_paciente": {"eq": id_paciente},
                "fecha":       {"gte": hoy},
                "estado_anulacion": {"eq": 0},
            }
            try:
                r = await _get(client, f"{MEDILINK_BASE_URL}/citas",
                               params={"q": _q(params)}, headers=HEADERS)
                if r.status_code == 200:
                    data = _safe_json(r).get("data", [])
                else:
                    log.warning("listar_citas_paciente id=%d → HTTP %d (necesita rut)",
                                id_paciente, r.status_code)
            except httpx.RequestError as e:
                log.error("listar_citas_paciente id=%d: %s", id_paciente, e)
                return []
        # Filtrar por id_paciente en cliente por si el RUT tiene más de un registro
        if rut:
            data = [c for c in data if not c.get("id_paciente") or c.get("id_paciente") == id_paciente]
        citas = []
        for c in data:
            id_prof = c.get("id_profesional")
            prof_info = PROFESIONALES.get(id_prof, {}) if id_prof else {}
            citas.append({
                "id":          c["id"],
                "id_profesional": id_prof,
                "profesional": c.get("nombre_profesional", "") or prof_info.get("nombre", ""),
                "especialidad": prof_info.get("especialidad", ""),
                "fecha":       c.get("fecha", ""),
                "fecha_display": _fmt_fecha(c.get("fecha", "")),
                "hora_inicio": c.get("hora_inicio", "")[:5],
                "estado":      c.get("estado_cita", ""),
            })
        return citas[:5]


async def listar_historial_paciente(id_paciente: int, meses: int = 6, rut: str | None = None) -> list:
    """Lista citas pasadas de un paciente (últimos N meses).

    Usa filtro por RUT (Medilink no soporta id_paciente en /citas).
    """
    hoy = datetime.now(_CHILE_TZ).date()
    desde = (hoy - timedelta(days=meses * 30)).strftime("%Y-%m-%d")
    hasta = hoy.strftime("%Y-%m-%d")
    async with httpx.AsyncClient(timeout=10) as client:
        data = []
        if rut:
            params = {
                "rut": {"eq": rut},
                "fecha": {"gte": desde, "lte": hasta},
                "estado_anulacion": {"eq": 0},
            }
            try:
                r = await _get(client, f"{MEDILINK_BASE_URL}/citas",
                               params={"q": _q(params)}, headers=HEADERS)
                if r.status_code == 200:
                    data = _safe_json(r).get("data", [])
            except httpx.RequestError as e:
                log.error("listar_historial_paciente rut=%s: %s", rut, e)
                return []
            # Filtrar por id_paciente por si hay otros registros con el mismo RUT
            data = [c for c in data if not c.get("id_paciente") or c.get("id_paciente") == id_paciente]
            # Defensa: Medilink a veces ignora `lte` y devuelve citas futuras.
            # Historial no debe incluir fechas >= hoy.
            hoy_str = hoy.strftime("%Y-%m-%d")
            data = [c for c in data if c.get("fecha", "") < hoy_str]
        else:
            log.warning("listar_historial_paciente id=%d sin rut — Medilink no soporta id_paciente filter", id_paciente)
            return []
        citas = []
        for c in data:
            id_prof = c.get("id_profesional")
            prof_info = PROFESIONALES.get(id_prof, {}) if id_prof else {}
            citas.append({
                "id":           c["id"],
                "profesional":  c.get("nombre_profesional", "") or prof_info.get("nombre", ""),
                "especialidad": prof_info.get("especialidad", ""),
                "fecha":        c.get("fecha", ""),
                "fecha_display": _fmt_fecha(c.get("fecha", "")),
                "hora_inicio":  c.get("hora_inicio", "")[:5],
            })
        # Ordenar por fecha descendente (más reciente primero)
        citas.sort(key=lambda x: x["fecha"], reverse=True)
        return citas[:20]


async def obtener_agenda_dia(id_prof: int, fecha: str | None = None) -> list[dict]:
    """Obtiene la agenda completa de un profesional para una fecha, con datos del paciente.
    Retorna lista de dicts con id_cita, hora, paciente (nombre, rut, edad, sexo), estado."""
    if not fecha:
        fecha = datetime.now(_CHILE_TZ).date().strftime("%Y-%m-%d")
    params = {
        "id_sucursal":      {"eq": MEDILINK_SUCURSAL},
        "id_profesional":   {"eq": id_prof},
        "fecha":            {"eq": fecha},
        "estado_anulacion": {"eq": 0},
    }
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await _get(client, f"{MEDILINK_BASE_URL}/citas",
                           params={"q": _q(params)}, headers=HEADERS)
        except httpx.RequestError as e:
            log.error("obtener_agenda_dia prof=%d fecha=%s: %s", id_prof, fecha, e)
            return []
        if r.status_code != 200:
            return []
        citas_raw = _safe_json(r).get("data", [])

        # Obtener datos de pacientes en paralelo (máx 5 concurrentes)
        import asyncio as _aio
        _sem = _aio.Semaphore(5)

        async def _fetch_pac(pac_id):
            if not pac_id:
                return {}
            # Cache por ID (10 min) reduce traffic Medilink en jobs repetitivos
            _cached = _paciente_id_cache.get(pac_id)
            if _cached and (time.monotonic() - _cached["_ts"]) < _PAC_ID_TTL:
                return _cached["data"]
            async with _sem:
                try:
                    rp = await _get(client, f"{MEDILINK_BASE_URL}/pacientes/{pac_id}",
                                    headers=HEADERS)
                    if rp.status_code == 200:
                        p = _safe_json(rp).get("data", {})
                        if isinstance(p, list) and p:
                            p = p[0]
                        if p:
                            _paciente_id_cache[pac_id] = {"data": p, "_ts": time.monotonic()}
                        return p
                except httpx.RequestError:
                    pass
            return {}

        pac_tasks = [_fetch_pac(c.get("id_paciente")) for c in citas_raw]
        pac_results = await _aio.gather(*pac_tasks)

        agenda = []
        for c, p in zip(citas_raw, pac_results):
            pac_nombre = c.get("nombre_paciente", "")
            pac_rut = ""
            pac_edad = ""
            pac_sexo = ""
            if p:
                pac_nombre = _fmt_nombre_apellidos(p.get('nombre'), p.get('apellidos')) or pac_nombre
                pac_rut = p.get("rut", "")
                pac_sexo = p.get("sexo", "")
                pac_fecha_nac = p.get("fecha_nacimiento", "")
                if pac_fecha_nac:
                    try:
                        fn = datetime.strptime(pac_fecha_nac[:10], "%Y-%m-%d").date()
                        hoy = datetime.now(_CHILE_TZ).date()
                        edad = hoy.year - fn.year - ((hoy.month, hoy.day) < (fn.month, fn.day))
                        pac_edad = f"{edad} años"
                    except ValueError:
                        pass

            agenda.append({
                "id_cita":    c["id"],
                "hora":       c.get("hora_inicio", "")[:5],
                "hora_fin":   c.get("hora_fin", "")[:5],
                "paciente":   pac_nombre,
                "rut":        pac_rut,
                "edad":       pac_edad,
                "sexo":       pac_sexo,
                "estado":     c.get("estado_cita", ""),
            })
        agenda.sort(key=lambda x: x["hora"])
        return agenda


async def get_cita(id_cita: int) -> dict | None:
    """Obtiene una cita por ID desde Medilink.
    Devuelve el dict crudo de Medilink (con estado_cita, id_estado, estado_anulacion, etc)
    o None si la cita no existe o la llamada fallo."""
    url = f"{MEDILINK_BASE_URL}/citas/{id_cita}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=HEADERS)
            if r.status_code != 200:
                return None
            data = _safe_json(r)
            if isinstance(data, dict) and "data" in data:
                payload = data["data"]
                if isinstance(payload, list):
                    return payload[0] if payload else None
                return payload if isinstance(payload, dict) else None
            return data if isinstance(data, dict) else None
    except Exception as e:
        log.warning("get_cita %d fallo: %s", id_cita, e)
        return None


def cita_esta_confirmada(cita: dict | None) -> bool:
    """True si la cita ya fue marcada como confirmada por la recepcion en Medilink.
    Detecta los valores tipicos en `estado_cita` (case-insensitive) porque Medilink
    no documenta los id_estado numericos y pueden variar por cuenta."""
    if not cita:
        return False
    estado = (cita.get("estado_cita") or "").strip().lower()
    if estado in ("confirmada", "confirmado", "asistira", "asiste"):
        return True
    return False


async def cancelar_cita(id_cita: int) -> bool:
    """Cancela una cita por su ID, con reintentos ante errores transitorios."""
    url = f"{MEDILINK_BASE_URL}/citas/{id_cita}"
    async with httpx.AsyncClient(timeout=10) as client:
        for attempt in range(3):
            try:
                r = await client.put(url, json={"id_estado": 1}, headers=HEADERS)
                if r.status_code == 429:
                    record_429(url)
                    wait = 3.0 * (2 ** attempt)
                    log.warning("Medilink PUT %s → 429 rate limit, esperando %.0fs (intento %d/3)", url, wait, attempt + 1)
                    await asyncio.sleep(wait)
                    continue
                if r.status_code in (200, 201):
                    _report_up()
                    # Slot liberado: invalidar cache de próxima fecha para que
                    # el próximo paciente vea el slot recién disponible.
                    _proxima_cache.clear()
                    return True
                if r.status_code >= 500:
                    log.warning("Medilink PUT %s → %s (intento %d/3)", url, r.status_code, attempt + 1)
                else:
                    log.error("Error cancelar cita %d: %s %s", id_cita, r.status_code, r.text[:200])
                    return False
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                log.warning("Medilink PUT %s error red: %s (intento %d/3)", url, e, attempt + 1)
            if attempt < 2:
                await asyncio.sleep(1.5 ** attempt)
        log.error("No se pudo cancelar cita %d tras 3 intentos", id_cita)
        return False


# Especialidades con pacientes recurrentes — para el panel de seguimiento
SEGUIMIENTO_ESPECIALIDADES = {
    "kinesiologia":  {"label": "Kinesiología",  "ids": [77, 21],     "precio_fonasa": 7830,  "precio_particular": 20000},
    "ortodoncia":    {"label": "Ortodoncia",     "ids": [66],         "precio_fonasa": None,  "precio_particular": 30000},
    "psicologia":    {"label": "Psicología",     "ids": [74, 49],     "precio_fonasa": 14420, "precio_particular": 20000},
    "nutricion":     {"label": "Nutrición",      "ids": [52],         "precio_fonasa": 4770,  "precio_particular": 20000},
}


async def sync_ortodoncia_rango(fecha_ini: str, fecha_fin: str, delay: float = 2.0):
    """Sincroniza visitas de Daniela Castillo (id=66) con monto desde atenciones.
    Llama a /citas por día y luego /atenciones/{id} para obtener el total.
    Clasifica automáticamente: 120000=instalacion, 30000=control, otro=pendiente."""
    from session import upsert_ortodoncia_cache, get_ortodoncia_sync_max_fecha
    from datetime import datetime as dt, timedelta

    inicio = dt.strptime(fecha_ini, "%Y-%m-%d").date()
    fin    = dt.strptime(fecha_fin,  "%Y-%m-%d").date()

    async with httpx.AsyncClient(timeout=20) as client:
        d = inicio
        while d <= fin:
            fecha = d.isoformat()
            params = {
                "id_sucursal":    {"eq": int(MEDILINK_SUCURSAL)},
                "id_profesional": {"eq": 66},
                "fecha":          {"eq": fecha},
                "estado_anulacion": {"eq": 0},
            }
            try:
                r = await _get(client, f"{MEDILINK_BASE_URL}/citas",
                               params={"q": _q(params)}, headers=HEADERS)
                if r.status_code == 200:
                    citas = [c for c in _safe_json(r).get("data", []) if c.get("id_paciente")]
                    visitas = []
                    for c in citas:
                        id_aten = c.get("id_atencion")
                        total   = 0
                        if id_aten:
                            await asyncio.sleep(delay)
                            ra = await _get(client, f"{MEDILINK_BASE_URL}/atenciones/{id_aten}",
                                            headers=HEADERS)
                            if ra.status_code == 200:
                                total = _safe_json(ra).get("data", {}).get("total", 0)
                        tipo = "instalacion" if total == 120000 else ("control" if total == 30000 else "pendiente")
                        visitas.append({
                            "id_atencion":     id_aten or 0,
                            "id_paciente":     c["id_paciente"],
                            "paciente_nombre": (c.get("nombre_paciente") or "").strip(),
                            "fecha":           fecha,
                            "hora_inicio":     (c.get("hora_inicio") or "")[:5],
                            "total":           total,
                            "tipo":            tipo,
                        })
                    if visitas:
                        upsert_ortodoncia_cache(visitas)
                        log.info("ortodoncia sync %s → %d visitas", fecha, len(visitas))
            except Exception as e:
                log.error("ortodoncia sync %s: %s", fecha, e)
            await asyncio.sleep(delay)
            d += timedelta(days=1)


async def sync_citas_dia(fecha: str, ids_prof: list[int]):
    """Descarga las citas de una fecha desde Medilink y las guarda en caché.
    Borra primero las existentes para esa fecha/profesional para mantener consistencia."""
    from session import upsert_citas_cache, delete_citas_cache_fecha
    async with httpx.AsyncClient(timeout=30) as client:
        for id_prof in ids_prof:
            delete_citas_cache_fecha(id_prof, fecha)
            params = {
                "id_sucursal":      {"eq": int(MEDILINK_SUCURSAL)},
                "id_profesional":   {"eq": id_prof},
                "fecha":            {"eq": fecha},
                "estado_anulacion": {"eq": 0},
            }
            try:
                r = await _get(client, f"{MEDILINK_BASE_URL}/citas",
                               params={"q": _q(params)}, headers=HEADERS)
                if r.status_code != 200:
                    continue
                citas = [
                    {
                        "id_prof":         id_prof,
                        "id_paciente":     c.get("id_paciente"),
                        "paciente_nombre": (c.get("nombre_paciente") or "").strip(),
                        "fecha":           fecha,
                        "hora_inicio":     (c.get("hora_inicio") or "")[:5],
                    }
                    for c in _safe_json(r).get("data", [])
                    if c.get("id_paciente")
                ]
                upsert_citas_cache(citas)
                log.info("sync_citas_dia: prof=%d fecha=%s → %d citas", id_prof, fecha, len(citas))
            except Exception as e:
                log.error("sync_citas_dia prof=%d fecha=%s: %s", id_prof, fecha, e)


async def get_citas_seguimiento_mes(year: int, month: int, especialidad: str = "kinesiologia") -> list:
    """Retorna las citas del mes para una especialidad recurrente, agrupadas por paciente."""
    import calendar
    from collections import defaultdict

    cfg = SEGUIMIENTO_ESPECIALIDADES.get(especialidad)
    if not cfg:
        return []

    last_day = calendar.monthrange(year, month)[1]
    ids_prof = cfg["ids"]

    # Sincronizar días del mes actual que no estén en caché todavía
    from session import get_citas_cache_mes, citas_cache_tiene_fecha
    hoy_cl = datetime.now(_CHILE_TZ).date()
    hoy_str = hoy_cl.strftime("%Y-%m-%d")

    dias_a_sync = []
    for id_prof in ids_prof:
        for day in range(1, last_day + 1):
            fecha = f"{year}-{month:02d}-{day:02d}"
            # Solo sincronizar días pasados o de hoy, no días futuros sin datos
            if fecha > hoy_str:
                continue
            if not citas_cache_tiene_fecha(id_prof, fecha):
                dias_a_sync.append((id_prof, fecha))

    if dias_a_sync:
        log.info("get_citas_seguimiento_mes: sincronizando %d combos prof/fecha faltantes", len(dias_a_sync))
        async with httpx.AsyncClient(timeout=30) as client:
            async def _fetch_y_cache(id_prof: int, fecha: str):
                from session import upsert_citas_cache, delete_citas_cache_fecha
                params = {
                    "id_sucursal":      {"eq": int(MEDILINK_SUCURSAL)},
                    "id_profesional":   {"eq": id_prof},
                    "fecha":            {"eq": fecha},
                    "estado_anulacion": {"eq": 0},
                }
                try:
                    r = await _get(client, f"{MEDILINK_BASE_URL}/citas",
                                   params={"q": _q(params)}, headers=HEADERS)
                    if r.status_code != 200:
                        return
                    citas = [
                        {
                            "id_prof":         id_prof,
                            "id_paciente":     c.get("id_paciente"),
                            "paciente_nombre": (c.get("nombre_paciente") or "").strip(),
                            "fecha":           fecha,
                            "hora_inicio":     (c.get("hora_inicio") or "")[:5],
                        }
                        for c in _safe_json(r).get("data", [])
                        if c.get("id_paciente")
                    ]
                    # Guardar aunque esté vacío (marca el día como sincronizado)
                    if citas:
                        upsert_citas_cache(citas)
                    else:
                        # Insertar registro centinela para no re-sync días sin citas
                        upsert_citas_cache([{
                            "id_prof": id_prof, "id_paciente": 0,
                            "paciente_nombre": "__empty__", "fecha": fecha, "hora_inicio": "00:00"
                        }])
                except Exception as e:
                    log.error("sync mes esp=%s prof=%d %s: %s", especialidad, id_prof, fecha, e)

            await asyncio.gather(*[_fetch_y_cache(p, f) for p, f in dias_a_sync])

    # Leer todo desde caché
    citas_raw_cache = get_citas_cache_mes(year, month, ids_prof)
    # Filtrar centinelas de días vacíos
    citas_raw = [
        {"id_prof": c["id_prof"], "prof_nombre": PROFESIONALES.get(c["id_prof"], {}).get("nombre", ""),
         "fecha": c["fecha"], "id_paciente": c["id_paciente"], "paciente_nombre": c["paciente_nombre"]}
        for c in citas_raw_cache
        if c["id_paciente"] != 0
    ]

    grupos: dict = defaultdict(list)
    for c in citas_raw:
        key = (c["id_paciente"], c["id_prof"])
        grupos[key].append(c)

    result = []
    for (id_pac, id_prof), citas in grupos.items():
        citas_sorted = sorted(citas, key=lambda x: x["fecha"])
        result.append({
            "id_paciente":     id_pac,
            "id_prof":         id_prof,
            "prof_nombre":     PROFESIONALES[id_prof]["nombre"],
            "paciente_nombre": citas_sorted[0]["paciente_nombre"],
            "sesiones_mes":    len(citas_sorted),
            "fechas":          [c["fecha"] for c in citas_sorted],
            "primera_fecha":   citas_sorted[0]["fecha"],
            "ultima_fecha":    citas_sorted[-1]["fecha"],
        })

    return sorted(result, key=lambda x: x["primera_fecha"])


# Mantener alias para compatibilidad
async def get_citas_kine_mes(year: int, month: int) -> list:
    return await get_citas_seguimiento_mes(year, month, "kinesiologia")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_fecha(fecha: str) -> str:
    """'2026-03-25' → 'Miércoles 25 de marzo'"""
    try:
        d = datetime.strptime(fecha, "%Y-%m-%d")
        dias = ["Lunes","Martes","Miércoles","Jueves","Viernes","Sábado","Domingo"]
        meses = ["enero","febrero","marzo","abril","mayo","junio",
                 "julio","agosto","septiembre","octubre","noviembre","diciembre"]
        return f"{dias[d.weekday()]} {d.day} de {meses[d.month-1]}"
    except (ValueError, TypeError):
        return fecha



def _fmt_nombre_apellidos(nombre: str, apellidos: str) -> str:
    """Une nombre + apellidos colapsando espacios extra (Medilink manda valores con trailing whitespace)."""
    return " ".join((str(nombre or "") + " " + str(apellidos or "")).split())


def valid_rut(rut: str) -> bool:
    """Valida RUT chileno con módulo 11. Rechaza formatos inválidos y rangos absurdos."""
    if not rut or not isinstance(rut, str):
        return False
    try:
        rut = rut.replace(".", "").replace("-", "").strip().upper()
        # Cuerpo numérico entre 7 y 8 dígitos; DV es dígito o K
        if len(rut) < 8 or len(rut) > 9:
            return False
        cuerpo, dv = rut[:-1], rut[-1]
        if not cuerpo.isdigit() or not (dv.isdigit() or dv == "K"):
            return False
        suma = 0
        multiplo = 2
        for c in reversed(cuerpo):
            suma += int(c) * multiplo
            multiplo = multiplo + 1 if multiplo < 7 else 2
        resto = 11 - (suma % 11)
        dv_calc = "0" if resto == 11 else ("K" if resto == 10 else str(resto))
        return dv == dv_calc
    except (ValueError, TypeError, IndexError):
        return False


def _calcular_dv_rut(cuerpo: str) -> str:
    """Calcula el dígito verificador de un RUT chileno con módulo 11."""
    if not cuerpo.isdigit():
        return ""
    suma, multiplo = 0, 2
    for c in reversed(cuerpo):
        suma += int(c) * multiplo
        multiplo = multiplo + 1 if multiplo < 7 else 2
    resto = 11 - (suma % 11)
    return "0" if resto == 11 else ("K" if resto == 10 else str(resto))


def clean_rut(rut: str) -> str:
    """Normaliza RUT con múltiples formatos aceptados:
    - '12.345.678-9' / '12345678-9'      → '12345678-9' (ya válido)
    - '123456789'                         → '12345678-9' (último char es DV)
    - '12 345 678 9' / '12.345.678 9'     → '12345678-9' (espacios/puntos)
    - '12345678' (8 dígitos sin DV)       → '12345678-K' (calcula DV)
    - '1234567' (7 dígitos sin DV)        → '1234567-K' (calcula DV)
    - 'rut 12.345.678-9' / '12345678 /9'  → '12345678-9'

    Reduce fricción en WAIT_RUT_* donde pacientes rurales escriben sin guión
    ni puntos ('209972077') o sólo el cuerpo ('20997207').

    Casos cubiertos para robustez máxima:
    - Unicode NFKC: fullwidth '１２３４５６７８－９' → '12345678-9'
    - Chars invisibles: ZWSP, ZWJ, ZWNJ, BOM, nbsp.
    - Envolturas: "...", '...', «...», [...], {...}, <...>.
    - Emojis o texto alrededor (filtro final por clase [^0-9-K]).
    - Prefijos: rut:, mi rut es, ci:, cédula:, n°, nro:, #.
    - Separadores: - _ / | : * · • y dashes Unicode (‐‑‒–—―−⸺⁃).
    """
    if not rut:
        return ""
    # 1. Unicode NFKC (fullwidth → ASCII, compatibility decomposition)
    try:
        import unicodedata as _ud
        rut = _ud.normalize("NFKC", rut)
    except Exception:
        pass
    # 2. Quitar chars invisibles (ZWSP, ZWNJ, ZWJ, LRM, RLM, BOM, word joiner)
    rut = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", rut)
    # 3. Prefijos contextuales ('rut:', 'mi rut es', 'ci:', 'cédula:', 'n°', 'nro:', '#')
    rut = re.sub(
        r"(?i)(mi\s+(rut|c[eé]dula)\s+es|rut[:\s]|c[eé]dula[:\s]|ci[:\s]"
        r"|n(ro|[°ºo])[:\s]+|#)",
        " ",
        rut,
    )
    # 4. Normalizar separadores extraños a guión: _ / | · • : * + dashes Unicode
    rut = re.sub(r"[/|·•_:\*\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u2E3A\u2043]", "-", rut)
    rut = rut.upper()
    # 5. EXTRACCIÓN por regex: buscar secuencia tipo RUT dentro del texto.
    # Permite puntos/espacios/nbsp internos en el cuerpo y guión(es) antes del DV.
    # Así toleramos texto circundante, envolturas ("", [], {}, «»), emojis, etc.
    m = re.search(
        r"(\d[\d.\s\u00a0]{5,}\d)(?:\s*-+\s*([0-9K]))?(?![0-9K])",
        rut,
    )
    if m:
        cuerpo_digitos = re.sub(r"\D", "", m.group(1))
        dv = m.group(2)
        if dv:
            if 7 <= len(cuerpo_digitos) <= 8:
                return f"{cuerpo_digitos}-{dv}"
            if len(cuerpo_digitos) == 9 and cuerpo_digitos[-1] == dv:
                return f"{cuerpo_digitos[:8]}-{dv}"
        else:
            if len(cuerpo_digitos) == 9:
                return f"{cuerpo_digitos[:8]}-{cuerpo_digitos[8]}"
            if 7 <= len(cuerpo_digitos) <= 8:
                dv_calc = _calcular_dv_rut(cuerpo_digitos)
                if dv_calc:
                    return f"{cuerpo_digitos}-{dv_calc}"
    # 6. Fallback para strings sin estructura clara: quitar puntuación/envolturas
    # y aplicar lógica simple (para que LEN-01/LEN-02 sigan retornando algo).
    rut = re.sub(r"[.\s()\[\]{}<>\,'\"«»‹›\u2018\u2019\u201c\u201d]", "", rut).strip()
    rut = re.sub(r"[^0-9\-K]", "", rut)
    rut = rut.replace("--", "-")
    if not rut:
        return ""
    if "-" in rut:
        cuerpo, dv = rut.rsplit("-", 1)
        return f"{cuerpo}-{dv}" if cuerpo and dv else rut
    if rut.isdigit() and 7 <= len(rut) <= 8:
        dv = _calcular_dv_rut(rut)
        if dv:
            return f"{rut}-{dv}"
    if len(rut) >= 2:
        return rut[:-1] + "-" + rut[-1]
    return rut


def especialidades_disponibles() -> str:
    vistas = set()
    lista = []
    for info in PROFESIONALES.values():
        e = info["especialidad"]
        if e not in vistas:
            lista.append(f"• {e}")
            vistas.add(e)
    return "\n".join(sorted(lista))
