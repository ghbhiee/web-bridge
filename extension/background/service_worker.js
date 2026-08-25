// service_worker.js — MV3 background for web-bridge.
//
// Holds an authenticated WebSocket to the local bridge server. On each
// `command` it resolves a target tab, makes sure the relay+page content scripts
// are live in it, forwards the command into the tab, and relays the result
// back. Results travel as chrome.runtime messages carrying their own id, so a
// recycled SW is simply re-woken and forwards by id (no correlation table).

import { BRIDGE_WS, BRIDGE_TOKEN } from "../config.js";

let ws = null;
let reconnectDelay = 800;
let pingTimer = null;
const outbox = [];

// --------------------------------------------------------------------------- //
// WebSocket to the bridge
// --------------------------------------------------------------------------- //
function send(obj) {
  const data = JSON.stringify(obj);
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(data); return; } catch (_) {}
  }
  outbox.push(data);
  connect();
}
function flush() {
  while (outbox.length && ws && ws.readyState === WebSocket.OPEN) ws.send(outbox.shift());
}
function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  try {
    ws = new WebSocket(BRIDGE_WS + "?token=" + encodeURIComponent(BRIDGE_TOKEN));
  } catch (_) {
    scheduleReconnect();
    return;
  }
  ws.onopen = () => {
    reconnectDelay = 800;
    send({ type: "hello", info: { ua: navigator.userAgent, ext: "web-bridge", v: "0.1.0" } });
    flush();
    if (pingTimer) clearInterval(pingTimer);
    pingTimer = setInterval(() => send({ type: "ping", t: Date.now() }), 20000);
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "notify" && msg.action === "sync-autorun") {
      syncAutorun().then((r) => console.log("[web-bridge] autorun resync", r));
      return;
    }
    if (msg.type === "command") {
      handleCommand(msg).catch((e) =>
        send({ type: "result", id: msg.id, ok: false, error: String((e && e.message) || e) })
      );
    }
  };
  ws.onclose = () => {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    scheduleReconnect();
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}
function scheduleReconnect() {
  const d = reconnectDelay;
  reconnectDelay = Math.min(reconnectDelay * 1.7, 5000);
  setTimeout(connect, d);
}

// --------------------------------------------------------------------------- //
// tab helpers
// --------------------------------------------------------------------------- //
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const q = (query) => new Promise((res) => chrome.tabs.query(query, (t) => res(t || [])));

// Pages the extension can never script: privileged chrome:// surfaces, the Web
// Store, PDFs, and blank tabs. Picking one of these as the target produced a
// confusing "manifest must request permission" error, so filter them out when
// resolving a tab rather than failing at injection time.
// A tab can be targeted only if it is a normal web page AND currently loaded.
// Chrome discards background tabs to save memory (`discarded: true`,
// `status: "unloaded"`); those cannot be scripted and fail with a misleading
// "manifest must request permission" error — so skip them when picking a tab,
// and reload them when they are the only match.
function injectable(tab) {
  const url = tab && tab.url;
  if (!url) return false;
  if (tab.discarded || tab.status === "unloaded") return false;
  return /^https?:\/\//i.test(url) && !/^https:\/\/chrome\.google\.com\/webstore/i.test(url)
         && !/^https:\/\/chromewebstore\.google\.com/i.test(url);
}

// Bring a discarded tab back to life so it can be scripted.
async function revive(tab) {
  await new Promise((r) => chrome.tabs.reload(tab.id, {}, () => r()));
  await waitComplete(tab.id);
  await sleep(700);
  return tab.id;
}

function matchAny(url, patterns) {
  if (!url || !patterns || !patterns.length) return false;
  return patterns.some((p) => {
    // glob: convert "*://host/*" to a RegExp
    const rx = new RegExp("^" + p.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*") + "$");
    return rx.test(url);
  });
}

function waitComplete(tabId, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    let done = false;
    const finish = (ok, err) => {
      if (done) return; done = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      ok ? resolve() : reject(err);
    };
    const listener = (id, info) => { if (id === tabId && info.status === "complete") finish(true); };
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId, (t) => {
      if (chrome.runtime.lastError) return finish(false, new Error("标签页不存在"));
      if (t && t.status === "complete") finish(true);
    });
    const timer = setTimeout(() => finish(false, new Error("页面加载超时")), timeoutMs);
  });
}

