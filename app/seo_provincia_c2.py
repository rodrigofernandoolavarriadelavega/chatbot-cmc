"""Páginas localidad x especialidad, parte 2: gastroenterología, cardiología,
oftalmología (examen de la vista), psiquiatría y psicología."""
from seo_provincia import pag, tabla_oferta, bloque_viaje

PAGINAS_2 = {}

# ───────────────────────── Gastroenterología ───────────────────────────────

PAGINAS_2["gastroenterologia-laraquete"] = pag(
    comuna="laraquete", base="gastroenterologia", esp="Gastroenterología", spec="Gastroenterologic",
    title="Gastroenterólogo en Laraquete · Dr. Quijano, a 12 km | CMC",
    meta="Gastroenterólogo para pacientes de Laraquete: Dr. Nicolás Quijano, consulta $35.000 en Carampangue, a 12 km por la Ruta 160. Fechas coordinadas por WhatsApp.",
    h1="Gastroenterólogo para pacientes de <em>Laraquete</em>",
    lead="El Dr. Nicolás Quijano atiende problemas digestivos en Carampangue, a 12 km de Laraquete, unos 12 minutos por la Ruta 160. Consulta particular de $35.000.",
    secciones=[
        ("Cuándo consultar al gastroenterólogo",
         "<p>El gastroenterólogo estudia el esófago, el estómago, el intestino, el hígado y la vesícula. Se consulta por reflujo o acidez frecuente, dolor abdominal que se repite, cambios persistentes en las deposiciones, hígado graso o transaminasas altas detectadas en un examen, o infección por <em>Helicobacter pylori</em>.</p>"
         "<p>Muchos casos se manejan con tratamiento y cambios de hábitos, sin necesidad de endoscopía. Cuando el médico la indica, ese procedimiento se realiza en Concepción.</p>"
         + tabla_oferta(["gastro_consulta", "gastro_revision"])),
        ("Cómo se coordina la fecha",
         "<p>El Dr. Quijano no atiende todos los días: sus fechas se coordinan mes a mes. Para quien vive en Laraquete, la distancia corta permite una gestión simple: se pide la hora por WhatsApp, se recibe la fecha disponible y se confirma. Quien llega con una ecografía abdominal, exámenes de sangre o de deposiciones recientes debe traerlos, porque permiten resolver más en una sola consulta.</p>"),
        ("De Laraquete a Carampangue",
         bloque_viaje("laraquete")
         + "<p>Desde Laraquete se toma la Ruta 160 en dirección sur. El trayecto es breve y directo, de modo que ir y volver el mismo día no requiere organización especial. Si el médico le pide una ecografía abdominal, también se realiza en el centro: <a href=\"/blog/ecografia-laraquete\">ver ecografía</a>.</p>"),
    ],
    faqs=[
        ("¿Tiene bono Fonasa la consulta?", "No. Gastroenterología es solo particular, $35.000. La revisión de exámenes cuesta $17.500."),
        ("¿Necesito ayuno para la consulta?", "No. El ayuno solo se pide para algunos exámenes, como la ecografía abdominal."),
        ("¿Se realizan endoscopías en el centro?", "No. Si el médico las indica, se realizan en Concepción."),
        ("¿El Dr. Quijano atiende a pacientes de otras comunas?", "Sí. Atiende a cualquier paciente, sin importar su comuna, en la sede de Carampangue."),
    ],
    enlaces=[
        ("/comuna/laraquete", "Centro médico para pacientes de Laraquete", "Especialidades y cómo llegar"),
        ("/blog/gastroenterologia", "Gastroenterología: guía de la especialidad", "Síntomas y qué resuelve la consulta"),
        ("/blog/gastroenterologia-canete", "Gastroenterólogo para pacientes de Cañete", "Si viene desde más lejos"),
        ("/blog/ecografia-precio-arauco", "Precios de ecografía", "Para completar el estudio"),
    ],
    wa_texto="Hola, soy de Laraquete y quiero agendar con el gastroenterólogo.",
)

