/* cmc-wa.js - marcador de origen en los links de WhatsApp del sitio.
   Reescribe el text= de cada a[href*="wa.me/56966610737"] para que termine en
   "(web: <pagina> · <articulo> · <boton>)". El bot en produccion lo lee y lo limpia.
   Fuente legible; el archivo servido es static/cmc-wa.js (terser). */
(function () {
  var NUM = 'wa.me/56966610737';
  var DEFAULT_TEXT = 'Hola, quiero agendar una hora.';
  var path = location.pathname.replace(/\/+$/, '') || '/';
  var seg = path.split('/').filter(Boolean);
  var slug = function (s) {
    return String(s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '')
      .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  };

  /* pagina segun la URL (solo si el link no trae ya una) */
  function pageFromPath() {
    if (path === '/') return 'home';
    if (seg[0] === 'blog') return 'blog';
    if (/^(comuna|lebu|canete|curanilahue|los-alamos)$/.test(seg[0])) return 'comuna';
    if (seg[0] === 'ortodoncia') return 'landing_ortodoncia';
    return slug(seg[0]).replace(/-/g, '_') || 'sitio';
  }
  /* articulo = ultimo segmento; "-" en home e indices */
  var article = (path === '/' || path === '/blog' || path === '/comuna') ? '-' : (slug(seg[seg.length - 1]) || '-');

  var nBody = 0;
  /* posicion corta del boton; el: <a> o boton de reserva */
  function button(el) {
    var c = function (s) { return el.closest(s); }, e, h, d;
    if ((d = c('[data-wa-btn]'))) return slug(d.getAttribute('data-wa-btn'));
    if ((d = c('[data-spec]'))) return (c('.promo-card') ? 'destacado-' : 'tarjeta-') + slug(d.getAttribute('data-spec'));
    if (c('.modal,[role=dialog]')) return 'modal';
    if (c('.hours-bar')) return 'barra-horario';
    if (c('.mob-bar')) return 'barra-movil';
    h = c('header,nav');
    if (h && !h.closest('article,main,section')) return 'menu';
    if (c('[class*=float]')) return 'flotante';
    for (e = el; e && e !== document.documentElement; e = e.parentElement)
      if (getComputedStyle(e).position === 'fixed') return 'flotante';
    if (c('footer')) return 'footer';
    if (c('aside')) return 'lateral';
    if (c('[class*=hero],[id*=hero]')) return 'hero';
    if (c('[class*=cta-band]')) return 'cierre';
    return 'cuerpo-' + (++nBody);
  }

  /* devuelve la URL wa.me con el marcador extendido */
  function build(href, el) {
    var u = new URL(href, location.href), q = u.search.slice(1).split('&'), text = '', rest = [], i, p, old = '';
    for (i = 0; i < q.length; i++) {
      if (q[i].indexOf('text=') === 0) {
        try { text = decodeURIComponent(q[i].slice(5).replace(/\+/g, ' ')); } catch (x) { text = q[i].slice(5); }
      } else if (q[i]) rest.push(q[i]);
    }
    text = text.replace(/\(\s*web\s*(?::([^()]*))?\)/gi, function (m, g) { old = old || (g || ''); return ' '; })
      .replace(/\s+/g, ' ').trim() || DEFAULT_TEXT;
    p = old.split(/[·|\/]/)[0].trim().toLowerCase().replace(/[^a-z0-9_-]/g, '') || pageFromPath();
    var b = button(el), f = /^(blog|comuna)_float$/.exec(p);
    if (f) { p = f[1]; b = 'flotante'; }
    else if (p === 'comuna_cta') p = 'comuna';
    rest.unshift('text=' + encodeURIComponent(text + ' (web: ' + p + ' · ' + article + ' · ' + b + ')'));
    return u.origin + u.pathname + '?' + rest.join('&') + u.hash;
  }

  function run() {
    var a = document.querySelectorAll('a[href*="' + NUM + '"]'), i;
    for (i = 0; i < a.length; i++) {
      if (a[i].hasAttribute('data-wa-skip') || a[i].hasAttribute('data-wa-ok')) continue;
      try { a[i].href = build(a[i].getAttribute('href'), a[i]); a[i].setAttribute('data-wa-ok', ''); } catch (x) {}
    }
  }

  /* para botones que abren WhatsApp con window.open (reserva del home) */
  window.cmcWa = { url: function (text, el) { return build('https://' + NUM + '?text=' + encodeURIComponent(text), el); } };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run); else run();
})();
