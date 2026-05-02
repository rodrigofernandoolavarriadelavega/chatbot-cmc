#!/usr/bin/env python3
"""
Exporta dos Custom Audiences de Meta a partir de los datos del CMC:

  1. meta_audience_pacientes_full_YYYYMMDD.csv
     Fuentes combinadas:
     a) Medilink API — sweep completo por nombre a-z+tildes y apellidos a-z+tildes.
        Retorna hasta ~600-700 pacientes activos con datos de contacto reales
        (celular, email, nombre, apellidos). El API tiene un cap de 50 por query
        y no expone paginación global; el sweep por letra cubre la base visible.
     b) contact_profiles en sessions.db — pacientes que usaron el bot de WA.
        El campo `phone` ES el número WhatsApp (ya normalizado, formato 56XXXXXXXXX).
        Aporta ~250 registros adicionales con RUT y nombre.
     Deduplicación por RUT antes de exportar.
     Uso: semilla Lookalikes + exclusión campañas de adquisición.

  2. meta_audience_wa_30d_YYYYMMDD.csv
     Fuente: sessions.db — phones únicos con mensajes entrantes en últimos 30 días.
     En el VPS hay ~622 phones activos en 30 días.
     Uso: exclusión de saturación en campañas de retargeting.

Formato de salida: Meta Customer File (hashed data).
  Columnas: email, phone, fn, ln, country
  - email / phone / fn / ln  → SHA-256 hex lowercase del valor normalizado
  - country                  → 'cl' en plain (Meta no hashea country)
  - Si campo vacío en fuente → columna vacía en CSV (no se hashea string vacío)

Uso:
    # Desde la raíz del proyecto:
    python3 scripts/export_meta_custom_audience.py
    python3 scripts/export_meta_custom_audience.py --out-dir ./scripts/out
    python3 scripts/export_meta_custom_audience.py --dry-run

Subir resultado a:
    Meta Ads Manager → Audiences → Create Custom Audience →
    Customer List → Hashed Data → Upload CSV
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

# ── Config ──────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _ROOT / ".env"


def _load_env() -> None:
    if not _ENV_FILE.exists():
        return
    with open(_ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

MEDILINK_BASE_URL = os.environ.get(
    "MEDILINK_BASE_URL", "https://api.medilink2.healthatom.com/api/v5"
)
MEDILINK_TOKEN = os.environ.get("MEDILINK_TOKEN", "")
SQLCIPHER_KEY = os.environ.get("SQLCIPHER_KEY", "")

DB_PATH = _ROOT / "data" / "sessions.db"

HEADERS = {"Authorization": f"Token {MEDILINK_TOKEN}"}

# Pausa entre requests a Medilink (segundos) para no saturar rate limit
RATE_PAUSE = 0.15


# ── Hashing ─────────────────────────────────────────────────────────────────

def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _strip_accents(text: str) -> str:
    """Elimina tildes/diacríticos preservando ñ."""
    result = []
    for ch in text:
        if ch in ("ñ", "Ñ"):
            result.append("n")
            continue
        nfkd = unicodedata.normalize("NFKD", ch)
        filtered = "".join(c for c in nfkd if not unicodedata.combining(c))
        result.append(filtered)
    return "".join(result)


def _hash_name(raw: Optional[str]) -> str:
    """Normaliza nombre (lowercase, sin tildes, trim) y devuelve hash.
    Retorna '' si vacío."""
    if not raw or not raw.strip():
        return ""
    normalized = _strip_accents(raw.strip().lower())
    return _sha256(normalized)


def _hash_email(raw: Optional[str]) -> str:
    """Normaliza email (lowercase, trim) y devuelve hash.
    Retorna '' si vacío o inválido."""
    if not raw or not raw.strip():
        return ""
    normalized = raw.strip().lower()
    if "@" not in normalized:
        return ""
    local, _, domain = normalized.partition("@")
    if not local or "." not in domain:
        return ""
    return _sha256(normalized)


def _normalize_phone_e164(raw: Optional[str]) -> str:
    """Convierte teléfono a E.164 sin '+' para Chile (56 9XXXXXXXX).

    Casos manejados:
        '+56 9 6661 0737' → '56966610737'
        '9 6661 0737'     → '56966610737'
        '966610737'       → '56966610737'
        '56966610737'     → '56966610737'
        '+1 555 0000'     → ''   (no es Chile)
        '123'             → ''   (muy corto)
    """
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""

    if digits.startswith("56") and len(digits) == 11:
        candidate = digits
    elif digits.startswith("9") and len(digits) == 9:
        candidate = "56" + digits
    elif digits.startswith("9") and len(digits) == 8:
        # 8 dígitos que empiezan con 9 → falta el primer dígito del celular chileno
        candidate = "569" + digits
    elif len(digits) == 8 and not digits.startswith("9"):
        # 8 dígitos sin 9 inicial → número corto chileno (antiguo fijo), no celular
        return ""
    else:
        return ""

    if len(candidate) != 11 or not candidate.startswith("56") or candidate[2] != "9":
        return ""
    return candidate


def _hash_phone(raw: Optional[str]) -> tuple[str, str]:
    """Retorna (e164_sin_plus, sha256_hex) o ('', '') si inválido."""
    normalized = _normalize_phone_e164(raw)
    if not normalized:
        return "", ""
    return normalized, _sha256(normalized)


# ── SQLite (con o sin SQLCipher) ─────────────────────────────────────────────

def _open_db():
    """Abre sessions.db. Usa sqlcipher3/pysqlcipher3 si SQLCIPHER_KEY está seteado."""
    if not DB_PATH.exists():
        raise RuntimeError(f"sessions.db no encontrado en {DB_PATH}")

    if SQLCIPHER_KEY:
        try:
            from sqlcipher3 import dbapi2 as sqlite3_mod
        except ImportError:
            try:
                from pysqlcipher3 import dbapi2 as sqlite3_mod
            except ImportError:
                raise RuntimeError(
                    "SQLCIPHER_KEY está seteada pero sqlcipher3/pysqlcipher3 "
                    "no están instalados."
                )
        # Validar que la clave sea hex (misma validación que session.py)
        if not re.fullmatch(r"[0-9a-fA-F]+", SQLCIPHER_KEY):
            raise ValueError("SQLCIPHER_KEY debe ser hex (0-9, a-f).")
        conn = sqlite3_mod.connect(str(DB_PATH), timeout=10)
        conn.execute(f"PRAGMA key = \"x'{SQLCIPHER_KEY}'\"")
        conn.execute("PRAGMA cipher_page_size = 4096")
    else:
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH), timeout=10)

    conn.row_factory = (
        lambda c, r: dict(zip([col[0] for col in c.description], r))
    )
    conn.execute("PRAGMA busy_timeout=5000")
    # Prueba que la DB es accesible
    conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    return conn


# ── Fuente 1a: Medilink API sweep ────────────────────────────────────────────

# Caracteres para el sweep de nombre/apellidos en Medilink.
# Cubre todo el alfabeto español (tildes incluidas) + ñ.
# Cada query retorna los 50 más recientes que contienen ese carácter en el campo.
# La unión de todos los resultados cubre la base activa con alta cobertura.
_SWEEP_CHARS = list("abcdefghijklmnopqrstuvwxyzñáéíóúü")


def _fetch_medilink_sweep(client: httpx.Client) -> dict[int, dict]:
    """
    Descarga pacientes de Medilink via sweep de caracteres en nombre y apellidos.
    Retorna dict {id_paciente: registro_crudo}.
    """
    all_records: dict[int, dict] = {}
    total_queries = len(_SWEEP_CHARS) * 2  # nombre + apellidos
    done = 0

    for field in ("nombre", "apellidos"):
        for ch in _SWEEP_CHARS:
            q_filter = json.dumps({field: {"lk": ch}}, separators=(",", ":"))
            try:
                r = client.get(
                    f"{MEDILINK_BASE_URL}/pacientes",
                    params={"q": q_filter},
                )
            except httpx.RequestError as e:
                print(f"  [WARN] Medilink lk '{ch}' en {field}: {e}")
                done += 1
                continue

            if r.status_code == 429:
                print(f"  [WARN] 429 rate limit en {field}='{ch}', esperando 10s...")
                time.sleep(10)
                # Reintentar una vez
                try:
                    r = client.get(
                        f"{MEDILINK_BASE_URL}/pacientes",
                        params={"q": q_filter},
                    )
                except httpx.RequestError:
                    done += 1
                    continue

            if r.status_code != 200:
                done += 1
                time.sleep(RATE_PAUSE)
                continue

            try:
                data = r.json().get("data", [])
            except ValueError:
                done += 1
                time.sleep(RATE_PAUSE)
                continue

            for p in data:
                pid = p.get("id")
                if pid and pid not in all_records:
                    all_records[pid] = p

            done += 1
            if done % 20 == 0:
                print(
                    f"  [medilink] sweep {done}/{total_queries} — "
                    f"{len(all_records)} únicos hasta ahora"
                )
            time.sleep(RATE_PAUSE)

    return all_records


def _fetch_all_patients_medilink() -> list[dict]:
    """Ejecuta el sweep Medilink y retorna lista de registros únicos."""
    if not MEDILINK_TOKEN:
        raise RuntimeError("MEDILINK_TOKEN no está configurado en .env")

    print(f"[medilink] Iniciando sweep ({len(_SWEEP_CHARS)*2} queries)...")
    with httpx.Client(timeout=20, headers=HEADERS) as client:
        records = _fetch_medilink_sweep(client)

    patients = list(records.values())
    print(f"[medilink] Sweep completado: {len(patients)} pacientes únicos")
    return patients


# ── Fuente 1b: contact_profiles en sessions.db ──────────────────────────────

def _fetch_contact_profiles() -> list[dict]:
    """
    Lee contact_profiles de sessions.db.
    El campo `phone` es el número WhatsApp del paciente (ya en formato 56XXXXXXXXX,
    sin '+', normalizado por el bot). Se usa directamente como phone E.164.
    """
    conn = _open_db()
    try:
        rows = conn.execute(
            """
            SELECT phone, rut, nombre, email
            FROM contact_profiles
            WHERE phone IS NOT NULL AND phone != ''
            """
        ).fetchall()
    finally:
        conn.close()

    print(f"[contact_profiles] {len(rows)} registros leídos de sessions.db")
    return rows


# ── Construcción CSV 1: pacientes_full ───────────────────────────────────────

def _rut_key(rut: Optional[str]) -> str:
    """Normaliza RUT para deduplicación (solo alfanumérico uppercase)."""
    if not rut:
        return ""
    return re.sub(r"[^0-9kKKK]", "", rut.upper())


def _build_pacientes_rows(
    medilink_patients: list[dict],
    contact_profiles: list[dict],
) -> tuple[list[dict], dict]:
    """
    Combina datos de Medilink y contact_profiles.
    Deduplicación por RUT. contact_profiles complementa a Medilink cuando
    hay RUT coincidente, y aporta registros adicionales que Medilink no expone.
    """
    stats = {
        "medilink_raw": len(medilink_patients),
        "profiles_raw": len(contact_profiles),
        "sin_rut_y_sin_phone": 0,
        "phone_invalido": 0,
        "email_invalido": 0,
        "duplicados_rut": 0,
        "exportados": 0,
    }

    seen_ruts: set[str] = set()
    seen_phones: set[str] = set()  # dedup secundario por phone cuando no hay RUT
    rows: list[dict] = []

    def _add_row(
        rut: str,
        nombre: str,
        apellidos: str,
        phone_raw: str,
        email_raw: str,
    ) -> None:
        rut_k = _rut_key(rut)
        if rut_k:
            if rut_k in seen_ruts:
                stats["duplicados_rut"] += 1
                return
            seen_ruts.add(rut_k)

        # Normalizar phone
        e164, phone_hash = _hash_phone(phone_raw)
        if phone_raw and not phone_hash:
            stats["phone_invalido"] += 1

        if not phone_hash and not rut_k:
            # Sin RUT ni phone → no sirve para Meta
            stats["sin_rut_y_sin_phone"] += 1
            return

        # Dedup por phone si no hay RUT
        if not rut_k and e164:
            if e164 in seen_phones:
                stats["duplicados_rut"] += 1
                return
            seen_phones.add(e164)

        # Email
        email_hash = _hash_email(email_raw)
        if email_raw and not email_hash:
            stats["email_invalido"] += 1

        rows.append(
            {
                "email": email_hash,
                "phone": phone_hash,
                "fn": _hash_name(nombre),
                "ln": _hash_name(apellidos),
                "country": "cl",
            }
        )

    # ── 1a: Medilink (tiene nombre + apellidos separados) ────────────────────
    for p in medilink_patients:
        _add_row(
            rut=p.get("rut", ""),
            nombre=(p.get("nombre") or "").strip(),
            apellidos=(p.get("apellidos") or "").strip(),
            phone_raw=p.get("celular") or p.get("telefono") or "",
            email_raw=p.get("email") or "",
        )

    # ── 1b: contact_profiles (phone = número WA, sin '+') ────────────────────
    # El phone en sessions.db YA está normalizado por el bot (sin '+').
    # Lo tratamos directamente como E.164 sin normalización adicional
    # (es '56XXXXXXXXX' por convención del bot).
    for cp in contact_profiles:
        phone_raw = cp.get("phone") or ""
        # El phone de sessions.db es '56XXXXXXXXX' sin '+' → válido directamente
        # Pero pasarlo por _normalize_phone_e164 igual para validar
        _add_row(
            rut=cp.get("rut") or "",
            nombre=cp.get("nombre") or "",
            apellidos="",  # sessions.db no guarda apellidos separados
            phone_raw=phone_raw,
            email_raw=cp.get("email") or "",
        )

    stats["exportados"] = len(rows)
    return rows, stats


# ── Fuente 2: sessions.db WA 30 días ─────────────────────────────────────────

def _fetch_wa_30d() -> list[dict]:
    """
    Phones únicos con mensajes entrantes en los últimos 30 días,
    enriquecidos con nombre/email de contact_profiles.
    """
    conn = _open_db()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).strftime("%Y-%m-%d %H:%M:%S")

    try:
        rows = conn.execute(
            """
            SELECT DISTINCT m.phone
            FROM messages m
            WHERE m.direction = 'in'
              AND m.ts >= ?
            """,
            (cutoff,),
        ).fetchall()

        phones = [r["phone"] for r in rows if r.get("phone")]
        print(f"[wa_30d] {len(phones)} phones únicos con actividad en últimos 30 días")

        results = []
        for phone in phones:
            profile = conn.execute(
                "SELECT nombre, email FROM contact_profiles WHERE phone = ?",
                (phone,),
            ).fetchone()
            nombre = (profile or {}).get("nombre") or ""
            email = (profile or {}).get("email") or ""
            results.append({"phone": phone, "nombre": nombre, "email": email})
    finally:
        conn.close()

    return results


def _build_wa_rows(raw_wa: list[dict]) -> tuple[list[dict], dict]:
    """Convierte registros WA 30d en filas para el CSV."""
    stats = {
        "total_raw": len(raw_wa),
        "phone_invalido": 0,
        "exportados": 0,
    }

    rows: list[dict] = []
    seen: set[str] = set()

    for entry in raw_wa:
        # Phone de sessions.db: '56XXXXXXXXX' (normalizado por el bot)
        e164, phone_hash = _hash_phone(entry.get("phone"))
        if not phone_hash:
            stats["phone_invalido"] += 1
            continue
        if e164 in seen:
            continue
        seen.add(e164)

        nombre = (entry.get("nombre") or "").strip()
        fn_hash = _hash_name(nombre) if nombre else ""

        rows.append(
            {
                "email": _hash_email(entry.get("email")),
                "phone": phone_hash,
                "fn": fn_hash,
                "ln": "",
                "country": "cl",
            }
        )

    stats["exportados"] = len(rows)
    return rows, stats


# ── Escritura CSV ────────────────────────────────────────────────────────────

FIELDNAMES = ["email", "phone", "fn", "ln", "country"]


def _write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[output] Escrito: {path}  ({len(rows)} filas)")


def _print_sample(rows: list[dict], n: int = 3) -> None:
    """Muestra N filas con hashes truncados a 12 chars."""
    if not rows:
        print("  (sin filas)")
        return
    print("  Sample (hashes truncados a 12 chars):")
    for row in rows[:n]:
        sample = {
            k: (v[:12] if v and k != "country" else v)
            for k, v in row.items()
        }
        print(f"    {sample}")


def _verify_hashes(rows: list[dict], label: str) -> None:
    """Verifica que los hashes tengan 64 chars hex y country sea 'cl' plain."""
    issues = []
    for i, row in enumerate(rows[:20]):
        for field in ("email", "phone", "fn", "ln"):
            val = row.get(field, "")
            if val and len(val) != 64:
                issues.append(f"  [WARN] {label} row {i} {field}: len={len(val)} (esperado 64)")
        if row.get("country") != "cl":
            issues.append(f"  [WARN] {label} row {i} country={row.get('country')!r} (esperado 'cl')")
    if issues:
        for issue in issues:
            print(issue)
    else:
        print(f"  [verify] {label}: hashes OK (64 hex chars), country='cl' OK")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Exporta Custom Audiences Meta (pacientes_full + wa_30d)"
    )
    parser.add_argument(
        "--out-dir",
        default=str(_ROOT / "scripts" / "out"),
        help="Directorio de salida (default: scripts/out/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo muestra conteos y sample, no escribe archivos",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    today = datetime.now().strftime("%Y%m%d")
    dry = args.dry_run

    if dry:
        print("[dry-run] No se escribirán archivos.\n")

    # ── CSV 1: pacientes_full ────────────────────────────────────────────────
    pacientes_ok = False
    rows_pac: list[dict] = []

    # 1a: Medilink sweep
    medilink_patients: list[dict] = []
    try:
        medilink_patients = _fetch_all_patients_medilink()
    except Exception as e:
        print(f"\n[WARN] Medilink sweep falló: {e}")
        print("[WARN] Continuando solo con contact_profiles.\n")

    # 1b: contact_profiles
    contact_profiles: list[dict] = []
    try:
        contact_profiles = _fetch_contact_profiles()
    except Exception as e:
        print(f"\n[WARN] contact_profiles falló: {e}\n")

    if medilink_patients or contact_profiles:
        rows_pac, stats_pac = _build_pacientes_rows(medilink_patients, contact_profiles)
        print(f"\n[pacientes_full] Estadísticas:")
        print(f"  Medilink raw:              {stats_pac['medilink_raw']}")
        print(f"  contact_profiles raw:      {stats_pac['profiles_raw']}")
        print(f"  Sin RUT y sin phone:       {stats_pac['sin_rut_y_sin_phone']}")
        print(f"  Phone inválido:            {stats_pac['phone_invalido']}")
        print(f"  Email inválido:            {stats_pac['email_invalido']}")
        print(f"  Duplicados (por RUT/phone):{stats_pac['duplicados_rut']}")
        print(f"  Exportados:                {stats_pac['exportados']}")
        _print_sample(rows_pac)
        _verify_hashes(rows_pac, "pacientes_full")

        if not dry:
            fname = out_dir / f"meta_audience_pacientes_full_{today}.csv"
            _write_csv(rows_pac, fname)
        pacientes_ok = True
    else:
        print("\n[ERROR] Sin datos de pacientes (Medilink y contact_profiles fallaron).")

    print()

    # ── CSV 2: wa_30d ────────────────────────────────────────────────────────
    wa_ok = False
    rows_wa: list[dict] = []
    try:
        raw_wa = _fetch_wa_30d()
        rows_wa, stats_wa = _build_wa_rows(raw_wa)

        print(f"\n[wa_30d] Estadísticas:")
        print(f"  Raw (phones únicos): {stats_wa['total_raw']}")
        print(f"  Phone inválido:      {stats_wa['phone_invalido']}")
        print(f"  Exportados:          {stats_wa['exportados']}")
        _print_sample(rows_wa)
        _verify_hashes(rows_wa, "wa_30d")

        if not dry:
            fname = out_dir / f"meta_audience_wa_30d_{today}.csv"
            _write_csv(rows_wa, fname)
        wa_ok = True
    except Exception as e:
        print(f"\n[ERROR] SQLite falló: {e}")
        print("[WARN] CSV de WA no generado.")

    # ── Verificación normalización phone ─────────────────────────────────────
    print()
    print("[verify] Normalización phone de muestra:")
    test_cases = [
        ("+56 9 6661 0737", "56966610737"),
        ("966610737",       "56966610737"),
        ("56966610737",     "56966610737"),
        ("+1 555 0000",     ""),
    ]
    all_ok = True
    for inp, expected in test_cases:
        e164, h = _hash_phone(inp)
        status = "OK" if e164 == expected else f"FAIL (got {e164!r})"
        if e164 != expected:
            all_ok = False
        expected_hash = _sha256(expected) if expected else ""
        print(f"  {inp!r:25} → {e164!r:15} hash[:12]={h[:12] if h else 'N/A':12} [{status}]")
    print(f"  check: {'PASS' if all_ok else 'FAIL'}")

    print()
    if pacientes_ok and wa_ok:
        print("[done] Ambos archivos generados correctamente.")
    elif pacientes_ok:
        print("[done] Solo pacientes_full generado (wa_30d falló).")
    elif wa_ok:
        print("[done] Solo wa_30d generado (pacientes_full falló).")
    else:
        print("[ERROR] Ningún archivo generado.")
        sys.exit(1)


if __name__ == "__main__":
    main()
