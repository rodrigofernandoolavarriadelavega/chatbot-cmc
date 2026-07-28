"""mapa_centro.py — Inventario único de todo lo que hay en marcha en el CMC.

Por qué existe: el centro tiene decenas de frentes en paralelo y el estado real
vive repartido en commits, flags apagados, crons registrados y dashboards
sueltos. Nadie puede sostener eso en la cabeza, y lo que no se ve, no se decide.

Dos capas, a propósito:

  1. INVENTARIO CURADO (`ITEMS`, abajo). Una línea por frente, con su etapa, qué
     falta para la siguiente y quién decide. Vive en código porque así queda
     versionado y se actualiza en el mismo commit que mueve la aguja.

  2. SONDAS EN VIVO (`_sondas`). Lo que el sistema puede responder solo —qué
     flags están encendidos, qué crons corren, cuántas citas se crearon— se
     lee de la realidad, no de esta lista. Si un ítem dice "encendido" pero el
     flag está apagado, el panel muestra la contradicción en vez de esconderla.

La regla de oro: si un dato se puede sondear, se sondea. Un inventario que se
mantiene solo a mano miente en dos semanas.

ETAPAS (el orden importa, es el embudo de madurez):
  idea                 — decidido que vale la pena, sin construir
  construido           — el código existe, NO está en producción
  desplegado_apagado   — está en producción pero detrás de un flag apagado
  prueba               — encendido, en observación, sin confiar todavía
  produccion           — encendido y en uso real
  medido               — en uso Y con un número que dice si sirve

La última etapa es la que casi siempre falta: hay cosas encendidas hace meses
que nadie sabe si funcionan.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("mapa_centro")

ETAPAS = ["idea", "construido", "desplegado_apagado", "prueba", "produccion", "medido"]

ETAPA_LABEL = {
    "idea": "Idea",
    "construido": "Construido, sin subir",
    "desplegado_apagado": "Desplegado, apagado",
    "prueba": "En prueba",
    "produccion": "En producción",
    "medido": "En producción y medido",
}

GRUPOS = ["Captación y agenda", "Caja y cobranza", "Clínico", "Operación",
          "Regulatorio e infraestructura", "Personas"]


def _i(id, nombre, grupo, etapa, falta, decide="equipo", plata=None,
       sonda=None, nota=None, alerta=None, confirmar=False):
    return {"id": id, "nombre": nombre, "grupo": grupo, "etapa": etapa,
            "falta": falta, "decide": decide, "plata": plata, "sonda": sonda,
            "nota": nota, "alerta": alerta, "confirmar": confirmar}


# ── EL INVENTARIO ───────────────────────────────────────────────────────────
# `confirmar=True` marca lo que deduje del código o de notas viejas y el dueño
# todavía no me confirmó. Es honestidad, no relleno: mejor decir "creo que" que
# dar por cierto algo que nadie revisó.
ITEMS = [
    # ── Captación y agenda ──────────────────────────────────────────────
    _i("bot_agendar", "Bot de WhatsApp — agendamiento", "Captación y agenda",
       "medido", "Nada urgente. Vigilar la conversión punta a punta (50,8%)",
       plata="582 citas/30d", sonda="metric:citas_30d"),
    _i("agendador_web", "Agendador web público /agendar", "Captación y agenda",
       "produccion", "Medir cuántas citas entran por acá versus por el bot",
       sonda="flag:AGENDADOR_PUBLICO_ENABLED"),
    _i("agendador_v2", "Agendador web v2 (rediseño)", "Captación y agenda",
       "desplegado_apagado",
       "Decidir si se enciende. La v1 tiene conversión 0% en escritorio ≥981px "
       "por un botón que no existe",
       decide="dueño", sonda="flag:AGENDADOR_V2_ENABLED"),
    _i("portal_v5", "Portal del paciente v5", "Captación y agenda",
       "produccion",
       "Prueba con 5 pacientes reales y revisión legal del EIPD. Ninguna hecha",
       decide="dueño", alerta="Sin medir desde que se encendió"),
    _i("lista_espera", "Lista de espera y aviso de cupos", "Captación y agenda",
       "produccion",
       "205 personas esperando, 142 hace más de 30 días. Gastro (59) y "
       "ecografía (49) tienen horas libres HOY",
       plata="Ingreso inmediato", sonda="metric:waitlist",
       alerta="Nadie los está llamando"),
    _i("reenganche", "Reenganche de pacientes que abandonan", "Captación y agenda",
       "produccion",
       "Fix de UNA línea sin deployar: 93% de los abandonos silenciosos "
       "(214 de 230) nunca recibieron el reenganche",
       decide="dueño", plata="Probablemente el mayor agujero de conversión",
       alerta="Fix listo, sin subir"),
    _i("persistencia", "Carril de persistencia (2º toque)", "Captación y agenda",
       "desplegado_apagado",
       "Encender el flag y subir el template a Meta",
       decide="dueño", sonda="flag:PERSISTENCIA_ACTIVE"),
    _i("embudo", "Instrumentación del embudo", "Captación y agenda",
       "construido", "Subirlo. Sin esto no se puede medir dónde se cae la gente"),
    _i("rieles", "Rieles proactivos (promo, pre-examen, recordatorios)",
       "Captación y agenda", "produccion",
       "Medir si mueven la aguja o solo hacen ruido", confirmar=True),
    _i("meta_ads", "Meta Ads y Autopilot", "Captación y agenda",
       "desplegado_apagado",
       "Compuerta legal Ley 21.719 antes de automatizar público",
       decide="dueño"),
    _i("seo", "Sitio, blog y SEO", "Captación y agenda", "produccion",
       "Medir qué tráfico convierte en hora agendada", confirmar=True),

    # ── Caja y cobranza ─────────────────────────────────────────────────
    _i("panel_pagos", "Panel de pagos y caja diaria", "Caja y cobranza",
       "produccion", "—"),
    _i("conciliacion", "Conciliación transferencias × banco", "Caja y cobranza",
       "desplegado_apagado",
       "Encender la lectura y contrastar un mes cerrado contra auditor.py",
       decide="dueño", plata="$136.406 detectados solo en junio",
       sonda="flag:CONCILIACION_TRANSFERENCIAS_ACTIVE"),
    _i("abonos_auto", "Abonos automáticos por transferencia", "Caja y cobranza",
       "desplegado_apagado",
       "Decidir si se enciende para psiquiatría. Le habla a pacientes",
       decide="dueño", sonda="flag:ABONO_AUTO_ACTIVE"),
    _i("metodo_pago", "Método de pago obligatorio en recepción", "Caja y cobranza",
       "construido",
       "Subirlo. Hoy metodo_pago cae en 'efectivo' por defecto y contamina "
       "el único dato de caja real que existe",
       plata="Distorsiona el mix débito/crédito"),
    _i("atenciones_abiertas", "Atenciones que nunca se cerraron", "Caja y cobranza",
       "idea",
       "Verificar a mano si los ~$25M/año son reales: el filtro de fechas de "
       "Medilink devuelve mal y el dato quedó EN DUDA",
       plata="~$25M/año por confirmar", alerta="Medición dudosa"),
    _i("brecha_cobro", "Brecha facturado ↔ cobrado (18%)", "Caja y cobranza",
       "idea", "Nadie la está persiguiendo. Medida, sin dueño",
       plata="18% de lo facturado"),
    _i("fonasa_mle", "Cobranza Fonasa MLE", "Caja y cobranza",
       "idea",
       "No existe sistema. Hoy es manual y nadie revisa rechazos en el portal "
       "Pago Prestadores",
       decide="dueño"),
    _i("ebitda", "EBITDA, DB Mensual y techo por profesional", "Caja y cobranza",
       "produccion", "—", confirmar=True),
    _i("auditor", "Auditor financiero mensual (offline)", "Caja y cobranza",
       "produccion", "Sirve de contraste para validar la conciliación"),

    # ── Clínico ─────────────────────────────────────────────────────────
    _i("copiloto", "Copiloto de ficha", "Clínico", "produccion",
       "Medir cuánto tiempo por consulta ahorra de verdad", confirmar=True),
    _i("pluma", "Alma Pluma (extensión que escribe en Medilink)", "Clínico",
       "produccion", "—", confirmar=True),
    _i("ges", "Motor GES de triage", "Clínico", "produccion", "—", confirmar=True),
    _i("contigo", "Contigo — acompañante de psiquiatría", "Clínico",
       "desplegado_apagado",
       "DNS y certbot. Además persistencia, consentimiento y EIPD antes de "
       "tocar un paciente real",
       decide="dueño"),
    _i("voz", "Agente de voz por WhatsApp", "Clínico", "construido",
       "NO activar hasta que conteste bien. Decisión tomada, no revisar todavía",
       decide="dueño"),
    _i("nutricion", "CMC Nutrición — minuta a canasta valorizada", "Clínico",
       "construido", "Deploy y OCR de minutas en foto"),

    # ── Operación ───────────────────────────────────────────────────────
    _i("alma_shell", "Alma — plataforma unificada", "Operación", "produccion", "—"),
    _i("panel_recepcion", "Panel de recepción v2", "Operación", "produccion", "—"),
    _i("kanban", "Recepción Kanban", "Operación", "produccion", "—", confirmar=True),
    _i("boxes", "Gemelo digital de boxes", "Operación", "produccion", "—", confirmar=True),
    _i("inventario", "Inventario dental, checklist y equipo", "Operación",
       "produccion", "—", confirmar=True),
    _i("mapa_centro", "Este mapa", "Operación", "produccion",
       "Mantenerlo vivo: revisarlo una vez por semana"),

    # ── Regulatorio e infraestructura ───────────────────────────────────
    _i("instagram", "Instagram — responder mensajes", "Regulatorio e infraestructura",
       "produccion",
       "El token expiró el 14 de junio. Van 6 semanas sin poder responder "
       "y nadie se enteró",
       decide="dueño", alerta="CAÍDO hace 6 semanas"),
    _i("saldo_ia", "Saldo de la IA (Anthropic)", "Regulatorio e infraestructura",
       "produccion",
       "Activar auto-reload y hacer que la alerta insista cada 24 h. Avisa una "
       "sola vez y por eso el bot estuvo 6 días sin entender a nadie",
       decide="dueño", plata="~$1,90/día", alerta="Ya falló dos veces"),
    _i("seremi", "Habilitación sanitaria SEREMI", "Regulatorio e infraestructura",
       "idea", "Expediente de ampliación. Filtro real es la DOM de Arauco",
       decide="dueño", confirmar=True),
    _i("ley21719", "Cumplimiento Ley 21.719", "Regulatorio e infraestructura",
       "construido",
       "EIPD del portal v5 en borrador, sin revisión legal. Y definir retención "
       "de los datos bancarios de terceros que guarda la conciliación",
       decide="dueño"),
    _i("sqlcipher", "Cifrado de sessions.db en el VPS", "Regulatorio e infraestructura",
       "idea", "Playbook listo, sin ejecutar", confirmar=True),
    _i("migracion_web", "Migrar el sitio a DigitalOcean", "Regulatorio e infraestructura",
       "idea", "Falta NIC y DNS. Riesgo con el correo MX de Hostinger",
       decide="dueño"),

    # ── Personas ────────────────────────────────────────────────────────
    _i("anguie", "Incorporación de Anguie", "Personas", "prueba",
       "Semana 1 de 8: llamar la lista de espera",
       plata="Su semana 1 es ingreso directo"),
    _i("brecha_plan", "Plan de brecha $3,15M/mes", "Personas", "idea",
       "21 palancas identificadas, sin dueño asignado a cada una",
       decide="dueño", plata="$3,15M/mes"),
    _i("contratacion", "Pipeline de contratación", "Personas", "idea",
       "—", decide="dueño", confirmar=True),
]


# ── Sondas: leer la realidad, no la lista ───────────────────────────────────

def _flag(nombre: str):
    """Los flags del proyecto viven en dos sitios: algunos como atributo de
    `config`, otros se leen del entorno en el punto de uso (ej. PERSISTENCIA_ACTIVE).
    La sonda cubre los dos, si no reportaría "no se pudo leer" para la mitad."""
    try:
        import config
        if hasattr(config, nombre):
            return bool(getattr(config, nombre))
    except Exception:
        pass
    import os
    v = os.getenv(nombre)
    if v is None:
        return None
    return v.strip().lower() in ("1", "true", "yes", "on")


def _metric(nombre: str):
    from session import db
    try:
        with db() as c:
            if nombre == "citas_30d":
                n = c.execute(
                    "SELECT COUNT(*) FROM conversation_events WHERE event='cita_creada' "
                    "AND ts > datetime('now','-30 days')").fetchone()[0]
                return f"{n} citas/30d"
            if nombre == "waitlist":
                n = c.execute(
                    "SELECT COUNT(*) FROM waitlist WHERE canceled_at IS NULL").fetchone()[0]
                viejos = c.execute(
                    "SELECT COUNT(*) FROM waitlist WHERE canceled_at IS NULL "
                    "AND julianday('now')-julianday(created_at) > 30").fetchone()[0]
                return f"{n} esperando · {viejos} hace +30 días"
    except Exception as e:
        log.warning("sonda %s falló: %s", nombre, e)
    return None


def _commit_desplegado() -> str | None:
    try:
        raiz = Path(__file__).resolve().parent.parent
        r = subprocess.run(["git", "-C", str(raiz), "log", "--oneline", "-1"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except Exception:
        return None


def _resolver_sonda(sonda: str | None):
    """Devuelve (valor_legible, contradice) — contradice=True si el flag dice
    lo contrario de lo que el inventario afirma."""
    if not sonda:
        return None, False
    tipo, _, arg = sonda.partition(":")
    if tipo == "flag":
        v = _flag(arg)
        if v is None:
            # No está en config ni en el entorno → nunca se seteó, o sea corre
            # con su default (todos los flags gateados del proyecto son false).
            return f"{arg} sin definir → apagado por defecto", False
        return f"{arg} = {'ON' if v else 'OFF'}", v
    if tipo == "metric":
        return _metric(arg), False
    return None, False


def estado() -> dict:
    """Arma el mapa completo, con las sondas ya resueltas."""
    items = []
    for it in ITEMS:
        d = dict(it)
        valor, flag_on = _resolver_sonda(it.get("sonda"))
        d["sonda_valor"] = valor
        # Contradicción: el inventario dice apagado pero el flag está encendido,
        # o dice producción y el flag está apagado. Mostrarla en vez de taparla.
        if (it.get("sonda") or "").startswith("flag:"):
            if it["etapa"] == "desplegado_apagado" and flag_on:
                d["contradiccion"] = "el inventario dice apagado pero el flag está ON"
            elif it["etapa"] in ("produccion", "medido") and flag_on is False:
                d["contradiccion"] = "el inventario dice en producción pero el flag está OFF"
        items.append(d)

    por_etapa = {e: 0 for e in ETAPAS}
    for it in items:
        por_etapa[it["etapa"]] = por_etapa.get(it["etapa"], 0) + 1

    return {
        "items": items,
        "grupos": GRUPOS,
        "etapas": ETAPAS,
        "etapa_label": ETAPA_LABEL,
        "por_etapa": por_etapa,
        "total": len(items),
        "esperan_dueno": sum(1 for i in items if i["decide"] == "dueño"),
        "con_alerta": sum(1 for i in items if i.get("alerta")),
        "por_confirmar": sum(1 for i in items if i.get("confirmar")),
        "commit": _commit_desplegado(),
    }
