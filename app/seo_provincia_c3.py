"""Páginas localidad x especialidad, parte 3: atención dental, ortodoncia,
medicina general y atención médica de niños."""
from seo_provincia import pag, tabla_oferta, bloque_viaje

PAGINAS_3 = {}

DENTAL = "Dentistry"
MG = "GeneralPractice"

# ───────────────────────────── Dental ──────────────────────────────────────

PAGINAS_3["odontologia-general-arauco"] = pag(
    comuna="arauco", base="odontologia-general", esp="Odontología general", spec=DENTAL,
    title="Dentista en Arauco · Evaluación $15.000, a 8 km | CMC",
    meta="Dentista para pacientes de Arauco: evaluación $15.000, limpieza $30.000, tapaduras desde $35.000. Dra. Burgos y Dr. Jiménez, en Carampangue, a 8 km.",
    h1="Dentista para pacientes de <em>Arauco</em>",
    lead="La Dra. Javiera Burgos y el Dr. Carlos Jiménez atienden en Carampangue, a 8 km de Arauco. La evaluación dental cuesta $15.000 e incluye el plan de tratamiento.",
    secciones=[
        ("Valores de las atenciones más pedidas",
         "<p>Todas las atenciones dentales son particulares. Se pagan en efectivo, por transferencia o con tarjeta de débito o crédito: en el centro, las tarjetas se aceptan solo en atención dental.</p>"
         + tabla_oferta(["odo_eval", "odo_limpieza", "odo_resina", "odo_exo"])),
        ("Primero la evaluación, después el plan",
         "<p>La evaluación revisa dientes, encías y mordida, y deja por escrito un plan con prioridades. Para pacientes de Arauco, que están a pocos minutos, resulta cómodo dividir el tratamiento en visitas cortas: una limpieza en una visita y las tapaduras en otra, según lo que indique el plan.</p>"
         "<p>Atienden adultos y niños. No hay odontopediatría dedicada, pero ambos odontólogos tratan a menores con técnicas adaptadas a su edad.</p>"),
        ("Horarios y cómo llegar desde Arauco",
         bloque_viaje("arauco")
         + "<p>La Dra. Burgos atiende de lunes a sábado y el Dr. Jiménez los viernes y sábados. Los sábados, de 9:00 a 14:00, son una alternativa para quienes trabajan durante la semana. Desde la ciudad de Arauco son 8 km por el camino que la une con Carampangue (Ruta P-20).</p>"),
        ("Si el plan incluye ortodoncia",
         "<p>La ortodoncia siempre parte con una evaluación con la dentista general, que revisa la boca, pide radiografías y gestiona la derivación con la ortodoncista, la Dra. Daniela Castillo. La instalación de brackets de boca completa cuesta $120.000 y cada control, $30.000.</p>"),
    ],
    faqs=[
        ("¿Se puede pagar con tarjeta?", "Sí, en atención dental se acepta efectivo, transferencia, débito y crédito."),
        ("¿Hay bono Fonasa para atención dental?", "No. La atención dental es solo particular."),
        ("¿Atienden urgencias dentales?", "Escriba por WhatsApp para ver si hay una hora cercana. El centro no es un servicio de urgencias."),
        ("¿Cuánto cuesta una limpieza dental?", "$30.000 por el destartraje y la profilaxis. Se recomienda cada seis meses."),
    ],
    enlaces=[
        ("/comuna/arauco", "Centro médico para pacientes de Arauco", "Especialidades y cómo llegar"),
        ("/blog/odontologia-general", "Odontología general: tratamientos y valores", "Guía completa"),
        ("/blog/limpieza-dental-precio-arauco", "Precio de la limpieza dental", "Cuándo hacerla y qué incluye"),
        ("/blog/precio-ortodoncia-arauco", "Precio de la ortodoncia", "Brackets y controles"),
    ],
    wa_texto="Hola, soy de Arauco y quiero agendar una hora con el dentista.",
)

