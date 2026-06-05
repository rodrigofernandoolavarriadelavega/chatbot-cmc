"""Sobrecupos — el bot ofrece horas extra (doble-cupo) en especialidades de alta
demanda donde el CMC sobrecupea, para no perder pacientes que llegan por publicidad
cuando la agenda formal está lejos.

Idea (definida por el dueño): para ECOGRAFÍA, ofrecer horas "por medio" — una cadencia
más amplia que la grilla normal de David (15 min) → ej. 10:00, 10:30, 11:00… — que
doblan sus citas existentes, hasta un tope diario. Cada sobrecupo se crea en Medilink
marcado `[SOBRECUPO]` para que recepción lo vea claro.

SEGURIDAD: gateado por SOBRECUPO_ENABLED (OFF por defecto → el bot se comporta igual
que hoy). Allowlist de especialidades. Tope duro por día por profesional. Crea citas
REALES en Medilink prod sólo cuando el flujo confirma y el flag está ON.
"""
import logging
import os
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/Santiago")
except Exception:  # noqa: BLE001
    _TZ = None

log = logging.getLogger("bot")


def _enabled() -> bool:
    return os.getenv("SOBRECUPO_ENABLED", "false").lower() == "true"


def _especialidades() -> set:
    raw = os.getenv("SOBRECUPO_ESPECIALIDADES", "ecografía,ecografia")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _max_dia() -> int:
    try:
        return int(os.getenv("SOBRECUPO_MAX_DIA", "4"))
    except (TypeError, ValueError):
        return 4


def _cadencia_min() -> int:
    try:
        return int(os.getenv("SOBRECUPO_CADENCIA_MIN", "30"))
    except (TypeError, ValueError):
        return 30


