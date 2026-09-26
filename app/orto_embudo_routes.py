# -*- coding: utf-8 -*-
"""Router /alma/api/orto-embudo — Embudo de ortodoncia ANTES de la instalacion.

POR QUE EXISTE (y por que no alcanzaba el modulo que ya habia)
--------------------------------------------------------------
`ortodoncia_routes.py` sigue a los pacientes YA instalados: controles vencidos,
avance del tratamiento, plan de pago. Empieza donde termina este.

Javiera Burgos pidio el 2026-09-01 el tramo anterior, que no existia: *"uno que
tenga el proceso inicial, tratamiento previo, venta de cupones, radiografia
recibida, enviada a Dani, respuesta de Dani, respuesta a pcte, agendado para
instalacion... ahora que ha aumentado el flujo se podria hacer uno... ahora lo
estoy haciendo con mi mente nomas"*.

El riesgo concreto: la Dra. Castillo vive en Concepcion y el ida y vuelta de
radiografias pasa por WhatsApp. Un paciente que se cae entre "enviada a Dani" y
"respuesta a paciente" no lo nota nadie, porque el unico registro esta en la
cabeza de una persona. Las dos etapas de espera externa son las fragiles: por
eso `dias_en_etapa` y el semaforo de atraso son el corazon del modulo, no un
adorno.

Tabla: orto_embudo (sessions.db). Auth: token de ortodoncia o admin.
"""
from __future__ import annotations

import csv
import html as _html
import json
import io
import logging
import os
import smtplib
import threading
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request, Cookie
from fastapi.responses import StreamingResponse

from session import db, log_event

log = logging.getLogger("orto_embudo_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/orto-embudo", tags=["orto-embudo"])

# Las 8 etapas son textuales de Javiera, en su orden. `espera` marca las que
# dependen de un tercero (la Dra. Castillo) — ahi es donde se cae la gente.
# `alerta` = dias despues de los cuales la tarjeta se pone en rojo.
ETAPAS = [
    # Etapa 0, medida sobre 114 conversaciones reales (2026-09-01): 114 preguntan
    # por ortodoncia y solo 38 agendan. De los 92 que se caen, el 98% deja de
    # responder despues de que el bot le explica el protocolo — y hoy no existen
    # en ninguna parte porque el tablero empezaba cuando el paciente ya llegaba
    # al box. Esta columna la llena el barrido `sincronizar`, no una persona.
    {"id": "consulto",            "label": "Consultó, no agendó", "alerta": 3,  "espera": False, "auto": True},
    {"id": "proceso_inicial",     "label": "Proceso inicial",       "alerta": 7,  "espera": False},
    {"id": "tratamiento_previo",  "label": "Tratamiento previo",    "alerta": 30, "espera": False},
    {"id": "venta_cupones",       "label": "Venta de cupones",      "alerta": 10, "espera": False},
    {"id": "rx_recibida",         "label": "Radiografía recibida",  "alerta": 5,  "espera": False},
    {"id": "enviada_dani",        "label": "Enviada a Dani",        "alerta": 4,  "espera": True},
    {"id": "respuesta_dani",      "label": "Respuesta de Dani",     "alerta": 3,  "espera": False},
    {"id": "respuesta_paciente",  "label": "Respuesta a paciente",  "alerta": 5,  "espera": True},
    {"id": "agendado",            "label": "Agendado instalación",  "alerta": 30, "espera": False},
]
FINALES = [
    # Columna aparte a proposito. Un paciente en control mensual NO puede entrar
    # a las etapas del embudo: el contador lo marcaria "detenido" a los pocos
    # dias y las alertas dejarian de significar algo. Pero tiene que VERSE, o
    # Javiera termina mirando dos tableros. Por eso: visible, fuera de los KPIs
    # de proceso, y sin limite de dias (alerta 0 = nunca se pone en rojo).
    {"id": "en_tratamiento", "label": "En tratamiento", "alerta": 0, "espera": False},
    {"id": "instalado",  "label": "Instalado",  "alerta": 0, "espera": False},
    {"id": "descartado", "label": "No sigue",   "alerta": 0, "espera": False},
]
_TODAS = {e["id"]: e for e in ETAPAS + FINALES}
_ORDEN = [e["id"] for e in ETAPAS] + [e["id"] for e in FINALES]


