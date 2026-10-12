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
from datetime import datetime, timedelta
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

def _match_candidatos(especialidad: str, id_prof, *, excluir_phone: str = "",
                      excluir_phones: set | None = None) -> list[dict]:
    """Candidatos de la lista de espera compatibles con un cupo liberado, FIFO.

    Compatible = misma especialidad (canónica) Y (sin profesional preferido O el
    preferido es justo el del cupo). Excluye a quien acaba de cancelar y a quien ya
    tiene una oferta de cupo VIVA (excluir_phones) — candado anti-doble-mensaje."""
    from session import get_waitlist_pending
    excluir_phones = excluir_phones or set()
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
        if row.get("phone") in excluir_phones:
            continue  # ya tiene una oferta de cupo viva — no lo invites de nuevo
        out.append(row)
    return out


# Tandas de aviso por hora liberada (pedido dueño 2026-10-11): máximo 5 avisos
# por hora; primero 2, y si a los 10 min nadie la tomó, 3 más. No se ofrece una
# hora que empieza en <60 min. Un paciente recibe como máximo 1 aviso de hora
# liberada cada 24 h.
MAX_AVISOS_POR_HORA = int(os.getenv("ALMA_OPERATIVA_MAX_AVISOS", "5"))
TANDA1_N = 2
TANDA2_N = 3
TANDA2_ESPERA_MIN = 10
MIN_ANTELACION_MIN = 60
_OPT_OUT_TAGS = ("marketing_opt_out", "recuperacion_opt_out")


# Antes de ofrecer una hora se confirma en Medilink que sigue LIBRE (1 llamada a
# /agenda por hora liberada). Cubre "Cambio de fecha" donde el mismo paciente
# quedó en esa hora/profesional y horas ya reocupadas. Fail-closed: si no se
# puede verificar, no se ofrece. Los tests lo apagan.
VERIFICAR_LIBRE = os.getenv("ALMA_OPERATIVA_VERIFICAR_LIBRE", "true").lower() in ("true", "1", "yes")


async def _hora_realmente_libre(id_prof, fecha: str, hora: str) -> bool:
    if not id_prof:
        return False
    try:
        from medilink import get_slots_libres
        slots = await get_slots_libres(int(id_prof), fecha)
        return any((sl.get("hora_inicio") or "")[:5] == hora[:5] for sl in (slots or []))
    except Exception as e:  # noqa: BLE001
        log.warning("operativa: no pude verificar disponibilidad %s/%s %s: %s", id_prof, fecha, hora, e)
        return False


def _telefono_de_cita(id_cita) -> str:
    """Teléfono del paciente de esa cita (citas_bot o recepción), para no
    ofrecerle su propia hora recién anulada."""
    try:
        from session import db
        with db() as c:
            r = c.execute("SELECT phone FROM citas_bot WHERE id_cita=? LIMIT 1", (str(id_cita),)).fetchone()
            if not r:
                r = c.execute("SELECT phone FROM citas_recepcion_reminders WHERE id_cita_medilink=? LIMIT 1",
                              (id_cita,)).fetchone()
            return (r["phone"] or "") if r else ""
    except Exception:
        return ""


async def ofrecer_hora_liberada(cita: dict, *, origen: str, send_fn=None) -> dict:
    """Punto de entrada ÚNICO para toda anulación (bot vía cancelar_cita(),
    detector de citas_bot, detector de recepción). `cita` = dict de Medilink
    (id, fecha, hora_inicio, id_profesional) o con especialidad/hora explícitas.
    Idempotente: una hora liberada se ofrece una sola vez aunque la detecten dos
    caminos (ver fill_freed_slot: slot_key ya ofrecida)."""
    from session import log_event
    try:
        from medilink import PROFESIONALES
        id_prof = cita.get("id_profesional")
        esp = cita.get("especialidad") or (
            PROFESIONALES.get(int(id_prof), {}).get("especialidad", "") if id_prof else "")
        hora = (cita.get("hora_inicio") or cita.get("hora") or "")[:5]
        slot = {"especialidad": esp, "id_prof": id_prof, "fecha": cita.get("fecha", ""),
                "hora": hora,
                "phone_cancelador": cita.get("phone") or _telefono_de_cita(cita.get("id") or cita.get("id_cita"))}
        if not (esp and id_prof and slot["fecha"] and hora):
            return {"ok": False, "motivo": "datos incompletos"}
        log_event(slot["phone_cancelador"], "hora_liberada_detectada",
                  {"origen": origen, "id_cita": str(cita.get("id") or cita.get("id_cita") or ""),
                   "especialidad": esp, "fecha": slot["fecha"], "hora": hora})
        return await fill_freed_slot(slot, send_fn=send_fn)
    except Exception as e:  # noqa: BLE001 — jamás frena la anulación
        log.warning("operativa: ofrecer_hora_liberada falló (%s): %s", origen, e)
        return {"ok": False, "motivo": str(e)}


