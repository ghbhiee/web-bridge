// panel.js — the side panel: agent chat, script library, page tools.
//
// The panel owns no logic of its own: every action goes through the local bridge
// on the same routes the CLI and MCP use (/agent/ask, /capability/{id}, /exec,
// /journal). That is deliberate — validation, the sensitive-site blocklist, tab
// resolution and the exec journal all live in one place, and a second
// implementation here would quietly drift from it.

import { BRIDGE_WS, BRIDGE_TOKEN } from "../config.js";

const BASE = BRIDGE_WS.replace(/^ws:/, "http:").replace(/\/ws\/ext$/, "");
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const KIND_LABEL = { extract: "抽取", automate: "自动化", restyle: "美化", inspect: "探查", other: "其它" };

const state = {
  tab: null,          // the browser tab the panel is looking at
  caps: [],
  filterSite: true,
  editing: null,      // capability id being edited, or "" for a new one
  context: null,      // page content pulled into the chat
  run: null,          // live agent run id
  session: null,      // agent session to continue, so follow-ups keep context
  agents: { default: "", runners: {} },
  wantAgent: null,    // agent picked last time the panel was open
  pendingRun: null,   // a run that was still going when the panel closed
};

// --------------------------------------------------------------------------- //
// bridge plumbing
// --------------------------------------------------------------------------- //
async function api(path, opts = {}) {
  const r = await fetch(BASE + path, {
    ...opts,
    headers: {
      Authorization: "Bearer " + BRIDGE_TOKEN,
      ...(opts.body ? { "Content-Type": "application/json" } : {}),
      ...(opts.headers || {}),
    },
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.error || "HTTP " + r.status);
  return data;
}

function toast(text, ms = 1600) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

async function currentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab || null;
}

// --------------------------------------------------------------------------- //
// persistence
// --------------------------------------------------------------------------- //
// A side panel is torn down every time it closes (switch window, click away),
// taking the conversation with it. Chat is the one thing here that is expensive
// to recreate — the agent already spent time and quota on it — so the transcript,
// the chosen agent, and the session id survive in chrome.storage.local.
const STORE_KEY = "wb_panel_state";
const MAX_KEPT = 40;

async function persist() {
  const turns = [...$("messages").querySelectorAll(".msg")].slice(-MAX_KEPT).map((m) => ({
    cls: m.className.replace("msg ", ""),
    html: m.innerHTML,
  }));
  try {
    await chrome.storage.local.set({
      [STORE_KEY]: { turns, session: state.session, agent: $("agent-pick").value,
                     run: state.run },
    });
  } catch (_) {}
}

async function restore() {
  let saved;
  try {
    saved = (await chrome.storage.local.get(STORE_KEY))[STORE_KEY];
  } catch (_) { return; }
  if (!saved || !(saved.turns || []).length) return;
  state.session = saved.session || null;
  $("messages").innerHTML = "";
  for (const t of saved.turns) {
    const el = document.createElement("div");
    el.className = "msg " + t.cls;
    el.innerHTML = t.html;
    $("messages").appendChild(el);
  }
  // Buttons rendered into restored HTML have no listeners any more; rather than
  // resurrect half-working ones, mark the transcript as history.
  $("messages").querySelectorAll(".save-code").forEach((b) => b.remove());
  $("messages").scrollTop = $("messages").scrollHeight;
  if (saved.agent) state.wantAgent = saved.agent;
  if (saved.run) state.pendingRun = saved.run;   // picked up after boot
}

// --------------------------------------------------------------------------- //
// tabs (panel navigation)
// --------------------------------------------------------------------------- //
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".pane").forEach((p) =>
      p.classList.toggle("active", p.id === "pane-" + btn.dataset.tab));
    if (btn.dataset.tab === "scripts") loadScripts();
    if (btn.dataset.tab === "page") loadPage();
  });
});

// --------------------------------------------------------------------------- //
// chat
// --------------------------------------------------------------------------- //
function addMsg(cls, html) {
  const hello = $("messages").querySelector(".hello");
  if (hello) hello.remove();
  const el = document.createElement("div");
  el.className = "msg " + cls;
  el.innerHTML = html;
  $("messages").appendChild(el);
  $("messages").scrollTop = $("messages").scrollHeight;
  return el;
}

