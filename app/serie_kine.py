"""Series de kinesiología: orden con N sesiones → agenda día por medio.

Flujo (diseñado con el dueño, 2026-08-01):
  1. El clasificador de imágenes lee la orden ("10 sesiones KNT rodilla...") y
     `detectar_sesiones_kine()` extrae el N.
  2. El bot pregunta CUÁNTAS quiere dejar agendadas (todas / menos / solo la
     primera) — estado WAIT_SERIE_KINE_N en flows.py.
  3. La PRIMERA sesión se agenda con el flujo normal completo (RUT, slots,
     confirmación — toda la maquinaria existente, cero atajos de identidad).
  4. Al confirmarse esa cita, `agendar_resto_serie()` corre en background:
     crea las N-1 restantes DÍA POR MEDIO con el MISMO profesional, en el
     horario más cercano al de la primera, y manda el calendario completo.

Guardrails respetados:
  - 429 Medilink ([[cmc-429]]): pausa de 1s entre llamadas, nunca ráfaga.
  - Bloqueo "1 cita por profesional": no aplica — las citas de la serie se
    crean directo acá (el bloqueo protege el flujo conversacional); citas en
    DÍAS DISTINTOS son exactamente el caso legítimo que el dueño definió.
  - Aborta tras 3 fallos consecutivos (agenda llena / Medilink caído): lo
    logrado queda, el resto lo coordina recepción — nunca loop infinito.
  - Cada cita queda en citas_bot → recordatorios y paneles funcionan solos.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

log = logging.getLogger("serie_kine")

_KINE_RE = re.compile(r"kinesi|kinesiterap|\bknt\b|fisioterap|rehabilitac", re.I)
_N_RE = re.compile(r"(\d{1,2})\s*(?:sesion|ses\b)", re.I)

_DIAS_ES = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def detectar_sesiones_kine(examenes: list) -> dict | None:
    """Si algún examen de la orden es kine con N sesiones (2-15), retorna
    {"n": N, "texto": examen}. N=1 no es serie — el flujo normal basta."""
    for e in examenes or []:
        if not isinstance(e, str) or not _KINE_RE.search(e):
            continue
        m = _N_RE.search(e)
        if not m:
            continue
        n = int(m.group(1))
        if 2 <= n <= 15:
            return {"n": n, "texto": e.strip()}
    return None


def _parse_fecha(s: str):
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime((s or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def _display(s: str) -> str:
    d = _parse_fecha(s)
    if not d:
        return s
    return f"{_DIAS_ES[d.weekday()]} {d.strftime('%d/%m')}"


def _mins(h: str) -> int:
    try:
        return int(h[:2]) * 60 + int(h[3:5])
    except (ValueError, IndexError, TypeError):
        return 0


async def proponer_plan(*, n_total: int, id_profesional: int,
                        fecha_base: str, hora_base: str) -> list[dict]:
    """Busca (SOLO LECTURA, sin crear nada) los cupos para las sesiones 2..N
    día por medio, salta domingos, horario más cercano al de la sesión 1.
    Retorna lista de slots serializables para guardar en la sesión."""
    from medilink import buscar_slots_dia_por_ids

    fecha_prev = _parse_fecha(fecha_base)
    if not fecha_prev:
        return []
    base_m = _mins(hora_base)
    plan: list[dict] = []
    fallos_consec = 0

    while len(plan) < n_total - 1 and fallos_consec < 3:
        target = fecha_prev + timedelta(days=2)  # día por medio
        slot = None
        for corrimiento in range(4):  # target, +1, +2, +3 si no hay agenda
            f = target + timedelta(days=corrimiento)
            if f.weekday() == 6:  # domingo
                continue
            await asyncio.sleep(0.9)  # pacing — guardrail 429 Medilink
            try:
                smart, todos = await buscar_slots_dia_por_ids(
                    [id_profesional], f.strftime("%Y-%m-%d"))
            except Exception as e:  # noqa: BLE001
                log.warning("serie_kine plan slots %s fallo: %s", f, str(e)[:120])
                continue
            pool = [s for s in (todos or smart or []) if not s.get("sobrecupo")]
            if not pool:
                continue
            slot = min(pool, key=lambda s: abs(_mins(s.get("hora_inicio", "")) - base_m))
            break
        if slot is None:
            fallos_consec += 1
            fecha_prev = target
            continue
        fallos_consec = 0
        plan.append({
            "fecha": slot["fecha"],
            "hora_inicio": slot["hora_inicio"],
            "hora_fin": slot["hora_fin"],
            "id_recurso": slot.get("id_recurso", 1),
        })
        fecha_prev = _parse_fecha(slot["fecha"]) or fecha_prev
    return plan


def _lineas_calendario(base: dict, plan: list[dict]) -> str:
    """Calendario legible: sesión 1 (ya reservada) + propuestas."""
    lineas = [f"  1. {_display(base.get('fecha_base', ''))} · "
              f"{str(base.get('hora_base', ''))[:5]} ✅ _(ya reservada)_"]
    for i, p in enumerate(plan, start=2):
        lineas.append(f"  {i}. {_display(p['fecha'])} · {p['hora_inicio'][:5]}")
    return "\n".join(lineas)


def msg_calendario(base: dict, plan: list[dict], nota: str = "") -> dict:
    """Mensaje interactivo con el calendario propuesto y los 3 botones:
    reservar / cambiar alguna sesión / no. Reutilizado por la propuesta
    inicial y por cada re-muestra tras editar una sesión."""
    n_total = int(base.get("n_total") or 0)
    faltan = n_total - 1 - len(plan)
    cuerpo = (
        f"Este sería tu calendario con {base.get('profesional', '')} 📅\n\n"
        + _lineas_calendario(base, plan) + "\n\n"
    )
    if faltan > 0:
        cuerpo += (f"_(Encontré cupo para {len(plan) + 1} de las {n_total}; "
                   "el resto lo coordina recepción)_\n\n")
    if nota:
        cuerpo += nota + "\n\n"
    cuerpo += "¿Te reservo estas horas?"
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": cuerpo},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "serie_cal_ok",
                                            "title": "✅ Sí, resérvalas"}},
                {"type": "reply", "reply": {"id": "serie_cal_editar",
                                            "title": "✏️ Cambiar alguna"}},
                {"type": "reply", "reply": {"id": "serie_cal_no",
                                            "title": "❌ Mejor no"}},
            ]},
        },
    }


async def ofrecer_plan(phone: str, base: dict) -> None:
    """Background: arma el calendario propuesto y lo MUESTRA con botones de
    confirmación — NO crea ninguna cita (fix 2026-08-01: el paciente eligió
    N pero nunca había confirmado el calendario concreto)."""
    from messaging import send_whatsapp
    from session import save_session, log_event, log_message

    n_total = int(base.get("n_total") or 0)
    plan = await proponer_plan(
        n_total=n_total,
        id_profesional=base["id_profesional"],
        fecha_base=base.get("fecha_base", ""),
        hora_base=base.get("hora_base", ""),
    )
    if not plan:
        msg = ("No encontré cupos para armar la serie en las próximas "
               "semanas 😕 Quedaste con tu primera sesión reservada; "
               "recepción te escribirá para coordinar el resto.")
        try:
            await send_whatsapp(phone, msg)
            log_message(phone, "out", msg, "IDLE", canal="whatsapp")
        except Exception:  # noqa: BLE001
            pass
        log_event(phone, "serie_kine_plan_sin_cupos", {"n_total": n_total})
        return

    save_session(phone, "WAIT_SERIE_KINE_CONFIRM", {
        "serie_kine_base": base,
        "serie_kine_plan": plan,
    })
    msg_btn = msg_calendario(base, plan)
    try:
        await send_whatsapp(phone, msg_btn)
        log_message(phone, "out", msg_btn["interactive"]["body"]["text"],
                    "WAIT_SERIE_KINE_CONFIRM", canal="whatsapp")
    except Exception as e:  # noqa: BLE001
        log.error("serie_kine ofrecer_plan envio fallo: %s", e)
    log_event(phone, "serie_kine_plan_propuesto", {
        "n_total": n_total, "propuestas": len(plan)})


async def buscar_alternativas(base: dict, plan: list[dict], idx0: int,
                              filtro: str = "") -> list[dict]:
    """Alternativas REALES (solo lectura) para la sesión plan[idx0]: otras
    horas del mismo día y de los días vecinos, evitando chocar con las demás
    sesiones del plan. filtro: "" | "manana" | "tarde"."""
    from medilink import buscar_slots_dia_por_ids

    objetivo = _parse_fecha(plan[idx0]["fecha"])
    if not objetivo:
        return []
    base_m = _mins(base.get("hora_base", "") or plan[idx0]["hora_inicio"])
    ocupadas = {(p["fecha"], p["hora_inicio"])
                for i, p in enumerate(plan) if i != idx0}
    ocupadas.add((base.get("fecha_base", ""), base.get("hora_base", "")))

    candidatos: list[dict] = []
    for delta in (0, -1, 1, 2):
        f = objetivo + timedelta(days=delta)
        if f.weekday() == 6:  # domingo
            continue
        await asyncio.sleep(0.9)  # pacing — guardrail 429 Medilink
        try:
            smart, todos = await buscar_slots_dia_por_ids(
                [base["id_profesional"]], f.strftime("%Y-%m-%d"))
        except Exception as e:  # noqa: BLE001
            log.warning("serie_kine alternativas %s fallo: %s", f, str(e)[:120])
            continue
        for s in (todos or smart or []):
            if s.get("sobrecupo"):
                continue
            h = s.get("hora_inicio", "")
            if (s.get("fecha"), h) in ocupadas:
                continue
            hm = _mins(h)
            if filtro == "manana" and hm >= 13 * 60:
                continue
            if filtro == "tarde" and hm < 13 * 60:
                continue
            candidatos.append({
                "fecha": s["fecha"], "hora_inicio": h,
                "hora_fin": s.get("hora_fin", ""),
                "id_recurso": s.get("id_recurso", 1),
                "_orden": (abs(delta), abs(hm - base_m)),
            })
    # Dedupe por (fecha, hora), ordenar por cercanía de día y de hora, cap 8
    vistos, unicos = set(), []
    for c in sorted(candidatos, key=lambda c: c["_orden"]):
        k = (c["fecha"], c["hora_inicio"])
        if k in vistos:
            continue
        vistos.add(k)
        c.pop("_orden", None)
        unicos.append(c)
        if len(unicos) >= 8:
            break
    return unicos


async def ofrecer_alternativas(phone: str, base: dict, plan: list[dict],
                               idx0: int, filtro: str = "") -> None:
    """Background: busca alternativas para la sesión idx0 y las muestra
    numeradas. Deja la sesión en WAIT_SERIE_KINE_EDIT_PICK."""
    from messaging import send_whatsapp
    from session import save_session, log_event, log_message

    alts = await buscar_alternativas(base, plan, idx0, filtro)
    sesion_num = idx0 + 2
    if not alts:
        # Sin alternativas cerca → volver al calendario tal cual
        save_session(phone, "WAIT_SERIE_KINE_CONFIRM", {
            "serie_kine_base": base, "serie_kine_plan": plan})
        msg_btn = msg_calendario(
            base, plan,
            nota=(f"_No encontré otras horas cerca de la sesión {sesion_num} 😕 "
                  "— si ninguna te acomoda, recepción puede armarlo contigo._"))
        try:
            await send_whatsapp(phone, msg_btn)
            log_message(phone, "out", msg_btn["interactive"]["body"]["text"],
                        "WAIT_SERIE_KINE_CONFIRM", canal="whatsapp")
        except Exception:  # noqa: BLE001
            pass
        log_event(phone, "serie_kine_edit_sin_alternativas",
                  {"sesion": sesion_num})
        return

    save_session(phone, "WAIT_SERIE_KINE_EDIT_PICK", {
        "serie_kine_base": base,
        "serie_kine_plan": plan,
        "serie_kine_edit_idx": idx0,
        "serie_kine_alts": alts,
    })
    actual = plan[idx0]
    lineas = "\n".join(
        f"  {i}. {_display(a['fecha'])} · {a['hora_inicio'][:5]}"
        for i, a in enumerate(alts, start=1))
    msg = (
        f"Opciones para la *sesión {sesion_num}* "
        f"(hoy propuesta {_display(actual['fecha'])} · "
        f"{actual['hora_inicio'][:5]}):\n\n{lineas}\n\n"
        "Elige un *número*, o escribe *volver* para dejarla como estaba."
    )
    try:
        await send_whatsapp(phone, msg)
        log_message(phone, "out", msg, "WAIT_SERIE_KINE_EDIT_PICK",
                    canal="whatsapp")
    except Exception as e:  # noqa: BLE001
        log.error("serie_kine ofrecer_alternativas envio fallo: %s", e)
    log_event(phone, "serie_kine_edit_alternativas", {
        "sesion": sesion_num, "opciones": len(alts), "filtro": filtro})


async def crear_serie(phone: str, base: dict, plan: list[dict]) -> None:
    """Background: crea las citas del plan YA CONFIRMADO por el paciente.
    Si un cupo se ocupó entre la propuesta y la confirmación, se omite y se
    informa — nunca se inventa otro horario sin mostrarlo."""
    from medilink import crear_cita
    from messaging import send_whatsapp
    from session import save_cita_bot, log_event, log_message

    n_total = int(base.get("n_total") or 0)
    creadas: list[tuple[str, str]] = [(base.get("fecha_base", ""),
                                       base.get("hora_base", ""))]
    fallidas = 0
    for i, p in enumerate(plan, start=2):
        await asyncio.sleep(1.0)  # pacing — guardrail 429 Medilink
        try:
            resultado = await asyncio.wait_for(crear_cita(
                id_paciente=base["id_paciente"],
                id_profesional=base["id_profesional"],
                fecha=p["fecha"],
                hora_inicio=p["hora_inicio"],
                hora_fin=p["hora_fin"],
                id_recurso=p.get("id_recurso", 1),
                observaciones_extra=f"[SERIE KNT {i}/{n_total}]",
            ), timeout=45)
        except Exception as e:  # noqa: BLE001
            log.warning("serie_kine crear s%d fallo: %s", i, str(e)[:150])
            resultado = None
        if not resultado:
            fallidas += 1
            log_event(phone, "serie_kine_cita_fallida", {
                "sesion": i, "fecha": p["fecha"], "hora": p["hora_inicio"]})
            continue
        id_cita = str(resultado.get("id", "")) if isinstance(resultado, dict) else ""
        try:
            save_cita_bot(
                phone=phone, id_cita=id_cita,
                especialidad=base.get("especialidad", "Kinesiología"),
                profesional=base.get("profesional", ""),
                fecha=p["fecha"], hora=p["hora_inicio"],
                modalidad=base.get("modalidad", "particular"),
                paciente_nombre=base.get("paciente_nombre", ""),
                es_tercero=bool(base.get("es_tercero")),
                id_paciente_medilink=base["id_paciente"],
            )
        except Exception as e:  # noqa: BLE001
            log.error("serie_kine save_cita_bot fallo: %s", e)
        log_event(phone, "serie_kine_cita_creada", {
            "sesion": i, "n_total": n_total, "id_cita": id_cita,
            "fecha": p["fecha"], "hora": p["hora_inicio"]})
        creadas.append((p["fecha"], p["hora_inicio"]))

    lineas = "\n".join(
        f"  {i + 1}. {_display(f)} · {str(h)[:5]}"
        for i, (f, h) in enumerate(creadas))
    msg = (f"✅ *¡Listo! Tus sesiones quedaron reservadas* — "
           f"{base.get('profesional', '')}:\n\n{lineas}\n\n")
    if fallidas > 0:
        msg += (f"_{fallidas} {'cupo se ocupó' if fallidas == 1 else 'cupos se ocuparon'} "
                "mientras confirmabas; recepción te escribirá para "
                "reubicarlo(s)._\n\n")
    msg += ("Te recordaremos cada sesión el día anterior. Si necesitas "
            "cambiar alguna, escríbeme no más 😊")
    try:
        await send_whatsapp(phone, msg)
        log_message(phone, "out", msg, "IDLE", canal="whatsapp")
    except Exception as e:  # noqa: BLE001
        log.error("serie_kine resumen fallo: %s", e)
    log_event(phone, "serie_kine_completada", {
        "creadas": len(creadas), "n_total": n_total, "fallidas": fallidas})