PAGINAS_2["gastroenterologia-arauco"] = pag(
    comuna="arauco", base="gastroenterologia", esp="Gastroenterología", spec="Gastroenterologic",
    title="Gastroenterólogo en Arauco · Dr. Quijano, a 8 km | CMC",
    meta="Gastroenterólogo para pacientes de Arauco: Dr. Nicolás Quijano, $35.000. El centro queda a 8 km, en Carampangue. Reflujo, gastritis, hígado graso. Fechas por WhatsApp.",
    h1="Gastroenterólogo para pacientes de <em>Arauco</em>",
    lead="El Dr. Nicolás Quijano atiende a 8 km de la ciudad de Arauco, en Carampangue. Reflujo, gastritis, colon irritable e hígado graso son los motivos de consulta más comunes.",
    secciones=[
        ("Del examen alterado a la consulta",
         "<p>Es frecuente llegar a gastroenterología después de un examen de rutina: transaminasas elevadas, una ecografía que menciona hígado graso o una vesícula con cálculos. También se consulta por ardor después de comer, hinchazón abdominal o episodios de diarrea y estreñimiento que se repiten.</p>"
         "<p>El especialista revisa los exámenes, indica tratamiento y un plan de alimentación y hábitos. Si necesita una endoscopía o una colonoscopía, la solicita y se realiza en Concepción.</p>"
         + tabla_oferta(["gastro_consulta", "gastro_revision"])),
        ("Si necesita una ecografía antes",
         "<p>Cuando el médico de cabecera sospecha un problema de hígado, vesícula o páncreas, la ecografía abdominal suele ser el primer examen. También se realiza en el centro y puede pedirse por WhatsApp para que las fechas queden cercanas: <a href=\"/blog/ecografia-arauco\">ver ecografía en Arauco</a>.</p>"),
        ("Cómo llegar desde Arauco",
         bloque_viaje("arauco")
         + "<p>El camino entre Arauco y Carampangue es corto: 8 km por la Ruta P-20. Las fechas del Dr. Quijano se coordinan mes a mes, por lo que conviene pedir la hora con anticipación. Llevar los exámenes anteriores, aunque sean de otro centro, permite que la consulta sea más resolutiva.</p>"),
    ],
    faqs=[
        ("¿Hay lista de espera?", "Las fechas del especialista se coordinan por WhatsApp. Si no hay un cupo cercano, se propone la primera fecha disponible."),
        ("¿Sirve llevar exámenes hechos en el hospital?", "Sí. Lleve los informes de ecografía, endoscopía y exámenes de sangre que tenga."),
        ("¿Se puede pagar con Fonasa?", "No. La consulta es particular, $35.000."),
        ("¿Atiende a niños?", "Consulte por WhatsApp la edad del paciente antes de agendar."),
    ],
    enlaces=[
        ("/comuna/arauco", "Centro médico para pacientes de Arauco", "Especialidades y cómo llegar"),
        ("/blog/gastroenterologia", "Gastroenterología: guía de la especialidad", "Síntomas y qué resuelve la consulta"),
        ("/blog/ecografia-arauco", "Ecografía para pacientes de Arauco", "Exámenes y valores"),
    ],
    wa_texto="Hola, soy de Arauco y quiero agendar con el gastroenterólogo.",
)

