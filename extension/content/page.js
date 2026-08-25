// page.js — MAIN world content script.
//
// Runs in the page's own JS world, so it can see page globals, call page
// functions, read framework state, and use fetch with the page's cookies —
// and, being extension-injected, it is exempt from the page CSP.
//
// It has NO chrome.* APIs (those live in the ISOLATED world). It talks to the
// ISOLATED relay (content/relay.js) purely via window.postMessage, gated by a
// per-page nonce the relay hands over during a handshake.
//
// Responsibilities:
//   * exec: run an arbitrary function body `(args) => { ... }` and return its
//     (JSON-safe) value — the generic primitive.
//   * adapters: a registry of site modules (adapters/*.js) that register
//     higher-level methods on window.__webBridge.adapters[name].

(() => {
  // Latest injection wins. We do NOT skip when an instance already exists: after
  // an extension reload the old MAIN-world script lingers (orphaned) with a dead
  // chrome side, so a same-version skip would keep running stale code. Instead
  // each injection supersedes the previous one; the old message handler bails
  // via the identity check (window.__webBridgePage !== PAGE). ensureScripts only
  // re-injects when its ping fails (fresh tab or post-reload), so healthy pages
  // are not needlessly re-inited.
  const PAGE = (window.__webBridgePage = {
    nonce: null,
    adapters: Object.create(null),
    registerAdapter(name, methods) {
      this.adapters[name] = methods;
      post({ __wb: "adapter-registered", nonce: this.nonce, name });
    },
  });
  // expose a stable global for adapter files to self-register into
  window.__webBridge = PAGE;

  const post = (m) => window.postMessage(m, window.location.origin);

  function jsonSafe(v) {
    try {
      return JSON.parse(JSON.stringify(v === undefined ? null : v));
    } catch (_) {
      try {
        return String(v);
      } catch (_e) {
        return null;
      }
    }
  }

  async function runExec(cmd) {
    // cmd.code is a function body; it receives `args` and may return / await.
    const fn = new Function(
      "args",
      "helpers",
      `return (async () => { ${cmd.code} })();`
    );
    const helpers = {
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
      $: (s, root = document) => (root || document).querySelector(s),
      $$: (s, root = document) => [...(root || document).querySelectorAll(s)],
      bridge: PAGE,
    };
    const out = await fn(cmd.args, helpers);
    return jsonSafe(out);
  }

  async function runAdapter(cmd) {
    const a = PAGE.adapters[cmd.site];
    if (!a) throw new Error(`no adapter registered for site "${cmd.site}" on this page`);
    const m = a[cmd.method];
    if (typeof m !== "function") throw new Error(`adapter "${cmd.site}" has no method "${cmd.method}"`);
    const onProgress = (stage) => post({ __wb: "progress", nonce: PAGE.nonce, id: cmd.id, stage });
    const out = await m(cmd.params || {}, { onProgress, ...cmd });
    return jsonSafe(out);
  }

  async function handle(cmd) {
    switch (cmd.action) {
      case "exec":
        return { result: await runExec(cmd) };
      case "adapter":
        return { result: await runAdapter(cmd) };
      case "adapters":
        return { adapters: Object.keys(PAGE.adapters) };
      case "ping":
        return { ready: true, url: location.href, title: document.title };
      default:
        throw new Error("unknown action: " + cmd.action);
    }
  }

  window.addEventListener("message", (ev) => {
    if (window.__webBridgePage !== PAGE) return; // superseded by a newer injection
    if (ev.source !== window) return;
    const m = ev.data;
    if (!m || typeof m !== "object") return;

    if (m.__wb === "hello") {
      // relay handed us the channel nonce
      PAGE.nonce = m.nonce;
      post({ __wb: "main-hello", nonce: PAGE.nonce, adapters: Object.keys(PAGE.adapters) });
      return;
    }
    if (m.__wb !== "cmd") return;
    if (!PAGE.nonce || m.nonce !== PAGE.nonce) return; // reject forged/foreign

    handle(m)
      .then((data) => post({ __wb: "res", nonce: PAGE.nonce, id: m.id, ok: true, data }))
      .catch((e) => post({ __wb: "res", nonce: PAGE.nonce, id: m.id, ok: false, error: String((e && e.message) || e) }));
  });

  // announce MAIN readiness so the relay (whichever loaded first) sends hello
  post({ __wb: "main-ready" });
})();
