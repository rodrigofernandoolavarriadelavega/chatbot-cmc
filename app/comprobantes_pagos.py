"""Comprobantes de transferencia por WhatsApp → cola de verificación de pagos.

El clasificador de imágenes (eco_orden_ocr, Sonnet visión) detecta
`comprobante_pago` y extrae la estructura (monto, N° operación, banco, cuenta
destino). Este módulo la registra en `comprobantes_whatsapp` con validaciones,
y el panel /alma/comprobantes se la muestra a recepción PRE-cruzada:

  - destinatario_ok  → ¿la cuenta/RUT destino es realmente del CMC?
    (el caso "transfirió a la cuenta equivocada" que la conciliación bancaria
    documenta como problema real — acá se detecta al segundo, no días después)
  - duplicado_de     → mismo N° de operación ya recibido (comprobante repetido
    o reutilizado por otro paciente)
  - paciente + cita  → el comprobante llega POR WhatsApp, así que sabemos de
    qué teléfono viene → perfil (nombre/RUT) → su cita más próxima.
    Ventaja estructural sobre los correos del banco, que llegan "ciegos".

LA PLATA NUNCA SE REGISTRA SOLA: el panel pre-llena el formulario y recepción
confirma con un click (POST /alma/api/pagos, el mismo endpoint de siempre —
hereda atribución, validación de métodos, todo). Este módulo solo encola.

Gated por COMPROBANTES_WHATSAPP_ACTIVE (config.py). Ver validación 2026-08-01:
el comprobante del corpus (Itaú $7.880) se leyó completo, incl. cuenta destino.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("comprobantes_pagos")

# Cuentas y RUT del CMC contra los que se valida el destinatario del
# comprobante. Fuente: comprobante real del corpus 2026-08-01 —
# "Centro Mdico Rodrigo Olavarria · RUT 77.140.898-2 · Banco Itaú · Cta 221708538".
# Ampliable por env sin deploy: COMPROBANTES_CUENTAS_CMC="221708538,otra"
_CUENTAS_CMC = frozenset(
    c.strip() for c in os.getenv(
        "COMPROBANTES_CUENTAS_CMC", "221708538"
    ).split(",") if c.strip()
)
_RUTS_CMC = frozenset(
    r.strip() for r in os.getenv(
        "COMPROBANTES_RUTS_CMC", "771408982"
    ).split(",") if r.strip()
)


def _solo_digitos(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def evaluar_destinatario(cuenta: str, rut: str) -> int | None:
    """1 = destino verificado CMC · 0 = destino NO es del CMC (alerta) ·
    None = el comprobante no traía datos de destinatario legibles."""
    cta = _solo_digitos(cuenta)
    rd = _solo_digitos(rut)
    if not cta and not rd:
        return None
    if cta and cta in _CUENTAS_CMC:
        return 1
    if rd and rd in _RUTS_CMC:
        return 1
    return 0


def ensure_comprobantes_table() -> None:
    """DDL idempotente."""
    from session import db
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS comprobantes_whatsapp (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                phone               TEXT NOT NULL,
                filename            TEXT DEFAULT '',
                file_id             INTEGER,
                monto               INTEGER DEFAULT 0,
                fecha_transf        TEXT DEFAULT '',
                hora_transf         TEXT DEFAULT '',
                banco               TEXT DEFAULT '',
                num_operacion       TEXT DEFAULT '',
                nombre_pagador      TEXT DEFAULT '',
                destinatario_nombre TEXT DEFAULT '',
                destinatario_cuenta TEXT DEFAULT '',
                destinatario_ok     INTEGER,
                duplicado_de        INTEGER,
                paciente_nombre     TEXT DEFAULT '',
                paciente_rut        TEXT DEFAULT '',
                cita_especialidad   TEXT DEFAULT '',
                cita_fecha          TEXT DEFAULT '',
                cita_hora           TEXT DEFAULT '',
                confianza           TEXT DEFAULT '',
                estado              TEXT DEFAULT 'pendiente',
                pago_id             INTEGER,
                created_at          TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cw_estado ON comprobantes_whatsapp(estado)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cw_numop  ON comprobantes_whatsapp(num_operacion)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cw_phone  ON comprobantes_whatsapp(phone)")


def registrar_comprobante(phone: str, datos: dict, filename: str,
                          confianza: str = "") -> dict:
    """Encola un comprobante leído por visión, con validaciones pre-cruzadas.

    `datos` es el sub-objeto "comprobante" de la extracción de visión
    (puede venir incompleto — se registra igual: recepción ve la foto).
    Retorna la fila creada (dict) para logging del caller.
    """
    ensure_comprobantes_table()
    from session import db, get_profile

    try:
        monto = int(datos.get("monto") or 0)
    except (TypeError, ValueError):
        monto = 0
    num_op = str(datos.get("num_operacion") or "").strip()

    dest_ok = evaluar_destinatario(
        str(datos.get("destinatario_cuenta") or ""),
        str(datos.get("destinatario_rut") or ""),
    )

    # Perfil del que envía (teléfono → nombre/RUT conocidos por el bot)
    paciente_nombre, paciente_rut = "", ""
    try:
        perfil = get_profile(phone)
        if perfil:
            paciente_nombre = perfil.get("nombre") or ""
            paciente_rut = perfil.get("rut") or ""
    except Exception:  # noqa: BLE001
        pass

    with db() as conn:
        # Duplicado: mismo N° de operación ya en cola (de cualquier teléfono)
        duplicado_de = None
        if num_op:
            row = conn.execute(
                "SELECT id FROM comprobantes_whatsapp WHERE num_operacion=? "
                "ORDER BY id LIMIT 1", (num_op,)
            ).fetchone()
            if row:
                duplicado_de = row[0]

        # Cita más próxima del teléfono (ayer en adelante, cubre "pagó después")
        cita_esp = cita_fecha = cita_hora = ""
        row = conn.execute(
            "SELECT especialidad, fecha, hora FROM citas_bot "
            "WHERE phone=? AND fecha >= date('now','-1 day') "
            "ORDER BY fecha, hora LIMIT 1", (phone,)
        ).fetchone()
        if row:
            cita_esp, cita_fecha, cita_hora = row[0] or "", row[1] or "", row[2] or ""

        # file_id para servir la foto vía /admin/api/file/{id} (ya existente)
        file_id = None
        if filename:
            row = conn.execute(
                "SELECT id FROM patient_files WHERE phone=? AND filename=? "
                "ORDER BY id DESC LIMIT 1", (phone, filename)
            ).fetchone()
            if row:
                file_id = row[0]

        cur = conn.execute(
            """INSERT INTO comprobantes_whatsapp
               (phone, filename, file_id, monto, fecha_transf, hora_transf,
                banco, num_operacion, nombre_pagador, destinatario_nombre,
                destinatario_cuenta, destinatario_ok, duplicado_de,
                paciente_nombre, paciente_rut, cita_especialidad, cita_fecha,
                cita_hora, confianza)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (phone, filename, file_id, monto,
             str(datos.get("fecha") or ""), str(datos.get("hora") or ""),
             str(datos.get("banco") or ""), num_op,
             str(datos.get("nombre_pagador") or ""),
             str(datos.get("destinatario_nombre") or ""),
             str(datos.get("destinatario_cuenta") or ""),
             dest_ok, duplicado_de, paciente_nombre, paciente_rut,
             cita_esp, cita_fecha, cita_hora, confianza),
        )
        fila_id = cur.lastrowid

    fila = {
        "id": fila_id, "phone": phone, "monto": monto,
        "num_operacion": num_op, "destinatario_ok": dest_ok,
        "duplicado_de": duplicado_de, "paciente_nombre": paciente_nombre,
        "cita": f"{cita_esp} {cita_fecha} {cita_hora}".strip(),
    }
    log.info("COMPROBANTE_WHATSAPP encolado id=%s monto=%s dest_ok=%s dup=%s",
             fila_id, monto, dest_ok, duplicado_de)
    return fila


def listar_comprobantes(estado: str | None = None, limit: int = 60) -> list[dict]:
    ensure_comprobantes_table()
    from session import db
    q = "SELECT * FROM comprobantes_whatsapp"
    args: list = []
    if estado:
        q += " WHERE estado=?"
        args.append(estado)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        rows = conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]


def marcar_comprobante(comp_id: int, estado: str, pago_id: int | None = None) -> bool:
    """estado: confirmado | descartado | pendiente."""
    if estado not in ("confirmado", "descartado", "pendiente"):
        return False
    from session import db
    with db() as conn:
        conn.execute(
            "UPDATE comprobantes_whatsapp SET estado=?, pago_id=? WHERE id=?",
            (estado, pago_id, comp_id),
        )
    return True
