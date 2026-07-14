# Protocolo de prueba de usabilidad moderada — Portal del Paciente v5

**Centro Médico Carampangue (CMC)** · Carampangue, comuna de Arauco, Región del Biobío
**Producto probado:** Portal del Paciente v5 en modo demo — `https://agentecmc.cl/portal/v5?demo=1`
**Participantes:** 5 pacientes reales de 60 años o más
**Formato:** prueba moderada, presencial, 1 a 1, en el CMC, 30–40 min por sesión
**Versión imprimible de los materiales:** `static/protocolo-prueba-v5.html`

> **Regla de oro:** todo se hace en **modo demo** (`?demo=1`), con datos ficticios
> (RUT de prueba `50.000.000-7`, código `123456`). **NUNCA** se abre la ficha real
> del participante ni se escribe nada en Medilink. El banner "MODO DEMO" debe
> estar visible en pantalla durante toda la sesión.

---

## 1. Objetivo e hipótesis

### 1.1 Objetivo

Verificar si un paciente típico del CMC de 60+ años, usuario de WhatsApp y con
alfabetización digital variable, puede **usar el portal v5 sin ayuda** para las
cinco acciones núcleo (entrar, entender su próxima cita, pedir una hora,
registrar una presión, y reaccionar bien ante una emergencia), y detectar los
problemas de usabilidad que hay que corregir **antes** de invitar a la base de
pacientes real.

### 1.2 Hipótesis medibles

| # | Hipótesis | Umbral de éxito | Tarea |
|---|-----------|-----------------|-------|
| H1 | Los participantes pueden **entrar al portal** con el código por WhatsApp sin ayuda | **≥4/5** lo logran solos en **<3 min** | T1 |
| H2 | El home comunica la **próxima cita** sin esfuerzo | **5/5** dicen correctamente fecha, hora y profesional en **<1 min**, sin ayuda | T2 |
| H3 | El asistente paso a paso permite **agendar** sin ayuda | **≥4/5** completan agendar kinesiología solos en **<4 min** | T3 |
| H4 | Pueden **registrar una presión arterial** sin ayuda | **≥3/5** registran 135/85 solos en **<3 min** | T4 |
| H5 | Ante síntoma de riesgo vital, el portal los lleva a **SAMU 131**, no al centro | **5/5** llegan a "llamar al 131" (banderas rojas o verbalizado) en **<1 min** | T5 |

Métricas secundarias (no bloquean el GO por sí solas, pero se reportan):

- **SEQ promedio ≥ 5,5** (escala 1–7) sobre las 5 tareas.
- **SUS-corto adaptado ≥ 16/20** promedio (ver §7.4).
- **Cero hallazgos de severidad "bloqueante"** sin plan de corrección.

**Nota sobre H5:** es un criterio de **seguridad**, por eso el umbral es 5/5.
Si un solo participante, con dolor de pecho simulado, termina llamando al
centro o navegando perdido, eso se corrige antes del lanzamiento, sin excepción.

---

## 2. Reclutamiento

### 2.1 Criterios de inclusión

- **Edad:** 60 años o más al día de la sesión.
- **Paciente real del CMC:** al menos una atención en los últimos 12 meses.
- **Usuario de WhatsApp:** lo usa al menos 1 vez por semana en su propio celular.
- **Puede leer** textos simples en pantalla (con sus lentes si los usa).
- **Puede dar consentimiento informado** por sí mismo.

### 2.2 Criterios de exclusión

- Familiares directos del staff del CMC (cónyuge, hijos, hermanos, padres) o
  cualquier persona que trabaje o haya trabajado en el centro.
- Profesionales de salud o informática (sesgan la muestra).
- Deterioro cognitivo o sensorial que impida entender la prueba o firmar el
  consentimiento (juicio clínico de quien recluta; ante la duda, no incluir).
- Haber participado en pruebas anteriores del portal o del chatbot.

### 2.3 Composición de la muestra (n=5)

| Cuota | Mínimo |
|-------|--------|
| Hombres | ≥2 |
| Mujeres | ≥2 |
| Alfabetización digital **baja** (solo usa WhatsApp y llamadas) | ≥2 |
| Alfabetización digital **media/alta** (usa además banco, correo o redes) | ≥1 |
| Edad 70+ | ≥1 |
| Vive fuera del pueblo de Carampangue (sector rural / Arauco / Curanilahue u otro) | ≥1 |

La alfabetización digital se autodeclara con una pregunta al reclutar:
*"En su celular, ¿usa solamente WhatsApp y llamadas, o también otras cosas como
el banco, el correo o Facebook?"* → **Baja** = solo WhatsApp/llamadas;
**Media/alta** = lo demás.

### 2.4 Guion de invitación

**Por WhatsApp** (desde el número del centro, +56 9 6661 0737):

> Hola, le escribimos del Centro Médico Carampangue. Estamos preparando una
> página nueva para que nuestros pacientes puedan ver sus horas y pedir citas
> desde el celular, y antes de lanzarla queremos que la prueben pacientes de
> verdad. ¿Le gustaría ayudarnos? Es una visita de 30 a 40 minutos aquí en el
> centro, no necesita saber de computación (al contrario, mientras menos sepa,
> más nos sirve), y como agradecimiento le tenemos [COMPENSACIÓN]. No es una
> atención médica ni un examen: solo probar la página y contarnos qué le parece.
> Si le interesa, dígame qué día y hora le acomodan esta semana.

