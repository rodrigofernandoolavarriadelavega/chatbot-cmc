"""
Alertas personales para el Dr. Olavarría:
  1. Resumen del paciente 10 min antes de cada cita
  2. Reporte de progreso a las 08:00, 12:00, 16:00 y 20:00
"""
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

from medilink import obtener_agenda_dia
from pni import _PNI_CALENDARIO
from session import get_tags, get_phone_by_rut

log = logging.getLogger("bot.doctor_alerts")
_CHILE_TZ = ZoneInfo("America/Santiago")

# ── Exámenes preventivos por edad/sexo (para el doctor) ─────────────────────
_PREVENTIVOS = [
    (25, 64, "F", "PAP cada 3 años"),
    (40, 54, "F", "Mamografía (evaluar FR)"),
    (50, 69, "F", "Mamografía GES cada 2 años"),
    (50, 75, "M", "PSA + TR próstata"),
    (40, 99, None, "Perfil lipídico + glicemia"),
    (45, 99, None, "Control PA"),
    (50, 99, None, "TSOH / colonoscopía"),
    (65, 99, None, "EMPAM anual"),
    (65, 99, None, "Vacuna influenza + neumococo"),
    (18, 39, None, "EMP (examen preventivo)"),
    # Pediátricos (screening neonatal)
    (0, 0,   None, "Screening neonatal (TSH, PKU)"),
]

# ── Screening por edad (opción 3: siempre visible por rango etario) ──────────
_SCREENING_EDAD = [
    (20, 39, None, "Glicemia en ayunas cada 3 años (buscar DM2 precoz)"),
    (40, 99, None, "Glicemia + HbA1c si FR metabólico (sobrepeso, sedentarismo, AHF DM)"),
    (40, 99, None, "PA en consulta (screening HTA)"),
    (18, 99, None, "IMC + circunferencia cintura"),
]

