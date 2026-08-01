"""Explotación clínica de documentos leídos por visión (docs 2-5, 2026-08-01).

Fuente ÚNICA de la detección de patologías crónicas (antes vivía inline en
flows.py — ahora flows importa de acá para que chat y documentos usen el
mismo diccionario). Además de los keywords conversacionales, incluye los
NOMBRES DE MEDICAMENTOS que aparecen en recetas: una receta de metformina
es evidencia más dura de DM2 que cualquier keyword del chat.

Qué habilita (gated DOCS_CLINICOS_ACTIVE, ver main.py):
  - resultado_examen → oferta de hora de control (el bot JAMÁS interpreta valores)
  - receta_medicamentos → tags dx:* + tabla recetas_whatsapp + oferta de control
  - órdenes de exámenes que el CMC no realiza → tabla demanda_examenes_ocr
    (evidencia dura para decidir el laboratorio en containers)
  - diagnóstico escrito en la orden → tags dx:* (alimenta doctor_alerts)
"""
from __future__ import annotations

import logging
import re
import unicodedata

log = logging.getLogger("docs_clinicos")


def _norm(texto: str) -> str:
    t = (texto or "").lower()
    out = []
    for c in t:
        if c == "ñ":
            out.append(c)
        else:
            out.append(unicodedata.normalize("NFD", c)[0])
    return "".join(out)


# ── Patologías crónicas: keywords de chat + medicamentos de receta ───────────
# Compartido con la detección pasiva de flows.py (import desde allá).
PATOLOGIAS_KEYWORDS: dict[str, list[str]] = {
    "dm2":  ["diabete", "diabetico", "diabetica", "diabetes", "insulina",
             "glicemia alta", "azucar alta", "azucar en la sangre",
             # medicamentos
             "metformina", "glibenclamida", "sitagliptina", "empagliflozina",
             "vildagliptina"],
    "hta":  ["hipertens", "presion alta", "presión alta", "hipertenso",
             "hipertensa", "antihipertensivo",
             "losartan", "enalapril", "amlodipino", "hidroclorotiazida",
             "valsartan", "atenolol", "nifedipino"],
    "asma": ["asma", "asmatico", "asmatica", "inhalador", "salbutamol",
             "broncodilatador", "budesonida", "fluticasona", "formoterol",
             "montelukast"],
    "epoc": ["epoc", "enfisema", "bronquitis cronica"],
    "hipotiroidismo": ["hipotiroid", "levotiroxina", "eutirox", "tiroides baja"],
    "dislipidemia": ["colesterol alto", "trigliceridos alto", "dislipidemia",
                     "estatina", "atorvastatina", "rosuvastatina", "lovastatina"],
    "depresion": ["depresion", "antidepresivo", "sertralina", "fluoxetina",
                  "escitalopram", "paroxetina", "venlafaxina", "citalopram"],
    "epilepsia": ["epilepsia", "epileptico", "convulsion", "anticonvulsivante",
                  "fenitoina", "carbamazepina", "acido valproico",
                  "levetiracetam"],
    "artrosis": ["artrosis", "desgaste articular", "osteoartrosis"],
    "irc": ["insuficiencia renal", "dialisis", "hemodialisis",
            "nefropatia"],
    "hpb": ["hiperplasia prostatica", "hpb", "tamsulosina", "prostata agrandada"],
}


def detectar_dx_tags(texto: str) -> list[str]:
    """Tags de patología crónica presentes en el texto (dx de orden,
    medicamentos de receta, o chat). Retorna claves como 'dm2', 'hta'."""
    t = _norm(texto)
    if not t:
        return []
    return [tag for tag, kws in PATOLOGIAS_KEYWORDS.items()
            if any(_norm(k) in t for k in kws)]


