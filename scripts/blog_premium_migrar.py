#!/usr/bin/env python3
"""Migra los artículos del blog (templates/blog/*.html) y su índice al sistema
editorial premium (static/blog/blog-premium.css).

Reproducible e idempotente: la FUENTE es siempre la versión de cada archivo en
git HEAD (`git show HEAD:templates/blog/<slug>.html`), nunca el archivo ya
migrado. Correr dos veces da el mismo resultado.

Qué conserva tal cual (byte a byte): <title>, meta description, canonical,
og:title/og:url/og:description, article:*, twitter:*, todos los JSON-LD, GTM
(script y noscript), el script de clics a WhatsApp (dataLayer) y cmc-wa.js; y del
cuerpo: H1, lead, cada H2/H3, párrafos, listas, tablas, FAQ, enlaces internos.

Qué cambia (solo presentación): cabecera, portada, índice, riel lateral, ficha de
la especialidad, pie, flotante; recuadros/tablas/FAQ re-vestidos; ids en los H2;
espacios no separables en precios y unidades; og:image propia por artículo.

Marcadores de los que depende app/main.py::_localize_blog (SEO local por comuna)
y que el script GARANTIZA en la salida (verificado al final con asserts):
  <title>… | CMC</title> (si la fuente lo traía) · <meta name="description" content="…" />
  · <meta property="og:title" content="…" /> · <link rel="canonical" href="…/blog/<slug>" />
  · <h1 class="blog-h1"> · <p class="blog-lead"> · <span class="current"> ·
  <section class="blog-body"> · </head> · </body>

Correcciones de DATOS: solo las listadas en CORRECCIONES (cada una contradice una
fuente verificada: app/claude_helper.py, app/medilink.py, app/main.py o el dueño).
Si una cadena esperada no aparece, el script se detiene (no corrige a ciegas).

Uso:
  python3 scripts/blog_premium_migrar.py            # migra los 36 + índice
  python3 scripts/blog_premium_migrar.py --solo nutricion-baja-peso-saludable
  python3 scripts/blog_premium_migrar.py --check    # no escribe, solo valida
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
BLOG_DIR = ROOT / "templates" / "blog"
INDEX = ROOT / "templates" / "blog_index.html"
EQUIPO = ROOT / "static" / "images" / "equipo"
OG_DIR = ROOT / "static" / "images" / "blog" / "og"
CSS_HREF = "/static/blog/blog-premium.css?v=1"
BASE = "https://centromedicocarampangue.cl"
WA = "56966610737"
NBSP = " "

# ─────────────────────────────────────────────────────────────────────────────
# Personas (nombres y roles: app/medilink.py PROFESIONALES + claude_helper.py)
# ─────────────────────────────────────────────────────────────────────────────
PERSONAS = {
    "olavarria": ("Dr. Rodrigo Olavarría", "Médico general"),
    "abarca": ("Dr. Andrés Abarca", "Médico general"),
    "marquez": ("Dr. Alonso Márquez", "Medicina familiar"),
    "millan": ("Dr. Miguel Millán", "Cardiólogo"),
    "borrego": ("Dr. Manuel Borrego", "Otorrinolaringólogo"),
    "arratia": ("Juana Arratia", "Fonoaudióloga"),
    "rejon": ("Dr. Tirso Rejón", "Ginecólogo"),
    "gomez": ("Sarai Gómez", "Matrona"),
    "quijano": ("Dr. Nicolás Quijano", "Gastroenterólogo"),
    "gonzalez": ("Dra. Franca González", "Neuróloga, por videollamada"),
    "armijo": ("Luis Armijo", "Kinesiólogo"),
    "etcheverry": ("Leonardo Etcheverry", "Kinesiólogo"),
    "acosta": ("Paola Acosta", "Masoterapia"),
    "pinto": ("Gisela Pinto", "Nutricionista"),
    "paz": ("Dr. Raúl Paz", "Nutriólogo y diabetólogo"),
    "montalba": ("Jorge Montalba", "Psicólogo"),
    "rodriguez": ("Juan Pablo Rodríguez", "Psicólogo"),
    "salas": ("Ps. Jacquelinne Salas", "Psicóloga"),
    "unibazo": ("Dra. Cecilia Unibazo", "Psiquiatra, por teleconsulta"),
    "guevara": ("Andrea Guevara", "Podóloga"),
    "celedon": ("TM Ana Celedón", "Tecnóloga médica, oftalmología"),
    "pardo": ("David Pardo", "Tecnólogo médico, realiza la ecografía"),
    "sandoval": ("Dr. Sergio Sandoval Jara", "Médico radiólogo, informa"),
    "burgos": ("Dra. Javiera Burgos", "Dentista general"),
    "jimenez": ("Dr. Carlos Jiménez", "Dentista general"),
    "castillo": ("Dra. Daniela Castillo", "Ortodoncista"),
    "fredes": ("Dr. Fernando Fredes", "Endodoncista"),
    "valdes": ("Dra. Aurora Valdés", "Implantóloga"),
    "fuentealba": ("Dra. Valentina Fuentealba", "Odontóloga, estética facial"),
}

# ─────────────────────────────────────────────────────────────────────────────
# Ficha por especialidad (precio de ENTRADA, verificado). Es el elemento firma.
#   k/v: rótulo y precio grande del ticket · alt: segundo precio
#   gancho: texto del enlace suave de la portada · facts: 2-3 datos que tranquilizan
# ─────────────────────────────────────────────────────────────────────────────
PAGO_MED = "Efectivo o transferencia."
PAGO_DENT = "Efectivo, transferencia, débito o crédito."
FICHAS = {
    "medicina-general": dict(esp="Medicina general", landing="/medicina-general", gente=["olavarria", "abarca"],
        k="Con bono Fonasa", v="$7.880", alt=("$25.000", "particular"), gancho="$7.880 con bono Fonasa",
        facts=["El bono se emite aquí, con tu huella: basta tu cédula.", "Atienden niños y adultos.", PAGO_MED],
        h2="Tu médico, en <em>Monsalve&nbsp;102</em>.", wa="una hora de medicina general",
        p="Atención presencial para niños y adultos: diagnóstico, tratamiento, licencias y derivación al especialista. El asistente de WhatsApp te muestra las horas libres a cualquier hora del día."),
    "medicina-familiar": dict(esp="Medicina familiar", landing="/medicina-familiar", gente=["marquez"],
        k="Con bono Fonasa", v="$7.880", alt=("$30.000", "particular"), gancho="$7.880 con bono Fonasa",
        facts=["Atiende a toda la familia, niños y adultos.", PAGO_MED],
        h2="Un médico para <em>toda la familia</em>.", wa="una hora de medicina familiar",
        p="Atención integral para toda la familia, niños y adultos, con foco en enfermedades crónicas y controles preventivos."),
    "cardiologia": dict(esp="Cardiología", landing="/cardiologia", gente=["millan"],
        k="Consulta particular", v="$40.000", alt=None, gancho="consulta $40.000",
        facts=["Presencial, en Carampangue.", "Solo particular: sin bono Fonasa.", PAGO_MED],
        h2="Tu corazón, revisado <em>cerca de casa</em>.", wa="una consulta de cardiología",
        p="Hipertensión, palpitaciones, dolor de pecho y control del riesgo cardiovascular. Las fechas siguen la agenda del cardiólogo: el asistente de WhatsApp te muestra las horas libres."),
    "otorrinolaringologia": dict(esp="Otorrinolaringología", landing="/otorrinolaringologia", gente=["borrego"],
        k="Consulta particular", v="$35.000", alt=None, gancho="consulta $35.000",
        facts=["Por ahora, solo por videollamada.", "Solo particular: sin bono Fonasa.", PAGO_MED],
        h2="Oído, nariz y garganta.", wa="una consulta de otorrinolaringología",
        p="Evaluación de oído, nariz y garganta."),
    "fonoaudiologia": dict(esp="Fonoaudiología", landing="/fonoaudiologia", gente=["arratia"],
        k="Evaluación", v="$25.000", alt=None, gancho="evaluación $25.000",
        facts=["Niños y adultos.", "Solo particular: sin bono Fonasa.", PAGO_MED],
        h2="Habla, voz y audición, <em>en Carampangue</em>.", wa="una evaluación de fonoaudiología",
        p="Lenguaje, habla, voz, audición y deglución. En la evaluación se define si necesitas terapia y de qué tipo."),
    "ginecologia": dict(esp="Ginecología", landing="/ginecologia", gente=["rejon"],
        k="Consulta particular", v="$30.000", alt=("+$35.000", "ecografía ginecológica, adicional"), gancho="consulta $30.000",
        facts=["La ecografía ginecológica se hace en la misma atención y se paga aparte.", "Solo particular: sin bono Fonasa.", PAGO_MED],
        h2="Tu control ginecológico, <em>cerca de casa</em>.", wa="una consulta de ginecología",
        p="Control ginecológico, trastornos menstruales, anticoncepción, menopausia y dolor pélvico."),
    "matrona": dict(esp="Matrona", landing="/matrona", gente=["gomez"],
        k="Con Fonasa, precio preferente", v="$16.000", alt=("$20.000", "particular"), gancho="$16.000 con Fonasa",
        facts=["Con PAP: $25.000 Fonasa · $30.000 particular.", "No es bono: es un precio rebajado para pacientes Fonasa.", PAGO_MED],
        h2="Tu salud, <em>en cada etapa</em>.", wa="una hora con la matrona",
        p="Control preventivo, PAP y anticoncepción, con un precio preferente si eres Fonasa."),
    "gastroenterologia": dict(esp="Gastroenterología", landing="/gastroenterologia", gente=["quijano"],
        k="Consulta particular", v="$35.000", alt=None, gancho="consulta $35.000",
        facts=["Trae tus exámenes recientes si los tienes.", "Solo particular: sin bono Fonasa.", PAGO_MED],
        h2="Tu digestión, con <em>especialista</em>.", wa="una consulta de gastroenterología",
        p="Reflujo, gastritis, colon irritable, hígado graso y dolor abdominal. Las fechas siguen la agenda del especialista: el asistente de WhatsApp te muestra las horas libres."),
    "neurologia": dict(esp="Neurología", landing="/neurologia", gente=["gonzalez"],
        k="Teleconsulta", v="$65.000", alt=None, gancho="$65.000 por videollamada",
        facts=["El enlace llega por WhatsApp: no instalas ninguna aplicación.", "Desde los 15 años.", "Solo particular: sin bono Fonasa."],
        h2="Neurología, <em>sin viajar</em>.", wa="una teleconsulta de neurología",
        p="Cefaleas, migraña, epilepsia, temblores o problemas de memoria, por videollamada desde tu casa."),
    "kinesiologia": dict(esp="Kinesiología", landing="/kinesiologia", gente=["armijo", "etcheverry"],
        k="Sesión con bono Fonasa", v="$7.830", alt=("$20.000", "particular"), gancho="$7.830 con bono Fonasa",
        facts=["Con bono necesitas orden médica.", "El bono se emite aquí, con tu huella.", PAGO_MED],
        h2="Vuelve a <em>moverte sin dolor</em>.", wa="una sesión de kinesiología",
        p="Rehabilitación de lesiones, dolor muscular y postoperatorio, en sesiones presenciales."),
    "masoterapia": dict(esp="Masoterapia", landing="/masoterapia", gente=["acosta"],
        k="Espalda y cuello, 20 min", v="$17.990", alt=("$26.990", "40 min, con zona lumbar"), gancho="desde $17.990",
        facts=["Sin orden médica.", "Solo particular: sin bono Fonasa.", PAGO_MED],
        h2="Suelta la <em>tensión</em>.", wa="una sesión de masoterapia",
        p="Masaje terapéutico de espalda y cuello para contracturas y tensión, en 20 o 40 minutos."),
    "nutricion": dict(esp="Nutrición", landing="/nutricion", gente=["pinto"],
        k="Con bono Fonasa", v="$4.770", alt=("$20.000", "particular"), gancho="$4.770 con bono Fonasa",
        facts=["El bono se emite aquí, con tu huella.", "Bioimpedanciometría: $20.000, se agenda sola.", PAGO_MED],
        h2="Un plan de comidas <em>hecho para ti</em>.", wa="una hora con la nutricionista",
        p="Consulta presencial de una hora: sales con un plan de comidas pensado para tu rutina."),
    "nutriologia-diabetologia": dict(esp="Nutriología y diabetología", landing="/nutriologia-diabetologia", gente=["paz"],
        k="Teleconsulta", v="$60.000", alt=None, gancho="teleconsulta $60.000",
        facts=["Subespecialista UC en Diabetología y Nutrición Clínica.", "Por videollamada, desde los 15 años.", "Se reserva pagando el valor por adelantado."],
        h2="Diabetes y peso, <em>con médico especialista</em>.", wa="una teleconsulta con el Dr. Raúl Paz",
        p="Diabetes, prediabetes, sobrepeso, colesterol e hígado graso, con un médico que evalúa, pide exámenes e indica tratamiento."),
    "psicologia-adulto": dict(esp="Psicología adulto", landing="/psicologia-adulto", gente=["montalba", "rodriguez", "salas"],
        k="Sesión con bono Fonasa", v="$14.420", alt=("$25.000", "particular"), gancho="$14.420 con bono Fonasa",
        facts=["Sesiones de 45 minutos.", "Bono con Jorge Montalba o Juan Pablo Rodríguez. Con la Ps. Jacquelinne Salas, Fonasa paga $20.000 directo, sin bono.", PAGO_MED],
        h2="Un espacio para <em>hablar con calma</em>.", wa="una hora de psicología",
        p="Ansiedad, depresión, duelo, estrés y problemas de pareja, en un espacio confidencial."),
    "psicologia-infantil": dict(esp="Psicología infantil", landing="/psicologia-infantil", gente=["montalba", "salas"],
        k="Sesión con bono Fonasa", v="$14.420", alt=("$25.000", "particular"), gancho="$14.420 con bono Fonasa",
        facts=["Sesiones de 45 minutos.", "Bono con Jorge Montalba. Con la Ps. Jacquelinne Salas, Fonasa paga $20.000 directo, sin bono.", PAGO_MED],
        h2="Apoyo para tu hijo, <em>cerca de casa</em>.", wa="una hora de psicología infantil",
        p="Ansiedad, conducta y dificultades escolares o emocionales en niños y adolescentes."),
    "psiquiatria": dict(esp="Psiquiatría", landing="/psiquiatria", gente=["unibazo"],
        k="Teleconsulta", v="$60.000", alt=None, gancho="teleconsulta $60.000",
        facts=["Martes y jueves, por videollamada.", "Se reserva con abono por transferencia; si avisas con al menos 24 horas, se devuelve o se reagenda.", "Solo particular: sin bono Fonasa."],
        h2="Psiquiatría, <em>sin viajar</em>.", wa="una teleconsulta de psiquiatría",
        p="Depresión, ansiedad y trastornos del ánimo, por videollamada desde tu casa."),
    "podologia": dict(esp="Podología", landing="/podologia", gente=["guevara"],
        k="Podología básica", v="$20.000", alt=None, gancho="podología básica $20.000",
        facts=["Solo particular: sin bono Fonasa.", PAGO_MED],
        h2="Tus pies, <em>bien cuidados</em>.", wa="una hora de podología",
        p="Uñas encarnadas, hongos, callosidades y cuidado del pie, en atención presencial."),
    "oftalmologia": dict(esp="Examen de la vista", landing="/oftalmologia", gente=["celedon"],
        k="Examen de la vista", v="$15.000", alt=None, gancho="examen $15.000",
        facts=["Mismo valor para todos: sin bono Fonasa.", "Presencial, unos 20 minutos.", PAGO_MED],
        h2="Revisa tu vista, <em>en Carampangue</em>.", wa="un examen de la vista",
        p="Receta de lentes, presión intraocular y revisión preventiva de retina, en un solo examen."),
    "ecografia": dict(esp="Ecografía", landing="/ecografia", gente=["pardo", "sandoval"],
        k="Ecografía general", v="$40.000", alt=None, gancho="$40.000",
        facts=["Necesitas orden médica: si no la tienes, parte por medicina general.", "Informe en 3 días hábiles.", "Solo particular · " + PAGO_MED.lower()],
        h2="Tu ecografía, <em>sin ir a Concepción</em>.", wa="una ecografía",
        p="La realiza David Pardo, tecnólogo médico, y la informa el Dr. Sergio Sandoval Jara, médico radiólogo."),
    "odontologia-general": dict(esp="Odontología general", landing="/odontologia-general", gente=["burgos", "jimenez"],
        k="Evaluación dental", v="$15.000", alt=("desde $30.000", "limpieza"), gancho="evaluación $15.000",
        facts=["Tapadura (restauración) desde $30.000 por diente.", "Niños y adultos.", PAGO_DENT],
        h2="Tu dentista, <em>en Carampangue</em>.", wa="una evaluación dental",
        p="Limpiezas, tapaduras y extracciones, para niños y adultos."),
    "ortodoncia": dict(esp="Ortodoncia", landing="/ortodoncia", gente=["castillo", "burgos"],
        k="Primer paso: evaluación", v="$15.000", alt=("$120.000", "instalación de brackets"), gancho="evaluación $15.000",
        facts=["Partes con la evaluación con la Dra. Javiera Burgos.", "Brackets metálicos · controles $30.000.", PAGO_DENT],
        h2="Brackets en Carampangue, <em>sin viajar</em>.", wa="una evaluación de ortodoncia",
        p="Brackets metálicos con la Dra. Daniela Castillo. El primer paso siempre es la evaluación con la Dra. Javiera Burgos."),
    "endodoncia": dict(esp="Endodoncia", landing="/endodoncia", gente=["fredes"],
        k="Primer paso: evaluación", v="$15.000", alt=None, gancho="evaluación $15.000",
        facts=["Partes con la evaluación con dentista general.", "El valor del tratamiento depende del diente.", PAGO_DENT],
        h2="Salva tu diente, <em>cerca de casa</em>.", wa="una evaluación dental por endodoncia",
        p="Tratamiento de conducto para conservar tu diente. Partes con una evaluación con dentista general."),
    "implantologia": dict(esp="Implantología", landing="/implantologia", gente=["valdes"],
        k="Implante + corona, desde", v="$650.000", alt=("$15.000", "evaluación dental"), gancho="desde $650.000",
        facts=["Partes con la evaluación con dentista general.", PAGO_DENT],
        h2="Vuelve a <em>masticar tranquilo</em>.", wa="una evaluación para implante dental",
        p="Reemplazo permanente de un diente perdido: tornillo de titanio y corona. Partes con una evaluación con dentista general."),
    "estetica": dict(esp="Estética facial", landing="/estetica", gente=["fuentealba"],
        k="Evaluación facial", v="$15.000", alt=None, gancho="evaluación $15.000",
        facts=["Se abona al reservar. Si ese día te haces el tratamiento, se descuenta.", "Si avisas con al menos 24 horas, se devuelve o se reagenda.", PAGO_DENT],
        h2="Resultados <em>naturales</em>.", wa="una evaluación de estética facial",
        p="Toxina botulínica, rellenos y otros tratamientos, siempre después de una evaluación de tu rostro."),
}

# artículo → (ficha, [fichas "también"])
ARTICULOS = {
    "bono-fonasa-mle-arauco": ("medicina-general", ["kinesiologia", "nutricion"]),
    "cardiologia": ("cardiologia", ["medicina-general"]),
    "cefalea-tipos-tratamiento": ("medicina-general", ["neurologia"]),
    "diabetes-tipo-2-control": ("medicina-general", ["nutriologia-diabetologia", "nutricion"]),
    "dolor-lumbar-cuando-consultar": ("kinesiologia", ["medicina-general"]),
    "ecografia-precio-arauco": ("ecografia", ["ginecologia"]),
    "ecografia": ("ecografia", ["medicina-general"]),
    "embarazo-controles-mensuales": ("matrona", ["ginecologia"]),
    "endodoncia": ("endodoncia", ["odontologia-general"]),
    "estetica-facial": ("estetica", []),
    "fonoaudiologia": ("fonoaudiologia", []),
    "gastroenterologia": ("gastroenterologia", ["medicina-general"]),
    "ginecologia": ("ginecologia", ["matrona"]),
    "hipertension-arterial-control": ("medicina-general", ["cardiologia"]),
    "implantologia": ("implantologia", ["odontologia-general"]),
    "kinesiologia": ("kinesiologia", ["masoterapia"]),
    "limpieza-dental-precio-arauco": ("odontologia-general", ["ortodoncia"]),
    "masoterapia": ("masoterapia", ["kinesiologia"]),
    "matrona": ("matrona", ["ginecologia"]),
    "medicina-general": ("medicina-general", ["medicina-familiar"]),
    "neurologia": ("neurologia", ["medicina-general"]),
    "nutricion-baja-peso-saludable": ("nutricion", ["nutriologia-diabetologia"]),
    "nutricion": ("nutricion", ["nutriologia-diabetologia"]),
    "odontologia-general": ("odontologia-general", ["ortodoncia"]),
    "oftalmologia": ("oftalmologia", []),
    "ortodoncia": ("ortodoncia", ["odontologia-general"]),
    "otorrinolaringologia": ("otorrinolaringologia", ["fonoaudiologia"]),
    "podologia": ("podologia", []),
    "precio-implante-dental-arauco": ("implantologia", ["odontologia-general"]),
    "precio-ortodoncia-arauco": ("ortodoncia", ["odontologia-general"]),
    "psicologia-adulto": ("psicologia-adulto", ["psiquiatria"]),
    "psicologia-infantil-cuando-consultar": ("psicologia-infantil", ["fonoaudiologia"]),
    "psicologia-infantil": ("psicologia-infantil", ["fonoaudiologia"]),
    "psiquiatria": ("psiquiatria", ["psicologia-adulto"]),
    "rinoplastia-funcional-tabique": ("medicina-general", []),
    "vacunas-pni-calendario-2026": ("medicina-general", ["medicina-familiar"]),
}

# Grupos del índice
GRUPOS = [
    ("Medicina y especialidades", ["medicina-general", "cardiologia", "gastroenterologia", "neurologia", "otorrinolaringologia",
                                   "hipertension-arterial-control", "diabetes-tipo-2-control", "cefalea-tipos-tratamiento",
                                   "vacunas-pni-calendario-2026", "rinoplastia-funcional-tabique", "bono-fonasa-mle-arauco"]),
    ("Mujer", ["ginecologia", "matrona", "embarazo-controles-mensuales"]),
    ("Salud mental", ["psicologia-adulto", "psicologia-infantil", "psicologia-infantil-cuando-consultar", "psiquiatria"]),
    ("Rehabilitación y bienestar", ["kinesiologia", "dolor-lumbar-cuando-consultar", "masoterapia", "fonoaudiologia",
                                    "nutricion", "nutricion-baja-peso-saludable", "podologia"]),
    ("Diagnóstico", ["ecografia", "ecografia-precio-arauco", "oftalmologia"]),
    ("Dental y estética", ["odontologia-general", "limpieza-dental-precio-arauco", "ortodoncia", "precio-ortodoncia-arauco",
                           "endodoncia", "implantologia", "precio-implante-dental-arauco", "estetica-facial"]),
]

# ─────────────────────────────────────────────────────────────────────────────
# CORRECCIONES DE DATOS (cada una contradice una fuente verificada).
# (archivo, viejo, nuevo, motivo). `viejo` debe existir: si no, el script falla.
# Se aplican a TODO el archivo (cuerpo visible Y su espejo JSON-LD/meta).
# ─────────────────────────────────────────────────────────────────────────────
_PSICO_ROW_OLD = '<td class="price-bono">$14.420</td>\n          <td class="price-part">$20.000</td>'
_PSICO_ROW_NEW = '<td class="price-bono">$14.420</td>\n          <td class="price-part">$25.000</td>'
CORRECCIONES: list[tuple[str, str, str, str]] = [
    # ── bono-fonasa-mle-arauco
    ("bono-fonasa-mle-arauco", _PSICO_ROW_OLD, _PSICO_ROW_NEW,
     "Psicología particular $20.000 → $25.000 (claude_helper: di SIEMPRE $25.000; confirmado por el dueño)"),
    ("bono-fonasa-mle-arauco", "<strong>Pasá por recepción</strong>", "<strong>Pasa por recepción</strong>", "voseo"),
    ("bono-fonasa-mle-arauco", "<strong>Laraquete</strong> — 8 km · 10 min vía Ruta 160", "<strong>Laraquete</strong> — 12 km · 12 min por la Ruta 160", "distancias: COMUNAS_ARAUCO (OSRM 6-oct)"),
    ("bono-fonasa-mle-arauco", "<strong>Ramadilla</strong> — 6 km · 10 min vía ruta rural", "<strong>Ramadilla</strong> — 7 km · 7 min por la Ruta 160", "distancias"),
    ("bono-fonasa-mle-arauco", "<strong>Arauco</strong> — 15 km · 20 min vía Ruta P-22", "<strong>Arauco</strong> — 8 km · 11 min por la Ruta P-20", "distancias"),
    ("bono-fonasa-mle-arauco", "<strong>Curanilahue</strong> — 25 km · 30 min vía Ruta 160", "<strong>Curanilahue</strong> — 30 km · 30 min por la Ruta 160", "distancias"),
    ("bono-fonasa-mle-arauco", "<strong>Los Álamos</strong> — 35 km · 40 min vía Ruta 160", "<strong>Los Álamos</strong> — 53 km · 55 min por la Ruta 160", "distancias"),
    ("bono-fonasa-mle-arauco", "<strong>Lebu</strong> — 50 km · 60 min vía Ruta P-40", "<strong>Lebu</strong> — 77 km · 1 h 20 min por la Ruta 160", "distancias"),
    ("bono-fonasa-mle-arauco", "<strong>Cañete</strong> — 70 km · 80 min vía Ruta P-72", "<strong>Cañete</strong> — 72 km · 1 h 20 min por la Ruta P-60 y la Ruta 160", "distancias"),
    ("bono-fonasa-mle-arauco", "<strong>Contulmo</strong> — 90 km · 100 min vía Ruta P-72 + P-60", "<strong>Contulmo</strong> — 106 km · 2 h por la Ruta P-60 y la Ruta 160", "distancias"),
    ("bono-fonasa-mle-arauco", "<strong>Tirúa</strong> — 110 km · 120 min vía Ruta P-72 sur", "<strong>Tirúa</strong> — 138 km · 2 h 30 min por las rutas P-72, P-60 y 160", "distancias"),
    ("limpieza-dental-precio-arauco", "Notás que", "Notas que", "voseo"),
    # ── cardiologia
    ("cardiologia", "Se realiza en el mismo centro durante la consulta cuando corresponde.",
     "Lo realiza el Dr. Millán en el centro una vez al mes; quien lo necesita queda en lista de espera.",
     "Ecocardiograma: 1 vez al mes con lista de espera (claude_helper), no 'durante la consulta'"),
    # ── ecografia-precio-arauco
    ("ecografia-precio-arauco", "La ginecológica transvaginal ($35.000) la hace el <strong>Dr. Tirso Rejón</strong>, ginecólogo.",
     "La ginecológica transvaginal la hace el <strong>Dr. Tirso Rejón</strong>, ginecólogo, en la consulta de ginecología: $35.000 adicionales a la consulta ($30.000).",
     "Eco ginecológica $35.000 es ADICIONAL a la consulta $30.000 (dueño + claude_helper)"),
    ("ecografia-precio-arauco", "la ginecológica transvaginal cuesta $35.000. Todas",
     "la ginecológica transvaginal cuesta $35.000, adicionales a la consulta de ginecología ($30.000). Todas", "eco gine adicional (JSON-LD FAQ)"),
    ("ecografia-precio-arauco", "<td><strong>Ecografía ginecológica</strong></td><td>$35.000</td>",
     "<td><strong>Ecografía ginecológica</strong></td><td>$35.000 adicional a la consulta ($30.000)</td>", "eco gine adicional (tabla)"),
    ("ecografia-precio-arauco", "$40.000 las ecografías generales y $35.000 la ginecológica transvaginal.",
     "$40.000 las ecografías generales y $35.000 la ginecológica transvaginal, adicionales a la consulta de ginecología ($30.000).", "eco gine adicional (FAQ visible)"),
    ("ecografia-precio-arauco", "ecografías generales (ginecológica: $35.000)", "ecografías generales (ginecológica: $35.000 + consulta $30.000)", "eco gine adicional (lateral)"),
    ("ecografia-precio-arauco", ", miomas, control de embarazo precoz.", ", miomas.", "la ecografía obstétrica NO se realiza en el CMC"),
    ("ecografia-precio-arauco", "2-3 días hábiles", "3 días hábiles", "informe en 3 días hábiles (claude_helper)"),
    # ── ecografia
    ("ecografia", "Atendemos derivaciones de tu médico tratante o consulta directa cuando corresponde.",
     "Necesitas orden médica: si no la tienes, puedes agendar con Medicina General en el CMC; el médico evalúa y, si corresponde, la extiende.",
     "la ecografía REQUIERE orden médica (claude_helper)"),
    ("ecografia", "Te avisamos las próximas fechas disponibles del Dr. Pardo y agendamos tu hora. Si tienes orden médica, mejor — ayuda a aprovechar mejor el examen.",
     "Te avisamos las próximas fechas disponibles de David Pardo y agendamos tu hora. Ten a mano tu orden médica.",
     "David Pardo es tecnólogo médico, NO 'Dr.'; la orden médica es requisito"),
    ("ecografia", "Para mamaria, pélvica completa o transvaginal sí es ideal. Para abdominal, tiroidea o partes blandas puedes consultar directamente.",
     "Sí. Las ecografías del centro requieren orden médica. Si no la tienes, puedes agendar con Medicina General en el CMC: el médico evalúa y, si corresponde, la extiende.",
     "orden médica requerida (JSON-LD FAQ)"),
    ("ecografia", "Para algunos exámenes específicos (mamaria, pélvica completa, transvaginal) sí es ideal. Para abdominal, tiroidea o partes blandas puedes consultar directamente. Si tienes dudas escríbenos.",
     "Sí. Las ecografías del centro requieren orden médica. Si no la tienes, puedes agendar con Medicina General en el CMC: el médico evalúa y, si corresponde, la extiende.",
     "orden médica requerida (FAQ visible)"),
    ("ecografia", "Cédula de identidad, orden médica si tienes,", "Cédula de identidad, orden médica,", "orden médica requerida"),
    # ── embarazo-controles-mensuales
    ("embarazo-controles-mensuales", "<p>En el CMC contamos con ecografía gineco-obstétrica a cargo del Dr. Tirso Rejón.</p>",
     "<p>En el CMC, el Dr. Tirso Rejón realiza la ecografía ginecológica (transvaginal). La ecografía obstétrica no se realiza en el centro.</p>",
     "ecografía obstétrica NO disponible en el CMC (claude_helper)"),
    ("embarazo-controles-mensuales", "En el CMC atendemos ginecología y ecografía gineco-obstétrica. Agenda tu control o tu ecografía directamente por WhatsApp",
     "En el CMC atendemos ginecología y matrona. La ecografía obstétrica no se realiza en el centro. Agenda tu control directamente por WhatsApp",
     "ecografía obstétrica NO disponible"),
    ("embarazo-controles-mensuales", "Sí. En el CMC ofrecemos ginecología y ecografía gineco-obstétrica. Muchas pacientes combinan sus controles regulares con el sistema público y complementan con ecografías o controles adicionales en el CMC.",
     "Sí. En el CMC atendemos ginecología y matrona. La ecografía obstétrica no se realiza en el centro. Muchas pacientes combinan sus controles regulares con el sistema público y complementan con controles adicionales en el CMC.",
     "ecografía obstétrica NO disponible (FAQ visible y JSON-LD)"),
    # ── estetica-facial
    ("estetica-facial", "<strong>Dra. Valentina Fuentealba</strong>, médico estético que coordina fechas según demanda.",
     "<strong>Dra. Valentina Fuentealba</strong>, odontóloga dedicada a la estética facial, que coordina fechas según demanda.",
     "la Dra. Fuentealba es ODONTÓLOGA (claude_helper), no médico"),
    ("estetica-facial", " Usamos técnicas internacionales con productos certificados.</p>", "</p>", "sin 'certificados' (regla del dueño) ni promesas no verificadas"),
    ("estetica-facial", "Resultados naturales 12–18 meses.", "El efecto dura de 8 a 12 meses.", "ácido hialurónico dura 8-12 meses (claude_helper)"),
    ("estetica-facial", "Sí, cuando los aplica un médico calificado con productos certificados con registro ISP.",
     "Sí, cuando los aplica una profesional capacitada, después de una evaluación.", "no es médico; sin 'certificados' (JSON-LD FAQ)"),
    ("estetica-facial", "Sí, cuando los aplica un médico calificado con productos certificados. La Dra. Fuentealba usa marcas con registro ISP. Los efectos adversos serios son extremadamente raros.",
     "Sí, cuando los aplica una profesional capacitada, después de una evaluación. La Dra. Fuentealba revisa tu caso antes de cualquier procedimiento. Los efectos adversos serios son extremadamente raros.",
     "no es médico; sin 'certificados' (FAQ visible)"),
    # ── fonoaudiologia
    ("fonoaudiologia", "Cuando corresponde, coordinamos directamente con el Dr. Borrego.",
     "Cuando corresponde, te orientamos para una evaluación médica.", "otorrinolaringología sin atención presencial por ahora (main.py OTORRINO_ACTIVO)"),
    # ── ginecologia
    ("ginecologia", "ginecólogo-obstetra que también realiza <strong>ecografías ginecológicas y obstétricas</strong> (transvaginal, abdominal, doppler, control embarazo) en la misma consulta.",
     "ginecólogo que también realiza la <strong>ecografía ginecológica</strong> (transvaginal) en la misma atención, con un valor adicional de $35.000. La ecografía obstétrica no se realiza en el centro.",
     "eco obstétrica NO disponible; eco gine adicional"),
    ("ginecologia", "<span>Ecografías mensuales, control prenatal, derivación a maternidad cuando corresponde.</span>",
     "<span>Orientación y control, derivación a maternidad cuando corresponde. La ecografía obstétrica no se realiza en el centro.</span>",
     "eco obstétrica NO disponible"),
    ("ginecologia", "<li><strong>Ecografía obstétrica</strong> — control de embarazo en cada trimestre.</li>\n", "", "eco obstétrica NO disponible"),
    ("ginecologia", "<li><strong>Ecografía transvaginal</strong> ($35.000) — evalúa",
     "<li><strong>Ecografía transvaginal</strong> ($35.000, adicional a la consulta) — evalúa", "eco gine adicional"),
    ("ginecologia", "Ecografía ginecológica $35.000 (en la misma consulta).", "Ecografía ginecológica $35.000 adicional, en la misma atención.", "eco gine adicional"),
    ("ginecologia", "Ecografía ginecológica $35.000 en la misma consulta.", "Ecografía ginecológica $35.000 adicional, en la misma atención.", "eco gine adicional"),
    ("ginecologia", "Sí. Control prenatal con ecografía obstétrica cada trimestre. Embarazos de alto riesgo se derivan a Concepción.",
     "No. La ecografía obstétrica no se realiza en el centro. El Dr. Rejón realiza la ecografía ginecológica (transvaginal).",
     "eco obstétrica NO disponible (JSON-LD FAQ)"),
    ("ginecologia", "Sí. Control prenatal con ecografía obstétrica en cada trimestre. Embarazos de alto riesgo se derivan a maternidad de Concepción.",
     "No. La ecografía obstétrica no se realiza en el centro. El Dr. Rejón realiza la ecografía ginecológica (transvaginal).",
     "eco obstétrica NO disponible (FAQ visible)"),
    ("ginecologia", "\"description\": \"Ginecología y obstetricia en Carampangue.", "\"description\": \"Ginecología en Carampangue.", "sin obstetricia (JSON-LD)"),
    # ── hipertension
    ("hipertension-arterial-control", "a 18 km de Arauco y 25 km de Curanilahue.", "a 8 km de Arauco y 30 km de Curanilahue.", "distancias: COMUNAS_ARAUCO"),
    # ── limpieza dental
    ("limpieza-dental-precio-arauco", " (incluye cuotas con tarjeta).", ".", "sin cuotas (regla del dueño)"),
    ("limpieza-dental-precio-arauco", "Efectivo · Transferencia bancaria · Débito · Crédito · Crédito en cuotas (3, 6, 12 cuotas)",
     "Efectivo · Transferencia bancaria · Débito · Crédito", "sin cuotas"),
    ("limpieza-dental-precio-arauco", "<td>desde $35.000</td>", "<td>desde $30.000</td>", "tapadura desde $30.000 por diente (dueño)"),
    ("limpieza-dental-precio-arauco", "<li><strong>Tapadura:</strong> desde $35.000</li>", "<li><strong>Tapadura:</strong> desde $30.000</li>", "tapadura desde $30.000"),
    ("limpieza-dental-precio-arauco", "<p>La <a href=\"/blog/ortodoncia\">Dra. Daniela Castillo</a> coordina las limpiezas con los odontólogos generales del CMC.</p>",
     "<p>Las limpiezas las realizan los odontólogos generales del CMC, la Dra. Javiera Burgos y el Dr. Carlos Jiménez; la <a href=\"/blog/ortodoncia\">ortodoncia</a> la lleva la Dra. Daniela Castillo.</p>",
     "la Dra. Castillo es ortodoncista; las limpiezas son de odontología general"),
    ("limpieza-dental-precio-arauco", "— brackets metálicos y estéticos (Dra. Daniela Castillo)", "— brackets metálicos (Dra. Daniela Castillo)", "sin brackets estéticos"),
    # ── masoterapia
    ("masoterapia", "o 40 min $26.990 (cuerpo completo)", "o 40 min $26.990 (espalda y cuello, con zona lumbar)",
     "40 min $26.990 es espalda y cuello con zona lumbar, no cuerpo completo (claude_helper)"),
    ("masoterapia", "o 40 min ($26.990) cuerpo completo", "o 40 min ($26.990) con zona lumbar", "masoterapia 40 min"),
    ("masoterapia", "La de 40 min ($26.990) abarca todo el cuerpo.", "La de 40 min ($26.990) es más extensa e incluye la zona lumbar.", "masoterapia 40 min"),
    ("masoterapia", "o <strong>40 min $26.990</strong> (cuerpo completo)", "o <strong>40 min $26.990</strong> (espalda y cuello, con zona lumbar)", "masoterapia 40 min"),
    ("masoterapia", "o 40 min para cuerpo completo.", "o 40 min si quieres incluir la zona lumbar.", "masoterapia 40 min"),
    # ── odontologia-general
    ("odontologia-general", "Tapadura $35.000", "Tapadura desde $30.000", "tapadura desde $30.000 (dueño)"),
    ("odontologia-general", "tapaduras desde $35.000", "tapaduras desde $30.000", "tapadura desde $30.000"),
    ("odontologia-general", "tapadura desde $35.000", "tapadura desde $30.000", "tapadura desde $30.000"),
    ("odontologia-general", "Tapadura (empaste) — desde $35.000", "Tapadura (empaste) — desde $30.000", "tapadura desde $30.000"),
    ("odontologia-general", "\"Desde $35.000. Puede variar", "\"Desde $30.000 por diente. Puede variar", "tapadura desde $30.000 (JSON-LD)"),
    ("odontologia-general", "Desde <strong>$35.000</strong>. El precio", "Desde <strong>$30.000</strong> por diente. El precio", "tapadura desde $30.000"),
    ("odontologia-general", "tapadura desde <strong>$35.000</strong>", "tapadura desde <strong>$30.000</strong>", "tapadura desde $30.000"),
    ("odontologia-general", "En el CMC: control desde $30.000, limpieza", "En el CMC: evaluación dental $15.000, limpieza",
     "la evaluación dental es $15.000 (dueño), no 'control desde $30.000' (JSON-LD)"),
    ("odontologia-general", "En el CMC: control/diagnóstico desde <strong>$30.000</strong>, limpieza", "En el CMC: evaluación dental <strong>$15.000</strong>, limpieza",
     "evaluación dental $15.000"),
    ("odontologia-general", "El CMC en Carampangue (15 km de Arauco)", "El CMC en Carampangue (8 km de Arauco)", "distancias (JSON-LD)"),
    ("odontologia-general", "El CMC en Carampangue (15 km de Arauco · 20 min por Ruta P-22)", "El CMC en Carampangue (8 km de Arauco · unos 11 min por la Ruta P-20)", "distancias"),
    ("odontologia-general", "Curanilahue (30 min), Lebu (1h), Cañete (1h 20min), Arauco (20 min), Los Álamos (40 min).",
     "Curanilahue (30 min), Lebu (1 h 20 min), Cañete (1 h 20 min), Arauco (11 min), Los Álamos (55 min).", "distancias (JSON-LD)"),
    ("odontologia-general", "(1 hora), <a href=\"/blog/odontologia-general-canete\">Cañete</a> (1h 20 min), <a href=\"/blog/odontologia-general-arauco\">Arauco</a> (20 min), <a href=\"/blog/odontologia-general-los-alamos\">Los Álamos</a> (40 min).",
     "(1 h 20 min), <a href=\"/blog/odontologia-general-canete\">Cañete</a> (1 h 20 min), <a href=\"/blog/odontologia-general-arauco\">Arauco</a> (11 min), <a href=\"/blog/odontologia-general-los-alamos\">Los Álamos</a> (55 min).",
     "distancias"),
    # ── oftalmologia
    ("oftalmologia", "realiza el examen oftalmológico más accesible de la Provincia de Arauco: $15.000 para todos los pacientes",
     "realiza un examen de la vista completo por $15.000 para todos los pacientes", "sin superlativos ni comparación con otros centros (regla del dueño)"),
    ("oftalmologia", " y precisión garantizada, si la necesitas.", ", si la necesitas.", "sin garantías"),
    ("oftalmologia", "<p>Además, a <strong>$15.000</strong> es el examen oftalmológico más accesible de la Provincia de Arauco — la referencia local en ópticas y centros privados de la zona ronda los $20.000 por un examen que, en general, no incluye el mismo tamizaje de retina y presión intraocular.</p>",
     "<p>El valor es <strong>$15.000</strong> para todos los pacientes e incluye el tamizaje de retina y de presión intraocular.</p>",
     "sin comparación con otros centros ni precios de terceros no verificados"),
    # ── ortodoncia
    ("ortodoncia", "Presupuesto inicial $15.000 (gratis si comienzas tratamiento ese día).",
     "La evaluación inicial cuesta $15.000 y se descuenta si ese mismo día comienzas o dejas pagado el tratamiento previo.",
     "nunca 'gratis': la evaluación $15.000 se descuenta del tratamiento previo del mismo día (regla del dueño, JSON-LD)"),
    ("ortodoncia", "<h3>2. Presupuesto: $15.000 (gratis si comienzas ese día)</h3>", "<h3>2. Evaluación: $15.000</h3>", "sin 'gratis'"),
    ("ortodoncia", "Si decides empezar tu tratamiento previo en la misma visita (limpieza, restauraciones o lo que necesites antes de los brackets), <strong>el presupuesto te sale gratis</strong>. Solo pagas la acción que se realice ese día.",
     "Si ese mismo día comienzas o dejas pagado tu tratamiento previo (limpieza, restauraciones o lo que necesites antes de los brackets), <strong>los $15.000 de la evaluación se descuentan</strong> de ese tratamiento.",
     "sin 'gratis'"),
    ("ortodoncia", "presupuesto $15.000 (gratis si comienzas tratamiento ese día).", "evaluación $15.000, que se descuenta si ese día comienzas el tratamiento previo.", "sin 'gratis'"),
    ("ortodoncia", "El presupuesto inicial cuesta $15.000 (gratis si comienzas tratamiento ese día).",
     "La evaluación inicial cuesta $15.000 y se descuenta si ese mismo día comienzas o dejas pagado el tratamiento previo.", "sin 'gratis'"),
    ("ortodoncia", "Presupuesto $15.000 (gratis si comienzas tratamiento ese día).", "Evaluación $15.000, que se descuenta si ese día comienzas el tratamiento previo.", "sin 'gratis'"),
    ("ortodoncia", "<strong>Dra. Javiera Burgos</strong> o <strong>Dr. Carlos Jiménez</strong>. Ellos evalúan", "<strong>Dra. Javiera Burgos</strong>, dentista general. Ella evalúa",
     "la evaluación de ortodoncia es SIEMPRE con la Dra. Burgos (claude_helper)"),
    # ── precio-implante-dental-arauco
    ("precio-implante-dental-arauco", "El precio de un implante dental en la zona de Arauco y la provincia del Biobío es comparable al de Concepción, con la ventaja de no tener que hacer el traslado. En CMC Carampangue atendemos con la <strong>Dra. Aurora Valdés</strong>, implantóloga, quien realiza la evaluación y el procedimiento completo.",
     "En CMC Carampangue, los implantes los realiza la <strong>Dra. Aurora Valdés</strong>, implantóloga, sin que tengas que viajar. Todo parte con la evaluación con dentista general.",
     "sin comparaciones con otros lugares; el primer paso es la evaluación con dentista general"),
    ("precio-implante-dental-arauco", "es más económica a corto plazo ($150.000 a $350.000), pero", "es más económica a corto plazo, pero",
     "rango de precios de terceros no verificado (regla: sin tablas de rangos)"),
    # ── precio-ortodoncia-arauco (tabla de rangos de $1,2–4 M: contradice los precios reales del CMC)
    ("precio-ortodoncia-arauco", "Precio ortodoncia en Arauco y Curanilahue 2026: brackets metálicos, estéticos e invisibles. Guía completa con valores reales y cómo agendar en CMC Carampangue.",
     "Precio ortodoncia en Arauco y Curanilahue 2026: evaluación $15.000, instalación de brackets metálicos $120.000 y controles $30.000 en CMC Carampangue.",
     "meta: sin brackets estéticos/invisibles; precios de entrada reales"),
    ("precio-ortodoncia-arauco", "Guía de precios de ortodoncia en Arauco 2026: brackets metálicos $1.2M-$1.8M, estéticos $1.5M-$2.2M. Dra. Daniela Castillo en CMC Carampangue.",
     "Ortodoncia en Arauco 2026: evaluación $15.000, instalación de brackets metálicos $120.000 y controles $30.000. Dra. Daniela Castillo en CMC Carampangue.",
     "og:description con rangos inventados"),
    ("precio-ortodoncia-arauco", "En la zona de Arauco, Curanilahue y la provincia del Biobío, los precios son comparables a los de Concepción, con la ventaja de no tener que trasladarse.</p>",
     "En el CMC se paga por etapa, así que siempre sabes cuánto pagas ese día.</p>", "sin comparación con otros lugares"),
    ("precio-ortodoncia-arauco", "En el CMC Carampangue atendemos con la <strong>Dra. Daniela Castillo</strong>, ortodoncista, quien realiza la evaluación inicial y diseña el plan de tratamiento individualizado.",
     "En el CMC Carampangue, la primera cita es la evaluación con la <strong>Dra. Javiera Burgos</strong>, dentista general ($15.000). Ella gestiona la derivación a la ortodoncista, la <strong>Dra. Daniela Castillo</strong>, que diseña tu plan de tratamiento.",
     "la evaluación inicial la hace la Dra. Burgos (claude_helper)"),
    ("precio-ortodoncia-arauco", "<h2>Tabla de <em>precios orientativos</em> 2026</h2>", "<h2>Precios <em>en el CMC</em> 2026</h2>", "tabla de rangos → precios de entrada reales"),
    ("precio-ortodoncia-arauco", "<p>Los valores a continuación son rangos aproximados para 2026 en la zona de Arauco. El precio final depende de la complejidad del caso y se confirma en la evaluación.</p>",
     "<p>Estos son los valores de cada etapa en el CMC. La duración del tratamiento depende de tu caso y se define después de la evaluación.</p>", "precios reales"),
    ("precio-ortodoncia-arauco", """<thead><tr><th>Tipo de ortodoncia</th><th>Rango de precio total</th><th>Observaciones</th></tr></thead>
          <tbody>
            <tr><td><strong>Brackets metálicos</strong></td><td>$1.200.000 – $1.800.000</td><td>Incluye instalación y controles mensuales</td></tr>
            <tr><td><strong>Brackets estéticos (zafiro o cerámica)</strong></td><td>$1.500.000 – $2.200.000</td><td>Menos visibles que los metálicos</td></tr>
            <tr><td><strong>Brackets linguales</strong></td><td>$2.500.000 – $3.500.000</td><td>Se colocan en la cara interna del diente</td></tr>
            <tr><td><strong>Alineadores (tipo Invisalign)</strong></td><td>$2.000.000 – $4.000.000</td><td>Dependiendo del sistema y nº de etapas</td></tr>
            <tr><td><strong>Aparatos removibles (niños)</strong></td><td>$150.000 – $400.000</td><td>Para interceptar problemas en desarrollo</td></tr>
          </tbody>""",
     """<thead><tr><th>Etapa</th><th>Valor</th><th>Detalle</th></tr></thead>
          <tbody>
            <tr><td><strong>Evaluación dental</strong></td><td>$15.000</td><td>Con la Dra. Javiera Burgos. Se descuenta si ese día comienzas o dejas pagado el tratamiento previo</td></tr>
            <tr><td><strong>Estudio radiográfico</strong></td><td>$40.000</td><td>Precio especial: panorámica, telerradiografía y bitewing</td></tr>
            <tr><td><strong>Instalación de brackets metálicos</strong></td><td>$120.000</td><td>Boca completa (una arcada: $60.000)</td></tr>
            <tr><td><strong>Control</strong></td><td>$30.000</td><td>Ajuste periódico de arcos y elásticos</td></tr>
          </tbody>""",
     "tabla de rangos de $150.000 a $4.000.000 (estéticos, linguales, alineadores) → precios reales de entrada del CMC (claude_helper + dueño)"),
    ("precio-ortodoncia-arauco", "<p><strong>Importante:</strong> estos valores son orientativos. El precio real se determina en la evaluación con la ortodoncista, una vez que se analiza la maloclusión, el tiempo estimado de tratamiento y el tipo de aparato más adecuado para cada caso.</p>",
     "<p><strong>Importante:</strong> en el CMC trabajamos con brackets metálicos. La duración y el número de controles se definen con la ortodoncista según tu caso.</p>",
     "solo brackets metálicos"),
    ("precio-ortodoncia-arauco", """<p>Cuando preguntas el precio de la ortodoncia, es importante saber qué abarca ese monto. En CMC, el tratamiento incluye:</p>
        <ul>
          <li>Evaluación ortodóntica inicial (diagnóstico, estudio de modelos)</li>
          <li>Instalación del aparato (brackets y arcos)</li>
          <li>Controles mensuales durante todo el tratamiento</li>
          <li>Cambios de arco según evolución</li>
          <li>Retención post-tratamiento (retenedores)</li>
        </ul>
        <p>Los exámenes complementarios (radiografía panorámica, cefalométrica) pueden tener un costo adicional si no los tienes. Consulta al agendar.</p>""",
     """<p>Cuando preguntas el precio de la ortodoncia, es importante saber qué abarca cada monto. En el CMC cada etapa tiene su valor:</p>
        <ul>
          <li>La evaluación dental, donde se revisa tu boca y se indica el estudio radiográfico</li>
          <li>El estudio radiográfico (panorámica, telerradiografía y bitewing), siempre antes de instalar</li>
          <li>La instalación de los brackets metálicos, que incluye el arco inicial</li>
          <li>Los controles periódicos, donde se ajustan arcos y elásticos</li>
        </ul>
        <p>Al terminar, el retiro y la contención tienen su propio valor; te lo informan en la evaluación.</p>""",
     "el precio NO incluye controles ni contención (se pagan por etapa)"),
    ("precio-ortodoncia-arauco", "<p>Con la Dra. Daniela Castillo en CMC Carampangue. Evaluación inicial para conocer tu caso y el plan de tratamiento.</p>",
     "<p>Tu primera cita es la evaluación con la Dra. Javiera Burgos ($15.000). Ella te deriva a la ortodoncista, la Dra. Daniela Castillo.</p>", "primer paso real"),
    ("precio-ortodoncia-arauco", "<summary>¿Las cuotas de ortodoncia se pagan mensualmente?", "<summary>¿Cómo se paga la ortodoncia?", "sin cuotas"),
    ("precio-ortodoncia-arauco", "Depende del esquema de pago del dentista. En CMC el valor total se puede dividir según lo acordado con la ortodoncista. Consulta las opciones al momento de la evaluación inicial.",
     "Cada etapa se paga el día en que se realiza: la instalación ($120.000) y cada control ($30.000). Puedes pagar en efectivo, transferencia, débito o crédito.",
     "sin cuotas ni financiamiento (regla del dueño)"),
    # ── psiquiatria (horario real: martes y jueves 16:00–20:00, app/medilink.py)
    ("psiquiatria", "martes 16-20h y jueves 15:20-20h", "martes y jueves de 16 a 20 h", "jueves 16:00–20:00 (medilink.py, 14-ago)"),
    ("psiquiatria", "los jueves de 15:20 a 20:00", "los jueves de 16:00 a 20:00", "jueves 16:00–20:00"),
    ("psiquiatria", "<strong>jueves de 15:20 a 20:00</strong>", "<strong>jueves de 16:00 a 20:00</strong>", "jueves 16:00–20:00"),
    ("psiquiatria", "jueves 15:20-20:00", "jueves 16:00–20:00", "jueves 16:00–20:00"),
    ("psiquiatria", "no esperes a la teleconsulta del jueves.", "no esperes a la teleconsulta.", "atiende martes y jueves"),
    ("psiquiatria", "teleconsulta de los jueves</em>", "teleconsulta de martes y jueves</em>", "atiende martes y jueves"),
    ("psiquiatria", "<strong>Días y horario fijos:</strong> solo jueves, de 16:00 a 20:00 horas.", "<strong>Días y horario fijos:</strong> martes y jueves, de 16:00 a 20:00 horas.", "atiende martes y jueves"),
    ("psiquiatria", "dentro del bloque del jueves.", "dentro del bloque del martes o del jueves.", "atiende martes y jueves"),
    ("psiquiatria", "tu horario del próximo jueves.", "tu horario del próximo martes o jueves.", "atiende martes y jueves"),
    ("psiquiatria", "igual que la primera consulta, los días jueves.", "igual que la primera consulta, los martes o jueves.", "atiende martes y jueves"),
    ("psiquiatria", "Jueves 16:00-20:00", "Martes y jueves 16:00–20:00", "atiende martes y jueves"),
    # ── rinoplastia (otorrino sin atención presencial: main.py redirige /blog/otorrinolaringologia*)
    ("rinoplastia-funcional-tabique", "Otorrino Dr. Manuel Borrego en Carampangue, Arauco.", "Evaluación inicial con medicina general en Carampangue, Arauco.",
     "otorrinolaringología sin atención presencial por ahora (meta)"),
    ("rinoplastia-funcional-tabique", "<p>El Dr. Manuel Borrego, otorrinolaringólogo del CMC, evalúa la desviación de tabique y otras patologías nasales. Atiende en Carampangue, a 25 km de Curanilahue.</p>",
     "<p>Por ahora, la otorrinolaringología del CMC no tiene atención presencial. Si tienes la nariz tapada de forma permanente o sospechas una desviación de tabique, puedes partir por Medicina General en Carampangue: el médico te evalúa y, si corresponde, te deriva.</p>",
     "otorrinolaringología sin atención presencial; ruta real: medicina general (claude_helper)"),
    # ── vacunas (no hay fuente de que el CMC aplique vacunas)
    ("vacunas-pni-calendario-2026", "<h2>Vacunas fuera del PNI que se pueden obtener en el CMC</h2>", "<h2>Vacunas fuera del PNI</h2>",
     "no hay registro de que el CMC aplique vacunas (por confirmar con el dueño)"),
    ("vacunas-pni-calendario-2026", "<p>En el CMC y en farmacias de la región puedes acceder a vacunas que no están en el PNI pero que pueden ser recomendadas según tu situación:</p>",
     "<p>En farmacias y vacunatorios privados puedes acceder a vacunas que no están en el PNI pero que pueden ser recomendadas según tu situación. Si no sabes si te corresponde alguna, consúltalo con tu médico:</p>",
     "no hay registro de que el CMC aplique vacunas"),
    ("vacunas-pni-calendario-2026", "Para vacunas no incluidas en el PNI, el CMC y farmacias también ofrecen alternativas privadas.",
     "Para vacunas no incluidas en el PNI, existen alternativas privadas en farmacias y vacunatorios.", "no hay registro de que el CMC aplique vacunas"),
]

# Ortografía (tildes faltantes en textos visibles; no cambia sentido)
TILDES = [
    (r"\bNutricion\b", "Nutrición"), (r"\bSabado\b", "Sábado"), (r"\bArticulos\b", "Artículos"), (r"\bAtencion\b", "Atención"),
    (r"\bEvaluacion\b", "Evaluación"), (r"\bhipertension\b", "hipertensión"), (r"\bdespues\b", "después"), (r"\bfisica\b", "física"),
    (r"\bfreitura\b", "fritura"), (r"\bdias\b", "días"),
]

COMUNAS = ("carampangue", "laraquete", "ramadilla", "arauco", "lebu", "canete", "tirua", "curanilahue", "los-alamos", "contulmo")

PLACEHOLDER_RE = re.compile(r"\[([A-ZÁÉÍÓÚa-záéíóúñ][^\]<>]{1,40})\]")

# ─────────────────────────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────────────────────────

def fuente(slug: str) -> str:
    rel = f"templates/blog/{slug}.html"
    try:
        return subprocess.run(["git", "-C", str(ROOT), "show", f"HEAD:{rel}"], check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError:
        return (ROOT / rel).read_text(encoding="utf-8")


def fuente_index() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:templates/blog_index.html"], check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError:
        return INDEX.read_text(encoding="utf-8")


def texto(s: str) -> str:
    s = re.sub(r"<svg.*?</svg>", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFD", texto(s).lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:64].rstrip("-") or "seccion"


def esc(s: str) -> str:
    return html.escape(s, quote=True)


FOTOS_EXTRA = {
    "acosta": "/static/images/masoterapia/paola-acosta.webp",
    "paz": "/static/images/nutriologia-diabetologia/dr-raul-paz-retrato.webp",
}


def foto(key: str) -> str | None:
    if key in FOTOS_EXTRA and (ROOT / FOTOS_EXTRA[key].lstrip("/")).exists():
        return FOTOS_EXTRA[key]
    for nombre in (f"{key}-360.webp", f"{key}.webp"):
        if (EQUIPO / nombre).exists():
            return f"/static/images/equipo/{nombre}"
    return None


def iniciales(nombre: str) -> str:
    partes = [p for p in re.sub(r"^(Dr|Dra|Ps|TM)\.?\s+", "", nombre).split() if p[0].isupper()]
    return (partes[0][0] + (partes[-1][0] if len(partes) > 1 else "")).upper()


def avatar(key: str, size: int = 44, lazy: bool = True) -> str:
    nombre, _ = PERSONAS[key]
    src = foto(key)
    if src:
        return (f'<img class="bp-av" src="{src}" alt="{esc(nombre)}" width="{size}" height="{size}"'
                f'{" loading=\"lazy\"" if lazy else ""} decoding="async" />')
    return f'<span class="bp-av bp-av-ini" aria-hidden="true">{iniciales(nombre)}</span>'


def wa_href(texto_msg: str, slug: str) -> str:
    t = quote(f"Hola, quiero agendar {texto_msg} (web: blog)", safe="")
    return f"https://wa.me/{WA}?text={t}&amp;utm_source=blog&amp;utm_medium=organic&amp;utm_campaign={slug}"


def nobreak(fragmento: str) -> str:
    """Espacios no separables en precios, unidades y títulos de nombres, SOLO en nodos de texto."""
    partes = re.split(r"(<script.*?</script>|<svg.*?</svg>|<[^>]+>)", fragmento, flags=re.S)
    precio = re.compile(r"(?<![\w$])((?:[Dd]esde\s)?\+?\$\s?\d{1,3}(?:\.\d{3})+(?:\s?[–-]\s?\$?\s?\d{1,3}(?:\.\d{3})+)?)")
    unidad = re.compile(r"(\d)\s(años|año|meses|mes|minutos|min|km|horas|hrs|h|días|día|semanas|sesiones|kg|g|mL|ml|cm|mmHg|mg/dL|%|veces)\b")
    titulo = re.compile(r"\b(Dr|Dra|Ps|TM)\.?\s(?=[A-ZÁÉÍÓÚ])")
    out = []
    dentro_nw = 0
    for p in partes:
        if not p:
            continue
        if p.startswith("<"):
            if re.match(r'<span class="nw"', p):
                dentro_nw += 1
            out.append(p)
            continue
        p = unidad.sub(lambda m: m.group(1) + NBSP + m.group(2), p)
        p = titulo.sub(lambda m: m.group(0)[:-1] + NBSP, p)
        p = re.sub(r"SAMU\s131", "SAMU" + NBSP + "131", p)
        p = precio.sub(lambda m: '<span class="nw">' + m.group(1).replace(" ", NBSP) + "</span>", p)
        out.append(p)
    return "".join(out)


def fecha_es(iso: str) -> str:
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    m = re.match(r"(\d{4})-(\d{2})", iso or "")
    if not m:
        return ""
    return f"{meses[int(m.group(2)) - 1]} {m.group(1)}"


SVG = {
    "wa": '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M17.5 14.4c-.3-.1-1.7-.8-2-.9-.3-.1-.5-.1-.7.1-.2.3-.7.9-.9 1.1-.2.2-.3.2-.6.1-.3-.1-1.2-.4-2.3-1.4-.8-.8-1.4-1.7-1.5-2-.2-.3 0-.4.1-.6.1-.1.3-.3.4-.5.1-.2.2-.3.3-.5.1-.2 0-.4 0-.5 0-.1-.7-1.6-.9-2.2-.2-.6-.5-.5-.7-.5h-.6c-.2 0-.5.1-.8.4-.3.3-1 1-1 2.5s1.1 2.9 1.2 3.1c.1.2 2.1 3.2 5.1 4.5.7.3 1.3.5 1.7.6.7.2 1.4.2 1.9.1.6-.1 1.7-.7 2-1.4.2-.7.2-1.3.2-1.4-.1-.1-.3-.2-.6-.3zM12 2a10 10 0 0 0-8.6 15.1L2 22l5-1.3A10 10 0 1 0 12 2z"/></svg>',
    "arrow": '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" d="M5 12h14M13 6l6 6-6 6"/></svg>',
    "clock": '<svg viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.25"/><path d="M12 7v5l3 2"/></g></svg>',
    "cal": '<svg viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M8 3v4M16 3v4M3.5 10h17"/></g></svg>',
    "check": '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.5 9.5 17.5 19.5 7"/></svg>',
    "down": '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M6 9l6 6 6-6"/></svg>',
    "pin": '<svg viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0z"/><circle cx="12" cy="10" r="3"/></g></svg>',
}

# ─────────────────────────────────────────────────────────────────────────────
# Extracción
# ─────────────────────────────────────────────────────────────────────────────

def aplicar_correcciones(slug: str, src: str, log: list) -> str:
    for archivo, viejo, nuevo, motivo in CORRECCIONES:
        if archivo != slug:
            continue
        n = src.count(viejo)
        if n == 0:
            raise SystemExit(f"[{slug}] corrección no encontrada (¿cambió la fuente?): {viejo[:90]!r}")
        src = src.replace(viejo, nuevo)
        log.append((slug, motivo, n, texto(viejo)[:160], texto(nuevo)[:160]))
    return src


def limpiar_head(head: str, slug: str) -> str:
    h = head
    h = re.sub(r"<style\b.*?</style>\s*", "", h, flags=re.S)
    h = re.sub(r'<link[^>]+fonts\.(?:googleapis|gstatic)\.com[^>]*>\s*', "", h)
    h = re.sub(r'<link[^>]+rel="(?:icon|apple-touch-icon|shortcut icon)"[^>]*>\s*', "", h)
    h = re.sub(r'<meta\s+charset[^>]*>\s*', "", h, flags=re.I)
    h = re.sub(r'<meta\s+name="viewport"[^>]*>\s*', "", h)
    h = re.sub(r'<meta\s+name="theme-color"[^>]*>\s*', "", h)
    og = f"{BASE}/static/images/blog/og/{slug}.jpg"
    if re.search(r'<meta property="og:image" content="[^"]*"', h):
        h = re.sub(r'<meta property="og:image" content="[^"]*"', f'<meta property="og:image" content="{og}"', h, count=1)
    else:
        h = h.replace("</title>", f'</title>\n<meta property="og:image" content="{og}" />', 1)
    if "og:image:width" not in h:
        h = re.sub(r'(<meta property="og:image" content="[^"]*"\s*/?>)', r'\1\n<meta property="og:image:width" content="1200" />\n<meta property="og:image:height" content="630" />', h, count=1)
    if re.search(r'<meta name="twitter:image" content="[^"]*"', h):
        h = re.sub(r'<meta name="twitter:image" content="[^"]*"', f'<meta name="twitter:image" content="{og}"', h, count=1)
    else:
        h = re.sub(r'(<meta name="twitter:card"[^>]*>)', r'\1\n<meta name="twitter:image" content="' + og + '" />', h, count=1)
    h = re.sub(r"\n{3,}", "\n\n", h).strip("\n")
    return h


def extraer(src: str, slug: str) -> dict:
    head = re.search(r"<head>(.*?)</head>", src, re.S).group(1)
    body = re.search(r"<body[^>]*>(.*)</body>", src, re.S).group(1)
    d: dict = {}
    m = re.search(r"<!-- Google Tag Manager \(noscript\) -->.*?<!-- End Google Tag Manager \(noscript\) -->", body, re.S) \
        or re.search(r"<noscript>\s*<iframe[^>]*googletagmanager.*?</noscript>", body, re.S)
    d["gtm_ns"] = m.group(0) if m else ""
    d["scripts"] = [s for s in re.findall(r"<script(?![^>]*ld\+json)[^>]*>.*?</script>", body, re.S) if "dataLayer" in s]
    m = re.search(r'class="eyebrow"[^>]*>(.*?)</(?:div|p|span)>', body, re.S)
    d["kicker"] = texto(m.group(1)) if m else ""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    d["h1"] = m.group(1).strip()
    m = re.search(r'<p class="blog-lead[^"]*"[^>]*>(.*?)</p>', body, re.S)
    d["lead"] = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    lead_full = m.group(0) if m else None
    m = re.search(r"(\d+)\s*min(?:utos)?\s+de\s+lectura", body)
    d["mins"] = int(m.group(1)) if m else None
    art = re.search(r"<article[^>]*>(.*?)</article>", body, re.S).group(1)
    if lead_full and lead_full in art:
        art = art.replace(lead_full, "", 1)
    d["articulo"] = art
    m = re.search(r'<span class="current">(.*?)</span>', body, re.S)
    d["current"] = texto(m.group(1)) if m else ""
    # enlaces del lateral y de la venta cruzada
    aside = " ".join(re.findall(r"<aside[^>]*>.*?</aside>", body, re.S))
    cross = " ".join(re.findall(r"<!-- CROSS-SELL -->.*?</section>", body, re.S))
    locs, rel = [], []
    for href, inner in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', aside + " " + cross, re.S):
        if "wa.me" in href or href.startswith("tel:"):
            continue
        mloc = re.match(rf"/blog/{re.escape(slug)}-([a-z-]+)$", href)
        if mloc and mloc.group(1) in COMUNAS:
            tmin = re.search(r"·\s*([^<]+)</small>", inner)
            locs.append((href, texto(re.sub(r"<small.*?</small>", "", inner, flags=re.S)), texto(tmin.group(1)) if tmin else ""))
            continue
        if "/sitio/" in href or href.rstrip("/") in (f"/blog/{slug}", f"{BASE}/blog/{slug}") or href in ("/", "/blog", BASE + "/"):
            continue
        strong = re.search(r"<strong[^>]*>(.*?)</strong>", inner, re.S)
        span = re.search(r"<span[^>]*>(.*?)</span>", inner, re.S)
        titulo = texto(strong.group(1)) if strong else texto(inner)
        desc = texto(span.group(1)) if (strong and span) else ""
        for pat, rep in TILDES:
            titulo = re.sub(pat, rep, titulo)
        rel.append((href, titulo, desc))
    vistos, rel2 = set(), []
    for h, t, dsc in rel:
        k = h.replace(BASE, "")
        if k in vistos or not t:
            continue
        vistos.add(k)
        rel2.append((h, t, dsc))
    d["rel"] = rel2
    d["locs"] = locs
    # meta
    d["modified"] = (re.search(r'article:modified_time" content="([^"]+)"', head) or re.search(r'"dateModified"\s*:\s*"([^"]+)"', head) or [None, ""])[1]
    sec = re.search(r'article:section" content="([^"]+)"', head)
    d["section"] = sec.group(1) if sec else ""
    bc = re.findall(r'"position"\s*:\s*3\s*,\s*"name"\s*:\s*"([^"]+)"', head)
    d["bc3"] = bc[0] if bc else ""
    d["head"] = head
    return d


def transformar_articulo(art: str) -> tuple[str, list[tuple[str, str]]]:
    a = re.sub(r'\sstyle="[^"]*"', "", art)
    a = re.sub(r"\s*<!--.*?-->\s*", "\n", a, flags=re.S)
    a = PLACEHOLDER_RE.sub(lambda m: f"<em>{m.group(1)}</em>", a)
    # FAQ de bloques → <details>
    a = re.sub(r'<div class="faq-item">\s*<div class="faq-q">(.*?)</div>\s*<div class="faq-a">(.*?)</div>\s*</div>',
               lambda m: f'<details><summary>{m.group(1).strip()}{SVG["down"]}</summary><div class="answer">{m.group(2).strip()}</div></details>',
               a, flags=re.S)
    # tablas: en móvil se apilan como fichas (cada celda con su rótulo de columna)
    def _tabla(m):
        t = m.group(1)
        ths = [texto(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", t, re.S)]
        if not ths:
            return f'<div class="bp-table">{t}</div>'

        def _tr(mr):
            i = [0]

            def _td(mt):
                lab = ths[i[0]] if i[0] < len(ths) else ""
                i[0] += 1
                return f'<td{mt.group(1)} data-label="{esc(lab)}">'
            return re.sub(r"<td([^>]*)>", _td, mr.group(0))
        t = re.sub(r"<tr[^>]*>(?:(?!</tr>).)*?<td.*?</tr>", _tr, t, flags=re.S)
        return f'<div class="bp-table is-stack">{t}</div>'
    a = re.sub(r"(<table\b.*?</table>)", _tabla, a, flags=re.S)
    # primer recuadro informativo → cita destacada
    a = re.sub(r'<div class="callout callout-info">', '<div class="callout callout-info is-quote">', a, count=1)
    # ids en H2 + índice
    toc, usados = [], set()

    def _h2(m):
        attrs, inner = m.group(1), m.group(2)
        mid = re.search(r'id="([^"]+)"', attrs)
        hid = mid.group(1) if mid else slugify(inner)
        base, i = hid, 2
        while hid in usados:
            hid, i = f"{base}-{i}", i + 1
        usados.add(hid)
        toc.append((hid, re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<svg.*?</svg>", "", inner, flags=re.S)))).strip()))
        attrs = re.sub(r'\sid="[^"]+"', "", attrs)
        return f'<h2 id="{hid}"{attrs}>{inner}</h2>'

    a = re.sub(r"<h2([^>]*)>(.*?)</h2>", _h2, a, flags=re.S)
    for pat, rep in TILDES:
        a = re.sub(pat, rep, a)
    a = nobreak(a)
    a = re.sub(r"\n\s*\n+", "\n", a).strip()
    return a, toc


# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────

def nav(slug: str, f: dict) -> str:
    return f"""<div class="emergency-strip">
  <strong>Emergencias:</strong> llama al SAMU{NBSP}<a href="tel:131">131</a><span class="em-long"> · Este centro no es servicio de urgencias</span>