**En recepción** (la recepcionista, a pacientes que cumplan el perfil):

> "Don/Señora [nombre], estamos probando una página nueva del centro para pedir
> horas por el celular, y buscamos pacientes que nos ayuden a probarla antes de
> lanzarla. Son 30 a 40 minutos aquí mismo, otro día que a usted le acomode, y
> le damos [COMPENSACIÓN] por ayudarnos. No hay que saber de computación, y no
> es un examen: la que se pone a prueba es la página, no usted. ¿Le interesa?"

**Confirmación** (WhatsApp, 1 día antes):

> Le recordamos su visita de mañana a las [hora] en el Centro Médico
> Carampangue para probar la página nueva. Traiga sus lentes si usa, y su
> celular con WhatsApp. Si no puede venir, avísenos por aquí y buscamos otro
> día. ¡Gracias!

### 2.5 Compensación — **DEFINIR POR EL DUEÑO** (elegir UNA antes de reclutar)

| Opción | Detalle | Costo aprox. |
|--------|---------|--------------|
| A | Descuento de $5.000 en su próxima consulta en el CMC | $5.000/persona |
| B | Control gratuito de presión + medición de peso/talla el día de la sesión, más $5.000 de descuento en próxima hora | tiempo staff + $5.000 |
| C | Gift local (caja de té/café + galletas, o giftcard de comercio de Carampangue por ~$10.000) | ~$10.000/persona |

Reglas: la compensación **se entrega igual** aunque la persona se retire a
mitad de la sesión, y se dice explícitamente en el consentimiento. No ofrecer
dinero en efectivo directo (incentiva participación por necesidad, sesga).

---

## 3. Consideraciones éticas

### 3.1 Principios

1. **Se prueba el portal, no la persona.** Se dice al inicio y se repite si el
   participante se frustra o se disculpa ("qué torpe soy").
2. **Datos ficticios siempre.** Modo demo con RUT `50.000.000-7`. Jamás se pide
   el RUT real para entrar, jamás se abre la ficha real, nada se escribe en
   Medilink. El banner "MODO DEMO · Datos ficticios" debe estar visible.
3. **Participación voluntaria y retirable.** Puede parar cuando quiera, sin dar
   explicaciones, sin perder la compensación y **sin ninguna consecuencia en su
   atención en el CMC**.
4. **Grabación opcional y separable.** Se puede participar sin grabar. Pantalla
   y voz se consienten por separado. Nunca se graba la cara.
5. **Ley 21.719** (protección de datos personales, Chile): el CMC es el
   responsable de datos; se informan finalidad, plazo de conservación y
   derechos de acceso, rectificación, supresión, oposición y portabilidad.
6. Notas y planillas se registran con **código de participante (P1–P5)**, nunca
   con nombre. La única hoja que une nombre↔código es el consentimiento firmado,
   que se guarda bajo llave en el CMC.
7. Grabaciones (si las hay) se **borran a los 30 días** de terminado el
   análisis; las notas anonimizadas se conservan como documentación del proyecto.

### 3.2 Texto completo del consentimiento (listo para imprimir y firmar)

> La versión diagramada para imprimir está en `static/protocolo-prueba-v5.html`
> (1 página). Este es el texto íntegro:

---

**CONSENTIMIENTO INFORMADO — Prueba de uso del nuevo portal del paciente**
**Centro Médico Carampangue** · Carampangue, comuna de Arauco

**¿Qué le estamos pidiendo?**
Le invitamos a probar una página nueva del Centro Médico Carampangue, pensada
para que los pacientes puedan ver sus horas, pedir citas y anotar sus
mediciones desde el celular. Queremos observar cómo la usa una persona real,
para encontrar y arreglar lo que resulte confuso **antes** de ofrecerla a todos
los pacientes.

**¿Qué va a pasar en la sesión?**
Durante 30 a 40 minutos, una persona del equipo le pedirá realizar algunas
acciones simples en la página usando un celular (el suyo o uno del centro),
mientras usted comenta en voz alta lo que va pensando. No es un examen ni una
atención médica: **aquí se evalúa la página, no a usted**. No hay respuestas
buenas ni malas, y equivocarse es justamente lo que más nos ayuda.

**Sus datos están protegidos.**
La prueba se hace en un **modo de demostración con datos inventados** (un
paciente ficticio). **No usaremos su ficha clínica real ni sus datos de salud**,
y nada de lo que haga en la prueba quedará registrado en su historial médico.
Las notas de la sesión se guardan con un código (por ejemplo "P3"), sin su
nombre. Conforme a la Ley N.º 21.719 de protección de datos personales, el
responsable del tratamiento es el Centro Médico Carampangue; los datos se usan
solo para mejorar el portal; y usted puede en cualquier momento pedir acceso,
rectificación o eliminación de sus datos, escribiendo al WhatsApp del centro
(+56 9 6661 0737) o llamando al (44) 296 5226.

**Grabación (opcional).**
Si usted lo autoriza, podemos grabar **la pantalla del celular** y/o **el
audio** de la conversación, solo para revisar los detalles después. Nunca se
graba su cara. Las grabaciones se eliminan a más tardar 30 días después del
análisis. Usted puede participar perfectamente **sin** grabación.

  ☐ SÍ autorizo grabar la **pantalla**  ☐ NO
  ☐ SÍ autorizo grabar la **voz**    ☐ NO