PAGINAS_2["gastroenterologia-canete"] = pag(
    comuna="canete", base="gastroenterologia", esp="Gastroenterología", spec="Gastroenterologic",
    title="Gastroenterólogo desde Cañete · Dr. Nicolás Quijano | CMC",
    meta="Gastroenterólogo para pacientes de Cañete: Dr. Nicolás Quijano, consulta $35.000 en Carampangue, a 72 km. Cómo coordinar la fecha y qué llevar a la consulta.",
    h1="Gastroenterólogo para pacientes de <em>Cañete</em>",
    lead="Si busca al Dr. Nicolás Quijano desde Cañete, la consulta es en Carampangue, a unos 72 km. Como sus fechas se coordinan mes a mes, se recomienda confirmar antes de viajar.",
    secciones=[
        ("Qué resuelve una consulta de gastroenterología",
         "<p>La consulta permite evaluar reflujo, gastritis, colon irritable, dolor abdominal crónico e hígado graso, y revisar exámenes previos. Con esa información el médico indica tratamiento, controles y, si hace falta, endoscopía o colonoscopía, que se realizan en Concepción.</p>"
         + tabla_oferta(["gastro_consulta", "gastro_revision"])),
        ("Antes de viajar desde Cañete",
         "<p>Por la distancia, vale la pena dejar resuelto lo siguiente antes de salir:</p>"
         "<ul><li>La fecha y la hora, confirmadas por WhatsApp.</li><li>Los exámenes que debe llevar: ecografía abdominal, exámenes de sangre y, si existen, informes de endoscopías previas.</li><li>El valor: $35.000, particular, que se paga en efectivo o por transferencia.</li></ul>"
         "<p>Si no tiene una ecografía reciente, puede pedirla con David Pardo ($40.000) para el mismo viaje, según la disponibilidad de ambas agendas.</p>"),
        ("Ruta desde Cañete",
         bloque_viaje("canete")
         + "<p>Se viaja por la Ruta P-60 y luego por la Ruta 160. Son cerca de 1 hora 20 minutos en auto. Pida una hora de la tarde si necesita volver de día, y deje un margen en invierno por lluvia y neblina.</p>"),
    ],
    faqs=[
        ("¿Quién es el Dr. Quijano?", "Es el gastroenterólogo que atiende en el centro, por fechas que se coordinan mes a mes. Atiende pacientes de todas las comunas."),
        ("¿Se pueden pagar los $35.000 con tarjeta?", "En atención médica se paga en efectivo o por transferencia. Las tarjetas se aceptan solo en atención dental."),
        ("¿Puede el hospital de Cañete derivar a esta consulta?", "No hace falta derivación. Si tiene una orden o exámenes del hospital, llévelos a la consulta."),
        ("¿Hay sede del centro en Cañete?", "No. La única sede está en Monsalve 102, esquina República, Carampangue."),
    ],
    enlaces=[
        ("/comuna/canete", "Centro médico para pacientes de Cañete", "Especialidades, rutas y cómo organizar el viaje"),
        ("/blog/gastroenterologia", "Gastroenterología: guía de la especialidad", "Síntomas y qué resuelve la consulta"),
        ("/blog/ecografia-canete", "Ecografía desde Cañete", "Exámenes y valores"),
        ("/blog/otorrinolaringologia-canete", "Otorrinolaringólogo desde Cañete", "Consulta de lunes a miércoles por la tarde"),
    ],
    wa_texto="Hola, soy de Cañete y quiero agendar con el gastroenterólogo.",
)

# ───────────────────────────── Cardiología ─────────────────────────────────

