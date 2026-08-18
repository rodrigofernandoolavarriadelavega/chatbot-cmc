"""abono_transferencia.py — Confirmación automática de abonos por transferencia
bancaria (hoy: Psiquiatría, Dra. Cecilia Unibazo, prof 78, abono ÚNICO de
$60.000 — la consulta completa por adelantado. Es la ÚNICA prestación con
abono previo del bot; ver `ABONO_PSIQUIATRIA_CLP` en config.py).

TODO GATEADO por `ABONO_AUTO_ACTIVE` (config.py, default false). Con el flag
apagado este módulo no abre conexiones IMAP, no registra el cron, y no cambia
en nada el comportamiento actual (`WAIT_ABONO_COMPROBANTE` sigue igual).

## Reemplaza qué
Hoy, cuando se activa el abono-gate de Psiquiatría (`ABONO_GATE_PSIQ_ACTIVE`),
el bot pide al paciente una FOTO del comprobante que alguien de recepción
revisa a mano. Este módulo lee el correo que el banco le manda al Gmail del
centro (`centromedicocarampangue@gmail.com`, solo lectura IMAP) y confirma la
hora SOLO cuando hay certeza — sin tocar el monto que paga el paciente ni
cobrar comisión de pasarela.

## El cruce (diseño final, decidido con el dueño 2026-07-14)
NO se le pregunta nada al paciente por adelantado — cero fricción para el
80%+ que transfiere lo suyo. El bot ya sabe, desde el momento en que pide el
abono, A QUIÉN se lo pidió, CUÁNTO y CUÁNDO (`abono_pendientes`, creado al
activarse el gate). Al llegar un correo bancario:

  1. Filtrar por CUENTA DESTINO del CMC (Itaú 0-002-21-70853-8 / RUT
     77.140.898-2) — el buzón recibe avisos de OTRAS cuentas del dueño
     (verificado con datos históricos: la única transferencia de $60.000 en
     ~5 años de este buzón iba a una cuenta BancoEstado de otro negocio, NO
     al CMC). Sin este filtro un correo ajeno podría confirmar una hora que
     nadie pagó.
  2. Buscar abonos `estado='pendiente'` con el MISMO monto y cuyo `creado_at`
     ≤ hora del correo ≤ `expira_at` (ventana del abono-gate, 90 min).
  3. 0 candidatos → el correo queda como transferencia SIN ASIGNAR (le sirve
     a la conciliación del otro módulo). Nunca confirma nada.
  4. 1 candidato → comparar el nombre del correo contra el nombre del
     paciente (con tolerancia: mayúsculas, tildes, apellidos parciales, ver
     `nombres_similares`). Si calza → CONFIRMA SOLA. Si NO calza → le
     pregunta al paciente por WhatsApp (Sí/No, un toque) si esa persona pagó
     su reserva — la plata YA llegó, esto solo asocia un pago existente a
     una hora; nunca "regala" una confirmación.
  5. >1 candidatos con el mismo monto — filtra por nombre; si eso deja
     exactamente 1, confirma ese. Si sigue ambiguo, pregunta al candidato
     MÁS ANTIGUO primero (nunca a los dos a la vez — evita que un mismo
     correo confirme dos reservas).
  6. Una transferencia se CONSUME al confirmar (columna
     `abono_pendiente_id` en `transferencias_banco`) — por construcción no
     puede volver a usarse para otra reserva.

## Degradación (por qué este proyecto no puede empeorar lo que hay hoy)
Si Gmail no responde, un correo no parsea, o queda ambiguo → el correo se
loguea/registra sin asignar y el paciente sigue en `WAIT_ABONO_COMPROBANTE`
exactamente como hoy: puede mandar la foto del comprobante en cualquier
momento y ese camino sigue intacto. El peor caso posible es "no se emparejó
nada" = igual que el sistema actual. Nunca hay un callejón sin salida.

## Latencia real medida (correos históricos, 2026-07-14, muestra 40 c/u)
Falabella y Banco de Chile incluyen la hora exacta de la transacción en el
cuerpo del correo — se pudo medir el delta real contra la hora de llegada
del correo (`Date` header): p90 < 1 minuto en ambos bancos. Una ventana de
espera de `ABONO_AUTO_WAIT_MIN` minutos (default 12) antes de recurrir al
mensaje "mándanos la foto" tiene margen de sobra.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import secrets
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("abono_transferencia")
_CL = ZoneInfo("America/Santiago")

# ── Remitentes de banco que nos interesan ───────────────────────────────────
# FUENTE ÚNICA: transferencias_email_parser.BANCOS_REMITENTES (12+ bancos).
# Este módulo tenía su propia lista de SOLO 3 bancos (Santander/Falabella/
# BancoChile) — el 2026-08-04 el correo Scotiabank del abono de Bryan (papá
# pagador, $60.000) fue descartado acá mientras conciliación lo leía perfecto:
# BancoEstado+Scotiabank+BCI son ~69% de los pagadores históricos y el carril
# de abonos era ciego a todos ellos. No volver a duplicar parsers bancarios.
REMITENTE_SANTANDER  = "mensajeria@santander.cl"
REMITENTE_FALABELLA  = "notificaciones@cl.bancofalabella.com"
REMITENTE_BANCOCHILE = "serviciodetransferencias@bancochile.cl"
from transferencias_email_parser import BANCOS_REMITENTES as _BANCOS_REM_SHARED
REMITENTES_BANCOS = tuple(
    rem for rems in _BANCOS_REM_SHARED.values() for rem in rems
)

# Huella de la cuenta del CMC — ver docstring del módulo (§1). Solo dígitos,
# se compara contra el cuerpo del correo también reducido a solo dígitos, así
# no importa el formato exacto (con o sin guiones/puntos) que use cada banco.
_CMC_CUENTA_DIGITS = "0221708538"    # Banco Itaú, 0-002-21-70853-8
_CMC_RUT_DIGITS    = "771408982"     # 77.140.898-2

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


# ── Helpers de parseo ───────────────────────────────────────────────────────

def _monto_a_entero(s: str | None) -> int | None:
    if not s:
        return None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def _fecha_a_iso(fecha_str: str, sep: str = "/") -> str | None:
    """DD/MM/YYYY o DD-MM-YYYY → YYYY-MM-DD. No se adivina: si no calza el
    formato exacto, devuelve None (mejor 'sin fecha' que una fecha inventada)."""
    try:
        d, m, y = fecha_str.split(sep)
        return datetime(int(y), int(m), int(d)).date().isoformat()
    except Exception:
        return None


def _es_cuenta_cmc(body: str) -> bool:
    """True si el correo menciona la cuenta o el RUT del CMC en algún lado
    del cuerpo (dígitos únicamente, formato-agnóstico). Ver docstring §1."""
    solo_digitos = re.sub(r"\D", "", body or "")
    return _CMC_CUENTA_DIGITS in solo_digitos or _CMC_RUT_DIGITS in solo_digitos


def _parse_santander(body: str) -> dict | None:
    """Cubre las 2 plantillas vistas en el buzón real (verificado 2026-07-14,
    196 correos históricos): 'Comprobante Transferencia de fondos' (dominante,
    39/40 recientes) y 'Aviso de Transferencia de Fondos' (rara)."""
    m_nombre = re.search(
        r"nuestro cliente\s+([A-ZÁÉÍÓÚÑ0-9.\s]+?)\s+realiz[oó]\s+una transferencia",
        body, re.IGNORECASE,
    )
    m_fecha = re.search(r"con fecha,?\s*(\d{2}/\d{2}/\d{4})", body, re.IGNORECASE)
    sep = "/"
    if not m_nombre:
        m_nombre = re.search(
            r"nuestro cliente\s+([A-ZÁÉÍÓÚÑ0-9.\s]+?),\s*le informamos",
            body, re.IGNORECASE,
        )
        if not m_fecha:
            m_fecha = re.search(r"con Fecha:?\s*(\d{2}-\d{2}-\d{4})", body, re.IGNORECASE)
            sep = "-"
    m_monto = re.search(r"Monto transferido[\s\S]{0,30}?\$\s*([\d.,]+)", body, re.IGNORECASE)
    if not (m_nombre and m_monto):
        return None
    return {
        "banco": "Santander",
        "nombre_pagador": re.sub(r"\s+", " ", m_nombre.group(1)).strip(),
        "monto": _monto_a_entero(m_monto.group(1)),
        "fecha": _fecha_a_iso(m_fecha.group(1), sep) if m_fecha else None,
        "hora": None,
        "codigo_operacion": None,
    }


def _parse_falabella(body: str) -> dict | None:
    """Plantilla 'Aviso de transferencia de fondos recibida' (verificado
    2026-07-14, 381 correos históricos, plantilla estable). Incluye hora
    exacta de la transacción — permite medir latencia real del correo."""
    m_nombre = re.search(
        r"nuestro\(a\)\s+cliente\s+([A-ZÁÉÍÓÚÑ0-9.\s]+?)\s+ha instruido",
        body, re.IGNORECASE,
    )
    m_monto = re.search(r"Monto transferencia\s*\$\s*([\d.,]+)", body, re.IGNORECASE)
    m_fecha = re.search(r"\bFecha\s*(\d{2}-\d{2}-\d{4})", body)
    m_hora = re.search(r"\bHora\s*(\d{2}:\d{2})", body)
    m_codigo = re.search(r"Numero de operacion\s*(\d+)", body, re.IGNORECASE)
    if not (m_nombre and m_monto):
        return None
    return {
        "banco": "Falabella",
        "nombre_pagador": re.sub(r"\s+", " ", m_nombre.group(1)).strip(),
        "monto": _monto_a_entero(m_monto.group(1)),
        "fecha": _fecha_a_iso(m_fecha.group(1), "-") if m_fecha else None,
        "hora": m_hora.group(1) if m_hora else None,
        "codigo_operacion": m_codigo.group(1) if m_codigo else None,
    }


def _parse_bancochile(body: str) -> dict | None:
    """HTML pesado (verificado 2026-07-14, 254 correos históricos, re-verificado
    con muestra de 30 correos reales). Tras limpiar tags
    (`email_ticker._strip_html`) las celdas de tabla dejan MUCHO espacio en
    blanco entre etiqueta y valor (padding de la tabla original) — por eso
    todo se extrae por POSICIÓN DE LÍNEA (línea recortada == etiqueta, la
    siguiente línea no vacía == valor), nunca con un regex de una sola línea
    con un salto corto tipo `\\s{0,10}` (eso falló contra correos reales:
    la etiqueta y el valor pueden quedar separados por >60 espacios)."""
    lines = [l.strip() for l in body.splitlines() if l.strip()]

    nombre = None
    for i, l in enumerate(lines):
        if l.lower().endswith("cliente") and i + 2 < len(lines):
            siguiente2 = lines[i + 1] + " " + lines[i + 2]
            if "ha efectuado" in siguiente2.lower():
                nombre = lines[i + 1]
                break

    monto = None
    for i, l in enumerate(lines):
        if l.lower() == "monto" and i + 1 < len(lines):
            m = re.match(r"\$\s*([\d.,]+)", lines[i + 1])
            if m:
                monto = _monto_a_entero(m.group(1))
                break

    fecha_iso, hora = None, None
    for i, l in enumerate(lines):
        if l.lower().startswith("fecha y hora") and i + 1 < len(lines):
            m_fh = re.match(
                r"\w+\s+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})\s+(\d{1,2}):(\d{2})",
                lines[i + 1], re.IGNORECASE,
            )
            if m_fh:
                dia, mes_nombre, anio, hh, mm = m_fh.groups()
                mes = _MESES.get(mes_nombre.lower())
                if mes:
                    try:
                        fecha_iso = datetime(int(anio), mes, int(dia)).date().isoformat()
                        hora = f"{int(hh):02d}:{mm}"
                    except Exception:
                        pass
            break

    codigo = None
    for i, l in enumerate(lines):
        if "mero de comprobante" in l.lower() and i + 1 < len(lines):
            m_c = re.match(r"[A-Z0-9]+", lines[i + 1])
            if m_c:
                codigo = m_c.group(0)
            break

    if not (nombre and monto):
        return None
    return {
        "banco": "Banco de Chile",
        "nombre_pagador": re.sub(r"\s+", " ", nombre).strip(),
        "monto": monto,
        "fecha": fecha_iso,
        "hora": hora,
        "codigo_operacion": codigo,
    }


def parse_bank_email(remitente: str, body: str, subject: str = "") -> dict | None:
    """Punto de entrada del parseo. DELEGA en transferencias_email_parser
    (fuente única, 12+ bancos) — este módulo tenía parsers propios de solo 3
    bancos Y una copia divergente de _es_cuenta_cmc que rechazaba correos
    válidos (caso Bryan 2026-08-04: Scotiabank, shared=True/local=False).
    Devuelve None si el remitente no es banco conocido, si no se pudo leer
    nombre+monto, o si el correo NO es de la cuenta del CMC. Nunca lanza."""
    try:
        from transferencias_email_parser import (
            identificar_banco, parse_email, _es_cuenta_cmc as _cta_shared)
        banco = identificar_banco(remitente or "")
        if not banco:
            return None
        parsed = parse_email(banco, subject or "", body)
        if not parsed or not parsed.get("monto"):
            return None
        if not _cta_shared(body):
            log.info("parse_bank_email: correo de %s no es de la cuenta del CMC — se descarta", banco)
            return None
        return {
            "banco": parsed.get("banco") or banco,
            "nombre_pagador": parsed.get("nombre") or "",
            "monto": parsed["monto"],
            "fecha": parsed.get("fecha") or "",
            "hora": parsed.get("hora") or "",
            "codigo_operacion": parsed.get("num_operacion") or "",
        }
    except Exception as e:
        log.warning("parse_bank_email: fallo parseando correo de %r: %s", remitente, e)
        return None


# ── Similitud de nombres (señal de refuerzo, NUNCA llave única) ────────────

def _normalizar_nombre(s: str) -> str:
    s = (s or "").upper().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def nombres_similares(a: str, b: str, umbral: float = 0.55) -> tuple[bool, float]:
    """Compara dos nombres de persona con tolerancia: mayúsculas, tildes,
    apellidos parciales, segundo nombre, orden. P.ej. 'Carmen Soto' vs
    'CARMEN ANDREA SOTO PEREZ' → coincide (todos los tokens del nombre corto
    están en el largo). Devuelve (coincide, score 0-1). Es una señal más, no
    la llave del emparejamiento — ver docstring del módulo §4."""
    na, nb = _normalizar_nombre(a), _normalizar_nombre(b)
    if not na or not nb:
        return False, 0.0
    if na == nb:
        return True, 1.0
    tokens_a = {t for t in na.split() if len(t) > 1}
    tokens_b = {t for t in nb.split() if len(t) > 1}
    ratio_tokens = 0.0
    if tokens_a and tokens_b:
        overlap = tokens_a & tokens_b
        ratio_tokens = len(overlap) / min(len(tokens_a), len(tokens_b))
    ratio_seq = difflib.SequenceMatcher(None, na, nb).ratio()
    score = max(ratio_tokens, ratio_seq)
    return score >= umbral, round(score, 3)


# ── Tablas ───────────────────────────────────────────────────────────────

def ensure_abono_pendiente_table() -> None:
    from session import db
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS abono_pendientes (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                token               TEXT UNIQUE NOT NULL,
                phone               TEXT NOT NULL,
                paciente_id         TEXT DEFAULT '',
                paciente_nombre     TEXT DEFAULT '',
                rut                 TEXT DEFAULT '',
                monto               INTEGER NOT NULL,
                especialidad        TEXT DEFAULT 'Psiquiatría',
                id_profesional      INTEGER,
                slot_json           TEXT DEFAULT '',
                estado              TEXT DEFAULT 'pendiente',
                creado_at           TEXT DEFAULT (datetime('now')),
                expira_at           TEXT NOT NULL,
                candidata_transferencia_id INTEGER,
                candidatos_siguientes_json TEXT DEFAULT '',
                foto_pedida         INTEGER DEFAULT 0,
                confirmado_at       TEXT DEFAULT '',
                confirmado_por      TEXT DEFAULT '',
                id_cita             TEXT DEFAULT '',
                nota                TEXT DEFAULT '',
                updated_at          TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_abono_pend_token  ON abono_pendientes(token)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_abono_pend_phone  ON abono_pendientes(phone)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_abono_pend_estado ON abono_pendientes(estado)")
        conn.commit()


def ensure_transferencias_table() -> None:
    """Registro de TODA transferencia detectada a la cuenta del CMC, se haya
    asignado o no — reusable por el módulo de conciliación (`GET` de solo
    lectura, no lo escribe nadie más)."""
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
        conn.commit()


# ── Creación de un abono pendiente (llamado desde flows.py) ───────────────

# Horario en que hay alguien en el centro. Fuera de esto nadie está mirando:
# si la ventana del abono venciera de madrugada, el paciente que transfirió a
# las 22:00 se encuentra a la mañana con que perdió la hora y no había nadie a
# quien reclamarle. Decisión del dueño 2026-07-29.
_CIERRA_H = 21
_ABRE_H = 9


def calcular_expira(creado: datetime, horas: int | None = None) -> datetime:
    """Cuándo vence la ventana del abono.

    Base: `ABONO_VENTANA_HORAS` (4 h por defecto). La de 90 min quedó corta con
    el primer caso real: un paciente transfirió y mandó el comprobante a los
    95 minutos — quedó fuera por 5.

    Si el vencimiento cae con el centro cerrado (21:00–09:00), se corre a las
    09:00 del día en que vuelve a haber alguien. Vale también para el abono
    tomado de madrugada: vence a las 09:00 de ese mismo día, no a las 04:00.
    """
    from config import ABONO_VENTANA_HORAS
    # timedelta en MINUTOS y sin int(): con int(horas) un wait_min=90 (=1,5 h)
    # se truncaba a 1 h, así que el mensaje prometía 90 minutos y el abono
    # vencía a los 60. Verificado en prod: los 3 abonos de Gastroenterología
    # del 30-jul tienen exactamente 60 min entre creado_at y expira_at.
    minutos = round(float(horas) * 60) if horas else int(ABONO_VENTANA_HORAS) * 60
    exp = creado + timedelta(minutes=minutos)
    if exp.hour >= _CIERRA_H:
        exp = (exp + timedelta(days=1)).replace(hour=_ABRE_H, minute=0, second=0, microsecond=0)
    elif exp.hour < _ABRE_H:
        exp = exp.replace(hour=_ABRE_H, minute=0, second=0, microsecond=0)
    return exp


def abono_vencido(expira_at: str, ahora: datetime | None = None) -> bool:
    """Único lugar que decide si un abono venció. Antes cada archivo comparaba
    contra un 90 escrito a mano (flows, jobs, abono_transferencia) y bastaba
    cambiar uno para que quedaran en desacuerdo."""
    if not expira_at:
        return False
    try:
        exp = datetime.fromisoformat(expira_at)
    except Exception:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=_CL)
    return (ahora or datetime.now(_CL)) > exp


def crear_abono_pendiente(*, phone: str, paciente_id, paciente_nombre: str, rut: str,
                          monto: int, especialidad: str, id_profesional, slot: dict,
                          wait_min: int | None = None) -> dict:
    """Crea el registro y devuelve {'token':, 'url':, 'expira_at':, 'expira_hhmm':}.

    La URL se arma con ABONO_BASE_URL (config.py) + el token — el link que va
    en el mensaje de WhatsApp. `expira_hhmm` ("18:30") sale de acá para que el
    mensaje diga la hora REAL de vencimiento: antes tenía "90 minutos" escrito
    a mano y el plazo efectivo era otro, así que el paciente no tenía forma de
    saber hasta cuándo le servía transferir.
    """
    from session import db
    from config import ABONO_BASE_URL

    ensure_abono_pendiente_table()
    token = secrets.token_urlsafe(24)
    now = datetime.now(_CL)
    expira = calcular_expira(now, horas=(wait_min / 60) if wait_min else None)
    with db() as conn:
        # Dedupe: un paciente = UN abono vivo por profesional/monto. Caso
        # Bryan 2026-08-04: abrió el gate desde 2 números distintos → 2
        # pendientes del mismo RUT; el matcher de correos vio 2 "candidatos"
        # y la pregunta de desambiguación habría ido al teléfono abandonado.
        # El intento más nuevo reemplaza al anterior.
        if rut:
            # RUT normalizado a solo dígitos+DV: el mismo paciente llega una
            # vez como '16216027-3' y otra como '16.216.027-3' (caso Pamela
            # 2026-08-04, abonos 16/18 — la comparación literal no los vio
            # iguales y el dedupe no disparó).
            _rut_norm = re.sub(r"[^0-9Kk]", "", rut).upper()
            conn.execute("""
                UPDATE abono_pendientes
                SET estado='reemplazado', updated_at=datetime('now')
                WHERE estado='pendiente' AND monto=?
                  AND CAST(id_profesional AS TEXT)=CAST(? AS TEXT)
                  AND UPPER(REPLACE(REPLACE(rut,'.',''),'-','')) = ?
            """, (int(monto), id_profesional, _rut_norm))
        conn.execute("""
            INSERT INTO abono_pendientes
                (token, phone, paciente_id, paciente_nombre, rut, monto,
                 especialidad, id_profesional, slot_json, estado, creado_at,
                 expira_at)
            VALUES (?,?,?,?,?,?,?,?,?, 'pendiente', ?, ?)
        """, (
            token, phone, str(paciente_id or ""), paciente_nombre, rut, int(monto),
            especialidad, id_profesional, json.dumps(slot, ensure_ascii=False),
            now.isoformat(), expira.isoformat(),
        ))
        conn.commit()
    return {
        "token": token,
        "url": f"{ABONO_BASE_URL.rstrip('/')}/abono/{token}",
        "expira_at": expira.isoformat(),
        "expira_hhmm": expira.strftime("%H:%M"),
        # True si vence otro día (calcular_expira corre el vencimiento a las
        # 09:00 cuando caería con el centro cerrado) — el mensaje necesita
        # decir "mañana a las 09:00", no un "09:00" a secas que se leería como
        # una hora que ya pasó.
        "expira_otro_dia": expira.date() != now.date(),
        # Días exactos de diferencia. Con la ventana de 24 h el vencimiento cae
        # normalmente al día siguiente, pero si además se corre a las 09:00 por
        # horario cerrado puede quedar a 2 días: ahí "mañana" sería falso.
        "expira_en_dias": (expira.date() - now.date()).days,
        "expira_fecha": expira.strftime("%d/%m"),
    }


def get_abono_pendiente(token: str) -> dict | None:
    from session import db
    ensure_abono_pendiente_table()
    with db() as conn:
        row = conn.execute("SELECT * FROM abono_pendientes WHERE token=?", (token,)).fetchone()
    return dict(row) if row else None


def get_abono_pendiente_activo_por_phone(phone: str) -> dict | None:
    """El más reciente abono NO resuelto de este teléfono (para que
    `procesar_imagen_abono` pueda chequear si ya se confirmó por correo antes
    de procesar la foto)."""
    from session import db
    ensure_abono_pendiente_table()
    with db() as conn:
        row = conn.execute(
            """SELECT * FROM abono_pendientes WHERE phone=?
               AND estado IN ('pendiente','esperando_confirmacion_paciente','confirmado')
               ORDER BY id DESC LIMIT 1""",
            (phone,),
        ).fetchone()
    return dict(row) if row else None


# ── Motor de emparejamiento ─────────────────────────────────────────────────

def _parse_ts_flexible(s: str) -> datetime | None:
    """Timestamp ISO con o sin 'T', con o sin offset → datetime AWARE (los
    naive se asumen hora de Chile, que es como escribe crear_abono_pendiente).
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CL)
    return dt


