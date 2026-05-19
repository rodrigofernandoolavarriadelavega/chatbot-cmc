# Sprint Win-back CMC — 2026-05 (en producción)

> Documento canónico del sprint. Cualquier sesión nueva de Claude (u otro asistente) que vaya a tocar `app/winback.py`, `app/custom_audiences_sync.py`, `app/jobs.py::_job_marketing_consent_blast`, los handlers de quick reply de `consent_marketing_v1` o el flujo de cohortes en BI debe leer este archivo primero.

## Por qué existe este sprint

El CMC tenía 14.677 pacientes en `bi.dim_paciente`, 66% one-shot (1 sola visita), y solo recuperaba revenue desde campañas Meta Ads frías ($1.09M/mes a CAC $2.183–$8.765). Win-back orgánico a la base existente = CAC cero + LTV recurrente ≈ +$2–3.5M/mes potencial reactivando 5%.

Hipótesis estratégica: la palanca de revenue #1 a 90 días no era contratar profesionales ni comprar más ads — era **reactivar pacientes que ya conocen el CMC**. Profundización sobre la base existente antes que expansión.

## Arquitectura final desplegada

### Infraestructura
- **BI Postgres migrado de Mac local a DigitalOcean** (container `health_bi_postgres`, postgres:16-alpine en `/opt/bi-postgres/`, bind `127.0.0.1:5432`, UFW deny 5432/tcp). Single source of truth en producción — el bot ya no depende del Mac de Rodrigo prendido.
- Backup pre-migración guardado: `~/bi-migration/bi_dump_20260513_0041.pgcustom`.

### Tablas nuevas en BI (schema `bi`)
- `marketing_consent(phone PK, status, consent_sent_at, response_at, response_method)` — tracking del opt-in de marketing post-Ley 21.719.
- `opt_outs_marketing(phone PK, source, reason, opted_out_at DEFAULT now())` — registro de bajas (footer "BAJA").
- `lista_espera_slots(paciente_id, especialidad, fecha_objetivo, prioridad)` — para anti no-show.
- `v_winback_cohortes_contactables` — vista con teléfonos válidos + sin opt-out.
- `v_winback_cohortes_con_consent` — vista filtrada por `marketing_consent.status='accepted'`.
- `winback_envios` ampliada: `especialidad`, `response_type`, `template_id_version`, `value_clp`, `cita_atribuida_id`.

### Módulos nuevos en `app/`
- `winback.py` — pool BI, candidatos por cohorte, lógica privacidad (sensibles vs no), `send_winback`, `process_inbound_response`, `run_daily_batch` (200/día, 30s gap, L-V 10:05–19:00).
- `custom_audiences_sync.py` — exporta 3 audiencias a Meta + Lookalike 1% Chile, SHA256, cron 04:00.
- `config.py::ARANCELES_CLP` — diccionario único de precios por especialidad (20 entradas). Usado por CAPI y métricas.

## Templates Meta (WABA_ID `962899599727507`)

### Aprobados y activos
| Template | Categoría | Idioma | Variables | Uso |
|---|---|---|---|---|
| `consent_marketing_v1` | UTILITY | es_CL | `{{1}}=nombre` | Re-consent obligatorio Ley 21.719 |
| `winback_medicina_general_v2` | MARKETING | es_CL | nombre, profesional | MG (Olavarría, Marquez, Abarca) |
| `winback_odontologia_v2` | MARKETING | es_CL | nombre, profesional | Odonto (Burgos, Marquez) |
| `winback_kinesiologia_v2` | MARKETING | es_CL | nombre, profesional | Kine (Etcheverry, Armijo, Borrego) |
| `winback_otorrino_v2` | MARKETING | es_CL | nombre | ORL (Dr. Borrego) |
| `winback_generico_sensible_v2` | MARKETING | es_CL | nombre | Psico/Gineco/Eco/Matrona (sin revelar especialidad) |
| `winback_one_shot_general_v2` | MARKETING | es_CL | nombre | Cohorte 365d, sin nombrar nada |

### Versiones obsoletas (NO usar)
Los 6 `winback_*_v1` aprobados antes tienen teléfono `(44) 296 5226` que es **incorrecto**. El código de área real de Carampangue es `(41)`. Los `_v1` quedaron dormidos pero APPROVED — no borrarlos para no perder historial. El mapeo en `winback.py::_TEMPLATE_MAP` apunta a `_v2` exclusivamente.

## Flujo legal Ley 21.719 (vigente 2026)

cmc-legal-compliance flageó BLOQUEANTE: el opt-in original (`method=rut_provided`) NO cubre marketing outbound. Solución implementada:

1. **Job `_job_marketing_consent_blast`** envía UTILITY `consent_marketing_v1` a 200 pacientes/día L-V 10:30 CLT.
2. Paciente recibe pregunta explícita "¿Aceptas recibir mensajes? SÍ/NO" con footer "BAJA" obligatorio.
3. Si responde **SÍ** → `marketing_consent.status='accepted'` + `privacy_consents(method='marketing_optin')`. **Event-driven**: bot dispara `send_winback` inmediato en el mismo turno.
4. Si responde **NO** → `marketing_consent.status='declined'` + insert en `opt_outs_marketing`.
5. Si responde **BAJA** en cualquier momento futuro → opt-out inmediato.

