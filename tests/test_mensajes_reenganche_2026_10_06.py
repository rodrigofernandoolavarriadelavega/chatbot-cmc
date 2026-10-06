"""Textos de reenganche y seguimiento de información (2026-10-06).

Offline: Medilink/Meta mockeados (reusa los fakes de harness_50). Correr:
    PYTHONPATH=app:. python3 tests/test_mensajes_reenganche_2026_10_06.py
"""
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_50 as H  # noqa: E402  (DB temporal + fakes; main guardado)
import reenganche_texto as rt  # noqa: E402
import jobs  # noqa: E402
import session  # noqa: E402
import flows  # noqa: E402

FALLAS: list[str] = []


def check(nombre: str, cond: bool, extra: str = ""):
    print(("OK   " if cond else "FAIL ") + nombre + (f"  {extra}" if (extra and not cond) else ""))
    if not cond:
        FALLAS.append(nombre)


SLOT = {"fecha": "2099-01-06", "fecha_display": "Martes 6 de enero de 2099",
        "hora_inicio": "10:30", "profesional": "Leonardo Etcheverry"}
EMOJIS = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def sano(t: str) -> bool:
    return ("  " not in t and "reserva pendiente" not in t.lower()
            and "Quedan solo" not in t and "quedan solo" not in t
            and len(EMOJIS.findall(t)) <= 1)


# ── Reenganche: textos ───────────────────────────────────────────────────────
for estado in ("WAIT_SLOT", "CONFIRMING_CITA", "WAIT_MODALIDAD", "WAIT_ESPECIALIDAD",
               "WAIT_RUT_CANCELAR", "WAIT_RUT_REAGENDAR", "WAIT_RUT_VER",
               "WAIT_DURACION_MASOTERAPIA"):
    for nombre in ("", "María"):
        for slot in (SLOT, None):
            t = rt.msg_reenganche(estado, nombre, "kinesiología", slot, 3)
            check(f"reeng {estado} nombre={nombre!r} slot={bool(slot)} sano", sano(t), repr(t))
            check(f"reeng {estado} saludo", t.startswith(f"Hola {nombre} 👋" if nombre else "Hola 👋"), repr(t))

t = rt.msg_reenganche("WAIT_MODALIDAD", "", "kinesiología", SLOT, 3)
check("reeng default: honesto + pregunta directa",
      "mitad de agendar tu hora de *Kinesiología*" in t and t.endswith("¿Te ayudo a terminar?")
      and "la próxima hora disponible es el martes 6 de enero de 2099 a las 10:30 con leonardo etcheverry"
      in t.lower(), repr(t))
t = rt.msg_reenganche("WAIT_SLOT", "Ana", "abarca", SLOT, 3)
check("reeng normaliza apellido", "con *Dr. Andrés Abarca*" in t and "*abarca*" not in t
      and "con Leonardo" not in t, repr(t))
t = rt.msg_reenganche("WAIT_SLOT", "Ana", "kinesiología", None, 0)
check("reeng WAIT_SLOT sin hora: pregunta directa", t.endswith("¿Te ayudo a terminar?"), repr(t))
check("sin urgencia aunque n=1", "única" not in rt.msg_reenganche("WAIT_SLOT", "", "kinesiología", SLOT, 1))
rt.ESCASEZ_ACTIVA = True
check("flag escasez encendido usa conteo real",
      "Ese día quedan solo 3 horas." in rt.msg_reenganche("WAIT_SLOT", "", "kinesiología", SLOT, 3))
rt.ESCASEZ_ACTIVA = False
for esp in ("psicología", "psiquiatría", "ginecología", "matrona", "salas", "ecografía ginecológica"):
    t = rt.msg_reenganche("WAIT_SLOT", "Ana", esp, SLOT, 3)
    check(f"reeng sensible no nombra {esp}", not re.search("psic|psiq|gineco|matron|salas", t.lower()), repr(t))
check("variantes", rt.variante_reenganche("WAIT_SLOT", SLOT) == "a_punto_elegir_con_hora"
      and rt.variante_reenganche("WAIT_MODALIDAD", None) == "mitad_agendar_sin_hora")

# ── Seguimiento: textos ──────────────────────────────────────────────────────
t, tipo = rt.msg_followup_info("María", "kinesiología", SLOT, 3)
check("followup slot", tipo == "slot" and t.startswith("Hola María 👋") and "*Kinesiología*" in t
      and "el martes 6 de enero de 2099 a las 10:30 con Leonardo Etcheverry" in t
      and t.endswith("¿Te la reservo?") and sano(t), repr(t))
t, tipo = rt.msg_followup_info("", "psicología", SLOT, 3)
check("followup sensible: sin especialidad, con profesional",
      tipo == "slot" and "psicolog" not in t.lower() and "la hora que consultaste" in t
      and "con Leonardo Etcheverry" in t and t.startswith("Hola 👋"), repr(t))
t, tipo = rt.msg_followup_info("Ana", "ginecología", None, 0)
check("followup sensible sin Medilink", tipo == "esp" and "gineco" not in t.lower(), repr(t))
t, tipo = rt.msg_followup_info("Ana", "kinesiología", None, 0)
check("followup fallback Medilink caído con esp", tipo == "esp" and "*Kinesiología*" in t and sano(t), repr(t))
t, tipo = rt.msg_followup_info("", None, None, 0)
check("followup corto", tipo == "corto" and t.startswith("Hola 👋 ") and sano(t)
      and "¿te gustaría que te ayude a agendar una hora?" not in t, repr(t))