// Agent answers are markdown; showing them as raw text meant every code block
// arrived as ``` fences. Rendered here rather than with a library because the
// panel has a strict CSP and this needs to stay dependency-free.
//
// SECURITY: the input is agent output, which may quote a web page. Everything is
// HTML-escaped FIRST, then a fixed set of inline patterns is re-introduced — so
// no path exists from page text to live markup.
function renderMarkdown(src) {
  const blocks = [];
  let text = esc(src || "");
  // fenced code first, stashed so its contents are never touched by inline rules
  text = text.replace(/```([a-zA-Z0-9]*)\n([\s\S]*?)```/g, (_m, lang, code) => {
    blocks.push(`<pre><code data-lang="${esc(lang)}">${code.replace(/\n$/, "")}</code></pre>`);
    return `\u0000B${blocks.length - 1}\u0000`;
  });
  text = text
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|\n)#{1,6}\s*([^\n]+)/g, "$1<h3>$2</h3>");
  // bullet runs → one list
  text = text.replace(/(?:^|\n)((?:[-*]\s+[^\n]+\n?)+)/g, (_m, run) => {
    const items = run.trim().split("\n").map((l) => `<li>${l.replace(/^[-*]\s+/, "")}</li>`).join("");
    return `<ul>${items}</ul>`;
  });
  const html = text.split(/\n{2,}/).map((para) =>
    /^\s*(<h3>|<ul>|\u0000B\d+\u0000)/.test(para) ? para : `<p>${para}</p>`).join("");
  return html.replace(/\u0000B(\d+)\u0000/g, (_m, i) => blocks[Number(i)]);
}

// What the agent is doing, in words. The raw JSON was unreadable at panel width,
// and the calls that matter most here are web-bridge's own — "运行能力
// extract-article" says something; {"capability":"extract-article","params":{}}
// truncated at 90 characters does not.
function describeTool(name, input) {
  const n = String(name || "").replace(/^mcp__[^_]*(?:-[^_]*)*__/, "");
  const a = input && typeof input === "object" ? input : {};
  const short = (v, len = 60) => {
    const t = typeof v === "string" ? v : JSON.stringify(v ?? "");
    return t.length > len ? t.slice(0, len) + "…" : t;
  };
  switch (n) {
    case "web_exec": return `在页面执行 JS：${short(a.code)}`;
    case "web_run_capability": return `运行能力 ${a.capability}` +
      (a.params && Object.keys(a.params).length ? ` ${short(a.params, 40)}` : "");
    case "web_save_capability": return `保存能力 ${a.capability}`;
    case "web_capabilities": return a.capability ? `查看能力 ${a.capability}` : "列出可用能力";
    case "web_journal": return `翻日志${a.host ? "：" + a.host : ""}`;
    case "web_tabs": return a.filter ? `列出标签页（${a.filter}）` : "列出标签页";
    case "web_open": return `打开 ${short(a.url, 50)}`;
    case "web_close_tab": return `关闭标签页 ${short(a.url || a.tab_id, 40)}`;
    case "web_status": return "查 bridge 状态";
    case "Read": return `读取 ${short(a.file_path || a.file, 50)}`;
    case "Write": return `写入 ${short(a.file_path, 50)}`;
    case "Edit": return `编辑 ${short(a.file_path, 50)}`;
    case "Bash": return `$ ${short(a.command, 60)}`;
    case "Glob": case "Grep": return `搜索 ${short(a.pattern, 40)}`;
    case "ToolSearch": return "查找可用工具";
    case "WebFetch": case "WebSearch": return `联网 ${short(a.url || a.query, 50)}`;
    default: {
      const arg = Object.keys(a).length ? " " + short(a, 50) : "";
      return n + arg;
    }
  }
}

function agentBubble() {
  const el = addMsg("agent", '<span class="who"></span><span class="body"></span>');
  el.querySelector(".who").textContent = state.agents.default || "agent";
  return el;
}

