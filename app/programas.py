"""
Router /alma/api/programas — Motor genérico de Programas Clínicos por especialidad.

Generaliza lo que Kinesiología y Ortodoncia hacen a mano, para cubrir el resto de
las especialidades del CMC sin reescribir la lógica cada vez. La idea de negocio
es la misma: la plata recurrente está en que el paciente VUELVA cuando debe —
sea para completar un tratamiento (adherencia) o para su control periódico
(recall). Dejar que un paciente se enfríe es ingreso y resultado clínico perdido.

Dos tipos de programa (config `tipo`):
  • "adherencia"  → tratamiento multi-sesión. Se detectan episodios (un hueco
        grande abre uno nuevo) y se clasifica por riesgo de abandono según los
        días desde la última sesión vs la cadencia esperada.
        Ej: Nutrición, Psicología, Fonoaudiología, Odontología.
  • "control"     → recall periódico. El paciente debería volver cada N días;
        si pasó el plazo está "por vencer" o "vencido" (a citar).
        Ej: Cardiología, Gastro, Ginecología, Matrona, Podología, Traumatología,
        Medicina General (crónicos/chequeo).

Cada programa es una entrada en PROGRAMAS (abajo). Agregar una especialidad nueva
= agregar una entrada de config; no hay código nuevo.

Fuente: bi.fact_atenciones + dim_paciente + dim_profesional + fact_ingresos.
Capa propia: sessions.db tabla `programa_plan` (plan/estado por programa+paciente).
Degradación elegante: BI caída → vacío + source_status="bi_unavailable".

Valor agéntico:
  • /accion/{paciente_id}  redacta el mensaje WhatsApp personalizado (next best
    action) para ese paciente según su estado — listo para enviar.
  • /digest  arma la "lista de hoy" priorizada cruzando todos los programas:
    a quién contactar primero para no perder ingreso recurrente.
"""
import csv
import io
import json
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from bi_helper import bi_query

log = logging.getLogger("programas")
_CHILE_TZ = ZoneInfo("America/Santiago")

router = APIRouter(prefix="/alma/api/programas", tags=["programas"])


# ── Registro de programas ────────────────────────────────────────────────────
# tipo: "adherencia" | "control"
# Para adherencia: en_curso_max / riesgo_max / abandono_max / gap_nuevo (días).
# Para control:    control_ok / control_due (días desde la última visita).
PROGRAMAS: dict[str, dict] = {
    "nutricion": {
        "label": "Nutrición", "especialidad_ids": [4], "tipo": "adherencia", "clinico": True,
        "cadencia": 21, "en_curso_max": 21, "riesgo_max": 45, "abandono_max": 90, "gap_nuevo": 150,
        "objetivo": "El control nutricional funciona con seguimiento cada 2-3 semanas. El que deja de venir no baja de peso y no vuelve.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. Para que tu plan nutricional dé resultados conviene no cortar el seguimiento. ¿Agendamos tu próximo control con la nutricionista?",
    },
    "psicologia": {
        "label": "Psicología", "especialidad_ids": [5], "tipo": "adherencia",
        "cadencia": 14, "en_curso_max": 16, "riesgo_max": 35, "abandono_max": 75, "gap_nuevo": 120,
        "objetivo": "La terapia psicológica pierde efecto si se interrumpe. Detectar al que dejó de venir permite reenganchar antes de que abandone el proceso.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. Notamos que ha pasado un tiempo desde tu última sesión. Si quieres retomar tu proceso, te ayudamos a agendar con tu psicólogo.",
    },
    "fonoaudiologia": {
        "label": "Fonoaudiología", "especialidad_ids": [8], "tipo": "adherencia",
        "cadencia": 14, "en_curso_max": 16, "riesgo_max": 35, "abandono_max": 75, "gap_nuevo": 120,
        "objetivo": "El tratamiento fonoaudiológico es de sesiones seguidas; cortar a mitad de camino frena el avance.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. ¿Retomamos tus sesiones de fonoaudiología para no perder el avance logrado?",
    },
    "odontologia": {
        "label": "Odontología", "especialidad_ids": [9], "tipo": "adherencia",
        "cadencia": 21, "en_curso_max": 30, "riesgo_max": 60, "abandono_max": 120, "gap_nuevo": 200,
        "objetivo": "Un tratamiento dental a medio terminar es una boca abierta y un presupuesto sin cerrar. Reenganchar al que dejó controles recupera ingreso y termina el caso.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. Tienes un tratamiento dental en curso. ¿Coordinamos tu próxima hora para terminarlo?",
    },
    "podologia": {
        "label": "Podología", "especialidad_ids": [12], "tipo": "control",
        "cadencia": 60, "control_ok": 60, "control_due": 80,
        "objetivo": "El pie diabético y el cuidado podológico necesitan atención periódica (~cada 2 meses). El recall evita complicaciones y fideliza.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. Ya corresponde tu control podológico. ¿Te agendamos una hora?",
    },
    "cardiologia": {
        "label": "Cardiología", "especialidad_ids": [16], "tipo": "control",
        "cadencia": 180, "control_ok": 180, "control_due": 240,
        "objetivo": "El paciente cardiológico crónico debe controlarse cada ~6 meses. Especialidad de baja oferta (1 día/sem) y alta prevalencia en la zona: el recall asegura que el cupo se llene con quien lo necesita.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. Corresponde tu control cardiológico. ¿Coordinamos tu hora con el Dr. Millán?",
    },
    "gastroenterologia": {
        "label": "Gastroenterología", "especialidad_ids": [18], "tipo": "control",
        "cadencia": 180, "control_ok": 180, "control_due": 240,
        "objetivo": "Control digestivo crónico y seguimiento de estudios. El recall ordena la agenda de una especialidad de baja frecuencia.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. Corresponde tu control gastroenterológico. ¿Te agendamos?",
    },
    "ginecologia": {
        "label": "Ginecología", "especialidad_ids": [14], "tipo": "control",
        "cadencia": 365, "control_ok": 365, "control_due": 420,
        "objetivo": "Control ginecológico y PAP anual. El recall preventivo trae de vuelta a la paciente para su examen del año.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. Ya corresponde tu control ginecológico anual (y PAP si toca). ¿Coordinamos tu hora?",
    },
    "matrona": {
        "label": "Matrona", "especialidad_ids": [11], "tipo": "control",
        "cadencia": 180, "control_ok": 180, "control_due": 240,
        "objetivo": "Controles de matrona (PAP, regulación de fertilidad, control sano). Recall preventivo.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. Corresponde tu control con la matrona. ¿Te agendamos una hora?",
    },
    "traumatologia": {
        "label": "Traumatología", "especialidad_ids": [17], "tipo": "control",
        "cadencia": 45, "control_ok": 45, "control_due": 70,
        "objetivo": "Recontrol post-tratamiento traumatológico. Cerrar el ciclo de control evita que el paciente quede sin seguimiento (y muchas veces deriva a kinesiología).",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. ¿Necesitas tu control de traumatología o continuar con la indicación de kinesiología?",
    },
    "medicina_general": {
        "label": "Medicina General", "especialidad_ids": [1, 10], "tipo": "control",
        "cadencia": 180, "control_ok": 180, "control_due": 270,
        "objetivo": "Pacientes crónicos (HTA, DM2) y chequeo preventivo. El recall a los que no vuelven hace seguimiento de salud y llena agenda de la base más grande del CMC.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. Ya corresponde tu control de medicina general. ¿Coordinamos tu hora?",
    },
}


