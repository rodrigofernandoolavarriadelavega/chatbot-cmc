"""
Script para generar un mapa de calor de comunas de pacientes CMC.

Dos fases:
  1. DESCARGA: Extrae citas + datos de pacientes de Medilink y los guarda en SQLite.
     Es incremental: si se interrumpe, retoma desde donde quedó.
  2. MAPA: Lee la SQLite local y genera el HTML interactivo.

Uso:
    cd /Users/rodrigoolavarria/chatbot-cmc
    PYTHONPATH=app python scripts/heatmap_comunas.py download   # Fase 1 (retomable)
    PYTHONPATH=app python scripts/heatmap_comunas.py map        # Fase 2
    PYTHONPATH=app python scripts/heatmap_comunas.py            # Ambas fases
"""
import asyncio
import json
import logging
import os
import sqlite3
import sys
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta

import httpx
from config import MEDILINK_BASE_URL, MEDILINK_TOKEN, MEDILINK_SUCURSAL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

HEADERS = {"Authorization": f"Token {MEDILINK_TOKEN}"}
DB_PATH = "data/heatmap_cache.db"

# ── Rate limiting ──────────────────────────────────────────────────────────
WAIT_BETWEEN_REQUESTS = 1.5  # segundos entre cada request (~40 req/min)


def _q(params: dict) -> str:
    return urllib.parse.quote(json.dumps(params))


