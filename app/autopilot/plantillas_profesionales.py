"""Biblioteca comercial de plantillas WhatsApp por profesional del CMC.

Una plantilla Meta-ready por cada profesional activo de PROFESIONALES
(medilink.py). Copy comercial y entendible (chileno, directo, beneficio +
precio real + CTA), con la variable {{1}} = nombre del paciente.

Fuente de precios: PRECIOS_SLOT en flows.py — lo que el bot ya le muestra a
los pacientes al ofrecer horas. Si cambia un arancel ahí, actualizar acá.

Esto NO envía nada: es la capa de borradores del embudo de templates.
  borrador (acá) → enviar a aprobación Meta → APPROVED (pestaña Plantillas
  ya las lista en vivo) → el bot puede usarlas en frío.

Cada entrada incluye `meta_payload()`-able fields para crear el template vía
POST /{WABA_ID}/message_templates sin re-escribir componentes.
"""

# Meta limita el FOOTER a 60 caracteres — este mide 53.
FOOTER = "Centro Médico Carampangue · SALIR para no recibir más"

# Botones por defecto: 3 quick-replies (máx Meta para QUICK_REPLY puros).
_BTN_DEFAULT = ["Agendar mi hora", "Ver horarios", "No por ahora"]

# area: agrupa las cards en el dashboard. emoji: solo decorativo en la UI.
BIBLIOTECA: list[dict] = [
    # ── Área Médica ──────────────────────────────────────────────────────────
    {
        "id_profesional": 73,
        "profesional": "Dr. Andrés Abarca",
        "especialidad": "Medicina General",
        "area": "Médica", "emoji": "🩺",
        "template_name": "prof_mg_abarca_v1",
        "precio_line": "Fonasa $7.880 · Particular $25.000",
        "angle": "Acceso rápido sin viajar a Concepción. Horario de día (08–16 h).",
        "header": "Médico general en Carampangue",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Necesitas ver a un médico y no quieres viajar a Concepción ni "
            "esperar semanas?\n\n"
            "El *Dr. Andrés Abarca* atiende de lunes a viernes en horario de "
            "día, aquí mismo en Carampangue.\n\n"
            "✅ Con *Fonasa pagas solo $7.880* (particular $25.000)\n"
            "✅ Horas disponibles esta misma semana\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y te damos la hora al tiro 📅"
        ),
    },
    {
        "id_profesional": 1,
        "profesional": "Dr. Rodrigo Olavarría",
        "especialidad": "Medicina General",
        "area": "Médica", "emoji": "🌙",
        "template_name": "prof_mg_olavarria_v1",
        "precio_line": "Fonasa $7.880 · Particular $25.000",
        "angle": "Horario vespertino (16–21 h): consulta sin pedir permiso en el trabajo.",
        "header": "Consulta médica después del trabajo",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Te cuesta ir al médico porque trabajas todo el día? Tenemos la "
            "solución.\n\n"
            "El *Dr. Rodrigo Olavarría* atiende en *horario vespertino, hasta "
            "las 21:00 hrs*, en el Centro Médico Carampangue.\n\n"
            "✅ Sales del trabajo y llegas a tu consulta\n"
            "✅ Con *Fonasa pagas solo $7.880* (particular $25.000)\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y asegura tu cupo de tarde 🌙"
        ),
    },
    {
        "id_profesional": 13,
        "profesional": "Dr. Alonso Márquez",
        "especialidad": "Medicina Familiar",
        "area": "Médica", "emoji": "👨‍👩‍👧",
        "template_name": "prof_mf_marquez_v1",
        "precio_line": "Fonasa $7.880 · Particular $30.000",
        "angle": "Un solo médico para toda la familia: niños, adultos y adultos mayores.",
        "header": "Un médico para toda tu familia",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Sabías que en Carampangue hay un *médico familiar*? Atiende a "
            "niños, adultos y adultos mayores: toda la familia con el mismo "
            "doctor, que conoce su historia.\n\n"
            "El *Dr. Alonso Márquez* atiende en el Centro Médico Carampangue.\n\n"
            "✅ Con *Fonasa pagas solo $7.880* (particular $30.000)\n"
            "✅ Control de crónicos, niños sanos y adultos mayores\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y cuida a los tuyos 👨‍👩‍👧‍👦"
        ),
    },
    {
        "id_profesional": 60,
        "profesional": "Dr. Miguel Millán",
        "especialidad": "Cardiología",
        "area": "Médica", "emoji": "❤️",
        "template_name": "prof_cardio_millan_v1",
        "precio_line": "Particular $40.000",
        "angle": "Cardiólogo en Carampangue (atiende viernes) — sin viajar ni esperar meses.",
        "header": "Cardiólogo en Carampangue, sin viajar",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Presión alta, palpitaciones o antecedentes de problemas al "
            "corazón en tu familia? No lo dejes pasar.\n\n"
            "El *Dr. Miguel Millán, cardiólogo*, atiende los *viernes* en el "
            "Centro Médico Carampangue. Sin viajar a Concepción ni esperar "
            "meses por una hora.\n\n"
            "✅ Consulta cardiológica: *$40.000*\n"
            "✅ Evaluación completa y plan de tratamiento claro\n"
            "✅ Cupos limitados — solo viernes\n\n"
            "Toca *Agendar mi hora* y cuida tu corazón ❤️"
        ),
    },
    {
        "id_profesional": 61,
        "profesional": "Dr. Tirso Rejón",
        "especialidad": "Ginecología",
        "area": "Médica", "emoji": "🌸",
        "template_name": "prof_gine_rejon_v1",
        "precio_line": "Particular $30.000",
        "angle": "Control ginecológico cerca de casa, con privacidad y sin listas de espera.",
        "header": "Ginecólogo en Carampangue",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Hace cuánto no te haces tu control ginecológico? La prevención "
            "es la mejor forma de cuidarte.\n\n"
            "El *Dr. Tirso Rejón, ginecólogo*, atiende en el Centro Médico "
            "Carampangue: cerca de tu casa, con privacidad y sin listas de "
            "espera eternas.\n\n"
            "✅ Consulta ginecológica: *$30.000*\n"
            "✅ Control preventivo, ecografía y tratamiento\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y date prioridad 🌸"
        ),
    },
    {
        "id_profesional": 65,
        "profesional": "Dr. Nicolás Quijano",
        "especialidad": "Gastroenterología",
        "area": "Médica", "emoji": "🔥",
        "template_name": "prof_gastro_quijano_v1",
        "precio_line": "Particular $35.000",
        "angle": "Síntomas digestivos crónicos que la gente normaliza: reflujo, hinchazón, colon.",
        "header": "¿Acidez o dolor de estómago frecuente?",
        "body": (
            "Hola {{1}} 👋\n\n"
            "Reflujo, acidez, hinchazón o colon irritable *no son normales* "
            "aunque te hayas acostumbrado a vivir con ellos.\n\n"
            "El *Dr. Nicolás Quijano, gastroenterólogo*, atiende en el Centro "
            "Médico Carampangue y puede ayudarte a encontrar la causa.\n\n"
            "✅ Consulta de especialista: *$35.000*\n"
            "✅ Sin viajar a Concepción\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y vuelve a comer tranquilo 🍽️"
        ),
    },
    {
        "id_profesional": 23,
        "profesional": "Dr. Manuel Borrego",
        "especialidad": "Otorrinolaringología",
        "area": "Médica", "emoji": "👂",
        "template_name": "prof_orl_borrego_v1",
        "precio_line": "Particular $35.000",
        "angle": "Oídos tapados, ronquidos, mareos. Complementa con Fonoaudiología (cross-sell).",
        "nota": "⚠️ ORL sin fecha de agenda por ahora — usar esta plantilla cuando el Dr. Borrego retome atención, o adaptarla a lista de espera.",
        "header": "Otorrino en Carampangue",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Oídos tapados, zumbidos, mareos o ronquidos que no te dejan "
            "dormir bien? Eso lo ve el especialista correcto.\n\n"
            "El *Dr. Manuel Borrego, otorrinolaringólogo*, atiende en el "
            "Centro Médico Carampangue.\n\n"
            "✅ Consulta de especialista: *$35.000*\n"
            "✅ Audiometría disponible en el mismo centro\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y escucha la diferencia 👂"
        ),
    },
    {
        "id_profesional": 68,
        "profesional": "David Pardo",
        "especialidad": "Ecografía",
        "area": "Médica", "emoji": "🖥️",
        "template_name": "prof_eco_pardo_v1",
        "precio_line": "Particular $40.000",
        "angle": "Tienen la orden de ecografía guardada hace semanas — cero fricción para tomarla ya.",
        "header": "Tu ecografía, esta semana y cerca",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Tienes una *orden de ecografía* guardada hace semanas? No la "
            "dejes vencer: mientras antes el resultado, antes el tratamiento.\n\n"
            "En el Centro Médico Carampangue tomamos ecografías *abdominal, "
            "renal, mamaria, tiroides, partes blandas* y más, con nuestro "
            "ecografista David Pardo.\n\n"
            "✅ Valor: *$40.000*\n"
            "✅ Horas esta misma semana, sin viajar\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y sácate la duda 🖥️"
        ),
    },
    {
        "id_profesional": 78,
        "profesional": "Dra. Cecilia Unibazo",
        "especialidad": "Psiquiatría",
        "area": "Médica", "emoji": "🧠",
        "template_name": "prof_psiq_unibazo_v1",
        "precio_line": "Particular $60.000 · Teleconsulta",
        "angle": "Psiquiatra por teleconsulta: privacidad total y cero viaje. Atiende martes y jueves.",
        "header": "Psiquiatra por teleconsulta",
        "body": (
            "Hola {{1}} 👋\n\n"
            "Pedir ayuda es de valientes. Si la ansiedad, el ánimo bajo o el "
            "insomnio te están ganando, hay tratamiento.\n\n"
            "La *Dra. Cecilia Unibazo, psiquiatra*, atiende por "
            "*teleconsulta* los jueves a través del Centro Médico "
            "Carampangue: desde tu casa, con total privacidad.\n\n"
            "✅ Consulta: *$60.000*\n"
            "✅ Sin viajar, desde tu celular\n"
            "✅ Receta y plan de tratamiento si lo necesitas\n\n"
            "Toca *Agendar mi hora* y da el primer paso 🧠"
        ),
    },
    # ── Área Dental ──────────────────────────────────────────────────────────
    {
        "id_profesional": 55,
        "profesional": "Dra. Javiera Burgos",
        "especialidad": "Odontología General",
        "area": "Dental", "emoji": "🦷",
        "template_name": "prof_odonto_burgos_v1",
        "precio_line": "Evaluación $15.000",
        "angle": "Evaluación dental completa barata como puerta de entrada al plan de tratamiento.",
        "header": "Evaluación dental completa a $15.000",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Hace más de un año que no ves a un dentista? Lo que hoy es una "
            "carie chica, mañana es un tratamiento caro.\n\n"
            "La *Dra. Javiera Burgos* te hace una *evaluación dental completa "
            "por solo $15.000* en el Centro Médico Carampangue.\n\n"
            "✅ Diagnóstico claro y plan de tratamiento con precios\n"
            "✅ Sin compromiso: tú decides cómo seguir\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y sonríe tranquilo 🦷"
        ),
    },
    {
        "id_profesional": 72,
        "profesional": "Dr. Carlos Jiménez",
        "especialidad": "Odontología General",
        "area": "Dental", "emoji": "🦷",
        "template_name": "prof_odonto_jimenez_v1",
        "precio_line": "Evaluación $15.000",
        "angle": "Dolor/urgencia dental: resolver hoy, no aguantar con analgésicos.",
        "header": "¿Dolor de muela? No lo aguantes más",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Dolor de muela, una tapadura que se cayó o un diente que "
            "molesta al comer? Aguantar con pastillas solo lo empeora.\n\n"
            "El *Dr. Carlos Jiménez* atiende en el Centro Médico "
            "Carampangue y puede verte rápido.\n\n"
            "✅ Evaluación: *$15.000*\n"
            "✅ Te dice exactamente qué tienes y cuánto cuesta arreglarlo\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y olvídate del dolor 🦷"
        ),
    },
    {
        "id_profesional": 66,
        "profesional": "Dra. Daniela Castillo",
        "especialidad": "Ortodoncia",
        "area": "Dental", "emoji": "😁",
        "template_name": "prof_orto_castillo_v1",
        "precio_line": "Evaluación $30.000 · Instalación $120.000",
        "angle": "Frenillos en Carampangue: precio transparente vs viajar a Concepción cada mes.",
        "header": "Frenillos sin viajar a Concepción",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Tú o tu hijo necesitan *frenillos*? Ya no tienes que viajar a "
            "Concepción todos los meses para los controles.\n\n"
            "La *Dra. Daniela Castillo, ortodoncista*, atiende en el Centro "
            "Médico Carampangue con precios claros:\n\n"
            "✅ Evaluación: *$30.000*\n"
            "✅ Instalación boca completa: *$120.000*\n"
            "✅ Controles mensuales aquí mismo, cerca de casa\n\n"
            "Toca *Agendar mi hora* y empieza el cambio 😁"
        ),
    },
    {
        "id_profesional": 75,
        "profesional": "Dr. Fernando Fredes",
        "especialidad": "Endodoncia",
        "area": "Dental", "emoji": "🛟",
        "template_name": "prof_endo_fredes_v1",
        "precio_line": "Desde $110.000",
        "angle": "Salvar el diente vs sacarlo: la endodoncia es más barata que el implante después.",
        "header": "Salva tu diente antes de perderlo",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Te dijeron que hay que *sacar* un diente? Antes de eso, hay "
            "una alternativa: el tratamiento de conducto puede *salvarlo*.\n\n"
            "El *Dr. Fernando Fredes, endodoncista*, atiende en el Centro "
            "Médico Carampangue.\n\n"
            "✅ Tratamiento de conducto desde *$110.000*\n"
            "✅ Mucho más barato que perder el diente y poner un implante\n"
            "✅ Especialista, no consulta general\n\n"
            "Toca *Agendar mi hora* y salva tu diente 🦷"
        ),
    },
    {
        "id_profesional": 69,
        "profesional": "Dra. Aurora Valdés",
        "especialidad": "Implantología",
        "area": "Dental", "emoji": "⚙️",
        "template_name": "prof_impla_valdes_v1",
        "precio_line": "Tratamiento desde $650.000",
        "angle": "Recuperar dientes fijos (no placa): autoestima + comer de todo de nuevo.",
        "header": "Vuelve a sonreír con dientes fijos",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Te falta uno o más dientes y la placa te incomoda? Los "
            "*implantes dentales* son dientes fijos: comes de todo y "
            "sonríes sin pensarlo.\n\n"
            "La *Dra. Aurora Valdés, implantóloga*, atiende en el Centro "
            "Médico Carampangue.\n\n"
            "✅ Tratamiento desde *$650.000*\n"
            "✅ Evaluación con la especialista para tu plan y presupuesto\n"
            "✅ Todo el tratamiento aquí, sin viajar\n\n"
            "Toca *Agendar mi hora* y recupera tu sonrisa 😁"
        ),
    },
    {
        "id_profesional": 76,
        "profesional": "Dra. Valentina Fuentealba",
        "especialidad": "Estética Facial",
        "area": "Dental", "emoji": "✨",
        "template_name": "prof_estetica_fuentealba_v1",
        "precio_line": "Evaluación $15.000",
        "angle": "Estética facial profesional en Arauco — evaluación accesible, plan personalizado.",
        "header": "Estética facial profesional, aquí mismo",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Pensabas que para un tratamiento de *estética facial* había que "
            "ir a Concepción? Ya no.\n\n"
            "La *Dra. Valentina Fuentealba* atiende en el Centro Médico "
            "Carampangue: armonización, líneas de expresión, perfilado y "
            "más, con respaldo médico real.\n\n"
            "✅ Evaluación: *$15.000* — sales con tu plan y presupuesto\n"
            "✅ Productos certificados y atención profesional\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y date ese gusto ✨"
        ),
    },
    # ── Salud y Bienestar ────────────────────────────────────────────────────
    {
        "id_profesional": 77,
        "profesional": "Luis Armijo",
        "especialidad": "Kinesiología",
        "area": "Salud y Bienestar", "emoji": "💪",
        "template_name": "prof_kine_armijo_v1",
        "precio_line": "Fonasa $7.830",
        "angle": "Dolor que no pasa solo: sesión por Fonasa más barata que seguir comprando pomadas.",
        "header": "Ese dolor no se va a ir solo",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Dolor de espalda, hombro o rodilla que lleva semanas? Las "
            "pomadas tapan el síntoma; la *kinesiología* trata la causa.\n\n"
            "El kinesiólogo *Luis Armijo* atiende en el Centro Médico "
            "Carampangue.\n\n"
            "✅ Con *Fonasa la sesión cuesta solo $7.830*\n"
            "✅ Sesiones de 40 minutos, tratamiento de verdad\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y muévete sin dolor 💪"
        ),
    },
    {
        "id_profesional": 21,
        "profesional": "Leonardo Etcheverry",
        "especialidad": "Kinesiología",
        "area": "Salud y Bienestar", "emoji": "🏃",
        "template_name": "prof_kine_etcheverry_v1",
        "precio_line": "Fonasa $7.830",
        "angle": "Rehabilitación post-lesión/operación: completar las sesiones = recuperarse bien.",
        "header": "Recupérate bien, no a medias",
        "body": (
            "Hola {{1}} 👋\n\n"
            "Después de una lesión, esguince u operación, dejar la "
            "rehabilitación a medias es la receta para recaer.\n\n"
            "El kinesiólogo *Leonardo Etcheverry* te acompaña sesión a "
            "sesión en el Centro Médico Carampangue.\n\n"
            "✅ Con *Fonasa la sesión cuesta solo $7.830*\n"
            "✅ Plan de recuperación a tu medida\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y termina tu recuperación 🏃"
        ),
    },
    {
        "id_profesional": 59,
        "profesional": "Paola Acosta",
        "especialidad": "Masoterapia",
        "area": "Salud y Bienestar", "emoji": "🧘",
        "template_name": "prof_maso_acosta_v1",
        "precio_line": "Desde $17.990",
        "angle": "Contracturas y estrés: regalo accesible para uno mismo (o para regalar).",
        "header": "Tu espalda te está pidiendo esto",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Hombros tensos, cuello agarrotado, estrés acumulado? Un masaje "
            "descontracturante no es un lujo: es mantención.\n\n"
            "*Paola Acosta, masoterapeuta*, atiende en el Centro Médico "
            "Carampangue.\n\n"
            "✅ Sesiones desde *$17.990*\n"
            "✅ Elige 20 o 40 minutos según lo que necesites\n"
            "✅ Ideal también para regalar 🎁\n\n"
            "Toca *Agendar mi hora* y suelta esa tensión 🧘"
        ),
    },
    {
        "id_profesional": 52,
        "profesional": "Gisela Pinto",
        "especialidad": "Nutrición",
        "area": "Salud y Bienestar", "emoji": "🥗",
        "template_name": "prof_nutri_pinto_v1",
        "precio_line": "Fonasa $4.770",
        "angle": "Precio ancla imbatible: la consulta Fonasa cuesta menos que una hamburguesa.",
        "header": "Nutricionista por menos que una hamburguesa",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Quieres bajar de peso, controlar la diabetes o simplemente "
            "comer mejor? Con *Fonasa, la consulta de nutrición cuesta "
            "$4.770* — menos que una hamburguesa 🍔\n\n"
            "*Gisela Pinto, nutricionista*, atiende en el Centro Médico "
            "Carampangue con planes reales, hechos para ti.\n\n"
            "✅ Sesión completa de 60 minutos\n"
            "✅ Plan de alimentación a tu medida (sin dietas imposibles)\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y parte hoy 🥗"
        ),
    },
    {
        "id_profesional": 74,
        "profesional": "Jorge Montalba",
        "especialidad": "Psicología Adulto e Infantil",
        "area": "Salud y Bienestar", "emoji": "💬",
        "template_name": "prof_psico_montalba_v1",
        "precio_line": "Fonasa $14.420",
        "angle": "Único que atiende infantil: papás preocupados por sus hijos + adultos.",
        "header": "Psicólogo para ti o para tus hijos",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Tu hijo está más irritable, bajó las notas o le cuesta "
            "relacionarse? ¿O eres tú quien necesita un espacio para "
            "conversar? No tienes que resolverlo solo.\n\n"
            "*Jorge Montalba, psicólogo*, atiende a *niños y adultos* en el "
            "Centro Médico Carampangue.\n\n"
            "✅ Con *Fonasa la sesión cuesta $14.420*\n"
            "✅ Sesiones de 45 minutos, espacio seguro y confidencial\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* — el primer paso es el que cuenta 💬"
        ),
    },
    {
        "id_profesional": 49,
        "profesional": "Juan Pablo Rodríguez",
        "especialidad": "Psicología Adulto",
        "area": "Salud y Bienestar", "emoji": "🌱",
        "template_name": "prof_psico_rodriguez_v1",
        "precio_line": "Fonasa $14.420",
        "angle": "Adultos con ansiedad/estrés laboral que postergan su salud mental.",
        "header": "Dormir mal y andar tenso no es normal",
        "body": (
            "Hola {{1}} 👋\n\n"
            "Ansiedad, estrés del trabajo, pena que no pasa, dormir mal... "
            "Acostumbrarse a sentirse así *no* es la solución.\n\n"
            "*Juan Pablo Rodríguez, psicólogo*, atiende adultos en el Centro "
            "Médico Carampangue.\n\n"
            "✅ Con *Fonasa la sesión cuesta $14.420*\n"
            "✅ Sesiones de 45 minutos, 100% confidenciales\n"
            "✅ Agendas por este WhatsApp en 1 minuto\n\n"
            "Toca *Agendar mi hora* y date ese espacio 🌱"
        ),
    },
    {
        "id_profesional": 70,
        "profesional": "Juana Arratia",
        "especialidad": "Fonoaudiología",
        "area": "Salud y Bienestar", "emoji": "🔊",
        "template_name": "prof_fono_arratia_v1",
        "precio_line": "Evaluación $30.000 · Audiometría $25.000",
        "angle": "Dos públicos en uno: niños que pronuncian mal + adultos que oyen poco.",
        "header": "¿Tu hijo pronuncia mal? ¿Te cuesta oír?",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Tu hijo habla poco para su edad o pronuncia mal algunas "
            "letras? ¿O en casa hay alguien que pide repetir todo porque "
            "*oye menos*? La fonoaudióloga ve ambas cosas.\n\n"
            "*Juana Arratia* atiende en el Centro Médico Carampangue:\n\n"
            "✅ Evaluación infantil o adulto: *$30.000*\n"
            "✅ Audiometría (examen de audición): *$25.000*\n"
            "✅ Terapias de lenguaje y audición: $25.000 por sesión\n\n"
            "Toca *Agendar mi hora* — mientras antes, mejor 🔊"
        ),
    },
    {
        "id_profesional": 67,
        "profesional": "Sarai Gómez",
        "especialidad": "Matrona",
        "area": "Salud y Bienestar", "emoji": "🤰",
        "template_name": "prof_matrona_gomez_v1",
        "precio_line": "Fonasa $16.000",
        "angle": "Control preventivo femenino (PAP, anticoncepción, embarazo) sin esperas del CESFAM.",
        "header": "Matrona con hora rápida en Carampangue",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿PAP pendiente, dudas de anticoncepción o control de embarazo "
            "y en el consultorio te dan hora para dentro de un mes?\n\n"
            "La matrona *Sarai Gómez* atiende en el Centro Médico "
            "Carampangue, con horas esta misma semana.\n\n"
            "✅ Con *Fonasa la consulta cuesta $16.000*\n"
            "✅ PAP, anticoncepción, control prenatal y ginecológico\n"
            "✅ Atención cercana y sin apuro\n\n"
            "Toca *Agendar mi hora* y ponte al día 🤰"
        ),
    },
    {
        "id_profesional": 56,
        "profesional": "Andrea Guevara",
        "especialidad": "Podología",
        "area": "Salud y Bienestar", "emoji": "🦶",
        "template_name": "prof_podo_guevara_v1",
        "precio_line": "Desde $20.000",
        "angle": "Uña encarnada / pie diabético: dolor concreto + prevención en diabéticos.",
        "header": "Tus pies también necesitan un profesional",
        "body": (
            "Hola {{1}} 👋\n\n"
            "¿Uña encarnada que duele al caminar, callos o durezas? ¿O "
            "alguien con *diabetes* en casa que necesita cuidado preventivo "
            "de sus pies?\n\n"
            "La podóloga *Andrea Guevara* atiende en el Centro Médico "
            "Carampangue.\n\n"
            "✅ Atención desde *$20.000*\n"
            "✅ Sesión completa de 60 minutos\n"
            "✅ Cuidado especializado en pie diabético\n\n"
            "Toca *Agendar mi hora* y camina sin dolor 🦶"
        ),
    },
]


