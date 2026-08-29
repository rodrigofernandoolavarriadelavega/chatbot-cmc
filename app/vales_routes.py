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
import json
import secrets
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import ADMIN_TOKEN, OLACORE_TOKEN
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
    return JSONResponse({"prestaciones": PRESTACIONES, "vigencia_dias": VIGENCIA_DIAS})


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
    # El anexo (CUARTO) EXIGE detallar piezas y finalidad para el escaneo: sin eso
    # Imagendent no sabe que registrar y el vale no cumple el convenio.
    obs = (b.get("observaciones") or "").strip()
    if p.get("pide_detalle") and len(obs) < 5:
        raise HTTPException(400, "El escaneo intraoral exige indicar piezas y finalidad (punto CUARTO del anexo)")

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


@router.get("/vale/{folio}", response_class=HTMLResponse)
def ver_vale(folio: str):
    """Pagina PUBLICA de validacion: la abre el mostrador de Imagendent.

    Sin token a proposito — Imagendent no tiene cuenta en nuestro sistema. El
    folio (6 hex aleatorios) es el secreto. No expone nada clinico: nombre, RUT,
    prestacion autorizada y el valor a descontar. **NUNCA el precio de venta.**
    """
    v = _vale(folio)
    if not v:
        return HTMLResponse(
            f"<style>{_CSS}</style><div class='hoja'><div class='cab'>"
            f"<img src='{_LOGO}' alt='Centro Médico Carampangue'>"
            "<div><h1>Vale no encontrado</h1><p>Verifique el folio</p></div></div>"
            "<div class='cuerpo'><p>Este folio no corresponde a ningún vale emitido "
            "por Centro Médico Carampangue.</p></div></div>", status_code=404)
    est = _estado_real(v)
    bolsa = ("Cuenta de Saldo Socio Estratégico" if v["bolsa"] == "saldo"
             else "Cuponera Plan Socio Estratégico Oro")
    obs = (f"<dt>Observaciones clínicas</dt><dd>{v['observaciones']}</dd>"
           if v["observaciones"] else "")
    ind = (f"<dt>Indicación original</dt><dd>{v['indicado_por']}</dd>"
           if v["indicado_por"] else "")
    return HTMLResponse(f"""<!doctype html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Vale {v['folio']} · Centro Médico Carampangue</title><style>{_CSS}</style></head><body>
<div class="hoja">
  <div class="cab">
    <img src="{_LOGO}" alt="Centro Médico Carampangue">
    <div><h1>Vale de convenio</h1><p>Radiología Dental · Centro Médico Carampangue</p></div>
    <div class="folio"><span>Folio</span><b>{v['folio']}</b></div>
  </div>
  <div class="cuerpo">
    <span class="estado e-{est}">{est}</span>
    <dl>
      <dt>Paciente</dt><dd class="grande">{v['paciente']}</dd>
      <dt>RUT</dt><dd>{v['rut']}</dd>
      <dt>Prestación autorizada</dt><dd class="grande">{v['prestacion_nombre']}</dd>
      {obs}
      <dt>Profesional que deriva</dt><dd>{v['profesional'] or 'Centro Médico Carampangue'}</dd>
      {ind}
      <dt>Emitido</dt><dd>{v['creado_at'][:10]}</dd>
      <dt>Válido hasta</dt><dd>{v['vence_el']}</dd>
    </dl>
    <div class="destacado">
      <div class="t">Valor a descontar del convenio</div>
      <div class="v">{_fmt(v['costo'])}</div>
      <div class="n">Con cargo a: <b>{bolsa}</b>. El paciente ya pagó la prestación
      en Centro Médico Carampangue — <b>no debe pagar en mostrador</b>.</div>
    </div>
  </div>
  <div class="pie">
    Emitido por <b>Centro Médico Carampangue</b> · Convenio de Colaboración Clínica
    Aliada – Plan Socio Estratégico.<br>
    Ante cualquier duda sobre este vale, contactar al centro médico antes de
    atender al paciente.
  </div>
</div></body></html>""")


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
    saldo_rest = 200000 - consumido.get("saldo", 0)
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
    return HTMLResponse(f"""<!doctype html><html lang="es"><head>
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
  <div class="g">
    <div><label>Paciente</label><input id="paciente" placeholder="Nombre y apellidos"></div>
    <div><label>RUT</label><input id="rut" placeholder="12.345.678-9"></div>
    <div><label>Teléfono (para enviar por WhatsApp)</label><input id="telefono" placeholder="56912345678"></div>
    <div><label>Prestación</label><select id="prestacion">{opciones}</select></div>
    <div><label>Profesional que deriva</label><input id="profesional" placeholder="Dr. Rodrigo Olavarría"></div>
    <div><label>Indicación original (si viene de otro médico)</label><input id="indicado_por" placeholder="Orden de Dr. X, otorrinolaringología"></div>
    <div><label>Precio de venta al paciente (si la prestación no lo trae)</label><input id="venta" inputmode="numeric" placeholder="49900"></div>
    <div style="grid-column:1/-1"><label>Observaciones — piezas y finalidad (obligatorio en escaneo intraoral)</label>
      <textarea id="observaciones" rows="2" placeholder="Pieza 2.6, evaluación previa a implante"></textarea></div>
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
async function crear(){{
  const b = {{}};
  ['paciente','rut','telefono','prestacion','profesional','indicado_por','venta','observaciones']
    .forEach(k => b[k] = (document.getElementById(k).value || '').trim());
  const m = document.getElementById('msg');
  m.textContent = 'Emitiendo…';
  const r = await fetch('/api/vales?token=' + encodeURIComponent(T),
    {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(b)}});
  const d = await r.json().catch(() => ({{}}));
  if (!r.ok) {{ m.style.color = '#a33a30'; m.textContent = d.detail || 'No se pudo emitir'; return; }}
  m.style.color = '#2e7d5b';
  m.innerHTML = 'Vale <b>' + d.folio + '</b> emitido. <a target="_blank" href="/vale/' + d.folio + '">Verlo</a>';
  setTimeout(() => location.reload(), 1200);
}}
async function acc(folio, a){{
  await fetch('/api/vales/' + folio + '/' + a + '?token=' + encodeURIComponent(T), {{method:'POST'}});
  location.reload();
}}
</script></body></html>""")
