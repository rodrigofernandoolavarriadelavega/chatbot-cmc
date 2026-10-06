"""recuperacion.py — "Recuperar pacientes": segundo contacto A MANO desde recepción.

Decisión del dueño (5-oct-2026): por ahora NADA automático. Las personas que
llegaron por un anuncio o por la web y se cayeron (vieron horarios y no
agendaron, no respondieron, no asistieron, anularon) se listan acá, priorizadas,
y recepción decide a quién escribirle y cuándo, con confirmación, de a una.

Qué reutiliza (no duplica):
  - `campanas_meta_routes.kanban_data`: la clasificación de cada persona
    (vio horas / escribió / perdido / anuló o no asistió, cita futura, nombre,
    especialidad). Acá solo se filtra y se prioriza.
  - `campanas_seguimiento` + `campanas_seguimiento_hist`: el estado de gestión
    (Contactado, Volver a llamar...). Lo que marca recepción lo ve el dueño en
    la ficha de Campañas Meta y al revés.
  - `admin_routes.responder_como_recepcion`: el envío de texto libre (takeover
    + lock por teléfono), el mismo camino del panel y del embudo de ortodoncia.
  - `contact_budget.record_contact`: cada envío cuenta para el presupuesto de
    contacto unificado.

La recepción NO ve plata de Meta: esta capa solo devuelve campos de la
persona y de su gestión; nada de campaña, anuncio, gasto ni plataforma.

El carril automático (`persistencia.py`, flag PERSISTENCIA_ACTIVE) no se toca.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from session import db

log = logging.getLogger("recuperacion")

_CL = ZoneInfo("America/Santiago")

DIAS_LISTA = 30                 # misma ventana que el kanban del dueño
COOLDOWN_DIAS = 7               # no volver a escribirle antes de esto
SILENCIO_MIN_HORAS = 2          # no interrumpir una conversación en curso
RECEPCION_ACTIVA_HORAS = 24     # si recepción le escribió hace poco, es de recepción
MAX_TEXTO = 1000

TAG_OPT_OUT = "recuperacion_opt_out"            # botón "No, gracias" de la plantilla
TAGS_EXCLUIDOS = ("marketing_opt_out", TAG_OPT_OUT, "posible_numero_equivocado")
EVENTOS_NUMERO_EQUIVOCADO = ("numero_equivocado_reportado", "numero_equivocado_limpiado")
# Recordatorios ya enviados (de este carril manual, del automático apagado y
# de cualquier riel proactivo que registre contacto en el presupuesto unificado).
# `reenganche_enviado` NO está: es el primer toque automático del bot y la
# persona que lo recibió es justamente la que esta vista viene a rescatar.
EVENTOS_RECORDATORIO = ("recuperacion_enviado", "persistencia_toque2_enviado", "proactive_contact")
EVENTOS_RECEPCION = ("recepcionista_respondio", "auto_takeover_recep_reply")
GESTION_CERRADA = ("agendo_otra_via", "no_interesa")

PAYLOAD_AGENDAR = "recup_agendar"
PAYLOAD_NO_GRACIAS = "recup_no_gracias"

# situacion → plantilla (sin variables de salud: solo el nombre).
PLANTILLAS = {
    "vio_horas":     "recuperar_hora_pendiente_v1",
    "escribio":      "recuperar_consulta_abierta_v1",
    "sin_respuesta": "recuperar_consulta_abierta_v1",
    "no_asistio":    "recuperar_hora_no_concretada_v1",
    "anulo":         "recuperar_hora_no_concretada_v1",
}
_TPL_DIR = Path(__file__).resolve().parent.parent / "templates" / "whatsapp_templates"

SITUACIONES = {
    "no_asistio":    {"label": "No asistió a su hora", "rank": 0},
    "vio_horas":     {"label": "Vio horarios y no agendó", "rank": 1},
    "anulo":         {"label": "Anuló su hora", "rank": 2},
    "escribio":      {"label": "Escribió y no siguió", "rank": 3},
    "sin_respuesta": {"label": "Sin respuesta hace más de 7 días", "rank": 4},
}

# Especialidades que no se nombran en un mensaje al paciente (privacidad:
# un mensaje de WhatsApp lo puede leer cualquiera que tenga el teléfono).
_SENSIBLES = ("psiquiatr", "psicolog", "salud mental", "ginecolog", "matrona",
              "urolog", "sexolog", "adicc")


# ── Utilidades ──────────────────────────────────────────────────────────────

def _k(phone: str | None) -> str:
    p = (phone or "").strip()
    dig = "".join(ch for ch in p if ch.isdigit())
    if len(dig) >= 9 and not p.startswith(("fb_", "ig_")):
        return dig[-9:]
    return p


def _ts_utc(s: str | None) -> datetime | None:
    try:
        return datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _primer_nombre(nombre: str | None) -> str:
    p = (nombre or "").strip().split()
    return p[0].capitalize() if p else ""


def esp_visible(esp: str | None) -> str:
    """Especialidad apta para un mensaje al paciente; '' si es sensible."""
    e = (esp or "").strip()
    if not e or any(s in e.lower() for s in _SENSIBLES):
        return ""
    return e


def texto_sugerido(situacion: str, nombre: str | None, especialidad: str | None) -> str:
    """Texto libre editable (ventana de 24 h abierta). Sin emojis, sin voseo."""
    n = _primer_nombre(nombre)
    hola = f"Hola {n}, te escribimos" if n else "Hola, te escribimos"
    esp = esp_visible(especialidad)
    de = f" de {esp.lower()}" if esp else ""
    if situacion == "vio_horas":
        return (f"{hola} del Centro Médico Carampangue. Estuviste revisando horas{de} y quedó "
                "pendiente. ¿Sigues necesitando la hora? Si quieres, te ayudamos a reservarla por aquí mismo.")
    if situacion == "no_asistio":
        return (f"{hola} del Centro Médico Carampangue. Notamos que no pudiste asistir a tu hora. "
                "Si todavía la necesitas, podemos buscarte un nuevo horario. ¿Lo vemos?")
    if situacion == "anulo":
        return (f"{hola} del Centro Médico Carampangue. Vimos que tu hora quedó anulada. "
                "Si aún necesitas atención, podemos buscarte otro horario. ¿Te ayudamos?")
    return (f"{hola} del Centro Médico Carampangue. Nos consultaste por una hora y no alcanzamos a "
            "concretarla. ¿Aún la necesitas? Respóndenos por aquí y te ayudamos a agendarla.")


# ── Plantillas: ¿están aprobadas en Meta? ───────────────────────────────────

_TPL_TTL = 300
_tpl_cache: tuple[float, dict[str, str]] | None = None


async def _consultar_meta(nombres: list[str]) -> dict[str, str]:
    """{nombre: estado} desde la Graph API (solo lectura)."""
    from config import META_ACCESS_TOKEN, META_WABA_ID
    from messaging import _get_meta_client
    if not META_WABA_ID or not META_ACCESS_TOKEN:
        return {}
    out: dict[str, str] = {}
    client = _get_meta_client()
    for n in nombres:
        r = await client.get(f"https://graph.facebook.com/v22.0/{META_WABA_ID}/message_templates",
                             params={"name": n, "fields": "name,status"},
                             headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"}, timeout=8)
        if r.status_code != 200:
            continue
        for t in r.json().get("data", []):
            if t.get("name") == n:
                # APPROVED gana si hay varios idiomas con el mismo nombre
                if out.get(n) != "APPROVED":
                    out[n] = t.get("status") or ""
    return out


async def estado_plantillas(forzar: bool = False) -> dict[str, str]:
    """{nombre: 'APPROVED'|'PENDING'|'REJECTED'|...|'NO_SUBIDA'}. Cache 5 min.
    Si Meta no responde, nada se considera aprobado (falla cerrada)."""
    global _tpl_cache
    ahora = time.monotonic()
    if not forzar and _tpl_cache and ahora - _tpl_cache[0] < _TPL_TTL:
        return _tpl_cache[1]
    nombres = sorted(set(PLANTILLAS.values()))
    try:
        res = await _consultar_meta(nombres)
    except Exception as e:  # noqa: BLE001
        log.warning("recuperacion: no se pudo consultar plantillas a Meta: %s", e)
        return {n: "NO_VERIFICADA" for n in nombres}
    est = {n: res.get(n, "NO_SUBIDA") for n in nombres}
    _tpl_cache = (ahora, est)
    return est


def _cuerpo_plantilla(nombre: str, params: list[str], con_marca: bool = True) -> str:
    for fn in (f"{nombre}.json", f"{nombre}.DRAFT.json"):
        p = _TPL_DIR / fn
        if p.exists():
            try:
                comps = json.loads(p.read_text(encoding="utf-8")).get("components", [])
                body = next((c["text"] for c in comps if c.get("type") == "BODY"), "")
                for i, v in enumerate(params, 1):
                    body = body.replace("{{%d}}" % i, v)
                return f"[template: {nombre}]\n{body}" if con_marca else body
            except Exception:  # noqa: BLE001
                break
    return f"[template: {nombre}]" if con_marca else ""


# ── Exclusiones (consultas masivas por teléfono, una sola vez) ──────────────

def _bi_optouts() -> set[str] | None:
    """Últimos 9 dígitos de bi.opt_outs_marketing. None si BI no responde."""
    try:
        from winback import bi_conn
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT phone FROM bi.opt_outs_marketing")
                return {_k(r[0]) for r in cur.fetchall()}
    except Exception as e:  # noqa: BLE001
        log.warning("recuperacion: BI opt-outs no disponible: %s", e)
        return None


def _exclusiones(c, hoy: str) -> dict:
    """Conjuntos de claves (últimos 9 dígitos) a excluir, por motivo."""
    ex: dict[str, set] = {k: set() for k in
                          ("opt_out", "numero_equivocado", "recordatorio", "cita_futura", "recepcion_activa")}
    for r in c.execute("SELECT phone, tag FROM contact_tags WHERE tag IN (%s)" % ",".join("?" * len(TAGS_EXCLUIDOS)),
                       TAGS_EXCLUIDOS):
        (ex["numero_equivocado"] if r[1] == "posible_numero_equivocado" else ex["opt_out"]).add(_k(r[0]))
    try:
        for r in c.execute("SELECT phone FROM privacy_consents WHERE revoked_at IS NOT NULL"):
            ex["opt_out"].add(_k(r[0]))
    except Exception:  # noqa: BLE001 — tabla ausente en DB mínima
        pass
    for r in c.execute("SELECT DISTINCT phone FROM conversation_events WHERE event IN (%s)"
                       % ",".join("?" * len(EVENTOS_NUMERO_EQUIVOCADO)), EVENTOS_NUMERO_EQUIVOCADO):
        ex["numero_equivocado"].add(_k(r[0]))
    for r in c.execute("SELECT DISTINCT phone FROM conversation_events WHERE event IN (%s) "
                       "AND ts >= datetime('now', ?)" % ",".join("?" * len(EVENTOS_RECORDATORIO)),
                       (*EVENTOS_RECORDATORIO, f"-{COOLDOWN_DIAS} days")):
        ex["recordatorio"].add(_k(r[0]))
    for r in c.execute("SELECT DISTINCT phone FROM conversation_events WHERE event IN (%s) "
                       "AND ts >= datetime('now', ?)" % ",".join("?" * len(EVENTOS_RECEPCION)),
                       (*EVENTOS_RECEPCION, f"-{RECEPCION_ACTIVA_HORAS} hours")):
        ex["recepcion_activa"].add(_k(r[0]))
    # Cita futura vigente desde cualquier clic (no solo la creada tras el último)
    for r in c.execute("SELECT DISTINCT phone FROM citas_bot WHERE fecha >= ? "
                       "AND (cancel_detected_at IS NULL OR cancel_detected_at = '')", (hoy,)):
        ex["cita_futura"].add(_k(r[0]))
    return ex


def _ultimo_entrante(c, desde_utc: str) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for r in c.execute("SELECT phone, MAX(ts) FROM messages WHERE direction='in' AND ts >= ? GROUP BY phone",
                       (desde_utc,)):
        t = _ts_utc(r[1])
        if t:
            k = _k(r[0])
            out[k] = max(out[k], t) if k in out else t
    return out


# ── La lista ────────────────────────────────────────────────────────────────

def lista(dias: int = DIAS_LISTA, ahora: datetime | None = None, gestion: str | None = None) -> dict:
    """Personas recuperables, priorizadas. `gestion`: filtra por estado de gestión
    ('sin_gestion', 'contactado', 'volver_llamar'); None = todas las abiertas."""
    import campanas_meta_routes as cm
    ahora = ahora or datetime.now(_CL)
    dias = max(7, min(int(dias), 60))
    hoy = ahora.date().isoformat()
    ahora_utc = ahora.astimezone(timezone.utc)
    kd = cm.kanban_data((ahora.date() - timedelta(days=dias - 1)).isoformat(), hoy, canal="todos", ahora=ahora)

    bi = _bi_optouts()
    with db() as c:
        ex = _exclusiones(c, hoy)
        ult_in = _ultimo_entrante(c, (ahora_utc - timedelta(days=dias + 35)).strftime("%Y-%m-%d %H:%M:%S"))
    if bi:
        ex["opt_out"] |= bi

    personas, excl = [], {"cita_futura": 0, "opt_out": 0, "numero_equivocado": 0, "recordatorio": 0,
                          "conversacion_activa": 0, "gestion_cerrada": 0, "otro_canal": 0}
    for col in kd["columnas"]:
        if col["id"] not in ("vio_horas", "escribio", "perdido", "anulo"):
            continue
        for t in col["tarjetas"]:
            k = _k(t["phone"])
            if t["phone"].startswith(("fb_", "ig_")):
                excl["otro_canal"] += 1          # Messenger/Instagram: se atienden en el panel de conversaciones
                continue
            if t.get("proxima_cita") or k in ex["cita_futura"]:
                excl["cita_futura"] += 1
                continue
            if k in ex["numero_equivocado"]:
                excl["numero_equivocado"] += 1
                continue
            if k in ex["opt_out"]:
                excl["opt_out"] += 1
                continue
            if k in ex["recordatorio"]:
                excl["recordatorio"] += 1
                continue
            g = t.get("gestion") or {}
            estado_g = g.get("estado") or "sin_gestion"
            if estado_g in GESTION_CERRADA:
                excl["gestion_cerrada"] += 1
                continue
            entrante = ult_in.get(k)
            if k in ex["recepcion_activa"] or (entrante and ahora_utc - entrante < timedelta(hours=SILENCIO_MIN_HORAS)):
                excl["conversacion_activa"] += 1
                continue
            if gestion and estado_g != gestion:
                continue

            if col["id"] == "anulo":
                sit = "no_asistio" if t.get("desenlace") == "no_asistio" else "anulo"
            elif col["id"] == "perdido":
                sit = "sin_respuesta"
            else:
                sit = col["id"]
            try:
                llegada = datetime.strptime(t["llegada_iso"], "%Y-%m-%d %H:%M").replace(tzinfo=_CL)
            except (KeyError, ValueError):
                llegada = None
            ref = entrante or (llegada.astimezone(timezone.utc) if llegada else None)
            silencio = max(0, (ahora_utc - ref).days) if ref else None
            abierta = bool(entrante and ahora_utc - entrante < timedelta(hours=24))
            cierra_en = (round((24 * 3600 - (ahora_utc - entrante).total_seconds()) / 3600, 1)
                         if abierta else None)
            prioridad = ("alta" if sit == "no_asistio" or (sit == "vio_horas" and (silencio or 0) <= 3)
                         else "baja" if sit == "sin_respuesta" else "media")
            personas.append({
                "clave": t["clave"],
                "nombre": t.get("nombre") or "",
                "telefono": cm._telefono_completo(t["phone"]),
                "tel_href": cm._tel_href(t["phone"]),
                "origen": "Página web" if t.get("canal") == "web" else "Anuncio",
                "especialidad": t.get("especialidad") or "",
                "situacion": sit,
                "situacion_label": SITUACIONES[sit]["label"],
                "prioridad": prioridad,
                "dias": t.get("dias_etapa"),
                "dias_silencio": silencio,
                "escribio": (entrante.astimezone(_CL).strftime("%d/%m %H:%M") if entrante
                             else (llegada.strftime("%d/%m %H:%M") if llegada else "")),
                "ventana_abierta": abierta,
                "ventana_cierra_en_h": cierra_en,
                "gestion": {"estado": estado_g, "label": g.get("label") or "Sin gestionar",
                            "proximo": g.get("proximo_fmt") or "", "alerta": g.get("alerta")},
                "texto_sugerido": texto_sugerido(sit, t.get("nombre"), t.get("especialidad")),
                "plantilla": PLANTILLAS[sit],
                "plantilla_texto": _cuerpo_plantilla(PLANTILLAS[sit], [_primer_nombre(t.get("nombre")) or "Estimado/a paciente"],
                                                     con_marca=False),
                "_orden": (SITUACIONES[sit]["rank"], silencio if silencio is not None else 9999),
                "_phone": t["phone"],
            })
    with db() as c:
        segs = cm._seguimientos(c, {p["clave"] for p in personas})
    for p in personas:
        sg = segs.get(p["clave"]) or {}
        p["gestion"]["nota"] = sg.get("nota") or ""
        p["gestion"]["proximo_iso"] = (sg.get("proximo") or "")[:10]
    personas.sort(key=lambda p: p["_orden"])
    resumen = {s: sum(1 for p in personas if p["situacion"] == s) for s in SITUACIONES}
    return {
        "rango": kd["rango"],
        "total": len(personas),
        "resumen": resumen,
        "ventana_abierta": sum(1 for p in personas if p["ventana_abierta"]),
        "excluidos": excl,
        "bi_disponible": bi is not None,
        "personas": personas,
        "situaciones": {k: v["label"] for k, v in SITUACIONES.items()},
        "gestion_opciones": [g for g in cm.GESTION if g["id"] not in GESTION_CERRADA]
                            + [g for g in cm.GESTION if g["id"] in GESTION_CERRADA],
    }


def publica(d: dict) -> dict:
    """Quita los campos internos antes de responder por la API."""
    d = dict(d)
    d["personas"] = [{k: v for k, v in p.items() if not k.startswith("_")} for p in d["personas"]]
    return d


# ── Gestión y envío ─────────────────────────────────────────────────────────

class NoEnviable(Exception):
    """El envío no corresponde; `status` es el código HTTP sugerido."""
    def __init__(self, msg: str, status: int = 409):
        super().__init__(msg)
        self.status = status


def registrar_envio_en_gestion(clave: str, detalle: str, origen: str = "recepción · recuperar pacientes") -> None:
    """Marca 'Contactado' conservando nota y próxima fecha, y deja una línea en el
    historial POR CADA envío (la tabla de gestión es la misma de la ficha del dueño)."""
    ts = datetime.now(_CL).strftime("%Y-%m-%d %H:%M:%S")
    with db() as c:
        prev = c.execute("SELECT nota, proximo FROM campanas_seguimiento WHERE clave=?", (clave,)).fetchone()
        nota = (prev[0] if prev else None) or ""
        prox = (prev[1] if prev else None) or None
        c.execute("INSERT INTO campanas_seguimiento (clave, estado, nota, proximo, updated_at) "
                  "VALUES (?,?,?,?,?) ON CONFLICT(clave) DO UPDATE SET estado=excluded.estado, "
                  "updated_at=excluded.updated_at", (clave, "contactado", nota, prox, ts))
        c.execute("INSERT INTO campanas_seguimiento_hist (clave, ts, estado, nota, proximo, origen) "
                  "VALUES (?,?,?,?,?,?)", (clave, ts, "contactado", detalle, prox, origen))
        c.commit()


_locks: dict[str, asyncio.Lock] = {}


async def _cita_externa(phone: str) -> str:
    """'tiene' si Medilink muestra una cita futura (agendada por recepción o por
    teléfono, invisible para citas_bot). Cualquier otra cosa no bloquea."""
    try:
        from jobs import verificar_cita_externa
        return await verificar_cita_externa(phone)
    except Exception as e:  # noqa: BLE001
        log.warning("recuperacion: no se pudo verificar cita externa: %s", e)
        return "error"


async def enviar(clave: str, modo: str, mensaje: str | None, confirmar: bool,
                 ahora: datetime | None = None) -> dict:
    """Envía UN recordatorio a UNA persona. Todas las reglas se revalidan acá
    (no se confía en lo que mostró la pantalla). Lanza NoEnviable si no procede."""
    if confirmar is not True:
        raise NoEnviable("Falta la confirmación del envío.", 400)
    if modo not in ("texto", "plantilla"):
        raise NoEnviable("Modo de envío inválido.", 400)
    lock = _locks.setdefault(clave, asyncio.Lock())
    if lock.locked():
        raise NoEnviable("Ya se está enviando un mensaje a esta persona.")
    async with lock:
        d = await asyncio.to_thread(lista, DIAS_LISTA, ahora)
        p = next((x for x in d["personas"] if x["clave"] == clave), None)
        if not p:
            raise NoEnviable("Esta persona ya no está en la lista: agendó, pidió no recibir mensajes, "
                             "ya se le envió un recordatorio en los últimos 7 días o está marcada como cerrada.")
        if not d["bi_disponible"]:
            raise NoEnviable("No se pudo verificar la lista de personas que no quieren recibir mensajes. "
                             "Intenta de nuevo en unos minutos.", 503)
        phone = p["_phone"]
        if await _cita_externa(phone) == "tiene":
            raise NoEnviable("Esta persona ya tiene una hora agendada en Medilink.")

        from session import log_event, log_message, get_session
        sit = p["situacion"]
        if modo == "texto":
            if not p["ventana_abierta"]:
                raise NoEnviable("Pasaron más de 24 h desde el último mensaje de esta persona: "
                                 "solo se le puede enviar la plantilla aprobada.")
            texto = (mensaje or "").strip()
            if not texto:
                raise NoEnviable("El mensaje está vacío.", 400)
            if len(texto) > MAX_TEXTO:
                raise NoEnviable(f"El mensaje no puede pasar de {MAX_TEXTO} caracteres.", 400)
            from admin_routes import responder_como_recepcion
            await responder_como_recepcion(phone, texto, exigir_entrega=True)
            plantilla, enviado = None, texto
        else:
            if p["ventana_abierta"]:
                raise NoEnviable("La ventana de 24 h está abierta: envía un texto, no la plantilla.")
            plantilla = p["plantilla"]
            estado = (await estado_plantillas()).get(plantilla)
            if estado != "APPROVED":
                raise NoEnviable("Falta la aprobación de Meta para esta plantilla. Mientras tanto, llama a la persona.")
            from messaging import send_whatsapp_template
            nombre = _primer_nombre(p["nombre"])
            wamid = await send_whatsapp_template(phone, plantilla, [nombre],
                                                 button_payloads=[PAYLOAD_AGENDAR, PAYLOAD_NO_GRACIAS])
            if not wamid:
                raise NoEnviable("WhatsApp no aceptó la plantilla. Intenta de nuevo o llama a la persona.", 502)
            enviado = _cuerpo_plantilla(plantilla, [nombre])
            log_message(phone, "out", enviado, (get_session(phone) or {}).get("state", "IDLE"), wamid=wamid)

        meta = {"clave": clave, "modo": modo, "plantilla": plantilla, "situacion": sit,
                "mensaje": (enviado or "")[:300]}
        log_event(phone, "recuperacion_enviado", meta)
        try:
            from contact_budget import record_contact
            record_contact(phone, "recuperacion", {"modo": modo, "situacion": sit})
        except Exception as e:  # noqa: BLE001
            log.warning("recuperacion: record_contact falló: %s", e)
        detalle = ("Recordatorio enviado por WhatsApp (texto libre): " + (enviado or "")[:200] if modo == "texto"
                   else f"Recordatorio enviado por WhatsApp (plantilla {plantilla})")
        registrar_envio_en_gestion(clave, detalle)
        return {"ok": True, "modo": modo, "plantilla": plantilla, "estado_gestion": "contactado"}


# ── Respuesta del paciente a los botones de la plantilla ────────────────────

def procesar_boton(phone: str, payload: str) -> str:
    """Se llama desde el webhook cuando la persona toca un botón de la plantilla.
    'No, gracias' registra la baja de este tipo de mensajes (tag propio, cierra
    la gestión y deja el evento); no toca otros opt-outs del paciente.
    Devuelve el texto con que el bot debe seguir procesando el mensaje."""
    from session import log_event, save_tag
    if payload == PAYLOAD_NO_GRACIAS:
        save_tag(phone, TAG_OPT_OUT)
        log_event(phone, "recuperacion_optout", {"payload": payload})
        try:
            import campanas_meta_routes as cm
            cm.guardar_seguimiento(cm._clave(phone), "no_interesa",
                                   "Pidió no recibir más recordatorios (botón No, gracias de la plantilla).",
                                   None, origen="paciente · botón")
        except Exception as e:  # noqa: BLE001 — no era una persona de anuncio/web: la baja igual quedó
            log.debug("recuperacion: sin ficha de gestión para %s: %s", phone[-4:], e)
        return PAYLOAD_NO_GRACIAS
    log_event(phone, "recuperacion_acepto", {"payload": payload})
    return "menu"
