# Alma Agents — flota de agentes autónomos

Una flota de **18 agentes** que operan la clínica sola: perciben el estado,
deciden, y (si los guardrails lo permiten) actúan. Construida 2026-06-02.

**Todos nacen APAGADOS.** Encender es una decisión deliberada y escalonada por
variables de entorno. Con los defaults, la flota es **inerte**: el scheduler no
registra ni un job.

## El insight: el riesgo es el AGREGADO, no el agente

Con 18 agentes que pueden contactar pacientes, mover plata y escribir en Medilink,
el peligro no es ninguno individual — es que entre todos spameen al mismo paciente
o ejecuten algo ilegal. Por eso el centro del diseño es `guardrails.authorize()`:
**toda acción de todo agente pasa por ahí** antes de tener efecto.

## Gating en cascada (todo OFF por defecto)

```
ALMA_AGENTS_ENABLED        maestro — si false, 0 jobs, flota inerte
  └─ ALMA_AGENT_<NOMBRE>   flag por agente — si false, ese agente no corre
       └─ ALMA_AGENTS_EXECUTE   si false, TODO es dry-run (propone, no actúa)
            └─ por cada acción, piso NO-DELEGABLE:
               · horas de silencio (no contactar pacientes 21–09 CLT)
               · consent vigente (Ley 21.719) para contacto a pacientes
               · presupuesto de contacto por paciente (máx N/semana, sumado
                 entre TODOS los agentes — anti-spam agregado)
               · escrituras Medilink off (ALMA_BRAIN_ALLOW_MEDILINK_WRITES)
               · riesgo 'extremo' off (ALMA_AGENTS_ALLOW_EXTREME)
```

El contacto a STAFF (Rodrigo/recepción) salta consent/budget pero respeta execute.
El piso legal NO lo decide ningún modelo y no se salta subiendo execute.

## La flota (18 agentes)

| Agente | Riesgo | Qué hace | Contacta | Flag |
|---|---|---|---|---|
| briefing | bajo | resumen ejecutivo diario | Rodrigo | ALMA_AGENT_BRIEFING |
| sre_watchdog | bajo | vigila salud, escala caídas | Rodrigo | ALMA_AGENT_SRE |
| supervisor | bajo | vigila a la flota, detecta sobre-contacto | Rodrigo | ALMA_AGENT_SUPERVISOR |
| conciliacion_auto | bajo | cuadre financiero, alerta gaps | Rodrigo | ALMA_AGENT_CONCILIACION |
| demanda_estrategia | bajo | recomienda contratación/especialidades | Rodrigo | ALMA_AGENT_DEMANDA_ESTRATEGIA |
| pricing_analyst | medio | detecta fuga de copago/ticket bajo | Rodrigo | ALMA_AGENT_PRICING |
| seo_content | medio | audita SEO, propone contenido | Rodrigo | ALMA_AGENT_SEO |
| creativos | medio | genera piezas por demanda → galería | — | ALMA_AGENT_CREATIVOS |
| postconsulta_smart | medio | seguimiento post-consulta, escala "peor" | paciente | ALMA_AGENT_POSTCONSULTA |
| abandono_recovery | medio | re-engancha reservas abandonadas | paciente | ALMA_AGENT_ABANDONO |
| reputacion | medio | pide reseña a pacientes NPS-alto | paciente | ALMA_AGENT_REPUTACION |
| yield_agenda | alto | contacta demanda reprimida al abrir cupo | paciente | ALMA_AGENT_YIELD |
| control_cronico | alto | controles crónicos vencidos → agendar | paciente | ALMA_AGENT_CONTROL_CRONICO |
| adherencia_kine | alto | kine en riesgo de abandono → nudge | paciente | ALMA_AGENT_ADHERENCIA_KINE |
| reactivacion_winback | alto | win-back RFM inactivos (consent) | paciente | ALMA_AGENT_WINBACK |
| ads_executor | alto | ejecuta decisiones del Autopilot (mueve plata) | — | ALMA_AGENT_ADS_EXECUTOR |
| inventario_auto | alto | predice quiebre dental → orden de compra | — | ALMA_AGENT_INVENTARIO |
| cobranza | **extremo** | cobra copagos impagos a pacientes | paciente | ALMA_AGENT_COBRANZA |