def _candidatos_pendientes(monto: int, email_dt_iso: str) -> list[dict]:
    """abonos con estado='pendiente' (NO 'esperando_confirmacion_paciente' —
    mientras hay una pregunta en curso no se abre una segunda para el mismo
    abono), mismo monto, y el correo cayó dentro de su ventana. Orden: más
    antiguo primero (para desempatar cuando hay 2+ candidatos, ver §5).

    BUG 2026-08-03 (caso Yendari, abono 10): la ventana se evaluaba con
    comparación de STRINGS en SQL, y `creado_at` ('...T13:44:00-04:00') vs
    email_ts ('... 14:00:48') mezclan 'T'/espacio y offset/naive — la 'T'
    (0x54) es mayor que el espacio (0x20), así que `creado_at <= email_ts`
    daba SIEMPRE falso y el correo del banco jamás encontraba candidatos.
    Ahora la ventana se evalúa en Python con datetimes normalizados."""
    from session import db
    ensure_abono_pendiente_table()
    email_dt = _parse_ts_flexible(email_dt_iso)
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM abono_pendientes
               WHERE estado='pendiente' AND monto=?
               ORDER BY creado_at ASC""",
            (monto,),
        ).fetchall()
    if email_dt is None:
        return [dict(r) for r in rows]
    out = []
    for r in rows:
        d = dict(r)
        creado = _parse_ts_flexible(d.get("creado_at") or "")
        expira = _parse_ts_flexible(d.get("expira_at") or "")
        if creado and creado > email_dt:
            continue
        if expira and expira < email_dt:
            continue
        out.append(d)
    return out


def _marcar_estado_abono(abono_id: int, estado: str, **campos) -> bool:
    """UPDATE atómico WHERE estado_anterior conocido — evita la carrera
    fotocomprobante-vs-correo (ver docstring §convivencia)."""
    from session import db
    set_cols = ["estado=?", "updated_at=datetime('now')"]
    vals = [estado]
    for k, v in campos.items():
        set_cols.append(f"{k}=?")
        vals.append(v)
    vals.append(abono_id)
    with db() as conn:
        cur = conn.execute(f"UPDATE abono_pendientes SET {', '.join(set_cols)} WHERE id=?", vals)
        conn.commit()
        return cur.rowcount > 0


def _tomar_abono_atomico(abono_id: int, desde_estado: str, hacia_estado: str, **campos) -> bool:
    """Solo transiciona si el estado actual es exactamente `desde_estado`.
    Rowcount 0 = alguien más (la foto, u otro correo) ya lo tomó primero."""
    from session import db
    set_cols = ["estado=?", "updated_at=datetime('now')"]
    vals = [hacia_estado]
    for k, v in campos.items():
        set_cols.append(f"{k}=?")
        vals.append(v)
    vals += [abono_id, desde_estado]
    with db() as conn:
        cur = conn.execute(
            f"UPDATE abono_pendientes SET {', '.join(set_cols)} WHERE id=? AND estado=?", vals
        )
        conn.commit()
        return cur.rowcount > 0


async def _crear_cita_y_confirmar(abono: dict, monto_recibido: int, metodo_detalle: str,
                                  codigo_operacion: str | None, banco_origen: str | None,
                                  confirmado_por: str) -> bool:
    """Crea la cita en Medilink, registra el abono en `abonos_cmc` (misma
    tabla que usa recepción y el camino de la foto — panel único), avisa al
    paciente por WhatsApp, y libera la sesión. Devuelve True si la cita quedó
    creada. Réplica deliberada (no reuso directo) de la sección final de
    `flows.procesar_imagen_abono` — evento distinto (correo/timer vs imagen
    entrante), mismo resultado. Si algún día se quiere unificar, extraer un
    helper común es tarea aparte, documentada como deuda en el reporte."""
    import asyncio
    from datetime import datetime as _dt
    from medilink import crear_cita
    from session import db, save_session, reset_session, log_event, log_message

    slot = json.loads(abono["slot_json"] or "{}")
    phone = abono["phone"]

    try:
        resultado_ml = await asyncio.wait_for(crear_cita(
            id_paciente=abono["paciente_id"],
            id_profesional=slot.get("id_profesional"),
            fecha=slot.get("fecha"),
            hora_inicio=slot.get("hora_inicio"),
            hora_fin=slot.get("hora_fin"),
            id_recurso=slot.get("id_recurso", 1),
            modalidad="TELEMEDICINA",
        ), timeout=45)
    except Exception as e:
        log.error("_crear_cita_y_confirmar: crear_cita falló abono_id=%s: %s", abono["id"], e)
        resultado_ml = None

    if not resultado_ml:
        # ¿El "tope" es una cita del MISMO paciente? Recepción puede haberla
        # agendado a mano en paralelo (caso real Yendari 2026-08-03: el
        # rescate creó un DUPLICADO a otra hora por no mirar esto). Si el
        # paciente ya tiene cita vigente ese día con ese profesional, el
        # abono se confirma contra ESA cita — jamás se crea una segunda.
        _cita_existente = None
        try:
            from medilink import listar_citas_paciente
            _citas_p = await asyncio.wait_for(listar_citas_paciente(
                int(abono["paciente_id"]), rut=abono.get("rut")), timeout=30)
            for _c in _citas_p or []:
                if (str(_c.get("fecha")) == str(slot.get("fecha"))
                        and _c.get("id_profesional") == slot.get("id_profesional")
                        and not _c.get("estado_anulacion")):
                    _cita_existente = _c
                    break
        except Exception as _e_ce:  # noqa: BLE001
            log.warning("check cita existente fallo abono_id=%s: %s",
                        abono["id"], _e_ce)
        if _cita_existente:
            log_event(phone, "abono_confirmado_contra_cita_existente", {
                "abono_id": abono["id"], "id_cita": _cita_existente.get("id"),
                "hora": _cita_existente.get("hora_inicio")})
            # La hora real es la de la cita existente — que el mensaje de
            # confirmación al paciente diga ESA, no la del slot original.
            if _cita_existente.get("hora_inicio"):
                slot["hora_inicio"] = str(_cita_existente["hora_inicio"])[:5]
            resultado_ml = {"id": _cita_existente.get("id")}
        else:
            # Slot ya no disponible — no hay reintento automático acá (a
            # diferencia de procesar_imagen_abono, que SÍ re-busca porque está
            # respondiendo en vivo al paciente). Deja el abono pendiente:
            # cuando expire su ventana cae al flujo de la foto/humano.
            log_event(phone, "abono_email_slot_no_disponible", {"abono_id": abono["id"]})
            _marcar_estado_abono(abono["id"], "pendiente", nota="slot no disponible al confirmar por correo")
            return False

    id_cita = str(resultado_ml.get("id", "")) if isinstance(resultado_ml, dict) else ""
    now_cl = _dt.now(_CL)
    precio_total = int(abono["monto"])
    saldo = max(precio_total - monto_recibido, 0)

    try:
        from abonos_routes import ensure_abonos_table
        ensure_abonos_table()
        with db() as conn:
            conn.execute(
                """INSERT INTO abonos_cmc
                   (fecha, hora, paciente_nombre, rut, id_profesional, profesional,
                    area, fecha_cita, precio_total, monto_abono, saldo,
                    metodo_pago, codigo_transferencia, estado, id_cita, nota,
                    creado_por, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
                (
                    now_cl.strftime("%Y-%m-%d"), now_cl.strftime("%H:%M"),
                    abono["paciente_nombre"], abono["rut"], slot.get("id_profesional"),
                    slot.get("profesional", ""), abono.get("especialidad", "Psiquiatría"),
                    slot.get("fecha_display", slot.get("fecha", "")), precio_total,
                    monto_recibido, saldo, "transferencia", codigo_operacion or "",
                    "pendiente", id_cita,
                    f"auto-email: {metodo_detalle} — banco {banco_origen or '?'}",
                    "bot_email",
                ),
            )
            conn.commit()
    except Exception as e:
        log.warning("_crear_cita_y_confirmar: INSERT abonos_cmc falló: %s", e)

    _marcar_estado_abono(
        abono["id"], "confirmado",
        confirmado_at=now_cl.isoformat(), confirmado_por=confirmado_por, id_cita=id_cita,
    )
    log_event(phone, "abono_email_confirmado", {
        "abono_id": abono["id"], "id_cita": id_cita, "monto": monto_recibido,
        "confirmado_por": confirmado_por,
    })

    # Avisar al paciente — solo si su sesión sigue en el gate (si ya se fue a
    # otro flujo por su cuenta, no lo interrumpimos con un reset agresivo).
    try:
        from session import get_session
        sess = get_session(phone)
        if sess and sess.get("state") == "WAIT_ABONO_COMPROBANTE":
            reset_session(phone)
    except Exception:
        pass

    nombre_corto = (abono["paciente_nombre"] or "").split(" ")[0] if abono["paciente_nombre"] else ""
    saludo = f"*{nombre_corto}*" if nombre_corto else "Tu hora"
    saldo_fmt = f"${saldo:,}".replace(",", ".")
    confirmacion = (
        f"✅ {saludo}, recibimos tu transferencia y tu hora de Psiquiatría quedó "
        f"*confirmada*.\n\n"
        f"👤 {abono['paciente_nombre']}\n"
        f"📅 {slot.get('fecha_display', slot.get('fecha', ''))}\n"
        f"🕐 {(slot.get('hora_inicio') or '')[:5]}\n\n"
        f"Abono recibido: ${monto_recibido:,} CLP\n"
        f"Saldo a pagar el día de la atención: {saldo_fmt} CLP\n\n"
        "_No necesitas mandarnos nada más — quedó todo listo._"
    ).replace(",", ".")

    try:
        from messaging import send_whatsapp
        await send_whatsapp(phone, confirmacion)
        log_message(phone, "out", confirmacion, "IDLE")
    except Exception as e:
        log.error("_crear_cita_y_confirmar: no se pudo notificar a %s: %s", phone, e)

    # Aviso a recepción (visibilidad, no bloquea nada)
    try:
        from config import ADMIN_ALERT_PHONE
        if ADMIN_ALERT_PHONE:
            aviso = (
                f"✅ *Abono Psiquiatría confirmado AUTOMÁTICO por correo bancario*\n"
                f"Paciente: {abono['paciente_nombre']} · WA: {phone}\n"
                f"Cita: {slot.get('fecha_display', slot.get('fecha',''))} "
                f"{(slot.get('hora_inicio') or '')[:5]} (ID Medilink: {id_cita})\n"
                f"Monto: ${monto_recibido:,} · Banco: {banco_origen or '?'} · "
                f"Código: {codigo_operacion or '?'}\n"
                f"Método: {metodo_detalle}"
            ).replace(",", ".")
            from messaging import send_whatsapp as _sw
            await _sw(ADMIN_ALERT_PHONE, aviso)
            log_message(ADMIN_ALERT_PHONE, "out", aviso, "IDLE")
    except Exception as e:
        log.warning("_crear_cita_y_confirmar: aviso a recepción falló: %s", e)

    return True


