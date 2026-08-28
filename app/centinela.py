"""centinela.py — Resumen diario de fallas silenciosas al dueño.

Nace 2026-08-05: esa semana CUATRO fallas graves (foto de abono con 500 en
cada intento, correo bancario descartado por el parser, falso "cancelada por
el profesional", cita duplicada bot↔recepción) vivieron días en producción
sin que nadie las viera, porque cada una fallaba EN SILENCIO: el error
quedaba en un log que nadie lee y el sistema seguía reportando "ok". Este
job las hace gritar: una vez al día barre log + DB + Medilink y manda UN
mensaje de WhatsApp al dueño con lo que encontró (o "sin hallazgos" en una
línea, para que la ausencia de alertas también sea señal).

Solo LEE — no repara nada. Apagable con CENTINELA_ACTIVE=false en .env.
"""

import asyncio
import logging
import os
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("centinela")

_CL = ZoneInfo("America/Santiago")
_LOG_PATHS = ("/var/log/cmc-bot.log", "/var/log/cmc-bot.log.1")

# Profesionales donde VARIAS citas futuras del mismo paciente son normales
# (series de kine, controles de ortodoncia/psico, masoterapia). Para el resto
# (médicos de consulta), 2+ citas activas del mismo paciente = sospecha de
# duplicado (caso Alexander 2026-08-05: 3 citas activas en 2 fichas).
_PROFS_SERIE_OK = {21, 49, 52, 55, 56, 59, 66, 70, 72, 74, 75, 76, 77}

# Tope de páginas por día al barrer la agenda (50 citas/página). 20 = 1.000
# citas/día, muy por encima de cualquier día real del CMC; existe solo para
# que un `next` en bucle no cuelgue el centinela.
_MAX_PAGINAS_DIA = 20


def _activo() -> bool:
    return os.getenv("CENTINELA_ACTIVE", "true").strip().lower() in ("1", "true", "yes")


def _scan_logs_sync(cutoff_utc: str) -> dict:
    """Barre los logs por líneas >= cutoff (prefijo 'YYYY-MM-DD HH:MM:SS' UTC).
    Corre en thread (los archivos pueden pesar >100MB tras semanas sin rotar)."""
    res = {"webhook_500": 0, "meta_400": 0, "tracebacks": 0,
           "traceback_sitios": Counter(), "meta_400_ejemplo": ""}
    for path in _LOG_PATHS:
        p = Path(path)
        try:
            if not p.exists():
                continue
            # Si el archivo no se ha tocado desde antes del corte, no aporta.
            mtime = datetime.utcfromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            if mtime < cutoff_utc:
                continue
            esperando_frame = False
            with p.open("r", errors="ignore") as f:
                for line in f:
                    ts = line[:19]
                    tiene_ts = len(ts) == 19 and ts[4] == "-" and ts[10] == " "
                    if esperando_frame and 'File "/opt/chatbot-cmc/app' in line:
                        sitio = line.strip().split('"')[1].rsplit("/", 1)[-1]
                        try:
                            sitio += ":" + line.split("line ")[1].split(",")[0]
                        except Exception:
                            pass
                        res["traceback_sitios"][sitio] += 1
                        esperando_frame = False
                        continue
                    if tiene_ts and ts < cutoff_utc:
                        continue
                    if 'POST /webhook HTTP/1.1" 500' in line:
                        res["webhook_500"] += 1
                    elif "Meta API 400" in line and tiene_ts:
                        res["meta_400"] += 1
                        if not res["meta_400_ejemplo"]:
                            res["meta_400_ejemplo"] = line.strip()[:160]
                    elif "Traceback (most recent call last)" in line:
                        res["tracebacks"] += 1
                        esperando_frame = True
        except Exception as e:
            log.warning("centinela: no se pudo leer %s: %s", path, e)
    return res


