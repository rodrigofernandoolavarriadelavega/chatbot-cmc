"""
Hitos del desarrollo psicomotor según edad (0-9 años).
Genera un recordatorio de hitos esperados + señales de alerta para enviarlo
junto al recordatorio PNI al confirmar cita pediátrica.

Fuentes:
- Norma Técnica para la Supervisión de Salud Integral de Niños y Niñas
  de 0 a 9 años en la APS (MINSAL).
- Escala EEDP (0-24 meses) — Rodríguez, Arancibia, Undurraga (U. de Chile).
- Test TEPSI (2-5 años) — Haeussler y Marchant.
- Programa Chile Crece Contigo.
"""
from datetime import date, datetime
from typing import Optional

# (edad_min_meses, edad_max_meses, etiqueta, hitos[], alertas[])
# El rango aplica si edad_min <= edad_meses < edad_max.
_HITOS_CALENDARIO = [
    (0, 2, "1 mes", [
        "Levanta brevemente la cabeza boca abajo",
        "Sigue objetos con la mirada por algunos segundos",
        "Reacciona al sonido (se sobresalta o se calma)",
        "Mira la cara del cuidador",
    ], [
        "No reacciona a sonidos fuertes",
        "No fija la mirada",
        "Llanto débil o ausente",
    ]),
    (2, 3, "2 meses", [
        "Sonrisa social ante la cara de los padres",
        "Levanta la cabeza 45° en posición boca abajo",
        "Sigue objeto en arco de 90°",
        "Vocaliza sonidos guturales (\"ah\", \"eh\")",
    ], [
        "No sonríe a las personas",
        "No sigue con la mirada",
        "Cuerpo muy rígido o muy flácido",
    ]),
    (3, 5, "3-4 meses", [
        "Sostiene la cabeza erguida apoyado en antebrazos",
        "Une las manos en la línea media",
        "Toma objetos voluntariamente",
        "Ríe a carcajadas y reconoce a la madre",
    ], [
        "No sostiene la cabeza",
        "No toma objetos",
        "No ríe ni responde a la voz",
    ]),
    (5, 7, "6 meses", [
        "Se sienta con apoyo y se gira en ambos sentidos",
        "Cambia objetos de mano",
        "Balbucea (\"ba-ba\", \"da-da\")",
        "Reconoce extraños",
    ], [
        "No se voltea",
        "No balbucea",
        "No se interesa por su entorno",
    ]),
    (7, 10, "8 meses (evaluación EEDP)", [
        "Se sienta sin apoyo",
        "Inicia gateo o desplazamiento",
        "Pinza inferior (palma + dedos)",
        "Cadenas silábicas (\"ma-ma-ma\")",
        "Imita gestos (aplaude, dice chao)",
    ], [
        "No se sienta",
        "No balbucea cadenas",
        "No responde a su nombre",
    ]),
    (10, 14, "12 meses", [
        "Se para con apoyo, da pasos tomado de la mano",
        "Pinza fina (índice + pulgar)",
        "1-2 palabras con sentido (\"papá\", \"mamá\")",
        "Entiende órdenes simples (\"dame\", \"ven\")",
    ], [
        "No se para con apoyo",
        "No dice ninguna palabra",
        "No señala con el dedo",
    ]),
    (14, 17, "15 meses", [
        "Camina solo",
        "Junta 2 cubos, garabatea",
        "3-5 palabras",
        "Indica lo que quiere apuntando",
    ], [
        "No camina solo",
        "No dice palabras",
        "No señala objetos",
    ]),
    (17, 22, "18 meses (evaluación EEDP)", [
        "Camina hacia atrás, sube escalones con ayuda",
        "Hace torre de 2-3 cubos, garabatea",
        "10-20 palabras, indica partes del cuerpo",
        "Come solo con cuchara, imita tareas del hogar",
    ], [
        "No camina",
        "Menos de 5 palabras",
        "No imita acciones",
        "Pérdida de habilidades ya adquiridas",
    ]),
    (22, 30, "2 años", [
        "Corre, sube y baja escalones tomado del pasamanos",
        "Patea pelota, hace torre de 4-6 cubos",
        "Frases de 2 palabras (\"mamá agua\")",
        "50+ palabras, juego paralelo",
    ], [
        "No une 2 palabras",
        "No corre",
        "No imita ni apunta para mostrar",
    ]),
    (30, 42, "3 años (evaluación TEPSI)", [
        "Anda en triciclo, salta con dos pies juntos",
        "Copia un círculo, arma rompecabezas simples",
        "Frases completas, 250-500 palabras",
        "Control diurno de esfínter consolidado",
    ], [
        "No habla con frases",
        "No juega con otros niños",
        "Habla poco entendible para extraños",
    ]),
    (42, 54, "4 años (evaluación TEPSI)", [
        "Salta en un pie, se para en un pie 5 segundos",
        "Copia una cruz, dibuja figura humana con extremidades",
        "Cuenta historias simples, pregunta \"¿por qué?\"",
        "Se viste y desviste solo, juego cooperativo",
    ], [
        "No salta en un pie",
        "No dibuja figura humana",
        "No se entiende lo que dice",
    ]),
    (54, 72, "5 años", [
        "Salta a la cuerda, equilibrio en un pie 10 seg",
        "Copia cuadrado y triángulo, escribe su nombre",
        "Define palabras, narra hechos",
        "Juego de roles, respeta turnos",
    ], [
        "No se viste solo",
        "No entiende reglas simples de juego",
        "Habla poco claro",
    ]),
    (72, 108, "6-9 años (etapa escolar)", [
        "Lee y escribe progresivamente con fluidez",
        "Resuelve operaciones aritméticas simples",
        "Mantiene amistades estables",
        "Maneja noción de tiempo (días, meses)",
    ], [
        "Dificultad persistente para leer/escribir",
        "Problemas de atención que afectan rendimiento",
        "Aislamiento social o cambios bruscos de ánimo",
        "Enuresis nocturna persistente sobre los 6 años",
    ]),
]