PAGINAS_2["cardiologia-curanilahue"] = pag(
    comuna="curanilahue", base="cardiologia", esp="Cardiología", spec="Cardiovascular",
    title="Cardiólogo en Curanilahue · Dr. Millán, a 30 km | CMC",
    meta="Cardiólogo para pacientes de Curanilahue: Dr. Miguel Millán, consulta $40.000, electrocardiograma $20.000. Atiende en Carampangue, a 30 km por la Ruta 160.",
    h1="Cardiólogo para pacientes de <em>Curanilahue</em>",
    lead="El Dr. Miguel Millán atiende en Carampangue, a 30 km de Curanilahue. La consulta cuesta $40.000 y el electrocardiograma informado, $20.000.",
    secciones=[
        ("Qué se evalúa en la consulta",
         "<p>El cardiólogo evalúa hipertensión, palpitaciones, soplos, mareos, dolor de pecho que se repite y el riesgo cardiovascular de quien tiene diabetes, colesterol alto o antecedentes familiares. El electrocardiograma, que dura unos 10 minutos, se realiza en el centro.</p>"
         "<p>Si hay dolor intenso de pecho, falta de aire súbita o desmayo, no se debe esperar una hora agendada: llame al 131 o vaya al servicio de urgencia más cercano.</p>"
         + tabla_oferta(["cardio_consulta", "cardio_ecg", "cardio_eco"])),
        ("Una consulta que se coordina por fechas",
         "<p>El Dr. Millán no tiene una agenda diaria: sus fechas se coordinan con el centro. Desde Curanilahue, el trayecto es corto, así que lo recomendable es pedir la hora por WhatsApp, esperar la fecha propuesta y salir solo cuando esté confirmada.</p>"
         "<p>Lleve la lista de los medicamentos que usa, los últimos exámenes de sangre (colesterol, glicemia, creatinina) y cualquier electrocardiograma anterior, aunque sea antiguo, para comparar.</p>"),
        ("De Curanilahue a Carampangue",
         bloque_viaje("curanilahue")
         + "<p>Se viaja por la Ruta 160, sin desvíos. Quien controla su presión en su centro de salud puede llevar esos registros: ayudan a decidir si el tratamiento necesita ajuste. El centro está en Monsalve 102, esquina República.</p>"),
    ],
    faqs=[
        ("¿El ecocardiograma se hace el mismo día?", "No necesariamente. El ecocardiograma ($110.000) se realiza una vez al mes y la persona queda en lista de espera. Pregunte por WhatsApp la próxima fecha."),
        ("¿Se atiende con Fonasa?", "No. La consulta de cardiología es solo particular, $40.000."),
        ("¿Necesito ayuno?", "No para la consulta ni para el electrocardiograma."),
        ("¿Puedo pedir solo el electrocardiograma?", "Sí. El electrocardiograma informado por cardiólogo cuesta $20.000. Consulte por WhatsApp si hay fecha."),
    ],
    enlaces=[
        ("/comuna/curanilahue", "Centro médico para pacientes de Curanilahue", "Especialidades y cómo llegar"),
        ("/blog/cardiologia", "Cardiología: cuándo consultar", "Señales de alerta y exámenes"),
        ("/blog/hipertension-arterial-control", "Hipertensión arterial: control", "Metas de presión y hábitos"),
    ],
    wa_texto="Hola, soy de Curanilahue y quiero agendar con el cardiólogo.",
)

