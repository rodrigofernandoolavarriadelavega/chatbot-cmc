#!/usr/bin/env python3
"""
medir_conversion_postfix.py
----------------------------
Mide el impacto de los fixes deployados ~2026-05-29 comparando una ventana
PRE-deploy con una ventana POST-deploy, usando sessions.db de producción.

Solo lectura — no modifica la DB.

Uso:
    python3 medir_conversion_postfix.py [opciones]

    --pre-inicio    YYYY-MM-DD  (default: 2026-05-15)
    --pre-fin       YYYY-MM-DD  (default: 2026-05-28)
    --post-inicio   YYYY-MM-DD  (default: 2026-05-29)
    --post-fin      YYYY-MM-DD  (default: hoy)
    --db            ruta a sessions.db (default: /opt/chatbot-cmc/data/sessions.db)
    --key           clave SQLCipher hex (default: lee SQLCIPHER_KEY del entorno)

Cómo ejecutar contra producción desde local:
    sshpass -p 'PASS' ssh root@157.245.13.107 \
      'cd /opt/chatbot-cmc && venv/bin/python3 scripts/medir_conversion_postfix.py'

    O copiando el script al servidor:
    sshpass -p 'PASS' scp scripts/medir_conversion_postfix.py root@157.245.13.107:/opt/chatbot-cmc/scripts/
    sshpass -p 'PASS' ssh root@157.245.13.107 \
      'cd /opt/chatbot-cmc && venv/bin/python3 scripts/medir_conversion_postfix.py'

Schema real verificado 2026-05-29:
  - conversation_events(id, phone, event, meta, ts)  — campo "meta" (no "payload")
  - sessions(phone, state, data, updated_at)          — campo "data" (no "slot_data")
  - messages(id, phone, direction, text, state, ts, canal, wamid, ...)
  - citas_bot(id, phone, id_cita, especialidad, profesional, fecha, hora, created_at, ...)
  - meta_referrals(id, phone, source_type, ctwa_clid, ts, ...)
  - waitlist(id, phone, especialidad, created_at, ...)
  - demanda_no_disponible(id, phone, solicitud, tipo, created_at)

Eventos clave confirmados en la DB (al 2026-05-29):
  intent_agendar, intent_detectado, cita_creada, cita_cancelada
  derivado_humano, human_takeover_clinico
  ecografia_sin_tipo, ecografia_tipo_matched
  savings:ecografia_routing
  ctwa_slots_ofrecidos, ctwa_slot_elegido, ctwa_sin_disponibilidad
  meta_referral_capturado, bienvenida_adaptativa_meta
  modo_degradado, modo_degradado_waitlist_ofrecida, sin_disponibilidad
  waitlist_inscrito, waitlist_inscrito_auto
  intent_pay_prefilter                      — filtro de intención de pago (preguntas precio)
  flujo_abandono                            — abandono con estado en meta JSON
  template_enviado                          — templates proactivos (ej. postconsulta_seguimiento)
  faq_agendar_acepto                        — aceptó agendar desde FAQ
  ortodoncia_redirigida_odonto              — ortodoncia → odontología general
  quick_book_offered, quick_book_accepted, quick_book_agendar_ok

NOTAS SOBRE EVENTOS INEXISTENTES:
  - "events" y "log_event" no existen; la tabla real es "conversation_events"
  - "ecografia_sin_tipo" SÍ existe (84 ocurrencias al 2026-05-29)
  - "proactive_pending_consumido" NO existe; el análogo real es "template_enviado"
     con meta JSON {"template": "postconsulta_seguimiento", ...}
  - "payload" no existe en conversation_events; el campo es "meta"
  - El estado de sesión "HUMAN_TAKEOVER" existe en sessions.state
  - "cita_anulada" no existe; el evento real es "cita_cancelada"
"""

import sys
import os
import re
import argparse
import json
from datetime import date, datetime
from pathlib import Path

# ─── Conectar a SQLCipher (mismo mecanismo que session.py) ────────────────────

