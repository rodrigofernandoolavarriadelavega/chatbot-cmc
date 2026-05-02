"""
Generador de blogs por especialidad — Wave 2.
Agrega 13 especialidades nuevas: ORL, Traumatología, Ginecología,
Gastroenterología, Endodoncia, Implantología, Masoterapia, Nutrición,
Psicología Adulto, Psicología Infantil, Fonoaudiología, Matrona, Podología.

Uso: python3 scripts/gen_blogs_specialty_v2.py
"""
import sys
from pathlib import Path

# Reusa helpers + build_blog de gen_blogs_specialty.py
sys.path.insert(0, str(Path(__file__).parent))
from gen_blogs_specialty import (
    build_blog, BLOG_DIR, ARROW, FAQ_CHEVRON, INFO_ICON, WA_SVG,
    make_cta_inline, callout_info, faq_item,
)


# ===================== OTORRINOLARINGOLOGÍA =====================

ORL_BODY = f'''
        <h2>¿Qué es la <em>Otorrinolaringología</em>?</h2>
        <p>La otorrinolaringología (ORL) es la especialidad que trata las enfermedades del oído, nariz y garganta. En Carampangue muchas personas viajan a Concepción por estos temas — en CMC los resolvemos cerca de casa con el <strong>Dr. Manuel Borrego</strong>, especialista que coordina fechas según demanda.</p>

        {callout_info('<strong>Lavado de oídos:</strong> es uno de los procedimientos más solicitados. Cuesta $10.000 y se hace en la misma consulta ($35.000), no en lugar de ella.')}

        <h2>¿Cuándo consultar al <em>otorrino</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg></div><div class="body"><strong>Sensación de oído tapado</strong><span>Audición disminuida, zumbidos o sensación de líquido. Suele ser tapón de cerumen o problema en oído medio.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 2"/></svg></div><div class="body"><strong>Vértigo o mareos</strong><span>Sensación de que el mundo gira. Puede deberse al oído interno (vértigo posicional, Ménière).</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11h.01"/><path d="M15 11h.01"/><path d="M12 17a3 3 0 0 1-3-3"/><circle cx="12" cy="12" r="10"/></svg></div><div class="body"><strong>Congestión nasal crónica</strong><span>Nariz tapada por más de 3 meses, ronquidos, dificultad para respirar de noche. Puede ser tabique desviado o pólipos.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2v4"/><path d="M16 2v4"/><rect x="3" y="4" width="18" height="18" rx="2"/></svg></div><div class="body"><strong>Dolor de garganta a repetición</strong><span>Amigdalitis frecuentes (3+ episodios/año). Evaluación para definir si requiere amigdalectomía.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3z"/></svg></div><div class="body"><strong>Pérdida de audición</strong><span>Subir el volumen del TV, pedir que repitan. Audiometría confirma el diagnóstico ($25.000).</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19.5 6.5L18 8m-12 4l8-8 4 4-8 8z"/></svg></div><div class="body"><strong>Voz ronca persistente</strong><span>Disfonía por más de 2 semanas. Puede ser nódulos, pólipos o reflujo. Evaluación con laringoscopia.</span></div></div>
        </div>

        <h2>Procedimientos en <em>la consulta</em></h2>
        <p>El Dr. Borrego trae el equipo necesario para resolver muchos temas en la misma cita:</p>
        <ul>
          <li><strong>Otoscopia</strong> — revisión visual del conducto auditivo y tímpano.</li>
          <li><strong>Lavado de oídos</strong> ($10.000) — extracción de cerumen acumulado por irrigación.</li>
          <li><strong>Audiometría</strong> ($25.000) — examen de audición en cabina silente, ~20 min.</li>
          <li><strong>Rinoscopia</strong> — evaluación de la cavidad nasal con espéculo o endoscopio.</li>
          <li><strong>Receta de tratamiento</strong> y derivación a cirugía si corresponde.</li>
        </ul>

        {make_cta_inline('Otorrinolaringología', 'Agenda tu hora con el [Otorrino]', 'Consulta $35.000 particular. Lavado oídos $10.000 adicional. Audiometría $25.000. WhatsApp 24/7.', 'quiero%20agendar%20una%20hora%20de%20Otorrinolaringolog%C3%ADa.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta la consulta de otorrino?', '<strong>$35.000 particular</strong>. La especialidad no acepta Fonasa. El lavado de oídos es $10.000 adicional dentro de la misma consulta y la audiometría $25.000.')}
          {faq_item('¿Atienden niños?', 'Sí. El Dr. Borrego atiende niños desde aproximadamente los 4 años: amigdalitis recurrente, otitis a repetición, ronquidos.')}
          {faq_item('¿Cuándo viene el otorrino?', 'Coordina fechas mes a mes según demanda. Escribe por WhatsApp para que te avisemos las próximas disponibles.')}
          {faq_item('¿Hacen cirugía de tabique?', 'No en CMC, pero el Dr. Borrego evalúa, indica TAC y deriva a cirugía en Concepción si corresponde.')}
          {faq_item('¿Sirve traer audiometría externa?', 'Sí, tráela. Si tienes una reciente (menos de 1 año) puede evitar repetir el examen.')}
          {faq_item('Tengo zumbido en los oídos hace meses. ¿Es grave?', 'El tinnitus tiene muchas causas (cerumen, presión arterial, daño auditivo, estrés). El otorrino lo evalúa y orienta el tratamiento. No lo dejes pasar.')}
        </div>'''

ORL_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta la consulta de otorrino?","acceptedAnswer":{"@type":"Answer","text":"$35.000 particular. No acepta Fonasa. Lavado de oídos $10.000 adicional, audiometría $25.000."}},
    {"@type":"Question","name":"¿Atienden niños en otorrino?","acceptedAnswer":{"@type":"Answer","text":"Sí, desde aproximadamente los 4 años: amigdalitis, otitis recurrentes, ronquidos."}},
    {"@type":"Question","name":"¿Cuándo viene el otorrino?","acceptedAnswer":{"@type":"Answer","text":"Coordina fechas mes a mes según demanda. Avisamos por WhatsApp."}},
    {"@type":"Question","name":"¿Hacen cirugía de tabique?","acceptedAnswer":{"@type":"Answer","text":"No en CMC. Evaluamos, indicamos TAC y derivamos a Concepción si corresponde."}},
    {"@type":"Question","name":"¿Sirve traer audiometría externa?","acceptedAnswer":{"@type":"Answer","text":"Sí. Si tienes una reciente (menos de 1 año) puede evitar repetirla."}}'''

ORL_RELATED = f'''
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/fonoaudiologia">{ARROW} Fonoaudiología</a></li>
            <li><a href="/blog/cardiologia">{ARROW} Cardiología</a></li>
            <li><a href="/blog/kinesiologia">{ARROW} Kinesiología</a></li>
            <li><a href="/blog/ecografia">{ARROW} Ecografía</a></li>
          '''


# ===================== TRAUMATOLOGÍA =====================

TRAU_BODY = f'''
        <h2>¿Qué es la <em>Traumatología</em>?</h2>
        <p>La traumatología es la especialidad que trata lesiones y enfermedades del sistema musculoesquelético: huesos, articulaciones, ligamentos, músculos y tendones. En el CMC atiende el <strong>Dr. Claudio Barraza</strong>, especialista que viene desde Concepción a coordinar fechas mes a mes según demanda.</p>

        {callout_info('<strong>Después de la consulta, kinesiología:</strong> la mayoría de las patologías traumatológicas se complementan con sesiones de kinesiología para acelerar la recuperación. Esto sí lo cubre el bono Fonasa $7.830 en el mismo CMC.')}

        <h2>¿Cuándo consultar al <em>traumatólogo</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11h.01"/><path d="M15 11h.01"/><circle cx="12" cy="12" r="10"/></svg></div><div class="body"><strong>Dolor de rodilla persistente</strong><span>Más de 2 semanas, con o sin hinchazón. Puede ser meniscos, ligamentos o artrosis incipiente.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v8"/><path d="m4.93 10.93 1.41 1.41"/><path d="M2 18h2"/><path d="M20 18h2"/></svg></div><div class="body"><strong>Dolor lumbar crónico</strong><span>Lumbago que no cede con reposo y kinesiología. Evaluación de hernia discal, ciática.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg></div><div class="body"><strong>Dolor de hombro</strong><span>Limitación al levantar el brazo, sobre todo al peinarse o vestirse. Lesiones del manguito rotador.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s-8-4-8-12A8 8 0 0 1 12 2a8 8 0 0 1 8 8c0 8-8 12-8 12z"/></svg></div><div class="body"><strong>Lesiones deportivas</strong><span>Esguinces de tobillo, lesiones de ligamentos, fascitis plantar, tendinitis.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></div><div class="body"><strong>Fracturas o post-cirugía</strong><span>Control después de fractura, yeso retirado o cirugía. Indicación de kinesiología.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div><div class="body"><strong>Artrosis y dolor articular</strong><span>Manos, caderas, rodillas. Evaluación, manejo del dolor e indicaciones para conservar movilidad.</span></div></div>
        </div>

        <h2>Lo que <em>resuelve</em> el traumatólogo</h2>
        <ul>
          <li><strong>Diagnóstico clínico</strong> — examen físico y revisión de tu historia.</li>
          <li><strong>Indicación de exámenes</strong> — radiografías, resonancia magnética, ecografía musculoesquelética.</li>
          <li><strong>Infiltraciones</strong> — corticoides o ácido hialurónico en articulaciones cuando corresponde.</li>
          <li><strong>Indicación de kinesiología</strong> — receta de sesiones para que sigas en CMC con bono Fonasa.</li>
          <li><strong>Derivación a cirugía</strong> — si requiere artroscopía u otra intervención, derivamos a Concepción.</li>
        </ul>

        {make_cta_inline('Traumatología', 'Agenda tu hora con el [Traumatólogo]', 'Consulta $35.000 particular. Mes a mes según disponibilidad del especialista. WhatsApp 24/7.', 'quiero%20agendar%20una%20hora%20de%20Traumatolog%C3%ADa.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta la consulta de traumatología?', '<strong>$35.000 particular</strong>. La especialidad no acepta Fonasa. Si después necesitas kinesiología, esa sí tiene bono Fonasa $7.830.')}
          {faq_item('¿Cuándo viene el traumatólogo?', 'El Dr. Barraza coordina fechas mes a mes según demanda. Escribe por WhatsApp para que te avisemos las próximas disponibles.')}
          {faq_item('¿Necesito traer radiografías?', 'Si tienes radiografías o resonancia recientes (últimos 3 meses), tráelas. Si no, el doctor te las indica en la consulta.')}
          {faq_item('¿Hacen infiltraciones?', 'Sí, el Dr. Barraza hace infiltraciones de rodilla, hombro y otras articulaciones cuando hay indicación. El valor se cotiza según el caso.')}
          {faq_item('¿Atienden niños?', 'En general el Dr. Barraza atiende adolescentes y adultos. Para niños menores derivamos a traumatología infantil en Concepción.')}
          {faq_item('Mi rodilla cruje pero no duele. ¿Tengo que consultar?', 'Si no duele ni limita el movimiento, no es urgente. Pero si aparece dolor o hinchazón, consulta. Una evaluación temprana puede prevenir lesiones mayores.')}
        </div>'''

TRAU_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta la consulta de traumatología?","acceptedAnswer":{"@type":"Answer","text":"$35.000 particular. No acepta Fonasa. La kinesiología posterior sí tiene bono Fonasa $7.830."}},
    {"@type":"Question","name":"¿Cuándo viene el traumatólogo?","acceptedAnswer":{"@type":"Answer","text":"Coordina fechas mes a mes según demanda. Avisamos por WhatsApp."}},
    {"@type":"Question","name":"¿Hacen infiltraciones?","acceptedAnswer":{"@type":"Answer","text":"Sí, infiltraciones de rodilla, hombro y otras articulaciones cuando hay indicación. Valor se cotiza según el caso."}},
    {"@type":"Question","name":"¿Atienden niños en traumatología?","acceptedAnswer":{"@type":"Answer","text":"En general adolescentes y adultos. Para niños derivamos a traumatología infantil en Concepción."}},
    {"@type":"Question","name":"¿Necesito traer radiografías?","acceptedAnswer":{"@type":"Answer","text":"Si tienes radiografías o resonancia recientes, tráelas. Si no, el doctor te las indica en la consulta."}}'''

