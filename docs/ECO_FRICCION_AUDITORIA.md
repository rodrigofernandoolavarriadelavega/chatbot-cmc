# Auditoría de fricción — Ecografía (portavión de agentes)

> Generado por el workflow `portavion-eco-friccion` (18 agentes `cmc-conversation-auditor`
> + síntesis) sobre conversaciones reales de producción del chatbot CMC.
> Universo: **917 conversaciones** que tocan ecografía (de 48.652 mensajes / 3.035
> pacientes, rango 30-mar a 02-jun 2026). Revisadas a fondo las **360 de mayor
> fricción**; **160** con fricción real (severidad ≥2), 24 severas (sev 4-5).
>
> Reconstruido el 2026-06-09 (el doc original se perdió del working tree antes de
> commitearse). El estado de los fixes está actualizado a esta fecha.

## Veredicto

La ecografía es la mayor fuente de fricción del bot y el patrón dominante **NO es
de catálogo sino de máquina de estados**: el bot reconoce el tipo de eco en el
primer mensaje, pero al pulsar `agendar_sugerido` vuelve a preguntar el tipo y
luego no parsea la respuesta libre ("Abdominal") como selección válida, generando
loops de menú que terminan en takeover o abandono. Convierte intención de alta
calidad en cita perdida.

## Hallazgos priorizados

1. **[RESUELTO 2026-06-09] Menu-loop en `agendar_sugerido` (el peor).**
   El bot ya entendió el tipo de eco, pero al ofrecer "✅ Sí, agendar" guardaba
   `especialidad_sugerida="ecografía"` (genérico) y perdía el órgano; al aceptar,
   `_iniciar_agendar` no tenía el órgano (el texto del turno era el payload del
   botón) → `route_ecografia`→None → re-preguntaba el tipo → loop.
   **Fix** (commit `525426a`, `app/flows.py`): persistir `data["eco_tipo_text"]`
   al ofrecer agendar; `_iniciar_agendar` lo consume (pop) y rutea sin
   re-preguntar. Regresión cubierta en `tests/test_eco_menu_loop.py`.

2. **[PENDIENTE] Cobertura léxica incompleta.**
   `muslo`, `pierna`, `pantorrilla`, `lumbar`, `mano`, `omóplato/escápula`,
   `glúteos`, `tobillo` y el alias raíz `ecotomografía` no resuelven y caen a
   "preguntar tipo". Agregar a `ecografia_general_pardo["keywords"]` en
   `app/ecografias.py` (todos van a David Pardo, id 68). Ver
   `docs/ECO_VARIANTES_DAVID.md`.

3. **[PENDIENTE] Ruteo cruzado.**
   - `eco` a secas a veces cae a Rejón en vez de preguntar el tipo.
   - mamaria → ginecología en ~11 casos (cuando Claude detecta "ginecología" en
     vez de "ecografía"; route_ecografia ya manda mamaria a Pardo, pero si el
     `especialidad_sugerida` viene como "ginecología" se salta el branch de eco).
   - Revisar que la prioridad ginecología > general no secuestre `eco mamaria`.

4. **[PENDIENTE] FAQ Fonasa/precio no contextual.**
   La eco general es solo particular ($40.000) y el bot a veces responde otra cosa
   ante "¿la cubre Fonasa?" / "¿cuánto sale?". Hacer la respuesta de precio/Fonasa
   sensible al contexto de eco.

5. **[CRÍTICO — VERIFICAR] Fuga de datos en templates viejos de takeover.**
   El teléfono personal `+56987834148` y el código (41) aparecían en templates
   antiguos de takeover; el guard `_final_phone_guard` (44) debe cubrirlos.
   Confirmar que ningún template de derivación emita el número personal.
   Ver memory de auditoría del leak histórico (60 mensajes 2026-03/04).

## Clasificación operativa

- **data_safe** (se pueden aplicar sin revisión clínica): #1 (hecho), #2, #3.
- **logic_review** (requieren criterio): #4 (qué responder), #5 (seguridad).

## Cómo reproducir la auditoría

Workflow guardado en `chatbot-cmc/.claude/workflows/portavion-eco-friccion`.
Corre 18 `cmc-conversation-auditor` sobre lotes de conversaciones de eco +
1 agente de síntesis. Requiere acceso a `sessions.db` de producción (SQLCipher).
