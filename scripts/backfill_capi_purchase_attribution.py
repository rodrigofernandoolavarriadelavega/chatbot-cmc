"""Backfill CAPI Purchase con atribución ctwa_clid.

Reenvía eventos Purchase históricos para pacientes atendidos recientemente
que llegaron al bot desde un Click-to-WhatsApp ad, pero cuyo Purchase
original fue enviado sin fbc/ctwa_clid.

VENTANA: Meta solo atribuye eventos con event_time <= 7 días de antigüedad.
Los Purchases fuera de esa ventana se loggean pero NO se envían.

DEDUP: El Purchase original usó uuid4 aleatorio como event_id, por lo que
no podemos reutilizarlo. Este script genera un event_id DETERMINÍSTICO:
  sha256(phone + fecha_atencion + especialidad)[:32]
Esto garantiza que el backfill sea idempotente (dos corridas producen el
mismo event_id) y maximiza la deduplicación de Meta contra el original.
NOTA: puede haber algo de doble conteo con el evento huérfano original,
pero es marginal y aceptable frente a la ganancia de atribución.

USO:
  # Modo dry-run (por defecto — solo imprime, no envía nada):
  python scripts/backfill_capi_purchase_attribution.py

  # Enviar de verdad:
  python scripts/backfill_capi_purchase_attribution.py --send

  # Ajustar ventana (default 7 días):
  python scripts/backfill_capi_purchase_attribution.py --days 3 --send
"""

import argparse
import asyncio
import hashlib
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta

# ── Apuntar al directorio app ─────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.join(_SCRIPT_DIR, "..", "app")
sys.path.insert(0, _APP_DIR)

# Cargar .env si existe (solo fuera de producción donde ya hay env vars)
_DOTENV = os.path.join(_SCRIPT_DIR, "..", ".env")
if os.path.exists(_DOTENV):
    with open(_DOTENV) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("backfill_capi")


def _deterministic_event_id(phone: str, fecha: str, especialidad: str) -> str:
    """Event ID estable para deduplicación:
    sha256(phone|fecha|especialidad) truncado a 32 chars hex.
    Producido igual en cada corrida → Meta puede deduplicar.
    """
    raw = f"{phone}|{fecha}|{(especialidad or '').lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _ya_purchase_forward(phone: str, fecha: str) -> bool:
    """¿El forward-path (cron postconsulta) YA envió un Purchase por esta atención?

    GUARD ANTI-DUPLICADO. El forward-path usa event_id uuid4 aleatorio, así que su
    Purchase NO deduplica contra el event_id determinístico de este backfill: enviar
    de nuevo contaría DOS conversiones en Meta para la misma atención.

    El cron postconsulta loguea `capi_send_ok` (event_type=Purchase) ~02:00 UTC del
    día siguiente a la cita. Buscamos uno con ts en [fecha, fecha+2d] → si existe,
    esta atención ya está atribuida y NO hay que reenviar.
    """
    import json as _json
    from session import _conn
    with _conn() as conn:
        rows = conn.execute(
            """SELECT meta FROM conversation_events
               WHERE phone=? AND event='capi_send_ok'
                 AND date(ts) >= date(?) AND date(ts) <= date(?, '+2 days')""",
            (phone, fecha, fecha),
        ).fetchall()
    for r in rows:
        try:
            d = _json.loads(r["meta"]) if r["meta"] else {}
        except Exception:
            d = {}
        if d.get("event_type") == "Purchase":
            return True
    return False


def _mask_phone(phone: str) -> str:
    """Enmascara teléfono para logs: '56966610737' → '569***0737'."""
    if len(phone) > 7:
        return phone[:3] + "***" + phone[-4:]
    return "***"