**Participación voluntaria.**
Su participación es completamente voluntaria. Puede hacer preguntas en
cualquier momento, saltarse cualquier actividad o **retirarse cuando quiera,
sin dar explicaciones**. Retirarse no afecta en nada su atención en el Centro
Médico Carampangue, y el agradecimiento comprometido se le entrega igual.

**Agradecimiento.**
Por su tiempo, recibirá: ______________________________________ (se entrega
aunque no complete la sesión).

Declaro que leí (o me leyeron en voz alta) este documento, que pude hacer
preguntas y que acepto participar.

| | |
|---|---|
| Nombre del participante: | _______________________________ |
| RUT: | _______________________________ |
| Firma: | _______________________________ |
| Fecha: | ____ / ____ / 2026 |
| Nombre y firma de quien modera: | _______________________________ |

*Se firma en dos copias: una para el participante y una para el centro.*

---

**Nota para el moderador:** si el participante tiene dificultad para leer, el
moderador **lee el consentimiento completo en voz alta, palabra por palabra**,
y lo consigna al margen ("leído en voz alta por el moderador").

---

## 4. Logística

### 4.1 Lugar, agenda y duración

- **Lugar:** un box desocupado del CMC (primera opción) o un rincón tranquilo
  de recepción fuera de horario punta. Requisitos: silla cómoda para el
  participante, mesa, buena luz, sin tránsito de gente, señal de celular/WiFi
  verificada **ese día en ese lugar**.
- **Duración:** 30–40 min por sesión. Agendar bloques de **60 min** (colchón de
  20 min para atrasos, conversación y reseteo del celular).
- **Máximo 2–3 sesiones por día**, con al menos 30 min entre sesiones.
- **Piloto obligatorio:** antes de la primera sesión real, correr la prueba
  completa con una funcionaria del CMC como "participante" (no cuenta para el
  n=5). Sirve para calibrar tiempos, el celular y el guion.

### 4.2 El celular

**Opción A — celular del participante (preferida, más realista):**
1. El moderador envía por WhatsApp, desde el número del centro
   (+56 9 6661 0737), el link `https://agentecmc.cl/portal/v5?demo=1`.
2. El participante lo abre tocando el link (así no tiene que tipear la URL).

**Opción B — celular del centro (si el suyo no sirve o no quiere usarlo):**
- Android de gama media, batería >80 %, brillo alto, fuente del sistema en
  tamaño normal (el A+ lo prueba el portal, no el sistema).
- Chrome con el portal demo **ya cargado** en `?demo=1`.
- WhatsApp configurado con la línea de prueba del centro, con el chat del CMC
  visible.
- Sin notificaciones ruidosas (modo No molestar, permitiendo WhatsApp).

**Simulación del código por WhatsApp (para T1):** en modo demo el código es
fijo (`123456`). Para que la tarea sea realista, **antes de iniciar T1** la
recepción envía al celular de prueba (o al del participante) un WhatsApp desde
el número del centro:

> Centro Médico Carampangue: su código de acceso al portal es **123456**. No lo
> comparta con nadie.

Así el participante vive el flujo real: cambiar a WhatsApp, leer el código,
volver al navegador y escribirlo.

**Reseteo entre sesiones:** cerrar sesión en el portal, recargar
`?demo=1`, borrar del chat el código anterior si corresponde, limpiar la
pantalla con toalla desinfectante, cargar batería.

### 4.3 Materiales (checklist para imprimir)

- ☐ 6 consentimientos impresos (5 + 1 repuesto), 2 copias c/u
- ☐ Guion del moderador impreso
- ☐ 6 tarjetas de tarea (T1–T5 + bonus) impresas en letra grande
- ☐ Tarjeta con el RUT de prueba `50.000.000-7` en letra grande
- ☐ Tarjeta con la escala SEQ 1–7 en letra grande
- ☐ 5 planillas de registro (1 por participante)
- ☐ Lápices de pasta (2)
- ☐ Celular de prueba cargado + cargador
- ☐ Acceso al WhatsApp del centro (para mandar link y código)
- ☐ Cronómetro (celular del moderador, en silencio)
- ☐ Vaso de agua para el participante
- ☐ Compensación lista para entregar (5 unidades)
- ☐ Toallas desinfectantes para el celular

### 4.4 Rol del moderador — guía de neutralidad

**Quién:** una sola persona modera y toma notas (equipo chico). Si hay una
segunda persona disponible, actúa de observadora en silencio, sentada fuera
del campo visual del participante.

**Reglas duras:**

1. **No ayudar salvo bloqueo total.** Escalera de intervención — usar siempre
   el peldaño más bajo posible, y anotar cada peldaño usado:
   - **Nivel 0 — silencio.** Esperar 10–15 s aunque duela. La mayoría se
     destraba sola.
   - **Nivel 1 — eco.** Repetir lo que dijo, como pregunta: *"¿No sabe dónde
     apretar?"*, *"¿Qué esperaría que pasara ahí?"*.
   - **Nivel 2 — recordar la meta.** *"Recuerde: lo que buscamos es pedir una
     hora de kinesiología."* (sin decir cómo).
   - **Nivel 3 — ayuda directa.** Solo si lleva >60 s totalmente bloqueado o lo
     pide 2 veces. Indicar el siguiente paso, uno solo. → la tarea pasa a
     **"éxito con ayuda"**.
   - **Abandono:** si aun con Nivel 3 no avanza o el participante se angustia,
     cerrar la tarea con amabilidad (*"perfecto, esto ya nos sirvió mucho"*) y
     marcar **"no logró"**. Nunca dejar a la persona macerándose en el error.
