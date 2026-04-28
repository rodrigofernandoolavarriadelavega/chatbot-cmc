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

## Simulador implementado

### `scripts/audit_properties.py`
Audita propiedades estáticas sobre el HISTORIAL de mensajes salientes en
`sessions.db` de producción (vía sqlcipher3). Detecta:
- Locale inglés (monday/april)
- Leak de número personal +56987834148
- Slots ofrecidos en el pasado (regex sobre "Te encontré hora ✨")
- Horario genérico aplicado a profesional específico
- Pago tarjeta para atención médica (no dental)

Resultado primer run (7d): 0 violaciones POST-fix · solo histórico pre-fix.

### `scripts/adversarial_chat.py`
21 conversaciones adversariales contra `handle_message()` con Medilink y Claude
mockeados de forma determinista. **Encontró 2 bugs nuevos en producción**:

- [x] **Bug K — UnboundLocalError `_MESES_ES`** (commit pendiente)
  - Variable global `_MESES_ES` (dict) shadowed por asignación local (lista)
    dentro de `handle_message`. Python trataba TODA la función como si
    `_MESES_ES` fuera local, fallaba en línea 3548 antes de definirse en 3807.
  - Fix: renombrar locales a `_DIAS_LBL` / `_MESES_LBL`.

- [x] **Bug L — UnboundLocalError `_slot_resp_c`** (commit pendiente)
  - Variable inicializada solo dentro de `if cercanos:`. Si `cercanos` vacío,
    fallaba al usarla más abajo.
  - Fix: inicializar a `None` antes del if + manejar caso None.

Casos cubiertos por el harness:
- locale_es_no_ingles
- no_personal_phone_leak
- horario_otorrino_real_no_inventado
- metodo_pago_separa_medico_dental
- control_mg_gratis_2_semanas
- para_hoy_avisa_si_no_hay_slot
- payload_huerfano_no_da_saludo_generico
- cierre_corto_no_repite_menu
- ecografia_abdominal_no_cae_en_fallback
- apellido_no_contamina_especialidad
- no_unbound_local_para_hoy
- rut_invalido_no_crashea
- saludo_solo_no_repite_menu_si_takeover
- cancel_sin_rut_no_crashea
- mensaje_vacio_no_crashea
- emergencia_deriva_samu
- boletas_no_crashea
- pregunta_horario_kine
- ortografia_rural
- respuesta_solo_numero
- agendar_para_otro

## Próximos pasos en la noche

- [ ] Agregar más casos al adversarial (replay de conversaciones reales)
- [ ] Iterar: encontrar más bugs, arreglar
- [ ] Sumar predeploy_check.sh con adversarial_chat.py