def _revisar_abonos() -> tuple[list, list]:
    """(transferencias sin dueño con abonos contemporáneos, vencidos 48h).

    Una entrada POR TRANSFERENCIA (no por pendiente): el primer dry-run hizo
    producto cartesiano — 2 transferencias × todos los pendientes del mismo
    monto de la historia = 15 pares de ruido. Candidato = pendiente creado a
    ±48h del correo (contemporáneo al pago)."""
    from session import db
    from abono_transferencia import _parse_ts_flexible
    ahora = datetime.now(_CL)
    with db() as c:
        pendientes = [dict(r) for r in c.execute(
            "SELECT id, paciente_nombre, monto, especialidad, creado_at, expira_at "
            "FROM abono_pendientes WHERE estado='pendiente'")]
        transf = [dict(r) for r in c.execute(
            "SELECT id, monto, nombre_pagador, banco, email_ts "
            "FROM transferencias_banco WHERE estado_match='sin_match'")]
    plata_sin_dueno = []
    for t in transf:
        ts = _parse_ts_flexible(t.get("email_ts") or "")
        if not ts or (ahora - ts) > timedelta(hours=48):
            continue
        candidatos = []
        for a in pendientes:
            if a["monto"] != t["monto"]:
                continue
            creado = _parse_ts_flexible(a.get("creado_at") or "")
            if creado and abs((creado - ts).total_seconds()) <= 48 * 3600:
                candidatos.append(a["paciente_nombre"])
        if candidatos:
            plata_sin_dueno.append((t, candidatos))
    vencidos = []
    for a in pendientes:
        exp = _parse_ts_flexible(a.get("expira_at") or "")
        if exp and exp < ahora and (ahora - exp) <= timedelta(hours=48):
            vencidos.append(a)
    return plata_sin_dueno, vencidos


async def _revisar_duplicados_medilink(dias: int = 5) -> list:
    """Citas ACTIVAS del mismo paciente, mismo profesional y MISMO DÍA dentro de
    los próximos `dias` — sospecha de duplicado bot↔recepción."""
    import json
    import urllib.parse
    from medilink import _get_shared_client, MEDILINK_BASE_URL, HEADERS
    client = _get_shared_client()
    grupos: dict[tuple, list] = {}
    dias_sin_revisar = []
    hoy = datetime.now(_CL).date()
    for i in range(dias):
        fecha = (hoy + timedelta(days=i)).strftime("%Y-%m-%d")
        q = json.dumps({"id_sucursal": {"eq": 1}, "fecha": {"eq": fecha},
                        "estado_anulacion": {"eq": 0}})
        url = f"{MEDILINK_BASE_URL}/citas?q={urllib.parse.quote(q)}"
        try:
            # Medilink corta en 50 por página y NO avisa que hay más: solo deja
            # un `links.next`. Leer únicamente la primera página dejaba ciego al
            # centinela en los días cargados — caso Isidora 2026-08-28: 75 citas
            # ese día, 2 de sus 3 horas duplicadas caían en la página 2 y el
            # reporte salió en CERO con el duplicado a la vista en la agenda.
            paginas = 0
            while url and paginas < _MAX_PAGINAS_DIA:
                r = await client.get(url, headers=HEADERS)
                if r.status_code != 200:
                    # Un centinela que salta días EN SILENCIO replica la clase de
                    # falla que vino a cazar — los días no revisados se reportan.
                    dias_sin_revisar.append(fecha)
                    break
                cuerpo = r.json()
                for cita in (cuerpo.get("data") or []):
                    prof = cita.get("id_profesional")
                    if prof in _PROFS_SERIE_OK:
                        continue
                    # Criterio (Rodrigo, 2026-08-28): duplicado = mismo paciente
                    # + mismo profesional + MISMO DÍA, igual que el bloqueo del bot
                    # en flows.py. Cruzar los 5 días marcaba como duplicado un
                    # control legítimo con el mismo médico otro día, y una alerta
                    # con falsos positivos deja de leerse.
                    key = (cita.get("id_paciente"), prof, cita.get("fecha"))
                    grupos.setdefault(key, []).append(cita)
                url = (cuerpo.get("links") or {}).get("next")
                paginas += 1
                if url:
                    await asyncio.sleep(0.3)
            else:
                # Salimos por tope de páginas con `next` todavía pendiente: el día
                # quedó a medio leer. Se reporta como no revisado en vez de darlo
                # por limpio — misma regla que el fallo HTTP de arriba.
                if url:
                    dias_sin_revisar.append(fecha)
        except Exception as e:
            log.debug("centinela duplicados fecha %s: %s", fecha, e)
            dias_sin_revisar.append(fecha)
        await asyncio.sleep(0.3)
    dups = []
    if dias_sin_revisar:
        dups.append({"paciente": "", "profesional": "",
                     "sin_revisar": dias_sin_revisar})
    for (_pac, _prof, _fecha), citas in grupos.items():
        if len(citas) > 1:
            c0 = citas[0]
            dups.append({
                "paciente": (c0.get("nombre_paciente") or "").strip(),
                "profesional": (c0.get("nombre_profesional") or "").strip(),
                "fecha": _fecha,
                # Llave estable para no repetir la misma alerta cada hora.
                "key": f"{_pac}-{_prof}-{_fecha}",
                "horas": sorted(str(c.get("hora_inicio"))[:5] for c in citas),
                "citas": [f"{c.get('fecha')} {str(c.get('hora_inicio'))[:5]}"
                          for c in citas],
            })
    return dups


