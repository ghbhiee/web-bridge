// adapters/chatgpt.js — MAIN-world site adapter for chatgpt.com.
//
// Injected into the page's MAIN world by the SW before a chatgpt command; it
// self-registers into window.__webBridge.adapters.chatgpt. Methods:
//   ask({prompt, files, new_chat, want_images, allow_guest}) -> {text, html, images, conversation_url}
//   status() -> {logged_in, guest, account, url, ready}
//
// DOM facts verified against live chatgpt.com (ported from the proven
// chatgpt-bridge content script). Completion signal = the turn's action toolbar
// (copy button) mounting, NOT stop-button disappearance.

(() => {
  const PAGE = window.__webBridge;
  if (!PAGE || !PAGE.registerAdapter) return; // page.js not present yet
  // Deliberately NO "already registered" guard: the SW re-injects this file on
  // every adapter call, and after an extension reload the page still holds the
  // previous version. Latest injection must win, or fixes silently never apply
  // (this exact guard once kept a stale, hanging upload loop alive across
  // reloads and cost an hour of misdiagnosis).

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const now = () => Date.now();

  const editorEl = () => document.querySelector("#prompt-textarea");
  const sendBtn = () =>
    document.querySelector('[data-testid="send-button"]') ||
    [...document.querySelectorAll("button")].find((b) => /^send/i.test(b.getAttribute("aria-label") || ""));
  const stopBtn = () => document.querySelector('[data-testid="stop-button"]');
  const turnEls = () => [...document.querySelectorAll('[data-testid^="conversation-turn"]')];
  const latestTurn = () => { const t = turnEls(); return t[t.length - 1] || null; };
  const copyBtnIn = (turn) => (turn ? turn.querySelector('[data-testid="copy-turn-action-button"]') : null);
  const composerReady = () => !!editorEl();

  async function accountInfo() {
    try {
      const r = await fetch("/api/auth/session", { credentials: "include", headers: { accept: "application/json" } });
      if (!r.ok) return { loggedIn: false, guest: false, unknown: true, reason: "http_" + r.status };
      const j = await r.json().catch(() => null);
      const email = (j && j.user && j.user.email) || "";
      return { loggedIn: !!email, guest: !email, email, name: (j && j.user && j.user.name) || "" };
    } catch (e) {
      return { loggedIn: composerReady(), guest: false, unknown: true, err: String(e) };
    }
  }

  function setPromptText(text) {
    const ed = editorEl();
    if (!ed) throw new Error("找不到输入框（可能未登录或页面未加载完）");
    ed.focus();
    document.execCommand("selectAll", false, null);
    document.execCommand("insertText", false, text);
    ed.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function b64ToFile(b64, name, mime) {
    const bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], name, { type: mime || "application/octet-stream" });
  }
  function fileInputFor(files) {
    const inputs = [...document.querySelectorAll('input[type="file"]')];
    if (!inputs.length) return null;
    const allImages = files.every((f) => (f.mime || "").startsWith("image/"));
    if (!allImages) {
      const generic = inputs.find((i) => !i.accept || (!i.accept.includes("image") && i.accept !== "image/*"));
      if (generic) return generic;
    }
    return inputs[0];
  }
  async function uploadFiles(files, deadline) {
    if (!files || !files.length) return;
    const input = fileInputFor(files);
    if (!input) throw new Error("页面上找不到文件上传 input");
    const dt = new DataTransfer();
    for (const f of files) dt.items.add(b64ToFile(f.b64, f.name, f.mime));
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
    // Wait for the attachment chips to render and any upload spinner to clear.
    // Bounded independently of the overall deadline: a stuck predicate here used
    // to silently eat the whole budget, so the prompt was typed but never sent.
    const uploadDeadline = Math.min(deadline, now() + 45000);
    let settled = 0;
    let shown = false;
    while (now() < uploadDeadline) {
      await sleep(500);
      const form = document.querySelector("form");
      const txt = form ? form.innerText : "";
      const allShown = files.every((f) => txt.includes(f.name) || txt.includes(f.name.slice(0, 18)));
      if (allShown) shown = true;
      const spinning = !!document.querySelector('form [role="progressbar"], form .animate-spin');
      if (allShown && !spinning) { if (++settled >= 2) return; } else settled = 0;
    }
    // Timed out waiting. If the chips did appear the upload is almost certainly
    // fine (a lingering spinner elsewhere in the form is common) — carry on and
    // let the send step decide. Only a total no-show is a real failure.
    if (!shown) {
      throw new Error("附件上传超时：未看到附件出现在输入框（文件可能过大或被拒绝）");
    }
  }

  function extractText(turn) {
    if (!turn) return "";
    const am =
      turn.querySelector('[data-message-author-role="assistant"] .markdown') ||
      [...turn.querySelectorAll(".markdown")].pop() ||
      turn.querySelector('[data-message-author-role="assistant"]');
    return (am ? am.innerText || am.textContent || "" : "").trim();
  }
  function answerSignature(turn) {
    if (!turn) return "none";
    const am = turn.querySelector('[data-message-author-role="assistant"]') || turn;
    const len = (am.textContent || "").length;
    const imgs = [...turn.querySelectorAll("img")].filter((im) => im.naturalWidth >= 64);
    return `${len}|${imgs.length}|` + imgs.map((im) => `${im.naturalWidth}x${im.naturalHeight}`).join(",");
  }
  const isGen = (src) => {
    try {
      const h = new URL(src, location.href).hostname;
      return h === "chatgpt.com" || h.endsWith(".chatgpt.com") || h.endsWith("oaiusercontent.com");
    } catch (_) { return false; }
  };
  function abToB64(buf) {
    let s = ""; const b = new Uint8Array(buf), c = 0x8000;
    for (let i = 0; i < b.length; i += c) s += String.fromCharCode.apply(null, b.subarray(i, i + c));
    return btoa(s);
  }
  async function extractImages(el, deadline) {
    if (!el) return [];
    const pick = () => {
      const seen = new Set(), out = [];
      for (const im of el.querySelectorAll("img")) {
        if (im.naturalWidth < 256 || !isGen(im.src) || im.src.startsWith("data:image/svg")) continue;
        let key; try { key = new URL(im.src, location.href).pathname; } catch (_) { key = im.src; }
        if (seen.has(key)) continue;
        seen.add(key); out.push(im);
      }
      return out;
    };
    let imgs = pick();
    while (!imgs.length && now() < deadline) { await sleep(500); imgs = pick(); }
    if (imgs.length) {
      let last = imgs.length, stableUntil = now() + 8000;
      while (now() < deadline && now() < stableUntil) {
        await sleep(800);
        const cur = pick();
        if (cur.length > last) { last = cur.length; stableUntil = now() + 8000; }
        imgs = cur;
      }
    }
    const out = [];
    for (const im of imgs) {
      let b64 = null, mime = "image/png";
      try {
        const r = await fetch(im.src);
        if (r.ok) { mime = r.headers.get("content-type") || mime; b64 = abToB64(await r.arrayBuffer()); }
      } catch (_) {}
      if (!b64) {
        try {
          const c = document.createElement("canvas");
          c.width = im.naturalWidth; c.height = im.naturalHeight;
          c.getContext("2d").drawImage(im, 0, 0);
          const durl = c.toDataURL("image/png");
          b64 = durl.slice(durl.indexOf(",") + 1);
        } catch (_) {}
      }
      if (b64) out.push({ b64, mime, width: im.naturalWidth, height: im.naturalHeight, alt: im.alt || "" });
    }
    return out;
  }

  async function waitForCompletion(beforeTurns, deadline, onBeat) {
    let lastBeat = now();
    const beat = () => { if (onBeat && now() - lastBeat > 8000) { onBeat("generating"); lastBeat = now(); } };
    // Did the message actually go out? Three independent signals, because with
    // an attachment the first click is sometimes swallowed while the file is
    // still being processed server-side: the composer emptying is the most
    // reliable one (the turn list can lag or be virtualised).
    const composerEmpty = () => {
      const ed = editorEl();
      return !!ed && (ed.innerText || "").trim().length === 0;
    };
    const startedNow = () => stopBtn() || turnEls().length > beforeTurns || composerEmpty();

    let started = false;
    let retries = 0;
    let nextRetryAt = now() + 8000;
    while (now() < deadline) {
      await sleep(250); beat();
      if (startedNow()) { started = true; break; }
      // nothing happened for 8s and the text is still sitting in the composer →
      // the click was swallowed; press send again (bounded retries).
      if (now() > nextRetryAt && retries < 2) {
        const b = sendBtn();
        if (b) { b.click(); retries++; }
        nextRetryAt = now() + 8000;
      }
    }
    if (!started) {
      throw new Error("发送后未检测到回复开始（重试 " + retries + " 次仍未提交；可能被限流或需要验证）");
    }
    const genImgLoaded = (turn) =>
      turn && [...turn.querySelectorAll("img")].some((im) => im.naturalWidth >= 256 && isGen(im.src));
    const isDone = () => !stopBtn() && (!!copyBtnIn(latestTurn()) || genImgLoaded(latestTurn()));
    let confirm = 0, prevSig = null, unchangedSince = now();
    while (now() < deadline) {
      await sleep(400); beat();
      const sig = answerSignature(latestTurn());
      if (sig !== prevSig) { prevSig = sig; unchangedSince = now(); }
      if (isDone()) { if (++confirm >= 2) return; } else confirm = 0;
      if (!stopBtn() && !copyBtnIn(latestTurn()) && sig !== "none" && now() - unchangedSince > 15000) return;
    }
  }

  async function ask(params, ctx) {
    const onBeat = (ctx && ctx.onProgress) || (() => {});
    const stage = (s) => { window.__wbAskStage = s + " @" + new Date().toISOString().slice(11, 19); onBeat(s); };
    stage("start");
    if (!composerReady()) throw new Error("chatgpt.com 页面未就绪（未加载完或被拦截）");
    if (!params.allow_guest) {
      const acct = await accountInfo();
      if (acct.guest) throw new Error("当前是未登录的访客会话(guest)，请用已登录账号打开 chatgpt.com（或传 allow_guest）");
    }
    const deadline = now() + (params.deadline_ms || 240000);
    if (params.files && params.files.length) {
      stage("uploading");
      await uploadFiles(params.files, Math.min(deadline, now() + 90000));
      stage("uploaded");
    }
    stage("typing");
    setPromptText(params.prompt || "");
    stage("await-send-button");
    let btn = null;
    while (now() < deadline) { await sleep(150); btn = sendBtn(); if (btn && !btn.disabled) break; }
    if (!btn) throw new Error("发送按钮未出现（输入未生效或附件还在上传）");
    const beforeTurns = turnEls().length;
    stage("clicking-send");
    btn.click();
    stage("awaiting-answer");
    await waitForCompletion(beforeTurns, deadline, onBeat);
    const turn = latestTurn();
    const text = extractText(turn);
    let images = [];
    if (params.want_images || (turn && turn.querySelector("img"))) {
      images = await extractImages(turn, Math.min(deadline, now() + 360000));
      if (!images.length && params.want_images) {
        const all = turnEls();
        images = await extractImages(all.length ? all[0].parentElement || document.body : document.body,
                                     Math.min(deadline, now() + 120000));
      }
    }
    const md = turn && (turn.querySelector('[data-message-author-role="assistant"] .markdown') ||
      [...turn.querySelectorAll(".markdown")].pop());
    return { text, html: md ? (md.innerHTML || "").slice(0, 200000) : "", images,
             conversation_url: location.href, title: document.title };
  }

  async function status() {
    const a = await accountInfo();
    return { logged_in: a.loggedIn, guest: a.guest, account: a.email || null, url: location.href, ready: composerReady() };
  }

  // ------------------------------------------------------------------------ //
  // last() — re-read the answer that is already on the page.
  //
  // The point of this method is that a finished answer must never be lost just
  // because the transport failed. It sends nothing and costs no quota: it reads
  // the conversation ChatGPT already stored.
  //
  // Four facts make this non-obvious, all learned by measurement (see the notes
  // in ~/cc/chatgpt-osascript):
  //   1. Walk the tree from `current_node` up `parent`. Do NOT sort by
  //      create_time — ChatGPT stamps the *user* message later than the reply
  //      it triggered, so a time sort inverts Q and A and "everything after my
  //      prompt" comes back empty.
  //   2. Generated images are NOT in the assistant message. They arrive as
  //      `role:"tool"` messages whose parts contain image_asset_pointer objects.
  //   3. The download URL is same-origin and cookie-gated, so the bytes must be
  //      fetched from inside the page; an external download gets 403.
  //   4. /backend-api/conversation/{id} rate-limits hard (1/s polling earns 50+
  //      429s and a penalty window of minutes). So: read it ONCE, and back off
  //      exponentially on 429 instead of retrying in a tight loop.
  // ------------------------------------------------------------------------ //
  let _tok = null, _tokExp = 0;
  async function accessToken() {
    if (_tok && _tokExp > now() + 60000) return _tok;
    const r = await fetch("/api/auth/session", { credentials: "include", headers: { accept: "application/json" } });
    if (!r.ok) throw new Error("拿不到登录会话（/api/auth/session http " + r.status + "）");
    const j = await r.json().catch(() => ({}));
    _tok = j.accessToken || null;
    _tokExp = Date.parse(j.expires || "") || now() + 600000;
    if (!_tok) throw new Error("拿不到 accessToken（未登录？）");
    return _tok;
  }

  const convIdOf = (href) => ((href || location.href).match(/\/c\/([0-9a-f-]+)/) || [])[1] || null;

  async function apiGet(path, tries = 3) {
    let waitMs = 5000;
    for (let i = 0; i < tries; i++) {
      const r = await fetch(path, {
        credentials: "include",
        headers: { Authorization: "Bearer " + (await accessToken()), accept: "application/json" },
      });
      if (r.ok) return await r.json();
      if (r.status === 429 && i < tries - 1) {
        // exponential, never a retry storm: this endpoint's penalty window
        // outlasts the answer if you hammer it
        await sleep(waitMs);
        waitMs *= 3;
        continue;
      }
      throw new Error("conversation http " + r.status + (r.status === 429 ? "（被限流，稍后再试）" : ""));
    }
    throw new Error("conversation http 429（重试后仍被限流）");
  }

  // current_node -> parent -> ... , unshifted so the result reads chronologically
  function branchOf(conv) {
    const map = conv.mapping || {};
    const out = [];
    const seen = new Set();
    let node = conv.current_node;
    while (node && map[node] && !seen.has(node)) {
      seen.add(node);
      const m = map[node].message;
      if (m && m.author && m.content) {
        out.unshift({
          id: m.id, role: m.author.role, status: m.status, end_turn: m.end_turn,
          ct: m.content.content_type, parts: m.content.parts || [],
        });
      }
      node = map[node].parent;
    }
    return out;
  }

  const partsToText = (parts) =>
    (parts || []).map((p) => (typeof p === "string" ? p : "")).join("").trim();
  const partsToPointers = (parts) =>
    (parts || []).filter((p) => p && typeof p === "object" && p.content_type === "image_asset_pointer")
                 .map((p) => ({ ptr: p.asset_pointer, width: p.width, height: p.height }));

  // "file-service://file-ABC" / "sediment://file-ABC" -> "file-ABC"
  const assetId = (ptr) => String(ptr).split("://").pop().split("?")[0];

  async function downloadAsset(ptr) {
    const fid = assetId(ptr);
    const meta = await apiGet("/backend-api/files/" + encodeURIComponent(fid) + "/download", 2);
    const url = meta && meta.download_url;
    if (!url) throw new Error("没有 download_url（file " + fid + "）");
    const r = await fetch(url);            // same-origin + cookies: must be in-page
    if (!r.ok) throw new Error("下载图片失败 http " + r.status);
    const mime = r.headers.get("content-type") || "image/png";
    return { b64: abToB64(await r.arrayBuffer()), mime, name: meta.file_name || null, id: fid };
  }

  async function last(params) {
    const cid = params.conversation_id || convIdOf(params.url);
    if (!cid) {
      throw new Error("当前 chatgpt.com 标签页不在某个会话里（URL 里没有 /c/<id>）——" +
                      "先切到那个会话的标签页，或传 conversation_id");
    }
    const conv = await apiGet("/backend-api/conversation/" + encodeURIComponent(cid));
    const msgs = branchOf(conv);
    let lastUser = -1;
    for (let i = 0; i < msgs.length; i++) if (msgs[i].role === "user") lastUser = i;
    const after = msgs.slice(lastUser + 1);
    // allowlist, not a blocklist: anything that is not text/multimodal_text
    // (thoughts, reasoning_recap, …) must never leak into the answer
    const said = after.filter((m) => m.role === "assistant" && (m.ct === "text" || m.ct === "multimodal_text"));
    let text = "";
    for (const m of said) {
      const t = partsToText(m.parts);
      if (t) text += (text ? "\n\n" : "") + t;
    }
    // One generated picture shows up twice: once in the role:"tool" message and
    // again in the assistant's multimodal_text. Measured on a real image turn —
    // without this, every reclaimed image is saved to disk twice.
    const pointers = [];
    const seenAssets = new Set();
    for (const m of after) {
      for (const p of partsToPointers(m.parts)) {
        const key = assetId(p.ptr);
        if (seenAssets.has(key)) continue;
        seenAssets.add(key);
        pointers.push(p);
      }
    }

    const images = [];
    const errors = [];
    if (params.want_images !== false) {
      for (const p of pointers) {
        try {
          const img = await downloadAsset(p.ptr);
          images.push({ ...img, width: p.width, height: p.height });
        } catch (e) {
          errors.push(String((e && e.message) || e));
        }
      }
    }
    return {
      text, images, conversation_url: location.origin + "/c/" + cid, cid,
      title: conv.title || document.title,
      done: said.some((m) => m.end_turn === true && m.status === "finished_successfully"),
      image_pointers: pointers.length,
      pending: after.filter((m) => m.status !== "finished_successfully").length,
      prompt: lastUser >= 0 ? partsToText(msgs[lastUser].parts).slice(0, 120) : "",
      errors: errors.length ? errors : undefined,
    };
  }

  // Let other tools see that this tab is spoken for — the same courtesy
  // chatgpt-osascript's window.__cgo asks of us.
  try {
    window.__webBridgeOwned = true;
    if (document.body) document.body.dataset.wbOwned = "1";
  } catch (_) {}

  PAGE.registerAdapter("chatgpt", { ask, status, last });
})();
