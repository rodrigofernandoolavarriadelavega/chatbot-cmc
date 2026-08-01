"""Lectura automática de órdenes médicas de ecografía (foto → tipo → agenda).

Práctica validada 2026-08-01 con 19 imágenes reales de producción (90 días):
15/15 órdenes de eco leídas correctamente (manuscritas de matrona, formularios
de Hospital Arauco/Curanilahue, CESFAM, IntegraMédica y órdenes del propio CMC).
Las otras 2 imágenes NO eran órdenes (comprobante de transferencia, orden de
Holter) — por eso el paso 1 es CLASIFICAR, no asumir.

Flujo (gated por ECO_ORDEN_OCR_ACTIVE, ver main.py media handler):
  1. Paciente manda foto con `wait_eco_tipo` activo (el bot preguntó el tipo).
  2. `leer_orden_medica()` — Claude Haiku visión: clasifica el documento y
     transcribe LITERALMENTE los exámenes solicitados. Nunca interpreta clínica.
  3. `decidir_accion()` — pura, testeable: cruza lo leído con
     `route_ecografia()` (la MISMA autoridad de ruteo del texto) y decide:
       - ofrecer_agenda  → botones "Sí, agendar" (mecanismo especialidad_sugerida
                           + eco_tipo_text ya existente; el paciente CONFIRMA,
                           nunca se agenda solo — cubre el caso real "el doctor
                           se equivocó de lado")
       - obstetrica      → CMC no la realiza: responder claro al tiro
       - recepcion       → todo lo demás (multi-examen, ilegible, no-orden,
                           ecocardiograma waitlist): flujo actual sin cambios.

La lectura NO se guarda como diagnóstico; solo el texto del examen para rutear.
"""
from __future__ import annotations

import base64
import json
import logging

log = logging.getLogger("eco_orden_ocr")

# Sonnet, no Haiku: en la validación 2026-08-01 Haiku leyó mal 3/3 formularios
# manuscritos de hospital (letra de médico); Sonnet los leyó 3/3 correcto.
# Volumen ~10-30 imágenes/mes → el costo extra es de centavos.
_VISION_MODEL = "claude-sonnet-5"
_VISION_TIMEOUT = 25.0  # s — el webhook no puede colgarse; peor caso cae a recepción

_PROMPT = """Eres un transcriptor de documentos médicos chilenos. Te paso la foto \
de un documento que un paciente envió por WhatsApp. NO interpretes clínicamente: \
solo clasifica y transcribe.

Responde SOLO un JSON válido, sin texto adicional:
{
  "tipo_documento": "orden_medica" | "comprobante_pago" | "receta_medicamentos" | "otro",
  "examenes_solicitados": ["transcripción literal de cada examen solicitado"],
  "confianza": "alta" | "media" | "baja",
  "paciente": null,
  "comprobante": null
}

Si tipo_documento es "orden_medica" y el documento muestra los datos del
paciente, "paciente" deja de ser null y lleva (cada campo "" si no aparece):
{
  "nombre": "Katherine Campos",     // nombre del PACIENTE tal como aparece
  "rut": "22.742.084-7",            // RUT/RUN del PACIENTE
  "fecha_nacimiento": "06/06/2008", // tal como aparece; NO la calcules desde la edad
  "sexo": "F",                      // "M" | "F" | "" — solo si el documento lo dice
  "direccion": "Pichilo s/n, Arauco" // dirección/comuna si aparece
}
No confundas al paciente con el médico que firma la orden. Si dudas de a
quién corresponde el nombre, deja "paciente" en null.

Si tipo_documento es "comprobante_pago" (transferencia bancaria, pago en app),
"comprobante" deja de ser null y lleva:
{
  "monto": 7880,                      // entero en pesos, sin puntos ni signos
  "fecha": "17/07/2026",              // tal como aparece
  "hora": "16:16",                    // "" si no aparece
  "banco": "Banco Estado",            // banco o app de ORIGEN; "" si no aparece
  "num_operacion": "8005079",         // "" si no aparece
  "nombre_pagador": "",               // titular de la cuenta de origen si aparece
  "destinatario_nombre": "",          // a quién se transfirió
  "destinatario_cuenta": "221708538", // solo dígitos; "" si no aparece
  "destinatario_rut": ""              // RUT del destinatario si aparece
}

Reglas:
- "orden_medica" = solicitud de examen/imagenología (impresa o manuscrita, \
formulario de hospital/CESFAM/consulta particular, checklist con casilla marcada).
- En checklists, transcribe SOLO los exámenes marcados (X, ✓, círculo).
- UN examen solicitado = UN ítem de la lista. No separes en ítems distintos la \
descripción o las estructuras que evalúa un mismo examen (ej: "ecografía renal \
y de vías urinarias, con evaluación de riñones, vejiga y próstata" es UN solo \
examen). El diagnóstico NO es un examen: no lo incluyas en la lista.
- Transcribe el examen tal como está escrito (ej: "Ecotomografía mamaria \
bilateral", "Eco partes blandas región inguinal izquierda"). Corrige solo la \
ortografía obvia del transcriptor, no cambies términos médicos.
- Si la letra es ilegible o dudas de qué examen es: "confianza": "baja". \
Nunca adivines: prefiere confianza baja antes que inventar un examen.
- Si hay varios exámenes solicitados, inclúyelos todos en la lista."""


