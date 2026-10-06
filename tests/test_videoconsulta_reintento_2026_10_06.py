"""Reintento por "Debe mandar el parámetro videoconsulta" + reintento por 429.

Casos reales:

* Julieta (56932133850, 6-oct 12:35) y Fabián (56942610884, 2-oct): reservaron
  con la Ps. Salas (82). Medilink rechazó la cita (su horario quedó con
  videoconsulta habilitada), el bot reintentó como TELEMEDICINA y la cita SÍ se
  creó (67726 / 67421)... pero el `except` caía igual al `raise` final y al
  paciente se le dijo "Tuve un problema técnico". Fabián no fue a su hora.
* Mauricio (56961745873, 6-oct 12:24): Medilink saturado → "te escribo apenas
  tenga las horas", pero el webhook de WhatsApp programaba el reintento con
  `canal`/`sender_id`/`send_fn` que no existen ahí → NameError y nunca se le
  escribió (7 veces entre 24-sep y 6-oct).
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

_TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_videoreint_")) / "sessions.db"
os.environ["SESSIONS_DB"] = str(_TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session as _session_mod  # noqa: E402

_session_mod.DB_PATH = _TMP_DB

import flows  # noqa: E402
import medilink  # noqa: E402
import messaging  # noqa: E402
import resilience  # noqa: E402
from session import get_session, reset_session, save_session  # noqa: E402

_FECHA = (date.today() + timedelta(days=2)).strftime("%Y-%m-%d")


async def _fake_noop(*a, **kw):
    return None


async def _fake_lista_vacia(*a, **kw):
    return []


resilience.is_medilink_down = lambda: False
flows.is_medilink_down = lambda: False
messaging.send_whatsapp = _fake_noop
flows.send_whatsapp = _fake_noop
flows.listar_citas_paciente = _fake_lista_vacia


async def _fake_slot_libre(*a, **kw):
    return True


flows.verificar_slot_disponible = _fake_slot_libre

LLAMADAS: list[dict] = []


async def _crear_cita_exige_video(**kw):
    """1ª llamada: Medilink exige el campo. 2ª: crea la cita."""
    LLAMADAS.append(kw)
    if len(LLAMADAS) == 1:
        raise medilink.MedilinkVideoconsultaRequired(
            f"Medilink exige parámetro videoconsulta (prof={kw['id_profesional']})")
    return {"id": 67726}


flows.crear_cita = _crear_cita_exige_video


def _slot(id_prof: int, nombre: str, esp: str):
    return {
        "profesional": nombre, "especialidad": esp,
        "fecha": _FECHA, "fecha_display": "jueves",
        "hora_inicio": "17:00", "hora_fin": "17:45",
        "id_profesional": id_prof, "id_recurso": 1, "duracion": 45,
    }


def _texto(resp) -> str:
    if isinstance(resp, dict):
        return str(resp)
    return str(resp or "")


def _confirmar(phone: str, slot: dict, especialidad: str) -> str:
    LLAMADAS.clear()
    reset_session(phone)
    save_session(phone, "CONFIRMING_CITA", {
        "especialidad": especialidad,
        "slot_elegido": slot,
        "rut": "29139884-1",
        "paciente": {"id": 15624, "nombre": "Julieta Lobos Rodriguez"},
        "modalidad": "fonasa",
    })
    return _texto(asyncio.run(flows.handle_message(phone, "si", get_session(phone))))


def test_salas_reintento_ok_no_dice_problema_tecnico():
    """El bug: la cita se creaba y el paciente recibía 'problema técnico'."""
    resp = _confirmar("56932133850", _slot(82, "Ps. Jacquelinne Salas", "Psicología Adulto"),
                      "psicología adulto")
    assert len(LLAMADAS) == 2
    assert "problema técnico" not in resp.lower(), resp


def test_salas_sigue_presencial():
    """Salas atiende presencial: se manda el campo, pero la cita no pasa a online."""
    _confirmar("56932133850", _slot(82, "Ps. Jacquelinne Salas", "Psicología Adulto"),
               "psicología adulto")
    reintento = LLAMADAS[1]
    assert reintento["modalidad"] == "PRESENCIAL"
    assert reintento["forzar_videoconsulta"] is True
    data = get_session("56932133850").get("data") or {}
    assert data.get("telemedicina_modalidad") != "TELEMEDICINA"


def test_otro_profesional_conserva_reintento_telemedicina():
    """Para el resto (slots de teleconsulta reales) el comportamiento no cambia."""
    resp = _confirmar("56900000001", _slot(1, "Dr. Rodrigo Olavarría", "Medicina General"),
                      "medicina general")
    assert "problema técnico" not in resp.lower(), resp
    reintento = LLAMADAS[1]
    assert reintento["modalidad"] == "TELEMEDICINA"
    assert reintento["forzar_videoconsulta"] is False


def _medilink_limpio():
    """Copia fresca de medilink: otros módulos de test reemplazan funciones del
    módulo compartido (p. ej. `medilink.crear_cita`) a nivel global."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "medilink_limpio_videoreint", ROOT / "app" / "medilink.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_crear_cita_manda_videoconsulta_si_se_fuerza():
    ml = _medilink_limpio()
    enviados: list[dict] = []

    class _R:
        status_code = 200
        text = "{}"

        def json(self):
            return {"data": {"id": 1}}

    async def _fake_post(client, url, json=None, headers=None, **kw):
        enviados.append(json)
        return _R()

    ml._post = _fake_post
    asyncio.run(ml.crear_cita(1, 82, _FECHA, "17:00", "17:45", forzar_videoconsulta=True))
    asyncio.run(ml.crear_cita(1, 82, _FECHA, "17:00", "17:45"))
    assert enviados[0].get("videoconsulta") == 1
    assert "[ONLINE]" not in (enviados[0].get("observaciones") or "")
    assert "videoconsulta" not in enviados[1]


def test_webhook_whatsapp_reintento_sin_nombres_indefinidos():
    """En el cuerpo de `_webhook_procesar` (no en `_process_social`, que sí los
    recibe como parámetros) el reintento no puede usar `canal`/`sender_id`/`send_fn`."""
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_webhook_procesar")

    def _llamadas_directas(nodo):
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # funciones anidadas tienen su propio scope
            if (isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Attribute)
                    and hijo.func.attr == "programar"):
                yield hijo
            yield from _llamadas_directas(hijo)

    llamadas = list(_llamadas_directas(fn))
    assert llamadas, "no se encontró el reintento en el webhook de WhatsApp"
    for call in llamadas:
        for kw in call.keywords:
            for nombre in ast.walk(kw.value):
                if isinstance(nombre, ast.Name):
                    assert nombre.id not in {"canal", "sender_id", "send_fn"}, (
                        f"{kw.arg}={nombre.id}: no existe en _webhook_procesar")