def get_biblioteca() -> dict:
    """Biblioteca agrupada por área + resumen, lista para el dashboard."""
    grupos: dict[str, list[dict]] = {}
    for t in BIBLIOTECA:
        item = dict(t)
        item.setdefault("footer", FOOTER)
        item.setdefault("buttons", list(_BTN_DEFAULT))
        item["meta_payload"] = meta_payload(item)
        grupos.setdefault(t["area"], []).append(item)
    orden = ["Médica", "Dental", "Salud y Bienestar"]
    return {
        "areas": [{"area": a, "plantillas": grupos[a]} for a in orden if a in grupos],
        "resumen": {"total": len(BIBLIOTECA),
                    **{a: len(v) for a, v in grupos.items()}},
    }


def meta_payload(t: dict) -> dict:
    """Payload listo para POST /{WABA_ID}/message_templates (crear template).

    El example del BODY es obligatorio cuando hay variables ({{1}} = nombre).
    """
    return {
        "name": t["template_name"],
        "category": "MARKETING",
        "language": "es",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": t["header"]},
            {"type": "BODY", "text": t["body"],
             "example": {"body_text": [["María"]]}},
            {"type": "FOOTER", "text": t.get("footer", FOOTER)},
            {"type": "BUTTONS", "buttons": [
                {"type": "QUICK_REPLY", "text": b}
                for b in t.get("buttons", _BTN_DEFAULT)
            ]},
        ],
    }