def _minutos_hasta(fecha: str, hora: str) -> float | None:
    try:
        fh = datetime.strptime(f"{fecha} {(hora or '')[:5]}", "%Y-%m-%d %H:%M").replace(tzinfo=_CL)
        return (fh - datetime.now(_CL)).total_seconds() / 60
    except (ValueError, TypeError):
        return None


def _tiene_baja(phone: str) -> bool:
    """Tags de baja (aviso de horas / recuperación). Fail-closed: si no se
    puede leer, se trata como baja."""
    try:
        from session import get_tags
        tags = set(get_tags(phone) or [])
        return any(t in tags for t in _OPT_OUT_TAGS)
    except Exception:
        return True


def _avisado_hoy(phone: str) -> bool:
    """¿Ya recibió un aviso de hora liberada en las últimas 24 h?"""
    try:
        from session import db
        with db() as c:
            return c.execute(
                "SELECT 1 FROM conversation_events WHERE phone=? "
                "AND event='hora_liberada_ofrecida' "
                "AND ts >= datetime('now','-1 day') LIMIT 1", (phone,)).fetchone() is not None
    except Exception:
        return True  # fail-closed: ante duda no se manda


def _consent_marketing_ok(phone: str) -> bool:
    """Consentimiento de MARKETING registrado (bi.marketing_consent='accepted')
    y sin baja. `privacy` (dio RUT) NO cuenta. Fail-closed."""
    try:
        from consent_marketing import estado_bi
        from session import normalize_wa_id
        st, opt_out = estado_bi(normalize_wa_id(phone))
        return st == "accepted" and not opt_out
    except Exception:
        return False


def _candidatos_grupo_b(esp: str, fecha: str, *, excluir: set) -> list[dict]:
    """Pacientes que en los últimos 3 días pidieron `esp` al bot y NO quedaron
    con cita. Fuente: conversation_events (intent_agendar / sin_disponibilidad /
    funnel_slot_ofrecido) menos quien tiene después un evento cita_creada /
    cita_reagendada o una cita futura vigente en citas_bot / recepción.
    NO consulta Medilink (una cita tomada en recepción que aún no se sincronizó
    puede escaparse). Prioridad: pidió ese mismo día, luego el pedido más reciente."""
    from session import db
    esp_canon = _esp_canonical(esp)
    hoy = datetime.now(_CL).strftime("%Y-%m-%d")
    out: dict[str, dict] = {}
    try:
        with db() as c:
            rows = c.execute(
                "SELECT phone, ts, event, "
                "COALESCE(json_extract(meta,'$.especialidad'), json_extract(meta,'$.esp')) AS esp, "
                "json_extract(meta,'$.fecha') AS fecha "
                "FROM conversation_events "
                "WHERE event IN ('intent_agendar','sin_disponibilidad','funnel_slot_ofrecido') "
                "AND ts >= datetime('now','-3 days') ORDER BY ts DESC").fetchall()
            for r in rows:
                ph = r["phone"]
                if not ph or ph in excluir or ph in out:
                    continue
                if _esp_canonical(r["esp"] or "") != esp_canon:
                    continue
                out[ph] = {"phone": ph, "ts": r["ts"], "mismo_dia": (r["fecha"] == fecha)}
            res = []
            for ph, info in out.items():
                cita_post = c.execute(
                    "SELECT 1 FROM conversation_events WHERE phone=? "
                    "AND event IN ('cita_creada','cita_reagendada') AND ts > ? LIMIT 1",
                    (ph, info["ts"])).fetchone()
                if cita_post:
                    continue
                fut = c.execute(
                    "SELECT especialidad FROM citas_bot WHERE phone=? AND fecha >= ? "
                    "AND cancel_detected_at IS NULL", (ph, hoy)).fetchall()
                fut_rec = c.execute(
                    "SELECT especialidad FROM citas_recepcion_reminders WHERE phone=? AND fecha >= ?",
                    (ph, hoy)).fetchall()
                if any(_esp_canonical(x["especialidad"] or "") == esp_canon for x in list(fut) + list(fut_rec)):
                    continue
                res.append(info)
    except Exception as e:  # noqa: BLE001
        log.warning("operativa: grupo b falló (%s)", e)
        return []
    res.sort(key=lambda i: (0 if i["mismo_dia"] else 1, i["ts"]), reverse=False)
    # dentro de cada prioridad, el pedido más reciente primero
    res.sort(key=lambda i: i["ts"], reverse=True)
    res.sort(key=lambda i: 0 if i["mismo_dia"] else 1)
    return res