# ── Tamizaje preventivo (cohortes poblacionales por edad/sexo) ───────────────
# A diferencia de PROGRAMAS (tratamiento, decenas de pacientes), el tamizaje
# mira TODA la base por demografía y detecta a quién le corresponde un control
# preventivo que no tiene reciente. Son miles → NO entran en la "lista de hoy"
# unificada (build_digest solo recorre PROGRAMAS); es un workflow aparte de salud
# poblacional. Por cohorte distinguimos:
#   • "lapsed" → tuvo el control pero se atrasó (alta confianza, recall directo).
#   • "nunca"  → sin registro de ese control (puede ser real o gap de backfill;
#                validar antes de outreach masivo).
# Edad = EXTRACT(YEAR FROM AGE(fecha_nacimiento)) (validado en Postgres 16 del CMC).
TAMIZAJE: dict[str, dict] = {
    "pap": {
        "label": "PAP / Control ginecológico", "tipo": "tamizaje",
        "genero": "F", "edad_min": 25, "edad_max": 64,
        "esp_recall": [11, 14, 15], "dias": 1095,
        "objetivo": "Mujeres 25-64 sin control ginecológico/PAP en 3 años. El PAP es tamizaje de cáncer cervicouterino — recall preventivo de alto impacto y cubierto por Fonasa/GES.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. Te recordamos que el PAP/control ginecológico se recomienda cada 3 años. ¿Te agendamos tu hora con la matrona o ginecólogo?",
    },
    "empam": {
        "label": "EMPAM (adulto mayor)", "tipo": "tamizaje",
        "genero": None, "edad_min": 65, "edad_max": 110,
        "esp_recall": [1, 10], "dias": 365,
        "objetivo": "Personas 65+ sin control de medicina general en 1 año. El EMPAM (Examen de Medicina Preventiva del Adulto Mayor) es anual y GES — detecta riesgos y ordena la salud del paciente crónico.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. Corresponde su Examen de Medicina Preventiva del Adulto Mayor (EMPAM), es anual y gratuito. ¿Le agendamos su hora?",
    },
    "cardiovascular": {
        "label": "Chequeo cardiovascular (40-64)", "tipo": "tamizaje",
        "genero": None, "edad_min": 40, "edad_max": 64,
        "esp_recall": [1, 10], "dias": 365,
        "objetivo": "Adultos 40-64 sin control de medicina general en 1 año. Ventana para pesquisar hipertensión, diabetes y riesgo cardiovascular antes de que se complique.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. Ya corresponde tu chequeo preventivo de salud (presión, azúcar, riesgo cardiovascular). ¿Coordinamos tu control de medicina general?",
    },
    "nino_sano": {
        "label": "Control del Niño Sano (0-6)", "tipo": "tamizaje",
        "genero": None, "edad_min": 0, "edad_max": 6,
        "esp_recall": [1, 10, 11, 21], "dias": 365,
        "objetivo": "Niños 0-6 sin control de salud (matrona/medicina general) en 1 año. El control del niño sano es GES y pesquisa rezago del desarrollo, nutrición y vacunas — recall preventivo de alto impacto en primaria.",
        "wa": "Hola {primer}, te saludamos del Centro Médico Carampangue. Corresponde el control del niño sano (es gratuito y revisa crecimiento, desarrollo y vacunas). ¿Le agendamos su hora con la matrona o el médico?",
    },
}


# ── Auth ──────────────────────────────────────────────────────────────────────

def _require_admin(request: Request, token: str | None,
                   cmc_session: str | None) -> tuple[str, int | None]:
    """(token_efectivo, scope_profesional). scope None = ve todos los programas;
    int = un profesional viendo solo SU programa + SUS pacientes. Vía alma_scope."""
    from alma_scope import resolve
    return resolve(request, token, cmc_session, "programas")


def _prog_for_prof(pid: int | None) -> str | None:
    """Programa al que pertenece un profesional (por su especialidad). Para forzar
    que un perfil de profesional solo vea SU programa."""
    if pid is None:
        return None
    for prog, cfg in PROGRAMAS.items():
        for e in cfg.get("especialidad_ids", []):
            if pid in _ESP_TO_PROF.get(e, []):
                return prog
    return None


def _cfg(prog: str) -> dict:
    cfg = PROGRAMAS.get(prog)
    if not cfg:
        raise HTTPException(404, f"Programa '{prog}' no existe")
    return cfg


# ── Capa de gestión propia ───────────────────────────────────────────────────

def ensure_programa_plan_table() -> None:
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS programa_plan (
                programa       TEXT NOT NULL,
                paciente_id    INTEGER NOT NULL,
                sesiones_plan  INTEGER DEFAULT 0,
                estado_manual  TEXT DEFAULT '',   -- '' | 'alta' | 'pausa' | 'no_contactar'
                notas          TEXT DEFAULT '',   -- etiqueta corta visible en la lista
                resumen        TEXT DEFAULT '',   -- resumen clínico largo por programa+paciente
                segmento       TEXT DEFAULT '',   -- categoría de segmentación (nombre, editable)
                pendientes     TEXT DEFAULT '[]', -- checklist JSON: [{"texto","hecho"}]
                updated_at     TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (programa, paciente_id)
            )
        """)
        # Migración para DBs creadas antes de estas columnas (prod ya tiene la tabla).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(programa_plan)").fetchall()}
        if "resumen" not in cols:
            conn.execute("ALTER TABLE programa_plan ADD COLUMN resumen TEXT DEFAULT ''")
        if "segmento" not in cols:
            conn.execute("ALTER TABLE programa_plan ADD COLUMN segmento TEXT DEFAULT ''")
        if "pendientes" not in cols:
            conn.execute("ALTER TABLE programa_plan ADD COLUMN pendientes TEXT DEFAULT '[]'")
        if "peso_meta" not in cols:
            conn.execute("ALTER TABLE programa_plan ADD COLUMN peso_meta REAL")
        if "objetivo" not in cols:
            conn.execute("ALTER TABLE programa_plan ADD COLUMN objetivo TEXT DEFAULT ''")
        if "plan" not in cols:
            conn.execute("ALTER TABLE programa_plan ADD COLUMN plan TEXT DEFAULT ''")
        conn.commit()


# ── Capa clínica (mediciones de peso/medidas + objetivo clínico) ──────────────
DEFAULT_OBJETIVOS = ["Bajar peso", "Subir peso", "Mantención", "Diabético", "Deportista", "Embarazo"]


def ensure_medicion_table() -> None:
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS programa_medicion (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                programa    TEXT NOT NULL,
                paciente_id INTEGER NOT NULL,
                fecha       TEXT NOT NULL,          -- YYYY-MM-DD del control
                peso        REAL,                   -- kg
                cintura     REAL,                   -- cm (opcional)
                grasa       REAL,                   -- % (opcional)
                nota        TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_medicion_pac ON programa_medicion(programa, paciente_id, fecha)")
        conn.commit()


def ensure_categoria_table() -> None:
    """Categorías editables multi-dimensión (dim='objetivo' para el motivo clínico).
    La segmentación demográfica sigue en programa_segmento (sin tocar)."""
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS programa_categoria (
                programa TEXT NOT NULL,
                dim      TEXT NOT NULL,
                nombre   TEXT NOT NULL,
                orden    INTEGER DEFAULT 0,
                PRIMARY KEY (programa, dim, nombre)
            )
        """)
        conn.commit()


def _categorias(prog: str, dim: str, defaults: list[str]) -> list[dict]:
    ensure_categoria_table()
    from session import _conn
    with _conn() as conn:
        rows = conn.execute("SELECT nombre, orden FROM programa_categoria WHERE programa=? AND dim=? ORDER BY orden, nombre", (prog, dim)).fetchall()
        if not rows and defaults:
            for i, n in enumerate(defaults):
                conn.execute("INSERT OR IGNORE INTO programa_categoria (programa, dim, nombre, orden) VALUES (?,?,?,?)", (prog, dim, n, i))
            conn.commit()
            rows = conn.execute("SELECT nombre, orden FROM programa_categoria WHERE programa=? AND dim=? ORDER BY orden, nombre", (prog, dim)).fetchall()
    return [{"nombre": r["nombre"], "orden": r["orden"]} for r in rows]


