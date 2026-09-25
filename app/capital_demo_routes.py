"""capital_demo_routes.py — endpoints internos para la bandeja de recepción
de Alma Capital sobre el desvío DEMO Capital Travel (ver app/capital_demo.py).

Todo el dominio agentecmc.cl hace proxy a este proceso (nginx), así que estas
rutas quedarían públicas si no se protegen aparte. Doble guarda en
`_solo_local_con_secreto`, aplicada a las 4 rutas:
  (a) header `X-Alma-Secret` == env CAPITAL_WA_SECRET, comparado con
      `hmac.compare_digest` — sin la env seteada, 404 siempre (fail-closed,
      mismo criterio que el resto del desvío).
  (b) 404 si la request trae `X-Forwarded-For` o `X-Real-IP` — nginx los
      setea SIEMPRE en tráfico proxied desde internet (confirmado en la
      config del server); las llamadas legítimas de Alma Capital son locales
      directas a 127.0.0.1:8001, sin pasar por nginx.
Se responde 404 (no 401/403) en ambos casos para no delatar que la ruta
existe a quien no tenga el secreto.
"""
import hmac
import logging
import os

from fastapi.responses import JSONResponse
from fastapi import APIRouter, HTTPException, Request

import capital_demo
from messaging import send_whatsapp, ventana_cerrada_ultimo_envio
from session import get_messages

log = logging.getLogger("capital_demo_routes")

router = APIRouter()


def _capital_wa_secret() -> str:
    return os.getenv("CAPITAL_WA_SECRET", "").strip()


def _solo_local_con_secreto(request: Request) -> None:
    secret = _capital_wa_secret()
    if not secret:
        raise HTTPException(status_code=404)
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        raise HTTPException(status_code=404)
    recibido = request.headers.get("x-alma-secret", "")
    if not hmac.compare_digest(recibido, secret):
        raise HTTPException(status_code=404)


def _ts_a_iso_utc(ts: str) -> str:
    """Los timestamps de `messages.ts` son `datetime('now')` de SQLite — UTC
    naive ('2026-09-25 06:26:00'). Se devuelven como ISO8601 con zona UTC."""
    if not ts:
        return ts
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(ts).replace(" ", "T")).replace(tzinfo=ZoneInfo("UTC"))
        return dt.isoformat()
    except Exception:  # noqa: BLE001
        return ts


def _autor_de_fila(m: dict) -> str:
    if m.get("direction") == "in":
        return "cliente"
    return "operador" if m.get("state") == "CAPITAL_DEMO_OPERADOR" else "bot"


@router.post("/internal/capital-demo/enviar")
async def capital_demo_enviar(request: Request) -> dict:
    _solo_local_con_secreto(request)
    body = await request.json()
    telefono = str(body.get("telefono") or "").strip().lstrip("+")
    texto = str(body.get("texto") or "").strip()
    if not telefono or not texto:
        raise HTTPException(status_code=400, detail="telefono y texto son requeridos")
    if not capital_demo.activo(telefono):
        raise HTTPException(status_code=403, detail="telefono fuera de la demo o demo vencida")

    wamid = await send_whatsapp(telefono, texto)
    if wamid is None and ventana_cerrada_ultimo_envio():
        return _json_ventana_cerrada()

    if wamid is None:
        # Meta no lo aceptó: no se registra como enviado (el panel de Capital no
        # debe mostrar como mandado algo que no salió).
        return JSONResponse({"error": "envio_fallido"}, status_code=502)
    await capital_demo._log(telefono, "out", texto, autor="operador",
                             wamid=wamid, state="CAPITAL_DEMO_OPERADOR")
    return {"ok": True, "wamid": wamid}


def _json_ventana_cerrada():
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "ventana_cerrada"}, status_code=409)


@router.post("/internal/capital-demo/modo")
async def capital_demo_modo(request: Request) -> dict:
    _solo_local_con_secreto(request)
    body = await request.json()
    telefono = str(body.get("telefono") or "").strip().lstrip("+")
    modo = str(body.get("modo") or "").strip().lower()
    if not telefono or modo not in ("humano", "bot"):
        raise HTTPException(status_code=400, detail="telefono y modo ('humano'|'bot') son requeridos")
    capital_demo.set_modo(telefono, modo)
    return {"ok": True, "telefono": telefono, "modo": modo}


@router.get("/internal/capital-demo/estado")
async def capital_demo_estado(request: Request, telefono: str = "") -> dict:
    _solo_local_con_secreto(request)
    telefono = telefono.strip().lstrip("+")
    if not telefono:
        raise HTTPException(status_code=400, detail="telefono es requerido")
    return {
        "modo": "humano" if capital_demo.es_modo_humano(telefono) else "bot",
        "flyer_enviado": not capital_demo._es_primera_vez(telefono),
    }


@router.get("/internal/capital-demo/historial")
async def capital_demo_historial(request: Request, telefono: str = "", limit: int = 300) -> dict:
    _solo_local_con_secreto(request)
    telefono = telefono.strip().lstrip("+")
    if not telefono:
        raise HTTPException(status_code=400, detail="telefono es requerido")
    limit = max(1, min(limit, 2000))
    filas = [m for m in get_messages(telefono, limit) if m.get("canal") == "capital_demo"]
    mensajes = [
        {
            "telefono": telefono,
            "direccion": m.get("direction"),
            "autor": _autor_de_fila(m),
            "texto": m.get("text") or "",
            "tipo": "text",
            "wamid": m.get("wamid"),
            "ts": _ts_a_iso_utc(m.get("ts")),
        }
        for m in filas
    ]
    return {"telefono": telefono, "mensajes": mensajes}
