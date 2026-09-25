"""Instagram: META_PAGE_ACCESS_TOKEN venció el 14-jun-2026 y todo envío fallaba
con 401 sin que el panel se enterara (192 personas, 167 respuestas de recepción
perdidas). Ahora se envía por /{page-id}/messages con el token de página
derivado del system-user (no vence); el camino antiguo queda de respaldo y
send_instagram devuelve False si no entregó."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import messaging as M  # noqa: E402


class _R:
    def __init__(self, code, data=None, text=""):
        self.status_code, self._d, self.text = code, data or {}, text
    def json(self): return self._d


class _Client:
    def __init__(self, responder): self.responder, self.posts = responder, []
    async def get(self, url, **k):
        return _R(200, {"data": [{"id": M.META_PAGE_ID, "access_token": "PAGE_TOK_DERIVADO"}]})
    async def post(self, url, headers=None, json=None, **k):
        self.posts.append((url, headers["Authorization"]))
        return self.responder(url)


def _run(responder, monkeypatch):
    cli = _Client(responder)
    M._IG_PAGE_TOKEN_CACHE.clear()
    monkeypatch.setattr(M, "_get_meta_client", lambda: cli)
    monkeypatch.setattr(M, "_is_dupe_outbound", lambda *a: False)
    monkeypatch.setattr(M, "META_ACCESS_TOKEN", "SYSTEM_USER_TOK")
    ok = asyncio.run(M.send_instagram("123", "Hola, sí atendemos por Fonasa"))
    return ok, cli.posts


def test_envia_por_pagina_con_token_derivado(monkeypatch):
    ok, posts = _run(lambda url: _R(200), monkeypatch)
    assert ok is True
    assert posts[0][0].startswith("https://graph.facebook.com/") and posts[0][0].endswith(f"/{M.META_PAGE_ID}/messages")
    assert posts[0][1] == "Bearer PAGE_TOK_DERIVADO"


def test_si_la_pagina_falla_prueba_la_ruta_antigua(monkeypatch):
    monkeypatch.setattr(M, "INSTAGRAM_USER_ID", "999")
    ok, posts = _run(lambda url: _R(400, text="err") if "graph.facebook" in url else _R(200), monkeypatch)
    assert ok is True and any("graph.instagram.com" in u for u, _ in posts)


def test_devuelve_false_si_no_entrega(monkeypatch):
    monkeypatch.setattr(M, "INSTAGRAM_USER_ID", "999")
    ok, _ = _run(lambda url: _R(401, text="Session has expired"), monkeypatch)
    assert ok is False
