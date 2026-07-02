/* QuantResearch service worker
   - cache-first for static assets
   - network-first for navigations, falling back to the offline page */
const CACHE = 'qr-v1';
const OFFLINE_URL = '/static/offline.html';
const PRECACHE = [
  OFFLINE_URL,
  '/static/icons/icon.svg',
  '/static/manifest.webmanifest'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  // Network-first for navigations; fall back to the offline page.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() =>
        caches.match(OFFLINE_URL, { ignoreSearch: true })
      )
    );
    return;
  }

  // Cache-first for same-origin static assets.
  const url = new URL(req.url);
  if (url.origin === self.location.origin && url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((resp) => {
          if (resp && resp.status === 200 && resp.type === 'basic') {
            const copy = resp.clone();
            caches.open(CACHE).then((cache) => cache.put(req, copy));
          }
          return resp;
        });
      })
    );
  }
});