def _mediciones(prog: str, pid: int) -> list[dict]:
    ensure_medicion_table()
    from session import _conn
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, fecha, peso, cintura, grasa, nota FROM programa_medicion "
            "WHERE programa=? AND paciente_id=? ORDER BY fecha, id", (prog, pid)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Categorías de segmentación (editables por la profesional, por programa) ───
DEFAULT_SEGMENTOS = ["Adultos", "Niños", "Trabajadores"]


def ensure_segmento_table() -> None:
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS programa_segmento (
                programa TEXT NOT NULL,
                nombre   TEXT NOT NULL,
                orden    INTEGER DEFAULT 0,
                PRIMARY KEY (programa, nombre)
            )
        """)
        conn.commit()


def _segmentos(prog: str) -> list[dict]:
    """Categorías de un programa. Si no hay ninguna todavía, siembra las 3 por
    defecto (la profesional luego las renombra / agrega / borra)."""
    ensure_segmento_table()
    from session import _conn
    with _conn() as conn:
        rows = conn.execute("SELECT nombre, orden FROM programa_segmento WHERE programa=? ORDER BY orden, nombre", (prog,)).fetchall()
        if not rows:
            for i, n in enumerate(DEFAULT_SEGMENTOS):
                conn.execute("INSERT OR IGNORE INTO programa_segmento (programa, nombre, orden) VALUES (?,?,?)", (prog, n, i))
            conn.commit()
            rows = conn.execute("SELECT nombre, orden FROM programa_segmento WHERE programa=? ORDER BY orden, nombre", (prog,)).fetchall()
    return [{"nombre": r["nombre"], "orden": r["orden"]} for r in rows]


def _parse_pendientes(raw) -> list[dict]:
    """Lee la columna JSON `pendientes` de forma tolerante → lista de {texto, hecho}."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except Exception:
        return []
    out = []
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict) and (it.get("texto") or "").strip():
                out.append({"texto": str(it["texto"]).strip()[:300], "hecho": bool(it.get("hecho"))})
    return out


def _planes(prog: str) -> dict[int, dict]:
    ensure_programa_plan_table()
    from session import _conn
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM programa_plan WHERE programa=?", (prog,)).fetchall()
    return {r["paciente_id"]: dict(r) for r in rows}


# ── BI ──────────────────────────────────────────────────────────────────────

# Mapa especialidad_id (BI) → ids de profesional (Medilink), para filtrar la CAJA REAL.
# Derivado del roster medilink.PROFESIONALES (cambia lento). Ortodoncia/endo/implanto/
# estética son módulos aparte; aquí solo las especialidades de programas de tratamiento.
_ESP_TO_PROF = {
    4:  [52],          # Nutrición — Gisela Pinto
    5:  [74, 49],      # Psicología — Jorge Montalba, Juan Pablo Rodríguez
    8:  [70],          # Fonoaudiología — Juana Arratia
    9:  [55, 72],      # Odontología general — Javiera Burgos, Carlos Jiménez
    12: [56],          # Podología — Andrea Guevara
    16: [60],          # Cardiología — Miguel Millán
    18: [65],          # Gastroenterología — Nicolás Quijano
    14: [61],          # Ginecología — Tirso Rejón
    11: [67],          # Matrona — Sarai Gómez
    17: [64],          # Traumatología — Claudio Barraza
    1:  [1, 73, 13],   # Medicina General — Olavarría, Abarca, Márquez
    10: [1, 73, 13],   # Medicina General (especialidad 10 unificada)
}


def _bi_rows(especialidad_ids: list[int], meses: int,
             scope_prof: int | None = None) -> tuple[list[dict], str]:
    """Visitas de tratamiento desde la CAJA REAL (fresca, sin Docker), no la BI vieja.
    Resuelve especialidad → profesionales y delega en caja_helper. El TAMIZAJE
    (cohortes poblacionales de quién NO vino) sigue en la BI — ver _compute_tamizaje.

    `scope_prof`: si se pasa, acota a ESE profesional (su perfil ve solo sus
    pacientes; clave en psicología, donde 2 profes comparten especialidad)."""
    from caja_helper import caja_visitas
    if scope_prof is not None:
        return caja_visitas([scope_prof], meses)
    prof_ids: list[int] = []
    for e in especialidad_ids:
        prof_ids.extend(_ESP_TO_PROF.get(e, []))
    prof_ids = list(dict.fromkeys(prof_ids))  # dedup preservando orden
    return caja_visitas(prof_ids, meses)


def _today() -> date:
    return datetime.now(_CHILE_TZ).date()


def _split_episodios(fechas: list[date], gap: int) -> list[list[date]]:
    if not fechas:
        return []
    eps, cur = [], [fechas[0]]
    for f in fechas[1:]:
        if (f - cur[-1]).days > gap:
            eps.append(cur); cur = [f]
        else:
            cur.append(f)
    eps.append(cur)
    return eps


def _estado_adherencia(dias, sesiones, plan, cfg):
    if plan and plan.get("estado_manual") == "alta":
        return "alta", "Dado de alta"
    if plan and plan.get("estado_manual") == "no_contactar":
        return "no_contactar", "No contactar"
    sp = (plan or {}).get("sesiones_plan", 0) or 0
    if sp and sesiones >= sp:
        return "completado", "Plan completado"
    if dias <= cfg["en_curso_max"]:
        return "en_curso", "En tratamiento"
    if dias <= cfg["riesgo_max"]:
        return "riesgo", "Riesgo de abandono"
    if dias <= cfg["abandono_max"]:
        return "abandono", "Abandono temprano"
    return "cerrado", "Episodio cerrado"


def _estado_control(dias, plan, cfg):
    if plan and plan.get("estado_manual") in ("alta", "no_contactar"):
        return "no_contactar", "No contactar"
    if dias <= cfg["control_ok"]:
        return "al_dia", "Al día"
    if dias <= cfg["control_due"]:
        return "pronto", "Control por vencer"
    return "vencido", "Control vencido"


# Buckets accionables (worklist) por tipo de programa
ACCIONABLES = {
    "adherencia": ("riesgo", "abandono"),
    "control": ("vencido", "pronto"),
    "tamizaje": ("lapsed", "nunca"),
}


