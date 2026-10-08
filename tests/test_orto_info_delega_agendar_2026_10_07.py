"""Caso real 6-oct (anuncio ORTO Padres): "¡Hola! Quiero más información" →
Claude clasifica info/ortodoncia y redactaba su propia respuesta (con voseo)
+ preview de hora con la ORTODONCISTA a un mes, contradiciendo "primero la
evaluación con la dentista". Ahora la vía info delega en _iniciar_agendar:
texto acordado + horas de odontología general.

    PYTHONPATH=app:. venv/bin/python tests/test_orto_info_delega_agendar_2026_10_07.py
"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_50 as h  # instala mocks (DB temporal, Medilink, Claude)
import flows

LLAMADAS = []

async def _intent(msg, **kw):
    return {"intent": "info", "especialidad": "ortodoncia", "respuesta_directa": None}

async def _faq(msg, **kw):
    return "Texto libre de Claude: si ese mismo día comenzás o dejás pagado..."

_orig_bpd = flows.buscar_primer_dia
async def _bpd(esp, *a, **kw):
    LLAMADAS.append(esp)
    return await _orig_bpd(esp, *a, **kw)

async def _no_activo(phone):
    return 0

async def main():
    flows.detect_intent = _intent
    flows.respuesta_faq = _faq
    flows.buscar_primer_dia = _bpd
    flows._paciente_ortodoncia_activo = _no_activo
    phone = "56900000777"
    h.reset_session(phone)
    h.save_privacy_consent(phone, "accepted") if "accepted" else None
    resp = await flows.handle_message(phone, "¡Hola! Quiero más información", h.get_session(phone))
    txt = h._normalize(resp)
    fallos = []
    if "Ortodoncia en Carampangue" not in txt: fallos.append("no usa el texto acordado")
    if "comenzás" in txt or "dejás" in txt: fallos.append("voseo")
    if "Castillo" in txt: fallos.append("ofrece a la ortodoncista")
    if not LLAMADAS or any("ortodoncia" in (e or "").lower() for e in LLAMADAS):
        fallos.append(f"búsqueda de horas en {LLAMADAS}")
    print(txt[:900]); print("búsquedas:", LLAMADAS)
    print("FALLOS:", fallos or "ninguno")
    sys.exit(1 if fallos else 0)

asyncio.run(main())