# ── Guías clínicas por patología crónica (opción 2: según tags) ──────────────
_GUIAS_CRONICAS = {
    "dx:dm2": {
        "nombre": "DM2",
        "examen_fisico": "PA, IMC, cintura, pulsos pedios, sensibilidad pies (monofilamento), fondo de ojo anual",
        "examenes": "HbA1c (c/3m), glicemia ayunas, perfil lipídico, creatinina + BUN, orina completa (microalbuminuria), ELP",
        "metas": "HbA1c <7%, PA <130/80, LDL <100 (o <70 si alto RCV)",
        "recomendaciones": "Dieta + ejercicio 150 min/sem, control podológico, vacuna influenza anual, evaluar fondo de ojo",
    },
    "dx:hta": {
        "nombre": "HTA",
        "examen_fisico": "PA ambos brazos, fondo de ojo, soplos carotídeos, edema EEII, pulsos periféricos",
        "examenes": "Creatinina + BUN, ELP (K+), perfil lipídico, glicemia, orina completa, ECG, Rx tórax si sospecha DOB",
        "metas": "PA <140/90 general; <130/80 si DM2 o ERC",
        "recomendaciones": "Restricción sal <5g/día, ejercicio aeróbico 30 min/día, evaluar adherencia farmacológica, AMPA domiciliario",
    },
    "dx:asma": {
        "nombre": "Asma",
        "examen_fisico": "Auscultación pulmonar (sibilancias), FR, SpO2, uso musculatura accesoria, rinitis alérgica",
        "examenes": "Espirometría (c/año), flujometría (PEF), Rx tórax si primera consulta, IgE total si sospecha alergia",
        "metas": "Síntomas diurnos ≤2/sem, sin limitación actividad, sin despertares nocturnos, SABA ≤2/sem",
        "recomendaciones": "Técnica inhalatoria correcta, plan de acción escrito, evitar gatillantes (humo, ácaros), vacuna influenza anual",
    },
    "dx:epoc": {
        "nombre": "EPOC",
        "examen_fisico": "Auscultación (MP disminuido, sibilancias), SpO2, FR, tórax en tonel, cianosis, edema",
        "examenes": "Espirometría con BD (VEF1/CVF <0.7), Rx tórax, GSA si SpO2 <92%, hemograma (poliglobulia)",
        "metas": "Dejar tabaco, vacunación al día, minimizar exacerbaciones (<2/año)",
        "recomendaciones": "Consejería cesación tabaco, rehabilitación pulmonar, vacuna influenza + neumococo, evaluar O2 domiciliario si PaO2 <55",
    },
    "dx:hipotiroidismo": {
        "nombre": "Hipotiroidismo",
        "examen_fisico": "Palpación tiroides, piel seca, edema, reflejos enlentecidos, bradicardia, peso",
        "examenes": "TSH (c/6-12m), T4L si TSH alterada, perfil lipídico (dislipidemia 2ria)",
        "metas": "TSH 0.5-4.0 mUI/L, eutiroideo clínico",
        "recomendaciones": "Levotiroxina en ayunas 30 min antes de desayuno, no tomar con calcio/hierro/omeprazol, control TSH 6-8 sem post-ajuste",
    },
    "dx:dislipidemia": {
        "nombre": "Dislipidemia",
        "examen_fisico": "IMC, cintura, xantelasmas, arco corneal, soplos carotídeos",
        "examenes": "Perfil lipídico completo (CT, LDL, HDL, TG) c/6-12m, glicemia, creatinina, GOT/GPT (si en estatina)",
        "metas": "LDL <100 (alto RCV) o <70 (muy alto RCV), TG <150",
        "recomendaciones": "Dieta mediterránea, ejercicio 150 min/sem, evaluar adherencia a estatina, control hepático a las 12 sem",
    },
    "dx:depresion": {
        "nombre": "Depresión",
        "examen_fisico": "Ánimo, afecto, ideación suicida (PHQ-9), sueño, apetito, funcionalidad",
        "examenes": "TSH (descartar hipotiroidismo), hemograma (anemia), B12/folato si adulto mayor",
        "metas": "PHQ-9 <5, funcionalidad recuperada, adherencia ≥6 meses post-remisión",
        "recomendaciones": "Psicoterapia + fármaco si moderada-severa, actividad física, higiene del sueño, control c/2-4 sem inicial",
    },
    "dx:epilepsia": {
        "nombre": "Epilepsia",
        "examen_fisico": "Examen neurológico completo, signos de lateralización, mordedura lingual, lesiones por caídas",
        "examenes": "Niveles plasmáticos de anticonvulsivante, hemograma, PH, EEG anual, RM cerebro si no tiene",
        "metas": "Libre de crisis ≥1 año, sin efectos adversos, licencia de conducir tras 2 años sin crisis",
        "recomendaciones": "Adherencia estricta, no suspender bruscamente, evitar privación de sueño y alcohol, ácido fólico si mujer en edad fértil",
    },
    "dx:artrosis": {
        "nombre": "Artrosis",
        "examen_fisico": "ROM articular, crepitación, derrame, alineamiento (varo/valgo), marcha, fuerza muscular periarticular",
        "examenes": "Rx articulación afectada (AP + lateral), VHS/PCR solo si sospecha inflamatoria, hemograma",
        "metas": "Control dolor (EVA <4), mantener funcionalidad, prevenir progresión",
        "recomendaciones": "Ejercicio terapéutico + fortalecimiento muscular, baja de peso si IMC >25, paracetamol/AINE tópico, derivar kine, evaluar cirugía si Kellgren ≥3 con falla tto conservador",
    },
    "dx:irc": {
        "nombre": "IRC",
        "examen_fisico": "PA, edema, palidez, piel urémico, soplos abdominales, peso seco",
        "examenes": "Creatinina + TFGe (c/3-6m), orina completa + RAC, ELP, Ca/P, PTH, hemograma, ferritina, albúmina",
        "metas": "PA <130/80, proteinuria <500 mg/día, K+ 3.5-5.0, Hb 10-12, evitar nefrotóxicos",
        "recomendaciones": "IECA/ARA2 si proteinuria, restricción proteica si TFG <30, evitar AINE y contrastes yodados, derivar nefrología si TFG <30 o caída rápida",
    },
}


