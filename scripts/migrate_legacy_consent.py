"""
Migración Ley 19.628 — marca como 'accepted' a todos los pacientes preexistentes.

Base legal: relación contractual preexistente (art. 12 lit. c Ley 19.628 reformada
y consentimiento tácito derivado del uso continuado del canal WhatsApp del CMC
desde antes de la entrada en vigencia del opt-in explícito 2026-04-16).

Qué hace:
  1. Itera todos los `phone` con (a) un `contact_profiles`, o (b) mensajes en
     `messages`. Estos son los pacientes/contactos que ya estaban en la base.
  2. Llama `save_privacy_consent(phone, 'accepted', method='legacy_migration')`
     para que NO reciban el prompt de consentimiento (evita fricción con 2k+
     conversaciones existentes).
  3. Reporta cuántos se migraron y cuántos ya tenían consent previo.

Idempotente: correr varias veces no rompe nada (PRIMARY KEY en phone hace UPSERT
vía INSERT OR REPLACE dentro de save_privacy_consent).

Uso:
    PYTHONPATH=app venv/bin/python scripts/migrate_legacy_consent.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Permite correr desde la raíz del repo sin PYTHONPATH si es necesario.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from session import _conn, save_privacy_consent, get_privacy_consent  # noqa: E402


def main() -> None:
    with _conn() as c:
        phones_profile = {
            row[0] for row in c.execute(
                "SELECT DISTINCT phone FROM contact_profiles WHERE phone IS NOT NULL AND phone != ''"
            ).fetchall()
        }
        phones_messages = {
            row[0] for row in c.execute(
                "SELECT DISTINCT phone FROM messages WHERE phone IS NOT NULL AND phone != ''"
            ).fetchall()
        }

    all_phones = phones_profile | phones_messages

    migrated = 0
    already = 0
    skipped = 0

    for phone in sorted(all_phones):
        existing = get_privacy_consent(phone)
        if existing and existing.get("status") in ("accepted", "declined"):
            already += 1
            continue
        try:
            save_privacy_consent(phone, "accepted", method="legacy_migration")
            migrated += 1
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] {phone}: {e}")
            skipped += 1

    print(f"Total phones candidatos: {len(all_phones)}")
    print(f"  - con contact_profile: {len(phones_profile)}")
    print(f"  - con mensajes:        {len(phones_messages)}")
    print(f"Migrados (accepted legacy): {migrated}")
    print(f"Ya con consent previo:      {already}")
    print(f"Saltados por error:         {skipped}")


if __name__ == "__main__":
    main()