PAGINAS_2["cardiologia-canete"] = pag(
    comuna="canete", base="cardiologia", esp="Cardiología", spec="Cardiovascular",
    title="Cardiólogo desde Cañete · Dr. Millán, consulta $40.000 | CMC",
    meta="Cardiólogo para pacientes de Cañete: Dr. Miguel Millán, consulta $40.000, electrocardiograma $20.000, en Carampangue a 72 km. Cómo planificar la visita.",
    h1="Cardiólogo para pacientes de <em>Cañete</em>",
    lead="La consulta de cardiología del Dr. Miguel Millán se realiza en Carampangue, a unos 72 km de Cañete. Cuesta $40.000 y se coordina por fechas.",
    secciones=[
        ("Consulta y electrocardiograma en una sola visita",
         "<p>Para quien viaja desde lejos, lo más eficiente es resolver la consulta y el electrocardiograma en la misma visita. El cardiólogo evalúa el resultado en el momento, por lo que no hace falta volver para conocerlo.</p>"
         + tabla_oferta(["cardio_consulta", "cardio_ecg", "cardio_eco"])),
        ("Organizar el viaje con una fecha confirmada",
         "<p>La agenda de cardiología se coordina por fechas con el especialista. Escriba por WhatsApp, indique que viaja desde Cañete y pida la fecha y la hora más convenientes: una hora de la tarde permite salir sin madrugar y volver de día en las estaciones más cortas.</p>"
         "<p>Lleve la lista de los medicamentos, los exámenes de sangre recientes y los electrocardiogramas anteriores. Si el motivo es el control de presión, lleve también los registros de las últimas semanas.</p>"),
        ("Ruta desde Cañete",
         bloque_viaje("canete")
         + "<p>El viaje se hace por la Ruta P-60 y luego por la Ruta 160, en cerca de 1 hora 20 minutos. El centro está en Monsalve 102, esquina República, en Carampangue. No hay sede en Cañete.</p>"
         "<p>Ante dolor de pecho intenso, falta de aire súbita o desmayo, no espere una hora agendada: llame al 131 o vaya al servicio de urgencia más cercano, como el hospital de su comuna.</p>"),
    ],
    faqs=[
        ("¿Cuánto cuesta en total la consulta con electrocardiograma?", "$60.000: $40.000 la consulta y $20.000 el electrocardiograma informado por el cardiólogo."),
        ("¿Se puede pagar con Fonasa?", "No. La cardiología es solo particular."),
        ("¿Hay atención de cardiología los fines de semana?", "Las fechas se coordinan con el especialista. Pregunte por WhatsApp qué días tiene disponibles."),
    ],
    enlaces=[
        ("/comuna/canete", "Centro médico para pacientes de Cañete", "Especialidades, rutas y cómo organizar el viaje"),
        ("/blog/cardiologia", "Cardiología: cuándo consultar", "Señales de alerta y exámenes"),
        ("/blog/ecografia-canete", "Ecografía desde Cañete", "Exámenes que se coordinan por fechas"),
    ],
    wa_texto="Hola, soy de Cañete y quiero agendar con el cardiólogo.",
)

# ─────────────────── Examen de la vista (TM oftalmológica) ──────────────────

PAGINAS_2["oftalmologia-arauco"] = pag(
    comuna="arauco", base="oftalmologia", esp="Oftalmología", spec="Optometric",
    title="Examen de la vista en Arauco · $15.000, a 8 km | CMC",
    meta="Examen de la vista para pacientes de Arauco: optometría, fondo de ojo y presión intraocular por $15.000 con TM Ana Celedón. Queda a 8 km, en Carampangue.",
    h1="Examen de la vista para pacientes de <em>Arauco</em>",
    lead="La tecnóloga médica Ana Celedón realiza el examen de la vista en Carampangue, a 8 km de Arauco. Incluye receta de lentes, fondo de ojo y presión intraocular. Valor único: $15.000.",
    secciones=[
        ("Qué es y qué no es esta atención",
         "<p>Es un examen de salud ocular realizado por una tecnóloga médica con mención en oftalmología y optometría. Incluye la medición de la visión y la graduación para la receta de lentes, la exploración preventiva de la retina y el nervio óptico, y la medición de la presión intraocular, que sirve para detectar a tiempo el glaucoma.</p>"
         "<p>No es una consulta con médico oftalmólogo: no se realizan cirugías ni tratamientos médicos oculares. Si el examen muestra algo que requiere evaluación médica, se indica la derivación correspondiente.</p>"
         + tabla_oferta(["oft"])),
        ("Quién debería hacérselo",
         "<p>Es especialmente recomendable para personas con diabetes o hipertensión, para mayores de 40 años que nunca se han medido la presión del ojo y para quienes usan lentes y no se controlan hace más de un año. En los niños, conviene consultar si se acercan mucho a la pantalla o al pizarrón, si entrecierran los ojos o si tienen dolores de cabeza frecuentes después de leer.</p>"),
        ("Cómo llegar desde Arauco",
         bloque_viaje("arauco")
         + "<p>Son 8 km por el camino que une Arauco con Carampangue (Ruta P-20). La atención es presencial y dura unos 20 minutos. Si usa lentes, llévelos. El pago es en efectivo o por transferencia, con el mismo valor para todos los pacientes, sin bono Fonasa.</p>"),
    ],
    faqs=[
        ("¿Cuánto cuesta y tiene bono Fonasa?", "$15.000 para todos los pacientes. No existe bono Fonasa para esta atención."),
        ("¿Entregan la receta de lentes?", "Sí. Si el examen lo indica, se entrega una receta de lentes con respaldo clínico."),
        ("¿Atienden niños?", "Consulte por WhatsApp según la edad del niño. La atención se programa en bloques de 20 minutos."),
        ("¿Qué días atiende?", "Los días y horas se confirman por WhatsApp al agendar."),
    ],
    enlaces=[
        ("/comuna/arauco", "Centro médico para pacientes de Arauco", "Especialidades y cómo llegar"),
        ("/blog/oftalmologia", "Examen de la vista: qué incluye", "Diferencia con el examen de una óptica"),
        ("/blog/diabetes-tipo-2-control", "Diabetes tipo 2: control", "Por qué conviene revisar la retina"),
    ],
    wa_texto="Hola, soy de Arauco y quiero agendar el examen de la vista.",
)

