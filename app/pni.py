"""
Programa Nacional de Inmunización (PNI) Chile 2026.
Genera recordatorios de vacunas pendientes según la edad del paciente.
Fuente: MINSAL — Calendario de Vacunación vigente.
"""
from datetime import date, datetime
from typing import Optional

# Calendario PNI: (edad_meses_min, edad_meses_max, vacuna, descripción breve)
# edad_meses_max es exclusivo (el rango aplica si edad_meses_min <= edad < edad_meses_max)
_PNI_CALENDARIO = [
    # Recién nacido
    (0, 1, "BCG", "protege contra tuberculosis"),
    (0, 1, "Hepatitis B (1ª dosis)", "primera dosis contra hepatitis B"),
    # 2 meses
    (2, 4, "Hexavalente (1ª dosis)", "protege contra difteria, tétanos, tos convulsiva, polio, Hib y hepatitis B"),
    (2, 4, "Neumocócica conjugada (1ª dosis)", "protege contra neumonía y meningitis neumocócica"),
    # 4 meses
    (4, 6, "Hexavalente (2ª dosis)", "segunda dosis de hexavalente"),
    (4, 6, "Neumocócica conjugada (2ª dosis)", "segunda dosis contra neumonía"),
    # 6 meses
    (6, 12, "Hexavalente (3ª dosis)", "tercera dosis de hexavalente"),
    # 12 meses
    (12, 18, "Tres Vírica SRP (1ª dosis)", "protege contra sarampión, rubéola y paperas"),
    (12, 18, "Meningocócica conjugada (1ª dosis)", "protege contra meningitis meningocócica"),
    (12, 18, "Neumocócica conjugada (3ª dosis)", "tercera dosis contra neumonía"),
    (12, 18, "Hepatitis A (1ª dosis)", "primera dosis contra hepatitis A"),
    # 18 meses
    (18, 24, "Hexavalente (refuerzo)", "refuerzo de hexavalente"),
    (18, 24, "Hepatitis A (2ª dosis)", "segunda dosis contra hepatitis A"),
    (18, 24, "Varicela (1ª dosis)", "protege contra varicela"),
    (18, 24, "Fiebre Amarilla", "solo si viaja a zona endémica"),
    # 36 meses (3 años)
    (36, 48, "Varicela (2ª dosis)", "segunda dosis contra varicela"),
    # 4 años (48 meses)
    (48, 72, "DPT (refuerzo)", "refuerzo contra difteria, tétanos y tos convulsiva"),
    (48, 72, "Polio oral (refuerzo)", "refuerzo contra poliomielitis"),
    # 1° Básico (~6 años = 72 meses)
    (72, 108, "Tres Vírica SRP (2ª dosis)", "segunda dosis contra sarampión, rubéola y paperas"),
    (72, 108, "dTpa (refuerzo escolar)", "refuerzo contra difteria, tétanos y tos convulsiva acelular"),
    # 4° Básico (~9 años = 108 meses) — VPH para niños y niñas
    (108, 132, "VPH (1ª dosis)", "primera dosis contra virus papiloma humano — previene cáncer"),
    # 5° Básico (~10 años = 120 meses)
    (120, 156, "VPH (2ª dosis)", "segunda dosis contra virus papiloma humano"),
    # 8° Básico (~13 años = 156 meses)
    (156, 180, "dTpa (refuerzo 8° básico)", "refuerzo adolescente contra difteria, tétanos y tos convulsiva"),
]

# Etiquetas de edad legibles
_EDAD_LABELS = {
    (0, 1): "recién nacido",
    (2, 4): "2 meses",
    (4, 6): "4 meses",
    (6, 12): "6 meses",
    (12, 18): "12 meses",
    (18, 24): "18 meses",
    (36, 48): "3 años",
    (48, 72): "4 años",
    (72, 108): "1° Básico (6 años)",
    (108, 132): "4° Básico (9 años)",
    (120, 156): "5° Básico (10 años)",
    (156, 180): "8° Básico (13 años)",
}


def _edad_meses(fecha_nac: date, hoy: date | None = None) -> int:
    """Calcula la edad en meses."""
    hoy = hoy or date.today()
    meses = (hoy.year - fecha_nac.year) * 12 + (hoy.month - fecha_nac.month)
    if hoy.day < fecha_nac.day:
        meses -= 1
    return max(meses, 0)


def _parse_fecha(fecha_str: str) -> Optional[date]:
    """Parsea fecha en formatos comunes de Medilink."""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(fecha_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def get_vaccine_reminder(fecha_nacimiento: str, nombre: str = "") -> Optional[str]:
    """
    Genera un mensaje de recordatorio de vacunas PNI si el paciente
    es menor de 15 años y tiene vacunas pendientes para su edad.

    Args:
        fecha_nacimiento: fecha en formato YYYY-MM-DD o DD/MM/YYYY
        nombre: nombre del paciente (para personalizar)

    Returns:
        Mensaje de recordatorio o None si no aplica.
    """
    fecha_nac = _parse_fecha(fecha_nacimiento)
    if not fecha_nac:
        return None

    hoy = date.today()
    edad_m = _edad_meses(fecha_nac, hoy)

    # Solo menores de 15 años (180 meses)
    if edad_m >= 180:
        return None

    # Buscar vacunas que corresponden a la edad actual
    vacunas = []
    for m_min, m_max, vacuna, desc in _PNI_CALENDARIO:
        if m_min <= edad_m < m_max:
            vacunas.append((vacuna, desc))

    if not vacunas:
        return None

    # Edad legible
    if edad_m < 24:
        edad_txt = f"{edad_m} meses"
    else:
        anios = edad_m // 12
        edad_txt = f"{anios} año{'s' if anios > 1 else ''}"

    nombre_corto = nombre.split()[0] if nombre else "tu hijo/a"

    lineas = [f"💉 *Recordatorio de vacunas — {nombre_corto} ({edad_txt})*\n"]
    lineas.append("Según el Programa Nacional de Inmunización (PNI), "
                  "las vacunas que corresponden a esta edad son:\n")
    for vacuna, desc in vacunas:
        lineas.append(f"• *{vacuna}* — {desc}")

    lineas.append("\n_Consulta con el doctor en tu próxima cita si están al día._")
    lineas.append("_Vacunación gratuita en tu consultorio (CESFAM)._")

    return "\n".join(lineas)