PAGINAS_3["odontologia-general-los-alamos"] = pag(
    comuna="los-alamos", base="odontologia-general", esp="Odontología general", spec=DENTAL,
    title="Dentista desde Los Álamos · Evaluación $15.000 | CMC",
    meta="Atención dental para pacientes de Los Álamos: evaluación $15.000, limpieza $30.000, ortodoncia con controles. En Carampangue, a 53 km por la Ruta 160.",
    h1="Dentista para pacientes de <em>Los Álamos</em>",
    lead="Desde Los Álamos hasta Carampangue hay unos 53 km por la Ruta 160. La ortodoncia, con controles aproximadamente mensuales, es la atención con más visitas de pacientes de Los Álamos; muchos aprovechan el viaje para otras atenciones dentales.",
    secciones=[
        ("Tratamientos dentales y valores",
         "<p>La evaluación dental ($15.000) incluye el diagnóstico y el plan de tratamiento. Las atenciones se pagan con efectivo, transferencia, débito o crédito.</p>"
         + tabla_oferta(["odo_eval", "odo_limpieza", "odo_resina", "odo_exo"])),
        ("Controles de ortodoncia sin viajes de más",
         "<p>La ortodoncia exige controles periódicos, cada tres o cuatro semanas, durante uno o dos años. Para quien viaja desde Los Álamos, ayuda pedir el siguiente control antes de salir del centro y agrupar en la misma visita una limpieza o una revisión con la dentista general. La ortodoncista, la Dra. Daniela Castillo, define sus fechas de atención; el control cuesta $30.000 y la instalación de brackets de boca completa, $120.000.</p>"
         + tabla_oferta(["orto_brackets", "orto_control"])),
        ("Duración de las visitas y ruta",
         bloque_viaje("los-alamos")
         + "<p>La Dra. Burgos atiende de lunes a sábado, en bloques de 60 minutos, y el Dr. Jiménez los viernes y sábados, en bloques de 30. Un viernes o sábado permite resolver una evaluación y una limpieza en el mismo viaje. Se viaja por la Ruta 160, que pasa por Curanilahue; en invierno conviene salir con luz.</p>"),
    ],
    faqs=[
        ("¿Dónde queda el centro respecto de Los Álamos?", "En Monsalve 102, esquina República, Carampangue. No hay sede en Los Álamos ni en otras comunas."),
        ("¿Se pueden agendar controles de ortodoncia desde Los Álamos?", "Sí. Se agendan por WhatsApp. La fecha depende de la agenda de la ortodoncista."),
        ("¿Atienden niños?", "Sí. La Dra. Burgos y el Dr. Jiménez atienden adultos y niños."),
    ],
    enlaces=[
        ("/comuna/los-alamos", "Centro médico para pacientes de Los Álamos", "Especialidades y cómo llegar"),
        ("/blog/odontologia-general", "Odontología general: tratamientos y valores", "Guía completa"),
        ("/blog/ortodoncia-los-alamos", "Ortodoncia desde Los Álamos", "Proceso, controles y valores"),
        ("/blog/precio-ortodoncia-arauco", "Precio de la ortodoncia", "Brackets y controles"),
    ],
    wa_texto="Hola, soy de Los Álamos y quiero agendar una hora con el dentista.",
)

PAGINAS_3["odontologia-general-lebu"] = pag(
    comuna="lebu", base="odontologia-general", esp="Odontología general", spec=DENTAL,
    title="Dentista desde Lebu · Evaluación $15.000, a 77 km | CMC",
    meta="Atención dental para pacientes de Lebu: evaluación $15.000, limpieza $30.000, tratamiento en pocas visitas. Se realiza en Carampangue, a 77 km por la Ruta 160.",
    h1="Dentista para pacientes de <em>Lebu</em>",
    lead="Para un paciente de Lebu, el viaje a Carampangue es de unos 77 km. Por eso conviene diseñar el tratamiento dental con el menor número posible de visitas.",
    secciones=[
        ("Evaluar y comenzar el mismo día",
         "<p>La evaluación dental cuesta $15.000 y se descuenta si ese mismo día comienzas o dejas pagado el tratamiento (por ejemplo, una limpieza o una tapadura): en ese caso pagas solo la atención. Para quien viene de lejos, esa es la forma de aprovechar el viaje: salir de la primera visita con el plan definido y parte del trabajo hecho.</p>"
         + tabla_oferta(["odo_eval", "odo_limpieza", "odo_resina", "odo_exo"])),
        ("Pedir la hora pensando en el viaje",
         "<p>La Dra. Burgos atiende de lunes a sábado y el Dr. Jiménez los viernes y sábados. Pida su hora por WhatsApp, indique que viaja desde Lebu y consulte por una hora que permita resolver más de un tratamiento en la misma visita. Las horas de la Dra. Burgos son de 60 minutos y las del Dr. Jiménez, de 30.</p>"
         "<p>Se aceptan efectivo, transferencia, débito y crédito.</p>"),
        ("De Lebu a Carampangue",
         bloque_viaje("lebu")
         + "<p>El camino es la Ruta 160, que pasa por Los Álamos y Curanilahue. Calcule cerca de 1 hora 20 minutos en auto. Después de una extracción o una anestesia local, evite conducir si siente adormecimiento; si puede, viaje acompañado.</p>"),
    ],
    faqs=[
        ("¿Atienden con Fonasa?", "No. La atención dental es solo particular."),
        ("¿Qué pasa si necesito ortodoncia o un implante?", "Se parte siempre con la evaluación dental. Desde ahí se gestiona la derivación a la ortodoncista o a la implantóloga, que definen sus propias fechas."),
        ("¿Puedo pedir la hora desde Lebu sin viajar antes?", "Sí. Se agenda por WhatsApp, y solo se viaja el día de la atención."),
    ],
    enlaces=[
        ("/comuna/lebu", "Centro médico para pacientes de Lebu", "Especialidades y cómo llegar"),
        ("/blog/odontologia-general", "Odontología general: tratamientos y valores", "Guía completa"),
        ("/blog/ortodoncia-lebu", "Ortodoncia desde Lebu", "Proceso, controles y valores"),
    ],
    wa_texto="Hola, soy de Lebu y quiero agendar una hora con el dentista.",
)

