"""Puente chatbot → Copiloto de Ficha (ficha.agentecmc.cl, :8965 local).

Pedido del Dr. (2026-08-01): los resultados de exámenes que llegan por
WhatsApp deben aparecer PRE-CARGADOS en el Copiloto ~15 minutos antes de la
cita, para que la ficha "se llene sola" y solo haya que revisarla/moverla.

Flujo (cron cada 4 min, gated COPILOTO_PRELOAD_ACTIVE):
  1. Citas de HOY en `citas_bot` cuyo profesional calce con
     COPILOTO_PRELOAD_PROFS (default "Olavarr") y cuya hora esté dentro de
     los próximos 15 minutos.
  2. Para cada una: resultados de ese teléfono aún no cargados
     (docs_clinicos.resultados_pendientes).
  3. POST /api/fichas al Copiloto con paciente + transcripciones en
     `enfermedad_actual` (bloque rotulado "EXÁMENES RECIBIDOS POR WHATSAPP…
     verificar contra la imagen") y motivo "Revisión de exámenes".
  4. Se marca cargado (idempotente — jamás duplica fichas).

Auth: el Copiloto exige Bearer; se canjea COPILOTO_CLAVE por token de 12 h
vía POST /api/ext/token (mismo mecanismo que la extensión Alma Pluma) y se
cachea en memoria ~11 h.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

log = logging.getLogger("copiloto_bridge")

_URL = os.getenv("COPILOTO_URL", "http://127.0.0.1:8965").rstrip("/")
_TOKEN_CACHE: dict = {"token": None, "ts": 0.0}
_TOKEN_TTL = 11 * 3600  # el token del Copiloto dura 12 h


async def _token(client: httpx.AsyncClient) -> str | None:
    if _TOKEN_CACHE["token"] and time.time() - _TOKEN_CACHE["ts"] < _TOKEN_TTL:
        return _TOKEN_CACHE["token"]
    clave = os.getenv("COPILOTO_CLAVE", "").strip()
    try:
        r = await client.post(f"{_URL}/api/ext/token", json={"clave": clave})
        if r.status_code != 200:
            log.warning("copiloto token: %s %s", r.status_code, r.text[:120])
            return None
        tok = r.json().get("token")
        _TOKEN_CACHE.update(token=tok, ts=time.time())
        return tok
    except Exception as e:  # noqa: BLE001
        log.warning("copiloto token error: %s", e)
        return None


async def crear_ficha_examenes(nombre: str, rut: str,
                               resultados: list[dict]) -> int | None:
    """Crea la ficha pre-cargada en el Copiloto. Retorna ficha_id o None."""
    bloques = []
    for r in resultados:
        cab = f"■ {r.get('titulo') or 'Examen'} — recibido por WhatsApp {str(r.get('created_at') or '')[:10]}"
        bloques.append(cab + "\n" + (r.get("contenido") or "(ver foto en el panel)"))
    cuerpo = (
        "— EXÁMENES RECIBIDOS POR WHATSAPP —\n"
        "(transcripción automática: VERIFICAR contra la imagen original)\n\n"
        + "\n\n".join(bloques)
    )
    payload = {
        "paciente_nombre": nombre or "",
        "paciente_rut": rut or "",
        "secciones": {
            "motivo_consulta": "Revisión de exámenes (resultados recibidos por WhatsApp).",
            "enfermedad_actual": cuerpo[:8000],
        },
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            tok = await _token(client)
            if tok is None:
                return None
            r = await client.post(f"{_URL}/api/fichas", json=payload,
                                  headers={"Authorization": f"Bearer {tok}"})
            if r.status_code != 200:
                log.warning("copiloto crear ficha: %s %s",
                            r.status_code, r.text[:150])
                return None
            return r.json().get("id")
    except Exception as e:  # noqa: BLE001
        log.warning("copiloto crear ficha error: %s", e)
        return None


async def precargar_para_citas() -> dict:
    """Job: pre-carga en el Copiloto los resultados de pacientes cuya cita
    (con los profesionales configurados) empieza en los próximos 15 min."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from session import db, log_event
    from docs_clinicos import resultados_pendientes, marcar_resultado_cargado

    profs = [p.strip() for p in os.getenv(
        "COPILOTO_PRELOAD_PROFS", "Olavarr").split(",") if p.strip()]
    ahora = datetime.now(ZoneInfo("America/Santiago"))
    desde = ahora.strftime("%H:%M")
    hasta = (ahora + timedelta(minutes=15)).strftime("%H:%M")
    hoy = ahora.strftime("%Y-%m-%d")

    with db() as conn:
        filtro_prof = " OR ".join("profesional LIKE ?" for _ in profs)
        citas = conn.execute(
            f"SELECT phone, paciente_nombre, hora, profesional FROM citas_bot "
            f"WHERE fecha=? AND hora >= ? AND hora <= ? AND ({filtro_prof})",
            [hoy, desde, hasta] + [f"%{p}%" for p in profs]).fetchall()

    cargadas = 0
    for cita in citas:
        phone = cita[0]
        pendientes = resultados_pendientes(phone)
        if not pendientes:
            continue
        nombre = cita[1] or (pendientes[0].get("paciente_nombre") or "")
        rut = next((p.get("paciente_rut") for p in pendientes
                    if p.get("paciente_rut")), "")
        ficha_id = await crear_ficha_examenes(nombre, rut, pendientes)
        if not ficha_id:
            continue
        for p in pendientes:
            marcar_resultado_cargado(p["id"], ficha_id)
        cargadas += 1
        log_event(phone, "copiloto_ficha_precargada", {
            "ficha_id": ficha_id, "resultados": len(pendientes),
            "cita": f"{cita[2]} {cita[3]}"})
        log.info("copiloto: ficha %s precargada (%d resultados) para cita %s",
                 ficha_id, len(pendientes), cita[2])
    return {"citas_revisadas": len(citas), "fichas_creadas": cargadas}