def _get_connection(db_path: str, key: str):
    """
    Abre sessions.db con SQLCipher si hay key, o sqlite3 plano si no.
    Replica el patrón de app/session.py exactamente.
    """
    if not re.fullmatch(r"[0-9a-fA-F]+", key):
        raise ValueError("SQLCIPHER_KEY debe ser hex (0-9, a-f).")

    # Intentar sqlcipher3 (instalado en venv del servidor)
    try:
        from sqlcipher3 import dbapi2 as _sc  # type: ignore
        conn = _sc.connect(db_path, timeout=10)
        conn.execute(f"PRAGMA key = \"x'{key}'\"")
        conn.row_factory = _sc.Row
        # Verificar que funciona
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn, "sqlcipher3"
    except ImportError:
        pass
    except Exception as exc:
        raise RuntimeError(f"SQLCipher falló con la key dada: {exc}") from exc

    # Fallback: pysqlcipher3
    try:
        from pysqlcipher3 import dbapi2 as _psc  # type: ignore
        conn = _psc.connect(db_path, timeout=10)
        conn.execute(f"PRAGMA key = \"x'{key}'\"")
        conn.row_factory = _psc.Row
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn, "pysqlcipher3"
    except ImportError:
        pass

    raise RuntimeError(
        "No se encontró sqlcipher3 ni pysqlcipher3. "
        "Asegurate de correr con el venv del servidor: venv/bin/python3"
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _q(conn, sql: str, params=()):
    """Ejecuta query y retorna lista de Row."""
    return conn.execute(sql, params).fetchall()


def _scalar(conn, sql: str, params=()):
    """Retorna el primer campo del primer row."""
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else 0


def _pct(num, den) -> str:
    """Formatea porcentaje con 1 decimal, o '-' si denominador es 0."""
    if den == 0:
        return "-"
    return f"{100 * num / den:.1f}%"


def _delta(pre_val, post_val, is_pct_str=False) -> str:
    """
    Calcula cambio relativo entre dos valores.
    Si is_pct_str=True, pre_val y post_val son strings "X.X%" → extrae float.
    Retorna string con signo, ej "+12.3%" o "-8.0%".
    """
    def _to_float(v):
        if isinstance(v, str):
            if v == "-":
                return None
            try:
                return float(v.rstrip("%"))
            except ValueError:
                return None  # string descriptivo, no numérico
        return float(v) if v else 0.0

    pre = _to_float(pre_val)
    post = _to_float(post_val)
    if pre is None or post is None:
        return "n/d"
    if pre == 0:
        if post == 0:
            return "0%"
        return "+inf"
    change = (post - pre) / abs(pre) * 100
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.1f}%"


def _fmt(v) -> str:
    """Formatea entero con separador de miles."""
    if isinstance(v, (int, float)):
        return f"{int(v):,}"
    return str(v)


# ─── Métricas ─────────────────────────────────────────────────────────────────

def m1_ecografia(conn, ini: str, fin: str) -> dict:
    """
    M1 — Ecografía: conversión intent → cita_creada; count ecografia_sin_tipo.

    Fuente:
      - intent_detectado con meta JSON {"esp": "ecografía"} → intent_agendar
        correlacionado por phone
      - cita_creada en mismo phone dentro de la ventana
      - ecografia_sin_tipo: evento directo
      - ecografia_tipo_matched: evento post-fix (el tipo fue identificado)
      - savings:ecografia_routing: routing correcto de ecografía

    Definición conversión: phones que tuvieron intent_agendar con esp ecografía
    y luego cita_creada en cualquier fecha (dentro del periodo intent_agendar).
    """
    # Phones con intent de agendar ecografía en el periodo
    intents = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM conversation_events
        WHERE event = 'intent_detectado'
          AND ts >= ? AND ts < ?
          AND json_extract(meta, '$.esp') IN ('ecografía','ecografia','Ecografía','Ecografia')
          AND json_extract(meta, '$.intent') = 'agendar'
    """, (ini, fin))

    # Subset que luego tuvo cita_creada (citas_bot.especialidad LIKE 'Eco%')
    citas = _scalar(conn, """
        SELECT COUNT(DISTINCT cb.phone)
        FROM citas_bot cb
        JOIN conversation_events ev ON ev.phone = cb.phone
        WHERE ev.event = 'intent_detectado'
          AND ev.ts >= ? AND ev.ts < ?
          AND json_extract(ev.meta, '$.esp') IN ('ecografía','ecografia','Ecografía','Ecografia')
          AND json_extract(ev.meta, '$.intent') = 'agendar'
          AND cb.especialidad LIKE 'Eco%'
          AND cb.created_at >= ?
    """, (ini, fin, ini))

    # ecografia_sin_tipo (el loop que perdía pacientes)
    sin_tipo = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'ecografia_sin_tipo' AND ts >= ? AND ts < ?
    """, (ini, fin))

    # ecografia_tipo_matched (post-fix: el tipo fue reconocido)
    tipo_matched = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'ecografia_tipo_matched' AND ts >= ? AND ts < ?
    """, (ini, fin))

    # savings:ecografia_routing
    routing = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'savings:ecografia_routing' AND ts >= ? AND ts < ?
    """, (ini, fin))

    return {
        "intents_eco_agendar": intents,
        "citas_eco_creadas": citas,
        "tasa_conv": _pct(citas, intents),
        "ecografia_sin_tipo": sin_tipo,
        "ecografia_tipo_matched": tipo_matched,
        "ecografia_routing": routing,
    }


