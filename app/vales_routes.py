# -*- coding: utf-8 -*-
"""Vales digitales de convenio — Radiología Dental CMC (Imagendent).

POR QUE EXISTE
--------------
El convenio con Imagendent (punto CUARTO del anexo) exige que el paciente llegue
con "la orden utilizada por la Clinica, debidamente identificada con el timbre
exclusivo de convenio". Ese timbre en papel obliga al paciente a pasar por
Carampangue SOLO a buscar un papel — incluso cuando ya fue atendido y ya trae la
orden de su medico. Caso real 2026-08-29: paciente de Arauco con orden de
otorrino para un CONE BEAM.

El vale digital cumple la MISMA funcion del timbre: le dice al mostrador de
Imagendent "este paciente va por convenio, cobrale al saldo del CMC, no a el".
Se manda por WhatsApp y se valida abriendo un link.

⚠️ REGLA DURA: esto NO sirve si Imagendent no lo acepta POR ESCRITO. Sin ese
acuerdo el paciente llega con un PDF que nadie sabe leer y le cobran precio
publico — peor que el viaje. Confirmar formato y quien valida en mostrador.

DISENO
------
- El FOLIO es el secreto: el link de validacion es publico (Imagendent no tiene
  cuenta en nuestro sistema) pero impredecible. No expone datos clinicos: solo
  nombre, RUT, prestacion autorizada y valor a descontar.
- El vale NUNCA muestra el precio de venta al paciente. Imagendent no tiene por
  que conocer el margen del CMC.
- Cada prestacion trae su valor de convenio y a que bolsa se carga (Cuenta de
  Saldo o Cuponera Oro), porque son dos mecanismos distintos en el contrato.
"""
from __future__ import annotations

import hmac
import json as _json
from pathlib import Path as _Path
import secrets
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import ADMIN_TOKEN, OLACORE_TOKEN, CMC_TELEFONO_FIJO
from session import db, log_event

router = APIRouter()

# ── Tarifario del convenio ──────────────────────────────────────────────────
# costo  = lo que Imagendent descuenta del saldo / cuponera (anexo 2026-08-29)
# venta  = tarifario Radiologia Dental CMC acordado internamente (2026-08-12)
#          None = AUN SIN DEFINIR; la UI lo marca y pide escribir el precio.
# bolsa  = "saldo" (Cuenta de Saldo Socio Estrategico) | "oro" (cuponera Plan Oro)
PRESTACIONES: dict[str, dict] = {
    "periapical":        {"nombre": "Radiografía Periapical",                    "costo": 3500,  "venta": 5000,  "bolsa": "saldo"},
    "periapical_ant":    {"nombre": "Radiografía Periapical Sector Anterior",    "costo": 10000, "venta": None,  "bolsa": "saldo"},
    "periapical_total":  {"nombre": "Radiografía Periapical Total",              "costo": 35000, "venta": 42000, "bolsa": "saldo"},
    "cbct_unitario":     {"nombre": "CONE BEAM unitario — pieza o sector",       "costo": 35000, "venta": 49900, "bolsa": "saldo"},
    "cbct_bimaxilar":    {"nombre": "CONE BEAM Bimaxilar",                       "costo": 55000, "venta": 69900, "bolsa": "saldo"},
    "escaneo_1":         {"nombre": "Escaneo intraoral digital — 1 maxilar",     "costo": 15000, "venta": None,  "bolsa": "saldo",
                          "pide_detalle": True},
    "escaneo_2":         {"nombre": "Escaneo intraoral digital — ambos maxilares", "costo": 30000, "venta": None, "bolsa": "saldo",
                          "pide_detalle": True},
    "panoramica":        {"nombre": "Radiografía Panorámica",                    "costo": 10000, "venta": 15000, "bolsa": "oro"},
    "teleradiografia":   {"nombre": "Teleradiografía de perfil",                 "costo": 10000, "venta": 15000, "bolsa": "oro"},
    "bitewing":          {"nombre": "Bitewing",                                  "costo": 10000, "venta": 15000, "bolsa": "oro"},
    "set_ortodoncia":    {"nombre": "Set radiológico ortodoncia (bitewing + panorámica + teleradiografía)",
                          "costo": 30000, "venta": 45000, "bolsa": "oro"},
}
VIGENCIA_DIAS = 60
CARGA_INICIAL = 200_000     # anexo, PRIMERO: carga inicial de la Cuenta de Saldo
AVISO_RECARGA = 50_000      # anexo, PRIMERO: Imagendent avisa al llegar a este saldo

# ── Qué hay que pedirle a recepción SEGÚN la prestación ─────────────────────
# La solicitud no es un texto libre igual para todo: un cone beam necesita pieza
# y finalidad, un escaneo necesita ademas para que trabajo de laboratorio es
# (el anexo lo EXIGE en su punto CUARTO), y una panoramica no necesita nada.
# Sin esto Imagendent no sabe que registrar y el vale no cumple el convenio.
_FIN_CBCT = ["Implante", "Tercer molar incluido", "Endodoncia / conducto",
             "Patología o quiste", "Seno maxilar / ORL", "ATM", "Ortodoncia", "Otro"]
_FIN_ESCANEO = ["Corona", "Incrustación", "Carilla", "Puente", "Prótesis removible",
                "Placa de bruxismo", "Ortodoncia / alineadores", "Modelo de estudio"]

