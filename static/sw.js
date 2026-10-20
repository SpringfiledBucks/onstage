/* Only immutable public artwork is cached. HTML, scripts and APIs always use the network. */
"use strict";
const ART_CACHE = "onstage-artwork-v1";
const MAX_FILES = 256;
const MAX_BYTES = 24 * 1024 * 1024;
const MAX_FILE_BYTES = 4 * 1024 * 1024;
const downloads = new Set();
let cacheEpoch = 0;
let mutation = Promise.resolve();
const serialize = (fn) => { const result = mutation.then(fn); mutation = result.catch(() => {}); return result; };

function artworkURL(value) {
  try {
    const url = new URL(value, self.location.origin);
    return url.origin === self.location.origin && !url.search && !url.hash &&
      /^\/static\/assets\/optimized\/[a-f0-9]{64}\.webp$/.test(url.pathname) ? url : null;
  } catch { return null; }
}
function cacheRequest(url) { return new Request(url.href, { credentials: "omit" }); }
async function putArtwork(url, response, epoch) {
  if (!response.ok || response.redirected || !["basic", "default"].includes(response.type) ||
      response.headers.get("Content-Type")?.split(";")[0].trim() !== "image/webp") throw new Error("invalid_artwork_response:" + response.status + ":" + response.type + ":" + (response.headers.get("Content-Type") || "missing"));
  const declared = Number(response.headers.get("Content-Length"));
  if (declared > MAX_FILE_BYTES) throw new Error("artwork_too_large");
  const bytes = await response.arrayBuffer();
  if (!bytes.byteLength || bytes.byteLength > MAX_FILE_BYTES) throw new Error("artwork_too_large");
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), (n) => n.toString(16).padStart(2, "0")).join("");
  if (!url.pathname.endsWith(`/${hash}.webp`)) throw new Error("artwork_hash_mismatch");
  return serialize(async () => {
    if (epoch !== cacheEpoch) return;
    const cache = await caches.open(ART_CACHE);
    const request = cacheRequest(url);
    if (await cache.match(request)) return;
    const keys = await cache.keys();
    let total = 0;
    const sizes = [];
    for (const key of keys) {
      const old = await cache.match(key);
      const size = Number(old?.headers.get("X-Onstage-Bytes")) || MAX_FILE_BYTES;
      total += size; sizes.push(size);
    }
    while (keys.length && (keys.length >= MAX_FILES || total + bytes.byteLength > MAX_BYTES)) {
      await cache.delete(keys.shift()); total -= sizes.shift();
    }
    const headers = new Headers(response.headers);
    // Fetch decoded the body; do not retain transport encoding/length for the synthetic response.
    headers.delete("Content-Encoding"); headers.delete("Content-Length");
    headers.set("X-Onstage-Bytes", String(bytes.byteLength));
    await cache.put(request, new Response(bytes, { status: 200, headers }));
  });
}
async function fetchArtwork(url, signal, explicit = false) {
  const epoch = cacheEpoch;
  const cache = await caches.open(ART_CACHE);
  const hit = await cache.match(cacheRequest(url));
  if (hit) return hit;
  if (explicit && downloads.size >= 3) throw new Error("artwork_download_busy");
  const abort = new AbortController();
  const cancel = () => abort.abort();
  signal?.addEventListener("abort", cancel, { once: true });
  if (signal?.aborted) abort.abort();
  if (explicit) downloads.add(abort);
  const timer = setTimeout(cancel, 15000);
  try {
    let response = await fetch(url.href, { credentials: "omit", signal: abort.signal, redirect: "error" });
    // A cache/quota failure must not prevent normal artwork from displaying.
    const copy = response.clone();
    try { await putArtwork(url, copy, epoch); } catch (error) {
      // A prior host may have cached a wrong MIME/body under an immutable URL.
      // One explicit retry bypasses only the HTTP cache; quota failures do not
      // trigger extra transfers, and a bad origin response still fails closed.
      if (explicit && (response.ok || response.status === 404) && (error.message.startsWith("invalid_artwork_response:") || error.message === "artwork_hash_mismatch")) {
        response = await fetch(url.href, { credentials: "omit", signal: abort.signal, redirect: "error", cache: "reload" });
        await putArtwork(url, response.clone(), epoch);
      } else if (explicit) throw error;
    }
    return response;
  } finally {
    clearTimeout(timer); downloads.delete(abort); signal?.removeEventListener("abort", cancel);
  }
}
async function status() {
  const cache = await caches.open(ART_CACHE);
  const keys = await cache.keys();
  let cachedBytes = 0;
  for (const key of keys) cachedBytes += Number((await cache.match(key))?.headers.get("X-Onstage-Bytes")) || 0;
  return { cached: keys.length, cachedBytes, maxBytes: MAX_BYTES, maxFiles: MAX_FILES };
}
self.addEventListener("install", () => { /* No precache, skipWaiting or forced reload: preserve open drafts. */ });
self.addEventListener("activate", (event) => { event.waitUntil(self.clients.claim()); });
self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET" || request.mode === "navigate" || request.headers.has("Authorization")) return;
  const url = artworkURL(request.url);
  if (url) event.respondWith(fetchArtwork(url, request.signal));
});
self.addEventListener("message", (event) => {
  const port = event.ports?.[0];
  if (!port || !event.source?.url || new URL(event.source.url).origin !== self.location.origin) return;
  const run = async () => {
    switch (event.data?.type) {
      case "PUSH_PROTOCOL": return { version: 1 };
      case "ARTWORK_ACTIVATE":
        await self.skipWaiting();
        return { activated: true };
      case "ARTWORK_STATUS": return status();
      case "ARTWORK_CACHE": {
        const url = artworkURL(event.data.url);
        if (!url) throw new Error("invalid_artwork_url");
        const response = await fetchArtwork(url, undefined, true);
        if (!response.ok) throw new Error("artwork_download_failed");
        return { cached: true };
      }
      case "ARTWORK_CANCEL": for (const abort of downloads) abort.abort(); return { cancelled: true };
      case "ARTWORK_CLEAR":
        cacheEpoch += 1;
        for (const abort of downloads) abort.abort();
        await serialize(() => caches.delete(ART_CACHE));
        return { cleared: true };
      default: throw new Error("unknown_artwork_command");
    }
  };
  event.waitUntil(run().then((result) => port.postMessage({ ok: true, result }), (error) => port.postMessage({ ok: false, error: String(error.message || "artwork_error") })));
});