// A fenced JS block in the answer is offered as a saveable script — this is the
// "tell the user they can keep it" step that makes the chat feed the library.
function offerSave(bubble, text) {
  const m = /```(?:js|javascript)\n([\s\S]*?)```/.exec(text || "");
  if (!m || m[1].trim().length < 20) return;
  const box = document.createElement("div");
  box.className = "save-code";
  box.innerHTML = '<button class="mini primary">存成脚本</button><button class="mini ghost">在本页运行</button>';
  const [saveBtn, runBtn] = box.querySelectorAll("button");
  saveBtn.addEventListener("click", () => {
    openForm(null, { code: m[1].trim(), title: "", kind: "extract" });
    document.querySelector('.tab[data-tab="scripts"]').click();
  });
  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true;
    try {
      const data = await api("/exec", {
        method: "POST",
        body: JSON.stringify({ code: m[1].trim(), url: state.tab?.url || "", timeout_ms: 60000 }),
      });
      showResult("对话里的脚本", data.result);
      document.querySelector('.tab[data-tab="scripts"]').click();
    } catch (e) {
      addMsg("err", esc(e.message));
    } finally {
      runBtn.disabled = false;
    }
  });
  bubble.appendChild(box);
}

// Reading the run's NDJSON is shared by a fresh ask and by reattaching to one
// already in flight, so both paths render identically.
async function consumeStream(resp, bubble, spin) {
  const body = bubble.querySelector(".body");
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let text = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      let ev;
      try { ev = JSON.parse(line); } catch { continue; }
      if (ev.type === "text") {
        text += ev.text;
        body.innerHTML = renderMarkdown(text);
      } else if (ev.type === "tool") {
        const t = document.createElement("div");
        t.className = "tool";
        t.textContent = "⚙ " + describeTool(ev.name, ev.input);
        t.title = `${ev.name} ${JSON.stringify(ev.input || {})}`;   // full call on hover
        bubble.insertBefore(t, spin);
      } else if (ev.type === "done") {
        if (ev.session_id) state.session = ev.session_id;
        if (!text && ev.text) { text = ev.text; body.innerHTML = renderMarkdown(text); }
      } else if (ev.type === "stderr") {
        console.warn("[agent]", ev.text);
      } else if (ev.type === "end" && ev.error) {
        addMsg("err", esc(ev.error));
      }
      $("messages").scrollTop = $("messages").scrollHeight;
    }
  }
  return text;
}

function startBubble() {
  const bubble = agentBubble();
  const spin = document.createElement("div");
  spin.className = "spin";
  spin.textContent = "运行中…";
  bubble.appendChild(spin);
  return { bubble, spin };
}

// A run outlives the panel: the agent keeps working while the panel is closed.
// On reopen, pick the answer back up instead of leaving the user with a question
// and no reply for work that was already paid for.
async function reattach(runId) {
  let info;
  try {
    info = await api(`/agent/run/${encodeURIComponent(runId)}`);
  } catch {
    return;                                  // run expired from the table
  }
  const { bubble, spin } = startBubble();
  if (info.done) {
    const text = (info.events || []).filter((e) => e.type === "text").map((e) => e.text).join("")
      || (info.events || []).find((e) => e.type === "done")?.text || "";
    bubble.querySelector(".body").innerHTML = renderMarkdown(text || "(这次运行没有产出文本)");
    spin.remove();
    if (info.error) addMsg("err", esc(info.error));
    offerSave(bubble, text);
    state.run = null;
    persist();
    return;
  }
  spin.textContent = "重新接上正在运行的任务…";
  $("send").disabled = true;
  $("stop").hidden = false;
  try {
    const resp = await fetch(`${BASE}/agent/run/${encodeURIComponent(runId)}?follow=true`, {
      headers: { Authorization: "Bearer " + BRIDGE_TOKEN },
    });
    const text = await consumeStream(resp, bubble, spin);
    offerSave(bubble, text);
  } catch (e) {
    addMsg("err", "重新接上失败：" + esc(e.message));
  } finally {
    spin.remove();
    $("send").disabled = false;
    $("stop").hidden = true;
    state.run = null;
    persist();
  }
}