def _compute(prog: str, meses: int = 6, scope_prof: int | None = None):
    cfg = _cfg(prog)
    rows, status = _bi_rows(cfg["especialidad_ids"], max(meses, 12), scope_prof)
    planes = _planes(prog)
    today = _today()
    es_clinico = bool(cfg.get("clinico"))
    meds_por_pac: dict[int, list] = {}
    if es_clinico:
        ensure_medicion_table()
        from session import _conn as _c_med
        with _c_med() as _cm:
            for _m in _cm.execute(
                "SELECT paciente_id, peso FROM programa_medicion "
                "WHERE programa=? AND peso IS NOT NULL ORDER BY paciente_id, fecha, id", (prog,)
            ).fetchall():
                meds_por_pac.setdefault(_m["paciente_id"], []).append(_m["peso"])

    porpac: dict[int, dict] = {}
    for r in rows:
        f = r["fecha"]
        if isinstance(f, str):
            try:
                f = datetime.strptime(f[:10], "%Y-%m-%d").date()
            except Exception:
                continue
        pid = r["paciente_id"]
        d = porpac.setdefault(pid, {
            "paciente_id": pid, "paciente": r["paciente"], "telefono": r.get("telefono") or "",
            "lugar": r.get("lugar") or "", "fechas": [], "monto": 0.0, "prof_last": "—",
        })
        d["fechas"].append(f)
        d["monto"] += float(r.get("monto") or 0)
        d["prof_last"] = r.get("profesional") or "—"

    tipo = cfg["tipo"]
    ventana_6m = today.toordinal() - meses * 30
    pacientes, ingreso_total = [], 0.0
    sesiones_por_episodio = []
    atendidos_mes = 0  # pacientes distintos con ≥1 visita en el mes calendario actual
    n_atenciones = sum(len(d["fechas"]) for d in porpac.values())

    for pid, d in porpac.items():
        fechas = sorted(set(d["fechas"]))
        if not fechas:
            continue
        if any(f.year == today.year and f.month == today.month for f in fechas):
            atendidos_mes += 1
        last = fechas[-1]
        dias = (today - last).days
        plan = planes.get(pid)
        ingreso_total += d["monto"]
        _pend = _parse_pendientes((plan or {}).get("pendientes"))
        _meds = meds_por_pac.get(pid, []) if es_clinico else []
        _peso_ini = _meds[0] if _meds else None
        _peso_act = _meds[-1] if _meds else None
        _delta = round(_peso_act - _peso_ini, 1) if (_peso_ini is not None and _peso_act is not None) else None

        if tipo == "adherencia":
            episodios = _split_episodios(fechas, cfg["gap_nuevo"])
            for ep in episodios:
                sesiones_por_episodio.append(len(ep))
            sesiones = len(episodios[-1])
            estado, etiqueta = _estado_adherencia(dias, sesiones, plan, cfg)
            sp = (plan or {}).get("sesiones_plan", 0) or 0
            avance = (round(sesiones / sp * 100) if sp else None)
            n_eps = len(episodios)
        else:
            sesiones = len(fechas)
            estado, etiqueta = _estado_control(dias, plan, cfg)
            sp, avance, n_eps = 0, None, 1

        pacientes.append({
            "paciente_id": pid, "paciente": d["paciente"] or f"Paciente {pid}",
            "telefono": d["telefono"], "lugar": d["lugar"], "profesional": d["prof_last"],
            "sesiones": sesiones, "sesiones_plan": sp, "avance_pct": avance,
            "ultima_visita": last.isoformat(), "dias_sin_visita": dias,
            "n_episodios": n_eps, "estado": estado, "estado_label": etiqueta,
            "notas": (plan or {}).get("notas", ""),
            "resumen": (plan or {}).get("resumen", ""),
            "segmento": (plan or {}).get("segmento", "") or "",
            "pendientes": _pend,
            "n_pendientes": sum(1 for it in _pend if not it["hecho"]),
            "peso_meta": (plan or {}).get("peso_meta"),
            "objetivo": (plan or {}).get("objetivo", "") or "",
            "plan": (plan or {}).get("plan", "") or "",
            "peso_inicial": _peso_ini, "peso_actual": _peso_act, "delta_peso": _delta,
            "n_mediciones": len(_meds),
        })

    # Orden: del control/atención más reciente al de hace más tiempo
    # (menos días sin visita primero). Estado como desempate secundario.
    prio = {"vencido": 0, "riesgo": 0, "abandono": 1, "pronto": 1,
            "en_curso": 2, "al_dia": 2, "completado": 3, "alta": 4, "no_contactar": 5, "cerrado": 6}
    pacientes.sort(key=lambda p: (p["dias_sin_visita"], prio.get(p["estado"], 9)))

    acc = ACCIONABLES[tipo]
    accionables = [p for p in pacientes if p["estado"] in acc]
    activos = [p for p in pacientes if p["estado"] in ("en_curso", "riesgo", "al_dia", "pronto")]
    n_urgentes = len([p for p in pacientes if p["estado"] == acc[0]])

    # Ingreso recuperable ("valor en riesgo"): plata que se rescata si la recepción
    # contacta la worklist. Ticket promedio del programa × visitas que se recuperan.
    #   adherencia → cada paciente en riesgo retoma un plan; estimamos 3 sesiones más.
    #   control    → cada control vencido recuperado vale ~1 atención.
    ticket_prom = (ingreso_total / n_atenciones) if n_atenciones else 0
    n_acc = len(accionables)
    sesiones_rescate = 3 if tipo == "adherencia" else 1
    valor_recuperable = round(n_acc * ticket_prom * sesiones_rescate)

    if tipo == "adherencia":
        eps_validos = [n for n in sesiones_por_episodio if n > 0]
        prom = round(sum(eps_validos) / len(eps_validos), 1) if eps_validos else 0
        metric_label, metric_val = "Sesiones / episodio", prom
    else:
        metric_label, metric_val = "Pacientes en cartera", len(pacientes)

    clin = {"clinico": es_clinico}
    if es_clinico:
        kg_perdidos = 0.0
        n_seg = n_mej = 0
        for p in pacientes:
            pi, pa = p.get("peso_inicial"), p.get("peso_actual")
            if pi is None or pa is None or p["n_mediciones"] < 2:
                continue
            n_seg += 1
            if pi > pa:
                kg_perdidos += (pi - pa)
            meta = p.get("peso_meta")
            obj = (p.get("objetivo") or "").lower()
            if meta is not None:
                mejoro = abs(pa - meta) < abs(pi - meta)
            elif obj.startswith("subir"):
                mejoro = pa > pi
            else:
                mejoro = pa < pi
            if mejoro:
                n_mej += 1
        clin.update({"kg_perdidos": round(kg_perdidos, 1), "n_seguimiento": n_seg,
                     "n_mejoraron": n_mej, "pct_mejora": round(100 * n_mej / n_seg) if n_seg else 0})

    kpis = {
        "tipo": tipo, "activos": len(activos), "accionables": n_acc, "urgentes": n_urgentes,
        "metric_label": metric_label, "metric_val": metric_val,
        "ingreso": round(ingreso_total), "n_pacientes": len(pacientes),
        "ticket_prom": round(ticket_prom), "valor_recuperable": valor_recuperable,
        "atendidos_mes": atendidos_mes, **clin,
    }
    return {"kpis": kpis, "pacientes": pacientes, "cfg": cfg, "source_status": status}