def m2_ctwa(conn, ini: str, fin: str) -> dict:
    """
    M2 — CTWA / Meta referral: conversión slots_ofrecidos → slot_elegido → cita.

    Fuente:
      - meta_referrals: phones que entraron por CTWA (tienen ctwa_clid)
      - ctwa_slots_ofrecidos: slots presentados al usuario
      - ctwa_slot_elegido: usuario eligió un slot
      - cita_creada: phones que tuvieron meta_referral CTWA y luego cita_creada

    Nota: meta_referrals.ts es INTEGER (epoch), no TEXT como el resto.
    Se convierte con datetime.fromtimestamp.
    """
    ini_epoch = int(datetime.fromisoformat(ini).timestamp())
    fin_epoch = int(datetime.fromisoformat(fin).timestamp())

    # Phones que entraron por CTWA en el periodo
    ctwa_phones = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM meta_referrals
        WHERE ts >= ? AND ts < ? AND ctwa_clid != '' AND ctwa_clid IS NOT NULL
    """, (ini_epoch, fin_epoch))

    # ctwa_slots_ofrecidos
    slots_ofrecidos = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'ctwa_slots_ofrecidos' AND ts >= ? AND ts < ?
    """, (ini, fin))

    slots_ofrecidos_phones = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM conversation_events
        WHERE event = 'ctwa_slots_ofrecidos' AND ts >= ? AND ts < ?
    """, (ini, fin))

    # ctwa_slot_elegido
    slot_elegido = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'ctwa_slot_elegido' AND ts >= ? AND ts < ?
    """, (ini, fin))

    slot_elegido_phones = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM conversation_events
        WHERE event = 'ctwa_slot_elegido' AND ts >= ? AND ts < ?
    """, (ini, fin))

    # ctwa_sin_disponibilidad
    ctwa_sin_disp = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'ctwa_sin_disponibilidad' AND ts >= ? AND ts < ?
    """, (ini, fin))

    # Phones CTWA que luego tuvieron cita_creada
    ctwa_cita = _scalar(conn, """
        SELECT COUNT(DISTINCT ce.phone)
        FROM conversation_events ce
        WHERE ce.event = 'cita_creada'
          AND ce.ts >= ? AND ce.ts < ?
          AND ce.phone IN (
              SELECT phone FROM meta_referrals
              WHERE ts >= ? AND ts < ? AND ctwa_clid != '' AND ctwa_clid IS NOT NULL
          )
    """, (ini, fin, ini_epoch, fin_epoch))

    return {
        "ctwa_phones_referidos": ctwa_phones,
        "ctwa_slots_ofrecidos": slots_ofrecidos,
        "ctwa_slots_ofrecidos_phones": slots_ofrecidos_phones,
        "ctwa_slot_elegido": slot_elegido,
        "ctwa_slot_elegido_phones": slot_elegido_phones,
        "ctwa_sin_disponibilidad": ctwa_sin_disp,
        "ctwa_cita_creada_phones": ctwa_cita,
        # Tasa: slot_elegido / slots_ofrecidos (ambos por phone)
        "tasa_slot_elegido": _pct(slot_elegido_phones, slots_ofrecidos_phones),
        # Tasa final: cita / phones referidos
        "tasa_cita_sobre_referidos": _pct(ctwa_cita, ctwa_phones),
    }


def m3_precio(conn, ini: str, fin: str) -> dict:
    """
    M3 — Precio: preguntas de precio → respuesta con bloque de métodos de pago vs. respuesta de precio.

    El fix esperado: ante "¿cuánto cuesta?", el bot responde con el precio,
    NO con el bloque de métodos de pago.

    Fuente:
      - messages inbound con palabras clave de precio
      - intent_pay_prefilter: intent que dispara la detección de pregunta de pago
        (en PRE esto también capturaba "cuánto cuesta" → mostraba métodos de pago)
      - messages outbound que contienen el bloque de pago ("transferencia" + "débito")
        apareciendo como respuesta inmediata a preguntas de precio (proxy)
      - El fix introduce respuesta de precio directo, se puede medir como:
        ausencia del bloque de pago en respuestas a preguntas precio

    Estrategia: para cada phone con mensaje inbound de precio en el periodo,
    ver si el siguiente mensaje outbound contiene el bloque de métodos de pago.
    """
    # Mensajes inbound con palabras de precio
    precio_in = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM messages
        WHERE direction = 'in'
          AND ts >= ? AND ts < ?
          AND (text LIKE '%cuánto%' OR text LIKE '%cuanto%' OR text LIKE '%precio%'
               OR text LIKE '%valor%' OR text LIKE '%cuesta%' OR text LIKE '%cobr%')
    """, (ini, fin))

    # intent_pay_prefilter (evento que registra pregunta tipo pago/precio)
    intent_pay = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'intent_pay_prefilter' AND ts >= ? AND ts < ?
    """, (ini, fin))

    # Mensajes outbound que son el bloque de métodos de pago completo
    # (contienen "transferencia" Y "débito" Y son salientes)
    # Proxy: respuestas del bot con bloque de pago en periodo
    bloque_pago_out = _scalar(conn, """
        SELECT COUNT(*) FROM messages
        WHERE direction = 'out'
          AND ts >= ? AND ts < ?
          AND text LIKE '%transferencia%'
          AND (text LIKE '%débito%' OR text LIKE '%debito%')
          AND text LIKE '%No se cobra al%'
    """, (ini, fin))

    # Phones que preguntaron precio Y recibieron bloque de métodos de pago
    # (par: mismo phone, mensaje out con bloque de pago después de mensaje in de precio)
    phones_precio_bloque = _scalar(conn, """
        SELECT COUNT(DISTINCT m_in.phone)
        FROM messages m_in
        JOIN messages m_out ON m_out.phone = m_in.phone
        WHERE m_in.direction = 'in'
          AND m_in.ts >= ? AND m_in.ts < ?
          AND (m_in.text LIKE '%cuánto%' OR m_in.text LIKE '%cuanto%'
               OR m_in.text LIKE '%precio%' OR m_in.text LIKE '%valor%'
               OR m_in.text LIKE '%cuesta%')
          AND m_out.direction = 'out'
          AND m_out.ts > m_in.ts
          AND m_out.ts <= datetime(m_in.ts, '+5 minutes')
          AND m_out.text LIKE '%transferencia%'
          AND (m_out.text LIKE '%débito%' OR m_out.text LIKE '%debito%')
          AND m_out.text LIKE '%No se cobra al%'
    """, (ini, fin))

    return {
        "phones_preguntaron_precio": precio_in,
        "intent_pay_prefilter": intent_pay,
        "bloque_pago_enviado": bloque_pago_out,
        "phones_precio_que_recibieron_bloque": phones_precio_bloque,
        # Tasa: phones que recibieron bloque / phones que preguntaron precio
        "tasa_bloque_sobre_precio": _pct(phones_precio_bloque, precio_in),
    }


