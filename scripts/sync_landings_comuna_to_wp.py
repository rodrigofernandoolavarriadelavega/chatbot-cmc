"""
Sincroniza las landings comuna (Curanilahue, Los Álamos, Cañete, Lebu) al
WordPress de centromedicocarampangue.cl como Pages bajo el slug correcto.

Patrón estilo Snippet 5 (sitio.html): el HTML lo renderiza el chatbot via
_render_comuna_html(for_wp=True) y este script lo pushea al WP via REST API.

Uso:
  # Desde el VPS:
  python3 scripts/sync_landings_comuna_to_wp.py [slug|all]

  # Ejemplos:
  python3 scripts/sync_landings_comuna_to_wp.py all
  python3 scripts/sync_landings_comuna_to_wp.py lebu
"""
import asyncio
import os
import sys
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

WP_BASE = "https://centromedicocarampangue.cl/wp-json/wp/v2"
WP_USER = "Adminweb"
WP_APP_PASS = os.getenv("WP_APP_PASSWORD") or "JObB yl8y 4QBF AZL3 mC96 Xj8U"
AUTH = (WP_USER, WP_APP_PASS)

SLUGS = ["curanilahue", "los-alamos", "canete", "lebu"]


async def render_landing(slug: str) -> tuple[str, str, str] | None:
    """Devuelve (title, description, html). Reutiliza el handler del bot."""
    from main import _render_comuna_html, _COMUNAS_DATA
    data = _COMUNAS_DATA.get(slug)
    if not data:
        return None
    html = await _render_comuna_html(slug, for_wp=True)
    if html is None:
        return None
    return data["title"], data["description"], html


async def find_existing_page(client: httpx.AsyncClient, slug: str) -> int | None:
    """Busca el ID de la página WP por slug. Retorna None si no existe."""
    r = await client.get(f"{WP_BASE}/pages",
                          params={"slug": slug, "status": "publish,draft,private"},
                          auth=AUTH, timeout=30)
    if r.status_code != 200:
        print(f"  ! GET /pages?slug={slug} → {r.status_code}: {r.text[:200]}")
        return None
    items = r.json()
    return items[0]["id"] if items else None


async def upsert_page(client: httpx.AsyncClient, slug: str) -> None:
    rendered = await render_landing(slug)
    if not rendered:
        print(f"[{slug}] sin datos — skip")
        return
    title, descr, html = rendered

    existing = await find_existing_page(client, slug)
    body = {
        "title": title,
        "slug": slug,
        "content": html,
        "status": "publish",
        "excerpt": descr,
        "comment_status": "closed",
        "ping_status": "closed",
    }

    if existing:
        print(f"[{slug}] UPDATE page id={existing}")
        r = await client.post(f"{WP_BASE}/pages/{existing}", json=body, auth=AUTH, timeout=60)
    else:
        print(f"[{slug}] CREATE new page")
        r = await client.post(f"{WP_BASE}/pages", json=body, auth=AUTH, timeout=60)

    if r.status_code in (200, 201):
        pid = r.json().get("id")
        url = r.json().get("link", "")
        print(f"  ✓ ok · id={pid} · {url}")
    else:
        print(f"  ! HTTP {r.status_code}: {r.text[:300]}")


async def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    target = SLUGS if arg == "all" else [arg]

    async with httpx.AsyncClient() as client:
        # Verify auth
        r = await client.get(f"{WP_BASE}/users/me", auth=AUTH, timeout=15)
        if r.status_code != 200:
            print(f"AUTH FAIL: {r.status_code} {r.text[:200]}")
            sys.exit(1)
        print(f"Auth OK como {r.json().get('name')}\n")

        for slug in target:
            await upsert_page(client, slug)
            print()

    print("Listo. Revisar:")
    for slug in target:
        print(f"  https://centromedicocarampangue.cl/{slug}")


if __name__ == "__main__":
    asyncio.run(main())