// Which page did the command actually land on? Callers address tabs by URL
// FRAGMENT ("--url x.com", or a conversation id), so the caller's string is not
// a usable identity — the journal was grouping one script under three different
// "hosts" because three ChatGPT conversation ids were passed as urls.
async function tabUrl(tabId) {
  try {
    const t = await new Promise((r) => chrome.tabs.get(tabId, (x) => r(x)));
    return (t && t.url) || "";
  } catch (_) { return ""; }
}

async function activate(tabId) {
  // make it the active tab of its window (unthrottled) WITHOUT focusing the window
  try { await new Promise((r) => chrome.tabs.update(tabId, { active: true }, () => r())); } catch (_) {}
}

function normalizeUrl(u) {
  // a bare host/substring used to CREATE a tab needs a scheme, else Chrome
  // resolves it relative to the extension origin (chrome-extension://…/host).
  if (/^[a-z]+:\/\//i.test(u) || u.startsWith("chrome:")) return u;
  return "https://" + u.replace(/^\/+/, "");
}

// --------------------------------------------------------------------------- //
// tab ownership — don't steal a page another tool is driving
// --------------------------------------------------------------------------- //
// web-bridge used to pick *any* chatgpt.com tab by URL and, with --new, navigate
// it to a fresh conversation. When chatgpt-osascript had a tab pinned there, that
// navigation hijacked its session and the two tools started reading each other's
// conversations. So: prefer the tab we opened ourselves (remembered per site,
// in session storage so it survives a service-worker recycle), and skip pages
// that carry someone else's ownership marker.
const OWNERSHIP_PROBE = () => {
  try {
    const foreign = !!(window.__cgo || window.__cgoOwner ||
      (document.body && document.body.dataset && document.body.dataset.cgoOwned));
    const mine = !!(window.__webBridgeOwned ||
      (document.body && document.body.dataset && document.body.dataset.wbOwned));
    return { foreign: foreign && !mine, mine };
  } catch (_) {
    return { foreign: false, mine: false };
  }
};

const siteTabs = new Map();          // site name -> the tab id we consider ours
let siteTabsLoaded = false;

async function loadSiteTabs() {
  if (siteTabsLoaded) return;
  siteTabsLoaded = true;
  try {
    const o = await chrome.storage.session.get("wbSiteTabs");
    for (const [k, v] of Object.entries((o && o.wbSiteTabs) || {})) siteTabs.set(k, v);
  } catch (_) {}
}
function saveSiteTabs() {
  try { chrome.storage.session.set({ wbSiteTabs: Object.fromEntries(siteTabs) }); } catch (_) {}
}

// executeScript with `func` (not a code string) — no eval, so it works on
// Trusted-Types / strict-CSP sites, and it is read-only.
async function ownership(tabId) {
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId }, world: "MAIN", func: OWNERSHIP_PROBE,
    });
    return (r && r.result) || { foreign: false, mine: false };
  } catch (_) {
    return { foreign: false, mine: false };   // can't tell → don't block on it
  }
}

async function claim(site, tabId) {
  if (!site) return;
  siteTabs.set(site, tabId);
  saveSiteTabs();
  try {
    await chrome.scripting.executeScript({
      target: { tabId }, world: "MAIN",
      func: () => { window.__webBridgeOwned = true; try { document.body.dataset.wbOwned = "1"; } catch (_) {} },
    });
  } catch (_) {}
}

