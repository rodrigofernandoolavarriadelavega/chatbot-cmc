"""
app/google_ads.py — Atribución Google Ads: clic web -> WhatsApp -> cita -> conversión offline.

Circuito (mismo patrón que Meta CAPI con ctwa_clid, pero Google no entrega el clic
por WhatsApp, así que el sitio lo codifica en el texto del wa.me):

  1. Landing con ?gclid=/gbraid=/wbraid= -> JS (static/cmc-gads.js) genera un código
     corto y hace POST /api/gclick {code, gclid, landing, ts}  -> tabla google_clicks.
     Los wa.me llevan el marcador "(web: <página> · <artículo> · <botón> · g-<code>)".
  2. flows.handle_message detecta g-<code>: tag referral_source:google_ads y
     vincula phone <-> code (vincular_phone). El marcador se limpia igual que (web:).
  3. save_cita_bot -> vincular_cita: la cita queda con el clic del teléfono (≤90 días,
     que es la ventana que acepta Google).
  4. capi_purchase.enviar_purchases (mismo gatillo: cita ATENDIDA + ticket real) llama
     encolar_conversion(); subir_pendientes() sube a ConversionUploadService.
     UploadClickConversions con order_id = id_cita (idempotente). Apagado por
     GOOGLE_ADS_OFFLINE_ENABLED=0; sin credenciales no hace nada y no rompe.

Reglas: ninguna función de aquí lanza hacia el flujo de agenda (todo es best-effort);
ninguna conexión SQLite se mantiene abierta durante una llamada de red (GIL x SQLite,
ver cmc_deadlock_gil_sqlite_2026_06_10).
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("bot.google_ads")

_CLT = ZoneInfo("America/Santiago")
VENTANA_DIAS = 90
MAX_INTENTOS = 5

# Sin ambigüedades: sin 0/O, 1/I/L.
ALFABETO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
RE_CODE = re.compile(r"^[A-HJ-KM-NP-Z2-9]{4,6}$")
RE_CLICK_ID = re.compile(r"^[A-Za-z0-9_\-]{4,512}$")
RE_LANDING = re.compile(r"^/[A-Za-z0-9_\-/.]{0,119}$")
_TIPOS = ("gclid", "gbraid", "wbraid")


# ── Paso 1: registrar el clic ────────────────────────────────────────────────

def validar_payload(body: dict) -> tuple[dict | None, str]:
    """Valida el JSON de POST /api/gclick. Devuelve (datos_limpios, '') o (None, motivo).
    Nunca devuelve nada que permita leer clics ajenos."""
    if not isinstance(body, dict):
        return None, "body"
    code = str(body.get("code") or "").strip().upper()
    if not RE_CODE.match(code):
        return None, "code"
    presentes = [(t, str(body.get(t) or "").strip()) for t in _TIPOS if body.get(t)]
    if len(presentes) != 1:
        return None, "click_id"
    tipo, cid = presentes[0]
    if not RE_CLICK_ID.match(cid):
        return None, "click_id"
    landing = str(body.get("landing") or "").strip().split("?")[0].split("#")[0]
    if landing and not RE_LANDING.match(landing):
        return None, "landing"
    return {"code": code, "click_type": tipo, "click_id": cid, "landing": landing}, ""


def registrar_click(code: str, click_type: str, click_id: str, landing: str = "",
                    ts: int | None = None) -> str:
    """Inserta el clic. 'ok' nuevo, 'dup' si el mismo código ya existe con el mismo clic
    (idempotente: el JS reintenta), 'conflict' si el código ya es de OTRO clic."""
    from session import db
    ahora = int(time.time())
    with db() as c:
        row = c.execute("SELECT click_type, click_id FROM google_clicks WHERE code=?", (code,)).fetchone()
        if row:
            return "dup" if (row["click_type"], row["click_id"]) == (click_type, click_id) else "conflict"
        c.execute(
            "INSERT INTO google_clicks (code, click_type, click_id, landing, ts) VALUES (?,?,?,?,?)",
            (code, click_type, click_id, (landing or "")[:120], ahora))
    return "ok"


# ── Paso 2: vincular teléfono <-> clic ───────────────────────────────────────

def extraer_codigo(partes: list[str]) -> tuple[str, list[str]]:
    """De las partes del marcador (ya en minúscula) saca `g-<code>`.
    Devuelve (CODE en mayúscula o '', partes sin el g-)."""
    code, resto = "", []
    for p in partes:
        m = re.fullmatch(r"g-([a-z0-9]{4,6})", p)
        if m and not code:
            code = m.group(1).upper()
        else:
            resto.append(p)
    return code, resto


def vincular_phone(phone: str, code: str) -> dict | None:
    """Une phone con el clic. Devuelve {'click_type','click_id','ts'} si el código existe
    y es de este teléfono (o estaba libre); None si no existe o ya es de otro teléfono
    (un link compartido no debe robar la atribución del primero)."""
    from session import db
    if not phone or not code:
        return None
    with db() as c:
        row = c.execute("SELECT click_type, click_id, ts, phone FROM google_clicks WHERE code=?",
                        (code,)).fetchone()
        if not row:
            return None
        if row["phone"] and _misma_linea(row["phone"], phone) is False:
            return None
        if not row["phone"]:
            c.execute("UPDATE google_clicks SET phone=?, linked_ts=? WHERE code=? AND phone IS NULL",
                      (phone, int(time.time()), code))
    return {"click_type": row["click_type"], "click_id": row["click_id"], "ts": row["ts"]}


def _digitos9(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())[-9:]


def _misma_linea(a: str, b: str) -> bool:
    return bool(_digitos9(a)) and _digitos9(a) == _digitos9(b)


# ── Paso 3: vincular la cita ─────────────────────────────────────────────────

def click_para_phone(phone: str, hasta_ts: int, ttl_dias: int = VENTANA_DIAS) -> dict | None:
    """Último clic de Google vinculado al teléfono en los `ttl_dias` previos a `hasta_ts`."""
    from session import db
    clave = _digitos9(phone)
    if not clave or (phone or "").startswith(("fb_", "ig_")):
        return None
    with db() as c:
        row = c.execute(
            """SELECT code, click_type, click_id, ts FROM google_clicks
               WHERE phone IS NOT NULL
                 AND substr(replace(replace(replace(phone,'+',''),' ',''),'-',''), -9) = ?
                 AND ts <= ? AND ts >= ?
               ORDER BY ts DESC LIMIT 1""",
            (clave, hasta_ts, hasta_ts - ttl_dias * 86400)).fetchone()
    return dict(row) if row else None


def vincular_cita(phone: str, id_cita: str) -> bool:
    """Marca la cita con el clic de Google del teléfono (≤90 días). Best-effort."""
    from session import db
    clic = click_para_phone(phone, int(time.time()))
    if not clic:
        return False
    with db() as c:
        c.execute(
            """UPDATE citas_bot SET gads_code=?, gads_click_type=?, gads_click_id=?, gads_click_ts=?
               WHERE id_cita=? AND phone=?""",
            (clic["code"], clic["click_type"], clic["click_id"], clic["ts"], str(id_cita), phone))
    return True


# ── Paso 4: conversión offline ───────────────────────────────────────────────

def _cfg():
    import config
    return config


def habilitado() -> bool:
    return bool(getattr(_cfg(), "GOOGLE_ADS_OFFLINE_ENABLED", False))


def _credenciales() -> dict | None:
    c = _cfg()
    cred = {
        "developer_token": c.GOOGLE_ADS_DEVELOPER_TOKEN, "client_id": c.GOOGLE_ADS_CLIENT_ID,
        "client_secret": c.GOOGLE_ADS_CLIENT_SECRET, "refresh_token": c.GOOGLE_ADS_REFRESH_TOKEN,
        "customer_id": re.sub(r"\D", "", c.GOOGLE_ADS_CUSTOMER_ID or ""),
        "action_id": re.sub(r"\D", "", c.GOOGLE_ADS_CONVERSION_ACTION_ID or ""),
        "login_customer_id": re.sub(r"\D", "", c.GOOGLE_ADS_LOGIN_CUSTOMER_ID or ""),
        "api_version": c.GOOGLE_ADS_API_VERSION,
    }
    obligatorias = ("developer_token", "client_id", "client_secret", "refresh_token",
                    "customer_id", "action_id")
    return cred if all(cred[k] for k in obligatorias) else None


def encolar_conversion(item: dict) -> bool:
    """Registra en la cola la cita ATENDIDA con su ticket real, si tiene clic de Google.
    Se llama desde capi_purchase por cada item 'enviar' (mismo gatillo que Purchase de Meta).
    Se encola aunque el flag esté apagado: al activarlo se sube lo pendiente (≤90 días)."""
    from session import db
    try:
        id_cita = str(item["id_cita"])
        with db() as c:
            row = c.execute(
                "SELECT gads_click_type, gads_click_id, gads_click_ts FROM citas_bot "
                "WHERE id_cita=? AND gads_click_id IS NOT NULL LIMIT 1", (id_cita,)).fetchone()
            if not row:
                return False
            hora = (item.get("hora") or "00:00")[:5]
            conv_ts = int(datetime.fromisoformat(f"{item['fecha']}T{hora}").replace(tzinfo=_CLT).timestamp())
            c.execute(
                """INSERT OR IGNORE INTO google_ads_uploads
                   (id_cita, phone, click_type, click_id, click_ts, conv_ts, value)
                   VALUES (?,?,?,?,?,?,?)""",
                (id_cita, item.get("phone"), row["gads_click_type"], row["gads_click_id"],
                 row["gads_click_ts"], conv_ts, float(item.get("value") or 0)))
        return True
    except Exception as e:
        log.warning("google_ads.encolar_conversion cita %s: %s", item.get("id_cita"), e)
        return False


def formato_fecha_google(ts: int) -> str:
    """'yyyy-mm-dd hh:mm:ss+|-hh:mm' en America/Santiago (con offset, como exige la API)."""
    dt = datetime.fromtimestamp(ts, _CLT)
    off = dt.strftime("%z")                      # -0300
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f"{off[:3]}:{off[3:]}"


def construir_conversion(fila: dict, cred: dict) -> dict:
    """Un ClickConversion (JSON REST). gclid/gbraid/wbraid según el tipo de clic."""
    conv_ts = max(int(fila["conv_ts"]), int(fila["click_ts"] or 0) + 60)   # nunca antes del clic
    conv = {
        fila["click_type"]: fila["click_id"],
        "conversionAction": f"customers/{cred['customer_id']}/conversionActions/{cred['action_id']}",
        "conversionDateTime": formato_fecha_google(conv_ts),
        "conversionValue": float(round(fila["value"] or 0)),
        "currencyCode": "CLP",
        "orderId": str(fila["id_cita"]),
    }
    return conv


_token_cache: dict = {"tok": "", "exp": 0.0}


async def _access_token(cli, cred: dict) -> str:
    if _token_cache["tok"] and time.time() < _token_cache["exp"] - 60:
        return _token_cache["tok"]
    r = await cli.post("https://oauth2.googleapis.com/token", data={
        "client_id": cred["client_id"], "client_secret": cred["client_secret"],
        "refresh_token": cred["refresh_token"], "grant_type": "refresh_token"})
    r.raise_for_status()
    j = r.json()
    _token_cache.update(tok=j["access_token"], exp=time.time() + int(j.get("expires_in", 3600)))
    return _token_cache["tok"]


def _errores_por_indice(resp: dict) -> dict[int, str]:
    """partialFailureError.details[].errors[] -> {indice_conversion: 'CODIGO: mensaje'}."""
    out: dict[int, str] = {}
    pf = resp.get("partialFailureError") or {}
    for d in pf.get("details") or []:
        for e in d.get("errors") or []:
            idx = None
            for el in (e.get("location") or {}).get("fieldPathElements") or []:
                if el.get("fieldName") == "conversions" and "index" in el:
                    idx = int(el["index"])
            cod = json.dumps(e.get("errorCode") or {}, ensure_ascii=False)
            out[idx if idx is not None else -1] = f"{cod}: {e.get('message', '')}"[:300]
    return out


def _marcar(id_cita: str, estado: str, error: str | None = None, suma_intento: bool = True) -> None:
    from session import db
    with db() as c:
        c.execute(
            """UPDATE google_ads_uploads SET estado=?, ultimo_error=?,
                      intentos = intentos + ?, sent_at = CASE WHEN ?='sent' THEN datetime('now') ELSE sent_at END
               WHERE id_cita=?""",
            (estado, error, 1 if suma_intento else 0, estado, id_cita))


async def subir_pendientes(max_lote: int = 200) -> dict:
    """Sube las conversiones pendientes. Sin flag o sin credenciales: no hace nada.
    Nunca lanza."""
    res = {"habilitado": habilitado(), "subidas": 0, "errores": 0, "expiradas": 0, "pendientes": 0}
    try:
        if not res["habilitado"]:
            return res
        cred = _credenciales()
        if not cred:
            log.warning("google_ads: GOOGLE_ADS_OFFLINE_ENABLED=1 pero faltan credenciales; no se sube nada")
            return res
        from session import db
        ahora = int(time.time())
        with db() as c:
            filas = [dict(r) for r in c.execute(
                "SELECT * FROM google_ads_uploads WHERE estado='pendiente' AND intentos < ? "
                "ORDER BY conv_ts LIMIT ?", (MAX_INTENTOS, max_lote))]
        vigentes = []
        for f in filas:
            if ahora - int(f["click_ts"] or 0) > VENTANA_DIAS * 86400:
                _marcar(f["id_cita"], "expirada", "clic >90 días", suma_intento=False)
                res["expiradas"] += 1
            else:
                vigentes.append(f)
        res["pendientes"] = len(vigentes)
        if not vigentes:
            return res
        import httpx
        async with httpx.AsyncClient(timeout=30) as cli:
            try:
                tok = await _access_token(cli, cred)
                headers = {"Authorization": f"Bearer {tok}", "developer-token": cred["developer_token"],
                           "Content-Type": "application/json"}
                if cred["login_customer_id"]:
                    headers["login-customer-id"] = cred["login_customer_id"]
                url = (f"https://googleads.googleapis.com/{cred['api_version']}/customers/"
                       f"{cred['customer_id']}:uploadClickConversions")
                r = await cli.post(url, headers=headers, json={
                    "conversions": [construir_conversion(f, cred) for f in vigentes],
                    "partialFailure": True})
            except Exception as e:
                # Red / OAuth / credenciales: no quema intentos de las conversiones.
                log.warning("google_ads: subida falló (reintenta mañana): %s", e)
                res["errores"] += len(vigentes)
                return res
        if r.status_code != 200:
            log.warning("google_ads: HTTP %s %s", r.status_code, r.text[:300])
            if r.status_code == 400:
                for f in vigentes:
                    _marcar(f["id_cita"], "pendiente", f"http_400 {r.text[:200]}")
            res["errores"] += len(vigentes)
            return res
        errores = _errores_por_indice(r.json())
        for i, f in enumerate(vigentes):
            err = errores.get(i)
            if err is None and -1 in errores:
                err = errores[-1]
            if err is None:
                _marcar(f["id_cita"], "sent")
                res["subidas"] += 1
            elif "DUPLICATE_ORDER_ID" in err:       # ya estaba subida: idempotencia
                _marcar(f["id_cita"], "sent", err)
                res["subidas"] += 1
            else:
                hecho = f["intentos"] + 1 >= MAX_INTENTOS
                _marcar(f["id_cita"], "fallido" if hecho else "pendiente", err)
                res["errores"] += 1
    except Exception as e:                            # jamás tumbar el job de las 07:07
        log.warning("google_ads.subir_pendientes: %s", e)
    log.info("google_ads_upload: %s", res)
    return res