async function ask(prompt) {
  if (!prompt.trim()) return;
  let full = prompt;
  if (state.context) {
    // The page text goes to the agent as context, with its source named so the
    // agent knows what it is looking at.
    full = `以下是我正在看的页面内容（${state.context.url}）：\n\n"""\n${state.context.text}\n"""\n\n${prompt}`;
  }
  addMsg("user", esc(prompt) + (state.context ? '<div class="tool">＋ 页面内容 ' + esc(state.context.title || "") + "</div>" : ""));
  $("input").value = "";
  $("send").disabled = true;
  $("stop").hidden = false;

  const { bubble, spin } = startBubble();
  let text = "";

  try {
    const resp = await fetch(BASE + "/agent/ask", {
      method: "POST",
      headers: { Authorization: "Bearer " + BRIDGE_TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: full,
        agent: $("agent-pick").value || undefined,
        session_id: state.session || undefined,
        // the agent cannot guess this, and without it one run opened with
        // osascript to ask Chrome what was on screen
        page: state.tab ? { url: state.tab.url, title: state.tab.title } : undefined,
      }),
    });
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || "HTTP " + resp.status);
    state.run = resp.headers.get("X-Run-Id");
    persist();                                 // remember it BEFORE the long wait
    text = await consumeStream(resp, bubble, spin);
    offerSave(bubble, text);
  } catch (e) {
    addMsg("err", esc(e.message));
  } finally {
    spin.remove();
    $("send").disabled = false;
    $("stop").hidden = true;
    state.run = null;
    if (state.context) { state.context = null; $("ctx-chip").hidden = true; }
    persist();
  }
}

$("composer").addEventListener("submit", (e) => { e.preventDefault(); ask($("input").value); });
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("input").value); }
});
$("stop").addEventListener("click", async () => {
  if (state.run) await api(`/agent/run/${state.run}/stop`, { method: "POST" }).catch(() => {});
});
$("clear-chat").addEventListener("click", () => {
  $("messages").innerHTML = "";
  state.session = null;
  chrome.storage.local.remove(STORE_KEY).catch(() => {});
  toast("对话已清空（下一条重新开一个会话）");
});
document.querySelectorAll(".chip-btn").forEach((b) =>
  b.addEventListener("click", () => { $("input").value = b.textContent; $("input").focus(); }));