async def _preguntar_paciente(abono: dict, transferencia_id: int, nombre_pagador: str, monto: int) -> None:
    """Envía Sí/No al paciente. La transferencia queda 'reservada' para este
    abono (candidata_transferencia_id) mientras se espera la respuesta —
    ningún otro abono la puede tomar en ese ínterin."""
    from session import save_session, get_session, log_event, log_message
    from messaging import send_whatsapp_interactive

    phone = abono["phone"]
    _tomar_abono_atomico(
        abono["id"], "pendiente", "esperando_confirmacion_paciente",
        candidata_transferencia_id=transferencia_id,
    )
    monto_fmt = f"${monto:,}".replace(",", ".")
    texto = (
        f"Recibimos una transferencia de *{monto_fmt}* a nombre de *{nombre_pagador}*.\n\n"
        "¿Es la persona que pagó tu reserva de Psiquiatría?"
    )
    try:
        from flows import _btn_msg
        bt = _btn_msg(texto, [
            {"id": "abono_pagador_si", "title": "Sí, es esa"},
            {"id": "abono_pagador_no", "title": "No"},
        ])
        await send_whatsapp_interactive(phone, bt["interactive"])
        log_message(phone, "out", texto, "WAIT_ABONO_PAGADOR_CONFIRM")
    except Exception as e:
        log.error("_preguntar_paciente: no se pudo enviar a %s: %s", phone, e)
        return

    sess = get_session(phone) or {"data": {}}
    data = sess.get("data") or {}
    data["abono_pregunta_id"] = abono["id"]
    data["abono_pregunta_transferencia_id"] = transferencia_id
    save_session(phone, "WAIT_ABONO_PAGADOR_CONFIRM", data)
    log_event(phone, "abono_email_pregunta_pagador", {
        "abono_id": abono["id"], "transferencia_id": transferencia_id, "nombre_pagador": nombre_pagador,
    })


