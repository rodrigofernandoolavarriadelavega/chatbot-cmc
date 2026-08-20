"""transferencias_email_parser.py — Parsers de avisos de transferencia bancaria.

Qué resuelve: el CMC recibe un correo automático cada vez que un banco
distinto (el del PACIENTE, no el del centro) ejecuta una transferencia hacia
la cuenta del CMC en Banco Itaú (0-002-21-70853-8, RUT 77.140.898-2) —es la
funcionalidad "aviso de transferencia con notificación al destinatario" que
ofrecen casi todos los bancos chilenos. Este módulo extrae de cada correo:
banco emisor, nombre de quien transfiere, monto, fecha, hora (si viene) y
número de operación (si viene).

Hallazgo relevante (2026-07-14, auditoría previa al backfill): el buzón NO
tiene avisos de solo 3 bancos — tiene de al menos 13. BancoEstado por sí solo
(`noreply@correo.bancoestado.cl`) es EL de mayor volumen histórico, muy por
encima de Santander/Falabella/Banco de Chile combinados. Cubrir solo 3 bancos
habría dejado fuera la mayoría de la plata real. Bancos con parser propio en
este módulo (ordenados por volumen visto en el buzón real, 2026-07-14):
BancoEstado, Banco Falabella, Banco de Chile, Scotiabank, Santander, BCI,
Banco Ripley, Coopeuch, Tenpo, Mach, Caja Los Andes, Prepago Los Héroes.
Bancos de volumen marginal vistos pero SIN parser (Banco Security, Banco
Consorcio, CopecPay, Itaú-persona-natural — <3 correos cada uno en 2026-07-14)
caen en `identificar_banco() -> None` y quedan registrados como "sin parser"
en `transferencias_banco_errores`, nunca se descartan en silencio.

Cada banco tiene, además, variantes de plantilla que cambiaron con los años
(Santander y Falabella al menos tienen una plantilla "legada" ~2022-2023 con
diseño de tabla transpuesta, distinta de la actual). Los parsers intentan la
plantilla vigente primero y caen a la legada si la primera no encuentra los
campos núcleo (nombre, monto, fecha).

Degradación: `parse_email()` NUNCA lanza. Si no logra extraer los 3 campos
núcleo (nombre, monto, fecha) devuelve None — el llamador debe loguear el
correo como no-parseado (uid, banco, asunto) y seguir con el siguiente. Un
correo con formato inesperado no puede tumbar el backfill ni el poller.

Reuso de infraestructura IMAP: `email_ticker.py` (lector de correos de citas
Medilink, mismo buzón) ya resolvió conexión IMAP + extracción de cuerpo de
correo + credenciales (`config.GMAIL_CMC_USER`/`GMAIL_CMC_APP_PASSWORD`) —
este módulo reusa esas funciones (`_connect_imap`, `_get_body_text`,
`_decode_subject`) en vez de reimplementarlas. Cada módulo mantiene su propio
cursor de sincronización (claves distintas en `system_state`) porque cada uno
filtra remitentes/asuntos completamente distintos del mismo buzón — no hay
beneficio en compartir cursor y sí riesgo de acoplar dos dominios de negocio
que no tienen relación (agendamiento vs. conciliación financiera).
"""
from __future__ import annotations

import re
import unicodedata

# ── Identificación de banco por remitente ───────────────────────────────────
# Cada banco puede tener más de un remitente conocido (variantes históricas).
BANCOS_REMITENTES: dict[str, tuple[str, ...]] = {
    "itau":        ("transferencias@itau.cl",),
    "bancoestado": ("noreply@correo.bancoestado.cl", "noreply@bancoestado.cl",
                     "sendmail@bancoestado.cl", "notificaciones@correo.bancoestado.cl"),
    "falabella":   ("notificaciones@cl.bancofalabella.com",),
    "bancochile":  ("serviciodetransferencias@bancochile.cl",),
    "scotiabank":  ("avisos.info@scotiabank.cl", "comprobantes.info@scotiabank.cl",
                     "informaciones@scotiabank.cl"),
    "santander":   ("mensajeria@santander.cl", "mensajes@santander.cl"),
    "bci":         ("transferencias@bci.cl", "contacto@bci.cl"),
    "ripley":      ("informaciones@bancoripley.cl",),
    "coopeuch":    ("notificaciones@transaccionalcoopeuch.com",),
    "tenpo":       ("no-reply@tenpo.cl",),
    "mach":        ("noreply@somosmach.com",),
    "losandes":    ("no.reply@losandesprepago.cl",),
    "losheroes":   ("transferenciaprepago@losheroes.cl",),
}

