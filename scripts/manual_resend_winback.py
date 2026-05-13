"""
manual_resend_winback.py — Reenvío manual de winback a phones que aceptaron consent
pero no recibieron el winback por bug de normalización de teléfono (corregido en
commit posterior a cd7aec1).

Uso:
    python scripts/manual_resend_winback.py

Checks antes de enviar:
    - Paciente NO en estado HUMAN_TAKEOVER (skip con aviso)
    - Paciente encontrado en BI (get_candidato_por_phone con fix)
    - ya_enviado_winback_hoy → False
    - Consent aceptado en bi.marketing_consent

Autorizado por Rodrigo Olavarría — 2026-05-13.
"""
import asyncio
import os
import sys
import logging

# Ajustar path para importar módulos del bot
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
os.chdir(os.path.join(_ROOT, "app"))

from dotenv import load_dotenv
load_dotenv(os.path.join(_ROOT, ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("manual_resend")

# Phones que aceptaron consent hoy y no recibieron winback
TARGET_PHONES = [
    "56987273140",  # Caroline — en HUMAN_TAKEOVER cuando respondió, skip si sigue así
    "56977281627",  # Eroldo
    "56966373231",  # Jose
    "56961800733",  # Benjamín
]


def get_session_state(phone: str) -> str | None:
    """Retorna el state de la sesión SQLite o None si no existe."""
    import sqlite3
    from pathlib import Path

    sqlcipher_key = os.getenv("SQLCIPHER_KEY", "").strip()
    db_path = Path(_ROOT) / "data" / "sessions.db"
    if not db_path.exists():
        return None
    try:
        if sqlcipher_key:
            try:
                from sqlcipher3 import dbapi2 as sc
                conn = sc.connect(str(db_path), timeout=5)
                conn.execute(f"PRAGMA key=\"x'{sqlcipher_key}'\";")
            except ImportError:
                import sqlite3
                conn = sqlite3.connect(str(db_path), timeout=5)
        else:
            conn = sqlite3.connect(str(db_path), timeout=5)
        row = conn.execute(
            "SELECT state FROM sessions WHERE phone = ?", (phone,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        log.warning("get_session_state error phone=%s: %s", phone, e)
        return None


async def resend_one(phone: str) -> dict:
    """Intenta reenviar el winback a un phone. Retorna dict con resultado."""
    from winback import (
        WINBACK_ACTIVE,
        get_candidato_por_phone,
        ya_enviado_winback_hoy,
        has_marketing_consent,
        send_winback,
    )

    result = {"phone": phone, "status": None, "reason": None}

    # Check 1: WINBACK_ACTIVE
    if not WINBACK_ACTIVE:
        result["status"] = "skip"
        result["reason"] = "WINBACK_ACTIVE=false"
        return result

    # Check 2: estado HUMAN_TAKEOVER en sessions.db
    state = get_session_state(phone)
    if state == "HUMAN_TAKEOVER":
        result["status"] = "skip"
        result["reason"] = f"state=HUMAN_TAKEOVER — recepción atiende, no reenviar"
        return result

    # Check 3: consent aceptado
    if not has_marketing_consent(phone):
        result["status"] = "skip"
        result["reason"] = "sin consent aceptado en marketing_consent"
        return result

    # Check 4: ya enviado hoy
    if ya_enviado_winback_hoy(phone):
        result["status"] = "skip"
        result["reason"] = "ya_enviado_winback_hoy=True"
        return result

    # Check 5: candidato en BI
    candidato = get_candidato_por_phone(phone)
    if not candidato:
        result["status"] = "skip"
        result["reason"] = "no encontrado en v_winback_cohortes_contactables (sin atenciones en BI)"
        return result

    log.info("resend: phone=%s candidato=%s cohorte=%s esp=%s",
             phone, candidato.get("nombre"), candidato.get("cohorte"),
             candidato.get("ultima_especialidad"))

    # Enviar
    ok = await send_winback(candidato)
    result["status"] = "sent" if ok else "error"
    result["reason"] = f"send_winback={'ok' if ok else 'falló'}"
    result["candidato"] = {
        "nombre": candidato.get("nombre"),
        "cohorte": candidato.get("cohorte"),
        "especialidad": candidato.get("ultima_especialidad"),
    }
    return result


async def main():
    log.info("=== manual_resend_winback.py — %d phones ===", len(TARGET_PHONES))

    results = []
    for phone in TARGET_PHONES:
        log.info("--- procesando %s ---", phone)
        r = await resend_one(phone)
        results.append(r)
        log.info("resultado: status=%s reason=%s", r["status"], r["reason"])
        # Pausa de 5s entre envíos
        if r["status"] == "sent":
            await asyncio.sleep(5)

    log.info("=== resumen ===")
    for r in results:
        log.info("  %s → %s (%s)", r["phone"], r["status"], r["reason"])

    sent = sum(1 for r in results if r["status"] == "sent")
    skipped = sum(1 for r in results if r["status"] == "skip")
    errors = sum(1 for r in results if r["status"] == "error")
    log.info("enviados=%d skipped=%d errors=%d", sent, skipped, errors)


if __name__ == "__main__":
    asyncio.run(main())
