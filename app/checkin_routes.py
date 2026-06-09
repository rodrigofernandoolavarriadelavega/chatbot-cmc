"""
Check-in QR de pacientes — Feature A (ver docs/CHECKIN_AUTOPAGO.md).

El paciente escanea un QR en recepción → /checkin → escribe su RUT → el sistema
encuentra su cita de HOY en Medilink → marca "en sala de espera" (tabla local
checkin_cmc) → aparece en el panel de recepción/profesional.

Gateado por CHECKIN_ENABLED (default OFF). Endpoints públicos (buscar/llegada) no
piden token admin (es cara al paciente, como el agendador); los de gestión
(en-sala/atendido) sí. Feature B (auto-pago) se monta encima después.
"""
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, HTTPException, Query, Request

log = logging.getLogger("checkin_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")

router = APIRouter(prefix="/checkin/api", tags=["checkin"])


def _enabled() -> bool:
    return os.getenv("CHECKIN_ENABLED", "false").lower() == "true"


def ensure_checkin_table() -> None:
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checkin_cmc (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha           TEXT NOT NULL,
                rut             TEXT DEFAULT '',
                paciente_nombre TEXT DEFAULT '',
                id_cita         TEXT DEFAULT '',
                id_profesional  INTEGER,
                profesional     TEXT DEFAULT '',
                hora_cita       TEXT DEFAULT '',
                estado          TEXT DEFAULT 'en_sala',   -- en_sala · atendido
                pagado          INTEGER NOT NULL DEFAULT 0,
                llegada_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_checkin_fecha ON checkin_cmc(fecha)")
        # Un check-in por cita por día (re-escanear no duplica)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_checkin_cita ON checkin_cmc(fecha, id_cita)")


def _hoy_variantes() -> tuple[str, str]:
    now = datetime.now(_CHILE_TZ)
    return now.strftime("%Y-%m-%d"), now.strftime("%d/%m/%Y")


def _es_hoy(fecha_cita: str) -> bool:
    iso, dmy = _hoy_variantes()
    f = (fecha_cita or "").strip()
    return f.startswith(iso) or f.startswith(dmy)


# ── Público: buscar mi cita de hoy por RUT ───────────────────────────────────

@router.get("/buscar")
async def buscar_cita_hoy(rut: str = Query(...)):
    if not _enabled():
        raise HTTPException(404, "Check-in no disponible")
    from medilink import buscar_paciente, listar_citas_paciente
    rut = (rut or "").strip()
    if len(rut) < 7:
        raise HTTPException(400, "RUT inválido")

    pac = await buscar_paciente(rut)
    if not pac:
        return {"encontrado": False, "mensaje": "No encontramos tu ficha. Acércate a recepción."}

    citas = await listar_citas_paciente(pac.get("id"), rut=rut)
    hoy = [c for c in citas if _es_hoy(c.get("fecha", ""))]
    # estado de check-in ya hecho hoy
    iso, _ = _hoy_variantes()
    from session import _conn
    ensure_checkin_table()
    with _conn() as conn:
        ya = {r["id_cita"] for r in conn.execute(
            "SELECT id_cita FROM checkin_cmc WHERE fecha = ?", (iso,))}

    return {
        "encontrado": True,
        "nombre": pac.get("nombre", ""),
        "id_paciente": pac.get("id"),
        "citas": [{
            "id_cita":       str(c["id"]),
            "hora":          c.get("hora_inicio", ""),
            "profesional":   c.get("profesional", ""),
            "especialidad":  c.get("especialidad", ""),
            "id_profesional": c.get("id_profesional"),
            "ya_en_sala":    str(c["id"]) in ya,
        } for c in hoy],
    }


# ── Público: avisar que llegué (marca en sala) ───────────────────────────────

@router.post("/llegada")
async def marcar_llegada(request: Request):
    if not _enabled():
        raise HTTPException(404, "Check-in no disponible")
    ensure_checkin_table()
    body = await request.json()
    id_cita = str(body.get("id_cita") or "").strip()
    rut     = (body.get("rut") or "").strip()
    if not id_cita:
        raise HTTPException(400, "Falta id_cita")

    nombre = (body.get("nombre") or "").strip()
    prof   = (body.get("profesional") or "").strip()
    id_prof = body.get("id_profesional")
    hora    = (body.get("hora") or "").strip()
    iso, _ = _hoy_variantes()

    from session import _conn
    with _conn() as conn:
        conn.execute(
            """INSERT INTO checkin_cmc
                  (fecha, rut, paciente_nombre, id_cita, id_profesional, profesional,
                   hora_cita, estado, llegada_at, updated_at)
               VALUES (?,?,?,?,?,?,?, 'en_sala', datetime('now'), datetime('now'))
               ON CONFLICT(fecha, id_cita) DO UPDATE SET
                  estado = 'en_sala', updated_at = datetime('now')""",
            (iso, rut, nombre, id_cita, id_prof, prof, hora),
        )
        conn.commit()
    log.info("checkin: llegada rut=%s cita=%s", rut[:4] + "***", id_cita)
    return {"ok": True, "mensaje": "¡Listo! Estás en sala de espera 🪑"}


# ── Gestión (recepción/profesional): lista en sala + marcar atendido ─────────

def _is_admin(token, cmc_session) -> bool:
    from admin_routes import _verify_cookie, _is_admin_token
    if token and _is_admin_token(token):
        return True
    if cmc_session and _verify_cookie(cmc_session) in ("admin", "ortodoncia"):
        return True
    return False


@router.get("/en-sala")
def lista_en_sala(
    request: Request,
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
):
    if not _is_admin(token, cmc_session):
        raise HTTPException(401, "Token inválido")
    ensure_checkin_table()
    iso, _ = _hoy_variantes()
    from session import _conn
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, rut, paciente_nombre, id_cita, profesional, hora_cita,
                      estado, pagado, llegada_at
                 FROM checkin_cmc
                WHERE fecha = ? AND estado = 'en_sala'
                ORDER BY llegada_at ASC""",   # más antiguo (más esperando) primero
            (iso,),
        ).fetchall()
    return {"fecha": iso, "en_sala": [dict(r) for r in rows], "total": len(rows)}


@router.post("/atendido/{checkin_id}")
def marcar_atendido(
    checkin_id: int,
    request: Request,
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
):
    if not _is_admin(token, cmc_session):
        raise HTTPException(401, "Token inválido")
    from session import _conn
    with _conn() as conn:
        conn.execute(
            "UPDATE checkin_cmc SET estado='atendido', updated_at=datetime('now') WHERE id=?",
            (checkin_id,),
        )
        conn.commit()
    return {"ok": True}