TRAU_RELATED = f'''
            <li><a href="/blog/kinesiologia">{ARROW} Kinesiología</a></li>
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/masoterapia">{ARROW} Masoterapia</a></li>
            <li><a href="/blog/ecografia">{ARROW} Ecografía</a></li>
            <li><a href="/blog/podologia">{ARROW} Podología</a></li>
          '''


# ===================== GINECOLOGÍA =====================

GINE_BODY = f'''
        <h2>¿Qué es la <em>Ginecología</em>?</h2>
        <p>La ginecología es la especialidad médica que cuida la salud sexual y reproductiva de la mujer. En el CMC atiende el <strong>Dr. Tirso Rejón</strong>, ginecólogo-obstetra que también realiza <strong>ecografías ginecológicas y obstétricas</strong> (transvaginal, abdominal, doppler, control embarazo) en la misma consulta.</p>

        {callout_info('<strong>Ginecología vs Matrona:</strong> para control sano, anticoncepción y PAP la matrona es excelente y más accesible (Fonasa preferencial $16.000). El ginecólogo es para problemas más complejos: dolor pélvico, miomas, infertilidad, ecografías especializadas.')}

        <h2>¿Cuándo consultar al <em>ginecólogo</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div><div class="body"><strong>Dolor pélvico crónico</strong><span>Dolor que no cede con analgésicos comunes. Endometriosis, miomas, quistes ováricos.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/></svg></div><div class="body"><strong>Sangrado anormal</strong><span>Reglas muy abundantes, irregulares, sangrado entre reglas o postmenopáusico. Requiere evaluación.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v8"/><path d="m4.93 10.93 1.41 1.41"/><path d="M22 22H2"/></svg></div><div class="body"><strong>Control de embarazo</strong><span>Ecografías mensuales, control prenatal, derivación a maternidad cuando corresponde.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11h.01"/><path d="M15 11h.01"/><path d="M12 17a3 3 0 0 1-3-3"/><circle cx="12" cy="12" r="10"/></svg></div><div class="body"><strong>Infertilidad</strong><span>Si no logras embarazo después de 1 año intentando (o 6 meses si tienes más de 35 años).</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 2"/></svg></div><div class="body"><strong>Climaterio y menopausia</strong><span>Bochornos, alteraciones del sueño, cambios de ánimo. Manejo médico individualizado.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/></svg></div><div class="body"><strong>Quistes ováricos / miomas</strong><span>Detección, seguimiento ecográfico y manejo. Derivación a cirugía si es necesario.</span></div></div>
        </div>

        <h2>Ecografías que realiza el Dr. Rejón</h2>
        <p>El Dr. Rejón hace en la misma consulta las ecografías especializadas:</p>
        <ul>
          <li><strong>Ecografía transvaginal</strong> ($35.000) — evalúa útero, ovarios, miomas, quistes.</li>
          <li><strong>Ecografía abdominal ginecológica</strong> — útero y ovarios por vía abdominal.</li>
          <li><strong>Ecografía obstétrica</strong> — control de embarazo en cada trimestre.</li>
          <li><strong>Ecografía Doppler ginecológica</strong> — flujo de arterias uterinas.</li>
        </ul>

        {make_cta_inline('Ginecología', 'Agenda tu hora con el [Ginecólogo]', 'Consulta $30.000 particular. Ecografía ginecológica $35.000 (en la misma consulta). WhatsApp 24/7.', 'quiero%20agendar%20una%20hora%20con%20Ginecolog%C3%ADa.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta la consulta de ginecología?', '<strong>$30.000 particular</strong>. La especialidad no acepta Fonasa. Si necesitas ecografía ginecológica son $35.000 adicionales en la misma consulta.')}
          {faq_item('¿Cuál es la diferencia con la matrona?', 'La matrona hace control ginecológico básico, PAP, anticoncepción y embarazo de bajo riesgo (Fonasa preferencial $16.000). El ginecólogo es médico especialista, evalúa patologías más complejas y hace ecografías.')}
          {faq_item('¿Hace ecografías de embarazo?', 'Sí. Control prenatal con ecografía obstétrica en cada trimestre. Embarazos de alto riesgo se derivan a maternidad de Concepción.')}
          {faq_item('¿Atiende a adolescentes?', 'Sí. Primera consulta ginecológica, irregularidades menstruales, anticoncepción. Recomendamos venir con un adulto responsable si es menor de edad.')}
          {faq_item('¿Cuándo viene el ginecólogo?', 'El Dr. Rejón coordina fechas mes a mes según demanda. Avisamos por WhatsApp las próximas disponibles.')}
          {faq_item('¿Hace inserción de DIU?', 'Sí, hace inserción y retiro de DIU. El procedimiento se cotiza aparte. La evaluación previa con ecografía es importante.')}
        </div>'''

GINE_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta la consulta de ginecología?","acceptedAnswer":{"@type":"Answer","text":"$30.000 particular. No acepta Fonasa. Ecografía ginecológica $35.000 adicionales si corresponde."}},
    {"@type":"Question","name":"¿Cuál es la diferencia con la matrona?","acceptedAnswer":{"@type":"Answer","text":"Matrona: control ginecológico básico, PAP, anticoncepción, embarazo bajo riesgo (Fonasa preferencial $16.000). Ginecólogo: médico especialista, patologías complejas, ecografías."}},
    {"@type":"Question","name":"¿Hace ecografías de embarazo?","acceptedAnswer":{"@type":"Answer","text":"Sí. Control prenatal con ecografía obstétrica cada trimestre. Embarazos de alto riesgo se derivan a Concepción."}},
    {"@type":"Question","name":"¿Atiende adolescentes?","acceptedAnswer":{"@type":"Answer","text":"Sí. Primera consulta, irregularidades, anticoncepción. Si es menor recomendamos venir con adulto responsable."}},
    {"@type":"Question","name":"¿Hace inserción de DIU?","acceptedAnswer":{"@type":"Answer","text":"Sí, inserción y retiro de DIU. Procedimiento se cotiza aparte. Evaluación previa con ecografía."}}'''

GINE_RELATED = f'''
            <li><a href="/blog/matrona">{ARROW} Matrona</a></li>
            <li><a href="/blog/ecografia">{ARROW} Ecografía</a></li>
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/nutricion">{ARROW} Nutrición</a></li>
            <li><a href="/blog/psicologia-adulto">{ARROW} Psicología Adulto</a></li>
          '''


# ===================== GASTROENTEROLOGÍA =====================

GAST_BODY = f'''
        <h2>¿Qué es la <em>Gastroenterología</em>?</h2>
        <p>La gastroenterología es la especialidad que trata enfermedades del aparato digestivo: esófago, estómago, intestino, hígado, vesícula y páncreas. En el CMC atiende el <strong>Dr. Nicolás Quijano</strong>, especialista que viene desde Concepción a coordinar fechas mes a mes según demanda.</p>

        {callout_info('<strong>Reflujo, gastritis y colon irritable</strong> son los motivos de consulta más frecuentes. Muchos casos se resuelven con medicación + cambios de hábito, sin necesidad de endoscopía.')}

        <h2>¿Cuándo consultar al <em>gastroenterólogo</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/></svg></div><div class="body"><strong>Reflujo y acidez</strong><span>Ardor en el pecho después de comer, sensación de líquido subiendo. Si es frecuente, requiere tratamiento.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11h.01"/><path d="M15 11h.01"/><circle cx="12" cy="12" r="10"/></svg></div><div class="body"><strong>Dolor abdominal recurrente</strong><span>Más de 3 episodios al mes, con o sin relación con comidas. Evaluación de gastritis, úlceras o colon irritable.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 2"/></svg></div><div class="body"><strong>Cambios en deposiciones</strong><span>Diarrea o constipación crónicas, alternancia, presencia de sangre. No esperes a que pase solo.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2L2 12h3v8h6v-6h2v6h6v-8h3z"/></svg></div><div class="body"><strong>Hígado graso o transaminasas altas</strong><span>Detectado en exámenes preventivos. Ecografía y manejo nutricional integral.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></div><div class="body"><strong>Pérdida de peso involuntaria</strong><span>Más de 5 kg en pocos meses sin intentarlo. Requiere descartar causas digestivas.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></div><div class="body"><strong>Helicobacter pylori</strong><span>Detección y tratamiento. Bacteria asociada a gastritis y úlceras.</span></div></div>
        </div>

        <h2>Lo que <em>resuelve</em> la consulta</h2>
        <ul>
          <li><strong>Diagnóstico clínico</strong> y revisión de exámenes (ecografía abdominal, exámenes de sangre, deposiciones).</li>
          <li><strong>Indicación de endoscopía o colonoscopía</strong> si corresponde — se realizan en Concepción.</li>
          <li><strong>Tratamiento médico</strong> de gastritis, reflujo, colon irritable, hígado graso.</li>
          <li><strong>Plan de hábitos</strong> — alimentación, manejo del estrés, control de peso.</li>
          <li><strong>Derivación a cirugía</strong> si es necesario (vesícula, hernias).</li>
        </ul>

        {make_cta_inline('Gastroenterología', 'Agenda tu hora con el [Gastroenterólogo]', 'Consulta $35.000 particular. Especialista coordina fechas mes a mes. WhatsApp 24/7.', 'quiero%20agendar%20una%20hora%20de%20Gastroenterolog%C3%ADa.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta la consulta?', '<strong>$35.000 particular</strong>. La especialidad no acepta Fonasa. Es preferible traer exámenes recientes (sangre, ecografía abdominal) si los tienes.')}
          {faq_item('¿Hacen endoscopía en CMC?', 'No. Se realiza en clínicas de Concepción. El Dr. Quijano la indica e interpreta el resultado en consulta de control.')}
          {faq_item('¿Cuándo viene el gastroenterólogo?', 'Coordina fechas mes a mes según demanda. Avisamos por WhatsApp.')}
          {faq_item('¿Atienden niños?', 'En general adolescentes desde 14-15 años. Para niños menores derivamos a gastroenterología pediátrica en Concepción.')}
          {faq_item('Tengo gastritis hace años. ¿Sirve consultar de nuevo?', 'Sí. La gastritis crónica requiere control: descartar H. pylori, ajustar tratamiento, evaluar progresión. No la dejes.')}
          {faq_item('Reflujo todas las noches: ¿qué hago mientras espero la consulta?', 'Eleva la cabecera de la cama 15 cm, no comer 3 horas antes de acostarte, evitar grasas/picantes/café/alcohol. Si es muy intenso, consulta primero a medicina general por ranitidina/omeprazol.')}
        </div>'''

GAST_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta la consulta de gastroenterología?","acceptedAnswer":{"@type":"Answer","text":"$35.000 particular. No acepta Fonasa. Trae exámenes recientes si los tienes."}},
    {"@type":"Question","name":"¿Hacen endoscopía?","acceptedAnswer":{"@type":"Answer","text":"No en CMC. Se realiza en clínicas de Concepción. El especialista la indica e interpreta los resultados."}},
    {"@type":"Question","name":"¿Cuándo viene el gastroenterólogo?","acceptedAnswer":{"@type":"Answer","text":"Coordina fechas mes a mes según demanda. Avisamos por WhatsApp."}},
    {"@type":"Question","name":"¿Atienden niños?","acceptedAnswer":{"@type":"Answer","text":"Adolescentes desde 14-15 años. Niños menores se derivan a gastroenterología pediátrica en Concepción."}},
    {"@type":"Question","name":"Tengo gastritis crónica. ¿Sirve consultar?","acceptedAnswer":{"@type":"Answer","text":"Sí. Requiere control: descartar H. pylori, ajustar tratamiento, evaluar progresión."}}'''

GAST_RELATED = f'''
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/nutricion">{ARROW} Nutrición</a></li>
            <li><a href="/blog/ecografia">{ARROW} Ecografía</a></li>
            <li><a href="/blog/cardiologia">{ARROW} Cardiología</a></li>
            <li><a href="/blog/psicologia-adulto">{ARROW} Psicología Adulto</a></li>
          '''


# ===================== ENDODONCIA =====================