def _min_to_h(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _ensure_table() -> None:
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sobrecupos (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                id_profesional INTEGER,
                fecha         TEXT,
                hora_inicio   TEXT,
                phone         TEXT,
                rut           TEXT,
                id_cita       TEXT,
                creado_ts     INTEGER,
                UNIQUE(id_profesional, fecha, hora_inicio)
            )
        """)


def count_dia(id_profesional: int, fecha: str) -> int:
    _ensure_table()
    from session import _conn
    with _conn() as conn:
        r = conn.execute("SELECT COUNT(*) FROM sobrecupos WHERE id_profesional=? AND fecha=?",
                         (id_profesional, fecha)).fetchone()
    return (r[0] if r else 0) or 0


def _ocupados(id_profesional: int, fecha: str) -> set:
    _ensure_table()
    from session import _conn
    with _conn() as conn:
        rows = conn.execute("SELECT hora_inicio FROM sobrecupos WHERE id_profesional=? AND fecha=?",
                           (id_profesional, fecha)).fetchall()
    return {r[0] for r in rows}


def registrar(id_profesional: int, fecha: str, hora_inicio: str,
              phone: str = "", rut: str = "", id_cita: str = "") -> None:
    """Anota un sobrecupo creado (para respetar el tope diario)."""
    _ensure_table()
    import time as _t
    from session import _conn
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sobrecupos "
                "(id_profesional, fecha, hora_inicio, phone, rut, id_cita, creado_ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (id_profesional, fecha, hora_inicio, phone, rut, str(id_cita), int(_t.time())))
    except Exception as e:  # noqa: BLE001
        log.warning("sobrecupo registrar falló: %s", e)


async def generar_slots(especialidad: str, dias_horizonte: int = 10) -> list[dict]:
    """Slots de SOBRECUPO ofrecibles para la especialidad (vacío si el flag está OFF
    o la especialidad no está en la allowlist). Genera horas 'por medio' en el próximo
    día laboral del profesional, hasta el tope diario, restando los ya creados."""
    if not _enabled() or (especialidad or "").lower() not in _especialidades():
        return []
    import medilink as M
    ids = M._ids_para_especialidad(especialidad)
    if not ids:
        return []
    id_prof = ids[0]                       # eco → David Pardo (68)
    client = M._get_shared_client()
    intervalo = M.PROFESIONALES.get(id_prof, {}).get("intervalo", 15)
    nombre = M.PROFESIONALES.get(id_prof, {}).get("nombre", "el profesional")
    esp_disp = M.PROFESIONALES.get(id_prof, {}).get("especialidad", especialidad)

    hoy = datetime.now(_TZ).date() if _TZ else datetime.utcnow().date()
    cad = _cadencia_min()
    tope = _max_dia()

    # Busca el próximo día con CITAS (= día que David trabaja y hay demanda). Ofrece
    # sobrecupos "por medio" a `cad` min a lo largo de su jornada de ese día, doblando
    # su grilla. Salta el día de hoy (no sobrecupear para hoy mismo).
    for d in range(1, dias_horizonte + 1):
        dia = hoy + timedelta(days=d)
        fecha = dia.strftime("%Y-%m-%d")
        try:
            ocup = await M._get_horas_ocupadas(client, id_prof, fecha)
        except Exception:  # noqa: BLE001 — Medilink puede fallar; probar siguiente día
            continue
        if not ocup:
            continue                       # no trabaja / sin citas ese día → no sobrecupear
        ya = count_dia(id_prof, fecha)
        if ya >= tope:
            continue
        usados = _ocupados(id_prof, fecha)
        try:
            mins = sorted(M._h_to_min(h) for h in ocup)
        except Exception:  # noqa: BLE001
            continue
        t0, t1 = mins[0], mins[-1]         # span de su jornada (primera→última cita)
        out: list[dict] = []
        t = t0
        while t <= t1 and (ya + len(out)) < tope:
            hora = _min_to_h(t)
            if hora not in usados:
                out.append({
                    "profesional": nombre,
                    "id_profesional": id_prof,
                    "especialidad": esp_disp,
                    "fecha": fecha,
                    "hora_inicio": hora,
                    "hora_fin": _min_to_h(t + intervalo),
                    "sobrecupo": True,
                })
            t += cad
        if out:
            return out                     # primer día con sobrecupos disponibles
    return []


async def crear_sobrecupo(slot: dict, id_paciente: int, *, phone: str = "",
                          rut: str = "", modalidad: str = "PRESENCIAL") -> dict | None:
    """Crea la cita de sobrecupo en Medilink marcada [SOBRECUPO] y la registra para
    el tope diario. Doble-chequea el gate y el tope (defensa). Devuelve el dict de la
    cita creada o None. Crea cita REAL en prod sólo con SOBRECUPO_ENABLED=true."""
    if not _enabled():
        log.info("[sobrecupo] crear bloqueado (SOBRECUPO_ENABLED=false)")
        return None
    id_prof = slot.get("id_profesional")
    fecha = slot.get("fecha")
    hora_inicio = slot.get("hora_inicio")
    hora_fin = slot.get("hora_fin")
    if not all([id_prof, fecha, hora_inicio, hora_fin]):
        return None
    if count_dia(id_prof, fecha) >= _max_dia():
        log.info("[sobrecupo] tope diario alcanzado prof=%s fecha=%s", id_prof, fecha)
        return None
    import medilink as M
    try:
        cita = await M.crear_cita(
            id_paciente=id_paciente, id_profesional=id_prof, fecha=fecha,
            hora_inicio=hora_inicio, hora_fin=hora_fin, modalidad=modalidad,
            observaciones_extra="[SOBRECUPO]",
        )
    except TypeError:
        # crear_cita sin soporte de observaciones_extra → crear igual y marcar luego no aplica
        cita = await M.crear_cita(
            id_paciente=id_paciente, id_profesional=id_prof, fecha=fecha,
            hora_inicio=hora_inicio, hora_fin=hora_fin, modalidad=modalidad)
    except Exception as e:  # noqa: BLE001
        log.error("[sobrecupo] crear_cita falló: %s", e)
        return None
    if cita:
        id_cita = cita.get("id") or cita.get("id_cita") or ""
        registrar(id_prof, fecha, hora_inicio, phone=phone, rut=rut, id_cita=id_cita)
        log.info("[sobrecupo] creado prof=%s %s %s id_cita=%s", id_prof, fecha, hora_inicio, id_cita)
    return cita