def _compute_tamizaje(prog: str, meses: int = 6):
    """Cohorte poblacional por edad/sexo: a quién le toca un control preventivo
    que no tiene reciente. Distinto de _compute (tratamiento): no hay episodios,
    el 'estado' es lapsed (tuvo control, se atrasó) o nunca (sin registro)."""
    cfg = TAMIZAJE[prog]
    gen_filter = "AND p.genero = %s" if cfg["genero"] else ""
    esp_ph = ",".join(["%s"] * len(cfg["esp_recall"]))
    sql = f"""
        WITH ult AS (
            SELECT fa.paciente_id, MAX(fa.fecha) AS ultima
            FROM bi.fact_atenciones fa
            JOIN bi.dim_profesional dp ON dp.profesional_id = fa.profesional_id
            WHERE dp.especialidad_id IN ({esp_ph})
            GROUP BY fa.paciente_id
        )
        SELECT p.paciente_id,
               TRIM(COALESCE(p.nombre,'') || ' ' || COALESCE(p.apellido,'')) AS paciente,
               EXTRACT(YEAR FROM AGE(p.fecha_nacimiento))::int AS edad,
               p.telefono,
               COALESCE(NULLIF(TRIM(p.localidad),''), p.comuna, '') AS lugar,
               ult.ultima
        FROM bi.dim_paciente p
        LEFT JOIN ult ON ult.paciente_id = p.paciente_id
        WHERE p.fecha_nacimiento IS NOT NULL
          {gen_filter}
          AND EXTRACT(YEAR FROM AGE(p.fecha_nacimiento)) BETWEEN %s AND %s
          AND (ult.ultima IS NULL OR ult.ultima < CURRENT_DATE - make_interval(days => %s))
        ORDER BY ult.ultima DESC NULLS LAST
    """
    params: list = list(cfg["esp_recall"])
    if cfg["genero"]:
        params.append(cfg["genero"])
    params += [cfg["edad_min"], cfg["edad_max"], cfg["dias"]]
    rows, status = bi_query(sql, tuple(params))

    today = _today()
    pacientes = []
    for r in rows:
        ult = r.get("ultima")
        if isinstance(ult, str):
            try:
                ult = datetime.strptime(ult[:10], "%Y-%m-%d").date()
            except Exception:
                ult = None
        if ult:
            estado, etiqueta = "lapsed", "Control atrasado"
            dias = (today - ult).days
            ultima_txt = ult.isoformat()
        else:
            estado, etiqueta = "nunca", "Sin registro"
            dias = None
            ultima_txt = "—"
        pacientes.append({
            "paciente_id": r["paciente_id"], "paciente": r["paciente"] or f"Paciente {r['paciente_id']}",
            "telefono": r.get("telefono") or "", "lugar": r.get("lugar") or "",
            "edad": r.get("edad"), "sesiones": r.get("edad"), "sesiones_plan": 0, "avance_pct": None,
            "profesional": "—", "ultima_visita": ultima_txt,
            "dias_sin_visita": dias if dias is not None else 9999,
            "n_episodios": 0, "estado": estado, "estado_label": etiqueta, "notas": "", "resumen": "",
            "segmento": "", "pendientes": [], "n_pendientes": 0,
        })
    # lapsed primero (alta confianza), luego nunca; dentro, más atrasados primero
    pacientes.sort(key=lambda p: (0 if p["estado"] == "lapsed" else 1, -(p["dias_sin_visita"] or 0)))

    n_lapsed = sum(1 for p in pacientes if p["estado"] == "lapsed")
    n_nunca = sum(1 for p in pacientes if p["estado"] == "nunca")
    con_tel = sum(1 for p in pacientes if p["telefono"])
    kpis = {
        "tipo": "tamizaje", "activos": len(pacientes), "accionables": len(pacientes),
        "urgentes": n_lapsed, "metric_label": "Con teléfono", "metric_val": con_tel,
        "ingreso": 0, "n_pacientes": len(pacientes),
        "n_lapsed": n_lapsed, "n_nunca": n_nunca, "ticket_prom": 0, "valor_recuperable": 0,
    }
    cfg_out = {"label": cfg["label"], "tipo": "tamizaje", "objetivo": cfg["objetivo"], "wa": cfg["wa"]}
    return {"kpis": kpis, "pacientes": pacientes, "cfg": cfg_out, "source_status": status}


def _dispatch(prog: str, meses: int = 6, scope_prof: int | None = None):
    """Enruta al motor correcto: tamizaje (cohorte) o tratamiento (especialidad)."""
    if prog in TAMIZAJE:
        return _compute_tamizaje(prog, meses)
    return _compute(prog, meses, scope_prof)


def _primer_nombre(nombre: str) -> str:
    return (nombre or "").strip().split(" ")[0] if nombre else ""


def _wa_link(tel: str, mensaje: str) -> str | None:
    t = "".join(c for c in (tel or "") if c.isdigit())
    if not t:
        return None
    if len(t) == 9 and t.startswith("9"):
        t = "56" + t
    elif len(t) == 8:
        t = "569" + t
    from urllib.parse import quote
    return f"https://wa.me/{t}?text={quote(mensaje)}"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def listar_programas(token: str | None = Query(None),
                           cmc_session: str | None = Cookie(None),
                           request: Request = None):
    """Lista los programas disponibles (para el selector del módulo).
    Un perfil de profesional ve solo SU programa."""
    _tok, scope = _require_admin(request, token, cmc_session)
    progs = [
        {"key": k, "label": v["label"], "tipo": v["tipo"], "objetivo": v["objetivo"],
         "categoria": "tratamiento"}
        for k, v in PROGRAMAS.items()
    ]
    progs += [
        {"key": k, "label": v["label"], "tipo": "tamizaje", "objetivo": v["objetivo"],
         "categoria": "tamizaje"}
        for k, v in TAMIZAJE.items()
    ]
    if scope is not None:
        mine = _prog_for_prof(scope)
        progs = [p for p in progs if p["key"] == mine]
    return {"programas": progs}


@router.get("/{prog}/resumen")
async def resumen(prog: str, meses: int = Query(6, ge=1, le=36),
                  token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                  request: Request = None):
    _tok, scope = _require_admin(request, token, cmc_session)
    if scope is not None:
        prog = _prog_for_prof(scope) or prog
    data = _dispatch(prog, meses, scope)
    return {"kpis": data["kpis"], "cfg": {"label": data["cfg"]["label"], "tipo": data["cfg"]["tipo"],
            "objetivo": data["cfg"]["objetivo"], "clinico": bool(data["cfg"].get("clinico"))},
            "source_status": data["source_status"]}


@router.get("/{prog}/calendario")
async def calendario(prog: str, meses: int = Query(3, ge=1, le=12),
                     token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                     request: Request = None):
    """Días trabajados del programa (atenciones por día) para el popup de calendario.
    A diferencia de /api/profesional/dashboard (un profesional), esto agrega las
    atenciones del/los profesional(es) del programa — así recepción y dueño ven el
    calendario sin estar atados a un token de profesional. Solo programas de
    tratamiento (el tamizaje es cohorte poblacional, no tiene 'días trabajados')."""
    _tok, scope = _require_admin(request, token, cmc_session)
    if scope is not None:
        prog = _prog_for_prof(scope) or prog
    if prog not in PROGRAMAS:
        raise HTTPException(404, f"Programa de tratamiento '{prog}' no existe")
    cfg = _cfg(prog)
    rows, status = _bi_rows(cfg["especialidad_ids"], meses, scope)
    por_dia: dict[str, int] = {}
    for r in rows:
        f = r["fecha"]
        if isinstance(f, str):
            f = f[:10]
        else:
            try:
                f = f.isoformat()[:10]
            except Exception:
                continue
        por_dia[f] = por_dia.get(f, 0) + 1
    # Objetivo = promedio de atenciones por día efectivamente trabajado (para colorear
    # verde sobre objetivo / rojo bajo objetivo). Mínimo 1 para no dividir por cero.
    trabajados = [v for v in por_dia.values() if v > 0]
    objetivo = max(round(sum(trabajados) / len(trabajados)), 1) if trabajados else 1
    return {"por_dia": por_dia, "objetivo": objetivo,
            "label": cfg["label"], "source_status": status}


@router.get("/{prog}/pacientes")
async def pacientes(prog: str, estado: str | None = Query(None), meses: int = Query(6, ge=1, le=36),
                    token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                    request: Request = None):
    _tok, scope = _require_admin(request, token, cmc_session)
    if scope is not None:
        prog = _prog_for_prof(scope) or prog
    data = _dispatch(prog, meses, scope)
    tipo = data["cfg"]["tipo"]
    pac = data["pacientes"]
    if estado and estado != "todos":
        pac = [p for p in pac if p["estado"] == estado]
    conteos: dict[str, int] = {}
    for p in data["pacientes"]:
        conteos[p["estado"]] = conteos.get(p["estado"], 0) + 1
    total = len(pac)
    # Tamizaje son cohortes poblacionales (miles) → cap del payload; el conteo real
    # va en conteos/total. Tratamiento es chico, no se capa.
    if tipo == "tamizaje":
        pac = pac[:500]
    # adjuntar wa link agéntico a cada paciente accionable
    msg_tpl = data["cfg"]["wa"]
    acc = ACCIONABLES[tipo]
    for p in pac:
        if p["estado"] in acc:
            p["wa"] = _wa_link(p["telefono"], msg_tpl.format(primer=_primer_nombre(p["paciente"])))
        else:
            p["wa"] = None
    return {"pacientes": pac, "conteos": conteos, "total": total, "mostrados": len(pac),
            "tipo": tipo, "source_status": data["source_status"]}