async def job_centinela_diario():
    """Cron 07:30 CL. Arma el resumen y lo manda al dueño por WhatsApp."""
    from config import ADMIN_ALERT_PHONE
    from session import log_event
    if not _activo() or not ADMIN_ALERT_PHONE:
        return
    from medilink import use_batch_lane
    use_batch_lane()

    cutoff_utc = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    secciones = []

    try:
        logs = await asyncio.to_thread(_scan_logs_sync, cutoff_utc)
        if logs["webhook_500"]:
            secciones.append(f"🔴 *{logs['webhook_500']} error(es) 500 en el webhook* — "
                             "pacientes cuyo mensaje se perdió sin respuesta")
        if logs["meta_400"]:
            secciones.append(f"🟠 {logs['meta_400']} mensaje(s) rechazados por Meta (400) — "
                             f"ej: {logs['meta_400_ejemplo']}")
        if logs["tracebacks"]:
            top = ", ".join(f"{s} ×{n}" for s, n in logs["traceback_sitios"].most_common(3))
            secciones.append(f"🟠 {logs['tracebacks']} excepción(es) en el código"
                             + (f" — focos: {top}" if top else ""))
    except Exception as e:
        secciones.append(f"⚠️ No pude barrer los logs: {e}")

    try:
        con_plata, vencidos = _revisar_abonos()
        for a, t in con_plata:
            secciones.append(
                f"💰 *Abono #{a['id']} ({a['paciente_nombre']}, ${a['monto']:,}) sigue "
                f"pendiente y HAY transferencia sin dueño por el mismo monto* "
                f"({t['nombre_pagador']}, {t['banco']}) → revisar /alma/comprobantes".replace(",", "."))
        if vencidos:
            nombres = ", ".join(f"{a['paciente_nombre']} ({a['especialidad']})"
                                for a in vencidos[:5])
            secciones.append(f"⏰ {len(vencidos)} abono(s) vencidos sin pago (48h): {nombres}"
                             + (" …" if len(vencidos) > 5 else ""))
    except Exception as e:
        secciones.append(f"⚠️ No pude revisar abonos: {e}")

    try:
        dups = await _revisar_duplicados_medilink()
        for d in dups:
            secciones.append(f"👥 *Posible duplicado:* {d['paciente']} tiene "
                             f"{len(d['citas'])} citas con {d['profesional']}: "
                             + " · ".join(d["citas"]))
    except Exception as e:
        secciones.append(f"⚠️ No pude revisar duplicados en Medilink: {e}")

    hoy_str = datetime.now(_CL).strftime("%d-%m")
    if secciones:
        msg = f"🛰️ *Centinela {hoy_str}* — {len(secciones)} hallazgo(s):\n\n" \
              + "\n\n".join(f"• {s}" for s in secciones)
    else:
        msg = f"🛰️ Centinela {hoy_str}: sin hallazgos. Webhook, abonos y agenda limpios."

    try:
        from messaging import send_whatsapp
        await send_whatsapp(ADMIN_ALERT_PHONE, msg[:3900])
        log_event("centinela", "centinela_diario", {"hallazgos": len(secciones)})
        log.info("Centinela diario: %d hallazgo(s) enviados al dueño", len(secciones))
    except Exception as e:
        log.error("Centinela: no se pudo enviar el resumen: %s", e)


# ── Duplicados EN CALIENTE ────────────────────────────────────────────────────
# El centinela diario avisa a las 07:30 del día siguiente. Caso Isidora
# (2026-08-28): las dos horas duplicadas se crearon a las 13:19 y 13:57 del día
# anterior — el aviso habría llegado 17 h tarde, con el bloque ya cerrado y las
# tres horas ocupadas. Este job corre DENTRO del horario de atención y le habla
# a RECEPCIÓN: es quien crea el duplicado a mano durante un takeover (el bot ya
# se bloquea solo en flows.py) y la única que puede deshacerlo a tiempo.
_INTRADIA_DEDUP_HORAS = 12


