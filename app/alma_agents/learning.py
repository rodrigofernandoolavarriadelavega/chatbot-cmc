"""Loop de aprendizaje — los agentes mejoran solos leyendo el Ledger.

El Ledger mide qué convirtió. Esto cierra el lazo: por cada agente y cada
"variante" (approach: mensaje A/B, hora de contacto, segmento), calcula la
conversión histórica y recomienda qué variante usar la próxima vez.

Mecanismo: bandit UCB1 (determinista, sin azar → tests reproducibles). Cada
variante tiene un valor (conversión ponderada: cita=1.0, respuesta=0.3,
sin_respuesta=0) más un bono de exploración que decae con los datos. Una
variante nunca probada tiene bono infinito → se explora primero (cold-start).
Así el sistema explota lo que funciona pero sigue probando lo nuevo.

`recommend(agent_id, candidates)` devuelve la variante a usar; el agente la pone
en `AgentAction.variant` y el Ledger la registra → el lazo se realimenta solo.
Read-only sobre el Ledger.
"""
import logging
import math

log = logging.getLogger("bot")

# Pesos del valor de un resultado.
_W_CITA = 1.0
_W_RESPUESTA = 0.3
# Constante de exploración del UCB (más alto = explora más).
_EXPLORE_C = 1.4
# Bono enorme para variantes sin datos → se prueban primero.
_COLD_START_BONUS = 1e6


def _conn():
    from session import _conn as _session_conn
    return _session_conn()


def variant_stats(agent_id: str) -> dict:
    """{variante: {n_medido, citas, respuestas, sin_respuesta, pendientes, valor}}."""
    from . import ledger
    stats: dict[str, dict] = {}
    try:
        ledger.ensure_table()
        conn = _conn()
        cur = conn.execute(
            """SELECT COALESCE(variant,'(default)') AS v,
                      SUM(outcome='cita'), SUM(outcome='respuesta'),
                      SUM(outcome='sin_respuesta'), SUM(outcome='pendiente')
               FROM agent_action_ledger WHERE agent_id = ?
               GROUP BY v""", (agent_id,))
        for v, citas, resp, sinr, pend in cur.fetchall():
            citas = citas or 0; resp = resp or 0; sinr = sinr or 0; pend = pend or 0
            medido = citas + resp + sinr
            valor = ((citas * _W_CITA + resp * _W_RESPUESTA) / medido) if medido else 0.0
            stats[v] = {"n_medido": medido, "citas": citas, "respuestas": resp,
                        "sin_respuesta": sinr, "pendientes": pend, "valor": round(valor, 4)}
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.warning("learning.variant_stats falló: %s", e)
    return stats


def _ucb(stats: dict) -> dict:
    """Agrega un ucb_score a cada variante (valor + bono de exploración)."""
    n_total = sum(s["n_medido"] for s in stats.values())
    ln = math.log(n_total) if n_total > 0 else 0.0
    out = {}
    for v, s in stats.items():
        n = s["n_medido"]
        if n == 0:
            bonus = _COLD_START_BONUS
        else:
            bonus = _EXPLORE_C * math.sqrt(ln / n) if ln > 0 else _EXPLORE_C
        out[v] = {**s, "ucb_score": round(s["valor"] + bonus, 4)}
    return out


def recommend(agent_id: str, candidates: list[str] | None = None) -> dict:
    """Recomienda la variante a usar para este agente.

    `candidates`: variantes disponibles ahora. Una candidata sin historial se
    recomienda primero (exploración). Si no se pasan candidatas, elige entre las
    que ya tienen historial. Devuelve {variant, reason, scores}.
    """
    stats = variant_stats(agent_id)
    scored = _ucb(stats)
    # Asegurar que las candidatas sin datos entren al ranking (cold-start).
    if candidates:
        for c in candidates:
            if c not in scored:
                scored[c] = {"n_medido": 0, "citas": 0, "respuestas": 0,
                             "sin_respuesta": 0, "pendientes": 0, "valor": 0.0,
                             "ucb_score": _COLD_START_BONUS}
        pool = {v: s for v, s in scored.items() if v in candidates}
    else:
        pool = scored
    if not pool:
        return {"variant": (candidates[0] if candidates else None),
                "reason": "sin datos ni candidatas con historial", "scores": {}}
    # Argmax determinista por ucb_score; desempate alfabético estable.
    best = sorted(pool.items(), key=lambda kv: (-kv[1]["ucb_score"], kv[0]))[0]
    v, s = best
    if s["n_medido"] == 0:
        reason = f"exploración: variante '{v}' sin historial todavía"
    else:
        reason = (f"explotación: '{v}' convierte {round(s['valor']*100)}% "
                  f"en {s['n_medido']} contactos medidos")
    return {"variant": v, "reason": reason,
            "scores": {k: val["ucb_score"] for k, val in pool.items()}}


def expected_conversion(agent_id: str, variant: str | None = None) -> float:
    """P(conversión) estimada para (agente, variante). La usa el broker para EV.

    Usa un prior suave Beta(1,2)≈0.33 cuando no hay datos (no sobre-promete)."""
    stats = variant_stats(agent_id)
    if variant is not None:
        s = stats.get(variant) or stats.get("(default)")
    else:
        # promedio ponderado del agente entre todas sus variantes
        tot = sum(x["n_medido"] for x in stats.values())
        if tot:
            citas = sum(x["citas"] for x in stats.values())
            resp = sum(x["respuestas"] for x in stats.values())
            return round((citas * _W_CITA + resp * _W_RESPUESTA) / tot, 4)
        s = None
    if not s or s["n_medido"] == 0:
        return 0.33  # prior conservador para cold-start
    return s["valor"]


def learning_summary() -> dict:
    """Para el panel: por agente, sus variantes + la recomendada."""
    from . import ledger
    out = {"per_agent": []}
    try:
        ledger.ensure_table()
        conn = _conn()
        agent_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT agent_id FROM agent_action_ledger ORDER BY agent_id").fetchall()]
        conn.close()
        for aid in agent_ids:
            scored = _ucb(variant_stats(aid))
            rec = recommend(aid)
            out["per_agent"].append({
                "agent_id": aid,
                "variantes": scored,
                "recomendada": rec["variant"],
                "motivo": rec["reason"],
            })
    except Exception as e:  # noqa: BLE001
        log.warning("learning.learning_summary falló: %s", e)
    return out
