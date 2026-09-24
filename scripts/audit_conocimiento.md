# Conocimiento del portaviones — lo que el auditor DEBE saber

> Lo lee `conversation_audit_swarm.py` en cada corrida (auditor horario,
> consolidador y verificador). Editar aquí cuando cambie una regla del negocio
> o aparezca un falso positivo recurrente. Los datos de profesionales, precios y
> abonos NO van aquí: se generan desde el código en cada corrida.
> Lo aprendido automáticamente por el verificador se agrega en
> `/var/log/cmc-audit/aprendido.md` (fuera del repo).

## El centro
- Centro Médico Carampangue (CMC), UNA sola sede, Carampangue (Arauco, Biobío).
  "Olavarría" es un médico, no un lugar. Nunca prometer otras sucursales.
- Pacientes rurales, mayoría Fonasa, escriben con typos y chilenismos.
  "cancelar" a veces significa PAGAR.
- Bot WhatsApp +56966610737 · fijo (44) 296 5226. El número personal
  +56987834148 NUNCA debe aparecer en un mensaje al paciente (leak = severidad alta).
- Prohibido decir "certificados/habilitados/acreditados/Superintendencia" y el
  argumento "sin necesidad de viajar".
- El bot atiende niños y NUNCA los manda al CESFAM.
- Tarjetas (débito/crédito) SOLO en dental.
- Tono: chileno, SIN voseo (nada de "querés", "tenés", "podés").

## Diseños DELIBERADOS (no son bugs)
- Abono total anticipado en Psiquiatría, Gastroenterología y Neurología
  (reglas en config.ABONO_REGLAS). Que el bot pida el abono antes de crear la
  hora es correcto. Lo que SÍ es bug: montos que no calzan con ABONO_REGLAS,
  o texto contradictorio ("abono previo" + "se paga el día de la atención").
- Dr. Alonso Márquez: Medicina General/Familiar a $30.000 particular; entra al
  pool de MG como overflow (bypass intencional). Que aparezca como alternativa
  no es bug; sí lo es no informar su precio distinto.
- Dr. Rodrigo Olavarría atiende Medicina General en la TARDE-NOCHE (~16-21):
  horas 20:00-20:45 con él son válidas si están en su agenda.
- Reenganche automático 10-60 min tras abandono de un flujo activo: feature.
  Bug solo si dispara sin abandono real, sin contexto, o tras cita confirmada.
- Auto-inscripción en lista de espera con opt-out ("responde BAJA") cuando el
  paciente tiene perfil completo: deliberado (sin ella nadie se inscribía).
- Mensaje de derivación cuando el CMC no tiene la especialidad (CESFAM red +
  clínicas de Concepción): decisión del dueño (commit 45907e4).
- Si no hay cupo hoy, ofrecer otro día NO es bug. Sí lo es ofrecerlo en
  silencio cuando el paciente pidió un día específico, o a meses sin pedirlo.
- Anuncios Click-to-WhatsApp: "Quiero más información" llega con referral del
  anuncio (p.ej. ortodoncia); responder sobre ese servicio es correcto.
- Sobrecupos de ecografía (David Pardo, id 68): el bot ofrece horas extra
  marcadas [SOBRECUPO] además de la agenda formal (2º tecnólogo). Son válidas.

## Falsos positivos ya vistos (descartar)
- "Unibazo ofertada un lunes": eran citas fantasma existentes en Medilink.
- "Reseña antes del upsell": arreglado 22-ago (secuenciación postconsulta).
- "Márquez en el pool de MG": bypass intencional.
- "El bot pidió abono de psiquiatría": política real.

## Señales de bug que el transcript solo NO muestra (mirar los EVENTOS)
- `sin_disponibilidad` / `waitlist_inscrito_auto` en serie para una misma
  especialidad = agenda sin días abiertos o búsqueda rota. Contrastar con
  `sobrecupo_*` y con que recepción sí haya ofrecido horas ese día.
- `sobrecupo_rechazado_race`, `reserva_resultado` con error, `cita_bloqueada_*`.
- `agendar_vacio_con_breaker_caido`, `breaker_falso_positivo`: Medilink.
- 429 de Medilink en el log justo antes de un "no hay horas": la búsqueda quedó
  a medias; decir "no hay horas" ahí es bug.

## Mapa REAL del código (usar SOLO estos nombres en target)
- app/flows.py — máquina de estados completa (handle_message, _iniciar_agendar,
  WAIT_* handlers, precios PRECIOS_SLOT, textos al paciente).
- app/claude_helper.py — detect_intent, respuesta_faq, classify_with_context,
  SYSTEM_PROMPT con glosario/precios, _INTENT_CACHE.
- app/medilink.py — PROFESIONALES, buscar_primer_dia, buscar_slots_dia,
  smart_select, crear_cita, cancelar_cita, _get (429/backoff).
- app/sobrecupo.py — generar_slots / crear_sobrecupo (capas C2/C3).
- app/ecografias.py — route_ecografia, vocabulario de tipos de eco.
- app/jobs.py — crons: recordatorios, reenganche, waitlist, watchdogs.
- app/fidelizacion.py — post-consulta, cross-sell, reactivación.
- app/pni.py / app/autocuidado.py — vacunas por edad / tips.
- app/main.py — webhook Meta (WA/IG/FB), media, scheduler.
- app/session.py — sesiones, messages, conversation_events, message_statuses.
- app/config.py — flags, ABONO_REGLAS, teléfonos.
- app/resilience.py / app/medilink_outage.py — breaker y modo caída.
- app/persistencia.py — segundo toque a consultas abiertas.
Si no sabes dónde está, pon null. NO inventes archivos.
