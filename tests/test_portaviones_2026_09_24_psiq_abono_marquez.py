"""
Regresión — portaviones consolidado 2026-09-24, problemas #1, #2, #3 y #12.

Verificados contra logs/DB de prod antes de tocar código (ver reporte de la
sesión). Resumen de lo que es bug real vs falso positivo:

  #1 Slots de Psiquiatría fuera de horario / fechas lejanas:
     La "hora fuera de horario" es un desajuste real de la AGENDA de Medilink
     (prof 78 cargado 14:00-18:00 mar/jue, no 16:00-20:00 como documenta el
     comentario/CLAUDE.md) — el bot lee `/profesionales/78/horarios` fielmente,
     no es un bug de código (mismo patrón que el falso positivo "Unibazo
     lunes" ya documentado: citas fantasma / desconfig de Medilink). Las
     fechas lejanas son consecuencia de la oferta real (12 cupos/semana).
     Lo único que SÍ era un bug de código: la tarjeta de oferta (WAIT_SLOT,
     "Te encontré hora ✨") nunca marcaba "Teleconsulta", a diferencia de
     CONFIRMING_CITA y el resto de mensajes de abono — el paciente solo se
     enteraba de que era por videollamada después de aceptar la hora. Fix
     en `_iniciar_agendar` (flows.py): agrega `_tele_linea` con
     `_es_teleconsulta(mejor)`, el mismo helper que ya usa el resto del bot.
     Cubierto acá: TestTeleconsultaTagEnOferta.

  #2 Abono $60.000 Paz / $65.000 Neurología "incorrecto":
     Ambos montos son correctos (ABONO_NUTRIOLOGIA_CLP=60000,
     ABONO_NEUROLOGIA_CLP=65000 en config.py, confirmado contra memoria del
     dueño). El bug real es de REDACCIÓN: `_preguntar_precio_respuesta` y
     `_preguntar_pago_respuesta` concatenaban la línea de abono-gate
     ("se paga antes de confirmar la hora") con el bloque genérico de pago
     ("se cancela al momento de la atención... No se cobra al agendar la
     hora") — contradictorio. Visto en prod: 56940013565, 56946473502.
     Cubierto acá: TestAbonoSinContradiccion.

  #3 Especialidad de Dr. Alonso Márquez inconsistente en la misma conversación
     ("Medicina General" en WAIT_SLOT/CONFIRMING_CITA, "Medicina Familiar" en
     la confirmación final/recordatorios) y precio omitido al listarlo junto
     a otro profesional. Confirmado en 5/5 conversaciones de ejemplo
     (56975778835, 56977564441, 56961770276, 56933455723, 56936253177): el
     ÚNICO lugar donde el slot nacía con "especialidad" era
     `PROFESIONALES[id_prof]["especialidad"]` en medilink.py (a propósito
     "Medicina General", por el bypass de ruteo), y solo se corregía a mano
     al GUARDAR la cita ya creada. Fix: `medilink._especialidad_display(id)`
     — choke point único usado en los dos sitios donde nace el dict del slot
     — retorna "Medicina Familiar" para id 13 sin tocar
     PROFESIONALES[13]["especialidad"] (el ruteo/menú no se toca).
     El precio omitido: `_format_slots_expansion` (listado agrupado por
     varios profesionales) nunca mostraba precio de ningún profesional.
     Cubierto acá: TestEspecialidadDisplayMarquez, TestPrecioEnExpansion.

  #12 Redacción "abono del valor total ($60.000)" + "no pagas nada" —
     ambigua (¿se cobra dos veces?). Reescrita en claude_helper.py a
     "se paga el 100% ... por adelantado ... no se cobra nada adicional".
     Cubierto acá: TestRedaccionAbonoClaudeHelper.

Ejecución:
    PYTHONPATH=app:. venv/bin/python tests/test_portaviones_2026_09_24_psiq_abono_marquez.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import harness_50 as H  # noqa: E402,F401 — aplica mocks de Medilink/Claude
import flows  # noqa: E402
import medilink  # noqa: E402
from session import reset_session  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    PASS += cond
    FAIL += not cond
    print(("OK  " if cond else "FAIL"), label, "" if cond else f"— {detail}")


# ══════════════════════════════════════════════════════════════════════════
# #1 — Teleconsulta tag en la primera tarjeta de oferta (WAIT_SLOT)
# ══════════════════════════════════════════════════════════════════════════

PHONE_TELE = "56900002501"

_SLOT_PSIQ = {
    "profesional": "Dra. Cecilia Unibazo", "id_profesional": 78,
    "especialidad": "Psiquiatría", "fecha": "2026-10-27",
    "fecha_display": "Martes 27 de octubre", "hora_inicio": "15:20",
    "hora_fin": "16:00", "duracion": 40,
}


async def _fake_psiq(*_a, **_k):
    return [dict(_SLOT_PSIQ)], [dict(_SLOT_PSIQ)]


def test_teleconsulta_tag_en_oferta():
    orig = flows.buscar_primer_dia
    flows.buscar_primer_dia = _fake_psiq
    try:
        reset_session(PHONE_TELE)
        r = H._normalize(asyncio.run(flows._iniciar_agendar(PHONE_TELE, {}, "psiquiatría")))
    finally:
        flows.buscar_primer_dia = orig
    check("la oferta de Psiquiatría marca Teleconsulta",
          "Teleconsulta" in r and "📡" in r, r)


# ══════════════════════════════════════════════════════════════════════════
# #2 — Sin contradicción abono-previo vs "se cancela al momento"
# ══════════════════════════════════════════════════════════════════════════

def test_precio_respuesta_sin_contradiccion_nutriologia():
    with patch.object(flows, "_abono_gate_psiq_activo", return_value=True):
        data = {"slot_elegido": {"especialidad": "Nutriología y Diabetología",
                                  "id_profesional": 81}}
        r = flows._preguntar_precio_respuesta(data, txt="cuanto cuesta")
    check("menciona el abono previo (Paz $60.000)",
          "Abono previo requerido" in r, r)
    check("NO contradice con 'se cancela al momento de la atención'",
          "se cancela al momento de la atención" not in r, r)
    check("NO contradice con 'No se cobra al agendar la hora'",
          "No se cobra al agendar la hora" not in r, r)
    check("aclara que no se cobra nada el día de la atención",
          "no se cobra nada adicional" in r, r)


def test_pago_respuesta_sin_contradiccion_neurologia():
    with patch.object(flows, "_abono_gate_psiq_activo", return_value=True):
        data = {"slot_elegido": {"especialidad": "Neurología", "id_profesional": 79}}
        r = flows._preguntar_pago_respuesta(data, txt="como se paga")
    check("menciona el abono previo (Neurología $65.000)",
          "Abono previo requerido" in r, r)
    check("NO contradice con 'se cancela al momento de la atención'",
          "se cancela al momento de la atención" not in r, r)


def test_pago_respuesta_medicina_general_sigue_igual():
    # Control: especialidad SIN abono-gate no cambia de comportamiento.
    with patch.object(flows, "_abono_gate_psiq_activo", return_value=True):
        data = {"slot_elegido": {"especialidad": "Medicina General", "id_profesional": 1}}
        r = flows._preguntar_pago_respuesta(data, txt="como se paga")
    check("Medicina General sigue diciendo 'se cancela al momento de la atención'",
          "se cancela al momento de la atención" in r, r)


# ══════════════════════════════════════════════════════════════════════════
# #3a — Especialidad de Márquez consistente ("Medicina Familiar", no MG)
# ══════════════════════════════════════════════════════════════════════════

def test_especialidad_display_marquez():
    check("Márquez (13) se muestra como Medicina Familiar",
          medilink._especialidad_display(13) == "Medicina Familiar")
    check("Abarca (73) sigue como Medicina General",
          medilink._especialidad_display(73) == "Medicina General")
    check("Olavarría (1) sigue como Medicina General",
          medilink._especialidad_display(1) == "Medicina General")
    check("PROFESIONALES[13] NO se tocó (ruteo/menú intactos)",
          medilink.PROFESIONALES[13]["especialidad"] == "Medicina General")


PHONE_MARQ = "56900002502"

_SLOT_MARQUEZ_AGENDAS = {
    "profesional": "Dr. Alonso Márquez", "id_profesional": 13,
    "fecha": "2026-09-28", "fecha_display": "Lunes 28 de septiembre",
    "hora_inicio": "17:00", "hora_fin": "17:20", "id_recurso": 1,
}


async def _fake_marquez_overflow(especialidad, dias_adelante=60, excluir=None,
                                  intervalo_override=None, solo_ids=None,
                                  fecha_desde=None, fecha_hasta=None, **kw):
    # Simula el overflow real: Abarca/Olavarría sin cupo, Márquez sí.
    if solo_ids and 13 in [int(i) for i in solo_ids]:
        slot = dict(_SLOT_MARQUEZ_AGENDAS)
        slot["especialidad"] = medilink._especialidad_display(13)
        return [slot], [slot]
    return [], []


def test_iniciar_agendar_marquez_overflow_ya_dice_medicina_familiar():
    """Reproduce el camino real (MG genérico → overflow a Márquez): la
    PRIMERA tarjeta que ve el paciente (WAIT_SLOT) debe decir 'Medicina
    Familiar', no 'Medicina General' — antes del fix decía General acá y
    recién cambiaba a Familiar en el mensaje final tras crear la cita."""
    orig = flows.buscar_primer_dia
    flows.buscar_primer_dia = _fake_marquez_overflow
    try:
        reset_session(PHONE_MARQ)
        r = H._normalize(asyncio.run(flows._iniciar_agendar(PHONE_MARQ, {}, "medicina general")))
    finally:
        flows.buscar_primer_dia = orig
    check("la oferta a Márquez dice 'Medicina Familiar'",
          "Medicina Familiar" in r, r)
    check("la oferta a Márquez NO dice 'Medicina General'",
          "Medicina General" not in r, r)


# ══════════════════════════════════════════════════════════════════════════
# #3b — Precio visible al listar varios profesionales agrupados
# ══════════════════════════════════════════════════════════════════════════

def test_precio_en_listado_multi_profesional():
    grupos = [
        {"slots": [{
            "profesional": "Dr. Andrés Abarca", "id_profesional": 73,
            "especialidad": "Medicina General", "fecha_display": "Lunes 28/09",
            "hora_inicio": "10:00", "hora_fin": "10:15",
        }]},
        {"slots": [{
            "profesional": "Dr. Alonso Márquez", "id_profesional": 13,
            "especialidad": medilink._especialidad_display(13),
            "fecha_display": "Lunes 28/09",
            "hora_inicio": "17:00", "hora_fin": "17:20",
        }]},
    ]
    # Fuerza el fallback de texto (>10 filas) para no depender del formato
    # de lista interactiva de WhatsApp.
    grupos_grandes = [
        {"slots": g["slots"] * 6} for g in grupos
    ]
    r = flows._format_slots_expansion(grupos_grandes, show_ver_mas=False)
    assert isinstance(r, str), f"esperaba texto plano, llegó {type(r)}"
    check("aparece el precio de Abarca ($25.000)", "25.000" in r, r)
    check("aparece el precio distinto de Márquez ($30.000)", "30.000" in r, r)


# ══════════════════════════════════════════════════════════════════════════
# #12 — Redacción del abono ya no es ambigua en claude_helper.py
# ══════════════════════════════════════════════════════════════════════════

def test_redaccion_abono_claude_helper():
    import claude_helper
    src_path = Path(claude_helper.__file__)
    src = src_path.read_text(encoding="utf-8")
    check("ya no queda la frase ambigua 'abono del valor total'",
          "abono del valor total" not in src)
    check("ya no queda 'abono del total' (Paz)",
          "abono del total" not in src)
    check("la nueva redacción explica el 100% por adelantado (Psiquiatría)",
          "100% del valor ($60.000) por adelantado" in src
          or "100% ($60.000) por adelantado" in src)
    check("Neurología también explica el 100% por adelantado",
          "100% del valor ($65.000) por adelantado" in src)


def main():
    test_teleconsulta_tag_en_oferta()
    test_precio_respuesta_sin_contradiccion_nutriologia()
    test_pago_respuesta_sin_contradiccion_neurologia()
    test_pago_respuesta_medicina_general_sigue_igual()
    test_especialidad_display_marquez()
    test_iniciar_agendar_marquez_overflow_ya_dice_medicina_familiar()
    test_precio_en_listado_multi_profesional()
    test_redaccion_abono_claude_helper()


if __name__ == "__main__":
    main()
    print(f"\n{PASS} pasaron, {FAIL} fallaron")
    sys.exit(1 if FAIL else 0)