async def fill_freed_slot(slot: dict, *, send_fn=None, top_n: int | None = None,
                          tanda: int = 1) -> dict:
    """Dispara el relleno de un cupo liberado. `slot` requiere: especialidad, id_prof,
    fecha, hora; opcional phone_cancelador. Devuelve un resumen de la corrida.

    Destinatarios, en orden: (a) lista de espera de la especialidad (FIFO,
    respetando profesional preferido) y (b) pacientes que pidieron esa
    especialidad al bot en ≤3 días y no quedaron con cita. Máximo
    MAX_AVISOS_POR_HORA por hora liberada, en tandas (2 + 3 a los 10 min); no se
    ofrece si faltan <60 min; 1 aviso por paciente cada 24 h.

    NO escribe en Medilink: solo crea ofertas y envía invitaciones. La reserva real
    ocurre recién en accept_offer() y solo si la política lo permite."""
    from session import make_slot_key, create_offer, slot_has_winner, log_event, db

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

    mins = _minutos_hasta(fecha, hora)
    if mins is not None and mins < MIN_ANTELACION_MIN:
        log_event(slot.get("phone_cancelador", ""), "hora_liberada_sin_antelacion",
                  {"slot_key": slot_key, "minutos": int(mins)})
        return {"ok": True, "slot_key": slot_key, "invitados": 0, "motivo": "faltan <60 min"}

    if slot_has_winner(slot_key):
        return {"ok": True, "slot_key": slot_key, "invitados": 0, "motivo": "ya tiene ganador"}

    with db() as c:
        # Cuentan los avisos efectivamente enviados (las 'cancelada' son envíos
        # fallidos/omitidos o el sello de la tanda 2).
        n_prev = c.execute("SELECT COUNT(*) FROM waitlist_offers WHERE slot_key=? "
                           "AND estado!='cancelada'", (slot_key,)).fetchone()[0]
    if tanda == 1 and n_prev > 0:
        # Ya se avisó esta hora (p. ej. el detector horario llega después del
        # aviso inmediato): no repetir; las tandas siguientes las lleva
        # continuar_tandas().
        return {"ok": True, "slot_key": slot_key, "invitados": 0, "motivo": "ya ofrecida"}
    if VERIFICAR_LIBRE and not await _hora_realmente_libre(id_prof, fecha, hora):
        log_event(slot.get("phone_cancelador", ""), "hora_liberada_no_libre",
                  {"slot_key": slot_key, "tanda": tanda})
        return {"ok": True, "slot_key": slot_key, "invitados": 0, "motivo": "hora no está libre"}
    cupo = MAX_AVISOS_POR_HORA - n_prev
    base = top_n if top_n is not None else (TANDA1_N if tanda == 1 else TANDA2_N)
    n = min(max(0, base), cupo)
    if n <= 0:
        return {"ok": True, "slot_key": slot_key, "invitados": 0, "motivo": "tope de avisos"}

    # Candado anti-doble-mensaje: excluye a quien ya tiene una oferta de cupo viva
    # (de otra cancelación o del cron) — fail-open si la consulta falla.
    try:
        from session import phones_with_open_offers
        _busy = phones_with_open_offers()
    except Exception:
        _busy = set()
    cancelador = slot.get("phone_cancelador", "")
    cand_a = _match_candidatos(esp, id_prof, excluir_phone=cancelador, excluir_phones=_busy)
    cand_a = [dict(c, _grupo="a") for c in cand_a]
    cand_b: list[dict] = []
    if len(cand_a) < n:
        excl_b = set(_busy) | {c["phone"] for c in cand_a} | ({cancelador} if cancelador else set())
        cand_b = [dict(phone=i["phone"], _grupo="b") for i in _candidatos_grupo_b(esp, fecha, excluir=excl_b)]
    candidatos = cand_a + cand_b

    invitados = 0
    ya_vistos: set = set()
    for c in candidatos:
        if invitados >= n:
            break
        ph = c["phone"]
        if ph in ya_vistos:
            continue
        ya_vistos.add(ph)
        if _tiene_baja(ph):
            log_event(ph, "hora_liberada_omitida", {"slot_key": slot_key, "grupo": c["_grupo"], "motivo": "baja"})
            continue
        if _avisado_hoy(ph):
            log_event(ph, "hora_liberada_omitida", {"slot_key": slot_key, "grupo": c["_grupo"], "motivo": "ya_avisado_24h"})
            continue
        if c["_grupo"] == "b":
            try:
                from session import get_profile
                _pf = get_profile(ph) or {}
                c["rut"], c["nombre"] = _pf.get("rut", ""), _pf.get("nombre", "")
            except Exception:
                pass
        try:
            oid = create_offer(slot_key, ph, esp, id_prof, fecha, hora,
                               waitlist_id=c.get("id"), rut=c.get("rut", "") or "",
                               nombre=c.get("nombre", "") or "",
                               grupo=c["_grupo"], tanda=tanda)
            ok, ventana = await _enviar_invitacion(c, esp, id_prof, fecha, hora, send_fn,
                                                    grupo=c["_grupo"])
            if not ok:
                from session import set_offer_estado
                set_offer_estado(oid, "cancelada")  # no bloquea al paciente ni al cupo
                log_event(ph, "hora_liberada_omitida", {"slot_key": slot_key, "grupo": c["_grupo"],
                                                        "motivo": "sin_envio", "ventana": ventana})
                continue
            log_event(ph, "hora_liberada_ofrecida", {
                "slot_key": slot_key, "grupo": c["_grupo"], "ventana": ventana,
                "tanda": tanda, "especialidad": esp, "fecha": fecha, "hora": hora})
            invitados += 1
        except Exception as e:  # noqa: BLE001 — un candidato que falla no frena al resto
            log.warning("operativa: fallo invitando a %s para %s: %s", ph, slot_key, e)

    if not invitados and not candidatos:
        log_event("", "operativa_sin_candidatos", {"slot_key": slot_key, "especialidad": esp})
        return {"ok": True, "slot_key": slot_key, "invitados": 0, "motivo": "sin candidatos"}
    log_event(cancelador, "operativa_fan_out",
              {"slot_key": slot_key, "especialidad": esp, "invitados": invitados,
               "candidatos": len(candidatos), "tanda": tanda})
    log.info("operativa: cupo %s tanda %d — %d invitados (de %d candidatos)",
             slot_key, tanda, invitados, len(candidatos))
    return {"ok": True, "slot_key": slot_key, "invitados": invitados,
            "candidatos": len(candidatos), "tanda": tanda}