def _get_vacunas_pni(edad_anios: int) -> list[str]:
    """Retorna vacunas PNI específicas para la edad (en años)."""
    edad_meses = edad_anios * 12
    vacunas = []
    for m_min, m_max, vacuna, _desc, escolar in _PNI_CALENDARIO:
        if m_min <= edad_meses < m_max:
            tag = " (escolar)" if escolar else ""
            vacunas.append(f"{vacuna}{tag}")
    return vacunas


def _get_guias_cronicas(tags: list[str]) -> list[dict]:
    """Retorna guías clínicas para las patologías crónicas detectadas en tags."""
    guias = []
    for tag in tags:
        if tag in _GUIAS_CRONICAS:
            guias.append(_GUIAS_CRONICAS[tag])
    return guias


def _get_preventivos_doctor(edad_str: str, sexo: str) -> list[str]:
    """Retorna lista de exámenes preventivos según edad/sexo."""
    if not edad_str:
        return []
    try:
        edad = int(edad_str.split()[0])
    except (ValueError, IndexError):
        return []
    sexo_upper = (sexo or "").upper()[:1]
    result = []
    for e_min, e_max, e_sexo, msg in _PREVENTIVOS:
        if e_min <= edad <= e_max:
            if e_sexo is None or e_sexo == sexo_upper:
                result.append(msg)
    # Screening general por edad (opción 3)
    for e_min, e_max, e_sexo, msg in _SCREENING_EDAD:
        if e_min <= edad <= e_max:
            if e_sexo is None or e_sexo == sexo_upper:
                result.append(msg)
    # Vacunas PNI específicas para menores de 15 años
    if edad <= 15:
        vacunas = _get_vacunas_pni(edad)
        for v in vacunas:
            result.append(f"PNI: {v}")
    return result

# Dr. Olavarría — ID 1 en Medilink
DOCTOR_PROF_ID = 1
DOCTOR_NOMBRE = "Rodrigo"

# Track de resúmenes ya enviados (por id_cita) para no repetir
_resumenes_enviados: set[int] = set()


def _ahora_cl() -> datetime:
    return datetime.now(_CHILE_TZ)


