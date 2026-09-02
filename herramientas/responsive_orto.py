"""Auditoría responsive del panel de Ortodoncia y de Cargos. Mide, no opina.

Reusa el medidor del Patio (`~/patio-app/herramientas_responsive.py`): desborde
horizontal · elementos fuera de pantalla · área táctil real ≥44 px comprobada
con elementFromPoint · texto tapado. Aquí además se recorren las PESTAÑAS, que
es donde vive casi toda la superficie del panel.

Uso:  python3 herramientas/responsive_orto.py
"""
from playwright.sync_api import sync_playwright
import os, sys

TOKEN = os.getenv("ORTODONCIA_TOKEN", "cmc_ortodoncia_b95f4b11")
BASE  = os.getenv("BASE", "https://agentecmc.cl")
# (nombre, url, js para dejar la vista puesta antes de medir)
VISTAS = [
    ("embudo",      f"/alma/orto-embudo?token={TOKEN}", None),
    ("tratamiento", f"/alma/orto-embudo?token={TOKEN}", "vista('tra')"),
    ("calendario",  f"/alma/orto-embudo?token={TOKEN}", "vista('cal')"),
    ("cargos",      f"/alma/cargos?token={TOKEN}",      None),
    ("cargos-liq",  f"/alma/cargos?token={TOKEN}",      "tab('liq')"),
    ("cargos-cat",  f"/alma/cargos?token={TOKEN}",      "tab('cat')"),
]
ANCHOS = [(320,640),(360,780),(375,812),(390,844),(414,896),(430,932),(540,960),
          (768,1024),(820,1180),(1024,768),(1180,820),(1280,800),(1440,900),(1920,1080),
          (844,390),(932,430)]   # los dos ultimos = telefono ACOSTADO

MEDIR = """() => {
  const out = {desborde:false, fuera:[], tap:[], tapado:[], barra:null};
  out.desborde = document.documentElement.scrollWidth > window.innerWidth + 1;
  const vw = window.innerWidth;
  document.querySelectorAll('body *').forEach(el=>{
    const r = el.getBoundingClientRect();
    if (r.width===0 || r.height===0) return;
    // Un cajon/modal CERRADO vive fuera de la pantalla a proposito. Si el
    // elemento o algun ancestro esta visibility:hidden, no se esta viendo:
    // medirlo daba 48 falsos positivos por la ficha lateral del embudo.
    if (getComputedStyle(el).visibility === 'hidden') return;
    if (el.closest('.drawer:not(.on), .modal-bg:not(.show)')) return;
    const cls = ((el.className.baseVal!==undefined?el.className.baseVal:el.className)||'').toString().slice(0,26);
    if (r.right <= vw+1 && r.left >= -1) return;
    if (el.closest('#confeti')) return;
    // ¿algún ancestro lo recorta? entonces no desborda nada
    let a = el.parentElement, recortado = false;
    while (a && a !== document.body) {
      const o = getComputedStyle(a);
      // `auto` y `scroll` tambien recortan: un carrusel horizontal (el tablero
      // kanban) tiene sus columnas fuera del viewport A PROPOSITO. El medidor
      // original solo miraba hidden|clip — venia del Patio, que no tenia
      // scrollers horizontales — y marcaba 9 falsos positivos.
      if (/hidden|clip|auto|scroll/.test(o.overflow + o.overflowX)) {
        const ar = a.getBoundingClientRect();
        if (ar.right <= vw + 1 && ar.left >= -1) { recortado = true; break; }
      }
      a = a.parentElement;
    }
    if (!recortado) out.fuera.push(el.tagName+'.'+cls);
  });
  document.querySelectorAll('a,button,input,summary,[tabindex]').forEach(el=>{
    if (el.hidden || el.closest('[hidden]')) return;
    const r = el.getBoundingClientRect();
    if (r.width===0||r.height===0) return;
    // El área táctil puede venir expandida por un ::before con inset negativo.
    // No se confía en el CSS: se COMPRUEBA con elementFromPoint, y para eso el
    // elemento tiene que estar en el viewport (si no, devuelve null y da un
    // falso "no llega" que hace romper algo que funcionaba).
    let ex = 0;
    if (r.width < 44 || r.height < 44) {
      const antes = getComputedStyle(el, '::before');
      if (antes && antes.content !== 'none' && antes.position === 'absolute') {
        const v = ['top','left','right','bottom'].map(k => parseFloat(antes[k]));
        if (v.every(x => !isNaN(x) && x <= 0)) {
          const decl = Math.min(...v.map(x => -x));
          el.scrollIntoView({block:'center'});
          const q = el.getBoundingClientRect();
          const dentro = q.top > 0 && q.bottom < innerHeight;
          const toca = d => { const t = document.elementFromPoint(q.left - d, q.top + q.height/2);
                              return !!(t && (t === el || el.contains(t))); };
          ex = (dentro && toca(decl - 1)) ? decl : 0;   // sólo cuenta si el toque LLEGA
        }
      }
    }
    if (r.width + 2*ex < 44 || r.height + 2*ex < 44) {
      const cls = (el.className||'').toString().slice(0,24);
      out.tap.push(el.tagName+'.'+cls+' '+Math.round(r.width)+'x'+Math.round(r.height));
    }
  });
  document.querySelectorAll('h1,h2,h3,.rot,.cifra b,.carta .pr,.cartel-nombre,.linea-t b').forEach(el=>{
    const r = el.getBoundingClientRect();
    if (r.width<5 || r.bottom<0 || r.top>window.innerHeight) return;
    if (getComputedStyle(el).visibility === 'hidden') return;
    if (el.closest('.drawer:not(.on), .modal-bg:not(.show)')) return;
    const t = document.elementFromPoint(r.left + Math.min(6, r.width/2), r.top + r.height/2);
    const pegajoso = t && (t.closest('.tope') || t.closest('.barra'));   // scroll-under, no es un defecto
    if (t && !pegajoso && !el.contains(t) && t!==el && !el.parentElement.contains(t))
      out.tapado.push((el.textContent||'').trim().slice(0,20)+' <- '+t.tagName+'.'+((t.className||'')+'').slice(0,18));
  });
  const barra = document.querySelector('.barra');
  if (barra && getComputedStyle(barra).display !== 'none') {
    const bt = barra.getBoundingClientRect().top;
    window.scrollTo(0, document.body.scrollHeight);
    const ult = [...document.querySelectorAll('main .bloque, main section')].pop();
    if (ult) out.barra = Math.round(ult.getBoundingClientRect().bottom - bt);  // >0 = tapado
  }
  return out;
}"""


