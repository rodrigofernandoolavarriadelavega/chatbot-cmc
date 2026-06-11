"""Alma operativa (Fase 4) — relleno de cupos liberados por evento.

Flujo:
    Paciente cancela una hora
        → fill_freed_slot(slot): busca candidatos compatibles en la lista de espera
        → envía invitación automática (broadcast a top-N — "primero que acepta gana")
        → accept_offer(phone): claim atómico → el primero reserva provisional (hold blando)
        → should_auto_confirm(): el sistema confirma solo (bajo riesgo) o cae a recepción

Principios heredados de alma_brain (la IA propone, las reglas mandan):
  - Todo es ADITIVO y degrada con gracia: un fallo nunca tumba el bot.
  - Gateado por kill-switches APAGADOS por defecto:
      ALMA_OPERATIVA_ENABLED      → habilita el fan-out de invitaciones por evento.
      ALMA_OPERATIVA_AUTOCONFIRM  → habilita que el sistema escriba en Medilink sin humano.
    Con ambos en false: las cancelaciones NO disparan nada y todo cupo aceptado va a recepción.
  - Capacidad angosta: NO usa el flag general ALMA_BRAIN_ALLOW_MEDILINK_WRITES.
"""
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from . import policy

log = logging.getLogger("bot")

_CL = ZoneInfo("America/Santiago")

# Cuántos candidatos invitar por cupo. Broadcast: el primero que acepta gana; el
# resto recibe "ya fue tomada". Más alto = llena más rápido pero más decepción.
TOP_N_INVITADOS = int(os.getenv("ALMA_OPERATIVA_TOP_N", "3"))
# TTL del hold blando una vez que un paciente aparta el cupo.
HOLD_TTL_MIN = int(os.getenv("ALMA_OPERATIVA_HOLD_TTL_MIN", "30"))


def _operativa_enabled() -> bool:
    env_val = os.getenv("ALMA_OPERATIVA_ENABLED", "false").lower() in ("true", "1", "yes")
    try:
        from alma_switchboard import effective
        return effective("ALMA_OPERATIVA_ENABLED", env_val)
    except Exception:
        return env_val


def _esp_canonical(s: str) -> str:
    """Normaliza especialidad a raíz comparable. Reusa el normalizador del cron de
    waitlist para que el match por evento y el diario coincidan exactamente."""
    try:
        from jobs import _waitlist_esp_canonical
        return _waitlist_esp_canonical(s or "")
    except Exception:
        return (s or "").strip().lower()


# ── PASO 1+2: cancelación → match de candidatos compatibles ──────────────────

def _match_candidatos(especialidad: str, id_prof, *, excluir_phone: str = "") -> list[dict]:
    """Candidatos de la lista de espera compatibles con un cupo liberado, FIFO.

    Compatible = misma especialidad (canónica) Y (sin profesional preferido O el
    preferido es justo el del cupo). Excluye a quien acaba de cancelar."""
    from session import get_waitlist_pending
    esp_canon = _esp_canonical(especialidad)
    out = []
    for row in get_waitlist_pending():  # ya viene FIFO (created_at ASC)
        if _esp_canonical(row.get("especialidad", "")) != esp_canon:
            continue
        pref = row.get("id_prof_pref")
        if pref and id_prof and str(pref) != str(id_prof):
            continue  # quiere otro profesional específico
        if excluir_phone and row.get("phone") == excluir_phone:
            continue
        out.append(row)
    return out