def _edad_meses(fecha_nac: date, hoy: date | None = None) -> int:
    hoy = hoy or date.today()
    meses = (hoy.year - fecha_nac.year) * 12 + (hoy.month - fecha_nac.month)
    if hoy.day < fecha_nac.day:
        meses -= 1
    return max(meses, 0)


def _parse_fecha(fecha_str: str) -> Optional[date]:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(fecha_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def get_milestones_reminder(fecha_nacimiento: str, nombre: str = "") -> Optional[str]:
    """
    Genera un mensaje de hitos del desarrollo si el paciente es menor de 9 años.

    Args:
        fecha_nacimiento: fecha en formato YYYY-MM-DD o DD/MM/YYYY
        nombre: nombre del paciente (para personalizar)

    Returns:
        Mensaje con los hitos esperados a su edad o None si no aplica.
    """
    fecha_nac = _parse_fecha(fecha_nacimiento)
    if not fecha_nac:
        return None

    edad_m = _edad_meses(fecha_nac)

    # Solo menores de 9 años (108 meses)
    if edad_m >= 108:
        return None

    bucket = next(
        ((m_min, m_max, etq, hitos, alertas)
         for m_min, m_max, etq, hitos, alertas in _HITOS_CALENDARIO
         if m_min <= edad_m < m_max),
        None,
    )
    if not bucket:
        return None

    _, _, etiqueta, hitos, alertas = bucket
    nombre_corto = nombre.split()[0] if nombre else "tu hijo/a"

    lineas = [f"🧒 *Hitos del desarrollo — {nombre_corto} ({etiqueta})*\n"]
    lineas.append("Según la guía MINSAL (Chile Crece Contigo), a esta edad se espera:\n")
    for h in hitos:
        lineas.append(f"• {h}")

    if alertas:
        lineas.append("\n⚠️ *Señales de alerta — consultar si:*")
        for a in alertas:
            lineas.append(f"• {a}")

    lineas.append("\n_Cada niño tiene su propio ritmo. Si tienes dudas, "
                  "coméntalo con el doctor en la próxima cita._")

    return "\n".join(lineas)