// Resolve a target tab from {site, matches, home, url, new_tab}.
// Returns {tabId, created} — `created` marks a tab this command opened, which
// the caller closes again if the page turns out to be unusable (a dead URL used
// to leave an error tab behind on every attempt).
async function resolveTab(p) {
  const all = await q({});
  // explicit url wins
  if (p.url) {
    let tab = all.find((t) => (t.url || "").includes(p.url) && injectable(t));
    if (!tab && !p.new_tab) {
      const sleeping = all.find((t) => (t.url || "").includes(p.url) && (t.discarded || t.status === "unloaded"));
      if (sleeping) { await revive(sleeping); return { tabId: sleeping.id, created: false }; }
    }
    let created = false;
    if (!tab || p.new_tab) {
      tab = await new Promise((r) => chrome.tabs.create({ url: normalizeUrl(p.url), active: !!p.activate }, r));
      created = true;
      ourTabs.add(tab.id);
      await waitComplete(tab.id); await sleep(600);
    }
    if (p.activate !== false) await activate(tab.id);
    return { tabId: tab.id, created };
  }
  // named site: our own tab first, then an unclaimed one, then a new one
  if (p.matches && p.matches.length) {
    await loadSiteTabs();
    const site = p.site || p.matches[0] || "";
    const matching = all.filter((t) => matchAny(t.url || "", p.matches));

    // 1. the tab we used last time for this site, if it is still that site
    const remembered = siteTabs.get(site);
    if (remembered != null && !p.new_tab) {
      const t = matching.find((x) => x.id === remembered);
      if (t) {
        if (!injectable(t)) await revive(t);
        if (p.activate !== false) await activate(t.id);
        await claim(site, t.id);
        return { tabId: t.id, created: false };
      }
      siteTabs.delete(site); saveSiteTabs();
    }

    // 2. any matching tab that nobody else has marked as theirs
    let tab = null;
    let skipped = 0;
    if (!p.new_tab) {
      for (const c of matching.filter(injectable).slice(0, 8)) {
        const own = await ownership(c.id);
        if (own.foreign) { skipped++; continue; }
        tab = c; break;
      }
      if (!tab) {
        for (const s of matching.filter((t) => t.discarded || t.status === "unloaded").slice(0, 3)) {
          if (p.activate !== false) await activate(s.id);
          await revive(s);
          const own = await ownership(s.id);
          if (own.foreign) { skipped++; continue; }
          tab = s; break;
        }
      }
    }

    // 3. every candidate belongs to someone else (or none existed) → open ours
    let created = false;
    if (!tab) {
      if (!p.home) {
        throw new Error(skipped
          ? `${p.site || "该站点"} 的 ${skipped} 个标签页都被别的工具占用了（页面上有 __cgo 标记），` +
            `而这个站点没配 home，无法另开一个——请指定 url`
          : `没有打开的 ${p.site || "该站点"} 标签页，请先打开它（或提供 url）`);
      }
      tab = await new Promise((r) => chrome.tabs.create({ url: p.home, active: !!p.activate }, r));
      created = true;
      ourTabs.add(tab.id);
      await waitComplete(tab.id); await sleep(800);
    }
    if (p.activate !== false) await activate(tab.id);
    await claim(site, tab.id);
    return { tabId: tab.id, created };
  }
  // fallback: the active tab
  const [act] = await q({ active: true, currentWindow: true });
  if (!act) throw new Error("没有可用的标签页");
  if (!injectable(act)) {
    throw new Error(`当前标签页无法注入（${(act.url || "").slice(0, 40)}）——` +
                    `请指定 --url/--site，或切到一个普通网页`);
  }
  return { tabId: act.id, created: false };
}

// Tabs this SW instance has already injected fresh scripts into. Cleared when
// the SW (re)starts — including after an extension reload — so the first command
// to each tab after a reload always injects the current code, never trusting a
// stale content script left over from the previous extension instance.
const injected = new Set();

// Tabs web-bridge itself opened. A dead URL used to leave an error tab behind
// and then *match* that tab on the next attempt (created === false), so retries
// piled up error tabs that never got cleaned. Remembering ours makes the cleanup
// cover the retry too, while never closing a tab the user opened.
const ourTabs = new Set();

function pingTab(tabId) {
  return new Promise((resolve) => {
    try {
      chrome.tabs.sendMessage(tabId, { type: "WB_CMD", cmd: { action: "ping", id: "ping" } }, (resp) => {
        resolve(chrome.runtime.lastError ? false : !!(resp && resp.acked));
      });
    } catch (_) { resolve(false); }
    setTimeout(() => resolve(false), 1200);
  });
}