async def resolver_confirmacion_paciente(abono_id: int, transferencia_id: int, respuesta_si: bool) -> str:
    """Llamado desde flows.py cuando el paciente responde Sí/No a la
    pregunta '¿es esta persona?'. Devuelve el texto para responder al
    paciente (flows.py lo retorna tal cual)."""
    from session import db, log_event

    abono = get_abono_pendiente_by_id(abono_id)
    if not abono or abono.get("estado") != "esperando_confirmacion_paciente":
        return "Ya resolvimos ese abono, gracias."

    if not respuesta_si:
        log_event(abono["phone"], "abono_email_pagador_rechazado", {
            "abono_id": abono_id, "transferencia_id": transferencia_id,
        })
        # Libera el abono para que el poller lo siga considerando pendiente
        # (otra transferencia futura, o la foto del comprobante).
        _marcar_estado_abono(abono_id, "pendiente", candidata_transferencia_id=None)
        return (
            "Entendido, gracias por confirmarlo.\n\n"
            "Sigo esperando tu comprobante de transferencia — puedes mandarnos "
            "la *foto* por este chat 📎 en cualquier momento."
        )

    with db() as conn:
        row = conn.execute("SELECT * FROM transferencias_banco WHERE id=?", (transferencia_id,)).fetchone()
    transferencia = dict(row) if row else None
    if not transferencia or transferencia.get("estado_match") == "consumida":
        # La transferencia ya se usó en otro lado (carrera) — no confirmar dos veces.
        _marcar_estado_abono(abono_id, "pendiente", candidata_transferencia_id=None)
        return (
            "Esa transferencia ya quedó asociada a otra reserva — puede haber sido un cruce. "
            "Le avisé a recepción para revisarlo. Si prefieres, manda la *foto* del comprobante 📎"
        )

    ok = await _crear_cita_y_confirmar(
        abono, transferencia["monto"], "correo bancario + confirmación del paciente",
        transferencia.get("codigo_operacion"), transferencia.get("banco"),
        "auto_email_confirmado_paciente",
    )
    if ok:
        with db() as conn:
            conn.execute(
                "UPDATE transferencias_banco SET abono_pendiente_id=?, estado_match='match_confirmado_paciente' WHERE id=?",
                (abono_id, transferencia_id),
            )
            conn.commit()
        return ""  # _crear_cita_y_confirmar ya le mandó la confirmación al paciente
    return (
        "Gracias por confirmarlo. Justo esa hora se ocupó mientras esperábamos — "
        "le avisé a recepción para coordinar contigo. Si prefieres, manda la *foto* "
        "del comprobante y lo resolvemos por ahí también 📎"
    )