# Nombre legible para el dashboard.
BANCOS_LABEL: dict[str, str] = {
    "itau": "Itaú",
    "bancoestado": "BancoEstado", "falabella": "Banco Falabella",
    "bancochile": "Banco de Chile", "scotiabank": "Scotiabank",
    "santander": "Santander", "bci": "BCI", "ripley": "Banco Ripley",
    "coopeuch": "Coopeuch", "tenpo": "Tenpo", "mach": "Mach",
    "losandes": "Caja Los Andes", "losheroes": "Prepago Los Héroes",
}


def identificar_banco(from_header: str) -> str | None:
    """Devuelve la clave de banco según el remitente, o None si no es un
    banco conocido (correo irrelevante para conciliación)."""
    f = (from_header or "").lower()
    for banco, remitentes in BANCOS_REMITENTES.items():
        if any(r in f for r in remitentes):
            return banco
    return None


# ── Helpers comunes de extracción ───────────────────────────────────────────

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# Frases de pie de página que a veces "sangran" hacia el campo Mensaje/Motivo
# cuando el remitente lo dejó vacío (la línea que sigue es la del disclaimer).
_MENSAJE_RUIDO = (
    "cordialmente", "scotiabank", "protege tu informaci", "informese",
    "infórmese", "por favor no respond", "nunca te", "nunca solicitaremos",
    "app banco", "consejos", "comprometidos",
)