// Chrome reports a failed navigation as a perfectly normal `complete` tab; the
// only tell is the injection error text. Translate it into what actually
// happened, so a bad URL doesn't read as a permissions/manifest problem.
function explainInjectFailure(err, tab) {
  const raw = String((err && err.message) || err);
  const url = (tab && tab.url) || "";
  if (/showing error page/i.test(raw)) {
    return `目标页面没有加载成功（打不开、被拦截或域名有误）：${url}\n` +
           `请确认这个 URL 在浏览器里能正常打开，或改用已打开标签页的 URL 片段`;
  }
  if (/cannot be scripted|must request permission|extensions gallery/i.test(raw)) {
    return `这个页面不允许扩展注入（浏览器内置页/应用商店/PDF 等）：${url}`;
  }
  return raw;
}

async function ensureScripts(tabId) {
  if (injected.has(tabId) && (await pingTab(tabId))) return; // known-live this SW session
  try {
    await chrome.scripting.executeScript({ target: { tabId }, world: "MAIN", files: ["content/page.js"] });
    await chrome.scripting.executeScript({ target: { tabId }, world: "ISOLATED", files: ["content/relay.js"] });
  } catch (e) {
    let tab = null, info = "";
    try {
      tab = await new Promise((r) => chrome.tabs.get(tabId, (x) => r(x)));
      info = ` [tab ${tabId} status=${tab && tab.status} discarded=${tab && tab.discarded}]`;
    } catch (_) { info = ` [tab ${tabId} 不可读]`; }
    const explained = explainInjectFailure(e, tab);
    const err = new Error(explained + info);
    if (explained !== String((e && e.message) || e)) err.wbFatal = true;  // page is dead, not a plumbing issue
    throw err;
  }
  injected.add(tabId);
  await sleep(400);
  await pingTab(tabId); // best effort; result still comes via runtime message
}

// Run user code in the page's MAIN world WITHOUT eval, via chrome.userScripts.
// The code is injected as a script (CSP 'unsafe-eval' does not apply); the last
// statement's value is returned through executeScript's result. Requires the
// browser's developer mode (already on for an unpacked extension).
async function execViaUserScripts(tabId, p) {
  if (!chrome.userScripts || typeof chrome.userScripts.execute !== "function") {
    // ONLY this case justifies the relay fallback (the browser's "Allow user
    // scripts" toggle is off). Every other failure is about the page or the
    // code, and must be reported as-is: falling back would run the same work
    // through `new Function`, which a Trusted-Types page (youtube.com) rejects
    // with "Evaluating a string as JavaScript violates…" — an error about the
    // fallback, masking the real one. That cost an hour of chasing the wrong bug.
    const e = new Error("chrome.userScripts.execute unavailable");
    e.wbUnavailable = true;
    throw e;
  }
  // The page's exception must come back as an exception. A rejected promise from
  // the injected async function surfaces as `result: null`, i.e. a capability
  // that failed looked exactly like one that found nothing — the single most
  // misleading failure mode in this whole path. So the wrapper catches its own
  // errors and returns them as data, and we re-throw on this side.
  const wrapped =
    "(async (args) => { try { return { __wbOk: true, value: await (async () => {\n" + p.code +
    "\n})() }; } catch (e) { return { __wbError: String((e && e.message) || e), " +
    "__wbStack: String((e && e.stack) || \"\").split(\"\\n\").slice(0, 3).join(\" | \") }; } })(" +
    JSON.stringify(p.args === undefined ? null : p.args) + ")";
  let results;
  try {
    results = await chrome.userScripts.execute({
      target: { tabId },
      js: [{ code: wrapped }],
      world: "MAIN",
      injectImmediately: true,
    });
  } catch (e) {
    const tab = await new Promise((r) => chrome.tabs.get(tabId, (x) => r(x))).catch(() => null);
    const explained = explainInjectFailure(e, tab);
    if (explained !== String((e && e.message) || e)) {
      // a dead/unscriptable page: the relay fallback would fail the same way,
      // so surface the real reason instead of a second confusing error
      const fatal = new Error(explained);
      fatal.wbFatal = true;
      throw fatal;
    }
    throw e;
  }
  const r = Array.isArray(results) ? results[0] : results;
  if (r && r.error) throw new Error(String(r.error.message || r.error));
  const out = r ? r.result : null;
  if (out && typeof out === "object" && out.__wbError) {
    const err = new Error(out.__wbError);
    err.wbPageStack = out.__wbStack;
    throw err;
  }
  // `__wbOk` unwraps the envelope; anything else came from the fallback path or
  // an older injection and is passed through untouched
  return { result: out && typeof out === "object" && out.__wbOk ? out.value : out };
}