def m4_abandono(conn, ini: str, fin: str) -> dict:
    """
    M4 — Abandono en flujo de agendamiento.

    Fuente:
      - intent_agendar: inicio del flujo
      - cita_creada: completion
      - flujo_abandono: evento con meta JSON {"state": "WAIT_SLOT", ...}
      - También se calcula con todos los phones con intent_agendar sin cita posterior

    El abandono se categoriza por estado de sesión (WAIT_SLOT, WAIT_RUT_AGENDAR,
    WAIT_ESPECIALIDAD, etc.).
    """
    # Total phones con intent_agendar en el periodo
    total_intent = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM conversation_events
        WHERE event = 'intent_agendar' AND ts >= ? AND ts < ?
    """, (ini, fin))

    # Phones con cita_creada posterior al intent_agendar (conversión completa)
    cita_post_intent = _scalar(conn, """
        SELECT COUNT(DISTINCT e1.phone)
        FROM conversation_events e1
        JOIN conversation_events e2 ON e2.phone = e1.phone
        WHERE e1.event = 'intent_agendar' AND e1.ts >= ? AND e1.ts < ?
          AND e2.event = 'cita_creada' AND e2.ts >= e1.ts
    """, (ini, fin))

    abandono_total = total_intent - cita_post_intent

    # flujo_abandono registrado (subset con evento explícito)
    flujo_abandono_ev = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'flujo_abandono' AND ts >= ? AND ts < ?
    """, (ini, fin))

    # Breakdown por estado en flujo_abandono
    abandono_breakdown = _q(conn, """
        SELECT json_extract(meta, '$.state') as state, COUNT(*) as n
        FROM conversation_events
        WHERE event = 'flujo_abandono' AND ts >= ? AND ts < ?
        GROUP BY state ORDER BY n DESC
    """, (ini, fin))

    breakdown_str = "; ".join(
        f"{r[0] or 'null'}:{r[1]}" for r in abandono_breakdown
    ) or "sin registros"

    return {
        "phones_intent_agendar": total_intent,
        "phones_cita_post_intent": cita_post_intent,
        "phones_abandonaron": abandono_total,
        "tasa_abandono": _pct(abandono_total, total_intent),
        "tasa_conversion": _pct(cita_post_intent, total_intent),
        "flujo_abandono_eventos": flujo_abandono_ev,
        "abandono_por_estado": breakdown_str,
    }


