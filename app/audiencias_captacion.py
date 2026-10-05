"""Audiencias de CAPTACIÓN en Meta — exclusión de pacientes actuales y semilla
de audiencia similar.

Dos Custom Audiences (teléfono hasheado SHA-256, mismo hashing y mismo
mecanismo de `custom_audiences_sync`):

  1. "CMC Exclusion captacion 24m" — personas con pago en caja en los últimos
     24 meses. Se EXCLUYE en los ad sets de captación: la venta atribuida a
     anuncios incluía pacientes que ya eran del centro.
  2. "CMC Semilla similar 18m" — las personas de mayor margen para el centro
     en 18 meses. Semilla de la audiencia similar 1% Chile. Margen =
     monto x (1 - pct_honorario/100); `equipo_cmc.pct_honorario` es lo del
     PROFESIONAL (se reusa `campanas_meta_routes._pct_honorarios`).

GATEADO OFF por `META_AUDIENCIAS_CAPTACION_ACTIVE` (default false). Con el flag
apagado NO se hace ninguna llamada a Meta (ni lectura). Análisis legal y pasos
en Ads Manager: docs/AUDIENCIAS_CAPTACION_2026-10.md.

Privacidad:
  - Nombre y descripción de las audiencias NO revelan especialidad, diagnóstico
    ni la palabra "paciente" (ver `_TERMINOS_PROHIBIDOS`, verificado en tests).
  - Solo viaja el hash del teléfono. Nunca nombre, RUT, monto ni profesional.
  - Un teléfono entra solo si pasa TODOS los filtros de consentimiento/baja que
    ya usa el resto del sistema (ver `_filtro_telefono`).
  - Si no se puede leer la lista de bajas (BI caído) NO se envía nada.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("bot")

# ── Definición ────────────────────────────────────────────────────────────────
NOMBRE_EXCLUSION = "CMC Exclusion captacion 24m"
NOMBRE_SEMILLA = "CMC Semilla similar 18m"
NOMBRE_SIMILAR = "CMC Similar 1% Chile (semilla 18m)"
DESCRIPCION_EXCLUSION = "Lista de exclusion para campanas de captacion."
DESCRIPCION_SEMILLA = "Semilla para audiencia similar 1% Chile."
DESCRIPCION_SIMILAR = "Audiencia similar 1% Chile."

EXCLUSION_MESES = 24
SEMILLA_MESES = 18
# Semilla = este tramo superior del margen entre las personas elegibles.
SEMILLA_FRACCION_TOP = 0.25
# Meta exige >=100 coincidencias para crear una audiencia similar.
MIN_SEMILLA_LOOKALIKE = 100
PCT_HONORARIO_DEFAULT = 70
EDAD_MINIMA = 18

# Texto que jamás puede aparecer en nombre/descripción de una audiencia.
_TERMINOS_PROHIBIDOS = (
    "paciente", "psiquiatr", "psicolog", "salud mental", "ginecolog", "matrona",
    "ortodon", "odontolog", "dental", "kinesio", "nutri", "diabet", "cardio",
    "neurolog", "gastro", "otorrino", "fono", "eco", "podolog", "traumato",
    "diagnostic", "enfermedad", "tratamiento", "depresion", "ansiedad",
    "embarazo", "vih", "cancer",
)


# ── Teléfonos ─────────────────────────────────────────────────────────────────
def _clave(telefono) -> str | None:
    """Clave de persona = últimos 9 dígitos de un celular chileno válido
    (569XXXXXXXX tras normalizar). None si no es un celular utilizable."""
    from custom_audiences_sync import _normalize_phone
    n = _normalize_phone(str(telefono or ""))
    if not n or len(n) != 11 or not n.startswith("569"):
        return None
    return n[-9:]


def _rut_norm(rut) -> str:
    return re.sub(r"[^0-9Kk]", "", str(rut or "")).upper()


def _edad(nacimiento, hoy: date) -> int | None:
    try:
        d = datetime.strptime(str(nacimiento)[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    return hoy.year - d.year - ((hoy.month, hoy.day) < (d.month, d.day))


def _es_menor(nacimiento, hoy: date) -> bool:
    e = _edad(nacimiento, hoy)
    return e is not None and e < EDAD_MINIMA


def _hace_meses(hoy: date, meses: int) -> str:
    return (hoy - timedelta(days=round(meses * 30.4375))).isoformat()


# ── Listas de baja y consentimiento ──────────────────────────────────────────
def cargar_bi_sets() -> dict | None:
    """(opt_out, consent_ok, consent_no) como sets de clave-9 desde BI Postgres.
    None si BI no responde: sin la lista de bajas no se puede enviar nada."""
    import custom_audiences_sync as cas
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=cas._BI_HOST, port=cas._BI_PORT, dbname=cas._BI_NAME,
            user=cas._BI_USER, password=cas._BI_PASSWORD, connect_timeout=10,
        )
    except Exception as e:
        log.error("audiencias_captacion: BI no disponible: %s", e)
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT phone FROM bi.opt_outs_marketing")
            opt_out = {k for (p,) in cur.fetchall() if (k := _clave(p))}
            cur.execute("SELECT phone, status FROM bi.marketing_consent")
            ok, no = set(), set()
            for p, st in cur.fetchall():
                k = _clave(p)
                if not k:
                    continue
                if st == "accepted":
                    ok.add(k)
                elif st == "declined":
                    no.add(k)
        return {"opt_out": opt_out, "consent_ok": ok, "consent_no": no}
    except Exception as e:
        log.error("audiencias_captacion: error leyendo consentimiento BI: %s", e)
        return None
    finally:
        conn.close()


def _bajas_locales(c) -> tuple[set[str], set[str]]:
    """(claves de baja, ruts con derecho al olvido ejercido).
    Mismas capas que `session.get_email_consent_sets` + derecho al olvido."""
    bajas: set[str] = set()
    for (p,) in c.execute("SELECT phone FROM contact_tags WHERE tag='marketing_opt_out'"):
        if k := _clave(p):
            bajas.add(k)
    try:
        for p, st, rev in c.execute("SELECT phone, status, revoked_at FROM privacy_consents"):
            if (rev or st == "declined") and (k := _clave(p)):
                bajas.add(k)
    except Exception:
        pass
    olvido_ruts: set[str] = set()
    try:
        for rut, p in c.execute("SELECT rut, phone FROM gdpr_deletions"):
            if r := _rut_norm(rut):
                olvido_ruts.add(r)
            if k := _clave(p):
                bajas.add(k)
    except Exception:
        pass
    return bajas, olvido_ruts


# ── Construcción de listas ───────────────────────────────────────────────────
def _vinculos(c, h) -> tuple[dict[int, dict], dict[str, set[int]], dict[str, str]]:
    """pacientes {id: {rut, nombre, nac}}, vínculos clave→{ids}, nombre que el
    bot conoce de cada clave (contact_profiles)."""
    pacientes: dict[int, dict] = {}
    por_clave: dict[str, set[int]] = {}
    por_rut: dict[str, set[int]] = {}
    for pid, rut, nom, ape, nac, cel in h.execute(
            "SELECT id, rut, nombre, apellidos, fecha_nacimiento, celular FROM pacientes_heatmap"):
        if pid is None:
            continue
        pid = int(pid)
        pacientes[pid] = {"rut": _rut_norm(rut), "nombre": f"{nom or ''} {ape or ''}".strip(),
                          "nac": nac}
        if pacientes[pid]["rut"]:
            por_rut.setdefault(pacientes[pid]["rut"], set()).add(pid)
        if k := _clave(cel):
            por_clave.setdefault(k, set()).add(pid)

    # citas del bot: el teléfono es del paciente solo si NO agendó a un tercero
    for ph, pid in c.execute(
            "SELECT phone, id_paciente_medilink FROM citas_bot "
            "WHERE COALESCE(es_tercero, 0) = 0 AND id_paciente_medilink IS NOT NULL"):
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if (k := _clave(ph)) and pid in pacientes:
            por_clave.setdefault(k, set()).add(pid)

    nombre_conocido: dict[str, str] = {}
    for ph, rut, nom in c.execute("SELECT phone, rut, nombre FROM contact_profiles"):
        k = _clave(ph)
        if not k:
            continue
        if nom and nom.strip():
            nombre_conocido[k] = nom.strip()
        ids = por_rut.get(_rut_norm(rut)) if rut else None
        if ids and len(ids) == 1:
            por_clave.setdefault(k, set()).update(ids)
    return pacientes, por_clave, nombre_conocido


def _telefono_compartido(k: str, ids: set[int], pacientes: dict, nombre_conocido: dict) -> bool:
    """Mismo criterio que `winback._anotar_phone_compartido`: >=2 pacientes con
    el número, o el nombre que el bot conoce de ese WhatsApp no calza con
    ninguno de los pacientes vinculados."""
    if len(ids) > 1:
        return True
    nom = nombre_conocido.get(k)
    if not nom:
        return False
    try:
        from abono_transferencia import nombres_similares
    except Exception:
        return False
    return not any(nombres_similares(pacientes[i]["nombre"], nom)[0] for i in ids)


def construir_listas(c, h, bi: dict, hoy: date | None = None,
                     exclusion_exige_consent: bool = True,
                     semilla_top: float = SEMILLA_FRACCION_TOP,
                     semilla_exige_consent: bool = True) -> dict:
    """Listas de claves-9 + conteos de cada filtro (solo números).

    c: conexión a sessions.db (row_factory Row); h: conexión a heatmap_cache.db;
    bi: salida de `cargar_bi_sets()`. `semilla_exige_consent=False` existe solo
    para dimensionar (reportes); la sincronización real siempre lo exige."""
    hoy = hoy or date.today()
    pacientes, por_clave, nombre_conocido = _vinculos(c, h)
    bajas_loc, olvido_ruts = _bajas_locales(c)
    bajas = bajas_loc | bi["opt_out"] | bi["consent_no"]

    # pagos: margen por paciente en cada ventana
    desde_semilla = _hace_meses(hoy, SEMILLA_MESES)
    desde_excl = _hace_meses(hoy, EXCLUSION_MESES)
    try:
        from campanas_meta_routes import _pct_honorarios
        pct = _pct_honorarios(c)
    except Exception as e:
        log.warning("audiencias_captacion: %% honorarios no disponible (%s) — default %d",
                    e, PCT_HONORARIO_DEFAULT)
        pct = {}
    con_pago_excl: set[int] = set()
    margen: dict[int, float] = {}
    for pid, prof, monto, fecha in c.execute(
            "SELECT id_paciente, id_profesional, monto, fecha FROM bi_pagos_caja "
            "WHERE fecha >= ? AND monto > 0 AND id_paciente IS NOT NULL", (desde_excl,)):
        pid = int(pid)
        con_pago_excl.add(pid)
        if fecha >= desde_semilla:
            p = pct.get(prof) or PCT_HONORARIO_DEFAULT   # 0/ausente = sin dato -> 70
            margen[pid] = margen.get(pid, 0.0) + monto * (1 - p / 100)

    con_telefono = set().union(*por_clave.values()) if por_clave else set()
    st = {"menor_o_olvidado": 0, "telefono_compartido": 0, "baja_o_declinado": 0, "sin_consent": 0}

    def _elegible(k: str, ids: set[int], exigir_consent: bool) -> bool:
        if any(_es_menor(pacientes[i]["nac"], hoy) or pacientes[i]["rut"] in olvido_ruts
               for i in ids):
            st["menor_o_olvidado"] += 1
            return False
        if _telefono_compartido(k, ids, pacientes, nombre_conocido):
            st["telefono_compartido"] += 1
            return False
        if k in bajas:
            st["baja_o_declinado"] += 1
            return False
        if exigir_consent and k not in bi["consent_ok"]:
            st["sin_consent"] += 1
            return False
        return True

    # clave → ids (solo ids con pago en la ventana correspondiente)
    def _claves_de(pids: set[int]) -> dict[str, set[int]]:
        out: dict[str, set[int]] = {}
        for k, ids in por_clave.items():
            if ids & pids:
                out[k] = ids          # ids completos: el compartido se juzga con todos
        return out

    excl = [k for k, ids in _claves_de(con_pago_excl).items()
            if _elegible(k, ids, exclusion_exige_consent)]

    st_excl = dict(st)
    st = dict.fromkeys(st, 0)
    cand = {}
    for k, ids in _claves_de(set(margen)).items():
        if _elegible(k, ids, semilla_exige_consent):
            cand[k] = sum(margen.get(i, 0.0) for i in ids)
    ranking = sorted(cand, key=lambda k: cand[k], reverse=True)
    n_top = max(1, round(len(ranking) * semilla_top)) if ranking else 0
    semilla = ranking[:n_top]
    st_sem = dict(st)

    return {
        "exclusion": excl, "semilla": semilla,
        "stats": {
            "exclusion": {**st_excl, "pacientes_con_pago_24m": len(con_pago_excl),
                          "pacientes_con_pago_sin_telefono": len(con_pago_excl - con_telefono),
                          "final": len(excl)},
            "semilla": {**st_sem, "pacientes_con_pago_18m": len(margen),
                        "elegibles": len(ranking), "final": len(semilla)},
        },
    }


def construir_listas_prod(hoy: date | None = None) -> dict | None:
    """Lee prod (sessions.db + heatmap_cache.db + BI). None si BI no responde."""
    import config
    import session
    bi = cargar_bi_sets()
    if bi is None:
        return None
    ruta = Path(session.DB_PATH).parent / "heatmap_cache.db"
    h = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
    try:
        c = session._conn()
        try:
            return construir_listas(
                c, h, bi, hoy,
                exclusion_exige_consent=config.META_AUDIENCIA_EXCLUSION_EXIGE_CONSENT)
        finally:
            c.close()
    finally:
        h.close()


# ── Meta (reusa el mecanismo de custom_audiences_sync) ───────────────────────
def _hashes(claves: list[str]) -> list[str]:
    from custom_audiences_sync import _sha256_phone
    return sorted({h for k in claves if (h := _sha256_phone("56" + k))})


async def _reemplazar_usuarios(audience_id: str, hashes: list[str]) -> bool:
    """Reemplazo ATÓMICO (`/usersreplace`): quien sale de la lista (baja,
    olvido, ventana vencida) sale también de la audiencia. `/users` solo agrega."""
    import custom_audiences_sync as cas
    LOTE = 10_000
    sesion = random.randint(1, 2**31 - 1)
    n_lotes = max(1, (len(hashes) + LOTE - 1) // LOTE)
    for i in range(n_lotes):
        lote = hashes[i * LOTE:(i + 1) * LOTE]
        try:
            await cas._meta_post(
                f"{cas._META_API_BASE}/{audience_id}/usersreplace",
                data={
                    "payload": json.dumps({"schema": ["PHONE_SHA256"], "data": [[x] for x in lote]}),
                    "session": json.dumps({
                        "session_id": sesion, "batch_seq": i + 1,
                        "last_batch_flag": i == n_lotes - 1,
                        "estimated_num_total": len(hashes),
                    }),
                },
            )
        except Exception as e:
            log.error("audiencias_captacion: error reemplazando usuarios id=%s: %s", audience_id, e)
            return False
    return True


async def _audiencia(nombre: str, descripcion: str) -> str | None:
    import custom_audiences_sync as cas
    return (await cas._buscar_audiencia_existente(nombre)) or (
        await cas._crear_audiencia(nombre, descripcion))


async def sync_audiencias_captacion(hoy: date | None = None) -> dict:
    """Sincroniza las dos audiencias. Con el flag apagado no hace NADA."""
    import config
    if not config.META_AUDIENCIAS_CAPTACION_ACTIVE:
        return {"status": "flag_off"}
    import custom_audiences_sync as cas
    if not cas._META_ACCESS_TOKEN:
        log.warning("audiencias_captacion: META_ACCESS_TOKEN vacío — omitido")
        return {"status": "no_token"}
    listas = construir_listas_prod(hoy)
    if listas is None:
        return {"status": "bi_no_disponible"}

    out: dict = {"status": "ok", "audiencias": [], "stats": listas["stats"]}

    # 1. exclusión
    hs = _hashes(listas["exclusion"])
    if not hs:
        out["audiencias"].append({"name": NOMBRE_EXCLUSION, "status": "empty", "phones": 0})
    else:
        aid = await _audiencia(NOMBRE_EXCLUSION, DESCRIPCION_EXCLUSION)
        ok = bool(aid) and await _reemplazar_usuarios(aid, hs)
        out["audiencias"].append({"name": NOMBRE_EXCLUSION, "id": aid, "phones": len(hs),
                                  "status": "ok" if ok else "error"})

    # 2. semilla (+ similar solo si llega al mínimo de Meta)
    hs = _hashes(listas["semilla"])
    if len(hs) < MIN_SEMILLA_LOOKALIKE:
        out["audiencias"].append({"name": NOMBRE_SEMILLA, "status": "bajo_minimo",
                                  "phones": len(hs), "minimo": MIN_SEMILLA_LOOKALIKE})
    else:
        aid = await _audiencia(NOMBRE_SEMILLA, DESCRIPCION_SEMILLA)
        ok = bool(aid) and await _reemplazar_usuarios(aid, hs)
        entry = {"name": NOMBRE_SEMILLA, "id": aid, "phones": len(hs),
                 "status": "ok" if ok else "error"}
        if ok and not await cas._buscar_audiencia_existente(NOMBRE_SIMILAR):
            entry["lookalike_id"] = await cas._crear_lookalike(
                aid, nombre=NOMBRE_SIMILAR, descripcion=DESCRIPCION_SIMILAR)
        out["audiencias"].append(entry)
    return out


async def job_audiencias_captacion() -> None:
    """Job diario 04:30 CLT (gated por META_AUDIENCIAS_CAPTACION_ACTIVE)."""
    try:
        res = await sync_audiencias_captacion()
        if res.get("status") != "flag_off":
            log.info("job_audiencias_captacion: %s", res)
    except Exception as e:
        log.error("job_audiencias_captacion fallo: %s", e)