async function injectAdapter(tabId, adapter) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, world: "MAIN", files: [`adapters/${adapter}.js`] });
    await sleep(150);
  } catch (e) {
    throw new Error(`加载适配器 ${adapter} 失败：` + ((e && e.message) || e));
  }
}

// --------------------------------------------------------------------------- //
// command dispatch
// --------------------------------------------------------------------------- //
async function handleCommand(msg) {
  const { id, action, payload } = msg;
  const p = payload || {};

  if (action === "tabs") {
    const all = await q({});
    const list = all
      .filter((t) => !p.filter || (t.url || "").includes(p.filter) || (t.title || "").includes(p.filter))
      .map((t) => ({ id: t.id, url: t.url, title: t.title, active: t.active, windowId: t.windowId }));
    send({ type: "result", id, ok: true, data: { tabs: list } });
    return;
  }
  if (action === "reload") {
    // reply first, then reload from disk (an unpacked extension re-reads its
    // source dir) — this lets an operator iterate on the code without touching
    // chrome://extensions after the initial one-time load.
    send({ type: "result", id, ok: true, data: { reloading: true } });
    setTimeout(() => { try { chrome.runtime.reload(); } catch (_) {} }, 400);
    return;
  }
  if (action === "close") {
    // Close tabs by id or URL substring. Automation that opens a tab should be
    // able to put it away again; without this every scripted run leaked a tab.
    const all = await q({});
    const victims = all.filter((t) =>
      (p.tab_id && t.id === p.tab_id) || (p.url && (t.url || "").includes(p.url)));
    for (const t of victims) {
      try { await new Promise((r) => chrome.tabs.remove(t.id, () => r())); } catch (_) {}
      ourTabs.delete(t.id); injected.delete(t.id);
    }
    send({ type: "result", id, ok: true,
           data: { closed: victims.map((t) => ({ id: t.id, url: t.url, title: t.title })) } });
    return;
  }
  if (action === "open") {
    const all = await q({});
    let tab = p.reuse ? all.find((t) => (t.url || "").includes(p.url)) : null;
    if (!tab) tab = await new Promise((r) => chrome.tabs.create({ url: p.url, active: !!p.activate }, r));
    else if (p.activate) await activate(tab.id);
    send({ type: "result", id, ok: true, data: { tabId: tab.id, url: tab.url || p.url } });
    return;
  }

  // exec / adapter / ping / adapters — all need a resolved, script-ready tab
  const { tabId, created } = await resolveTab(p);
  // a tab we opened for a page that turns out to be unusable gets closed again
  const cleanup = (e) => {
    if ((created || ourTabs.has(tabId)) && e && e.wbFatal) {
      ourTabs.delete(tabId);
      try { chrome.tabs.remove(tabId); } catch (_) {}
    }
    throw e;
  };

  // Fast path for exec: chrome.userScripts INJECTS the code as a script instead
  // of eval()-ing a string inside the page, so it works on sites whose CSP omits
  // 'unsafe-eval' (chatgpt.com et al) — where the postMessage+new Function path
  // is blocked. Falls back to the relay path if userScripts is unavailable
  // (needs developer mode) or errors.
  if (action === "exec") {
    try {
      const data = await execViaUserScripts(tabId, p);
      data.tab_url = await tabUrl(tabId);   // the REAL page, not the caller's fragment
      send({ type: "result", id, ok: true, data });
      return;
    } catch (e) {
      if (e && e.wbFatal) cleanup(e);
      if (!e || !e.wbUnavailable) throw e;      // a real page/code error — report it
      console.warn("[web-bridge] userScripts unavailable, falling back to relay:", (e && e.message) || e);
    }
  }

  // Adapters take the same userScripts route. They used to go through the
  // ISOLATED relay and answer via a runtime message — but the relay can be gone
  // (page navigations drop it, and it is not re-injected while the MAIN-world
  // half still answers pings), and then the result never came back at all: the
  // work completed in the page and the caller just timed out. Returning through
  // userScripts keeps the reply on the same channel as the request.
  if (action === "adapter" && p.adapter) {
    try {
      // new_chat means "start a fresh conversation": navigate the tab to the
      // site's home before injecting. Without this the ask piles onto whatever
      // conversation happened to be open, so context leaks between unrelated
      // calls (the parameter used to be silently ignored).
      if (p.params && p.params.new_chat && p.home) {
        // Safe now only because resolveTab guaranteed this tab is ours or
        // unclaimed: this navigation used to land on whatever chatgpt.com tab
        // matched first, which is how it walked off with another tool's session.
        await new Promise((r) => chrome.tabs.update(tabId, { url: p.home }, () => r()));
        await waitComplete(tabId);
        await sleep(1200);
        await claim(p.site, tabId);          // navigation wiped the marker
      }
      await injectAdapter(tabId, p.adapter);
      const data = await execViaUserScripts(tabId, {
        args: { site: p.site, method: p.method, params: p.params || {} },
        timeout_ms: p.timeout_ms,
        code: `
          const a = (window.__webBridge && window.__webBridge.adapters || {})[args.site];
          if (!a) throw new Error("适配器 " + args.site + " 未注册");
          const fn = a[args.method];
          if (typeof fn !== "function") throw new Error("适配器无方法 " + args.method);
          return await fn(args.params || {}, {});
        `,
      });
      data.tab_url = await tabUrl(tabId);
      send({ type: "result", id, ok: true, data });
      return;
    } catch (e) {
      if (e && e.wbFatal) cleanup(e);
      if (!e || !e.wbUnavailable) throw e;
      console.warn("[web-bridge] userScripts unavailable, adapter falls back to relay:", (e && e.message) || e);
    }
  }

  // ---- fallback path: relay (ISOLATED) -> page.js (MAIN) via postMessage ----
  // Only reached when chrome.userScripts is unavailable — it needs the browser's
  // "Allow user scripts" toggle, which a fresh profile has off. Keep it working,
  // but know its weakness: the reply comes back as a runtime message through the
  // relay, and if the relay is gone (page navigated, MAIN half still answering
  // pings so no re-injection happens) the result silently never arrives.
  await ensureScripts(tabId).catch(cleanup);
  if (action === "adapter" && p.adapter) await injectAdapter(tabId, p.adapter);

  const cmd = { action, id, ...p };
  const acked = await new Promise((resolve) => {
    try {
      chrome.tabs.sendMessage(tabId, { type: "WB_CMD", cmd }, (resp) => {
        resolve(chrome.runtime.lastError ? false : !!(resp && resp.acked));
      });
    } catch (_) { resolve(false); }
    setTimeout(() => resolve(false), 1500);
  });
  if (!acked) {
    // The relay never acked, so nothing will ever answer. Fail loudly now
    // instead of letting the caller sit until its timeout.
    throw new Error("内容脚本无响应（relay 未确认）——请刷新目标页面后重试");
  }
}

