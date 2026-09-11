# -*- coding: utf-8 -*-
"""Modulo Alma — Convenio Imagendent (Radiologia Dental).

POR QUE EXISTE
--------------
El 2026-09-07 el dueno pregunto cuantos cupones quedaban de la cuponera Plan Oro
y NINGUN sistema pudo responder. Hubo que barrer 250 atenciones de Medilink a
mano para descubrir que quedaban 3 de 30 — justo lo que el dueno intuia, pero
sin forma de probarlo ni de discutirle la cuenta al proveedor.

El agujero es de DISENO, no de datos. El modulo de vales (`vales_routes`) cuenta
el consumo por los VALES QUE EL MISMO EMITE, y el flujo real resulto ser otro:
la odontologa carga la prestacion directo en la atencion de Medilink, sin pasar
por el vale. Resultado, las dos bolsas mienten en direcciones opuestas:

  * Cuponera Oro  -> se vacio (27/30) sin que el modulo lo notara.
  * Cuenta Saldo  -> muestra $100.000 consumidos que NO existen: son 2 vales de
                     prueba nunca borrados ($65.000) mas un vale emitido y jamas
                     usado. En Medilink hay CERO conebeam realizados.

La leccion: un vale emitido es una PROMESA; la bolsa se descuenta cuando
Imagendent realiza el examen. Este modulo mide lo realizado, no lo prometido.

QUE HACE
--------
Un cron nocturno relee las atenciones dentales recientes, saca sus prestaciones
de `/atenciones/{id}/detalles` y guarda en `convenio_consumo` cada radiografia
del convenio. El panel /alma/imagendent lee esa tabla: cupones restantes, saldo,
margen, ritmo de consumo y la conciliacion contra los vales emitidos.

Las dos bolsas del convenio (anexo 2026-08-29) se descuentan DISTINTO:
  * Cuponera Plan Oro  -> cupo en UNIDADES: 30 RX (pano/tele/bitewing) + 2 CBCT
                          de cortesia. Un pack de ortodoncia consume 3.
  * Cuenta de Saldo    -> prepago en PESOS ($200.000), se descuenta al precio de
                          socio por examen (CBCT, periapicales, escaneos).

Tarifario: se importa de `vales_routes.PRESTACIONES`. NO se copia — una copia
diverge en silencio (misma leccion que los parsers bancarios duplicados).
Auth: admin / administracion, igual que el resto de los modulos de plata.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from session import db, log_event

log = logging.getLogger("imagendent")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(tags=["imagendent"])

# ── El plan contratado (anexo Imagendent 2026-08-12 / 2026-08-29) ────────────
# Plan Oro $300.000 = 30 RX a $10.000 + 2 CBCT de cortesia.
CUPONERA_ORO_RX   = 30
CUPONERA_ORO_CBCT = 2
CARGA_SALDO       = 200_000    # carga inicial Cuenta de Saldo Socio Estrategico
AVISO_SALDO       = 50_000     # Imagendent avisa para recargar en este piso

# ── Prestaciones de Medilink que pertenecen al convenio ─────────────────────
# Son la categoria 197 del catalogo (ids 5745-5749, creadas para el convenio).
# `unidades` = cuantos cupones de la cuponera consume. El pack de ortodoncia es
# bitewing + panoramica + teleradiografia, o sea TRES cupones en una sola linea:
# esa es la razon numero uno por la que contar filas da un resultado errado.
# `slug` apunta al tarifario de vales_routes (fuente unica de costo y venta).
MEDILINK_CONVENIO: dict[int, dict] = {
    5745: {"slug": "panoramica",      "unidades": 1},
    5746: {"slug": "bitewing",        "unidades": 1},
    5747: {"slug": "teleradiografia", "unidades": 1},
    5748: {"slug": "set_ortodoncia",  "unidades": 3},
    5749: {"slug": "cbct_unitario",   "unidades": 0},  # no gasta cupon de RX
}
PROFESIONALES_DENTALES = (55, 66, 72)   # Burgos · Castillo · Jimenez
DIAS_VENTANA = 45   # cuanto atras rebarre el cron cada noche


def _tarifa(slug: str) -> dict:
    """Costo/venta/bolsa desde el tarifario del modulo de vales."""
    from vales_routes import PRESTACIONES
    return PRESTACIONES.get(slug, {})


def _crear_tablas() -> None:
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS convenio_consumo (
                detalle_id     INTEGER PRIMARY KEY,   -- id del detalle en Medilink
                convenio       TEXT NOT NULL DEFAULT 'imagendent',
                atencion_id    INTEGER NOT NULL,
                fecha          TEXT NOT NULL,
                id_paciente    INTEGER,
                paciente       TEXT DEFAULT '',
                id_profesional INTEGER,
                profesional    TEXT DEFAULT '',
                id_prestacion  INTEGER,
                prestacion     TEXT DEFAULT '',
                slug           TEXT DEFAULT '',
                bolsa          TEXT DEFAULT '',
                unidades       INTEGER DEFAULT 0,
                costo          INTEGER DEFAULT 0,
                venta          INTEGER DEFAULT 0,
                cobrado        INTEGER DEFAULT 0,
                pagado         INTEGER DEFAULT 0,
                realizado      INTEGER DEFAULT 0,
                synced_at      TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_conv_fecha ON convenio_consumo(convenio, fecha)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_conv_bolsa ON convenio_consumo(convenio, bolsa)")


_crear_tablas()


def _auth(request: Request, token: str | None, cmc_session: str | None) -> str:
    from admin_routes import _verify_cookie, _is_admin_token
    from config import ADMIN_TOKEN

    auth_header = request.headers.get("authorization", "") if request else ""
    if auth_header.lower().startswith("bearer "):
        tk = auth_header.split(None, 1)[1].strip()
        if _is_admin_token(tk):
            return tk
    if cmc_session:
        if _verify_cookie(cmc_session) in ("admin", "administracion"):
            return ADMIN_TOKEN
    if token and _is_admin_token(token):
        return token
    raise HTTPException(status_code=401, detail="Token invalido")


# ── Sincronizacion: lo REALIZADO en Medilink, no lo prometido en vales ───────

async def sync_consumo(dias: int = DIAS_VENTANA) -> dict:
    """Relee las atenciones dentales recientes y guarda las RX del convenio.

    Idempotente: la PK es el `detalle_id` de Medilink, asi que rebarrer el mismo
    rango no duplica nada — solo refresca `pagado`/`realizado`, que es justo lo
    que cambia tarde (el caso Amina Saez: la prestacion se marco realizada 21
    dias despues de la consulta).

    Corre en el carril BATCH de Medilink: son ~250 GET a /atenciones/{id}/detalles
    y no puede competir con el bot atendiendo pacientes. Ver el guardrail de 429.
    """
    import asyncio
    import httpx
    from config import MEDILINK_BASE_URL, MEDILINK_TOKEN
    from medilink import use_batch_lane

    use_batch_lane()
    desde = (datetime.now(_CHILE_TZ).date() - timedelta(days=dias)).isoformat()
    marcas = ",".join("?" * len(PROFESIONALES_DENTALES))
    with db() as c:
        ats = list(c.execute(
            f"""SELECT atencion_id, fecha, id_paciente, paciente_nombre, id_profesional
                FROM bi_atenciones
                WHERE id_profesional IN ({marcas}) AND fecha >= ?
                ORDER BY fecha""",
            (*PROFESIONALES_DENTALES, desde)))

    headers = {"Authorization": f"Token {MEDILINK_TOKEN}"}
    nuevos = actualizados = 0
    fallidas: list[tuple] = []
    ahora = datetime.now(_CHILE_TZ).isoformat(timespec="seconds")

    async with httpx.AsyncClient(timeout=45) as cli:
        # DOS pasadas: la segunda reintenta lo que el rate-limit dejo afuera.
        # Sin esto el barrido sub-cuenta EN SILENCIO — la primera corrida real
        # (2026-09-08) perdio 10 atenciones por 429 y el panel dijo "4 cupones
        # restantes" cuando quedaban 3. Un contador que se equivoca callado es
        # peor que no tenerlo: con ese numero se le discute al proveedor.
        pendientes = list(ats)
        for pasada in (1, 2):
            if pasada == 2:
                if not fallidas:
                    break
                log.info("[imagendent] reintento de %d atenciones que fallaron", len(fallidas))
                pendientes, fallidas = fallidas, []
                await asyncio.sleep(20)
            for aid, fecha, id_pac, paciente, id_prof in pendientes:
                data = None
                for intento in range(5):
                    try:
                        r = await cli.get(f"{MEDILINK_BASE_URL}/atenciones/{aid}/detalles",
                                          headers=headers)
                    except Exception as e:            # red caida: no aborta el barrido
                        log.warning("imagendent sync: atencion %s error de red: %s", aid, e)
                        break
                    if r.status_code == 429:
                        await asyncio.sleep(2.5 * (intento + 1))
                        continue
                    if r.status_code != 200:
                        break
                    data = (r.json() or {}).get("data")
                    break

                # La atencion no se pudo leer: red caida, status != 200, o los 5
                # intentos de 429 agotados. NUNCA seguir con `data` en None — antes
                # reventaba en `for det in data` y se caia el barrido ENTERO, asi que
                # no se guardaba nada de lo que venia despues (el cron nocturno lleva
                # fallando desde que existe). Se anota para la segunda pasada y para
                # que `errores` diga la verdad: `fallidas` se declaraba y se leia,
                # pero nadie la llenaba, o sea el reintento era codigo muerto y
                # `errores` reportaba 0 por construccion.
                if data is None:
                    fallidas.append((aid, fecha, id_pac, paciente, id_prof))
                    continue

                for det in data:
                    reg = MEDILINK_CONVENIO.get(det.get("id_prestacion"))
                    if not reg:
                        continue
                    tar = _tarifa(reg["slug"])
                    fila = (
                        det.get("id"), aid, fecha, id_pac, paciente or "", id_prof,
                        det.get("profesional_realizador") or "",
                        det.get("id_prestacion"), (det.get("nombre_prestacion") or "").strip(),
                        reg["slug"], tar.get("bolsa", ""), reg["unidades"],
                        int(tar.get("costo") or 0), int(tar.get("venta") or 0),
                        int(det.get("total") or 0), int(det.get("pagado") or 0),
                        1 if det.get("realizado") else 0, ahora,
                    )
                    with db() as c:
                        existe = c.execute("SELECT 1 FROM convenio_consumo WHERE detalle_id=?",
                                           (det.get("id"),)).fetchone()
                        c.execute("""
                            INSERT INTO convenio_consumo
                              (detalle_id, atencion_id, fecha, id_paciente, paciente,
                               id_profesional, profesional, id_prestacion, prestacion,
                               slug, bolsa, unidades, costo, venta, cobrado, pagado,
                               realizado, synced_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(detalle_id) DO UPDATE SET
                              cobrado=excluded.cobrado, pagado=excluded.pagado,
                              realizado=excluded.realizado, profesional=excluded.profesional,
                              synced_at=excluded.synced_at
                        """, fila)
                    if existe:
                        actualizados += 1
                    else:
                        nuevos += 1
                await asyncio.sleep(0.35)

    res = {"atenciones": len(ats), "nuevos": nuevos, "actualizados": actualizados,
           "errores": len(fallidas), "desde": desde, "corrio": ahora}
    # Se persiste ACA y no en el cron para que el barrido manual del panel deje
    # la misma huella. En JSON, no str(dict): el panel necesita leer `errores`.
    import json as _json
    with db() as c:
        c.execute("INSERT OR REPLACE INTO system_state(key, value) VALUES(?,?)",
                  ("imagendent_ultimo_sync", _json.dumps(res)))
    log.info("[imagendent] sync — %s", res)
    return res


async def job_sync_imagendent() -> None:
    """Cron nocturno. Corre DESPUES del bi_sync (03:59), que llena bi_atenciones."""
    try:
        await sync_consumo()   # persiste su propio resultado
    except Exception as e:
        log.error("[imagendent] job nocturno fallo: %s", e, exc_info=True)


# ── Estado de las dos bolsas ────────────────────────────────────────────────

def _ultimo_sync(raw: str | None) -> dict | None:
    """El barrido guardado, o None. Tolera el formato viejo (str(dict))."""
    if not raw:
        return None
    import json
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {"crudo": raw}


def estado() -> dict:
    """Cuanto queda de cada bolsa, calculado sobre lo REALIZADO."""
    # `vales_convenio` se ASEGURA explicitamente, no se hereda del import.
    # vales_routes la crea como efecto colateral al importarse, pero un import
    # ya hecho es un no-op: si ese import ocurrio apuntando a otra DB, la tabla
    # no existe en la actual y este endpoint responde 500. Llamar a la funcion
    # es idempotente (CREATE TABLE IF NOT EXISTS) y no depende del orden.
    # Va ANTES de abrir la conexion: su DDL dentro de un `with db()` abierto
    # choca y SQLite tira "database is locked".
    import vales_routes
    vales_routes._crear_tabla()
    with db() as c:
        filas = list(c.execute(
            """SELECT fecha, paciente, profesional, prestacion, slug, bolsa,
                      unidades, costo, venta, cobrado, pagado, realizado, atencion_id
               FROM convenio_consumo WHERE convenio='imagendent' ORDER BY fecha DESC"""))
        vales = list(c.execute(
            """SELECT folio, creado_at, paciente, prestacion_nombre, bolsa, costo, estado, usado_at
               FROM vales_convenio ORDER BY creado_at DESC"""))
        ult = c.execute("SELECT value FROM system_state WHERE key='imagendent_ultimo_sync'").fetchone()

    rx_usados   = sum(f[6] for f in filas if f[5] == "oro")
    cbct_hechos = sum(1 for f in filas if f[4] == "cbct_unitario")
    cbct_cortesia_usados = min(cbct_hechos, CUPONERA_ORO_CBCT)
    # Los CBCT que exceden la cortesia recien ahi descuentan la Cuenta de Saldo.
    saldo_usado = sum(f[7] for f in filas
                      if f[5] == "saldo") - (cbct_cortesia_usados * 35_000)
    saldo_usado = max(saldo_usado, 0)

    # Lo COBRADO manda sobre el tarifario. Al reves (venta or cobrado) el margen
    # reporta el precio que "deberia" haberse cobrado y esconde justo el error que
    # este modulo tiene que delatar: los 7 packs salieron a $40.000 (el precio que
    # quedo mal cargado en Medilink) en vez de los $45.000 del tarifario.
    facturado = sum(f[9] or f[8] for f in filas)
    costo_real = sum(f[7] for f in filas if f[5] == "oro") + saldo_usado

    # Ritmo: unidades por semana ISO, para ver si acelera o se apaga.
    ritmo: dict[str, int] = {}
    for f in filas:
        if f[5] != "oro":
            continue
        try:
            iso = datetime.fromisoformat(f[0]).isocalendar()
            ritmo[f"{iso[0]}-S{iso[1]:02d}"] = ritmo.get(f"{iso[0]}-S{iso[1]:02d}", 0) + f[6]
        except ValueError:
            pass
    semanas = [ritmo[k] for k in sorted(ritmo)][-4:]

    # Vales de prueba: no son consumo, ensucian el saldo. Se marcan, no se borran.
    def _es_prueba(nombre: str) -> bool:
        n = (nombre or "").upper()
        return "MUESTRA" in n or "PRUEBA" in n

    vales_reales = [v for v in vales if not _es_prueba(v[2]) and v[6] in ("vigente", "usado")]
    vales_prueba = [v for v in vales if _es_prueba(v[2])]

    return {
        "oro": {
            "rx_total": CUPONERA_ORO_RX, "rx_usados": rx_usados,
            "rx_restantes": max(CUPONERA_ORO_RX - rx_usados, 0),
            "cbct_total": CUPONERA_ORO_CBCT, "cbct_usados": cbct_cortesia_usados,
            "cbct_restantes": max(CUPONERA_ORO_CBCT - cbct_cortesia_usados, 0),
        },
        "saldo": {"carga": CARGA_SALDO, "usado": saldo_usado,
                  "restante": CARGA_SALDO - saldo_usado, "aviso_en": AVISO_SALDO},
        "plata": {"facturado": facturado, "costo": costo_real,
                  "margen": facturado - costo_real},
        "ritmo_semanal": semanas,
        "alerta": alerta_reposicion(max(CUPONERA_ORO_RX - rx_usados, 0), semanas),
        "consumo": [dict(zip(
            ("fecha", "paciente", "profesional", "prestacion", "slug", "bolsa",
             "unidades", "costo", "venta", "cobrado", "pagado", "realizado", "atencion_id"), f))
            for f in filas],
        "vales_reales": len(vales_reales), "vales_prueba": len(vales_prueba),
        "vales_prueba_detalle": [{"folio": v[0], "paciente": v[2], "costo": v[5]}
                                 for v in vales_prueba],
        "ultimo_sync": _ultimo_sync(ult[0] if ult else None),
    }


# ─────────────────────────────────────────────────────────────────────────────
# TODO(dueno) — LA REGLA DE REPOSICION. Es tuya, no mia.
#
# Devuelve None si no hay que avisar, o {"nivel": "amarillo"|"rojo",
# "texto": "..."} para que el panel pinte el aviso.
#
# El dato duro que ya tenemos: el consumo NO es plano, acelera. Las semanas
# reales de la primera cuponera fueron 3 -> 3 -> 8 -> 13 cupones. Un umbral fijo
# ("avisa bajo 5") habria gritado el mismo dia en que se acabo, porque en la
# ultima semana se fueron 13 de un saque.
#
# Las dos formas de escribirlo, y lo que cada una te cuesta:
#
#   (a) UMBRAL FIJO — `if restantes <= 5`. Simple, predecible, y lo entiende
#       cualquiera que lea el panel. Pero con consumo acelerando llega tarde:
#       a 13/semana, 5 cupones son dos dias y medio.
#
#   (b) DIAS DE COBERTURA — proyectar con el promedio de las ultimas 2 semanas
#       (`semanas[-2:]`) y avisar cuando queden menos de N dias de consumo.
#       Se adapta al ritmo, pero exige que definas N — y N no es un numero
#       tecnico: es cuanto te demoras TU en hablar con Luis, negociar el tramo
#       y transferir. Si eso toma una semana, N=7 es tarde; queres N=14.
#
# Yo iria por (b) con N = tu tiempo de reposicion real x2, y ademas un piso
# duro de (a) por si el ritmo se cae a cero y el promedio miente. Pero el numero
# lo tenes que poner vos: yo no se cuanto tarda Luis en responderte.
# ─────────────────────────────────────────────────────────────────────────────
def alerta_reposicion(restantes: int, semanas: list[int]) -> dict | None:
    """Cuando el panel debe gritar 'hay que recargar la cuponera'."""
    return None  # ← escribi la regla aca


# ── API ─────────────────────────────────────────────────────────────────────

@router.get("/alma/api/imagendent/resumen")
def api_resumen(request: Request, token: str | None = Query(None),
                cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    return JSONResponse(estado())


@router.post("/alma/api/imagendent/sync")
async def api_sync(request: Request, token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None),
                   dias: int = Query(DIAS_VENTANA, ge=1, le=365)):
    """Barrido a demanda. El nocturno hace lo mismo sin que nadie apriete nada."""
    _auth(request, token, cmc_session)
    res = await sync_consumo(dias)
    log_event("imagendent", "sync_manual", res)
    return JSONResponse(res)


# ── Panel ───────────────────────────────────────────────────────────────────

_CSS = """
:root{--tinta:#0f2740;--gris:#5a7182;--linea:#e3ebf2;--fondo:#f5f8fb;--blanco:#fff;
--ok:#0f8a5f;--alerta:#b45309;--malo:#b91c1c;--marca:#0050a0}
*{box-sizing:border-box}
body{margin:0;background:var(--fondo);color:var(--tinta);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:26px 18px 60px}
h1{font-size:1.35rem;margin:0 0 2px}
.sub{color:var(--gris);font-size:.9rem;margin-bottom:22px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:20px}
.kpi{background:var(--blanco);border:1px solid var(--linea);border-radius:12px;padding:15px 16px}
.kpi b{display:block;font-size:1.7rem;line-height:1.15;font-variant-numeric:tabular-nums}
.kpi span{display:block;color:var(--gris);font-size:.76rem;margin-top:5px}
.kpi.bajo b{color:var(--malo)} .kpi.medio b{color:var(--alerta)}
.barra{height:6px;background:var(--linea);border-radius:3px;margin-top:9px;overflow:hidden}
.barra i{display:block;height:100%;background:var(--marca)}
.barra i.bajo{background:var(--malo)}
.card{background:var(--blanco);border:1px solid var(--linea);border-radius:12px;
padding:18px;margin-bottom:16px}
.card h2{font-size:1rem;margin:0 0 12px}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th,td{padding:8px 10px;border-bottom:1px solid var(--linea);text-align:left;vertical-align:top}
th{color:var(--gris);font-weight:600;font-size:.74rem;text-transform:uppercase;letter-spacing:.04em}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tabla-scroll{overflow-x:auto}
.aviso{border-radius:10px;padding:13px 15px;font-size:.86rem;margin-bottom:16px}
.aviso.amarillo{background:#fef6e7;border:1px solid #f5d9a0;color:#7c4a03}
.aviso.rojo{background:#fdeaea;border:1px solid #f3c0c0;color:#8c1c1c}
.aviso.gris{background:#eef3f8;border:1px solid var(--linea);color:var(--gris)}
.pill{display:inline-block;padding:2px 8px;border-radius:20px;font-size:.72rem;
background:#eef3f8;color:var(--gris)}
.pill.oro{background:#fdf3d8;color:#7c5a03} .pill.saldo{background:#e7f1fb;color:#124a86}
.pill.nopag{background:#fdeaea;color:#8c1c1c}
button{font:inherit;padding:8px 15px;border-radius:8px;border:1px solid var(--marca);
background:var(--marca);color:#fff;cursor:pointer}
button.sec{background:#fff;color:var(--marca)}
.pie{color:var(--gris);font-size:.78rem;margin-top:10px}
.spark{display:flex;gap:5px;align-items:flex-end;height:44px;margin-top:6px}
.spark i{flex:1;background:var(--marca);border-radius:2px 2px 0 0;min-height:3px}
"""


@router.get("/alma/imagendent", response_class=HTMLResponse)
def panel(request: Request, token: str | None = Query(None),
          cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    e = estado()
    oro, sal, plata = e["oro"], e["saldo"], e["plata"]
    # El token viaja en la URL: si no lo reenviamos, el fetch del panel se
    # autodeniega contra su propio endpoint (guardrail de auth que ya nos mordio).
    tk = f"?token={token}" if token else ""

    def _m(v: int) -> str:
        return "$" + f"{int(v or 0):,}".replace(",", ".")

    pct_rx = 100 * oro["rx_restantes"] / max(oro["rx_total"], 1)
    pct_sal = 100 * sal["restante"] / max(sal["carga"], 1)
    cls_rx = "bajo" if oro["rx_restantes"] <= 5 else ("medio" if pct_rx < 35 else "")
    cls_sal = "bajo" if sal["restante"] <= sal["aviso_en"] else ""

    al = e["alerta"]
    if al:
        aviso = f'<div class="aviso {al.get("nivel","amarillo")}">{al.get("texto","")}</div>'
    else:
        aviso = ('<div class="aviso gris">La regla de reposición todavía no está escrita '
                 '(<code>alerta_reposicion()</code> en <code>app/imagendent_routes.py</code>) — '
                 'el panel muestra los cupones pero aún no avisa solo.</div>')

    prueba = ""
    if e["vales_prueba"]:
        det = " · ".join(f'{v["folio"]} {v["paciente"]} {_m(v["costo"])}'
                         for v in e["vales_prueba_detalle"])
        prueba = (f'<div class="aviso amarillo"><b>{e["vales_prueba"]} vales de prueba</b> '
                  f'siguen contados como consumo en el módulo de vales y ensucian el saldo '
                  f'que se ve allá: {det}. Acá no se cuentan.</div>')

    filas = []
    for f in e["consumo"]:
        margen = (f["cobrado"] or f["venta"] or 0) - f["costo"]
        nopag = '' if f["pagado"] else ' <span class="pill nopag">sin pagar</span>'
        filas.append(
            f'<tr><td>{f["fecha"]}</td><td>{f["paciente"]}{nopag}</td>'
            f'<td>{f["prestacion"]}</td>'
            f'<td><span class="pill {f["bolsa"]}">{f["bolsa"]}</span></td>'
            f'<td class="n">{f["unidades"] or "—"}</td>'
            f'<td class="n">{_m(f["costo"])}</td>'
            f'<td class="n">{_m(f["cobrado"] or f["venta"])}</td>'
            f'<td class="n">{_m(margen)}</td></tr>')
    tabla = "".join(filas) or '<tr><td colspan="8" style="color:#5a7182">Sin consumo registrado. Corré el barrido.</td></tr>'

    sem = e["ritmo_semanal"]
    tope = max(sem) if sem else 1
    spark = "".join(f'<i style="height:{100*v/max(tope,1):.0f}%" title="{v} cupones"></i>'
                    for v in sem) or '<span style="color:#5a7182;font-size:.82rem">sin datos</span>'

    us = e["ultimo_sync"] or {}
    incompleto = ""
    if us.get("errores"):
        incompleto = (
            f'<div class="aviso rojo"><b>El último barrido no pudo leer '
            f'{us["errores"]} atenciones</b> (Medilink devolvió 429). '
            f'El conteo de abajo puede quedar CORTO — no lo uses para discutirle '
            f'el saldo a Imagendent hasta volver a barrer sin errores.</div>')
    ult_txt = (f'{us.get("corrio", "?")} · {us.get("atenciones", "?")} atenciones · '
               f'{us.get("nuevos", 0)} nuevas') if us else "nunca"

    return HTMLResponse(f"""<!doctype html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Convenio Imagendent · Alma</title><style>{_CSS}</style></head><body>
<div class="wrap">
  <h1>Convenio Imagendent — Radiología Dental</h1>
  <div class="sub">Se mide lo <b>realizado en Medilink</b>, no los vales emitidos.
  Último barrido: {ult_txt}</div>\n\n  {incompleto}

  {aviso}{prueba}

  <div class="kpis">
    <div class="kpi {cls_rx}"><b>{oro["rx_restantes"]}</b>
      <span>Cupones de radiografía restantes<br>de {oro["rx_total"]} del Plan Oro</span>
      <div class="barra"><i class="{cls_rx}" style="width:{pct_rx:.0f}%"></i></div></div>
    <div class="kpi"><b>{oro["cbct_restantes"]}</b>
      <span>CBCT de cortesía restantes<br>de {oro["cbct_total"]}</span></div>
    <div class="kpi {cls_sal}"><b>{_m(sal["restante"])}</b>
      <span>Cuenta de Saldo Socio Estratégico<br>de {_m(sal["carga"])} · avisa en {_m(sal["aviso_en"])}</span>
      <div class="barra"><i class="{cls_sal}" style="width:{pct_sal:.0f}%"></i></div></div>
    <div class="kpi"><b>{_m(plata["margen"])}</b>
      <span>Margen del convenio<br>{_m(plata["facturado"])} facturado − {_m(plata["costo"])} de costo</span></div>
  </div>

  <div class="card">
    <h2>Ritmo de consumo (cupones por semana)</h2>
    <div class="spark">{spark}</div>
    <div class="pie">Las últimas {len(sem)} semanas. Si la barra sube, la cuponera
    se acaba antes de lo que sugiere el promedio — es lo que pasó con la primera.</div>
  </div>

  <div class="card">
    <h2>Consumo registrado</h2>
    <div class="tabla-scroll"><table>
      <tr><th>Fecha</th><th>Paciente</th><th>Prestación</th><th>Bolsa</th>
          <th style="text-align:right">Cupones</th><th style="text-align:right">Costo</th>
          <th style="text-align:right">Cobrado</th><th style="text-align:right">Margen</th></tr>
      {tabla}
    </table></div>
    <div class="pie">El pack de ortodoncia consume <b>3</b> cupones en una sola línea:
    contar filas da un número errado.</div>
  </div>

  <div class="card">
    <h2>Barrido</h2>
    <p style="font-size:.86rem;color:var(--gris);margin:0 0 12px">
      Corre solo todas las noches a las 05:10, después del sync de Medilink.
      Acá lo podés forzar si acabás de cargar una prestación y la querés ver ya.</p>
    <button onclick="sync()">Barrer ahora</button>
    <span id="msg" style="margin-left:12px;font-size:.85rem;color:var(--gris)"></span>
  </div>
</div>
<script>
async function sync(){{
  const m=document.getElementById('msg'); m.textContent='Barriendo Medilink…';
  try{{
    const r=await fetch('/alma/api/imagendent/sync{tk}',{{method:'POST'}});
    const d=await r.json();
    if(!r.ok){{ m.textContent='Error: '+(d.detail||r.status); return; }}
    m.textContent=`Listo — ${{d.atenciones}} atenciones · ${{d.nuevos}} nuevos · ${{d.actualizados}} actualizados`;
    setTimeout(()=>location.reload(),900);
  }}catch(e){{ m.textContent='Error de red: '+e; }}
}}
</script>
</body></html>""")
