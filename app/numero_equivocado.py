"""numero_equivocado.py — Teléfonos reciclados: detección y limpieza.

Caso real 2026-08-06 (Maria Martinez, Medilink 15673): un número de una ficha
antigua hoy pertenece a otra persona; el blast de consent le escribió por su
nombre y el dueño actual respondió "número equivocado" — el bot conversó dos
veces sin marcar nada y recepción tuvo que rescatar + limpieza manual.

Diseño (decisión del dueño 2026-08-06): NO auto-opt-out por texto libre (un
falso positivo silenciaría a un paciente real). El bot solo DETECTA la frase,
responde que dejaremos de escribir, pasa la conversación a recepción
(HUMAN_TAKEOVER con motivo) y recepción confirma con el botón "Nº equivocado"
del panel v2, que ejecuta acá la receta completa de 4 capas:

  1. Medilink (fuente de verdad): PUT /pacientes/{id} borrando celular/telefono
     — si no, el próximo ETL de BI y los recordatorios re-usan el número.
  2. BI: dim_paciente.telefono=NULL + INSERT bi.opt_outs_marketing en AMBOS
     formatos (56… y +56…) — esa lista dura la respetan winback, consent
     blast, custom audiences, campañas estacionales y fidelización.
  3. bi.marketing_consent: cerrar pending → declined / wrong_number.
  4. Local: tag marketing_opt_out + evento de auditoría.

Memoria relacionada: cmc_numeros_reciclados_receta.md.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("numero_equivocado")

_CLT = ZoneInfo("America/Santiago")

# ── Detección ────────────────────────────────────────────────────────────────
# Patrones FUERTES: inequívocos de "este número ya no es de esa persona".
_FUERTES = [
    r"n[uú]mero\s+equivocado",
    r"se\s+equivoc(?:o|ó|aron)\s+de\s+(?:n[uú]mero|tel[eé]fono|celular|fono)",
    r"este\s+(?:celular|n[uú]mero|tel[eé]fono|fono)\s+no\s+es\s+de",
    r"\bno\s+me\s+llamo\b",
    r"\bni\s+l[ao]\s+conozco\b",
    r"no\s+conozco\s+a\s+(?:ninguna|ning[uú]n)",
    r"ya\s+no\s+(?:tiene|usa|ocupa)\s+este\s+n[uú]mero",
    r"n[uú]mero\s+(?:nuevo|reciclado).*(?:due[ñn]o|otra\s+persona)",
]
# Patrones DÉBILES: solo cuentan si el mensaje además habla de número/identidad
# ("están equivocados" a secas puede ser un reclamo por una hora mal agendada).
_DEBILES = [
    r"est[aá]n?\s+equivocad",
    r"no\s+conozco\s+a\s+(?:esa|ese)\b",
]
_CONTEXTO = r"(n[uú]mero|celular|tel[eé]fono|fono\b|no\s+soy\b|no\s+es\s+de|conozco)"

_RE_FUERTES = [re.compile(p) for p in _FUERTES]
_RE_DEBILES = [re.compile(p) for p in _DEBILES]
_RE_CONTEXTO = re.compile(_CONTEXTO)


def detectar_reporte_numero_equivocado(texto_lower: str) -> bool:
    """True si el texto parece un reporte de 'número equivocado'. Conservador:
    prefiere no detectar antes que silenciar a un paciente real por error."""
    if not texto_lower or len(texto_lower) > 400:
        return False
    if any(rx.search(texto_lower) for rx in _RE_FUERTES):
        return True
    if any(rx.search(texto_lower) for rx in _RE_DEBILES) and _RE_CONTEXTO.search(texto_lower):
        return True
    return False


# ── Limpieza (la ejecuta recepción desde el panel, con confirmación) ─────────

def _variantes(phone: str) -> tuple[str, list[str]]:
    """last9 dígitos + variantes de formato con que el número puede estar
    guardado en BI/consent ('569…', '+569…', '9…')."""
    digitos = re.sub(r"\D", "", phone or "")
    last9 = digitos[-9:]
    return last9, [f"56{last9}", f"+56{last9}", last9]


async def limpiar_numero(phone: str, quien: str = "recepcion") -> dict:
    """Ejecuta la receta completa. Idempotente — repetirla no rompe nada."""
    from config import MEDILINK_BASE_URL
    from medilink import HEADERS, _get_shared_client, use_batch_lane
    from winback import bi_conn
    from session import db, log_event

    use_batch_lane()
    last9, variantes = _variantes(phone)
    if len(last9) < 9:
        return {"ok": False, "error": "teléfono demasiado corto"}

    resultado: dict = {"ok": True, "phone": phone, "pacientes": [], "acciones": []}

    # 1) localizar en BI todas las fichas que usan este número
    with bi_conn() as pg:
        with pg.cursor() as cur:
            cur.execute(
                "SELECT paciente_id, nombre, apellido, telefono FROM bi.dim_paciente "
                "WHERE telefono LIKE %s", (f"%{last9}",))
            fichas = cur.fetchall()

    # 2) Medilink: borrar el número de cada ficha (celular y/o telefono)
    client = _get_shared_client()
    for pid, nombre, apellido, _tel in fichas:
        nombre_full = " ".join(x for x in [nombre, apellido] if x)
        info = {"paciente_id": pid, "nombre": nombre_full, "medilink": "sin_cambio"}
        try:
            r = await client.get(f"{MEDILINK_BASE_URL}/pacientes/{pid}", headers=HEADERS, timeout=15)
            ficha = (r.json() or {}).get("data") or {}
            body = {}
            for campo in ("celular", "telefono"):
                if last9 in re.sub(r"\D", "", str(ficha.get(campo) or "")):
                    body[campo] = ""
            if body:
                ru = await client.put(f"{MEDILINK_BASE_URL}/pacientes/{pid}",
                                      json=body, headers=HEADERS, timeout=15)
                info["medilink"] = f"borrado {list(body)} (HTTP {ru.status_code})"
        except Exception as e:
            info["medilink"] = f"error: {e}"
            log.warning("limpiar_numero: Medilink paciente %s error: %s", pid, e)
        resultado["pacientes"].append(info)

    # 3) BI: teléfono NULL + lista dura + consent cerrado
    with bi_conn() as pg:
        with pg.cursor() as cur:
            if fichas:
                cur.execute(
                    "UPDATE bi.dim_paciente SET telefono=NULL, updated_at=NOW() "
                    "WHERE telefono LIKE %s", (f"%{last9}",))
                resultado["acciones"].append(f"bi_telefono_null ({cur.rowcount})")
            for v in variantes:
                cur.execute(
                    "INSERT INTO bi.opt_outs_marketing (phone, source, reason) "
                    "VALUES (%s,%s,%s) ON CONFLICT (phone) DO NOTHING",
                    (v, "numero_reciclado", f"confirmado_por_{quien}"))
            resultado["acciones"].append("opt_outs_marketing")
            cur.execute(
                "UPDATE bi.marketing_consent SET status='declined', response_at=NOW(), "
                "response_method='wrong_number' WHERE phone = ANY(%s) AND status='pending'",
                (variantes,))
            if cur.rowcount:
                resultado["acciones"].append("consent_declined")
            pg.commit()

    # 4) local: tag duro + auditoría
    phone_local = f"56{last9}"
    with db() as c:
        c.execute("INSERT OR IGNORE INTO contact_tags (phone, tag, ts) "
                  "VALUES (?, 'marketing_opt_out', datetime('now'))", (phone_local,))
        c.commit()
    resultado["acciones"].append("tag_marketing_opt_out")
    log_event(phone_local, "numero_equivocado_limpiado", {
        "quien": quien,
        "pacientes": [p["paciente_id"] for p in resultado["pacientes"]],
        "acciones": resultado["acciones"],
        "ts": datetime.now(_CLT).isoformat(timespec="seconds"),
    })
    return resultado


def register_numero_equivocado_routes(app):
    from fastapi import Query, Cookie, HTTPException
    from fastapi.responses import JSONResponse

    def _auth(token, cmc_session):
        from admin_routes import _verify_cookie, _is_admin_token
        if not ((token and _is_admin_token(token)) or (cmc_session and _verify_cookie(cmc_session))):
            raise HTTPException(403, "No autorizado")

    @app.post("/admin/api/numero-equivocado/{phone}/limpiar", include_in_schema=False)
    async def numero_equivocado_limpiar(phone: str, token: str | None = Query(None),
                                        cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        return JSONResponse(await limpiar_numero(phone, quien="recepcion_panel"))
