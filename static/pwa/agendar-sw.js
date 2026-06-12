// CMC Agendador público — Service Worker (instalable como web app)

const CACHE_VERSION = 'cmc-agendar-v1';
const SHELL_CACHE = `${CACHE_VERSION}-shell`;

const SHELL_URLS = [
  '/static/pwa/icon-192.png',
  '/static/pwa/icon-512.png',
  '/static/pwa/agendar-manifest.webmanifest',
  '/static/isotipo.png',
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

// Estrategia: red primero SIEMPRE (los slots cambian minuto a minuto — jamás
// servir disponibilidad cacheada); cache solo como respaldo de assets estáticos.
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      fetch(event.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(SHELL_CACHE).then((c) => c.put(event.request, copy)).catch(() => null);
          return res;
        })
        .catch(() => caches.match(event.request))
    );
  }
});