2. **No enseñar, no defender el diseño, no explicar "es que eso es porque…".**
   Toda explicación contamina las tareas siguientes.
3. **Preguntas devueltas:** si pregunta *"¿aprieto aquí?"*, responder *"¿usted
   qué cree? haga lo que haría en su casa"*.
4. **Lenguaje corporal neutro:** no asentir hacia la pantalla, no mirar el
   botón correcto, no suspirar.
5. **Think-aloud suave:** si deja de hablar >20 s, un empujón amable: *"¿qué
   está pensando?"* o *"¿qué está buscando ahora?"*. No exigirlo: con adultos
   mayores el think-aloud se sugiere, no se impone; si narra poco, observar y
   preguntar al final de la tarea qué le costó.
6. **Cronómetro discreto:** partir al terminar de leer la tarjeta de tarea;
   nunca comentar el tiempo (*"¡qué rápido!"* prohibido).
7. **Frustración:** si se disculpa o se pone nervioso, repetir la frase madre:
   *"Si esto cuesta, la culpa es de la página, no suya — y encontrarlo es
   exactamente lo que necesitamos."*
8. **Emergencias reales:** T5 es simulada. Decir SIEMPRE la frase de seguridad
   (ver tarjeta T5) y no dejar que se complete una llamada real al 131.

---

## 5. Guion del moderador (palabra por palabra)

> Trato de **usted**, chileno formal cercano. Leer tal cual; se permite ajustar
> el nombre y muletillas naturales, no el contenido.

### 5.1 Bienvenida (3 min)

> "Don/Señora [nombre], bienvenido(a), y muchas gracias por venir a ayudarnos.
> Yo soy [nombre del moderador] y trabajo con el equipo del centro.
>
> Le cuento en qué consiste esto. En el centro estamos preparando una página
> nueva para que los pacientes puedan ver sus horas, pedir citas y anotar sus
> mediciones desde el celular. Antes de ofrecérsela a todos, necesitamos que la
> prueben pacientes de verdad, como usted.
>
> Quiero dejarle algo muy claro desde ya: **hoy no lo(a) estamos evaluando a
> usted. Estamos evaluando la página.** Si algo le resulta difícil o confuso,
> la culpa es de la página, no suya — y encontrar esas cosas es exactamente
> para lo que lo(a) invitamos. Así que aquí no se puede 'echar a perder' nada,
> y no hay respuestas buenas ni malas.
>
> La sesión dura más o menos media hora. Le voy a pedir que haga algunas cosas
> en el celular, como pedir una hora de mentira, y yo voy a ir mirando y
> tomando notas. Todo se hace con un **paciente inventado**, con datos de
> mentira: no vamos a tocar su ficha ni sus datos reales para nada.
>
> Antes de partir, le voy a leer este documento que explica todo esto por
> escrito, y si está de acuerdo, lo firmamos."

*(Leer o dar a leer el consentimiento. Resolver preguntas. Marcar las casillas
de grabación según lo que decida. Firmar ambas copias, entregarle una.)*

*(Si autorizó grabación de pantalla: iniciar la grabación de pantalla del
celular ahora. Si autorizó voz: iniciar grabadora.)*

### 5.2 Calentamiento (3 min)

> "Antes de mirar la página, cuénteme un poquito:
>
> — ¿Usa harto el celular? ¿Para qué lo ocupa más?
> — ¿Y WhatsApp? ¿Con quién habla más por ahí?
> — Cuando necesita una hora médica con nosotros, ¿cómo la pide hoy?
> — ¿Alguna vez ha usado el celular para algo del banco, o para comprar algo?"

*(Anotar en la planilla: alfabetización digital observada, cómo pide horas hoy.)*

### 5.3 Instrucción de pensar en voz alta (2 min)

> "Ahora sí, vamos a la página. Le voy a pedir un favor especial: mientras use
> el celular, **vaya contándome en voz alta lo que va pensando**, como si
> estuviera solo(a) en su casa hablándose a sí mismo(a). Por ejemplo: 'aquí veo
> un botón verde… lo voy a apretar porque creo que es para pedir la hora… uy,
> no era lo que pensaba'. Todo eso a mí me sirve muchísimo.
>
> Otra cosa importante: como la que está a prueba es la página, **yo no le voy
> a poder ayudar** como le ayudaría un hijo o un nieto. Si se traba, intente
> arreglárselas como lo haría en su casa. Si de verdad no se puede, me dice y
> lo vemos, pero primero inténtelo usted. ¿Le parece?
>
> Y de nuevo: aquí no se puede echar nada a perder. Es todo de mentira. ¿Alguna
> pregunta antes de partir?"

### 5.4 Tareas (18–22 min)

*(Para cada tarea: leer la tarjeta palabra por palabra —el texto exacto está en
§6—, entregar la tarjeta impresa, partir el cronómetro al terminar de leer,
observar y anotar. Al terminar cada tarea, hacer la pregunta SEQ:)*

> "En una escala de 1 a 7, donde 1 es 'muy difícil' y 7 es 'muy fácil',
> ¿qué tan fácil o difícil le resultó esto que acaba de hacer?"

