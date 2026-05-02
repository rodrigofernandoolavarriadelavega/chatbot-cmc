"""
Generador de blogs por especialidad — usa cardiologia.html como template
y reemplaza únicamente las secciones específicas de cada blog.

Uso: python3 scripts/gen_blogs_specialty.py
"""
import re
from pathlib import Path

BLOG_DIR = Path(__file__).parent.parent / "templates" / "blog"
TEMPLATE = (BLOG_DIR / "cardiologia.html").read_text(encoding="utf-8")


def build_blog(cfg: dict) -> str:
    """Toma el template cardiología y reemplaza por la config de la especialidad."""
    h = TEMPLATE

    # 1. Metadatos head
    h = h.replace(
        'Cardiología en Carampangue — Cuándo consultar y exámenes preventivos | CMC',
        cfg['title']
    )
    h = h.replace(
        'Guía clara sobre cardiología: señales de alerta, exámenes preventivos, hábitos cardioprotectores y cuándo agendar con el cardiólogo. Centro Médico Carampangue, Provincia de Arauco.',
        cfg['description']
    )
    h = h.replace(
        'Cardiología: cuándo consultar y exámenes preventivos | CMC',
        cfg['og_title']
    )
    h = h.replace(
        'Señales de alerta, exámenes y hábitos para cuidar tu corazón. Atención cardiológica en Carampangue, Provincia de Arauco.',
        cfg['og_description']
    )
    h = h.replace(
        'https://agentecmc.cl/blog/cardiologia',
        f'https://agentecmc.cl/blog/{cfg["slug"]}'
    )

    # 2. Schema.org WebPage
    h = h.replace(
        '"headline": "Cardiología: cuándo consultar y exámenes preventivos"',
        f'"headline": "{cfg["headline"]}"'
    )
    h = h.replace(
        '"description": "Guía sobre cardiología, señales de alerta, exámenes preventivos y hábitos cardioprotectores."',
        f'"description": "{cfg["description"]}"'
    )
    h = h.replace(
        '"about": {"@type":"MedicalSpecialty","name":"Cardiology"}',
        f'"about": {{"@type":"MedicalSpecialty","name":"{cfg["specialty_en"]}"}}'
    )

    # 3. Schema.org Breadcrumb
    h = h.replace(
        '{"@type":"ListItem","position":3,"name":"Cardiología","item":"https://agentecmc.cl/blog/cardiologia"}',
        f'{{"@type":"ListItem","position":3,"name":"{cfg["breadcrumb_name"]}","item":"https://agentecmc.cl/blog/{cfg["slug"]}"}}'
    )

    # 4. Schema.org FAQ - reemplaza el bloque entero
    faq_jsonld_block = re.search(
        r'(<script type="application/ld\+json">\s*\{\s*"@context":\s*"https://schema\.org",\s*"@type":\s*"FAQPage",\s*"mainEntity":\s*\[)(.+?)(\s*\]\s*\}\s*</script>)',
        h, re.DOTALL
    )
    if faq_jsonld_block:
        h = h.replace(faq_jsonld_block.group(0),
            faq_jsonld_block.group(1) + cfg['faq_jsonld'] + faq_jsonld_block.group(3))

    # 5. Hero — breadcrumb, eyebrow, h1, lead
    h = h.replace(
        '<span class="current">Cardiología</span>',
        f'<span class="current">{cfg["breadcrumb_name"]}</span>'
    )
    h = h.replace(
        '<div class="eyebrow">Cardiología · Salud cardiovascular</div>',
        f'<div class="eyebrow">{cfg["eyebrow"]}</div>'
    )
    h = h.replace(
        '<h1 class="blog-h1">Tu corazón merece <em>los mejores cuidados</em></h1>',
        f'<h1 class="blog-h1">{cfg["h1"]}</h1>'
    )
    h = h.replace(
        'Las enfermedades cardiovasculares son la primera causa de muerte en Chile. Aprende a reconocer las señales de alerta, cuáles son los exámenes preventivos y cómo cuidar tu corazón día a día.',
        cfg['lead']
    )
    h = h.replace(
        '7 min de lectura',
        f'{cfg["read_time"]} min de lectura'
    )

    # 6. Body content completo — reemplaza desde primer <h2> hasta antes de </article>
    body_match = re.search(
        r'(<article class="blog-content">\s*\n)(.+?)(\s*</article>)',
        h, re.DOTALL
    )
    if body_match:
        h = h.replace(body_match.group(0),
            body_match.group(1) + '\n        ' + cfg['body_html'] + body_match.group(3))

    # 7. Sidebar — h4 CTA + descripción + link CTA + texto WhatsApp
    h = h.replace(
        '<p>Cardiología con confirmación inmediata por WhatsApp. Atención particular.</p>',
        f'<p>{cfg["sidebar_cta_desc"]}</p>'
    )
    h = h.replace(
        'Agendar Cardiología',
        f'Agendar {cfg["specialty_short"]}'
    )

    # 8. Sidebar related-list — reemplaza el bloque
    related_block = re.search(
        r'(<ul class="related-list">)(.+?)(</ul>)',
        h, re.DOTALL
    )
    if related_block:
        h = h.replace(related_block.group(0),
            related_block.group(1) + cfg['related_list'] + related_block.group(3))

    # 9. CTA Band — h2, p
    h = h.replace(
        '<h2>Tu corazón <em>no espera</em></h2>',
        f'<h2>{cfg["cta_band_h2"]}</h2>'
    )
    h = h.replace(
        '<p>Agenda tu consulta cardiológica por WhatsApp. Disponibilidad real consultable las 24 horas.</p>',
        f'<p>{cfg["cta_band_p"]}</p>'
    )

    # 10. Reemplaza textos prellenados WhatsApp (por slug)
    h = h.replace(
        'quiero%20agendar%20una%20consulta%20de%20Cardiolog%C3%ADa.',
        cfg['wa_text']
    )
    # También en aria-label
    h = h.replace(
        'aria-label="Agendar Cardiología por WhatsApp"',
        f'aria-label="Agendar {cfg["specialty_short"]} por WhatsApp"'
    )

    return h


# ===================== CONFIGS =====================

# helper para SVG flecha del related-list
ARROW = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>'

# helper para chevron FAQ
FAQ_CHEVRON = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>'

# helper para SVG check (callout-info)
INFO_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>'

# WhatsApp icon SVG
WA_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M17.5 14.4c-.3-.1-1.7-.8-2-.9-.3-.1-.5-.1-.7.1-.2.3-.7.9-.9 1.1-.2.2-.3.2-.6.1-.3-.1-1.2-.4-2.3-1.4-.8-.8-1.4-1.7-1.5-2-.2-.3 0-.4.1-.6.1-.1.3-.3.4-.5.1-.2.2-.3.3-.5.1-.2 0-.4 0-.5 0-.1-.7-1.6-.9-2.2-.2-.6-.5-.5-.7-.5h-.6c-.2 0-.5.1-.8.4-.3.3-1 1-1 2.5s1.1 2.9 1.2 3.1c.1.2 2.1 3.2 5.1 4.5.7.3 1.3.5 1.7.6.7.2 1.3.2 1.8.1.6-.1 1.7-.7 2-1.4.2-.6.2-1.2.2-1.4-.1-.1-.3-.2-.6-.3M12 2C6.5 2 2 6.5 2 12c0 1.8.5 3.5 1.4 5L2 22l5.1-1.3c1.4.8 3 1.3 4.9 1.3 5.5 0 10-4.5 10-10S17.5 2 12 2"/></svg>'


