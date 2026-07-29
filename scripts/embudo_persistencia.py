#!/usr/bin/env python3
"""
embudo_persistencia.py — Medición read-only del embudo real de agendamiento.
NO modifica nada, NO envía mensajes. Corre con el venv del bot en el VPS.

Dos fuentes, para poder contrastarlas:
  (A) HISTÓRICA reconstruida desde `messages.state` (existe desde siempre,
      no depende de que el evento de turno estuviera bien instrumentado).
      Cada mensaje SALIENTE del bot queda taggeado con el `state` AL QUE
      el bot transicionó justo antes de mandarlo (ver app/main.py:9061
      `log_message(phone, "out", resp_text, state_after, ...)`). Así que
      "un teléfono tuvo un mensaje saliente con state=WAIT_SLOT" == "se le
      ofrecieron horarios", sin necesitar el evento `funnel_*`.
  (B) `conversation_events` (funnel_intent_agendar / funnel_especialidad /
      funnel_slot_ofrecido / funnel_slot_elegido / funnel_confirmacion /
      cita_creada) — la instrumentación NUEVA (2026-07-13), solo válida
      desde que se deployó (fuente de verdad hacia ADELANTE).

Uso:
  /opt/chatbot-cmc/venv/bin/python3 embudo_persistencia.py --dias 30
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/opt/chatbot-cmc/app")
sys.path.insert(0, "/opt/chatbot-cmc")

from dotenv import load_dotenv  # noqa: E402
load_dotenv("/opt/chatbot-cmc/.env")

# Estados de agendamiento (subset de _FLUJO_ACTIVO_STATES en session.py,
# excluye cancelar/reagendar/ver que no son "quiero una hora nueva").
BOOKING_STATES = [
    "WAIT_ESPECIALIDAD", "WAIT_SLOT", "WAIT_MODALIDAD", "WAIT_BOOKING_FOR",
    "WAIT_PHONE_OWNER_NAME", "WAIT_RUT_AGENDAR", "WAIT_NOMBRE_NUEVO",
    "WAIT_FECHA_NAC", "WAIT_SEXO", "WAIT_COMUNA", "WAIT_EMAIL",
    "WAIT_REFERRAL", "WAIT_REFERRAL_CODE", "WAIT_DATOS_NUEVO",
    "CONFIRMING_CITA", "WAIT_DURACION_MASOTERAPIA", "WAIT_QUICK_BOOK",
    "WAIT_WAITLIST_CONFIRM", "WAIT_WAITLIST_RUT", "WAIT_REFERRAL_POST",
    "WAIT_MEDFAM_FALLBACK", "WAIT_CONFIRMAR_ADULTO",
]
# Orden lógico del embudo (no todos los estados aplican a todos los caminos,
# pero esto da un orden razonable para el reporte).
FUNNEL_ORDER = ["WAIT_ESPECIALIDAD", "WAIT_SLOT", "WAIT_MODALIDAD",
                "WAIT_BOOKING_FOR", "WAIT_RUT_AGENDAR", "WAIT_NOMBRE_NUEVO",
                "WAIT_QUICK_BOOK", "CONFIRMING_CITA"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=30)
    args = ap.parse_args()
    dias = args.dias
    desde = f"-{dias} day"

    from session import db  # type: ignore

    with db() as c:
        print(f"\n{'='*70}\nFASE 1 — EMBUDO REAL, últimos {dias} días\n{'='*70}\n")

        # ── 0. Volumen base ──────────────────────────────────────────────
        n_phones_in = c.execute(
            "SELECT COUNT(DISTINCT phone) FROM messages WHERE direction='in' "
            "AND ts>=datetime('now',?)", (desde,)).fetchone()[0]
        n_citas = c.execute(
            "SELECT COUNT(*) FROM citas_bot WHERE created_at>=datetime('now',?)",
            (desde,)).fetchone()[0]
        n_phones_citas = c.execute(
            "SELECT COUNT(DISTINCT phone) FROM citas_bot WHERE created_at>=datetime('now',?)",
            (desde,)).fetchone()[0]
        print(f"Personas que escribieron:     {n_phones_in}")
        print(f"Citas creadas (citas_bot):    {n_citas}")
        print(f"Personas distintas que agendaron: {n_phones_citas}")
        print()

        # ── A. RECONSTRUCCIÓN HISTÓRICA vía messages.state ─────────────────
        print("── (A) Histórico reconstruido desde messages.state (funciona para CUALQUIER rango) ──")
        por_estado_phones = {}
        for st in FUNNEL_ORDER:
            rows = c.execute(
                "SELECT DISTINCT phone FROM messages WHERE direction='out' AND state=? "
                "AND ts>=datetime('now',?)", (st, desde)).fetchall()
            por_estado_phones[st] = {r[0] for r in rows}
            print(f"  {st:22s} {len(por_estado_phones[st]):5d} teléfonos distintos")
        citas_phones_set = {r[0] for r in c.execute(
            "SELECT DISTINCT phone FROM citas_bot WHERE created_at>=datetime('now',?)", (desde,)
        ).fetchall()}
        entraron = por_estado_phones.get("WAIT_ESPECIALIDAD", set()) | por_estado_phones.get("WAIT_SLOT", set())
        vieron_slot = por_estado_phones.get("WAIT_SLOT", set())
        llegaron_confirmar = por_estado_phones.get("CONFIRMING_CITA", set()) | por_estado_phones.get("WAIT_QUICK_BOOK", set())
        print()
        print(f"  Entraron al flujo (WAIT_ESPECIALIDAD ∪ WAIT_SLOT):     {len(entraron)}")
        print(f"  Vieron slots (WAIT_SLOT):                              {len(vieron_slot)}")
        print(f"  Llegaron a confirmar (CONFIRMING_CITA ∪ WAIT_QUICK_BOOK): {len(llegaron_confirmar)}")
        print(f"  Terminaron con cita (citas_bot):                       {len(citas_phones_set)}")
        if entraron:
            print(f"  Conversión entraron→cita: {len(entraron & citas_phones_set)/len(entraron)*100:.1f}%"
                  f"  ({len(entraron & citas_phones_set)}/{len(entraron)})")
        if vieron_slot:
            print(f"  Conversión vieron_slot→cita: {len(vieron_slot & citas_phones_set)/len(vieron_slot)*100:.1f}%"
                  f"  ({len(vieron_slot & citas_phones_set)}/{len(vieron_slot)})")
        if llegaron_confirmar:
            print(f"  Conversión llegaron_confirmar→cita: {len(llegaron_confirmar & citas_phones_set)/len(llegaron_confirmar)*100:.1f}%"
                  f"  ({len(llegaron_confirmar & citas_phones_set)}/{len(llegaron_confirmar)})")
        print()
        print("  Se fueron VIENDO SLOTS pero sin confirmar (WAIT_SLOT sin CONFIRMING_CITA):",
              len(vieron_slot - llegaron_confirmar))
        print("  Llegaron a confirmar pero SIN cita final (abandono en la recta final):",
              len(llegaron_confirmar - citas_phones_set))
        print("  Dieron RUT (WAIT_RUT_AGENDAR) pero nunca llegaron a confirmar:",
              len(por_estado_phones.get("WAIT_RUT_AGENDAR", set()) - llegaron_confirmar))
        print("  Datos nuevo paciente (WAIT_NOMBRE_NUEVO) pero nunca confirmaron:",
              len(por_estado_phones.get("WAIT_NOMBRE_NUEVO", set()) - llegaron_confirmar))
        print()

        # ── B. conversation_events (instrumentación NUEVA, solo hacia adelante) ─
        print("── (B) conversation_events — instrumentación nueva (funnel_id), solo válida desde el deploy ──")
        for ev in ("funnel_intent_agendar", "funnel_especialidad", "funnel_slot_ofrecido",
                   "funnel_slot_elegido", "funnel_confirmacion", "cita_creada",
                   "intent_agendar", "funnel_slot_rechazado"):
            n = c.execute(
                "SELECT COUNT(*) FROM conversation_events WHERE event=? AND ts>=datetime('now',?)",
                (ev, desde)).fetchone()[0]
            print(f"  {ev:24s} {n}")
        print()

        # ── C. Distribución completa de eventos (para no adivinar nombres) ──
        print("── (C) TODOS los eventos de conversation_events últimos", dias, "días (para no adivinar) ──")
        rows = c.execute(
            "SELECT event, COUNT(*) n FROM conversation_events WHERE ts>=datetime('now',?) "
            "GROUP BY event ORDER BY n DESC LIMIT 60", (desde,)).fetchall()
        for ev, n in rows:
            print(f"  {ev:34s} {n}")
        print()

        # ── D. Drop-off por ESTADO (timeout real, flujo_abandono/registro_abandono) ─
        print("── (D) Drop-off por estado de la máquina de estados (timeout, evento flujo_abandono) ──")
        abandono_estado = Counter()
        for meta, in c.execute(
            "SELECT meta FROM conversation_events WHERE event='flujo_abandono' "
            "AND ts>=datetime('now',?)", (desde,)).fetchall():
            try:
                d = json.loads(meta) if meta else {}
            except Exception:
                d = {}
            abandono_estado[d.get("state", "?")] += 1
        for st, n in abandono_estado.most_common(20):
            print(f"  {st:28s} {n}")
        print()
        reg_abandono = Counter()
        for meta, in c.execute(
            "SELECT meta FROM conversation_events WHERE event='registro_abandono' "
            "AND ts>=datetime('now',?)", (desde,)).fetchall():
            try:
                d = json.loads(meta) if meta else {}
            except Exception:
                d = {}
            reg_abandono[d.get("step", "?")] += 1
        print("  registro_abandono por paso:")
        for st, n in reg_abandono.most_common(20):
            print(f"    {st:26s} {n}")
        print()

        # ── E. sin_disponibilidad por especialidad (contraste con el mito) ──
        print("── (E) sin_disponibilidad por especialidad ──")
        sin_disp = Counter()
        for meta, in c.execute(
            "SELECT meta FROM conversation_events WHERE event='sin_disponibilidad' "
            "AND ts>=datetime('now',?)", (desde,)).fetchall():
            try:
                d = json.loads(meta) if meta else {}
            except Exception:
                d = {}
            sin_disp[(d.get("especialidad") or "").strip().lower()] += 1
        for esp, n in sin_disp.most_common(20):
            print(f"  {esp or '(vacío)':28s} {n}")
        print()

        # ── F. Modo "pidió HOY, no había" — disclaimer explícito en mensajes ──
        print("── (F) Modo 'pidió HOY y no había' — disclaimer 'No tengo horarios para' ──")
        hoy_msgs = c.execute(
            "SELECT phone, ts FROM messages WHERE direction='out' "
            "AND text LIKE '%No tengo horarios para%' AND ts>=datetime('now',?)",
            (desde,)).fetchall()
        n_hoy_con_cita = 0
        for phone, ts in hoy_msgs:
            row = c.execute(
                "SELECT 1 FROM citas_bot WHERE phone=? AND created_at>=? "
                "AND created_at<=datetime(?, '+3 hours') LIMIT 1", (phone, ts, ts)
            ).fetchone()
            if row:
                n_hoy_con_cita += 1
        hoy_pedido_evt = c.execute(
            "SELECT COUNT(*) FROM conversation_events WHERE event='wait_slot_hoy_pedido' "
            "AND ts>=datetime('now',?)", (desde,)).fetchone()[0]
        print(f"  Veces que se mostró el disclaimer 'no tengo para <fecha pedida>': {len(hoy_msgs)}")
        print(f"  De esos, agendaron algo en las 3h siguientes:                    {n_hoy_con_cita}"
              f" ({(n_hoy_con_cita/len(hoy_msgs)*100 if hoy_msgs else 0):.1f}%)")
        print(f"  Evento wait_slot_hoy_pedido (fallback libre con 'hoy' en WAIT_SLOT): {hoy_pedido_evt}")
        print()

        # ── G. Medicina General específico ───────────────────────────────
        print("── (G) Medicina General / Familiar — foco original del pedido ──")
        mg_esp_msgs = c.execute(
            "SELECT DISTINCT phone FROM messages WHERE direction='out' AND state='WAIT_SLOT' "
            "AND ts>=datetime('now',?) AND phone IN ("
            "  SELECT DISTINCT phone FROM messages WHERE ts>=datetime('now',?) AND direction='in' "
            "  AND (lower(text) LIKE '%medic%general%' OR lower(text) LIKE '%medic%familiar%'"
            "       OR lower(text) LIKE '%doctor%' OR lower(text) LIKE '%medico general%'))",
            (desde, desde)).fetchall()
        mg_citas = c.execute(
            "SELECT COUNT(DISTINCT phone) FROM citas_bot WHERE created_at>=datetime('now',?) "
            "AND lower(especialidad) LIKE '%medicina general%'", (desde,)).fetchone()[0]
        mg_citas_fam = c.execute(
            "SELECT COUNT(DISTINCT phone) FROM citas_bot WHERE created_at>=datetime('now',?) "
            "AND lower(especialidad) LIKE '%medicina familiar%'", (desde,)).fetchone()[0]
        print(f"  Teléfonos con texto 'medic* general/familiar/doctor' que llegaron a ver slots: {len(mg_esp_msgs)} (proxy, ruidoso)")
        print(f"  Citas Medicina General creadas: {mg_citas}   Medicina Familiar: {mg_citas_fam}")


if __name__ == "__main__":
    main()