ENDO_BODY = f'''
        <h2>¿Qué es la <em>Endodoncia</em>?</h2>
        <p>La endodoncia, también conocida como "tratamiento de conducto", es el procedimiento que salva un diente cuando la caries llegó al nervio. Se limpia el interior del diente, se sella y se protege con una corona o tapadura grande. Evita la extracción y conserva tu diente natural por años.</p>
        <p>En el CMC atiende el <strong>Dr. Fernando Fredes</strong>, endodoncista que viene a coordinar fechas mes a mes según demanda.</p>

        {callout_info('<strong>El proceso parte siempre con tu dentista general</strong>: ella detecta la necesidad de endodoncia (radiografía, evaluación clínica) y te deriva al Dr. Fredes con el diagnóstico claro.')}

        <h2>¿Cuándo necesitas <em>endodoncia</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2L2 12h3v8h6v-6h2v6h6v-8h3z"/></svg></div><div class="body"><strong>Dolor intenso de muela</strong><span>Dolor punzante, espontáneo, que no cede con analgésicos. Es la señal clásica de pulpitis.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 2"/></svg></div><div class="body"><strong>Sensibilidad al frío/calor que persiste</strong><span>Si el dolor sigue 30+ segundos después de terminar el estímulo, ya hay daño pulpar.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div><div class="body"><strong>Diente oscurecido</strong><span>Cambio de color a gris/morado: el nervio puede estar muerto. Requiere endodoncia aunque no duela.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="12" y1="6" x2="12" y2="14"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></div><div class="body"><strong>Absceso o "nacido"</strong><span>Bolsa de pus en la encía. Infección que requiere drenaje + endodoncia urgente.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></div><div class="body"><strong>Trauma dental</strong><span>Golpe que fracturó el diente exponiendo el nervio. Tratamiento de conducto + reconstrucción.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3z"/></svg></div><div class="body"><strong>Caries muy profunda</strong><span>Detectada en radiografía: si llegó al nervio se requiere endodoncia para conservar el diente.</span></div></div>
        </div>

        <h2>El <em>proceso</em> en detalle</h2>
        <ol>
          <li><strong>Evaluación con dentista general</strong> ($25.000) — radiografía, diagnóstico clínico, derivación al Dr. Fredes.</li>
          <li><strong>Anestesia local</strong> — la endodoncia moderna no duele durante el procedimiento.</li>
          <li><strong>Apertura del diente y limpieza de los conductos</strong> — se elimina el nervio infectado.</li>
          <li><strong>Sellado de los conductos</strong> con material biocompatible (gutapercha).</li>
          <li><strong>Reconstrucción del diente</strong> — tapadura grande o corona, según el caso.</li>
        </ol>
        <p>Una endodoncia típica toma 1–2 sesiones. El diente queda funcional para masticación normal por muchos años.</p>

        {make_cta_inline('Endodoncia', 'Agenda evaluación con [dentista general]', 'Empezamos con dentista general ($25.000). Si requiere endodoncia, derivamos al Dr. Fredes y te entregamos presupuesto detallado.', 'tengo%20dolor%20de%20muela%2C%20creo%20que%20necesito%20endodoncia.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta una endodoncia?', 'Depende del diente: los molares tienen más conductos y son más complejos que los incisivos. El presupuesto exacto te lo entrega el Dr. Fredes después de la radiografía. Empezamos siempre con la evaluación de dentista general ($25.000).')}
          {faq_item('¿Duele el tratamiento?', 'No. Con anestesia local moderna, el procedimiento es indoloro. Puede haber molestia los 2-3 días siguientes que cede con analgésicos comunes.')}
          {faq_item('¿Aceptan Fonasa?', 'No. Toda la atención dental en CMC es particular. Aceptamos efectivo, transferencia, débito y crédito.')}
          {faq_item('¿Cuándo viene el endodoncista?', 'El Dr. Fredes coordina fechas mes a mes según demanda. La dentista general agenda directamente con él según tu caso.')}
          {faq_item('¿Después de la endodoncia el diente queda como nuevo?', 'Sí, queda funcional. Puede oscurecerse levemente con los años. Si es un diente muy expuesto a fuerzas (como un molar), puede requerir corona para protegerlo.')}
          {faq_item('¿Mejor sacar el diente o hacer endodoncia?', 'Casi siempre es mejor conservar tu diente natural. La extracción te obliga a evaluar implante después ($650.000+) o aceptar el espacio. Tu dentista general te orienta caso por caso.')}
        </div>'''

ENDO_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta una endodoncia?","acceptedAnswer":{"@type":"Answer","text":"Depende del diente. Molares más complejos que incisivos. Presupuesto exacto tras radiografía. Inicio con dentista general $25.000."}},
    {"@type":"Question","name":"¿Duele el tratamiento?","acceptedAnswer":{"@type":"Answer","text":"No. Con anestesia local moderna es indoloro. Molestia 2-3 días post que cede con analgésicos."}},
    {"@type":"Question","name":"¿Aceptan Fonasa en endodoncia?","acceptedAnswer":{"@type":"Answer","text":"No. Toda atención dental es particular. Aceptamos efectivo, transferencia, débito y crédito."}},
    {"@type":"Question","name":"¿Mejor sacar el diente o hacer endodoncia?","acceptedAnswer":{"@type":"Answer","text":"Casi siempre conservar el diente natural. La extracción obliga a evaluar implante ($650.000+) o aceptar el espacio."}},
    {"@type":"Question","name":"¿Cuándo viene el endodoncista?","acceptedAnswer":{"@type":"Answer","text":"Coordina fechas mes a mes. Dentista general agenda directamente con él según el caso."}}'''

ENDO_RELATED = f'''
            <li><a href="/blog/odontologia-general">{ARROW} Odontología General</a></li>
            <li><a href="/blog/implantologia">{ARROW} Implantología</a></li>
            <li><a href="/blog/ortodoncia">{ARROW} Ortodoncia</a></li>
            <li><a href="/blog/estetica-facial">{ARROW} Estética Facial</a></li>
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
          '''


# ===================== IMPLANTOLOGÍA =====================

IMP_BODY = f'''
        <h2>¿Qué es la <em>Implantología</em>?</h2>
        <p>El implante dental es la solución más completa cuando pierdes un diente. Consiste en un <strong>tornillo de titanio</strong> que se instala en el hueso maxilar y, sobre él, una <strong>corona</strong> que se ve y funciona como un diente natural. Es una alternativa muy superior a las prótesis removibles tradicionales.</p>
        <p>En el CMC atiende la <strong>Dra. Aurora Valdés</strong>, implantóloga que viene a coordinar fechas mes a mes según demanda.</p>

        {callout_info('<strong>Empezamos con dentista general</strong>: evaluación clínica, radiografía panorámica y, si corresponde, TAC dental para planificar el implante. Sin esa evaluación no se puede dar presupuesto exacto.')}

        <h2>¿Cuándo necesitas un <em>implante dental</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg></div><div class="body"><strong>Pérdida reciente de un diente</strong><span>Por extracción, trauma o caries. Mientras antes evalúes, mejor (el hueso se reabsorbe con el tiempo).</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div><div class="body"><strong>Prótesis removible incómoda</strong><span>Si la "plaquita" se mueve o duele al masticar, los implantes son la mejor solución de largo plazo.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11h.01"/><path d="M15 11h.01"/><path d="M12 17a3 3 0 0 1-3-3"/><circle cx="12" cy="12" r="10"/></svg></div><div class="body"><strong>Espacio sin diente hace tiempo</strong><span>El hueso se reabsorbe pero todavía es posible. Puede requerir injerto óseo previo.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/></svg></div><div class="body"><strong>Múltiples piezas perdidas</strong><span>Implantes individuales o estrategia de "todo en 4/6" para reemplazar arcadas completas.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg></div><div class="body"><strong>Tras endodoncia fallida</strong><span>Si una endodoncia no resultó y el diente debe extraerse, el implante es la siguiente opción.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></div><div class="body"><strong>Quieres una solución estable y a largo plazo</strong><span>Los implantes pueden durar décadas con buenos cuidados. Inversión que vale la pena.</span></div></div>
        </div>

        <h2>El <em>proceso</em> de implante</h2>
        <ol>
          <li><strong>Evaluación inicial con dentista general</strong> ($25.000) — examen clínico, radiografía panorámica.</li>
          <li><strong>TAC dental</strong> (si la implantóloga lo solicita) — para planificar exactamente la posición del implante.</li>
          <li><strong>Cirugía de implante</strong> con la Dra. Valdés — instalación del tornillo de titanio en el hueso. Anestesia local.</li>
          <li><strong>Período de osteointegración</strong> (3-6 meses) — el hueso se fusiona con el implante.</li>
          <li><strong>Instalación de la corona</strong> — la pieza visible que reemplaza el diente.</li>
        </ol>

        {make_cta_inline('Implantología', 'Agenda evaluación con [dentista general]', 'Implantes desde $650.000 (evaluación final tras radiografía). Aceptamos efectivo, transferencia, débito y crédito.', 'quiero%20evaluar%20un%20implante%20dental.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta un implante dental?', '<strong>Desde $650.000</strong> incluyendo implante + corona. El precio exacto depende de la zona, tipo de hueso y si requiere injerto óseo. Te entregamos presupuesto detallado tras la evaluación inicial.')}
          {faq_item('¿Duele la cirugía?', 'No durante el procedimiento (anestesia local). Después puede haber molestia leve por 3-5 días que cede con analgésicos comunes. Volverás al trabajo al día siguiente.')}
          {faq_item('¿Cuánto tiempo demora todo el proceso?', 'Desde la cirugía hasta la corona definitiva, entre 3 y 6 meses. Depende de cómo cicatriza tu hueso. La corona provisoria se puede instalar antes para que no andes sin diente visible.')}
          {faq_item('¿Aceptan Fonasa?', 'No. Toda la atención dental en CMC es particular. Aceptamos efectivo, transferencia, débito y crédito.')}
          {faq_item('¿Cuánto duran los implantes?', 'Con buenos cuidados (cepillado, limpiezas profesionales semestrales) pueden durar 20+ años. La corona puede requerir reemplazo antes que el implante mismo.')}
          {faq_item('Tengo diabetes / fumo. ¿Puedo hacerme implante?', 'Diabetes bien controlada: sí. Fumador: posible pero con mayor riesgo de fracaso. La Dra. Valdés evalúa caso por caso y conversa contigo el riesgo-beneficio.')}
        </div>'''

IMP_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta un implante dental?","acceptedAnswer":{"@type":"Answer","text":"Desde $650.000 incluyendo implante + corona. Precio exacto depende de zona, hueso e injerto si corresponde."}},
    {"@type":"Question","name":"¿Duele la cirugía de implante?","acceptedAnswer":{"@type":"Answer","text":"No durante el procedimiento (anestesia local). Molestia leve 3-5 días post que cede con analgésicos."}},
    {"@type":"Question","name":"¿Cuánto demora todo el proceso?","acceptedAnswer":{"@type":"Answer","text":"3-6 meses desde la cirugía hasta la corona definitiva. Depende de cicatrización del hueso."}},
    {"@type":"Question","name":"¿Aceptan Fonasa en implantología?","acceptedAnswer":{"@type":"Answer","text":"No. Toda atención dental es particular. Aceptamos efectivo, transferencia, débito y crédito."}},
    {"@type":"Question","name":"¿Cuánto duran los implantes?","acceptedAnswer":{"@type":"Answer","text":"Con buenos cuidados, 20+ años. La corona puede requerir reemplazo antes que el implante."}}'''

IMP_RELATED = f'''
            <li><a href="/blog/odontologia-general">{ARROW} Odontología General</a></li>
            <li><a href="/blog/endodoncia">{ARROW} Endodoncia</a></li>
            <li><a href="/blog/ortodoncia">{ARROW} Ortodoncia</a></li>
            <li><a href="/blog/estetica-facial">{ARROW} Estética Facial</a></li>
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
          '''


# ===================== MASOTERAPIA =====================