def _intradia_activo() -> bool:
    return os.getenv("CENTINELA_INTRADIA_ACTIVE", "true").strip().lower() in ("1", "true", "yes")


def _destinos_recepcion() -> tuple[list[str], bool]:
    """Números de recepción desde `RECEPCION_ALERT_PHONES` (CSV, formato 569…).

    Si no está configurado cae al dueño, pero el mensaje LO DICE: un fallback
    silencioso se vuelve permanente por olvido y el aviso nunca llega a quien
    tiene que actuar.
    """
    raw = os.getenv("RECEPCION_ALERT_PHONES", "").strip()
    destinos = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    if destinos:
        return destinos, False
    from config import ADMIN_ALERT_PHONE
    return ([ADMIN_ALERT_PHONE] if ADMIN_ALERT_PHONE else []), True


def _ya_alertado(key: str, horas: int = _INTRADIA_DEDUP_HORAS) -> bool:
    """¿Ya avisamos este duplicado? Evita repetir la misma alerta cada hora
    mientras recepción todavía no la resuelve."""
    from session import db
    try:
        with db() as conn:
            cur = conn.execute(
                "SELECT 1 FROM conversation_events "
                "WHERE phone = 'centinela' AND event = 'dup_intradia_alertado' "
                "  AND meta LIKE ? AND ts > datetime('now', ?) LIMIT 1",
                (f'%"{key}"%', f"-{int(horas)} hours"),
            )
            return cur.fetchone() is not None
    except Exception as e:
        # Sin historial preferimos repetir el aviso antes que callarlo: una
        # alerta repetida molesta, una perdida cuesta una hora de agenda.
        log.warning("centinela intradía: dedup no disponible (%s) — se envía igual", e)
        return False


async def job_centinela_duplicados_intradia():
    """Cron horario en horario de atención: horas duplicadas del MISMO día (hoy
    y mañana) mientras todavía se pueden anular. Avisa a recepción."""
    from session import log_event
    if not _activo() or not _intradia_activo():
        return
    from medilink import use_batch_lane
    use_batch_lane()

    try:
        dups = await _revisar_duplicados_medilink(dias=2)
    except Exception as e:
        log.error("centinela intradía: no pude revisar duplicados: %s", e)
        return

    # Los días que no se pudieron leer (429, tope de páginas) quedan en el log;
    # el resumen de las 07:30 los reporta. Alertarlos cada hora sería ruido.
    for d in dups:
        if d.get("sin_revisar"):
            log.warning("centinela intradía: días sin revisar %s", d["sin_revisar"])

    nuevos = [d for d in dups if d.get("key") and not _ya_alertado(d["key"])]
    if not nuevos:
        return

    lineas = []
    for d in nuevos:
        f = d.get("fecha") or ""
        f_disp = f"{f[8:10]}-{f[5:7]}" if len(f) == 10 else f
        lineas.append(f"• *{d['paciente']}* tiene {len(d['horas'])} horas el {f_disp} "
                      f"con {d['profesional']}: {' · '.join(d['horas'])}")
    msg = ("⚠️ *Horas duplicadas en la agenda*\n\n" + "\n".join(lineas)
           + "\n\nFavor confirmar con el paciente cuál hora queda y anular la otra.")

    destinos, es_fallback = _destinos_recepcion()
    if es_fallback:
        msg += ("\n\n_Este aviso debería llegarle a recepción: falta configurar "
                "RECEPCION_ALERT_PHONES en el servidor._")

    enviados = 0
    from messaging import send_whatsapp
    for tel in destinos:
        try:
            if await send_whatsapp(tel, msg[:3900]):
                enviados += 1
        except Exception as e:
            log.warning("centinela intradía: WhatsApp a %s falló: %s", tel, e)
    if not enviados:
        # Fuera de la ventana de 24h de Meta el texto libre se rechaza (400). El
        # aviso no se pierde: sale por Telegram, que no tiene esa restricción.
        try:
            from alertas_oob import enviar_telegram
            await enviar_telegram(msg, header="🛰️ Centinela — horas duplicadas")
        except Exception as e:
            log.error("centinela intradía: tampoco pude avisar por Telegram: %s", e)

    for d in nuevos:
        log_event("centinela", "dup_intradia_alertado", {
            "key": d["key"], "paciente": d["paciente"],
            "horas": d["horas"], "destinos_ok": enviados,
        })
    log.info("Centinela intradía: %d duplicado(s) nuevo(s), %d destino(s) OK",
             len(nuevos), enviados)
