"""Preparación pre-examen de ecografía — riel UTILITY (valor clínico, no ads).

Problema: cada paciente que llega sin ayuno / sin vejiga llena es una hora
perdida del ecógrafo y un examen a re-agendar.

Mecánica: barrido HORARIO de las citas de David Pardo (ID 68 — ecografista,
TODAS sus citas son ecos) en una ventana móvil de HOY a HOY+2 días:
  - Agendó hoy para mañana → el mensaje le llega dentro de la hora.
  - Agendó hace 2 semanas → le llega cuando el examen entra a la ventana
    (≈ el día antes, que es cuando la preparación importa).

Como el template tiene texto fijo, va la preparación por tipo (ayuno /
vejiga llena / sin preparación) — mismo contenido conservador que
ecografias.prep_ayuno_eco().

Guardrails:
  - Gated ECO_PREP_ACTIVE (default OFF) + override en vivo vía alma_switchboard.
  - Template UTILITY `preparacion_ecografia` debe estar APPROVED en Meta
    (el job lo verifica; si no, queda inerte).
  - 1 mensaje por id_cita, para siempre (tabla eco_prep_sends).
  - Cap por corrida + 20s entre envíos + ventana horaria 08-21 CLT.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import JSONResponse

log = logging.getLogger("bot")

router = APIRouter(tags=["eco-prep"])

# David Pardo — ecografista (ver tabla PROFESIONALES en CLAUDE.md/medilink.py).
ECO_PROFESIONAL_ID = 68

TEMPLATE_NAME = "preparacion_ecografia"

_EVENT_ENVIADA = "eco_prep_enviada"


def _active() -> bool:
    env_val = os.getenv("ECO_PREP_ACTIVE", "false").lower() in ("true", "1", "yes")
    try:
        from alma_switchboard import effective
        return effective("ECO_PREP_ACTIVE", env_val)
    except Exception:
        return env_val


def _cfg():
    return {
        "cap": int(os.getenv("ECO_PREP_CAP", "20")),
        "dias_ventana": int(os.getenv("ECO_PREP_DIAS_VENTANA", "3")),  # hoy..+2
    }


# ── tracking (1 mensaje por cita) ────────────────────────────────────────────

def _ensure_table() -> None:
    from session import db
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS eco_prep_sends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_cita TEXT NOT NULL UNIQUE,
                phone TEXT,
                fecha_cita TEXT,
                sent_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        conn.commit()


def _citas_ya_enviadas() -> set[str]:
    from session import db
    _ensure_table()
    with db() as conn:
        rows = conn.execute("SELECT id_cita FROM eco_prep_sends").fetchall()
    return {str(r[0]) for r in rows}


def _registrar_envio(id_cita: str, phone: str, fecha_cita: str) -> None:
    from session import db
    _ensure_table()
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO eco_prep_sends (id_cita, phone, fecha_cita) VALUES (?, ?, ?)",
            (str(id_cita), phone, fecha_cita),
        )
        conn.commit()


# ── citas de eco en la ventana ───────────────────────────────────────────────

async def _citas_eco_ventana(dias: int) -> list[dict]:
    """Citas vigentes de David Pardo entre HOY y HOY+dias-1 (Medilink en vivo,
    1 request por día — ventana corta a propósito para no gatillar 429)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from medilink import _get_shared_client, _q, _safe_json, HEADERS
    from config import MEDILINK_BASE_URL

    hoy = datetime.now(ZoneInfo("America/Santiago")).date()
    cli = _get_shared_client()
    citas: list[dict] = []
    for d in range(dias):
        fecha = (hoy + timedelta(days=d)).strftime("%Y-%m-%d")
        try:
            r = await cli.get(
                f"{MEDILINK_BASE_URL}/citas",
                params={"q": _q({"fecha": {"eq": fecha}, "estado_anulacion": {"eq": 0}})},
                headers=HEADERS, timeout=15)
            data = _safe_json(r).get("data", []) if r.status_code == 200 else []
        except Exception as e:
            log.warning("eco_prep: citas %s falló: %s", fecha, e)
            data = []
        for c in data:
            if str(c.get("id_profesional") or "") == str(ECO_PROFESIONAL_ID):
                c["_fecha"] = fecha
                citas.append(c)
        await asyncio.sleep(1)  # respiro entre días (429-safe)
    return citas


def _fecha_legible(fecha_iso: str, hora: str) -> str:
    """'2026-06-12' + '10:30' → 'viernes 12/06 a las 10:30'."""
    from datetime import datetime
    _DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    try:
        d = datetime.strptime(fecha_iso, "%Y-%m-%d")
        base = f"{_DIAS[d.weekday()]} {d.strftime('%d/%m')}"
    except Exception:
        base = fecha_iso
    return f"{base} a las {hora}" if hora else base


# ── job principal ────────────────────────────────────────────────────────────