MAS_BODY = f'''
        <h2>¿Qué es la <em>Masoterapia</em>?</h2>
        <p>La masoterapia es el masaje terapéutico aplicado por una profesional formada. No es relajación general — es una intervención específica para tratar <strong>contracturas, dolor muscular, tensión cervical y estrés crónico</strong> que afecta tu cuerpo. En el CMC atiende <strong>Paola Acosta</strong>, masoterapeuta con experiencia en patología musculoesquelética.</p>

        {callout_info('<strong>20 o 40 minutos:</strong> tenemos dos modalidades. La de 20 min ($17.990) está enfocada en una zona específica (espalda y cuello). La de 40 min ($26.990) abarca todo el cuerpo.')}

        <h2>¿Cuándo te conviene <em>masoterapia</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg></div><div class="body"><strong>Tensión cervical y dolor de cuello</strong><span>Por trabajo de oficina, mucho computador o conducir. La masoterapia libera la tensión acumulada.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v8"/><path d="M22 22H2"/></svg></div><div class="body"><strong>Dolor de espalda alta</strong><span>"Nudos" entre los omóplatos, contracturas musculares por mala postura o estrés.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div><div class="body"><strong>Estrés crónico</strong><span>El estrés se acumula físicamente. Una sesión semanal o quincenal mejora ánimo, sueño y energía.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s-8-4-8-12A8 8 0 0 1 12 2a8 8 0 0 1 8 8c0 8-8 12-8 12z"/></svg></div><div class="body"><strong>Tras kinesiología</strong><span>Complementa tratamientos de rehabilitación cuando hay tensión muscular adyacente al área tratada.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/></svg></div><div class="body"><strong>Dolor menstrual</strong><span>Masaje abdominal y lumbar suave puede aliviar el dolor de algunos cólicos menstruales.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></div><div class="body"><strong>Después de actividad física intensa</strong><span>Recuperación muscular post-deporte. Reduce DOMS (dolor muscular tardío).</span></div></div>
        </div>

        <h2>Masoterapia vs <em>Kinesiología</em></h2>
        <ul>
          <li><strong>Masoterapia</strong> — masaje terapéutico para liberar tensión y contracturas. Bienestar y manejo del estrés.</li>
          <li><strong>Kinesiología</strong> — rehabilitación de lesiones específicas con ejercicios + terapia manual. Bono Fonasa $7.830.</li>
          <li>Si tienes una <strong>lesión definida</strong> (esguince, hernia, tendinitis), parte por kinesiología. Si tienes <strong>tensión generalizada o estrés</strong>, masoterapia.</li>
        </ul>

        {make_cta_inline('Masoterapia', 'Agenda tu sesión de [Masoterapia]', '20 min $17.990 (zona específica) o 40 min $26.990 (cuerpo completo). WhatsApp 24/7.', 'quiero%20agendar%20una%20sesi%C3%B3n%20de%20Masoterapia.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta?', '<strong>20 min $17.990</strong> (espalda y cuello, zona específica) o <strong>40 min $26.990</strong> (cuerpo completo). No acepta Fonasa.')}
          {faq_item('¿Cuál sesión me conviene?', 'Si solo te molesta una zona (cuello, espalda alta), 20 min basta. Si tienes tensión general o quieres mejor experiencia, 40 min es más completa.')}
          {faq_item('¿Con qué frecuencia conviene?', 'Para tensión crónica: 1 sesión semanal por 4-6 semanas y luego quincenal de mantención. Para casos puntuales: 1-2 sesiones suelen alcanzar.')}
          {faq_item('¿Es lo mismo que un masaje relajante en spa?', 'Compartimos técnicas básicas pero el enfoque es terapéutico. Paola identifica puntos de tensión específicos y trabaja sobre ellos. No es solo bienestar — busca resultados.')}
          {faq_item('¿Atiende embarazadas?', 'Sí, con técnica adaptada (posición lateral, evitar zonas específicas). Idealmente desde el segundo trimestre. Comunícale tu estado al agendar.')}
          {faq_item('¿Tengo que traer toalla o algo?', 'No, el centro tiene todo lo necesario (toallas, aceites, camilla). Llega 5 min antes para acomodarte.')}
        </div>'''

MAS_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta la masoterapia?","acceptedAnswer":{"@type":"Answer","text":"20 min $17.990 (zona específica) o 40 min $26.990 (cuerpo completo). No acepta Fonasa."}},
    {"@type":"Question","name":"¿Cuál sesión me conviene?","acceptedAnswer":{"@type":"Answer","text":"20 min para zona específica (cuello/espalda alta). 40 min para tensión generalizada."}},
    {"@type":"Question","name":"¿Con qué frecuencia conviene?","acceptedAnswer":{"@type":"Answer","text":"Tensión crónica: 1 sesión semanal x 4-6 semanas, luego quincenal. Casos puntuales: 1-2 sesiones."}},
    {"@type":"Question","name":"¿Es lo mismo que masaje relajante?","acceptedAnswer":{"@type":"Answer","text":"Comparte técnicas pero el enfoque es terapéutico. Identifica puntos de tensión específicos."}},
    {"@type":"Question","name":"¿Atiende embarazadas?","acceptedAnswer":{"@type":"Answer","text":"Sí, con técnica adaptada. Idealmente desde el segundo trimestre. Comunícale tu estado al agendar."}}'''

MAS_RELATED = f'''
            <li><a href="/blog/kinesiologia">{ARROW} Kinesiología</a></li>
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/psicologia-adulto">{ARROW} Psicología Adulto</a></li>
            <li><a href="/blog/podologia">{ARROW} Podología</a></li>
            <li><a href="/blog/estetica-facial">{ARROW} Estética Facial</a></li>
          '''


# ===================== NUTRICIÓN =====================

NUT_BODY = f'''
        <h2>¿Qué es la <em>Nutrición</em>?</h2>
        <p>La nutricionista evalúa tu alimentación, composición corporal, hábitos y patologías para diseñar un plan alimentario personalizado. No es "ponerse a dieta" — es aprender a comer mejor de manera sostenible. En el CMC atiende <strong>Gisela Pinto</strong>, nutricionista con bono Fonasa.</p>

        {callout_info('<strong>Bono Fonasa $4.770</strong>: la nutrición es una de las prestaciones que sí cubre Fonasa. Lo emitimos en el centro con huella biométrica. Particular $20.000.')}

        <h2>¿Cuándo consultar a la <em>nutricionista</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3z"/></svg></div><div class="body"><strong>Quieres bajar de peso de forma saludable</strong><span>Plan personalizado según tu metabolismo, edad y rutina. Sin dietas extremas.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 2"/></svg></div><div class="body"><strong>Diabetes o prediabetes</strong><span>La alimentación es pilar fundamental del tratamiento. Plan adaptado a tus medicamentos y estilo de vida.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div><div class="body"><strong>Hipertensión o colesterol alto</strong><span>Reducir sodio, grasas saturadas, aumentar fibra y omega-3. Mejora medible en pocas semanas.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s-8-4-8-12A8 8 0 0 1 12 2a8 8 0 0 1 8 8c0 8-8 12-8 12z"/></svg></div><div class="body"><strong>Embarazo o lactancia</strong><span>Necesidades nutricionales especiales. Hierro, ácido fólico, calorías ajustadas a la etapa.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9h18M9 21V9"/></svg></div><div class="body"><strong>Niños con obesidad o malos hábitos</strong><span>Educación alimentaria familiar. Cambios sostenibles, sin restricciones traumáticas.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></div><div class="body"><strong>Deportistas y aumento de masa muscular</strong><span>Plan ajustado a tu actividad física: macros, timing de comidas, suplementación cuando corresponde.</span></div></div>
        </div>

        <h2>Bioimpedanciometría — análisis de <em>composición corporal</em></h2>
        <p>En el CMC ofrecemos <strong>bioimpedanciometría ($20.000)</strong>: examen que mide tu composición corporal real (% grasa, músculo, agua) en 5 minutos, indoloro. Indispensable para:</p>
        <ul>
          <li>Tener un punto de partida objetivo (no solo el peso de la balanza).</li>
          <li>Evaluar progreso real del tratamiento.</li>
          <li>Detectar sarcopenia (pérdida de masa muscular) en adultos mayores.</li>
        </ul>

        {make_cta_inline('Nutrición', 'Agenda tu hora con [Nutrición]', 'Bono Fonasa $4.770 (se emite en el centro) o particular $20.000. Bioimpedanciometría $20.000 si corresponde.', 'quiero%20agendar%20una%20hora%20de%20Nutrici%C3%B3n.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta la consulta?', '<strong>$4.770 con bono Fonasa</strong> (se emite en el centro con huella biométrica) o <strong>$20.000 particular</strong>. Bioimpedanciometría $20.000 adicional cuando corresponde.')}
          {faq_item('¿Cuántas sesiones necesito?', 'Generalmente: 1 sesión inicial + controles cada 3-4 semanas durante 3-6 meses. Después controles bimensuales o trimestrales para mantener.')}
          {faq_item('¿Atiende niños?', 'Sí. Gisela atiende desde edad escolar para temas de obesidad infantil, alergias alimentarias y educación nutricional familiar.')}
          {faq_item('¿Hace planes para deportistas?', 'Sí. Ajustamos planes según tipo de deporte, intensidad, objetivos (rendimiento, hipertrofia, recomposición).')}
          {faq_item('¿Hago el bono Fonasa antes de venir?', 'No, lo emitimos en el centro. Trae solo tu cédula. La huella se toma al momento.')}
          {faq_item('¿Me hace dieta cetogénica / ayuno intermitente?', 'Si tu caso lo permite, sí. Pero con evaluación previa: estos enfoques no funcionan para todos. Gisela evalúa caso por caso, sin "modas".')}
        </div>'''

NUT_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta la consulta de nutrición?","acceptedAnswer":{"@type":"Answer","text":"$4.770 con bono Fonasa (se emite en el centro) o $20.000 particular. Bioimpedanciometría $20.000 adicional."}},
    {"@type":"Question","name":"¿Cuántas sesiones necesito?","acceptedAnswer":{"@type":"Answer","text":"1 sesión inicial + controles cada 3-4 semanas por 3-6 meses. Luego mantención bimensual o trimestral."}},
    {"@type":"Question","name":"¿Atiende niños?","acceptedAnswer":{"@type":"Answer","text":"Sí, desde edad escolar para obesidad infantil, alergias alimentarias y educación familiar."}},
    {"@type":"Question","name":"¿Hace planes para deportistas?","acceptedAnswer":{"@type":"Answer","text":"Sí, ajustados al tipo de deporte, intensidad y objetivos (rendimiento, hipertrofia, recomposición)."}},
    {"@type":"Question","name":"¿Hago el bono Fonasa antes de venir?","acceptedAnswer":{"@type":"Answer","text":"No, se emite en el centro con huella biométrica. Trae solo tu cédula."}}'''

NUT_RELATED = f'''
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/cardiologia">{ARROW} Cardiología</a></li>
            <li><a href="/blog/psicologia-adulto">{ARROW} Psicología Adulto</a></li>
            <li><a href="/blog/kinesiologia">{ARROW} Kinesiología</a></li>
            <li><a href="/blog/matrona">{ARROW} Matrona</a></li>
          '''


# ===================== PSICOLOGÍA ADULTO =====================

