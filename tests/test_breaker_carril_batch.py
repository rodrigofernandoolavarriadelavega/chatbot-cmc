"""Regresión del incidente 2026-07-27: pacientes de Medicina General mandados a
lista de espera con la agenda LLENA de cupos.

Cadena original de la falla:
  cron de pagos (cada 30 min) recorre /atenciones/{id} y /pacientes/{id} cita
  por cita → 105-114 429/hora → `_report_down()` marca el circuit breaker GLOBAL
  → `_iniciar_agendar` cortaba ANTES de consultar → modo degradado → lista de
  espera. Mientras tanto /citas y /agendas respondían 200 y el Dr. Abarca tenía
  33 cupos libres. Ese día 34 pacientes recibieron "problema técnico" y 17
  terminaron en lista de espera, contra 13 citas creadas.

Este test fija las tres barandas del arreglo:
  1. Las fallas del carril batch NO apagan el breaker del paciente.
  2. El carril batch tiene un cuello más angosto que el del paciente.
  3. `_iniciar_agendar` no confía en el flag: lo VERIFICA con una sonda en vivo
     y, si Medilink responde, sigue agendando en vez de ofrecer lista de espera.

Correr: venv/bin/python3 tests/test_breaker_carril_batch.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# DB temporal ANTES de importar nada que toque session.
import session  # noqa: E402
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
session.DB_PATH = Path(_tmp.name)

import medilink  # noqa: E402
import resilience  # noqa: E402
import flows  # noqa: E402

_FALLOS: list[str] = []


def _ok(cond, msg):
    print(("OK    " if cond else "FALLO ") + msg)
    if not cond:
        _FALLOS.append(msg)


def _texto(resp) -> str:
    """Los mensajes con botones/lista viajan como dict — extrae el cuerpo."""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        inter = resp.get("interactive", {})
        partes = [inter.get("body", {}).get("text", ""),
                  inter.get("header", {}).get("text", "")]
        for b in inter.get("action", {}).get("buttons", []):
            partes.append(b.get("reply", {}).get("title", ""))
        for sec in inter.get("action", {}).get("sections", []):
            for row in sec.get("rows", []):
                partes.append(row.get("title", ""))
        return "\n".join(p for p in partes if p)
    return str(resp)


def test_falla_batch_no_apaga_el_breaker():
    resilience.mark_medilink_up()
    medilink._last_reported_status = None
    with medilink.lane_batch():
        medilink._report_down("429 del cron de pagos")
    _ok(not resilience.is_medilink_down(),
        "un 429 del cron de pagos NO apaga el agendamiento del paciente")


def test_falla_del_carril_paciente_si_apaga():
    resilience.mark_medilink_up()
    medilink._last_reported_status = None
    medilink._report_down("timeout real consultando slots")
    _ok(resilience.is_medilink_down(),
        "una falla del carril paciente SÍ apaga el breaker")


def test_batch_tiene_cuello_mas_angosto():
    _ok(medilink._BATCH_SEM._value < medilink._MEDILINK_SEM._value,
        "el carril batch se limita más que el del paciente "
        f"(batch={medilink._BATCH_SEM._value}, paciente={medilink._MEDILINK_SEM._value})")


def test_use_batch_lane_no_se_filtra_entre_tasks():
    async def _corrida():
        async def _job():
            medilink.use_batch_lane()
            return medilink.current_lane()
        dentro = await asyncio.create_task(_job())
        return dentro, medilink.current_lane()

    dentro, fuera = asyncio.run(_corrida())
    _ok(dentro == "batch" and fuera == "patient",
        "use_batch_lane() marca su task y no contamina a las demás")


def test_breaker_viejo_no_manda_a_lista_de_espera():
    """El corazón del incidente: breaker en 'down' pero Medilink vivo."""
    phone = "56900000001"
    slot = {
        "fecha": "2030-01-10", "fecha_display": "Jueves 10 de enero",
        "hora_inicio": "10:30", "hora_fin": "10:45",
        "id_profesional": 73, "profesional": "Dr. Andrés Abarca",
        "especialidad": "Medicina General", "id_recurso": 1,
    }

    orig_probe = medilink.probe_up
    orig_primer_dia = flows.buscar_primer_dia
    sondas = {"n": 0}

    async def _fake_probe(timeout: float = 3.0):
        sondas["n"] += 1
        return True  # Medilink responde: el flag estaba viejo

    async def _fake_primer_dia(esp, solo_ids=None, **kw):
        return [slot], [slot]

    medilink.probe_up = _fake_probe
    flows.buscar_primer_dia = _fake_primer_dia
    try:
        resilience.mark_medilink_down("429 acumulados del cron")
        session.reset_session(phone)
        resp = _texto(asyncio.run(flows._iniciar_agendar(phone, {}, "medicina general")))
    finally:
        medilink.probe_up = orig_probe
        flows.buscar_primer_dia = orig_primer_dia
        resilience.mark_medilink_up()

    _ok(sondas["n"] == 1,
        "con el breaker abajo se sondea Medilink en vez de creerle al flag")
    _ok("lista de espera" not in resp.lower(),
        "NO se ofrece lista de espera cuando Medilink responde y hay cupos")
    _ok("problema técnico" not in resp.lower(),
        "NO se muestra el mensaje de caída con Medilink vivo")
    _ok("10:30" in resp,
        "se ofrece la hora real que estaba disponible")


def test_breaker_real_si_degrada():
    """Contracara: si la sonda tampoco responde, el modo degradado se mantiene."""
    phone = "56900000002"
    orig_probe = medilink.probe_up

    async def _fake_probe_caido(timeout: float = 3.0):
        return False

    medilink.probe_up = _fake_probe_caido
    try:
        resilience.mark_medilink_down("caída real")
        session.reset_session(phone)
        resp = _texto(asyncio.run(flows._iniciar_agendar(phone, {}, "medicina general")))
    finally:
        medilink.probe_up = orig_probe
        resilience.mark_medilink_up()

    _ok("problema técnico" in resp.lower(),
        "con Medilink realmente caído el paciente recibe el aviso honesto")


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
    print("\n" + ("── TODO OK ──" if not _FALLOS else f"── FALLARON {len(_FALLOS)} ──"))
    sys.exit(1 if _FALLOS else 0)
