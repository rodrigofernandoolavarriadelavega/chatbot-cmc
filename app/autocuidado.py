"""
Recordatorios de autocuidado y exámenes preventivos.
Genera tips personalizados según edad y sexo del paciente.
Basado en las guías preventivas del MINSAL Chile.
"""
from datetime import date, datetime
from typing import Optional
import random

# ── Tips genéricos (para todos) ─────────────────────────────────────────────
_TIPS_GENERICOS = [
    "💧 Recuerda beber al menos 6-8 vasos de agua al día.",
    "🥗 Mantén una alimentación equilibrada: frutas, verduras, proteínas y menos azúcar.",
    "🏃 La OMS recomienda al menos 150 minutos de actividad física moderada a la semana.",
    "😴 Dormir 7-8 horas al día es fundamental para tu salud.",
    "🧘 Dedica unos minutos al día a relajarte y cuidar tu salud mental.",
    "🦷 Visita al dentista cada 6 meses para un control preventivo.",
    "☀️ Usa protector solar diariamente, incluso en días nublados.",
    "🚭 Si fumas, pide ayuda para dejarlo — tu consultorio tiene programas gratuitos.",
]

# ── Exámenes preventivos por grupo (edad_min, edad_max, sexo, recordatorio) ──
# sexo: "F", "M", o None (ambos)
_EXAMENES_PREVENTIVOS = [
    # Mujeres
    (25, 64, "F",
     "🩺 *PAP (Papanicolau)*: se recomienda cada 3 años para mujeres de 25 a 64 años. "
     "Es gratuito en tu consultorio (CESFAM). En el CMC lo realiza la matrona Saraí Gómez."),
    (40, 54, "F",
     "🩺 *Mamografía*: a partir de los 40 años se recomienda realizar mamografía. "
     "Consulta con tu médico la frecuencia adecuada según tus factores de riesgo."),
    (50, 69, "F",
     "🩺 *Mamografía GES*: entre 50 y 69 años tienes garantía GES para mamografía cada 2 años. "
     "Pídela en tu consultorio, es gratuita."),
    # Hombres
    (50, 75, "M",
     "🩺 *Control de próstata*: a partir de los 50 años se recomienda hablar con tu doctor "
     "sobre el examen de próstata (PSA + tacto rectal). Detección temprana salva vidas."),
    # Ambos sexos
    (40, 99, None,
     "🩺 *Perfil lipídico y glicemia*: a partir de los 40 años se recomienda controlar "
     "colesterol y azúcar en sangre periódicamente."),
    (50, 99, None,
     "🩺 *Detección cáncer colorrectal*: a partir de los 50 años consulta con tu doctor "
     "sobre el test de sangre oculta en deposiciones. Examen simple y preventivo."),
    (65, 99, None,
     "🩺 *EMPAM (Examen de Medicina Preventiva del Adulto Mayor)*: a los 65+ años tienes "
     "derecho al EMPAM anual gratuito en tu consultorio. Evalúa tu estado de salud integral."),
    (65, 99, None,
     "🩺 *Vacuna Influenza y Neumococo*: como adulto mayor, recuerda vacunarte cada año contra "
     "la influenza (otoño) y consultar por la vacuna antineumocócica. Gratuitas en tu CESFAM."),
    # Adultos jóvenes
    (18, 39, None,
     "🩺 *Examen de Medicina Preventiva (EMP)*: tienes derecho a un examen preventivo gratuito "
     "en tu consultorio. Incluye presión, peso, glicemia y orientación en salud."),
]