PAGINAS_3["odontologia-general-canete"] = pag(
    comuna="canete", base="odontologia-general", esp="Odontología general", spec=DENTAL,
    title="Dentista desde Cañete · Atención dental en Carampangue | CMC",
    meta="Atención dental para pacientes de Cañete: evaluación $15.000, limpieza $30.000, ortodoncia e implantes por derivación. Se realiza en Carampangue, a 72 km.",
    h1="Dentista para pacientes de <em>Cañete</em>",
    lead="La atención dental se realiza en Carampangue, a unos 72 km de Cañete. Si busca un dentista o una clínica dental en Cañete, esta es la alternativa más cercana del centro: una sola sede, con plan de tratamiento desde la primera visita.",
    secciones=[
        ("Qué se puede resolver en una visita",
         "<p>La evaluación dental ($15.000) revisa dientes, encías y mordida, y define un plan. En la misma visita se puede avanzar con una limpieza o con una tapadura, según el caso. Para quien viaja desde Cañete, esa organización permite reducir el número de viajes.</p>"
         + tabla_oferta(["odo_eval", "odo_limpieza", "odo_resina", "odo_exo"])),
        ("Sábado: una opción para quienes trabajan",
         "<p>Los sábados, de 9:00 a 14:00, atienden tanto la Dra. Javiera Burgos como el Dr. Carlos Jiménez. Un sábado temprano permite salir de Cañete con luz y estar de vuelta antes de la tarde. La Dra. Burgos también atiende de lunes a viernes, y el Dr. Jiménez los viernes.</p>"
         "<p>Se acepta pago en efectivo, transferencia, débito y crédito.</p>"),
        ("Ruta desde Cañete",
         bloque_viaje("canete")
         + "<p>Se viaja por la Ruta P-60 y luego por la Ruta 160. En invierno, con lluvia o neblina, conviene sumar tiempo. El centro no tiene sede en Cañete: está en Monsalve 102, esquina República, Carampangue.</p>"),
        ("Ortodoncia desde Cañete",
         "<p>La ortodoncia comienza con la evaluación con la dentista general, que gestiona la derivación con la ortodoncista. Los controles son periódicos, por lo que conviene calcular el costo de traslado antes de iniciar. Para ver el proceso y los valores, revise la guía de ortodoncia desde Cañete.</p>"),
    ],
    faqs=[
        ("¿Hay una clínica dental del centro en Cañete?", "No. La única sede está en Monsalve 102, esquina República, Carampangue."),
        ("¿La atención dental tiene bono Fonasa?", "No. Es solo particular."),
        ("¿Se puede pedir la hora por WhatsApp desde Cañete?", "Sí. Se agenda por WhatsApp y solo se viaja el día de la atención."),
        ("¿Atienden niños?", "Sí. Ambos odontólogos atienden adultos y niños."),
    ],
    enlaces=[
        ("/comuna/canete", "Centro médico para pacientes de Cañete", "Especialidades, rutas y cómo organizar el viaje"),
        ("/blog/odontologia-general", "Odontología general: tratamientos y valores", "Guía completa"),
        ("/blog/ortodoncia-canete", "Ortodoncia desde Cañete", "Proceso, controles y valores"),
        ("/blog/limpieza-dental-precio-arauco", "Precio de la limpieza dental", "Cuándo hacerla y qué incluye"),
    ],
    wa_texto="Hola, soy de Cañete y quiero agendar una hora con el dentista.",
)