def _limpiar_mensaje(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    low = s.lower()
    if any(low.startswith(n) for n in _MENSAJE_RUIDO):
        return None
    if low in ("sin mensaje", "sin mensaje.", "-"):
        return None
    return s


def _monto(text: str, pat: str = r'\$\s*([\d\.]{1,15})') -> int | None:
    m = re.search(pat, text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(".", ""))
    except ValueError:
        return None


def _fecha_ddmmyyyy(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', s)
    if not m:
        return None
    d, mo, y = m.groups()
    try:
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    except ValueError:
        return None


def _fecha_texto(dia: str, mes_nombre: str, anio: str) -> str | None:
    mo = _MESES.get((mes_nombre or "").strip().lower())
    if not mo:
        return None
    try:
        return f"{anio}-{mo:02d}-{int(dia):02d}"
    except ValueError:
        return None


def _normalizar_nombre(s: str | None) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# Charclase de nombre: mayúscula inicial + mezcla de mayúsculas/minúsculas +
# acentos + espacios/puntos (los bancos no son consistentes con el casing).
_NOMBRE = r"[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ\s\.]+?"


# ── Parsers por banco ────────────────────────────────────────────────────

def _parse_santander(text: str, subject: str) -> dict:
    m_nombre = re.search(
        rf'nuestro cliente\s+({_NOMBRE})\s+(?:realiz[oó]|ha\s+instru[ií][dg][oó])\s+una\s+transferencia',
        text)
    m_fecha = re.search(r'con fecha (\d{1,2}/\d{1,2}/\d{4})', text)
    monto = _monto(text, r'Monto transferido\s*\n?\s*\$\s*([\d\.]{1,15})')
    if monto is None:
        # Plantilla legada (~2022-2023): tabla transpuesta. Tras "Monto de la
        # Operacion:" vienen 2 líneas — la 1ª es el RUT del destinatario, la
        # 2ª es el monto.
        m_legacy = re.search(
            r'Monto de la Operaci[oó]n:\s*\n[\d\.\-]+\s*\n([\d\.]+)', text)
        if m_legacy:
            monto = int(m_legacy.group(1).replace(".", ""))
    return {
        "banco": "santander",
        "nombre": m_nombre.group(1).strip() if m_nombre else None,
        "monto": monto,
        "fecha": _fecha_ddmmyyyy(m_fecha.group(1)) if m_fecha else None,
        "hora": None,
        "num_operacion": None,
        "mensaje": None,
    }


def _parse_falabella(text: str, subject: str) -> dict:
    m_nombre = re.search(
        rf'nuestro\(a\)\s+cliente\s+({_NOMBRE})\s+ha\s+instru[ií][dg][oó]',
        text)
    m_fecha = re.search(r'Fecha\s*\n?\s*(\d{1,2}-\d{1,2}-\d{4})', text)
    m_hora = re.search(r'Hora\s*\n?\s*(\d{1,2}:\d{2})', text)
    m_op = re.search(
        r'(?:N[ºo°]\.?\s*de\s*operaci[oó]n|Numero\s*de\s*operaci[oó]n)\s*\n?\s*(\d+)',
        text)
    monto = _monto(text, r'Monto transferencia\s*\n?\s*\$\s*([\d\.]{1,15})')
    return {
        "banco": "falabella",
        "nombre": m_nombre.group(1).strip() if m_nombre else None,
        "monto": monto,
        "fecha": _fecha_ddmmyyyy(m_fecha.group(1)) if m_fecha else None,
        "hora": m_hora.group(1) if m_hora else None,
        "num_operacion": m_op.group(1) if m_op else None,
        "mensaje": None,
    }


def _parse_bancochile(text: str, subject: str) -> dict:
    m_nombre = re.search(
        rf'nuestro\(a\)\s+cliente\s+({_NOMBRE})\s+ha\s+efectuado', text)
    if not m_nombre:
        # Plantilla "Le informamos que\nNOMBRE\n le ha transferido": el nombre
        # va en su PROPIA línea, no pegado a la frase. Buscarlo en la misma
        # línea descartaba estos avisos aunque el texto estuviera completo.
        m_nombre = re.search(
            rf'Le informamos que\s*\n\s*({_NOMBRE})\s*\n\s*le ha transferido', text)
    m_op = re.search(r'Número de comprobante\s*\n?\s*(\S+)', text)
    m_fechahora = re.search(
        r'Fecha y Hora:\s*\n?\s*\w+ (\d{1,2}) de (\w+) de (\d{4}) (\d{1,2}:\d{2})', text)
    monto = _monto(text)
    fecha = hora = None
    if m_fechahora:
        d, mes, y, h = m_fechahora.groups()
        fecha = _fecha_texto(d, mes, y)
        hora = h
    if fecha is None:
        m_fecha_simple = re.search(r'^Fecha\s*\n?\s*(\d{1,2}/\d{1,2}/\d{4})', text, re.M)
        if m_fecha_simple:
            fecha = _fecha_ddmmyyyy(m_fecha_simple.group(1))
    return {
        "banco": "bancochile",
        "nombre": m_nombre.group(1).strip() if m_nombre else None,
        "monto": monto,
        "fecha": fecha,
        "hora": hora,
        "num_operacion": m_op.group(1) if m_op else None,
        "mensaje": None,
    }


def _parse_bancoestado(text: str, subject: str) -> dict:
    # Plantilla vigente (2024+): "Aviso de envío o recepción de dinero".
    m_nombre = re.search(
        rf'de nuestro\(a\) cliente\s+({_NOMBRE}),\s*con los siguientes datos', text)
    m_fechahora = re.search(r'hoy (\d{1,2}/\d{1,2}/\d{4}) (\d{1,2}:\d{2}):\d{2}', text)
    m_op = re.search(r'Número de Operación:\s*(\d+)', text)
    m_msg = re.search(r'Comentario:(.*)', text)
    monto = _monto(text, r'Monto transferido:\s*\n?\s*\$\s*([\d\.]{1,15})')
    fecha = hora = None
    if m_fechahora:
        fecha = _fecha_ddmmyyyy(m_fechahora.group(1))
        hora = m_fechahora.group(2)
    if m_nombre and monto and fecha:
        return {
            "banco": "bancoestado", "nombre": m_nombre.group(1).strip(),
            "monto": monto, "fecha": fecha, "hora": hora,
            "num_operacion": m_op.group(1) if m_op else None,
            "mensaje": _limpiar_mensaje(m_msg.group(1) if m_msg else None),
        }
    # Plantilla legada (hasta ~2023): "Aviso de Transferencia de Fondos".
    m_nombre2 = re.search(
        rf'de nuestro\(a\) cliente\s+({_NOMBRE})\s*\nDatos', text)
    # Los segundos son OPCIONALES (2026-07-28). El correo llega indistintamente
    # como "Fecha y hora26/08/2025 14:59:57" y como "Fecha y hora05/05/2025 14:45";
    # exigir :SS descartaba silenciosamente la segunda forma. Era la causa de 34
    # de los 54 avisos del CMC que el cruce no veía.
    m_fechahora2 = re.search(
        r'Fecha y hora\s*(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2})(?::\d{2})?', text)
    m_op2 = re.search(r'N° transaccion(\d+)', text)
    m_msg2 = re.search(r'Mensaje(.*)', text)
    monto2 = _monto(text, r'Monto\$\s*([\d\.]{1,15})')
    fecha2 = hora2 = None
    if m_fechahora2:
        fecha2 = _fecha_ddmmyyyy(m_fechahora2.group(1))
        hora2 = m_fechahora2.group(2)
    # Tercera plantilla: "Hemos realizado una Transferencia instruida por nuestro
    # cliente X". Llega con el HTML SIN limpiar (el texto plano viene vacío y el
    # extractor cae al html crudo), así que el nombre viene envuelto en <strong>
    # y la fecha en su propia línea como "Fecha DD/MM/YYYY - HH:MM".
    if not (m_nombre2 and monto2 and fecha2):
        m_n3 = re.search(r'instruida por nuestro cliente\s*(?:<[^>]+>)*\s*'
                         rf'({_NOMBRE})\s*(?:</[^>]+>|<)', text)
        m_f3 = re.search(r'Fecha\s+(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}:\d{2})', text)
        m_m3 = _monto(text, r'Monto\s*(?:<[^>]+>)*\s*\$?\s*([\d\.]{1,15})')
        if m_n3 and m_f3:
            return {
                "banco": "bancoestado", "nombre": m_n3.group(1).strip(),
                "monto": m_m3 or monto2,
                "fecha": _fecha_ddmmyyyy(m_f3.group(1)), "hora": m_f3.group(2),
                "num_operacion": m_op2.group(1) if m_op2 else None,
                "mensaje": None,
            }

    return {
        "banco": "bancoestado",
        "nombre": m_nombre2.group(1).strip() if m_nombre2 else None,
        "monto": monto2,
        "fecha": fecha2,
        "hora": hora2,
        "num_operacion": m_op2.group(1) if m_op2 else None,
        "mensaje": _limpiar_mensaje(m_msg2.group(1) if m_msg2 else None),
    }


def _parse_scotiabank(text: str, subject: str) -> dict:
    m_nombre = re.search(
        rf'nuestros?\s+clientes?\s+el\s+Sr\(a\)\s+({_NOMBRE})\s+ha\s+instru[ií][dg][oó]',
        text)
    m_fecha = re.search(r'Con fecha de hoy (\d{1,2}/\d{1,2}/\d{4})', text)
    m_msg = re.search(r'Mensaje\s*:\s*\n?(.*)', text)
    monto = _monto(text, r'Monto\s*:\s*\n?\s*\$?\s*([\d\.]{1,15})')
    return {
        "banco": "scotiabank",
        "nombre": m_nombre.group(1).strip() if m_nombre else None,
        "monto": monto,
        "fecha": _fecha_ddmmyyyy(m_fecha.group(1)) if m_fecha else None,
        "hora": None,
        "num_operacion": None,
        "mensaje": _limpiar_mensaje(m_msg.group(1) if m_msg else None),
    }


def _parse_bci(text: str, subject: str) -> dict:
    # Plantillas antiguas (~2021-2022) insertan un salto de línea ENTRE CADA
    # PALABRA de la frase fija (probablemente <span> por palabra en el HTML
    # original) — por eso cada espacio literal de la frase se reemplaza por
    # \s+ y no solo los que rodean el nombre.
    m_nombre = re.search(
        rf'Has\s+recibido\s+una\s+transferencia\s+de\s+fondos\s+de\s+({_NOMBRE})\s+hacia\s+tu\s+cuenta',
        text)
    m_fecha = re.search(r'Fecha de la transferencia\s*\n?\s*(\d{1,2}/\d{1,2}/\d{4})', text)
    m_op = re.search(r'Número de comprobante\s*\n?\s*(\d+)', text)
    monto = _monto(text, r'Monto recibido\s*\n?\s*\$\s*([\d\.]{1,15})')
    return {
        "banco": "bci",
        "nombre": m_nombre.group(1).strip() if m_nombre else None,
        "monto": monto,
        "fecha": _fecha_ddmmyyyy(m_fecha.group(1)) if m_fecha else None,
        "hora": None,
        "num_operacion": m_op.group(1) if m_op else None,
        "mensaje": None,
    }


def _parse_ripley(text: str, subject: str) -> dict:
    m_nombre = re.search(
        rf'Nuestro cliente\s+({_NOMBRE})\s+ha realizado una transferencia', text)
    m_fecha = re.search(r'^(\d{1,2}/\d{1,2}/\d{4}) \d{1,2}:\d{2}:\d{2}', text, re.M)
    m_op = re.search(r'Número de Transacción:\s*\n?\s*(\d+)', text)
    m_msg = re.search(r'Comentario:\s*\n?(.*)', text)
    monto = _monto(text, r'Monto Transferido:\s*\n?\s*\$\s*([\d\.]{1,15})')
    return {
        "banco": "ripley",
        "nombre": m_nombre.group(1).strip() if m_nombre else None,
        "monto": monto,
        "fecha": _fecha_ddmmyyyy(m_fecha.group(1)) if m_fecha else None,
        "hora": None,
        "num_operacion": m_op.group(1) if m_op else None,
        "mensaje": _limpiar_mensaje(m_msg.group(1) if m_msg else None),
    }


def _parse_coopeuch(text: str, subject: str) -> dict:
    m_nombre = re.search(r'Nombre:\s*([A-ZÁÉÍÓÚÑ, \.]+)', text)
    m_fechahora = re.search(
        r'^Fecha:\s*(\d{1,2}-\d{1,2}-\d{4})\s*Hora:\s*(\d{1,2}:\d{2})', text, re.M)
    m_op = re.search(r'Transacción Num\s*\n?\s*(\d+)', text)
    m_msg = re.search(r'Motivo:\s*(.*)', text)
    monto = _monto(text, r'Monto\s*\n?\s*\$\s*([\d\.]{1,15})')
    fecha = hora = None
    if m_fechahora:
        fecha = _fecha_ddmmyyyy(m_fechahora.group(1))
        hora = m_fechahora.group(2)
    nombre = None
    if m_nombre:
        # Viene "APELLIDOS, NOMBRES" — se reordena a "NOMBRES APELLIDOS" para
        # que la comparación con pagos_cmc.paciente_nombre sea consistente.
        partes = [p.strip() for p in m_nombre.group(1).split(",")]
        nombre = " ".join(reversed(partes)) if len(partes) == 2 else m_nombre.group(1).strip()
    return {
        "banco": "coopeuch",
        "nombre": nombre,
        "monto": monto,
        "fecha": fecha,
        "hora": hora,
        "num_operacion": m_op.group(1) if m_op else None,
        "mensaje": _limpiar_mensaje(m_msg.group(1) if m_msg else None),
    }


def _parse_tenpo(text: str, subject: str) -> dict:
    m_nombre = re.search(rf'La transferencia de\s+({_NOMBRE})\s+por \$', text)
    m_fecha = re.search(r'Fecha:\s*\n?\s*(\d{1,2}-\d{1,2}-\d{4})', text)
    m_hora = re.search(r'Hora:\s*\n?\s*(\d{1,2}:\d{2})', text)
    m_op = re.search(r'Código de transferencia:\s*\n?\s*(\d+)', text)
    monto = _monto(text, r'Monto transferencia:\s*\n?\s*\$\s*([\d\.]{1,15})')
    return {
        "banco": "tenpo",
        "nombre": m_nombre.group(1).strip() if m_nombre else None,
        "monto": monto,
        "fecha": _fecha_ddmmyyyy(m_fecha.group(1)) if m_fecha else None,
        "hora": m_hora.group(1) if m_hora else None,
        "num_operacion": m_op.group(1) if m_op else None,
        "mensaje": None,
    }


def _parse_mach(text: str, subject: str) -> dict:
    m_nombre = re.search(
        rf'Acabas de recibir una transferencia de\s+({_NOMBRE})\s+sin costo desde', text)
    m_fechahora = re.search(r'^(\d{1,2}/\d{1,2}/\d{4}) - (\d{1,2}:\d{2}):\d{2}', text, re.M)
    m_op = re.search(r'Código de confirmación\s*\n?\s*(\d+)', text)
    monto = _monto(text, r'Monto\s*\n?\s*\$\s*([\d\.]{1,15})')
    fecha = hora = None
    if m_fechahora:
        fecha = _fecha_ddmmyyyy(m_fechahora.group(1))
        hora = m_fechahora.group(2)
    return {
        "banco": "mach",
        "nombre": m_nombre.group(1).strip() if m_nombre else None,
        "monto": monto,
        "fecha": fecha,
        "hora": hora,
        "num_operacion": m_op.group(1) if m_op else None,
        "mensaje": None,
    }


def _parse_losandes(text: str, subject: str) -> dict:
    m_nombre = re.search(
        rf'Te informamos que\s+({_NOMBRE})\s+ha enviado una transferencia', text)
    m_fecha = re.search(r'Fecha:\s*\n?\s*(\d{1,2}/\d{1,2}/\d{4})', text)
    m_op = re.search(r'Código de transacción:\s*\n?\s*(\d+)', text)
    monto = _monto(text, r'Monto:\s*\n?\s*\$\s*([\d\.]{1,15})')
    return {
        "banco": "losandes",
        "nombre": m_nombre.group(1).strip() if m_nombre else None,
        "monto": monto,
        "fecha": _fecha_ddmmyyyy(m_fecha.group(1)) if m_fecha else None,
        "hora": None,
        "num_operacion": m_op.group(1) if m_op else None,
        "mensaje": None,
    }


def _parse_losheroes(text: str, subject: str) -> dict:
    m_nombre = re.search(
        rf'nuestro\(a\) cliente\s+({_NOMBRE})\s+ha efectuado una transferencia', text)
    m_fechahora = re.search(
        r'Fecha y Hora:\s*\n?\s*(\d{1,2}) de (\w+) de (\d{4}) (\d{1,2}:\d{2}):\d{2}', text)
    m_op = re.search(r'Comprobante:\s*\n?\s*(\d+)', text)
    monto = _monto(text, r'MONTO TRANSFERIDO:\s*\$\s*([\d\.]{1,15})')
    fecha = hora = None
    if m_fechahora:
        d, mes, y, h = m_fechahora.groups()
        fecha = _fecha_texto(d, mes, y)
        hora = h
    return {
        "banco": "losheroes",
        "nombre": m_nombre.group(1).strip() if m_nombre else None,
        "monto": monto,
        "fecha": fecha,
        "hora": hora,
        "num_operacion": m_op.group(1) if m_op else None,
        "mensaje": None,
    }


def _parse_itau(text: str, subject: str) -> dict:
    """Itaú — 'Itaú informa.' desde transferencias@itau.cl. Es el banco del
    CMC: el aviso llega cuando el PAGADOR transfiere desde Itaú (mismo banco).
    Plantilla HTML tabular con MUCHO whitespace entre rótulo y valor
    (verificado contra correo real del 14-08-2026, transacción 590197386):
        Datos de la Cuenta de Origen ... Nombre \\n FREDDY MICHAEL ORELLANA ...
        Fecha - Hora \\n 14/08/2026-19:19:13 hrs
        Numero de Transaccion \\n 590197386
        Monto: \\n $41.680
    El destinatario viene con cuenta y RUT completos → _es_cuenta_cmc aplica
    directo (0221708538 contiene 221708538)."""
    m_nombre = re.search(
        r'Datos de la Cuenta de Origen.*?Nombre\s*\n\s*(.+?)\s*\n',
        text, re.S)
    m_fh = re.search(
        r'Fecha\s*-\s*Hora\s*\n?\s*(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}:\d{2})',
        text, re.S)
    m_num = re.search(r'Numero de Transacci[oó]n\s*\n?\s*(\d+)', text, re.S)
    m_msg = re.search(r'Comentario\s*\n\s*(\S.*?)\s*\n', text, re.S)
    monto = _monto(text, r'Monto\s*:?\s*\n?\s*\$\s*([\d\.]{1,15})')
    return {
        "banco": "itau",
        "nombre": re.sub(r"\s+", " ", m_nombre.group(1)).strip() if m_nombre else None,
        "monto": monto,
        "fecha": _fecha_ddmmyyyy(m_fh.group(1)) if m_fh else None,
        "hora": m_fh.group(2) if m_fh else None,
        "num_operacion": m_num.group(1) if m_num else None,
        "mensaje": _limpiar_mensaje(m_msg.group(1) if m_msg else None),
    }


PARSERS = {
    "itau": _parse_itau,
    "santander": _parse_santander,
    "falabella": _parse_falabella,
    "bancochile": _parse_bancochile,
    "bancoestado": _parse_bancoestado,
    "scotiabank": _parse_scotiabank,
    "bci": _parse_bci,
    "ripley": _parse_ripley,
    "coopeuch": _parse_coopeuch,
    "tenpo": _parse_tenpo,
    "mach": _parse_mach,
    "losandes": _parse_losandes,
    "losheroes": _parse_losheroes,
}

# Campos sin los que un registro es inútil para conciliar (no se puede cruzar
# ni reportar como "plata sin registrar" sin al menos monto+fecha; el nombre
# se exige también porque sin él no hay ninguna posibilidad de subir la
# confianza del cruce más adelante).
_CAMPOS_NUCLEO = ("nombre", "monto", "fecha")

# El buzón `centromedicocarampangue@gmail.com` recibe avisos de OTRAS cuentas
# del dueño, no solo la del CMC (hallazgo de `app/abono_transferencia.py`,
# 2026-07-14: en ~5 años de correos, al menos una transferencia de $60.000
# iba a una cuenta BancoEstado de otro negocio, NO al CMC). Sin este filtro
# se contaría como "ingreso del CMC" plata que nunca fue del CMC. Mismo
# criterio que ese módulo: buscar los dígitos de la cuenta o el RUT del CMC
# en cualquier parte del cuerpo (formato-agnóstico — "0-002-21-70853-8",
# "0221708538" y "022 170 8538" deben matchear igual).
_CMC_CUENTA_DIGITS = "0221708538"   # Banco Itaú, 0-002-21-70853-8
_CMC_RUT_DIGITS = "771408982"       # 77.140.898-2


def _es_cuenta_cmc(text: str) -> bool:
    solo_digitos = re.sub(r"\D", "", text or "")
    if _CMC_CUENTA_DIGITS in solo_digitos or _CMC_RUT_DIGITS in solo_digitos:
        return True
    # Algunos bancos (BCI, Ripley, Scotiabank en ciertas plantillas) NO
    # restatean el número de cuenta ni el RUT del destinatario — solo el
    # NOMBRE del banco destino ("hacia tu cuenta del Banco Itau" / "...del
    # BANCO ESTADO"). Verificado 2026-07-14 contra muestra real: mismos
    # remitentes BCI en el buzón traen indistintamente "tu cuenta del Banco
    # ITAU" (sí es el CMC) y "tu cuenta del BANCO ESTADO" (NO es el CMC —
    # es la otra cuenta del dueño). Sin número de cuenta para confirmar, el
    # único criterio disponible es: el ÚNICO banco destino mencionado debe
    # ser Itaú, y ningún otro banco chileno debe aparecer como destino.
    low = _normalizar_nombre(text)
    menciona_itau_destino = bool(re.search(r"cuenta del banco itau|banco de destino\s*banco itau|"
                                            r"institucion\s*banco itau|banco\s*:\s*banco itau", low))
    menciona_otro_destino = bool(re.search(
        r"cuenta del banco estado|banco de destino\s*banco estado|"
        r"institucion\s*banco estado|banco\s*:\s*banco estado", low))
    return menciona_itau_destino and not menciona_otro_destino


def parse_email(banco: str, subject: str, text: str) -> dict | None:
    """Parsea el cuerpo de un correo ya identificado como de banco `banco`.
    Devuelve None (nunca lanza) si no logra extraer los campos núcleo O si
    el correo no es de la cuenta del CMC (ver `_es_cuenta_cmc`)."""
    fn = PARSERS.get(banco)
    if not fn:
        return None
    if not _es_cuenta_cmc(text):
        return None
    try:
        r = fn(text or "", subject or "")
    except Exception:
        return None
    if any(not r.get(k) for k in _CAMPOS_NUCLEO):
        return None
    return r