def get_abono_pendiente_by_id(abono_id: int) -> dict | None:
    from session import db
    with db() as conn:
        row = conn.execute("SELECT * FROM abono_pendientes WHERE id=?", (abono_id,)).fetchone()
    return dict(row) if row else None


async def procesar_correo_bancario(remitente: str, body: str, email_dt: datetime, uid: int,
                                   subject: str = "") -> dict:
    """Un correo bancario ya identificado (remitente correcto). Parsea,
    registra en `transferencias_banco`, e intenta emparejar contra abonos
    pendientes. Nunca lanza — cualquier fallo se loguea y retorna
    ok=False sin propagar (ver docstring del módulo, degradación)."""
    from session import db

    try:
        parsed = parse_bank_email(remitente, body, subject)
        if not parsed:
            return {"ok": True, "match": False, "motivo": "no_parseable_o_cuenta_ajena"}

        ensure_transferencias_table()
        email_ts_iso = email_dt.isoformat()
        with db() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO transferencias_banco
                    (uid, banco, email_ts, nombre_pagador, monto, fecha, hora, codigo_operacion)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (uid, parsed["banco"], email_ts_iso, parsed["nombre_pagador"], parsed["monto"],
                 parsed.get("fecha") or "", parsed.get("hora") or "", parsed.get("codigo_operacion") or ""),
            )
            conn.commit()
            transferencia_id = cur.lastrowid
            if transferencia_id == 0:
                # ya existía (reproceso del mismo uid) — recuperar su id real
                row = conn.execute("SELECT id FROM transferencias_banco WHERE uid=?", (uid,)).fetchone()
                transferencia_id = row["id"] if row else None

        candidatos = _candidatos_pendientes(parsed["monto"], email_ts_iso)
        if not candidatos:
            return {"ok": True, "match": False, "motivo": "sin_abonos_pendientes", "monto": parsed["monto"]}

        # Filtrar por nombre entre los candidatos con el mismo monto/ventana.
        con_nombre = [c for c in candidatos if nombres_similares(parsed["nombre_pagador"], c["paciente_nombre"])[0]]

        if len(candidatos) == 1 and not con_nombre:
            # 1 solo candidato, nombre no calza → preguntar (no descarta).
            await _preguntar_paciente(candidatos[0], transferencia_id, parsed["nombre_pagador"], parsed["monto"])
            return {"ok": True, "match": "pregunta", "abono_id": candidatos[0]["id"]}

        elegido = con_nombre[0] if len(con_nombre) == 1 else (candidatos[0] if len(candidatos) == 1 else None)

        if elegido:
            tomado = _tomar_abono_atomico(elegido["id"], "pendiente", "confirmando")
            if not tomado:
                return {"ok": True, "match": False, "motivo": "abono_tomado_por_otro_camino"}
            ok = await _crear_cita_y_confirmar(
                elegido, parsed["monto"], f"correo bancario {parsed['banco']}",
                parsed.get("codigo_operacion"), parsed["banco"], "auto_email",
            )
            with db() as conn:
                conn.execute(
                    "UPDATE transferencias_banco SET abono_pendiente_id=?, estado_match=? WHERE id=?",
                    (elegido["id"], "match_automatico" if ok else "sin_match", transferencia_id),
                )
                conn.commit()
            return {"ok": True, "match": ok, "abono_id": elegido["id"]}

        # >1 candidatos y el filtro de nombre no dejó exactamente 1 →
        # preguntar al más antiguo primero (nunca a los dos a la vez).
        await _preguntar_paciente(candidatos[0], transferencia_id, parsed["nombre_pagador"], parsed["monto"])
        restantes = [c["id"] for c in candidatos[1:]]
        if restantes:
            with db() as conn:
                conn.execute(
                    "UPDATE abono_pendientes SET candidatos_siguientes_json=? WHERE id=?",
                    (json.dumps(restantes), candidatos[0]["id"]),
                )
                conn.commit()
        return {"ok": True, "match": "pregunta_ambiguo", "abono_id": candidatos[0]["id"], "n_candidatos": len(candidatos)}

    except Exception as e:
        log.error("procesar_correo_bancario: fallo inesperado (uid=%s): %s", uid, e)
        return {"ok": False, "error": str(e)}