PSIA_BODY = f'''
        <h2>¿Qué es la <em>Psicología Adulto</em>?</h2>
        <p>La psicología es el espacio para abordar problemas emocionales, relacionales y de salud mental que afectan tu bienestar diario. No tienes que esperar a estar "muy mal" para consultar — la psicoterapia preventiva o de mantención es tan válida como la de crisis. En el CMC atienden <strong>Jorge Montalba</strong> y <strong>Juan Pablo Rodríguez</strong>.</p>

        {callout_info('<strong>Bono Fonasa $14.420</strong> en sesiones de 45 minutos. Lo emitimos en el centro con huella biométrica. Particular $20.000.')}

        <h2>¿Cuándo consultar a un <em>psicólogo</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/></svg></div><div class="body"><strong>Ansiedad o ataques de pánico</strong><span>Sensación constante de preocupación, palpitaciones, opresión en el pecho. Tratable con psicoterapia.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></div><div class="body"><strong>Depresión o tristeza prolongada</strong><span>Pérdida de interés, fatiga, alteraciones del sueño y apetito por más de 2 semanas. No esperes a tocar fondo.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div><div class="body"><strong>Duelo</strong><span>Pérdida de un ser querido, separación, cambio mayor de vida. El proceso requiere apoyo profesional.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg></div><div class="body"><strong>Problemas de pareja o familia</strong><span>Conflictos recurrentes, comunicación rota. Sesiones individuales o de pareja.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></div><div class="body"><strong>Estrés laboral o burnout</strong><span>Agotamiento crónico, irritabilidad, dificultad para desconectarse del trabajo. Antes que se vuelva incapacitante.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></div><div class="body"><strong>Insomnio crónico</strong><span>Dificultad para dormirse o despertares nocturnos por más de 3 semanas. La psicoterapia es el tratamiento de primera línea.</span></div></div>
        </div>

        <h2>¿Qué <em>esperar</em> de la primera sesión?</h2>
        <ul>
          <li><strong>Conversación abierta</strong> — el psicólogo te escucha, hace preguntas, no juzga.</li>
          <li><strong>Identificación de objetivos</strong> — qué quieres lograr con la terapia.</li>
          <li><strong>Plan inicial</strong> — frecuencia de sesiones (típicamente semanal al inicio), duración estimada.</li>
          <li><strong>Confidencialidad total</strong> — todo lo que cuentas queda entre tú y el profesional.</li>
        </ul>
        <p>No tienes que llegar "preparado" ni con la "respuesta correcta". Llega y conversemos.</p>

        <h2>Informes psicológicos</h2>
        <p>El CMC también realiza <strong>informes psicológicos</strong> ($25.000-$30.000) para trámites legales, laborales, escolares o de salud. Pregunta al agendar si tu caso lo requiere.</p>

        {make_cta_inline('Psicología Adulto', 'Agenda tu hora de [Psicología]', 'Bono Fonasa $14.420 (se emite en el centro) o particular $20.000. Sesión de 45 min. WhatsApp confidencial 24/7.', 'quiero%20agendar%20una%20hora%20de%20Psicolog%C3%ADa%20Adulto.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta la sesión?', '<strong>$14.420 con bono Fonasa</strong> (se emite en el centro con huella biométrica) o <strong>$20.000 particular</strong>. Sesión de 45 minutos.')}
          {faq_item('¿Cuántas sesiones necesito?', 'Depende del motivo. Para temas puntuales: 6-12 sesiones. Para procesos profundos: 6 meses a 1 año o más. Conversamos eso en la primera consulta.')}
          {faq_item('¿Hay diferencia entre Jorge y Juan Pablo?', 'Ambos atienden adultos con enfoques complementarios. Si no tienes preferencia, agendamos con quien tenga cupo más cercano. Si quieres elegir, dinos al agendar.')}
          {faq_item('¿Recetan medicamentos?', 'No, los psicólogos no recetan. Si tu caso requiere medicación, derivamos a psiquiatría (no en CMC) o a medicina general para evaluación inicial.')}
          {faq_item('¿Atienden online?', 'Privilegiamos atención presencial en Carampangue. Para casos puntuales podemos coordinar online — consulta al agendar.')}
          {faq_item('Es mi primera vez con psicólogo. ¿Es raro?', 'Para nada. Cada vez más personas consultan psicología preventivamente o en momentos difíciles puntuales. No tienes que estar "loco" ni en crisis para consultar.')}
        </div>'''

PSIA_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta la sesión de psicología?","acceptedAnswer":{"@type":"Answer","text":"$14.420 con bono Fonasa (se emite en el centro) o $20.000 particular. Sesión de 45 minutos."}},
    {"@type":"Question","name":"¿Cuántas sesiones necesito?","acceptedAnswer":{"@type":"Answer","text":"Temas puntuales: 6-12 sesiones. Procesos profundos: 6 meses a 1 año o más."}},
    {"@type":"Question","name":"¿Recetan medicamentos los psicólogos?","acceptedAnswer":{"@type":"Answer","text":"No. Si requiere medicación, derivamos a psiquiatría (no en CMC) o medicina general."}},
    {"@type":"Question","name":"¿Atienden online?","acceptedAnswer":{"@type":"Answer","text":"Privilegiamos presencial en Carampangue. Para casos puntuales puede coordinarse online."}},
    {"@type":"Question","name":"¿Tengo que estar en crisis para consultar?","acceptedAnswer":{"@type":"Answer","text":"No. La psicoterapia preventiva o de mantención es tan válida como la de crisis."}}'''

PSIA_RELATED = f'''
            <li><a href="/blog/psicologia-infantil">{ARROW} Psicología Infantil</a></li>
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/nutricion">{ARROW} Nutrición</a></li>
            <li><a href="/blog/masoterapia">{ARROW} Masoterapia</a></li>
            <li><a href="/blog/matrona">{ARROW} Matrona</a></li>
          '''


# ===================== PSICOLOGÍA INFANTIL =====================

PSII_BODY = f'''
        <h2>¿Qué es la <em>Psicología Infantil</em>?</h2>
        <p>La psicología infantil es la especialidad que trabaja con niños y adolescentes en su desarrollo emocional, conductual y social. Las dificultades en esta etapa, atendidas a tiempo, suelen resolverse rápido y dejar huella positiva para toda la vida. En el CMC atiende <strong>Jorge Montalba</strong>, psicólogo con experiencia en niños y adolescentes.</p>

        {callout_info('<strong>Bono Fonasa $14.420</strong> en sesiones de 45 minutos, igual que adulto. Particular $20.000. Lo emitimos en el centro con huella biométrica del adulto responsable.')}

        <h2>¿Cuándo consultar a la <em>psicóloga infantil</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/></svg></div><div class="body"><strong>Ansiedad escolar</strong><span>Dolor de estómago/cabeza antes del colegio, miedo a separarse, rechazo a ir a clases.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9h18M9 21V9"/></svg></div><div class="body"><strong>Cambios bruscos de conducta</strong><span>Irritabilidad nueva, retraimiento, baja del rendimiento. Algo está pasando — vale la pena explorarlo.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div><div class="body"><strong>Duelo o crisis familiar</strong><span>Separación de los padres, fallecimiento, mudanza, llegada de hermano. Acompañamiento del proceso.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></div><div class="body"><strong>Problemas de aprendizaje</strong><span>Bajo rendimiento, dificultades atencionales. Evaluación y derivación si corresponde.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg></div><div class="body"><strong>Dificultades sociales</strong><span>Aislamiento, conflictos repetidos con compañeros, bullying. Desarrollo de habilidades sociales.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></div><div class="body"><strong>Adolescencia difícil</strong><span>Cambios de ánimo extremos, conductas de riesgo, problemas de identidad. Apoyo profesional para la familia.</span></div></div>
        </div>

        <h2>El proceso con <em>niños</em></h2>
        <ul>
          <li><strong>Primera sesión con padres</strong> — historia del niño, motivo de consulta, contexto.</li>
          <li><strong>Sesiones individuales con el niño</strong> — juego terapéutico, dibujos, conversación según la edad.</li>
          <li><strong>Sesiones de retroalimentación</strong> a los padres — orientación para apoyar el proceso en casa.</li>
          <li><strong>Coordinación con el colegio</strong> cuando es relevante (con autorización).</li>
        </ul>
        <p>El niño no necesita "saber" exactamente para qué viene. La psicóloga lo presenta como un espacio seguro para conversar y jugar.</p>

        {make_cta_inline('Psicología Infantil', 'Agenda hora de [Psicología Infantil]', 'Bono Fonasa $14.420 (se emite en el centro con huella del adulto) o particular $20.000. Primera sesión es con padres.', 'quiero%20agendar%20una%20hora%20de%20Psicolog%C3%ADa%20Infantil.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Desde qué edad atiende?', 'Desde aproximadamente los 5 años. Para menores derivamos a estimulación temprana o a psicología infantil especializada en preescolares.')}
          {faq_item('¿Cuántas sesiones necesita?', 'Depende del caso. Procesos típicos: 8-15 sesiones para temas puntuales, 6 meses para procesos más profundos. Lo conversamos tras la primera evaluación.')}
          {faq_item('¿Atiende adolescentes?', 'Sí, hasta 17 años. La dinámica con adolescentes incluye más confidencialidad — lo conversado entre el psicólogo y el adolescente queda entre ellos, salvo riesgo de daño.')}
          {faq_item('¿Hay que ir todos los hermanos?', 'No. La consulta es por el niño que presenta la dificultad. Si la psicóloga ve que un hermano está involucrado, lo conversa con la familia.')}
          {faq_item('¿Hace evaluaciones para colegio o tribunales?', 'Sí, informes psicológicos ($25.000-$30.000). Avísanos al agendar para preparar el formato adecuado.')}
          {faq_item('Mi hijo no quiere ir. ¿Qué hago?', 'Es normal. Con niños no decimos "vamos al psicólogo porque hay un problema" — explicamos que es un espacio para conversar y jugar. Si persiste el rechazo, conversamos cómo abordarlo en la primera cita con padres.')}
        </div>'''

PSII_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Desde qué edad atiende psicología infantil?","acceptedAnswer":{"@type":"Answer","text":"Desde aproximadamente 5 años. Menores se derivan a estimulación temprana o psicología preescolar especializada."}},
    {"@type":"Question","name":"¿Cuántas sesiones necesita un niño?","acceptedAnswer":{"@type":"Answer","text":"Temas puntuales: 8-15 sesiones. Procesos profundos: alrededor de 6 meses."}},
    {"@type":"Question","name":"¿Atiende adolescentes?","acceptedAnswer":{"@type":"Answer","text":"Sí, hasta 17 años, con dinámica de mayor confidencialidad respecto a los padres."}},
    {"@type":"Question","name":"¿Hace informes para colegio o tribunales?","acceptedAnswer":{"@type":"Answer","text":"Sí, $25.000-$30.000. Avisar al agendar para preparar el formato adecuado."}},
    {"@type":"Question","name":"Mi hijo no quiere ir. ¿Qué hago?","acceptedAnswer":{"@type":"Answer","text":"Es normal. Conviene explicarlo como espacio para conversar y jugar, no como solución a un problema. Lo abordamos en la primera cita con padres."}}'''

PSII_RELATED = f'''
            <li><a href="/blog/psicologia-adulto">{ARROW} Psicología Adulto</a></li>
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/fonoaudiologia">{ARROW} Fonoaudiología</a></li>
            <li><a href="/blog/nutricion">{ARROW} Nutrición</a></li>
            <li><a href="/blog/odontologia-general">{ARROW} Odontología General</a></li>
          '''


# ===================== FONOAUDIOLOGÍA =====================

FON_BODY = f'''
        <h2>¿Qué es la <em>Fonoaudiología</em>?</h2>
        <p>La fonoaudiología trata las dificultades de comunicación: <strong>habla, lenguaje, voz, audición y deglución</strong>. Atiende niños con retraso del lenguaje, adultos con disfonía, pacientes post-ACV con problemas para tragar y muchos casos más. En el CMC atiende <strong>Juana Arratia</strong>.</p>

        {callout_info('<strong>Trabajamos en equipo con otorrino</strong>: muchos problemas de voz, audición y deglución requieren evaluación conjunta. Cuando corresponde, coordinamos directamente con el Dr. Borrego.')}

        <h2>¿Cuándo consultar a <em>fonoaudiología</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9h18M9 21V9"/></svg></div><div class="body"><strong>Niños que no hablan a tiempo</strong><span>A los 2 años no dice palabras claras, a los 3 no arma frases. Mejor consultar antes que esperar.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div><div class="body"><strong>Tartamudez</strong><span>Repetición de sílabas, bloqueos al hablar. Tratamiento temprano mejora pronóstico.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></div><div class="body"><strong>Problemas de pronunciación</strong><span>Dislalia: no pronuncia bien R, S, L u otros sonidos. Adultos también pueden tratarse.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/></svg></div><div class="body"><strong>Disfonía / problemas de voz</strong><span>Voz ronca, fatigada o "rota" persistente. Especialmente en docentes, locutores, vendedores.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></div><div class="body"><strong>Disfagia (dificultad para tragar)</strong><span>Atragantamientos frecuentes, sensación de que la comida se queda en la garganta. Común tras ACV.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/></svg></div><div class="body"><strong>Hipoacusia rehabilitación</strong><span>Tras audiometría con pérdida auditiva: trabajo de lectura labiofacial y adaptación a audífonos.</span></div></div>
        </div>

        <h2>Áreas de <em>trabajo</em></h2>
        <ul>
          <li><strong>Lenguaje infantil</strong> — retraso del lenguaje, trastornos específicos del lenguaje (TEL).</li>
          <li><strong>Habla</strong> — dislalias, tartamudez (disfemia), apraxia.</li>
          <li><strong>Voz</strong> — disfonías, nódulos vocales, voz profesional.</li>
          <li><strong>Deglución</strong> — disfagia neurogénica (ACV, Parkinson) o estructural.</li>
          <li><strong>Audición</strong> — rehabilitación auditiva, trabajo en lectura labiofacial.</li>
        </ul>

        {make_cta_inline('Fonoaudiología', 'Agenda evaluación con [Fonoaudiología]', 'Sesiones desde $25.000 a $50.000 según tipo y duración. Atendemos niños y adultos. WhatsApp 24/7.', 'quiero%20agendar%20una%20hora%20de%20Fonoaudiolog%C3%ADa.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta la sesión?', 'Entre <strong>$25.000 y $50.000</strong> según tipo de evaluación o tratamiento (la primera evaluación suele ser más larga). Solo particular — no acepta Fonasa.')}
          {faq_item('¿Desde qué edad atiende niños?', 'Desde 2 años aprox. La primera sesión es con padres + observación del niño. Después sesiones individuales con seguimiento a la familia.')}
          {faq_item('Mi hijo de 3 años habla "raro". ¿Es para preocuparse?', 'A los 3 años ya debería decir frases de 3-4 palabras y entenderse en al menos 75% de lo que dice. Si no es así, consulta. La intervención temprana es clave.')}
          {faq_item('Soy profesor y tengo voz ronca todos los días. ¿Sirve?', 'Sí. La fonoaudiología es la especialidad para "voz profesional": técnica vocal, manejo del esfuerzo, recuperación de lesiones (nódulos, pólipos vocales).')}
          {faq_item('¿Trabajan tras un ACV?', 'Sí. Rehabilitación de afasia (problemas de lenguaje), disartria (problemas de articulación) y disfagia (problemas para tragar) son áreas centrales.')}
          {faq_item('¿Cuántas sesiones necesito?', 'Muy variable. Procesos cortos: 8-12 sesiones (ej. dislalia simple). Procesos largos: 6 meses a 1 año (ej. TEL, post-ACV). Lo evaluamos en la primera sesión.')}
        </div>'''

