#!/usr/bin/env python3
"""
Reporte CAC dual del CMC.

Modos:
  --mode fixed       : compara mes-corriente vs mes-anterior (mismos días).
  --mode custom      : usa --since/--until y --baseline-since/--baseline-until.

Cruza dos atribuciones lado a lado:
  A) Tracking del bot: phones meta_referrals → citas_bot → bi_pagos_caja por id_paciente.
  B) Trendline de caja: delta de ingreso por especialidad vs baseline (mismo rango mes anterior),
     más pacientes nuevos (id_paciente que nunca antes vio a ese profesional).

Uso:
  python scripts/cac_report.py
  python scripts/cac_report.py --mode custom --since 2026-05-01 --until 2026-05-15 \\
                               --baseline-since 2026-04-01 --baseline-until 2026-04-15
  python scripts/cac_report.py --html ~/cac_reports/cac_mayo.html
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import os
import sys
from collections import defaultdict
from pathlib import Path

# Bootstrap del path/env para correr standalone desde host o servidor.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(str(ROOT / ".env"))
except ImportError:
    pass

import httpx  # noqa: E402

from app.session import _conn  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Mapping id_profesional → (nombre, especialidad).  Fuente: CLAUDE.md.
# ─────────────────────────────────────────────────────────────────────────────
PROFS: dict[int, tuple[str, str]] = {
    1:  ("Dr. Olavarría",    "Medicina General"),
    73: ("Dr. Abarca",       "Medicina General"),
    13: ("Dr. Márquez",      "Medicina Familiar"),
    23: ("Dr. Borrego",      "Otorrinolaringología"),
    60: ("Dr. Millán",       "Cardiología"),
    64: ("Dr. Barraza",      "Traumatología"),
    61: ("Dr. Rejón",        "Ginecología"),
    65: ("Dr. Quijano",      "Gastroenterología"),
    55: ("Dra. Burgos",      "Odontología"),
    72: ("Dr. Jiménez",      "Odontología"),
    66: ("Dra. Castillo",    "Ortodoncia"),
    75: ("Dr. Fredes",       "Endodoncia"),
    69: ("Dra. Valdés",      "Implantología"),
    76: ("Dra. Fuentealba",  "Estética Facial"),
    59: ("P. Acosta",        "Masoterapia"),
    77: ("L. Armijo",        "Kinesiología"),
    21: ("L. Etcheverry",    "Kinesiología"),
    52: ("G. Pinto",         "Nutrición"),
    74: ("J. Montalba",      "Psicología"),
    49: ("JP. Rodríguez",    "Psicología"),
    70: ("J. Arratia",       "Fonoaudiología"),
    67: ("S. Gómez",         "Matrona"),
    56: ("A. Guevara",       "Podología"),
    68: ("D. Pardo",         "Ecografía"),
}

ESP_TO_PROFS: dict[str, list[int]] = defaultdict(list)
for pid, (_n, esp) in PROFS.items():
    ESP_TO_PROFS[esp].append(pid)

# ─────────────────────────────────────────────────────────────────────────────
# Argparse y resolución de períodos
# ─────────────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reporte CAC dual del CMC.")
    p.add_argument("--mode", choices=("fixed", "custom"), default="fixed",
                   help="fixed: mes-corriente vs mes-anterior (mismos días). "
                        "custom: período definido por flags.")
    p.add_argument("--since", help="Inicio del período actual (YYYY-MM-DD).")
    p.add_argument("--until", help="Fin del período actual (YYYY-MM-DD).")
    p.add_argument("--baseline-since", help="Inicio del baseline (YYYY-MM-DD).")
    p.add_argument("--baseline-until", help="Fin del baseline (YYYY-MM-DD).")
    p.add_argument("--html", help="Ruta del archivo HTML de salida (opcional).")
    p.add_argument("--no-meta", action="store_true",
                   help="Saltarse llamada a Meta Marketing API (modo offline).")
    return p.parse_args()


def resolve_periods(args: argparse.Namespace) -> tuple[tuple[str, str], tuple[str, str]]:
    """Devuelve ((cur_since, cur_until), (base_since, base_until))."""
    if args.mode == "custom":
        for f in ("since", "until", "baseline_since", "baseline_until"):
            if not getattr(args, f):
                sys.exit(f"--mode custom requiere --{f.replace('_','-')}")
        return (args.since, args.until), (args.baseline_since, args.baseline_until)

    today = dt.date.today()
    first_of_month = today.replace(day=1)
    cur_since, cur_until = first_of_month.isoformat(), today.isoformat()

    days = (today - first_of_month).days
    prev_month_end = first_of_month - dt.timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    last_day_prev = calendar.monthrange(prev_month_start.year, prev_month_start.month)[1]
    base_until_day = min(today.day, last_day_prev)
    base_since = prev_month_start.isoformat()
    base_until = prev_month_start.replace(day=base_until_day).isoformat()
    return (cur_since, cur_until), (base_since, base_until)


# ─────────────────────────────────────────────────────────────────────────────
# Atribución B: Trendline de caja
# ─────────────────────────────────────────────────────────────────────────────
def caja_periodo(cur, prof_ids: list[int], since: str, until: str) -> tuple[int, int, int]:
    if not prof_ids:
        return 0, 0, 0
    ph = ",".join("?" for _ in prof_ids)
    cur.execute(
        f"SELECT COALESCE(SUM(monto),0), COUNT(*), COUNT(DISTINCT id_paciente) "
        f"FROM bi_pagos_caja WHERE id_profesional IN ({ph}) AND fecha BETWEEN ? AND ?",
        prof_ids + [since, until],
    )
    s, n, uniq = cur.fetchone()
    return s or 0, n or 0, uniq or 0


def dias_activos(cur, prof_ids: list[int], since: str, until: str) -> int:
    """Cuenta días distintos en que el profesional registró al menos un pago."""
    if not prof_ids:
        return 0
    ph = ",".join("?" for _ in prof_ids)
    cur.execute(
        f"SELECT COUNT(DISTINCT fecha) FROM bi_pagos_caja "
        f"WHERE id_profesional IN ({ph}) AND fecha BETWEEN ? AND ?",
        prof_ids + [since, until],
    )
    return cur.fetchone()[0] or 0


def pacientes_nuevos(cur, prof_ids: list[int], since: str, until: str) -> int:
    if not prof_ids:
        return 0
    ph = ",".join("?" for _ in prof_ids)
    cur.execute(
        f"""SELECT COUNT(DISTINCT id_paciente) FROM bi_pagos_caja p1
            WHERE p1.id_profesional IN ({ph}) AND p1.fecha BETWEEN ? AND ?
              AND NOT EXISTS (
                SELECT 1 FROM bi_pagos_caja p2
                WHERE p2.id_paciente = p1.id_paciente
                  AND p2.id_profesional IN ({ph})
                  AND p2.fecha < ?
              )""",
        prof_ids + [since, until] + prof_ids + [since],
    )
    return cur.fetchone()[0] or 0


# ─────────────────────────────────────────────────────────────────────────────
# Atribución A: Tracking del bot
# ─────────────────────────────────────────────────────────────────────────────
def bot_funnel(cur, since: str, until: str) -> dict:
    """Devuelve dict con phones meta, agrupado por ad y por especialidad."""
    cur.execute(
        "SELECT phone, source_id, headline, datetime(ts,'unixepoch') as ts_iso "
        "FROM meta_referrals WHERE phone IS NOT NULL AND phone != '' "
        "AND datetime(ts,'unixepoch') BETWEEN ? AND ?",
        (since + " 00:00:00", until + " 23:59:59"),
    )
    referrals = {r[0]: {"source_id": r[1], "headline": r[2], "ts": r[3]} for r in cur.fetchall()}
    phones = list(referrals.keys())
    if not phones:
        return {"referrals": {}, "conversaron": 0, "agendaron": 0, "atendidos": [], "ingreso": 0}

    ph = ",".join("?" for _ in phones)

    cur.execute(
        f"SELECT COUNT(DISTINCT phone) FROM conversation_events "
        f"WHERE phone IN ({ph}) AND event='intent_detectado' "
        f"AND ts BETWEEN ? AND ?",
        phones + [since, until + " 23:59:59"],
    )
    conversaron = cur.fetchone()[0] or 0

    cur.execute(
        f"SELECT cb.phone, cb.id_cita, cb.especialidad, cb.fecha, cb.id_paciente_medilink, cb.cancel_detected_at "
        f"FROM citas_bot cb WHERE cb.phone IN ({ph})",
        phones,
    )
    citas = cur.fetchall()
    agendaron_phones = {c[0] for c in citas}

    atendidos: list[dict] = []
    hoy = dt.date.today().isoformat()
    total_ing = 0
    for phone, id_cita, esp, fecha, id_pac, cancel in citas:
        if not fecha or fecha >= hoy or cancel is not None:
            continue
        cobrado = 0
        if id_pac:
            cur.execute(
                "SELECT COALESCE(SUM(monto),0) FROM bi_pagos_caja "
                "WHERE id_paciente = ? AND fecha BETWEEN date(?, '-2 days') AND date(?, '+30 days')",
                (id_pac, fecha, fecha),
            )
            cobrado = cur.fetchone()[0] or 0
        atendidos.append({
            "phone": phone, "esp": esp, "fecha": fecha,
            "id_pac": id_pac, "cobrado": cobrado,
            "source_id": referrals[phone]["source_id"],
        })
        total_ing += cobrado

    return {
        "referrals": referrals,
        "phones": len(phones),
        "conversaron": conversaron,
        "agendaron": len(agendaron_phones),
        "atendidos": atendidos,
        "ingreso": total_ing,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Meta Marketing API: spend por ad
# ─────────────────────────────────────────────────────────────────────────────
def meta_spend(since: str, until: str) -> dict[str, dict]:
    token = os.getenv("META_ACCESS_TOKEN") or os.getenv("META_CAPI_ACCESS_TOKEN", "")
    acct = os.getenv("META_AD_ACCOUNT_ID", "")
    if not token or not acct:
        return {}
    if not acct.startswith("act_"):
        acct = f"act_{acct}"
    url = f"https://graph.facebook.com/v21.0/{acct}/insights"
    params = {
        "access_token": token,
        "fields": "spend,impressions,clicks,ctr,ad_id,ad_name,campaign_name",
        "level": "ad",
        "time_range": f'{{"since":"{since}","until":"{until}"}}',
        "limit": 500,
    }
    try:
        r = httpx.get(url, params=params, timeout=30)
        if r.status_code != 200:
            print(f"[!] Meta API HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return {}
        data = r.json().get("data", [])
    except Exception as e:
        print(f"[!] Meta API error: {e}", file=sys.stderr)
        return {}
    return {row["ad_id"]: {
        "ad_name": row.get("ad_name", ""),
        "campaign_name": row.get("campaign_name", ""),
        "spend": float(row.get("spend") or 0),
        "impressions": int(row.get("impressions") or 0),
        "clicks": int(row.get("clicks") or 0),
        "ctr": float(row.get("ctr") or 0),
    } for row in data}


# Heurísticas para asociar ad → especialidad (por nombre del ad/campaña)
ESP_KEYWORDS: list[tuple[str, str]] = [
    ("orto",       "Ortodoncia"),
    ("brackets",   "Ortodoncia"),
    ("ortodonc",   "Ortodoncia"),
    ("endodonc",   "Endodoncia"),
    ("estét",      "Estética Facial"),
    ("estet",      "Estética Facial"),
    ("dental",     "Odontología"),
    ("odonto",     "Odontología"),
    ("muela",      "Odontología"),
    ("orl",        "Otorrinolaringología"),
    ("otorrino",   "Otorrinolaringología"),
    ("oído",       "Otorrinolaringología"),
    ("oido",       "Otorrinolaringología"),
    ("eco",        "Ecografía"),
    ("ecograf",    "Ecografía"),
    ("ecotom",     "Ecografía"),
    ("kine",       "Kinesiología"),
    ("psicol",     "Psicología"),
    ("nutri",      "Nutrición"),
    ("cardio",     "Cardiología"),
    ("gineco",     "Ginecología"),
    ("matrona",    "Matrona"),
    ("trauma",     "Traumatología"),
    ("medicina",   "Medicina General"),
    ("médico",     "Medicina General"),
    ("medico",     "Medicina General"),
    ("salud",      "Medicina General"),
]


def ad_to_esp(ad_name: str, campaign_name: str = "") -> str | None:
    s = f"{(ad_name or '').lower()} {(campaign_name or '').lower()}"
    for kw, esp in ESP_KEYWORDS:
        if kw in s:
            return esp
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Render terminal + HTML
# ─────────────────────────────────────────────────────────────────────────────
def fmt_clp(n: float | int) -> str:
    if n is None:
        return "—"
    return f"${int(n):,}".replace(",", ".")


def render(cur_period, base_period, bot_data, ads, args) -> tuple[str, str]:
    cur_since, cur_until = cur_period
    base_since, base_until = base_period

    # Spend agregado por especialidad
    spend_by_esp: dict[str, float] = defaultdict(float)
    for ad in ads.values():
        esp = ad_to_esp(ad["ad_name"], ad["campaign_name"])
        if esp:
            spend_by_esp[esp] += ad["spend"]

    # Trendline por especialidad
    rows = []
    for esp in sorted(ESP_TO_PROFS):
        profs = ESP_TO_PROFS[esp]
        cur_ing, cur_n, cur_uniq = caja_periodo(cur, profs, cur_since, cur_until)
        base_ing, base_n, base_uniq = caja_periodo(cur, profs, base_since, base_until)
        cur_dias = dias_activos(cur, profs, cur_since, cur_until)
        base_dias = dias_activos(cur, profs, base_since, base_until)
        nuevos = pacientes_nuevos(cur, profs, cur_since, cur_until)
        delta = cur_ing - base_ing
        spend = spend_by_esp.get(esp, 0)
        roas_bruto = (delta / spend) if spend else None
        roas_neto = ((delta - spend) / spend) if spend else None

        ing_por_dia_cur = (cur_ing / cur_dias) if cur_dias else 0
        ing_por_dia_base = (base_ing / base_dias) if base_dias else 0
        delta_norm = ing_por_dia_cur - ing_por_dia_base
        # ROAS normalizado: ¿cuánto subió el ingreso/día vs costo unitario por día?
        # Si el spend cubre N días del rango actual, divido el spend total entre cur_dias
        # para obtener un costo por día activo y comparar contra el delta por día.
        roas_norm = (delta_norm * cur_dias / spend) if (spend and cur_dias) else None

        rows.append({
            "esp": esp, "cur_ing": cur_ing, "base_ing": base_ing,
            "delta": delta, "spend": spend,
            "roas_bruto": roas_bruto, "roas_neto": roas_neto,
            "roas_norm": roas_norm,
            "pac_cur": cur_uniq, "pac_base": base_uniq, "pac_nuevos": nuevos,
            "cur_dias": cur_dias, "base_dias": base_dias,
            "ing_por_dia_cur": ing_por_dia_cur, "ing_por_dia_base": ing_por_dia_base,
            "delta_norm": delta_norm,
        })

    # ── Terminal ──
    lines: list[str] = []
    lines.append("=" * 100)
    lines.append(f"  REPORTE CAC DUAL — CMC")
    lines.append(f"  Período actual : {cur_since} → {cur_until}")
    lines.append(f"  Baseline       : {base_since} → {base_until}")
    lines.append("=" * 100)

    # Sección A — Bot
    lines.append("\n[A] ATRIBUCIÓN DIRECTA — Tracking del bot")
    lines.append(f"  Phones llegados vía Meta ad : {bot_data.get('phones',0)}")
    lines.append(f"  Conversaron con el bot      : {bot_data.get('conversaron',0)}")
    lines.append(f"  Agendaron cita              : {bot_data.get('agendaron',0)}")
    lines.append(f"  Se atendieron               : {len(bot_data.get('atendidos',[]))}")
    lines.append(f"  Ingreso cobrado (caja)      : {fmt_clp(bot_data.get('ingreso',0))}")

    # Sección B — Trendline
    lines.append("\n[B] ATRIBUCIÓN POR TRENDLINE — Delta de caja por especialidad")
    lines.append("  Bruto compara totales · Norm compara ingreso/día activo (corrige profesionales con calendario distinto)")
    lines.append(f"  {'Especialidad':<22} {'Base $':>10} {'d':>3} {'Actual $':>10} {'d':>3} {'$/día base':>11} {'$/día cur':>11} {'Pac N':>6} {'Spend':>9} {'ROAS br':>8} {'ROAS norm':>10}")
    lines.append("  " + "-" * 116)
    for r in rows:
        if r["cur_ing"] == 0 and r["base_ing"] == 0 and r["spend"] == 0:
            continue
        roas_s = f"{r['roas_bruto']:>6.2f}x" if r["roas_bruto"] is not None else "      —"
        roas_n = f"{r['roas_norm']:>8.2f}x" if r["roas_norm"] is not None else "        —"
        lines.append(
            f"  {r['esp']:<22} {fmt_clp(r['base_ing']):>10} {r['base_dias']:>3} "
            f"{fmt_clp(r['cur_ing']):>10} {r['cur_dias']:>3} "
            f"{fmt_clp(r['ing_por_dia_base']):>11} {fmt_clp(r['ing_por_dia_cur']):>11} "
            f"{r['pac_nuevos']:>6} {fmt_clp(r['spend']):>9} {roas_s:>8} {roas_n:>10}"
        )

    # Sección C — Veredicto por ad
    lines.append("\n[C] VEREDICTO POR AD")
    for ad_id, ad in sorted(ads.items(), key=lambda kv: -kv[1]["spend"]):
        if ad["spend"] <= 0:
            continue
        esp = ad_to_esp(ad["ad_name"], ad["campaign_name"]) or "?"
        match_bot = [a for a in bot_data.get("atendidos", []) if a["source_id"] == ad_id]
        ing_bot = sum(a["cobrado"] for a in match_bot)
        roas_bot = (ing_bot / ad["spend"]) if ad["spend"] else 0
        esp_row = next((r for r in rows if r["esp"] == esp), None)
        roas_tl = esp_row["roas_bruto"] if (esp_row and esp_row["roas_bruto"] is not None) else None

        veredicto = _veredicto(roas_bot, roas_tl, esp_row, ad["spend"])
        ad_short = ad["ad_name"][:48] if ad["ad_name"] else ad_id
        lines.append(f"\n  • {ad_short}  ({ad_id})")
        lines.append(f"    esp inferida: {esp} | spend: {fmt_clp(ad['spend'])} | clicks: {ad['clicks']} | CTR: {ad['ctr']:.2f}%")
        lines.append(f"    bot:  {len(match_bot)} atendidos, ingreso {fmt_clp(ing_bot)} → ROAS {roas_bot:.2f}x")
        roas_tl_s = f"{roas_tl:.2f}x" if roas_tl is not None else "n/d"
        if esp_row:
            roas_norm_s = f"{esp_row['roas_norm']:.2f}x" if esp_row["roas_norm"] is not None else "n/d"
            lines.append(f"    trend: Δcaja {fmt_clp(esp_row['delta'])} | {esp_row['pac_nuevos']} pac nuevos → ROAS bruto {roas_tl_s} · norm {roas_norm_s}")
            lines.append(f"    días activos: base {esp_row['base_dias']} · actual {esp_row['cur_dias']} · "
                         f"$/día base {fmt_clp(esp_row['ing_por_dia_base'])} · $/día actual {fmt_clp(esp_row['ing_por_dia_cur'])}")
        lines.append(f"    → {veredicto}")

    text = "\n".join(lines)
    html = _html(cur_period, base_period, bot_data, rows, ads)
    return text, html


def _veredicto(roas_bot: float, roas_tl: float | None, esp_row, spend: float) -> str:
    if roas_tl is None and roas_bot == 0:
        return "Sin atribución (ni bot ni trendline). Revisar destino del ad."
    if roas_tl is None:
        if roas_bot >= 1:
            return "Ad funcionando vía bot. Mantener."
        return "Bot sin retorno claro. Revisar."

    # Si hay desbalance de días activos (>30% de diferencia), preferir ROAS normalizado
    base_dias = esp_row.get("base_dias", 0) if esp_row else 0
    cur_dias = esp_row.get("cur_dias", 0) if esp_row else 0
    roas_norm = esp_row.get("roas_norm") if esp_row else None
    desbalance_dias = (base_dias > 0 and cur_dias > 0
                      and abs(cur_dias - base_dias) / max(base_dias, cur_dias) > 0.3)
    if desbalance_dias and roas_norm is not None:
        if roas_norm >= 1 and roas_bot >= 1:
            return f"Días distintos (base {base_dias} vs cur {cur_dias}). Normalizado: ESCALAR."
        if roas_norm >= 0.5:
            return f"Días distintos (base {base_dias} vs cur {cur_dias}). Norm OK, mantener."
        if roas_norm < 0 and cur_dias < base_dias:
            return f"Ojo: profesional tuvo {cur_dias} días vs {base_dias} en baseline. Confirmar agenda antes de juzgar."

    if cur_dias == 0 and base_dias > 0:
        return f"Profesional aún no atendió este período ({base_dias} días en baseline). Esperar o confirmar agenda."
    if roas_tl >= 2 and roas_bot >= 1:
        return "Ambas señales positivas: ESCALAR."
    if roas_tl >= 1 and roas_bot < 1:
        return "Trendline positiva, bot no captura — los pacientes llaman al fijo. Mantener."
    if roas_tl < 0 and esp_row["pac_cur"] == 0:
        return f"Profesional sin agenda en período (0 pacientes). PAUSAR hasta confirmar agenda."
    if roas_tl < 0:
        return "Caja bajó vs baseline. Revisar profesional/agenda antes de culpar al ad."
    if roas_bot >= 1 and roas_tl < 1:
        return "Bot rinde pero la especialidad total cae. Posible canibalización o estacionalidad."
    return "Marginal. Monitorear 7 días más antes de decidir."


def _html(cur_period, base_period, bot_data, rows, ads) -> str:
    cur_s, cur_u = cur_period
    base_s, base_u = base_period
    rows_html = ""
    for r in rows:
        if r["cur_ing"] == 0 and r["base_ing"] == 0 and r["spend"] == 0:
            continue
        cls = ""
        if r["roas_bruto"] is not None:
            if r["roas_bruto"] >= 2: cls = "good"
            elif r["roas_bruto"] < 0: cls = "bad"
            elif r["roas_bruto"] < 1: cls = "warn"
        roas_b_s = f"{r['roas_bruto']:.2f}x" if r["roas_bruto"] is not None else "—"
        roas_n_s = f"{r['roas_norm']:.2f}x" if r["roas_norm"] is not None else "—"
        rows_html += f"""
            <tr class="{cls}">
              <td>{r['esp']}</td>
              <td class="num">{fmt_clp(r['base_ing'])}</td>
              <td class="num">{r['base_dias']}</td>
              <td class="num">{fmt_clp(r['cur_ing'])}</td>
              <td class="num">{r['cur_dias']}</td>
              <td class="num">{fmt_clp(r['ing_por_dia_base'])}</td>
              <td class="num">{fmt_clp(r['ing_por_dia_cur'])}</td>
              <td class="num">{r['pac_nuevos']}</td>
              <td class="num">{fmt_clp(r['spend'])}</td>
              <td class="num">{roas_b_s}</td>
              <td class="num"><b>{roas_n_s}</b></td>
            </tr>"""

    ads_html = ""
    for ad_id, ad in sorted(ads.items(), key=lambda kv: -kv[1]["spend"]):
        if ad["spend"] <= 0: continue
        esp = ad_to_esp(ad["ad_name"], ad["campaign_name"]) or "?"
        match_bot = [a for a in bot_data.get("atendidos", []) if a["source_id"] == ad_id]
        ing_bot = sum(a["cobrado"] for a in match_bot)
        roas_bot = (ing_bot / ad["spend"]) if ad["spend"] else 0
        esp_row = next((r for r in rows if r["esp"] == esp), None)
        roas_tl = esp_row["roas_bruto"] if (esp_row and esp_row["roas_bruto"] is not None) else None
        roas_tl_s = f"{roas_tl:.2f}x" if roas_tl is not None else "—"
        veredicto = _veredicto(roas_bot, roas_tl, esp_row, ad["spend"])
        ads_html += f"""
            <tr>
              <td>{(ad['ad_name'] or ad_id)[:60]}</td>
              <td>{esp}</td>
              <td class="num">{fmt_clp(ad['spend'])}</td>
              <td class="num">{ad['clicks']}</td>
              <td class="num">{ad['ctr']:.2f}%</td>
              <td class="num">{len(match_bot)} / {fmt_clp(ing_bot)} / {roas_bot:.2f}x</td>
              <td class="num">{roas_tl_s}</td>
              <td>{veredicto}</td>
            </tr>"""

    return f"""<!doctype html><html lang=es><head>
