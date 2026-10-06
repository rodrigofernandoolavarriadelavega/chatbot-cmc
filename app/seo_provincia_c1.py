"""Páginas localidad x especialidad, parte 1: otorrinolaringología y ecografía.

Cada página tiene texto propio para su localidad. Precios y profesionales salen de
seo_provincia.OFERTA (código del bot). No se nombran líneas de bus.
"""
from seo_provincia import pag, tabla_oferta, bloque_viaje

SPEC_ORL = "Otolaryngologic"
SPEC_ECO = "RadiologyImaging"

PAGINAS_1 = {}

# ───────────────────────────── ORL ─────────────────────────────────────────

PAGINAS_1["otorrinolaringologia-curanilahue"] = pag(
    comuna="curanilahue", base="otorrinolaringologia", esp="Otorrinolaringología", spec=SPEC_ORL,
    title="Otorrinolaringólogo en Curanilahue · Dr. Borrego | CMC",
    meta="Otorrinolaringólogo para pacientes de Curanilahue: Dr. Manuel Borrego, consulta $35.000, lunes a miércoles en Carampangue, a 30 km por la Ruta 160.",
    h1="Otorrinolaringólogo para pacientes de <em>Curanilahue</em>",
    lead="El Dr. Manuel Borrego atiende oído, nariz y garganta en Carampangue, a 30 km de Curanilahue por la Ruta 160. Adultos y niños. Consulta particular de $35.000.",
    secciones=[
        ("Qué se consulta con el otorrinolaringólogo",
         "<p>El otorrinolaringólogo evalúa oído, nariz y garganta. Con el Dr. Borrego se consulta por otitis repetidas, sinusitis o congestión nasal que no cede, amigdalitis, ronquidos, mareos, disminución de la audición o un tapón de cera que no sale. En los niños, las otitis y las amígdalas son los motivos más habituales.</p>"
         "<p>Si corresponde, el lavado de oídos se realiza durante la misma atención, con un valor adicional.</p>"
         + tabla_oferta(["orl_consulta", "orl_control", "orl_lavado", "audiometria"])),
        ("Cómo llegar desde Curanilahue",
         bloque_viaje("curanilahue")
         + "<p>Desde Curanilahue se toma la Ruta 160 en dirección a Carampangue. Como el Dr. Borrego atiende por la tarde, de lunes a miércoles, el viaje completo cabe en una sola tarde: unos 30 minutos de ida, la consulta y 30 minutos de regreso. Conviene salir con 20 minutos de margen y pedir una hora entre las 16:00 y las 19:00.</p>"),
        ("Exámenes que conviene resolver en el mismo viaje",
         "<p>Cuando hay molestias auditivas, el otorrinolaringólogo suele pedir una audiometría. En el centro se realiza con la fonoaudióloga Juana Arratia ($25.000), y la impedanciometría, que mide el movimiento del tímpano, cuesta $20.000. Al agendar, consulte si es posible coordinarlas el mismo día que la consulta, para no viajar dos veces.</p>"),
    ],
    faqs=[
        ("¿La consulta se puede pagar con Fonasa?", "No. Otorrinolaringología es solo particular, $35.000. Se paga en efectivo o por transferencia."),
        ("¿Necesito derivación del CESFAM u orden médica?", "No es obligatoria para pedir la hora. Si tiene una audiometría u otros exámenes recientes, llévelos a la consulta."),
        ("¿Qué día conviene viajar desde Curanilahue?", "El Dr. Borrego atiende de lunes a miércoles entre las 16:00 y las 20:00. Confirme la fecha disponible por WhatsApp antes de salir."),
        ("¿Se hacen cirugías en el centro?", "No. En el centro se realiza la evaluación y los procedimientos de consulta. Si se necesita una cirugía, el médico indica a qué centro derivar."),
    ],
    enlaces=[
        ("/comuna/curanilahue", "Atención en el centro para pacientes de Curanilahue", "Todas las especialidades y cómo llegar"),
        ("/blog/otorrinolaringologia", "Otorrinolaringología: cuándo consultar", "Guía general de la especialidad"),
        ("/blog/fonoaudiologia", "Fonoaudiología y audiometría", "Exámenes auditivos y terapia"),
        ("/blog/rinoplastia-funcional-tabique", "Tabique desviado: cuándo se opera", "Para quienes respiran mal por la nariz"),
    ],
    wa_texto="Hola, soy de Curanilahue y quiero agendar con el otorrinolaringólogo.",
)

