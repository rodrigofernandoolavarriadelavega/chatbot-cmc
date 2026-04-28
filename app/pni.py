"""
Programa Nacional de Inmunización (PNI) Chile 2026.
Genera recordatorios de vacunas pendientes según la edad del paciente.
Fuente: MINSAL — Calendario de Vacunación vigente.
"""
from datetime import date, datetime
from typing import Optional

# Calendario PNI: (edad_meses_min, edad_meses_max, vacuna, descripción, escolar)
# edad_meses_max es exclusivo (el rango aplica si edad_meses_min <= edad < edad_meses_max)
# escolar=True indica que la vacuna se da por curso escolar, no por edad exacta
_PNI_CALENDARIO = [
    # Recién nacido
    (0, 1, "BCG", "protege contra tuberculosis", False),
    (0, 1, "Hepatitis B (1ª dosis)", "primera dosis contra hepatitis B", False),
    # 2 meses
    (2, 4, "Hexavalente (1ª dosis)", "protege contra difteria, tétanos, tos convulsiva, polio, Hib y hepatitis B", False),
    (2, 4, "Neumocócica conjugada (1ª dosis)", "protege contra neumonía y meningitis neumocócica", False),
    (2, 4, "Rotavirus (1ª dosis)", "protege contra gastroenteritis grave por rotavirus (PNI Chile desde 2022)", False),
    # 4 meses
    (4, 6, "Hexavalente (2ª dosis)", "segunda dosis de hexavalente", False),
    (4, 6, "Neumocócica conjugada (2ª dosis)", "segunda dosis contra neumonía", False),
    (4, 6, "Rotavirus (2ª dosis)", "segunda dosis contra rotavirus", False),
    # 6 meses
    (6, 12, "Hexavalente (3ª dosis)", "tercera dosis de hexavalente", False),
    # 12 meses
    (12, 18, "Tres Vírica SRP (1ª dosis)", "protege contra sarampión, rubéola y paperas", False),
    (12, 18, "Meningocócica conjugada (1ª dosis)", "protege contra meningitis meningocócica", False),
    (12, 18, "Neumocócica conjugada (3ª dosis)", "tercera dosis contra neumonía", False),
    (12, 18, "Hepatitis A (1ª dosis)", "primera dosis contra hepatitis A", False),
    # 18 meses
    (18, 24, "Hexavalente (refuerzo)", "refuerzo de hexavalente", False),
    (18, 24, "Hepatitis A (2ª dosis)", "segunda dosis contra hepatitis A", False),
    (18, 24, "Varicela (1ª dosis)", "protege contra varicela", False),
    # 36 meses (3 años)
    (36, 48, "Varicela (2ª dosis)", "segunda dosis contra varicela", False),
    # 4 años (48 meses)
    (48, 72, "DPT (refuerzo)", "refuerzo contra difteria, tétanos y tos convulsiva", False),
    (48, 72, "Polio oral (refuerzo)", "refuerzo contra poliomielitis", False),
    # 1° Básico (~5-7 años) — vacunación escolar
    (60, 96, "Tres Vírica SRP (2ª dosis)", "segunda dosis contra sarampión, rubéola y paperas", True),
    (60, 96, "dTpa (refuerzo escolar)", "refuerzo contra difteria, tétanos y tos convulsiva acelular", True),
    # 4° Básico (~9-11 años) — VPH para niños y niñas
    (108, 144, "VPH (1ª dosis)", "primera dosis contra virus papiloma humano — previene cáncer", True),
    # 5° Básico (~10-12 años)
    (120, 156, "VPH (2ª dosis)", "segunda dosis contra virus papiloma humano", True),
    # 8° Básico (~13-15 años)
    (156, 180, "dTpa (refuerzo 8° básico)", "refuerzo adolescente contra difteria, tétanos y tos convulsiva", True),
]



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
    vacunas_exactas = []
    vacunas_escolares = []
    for m_min, m_max, vacuna, desc, escolar in _PNI_CALENDARIO:
        if m_min <= edad_m < m_max:
            if escolar:
                vacunas_escolares.append((vacuna, desc))
            else:
                vacunas_exactas.append((vacuna, desc))

    if not vacunas_exactas and not vacunas_escolares:
        return None

    # Edad legible
    if edad_m < 24:
        edad_txt = f"{edad_m} meses"
    else:
        anios = edad_m // 12
        edad_txt = f"{anios} año{'s' if anios > 1 else ''}"

    nombre_corto = ((nombre or "").split() or [""])[0] if nombre else "tu hijo/a"

    lineas = [f"💉 *Recordatorio de vacunas — {nombre_corto} ({edad_txt})*\n"]

    if vacunas_exactas:
        lineas.append("Según el Programa Nacional de Inmunización (PNI), "
                      "las vacunas que corresponden a esta edad son:\n")
        for vacuna, desc in vacunas_exactas:
            lineas.append(f"• *{vacuna}* — {desc}")

    if vacunas_escolares:
        # Determinar curso probable
        anios = edad_m // 12
        if anios <= 7:
            curso = "1° Básico"
        elif anios <= 11:
            curso = "4° o 5° Básico"
        else:
            curso = "8° Básico"
        if vacunas_exactas:
            lineas.append("")
        lineas.append(
            f"Si {nombre_corto} está en *{curso}*, "
            "podría corresponderle también:\n")
        for vacuna, desc in vacunas_escolares:
            lineas.append(f"• *{vacuna}* — {desc}")

    lineas.append("\n_Consulta con el doctor en tu próxima cita si están al día._")
    lineas.append("_Vacunación gratuita en tu consultorio (CESFAM)._")

    return "\n".join(lineas)