</div>
<header class="nav-wrap">
  <div class="container">
    <nav class="nav" aria-label="Principal">
      <a href="{BASE}/" class="nav-brand" aria-label="Centro Médico Carampangue, inicio">
        <img src="/static/images/ortodoncia/logo-cmc.png" alt="Centro Médico Carampangue" width="168" height="40" />
      </a>
      <a href="/blog" class="nav-sec">Guía de salud</a>
      <div class="nav-menu">
        <a href="{BASE}/#especialidades">Especialidades</a>
        <a href="{f['landing']}">{esc(f['esp'])}</a>
        <a href="/blog">Todos los artículos</a>
      </div>
      <div class="nav-cta">
        <a href="{wa_href(f['wa'], slug)}" class="btn btn-main" target="_blank" rel="noopener" data-wa-btn="menu" aria-label="Agendar {esc(f['esp'].lower())} por WhatsApp">
          <span class="wa-dot" aria-hidden="true">{SVG['wa']}</span>Agendar
        </a>
      </div>
    </nav>
  </div>
  <span class="bp-progress" aria-hidden="true"></span>
</header>"""


def foot(slug: str, f: dict) -> str:
    return f"""<footer class="bp-foot">
  <div class="container">
    <div class="bp-foot-grid">
      <div class="bp-foot-brand">
        <img src="/static/images/ortodoncia/logo-cmc.png" alt="Centro Médico Carampangue" width="168" height="40" loading="lazy" />
        <p>Especialidades médicas y dentales en el corazón de la Provincia de Arauco. Una sola sede: Monsalve{NBSP}102, esquina República, frente a la antigua estación de trenes.</p>
      </div>
      <div>
        <h4>Contacto</h4>
        <ul>
          <li><a href="https://wa.me/{WA}?text={quote('Hola, quiero agendar una hora (web: blog)', safe='')}&amp;utm_source=blog&amp;utm_medium=organic&amp;utm_campaign={slug}" target="_blank" rel="noopener" data-wa-btn="footer">WhatsApp +56{NBSP}9{NBSP}6661{NBSP}0737</a></li>
          <li><a href="tel:+56442965226">Teléfono (44){NBSP}296{NBSP}5226</a></li>
          <li><a href="https://www.google.com/maps/search/?api=1&amp;query=-37.2548769,-73.2355041" target="_blank" rel="noopener">Monsalve{NBSP}102, Carampangue</a></li>
        </ul>
      </div>
      <div>
        <h4>Horario del centro</h4>
        <ul>
          <li>Lunes a viernes, 08:00–21:00</li>
          <li>Sábado, 09:00–14:00</li>
          <li><a href="{f['landing']}">{esc(f['esp'])} en el CMC</a></li>
        </ul>
      </div>
      <div>
        <h4>Información</h4>
        <ul>
          <li><a href="{BASE}/">Inicio</a></li>
          <li><a href="/blog">Guía de salud</a></li>
          <li><a href="/privacidad">Privacidad</a></li>
        </ul>
      </div>
    </div>
    <div class="bp-foot-base">
      <div>© 2026 Centro Médico Carampangue. Este contenido es informativo y no reemplaza una consulta.</div>
      <div>Provincia de Arauco · Región del Biobío</div>
    </div>
  </div>
