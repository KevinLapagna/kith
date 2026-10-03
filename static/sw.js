/* Kith's service worker: the shell, cached; the app, never.
 *
 * Training is a conversation with the server — which face comes next, what you just got wrong,
 * where the ladder moves you — so caching an answer would not be "working offline", it would be
 * inventing results. What this caches is the part that is identical on every visit: stylesheets,
 * scripts, icons. Everything else goes to the network, always, and no answer or page is ever
 * stored.
 *
 * The version arrives in the registration URL, so any deploy that touches an asset retires the
 * previous cache the next time the app is opened, without a build step.
 */
const VERSION = new URL(self.location.href).searchParams.get("v") || "0";
const CACHE = "kith-shell-" + VERSION;

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)));
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  // Only same-origin GETs, and only the shell. Pages, photos and the API are never cached:
  // a stale face would be worse than no face.
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith("/static/")) return;

  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE);
      const hit = await cache.match(request);
      if (hit) return hit;
      const response = await fetch(request);
      if (response.ok) cache.put(request, response.clone());
      return response;
    })(),
  );
});
