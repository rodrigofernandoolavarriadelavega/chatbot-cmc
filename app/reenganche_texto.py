"""Texto de los mensajes proactivos de reenganche y seguimiento de información.

Solo funciones puras (sin I/O): `jobs.py` decide a quién y cuándo, acá se arma
QUÉ se le dice. Reglas:
  - nunca saludo con doble espacio (nombre vacío → "Hola 👋");
  - nunca "reserva pendiente": el paciente miró horas, no reservó nada;
  - escasez solo con el conteo real de horas de ese día;
  - especialidades sensibles (psiquiatría, psicología, ginecología/matrona)
    no se nombran en un mensaje que puede leerse en la pantalla bloqueada.
Nombres de especialidad/profesional: se reusa `persistencia._esp_para_mensaje`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from persistencia import _esp_para_mensaje, _sin_tildes

_SENSIBLES = ("psiquiatr", "psicolog", "ginecolog", "matron", "obstetric")


def primer_nombre(*fuentes) -> str:
    """Primer nombre capitalizado de la primera fuente no vacía."""
    for f in fuentes:
        partes = str(f or "").replace("*", "").split()
        if partes:
            return partes[0].capitalize()
    return ""


def saludo(nombre: str) -> str:
    return f"Hola {nombre} 👋" if nombre else "Hola 👋"


def es_sensible(esp: str | None) -> bool:
    e = _sin_tildes(esp or "")
    if not e:
        return False
    if any(k in e for k in _SENSIBLES):
        return True
    # "esp" puede ser el apellido de un profesional ("salas"): mirar su especialidad.
    try:
        from medilink import PROFESIONALES
        for info in PROFESIONALES.values():
            apellidos = [_sin_tildes(w) for w in info.get("nombre", "").replace(".", " ").split()[1:]]
            if e in apellidos and len(e) >= 4:
                esp_prof = _sin_tildes(info.get("especialidad", ""))
                return any(k in esp_prof for k in _SENSIBLES)
    except Exception:
        pass
    return False


def esp_txt(esp: str | None) -> str:
    """' de *Kinesiología*' / ' con *Dr. Andrés Abarca*' / '' (vacía o sensible)."""
    if not esp or es_sensible(esp):
        return ""
    return _esp_para_mensaje(esp)


def referencia_slot(slot: dict, hoy: date | None = None, con_prof: bool = True) -> str:
    """'el martes 7 de octubre a las 10:30 con Leonardo Etcheverry' (o 'hoy'/'mañana')."""
    hoy = hoy or datetime.now(ZoneInfo("America/Santiago")).date()
    fecha = slot.get("fecha") or ""
    disp = (slot.get("fecha_display") or "").strip()
    if fecha == hoy.isoformat():
        dia = "hoy"
    elif fecha == (hoy + timedelta(days=1)).isoformat():
        dia = "mañana"
    else:
        dia = "el " + (disp[:1].lower() + disp[1:]) if disp else ""
    hora = (slot.get("hora_inicio") or "")[:5]
    txt = f"{dia} a las {hora}".strip()
    prof = (slot.get("profesional") or "").strip()
    return f"{txt} con {prof}" if (prof and con_prof) else txt


# Medición prod 90 días: hora concreta sola 18,7% agenda en 7d; con "Quedan solo N
# horas" 9,5% (la mitad). Apagado por defecto; si se enciende, N es el conteo real
# de horas libres de ese día.
ESCASEZ_ACTIVA = False


def aviso_escasez(n: int | None) -> str:
    if not ESCASEZ_ACTIVA:
        return ""
    if n == 1:
        return " Es la única hora que queda ese día."
    if n and 2 <= n <= 4:
        return f" Ese día quedan solo {n} horas."
    return ""


def bloque_slot(slot: dict | None, n: int | None, esp: str | None = None) -> str:
    """Párrafo con la próxima hora real, listo para insertar tras el saludo."""
    if not slot:
        return ""
    con_prof = not esp_txt(esp).startswith(" con")
    return (f"\n\nLa próxima hora disponible es {referencia_slot(slot, con_prof=con_prof)}."
            f"{aviso_escasez(n)}")


# ── Seguimiento de información ───────────────────────────────────────────────

def msg_followup_info(nombre: str, esp: str | None, slot: dict | None,
                      n_slots: int | None = None) -> tuple[str, str]:
    """Devuelve (texto, tipo) con tipo in {'slot', 'esp', 'corto'}.

    'slot': especialidad + próxima hora real → botones Sí reservar / Ver otras / No.
    'esp' : especialidad sin hora (Medilink sin respuesta) → Ver horas / No.
    'corto': sin especialidad → Ver horas / No.
    """
    sal = saludo(nombre)
    sensible = es_sensible(esp)
    et = esp_txt(esp)
    if esp and slot:
        quien = ("la hora que consultaste" if sensible
                 else f"una hora{et}")
        intro = (f"Te escribo por {quien} hace un rato." if sensible
                 else f"Hace un rato nos consultaste por {quien}.")
        con_prof = not et.startswith(" con")
        return (f"{sal} {intro}\n\nLa próxima hora disponible es "
                f"{referencia_slot(slot, con_prof=con_prof)}.{aviso_escasez(n_slots)}\n\n"
                "¿Te la reservo?", "slot")
    if esp:
        intro = ("Te escribo por la hora que consultaste hace un rato." if sensible
                 else f"Hace un rato nos consultaste por una hora{et}.")
        return (f"{sal} {intro}\n\n¿Quieres que te ayude a ver las horas disponibles?", "esp")
    return (f"{sal} Hace un rato nos escribiste al Centro Médico Carampangue.\n\n"
            "¿Quieres que te ayude a ver las horas disponibles?", "corto")


BOTONES_FOLLOWUP = {
    "slot": [{"id": "agendar_sugerido", "title": "Sí, reservar"},
             {"id": "ver_otros", "title": "Ver otras horas"},
             {"id": "no_gracias_reeng", "title": "No, gracias"}],
    "esp": [{"id": "agendar_sugerido", "title": "Ver horas"},
            {"id": "no_gracias_reeng", "title": "No, gracias"}],
    "corto": [{"id": "1", "title": "Ver horas"},
              {"id": "no_gracias_reeng", "title": "No, gracias"}],
}

PIE_TEXTO_LIBRE = {
    "slot": "\n\nResponde *sí* para reservar, *otras horas* para ver más o *no* si ya no te interesa.",
    "esp": "\n\nResponde *sí* para ver las horas o *no* si ya no te interesa.",
    "corto": "\n\nEscribe *menu* para ver las horas o *no* si ya no te interesa.",
}


# ── Reenganche de sesiones abandonadas ───────────────────────────────────────

_ESTADOS_SIN_SLOT = ("WAIT_DURACION_MASOTERAPIA", "WAIT_RUT_CANCELAR", "WAIT_CITA_CANCELAR",
                     "WAIT_RUT_REAGENDAR", "WAIT_CITA_REAGENDAR", "WAIT_RUT_VER",
                     "WAIT_ESPECIALIDAD")


def usa_slot(estado: str) -> bool:
    """False en los estados cuyo mensaje no muestra hora (evita consultar Medilink de más)."""
    return estado not in _ESTADOS_SIN_SLOT


def msg_reenganche(estado: str, nombre: str, esp: str | None,
                   slot: dict | None, n_slots: int | None) -> str:
    sal = saludo(nombre)
    et = esp_txt(esp)
    bloque = bloque_slot(slot, n_slots, esp)
    if estado == "WAIT_SLOT":
        return (f"{sal} Te quedaste a punto de elegir tu hora{et}."
                f"{bloque}\n\n" + ("¿Te la reservo?" if slot else "¿Te ayudo a terminar?"))
    if estado in ("CONFIRMING_CITA", "WAIT_RUT_AGENDAR", "WAIT_DATOS_NUEVO", "WAIT_NOMBRE_NUEVO"):
        return (f"{sal} Quedaste a un paso de confirmar tu hora{et}."
                f"{bloque}\n\nSolo falta un dato para reservarla. ¿Seguimos?")
    if estado == "WAIT_DURACION_MASOTERAPIA":
        return (f"{sal} Te quedaste eligiendo la duración de tu *masoterapia* "
                "(20 o 40 min). ¿Seguimos para reservar tu hora?")
    if estado in ("WAIT_RUT_CANCELAR", "WAIT_CITA_CANCELAR"):
        return (f"{sal} Te quedaste a mitad de cancelar tu hora{et}. "
                "¿Seguimos con la anulación o prefieres dejarla como está?")
    if estado in ("WAIT_RUT_REAGENDAR", "WAIT_CITA_REAGENDAR"):
        return (f"{sal} Te quedaste a mitad de reagendar tu hora{et}. "
                "¿Seguimos buscando un nuevo horario?")
    if estado == "WAIT_RUT_VER":
        return f"{sal} Te quedaste viendo tus horas reservadas. ¿Te ayudo con algo más?"
    if estado == "WAIT_ESPECIALIDAD":
        return (f"{sal} Te quedaste eligiendo la especialidad que necesitas. "
                "¿Seguimos buscando tu hora?")
    # Resto de estados WAIT_*: no hay reserva, solo un agendamiento a medias.
    return (f"{sal} Te quedaste a mitad de agendar tu hora{et}."
            f"{bloque}\n\n¿Te ayudo a terminar?")


def variante_reenganche(estado: str, slot: dict | None) -> str:
    """Etiqueta para medir cada versión (event reenganche_enviado.meta.variante)."""
    base = {"WAIT_SLOT": "a_punto_elegir", "WAIT_ESPECIALIDAD": "eligiendo_esp",
            "WAIT_DURACION_MASOTERAPIA": "duracion_maso", "WAIT_RUT_VER": "viendo_horas"}.get(estado)
    if estado in ("CONFIRMING_CITA", "WAIT_RUT_AGENDAR", "WAIT_DATOS_NUEVO", "WAIT_NOMBRE_NUEVO"):
        base = "a_un_paso"
    elif estado in ("WAIT_RUT_CANCELAR", "WAIT_CITA_CANCELAR"):
        base = "mitad_cancelar"
    elif estado in ("WAIT_RUT_REAGENDAR", "WAIT_CITA_REAGENDAR"):
        base = "mitad_reagendar"
    base = base or "mitad_agendar"
    return base + ("_con_hora" if slot and usa_slot(estado) else "_sin_hora")
