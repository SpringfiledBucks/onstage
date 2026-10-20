"use strict";
// Run actual worker handlers with isolated CacheStorage/Fetch implementations.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { webcrypto, createHash } = require("node:crypto");
const { MessageChannel } = require("node:worker_threads");
const listeners = {};
const stores = new Map();
class MemoryCache {
  constructor() { this.entries = new Map(); }
  async match(request) { return this.entries.get(typeof request === "string" ? request : request.url)?.clone(); }
  async put(request, response) { this.entries.set(request.url, response.clone()); }
  async keys() { return [...this.entries.keys()].map((url) => new Request(url)); }
  async delete(request) { return this.entries.delete(request.url); }
}
const caches = {
  async open(name) { if (!stores.has(name)) stores.set(name, new MemoryCache()); return stores.get(name); },
  async delete(name) { return stores.delete(name); },
};
const body = Buffer.from("synthetic-webp-payload");
const hash = createHash("sha256").update(body).digest("hex");
const url = `https://onstage.test/static/assets/optimized/${hash}.webp`;
let fetchCount = 0;
let responseBody = body;
let contentType = "image/webp";
let repairCachedResponse = false;
let activations = 0;
let responseStatus = 200;
const fetchCalls = [];
const context = vm.createContext({
  self: { location: { origin: "https://onstage.test" }, clients: { claim: async () => {} }, skipWaiting: async () => { activations++; }, addEventListener: (type, fn) => { listeners[type] = fn; } },
  caches, Request, Response, Headers, URL, AbortController, crypto: webcrypto, Uint8Array, setTimeout, clearTimeout,
  fetch: async (_, options) => { fetchCount++; fetchCalls.push(options); return new Response(responseBody, { status: repairCachedResponse && options.cache === "reload" ? 200 : responseStatus, headers: { "Content-Type": repairCachedResponse && options.cache === "reload" ? "image/webp" : contentType } }); },
});
vm.runInContext(fs.readFileSync("static/sw.js", "utf8"), context);
function fetchEvent(path, options = {}) {
  let response;
  const request = new Request(new URL(path, "https://onstage.test").href, options);
  listeners.fetch({ request, respondWith: (promise) => { response = promise; } });
  return response;
}
async function message(type, extra = {}) {
  let pending;
  let answer;
  listeners.message({ data: { type, ...extra }, source: { url: "https://onstage.test/" }, ports: [{ postMessage: (value) => { answer = value; } }], waitUntil: (promise) => { pending = promise; } });
  await pending;
  return answer;
}
(async () => {
  for (const path of ["/", "/api/session", "/api/rooms", "/api/reports", "/static/app.js", "/static/assets-manifest.json", "/static/assets/private.png", `${url}?token=secret`, "https://other.test/static/assets/optimized/" + hash + ".webp"]) {
    assert.equal(fetchEvent(path), undefined, `must not intercept ${path}`);
  }
  assert.equal(fetchEvent(url, { headers: { Authorization: "Bearer secret" } }), undefined);
  assert.equal(fetchEvent(url, { method: "POST" }), undefined);
  const first = await fetchEvent(url);
  assert.deepEqual(Buffer.from(await first.arrayBuffer()), body);
  assert.equal(fetchCount, 1);
  const second = await fetchEvent(url);
  assert.deepEqual(Buffer.from(await second.arrayBuffer()), body);
  assert.equal(fetchCount, 1, "warm hash artwork must not re-fetch");
  let status = await message("ARTWORK_STATUS");
  assert.equal(status.result.cached, 1);
  assert.equal(status.result.cachedBytes, body.length);
  assert.equal((await message("ARTWORK_CACHE", { url: "/api/session" })).ok, false);
  responseBody = Buffer.from("wrong-hash");
  const other = "https://onstage.test/static/assets/optimized/" + "f".repeat(64) + ".webp";
  assert.equal((await message("ARTWORK_CACHE", { url: other })).ok, false);
  assert.equal((await message("ARTWORK_STATUS")).result.cached, 1, "invalid body must not be cached");
  responseBody = body; contentType = "text/html";
  assert.equal((await message("ARTWORK_CACHE", { url: other })).ok, false);
  await caches.open("unrelated-cache");
  assert.equal((await message("ARTWORK_CLEAR")).ok, true);
  assert(stores.has("unrelated-cache"), "clear must preserve unrelated applications");
  assert.equal((await message("ARTWORK_STATUS")).result.cached, 0);
  responseBody = body; contentType = "application/octet-stream"; repairCachedResponse = true;
  const beforeRepair = fetchCount;
  assert.equal((await message("ARTWORK_CACHE", { url })).ok, true, "explicit caching must recover stale HTTP MIME metadata");
  assert.equal(fetchCount - beforeRepair, 2, "only one bounded cache-bypassing retry");
  assert.equal(fetchCalls.at(-1).cache, "reload");
  await message("ARTWORK_CACHE", { url });
  assert.equal(fetchCount - beforeRepair, 2, "a repaired cached asset must never be downloaded again");
  assert.equal(activations, 0, "install/cache must not force an update over open drafts");
  assert.equal((await message("ARTWORK_ACTIVATE")).ok, true);
  assert.equal(activations, 1, "activation is only an explicit application action, without reload");
  await message("ARTWORK_CLEAR");
  responseStatus = 404; contentType = 'text/html';
  const before404 = fetchCount;
  assert.equal((await message("ARTWORK_CACHE", { url })).ok, true, "cached immutable 404 must recover after host repair");
  assert.equal(fetchCount - before404, 2);
  const manifest = JSON.parse(fs.readFileSync("static/manifest.webmanifest", "utf8"));
  assert.equal(manifest.scope, "/"); assert.equal(manifest.start_url, "/"); assert.equal(manifest.display, "standalone");
  for (const icon of manifest.icons) {
    const png = fs.readFileSync(icon.src.slice(1));
    assert.equal(png.subarray(1, 4).toString(), "PNG");
    const expected = Number(icon.sizes.split("x")[0]);
    assert.equal(png.readUInt32BE(16), expected); assert.equal(png.readUInt32BE(20), expected);
  }
  let active = 0; let peak = 0; let registrationCount = 0; let assetCount = 0;
  const worker = { postMessage({ type }, ports) {
    const port = ports[0];
    if (type === "ARTWORK_CACHE") { active++; peak = Math.max(peak, active); assetCount++; }
    setTimeout(() => {
      if (type === "ARTWORK_CACHE") active--;
      port.postMessage({ ok: true, result: type === "ARTWORK_STATUS" ? { cached: assetCount, cachedBytes: assetCount * 42 } : {} }); port.close();
    }, type === "ARTWORK_CACHE" ? 5 : 0);
  } };
  const assets = Object.fromEntries(Array.from({ length: 10 }, (_, i) => {
    const sha256 = String(i).padStart(64, "0");
    return [`original-${i}.png`, { url: `/static/assets/optimized/${sha256}.webp`, sha256, bytes: 42, mime: "image/webp" }];
  }));
  const pageContext = vm.createContext({
    window: { isSecureContext: true, MessageChannel }, MessageChannel, navigator: { serviceWorker: { register: async (path, options) => {
      registrationCount++; assert.equal(path, "/sw.js"); assert.equal(options.scope, "/"); assert.equal(options.updateViaCache, "none"); return { active: worker };
    } } }, AbortController, setTimeout, clearTimeout,
    fetch: async (path, options) => { assert.equal(path, "/static/assets-manifest.json"); assert.equal(options.cache, "no-store"); return new Response(JSON.stringify({ schema_version: 1, assets })); },
  });
  vm.runInContext(fs.readFileSync("static/pwa.js", "utf8"), pageContext);
  const pwa = pageContext.window.OnstagePWA;
  await pwa.init(); assert.equal(registrationCount, 1); assert.equal(assetCount, 0, "init must not preload artwork");
  await pwa.downloadArtwork(); assert.equal(assetCount, 10); assert.equal(peak, 3); assert.equal(pwa.getStatus().completed, 10); assert.equal(pwa.getStatus().busy, false);
  const downloading = pwa.downloadArtwork(); await new Promise((resolve) => setTimeout(resolve, 1)); await pwa.cancelDownload(); await downloading;
  assert(assetCount < 20, "cancel must stop scheduling remaining artwork");
  assert.equal(pwa.getStatus().busy, false);
  console.log("PWA: private requests bypassed; exact hash cache hit; invalid content rejected; isolated clear; install icons verified.");
  console.log("PWA UI: no automatic precache; explicit downloads bounded at 3; cancellation stops scheduling.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
