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
    # Con el 2º tecnólogo, se sobrecupea EN CADA atención de David → tope alto
    # (≈ una jornada completa). Ajustable por env.
    try:
        return int(os.getenv("SOBRECUPO_MAX_DIA", "30"))
    except (TypeError, ValueError):
        return 30


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
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sobrecupos)").fetchall()]
        if cols and "capa" not in cols:
            # Esquema viejo (sin capa, UNIQUE por hora) → recrear para permitir 2 capas
            # por hora. La tabla es solo cap-tracking; las citas reales viven en Medilink.
            conn.execute("DROP TABLE sobrecupos")
            cols = []
        if not cols:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sobrecupos (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    id_profesional INTEGER,
                    fecha         TEXT,
                    hora_inicio   TEXT,
                    capa          INTEGER DEFAULT 2,
                    phone         TEXT,
                    rut           TEXT,
                    id_cita       TEXT,
                    creado_ts     INTEGER,
                    UNIQUE(id_profesional, fecha, hora_inicio, capa)
                )
            """)


def count_dia(id_profesional: int, fecha: str, capa: int | None = None) -> int:
    """Sobrecupos creados ese día (total, o de una capa si se pasa)."""
    _ensure_table()
    from session import _conn
    with _conn() as conn:
        if capa is None:
            r = conn.execute("SELECT COUNT(*) FROM sobrecupos WHERE id_profesional=? AND fecha=?",
                             (id_profesional, fecha)).fetchone()
        else:
            r = conn.execute("SELECT COUNT(*) FROM sobrecupos WHERE id_profesional=? AND fecha=? AND capa=?",
                             (id_profesional, fecha, capa)).fetchone()
    return (r[0] if r else 0) or 0


def _ocupados(id_profesional: int, fecha: str) -> set:
    """Set de (hora_inicio, capa) ya creados ese día."""
    _ensure_table()
    from session import _conn
    with _conn() as conn:
        rows = conn.execute("SELECT hora_inicio, capa FROM sobrecupos WHERE id_profesional=? AND fecha=?",
                           (id_profesional, fecha)).fetchall()
    return {(r[0], r[1]) for r in rows}


def registrar(id_profesional: int, fecha: str, hora_inicio: str, capa: int = 2,
              phone: str = "", rut: str = "", id_cita: str = "") -> None:
    """Anota un sobrecupo creado (para respetar el tope diario)."""
    _ensure_table()
    import time as _t
    from session import _conn
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sobrecupos "
                "(id_profesional, fecha, hora_inicio, capa, phone, rut, id_cita, creado_ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (id_profesional, fecha, hora_inicio, int(capa), phone, rut, str(id_cita), int(_t.time())))
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
    cad3 = _cadencia_min()                  # capa 3: "por medio" (default 30 min)
    tope = _max_dia()

    # MODELO DE 3 CAPAS (David viene con un 2º tecnólogo médico → atienden en paralelo):
    #   Capa 1 = slots normales de David (la maneja el flujo normal del bot).
    #   Capa 2 = PRIMER sobrecupo: uno EN CADA ATENCIÓN de David (15min: 10:00,10:15,...).
    #   Capa 3 = SEGUNDO sobrecupo: "hora por medio" (30min: 10:00,10:30,...), capa extra.
    # Prioridad de oferta: capa 2 antes que capa 3 (el bot toma las primeras del listado).
    for d in range(1, dias_horizonte + 1):
        dia = hoy + timedelta(days=d)
        fecha = dia.strftime("%Y-%m-%d")
        try:
            ocup = await M._get_horas_ocupadas(client, id_prof, fecha)
        except Exception:  # noqa: BLE001 — Medilink puede fallar; probar siguiente día
            continue
        if not ocup:
            continue                       # no trabaja / sin citas ese día → no sobrecupear
        if count_dia(id_prof, fecha) >= tope:
            continue
        usados = _ocupados(id_prof, fecha)  # set de (hora, capa)
        try:
            ocup_min = {M._h_to_min(h) for h in ocup}
        except Exception:  # noqa: BLE001
            continue
        t0, t1 = min(ocup_min), max(ocup_min)

        def _slot(t, capa):
            return {
                "profesional": nombre, "id_profesional": id_prof,
                "especialidad": esp_disp, "fecha": fecha,
                "hora_inicio": _min_to_h(t), "hora_fin": _min_to_h(t + intervalo),
                "sobrecupo": True, "capa": capa,
            }

        capa2: list[dict] = []  # uno por atención (15 min)
        capa3: list[dict] = []  # por medio (30 min)
        n2 = count_dia(id_prof, fecha, 2)
        n3 = count_dia(id_prof, fecha, 3)
        # Capa 2: recorre al intervalo de David; ofrece donde tiene paciente.
        t = t0
        while t <= t1 and (n2 + len(capa2)) < tope:
            if any((t <= mm < t + intervalo) for mm in ocup_min) and (_min_to_h(t), 2) not in usados:
                capa2.append(_slot(t, 2))
            t += intervalo
        # Capa 3: "por medio" a cad3 (30 min) a lo largo de la jornada.
        t = t0
        while t <= t1 and (n3 + len(capa3)) < tope:
            if (_min_to_h(t), 3) not in usados:
                capa3.append(_slot(t, 3))
            t += cad3
        # Mostrar capa 2 Y capa 3 JUNTAS, ordenadas por hora (no capa2 en bloque). Así
        # el paciente ve todos los horarios disponibles cronológicos; cuando capa 2 de
        # una hora ya está tomada, esa misma hora sigue ofreciéndose vía capa 3. Dedup
        # por hora (capa 2 gana en la asignación, por eso va primero en el merge).
        by_hora: dict = {}
        for s in capa2 + capa3:             # capa 2 primero → gana el dedup por hora
            by_hora.setdefault(s["hora_inicio"], s)
        out = sorted(by_hora.values(), key=lambda s: s["hora_inicio"])
        if out:
            return out                      # primer día con sobrecupos disponibles
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
    capa = int(slot.get("capa", 2) or 2)
    if not all([id_prof, fecha, hora_inicio, hora_fin]):
        return None
    if count_dia(id_prof, fecha) >= _max_dia():
        log.info("[sobrecupo] tope diario alcanzado prof=%s fecha=%s", id_prof, fecha)
        return None
    import medilink as M
    _marca = f"[SOBRECUPO C{capa}]"          # C2 = primer sobrecupo, C3 = segundo (por medio)
    try:
        cita = await M.crear_cita(
            id_paciente=id_paciente, id_profesional=id_prof, fecha=fecha,
            hora_inicio=hora_inicio, hora_fin=hora_fin, modalidad=modalidad,
            observaciones_extra=_marca,
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
        registrar(id_prof, fecha, hora_inicio, capa=capa, phone=phone, rut=rut, id_cita=id_cita)
        log.info("[sobrecupo] creado C%s prof=%s %s %s id_cita=%s", capa, id_prof, fecha, hora_inicio, id_cita)
    return cita