// Push payloads contain identifiers only. Never render message content or trust a supplied URL.
function pushRoom(value) {
  return value && value.type === 'room_message' && typeof value.room_id === 'string' &&
    /^[a-f0-9]{32}$/.test(value.room_id) ? value.room_id : null;
}
self.addEventListener('push', (event) => {
  let payload;
  try { payload = event.data?.json(); } catch { /* Still display a generic visible notification. */ }
  const room = pushRoom(payload);
  event.waitUntil(self.registration.showNotification('Onstage', {
    body: '你关注的房间有新消息',
    icon: '/static/onstage-mark.svg',
    tag: room ? 'onstage-room-' + room : 'onstage-message',
    data: { room_id: room },
  }));
});
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const room = event.notification.data?.room_id;
  const path = typeof room === 'string' && /^[a-f0-9]{32}$/.test(room) ? '/room/' + room : '/';
  const target = new URL(path, self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    const existing = windows.find(client => {
      try { return client.frameType !== 'nested' && new URL(client.url).origin === self.location.origin; } catch { return false; }
    });
    if (existing) {
      try {
        const navigated = await existing.navigate(target);
        if (navigated) { await navigated.focus(); return; }
      } catch { /* A closing tab must not swallow the notification click. */ }
    }
    await self.clients.openWindow(target);
  })());
});