PAGINAS_3["ortodoncia-curanilahue"] = pag(
    comuna="curanilahue", base="ortodoncia", esp="Ortodoncia", spec=DENTAL,
    title="Ortodoncia en Curanilahue · Brackets $120.000, controles $30.000 | CMC",
    meta="Ortodoncia para pacientes de Curanilahue: instalación de brackets $120.000 y controles de $30.000 con la Dra. Daniela Castillo. En Carampangue, a 30 km.",
    h1="Ortodoncia para pacientes de <em>Curanilahue</em>",
    lead="Fuera de la comuna de Arauco, Curanilahue es la localidad de la que más pacientes vienen a controles de ortodoncia al centro. La razón es práctica: 30 km por la Ruta 160 permiten mantener los controles mensuales sin faltar.",
    secciones=[
        ("Cómo se inicia el tratamiento",
         "<p>La ortodoncia parte con una evaluación con la dentista general (Dra. Javiera Burgos o Dr. Carlos Jiménez), que revisa la boca, solicita radiografías y descarta caries o problemas de encías antes de instalar los brackets. Después gestiona la derivación con la ortodoncista, la Dra. Daniela Castillo. La evaluación cuesta $15.000 y se descuenta si ese mismo día comienzas o dejas pagado el tratamiento previo (casi siempre limpieza y flúor).</p>"
         + tabla_oferta(["odo_eval", "orto_brackets", "orto_control"])),
        ("Constancia: el factor que más pesa",
         "<p>Un tratamiento de ortodoncia dura entre 18 y 36 meses, con visitas cada tres o cuatro semanas. Lo que más determina el resultado es no perder controles. Para una familia de Curanilahue, la forma de sostenerlos es pedir el siguiente control antes de salir de la consulta, acordar una hora que no choque con el trabajo o el colegio y avisar por WhatsApp con tiempo si hay que reagendar.</p>"
         "<p>En niños y adolescentes, el control de ortopedia cuesta $20.000.</p>"),
        ("De Curanilahue a Carampangue",
         bloque_viaje("curanilahue")
         + "<p>Se viaja por la Ruta 160. Con buen tiempo, 30 minutos de ida. Las fechas de la ortodoncista se coordinan por WhatsApp, por lo que se recomienda confirmar antes de salir. El centro está en Monsalve 102, esquina República.</p>"),
    ],
    faqs=[
        ("¿Cuánto cuesta en total?", "La instalación de brackets de boca completa cuesta $120.000. Cada control, $30.000. El valor total depende de la duración, que se define en la evaluación."),
        ("¿Se puede pagar con tarjeta?", "Sí. En atención dental se acepta efectivo, transferencia, débito y crédito."),
        ("¿Se puede comenzar directamente con la ortodoncista?", "No. Siempre se parte con la evaluación con la dentista general, que gestiona la derivación."),
    ],
    enlaces=[
        ("/comuna/curanilahue", "Centro médico para pacientes de Curanilahue", "Especialidades y cómo llegar"),
        ("/blog/ortodoncia", "Ortodoncia: cómo es el tratamiento", "Etapas, controles y valores"),
        ("/blog/precio-ortodoncia-arauco", "Precio de la ortodoncia", "Brackets y controles"),
        ("/blog/odontologia-general-curanilahue", "Atención dental desde Curanilahue", "Evaluación y tratamientos previos"),
    ],
    wa_texto="Hola, soy de Curanilahue y quiero agendar un control de ortodoncia.",
)

# ───────────────────────── Medicina general ───────────────────────────────