async def job_eco_prep(dry_run: bool = False) -> dict:
    """Corrida horaria. dry_run=True → solo lista a quién le saldría."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if not dry_run and not _active():
        log.debug("eco_prep: ECO_PREP_ACTIVE=false — skip")
        return {"status": "inactive"}

    now_cl = datetime.now(ZoneInfo("America/Santiago"))
    if not dry_run and not (8 <= now_cl.hour < 21):
        return {"status": "fuera_horario"}

    cfg = _cfg()
    try:
        from winback import is_template_approved
        from session import normalize_wa_id, log_message, log_event
        from medilink import _get_shared_client, _safe_json, HEADERS
        from config import MEDILINK_BASE_URL

        if not dry_run and not await is_template_approved(TEMPLATE_NAME):
            log.info("eco_prep: template %s aún no APPROVED — skip", TEMPLATE_NAME)
            return {"status": "template_not_approved"}

        citas = await _citas_eco_ventana(cfg["dias_ventana"])
        enviadas = _citas_ya_enviadas()

        cli = _get_shared_client()
        pac_cache: dict = {}
        candidatos: list[dict] = []
        seen_phones: set[str] = set()
        for cita in citas:
            if len(candidatos) >= cfg["cap"]:
                break
            id_cita = str(cita.get("id_cita") or cita.get("id") or "")
            if not id_cita or id_cita in enviadas:
                continue
            id_pac = cita.get("id_paciente")
            if not id_pac:
                continue
            if id_pac in pac_cache:
                pac = pac_cache[id_pac]
            else:
                try:
                    rp = await cli.get(f"{MEDILINK_BASE_URL}/pacientes/{id_pac}",
                                       headers=HEADERS, timeout=12)
                    pac = (_safe_json(rp).get("data") or {}) if rp.status_code == 200 else {}
                    if isinstance(pac, list):
                        pac = pac[0] if pac else {}
                except Exception:
                    pac = {}
                pac_cache[id_pac] = pac
            tel = (pac.get("celular") or pac.get("telefono") or "").strip()
            teln = normalize_wa_id(tel) if tel else ""
            if not teln or len(teln) < 11:
                continue
            if teln in seen_phones:
                continue  # 2 ecos del mismo número en la ventana → 1 mensaje
            nombre = (pac.get("nombre") or cita.get("paciente_nombre") or "Paciente").split(" ")[0]
            hora = str(cita.get("hora_inicio") or "")[:5]
            seen_phones.add(teln)
            candidatos.append({
                "id_cita": id_cita,
                "phone": teln,
                "nombre": nombre,
                "fecha": cita.get("_fecha", ""),
                "cuando": _fecha_legible(cita.get("_fecha", ""), hora),
            })

        if dry_run:
            return {
                "candidatos": len(candidatos),
                "muestra": [{"phone": c["phone"][:5] + "***" + c["phone"][-2:],
                             "nombre": c["nombre"], "cuando": c["cuando"]}
                            for c in candidatos[:25]],
                "citas_eco_ventana": len(citas),
                "ya_enviadas": len(enviadas),
                "template": TEMPLATE_NAME,
                "config": cfg,
                "active": _active(),
            }

        from messaging import send_whatsapp_template, render_template_body
        from contact_budget import record_contact  # transaccional: contribuye al presupuesto, no se throttlea

        enviados = 0
        for c in candidatos:
            try:
                await send_whatsapp_template(
                    c["phone"], TEMPLATE_NAME,
                    body_params=[c["nombre"], c["cuando"]],
                )
                log_message(c["phone"], "out",
                            render_template_body(TEMPLATE_NAME, [c["nombre"], c["cuando"]]),
                            "IDLE")
                log_event(c["phone"], _EVENT_ENVIADA,
                          {"id_cita": c["id_cita"], "fecha_cita": c["fecha"]})
                _registrar_envio(c["id_cita"], c["phone"], c["fecha"])
                record_contact(c["phone"], "eco_prep", {"id_cita": c["id_cita"]})
                enviados += 1
                log.info("eco_prep: enviado → ...%s cita=%s (%d/%d)",
                         c["phone"][-4:], c["id_cita"], enviados, len(candidatos))
            except Exception as e:
                log.error("eco_prep: error ...%s: %s", c["phone"][-4:], e)
            await asyncio.sleep(20)

        log.info("job_eco_prep: enviados=%d de %d candidatos", enviados, len(candidatos))
        return {"enviados": enviados, "candidatos": len(candidatos)}
    except Exception as e:
        log.error("job_eco_prep falló: %s", e)
        return {"status": "error", "error": str(e)}


# ── endpoint dry-run (solo dueño / admin) ────────────────────────────────────

def _check_token(token: str | None, request: Request | None) -> None:
    from config import ADMIN_TOKEN, OLACORE_TOKEN
    eff = token or (request.cookies.get("admin_token") if request else None)
    if eff not in (t for t in (ADMIN_TOKEN, OLACORE_TOKEN) if t):
        raise HTTPException(status_code=403, detail="token inválido")


@router.get("/alma/api/eco-prep/dryrun")
async def api_dryrun(request: Request, token: str | None = Query(default=None)):
    """Preview: a quién le saldría la preparación HOY, sin tocar a nadie."""
    _check_token(token, request)
    return JSONResponse(await job_eco_prep(dry_run=True))
