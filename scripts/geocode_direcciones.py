"""
Geocodifica las direcciones de pacientes usando Nominatim (OpenStreetMap).
Guarda resultados en SQLite para no repetir llamadas.

Uso:
    cd /Users/rodrigoolavarria/chatbot-cmc
    PYTHONPATH=app python scripts/geocode_direcciones.py
"""
import asyncio
import json
import logging
import sqlite3
import urllib.parse
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

DB_PATH = "data/heatmap_cache.db"

# Nominatim requiere un User-Agent identificable
NOMINATIM_UA = "CMC-Heatmap/1.0 (Centro Medico Carampangue)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


def init_geocode_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS geocode_cache (
            direccion_key TEXT PRIMARY KEY,
            lat REAL,
            lng REAL,
            display_name TEXT,
            source TEXT DEFAULT 'nominatim',
            geocoded_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def get_cached(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT lat, lng FROM geocode_cache WHERE direccion_key = ?", (key,)).fetchone()
    return row


def save_cache(conn: sqlite3.Connection, key: str, lat: float, lng: float, display: str, source: str = "nominatim"):
    conn.execute("""
        INSERT OR REPLACE INTO geocode_cache (direccion_key, lat, lng, display_name, source)
        VALUES (?, ?, ?, ?, ?)
    """, (key, lat, lng, display, source))
    conn.commit()


async def geocode_nominatim(client: httpx.AsyncClient, address: str, hint_city: str = "Arauco") -> dict | None:
    """Geocodifica una dirección usando Nominatim."""
    # Intentar con la dirección completa + hint de ciudad + Chile
    queries = [
        f"{address}, {hint_city}, Biobio, Chile",
        f"{address}, Chile",
    ]
    for q in queries:
        try:
            r = await client.get(NOMINATIM_URL, params={
                "q": q,
                "format": "json",
                "limit": 1,
                "countrycodes": "cl",
            }, headers={"User-Agent": NOMINATIM_UA})
            if r.status_code == 200:
                data = r.json()
                if data:
                    return {
                        "lat": float(data[0]["lat"]),
                        "lng": float(data[0]["lon"]),
                        "display": data[0].get("display_name", ""),
                    }
        except (httpx.RequestError, ValueError, KeyError):
            pass
        await asyncio.sleep(1.1)  # Nominatim rate limit: 1 req/s
    return None


# Coordenadas conocidas de sectores/calles de Arauco para fallback manual
KNOWN_LOCATIONS = {
    "CONUMO ALTO": (-37.2580, -73.2750),
    "CONUMO BAJO": (-37.2620, -73.2820),
    "CONUMO": (-37.2600, -73.2780),
    "LA MESETA": (-37.2700, -73.2700),
    "CRUCE NORTE": (-37.2550, -73.2750),
    "VICENTE MILLAN": (-37.2640, -73.2810),
    "VICENTE MILLÁN": (-37.2640, -73.2810),
    "MONSALVE": (-37.2650, -73.2800),
    "MANUEL LUENGO": (-37.2645, -73.2795),
    "VILLA LA PAZ": (-37.2660, -73.2830),
    "VILLA ESPERANZA": (-37.2655, -73.2790),
    "LOS HORCONES": (-37.2850, -73.2600),
    "HORCONES": (-37.2850, -73.2600),
    "HORCONES CORDILLERA": (-37.2900, -73.2550),
    "PICHILO": (-37.2950, -73.2650),
    "CHILLANCITO": (-37.2750, -73.2650),
    "LOS BOLDOS": (-37.2680, -73.2780),
    "LOS CILOS": (-37.2670, -73.2810),
    "LOS SILOS": (-37.2670, -73.2810),
    "EL PARRON": (-37.2630, -73.2760),
    "LOS CANELOS": (-37.2610, -73.2740),
    "LOS MAITENES": (-37.2560, -73.2730),
    "PUNTA CARAMPANGUE": (-37.2500, -73.2900),
    "CARAMPANGUE VIEJO": (-37.2660, -73.2850),
    # Laraquete
    "EL PINAR": (-37.1650, -73.1800),
    "VILLA EL BOSQUE": (-37.1680, -73.1850),
    "VILLA VISTA HERMOSA": (-37.1720, -73.1870),
    "PLAYA NORTE": (-37.1600, -73.1750),
    "EL BOLDO": (-37.1750, -73.1900),
    "POBLACION SAN PEDRO": (-37.1690, -73.1830),
    # Ramadillas
    "LOS ARTESANOS": (-37.2210, -73.2050),
    "MOLINO DEL SOL": (-37.2180, -73.2080),
    "IGNACIO CARRERA PINTO": (-37.2200, -73.2100),
    "JULIO MONTT": (-37.2190, -73.2090),
    "LOS PINTORES": (-37.2220, -73.2070),
    # Arauco urbano
    "VILLA PEHUEN": (-37.2400, -73.3100),
    "VILLA DON CARLOS": (-37.2430, -73.3050),
    "VILLA RADIATA": (-37.2350, -73.3000),
    "VILLA LOS TRONCOS": (-37.2380, -73.3020),
    "VILLA LAS ARAUCARIAS": (-37.2420, -73.3080),
    "BOSQUES DE MONTEMAR": (-37.2370, -73.3040),
    "PORTAL DEL VALLE": (-37.2440, -73.3060),
    "VILLA EL MIRADOR": (-37.2450, -73.3070),
    "LAS PEÑAS": (-37.2500, -73.3150),
    "POBLACION 18": (-37.2480, -73.3130),
    "ALTO LOS PADRES": (-37.2460, -73.3090),
    # Tubul
    "TUBUL": (-37.2300, -73.4400),
    "SAN JOSE": (-37.2280, -73.4350),
    "SAN JOSE DE COLICO": (-37.2270, -73.4350),
    "SAN JOSÉ DE COLICO": (-37.2270, -73.4350),
    # Carampangue calles adicionales
    "REPUBLICA": (-37.2640, -73.2800),
    "CARMAPANGUE": (-37.2650, -73.2800),  # typo frecuente
    "CARAMPANGUE": (-37.2650, -73.2800),
    "EL PARRON": (-37.2630, -73.2760),
    "EL PARRÓN": (-37.2630, -73.2760),
    "MAQUEHUA": (-37.2600, -73.2850),
    "CALLEJON MAQUEHUA": (-37.2600, -73.2850),
    "COVADONGA": (-37.2580, -73.2770),
    "LA QUINTA": (-37.2580, -73.2770),
    "CALLE MAITEN": (-37.2645, -73.2810),
    "VILLA AMANECER": (-37.2670, -73.2840),
    "LAS HORTENSIAS": (-37.2660, -73.2790),
    "PASAJE ZARATE": (-37.2635, -73.2805),
    "ZÁRATE": (-37.2635, -73.2805),
    "HORCONOES": (-37.2850, -73.2600),  # typo de Horcones
    "10 DE JULIO": (-37.2200, -73.2070),
    "RAMADILLAS": (-37.2200, -73.2080),
    # Laraquete calles adicionales
    "LOS MAÑIOS": (-37.1680, -73.1850),
    "PEDRO PRADO": (-37.1670, -73.1830),
    "LAS ARAUCARIAS": (-37.1700, -73.1860),
    "LAGO CALAFQUEN": (-37.1720, -73.1870),
    "POBLACIÓN EL PINAR": (-37.1650, -73.1800),
    "POBLACION EL PINAR": (-37.1650, -73.1800),
    # Volcan Antuco / Villa Don Carlos
    "VOLCAN ANTUCO": (-37.2430, -73.3050),
    "VOLCÁN ANTUCO": (-37.2430, -73.3050),
    # Curanilahue
    "AV.LAS ESTRELLA": (-37.4744, -73.3481),
    "AV. LAS ESTRELLA": (-37.4744, -73.3481),
    "JUAN BENITEZ": (-37.4750, -73.3490),
    "ELEUTERIO RAMIREZ": (-37.4740, -73.3475),
    # Lota
    "POLVORIN": (-37.0850, -73.1500),
    # Coronel
    "SCHWAGER": (-37.0300, -73.1550),
    "LAGUNILLAS": (-37.0400, -73.1450),
}

import hashlib

_JITTER_DEG = 0.003  # ~330m por eje — dispersa el cluster artificial al zoom out


def _deterministic_jitter(key: str) -> tuple[float, float]:
    """Offset reproducible basado en hash de la dirección.
    Direcciones distintas siempre caen en posiciones distintas; misma dirección
    siempre cae en la misma posición. Sin esto, los matches de KNOWN_LOCATIONS
    se apilan visualmente y crean clusters falsos."""
    h = hashlib.sha1(key.encode("utf-8")).digest()
    a = int.from_bytes(h[0:4], "big") / 0xFFFFFFFF
    b = int.from_bytes(h[4:8], "big") / 0xFFFFFFFF
    return ((a * 2 - 1) * _JITTER_DEG, (b * 2 - 1) * _JITTER_DEG)


def fallback_coords(direccion: str, localidad: str) -> tuple[float, float] | None:
    """Intenta obtener coordenadas desde el diccionario de ubicaciones conocidas."""
    dir_upper = direccion.upper()
    for name, coords in KNOWN_LOCATIONS.items():
        if name in dir_upper:
            d_lat, d_lng = _deterministic_jitter(direccion)
            return (coords[0] + d_lat, coords[1] + d_lng)
    return None


async def main():
    init_geocode_table()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Obtener todas las direcciones de pacientes con citas
    rows = conn.execute("""
        SELECT p.id, p.nombre, p.apellidos, p.comuna, p.direccion,
               COUNT(c.id) as num_citas
        FROM pacientes_heatmap p
        INNER JOIN citas_heatmap c ON c.id_paciente = p.id
        WHERE p.direccion IS NOT NULL AND TRIM(p.direccion) != ''
        GROUP BY p.id
        ORDER BY p.direccion
    """).fetchall()

    log.info("Pacientes con dirección: %d", len(rows))

    # Obtener detalle de atenciones por paciente (profesional + fecha)
    atenciones_rows = conn.execute("""
        SELECT c.id_paciente, c.nombre_profesional, c.fecha, c.hora_inicio
        FROM citas_heatmap c
        WHERE c.id_paciente IS NOT NULL
        ORDER BY c.fecha DESC, c.hora_inicio DESC
    """).fetchall()

    # Indexar atenciones por paciente
    atenciones_por_pac = {}
    for a in atenciones_rows:
        pid = a["id_paciente"]
        if pid not in atenciones_por_pac:
            atenciones_por_pac[pid] = []
        atenciones_por_pac[pid].append({
            "prof": a["nombre_profesional"].strip(),
            "fecha": a["fecha"],
            "hora": a["hora_inicio"][:5] if a["hora_inicio"] else "",
        })

    # Agrupar por dirección normalizada para no geocodificar duplicados
    dir_groups = {}
    for r in rows:
        key = r["direccion"].strip().upper()
        if key not in dir_groups:
            dir_groups[key] = {"direccion": r["direccion"], "comuna": r["comuna"] or "", "pacientes": []}
        pac_id = r["id"]
        dir_groups[key]["pacientes"].append({
            "nombre": f"{r['nombre']} {r['apellidos']}".strip(),
            "citas": r["num_citas"],
            "atenciones": atenciones_por_pac.get(pac_id, [])[:8],
        })

    log.info("Direcciones únicas: %d", len(dir_groups))

    # Fase 1: Geocodificar con Nominatim (solo las que no estén en caché)
    pendientes = []
    ya_cacheados = 0
    for key, info in dir_groups.items():
        cached = get_cached(conn, key)
        if cached:
            ya_cacheados += 1
        else:
            pendientes.append((key, info))

    log.info("Ya cacheados: %d, pendientes de geocodificar: %d", ya_cacheados, len(pendientes))

    if pendientes:
        geocoded = 0
        fallback_used = 0
        not_found = 0

        async with httpx.AsyncClient(timeout=10) as client:
            for i, (key, info) in enumerate(pendientes):
                comuna = info["comuna"].strip() or "Arauco"
                result = await geocode_nominatim(client, info["direccion"], comuna)

                if result:
                    save_cache(conn, key, result["lat"], result["lng"], result["display"], "nominatim")
                    geocoded += 1
                else:
                    # Fallback a coordenadas conocidas
                    fb = fallback_coords(info["direccion"], "")
                    if fb:
                        save_cache(conn, key, fb[0], fb[1], f"fallback: {info['direccion']}", "fallback")
                        fallback_used += 1
                    else:
                        not_found += 1

                if (i + 1) % 20 == 0:
                    log.info("  Progreso: %d/%d (geo: %d, fallback: %d, no: %d)",
                             i + 1, len(pendientes), geocoded, fallback_used, not_found)

        log.info("Geocodificación: %d nominatim, %d fallback, %d sin coordenadas", geocoded, fallback_used, not_found)

    # Fase 2: Generar mapa con puntos individuales
    points = []
    sin_geo = 0
    for key, info in dir_groups.items():
        cached = get_cached(conn, key)
        if cached:
            lat, lng = cached
            # Verificar que las coordenadas estén en rango razonable (Chile, Biobío)
            if -39.0 < lat < -36.0 and -74.0 < lng < -71.0:
                total_citas = sum(p["citas"] for p in info["pacientes"])
                # Armar detalle por paciente con sus atenciones
                detalle = []
                for p in info["pacientes"][:8]:
                    det = {"n": p["nombre"], "c": p["citas"], "a": []}
                    for at in p.get("atenciones", [])[:5]:
                        # Formato fecha corta: "11/04 10:30 — Dra. Castillo"
                        f = at["fecha"]
                        if f and len(f) >= 10:
                            f = f[8:10] + "/" + f[5:7]
                        prof = at["prof"].split()
                        # Abreviar: primer nombre + primer apellido
                        prof_short = " ".join(prof[:2]) if len(prof) >= 2 else at["prof"]
                        det["a"].append(f"{f} {at['hora']} — {prof_short}")
                    detalle.append(det)
                points.append({
                    "lat": lat,
                    "lng": lng,
                    "direccion": info["direccion"],
                    "comuna": info["comuna"],
                    "pacientes": len(info["pacientes"]),
                    "nombres": [p["nombre"] for p in info["pacientes"]][:5],
                    "citas": total_citas,
                    "detalle": detalle,
                })
            else:
                sin_geo += 1
        else:
            sin_geo += 1

    conn.close()
    log.info("Puntos para el mapa: %d, sin coordenadas válidas: %d", len(points), sin_geo)

    generate_address_map(points)
    update_dashboard_pts(points)


def update_dashboard_pts(points: list[dict]):
    """Actualiza ptsData en dashboard.html con datos enriquecidos (detalle pacientes)."""
    import re
    dashboard_path = Path(__file__).parent.parent / "templates" / "dashboard.html"
    if not dashboard_path.exists():
        log.warning("dashboard.html no encontrado, omitiendo update")
        return

    content = dashboard_path.read_text(encoding="utf-8")

    # Generar ptsData con formato del dashboard + detalle nuevo
    pts_dash = []
    for p in points:
        pts_dash.append({
            "lat": round(p["lat"], 3),
            "lng": round(p["lng"], 3),
            "d": p["direccion"].strip(),
            "p": p["pacientes"],
            "c": p["citas"],
            "det": p.get("detalle", []),
        })

    pts_json = json.dumps(pts_dash, ensure_ascii=False)
    new_line = f"const ptsData = {pts_json};"

    # Reemplazar la línea existente de ptsData
    pattern = r"const ptsData = \[.*?\];"
    if re.search(pattern, content, re.DOTALL):
        content = re.sub(pattern, new_line, content, count=1, flags=re.DOTALL)

        # Actualizar popups para mostrar detalle de pacientes
        # Clusters popup
        old_popup_cluster = '`<b>${p.d}</b><br><b>${p.p}</b> pac, <b>${p.c}</b> citas`'
        new_popup_cluster = 'buildDashPopup(p)'
        content = content.replace(old_popup_cluster, new_popup_cluster)

        # Points popup
        old_popup_points = '`<b>${p.d}</b><br>${p.p} pac, ${p.c} citas`'
        new_popup_points = 'buildDashPopup(p)'
        content = content.replace(old_popup_points, new_popup_points)

        # Insertar función buildDashPopup y buildDashTooltip antes de initMaps
        if 'function buildDashPopup' not in content:
            helper_js = """
function buildDashTooltip(p) {
  let h = '<b>' + p.d + '</b><br>' + p.p + ' pac, ' + p.c + ' citas';
  if (p.det && p.det.length > 0) {
    h += '<br><span style="color:#94a3b8;font-size:12px">';
    p.det.slice(0, 3).forEach(function(d) {
      h += d.n + (d.a && d.a[0] ? ' — ' + d.a[0].split(' — ')[1] : '') + '<br>';
    });
    if (p.det.length > 3) h += '... +' + (p.det.length - 3) + ' más';
    h += '</span>';
  }
  return h;
}

function buildDashPopup(p) {
  let h = '<div style="max-width:320px;font-size:13px"><b style="font-size:14px">' + p.d + '</b>';
  h += '<br><span style="color:#64748b">' + p.p + ' paciente' + (p.p > 1 ? 's' : '') + ' · ' + p.c + ' cita' + (p.c > 1 ? 's' : '') + '</span>';
  if (p.det && p.det.length > 0) {
    p.det.forEach(function(d) {
      h += '<div style="margin-top:8px;padding-top:6px;border-top:1px solid #e2e8f0">';
      h += '<b style="color:#1e293b">' + d.n + '</b> <span style="color:#94a3b8">(' + d.c + ' cita' + (d.c > 1 ? 's' : '') + ')</span>';
      if (d.a && d.a.length > 0) {
        d.a.forEach(function(at) {
          var parts = at.split(' — ');
          h += '<div style="padding-left:10px;font-size:12px;color:#475569">';
          h += '<span style="color:#3b82f6">' + (parts[0] || '') + '</span>';
          if (parts[1]) h += ' — <span style="color:#8b5cf6">' + parts[1] + '</span>';
          h += '</div>';
        });
      }
      h += '</div>';
    });
  }
  h += '</div>';
  return h;
}

"""
            content = content.replace('function initMaps() {', helper_js + 'function initMaps() {')

        # Add bindTooltip where we have clusters and points
        # For clusters: add tooltip after popup
        old_cluster_bind = ".bindPopup(buildDashPopup(p));\n      dirClusters"
        new_cluster_bind = ".bindTooltip(buildDashTooltip(p), {sticky:true, direction:'top', offset:[0,-10]}).bindPopup(buildDashPopup(p), {maxWidth:360, maxHeight:400});\n      dirClusters"
        content = content.replace(old_cluster_bind, new_cluster_bind)

        # For points: add tooltip after popup
        old_points_bind = ".bindPopup(buildDashPopup(p));\n      dirPoints"
        new_points_bind = ".bindTooltip(buildDashTooltip(p), {sticky:true, direction:'top', offset:[0,-10]}).bindPopup(buildDashPopup(p), {maxWidth:360, maxHeight:400});\n      dirPoints"
        content = content.replace(old_points_bind, new_points_bind)

        dashboard_path.write_text(content, encoding="utf-8")
        log.info("dashboard.html actualizado con %d puntos enriquecidos", len(pts_dash))
    else:
        log.warning("No se encontró ptsData en dashboard.html")


def generate_address_map(points: list[dict]):
    """Genera un HTML con mapa de direcciones exactas, tooltip hover y popup click."""
    total_dirs = len(points)
    total_pacs = sum(p['pacientes'] for p in points)
    total_citas = sum(p['citas'] for p in points)

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mapa de Direcciones — Pacientes CMC — Abril 2026</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; }}
  .header {{ text-align: center; padding: 20px 16px 10px; }}
  .header h1 {{ font-size: 1.5rem; color: #38bdf8; }}
  .header p {{ color: #94a3b8; margin-top: 4px; font-size: 0.9rem; }}
  .nav-links {{ display: flex; gap: 12px; justify-content: center; margin-top: 10px; }}
  .nav-links a {{ color: #38bdf8; text-decoration: none; font-size: 0.85rem; padding: 5px 14px; border: 1px solid #334155; border-radius: 6px; transition: all 0.2s; }}
  .nav-links a:hover {{ background: #334155; }}
  .stats {{ display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; margin: 10px 16px; }}
  .stat-card {{ background: #1e293b; border-radius: 8px; padding: 10px 20px; text-align: center; }}
  .stat-card .num {{ font-size: 1.5rem; font-weight: 700; color: #38bdf8; }}
  .stat-card .label {{ font-size: 0.75rem; color: #94a3b8; }}
  #map {{ width: 100%; height: 75vh; margin: 10px auto; max-width: 1400px; border-radius: 12px; }}
  .controls {{ display: flex; gap: 10px; justify-content: center; margin: 10px; flex-wrap: wrap; }}
  .btn {{ background: #334155; color: #e2e8f0; border: 1px solid #475569; border-radius: 6px;
          padding: 6px 14px; cursor: pointer; font-size: 0.85rem; transition: all 0.2s; }}
  .btn:hover {{ background: #475569; }}
  .btn.active {{ background: #2563eb; border-color: #3b82f6; }}
  .legend {{ text-align: center; padding: 8px; color: #64748b; font-size: 0.75rem; }}
  .leaflet-popup-content {{ font-size: 13px; max-width: 340px; max-height: 350px; overflow-y: auto; }}
  .leaflet-tooltip {{ font-size: 12px; max-width: 280px; white-space: normal; line-height: 1.4; }}
  .marker-cluster-small {{ background-color: rgba(56,189,248,0.6); }}
  .marker-cluster-small div {{ background-color: rgba(56,189,248,0.8); }}
  .marker-cluster-medium {{ background-color: rgba(251,191,36,0.6); }}
  .marker-cluster-medium div {{ background-color: rgba(251,191,36,0.8); }}
  .marker-cluster-large {{ background-color: rgba(239,68,68,0.6); }}
  .marker-cluster-large div {{ background-color: rgba(239,68,68,0.8); }}
  .pac-name {{ font-weight: 600; color: #1e293b; margin-top: 6px; }}
  .atencion {{ color: #555; font-size: 11px; padding-left: 10px; }}
  .atencion .fecha {{ color: #2563eb; font-weight: 500; }}
  .atencion .prof {{ color: #7c3aed; }}
</style>
</head>
<body>

<div class="header">
  <h1>Mapa de Direcciones — Pacientes CMC</h1>
  <p>Abril 2026 — Hover para resumen, click para detalle de atenciones</p>
  <div class="nav-links">
    <a href="/admin/mapa-comunas">Mapa comunas</a>
    <a href="/admin/dashboard">Dashboard KPIs</a>
    <a href="/admin">Panel admin</a>
  </div>
</div>

<div class="stats">
  <div class="stat-card"><div class="num">{total_dirs}</div><div class="label">Direcciones</div></div>
  <div class="stat-card"><div class="num">{total_pacs}</div><div class="label">Pacientes</div></div>
  <div class="stat-card"><div class="num">{total_citas}</div><div class="label">Citas</div></div>
</div>

<div class="controls">
  <button class="btn active" onclick="toggleLayer('clusters')">Clusters</button>
  <button class="btn" onclick="toggleLayer('heat')">Calor</button>
  <button class="btn" onclick="toggleLayer('points')">Puntos</button>
</div>

<div id="map"></div>

<div class="legend">
  Datos geocodificados desde Nominatim (OpenStreetMap) + coordenadas manuales de sectores.
  Hover para ver nombres, click para detalle completo de atenciones.
</div>

<script>
const pts = {json.dumps(points, ensure_ascii=False)};

// ── Helpers para construir tooltip y popup ──
function buildTooltip(p) {{
  let html = '<b>' + p.direccion + '</b><br>';
  html += '<span style="color:#888">' + (p.comuna || 'Sin comuna') + '</span> · ';
  html += '<b>' + p.pacientes + '</b> pac · <b>' + p.citas + '</b> citas<br>';
  if (p.detalle && p.detalle.length > 0) {{
    html += '<hr style="margin:4px 0;border-color:#ddd">';
    p.detalle.forEach(function(d) {{
      html += '<b>' + d.n + '</b>';
      if (d.a && d.a.length > 0) {{
        html += ' — <span style="color:#7c3aed">' + d.a[0].split(' — ')[1] + '</span>';
      }}
      html += '<br>';
    }});
  }}
  return html;
}}

function buildPopup(p) {{
  let html = '<div style="min-width:220px">';
  html += '<b style="font-size:14px">' + p.direccion + '</b><br>';
  html += '<span style="color:#888">' + (p.comuna || 'Sin comuna') + '</span><br>';
  html += '<b>' + p.pacientes + '</b> paciente(s), <b>' + p.citas + '</b> cita(s)';
  if (p.detalle && p.detalle.length > 0) {{
    html += '<hr style="margin:8px 0;border-color:#eee">';
    p.detalle.forEach(function(d) {{
      html += '<div class="pac-name">' + d.n + ' <span style="color:#888;font-weight:400">(' + d.c + ' cita' + (d.c > 1 ? 's' : '') + ')</span></div>';
      if (d.a && d.a.length > 0) {{
        d.a.forEach(function(at) {{
          var parts = at.split(' — ');
          html += '<div class="atencion"><span class="fecha">' + parts[0] + '</span>';
          if (parts[1]) html += ' — <span class="prof">' + parts[1] + '</span>';
          html += '</div>';
        }});
      }}
    }});
  }}
  html += '</div>';
  return html;
}}

const map = L.map('map').setView([-37.25, -73.28], 13);

// Capas base
const osm = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '&copy; OpenStreetMap', maxZoom: 19
}});
const satellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
  attribution: 'Esri World Imagery', maxZoom: 19
}});
osm.addTo(map);
L.control.layers({{'Mapa': osm, 'Satelite': satellite}}).addTo(map);

// Cluster layer
const clusters = L.markerClusterGroup({{
  maxClusterRadius: 40,
  spiderfyOnMaxZoom: true,
  showCoverageOnHover: false,
}});

pts.forEach(function(p) {{
  const marker = L.circleMarker([p.lat, p.lng], {{
    radius: Math.max(5, Math.min(12, p.pacientes * 4)),
    fillColor: p.pacientes > 2 ? '#ef4444' : p.pacientes > 1 ? '#fbbf24' : '#38bdf8',
    color: '#1e293b',
    weight: 1.5,
    fillOpacity: 0.8,
  }});

  marker.bindTooltip(buildTooltip(p), {{ sticky: true, direction: 'top', offset: [0, -10] }});
  marker.bindPopup(buildPopup(p), {{ maxWidth: 360, maxHeight: 400 }});
  clusters.addLayer(marker);
}});

clusters.addTo(map);

// Heat layer (oculto por defecto)
const heatData = pts.map(function(p) {{ return [p.lat, p.lng, p.pacientes]; }});
const heatLayer = L.heatLayer(heatData, {{
  radius: 25, blur: 20, maxZoom: 16, max: 5,
  gradient: {{0.2:'#2563eb', 0.4:'#38bdf8', 0.6:'#fbbf24', 0.8:'#f97316', 1:'#ef4444'}},
}});

// Points layer (oculto por defecto)
const pointsLayer = L.layerGroup();
pts.forEach(function(p) {{
  const dot = L.circleMarker([p.lat, p.lng], {{
    radius: Math.max(4, Math.min(10, p.pacientes * 3)),
    fillColor: p.pacientes > 2 ? '#ef4444' : p.pacientes > 1 ? '#fbbf24' : '#38bdf8',
    color: '#fff', weight: 1, fillOpacity: 0.85,
  }});
  dot.bindTooltip(buildTooltip(p), {{ sticky: true, direction: 'top', offset: [0, -10] }});
  dot.bindPopup(buildPopup(p), {{ maxWidth: 360, maxHeight: 400 }});
  pointsLayer.addLayer(dot);
}});

let activeLayer = 'clusters';
function toggleLayer(name) {{
  map.removeLayer(clusters);
  map.removeLayer(heatLayer);
  map.removeLayer(pointsLayer);
  if (name === 'clusters') clusters.addTo(map);
  else if (name === 'heat') heatLayer.addTo(map);
  else if (name === 'points') pointsLayer.addTo(map);
  activeLayer = name;
  document.querySelectorAll('.btn').forEach(function(b) {{ b.classList.remove('active'); }});
  event.target.classList.add('active');
}}

// Fit bounds
if (pts.length > 0) {{
  const bounds = pts.map(function(p) {{ return [p.lat, p.lng]; }});
  map.fitBounds(bounds, {{ padding: [30, 30] }});
}}
</script>
</body>
</html>"""

    path = "templates/heatmap_direcciones.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("Mapa de direcciones generado: %s", path)
    print(f"\nMapa generado: {path}")
    print(f"Abrir con: open {path}")


if __name__ == "__main__":
    asyncio.run(main())
