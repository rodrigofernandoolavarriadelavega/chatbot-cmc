"""
examenes_lab.py — reconocer un RESULTADO de examen enviado por el paciente.

POR QUÉ EXISTE (caso real 2026-08-06):
    Manuel Yaupe mandó por WhatsApp el PDF de sus exámenes de Inmunomédica.
    El bot SÍ leyó el PDF (`extract_text_from_pdf` devolvió el texto completo),
    pero como no lo reconoció, el texto entró al pipeline de agendamiento como
    si fuera un dato más y el paciente recibió:

        "¡Gracias por enviarme tus datos! 🙌 Para agendar necesito saber qué
         especialidad…"

    El problema nunca fue leer. Fue CLASIFICAR.

LA DISTINCIÓN QUE IMPORTA — resultado ≠ orden
    Una *orden* ("se solicita hemograma") es una indicación para HACERSE el
    examen: ese paciente quiere agendar, y de eso ya se encarga
    `eco_orden_ocr.py`. Un *resultado* es un documento terminado que va a la
    ficha para que lo lea el profesional. Mandar una orden por el carril del
    resultado deja al paciente sin su hora. Por eso `_es_orden_sin_resultados`
    es un veto duro, no un punto negativo más del puntaje.

CÓMO DECIDE
    Puntaje, no lista de palabras sueltas. Una señal fuerte (nombre de
    laboratorio, "toma de muestra", "valores de referencia") vale 2; una media
    (nombre de un analito, "resultado", tipo de muestra) vale 1; el umbral son
    3 puntos. Eso deja fuera al documento que menciona "resultado" una vez de
    pasada y deja entrar al que trae laboratorio + fecha de toma.

    Falla hacia el lado seguro: si no llega a 3, el documento sigue su camino
    normal. Un examen no reconocido es el bug de hoy —molesto, recuperable—;
    una hora perdida por clasificar de más es peor.
"""
from __future__ import annotations

import re
import unicodedata

# ── Señales fuertes (2 pts) ───────────────────────────────────────────────
# Laboratorios y prestadores que circulan de verdad en la provincia de Arauco
# y el Gran Concepción. Ampliar desde el evento `examen_recibido`.
_LABORATORIOS = (
    "inmunomedica", "blanco", "davila", "integramedica", "bionet",
    "labocer", "redsalud", "clinica sanatorio aleman", "hospital de arauco",
    "hospital provincial", "cesfam", "vidaintegra", "megasalud",
    "biosalud", "clinica biobio", "laboratorio clinico", "lab. clinico",
)
_FUERTES = (
    "toma de muestra", "toma muestra", "fecha de toma",
    "valor de referencia", "valores de referencia", "rango de referencia",
    "rangos de referencia", "valores normales",
    "informe de laboratorio", "resultado de examen", "resultados de examen",
    "resultado de laboratorio", "examenes de laboratorio",
)

# ── Señales medias (1 pt) ────────────────────────────────────────────────
_ANALITOS = (
    "hemograma", "glicemia", "glucosa", "colesterol", "trigliceridos",
    "creatinina", "uremia", "nitrogeno ureico", "acido urico",
    "perfil lipidico", "perfil bioquimico", "perfil hepatico",
    "hemoglobina glicosilada", "hba1c", "tsh", "t4 libre", "t3",
    "orina completa", "urocultivo", "sedimento urinario",
    "vhs", "proteina c reactiva", "pcr cuantitativa",
    "got", "gpt", "sgot", "sgpt", "bilirrubina", "fosfatasas alcalinas",
    "electrolitos plasmaticos", "sodio", "potasio", "cloro",
    "vitamina d", "vitamina b12", "ferritina", "hierro serico",
    "antigeno prostatico", "psa", "vdrl", "vih", "hepatitis b",
    "tiempo de protrombina", "ttpk", "inr",
    "hematocrito", "hemoglobina", "leucocitos", "plaquetas",
    "baciloscopia", "cultivo corriente", "test pack",
)
_MEDIAS = (
    "n° orden", "nº orden", "no orden", "numero de orden", "num. orden",
    "folio", "metodo:", "metodo :", "muestra:", "muestra :",
    "suero", "plasma", "sangre total", "orina",
    "resultado", "resultados", "unidad", "unidades",
    "profesional solicitante", "medico solicitante", "solicitado por",
    "validado por", "tecnologo medico", "bioquimico",
)

# Informes de imagenología: se tratan igual que un resultado de laboratorio.
_IMAGENES = (
    "radiografia", "ecografia", "ecotomografia", "tomografia",
    "resonancia", "mamografia", "densitometria", "endoscopia",
    "colonoscopia", "electrocardiograma", "holter", "espirometria",
)
_IMAGEN_INFORME = (
    "hallazgos", "conclusion", "impresion diagnostica", "tecnica:",
    "se informa", "informe radiologico", "medico radiologo",
)

