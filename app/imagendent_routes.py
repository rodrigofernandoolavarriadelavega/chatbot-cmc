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
import asyncio
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from session import db, log_event

log = logging.getLogger("imagendent")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(tags=["imagendent"])

# ── El plan contratado (anexo Imagendent 2026-08-12 / 2026-08-29) ────────────
# Plan Oro $300.000 = 30 RX a $10.000 + 2 CBCT de cortesia. Es un bundle FIJO:
# cada tramo trae lo mismo. Por eso el total se deriva de cuantos tramos hay
# comprados y no se escribe a mano — escribir "90" suelto esconde que son tres
# compras distintas y obliga a recalcular el CBCT a ojo.
RX_POR_TRAMO   = 30
CBCT_POR_TRAMO = 2
PRECIO_TRAMO   = 300_000
# Cada ENTREGA es un hecho con fecha. El consumo se imputa FIFO (se gasta
# primero la cuponera mas antigua), y asi se puede medir cuanto DURO cada una:
# ese es el dato que decide cuando pedir la siguiente, y es mucho mas honesto
# que un promedio sobre una bolsa unica de 90 cupones.
# OJO: la fecha del tramo 1 es la del anexo, no hay guia de despacho.
ENTREGAS_ORO = [
    {"fecha": "2026-08-12", "cuponeras": 1},   # piloto
    {"fecha": "2026-09-15", "cuponeras": 2},   # las dos que llegaron juntas
]
TRAMOS_ORO        = sum(e["cuponeras"] for e in ENTREGAS_ORO)   # 3
CUPONERA_ORO_RX   = RX_POR_TRAMO * TRAMOS_ORO                   # 90
CUPONERA_ORO_CBCT = CBCT_POR_TRAMO * TRAMOS_ORO                 # 6
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
# Que prestaciones sueltas equivalen a un pack. Sirve para medir cuanto cuesta
# la OFERTA: el pack va a $40.000 y las tres sueltas suman $45.000, o sea cada
# pack entrega $5.000 de descuento a proposito para ganar el caso de ortodoncia.
# No es un error de precio — el dueno lo confirmo el 2026-09-15 — pero conviene
# ver el acumulado para decidir si la oferta sigue conviniendo.
PACK_COMPONENTES = {"set_ortodoncia": ("bitewing", "panoramica", "teleradiografia")}

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
                              -- costo y venta TAMBIEN se refrescan: el tarifario es la
                              -- fuente unica y esta copia se independizaba para siempre.
                              -- Sin esto, corregir un precio en el tarifario no llegaba
                              -- nunca a las filas ya barridas y el panel comparaba contra
                              -- un precio que ya no existia (paso con el pack de
                              -- ortodoncia: $45.000 guardados vs $40.000 de oferta).
                              costo=excluded.costo, venta=excluded.venta,
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


