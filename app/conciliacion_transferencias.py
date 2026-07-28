"""conciliacion_transferencias.py — Conciliación transferencias × correos banco.

Pregunta de negocio: ¿cuánto entró REALMENTE por transferencia y dónde están
las diferencias? Cruza dos fuentes independientes:

  1. `pagos_cmc` con `metodo_pago='transferencia'` — lo que RECEPCIÓN anotó
     como pagado por transferencia (ver `docs/LIBRO_DE_LA_VERDAD.md`: el
     medio de pago SIEMPRE sale de `pagos_cmc.metodo_pago`, nunca de
     `bi_pagos_caja.metodo_pago`, que es 99,7% "Efectivo" por default).
  2. `transferencias_banco` — los avisos de transferencia que los BANCOS DEL
     PACIENTE mandan al Gmail del centro cuando la plata efectivamente sale
     de la cuenta del que paga (ver `app/transferencias_email_parser.py`).

Ninguna de las dos fuentes es "la verdad" por sí sola: recepción puede anotar
un pago que nunca llegó (paciente dijo que transfirió y no lo hizo, o
transfirió a la cuenta equivocada), y puede entrar plata al banco que nadie
anotó (paciente transfirió sin avisar, quedó en el limbo). Cruzarlas es lo
único que responde la pregunta real.

LA TRAMPA (no ignorar): quien transfiere no siempre es el paciente — un hijo
paga la consulta de la madre, una empresa paga por un trabajador. Por eso el
cruce usa MONTO + FECHA como llave primaria (con tolerancia ±1 día por
desfase de registro) y el NOMBRE es solo una señal de confianza, nunca un
filtro. Si el monto y la fecha calzan pero el nombre no, el registro se
reporta como "probable" (no como "sin registrar") — puede ser perfectamente
un familiar pagando. Cuando dos o más pagos/correos comparten el mismo monto
y fecha, el cruce es ambiguo POR DISEÑO — se reporta como tal, nunca se
adivina cuál es cuál (ver `LOG_AMBIGUOS` más abajo: "un empate declarado vale
más que una asignación inventada").

Reuso de infraestructura IMAP: mismas credenciales y helpers de bajo nivel
que `email_ticker.py` (`_connect_imap`, `_get_body_text`, `_decode_subject`,
mismo buzón `INBOX`). Cursor de sincronización PROPIO
(`transferencias_banco_last_uid` en `system_state`) — independiente del
cursor de citas porque ambos módulos filtran remitentes/asuntos
completamente distintos del mismo buzón; compartir cursor acoplaría dos
dominios de negocio sin relación.
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from transferencias_email_parser import (
    BANCOS_LABEL,
    BANCOS_REMITENTES,
    identificar_banco,
    parse_email,
)

log = logging.getLogger("conciliacion_transferencias")

_CLT = ZoneInfo("America/Santiago")

IMAP_MAILBOX = "INBOX"  # mismo buzón y convención que email_ticker.py

# Tope defensivo por pasada incremental (igual criterio que email_ticker.py).
_MAX_NUEVOS_POR_PASADA = 300


# ── DDL ──────────────────────────────────────────────────────────────────

def ensure_conciliacion_tables() -> None:
    """Crea las tablas si no existen. Idempotente.

    `transferencias_banco` es TABLA COMPARTIDA con `app/abono_transferencia.py`
    (confirmación automática de abonos de Psiquiatría, gateado
    `ABONO_AUTO_ACTIVE`, aún no activado a la fecha de esto). Ese módulo la
    diseñó primero y su propio docstring la describe como "reusable por el
    módulo de conciliación" — se adopta AQUÍ el esquema exacto de
    `abono_transferencia.ensure_transferencias_table()` (columnas
    `nombre_pagador`, `codigo_operacion`, `email_ts`, `abono_pendiente_id`,
    `estado_match`) para que sea la MISMA tabla física sin importar cuál de
    los dos módulos la crea primero. Se agregan 3 columnas propias
    (`mensaje`, `subject`, `remitente`) vía `ALTER TABLE` idempotente — no
    se tocan ni se renombran las columnas del otro módulo. `estado_match`
    queda intacto para su uso (nunca se escribe desde este módulo)."""
    from session import db
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transferencias_banco (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                uid                 INTEGER UNIQUE,
                banco               TEXT DEFAULT '',
                email_ts            TEXT DEFAULT '',
                nombre_pagador      TEXT DEFAULT '',
                monto               INTEGER DEFAULT 0,
                fecha               TEXT DEFAULT '',
                hora                TEXT DEFAULT '',
                codigo_operacion    TEXT DEFAULT '',
                abono_pendiente_id  INTEGER,
                estado_match        TEXT DEFAULT 'sin_match',
                created_at          TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transf_ts     ON transferencias_banco(email_ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transf_monto  ON transferencias_banco(monto)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transf_estado ON transferencias_banco(estado_match)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transf_banco_fecha ON transferencias_banco(fecha)")
        for col_ddl in [
            "ALTER TABLE transferencias_banco ADD COLUMN mensaje TEXT DEFAULT ''",
            "ALTER TABLE transferencias_banco ADD COLUMN subject TEXT DEFAULT ''",
            "ALTER TABLE transferencias_banco ADD COLUMN remitente TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(col_ddl)
            except Exception:
                pass  # columna ya existe
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transferencias_banco_errores (
                uid          INTEGER PRIMARY KEY,
                banco        TEXT DEFAULT '',
                remitente    TEXT DEFAULT '',
                subject      TEXT DEFAULT '',
                motivo       TEXT DEFAULT '',
                email_date   TEXT DEFAULT '',
                creado_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


# ── IMAP: helpers bloqueantes (SIEMPRE llamados vía asyncio.to_thread) ────

def _connect_readonly() -> "imaplib.IMAP4_SSL":
    from email_ticker import _connect_imap
    from config import GMAIL_CMC_USER, GMAIL_CMC_APP_PASSWORD
    if not GMAIL_CMC_USER or not GMAIL_CMC_APP_PASSWORD:
        raise RuntimeError("GMAIL_CMC_USER/GMAIL_CMC_APP_PASSWORD no configurados")
    M = _connect_imap()
    M.login(GMAIL_CMC_USER, GMAIL_CMC_APP_PASSWORD)
    M.select(IMAP_MAILBOX, readonly=True)
    return M


def _uids_ya_vistos() -> set[int]:
    from session import db
    ensure_conciliacion_tables()
    with db() as c:
        vistos = {r[0] for r in c.execute("SELECT uid FROM transferencias_banco")}
        vistos |= {r[0] for r in c.execute("SELECT uid FROM transferencias_banco_errores")}
    return vistos


def _parse_y_guardar(uid: int, banco: str, remitente: str, subject: str,
                      body: str, email_date: str) -> bool:
    """Parsea un correo ya identificado y lo persiste (OK o error). Devuelve
    True si quedó parseado con los campos núcleo, False si fue a errores."""
    from session import db
    r = parse_email(banco, subject, body)
    nuevo = False
    with db() as c:
        if r:
            cur = c.execute("""
                INSERT OR IGNORE INTO transferencias_banco
                    (uid, banco, nombre_pagador, monto, fecha, hora,
                     codigo_operacion, mensaje, subject, remitente, email_ts)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (uid, r["banco"], r["nombre"], r["monto"], r["fecha"], r["hora"],
                  r["num_operacion"], r["mensaje"], subject, remitente, email_date))
            c.commit()
            nuevo = cur.rowcount > 0
        else:
            c.execute("""
                INSERT OR IGNORE INTO transferencias_banco_errores
                    (uid, banco, remitente, subject, motivo, email_date)
                VALUES (?,?,?,?,?,?)
            """, (uid, banco or "", remitente, subject,
                  "no se pudo extraer nombre/monto/fecha del cuerpo", email_date))
            c.commit()

    if r and nuevo:
        # Solo INTENTA sugerir (nunca registra solo) — no-op si la fecha del
        # correo no es hoy (ver docstring de registrar_sugerencia_si_aplica).
        try:
            from pagos_transferencia_sugeridos import registrar_sugerencia_si_aplica
            registrar_sugerencia_si_aplica(uid, r["banco"], r["nombre"], r["monto"],
                                            r["fecha"], r["hora"], r["num_operacion"])
        except Exception as e:
            log.warning("_parse_y_guardar: fallo generando sugerencia uid=%s: %s", uid, e)

    return bool(r)