# ── Job del scheduler ────────────────────────────────────────────────────

async def poll_abonos_transferencia() -> dict:
    """Job del scheduler (cada 60s), gateado por ABONO_AUTO_ACTIVE. Reusa el
    mecanismo IMAP genérico de `email_ticker.py` (mismo buzón, cursor propio
    en `system_state` — 'abono_email_last_uid' — para no interferir con el
    cursor del ticker de citas Medilink)."""
    import asyncio
    import email as email_mod
    from zoneinfo import ZoneInfo as _ZI
    from session import system_state_get, system_state_set
    from config import ABONO_AUTO_ACTIVE

    if not ABONO_AUTO_ACTIVE:
        return {"ok": True, "skip": "gated_off"}

    from config import GMAIL_CMC_USER, GMAIL_CMC_APP_PASSWORD
    if not GMAIL_CMC_USER or not GMAIL_CMC_APP_PASSWORD:
        return {"ok": False, "error": "GMAIL_CMC_USER/GMAIL_CMC_APP_PASSWORD no configurados"}

    try:
        from email_ticker import _fetch_new_emails_sync, _seed_cursor_sync

        ensure_transferencias_table()
        ensure_abono_pendiente_table()

        cursor_raw = system_state_get("abono_email_last_uid")
        if cursor_raw is None:
            seed = await asyncio.to_thread(_seed_cursor_sync)
            if seed is None:
                return {"ok": False, "error": "no se pudo sembrar cursor inicial"}
            system_state_set("abono_email_last_uid", str(seed))
            return {"ok": True, "nuevos": 0, "cursor": seed, "cold_start": True}

        cursor = int(cursor_raw)
        raw_emails, new_max = await asyncio.to_thread(_fetch_new_emails_sync, cursor)
        if new_max > cursor:
            system_state_set("abono_email_last_uid", str(new_max))
        if not raw_emails:
            return {"ok": True, "nuevos": 0, "cursor": new_max}

        procesados = 0
        for m in raw_emails:
            if not any(rem.lower() in (m["from"] or "").lower() for rem in REMITENTES_BANCOS):
                continue
            try:
                dt = email_mod.utils.parsedate_to_datetime(m["date_header"])
            except Exception:
                dt = None
            if dt is None:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_ZI("UTC"))
            dt_local = dt.astimezone(_CL)
            await procesar_correo_bancario(m["from"], m["body"], dt_local, m["uid"],
                                           subject=m.get("subject", ""))
            procesados += 1

        return {"ok": True, "nuevos": procesados, "vistos": len(raw_emails), "cursor": new_max}
    except Exception as e:
        log.error("poll_abonos_transferencia: fallo inesperado, no se propaga: %s", e)
        return {"ok": False, "error": str(e)}