FON_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta la sesión de fonoaudiología?","acceptedAnswer":{"@type":"Answer","text":"$25.000 a $50.000 según tipo y duración. Solo particular, no acepta Fonasa."}},
    {"@type":"Question","name":"¿Desde qué edad atiende fonoaudiología?","acceptedAnswer":{"@type":"Answer","text":"Desde aproximadamente 2 años. Primera sesión con padres + observación del niño."}},
    {"@type":"Question","name":"Mi hijo habla raro a los 3 años. ¿Es preocupante?","acceptedAnswer":{"@type":"Answer","text":"A los 3 años ya debería decir frases de 3-4 palabras y entenderse 75%+. Si no, consulta. Intervención temprana es clave."}},
    {"@type":"Question","name":"¿Trata problemas de voz en profesores?","acceptedAnswer":{"@type":"Answer","text":"Sí, voz profesional es área central: técnica vocal, manejo del esfuerzo, recuperación de lesiones."}},
    {"@type":"Question","name":"¿Trabajan tras ACV?","acceptedAnswer":{"@type":"Answer","text":"Sí. Rehabilitación de afasia, disartria y disfagia son áreas centrales."}}'''

FON_RELATED = f'''
            <li><a href="/blog/otorrinolaringologia">{ARROW} Otorrinolaringología</a></li>
            <li><a href="/blog/psicologia-infantil">{ARROW} Psicología Infantil</a></li>
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/kinesiologia">{ARROW} Kinesiología</a></li>
            <li><a href="/blog/odontologia-general">{ARROW} Odontología General</a></li>
          '''


# ===================== MATRONA =====================

MAT_BODY = f'''
        <h2>¿Qué es la <em>Matrona</em>?</h2>
        <p>La matrona es la profesional especializada en salud sexual y reproductiva de la mujer en todas las etapas de la vida. Hace control ginecológico preventivo, anticoncepción, control de embarazo de bajo riesgo, atención del climaterio y mucho más. En el CMC atiende <strong>Sarai Gómez</strong>.</p>

        {callout_info('<strong>Tarifa preferencial Fonasa $16.000</strong> (no es bono — es un precio rebajado para pacientes Fonasa). Particular $30.000. Con PAP incluido: $25.000 Fonasa / $30.000 particular.')}

        <h2>¿Cuándo consultar a la <em>matrona</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></div><div class="body"><strong>PAP anual</strong><span>Examen preventivo de cáncer cervicouterino. Indicado a toda mujer con vida sexual activa, anualmente.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div><div class="body"><strong>Anticoncepción</strong><span>Pastillas, inyectables, implante subdérmico, parche. Asesoría según tu perfil de salud y preferencias.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/></svg></div><div class="body"><strong>Embarazo de bajo riesgo</strong><span>Control prenatal mes a mes. Cuando hay factores de riesgo, deriva a ginecología o maternidad.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/></svg></div><div class="body"><strong>Climaterio y menopausia</strong><span>Manejo de bochornos, alteraciones del sueño, salud ósea. Plan de seguimiento.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></div><div class="body"><strong>Infección urinaria o flujo vaginal</strong><span>Síntomas de cistitis, candidiasis, vaginosis. Diagnóstico y tratamiento inicial.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/></svg></div><div class="body"><strong>Adolescentes y primera consulta</strong><span>Primera evaluación ginecológica, anticoncepción joven, educación en salud sexual.</span></div></div>
        </div>

        <h2>Matrona vs <em>Ginecología</em></h2>
        <ul>
          <li><strong>Matrona</strong> — control sano, PAP, anticoncepción, embarazo bajo riesgo, climaterio. Más accesible (Fonasa preferencial).</li>
          <li><strong>Ginecología</strong> — médico especialista. Patologías más complejas, ecografías, fertilidad, embarazo de alto riesgo.</li>
          <li>Para la mayoría de las mujeres, <strong>la matrona resuelve el 80% de las necesidades</strong>. Cuando hace falta especialista, deriva al Dr. Rejón.</li>
        </ul>

        {make_cta_inline('Matrona', 'Agenda tu hora con [Matrona]', 'Fonasa preferencial $16.000 / particular $30.000. Con PAP $25.000 / $30.000. WhatsApp 24/7.', 'quiero%20agendar%20una%20hora%20con%20Matrona.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta la consulta?', '<strong>$16.000 con tarifa preferencial Fonasa</strong> o <strong>$30.000 particular</strong>. Si incluye PAP: $25.000 (Fonasa) / $30.000 (particular). Revisión de exámenes: $10.000.')}
          {faq_item('¿Cuál es la diferencia con bono Fonasa?', 'La matrona no usa bono Fonasa MLE como medicina general. Es una <strong>tarifa preferencial</strong>: precio rebajado para quienes acreditan ser Fonasa. Trae tu cédula al venir.')}
          {faq_item('¿Atiende adolescentes?', 'Sí, a partir de los 14 años aprox. La primera consulta es exploratoria, sin examen ginecológico si la paciente no se siente lista. Recomendamos venir con un adulto responsable.')}
          {faq_item('¿Hace inserción de DIU?', 'No, la inserción de DIU la realiza el ginecólogo (Dr. Tirso Rejón). La matrona evalúa, te orienta sobre el método y deriva.')}
          {faq_item('¿Atiende parto?', 'No en CMC. El parto se realiza en hospital de Curanilahue u otra maternidad. La matrona del CMC hace control prenatal y educación en parto.')}
          {faq_item('Tengo más de 50 años. ¿Sigo necesitando control ginecológico?', 'Sí. El climaterio y la postmenopausia tienen sus propias necesidades de salud (osteoporosis, salud cardiovascular, control PAP hasta los 65 años en general).')}
        </div>'''

MAT_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta la consulta de matrona?","acceptedAnswer":{"@type":"Answer","text":"$16.000 Fonasa preferencial o $30.000 particular. Con PAP: $25.000 / $30.000."}},
    {"@type":"Question","name":"¿Cuál es la diferencia con bono Fonasa?","acceptedAnswer":{"@type":"Answer","text":"Matrona no usa bono MLE — es tarifa preferencial: precio rebajado para Fonasa que se acredita con cédula."}},
    {"@type":"Question","name":"¿Atiende adolescentes?","acceptedAnswer":{"@type":"Answer","text":"Sí desde 14 años aprox. Primera consulta exploratoria, sin examen ginecológico si no está lista."}},
    {"@type":"Question","name":"¿Hace inserción de DIU?","acceptedAnswer":{"@type":"Answer","text":"No. La realiza el ginecólogo. Matrona orienta sobre método y deriva."}},
    {"@type":"Question","name":"¿Atiende parto?","acceptedAnswer":{"@type":"Answer","text":"No en CMC. Parto se realiza en hospital de Curanilahue u otra maternidad. CMC hace control prenatal."}}'''

MAT_RELATED = f'''
            <li><a href="/blog/ginecologia">{ARROW} Ginecología</a></li>
            <li><a href="/blog/ecografia">{ARROW} Ecografía</a></li>
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/nutricion">{ARROW} Nutrición</a></li>
            <li><a href="/blog/psicologia-adulto">{ARROW} Psicología Adulto</a></li>
          '''


# ===================== PODOLOGÍA =====================

POD_BODY = f'''
        <h2>¿Qué es la <em>Podología</em>?</h2>
        <p>La podología cuida la salud de tus pies: uñas, piel, callosidades, deformidades y problemas relacionados con la marcha. Es indispensable en personas con diabetes (riesgo de pie diabético), adultos mayores y deportistas. En el CMC atiende <strong>Andrea Guevara</strong>.</p>

        {callout_info('<strong>El "pie diabético"</strong> es una de las complicaciones más graves de la diabetes mal controlada. La podología clínica preventiva reduce drásticamente el riesgo de úlceras y amputaciones.')}

        <h2>¿Cuándo consultar a <em>podología</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div><div class="body"><strong>Uña encarnada</strong><span>Dolor, enrojecimiento, infección. Tratamiento conservador o procedimiento quirúrgico ambulatorio.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg></div><div class="body"><strong>Hongos en uñas (onicomicosis)</strong><span>Uñas amarillas, gruesas, despegadas. Tratamiento tópico o sistémico según el caso.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/></svg></div><div class="body"><strong>Callosidades y durezas</strong><span>Eliminación profesional, identificación de la causa (mal calzado, alteración de la pisada).</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></div><div class="body"><strong>Pie diabético — control preventivo</strong><span>Si tienes diabetes, control podológico cada 2-3 meses previene úlceras y complicaciones graves.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11h.01"/><path d="M15 11h.01"/><path d="M12 17a3 3 0 0 1-3-3"/></svg></div><div class="body"><strong>Verrugas plantares</strong><span>"Ojos de gallo" o verrugas dolorosas. Tratamiento con criocirugía o fármaco tópico.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg></div><div class="body"><strong>Adultos mayores y autonomía</strong><span>Cuidado regular cuando ya no se pueden cortar las uñas solos. Mantiene movilidad.</span></div></div>
        </div>

        <h2>Atención <em>específica</em> en pie diabético</h2>
        <ul>
          <li><strong>Evaluación de sensibilidad</strong> (monofilamento) — detecta neuropatía diabética temprana.</li>
          <li><strong>Corte y limado de uñas</strong> con técnica especial para evitar microcortes.</li>
          <li><strong>Cuidado de callosidades</strong> sin causar lesiones.</li>
          <li><strong>Detección precoz de úlceras</strong> y derivación inmediata si las hay.</li>
          <li><strong>Educación al paciente</strong> sobre autocuidado diario en casa.</li>
        </ul>

        {make_cta_inline('Podología', 'Agenda tu hora de [Podología]', 'Sesiones desde $20.000 según procedimiento. Atención especializada en pie diabético. WhatsApp 24/7.', 'quiero%20agendar%20una%20hora%20de%20Podolog%C3%ADa.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta?', '<strong>Desde $20.000</strong> por sesión. El valor exacto depende de procedimientos adicionales (criocirugía de verruga, retiro de uña encarnada, etc.). Solo particular — no acepta Fonasa.')}
          {faq_item('Tengo diabetes. ¿Con qué frecuencia debo venir?', 'Idealmente cada 2-3 meses. La podología preventiva en diabetes reduce el riesgo de úlceras y amputaciones de manera significativa.')}
          {faq_item('Mi mamá adulta mayor ya no puede cortarse las uñas. ¿Pueden ayudar?', 'Sí, es una de las atenciones más frecuentes. Cuidado periódico que mantiene la autonomía y previene infecciones.')}
          {faq_item('¿Atienden niños?', 'Sí, niños desde los 6 años aprox. Verrugas plantares, alteraciones de la pisada, uñas encarnadas son frecuentes a esa edad.')}
          {faq_item('¿Hacen plantillas o estudio de la pisada?', 'Hacemos evaluación clínica básica de la pisada. Para estudio biomecánico completo y plantillas a medida derivamos a podología deportiva en Concepción.')}
          {faq_item('Tengo hongos en las uñas hace años. ¿Sirve venir?', 'Sí. La onicomicosis es tratable pero requiere constancia (4-12 meses según el caso). Evaluamos qué tipo de tratamiento es el adecuado para ti.')}
        </div>'''