*(Mostrar la tarjeta de la escala. Anotar el número. Luego:)*

> "¿Qué fue lo que más le costó de esta parte?"

*(Una frase, anotarla textual si se puede. No discutir, no explicar. Pasar a la
siguiente tarea.)*

### 5.5 Cuestionario final y preguntas abiertas (5 min)

> "Ya terminamos con las tareas. Lo hizo muy bien y nos ayudó un montón. Ahora
> le voy a leer cuatro frases, y usted me dice qué tan de acuerdo está con cada
> una, de 1 a 5: 1 es 'muy en desacuerdo' y 5 es 'muy de acuerdo'."

*(Leer los 4 ítems del SUS-corto —§7.4— y anotar. Luego:)*

> "Y para terminar, tres preguntas de conversación:
>
> 1. ¿Qué fue lo que **más le gustó** de la página?
> 2. ¿Qué fue lo **más difícil o confuso**?
> 3. Si pudiera cambiar **una sola cosa** de la página, ¿cuál sería? ¿Y se la
>    recomendaría a un vecino o vecina de su edad?"

### 5.6 Cierre (2 min)

> "Eso era todo. De verdad, muchas gracias: lo que usted hizo hoy va a hacer
> que esta página sea más fácil para todos los pacientes del centro. Aquí está
> [compensación], como le habíamos dicho, y su copia del consentimiento.
>
> Cuando la página esté lista de verdad, ¿le gustaría que le avisáramos por
> WhatsApp para que sea de los primeros en usarla?"

*(Anotar sí/no. Detener grabaciones. Acompañar a la salida. Resetear el celular
antes de la siguiente sesión.)*

---

## 6. Las 5 tareas (+ bonus)

Formato de cada tarea: **escenario narrado** (se lee tal cual y se entrega
impreso en letra grande), **criterio de éxito** binario y su variante con
ayuda, **límite de tiempo**, y **qué observar**.

Niveles de resultado (los mismos en toda la prueba):
- **Éxito solo:** completa el criterio sin ninguna intervención de Nivel 3.
- **Éxito con ayuda:** completa el criterio, pero necesitó ≥1 ayuda directa (Nivel 3).
- **No logró:** no completa el criterio dentro del tiempo tope, o abandona.

---

### T1 — Entrar al portal con el código

**Preparación:** portal en la pantalla de inicio de sesión (`?demo=1`).
Recepción ya envió al WhatsApp del celular el mensaje con el código `123456`.
Entregar la tarjeta con el RUT de prueba.

**Escenario (leer tal cual):**

> "Imagine que el centro le mandó un mensaje contándole de esta página nueva, y
> usted quiere entrar a verla. Para esta prueba usamos un paciente inventado:
> su RUT es este que está en la tarjeta [entregar: 50.000.000-7]. Entre al
> portal con ese RUT. El código para entrar le va a llegar por WhatsApp, a este
> mismo celular."

**Criterio de éxito:** llega al home del portal con la sesión iniciada (ve el
saludo y su próxima cita). — **Con ayuda:** lo mismo, con ≥1 ayuda directa.
**Tiempo:** objetivo <3 min (H1) · tope 5 min.

**Qué observar:**
- ¿Entiende el campo RUT? ¿Se enreda con puntos y guion?
- ¿Encuentra y entiende el botón **"Enviar código por WhatsApp"**?
- El cambio de aplicación: navegador → WhatsApp → volver al navegador (punto de
  quiebre clásico en 60+; anotar cómo lo resuelve: memoriza el código, copia y
  pega, va y vuelve varias veces, o se pierde en WhatsApp).
- ¿Escribe los 6 dígitos sin problema? ¿Usa "Reenviar código" si se confunde?
- ¿Lee el banner MODO DEMO? ¿Le genera dudas?
- Frases textuales de confusión.

---

### T2 — ¿Cuándo y con quién es su próxima hora?

**Preparación:** continúa desde T1, home a la vista (si T1 falló, el moderador
deja la sesión iniciada antes de leer T2).

**Escenario (leer tal cual):**

> "Ya está adentro de la página. Ahora dígame, mirando el celular: ¿cuándo es
> su próxima hora en el centro, a qué hora, y con quién le toca?"

**Criterio de éxito:** dice correctamente **fecha, hora y profesional** tal
como aparecen en el home demo (el moderador los verifica contra la pantalla).
— **Con ayuda:** los dice tras ayuda directa (p. ej., indicarle dónde mirar).
**Tiempo:** objetivo <1 min (H2) · tope 2 min.

**Qué observar:**
- ¿Lo ve de inmediato en el bloque principal o se pone a navegar?
- ¿Confunde fecha con hora, o la especialidad con el nombre del profesional?
- ¿Entiende las indicaciones extra ("llegue 10 minutos antes…")?
- ¿El tamaño de letra le resulta suficiente? (si se acerca el teléfono a la
  cara o lo aleja, anotarlo — alimenta la tarea bonus).

---

### T3 — Pedir una hora de kinesiología para la próxima semana

**Escenario (leer tal cual):**

> "Ahora imagine que hace unos días le empezó a molestar una rodilla, y su
> médico le dijo que necesita **kinesiología**. Usando la página, pida una hora
> de kinesiología para la **próxima semana**, en el día y horario que más le
> acomoden. Recuerde que es todo de mentira: la hora no queda tomada de verdad."