# ─────────────────────────── Psiquiatría y psicología ───────────────────────

PAGINAS_2["psiquiatria-arauco"] = pag(
    comuna="arauco", base="psiquiatria", esp="Psiquiatría", spec="Psychiatric",
    title="Psiquiatra para pacientes de Arauco · Teleconsulta $60.000 | CMC",
    meta="Psiquiatra para pacientes de Arauco: la Dra. Cecilia Unibazo atiende por videollamada, martes y jueves por la tarde. Consulta particular de $60.000. Se agenda por WhatsApp.",
    h1="Psiquiatra para pacientes de <em>Arauco</em>: atención por videollamada",
    lead="La Dra. Cecilia Unibazo atiende por teleconsulta, martes y jueves por la tarde. Quien vive en Arauco puede consultar desde su casa, sin trasladarse a Carampangue.",
    secciones=[
        ("Cómo funciona la teleconsulta",
         "<p>La consulta es por videollamada, con una duración de 40 minutos. Se agenda por WhatsApp, y para reservar el cupo se abona por transferencia el valor completo, $60.000, antes de confirmar la hora. Una vez recibido el comprobante, se agenda y se le entregan las instrucciones para conectarse. El día de la consulta no se paga nada más.</p>"
         + tabla_oferta(["psiq"])),
        ("Psiquiatra o psicólogo",
         "<p>El psiquiatra es médico: evalúa el cuadro, puede indicar medicamentos y descarta causas médicas. El psicólogo trabaja con psicoterapia. Muchas personas se benefician de ambos en conjunto. Para pacientes de Arauco, esa combinación es posible en el mismo centro: la teleconsulta psiquiátrica y las sesiones de psicología, que pueden ser presenciales en Carampangue (a 8 km) o por videollamada, según el profesional.</p>"
         + tabla_oferta(["psico_bono", "psico_part"])),
        ("Qué necesita para conectarse",
         "<p>Un celular o computador con cámara y micrófono, conexión a internet estable y un lugar privado y tranquilo durante la consulta. Conéctese unos minutos antes. Si usted vive en un sector rural de la comuna con señal irregular, avise al agendar: se puede probar la conexión antes.</p>"
         "<p>Si hay riesgo inmediato para usted o para otra persona, no espere la teleconsulta: llame al 131 o a la línea de prevención del suicidio *4141 del Minsal, o acuda a un servicio de urgencia.</p>"),
    ],
    faqs=[
        ("¿Por qué se pide el abono antes?", "Los cupos son pocos y la hora queda reservada solo para usted. Por eso se abona el total antes de confirmar."),
        ("¿La consulta tiene bono Fonasa?", "No. La consulta psiquiátrica es solo particular."),
        ("¿Se pueden recetar medicamentos?", "La psiquiatra, como médico, puede indicar el tratamiento que corresponda según la evaluación."),
        ("¿Puedo ir a Carampangue a que me atiendan en persona?", "La psiquiatría es solo por teleconsulta. La psicología sí tiene atención presencial en la sede."),
    ],
    enlaces=[
        ("/comuna/arauco", "Centro médico para pacientes de Arauco", "Especialidades y cómo llegar"),
        ("/blog/psiquiatria", "Psiquiatría: cuándo consultar", "Motivos frecuentes y cómo funciona"),
        ("/blog/psicologia-adulto", "Psicología adulto con bono Fonasa", "Sesiones presenciales y por videollamada"),
    ],
    wa_texto="Hola, soy de Arauco y quiero agendar con la psiquiatra por teleconsulta.",
)

