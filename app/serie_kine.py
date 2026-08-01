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


async def agendar_resto_serie(phone: str, *, n_total: int, id_paciente: int,
                              id_profesional: int, profesional: str,
                              especialidad: str, fecha_base: str, hora_base: str,
                              modalidad: str, paciente_nombre: str,
                              es_tercero: bool = False) -> None:
    """Crea las sesiones 2..N día por medio (salta domingos) con el mismo
    profesional, en el horario disponible más cercano al de la sesión 1.
    Corre como task de background — todos los errores quedan logueados,
    nunca propagan al webhook."""
    from medilink import buscar_slots_dia_por_ids, crear_cita
    from messaging import send_whatsapp
    from session import save_cita_bot, log_event, log_message

    fecha_prev = _parse_fecha(fecha_base)
    if not fecha_prev:
        log.error("serie_kine: fecha_base ilegible %r — abortando", fecha_base)
        return

    creadas: list[tuple[str, str]] = [(fecha_base, hora_base)]  # sesión 1 (ya existe)
    base_m = _mins(hora_base)
    fallos_consec = 0
    sesion = 1

    while len(creadas) < n_total and fallos_consec < 3:
        sesion += 1
        target = fecha_prev + timedelta(days=2)  # día por medio
        slot = None
        for corrimiento in range(4):  # target, +1, +2, +3 si no hay agenda
            f = target + timedelta(days=corrimiento)
            if f.weekday() == 6:  # domingo
                continue
            await asyncio.sleep(1.0)  # pacing — guardrail 429 Medilink
            try:
                smart, todos = await buscar_slots_dia_por_ids(
                    [id_profesional], f.strftime("%Y-%m-%d"))
            except Exception as e:  # noqa: BLE001
                log.warning("serie_kine slots %s fallo: %s", f, str(e)[:120])
                continue
            pool = [s for s in (todos or smart or []) if not s.get("sobrecupo")]
            if not pool:
                continue
            slot = min(pool, key=lambda s: abs(_mins(s.get("hora_inicio", "")) - base_m))
            break

        if slot is None:
            fallos_consec += 1
            fecha_prev = target  # avanzar la ventana, no re-picar el mismo hueco
            log_event(phone, "serie_kine_sesion_sin_cupo", {
                "sesion": sesion, "desde": target.isoformat()})
            continue

        await asyncio.sleep(1.0)
        try:
            resultado = await asyncio.wait_for(crear_cita(
                id_paciente=id_paciente,
                id_profesional=id_profesional,
                fecha=slot["fecha"],
                hora_inicio=slot["hora_inicio"],
                hora_fin=slot["hora_fin"],
                id_recurso=slot.get("id_recurso", 1),
                observaciones_extra=f"[SERIE KNT {sesion}/{n_total}]",
            ), timeout=45)
        except Exception as e:  # noqa: BLE001
            log.warning("serie_kine crear_cita fallo s%d: %s", sesion, str(e)[:150])
            resultado = None

        if not resultado:
            fallos_consec += 1
            fecha_prev = _parse_fecha(slot["fecha"]) or (fecha_prev + timedelta(days=2))
            continue

        fallos_consec = 0
        id_cita = str(resultado.get("id", "")) if isinstance(resultado, dict) else ""
        try:
            save_cita_bot(
                phone=phone, id_cita=id_cita, especialidad=especialidad,
                profesional=profesional, fecha=slot["fecha"],
                hora=slot["hora_inicio"], modalidad=modalidad,
                paciente_nombre=paciente_nombre, es_tercero=es_tercero,
                id_paciente_medilink=id_paciente,
            )
        except Exception as e:  # noqa: BLE001
            log.error("serie_kine save_cita_bot fallo: %s", e)
        log_event(phone, "serie_kine_cita_creada", {
            "sesion": sesion, "n_total": n_total, "id_cita": id_cita,
            "fecha": slot["fecha"], "hora": slot["hora_inicio"],
        })
        creadas.append((slot["fecha"], slot["hora_inicio"]))
        fecha_prev = _parse_fecha(slot["fecha"]) or fecha_prev

    # ── Resumen al paciente ──────────────────────────────────────────────
    lineas = "\n".join(
        f"  {i + 1}. {_display(f)} · {str(h)[:5]}" for i, (f, h) in enumerate(creadas)
    )
    faltan = n_total - len(creadas)
    msg = (
        f"📅 *Tu plan de {especialidad.lower()} quedó agendado* — "
        f"{profesional}:\n\n{lineas}\n\n"
    )
    if faltan > 0:
        msg += (f"_No encontré cupo para {faltan} "
                f"{'sesión' if faltan == 1 else 'sesiones'}; recepción te "
                "escribirá para coordinarlas._\n\n")
    msg += ("Te recordaremos cada sesión el día anterior. Si necesitas "
            "cambiar alguna, escríbeme no más 😊")
    try:
        await send_whatsapp(phone, msg)
        log_message(phone, "out", msg, "IDLE", canal="whatsapp")
    except Exception as e:  # noqa: BLE001
        log.error("serie_kine resumen fallo: %s", e)
    log_event(phone, "serie_kine_completada", {
        "creadas": len(creadas), "n_total": n_total, "faltantes": faltan,
    })
