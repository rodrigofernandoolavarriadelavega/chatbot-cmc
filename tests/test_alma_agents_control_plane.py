"""Tests del plano de control en lenguaje natural (camino determinista). Aislado.

Verifica que instrucciones en español se traduzcan a foco + pesos por agente, que
'bajar/pausar' invierta el sentido, y que apply() escriba el runtime.
Corre: `python3 tests/test_alma_agents_control_plane.py`
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ["ALMA_AGENTS_DATA_DIR"] = tempfile.mkdtemp(prefix="control_test_")

from alma_agents import control_plane, runtime  # noqa: E402

_OK = 0; _FAIL = 0
def check(c, l):
    global _OK, _FAIL
    if c: _OK += 1; print(f"OK  {l}")
    else: _FAIL += 1; print(f"XX  FALLA: {l}")


def main():
    # 1) Priorizar kinesiología
    r = control_plane.interpret("Enfócate en llenar la agenda de kinesiología esta semana")
    check("kinesiologia" in r["focus"], f"detecta foco kinesiologia (got {r['focus']})")
    check(r["agent_weights"].get("adherencia_kine") == 2.0, "prioriza adherencia_kine (2.0)")
    check(r["agent_weights"].get("yield_agenda") == 2.0, "prioriza yield_agenda (2.0)")
    check(r["direction"] == "priorizar", "dirección = priorizar")

    # 2) Bajar la cobranza
    r2 = control_plane.interpret("Baja la cobranza, no quiero presionar a los pacientes")
    check(r2["direction"] == "bajar", "detecta 'baja' → bajar")
    check(r2["agent_weights"].get("cobranza") == 0.4, f"cobranza peso 0.4 (got {r2['agent_weights'].get('cobranza')})")

    # 3) Objetivo numérico
    r3 = control_plane.interpret("Recupera 10 pacientes inactivos")
    check(r3["target"] == 10, f"extrae target 10 (got {r3['target']})")
    check(r3["agent_weights"].get("reactivacion_winback") == 2.0, "prioriza winback")

    # 4) apply() escribe el runtime
    runtime.reset()
    out = control_plane.apply("Prioriza reputación y reseñas en Google")
    check(out["runtime"]["agent_weights"].get("reputacion") == 2.0, "apply persiste peso reputacion")
    check("reputacion" in out["runtime"]["focus"], "apply persiste foco")
    st = runtime.get_state()
    check(st["agent_weights"].get("reputacion") == 2.0, "runtime refleja el cambio")

    # 5) Instrucción sin match no rompe
    r5 = control_plane.interpret("hola qué tal")
    check(r5["focus"] == [] and r5["agent_weights"] == {}, "instrucción vaga → sin overrides")

    print(f"\n{_OK} OK · {_FAIL} FALLAS")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
