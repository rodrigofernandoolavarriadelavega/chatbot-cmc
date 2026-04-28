"""
Auditoría de propiedades estáticas sobre el HISTORIAL de mensajes salientes
del bot (los últimos N días en sessions.db de producción).

Reglas que el bot NUNCA debe romper. Cada vez que se rompe, se imprime el
ejemplo concreto (phone, ts, fragmento). Output amigable para revisar en
una sola pasada.

Uso:
    python scripts/audit_properties.py [--db /opt/chatbot-cmc/data/sessions.db] [--days 7]

Pensado para correr remoto vía SSH:
    ssh root@<server> 'cd /opt/chatbot-cmc && python3 scripts/audit_properties.py'
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Permitir importar app.session (que maneja SQLCipher si la DB está encriptada)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

CHILE_TZ = ZoneInfo("America/Santiago")

# ─────────────────────── Reglas (predicates) ───────────────────────────────

ENGLISH_DAYS = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
ENGLISH_MONTHS = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b",
    re.IGNORECASE,
)
PERSONAL_PHONE = "+56987834148"
PERSONAL_PHONE_NO_PLUS = "56987834148"

# Horarios genéricos del CMC inventados (debe NO aparecer en respuesta a
# pregunta de horario por profesional). Si la respuesta dice "lunes a viernes
# 08:00–21:00 + sábado 09:00–14:00" Y va con un nombre de profesional, es bug.
HORARIO_GENERICO = re.compile(
    r"lunes\s*a\s*viernes\s*(de\s*)?08[:.]00\s*[–\-a]\s*21[:.]00",
    re.IGNORECASE,
)

# Slots ofrecidos en el pasado: regex aproximado para detectar "Te encontré
# hora ✨ ... 📅 lunes 27 de abril ... 🕐 11:40" en una fecha que ya pasó.
SLOT_OFRECIDO_RE = re.compile(
    r"📅\s*\*?(?P<dia>lunes|martes|miércoles|jueves|viernes|sábado|domingo)"
    r"\s+(?P<dnum>\d{1,2})\s+de\s+(?P<mes>enero|febrero|marzo|abril|mayo|junio|"
    r"julio|agosto|septiembre|octubre|noviembre|diciembre).*?"
    r"🕐\s*\*?(?P<hora>\d{1,2}:\d{2})",
    re.IGNORECASE | re.DOTALL,
)
MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}

# Bot diciendo info de pago de tarjeta para atenciones médicas (no dental).
# Ej: "consulta general — débito" / "kine — crédito".
PAGO_TARJETA_MEDICO = re.compile(
    r"(medicina general|kinesiolog|psicolog|nutrici|otorrinolaring|cardio|"
    r"traumat|gastro|matrona|fonoaud|podolog).{0,80}(débito|debito|crédito|credito|"
    r"tarjeta)",
    re.IGNORECASE | re.DOTALL,
)


def _now_cl() -> datetime:
    return datetime.now(CHILE_TZ)


def _check_english_locale(content: str) -> str | None:
    if ENGLISH_DAYS.search(content):
        m = ENGLISH_DAYS.search(content)
        return f"english_day: '{m.group(0)}'"
    if ENGLISH_MONTHS.search(content):
        m = ENGLISH_MONTHS.search(content)
        return f"english_month: '{m.group(0)}'"
    return None


def _check_personal_phone(content: str) -> str | None:
    if PERSONAL_PHONE in content or PERSONAL_PHONE_NO_PLUS in content:
        return f"personal_phone leaked"
    return None


def _check_slot_pasado(content: str, ts: str) -> str | None:
    # Solo considerar el caso donde el bot está OFRECIENDO un slot fresco
    # ("Te encontré hora" + "¿Te la reservo?"). NO los mensajes de confirmación
    # ("te reservo esta hora", "Tu hora sigue apartada") porque el slot ya fue
    # elegido por el paciente y está en flujo de confirmación normal.
    if not ("Te encontré hora" in content and "¿Te la reservo" in content):
        return None
    m = SLOT_OFRECIDO_RE.search(content)
    if not m:
        return None
    try:
        dnum = int(m.group("dnum"))
        mes = MESES.get(m.group("mes").lower())
        hora = m.group("hora")
        if not mes:
            return None
        # Year inferido: el del timestamp del mensaje
        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if "T" in ts else datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        year = ts_dt.year
        h, mi = hora.split(":")
        slot_dt = datetime(year, mes, dnum, int(h), int(mi), tzinfo=CHILE_TZ)
        # Comparar contra el ts del mensaje (cuándo se envió)
        ts_cl = ts_dt.replace(tzinfo=CHILE_TZ) if ts_dt.tzinfo is None else ts_dt.astimezone(CHILE_TZ)
        if slot_dt < ts_cl:
            return f"slot_pasado: ofreció {slot_dt.strftime('%Y-%m-%d %H:%M')} pero ts mensaje {ts_cl.strftime('%Y-%m-%d %H:%M')}"
    except Exception:
        return None
    return None


def _check_horario_generico_por_prof(content: str) -> str | None:
    if not HORARIO_GENERICO.search(content):
        return None
    # Solo cuenta si el mensaje nombra a un profesional específico Y el horario
    # genérico viene acompañado de un verbo que vincula al prof con el horario.
    if not re.search(r"Dr\.?\s|Dra\.?\s|doctor |doctora ", content, re.IGNORECASE):
        return None
    # Patrón clave: "<Dr X> atiende ... lunes a viernes 08:00–21:00"
    # Si el horario aparece SIN verbo de vinculación, es contexto de info
    # general del CMC, no un bug.
    if re.search(
        r"(Dr\.?|Dra\.?|doctor|doctora)\s+\w+.{0,80}atiende.{0,80}lunes\s*a\s*viernes",
        content, re.IGNORECASE | re.DOTALL,
    ):
        return "horario_generico_aplicado_a_prof"
    return None


def _check_pago_tarjeta_medico(content: str) -> str | None:
    if PAGO_TARJETA_MEDICO.search(content):
        return "pago_tarjeta_para_medico"
    return None


PROPIEDADES = [
    ("locale_ingles", _check_english_locale, lambda c, ts: _check_english_locale(c)),
    ("personal_phone", _check_personal_phone, lambda c, ts: _check_personal_phone(c)),
    ("slot_pasado", None, _check_slot_pasado),
    ("horario_generico_por_prof", _check_horario_generico_por_prof, lambda c, ts: _check_horario_generico_por_prof(c)),
    ("pago_tarjeta_medico", _check_pago_tarjeta_medico, lambda c, ts: _check_pago_tarjeta_medico(c)),
]


# ────────────────────────── Main ─────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/sessions.db")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit-violations", type=int, default=10,
                    help="cuántos ejemplos mostrar por propiedad")
    args = ap.parse_args()

    # Usa el conector de app.session (maneja SQLCipher si SQLCIPHER_KEY existe)
    if args.db != "data/sessions.db":
        os.environ["SESSIONS_DB_PATH"] = args.db
    from session import _conn  # type: ignore
    cutoff = (_now_cl() - timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")

    with _conn() as cn:
        rows = cn.execute(
            """
            SELECT phone, ts, text FROM messages
             WHERE direction = 'out'
               AND datetime(ts) > datetime(?)
            """,
            (cutoff,),
        ).fetchall()

    print(f"\n=== Auditoría de propiedades · últimos {args.days}d · {len(rows)} mensajes salientes ===\n")

    violaciones: dict[str, list[tuple]] = defaultdict(list)
    for phone, ts, content in rows:
        if not content:
            continue
        for nombre, _quick, fn in PROPIEDADES:
            try:
                hit = fn(content, ts)
            except Exception:
                hit = None
            if hit:
                violaciones[nombre].append((phone, ts, hit, content[:200]))

    if not violaciones:
        print("✓ Sin violaciones de propiedades en la ventana auditada.\n")
        return 0

    for prop, hits in violaciones.items():
        print(f"\n● {prop}: {len(hits)} casos")
        for phone, ts, detalle, snip in hits[: args.limit_violations]:
            print(f"  · {ts}  phone={phone[-4:]}  {detalle}")
            print(f"    “{snip[:180]}”")
        if len(hits) > args.limit_violations:
            print(f"  … (+{len(hits) - args.limit_violations} más)")

    print()
    return 1 if violaciones else 0


if __name__ == "__main__":
    sys.exit(main())