POD_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta la podología?","acceptedAnswer":{"@type":"Answer","text":"Desde $20.000 por sesión. Procedimientos adicionales se cotizan aparte. Solo particular."}},
    {"@type":"Question","name":"¿Con qué frecuencia conviene en diabetes?","acceptedAnswer":{"@type":"Answer","text":"Cada 2-3 meses idealmente. Reduce el riesgo de úlceras y amputaciones significativamente."}},
    {"@type":"Question","name":"¿Atienden adultos mayores?","acceptedAnswer":{"@type":"Answer","text":"Sí. Cuidado periódico de uñas y callos que mantiene autonomía y previene infecciones."}},
    {"@type":"Question","name":"¿Atienden niños?","acceptedAnswer":{"@type":"Answer","text":"Sí, desde 6 años aproximadamente. Verrugas, alteraciones de pisada, uñas encarnadas."}},
    {"@type":"Question","name":"¿Hacen plantillas a medida?","acceptedAnswer":{"@type":"Answer","text":"Evaluación clínica básica de la pisada. Para plantillas a medida derivamos a podología deportiva."}}'''

POD_RELATED = f'''
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/kinesiologia">{ARROW} Kinesiología</a></li>
            <li><a href="/blog/nutricion">{ARROW} Nutrición</a></li>
            <li><a href="/blog/cardiologia">{ARROW} Cardiología</a></li>
            <li><a href="/blog/masoterapia">{ARROW} Masoterapia</a></li>
          '''


# ===================== BLOGS LIST =====================

BLOGS = [
    {
        'slug': 'otorrinolaringologia',
        'specialty_short': 'Otorrinolaringología',
        'specialty_en': 'Otolaryngology',
        'breadcrumb_name': 'Otorrinolaringología',
        'eyebrow': 'Otorrinolaringología · Oído, nariz y garganta',
        'h1': 'Especialista en oído, nariz y garganta <em>cerca de casa</em>',
        'lead': 'Lavado de oídos, audiometría, evaluación de tabique, vértigo, amigdalitis recurrente. Con el Dr. Manuel Borrego, mes a mes según demanda.',
        'title': 'Otorrinolaringología en Carampangue · Dr. Borrego | CMC',
        'description': 'Otorrinolaringología en Carampangue: lavado de oídos $10.000, audiometría $25.000, consulta $35.000. Dr. Manuel Borrego. WhatsApp 24/7.',
        'og_title': 'Otorrinolaringología en Carampangue · Dr. Borrego',
        'og_description': 'Especialista ORL en Carampangue: oído, nariz, garganta. Sin viajar a Concepción.',
        'headline': 'Otorrinolaringología en Carampangue: cuándo consultar y procedimientos',
        'read_time': 6,
        'body_html': ORL_BODY,
        'faq_jsonld': ORL_FAQ_JSONLD,
        'related_list': ORL_RELATED,
        'sidebar_cta_desc': 'Consulta $35.000. Lavado oídos $10.000. Audiometría $25.000. Mes a mes según demanda.',
        'cta_band_h2': 'Tu salud auditiva <em>cerca de casa</em>',
        'cta_band_p': 'Agenda tu evaluación con el Dr. Borrego por WhatsApp. Coordinamos fechas según disponibilidad del especialista.',
        'wa_text': 'quiero%20agendar%20una%20hora%20de%20Otorrinolaringolog%C3%ADa.',
    },
    {
        'slug': 'traumatologia',
        'specialty_short': 'Traumatología',
        'specialty_en': 'Orthopedics',
        'breadcrumb_name': 'Traumatología',
        'eyebrow': 'Traumatología · Lesiones musculoesqueléticas',
        'h1': 'Recupera tu movilidad <em>sin viajar lejos</em>',
        'lead': 'Dolor de rodilla, lumbar, hombro, lesiones deportivas, post-cirugía. Dr. Claudio Barraza coordina mes a mes según demanda.',
        'title': 'Traumatología en Carampangue · Dr. Barraza | CMC',
        'description': 'Traumatología en Carampangue: dolor de rodilla, lumbar, hombro, lesiones deportivas, infiltraciones. Dr. Claudio Barraza. Consulta $35.000.',
        'og_title': 'Traumatología en Carampangue · Dr. Barraza',
        'og_description': 'Traumatólogo en Carampangue. Diagnóstico, manejo del dolor, infiltraciones y derivación a kinesiología.',
        'headline': 'Traumatología en Carampangue: cuándo consultar y qué resuelve',
        'read_time': 6,
        'body_html': TRAU_BODY,
        'faq_jsonld': TRAU_FAQ_JSONLD,
        'related_list': TRAU_RELATED,
        'sidebar_cta_desc': 'Consulta $35.000. Dr. Barraza coordina fechas mes a mes. Te avisamos por WhatsApp.',
        'cta_band_h2': 'Recupera el <em>movimiento</em>',
        'cta_band_p': 'Agenda tu consulta de traumatología por WhatsApp. Después coordinamos kinesiología en el mismo centro con bono Fonasa.',
        'wa_text': 'quiero%20agendar%20una%20hora%20de%20Traumatolog%C3%ADa.',
    },
    {
        'slug': 'ginecologia',
        'specialty_short': 'Ginecología',
        'specialty_en': 'Gynecology',
        'breadcrumb_name': 'Ginecología',
        'eyebrow': 'Ginecología · Salud sexual y reproductiva',
        'h1': 'Salud ginecológica especializada <em>cerca de casa</em>',
        'lead': 'Control ginecológico, dolor pélvico, sangrado anormal, embarazo, climaterio. Dr. Tirso Rejón con ecografías especializadas en la misma consulta.',
        'title': 'Ginecología en Carampangue · Dr. Rejón | CMC',
        'description': 'Ginecología y obstetricia en Carampangue. Dr. Tirso Rejón. Consulta $30.000, ecografía ginecológica $35.000. Embarazo, miomas, climaterio.',
        'og_title': 'Ginecología en Carampangue · Dr. Rejón',
        'og_description': 'Ginecólogo-obstetra con ecografías especializadas en Carampangue. Sin viajar a Concepción.',
        'headline': 'Ginecología en Carampangue: cuándo consultar y ecografías especializadas',
        'read_time': 6,
        'body_html': GINE_BODY,
        'faq_jsonld': GINE_FAQ_JSONLD,
        'related_list': GINE_RELATED,
        'sidebar_cta_desc': 'Consulta $30.000. Ecografía ginecológica $35.000 en la misma consulta. Mes a mes según demanda.',
        'cta_band_h2': 'Tu salud ginecológica <em>integral</em>',
        'cta_band_p': 'Agenda tu hora con el Dr. Rejón por WhatsApp. Para control sano la matrona también es excelente alternativa.',
        'wa_text': 'quiero%20agendar%20una%20hora%20con%20Ginecolog%C3%ADa.',
    },
    {
        'slug': 'gastroenterologia',
        'specialty_short': 'Gastroenterología',
        'specialty_en': 'Gastroenterology',
        'breadcrumb_name': 'Gastroenterología',
        'eyebrow': 'Gastroenterología · Sistema digestivo',
        'h1': 'Soluciones para tu salud <em>digestiva</em>',
        'lead': 'Reflujo, gastritis, colon irritable, hígado graso, dolor abdominal recurrente. Dr. Nicolás Quijano coordina mes a mes según demanda.',
        'title': 'Gastroenterología en Carampangue · Dr. Quijano | CMC',
        'description': 'Gastroenterología en Carampangue: reflujo, gastritis, colon irritable, hígado graso. Dr. Nicolás Quijano. Consulta $35.000. WhatsApp 24/7.',
        'og_title': 'Gastroenterología en Carampangue · Dr. Quijano',
        'og_description': 'Gastroenterólogo en Carampangue. Reflujo, gastritis, colon irritable. Sin viajar a Concepción.',
        'headline': 'Gastroenterología en Carampangue: motivos de consulta y tratamiento',
        'read_time': 6,
        'body_html': GAST_BODY,
        'faq_jsonld': GAST_FAQ_JSONLD,
        'related_list': GAST_RELATED,
        'sidebar_cta_desc': 'Consulta $35.000. Especialista coordina fechas mes a mes. Trae exámenes recientes si los tienes.',
        'cta_band_h2': 'Tu sistema digestivo <em>en buenas manos</em>',
        'cta_band_p': 'Agenda con el Dr. Quijano por WhatsApp. Reflujo, gastritis, colon irritable y otras patologías digestivas.',
        'wa_text': 'quiero%20agendar%20una%20hora%20de%20Gastroenterolog%C3%ADa.',
    },
    {
        'slug': 'endodoncia',
        'specialty_short': 'Endodoncia',
        'specialty_en': 'Endodontic',
        'breadcrumb_name': 'Endodoncia',
        'eyebrow': 'Endodoncia · Tratamiento de conducto',
        'h1': 'Salva tu diente <em>con tratamiento de conducto</em>',
        'lead': 'Endodoncia con el Dr. Fernando Fredes para conservar tus dientes naturales. Empezamos siempre con evaluación de dentista general.',
        'title': 'Endodoncia en Carampangue · Tratamiento de conducto | CMC',
        'description': 'Endodoncia en Carampangue con Dr. Fernando Fredes. Tratamiento de conducto para conservar tu diente natural. Evaluación inicial $25.000.',
        'og_title': 'Endodoncia en Carampangue · Dr. Fredes',
        'og_description': 'Tratamiento de conducto para salvar tu diente. Empezamos con dentista general.',
        'headline': 'Endodoncia en Carampangue: cuándo es necesaria y cómo es el proceso',
        'read_time': 6,
        'body_html': ENDO_BODY,
        'faq_jsonld': ENDO_FAQ_JSONLD,
        'related_list': ENDO_RELATED,
        'sidebar_cta_desc': 'Empezamos con dentista general ($25.000). Si requiere endodoncia, derivamos al Dr. Fredes con presupuesto detallado.',
        'cta_band_h2': 'Conserva tu <em>diente natural</em>',
        'cta_band_p': 'Agenda evaluación con dentista general por WhatsApp. Si la endodoncia es necesaria, te derivamos al Dr. Fredes.',
        'wa_text': 'tengo%20dolor%20de%20muela%2C%20creo%20que%20necesito%20endodoncia.',
    },
    {
        'slug': 'implantologia',
        'specialty_short': 'Implantología',
        'specialty_en': 'Implant',
        'breadcrumb_name': 'Implantología',
        'eyebrow': 'Implantología · Implantes dentales',
        'h1': 'Recupera tu sonrisa <em>con implantes</em>',
        'lead': 'Implantes dentales con la Dra. Aurora Valdés. Solución estable y duradera para reemplazar dientes perdidos. Desde $650.000 (incluye corona).',
        'title': 'Implantología en Carampangue · Dra. Valdés desde $650.000 | CMC',
        'description': 'Implantes dentales en Carampangue con Dra. Aurora Valdés. Desde $650.000 incluye implante + corona. Empezamos con evaluación de dentista general.',
        'og_title': 'Implantología en Carampangue · Dra. Valdés',
        'og_description': 'Implantes dentales: tornillo de titanio + corona. Solución a largo plazo. Carampangue.',
        'headline': 'Implantología en Carampangue: implantes dentales paso a paso',
        'read_time': 7,
        'body_html': IMP_BODY,
        'faq_jsonld': IMP_FAQ_JSONLD,
        'related_list': IMP_RELATED,
        'sidebar_cta_desc': 'Implantes desde $650.000. Empezamos con evaluación dental general. Aceptamos efectivo, transferencia, débito y crédito.',
        'cta_band_h2': 'Una sonrisa <em>completa</em>',
        'cta_band_p': 'Agenda tu evaluación inicial por WhatsApp. La Dra. Valdés evalúa tu caso y entrega presupuesto detallado.',
        'wa_text': 'quiero%20evaluar%20un%20implante%20dental.',
    },
    {
        'slug': 'masoterapia',
        'specialty_short': 'Masoterapia',
        'specialty_en': 'PhysicalTherapy',
        'breadcrumb_name': 'Masoterapia',
        'eyebrow': 'Masoterapia · Masaje terapéutico',
        'h1': 'Libera la tensión <em>de tu cuerpo</em>',
        'lead': 'Masaje terapéutico con Paola Acosta. 20 min ($17.990) para zona específica o 40 min ($26.990) cuerpo completo. Tensión, contracturas, estrés.',
        'title': 'Masoterapia en Carampangue · 20 min $17.990 / 40 min $26.990 | CMC',
        'description': 'Masoterapia en Carampangue con Paola Acosta. Sesión 20 min $17.990 (zona específica) o 40 min $26.990 (cuerpo completo). Tensión, contracturas.',
        'og_title': 'Masoterapia en Carampangue · 20/40 min',
        'og_description': 'Masaje terapéutico para tensión, contracturas y estrés. Carampangue, Provincia de Arauco.',
        'headline': 'Masoterapia en Carampangue: tipos de sesión y cuándo te conviene',
        'read_time': 5,
        'body_html': MAS_BODY,
        'faq_jsonld': MAS_FAQ_JSONLD,
        'related_list': MAS_RELATED,
        'sidebar_cta_desc': '20 min $17.990 (zona específica) o 40 min $26.990 (cuerpo completo). Confirmación inmediata por WhatsApp.',
        'cta_band_h2': 'Tu cuerpo <em>te lo agradecerá</em>',
        'cta_band_p': 'Agenda tu sesión de masoterapia por WhatsApp. Elige 20 min para zona específica o 40 min para cuerpo completo.',
        'wa_text': 'quiero%20agendar%20una%20sesi%C3%B3n%20de%20Masoterapia.',
    },
    {
        'slug': 'nutricion',
        'specialty_short': 'Nutrición',
        'specialty_en': 'Nutrition',
        'breadcrumb_name': 'Nutrición',
        'eyebrow': 'Nutrición · Plan alimentario personalizado',
        'h1': 'Aprende a comer <em>mejor</em>',
        'lead': 'Plan alimentario personalizado con Gisela Pinto. Bono Fonasa $4.770 (se emite en el centro) o particular $20.000. Bioimpedanciometría $20.000.',
        'title': 'Nutrición en Carampangue · Bono Fonasa $4.770 | CMC',
        'description': 'Nutrición en Carampangue con Gisela Pinto. Bono Fonasa $4.770 (se emite en el centro) o particular $20.000. Bioimpedanciometría disponible.',
        'og_title': 'Nutrición en Carampangue · Bono Fonasa $4.770',
        'og_description': 'Plan alimentario personalizado. Diabetes, baja de peso, embarazo, deportistas.',
        'headline': 'Nutrición en Carampangue: cuándo consultar y bioimpedanciometría',
        'read_time': 6,
        'body_html': NUT_BODY,
        'faq_jsonld': NUT_FAQ_JSONLD,
        'related_list': NUT_RELATED,
        'sidebar_cta_desc': 'Bono Fonasa $4.770 emitido en el centro o particular $20.000. Bioimpedanciometría $20.000 si corresponde.',
        'cta_band_h2': 'Una alimentación <em>que se sostenga</em>',
        'cta_band_p': 'Agenda con Gisela por WhatsApp. Bono Fonasa o particular, según prefieras.',
        'wa_text': 'quiero%20agendar%20una%20hora%20de%20Nutrici%C3%B3n.',
    },
    {
        'slug': 'psicologia-adulto',
        'specialty_short': 'Psicología Adulto',
        'specialty_en': 'Psychiatric',
        'breadcrumb_name': 'Psicología Adulto',
        'eyebrow': 'Psicología Adulto · Salud mental',
        'h1': 'Cuida tu salud mental <em>con apoyo profesional</em>',
        'lead': 'Ansiedad, depresión, duelo, estrés, problemas de pareja. Con Jorge Montalba o Juan Pablo Rodríguez. Bono Fonasa $14.420 / particular $20.000.',
        'title': 'Psicología Adulto en Carampangue · Bono Fonasa $14.420 | CMC',
        'description': 'Psicología adulto en Carampangue. Ansiedad, depresión, duelo, estrés. Bono Fonasa $14.420 / particular $20.000. Sesión 45 min. Confidencial.',
        'og_title': 'Psicología Adulto en Carampangue · Bono Fonasa $14.420',
        'og_description': 'Psicoterapia para adultos. Ansiedad, depresión, duelo, problemas de pareja. Carampangue.',
        'headline': 'Psicología Adulto en Carampangue: cuándo consultar y qué esperar',
        'read_time': 6,
        'body_html': PSIA_BODY,
        'faq_jsonld': PSIA_FAQ_JSONLD,
        'related_list': PSIA_RELATED,
        'sidebar_cta_desc': 'Bono Fonasa $14.420 (se emite en el centro) o particular $20.000. Sesión 45 min. Espacio confidencial.',
        'cta_band_h2': 'Tu bienestar <em>importa</em>',
        'cta_band_p': 'Agenda tu hora de psicología por WhatsApp. Espacio confidencial, sin juicios.',
        'wa_text': 'quiero%20agendar%20una%20hora%20de%20Psicolog%C3%ADa%20Adulto.',
    },
    {
        'slug': 'psicologia-infantil',
        'specialty_short': 'Psicología Infantil',
        'specialty_en': 'PediatricPsychology',
        'breadcrumb_name': 'Psicología Infantil',
        'eyebrow': 'Psicología Infantil · Niños y adolescentes',
        'h1': 'Apoyo emocional para <em>niños y adolescentes</em>',
        'lead': 'Ansiedad escolar, cambios de conducta, duelo, problemas de aprendizaje. Con Jorge Montalba. Bono Fonasa $14.420 / particular $20.000.',
        'title': 'Psicología Infantil en Carampangue · Bono Fonasa $14.420 | CMC',
        'description': 'Psicología infantil y adolescente en Carampangue. Ansiedad escolar, cambios de conducta, duelo. Bono Fonasa $14.420 / particular $20.000.',
        'og_title': 'Psicología Infantil en Carampangue · Bono Fonasa',
        'og_description': 'Apoyo psicológico para niños y adolescentes. Familia y colegio. Carampangue.',
        'headline': 'Psicología Infantil en Carampangue: cuándo consultar y proceso con niños',
        'read_time': 6,
        'body_html': PSII_BODY,
        'faq_jsonld': PSII_FAQ_JSONLD,
        'related_list': PSII_RELATED,
        'sidebar_cta_desc': 'Bono Fonasa $14.420 / particular $20.000. Primera sesión es con padres. Atendemos hasta 17 años.',
        'cta_band_h2': 'Acompañamos su <em>desarrollo emocional</em>',
        'cta_band_p': 'Agenda hora con Jorge Montalba por WhatsApp. La primera sesión es con padres para conocer el contexto.',
        'wa_text': 'quiero%20agendar%20una%20hora%20de%20Psicolog%C3%ADa%20Infantil.',
    },
    {
        'slug': 'fonoaudiologia',
        'specialty_short': 'Fonoaudiología',
        'specialty_en': 'SpeechPathology',
        'breadcrumb_name': 'Fonoaudiología',
        'eyebrow': 'Fonoaudiología · Habla, lenguaje, voz, deglución',
        'h1': 'Habla, voz y lenguaje <em>cerca de casa</em>',
        'lead': 'Retraso del lenguaje, tartamudez, disfonía, disfagia post-ACV. Con Juana Arratia. Atendemos niños y adultos.',
        'title': 'Fonoaudiología en Carampangue · Niños y adultos | CMC',
        'description': 'Fonoaudiología en Carampangue: lenguaje infantil, voz profesional, disfagia. Con Juana Arratia. Sesiones $25.000-$50.000. WhatsApp 24/7.',
        'og_title': 'Fonoaudiología en Carampangue · Habla y voz',
        'og_description': 'Lenguaje infantil, voz profesional, deglución. Juana Arratia. Carampangue.',
        'headline': 'Fonoaudiología en Carampangue: áreas de trabajo y cuándo consultar',
        'read_time': 6,
        'body_html': FON_BODY,
        'faq_jsonld': FON_FAQ_JSONLD,
        'related_list': FON_RELATED,
        'sidebar_cta_desc': 'Sesiones $25.000-$50.000 según tipo. Niños desde 2 años y adultos. Coordinamos con otorrino cuando corresponde.',
        'cta_band_h2': 'Comunícate <em>mejor</em>',
        'cta_band_p': 'Agenda tu evaluación con Juana por WhatsApp. Atendemos niños y adultos en todas las áreas de fonoaudiología.',
        'wa_text': 'quiero%20agendar%20una%20hora%20de%20Fonoaudiolog%C3%ADa.',
    },
    {
        'slug': 'matrona',
        'specialty_short': 'Matrona',
        'specialty_en': 'Midwifery',
        'breadcrumb_name': 'Matrona',
        'eyebrow': 'Matrona · Salud sexual y reproductiva',
        'h1': 'Salud de la mujer <em>en cada etapa</em>',
        'lead': 'PAP, anticoncepción, control de embarazo, climaterio. Con Sarai Gómez. Tarifa preferencial Fonasa $16.000 / particular $30.000.',
        'title': 'Matrona en Carampangue · Fonasa $16.000 | CMC',
        'description': 'Matrona en Carampangue con Sarai Gómez. Tarifa preferencial Fonasa $16.000 / particular $30.000. PAP, anticoncepción, embarazo, climaterio.',
        'og_title': 'Matrona en Carampangue · Fonasa $16.000',
        'og_description': 'Control ginecológico, PAP, anticoncepción, embarazo. Sarai Gómez. Carampangue.',
        'headline': 'Matrona en Carampangue: salud sexual y reproductiva integral',
        'read_time': 6,
        'body_html': MAT_BODY,
        'faq_jsonld': MAT_FAQ_JSONLD,
        'related_list': MAT_RELATED,
        'sidebar_cta_desc': 'Fonasa preferencial $16.000 / particular $30.000. Con PAP $25.000 / $30.000. Atendemos adolescentes y adultas.',
        'cta_band_h2': 'Tu salud reproductiva <em>en buenas manos</em>',
        'cta_band_p': 'Agenda hora con Sarai por WhatsApp. PAP, anticoncepción, embarazo bajo riesgo, climaterio.',
        'wa_text': 'quiero%20agendar%20una%20hora%20con%20Matrona.',
    },
    {
        'slug': 'podologia',
        'specialty_short': 'Podología',
        'specialty_en': 'Podiatry',
        'breadcrumb_name': 'Podología',
        'eyebrow': 'Podología · Salud de los pies',
        'h1': 'Cuida tus pies <em>cerca de casa</em>',
        'lead': 'Uñas encarnadas, hongos, callos, pie diabético. Con Andrea Guevara. Atención especializada y prevención de complicaciones.',
        'title': 'Podología en Carampangue · Pie diabético | CMC',
        'description': 'Podología en Carampangue con Andrea Guevara. Uñas encarnadas, hongos, callos, pie diabético. Sesiones desde $20.000. WhatsApp 24/7.',
        'og_title': 'Podología en Carampangue · Pie diabético',
        'og_description': 'Cuidado profesional de pies. Pie diabético, uñas encarnadas, callos. Andrea Guevara. Carampangue.',
        'headline': 'Podología en Carampangue: tratamientos y atención al pie diabético',
        'read_time': 5,
        'body_html': POD_BODY,
        'faq_jsonld': POD_FAQ_JSONLD,
        'related_list': POD_RELATED,
        'sidebar_cta_desc': 'Sesiones desde $20.000. Atención especializada en pie diabético. Adultos mayores, deportistas, niños.',
        'cta_band_h2': 'Tus pies <em>en buenas manos</em>',
        'cta_band_p': 'Agenda con Andrea por WhatsApp. Cuidado profesional, prevención de complicaciones, atención especializada en diabetes.',
        'wa_text': 'quiero%20agendar%20una%20hora%20de%20Podolog%C3%ADa.',
    },
]


def main():
    for cfg in BLOGS:
        out_path = BLOG_DIR / f'{cfg["slug"]}.html'
        out_path.write_text(build_blog(cfg), encoding="utf-8")
        size_kb = out_path.stat().st_size // 1024
        print(f"[OK] /blog/{cfg['slug']:25s}  {size_kb} KB")


if __name__ == "__main__":
    main()
