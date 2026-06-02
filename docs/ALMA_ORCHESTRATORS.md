# Alma — Orquestadores (catálogo y estado)

> Sesión autónoma 2026-06-02 (noche): "crea todos los orquestadores que se te ocurran,
> déjalos apagados, crea las herramientas que falten". TODO gateado OFF, sin commit, sin deploy.

## Qué es un orquestador

Un agente acotado que vive en `app/alma_brain/orchestrators/`. Hereda de `base.Orchestrator`
y sigue el ADN de alma_brain: **la IA propone, las reglas mandan**.

Ciclo: `sense()` (read-only) → `propose()` (acciones concretas) → `run(mode)`:
- `mode="dry"`  → sense + propose, **NO persiste ni ejecuta**. Siempre disponible (preview del panel), incluso con el orquestador apagado.
- `mode="propose"` → encola propuestas para aprobación humana (Fase 3). Requiere el orquestador encendido.
- `mode="execute"` → encola + auto-ejecuta lo que la política permita (Fase 4). Requiere encendido + `ALMA_BRAIN_EXECUTE`.

Kill-switch por orquestador: `ALMA_ORQ_<NAME>_ENABLED` (default **false**).
Con todo apagado, el bot es idéntico a hoy.

## Chasis (herramientas base)

- [x] `base.py` — `Orchestrator` + `OrqResult` + gating + helpers de propuesta.
- [x] `__init__.py` — registro (`REGISTRY`), `run_all`, `run_one`, `catalog`.
- [x] reusa `tools.add_proposal` (cola existente) + `policy.check` (límites duros).
- [x] `snapshot.py` — corre `run_all(mode="dry")` y persiste a `data/alma_orq_snapshot.json` (atómico, gitignored). Endpoint `GET /admin/api/orquestadores/snapshot[?refresh=1]`. El panel lo lee al instante (no re-toca BI/Medilink en cada carga).
- [x] `briefing.py` — aplana el snapshot en un digest priorizado (alta prioridad + tamaño de worklist): el "resumen matinal" de Alma observadora. Endpoint `GET /admin/api/orquestadores/briefing[?refresh=1]`. El panel lo muestra como tarjeta "Briefing del día" arriba de todo.
- [x] `scripts/alma_orq_snapshot.py` — CLI que refresca el snapshot (build_and_save) sin tocar main.py. Cron sugerido (NO registrado, requiere aprobación): `10 6 * * * cd /opt/chatbot-cmc && PYTHONPATH=app:. python3 scripts/alma_orq_snapshot.py >> /var/log/alma-orq.log 2>&1`.

## Catálogo de orquestadores

