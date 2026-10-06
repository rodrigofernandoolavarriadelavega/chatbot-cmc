"""Opinión libre tras la encuesta postconsulta (2026-10-06). Offline: harness_50 aporta DB
temporal y mocks de Medilink/Claude; el envío de alertas se captura sin red.

Uso: PYTHONPATH=app:. venv/bin/python tests/test_opinion_mejora_2026_10_06.py
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import harness_50  # noqa: E402,F401  (monta DB temporal y mocks)
import flows  # noqa: E402
import session  # noqa: E402
import opinion_mejora as om  # noqa: E402
import meta_alertas  # noqa: E402
import resilience  # noqa: E402

with session.db() as _c:
    session._run_ddl_inline(_c)
    _c.commit()

FALLAS = []
ALERTAS = []


def check(n, c):
    print(("OK  " if c else "FAIL") + " " + n)
    if not c:
        FALLAS.append(n)


async def _falsa_alerta(texto):
    ALERTAS.append(texto)
    return "telegram"


meta_alertas.enviar_al_dueno = _falsa_alerta


def _spawn(coro, name=None):
    return asyncio.ensure_future(coro)


resilience.spawn_task = _spawn
_n = [0]


def nuevo(prof="Dr. Prueba", esp="Medicina General"):
    """Paciente con encuesta postconsulta pendiente."""
    _n[0] += 1
    ph = f"5691000{_n[0]:04d}"
    with session.db() as c:
        c.execute("INSERT INTO citas_bot(id_cita,phone,profesional,especialidad) VALUES(?,?,?,?)",
                  (f"C{_n[0]}", ph, prof, esp))
        c.execute("INSERT INTO fidelizacion_msgs(phone,tipo,cita_id,enviado_en) "
                  "VALUES(?,'postconsulta',?,datetime('now'))", (ph, f"C{_n[0]}"))
        c.execute("INSERT OR REPLACE INTO contact_profiles(phone,rut,nombre) VALUES(?,?,?)",
                  (ph, "11111111-1", "Ana Prueba"))
        c.commit()
    return ph


async def di(ph, txt):
    r = await flows.handle_message(ph, txt, session.get_session(ph))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return r


def texto(r):
    if isinstance(r, dict):
        i = r.get("interactive", {})
        return i.get("body", {}).get("text", "") + " " + json.dumps(i.get("action", {}), ensure_ascii=False)
    return r or ""


def filas(ph):
    with session.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM opinion_libre WHERE phone=?", (ph,))]


async def main():
    # 1. mejor / igual / peor -> pregunta con botón "Nada por ahora"
    for cat, bid in (("mejor", "seg_5"), ("igual", "seg_3"), ("peor", "seg_1")):
        ph = nuevo()
        r = await di(ph, bid)
        t = texto(r)
        check(f"{cat}: pregunta de mejora + botón Nada por ahora", "podamos mejorar" in t and "opinion_nada" in t)
        if cat != "mejor":
            check(f"{cat}: conserva la oferta de agendar", "seg_control" in t and "Lamentamos" in t)
        check(f"{cat}: queda pendiente en sesión",
              session.get_session(ph)["data"].get("opinion_pendiente", {}).get("respuesta") == cat)

    # 2. texto libre -> guarda + agradece (mejor, sin queja: sin alerta)
    ALERTAS.clear()
    ph = nuevo("Dra. Ruiz", "Cardiología")
    await di(ph, "seg_5")
    r = await di(ph, "Todo muy bien, solo que el estacionamiento es chico")
    f = filas(ph)
    check("texto libre: agradece", "lo vamos a revisar" in texto(r))
    check("texto libre: guarda 1 fila con contexto",
          len(f) == 1 and f[0]["profesional"] == "Dra. Ruiz" and f[0]["especialidad"] == "Cardiología"
          and f[0]["respuesta_encuesta"] == "mejor" and f[0]["cita_id"].startswith("C"))
    check("texto libre en mejor sin queja: sin alerta", not ALERTAS)
    check("pendiente consumido", "opinion_pendiente" not in session.get_session(ph)["data"])
    r2 = await di(ph, "Otra cosa que se me ocurre sobre la atención")
    check("una sola vez: segundo texto no se guarda", len(filas(ph)) == 1)

    # 3. intent claro -> no guarda, se procesa normal
    ph = nuevo()
    await di(ph, "seg_5")
    r = await di(ph, "quiero hablar con recepcion")
    check("intent humano: no se guarda como opinión", filas(ph) == [])
    check("intent humano: no responde el agradecimiento", "lo vamos a revisar" not in texto(r))
    ph = nuevo()
    await di(ph, "seg_5")
    r = await di(ph, "donde queda el centro medico")
    check("intent info: no se guarda", filas(ph) == [])

    # 4. Nada por ahora -> no pregunta más, no guarda; texto posterior no se captura
    ph = nuevo()
    await di(ph, "seg_5")
    r = await di(ph, "opinion_nada")
    check("Nada por ahora: agradece", "Gracias" in texto(r))
    check("Nada por ahora: consume pendiente", "opinion_pendiente" not in session.get_session(ph)["data"])
    await di(ph, "me gustaria dejar un comentario tarde")
    check("Nada por ahora: texto posterior no se guarda", filas(ph) == [])
    ph = nuevo()
    await di(ph, "seg_5")
    r = await di(ph, "Nada por ahora")  # texto del botón (Instagram/Messenger)
    check("Nada por ahora como texto: mismo efecto", "opinion_pendiente" not in session.get_session(ph)["data"])

    # 5. timeout 6 h
    ph = nuevo()
    await di(ph, "seg_5")
    s = session.get_session(ph)
    s["data"]["opinion_pendiente"]["ts"] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    session.save_session(ph, "IDLE", s["data"])
    r = await di(ph, "La atención fue buena en general")
    check("timeout: pasadas 6 h no se guarda", filas(ph) == [])

    # 6. alertas
    ALERTAS.clear()
    ph = nuevo("Dr. Soto", "Psicología")
    await di(ph, "seg_1")
    await di(ph, "Muy mal, nadie me ayudó con la hora")
    check("peor + texto: alerta con paciente, profesional, especialidad y texto",
          len(ALERTAS) == 1 and all(x in ALERTAS[0] for x in ("Ana Prueba", "Dr. Soto", "Psicología", "nadie me ayudó")))
    ALERTAS.clear()
    ph = nuevo("Dr. Soto", "Psicología")
    await di(ph, "seg_1")
    await di(ph, "opinion_nada")
    check("peor + Nada por ahora: alerta igual (sin comentario)", len(ALERTAS) == 1 and "sin comentario" in ALERTAS[0])
    ALERTAS.clear()
    ph = nuevo("Dra. Lira", "Psicología")
    await di(ph, "seg_5")
    await di(ph, "La videollamada no se conectó y esperé 30 minutos")
    check("mejor + queja en texto: alerta", len(ALERTAS) == 1 and "Dra. Lira" in ALERTAS[0])
    check("queja: se guarda igual", len(filas(ph)) == 1)
    for q, esperado in (("no llegó el link", True), ("nadie contestó el teléfono", True), ("esperé mucho", True),
                        ("buenas tardes, todo bien", False), ("excelente trato, gracias", False),
                        ("fue muy mala la espera", True)):
        check(f"es_queja({q!r}) == {esperado}", om.es_queja(q) == esperado)

    # 7. flag apagado
    import os
    os.environ["OPINION_MEJORA_ACTIVE"] = "false"
    try:
        ALERTAS.clear()
        for bid in ("seg_5", "seg_1"):
            ph = nuevo()
            r = await di(ph, bid)
            check(f"flag off ({bid}): sin pregunta", "podamos mejorar" not in texto(r) and "opinion_nada" not in texto(r))
            check(f"flag off ({bid}): sin pendiente", "opinion_pendiente" not in session.get_session(ph)["data"])
            await di(ph, "Texto cualquiera sobre mi atención")
            check(f"flag off ({bid}): no guarda", filas(ph) == [])
    finally:
        os.environ.pop("OPINION_MEJORA_ACTIVE")

    # 8. texto libre como respuesta a la encuesta (clasificar_respuesta_seguimiento) también pregunta
    ph = nuevo()
    r = await di(ph, "me siento mucho mejor")
    check("encuesta en texto libre: también pregunta", "podamos mejorar" in texto(r))

    # 9. integración con opinion_temas: fuente opinion_libre sin duplicar con messages
    import opinion_temas as ot
    ph = nuevo("Dr. Temas", "Cardiología")
    await di(ph, "seg_5")
    txt = "El doctor explicó todo con mucha calma"
    with session.db() as c:
        c.execute("INSERT INTO messages(phone,direction,text,ts) VALUES(?,'in',?,datetime('now'))", (ph, txt))
        c.commit()
    await di(ph, txt)
    with session.db() as c:
        c.execute(ot.DDL)
        cand_op = [x for x in ot.candidatos_opinion_libre(c, 100) if x["texto"] == txt]
        cand_ms = [x for x in ot.candidatos_mensajes(c) if x["texto"] == txt]
    check("opinion_temas: la opinión libre es candidata", len(cand_op) == 1 and cand_op[0]["fuente"] == "opinion_libre")
    check("opinion_temas: no se duplica con la fuente messages", cand_ms == [])

    class _Fake:
        messages = None

        def __init__(self):
            self.messages = self

        def create(self, **kw):
            from types import SimpleNamespace as NS
            out = json.dumps([{"i": 0, "es_opinion": False, "tema": "trato", "tono": "positivo"}])
            return NS(content=[NS(text=out)], usage=NS(input_tokens=1, output_tokens=1))
    res, _, _ = ot.clasificar_lote(_Fake(), cand_op[:1])
    check("opinion_temas: opinión libre se fuerza es_opinion=1 y conserva tema/tono",
          res[0]["es_opinion"] == 1 and res[0]["tema"] == "trato" and res[0]["tono"] == "positivo")

    print(f"\n{'TODO OK' if not FALLAS else 'FALLAS: ' + str(FALLAS)}")
    return 0 if not FALLAS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