def repartir_en_cuponeras(consumo_oro: list) -> tuple[list, dict]:
    """Imputa cada cupon a una cuponera concreta, la mas antigua primero (FIFO).

    `consumo_oro` son tuplas (fecha ISO, unidades) de la bolsa oro. Devuelve
    `(cuponeras, dias)`:

      · `cuponeras` — una fila por cuponera: cuando se entrego, cuantos cupones
        lleva, cuando se agoto y cuantos dias duro.
      · `dias` — el libro mayor: por fecha, cuantos cupones salieron ESE dia,
        en que cuponera iban y a que acumulado llegaron. Es lo que permite leer
        "1° +2 = 5" en el calendario sin recalcular nada.

    Dos casos que el modelo tiene que decir en voz alta en vez de promediar:

      · `antes_de_entrega` — cupones consumidos ANTES de que esa cuponera
        llegara. Pasa cuando la anterior se agoto y se siguio haciendo
        radiografias a cuenta de la que venia en camino. No es un error de
        calculo: es credito, y conviene verlo.
      · `sobregiro` — cupones que no caben en ninguna cuponera comprada.
    """
    cuponeras, sobregiro, dias = [], 0, {}
    for entrega in ENTREGAS_ORO:
        for _ in range(entrega["cuponeras"]):
            cuponeras.append({
                "n": len(cuponeras) + 1, "entregada": entrega["fecha"],
                "rx": RX_POR_TRAMO, "usados": 0, "agotada": None,
                "antes_de_entrega": 0, "primer_uso": None,
            })

    i = 0
    for fecha, unidades in sorted(consumo_oro):
        # El acumulado hay que anotarlo EN EL MOMENTO de imputar: despues de
        # repartir ya no se puede reconstruir a que altura iba la cuponera ese
        # dia. Por eso el libro mayor se arma aca y no en una segunda pasada.
        d = dias.setdefault(fecha, {"cupones": 0, "cuponera": None, "acum": 0,
                                    "cruce": False, "cbct": 0})
        for _ in range(int(unidades or 0)):
            while i < len(cuponeras) and cuponeras[i]["usados"] >= RX_POR_TRAMO:
                i += 1
            if i >= len(cuponeras):
                sobregiro += 1
                continue
            c = cuponeras[i]
            c["usados"] += 1
            c["primer_uso"] = c["primer_uso"] or fecha
            if fecha < c["entregada"]:
                c["antes_de_entrega"] += 1
            if c["usados"] == RX_POR_TRAMO:
                c["agotada"] = fecha
            d["cupones"] += 1
            # Un dia puede cruzar de una cuponera a la siguiente: se muestra la
            # ultima tocada y se marca el cruce en vez de esconderlo.
            if d["cuponera"] not in (None, c["n"]):
                d["cruce"] = True
            d["cuponera"], d["acum"] = c["n"], c["usados"]

    for c in cuponeras:
        if c["agotada"]:
            c["dias"] = (date.fromisoformat(c["agotada"])
                         - date.fromisoformat(c["entregada"])).days
            c["estado"] = "agotada"
        elif c["usados"]:
            c["estado"] = "en uso"
        else:
            c["estado"] = "sin abrir"
        c["restantes"] = RX_POR_TRAMO - c["usados"]
    if cuponeras:
        cuponeras[-1]["sobregiro"] = sobregiro
    return cuponeras, dias


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

    # DESCALCE TARIFARIO vs CAJA: en condiciones normales es 0. Si aparece algo,
    # es que la caja cobro distinto de lo que dice el tarifario — un precio mal
    # cargado en Medilink, por ejemplo. Se deja calculado para que ese caso no
    # pase inadvertido.
    descalce = sum(max((f[8] or 0) - f[9], 0) for f in filas if f[9])

    # DESCUENTO DEL PACK: lo que la oferta entrega respecto de vender las tres
    # radiografias por separado. Decision comercial, no error: el costo no
    # cambia ($30.000), asi que el descuento sale entero del margen.
    descuento_pack, packs_n, pack_venta, pack_suelto = 0, 0, 0, 0
    for f in filas:
        comps = PACK_COMPONENTES.get(f[4])
        if not comps:
            continue
        suelto = sum(_tarifa(c).get("venta") or 0 for c in comps)
        descuento_pack += max(suelto - (f[8] or 0), 0)
        packs_n += 1
        pack_venta, pack_suelto = (f[8] or 0), suelto

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
    # Una semana SIN consumo vale 0, no "no existe". Si se salta, dos semanas no
    # contiguas quedan pegadas en el grafico y una caida a cero se lee como
    # continuidad: S36=14 junto a S38=3 parecia "bajo a 3" cuando fue 14 -> 0 -> 3.
    # Se rellena entre la primera y la ultima semana CON DATOS (no hasta hoy: eso
    # dejaria el grafico en cero al mirar un periodo historico).
    if ritmo:
        _o = [datetime.strptime(k + "-1", "%G-S%V-%u").date() for k in sorted(ritmo)]
        _lunes, _d = [], _o[0]
        while _d <= _o[-1]:
            _lunes.append(_d)
            _d += timedelta(weeks=1)
        _claves = [f"{d.isocalendar()[0]}-S{d.isocalendar()[1]:02d}" for d in _lunes][-4:]
    else:
        _claves = []
    semanas = [ritmo.get(k, 0) for k in _claves]
    etiquetas = [k.split("-")[1] for k in _claves]   # "2026-S37" -> "S37"
    # Si la ultima barra es la semana en curso no es comparable con las cerradas
    # y no puede entrar en ninguna proyeccion: cerrado se compara con cerrado.
    _hoy = datetime.now(_CHILE_TZ).date()
    _sem_hoy = f"{_hoy.isocalendar()[0]}-S{_hoy.isocalendar()[1]:02d}"
    parcial = bool(_claves) and _claves[-1] == _sem_hoy and _hoy.weekday() < 6

    # Vales de prueba: no son consumo, ensucian el saldo. Se marcan, no se borran.
    def _es_prueba(nombre: str) -> bool:
        n = (nombre or "").upper()
        return "MUESTRA" in n or "PRUEBA" in n

    vales_reales = [v for v in vales if not _es_prueba(v[2]) and v[6] in ("vigente", "usado")]
    vales_prueba = [v for v in vales if _es_prueba(v[2])]

    # El CBCT se cuenta APARTE: no gasta cupon de radiografia (unidades=0 en el
    # catalogo), asi que sumarlo al mismo numero mentiria sobre la cuponera.
    _cuponeras, _dias_rx = repartir_en_cuponeras(
        [(f[0], f[6]) for f in filas if f[5] == "oro"])
    for f in filas:
        if f[4] == "cbct_unitario":
            _d = _dias_rx.setdefault(f[0], {"cupones": 0, "cuponera": None,
                                            "acum": 0, "cruce": False, "cbct": 0})
            _d["cbct"] += 1

    return {
        "oro": {
            "rx_total": CUPONERA_ORO_RX, "rx_usados": rx_usados,
            "rx_restantes": max(CUPONERA_ORO_RX - rx_usados, 0),
            "cbct_total": CUPONERA_ORO_CBCT, "cbct_usados": cbct_cortesia_usados,
            "cbct_restantes": max(CUPONERA_ORO_CBCT - cbct_cortesia_usados, 0),
            # Sobregiro = cupones consumidos POR ENCIMA de los 30 comprados.
            # Es un hecho consumado, no una alerta: esas radiografias ya se
            # hicieron y hay que pagarlas a tarifa o renegociar el tramo.
            "rx_sobregiro": max(rx_usados - CUPONERA_ORO_RX, 0),
        },
        "saldo": {"carga": CARGA_SALDO, "usado": saldo_usado,
                  "restante": CARGA_SALDO - saldo_usado, "aviso_en": AVISO_SALDO},
        "plata": {"facturado": facturado, "costo": costo_real,
                  "margen": facturado - costo_real, "descalce": descalce,
                  "descuento_pack": descuento_pack, "packs": packs_n,
                  "pack_venta": pack_venta, "pack_suelto": pack_suelto,
                  "margen_sin_descuento": facturado + descuento_pack - costo_real},
        "cuponeras": _cuponeras, "dias": _dias_rx,
        "ritmo_semanal": semanas, "ritmo_labels": etiquetas,
        "ritmo_parcial": parcial, "ritmo_dia_semana": _hoy.weekday() + 1,
        "tramos": {"n": TRAMOS_ORO, "invertido": TRAMOS_ORO * PRECIO_TRAMO},
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
#       y transferir. Si eso toma una semana, N=7 llega tarde; quieres N=14.
#
# Yo iria por (b) con N = tu tiempo de reposicion real x2, y ademas un piso
# duro de (a) por si el ritmo se cae a cero y el promedio miente. Pero el numero
# lo tienes que poner tu: yo no se cuanto se demora Luis en responderte.
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


# Ventana del barrido MANUAL. El nocturno usa DIAS_VENTANA (45) porque tiene la
# madrugada entera; el manual no puede.
#
# Comprobado en vivo el 2026-09-15: un barrido de 45 dias son ~300 atenciones y
# revienta la CUOTA DE CUENTA de Medilink. El carril batch (use_batch_lane, que
# sync_consumo ya llamaba) no alcanza: reparte los cupos LOCALES, pero el 429 lo
# devuelve Medilink por cuenta, sin importar de que carril venga. Resultado: el
# bot en vivo empezo a comerse 429 en /pacientes y en el ticker de agenda, en
# plena hora de atencion.
#
# 7 dias son ~50 atenciones (~20s) y cubren de sobra el caso real del boton:
# "acabo de cargar una prestacion y la quiero ver ya".
DIAS_MANUAL = 7

# Ademas: ~300 atenciones x 0.35s pasan los 100 segundos y nginx corta a los 30,
# asi que el boton devolvia 504 SIEMPRE. Por eso el barrido se lanza en segundo
# plano y el panel consulta el resultado con /sync/estado.
_sync_corriendo = {"activo": False, "res": None, "error": None}


async def _sync_en_fondo(dias: int) -> None:
    _sync_corriendo.update(activo=True, res=None, error=None)
    try:
        res = await sync_consumo(dias)
        log_event("imagendent", "sync_manual", res)
        _sync_corriendo["res"] = res
    except Exception as exc:                      # noqa: BLE001
        log.exception("Barrido manual de Imagendent fallo")
        _sync_corriendo["error"] = str(exc)
    finally:
        _sync_corriendo["activo"] = False


@router.post("/alma/api/imagendent/sync")
async def api_sync(request: Request, token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None),
                   dias: int = Query(DIAS_MANUAL, ge=1, le=365)):
    """Lanza el barrido y responde al tiro. El nocturno hace lo mismo a las 05:10."""
    _auth(request, token, cmc_session)
    if _sync_corriendo["activo"]:
        return JSONResponse({"estado": "corriendo",
                             "detalle": "Ya hay un barrido en curso."})
    asyncio.create_task(_sync_en_fondo(dias))
    return JSONResponse({"estado": "lanzado"})


