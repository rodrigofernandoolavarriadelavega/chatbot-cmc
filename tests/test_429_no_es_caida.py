"""Regresión del 2º incidente 2026-07-27: Psicología a lista de espera por 429.

Nelson pidió Psicología a las 11:24 y a los 2 segundos quedó en lista de espera.
En el log, justo antes: tres 429 seguidos en `/profesionales/73/horariosespeciales`
→ "no respondió tras 3 intentos" → `_report_down()` apagó el circuit breaker →
modo degradado. Medilink estaba VIVO: contestaba 429, que significa "más lento",
no "estoy muerto". La sonda `probe_up` cometía el mismo error de lectura.

Fija el contrato de los tres estados que antes se confundían en uno solo:
  429 sostenido  → SATURADO  → MedilinkRateLimited, breaker NO se apaga
  5xx / red      → CAÍDO     → httpx.RequestError, breaker se apaga
  respuesta ok   → VIVO

Y la amplificación que generaba los 429: /horariosbloqueados devuelve lo mismo
para todos los profesionales (la API solo filtra por sucursal+fecha), así que
buscar en N médicos × M días hacía N×M requests idénticas. Ahora 1 por fecha.

Correr: venv/bin/python3 tests/test_429_no_es_caida.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import session  # noqa: E402
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
session.DB_PATH = Path(_tmp.name)

import httpx  # noqa: E402
import medilink  # noqa: E402
import resilience  # noqa: E402

_FALLOS: list[str] = []


def _ok(cond, msg):
    print(("OK    " if cond else "FALLO ") + msg)
    if not cond:
        _FALLOS.append(msg)


class _ClienteFalso:
    """Cliente httpx de mentira que responde siempre lo mismo."""

    def __init__(self, status=None, excepcion=None):
        self.status = status
        self.excepcion = excepcion
        self.llamadas = 0

    async def get(self, url, **kw):
        self.llamadas += 1
        if self.excepcion:
            raise self.excepcion
        return httpx.Response(self.status, request=httpx.Request("GET", url))


def _sin_esperas():
    """Anula los backoff para que el test no tarde 21 segundos."""
    async def _nada(*a, **k):
        return None
    medilink.asyncio.sleep = _nada


def test_429_sostenido_no_apaga_el_breaker():
    _sin_esperas()
    resilience.mark_medilink_up()
    medilink._last_reported_status = None
    cli = _ClienteFalso(status=429)

    try:
        asyncio.run(medilink._get(cli, "http://x/citas"))
        salio = "sin excepción"
    except medilink.MedilinkRateLimited:
        salio = "MedilinkRateLimited"
    except httpx.RequestError:
        salio = "RequestError generico"

    _ok(salio == "MedilinkRateLimited",
        f"429 sostenido levanta MedilinkRateLimited (salió: {salio})")
    _ok(not resilience.is_medilink_down(),
        "429 sostenido NO apaga el breaker — Medilink está vivo")


def test_5xx_si_apaga_el_breaker():
    _sin_esperas()
    resilience.mark_medilink_up()
    medilink._last_reported_status = None
    cli = _ClienteFalso(status=503)

    try:
        asyncio.run(medilink._get(cli, "http://x/citas"))
        hubo = False
    except medilink.MedilinkRateLimited:
        hubo = "rate"
    except httpx.RequestError:
        hubo = True

    _ok(hubo is True, "un 503 sostenido levanta RequestError, no rate-limit")
    _ok(resilience.is_medilink_down(), "un 503 sostenido SÍ apaga el breaker")


def test_error_de_red_si_apaga_el_breaker():
    _sin_esperas()
    resilience.mark_medilink_up()
    medilink._last_reported_status = None
    cli = _ClienteFalso(excepcion=httpx.ConnectError("sin ruta al host"))

    try:
        asyncio.run(medilink._get(cli, "http://x/citas"))
        hubo = False
    except httpx.RequestError:
        hubo = True

    _ok(hubo, "un error de red levanta RequestError")
    _ok(resilience.is_medilink_down(), "un error de red SÍ apaga el breaker")


def test_mezcla_429_y_5xx_cuenta_como_caida():
    """Si hubo aunque sea una falla real, no se puede cantar 'solo saturado'."""
    _sin_esperas()
    resilience.mark_medilink_up()
    medilink._last_reported_status = None

    class _Alternante(_ClienteFalso):
        async def get(self, url, **kw):
            self.llamadas += 1
            code = 429 if self.llamadas == 1 else 503
            return httpx.Response(code, request=httpx.Request("GET", url))

    try:
        asyncio.run(medilink._get(_Alternante(), "http://x/citas"))
    except Exception:
        pass
    _ok(resilience.is_medilink_down(),
        "429 mezclado con 5xx se trata como caída (conservador)")


def test_bloqueos_se_consultan_una_vez_por_fecha():
    """La amplificación que producía los 429."""
    medilink._bloqueos_dia_cache.clear()

    class _CuentaBloqueos(_ClienteFalso):
        async def get(self, url, **kw):
            self.llamadas += 1
            return httpx.Response(
                200, json={"data": []}, request=httpx.Request("GET", url))

    cli = _CuentaBloqueos()

    async def _corrida():
        # 3 profesionales × 1 fecha, como una búsqueda de Medicina General
        for prof in (1, 73, 13):
            await medilink._get_bloqueos(cli, prof, "2030-01-10")

    asyncio.run(_corrida())
    _ok(cli.llamadas == 1,
        f"3 profesionales misma fecha = 1 sola request (fueron {cli.llamadas})")

    # Fecha distinta sí debe volver a preguntar
    asyncio.run(medilink._get_bloqueos(cli, 1, "2030-01-11"))
    _ok(cli.llamadas == 2, f"otra fecha sí consulta de nuevo (van {cli.llamadas})")
    medilink._bloqueos_dia_cache.clear()


def test_bloqueos_filtran_por_profesional_desde_la_cache():
    """La caché no puede hacer que un bloqueo ajeno tape una hora buena."""
    medilink._bloqueos_dia_cache.clear()
    datos = [
        {"id_profesional": 73, "hora_inicio": "10:00:00", "hora_fin": "11:00:00"},
        {"id_profesional": 13, "hora_inicio": "15:00:00", "hora_fin": "16:00:00"},
        {"id_profesional": None, "hora_inicio": "13:00:00", "hora_fin": "14:00:00"},
    ]

    class _ConDatos(_ClienteFalso):
        async def get(self, url, **kw):
            self.llamadas += 1
            return httpx.Response(
                200, json={"data": datos}, request=httpx.Request("GET", url))

    cli = _ConDatos()
    b73 = asyncio.run(medilink._get_bloqueos(cli, 73, "2030-02-02"))
    b13 = asyncio.run(medilink._get_bloqueos(cli, 13, "2030-02-02"))

    _ok(("10:00", "11:00") in b73 and ("15:00", "16:00") not in b73,
        "el profesional 73 ve su bloqueo y NO el del 13")
    _ok(("15:00", "16:00") in b13 and ("10:00", "11:00") not in b13,
        "el profesional 13 ve el suyo y no el del 73")
    _ok(("13:00", "14:00") in b73 and ("13:00", "14:00") in b13,
        "el bloqueo de sucursal (sin profesional) aplica a los dos")
    medilink._bloqueos_dia_cache.clear()


def test_probe_up_lee_429_como_vivo():
    """El error puntual que dejó a Nelson en lista de espera."""
    resilience.mark_medilink_down("simulado")

    class _Cli429:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return httpx.Response(429, request=httpx.Request("GET", url))

    orig = medilink.httpx.AsyncClient
    medilink.httpx.AsyncClient = lambda *a, **k: _Cli429()
    try:
        vivo = asyncio.run(medilink.probe_up())
    finally:
        medilink.httpx.AsyncClient = orig
        resilience.mark_medilink_up()

    _ok(vivo is True,
        "un 429 en la sonda significa VIVO (contestó), no caído")


def test_rate_limit_no_se_traduce_a_sin_horas():
    """El mismo bug con otro disfraz, cazado en producción a las 14:31.

    `_iniciar_agendar` tenía un `except Exception` que convertía cualquier fallo
    de `buscar_primer_dia` en `smart, todos = [], []` → "no hay horas" → lista de
    espera. Con un rate limit la búsqueda quedó A MEDIAS: no autoriza a concluir
    nada sobre la disponibilidad. Debe propagarse, no inventarse un resultado.
    """
    import flows  # noqa: E402
    import session as _s  # noqa: E402

    phone = "56900000009"
    orig = flows.buscar_primer_dia

    async def _saturado(*a, **k):
        raise medilink.MedilinkRateLimited("429 en /citas")

    flows.buscar_primer_dia = _saturado
    resilience.mark_medilink_up()
    try:
        _s.reset_session(phone)
        try:
            asyncio.run(flows._iniciar_agendar(phone, {}, "otorrinolaringología"))
            salio = "devolvió un mensaje (se tragó el error)"
        except medilink.MedilinkRateLimited:
            salio = "propagó"
        except Exception as e:
            salio = f"otra excepción: {type(e).__name__}"
    finally:
        flows.buscar_primer_dia = orig

    _ok(salio == "propagó",
        f"un rate limit NO se traduce a 'no hay horas' (salió: {salio})")


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
    print("\n" + ("── TODO OK ──" if not _FALLOS else f"── FALLARON {len(_FALLOS)} ──"))
    sys.exit(1 if _FALLOS else 0)