Frase clave del consent: *"Tu decisión no afecta tu atención médica"* — separa canal marketing del clínico.

Privacidad por especialidad:
- **Sensibles** (Psico, Gineco, Eco, Matrona): template genérico que NO menciona la especialidad.
- **No sensibles** (MG, Odonto, Kine, Otorrino, Nutri, Podo, Orto, Cardio, Traumato, Fono): template específico nombrando especialidad y profesional.

## Flags `.env` (gates duros)

- `MARKETING_CONSENT_BLAST_ACTIVE` — controla el envío del UTILITY consent.
- `WINBACK_ACTIVE` — controla el envío de winback MARKETING (event-driven y batch).
- `CROSS_SELL_ACTIVE` — controla el envío de cross-sell por dx (dm2/hta/pap). Mantener false hasta validar winback con ≥20 conversiones.

## Bugs encontrados en producción (con commits)

| # | Bug | Fix commit |
|---|---|---|
| 1 | `_hoy_cl` no asignado en handler WAIT_SLOT (preexistente, no del sprint) | `e6aef90` |
| 2 | `get_candidato_por_phone` con teléfono no normalizado (vista tenía formatos `+56...`, `9 ...`, `+5+6...`) | `870045b` |
| 3 | `messaging.py:374` `language="es"` hardcoded — Meta tiene templates en `es_CL`. Fix sistémico: `_get_template_language()` consulta Meta API con caché 1h | `400e82e` |
| 4 | `_job_marketing_consent_blast` no pasaba `body_params=[nombre]` al template UTILITY que tiene `{{1}}` | `d4129bf` |
| 5 | `opt_outs_marketing` INSERT con columna `created_at` que no existe (es `opted_out_at` con DEFAULT now()) + `spawn_task(name=...)` no soportaba kwarg | `66fde3f` |
| 6 | Teléfonos `(44)` en templates v1 — código real es `(41)`. Llevó a crear los `_v2` con número correcto | (templates Meta nuevos) |

## Bug Meta 131042 (Business eligibility payment issue)

Durante 13–16 may, todos los templates MARKETING (y algunos UTILITY) generaban wamid OK pero llegaban con delivery status `code=131042` 0.001s después — **el mensaje no llegaba al paciente**. Tendencia diaria: `69 → 18 → 9 → 1 → 0 → 0 → 0`. Resuelto solo a partir del 17-may (probable: cargo de billing aprobado en Meta Business Manager). Sin acción de código requerida — chequear `business.facebook.com/billing_hub` si vuelve a aparecer.

## Estado actual (19-may-2026)

| Métrica | Valor |
|---|---|
| Pool total contactables sin consent | 12.828 |
| Consent acepted | 13 |
| Consent declined | 5 |
| Consent pending | 200 (sin responder al primer batch del 13-may) |
| Winback enviados con delivery OK | 7 (del 13 al 18-may) |
| Flags | `WINBACK_ACTIVE=true`, `MARKETING_CONSENT_BLAST_ACTIVE=true`, `CROSS_SELL_ACTIVE=false` |
| Próximo blast UTILITY | hoy 10:30 CLT (200 mensajes, ~$1.400 CLP) |

## Cómo monitorear día a día

```sql
-- Status global consent
SELECT status, COUNT(*) FROM bi.marketing_consent GROUP BY status;

-- Enviados/respondidos hoy
SELECT
  COUNT(*) FILTER (WHERE consent_sent_at::date = CURRENT_DATE) AS enviados_hoy,
  COUNT(*) FILTER (WHERE response_at::date = CURRENT_DATE AND status='accepted') AS aceptaron_hoy,
  COUNT(*) FILTER (WHERE response_at::date = CURRENT_DATE AND status='declined') AS declinaron_hoy
FROM bi.marketing_consent;

-- Winbacks reales con delivery
SELECT enviado_at::date, COUNT(*) FROM bi.winback_envios
WHERE enviado_at::date >= CURRENT_DATE - 7 GROUP BY 1 ORDER BY 1;
```

Errores Meta:
```bash
ssh root@157.245.13.107 "grep \"$(date +%Y-%m-%d)\" /var/log/cmc-bot.log | grep -c '131042\\|132000\\|MSG FAILED'"
```

## Pendientes (próxima sesión)

1. **Consent escrito de profesionales** mencionados en templates (Burgos, Borrego, Armijo, Castillo, Etcheverry). Sin esto hay riesgo legal residual por uso de su nombre en marketing.
2. **Test CAPI Purchase** con 3 eventos reales a Pixel `915173108221261` para validar que el value_clp llega correcto post-fix.
3. **Cross-sell** (`CROSS_SELL_ACTIVE=true`) solo después de ≥20 conversiones validadas del winback.
4. **Documentar en CLAUDE.md** sección "Sesión en curso" un link a este archivo cada vez que se toque algo del sprint.

## Servidor y acceso

- DigitalOcean: `root@157.245.13.107` (SSH llave Ed25519, password deshabilitado desde 2026-04-10)
- Bot: `/opt/chatbot-cmc/`, systemd service `chatbot-cmc`, uvicorn puerto 8001
- BI: `/opt/bi-postgres/`, container Docker `health_bi_postgres`
- Logs bot: `/var/log/cmc-bot.log`
- Health: `https://agentecmc.cl/health`