PAGINAS_1["otorrinolaringologia-arauco"] = pag(
    comuna="arauco", base="otorrinolaringologia", esp="Otorrinolaringología", spec=SPEC_ORL,
    title="Otorrino en Arauco · Dr. Borrego, a 8 km | Centro Médico Carampangue",
    meta="Otorrino para pacientes de Arauco: Dr. Manuel Borrego, consulta $35.000, lunes a miércoles por la tarde. El centro queda a 8 km, en Carampangue.",
    h1="Otorrino para pacientes de <em>Arauco</em>",
    lead="Desde la ciudad de Arauco, el Centro Médico Carampangue queda a 8 km, unos 11 minutos en auto. El Dr. Manuel Borrego atiende de lunes a miércoles por la tarde.",
    secciones=[
        ("Una consulta cerca, sin salir de la comuna",
         "<p>Carampangue pertenece a la comuna de Arauco, de modo que quien vive en la ciudad de Arauco, en Ramadilla o en las localidades cercanas no necesita salir de la comuna para ver a un otorrinolaringólogo. Más de doscientos pacientes de la comuna de Arauco consultaron esta especialidad en el centro durante el último año.</p>"
         "<p>El Dr. Borrego atiende a adultos y niños. Los motivos habituales son otitis, sinusitis, amígdalas, ronquidos, vértigo y tapones de cera.</p>"
         + tabla_oferta(["orl_consulta", "orl_control", "orl_lavado"])),
        ("Cómo llegar desde Arauco",
         bloque_viaje("arauco")
         + "<p>El camino que une Arauco con Carampangue (Ruta P-20) es corto y directo. Para una consulta de tarde, salir de Arauco a las 15:30 permite llegar con tiempo para las horas de las 16:00. El centro está en Monsalve 102, esquina República, y hay estacionamiento libre en la calle.</p>"),
        ("Un dato práctico: el control cuesta menos",
         "<p>Después de la primera consulta, el control de seguimiento tiene un valor menor ($8.000). Si el tratamiento requiere revisar la evolución en pocas semanas, conviene pedir ese control al terminar la consulta, mientras se coordina la agenda del especialista.</p>"),
    ],
    faqs=[
        ("¿El otorrino atiende todos los días?", "No. Atiende de lunes a miércoles, entre las 16:00 y las 20:00. El resto de la semana no hay consulta de esta especialidad."),
        ("¿Se puede ir sin hora previa?", "No. La atención es con hora agendada. Se pide por WhatsApp y se confirma el mismo día."),
        ("¿Atienden a niños con otitis?", "Sí. El Dr. Borrego atiende niños desde aproximadamente los 4 años. Para menores, ayuda llevar anotadas las veces que ha tenido infecciones de oído en el último año."),
        ("¿Cuánto cuesta el lavado de oídos?", "$10.000, adicional a la consulta de $35.000."),
    ],
    enlaces=[
        ("/comuna/arauco", "Centro médico para pacientes de Arauco", "Especialidades y cómo llegar"),
        ("/blog/otorrinolaringologia", "Otorrinolaringología: guía de la especialidad", "Cuándo consultar y qué esperar"),
        ("/blog/otorrinolaringologia-curanilahue", "Otorrino desde Curanilahue", "Si usted viene desde más al sur"),
    ],
    wa_texto="Hola, soy de Arauco y quiero agendar con el otorrinolaringólogo.",
)