// content relay -> SW -> bridge
chrome.runtime.onMessage.addListener((msg) => {
  if (!msg) return false;
  if (msg.type === "WB_RESULT") {
    send({ type: "result", id: msg.id, ok: !!msg.ok, data: msg.data, error: msg.error });
  } else if (msg.type === "WB_PROGRESS") {
    send({ type: "progress", id: msg.id, stage: msg.stage });
  }
  return false;
});

// keepalive
try {
  chrome.alarms.create("wb-keepalive", { periodInMinutes: 0.5 });
  chrome.alarms.onAlarm.addListener((a) => {
    if (a.name !== "wb-keepalive") return;
    if (!ws || ws.readyState > WebSocket.OPEN) connect();
    else send({ type: "ping", t: Date.now() });
  });
} catch (_) {}

try {
  chrome.tabs.onRemoved.addListener((tabId) => {
    injected.delete(tabId); ourTabs.delete(tabId);
    for (const [site, id] of siteTabs) if (id === tabId) siteTabs.delete(site);
    saveSiteTabs();
  });
  // a navigated/reloaded tab drops our content scripts → force re-inject next time
  chrome.tabs.onUpdated.addListener((tabId, info) => { if (info.status === "loading") injected.delete(tabId); });
} catch (_) {}

// --------------------------------------------------------------------------- //
// side panel + autorun scripts
// --------------------------------------------------------------------------- //
// Clicking the toolbar icon opens the panel. setPanelBehavior is the supported
// way to do this — an onClicked listener never fires once a side panel is
// declared, which looks exactly like a broken extension.
try {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true })
    .catch((e) => console.warn("[web-bridge] setPanelBehavior:", e));
} catch (_) {}

