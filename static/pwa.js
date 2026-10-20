/* Optional public artwork cache; private application state is never stored here. */
(() => {
  "use strict";
  let registration;
  let listener = () => {};
  let entries;
  let cancelled = false;
  let operation;
  const state = { supported: false, registered: false, busy: false, total: 0, completed: 0, failed: 0, cached: 0, cachedBytes: 0, error: "", message: "美术缓存不会保存聊天记录；多人对戏仍需联网。" };
  const publish = (patch = {}) => { Object.assign(state, patch); try { listener({ ...state }); } catch { /* UI cannot break downloads. */ } return { ...state }; };
  function rpc(type, extra = {}, targetWorker = registration?.active) {
    return new Promise((resolve, reject) => {
      const worker = targetWorker;
      if (!worker) { reject(new Error("美术缓存尚未就绪，请稍后重试。")); return; }
      const channel = new MessageChannel();
      const timer = setTimeout(() => { channel.port1.close(); reject(new Error("美术缓存请求超时，请重试。")); }, 20000);
      channel.port1.onmessage = ({ data }) => {
        clearTimeout(timer); channel.port1.close();
        data.ok ? resolve(data.result) : reject(new Error(data.error || "美术缓存操作失败。"));
      };
      worker.postMessage({ type, ...extra }, [channel.port2]);
    });
  }
  async function manifest() {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 10000);
    try {
      const response = await fetch("/static/assets-manifest.json", { credentials: "omit", cache: "no-store", signal: abort.signal });
      if (!response.ok) throw new Error("无法获取美术清单。");
      const json = await response.json();
      if (json.schema_version !== 1 || !json.assets || typeof json.assets !== "object") throw new Error("美术清单格式不受支持。");
      const seen = new Set();
      const result = [];
      for (const item of Object.values(json.assets)) {
        if (typeof item.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(item.sha256) ||
            item.url !== `/static/assets/optimized/${item.sha256}.webp` || item.mime !== "image/webp" ||
            !Number.isSafeInteger(item.bytes) || item.bytes < 1 || item.bytes > 4 * 1024 * 1024) throw new Error("美术清单包含无效资源。");
        if (!seen.has(item.url)) { seen.add(item.url); result.push(item); }
      }
      if (result.length > 256 || result.reduce((sum, item) => sum + item.bytes, 0) > 24 * 1024 * 1024) throw new Error("美术资源超出本机缓存上限。");
      return result;
    } finally { clearTimeout(timer); }
  }
  async function refreshStatus() {
    if (!registration?.active) return publish();
    try { return publish({ ...await rpc("ARTWORK_STATUS"), updateAvailable: Boolean(registration.waiting) }); } catch (error) { return publish({ error: error.message }); }
  }
  async function init({ onStatus } = {}) {
    if (typeof onStatus === "function") listener = onStatus;
    state.supported = Boolean(window.isSecureContext && navigator.serviceWorker && window.MessageChannel);
    if (!state.supported) return publish({ message: "此环境不支持持久美术缓存；请使用 HTTPS 或 localhost。多人对戏仍需联网。" });
    try {
      registration = await navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" });
      publish({ registered: true, error: "", updateAvailable: Boolean(registration.waiting) });
      registration.addEventListener?.("updatefound", () => {
        registration.installing?.addEventListener("statechange", () => {
          if (registration.waiting) publish({ updateAvailable: true });
        });
      });
      navigator.serviceWorker.addEventListener?.("controllerchange", () => { void refreshStatus(); });
      // Do not await ready indefinitely: installation may fail or the device may go offline.
      if (!registration.active) {
        const worker = registration.installing || registration.waiting;
        worker?.addEventListener("statechange", () => { if (registration.active) void refreshStatus(); });
      }
      return await refreshStatus();
    } catch (error) { return publish({ error: "无法启用美术缓存，请检查网络或浏览器权限。" }); }
  }
  async function downloadArtwork() {
    if (operation) return operation;
    if (!registration?.active) return publish({ error: "美术缓存尚未就绪，请稍后重试。" });
    cancelled = false;
    publish({ busy: true, completed: 0, failed: 0, error: "", message: "正在缓存公开美术；可以取消，不影响聊天。" });
    operation = (async () => {
      try {
        entries = await manifest();
        publish({ total: entries.length });
        let next = 0;
        const runner = async () => {
          while (!cancelled && next < entries.length) {
            const item = entries[next++];
            try { await rpc("ARTWORK_CACHE", { url: item.url }); if (!cancelled) publish({ completed: state.completed + 1 }); }
            catch (error) {
              if (!cancelled) {
                console.warn("Onstage public artwork cache failure:", String(error?.message || "unknown").slice(0, 160));
                publish({ failed: state.failed + 1 });
              }
            }
          }
        };
        await Promise.all(Array.from({ length: Math.min(3, entries.length) }, runner));
        publish({ message: cancelled ? "已取消缓存；已完成的素材仍保留。" : state.failed ? "部分美术未缓存，可再次点击重试；已有缓存仍保留。" : "美术缓存完成；多人对戏仍需联网。" });
      } catch (error) { publish({ error: error.message }); }
      finally { operation = undefined; publish({ busy: false }); await refreshStatus(); }
      return { ...state };
    })();
    return operation;
  }
  async function cancelDownload() { cancelled = true; try { await rpc("ARTWORK_CANCEL"); } catch { /* In-flight operations have finite timeouts. */ } return publish(); }
  async function clearArtwork() {
    await cancelDownload();
    if (operation) await operation;
    try { await rpc("ARTWORK_CLEAR"); return publish({ cached: 0, cachedBytes: 0, error: "", message: "本应用美术缓存已清除；聊天身份和草稿未改动。" }); }
    catch (error) { return publish({ error: error.message }); }
  }
  async function activateUpdate() {
    if (operation) return publish({ error: "请先完成或取消当前美术下载。" });
    if (!registration?.waiting) return refreshStatus();
    try {
      await rpc("ARTWORK_ACTIVATE", {}, registration.waiting);
      return publish({ updateAvailable: false, error: "", message: "美术缓存服务已更新；聊天与草稿无需重载。" });
    } catch { return publish({ error: "缓存服务更新未完成，请稍后重试。" }); }
  }
  window.OnstagePWA = { init, downloadArtwork, cancelDownload, clearArtwork, activateUpdate, getStatus: () => ({ ...state }), refreshStatus };
})();