@router.get("/{prog}/accion/{paciente_id}")
async def accion(prog: str, paciente_id: int, meses: int = Query(12, ge=1, le=36),
                 token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                 request: Request = None):
    """Next best action: redacta el mensaje WhatsApp personalizado para el paciente."""
    _tok, scope = _require_admin(request, token, cmc_session)
    if scope is not None:
        prog = _prog_for_prof(scope) or prog
    data = _dispatch(prog, meses, scope)
    p = next((x for x in data["pacientes"] if x["paciente_id"] == paciente_id), None)
    if not p:
        raise HTTPException(404, "Paciente no está en este programa")
    mensaje = data["cfg"]["wa"].format(primer=_primer_nombre(p["paciente"]))
    return {
        "paciente": p["paciente"], "telefono": p["telefono"], "estado": p["estado_label"],
        "dias_sin_visita": p["dias_sin_visita"], "mensaje": mensaje,
        "wa": _wa_link(p["telefono"], mensaje),
    }


@router.put("/{prog}/plan/{paciente_id}")
async def set_plan(prog: str, paciente_id: int, request: Request,
                   token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    _tok, scope = _require_admin(request, token, cmc_session)
    if scope is not None:
        prog = _prog_for_prof(scope) or prog  # un profesional solo edita planes de SU programa
    if prog not in PROGRAMAS and prog not in TAMIZAJE:
        raise HTTPException(404, f"Programa '{prog}' no existe")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    sesiones_plan = int(body.get("sesiones_plan") or 0)
    estado_manual = (body.get("estado_manual") or "").strip()
    if estado_manual not in ("", "alta", "pausa", "no_contactar"):
        estado_manual = ""
    notas = (body.get("notas") or "").strip()
    resumen = (body.get("resumen") or "").strip()
    plan = (body.get("plan") or "").strip()
    try:
        peso_meta = float(body["peso_meta"]) if body.get("peso_meta") not in (None, "", "null") else None
    except (ValueError, TypeError):
        peso_meta = None
    objetivo = (body.get("objetivo") or "").strip()[:60]
    ensure_programa_plan_table()
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            INSERT INTO programa_plan (programa, paciente_id, sesiones_plan, estado_manual, notas, resumen, plan, peso_meta, objetivo, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(programa, paciente_id) DO UPDATE SET
                sesiones_plan=excluded.sesiones_plan, estado_manual=excluded.estado_manual,
                notas=excluded.notas, resumen=excluded.resumen, plan=excluded.plan,
                peso_meta=excluded.peso_meta, objetivo=excluded.objetivo, updated_at=excluded.updated_at
        """, (prog, paciente_id, sesiones_plan, estado_manual, notas, resumen, plan, peso_meta, objetivo))
        conn.commit()
    return {"ok": True}


def _resolve_editable(request, token, cmc_session, prog: str) -> str:
    """Auth + resuelve el programa editable (un profesional solo toca el suyo) y
    valida que exista. Reutilizado por los endpoints de segmento/pendientes."""
    _tok, scope = _require_admin(request, token, cmc_session)
    if scope is not None:
        prog = _prog_for_prof(scope) or prog
    if prog not in PROGRAMAS and prog not in TAMIZAJE:
        raise HTTPException(404, f"Programa '{prog}' no existe")
    return prog


# ── Segmentación: categorías editables + asignación por paciente ──────────────

@router.get("/{prog}/segmentos")
async def listar_segmentos(prog: str, token: str | None = Query(None),
                           cmc_session: str | None = Cookie(None), request: Request = None):
    prog = _resolve_editable(request, token, cmc_session, prog)
    return {"categorias": _segmentos(prog)}


@router.post("/{prog}/segmentos")
async def crear_segmento(prog: str, request: Request, token: str | None = Query(None),
                         cmc_session: str | None = Cookie(None)):
    prog = _resolve_editable(request, token, cmc_session, prog)
    try:
        nombre = (await request.json()).get("nombre", "").strip()[:60]
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    if not nombre:
        raise HTTPException(400, "Nombre vacío")
    ensure_segmento_table()
    from session import _conn
    with _conn() as conn:
        nxt = conn.execute("SELECT COALESCE(MAX(orden),-1)+1 FROM programa_segmento WHERE programa=?", (prog,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO programa_segmento (programa, nombre, orden) VALUES (?,?,?)", (prog, nombre, nxt))
        conn.commit()
    return {"categorias": _segmentos(prog)}


@router.put("/{prog}/segmentos")
async def renombrar_segmento(prog: str, request: Request, token: str | None = Query(None),
                             cmc_session: str | None = Cookie(None)):
    """Renombra una categoría y arrastra a todos los pacientes que la tenían."""
    prog = _resolve_editable(request, token, cmc_session, prog)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    old = (body.get("old") or "").strip()
    new = (body.get("new") or "").strip()[:60]
    if not old or not new:
        raise HTTPException(400, "old/new requeridos")
    ensure_segmento_table(); ensure_programa_plan_table()
    from session import _conn
    with _conn() as conn:
        row = conn.execute("SELECT orden FROM programa_segmento WHERE programa=? AND nombre=?", (prog, old)).fetchone()
        if row is None:
            raise HTTPException(404, "Categoría no existe")
        if new != old:
            conn.execute("INSERT OR IGNORE INTO programa_segmento (programa, nombre, orden) VALUES (?,?,?)", (prog, new, row["orden"]))
            conn.execute("DELETE FROM programa_segmento WHERE programa=? AND nombre=?", (prog, old))
            conn.execute("UPDATE programa_plan SET segmento=? WHERE programa=? AND segmento=?", (new, prog, old))
        conn.commit()
    return {"categorias": _segmentos(prog)}


@router.delete("/{prog}/segmentos")
async def borrar_segmento(prog: str, nombre: str = Query(...), token: str | None = Query(None),
                          cmc_session: str | None = Cookie(None), request: Request = None):
    """Borra una categoría; los pacientes que la tenían quedan sin segmento."""
    prog = _resolve_editable(request, token, cmc_session, prog)
    ensure_segmento_table(); ensure_programa_plan_table()
    from session import _conn
    with _conn() as conn:
        conn.execute("DELETE FROM programa_segmento WHERE programa=? AND nombre=?", (prog, nombre))
        conn.execute("UPDATE programa_plan SET segmento='' WHERE programa=? AND segmento=?", (prog, nombre))
        conn.commit()
    return {"categorias": _segmentos(prog)}


@router.put("/{prog}/segmento/{paciente_id}")
async def set_segmento(prog: str, paciente_id: int, request: Request,
                       token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    """Asigna la categoría a un paciente (upsert liviano, no toca plan/notas/resumen)."""
    prog = _resolve_editable(request, token, cmc_session, prog)
    try:
        seg = (await request.json()).get("segmento", "").strip()[:60]
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ensure_programa_plan_table()
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            INSERT INTO programa_plan (programa, paciente_id, segmento, updated_at)
            VALUES (?,?,?, datetime('now'))
            ON CONFLICT(programa, paciente_id) DO UPDATE SET segmento=excluded.segmento, updated_at=excluded.updated_at
        """, (prog, paciente_id, seg))
        conn.commit()
    return {"ok": True, "segmento": seg}


@router.put("/{prog}/pendientes/{paciente_id}")
async def set_pendientes(prog: str, paciente_id: int, request: Request,
                         token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    """Reemplaza el checklist de pendientes del paciente (upsert liviano)."""
    prog = _resolve_editable(request, token, cmc_session, prog)
    try:
        items = (await request.json()).get("pendientes")
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    clean = _parse_pendientes(items)[:50]
    ensure_programa_plan_table()
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            INSERT INTO programa_plan (programa, paciente_id, pendientes, updated_at)
            VALUES (?,?,?, datetime('now'))
            ON CONFLICT(programa, paciente_id) DO UPDATE SET pendientes=excluded.pendientes, updated_at=excluded.updated_at
        """, (prog, paciente_id, json.dumps(clean, ensure_ascii=False)))
        conn.commit()
    return {"ok": True, "pendientes": clean, "n_pendientes": sum(1 for it in clean if not it["hecho"])}


# ── Capa clínica: mediciones de peso ──────────────────────────────────────────

@router.get("/{prog}/mediciones/{paciente_id}")
async def listar_mediciones(prog: str, paciente_id: int, token: str | None = Query(None),
                            cmc_session: str | None = Cookie(None), request: Request = None):
    prog = _resolve_editable(request, token, cmc_session, prog)
    return {"mediciones": _mediciones(prog, paciente_id)}


@router.post("/{prog}/mediciones/{paciente_id}")
async def agregar_medicion(prog: str, paciente_id: int, request: Request,
                           token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    prog = _resolve_editable(request, token, cmc_session, prog)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    fecha = (body.get("fecha") or _today().isoformat())[:10]
    def _num(k):
        try:
            return float(body[k]) if body.get(k) not in (None, "", "null") else None
        except (ValueError, TypeError):
            return None
    peso, cintura, grasa = _num("peso"), _num("cintura"), _num("grasa")
    if peso is None and cintura is None and grasa is None:
        raise HTTPException(400, "Ingresa al menos un valor (peso/cintura/grasa)")
    nota = (body.get("nota") or "").strip()[:200]
    ensure_medicion_table()
    from session import _conn
    with _conn() as conn:
        conn.execute(
            "INSERT INTO programa_medicion (programa, paciente_id, fecha, peso, cintura, grasa, nota) "
            "VALUES (?,?,?,?,?,?,?)", (prog, paciente_id, fecha, peso, cintura, grasa, nota))
        conn.commit()
    return {"ok": True, "mediciones": _mediciones(prog, paciente_id)}


@router.delete("/{prog}/mediciones/{paciente_id}/{medicion_id}")
async def borrar_medicion(prog: str, paciente_id: int, medicion_id: int, token: str | None = Query(None),
                          cmc_session: str | None = Cookie(None), request: Request = None):
    prog = _resolve_editable(request, token, cmc_session, prog)
    ensure_medicion_table()
    from session import _conn
    with _conn() as conn:
        conn.execute("DELETE FROM programa_medicion WHERE id=? AND programa=? AND paciente_id=?",
                     (medicion_id, prog, paciente_id))
        conn.commit()
    return {"ok": True, "mediciones": _mediciones(prog, paciente_id)}


# ── Capa clínica: objetivo (categorías editables, dim='objetivo') ─────────────

@router.get("/{prog}/objetivos")
async def listar_objetivos(prog: str, token: str | None = Query(None),
                           cmc_session: str | None = Cookie(None), request: Request = None):
    prog = _resolve_editable(request, token, cmc_session, prog)
    return {"categorias": _categorias(prog, "objetivo", DEFAULT_OBJETIVOS)}


@router.post("/{prog}/objetivos")
async def crear_objetivo(prog: str, request: Request, token: str | None = Query(None),
                         cmc_session: str | None = Cookie(None)):
    prog = _resolve_editable(request, token, cmc_session, prog)
    try:
        nombre = (await request.json()).get("nombre", "").strip()[:60]
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    if not nombre:
        raise HTTPException(400, "Nombre vacío")
    ensure_categoria_table()
    from session import _conn
    with _conn() as conn:
        nxt = conn.execute("SELECT COALESCE(MAX(orden),-1)+1 FROM programa_categoria WHERE programa=? AND dim='objetivo'", (prog,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO programa_categoria (programa, dim, nombre, orden) VALUES (?,?,?,?)", (prog, "objetivo", nombre, nxt))
        conn.commit()
    return {"categorias": _categorias(prog, "objetivo", DEFAULT_OBJETIVOS)}


@router.put("/{prog}/objetivos")
async def renombrar_objetivo(prog: str, request: Request, token: str | None = Query(None),
                             cmc_session: str | None = Cookie(None)):
    prog = _resolve_editable(request, token, cmc_session, prog)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    old = (body.get("old") or "").strip()
    new = (body.get("new") or "").strip()[:60]
    if not old or not new:
        raise HTTPException(400, "old/new requeridos")
    ensure_categoria_table(); ensure_programa_plan_table()
    from session import _conn
    with _conn() as conn:
        row = conn.execute("SELECT orden FROM programa_categoria WHERE programa=? AND dim='objetivo' AND nombre=?", (prog, old)).fetchone()
        if row is None:
            raise HTTPException(404, "Categoría no existe")
        if new != old:
            conn.execute("INSERT OR IGNORE INTO programa_categoria (programa, dim, nombre, orden) VALUES (?,?,?,?)", (prog, "objetivo", new, row["orden"]))
            conn.execute("DELETE FROM programa_categoria WHERE programa=? AND dim='objetivo' AND nombre=?", (prog, old))
            conn.execute("UPDATE programa_plan SET objetivo=? WHERE programa=? AND objetivo=?", (new, prog, old))
        conn.commit()
    return {"categorias": _categorias(prog, "objetivo", DEFAULT_OBJETIVOS)}


@router.delete("/{prog}/objetivos")
async def borrar_objetivo(prog: str, nombre: str = Query(...), token: str | None = Query(None),
                          cmc_session: str | None = Cookie(None), request: Request = None):
    prog = _resolve_editable(request, token, cmc_session, prog)
    ensure_categoria_table(); ensure_programa_plan_table()
    from session import _conn
    with _conn() as conn:
        conn.execute("DELETE FROM programa_categoria WHERE programa=? AND dim='objetivo' AND nombre=?", (prog, nombre))
        conn.execute("UPDATE programa_plan SET objetivo='' WHERE programa=? AND objetivo=?", (prog, nombre))
        conn.commit()
    return {"categorias": _categorias(prog, "objetivo", DEFAULT_OBJETIVOS)}


@router.put("/{prog}/objetivo/{paciente_id}")
async def set_objetivo(prog: str, paciente_id: int, request: Request,
                       token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    """Asigna el objetivo clínico a un paciente (upsert liviano, no toca el resto del plan)."""
    prog = _resolve_editable(request, token, cmc_session, prog)
    try:
        obj = (await request.json()).get("objetivo", "").strip()[:60]
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ensure_programa_plan_table()
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            INSERT INTO programa_plan (programa, paciente_id, objetivo, updated_at)
            VALUES (?,?,?, datetime('now'))
            ON CONFLICT(programa, paciente_id) DO UPDATE SET objetivo=excluded.objetivo, updated_at=excluded.updated_at
        """, (prog, paciente_id, obj))
        conn.commit()
    return {"ok": True, "objetivo": obj}


def build_digest(meses: int = 6) -> dict:
    """Lista de hoy UNIFICADA: pacientes a contactar cruzando TODOS los programas
    de este motor + los módulos dedicados Kine y Ortodoncia. Priorizada.

    Reutilizable por el endpoint /digest y por el Copilot Alma (tool de lectura),
    para que el negocio tenga una sola cola de reenganche accionable."""
    items: list[dict] = []
    status = "ok"

    for prog, cfg in PROGRAMAS.items():
        data = _compute(prog, meses)
        if data["source_status"] != "ok":
            status = data["source_status"]
        acc = ACCIONABLES[cfg["tipo"]]
        for p in data["pacientes"]:
            if p["estado"] in acc:
                items.append({
                    "programa": prog, "programa_label": cfg["label"],
                    "paciente": p["paciente"], "paciente_id": p["paciente_id"],
                    "telefono": p["telefono"], "estado": p["estado_label"], "estado_key": p["estado"],
                    "dias_sin_visita": p["dias_sin_visita"],
                    "wa": _wa_link(p["telefono"], cfg["wa"].format(primer=_primer_nombre(p["paciente"]))),
                })

    # Sumar los módulos dedicados Kine + Ortodoncia (misma lógica, otra implementación)
    for mod_name, prog_label, accionables, msg in (
        ("kine_routes", "Kinesiología", ("riesgo", "abandono"),
         "Hola {primer}, te saludamos del Centro Médico Carampangue. ¿Agendamos tu próxima sesión de kinesiología para no perder el avance?"),
        ("ortodoncia_routes", "Ortodoncia", ("vencido", "pronto"),
         "Hola {primer}, te saludamos del Centro Médico Carampangue. Corresponde tu control de ortodoncia. ¿Coordinamos tu hora?"),
    ):
        try:
            mod = __import__(mod_name)
            data = mod._compute(meses)
            if data.get("source_status") != "ok":
                status = data.get("source_status", status)
            for p in data["pacientes"]:
                if p["estado"] in accionables:
                    dias = p.get("dias_sin_sesion", p.get("dias_sin_control", 0))
                    items.append({
                        "programa": mod_name.replace("_routes", ""), "programa_label": prog_label,
                        "paciente": p["paciente"], "paciente_id": p["paciente_id"],
                        "telefono": p.get("telefono", ""), "estado": p["estado_label"],
                        "estado_key": p["estado"], "dias_sin_visita": dias,
                        "wa": _wa_link(p.get("telefono", ""), msg.format(primer=_primer_nombre(p["paciente"]))),
                    })
        except Exception as e:  # noqa: BLE001 — un módulo caído no debe romper el digest
            log.warning("digest: módulo %s falló: %s", mod_name, e)

    urgent_keys = {"riesgo", "vencido"}
    items.sort(key=lambda x: (0 if x["estado_key"] in urgent_keys else 1, -x["dias_sin_visita"]))
    por_programa: dict[str, int] = {}
    for it in items:
        por_programa[it["programa_label"]] = por_programa.get(it["programa_label"], 0) + 1
    return {"items": items, "total": len(items), "por_programa": por_programa, "source_status": status}


@router.get("/_/digest")
async def digest(meses: int = Query(6, ge=1, le=12),
                 token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                 request: Request = None):
    """Lista de hoy: top pacientes a contactar cruzando TODOS los programas + Kine + Ortodoncia."""
    _tok, scope = _require_admin(request, token, cmc_session)
    if scope is not None:
        raise HTTPException(403, "Vista cruzada no disponible para perfil de profesional")
    d = build_digest(meses)
    d["items"] = d["items"][:200]
    return d


def build_overview(meses: int = 6) -> dict:
    """Vista portfolio: cada programa de tratamiento (los 11 + Kine + Ortodoncia)
    resumido en una fila — accionables, urgentes e ingreso recuperable — ranqueado
    por la plata en juego. El tablero ejecutivo: dónde está la mayor oportunidad de
    reenganche HOY. (Tamizaje no entra: es población, no recurrencia de ingreso.)"""
    filas, status = [], "ok"
    tot_acc = tot_urg = 0
    tot_valor = 0

    for prog, cfg in PROGRAMAS.items():
        data = _compute(prog, meses)
        if data["source_status"] != "ok":
            status = data["source_status"]
        k = data["kpis"]
        filas.append({
            "programa": prog, "label": cfg["label"], "tipo": cfg["tipo"],
            "activos": k["activos"], "accionables": k["accionables"], "urgentes": k["urgentes"],
            "valor_recuperable": k["valor_recuperable"], "n_pacientes": k["n_pacientes"],
        })
        tot_acc += k["accionables"]; tot_urg += k["urgentes"]; tot_valor += k["valor_recuperable"]

    for mod_name, label, tipo in (("kine_routes", "Kinesiología", "adherencia"),
                                  ("ortodoncia_routes", "Ortodoncia", "control")):
        try:
            mod = __import__(mod_name)
            data = mod._compute(meses)
            if data.get("source_status") != "ok":
                status = data.get("source_status", status)
            pac = data["pacientes"]
            k = data.get("kpis", {})
            acc_keys = ("riesgo", "abandono") if tipo == "adherencia" else ("vencido", "pronto")
            urg_key = acc_keys[0]
            accionables = [p for p in pac if p["estado"] in acc_keys]
            urgentes = [p for p in pac if p["estado"] == urg_key]
            activos = [p for p in pac if p["estado"] in ("en_curso", "riesgo", "al_dia", "pronto")]
            # ingreso recuperable ≈ accionables × ingreso promedio por paciente (6m).
            ingreso = k.get("ingreso_programa") or k.get("ingreso_orto") or 0
            n_pac = max(k.get("n_pacientes", len(pac)), 1)
            valor = round(len(accionables) * (ingreso / n_pac))
            filas.append({
                "programa": mod_name.replace("_routes", ""), "label": label, "tipo": tipo,
                "activos": len(activos), "accionables": len(accionables), "urgentes": len(urgentes),
                "valor_recuperable": valor, "n_pacientes": len(pac),
            })
            tot_acc += len(accionables); tot_urg += len(urgentes); tot_valor += valor
        except Exception as e:  # noqa: BLE001
            log.warning("overview: módulo %s falló: %s", mod_name, e)

    filas.sort(key=lambda f: -f["valor_recuperable"])
    return {"filas": filas, "totales": {"accionables": tot_acc, "urgentes": tot_urg,
            "valor_recuperable": tot_valor, "n_programas": len(filas)}, "source_status": status}


@router.get("/_/overview")
async def overview(meses: int = Query(6, ge=1, le=12),
                   token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                   request: Request = None):
    """Portfolio de programas: todos de un vistazo, ranqueados por ingreso recuperable."""
    _tok, scope = _require_admin(request, token, cmc_session)
    if scope is not None:
        raise HTTPException(403, "Vista portfolio no disponible para perfil de profesional")
    return build_overview(meses)


@router.get("/{prog}/export")
async def export_csv(prog: str, meses: int = Query(6, ge=1, le=36),
                     token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                     request: Request = None):
    _tok, scope = _require_admin(request, token, cmc_session)
    if scope is not None:
        prog = _prog_for_prof(scope) or prog
    data = _dispatch(prog, meses, scope)
    tipo = data["cfg"]["tipo"]
    acc = ACCIONABLES.get(tipo, ())
    msg_tpl = data["cfg"]["wa"]
    buf = io.StringIO()
    w = csv.writer(buf)
    # Incluye el mensaje sugerido + link wa.me para los accionables → recepción
    # baja la lista y contacta uno por uno (la worklist como sesión de trabajo).
    w.writerow(["Paciente", "Segmento", "Teléfono", "Lugar", "Profesional", "Visitas/Sesiones",
                "Última visita", "Días sin visita", "Estado", "Pendientes abiertos",
                "Mensaje sugerido", "Link WhatsApp"])
    for p in data["pacientes"]:
        if p["estado"] in acc:
            mensaje = msg_tpl.format(primer=_primer_nombre(p["paciente"]))
            wa = _wa_link(p["telefono"], mensaje) or ""
        else:
            mensaje, wa = "", ""
        w.writerow([p["paciente"], p.get("segmento", ""), p["telefono"], p["lugar"], p["profesional"],
                    p["sesiones"], p["ultima_visita"], p["dias_sin_visita"], p["estado_label"],
                    p.get("n_pendientes", 0), mensaje, wa])
    buf.seek(0)
    fname = f"programa_{prog}_{_today().isoformat()}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})