PAGINAS_3["medicina-general-curanilahue"] = pag(
    comuna="curanilahue", base="medicina-general", esp="Medicina general", spec=MG,
    title="Médico general desde Curanilahue · Bono Fonasa $7.880 | CMC",
    meta="Médico general para pacientes de Curanilahue: bono Fonasa $7.880 o particular $25.000, lunes a sábado en Carampangue, a 30 km por la Ruta 160.",
    h1="Médico general para pacientes de <em>Curanilahue</em>",
    lead="Es la atención que más pacientes de Curanilahue piden al centro. Con bono Fonasa cuesta $7.880 y el bono se emite en el mismo centro, sin trámites previos.",
    secciones=[
        ("Valores y qué incluye",
         "<p>La consulta de medicina general incluye diagnóstico, tratamiento, recetas, licencias médicas y derivación a especialista. La revisión de exámenes no tiene costo.</p>"
         + tabla_oferta(["mg_fonasa", "mg_part", "mg_control"])),
        ("Tres médicos, casi toda la semana",
         "<p>El Dr. Rodrigo Olavarría atiende de lunes a sábado, el Dr. Andrés Abarca de lunes a viernes y el Dr. Alonso Márquez, que ejerce como médico familiar, lunes, miércoles y viernes. Para una consulta de morbilidad (un cuadro agudo), el centro ofrece horas el mismo día según disponibilidad; se consultan por WhatsApp.</p>"),
        ("El viaje desde Curanilahue",
         bloque_viaje("curanilahue")
         + "<p>La Ruta 160 es el camino directo. Para el bono Fonasa, lleve su cédula de identidad: se emite en el centro con huella biométrica, de modo que no es necesario comprarlo antes ni hacer otro trámite. Quien tiene hipertensión o diabetes puede combinar el control con la revisión de sus exámenes en la misma visita.</p>"),
    ],
    faqs=[
        ("¿Se puede agendar el mismo día?", "Muchas veces sí, según disponibilidad. Escriba por WhatsApp y se le ofrece la primera hora libre."),
        ("¿Qué pasa si necesito un especialista?", "El médico general evalúa y, si corresponde, deriva. En el centro hay otras especialidades, como otorrinolaringología, cardiología y ecografía, con valores propios."),
        ("¿Dan licencias médicas?", "Sí, cuando el médico lo estima necesario tras la evaluación."),
    ],
    enlaces=[
        ("/comuna/curanilahue", "Centro médico para pacientes de Curanilahue", "Especialidades y cómo llegar"),
        ("/blog/medicina-general", "Medicina general en Carampangue", "Valores, médicos y horarios"),
        ("/blog/bono-fonasa-mle-arauco", "Bono Fonasa MLE", "Cómo funciona y dónde se emite"),
        ("/blog/otorrinolaringologia-curanilahue", "Otorrinolaringólogo desde Curanilahue", "Consulta de lunes a miércoles por la tarde"),
    ],
    wa_texto="Hola, soy de Curanilahue y quiero agendar con un médico general.",
)

PAGINAS_3["medicina-general-arauco"] = pag(
    comuna="arauco", base="medicina-general", esp="Medicina general", spec=MG,
    title="Médico general en Arauco · Bono Fonasa $7.880, a 8 km | CMC",
    meta="Médico general para pacientes de Arauco: bono Fonasa $7.880 o particular $25.000, lunes a sábado en Carampangue, a 8 km de la ciudad de Arauco.",
    h1="Médico general para pacientes de <em>Arauco</em>",
    lead="Carampangue pertenece a la comuna de Arauco y queda a 8 km de la ciudad. La consulta con bono Fonasa cuesta $7.880 y se atiende de lunes a sábado.",
    secciones=[
        ("Atención sin salir de la comuna",
         "<p>La medicina general es la primera puerta de entrada: resuelve cuadros agudos, controla enfermedades crónicas como la hipertensión y la diabetes, emite licencias y recetas, y deriva a especialistas cuando corresponde. Quien vive en Arauco, Ramadilla o Laraquete puede resolverla en Carampangue en pocos minutos.</p>"
         + tabla_oferta(["mg_fonasa", "mg_part", "mg_control"])),
        ("Los tres médicos y sus días",
         "<p>El Dr. Rodrigo Olavarría atiende de lunes a sábado; el Dr. Andrés Abarca, de lunes a viernes; y el Dr. Alonso Márquez, médico familiar, lunes, miércoles y viernes. El centro funciona de lunes a viernes de 8:00 a 21:00 y los sábados de 9:00 a 14:00.</p>"
         "<p>El bono Fonasa se emite en el centro con huella biométrica el día de la atención. Lleve su cédula de identidad.</p>"),
        ("Cómo llegar desde Arauco",
         bloque_viaje("arauco")
         + "<p>Son 8 km por el camino que une Arauco con Carampangue (Ruta P-20). El centro está en Monsalve 102, esquina República, con estacionamiento libre en la calle. Si necesita una consulta de especialidad después de la evaluación, puede pedirla en el mismo lugar.</p>"),
    ],
    faqs=[
        ("¿Se puede ir sin hora?", "No. Se atiende con hora agendada, que se pide por WhatsApp. A menudo hay horas el mismo día."),
        ("¿Atienden niños?", "Sí. Para resfríos, fiebre y controles. Para casos más complejos se deriva a un especialista."),
        ("¿Qué pasa con el control de exámenes?", "La revisión de resultados de exámenes no tiene costo."),
    ],
    enlaces=[
        ("/comuna/arauco", "Centro médico para pacientes de Arauco", "Especialidades y cómo llegar"),
        ("/blog/medicina-general", "Medicina general en Carampangue", "Valores, médicos y horarios"),
        ("/blog/bono-fonasa-mle-arauco", "Bono Fonasa MLE", "Cómo funciona y dónde se emite"),
        ("/blog/pediatra-arauco", "Atención médica de niños", "Qué atenciones infantiles hay en el centro"),
    ],
    wa_texto="Hola, soy de Arauco y quiero agendar con un médico general.",
)

