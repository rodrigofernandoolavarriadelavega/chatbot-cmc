# Night log 2026-04-28

Sesión nocturna autónoma. Plan:
1. Terminar fixes pendientes del auditor + screenshots
2. Implementar simulador (tests de propiedades + replay)
3. Iterar: simulador → bug → fix → deploy

Cada fix con commit individual + predeploy_check + deploy. Si predeploy falla, NO deployar y dejar nota.

## Status de fixes

### En progreso / completados esta noche

- [x] **Bug A — Horarios inventados por profesional** (commit pendiente)
  - SYSTEM_PROMPT advertía a Claude que NO improvise horarios por profesional.
  - Helper `_responder_horario_por_especialidad` consulta Medilink directo (`_get_horario`).
  - Detector en handler IDLE para "qué día/hora atiende [esp]" → corta antes de Claude.
  - Trigger del bug: paciente preguntó días del otorrino, bot inventó "lun-vie 08-21". Real: lun-mié 16-20.

- [x] **Bug B — HUMAN_TAKEOVER sin TTL** (commit pendiente)
  - 107 sesiones bloqueadas en auditoría 7d, 29 con +7 días.
  - `session.reanudar_takeovers_expirados(horas_max=24)` resetea a IDLE.
  - Cron `_job_takeover_ttl` cada hora a los :15.
  - Loguea evento `takeover_ttl_reanudado` por phone para auditoría.

