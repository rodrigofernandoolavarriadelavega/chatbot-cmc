"""
Redistribuye coordenadas de entradas fallback en geocode_cache aplicando
jitter determinístico (hash de la dirección) para dispersar clusters
artificiales sin romper la idempotencia del cache.

Lee centros por (source, lat~3dec, lng~3dec), reasigna cada fila con offset
basado en SHA-1 de direccion_key, ±0.003 grados (~330m) por eje. Direcciones
distintas siempre caen en posiciones distintas y reproducibles.
"""
import hashlib
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "heatmap_cache.db"
JITTER_DEG = 0.003  # ~330m por eje


def deterministic_offset(key: str) -> tuple[float, float]:
    h = hashlib.sha1(key.encode("utf-8")).digest()
    a = int.from_bytes(h[0:4], "big") / 0xFFFFFFFF  # [0,1]
    b = int.from_bytes(h[4:8], "big") / 0xFFFFFFFF
    return ((a * 2 - 1) * JITTER_DEG, (b * 2 - 1) * JITTER_DEG)


def main():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    rows = cur.execute(
        "SELECT direccion_key, lat, lng, source FROM geocode_cache "
        "WHERE source LIKE 'fallback%'"
    ).fetchall()
    print(f"Procesando {len(rows)} entradas fallback…")

    centroids: dict[tuple[str, float, float], tuple[float, float]] = {}
    for key, lat, lng, source in rows:
        bucket = (source, round(lat, 3), round(lng, 3))
        centroids.setdefault(bucket, (lat, lng))

    print(f"Centroides únicos detectados: {len(centroids)}")

    updates = 0
    for key, lat, lng, source in rows:
        bucket = (source, round(lat, 3), round(lng, 3))
        center_lat, center_lng = centroids[bucket]
        d_lat, d_lng = deterministic_offset(key)
        new_lat = center_lat + d_lat
        new_lng = center_lng + d_lng
        cur.execute(
            "UPDATE geocode_cache SET lat=?, lng=? WHERE direccion_key=?",
            (new_lat, new_lng, key),
        )
        updates += 1

    conn.commit()
    conn.close()
    print(f"Actualizadas {updates} entradas con jitter determinístico ±{JITTER_DEG}°")


if __name__ == "__main__":
    main()