t, tipo = rt.msg_followup_info("Ana", "abarca", SLOT, 3)
check("followup apellido sin duplicar prof", t.count("Abarca") == 1 and "con Leonardo" not in t, repr(t))
for tp, bs in rt.BOTONES_FOLLOWUP.items():
    check(f"botones {tp} <=20 chars", all(len(b["title"]) <= 20 for b in bs))

# ── Followup job end-to-end (mocks) ──────────────────────────────────────────
ENVIADOS: list[tuple[str, dict]] = []


async def fake_send_interactive(phone, interactive):
    ENVIADOS.append((phone, interactive))

jobs.send_whatsapp_interactive = fake_send_interactive
jobs._get_followup_info_enabled = lambda: True
jobs.is_medilink_down = lambda: False
jobs.buscar_primer_dia = H.fake_buscar_primer_dia
session.is_window_open = lambda phone: True


def preparar(phone, extra: dict):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
    data = {"followup_info_ts": ts, "followup_info_sent": False, **extra}
    session.save_session(phone, "IDLE", data)
    with session.db() as c:
        c.execute("UPDATE sessions SET updated_at=datetime('now','-12 minutes') WHERE phone=?", (phone,))
        c.commit()


def ids_botones(inter):
    return [b["reply"]["id"] for b in inter["action"]["buttons"]]


def cuerpo(inter):
    return inter["body"]["text"]


async def main():
    # con especialidad detectada en la conversación (last_esp_context) + perfil con nombre
    p1 = "56911110001"
    session.save_profile(p1, "11111111-1", "MARIA PEREZ")
    preparar(p1, {"last_esp_context": "kinesiología",
                  "last_esp_context_ts": datetime.now(timezone.utc).isoformat()})
    await jobs._job_followup_info()
    ph, inter = ENVIADOS[-1]
    txt = cuerpo(inter)
    check("job: slot real + nombre de perfil", ph == p1 and txt.startswith("Hola Maria 👋")
          and "Kinesiología" in txt and "a las 09:00 con Luis Armijo" in txt, repr(txt))
    check("job: botones existentes", ids_botones(inter) == ["agendar_sugerido", "ver_otros", "no_gracias_reeng"])
    s = session.get_session(p1)
    check("job: deja especialidad_sugerida", s["data"].get("especialidad_sugerida") == "kinesiología")

    # handlers de los 3 botones
    r = await flows.handle_message(p1, "agendar_sugerido", session.get_session(p1))
    check("handler agendar_sugerido muestra horas", "Armijo" in H._normalize(r), H._normalize(r)[:200])
    preparar(p1 + "1", {"last_esp_context": "kinesiología",
                        "last_esp_context_ts": datetime.now(timezone.utc).isoformat()})
    sess = session.get_session(p1 + "1")
    sess["data"]["especialidad_sugerida_ts"] = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    sess["data"]["especialidad_sugerida"] = "kinesiología"
    r = await flows.handle_message(p1 + "1", "ver_otros", sess)
    n = H._normalize(r)
    check("handler ver_otros (aunque pasen minutos) lista horas de la especialidad",
          "Armijo" in n and session.get_session(p1 + "1")["state"] == "WAIT_SLOT", n[:200])
    sess = {"state": "IDLE", "data": {"especialidad_sugerida": "kinesiología",
                                      "especialidad_sugerida_ts": datetime.now(timezone.utc).isoformat()}}
    r = await flows.handle_message(p1, "no_gracias_reeng", sess)
    check("handler no_gracias_reeng", "Sin problema" in H._normalize(r))

    # sin especialidad -> corto, botón "1" lleva a elegir especialidad
    p2 = "56911110002"
    preparar(p2, {})
    n0 = len(ENVIADOS)
    await jobs._job_followup_info()
    ph, inter = ENVIADOS[-1]
    check("job: sin esp → corto", len(ENVIADOS) == n0 + 1 and ids_botones(inter) == ["1", "no_gracias_reeng"]
          and "Hola 👋" in cuerpo(inter), cuerpo(inter))
    r = await flows.handle_message(p2, "1", session.get_session(p2))
    check("handler '1' abre agendar", "especialidad" in H._normalize(r).lower(), H._normalize(r)[:200])

    # especialidad sensible
    p3 = "56911110003"
    preparar(p3, {"followup_info_esp": "psicología"})
    await jobs._job_followup_info()
    txt = cuerpo(ENVIADOS[-1][1])
    check("job: psicología no se nombra", ENVIADOS[-1][0] == p3 and "sicolog" not in txt
          and "con Jorge Montalba" in txt, repr(txt))

    # Medilink sin horas
    p4 = "56911110004"
    preparar(p4, {"followup_info_esp": "kinesiología"})
    H.FAKE_SIN_SLOTS["value"] = True
    jobs.buscar_primer_dia = H.fake_buscar_primer_dia
    await jobs._job_followup_info()
    H.FAKE_SIN_SLOTS["value"] = False
    inter = ENVIADOS[-1][1]
    check("job: sin horas → versión con especialidad y botones Ver horas/No",
          ENVIADOS[-1][0] == p4 and ids_botones(inter) == ["agendar_sugerido", "no_gracias_reeng"]
          and "Kinesiología" in cuerpo(inter), cuerpo(inter))

    # sin fan-out: misma especialidad, una consulta a Medilink por pasada
    llamadas = []
    orig = H.fake_buscar_primer_dia

    async def contador(*a, **k):
        llamadas.append(a)
        return await orig(*a, **k)
    jobs.buscar_primer_dia = contador
    for i in (5, 6, 7):
        preparar(f"5691111000{i}", {"followup_info_esp": "nutrición"})
    await jobs._job_followup_info()
    check("job: 3 pacientes misma especialidad → 1 consulta", len(llamadas) == 1, str(len(llamadas)))

asyncio.run(main())
print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