def _fetch_rfc822_batch(M, uids: list[int], chunk: int = 40):
    """Trae cada correo con SU PROPIO comando `UID FETCH` (uno por uno), NO
    un solo FETCH con varios UID separados por coma.

    Se intentó primero el fetch por lotes (un solo comando IMAP con N UID
    separados por coma) por velocidad — MEDIDO EN PRODUCCIÓN 2026-07-14 y
    descartado: con cuerpos RFC822 completos (HTML pesado, algunos >80KB),
    lotes de 40 devolvían de forma consistente ~10-15% MENOS tuplas que UID
    pedidos (ej. lote de 40 → 36 tuplas), y no por descarte prolijo del
    servidor sino por CORRIMIENTO del parseo de literales de `imaplib` — la
    prueba decisiva fue encontrar, en la respuesta de un lote específico, un
    email cuyo UID en la cabecera no correspondía a ningún UID solicitado en
    ESE lote (arrastrado de un lote de fecha muy anterior). Para una
    auditoría financiera esto es inaceptable: significa contenido de un
    correo emparejado con el UID de otro. Se cambió a un fetch por correo
    (mismo patrón que `email_ticker._fetch_new_emails_sync`, ya probado en
    producción) — más lento (~1 round-trip por correo) pero sin ambigüedad
    posible de emparejamiento. El parámetro `chunk` se conserva solo como
    cadencia de log de progreso, no como tamaño de lote IMAP real.
    """
    procesados = 0
    for uid in uids:
        try:
            typ, data = M.uid("fetch", str(uid), "(RFC822)")
        except imaplib.IMAP4.error as e:
            log.warning("_fetch_rfc822_batch: fallo IMAP UID %d: %s", uid, e)
            continue
        if typ != "OK" or not data or not data[0]:
            log.warning("_fetch_rfc822_batch: UID %d sin datos (typ=%s)", uid, typ)
            continue
        item = data[0]
        if not isinstance(item, tuple):
            log.warning("_fetch_rfc822_batch: UID %d respuesta inesperada: %r", uid, item)
            continue
        try:
            msg = email.message_from_bytes(item[1])
        except Exception as e:
            log.warning("_fetch_rfc822_batch: fallo parseando UID %d: %s", uid, e)
            continue
        procesados += 1
        if chunk and procesados % chunk == 0:
            log.info("_fetch_rfc822_batch: %d/%d correos traídos", procesados, len(uids))
        yield uid, msg