**Criterio de éxito:** completa el asistente paso a paso (especialidad →
profesional → día/hora → revisar → **confirmar**) y llega a la pantalla de
confirmación, con una hora de kinesiología en fecha de la próxima semana.
— **Con ayuda:** lo mismo, con ≥1 ayuda directa.
**Tiempo:** objetivo <4 min (H3) · tope 6 min.

**Qué observar:**
- ¿Encuentra "Pedir una hora" desde el home?
- Paso especialidad: ¿encuentra kinesiología en la lista? ¿entiende el término?
- Paso profesional: ¿entiende que puede elegir o duda ("¿cuál será bueno?")?
- Paso día/hora: **punto crítico** — ¿entiende el selector de días? ¿logra
  moverse a la próxima semana o toma la primera hora que ve, aunque sea de esta
  semana? (si toma una de esta semana: anotar, cuenta como desvío, no como
  fallo, si él cree que es "la próxima semana" — la confusión es hallazgo).
- Pantalla revisar: ¿la lee o pasa de largo? ¿detectaría un error ahí?
- Tras confirmar: ¿sabe que la hora quedó tomada? Preguntar al terminar:
  *"¿quedó tomada su hora? ¿cómo lo sabe?"*
- Botón "atrás": ¿lo usa? ¿lo saca del asistente sin querer?

---

### T4 — Anotar la presión de esta mañana

**Escenario (leer tal cual):**

> "Ahora otra cosa. Imagine que esta mañana usted se tomó la presión en su casa
> y le salió **135 la alta y 85 la baja**. Deje anotada esa medición en la
> página, para que quede en su registro."

**Criterio de éxito:** guarda una medición de presión con sistólica 135 y
diastólica 85 (visible en el historial de mediciones). — **Con ayuda:** lo
mismo, con ≥1 ayuda directa.
**Tiempo:** objetivo <3 min (H4) · tope 5 min.

**Qué observar:**
- ¿Encuentra "Registrar medición" desde el home?
- ¿Selecciona bien el tipo "Presión" (vs glicemia/peso/temperatura)?
- ¿Sabe cuál número va en qué campo ("la alta" vs "la baja")? ¿Las etiquetas de
  los campos se lo aclaran o hay que adivinarlo?
- Teclado numérico: ¿le aparece? ¿tipea con seguridad?
- Al guardar: ¿entiende el mensaje de clasificación de la presión? ¿Lo
  tranquiliza, lo asusta o lo ignora? (135/85 es levemente elevada — anotar la
  reacción textual).
- ¿Verifica que quedó guardada?

---

### T5 — Dolor fuerte en el pecho (seguridad — criterio duro)

**IMPORTANTE — frase de seguridad, decirla SIEMPRE antes del escenario:**

> "La que viene es distinta, y ojo: es **pura imaginación**, aquí nadie está
> enfermo y **no vamos a llamar a nadie de verdad**. Si llega a una pantalla de
> llamada, me la muestra pero **no aprieta el botón verde de llamar**. ¿Ya?"

**Escenario (leer tal cual):**

> "Imagine que está en su casa, solo(a), y de repente siente un **dolor fuerte
> y apretado en el pecho**, que no se le pasa. Tiene el celular en la mano, con
> esta página abierta. Muéstreme qué haría."

**Criterio de éxito:** llega a la acción **"llamar al SAMU 131"** — por
cualquiera de estas vías: (a) la sección "Banderas rojas — cuándo pedir ayuda"
y su botón 131, (b) el enlace SAMU 131 del pie, o (c) verbaliza espontánea e
inequívocamente "yo aquí llamo a la ambulancia / al 131" (la conducta segura
cuenta como éxito aunque no use el portal). — **Con ayuda:** llega al 131 solo
tras ayuda directa. — **Se registra como hallazgo grave** (aunque técnicamente
sea "éxito con ayuda o desvío"): tocar el botón **"Llamar"** del portal creyendo
que corresponde en una emergencia — ese botón llama al **centro** ((44) 296
5226), no al SAMU; si esto pasa, anotar textual qué esperaba que ocurriera.
**Tiempo:** objetivo <1 min (H5) · tope 2 min.

**Qué observar:**
- Primera reacción: ¿portal, WhatsApp, botón Llamar, banderas rojas, o "llamaría
  a mi hija"? (anotar el orden de las reacciones).
- ¿Distingue "Llamar" (al centro) de "SAMU 131"? — esta distinción es el
  corazón de la tarea.
- ¿Encuentra la sección de banderas rojas? ¿Entiende para qué sirve?
- ¿El rojo/urgencia del banner SAMU comunica de inmediato?
- **Bajo ningún motivo dejar que se complete una llamada real al 131.**

*Después de cerrar la tarea, descomprimir:* "Muy bien. Y en la vida real, ojo:
dolor fuerte al pecho es 131 al tiro, tal como usted [hizo/vio aquí]."

---

### Tarea BONUS — Agrandar la letra (si queda tiempo y ánimo)

**Escenario (leer tal cual):**

> "Última cosita, cortita. Imagine que la letra de la página le parece muy
> chica para leerla cómodo. Haga que la página se vea con la **letra más
> grande**."

**Criterio de éxito:** usa el botón **A+** de la barra superior y la letra
aumenta visiblemente. — Sin límite estricto: tope 2 min y se cierra con
amabilidad. No entra en las hipótesis; alimenta hallazgos de accesibilidad.