PAGINAS_2["psiquiatria-canete"] = pag(
    comuna="canete", base="psiquiatria", esp="Psiquiatría", spec="Psychiatric",
    title="Psiquiatra para pacientes de Cañete · Teleconsulta | CMC",
    meta="Psiquiatra para pacientes de Cañete sin viajar: Dra. Cecilia Unibazo, teleconsulta martes y jueves por la tarde, $60.000. Cómo reservar y conectarse.",
    h1="Psiquiatra para pacientes de <em>Cañete</em>: sin viajar",
    lead="Cañete está a unos 72 km de Carampangue, pero la consulta de psiquiatría se realiza por videollamada. La Dra. Cecilia Unibazo atiende martes y jueves por la tarde.",
    secciones=[
        ("Una consulta que no obliga al viaje",
         "<p>A diferencia de otras especialidades del centro, la psiquiatría no exige trasladarse: la Dra. Unibazo atiende solo por teleconsulta, en sesiones de 40 minutos. Para un paciente de Cañete eso evita un trayecto de más de una hora hacia cada lado.</p>"
         "<p>La reserva se hace por WhatsApp. Se abona el valor completo por transferencia antes de que se confirme la hora; una vez recibido el comprobante se agenda y se le entregan las instrucciones para conectarse.</p>"
         + tabla_oferta(["psiq"])),
        ("Revise su conexión antes",
         "<p>En zonas rurales de la comuna, la señal puede ser irregular. Antes de reservar, verifique que en el lugar desde donde se conectará la cobertura de su celular o su internet permita una videollamada estable. Si no es así, avise al agendar: se prueba la conexión previamente, o se busca un horario en que usted pueda estar en un lugar con mejor señal.</p>"
         "<p>Es útil tener a mano una lista de los medicamentos que toma y de los diagnósticos previos, con fechas aproximadas.</p>"),
        ("Si además necesita atención presencial",
         "<p>Si junto con la psiquiatría le conviene psicoterapia, hay sesiones con bono Fonasa ($14.420) por videollamada de lunes a viernes con el psicólogo Jorge Montalba, y atención presencial los sábados. La distancia a Carampangue es de unos 72 km por la Ruta P-60 y la Ruta 160.</p>"
         + tabla_oferta(["psico_bono"])),
    ],
    faqs=[
        ("¿Dónde me conecto?", "Desde cualquier lugar privado con buena señal. Las instrucciones para conectarse se entregan por WhatsApp una vez confirmado el pago."),
        ("¿Se puede pagar con Fonasa?", "No. La consulta de psiquiatría es particular, $60.000."),
        ("¿Hay atención en Cañete?", "No hay sede ni consultas en Cañete. La atención psiquiátrica es por videollamada."),
        ("¿Qué hago ante una crisis?", "La teleconsulta no es un servicio de urgencia. Llame al 131 o a la línea de prevención del suicidio *4141 del Minsal, o acuda al servicio de urgencia más cercano."),
    ],
    enlaces=[
        ("/comuna/canete", "Centro médico para pacientes de Cañete", "Especialidades, rutas y cómo organizar el viaje"),
        ("/blog/psiquiatria", "Psiquiatría: cuándo consultar", "Motivos frecuentes y cómo funciona"),
        ("/blog/neurologia-canete", "Neurología por telemedicina", "Otra consulta que no exige viajar"),
    ],
    wa_texto="Hola, soy de Cañete y quiero agendar con la psiquiatra por teleconsulta.",
)

