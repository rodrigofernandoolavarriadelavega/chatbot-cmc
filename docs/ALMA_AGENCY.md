# Alma Agency — las 6 capas que hacen la flota "más agéntica"

Construidas el 2026-06-02 sobre la flota (`alma_agents`) + el cerebro
(`alma_brain`) + la capa de medición (Ledger / Simulador / Capstone).

La flota ya sabía **percibir** y **actuar**. Estas capas cierran el círculo:
**medir → aprender → reaccionar → competir por valor → perseguir metas →
obedecer por intención → conversar**. Todo OFF por defecto; todo detrás de la
misma cascada de gating (`ALMA_AGENTS_ENABLED` → flag por agente →
`ALMA_AGENTS_EXECUTE` → piso legal no-delegable).

## #2 · Loop de aprendizaje — `learning.py`
Bandit UCB1 determinista sobre el Ledger. Cada agente prueba "variantes"
(`AgentAction.variant`: mensaje A/B, hora, segmento) y el sistema calcula la
conversión por variante. `recommend(agent, candidates)` explota lo que funciona y
explora lo nuevo (cold-start). `expected_conversion()` da el P(conversión) que usa
el broker. → `GET /api/learning`. Test: 10/10.

## #3 · Broker de contacto — `broker.py`
Cuando varios agentes quieren contactar al mismo paciente, gana el de mayor
**valor esperado** = P(conversión) × ticket × prioridad del dueño. Resuelve el
spam agregado con criterio (no por orden de llegada). Integrado en el simulador.
Test: 8/8.

## #1 · Bus de eventos — `events.py`
La flota reacciona en tiempo real, no solo por cron. `emit(tipo, payload)`
despacha a los agentes suscritos (`Agent.triggers` + `react()` vía
`run_reactive`). No-op seguro con el maestro off. Sembrado en `jobs.py`
(`cita_cancelada`); `yield_agenda` reacciona contactando demanda reprimida de esa
especialidad al instante. → `GET /api/events`, `POST /api/emit`. Test: 8/8.

## #4 · Goal-setting con presupuestos — `goals.py`
El cerebro **propone metas** semanales desde sus alertas (agenda→llenar,
demanda→capturar, fidelización→recuperar, caja→cobrar), con agentes y presupuesto
de contacto. `create_goal` las activa; `progress()` mide citas/respuestas
atribuidas vs target. Tabla `agent_goals`. → `GET /api/goals`,
`POST /api/goals/create`. Test: 10/10.

## #6 · Plano de control en lenguaje natural — `control_plane.py` + `runtime.py`
Manejas la flota por intención, en español: "enfócate en kine", "baja la
cobranza", "recupera 10 inactivos" → overrides de foco + prioridad por agente que
el broker lee (sube/baja el EV). `interpret()` determinista (offline, nunca falla)
+ `interpret_llm()` con Claude para lo difuso. El `runtime.py` es el dial
persistido. → `POST /api/control`, `GET /api/runtime`. Test: 12/12.

## #5 · Agentes conversacionales — `conversation.py`
Micro-diálogos dirigidos con tope duro de turnos (negociar hora, objeción) que
cierran en éxito / rechazo / derivación a recepción. Gating DURO propio
(`ALMA_AGENTS_CONVERSA_ENABLED=false`). **Engine-ready**: probado punta a punta;
el wiring en `flows.py` queda como paso guardado. Test: 15/15.

## Cómo se encadenan
```
percibir → ACTUAR (flota)
   → MEDIR (Ledger)              ── ¿funcionó?
   → APRENDER (#2 learning)      ── ¿qué variante conviene?
   → REACCIONAR (#1 eventos)     ── al instante, no por reloj
   → COMPETIR (#3 broker)        ── el de mayor valor contacta
   → PERSEGUIR (#4 goals)        ── metas con presupuesto
   → OBEDECER (#6 control NL)    ── "enfócate en kine"
   → CONVERSAR (#5 diálogos)     ── negociar, no fire-and-forget
```

## Encender (escalado, igual que la flota)
Nada de esto actúa con los defaults. Orden sugerido: encender el maestro y
observar (Simulador + Ledger) → prender un agente de bajo riesgo en dry-run →
execute on para briefing/sre → recién después contacto a pacientes (con consent +
templates + presupuesto). El plano de control y las metas solo **orientan** la
flota cuando ya está encendida; no la encienden.

Tests de toda la agencia: learning(10) · broker(8) · events(8) · goals(10) ·
control_plane(12) · conversation(15) = **63 checks**, todos verdes.