async def fill_freed_slot(slot: dict, *, send_fn=None, top_n: int | None = None) -> dict:
    """Dispara el relleno de un cupo liberado. `slot` requiere: especialidad, id_prof,
    fecha, hora; opcional phone_cancelador. Devuelve un resumen de la corrida.

    NO escribe en Medilink: solo crea ofertas y envía invitaciones. La reserva real
    ocurre recién en accept_offer() y solo si la política lo permite."""
    from session import make_slot_key, create_offer, slot_has_winner, log_event

    esp = slot.get("especialidad", "")
    id_prof = slot.get("id_prof")
    fecha = slot.get("fecha", "")
    hora = (slot.get("hora", "") or "")[:5]
    slot_key = make_slot_key(id_prof, fecha, hora)

    # Gate de evento: sin el flag, registramos la oportunidad pero no contactamos a nadie.
    if not _operativa_enabled():
        log_event(slot.get("phone_cancelador", ""), "operativa_fill_gated",
                  {"slot_key": slot_key, "especialidad": esp, "motivo": "ALMA_OPERATIVA_ENABLED=false"})
        log.info("operativa: cupo liberado %s — fan-out GATEADO (flag off)", slot_key)
        return {"ok": True, "enabled": False, "slot_key": slot_key, "invitados": 0}

    if slot_has_winner(slot_key):
        return {"ok": True, "slot_key": slot_key, "invitados": 0, "motivo": "ya tiene ganador"}

    candidatos = _match_candidatos(esp, id_prof, excluir_phone=slot.get("phone_cancelador", ""))
    n = top_n if top_n is not None else TOP_N_INVITADOS
    elegidos = candidatos[:max(0, n)]
    if not elegidos:
        log_event("", "operativa_sin_candidatos", {"slot_key": slot_key, "especialidad": esp})
        return {"ok": True, "slot_key": slot_key, "invitados": 0, "motivo": "sin candidatos"}

    # OJO: NO pre-resolvemos send_fn acá. _enviar_invitacion usa send_fn=None como
    # señal de "canal real" → ahí decide template Meta vs texto libre. En tests
    # llega un mock y fuerza el texto libre.
    invitados = 0
    for c in elegidos:
        try:
            create_offer(slot_key, c["phone"], esp, id_prof, fecha, hora,
                         waitlist_id=c.get("id"), rut=c.get("rut", "") or "",
                         nombre=c.get("nombre", "") or "")
            await _enviar_invitacion(c, esp, id_prof, fecha, hora, send_fn)
            invitados += 1
        except Exception as e:  # noqa: BLE001 — un candidato que falla no frena al resto
            log.warning("operativa: fallo invitando a %s para %s: %s", c.get("phone"), slot_key, e)

    log_event(slot.get("phone_cancelador", ""), "operativa_fan_out",
              {"slot_key": slot_key, "especialidad": esp, "invitados": invitados,
               "candidatos": len(candidatos)})
    log.info("operativa: cupo %s — %d invitados (de %d candidatos)",
             slot_key, invitados, len(candidatos))
    return {"ok": True, "slot_key": slot_key, "invitados": invitados,
            "candidatos": len(candidatos)}


async def _enviar_invitacion(cand: dict, esp: str, id_prof, fecha: str, hora: str, send_fn):
    """Invita a un candidato al cupo liberado.

    Con USE_TEMPLATES=true usa el template Meta `oferta_cupo` (botón "Tomar la
    hora"), que ENTREGA fuera de la ventana de 24h de WhatsApp — el caso real,
    porque la mayoría de los pacientes en espera no escribieron hace poco. Sin
    el flag (o en tests), cae a texto libre con instrucción de responder *TOMAR*.
    El botón y el texto resuelven igual: el webhook entrega 'Tomar la hora' y el
    handler de aceptación lo captura (maybe_accept_offer)."""
    from session import log_message
    from medilink import PROFESIONALES
    phone = cand["phone"]
    nombre = ((cand.get("nombre") or "").split() or [""])[0]
    prof_nombre = PROFESIONALES.get(int(id_prof), {}).get("nombre", "") if id_prof else ""

    use_templates = os.getenv("USE_TEMPLATES", "false").lower() in ("true", "1", "yes")
    # En tests inyectamos un send_fn mock → forzamos el camino de texto libre.
    if use_templates and send_fn is None:
        try:
            from messaging import send_whatsapp_template, render_template_body
            params = [nombre or "paciente", esp.title(),
                      prof_nombre or "nuestro equipo", fecha, hora]
            await send_whatsapp_template(phone, "oferta_cupo", body_params=params)
            try:
                log_message(phone, "out", render_template_body("oferta_cupo", params), "IDLE")
            except Exception:
                pass
            return
        except Exception as e:  # noqa: BLE001 — si el template falla, caemos a texto libre
            log.warning("operativa: template oferta_cupo falló para %s (%s) → texto libre", phone, e)

    if send_fn is None:
        from messaging import send_whatsapp as send_fn  # type: ignore
    saludo = f"Hola {nombre} 👋" if nombre else "Hola 👋"
    prof_txt = f" con *{prof_nombre}*" if prof_nombre else ""
    msg = (
        f"{saludo}\n\n"
        f"¡Se liberó una hora de *{esp.title()}*{prof_txt}!\n\n"
        f"📅 *{fecha}* a las *{hora}*\n\n"
        "Es por orden de llegada. Si la quieres, responde *TOMAR* y te la aparto al tiro. "
        "Si no respondes o ya la tomó otra persona, sigue tu lugar en la lista de espera.\n\n"
        "_Te escribimos porque estás en nuestra lista de espera._"
    )
    await send_fn(phone, msg)
    try:
        log_message(phone, "out", msg, "IDLE")
    except Exception:
        pass