def m5_takeover(conn, ini: str, fin: str) -> dict:
    """
    M5 — HUMAN_TAKEOVER / derivado_humano.

    Fuente:
      - derivado_humano: evento cuando el bot deriva a humano (voluntario/forzado)
      - human_takeover_clinico: takeover por motivo clínico o conversacional
      - sessions.state = 'HUMAN_TAKEOVER': sesiones actualmente en takeover
        (snapshot puntual, no comparable entre ventanas de fecha)
      - auto_takeover_recep_reply: cuando recepción respondió manualmente

    Se espera que con los fixes (retiro de informe, pediatría, ORL, etc.) bajen
    derivado_humano y human_takeover_clinico por motivos evitables.
    """
    derivado = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'derivado_humano' AND ts >= ? AND ts < ?
    """, (ini, fin))

    derivado_phones = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM conversation_events
        WHERE event = 'derivado_humano' AND ts >= ? AND ts < ?
    """, (ini, fin))

    clinico = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'human_takeover_clinico' AND ts >= ? AND ts < ?
    """, (ini, fin))

    auto_takeover = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'auto_takeover_recep_reply' AND ts >= ? AND ts < ?
    """, (ini, fin))

    # Razones de derivado_humano (meta JSON)
    razon_breakdown = _q(conn, """
        SELECT json_extract(meta, '$.razon') as razon, COUNT(*) as n
        FROM conversation_events
        WHERE event = 'derivado_humano' AND ts >= ? AND ts < ?
        GROUP BY razon ORDER BY n DESC LIMIT 6
    """, (ini, fin))

    razones_str = "; ".join(
        f"{r[0] or 'null'}:{r[1]}" for r in razon_breakdown
    ) or "sin registros"

    # Total mensajes outbound para normalizar (tráfico total)
    total_out = _scalar(conn, """
        SELECT COUNT(*) FROM messages WHERE direction = 'out' AND ts >= ? AND ts < ?
    """, (ini, fin))

    return {
        "derivado_humano_total": derivado,
        "derivado_humano_phones": derivado_phones,
        "human_takeover_clinico": clinico,
        "auto_takeover_recep_reply": auto_takeover,
        "razones_top": razones_str,
        "msgs_out_total": total_out,
        "tasa_takeover_sobre_msgs": _pct(derivado, total_out) if total_out else "-",
    }