PAGINAS_3["medicina-general-los-alamos"] = pag(
    comuna="los-alamos", base="medicina-general", esp="Medicina general", spec=MG,
    title="Médico general desde Los Álamos · Bono Fonasa $7.880 | CMC",
    meta="Médico general para pacientes de Los Álamos: bono Fonasa $7.880 o particular $25.000, en Carampangue, a 53 km por la Ruta 160. Cómo aprovechar el viaje.",
    h1="Médico general para pacientes de <em>Los Álamos</em>",
    lead="Desde Los Álamos al Centro Médico Carampangue hay unos 53 km por la Ruta 160. La consulta con bono Fonasa cuesta $7.880 y la revisión de exámenes no tiene costo.",
    secciones=[
        ("Aprovechar un viaje de una hora",
         "<p>Cuando la distancia es de una hora, conviene que la consulta resuelva lo más posible. Lleve sus exámenes recientes, las recetas vigentes y la lista de los medicamentos que usa. El médico puede renovar tratamientos, emitir licencias, pedir nuevos exámenes y derivar a un especialista, y la revisión posterior de los resultados no tiene costo.</p>"
         + tabla_oferta(["mg_fonasa", "mg_part", "mg_control"])),
        ("Elegir día de viaje",
         "<p>El Dr. Rodrigo Olavarría atiende de lunes a sábado; el Dr. Andrés Abarca, de lunes a viernes; y el Dr. Alonso Márquez, lunes, miércoles y viernes. Los sábados, de 9:00 a 14:00, es una alternativa para quienes trabajan durante la semana.</p>"
         "<p>El bono Fonasa se emite en el centro con huella biométrica, de modo que no hace falta comprarlo antes.</p>"),
        ("La ruta desde Los Álamos",
         bloque_viaje("los-alamos")
         + "<p>Se viaja por la Ruta 160, que pasa por Curanilahue. El camino es directo. Si el motivo es un control de presión o de azúcar, pida una hora de mañana para volver con luz en invierno.</p>"),
    ],
    faqs=[
        ("¿Hay atención en Los Álamos?", "No. El centro tiene una sola sede, en Monsalve 102, esquina República, Carampangue."),
        ("¿Puedo pedir la hora por WhatsApp?", "Sí. Se agenda por WhatsApp y solo se viaja el día de la atención."),
        ("¿Dan licencias médicas?", "Sí, cuando el médico lo estima necesario después de evaluar al paciente."),
    ],
    enlaces=[
        ("/comuna/los-alamos", "Centro médico para pacientes de Los Álamos", "Especialidades y cómo llegar"),
        ("/blog/medicina-general", "Medicina general en Carampangue", "Valores, médicos y horarios"),
        ("/blog/bono-fonasa-mle-arauco", "Bono Fonasa MLE", "Cómo funciona y dónde se emite"),
        ("/blog/odontologia-general-los-alamos", "Atención dental desde Los Álamos", "Evaluación, limpieza y ortodoncia"),
    ],
    wa_texto="Hola, soy de Los Álamos y quiero agendar con un médico general.",
)