@router.get("/alma/api/imagendent/sync/estado")
def api_sync_estado(request: Request, token: str | None = Query(None),
                    cmc_session: str | None = Cookie(None)):
    """En que va el barrido lanzado desde el panel."""
    _auth(request, token, cmc_session)
    if _sync_corriendo["activo"]:
        return JSONResponse({"estado": "corriendo"})
    if _sync_corriendo["error"]:
        return JSONResponse({"estado": "error", "detalle": _sync_corriendo["error"]})
    if _sync_corriendo["res"]:
        return JSONResponse({"estado": "listo", **_sync_corriendo["res"]})
    return JSONResponse({"estado": "inactivo"})


# ── Panel ───────────────────────────────────────────────────────────────────

_CSS = """
/* Sistema visual Alma — misma paleta que el resto de los modulos del shell.
   El panel vivia con una paleta propia (#0050a0) y por eso se veia pegado
   con calzador dentro de Alma. Las fuentes degradan a system-ui si el CDN
   de Google esta bloqueado: ninguna medida depende de que cargue. */
:root{
  --aqua:#4FBECE; --aqua-s:#e8f8fb; --blue:#1172AB; --navy:#0F3F68;
  --bg:#F7FBFD; --card:#fff; --border:#d6e4ec; --text:#0F3F68; --mute:#62788a;
  --red:#e84545; --red-s:#fdeef0; --amber:#d98407; --amber-s:#fdf6e7;
  --green:#12a150; --green-s:#e9f9f0;
  --sh-sm:0 1px 3px rgba(15,63,104,.08);
  --sh-md:0 8px 30px rgba(15,63,104,.12);
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-size:13px;
  font-family:'Montserrat',-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px 56px}

/* ── Cabecera ─────────────────────────────────────────── */
.top{position:sticky;top:0;z-index:20;background:rgba(247,251,253,.92);
  backdrop-filter:blur(10px);border-bottom:1px solid var(--border);
  margin:0 -16px 20px;padding:16px;display:flex;gap:14px;
  align-items:center;flex-wrap:wrap}
.top h1{font-size:17px;font-weight:800;margin:0;letter-spacing:-.2px}
.top .meta{color:var(--mute);font-size:11.5px;margin-top:3px}
.top .sp{flex:1 1 60px}

/* ── Semaforo: la unica pregunta que importa ──────────── */
.hero{border-radius:16px;padding:22px 24px;margin-bottom:18px;
  box-shadow:var(--sh-md);position:relative;overflow:hidden}
.hero.rojo{background:linear-gradient(135deg,#e84545,#b8202f);color:#fff}
.hero.amber{background:linear-gradient(135deg,#f0a92b,#d98407);color:#fff}
.hero.verde{background:linear-gradient(135deg,#1fb862,#0d8a48);color:#fff}
.hero .est{display:flex;align-items:center;gap:9px;font-size:11px;
  font-weight:800;letter-spacing:.14em;text-transform:uppercase;opacity:.92}
.hero .est .dot{width:9px;height:9px;border-radius:50%;background:#fff;
  box-shadow:0 0 0 4px rgba(255,255,255,.28)}
.hero .cifra{font-size:40px;font-weight:800;line-height:1.05;margin:10px 0 4px;
  font-variant-numeric:tabular-nums;letter-spacing:-1.2px}
.hero .cifra small{font-size:19px;font-weight:700;opacity:.8}
.hero p{margin:8px 0 0;font-size:13px;line-height:1.55;max-width:62ch;opacity:.95}
.hero .acc{margin-top:14px;display:inline-block;background:rgba(255,255,255,.18);
  border:1px solid rgba(255,255,255,.3);border-radius:9px;padding:9px 13px;
  font-size:12.5px;line-height:1.5;max-width:64ch}

/* ── Hallazgos: plata que se mueve con una accion ─────── */
.hall{display:grid;gap:12px;margin-bottom:18px;
  grid-template-columns:repeat(auto-fit,minmax(248px,1fr))}
.h{background:var(--card);border:1px solid var(--border);border-left:4px solid var(--mute);
  border-radius:13px;padding:15px 16px;box-shadow:var(--sh-sm)}
.h.rojo{border-left-color:var(--red)} .h.amber{border-left-color:var(--amber)}
.h.aqua{border-left-color:var(--aqua)}
.h .tag{font-size:10px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;
  color:var(--mute)}
.h .v{font-size:25px;font-weight:800;font-variant-numeric:tabular-nums;
  margin:5px 0 4px;letter-spacing:-.6px}
.h.rojo .v{color:var(--red)} .h.amber .v{color:var(--amber)} .h.aqua .v{color:var(--blue)}
.h p{margin:0;font-size:12px;line-height:1.5;color:var(--mute)}
.h p b{color:var(--text)}

/* ── KPIs con medidor ─────────────────────────────────── */
.kpis{display:grid;gap:12px;margin-bottom:18px;
  grid-template-columns:repeat(auto-fit,minmax(215px,1fr))}
.k{background:var(--card);border:1px solid var(--border);border-radius:13px;
  padding:15px 16px;box-shadow:var(--sh-sm)}
.k .lbl{font-size:10px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;
  color:var(--mute)}
.k .v{font-size:27px;font-weight:800;font-variant-numeric:tabular-nums;
  margin:6px 0 2px;letter-spacing:-.7px}
.k .v.rojo{color:var(--red)} .k .v.amber{color:var(--amber)}
.k .de{font-size:11.5px;color:var(--mute);line-height:1.45}
.g{height:7px;background:#e8f0f5;border-radius:4px;margin-top:10px;overflow:hidden}
.g i{display:block;height:100%;border-radius:4px;background:var(--aqua);
  transition:width .5s ease}
.g i.rojo{background:var(--red)} .g i.amber{background:var(--amber)}

/* ── Tarjetas ─────────────────────────────────────────── */
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;
  padding:18px 20px;margin-bottom:16px;box-shadow:var(--sh-sm)}
.card > h2{font-size:13px;font-weight:800;margin:0 0 3px;letter-spacing:-.1px}
.card > .h2s{font-size:11.5px;color:var(--mute);margin:0 0 15px;line-height:1.5}
.nota{font-size:11.5px;color:var(--mute);line-height:1.55;margin:13px 0 0;
  padding-top:12px;border-top:1px solid var(--border)}

/* ── Ritmo ────────────────────────────────────────────── */
.ritmo{display:flex;gap:12px;align-items:flex-end;height:132px;padding-top:6px}
.ritmo .col{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;
  height:100%;justify-content:flex-end}
.ritmo .n{font-size:14px;font-weight:800;font-variant-numeric:tabular-nums}
.ritmo .b{width:100%;max-width:70px;border-radius:6px 6px 0 0;min-height:4px;
  background:linear-gradient(180deg,var(--aqua),var(--blue));transition:height .5s ease}
.ritmo .col.max .b{background:linear-gradient(180deg,#f0a92b,var(--amber))}
.ritmo .s{font-size:10.5px;color:var(--mute);font-weight:700;letter-spacing:.04em}

/* ── Cuponeras: de la entrega hasta que se gasta ───────── */
.cup{display:flex;flex-direction:column;gap:11px}
.c{border:1px solid var(--border);border-radius:12px;padding:13px 15px;
  background:linear-gradient(90deg,#fbfdfe,#fff)}
.c.agotada{border-color:#e4d4a8;background:linear-gradient(90deg,#fffcf3,#fff)}
.c.uso{border-color:var(--aqua);box-shadow:0 0 0 3px rgba(79,190,206,.14)}
.c .l1{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin-bottom:9px}
.c .tit{font-weight:800;font-size:13px}
.c .fechas{font-size:11.5px;color:var(--mute)}
.c .dur{margin-left:auto;font-size:11.5px;font-weight:700;color:var(--mute);
  font-variant-numeric:tabular-nums}
.c .est{font-size:9.5px;font-weight:800;letter-spacing:.09em;text-transform:uppercase;
  padding:2.5px 8px;border-radius:20px}
.c.agotada .est{background:#fdf3d8;color:#8a6205}
.c.uso .est{background:var(--aqua-s);color:var(--blue)}
.c.nueva .est{background:#eef4f8;color:var(--mute)}
.c .celdas{display:flex;gap:2.5px;flex-wrap:wrap}
.c .u{width:100%;max-width:17px;height:15px;flex:1 1 9px;border-radius:2.5px;
  background:#e4eef4}
.c .u.on{background:linear-gradient(180deg,var(--aqua),var(--blue))}
.c.agotada .u.on{background:linear-gradient(180deg,#e9c463,var(--amber))}
.c .pie{font-size:11.5px;color:var(--mute);margin-top:8px;line-height:1.5}
.c .pie b{color:var(--text)}
.c .alerta{color:var(--red);font-weight:700}

/* ── Mapa calendario: el saldo corriente, dia a dia ────── */
.mes{margin-bottom:20px;max-width:640px}
.mes h3{font-size:12px;font-weight:800;margin:0 0 9px;text-transform:capitalize;
  letter-spacing:-.1px}
.dow{display:grid;grid-template-columns:repeat(7,1fr);gap:5px;margin-bottom:5px}
.dow span{font-size:9.5px;font-weight:800;color:var(--mute);text-align:center;
  letter-spacing:.06em;text-transform:uppercase}
.grid{display:grid;grid-template-columns:repeat(7,1fr);gap:5px}
.d{border:1px solid var(--border);border-radius:8px;padding:3px 5px 4px;
  background:var(--card);display:flex;flex-direction:column;height:58px;
  position:relative;overflow:hidden}
.d.off{background:transparent;border-color:transparent}
.d.nada{background:#fbfdfe}
.d .dn{font-size:9.5px;font-weight:700;color:var(--mute);line-height:1}
.d.act{border-color:var(--aqua);background:linear-gradient(160deg,#f3fbfd,#fff)}
.d.act .dn{color:var(--blue)}
.d .mas{font-size:15px;font-weight:800;line-height:1;margin-top:auto;
  font-variant-numeric:tabular-nums;letter-spacing:-.5px}
.d .cup{font-size:9px;font-weight:800;line-height:1.1;color:var(--mute);
  font-variant-numeric:tabular-nums;white-space:nowrap;margin-top:2px}
.d .cup b{color:var(--blue)}
/* El ordinal va arriba a la izquierda, al lado del numero del dia: en la
   linea de abajo competia por el ancho con el acumulado y se partia en dos. */
.d .dn{display:flex;gap:4px;align-items:baseline}
.d .dn b{font-size:9px;font-weight:800}
.d.c2{border-color:#b9a05e;background:linear-gradient(160deg,#fffcf2,#fff)}
.d.c2 .cup b,.d.c2 .dn{color:#8a6205}
.d.c3{border-color:#8f9fd6;background:linear-gradient(160deg,#f6f7fd,#fff)}
.d.c3 .cup b,.d.c3 .dn{color:#4a5aa8}
.d .ord{position:absolute;top:3px;left:19px;font-size:9px;font-weight:800;
  color:var(--blue);line-height:1}
.d.c2 .ord{color:#8a6205} .d.c3 .ord{color:#4a5aa8}
.d .cb{position:absolute;top:3px;right:3px;font-size:8px;font-weight:800;
  background:#efe3fb;color:#6b3fa0;border-radius:5px;padding:1px 4px;line-height:1.4}
.d .hito{position:absolute;inset:0;border-radius:8px;pointer-events:none;
  box-shadow:inset 0 0 0 2px var(--amber)}
.d.entrega{border-color:var(--green)}
.d.entrega .dn{color:var(--green)}
.d .ent{position:absolute;bottom:2px;right:4px;font-size:8.5px;font-weight:800;
  color:var(--green)}
.ley{display:flex;gap:14px;flex-wrap:wrap;font-size:10.5px;color:var(--mute);
  margin-top:4px}
.ley i{display:inline-block;width:10px;height:10px;border-radius:3px;
  margin-right:4px;vertical-align:-1px;border:1px solid var(--border)}
.ley .l1 i{background:linear-gradient(160deg,#f3fbfd,#fff);border-color:var(--aqua)}
.ley .l2 i{background:linear-gradient(160deg,#fffcf2,#fff);border-color:#b9a05e}
.ley .l3 i{background:linear-gradient(160deg,#f6f7fd,#fff);border-color:#8f9fd6}
.ley .lc i{background:#efe3fb;border-color:#c9a9ec}
.ley .le i{border-color:var(--green)}

/* ── Tabla ────────────────────────────────────────────── */
.scroll{overflow-x:auto;margin:0 -20px;padding:0 20px}
table{width:100%;border-collapse:collapse;font-size:12px;min-width:660px}
th{text-align:left;font-size:10px;font-weight:800;letter-spacing:.08em;
  text-transform:uppercase;color:var(--mute);padding:0 10px 9px;
  border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid #eef4f8;vertical-align:middle}
tbody tr:hover{background:var(--aqua-s)}
tbody tr:last-child td{border-bottom:0}
.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;
  font-family:var(--mono);font-size:11.5px}
.nom{font-weight:600}
.sub{font-size:10.5px;color:var(--mute);font-weight:500}
.delta{color:var(--red);font-weight:700}
.pill{display:inline-block;padding:2.5px 8px;border-radius:20px;font-size:10px;
  font-weight:800;letter-spacing:.05em;text-transform:uppercase}
.pill.oro{background:#fdf3d8;color:#8a6205}
.pill.saldo{background:var(--aqua-s);color:var(--blue)}
.pill.nopag{background:var(--red-s);color:var(--red);margin-left:5px}
.vacio{text-align:center;color:var(--mute);padding:26px 10px;font-size:12.5px}

/* ── Avisos y botones ─────────────────────────────────── */
.aviso{border-radius:11px;padding:13px 16px;font-size:12.5px;line-height:1.55;
  margin-bottom:14px;border:1px solid}
.aviso.amber{background:var(--amber-s);border-color:#f0dcae;color:#7c4a03}
.aviso.rojo{background:var(--red-s);border-color:#f5c4ca;color:#8c1c1c}
.aviso.gris{background:#eff5f9;border-color:var(--border);color:var(--mute)}
.aviso code{font-family:var(--mono);font-size:11px;background:rgba(15,63,104,.07);
  padding:1px 5px;border-radius:4px}
.btn{font:inherit;font-size:12.5px;font-weight:700;padding:9px 16px;border-radius:9px;
  border:1px solid var(--blue);background:var(--blue);color:#fff;cursor:pointer;
  box-shadow:var(--sh-sm);transition:filter .15s,transform .1s}
.btn:hover{filter:brightness(1.08)} .btn:active{transform:translateY(1px)}
.btn[disabled]{opacity:.55;cursor:progress}
#msg{font-size:12px;color:var(--mute);margin-left:11px}

@media(max-width:560px){
  .hero{padding:18px 17px} .hero .cifra{font-size:33px}
  .card{padding:15px 16px} .scroll{margin:0 -16px;padding:0 16px}
  .top h1{font-size:15.5px}
}
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

    def _m(v) -> str:
        return "$" + f"{int(v or 0):,}".replace(",", ".")

    sem = e["ritmo_semanal"]
    lbl = e.get("ritmo_labels") or []
    prom2 = (sum(sem[-2:]) / len(sem[-2:])) if sem[-2:] else 0

    # ── Semaforo. Separa HECHO de PRONOSTICO a proposito:
    #    el sobregiro y el agotamiento ya ocurrieron y se afirman siempre;
    #    alerta_reposicion() es la regla PREDICTIVA (avisar ANTES) y es del dueno.
    sobre = oro.get("rx_sobregiro", 0)
    al = e["alerta"]
    if sobre:
        h_cls, h_est = "rojo", "Cuponera sobregirada"
        h_cifra = f'{oro["rx_usados"]}<small> de {oro["rx_total"]} cupones</small>'
        h_txt = (f'Se consumieron <b>{sobre} cupón{"es" if sobre > 1 else ""} mas</b> de los que '
                 f'tiene el Plan Oro. Esas radiografías ya se hicieron: o se pagan a tarifa '
                 f'llena o entran en la negociación del próximo tramo con Luis.')
    elif oro["rx_restantes"] == 0:
        h_cls, h_est = "rojo", "Cuponera agotada"
        h_cifra = f'0<small> cupones</small>'
        h_txt = 'No quedan cupones del Plan Oro. Cada radiografía nueva se paga a tarifa llena.'
    elif al:
        h_cls = "rojo" if al.get("nivel") == "rojo" else "amber"
        h_est, h_cifra = "Hay que recargar", f'{oro["rx_restantes"]}<small> cupones</small>'
        h_txt = al.get("texto", "")
    else:
        h_cls, h_est = "verde", "Convenio vigente"
        h_cifra = f'{oro["rx_restantes"]}<small> cupones</small>'
        dias = (oro["rx_restantes"] / prom2 * 7) if prom2 else 0
        h_txt = (f'Al ritmo de las últimas 2 semanas ({prom2:.1f} por semana) alcanzan '
                 f'para <b>{dias:.0f} días</b> más.' if prom2
                 else 'Sin consumo reciente para proyectar cuánto duran.')

    # La regla de aviso sigue sin escribirse: se dice una sola vez y sin gritar,
    # porque el estado de arriba ya no depende de ella.
    h_acc = ""
    if not al:
        h_acc = ('<div class="acc">El panel avisa cuando la cuponera <b>ya</b> se agotó. '
                 'Para que avise <b>antes</b> falta escribir la regla de reposición '
                 '(<code style="opacity:.85">alerta_reposicion()</code>).</div>')

    # ── Hallazgos: solo plata que se mueve con UNA accion concreta.
    hall = []
    if plata.get("descalce"):
        n_d = sum(1 for f in e["consumo"] if f["cobrado"] and f["cobrado"] < (f["venta"] or 0))
        hall.append(
            f'<div class="h rojo"><div class="tag">Descalce con el tarifario</div>'
            f'<div class="v">{_m(plata["descalce"])}</div>'
            f'<p>{n_d} atenciones se cobraron <b>distinto de lo que dice el tarifario</b>. '
            f'Suele ser un precio mal cargado en Medilink — conviene revisar el catálogo.'
            f'</p></div>')
    if plata.get("descuento_pack"):
        _n = plata.get("packs", 0)
        _uno = plata["descuento_pack"] // max(_n, 1)
        _mg_pack = 100 * (plata["pack_venta"] - 30_000) / max(plata["pack_venta"], 1)
        _mg_suel = 100 * (plata["pack_suelto"] - 30_000) / max(plata["pack_suelto"], 1)
        hall.append(
            f'<div class="h amber"><div class="tag">Lo que cuesta la oferta</div>'
            f'<div class="v">{_m(plata["descuento_pack"])}</div>'
            f'<p><b>{_n} packs</b> a {_m(plata["pack_venta"])} en vez de '
            f'{_m(plata["pack_suelto"])} sueltas: {_m(_uno)} de descuento por caso. '
            f'Es a propósito y el costo no cambia, así que el pack deja '
            f'<b>{_mg_pack:.0f}%</b> de margen contra <b>{_mg_suel:.0f}%</b> vendiendo '
            f'las tres por separado.</p></div>')
    if oro["cbct_restantes"]:
        hall.append(
            f'<div class="h amber"><div class="tag">Sobre la mesa</div>'
            f'<div class="v">{_m(oro["cbct_restantes"] * 35_000)}</div>'
            f'<p><b>{oro["cbct_restantes"]} CBCT de cortesía</b> sin usar, ya pagados dentro '
            f'del Plan Oro. Si vencen o se arrastran al próximo tramo <b>depende del '
            f'anexo y no está cerrado por escrito</b> — conviene confirmarlo con Luis.'
            f'</p></div>')
    hall.append(
        f'<div class="h aqua"><div class="tag">Margen del convenio</div>'
        f'<div class="v">{_m(plata["margen"])}</div>'
        f'<p>{_m(plata["facturado"])} facturado menos {_m(plata["costo"])} de costo = '
        f'<b>{100 * plata["margen"] / max(plata["facturado"], 1):.0f}%</b>'
        + (f'. Sin el descuento del pack serían {_m(plata["margen_sin_descuento"])} '
           f'({100 * plata["margen_sin_descuento"] / max(plata["facturado"] + plata["descuento_pack"], 1):.0f}%).'
           if plata.get("descuento_pack") else '.')
        + '</p></div>')

    # ── Vales de prueba: ensucian el saldo del OTRO modulo, no el de este.
    prueba = ""
    if e["vales_prueba"]:
        det = " · ".join(f'{v["folio"]} {v["paciente"]} {_m(v["costo"])}'
                         for v in e["vales_prueba_detalle"])
        prueba = (f'<div class="aviso amber"><b>{e["vales_prueba"]} vales de prueba</b> siguen '
                  f'contados como consumo en el módulo de vales y ensucian el saldo que se ve '
                  f'allá: {det}. Acá no se cuentan.</div>')

    us = e["ultimo_sync"] or {}
    incompleto = ""
    if us.get("errores"):
        incompleto = (
            f'<div class="aviso rojo"><b>El último barrido no pudo leer {us["errores"]} '
            f'atenciones</b> (Medilink devolvió 429). El conteo de abajo puede quedar CORTO — '
            f'no lo uses para discutirle el saldo a Imagendent hasta volver a barrer sin errores.'
            f'</div>')

    # ── KPIs
    pct_rx = 100 * oro["rx_restantes"] / max(oro["rx_total"], 1)
    pct_sal = 100 * sal["restante"] / max(sal["carga"], 1)
    cls_rx = "rojo" if oro["rx_restantes"] == 0 else ("amber" if pct_rx < 35 else "")
    cls_sal = "rojo" if sal["restante"] <= sal["aviso_en"] else ""
    pct_mar = 100 * plata["margen"] / max(plata["facturado"], 1)

    kpis = f"""
    <div class="k"><div class="lbl">Cupones de radiografía</div>
      <div class="v {cls_rx}">{oro["rx_restantes"]}</div>
      <div class="de">restantes de {oro["rx_total"]} · {oro["rx_usados"]} usados</div>
      <div class="g"><i class="{cls_rx}" style="width:{pct_rx:.0f}%"></i></div></div>
    <div class="k"><div class="lbl">CBCT de cortesía</div>
      <div class="v">{oro["cbct_restantes"]}</div>
      <div class="de">de {oro["cbct_total"]} incluidos en el Plan Oro</div>
      <div class="g"><i style="width:{100 * oro["cbct_restantes"] / max(oro["cbct_total"], 1):.0f}%"></i></div></div>
    <div class="k"><div class="lbl">Cuenta Socio Estratégico</div>
      <div class="v {cls_sal}">{_m(sal["restante"])}</div>
      <div class="de">de {_m(sal["carga"])} · avisa bajo {_m(sal["aviso_en"])}</div>
      <div class="g"><i class="{cls_sal}" style="width:{pct_sal:.0f}%"></i></div></div>
    <div class="k"><div class="lbl">Margen acumulado</div>
      <div class="v">{_m(plata["margen"])}</div>
      <div class="de">{pct_mar:.0f}% sobre {_m(plata["facturado"])} facturado</div>
      <div class="g"><i style="width:{min(pct_mar, 100):.0f}%"></i></div></div>"""

    # ── Cuponeras: una linea de vida por cuponera, no una bolsa unica.
    _MES = ("ene", "feb", "mar", "abr", "may", "jun",
            "jul", "ago", "sep", "oct", "nov", "dic")
    _MESL = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
             "agosto", "septiembre", "octubre", "noviembre", "diciembre")

    def _fec(iso):
        if not iso:
            return "—"
        d = date.fromisoformat(iso)
        return f"{d.day} {_MES[d.month - 1]}"

    cups, dur_ref = [], []
    for c in e.get("cuponeras", []):
        cls = {"agotada": "agotada", "en uso": "uso"}.get(c["estado"], "nueva")
        if c.get("dias"):
            dur_ref.append((c["dias"], c["rx"]))
        celdas = "".join(f'<i class="u{" on" if k < c["usados"] else ""}"></i>'
                         for k in range(c["rx"]))
        if c["estado"] == "agotada":
            ritmo_c = c["rx"] / max(c["dias"] or 1, 1)
            pie = (f'Duró <b>{c["dias"]} días</b> — {ritmo_c:.1f} cupones por día. '
                   f'Se agotó el {_fec(c["agotada"])}.')
        elif c["estado"] == "en uso":
            pie = (f'Lleva <b>{c["usados"]} de {c["rx"]}</b>. '
                   f'Primer uso: {_fec(c["primer_uso"])}.')
        else:
            pie = "Sin abrir. Entra en uso cuando se agote la anterior."
        if c.get("antes_de_entrega"):
            n_ant = c["antes_de_entrega"]
            pie += (f' <span class="alerta">{n_ant} cupón'
                    f'{"es" if n_ant > 1 else ""} se usó antes de que llegara</span> '
                    f'— se siguió trabajando a cuenta de esta cuponera.')
        if c.get("sobregiro"):
            pie += (f' <span class="alerta">{c["sobregiro"]} sin cuponera que los '
                    f'cubra.</span>')
        cups.append(
            f'<div class="c {cls}"><div class="l1">'
            f'<span class="tit">Cuponera {c["n"]}</span>'
            f'<span class="est">{c["estado"]}</span>'
            f'<span class="fechas">entregada {_fec(c["entregada"])}</span>'
            f'<span class="dur">{c["usados"]}/{c["rx"]}</span></div>'
            f'<div class="celdas">{celdas}</div>'
            f'<div class="pie">{pie}</div></div>')

    # Proyeccion con las cuponeras YA agotadas: es el unico ritmo comprobado.
    if dur_ref:
        _rpd = sum(rx for _, rx in dur_ref) / sum(d for d, _ in dur_ref)
        _quedan = oro["rx_restantes"]
        _dias_quedan = _quedan / _rpd if _rpd else 0
        # El VPS corre en UTC y Chile va en -4: date.today() adelanta la fecha
        # despues de las 20:00 locales. Misma trampa que comparar dias abiertos.
        _fin = datetime.now(_CHILE_TZ).date() + timedelta(days=round(_dias_quedan))
        proy = (f'Las cuponeras ya agotadas se gastaron a <b>{_rpd:.1f} cupones por '
                f'día</b>. A ese ritmo los <b>{_quedan}</b> que quedan alcanzan hasta '
                f'cerca del <b>{_fec(_fin.isoformat())}</b> ({_dias_quedan:.0f} días).')
    else:
        proy = ('Todavía no se agota ninguna cuponera completa, así que no hay un '
                'ritmo comprobado con el cual proyectar cuándo pedir la próxima.')

    # ── Mapa calendario: cada dia muestra en que cuponera vamos, cuantos
    #    cupones salieron ESE dia y a que acumulado llego esa cuponera.
    #    El CBCT va en su propio distintivo: no gasta cupon.
    dias_log = e.get("dias", {})
    entregas = {x["fecha"]: x["cuponeras"] for x in ENTREGAS_ORO}
    _ORD = {1: "1°", 2: "2°", 3: "3°"}
    meses_html = []
    if dias_log or entregas:
        _todas = sorted(set(dias_log) | set(entregas))
        _ini = date.fromisoformat(_todas[0]).replace(day=1)
        _hoy_cl = datetime.now(_CHILE_TZ).date()
        _ult = max(date.fromisoformat(_todas[-1]), _hoy_cl)
        _cur = _ini
        while _cur <= _ult:
            # Lunes de la semana en que cae el dia 1, hasta cubrir el mes.
            _primer = _cur
            _sig = (_cur.replace(day=28) + timedelta(days=4)).replace(day=1)
            _cursor = _primer - timedelta(days=_primer.weekday())
            celdas = []
            while _cursor < _sig:
                if _cursor.month != _cur.month:
                    celdas.append('<div class="d off"></div>')
                else:
                    iso = _cursor.isoformat()
                    d = dias_log.get(iso)
                    ent = entregas.get(iso)
                    cls, cuerpo, extra = "nada", "", ""
                    if d and d["cupones"]:
                        cls = f'act c{d["cuponera"]}'
                        cuerpo = (f'<div class="mas">+{d["cupones"]}</div>'
                                  f'<div class="cup">{d["acum"]}/{RX_POR_TRAMO}</div>')
                        extra += f'<b class="ord">{_ORD.get(d["cuponera"], "?")}</b>'
                        if d["cruce"]:
                            extra += '<div class="hito"></div>'
                    if d and d["cbct"]:
                        extra += f'<div class="cb">CB {d["cbct"]}</div>'
                    if ent:
                        cls += " entrega"
                        extra += f'<div class="ent">+{ent} 📦</div>'
                    celdas.append(
                        f'<div class="d {cls}"><div class="dn">{_cursor.day}</div>'
                        f'{cuerpo}{extra}</div>')
                _cursor += timedelta(days=1)
            meses_html.append(
                f'<div class="mes"><h3>{_MESL[_cur.month - 1]} {_cur.year}</h3>'
                f'<div class="dow"><span>L</span><span>M</span><span>M</span>'
                f'<span>J</span><span>V</span><span>S</span><span>D</span></div>'
                f'<div class="grid">{"".join(celdas)}</div></div>')
            _cur = _sig
    cal = "".join(meses_html) or '<div class="vacio">Sin movimientos todavía.</div>'

    # ── Ritmo
    tope = max(sem) if sem else 1
    if sem:
        cols = "".join(
            f'<div class="col{" max" if v == tope else ""}"><div class="n">{v}</div>'
            f'<div class="b" style="height:{max(100 * v / max(tope, 1), 4):.0f}%"></div>'
            f'<div class="s">{lbl[i] if i < len(lbl) else ""}</div></div>'
            for i, v in enumerate(sem))
    else:
        cols = '<div class="vacio">Sin consumo para graficar todavía.</div>'

    # ── Tabla
    filas = []
    for f in e["consumo"]:
        cobrado = f["cobrado"] or f["venta"] or 0
        margen = cobrado - f["costo"]
        d = (f["venta"] or 0) - cobrado
        d_txt = f'<span class="delta">−{_m(d)}</span>' if d > 0 else '<span class="sub">—</span>'
        nopag = '' if f["pagado"] else '<span class="pill nopag">sin pagar</span>'
        filas.append(
            f'<tr><td class="n" style="text-align:left">{f["fecha"]}</td>'
            f'<td><div class="nom">{f["paciente"].strip()}{nopag}</div>'
            f'<div class="sub">{f["profesional"]}</div></td>'
            f'<td>{f["prestacion"]}</td>'
            f'<td><span class="pill {f["bolsa"]}">{f["bolsa"]}</span></td>'
            f'<td class="n">{f["unidades"] or "—"}</td>'
            f'<td class="n">{_m(f["costo"])}</td>'
            f'<td class="n">{_m(cobrado)}</td>'
            f'<td class="n">{d_txt}</td>'
            f'<td class="n"><b>{_m(margen)}</b></td></tr>')
    tabla = "".join(filas) or ('<tr><td colspan="9" class="vacio">Sin consumo registrado. '
                               'Usa el botón "Barrer ahora".</td></tr>')

    ult_txt = (f'{us.get("corrio", "?")[:16].replace("T", " ")} · {us.get("atenciones", "?")} '
               f'atenciones revisadas · {us.get("nuevos", 0)} nuevas') if us else "nunca"

    return HTMLResponse(f"""<!doctype html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Convenio Imagendent · Alma</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@500;600;700;800&display=swap" rel="stylesheet">