def make_cta_inline(title_em_word: str, title_rest: str, desc: str, wa_text: str) -> str:
    """Genera el CTA inline navy con WhatsApp."""
    title = title_rest.replace(f'[{title_em_word}]', f'<em>{title_em_word}</em>')
    return f'''<div class="cta-inline">
          <div>
            <h3>{title}</h3>
            <p>{desc}</p>
          </div>
          <a href="https://wa.me/56966610737?text=Hola%2C%20{wa_text}" target="_blank" rel="noopener" class="btn btn-wa">
            {WA_SVG}
            Agendar por WhatsApp
          </a>
        </div>'''


def callout_info(text: str) -> str:
    return f'''<div class="callout callout-info">
          <div class="callout-icon">{INFO_ICON}</div>
          <p>{text}</p>
        </div>'''


def faq_item(q: str, a: str) -> str:
    return f'''<details>
            <summary>{q}
              {FAQ_CHEVRON}
            </summary>
            <div class="answer">{a}</div>
          </details>'''


# ===================== BLOG: MEDICINA GENERAL =====================

MG_BODY = f'''
        <h2>¿Qué es la <em>Medicina General</em>?</h2>
        <p>La medicina general es la puerta de entrada al sistema de salud. Es la consulta donde tu médico evalúa tu estado general, resuelve problemas comunes (resfríos, dolores, controles) y, si es necesario, te deriva al especialista correcto.</p>
        <p>En Carampangue es la especialidad más solicitada del CMC: <strong>la mayoría de las personas que se atienden con nosotros parten por aquí</strong>. Atendemos con bono Fonasa o particular, según prefieras.</p>

        {callout_info('<strong>Bono Fonasa $7.880</strong>: lo emitimos en el mismo centro con huella biométrica, sin que tengas que ir al banco ni a oficinas Fonasa. Trae solo tu carnet.')}

        <h2>¿Cuándo consultar con un <em>médico general</em>?</h2>
        <p>Hay síntomas que la medicina general puede resolver directamente, otros donde te orientamos para llegar al especialista correcto:</p>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v8"/><path d="m4.93 10.93 1.41 1.41"/><path d="M2 18h2"/><path d="M20 18h2"/><path d="m19.07 10.93-1.41 1.41"/><path d="M22 22H2"/><path d="m8 6 4-4 4 4"/><path d="M16 18a4 4 0 0 0-8 0"/></svg></div><div class="body"><strong>Resfrío y fiebre</strong><span>Tos, congestión, fiebre persistente más de 3 días o decaimiento marcado.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 2"/></svg></div><div class="body"><strong>Control de salud anual</strong><span>Chequeo preventivo: presión, examen físico, indicación de exámenes según edad.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11h.01"/><path d="M15 11h.01"/><path d="M12 17a3 3 0 0 1-3-3"/><circle cx="12" cy="12" r="10"/></svg></div><div class="body"><strong>Dolores comunes</strong><span>Dolor de cabeza, lumbar, abdominal o muscular sin causa clara.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></div><div class="body"><strong>Renovación de licencias</strong><span>Licencias médicas, certificados de salud para trabajo o estudios.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div><div class="body"><strong>Hipertensión y diabetes</strong><span>Control crónico, ajuste de medicamentos y derivación a especialistas.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg></div><div class="body"><strong>Atención de niños</strong><span>Resfríos, fiebre y controles. Para temas más complejos derivamos a especialistas.</span></div></div>
        </div>

        <h2>Nuestros médicos en <em>Medicina General</em></h2>
        <p>El CMC tiene 3 médicos generales que cubren toda la semana, mañana y tarde:</p>
        <ul>
          <li><strong>Dr. Rodrigo Olavarría</strong> — Lunes a sábado. Atiende también medicina familiar, controles crónicos y morbilidad.</li>
          <li><strong>Dr. Andrés Abarca</strong> — Lunes a viernes. Foco en morbilidad y atención general.</li>
          <li><strong>Dr. Alonso Márquez</strong> — Lunes, miércoles y viernes. Medicina familiar — atención integral del grupo familiar (niños, adultos, adultos mayores).</li>
        </ul>

        {make_cta_inline('Medicina General', 'Agenda tu hora de [Medicina General]', 'Bono Fonasa $7.880 (se emite en el centro) o particular $25.000. Confirmación inmediata por WhatsApp 24/7.', 'quiero%20agendar%20una%20hora%20de%20Medicina%20General.')}

        <h2>Hábitos para una <em>vida saludable</em></h2>
        <p>La consulta de medicina general también es el espacio para conversar sobre prevención. Cuatro pilares básicos:</p>
        <div class="habit-grid">
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="13" cy="4" r="2"/><path d="m6 14 2.5-3 2.5 1 3-3 4 5"/><path d="M11 12v9"/><path d="M16 21v-5"/><path d="M3 21h18"/></svg></div>
            <h3>Actividad física</h3>
            <p>150 minutos semanales de ejercicio moderado reducen riesgo cardiovascular, diabetes y depresión.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg></div>
            <h3>Alimentación equilibrada</h3>
            <p>Más frutas, verduras, legumbres y pescado. Menos sal, azúcar y alimentos ultraprocesados.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg></div>
            <h3>Sueño reparador</h3>
            <p>7–9 horas por noche. La privación crónica eleva el riesgo de hipertensión y depresión.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg></div>
            <h3>No fumes</h3>
            <p>El tabaco es factor de riesgo de cáncer, EPOC y enfermedad cardiovascular. Te apoyamos para dejar de fumar.</p>
          </div>
        </div>

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta la consulta de Medicina General?', '<strong>$7.880 con bono Fonasa</strong> (se emite en el centro con huella biométrica) o <strong>$25.000 particular</strong>. Sin sobreprecios ni cobros adicionales.')}
          {faq_item('¿Cómo funciona el bono Fonasa en el CMC?', 'Lo emitimos directamente en el centro con tu huella biométrica. No tienes que ir al banco ni hacer trámites previos. Trae solo tu cédula de identidad.')}
          {faq_item('¿Atienden niños?', 'Sí. Nuestros médicos generales atienden niños para morbilidad común (resfríos, fiebre, controles). Para temas más complejos derivamos a especialistas pediátricos en Concepción.')}
          {faq_item('¿Qué pasa si necesito un especialista?', 'Tu médico general te entregará la orden de derivación al especialista correcto. En CMC tenemos 19 especialidades — muchas veces puedes seguir el tratamiento en el mismo centro.')}
          {faq_item('¿Hacen licencias médicas?', 'Sí. Si tu médico determina que necesitas reposo, te entrega la licencia electrónica al momento. También certificados de salud para trabajo o estudios.')}
          {faq_item('¿Dan hora para hoy?', 'Generalmente sí. Tenemos cupos diarios reservados para morbilidad. Escribe por WhatsApp y nuestro asistente revisa disponibilidad inmediata.')}
        </div>'''

