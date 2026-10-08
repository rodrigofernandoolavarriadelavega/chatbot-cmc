"""Caso real 7-oct (auditoría nocturna 2026-10-08): lead del anuncio
"Brackets sin viajar $120K" recibía "No hay horas disponibles esta semana para
*Ortodoncia*" porque el saludo CTWA buscaba 3 horas de la ORTODONCISTA (agenda
llena). La primera evaluación de ortodoncia es con la dentista general
(Dra. Burgos): el saludo debe delegar en _iniciar_agendar, que ya aplica esa
regla (y manda a la Dra. Castillo solo a quien ya está en tratamiento).

    PYTHONPATH=app:. venv/bin/python tests/test_orto_anuncio_ctwa_2026_10_08.py
"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_50 as h  # instala mocks (DB temporal, Medilink, Claude)
import flows
import medilink
import session as _session

BUSQUEDAS, TOP3, ENVIADOS = [], [], []

_orig_bpd = flows.buscar_primer_dia
async def _bpd(esp, *a, **kw):
    BUSQUEDAS.append(esp)
    return await _orig_bpd(esp, *a, **kw)

async def _top3(esp, dias=7):
    TOP3.append(esp)
    return []          # agenda de la ortodoncista llena (el caso real)

async def _send(phone, msg, *a, **kw):
    ENVIADOS.append(msg)
    return "wamid.test"

def _referral(headline):
    return lambda phone, ttl_horas=24: {"headline": headline}

async def _caso(headline, activo, phone):
    BUSQUEDAS.clear(); TOP3.clear(); ENVIADOS.clear()
    async def _act(p):
        return activo
    flows._paciente_ortodoncia_activo = _act
    _session.get_meta_referral_fresh = _referral(headline)
    h.reset_session(phone)
    resp = await flows.handle_message(phone, "Hola", h.get_session(phone))
    return h._normalize(resp)

async def main():
    flows.buscar_primer_dia = _bpd
    flows.send_whatsapp = _send
    medilink.top3_slots_especialidad = _top3
    fallos = []

    # 1) Paciente nuevo desde anuncio de brackets → evaluación con la dentista
    txt = await _caso("Brackets sin viajar $120K", 0, "56900000881")
    if "No hay horas disponibles esta semana" in txt: fallos.append("nuevo: dice 'no hay horas'")
    if "Ortodoncia en Carampangue" not in txt: fallos.append("nuevo: no usa el texto acordado")
    if "Castillo" in txt: fallos.append("nuevo: ofrece a la ortodoncista")
    if TOP3: fallos.append(f"nuevo: buscó top3 de {TOP3}")
    if any("ortodoncia" in (e or "").lower() for e in BUSQUEDAS):
        fallos.append(f"nuevo: buscó horas de ortodoncia {BUSQUEDAS}")
    if not any("asistente automático" in (m or "") for m in ENVIADOS):
        fallos.append("nuevo: no envió el disclosure aparte")
    print("NUEVO:", txt[:300].replace("\n", " "), "| búsquedas:", BUSQUEDAS)

    # 2) Paciente ya en tratamiento → directo con la Dra. Castillo
    txt = await _caso("Brackets sin viajar $120K", 3, "56900000882")
    if "Castillo" not in txt: fallos.append("activo: no va con la ortodoncista")
    if "No hay horas disponibles esta semana" in txt: fallos.append("activo: dice 'no hay horas' del CTWA")
    print("ACTIVO:", txt[:200].replace("\n", " "))

    # 3) Otra especialidad no cambia: sigue ofreciendo top3 del anuncio
    txt = await _caso("Psicología", 0, "56900000883")
    if "psicolog" not in " ".join(TOP3).lower(): fallos.append(f"psico: no buscó top3 ({TOP3})")
    print("PSICO top3:", TOP3)

    print("FALLOS:", fallos or "ninguno")
    sys.exit(1 if fallos else 0)

asyncio.run(main())