PAGINAS_1["otorrinolaringologia-canete"] = pag(
    comuna="canete", base="otorrinolaringologia", esp="Otorrinolaringología", spec=SPEC_ORL,
    title="Otorrinolaringólogo desde Cañete · Dr. Borrego | CMC",
    meta="Otorrinolaringólogo para pacientes de Cañete: Dr. Manuel Borrego, $35.000, lunes a miércoles por la tarde en Carampangue, a 72 km. Cómo planificar el viaje.",
    h1="Otorrinolaringólogo para pacientes de <em>Cañete</em>",
    lead="Desde Cañete hasta el Centro Médico Carampangue hay unos 72 km y cerca de 1 hora 20 minutos. Por eso conviene confirmar la fecha con el Dr. Borrego antes de salir.",
    secciones=[
        ("Cómo planificar un viaje de 72 km",
         "<p>El Dr. Manuel Borrego atiende solo de lunes a miércoles, por la tarde. Desde Cañete, la forma más práctica es pedir una hora entre las 17:00 y las 18:30: permite salir después del mediodía, llegar sin apuro y volver antes de que sea muy tarde.</p>"
         "<p>Antes de salir, escriba por WhatsApp y confirme tres cosas: la fecha, la hora y el valor. La consulta cuesta $35.000, es particular y se paga el mismo día en efectivo o por transferencia.</p>"
         + tabla_oferta(["orl_consulta", "orl_control", "orl_lavado"])),
        ("Ruta y tiempos desde Cañete",
         bloque_viaje("canete")
         + "<p>El trayecto se hace por la Ruta P-60 y luego por la Ruta 160, en dirección a Carampangue. Es un camino de campo y bosque, con tramos donde la velocidad baja; en invierno, con lluvia o neblina, conviene sumar tiempo. Si viaja en auto, el centro está en Monsalve 102, esquina República, con estacionamiento libre en la calle.</p>"),
        ("Cuándo vale la pena el viaje y cuándo no",
         "<p>Para controles simples, una otitis de evolución habitual o una molestia que lleva semanas, consultar con anticipación evita viajes innecesarios. Si hay dolor intenso de oído con fiebre alta, pérdida brusca de la audición, sangrado de nariz que no cede o dificultad para respirar, no espere una hora agendada: acuda al servicio de urgencia más cercano.</p>"),
    ],
    faqs=[
        ("¿Se puede pagar con Fonasa?", "No. La consulta de otorrinolaringología es solo particular, $35.000."),
        ("¿Conviene combinar la consulta con otra atención en el mismo viaje?", "Sí. Al agendar, consulte si hay disponibilidad de audiometría, de gastroenterología o de ecografía el mismo día. Las fechas de cada especialista se coordinan por WhatsApp."),
        ("¿Hay atención en Cañete?", "No. El Centro Médico Carampangue tiene una sola sede, en Monsalve 102, esquina República, Carampangue. No tenemos consultas ni sucursales en otras comunas."),
        ("¿El hospital de Cañete puede derivar para esta consulta?", "No hace falta derivación para pedir la hora. Si el Hospital Intercultural Kallvu Llanka le entregó una orden o exámenes, llévelos."),
    ],
    enlaces=[
        ("/comuna/canete", "Centro médico para pacientes de Cañete", "Especialidades, rutas y cómo organizar el viaje"),
        ("/blog/otorrinolaringologia", "Otorrinolaringología: guía de la especialidad", "Síntomas y procedimientos"),
        ("/blog/ecografia-canete", "Ecografía desde Cañete", "Para aprovechar el viaje"),
        ("/blog/gastroenterologia-canete", "Gastroenterología desde Cañete", "Otra consulta que se coordina por fechas"),
    ],
    wa_texto="Hola, soy de Cañete y quiero agendar con el otorrinolaringólogo.",
)