MG_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta la consulta de Medicina General?","acceptedAnswer":{"@type":"Answer","text":"$7.880 con bono Fonasa (se emite en el centro con huella biométrica) o $25.000 particular. Sin sobreprecios ni cobros adicionales."}},
    {"@type":"Question","name":"¿Cómo funciona el bono Fonasa en el CMC?","acceptedAnswer":{"@type":"Answer","text":"Lo emitimos directamente en el centro con tu huella biométrica. No tienes que ir al banco ni hacer trámites previos. Trae solo tu cédula de identidad."}},
    {"@type":"Question","name":"¿Atienden niños?","acceptedAnswer":{"@type":"Answer","text":"Sí. Nuestros médicos generales atienden niños para morbilidad común. Para temas más complejos derivamos a especialistas pediátricos."}},
    {"@type":"Question","name":"¿Hacen licencias médicas?","acceptedAnswer":{"@type":"Answer","text":"Sí. Licencias electrónicas al momento de la consulta cuando corresponde, y certificados de salud para trabajo o estudios."}},
    {"@type":"Question","name":"¿Dan hora para hoy?","acceptedAnswer":{"@type":"Answer","text":"Generalmente sí. Tenemos cupos diarios reservados para morbilidad. Escribe por WhatsApp y nuestro asistente revisa disponibilidad inmediata."}}'''

MG_RELATED = f'''
            <li><a href="/blog/cardiologia">{ARROW} Cardiología</a></li>
            <li><a href="/blog/kinesiologia">{ARROW} Kinesiología</a></li>
            <li><a href="/blog/nutricion">{ARROW} Nutrición</a></li>
            <li><a href="/blog/psicologia-adulto">{ARROW} Psicología Adulto</a></li>
            <li><a href="/blog/odontologia-general">{ARROW} Odontología General</a></li>
          '''


# ===================== BLOG: ORTODONCIA =====================

ORT_BODY = f'''
        <h2>¿Qué es la <em>Ortodoncia</em>?</h2>
        <p>La ortodoncia es la rama de la odontología que corrige la posición de los dientes y los huesos de la mandíbula. Mejora la mordida, la masticación, la salud dental a largo plazo y, por supuesto, la sonrisa.</p>
        <p>En el CMC ofrecemos tratamiento con <strong>brackets metálicos tradicionales</strong> y seguimiento mes a mes con la <strong>Dra. Daniela Castillo</strong>, ortodoncista que viene desde Concepción a coordinar fechas según demanda.</p>

        {callout_info('<strong>El proceso parte siempre con tu dentista general</strong>: ella te evalúa, indica radiografías y, si todo está OK, te deriva a la ortodoncista. Esto evita problemas (caries, encías inflamadas) que comprometan el tratamiento.')}

        <h2>¿Cuándo necesitas <em>ortodoncia</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0z"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><path d="M9 9h.01"/><path d="M15 9h.01"/></svg></div><div class="body"><strong>Dientes apiñados o chuecos</strong><span>Apiñamiento, dientes torcidos o que no calzan bien al cerrar la boca.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M3 12h18M3 18h18"/></svg></div><div class="body"><strong>Mordida cruzada o abierta</strong><span>Los dientes superiores e inferiores no encajan bien al masticar.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 12h8"/></svg></div><div class="body"><strong>Espacios entre dientes</strong><span>Diastemas — separaciones entre dientes que afectan estética o función.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 4 9l8 7 8-7-8-7z"/></svg></div><div class="body"><strong>Problemas para masticar</strong><span>Dolor o dificultad al masticar por mordida deficiente.</span></div></div>
        </div>

        <h2>Cómo es el <em>tratamiento</em></h2>
        <h3>1. Consulta inicial con dentista general</h3>
        <p>Tu primera cita es con la <strong>Dra. Javiera Burgos</strong> o <strong>Dr. Carlos Jiménez</strong>. Ellos evalúan tu boca completa, descartan caries o problemas de encías, y te indican radiografías para el plan de tratamiento.</p>

        <h3>2. Presupuesto: $15.000 (gratis si comienzas ese día)</h3>
        <p>Si decides empezar tu tratamiento previo en la misma visita (limpieza, tapaduras o lo que necesites antes de los brackets), <strong>el presupuesto te sale gratis</strong>. Solo pagas la acción que se realice ese día.</p>

        <h3>3. Derivación con la ortodoncista</h3>
        <p>Una vez tu boca está lista, la dentista general gestiona tu derivación con la Dra. Castillo. Ella define los días según su disponibilidad y te avisamos por WhatsApp.</p>

        <h3>4. Instalación de brackets — $120.000</h3>
        <p>Sesión donde se cementan los brackets a tus dientes. Duración 1–2 horas, sin dolor. Al inicio puede haber molestia leve por presión.</p>

        <h3>5. Controles mensuales — $30.000 cada uno</h3>
        <p>Visitas cada 4–6 semanas para ajustar arcos y avanzar el tratamiento. Duración promedio: 18 a 24 meses.</p>

        {make_cta_inline('ortodoncia', 'Agenda tu evaluación de [ortodoncia]', 'Primera cita con dentista general · presupuesto $15.000 (gratis si comienzas tratamiento ese día). Confirmamos por WhatsApp.', 'quiero%20iniciar%20tratamiento%20de%20Ortodoncia.')}

        <h2>Cuidados durante el <em>tratamiento</em></h2>
        <div class="habit-grid">
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg></div>
            <h3>Cepilla después de comer</h3>
            <p>Cepillo dental + cepillo interproximal + enjuague con flúor. La higiene es CRÍTICA con brackets.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="6" y1="6" x2="18" y2="18"/></svg></div>
            <h3>Evita ciertos alimentos</h3>
            <p>Caramelos duros, chicle, hielo, frutos secos. Pueden romper brackets o doblar arcos.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></div>
            <h3>No te pierdas controles</h3>
            <p>Cada control ajusta presión y avance. Saltarte una cita atrasa el tratamiento varias semanas.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v4"/><path d="M12 18v4"/><path d="m4.93 4.93 2.83 2.83"/><path d="m16.24 16.24 2.83 2.83"/><path d="M2 12h4"/><path d="M18 12h4"/><path d="m4.93 19.07 2.83-2.83"/><path d="m16.24 7.76 2.83-2.83"/></svg></div>
            <h3>Avísanos si algo se rompe</h3>
            <p>Si se sale un bracket o se sale el arco, escríbenos por WhatsApp. Coordinamos hora de urgencia.</p>
          </div>
        </div>

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta la ortodoncia?', '<strong>Instalación de brackets metálicos $120.000 · Controles $30.000 cada uno</strong>. La duración es de 18–24 meses, depende de cada caso. El presupuesto inicial cuesta $15.000 (gratis si comienzas tratamiento ese día).')}
          {faq_item('¿Por qué no puedo agendar directo con la ortodoncista?', 'Para que tu tratamiento sea exitoso, primero necesitamos descartar caries, problemas de encías o de mordida que requieran tratamiento previo. La dentista general es quien evalúa eso y te entrega la orden de derivación.')}
          {faq_item('¿Cuánto demora el tratamiento?', 'Promedio 18 a 24 meses. Casos simples pueden ser menos. La constancia con los controles mensuales es lo que define el tiempo total.')}
          {faq_item('¿Atienden ortodoncia para niños?', 'Sí. Hay momentos clave en la dentición mixta (8–12 años) donde la ortodoncia previene problemas mayores. La dentista general evalúa si tu hijo necesita tratamiento ahora o esperar.')}
          {faq_item('¿Aceptan Fonasa?', 'No. La ortodoncia es atención particular. Aceptamos efectivo, transferencia, débito y crédito. Algunos seguros complementarios pueden cubrir parte vía reembolso — consulta con el tuyo.')}
        </div>'''

ORT_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta la ortodoncia?","acceptedAnswer":{"@type":"Answer","text":"Instalación de brackets metálicos $120.000 y controles $30.000 cada uno. Duración 18-24 meses. Presupuesto inicial $15.000 (gratis si comienzas tratamiento ese día)."}},
    {"@type":"Question","name":"¿Por qué no puedo agendar directo con la ortodoncista?","acceptedAnswer":{"@type":"Answer","text":"Para que el tratamiento sea exitoso primero hay que descartar caries, problemas de encías o de mordida que requieran tratamiento previo. La dentista general lo evalúa y entrega la orden de derivación."}},
    {"@type":"Question","name":"¿Cuánto demora el tratamiento de ortodoncia?","acceptedAnswer":{"@type":"Answer","text":"Promedio 18 a 24 meses. Casos simples pueden durar menos."}},
    {"@type":"Question","name":"¿Atienden ortodoncia para niños?","acceptedAnswer":{"@type":"Answer","text":"Sí. La dentista general evalúa si el niño necesita tratamiento ahora o esperar."}},
    {"@type":"Question","name":"¿Aceptan Fonasa en ortodoncia?","acceptedAnswer":{"@type":"Answer","text":"No, es atención particular. Aceptamos efectivo, transferencia, débito y crédito."}}'''