async def leer_orden_medica(image_bytes: bytes, mime: str) -> dict | None:
    """Clasifica + transcribe la imagen con Claude Haiku visión.

    Retorna el dict parseado o None si falla (API caída, JSON inválido,
    mime no soportado). El caller trata None como "cae a recepción".
    """
    if mime not in ("image/jpeg", "image/png", "image/webp"):
        return None
    try:
        from claude_helper import client  # AsyncAnthropic ya configurado
        resp = await client.messages.create(
            model=_VISION_MODEL,
            # Sonnet 5 piensa por defecto: para extracción pura lo apagamos —
            # sin esto, el thinking consume el max_tokens y/o antepone un
            # bloque de razonamiento (bug real 2026-08-01 18:21: el parser
            # leía el primer bloque como texto y explotaba con None).
            # Vía extra_body porque el SDK instalado no tipa `thinking`.
            extra_body={"thinking": {"type": "disabled"}},
            max_tokens=1200,
            timeout=_VISION_TIMEOUT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": mime,
                        "data": base64.b64encode(image_bytes).decode(),
                    }},
                    {"type": "text", "text": _PROMPT},
                ],
            }],
        )
        # Buscar el PRIMER bloque de texto real — nunca asumir que content[0]
        # es texto (puede venir un bloque de thinking primero).
        raw = ""
        for _bloque in resp.content or []:
            if getattr(_bloque, "type", "") == "text" and getattr(_bloque, "text", None):
                raw = _bloque.text.strip()
                break
        if not raw:
            log.warning("leer_orden_medica sin bloque de texto (stop=%s)",
                        getattr(resp, "stop_reason", "?"))
            return None
        # Tolerar fences ```json ... ```
        if raw.startswith("```"):
            raw = raw.strip("`").removeprefix("json").strip()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        data.setdefault("tipo_documento", "otro")
        data.setdefault("examenes_solicitados", [])
        data.setdefault("confianza", "baja")
        data.setdefault("paciente", None)
        data.setdefault("comprobante", None)
        return data
    except Exception as e:  # noqa: BLE001 — cualquier fallo cae a recepción
        log.warning("leer_orden_medica fallo: %s", str(e)[:200])
        return None


def _parece_eco(texto: str) -> bool:
    """True si el nombre del examen menciona una raíz ecográfica.

    Regex del gate de ecografias.py + fallback fuzzy para transcripciones
    imperfectas de la visión (caso real: "Ecozoografía cervical" — es una eco
    cervical, pero la raíz no calza con el regex exacto)."""
    from difflib import SequenceMatcher
    from ecografias import _tiene_contexto_eco, _norm
    t = _norm(texto)
    if _tiene_contexto_eco(t):
        return True
    raices = ("ecografia", "ecotomografia", "ultrasonido")
    for tok in t.replace("/", " ").split():
        if len(tok) < 7:
            continue
        for raiz in raices:
            if SequenceMatcher(None, tok, raiz).ratio() >= 0.8:
                return True
    return False