SOLICITUD: dict[str, list[dict]] = {
    "periapical":       [{"id": "piezas", "label": "Pieza o piezas", "req": True,
                          "ph": "ej: 2.6"}],
    "periapical_ant":   [{"id": "piezas", "label": "Piezas del sector anterior", "req": False,
                          "ph": "ej: 1.1 a 2.2"}],
    "periapical_total": [{"id": "motivo", "label": "Motivo", "req": False,
                          "ph": "ej: evaluación periodontal completa"}],
    "cbct_unitario":    [{"id": "piezas", "label": "Pieza o sector", "req": True,
                          "ph": "ej: 4.6 / maxilar derecho"},
                         {"id": "finalidad", "label": "Finalidad", "req": True,
                          "opts": _FIN_CBCT}],
    "cbct_bimaxilar":   [{"id": "finalidad", "label": "Finalidad", "req": True,
                          "opts": _FIN_CBCT},
                         {"id": "piezas", "label": "Zonas de interés", "req": False,
                          "ph": "ej: ambos maxilares, foco en 3.8 y 4.8"}],
    "escaneo_1":        [{"id": "piezas", "label": "Pieza o piezas", "req": True,
                          "ph": "ej: 2.6"},
                         {"id": "finalidad", "label": "Para qué trabajo", "req": True,
                          "opts": _FIN_ESCANEO},
                         {"id": "maxilar", "label": "Maxilar a escanear", "req": True,
                          "opts": ["Superior", "Inferior"]}],
    "escaneo_2":        [{"id": "piezas", "label": "Pieza de trabajo", "req": True,
                          "ph": "ej: 3.6"},
                         {"id": "finalidad", "label": "Para qué trabajo", "req": True,
                          "opts": _FIN_ESCANEO},
                         {"id": "nota", "label": "Por qué se necesita el antagonista", "req": False,
                          "ph": "ej: requiere relación oclusal"}],
    "panoramica":       [{"id": "motivo", "label": "Motivo", "req": False, "ph": "ej: control general"}],
    "teleradiografia":  [{"id": "motivo", "label": "Motivo", "req": False, "ph": "ej: estudio cefalométrico"}],
    "bitewing":         [{"id": "piezas", "label": "Sector", "req": False, "ph": "ej: bilateral posterior"}],
    "set_ortodoncia":   [{"id": "motivo", "label": "Motivo", "req": False, "ph": "ej: inicio de tratamiento"}],
}