ORT_RELATED = f'''
            <li><a href="/blog/odontologia-general">{ARROW} Odontología General</a></li>
            <li><a href="/blog/endodoncia">{ARROW} Endodoncia</a></li>
            <li><a href="/blog/implantologia">{ARROW} Implantología</a></li>
            <li><a href="/blog/estetica-facial">{ARROW} Estética Facial</a></li>
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
          '''


# ===================== BLOG: ECOGRAFÍA =====================

ECO_BODY = f'''
        <h2>¿Qué es una <em>ecografía</em>?</h2>
        <p>La ecografía (también llamada ecotomografía) es un examen de imagen que usa ondas de sonido para ver órganos y tejidos en tiempo real. <strong>No usa radiación</strong>, es indolora y segura para todas las edades.</p>
        <p>En el CMC realizamos ecografías con <strong>David Pardo</strong>, ecografista que coordina fechas mes a mes según disponibilidad. Atendemos derivaciones de tu médico tratante o consulta directa cuando corresponde.</p>

        {callout_info('<strong>Ecografías ginecológicas (vaginal/transvaginal):</strong> esas las realiza el <strong>Dr. Tirso Rejón</strong> en consulta de Ginecología ($35.000), no en el servicio de Ecografía general.')}

        <h2>Tipos de <em>ecografía</em> que realizamos</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12c4-4 8 4 12 0s8-4 8-4"/><path d="M2 18c4-4 8 4 12 0s8-4 8-4"/></svg></div><div class="body"><strong>Abdominal</strong><span>Hígado, vesícula, páncreas, riñones, bazo. Detecta cálculos, quistes, tumores.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/></svg></div><div class="body"><strong>Renal</strong><span>Estudio de los riñones — cálculos, quistes, hidronefrosis, masas.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/></svg></div><div class="body"><strong>Tiroidea</strong><span>Estudio de la tiroides — nódulos, agrandamiento, hipotiroidismo.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/></svg></div><div class="body"><strong>Mamaria</strong><span>Complemento o alternativa a la mamografía. Detecta quistes, fibroadenomas.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22V8M12 8L8 12M12 8l4 4"/></svg></div><div class="body"><strong>Testicular / inguinal</strong><span>Estudio de testículos, hernias inguinales, ganglios en la zona.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/></svg></div><div class="body"><strong>Partes blandas</strong><span>Bultos en piel, lipomas, ganglios, lesiones musculares.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h18M12 3v18"/></svg></div><div class="body"><strong>Doppler</strong><span>Estudio del flujo de sangre en venas y arterias — várices, trombosis.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/></svg></div><div class="body"><strong>Pélvica abdominal</strong><span>Útero, ovarios y vejiga vistos por vía abdominal (no transvaginal).</span></div></div>
        </div>

        <h2>Sin viajar a <em>Concepción</em></h2>
        <p>La principal ventaja de hacerte la ecografía aquí: <strong>no tienes que viajar 1.5 horas a Concepción</strong> para un examen de 15–20 minutos. El ecografista viene al CMC y coordinamos fechas según demanda.</p>

        {callout_info('<strong>Cómo funciona:</strong> escríbenos por WhatsApp diciendo qué tipo de ecografía necesitas. Te avisamos las próximas fechas disponibles del Dr. Pardo y agendamos tu hora. Si tienes orden médica, mejor — ayuda a aprovechar mejor el examen.')}

        {make_cta_inline('Ecografía', 'Agenda tu [Ecografía]', 'David Pardo coordina fechas mes a mes. Te avisamos por WhatsApp las próximas disponibilidades.', 'quiero%20agendar%20una%20Ecograf%C3%ADa.')}

        <h2>Preparación según <em>tipo de examen</em></h2>
        <div class="habit-grid">
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0z"/><path d="M12 7v5l3 3"/></svg></div>
            <h3>Abdominal y renal</h3>
            <p>Ayuno de 6–8 horas previas (sin alimentos sólidos). Puedes tomar agua. Trae tus exámenes anteriores si los tienes.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/></svg></div>
            <h3>Pélvica abdominal</h3>
            <p>Vejiga llena. Toma 1 litro de agua 1 hora antes y NO orines hasta que termine el examen.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></div>
            <h3>Tiroidea, mamaria, doppler</h3>
            <p>Sin preparación especial. Solo evita usar cremas o lociones en la zona del examen ese día.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="6" x2="12" y2="12"/></svg></div>
            <h3>Trae siempre</h3>
            <p>Cédula de identidad, orden médica si tienes, exámenes previos relacionados, lista de medicamentos.</p>
          </div>
        </div>

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Necesito orden médica para hacerme una ecografía?', 'Para algunos exámenes específicos (mamaria, pélvica completa, transvaginal) sí es ideal. Para abdominal, tiroidea o partes blandas puedes consultar directamente. Si tienes dudas escríbenos.')}
          {faq_item('¿La ecografía duele?', 'No. Es completamente indolora. Solo se aplica un gel frío sobre la piel y el ecografista mueve un transductor. Dura 15–30 minutos según el tipo.')}
          {faq_item('¿Cuándo entregan los resultados?', 'El informe te lo enviamos por WhatsApp en 24–72 horas hábiles. Si necesitas algo urgente, lo coordinamos.')}
          {faq_item('¿Hacen ecografías ginecológicas (transvaginales)?', 'No en el servicio de Ecografía. Las ecografías ginecológicas las realiza el Dr. Tirso Rejón en consulta de Ginecología por $35.000.')}
          {faq_item('¿Aceptan Fonasa para ecografía?', 'No. La ecografía es atención particular. Algunos seguros complementarios reembolsan parte — consulta con el tuyo.')}
        </div>'''