# ── Tips por especialidad de la cita ────────────────────────────────────────
_TIPS_POR_ESPECIALIDAD = {
    "kinesiología": [
        "💪 Mantén los ejercicios que te indicó el kinesiólogo entre sesiones.",
        "🧊 Si sientes inflamación, aplica hielo 15 min cada 2-3 horas el primer día.",
    ],
    "traumatología": [
        "⚠️ Sigue las indicaciones de reposo o uso de férula al pie de la letra.",
        "🏋️ No retomes actividad física intensa sin autorización de tu traumatólogo.",
    ],
    "nutrición": [
        "📝 Lleva un registro de lo que comes los primeros días — te ayudará en el próximo control.",
        "🥤 Reduce las bebidas azucaradas — es el cambio con mayor impacto en tu salud.",
    ],
    "psicología adulto": [
        "🧠 La terapia es un proceso — sé paciente contigo mismo y mantén la constancia.",
        "📓 Si el psicólogo te dejó tareas o ejercicios, intenta hacerlos antes de la próxima sesión.",
    ],
    "odontología general": [
        "🦷 Cepíllate al menos 3 veces al día y usa hilo dental a diario.",
        "🍬 Reduce el consumo de dulces y bebidas ácidas para proteger tus dientes.",
    ],
    "cardiología": [
        "❤️ Controla tu presión arterial regularmente — puedes hacerlo en tu farmacia.",
        "🧂 Reduce la sal en las comidas — tu corazón te lo agradecerá.",
    ],
    "ginecología": [
        "🩺 Mantén tus controles ginecológicos al día — prevención es lo más importante.",
    ],
    "medicina general": [
        "📋 Si te recetaron medicamentos, tómalos en el horario indicado hasta completar el tratamiento.",
    ],
}


def _parse_fecha(fecha_str: str) -> Optional[date]:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(fecha_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _edad_anios(fecha_nac: date, hoy: date | None = None) -> int:
    hoy = hoy or date.today()
    edad = hoy.year - fecha_nac.year
    if (hoy.month, hoy.day) < (fecha_nac.month, fecha_nac.day):
        edad -= 1
    return max(edad, 0)


def get_tips_autocuidado(
    fecha_nacimiento: str | None = None,
    sexo: str | None = None,
    especialidad: str | None = None,
    nombre: str | None = None,
) -> str:
    """
    Genera un bloque de texto con tips de autocuidado personalizados.

    Args:
        fecha_nacimiento: YYYY-MM-DD o DD/MM/YYYY (puede ser None)
        sexo: "M" o "F" (puede ser None)
        especialidad: especialidad de la cita (para tips específicos)
        nombre: nombre del paciente

    Returns:
        Texto con 2-3 tips + exámenes preventivos si aplica.
    """
    nombre_corto = nombre.split()[0] if nombre else None

    # 1. Tip genérico aleatorio (siempre 1)
    tip_generico = random.choice(_TIPS_GENERICOS)

    # 2. Tip por especialidad (si existe)
    tip_esp = None
    if especialidad:
        tips_esp = _TIPS_POR_ESPECIALIDAD.get(especialidad.lower(), [])
        if tips_esp:
            tip_esp = random.choice(tips_esp)

    # 3. Exámenes preventivos según edad y sexo (máximo 2)
    examenes = []
    if fecha_nacimiento:
        fecha_nac = _parse_fecha(fecha_nacimiento)
        if fecha_nac:
            edad = _edad_anios(fecha_nac)
            sexo_upper = (sexo or "").upper()[:1]  # "M", "F", or ""
            for e_min, e_max, e_sexo, msg in _EXAMENES_PREVENTIVOS:
                if e_min <= edad <= e_max:
                    if e_sexo is None or e_sexo == sexo_upper:
                        examenes.append(msg)
            # Máximo 2 exámenes para no saturar
            if len(examenes) > 2:
                examenes = random.sample(examenes, 2)

    # Armar mensaje
    titulo = f"*{nombre_corto}*" if nombre_corto else "Paciente"
    lineas = [f"🌿 *Tips de autocuidado para {titulo}:*\n"]
    lineas.append(tip_generico)
    if tip_esp:
        lineas.append(tip_esp)
    if examenes:
        lineas.append("")
        lineas.append("📋 *Exámenes preventivos que podrían corresponderte:*\n")
        for ex in examenes:
            lineas.append(ex)

    return "\n".join(lineas)