def init_db():
    """Crea las tablas de caché si no existen."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS citas_heatmap (
            id INTEGER PRIMARY KEY,
            id_paciente INTEGER,
            id_profesional INTEGER,
            nombre_profesional TEXT,
            fecha TEXT,
            hora_inicio TEXT,
            estado_cita TEXT,
            descargado_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pacientes_heatmap (
            id INTEGER PRIMARY KEY,
            nombre TEXT,
            apellidos TEXT,
            rut TEXT,
            comuna TEXT,
            ciudad TEXT,
            direccion TEXT,
            fecha_nacimiento TEXT,
            sexo TEXT,
            celular TEXT,
            email TEXT,
            descargado_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dias_descargados (
            fecha TEXT PRIMARY KEY,
            total_citas INTEGER,
            descargado_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


# ── FASE 1: Descarga ──────────────────────────────────────────────────────

async def _api_get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> httpx.Response | None:
    """GET con reintentos ante 429."""
    for attempt in range(5):
        try:
            r = await client.get(url, params=params, headers=HEADERS)
            if r.status_code == 429:
                wait = 10.0 * (2 ** attempt)
                log.warning("429 en %s, esperando %.0fs (intento %d/5)", url.split("/")[-1], wait, attempt + 1)
                await asyncio.sleep(wait)
                continue
            return r
        except httpx.RequestError as e:
            log.error("Error de red %s: %s", url, e)
            return None
    log.error("Demasiados 429 en %s, saltando", url)
    return None


async def download_citas_dia(client: httpx.AsyncClient, fecha: str) -> list[dict]:
    """Descarga citas de un día."""
    params = {
        "id_sucursal": {"eq": MEDILINK_SUCURSAL},
        "fecha": {"eq": fecha},
        "estado_anulacion": {"eq": 0},
    }
    r = await _api_get(client, f"{MEDILINK_BASE_URL}/citas", {"q": _q(params)})
    if not r or r.status_code != 200:
        return []
    return r.json().get("data", [])


async def download_paciente(client: httpx.AsyncClient, pac_id: int) -> dict | None:
    """Descarga datos de un paciente."""
    r = await _api_get(client, f"{MEDILINK_BASE_URL}/pacientes/{pac_id}")
    if not r or r.status_code != 200:
        return None
    p = r.json().get("data", {})
    if isinstance(p, list) and p:
        p = p[0]
    return p


async def fase_download(year: int = 2026, month: int = 4):
    """Descarga citas y pacientes de un mes, guardando en SQLite. Retomable."""
    from calendar import monthrange
    init_db()

    _, last_day = monthrange(year, month)
    hoy = datetime.now().date()
    conn = sqlite3.connect(DB_PATH)

    # ── 1. Descargar citas día por día (saltando días ya descargados) ──
    dias_ya = {row[0] for row in conn.execute("SELECT fecha FROM dias_descargados").fetchall()}
    dias_pendientes = []
    for day in range(1, last_day + 1):
        fecha = f"{year}-{month:02d}-{day:02d}"
        fecha_date = datetime.strptime(fecha, "%Y-%m-%d").date()
        if fecha_date > hoy:
            break
        if fecha not in dias_ya:
            dias_pendientes.append(fecha)

    if dias_pendientes:
        log.info("Descargando citas de %d días pendientes...", len(dias_pendientes))
        async with httpx.AsyncClient(timeout=30) as client:
            for fecha in dias_pendientes:
                citas = await download_citas_dia(client, fecha)
                # Guardar en SQLite
                for c in citas:
                    conn.execute("""
                        INSERT OR REPLACE INTO citas_heatmap (id, id_paciente, id_profesional,
                            nombre_profesional, fecha, hora_inicio, estado_cita)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        c["id"], c.get("id_paciente"), c.get("id_profesional"),
                        c.get("nombre_profesional", ""), c.get("fecha", ""),
                        c.get("hora_inicio", ""), c.get("estado_cita", ""),
                    ))
                conn.execute("INSERT OR REPLACE INTO dias_descargados (fecha, total_citas) VALUES (?, ?)",
                             (fecha, len(citas)))
                conn.commit()
                log.info("  %s: %d citas guardadas", fecha, len(citas))
                await asyncio.sleep(WAIT_BETWEEN_REQUESTS)
    else:
        log.info("Todas las citas ya están descargadas.")

    # ── 2. Descargar datos de pacientes (saltando los ya descargados) ──
    pac_ids_en_citas = {row[0] for row in conn.execute(
        "SELECT DISTINCT id_paciente FROM citas_heatmap WHERE id_paciente IS NOT NULL"
    ).fetchall()}
    pac_ids_ya = {row[0] for row in conn.execute("SELECT id FROM pacientes_heatmap").fetchall()}
    pac_pendientes = pac_ids_en_citas - pac_ids_ya

    if pac_pendientes:
        log.info("Descargando datos de %d pacientes pendientes (de %d únicos)...",
                 len(pac_pendientes), len(pac_ids_en_citas))
        pac_list = sorted(pac_pendientes)
        async with httpx.AsyncClient(timeout=15) as client:
            for i, pid in enumerate(pac_list):
                p = await download_paciente(client, pid)
                if p:
                    conn.execute("""
                        INSERT OR REPLACE INTO pacientes_heatmap
                            (id, nombre, apellidos, rut, comuna, ciudad, direccion,
                             fecha_nacimiento, sexo, celular, email)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        p.get("id"), p.get("nombre", ""), p.get("apellidos", ""),
                        p.get("rut", ""), p.get("comuna", ""), p.get("ciudad", ""),
                        p.get("direccion", ""), p.get("fecha_nacimiento", ""),
                        p.get("sexo", ""), p.get("celular", ""), p.get("email", ""),
                    ))
                    conn.commit()
                if (i + 1) % 20 == 0:
                    log.info("  Progreso pacientes: %d/%d", i + 1, len(pac_list))
                await asyncio.sleep(WAIT_BETWEEN_REQUESTS)
        log.info("Descarga de pacientes completada.")
    else:
        log.info("Todos los pacientes ya están descargados.")

    # Resumen
    total_citas = conn.execute("SELECT COUNT(*) FROM citas_heatmap").fetchone()[0]
    total_pacs = conn.execute("SELECT COUNT(*) FROM pacientes_heatmap").fetchone()[0]
    total_dias = conn.execute("SELECT COUNT(*) FROM dias_descargados").fetchone()[0]
    conn.close()
    log.info("=== Resumen caché: %d citas, %d pacientes, %d días ===", total_citas, total_pacs, total_dias)


# ── FASE 2: Generar mapa ──────────────────────────────────────────────────

# Coordenadas aproximadas de comunas de la Provincia de Arauco y alrededores
COMUNA_COORDS = {
    "ARAUCO": (-37.2467, -73.3178),
    "CAÑETE": (-37.8009, -73.3967),
    "CANETE": (-37.8009, -73.3967),
    "CONTULMO": (-38.0131, -73.2292),
    "CURANILAHUE": (-37.4744, -73.3481),
    "LEBU": (-37.6083, -73.6500),
    "LOS ÁLAMOS": (-37.6200, -73.4700),
    "LOS ALAMOS": (-37.6200, -73.4700),
    "TIRÚA": (-38.3333, -73.5000),
    "TIRUA": (-38.3333, -73.5000),
    "CARAMPANGUE": (-37.2650, -73.2800),
    "CONCEPCIÓN": (-36.8201, -73.0444),
    "CONCEPCION": (-36.8201, -73.0444),
    "TALCAHUANO": (-36.7167, -73.1167),
    "HUALPÉN": (-36.7833, -73.0833),
    "HUALPEN": (-36.7833, -73.0833),
    "CHIGUAYANTE": (-36.9167, -73.0167),
    "SAN PEDRO DE LA PAZ": (-36.8500, -73.1167),
    "CORONEL": (-37.0167, -73.1500),
    "LOTA": (-37.0833, -73.1500),
    "PENCO": (-36.7333, -72.9833),
    "TOMÉ": (-36.6167, -72.9500),
    "TOME": (-36.6167, -72.9500),
    "FLORIDA": (-36.8167, -72.6667),
    "HUALQUI": (-36.9667, -72.9333),
    "SANTA JUANA": (-37.1667, -72.9333),
    "LOS ÁNGELES": (-37.4694, -72.3528),
    "LOS ANGELES": (-37.4694, -72.3528),
    "NACIMIENTO": (-37.5000, -72.6667),
    "MULCHÉN": (-37.7167, -72.2333),
    "MULCHEN": (-37.7167, -72.2333),
    "ANGOL": (-37.7833, -72.7167),
    "COLLIPULLI": (-37.9500, -72.4333),
    "CHILLÁN": (-36.6167, -72.1000),
    "CHILLAN": (-36.6167, -72.1000),
    "YUMBEL": (-37.1000, -72.5667),
    "CABRERO": (-37.0333, -72.4000),
    "LAJA": (-37.2833, -72.7167),
    "SAN ROSENDO": (-37.2000, -72.7333),
    "TUCAPEL": (-37.2833, -71.9500),
    "ANTUCO": (-37.3333, -71.6833),
    "QUILLECO": (-37.4833, -71.9833),
    "SANTA BÁRBARA": (-37.6667, -72.0167),
    "SANTA BARBARA": (-37.6667, -72.0167),
    "ALTO BIOBÍO": (-37.8833, -71.5833),
    "ALTO BIOBIO": (-37.8833, -71.5833),
    "NEGRETE": (-37.5833, -72.5333),
    "SANTIAGO": (-33.4489, -70.6693),
    "TEMUCO": (-38.7359, -72.5904),
    "VALDIVIA": (-39.8142, -73.2459),
    "VIÑA DEL MAR": (-33.0153, -71.5500),
    "VALPARAÍSO": (-33.0472, -71.6127),
    "VALPARAISO": (-33.0472, -71.6127),
    "LA SERENA": (-29.9027, -71.2519),
    "RANCAGUA": (-34.1708, -70.7394),
    "TALCA": (-35.4264, -71.6553),
    "LINARES": (-35.8467, -71.5933),
    "OSORNO": (-40.5733, -73.1342),
    "PUERTO MONTT": (-41.4693, -72.9424),
    "SAN CARLOS": (-36.4242, -71.9564),
    "COELEMU": (-36.4833, -72.7000),
    "BULNES": (-36.7333, -72.3000),
    "QUILLÓN": (-36.7333, -72.4667),
    "QUILLON": (-36.7333, -72.4667),
    "CHILLÁN VIEJO": (-36.6333, -72.1333),
    "CHILLAN VIEJO": (-36.6333, -72.1333),
    "SAN PEDRO": (-36.8500, -73.1167),
    "COLICO": (-37.3833, -73.2500),
    "HUECHURABA": (-33.3667, -70.6333),
    "PUENTE ALTO": (-33.6167, -70.5833),
    "MAIPÚ": (-33.5167, -70.7500),
    "MAIPU": (-33.5167, -70.7500),
    "LA FLORIDA": (-33.5167, -70.6000),
    "ÑUÑOA": (-33.4500, -70.6000),
    "NUNOA": (-33.4500, -70.6000),
    "PROVIDENCIA": (-33.4333, -70.6167),
    "LAS CONDES": (-33.4167, -70.5833),
}


def fase_mapa():
    """Lee la SQLite y genera el HTML con mapa de calor."""
    if not os.path.exists(DB_PATH):
        log.error("No existe %s. Ejecuta primero: python scripts/heatmap_comunas.py download", DB_PATH)
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Estadísticas generales
    total_citas = conn.execute("SELECT COUNT(*) FROM citas_heatmap").fetchone()[0]
    pac_ids_unicos = conn.execute("SELECT COUNT(DISTINCT id_paciente) FROM citas_heatmap").fetchone()[0]
    total_pacientes = conn.execute("SELECT COUNT(*) FROM pacientes_heatmap").fetchone()[0]

    # Normalización de comunas con typos / datos sucios
    COMUNA_NORMALIZE = {
        "ARAUVO": "ARAUCO",
        "ARAUCI": "ARAUCO",
        "ARAICO": "ARAUCO",
        "ARUACO": "ARAUCO",
        "LARAQUETE": "ARAUCO",  # Localidad dentro de la comuna de Arauco
        "RAMADILLAS": "ARAUCO",
        "CARAMPANGUE": "ARAUCO",  # Localidad dentro de la comuna de Arauco
        "PUNTA LAVAPIÉ": "ARAUCO",
        "PUNTA LAVAPIE": "ARAUCO",
        "CURANILAGUE": "CURANILAHUE",
        "CURANILAHIE": "CURANILAHUE",
        "LOS ALAMOS": "LOS ALAMOS",
        "LOS ÁLAMOS": "LOS ALAMOS",
        "CANETE": "CAÑETE",
    }

    def normalizar_comuna(c: str) -> str:
        c = c.strip().upper()
        if not c or c.isdigit() or len(c) < 3 or any(x in c for x in ["VOLCAN", "CALLE", "PASAJE", "#"]):
            return ""
        return COMUNA_NORMALIZE.get(c, c)

    # Keywords para detectar localidad dentro de Arauco a partir de la dirección
    LOCALIDAD_KEYWORDS = [
        ("CARAMPANGUE", ["CARAMPANGUE", "VICENTE MILLAN", "VICENTE MILLÁN", "MONSALVE",
                         "CONUMO", "LA MESETA", "PRAT ", "CRUCE NORTE", "LOS MAITENES",
                         "VILLA LA PAZ", "LOS CILOS", "LOS SILOS", "CHILLANCITO",
                         "DUARTE", "PATRIA ", "LOS BOLDOS", "CONQUISTA", "CALLE ESTACION",
                         "MANUEL LUENGO", "MAITEN ", "EL PARRON", "LOS PERALES",
                         "PUNTA CARAMPANGUE", "VILLA ESPERANZA", "RENACER LOS PADRES",
                         "LOS HORCONES", "HORCONES", "PICHILO", "CARIPILUN"]),
        ("LARAQUETE", ["LARAQUETE", "EL PINAR", "VILLA EL BOSQUE", "VILLA VISTA HERMOSA",
                       "BOLDO ", "GONZALO ROJAS", "PIEDRA CRUZ", "LOS LINGUES",
                       "VILLA BOSQUE", "MAÑIO"]),
        ("RAMADILLAS", ["RAMADILLAS", "LOS ARTESANOS", "MOLINO DEL SOL",
                        "LOS PINTORES", "IGNACIO CARRERA PINTO", "JULIO MONTT",
                        "ARTURO PEREZ", "ARTURO PÉREZ", "COSTADO RUTA 160"]),
        ("ARAUCO URBANO", ["ARAUCO", "VILLA PEHUEN", "VILLA DON CARLOS", "PORTAL DEL VALLE",
                           "VILLA LOS TRONCOS", "VILLA RADIATA", "LAS ARAUCARIAS",
                           "VILLA LA PAZ", "VILLA EL MIRADOR", "LAS PEÑAS", "FRESIA",
                           "CAUPOLICAN", "SERRANO", "BLANCO ", "SAN MARTIN",
                           "TUCAPEL", "CALIFORNIA", "COVADONGA", "BOSQUES DE MONTEMAR",
                           "RENE SCHNIER", "ALTO LOS PADRES", "LAS AMAPOLAS",
                           "POBLACION 18", "NUEVA ESPERANZA", "EDUARDO FREI",
                           "VILLA ALTO", "PEDRO AGUIRRE", "HORTALIZAS"]),
        ("TUBUL", ["TUBUL"]),
        ("LLICO", ["LLICO"]),
        ("COLICO", ["COLICO"]),
    ]

    def detectar_localidad(direccion: str, comuna_norm: str) -> str:
        """Detecta la localidad dentro de Arauco a partir de la dirección."""
        if comuna_norm != "ARAUCO":
            return ""
        dir_upper = (direccion or "").upper()
        if not dir_upper or dir_upper == "ARAUCO":
            return "ARAUCO (SIN DETALLE)"
        for localidad, keywords in LOCALIDAD_KEYWORDS:
            for kw in keywords:
                if kw in dir_upper:
                    return localidad
        return "ARAUCO (OTRO)"

    # Contar por comuna
    rows = conn.execute("""
        SELECT p.id, p.comuna, p.ciudad, p.direccion, p.nombre || ' ' || p.apellidos AS nombre_completo
        FROM pacientes_heatmap p
        INNER JOIN (SELECT DISTINCT id_paciente FROM citas_heatmap) c ON c.id_paciente = p.id
    """).fetchall()

    # Citas por paciente (para repartir atenciones por comuna y localidad)
    citas_por_paciente: dict[int, int] = dict(conn.execute(
        "SELECT id_paciente, COUNT(*) FROM citas_heatmap WHERE id_paciente IS NOT NULL GROUP BY id_paciente"
    ).fetchall())

    comuna_counter = Counter()
    localidad_counter = Counter()
    localidad_citas = Counter()
    direccion_samples = {}
    localidad_direcciones = {}  # localidad → [direcciones de ejemplo]
    sin_comuna = 0

    # Keywords para inferir comuna desde dirección cuando comuna está vacía
    DIRECCION_TO_COMUNA = [
        ("CARAMPANGUE", "ARAUCO"), ("CONUMO", "ARAUCO"), ("RAMADILLAS", "ARAUCO"),
        ("LARAQUETE", "ARAUCO"), ("ARAUCO", "ARAUCO"), ("PICHILO", "ARAUCO"),
        ("HORCONES", "ARAUCO"), ("TUBUL", "ARAUCO"), ("LLICO", "ARAUCO"),
        ("CURANILAHUE", "CURANILAHUE"), ("COLICO", "ARAUCO"),
        ("VILLA DON CARLOS", "ARAUCO"), ("VILLA LA PAZ", "ARAUCO"),
        ("MAITENES", "ARAUCO"), ("MESETA", "ARAUCO"), ("MONSALVE", "ARAUCO"),
        ("VICENTE MILLAN", "ARAUCO"), ("VICENTE MILLÁN", "ARAUCO"),
    ]

    def inferir_comuna_desde_direccion(direccion: str) -> str:
        dir_upper = (direccion or "").upper()
        for kw, comuna in DIRECCION_TO_COMUNA:
            if kw in dir_upper:
                return comuna
        return ""

    for row in rows:
        comuna = (row["comuna"] or "").strip()
        direccion = (row["direccion"] or "").strip()
        n_citas_pac = citas_por_paciente.get(row["id"], 0)

        cu = normalizar_comuna(comuna)
        # Si no hay comuna, intentar inferir desde la dirección
        if not cu and direccion:
            cu = inferir_comuna_desde_direccion(direccion)

        if cu:
            comuna_counter[cu] += 1
            # Detectar localidad dentro de Arauco
            loc = detectar_localidad(direccion, cu)
            if loc:
                localidad_counter[loc] += 1
                localidad_citas[loc] += n_citas_pac
                if loc not in localidad_direcciones:
                    localidad_direcciones[loc] = []
                if len(localidad_direcciones[loc]) < 3:
                    localidad_direcciones[loc].append(direccion)
            if direccion:
                if cu not in direccion_samples:
                    direccion_samples[cu] = []
                if len(direccion_samples[cu]) < 5:
                    direccion_samples[cu].append(direccion)
        else:
            sin_comuna += 1

    # Conteo de citas por comuna (aplicando normalización)
    rows_citas = conn.execute("""
        SELECT p.comuna, COUNT(*) as total_citas
        FROM citas_heatmap c
        INNER JOIN pacientes_heatmap p ON p.id = c.id_paciente
        WHERE p.comuna IS NOT NULL AND p.comuna != ''
        GROUP BY p.comuna
    """).fetchall()
    citas_por_comuna: Counter = Counter()
    for row in rows_citas:
        cu = normalizar_comuna(row["comuna"])
        if cu:
            citas_por_comuna[cu] += row["total_citas"]

    conn.close()

    total_con_comuna = sum(comuna_counter.values())

    # Imprimir resumen
    print("\n" + "=" * 70)
    print("MAPA DE CALOR — COMUNAS DE PACIENTES CMC — ABRIL 2026")
    print("=" * 70)
    print(f"\nTotal citas:              {total_citas}")
    print(f"Pacientes únicos:         {pac_ids_unicos}")
    print(f"Con datos descargados:    {total_pacientes}")
    print(f"Con comuna registrada:    {total_con_comuna}")
    print(f"Sin comuna:               {sin_comuna}")

    print(f"\n{'COMUNA':<30} {'PAC.':>6} {'%':>7} {'CITAS':>7}")
    print("-" * 55)
    for comuna, count in comuna_counter.most_common():
        pct = count / total_con_comuna * 100 if total_con_comuna else 0
        nc = citas_por_comuna.get(comuna, 0)
        bar = "█" * max(1, int(pct / 2))
        print(f"{comuna:<30} {count:>6} {pct:>6.1f}% {nc:>7}  {bar}")

    if localidad_counter:
        total_arauco = comuna_counter.get("ARAUCO", 0)
        print(f"\n{'LOCALIDAD (ARAUCO)':<30} {'PAC.':>6} {'% ARAUCO':>9}")
        print("-" * 50)
        for loc, count in localidad_counter.most_common():
            pct = count / total_arauco * 100 if total_arauco else 0
            bar = "█" * max(1, int(pct / 2))
            print(f"{loc:<30} {count:>6} {pct:>8.1f}%  {bar}")

    # Datos para el HTML
    total_arauco = comuna_counter.get("ARAUCO", 0)
    data = {
        "periodo": "Abril 2026",
        "total_citas": total_citas,
        "pacientes_unicos": pac_ids_unicos,
        "con_comuna": total_con_comuna,
        "sin_comuna": sin_comuna,
        "comunas": [
            {
                "comuna": comuna,
                "pacientes": count,
                "porcentaje": round(count / total_con_comuna * 100, 1) if total_con_comuna else 0,
                "citas": citas_por_comuna.get(comuna, 0),
                "direcciones_ejemplo": direccion_samples.get(comuna, []),
            }
            for comuna, count in comuna_counter.most_common()
        ],
        "localidades_arauco": [
            {
                "localidad": loc,
                "pacientes": count,
                "citas": localidad_citas.get(loc, 0),
                "porcentaje": round(count / total_arauco * 100, 1) if total_arauco else 0,
                "direcciones_ejemplo": localidad_direcciones.get(loc, []),
            }
            for loc, count in localidad_counter.most_common()
        ],
    }

    # Guardar JSON
    json_path = "data/heatmap_abril_2026.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    generate_html(data)


# Coordenadas de localidades dentro de la comuna de Arauco
LOCALIDAD_COORDS = {
    "CARAMPANGUE": (-37.2650, -73.2800),
    "LARAQUETE": (-37.1700, -73.1833),
    "RAMADILLAS": (-37.2200, -73.2100),
    "ARAUCO URBANO": (-37.2467, -73.3178),
    "TUBUL": (-37.2300, -73.4400),
    "LLICO": (-37.1950, -73.5650),
    "COLICO": (-37.3833, -73.2500),
    "ARAUCO (SIN DETALLE)": (-37.2467, -73.3178),
    "ARAUCO (OTRO)": (-37.2467, -73.3178),
}


def generate_html(data: dict):
    """Genera un HTML interactivo con mapa de calor usando Leaflet."""
    comunas_js = []
    sin_coords = []
    for item in data["comunas"]:
        comuna = item["comuna"]
        coords = COMUNA_COORDS.get(comuna)
        if coords:
            comunas_js.append({
                "comuna": comuna,
                "lat": coords[0],
                "lng": coords[1],
                "pacientes": item["pacientes"],
                "citas": item.get("citas", 0),
                "porcentaje": item["porcentaje"],
                "direcciones": item.get("direcciones_ejemplo", []),
            })
        else:
            sin_coords.append(comuna)

    if sin_coords:
        log.warning("Comunas sin coordenadas (no se mostrarán en mapa): %s", sin_coords)

    # Preparar datos de localidades de Arauco
    localidades_js = []
    for item in data.get("localidades_arauco", []):
        loc = item["localidad"]
        coords = LOCALIDAD_COORDS.get(loc)
        if coords:
            localidades_js.append({
                "localidad": loc,
                "lat": coords[0],
                "lng": coords[1],
                "pacientes": item["pacientes"],
                "porcentaje": item["porcentaje"],
                "direcciones": item.get("direcciones_ejemplo", []),
            })

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mapa de Calor — Pacientes CMC — {data['periodo']}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; }}
  .map-container {{ width: 100%; height: 55vh; border-radius: 12px; margin: 16px auto; max-width: 1200px; }}
  #map {{ width: 100%; height: 100%; border-radius: 12px; }}
  #map2 {{ width: 100%; height: 100%; border-radius: 12px; }}
  .section-title {{ text-align: center; margin: 30px 16px 10px; font-size: 1.3rem; color: #f59e0b; }}
  .header {{ text-align: center; padding: 20px 16px 0; }}
  .header h1 {{ font-size: 1.6rem; color: #38bdf8; }}
  .header p {{ color: #94a3b8; margin-top: 4px; }}
  .stats {{ display: flex; gap: 16px; justify-content: center; flex-wrap: wrap; margin: 16px; }}
  .stat-card {{ background: #1e293b; border-radius: 10px; padding: 14px 24px; text-align: center; min-width: 130px; }}
  .stat-card .num {{ font-size: 1.8rem; font-weight: 700; color: #38bdf8; }}
  .stat-card .label {{ font-size: 0.8rem; color: #94a3b8; margin-top: 2px; }}
  .table-container {{ max-width: 950px; margin: 20px auto; padding: 0 16px; }}
  table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 10px; overflow: hidden; }}
  th {{ background: #334155; color: #38bdf8; padding: 10px 14px; text-align: left; font-size: 0.85rem; }}
  td {{ padding: 8px 14px; border-top: 1px solid #334155; font-size: 0.85rem; }}
  tr:hover {{ background: #334155; }}
  .bar {{ height: 8px; background: #38bdf8; border-radius: 4px; display: inline-block; vertical-align: middle; }}
  .bar-bg {{ height: 8px; background: #334155; border-radius: 4px; width: 120px; display: inline-block; vertical-align: middle; }}
  .legend {{ max-width: 950px; margin: 10px auto; padding: 0 16px 20px; color: #64748b; font-size: 0.75rem; text-align: center; }}
  .sin-coords {{ background: #1e293b; border-radius: 10px; padding: 12px 20px; max-width: 950px; margin: 12px auto; }}
  .sin-coords h3 {{ color: #f59e0b; font-size: 0.9rem; margin-bottom: 6px; }}
  .sin-coords p {{ color: #94a3b8; font-size: 0.8rem; }}
</style>
</head>
<body>

<div class="header">
  <h1>Mapa de Calor — Pacientes CMC</h1>
  <p>{data['periodo']} · Centro Medico Carampangue</p>
</div>

<div class="stats">
  <div class="stat-card"><div class="num">{data['total_citas']}</div><div class="label">Citas totales</div></div>
  <div class="stat-card"><div class="num">{data['pacientes_unicos']}</div><div class="label">Pacientes unicos</div></div>
  <div class="stat-card"><div class="num">{data['con_comuna']}</div><div class="label">Con comuna</div></div>
  <div class="stat-card"><div class="num">{data['sin_comuna']}</div><div class="label">Sin comuna</div></div>
</div>

<div class="section-title">Mapa por Comunas</div>
<div class="map-container"><div id="map"></div></div>

<div class="table-container">
<table>
  <thead>
    <tr><th>#</th><th>Comuna</th><th>Pacientes</th><th>Citas</th><th>%</th><th>Distribucion</th></tr>
  </thead>
  <tbody id="tabla-comunas"></tbody>
</table>
</div>

{"<div class='sin-coords'><h3>Comunas sin coordenadas (no aparecen en mapa)</h3><p>" + ", ".join(sin_coords) + "</p></div>" if sin_coords else ""}

<div class="section-title">Localidades dentro de Arauco</div>
<div class="map-container"><div id="map2"></div></div>

<div class="table-container">
<table>
  <thead>
    <tr><th>#</th><th>Localidad</th><th>Pacientes</th><th>% de Arauco</th><th>Distribucion</th></tr>
  </thead>
  <tbody id="tabla-localidades"></tbody>
</table>
</div>

<div class="legend">
  Datos extraidos de Medilink. Las coordenadas son aproximadas al centro de cada comuna.
  Pacientes sin comuna registrada no se incluyen en la distribucion.
</div>

<script>
const comunas = {json.dumps(comunas_js, ensure_ascii=False)};
const sinCoords = {json.dumps([{"comuna": c["comuna"], "pacientes": c["pacientes"], "porcentaje": c["porcentaje"], "citas": c.get("citas", 0)} for c in data["comunas"] if c["comuna"] in sin_coords], ensure_ascii=False)};

// Mapa centrado en Carampangue
const map = L.map('map').setView([-37.27, -73.28], 10);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '&copy; OpenStreetMap',
  maxZoom: 18,
}}).addTo(map);

// Heat layer
const maxPac = Math.max(...comunas.map(c => c.pacientes), 1);
const heatData = [];
comunas.forEach(c => {{
  const intensity = c.pacientes / maxPac;
  heatData.push([c.lat, c.lng, intensity]);
}});

L.heatLayer(heatData, {{
  radius: 35,
  blur: 25,
  maxZoom: 13,
  max: 1.0,
  gradient: {{0.2: '#2563eb', 0.4: '#38bdf8', 0.6: '#fbbf24', 0.8: '#f97316', 1.0: '#ef4444'}},
}}).addTo(map);

// Marcadores circulares con popup
comunas.forEach(c => {{
  const radius = Math.max(8, Math.min(30, Math.sqrt(c.pacientes) * 6));
  const circle = L.circleMarker([c.lat, c.lng], {{
    radius: radius,
    fillColor: '#38bdf8',
    color: '#1e293b',
    weight: 2,
    opacity: 0.9,
    fillOpacity: 0.6,
  }}).addTo(map);

  let popup = `<b style="font-size:14px">${{c.comuna}}</b><br>
    <b>${{c.pacientes}}</b> pacientes (${{c.porcentaje}}%)<br>
    <b>${{c.citas}}</b> citas en el mes`;
  if (c.direcciones && c.direcciones.length > 0) {{
    popup += `<br><br><small><b>Direcciones ejemplo:</b><br>${{c.direcciones.join('<br>')}}</small>`;
  }}
  circle.bindPopup(popup);
}});

// Tabla
const tbody = document.getElementById('tabla-comunas');
const allComunas = [...comunas.map(c => ({{...c}})), ...sinCoords];
allComunas.sort((a, b) => b.pacientes - a.pacientes);
const totalPac = allComunas.reduce((s, c) => s + c.pacientes, 0);
allComunas.forEach((c, i) => {{
  const pct = totalPac ? (c.pacientes / totalPac * 100).toFixed(1) : 0;
  const barW = totalPac ? Math.max(2, c.pacientes / allComunas[0].pacientes * 120) : 0;
  const row = document.createElement('tr');
  row.innerHTML = `<td>${{i+1}}</td><td>${{c.comuna}}</td><td>${{c.pacientes}}</td><td>${{c.citas || '-'}}</td><td>${{pct}}%</td>
    <td><div class="bar-bg"><div class="bar" style="width:${{barW}}px"></div></div></td>`;
  tbody.appendChild(row);
}});

// Auto-fit bounds
if (comunas.length > 0) {{
  const bounds = comunas.map(c => [c.lat, c.lng]);
  map.fitBounds(bounds, {{ padding: [30, 30] }});
}}

// ── Mapa 2: Localidades de Arauco ──
const localidades = {json.dumps(localidades_js, ensure_ascii=False)};

const map2 = L.map('map2').setView([-37.24, -73.30], 12);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '&copy; OpenStreetMap',
  maxZoom: 18,
}}).addTo(map2);

const colorsLoc = ['#ef4444','#f97316','#fbbf24','#38bdf8','#2563eb','#8b5cf6','#ec4899','#10b981'];
const maxLocPac = Math.max(...localidades.map(l => l.pacientes), 1);

// Heat layer localidades
const heatDataLoc = [];
localidades.forEach(l => {{
  const intensity = l.pacientes / maxLocPac;
  heatDataLoc.push([l.lat, l.lng, intensity]);
}});
L.heatLayer(heatDataLoc, {{
  radius: 30,
  blur: 20,
  maxZoom: 14,
  max: 1.0,
  gradient: {{0.2: '#2563eb', 0.4: '#38bdf8', 0.6: '#fbbf24', 0.8: '#f97316', 1.0: '#ef4444'}},
}}).addTo(map2);

localidades.forEach((l, i) => {{
  const radius = Math.max(10, Math.min(35, Math.sqrt(l.pacientes) * 5));
  const color = colorsLoc[i % colorsLoc.length];
  const circle = L.circleMarker([l.lat, l.lng], {{
    radius: radius,
    fillColor: color,
    color: '#1e293b',
    weight: 2,
    opacity: 0.9,
    fillOpacity: 0.65,
  }}).addTo(map2);

  // Label permanente
  const label = L.divIcon({{
    className: '',
    html: `<div style="color:#fff;font-size:11px;font-weight:700;text-shadow:1px 1px 3px #000;white-space:nowrap">${{l.localidad}} (${{l.pacientes}})</div>`,
    iconSize: [120, 20],
    iconAnchor: [60, -radius - 4],
  }});
  L.marker([l.lat, l.lng], {{ icon: label, interactive: false }}).addTo(map2);

  let popup = `<b style="font-size:14px">${{l.localidad}}</b><br>
    <b>${{l.pacientes}}</b> pacientes (${{l.porcentaje}}% de Arauco)`;
  if (l.direcciones && l.direcciones.length > 0) {{
    popup += `<br><br><small><b>Ej:</b><br>${{l.direcciones.join('<br>')}}</small>`;
  }}
  circle.bindPopup(popup);
}});

if (localidades.length > 0) {{
  const boundsLoc = localidades.map(l => [l.lat, l.lng]);
  map2.fitBounds(boundsLoc, {{ padding: [40, 40] }});
}}

// Tabla localidades
const tbodyLoc = document.getElementById('tabla-localidades');
localidades.sort((a, b) => b.pacientes - a.pacientes);
const totalLocPac = localidades.reduce((s, l) => s + l.pacientes, 0);
localidades.forEach((l, i) => {{
  const barW = totalLocPac ? Math.max(2, l.pacientes / localidades[0].pacientes * 120) : 0;
  const row = document.createElement('tr');
  row.innerHTML = `<td>${{i+1}}</td><td>${{l.localidad}}</td><td>${{l.pacientes}}</td><td>${{l.porcentaje}}%</td>
    <td><div class="bar-bg"><div class="bar" style="width:${{barW}}px;background:${{colorsLoc[i % colorsLoc.length]}}"></div></div></td>`;
  tbodyLoc.appendChild(row);
}});
</script>
</body>
</html>"""

    html_path = "templates/heatmap_comunas.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("Mapa HTML generado en %s", html_path)
    print(f"\nMapa generado: {html_path}")
    print(f"Abrir con: open {html_path}")


# ── Entrypoint ─────────────────────────────────────────────────────────────

async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    # Opcional: año y mes como 3er y 4to argumento  (ej: download 2026 3)
    year = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    month = int(sys.argv[3]) if len(sys.argv) > 3 else datetime.now().month

    if cmd in ("download", "all"):
        log.info("=== FASE 1: Descarga de datos de Medilink (%d-%02d) ===", year, month)
        await fase_download(year, month)

    if cmd in ("map", "all"):
        log.info("=== FASE 2: Generacion del mapa de calor ===")
        fase_mapa()


if __name__ == "__main__":
    asyncio.run(main())