def rut_normalizado(rut: str) -> str | None:
    """Valida módulo 11 y normaliza a '12345678-9'. None si inválido/ilegible.

    Se usa sobre el RUT leído de la orden ANTES de arrastrarlo al flujo de
    agendamiento: un RUT mal leído por visión no debe entrar jamás — y aun
    entrando válido, la confirmación final del flujo muestra el nombre de
    Medilink y el paciente confirma antes de reservar (2ª puerta)."""
    import re as _re
    limpio = _re.sub(r"[^0-9kK]", "", rut or "")
    if not 8 <= len(limpio) <= 9:
        return None
    cuerpo, dv = limpio[:-1], limpio[-1].upper()
    if not cuerpo.isdigit():
        return None
    suma, factor = 0, 2
    for c in reversed(cuerpo):
        suma += int(c) * factor
        factor = 2 if factor == 7 else factor + 1
    resto = 11 - (suma % 11)
    dv_ok = "0" if resto == 11 else "K" if resto == 10 else str(resto)
    if dv != dv_ok:
        return None
    return f"{cuerpo}-{dv}"


def decidir_accion(extraccion: dict | None) -> dict:
    """Decide qué hacer con la extracción. Pura (sin I/O) para testearla.

    Retorna:
      {"accion": "ofrecer_agenda", "tipo_texto": str, "routing": dict}
      {"accion": "obstetrica", "tipo_texto": str}
      {"accion": "recepcion", "motivo": str}
    """
    if not extraccion or extraccion.get("tipo_documento") != "orden_medica":
        return {"accion": "recepcion", "motivo": "no_es_orden"}
    if extraccion.get("confianza") == "baja":
        return {"accion": "recepcion", "motivo": "confianza_baja"}

    examenes = [e for e in (extraccion.get("examenes_solicitados") or [])
                if isinstance(e, str) and e.strip()]
    if not examenes:
        return {"accion": "recepcion", "motivo": "sin_examenes"}

    # GUARD anti-falso-positivo (caso real de la validación: Haiku leyó
    # "Escoliosis lumbar (radiografía)" y el keyword suelto "lumbar" habría
    # ofrecido agenda de eco). Solo se rutea un examen que MENCIONE una raíz
    # ecográfica; radiografías/holter/etc. caen a recepción.
    from ecografias import route_ecografia
    eco_examenes = [e for e in examenes if _parece_eco(e)]
    if not eco_examenes:
        return {"accion": "recepcion", "motivo": "sin_eco_ruteable"}
    rutas = [(e, route_ecografia(e, assume_context=True)) for e in eco_examenes]
    eco_rutas = [(e, r) for e, r in rutas if r is not None]

    if not eco_rutas:
        # Orden legible pero ningún examen es una eco que sepamos rutear
        return {"accion": "recepcion", "motivo": "sin_eco_ruteable"}
    if len(eco_rutas) > 1:
        # Varias ecos (caso real: 4 órdenes antebrazo+muñeca) → recepción decide
        return {"accion": "recepcion", "motivo": "multi_examen"}

    tipo_texto, routing = eco_rutas[0]
    if routing.get("flujo") == "no_disponible":
        return {"accion": "obstetrica", "tipo_texto": tipo_texto}
    if routing.get("flujo") != "normal":
        # waitlist (ecocardiograma) u otros flujos especiales → recepción
        return {"accion": "recepcion", "motivo": f"flujo_{routing.get('flujo')}"}
    return {"accion": "ofrecer_agenda", "tipo_texto": tipo_texto, "routing": routing}


def msg_oferta(tipo_texto: str, routing: dict) -> dict:
    """Mensaje interactivo de confirmación (formato send_whatsapp interactive)."""
    prof = "David Pardo" if routing.get("id_profesional") == 68 else \
           "el Dr. Tirso Rejón (Ginecología)" if routing.get("id_profesional") == 61 else \
           "el profesional que corresponde"
    precio = routing.get("precio_particular")
    precio_fmt = f"${precio:,}".replace(",", ".") if precio else ""
    cuerpo = (
        f"Leí en tu orden: *{tipo_texto.strip()}* 📄\n\n"
        f"Esa ecografía la realiza {prof}"
        + (f" · {precio_fmt} particular" if precio_fmt else "") + ".\n\n"
        "¿Te busco una hora?\n\n"
        "_Si leí mal la orden, dime el tipo correcto y seguimos._"
    )
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": cuerpo},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "agendar_sugerido", "title": "✅ Sí, agendar"}},
                {"type": "reply", "reply": {"id": "no_agendar", "title": "No por ahora"}},
            ]},
        },
    }


MSG_OBSTETRICA = (
    "Leí tu orden: es una *ecografía obstétrica (de embarazo)* 📄\n\n"
    "Por ahora *no realizamos ese tipo de ecografía* en el Centro Médico "
    "Carampangue.\n\n"
    "Puedes consultar en la Clínica de Maternidad más cercana o en el "
    "hospital de tu red de salud."
)
