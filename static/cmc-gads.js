/* cmc-gads.js — atribución Google Ads: gclid/gbraid/wbraid -> código corto -> marcador del wa.me.
 * Sin gclid/gbraid/wbraid (ni uno guardado de las últimas visitas) NO hace nada: la página queda igual.
 * Flujo: genera código (5 chars sin 0/O/1/I/L) -> POST /api/gclick -> recién ahí agrega
 * " · g-<code>" al marcador "(web: ...)" de los links wa.me (el bot lo lee y lo limpia).
 * Sin CDN ni librerías. Cargar DESPUÉS de cmc-wa.js. */
!function () {
  var KEY = "cmc_gads", TTL = 90 * 864e5, ALF = "ABCDEFGHJKMNPQRSTUVWXYZ23456789", WA = "wa.me/56966610737";
  var mem = null, st = null;

  function load() {
    try { var v = JSON.parse(localStorage.getItem(KEY) || "null"); if (v) return v; } catch (e) {}
    return mem;
  }
  function save(v) { mem = v; try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) {} }
  function gen(n) {
    var out = "", i, b = new Uint8Array(n);
    try { (window.crypto || window.msCrypto).getRandomValues(b); }
    catch (e) { for (i = 0; i < n; i++) b[i] = Math.floor(Math.random() * 256); }
    for (i = 0; i < n; i++) out += ALF.charAt(b[i] % ALF.length);
    return out;
  }

  // 1) ¿Viene clic nuevo en la URL?
  var q = {}, s = location.search.replace(/^\?/, "").split("&"), i, kv, tipo = "", cid = "";
  for (i = 0; i < s.length; i++) {
    kv = s[i].split("=");
    if (kv[0]) { try { q[kv[0]] = decodeURIComponent((kv[1] || "").replace(/\+/g, " ")); } catch (e) {} }
  }
  var tipos = ["gclid", "gbraid", "wbraid"];
  for (i = 0; i < tipos.length; i++) if (q[tipos[i]] && /^[A-Za-z0-9_\-]{4,512}$/.test(q[tipos[i]])) { tipo = tipos[i]; cid = q[tipo]; break; }

  st = load();
  if (st && (!st.ts || Date.now() - st.ts > TTL)) st = null;
  if (cid && (!st || st.id !== cid)) { st = { code: gen(5), type: tipo, id: cid, ts: Date.now(), ok: false }; save(st); }
  if (!st) return;                                   // sin clic de Google: página idéntica a hoy

  // 2) Marcar los wa.me (idempotente)
  function marcar(href) {
    var u, partes, out = [], txt = "", j, re = /\(\s*web\s*:([^()]*)\)/i;
    try { u = new URL(href, location.href); } catch (e) { return href; }
    partes = u.search.replace(/^\?/, "").split("&");
    for (j = 0; j < partes.length; j++) {
      if (partes[j].indexOf("text=") === 0) {
        try { txt = decodeURIComponent(partes[j].slice(5).replace(/\+/g, " ")); } catch (e) { txt = partes[j].slice(5); }
      } else if (partes[j]) out.push(partes[j]);
    }
    if (/\bg-[A-Za-z0-9]{4,6}\b/.test(txt.match(re) ? txt.match(re)[1] : "")) return href;   // ya marcado
    txt = re.test(txt) ? txt.replace(re, function (m, x) { return "(web:" + x.replace(/\s+$/, "") + " · g-" + st.code + ")"; })
                       : (txt || "Hola, quiero agendar una hora.") + " (web: g-" + st.code + ")";
    out.unshift("text=" + encodeURIComponent(txt));
    return u.origin + u.pathname + "?" + out.join("&") + u.hash;
  }
  function tag(a) {
    if (!st || !st.ok || !a || a.hasAttribute("data-wa-skip")) return;
    var h = a.getAttribute("href") || "";
    if (h.indexOf(WA) < 0) return;
    try { var n = marcar(h); if (n !== h) a.setAttribute("href", n); } catch (e) {}
  }
  function todos() {
    var l = document.querySelectorAll('a[href*="' + WA + '"]'), k;
    for (k = 0; k < l.length; k++) tag(l[k]);
  }
  function alClic(ev) {
    var t = ev.target;
    while (t && t.nodeType === 1 && t.tagName !== "A") t = t.parentNode;
    if (t && t.nodeType === 1) tag(t);              // cubre hrefs que otros scripts arman al vuelo
  }
  ["mousedown", "touchstart", "click", "contextmenu", "keydown"].forEach(function (e) {
    document.addEventListener(e, alClic, true);
  });

  // 3) Registrar en el backend (reintenta con código nuevo si el servidor lo tiene ocupado)
  function registrar(intento) {
    if (st.ok) { todos(); return; }
    var body = { code: st.code, landing: location.pathname, ts: Math.floor(Date.now() / 1000) };
    body[st.type] = st.id;
    fetch("/api/gclick", { method: "POST", headers: { "Content-Type": "application/json" },
                           body: JSON.stringify(body), keepalive: true, credentials: "omit" })
      .then(function (r) {
        if (r.ok) { st.ok = true; save(st); todos(); }
        else if (r.status === 409 && intento < 3) { st.code = gen(5); save(st); registrar(intento + 1); }
      })
      .catch(function () { /* sin red: los links quedan como hoy */ });
  }
  registrar(0);
  if (st.ok) { todos(); }
  window.addEventListener("load", todos);
}();
