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
}

import random

def fallback_coords(direccion: str, localidad: str) -> tuple[float, float] | None:
    """Intenta obtener coordenadas desde el diccionario de ubicaciones conocidas."""
    dir_upper = direccion.upper()
    # Buscar match en known locations
    for name, coords in KNOWN_LOCATIONS.items():
        if name in dir_upper:
            # Agregar jitter pequeño para que no se apilen
            jitter_lat = random.uniform(-0.001, 0.001)
            jitter_lng = random.uniform(-0.001, 0.001)
            return (coords[0] + jitter_lat, coords[1] + jitter_lng)
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

    # Agrupar por dirección normalizada para no geocodificar duplicados
    dir_groups = {}
    for r in rows:
        key = r["direccion"].strip().upper()
        if key not in dir_groups:
            dir_groups[key] = {"direccion": r["direccion"], "comuna": r["comuna"] or "", "pacientes": []}
        dir_groups[key]["pacientes"].append({
            "nombre": f"{r['nombre']} {r['apellidos']}".strip(),
            "citas": r["num_citas"],
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
                nombres = [p["nombre"] for p in info["pacientes"]]
                points.append({
                    "lat": lat,
                    "lng": lng,
                    "direccion": info["direccion"],
                    "comuna": info["comuna"],
                    "pacientes": len(info["pacientes"]),
                    "nombres": nombres[:5],
                    "citas": total_citas,
                })
            else:
                sin_geo += 1
        else:
            sin_geo += 1

    conn.close()
    log.info("Puntos para el mapa: %d, sin coordenadas válidas: %d", len(points), sin_geo)

    generate_address_map(points)


def generate_address_map(points: list[dict]):
    """Genera un HTML con mapa de direcciones exactas."""
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
  .leaflet-popup-content {{ font-size: 13px; }}
  .marker-cluster-small {{ background-color: rgba(56,189,248,0.6); }}
  .marker-cluster-small div {{ background-color: rgba(56,189,248,0.8); }}
  .marker-cluster-medium {{ background-color: rgba(251,191,36,0.6); }}
  .marker-cluster-medium div {{ background-color: rgba(251,191,36,0.8); }}
  .marker-cluster-large {{ background-color: rgba(239,68,68,0.6); }}
  .marker-cluster-large div {{ background-color: rgba(239,68,68,0.8); }}
</style>
</head>
<body>

<div class="header">
  <h1>Mapa de Direcciones — Pacientes CMC</h1>
  <p>Abril 2026 — Cada punto es una direccion con pacientes</p>
</div>

<div class="stats">
  <div class="stat-card"><div class="num">{len(points)}</div><div class="label">Direcciones</div></div>
  <div class="stat-card"><div class="num">{sum(p['pacientes'] for p in points)}</div><div class="label">Pacientes</div></div>
  <div class="stat-card"><div class="num">{sum(p['citas'] for p in points)}</div><div class="label">Citas</div></div>
</div>

<div class="controls">
  <button class="btn active" onclick="toggleLayer('clusters')">Clusters</button>
  <button class="btn" onclick="toggleLayer('heat')">Calor</button>
  <button class="btn" onclick="toggleLayer('points')">Puntos</button>
</div>

<div id="map"></div>

<div class="legend">
  Datos geocodificados desde Nominatim (OpenStreetMap) + coordenadas manuales de sectores.
  Click en cada punto/cluster para ver detalle.
</div>

<script>
const pts = {json.dumps(points, ensure_ascii=False)};

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

pts.forEach(p => {{
  const marker = L.circleMarker([p.lat, p.lng], {{
    radius: Math.max(5, Math.min(12, p.pacientes * 4)),
    fillColor: p.pacientes > 2 ? '#ef4444' : p.pacientes > 1 ? '#fbbf24' : '#38bdf8',
    color: '#1e293b',
    weight: 1.5,
    fillOpacity: 0.8,
  }});

  let popup = `<b>${{p.direccion}}</b><br>`;
  popup += `<span style="color:#666">${{p.comuna || 'Sin comuna'}}</span><br>`;
  popup += `<b>${{p.pacientes}}</b> paciente(s), <b>${{p.citas}}</b> cita(s)<br>`;
  if (p.nombres.length > 0) {{
    popup += `<br><small>${{p.nombres.join('<br>')}}</small>`;
  }}
  marker.bindPopup(popup);
  clusters.addLayer(marker);
}});

clusters.addTo(map);

// Heat layer (oculto por defecto)
const heatData = pts.map(p => [p.lat, p.lng, p.pacientes]);
const heatLayer = L.heatLayer(heatData, {{
  radius: 25, blur: 20, maxZoom: 16, max: 5,
  gradient: {{0.2:'#2563eb', 0.4:'#38bdf8', 0.6:'#fbbf24', 0.8:'#f97316', 1:'#ef4444'}},
}});

// Points layer (oculto por defecto)
const pointsLayer = L.layerGroup();
pts.forEach(p => {{
  const dot = L.circleMarker([p.lat, p.lng], {{
    radius: Math.max(4, Math.min(10, p.pacientes * 3)),
    fillColor: p.pacientes > 2 ? '#ef4444' : p.pacientes > 1 ? '#fbbf24' : '#38bdf8',
    color: '#fff', weight: 1, fillOpacity: 0.85,
  }});
  let popup = `<b>${{p.direccion}}</b><br>${{p.pacientes}} pac, ${{p.citas}} citas`;
  dot.bindPopup(popup);
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
  document.querySelectorAll('.btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
}}

// Fit bounds
if (pts.length > 0) {{
  const bounds = pts.map(p => [p.lat, p.lng]);
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