</footer>"""


def ticket(f: dict, extra_cls: str = "") -> str:
    alt = ""
    if f.get("alt"):
        alt = f'<span class="alt">{f["alt"][0].replace(" ", NBSP)}<small>{esc(f["alt"][1])}</small></span>'
    facts = "".join(f"<li>{SVG['check']}<span>{nobreak(esc(x))}</span></li>" for x in f["facts"])
    return (f'<div class="ticket {extra_cls}"><div class="k">{esc(f["k"])}</div>'
            f'<div class="row"><div class="v">{f["v"].replace(" ", NBSP)}</div>{alt}</div>'
            f'<ul class="bp-facts">{facts}</ul></div>')


JS = """<script>
(function(){var d=document,w=window,nav=d.querySelector('.nav-wrap'),bar=d.querySelector('.bp-progress'),art=d.querySelector('.bp-prose'),
fl=d.querySelector('.wa-float'),hero=d.querySelector('.bp-hero'),ctas=[].slice.call(d.querySelectorAll('.bp-ficha,.cta-inline')),
links=[].slice.call(d.querySelectorAll('.bp-toc a')),heads=links.map(function(a){return d.getElementById(a.getAttribute('href').slice(1))}),tk=0;
function upd(){tk=0;var y=w.pageYOffset||0,vh=w.innerHeight;if(nav)nav.classList.toggle('is-scrolled',y>8);
if(art&&bar){var r=art.getBoundingClientRect(),p=(vh*.35-r.top)/Math.max(1,r.height-vh*.4);bar.style.transform='scaleX('+Math.min(1,Math.max(0,p))+')'}
if(links.length){var c=-1;for(var i=0;i<heads.length;i++){if(heads[i]&&heads[i].getBoundingClientRect().top<vh*.32)c=i}
for(var j=0;j<links.length;j++)links[j].classList.toggle('is-on',j===c)}
if(fl){var past=hero?hero.getBoundingClientRect().bottom<0:y>400,busy=false;
for(var k=0;k<ctas.length;k++){var b=ctas[k].getBoundingClientRect();if(b.top<vh&&b.bottom>0){busy=true;break}}
var hid=!past||busy;fl.classList.toggle('is-hidden',hid);d.body.classList.toggle('float-on',!hid)}}
function q(){if(!tk){tk=1;requestAnimationFrame(upd)}}
w.addEventListener('scroll',q,{passive:true});w.addEventListener('resize',q);upd()})();
</script>"""


def render(slug: str, src: str, log: list) -> str:
    fkey, tambien = ARTICULOS[slug]
    f = FICHAS[fkey]
    src = aplicar_correcciones(slug, src, log)
    d = extraer(src, slug)
    head = limpiar_head(d["head"], slug)
    art, toc = transformar_articulo(d["articulo"])
    mins = d["mins"] or max(3, round(len(texto(art).split()) / 200))
    upd = fecha_es(d["modified"])
    kicker = d["kicker"]
    for pat, rep in TILDES:
        kicker = re.sub(pat, rep, kicker)
    current = d["current"] or d["bc3"] or d["section"] or f["esp"]
    lead = nobreak(d["lead"])
    h1 = d["h1"]
    gente = f["gente"]
    # portada: quién atiende
    avs = "".join(avatar(k, 44, lazy=False) for k in gente[:3])
    nombres = " · ".join(PERSONAS[k][0] for k in gente[:3])
    hero_go = (f'<a class="bp-go" href="{f["landing"]}"><span><small>{esc(f["esp"])} en el CMC</small>'
               f'<b>{nobreak(esc(f["gancho"][:1].upper() + f["gancho"][1:]))}</b></span>{SVG["arrow"]}</a>')
    k0 = gente[0]
    src0 = foto(k0)
    if src0:
        port_img = (f'<img src="{src0}" alt="{esc(PERSONAS[k0][0])}, {esc(PERSONAS[k0][1].lower())}" width="260" height="318" '
                    f'decoding="async" />')
    else:
        port_img = ('<img src="/static/images/ecografia/box-640.webp" alt="Box de atención del Centro Médico Carampangue" '
                    'width="260" height="318" decoding="async" />')
    cap = "".join(f'<b>{esc(PERSONAS[k][0])}</b><span>{esc(PERSONAS[k][1])}</span>' for k in gente[:3])
    portrait = (f'<figure class="bp-port">{port_img}<figcaption><span class="bp-by-k">Te atiende en el CMC</span>{cap}</figcaption></figure>')
    toc_items = "".join(f'<li><a href="#{h}">{esc(t)}</a></li>' for h, t in toc)
    toc_m = (f'<details class="bp-toc-m"><summary><span>En este artículo<small>{len(toc)} secciones</small></span>{SVG["down"]}</summary>'
             f'<ol>{toc_items}</ol></details>') if toc else ""
    toc_d = f'<nav class="bp-toc" aria-label="En este artículo"><span class="bp-toc-h">En este artículo</span><ol>{toc_items}</ol></nav>' if toc else ""
    rail_who = "".join(
        f'<div class="who">{avatar(k, 40)}<div><b>{esc(PERSONAS[k][0])}</b><span>{esc(PERSONAS[k][1])}</span></div></div>'
        for k in gente[:2])
    rail = f"""<aside class="bp-rail" aria-label="Índice y ficha">
        {toc_d}
        <div class="rail-ticket">
          {rail_who}
          {ticket(f)}
          <div class="go">
            <a class="lnk wa" href="{wa_href(f['wa'], slug)}" target="_blank" rel="noopener" data-wa-btn="lateral"><span>Agendar por WhatsApp</span>{SVG['wa']}</a>
            <a class="lnk" href="{f['landing']}"><span>Ver {esc(f['esp'].lower())}</span>{SVG['arrow']}</a>
          </div>
        </div>
      </aside>"""
    team = "".join(f'<li>{avatar(k, 48)}<div><b>{esc(PERSONAS[k][0])}</b><span>{esc(PERSONAS[k][1])}</span></div></li>' for k in gente)
    also = ""
    if tambien:
        enl = " · ".join(f'<a href="{FICHAS[t]["landing"]}">{esc(FICHAS[t]["esp"])}</a>' for t in tambien)
        also = f'<p class="bp-also">También en el CMC: {enl}</p>'
    ficha = f"""<section class="bp-close" aria-labelledby="ficha-t">
  <div class="container">
    <div class="bp-ficha">
      <div>
        <span class="kicker">{esc(f['esp'])} en el CMC</span>
        <h2 id="ficha-t">{f['h2']}</h2>
        <p class="bp-ficha-p">{nobreak(esc(f['p']))}</p>
        <ul class="bp-team">{team}</ul>
        {also}
      </div>
      <div>
        {ticket(f)}
        <div class="bp-ficha-cta">
          <a class="btn btn-light" href="{wa_href(f['wa'], slug)}" target="_blank" rel="noopener" data-wa-btn="cierre"><span class="wa-dot" aria-hidden="true">{SVG['wa']}</span>Agendar por WhatsApp</a>
          <a class="btn btn-ghost" href="{f['landing']}">Ver {esc(f['esp'].lower())}{SVG['arrow']}</a>
        </div>
        <p class="bp-ficha-tel">¿Prefieres llamar? <a href="tel:+56442965226">(44){NBSP}296{NBSP}5226</a> · Monsalve{NBSP}102, Carampangue</p>
      </div>
    </div>
  </div>
