const CACHE_NAME = "aethercloud-static-v1";
const CORE_ASSETS = [
  "/",
  "/login",
  "/register",
  "/manifest.webmanifest",
  "/favicon.svg",
  "/static/css/landing.css",
  "/static/css/auth.css",
  "/static/css/cloud.css",
  "/static/css/admin.css",
  "/static/js/pwa.js",
  "/static/js/cloud.js",
  "/static/icons/aether-icon.svg"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
          return Promise.resolve();
        })
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") {
    return;
  }

  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  if (
    url.pathname.startsWith("/static/") ||
    url.pathname === "/" ||
    url.pathname === "/login" ||
    url.pathname === "/register" ||
    url.pathname === "/manifest.webmanifest" ||
    url.pathname === "/favicon.svg"
  ) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
  }
});