// Scripts marked `autorun` in the capability library run on page load. They live
// on the bridge (one source of truth for every surface), so the SW pulls them
// and registers them with chrome.userScripts — at page-load time there is no
// round trip to ask.
const AUTORUN_PREFIX = "wb-auto-";

async function bridgeGet(path) {
  const base = BRIDGE_WS.replace(/^ws:/, "http:").replace(/\/ws\/ext$/, "");
  const r = await fetch(base + path, { headers: { Authorization: "Bearer " + BRIDGE_TOKEN } });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

async function syncAutorun() {
  if (!chrome.userScripts) return { ok: false, error: "userScripts 不可用（需在扩展详情页开启「允许用户脚本」）" };
  let scripts = [];
  try {
    scripts = (await bridgeGet("/capabilities/autorun")).scripts || [];
  } catch (e) {
    return { ok: false, error: "读不到 bridge：" + ((e && e.message) || e) };
  }
  try {
    const existing = await chrome.userScripts.getScripts();
    const ours = existing.filter((s) => s.id.startsWith(AUTORUN_PREFIX)).map((s) => s.id);
    if (ours.length) await chrome.userScripts.unregister({ ids: ours });
  } catch (_) {}
  if (!scripts.length) return { ok: true, registered: 0 };

  // Wrapped the same way exec is: the body is a function body, so `return` and
  // top-level await work exactly as they do everywhere else in this project.
  const regs = scripts.map((s) => ({
    id: AUTORUN_PREFIX + s.id,
    matches: s.matches,
    js: [{ code: `(async (args) => {\n${s.code}\n})({}).catch(e => console.warn("[web-bridge] ${s.id}:", e));` }],
    world: "MAIN",
    runAt: "document_idle",
  }));
  try {
    await chrome.userScripts.register(regs);
    return { ok: true, registered: regs.length };
  } catch (e) {
    // Registration is all-or-nothing: one bad match pattern would silently take
    // down every other script, so fall back to one at a time.
    let n = 0;
    const failed = [];
    for (const reg of regs) {
      try { await chrome.userScripts.register([reg]); n++; }
      catch (e2) { failed.push(reg.id + ": " + ((e2 && e2.message) || e2)); }
    }
    return { ok: true, registered: n, failed };
  }
}

// The panel asks for things the bridge cannot do for it: which tab is active,
// and re-registering autorun scripts after an edit.
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (!msg || msg.type !== "WB_PANEL") return false;
  if (msg.action === "sync-autorun") {
    syncAutorun().then(reply);
    return true;                       // async reply
  }
  if (msg.action === "ping") {
    reply({ ok: true, connected: !!(ws && ws.readyState === WebSocket.OPEN) });
    return false;
  }
  return false;
});

connect();
syncAutorun().then((r) => console.log("[web-bridge] autorun sync", r));
console.log("[web-bridge] service worker loaded; connecting to", BRIDGE_WS);
