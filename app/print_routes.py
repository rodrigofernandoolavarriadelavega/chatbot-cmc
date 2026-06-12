"""Rutas de impresion remota — proxy a Alma Print (print.agentecmc.cl).

GET  /admin/api/print/printers  → lista agentes/impresoras online
POST /admin/api/print            → envia archivo de paciente a imprimir
"""
import logging
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from admin_routes import require_admin

log = logging.getLogger(__name__)

router = APIRouter()

_ALMA_PRINT_URL       = os.getenv("ALMA_PRINT_URL", "https://print.agentecmc.cl").rstrip("/")
_ALMA_PRINT_USER_TOKEN = os.getenv("ALMA_PRINT_USER_TOKEN", "")

# ── helpers ──────────────────────────────────────────────────────────────────

def _check_token() -> None:
    """Aborta con 503 descriptivo si el token no esta configurado."""
    if not _ALMA_PRINT_USER_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="ALMA_PRINT_USER_TOKEN no configurado en el servidor — contacta al administrador.",
        )


def _resolve_file(file_id: int) -> tuple[Path, str, str]:
    """Busca el archivo en la BD y devuelve (path_absoluto, mime, filename).

    Reutiliza la misma logica que /admin/api/file/{file_id}.
    """
    from session import db as _db_fn
    from pathlib import Path as _Path

    with _db_fn() as conn:
        row = conn.execute(
            "SELECT file_path, mime_type, filename FROM patient_files WHERE id=?",
            (file_id,),
        ).fetchone()

    if not row:
        raise HTTPException(404, "Archivo no encontrado")

    _root = _Path(__file__).parent.parent
    fpath = (_root / row["file_path"]).resolve()
    try:
        fpath.relative_to((_root / "data").resolve())
    except ValueError:
        raise HTTPException(400, "Ruta fuera del directorio permitido")

    if not fpath.exists():
        raise HTTPException(404, "Archivo eliminado del disco")

    return fpath, row["mime_type"] or "application/octet-stream", row["filename"] or "archivo"


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/admin/api/print/printers")
async def print_printers(_: str = Depends(require_admin)):
    """Lista agentes/impresoras disponibles en Alma Print."""
    _check_token()
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"{_ALMA_PRINT_URL}/api/printers",
                params={"token": _ALMA_PRINT_USER_TOKEN},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        log.warning("print_printers error: %s", exc)
        return JSONResponse({"agentes": [], "error": "sin conexion con Alma Print"})


@router.post("/admin/api/print")
async def print_file(
    payload: dict,
    _: str = Depends(require_admin),
):
    """Envia un archivo de paciente a la impresora seleccionada.

    Body JSON esperado:
      file_id   int    — id en patient_files
      agent_id  str    — id del agente Alma Print
      printer   str    — nombre de la impresora
      copias    int    — (default 1)
      nota      str    — (opcional)
    """
    _check_token()

    file_id  = payload.get("file_id")
    agent_id = payload.get("agent_id", "")
    printer  = payload.get("printer", "")
    copias   = int(payload.get("copias") or 1)
    nota     = payload.get("nota") or ""

    if not file_id:
        raise HTTPException(400, "file_id requerido")
    if not agent_id or not printer:
        raise HTTPException(400, "agent_id y printer son requeridos")

    fpath, mime, filename = _resolve_file(int(file_id))
    file_bytes = fpath.read_bytes()

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_ALMA_PRINT_URL}/api/jobs",
                data={
                    "token":    _ALMA_PRINT_USER_TOKEN,
                    "agent_id": agent_id,
                    "printer":  printer,
                    "copias":   str(copias),
                    "nota":     nota,
                },
                files={"archivo": (filename, file_bytes, mime)},
            )
            resp.raise_for_status()
            result = resp.json()
    except httpx.HTTPStatusError as exc:
        log.error("print_file HTTP error %s: %s", exc.response.status_code, exc.response.text[:200])
        raise HTTPException(502, f"Alma Print respondio error {exc.response.status_code}")
    except Exception as exc:
        log.error("print_file error: %s", exc)
        raise HTTPException(502, "No se pudo conectar con Alma Print")

    log.info("print_file ok file_id=%s agent=%s printer=%s job=%s", file_id, agent_id, printer, result.get("id"))

    # log_event opcional — session.py no tiene phone aqui, pero podemos loguear igual
    try:
        from session import log_event as _log_event
        _log_event("panel", "panel_print", {
            "file_id": file_id,
            "agent_id": agent_id,
            "printer": printer,
            "copias": copias,
            "job_id": result.get("id"),
        })
    except Exception:
        pass  # no-op si log_event falla (no es critico)

    return {"ok": True, "id": result.get("id"), "detail": result}