// Pull the current page into the chat context, using the library's own
// extractor rather than a second scraping implementation here.
$("ctx-btn").addEventListener("click", async () => {
  const btn = $("ctx-btn");
  btn.disabled = true;
  btn.textContent = "读取中…";
  try {
    const data = await api("/capability/extract-article", {
      method: "POST",
      body: JSON.stringify({ params: { max_chars: 30000 }, url: state.tab?.url || "" }),
    });
    const r = data.result || {};
    state.context = { title: r.title, url: r.url || state.tab?.url, text: r.content || "" };
    $("ctx-label").textContent = `已附带页面内容：${r.title || state.tab?.url} (${(r.content || "").length} 字)`;
    $("ctx-chip").hidden = false;
  } catch (e) {
    // Not every page is an article; fall back to the visible text.
    try {
      const data = await api("/exec", {
        method: "POST",
        body: JSON.stringify({ code: "return document.body.innerText.slice(0, 30000)", url: state.tab?.url || "" }),
      });
      state.context = { title: state.tab?.title, url: state.tab?.url, text: data.result || "" };
      $("ctx-label").textContent = `已附带页面文本 (${(data.result || "").length} 字)`;
      $("ctx-chip").hidden = false;
    } catch (e2) {
      toast("读取失败：" + e2.message, 2600);
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "＋页面内容";
  }
});
$("ctx-drop").addEventListener("click", () => { state.context = null; $("ctx-chip").hidden = true; });

// --------------------------------------------------------------------------- //
// scripts
// --------------------------------------------------------------------------- //
async function loadScripts() {
  const url = state.filterSite ? state.tab?.url || "" : "";
  try {
    const data = await api("/capabilities" + (url ? "?url=" + encodeURIComponent(url) : ""));
    state.caps = data.capabilities || [];
    renderScripts();
  } catch (e) {
    $("script-list").innerHTML = "";
    $("script-empty").hidden = false;
    $("script-empty").textContent = "读不到能力库：" + e.message;
  }
}

function paramField(name, spec) {
  spec = spec || {};
  const t = (spec.type || "string").toLowerCase();
  const def = spec.default;
  if (t === "boolean")
    return `<input type="checkbox" data-p="${esc(name)}" data-t="boolean" ${def ? "checked" : ""}>`;
  if (Array.isArray(spec.enum) && spec.enum.length)
    return `<select data-p="${esc(name)}" data-t="${t}">` +
      spec.enum.map((v) => `<option ${v === def ? "selected" : ""}>${esc(v)}</option>`).join("") + "</select>";
  return `<input type="${t === "number" ? "number" : "text"}" data-p="${esc(name)}" data-t="${t}"
    value="${def === undefined || def === null ? "" : esc(typeof def === "string" ? def : JSON.stringify(def))}"
    placeholder="${esc(spec.description || name)}">`;
}

function visibleCaps() {
  const q = ($("s-search").value || "").trim().toLowerCase();
  if (!q) return state.caps;
  return state.caps.filter((c) =>
    `${c.id} ${c.title || ""} ${c.description || ""} ${(c.match || []).join(" ")}`.toLowerCase().includes(q));
}

function renderScripts() {
  const list = $("script-list");
  const caps = visibleCaps();
  if (!caps.length) {
    list.innerHTML = "";
    $("script-empty").hidden = false;
    $("script-empty").textContent = $("s-search").value.trim()
      ? "没有匹配的脚本。"
      : state.filterSite
        ? "这个站点还没有专属脚本。切到「全部」看通用能力，或让对话里的 agent 写一个。"
        : "能力库是空的。";
    return;
  }
  $("script-empty").hidden = true;
  list.innerHTML = caps.map((c) => {
    const names = Object.keys(c.params || {});
    const canAuto = c.kind !== "extract";
    return `<div class="item" data-id="${esc(c.id)}">
      <div class="top">
        <span class="name">${esc(c.title || c.id)}</span>
        <span class="kind">${KIND_LABEL[c.kind] || c.kind || ""}</span>
      </div>
      ${c.description ? `<div class="desc">${esc(c.description.slice(0, 120))}</div>` : ""}
      <div class="ops">
        <button class="mini primary run">运行</button>
        <button class="mini ghost edit">编辑</button>
        ${names.length ? '<button class="mini ghost params-btn">参数</button>' : ""}
        ${canAuto ? `<label class="switch"><input type="checkbox" class="auto" ${c.autorun ? "checked" : ""}>自动运行</label>` : ""}
      </div>
      ${names.length ? `<div class="params">${names.map((n) =>
        `<div class="prow"><label title="${esc((c.params[n] || {}).description || "")}">${esc(n)}</label>${paramField(n, c.params[n])}</div>`).join("")}</div>` : ""}
    </div>`;
  }).join("");

  list.querySelectorAll(".item").forEach((item) => {
    const id = item.dataset.id;
    const cap = state.caps.find((c) => c.id === id);
    item.querySelector(".params-btn")?.addEventListener("click", () => item.classList.toggle("open"));
    item.querySelector(".run").addEventListener("click", () => runScript(item, cap));
    item.querySelector(".edit").addEventListener("click", () => openForm(id));
    item.querySelector(".auto")?.addEventListener("change", async (e) => {
      try {
        await api(`/capability/${encodeURIComponent(id)}/autorun`, {
          method: "POST", body: JSON.stringify({ autorun: e.target.checked }),
        });
        await chrome.runtime.sendMessage({ type: "WB_PANEL", action: "sync-autorun" });
        toast(e.target.checked ? "已开启自动运行（刷新页面生效）" : "已关闭（刷新页面还原）");
        cap.autorun = e.target.checked;
      } catch (err) {
        e.target.checked = !e.target.checked;
        toast("失败：" + err.message, 2600);
      }
    });
  });
}

function readParams(item) {
  const params = {};
  item.querySelectorAll("[data-p]").forEach((el) => {
    const t = el.dataset.t;
    if (t === "boolean") { params[el.dataset.p] = el.checked; return; }
    const raw = el.value.trim();
    if (!raw) return;
    if (t === "number") params[el.dataset.p] = Number(raw);
    else if (t === "object" || t === "array") {
      try { params[el.dataset.p] = JSON.parse(raw); }
      catch { throw new Error(`参数 ${el.dataset.p} 不是合法 JSON`); }
    } else params[el.dataset.p] = raw;
  });
  return params;
}

async function runScript(item, cap) {
  const btn = item.querySelector(".run");
  btn.disabled = true;
  btn.textContent = "运行中…";
  try {
    const data = await api(`/capability/${encodeURIComponent(cap.id)}`, {
      method: "POST",
      body: JSON.stringify({ params: readParams(item), url: state.tab?.url || "", timeout_ms: 120000 }),
    });
    showResult(cap.title || cap.id, data.result);
  } catch (e) {
    showResult(cap.title || cap.id, { error: e.message });
  } finally {
    btn.disabled = false;
    btn.textContent = "运行";
  }
}

function showResult(name, result) {
  const text = typeof result === "string" ? result : JSON.stringify(result, null, 2);
  $("rr-name").textContent = name;
  $("rr-body").textContent = text ?? "(无返回值)";
  $("run-result").hidden = false;
  const blob = new Blob([text ?? ""], { type: "application/json" });
  $("rr-download").href = URL.createObjectURL(blob);
  $("rr-download").download = name.replace(/\s+/g, "-") + ".json";
}
$("rr-close").addEventListener("click", () => ($("run-result").hidden = true));
$("rr-copy").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("rr-body").textContent);
  toast("已复制");
});