## Panel de control

Módulo `agentes` ("Flota de Agentes") en el shell → `/alma/agents` (solo dueño).
- Ve cada agente: riesgo, flag, schedule, último run.
- Muestra los kill-switches (read-only — se controlan por .env).
- **Previsualizar (dry-run)**: corre perceive+decide de un agente y muestra qué
  HARÍA y si cada acción se ejecutaría o se bloquearía (y por qué). Seguro, no actúa.

Endpoints: `GET /alma/agents`, `GET /alma/agents/api/fleet`,
`POST /alma/agents/api/{id}/dryrun`.

## Cómo encender (escalado recomendado)

1. **Observar**: `ALMA_AGENTS_ENABLED=true` con todos los `ALMA_AGENT_*` en false,
   o usa el panel para previsualizar dry-runs. La flota agenda pero nadie actúa.
2. **Un agente de bajo riesgo en dry-run**: activa su flag (ej. `ALMA_AGENT_BRIEFING=true`)
   con `ALMA_AGENTS_EXECUTE=false`. Verás en el decision log qué propondría.
3. **Ejecución real, bajo riesgo primero**: `ALMA_AGENTS_EXECUTE=true`. Empieza por
   briefing/sre (solo te contactan a ti). Sube el presupuesto de contacto despacio.
4. **Contacto a pacientes**: requiere consent vigente (Ley 21.719) + templates
   Meta aprobados + `ALMA_AGENTS_CONTACT_MAX_WEEK` razonable.
5. **Medilink writes / extremo**: `ALMA_BRAIN_ALLOW_MEDILINK_WRITES=true` /
   `ALMA_AGENTS_ALLOW_EXTREME=true` — solo cuando confíes plenamente.

## Flags .env

```bash
ALMA_AGENTS_ENABLED=false           # maestro
ALMA_AGENTS_EXECUTE=false           # actuar (si no, dry-run)
ALMA_AGENTS_ALLOW_EXTREME=false     # habilita agentes de riesgo extremo (cobranza)
ALMA_AGENTS_REQUIRE_CONSENT=true    # contacto a pacientes exige opt-in
ALMA_AGENTS_CONTACT_MAX_WEEK=2      # tope de mensajes proactivos por paciente / 7d
ALMA_AGENTS_QUIET_START=21          # horas de silencio (no molestar)
ALMA_AGENTS_QUIET_END=9
ALMA_BRAIN_ALLOW_MEDILINK_WRITES=false   # compartido con el cerebro/operativa
ADMIN_ALERT_PHONE=569...            # a dónde llegan los reportes a Rodrigo
# + un ALMA_AGENT_<NOMBRE>=false por cada agente (ver tabla)
```

## Arquitectura

```
base.py          Agent (perceive/decide/execute_one) + loop run() común
guardrails.py    authorize() — el piso de seguridad (gating en cascada)
store.py         último run por agente + decision log (data/alma_agents/)
registry.py      descubre agentes automáticamente (soltar archivo = sumar agente)
scheduler_hook.py register_agent_jobs() — inerte si maestro off
agents/*.py      un archivo por agente (subclase de Agent + AGENT = ...)
routes.py        panel /alma/agents + dry-run preview
```

Reusa funciones existentes (winback, fidelizacion, autopilot, inventario, kine,
google_rating, conciliacion, alma_brain) — no reinventa lógica de negocio.

## Tests

```bash
python3 tests/test_alma_agents.py   # guardrails + registry + dry-run, sin red
```