ECO_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Necesito orden médica para hacerme una ecografía?","acceptedAnswer":{"@type":"Answer","text":"Para mamaria, pélvica completa o transvaginal sí es ideal. Para abdominal, tiroidea o partes blandas puedes consultar directamente."}},
    {"@type":"Question","name":"¿La ecografía duele?","acceptedAnswer":{"@type":"Answer","text":"No. Es completamente indolora. Solo se aplica un gel frío sobre la piel. Dura 15-30 minutos según el tipo."}},
    {"@type":"Question","name":"¿Cuándo entregan los resultados?","acceptedAnswer":{"@type":"Answer","text":"Por WhatsApp en 24-72 horas hábiles. Si necesitas algo urgente lo coordinamos."}},
    {"@type":"Question","name":"¿Hacen ecografías ginecológicas?","acceptedAnswer":{"@type":"Answer","text":"No en el servicio de Ecografía. Las ginecológicas las realiza el Dr. Tirso Rejón en consulta de Ginecología por $35.000."}},
    {"@type":"Question","name":"¿Aceptan Fonasa para ecografía?","acceptedAnswer":{"@type":"Answer","text":"No. Es atención particular. Algunos seguros complementarios reembolsan parte."}}'''

ECO_RELATED = f'''
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/cardiologia">{ARROW} Cardiología</a></li>
            <li><a href="/blog/ginecologia">{ARROW} Ginecología</a></li>
            <li><a href="/blog/kinesiologia">{ARROW} Kinesiología</a></li>
            <li><a href="/blog/nutricion">{ARROW} Nutrición</a></li>
          '''


# ===================== BLOG: ESTÉTICA FACIAL =====================

EST_BODY = f'''
        <h2>¿Qué es la <em>Estética Facial</em>?</h2>
        <p>La estética facial es el área de la medicina que mejora la apariencia y armonía del rostro mediante procedimientos no quirúrgicos. En el CMC trabajamos con la <strong>Dra. Valentina Fuentealba</strong>, médico estético que coordina fechas según demanda.</p>
        <p>Nuestro foco es la naturalidad: realzar tu belleza sin que se note que te hiciste algo. Usamos técnicas internacionales con productos certificados.</p>

        {callout_info('<strong>Todos los procedimientos se realizan tras una evaluación previa</strong>. La Dra. Fuentealba estudia tu rostro, tus expectativas y propone un plan personalizado. No hay tratamientos genéricos.')}

        <h2>Tratamientos <em>disponibles</em></h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg></div><div class="body"><strong>Botox / toxina botulínica</strong><span>Suaviza líneas de expresión: arrugas frontales, patas de gallo, entrecejo. Dura 4–6 meses.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v8M12 14v8M2 12h8M14 12h8"/></svg></div><div class="body"><strong>Ácido hialurónico (rellenos)</strong><span>Volumen en labios, pómulos, surcos nasogenianos, ojeras. Resultados naturales 12–18 meses.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22V12M3 12h18"/></svg></div><div class="body"><strong>Hilos tensores</strong><span>Lifting no quirúrgico. Levantan pómulos, mandíbula y cuello con hilos reabsorbibles.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="14" r="6"/></svg></div><div class="body"><strong>Lipopapada</strong><span>Reducción de grasa submentoniana (papada) con inyecciones de ácido desoxicólico.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="9"/></svg></div><div class="body"><strong>Bioestimuladores</strong><span>Estimulan tu propio colágeno. Mejoran calidad de la piel y firmeza progresivamente.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 12h8M12 8v8"/></svg></div><div class="body"><strong>Exosomas</strong><span>Tratamiento celular regenerativo: textura, luminosidad, líneas finas.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/></svg></div><div class="body"><strong>Armonización facial</strong><span>Plan integral combinando técnicas para equilibrar proporciones del rostro.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12c4-4 8 4 12 0s8-4 8-4"/></svg></div><div class="body"><strong>Peeling químico</strong><span>Renueva la piel: manchas, cicatrices de acné, textura. Distintos tipos según tu piel.</span></div></div>
        </div>

        {make_cta_inline('Estética Facial', 'Agenda tu evaluación de [Estética Facial]', 'Coordinamos fecha con la Dra. Fuentealba según disponibilidad. Evaluación personalizada antes de cualquier procedimiento.', 'quiero%20agendar%20una%20evaluaci%C3%B3n%20de%20Est%C3%A9tica%20Facial.')}

        <h2>Antes y después del <em>tratamiento</em></h2>
        <div class="habit-grid">
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="9"/></svg></div>
            <h3>Antes: evaluación</h3>
            <p>Conversamos tus objetivos, evaluamos tu rostro y te explicamos opciones, riesgos y costos. No hay presión por agendar.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg></div>
            <h3>El procedimiento</h3>
            <p>Aplicamos anestesia tópica si corresponde. Cada técnica tiene su tiempo: 20 min (botox) hasta 60 min (hilos).</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></div>
            <h3>Cuidados post</h3>
            <p>Evita ejercicio intenso, sauna y exposición solar 24–48 horas. Bloqueador solar diario siempre.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v4"/><path d="m4.93 4.93 2.83 2.83"/><path d="m16.24 16.24 2.83 2.83"/><path d="M12 18v4"/></svg></div>
            <h3>Resultados progresivos</h3>
            <p>Botox: a los 7–14 días. Rellenos: inmediato pero se asienta a los 7 días. Bioestimuladores: 2–3 meses.</p>
          </div>
        </div>

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuestan los tratamientos de estética facial?', 'Los valores varían mucho según el tratamiento (desde botox por zona hasta planes integrales de armonización). En tu evaluación inicial la Dra. Fuentealba te entrega presupuesto detallado según tu plan personalizado.')}
          {faq_item('¿Es seguro hacerse botox o rellenos?', 'Sí, cuando los aplica un médico calificado con productos certificados. La Dra. Fuentealba usa marcas con registro ISP. Los efectos adversos serios son extremadamente raros.')}
          {faq_item('¿Se nota que me hice algo?', 'Nuestro enfoque es la naturalidad. No buscamos cambiar tu rostro sino realzarlo. La gente notará que te ves bien, pero no podrá precisar qué cambió.')}
          {faq_item('¿Cuándo se ven los resultados?', 'Botox: 7–14 días. Rellenos: inmediato (se asienta en 7 días). Hilos: en el momento. Bioestimuladores: 2–3 meses (es progresivo).')}
          {faq_item('¿Aceptan Fonasa?', 'No. Estética facial es atención particular. Aceptamos efectivo, transferencia, débito y crédito.')}
        </div>'''

EST_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuestan los tratamientos de estética facial?","acceptedAnswer":{"@type":"Answer","text":"Los valores varían según el tratamiento. En la evaluación inicial la Dra. Fuentealba entrega presupuesto detallado según el plan personalizado."}},
    {"@type":"Question","name":"¿Es seguro hacerse botox o rellenos?","acceptedAnswer":{"@type":"Answer","text":"Sí, cuando los aplica un médico calificado con productos certificados con registro ISP."}},
    {"@type":"Question","name":"¿Se nota que me hice algo?","acceptedAnswer":{"@type":"Answer","text":"El enfoque es la naturalidad. La gente notará que te ves bien, pero no podrá precisar qué cambió."}},
    {"@type":"Question","name":"¿Cuándo se ven los resultados?","acceptedAnswer":{"@type":"Answer","text":"Botox: 7-14 días. Rellenos: inmediato. Bioestimuladores: 2-3 meses progresivo."}},
    {"@type":"Question","name":"¿Aceptan Fonasa en estética facial?","acceptedAnswer":{"@type":"Answer","text":"No. Es atención particular. Aceptamos efectivo, transferencia, débito y crédito."}}'''

EST_RELATED = f'''
            <li><a href="/blog/odontologia-general">{ARROW} Odontología General</a></li>
            <li><a href="/blog/ortodoncia">{ARROW} Ortodoncia</a></li>
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/nutricion">{ARROW} Nutrición</a></li>
            <li><a href="/blog/kinesiologia">{ARROW} Kinesiología</a></li>
          '''


# ===================== BLOG: KINESIOLOGÍA =====================

