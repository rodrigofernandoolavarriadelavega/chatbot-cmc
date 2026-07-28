"""abono_pago_routes.py — Página pública de transferencia para el abono de
Psiquiatría (`/abono/{token}`) + endpoint de estado que la página consulta
para mostrar "confirmado" sin que el paciente tenga que hacer nada.

Público (sin auth) por diseño: el token es aleatorio (32+ bytes, no
adivinable, `secrets.token_urlsafe`) y es la única llave — mismo patrón que
cualquier link de pago. No expone nada del paciente salvo lo que él mismo ya
sabe (monto, estado de SU propio abono).

Convive con `ABONO_AUTO_ACTIVE` apagado: el link se genera solo si
`abono_transferencia.crear_abono_pendiente` se llama (gateado en flows.py),
así que con el flag off esta ruta simplemente nunca recibe tráfico real —
pero queda desplegada y no rompe nada por sí sola.
"""
import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("abono_pago_routes")
router = APIRouter(tags=["abono_pago"])

_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "abono_pago.html"
_HTML = _TEMPLATE_PATH.read_text(encoding="utf-8") if _TEMPLATE_PATH.exists() else "<h1>No disponible</h1>"


@router.get("/abono/{token}", response_class=HTMLResponse)
async def pagina_abono(token: str):
    # El HTML es estático (una sola plantilla para todos los estados); el JS
    # de la página pide los datos reales a /api/abono/{token}/estado. Así el
    # HTML se puede cachear en el borde sin filtrar datos de nadie.
    return HTMLResponse(_HTML)


@router.get("/api/abono/{token}/estado")
async def estado_abono(token: str):
    from abono_transferencia import get_abono_pendiente
    from config import CMC_TRANSFERENCIA

    try:
        abono = get_abono_pendiente(token)
    except Exception as e:
        log.error("estado_abono: error leyendo token: %s", e)
        return JSONResponse({"error": "no_encontrado"}, status_code=404)

    if not abono:
        return JSONResponse({"error": "no_encontrado"}, status_code=404)

    from datetime import datetime
    from zoneinfo import ZoneInfo
    _CL = ZoneInfo("America/Santiago")

    estado = abono["estado"]
    if estado in ("pendiente", "esperando_confirmacion_paciente"):
        try:
            expira = datetime.fromisoformat(abono["expira_at"])
            if expira.tzinfo is None:
                expira = expira.replace(tzinfo=_CL)
            if datetime.now(_CL) > expira:
                estado = "expirado"
        except Exception:
            pass

    resp = {
        "estado": "confirmado" if estado == "confirmado" else ("expirado" if estado == "expirado" else "pendiente"),
        "monto": abono["monto"],
        "cuenta": CMC_TRANSFERENCIA,
    }
    return JSONResponse(resp)
