// CMC Admin v2 — Service Worker
// Estrategia: network-first para HTML/API, cache-first para assets estáticos.
// Permite la app instalable y un fallback offline básico.

const CACHE_VERSION = 'cmc-admin-v3';
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


// ── Web Push: notificación nativa + badge en ícono PWA ──────────────────────
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: 'CMC', body: event.data ? event.data.text() : '' }; }

  const title = data.title || 'Centro Médico Carampangue';
  const body = data.body || '';
  const url = data.url || '/admin/v2';
  const tag = data.tag || 'cmc-msg';
  const badge = (typeof data.badge === 'number') ? data.badge : null;

  const ops = [
    self.registration.showNotification(title, {
      body,
      icon: '/static/pwa/icon-192.png',
      badge: '/static/pwa/icon-192.png',
      tag,
      renotify: true,
      requireInteraction: false,
      vibrate: [180, 80, 180],
      data: { url },
    }),
  ];

  if (badge !== null && 'setAppBadge' in self.navigator) {
    ops.push(badge > 0 ? self.navigator.setAppBadge(badge) : self.navigator.clearAppBadge());
  }

  // Notificar a clientes abiertos para que actualicen UI in-app sin recargar
  ops.push(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
    clients.forEach((c) => c.postMessage({ type: 'cmc-push', payload: data }));
  }));

  event.waitUntil(Promise.all(ops));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/admin/v2';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      // Si hay una ventana de admin abierta, enfocala y navegá ahí
      for (const c of clients) {
        if (c.url.includes('/admin/v2')) {
          c.navigate(target).catch(() => null);
          return c.focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});

// Permitir limpiar el badge desde el cliente cuando lee
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'cmc-clear-badge') {
    if ('clearAppBadge' in self.navigator) self.navigator.clearAppBadge();
  } else if (event.data && event.data.type === 'cmc-set-badge') {
    const n = event.data.count || 0;
    if ('setAppBadge' in self.navigator) {
      n > 0 ? self.navigator.setAppBadge(n) : self.navigator.clearAppBadge();
    }
  }
});