// ---- new / edit form ----
function openForm(id, preset) {
  const cap = id ? state.caps.find((c) => c.id === id) : null;
  state.editing = id || "";
  $("script-form").hidden = false;
  $("script-list").hidden = true;
  $("script-empty").hidden = true;
  $("s-err").hidden = true;
  $("s-delete").hidden = !id;
  $("s-id").disabled = !!id;
  $("s-title").value = cap?.title || preset?.title || "";
  $("s-id").value = cap?.id || "";
  $("s-desc").value = cap?.description || "";
  $("s-kind").value = cap?.kind || preset?.kind || "extract";
  $("s-autorun").checked = !!cap?.autorun;
  $("s-code").value = preset?.code || "";
  fillParams(cap?.params || {});
  const m = cap?.match || [];
  $("s-scope").value = !cap ? "site" : m.includes("*") ? "all" : "custom";
  $("s-match").value = m.filter((x) => x !== "*").join(", ");
  $("s-match").hidden = $("s-scope").value !== "custom";
  if (id) {
    // The source is not in the listing payload — fetch it so an edit starts
    // from the real code rather than an empty box.
    api(`/capability/${encodeURIComponent(id)}`).then((d) => ($("s-code").value = d.source || ""))
      .catch(() => {});
  }
}
$("new-script").addEventListener("click", () => openForm(null));
$("s-search").addEventListener("input", renderScripts);
$("s-cancel").addEventListener("click", closeForm);
$("s-scope").addEventListener("change", () => ($("s-match").hidden = $("s-scope").value !== "custom"));

// ---- parameter editor ----
// Without this the panel could only ever save `params: {}`, so a script authored
// here could never take arguments and the bridge's validation had nothing to
// check. Editing the declaration is what makes a saved script reusable.
const PARAM_TYPES = ["string", "number", "boolean", "object", "array"];

function paramRow(name = "", spec = {}) {
  const row = document.createElement("div");
  row.className = "prow-edit";
  row.innerHTML = `
    <input class="pname" placeholder="参数名" value="${esc(name)}">
    <select class="ptype">${PARAM_TYPES.map((t) =>
      `<option ${t === (spec.type || "string") ? "selected" : ""}>${t}</option>`).join("")}</select>
    <label class="req"><input type="checkbox" class="preq" ${spec.required ? "checked" : ""}>必填</label>
    <button class="icon-btn pdel" type="button" title="删除">✕</button>
    <input class="pdesc" placeholder="说明（agent 靠它知道该传什么）＋默认值写在下一格"
           value="${esc(spec.description || "")}">
    <input class="pdef" placeholder="默认值（留空=无默认）"
           value="${spec.default === undefined ? "" : esc(typeof spec.default === "string" ? spec.default : JSON.stringify(spec.default))}">`;
  row.querySelector(".pdef").style.gridColumn = "1 / -1";
  row.querySelector(".pdel").addEventListener("click", () => row.remove());
  return row;
}

function fillParams(params) {
  const box = $("s-params");
  box.innerHTML = "";
  for (const [name, spec] of Object.entries(params || {})) box.appendChild(paramRow(name, spec));
}