async def continuar_tandas(send_fn=None) -> int:
    """Tanda 2: horas liberadas cuya tanda 1 salió hace ≥10 min y nadie tomó.
    La corre un cron cada 5 min. Devuelve cuántas horas procesó."""
    from session import db
    with db() as c:
        rows = c.execute(
            "SELECT slot_key, especialidad, id_prof, fecha, hora, "
            "SUM(estado!='cancelada') AS n, MAX(tanda) AS maxt, MIN(created_at) AS primero "
            "FROM waitlist_offers WHERE fecha >= date('now','-1 day') "
            "GROUP BY slot_key HAVING maxt = 1 AND n >= 1 AND n < ? "
            "AND primero <= datetime('now', ?)",
            (MAX_AVISOS_POR_HORA, f"-{TANDA2_ESPERA_MIN} minutes")).fetchall()
    hechas = 0
    for r in rows:
        try:
            # Sello de la tanda 2 (oferta 'cancelada' que no cuenta como aviso):
            # evita reprocesar la misma hora cada 5 min si no hay a quién avisar.
            with db() as c:
                c.execute(
                    "INSERT INTO waitlist_offers (slot_key, phone, especialidad, id_prof, fecha, hora, "
                    "estado, grupo, tanda) VALUES (?, '__tanda2__', ?, ?, ?, ?, 'cancelada', 'x', 2)",
                    (r["slot_key"], r["especialidad"], r["id_prof"], r["fecha"], r["hora"]))
                c.commit()
            await fill_freed_slot(
                {"especialidad": r["especialidad"], "id_prof": r["id_prof"],
                 "fecha": r["fecha"], "hora": r["hora"]},
                send_fn=send_fn, tanda=2)
            hechas += 1
        except Exception as e:  # noqa: BLE001
            log.warning("operativa: tanda 2 de %s falló: %s", r["slot_key"], e)
    return hechas