</section>"""
    reads = "".join(
        f'<li><a href="{esc(h)}"><div><b>{nobreak(esc(t))}</b>{f"<span>{nobreak(esc(dsc))}</span>" if dsc else ""}</div>{SVG["arrow"]}</a></li>'
        for h, t, dsc in d["rel"][:8])
    locs = ""
    if d["locs"]:
        li = "".join(f'<li><a href="{esc(h)}">{esc(t)}{f" <small>· {esc(tm)}</small>" if tm and tm != "0 min" else ""}</a></li>' for h, t, tm in d["locs"])
        locs = f'<div class="bp-locs"><h3>Atención por localidad</h3><ul>{li}</ul></div>'
    more = ""
    if reads or locs:
        more = f"""<section class="bp-more" aria-labelledby="mas-t">
  <div class="container">
    <div class="bp-more-in">
      <h2 id="mas-t">Sigue leyendo</h2>
      <ul class="bp-reads">{reads}</ul>
      {locs}
    </div>
  </div>
</section>"""
    meta_items = f'<span>{SVG["clock"]}{mins}{NBSP}min de lectura</span>'
    if upd:
        meta_items += f'<span>{SVG["cal"]}Actualizado {upd}</span>'
    out = f"""<!DOCTYPE html>
<html lang="es-CL">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
{head}
<meta name="theme-color" content="#F7F3EC" />
<link rel="icon" type="image/png" sizes="192x192" href="https://centromedicocarampangue.cl/static/sitio/cropped-logo-carampangue.png" />
<link rel="apple-touch-icon" href="https://centromedicocarampangue.cl/static/sitio/cropped-logo-carampangue.png" />
<link rel="preload" href="/static/fonts/montserrat-var-latin.woff2" as="font" type="font/woff2" crossorigin />
<link rel="stylesheet" href="{CSS_HREF}" />
</head>
<body class="bp">
{d['gtm_ns']}
<div class="bp-page">
{nav(slug, f)}
<main id="contenido">
<header class="bp-hero">
  <div class="container bp-hero-in">
    <nav class="breadcrumb" aria-label="Ruta"><a href="{BASE}/">Inicio</a><span class="sep">/</span><a href="/blog">Guía de salud</a><span class="sep">/</span><span class="current">{esc(current)}</span></nav>
    {portrait}
    <span class="kicker">{esc(kicker)}</span>
    <h1 class="blog-h1">{h1}</h1>
    <p class="blog-lead">{lead}</p>
    <div class="bp-by">
      <div class="bp-by-who"><span class="bp-avs">{avs}</span><span><span class="bp-by-k">Te atiende en el CMC</span><span class="bp-by-n">{esc(nombres)}</span></span></div>
      <div class="bp-meta">{meta_items}</div>
    </div>
    {hero_go}
  </div>