PAGINAS_1["otorrinolaringologia-los-alamos"] = pag(
    comuna="los-alamos", base="otorrinolaringologia", esp="Otorrinolaringología", spec=SPEC_ORL,
    title="Otorrinolaringólogo desde Los Álamos · Dr. Borrego | CMC",
    meta="Otorrinolaringólogo para pacientes de Los Álamos: Dr. Manuel Borrego, $35.000, lunes a miércoles por la tarde. El centro queda a 53 km, por la Ruta 160.",
    h1="Otorrinolaringólogo para pacientes de <em>Los Álamos</em>",
    lead="Desde Los Álamos al Centro Médico Carampangue hay unos 53 km por la Ruta 160, cerca de una hora. El Dr. Manuel Borrego atiende oído, nariz y garganta de lunes a miércoles por la tarde.",
    secciones=[
        ("Atención de oído, nariz y garganta",
         "<p>El Dr. Borrego atiende a adultos y niños. Los motivos de consulta habituales son sinusitis, otitis que se repiten, ronquidos, vértigo o una disminución de la audición. Para evaluar la audición, el especialista puede indicar una audiometría, que también se realiza en el centro.</p>"
         + tabla_oferta(["orl_consulta", "orl_control", "audiometria"])),
        ("El camino desde Los Álamos",
         bloque_viaje("los-alamos")
         + "<p>La Ruta 160 conecta Los Álamos con Carampangue pasando por Curanilahue, por lo que el viaje es directo y no requiere cambiar de camino. Una hora a las 17:00 permite salir con luz y estar de vuelta antes de la noche, sobre todo en invierno, cuando oscurece temprano.</p>"
         "<p>Si viaja con un niño, calcule una parada breve a mitad de camino.</p>"),
    ],
    faqs=[
        ("¿Cuánto cuesta y cómo se paga?", "La consulta cuesta $35.000, solo particular. Se paga en efectivo o por transferencia. El control de seguimiento cuesta $8.000."),
        ("¿Qué días atiende el otorrinolaringólogo?", "De lunes a miércoles por la tarde. La fecha exacta se confirma por WhatsApp, así que conviene preguntar antes de viajar."),
        ("¿Puedo pedir la hora desde Los Álamos sin ir al centro?", "Sí. La hora se pide por WhatsApp. Solo se viaja el día de la atención."),
    ],
    enlaces=[
        ("/comuna/los-alamos", "Centro médico para pacientes de Los Álamos", "Especialidades y cómo llegar"),
        ("/blog/otorrinolaringologia", "Otorrinolaringología: guía de la especialidad", "Síntomas y procedimientos"),
        ("/blog/odontologia-general-los-alamos", "Atención dental desde Los Álamos", "Controles y tratamientos dentales"),
    ],
    wa_texto="Hola, soy de Los Álamos y quiero agendar con el otorrinolaringólogo.",
)

