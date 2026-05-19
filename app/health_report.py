"""
Reporte semanal de salud del bot CMC.

build_weekly_health_report() -> str   genera el texto del reporte (max ~1500 chars).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("health_report")

# ── helpers ──────────────────────────────────────────────────────────────────

def _conn():
    from session import _conn as _session_conn
    return _session_conn()


def _count_event(conn, event: str, since_iso: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM conversation_events WHERE event=? AND ts >= ?",
        (event, since_iso),
    ).fetchone()
    return row[0] if row else 0


def _count_event_meta(conn, event: str, since_iso: str, meta_filter: dict | None = None) -> int:
    """Cuenta eventos con filtro opcional sobre el campo meta (JSON)."""
    rows = conn.execute(
        "SELECT meta FROM conversation_events WHERE event=? AND ts >= ?",
        (event, since_iso),
    ).fetchall()
    if meta_filter is None:
        return len(rows)
    count = 0
    for r in rows:
        try:
            m = json.loads(r[0] or "{}")
            if all(m.get(k) == v for k, v in meta_filter.items()):
                count += 1
        except Exception:
            pass
    return count


# ── sección 1: tip_sexo_mismatch ─────────────────────────────────────────────

def _section_sexo_mismatch(conn, since_iso: str) -> tuple[int, list[str]]:
    """Retorna (total, top-10-lineas)."""
    rows = conn.execute(
        "SELECT phone, meta, ts FROM conversation_events "
        "WHERE event='tip_sexo_mismatch' AND ts >= ? "
        "ORDER BY ts DESC LIMIT 10",
        (since_iso,),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM conversation_events "
        "WHERE event='tip_sexo_mismatch' AND ts >= ?",
        (since_iso,),
    ).fetchone()[0]

    top_lines = []
    for r in rows:
        try:
            m = json.loads(r[0] or "{}") if isinstance(r[0], str) else {}
            # r is (phone, meta, ts) — index correcto
            m = json.loads(r[1] or "{}")
            rut = m.get("rut") or "sin RUT"
            nombre = m.get("nombre") or "?"
            sex_ml = m.get("sexo_medilink") or "?"
            sex_inf = m.get("sexo_inferido") or "?"
            top_lines.append(f"  {rut} {nombre}: ML={sex_ml}, nombre sugiere {sex_inf}")
        except Exception:
            top_lines.append("  (error parseando meta)")
    return total, top_lines


# ── sección 2: template skips ─────────────────────────────────────────────────

def _section_template_skips(conn, since_iso: str) -> tuple[dict, dict]:
    """Retorna (skip_no_aprobado_by_name, skip_no_consent_by_name)."""
    def _breakdown(event: str) -> dict:
        rows = conn.execute(
            "SELECT meta FROM conversation_events WHERE event=? AND ts >= ?",
            (event, since_iso),
        ).fetchall()
        result: dict[str, int] = {}
        for r in rows:
            try:
                m = json.loads(r[0] or "{}")
                tmpl = m.get("template") or m.get("template_name") or "desconocido"
            except Exception:
                tmpl = "desconocido"
            result[tmpl] = result.get(tmpl, 0) + 1
        return result

    return _breakdown("template_skip_no_aprobado"), _breakdown("template_skip_no_consent")


# ── sección 3: proactive guards ───────────────────────────────────────────────

def _section_proactive_guards(conn, since_iso: str) -> tuple[int, int]:
    blocklist = _count_event(conn, "proactive_skip_blocklist", since_iso)
    late = _count_event(conn, "proactive_skip_blocklist_late", since_iso)
    return blocklist, late


# ── sección 4: errores Meta ───────────────────────────────────────────────────

def _section_meta_errors(conn, since_iso: str) -> dict[str, int]:
    """Cuenta por error_code en message_statuses (status='failed')."""
    target_codes = {"131047", "131042", "131056", "131026", "100"}
    rows = conn.execute(
        "SELECT error_code, COUNT(*) as cnt FROM message_statuses "
        "WHERE status='failed' AND ts >= ? AND error_code IS NOT NULL "
        "GROUP BY error_code",
        (since_iso,),
    ).fetchall()
    result: dict[str, int] = {}
    otros = 0
    for r in rows:
        code = str(r[0])
        cnt = r[1]
        if code in target_codes:
            result[code] = cnt
        else:
            otros += cnt
    if otros:
        result["otros"] = otros
    return result


# ── sección 5: CAPI ───────────────────────────────────────────────────────────

def _section_capi(conn, since_iso: str) -> tuple[int, int]:
    ok = _count_event(conn, "capi_send_ok", since_iso)
    failed = _count_event(conn, "capi_send_failed", since_iso)
    return ok, failed


# ── sección 6: emergencias ────────────────────────────────────────────────────

_EMERGENCIA_PATTERNS = {
    "ACV/FAST": ("acv", "derrame", "hemiparesia", "cara caida", "paralisis",
                 "no puedo mover", "boca torcida"),
    "Cefalea subita": ("exploto la cabeza", "cabeza exploto", "peor dolor de cabeza",
                       "dolor cabeza subito", "cefala subita"),
    "Dental (FP)": ("urgencia dental", "muela", "diente", "bracket",
                    "corona", "frenillo"),
}


def _section_emergencias(conn, since_iso: str) -> tuple[int, dict[str, int]]:
    rows = conn.execute(
        "SELECT meta FROM conversation_events "
        "WHERE event='emergencia_detectada' AND ts >= ?",
        (since_iso,),
    ).fetchall()
    total = len(rows)
    breakdown: dict[str, int] = {"ACV/FAST": 0, "Cefalea subita": 0, "Dental (FP)": 0, "Otros": 0}

    for r in rows:
        try:
            m = json.loads(r[0] or "{}")
            texto = (m.get("texto") or "").lower()
        except Exception:
            texto = ""
        matched = False
        for cat, keywords in _EMERGENCIA_PATTERNS.items():
            if any(kw in texto for kw in keywords):
                breakdown[cat] = breakdown.get(cat, 0) + 1
                matched = True
                break
        if not matched:
            breakdown["Otros"] += 1

    return total, breakdown


# ── sección 7: citas creadas / canceladas ─────────────────────────────────────

def _section_citas(conn, since_iso: str) -> tuple[int, int]:
    creadas = _count_event(conn, "cita_creada", since_iso)
    canceladas = _count_event(conn, "cita_cancelada", since_iso)
    return creadas, canceladas


# ── sección 8: postconsulta ───────────────────────────────────────────────────

def _section_postconsulta(conn, since_iso: str) -> tuple[int, int, dict[str, int]]:
    """Retorna (enviados, respondidos, breakdown mejor/igual/peor)."""
    rows = conn.execute(
        "SELECT respuesta FROM fidelizacion_msgs "
        "WHERE tipo='postconsulta' AND enviado_en >= ?",
        (since_iso,),
    ).fetchall()
    enviados = len(rows)
    breakdown = {"mejor": 0, "igual": 0, "peor": 0}
    respondidos = 0
    for r in rows:
        resp = r[0]
        if resp:
            respondidos += 1
            if resp in breakdown:
                breakdown[resp] += 1
    return enviados, respondidos, breakdown


# ── sección 9: winback ────────────────────────────────────────────────────────

def _section_winback(conn, since_iso: str) -> tuple[int, int]:
    """Retorna (enviados, citas_creadas_post_winback_7d)."""
    # Enviados: filas en fidelizacion_msgs con tipo=winback en el período
    row = conn.execute(
        "SELECT COUNT(*) FROM fidelizacion_msgs "
        "WHERE tipo IN ('winback', 'winback_fidelizacion') AND enviado_en >= ?",
        (since_iso,),
    ).fetchone()
    enviados = row[0] if row else 0

    # Citas atribuidas: cita_creada dentro de 7d después de un winback al mismo phone
    row2 = conn.execute(
        """
        SELECT COUNT(DISTINCT ce.phone)
        FROM conversation_events ce
        JOIN fidelizacion_msgs fm ON fm.phone = ce.phone
        WHERE ce.event = 'cita_creada'
          AND fm.tipo IN ('winback', 'winback_fidelizacion')
          AND fm.enviado_en >= ?
          AND ce.ts >= fm.enviado_en
          AND ce.ts <= datetime(fm.enviado_en, '+7 days')
        """,
        (since_iso,),
    ).fetchone()
    citas_post = row2[0] if row2 else 0

    return enviados, citas_post


# ── función principal ─────────────────────────────────────────────────────────

def build_weekly_health_report() -> str:
    """
    Genera el texto completo del reporte semanal de salud del bot.
    Retorna string <= ~1500 chars formateado para WhatsApp.
    """
    now_utc = datetime.now(timezone.utc)
    # Período: últimos 7 días
    since_dt = now_utc - timedelta(days=7)
    since_iso = since_dt.strftime("%Y-%m-%d %H:%M:%S")

    # Fechas para el encabezado en zona Santiago (UTC-4 aprox, usamos offset fijo simple)
    tz_stgo = timezone(timedelta(hours=-4))
    fecha_fin_s = now_utc.astimezone(tz_stgo).strftime("%d/%m")
    fecha_ini_s = since_dt.astimezone(tz_stgo).strftime("%d/%m")

    try:
        with _conn() as conn:
            # Métricas
            sexo_total, sexo_top = _section_sexo_mismatch(conn, since_iso)
            skip_no_ap, skip_no_consent = _section_template_skips(conn, since_iso)
            blocklist_n, late_n = _section_proactive_guards(conn, since_iso)
            meta_errors = _section_meta_errors(conn, since_iso)
            capi_ok, capi_failed = _section_capi(conn, since_iso)
            emerg_total, emerg_bd = _section_emergencias(conn, since_iso)
            citas_creadas, citas_canceladas = _section_citas(conn, since_iso)
            pc_env, pc_resp, pc_bd = _section_postconsulta(conn, since_iso)
            wb_env, wb_citas = _section_winback(conn, since_iso)
    except Exception as e:
        log.error("build_weekly_health_report: error consultando DB: %s", e)
        return f"[ERROR generando reporte semanal: {e}]"

    # ── Errores Meta ──
    err_131047 = meta_errors.get("131047", 0)
    err_131042 = meta_errors.get("131042", 0)
    err_otros = sum(v for k, v in meta_errors.items() if k not in ("131047", "131042"))
    capi_total = capi_ok + capi_failed
    capi_pct = f"{round(capi_ok / capi_total * 100)}%" if capi_total else "n/a"

    # ── Templates pendientes ──
    if skip_no_ap:
        skip_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(skip_no_ap.items(), key=lambda x: -x[1]))
    else:
        skip_lines = "  ninguno"

    # ── Postconsulta ──
    pc_tasa = f"{round(pc_resp / pc_env * 100)}%" if pc_env else "n/a"

    # ── Sexo mismatch top ──
    if sexo_top:
        sexo_lines = "\n".join(sexo_top[:5])
    else:
        sexo_lines = "  ninguno"

    # ── Guardia proactiva ──
    guards_total = blocklist_n + late_n
    guards_status = "OK" if guards_total > 0 else "sin eventos (normal si no hubo envios)"

    # ── Armar mensaje ──
    lines = [
        f"*Reporte salud bot CMC* — {fecha_ini_s} a {fecha_fin_s}",
        "",
        "*Errores Meta:*",
        f"  131047: {err_131047}  131042: {err_131042}  otros: {err_otros}",
        "",
        f"*CAPI:* {capi_ok}/{capi_total} OK ({capi_pct})",
        "",
        "*Templates enviados (postconsulta):*",
        f"  enviados: {pc_env}  respuestas: {pc_resp} ({pc_tasa})",
        f"  mejor: {pc_bd['mejor']}  igual: {pc_bd['igual']}  peor: {pc_bd['peor']}",
        f"  winback: {wb_env} → {wb_citas} citas atribuidas",
        "",
        "*Templates pendientes aprobacion Meta:*",
        skip_lines,
        "",
        "*Datos a corregir en Medilink (sexo):*",
        f"  {sexo_total} pacientes detectados",
        sexo_lines,
        "",
        f"*Bloqueos proactivos (loop prevention):* {guards_total} ({guards_status})",
        "",
        f"*Emergencias detectadas:* {emerg_total}",
        f"  ACV/FAST: {emerg_bd['ACV/FAST']}  Cefalea: {emerg_bd['Cefalea subita']}",
        f"  Dental FP: {emerg_bd['Dental (FP)']}  Otros: {emerg_bd['Otros']}",
        "",
        f"*Citas:* {citas_creadas} creadas / {citas_canceladas} canceladas",
    ]

    report = "\n".join(lines)

    # Recortar si supera 1500 chars (Meta límite para texto libre)
    if len(report) > 1500:
        report = report[:1470] + "\n...(recortado)"

    return report