</header>
<section class="blog-body">
  <div class="container">
    <div class="bp-grid">
      {rail}
      <div class="bp-main">
        {toc_m}
        <article class="blog-content bp-prose">
{art}
        </article>
      </div>
    </div>
  </div>
</section>
{ficha}
{more}
</main>
</div>
{foot(slug, f)}
<a href="{wa_href(f['wa'], slug)}" class="wa-float is-hidden" target="_blank" rel="noopener" data-wa-btn="flotante" aria-label="Agendar {esc(f['esp'].lower())} por WhatsApp">
  <span class="wa-dot" aria-hidden="true">{SVG['wa']}</span><span>Agendar</span>
</a>
{JS}
{chr(10).join(d['scripts'])}
</body>
</html>
"""
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Índice del blog
# ─────────────────────────────────────────────────────────────────────────────

def render_index(datos: dict) -> str:
    src = fuente_index()
    head = re.search(r"<head>(.*?)</head>", src, re.S).group(1)
    head = re.sub(r"<style\b.*?</style>\s*", "", head, flags=re.S)
    head = re.sub(r'<link[^>]+fonts\.(?:googleapis|gstatic)\.com[^>]*>\s*', "", head)
    head = re.sub(r'<link[^>]+rel="(?:icon|apple-touch-icon)"[^>]*>\s*', "", head)
    head = re.sub(r'<meta\s+charset[^>]*>\s*', "", head, flags=re.I)
    head = re.sub(r'<meta\s+name="viewport"[^>]*>\s*', "", head)
    og = f"{BASE}/static/images/blog/og/blog.jpg"
    head = re.sub(r'<meta property="og:image" content="[^"]*"', f'<meta property="og:image" content="{og}"', head, count=1)
    if re.search(r'<meta name="twitter:image" content="[^"]*"', head):
        head = re.sub(r'<meta name="twitter:image" content="[^"]*"', f'<meta name="twitter:image" content="{og}"', head, count=1)
    else:
        head = re.sub(r'(<meta name="twitter:card"[^>]*>)', r'\1\n<meta name="twitter:image" content="' + og + '">', head, count=1)
    head = re.sub(r"\n{3,}", "\n\n", head).strip("\n")
    body = re.search(r"<body[^>]*>(.*)</body>", src, re.S).group(1)
    ns = re.search(r"<noscript>.*?</noscript>", body, re.S)
    scripts = [s for s in re.findall(r"<script(?![^>]*ld\+json)[^>]*>.*?</script>", body, re.S) if "dataLayer" in s]
    secciones = []
    n = 0
    for titulo, slugs in GRUPOS:
        cards = []
        for s in slugs:
            if s not in datos:
                continue
            dd = datos[s]
            f = FICHAS[ARTICULOS[s][0]]
            k = f["gente"][0]
            n += 1
            cards.append(f"""<li><a class="ix-card" href="/blog/{s}">
          <span class="ix-top"><span class="kicker">{esc(dd['kicker'] or f['esp'])}</span><span class="ix-min">{dd['mins']}{NBSP}min</span></span>
          <b class="ix-h">{texto(dd['h1'])}</b>
          <span class="ix-who">{avatar(k, 32)}<span>{esc(PERSONAS[k][0])}</span><span class="ix-go">{SVG['arrow']}</span></span>
        </a></li>""")
        secciones.append(f"""<section class="ix-sec" aria-labelledby="g-{slugify(titulo)}">
      <h2 id="g-{slugify(titulo)}">{esc(titulo)}</h2>
      <ul class="ix-grid">{''.join(cards)}</ul>
    </section>""")
    f = FICHAS["medicina-general"]
    css_extra = """<style>
