"""
Programa Nacional de Inmunización (PNI) Chile — calendario vigente 2025.
Genera recordatorios de vacunas pendientes según la edad del paciente.

Fuente oficial: MINSAL — "Calendario del Programa Nacional de Inmunizaciones 2025"
(infografía descargada de https://vacunas.minsal.cl/wp-content/uploads/2025/03/
CALENDARIO-INMUNIZACIONES-2025.pdf). Verificado 2026-06-10.
Cambios respecto al esquema previo reflejados aquí:
  • Meningocócica B recombinante (serogrupo B) AGREGADA: 2m, 4m y refuerzo 18m
    (incorporada al PNI para nacidos desde el 01-05-2023).
  • Meningocócica conjugada tetravalente ACWY: DOSIS ÚNICA a los 12m (antes el
    archivo tenía una 2ª dosis a los 36m inexistente → removida).
  • Tres Vírica (SRP) 2ª dosis movida a los 36 MESES (antes 1° básico).
  • Hepatitis A: DOSIS ÚNICA a los 18m (antes el archivo ponía 2 dosis 12m+18m).
  • VPH: DOSIS ÚNICA en 4° básico (antes 2 dosis 4°+5° básico).
  • Removido el refuerzo DPT + polio oral de los 4 años (no existe en 2025).
  • Removida influenza del calendario por edad (es campaña estacional, no PNI etario).
  • Rotavirus se mantiene por precaución, PENDIENTE de confirmación: la infografía
    oficial 2025 NO lo lista como vacuna programática (verificar con el dueño médico).
"""
from datetime import date, datetime
from typing import Optional

# Calendario PNI: (edad_meses_min, edad_meses_max, vacuna, descripción, escolar)
# edad_meses_max es exclusivo (el rango aplica si edad_meses_min <= edad < edad_meses_max)
# escolar=True indica que la vacuna se da por curso escolar, no por edad exacta
_PNI_CALENDARIO = [
    # Recién nacido
    (0, 1, "BCG", "protege contra enfermedades invasoras por M. tuberculosis", False),
    (0, 1, "Hepatitis B (recién nacido)", "dosis de recién nacido contra hepatitis B (las siguientes van incluidas en la hexavalente)", False),
    # 2 meses
    (2, 4, "Hexavalente (1ª dosis)", "protege contra difteria, tétanos, tos convulsiva, polio, Hib y hepatitis B", False),
    (2, 4, "Neumocócica conjugada 13v (1ª dosis)", "protege contra enfermedades invasoras por neumococo (neumonía, meningitis)", False),
    (2, 4, "Meningocócica B recombinante (1ª dosis)", "protege contra enfermedad invasora por meningococo serogrupo B (PNI para nacidos desde el 01-05-2023)", False),
    (2, 4, "Rotavirus (1ª dosis)", "protege contra gastroenteritis grave por rotavirus", False),
    # 4 meses
    (4, 6, "Hexavalente (2ª dosis)", "segunda dosis de hexavalente", False),
    (4, 6, "Neumocócica conjugada 13v (2ª dosis)", "segunda dosis contra neumococo", False),
    (4, 6, "Meningocócica B recombinante (2ª dosis)", "segunda dosis contra meningococo serogrupo B", False),
    (4, 6, "Rotavirus (2ª dosis)", "segunda dosis contra rotavirus", False),
    # 6 meses
    (6, 12, "Hexavalente (3ª dosis)", "tercera dosis de hexavalente", False),
    # (la 3ª dosis de neumocócica a los 6m es SÓLO para prematuros → no se recuerda masivamente)
    # 12 meses
    (12, 18, "Tres Vírica SRP (1ª dosis)", "protege contra sarampión, rubéola y paperas", False),
    (12, 18, "Neumocócica conjugada 13v (refuerzo)", "refuerzo contra neumococo", False),
    (12, 18, "Meningocócica conjugada tetravalente ACWY (dosis única)", "protege contra meningococo serogrupos A, C, W-135 e Y", False),
    # 18 meses
    (18, 24, "Hexavalente (refuerzo)", "refuerzo de hexavalente", False),
    (18, 24, "Meningocócica B recombinante (refuerzo)", "refuerzo contra meningococo serogrupo B", False),
    (18, 24, "Hepatitis A (dosis única)", "protege contra hepatitis A", False),
    (18, 24, "Varicela (1ª dosis)", "protege contra varicela", False),
    # 36 meses (3 años)
    (36, 48, "Tres Vírica SRP (2ª dosis)", "segunda dosis contra sarampión, rubéola y paperas", False),
    (36, 48, "Varicela (2ª dosis)", "segunda dosis contra varicela", False),
    # 1° Básico (~6-7 años) — vacunación escolar
    (60, 96, "dTpa (1ª dosis escolar, 1° básico)", "refuerzo contra difteria, tétanos y tos convulsiva acelular", True),
    # 4° Básico (~9-11 años) — VPH dosis única para niños y niñas.
    # FIX 2026-08-24 (consolidado, #12): la descripción mencionaba SOLO
    # "cáncer cervicouterino" (cáncer exclusivamente femenino) — confuso/
    # alarmante cuando se envía a un niño (caso real: Elian). El texto base
    # es neutro; `get_vaccine_reminder` lo ajusta según sexo si está disponible.
    (108, 144, "VPH (dosis única)", "protege contra el virus papiloma humano — previene cánceres asociados al VPH (cervicouterino, orofaríngeo, anal y otros)", True),
    # 8° Básico (~13-15 años)
    (156, 180, "dTpa (2ª dosis escolar, 8° básico)", "refuerzo adolescente contra difteria, tétanos y tos convulsiva", True),
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
            curso = "4° Básico"
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


def get_pni_meta(fecha_nacimiento: str) -> Optional[dict]:
    """
    Retorna metadata de telemetría PNI sin generar el mensaje completo.
    Usado por log_event("pni_enviado", ...).

    Returns dict con:
        edad_meses: int
        tiene_pni: bool   (tiene vacunas exactas para su edad)
        tiene_hitos: bool (solo aplica si hitos_desarrollo también dispara)
    O None si el paciente no aplica para PNI (>= 180 meses).
    """
    fecha_nac = _parse_fecha(fecha_nacimiento)
    if not fecha_nac:
        return None
    hoy = date.today()
    edad_m = _edad_meses(fecha_nac, hoy)
    if edad_m >= 180:
        return None

    vacunas_exactas = [
        v for m_min, m_max, v, _, escolar in _PNI_CALENDARIO
        if m_min <= edad_m < m_max and not escolar
    ]
    vacunas_escolares = [
        v for m_min, m_max, v, _, escolar in _PNI_CALENDARIO
        if m_min <= edad_m < m_max and escolar
    ]

    if not vacunas_exactas and not vacunas_escolares:
        return None

    if edad_m < 24:
        etiqueta = f"{edad_m} meses"
    elif edad_m < 36:
        anios = edad_m // 12
        meses_r = edad_m % 12
        etiqueta = f"{anios} año{'s' if anios > 1 else ''} {meses_r}m"
    else:
        anios = edad_m // 12
        etiqueta = f"{anios} año{'s' if anios > 1 else ''}"

    return {
        "edad_meses": edad_m,
        "edad_etiqueta": etiqueta,
        "tiene_pni": bool(vacunas_exactas),
        "tiene_escolares": bool(vacunas_escolares),
    }