def m6_proactive(conn, ini: str, fin: str) -> dict:
    """
    M6 — Templates proactivos: respuesta → cita directa.

    El fix esperado: "Si acepto" en template proactivo → cita_creada sin caer a menú.
    Evento esperado: proactive_pending_consumido (NO existe en DB actual).
    Evento real análogo: template_enviado + luego faq_agendar_acepto en mismo phone.

    Fuente:
      - template_enviado: template enviado (ej. postconsulta_seguimiento, winback)
      - faq_agendar_acepto: usuario aceptó agendar desde flujo FAQ/proactive
      - cita_creada posterior a template_enviado en mismo phone
      - quick_book_offered / quick_book_accepted / quick_book_agendar_ok:
        flujo de reserva rápida (post-template)

    NOTA: proactive_pending_consumido NO existe en la DB. Se usa faq_agendar_acepto
    como proxy de "usuario respondió sí al template".
    """
    # Templates enviados
    templates_enviados = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'template_enviado' AND ts >= ? AND ts < ?
    """, (ini, fin))

    templates_phones = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM conversation_events
        WHERE event = 'template_enviado' AND ts >= ? AND ts < ?
    """, (ini, fin))

    # Tipos de template
    template_tipos = _q(conn, """
        SELECT json_extract(meta, '$.template') as tmpl, COUNT(*) as n
        FROM conversation_events
        WHERE event = 'template_enviado' AND ts >= ? AND ts < ?
        GROUP BY tmpl ORDER BY n DESC LIMIT 8
    """, (ini, fin))
    tipos_str = "; ".join(f"{r[0] or 'null'}:{r[1]}" for r in template_tipos) or "sin registros"

    # faq_agendar_acepto (proxy de "sí acepto" post-template)
    faq_acepto = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'faq_agendar_acepto' AND ts >= ? AND ts < ?
    """, (ini, fin))

    faq_acepto_phones = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM conversation_events
        WHERE event = 'faq_agendar_acepto' AND ts >= ? AND ts < ?
    """, (ini, fin))

    # quick_book flow
    qb_offered = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'quick_book_offered' AND ts >= ? AND ts < ?
    """, (ini, fin))
    qb_accepted = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'quick_book_accepted' AND ts >= ? AND ts < ?
    """, (ini, fin))
    qb_ok = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'quick_book_agendar_ok' AND ts >= ? AND ts < ?
    """, (ini, fin))

    # Phones que tuvieron template Y luego cita_creada
    template_to_cita = _scalar(conn, """
        SELECT COUNT(DISTINCT e1.phone)
        FROM conversation_events e1
        JOIN conversation_events e2 ON e2.phone = e1.phone
        WHERE e1.event = 'template_enviado' AND e1.ts >= ? AND e1.ts < ?
          AND e2.event = 'cita_creada' AND e2.ts >= e1.ts
    """, (ini, fin))

    return {
        "templates_enviados": templates_enviados,
        "templates_phones": templates_phones,
        "tipos_templates": tipos_str,
        "faq_agendar_acepto": faq_acepto,
        "faq_acepto_phones": faq_acepto_phones,
        "quick_book_offered": qb_offered,
        "quick_book_accepted": qb_accepted,
        "quick_book_agendar_ok": qb_ok,
        "tasa_qb_accepted": _pct(qb_accepted, qb_offered),
        "tasa_qb_ok_sobre_accepted": _pct(qb_ok, qb_accepted),
        "phones_template_a_cita": template_to_cita,
        "tasa_template_a_cita": _pct(template_to_cita, templates_phones),
    }


def m7_dental(conn, ini: str, fin: str) -> dict:
    """
    M7 — Dental: endodoncia/ortodoncia → evaluación general agendada.

    Fuente:
      - intent_detectado con esp endodoncia/ortodoncia
      - ortodoncia_redirigida_odonto: ortodoncia redirigida a odontología general
      - citas_bot con especialidad dental (Odontología, Ortodoncia, Endodoncia, Implanto)
      - Mensajes con tarjeta en contexto dental (confirmar que se informa correctamente)

    Fix esperado: endodoncia/ortodoncia → se agenda evaluación en odontología general.
    Métrica pago dental con tarjeta: mensajes out con "débito" o "crédito" en contexto dental.
    """
    # Intents de endodoncia
    endo_intents = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM conversation_events
        WHERE event = 'intent_detectado'
          AND ts >= ? AND ts < ?
          AND json_extract(meta, '$.esp') IN ('endodoncia','Endodoncia')
    """, (ini, fin))

    # Intents de ortodoncia
    orto_intents = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM conversation_events
        WHERE event = 'intent_detectado'
          AND ts >= ? AND ts < ?
          AND json_extract(meta, '$.esp') IN ('ortodoncia','Ortodoncia')
    """, (ini, fin))

    # ortodoncia_redirigida_odonto (el fix de dental)
    orto_redirigida = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'ortodoncia_redirigida_odonto' AND ts >= ? AND ts < ?
    """, (ini, fin))

    # Citas creadas en especialidades dentales
    citas_dental = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM citas_bot
        WHERE created_at >= ? AND created_at < ?
          AND (especialidad LIKE 'Odont%' OR especialidad LIKE 'odont%'
               OR especialidad LIKE 'Orto%' OR especialidad LIKE 'orto%'
               OR especialidad LIKE 'Endo%' OR especialidad LIKE 'endo%'
               OR especialidad LIKE 'Implant%' OR especialidad LIKE 'implant%')
    """, (ini, fin))

    # Mensajes out informando tarjetas en contexto dental
    info_tarjetas_dental = _scalar(conn, """
        SELECT COUNT(*) FROM messages
        WHERE direction = 'out'
          AND ts >= ? AND ts < ?
          AND (text LIKE '%débito%' OR text LIKE '%debito%' OR text LIKE '%crédito%' OR text LIKE '%credito%')
          AND (text LIKE '%dental%' OR text LIKE '%odonto%' OR text LIKE '%orto%')
    """, (ini, fin))

    return {
        "endo_intents_phones": endo_intents,
        "orto_intents_phones": orto_intents,
        "orto_redirigida_odonto": orto_redirigida,
        "citas_dental_creadas_phones": citas_dental,
        "msgs_info_tarjetas_dental": info_tarjetas_dental,
        # Tasa de redirección ortodoncia
        "tasa_orto_redirigida": _pct(orto_redirigida, orto_intents),
    }


def m8_degradado_orl(conn, ini: str, fin: str) -> dict:
    """
    M8 — Modo degradado / ORL: sin_disponibilidad/modo_degradado → waitlist ofrecida.

    Fix esperado: cuando no hay disponibilidad, el bot ofrece waitlist en lugar
    de cerrar el flujo. Medir cobertura: modo_degradado_waitlist_ofrecida / modo_degradado.

    Fuente:
      - sin_disponibilidad: no hay horas disponibles para la especialidad
      - modo_degradado: bot en modo degradado (Medilink offline o sin slots)
      - modo_degradado_waitlist_ofrecida: se ofreció waitlist correctamente
      - waitlist_inscrito / waitlist_inscrito_auto: paciente se inscribió
      - demanda_no_disponible: registro en tabla aparte de especialidades sin oferta
    """
    sin_disp = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'sin_disponibilidad' AND ts >= ? AND ts < ?
    """, (ini, fin))

    sin_disp_phones = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM conversation_events
        WHERE event = 'sin_disponibilidad' AND ts >= ? AND ts < ?
    """, (ini, fin))

    modo_deg = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'modo_degradado' AND ts >= ? AND ts < ?
    """, (ini, fin))

    waitlist_ofrecida = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event = 'modo_degradado_waitlist_ofrecida' AND ts >= ? AND ts < ?
    """, (ini, fin))

    waitlist_inscrito = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events
        WHERE event IN ('waitlist_inscrito', 'waitlist_inscrito_auto') AND ts >= ? AND ts < ?
    """, (ini, fin))

    waitlist_inscrito_phones = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM conversation_events
        WHERE event IN ('waitlist_inscrito', 'waitlist_inscrito_auto') AND ts >= ? AND ts < ?
    """, (ini, fin))

    # demanda_no_disponible en tabla propia
    demanda_nd = _scalar(conn, """
        SELECT COUNT(*) FROM demanda_no_disponible
        WHERE created_at >= ? AND created_at < ?
    """, (ini, fin))

    # Especialidades más demandadas sin disponibilidad
    esp_sin_disp = _q(conn, """
        SELECT json_extract(meta, '$.especialidad') as esp, COUNT(*) as n
        FROM conversation_events
        WHERE event = 'sin_disponibilidad' AND ts >= ? AND ts < ?
        GROUP BY esp ORDER BY n DESC LIMIT 6
    """, (ini, fin))
    esp_str = "; ".join(f"{r[0] or 'null'}:{r[1]}" for r in esp_sin_disp) or "sin registros"

    return {
        "sin_disponibilidad": sin_disp,
        "sin_disp_phones": sin_disp_phones,
        "modo_degradado": modo_deg,
        "waitlist_ofrecida": waitlist_ofrecida,
        "waitlist_inscrito": waitlist_inscrito,
        "waitlist_inscrito_phones": waitlist_inscrito_phones,
        "demanda_no_disponible_registros": demanda_nd,
        "especialidades_sin_disp": esp_str,
        # Cobertura: cuando hubo modo_degradado, ¿se ofreció waitlist?
        "tasa_waitlist_sobre_degradado": _pct(waitlist_ofrecida, modo_deg),
        # Conversión: de waitlist ofrecida, cuántos se inscribieron
        # (inscripciones / sin_disp_phones como proxy)
        "tasa_inscripcion_waitlist": _pct(waitlist_inscrito_phones, sin_disp_phones),
    }


# ─── Resumen general ──────────────────────────────────────────────────────────

def m0_general(conn, ini: str, fin: str) -> dict:
    """Métricas de volumen global para contextualizar el periodo."""
    total_msgs_in = _scalar(conn, """
        SELECT COUNT(*) FROM messages WHERE direction = 'in' AND ts >= ? AND ts < ?
    """, (ini, fin))
    total_msgs_out = _scalar(conn, """
        SELECT COUNT(*) FROM messages WHERE direction = 'out' AND ts >= ? AND ts < ?
    """, (ini, fin))
    phones_activos = _scalar(conn, """
        SELECT COUNT(DISTINCT phone) FROM messages WHERE ts >= ? AND ts < ?
    """, (ini, fin))
    total_citas = _scalar(conn, """
        SELECT COUNT(*) FROM citas_bot WHERE created_at >= ? AND created_at < ?
    """, (ini, fin))
    total_events = _scalar(conn, """
        SELECT COUNT(*) FROM conversation_events WHERE ts >= ? AND ts < ?
    """, (ini, fin))
    return {
        "msgs_in": total_msgs_in,
        "msgs_out": total_msgs_out,
        "phones_activos": phones_activos,
        "citas_creadas": total_citas,
        "eventos_totales": total_events,
    }


# ─── Formateo de tabla comparativa ───────────────────────────────────────────

def _print_section(title: str, pre: dict, post: dict, dias_pre: int, dias_post: int):
    """Imprime una sección de comparación PRE vs POST."""
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")
    col_w = 40
    print(f"  {'METRICA':<{col_w}}  {'PRE':>12}  {'POST':>12}  {'DELTA':>10}")
    print(f"  {'-'*col_w}  {'-'*12}  {'-'*12}  {'-'*10}")

    all_keys = list(pre.keys())
    for k in all_keys:
        v_pre = pre.get(k, 0)
        v_post = post.get(k, 0)

        # Detectar si es porcentaje (string con %)
        is_pct = isinstance(v_pre, str) and "%" in str(v_pre)

        # Para strings descriptivos (razones, especialidades) no hacer delta.
        # Se detectan por nombre de clave (sufijos conocidos) O porque ambos valores
        # son strings sin "%" (incluyendo "sin registros" que no tiene ":").
        _TEXT_SUFFIXES = ("_top", "_por_estado", "_tipos", "_sin_disp", "_str", "_breakdown")
        _key_is_text = any(k.endswith(s) for s in _TEXT_SUFFIXES)
        _val_is_text = (isinstance(v_pre, str) and "%" not in v_pre) or (isinstance(v_post, str) and "%" not in str(v_post) and not isinstance(v_pre, (int, float)))
        is_text = _key_is_text or _val_is_text

        if is_text:
            print(f"  {k:<{col_w}}  PRE: {str(v_pre)[:50]}")
            print(f"  {' '*col_w}  POST: {str(v_post)[:50]}")
            continue

        # Normalizar por días para métricas de conteo (para comparar ventanas distintas)
        if isinstance(v_pre, (int, float)) and dias_pre > 0 and dias_post > 0:
            v_pre_norm = v_pre / dias_pre
            v_post_norm = v_post / dias_post
            delta = _delta(v_pre_norm, v_post_norm)
            print(
                f"  {k:<{col_w}}  {_fmt(v_pre):>12}  {_fmt(v_post):>12}  {delta:>10}"
                f"  ({v_pre_norm:.1f}/d → {v_post_norm:.1f}/d)"
            )
        else:
            delta = _delta(v_pre, v_post, is_pct_str=is_pct)
            print(f"  {k:<{col_w}}  {str(v_pre):>12}  {str(v_post):>12}  {delta:>10}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Compara métricas PRE vs POST deploy de fixes 2026-05-29."
    )
    parser.add_argument("--pre-inicio",  default="2026-05-15", help="Inicio ventana PRE (YYYY-MM-DD)")
    parser.add_argument("--pre-fin",     default="2026-05-29", help="Fin ventana PRE (YYYY-MM-DD, exclusivo)")
    parser.add_argument("--post-inicio", default="2026-05-29", help="Inicio ventana POST (YYYY-MM-DD)")
    parser.add_argument("--post-fin",    default=str(date.today()), help="Fin ventana POST (YYYY-MM-DD, exclusivo)")
    parser.add_argument("--db",          default="/opt/chatbot-cmc/data/sessions.db")
    parser.add_argument("--key",         default=os.environ.get("SQLCIPHER_KEY", ""))
    args = parser.parse_args()

    pre_ini  = args.pre_inicio
    pre_fin  = args.pre_fin
    post_ini = args.post_inicio
    post_fin = args.post_fin
    db_path  = args.db
    key      = args.key

    if not key:
        print("ERROR: SQLCIPHER_KEY no definido. Setea la variable de entorno o usa --key.", file=sys.stderr)
        sys.exit(1)

    # Días en cada ventana (para normalizar métricas de conteo)
    dias_pre  = (date.fromisoformat(pre_fin)  - date.fromisoformat(pre_ini)).days
    dias_post = (date.fromisoformat(post_fin) - date.fromisoformat(post_ini)).days

    print(f"\n{'#'*80}")
    print(f"  REPORTE DE CONVERSION POST-DEPLOY — CMC Bot")
    print(f"  Generado: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'#'*80}")
    print(f"\n  PRE-deploy:  {pre_ini} → {pre_fin}  ({dias_pre} dias)")
    print(f"  POST-deploy: {post_ini} → {post_fin}  ({dias_post} dias)")
    print(f"  DB: {db_path}")

    # Conectar
    conn, backend = _get_connection(db_path, key)
    print(f"  Backend: {backend}\n")

    # Calcular todas las métricas
    sections = [
        ("M0 — VOLUMEN GENERAL",           m0_general),
        ("M1 — ECOGRAFIA: intent → cita",  m1_ecografia),
        ("M2 — CTWA / META REFERRAL",       m2_ctwa),
        ("M3 — PRECIO: bloque de pago",     m3_precio),
        ("M4 — ABANDONO EN AGENDAMIENTO",   m4_abandono),
        ("M5 — HUMAN TAKEOVER / DERIVADO",  m5_takeover),
        ("M6 — TEMPLATES PROACTIVOS",       m6_proactive),
        ("M7 — DENTAL: orto/endo → eval",   m7_dental),
        ("M8 — MODO DEGRADADO / WAITLIST",  m8_degradado_orl),
    ]

    for title, fn in sections:
        pre_data  = fn(conn, pre_ini,  pre_fin)
        post_data = fn(conn, post_ini, post_fin)
        _print_section(title, pre_data, post_data, dias_pre, dias_post)

    conn.close()

    print(f"\n{'='*80}")
    print("  INTERPRETACION DEL DELTA")
    print(f"{'='*80}")
    print("""
  Los deltas se calculan sobre la tasa diaria (valor / dias_ventana) para
  neutralizar diferencias de largo entre ventanas PRE y POST.

  Metricas clave a vigilar en POST:
    M1 ecografia_sin_tipo         → debe BAJAR (loop corregido)
    M1 tasa_conv ecografia        → debe SUBIR (menos loop, mas citas)
    M2 tasa_slot_elegido          → debe SUBIR (fix CTWA bajo friction)
    M3 tasa_bloque_sobre_precio   → debe BAJAR (responder precio, no métodos de pago)
    M4 tasa_abandono              → debe BAJAR
    M5 derivado_humano_total      → debe BAJAR (fix retiro informe, pediatría, ORL)
    M6 quick_book_agendar_ok      → debe SUBIR
    M7 orto_redirigida_odonto     → debe SUBIR (fix dental activo)
    M8 tasa_waitlist_sobre_degradado → debe SUBIR (fix modo degradado)

  NOTA: ventana POST con < 3 dias tiene baja potencia estadistica.
  Re-ejecutar el script en ~1 semana (2026-06-05) para resultados confiables.
    """)


if __name__ == "__main__":
    main()