def _fecha_humana(fecha: str) -> str:
    try:
        d = datetime.strptime(fecha, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return fecha
    hoy = datetime.now(_CL).date()
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
             "septiembre", "octubre", "noviembre", "diciembre"]
    base = f"{dias[d.weekday()]} {d.day} de {meses[d.month - 1]}"
    if d == hoy:
        return f"hoy {base}"
    if (d - hoy).days == 1:
        return f"mañana {base}"
    return base


async def _enviar_invitacion(cand: dict, esp: str, id_prof, fecha: str, hora: str, send_fn,
                             grupo: str = "a") -> tuple[bool, str]:
    """Invita a un candidato al cupo liberado. Devuelve (enviado, ventana) con
    ventana ∈ {'abierta','cerrada'}.

    Ventana 24 h ABIERTA → mensaje normal con botón "Tomar la hora" (gratis).
    Ventana CERRADA:
      - grupo a (lista de espera) → template UTILITY `oferta_cupo` (hora concreta
        + botón "Tomar la hora"); es la respuesta a que se inscribió.
      - grupo b (pidió hora y no la consiguió) → SOLO con consentimiento de
        MARKETING registrado, y solo si la hora es de mañana, porque el único
        template adecuado (`horas_liberadas_v1`) dice "para mañana". Una hora de
        HOY a un paciente b con ventana cerrada no tiene template aprobado: no se
        envía.
    Sin USE_TEMPLATES (o en tests con send_fn inyectado) el cierre es texto libre
    con la instrucción de responder *TOMAR*."""
    from session import log_message, is_window_open
    from medilink import PROFESIONALES
    phone = cand["phone"]
    nombre = ((cand.get("nombre") or "").split() or [""])[0]
    prof_nombre = PROFESIONALES.get(int(id_prof), {}).get("nombre", "") if id_prof else ""
    cuando = _fecha_humana(fecha)

    use_templates = os.getenv("USE_TEMPLATES", "false").lower() in ("true", "1", "yes")
    real = send_fn is None
    abierta = is_window_open(phone) if real else True
    ventana = "abierta" if abierta else "cerrada"

    if not abierta:
        if not use_templates:
            return False, ventana
        from messaging import send_whatsapp_template, render_template_body
        if grupo == "a":
            tpl = "oferta_cupo"
            params = [nombre or "paciente", esp.title(), prof_nombre or "nuestro equipo", cuando, hora]
        else:
            if not _consent_marketing_ok(phone):
                return False, ventana
            manana = (datetime.now(_CL).date() + timedelta(days=1)).strftime("%Y-%m-%d")
            if fecha != manana:
                return False, ventana   # sin template adecuado para horas de hoy
            tpl = "horas_liberadas_v1"
            params = [nombre or "Hola", esp.title(), _fecha_humana(fecha).replace("mañana ", ""), hora]
        wamid = await send_whatsapp_template(phone, tpl, body_params=params)
        if not wamid:
            return False, ventana
        try:
            log_message(phone, "out", render_template_body(tpl, params), "IDLE")
        except Exception:
            pass
        return True, ventana

    saludo = f"Hola {nombre} 👋" if nombre else "Hola 👋"
    prof_txt = f" con *{prof_nombre}*" if prof_nombre else ""
    cola = ("Es por orden de llegada. Si la quieres, toca *Tomar la hora* y te la aparto al tiro. "
            "Si ya la tomó otra persona, te aviso.")
    if grupo == "a":
        cola += "\n\n_Te escribimos porque estás en nuestra lista de espera._"
    else:
        cola += "\n\n_Te escribimos porque hace poco pediste hora de esta especialidad._"
    msg = (f"{saludo}\n\n¡Se liberó una hora de *{esp.title()}*{prof_txt}!\n\n"
           f"📅 *{cuando}* a las *{hora}*\n\n{cola}")
    if real:
        from messaging import send_whatsapp_interactive
        await send_whatsapp_interactive(phone, {
            "type": "button", "body": {"text": msg},
            "action": {"buttons": [{"type": "reply",
                                    "reply": {"id": "tomar_hora", "title": "Tomar la hora"}}]},
        })
    else:
        msg = msg.replace("toca *Tomar la hora*", "responde *TOMAR*")
        await send_fn(phone, msg)
    try:
        log_message(phone, "out", msg, "IDLE")
    except Exception:
        pass
    return True, ventana


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