PAGINAS_3["medicina-general-lebu"] = pag(
    comuna="lebu", base="medicina-general", esp="Medicina general", spec=MG,
    title="Médico general desde Lebu · Fonasa $7.880, a 77 km | CMC",
    meta="Médico general para pacientes de Lebu: bono Fonasa $7.880 (se emite en el centro) o particular $25.000, en Carampangue, a 77 km por la Ruta 160.",
    h1="Médico general para pacientes de <em>Lebu</em>",
    lead="Si atiende su salud con Fonasa, el bono de medicina general ($7.880) se emite en el mismo centro, con huella biométrica, el día de la atención. Lebu queda a unos 77 km, por la Ruta 160.",
    secciones=[
        ("Fonasa sin trámites previos",
         "<p>Para un paciente de Lebu, un trámite previo para comprar el bono significaría más tiempo y otro traslado. En el centro, el bono Fonasa de medicina general se emite al llegar, con la cédula de identidad y la huella. La consulta particular cuesta $25.000, o $30.000 con el médico familiar Dr. Alonso Márquez.</p>"
         + tabla_oferta(["mg_fonasa", "mg_part", "mg_control"])),
        ("Una visita bien aprovechada",
         "<p>El viaje justifica resolver varias cosas en una sola atención: la consulta, la renovación de recetas, la licencia si corresponde y la derivación a un especialista del mismo centro (por ejemplo, otorrinolaringología o ecografía), cuyas fechas se coordinan por WhatsApp. Pregunte al agendar qué otras horas hay el mismo día.</p>"
         "<p>El Dr. Olavarría atiende de lunes a sábado, el Dr. Abarca de lunes a viernes y el Dr. Márquez, lunes, miércoles y viernes.</p>"),
        ("De Lebu a Carampangue",
         bloque_viaje("lebu")
         + "<p>El trayecto se hace por la Ruta 160, que pasa por Los Álamos y Curanilahue. Calcule cerca de 1 hora 20 minutos. No hay sede del centro en Lebu: la dirección es Monsalve 102, esquina República, Carampangue.</p>"),
    ],
    faqs=[
        ("¿Puedo usar el bono Fonasa que compré en otro lugar?", "El bono de medicina general se emite en el centro al momento de la atención. Consulte por WhatsApp si tiene dudas sobre un bono ya comprado."),
        ("¿Atienden sin hora?", "No. Se atiende con hora agendada, que se pide por WhatsApp."),
        ("¿Cuánto cuesta la revisión de exámenes?", "No tiene costo."),
    ],
    enlaces=[
        ("/comuna/lebu", "Centro médico para pacientes de Lebu", "Especialidades y cómo llegar"),
        ("/blog/medicina-general", "Medicina general en Carampangue", "Valores, médicos y horarios"),
        ("/blog/bono-fonasa-mle-arauco", "Bono Fonasa MLE", "Cómo funciona y dónde se emite"),
        ("/blog/otorrinolaringologia-lebu", "Otorrinolaringólogo desde Lebu", "Consulta de lunes a miércoles por la tarde"),
    ],
    wa_texto="Hola, soy de Lebu y quiero agendar con un médico general.",
)

PAGINAS_3["medicina-general-canete"] = pag(
    comuna="canete", base="medicina-general", esp="Medicina general", spec=MG,
    title="Médico general desde Cañete · Bono Fonasa $7.880 | CMC",
    meta="Médico general para pacientes de Cañete: bono Fonasa $7.880 o particular $25.000, lunes a sábado en Carampangue, a 72 km por la Ruta P-60 y la Ruta 160.",
    h1="Médico general para pacientes de <em>Cañete</em>",
    lead="Los sábados, de 9:00 a 14:00, atiende el Dr. Rodrigo Olavarría, una buena alternativa para quien viaja desde Cañete. Desde allí hay unos 72 km hasta Carampangue.",
    secciones=[
        ("Valores y qué incluye la consulta",
         "<p>La consulta de medicina general incluye evaluación, tratamiento, recetas, licencias y derivación a especialista. Con bono Fonasa cuesta $7.880; el bono se emite en el centro con huella biométrica. Particular: $25.000.</p>"
         + tabla_oferta(["mg_fonasa", "mg_part", "mg_control"])),
        ("Elegir el día del viaje",
         "<p>El Dr. Olavarría atiende de lunes a sábado; el Dr. Abarca, de lunes a viernes; y el Dr. Alonso Márquez, médico familiar, lunes, miércoles y viernes. Un sábado en la mañana permite salir de Cañete temprano y volver antes de la tarde, sin pedir permiso en el trabajo.</p>"
         "<p>Si necesita ver a un especialista del centro, pregunte por WhatsApp qué días atiende y si coincide con su viaje.</p>"),
        ("Ruta desde Cañete",
         bloque_viaje("canete")
         + "<p>Se toma la Ruta P-60 y luego la Ruta 160 hacia Carampangue. En invierno, con lluvia o neblina, conviene sumar tiempo. El centro no tiene sede en Cañete.</p>"
         "<p>Para una emergencia, no espere una hora agendada: llame al 131 o acuda al servicio de urgencia más cercano.</p>"),
    ],
    faqs=[
        ("¿Se puede pagar con tarjeta?", "En atención médica se paga en efectivo o por transferencia. Las tarjetas se aceptan solo en atención dental."),
        ("¿Se puede pedir la hora desde Cañete por WhatsApp?", "Sí. La hora se agenda por WhatsApp y solo se viaja el día de la atención."),
        ("¿Es necesario traer algún documento?", "La cédula de identidad. Si tiene exámenes o recetas anteriores, llévelos."),
    ],
    enlaces=[
        ("/comuna/canete", "Centro médico para pacientes de Cañete", "Especialidades, rutas y cómo organizar el viaje"),
        ("/blog/medicina-general", "Medicina general en Carampangue", "Valores, médicos y horarios"),
        ("/blog/bono-fonasa-mle-arauco", "Bono Fonasa MLE", "Cómo funciona y dónde se emite"),
        ("/blog/odontologia-general-canete", "Atención dental desde Cañete", "Sábado de atención dental"),
    ],
    wa_texto="Hola, soy de Cañete y quiero agendar con un médico general.",
)