def _ahora() -> str:
    return datetime.now(_CHILE_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _crear_tabla() -> None:
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS orto_embudo (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                paciente     TEXT NOT NULL,
                telefono     TEXT,
                rut          TEXT,
                etapa        TEXT NOT NULL DEFAULT 'proceso_inicial',
                etapa_desde  TEXT NOT NULL,
                notas        TEXT,
                examenes     TEXT,
                origen       TEXT,
                phone        TEXT,
                consulta_txt TEXT,
                valor_cupones INTEGER DEFAULT 0,
                historial    TEXT,
                creado_por   TEXT,
                created_at   TEXT,
                updated_at   TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_orto_embudo_etapa ON orto_embudo(etapa, etapa_desde)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS orto_embudo_config (
                clave TEXT PRIMARY KEY,
                valor TEXT
            )""")
        c.commit()


_crear_tabla()

# La tabla pudo nacer sin `examenes` (se desplego antes que esta funcion).
with db() as _c:
    _cols = [r[1] for r in _c.execute("PRAGMA table_info(orto_embudo)")]
    for _col in ("examenes", "origen", "phone", "consulta_txt"):
        if _col not in _cols:
            _c.execute(f"ALTER TABLE orto_embudo ADD COLUMN {_col} TEXT")
            log.info("orto_embudo: columna %s agregada", _col)
    _c.commit()


# ── Aviso por correo ────────────────────────────────────────────────────────
# El equipo ya se coordina por correo, asi que el aviso entra por el canal que
# ya usan en vez de inventar uno nuevo. Los destinatarios viven en la BD (no en
# el codigo) para poder cambiarlos sin un deploy.
#
# PRIVACIDAD: el correo lleva SOLO nombre y etapa. Nada clinico, ningun RUT,
# ningun telefono. Es un aviso de coordinacion, no una ficha (Ley 21.719).
# La solicitud de examenes NO se inventa: se siembra del catalogo real del
# convenio (`vales_routes.PRESTACIONES`), donde el "Set radiologico ortodoncia"
# ya viene definido y con precio como bitewing + panoramica + teleradiografia.
# Javiera edita la lista desde la pagina; esto es solo el punto de partida.
_EXAMENES_SEED = [
    {"n": "Set radiológico ortodoncia (bitewing + panorámica + teleradiografía)", "d": 1},
    {"n": "Fotografías clínicas (intraorales y extraorales)", "d": 1},
    {"n": "Modelos de estudio", "d": 1},
    {"n": "Radiografía Panorámica", "d": 0},
    {"n": "Teleradiografía de perfil", "d": 0},
    {"n": "Escaneo intraoral digital — ambos maxilares", "d": 0},
    {"n": "CONE BEAM Bimaxilar", "d": 0},
]
_DEFAULTS = {"mails": "", "avisar_alta": "1", "avisar_dani": "1",
             "examenes": json.dumps(_EXAMENES_SEED, ensure_ascii=False)}


def _examenes_catalogo() -> list:
    try:
        v = json.loads(_cfg().get("examenes") or "[]")
        return v if isinstance(v, list) and v else list(_EXAMENES_SEED)
    except Exception:
        return list(_EXAMENES_SEED)


def _cfg() -> dict:
    with db() as c:
        filas = dict(c.execute("SELECT clave, valor FROM orto_embudo_config"))
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in filas.items() if v is not None})
    return out


def _cfg_set(d: dict) -> None:
    with db() as c:
        for k, v in d.items():
            if k in _DEFAULTS:
                c.execute("INSERT INTO orto_embudo_config(clave,valor) VALUES (?,?) "
                          "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor", (k, str(v)))
        c.commit()


def _destinatarios() -> list[str]:
    raw = _cfg().get("mails") or ""
    return [x.strip() for x in raw.replace(";", ",").split(",") if "@" in x.strip()]


def _enviar(asunto: str, cuerpo_html: str, para: list[str]) -> None:
    """Envia por el Gmail del centro. Corre en un hilo: si el correo falla o
    tarda, la anotacion de Javiera ya quedo guardada igual."""
    user = os.getenv("GMAIL_CMC_USER", "")
    pwd = os.getenv("GMAIL_CMC_APP_PASSWORD", "")
    if not (user and pwd and para):
        log.info("orto_embudo: aviso omitido (sin credencial o sin destinatarios)")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"] = f"Centro Medico Carampangue <{user}>"
    msg["To"] = ", ".join(para)
    msg.attach(MIMEText(cuerpo_html, "html", "utf-8"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=25) as sv:
            sv.starttls()
            sv.login(user, pwd)
            sv.sendmail(user, para, msg.as_string())
        log.info("orto_embudo: aviso enviado a %d destinatarios", len(para))
    except Exception as e:
        log.warning("orto_embudo: no se pudo enviar el aviso: %s", e)


def _avisar(asunto: str, titulo: str, lineas: list, pie: str,
            examenes: list | None = None) -> None:
    para = _destinatarios()
    if not para:
        return
    bloque = ""
    if examenes:
        items = "".join(f'<li style="margin:4px 0">{_html.escape(x)}</li>' for x in examenes)
        bloque = (f'<div style="margin:18px 0 0;padding:14px 16px;background:#F1F8FA;'
                  f'border-left:3px solid #4FBECE;border-radius:0 8px 8px 0">'
                  f'<div style="font-size:12px;letter-spacing:.12em;text-transform:uppercase;'
                  f'color:#1172AB;font-weight:700">Solicitud de examenes</div>'
                  f'<ul style="margin:9px 0 0;padding-left:18px;color:#0F3F68;font-size:14px">'
                  f'{items}</ul></div>')
    filas = "".join(
        f'<tr><td style="padding:5px 14px 5px 0;color:#6b8095;font-size:13px">{_html.escape(k)}</td>'
        f'<td style="padding:5px 0;color:#16324f;font-size:14px;font-weight:600">{_html.escape(v)}</td></tr>'
        for k, v in lineas)
    cuerpo = f"""<div style="font-family:Helvetica,Arial,sans-serif;max-width:520px">
<h2 style="color:#16324f;font-size:17px;margin:0 0 4px">{_html.escape(titulo)}</h2>
<p style="color:#6b8095;font-size:13px;margin:0 0 14px">Embudo de Ortodoncia &middot; Centro Medico Carampangue</p>
<table style="border-collapse:collapse">{filas}</table>{bloque}
<p style="color:#6b8095;font-size:12px;margin:16px 0 0;line-height:1.6">{_html.escape(pie)}</p>
<p style="color:#9aabbd;font-size:11px;margin:14px 0 0;line-height:1.5">
Aviso automatico de coordinacion. No contiene informacion clinica.</p></div>"""
    threading.Thread(target=_enviar, args=(asunto, cuerpo, para), daemon=True).start()


# Palabras con las que un paciente nombra la ortodoncia. Salieron de leer las
# 114 conversaciones reales, no de imaginarlas.
_KW_ORTO = ["ortodonc", "braket", "bracket", "frenillo", "ortodoncista", "invisalign"]


def _sincronizar_consultas(dias: int = 60) -> dict:
    """Mete al embudo a quien pregunto por ortodoncia por WhatsApp y no agendo.

    Tres filtros, y el segundo es el que importa:
      1. Preguntó en los ultimos `dias`.
      2. NO tiene cita de Ortodoncia. Esto ademas saca la post-venta: quien ya
         esta en tratamiento y escribe porque "se le solto un bracket" tiene
         cita previa, asi que no ensucia la columna de captacion (eran 10 de
         las 114 consultas).
      3. No esta ya en el embudo (por telefono).
    No dispara correos: escribe directo, sin pasar por `crear`.
    """
    like = " OR ".join(f"lower(m.text) LIKE '%{k}%'" for k in _KW_ORTO)
    creados, revisados = 0, 0
    with db() as c:
        ya = {(r[0] or "").strip() for r in c.execute(
            "SELECT phone FROM orto_embudo WHERE phone IS NOT NULL")}
        con_cita = {r[0] for r in c.execute(
            "SELECT DISTINCT phone FROM citas_bot WHERE especialidad='Ortodoncia'")}
        filas = list(c.execute(
            f"SELECT m.phone, MIN(m.ts), MIN(m.text) FROM messages m "
            f"WHERE m.direction='in' AND ({like}) "
            f"AND m.ts >= datetime('now', ?) GROUP BY m.phone", (f"-{int(dias)} days",)))
        for phone, ts, txt in filas:
            revisados += 1
            if not phone or phone in con_cita or phone in ya:
                continue
            nombre = None
            r = list(c.execute("SELECT paciente_nombre FROM citas_bot WHERE phone=? "
                               "AND paciente_nombre IS NOT NULL ORDER BY id DESC LIMIT 1", (phone,)))
            if r:
                nombre = r[0][0]
            if not nombre:
                r = list(c.execute("SELECT nombre FROM contact_profiles WHERE phone=?", (phone,)))
                nombre = (r[0][0] if r and r[0][0] else None)
            nombre = (nombre or "").strip() or f"Sin nombre · {phone[-4:]}"
            c.execute("INSERT INTO orto_embudo(paciente,telefono,etapa,etapa_desde,origen,phone,"
                      "consulta_txt,historial,creado_por,created_at,updated_at) "
                      "VALUES (?,?,'consulto',?,'bot',?,?,?,'sync',?,?)",
                      (nombre, phone, (ts or _ahora())[:19], phone,
                       (txt or "").strip()[:300],
                       f"{(ts or _ahora())[:19]} consulto por WhatsApp", _ahora(), _ahora()))
            creados += 1
        c.commit()
    log.info("orto_embudo sync: %d creados de %d consultas revisadas", creados, revisados)
    return {"creados": creados, "revisados": revisados, "dias": dias}


# ── Avance automático por evidencia real ─────────────────────────────────────
# Las etapas que dejan rastro en los sistemas no dependen de la memoria de
# Javiera: una atención con la dentista general, un tratamiento cobrado o una
# hora con la ortodoncista ya dicen dónde va el paciente. Solo AVANZA (nunca
# retrocede ni pisa lo que alguien movió a mano más allá) y deja escrito en el
# historial qué evidencia usó. Las etapas del medio (radiografía, Dani,
# respuesta) no tienen fuente: siguen siendo manuales.
_DENT_GENERAL = (55, 72)     # Burgos, Jiménez
_ORTODONCISTA = 66           # Castillo
_EVALUACION_MAX = 15000      # la evaluación cuesta $15.000; más que eso es tratamiento
_INSTALACION_MIN = 80000     # misma frontera que ortodoncia_routes


def _pids_por_telefono(tails: set[str]) -> dict[str, set[int]]:
    """Pacientes Medilink por los últimos 9 dígitos del celular: ficha del BI
    (dim_paciente) + citas que agendó el bot desde ese número."""
    out: dict[str, set[int]] = {t: set() for t in tails}
    try:
        from main import _bi_pool
        pool = _bi_pool()
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT paciente_id, telefono FROM bi.dim_paciente WHERE telefono IS NOT NULL")
                for pid, tel in cur.fetchall():
                    d = "".join(ch for ch in tel if ch.isdigit())[-9:]
                    if d in out:
                        out[d].add(int(pid))
        finally:
            pool.putconn(conn)
    except Exception as e:
        log.warning("orto_embudo: BI no disponible para identidad (%s)", e)
    with db() as c:
        for t in tails:
            for r in c.execute("SELECT DISTINCT id_paciente_medilink FROM citas_bot WHERE phone LIKE ? "
                               "AND id_paciente_medilink IS NOT NULL", ("%" + t,)):
                out[t].add(int(r[0]))
    return out


def _etapa_por_evidencia(c, pids: set[int], desde: str, hoy: str) -> tuple[str, str] | None:
    """(etapa, motivo) más avanzada que prueban los datos, o None."""
    if not pids:
        return None
    q = ",".join("?" * len(pids))
    orto = list(c.execute(
        f"SELECT fecha, monto FROM bi_pagos_caja WHERE id_paciente IN ({q}) AND id_profesional=? "
        f"AND fecha>=? UNION ALL SELECT fecha, total FROM bi_atenciones WHERE id_paciente IN ({q}) "
        f"AND id_profesional=? AND fecha>=?", (*pids, _ORTODONCISTA, desde, *pids, _ORTODONCISTA, desde)))
    inst = sorted(f for f, m in orto if (m or 0) >= _INSTALACION_MIN)
    if inst:
        return "instalado", f"instalación con la ortodoncista el {inst[0]}"
    ctrl = sorted(f for f, m in orto if 0 < (m or 0) < _INSTALACION_MIN)
    previo = list(c.execute(f"SELECT MIN(fecha) FROM bi_pagos_caja WHERE id_paciente IN ({q}) "
                            f"AND id_profesional=? AND fecha<?", (*pids, _ORTODONCISTA, desde)))[0][0]
    if ctrl and previo:
        # Ya se atendía con la ortodoncista ANTES de escribir: es post-venta
        # (un bracket suelto, o la mamá preguntando por el hijo en control).
        return "en_tratamiento", f"en control con la ortodoncista desde el {previo}"
    if ctrl:
        # Primera visita con la ortodoncista sin instalación todavía: fue a la
        # evaluación. No hay etapa exacta para eso; queda como agendado.
        return "agendado", f"evaluación con la ortodoncista el {ctrl[0]}"
    fut = list(c.execute(f"SELECT MIN(fecha) FROM citas_cache WHERE id_paciente IN ({q}) AND id_prof=? "
                         f"AND fecha>=?", (*pids, _ORTODONCISTA, hoy)))[0][0]
    if fut:
        return "agendado", f"hora con la ortodoncista el {fut}"
    # Pack de radiografías de ortodoncia (cupones Imagendent). Es específico de
    # ortodoncia, así que vale aunque el celular sea compartido.
    cup = list(c.execute(
        f"SELECT fecha, realizado, pagado, prestacion FROM convenio_consumo WHERE id_paciente IN ({q}) "
        f"AND fecha>=? AND (slug='set_ortodoncia' OR lower(prestacion) LIKE '%ortodon%') ORDER BY fecha",
        (*pids, desde)))
    if any(r[1] for r in cup):
        f = next(r[0] for r in cup if r[1])
        return "rx_recibida", f"radiografías de ortodoncia tomadas el {f}"
    if any((r[2] or 0) > 0 for r in cup):
        f = next(r[0] for r in cup if (r[2] or 0) > 0)
        return "venta_cupones", f"pagó el pack de radiografías el {f}"
    if len(pids) > 1:
        # Celular compartido (mamá que agenda para el hijo): la dentista general
        # no prueba nada de ESTE paciente. La ortodoncista sí, por eso va arriba.
        return None
    q2 = ",".join("?" * len(_DENT_GENERAL))
    gen = list(c.execute(
        f"SELECT fecha, monto FROM bi_pagos_caja WHERE id_paciente IN ({q}) AND id_profesional IN ({q2}) "
        f"AND fecha>=? UNION ALL SELECT fecha, total FROM bi_atenciones WHERE id_paciente IN ({q}) "
        f"AND id_profesional IN ({q2}) AND fecha>=?", (*pids, *_DENT_GENERAL, desde, *pids, *_DENT_GENERAL, desde)))
    trat = sorted(f for f, m in gen if (m or 0) > _EVALUACION_MAX)
    if trat:
        return "tratamiento_previo", f"tratamiento con la dentista general el {trat[0]}"
    if gen:
        return "proceso_inicial", f"atención con la dentista general el {min(f for f, _ in gen)}"
    return None


def _avanzar_por_evidencia(aplicar: bool = True) -> dict:
    hoy = _ahora()[:10]
    with db() as c:
        filas = list(c.execute("SELECT id, paciente, phone, telefono, etapa, etapa_desde, historial "
                               "FROM orto_embudo WHERE etapa IN (%s)" % ",".join("?" * len(_ORDEN)), _ORDEN))
    tails = {}
    for f in filas:
        d = "".join(ch for ch in (f[2] or f[3] or "") if ch.isdigit())
        if len(d) >= 9:
            tails[f[0]] = d[-9:]
    por_tel = _pids_por_telefono(set(tails.values()))
    movidos, cambios = 0, []
    with db() as c:
        for pid, nombre, _, _, etapa, desde, hist in filas:
            if pid not in tails:
                continue
            # la evidencia vale desde que consultó (primera línea del historial)
            ini = min((hist or desde or "")[:10] or desde[:10], (desde or "")[:10])
            r = _etapa_por_evidencia(c, por_tel[tails[pid]], ini, hoy)
            if not r:
                continue
            nueva, motivo = r
            if _ORDEN.index(nueva) <= _ORDEN.index(etapa) or etapa in ("descartado",):
                continue
            cambios.append({"id": pid, "paciente": nombre, "de": etapa, "a": nueva, "motivo": motivo})
            if aplicar:
                h = (hist or "") + f"\n{_ahora()} {etapa} → {nueva} (automático: {motivo})"
                c.execute("UPDATE orto_embudo SET etapa=?, etapa_desde=?, historial=?, updated_at=? "
                          "WHERE id=?", (nueva, _ahora(), h[-4000:], _ahora(), pid))
                movidos += 1
        if aplicar:
            c.commit()
    if movidos:
        log_event(None, "orto_embudo_avance_auto", {"movidos": movidos})
    return {"movidos": movidos, "cambios": cambios}


def _tokens_nombre(s: str) -> set:
    """Tokens significativos de un nombre, ya normalizados (sin tildes)."""
    from email_ticker import _normalizar_nombre
    return {t for t in _normalizar_nombre(s).split() if len(t) >= 3}


def _calza_nombre(a: str, b: str) -> bool:
    """Compara el nombre del correo con el del embudo.

    NO se reusa `email_ticker._nombres_coinciden` a proposito: ese acepta
    contencion ("juan" in "juan ignacio garrido"), y su propio comentario
    aclara que es seguro PORQUE alli el match ya viene acotado por
    profesional + fecha + hora. Aca no hay ese acotamiento — se compara
    contra todo el embudo — asi que la contencion haria calzar a cualquier
    Juan con cualquier otro. Se exige coincidencia fuerte.
    """
    from email_ticker import _normalizar_nombre
    na, nb = _normalizar_nombre(a), _normalizar_nombre(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = _tokens_nombre(a), _tokens_nombre(b)
    if len(ta) < 2 or len(tb) < 2:
        return False       # con un solo token no hay evidencia suficiente
    comunes = ta & tb
    if len(comunes) < 2:
        return False       # al menos nombre + un apellido
    corto, largo = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return corto <= largo  # el nombre corto tiene que estar entero en el largo


# Etapas desde las que tiene sentido saltar a "agendado".
_ANTES_DE_AGENDADO = [e["id"] for e in ETAPAS if e["id"] != "agendado"]


def _sugerencias_agendados() -> list:
    """Quien del embudo YA tiene hora con la ortodoncista, sin mover nada.

    Dentalink manda un correo por cada cita y `email_ticker` lo deja parseado
    con paciente, fecha, hora y profesional. Aca solo se cruza por nombre y se
    PROPONE: mover queda a un clic de Javiera.

    Se sugiere en vez de mover solo porque el cruce es por texto contra todo el
    embudo, sin acotar por fecha ni profesional. Un cruce equivocado moveria al
    paciente que no era y nadie se enteraria; asi, si esta mal, ella lo ve.
    Un nombre que calza con MAS DE UNO no se sugiere: mejor callar que adivinar.
    """
    with db() as c:
        try:
            citas = list(c.execute(
                "SELECT paciente_nombre, fecha_cita, hora_cita, id_cita FROM email_ticker "
                "WHERE tipo='agendada' AND lower(profesional_nombre) LIKE '%castillo%' "
                "AND fecha_cita >= date('now','-7 days') ORDER BY fecha_cita"))
        except Exception as e:
            log.warning("orto_embudo: email_ticker no disponible: %s", e)
            return []
        pend = list(c.execute(
            "SELECT id, paciente, etapa FROM orto_embudo WHERE etapa IN (%s)"
            % ",".join("?" * len(_ANTES_DE_AGENDADO)), _ANTES_DE_AGENDADO))

    out, usados = [], set()
    for nombre, fecha, hora, id_cita in citas:
        cands = [p for p in pend if p[0] not in usados and _calza_nombre(nombre, p[1])]
        if len(cands) != 1:
            continue
        pid, pnom, etapa = cands[0]
        usados.add(pid)
        out.append({"id": pid, "paciente": pnom, "nombre_cita": nombre,
                    "etapa_actual": _TODAS.get(etapa, {}).get("label", etapa),
                    "fecha": fecha, "hora": hora, "id_cita": id_cita})
    return out


def _auth(request: Request, token: str | None, cmc_session: str | None) -> str:
    from admin_routes import _verify_cookie, _is_admin_token
    from config import ADMIN_TOKEN, ORTODONCIA_TOKEN

    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        tk = auth_header.split(None, 1)[1].strip()
        if _is_admin_token(tk) or (ORTODONCIA_TOKEN and tk == ORTODONCIA_TOKEN):
            return tk
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia", "administracion"):
            return ADMIN_TOKEN
    if token and (_is_admin_token(token) or (ORTODONCIA_TOKEN and token == ORTODONCIA_TOKEN)):
        return token
    raise HTTPException(status_code=401, detail="Token invalido")


def _dias(desde: str) -> int:
    try:
        d0 = datetime.strptime(desde[:10], "%Y-%m-%d").date()
        return (datetime.now(_CHILE_TZ).date() - d0).days
    except Exception:
        return 0


def _fila(r) -> dict:
    etapa = r[4] if r[4] in _TODAS else "proceso_inicial"
    meta = _TODAS[etapa]
    d = _dias(r[5])
    return {
        "id": r[0], "paciente": r[1], "telefono": r[2], "rut": r[3],
        "etapa": etapa, "etapa_label": meta["label"], "etapa_desde": r[5][:10],
        "dias_en_etapa": d, "espera_externa": meta["espera"],
        "atrasado": bool(meta["alerta"]) and d > meta["alerta"],
        "limite": meta["alerta"],
        "notas": r[6], "valor_cupones": r[7] or 0,
        "origen": (r[8] if len(r) > 8 else None) or "manual",
        "consulta": (r[9] if len(r) > 9 else None),
    }


_SEL = ("SELECT id,paciente,telefono,rut,etapa,etapa_desde,notas,valor_cupones,"
        "origen,consulta_txt FROM orto_embudo")


@router.get("/tablero")
def tablero(request: Request, incluir_finales: int = Query(0),
            token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    """El tablero completo, una columna por etapa."""
    _auth(request, token, cmc_session)
    with db() as c:
        filas = [_fila(r) for r in c.execute(_SEL + " ORDER BY etapa_desde")]
    _FUERA = ("en_tratamiento", "instalado", "descartado")
    activos = [f for f in filas if f["etapa"] not in _FUERA]
    cols = []
    # Las terminales no van en el tablero. "En tratamiento" en particular vive
    # en su propia pestaña, leyendo EN VIVO el modulo de Ortodoncia que ya
    # existe (dias sin control, estado y saldo calculados desde el BI) — mucho
    # mejor dato que copiarlo aca desde un cache que no sincroniza desde marzo.
    for e in ETAPAS + (FINALES if incluir_finales else []):
        ps = [f for f in filas if f["etapa"] == e["id"]]
        ps.sort(key=lambda x: -x["dias_en_etapa"])
        cols.append({"id": e["id"], "label": e["label"], "espera": e["espera"],
                     "limite": e["alerta"], "n": len(ps), "pacientes": ps})
    atrasados = [f for f in activos if f["atrasado"]]
    return {
        "columnas": cols,
        "total_activos": len(activos),
        "atrasados": len(atrasados),
        "esperando_dani": len([f for f in activos if f["etapa"] == "enviada_dani"]),
        "esperando_paciente": len([f for f in activos if f["etapa"] == "respuesta_paciente"]),
        "valor_cupones": sum(f["valor_cupones"] for f in activos),
        "en_tratamiento": len([f for f in filas if f["etapa"] == "en_tratamiento"]),
        "peor": sorted(atrasados, key=lambda x: -x["dias_en_etapa"])[:5],
    }


@router.post("")
async def crear(request: Request, token: str | None = Query(None),
                cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    b = await request.json()
    nombre = (b.get("paciente") or "").strip()
    if not nombre:
        raise HTTPException(400, "Falta el nombre del paciente")
    etapa = b.get("etapa") if b.get("etapa") in _TODAS else "proceso_inicial"
    with db() as c:
        exs = [x.strip() for x in (b.get("examenes") or []) if str(x).strip()]
        c.execute("INSERT INTO orto_embudo(paciente,telefono,rut,etapa,etapa_desde,notas,"
                  "valor_cupones,examenes,historial,creado_por,created_at,updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (nombre, (b.get("telefono") or "").strip() or None,
                   (b.get("rut") or "").strip() or None, etapa, _ahora(),
                   b.get("notas"), int(b.get("valor_cupones") or 0),
                   json.dumps(exs, ensure_ascii=False) if exs else None,
                   f"{_ahora()} creado en {etapa}", b.get("creado_por") or "recepcion",
                   _ahora(), _ahora()))
        pid = list(c.execute("SELECT last_insert_rowid()"))[0][0]
        c.commit()
    log_event(None, "orto_embudo_alta", {"paciente": nombre, "etapa": etapa})
    if _cfg().get("avisar_alta") == "1":
        lineas = [("Paciente", nombre), ("Etapa", _TODAS[etapa]["label"]),
                  ("Fecha", _ahora()[:16])]
        _avisar(f"Ortodoncia · paciente nuevo: {nombre}",
                "Entro un paciente nuevo al embudo", lineas,
                "Queda registrado en el embudo de ortodoncia de Alma.",
                examenes=exs)
    return {"ok": True, "id": pid}


@router.patch("/{pid}")
async def editar(pid: int, request: Request, token: str | None = Query(None),
                 cmc_session: str | None = Cookie(None)):
    """Mover de etapa o corregir datos.

    Al cambiar de etapa se reinicia `etapa_desde` — es lo que hace que el
    contador de dias signifique "cuanto lleva esperando ESTO", que es la
    pregunta que Javiera hoy responde de memoria.
    """
    _auth(request, token, cmc_session)
    b = await request.json()
    with db() as c:
        row = list(c.execute("SELECT etapa,historial FROM orto_embudo WHERE id=?", (pid,)))
        if not row:
            raise HTTPException(404, "No existe ese paciente en el embudo")
        if "etapa" in b:
            nueva = b["etapa"]
            if nueva not in _TODAS:
                raise HTTPException(400, "Etapa desconocida")
            if nueva != row[0][0]:
                hist = (row[0][1] or "") + f"\n{_ahora()} {row[0][0]} → {nueva}"
                c.execute("UPDATE orto_embudo SET etapa=?,etapa_desde=?,historial=?,updated_at=? WHERE id=?",
                          (nueva, _ahora(), hist[-4000:], _ahora(), pid))
                if nueva == "enviada_dani" and _cfg().get("avisar_dani") == "1":
                    nom = list(c.execute("SELECT paciente FROM orto_embudo WHERE id=?", (pid,)))[0][0]
                    _avisar(f"Ortodoncia · radiografia enviada: {nom}",
                            "Se envio una radiografia para evaluacion",
                            [("Paciente", nom), ("Enviada", _ahora()[:16])],
                            "Queda esperando respuesta. Si pasan mas de 4 dias, el embudo la marca en rojo.")
        for campo in ("paciente", "telefono", "rut", "notas"):
            if campo in b:
                c.execute(f"UPDATE orto_embudo SET {campo}=?,updated_at=? WHERE id=?",
                          (b[campo], _ahora(), pid))
        if "valor_cupones" in b:
            c.execute("UPDATE orto_embudo SET valor_cupones=?,updated_at=? WHERE id=?",
                      (int(b["valor_cupones"] or 0), _ahora(), pid))
        c.commit()
    return {"ok": True}


@router.delete("/{pid}")
def borrar(pid: int, request: Request, token: str | None = Query(None),
           cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    with db() as c:
        c.execute("DELETE FROM orto_embudo WHERE id=?", (pid,))
        c.commit()
    return {"ok": True}


@router.get("/etapas")
def etapas(request: Request, token: str | None = Query(None),
           cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    return {"etapas": ETAPAS, "finales": FINALES, "orden": _ORDEN}


@router.get("/paciente/{pid}")
def detalle(pid: int, request: Request, token: str | None = Query(None),
            cmc_session: str | None = Cookie(None)):
    """Ficha completa con su linea de tiempo.

    `historial` se guarda como texto append-only ("<ts> <de> -> <a>") porque el
    volumen es chico y asi sobrevive a cualquier cambio de esquema. Se parsea
    aca para la vista en vez de guardar filas: si manana cambian las etapas, el
    historial viejo se sigue leyendo igual.
    """
    _auth(request, token, cmc_session)
    with db() as c:
        r = list(c.execute(
            "SELECT id,paciente,telefono,rut,etapa,etapa_desde,notas,valor_cupones,"
            "historial,created_at FROM orto_embudo WHERE id=?", (pid,)))
    if not r:
        raise HTTPException(404, "No existe")
    row = r[0]
    pasos = []
    for linea in (row[8] or "").strip().split("\n"):
        linea = linea.strip()
        if not linea:
            continue
        ts, _, resto = linea.partition(" ")
        if len(ts) == 10 and " " in linea:
            ts, resto = linea[:19], linea[20:]
        if "→" in resto:
            de, _, a = resto.partition("→")
            pasos.append({"ts": ts, "de": _TODAS.get(de.strip(), {}).get("label", de.strip()),
                          "a": _TODAS.get(a.strip(), {}).get("label", a.strip())})
        else:
            pasos.append({"ts": ts, "de": None, "a": resto.strip()})
    base = _fila(row[:8])
    base["creado"] = (row[9] or "")[:16]
    base["pasos"] = pasos
    base["orden"] = _ORDEN.index(base["etapa"]) + 1 if base["etapa"] in _ORDEN else 0
    base["total_etapas"] = len(ETAPAS)
    return base


# ── Actividad real del paciente (citas, pagos, cupones) ──────────────────────
_PROF_DENTAL = {55: "Dra. Javiera Burgos", 72: "Dr. Carlos Jiménez", 66: "Dra. Daniela Castillo",
                75: "Dr. Fernando Fredes", 69: "Dra. Aurora Valdés"}


@router.get("/paciente/{pid}/actividad")
def actividad(pid: int, request: Request, token: str | None = Query(None),
              cmc_session: str | None = Cookie(None)):
    """Lo que dejó rastro en los sistemas: atenciones y pagos del área dental,
    cupones del pack de radiografías y horas futuras. Solo odontología: con un
    celular compartido no se muestra la medicina general del resto de la familia."""
    _auth(request, token, cmc_session)
    phone, _ = _phone_de(pid)
    tail = phone[-9:]
    pids = _pids_por_telefono({tail})[tail]
    if not pids:
        return {"eventos": [], "pacientes": 0}
    q = ",".join("?" * len(pids))
    qp = ",".join("?" * len(_PROF_DENTAL))
    ev = []
    with db() as c:
        nombres = {r[0]: r[1] for r in c.execute(
            f"SELECT id_paciente, MAX(nombre_paciente) FROM bi_pagos_caja WHERE id_paciente IN ({q}) "
            f"GROUP BY id_paciente", tuple(pids)) if r[1]}
        for r in c.execute(f"SELECT fecha, id_profesional, monto, metodo_pago, id_paciente FROM bi_pagos_caja "
                           f"WHERE id_paciente IN ({q}) AND id_profesional IN ({qp})",
                           (*pids, *_PROF_DENTAL)):
            ev.append({"fecha": r[0], "tipo": "pago", "prof": _PROF_DENTAL[r[1]], "monto": r[2],
                       "detalle": r[3] or "", "pac": r[4]})
        pagadas = {(e["fecha"], e["prof"]) for e in ev}
        for r in c.execute(f"SELECT fecha, id_profesional, total, id_paciente FROM bi_atenciones "
                           f"WHERE id_paciente IN ({q}) AND id_profesional IN ({qp})",
                           (*pids, *_PROF_DENTAL)):
            if (r[0], _PROF_DENTAL[r[1]]) not in pagadas:
                ev.append({"fecha": r[0], "tipo": "atencion", "prof": _PROF_DENTAL[r[1]],
                           "monto": r[2] or 0, "detalle": "sin pago ese día", "pac": r[3]})
        for r in c.execute(f"SELECT fecha, prestacion, unidades, pagado, realizado, id_paciente "
                           f"FROM convenio_consumo WHERE id_paciente IN ({q})", tuple(pids)):
            ev.append({"fecha": r[0], "tipo": "cupones", "prof": "Imagendent", "monto": r[3] or 0,
                       "detalle": f"{r[1]} · {r[2]} {'cupón' if r[2] == 1 else 'cupones'}"
                                  + (" · tomadas" if r[4] else " · pendientes"), "pac": r[5]})
        for r in c.execute(f"SELECT fecha, hora_inicio, id_prof, id_paciente FROM citas_cache "
                           f"WHERE id_paciente IN ({q}) AND id_prof IN ({qp}) AND fecha>=?",
                           (*pids, *_PROF_DENTAL, _ahora()[:10])):
            ev.append({"fecha": r[0], "tipo": "hora", "prof": _PROF_DENTAL[r[2]], "monto": 0,
                       "detalle": f"agendada a las {(r[1] or '')[:5]}", "pac": r[3]})
    for e in ev:
        dueno = e.pop("pac")
        e["paciente"] = nombres.get(dueno, "") if len(pids) > 1 else ""
    ev.sort(key=lambda e: e["fecha"], reverse=True)
    return {"eventos": ev[:80], "pacientes": len(pids),
            # el pack de cupones también entra a caja como pago: no se suma dos veces
            "total_pagado": sum(e["monto"] for e in ev if e["tipo"] == "pago")}


# ── Conversación de WhatsApp desde la tarjeta ────────────────────────────────
# El token de ortodoncia circula por WhatsApp: estos endpoints NO reciben un
# teléfono, reciben el id de la tarjeta y el teléfono sale de la fila. Así solo
# se puede leer o escribir a pacientes que ya están en el embudo, nunca a
# cualquier conversación del bot.

def _phone_de(pid: int) -> tuple[str, str]:
    with db() as c:
        r = list(c.execute("SELECT phone, telefono, paciente FROM orto_embudo WHERE id=?", (pid,)))
    if not r:
        raise HTTPException(404, "No existe")
    phone = "".join(ch for ch in (r[0][0] or r[0][1] or "") if ch.isdigit())
    if len(phone) == 9 and phone.startswith("9"):
        phone = "56" + phone
    if len(phone) < 11:
        raise HTTPException(400, "La tarjeta no tiene un celular valido")
    return phone, r[0][2]


def _ventana_abierta(phone: str) -> tuple[bool, str | None]:
    """WhatsApp solo deja escribir texto libre hasta 24 h despues del ultimo
    mensaje DEL paciente. Meta igual devuelve wamid fuera de ventana y el
    rechazo llega despues por webhook, asi que hay que mirarlo antes."""
    with db() as c:
        r = list(c.execute("SELECT MAX(ts) FROM messages WHERE phone=? AND direction='in'", (phone,)))
        ult = r[0][0] if r else None
        if not ult:
            return False, None
        ok = list(c.execute("SELECT ? >= datetime('now','-24 hours')", (ult,)))[0][0]
    return bool(ok), ult


@router.get("/conversacion/{pid}")
def conversacion(pid: int, request: Request, token: str | None = Query(None),
                 cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    from session import get_messages, get_session
    phone, nombre = _phone_de(pid)
    abierta, ult_in = _ventana_abierta(phone)
    msgs = [{"id": m["id"], "dir": m["direction"], "texto": m["text"] or "",
             "ts": m["ts"], "media": m.get("media_tipo")}
            for m in get_messages(phone, limit=150)]
    return {"paciente": nombre, "telefono": phone, "mensajes": msgs,
            "ventana_abierta": abierta, "ultimo_del_paciente": ult_in,
            "estado_bot": (get_session(phone) or {}).get("state", "IDLE")}


@router.post("/conversacion/{pid}")
async def conversacion_responder(pid: int, request: Request, token: str | None = Query(None),
                                 cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    texto = ((await request.json()).get("mensaje") or "").strip()
    if not texto:
        raise HTTPException(400, "Mensaje vacio")
    if len(texto) > 4000:
        raise HTTPException(400, "Mensaje demasiado largo")
    phone, _ = _phone_de(pid)
    abierta, _ = _ventana_abierta(phone)
    if not abierta:
        raise HTTPException(409, "Pasaron mas de 24 h desde el ultimo mensaje del paciente: "
                                 "WhatsApp no permite escribirle texto libre.")
    from admin_routes import responder_como_recepcion
    r = await responder_como_recepcion(phone, texto, exigir_entrega=True)
    log_event(phone, "orto_embudo_respuesta", {"tarjeta": pid, "mensaje": texto[:200]})
    return r


@router.get("/sugerencias")
def sugerencias(request: Request, token: str | None = Query(None),
                cmc_session: str | None = Cookie(None)):
    """Pacientes del embudo que ya tienen hora con la ortodoncista."""
    _auth(request, token, cmc_session)
    return {"items": _sugerencias_agendados()}


@router.post("/sugerencias/{pid}/aplicar")
def aplicar_sugerencia(pid: int, request: Request, token: str | None = Query(None),
                       cmc_session: str | None = Cookie(None)):
    """Mueve a 'Agendado instalacion' dejando escrito de donde salio el dato."""
    _auth(request, token, cmc_session)
    sug = [s for s in _sugerencias_agendados() if s["id"] == pid]
    if not sug:
        raise HTTPException(404, "Esa sugerencia ya no aplica")
    g = sug[0]
    with db() as c:
        r = list(c.execute("SELECT etapa, historial FROM orto_embudo WHERE id=?", (pid,)))
        if not r:
            raise HTTPException(404, "No existe")
        hist = (r[0][1] or "") + (
            f"\n{_ahora()} {r[0][0]} \u2192 agendado "
            f"(desde el correo: hora con la ortodoncista el {g['fecha']} {g['hora']})")
        c.execute("UPDATE orto_embudo SET etapa='agendado',etapa_desde=?,historial=?,updated_at=? "
                  "WHERE id=?", (_ahora(), hist[-4000:], _ahora(), pid))
        c.commit()
    log_event(None, "orto_embudo_sugerencia_aplicada",
              {"paciente": g["paciente"], "fecha": g["fecha"]})
    return {"ok": True, **g}


@router.post("/sincronizar")
def sincronizar(request: Request, dias: int = Query(60, ge=1, le=365),
                token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    entrada = _sincronizar_consultas(dias)
    avance = _avanzar_por_evidencia()
    return {"ok": True, **entrada, "avanzados": avance["movidos"],
            "sugerencias": len(_sugerencias_agendados())}


@router.get("/examenes")
def examenes_get(request: Request, token: str | None = Query(None),
                 cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    return {"items": _examenes_catalogo()}


@router.post("/examenes")
async def examenes_set(request: Request, token: str | None = Query(None),
                       cmc_session: str | None = Cookie(None)):
    """Guarda cual es 'lo que se pide siempre'. Solo Javiera lo sabe."""
    _auth(request, token, cmc_session)
    b = await request.json()
    items = [{"n": str(x.get("n", "")).strip(), "d": 1 if x.get("d") else 0}
             for x in (b.get("items") or []) if str(x.get("n", "")).strip()]
    if not items:
        raise HTTPException(400, "La lista no puede quedar vacia")
    _cfg_set({"examenes": json.dumps(items, ensure_ascii=False)})
    return {"ok": True, "items": items}


@router.get("/config")
def config_get(request: Request, token: str | None = Query(None),
               cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    c = _cfg()
    return {"mails": c.get("mails", ""), "avisar_alta": c.get("avisar_alta") == "1",
            "avisar_dani": c.get("avisar_dani") == "1",
            "hay_credencial": bool(os.getenv("GMAIL_CMC_USER")),
            "destinatarios": _destinatarios()}


@router.post("/config")
async def config_set(request: Request, token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    b = await request.json()
    _cfg_set({"mails": (b.get("mails") or "").strip(),
              "avisar_alta": "1" if b.get("avisar_alta") else "0",
              "avisar_dani": "1" if b.get("avisar_dani") else "0"})
    return {"ok": True, "destinatarios": _destinatarios()}


@router.post("/config/probar")
def config_probar(request: Request, token: str | None = Query(None),
                  cmc_session: str | None = Cookie(None)):
    """Correo de prueba, para confirmar que llega antes de confiar en el."""
    _auth(request, token, cmc_session)
    para = _destinatarios()
    if not para:
        raise HTTPException(400, "No hay correos configurados")
    _avisar("Ortodoncia · correo de prueba", "Prueba de aviso",
            [("Estado", "Si te llego esto, los avisos funcionan"),
             ("Enviado", _ahora()[:16])],
            "Puedes ignorar este correo.")
    return {"ok": True, "enviados_a": para}


@router.get("/export")
def export(request: Request, token: str | None = Query(None),
           cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    with db() as c:
        filas = [_fila(r) for r in c.execute(_SEL + " ORDER BY etapa, etapa_desde")]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Paciente", "Telefono", "RUT", "Etapa", "Desde", "Dias en etapa",
                "Atrasado", "Valor cupones", "Notas"])
    for f in filas:
        w.writerow([f["paciente"], f["telefono"] or "", f["rut"] or "", f["etapa_label"],
                    f["etapa_desde"], f["dias_en_etapa"], "SI" if f["atrasado"] else "",
                    f["valor_cupones"], (f["notas"] or "").replace("\n", " ")])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="embudo-ortodoncia.csv"'})