<style>{_CSS}</style></head><body>
<div class="wrap">

  <div class="top">
    <div><h1>Convenio Imagendent</h1>
      <div class="meta">Radiología dental · se mide lo <b>realizado en Medilink</b>, no los
      vales emitidos · último barrido: {ult_txt}</div></div>
    <div class="sp"></div>
    <button class="btn" id="bs" onclick="sync()">Barrer ahora</button>
    <span id="msg"></span>
  </div>

  {incompleto}{prueba}

  <div class="hero {h_cls}">
    <div class="est"><span class="dot"></span>{h_est}</div>
    <div class="cifra">{h_cifra}</div>
    <p>{h_txt}</p>
    {h_acc}
  </div>

  <div class="hall">{"".join(hall)}</div>

  <div class="kpis">{kpis}</div>

  <div class="card">
    <h2>Cuponeras — de la entrega hasta que se gasta</h2>
    <p class="h2s">Cada cupón se imputa a la cuponera más antigua que todavía tenga
    saldo. Así se ve cuánto duró cada una de verdad, no un promedio sobre el total.</p>
    <div class="cup">{"".join(cups)}</div>
    <p class="nota">{proy}</p>
  </div>

  <div class="card">
    <h2>Mapa por día</h2>
    <p class="h2s">Cada día muestra en qué cuponera vamos, cuántos cupones salieron
    ese día y el acumulado de esa cuponera. El CBCT se cuenta aparte porque no gasta
    cupón de radiografía.</p>
    {cal}
    <div class="ley">
      <span class="l1"><i></i>Cuponera 1</span>
      <span class="l2"><i></i>Cuponera 2</span>
      <span class="l3"><i></i>Cuponera 3</span>
      <span class="lc"><i></i>CB = CBCT (aparte)</span>
      <span class="le"><i></i>📦 día de entrega</span>
      <span>Borde naranjo = ese día se cambió de cuponera</span>
    </div>
  </div>

  <div class="card">
    <h2>Ritmo de consumo</h2>
    <p class="h2s">Cupones por semana ISO. Las últimas {len(sem)} semanas.</p>
    <div class="ritmo">{cols}</div>
    <p class="nota">El consumo <b>no es plano, acelera</b>. La primera cuponera fue
    3 → 3 → 8 → 13 y se acabó de golpe en la última semana. Por eso un promedio simple
    llega tarde: hay que mirar la pendiente, no el promedio.</p>
  </div>

  <div class="card">
    <h2>Consumo registrado</h2>
    <p class="h2s">Cada línea es una atención cerrada en Medilink. La columna Δ marca si
    la caja cobró algo <b>distinto del tarifario</b>: con todo bien cargado va vacía.</p>
    <div class="scroll"><table>
      <thead><tr><th>Fecha</th><th>Paciente</th><th>Prestación</th><th>Bolsa</th>
        <th style="text-align:right">Cup.</th><th style="text-align:right">Costo</th>
        <th style="text-align:right">Cobrado</th><th style="text-align:right">Δ</th>
        <th style="text-align:right">Margen</th></tr></thead>
      <tbody>{tabla}</tbody>
    </table></div>
    <p class="nota">El pack de ortodoncia consume <b>3 cupones en una sola línea</b>:
    contar filas da un número errado. Por eso la columna "Cup." manda sobre el conteo de filas.</p>
  </div>

  <div class="card">
    <h2>Barrido</h2>
    <p class="h2s">Corre solo todas las noches a las 05:10 y barre los últimos
    {DIAS_VENTANA} días. Acá lo puedes forzar si acabas de cargar una prestación y la
    quieres ver al tiro: revisa los últimos <b>{DIAS_MANUAL} días</b>, que es lo que
    Medilink aguanta sin ponerse lento para el bot en horario de atención.</p>
    <button class="btn" onclick="sync()">Barrer ahora</button>
  </div>