async def _responder(phone: str, msg: str, send_fn, boton_agendar: str | None = None) -> None:
    """Responde al paciente que acaba de escribir (ventana abierta). Con
    `boton_agendar` (canal real) agrega "Buscar otra hora": deja la especialidad
    en la sesión para que el botón agende directo (mismo mecanismo que tras
    cancelar)."""
    from session import log_message
    if send_fn is None:
        if boton_agendar:
            try:
                from session import get_session, save_session
                ses = get_session(phone)
                data = dict(ses.get("data") or {})
                data["_post_cancel_esp"] = (boton_agendar or "").lower().strip()
                save_session(phone, "IDLE", data)
                from messaging import send_whatsapp_interactive
                await send_whatsapp_interactive(phone, {
                    "type": "button", "body": {"text": msg},
                    "action": {"buttons": [{"type": "reply",
                                            "reply": {"id": "1", "title": "Buscar otra hora"}}]}})
                log_message(phone, "out", msg, "IDLE")
                return
            except Exception as e:  # noqa: BLE001
                log.warning("operativa: botón 'otra hora' falló (%s) → texto", e)
        from messaging import send_whatsapp as send_fn  # type: ignore
    await send_fn(phone, msg)
    try:
        log_message(phone, "out", msg, "IDLE")
    except Exception:
        pass


async def accept_offer(offer: dict, *, send_fn=None) -> dict:
    """Resuelve la aceptación de una oferta: claim atómico (primero que acepta gana)
    → reserva provisional (hold blando) → política de confirmación."""
    from session import claim_offer, log_event, log_message
    phone = offer["phone"]
    real = send_fn is None   # canal real: se pueden mandar botones
    mins = _minutos_hasta(offer.get("fecha", ""), offer.get("hora", ""))
    if mins is not None and mins <= 0:
        from session import set_offer_estado
        set_offer_estado(offer["id"], "expirada")
        msg = "Esa hora ya pasó 😔 Escribe *menu* si quieres que te busque otra."
        await _responder(phone, msg, send_fn)
        log_event(phone, "operativa_oferta_vencida", {"offer_id": offer["id"], "slot_key": offer["slot_key"]})
        return {"ok": False, "reason": "hora_pasada"}

    claim = claim_offer(offer["id"], ttl_minutes=HOLD_TTL_MIN)
    if not claim["ok"]:
        if claim["reason"] == "slot_taken":
            es_lista = (offer.get("grupo") or "a") == "a"
            msg = ("Uy, esa hora ya la tomaron 😔\n\n"
                   + ("Sigues en tu lugar en la lista de espera y te avisamos apenas se libere otra.\n\n"
                      if es_lista else "")
                   + "¿Quieres que te busque la siguiente hora disponible?")
            await _responder(phone, msg, send_fn, boton_agendar=offer.get("especialidad", "") if real else None)
            log_event(phone, "operativa_perdida", {"offer_id": offer["id"], "slot_key": offer["slot_key"]})
        return claim

    if send_fn is None:
        from messaging import send_whatsapp as send_fn  # type: ignore

    o = claim["offer"]
    log_event(phone, "operativa_apartada", {"offer_id": o["id"], "slot_key": o["slot_key"]})
    log_event(phone, "hora_liberada_tomada", {
        "offer_id": o["id"], "slot_key": o["slot_key"], "grupo": o.get("grupo") or "a",
        "tanda": o.get("tanda") or 1, "especialidad": o.get("especialidad"),
        "fecha": o.get("fecha"), "hora": o.get("hora")})

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