| Name | Dominio | Señal → Acción | Fuente de datos | Estado |
|------|---------|----------------|-----------------|--------|
| `confirmaciones` | Agenda | citas de mañana sin confirmar → nudge; sin respuesta → liberar a waitlist | citas_cache / Medilink | [x] |
| `hueco_proactivo` | Agenda | huecos en N días → ofrecer a lista de espera (generaliza Fase 4) | medilink slots + waitlist | [x] |
| `no_show_recovery` | Agenda | inasistencia detectada → reagendar + recuperar cupo | citas_bot + pagos | [x] |
| `adherencia_kine` | Kine | pacientes kine en riesgo/abandono → nudge de adherencia | bi.fact_atenciones esp=3 | [x] |
| `controles_ortodoncia` | Ortodoncia | controles vencidos (>45d) → recordatorio de control | bi orto esp=19 | [x] |
| `cobranza_ortodoncia` | Finanzas | saldos vencidos / cartera → recordatorio de pago | ortodoncia_plan | [x] |
| `demanda_abrir_agenda` | Demanda | especialidad con N solicitudes sin cupo → proponer abrir agenda / avisar médico | sensors.sense_demanda | [x] |
| `inventario_compra` | Inventario | stock bajo punto de reorden → orden de compra | inventario module | [x] |
| `reactivacion` | Fidelización | cohortes inactivas contactables → win-back dirigido | sensors.sense_fidelizacion | [x] |
| `reputacion_nps` | Reputación | NPS alto → pedir reseña Google; NPS bajo → alertar dueño | sessions.db NPS | [x] |
| `resultados_examenes` | Clínico | informe listo sin avisar → notificar + seguimiento | tabla nueva `resultados_pendientes` | [x] |
| `preventivo_edad` | Clínico | pacientes en ventana de examen preventivo (PAP/mamo/PSA/EMPAM) → invitar | dim_paciente edad/sexo | [x] |
| `cumpleanos` | Fidelización | cumpleañeros de hoy → saludo + tip preventivo | dim_paciente fecha_nacimiento | [x] |
| `crosssell_postconsulta` | Fidelización | pacientes satisfechos → cross-sell contextual por especialidad | get_promotores_recientes | [x] |
| `campanas_estacionales` | Marketing | mes del año → campaña de salud pertinente | calendario | [x] |
| `agenda_salud` | Agenda | caída/subida fuerte de agenda → activar palancas (meta-orquestador) | sensors.sense_agenda | [x] |
| `ges_backlog` | Clínico | GES detectó síntoma→especialidad pero no agendó → seguir (urgentes priorizados) | conversation_events triage_ges_match | [x] |
| `primera_vez_sin_retorno` | Fidelización | pacientes de 1 sola atención (30–120d) sin volver → reenganche (retención de nuevos) | bi.fact_atenciones + dim_paciente | [x] |
| `control_cronico` | Clínico | pacientes dx:* (HTA/DM2/...) sin cita hace >90d → recordar control | contact_tags + citas_bot (sessions.db) | [x] |
| `ficha_incompleta` | Datos | pacientes con teléfono pero sin email/comuna/fecha_nac → completar ficha | bi.dim_paciente | [x] |
| `ads_anomalia` | Marketing | Meta sobre-atribuye (ratio<0.7) / campañas sin gasto / acciones pendientes → revisar Ads | sensors.sense_ads (autopilot) | [x] |
| `referral_sin_cerrar` | Marketing | leads de Meta Ads (CTWA) que no agendaron → seguir para cerrar | meta_referrals + citas_bot | [x] |
| `conversacion_parada` | Atención | último mensaje del paciente sin responder (2h–7d) → responder | messages (sessions.db) | [x] |

**Total: 23 orquestadores.** Herramientas nuevas creadas para ellos: tabla `resultados_pendientes` + helpers, `get_promotores_recientes()`, endpoints `/admin/api/orquestadores[/dryrun]` + `/admin/api/orquestadores/propuestas` (+approve/reject) + `/admin/api/resultados`.

## Panel UI (inbox + catálogo)

- [x] `templates/alma_orquestadores.html` — estilo premium Alma (Montserrat + aqua/navy + cards 16px). Catálogo de los 17 con badge on/off, botón "Ver qué propondría" por orquestador (dry-run en vivo, sin tocar nada), e inbox de propuestas con aprobar/descartar.
- [x] Ruta `GET /alma/orquestadores` en `admin_routes.py` (NO toca main.py/alma.html — auth token/cookie como el resto de Alma). Pendiente opcional: sumarlo al sidebar del shell cuando no haya WIP paralelo.

## Reglas de oro (todas cumplidas)

1. **OFF por defecto** — cada `ALMA_ORQ_*_ENABLED=false`. Dry-run siempre seguro.
2. **Degradación elegante** — si una fuente no responde, el orquestador devuelve vacío con `signals.source_status`, nunca 500.
3. **Contacto a pacientes = compuerta legal** — cualquier acción de contacto masivo pasa por `policy.check` (Ley 21.719: consent + templates). El orquestador NO la salta.
4. **Sin auto-escritura salvo gate angosto** — Medilink/contacto solo con el flag dedicado del orquestador + `ALMA_BRAIN_EXECUTE`.
5. **Trazabilidad** — toda propuesta y ejecución al decision log existente.

## Pendientes (requieren aprobación / tocan prod)

- Cron que corra `run_all(mode="propose")` (hoy NO registrado para no tocar main.py con WIP de sesiones paralelas).
- Sumar `/alma/orquestadores` al sidebar del shell Alma (cuando no haya WIP paralelo en alma.html/config.py).
- Encender flags uno a uno, tras Fase 0 (auth dura + auditoría).

## Self-audit (iter 7)

