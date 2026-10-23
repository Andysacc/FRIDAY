const SHELL_CACHE = "friday-shell-v6";
const SHELL_URLS = [
  "/",
  "/index.html",
  "/manifest.webmanifest",
  "/styles.css?v=arc-17",
  "/app.js?v=arc-17",
  "/orb.js?v=arc-4",
  "/chat-code.js?v=arc-1",
  "/icons/friday.svg",
  "/icons/friday-180.png",
  "/icons/friday-192.png",
  "/icons/friday-512.png",
  "/icons/friday-maskable-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_URLS)));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== SHELL_CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("message", (event) => {
  if (event.data?.type === "SKIP_WAITING") self.skipWaiting();
});

async function networkFirstNavigation(request) {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) await cache.put("/index.html", response.clone());
    return response;
  } catch {
    return (await cache.match("/index.html")) || Response.error();
  }
}

async function cachedShellAsset(request) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(request, { ignoreSearch: false });
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) await cache.put(request, response.clone());
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(networkFirstNavigation(request));
    return;
  }

  const isShellAsset = SHELL_URLS.some((asset) => {
    const expected = new URL(asset, self.location.origin);
    return expected.pathname === url.pathname && expected.search === url.search;
  });
  if (isShellAsset) event.respondWith(cachedShellAsset(request));
});