PAGINAS_1["otorrinolaringologia-lebu"] = pag(
    comuna="lebu", base="otorrinolaringologia", esp="Otorrinolaringología", spec=SPEC_ORL,
    title="Otorrinolaringólogo desde Lebu · Dr. Borrego | CMC",
    meta="Otorrinolaringólogo para pacientes de Lebu: Dr. Manuel Borrego, $35.000, lunes a miércoles por la tarde en Carampangue, a 77 km por la Ruta 160.",
    h1="Otorrinolaringólogo para pacientes de <em>Lebu</em>",
    lead="Lebu está a unos 77 km del Centro Médico Carampangue, cerca de 1 hora 20 minutos por la Ruta 160. Es la especialidad con más consultas de pacientes de Lebu en el último año.",
    secciones=[
        ("Un viaje que conviene dejar bien agendado",
         "<p>El Dr. Manuel Borrego atiende de lunes a miércoles, entre las 16:00 y las 20:00. Para un paciente de Lebu, lo recomendable es pedir una hora de las 17:00 o 18:00, de modo de salir después del almuerzo y regresar sin apuro.</p>"
         "<p>Pida la hora por WhatsApp y confirme la fecha, el valor y la dirección: Monsalve 102, esquina República, Carampangue.</p>"
         + tabla_oferta(["orl_consulta", "orl_control", "orl_lavado"])),
        ("Cómo llegar desde Lebu",
         bloque_viaje("lebu")
         + "<p>Desde Lebu, el viaje se hace por la Ruta 160, que pasa por Los Álamos y Curanilahue antes de llegar a Carampangue. Es un recorrido largo: lleve agua, combustible suficiente si va en auto y calcule el regreso con luz de día cuando sea posible.</p>"),
        ("Qué llevar a la consulta",
         "<p>Lleve su cédula de identidad, los exámenes de audición o imágenes que tenga (audiometrías, tomografías de senos paranasales) y la lista de los medicamentos que usa. Si el motivo es un niño con otitis repetidas, el carné de control y el registro de las veces que ha tenido infecciones ayudan al especialista.</p>"),
    ],
    faqs=[
        ("¿Hay consulta de otorrinolaringología en Lebu a través del centro?", "No. El centro atiende solo en Carampangue. No tenemos sede ni consultas en Lebu."),
        ("¿Es necesario traer orden del médico?", "No es obligatoria. Si ya tiene una, llévela junto con los exámenes."),
        ("¿La consulta tiene bono Fonasa?", "No. Es particular, $35.000."),
        ("¿Puedo agendar para el mismo día otras atenciones?", "Muchas veces sí. Pregunte por WhatsApp si hay horas de audiometría o de otra especialidad ese mismo día."),
    ],
    enlaces=[
        ("/comuna/lebu", "Centro médico para pacientes de Lebu", "Especialidades y cómo llegar"),
        ("/blog/otorrinolaringologia", "Otorrinolaringología: guía de la especialidad", "Síntomas y procedimientos"),
        ("/blog/ecografia-lebu", "Ecografía desde Lebu", "Otra atención que se coordina por fechas"),
    ],
    wa_texto="Hola, soy de Lebu y quiero agendar con el otorrinolaringólogo.",
)

# ───────────────────────────── Ecografía ──────────────────────────────────

PAGINAS_1["ecografia-arauco"] = pag(
    comuna="arauco", base="ecografia", esp="Ecografía", spec=SPEC_ECO,
    title="Ecografía en Arauco · Precios 2026, a 8 km | Centro Médico Carampangue",
    meta="Ecografía para pacientes de Arauco: abdominal, tiroidea, partes blandas y más con David Pardo, $40.000. Queda a 8 km, en Carampangue. Fechas por WhatsApp.",
    h1="Ecografía para pacientes de <em>Arauco</em>",
    lead="David Pardo realiza ecografías abdominales, tiroideas, renales, de partes blandas y otras en Carampangue, a 8 km de Arauco. Valor general: $40.000, solo particular.",
    secciones=[
        ("Qué ecografías se hacen y cuánto cuestan",
         "<p>Las ecografías generales cuestan $40.000. Esto incluye abdominal, renal, tiroidea, de partes blandas, musculoesquelética (hombro, rodilla, codo), pelviana, testicular y mamaria. La ecografía doppler de arterias y venas cuesta $90.000. La ecografía ginecológica transvaginal la realiza el Dr. Tirso Rejón, en ginecología, y cuesta $35.000.</p>"
         "<p>La ecografía obstétrica no se realiza en el centro. Si necesita una, se le indicará otro lugar al agendar.</p>"
         + tabla_oferta(["eco_general", "eco_doppler", "eco_gineco"])),
        ("Cómo se coordina la fecha",
         "<p>La agenda de ecografía se organiza por fechas definidas por el especialista, que varían de un mes a otro. Por eso la hora se pide por WhatsApp: se le propone la primera fecha disponible y, si llega con orden médica, se revisa que el tipo de examen quede bien indicado. Si no existe cupo próximo, se puede dejar en lista de espera.</p>"
         "<p>Según el examen, se indica ayuno, vejiga llena o ninguna preparación. Esa indicación se informa al agendar; seguirla evita tener que repetir la hora.</p>"),
        ("Cómo llegar desde Arauco",
         bloque_viaje("arauco")
         + "<p>Desde la ciudad de Arauco son 8 km por el camino que la une con Carampangue (Ruta P-20), unos 11 minutos en auto. El centro está en Monsalve 102, esquina República. Para exámenes con vejiga llena, conviene calcular el viaje con holgura y evitar llegar con apuro.</p>"),
    ],
    faqs=[
        ("¿La ecografía se puede pagar con Fonasa?", "No. La ecografía es solo particular y no tiene bono Fonasa en ninguna modalidad."),
        ("¿Se necesita orden médica?", "Es recomendable llevarla, porque indica qué debe estudiarse. Si no la tiene, consulte por WhatsApp antes de agendar."),
        ("¿Se realizan ecografías de embarazo?", "No. La ecografía obstétrica no está disponible en el centro."),
        ("¿Cuándo se entrega el informe?", "Si desea revisar el resultado con un médico, consulte por WhatsApp por la hora de revisión de exámenes con medicina general, que no tiene costo."),
    ],
    enlaces=[
        ("/comuna/arauco", "Centro médico para pacientes de Arauco", "Especialidades y cómo llegar"),
        ("/blog/ecografia-precio-arauco", "Precios de ecografía en Arauco", "Tipos de examen y preparación"),
        ("/blog/gastroenterologia-arauco", "Gastroenterología para pacientes de Arauco", "Después de una ecografía abdominal"),
    ],
    wa_texto="Hola, soy de Arauco y quiero agendar una ecografía.",
)

