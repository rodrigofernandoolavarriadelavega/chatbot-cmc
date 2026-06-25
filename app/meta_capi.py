"""Meta Conversion API (CAPI) — server-side events.

Envía eventos de conversión directamente desde el servidor a Meta,
sin depender del pixel de navegador. Permite atribución precisa de
citas agendadas vía WhatsApp.

Eventos usados en CMC:
  Schedule             — cita confirmada en Medilink
  CompleteRegistration — paciente nuevo registrado
  Lead                 — paciente calificado (eligió especialidad)
  Purchase             — cita ocurrida (post-consulta enviado)
  Contact              — primer mensaje al bot (futuro)

Referencia: https://developers.facebook.com/docs/marketing-api/conversions-api
"""

import asyncio
import hashlib
import logging
import time
import unicodedata
import uuid
from typing import Any

import httpx

log = logging.getLogger("bot")

# ── Carga lazy de config para no romper si el módulo se importa antes de .env ──
def _cfg():
    try:
        from config import META_PIXEL_ID, META_CAPI_ACCESS_TOKEN, META_CAPI_TEST_EVENT_CODE
        return META_PIXEL_ID, META_CAPI_ACCESS_TOKEN, META_CAPI_TEST_EVENT_CODE
    except ImportError:
        import os
        pixel  = os.getenv("META_PIXEL_ID", "")
        token  = os.getenv("META_CAPI_ACCESS_TOKEN", "") or os.getenv("META_ACCESS_TOKEN", "")
        tecode = os.getenv("META_CAPI_TEST_EVENT_CODE", "")
        return pixel, token, tecode


# ── Helpers ────────────────────────────────────────────────────────────────────

def _sha256(s: str | None) -> str | None:
    """Hash SHA-256 hex lowercase de una cadena normalizada.

    Normalización:
    - strip() + lower()
    - sin diacríticos (á→a, é→e, ñ→n queda ñ — Meta lo documenta así para nombres en es-LA)
    - No hashea cadenas vacías (retorna None).
    """
    if not s or not s.strip():
        return None
    # Quitar diacríticos excepto ñ (NFD → quitar combining marks → recomponer)
    nfd = unicodedata.normalize("NFD", s.strip().lower())
    cleaned = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    cleaned = unicodedata.normalize("NFC", cleaned)
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def _normalize_phone(phone: str | None) -> str | None:
    """Normaliza teléfono a E.164 sin '+', prefijo Chile 56.

    Acepta: "+56966610737", "56966610737", "966610737", "9 6661 0737".
    Retorna None si no es parseable.
    """
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return None
    if digits.startswith("56") and len(digits) == 11:
        return digits
    if digits.startswith("9") and len(digits) == 9:
        return "56" + digits
    if len(digits) == 8:
        return "569" + digits
    if digits.startswith("56") and len(digits) >= 10:
        return digits[:11]
    return digits if digits else None


def _build_fbc(fbclid: str | None, fbclid_ts: int | None = None) -> str | None:
    """Construye fbc (Facebook Click ID Cookie) desde un fbclid."""
    if not fbclid:
        return None
    ts = fbclid_ts or int(time.time())
    return f"fb.1.{ts}.{fbclid}"


def _clean_none(d: dict) -> dict:
    """Elimina recursivamente keys con valor None de un dict.
    Meta rechaza arrays con None y campos nulos explícitos."""
    result = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, dict):
            cleaned = _clean_none(v)
            if cleaned:
                result[k] = cleaned
        elif isinstance(v, list):
            filtered = [x for x in v if x is not None]
            if filtered:
                result[k] = filtered
        else:
            result[k] = v
    return result


# ── Cliente httpx reutilizable ─────────────────────────────────────────────────

_client: httpx.AsyncClient | None = None

def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=5.0)
    return _client


# ── Función principal ──────────────────────────────────────────────────────────