def _procesar_mensaje(uid: int, msg: "email.message.Message") -> str:
    """Identifica banco, parsea y guarda. Devuelve 'ok' | 'error' | 'ignorado'."""
    from email_ticker import _get_body_text, _decode_subject
    remitente = msg.get("From", "")
    banco = identificar_banco(remitente)
    if not banco:
        return "ignorado"
    subject = _decode_subject(msg.get("Subject"))
    body = _get_body_text(msg)
    try:
        dt = email.utils.parsedate_to_datetime(msg.get("Date", ""))
        email_date = dt.astimezone(_CLT).strftime("%Y-%m-%d %H:%M:%S") if dt else ""
    except Exception:
        email_date = ""
    ok = _parse_y_guardar(uid, banco, remitente, subject, body, email_date)
    return "ok" if ok else "error"


# ── Backfill histórico (una vez, o reanudable) ─────────────────────────────

def backfill_sync(limite: int | None = None) -> dict:
    """Recorre TODO `INBOX` y parsea los avisos de banco nuevos.

    NO usa `SEARCH FROM "addr"` — medido en producción 2026-07-14 y
    descartado: en este buzón esa búsqueda pierde correos GENUINOS y
    RECIENTES de forma silenciosa (verificado con casos concretos: 4 avisos
    reales de Scotiabank/Falabella de la primera semana de julio de 2026,
    con el remitente exacto, que `SEARCH FROM` no devolvía aunque un
    `SEARCH ALL` + filtro del encabezado real sí los encontraba). Es un
    comportamiento del backend de búsqueda de Gmail (no indexa/replica al
    instante, o trata "FROM" de forma más laxa de lo que documenta IMAP),
    no un bug de este código — pero el efecto práctico es el mismo: una
    auditoría financiera basada en `SEARCH FROM` subestima calladamente.

    Estrategia confiable (dos fases, medida ~40x más rápida que iterar cada
    correo con RFC822 completo):
      1. `SEARCH ALL` + fetch de SOLO el encabezado `From` (liviano, se
         puede pedir en lotes grandes sin el problema de desalineación que
         sí afecta al RFC822 completo — ver docstring de
         `_fetch_rfc822_batch`) de TODOS los correos del buzón. Filtra
         client-side con `identificar_banco()` — nunca confía en lo que
         Gmail decidió que "matchea" un FROM.
      2. Fetch RFC822 completo (uno por uno, confiable) SOLO para los que
         de verdad son de un banco conocido y no están ya procesados.

    Reanudable: `_uids_ya_vistos()` excluye tanto los OK como los ya
    marcados error, así que un correo irrecuperable no se reintenta
    infinitamente — para reintentar tras arreglar un parser hay que borrar
    sus filas de `transferencias_banco_errores` a propósito."""
    from config import GMAIL_CMC_USER, GMAIL_CMC_APP_PASSWORD
    resumen = {"ok": True, "total_mensajes_buzon": 0, "candidatos_banco": 0,
               "ya_existian": 0, "nuevos_ok": 0, "nuevos_error": 0, "por_banco": {}}
    if not GMAIL_CMC_USER or not GMAIL_CMC_APP_PASSWORD:
        return {"ok": False, "error": "GMAIL_CMC_USER/GMAIL_CMC_APP_PASSWORD no configurados"}

    ensure_conciliacion_tables()
    vistos = _uids_ya_vistos()

    M = None
    try:
        M = _connect_readonly()
        typ, data = M.search(None, "ALL")
        if typ != "OK":
            return {"ok": False, "error": f"SEARCH ALL falló: {typ}"}
        todos_uids = sorted(int(u) for u in data[0].split() if u.isdigit())
        resumen["total_mensajes_buzon"] = len(todos_uids)

        # Fase 1 — encabezados FROM en lotes grandes (liviano y seguro en
        # lote a diferencia del RFC822 completo).
        candidatos: list[tuple[int, str]] = []
        chunk_hdr = 500
        for i in range(0, len(todos_uids), chunk_hdr):
            lote = todos_uids[i:i + chunk_hdr]
            uid_str = ",".join(str(u) for u in lote)
            try:
                typ, resp = M.uid("fetch", uid_str, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
            except imaplib.IMAP4.error as e:
                log.warning("backfill_sync: fase1 fallo en lote desde %s: %s", lote[0], e)
                continue
            if typ != "OK" or not resp:
                continue
            for item in resp:
                if not isinstance(item, tuple):
                    continue
                m = re.search(rb"UID (\d+)", item[0])
                if not m:
                    continue
                uid = int(m.group(1))
                raw = item[1].decode(errors="replace")
                m_from = re.search(r"From:\s*(.*)", raw)
                frm = m_from.group(1).strip() if m_from else ""
                banco = identificar_banco(frm)
                if banco:
                    candidatos.append((uid, banco))
        resumen["candidatos_banco"] = len(candidatos)
        log.info("backfill_sync: fase1 — %d candidatos de banco de %d mensajes en el buzón",
                  len(candidatos), len(todos_uids))

        # Fase 2 — fetch RFC822 completo solo para candidatos pendientes.
        pendientes = [(u, b) for u, b in candidatos if u not in vistos]
        resumen["ya_existian"] = len(candidatos) - len(pendientes)
        if limite:
            pendientes = pendientes[:limite]
        uids_pend = [u for u, _b in pendientes]

        conteo_banco: dict[str, dict] = {}
        for uid, msg in _fetch_rfc822_batch(M, uids_pend, chunk=100):
            estado = _procesar_mensaje(uid, msg)
            banco_de_este = identificar_banco(msg.get("From", "")) or "?"
            conteo_banco.setdefault(banco_de_este, {"nuevos_ok": 0, "nuevos_error": 0})
            if estado == "ok":
                resumen["nuevos_ok"] += 1
                conteo_banco[banco_de_este]["nuevos_ok"] += 1
            elif estado == "error":
                resumen["nuevos_error"] += 1
                conteo_banco[banco_de_este]["nuevos_error"] += 1
            vistos.add(uid)
        resumen["por_banco"] = conteo_banco
        log.info("backfill_sync: fase2 completa — %d nuevos OK, %d nuevos error (de %d pendientes)",
                  resumen["nuevos_ok"], resumen["nuevos_error"], len(pendientes))
    except Exception as e:
        log.error("backfill_sync: fallo inesperado: %s", e)
        resumen["ok"] = False
        resumen["error"] = str(e)
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass
    return resumen


async def backfill_transferencias_banco(limite: int | None = None) -> dict:
    """Wrapper async — todo el trabajo bloqueante corre en threadpool."""
    import asyncio
    from session import system_state_set
    system_state_set("transferencias_backfill_status", "running")
    try:
        r = await asyncio.to_thread(backfill_sync, limite)
    finally:
        system_state_set("transferencias_backfill_status", "idle")
    return r


# ── Sincronización incremental (cron) ───────────────────────────────────────

def _fetch_new_emails_sync(cursor_uid: int) -> tuple[list[tuple[int, "email.message.Message"]], int]:
    from config import GMAIL_CMC_USER, GMAIL_CMC_APP_PASSWORD
    if not GMAIL_CMC_USER or not GMAIL_CMC_APP_PASSWORD:
        return [], cursor_uid
    M = None
    try:
        M = _connect_readonly()
        typ, data = M.uid("search", None, f"UID {cursor_uid + 1}:*")
        if typ != "OK":
            return [], cursor_uid
        uids = sorted(int(u) for u in data[0].split() if u.isdigit() and int(u) > cursor_uid)
        if not uids:
            return [], cursor_uid
        if len(uids) > _MAX_NUEVOS_POR_PASADA:
            log.warning("_fetch_new_emails_sync: %d correos nuevos (> %d), se procesan los primeros %d",
                        len(uids), _MAX_NUEVOS_POR_PASADA, _MAX_NUEVOS_POR_PASADA)
            uids = uids[:_MAX_NUEVOS_POR_PASADA]
        new_max = max(uids)
        out = list(_fetch_rfc822_batch(M, uids, chunk=40))
        return out, new_max
    except Exception as e:
        log.error("_fetch_new_emails_sync: fallo inesperado: %s", e)
        return [], cursor_uid
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass


def _seed_cursor_sync() -> int | None:
    from config import GMAIL_CMC_USER, GMAIL_CMC_APP_PASSWORD
    if not GMAIL_CMC_USER or not GMAIL_CMC_APP_PASSWORD:
        return None
    M = None
    try:
        M = _connect_readonly()
        typ, data = M.status(IMAP_MAILBOX, "(UIDNEXT)")
        if typ != "OK" or not data:
            return None
        raw = data[0].decode() if isinstance(data[0], bytes) else data[0]
        m = re.search(r"UIDNEXT (\d+)", raw)
        return max(int(m.group(1)) - 1, 0) if m else None
    except Exception as e:
        log.error("_seed_cursor_sync: %s", e)
        return None
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass


async def poll_conciliacion_transferencias() -> dict:
    """Job del scheduler — sincroniza SOLO correos nuevos desde el cursor.
    El backfill histórico es una acción aparte (`backfill_transferencias_banco`,
    disparada manual desde el panel), igual que el arranque en frío de
    `email_ticker.poll_email_ticker`: la primera pasada solo siembra el
    cursor, no procesa historial (evitaría re-descubrir todo el backfill)."""
    import asyncio
    from session import system_state_get, system_state_set

    try:
        ensure_conciliacion_tables()
        cursor_raw = system_state_get("transferencias_banco_last_uid")
        if cursor_raw is None:
            seed = await asyncio.to_thread(_seed_cursor_sync)
            if seed is None:
                return {"ok": False, "error": "no se pudo sembrar cursor inicial"}
            system_state_set("transferencias_banco_last_uid", str(seed))
            log.info("poll_conciliacion_transferencias: arranque en frío, cursor=%d", seed)
            return {"ok": True, "nuevos": 0, "cursor": seed, "cold_start": True}

        cursor = int(cursor_raw)
        raw, new_max = await asyncio.to_thread(_fetch_new_emails_sync, cursor)
        if new_max > cursor:
            system_state_set("transferencias_banco_last_uid", str(new_max))
        if not raw:
            return {"ok": True, "nuevos": 0, "cursor": new_max}

        ok = err = ignorado = 0
        for uid, msg in raw:
            try:
                estado = await asyncio.to_thread(_procesar_mensaje, uid, msg)
            except Exception as e:
                log.error("poll_conciliacion_transferencias: fallo UID %d: %s", uid, e)
                estado = "error"
            if estado == "ok":
                ok += 1
            elif estado == "error":
                err += 1
            else:
                ignorado += 1
        log.info("poll_conciliacion_transferencias: %d ok, %d error, %d ignorados (no bancarios), cursor=%d",
                  ok, err, ignorado, new_max)
        return {"ok": True, "nuevos": ok, "errores": err, "ignorados": ignorado, "cursor": new_max}
    except Exception as e:
        log.error("poll_conciliacion_transferencias: fallo inesperado: %s", e)
        return {"ok": False, "error": str(e)}


# ── Motor de conciliación (puro, sin IO — testeable con listas en memoria) ──

def _normalizar_nombre(s: str | None) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _similitud_nombre(a: str | None, b: str | None) -> float:
    """Jaccard sobre tokens (palabras) del nombre normalizado. 0 si no hay
    ninguna palabra en común, 1 si son el mismo conjunto de palabras. Sirve
    de SEÑAL de confianza — nunca de filtro (ver docstring del módulo)."""
    na, nb = _normalizar_nombre(a), _normalizar_nombre(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(w for w in na.split() if len(w) >= 2), set(w for w in nb.split() if len(w) >= 2)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    return len(inter) / len(union) if union else 0.0


def _norm_codigo(s: str | None) -> str:
    """Normaliza un código/número de operación para comparar: solo dígitos,
    sin ceros a la izquierda (distintos bancos rellenan con ceros distinto)."""
    s = re.sub(r"\D", "", s or "")
    return s.lstrip("0") or s


def _confianza(sim: float, desfase_dias: int) -> str:
    base = "exacto" if sim >= 0.5 else "probable"
    if desfase_dias:
        base += "_fecha_desfasada"
    return base


def _build_match(p: dict, e: dict, confianza: str) -> dict:
    return {
        "pago_id": p["id"],
        "uid": e["uid"],
        "banco": e["banco"],
        "banco_label": BANCOS_LABEL.get(e["banco"], e["banco"]),
        "fecha_pago": p["fecha"],
        "fecha_email": e["fecha"],
        "monto": p["copago"],
        "paciente": p["paciente_nombre"],
        "profesional": p.get("profesional") or "",
        "nombre_transfiere": e["nombre_transfiere"],
        "confianza": confianza,
    }


def _conciliar_listas(pagos: list[dict], emails: list[dict],
                       desde: str, hasta: str) -> dict:
    """Núcleo puro del motor de conciliación — recibe listas ya cargadas
    (con buffer ±1 día alrededor de [desde,hasta]) y devuelve el reporte
    completo. Sin acceso a base de datos: 100% testeable con fixtures."""
    pagos_by_id = {p["id"]: p for p in pagos}
    emails_by_uid = {e["uid"]: e for e in emails}
    usados_pago: set = set()
    usados_email: set = set()
    matches: list[dict] = []

    # Pass A — código de transferencia (pagos_cmc) == número de operación
    # (correo banco). Es la única llave EXACTA disponible (no depende de
    # monto/fecha/nombre) y por eso corre primero.
    op_index: dict[str, list[int]] = defaultdict(list)
    for e in emails:
        if e.get("num_operacion"):
            op_index[_norm_codigo(e["num_operacion"])].append(e["uid"])
    for p in pagos:
        cod = (p.get("codigo_transferencia") or "").strip()
        if not cod:
            continue
        cands = [u for u in op_index.get(_norm_codigo(cod), []) if u not in usados_email]
        if len(cands) == 1:
            e = emails_by_uid[cands[0]]
            matches.append(_build_match(p, e, "exacto_codigo"))
            usados_pago.add(p["id"])
            usados_email.add(e["uid"])

    # Pass B/C — monto + fecha (tolerancia 0, luego ±1 día). Dentro de cada
    # bucle de monto, si hay más de un candidato posible de cada lado, solo
    # se asigna automáticamente el/los par(es) con similitud de nombre FUERTE
    # (>=0.5) — el resto queda para el reporte de ambiguos, nunca se adivina.
    for tolerancia in (0, 1):
        pagos_restantes = [p for p in pagos if p["id"] not in usados_pago]
        emails_restantes = [e for e in emails if e["uid"] not in usados_email]
        by_monto_p: dict[int, list[dict]] = defaultdict(list)
        for p in pagos_restantes:
            if p.get("copago"):
                by_monto_p[p["copago"]].append(p)
        by_monto_e: dict[int, list[dict]] = defaultdict(list)
        for e in emails_restantes:
            if e.get("monto"):
                by_monto_e[e["monto"]].append(e)

        for monto, plist in by_monto_p.items():
            elist = by_monto_e.get(monto)
            if not elist:
                continue
            pares = []
            for p in plist:
                if p["id"] in usados_pago:
                    continue
                fp = date.fromisoformat(p["fecha"])
                for e in elist:
                    if e["uid"] in usados_email:
                        continue
                    fe = date.fromisoformat(e["fecha"])
                    dias = abs((fp - fe).days)
                    if dias <= tolerancia:
                        sim = _similitud_nombre(p.get("paciente_nombre"), e.get("nombre_transfiere"))
                        pares.append((sim, dias, p, e))
            if not pares:
                continue
            pagos_unicos = {p["id"] for _, _, p, _ in pares}
            emails_unicos = {e["uid"] for _, _, _, e in pares}
            if len(pagos_unicos) == 1 and len(emails_unicos) == 1:
                sim, dias, p, e = pares[0]
                matches.append(_build_match(p, e, _confianza(sim, dias)))
                usados_pago.add(p["id"])
                usados_email.add(e["uid"])
            else:
                # Ambiguo: solo se asigna automático lo que tenga nombre
                # fuertemente coincidente (greedy por similitud desc.).
                for sim, dias, p, e in sorted(pares, key=lambda x: (-x[0], x[1])):
                    if sim < 0.5:
                        continue
                    if p["id"] in usados_pago or e["uid"] in usados_email:
                        continue
                    matches.append(_build_match(p, e, _confianza(sim, dias)))
                    usados_pago.add(p["id"])
                    usados_email.add(e["uid"])

    # Reporte final — SOLO para lo que cae dentro del rango estricto pedido
    # (lo del buffer ±1 día que quedó sin usar se descarta del reporte: se
    # evaluará correctamente cuando se consulte SU propio período).
    d0, d1 = date.fromisoformat(desde), date.fromisoformat(hasta)

    def _en_rango(f: str) -> bool:
        try:
            return d0 <= date.fromisoformat(f) <= d1
        except ValueError:
            return False

    pagos_restantes = [p for p in pagos if p["id"] not in usados_pago and _en_rango(p["fecha"])]
    emails_restantes = [e for e in emails if e["uid"] not in usados_email and _en_rango(e["fecha"])]

    # Grupos ambiguos: UNION-FIND por (mismo monto + fecha dentro de ±1 día).
    # BUG evitado a propósito (medido en producción 2026-07-14): agrupar
    # "todo lo que comparte este monto" sin acotar por fecha juntaba, para
    # montos redondos frecuentes ($15.000, $30.000, etc.), pagos y correos
    # de AÑOS distintos en un solo "grupo ambiguo" gigante — con 3+ años de
    # historia, un monto típico de consulta se repite cientos de veces y
    # ese agrupamiento plano volvía "ambiguo" casi todo el histórico. El
    # union-find solo conecta un pago con un correo si están DENTRO de la
    # ventana de fecha real (±1 día) — dos pagos de $15.000 con 2 años de
    # diferencia nunca quedan en el mismo componente.
    candidatos_email_por_monto: dict[int, list[dict]] = defaultdict(list)
    for e in emails:
        if e.get("monto") and e["uid"] not in usados_email:
            candidatos_email_por_monto[e["monto"]].append(e)

    nodos: list[tuple[str, dict]] = [("p", p) for p in pagos_restantes]
    montos_relevantes = {p["copago"] for p in pagos_restantes if p.get("copago")}
    emails_relevantes = [e for m in montos_relevantes for e in candidatos_email_por_monto.get(m, [])]
    emails_relevantes = list({e["uid"]: e for e in emails_relevantes}.values())
    nodos += [("e", e) for e in emails_relevantes]

    parent = list(range(len(nodos)))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    idx_email_por_monto: dict[int, list[int]] = defaultdict(list)
    for i, (tipo, obj) in enumerate(nodos):
        if tipo == "e":
            idx_email_por_monto[obj["monto"]].append(i)

    for i, (tipo, obj) in enumerate(nodos):
        if tipo != "p" or not obj.get("copago"):
            continue
        fp = date.fromisoformat(obj["fecha"])
        for j in idx_email_por_monto.get(obj["copago"], []):
            e = nodos[j][1]
            fe = date.fromisoformat(e["fecha"])
            if abs((fp - fe).days) <= 1:
                _union(i, j)

    grupos_raw: dict[int, dict] = defaultdict(lambda: {"pagos": [], "correos": []})
    for i, (tipo, obj) in enumerate(nodos):
        root = _find(i)
        grupos_raw[root]["pagos" if tipo == "p" else "correos"].append(obj)

    ambiguos = []
    pagos_en_ambiguo: set = set()
    emails_en_ambiguo: set = set()
    for g in grupos_raw.values():
        if not g["pagos"] or not g["correos"]:
            continue  # componente de un solo lado -> no es un empate, no reporta acá
        plist, elist = g["pagos"], g["correos"]
        monto = plist[0]["copago"]
        ambiguos.append({
            "monto": monto,
            "pagos": [{"pago_id": p["id"], "fecha": p["fecha"], "paciente": p["paciente_nombre"],
                       "profesional": p.get("profesional") or ""} for p in plist],
            "correos": [{"uid": e["uid"], "fecha": e["fecha"], "banco": e["banco"],
                         "banco_label": BANCOS_LABEL.get(e["banco"], e["banco"]),
                         "nombre_transfiere": e["nombre_transfiere"]} for e in elist],
            "nota": f"{len(plist)} pago(s) registrado(s) y {len(elist)} correo(s) de "
                    f"banco comparten el monto ${monto:,}".replace(",", ".") +
                    " en fechas cercanas — no se puede asignar con certeza.",
        })
        pagos_en_ambiguo |= {p["id"] for p in plist}
        emails_en_ambiguo |= {e["uid"] for e in elist}

    registrado_sin_correo = [p for p in pagos_restantes if p["id"] not in pagos_en_ambiguo]
    correo_sin_registro = [e for e in emails_restantes if e["uid"] not in emails_en_ambiguo]

    matches_en_rango = [m for m in matches if _en_rango(m["fecha_pago"])]

    def _por_mes(items: list[dict], campo_fecha: str, campo_monto: str) -> dict:
        out: dict[str, dict] = defaultdict(lambda: {"n": 0, "monto": 0})
        for it in items:
            mes = it[campo_fecha][:7]
            out[mes]["n"] += 1
            out[mes]["monto"] += it[campo_monto] or 0
        return dict(sorted(out.items()))

    return {
        "periodo": {"desde": desde, "hasta": hasta},
        "matches": matches_en_rango,
        "registrado_sin_correo": registrado_sin_correo,
        "correo_sin_registro": correo_sin_registro,
        "ambiguos": ambiguos,
        "totales": {
            "conciliado_n": len(matches_en_rango),
            "conciliado_monto": sum(m["monto"] or 0 for m in matches_en_rango),
            "registrado_sin_correo_n": len(registrado_sin_correo),
            "registrado_sin_correo_monto": sum(p.get("copago") or 0 for p in registrado_sin_correo),
            "correo_sin_registro_n": len(correo_sin_registro),
            "correo_sin_registro_monto": sum(e.get("monto") or 0 for e in correo_sin_registro),
            "ambiguo_grupos": len(ambiguos),
            "ambiguo_monto_pagos": sum(g["monto"] * len(g["pagos"]) for g in ambiguos),
            "ambiguo_monto_correos": sum(g["monto"] * len(g["correos"]) for g in ambiguos),
        },
        "por_mes": {
            "conciliado": _por_mes(matches_en_rango, "fecha_pago", "monto"),
            "registrado_sin_correo": _por_mes(registrado_sin_correo, "fecha", "copago"),
            "correo_sin_registro": _por_mes(correo_sin_registro, "fecha", "monto"),
        },
    }


# ── Carga desde SQLite + wrapper público ────────────────────────────────────

def _shift(iso: str, dias: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=dias)).isoformat()


def _cargar_pagos_transferencia(desde: str, hasta: str) -> list[dict]:
    from session import db
    with db() as c:
        rows = c.execute("""
            SELECT id, fecha, hora, paciente_nombre, rut, profesional, area,
                   copago, codigo_transferencia
            FROM pagos_cmc
            WHERE metodo_pago = 'transferencia' AND fecha BETWEEN ? AND ?
            ORDER BY fecha, hora
        """, (desde, hasta)).fetchall()
    return [dict(r) for r in rows]


def _cargar_emails_banco(desde: str, hasta: str) -> list[dict]:
    from session import db
    ensure_conciliacion_tables()
    with db() as c:
        rows = c.execute("""
            SELECT uid, banco, nombre_pagador AS nombre_transfiere, monto, fecha, hora,
                   codigo_operacion AS num_operacion, mensaje
            FROM transferencias_banco
            WHERE fecha BETWEEN ? AND ?
            ORDER BY fecha, hora
        """, (desde, hasta)).fetchall()
    return [dict(r) for r in rows]


def conciliar(desde: str, hasta: str) -> dict:
    """Punto de entrada público. `desde`/`hasta` en formato YYYY-MM-DD."""
    buf_desde, buf_hasta = _shift(desde, -1), _shift(hasta, 1)
    pagos = _cargar_pagos_transferencia(buf_desde, buf_hasta)
    emails = _cargar_emails_banco(buf_desde, buf_hasta)
    return _conciliar_listas(pagos, emails, desde, hasta)


def estado_backfill() -> dict:
    from session import db, system_state_get
    ensure_conciliacion_tables()
    with db() as c:
        total_ok = c.execute("SELECT COUNT(*) FROM transferencias_banco").fetchone()[0]
        total_err = c.execute("SELECT COUNT(*) FROM transferencias_banco_errores").fetchone()[0]
        por_banco = c.execute(
            "SELECT banco, COUNT(*) n, MIN(fecha) desde, MAX(fecha) hasta "
            "FROM transferencias_banco GROUP BY banco ORDER BY n DESC"
        ).fetchall()
        errores_por_banco = c.execute(
            "SELECT banco, COUNT(*) n FROM transferencias_banco_errores GROUP BY banco ORDER BY n DESC"
        ).fetchall()
    return {
        "running": system_state_get("transferencias_backfill_status") == "running",
        "total_parseados": total_ok,
        "total_errores": total_err,
        "por_banco": [dict(r) for r in por_banco],
        "errores_por_banco": [dict(r) for r in errores_por_banco],
    }