**Qué observar:** ¿nota el botón A+ por sí solo? ¿entiende el ícono "A+"? ¿usa
en cambio el gesto de pellizcar/zoom del sistema? (si pellizca: hallazgo — el
botón no se descubre).

---

## 7. Métricas

### 7.1 Éxito de tarea (3 niveles)

Por tarea y participante: **Éxito solo / Éxito con ayuda / No logró**
(definiciones en §6). Para las hipótesis H1–H5 solo cuenta **"éxito solo"
dentro del tiempo objetivo** (en H5, la vía verbal segura cuenta — ver T5).

### 7.2 Tiempo en tarea

Desde que el moderador termina de leer el escenario hasta que se cumple el
criterio de éxito (o tope). Registrar en mm:ss. Si hubo ayudas, registrar el
tiempo igual y marcar las ayudas aparte.

### 7.3 Errores, desvíos y ayudas

- **Error:** acción que aleja del objetivo (toca el botón equivocado, entra a
  otra sección, borra lo escrito). Contar y describir en una frase.
- **Desvío:** llega al objetivo por un camino no previsto (no se penaliza,
  se anota — a veces el camino "no previsto" es el bueno).
- **Ayudas:** número de intervenciones Nivel 3 del moderador.

### 7.4 SEQ y SUS-corto adaptado

**SEQ (después de CADA tarea)** — fraseo adaptado, con tarjeta visual 1–7:

> "De 1 a 7, donde 1 es 'muy difícil' y 7 es 'muy fácil', ¿qué tan fácil o
> difícil le resultó [entrar a la página / encontrar su hora / pedir la hora /
> anotar su presión / saber qué hacer con el dolor de pecho]?"

**SUS-corto adaptado a adultos mayores (al final, 4 frases, escala 1–5,
1 = muy en desacuerdo, 5 = muy de acuerdo, se leen en voz alta):**

1. "Me gustaría usar esta página para mis horas y mis controles."
2. "La página me pareció fácil de usar."
3. "Creo que necesitaría que alguien me ayudara cada vez que quiera usarla." **(inversa)**
4. "Usándola me sentí seguro(a) y en confianza."

**Puntaje:** ítems 1, 2 y 4 valen su número; el ítem 3 se invierte (6 − valor).
Total por participante: 4–20. **Meta: promedio ≥16.**

### 7.5 Tres preguntas abiertas (al final)

1. ¿Qué fue lo que más le gustó de la página?
2. ¿Qué fue lo más difícil o confuso?
3. Si pudiera cambiar una sola cosa, ¿cuál sería? ¿Se la recomendaría a un
   vecino o vecina de su edad?

Anotar respuestas lo más textuales posible (las citas literales valen oro en
el informe).

---

## 8. Planilla de registro por participante

> Versión diagramada e imprimible (1 página, con casillas) en
> `static/protocolo-prueba-v5.html`. Imprimir 5 copias.

**PLANILLA DE REGISTRO — Prueba portal v5 · Participante P___**

| Campo | Registro |
|---|---|
| Fecha / hora sesión | ____/____/2026 · ____:____ |
| Moderador | ______________________ |
| Edad / género | ______ / ______ |
| Alfabetización digital (declarada) | ☐ Baja (solo WhatsApp) ☐ Media/alta |
| Cómo pide horas hoy | ☐ Presencial ☐ Teléfono ☐ WhatsApp/bot ☐ Otro: ______ |
| Celular usado | ☐ Propio ☐ Del centro |
| Grabación | Pantalla ☐ Sí ☐ No · Voz ☐ Sí ☐ No |
| Consentimiento firmado | ☐ Sí |

**Tareas:**

| Tarea | Resultado | Tiempo | Ayudas (N3) | Errores (n) | SEQ (1–7) |
|---|---|---|---|---|---|
| T1 Entrar con código | ☐ Solo ☐ Con ayuda ☐ No logró | ___:___ | ___ | ___ | ___ |
| T2 Próxima cita | ☐ Solo ☐ Con ayuda ☐ No logró | ___:___ | ___ | ___ | ___ |
| T3 Agendar kine | ☐ Solo ☐ Con ayuda ☐ No logró | ___:___ | ___ | ___ | ___ |
| T4 Presión 135/85 | ☐ Solo ☐ Con ayuda ☐ No logró | ___:___ | ___ | ___ | ___ |
| T5 Dolor pecho → 131 | ☐ Solo ☐ Con ayuda ☐ No logró | ___:___ | ___ | ___ | ___ |
| Bonus letra A+ | ☐ Solo ☐ Con ayuda ☐ No logró ☐ No se hizo | ___:___ | ___ | ___ | — |

**T5 — vía usada:** ☐ Banderas rojas→131 ☐ Pie SAMU 131 ☐ Verbal "llamo al 131"
☐ **Tocó "Llamar" (centro) creyendo que era emergencia** ☐ Otro: __________

**Observaciones por tarea** (errores textuales, citas, confusiones):
T1 ________________________________________________________________
T2 ________________________________________________________________
T3 ________________________________________________________________
T4 ________________________________________________________________
T5 ________________________________________________________________

**SUS-corto (1–5):** Ítem 1 ___ · Ítem 2 ___ · Ítem 3 ___ (invertir: 6−x) ·
Ítem 4 ___ · **Total (4–20): ___**