# ── PASO 3+4: aceptación → claim atómico → confirmación según política ───────

def _es_afirmacion_tomar(texto: str) -> bool:
    """¿El mensaje del paciente acepta el cupo? Permisivo con la palabra clave."""
    t = (texto or "").strip().lower()
    if "tomar" in t or "tomo" in t or "la quiero" in t or "lo quiero" in t:
        return True
    return t in ("si", "sí", "sí.", "si.", "acepto", "dale", "ya", "ok", "okay", "quiero")


async def maybe_accept_offer(phone: str, texto: str, *, send_fn=None) -> bool:
    """Punto de entrada desde flows.py (estado IDLE). Si este teléfono tiene una
    oferta abierta y el mensaje la acepta, la resuelve. Devuelve True si manejó el
    mensaje (flows debe cortar ahí), False si no había nada que hacer."""
    from session import get_open_offer_for_phone
    offer = get_open_offer_for_phone(phone)
    if not offer:
        return False
    if not _es_afirmacion_tomar(texto):
        return False  # tiene oferta pero no la está aceptando — flujo normal
    await accept_offer(offer, send_fn=send_fn)
    return True


async def accept_offer(offer: dict, *, send_fn=None) -> dict:
    """Resuelve la aceptación de una oferta: claim atómico (primero que acepta gana)
    → reserva provisional (hold blando) → política de confirmación."""
    from session import claim_offer, log_event, log_message
    phone = offer["phone"]
    if send_fn is None:
        from messaging import send_whatsapp as send_fn  # type: ignore

    claim = claim_offer(offer["id"], ttl_minutes=HOLD_TTL_MIN)
    if not claim["ok"]:
        if claim["reason"] == "slot_taken":
            msg = ("Uy, esa hora la tomó otro paciente recién 😔\n\n"
                   "Sigues en tu lugar en la lista de espera y te avisamos apenas se libere otra.")
            await send_fn(phone, msg)
            try: log_message(phone, "out", msg, "IDLE")
            except Exception: pass
            log_event(phone, "operativa_perdida", {"offer_id": offer["id"], "slot_key": offer["slot_key"]})
        return claim

    o = claim["offer"]
    log_event(phone, "operativa_apartada", {"offer_id": o["id"], "slot_key": o["slot_key"]})

    ctx = await _build_offer_context(o)
    auto, motivo = policy.should_auto_confirm(ctx)

    if auto:
        res = await _confirmar_en_medilink(o)
        if res.get("ok"):
            from session import set_offer_estado, mark_waitlist_notified
            set_offer_estado(o["id"], "confirmada", id_cita=str(res.get("id_cita", "")))
            if o.get("waitlist_id"):
                mark_waitlist_notified(int(o["waitlist_id"]))
            msg = (f"¡Listo! Te confirmamos tu hora de *{o['especialidad'].title()}* "
                   f"para el *{o['fecha']}* a las *{o['hora']}* ✅\n\n"
                   "Te esperamos. Si no puedes asistir, avísanos para liberar el cupo.")
            await send_fn(phone, msg)
            try: log_message(phone, "out", msg, "IDLE")
            except Exception: pass
            log_event(phone, "operativa_autoconfirmada",
                      {"offer_id": o["id"], "motivo": motivo, "id_cita": res.get("id_cita")})
            return {"ok": True, "estado": "confirmada", "auto": True, "motivo": motivo}
        # La escritura en Medilink falló → no perdemos al paciente: cae a recepción.
        log_event(phone, "operativa_autoconfirm_fallo",
                  {"offer_id": o["id"], "error": res.get("error")})
        motivo = f"auto-confirmación falló ({res.get('error')}) → recepción"

    # No auto-confirmable (o falló) → queda apartado para validación humana.
    from session import set_offer_estado, mark_waitlist_notified as _mwn
    set_offer_estado(o["id"], "recepcion")
    # Marcar la inscripción también acá: si no, el cron waitlist de las 07:00
    # le ofrece OTRO cupo al mismo paciente mientras su apartada espera a
    # recepción (doble-mensaje — advertencia de la auditoría 2026-06-05).
    try:
        if o.get("waitlist_id"):
            _mwn(int(o["waitlist_id"]))
    except Exception as _e_wl:
        log.warning("operativa: no pude marcar waitlist_id=%s: %s", o.get("waitlist_id"), _e_wl)
    msg = (f"¡Te aparté la hora de *{o['especialidad'].title()}* para el *{o['fecha']}* "
           f"a las *{o['hora']}*! 🟡\n\n"
           "Nuestra recepción la confirma y te avisa en breve. "
           "Queda reservada provisionalmente a tu nombre.")
    await send_fn(phone, msg)
    try: log_message(phone, "out", msg, "IDLE")
    except Exception: pass
    log_event(phone, "operativa_a_recepcion", {"offer_id": o["id"], "motivo": motivo})
    return {"ok": True, "estado": "recepcion", "auto": False, "motivo": motivo}


