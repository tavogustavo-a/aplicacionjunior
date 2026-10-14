/* Service worker: página amable cuando no hay red o el servidor no responde. */
var CACHE_NAME = 'effi-panel-offline-v1';
var OFFLINE_URL = '/offline';
var ERROR_PAGE_JS = '/static/error_page.js';

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll([OFFLINE_URL, ERROR_PAGE_JS]);
    }).then(function () {
      return self.skipWaiting();
    }).catch(function () {
      return caches.open(CACHE_NAME).then(function (cache) {
        return cache.add(OFFLINE_URL);
      }).then(function () {
        return self.skipWaiting();
      });
    })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.map(function (key) {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
          return null;
        })
      );
    }).then(function () {
      return self.clients.claim();
    })
  );
});

self.addEventListener('fetch', function (event) {
  if (event.request.method !== 'GET') {
    return;
  }

  var url;
  try {
    url = new URL(event.request.url);
  } catch (e) {
    return;
  }

  if (url.origin === self.location.origin && url.pathname === ERROR_PAGE_JS) {
    event.respondWith(
      fetch(event.request).then(function (resp) {
        if (resp && resp.ok) {
          var copy = resp.clone();
          caches.open(CACHE_NAME).then(function (cache) {
            cache.put(ERROR_PAGE_JS, copy);
          });
        }
        return resp;
      }).catch(function () {
        return caches.match(ERROR_PAGE_JS).then(function (cached) {
          return cached || Response.error();
        });
      })
    );
    return;
  }

  if (event.request.mode !== 'navigate') {
    return;
  }

  event.respondWith(
    fetch(event.request).catch(function () {
      return caches.match(OFFLINE_URL).then(function (cached) {
        if (cached) {
          return cached;
        }
        return new Response(
          '<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sin conexión</title><style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:1.25rem;font-family:Segoe UI,Tahoma,sans-serif;background:linear-gradient(160deg,#1e293b,#0f172a);color:#0f172a}.c{max-width:420px;background:#fff;border-radius:16px;padding:1.75rem 1.5rem;text-align:center}h1{margin:0 0 .55rem;font-size:1.35rem}p{margin:0 0 .75rem;color:#475569}.a{display:flex;gap:.55rem;justify-content:center;flex-wrap:wrap;margin-top:1rem}button,a{display:inline-block;border:0;border-radius:10px;padding:.7rem 1.1rem;font-weight:600;text-decoration:none;background:#1e293b;color:#fff;cursor:pointer;font-size:.95rem}a.s{background:#e2e8f0;color:#0f172a}</style></head><body><div class="c"><h1>Sin conexión</h1><p>No hay internet o el servidor no responde. Cuando vuelva, pulsa reintentar.</p><div class="a"><form method="get" action=""><input type="hidden" name="_retry" value="1"><button type="submit">Reintentar</button></form><a class="s" href="/">Ir al inicio</a></div></div></body></html>',
          { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
        );
      });
    })
  );
});