async def send_event(
    event_name: str,
    phone: str,
    *,
    email: str | None = None,
    rut: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    fbclid: str | None = None,
    fbclid_ts: int | None = None,
    ctwa_clid: str | None = None,
    ctwa_clid_ts: int | None = None,
    fbp: str | None = None,
    value: float | None = None,
    currency: str = "CLP",
    event_id: str | None = None,
    event_time: int | None = None,
    custom_data: dict | None = None,
    action_source: str = "business_messaging",
) -> dict[str, Any]:
    """Envía un evento server-side a Meta CAPI. Async, retry 3x con backoff.

    Retorna la respuesta de Meta o {"error": ...} si falla.
    Si META_PIXEL_ID no está configurado, retorna {"skipped": "no_pixel_id"} inmediatamente.
    Nunca bloquea el flujo conversacional — usar con asyncio.create_task().
    """
    pixel_id, access_token, test_event_code = _cfg()

    if not pixel_id:
        return {"skipped": "no_pixel_id"}

    if not access_token:
        log.warning("CAPI: META_PIXEL_ID configurado pero META_CAPI_ACCESS_TOKEN vacío — no se puede enviar")
        return {"error": "no_access_token"}

    # Normalizar teléfono
    phone_norm = _normalize_phone(phone)
    if not phone_norm:
        log.warning("CAPI: teléfono inválido phone=%r — evento omitido", phone)
        return {"error": "invalid_phone"}

    eid = event_id or str(uuid.uuid4())

    # Construir user_data — al menos uno de ph/em/external_id requerido
    user_data: dict[str, Any] = {
        "ph": [_sha256(phone_norm)],
        "country": [_sha256("cl")],
    }
    if email:
        h = _sha256(email)
        if h:
            user_data["em"] = [h]
    if first_name:
        h = _sha256(first_name)
        if h:
            user_data["fn"] = [h]
    if last_name:
        h = _sha256(last_name)
        if h:
            user_data["ln"] = [h]
    if rut:
        # RUT como external_id: normalizar a solo dígitos + DV sin puntos/guión
        rut_clean = rut.strip().upper().replace(".", "").replace("-", "")
        h = _sha256(rut_clean)
        if h:
            user_data["external_id"] = [h]
    fbc = _build_fbc(fbclid, fbclid_ts)
    # ── Fallback de atribución CENTRAL ──────────────────────────────────────
    # Si ningún call-site pasó fbclid ni ctwa_clid, buscar el ctwa_clid en
    # meta_referrals por teléfono (ad CTWA, TTL 90d). Así TODO evento atribuye
    # sin depender de que cada call-site se acuerde de pasarlo (agenda_routes,
    # agendador_routes y cualquier camino futuro). Es ADITIVO: solo actúa cuando
    # no había atribución → nunca pisa una más precisa ni rompe la existente.
    if not fbc and not ctwa_clid:
        try:
            from session import get_meta_referral_fresh as _gmrf_central
            _ref_central = _gmrf_central(phone_norm, ttl_horas=2160) or {}
            _clid_central = (_ref_central.get("ctwa_clid") or "").strip()
            if _clid_central:
                ctwa_clid = _clid_central
                # ts REAL del clic → el fbc lleva la hora del clic, no 'now'.
                # Crítico para eventos backdateados (batch Purchase 22:00): si el
                # fbc quedara con ts > event_time, Meta DESCARTA la atribución en
                # silencio. Con el ts del referral el fbc siempre precede al evento.
                _ref_ts_central = _ref_central.get("ts")
                if _ref_ts_central and ctwa_clid_ts is None:
                    try:
                        ctwa_clid_ts = int(_ref_ts_central) * 1000
                    except (ValueError, TypeError):
                        pass
                log.debug("CAPI %s: ctwa_clid recuperado de meta_referrals (fallback central)", event_name)
        except Exception as _e_central:
            log.debug("CAPI: fallback central ctwa_clid lookup falló: %s", _e_central)
    if not fbc and ctwa_clid:
        # ctwa_clid es el ID nativo de Click-to-WhatsApp ads.
        # Meta lo acepta como fbc con el mismo formato que fbclid.
        # Solo se usa si no viene fbclid (no pisar atribución más precisa).
        # ctwa_clid_ts debe estar en ms (epoch*1000); si no viene, cae a now.
        # En backfill es el ts real del clic → evita que el fbc quede con
        # timestamp posterior al event_time (Meta descarta esa atribución).
        _fbc_ts_ms = ctwa_clid_ts if ctwa_clid_ts is not None else int(time.time() * 1000)
        fbc = f"fb.1.{_fbc_ts_ms}.{ctwa_clid}"
    if fbc:
        user_data["fbc"] = fbc
    if fbp:
        user_data["fbp"] = fbp

    # Construir evento
    event: dict[str, Any] = {
        "event_name": event_name,
        "event_time": event_time if event_time is not None else int(time.time()),
        "event_id": eid,
        "action_source": action_source,
        "user_data": user_data,
    }
    # messaging_channel solo aplica a eventos de mensajería (no a offline/physical_store)
    if action_source == "business_messaging":
        event["messaging_channel"] = "whatsapp"

    if value is not None or custom_data:
        cd: dict[str, Any] = {"currency": currency}
        if value is not None:
            cd["value"] = value
        if custom_data:
            cd.update(custom_data)
        event["custom_data"] = cd

    # Construir payload
    payload: dict[str, Any] = {
        "data": [event],
        "access_token": access_token,
    }
    if test_event_code:
        payload["test_event_code"] = test_event_code

    # Limpiar nulos antes de enviar
    payload = _clean_none(payload)

    # Endpoint
    url = f"https://graph.facebook.com/v22.0/{pixel_id}/events"

    backoffs = [0.5, 1.0, 2.0]
    last_error = ""

    for attempt in range(3):
        try:
            client = _get_client()
            r = await client.post(url, json=payload)

            if r.status_code == 200:
                try:
                    resp = r.json()
                except Exception:
                    resp = {}
                ms = resp.get("events_received", "?")
                score = resp.get("quality_score", {})
                # Observabilidad de atribución: registrar SI el evento llevó click-id.
                # Sin esto, "0 eventos con fbc" en logs es ambiguo (¿no se adjuntó o no
                # se logueó?). Hace auditable el cierre del loop CAC de un solo grep.
                _attr = "fbc" if user_data.get("fbc") else "none"
                log.info(
                    "CAPI %s event_id=%s received=%s attr=%s quality=%s test_code=%s",
                    event_name, eid[:8], ms, _attr,
                    score if score else "n/a",
                    test_event_code or "off",
                )
                try:
                    from session import log_event as _le_ok
                    _le_ok(phone, "capi_send_ok", {
                        "event_type": event_name,
                        "value": value,
                        "currency": currency,
                        "content_name": (custom_data or {}).get("content_name"),
                        "events_received": ms,
                        "event_id": eid[:16],
                        "attribution": _attr,  # "fbc" si llevó click-id, "none" si no
                    })
                except Exception as _e_ok:
                    log.debug("CAPI: no se pudo loggear capi_send_ok: %s", _e_ok)
                return resp

            # 4xx no-transitorio: no reintentar
            if 400 <= r.status_code < 500 and r.status_code != 429:
                try:
                    err_body = r.json()
                except Exception:
                    err_body = {"raw": r.text[:300]}
                log.error(
                    "CAPI %s HTTP %s (no-retry): %s",
                    event_name, r.status_code, err_body
                )
                last_error = f"http_{r.status_code}"
                break  # no reintentar 4xx

            # 429 / 5xx: transitorio
            try:
                err_body = r.json()
            except Exception:
                err_body = r.text[:200]
            log.warning(
                "CAPI %s intento %d → HTTP %s: %s",
                event_name, attempt + 1, r.status_code, err_body
            )
            last_error = f"http_{r.status_code}"

        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.warning("CAPI %s intento %d error red: %s", event_name, attempt + 1, e)
            last_error = str(e)[:100]

        if attempt < len(backoffs):
            await asyncio.sleep(backoffs[attempt])

    # Todos los intentos fallaron — loggear en sessions.db sin bloquear
    try:
        from session import log_event as _le
        _le(phone, "capi_send_failed", {
            "event_type": event_name,
            "event_id": eid[:16],
            "error": last_error,
        })
    except Exception as e:
        log.debug("CAPI: no se pudo loggear capi_send_failed: %s", e)

    return {"error": last_error, "event_name": event_name}