**Abiertas:**
Lo que más le gustó: ______________________________________________
Lo más difícil/confuso: ___________________________________________
Cambiaría una cosa / ¿recomendaría?: ______________________________

**Cita textual de la sesión (la mejor):** "___________________________"

**¿Quiere que le avisemos cuando esté lista?** ☐ Sí ☐ No

---

## 9. Análisis

### 9.1 Verificación de hipótesis

Al terminar las 5 sesiones, volcar las 5 planillas a una tabla resumen y
evaluar cada hipótesis contra su umbral (§1.2). Regla de decisión:

- **GO (lanzar a un grupo piloto de pacientes reales):**
  - H5 cumplida al 100 % (5/5), **y**
  - se cumplen al menos 3 de las otras 4 hipótesis, **y**
  - cero hallazgos "bloqueantes" sin corrección comprometida.
- **AJUSTAR y re-testear:** cualquier otro caso. Corregir los hallazgos de las
  tareas fallidas y repetir **solo esas tareas** con 3 participantes nuevos
  (pueden reclutarse con el mismo guion).
- Un fallo de H5 **siempre** manda a AJUSTAR, aunque todo lo demás esté verde.

### 9.2 Priorización de hallazgos: frecuencia × severidad

Cada problema observado se registra una vez como **hallazgo** con:

- **Frecuencia (F):** número de participantes afectados (1–5).
- **Severidad (S):**
  - **4 — Bloqueante:** impide completar la tarea o compromete la seguridad
    (todo lo que toque T5 con riesgo parte en 4).
  - **3 — Grave:** se completa la tarea, pero con gran esfuerzo, ayuda o error
    con consecuencias (p. ej., agendó en la semana equivocada sin darse cuenta).
  - **2 — Moderado:** fricción clara, la resuelve solo (dudas, idas y vueltas).
  - **1 — Leve:** cosmético o de preferencia.

**Puntaje = F × S** (rango 1–20). Reglas de acción:

| Puntaje | Acción |
|---|---|
| ≥ 12, o cualquier S=4 | Corregir **antes** del lanzamiento (bloquea el GO) |
| 6 – 11 | Corregir en el primer sprint post-lanzamiento |
| ≤ 5 | Backlog |

### 9.3 Plantilla de informe de 1 página

```
INFORME — PRUEBA DE USABILIDAD PORTAL v5 · CMC
Fechas: __/__ al __/__ de ____ · Participantes: 5 (edades __–__, _M/_F,
_ alfabetización baja) · Moderador: ________

VEREDICTO: ☐ GO  ☐ AJUSTAR Y RE-TESTEAR
Motivo en una frase: _________________________________________

HIPÓTESIS                     RESULTADO      ¿CUMPLE?
H1 Entrar solos <3min          _/5            ☐ Sí ☐ No
H2 Próxima cita <1min          _/5            ☐ Sí ☐ No
H3 Agendar solos <4min         _/5            ☐ Sí ☐ No
H4 Presión solos <3min         _/5            ☐ Sí ☐ No
H5 Dolor pecho → 131 <1min     _/5            ☐ Sí ☐ No  ← 5/5 obligatorio
SEQ promedio: _._ /7 · SUS-corto promedio: __ /20

TOP 5 HALLAZGOS (F×S, de mayor a menor)
1. [S_] [F_] ____________________________________ → fix: ______
2. [S_] [F_] ____________________________________ → fix: ______
3. [S_] [F_] ____________________________________ → fix: ______
4. [S_] [F_] ____________________________________ → fix: ______
5. [S_] [F_] ____________________________________ → fix: ______

LO QUE FUNCIONÓ (no tocar): _________________________________

3 CITAS TEXTUALES:
"_____________" (P_) · "_____________" (P_) · "_____________" (P_)

PRÓXIMOS PASOS Y RESPONSABLES:
1. ______________________ (quien: ____, cuándo: ____)
2. ______________________ (quien: ____, cuándo: ____)
3. ______________________ (quien: ____, cuándo: ____)
```

---

## 10. Cronograma sugerido

| Día | Actividad | Detalle |
|---|---|---|
| **Día 0** (½ día) | Preparación + piloto | Definir compensación (dueño). Imprimir materiales (`static/protocolo-prueba-v5.html`). Preparar celular de prueba y WhatsApp. **Piloto con una funcionaria** (no cuenta en n=5); ajustar guion/tiempos. Reclutar y agendar los 5. |
| **Día 1** | Sesiones P1 y P2 | Mañana y tarde. Volcar planillas el mismo día (memoria fresca). |
| **Día 2** | Sesiones P3 y P4 | Ídem. Si un patrón grave se repite en 3+ participantes, se puede corregir algo menor entre sesiones **solo si no invalida la comparación** (anotarlo). |
| **Día 3** | Sesión P5 + colchón | El colchón absorbe inasistencias de días 1–2. |
| **Día 4** | Análisis | Tabla resumen, verificación H1–H5, matriz F×S, informe de 1 página. |
| **Día 5** | Decisión | Reunión con el dueño: **GO / AJUSTAR**, lista de correcciones con responsable y fecha. Si GO: definir grupo piloto real y mensaje de invitación. |

**Recordatorio final de higiene del estudio:** entre sesiones, resetear el
portal a `?demo=1` sin sesión, borrar el código del chat, cargar y limpiar el
celular, y revisar que el banner **MODO DEMO** aparezca antes de sentar al
siguiente participante.
