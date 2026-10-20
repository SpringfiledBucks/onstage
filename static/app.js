/* Onstage: shared UI. The server declares HTTP polling or WS notification;
 * only an explicitly injected adapter enables browser-local preview. */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const previewAdapter = typeof window.OnstagePreview?.request === "function" ? window.OnstagePreview : null;
  const PREVIEW = Boolean(previewAdapter) || window.OnstagePreviewConfig?.enabled === true || document.documentElement.dataset.onstageMode === "preview";
  const PHASES = { preparing: "候场中", live: "演出中", paused: "已暂停", ended: "已落幕" };
  const CHANNELS = { stage: "舞台", backstage: "后台" };
  const storagePrefix = PREVIEW ? "onstage.preview.v04." : "onstage.phase1.";
  const state = {
    authenticated: false,
    csrf: "",
    nickname: "",
    awaitingNickname: false,
    guestComposing: false,
    scope: "",
    version: "",
    transport: { type: "websocket", intervalMs: 8000, presenceIntervalMs: 15000, presenceTtlMs: 45000 },
    catalog: { bands: [], characters: [], scenarios: [], stickers: [] },
    rooms: [],
    roomPage: 0,
    roomCursors: [""],
    roomHasMore: false,
    assetMap: {},
    artworkReady: null,
    sendShortcut: "enter",
    followHistoryStart: false,
    filter: "active",
    room: null,
    generation: 0,
    navigation: 0,
    channel: "stage",
    mobilePanel: "stage",
    cursors: { stage: 0, backstage: 0 },
    messages: { stage: new Map(), backstage: new Map() },
    unread: { stage: 0, backstage: 0 },
    drafts: new Map(),
    stickerDrafts: new Map(),
    stickerFilter: "all",
    stickerSource: "user-v04",
    stickerCategory: "all",
    stickerSearch: "",
    pending: new Map(),
    posting: new Set(),
    busyRooms: new Set(),
    syncRunning: false,
    syncQueued: false,
    initialSynced: false,
    syncTimer: null,
    syncController: null,
    pollTimer: null,
    presenceTimer: null,
    presenceController: null,
    presenceBaseId: "",
    presenceId: "",
    lastPresence: 0,
    presenceError: "",
    socket: null,
    socketMode: "idle",
    reconnectTimer: null,
    reconnectAttempts: 0,
    heartbeatTimer: null,
    lastSocketMessage: 0,
    lastSync: 0,
    syncError: "",
    socketError: "",
    messageSignature: "",
    roomListRequest: null,
    joinRequest: null,
    createScenario: null,
    createBusy: false,
    bootstrapBusy: false,
    recoverRequested: false,
    lastAutoRecovery: 0,
    recoveryNotice: "",
    sceneFilter: "all",
    settingsRoomId: "",
    settingsBusy: false,
    reportTarget: null,
    reportBusy: false,
    reportsScope: "",
    reportList: [],
    reportCursor: 0,
    reportHasMore: false,
    reportLoading: false,
    reportsGeneration: 0,
    resolveTarget: null,
    resolveBusy: false,
    composing: false,
    eventsBound: false,
    storageWarning: false,
    confirmResolver: null,
    routeTimer: null,
    previewResetting: false,
    sessionGeneration: 0,
  };

  class ApiError extends Error {
    constructor(message, status = 0) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }

  function node(tag, className = "", text = null) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== null && text !== undefined) item.textContent = String(text);
    return item;
  }

  function actionButton(text, className, callback, disabled = false, focusKey = "") {
    const item = node("button", className, text);
    item.type = "button";
    item.disabled = disabled;
    if (focusKey) item.dataset.focusKey = focusKey;
    item.addEventListener("click", callback);
    return item;
  }

  const sameId = (a, b) => a !== null && a !== undefined && b !== null && b !== undefined && String(a) === String(b);
  const roomPath = (id) => `/api/rooms/${encodeURIComponent(String(id))}`;
  const currentRoomId = () => state.room ? String(state.room.id) : "";
  const character = (id) => state.catalog.characters.find((item) => sameId(item.id, id));
  const scenario = (id) => state.catalog.scenarios.find((item) => sameId(item.id, id));
  const band = (id) => state.catalog.bands.find((item) => sameId(item.id, id));
  const sticker = (id) => (state.catalog.stickers || []).find((item) => sameId(item.id, id));
  const members = (room = state.room) => Array.isArray(room?.members) ? room.members : [];
  const actors = (room = state.room) => members(room).filter((item) => Boolean(item.character_id));
  const pendingMessage = () => state.pending.get(currentRoomId());
  const canReadBackstage = () => Boolean(state.room?.me?.can_backstage);
  const isRoomBusy = () => state.busyRooms.has(currentRoomId());

  const SVG_NS = "http://www.w3.org/2000/svg";
  const ICONS = {
    spark: ["M13 2 16 9 23 11 16 14 13 22 10 15 2 12 9 9Z"],
    star: ["m12 2 3.1 6.3 7 .9-5.1 5 1.2 7L12 18l-6.2 3.2 1.2-7-5.1-5 7-.9Z"],
    chat: ["M20 11a8 8 0 0 1-8 8H5l-4 3 1.5-7A8 8 0 1 1 20 11Z", "M7 10h8M7 14h5"],
    headphones: ["M4 14v-3a8 8 0 0 1 16 0v3", "M4 12H2v7h4v-7ZM20 12h2v7h-4v-7ZM18 20c-1 2-3 2-5 2"],
    sound: ["m3 10 4-1 5-5v16l-5-5-4-1Z", "M16 8a7 7 0 0 1 0 8M19 5a11 11 0 0 1 0 14"],
    moon: ["M20.5 14.5A9 9 0 0 1 9.5 3.5 9 9 0 1 0 20.5 14.5Z"],
    sun: ["M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5", "M16.5 12a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0Z"],
    flag: ["M5 22V3c5-4 9 4 15 0v12c-6 4-10-4-15 0"],
    refresh: ["M20 7a9 9 0 0 0-15-2L2 8M2 2v6h6M4 17a9 9 0 0 0 15 2l3-3M22 22v-6h-6"],
    "arrow-right": ["M4 12h16M14 6l6 6-6 6"],
    "arrow-left": ["M20 12H4M10 6l-6 6 6 6"],
    settings: ["M4 6h16M4 12h16M4 18h16M8 3v6M16 9v6M10 15v6"],
    "arrow-up-right": ["M6 18 18 6M6 6h12v12"],
    "arrow-down": ["M12 4v16M6 14l6 6 6-6"],
    "chevron-down": ["m6 9 6 6 6-6"],
    close: ["m6 6 12 12M6 18 18 6"],
    link: ["m10 14 4-4M8 16l-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0M16 8l2-2a4 4 0 1 1 6 6l-5 5a4 4 0 0 1-6 0"],
    edit: ["m15 4 5 5M3 21l5-1L21 7a3.5 3.5 0 0 0-5-5L3 15Z"],
    pin: ["m15 3 6 6-4 1-4 4v4l-7-7h4l4-4ZM4 20l5-5"],
    send: ["m22 2-7 20-4-9L2 9ZM11 13 22 2"],
    people: ["M9 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM2 22v-3a7 7 0 0 1 14 0v3M17 5a4 4 0 0 1 0 7M19 16a5 5 0 0 1 3 5"],
    user: ["M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 22v-3a8 8 0 0 1 16 0v3"],
    cast: ["M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z", "M15 9a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM7 18a5 5 0 0 1 10 0"],
    inbox: ["M3 3h18v18H3ZM3 14h5l2 3h4l2-3h5M8 7h8M8 10h5"],
    info: ["M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0ZM12 11v6M12 7v.1"],
    remove: ["M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM1 22v-3a8 8 0 0 1 13-6M16 17h7"],
    location: ["M20 9c0 6-8 13-8 13S4 15 4 9a8 8 0 0 1 16 0ZM15 9a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"],
    music: ["M9 18V5l12-3v13M9 5v5l12-3M9 18c0 2-2 3-4 3s-3-1-3-2 2-3 4-3 3 1 3 2ZM21 15c0 2-2 3-4 3s-3-1-3-2 2-3 4-3 3 1 3 2Z"],
    check: ["m4 12 5 5L20 6"],
    sticker: ["M14 22H5a3 3 0 0 1-3-3V5a3 3 0 0 1 3-3h14a3 3 0 0 1 3 3v9Z", "M14 22v-5a3 3 0 0 1 3-3h5M7 8v1M15 8v1M7 12q4 4 8 0"],
    search: ["M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0ZM15 15l7 7"],
  };

  function icon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    for (const d of ICONS[name] || ICONS.info) {
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", d);
      svg.append(path);
    }
    return svg;
  }

  function hydrateIcons(root = document) {
    for (const element of root.querySelectorAll("[data-icon]")) element.replaceChildren(icon(element.dataset.icon));
  }

  function setButtonText(button, text, iconName = "") {
    button.replaceChildren(...(iconName ? [icon(iconName)] : []), node("span", "", text));
  }

  function localAssetPath(value, fallback = "") {
    for (const candidate of [value, fallback]) {
      if (typeof candidate !== "string" || !candidate) continue;
      try {
        const url = new URL(candidate, location.origin);
        if (url.origin === location.origin && url.pathname.startsWith("/static/assets/")) return state.assetMap[url.pathname]?.url || url.pathname + url.search;
      } catch { /* A missing asset uses a text fallback, never an arbitrary URL. */ }
    }
    return "";
  }

  function characterAsset(id, full = false) {
    const char = character(id);
    const source = full ? char?.detail_image || char?.avatar_image || char?.image : char?.avatar_image || char?.image;
    return localAssetPath(source, `/static/assets/characters/${encodeURIComponent(String(id).replace(".", "-"))}.webp`);
  }

  function sceneAsset(item) {
    if (!item) return "";
    return localAssetPath(item.image_url || item.image, `/static/assets/scenes/${encodeURIComponent(item.id)}.webp`);
  }

  function stickerSnapshot(message) {
    const source = message?.sticker || sticker(message?.sticker_id);
    if (!source) return { id: String(message?.sticker_id || ""), label: String(message?.text || "贴纸").replace(/^\[贴纸\]\s*/, ""), character_id: null, image: "" };
    return { id: String(source.id || message?.sticker_id || ""), label: String(source.label || "贴纸"), character_id: source.character_id == null ? null : String(source.character_id), image: localAssetPath(source.image) };
  }

  function stickerArt(item, className = "", caption = true) {
    const figure = node("figure", `sticker-art${className ? ` ${className}` : ""}`);
    const path = localAssetPath(item?.image);
    const label = String(item?.label || "贴纸");
    if (path) {
      const img = document.createElement("img");
      img.src = path; img.alt = label; img.width = 162; img.height = 162; img.loading = "lazy"; img.decoding = "async";
      img.addEventListener("error", () => {
        img.remove(); figure.dataset.imageMissing = "true";
        figure.prepend(node("span", "sticker-image-fallback", `贴纸「${label}」暂未载入`));
      }, { once: true });
      figure.append(img);
    } else {
      figure.dataset.imageMissing = "true";
      figure.append(node("span", "sticker-image-fallback", `贴纸「${label}」暂未载入`));
    }
    if (caption) figure.append(node("figcaption", "", label));
    return figure;
  }

  function renderBackdrop(item) {
    const image = $("room-backdrop-image");
    const path = sceneAsset(item);
    if (image.dataset.source === path) return;
    image.dataset.source = path;
    $("room-backdrop").dataset.ready = "false";
    $("room-backdrop").dataset.sceneId = item?.id || "";
    if (path) image.src = path;
    else image.removeAttribute("src");
  }

  function avatar(id, className = "", full = false) {
    const result = node("span", `character-avatar${className ? ` ${className}` : ""}${full ? " full-art" : ""}`);
    const char = character(id);
    result.dataset.tone = char?.band_id === "mujica" ? "mujica" : "mygo";
    if (!char) { result.append(icon("user")); return result; }
    result.dataset.character = char.id;
    const img = document.createElement("img");
    img.src = characterAsset(char.id, full);
    img.alt = full ? `${char.name}角色图` : "";
    img.loading = "lazy";
    img.decoding = "async";
    const crop = char.avatar_crop;
    const imageWidth = Number(char.avatar_width ?? char.image_width), imageHeight = Number(char.avatar_height ?? char.image_height);
    if (!full && crop && [crop.x, crop.y, crop.width, crop.height, imageWidth, imageHeight].every(Number.isFinite) &&
        crop.x >= 0 && crop.y >= 0 && crop.width > 0 && crop.height > 0 &&
        crop.x + crop.width <= imageWidth && crop.y + crop.height <= imageHeight) {
      const edge = Math.min(crop.width, crop.height);
      result.dataset.crop = "true";
      img.style.width = `${100 * imageWidth / edge}%`;
      img.style.height = `${100 * imageHeight / edge}%`;
      img.style.left = `${-100 * (crop.x + (crop.width - edge) / 2) / edge}%`;
      img.style.top = `${-100 * (crop.y + (crop.height - edge) / 2) / edge}%`;
    }
    img.addEventListener("error", () => { img.remove(); result.append(icon("user")); result.setAttribute("aria-label", `${char.name}的图片暂未载入`); }, { once: true });
    result.append(img);
    return result;
  }

  function participantName(person, room = state.room) {
    if (!person) return "观众";
    if (person.display_name) return String(person.display_name);
    if (person.character_id) return character(person.character_id)?.name || "演员";
    if (person.nickname) return String(person.nickname);
    const index = members(room).findIndex((item) => sameId(item.id, person.id));
    return `观众 ${Math.max(1, index + 1)}`;
  }

  function messageAuthor(message) {
    const saved = typeof message.author === "string" ? message.author : "";
    if (saved && !/^旅人[·.][0-9a-f]+$/i.test(saved)) return saved;
    if (message.character_id) return character(message.character_id)?.name || "演员";
    const member = members().find((person) => sameId(person.id, message.member_id));
    return member ? participantName(member) : sameId(message.member_id, state.room?.host_member_id) ? "房主" : "观众";
  }

  function sceneIllustration(item) {
    // A simple local geometric fallback, used only if a backdrop image fails.
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 320 160");
    svg.setAttribute("class", "scene-illustration");
    svg.setAttribute("aria-hidden", "true");
    const outdoor = /户外|街|公园|河|广场|雕像|河畔/.test(`${item?.category || ""}${item?.title || ""}`);
    const school = /学校|校园|学园|屋顶/.test(`${item?.category || ""}${item?.title || ""}`);
    const add = (tag, attrs) => { const el = document.createElementNS(SVG_NS, tag); for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value)); svg.append(el); };
    add("rect", { width: 320, height: 160, fill: outdoor ? "#d9f1f5" : school ? "#e6eafa" : "#e4f0fa" });
    add("circle", { cx: 265, cy: 36, r: 23, fill: "#fff2bc" });
    if (outdoor) {
      add("path", { d: "M0 105Q80 69 157 106T320 100V160H0Z", fill: "#b5dacc" });
      add("path", { d: "M80 160 147 105h25l58 55", fill: "#f8f2e0" });
      for (const x of [42, 272]) { add("rect", { x: x-3, y: 73, width: 6, height: 54, fill: "#a5bfc0" }); add("circle", { cx: x, cy: 66, r: 25, fill: "#8ccbc0" }); }
      add("path", { d: "M212 121h41M217 115h32M218 121v13M247 121v13", fill: "none", stroke: "#8babb6", "stroke-width": 4 });
    } else {
      add("rect", { x: 31, y: 28, width: 258, height: 109, rx: 4, fill: school ? "#d2d9ec" : "#cedfeb" });
      for (const x of [48, 96, 176, 224]) { add("rect", { x, y: 46, width: 32, height: 40, fill: "#f4fbff" }); add("path", { d: `M${x} 65h32M${x+16} 46v40`, stroke: "#acccd9", "stroke-width": 2 }); }
      add("rect", { x: 129, y: 80, width: 31, height: 57, fill: "#91b6c7" });
      add("path", { d: "M20 139h280M44 112h69M184 111h80", stroke: "#a6c4d2", "stroke-width": 5 });
      if (school) add("path", { d: "m112 28 34-19 34 19", fill: "#b6c4e3" });
      else add("rect", { x: 117, y: 29, width: 67, height: 17, rx: 3, fill: "#f1c8da" });
    }
    return svg;
  }

  function sceneVisual(item) {
    const wrap = node("div", "scene-visual");
    if (!item) { wrap.append(sceneIllustration(null)); return wrap; }
    const img = document.createElement("img");
    img.src = sceneAsset(item);
    img.alt = item.title || "场景背景";
    img.loading = "lazy";
    img.decoding = "async";
    img.addEventListener("error", () => { img.remove(); wrap.append(sceneIllustration(item)); }, { once: true });
    wrap.append(img);
    return wrap;
  }

  function updateTheme() {
    const dark = document.documentElement.dataset.theme === "dark";
    $("theme-icon").replaceChildren(icon(dark ? "sun" : "moon"));
    $("theme-toggle").setAttribute("aria-label", dark ? "切换浅色主题" : "切换夜间主题");
    $("theme-toggle").setAttribute("aria-pressed", String(dark));
    $("theme-toggle").title = dark ? "切换浅色主题" : "切换夜间主题";
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = dark ? "#202b40" : "#f6f8fc";
  }

  function toggleTheme() {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("onstage.theme", theme); }
    catch { toast("主题已切换。浏览器暂不允许保存偏好，重新打开时会使用浅色。", "info"); }
    updateTheme();
  }

  function updateViewport() {
    const height = window.visualViewport?.height || window.innerHeight;
    document.documentElement.style.setProperty("--viewport-height", `${Math.max(240, Math.round(height))}px`);
    document.documentElement.style.setProperty("--network-height", `${$("network-banner").hidden ? 0 : $("network-banner").offsetHeight}px`);
    document.documentElement.style.setProperty("--preview-height", `${$("preview-banner").hidden ? 0 : $("preview-banner").offsetHeight}px`);
    const editable = document.activeElement?.matches("input,textarea,select");
    const small = window.matchMedia("(max-width: 720px)").matches;
    document.documentElement.classList.toggle("keyboard-open", Boolean(small && editable && (height < window.innerHeight - 70 || height < 560)));
  }

  function resizeComposer() {
    const input = $("message-input");
    if (!input.getClientRects().length) return;
    input.style.height = "";
    const style = getComputedStyle(input);
    const min = parseFloat(style.minHeight) || 47;
    const max = parseFloat(style.maxHeight) || 145;
    input.style.height = `${Math.min(max, Math.max(min, input.scrollHeight + 2))}px`;
  }

  async function showLobbySection(id) {
    if (!state.authenticated) return;
    if (state.room || routeRoomId()) await backToLobby();
    $(id)?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }

  function toneForScenario(item) {
    if (!item || !Array.isArray(item.band_ids)) return "shared";
    if (item.band_ids.length > 1) return "shared";
    return item.band_ids[0] === "mujica" ? "mujica" : "mygo";
  }

  function timeLabel(value, includeDate = false) {
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "时间待确认";
    const options = includeDate
      ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
      : { hour: "2-digit", minute: "2-digit", hour12: false };
    return date.toLocaleString("zh-CN", options);
  }

  function readableError(error) {
    if (error instanceof ApiError) return error.message;
    if (PREVIEW) return error?.name === "AbortError" ? "本机操作尚未确认，请重新读取记录后重试。" : "暂时无法读取本机预览。请重试，并检查浏览器是否允许保存数据。";
    if (error?.name === "AbortError") return "请求等待超时，尚未收到服务器确认。请检查连接后重试。";
    return "暂时无法连接服务器，请检查网络后重试。";
  }

  async function api(path, { method = "GET", body, allow401 = false, timeout = 15000, signal } = {}) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeout);
    const abortFromCaller = () => controller.abort();
    if (signal?.aborted) controller.abort();
    else signal?.addEventListener("abort", abortFromCaller, { once: true });
    const requestCsrf = state.csrf;
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET" && requestCsrf) headers["X-CSRF-Token"] = requestCsrf;
    try {
      if (PREVIEW) {
        if (!previewAdapter) throw new ApiError("本机预览组件未载入，请刷新页面再试。", 503);
        try { return await previewAdapter.request(path, { method, body, headers, signal: controller.signal }); }
        catch (error) {
          if (error?.name === "AbortError") throw error;
          if (error?.status === 401 && !allow401 && state.authenticated && state.csrf === requestCsrf) restartPreviewSession();
          throw new ApiError(error?.message || "本机操作尚未确认，请重新读取记录。", Number(error?.status) || 0);
        }
      }
      const response = await fetch(`${(window.ONSTAGE_CONFIG?.apiBaseUrl || "").replace(/\/$/, "")}${path}`, {
        method,
        headers,
        credentials: "include",
        cache: "no-store",
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      let data;
      try { data = await response.json(); }
      catch { throw new ApiError(`服务器未返回可识别的响应（HTTP ${response.status}）。`, response.status); }
      if (!response.ok) {
        if (response.status === 401 && !allow401 && state.authenticated && state.csrf === requestCsrf) expireSession();
        const detail = typeof data.detail === "string" ? data.detail : `请求未完成（HTTP ${response.status}）。`;
        throw new ApiError(detail, response.status);
      }
      return data;
    } finally {
      window.clearTimeout(timer);
      signal?.removeEventListener("abort", abortFromCaller);
    }
  }

  function toast(message, kind = "info", timeout = 5500) {
    const item = node("div", "toast");
    item.dataset.kind = kind;
    item.append(node("p", "", message));
    const dismiss = actionButton("×", "", () => item.remove());
    dismiss.setAttribute("aria-label", "关闭提示");
    item.append(dismiss);
    $("toasts").append(item);
    while ($("toasts").children.length > 4) $("toasts").firstElementChild.remove();
    if (timeout > 0) window.setTimeout(() => item.remove(), timeout);
  }

  function readStorage(key) {
    try { return sessionStorage.getItem(storagePrefix + key); }
    catch { return null; }
  }

  function writeStorage(key, value) {
    try {
      if (value === null) sessionStorage.removeItem(storagePrefix + key);
      else sessionStorage.setItem(storagePrefix + key, value);
      return true;
    } catch {
      if (!state.storageWarning) {
        state.storageWarning = true;
        toast("浏览器暂存不可用。草稿和待确认发言仍在本页，请保存或完成确认后再刷新、关闭。", "error", 10000);
      }
      return false;
    }
  }

  async function sessionScope(token) {
    // A non-credential storage namespace; no session cookie or invite code is stored by JavaScript.
    if (globalThis.crypto?.subtle) {
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
      return Array.from(new Uint8Array(digest).slice(0, 12), (v) => v.toString(16).padStart(2, "0")).join("");
    }
    let hash = 2166136261;
    for (const char of token) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
    return `session-${(hash >>> 0).toString(16)}`;
  }

  function loadPending() {
    state.pending.clear();
    if (!state.scope) return;
    try {
      const items = JSON.parse(readStorage(`pending.${state.scope}`) || "[]");
      if (!Array.isArray(items)) return;
      for (const item of items) {
        if (!item || typeof item.room_id !== "string" || !/^[a-zA-Z0-9_-]{1,120}$/.test(item.room_id) ||
            typeof item.client_message_id !== "string" || !CHANNELS[item.channel] ||
            item.client_message_id.length < 8 || item.client_message_id.length > 80 ||
            (item.content_type !== undefined && !["text", "sticker"].includes(item.content_type))) continue;
        const isSticker = item.content_type === "sticker";
        if (isSticker ? typeof item.sticker_id !== "string" || !/^[a-zA-Z0-9_.-]{1,120}$/.test(item.sticker_id) : typeof item.text !== "string" || item.text.length > 4000) continue;
        state.pending.set(item.room_id, {
          room_id: item.room_id,
          scope: state.scope,
          character_id: item.character_id || null,
          author: item.author || "我",
          member_id: typeof item.member_id === "string" ? item.member_id : null,
          content_type: isSticker ? "sticker" : "text",
          text: isSticker ? `[贴纸] ${stickerSnapshot(item).label}` : item.text,
          ...(isSticker ? { sticker_id: item.sticker_id, sticker: stickerSnapshot(item) } : {}),
          channel: item.channel,
          client_message_id: item.client_message_id,
          status: "uncertain",
          detail: "页面恢复后，请核对记录或原样重试以确认这条内容。",
        });
      }
    } catch { /* Corrupt local transient state is not a source of server state. */ }
  }

  function persistPending() {
    if (state.scope) writeStorage(`pending.${state.scope}`, JSON.stringify(Array.from(state.pending.values())));
  }

  function loadDrafts() {
    state.drafts.clear();
    if (!state.scope) return;
    try {
      const items = JSON.parse(readStorage(`drafts.${state.scope}`) || "[]");
      if (!Array.isArray(items)) return;
      for (const entry of items) {
        if (!Array.isArray(entry) || entry.length !== 2) continue;
        const [key, value] = entry;
        if (typeof key !== "string" || !/^[a-zA-Z0-9_-]{1,120}:(stage|backstage)$/.test(key) ||
            typeof value !== "string" || value.length > 10000) continue;
        state.drafts.set(key, value);
      }
    } catch { /* Invalid local drafts never affect server records or permissions. */ }
  }

  function persistDrafts() {
    if (state.scope) writeStorage(`drafts.${state.scope}`, JSON.stringify(Array.from(state.drafts.entries()).filter((entry) => entry[1])));
  }

  function loadStickerDrafts() {
    state.stickerDrafts.clear();
    if (!state.scope) return;
    try {
      const items = JSON.parse(readStorage(`sticker-drafts.${state.scope}`) || "[]");
      if (!Array.isArray(items)) return;
      for (const entry of items) {
        if (!Array.isArray(entry) || entry.length !== 2) continue;
        const [key, value] = entry;
        if (typeof key === "string" && /^[a-zA-Z0-9_-]{1,120}:(stage|backstage)$/.test(key) &&
            typeof value === "string" && /^[a-zA-Z0-9_.-]{1,120}$/.test(value)) state.stickerDrafts.set(key, value);
      }
    } catch { /* Invalid local choices cannot change the catalog or send messages. */ }
  }

  function persistStickerDrafts() {
    if (state.scope) writeStorage(`sticker-drafts.${state.scope}`, JSON.stringify(Array.from(state.stickerDrafts.entries())));
  }

  function clearSessionContent(scope) {
    if (!scope) return;
    writeStorage(`pending.${scope}`, null);
    writeStorage(`drafts.${scope}`, null);
    writeStorage(`sticker-drafts.${scope}`, null);
  }

  function pruneOtherSessionContent(scope) {
    try {
      for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
        const key = sessionStorage.key(index);
        if (["pending", "drafts", "sticker-drafts"].some((kind) => key?.startsWith(`${storagePrefix}${kind}.`) && key !== `${storagePrefix}${kind}.${scope}`)) sessionStorage.removeItem(key);
      }
    } catch { /* Storage may be disabled; in-memory state remains scoped to this session. */ }
  }

  function freshMessageId() {
    if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
    if (globalThis.crypto?.getRandomValues) {
      return Array.from(crypto.getRandomValues(new Uint8Array(20)), (value) => value.toString(16).padStart(2, "0")).join("");
    }
    throw new ApiError("当前浏览器不能生成安全的发言编号。请使用支持 HTTPS 的现代浏览器。");
  }

  function setView(name) {
    for (const view of ["loading", "lobby", "room"]) $(view + "-view").hidden = name !== view;
    document.body.dataset.view = name;
    $("feedback-button").disabled = !state.authenticated;
    updateConnection();
    updateViewport();
  }

  function routeRoomId() {
    try {
      const id = new URLSearchParams(location.hash.slice(1)).get("room");
      return id && id.length <= 120 && /^[a-zA-Z0-9_-]+$/.test(id) ? id : null;
    } catch { return null; }
  }

  function setRoomRoute(id, replace = false) {
    const next = id ? `#room=${encodeURIComponent(String(id))}` : "";
    if (location.hash === next) return;
    history[replace ? "replaceState" : "pushState"]({}, "", location.pathname + location.search + next);
  }

  function closeAllDialogs() {
    for (const id of ["create-dialog", "settings-dialog", "preferences-dialog", "report-dialog", "reports-dialog", "resolve-dialog", "confirm-dialog", "sticker-dialog"]) {
      if ($(id).open) $(id).close("cancel");
    }
  }

  function confirmAction({ title, description, accept = "确认", cancel = "取消", danger = false, extra = null }) {
    // Keep one confirmation active until its own close event settles. Reusing a
    // dialog before that event fires can let an old close accept a new action.
    if ($("confirm-dialog").open || state.confirmResolver) return Promise.resolve(false);
    $("confirm-title").textContent = title;
    $("confirm-description").textContent = description;
    $("confirm-accept").textContent = accept;
    $("confirm-cancel").textContent = cancel;
    $("confirm-accept").className = `button ${danger ? "danger" : "primary"}`;
    $("confirm-extra").replaceChildren(...(extra ? [extra] : []));
    $("confirm-dialog").returnValue = "cancel";
    $("confirm-dialog").showModal();
    return new Promise((resolve) => { state.confirmResolver = resolve; });
  }

  async function acceptSession(data) {
    if (!data.authenticated || typeof data.csrf_token !== "string") throw new ApiError("连接尚未完成，请重新尝试。");
    configureTransport(data.transport);
    const sessionGeneration = state.sessionGeneration;
    const nickname = typeof data.nickname === "string" ? data.nickname.trim() : "";
    if (data.requires_nickname || !nickname) {
      state.csrf = data.csrf_token;
      state.version = typeof data.version === "string" ? data.version : "";
      showGuestEntry();
      return;
    }
    const scope = typeof data.storage_scope === "string" ? data.storage_scope : await sessionScope(data.csrf_token);
    if (sessionGeneration !== state.sessionGeneration) return;
    state.authenticated = true;
    state.awaitingNickname = false;
    state.csrf = data.csrf_token;
    state.nickname = nickname;
    state.scope = scope;
    const acceptedScope = state.scope;
    state.version = typeof data.version === "string" ? data.version : "";
    pruneOtherSessionContent(state.scope);
    loadPending();
    loadDrafts();
    loadStickerDrafts();
    $("build-version").textContent = state.version ? state.version : "";
    $("loading-error").hidden = true;
    $("retry-bootstrap").hidden = true;
    $("guest-form").hidden = true;
    setView("lobby");
    renderLoading($("scenario-list"), "正在准备场景…");
    renderLoading($("room-list"), "正在寻找亮着灯的房间…");
    try {
      const catalogRequest = api("/api/catalog");
      const [catalog] = await Promise.all([catalogRequest, state.artworkReady]);
      if (!state.authenticated || state.scope !== acceptedScope) return;
      if (!Array.isArray(catalog.scenarios) || !Array.isArray(catalog.characters) || !Array.isArray(catalog.bands)) throw new ApiError("场景暂未准备好，请稍后刷新。");
      state.catalog = { ...catalog, stickers: Array.isArray(catalog.stickers) ? catalog.stickers : [] };
      renderScenarios();
      renderLobbyCharacters();
      await fetchRooms();
      const target = routeRoomId();
      if (target) await enterRoom(target, true);
      if (state.recoveryNotice) { toast(state.recoveryNotice, "info", 10000); state.recoveryNotice = ""; }
    } catch (error) {
      if (state.authenticated && state.scope === acceptedScope) {
        $("lobby-error").textContent = readableError(error);
        $("lobby-error").hidden = false;
        $("scenario-list").setAttribute("aria-busy", "false");
        $("room-list").setAttribute("aria-busy", "false");
        toast(readableError(error), "error");
      }
    }
  }

  function expireSession() {
    if (!state.authenticated) return;
    state.navigation += 1;
    saveDraft();
    stopRoom();
    clearSessionContent(state.scope);
    state.authenticated = false;
    state.csrf = "";
    state.nickname = "";
    state.awaitingNickname = false;
    state.scope = "";
    state.pending.clear();
    state.drafts.clear();
    state.stickerDrafts.clear();
    state.catalog = { bands: [], characters: [], scenarios: [], stickers: [] };
    state.rooms = [];
    state.reportList = [];
    state.reportsGeneration += 1;
    closeAllDialogs();
    setView("loading");
    $("guest-form").hidden = true;
    $("loading-spark").hidden = false;
    $("loading-title").textContent = "正在重新连接…";
    $("loading-note").textContent = "正在检查连接，昵称确认后就能重新进入。";
    state.recoveryNotice = "已重新进入现场。之前的连接已过期，原角色与房主身份不能由新连接接管。";
    const mayRetry = Date.now() - state.lastAutoRecovery > 10000;
    state.lastAutoRecovery = Date.now();
    if (mayRetry) {
      state.recoverRequested = true;
      if (!state.bootstrapBusy) { state.recoverRequested = false; window.setTimeout(connectAnonymous, 0); }
    } else {
      $("loading-error").textContent = "这次连接没有完成，请重新连接。";
      $("loading-error").hidden = false;
      $("retry-bootstrap").hidden = false;
    }
  }

  async function connectAnonymous() {
    if (state.bootstrapBusy) return;
    const sessionGeneration = state.sessionGeneration;
    state.bootstrapBusy = true;
    $("retry-bootstrap").disabled = true;
    $("loading-error").hidden = true;
    try {
      const result = await api("/api/session", { allow401: true });
      if (sessionGeneration !== state.sessionGeneration) return;
      await acceptSession(result);
    } catch (error) {
      if (sessionGeneration !== state.sessionGeneration) return;
      if (error?.status === 401) {
        state.csrf = "";
        showGuestEntry();
        return;
      }
      setView("loading");
      state.awaitingNickname = false;
      $("guest-form").hidden = true;
      $("loading-spark").hidden = false;
      $("loading-title").textContent = PREVIEW ? "本机预览暂未打开" : "差一点，就到现场了";
      $("loading-note").textContent = PREVIEW ? "可以重新尝试，或重置这个浏览器的预览记录。" : "检查一下网络，再试一次。";
      $("loading-error").textContent = readableError(error);
      $("loading-error").hidden = false;
      $("retry-bootstrap").hidden = false;
    } finally {
      state.bootstrapBusy = false;
      $("retry-bootstrap").disabled = false;
      $("guest-submit").disabled = false;
      $("guest-nickname").disabled = false;
      if (state.recoverRequested) { state.recoverRequested = false; window.setTimeout(connectAnonymous, 0); }
    }
  }

  function guestEntryLabel() {
    return routeRoomId() ? state.csrf ? "继续进入房间" : "以旁观者进入" : "进入大厅";
  }

  function showGuestEntry(focus = true) {
    state.authenticated = false;
    state.awaitingNickname = true;
    const invited = Boolean(routeRoomId());
    setView("loading");
    $("loading-title").textContent = invited ? "先留一个称呼，再走进房间" : "下一句，怎么称呼你？";
    $("loading-note").textContent = invited
      ? state.csrf ? "补上昵称，继续进入房间。已参与的角色与记录会保留。" : "填写昵称后，先以旁观者身份进入。想出演时再选角色。"
      : PREVIEW ? "填一个昵称，体验场景、角色和对话界面。" : "填一个昵称就可以进入现场，找个房间旁观，或开启自己的故事。";
    $("loading-spark").hidden = true;
    $("loading-error").hidden = true;
    $("retry-bootstrap").hidden = true;
    $("guest-form").hidden = false;
    $("guest-error").hidden = true;
    setButtonText($("guest-submit"), guestEntryLabel(), "arrow-right");
    if (focus) window.setTimeout(() => {
      if (state.awaitingNickname && !$("guest-nickname").disabled) $("guest-nickname").focus({ preventScroll: true });
    }, 0);
  }

  async function submitGuest(event) {
    event.preventDefault();
    if (state.bootstrapBusy || state.guestComposing || !state.awaitingNickname) return;
    const rawNickname = $("guest-nickname").value;
    const nickname = rawNickname.trim();
    if (!nickname || Array.from(nickname).length > 24 || /[\p{Cc}\p{Cf}\p{Cs}\p{Zl}\p{Zp}]/u.test(rawNickname)) {
      $("guest-error").textContent = "请填写 1–24 字的昵称，不要包含换行或控制字符。";
      $("guest-error").hidden = false;
      $("guest-nickname").setAttribute("aria-invalid", "true");
      $("guest-nickname").focus();
      return;
    }
    const sessionGeneration = state.sessionGeneration;
    state.bootstrapBusy = true;
    $("guest-error").hidden = true;
    $("guest-nickname").removeAttribute("aria-invalid");
    $("guest-nickname").disabled = true;
    $("guest-submit").disabled = true;
    setButtonText($("guest-submit"), "正在进入…");
    try {
      const result = await api("/api/session", { method: "POST", body: { nickname }, allow401: true });
      if (sessionGeneration !== state.sessionGeneration) return;
      await acceptSession(result);
    } catch (error) {
      if (sessionGeneration !== state.sessionGeneration) return;
      $("guest-error").textContent = readableError(error);
      $("guest-error").hidden = false;
    } finally {
      state.bootstrapBusy = false;
      $("guest-nickname").disabled = false;
      $("guest-submit").disabled = false;
      setButtonText($("guest-submit"), guestEntryLabel(), "arrow-right");
      if (state.recoverRequested) { state.recoverRequested = false; window.setTimeout(connectAnonymous, 0); }
    }
  }

  function renderLoading(container, text) {
    container.setAttribute("aria-busy", "true");
    container.replaceChildren(node("div", "loading-state", text));
  }

  function renderLobbyCharacters() {
    const fragment = document.createDocumentFragment();
    for (const char of state.catalog.characters) {
      const button = actionButton("", "lobby-character", () => showCharacter(char.id));
      button.dataset.tone = char.band_id;
      button.setAttribute("aria-label", `查看 ${char.name}`);
      button.append(avatar(char.id), node("span", "", char.name));
      fragment.append(button);
    }
    $("lobby-characters").replaceChildren(fragment);
  }

  function renderScenarios() {
    $("scene-count-label").textContent = `${state.catalog.scenarios.length} PLACES / YOUR OWN STORY`;
    const categories = [...new Set(state.catalog.scenarios.map((item) => item.category || "其他场景"))];
    if (state.sceneFilter !== "all" && !categories.includes(state.sceneFilter)) state.sceneFilter = "all";
    const filters = document.createDocumentFragment();
    for (const category of ["all", ...categories]) {
      const button = actionButton(category === "all" ? "全部场景" : category, `filter-chip${state.sceneFilter === category ? " selected" : ""}`, () => { state.sceneFilter = category; renderScenarios(); });
      button.setAttribute("aria-pressed", String(state.sceneFilter === category));
      filters.append(button);
    }
    $("scene-filters").replaceChildren(filters);
    const fragment = document.createDocumentFragment();
    state.catalog.scenarios.forEach((item, index) => {
      if (state.sceneFilter !== "all" && (item.category || "其他场景") !== state.sceneFilter) return;
      const card = node("article", "scenario-card");
      const cover = actionButton("", "scenario-image-button", () => openCreate(item.id));
      cover.setAttribute("aria-label", `在${item.title}创建房间`);
      cover.append(sceneVisual(item), node("span", "scene-number", `SCENE / ${String(index + 1).padStart(2, "0")}`));
      const copy = node("div", "scenario-card-copy");
      const bottom = node("div", "scenario-card-bottom");
      const start = actionButton("", "button quiet small", () => openCreate(item.id));
      start.append(node("span", "", "在这里开聊"), icon("arrow-up-right"));
      bottom.append(node("span", "scene-category", item.category || "自由场景"), start);
      copy.append(node("h3", "", item.title), node("p", "", item.summary || ""), bottom);
      card.append(cover, copy);
      fragment.append(card);
    });
    $("scenario-list").replaceChildren(fragment);
    $("scenario-list").setAttribute("aria-busy", "false");
  }

  async function fetchRooms({ page = 0, cursor = "" } = {}) {
    if (!state.authenticated) return;
    state.roomListRequest?.controller.abort();
    const scope = state.scope;
    const filter = state.filter;
    const request = { scope, controller: new AbortController() };
    state.roomListRequest = request;
    $("refresh-rooms").disabled = true;
    $("refresh-rooms").textContent = "读取中…";
    try {
      updateRoomPagination(true);
      const params = new URLSearchParams({ limit: "24", filter });
      if (cursor) params.set("cursor", cursor);
      const result = await api(`/api/rooms?${params}`, { signal: request.controller.signal });
      if (!state.authenticated || state.scope !== scope || state.roomListRequest !== request || state.filter !== filter) return;
      state.rooms = Array.isArray(result.rooms) ? result.rooms : [];
      state.roomPage = page;
      if (!page) state.roomCursors = [""];
      state.roomCursors[page] = cursor;
      state.roomCursors.length = page + 1;
      state.roomHasMore = Boolean(result.has_more && result.next_cursor);
      if (state.roomHasMore) state.roomCursors.push(result.next_cursor);
      $("lobby-error").hidden = true;
      $("rooms-updated").textContent = `更新于 ${timeLabel(new Date())}`;
      renderRooms();
    } catch (error) {
      if (state.authenticated && state.scope === scope && state.roomListRequest === request && !request.controller.signal.aborted) {
        $("lobby-error").textContent = readableError(error);
        $("lobby-error").hidden = false;
        if (!state.rooms.length) $("room-list").replaceChildren(node("p", "loading-state", "场次暂未读取成功，点击刷新重新尝试。"));
      }
    } finally {
      if (state.roomListRequest === request) {
        state.roomListRequest = null;
        $("refresh-rooms").disabled = false;
        $("refresh-rooms").textContent = "刷新场次";
        $("room-list").setAttribute("aria-busy", "false");
        updateRoomPagination();
      }
    }
  }

  function updateRoomPagination(busy = false) {
    $("rooms-previous").disabled = busy || state.roomPage === 0;
    $("rooms-next").disabled = busy || !state.roomHasMore;
    $("rooms-page").textContent = `第 ${state.roomPage + 1} 页 · 本页 ${state.rooms.length} 间`;
    $("room-pagination").setAttribute("aria-busy", String(busy));
  }

  function renderRooms() {
    const filtered = state.rooms.filter((room) => state.filter === "ended" ? room.status === "ended" : state.filter === "mine" ? Boolean(room.me) : room.status !== "ended");
    $("room-count").textContent = `${filtered.length} / 本页`;
    const fragment = document.createDocumentFragment();
    if (!filtered.length) {
      const empty = node("div", "empty-state");
      const symbol = node("span", "empty-symbol"); symbol.append(icon("music"));
      const title = state.filter === "ended" ? "故事还在开始" : state.filter === "mine" ? "你的第一间房，正等着你" : "把第一声，留给你";
      const description = state.filter === "ended" ? "结束的房间会留在这里，随时可以回看。" : PREVIEW ? "往下挑个喜欢的场景，体验角色、对话和贴纸。记录会保存在此浏览器。" : "往下挑个喜欢的场景，发起一场对话。也可以稍后刷新，等同伴入场。";
      empty.append(symbol, node("h3", "", title), node("p", "", description));
      fragment.append(empty);
    }
    for (const room of filtered) {
      const item = scenario(room.scenario_id);
      const card = node("article", "room-row"); card.dataset.phase = room.status;
      const top = node("div", "room-card-top");
      const location = node("div", "room-scene-label"); location.append(icon("location"), node("span", "", item?.title || "自由场景"));
      const phase = node("span", "phase-label", PHASES[room.status] || "连接中"); phase.dataset.phase = room.status;
      top.append(location, phase);
      const title = node("h3", "", room.title);
      if (room.me) title.append(node("span", "room-mine", room.me.is_host ? "我创建的" : "已加入"));
      const bottom = node("div", "room-card-bottom");
      const cast = node("div", "room-mini-cast");
      for (const person of actors(room).slice(0, 4)) cast.append(avatar(person.character_id));
      if (!actors(room).length) cast.append(icon("people"));
      cast.append(node("span", "small-copy muted", PREVIEW ? `${actors(room).length} 个已选角色 · 本机记录` : `${actors(room).length} 位出演 · ${Number(room.online_count) || 0} 人在线`));
      const label = room.status === "ended" ? "回看记录" : room.me?.character_id ? "回到房间" : room.me ? "继续旁观" : "以旁观者进入";
      const enter = actionButton("", "button small join-room", async () => {
        enter.disabled = true; setButtonText(enter, "正在进入");
        try { await enterRoom(room.id); }
        finally { enter.disabled = false; setButtonText(enter, label, "arrow-right"); }
      });
      setButtonText(enter, label, "arrow-right");
      bottom.append(cast, enter);
      card.append(top, title, node("p", "room-row-summary", room.announcement || room.background || item?.summary || "等一句来自你的回应。"), bottom);
      fragment.append(card);
    }
    $("room-list").replaceChildren(fragment);
    $("room-list").setAttribute("aria-busy", "false");
  }

  function openCreate(id) {
    const selected = scenario(id);
    if (!selected || state.createBusy) return;
    if (!state.room && routeRoomId()) { state.navigation += 1; setRoomRoute(null); }
    state.createScenario = id;
    $("create-title").textContent = selected.title;
    $("create-summary").textContent = selected.summary || "";
    $("create-scene-art").replaceChildren(sceneVisual(selected));
    $("new-room-title").value = "";
    $("new-room-title").placeholder = selected.title;
    $("new-room-background").value = selected.opening || selected.summary || "";
    $("new-room-announcement").value = "";
    $("create-error").hidden = true;
    $("create-dialog").showModal();
  }

  async function createRoom(event) {
    event.preventDefault();
    if (state.createBusy || !state.createScenario) return;
    state.createBusy = true;
    $("create-submit").disabled = true;
    setButtonText($("create-submit"), "正在创建…");
    $("create-error").hidden = true;
    const title = $("new-room-title").value.trim();
    const background = $("new-room-background").value;
    const announcement = $("new-room-announcement").value;
    const scope = state.scope;
    const navigation = state.navigation;
    try {
      const result = await api("/api/rooms", { method: "POST", body: { scenario_id: state.createScenario, background, announcement, ...(title ? { title } : {}) } });
      if (!state.authenticated || state.scope !== scope) return;
      if (!result.room?.id || !result.room.me) throw new ApiError("房间可能已创建，请先刷新大厅核对。");
      $("create-dialog").close();
      if (navigation !== state.navigation) { toast("房间已创建，可以从大厅进入。"); fetchRooms(); return; }
      state.navigation += 1;
      activateRoom(result.room);
      setMobilePanel("seats");
      toast(PREVIEW ? "房间准备好了。选一个角色，开始本机预览。" : "房间准备好了。选个空角色，或者邀请朋友一起进来。");
    } catch (error) {
      if (state.scope !== scope) return;
      $("create-error").textContent = readableError(error);
      $("create-error").hidden = false;
      fetchRooms();
    } finally {
      state.createBusy = false;
      $("create-submit").disabled = false;
      setButtonText($("create-submit"), "创建房间", "arrow-up-right");
    }
  }

  async function enterRoom(id, restoring = false) {
    if (!state.authenticated) return;
    if (sameId(state.room?.id, id)) { setView("room"); scheduleSync(); return; }
    if (sameId(state.joinRequest?.id, id) && state.joinRequest.scope === state.scope && state.joinRequest.navigation === state.navigation) return;
    const request = { id: String(id), scope: state.scope, navigation: state.navigation + 1 };
    state.joinRequest = request;
    const navigation = ++state.navigation;
    if (state.room) {
      saveDraft();
      stopRoom();
      setView("lobby");
    }
    // Keep an invitation target through a temporary outage, including refresh.
    setRoomRoute(id, restoring);
    try {
      const result = await api(`${roomPath(id)}/join`, { method: "POST", body: {} });
      if (navigation !== state.navigation || !state.authenticated) return;
      if (!result.room?.me) throw new ApiError("尚未取得本场成员身份，请重新从大厅进入。");
      activateRoom(result.room, restoring);
    } catch (error) {
      if (navigation !== state.navigation || !state.authenticated) return;
      const unavailable = error instanceof ApiError && [403, 404].includes(error.status);
      if (unavailable) setRoomRoute(null, restoring);
      setView("lobby");
      toast(readableError(error), "error", 8500);
      await fetchRooms();
      if (!unavailable && navigation === state.navigation && !state.room) {
        $("lobby-error").textContent = `还没进入目标房间：${readableError(error)} 链接已保留，点击“刷新”或连接恢复后会继续尝试。`;
        $("lobby-error").hidden = false;
      }
    } finally {
      if (state.joinRequest === request) state.joinRequest = null;
    }
  }

  function activateRoom(room, replaceRoute = false) {
    stopRoom();
    state.room = room;
    if (location.pathname.startsWith("/play/") && window.parent !== window) window.parent.postMessage({ type: "onstage:room-updated", room_id: String(room.id) }, location.origin);
    state.channel = "stage";
    state.mobilePanel = "stage";
    state.cursors = { stage: 0, backstage: 0 };
    state.messages = { stage: new Map(), backstage: new Map() };
    state.unread = { stage: 0, backstage: 0 };
    state.initialSynced = false;
    state.followHistoryStart = room.status === "ended";
    state.messageSignature = "";
    state.lastSync = 0;
    state.syncError = "";
    state.socketError = "";
    if (!PREVIEW && state.transport.type === "poll") {
      // A fresh room-view suffix prevents a delayed departure request from a
      // previous view from marking a newly opened view of this room offline.
      state.presenceId = `${state.presenceBaseId}.${state.generation}`;
    }
    $("scene-details").open = false;
    $("message-input").value = state.drafts.get(`${room.id}:stage`) || "";
    setRoomRoute(room.id, replaceRoute);
    document.title = `${room.title} · 上皮`;
    renderRoom();
    setMobilePanel("stage");
    setView("room");
    window.scrollTo(0, 0);
    scheduleSync(0);
    connectSocket();
  }

  function stopRoom() {
    state.presenceController?.abort();
    releasePresence();
    state.generation += 1;
    state.syncController?.abort();
    state.syncController = null;
    window.clearTimeout(state.syncTimer);
    window.clearTimeout(state.reconnectTimer);
    window.clearTimeout(state.pollTimer);
    window.clearTimeout(state.presenceTimer);
    window.clearInterval(state.heartbeatTimer);
    state.syncTimer = state.reconnectTimer = state.pollTimer = state.heartbeatTimer = null;
    state.presenceTimer = state.presenceController = null;
    state.presenceId = "";
    state.lastPresence = 0;
    state.presenceError = "";
    if (state.socket) {
      const socket = state.socket;
      state.socket = null;
      socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
      socket.close(1000, "Leaving page");
    }
    state.socketMode = "idle";
    state.reconnectAttempts = 0;
    state.syncRunning = false;
    state.syncQueued = false;
    state.syncError = "";
    state.socketError = "";
    state.room = null;
    state.messages = { stage: new Map(), backstage: new Map() };
    state.cursors = { stage: 0, backstage: 0 };
    state.messageSignature = "";
    $("message-feed").replaceChildren();
    $("pending-message").hidden = true;
    $("message-input").value = "";
    if ($("sticker-dialog").open) $("sticker-dialog").close("cancel");
    $("sticker-preview").replaceChildren();
    $("room-backdrop-image").removeAttribute("src");
    $("room-backdrop-image").dataset.source = "";
    $("room-backdrop").dataset.ready = "false";
    document.title = "上皮 · ONSTAGE";
  }

  async function backToLobby({ replace = false } = {}) {
    if (!state.authenticated) { setView("loading"); return; }
    state.navigation += 1;
    saveDraft();
    stopRoom();
    setRoomRoute(null, replace);
    setView("lobby");
    await fetchRooms();
  }

  async function leaveRoom() {
    if (!state.room || isRoomBusy()) return;
    const room = state.room;
    if (room.status === "ended") {
      await backToLobby();
      toast(PREVIEW ? "已返回大厅。本机角色和记录已保留，可以随时回看。" : "已返回大厅。本场成员资格保留，可按原有权限回看记录。");
      return;
    }
    if (room.me?.is_host && room.status !== "ended") {
      toast("你是房主，需要先结束房间才能离开。返回大厅会保留你的房主身份。", "error", 8000);
      setMobilePanel("desk");
      return;
    }
    const outstanding = state.pending.has(String(room.id));
    const approved = await confirmAction({
      title: "离开本场？",
      description: `${room.me?.character_id ? "你会让出当前角色。重新进来后，这一场只能再次选择原角色。" : "你将离开这间房。之后仍可以从大厅重新进入。"}${outstanding ? "\n\n还有一条发言尚未确认。原文会暂存，请返回后先核对记录或原样重试。" : ""}`,
      accept: "确认离场",
      cancel: "继续留场",
    });
    if (!approved || !sameId(state.room?.id, room.id)) return;
    const scope = state.scope;
    const navigation = state.navigation;
    state.busyRooms.add(String(room.id));
    $("leave-room").disabled = true;
    try {
      await api(`${roomPath(room.id)}/leave`, { method: "POST", body: {} });
      if (scope !== state.scope) return;
      // A slow leave acknowledgement must not navigate away from a newer room.
      if (navigation === state.navigation && sameId(state.room?.id, room.id)) await backToLobby();
      toast("已离开本场。");
    } catch (error) { if (scope === state.scope) { toast(readableError(error), "error"); scheduleSync(); } }
    finally {
      state.busyRooms.delete(String(room.id));
      $("leave-room").disabled = false;
      if (state.room) renderRoom();
    }
  }

  function applyRoom(room) {
    if (!room || !sameId(state.room?.id, room.id)) return false;
    if (Number.isFinite(Number(room.revision)) && Number.isFinite(Number(state.room.revision)) && Number(room.revision) < Number(state.room.revision)) return false;
    const previousRevision = state.room.revision;
    state.room = room;
    if (previousRevision !== room.revision && location.pathname.startsWith("/play/") && window.parent !== window) window.parent.postMessage({ type: "onstage:room-updated", room_id: String(room.id) }, location.origin);
    // Older transient snapshots used the same session scope but omitted its stable room member.
    const restored = state.pending.get(String(room.id));
    if (restored && restored.scope === state.scope && !restored.member_id && room.me?.member_id) {
      restored.member_id = room.me.member_id;
      persistPending();
    }
    if (!canReadBackstage()) {
      state.messages.backstage.clear();
      state.cursors.backstage = 0;
      state.unread.backstage = 0;
      if (state.channel === "backstage") switchChannel("stage");
    }
    return true;
  }

  function preserveFocus(container, callback) {
    const focused = document.activeElement;
    const key = container.contains(focused) ? focused.dataset.focusKey : null;
    callback();
    if (key) {
      const replacement = Array.from(container.querySelectorAll("[data-focus-key]")).find((item) => item.dataset.focusKey === key);
      if (replacement && !replacement.disabled) replacement.focus({ preventScroll: true });
    }
  }

  function renderRoom() {
    const room = state.room;
    if (!room) return;
    const item = scenario(room.scenario_id);
    renderBackdrop(item);
    $("room-title").textContent = room.title;
    $("room-status").textContent = PHASES[room.status] || "连接中";
    $("room-status").dataset.phase = room.status;
    $("room-subtitle").textContent = `${item?.title || "自由场景"} · ${PREVIEW ? "本机单人预览" : `${actors().length} 位出演`}`;
    $("leave-room").disabled = isRoomBusy();
    $("leave-room").textContent = room.status === "ended" ? "返回大厅" : "离开房间";
    $("leave-room").title = room.status === "ended" ? "保留当前资格，稍后回看" : "离开房间并释放角色";
    $("scene-title").textContent = item?.title || "自由场景";
    $("scene-category").textContent = item?.category || "场景背景";
    $("scene-summary").textContent = item?.summary || "";
    if ($("scene-art").dataset.sceneId !== String(room.scenario_id)) {
      $("scene-art").dataset.sceneId = String(room.scenario_id);
      $("scene-art").replaceChildren(sceneVisual(item));
    }
    $("room-background").textContent = room.background || (PREVIEW ? "这里还没有补充背景，可以从右上角编辑。" : "这里还没有补充背景。你们的对话，就是故事的开始。");
    const announcement = room.announcement || (PREVIEW ? "试着写下一段公告，看看置顶的样子。" : "还没有公告，先和同伴打个招呼吧。");
    $("announcement-preview").textContent = announcement.replace(/\s+/g, " ");
    $("announcement-body").textContent = announcement;
    const mayEdit = Boolean(room.me?.is_host && room.status !== "ended");
    $("room-settings").hidden = !mayEdit;
    $("announcement-edit").hidden = !mayEdit;
    $("room-settings").disabled = isRoomBusy();
    $("announcement-edit").disabled = isRoomBusy();
    $("room-reports-label").textContent = PREVIEW ? "举报功能说明" : room.me?.can_manage_reports ? "反馈处理" : "我的反馈";
    renderSeats();
    renderHouseDesk();
    renderMembers();
    renderChannels();
    renderMessages();
    renderPending();
    renderComposer();
    updateConnection();
  }

  function claimDisabledReason(characterId) {
    const me = state.room?.me;
    if (!me) return "先进入房间";
    if (state.room.status === "ended") return "房间已结束";
    const occupied = members().find((person) => sameId(person.character_id, characterId));
    if (occupied) return sameId(occupied.id, me.member_id) ? "你正在出演" : "这位角色已有人出演";
    if (me.character_id) return "你已选择了角色";
    if (me.first_character_id && !sameId(me.first_character_id, characterId)) return "这一场只能回到原角色";
    if (!me.can_claim) return "房主暂时关闭了补位";
    return "";
  }

  function renderSeats() {
    const room = state.room;
    const ids = scenario(room.scenario_id)?.character_ids || [];
    const me = room.me;
    $("seat-count").textContent = `${actors().length} / ${ids.length} 在场`;
    $("seat-guidance").textContent = room.status === "ended" ? "这一场已结束，角色与对话仍在这里。" : me?.character_id ? "你已入场。暂时断线不会让出角色。" : me?.first_character_id ? `这一场，你可以回到 ${character(me.first_character_id)?.name || "原角色"}。` : me?.can_claim ? "点选空角色，直接加入。不着急，也可以先旁观。" : "暂未开放补位，可以先在舞台旁观。";
    if (PREVIEW && room.status !== "ended") $("seat-guidance").textContent = me?.character_id ? room.status === "preparing" ? "角色已选好。到房间面板点击“预览演出”。" : "本场角色已保留，想体验其他人物可以新建场景。" : "选一个喜欢的角色，体验本机单人舞台。";
    preserveFocus($("seat-list"), () => {
      const fragment = document.createDocumentFragment();
      for (const id of ids) {
        const char = character(id);
        if (!char) continue;
        const occupied = members().find((person) => sameId(person.character_id, id));
        const own = occupied && sameId(occupied.id, me?.member_id);
        const reason = claimDisabledReason(id);
        const card = node("article", `seat-card${own ? " mine" : ""}`); card.dataset.tone = char.band_id;
        const main = actionButton("", "seat-main", () => claimCharacter(id), Boolean(reason) || isRoomBusy(), `claim-${id}`);
        main.setAttribute("aria-label", `${char.name}，${reason || "空角色，点击直接入场"}`);
        const label = occupied ? own ? "正在出演 · 我" : occupied.online ? "有人出演 · 在线" : "有人出演 · 暂离" : room.status === "ended" ? "未出演" : me?.first_character_id && !sameId(me.first_character_id, id) ? "本场不可换角" : me?.character_id ? "空角色" : me?.can_claim ? "空角色 · 加入" : "暂未开放";
        main.append(avatar(id), node("span", "seat-name", char.name), node("span", `seat-state${!reason ? " available" : ""}`, label));
        const info = actionButton("", "icon-button seat-info", () => showCharacter(id), false, `character-info-${id}`);
        info.append(icon("info")); info.setAttribute("aria-label", `了解 ${char.name}`);
        card.append(main, info); fragment.append(card);
      }
      $("seat-list").replaceChildren(fragment);
    });
  }

  async function claimCharacter(id) {
    const reason = claimDisabledReason(id);
    if (reason) { toast(reason, "info"); return; }
    const joined = await mutateRoom("/characters/claim", { character_id: id }, `你已作为 ${character(id)?.name || "角色"} 入场。`);
    if (joined && state.room) setMobilePanel("stage");
  }

  async function showCharacter(id) {
    const char = character(id);
    if (!char) return;
    const extra = node("div", "character-detail full-character-detail");
    const art = node("div", "character-full-art");
    const img = document.createElement("img"); img.src = characterAsset(id, true); img.alt = `${char.name}角色图`; img.loading = "lazy";
    art.append(img);
    const copy = node("div", "character-detail-copy");
    copy.append(node("strong", "", char.name), node("p", "muted", `${band(char.band_id)?.name || char.band_id} · ${char.instrument || ""}`), node("p", "muted", char.description || ""));
    if (char.source_url) {
      try { const url = new URL(char.source_url); if (["https:", "http:"].includes(url.protocol)) { const link = node("a", "", "角色资料 ↗"); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; copy.append(link); } } catch {}
    }
    extra.append(art, copy);
    await confirmAction({ title: "认识这位角色", description: "", accept: "知道了", cancel: "关闭", extra });
  }

  function renderHouseDesk() {
    const room = state.room;
    const me = room.me;
    const count = actors().length;
    const descriptions = { preparing: "同伴们还在陆续到场。两位角色就位，就可以让舞台亮起来。", live: room.allow_join ? "这场对话正在继续，空角色也欢迎新的同伴。" : "对话正在继续。房主暂时关闭了新角色补位。", paused: "让舞台歇一会儿。后台依然可以讨论，准备好后再继续。", ended: "这一场告一段落了。保留这份回响，下次再遇见。" };
    if (PREVIEW) Object.assign(descriptions, { preparing: "选好一个角色，就可以预览舞台画面。这里只记录你的本机操作。", live: "本机演出预览中。试试文字、贴纸，或切换到后台。", paused: "舞台已暂停，仍可体验后台。准备好后再继续。", ended: "本机预览已结束，角色和记录保存在这个浏览器。" });
    $("room-phase-description").textContent = descriptions[room.status] || "正在同步房间状态。";
    const copy = node("div");
    const role = me?.character_id ? character(me.character_id)?.name || "角色" : "旁观中";
    copy.append(node("strong", "", `${me?.nickname || state.nickname || "我"}${me?.is_host ? " · 房主" : ""} · ${role}`), node("p", "", me?.character_id ? "在这一幕里，写下你的回应。" : PREVIEW ? "选一个角色，试着写下第一句。" : "先听听大家，也很好。"));
    $("my-identity").replaceChildren(avatar(me?.character_id), copy);
    preserveFocus($("host-controls"), () => {
      const fragment = document.createDocumentFragment();
      if (me?.is_host) {
        if (room.status === "preparing") {
          fragment.append(actionButton(PREVIEW ? "预览演出" : "开启舞台", "button primary", () => roomControl("start"), isRoomBusy() || count < (PREVIEW ? 1 : 2), "control-start"));
          fragment.append(node("p", "control-note", PREVIEW ? count ? "单人画面预览，内容仅保存在本机。" : "先在角色面板选一个角色。" : count < 2 ? `当前 ${count} 位角色就位，还差 ${2 - count} 位。` : "同伴已就位，随时可以开场。"));
        }
        if (room.status === "live") fragment.append(actionButton("暂停舞台", "button secondary", () => roomControl("pause"), isRoomBusy(), "control-pause"));
        if (room.status === "paused") fragment.append(actionButton("继续舞台", "button primary", () => roomControl("resume"), isRoomBusy(), "control-resume"));
        if (["live", "paused"].includes(room.status)) fragment.append(actionButton(room.allow_join ? "关闭补位" : "开放补位", "button secondary", () => roomControl(room.allow_join ? "close_join" : "open_join"), isRoomBusy(), "control-join"));
        if (room.status !== "ended") fragment.append(actionButton("结束这一场", "button quiet", () => roomControl("end"), isRoomBusy(), "control-end"));
        else fragment.append(actionButton("去开一间新房", "button secondary", () => showLobbySection("scene-section"), false, "control-new"));
      } else if (!me?.character_id && room.status !== "ended") fragment.append(actionButton("去看看空角色", "button secondary", () => setMobilePanel("seats", true), false, "show-cast"));
      $("host-controls").replaceChildren(fragment);
    });
  }

  function renderMembers() {
    const all = members();
    const online = Number(state.room.online_count) || 0;
    $("member-count").textContent = PREVIEW ? "仅你在本机" : `${online} 在线 / ${all.length} 在场`;
    const manage = Boolean(state.room.me?.is_host && !PREVIEW);
    setButtonText($("audience-label"), `${manage ? "管理成员" : "成员"} · ${all.length}`, "people");
    $("audience-label").setAttribute("aria-label", PREVIEW ? "查看成员，只有你的本机身份" : `${manage ? "管理" : "查看"}本房成员，${all.length} 人在场，${online} 人在线`);
    $("members-heading").textContent = manage ? "成员管理" : "同场的人";
    preserveFocus($("member-list"), () => {
      const fragment = document.createDocumentFragment();
      for (const person of all) {
        const own = sameId(person.id, state.room.me?.member_id);
        const row = node("div", "member-row");
        const picture = node("div", "member-avatar-wrap");
        picture.append(avatar(person.character_id));
        if (!PREVIEW) picture.append(node("span", `presence-dot${person.online ? " online" : ""}`));
        const copy = node("div", "member-copy");
        const nickname = person.character_id && person.nickname ? `${person.nickname} · ` : "";
        copy.append(node("span", "member-name", `${participantName(person)}${own ? "（我）" : ""}`), node("span", "member-role", PREVIEW ? `${nickname}${person.character_id ? "出演中" : "旁观中"} · 本机` : `${nickname}${person.character_id ? "出演中" : "旁观中"} · ${person.online ? "在线" : "暂时离线"}`));
        row.append(picture, copy);
        if (person.is_host) row.append(node("span", "member-label", "房主"));
        if (state.room.me?.is_host && state.room.status !== "ended" && !own && !person.is_host) {
          const remove = actionButton("移出", "button quiet member-remove", () => removeMember(person.id), isRoomBusy(), `remove-${person.id}`);
          remove.setAttribute("aria-label", `移出 ${participantName(person)}`); remove.title = `移出 ${participantName(person)}`; row.append(remove);
        }
        fragment.append(row);
      }
      $("member-list").replaceChildren(fragment);
    });
  }

  function showMembers() {
    if (!state.room?.me) return;
    setMobilePanel("desk");
    $("members-heading").focus({ preventScroll: true });
    $("members-section").scrollIntoView({ block: "start", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }

  function openSettings() {
    if (!state.room?.me?.is_host || state.room.status === "ended" || state.settingsBusy) return;
    state.settingsRoomId = currentRoomId();
    $("settings-background").value = state.room.background || "";
    $("settings-announcement").value = state.room.announcement || "";
    $("settings-error").hidden = true;
    $("settings-dialog").showModal();
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (state.settingsBusy || !state.settingsRoomId || currentRoomId() !== state.settingsRoomId) return;
    const roomId = state.settingsRoomId;
    const scope = state.scope;
    state.settingsBusy = true;
    $("settings-submit").disabled = true;
    $("settings-submit").textContent = "正在保存…";
    $("settings-error").hidden = true;
    try {
      const result = await api(`${roomPath(roomId)}/settings`, { method: "POST", body: { background: $("settings-background").value, announcement: $("settings-announcement").value } });
      if (scope !== state.scope) return;
      if (!result.room) throw new ApiError("保存结果还没有确认，请重新同步房间后查看。");
      if (currentRoomId() === roomId) { applyRoom(result.room); renderRoom(); scheduleSync(0); }
      $("settings-dialog").close();
      toast("背景和公告已更新。");
    } catch (error) {
      if (scope !== state.scope) return;
      $("settings-error").textContent = readableError(error); $("settings-error").hidden = false;
    } finally {
      state.settingsBusy = false;
      $("settings-submit").disabled = false;
      $("settings-submit").textContent = "保存修改";
    }
  }

  async function removeMember(memberId) {
    const roomId = currentRoomId();
    const person = members().find((item) => sameId(item.id, memberId));
    if (!person || !state.room?.me?.is_host || state.room.status === "ended" || person.is_host || sameId(memberId, state.room.me.member_id)) return;
    const name = `${participantName(person)}${person.character_id && person.nickname ? `（${person.nickname}）` : ""}`;
    const confirmed = await confirmAction({
      title: `将 ${name} 移出房间？`,
      description: `你将把 ${name} 移出“${state.room.title}”。${person.character_id ? "对方正在出演的角色会让出。" : ""}\n\n对方的本房连接会断开，当前身份不能再次加入这间房；已经发送的消息会保留。`,
      accept: "确认移出", cancel: "留在房间", danger: true,
    });
    if (!confirmed || currentRoomId() !== roomId || state.room.status === "ended" || !state.room.me?.is_host) return;
    await mutateRoom(`/members/${encodeURIComponent(String(memberId))}/remove`, {}, `已将 ${name} 移出房间。`);
  }

  const REPORT_REASONS = { harassment: "骚扰或攻击", spam: "广告或刷屏", unsafe: "不安全或令人不适的内容", other: "其他问题" };
  const REPORT_STATUSES = { open: "待处理", pending: "待处理", resolved: "已处理", dismissed: "无需进一步处理" };

  function reportReceipt(report) {
    return String(report.id || "").slice(-8).toUpperCase();
  }

  function openReport(message = null) {
    if (!state.room || state.reportBusy) return;
    state.reportTarget = { room_id: currentRoomId(), message_id: message?.id ?? null };
    $("report-title").textContent = message ? "举报这条消息" : "举报这间房";
    if (message?.content_type === "sticker") {
      $("report-target").replaceChildren(node("p", "", `${messageAuthor(message)} · ${CHANNELS[message.channel]}`), stickerArt(stickerSnapshot(message), "report-sticker", false));
    } else $("report-target").textContent = message ? `${messageAuthor(message)} · ${CHANNELS[message.channel]}\n${String(message.text).slice(0, 240)}${String(message.text).length > 240 ? "…" : ""}` : state.room.title;
    $("report-reason").value = "harassment";
    $("report-details").value = "";
    $("report-error").hidden = !PREVIEW;
    if (PREVIEW) $("report-error").textContent = "本机预览只展示举报表单，没有提交给维护者，也不会生成回执。";
    $("report-submit").disabled = PREVIEW;
    $("report-submit").textContent = PREVIEW ? "预览版不提交" : "提交举报";
    $("report-dialog").showModal();
  }

  async function submitReport(event) {
    event.preventDefault();
    if (PREVIEW) { toast("本机预览没有提交举报，也不会生成处理回执。", "info"); return; }
    if (!state.reportTarget || state.reportBusy || !state.authenticated) return;
    const target = { ...state.reportTarget };
    const scope = state.scope;
    state.reportBusy = true;
    $("report-submit").disabled = true;
    $("report-submit").textContent = "正在提交…";
    $("report-error").hidden = true;
    try {
      const result = await api(`${roomPath(target.room_id)}/reports`, { method: "POST", body: { message_id: target.message_id, reason: $("report-reason").value, details: $("report-details").value.trim() } });
      if (scope !== state.scope) return;
      if (!result.report?.id) throw new ApiError("还没收到提交回执。请先在“我的反馈”查看记录，避免重复提交。");
      $("report-dialog").close();
      toast(`举报已提交 · 回执 ${reportReceipt(result.report)}。可以在“我的反馈”查看进度。`, "info", 10000);
      await openReports(null);
    } catch (error) {
      if (scope !== state.scope) return;
      $("report-error").textContent = `${readableError(error)}${!error.status || error.status >= 500 ? " 请先查看“我的反馈”，再决定是否重新提交。" : ""}`;
      $("report-error").hidden = false;
    } finally {
      state.reportBusy = false;
      $("report-submit").disabled = false;
      $("report-submit").textContent = "提交举报";
    }
  }

  async function openReports(roomId = null) {
    if (!state.authenticated) return;
    state.reportsScope = roomId ? String(roomId) : "";
    state.reportsGeneration += 1;
    state.reportList = [];
    state.reportCursor = 0;
    state.reportHasMore = false;
    state.reportLoading = false;
    const manage = roomId && sameId(state.room?.id, roomId) && state.room.me?.can_manage_reports;
    $("reports-title").textContent = manage ? "房间反馈处理" : "我的反馈";
    $("reports-description").textContent = manage ? "这里显示你有权限查看和处理的反馈。" : "每一条反馈都有回执，处理进度会留在这里。";
    $("reports-error").hidden = true;
    $("more-reports").hidden = true;
    $("refresh-reports").hidden = PREVIEW;
    if (PREVIEW) {
      $("reports-title").textContent = "举报功能说明";
      $("reports-description").textContent = "本机预览未连接维护者。";
      $("reports-list").setAttribute("aria-busy", "false");
      $("reports-list").replaceChildren(node("p", "empty-state", "这里可以查看举报表单的样子；不会实际提交，也不会产生举报记录或处理回执。"));
      if (!$("reports-dialog").open) $("reports-dialog").showModal();
      return;
    }
    renderLoading($("reports-list"), "正在读取反馈记录…");
    if (!$("reports-dialog").open) $("reports-dialog").showModal();
    await fetchReports();
  }

  async function fetchReports() {
    if (!state.authenticated || state.reportLoading) return;
    const generation = state.reportsGeneration;
    const scope = state.scope;
    const roomId = state.reportsScope;
    const before = state.reportCursor;
    state.reportLoading = true;
    $("refresh-reports").disabled = true;
    $("more-reports").disabled = true;
    $("reports-error").hidden = true;
    try {
      const params = new URLSearchParams({ after: String(before), limit: "50" });
      const result = await api(`${roomId ? `${roomPath(roomId)}/reports` : "/api/reports"}?${params}`);
      if (scope !== state.scope || generation !== state.reportsGeneration) return;
      const cursor = Number(result.cursor);
      if (!Array.isArray(result.reports) || !Number.isFinite(cursor) || cursor < before || (result.has_more && cursor <= before)) throw new ApiError("反馈记录暂未完整载入，请刷新重试。");
      const map = new Map(state.reportList.map((report) => [String(report.id), report]));
      for (const report of result.reports) map.set(String(report.id), report);
      state.reportList = Array.from(map.values());
      state.reportCursor = cursor;
      state.reportHasMore = Boolean(result.has_more);
      renderReports();
    } catch (error) {
      if (scope !== state.scope || generation !== state.reportsGeneration) return;
      $("reports-error").textContent = readableError(error); $("reports-error").hidden = false;
      if (!state.reportList.length) $("reports-list").replaceChildren();
    } finally {
      if (generation === state.reportsGeneration) {
        state.reportLoading = false;
        $("refresh-reports").disabled = false;
        $("more-reports").disabled = false;
        $("more-reports").hidden = !state.reportHasMore;
        $("reports-list").setAttribute("aria-busy", "false");
      }
    }
  }

  function renderReports() {
    preserveFocus($("reports-list"), () => {
      const fragment = document.createDocumentFragment();
      if (!state.reportList.length) {
        const empty = node("div", "empty-state");
        const symbol = node("span", "empty-symbol"); symbol.append(icon("inbox"));
        empty.append(symbol, node("h3", "", "这里暂时没有反馈"), node("p", "", "提交的反馈和处理说明会保留在这里。")); fragment.append(empty);
      }
      for (const report of state.reportList) {
        const card = node("article", "report-card");
        const top = node("div", "report-card-head");
        const pending = !["resolved", "dismissed"].includes(report.status);
        const label = pending && report.against_host && report.is_own ? "交维护者处理" : REPORT_STATUSES[report.status] || "已记录";
        const status = node("span", "report-status", label); status.dataset.status = report.status;
        top.append(node("strong", "", `${report.target_type === "message" ? "消息反馈" : "房间反馈"} · ${reportReceipt(report)}`), status);
        card.append(top, node("p", "report-meta", `${timeLabel(report.created_at, true)}${report.is_own ? " · 我提交的" : ""}`), node("p", "", REPORT_REASONS[report.reason] || "内容反馈"));
        if (report.details) card.append(node("p", "muted", report.details));
        if (!report.snapshot_redacted && report.snapshot) {
          if (report.snapshot.message) {
            const message = report.snapshot.message;
            const author = message.author && !/^旅人[·.][0-9a-f]+$/i.test(message.author) ? message.author : "本房成员";
            if (message.content_type === "sticker") card.append(node("p", "report-snapshot", `${author} · 贴纸`), stickerArt(stickerSnapshot(message), "report-sticker", false));
            else card.append(node("p", "report-snapshot", `${author}：${message.text || ""}`));
          } else if (report.snapshot.room) {
            const room = report.snapshot.room;
            card.append(node("p", "report-snapshot", `${room.title || "房间"}${room.announcement ? `\n公告：${room.announcement}` : ""}${room.background ? `\n背景：${room.background}` : ""}`));
          }
        }
        if (report.resolution_note) card.append(node("p", "report-resolution", `处理说明：${report.resolution_note}`));
        else if (report.against_host && report.is_own && pending) card.append(node("p", "report-resolution", "这条反馈需要由维护者处理，房主不能处理针对自己的举报。"));
        if (report.can_resolve) card.append(actionButton("处理反馈", "button secondary small", () => openResolve(report), false, `resolve-${report.id}`));
        fragment.append(card);
      }
      $("reports-list").replaceChildren(fragment);
    });
  }

  function openResolve(report) {
    if (!report.can_resolve || state.resolveBusy) return;
    state.resolveTarget = { ...report };
    $("resolve-status").value = "resolved";
    $("resolve-note").value = "";
    $("resolve-error").hidden = true;
    $("resolve-dialog").showModal();
  }

  async function resolveReport(event) {
    event.preventDefault();
    const target = state.resolveTarget;
    const note = $("resolve-note").value.trim();
    if (!target?.can_resolve || state.resolveBusy || !note) return;
    const scope = state.scope;
    state.resolveBusy = true;
    $("resolve-submit").disabled = true;
    $("resolve-submit").textContent = "正在保存…";
    $("resolve-error").hidden = true;
    try {
      const result = await api(`${roomPath(target.room_id)}/reports/${encodeURIComponent(String(target.id))}/resolve`, { method: "POST", body: { status: $("resolve-status").value, note } });
      if (scope !== state.scope) return;
      if (!result.report?.id) throw new ApiError("处理结果尚未确认，请刷新反馈记录后核对。");
      state.reportList = state.reportList.map((report) => sameId(report.id, result.report.id) ? result.report : report);
      renderReports();
      $("resolve-dialog").close();
      toast("处理说明已保存，反馈者可以查看结果。");
    } catch (error) {
      if (scope !== state.scope) return;
      $("resolve-error").textContent = readableError(error); $("resolve-error").hidden = false;
    } finally {
      state.resolveBusy = false;
      $("resolve-submit").disabled = false;
      $("resolve-submit").textContent = "确认处理";
    }
  }

  async function mutateRoom(suffix, body, successText) {
    const id = currentRoomId();
    if (!id || state.busyRooms.has(id)) return false;
    const scope = state.scope;
    state.busyRooms.add(id);
    renderRoom();
    try {
      const result = await api(`${roomPath(id)}${suffix}`, { method: "POST", body });
      if (scope !== state.scope) return false;
      if (currentRoomId() === id) {
        if (result.room) applyRoom(result.room);
        scheduleSync(0);
        renderRoom();
      }
      if (successText) toast(successText);
      return true;
    } catch (error) {
      toast(readableError(error), "error", 8000);
      if (currentRoomId() === id) scheduleSync(0);
      return false;
    } finally {
      state.busyRooms.delete(id);
      if (currentRoomId() === id) renderRoom();
    }
  }



  async function roomControl(action) {
    const id = currentRoomId();
    if (!id || !state.room.me?.is_host || isRoomBusy()) return;
    if (action === "end") {
      const confirmed = await confirmAction({ title: "让这一场落幕？", description: PREVIEW ? "结束本机预览后，舞台和后台停止添加内容，角色与记录仍可回看。想再试一场，可以新建场景。" : "结束后，舞台和后台停止接收新发言，记录仍可按原有权限阅读。本场不能重新开场；下一幕请新建场次。", accept: "确认结束本场", cancel: "继续这一场", danger: true });
      if (!confirmed || currentRoomId() !== id) return;
    }
    const messages = { start: "舞台已开启。", pause: "舞台已暂停，后台仍可沟通。", resume: "舞台已继续。", end: "本场已结束，记录已保留。", open_join: "已开放空角色，新同伴可以直接加入。", close_join: "已关闭角色补位，同伴仍可旁观。" };
    if (PREVIEW) Object.assign(messages, { start: "本机演出预览已开启。", pause: "本机舞台已暂停，仍可体验后台。", resume: "本机演出预览已继续。", end: "本机预览已结束，记录已保留。", open_join: "已切换为开放补位的预览状态。", close_join: "已切换为关闭补位的预览状态。" });
    await mutateRoom("/control", { action }, messages[action]);
  }

  function renderChannels() {
    const access = canReadBackstage();
    $("backstage-channel").disabled = !access;
    $("backstage-channel").title = access ? "演员与房主的交流空间" : "选择角色后即可进入后台";
    for (const channel of ["stage", "backstage"]) {
      const active = state.channel === channel;
      $(channel + "-channel").setAttribute("aria-selected", String(active));
      $(channel + "-channel").tabIndex = active ? 0 : -1;
      const count = state.unread[channel];
      $(channel + "-unread").hidden = !count;
      $(channel + "-unread").textContent = count > 99 ? "99+" : String(count);
    }
    const unread = state.unread.stage + state.unread.backstage;
    $("mobile-chat-unread").hidden = !unread || state.mobilePanel === "stage";
    $("mobile-chat-unread").textContent = unread > 99 ? "99+" : String(unread);
    const ended = state.room.status === "ended";
    $("channel-note").textContent = state.channel === "stage"
      ? ended ? "这一场已结束 · 可以继续回看" : "角色发言 · 本房同伴都能看到"
      : ended ? "后台记录 · 仍仅房主与留场演员可见" : "演员交流 · 仅房主与现任演员可见";
    if (PREVIEW) $("channel-note").textContent = ended ? "本机留档 · 可以继续回看" : state.channel === "stage" ? "舞台画面预览 · 仅记录你的操作" : "后台画面预览 · 内容保存在本机";
    $("stage-heading").textContent = `${CHANNELS[state.channel]}发言`;
    $("message-feed").setAttribute("aria-label", `${CHANNELS[state.channel]}发言`);
    $("message-feed").setAttribute("aria-labelledby", state.channel + "-channel");
    $("message-input-label").textContent = `输入${CHANNELS[state.channel]}发言`;
    $("message-form").dataset.channel = state.channel;
  }

  function stickerBand(item) {
    return item?.band_id || character(item?.character_id)?.band_id || "supplementary";
  }

  function selectedStickerId() {
    return state.room ? state.stickerDrafts.get(`${state.room.id}:${state.channel}`) || "" : "";
  }

  function openStickerPanel() {
    if (!state.room?.me || (state.channel === "backstage" && !canReadBackstage())) return;
    state.stickerSearch = "";
    $("sticker-search").value = "";
    renderStickerFilters();
    renderStickerList();
    renderStickerPreview();
    if (!$("sticker-dialog").open) $("sticker-dialog").showModal();
  }

  function renderStickerFilters() {
    const items = state.catalog.stickers || [];
    const sourceCount = items.filter((item) => item.source_pack === "user-v04").length;
    if (!sourceCount) state.stickerSource = "all";
    const sources = [...(sourceCount ? [{ id: "user-v04", name: `本次 ${sourceCount}` }] : []), { id: "all", name: `全部 ${items.length}` }];
    preserveFocus($("sticker-sources"), () => {
      const fragment = document.createDocumentFragment();
      for (const choice of sources) {
        const active = state.stickerSource === choice.id;
        const button = actionButton(choice.name, `filter-chip${active ? " selected" : ""}`, () => {
          state.stickerSource = choice.id;
          renderStickerFilters(); renderStickerList(); $("sticker-list").scrollTop = 0;
        }, false, `sticker-source-${choice.id}`);
        button.dataset.stickerSource = choice.id;
        button.setAttribute("aria-pressed", String(active)); fragment.append(button);
      }
      $("sticker-sources").replaceChildren(fragment);
    });
    const categories = Array.isArray(state.catalog.sticker_categories) ? state.catalog.sticker_categories : [...new Set(items.map((item) => item.source_category).filter(Boolean))].map((id) => ({ id, name: id }));
    if (state.stickerCategory !== "all" && !categories.some((item) => item.id === state.stickerCategory)) state.stickerCategory = "all";
    const options = document.createDocumentFragment();
    const allOption = node("option", "", "全部人物"); allOption.value = "all"; options.append(allOption);
    for (const category of categories) {
      const count = items.filter((item) => item.source_category === category.id && (state.stickerSource === "all" || item.source_pack === state.stickerSource)).length;
      const option = node("option", "", `${category.name} · ${count}`); option.value = category.id; options.append(option);
    }
    $("sticker-category").replaceChildren(options);
    $("sticker-category").value = state.stickerCategory;
    const choices = [{ id: "all", name: "全部乐队" }, ...state.catalog.bands.map((item) => ({ id: item.id, name: item.name })), ...(items.some((item) => stickerBand(item) === "supplementary") ? [{ id: "supplementary", name: "其他人物" }] : [])];
    if (!choices.some((item) => item.id === state.stickerFilter)) state.stickerFilter = "all";
    preserveFocus($("sticker-filters"), () => {
      const fragment = document.createDocumentFragment();
      for (const choice of choices) {
        const active = state.stickerFilter === choice.id;
        const button = actionButton(choice.name, `filter-chip${active ? " selected" : ""}`, () => {
          state.stickerFilter = choice.id;
          state.stickerCategory = "all";
          renderStickerFilters();
          renderStickerList();
          $("sticker-list").scrollTop = 0;
        }, false, `sticker-filter-${choice.id}`);
        button.dataset.stickerFilter = choice.id;
        button.setAttribute("aria-pressed", String(active));
        fragment.append(button);
      }
      $("sticker-filters").replaceChildren(fragment);
    });
  }

  function renderStickerList() {
    const query = state.stickerSearch.trim().toLocaleLowerCase("zh-CN");
    const selectedId = selectedStickerId();
    const filtered = (state.catalog.stickers || []).filter((item) => {
      if (state.stickerSource !== "all" && item.source_pack !== state.stickerSource) return false;
      if (state.stickerCategory !== "all" && item.source_category !== state.stickerCategory) return false;
      if (state.stickerFilter !== "all" && stickerBand(item) !== state.stickerFilter) return false;
      const words = [item.label, item.source_category, character(item.character_id)?.name, band(stickerBand(item))?.name, ...(Array.isArray(item.keywords) ? item.keywords : [])].join(" ").toLocaleLowerCase("zh-CN");
      return !query || words.includes(query);
    });
    preserveFocus($("sticker-list"), () => {
      const fragment = document.createDocumentFragment();
      for (const item of filtered) {
        const selected = sameId(item.id, selectedId);
        const button = actionButton("", "sticker-choice", () => chooseSticker(item.id), false, `sticker-choice-${item.id}`);
        button.dataset.stickerId = item.id;
        button.dataset.selected = String(selected);
        button.dataset.tone = stickerBand(item);
        button.setAttribute("aria-pressed", String(selected));
        button.setAttribute("aria-label", `${item.label} · ${character(item.character_id)?.name || item.source_category || "角色贴纸"}，选择后预览`);
        button.append(stickerArt(item, "sticker-thumb", false), node("span", "sticker-choice-label", item.label));
        fragment.append(button);
      }
      if (!filtered.length) {
        const empty = node("div", "sticker-empty");
        empty.append(icon("search"), node("p", "", state.catalog.stickers?.length ? "暂时没找到这个心情。换个词，或试试全部人物和素材。" : "贴纸还没准备好，稍后再来看看。"));
        fragment.append(empty);
      }
      $("sticker-list").replaceChildren(fragment);
    });
  }

  function chooseSticker(id) {
    if (!state.room || (id && !sticker(id))) return;
    const key = `${state.room.id}:${state.channel}`;
    if (id) state.stickerDrafts.set(key, String(id));
    else state.stickerDrafts.delete(key);
    persistStickerDrafts();
    renderStickerList();
    renderStickerPreview();
    $("sticker-selection-dot").hidden = !id;
  }

  function renderStickerPreview() {
    if (!state.room) return;
    const selectedId = selectedStickerId();
    const selected = sticker(selectedId);
    const preview = $("sticker-preview");
    const fragment = document.createDocumentFragment();
    if (selected) {
      const copy = node("div", "sticker-preview-copy");
      copy.append(node("strong", "", selected.label), node("p", "", `${character(selected.character_id)?.name || selected.source_category || "角色贴纸"} · ${band(stickerBand(selected))?.name || "其他人物"}`));
      fragment.append(stickerArt(selected, "sticker-preview-art", false), copy);
    } else {
      const empty = node("div", "sticker-preview-empty");
      empty.append(icon("sticker"), node("p", "", selectedId ? "这张贴纸暂不在当前贴纸包里，请重新选择。" : "点选一张贴纸，在这里看清楚再发送。"));
      fragment.append(empty);
    }
    preview.replaceChildren(fragment);
    const pending = pendingMessage();
    const reason = sendDisabledReason();
    const identity = state.room.me?.character_id ? character(state.room.me.character_id)?.name || "角色" : "房主";
    $("sticker-send-note").textContent = reason || (pending ? "还有一条内容待确认。请返回聊天核对或重试，文字草稿会保留。" : selected ? `以 ${identity} ${PREVIEW ? "保存到本机" : "发送到"}${CHANNELS[state.channel]}。文字草稿会保留。` : "选择后，点击发送才会出现在对话里。");
    $("send-sticker").disabled = Boolean(reason) || Boolean(pending) || !selected || !state.authenticated;
    setButtonText($("send-sticker"), `发送到${CHANNELS[state.channel]}`, "send");
    $("clear-sticker").disabled = !selectedId;
    $("sticker-selection-dot").hidden = !selectedId;
  }

  async function submitSticker() {
    if (!state.room || !state.authenticated || pendingMessage()) return;
    const reason = sendDisabledReason();
    if (reason) { toast(reason, "error"); renderStickerPreview(); return; }
    const selected = sticker(selectedStickerId());
    if (!selected) { toast("先选择一张贴纸。", "info"); return; }
    let id;
    try { id = freshMessageId(); }
    catch (error) { toast(readableError(error), "error"); return; }
    const snapshot = stickerSnapshot({ sticker_id: selected.id, sticker: selected });
    const pending = { scope: state.scope, member_id: state.room.me.member_id, character_id: state.room.me.character_id, author: state.room.me.nickname || state.nickname || "我", room_id: currentRoomId(), channel: state.channel, content_type: "sticker", sticker_id: selected.id, sticker: snapshot, text: `[贴纸] ${snapshot.label}`, client_message_id: id, status: "sending", detail: "" };
    saveDraft();
    state.pending.set(pending.room_id, pending);
    persistPending();
    $("sticker-dialog").close("sending");
    await postPending(pending);
  }

  function saveDraft() {
    if (state.room) {
      state.drafts.set(`${state.room.id}:${state.channel}`, $("message-input").value);
      persistDrafts();
    }
  }

  function switchChannel(channel) {
    if (!CHANNELS[channel] || !state.room || (channel === "backstage" && !canReadBackstage()) || channel === state.channel) return;
    saveDraft();
    state.channel = channel;
    state.unread[channel] = 0;
    state.messageSignature = "";
    $("message-input").value = state.drafts.get(`${state.room.id}:${channel}`) || "";
    $("scroll-bottom").hidden = true;
    renderChannels();
    renderMessages(true);
    renderPending();
    renderComposer();
  }

  function setMobilePanel(panel, focus = false) {
    if (!["seats", "stage", "desk"].includes(panel)) return;
    state.mobilePanel = panel;
    if (panel !== "stage") $("room-view").dataset.desktopPanel = panel;
    for (const name of ["seats", "stage", "desk"]) {
      const selected = name === panel;
      $(name + "-panel").dataset.active = String(selected);
      $("mobile-tab-" + name).setAttribute("aria-selected", String(selected));
      $("mobile-tab-" + name).tabIndex = selected ? 0 : -1;
    }
    if (panel === "stage" && state.room) {
      state.unread[state.channel] = 0;
      renderChannels();
      if (!state.messageSignature || window.matchMedia("(max-width: 720px)").matches) renderMessages(true);
      resizeComposer();
    }
    if (focus) {
      const tab = $("mobile-tab-" + panel);
      if (tab.getClientRects().length) tab.focus({ preventScroll: true });
      else {
        const target = $(panel + "-panel");
        target.setAttribute("tabindex", "-1");
        target.focus({ preventScroll: true });
      }
    }
  }

  // Both POST ACK and paginated sync use the same identity-bound receipt path.
  function reconcileMessage(message, roomId = currentRoomId(), scope = state.scope) {
    const pending = state.pending.get(roomId);
    if (!pending || scope !== state.scope || pending.scope !== scope || !pending.member_id ||
        !sameId(message?.member_id, pending.member_id) || message.channel !== pending.channel ||
        message.client_message_id !== pending.client_message_id || !message.id ||
        !Number.isFinite(Number(message.seq))) return false;
    state.pending.delete(roomId);
    persistPending();
    if (pending.content_type === "sticker") {
      const key = `${roomId}:${pending.channel}`;
      if (sameId(state.stickerDrafts.get(key), pending.sticker_id)) state.stickerDrafts.delete(key);
      persistStickerDrafts();
    }
    return true;
  }

  function insertMessage(message, countUnread = true) {
    if (!message || !CHANNELS[message.channel] || !Number.isFinite(Number(message.seq))) return false;
    if (message.channel === "backstage" && !canReadBackstage()) return false;
    reconcileMessage(message);
    const map = state.messages[message.channel];
    const key = String(message.id ?? `${message.channel}:${message.seq}`);
    if (map.has(key)) return false;
    map.set(key, message);
    if (countUnread && state.initialSynced && (message.channel !== state.channel || (window.matchMedia("(max-width: 720px)").matches && state.mobilePanel !== "stage"))) state.unread[message.channel] += 1;
    return true;
  }

  function renderMessages(forceBottom = false) {
    if (!state.room) return;
    const channel = state.channel;
    const list = Array.from(state.messages[channel].values()).sort((a, b) => Number(a.seq) - Number(b.seq));
    const pending = pendingMessage();
    const visiblePending = pending && pending.scope === state.scope && pending.channel === channel && sameId(pending.member_id, state.room.me?.member_id) ? pending : null;
    if (visiblePending) list.push({ ...visiblePending, id: `pending-${visiblePending.client_message_id}`, kind: "speech", local_pending: true });
    const signature = `${visiblePending ? `${visiblePending.client_message_id}:${visiblePending.status}:${visiblePending.detail || ""}` : ""}:${channel}:${list.length}:${list.map((message) => message.id ?? message.seq).join(",")}:${state.initialSynced}:${state.room.status}`;
    if (signature === state.messageSignature && !forceBottom) return;
    const feed = $("message-feed");
    const wasBottom = feed.scrollHeight - feed.clientHeight - feed.scrollTop < 85;
    const oldHeight = feed.scrollHeight;
    const oldTop = feed.scrollTop;
    const context = `${state.scope}:${state.room.id}:${channel}`;
    const changedChannel = feed.dataset.context !== context;
    if (changedChannel) { feed.replaceChildren(); feed.dataset.context = context; }
    const existing = new Map(Array.from(feed.children).map((item) => [item.dataset.messageId, item]));
    const alreadyVisible = new Set(Array.from(feed.children).map((item) => item.dataset.messageId));
    const feedTop = feed.getBoundingClientRect().top;
    const firstVisible = Array.from(feed.children).find((item) => item.dataset.messageId && item.getBoundingClientRect().bottom > feedTop);
    const firstVisibleId = firstVisible?.dataset.messageId;
    const anchorOffset = firstVisible ? firstVisible.getBoundingClientRect().top - feedTop : 0;
    const focusedId = document.activeElement?.dataset.messageAction;
    const rendered = [];
    if (!list.length) {
      const empty = node("div", "feed-empty");
      const symbol = node("span", "empty-symbol"); symbol.append(icon(channel === "backstage" ? "headphones" : "chat"));
      const title = !state.initialSynced ? "正在接上这一段对话…" : channel === "backstage" ? state.room.status === "ended" ? "后台静悄悄的" : "先在这里，碰一碰想法" : state.room.status === "preparing" ? PREVIEW ? "选好角色，预览这一场" : "等同伴就位，一起开场" : state.room.status === "ended" ? "这一场没有留下发言" : "把第一句，交给你";
      const detail = !state.initialSynced ? "稍等一下，正在读取记录。" : channel === "backstage" ? state.room.status === "ended" ? "新的一段相遇，可以从另一间房开始。" : "节奏、想法、小约定，都可以先在这里说。" : state.room.status === "preparing" ? PREVIEW ? "在角色面板选择一位，再到房间面板点击“预览演出”。" : "点选空角色直接入场。两位角色就位后，由房主开启舞台。" : state.room.status === "ended" ? "那些还没说出口的话，下次再遇见。" : PREVIEW ? "写一句话，或贴一张心情。这里只会出现你自己的记录。" : "一句话，一个动作。让同伴看见你的回应。";
      empty.append(symbol, node("h3", "", title), node("p", "", detail)); rendered.push(empty);
    }
    for (const message of list) {
      const key = String(message.id ?? message.seq);
      // Committed map entries are immutable. Do not stringify the entire history
      // on every notification; only the one mutable pending snapshot needs it.
      const renderKey = message.local_pending ? JSON.stringify(message) : message;
      const author = messageAuthor(message);
      const previous = existing.get(key);
      if (previous?.onstageRenderKey === renderKey && previous.onstageAuthor === author && previous.onstageOwner === state.room.me?.member_id) { rendered.push(previous); continue; }
      const item = node("article", message.kind === "system" ? "system-message" : "message");
      item.onstageRenderKey = renderKey;
      item.onstageAuthor = author;
      item.onstageOwner = state.room.me?.member_id;
      item.dataset.messageId = String(message.id ?? message.seq);
      if (state.initialSynced && !changedChannel && !alreadyVisible.has(item.dataset.messageId)) item.dataset.enter = "true";
      if (message.kind === "system") {
        const copy = node("div");
        const time = node("time", "", timeLabel(message.created_at)); time.dateTime = message.created_at || "";
        copy.append(node("span", "", message.text), time); item.append(copy);
      } else {
        const own = sameId(message.member_id, state.room.me?.member_id);
        item.dataset.tone = character(message.character_id)?.band_id === "mujica" ? "mujica" : "mygo";
        item.dataset.own = String(own);
        item.dataset.contentType = message.content_type === "sticker" ? "sticker" : "text";
        const meta = node("div", "message-meta");
        meta.append(node("span", "message-author", messageAuthor(message)));
        if (own) meta.append(node("span", "message-self", "我"));
        if (message.local_pending) {
          item.dataset.pendingId = message.client_message_id;
          item.dataset.status = message.status;
          const sending = message.status === "sending";
          const status = node("span", `message-delivery${sending ? " is-sending" : " is-uncertain"}`, sending ? "" : "状态未确认");
          status.setAttribute("role", "status");
          status.setAttribute("aria-label", sending ? "正在确认发送" : "状态未确认，可原样重试");
          meta.append(status);
          if (!sending) {
            const retry = actionButton("原样重试", "button message-retry", () => postPending(visiblePending));
            retry.dataset.messageAction = `retry-${message.client_message_id}`;
            const details = node("details", "message-delivery-detail");
            details.append(node("summary", "", "详情"), node("p", "", message.detail || "内容已保留，原样重试不会重复发送。"));
            const restore = actionButton(message.content_type === "sticker" ? "放回选择" : "放回草稿", "button message-retry", restorePendingDraft);
            details.append(restore);
            meta.append(retry, details);
          }
        } else {
          const time = node("time", "message-time", timeLabel(message.created_at)); time.dateTime = message.created_at || ""; time.title = timeLabel(message.created_at, true); meta.append(time);
        }
        const report = actionButton("", "icon-button message-report", () => openReport(message));
        report.append(icon("flag")); report.setAttribute("aria-label", `举报 ${messageAuthor(message)} 的这条发言`); report.title = "举报这条消息"; report.dataset.messageAction = String(message.id);
        if (!message.local_pending) meta.append(report);
        item.append(avatar(message.character_id), meta, message.content_type === "sticker" ? stickerArt(stickerSnapshot(message), "message-sticker", false) : node("p", "message-body", message.text));
      }
      rendered.push(item);
    }
    feed.setAttribute("aria-busy", String(!state.initialSynced));
    const retained = new Set(rendered);
    for (const item of Array.from(feed.children)) if (!retained.has(item)) item.remove();
    let position = feed.firstChild;
    for (const item of rendered) {
      if (item === position) position = position.nextSibling;
      else feed.insertBefore(item, position);
    }
    state.messageSignature = signature;
    $("jump-start").hidden = state.room.status !== "ended";
    if (state.followHistoryStart && state.room.status === "ended") { feed.scrollTop = 0; $("scroll-bottom").hidden = false; }
    else if (forceBottom || wasBottom || changedChannel) { feed.scrollTop = feed.scrollHeight; $("scroll-bottom").hidden = true; }
    else {
      const anchor = firstVisibleId ? Array.from(feed.children).find((item) => item.dataset.messageId === firstVisibleId) : null;
      if (anchor) feed.scrollTop += anchor.getBoundingClientRect().top - feed.getBoundingClientRect().top - anchorOffset;
      else feed.scrollTop = Math.min(oldTop, Math.max(0, feed.scrollHeight - feed.clientHeight));
      if (feed.scrollHeight > oldHeight) $("scroll-bottom").hidden = false;
    }
    if (focusedId) Array.from(feed.querySelectorAll("[data-message-action]")).find((button) => button.dataset.messageAction === focusedId)?.focus({ preventScroll: true });
  }

  function sendDisabledReason() {
    const room = state.room;
    const me = room?.me;
    if (!me) return "先进入房间，再开始对话。";
    if (room.status === "ended") return "这一场已结束，可以继续回看。";
    if (state.channel === "backstage") return me.can_backstage ? "" : "选好角色后，就能和演员们在后台交流。";
    if (me.can_stage) return "";
    if (room.status === "preparing") return PREVIEW ? "选好角色后，在房间面板点击“预览演出”。" : "舞台还没开启，演员与房主可以先在后台打个招呼。";
    if (room.status === "paused") return "舞台歇一会儿，房主继续后就能发言。";
    return "你正在旁观。选个空角色，就能参与对话。";
  }

  function renderComposer() {
    if (!state.room) return;
    const me = state.room.me;
    const reason = sendDisabledReason();
    const pending = pendingMessage();
    const identity = me?.character_id ? character(me.character_id)?.name || "角色" : `${me?.nickname || state.nickname || "我"}${me?.is_host ? " · 房主" : ""} · 旁观中`;
    $("composer-identity").textContent = `${identity} · ${CHANNELS[state.channel]}`;
    $("message-input").disabled = Boolean(reason);
    $("message-input").placeholder = reason || (state.channel === "backstage" ? "和同伴说说接下来的想法…" : "接住上一句，写下你的回应…");
    const length = Array.from($("message-input").value).length;
    $("message-counter").textContent = `${length} / 2000`;
    $("send-message").disabled = Boolean(reason) || Boolean(pending) || !$("message-input").value.trim() || length > 2000;
    setButtonText($("send-message"), "发送", "send");
    $("send-message").setAttribute("aria-label", pending?.status === "sending" ? "正在确认内容" : `发送${CHANNELS[state.channel]}发言`);
    $("composer-hint").textContent = reason || (pending ? "上方还有待确认的内容。草稿可以继续写。" : state.sendShortcut === "enter" ? "Enter 发送 · Shift + Enter 换行 · 可在使用设置中调整" : "Enter 换行 · Ctrl / ⌘ + Enter 发送 · 可在使用设置中调整");
    $("open-stickers").disabled = Boolean(reason) || !state.catalog.stickers?.length;
    $("open-stickers").title = reason || (!state.catalog.stickers?.length ? "贴纸还没准备好" : selectedStickerId() ? "继续查看已选贴纸" : "选择贴纸");
    $("sticker-selection-dot").hidden = !selectedStickerId();
    if ($("sticker-dialog").open) renderStickerPreview();
    resizeComposer();
  }

  function renderPending() {
    // Legacy banner remains hidden; feedback belongs to the exact message in the feed.
    $("pending-message").hidden = true;
    renderMessages();
  }

  async function submitMessage(event) {
    event.preventDefault();
    if (!state.room || pendingMessage()) return;
    const reason = sendDisabledReason();
    if (reason) { toast(reason, "error"); return; }
    const text = $("message-input").value;
    if (!text.trim()) return;
    if (Array.from(text).length > 2000) { toast("单条发言最多 2000 字。", "error"); return; }
    let id;
    try { id = freshMessageId(); }
    catch (error) { toast(readableError(error), "error"); return; }
    const pending = { scope: state.scope, member_id: state.room.me.member_id, character_id: state.room.me.character_id, author: state.room.me.nickname || state.nickname || "我", room_id: currentRoomId(), channel: state.channel, content_type: "text", text, client_message_id: id, status: "sending", detail: "" };
    state.pending.set(pending.room_id, pending);
    // Snapshot is held by pending; the composer is now a separate new draft.
    state.drafts.set(`${pending.room_id}:${pending.channel}`, "");
    $("message-input").value = "";
    persistDrafts();
    persistPending();
    await postPending(pending);
  }

  async function postPending(pending) {
    if (!state.authenticated || state.posting.has(pending.client_message_id)) return;
    const scope = state.scope;
    if (pending.scope !== scope || !sameId(pending.member_id, state.room?.me?.member_id) || currentRoomId() !== pending.room_id) return;
    const isSticker = pending.content_type === "sticker";
    pending.status = "sending";
    pending.detail = "";
    state.posting.add(pending.client_message_id);
    persistPending();
    if (currentRoomId() === pending.room_id) { renderPending(); renderComposer(); }
    try {
      const result = await api(`${roomPath(pending.room_id)}/messages`, {
        method: "POST",
        allow401: true,
        body: isSticker
          ? { channel: pending.channel, content_type: "sticker", sticker_id: pending.sticker_id, client_message_id: pending.client_message_id }
          : { channel: pending.channel, text: pending.text, client_message_id: pending.client_message_id },
      });
      if (scope !== state.scope) return;
      const message = result.message;
      const matches = message && message.client_message_id === pending.client_message_id &&
        sameId(message.member_id, pending.member_id) && message.channel === pending.channel &&
        message.id && Number.isFinite(Number(message.seq));
      if (!matches) throw new ApiError("发送结果还不完整。请原样重试确认。");
      reconcileMessage(message, pending.room_id, scope);
      if (currentRoomId() === pending.room_id) {
        // A POST receipt never advances the paginated sync cursors.
        insertMessage(message, false);
        renderMessages(state.channel === pending.channel);
        scheduleSync(0);
      }
    } catch (error) {
      if (scope !== state.scope || state.pending.get(pending.room_id) !== pending) return;
      // Even a later 4xx cannot prove a prior timed-out attempt was not committed.
      pending.status = "uncertain";
      pending.detail = `${readableError(error)}${pending.status === "uncertain" ? " 内容已保留，原样重试不会重复发送。" : isSticker ? " 请核对房间状态，再重试或放回选择。" : " 请核对房间状态，再重试或放回草稿。"}`;
      persistPending();
      if (currentRoomId() === pending.room_id) scheduleSync(0);
    } finally {
      state.posting.delete(pending.client_message_id);
      if (currentRoomId() === pending.room_id) { renderPending(); renderComposer(); }
    }
  }

  async function restorePendingDraft() {
    const pending = pendingMessage();
    if (!pending || state.posting.has(pending.client_message_id)) return;
    const roomId = pending.room_id;
    const isSticker = pending.content_type === "sticker";
    const key = `${roomId}:${pending.channel}`;
    const anotherChoice = isSticker && state.stickerDrafts.has(key) && !sameId(state.stickerDrafts.get(key), pending.sticker_id);
    const confirmed = await confirmAction({
      title: isSticker ? "把待确认贴纸放回选择？" : "把待确认发言放回草稿？",
      description: PREVIEW ? `这条内容可能已经保存在本机。建议先查看记录，或者“原样重试”来确认。\n\n放回${isSticker ? "选择" : "草稿"}后再次发送，可能留下重复记录。当前文字草稿会保留。` : isSticker ? `这张贴纸可能已经发出。建议先查看记录，或者“原样重试”来确认。\n\n放回选择后再次发送，可能出现重复贴纸。文字草稿会保留。${anotherChoice ? "本频道已有另一张待发贴纸，这次会改选为待确认的这一张。" : ""}` : "这句话可能已经发出。建议先查看记录，或者“原样重试”来确认。\n\n放回草稿后再次发送，可能会出现重复消息。现有草稿会保留在原文下方。",
      accept: isSticker ? "我已核对，放回选择" : "我已核对，放回草稿",
      cancel: "继续核对",
    });
    if (!confirmed || currentRoomId() !== roomId || pendingMessage()?.client_message_id !== pending.client_message_id) return;
    saveDraft();
    if (isSticker) {
      state.stickerDrafts.set(key, pending.sticker_id);
      persistStickerDrafts();
    } else {
      const existing = state.drafts.get(key) || "";
      state.drafts.set(key, existing === pending.text ? pending.text : `${pending.text}${existing.trim() ? `\n\n${existing}` : ""}`);
    }
    state.pending.delete(roomId);
    persistPending();
    if (pending.channel === "stage" || canReadBackstage()) switchChannel(pending.channel);
    $("message-input").value = state.drafts.get(`${roomId}:${state.channel}`) || "";
    persistDrafts();
    renderPending();
    renderComposer();
    if (isSticker && state.channel === pending.channel) openStickerPanel();
    else if (!$("message-input").disabled) $("message-input").focus();
  }

  function configureTransport(value) {
    if (PREVIEW) return;
    if (value !== undefined && (!value || !["poll", "websocket"].includes(value.type))) {
      throw new ApiError("服务器的现场连接方式暂不可用，请刷新后重试。");
    }
    const bounded = (input, fallback, min, max) => Number.isFinite(Number(input))
      ? Math.min(max, Math.max(min, Math.round(Number(input)))) : fallback;
    const poll = value?.type === "poll";
    state.transport = {
      type: poll ? "poll" : "websocket",
      intervalMs: poll ? bounded(value.interval_ms, 1500, 1000, 30000) : 8000,
      presenceIntervalMs: bounded(value?.presence_interval_ms, 15000, 5000, 30000),
      presenceTtlMs: bounded(value?.presence_ttl_ms, 45000, 15000, 300000),
    };
    if (poll && !state.presenceBaseId) state.presenceBaseId = freshMessageId();
    document.body.dataset.transport = state.transport.type;
    document.querySelector(".presence-note").textContent = poll
      ? "暂时断线或长时间留在后台会显示暂离，角色仍会为你保留。"
      : "暂时断线，角色会等你回来。";
  }

  function schedulePoll() {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (PREVIEW || !state.room || !state.authenticated || !navigator.onLine) return;
    const generation = state.generation;
    const delay = document.hidden && state.transport.type === "poll"
      ? Math.max(15000, state.transport.intervalMs) : state.transport.intervalMs;
    // Schedule from completion, so a slow network never creates parallel syncs
    // or an unbounded queue. Pagination still runs immediately to catch up.
    state.pollTimer = window.setTimeout(() => {
      state.pollTimer = null;
      if (generation === state.generation) scheduleSync(0);
    }, delay);
  }

  function schedulePresence() {
    window.clearTimeout(state.presenceTimer);
    state.presenceTimer = null;
    if (PREVIEW || state.transport.type !== "poll" || !state.room || !state.authenticated || !navigator.onLine) return;
    const generation = state.generation;
    const delay = document.hidden ? Math.min(30000, state.transport.presenceTtlMs * 2 / 3) : state.transport.presenceIntervalMs;
    state.presenceTimer = window.setTimeout(() => {
      state.presenceTimer = null;
      if (generation === state.generation) sendPresence();
    }, delay);
  }

  async function sendPresence() {
    if (PREVIEW || state.transport.type !== "poll" || !state.room || !state.authenticated || !navigator.onLine || state.presenceController) return;
    window.clearTimeout(state.presenceTimer);
    state.presenceTimer = null;
    const generation = state.generation;
    const id = currentRoomId();
    const controller = new AbortController();
    state.presenceController = controller;
    try {
      const result = await api(`${roomPath(id)}/presence`, {
        method: "POST", body: { connection_id: state.presenceId, active: true }, timeout: 6000, signal: controller.signal,
      });
      if (generation !== state.generation || controller.signal.aborted) return;
      if (result.ok !== true) throw new ApiError("在线状态尚未确认。");
      const first = !state.lastPresence;
      state.lastPresence = Date.now();
      state.presenceError = "";
      if (first) scheduleSync(0);
    } catch (error) {
      if (generation !== state.generation || controller.signal.aborted || !state.authenticated) return;
      state.presenceError = `在线状态暂未确认：${readableError(error)}`;
      if (error instanceof ApiError && [403, 404].includes(error.status)) scheduleSync(0);
    } finally {
      if (state.presenceController === controller) state.presenceController = null;
      if (generation === state.generation) { schedulePresence(); updateConnection(); }
    }
  }

  function releasePresence() {
    if (PREVIEW || state.transport.type !== "poll" || !state.room || !state.presenceId || !state.csrf || !navigator.onLine) return;
    // A best-effort per-view departure changes presence only. The server's TTL
    // remains authoritative when a browser is killed or the network disappears.
    // A detached departure must never expire or mutate a newer UI session.
    fetch(`${(window.ONSTAGE_CONFIG?.apiBaseUrl || "").replace(/\/$/, "")}${roomPath(currentRoomId())}/presence`, {
      method: "POST", credentials: "include", cache: "no-store", keepalive: true,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: JSON.stringify({ connection_id: state.presenceId, active: false }),
    }).catch(() => {});
  }

  function scheduleSync(delay = 120) {
    if (!state.room || !state.authenticated) return;
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
    state.syncQueued = true;
    if (state.syncRunning || state.syncTimer !== null) return;
    const generation = state.generation;
    state.syncTimer = window.setTimeout(() => {
      state.syncTimer = null;
      if (generation === state.generation) syncRoom();
    }, delay);
  }

  function restartSync() {
    if (!state.room || !state.authenticated) return;
    state.syncQueued = true;
    state.syncError = "";
    // Read requests can be restarted safely. Never cancel or recreate a message
    // POST here: its acknowledgement and original idempotency key remain intact.
    state.syncController?.abort();
    if (state.syncTimer !== null) {
      window.clearTimeout(state.syncTimer);
      state.syncTimer = null;
    }
    scheduleSync(0);
  }

  async function syncRoom() {
    if (!state.room || !state.authenticated || state.syncRunning) return;
    if (!PREVIEW && !navigator.onLine) {
      $("manual-sync").disabled = false;
      updateConnection();
      return;
    }
    const generation = state.generation;
    const id = currentRoomId();
    const controller = new AbortController();
    state.syncController = controller;
    state.syncRunning = true;
    try {
      do {
        state.syncQueued = false;
        let more;
        do {
          const before = { ...state.cursors };
          const params = new URLSearchParams({ after_stage: String(before.stage), after_backstage: String(canReadBackstage() ? before.backstage : 0) });
          const result = await api(`${roomPath(id)}/sync?${params}`, { timeout: 6000, signal: controller.signal });
          if (generation !== state.generation || currentRoomId() !== id) return;
          if (controller.signal.aborted) return;
          if (!result.room?.me || !result.messages || !result.cursors || !result.has_more) throw new ApiError("本场同步响应不完整，请重新核对。");
          applyRoom(result.room);
          for (const channel of ["stage", "backstage"]) {
            if (channel === "backstage" && !canReadBackstage()) continue;
            const batch = result.messages[channel];
            const cursor = Number(result.cursors[channel]);
            if (!Array.isArray(batch) || !Number.isFinite(cursor) || cursor < before[channel]) throw new ApiError("记录游标异常，已停止这一轮同步。请重新核对。");
            for (const message of batch) insertMessage(message);
            state.cursors[channel] = cursor;
            if (result.has_more[channel] && cursor <= before[channel]) throw new ApiError("服务器提示仍有记录，但读取位置没有推进。请联系测试组织者核对。");
          }
          more = Boolean(result.has_more.stage || (canReadBackstage() && result.has_more.backstage));
          $("sync-progress").textContent = more ? `正在补齐记录 · ${state.messages.stage.size + state.messages.backstage.size} 条` : "";
          state.lastSync = Date.now();
          state.syncError = "";
          if (!more) state.initialSynced = true;
          renderRoom();
        } while (more && generation === state.generation);
      } while (state.syncQueued && generation === state.generation);
    } catch (error) {
      if (generation !== state.generation || currentRoomId() !== id || !state.authenticated) return;
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && (error.status === 403 || error.status === 404)) {
        toast(`已无法继续读取本场：${readableError(error)}`, "error", 9000);
        await backToLobby({ replace: true });
        return;
      }
      state.syncError = readableError(error);
      $("sync-progress").textContent = "同步暂未完成";
      updateConnection();
    } finally {
      if (generation === state.generation) {
        if (state.syncController === controller) state.syncController = null;
        state.syncRunning = false;
        $("manual-sync").disabled = false;
        if (state.syncQueued && (PREVIEW || navigator.onLine)) scheduleSync(state.syncError ? 500 : 0);
        else schedulePoll();
      }
    }
  }

  function connectSocket() {
    if (PREVIEW) { updateConnection(); return; }
    if (!state.room || !state.authenticated) return;
    if (state.transport.type === "poll") { sendPresence(); updateConnection(); return; }
    window.clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
    if (!navigator.onLine) { state.socketMode = "offline"; updateConnection(); return; }
    const generation = state.generation;
    const id = currentRoomId();
    if (state.socket && (state.socket.readyState === WebSocket.OPEN || state.socket.readyState === WebSocket.CONNECTING)) return;
    state.socketMode = state.reconnectAttempts ? "retrying" : "connecting";
    updateConnection();
    const url = new URL(`${roomPath(id)}/stream`, window.ONSTAGE_CONFIG?.wsBaseUrl || window.ONSTAGE_CONFIG?.apiBaseUrl || location.origin);
    url.protocol = ["https:", "wss:"].includes(url.protocol) ? "wss:" : "ws:";
    let socket;
    try { socket = new WebSocket(url.href); }
    catch {
      state.socketError = "实时连接暂不可用，正在周期核对记录。";
      scheduleReconnect(generation);
      return;
    }
    state.socket = socket;
    socket.onopen = () => {
      if (generation !== state.generation || state.socket !== socket) { socket.close(); return; }
      state.socketMode = "online";
      state.socketError = "";
      state.reconnectAttempts = 0;
      state.lastSocketMessage = Date.now();
      restartSync();
      updateConnection();
      window.clearInterval(state.heartbeatTimer);
      state.heartbeatTimer = window.setInterval(() => {
        if (generation !== state.generation || state.socket !== socket || socket.readyState !== WebSocket.OPEN || document.hidden) return;
        if (Date.now() - state.lastSocketMessage > 50000) {
          socket.close(4000, "Heartbeat expired");
          return;
        }
        socket.send("ping");
      }, 18000);
    };
    socket.onmessage = (event) => {
      if (generation !== state.generation || state.socket !== socket) return;
      state.lastSocketMessage = Date.now();
      try {
        const data = JSON.parse(event.data);
        if (data.type === "sync") scheduleSync(60);
      } catch { /* Business data is never interpreted from an unrecognised WS frame. */ }
    };
    socket.onerror = () => {
      if (generation !== state.generation || state.socket !== socket) return;
      state.socketMode = "retrying";
      updateConnection();
    };
    socket.onclose = (event) => {
      if (generation !== state.generation || state.socket !== socket) return;
      window.clearInterval(state.heartbeatTimer);
      state.heartbeatTimer = null;
      state.socket = null;
      if (event.code === 1008) state.socketError = "实时连接未获允许。正在重新核对连接；如果持续出现，请稍后重试。";
      scheduleSync(0);
      scheduleReconnect(generation);
    };
  }

  function scheduleReconnect(generation) {
    if (PREVIEW || state.transport.type === "poll") return;
    if (generation !== state.generation || !state.room || !state.authenticated) return;
    state.reconnectAttempts += 1;
    state.socketMode = navigator.onLine ? "retrying" : "offline";
    if (state.reconnectAttempts >= 3 && !state.socketError) state.socketError = "实时连接正在重试，记录每 8 秒自动核对。发言仍须等待服务器确认。";
    updateConnection();
    window.clearTimeout(state.reconnectTimer);
    if (navigator.onLine) state.reconnectTimer = window.setTimeout(() => {
      state.reconnectTimer = null;
      if (generation === state.generation) connectSocket();
    }, Math.min(8000, 1000 * (2 ** Math.min(3, state.reconnectAttempts - 1))));
  }

  function updateConnection() {
    const badge = $("connection-state");
    if (PREVIEW) {
      badge.dataset.state = state.syncError ? "error" : "preview";
      $("connection-label").textContent = "界面评审";
      $("network-banner").hidden = true;
      $("room-alert").hidden = !state.room || !state.syncError;
      $("room-alert").textContent = state.syncError ? `${state.syncError} 可以点击“重新读取”核对。` : "";
      $("last-sync").textContent = state.lastSync ? `本机记录 · ${timeLabel(state.lastSync)}` : "正在读取本机记录";
      updateViewport();
      return;
    }
    let mode = "idle";
    let label = state.authenticated ? "连接正常" : state.awaitingNickname ? "填写昵称即可进入" : "正在连接";
    if (!navigator.onLine) { mode = "offline"; label = "浏览器已离线"; }
    else if (state.room) {
      if (state.transport.type === "poll") {
        const present = state.lastPresence && Date.now() - state.lastPresence < state.transport.presenceTtlMs;
        mode = state.syncError ? "error" : state.presenceError ? "retrying" : state.lastSync && present ? "online" : "connecting";
        label = state.syncError ? "记录同步受阻" : state.presenceError ? "在线状态待核对" : mode === "online" ? "多人同步正常" : "正在同步现场";
      } else {
        mode = state.syncError ? "error" : state.socketMode;
        if (state.syncError) label = "记录同步受阻";
        else if (mode === "online") label = state.lastSync ? "实时连接正常" : "连接后同步中";
        else if (mode === "connecting") label = "正在连接现场";
        else if (mode === "retrying") label = "实时连接重试";
        else label = "正在核对现场";
      }
    }
    badge.dataset.state = mode;
    $("connection-label").textContent = label;
    $("network-banner").hidden = navigator.onLine;
    $("network-banner").textContent = "浏览器当前处于离线状态。席位会保留；已输入内容不会自动当作发送成功，恢复连接后请核对。";
    if (state.room) {
      const issue = state.syncError ? `记录同步暂未完成：${state.syncError} 会自动重试，也可以点击“重新同步”。`
        : state.transport.type === "poll" ? state.presenceError : state.socketError;
      $("room-alert").hidden = !issue;
      $("room-alert").textContent = issue;
      $("last-sync").textContent = state.lastSync ? `最近核对 ${timeLabel(state.lastSync)}` : "正在核对本场记录";
    } else $("room-alert").hidden = true;
    updateViewport();
  }

  async function copyRoomLink() {
    if (!state.room) return;
    const accountFrontend = location.pathname.startsWith("/play/");
    const url = new URL(accountFrontend ? `/room/${encodeURIComponent(currentRoomId())}` : location.pathname, location.origin);
    if (!PREVIEW && !accountFrontend) url.hash = `room=${encodeURIComponent(currentRoomId())}`;
    try {
      await navigator.clipboard.writeText(url.href);
      toast(PREVIEW ? "评审链接已复制；朋友打开后会看到自己的浏览器记录。" : accountFrontend ? "房间链接已复制。朋友登录并获得房间访问权限后可入场。" : "房间链接已复制。朋友填写昵称后就能以旁观者进入，无需注册。");
    } catch {
      const extra = node("div", "confirm-extra");
      const label = node("label", "", PREVIEW ? "预览链接" : "入场链接");
      label.htmlFor = "copy-link-fallback";
      const input = node("input");
      input.id = "copy-link-fallback";
      input.value = url.href;
      input.readOnly = true;
      input.addEventListener("focus", () => input.select());
      extra.append(label, input);
      confirmAction({ title: PREVIEW ? "复制评审链接" : "复制本场入场链接", description: PREVIEW ? "选中下方链接复制。朋友打开后会看到自己的浏览器记录，不会进入你的本机房间。" : "浏览器未允许自动复制。选中下方链接复制，朋友填写昵称后就能以旁观者进入。", accept: "完成", cancel: "关闭", extra });
      input.focus();
      input.select();
    }
  }

  async function handleRoute() {
    if (state.awaitingNickname) { showGuestEntry(false); return; }
    if (!state.authenticated || !state.catalog.scenarios.length) return;
    const id = routeRoomId();
    if (id) await enterRoom(id, true);
    else if (state.room) await backToLobby({ replace: true });
  }

  async function refreshLobby() {
    if (!state.authenticated) return;
    const scope = state.scope;
    if (!state.catalog.scenarios.length) {
      try {
        const catalog = await api("/api/catalog");
        if (!state.authenticated || scope !== state.scope) return;
        if (!Array.isArray(catalog.scenarios) || !Array.isArray(catalog.characters) || !Array.isArray(catalog.bands)) throw new ApiError("场景暂未准备好。");
        state.catalog = { ...catalog, stickers: Array.isArray(catalog.stickers) ? catalog.stickers : [] };
        renderScenarios();
        renderLobbyCharacters();
      } catch (error) {
        if (scope === state.scope) toast(readableError(error), "error");
        return;
      }
    }
    await fetchRooms();
    if (state.authenticated && scope === state.scope && !state.room && routeRoomId()) await handleRoute();
  }

  function queueRoute() {
    window.clearTimeout(state.routeTimer);
    state.routeTimer = window.setTimeout(handleRoute, 0);
  }

  async function restartPreviewSession(reset = false, external = false) {
    if (!PREVIEW || state.previewResetting) return;
    if (!previewAdapter) { toast("本机预览组件未载入，请刷新页面再试。", "error"); return; }
    state.previewResetting = true;
    state.sessionGeneration += 1;
    state.navigation += 1;
    $("reset-preview").disabled = true;
    stopRoom();
    clearSessionContent(state.scope);
    state.authenticated = false;
    state.csrf = "";
    state.scope = "";
    state.nickname = "";
    state.awaitingNickname = false;
    $("guest-nickname").value = "";
    $("guest-form").hidden = true;
    $("loading-spark").hidden = false;
    state.pending.clear(); state.drafts.clear(); state.stickerDrafts.clear(); state.posting.clear();
    state.rooms = []; state.reportList = []; state.reportsGeneration += 1;
    state.catalog = { bands: [], characters: [], scenarios: [], stickers: [] };
    closeAllDialogs();
    setRoomRoute(null, true);
    setView("loading");
    $("loading-title").textContent = reset ? "正在重置本机预览…" : "正在读取本机预览…";
    $("loading-note").textContent = "载入场景和这个浏览器的记录。";
    try {
      const result = reset ? await previewAdapter.reset() : await previewAdapter.request("/api/session", { method: "POST", body: {} });
      await acceptSession(result);
      if (reset) toast("本机预览已重置。房间、消息和草稿已清空，主题偏好保留。", "info");
      else if (external) toast("本机记录已在另一个页面重置，这里也已重新打开。", "info");
    } catch (error) {
      $("loading-title").textContent = "本机预览暂未打开";
      $("loading-note").textContent = "可以重新尝试，原有记录是否清除以操作结果为准。";
      $("loading-error").textContent = error?.message || readableError(error);
      $("loading-error").hidden = false;
      $("retry-bootstrap").hidden = false;
      setView("loading");
    } finally {
      state.previewResetting = false;
      $("reset-preview").disabled = false;
      updateViewport();
    }
  }

  async function resetLocalPreview() {
    if (!PREVIEW || state.previewResetting) return;
    const confirmed = await confirmAction({ title: "重置本机预览？", description: "将清空这个浏览器的预览房间、消息、选角和草稿，重新开始。主题偏好会保留。", accept: "清空并重新开始", cancel: "保留记录", danger: true });
    if (confirmed) await restartPreviewSession(true);
  }

  function setupPreviewUI() {
    if (!PREVIEW) return;
    document.body.dataset.preview = "true";
    $("preview-banner").hidden = false;
    $("reset-preview").disabled = !previewAdapter;
    $("loading-title").textContent = "正在打开本机预览…";
    $("loading-note").textContent = "载入场景和你的浏览器记录。";
    setButtonText($("retry-bootstrap"), "重新读取", "refresh");
    setButtonText($("hero-join"), "查看本机记录", "arrow-right");
    setButtonText($("copy-room-link"), "分享评审", "link");
    $("browse-rooms").textContent = "本机房间";
    document.querySelector(".hero-note").textContent = "场景体验 · 角色与贴纸 · 本机保存";
    $("directory-title").firstChild.textContent = "本机房间 ";
    $("room-directory").querySelector(".section-description").textContent = "你在此浏览器创建的场景和记录。";
    $("desk-heading").textContent = "你的本机舞台";
    document.querySelector(".presence-note").textContent = "只有你的本机操作会出现在这里。";
    document.querySelector(".seat-footnote").textContent = "想体验另一个角色，可以再开一个场景。";
    $("manual-sync").textContent = "重新读取";
    $("feedback-button").setAttribute("aria-label", "举报功能说明");
    $("feedback-button").title = "举报功能说明";
    if (typeof ResizeObserver === "function") new ResizeObserver(updateViewport).observe($("preview-banner"));
    previewAdapter?.subscribe((event) => {
      if (state.previewResetting) return;
      if (event.type === "reset") { restartPreviewSession(false, Boolean(event.external)); return; }
      if (!state.authenticated) return;
      if (state.room) { if (!event.room_id || sameId(event.room_id, state.room.id)) scheduleSync(0); }
      else fetchRooms();
    });
  }

  function bindEvents() {
    if (state.eventsBound) return;
    state.eventsBound = true;
    document.querySelector(".skip-link").addEventListener("click", (event) => { event.preventDefault(); $("main").focus(); });
    $("retry-bootstrap").addEventListener("click", connectAnonymous);
    $("guest-form").addEventListener("submit", submitGuest);
    $("guest-nickname").addEventListener("input", () => { $("guest-error").hidden = true; $("guest-nickname").removeAttribute("aria-invalid"); });
    $("guest-nickname").addEventListener("compositionstart", () => { state.guestComposing = true; });
    $("guest-nickname").addEventListener("compositionend", () => { state.guestComposing = false; });
    $("guest-nickname").addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.isComposing || state.guestComposing || event.keyCode === 229)) event.preventDefault(); });
    $("reset-preview").addEventListener("click", resetLocalPreview);
    $("home-button").addEventListener("click", () => { if (state.authenticated) backToLobby(); });
    $("browse-rooms").addEventListener("click", () => showLobbySection("room-directory"));
    $("browse-scenes").addEventListener("click", () => showLobbySection("scene-section"));
    $("hero-create").addEventListener("click", () => showLobbySection("scene-section"));
    $("hero-join").addEventListener("click", () => showLobbySection("room-directory"));
    $("theme-toggle").addEventListener("click", toggleTheme);
    $("preferences-button").addEventListener("click", () => $("preferences-dialog").showModal());
    $("close-preferences").addEventListener("click", () => $("preferences-dialog").close());
    $("send-shortcut").addEventListener("change", () => {
      state.sendShortcut = $("send-shortcut").value === "ctrl-enter" ? "ctrl-enter" : "enter";
      try { localStorage.setItem("onstage.send-shortcut", state.sendShortcut); } catch { /* This visit still works without storage. */ }
      if (state.room) renderComposer();
    });
    $("download-artwork").addEventListener("click", () => window.OnstagePWA?.downloadArtwork().catch(() => {}));
    $("cancel-artwork").addEventListener("click", () => window.OnstagePWA?.cancelDownload());
    $("clear-artwork").addEventListener("click", () => window.OnstagePWA?.clearArtwork().catch(() => {}));
    $("room-backdrop-image").addEventListener("load", () => { $("room-backdrop").dataset.ready = "true"; });
    $("room-backdrop-image").addEventListener("error", () => { $("room-backdrop").dataset.ready = "false"; });
    $("feedback-button").addEventListener("click", () => openReports(null));
    $("refresh-rooms").addEventListener("click", refreshLobby);
    $("rooms-previous").addEventListener("click", () => fetchRooms({ page: state.roomPage - 1, cursor: state.roomCursors[state.roomPage - 1] || "" }));
    $("rooms-next").addEventListener("click", () => fetchRooms({ page: state.roomPage + 1, cursor: state.roomCursors[state.roomPage + 1] || "" }));
    for (const button of document.querySelectorAll("[data-room-filter]")) button.addEventListener("click", () => {
      state.filter = button.dataset.roomFilter;
      for (const other of document.querySelectorAll("[data-room-filter]")) { const selected = other === button; other.classList.toggle("selected", selected); other.setAttribute("aria-pressed", String(selected)); }
      state.rooms = []; state.roomPage = 0; state.roomHasMore = false;
      renderLoading($("room-list"), "正在读取这一类场次…");
      fetchRooms();
    });
    $("create-form").addEventListener("submit", createRoom);
    $("settings-form").addEventListener("submit", saveSettings);
    $("report-form").addEventListener("submit", submitReport);
    $("resolve-form").addEventListener("submit", resolveReport);
    for (const [dialog, buttons] of [
      ["create-dialog", ["close-create", "cancel-create"]], ["settings-dialog", ["close-settings", "cancel-settings"]],
      ["report-dialog", ["close-report", "cancel-report"]], ["reports-dialog", ["close-reports"]],
      ["resolve-dialog", ["close-resolve", "cancel-resolve"]],
    ]) for (const id of buttons) $(id).addEventListener("click", () => $(dialog).close("cancel"));
    $("confirm-cancel").addEventListener("click", () => $("confirm-dialog").close("cancel"));
    $("confirm-accept").addEventListener("click", () => $("confirm-dialog").close("accept"));
    $("confirm-dialog").addEventListener("close", () => {
      if (state.confirmResolver) { const resolver = state.confirmResolver; state.confirmResolver = null; resolver($("confirm-dialog").returnValue === "accept"); }
    });
    $("room-settings").addEventListener("click", openSettings);
    $("announcement-edit").addEventListener("click", openSettings);
    $("report-room-button").addEventListener("click", () => openReport(null));
    $("room-reports-button").addEventListener("click", () => openReports(currentRoomId()));
    $("refresh-reports").addEventListener("click", () => openReports(state.reportsScope || null));
    $("more-reports").addEventListener("click", fetchReports);
    $("back-to-lobby").addEventListener("click", () => backToLobby());
    $("leave-room").addEventListener("click", leaveRoom);
    $("copy-room-link").addEventListener("click", copyRoomLink);
    $("audience-label").addEventListener("click", showMembers);
    $("manual-sync").addEventListener("click", () => {
      if (!PREVIEW && !navigator.onLine) { updateConnection(); toast("现在处于离线状态，网络恢复后会自动同步。", "error"); return; }
      $("manual-sync").disabled = true; restartSync();
      if (!PREVIEW && (!state.socket || state.socket.readyState > WebSocket.OPEN)) connectSocket();
    });
    for (const button of document.querySelectorAll(".close-mobile-panel")) button.addEventListener("click", () => setMobilePanel("stage", true));
    for (const button of document.querySelectorAll("[data-room-panel]")) {
      button.addEventListener("click", () => setMobilePanel(button.dataset.roomPanel));
      button.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault(); const names = ["seats", "stage", "desk"]; const current = names.indexOf(button.dataset.roomPanel);
        const index = event.key === "Home" ? 0 : event.key === "End" ? 2 : (current + (event.key === "ArrowRight" ? 1 : -1) + 3) % 3;
        setMobilePanel(names[index], true);
      });
    }
    for (const button of document.querySelectorAll("[data-channel]")) {
      button.addEventListener("click", () => switchChannel(button.dataset.channel));
      button.addEventListener("keydown", (event) => {
        if ((event.key === "ArrowLeft" || event.key === "ArrowRight") && canReadBackstage()) { event.preventDefault(); switchChannel(state.channel === "stage" ? "backstage" : "stage"); $(state.channel + "-channel").focus(); }
      });
    }
    $("message-form").addEventListener("submit", submitMessage);
    $("open-stickers").addEventListener("click", openStickerPanel);
    $("close-stickers").addEventListener("click", () => $("sticker-dialog").close("cancel"));
    $("clear-sticker").addEventListener("click", () => chooseSticker(""));
    $("send-sticker").addEventListener("click", submitSticker);
    $("sticker-category").addEventListener("change", () => { state.stickerCategory = $("sticker-category").value; state.stickerFilter = "all"; renderStickerFilters(); renderStickerList(); $("sticker-list").scrollTop = 0; });
    $("sticker-search").addEventListener("input", () => { state.stickerSearch = $("sticker-search").value; renderStickerList(); $("sticker-list").scrollTop = 0; });
    $("message-input").addEventListener("input", () => { saveDraft(); renderComposer(); });
    $("message-input").addEventListener("compositionstart", () => { state.composing = true; });
    $("message-input").addEventListener("compositionend", () => { state.composing = false; saveDraft(); renderComposer(); });
    $("message-input").addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.isComposing || state.composing || event.keyCode === 229 || event.shiftKey || event.altKey) return;
      if (state.sendShortcut === "enter" || event.ctrlKey || event.metaKey) {
        event.preventDefault();
        if (!event.repeat && !$("send-message").disabled) $("message-form").requestSubmit();
      }
    });
    $("message-input").addEventListener("focus", () => { updateViewport(); resizeComposer(); });
    document.addEventListener("focusin", updateViewport);
    document.addEventListener("focusout", () => window.setTimeout(updateViewport, 120));
    $("retry-message").addEventListener("click", () => { const pending = pendingMessage(); if (pending) postPending(pending); });
    $("restore-draft").addEventListener("click", restorePendingDraft);
    const jumpLatest = () => { state.followHistoryStart = false; $("message-feed").scrollTop = $("message-feed").scrollHeight; $("scroll-bottom").hidden = true; };
    $("scroll-bottom").addEventListener("click", jumpLatest);
    $("jump-latest").addEventListener("click", jumpLatest);
    $("jump-start").addEventListener("click", () => { state.followHistoryStart = true; $("message-feed").scrollTop = 0; $("message-feed").focus({ preventScroll: true }); });
    $("message-feed").addEventListener("scroll", () => { const feed = $("message-feed"); $("scroll-bottom").hidden = feed.scrollHeight - feed.clientHeight - feed.scrollTop < 65; if (feed.scrollTop > 65) state.followHistoryStart = false; }, { passive: true });
    window.addEventListener("hashchange", queueRoute);
    window.addEventListener("popstate", queueRoute);
    window.addEventListener("resize", () => { updateViewport(); resizeComposer(); });
    window.visualViewport?.addEventListener("resize", () => { updateViewport(); resizeComposer(); });
    window.visualViewport?.addEventListener("scroll", updateViewport);
    window.addEventListener("storage", (event) => { if (event.key === "onstage.theme") { document.documentElement.dataset.theme = event.newValue === "dark" ? "dark" : "light"; updateTheme(); } });
    window.addEventListener("offline", () => {
      if (PREVIEW) { updateConnection(); return; }
      state.syncQueued = true;
      state.syncController?.abort();
      state.presenceController?.abort();
      window.clearTimeout(state.pollTimer); window.clearTimeout(state.presenceTimer);
      state.pollTimer = state.presenceTimer = null;
      if (state.socket) state.socket.close(4001, "Browser offline");
      state.socketMode = "offline";
      updateConnection();
    });
    window.addEventListener("online", () => { updateConnection(); if (state.room) { restartSync(); connectSocket(); } else if (state.authenticated) refreshLobby(); else if (!state.awaitingNickname) connectAnonymous(); });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && state.authenticated) {
        if (state.room) { restartSync(); if (!PREVIEW && state.socket?.readyState === WebSocket.OPEN && Date.now() - state.lastSocketMessage > 50000) state.socket.close(4000, "Resume connection"); else connectSocket(); }
        else refreshLobby();
      } else if (document.hidden && state.room && state.transport.type === "poll") {
        schedulePoll(); schedulePresence();
      }
    });
    window.addEventListener("pagehide", releasePresence);
    window.addEventListener("pageshow", (event) => {
      if (event.persisted && state.authenticated && state.room) { restartSync(); connectSocket(); }
    });
    window.addEventListener("beforeunload", () => { saveDraft(); persistPending(); persistStickerDrafts(); });
  }

  async function boot() {
    hydrateIcons();
    bindEvents();
    setupPreviewUI();
    updateTheme();
    updateViewport();
    updateConnection();
    try { state.sendShortcut = localStorage.getItem("onstage.send-shortcut") === "ctrl-enter" ? "ctrl-enter" : "enter"; } catch { /* Default remains usable. */ }
    $("send-shortcut").value = state.sendShortcut;
    state.artworkReady = (async () => {
      if (PREVIEW) return;
      const controller = new AbortController();
      const timer = window.setTimeout(() => controller.abort(), 1200);
      try {
        const response = await fetch("/static/assets-manifest.json", { cache: "no-cache", credentials: "omit", signal: controller.signal });
        if (response.ok) {
          const manifest = await response.json();
          if (manifest.schema_version === 1 && manifest.assets) for (const [original, asset] of Object.entries(manifest.assets)) {
            if (original.startsWith("/static/assets/") && /^\/static\/assets\/optimized\/[0-9a-f]{64}\.webp$/.test(asset?.url)) state.assetMap[original] = asset;
          }
        }
      } catch { /* Original URLs remain a complete compatible fallback. */ }
      finally { window.clearTimeout(timer); }
    })();
    void state.artworkReady.then(() => {
      $("hero-mygo").src = localAssetPath("/static/assets/bands/mygo.webp");
      $("hero-mujica").src = localAssetPath("/static/assets/bands/mujica.webp");
    });
    if (location.pathname.startsWith("/play/") && window.parent !== window) {
      try {
        if (window.parent.location.origin === location.origin && window.parent.OnstageArtworkCache) window.OnstagePWA = window.parent.OnstageArtworkCache;
      } catch { /* A standalone or cross-origin frame uses its own bounded cache controller. */ }
    }
    const updateMotionPreference = () => {
      let reduce = false;
      try { reduce = localStorage.getItem("onstage.reduce-motion") === "true"; } catch {}
      document.documentElement.dataset.motion = reduce ? "reduce" : "full";
    };
    updateMotionPreference();
    const motionStyle = document.createElement("style");
    motionStyle.textContent = ":root[data-motion=reduce] *,:root[data-motion=reduce] *::before,:root[data-motion=reduce] *::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}";
    document.head.append(motionStyle);
    window.addEventListener("storage", (event) => { if (event.key === "onstage.reduce-motion") updateMotionPreference(); });
    window.addEventListener("pagehide", () => window.OnstagePWA?.unsubscribe?.(renderArtworkStatus));
    if (!PREVIEW) window.OnstagePWA?.init({ onStatus: renderArtworkStatus });
    else { $("download-artwork").disabled = $("clear-artwork").disabled = true; $("artwork-status").textContent = "本机评审不启用多人站点的素材缓存。"; }
    await connectAnonymous();
  }

  function renderArtworkStatus(status) {
    const progress = status.busy ? `已处理 ${status.completed || 0} / ${status.total || 0} 张${status.failed ? `，${status.failed} 张未完成` : ""}。` : status.cached ? `已保存 ${status.cached} 张，${((status.cachedBytes || 0) / 1000000).toFixed(2)} MB。` : "";
    $("artwork-status").textContent = `${progress}${status.error || status.message || "素材按需保存，聊天仍需联网。"}`;
    $("download-artwork").disabled = !status.supported || status.busy;
    $("cancel-artwork").hidden = !status.busy;
    $("clear-artwork").disabled = !status.supported || status.busy;
  }

  boot();
})();