Revisión de los 21 orquestadores. Veredicto: **sólido**. Detalle:
- **Degradación**: doble red. (a) cada `sense()` que toca una fuente opcional (BI, autopilot, Medilink) chequea disponibilidad y devuelve `source_status="unavailable"` con listas vacías; (b) `base.run()` envuelve `sense()` y `propose()` en try/except → una excepción nunca tumba el barrido (`run_all`), degrada a vacío. Verificado: `test_run_all_dry_no_crashea` corre los 21 sin BI/Medilink y ninguno crashea.
- **propose() con señales vacías**: todos abren con guarda `if not <lista>: return []`. Cero crashes con datos vacíos.
- **Caps de worklist**: la mayoría ya acotaba (TOP 15–40 / per-exam 25). Endurecidos los 3 naturalmente-acotados que faltaban: `confirmaciones` y `no_show_recovery` (worklist ≤80), `crosssell_postconsulta` (≤25 por bucket).
- **PII**: las worklists exponen solo `nombre` + `telefono` (+ wa.me) y el campo contextual mínimo necesario (especialidad / examen / faltan-datos). Único dato sensible: `control_cronico` incluye el dx (HTA/DM2) en la worklist — es necesario para que recepción sepa qué control recordar, y es panel interno autenticado. Aceptable y anotado.
- **Sin auto-contacto**: 20/21 proponen `tarea_manual` (worklists para humano). El único con executor (`hueco_proactivo`) delega en `operativa`, doblemente gateada.

## Bitácora de la noche

- Iter 1: chasis + 15 orquestadores + tools + endpoints + tests (6/6).
- Iter 2: +panel UI (`alma_orquestadores.html` + ruta `/alma/orquestadores`) +2 orquestadores (`agenda_salud`, `ges_backlog`). 17 total. Tests 9/9. 3 suites Alma verdes (23 tests).
- Iter 3: +snapshot dry-run persistido (`snapshot.py` + endpoint + panel lo consume al instante) +1 orquestador (`primera_vez_sin_retorno`, retención de nuevos vía BI). 18 total. Tests 10/10. 3 suites verdes (24 tests).
- Iter 4: +briefing diario (`briefing.py` + endpoint + tarjeta "Briefing del día" en el panel — resumen matinal priorizado de Alma observadora). Tests 11/11. 3 suites verdes (25 tests).
- Iter 5: +CLI `scripts/alma_orq_snapshot.py` (refresca snapshot por cron sin tocar main.py) +2 orquestadores (`control_cronico` vía contact_tags+citas_bot sin BI, `ficha_incompleta` vía BI) +tool `get_cronicos_para_control`. 20 total. Tests 13/13. 3 suites verdes (27 tests).
- Iter 6: +orquestador `ads_anomalia` (sense_ads: ratio<0.7 / campañas sin gasto / acciones pendientes) +doc de handoff matinal `docs/ALMA_NOCHE_HANDOFF.md` (review + orden seguro de encendido + Fase 0) +runner consolidado `tests/run_alma_suite.py`. 21 total. Tests 14/14. 3 suites verdes (28 tests).
- Iter 7 (calidad): self-audit de los 21 (ver sección arriba) + caps endurecidos en 3 orquestadores + panel polish (filtro por dominio: chips clicables con conteo) + endpoint `GET /admin/api/orquestadores/metrics` (por dominio + on/off). Tests 16/16. 3 suites verdes (30 tests).
- Iter 8 (verificación): test de **integración end-to-end** `tests/test_alma_orq_integration.py` (TestClient sobre admin_routes.router, sin main.py): catálogo/metrics/snapshot/briefing/dryrun/propuestas → 200 + JSON sano, página `/alma/orquestadores` → 200 HTML con token inyectado, sin/mal token → 401/403. Agregado al runner (`run_alma_suite.py` ahora corre 4 suites). Handoff doc con conteos + URL del panel. **31 tests verdes en 4 suites.**
- Iter 9: recon confirmó data sólida → +2 orquestadores (`referral_sin_cerrar` vía meta_referrals+citas_bot, `conversacion_parada` vía messages). **23 orquestadores.** Tests con datos sembrados (in/out, lead con/sin cita). 4 suites verdes (~33 tests).
- Iter 10 (CIERRE): verificación final (4 suites verdes, todo OFF, import profundo OK) + sección "Cierre de la noche" en el handoff (totales + backlog de orquestadores futuros que necesitan infra de datos nueva). NO se agregaron orquestadores de bajo valor a propósito. Sesión nocturna cerrada limpiamente. **23 orquestadores, ~3.200 LOC nuevas, ~32 tests + integración, todo OFF, sin commit/deploy.**