async def enviar_resumen_precita(send_fn, doctor_phone: str):
    """
    Llamar cada 5 min. Si hay un paciente con cita en los próximos 10 min,
    envía un resumen al WhatsApp del doctor.
    """
    ahora = _ahora_cl()
    fecha_hoy = ahora.strftime("%Y-%m-%d")
    hora_actual = ahora.hour * 60 + ahora.minute

    try:
        agenda = await obtener_agenda_dia(DOCTOR_PROF_ID, fecha_hoy)
    except Exception as e:
        log.error("Error obteniendo agenda para resumen pre-cita: %s", e)
        return

    for cita in agenda:
        cid = cita["id_cita"]
        if cid in _resumenes_enviados:
            continue

        # Parsear hora de la cita
        try:
            h, m = map(int, cita["hora"].split(":"))
            hora_cita = h * 60 + m
        except (ValueError, AttributeError):
            continue

        # Enviar si faltan entre 0 y 10 minutos
        diff = hora_cita - hora_actual
        if 0 <= diff <= 10:
            pac = cita["paciente"] or "Sin nombre"
            rut = cita["rut"] or "Sin RUT"
            edad = cita["edad"] or "—"
            sexo_txt = {"M": "Masculino", "F": "Femenino"}.get(cita.get("sexo", ""), "—")

            msg = (
                f"📋 *Próximo paciente — {cita['hora']}*\n\n"
                f"👤 *{pac}*\n"
                f"🪪 RUT: {rut}\n"
                f"🎂 Edad: {edad}\n"
                f"⚧ Sexo: {sexo_txt}\n"
            )

            # Exámenes preventivos por edad/sexo + screening
            preventivos = _get_preventivos_doctor(edad, cita.get("sexo", ""))
            if preventivos:
                msg += "\n🩺 *Preventivo / screening:*\n"
                for p in preventivos:
                    msg += f"  • {p}\n"

            # Guías clínicas por patología crónica (tags del paciente)
            phone_pac = get_phone_by_rut(rut)
            tags = get_tags(phone_pac) if phone_pac else []
            guias = _get_guias_cronicas(tags)
            if guias:
                for g in guias:
                    msg += (
                        f"\n⚠️ *{g['nombre']}*\n"
                        f"  🔍 Buscar: {g['examen_fisico']}\n"
                        f"  🧪 Pedir: {g['examenes']}\n"
                        f"  🎯 Metas: {g['metas']}\n"
                        f"  💊 Rec: {g['recomendaciones']}\n"
                    )

            _resumenes_enviados.add(cid)
            try:
                await send_fn(doctor_phone, msg)
                log.info("Resumen pre-cita enviado: cita=%d pac=%s hora=%s tags=%s",
                         cid, pac, cita["hora"], [t for t in tags if t.startswith("dx:")])
            except Exception as e:
                log.error("Error enviando resumen pre-cita: %s", e)


async def enviar_reporte_progreso(send_fn, doctor_phone: str):
    """
    Llamar a las 08:00, 12:00, 16:00 y 20:00.
    Envía cuántos pacientes tiene agendados, cuántos ya pasaron, cuántos faltan.
    """
    ahora = _ahora_cl()
    fecha_hoy = ahora.strftime("%Y-%m-%d")
    hora_actual = ahora.hour * 60 + ahora.minute

    try:
        agenda = await obtener_agenda_dia(DOCTOR_PROF_ID, fecha_hoy)
    except Exception as e:
        log.error("Error obteniendo agenda para reporte progreso: %s", e)
        return

    total = len(agenda)

    if total == 0:
        msg = (
            f"📊 *Reporte {ahora.strftime('%H:%M')}*\n\n"
            "No tienes pacientes agendados para hoy 🎉"
        )
        await send_fn(doctor_phone, msg)
        return

    vistos = 0
    pendientes = 0
    proximos = []

    for cita in agenda:
        try:
            h, m = map(int, cita["hora"].split(":"))
            hora_cita = h * 60 + m
        except (ValueError, AttributeError):
            pendientes += 1
            continue

        if hora_cita < hora_actual:
            vistos += 1
        else:
            pendientes += 1
            if len(proximos) < 3:
                pac = cita["paciente"] or "Sin nombre"
                proximos.append(f"  • {cita['hora']} — {pac}")

    msg = (
        f"📊 *Reporte {ahora.strftime('%H:%M')}*\n\n"
        f"📅 Agendados hoy: *{total}*\n"
        f"✅ Atendidos: *{vistos}*\n"
        f"⏳ Pendientes: *{pendientes}*\n"
    )

    if proximos:
        msg += f"\n*Próximos:*\n" + "\n".join(proximos)

    if pendientes == 0:
        msg += "\n\n🎉 *¡Terminaste tu agenda de hoy!*"

    try:
        await send_fn(doctor_phone, msg)
        log.info("Reporte progreso enviado: total=%d vistos=%d pendientes=%d",
                 total, vistos, pendientes)
    except Exception as e:
        log.error("Error enviando reporte progreso: %s", e)


def reset_resumenes_diarios():
    """Llamar a medianoche para limpiar el set de resúmenes enviados."""
    _resumenes_enviados.clear()