# ─────────────────── Atención de niños (URL nueva) ──────────────────────────

PAGINAS_3["pediatra-arauco"] = pag(
    comuna="arauco", base="medicina-general", esp="Atención médica de niños", spec="FamilyPractice",
    title="¿Pediatra en Arauco? Atención médica para niños en Carampangue | CMC",
    meta="El Centro Médico Carampangue no cuenta con pediatra. Los niños se atienden con medicina familiar y general, psicología infantil, fonoaudiología y odontología.",
    h1="¿Hay pediatra en <em>Arauco</em>? Atención médica para niños",
    lead="El centro no cuenta con un médico pediatra. Los niños se atienden con medicina familiar y medicina general, y con psicología infantil, fonoaudiología y odontología. Está en Carampangue, a 8 km de la ciudad de Arauco.",
    secciones=[
        ("Qué atención infantil hay y con quién",
         "<p>Es mejor saberlo antes de venir: no hay pediatra. Para resfríos, fiebre, infecciones comunes y controles, los niños se atienden con medicina familiar (Dr. Alonso Márquez, lunes, miércoles y viernes) o con medicina general. Si el médico considera que el niño necesita un pediatra u otro especialista, lo deriva.</p>"
         + tabla_oferta(["mf_ninos", "mg_fonasa", "mg_part"])),
        ("Otras atenciones para niños y adolescentes",
         "<ul>"
         "<li><strong>Psicología:</strong> la Ps. Jacquelinne Salas atiende niños, adolescentes y adultos, de forma presencial (Fonasa $20.000 directo, particular $25.000). Jorge Montalba también atiende psicología infantil, con bono Fonasa ($14.420).</li>"
         "<li><strong>Fonoaudiología:</strong> Juana Arratia evalúa lenguaje, habla, voz y deglución en niños. La evaluación cuesta $30.000.</li>"
         "<li><strong>Odontología:</strong> la Dra. Burgos y el Dr. Jiménez atienden niños; no hay odontopediatría dedicada. Evaluación: $15.000.</li>"
         "<li><strong>Podología infantil:</strong> Andrea Guevara realiza atención pediátrica por $13.000.</li>"
         "</ul>"),
        ("Cómo llegar desde Arauco",
         bloque_viaje("arauco")
         + "<p>Son 8 km por el camino que une Arauco con Carampangue (Ruta P-20), unos 11 minutos en auto. Lleve el carné de control del niño y, si tiene, los informes anteriores.</p>"
         "<p>Las vacunas del Programa Nacional de Inmunización se aplican en la red pública de salud. Para saber cuál corresponde a cada edad, revise el calendario 2026 en nuestra guía.</p>"),
        ("Cuándo no esperar una hora agendada",
         "<p>Acuda a un servicio de urgencia, o llame al 131, si el niño tiene dificultad para respirar, fiebre alta que no baja, decaimiento marcado, convulsiones, signos de deshidratación o si es un lactante menor con fiebre.</p>"),
    ],
    faqs=[
        ("¿Dónde encuentro un pediatra cerca de Arauco?", "El centro no cuenta con pediatra. Los niños se atienden con medicina familiar o general. Si se necesita un pediatra, el médico lo deriva."),
        ("¿Cuánto cuesta la consulta de un niño?", "Con bono Fonasa, $7.880 en medicina general o familiar. Particular: $25.000 en medicina general y $30.000 con el Dr. Márquez."),
        ("¿Desde qué edad se atienden niños?", "Para medicina general y familiar, consulte la edad del niño por WhatsApp antes de agendar."),
        ("¿Aplican vacunas?", "Las vacunas del PNI se aplican en la red pública. El centro no es un vacunatorio."),
    ],
    enlaces=[
        ("/comuna/arauco", "Centro médico para pacientes de Arauco", "Especialidades y cómo llegar"),
        ("/blog/medicina-general", "Medicina general en Carampangue", "Valores, médicos y horarios"),
        ("/blog/vacunas-pni-calendario-2026", "Calendario de vacunas PNI 2026", "Qué vacuna corresponde a cada edad"),
        ("/blog/psicologia-infantil-cuando-consultar", "Psicología infantil: cuándo consultar", "Señales en niños y adolescentes"),
    ],
    wa_texto="Hola, quiero agendar una hora de atención médica para mi hijo o hija.",
    eyebrow="Atención médica de niños · Provincia de Arauco",
    cta_h2="Atención para <em>niños y adolescentes</em>, cerca de casa",
)
