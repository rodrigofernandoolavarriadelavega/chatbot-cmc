"""
Wrapper para la API de Medilink 2 (healthatom).
Base URL: https://api.medilink2.healthatom.com/api/v5
"""
import asyncio
import json
import logging
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

import httpx

from config import MEDILINK_BASE_URL, MEDILINK_TOKEN, MEDILINK_SUCURSAL

log = logging.getLogger("medilink")

HEADERS = {"Authorization": f"Token {MEDILINK_TOKEN}"}

# Caché de horarios por profesional: id_prof → {intervalo, dias (set de weekdays Python)}
_horarios_cache: dict = {}

# Medilink usa dia 1=Lun..6=Sáb, 7=Dom → Python weekday 0=Lun..5=Sáb, 6=Dom
_MEDILINK_DIA_TO_WEEKDAY = {1:0, 2:1, 3:2, 4:3, 5:4, 6:5, 7:6}

# Profesionales habilitados en el CMC (id → info)
PROFESIONALES = {
     1: {"nombre": "Dr. Rodrigo Olavarría",    "especialidad": "Medicina General",      "intervalo": 15},
    73: {"nombre": "Dr. Andrés Abarca",        "especialidad": "Medicina General",      "intervalo": 15},
    13: {"nombre": "Dr. Alonso Márquez",       "especialidad": "Medicina General",      "intervalo": 20},
    23: {"nombre": "Dr. Manuel Borrego",       "especialidad": "Otorrinolaringología",  "intervalo": 20},
    60: {"nombre": "Dr. Miguel Millán",        "especialidad": "Cardiología",           "intervalo": 20, "dias": [5]},
    64: {"nombre": "Dr. Claudio Barraza",      "especialidad": "Traumatología",         "intervalo": 15},
    61: {"nombre": "Dr. Tirso Rejón",          "especialidad": "Ginecología",           "intervalo": 20},
    65: {"nombre": "Dr. Nicolás Quijano",      "especialidad": "Gastroenterología",     "intervalo": 20},
    55: {"nombre": "Dra. Javiera Burgos",      "especialidad": "Odontología General",   "intervalo": 30},
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
    "medicina general": [73, 1, 13], "médico": [73, 1, 13], "medico general": [73, 1, 13], "doctor": [73, 1, 13],
    "medicina familiar": [13], "médico familiar": [13],
    "otorrinolaringología": [23], "otorrino": [23], "orl": [23],
    "olavarría": [1], "olavarria": [1],
    "abarca": [73],
    "etcheverry": [21], "leo": [21], "leonardo": [21],
    "armijo": [77], "luis armijo": [77],
    "paola acosta": [59], "paola": [59],
    "odontología": [72, 55], "dentista": [72, 55], "odontólogo": [72, 55],
    "endodoncia": [75], "endodoncista": [75],
    "estética facial": [76], "estetica facial": [76], "estética": [76],
    "fonoaudiología": [70], "fonoaudiólogo": [70], "fonoaudiologa": [70],
    "implantología": [69], "implantes": [69],
    "matrona": [67], "ginecología": [61], "ginecólogo": [61],
    "traumatología": [64], "traumatólogo": [64],
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
    "medicina general": 10, "médico": 10, "medico general": 10, "doctor": 10,
    "odontología": 9, "dentista": 9, "odontólogo": 9,
    "fonoaudiología": 8, "fonoaudiólogo": 8, "fonoaudiologa": 8,
    "implantología": 20, "implantes": 20,
    "matrona": 11,
    "ginecología": 14, "ginecólogo": 14,
    "traumatología": 17, "traumatólogo": 17,
    "cardiología": 16, "cardiólogo": 16,
    "gastroenterología": 18, "gastroenterólogo": 18,
    "psicología": 5, "psicólogo": 5, "psicóloga": 5,
    "nutrición": 4, "nutricionista": 4,
    "podología": 12, "podólogo": 12,
    "ortodoncia": 19, "ortodoncista": 19,
    "ecografía": 13, "ecografista": 13, "tecnólogo": 13,
    "otorrinolaringología": 6, "otorrino": 6,
}


def _q(params: dict) -> str:
    return urllib.parse.quote(json.dumps(params))