function collectParams() {
  const out = {};
  for (const row of $("s-params").querySelectorAll(".prow-edit")) {
    const name = row.querySelector(".pname").value.trim();
    if (!name) continue;
    const type = row.querySelector(".ptype").value;
    const spec = { type, description: row.querySelector(".pdesc").value.trim() };
    if (row.querySelector(".preq").checked) spec.required = true;
    const raw = row.querySelector(".pdef").value.trim();
    if (raw !== "") {
      // a default must match the declared type, or the bridge rejects every call
      if (type === "number") spec.default = Number(raw);
      else if (type === "boolean") spec.default = /^(true|1|yes)$/i.test(raw);
      else if (type === "object" || type === "array") {
        try { spec.default = JSON.parse(raw); }
        catch { throw new Error(`参数 ${name} 的默认值不是合法 JSON`); }
      } else spec.default = raw;
      if (spec.required) delete spec.required;   // lint refuses both at once
    }
    out[name] = spec;
  }
  return out;
}

$("s-addparam").addEventListener("click", () => $("s-params").appendChild(paramRow()));

function closeForm() {
  $("script-form").hidden = true;
  $("script-list").hidden = false;
  state.editing = null;
  loadScripts();
}

$("s-save").addEventListener("click", async () => {
  const id = ($("s-id").value || "").trim().replace(/[^A-Za-z0-9_.-]/g, "-");
  const code = $("s-code").value;
  if (!id) return showErr("需要一个 id（脚本的文件名）");
  if (!code.trim()) return showErr("代码不能为空");
  const host = (() => { try { return new URL(state.tab?.url || "").hostname.replace(/^www\./, ""); } catch { return ""; } })();
  let params;
  try { params = collectParams(); } catch (e) { return showErr(e.message); }
  const scope = $("s-scope").value;
  const match = scope === "all" ? ["*"]
    : scope === "custom" ? ($("s-match").value.split(",").map((x) => x.trim()).filter(Boolean) || ["*"])
    : [host || "*"];
  const meta = {
    id,
    title: $("s-title").value.trim() || id,
    description: $("s-desc").value.trim(),
    kind: $("s-kind").value,
    match: match.length ? match : ["*"],
    params,
    autorun: $("s-autorun").checked,
  };
  const source = "/* @web-bridge-capability\n" + JSON.stringify(meta, null, 2) + "\n*/\n" + code;
  try {
    await api(`/capability/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ source }) });
    await chrome.runtime.sendMessage({ type: "WB_PANEL", action: "sync-autorun" });
    toast("已保存到能力库");
    closeForm();
  } catch (e) {
    showErr(e.message);
  }
});
$("s-delete").addEventListener("click", async () => {
  if (!state.editing) return;
  await api(`/capability/${encodeURIComponent(state.editing)}`, { method: "DELETE" }).catch(() => {});
  await chrome.runtime.sendMessage({ type: "WB_PANEL", action: "sync-autorun" });
  toast("已删除");
  closeForm();
});
function showErr(msg) { $("s-err").hidden = false; $("s-err").textContent = msg; }

$("f-site").addEventListener("click", () => { state.filterSite = true; $("f-site").classList.add("active"); $("f-all").classList.remove("active"); loadScripts(); });
$("f-all").addEventListener("click", () => { state.filterSite = false; $("f-all").classList.add("active"); $("f-site").classList.remove("active"); loadScripts(); });

// --------------------------------------------------------------------------- //
// page tab
// --------------------------------------------------------------------------- //
const QUICK = [
  { id: "extract-article", label: "提取正文" },
  { id: "inspect-page", label: "探查结构" },
  { id: "extract-tables", label: "提取表格" },
  { id: "reader-mode", label: "阅读模式" },
];

async function loadPage() {
  $("quick").innerHTML = QUICK.map((q) => `<button class="chip-btn" data-cap="${q.id}">${q.label}</button>`).join("");
  $("quick").querySelectorAll("[data-cap]").forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        const data = await api(`/capability/${b.dataset.cap}`, {
          method: "POST", body: JSON.stringify({ params: {}, url: state.tab?.url || "" }),
        });
        showResult(b.textContent, data.result);
        document.querySelector('.tab[data-tab="scripts"]').click();
      } catch (e) { toast(e.message, 2800); } finally { b.disabled = false; }
    }));

  try {
    const h = await fetch(BASE + "/health").then((r) => r.json());
    $("st-bridge").textContent = h.ok ? "运行中" : "异常";
    $("st-ext").textContent = h.extension_connected ? "已连接 ✅" : "未连接 ❌";
    $("st-svc").textContent = h.version ? `v${h.version}` : "—";
  } catch {
    $("st-bridge").textContent = "未运行 ❌";
  }
  try {
    const a = await chrome.runtime.sendMessage({ type: "WB_PANEL", action: "sync-autorun" });
    $("st-auto").textContent = a?.ok ? `${a.registered ?? 0} 个已注册` : a?.error || "—";
  } catch { $("st-auto").textContent = "—"; }

  try {
    const j = await api("/journal?limit=6&host=" + encodeURIComponent(hostOf(state.tab?.url)));
    const rows = j.matches || [];
    $("prior").innerHTML = rows.length
      ? rows.map((m) => `<div class="p" data-code="${esc(m.code || "")}">
          <b>${esc((m.summary || m.capability || "").slice(0, 60))}</b>
          <span>成功 ${m.ok_runs}/${m.runs} 次 · ${esc(m.last || "")}${m.promoted_to ? " · 已沉淀 " + esc(m.promoted_to) : ""}</span></div>`).join("")
      : '<p class="empty">这个站还没有记录。</p>';
    $("prior").querySelectorAll(".p").forEach((el) =>
      el.addEventListener("click", () => {
        if (!el.dataset.code) return;
        openForm(null, { code: el.dataset.code, kind: "extract" });
        document.querySelector('.tab[data-tab="scripts"]').click();
      }));
  } catch { $("prior").innerHTML = ""; }

  try {
    const t = await api("/tabs");
    $("tabs-list").innerHTML = (t.tabs || []).slice(0, 12).map((x) =>
      `<div class="t" data-id="${x.id}"><b>${esc((x.title || "").slice(0, 46))}</b><span>${esc((x.url || "").slice(0, 70))}</span></div>`).join("");
    $("tabs-list").querySelectorAll(".t").forEach((el) =>
      el.addEventListener("click", () => chrome.tabs.update(Number(el.dataset.id), { active: true })));
  } catch { $("tabs-list").innerHTML = ""; }
}

const hostOf = (u) => { try { return new URL(u).hostname; } catch { return ""; } };

// --------------------------------------------------------------------------- //
// boot
// --------------------------------------------------------------------------- //
async function refreshHeader() {
  state.tab = await currentTab();
  $("host").textContent = hostOf(state.tab?.url) || "(无页面)";
  $("url").textContent = state.tab?.url || "";
  try {
    const h = await fetch(BASE + "/health").then((r) => r.json());
    $("dot").classList.toggle("on", !!h.extension_connected);
    $("dot").title = h.extension_connected ? "bridge 与扩展都在线" : "bridge 在，扩展未连接";
  } catch {
    $("dot").classList.remove("on");
    $("dot").title = "本地 bridge 未运行";
  }
}

async function loadAgents() {
  try {
    const a = await api("/agents");
    state.agents = a;
    const opts = Object.entries(a.runners || {})
      .filter(([, r]) => r.available && r.enabled !== false)
      .map(([n, r]) => `<option value="${esc(n)}" ${n === a.default ? "selected" : ""}>${esc(r.label || n)}</option>`);
    $("agent-pick").innerHTML = opts.join("") || '<option value="">没有可用 agent</option>';
    if (state.wantAgent && a.runners?.[state.wantAgent]?.available) {
      $("agent-pick").value = state.wantAgent;      // the choice from last time
    }
    if (!opts.length) $("send").disabled = true;
  } catch (e) {
    $("agent-pick").innerHTML = '<option value="">bridge 未运行</option>';
    $("send").disabled = true;
    addMsg("err", "本地 bridge 没在运行，对话不可用。\n终端里跑：python3 ~/cc/web-bridge/bridge/cli.py service status");
  }
}

$("refresh").addEventListener("click", async () => {
  await refreshHeader();
  const active = document.querySelector(".tab.active").dataset.tab;
  if (active === "scripts") loadScripts();
  if (active === "page") loadPage();
  toast("已刷新");
});
chrome.tabs.onActivated.addListener(refreshHeader);
chrome.tabs.onUpdated.addListener((id, info) => { if (info.status === "complete") refreshHeader(); });

$("agent-pick").addEventListener("change", persist);

(async () => {
  await restore();          // before loadAgents, which honours the remembered pick
  await refreshHeader();
  await loadAgents();
  if (state.pendingRun) { const id = state.pendingRun; state.pendingRun = null; reattach(id); }
})();