PAGINAS_1["ecografia-curanilahue"] = pag(
    comuna="curanilahue", base="ecografia", esp="Ecografía", spec=SPEC_ECO,
    title="Ecografía desde Curanilahue · $40.000, a 30 km | CMC",
    meta="Ecografía para pacientes de Curanilahue: abdominal, tiroidea, partes blandas y más, $40.000. David Pardo atiende en Carampangue, a 30 km por la Ruta 160.",
    h1="Ecografía para pacientes de <em>Curanilahue</em>",
    lead="Desde Curanilahue, el Centro Médico Carampangue queda a 30 km por la Ruta 160. David Pardo realiza las ecografías generales; el valor es $40.000 y se coordina por fechas.",
    secciones=[
        ("Qué se puede hacer y a qué precio",
         "<p>La ecografía es un examen de imágenes que no usa radiación. En el centro se realizan estudios abdominales, renales, tiroideos, de partes blandas (bultos, ganglios, hernias), musculoesqueléticos, pélvicos y testiculares por $40.000. El doppler de arterias y venas cuesta $90.000.</p>"
         + tabla_oferta(["eco_general", "eco_doppler"])),
        ("Planifique el viaje según la fecha del especialista",
         "<p>La ecografía no se agenda todos los días: las fechas las define el especialista y cambian de un mes a otro. Para quien viaja desde Curanilahue, lo práctico es escribir primero, indicar el examen que le pidieron y esperar la propuesta de fecha antes de organizar el día. De ese modo no se viaja sin cupo confirmado.</p>"
         "<p>Si el examen exige ayuno, pida la hora más temprana disponible y calcule la salida con tiempo; si exige vejiga llena, siga la indicación de agua que se le entregue al agendar.</p>"),
        ("De Curanilahue a Carampangue",
         bloque_viaje("curanilahue")
         + "<p>Se toma la Ruta 160 hacia Carampangue; no hay desvíos. Con buen tiempo son unos 30 minutos en auto. Como el viaje es corto, es fácil agrupar la ecografía con una consulta de medicina general, ginecología u otorrinolaringología el mismo día, si las agendas coinciden.</p>"),
    ],
    faqs=[
        ("¿Cuánto cuesta una ecografía abdominal?", "$40.000. Es el mismo valor para la mayoría de las ecografías generales. No tiene bono Fonasa."),
        ("¿Qué pasa si no hay fecha disponible pronto?", "Se puede dejar a la persona en lista de espera y se le avisa por WhatsApp cuando se libera un cupo."),
        ("¿Se necesita ayuno?", "Depende del examen. La ecografía abdominal suele requerir ayuno; la tiroidea, por lo general, no. Las indicaciones exactas se entregan al agendar."),
        ("¿Se pueden hacer ecografías de embarazo?", "No. La ecografía obstétrica no está disponible en el centro."),
    ],
    enlaces=[
        ("/comuna/curanilahue", "Centro médico para pacientes de Curanilahue", "Especialidades y cómo llegar"),
        ("/blog/ecografia-precio-arauco", "Precios y tipos de ecografía", "Qué incluye cada examen"),
        ("/blog/otorrinolaringologia-curanilahue", "Otorrinolaringólogo desde Curanilahue", "Otra consulta frecuente de la zona"),
    ],
    wa_texto="Hola, soy de Curanilahue y quiero agendar una ecografía.",
)