def _crear_tabla() -> None:
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS vales_convenio (
                folio         TEXT PRIMARY KEY,
                creado_at     TEXT NOT NULL,
                creado_por    TEXT,
                paciente      TEXT NOT NULL,
                rut           TEXT NOT NULL,
                telefono      TEXT,
                prestacion    TEXT NOT NULL,
                prestacion_nombre TEXT NOT NULL,
                costo         INTEGER NOT NULL,
                venta         INTEGER,
                bolsa         TEXT NOT NULL,
                profesional   TEXT,
                indicado_por  TEXT,
                observaciones TEXT,
                vence_el      TEXT NOT NULL,
                estado        TEXT NOT NULL DEFAULT 'vigente',
                usado_at      TEXT,
                anulado_at    TEXT,
                nota          TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_vales_estado ON vales_convenio(estado, creado_at)")
        c.commit()


_crear_tabla()


def _ok(token: str | None) -> bool:
    if not token:
        return False
    for t in (ADMIN_TOKEN, OLACORE_TOKEN):
        if t and hmac.compare_digest(token, t):
            return True
    return False


def _fmt(n) -> str:
    return "$" + format(int(n or 0), ",").replace(",", ".")


def _estado_real(v: dict) -> str:
    """Vencido se calcula al vuelo: no hay cron que lo marque y no hace falta."""
    if v["estado"] != "vigente":
        return v["estado"]
    return "vencido" if v["vence_el"] < date.today().isoformat() else "vigente"


# ═══════════════════════ API ════════════════════════════════════════════════
@router.get("/api/vales/prestaciones")
def api_prestaciones(token: str | None = Query(None)):
    if not _ok(token):
        raise HTTPException(401, "No autorizado")
    return JSONResponse({"prestaciones": PRESTACIONES, "solicitud": SOLICITUD,
                         "vigencia_dias": VIGENCIA_DIAS})


@router.get("/api/vales/paciente")
async def api_paciente(rut: str = Query(...), token: str | None = Query(None)):
    """Autocompleta el vale desde el RUT: recepcion no deberia tipear el nombre.

    El nombre sale de Medilink (fuente de verdad de la ficha) y el telefono de
    nuestra propia base — Medilink no lo devuelve en la busqueda por RUT. Si
    Medilink no responde, igual devolvemos el telefono para no bloquear: el
    formulario permite escribir el nombre a mano.
    """
    if not _ok(token):
        raise HTTPException(401, "No autorizado")
    nombre = None
    try:
        import medilink
        p = await medilink.buscar_paciente(rut)
        if p:
            nombre = p.get("nombre")
    except Exception:                                                # noqa: BLE001
        pass                       # Medilink caido no puede tumbar el mostrador
    tel = None
    try:
        from session import get_phone_by_rut
        tel = get_phone_by_rut(rut)
    except Exception:                                                # noqa: BLE001
        pass
    return JSONResponse({"nombre": nombre, "telefono": tel,
                         "encontrado": bool(nombre)})


@router.post("/api/vales")
async def api_crear(request: Request, token: str | None = Query(None)):
    if not _ok(token):
        raise HTTPException(401, "No autorizado")
    b = await request.json()
    clave = (b.get("prestacion") or "").strip()
    p = PRESTACIONES.get(clave)
    if not p:
        raise HTTPException(400, "Prestación desconocida")
    paciente = (b.get("paciente") or "").strip()
    rut = (b.get("rut") or "").strip()
    if not paciente or not rut:
        raise HTTPException(400, "Falta nombre o RUT del paciente")
    # La solicitud se arma desde los campos propios de ESTA prestacion. El anexo
    # (CUARTO) exige piezas y finalidad para el escaneo; los demas examenes
    # tienen su propio minimo. Se valida aca, no en el navegador.
    campos = SOLICITUD.get(clave, [])
    sol = b.get("solicitud") or {}
    partes, faltan = [], []
    for c in campos:
        val = str(sol.get(c["id"]) or "").strip()
        if not val:
            if c["req"]:
                faltan.append(c["label"])
            continue
        partes.append(f'{c["label"]}: {val}')
    if faltan:
        raise HTTPException(400, "Falta " + " y ".join(faltan) + " para esta prestación")
    libre = (b.get("observaciones") or "").strip()
    if libre:
        partes.append(libre)
    obs = " · ".join(partes)

    folio = "CMC-" + secrets.token_hex(3).upper()
    hoy = date.today()
    fila = {
        "folio": folio, "creado_at": datetime.now().isoformat(timespec="seconds"),
        "creado_por": (b.get("creado_por") or "").strip() or None,
        "paciente": paciente, "rut": rut,
        "telefono": (b.get("telefono") or "").strip() or None,
        "prestacion": clave, "prestacion_nombre": p["nombre"],
        "costo": p["costo"],
        "venta": int(b["venta"]) if str(b.get("venta") or "").strip().isdigit() else p["venta"],
        "bolsa": p["bolsa"],
        "profesional": (b.get("profesional") or "").strip() or None,
        "indicado_por": (b.get("indicado_por") or "").strip() or None,
        "observaciones": obs or None,
        "vence_el": (hoy + timedelta(days=VIGENCIA_DIAS)).isoformat(),
        "estado": "vigente",
    }
    cols = ",".join(fila)
    with db() as c:
        c.execute(f"INSERT INTO vales_convenio ({cols}) VALUES ({','.join('?' * len(fila))})",
                  tuple(fila.values()))
        c.commit()
    log_event(fila["telefono"] or "sistema", "vale_convenio_emitido",
              {"folio": folio, "prestacion": clave, "costo": p["costo"], "bolsa": p["bolsa"]})
    return JSONResponse({"ok": True, "folio": folio,
                         "url": f"/vale/{folio}", "vence_el": fila["vence_el"]})


@router.get("/api/vales/lista")
def api_lista(token: str | None = Query(None), limite: int = Query(200)):
    """Vales + saldo, para que el modulo de Convenios se dibuje solo."""
    if not _ok(token):
        raise HTTPException(401, "No autorizado")
    with db() as c:
        cols = ("folio", "creado_at", "paciente", "rut", "telefono", "prestacion_nombre",
                "costo", "venta", "bolsa", "estado", "vence_el", "observaciones",
                "profesional", "indicado_por")
        rows = c.execute(f"SELECT {','.join(cols)} FROM vales_convenio "
                         f"ORDER BY creado_at DESC LIMIT ?", (limite,)).fetchall()
        gast = c.execute("""SELECT bolsa, SUM(costo) FROM vales_convenio
                            WHERE estado IN ('vigente','usado') GROUP BY bolsa""").fetchall()
    consumido = {b: t for b, t in gast}
    vales = []
    for r in rows:
        v = dict(zip(cols, tuple(r)))
        v["estado_real"] = _estado_real(v)
        v["margen"] = (v["venta"] - v["costo"]) if v["venta"] else None
        vales.append(v)
    return JSONResponse({
        "vales": vales,
        "saldo": {"carga": CARGA_INICIAL, "consumido": consumido.get("saldo", 0),
                  "restante": CARGA_INICIAL - consumido.get("saldo", 0),
                  "aviso_en": AVISO_RECARGA, "oro": consumido.get("oro", 0)},
    })


@router.post("/api/vales/{folio}/enviar")
async def api_enviar(folio: str, token: str | None = Query(None)):
    """Manda el vale por el WhatsApp DEL BOT, no por un wa.me que abre el celular.

    OJO ventana de 24 h: si el paciente no le ha escrito al bot en las ultimas
    24 horas, Meta rechaza el texto libre y `send_whatsapp` devuelve None (no
    levanta). Eso NO es un error del sistema — es la politica de WhatsApp. Se
    devuelve `enviado:false` para que el mostrador use el enlace a mano en vez
    de creer que salio.
    """
    if not _ok(token):
        raise HTTPException(401, "No autorizado")
    v = _vale(folio)
    if not v:
        raise HTTPException(404, "Vale no encontrado")
    tel = (v.get("telefono") or "").strip()
    if not tel:
        raise HTTPException(400, "Este vale no tiene teléfono registrado")
    url = f"https://agentecmc.cl/vale/{folio}"
    texto = (
        f"Hola {(v['paciente'] or '').split(' ')[0]}, le enviamos su vale para el examen "
        f"*{v['prestacion_nombre']}*.\n\n"
        f"Muéstrelo al llegar: {url}\n\n"
        f"Ya está pagado en el Centro Médico Carampangue, no debe pagar en el lugar del examen. "
        f"Válido hasta el {v['vence_el']}.\n\n"
        f"Cualquier duda, llámenos al {CMC_TELEFONO_FIJO}."
    )
    try:
        from messaging import send_whatsapp
        wamid = await send_whatsapp(tel, texto)
    except Exception as e:                                            # noqa: BLE001
        raise HTTPException(502, f"No se pudo enviar: {e}") from e
    log_event(tel, "vale_convenio_enviado", {"folio": folio, "ok": bool(wamid)})
    if not wamid:
        return JSONResponse({"enviado": False, "url": url, "motivo":
                             "El paciente no le ha escrito al bot en 24 horas, así que "
                             "WhatsApp no deja mandarle un mensaje nuevo. Cópiele el enlace."})
    return JSONResponse({"enviado": True, "url": url})


@router.post("/api/vales/{folio}/{accion}")
def api_accion(folio: str, accion: str, token: str | None = Query(None)):
    if not _ok(token):
        raise HTTPException(401, "No autorizado")
    if accion not in ("usar", "anular", "reactivar"):
        raise HTTPException(400, "Acción inválida")
    campo = {"usar": ("usado", "usado_at"), "anular": ("anulado", "anulado_at"),
             "reactivar": ("vigente", None)}[accion]
    with db() as c:
        r = c.execute("SELECT folio FROM vales_convenio WHERE folio=?", (folio,)).fetchone()
        if not r:
            raise HTTPException(404, "Vale no encontrado")
        if campo[1]:
            c.execute(f"UPDATE vales_convenio SET estado=?, {campo[1]}=? WHERE folio=?",
                      (campo[0], datetime.now().isoformat(timespec="seconds"), folio))
        else:
            c.execute("UPDATE vales_convenio SET estado='vigente', usado_at=NULL, anulado_at=NULL WHERE folio=?",
                      (folio,))
        c.commit()
    return JSONResponse({"ok": True, "folio": folio, "estado": campo[0]})


def _vale(folio: str) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM vales_convenio WHERE folio=?", (folio,)).fetchone()
        if not r:
            return None
        cols = [d[0] for d in c.execute("SELECT * FROM vales_convenio LIMIT 0").description]
    return dict(zip(cols, tuple(r)))


# ═══════════════════════ Páginas ════════════════════════════════════════════
_CSS = """
:root{--navy:#0F3F68;--azul:#1172AB;--aqua:#4FBECE;--tinta:#12303f;--gris:#5a7182;
      --papel:#f6f8fa;--linea:#d9e2e8;--ok:#2e7d5b;--warn:#9a6b12;--mal:#a33a30}
*{box-sizing:border-box}
body{margin:0;background:var(--papel);color:var(--tinta);
     font-family:'Montserrat',-apple-system,'Segoe UI',Roboto,sans-serif;line-height:1.5}
@font-face{font-family:'Montserrat';src:url('/static/fonts/montserrat-var-latin.woff2') format('woff2-variations');
           font-weight:400 800;font-display:swap}
.hoja{max-width:640px;margin:24px auto;background:#fff;border:1px solid var(--linea);
      border-radius:10px;overflow:hidden;box-shadow:0 2px 14px rgba(15,63,104,.07)}
.cab{background:var(--navy);color:#fff;padding:20px 24px;display:flex;gap:16px;align-items:center}
.cab img{height:52px;width:auto;background:#fff;border-radius:8px;padding:6px}
.cab h1{margin:0;font-size:1.05rem;letter-spacing:.02em}
.cab p{margin:3px 0 0;font-size:.78rem;color:#b9d3e4}
.folio{margin-left:auto;text-align:right;font-family:ui-monospace,monospace}
.folio b{font-size:1.15rem;letter-spacing:.06em;display:block}
.folio span{font-size:.68rem;color:#b9d3e4;text-transform:uppercase;letter-spacing:.14em}
.cuerpo{padding:24px}
.estado{display:inline-block;padding:5px 12px;border-radius:99px;font-size:.72rem;font-weight:700;
        letter-spacing:.12em;text-transform:uppercase;margin-bottom:18px}
.e-vigente{background:#e4f4ec;color:var(--ok)} .e-usado{background:#eceff1;color:var(--gris)}
.e-vencido{background:#fdf3e0;color:var(--warn)} .e-anulado{background:#fbe9e7;color:var(--mal)}
dl{margin:0;display:grid;grid-template-columns:1fr;gap:0}
dt{font-size:.68rem;text-transform:uppercase;letter-spacing:.14em;color:var(--gris);
   margin-top:14px;font-weight:600}
dd{margin:3px 0 0;font-size:1rem}
dd.grande{font-size:1.18rem;font-weight:700;color:var(--navy)}
.destacado{background:#eef6fa;border:1px solid #cfe3ef;border-left:4px solid var(--azul);
           border-radius:6px;padding:14px 16px;margin-top:20px}
.destacado .t{font-size:.68rem;text-transform:uppercase;letter-spacing:.14em;color:var(--azul);font-weight:700}
.destacado .v{font-family:ui-monospace,monospace;font-size:1.5rem;font-weight:700;color:var(--navy);margin-top:4px}
.destacado .n{font-size:.82rem;color:var(--gris);margin-top:6px}
.pie{border-top:1px solid var(--linea);padding:16px 24px;font-size:.76rem;color:var(--gris);background:#fbfcfd}
.pie b{color:var(--tinta)}
@media print{body{background:#fff}.hoja{box-shadow:none;border:0;margin:0}.noprint{display:none}}
"""

_LOGO = "/static/sitio/cropped-logo-carampangue.png"


# ── La credencial (dirección A, elegida por el dueño 2026-08-29) ────────────
# Fuentes AUTOSERVIDAS: la abre el mostrador en un telefono con señal rural, y
# la CSP prohibe data: en font-src, asi que van como archivo propio.
_CSS_VALE = """
@font-face{font-family:'Montserrat';src:url('/static/fonts/montserrat-var-latin.woff2') format('woff2-variations');
           font-weight:400 800;font-display:swap}
@font-face{font-family:'Plex Mono';src:url('/static/fonts/plexmono-500-latin.woff2') format('woff2');
           font-weight:500;font-display:swap}
@font-face{font-family:'Plex Mono';src:url('/static/fonts/plexmono-600-latin.woff2') format('woff2');
           font-weight:600;font-display:swap}
*{box-sizing:border-box}
body{margin:0;background:#f3f6f9;color:#12303f;-webkit-font-smoothing:antialiased;
     font-family:Montserrat,'Helvetica Neue',Arial,sans-serif;line-height:1.5}
.mono{font-family:'Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
.eyebrow{font-size:8.5px;font-weight:700;letter-spacing:.22em;text-transform:uppercase;color:#7d97a8}
.marco{max-width:640px;margin:0 auto;padding:28px 20px}
.cred{background:#fff;border-radius:14px;overflow:hidden;
      box-shadow:0 4px 12px rgba(15,63,104,.06),0 16px 36px rgba(15,63,104,.10)}
.cab{position:relative;background:#0F3F68;padding:26px 30px 22px;overflow:hidden}
.cab .guilloche{position:absolute;inset:0;opacity:.10;
  background:repeating-linear-gradient(58deg,transparent 0 6px,#4FBECE 6px 7px),
             repeating-linear-gradient(-58deg,transparent 0 11px,#fff 11px 12px)}
.cab .fila{position:relative;display:flex;align-items:flex-start;gap:16px}
.placa{width:56px;height:56px;background:#fff;border-radius:12px;flex:none;
       display:flex;align-items:center;justify-content:center}
.cab h1{margin:5px 0 0;font-size:23px;font-weight:700;color:#fff;letter-spacing:-.015em}
.cab .sub{font-size:11.5px;color:#a7c6da;margin-top:3px;font-weight:500}
.cab .eyebrow{color:#8fb3ca}
.folio{text-align:right;flex:none;padding-top:2px}
.folio b{display:block;font-size:21px;font-weight:600;color:#fff;letter-spacing:.08em;margin-top:5px}
.rule{height:3px;background:linear-gradient(90deg,#4FBECE 0%,#1172AB 55%,#0F3F68 100%)}
.cuerpo{padding:26px 30px 0;display:flex;flex-direction:column;gap:22px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:6px 13px;border-radius:99px;
      font-size:9.5px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}
.c-vigente{background:#e4f4ec;color:#1b7a55}
.c-usado{background:#eceff1;color:#5a7182}
.c-vencido{background:#fff4d6;color:#b58105}
.c-anulado{background:#ffebee;color:#e53935}
.dato .v{font-size:26px;font-weight:700;color:#0F3F68;letter-spacing:-.02em;margin-top:6px;text-wrap:balance}
.dato .rut{font-size:14px;color:#5a7182;margin-top:4px;letter-spacing:.04em}
.hair{height:1px;background:#e3ebf1}
.prest{font-size:19px;font-weight:600;color:#12303f;margin-top:6px;line-height:1.3;text-wrap:balance}
.obs{font-size:12.5px;color:#5a7182;margin-top:8px;line-height:1.55}
.par{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px}
.par .v{font-size:13.5px;font-weight:500;margin-top:5px;line-height:1.4}
.valor{position:relative;margin:26px 30px 0;padding:22px 24px;background:#e5f5f8;
       border:1px solid #bfe4ec;border-radius:12px;overflow:hidden}
.valor::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:#1172AB}
.valor .in{max-width:330px}
.valor .eyebrow{color:#1172AB}
.valor .n{font-size:40px;font-weight:600;color:#0F3F68;letter-spacing:-.02em;line-height:1.05;margin-top:8px}
.valor .b{font-size:11.5px;color:#3d6076;margin-top:9px;line-height:1.5}
.valor .b strong{color:#0F3F68}
.timbre{position:absolute;right:14px;top:50%;transform:translateY(-50%) rotate(-11deg);opacity:.9;pointer-events:none}
.aviso{margin:16px 30px 0;padding:15px 18px;border:1px solid #e3ebf1;border-radius:10px;background:#f7fafc;
       display:flex;gap:13px;align-items:flex-start}
.aviso div{font-size:12.5px;line-height:1.6;color:#3d6076}
.aviso strong{color:#0F3F68}
.pie{margin-top:22px;border-top:1px solid #e3ebf1;padding:16px 30px 18px;background:#fbfcfd}
.pie .f{display:flex;gap:18px;align-items:flex-end}
.pie .txt{font-size:10.5px;color:#5a7182;line-height:1.6}
.pie .txt strong{color:#12303f}
.pie .url{font-size:10px;color:#8aa2b2;margin-top:6px}
.micro{margin-top:12px;font-size:5.5px;letter-spacing:.34em;color:#c3d3de;white-space:nowrap;overflow:hidden}
@media(max-width:560px){
  .cab,.cuerpo,.pie{padding-left:20px;padding-right:20px}
  .valor,.aviso{margin-left:20px;margin-right:20px}
  .cab .fila{flex-wrap:wrap}
  .folio{text-align:left;width:100%;padding-top:10px}
  .timbre{position:static;transform:rotate(-6deg);margin:18px auto 0;display:block;width:104px}
  .valor .in{max-width:none}
  .valor .n{font-size:34px}
}
@media print{body{background:#fff}.cred{box-shadow:none;border:1px solid #e3ebf1}.marco{padding:0}}
"""

# Cada estado tiene su timbre: el color y la palabra cambian, la forma no.
_TIMBRE = {
    "vigente": ("#1172AB", "#0F3F68", "VÁLIDO"),
    "usado":   ("#7d97a8", "#5a7182", "UTILIZADO"),
    "vencido": ("#b58105", "#8a6410", "VENCIDO"),
    "anulado": ("#e53935", "#b32b26", "ANULADO"),
}


def _svg_timbre(estado: str, fecha: str) -> str:
    aro, tinta, palabra = _TIMBRE.get(estado, _TIMBRE["vigente"])
    return f"""<svg class="timbre" width="128" height="128" viewBox="0 0 128 128" aria-hidden="true">
  <defs><path id="orb{estado}" d="M64,64 m-47,0 a47,47 0 1,1 94,0 a47,47 0 1,1 -94,0"/></defs>
  <circle cx="64" cy="64" r="59" fill="none" stroke="{aro}" stroke-width="2.5"/>
  <circle cx="64" cy="64" r="53.5" fill="none" stroke="{aro}" stroke-width="1"/>
  <circle cx="64" cy="64" r="37" fill="none" stroke="{aro}" stroke-width="1"/>
  <text font-family="Montserrat,sans-serif" font-size="8.4" font-weight="700" letter-spacing="2.1" fill="{aro}">
    <textPath href="#orb{estado}" startOffset="50%" text-anchor="middle">CENTRO MÉDICO CARAMPANGUE · CONVENIO ·</textPath>
  </text>
  <path d="M61.2 41h5.6v5.4h5.4v5.6h-5.4v5.4h-5.6v-5.4H55.8v-5.6h5.4z" fill="{aro}" opacity=".55"/>
  <text x="64" y="69" text-anchor="middle" font-family="Montserrat,sans-serif" font-size="12" font-weight="800" letter-spacing="1.2" fill="{tinta}">{palabra}</text>
  <text x="64" y="84" text-anchor="middle" font-family="'Plex Mono',monospace" font-size="9.5" font-weight="600" letter-spacing=".8" fill="{aro}">{fecha}</text>
</svg>"""


_CRUZ = ('<svg width="34" height="34" viewBox="0 0 40 40" aria-label="Centro Médico Carampangue">'
         '<path d="M16 5h8v11h11v8H24v11h-8V24H5v-8h11z" fill="#1172AB"/>'
         '<path d="M16 5h8v11h11v8H24v11h-8V24H5v-8h11z" fill="none" stroke="#4FBECE" stroke-width="2.4"/></svg>')


def _dmy(iso: str) -> str:
    try:
        a, m, d = iso[:10].split("-")
        return f"{d}·{m}·{a}"
    except Exception:
        return iso[:10]


@router.get("/vale/{folio}", response_class=HTMLResponse)
def ver_vale(folio: str):
    """Pagina PUBLICA de validacion: la abre el mostrador de Imagendent.

    Sin token a proposito — Imagendent no tiene cuenta en nuestro sistema. El
    folio (6 hex aleatorios) es el secreto. No expone nada clinico mas alla de
    la prestacion autorizada, y **NUNCA el precio de venta**: el margen del CMC
    no es asunto del proveedor.
    """
    v = _vale(folio)
    if not v:
        return HTMLResponse(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex, nofollow">
<title>Vale no encontrado</title><style>{_CSS_VALE}</style></head><body><div class="marco"><div class="cred">
<div class="cab"><div class="guilloche"></div><div class="fila"><div class="placa">{_CRUZ}</div>
<div><div class="eyebrow">Centro Médico Carampangue</div><h1>Vale no encontrado</h1>
<div class="sub">Verifique el folio</div></div></div></div><div class="rule"></div>
<div class="cuerpo" style="padding-bottom:26px"><p style="font-size:13px;color:#5a7182">
Este folio no corresponde a ningún vale emitido por Centro Médico Carampangue.</p></div>
</div></div></body></html>""", status_code=404)

    est = _estado_real(v)
    bolsa = ("Cuenta de Saldo Socio Estratégico" if v["bolsa"] == "saldo"
             else "Cuponera Plan Socio Estratégico Oro")
    obs = f'<div class="obs">{v["observaciones"]}</div>' if v["observaciones"] else ""
    ind = (f'<div><div class="eyebrow">Indicación original</div>'
           f'<div class="v">{v["indicado_por"]}</div></div>') if v["indicado_por"] else ""
    tick = ('<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>')

    return HTMLResponse(f"""<!doctype html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Vale {v['folio']} · Centro Médico Carampangue</title>
<style>{_CSS_VALE}</style></head><body>
<div class="marco"><div class="cred">

  <div class="cab"><div class="guilloche"></div>
    <div class="fila">
      <div class="placa">{_CRUZ}</div>
      <div style="flex-grow:1;padding-top:2px">
        <div class="eyebrow">Centro Médico Carampangue</div>
        <h1>Vale de convenio</h1>
        <div class="sub">Radiología Dental · Plan Socio Estratégico</div>
      </div>
      <div class="folio"><div class="eyebrow">Folio</div><b class="mono">{v['folio']}</b></div>
    </div>
  </div>
  <div class="rule"></div>

  <div class="cuerpo">
    <div style="display:flex;align-items:center;gap:14px">
      <span class="chip c-{est}">{tick if est == 'vigente' else ''}{est}</span>
      <div style="margin-left:auto;text-align:right">
        <div class="eyebrow">Válido hasta</div>
        <div class="mono" style="font-size:13px;font-weight:600;color:#0F3F68;margin-top:2px">{_dmy(v['vence_el'])}</div>
      </div>
    </div>

    <div class="dato">
      <div class="eyebrow">Paciente</div>
      <div class="v">{v['paciente']}</div>
      <div class="rut mono">{v['rut']}</div>
    </div>

    <div class="hair"></div>

    <div>
      <div class="eyebrow">Prestación autorizada</div>
      <div class="prest">{v['prestacion_nombre']}</div>
      {obs}
    </div>

    <div class="par">
      <div><div class="eyebrow">Profesional que deriva</div>
        <div class="v">{v['profesional'] or 'Centro Médico Carampangue'}</div></div>
      {ind}
    </div>
  </div>

  <div class="valor">
    <div class="in">
      <div class="eyebrow">Valor a descontar del convenio</div>
      <div class="n mono">{_fmt(v['costo'])}</div>
      <div class="b">Con cargo a <strong>{bolsa}</strong></div>
    </div>
    {_svg_timbre(est, _dmy(v['creado_at']))}
  </div>

  <div class="aviso">
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="#1172AB" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round" style="flex:none;margin-top:1px">
      <circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
    <div>El paciente <strong>ya pagó esta prestación</strong> en Centro Médico Carampangue.
    <strong>No debe pagar en mostrador</strong>: el valor se descuenta del convenio.</div>
  </div>

  <div class="pie">
    <div class="f">
      <div style="flex-grow:1">
        <div class="txt">Emitido por <strong>Centro Médico Carampangue</strong> ·
          Convenio de Colaboración Clínica Aliada – Plan Socio Estratégico</div>
        <div class="txt" style="margin-top:5px">¿Dudas antes de atender?
          Llame al <strong class="mono">{CMC_TELEFONO_FIJO}</strong></div>
        <div class="url mono">Verificar en agentecmc.cl/vale/{v['folio']}</div>
      </div>
      <div style="text-align:right;flex:none">
        <div class="eyebrow" style="font-size:7.5px">Emitido</div>
        <div class="mono" style="font-size:11px;color:#5a7182;margin-top:3px">{_dmy(v['creado_at'])}</div>
      </div>
    </div>
    <div class="micro mono" aria-hidden="true">{'CMC·' * 40}</div>
  </div>

</div></div></body></html>""")


# ── Modulo CONVENIOS de recepcion ──────────────────────────────────────────
_TPL = _Path(__file__).resolve().parent.parent / "templates"


def _tpl(nombre: str) -> str:
    f = _TPL / nombre
    return f.read_text(encoding="utf-8") if f.exists() else ""


@router.get("/recepcion/convenios", response_class=HTMLResponse)
def convenios_index(token: str | None = Query(None)):
    """Portada del modulo: un convenio por tarjeta, con su saldo en vivo."""
    if not _ok(token):
        raise HTTPException(401, "No autorizado")
    html = _tpl("recepcion_convenios.html")
    if not html:
        raise HTTPException(404, "Módulo no disponible")
    return HTMLResponse(html.replace("__TOKEN__", token or ""),
                        headers={"Cache-Control": "no-store"})


@router.get("/recepcion/convenios/imagendent", response_class=HTMLResponse)
def convenio_imagendent(token: str | None = Query(None)):
    """Mesa de trabajo del convenio Imagendent: emitir, enviar y llevar el saldo.

    El tarifario y los campos de solicitud se inyectan como JSON para que el
    formulario se dibuje solo — si cambia PRESTACIONES o SOLICITUD, la pagina
    cambia sola y no hay dos verdades.
    """
    if not _ok(token):
        raise HTTPException(401, "No autorizado")
    html = _tpl("recepcion_convenio_imagendent.html")
    if not html:
        raise HTTPException(404, "Módulo no disponible")
    html = (html.replace("__TOKEN__", token or "")
                .replace("__PRESTACIONES__", _json.dumps(PRESTACIONES, ensure_ascii=False))
                .replace("__SOLICITUD__", _json.dumps(SOLICITUD, ensure_ascii=False)))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/vales", response_class=HTMLResponse)
def panel_vales(token: str | None = Query(None)):
    if not _ok(token):
        raise HTTPException(401, "No autorizado")
    with db() as c:
        rows = c.execute("""SELECT folio,creado_at,paciente,rut,prestacion_nombre,costo,venta,
                                   bolsa,estado,vence_el,telefono
                            FROM vales_convenio ORDER BY creado_at DESC LIMIT 200""").fetchall()
        gast = c.execute("""SELECT bolsa, SUM(costo) FROM vales_convenio
                            WHERE estado IN ('vigente','usado') GROUP BY bolsa""").fetchall()
    consumido = {b: t for b, t in gast}
    saldo_rest = CARGA_INICIAL - consumido.get("saldo", 0)
    filas = []
    for r in rows:
        v = dict(zip(("folio", "creado_at", "paciente", "rut", "prestacion_nombre", "costo",
                      "venta", "bolsa", "estado", "vence_el", "telefono"), tuple(r)))
        est = _estado_real(v)
        margen = (v["venta"] - v["costo"]) if v["venta"] else None
        wa = (f"<a class='btn' target='_blank' href='https://wa.me/{v['telefono']}"
              f"?text=Tu%20vale%20de%20convenio%3A%20https%3A%2F%2Fagentecmc.cl%2Fvale%2F{v['folio']}'>WhatsApp</a>"
              if v["telefono"] else "")
        filas.append(
            f"<tr><td><a href='/vale/{v['folio']}' target='_blank'>{v['folio']}</a></td>"
            f"<td>{v['creado_at'][:10]}</td><td>{v['paciente']}<br><small>{v['rut']}</small></td>"
            f"<td>{v['prestacion_nombre']}</td><td class='n'>{_fmt(v['costo'])}</td>"
            f"<td class='n'>{_fmt(v['venta']) if v['venta'] else '<i>sin definir</i>'}</td>"
            f"<td class='n'>{_fmt(margen) if margen is not None else '—'}</td>"
            f"<td><span class='estado e-{est}'>{est}</span></td>"
            f"<td>{wa} <button class='btn' onclick=\"acc('{v['folio']}','usar')\">Usado</button>"
            f"<button class='btn' onclick=\"acc('{v['folio']}','anular')\">Anular</button></td></tr>")
    opciones = "".join(
        f"<option value='{k}'>{p['nombre']} — {_fmt(p['costo'])}"
        + ("" if p["venta"] else "  ⚠ sin precio de venta") + "</option>"
        for k, p in PRESTACIONES.items())
    return HTMLResponse((f"""<!doctype html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>Vales de convenio · CMC</title>
<style>{_CSS}
.wrap{{max-width:1180px;margin:24px auto;padding:0 16px}}
.hd{{display:flex;gap:14px;align-items:center;margin-bottom:18px}}
.hd img{{height:44px}} .hd h1{{margin:0;font-size:1.3rem;color:var(--navy)}}
.kpis{{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));margin-bottom:18px}}
.kpi{{background:#fff;border:1px solid var(--linea);border-left:3px solid var(--aqua);
      border-radius:8px;padding:14px}}