<meta charset=utf-8><title>CAC dual — CMC</title>
<style>
 body{{font-family:-apple-system,system-ui,sans-serif;margin:24px;color:#1a1a1a;background:#f6f7f9}}
 h1{{color:#0F3F68}} h2{{color:#0F3F68;border-bottom:2px solid #4FBECE;padding-bottom:4px}}
 .meta{{color:#555;font-size:14px;margin-bottom:24px}}
 table{{border-collapse:collapse;width:100%;background:white;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:32px}}
 th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid #eee;font-size:14px}}
 th{{background:#0F3F68;color:white;font-weight:600}}
 .num{{text-align:right;font-variant-numeric:tabular-nums}}
 tr.good{{background:#e8f7ee}} tr.warn{{background:#fff8e1}} tr.bad{{background:#fde8e8}}
 .kpis{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:16px 0}}
 .kpi{{background:white;padding:12px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 .kpi .v{{font-size:24px;font-weight:600;color:#0F3F68}}
 .kpi .l{{font-size:12px;color:#666;text-transform:uppercase}}
</style></head><body>
<h1>Reporte CAC dual — CMC</h1>
<div class=meta>Período: <b>{cur_s} → {cur_u}</b> vs baseline <b>{base_s} → {base_u}</b></div>

<h2>A. Atribución directa (bot)</h2>
<div class=kpis>
  <div class=kpi><div class=l>Phones Meta</div><div class=v>{bot_data.get('phones',0)}</div></div>
  <div class=kpi><div class=l>Conversaron</div><div class=v>{bot_data.get('conversaron',0)}</div></div>
  <div class=kpi><div class=l>Agendaron</div><div class=v>{bot_data.get('agendaron',0)}</div></div>
  <div class=kpi><div class=l>Atendidos</div><div class=v>{len(bot_data.get('atendidos',[]))}</div></div>
  <div class=kpi><div class=l>Ingreso bot</div><div class=v>{fmt_clp(bot_data.get('ingreso',0))}</div></div>
</div>

<h2>B. Trendline de caja <small style="font-weight:400;color:#666">— "ROAS norm" corrige cuando el profesional tuvo distinto número de días activos en cada período</small></h2>
<table>
  <tr>
    <th>Especialidad</th>
    <th>Base $</th><th>d</th>
    <th>Actual $</th><th>d</th>
    <th>$/día base</th><th>$/día actual</th>
    <th>Pac nuevos</th>
    <th>Spend</th>
    <th>ROAS bruto</th>
    <th>ROAS norm</th>
  </tr>
  {rows_html}
</table>

<h2>C. Veredicto por ad</h2>
<table>
  <tr><th>Ad</th><th>Esp</th><th>Spend</th><th>Clicks</th><th>CTR</th><th>Bot (n/ing/ROAS)</th><th>ROAS trend</th><th>Veredicto</th></tr>
  {ads_html}
</table>

<p style="color:#888;font-size:12px;margin-top:48px">Generado por scripts/cac_report.py</p>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = parse_args()
    cur_period, base_period = resolve_periods(args)

    con = _conn()
    cur = con.cursor()

    bot_data = bot_funnel(cur, *cur_period)
    ads = {} if args.no_meta else meta_spend(*cur_period)

    text, html = render(cur_period, base_period, bot_data, ads, args)
    print(text)

    if args.html:
        out = Path(args.html).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"\n[✓] HTML escrito en {out}")