KIN_BODY = f'''
        <h2>¿Qué es la <em>Kinesiología</em>?</h2>
        <p>La kinesiología es la profesión de la salud que se dedica al movimiento humano. El kinesiólogo trata lesiones, dolores y limitaciones funcionales con ejercicios terapéuticos, terapia manual y agentes físicos.</p>
        <p>En el CMC tenemos <strong>sala equipada</strong> con camillas, electroestimulación y elementos para rehabilitación. Atendemos con <strong>Luis Armijo</strong> (lunes a sábado) y <strong>Leo Etcheverry</strong> (lunes a viernes), ambos kinesiólogos titulados.</p>

        {callout_info('<strong>Bono Fonasa $7.830</strong>: lo emitimos en el mismo centro con huella biométrica. Sin trámites previos en banco u oficinas. Trae solo tu carnet.')}

        <h2>¿Cuándo necesitas <em>kinesiología</em>?</h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v8"/><path d="M12 14v8"/><path d="M5 12h14"/></svg></div><div class="body"><strong>Dolor lumbar (lumbago)</strong><span>Dolor en zona baja de la espalda — aguda o crónica. Es la causa más común de consulta.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/></svg></div><div class="body"><strong>Cervicalgia</strong><span>Dolor de cuello, contracturas, dolor que irradia a hombros o brazos.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div><div class="body"><strong>Lesiones deportivas</strong><span>Esguinces, tendinopatías, distensiones musculares, lesiones de menisco o ligamento.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 6 12 12 16 14"/></svg></div><div class="body"><strong>Postoperatorio</strong><span>Rehabilitación tras cirugía: rodilla, hombro, cadera, columna.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2C6 2 4 9 12 22c8-13 6-20 0-20z"/></svg></div><div class="body"><strong>Bronquitis y respiratorio</strong><span>Kinesiología respiratoria para niños y adultos: bronquitis, asma, post-COVID.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 17 9 11 13 15 21 7"/></svg></div><div class="body"><strong>Postura y ergonomía</strong><span>Dolor por horas frente al computador, desbalances posturales, tensión crónica.</span></div></div>
        </div>

        <h2>Tipos de <em>kinesiología</em> que ofrecemos</h2>
        <h3>Kinesiología musculoesquelética</h3>
        <p>La más común. Trata lumbago, cervicalgia, lesiones deportivas, dolor de hombro, rodilla, codo, etc. Combinación de terapia manual, ejercicios específicos y electroterapia.</p>

        <h3>Kinesiología respiratoria</h3>
        <p>Especializada en niños con bronquitis y adultos con problemas respiratorios. Técnicas para movilizar secreciones, mejorar capacidad pulmonar y enseñar manejo en casa.</p>

        <h3>Kinesiología a domicilio</h3>
        <p>Para pacientes que no pueden trasladarse: postoperatorio reciente, adultos mayores, condiciones que limitan la movilidad. Coordinamos visitas según disponibilidad.</p>

        {make_cta_inline('Kinesiología', 'Agenda tu sesión de [Kinesiología]', 'Bono Fonasa $7.830 (se emite en el centro) o particular $20.000. Lunes a sábado por WhatsApp.', 'quiero%20agendar%20una%20sesi%C3%B3n%20de%20Kinesiolog%C3%ADa.')}

        <h2>Cómo es tu <em>tratamiento</em></h2>
        <div class="habit-grid">
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9 12h6M12 9v6"/></svg></div>
            <h3>1. Evaluación inicial</h3>
            <p>Anamnesis, examen físico, evaluación de movimiento. Definimos plan y número estimado de sesiones.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg></div>
            <h3>2. Sesiones terapéuticas</h3>
            <p>40 minutos cada una. Combinamos terapia manual, ejercicios, electroestimulación y educación.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v8M12 14v8M5 12h14"/></svg></div>
            <h3>3. Ejercicios en casa</h3>
            <p>Te enseñamos rutinas para hacer entre sesiones. La adherencia en casa es 50% del éxito del tratamiento.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></div>
            <h3>4. Alta progresiva</h3>
            <p>Cuando los síntomas ceden, espaciamos sesiones y damos pautas para prevenir recaídas.</p>
          </div>
        </div>

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta la sesión de kinesiología?', '<strong>$7.830 con bono Fonasa</strong> (se emite en el centro con huella biométrica) o <strong>$20.000 particular</strong>.')}
          {faq_item('¿Cuántas sesiones necesito?', 'Depende del caso. Lumbago agudo: 6–10 sesiones. Lesión deportiva: 10–15. Postoperatorio: variable. En la primera evaluación te damos un estimado realista.')}
          {faq_item('¿Necesito orden médica?', 'No es obligatoria para empezar. Pero si tienes una orden de tu médico (con diagnóstico y indicaciones), nos sirve para enfocar mejor el tratamiento desde la primera sesión.')}
          {faq_item('¿Hacen kinesiología a domicilio?', 'Sí. Coordinamos visitas para pacientes que no pueden trasladarse: postoperatorio reciente, adultos mayores, movilidad reducida. Escribe por WhatsApp para coordinar.')}
          {faq_item('¿Atienden niños con bronquitis?', 'Sí. Hacemos kinesiología respiratoria pediátrica. Técnicas suaves y efectivas para movilizar secreciones y mejorar la respiración.')}
        </div>'''