# ── Exámenes que el CMC NO realiza (demanda no satisfecha estructurada) ──────
EXAMENES_EXTERNOS: dict[str, list[str]] = {
    "radiografia": ["radiografia", "rayos x", "rx de", "rx torax", "rx "],
    "escaner_tac": ["escaner", "scanner", "tomografia", "tac de", " tac"],
    "resonancia": ["resonancia", "rnm", " rm de"],
    "mamografia": ["mamografia"],
    "endoscopia": ["endoscopia", "endoscopía"],
    "colonoscopia": ["colonoscopia", "colonoscopía"],
    "densitometria": ["densitometria"],
    "holter": ["holter"],
    "laboratorio": ["hemograma", "perfil bioquimico", "perfil lipidico",
                    "orina completa", "urocultivo", "hemoglobina glicosilada",
                    "hba1c", "tsh", "perfil hepatico", "perfil tiroideo",
                    "creatinina", "examen de sangre", "examenes de sangre"],
}


def clasificar_examen_externo(texto: str) -> str | None:
    """Si el examen pedido es de un tipo que el CMC no realiza, retorna la
    categoría (para la tabla de demanda). None si no calza (eco/kine/etc.)."""
    t = " " + _norm(texto) + " "
    for categoria, kws in EXAMENES_EXTERNOS.items():
        if any(_norm(k) in t for k in kws):
            return categoria
    return None


# ── Persistencia ─────────────────────────────────────────────────────────────
def ensure_docs_tables() -> None:
    from session import db
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recetas_whatsapp (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                phone         TEXT NOT NULL,
                medicamentos  TEXT DEFAULT '',
                dx_tags       TEXT DEFAULT '',
                filename      TEXT DEFAULT '',
                created_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS demanda_examenes_ocr (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                phone         TEXT NOT NULL,
                examen        TEXT DEFAULT '',
                categoria     TEXT DEFAULT '',
                created_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dem_ocr_cat "
                     "ON demanda_examenes_ocr(categoria)")


def registrar_receta(phone: str, medicamentos: list, dx_tags: list,
                     filename: str = "") -> None:
    ensure_docs_tables()
    from session import db
    with db() as conn:
        conn.execute(
            "INSERT INTO recetas_whatsapp (phone, medicamentos, dx_tags, filename) "
            "VALUES (?,?,?,?)",
            (phone, "; ".join(str(m)[:60] for m in medicamentos[:10]),
             ",".join(dx_tags), filename))


def nombre_mas_probable(phone: str, nombre_ocr: str) -> tuple[str, str]:
    """Concilia el nombre leído por visión contra los nombres CONOCIDOS del
    teléfono (perfil + historial de citas). La letra manuscrita produce
    lecturas imperfectas ("Anyie Ruby" por "Anguie Rondoy", caso real
    2026-08-01): si lo leído se parece a alguien conocido del número, usamos
    el nombre bueno; si no se parece a nadie, probablemente es la orden de un
    TERCERO y se muestra lo leído tal cual.

    Retorna (nombre_final, fuente) con fuente ∈ {"conocido", "ocr"}."""
    from difflib import SequenceMatcher
    from session import get_profile, db

    def _sim(a: str, b: str) -> float:
        ta = [t for t in _norm(a).split() if len(t) > 2]
        tb = [t for t in _norm(b).split() if len(t) > 2]
        if not ta or not tb:
            return 0.0
        scores = [max(SequenceMatcher(None, x, y).ratio() for y in tb)
                  for x in ta]
        return sum(scores) / len(scores)

    candidatos: list[str] = []
    try:
        p = get_profile(phone)
        if p and p.get("nombre"):
            candidatos.append(p["nombre"])
        with db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT paciente_nombre FROM citas_bot "
                "WHERE phone=? AND paciente_nombre != '' "
                "ORDER BY id DESC LIMIT 10", (phone,)).fetchall()
            candidatos += [r[0] for r in rows]
    except Exception:  # noqa: BLE001
        pass

    mejor, mejor_s = None, 0.0
    for c in dict.fromkeys(candidatos):
        s = _sim(nombre_ocr, c)
        if s > mejor_s:
            mejor, mejor_s = c, s
    if mejor and mejor_s >= 0.55:
        return mejor, "conocido"
    return nombre_ocr, "ocr"


def registrar_demanda_examen(phone: str, examen: str, categoria: str) -> None:
    ensure_docs_tables()
    from session import db
    with db() as conn:
        conn.execute(
            "INSERT INTO demanda_examenes_ocr (phone, examen, categoria) "
            "VALUES (?,?,?)", (phone, examen[:120], categoria))