</div>
<script>
// El barrido demora mas de 100s y nginx corta a los 30: se lanza y despues se
// consulta el estado, en vez de esperar una respuesta que nunca llega.
async function sync(){{
  const m=document.getElementById('msg'), b=document.getElementById('bs');
  m.textContent='Barriendo Medilink…'; if(b) b.disabled=true;
  try{{
    const r=await fetch('/alma/api/imagendent/sync{tk}',{{method:'POST'}});
    const d=await r.json();
    if(!r.ok){{ m.textContent='Error: '+(d.detail||r.status); if(b) b.disabled=false; return; }}
    if(d.estado==='corriendo'){{ m.textContent='Ya hay un barrido en curso…'; }}
    esperar();
  }}catch(e){{ m.textContent='Error de red: '+e; if(b) b.disabled=false; }}
}}
async function esperar(){{
  const m=document.getElementById('msg'), b=document.getElementById('bs');
  for(let i=0;i<90;i++){{
    await new Promise(r=>setTimeout(r,3000));
    try{{
      const d=await (await fetch('/alma/api/imagendent/sync/estado{tk}')).json();
      if(d.estado==='listo'){{
        m.textContent=`${{d.atenciones}} atenciones · ${{d.nuevos}} nuevas · ${{d.actualizados}} actualizadas`;
        setTimeout(()=>location.reload(),900); return;
      }}
      if(d.estado==='error'){{ m.textContent='Error: '+d.detalle; if(b) b.disabled=false; return; }}
    }}catch(e){{ /* la consulta puede fallar suelta, se reintenta */ }}
  }}
  m.textContent='El barrido sigue corriendo — recarga en un rato.';
  if(b) b.disabled=false;
}}
</script>
</body></html>""")