async def job_nudge_foto_fallback() -> dict:
    """Complementa el poller: si un abono lleva más de ABONO_AUTO_WAIT_MIN
    minutos pendiente y el correo bancario no llegó (o llegó ambiguo y nadie
    contestó), le pide proactivamente la foto — nunca deja al paciente
    esperando en silencio. Gateado por ABONO_AUTO_ACTIVE."""
    from config import ABONO_AUTO_ACTIVE, ABONO_AUTO_WAIT_MIN, CMC_TELEFONO_FIJO
    if not ABONO_AUTO_ACTIVE:
        return {"ok": True, "skip": "gated_off"}

    from session import db, log_message, log_event

    ensure_abono_pendiente_table()
    corte = (datetime.now(_CL) - timedelta(minutes=ABONO_AUTO_WAIT_MIN)).isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM abono_pendientes
               WHERE estado='pendiente' AND foto_pedida=0 AND creado_at <= ?""",
            (corte,),
        ).fetchall()

    enviados = 0
    for r in rows:
        abono = dict(r)
        _marcar_estado_abono(abono["id"], "pendiente", foto_pedida=1)
        # Especialidad REAL del abono — estaba fija en "Psiquiatría" y el
        # 05-08 una paciente de Gastro recibió "tu hora de Psiquiatría" en
        # pleno paso de pago (hallazgo auditoría conversaciones).
        _esp_nudge = (abono.get("especialidad") or "").strip() or "tu especialidad"
        texto = (
            "Todavía no nos llegó la confirmación de tu transferencia.\n\n"
            "¿Nos mandas una *foto* del comprobante para confirmar tu hora de "
            f"{_esp_nudge}? 📎\n\n"
            f"Si tienes dudas, llama al 📞 *{CMC_TELEFONO_FIJO}*"
        )
        try:
            from messaging import send_whatsapp
            await send_whatsapp(abono["phone"], texto)
            log_message(abono["phone"], "out", texto, "WAIT_ABONO_COMPROBANTE")
            log_event(abono["phone"], "abono_email_nudge_foto", {"abono_id": abono["id"]})
            enviados += 1
        except Exception as e:
            log.warning("job_nudge_foto_fallback: no se pudo avisar a %s: %s", abono["phone"], e)

    return {"ok": True, "enviados": enviados}
