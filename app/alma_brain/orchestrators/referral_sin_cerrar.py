"""Orquestador: leads de Meta Ads que no agendaron.

Señal: teléfonos que entraron por un anuncio de Meta (Click-to-WhatsApp, tabla
meta_referrals) en los últimos 7 días y que NO registran ninguna cita posterior.
Acción: worklist de seguimiento — pagaste el clic, el lead llegó, y no cerró.
Cerrar esos leads es el ROI directo de la pauta. Propone (no auto-contacta)."""
from .base import Orchestrator, Proposal

_DIAS = 7
_TOP = 30


class ReferralSinCerrarOrq(Orchestrator):
    name = "referral_sin_cerrar"
    descripcion = "Leads de Meta Ads (CTWA) sin agendar → seguir para cerrar"
    dominio = "marketing"
    cadencia = "cron:10:00"

    async def sense(self) -> dict:
        from session import db as _conn
        with _conn() as conn:
            refs = conn.execute(
                "SELECT phone, MAX(ts) AS ts, "
                "       MAX(headline) AS headline, MAX(source_type) AS source_type "
                "FROM meta_referrals "
                "WHERE ts >= CAST(strftime('%s','now',?) AS INTEGER) "
                "GROUP BY phone ORDER BY ts DESC",
                (f"-{_DIAS} days",)).fetchall()
            cand = []
            for r in refs:
                phone = r["phone"]
                if not phone:
                    continue
                booked = conn.execute(
                    "SELECT 1 FROM citas_bot WHERE phone = ? "
                    "AND created_at >= datetime(?, 'unixepoch') LIMIT 1",
                    (phone, r["ts"])).fetchone()
                if booked:
                    continue
                prof = conn.execute(
                    "SELECT nombre FROM contact_profiles WHERE phone = ?", (phone,)).fetchone()
                cand.append({"phone": phone,
                             "nombre": (prof["nombre"] if prof else "") or "",
                             "headline": r["headline"] or "",
                             "source_type": r["source_type"] or ""})
        return {"source_status": "ok", "candidatos": cand}

    async def propose(self, signals: dict) -> list[Proposal]:
        cand = signals.get("candidatos", [])[:_TOP]
        if not cand:
            return []

        def _row(c):
            tel = (c["phone"] or "").lstrip("+")
            return {"nombre": c.get("nombre") or "lead", "telefono": c["phone"],
                    "anuncio": c.get("headline", ""), "canal": c.get("source_type", ""),
                    "wa": f"https://wa.me/{tel}" if tel else ""}

        return [Proposal(
            kind="tarea_manual",
            summary=f"Cerrar {len(cand)} lead(s) de Meta Ads que no agendaron",
            rationale="Llegaron por un anuncio (ya pagaste el clic) y no terminaron agendando. "
                      "Un seguimiento cálido con la hora directa (el ad ya filtró intención) "
                      "convierte parte de esos leads — es el ROI directo de la pauta.",
            params={"worklist": [_row(c) for c in cand]},
            impact="Conversión de leads pagados / CAC efectivo / ROAS real",
        )]


ORQ = ReferralSinCerrarOrq()
