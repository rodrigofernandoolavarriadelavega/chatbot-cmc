"""Tests del motor conversacional (gated). DB temporal aislada.

Verifica: gating duro OFF por defecto; con flag ON un micro-diálogo de negociar
hora maneja sí/otra/no, respeta el tope de turnos y deriva a recepción.
Corre: `python3 tests/test_alma_agents_conversation.py`
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
_TMP = tempfile.mkdtemp(prefix="conversa_test_")
import session  # noqa: E402
session.DB_PATH = Path(_TMP) / "sessions.db"

from alma_agents import conversation as conv  # noqa: E402

_OK = 0; _FAIL = 0
def check(c, l):
    global _OK, _FAIL
    if c: _OK += 1; print(f"OK  {l}")
    else: _FAIL += 1; print(f"XX  FALLA: {l}")


def main():
    P = "56933330001"
    ctx = {"especialidad": "kinesiología", "slots": ["martes 10:00", "martes 16:00", "miércoles 9:00"]}

    # Gating duro OFF
    os.environ["ALMA_AGENTS_CONVERSA_ENABLED"] = "false"
    check(conv.is_enabled() is False, "gating OFF por defecto")
    check(conv.start(P, "negociar_hora", ctx).get("skipped"), "start no-op con flag off")
    check(conv.handle_inbound(P, "sí") is None, "handle_inbound no-op con flag off")

    # Encender
    os.environ["ALMA_AGENTS_CONVERSA_ENABLED"] = "true"
    check(conv.is_enabled() is True, "gating ON con flag")

    # Clasificador
    check(conv.classify_reply("sí, dale") == "afirmativo", "clasifica afirmativo")
    check(conv.classify_reply("otra hora porfa") == "otra_opcion", "clasifica otra_opcion")
    check(conv.classify_reply("no puedo") == "negativo", "clasifica negativo")

    # Apertura
    op = conv.start(P, "negociar_hora", ctx, agent_id="yield_agenda")
    check("martes 10:00" in op["message"], "abre ofreciendo el primer slot")
    check(conv.get_open(P) is not None, "diálogo queda abierto")

    # "otra" → ofrece el segundo slot
    r1 = conv.handle_inbound(P, "otra")
    check("martes 16:00" in r1["message"] and r1["status"] == "abierta", "‘otra’ ofrece el 2º slot")

    # "sí" → éxito + acción book con el slot vigente (idx=1 → martes 16:00)
    r2 = conv.handle_inbound(P, "sí")
    check(r2["status"] == "exito", "‘sí’ cierra con éxito")
    check(r2["action"]["kind"] == "book" and r2["action"]["slot"] == "martes 16:00",
          f"agenda el slot vigente (got {r2['action']})")
    check(conv.get_open(P) is None, "diálogo cerrado tras éxito")

    # Flujo de rechazo
    P2 = "56933330002"
    conv.start(P2, "negociar_hora", ctx)
    rneg = conv.handle_inbound(P2, "no gracias")
    check(rneg["status"] == "rechazo", "‘no’ cierra como rechazo")

    # Tope de turnos → derivación
    P3 = "56933330003"
    conv.start(P3, "negociar_hora", {"slots": ["x"], "especialidad": "kine"}, max_turns=2)
    conv.handle_inbound(P3, "quizás")  # turno 1 ambiguo
    conv.handle_inbound(P3, "ehh")     # turno 2 ambiguo
    rderiv = conv.handle_inbound(P3, "mmm")  # turno 3 > max 2 → deriva
    check(rderiv["status"] == "derivada" and rderiv["action"]["kind"] == "handoff",
          f"supera tope de turnos → deriva a recepción (got {rderiv['status']})")

    os.environ["ALMA_AGENTS_CONVERSA_ENABLED"] = "false"
    print(f"\n{_OK} OK · {_FAIL} FALLAS")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