.kpi b{{display:block;font-family:ui-monospace,monospace;font-size:1.4rem;color:var(--navy)}}
.kpi span{{font-size:.74rem;color:var(--gris)}}
.card{{background:#fff;border:1px solid var(--linea);border-radius:10px;padding:18px;margin-bottom:18px}}
.g{{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}}
label{{display:block;font-size:.7rem;text-transform:uppercase;letter-spacing:.12em;
       color:var(--gris);font-weight:600;margin-bottom:4px}}
input,select,textarea{{width:100%;padding:9px 10px;border:1px solid var(--linea);
       border-radius:6px;font:inherit;background:#fff}}
.btn{{background:var(--azul);color:#fff;border:0;border-radius:6px;padding:8px 14px;
      font-size:.8rem;cursor:pointer;text-decoration:none;display:inline-block}}
.btn:hover{{background:var(--navy)}}
table{{width:100%;border-collapse:collapse;background:#fff;font-size:.85rem}}
th,td{{padding:9px 10px;border-bottom:1px solid var(--linea);text-align:left;vertical-align:top}}
th{{font-size:.68rem;text-transform:uppercase;letter-spacing:.1em;color:var(--gris)}}
td.n{{font-family:ui-monospace,monospace;text-align:right}}
.aviso{{background:#fdf3e0;border:1px solid #f0dcb4;border-left:4px solid var(--warn);
        border-radius:6px;padding:12px 14px;font-size:.85rem;margin-bottom:18px}}
#msg{{margin-top:10px;font-size:.85rem}}
</style></head><body><div class="wrap">
<div class="hd"><img src="{_LOGO}" alt=""><h1>Vales de convenio · Radiología Dental</h1></div>
<div class="aviso"><b>Antes de usar esto con un paciente:</b> Imagendent tiene que
aceptar el vale digital <b>por escrito</b> y decir quién lo valida en mostrador.
Sin eso, el paciente llega con un link que nadie sabe leer y le cobran precio público.</div>
<div class="kpis">
  <div class="kpi"><b>{_fmt(saldo_rest)}</b><span>Saldo estimado de la Cuenta Socio Estratégico<br>(carga $200.000 − vales emitidos)</span></div>
  <div class="kpi"><b>{_fmt(consumido.get('saldo',0))}</b><span>Consumido de la Cuenta de Saldo</span></div>
  <div class="kpi"><b>{_fmt(consumido.get('oro',0))}</b><span>Consumido de la Cuponera Oro</span></div>
  <div class="kpi"><b>{len(rows)}</b><span>Vales emitidos</span></div>
</div>
<div class="card"><h2 style="margin:0 0 14px;font-size:1rem">Emitir vale</h2>
  <p style="font-size:.84rem;color:var(--gris);margin:-6px 0 14px">
    Escriba el RUT y presione Enter: el nombre y el teléfono se completan solos desde la ficha.</p>
  <div class="g">
    <div><label>RUT del paciente</label>
      <input id="rut" placeholder="12.345.678-9" autofocus>
      <span id="rutmsg" style="font-size:.74rem;color:var(--gris)"></span></div>
    <div><label>Paciente</label><input id="paciente" placeholder="Se completa solo"></div>
    <div><label>Teléfono (para enviar por WhatsApp)</label><input id="telefono" placeholder="Se completa solo"></div>
    <div><label>Prestación</label><select id="prestacion">{opciones}</select></div>
    <div><label>Profesional que deriva</label><input id="profesional" placeholder="Dr. Rodrigo Olavarría"></div>
    <div><label>Indicación original (si viene de otro médico)</label><input id="indicado_por" placeholder="Orden de Dr. X, otorrinolaringología"></div>
    <div><label>Precio de venta al paciente (si la prestación no lo trae)</label><input id="venta" inputmode="numeric" placeholder="49900"></div>
  </div>
  <div id="solicitud" style="margin-top:16px;padding-top:16px;border-top:1px dashed var(--linea)">
    <div style="font-family:ui-monospace,monospace;font-size:9px;letter-spacing:.14em;
                text-transform:uppercase;color:var(--gris);margin-bottom:10px">Solicitud del examen</div>
    <div class="g" id="solcampos"></div>
    <div style="margin-top:12px"><label>Observaciones adicionales (opcional)</label>
      <input id="observaciones" placeholder="Cualquier cosa que Imagendent deba saber"></div>
  </div>
  <div style="margin-top:14px"><button class="btn" onclick="crear()">Emitir vale</button></div>
  <div id="msg"></div>
</div>
<table><thead><tr><th>Folio</th><th>Fecha</th><th>Paciente</th><th>Prestación</th>
<th class="n">Costo</th><th class="n">Venta</th><th class="n">Margen</th><th>Estado</th><th></th></tr></thead>
<tbody>{''.join(filas) or '<tr><td colspan=9>Todavía no hay vales emitidos.</td></tr>'}</tbody></table>
</div>
<script>
const T = new URLSearchParams(location.search).get('token');
const SOL = __SOLICITUD_JSON__;
function pintarSolicitud(){{
  const k = document.getElementById('prestacion').value;
  const campos = SOL[k] || [];
  document.getElementById('solcampos').innerHTML = campos.map(c => {{
    const req = c.req ? ' <span style="color:#a33a30">*</span>' : '';
    const inp = c.opts
      ? '<select data-sol="' + c.id + '"><option value="">— elegir —</option>' +
        c.opts.map(o => '<option>' + o + '</option>').join('') + '</select>'
      : '<input data-sol="' + c.id + '" placeholder="' + (c.ph || '') + '">';
    return '<div><label>' + c.label + req + '</label>' + inp + '</div>';
  }}).join('') || '<div style="font-size:.82rem;color:var(--gris)">Esta prestación no necesita detalle.</div>';
}}
document.getElementById('prestacion').addEventListener('change', pintarSolicitud);
pintarSolicitud();

async function crear(){{
  const b = {{}};
  ['paciente','rut','telefono','prestacion','profesional','indicado_por','venta','observaciones']
    .forEach(k => b[k] = (document.getElementById(k).value || '').trim());
  b.solicitud = {{}};
  document.querySelectorAll('[data-sol]').forEach(el => {{
    b.solicitud[el.dataset.sol] = (el.value || '').trim();
  }});
  const m = document.getElementById('msg');
  m.textContent = 'Emitiendo…';
  const r = await fetch('/api/vales?token=' + encodeURIComponent(T),
    {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(b)}});
  const d = await r.json().catch(() => ({{}}));
  if (!r.ok) {{ m.style.color = '#a33a30'; m.textContent = d.detail || 'No se pudo emitir'; return; }}
  const url = location.origin + '/vale/' + d.folio;
  const tel = (b.telefono || '').replace(/\\D/g, '');
  const wa = tel
    ? '<a class="btn" style="background:#128c7e" target="_blank" href="https://wa.me/' + tel +
      '?text=' + encodeURIComponent('Su vale para el examen: ' + url) + '">Enviar por WhatsApp</a>'
    : '<span style="color:var(--gris);font-size:.82rem">Sin teléfono: copie el enlace y mándelo a mano.</span>';
  m.style.color = 'inherit';
  m.innerHTML =
    '<div style="border:1px solid var(--linea);border-left:3px solid var(--ok);border-radius:8px;' +
    'padding:14px 16px;margin-top:4px;background:#fff">' +
    '<div style="font-weight:600;color:var(--ok)">Vale ' + d.folio + ' emitido</div>' +
    '<div style="font-family:ui-monospace,monospace;font-size:.8rem;margin:8px 0 12px">' + url + '</div>' +
    '<div style="display:flex;gap:8px;flex-wrap:wrap">' + wa +
    '<a class="btn sec" target="_blank" href="/vale/' + d.folio + '">Ver el vale</a>' +
    '<button class="btn sec" onclick="location.reload()">Emitir otro</button></div></div>';
  ['rut','paciente','telefono','observaciones'].forEach(k => document.getElementById(k).value = '');
  document.querySelectorAll('[data-sol]').forEach(el => el.value = '');
}}
async function buscarRut(){{
  const rut = document.getElementById('rut').value.trim();
  const msg = document.getElementById('rutmsg');
  if (rut.replace(/\\D/g, '').length < 7) {{ msg.textContent = ''; return; }}
  msg.textContent = 'Buscando…';
  try {{
    const r = await fetch('/api/vales/paciente?rut=' + encodeURIComponent(rut) +
                          '&token=' + encodeURIComponent(T));
    const d = await r.json();
    if (d.nombre) document.getElementById('paciente').value = d.nombre;
    if (d.telefono) document.getElementById('telefono').value = d.telefono;
    msg.style.color = d.encontrado ? '#2e7d5b' : '#9a6b12';
    msg.textContent = d.encontrado
      ? ('Ficha encontrada' + (d.telefono ? ' · teléfono cargado' : ' · sin teléfono en el bot'))
      : 'No está en Medilink: escriba el nombre a mano.';
  }} catch (e) {{ msg.style.color = '#9a6b12'; msg.textContent = 'No se pudo consultar la ficha.'; }}
}}
document.getElementById('rut').addEventListener('blur', buscarRut);
document.getElementById('rut').addEventListener('keydown', e => {{
  if (e.key === 'Enter') {{ e.preventDefault(); buscarRut(); }}
}});

async function acc(folio, a){{
  await fetch('/api/vales/' + folio + '/' + a + '?token=' + encodeURIComponent(T), {{method:'POST'}});
  location.reload();
}}
</script></body></html>""").replace("__SOLICITUD_JSON__", _json.dumps(SOLICITUD, ensure_ascii=False)))