def get_citas_con_atribucion(days: int) -> list[dict]:
    """Cruza citas_bot con meta_referrals para el período reciente.

    Retorna citas que:
    - Tienen fecha en los últimos `days` días (ventana Meta).
    - Tienen fecha <= hoy (no futuras — el paciente aún no fue atendido).
    - Tienen un meta_referral con ctwa_clid en los últimos 7 días (TTL extendido).
    - No están canceladas.
    - Son de canal WhatsApp (no fb_/ig_).
    """
    from session import _conn

    # Hora Chile = UTC-4 (conservador, sin DST). Usamos strftime local para obtener
    # la fecha en zona Chile en vez de UTC (evita diferencias de 1 día en horario nocturno).
    chile_offset = timezone(timedelta(hours=-4))
    now_chile = datetime.now(chile_offset)

    cutoff_fecha = (now_chile - timedelta(days=days)).strftime("%Y-%m-%d")
    hoy_fecha = now_chile.strftime("%Y-%m-%d")  # cota superior: no incluir fechas futuras

    # TTL para meta_referral: 7 días desde el referral (no desde la cita)
    cutoff_ts = int(time.time()) - 7 * 24 * 3600

    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT
                cb.phone,
                cb.id_cita,
                cb.especialidad,
                cb.profesional,
                cb.fecha,
                cb.hora,
                p.nombre,
                p.rut,
                mr.ctwa_clid,
                mr.source_id,
                mr.ts AS referral_ts
            FROM citas_bot cb
            LEFT JOIN contact_profiles p ON p.phone = cb.phone
            INNER JOIN meta_referrals mr
                ON mr.phone = cb.phone
                AND mr.ctwa_clid IS NOT NULL
                AND mr.ctwa_clid != ''
                AND mr.ts >= ?
            WHERE cb.fecha >= ?
            AND cb.fecha <= ?
            AND cb.cancel_detected_at IS NULL
            AND cb.phone NOT LIKE 'fb_%'
            AND cb.phone NOT LIKE 'ig_%'
            ORDER BY cb.fecha DESC, mr.ts DESC
            """,
            (cutoff_ts, cutoff_fecha, hoy_fecha),
        ).fetchall()

    # Deduplicar: si hay varios referrals para el mismo phone, quedarse con el más reciente.
    # El ORDER BY ya los pone en orden; usamos phone+id_cita como clave.
    seen: set[tuple] = set()
    result = []
    for r in rows:
        key = (r["phone"], r["id_cita"])
        if key not in seen:
            seen.add(key)
            result.append(dict(r))
    return result


def is_within_meta_window(fecha_cita: str, hora_cita: str, days: int) -> tuple[bool, str]:
    """Verifica que la fecha/hora de atención esté en [cutoff, ahora].

    Retorna (dentro_de_ventana, motivo_exclusion).
    motivo_exclusion es '' si está dentro, 'futura' o 'antigua' si no.
    La comparación usa granularidad de hora: una cita de hoy 18:00 a las 15:00 NO se incluye.
    """
    try:
        event_time = cita_fecha_to_unix(fecha_cita, hora_cita)
        now_ts = time.time()
        cutoff_ts = now_ts - days * 24 * 3600

        if event_time > now_ts:
            return False, "futura"
        if event_time < cutoff_ts:
            return False, "antigua"
        return True, ""
    except Exception:
        return False, "antigua"


def cita_fecha_to_unix(fecha: str, hora: str) -> int:
    """Convierte fecha+hora de cita a unix timestamp UTC.
    fecha: 'YYYY-MM-DD', hora: 'HH:MM' o 'HH:MM:SS' en zona Chile (UTC-4 / UTC-3 DST).
    Asume UTC-4 (hora de invierno) para ser conservador.
    """
    try:
        hora_clean = hora[:5] if hora else "12:00"
        dt_str = f"{fecha} {hora_clean}"
        # Chile UTC-4 (sin DST para simplificar; el error es < 1h, irrelevante para Meta)
        chile_offset = timezone(timedelta(hours=-4))
        dt_local = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=chile_offset)
        return int(dt_local.timestamp())
    except Exception:
        return int(time.time())


async def run_backfill(days: int, send: bool, force_resend: bool = False) -> None:
    from config import get_arancel_cpl
    import meta_capi

    log.info("=== Backfill CAPI Purchase con atribución ===")
    log.info("Ventana: %d días | Modo: %s%s", days, "ENVÍO REAL" if send else "DRY-RUN",
             " | FORCE-RESEND (ignora guard anti-duplicado)" if force_resend else "")

    citas = get_citas_con_atribucion(days)
    log.info("Citas con referral recuperadas del DB: %d", len(citas))

    if not citas:
        log.info("Sin citas con atribución disponibles. Nada que backfilliar.")
        return

    dentro = []
    fuera_antiguas = []
    fuera_futuras = []
    for c in citas:
        ok, motivo = is_within_meta_window(c["fecha"], c.get("hora") or "00:00", days)
        if ok:
            dentro.append(c)
        elif motivo == "futura":
            fuera_futuras.append(c)
        else:
            fuera_antiguas.append(c)

    log.info(
        "Dentro de ventana Meta (%dd): %d | Excluidas antiguas: %d | Excluidas futuras: %d",
        days, len(dentro), len(fuera_antiguas), len(fuera_futuras),
    )

    if fuera_futuras:
        log.info("Citas FUTURAS excluidas (aún no ocurrieron — NO enviar a Meta):")
        for c in fuera_futuras:
            log.info("  phone=%s fecha=%s hora=%s esp=%s — FUTURA, omitida",
                     _mask_phone(c["phone"]), c["fecha"],
                     c.get("hora") or "?", c.get("especialidad"))

    if fuera_antiguas:
        log.info("Citas ANTIGUAS fuera de ventana (solo informativo):")
        for c in fuera_antiguas:
            log.info("  phone=%s fecha=%s esp=%s — antigua, omitida",
                     _mask_phone(c["phone"]), c["fecha"], c.get("especialidad"))

    if not dentro:
        log.info("Ninguna cita dentro de la ventana de 7 días. Nada que enviar.")
        return

    enviadas = 0
    errores = 0
    saltadas_dup = 0

    for cita in dentro:
        phone = cita["phone"]
        fecha = cita["fecha"]
        hora = cita.get("hora") or "00:00"
        esp = cita.get("especialidad") or ""
        prof = cita.get("profesional") or ""
        nombre = cita.get("nombre") or ""
        rut = cita.get("rut") or None
        ctwa_clid = cita.get("ctwa_clid") or ""
        ad_id = cita.get("source_id") or "?"
        referral_ts_s = cita.get("referral_ts")  # segundos unix (mr.ts)
        value = float(get_arancel_cpl(esp))

        event_id = _deterministic_event_id(phone, fecha, esp)
        event_time = cita_fecha_to_unix(fecha, hora)

        # Timestamp del clic en ms para el fbc. Cap: el clic no puede ser
        # posterior a la atención (referral_ts > event_time es dato raro/corrupto).
        ctwa_clid_ts_ms: int | None = None
        if referral_ts_s is not None:
            capped_ts_s = min(int(referral_ts_s), event_time)
            ctwa_clid_ts_ms = capped_ts_s * 1000

        nom_parts = nombre.split()
        first_name = nom_parts[0] if nom_parts else None
        last_name = nom_parts[-1] if len(nom_parts) > 1 else None

        # GUARD ANTI-DUPLICADO: si el forward-path ya envió el Purchase de esta
        # atención, reenviar duplicaría la conversión en Meta (event_id distinto).
        if not force_resend and _ya_purchase_forward(phone, fecha):
            log.info(
                "[SKIP-DUP] phone=%s fecha=%s esp=%s — forward-path ya atribuyó esta "
                "atención; reenviar duplicaría la conversión. Usá --force-resend para forzar.",
                _mask_phone(phone), fecha, esp or "(sin esp)",
            )
            saltadas_dup += 1
            continue

        log.info(
            "[%s] phone=%s fecha=%s esp=%s value=%d ad_id=%s ctwa=%s... event_id=%s",
            "SEND" if send else "DRY-RUN",
            _mask_phone(phone),
            fecha,
            esp or "(sin esp)",
            int(value),
            ad_id,
            ctwa_clid[:12] if ctwa_clid else "N/A",
            event_id[:12],
        )

        if not send:
            continue

        try:
            result = await meta_capi.send_event(
                "Purchase",
                phone=phone,
                rut=rut,
                first_name=first_name,
                last_name=last_name,
                ctwa_clid=ctwa_clid or None,
                ctwa_clid_ts=ctwa_clid_ts_ms,  # ms del clic real, no now
                value=value,
                currency="CLP",
                event_id=event_id,
                event_time=event_time,  # retroactivo = timestamp real de la atención
                custom_data={
                    "content_name": esp,
                    "content_category": "medical_appointment",
                },
            )
            if result.get("error") or result.get("skipped"):
                log.warning("Error enviando phone=%s: %s", _mask_phone(phone), result)
                errores += 1
            else:
                log.info("OK → phone=%s events_received=%s",
                         _mask_phone(phone), result.get("events_received", "?"))
                enviadas += 1
        except Exception as e:
            log.error("Excepción enviando phone=%s: %s", _mask_phone(phone), e)
            errores += 1

        # Pequeña pausa para no saturar el rate limit de CAPI
        await asyncio.sleep(0.1)

    if send:
        log.info("=== Resultado: enviadas=%d saltadas_dup=%d errores=%d ===",
                 enviadas, saltadas_dup, errores)
    else:
        a_enviar = len(dentro) - saltadas_dup
        log.info("=== DRY-RUN completo: %d candidatos | %d ya atribuidos por forward-path (SKIP) | "
                 "%d realmente faltantes ===", len(dentro), saltadas_dup, a_enviar)
        if a_enviar > 0:
            log.info("Para enviar los faltantes: python scripts/backfill_capi_purchase_attribution.py --send")
        else:
            log.info("Nada que enviar: el forward-path ya atribuyó todas las atenciones. "
                     "Backfill innecesario.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill CAPI Purchase con ctwa_clid.")
    parser.add_argument(
        "--days", type=int, default=7,
        help="Ventana de días hacia atrás (default 7, máximo recomendado por Meta).",
    )
    parser.add_argument(
        "--send", action="store_true", default=False,
        help="Enviar eventos de verdad. Sin este flag solo hace dry-run.",
    )
    parser.add_argument(
        "--force-resend", action="store_true", default=False,
        help="Ignora el guard anti-duplicado y reenvía aunque el forward-path "
             "ya haya atribuido la atención. PELIGRO: duplica conversiones en Meta.",
    )
    args = parser.parse_args()

    if args.days > 7:
        log.warning(
            "Meta ignora/no atribuye eventos con event_time > 7 días. "
            "Procedeiendo igualmente con --days=%d.", args.days
        )

    asyncio.run(run_backfill(days=args.days, send=args.send, force_resend=args.force_resend))


if __name__ == "__main__":
    main()