problemas = 0
with sync_playwright() as p:
    b = p.chromium.launch()
    # Un telefono es TACTIL: sin `has_touch` el navegador reporta pointer:fine y
    # las reglas @media(pointer:coarse) no se aplican, asi que la medicion del
    # area tactil daria un falso negativo en justo los anchos que importan.
    ctx_movil = b.new_context(has_touch=True, is_mobile=True)
    ctx_esc   = b.new_context()
    pg_movil, pg_esc = ctx_movil.new_page(), ctx_esc.new_page()
    for nombre, url, prep in VISTAS:
        for w, h in ANCHOS:
            tactil = min(w, h) <= 500          # telefono, de pie o acostado
            pg = pg_movil if tactil else pg_esc
            pg.set_viewport_size({"width": w, "height": h})
            pg.goto(BASE + url, wait_until="networkidle")
            if prep:
                pg.evaluate(prep)
            pg.wait_for_timeout(2600 if prep else 1400)
            r = pg.evaluate(MEDIR)
            fallas = []
            if r["desborde"]:
                fallas.append("DESBORDE horizontal")
            if r["fuera"]:
                fallas.append(f"{len(r['fuera'])} fuera de pantalla: {r['fuera'][:3]}")
            # El mínimo de 44 px es una regla TÁCTIL. Exigirlo con mouse marca
            # como defecto botones de 40 px que se pulsan sin problema.
            if r["tap"] and tactil:
                fallas.append(f"{len(r['tap'])} bajo 44px: {r['tap'][:3]}")
            if r["tapado"]:
                fallas.append(f"tapado: {r['tapado'][:2]}")
            if fallas:
                problemas += 1
                print(f"  {nombre:<12} {w}x{h}")
                for f in fallas:
                    print(f"      - {f}")
    b.close()
print()
print("SIN HALLAZGOS" if not problemas else f"{problemas} combinaciones con hallazgos")
sys.exit(0)