.ix-hero{padding:34px 0 40px}.ix-hero h1{font:800 clamp(2.2rem,1.2rem + 4vw,4.2rem)/1.02 var(--display);letter-spacing:-.034em;color:var(--tinta);margin:14px 0 0;max-width:16ch;text-wrap:balance}
.ix-hero h1 em{font-style:normal;color:var(--azul)}.ix-hero p{margin-top:16px;font-size:clamp(1.06rem,.98rem + .4vw,1.25rem);line-height:1.55;max-width:40em}
.ix-facts{display:flex;flex-wrap:wrap;gap:8px;margin-top:22px;padding:0;list-style:none}.ix-facts li{display:inline-flex;align-items:center;min-height:36px;padding:7px 14px;line-height:1.35;border-radius:18px;background:var(--papel);border:1px solid var(--linea);font-size:13.5px}
.ix-sec{padding:20px 0 34px}.ix-sec h2{font:700 clamp(1.3rem,1.1rem + .8vw,1.7rem)/1.2 var(--display);letter-spacing:-.022em;color:var(--tinta);margin:0 0 16px;padding-top:22px;border-top:1px solid var(--linea)}
.ix-grid{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:minmax(0,1fr);gap:12px}
.ix-card{display:flex;flex-direction:column;gap:12px;height:100%;padding:20px;border-radius:var(--r-lg);background:var(--papel);border:1px solid var(--linea-2);box-shadow:var(--shadow-sm);transition:transform .25s var(--ease),box-shadow .25s var(--ease)}
.ix-card:hover{transform:translateY(-2px);box-shadow:var(--shadow-md)}.ix-top{display:flex;justify-content:space-between;gap:10px;align-items:center}.ix-top .kicker{font-size:10.5px}.ix-top .kicker::before{width:14px}
.ix-min{font-size:12.5px;color:var(--gris);white-space:nowrap}.ix-h{font:700 1.12rem/1.3 var(--display);letter-spacing:-.016em;color:var(--tinta);text-wrap:balance}
.ix-who{margin-top:auto;display:flex;align-items:center;gap:10px;font-size:13.5px;color:var(--gris)}.ix-who .bp-av{width:32px;height:32px;border-width:1.5px}.ix-go{margin-left:auto;color:var(--aqua-ink)}.ix-go svg{width:18px;height:18px;transition:transform .22s var(--ease)}.ix-card:hover .ix-go svg{transform:translateX(3px)}
@media(min-width:600px){.ix-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(min-width:1000px){.ix-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.ix-hero{padding:52px 0 56px}}
</style>"""
    return f"""<!DOCTYPE html>
<html lang="es-CL">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
{head}
<meta name="theme-color" content="#F7F3EC" />
<link rel="icon" type="image/png" sizes="192x192" href="https://centromedicocarampangue.cl/static/sitio/cropped-logo-carampangue.png" />
<link rel="apple-touch-icon" href="https://centromedicocarampangue.cl/static/sitio/cropped-logo-carampangue.png" />
<link rel="preload" href="/static/fonts/montserrat-var-latin.woff2" as="font" type="font/woff2" crossorigin />
<link rel="stylesheet" href="{CSS_HREF}" />
{css_extra}
<script defer src="/static/cmc-wa.js?v=1"></script>
</head>
<body class="bp">
{ns.group(0) if ns else ''}
<div class="bp-page">
{nav('blog', dict(f, esp='Medicina general'))}
<main id="contenido">
  <header class="ix-hero">
    <div class="container">
      <span class="kicker">Guía de salud del CMC</span>
      <h1>Lo que conviene saber <em>antes de tu hora</em>.</h1>
      <p>{n} guías claras sobre las especialidades del centro: cuándo consultar, qué esperar, cuánto cuesta y quién te atiende en Monsalve{NBSP}102, Carampangue.</p>
      <ul class="ix-facts"><li>Lunes a viernes 08:00–21:00</li><li>Sábado 09:00–14:00</li><li>Bono Fonasa en medicina general, kinesiología, nutrición y psicología</li></ul>
    </div>
  </header>
  <div class="container">
    {''.join(secciones)}
  </div>
</main>
</div>
{foot('blog', f)}
{JS}
{chr(10).join(scripts)}
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────

MARCADORES = ['<h1 class="blog-h1">', '<p class="blog-lead">', '<span class="current">', '<section class="blog-body">', "</head>", "</body>"]


def validar(slug: str, src: str, out: str) -> list[str]:
    errs = []
    for m in MARCADORES:
        if out.count(m) != 1:
            errs.append(f"marcador {m!r} aparece {out.count(m)} veces")
    for pat in (r"<title>.*?</title>", r'<link rel="canonical"[^>]*>', r'<meta name="description" content="[^"]*"\s*/?>', r'<meta property="og:title" content="[^"]*"\s*/?>'):
        a, b = re.search(pat, src, re.S), re.search(pat, out, re.S)
        if (a is None) != (b is None):
            errs.append(f"{pat} presente/ausente distinto")
    # mismos JSON-LD (contenido idéntico salvo correcciones de datos)
    if len(re.findall(r"application/ld\+json", src)) != len(re.findall(r"application/ld\+json", out)):
        errs.append("cambió el número de bloques JSON-LD")
    for blk in re.findall(r'<script type="application/ld\+json">(.*?)</script>', out, re.S):
        try:
            json.loads(blk)
        except Exception as e:  # noqa: BLE001
            errs.append(f"JSON-LD inválido: {e}")
    # mismo set de H2 / H3 (por texto)
    h2a = [texto(x) for x in re.findall(r"<h2[^>]*>(.*?)</h2>", re.search(r"<article.*?</article>", src, re.S).group(0), re.S)]
    for pat, rep in TILDES:
        h2a = [re.sub(pat, rep, h) for h in h2a]
    h2b = [texto(x) for x in re.findall(r"<h2[^>]*>(.*?)</h2>", re.search(r"<article.*?</article>", out, re.S).group(0), re.S)]
    if [h.replace(NBSP, " ") for h in h2a] != [h.replace(NBSP, " ") for h in h2b]:
        errs.append(f"H2 distintos: {set(h2a) ^ set(h2b)}")
    if "GTM-W428KN3R" not in out or out.count("googletagmanager.com/gtm.js") != 1:
        errs.append("GTM")
    if "cmc-wa.js" not in out:
        errs.append("cmc-wa.js")
    return errs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo", nargs="*")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--log", default="")
    args = ap.parse_args()
    slugs = sorted(ARTICULOS)
    disco = sorted(p.stem for p in BLOG_DIR.glob("*.html"))
    faltan = set(disco) - set(slugs)
    if faltan:
        raise SystemExit(f"artículos sin mapear en ARTICULOS: {faltan}")
    log: list = []
    datos = {}
    errores = 0
    for s in slugs:
        src = fuente(s)
        out = render(s, src, log)
        # textos que alimentan el índice (se leen ya corregidos)
        d = extraer(aplicar_correcciones(s, src, []), s)
        for pat, rep in TILDES:
            d["kicker"] = re.sub(pat, rep, d["kicker"])
        d["mins"] = d["mins"] or max(3, round(len(texto(d["articulo"]).split()) / 200))
        datos[s] = d
        errs = validar(s, aplicar_correcciones(s, src, []), out)
        if errs:
            errores += 1
            print(f"✗ {s}: {errs}")
        if args.solo and s not in args.solo:
            continue
        if not args.check:
            (BLOG_DIR / f"{s}.html").write_text(out, encoding="utf-8")
    if not args.check and not args.solo:
        INDEX.write_text(render_index(datos), encoding="utf-8")
    if args.log:
        Path(args.log).write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(slugs)} artículos · {len(log)} correcciones aplicadas · {errores} con errores de validación")
    if errores:
        sys.exit(1)


if __name__ == "__main__":
    main()