async def _get(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """GET con 2 reintentos ante errores de red, 5xx o 429 (rate limit)."""
    for attempt in range(3):
        try:
            r = await client.get(url, **kwargs)
            if r.status_code == 429:
                wait = 3.0 * (2 ** attempt)  # 3s, 6s, 12s
                log.warning("Medilink GET %s → 429 rate limit, esperando %.0fs (intento %d/3)", url, wait, attempt + 1)
                await asyncio.sleep(wait)
                continue
            if r.status_code < 500:
                return r
            log.warning("Medilink GET %s → %s (intento %d/3)", url, r.status_code, attempt + 1)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.warning("Medilink GET %s error red: %s (intento %d/3)", url, e, attempt + 1)
        if attempt < 2:
            await asyncio.sleep(1.5 ** attempt)
    raise httpx.RequestError(f"Medilink no respondió tras 3 intentos: {url}")


async def _post(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """POST con 1 reintento ante errores de red, 5xx o 429 (rate limit)."""
    for attempt in range(2):
        try:
            r = await client.post(url, **kwargs)
            if r.status_code == 429:
                wait = 3.0 * (2 ** attempt)  # 3s, 6s
                log.warning("Medilink POST %s → 429 rate limit, esperando %.0fs (intento %d/2)", url, wait, attempt + 1)
                await asyncio.sleep(wait)
                continue
            if r.status_code < 500:
                return r
            log.warning("Medilink POST %s → %s (intento %d/2)", url, r.status_code, attempt + 1)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.warning("Medilink POST %s error red: %s (intento %d/2)", url, e, attempt + 1)
        if attempt < 1:
            await asyncio.sleep(1.5)
    raise httpx.RequestError(f"Medilink no respondió tras 2 intentos: {url}")


async def _get_horario(client: httpx.AsyncClient, id_prof: int) -> dict:
    """Obtiene intervalo, días de trabajo y horarios por día desde la API (con caché).
    Retorna: {intervalo, dias: set(weekdays), horario_dia: {weekday: (hi, hf)}}
    """
    if id_prof in _horarios_cache:
        return _horarios_cache[id_prof]

    try:
        r = await _get(client, f"{MEDILINK_BASE_URL}/profesionales/{id_prof}/horarios", headers=HEADERS)
    except httpx.RequestError as e:
        log.error("No se pudo obtener horario del profesional %d: %s", id_prof, e)
        return {"intervalo": PROFESIONALES[id_prof]["intervalo"], "dias": set(range(5)), "horario_dia": {}}
    horario = {"intervalo": PROFESIONALES[id_prof]["intervalo"], "dias": set(range(6)), "horario_dia": {}}
    if r.status_code == 200:
        data = r.json().get("data", [])
        sucursal_data = next((x for x in data if x.get("id_sucursal") == int(MEDILINK_SUCURSAL)), None)
        if sucursal_data:
            dias_activos = set()
            horario_dia = {}
            for d in sucursal_data.get("dias", []):
                hi = d.get("hora_inicio", "")
                hf = d.get("hora_fin", "")
                if hi and hf and hi != hf:
                    wd = _MEDILINK_DIA_TO_WEEKDAY.get(d["dia"])
                    if wd is not None:
                        dias_activos.add(wd)
                        horario_dia[wd] = (hi[:5], hf[:5])
            horario = {
                "intervalo":   PROFESIONALES[id_prof]["intervalo"],
                "dias":        dias_activos if dias_activos else set(range(5)),
                "horario_dia": horario_dia,
            }

    _horarios_cache[id_prof] = horario
    return horario


async def _get_bloqueos(client: httpx.AsyncClient, id_prof: int, fecha: str) -> list:
    """Retorna lista de rangos bloqueados (hora_inicio, hora_fin) para ese profesional y fecha.
    La API solo filtra por id_sucursal y fecha — filtramos id_profesional en código.
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
    return [
        (b["hora_inicio"][:5], b["hora_fin"][:5])
        for b in r.json().get("data", [])
        if b.get("id_profesional") == id_prof
    ]


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
    """Retorna set de hora_inicio ocupadas según /citas (fuente de verdad real)."""
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
        return set()
    if r.status_code != 200:
        return set()
    return {c["hora_inicio"][:5] for c in r.json().get("data", [])}


def _generar_slots_horario(hora_inicio: str, hora_fin: str, intervalo: int) -> list:
    """Genera lista de (hi, hf) en strings 'HH:MM' desde hora_inicio hasta hora_fin."""
    slots = []
    cur = _h_to_min(hora_inicio)
    fin = _h_to_min(hora_fin)
    while cur + intervalo <= fin:
        hi = f"{cur // 60:02d}:{cur % 60:02d}"
        hf_min = cur + intervalo
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
            hi_dia, hf_dia = horario_dia[weekday]
        else:
            # Sin info de horas específicas para este día, saltamos
            log.warning("Sin horario_dia para prof %d weekday %d", id_prof, weekday)
            continue

        intervalo       = h["intervalo"]
        bloqueos        = await _get_bloqueos(client, id_prof, fecha)
        ocupadas_citas  = await _get_horas_ocupadas(client, id_prof, fecha)
        horas_ocupadas |= ocupadas_citas
        horas_vistas    = {s["hora_inicio"] for s in todos_libres}

        ahora_min = _h_to_min(datetime.now().strftime("%H:%M")) if fecha == datetime.now().date().strftime("%Y-%m-%d") else None
        for hi, hf in _generar_slots_horario(hi_dia, hf_dia, intervalo):
            if ahora_min is not None and _h_to_min(hi) <= ahora_min:
                continue  # slot ya pasó hoy
            if hi in ocupadas_citas:
                horas_ocupadas.add(hi)
            elif not _slot_bloqueado(hi, hf, bloqueos) and hi not in horas_vistas:
                horas_vistas.add(hi)
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
                            intervalo_override: dict = None) -> tuple[list, list]:
    """
    Retorna (smart_5, todos_libres) del día disponible más próximo.
    Usa /especialidades/{id}/proxima para descubrir la primera fecha disponible,
    luego obtiene todos los slots reales de ese día cruzando con /citas.
    intervalo_override: {id_prof: minutos} para sobreescribir el intervalo de un profesional.
    """
    ids = _ids_para_especialidad(especialidad)
    if not ids:
        return [], []

    usar_prioridad = especialidad.lower() in _ESPECIALIDADES_PRIORIDAD
    excluir_set = set(excluir or [])
    id_esp = _id_especialidad(especialidad)

    async with httpx.AsyncClient(timeout=15) as client:
        horarios = {i: await _get_horario(client, i) for i in ids}
        if intervalo_override:
            for id_prof, mins in intervalo_override.items():
                if id_prof in horarios:
                    horarios[id_prof] = {**horarios[id_prof], "intervalo": mins}

        # Intentar con /proxima para descubrir la fecha rápidamente
        primera_fecha = None
        if id_esp:
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
                for slot in r.json().get("data", []):
                    # fecha viene en formato DD/MM/YYYY
                    raw = slot.get("fecha", "")
                    try:
                        fecha_dt = datetime.strptime(raw, "%d/%m/%Y").strftime("%Y-%m-%d")
                    except ValueError:
                        continue
                    if fecha_dt not in excluir_set:
                        primera_fecha = fecha_dt
                        break

        # Siempre escanear desde hoy, usando primera_fecha como límite superior o fallback
        hoy = datetime.now().date()
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
            for slot in r.json().get("data", []):
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
        horarios = {i: await _get_horario(client, i) for i in ids}
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
        horarios = {i: await _get_horario(client, i) for i in ids}
        if intervalo_override:
            for id_prof, mins in intervalo_override.items():
                if id_prof in horarios:
                    horarios[id_prof] = {**horarios[id_prof], "intervalo": mins}
        return await _slots_para_fecha(client, ids, horarios, fecha)


async def crear_paciente(rut: str, nombre: str, apellidos: str) -> Optional[dict]:
    """Crea un nuevo paciente en Medilink. Retorna dict con id, nombre, rut o None."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await _post(client, f"{MEDILINK_BASE_URL}/pacientes",
                            json={"rut": rut, "nombre": nombre, "apellidos": apellidos},
                            headers=HEADERS)
        except httpx.RequestError as e:
            log.error("No se pudo crear paciente rut=%s: %s", rut, e)
            return None
        if r.status_code in (200, 201):
            p = r.json().get("data", {})
            return {
                "id":     p["id"],
                "nombre": f"{p.get('nombre','')} {p.get('apellidos','')}".strip(),
                "rut":    p.get("rut", ""),
            }
        log.error("Error crear paciente rut=%s: %s %s", rut, r.status_code, r.text[:200])
        return None


async def buscar_paciente(rut: str) -> Optional[dict]:
    """Busca paciente por RUT. Devuelve dict con id, nombre, rut o None."""
    rut_clean = rut.replace(".", "").replace("-", "").strip().upper()
    # El dígito verificador va después del guión
    if len(rut_clean) > 1:
        rut_fmt = rut_clean[:-1] + "-" + rut_clean[-1]
    else:
        return None

    async with httpx.AsyncClient(timeout=10) as client:
        params = {"rut": {"lk": rut_clean[:-1]}}
        try:
            r = await _get(client, f"{MEDILINK_BASE_URL}/pacientes",
                           params={"q": _q(params)}, headers=HEADERS)
        except httpx.RequestError as e:
            log.error("No se pudo buscar paciente rut=%s: %s", rut_clean, e)
            return None
        if r.status_code != 200:
            return None
        data = r.json().get("data", [])
        if not data:
            return None
        p = data[0]
        return {
            "id":     p["id"],
            "nombre": f"{p.get('nombre','')} {p.get('apellidos','')}".strip(),
            "rut":    p.get("rut", ""),
        }


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
            data = r.json()
            cita = data.get("data", data)
            if isinstance(cita, list) and cita:
                cita = cita[0]
            return {"id": cita.get("id"), "confirmado": True}
        print(f"[ERROR crear_cita] {r.status_code}: {r.text[:500]}")
        return None


async def listar_citas_paciente(id_paciente: int) -> list:
    """Lista citas futuras de un paciente."""
    hoy = datetime.now().date().strftime("%Y-%m-%d")
    params = {
        "id_paciente": {"eq": id_paciente},
        "fecha":       {"gte": hoy},
        "estado_anulacion": {"eq": 0},
    }
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await _get(client, f"{MEDILINK_BASE_URL}/citas",
                           params={"q": _q(params)}, headers=HEADERS)
        except httpx.RequestError as e:
            log.error("No se pudo listar citas paciente=%d: %s", id_paciente, e)
            return []
        if r.status_code != 200:
            return []
        data = r.json().get("data", [])
        citas = []
        for c in data:
            citas.append({
                "id":          c["id"],
                "profesional": c.get("nombre_profesional", ""),
                "fecha":       c.get("fecha", ""),
                "fecha_display": _fmt_fecha(c.get("fecha", "")),
                "hora_inicio": c.get("hora_inicio", "")[:5],
                "estado":      c.get("estado_cita", ""),
            })
        return citas[:5]


async def cancelar_cita(id_cita: int) -> bool:
    """Cancela una cita por su ID."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.put(
                f"{MEDILINK_BASE_URL}/citas/{id_cita}",
                json={"id_estado": 1},
                headers=HEADERS,
            )
            if r.status_code not in (200, 201):
                log.error("Error cancelar cita %d: %s %s", id_cita, r.status_code, r.text[:200])
            return r.status_code in (200, 201)
        except httpx.RequestError as e:
            log.error("No se pudo cancelar cita %d: %s", id_cita, e)
            return False


# Especialidades con pacientes recurrentes — para el panel de seguimiento
SEGUIMIENTO_ESPECIALIDADES = {
    "kinesiologia":  {"label": "Kinesiología",  "ids": [77, 21],     "precio_fonasa": 7830,  "precio_particular": 20000},
    "ortodoncia":    {"label": "Ortodoncia",     "ids": [66],         "precio_fonasa": None,  "precio_particular": 30000},
    "psicologia":    {"label": "Psicología",     "ids": [74, 49],     "precio_fonasa": 14420, "precio_particular": 20000},
    "nutricion":     {"label": "Nutrición",      "ids": [52],         "precio_fonasa": 4770,  "precio_particular": 20000},
}


async def get_citas_seguimiento_mes(year: int, month: int, especialidad: str = "kinesiologia") -> list:
    """Retorna las citas del mes para una especialidad recurrente, agrupadas por paciente."""
    import calendar
    from collections import defaultdict

    cfg = SEGUIMIENTO_ESPECIALIDADES.get(especialidad)
    if not cfg:
        return []

    last_day = calendar.monthrange(year, month)[1]
    fecha_ini = f"{year}-{month:02d}-01"
    fecha_fin  = f"{year}-{month:02d}-{last_day:02d}"
    citas_raw = []

    # /citas en Medilink no soporta rango gte/lte por profesional — se consulta día a día
    async with httpx.AsyncClient(timeout=120) as client:
        for id_prof in cfg["ids"]:
            for day in range(1, last_day + 1):
                fecha = f"{year}-{month:02d}-{day:02d}"
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
                    for c in r.json().get("data", []):
                        id_pac = c.get("id_paciente")
                        nombre = (c.get("nombre_paciente") or "").strip()
                        if not id_pac:
                            continue
                        citas_raw.append({
                            "id_prof":         id_prof,
                            "prof_nombre":     PROFESIONALES[id_prof]["nombre"],
                            "fecha":           fecha,
                            "id_paciente":     id_pac,
                            "paciente_nombre": nombre,
                        })
                except Exception as e:
                    log.error("get_citas_seguimiento esp=%s prof=%d %s: %s", especialidad, id_prof, fecha, e)

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
    except Exception:
        return fecha


def valid_rut(rut: str) -> bool:
    """Valida RUT chileno con módulo 11."""
    try:
        rut = rut.replace(".", "").replace("-", "").strip().upper()
        cuerpo, dv = rut[:-1], rut[-1]
        suma = 0
        multiplo = 2
        for c in reversed(cuerpo):
            suma += int(c) * multiplo
            multiplo = multiplo + 1 if multiplo < 7 else 2
        resto = 11 - (suma % 11)
        dv_calc = "0" if resto == 11 else ("K" if resto == 10 else str(resto))
        return dv == dv_calc
    except Exception:
        return False


def clean_rut(rut: str) -> str:
    """Normaliza RUT: '12.345.678-9' → '12345678-9'"""
    rut = rut.replace(".", "").replace(" ", "").strip().upper()
    if "-" not in rut and len(rut) > 1:
        rut = rut[:-1] + "-" + rut[-1]
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