# ── Vetos ────────────────────────────────────────────────────────────────
# Una ORDEN pide el examen; un RESULTADO lo entrega. Ver docstring.
_MARCAS_ORDEN = (
    "orden de examen", "orden de atencion", "solicitud de examen",
    "se solicita", "solicito ", "indicacion medica", "orden medica",
    "interconsulta", "derivacion",
)
# Un comprobante bancario tiene su propio carril (abono_transferencia).
_MARCAS_PAGO = (
    "comprobante", "transferencia", "monto transferido", "abono",
    "cuenta corriente", "cuenta rut", "destinatario", "nro. de operacion",
)


def _norm(t: str) -> str:
    """Minúsculas sin tildes. Los PDF de laboratorio mezclan MAYÚSCULAS,
    acentos y sin acentos en el mismo documento."""
    t = unicodedata.normalize("NFD", (t or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def _es_orden_sin_resultados(t: str) -> bool:
    """True si el documento PIDE exámenes en vez de entregarlos.

    Exige ausencia de marcas de resultado: una orden que además trae los
    valores (algunos laboratorios reimprimen la orden en la primera página)
    sigue siendo un resultado.
    """
    if not any(m in t for m in _MARCAS_ORDEN):
        return False
    marcas_resultado = ("valor de referencia", "valores de referencia",
                        "resultado", "metodo", "toma de muestra")
    return not any(m in t for m in marcas_resultado)


def parece_examen(texto: str) -> tuple[bool, list[str]]:
    """¿Es el resultado de un examen? Devuelve (veredicto, señales vistas).

    Las señales viajan al `log_event` para poder auditar por qué se clasificó
    así — sin eso, afinar el umbral sería adivinar.
    """
    t = _norm(texto)
    if len(t) < 40:                       # un PDF sin texto útil (escaneado)
        return False, []
    if _es_orden_sin_resultados(t):
        return False, ["veto:orden"]
    if any(m in t for m in _MARCAS_PAGO) and "referencia" not in t:
        return False, ["veto:pago"]

    puntaje, senales = 0, []

    for lab in _LABORATORIOS:
        if lab in t:
            puntaje += 2; senales.append(f"lab:{lab}"); break
    for f in _FUERTES:
        if f in t:
            puntaje += 2; senales.append(f); break

    vistos_analitos = [a for a in _ANALITOS if a in t]
    if vistos_analitos:
        # Varios analitos distintos es evidencia más fuerte que uno suelto:
        # un mensaje puede nombrar "colesterol", una hoja de resultados trae 8.
        puntaje += min(len(vistos_analitos), 3)
        senales += [f"analito:{a}" for a in vistos_analitos[:4]]

    vistas_medias = [m for m in _MEDIAS if m in t]
    if vistas_medias:
        puntaje += min(len(vistas_medias), 2)
        senales += vistas_medias[:3]

    # Informe de imagen: tiene vocabulario propio y casi ningún analito, así
    # que puntúa aparte. Exige la conjunción modalidad + lenguaje de informe
    # ("radiografía" sola la dice cualquiera pidiendo hora), y desde ahí suma
    # por cada marca de informe: un documento con hallazgos Y conclusión Y
    # técnica es un informe, no una mención.
    vistas_informe = [x for x in _IMAGEN_INFORME if x in t]
    if any(i in t for i in _IMAGENES) and vistas_informe:
        puntaje += 2 + min(len(vistas_informe), 2)
        senales.append(f"informe:imagen×{len(vistas_informe)}")

    return puntaje >= 3, senales


_RE_NOMBRE = re.compile(
    r"(?:nombre\s*(?:del\s*)?(?:paciente)?|paciente|apellidos?\s+y\s+nombres?)"
    r"\s*[:\-]\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑa-záéíóúñ'\.\s]{5,60})",
    re.IGNORECASE,
)


def nombre_en_examen(texto: str) -> str:
    """Nombre del paciente tal como viene impreso, o "" si no se puede leer.

    Es informativo: va en el aviso a recepción para que sepan de quién es el
    documento sin abrirlo. Nunca se usa para decidir nada.
    """
    m = _RE_NOMBRE.search(texto or "")
    if not m:
        return ""
    nombre = " ".join(m.group(1).split())
    # El regex es goloso: corta en la primera etiqueta siguiente ("Rut:",
    # "Edad:", "Fecha…") que suele venir en la misma línea del encabezado.
    nombre = re.split(
        r"\s*(?:rut|run|edad|sexo|fecha|orden|folio|previsi|medico|profesional)\b",
        nombre, maxsplit=1, flags=re.IGNORECASE,
    )[0].strip(" .:-")
    return nombre[:80] if len(nombre) >= 5 else ""