PAGINAS_1["ecografia-canete"] = pag(
    comuna="canete", base="ecografia", esp="Ecografía", spec=SPEC_ECO,
    title="Ecografía en Cañete: dónde hacerla y cuánto cuesta | CMC",
    meta="Ecografía para pacientes de Cañete: abdominal, tiroidea, partes blandas y más, $40.000, solo particular. Se realiza en Carampangue, a 72 km. Fechas por WhatsApp.",
    h1="Ecografía para pacientes de <em>Cañete</em>",
    lead="Las ecografías generales cuestan $40.000 y se realizan en Carampangue, a unos 72 km de Cañete. Como la agenda se arma por fechas, se recomienda confirmar antes de viajar.",
    secciones=[
        ("Qué ofrece el centro",
         "<p>David Pardo realiza ecografías abdominal, renal, tiroidea, de partes blandas, musculoesquelética, pelviana, testicular y mamaria. Todas tienen el mismo valor, $40.000, y son solo particulares. El doppler cuesta $90.000.</p>"
         "<p>La ecografía obstétrica no se realiza en el centro.</p>"
         + tabla_oferta(["eco_general", "eco_doppler"])),
        ("Confirmar la fecha antes de salir de Cañete",
         "<p>Para una persona que viaja 72 km, lo más importante es no llegar sin cupo. La agenda de ecografía se organiza por fechas definidas por el especialista, que cambian cada mes. El procedimiento es sencillo: escriba por WhatsApp, indique el examen que le pidieron (o envíe una foto de la orden) y espere la fecha propuesta.</p>"
         "<p>Si el examen requiere ayuno o vejiga llena, se lo indicaremos con la confirmación. Tener esa preparación bien hecha evita volver otro día.</p>"),
        ("Cómo llegar desde Cañete",
         bloque_viaje("canete")
         + "<p>Desde Cañete se toma la Ruta P-60 y luego la Ruta 160 hacia Carampangue. Calcule alrededor de 1 hora 20 minutos en auto. El centro está en Monsalve 102, esquina República. Si el examen exige ayuno, considere que el viaje de ida y vuelta, más el examen, ocupa buena parte de la jornada.</p>"),
        ("Aproveche el viaje",
         "<p>Si ya va a viajar, pregunte si hay horas el mismo día con otros especialistas del centro. Una ecografía abdominal suele acompañarse de una consulta de gastroenterología, y una tiroidea de una consulta de medicina general. Las fechas de cada especialista se coordinan por WhatsApp.</p>"),
    ],
    faqs=[
        ("¿Es necesario tener orden médica?", "Es recomendable. La orden indica qué zona debe estudiarse. Puede enviarla por WhatsApp para que se revise antes de agendar."),
        ("¿Se pueden pagar con Fonasa?", "No. La ecografía es solo particular. Se paga en efectivo o por transferencia."),
        ("¿Tienen sede en Cañete?", "No. El centro tiene una sola sede, en Monsalve 102, esquina República, Carampangue."),
        ("¿Cuánto demora el examen?", "Cada hora de ecografía se programa en bloques de 15 minutos. Si necesita varios estudios, indíquelo al agendar para reservar más de un bloque."),
    ],
    enlaces=[
        ("/comuna/canete", "Centro médico para pacientes de Cañete", "Especialidades, rutas y cómo organizar el viaje"),
        ("/blog/ecografia-precio-arauco", "Precios y tipos de ecografía", "Preparación para cada examen"),
        ("/blog/gastroenterologia-canete", "Gastroenterología desde Cañete", "Para revisar una ecografía abdominal"),
        ("/blog/otorrinolaringologia-canete", "Otorrinolaringólogo desde Cañete", "Otra consulta que se coordina por fechas"),
    ],
    wa_texto="Hola, soy de Cañete y quiero agendar una ecografía.",
)

