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

- [x] **Bug B — HUMAN_TAKEOVER sin TTL** (commit `0efef82`)
  - 107 sesiones bloqueadas en auditoría 7d, 29 con +7 días.
  - `session.reanudar_takeovers_expirados(horas_max=24)` resetea a IDLE.
  - Cron `_job_takeover_ttl` cada hora a los :15.
  - Loguea evento `takeover_ttl_reanudado` por phone para auditoría.

- [x] **Bug C — WAIT_SLOT interrumpido por recepcionista** (commit pendiente)
  - 55 takeovers desde WAIT_SLOT/7d, solo 1 cita.
  - `admin_routes.py`: si paciente está en estado TRANSACCIONAL, NO setear
    HUMAN_TAKEOVER — recepcionista puede escribir en paralelo sin interrumpir.
  - Loguea `recep_msg_durante_flow` para análisis.

- [x] **Bug D — Apellido alucinado como especialidad** (commit pendiente)
  - Caso real 2026-04-28 (56993584481): "Tiene hora para médico mañana?"
    → Claude retornó esp="jimenez" → bot ofreció odontología.
  - `_APELLIDOS_INDIVIDUALES_KEYS` set con apellidos como jimenez/abarca/etc.
  - Si Claude retorna esp en este set Y el txt no contiene el apellido →
    fallback a `_detectar_especialidad_en_texto`.

- [x] **Bug E — "ver_otros" duplica slot único** (commit pendiente)
  - Caso real 2026-04-28 (56934363158): bot ofreció Dr. Abarca 08:00, paciente
    clickeó ver_otros, bot mostró el mismo slot.
  - Si `len(todos_slots) <= 1`, expandir a OTRO día con `excluir=[fecha_actual]`.
  - Si tampoco hay otros días, mensaje claro "única hora disponible".

- [x] **Bug F — WAIT_WAITLIST_CONFIRM no resetea con nuevo intent** (pending)
  - Caso real 2026-04-28 (56989488187): paciente dijo "médico para viernes",
    bot ofreció waitlist de implantología (de un flujo anterior contaminado).
  - Si paciente envía nuevo intent (palabras de agendar/cancelar/etc),
    `reset_session` y reentra `handle_message` con sesión limpia.

- [x] **Bug G — Ecografías por tipo no detectadas** (commit `1202ce7`)
  - Auditoría: 12 sin_disponibilidad/7d en ecografía porque solo "transvaginal"
    se ruteaba a Ginecología; abdominal/renal/tiroidea/etc. caían a fallback.
  - `_INTENT_CACHE`: agregadas 18 variantes de ecografía con intent="agendar"
    + esp correcta (ginecología para transvaginal/pélvica, ecografía para
    el resto = David Pardo).

- [x] **Bug I — Fallback loop counter** (pending)
  - Caso real 56971038302: bot mandó 4 menús en 26s sin entender al paciente.
  - Contador `data["fallback_otro_count"]` aumenta con cada intent="otro" o
    "menu". Al llegar a 3 → escala a HUMAN_TAKEOVER con mensaje claro.
  - Si paciente avanza, el contador se resetea.

- [x] **Bug J — GES triage pre-filtro mejorado** (pending)
  - 7 nomatch/7d con intención clara: "tengo hora hoy con Dr X", "no podré
    asistir", etc. → caían al triage GES en vez de cancelar/reagendar.
  - `_TRIAGE_SKIP_KWS` extendido con: frases de gestión de cita ("tengo hora",
    "no podre", "mi cita", "horita", etc.) + apellidos de TODOS los
    profesionales del CMC. Si menciona apellido, NO es síntoma — es gestión.

- [x] **Bug H — Reagendar id_cita_old null** (pending)
  - Auditoría: 2 casos con cita_creada que tiene id_cita_old=null y SIN
    cita_cancelada — flag `reagendar_mode` se perdía en save_session intermedio.
  - Defensa: `reagendar = bool(reagendar_mode) or bool(cita_old.get("id"))`.
    Si hay cita_old con id en data, tratar como reagendar.

## Pendientes de la noche

- [ ] Implementar simulador (props + replay)
- [ ] Iterar con simulador, encontrar bugs nuevos, arreglar