async def _build_offer_context(offer: dict) -> policy.OfferContext:
    """Computa (con I/O) las señales de riesgo que la regla de negocio evalúa pura."""
    from medilink import valid_rut, buscar_paciente
    rut = (offer.get("rut") or "").strip()
    rut_valido = bool(rut) and valid_rut(rut)
    paciente_conocido = False
    if rut_valido:
        try:
            pac = await buscar_paciente(rut)
            paciente_conocido = bool(pac and pac.get("id"))
        except Exception as e:  # noqa: BLE001
            log.warning("operativa: buscar_paciente falló para ctx (%s)", e)

    try:
        fh = datetime.strptime(f"{offer['fecha']} {offer['hora'][:5]}", "%Y-%m-%d %H:%M").replace(tzinfo=_CL)
        horas_hasta = (fh - datetime.now(_CL)).total_seconds() / 3600
    except (ValueError, KeyError):
        horas_hasta = 0.0

    return policy.OfferContext(
        paciente_conocido=paciente_conocido,
        rut_valido=rut_valido,
        esp_coincide=True,  # por construcción: la oferta nació de su inscripción en esa especialidad
        horas_hasta=horas_hasta,
        desde_waitlist=bool(offer.get("waitlist_id")),
        especialidad=offer.get("especialidad", ""),
    )


async def _confirmar_en_medilink(offer: dict) -> dict:
    """Crea la cita REAL en Medilink (la escritura acotada de la auto-confirmación).
    Solo se llama cuando should_auto_confirm devolvió True (flag ya verificado)."""
    from medilink import buscar_paciente, crear_cita, PROFESIONALES
    rut = (offer.get("rut") or "").strip()
    id_prof = offer.get("id_prof")
    try:
        pac = await buscar_paciente(rut)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"buscar_paciente: {e}"}
    if not (pac and pac.get("id")):
        return {"ok": False, "error": "paciente no encontrado en Medilink"}

    intervalo = PROFESIONALES.get(int(id_prof), {}).get("intervalo", 20) if id_prof else 20
    hi = offer["hora"][:5]
    try:
        h, m = map(int, hi.split(":"))
        total = h * 60 + m + int(intervalo)
        hf = f"{total // 60:02d}:{total % 60:02d}"
    except Exception:
        return {"ok": False, "error": f"hora inválida: {hi}"}

    try:
        res = await crear_cita(int(pac["id"]), int(id_prof), offer["fecha"], hi, hf)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"crear_cita: {e}"}
    if res and res.get("id"):
        return {"ok": True, "id_cita": res["id"]}
    return {"ok": False, "error": "crear_cita no devolvió id"}