PAGINAS_2["psicologia-adulto-laraquete"] = pag(
    comuna="laraquete", base="psicologia-adulto", esp="Psicología adulto", spec="Psychiatric",
    title="Psicólogo en Laraquete · Bono Fonasa $14.420, a 12 km | CMC",
    meta="Psicólogo para pacientes de Laraquete: sesiones con bono Fonasa ($14.420) o particular, presenciales en Carampangue a 12 km o por videollamada según el profesional.",
    h1="Psicólogo para pacientes de <em>Laraquete</em>",
    lead="Desde Laraquete, el Centro Médico Carampangue queda a 12 km por la Ruta 160. Hay psicólogos con bono Fonasa ($14.420) y atención presencial o por videollamada.",
    secciones=[
        ("Elegir según horario y modalidad",
         "<p>En el centro atienden tres profesionales de psicología, con condiciones distintas. Jorge Montalba atiende de lunes a viernes de 18:00 a 20:30 por videollamada y los sábados de 9:00 a 14:00 en forma presencial. La psicóloga Jacquelinne Salas atiende en forma presencial, de lunes a viernes de 15:30 a 20:00 y los sábados de 9:00 a 14:00, y atiende niños, adolescentes y adultos. Juan Pablo Rodríguez atiende adultos; consulte su modalidad al agendar.</p>"
         + tabla_oferta(["psico_bono", "psico_part"])),
        ("Una terapia que se sostiene en el tiempo",
         "<p>La psicoterapia suele comenzar con sesiones semanales. Para una persona de Laraquete, la distancia de 12 minutos hace posible mantener la continuidad sin faltar por dificultades de traslado, y la opción por videollamada de lunes a viernes permite no viajar en días de lluvia o de turno largo.</p>"
         "<p>La primera sesión es una conversación para definir qué quiere trabajar y con qué frecuencia. Todo lo que se conversa es confidencial.</p>"),
        ("De Laraquete a Carampangue",
         bloque_viaje("laraquete")
         + "<p>Se toma la Ruta 160 hacia el sur. Para sesiones de tarde, salir de Laraquete 25 minutos antes es suficiente. El centro está en Monsalve 102, esquina República.</p>"),
    ],
    faqs=[
        ("¿Cómo consigo el bono Fonasa?", "Con Montalba y Rodríguez, el bono se emite en el centro con huella biométrica. Con la Ps. Salas los pacientes Fonasa pagan $20.000 directo, sin bono."),
        ("¿Se puede hacer terapia por videollamada?", "Sí, con Jorge Montalba de lunes a viernes. Los sábados su atención es presencial."),
        ("¿Atienden niños?", "Sí. La Ps. Jacquelinne Salas atiende niños, adolescentes y adultos, de forma presencial."),
        ("¿Hay atención en caso de crisis?", "No es un servicio de urgencia. Ante riesgo inmediato, llame al 131 o a la línea de prevención del suicidio *4141 del Minsal."),
    ],
    enlaces=[
        ("/comuna/laraquete", "Centro médico para pacientes de Laraquete", "Especialidades y cómo llegar"),
        ("/blog/psicologia-adulto", "Psicología adulto: cuándo consultar", "Qué esperar de la primera sesión"),
        ("/blog/psicologia-infantil-cuando-consultar", "Psicología infantil: cuándo consultar", "Señales en niños y adolescentes"),
    ],
    wa_texto="Hola, soy de Laraquete y quiero agendar con un psicólogo.",
)