KIN_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta la sesión de kinesiología?","acceptedAnswer":{"@type":"Answer","text":"$7.830 con bono Fonasa (se emite en el centro con huella biométrica) o $20.000 particular."}},
    {"@type":"Question","name":"¿Cuántas sesiones de kinesiología necesito?","acceptedAnswer":{"@type":"Answer","text":"Depende del caso. Lumbago agudo: 6-10 sesiones. Lesión deportiva: 10-15. En la primera evaluación damos un estimado."}},
    {"@type":"Question","name":"¿Necesito orden médica para kinesiología?","acceptedAnswer":{"@type":"Answer","text":"No es obligatoria. Pero si tienes orden de tu médico ayuda a enfocar mejor el tratamiento desde la primera sesión."}},
    {"@type":"Question","name":"¿Hacen kinesiología a domicilio?","acceptedAnswer":{"@type":"Answer","text":"Sí. Coordinamos visitas para pacientes que no pueden trasladarse."}},
    {"@type":"Question","name":"¿Atienden niños con bronquitis?","acceptedAnswer":{"@type":"Answer","text":"Sí. Hacemos kinesiología respiratoria pediátrica con técnicas suaves y efectivas."}}'''

KIN_RELATED = f'''
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
            <li><a href="/blog/nutricion">{ARROW} Nutrición</a></li>
            <li><a href="/blog/cardiologia">{ARROW} Cardiología</a></li>
            <li><a href="/blog/podologia">{ARROW} Podología</a></li>
            <li><a href="/blog/fonoaudiologia">{ARROW} Fonoaudiología</a></li>
          '''


# ===================== BLOG: ODONTOLOGÍA GENERAL =====================

ODG_BODY = f'''
        <h2>¿Qué es la <em>Odontología General</em>?</h2>
        <p>La odontología general es la base de la salud bucal. Resuelve problemas comunes: caries, limpiezas, extracciones, blanqueamientos, y deriva a especialistas cuando se necesita. Es la primera puerta cuando algo te molesta en la boca.</p>
        <p>En el CMC atienden la <strong>Dra. Javiera Burgos</strong> (lunes a sábado) y el <strong>Dr. Carlos Jiménez</strong> (viernes y sábado). Ambos atienden adultos y niños.</p>

        {callout_info('<strong>Atendemos niños:</strong> nuestros odontólogos generales atienden a menores. No tenemos odontopediatría dedicada, pero tienen experiencia con niños y los tratan con paciencia y técnicas adaptadas a su edad.')}

        <h2>Tratamientos <em>más comunes</em></h2>
        <div class="symptom-grid">
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M9 9h.01M15 9h.01"/></svg></div><div class="body"><strong>Limpieza dental — $30.000</strong><span>Destartraje + profilaxis. ~40 min, sin dolor. Recomendada cada 6 meses.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v6"/><circle cx="12" cy="14" r="6"/></svg></div><div class="body"><strong>Tapadura (empaste) — desde $35.000</strong><span>Reparación de caries con resina del color del diente. ~30 min con anestesia local.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12h20"/></svg></div><div class="body"><strong>Extracción simple — $40.000</strong><span>Para muelas afectadas que no se pueden recuperar. Anestesia local, ~30–45 min.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="6" y1="6" x2="18" y2="18"/></svg></div><div class="body"><strong>Extracción compleja — $60.000</strong><span>Muelas del juicio o piezas con raíces complicadas. ~60 min con anestesia.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 4 5v6c0 5.5 3.84 10.74 8 12 4.16-1.26 8-6.5 8-12V5z"/></svg></div><div class="body"><strong>Blanqueamiento — $75.000</strong><span>Aclara varios tonos. Aplicación de gel ~60 min, indoloro. Resultados visibles inmediatos.</span></div></div>
          <div class="symptom-card"><div class="ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></div><div class="body"><strong>Control y diagnóstico</strong><span>Revisión completa, indicación de radiografías, plan de tratamiento.</span></div></div>
        </div>

        <h2>Cuándo deberías <em>consultar</em></h2>
        <div class="habit-grid">
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 2"/></svg></div>
            <h3>Cada 6 meses</h3>
            <p>Control + limpieza preventiva. Detecta caries pequeñas antes de que duelan o crezcan.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="12" y1="6" x2="12" y2="14"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></div>
            <h3>Dolor de muela</h3>
            <p>No esperes a que pase solo. Una caries que duele probablemente ya llegó al nervio y necesita tratamiento.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></svg></div>
            <h3>Sangrado de encías</h3>
            <p>Si sangran al cepillarte, es signo de gingivitis. Si no se trata progresa a periodontitis y pérdida de dientes.</p>
          </div>
          <div class="habit-card">
            <div class="ico-circle"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></div>
            <h3>Mal aliento persistente</h3>
            <p>Suele ser por bacterias de la placa o caries no detectadas. Una limpieza profesional resuelve la mayoría de casos.</p>
          </div>
        </div>

        {make_cta_inline('Odontología General', 'Agenda tu hora de [Odontología General]', 'Lunes a sábado · Aceptamos efectivo, transferencia, débito y crédito. Confirmación inmediata por WhatsApp.', 'quiero%20agendar%20una%20hora%20de%20Odontolog%C3%ADa%20General.')}

        <h2>Preguntas <em>frecuentes</em></h2>
        <div class="faq-list">
          {faq_item('¿Cuánto cuesta una tapadura?', 'Desde <strong>$35.000</strong>. El precio puede variar según el tamaño de la caries y la cara del diente que se trate. Si la caries llega al nervio puede requerir endodoncia (mayor costo, derivamos al endodoncista).')}
          {faq_item('¿Atienden niños?', 'Sí. La Dra. Burgos y el Dr. Jiménez atienden niños desde aproximadamente los 3 años. Tratan caries, sellantes, limpiezas y extracciones de dientes de leche.')}
          {faq_item('¿Aceptan Fonasa en odontología?', 'No. Toda la atención dental en CMC es particular. Aceptamos efectivo, transferencia, débito y crédito (las dentales son las únicas que aceptan tarjetas en el centro).')}
          {faq_item('¿Hacen ortodoncia?', 'Sí, derivamos al ortodoncista tras tu evaluación inicial con dentista general. La instalación de brackets es $120.000 y los controles $30.000. Empieza siempre con cita en odontología general.')}
          {faq_item('¿Atienden urgencias dentales?', 'Tenemos cupos para morbilidad dental el mismo día según disponibilidad. Escribe por WhatsApp con tu situación y revisamos cómo atenderte rápido.')}
          {faq_item('¿Qué hago si se me sale una tapadura?', 'Agenda lo antes posible. Mientras tanto, mantén la zona limpia (cepillado suave) y evita comer cosas duras o muy frías/calientes en ese lado.')}
        </div>'''

ODG_FAQ_JSONLD = '''
    {"@type":"Question","name":"¿Cuánto cuesta una tapadura?","acceptedAnswer":{"@type":"Answer","text":"Desde $35.000. Puede variar según el tamaño de la caries y la cara del diente. Si llega al nervio puede requerir endodoncia."}},
    {"@type":"Question","name":"¿Atienden niños en odontología?","acceptedAnswer":{"@type":"Answer","text":"Sí. La Dra. Burgos y el Dr. Jiménez atienden niños desde los 3 años aproximadamente."}},
    {"@type":"Question","name":"¿Aceptan Fonasa en odontología?","acceptedAnswer":{"@type":"Answer","text":"No. Toda la atención dental es particular. Aceptamos efectivo, transferencia, débito y crédito."}},
    {"@type":"Question","name":"¿Hacen ortodoncia?","acceptedAnswer":{"@type":"Answer","text":"Sí, tras evaluación inicial con dentista general. Instalación $120.000 y controles $30.000."}},
    {"@type":"Question","name":"¿Atienden urgencias dentales?","acceptedAnswer":{"@type":"Answer","text":"Tenemos cupos para morbilidad dental el mismo día según disponibilidad. Escribe por WhatsApp."}}'''

ODG_RELATED = f'''
            <li><a href="/blog/ortodoncia">{ARROW} Ortodoncia</a></li>
            <li><a href="/blog/endodoncia">{ARROW} Endodoncia</a></li>
            <li><a href="/blog/implantologia">{ARROW} Implantología</a></li>
            <li><a href="/blog/estetica-facial">{ARROW} Estética Facial</a></li>
            <li><a href="/blog/medicina-general">{ARROW} Medicina General</a></li>
          '''


# ===================== CONFIGS DE BLOGS =====================

BLOGS = [
    {
        'slug': 'medicina-general',
        'specialty_short': 'Medicina General',
        'specialty_en': 'GeneralPractice',
        'breadcrumb_name': 'Medicina General',
        'eyebrow': 'Medicina General · Atención preventiva y curativa',
        'h1': 'Tu médico de cabecera <em>cerca de casa</em>',
        'lead': 'Atención preventiva y curativa para toda la familia. Bono Fonasa $7.880 que emitimos en el mismo centro con huella biométrica. Lunes a sábado.',
        'title': 'Medicina General en Carampangue · Bono Fonasa $7.880 | CMC',
        'description': 'Atención de medicina general con bono Fonasa $7.880 (se emite en CMC con huella biométrica) o particular $25.000. Lunes a sábado. WhatsApp 24/7.',
        'og_title': 'Medicina General en Carampangue · Bono Fonasa $7.880',
        'og_description': 'Atención de medicina general con bono Fonasa $7.880 o particular $25.000. Lunes a sábado en Carampangue.',
        'headline': 'Medicina General en Carampangue: cuándo consultar, precios y profesionales',
        'read_time': 6,
        'body_html': MG_BODY,
        'faq_jsonld': MG_FAQ_JSONLD,
        'related_list': MG_RELATED,
        'sidebar_cta_desc': 'Bono Fonasa $7.880 emitido en el centro con huella, o particular $25.000. Confirmación inmediata por WhatsApp.',
        'cta_band_h2': 'Tu salud <em>no espera</em>',
        'cta_band_p': 'Agenda tu hora de Medicina General por WhatsApp. Bono Fonasa o particular, según prefieras.',
        'wa_text': 'quiero%20agendar%20una%20hora%20de%20Medicina%20General.',
    },
    {
        'slug': 'ortodoncia',
        'specialty_short': 'Ortodoncia',
        'specialty_en': 'Orthodontic',
        'breadcrumb_name': 'Ortodoncia',
        'eyebrow': 'Ortodoncia · Brackets y corrección de mordida',
        'h1': 'Endereza tu sonrisa <em>en Carampangue</em>',
        'lead': 'Tratamiento de ortodoncia con brackets metálicos. Instalación $120.000 y controles mensuales $30.000. Empezamos siempre con evaluación de dentista general.',
        'title': 'Ortodoncia en Carampangue · Brackets desde $120.000 | CMC',
        'description': 'Ortodoncia con Dra. Daniela Castillo en Carampangue. Instalación brackets $120.000, controles $30.000. Evaluación inicial con dentista general. WhatsApp 24/7.',
        'og_title': 'Ortodoncia en Carampangue · Brackets $120.000',
        'og_description': 'Tratamiento de ortodoncia: instalación $120.000 y controles $30.000. Carampangue, Provincia de Arauco.',
        'headline': 'Ortodoncia en Carampangue: brackets, controles y proceso completo',
        'read_time': 7,
        'body_html': ORT_BODY,
        'faq_jsonld': ORT_FAQ_JSONLD,
        'related_list': ORT_RELATED,
        'sidebar_cta_desc': 'Empezamos con dentista general. Presupuesto $15.000 (gratis si comienzas tratamiento ese día).',
        'cta_band_h2': 'Sonríe <em>con confianza</em>',
        'cta_band_p': 'Agenda tu evaluación inicial con dentista general por WhatsApp. Te derivamos a la ortodoncista cuando estés listo.',
        'wa_text': 'quiero%20iniciar%20tratamiento%20de%20Ortodoncia.',
    },
    {
        'slug': 'ecografia',
        'specialty_short': 'Ecografía',
        'specialty_en': 'DiagnosticImaging',
        'breadcrumb_name': 'Ecografía',
        'eyebrow': 'Ecografía · Imagen diagnóstica',
        'h1': 'Ecografías <em>sin viajar a Concepción</em>',
        'lead': 'Realizamos ecografías abdominal, renal, tiroidea, mamaria, partes blandas, doppler y más. David Pardo coordina fechas mes a mes según demanda.',
        'title': 'Ecografía en Carampangue · Sin viajar a Concepción | CMC',
        'description': 'Ecografía abdominal, renal, tiroidea, mamaria, partes blandas y doppler en Carampangue. David Pardo coordina fechas mes a mes. WhatsApp 24/7.',
        'og_title': 'Ecografía en Carampangue · Sin viajar a Concepción',
        'og_description': 'Ecografías de todo tipo en Carampangue. Evita el viaje a Concepción para un examen de 15 minutos.',
        'headline': 'Ecografía en Carampangue: tipos, precios y cómo agendar',
        'read_time': 6,
        'body_html': ECO_BODY,
        'faq_jsonld': ECO_FAQ_JSONLD,
        'related_list': ECO_RELATED,
        'sidebar_cta_desc': 'David Pardo coordina fechas mes a mes. Te avisamos por WhatsApp las próximas disponibilidades.',
        'cta_band_h2': 'Tu examen <em>cerca de casa</em>',
        'cta_band_p': 'Escríbenos por WhatsApp diciendo qué tipo de ecografía necesitas. Te avisamos las próximas fechas disponibles.',
        'wa_text': 'quiero%20agendar%20una%20Ecograf%C3%ADa.',
    },
    {
        'slug': 'estetica-facial',
        'specialty_short': 'Estética Facial',
        'specialty_en': 'CosmeticDermatology',
        'breadcrumb_name': 'Estética Facial',
        'eyebrow': 'Estética Facial · Medicina estética',
        'h1': 'Realza tu belleza <em>con naturalidad</em>',
        'lead': 'Botox, ácido hialurónico, hilos tensores, bioestimuladores, exosomas, peeling y armonización facial. Con la Dra. Valentina Fuentealba.',
        'title': 'Estética Facial en Carampangue · Botox, Rellenos, Hilos | CMC',
        'description': 'Estética facial con Dra. Fuentealba: botox, ácido hialurónico, hilos tensores, bioestimuladores, exosomas, peeling. Carampangue. WhatsApp 24/7.',
        'og_title': 'Estética Facial en Carampangue · Naturalidad',
        'og_description': 'Tratamientos de estética facial con Dra. Fuentealba: botox, rellenos, hilos, bioestimuladores. Carampangue.',
        'headline': 'Estética Facial en Carampangue: tratamientos y resultados',
        'read_time': 7,
        'body_html': EST_BODY,
        'faq_jsonld': EST_FAQ_JSONLD,
        'related_list': EST_RELATED,
        'sidebar_cta_desc': 'Evaluación previa con la Dra. Fuentealba. Plan personalizado y presupuesto detallado.',
        'cta_band_h2': 'Verte bien <em>te hace sentir bien</em>',
        'cta_band_p': 'Agenda tu evaluación con la Dra. Fuentealba por WhatsApp. Coordinamos fechas según disponibilidad.',
        'wa_text': 'quiero%20agendar%20una%20evaluaci%C3%B3n%20de%20Est%C3%A9tica%20Facial.',
    },
    {
        'slug': 'kinesiologia',
        'specialty_short': 'Kinesiología',
        'specialty_en': 'PhysicalTherapy',
        'breadcrumb_name': 'Kinesiología',
        'eyebrow': 'Kinesiología · Rehabilitación y movimiento',
        'h1': 'Recupera tu movimiento <em>sin dolor</em>',
        'lead': 'Kinesiología musculoesquelética, respiratoria y a domicilio. Bono Fonasa $7.830 emitido en el centro o particular $20.000. Sala equipada.',
        'title': 'Kinesiología en Carampangue · Bono Fonasa $7.830 | CMC',
        'description': 'Kinesiología musculoesquelética, respiratoria y domicilio en Carampangue. Bono Fonasa $7.830 (se emite en el centro) o particular $20.000. Lun-Sáb.',
        'og_title': 'Kinesiología en Carampangue · Bono Fonasa $7.830',
        'og_description': 'Kinesiología en Carampangue. Bono Fonasa $7.830 emitido en el centro. Sala equipada. Lunes a sábado.',
        'headline': 'Kinesiología en Carampangue: tipos, precios y profesionales',
        'read_time': 6,
        'body_html': KIN_BODY,
        'faq_jsonld': KIN_FAQ_JSONLD,
        'related_list': KIN_RELATED,
        'sidebar_cta_desc': 'Bono Fonasa $7.830 emitido en el centro o particular $20.000. Lunes a sábado.',
        'cta_band_h2': 'Recupera tu <em>calidad de vida</em>',
        'cta_band_p': 'Agenda tu sesión de kinesiología por WhatsApp. Bono Fonasa o particular, según prefieras.',
        'wa_text': 'quiero%20agendar%20una%20sesi%C3%B3n%20de%20Kinesiolog%C3%ADa.',
    },
    {
        'slug': 'odontologia-general',
        'specialty_short': 'Odontología',
        'specialty_en': 'Dentistry',
        'breadcrumb_name': 'Odontología General',
        'eyebrow': 'Odontología General · Salud bucal integral',
        'h1': 'Tu salud bucal <em>en buenas manos</em>',
        'lead': 'Limpiezas, tapaduras, extracciones, blanqueamiento y más. Atendemos adultos y niños. Dra. Burgos (lun-sáb) y Dr. Jiménez (vie-sáb).',
        'title': 'Odontología General en Carampangue · Tapadura $35.000 | CMC',
        'description': 'Odontología general en Carampangue: tapaduras desde $35.000, limpieza $30.000, extracciones desde $40.000, blanqueamiento $75.000. Adultos y niños.',
        'og_title': 'Odontología General en Carampangue · Adultos y niños',
        'og_description': 'Limpiezas, tapaduras, extracciones, blanqueamiento. Atención dental para toda la familia en Carampangue.',
        'headline': 'Odontología General en Carampangue: tratamientos, precios y profesionales',
        'read_time': 6,
        'body_html': ODG_BODY,
        'faq_jsonld': ODG_FAQ_JSONLD,
        'related_list': ODG_RELATED,
        'sidebar_cta_desc': 'Atendemos adultos y niños. Aceptamos efectivo, transferencia, débito y crédito.',
        'cta_band_h2': 'Cuida tu sonrisa <em>cerca de casa</em>',
        'cta_band_p': 'Agenda tu hora de odontología general por WhatsApp. Lunes a sábado, atendemos toda la familia.',
        'wa_text': 'quiero%20agendar%20una%20hora%20de%20Odontolog%C3%ADa%20General.',
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
