// CMC Admin v2 — Service Worker
// Estrategia: network-first para HTML/API, cache-first para assets estáticos.
// Permite la app instalable y un fallback offline básico.

const CACHE_VERSION = 'cmc-admin-v2';
const SHELL_CACHE = `${CACHE_VERSION}-shell`;
const ASSETS_CACHE = `${CACHE_VERSION}-assets`;

const SHELL_URLS = [
  '/admin/v2',
  '/static/pwa/icon-192.png',
  '/static/pwa/icon-512.png',
  '/static/pwa/admin-manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_URLS).catch(() => null))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => !k.startsWith(CACHE_VERSION)).map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // No interceptar APIs ni websockets — siempre red
  if (url.pathname.startsWith('/admin/api/') ||
      url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/webhook')) {
    return;
  }

  // Assets estáticos (PNG/SVG/CSS/JS/fonts) — cache-first
  if (url.pathname.startsWith('/static/') ||
      /\.(png|svg|jpg|jpeg|webp|ico|css|js|woff2?)$/.test(url.pathname)) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((res) => {
          if (res && res.status === 200) {
            const copy = res.clone();
            caches.open(ASSETS_CACHE).then((c) => c.put(req, copy));
          }
          return res;
        }).catch(() => cached);
      })
    );
    return;
  }

  // HTML del shell (/admin/v2) — network-first con fallback al cache
  if (url.pathname === '/admin/v2' || url.pathname === '/admin/v2/') {
    event.respondWith(
      fetch(req).then((res) => {
        if (res && res.status === 200) {
          const copy = res.clone();
          caches.open(SHELL_CACHE).then((c) => c.put('/admin/v2', copy));
        }
        return res;
      }).catch(() => caches.match('/admin/v2'))
    );
  }
});