PAGINAS_1["ecografia-lebu"] = pag(
    comuna="lebu", base="ecografia", esp="Ecografía", spec=SPEC_ECO,
    title="Ecografía desde Lebu · $40.000, a 77 km | CMC",
    meta="Ecografía para pacientes de Lebu: abdominal, tiroidea, partes blandas y más, $40.000. Se realiza en Carampangue, a 77 km por la Ruta 160. Fecha por WhatsApp.",
    h1="Ecografía para pacientes de <em>Lebu</em>",
    lead="Desde Lebu se llega a Carampangue por la Ruta 160 en cerca de 1 hora 20 minutos. David Pardo realiza ecografías generales por $40.000, con fechas coordinadas por WhatsApp.",
    secciones=[
        ("Exámenes disponibles",
         "<p>Se realizan ecografías abdominales, renales, tiroideas, de partes blandas, musculoesqueléticas, pelvianas, testiculares y mamarias a $40.000, solo particular. El doppler cuesta $90.000. No se realizan ecografías obstétricas.</p>"
         + tabla_oferta(["eco_general", "eco_doppler"])),
        ("Cómo evitar un viaje en vano",
         "<p>La agenda de ecografía no es diaria. Las fechas las fija el especialista y varían de un mes a otro. Para un paciente de Lebu, el orden recomendado es: enviar por WhatsApp la orden o el examen que necesita, recibir la fecha propuesta, confirmar y recién ahí planificar el día.</p>"
         "<p>Si la indicación es de ayuno, pida la hora más temprana disponible, para volver a Lebu sin pasar demasiadas horas sin comer.</p>"),
        ("Ruta desde Lebu",
         bloque_viaje("lebu")
         + "<p>El camino es la Ruta 160, que pasa por Los Álamos y Curanilahue. Son unos 77 km. Como la ecografía no exige reposo posterior, el regreso se puede hacer de inmediato.</p>"),
    ],
    faqs=[
        ("¿Cuánto cuesta una ecografía?", "$40.000 las generales y $90.000 el doppler. No hay bono Fonasa."),
        ("¿Puedo mandar la orden por WhatsApp?", "Sí. Una foto legible de la orden permite revisar el tipo de examen antes de dar la hora."),
        ("¿Hay lista de espera si no hay cupo cercano?", "Sí. Se puede dejar a la persona en lista de espera y se le avisa por WhatsApp cuando haya un cupo."),
    ],
    enlaces=[
        ("/comuna/lebu", "Centro médico para pacientes de Lebu", "Especialidades y cómo llegar"),
        ("/blog/ecografia-precio-arauco", "Precios y tipos de ecografía", "Preparación para cada examen"),
        ("/blog/otorrinolaringologia-lebu", "Otorrinolaringólogo desde Lebu", "Consulta de lunes a miércoles por la tarde"),
    ],
    wa_texto="Hola, soy de Lebu y quiero agendar una ecografía.",
)
