"""persistencia_routes.py — Panel/API del carril de persistencia.

Mismo patrón de auth que mg_abandono_routes.py (token admin o cookie de
sesión), mismo estilo de endpoint de solo lectura. No crea un dashboard
nuevo — pensado para embeberse en /admin/v2 (pill "Persistencia") o
consultarse standalone como JSON, igual que /api/mg-abandono.
"""
from __future__ import annotations

from fastapi import Query, Cookie, HTTPException


def _auth(token, cmc_session):
    from admin_routes import _verify_cookie, _is_admin_token
    if not ((token and _is_admin_token(token)) or (cmc_session and _verify_cookie(cmc_session))):
        raise HTTPException(403, "No autorizado")


def register_persistencia_routes(app):
    @app.get("/api/persistencia", tags=["persistencia"], include_in_schema=False)
    def persistencia_funnel(dias: int = Query(30), token: str | None = Query(None),
                             cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from persistencia import medir_funnel_persistencia
        return medir_funnel_persistencia(dias=dias)

    @app.post("/api/persistencia/sync", tags=["persistencia"], include_in_schema=False)
    def persistencia_sync_manual(token: str | None = Query(None),
                                  cmc_session: str | None = Cookie(None)):
        """Corre sync_consultas() a demanda (solo bookkeeping, cero contacto)
        — útil para poblar la tabla y ver el estado ANTES de encender el
        contacto (PERSISTENCIA_ACTIVE)."""
        _auth(token, cmc_session)
        from persistencia import sync_consultas
        return sync_consultas()
