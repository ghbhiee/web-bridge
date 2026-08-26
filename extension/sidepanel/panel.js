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
  usage: {},          // capability id → how often it has been used here
  userScripts: [],    // the user's own scripts
  userTotal: 0,
  userFilterSite: true,
  editingUser: null,
  composing: false,   // an IME candidate window is open
  savedScript: null,  // {id, name} saved from THIS conversation — later saves update it
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
                     run: state.run, savedScript: state.savedScript },
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
  state.savedScript = saved.savedScript || null;
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
    if (btn.dataset.tab === "page") loadUserScripts();
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
// A saved script needs a name and the user pressed one button, so derive one:
// the code's own first comment if it has one, else the request that produced it.
function scriptName(code, bubble) {
  const comment = /^\s*\/\/\s*(.+)$/m.exec(code || "");
  if (comment && comment[1].trim().length > 1) return comment[1].trim().slice(0, 40);
  const asked = [...$("messages").querySelectorAll(".msg.user")].pop();
  const t = (asked?.textContent || "").trim().replace(/\s+/g, " ");
  return (t ? t.slice(0, 30) : "对话里写的脚本") + "（" + hostOf(state.tab?.url) + "）";
}

function offerSave(bubble, text) {
  const m = /```(?:js|javascript)\n([\s\S]*?)```/.exec(text || "");
  if (!m || m[1].trim().length < 20) return;
  const code = m[1].trim();
  const box = document.createElement("div");
  box.className = "save-code";

  // Refining a script over several rounds is the normal way this is used, so the
  // default has to be "update the one I saved", not "make another copy". Saving
  // used to always create, leaving a pile of near-identical scripts behind.
  const saved = state.savedScript;
  box.innerHTML =
    `<button class="mini primary">${saved ? "更新「" + esc(saved.name) + "」" : "保存到我的脚本库"}</button>` +
    `<button class="mini ghost">在本页运行</button>` +
    (saved ? '<button class="mini ghost asnew">另存为新脚本</button>' : "");
  const saveBtn = box.querySelector(".mini.primary");
  const runBtn = box.querySelectorAll("button")[1];
  const asNewBtn = box.querySelector(".asnew");

  async function store(asNew) {
    const target = !asNew && state.savedScript ? state.savedScript.id : "new";
    // On an update only the code travels: name, matches and the autorun switch
    // belong to whatever the user set in the panel and must survive.
    const body = target === "new"
      ? { name: scriptName(code, bubble), code, matches: [hostOf(state.tab?.url) || "*"],
          autorun: false, note: "对话里让 agent 写的" }
      : { code };
    const data = await api(`/user-script/${encodeURIComponent(target)}`, {
      method: "PUT", body: JSON.stringify(body),
    });
    state.savedScript = { id: data.script.id, name: data.script.name };
    persist();
    if (document.querySelector(".tab.active")?.dataset.tab === "page") loadUserScripts();
    return data.script;
  }

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      const rec = await store(false);
      saveBtn.textContent = state.savedScript ? "已更新 ✓" : "已保存 ✓";
      toast(`已存到「页面」→ ${rec.name}`, 2200);
    } catch (e) {
      saveBtn.disabled = false;
      addMsg("err", "保存失败：" + esc(e.message));
    }
  });

  asNewBtn?.addEventListener("click", async () => {
    asNewBtn.disabled = true;
    try {
      const rec = await store(true);
      asNewBtn.textContent = "已另存 ✓";
      toast(`已新建 → ${rec.name}`, 2200);
    } catch (e) {
      asNewBtn.disabled = false;
      addMsg("err", "保存失败：" + esc(e.message));
    }
  });

  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true;
    try {
      const data = await api("/exec", {
        method: "POST",
        body: JSON.stringify({ code, url: state.tab?.url || "", timeout_ms: 60000 }),
      });
      document.querySelector('.tab[data-tab="page"]').click();
      showUserResult("对话里的脚本", data.result);
    } catch (e) {
      addMsg("err", esc(e.message));
    } finally {
      runBtn.disabled = false;
    }
  });
  bubble.appendChild(box);
}

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
  // While an IME is composing, Enter belongs to the candidate window — it picks
  // the word. Treating it as "send" fired the message mid-word and left the
  // English spelling behind. keyCode 229 is the older signal for the same thing;
  // both are checked because browsers disagree about which they set.
  if (e.isComposing || e.keyCode === 229 || state.composing) return;
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("input").value); }
});
$("input").addEventListener("compositionstart", () => (state.composing = true));
$("input").addEventListener("compositionend", () => {
  // the composition's own Enter can arrive after this event on some IMEs, so
  // stay "composing" until the next tick
  setTimeout(() => (state.composing = false), 0);
});
$("stop").addEventListener("click", async () => {
  if (state.run) await api(`/agent/run/${state.run}/stop`, { method: "POST" }).catch(() => {});
});
$("clear-chat").addEventListener("click", () => {
  $("messages").innerHTML = "";
  state.session = null;
  state.savedScript = null;                 // a new conversation saves a new script
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
    try {
      const j = await api("/journal?limit=100" + (url ? "&host=" + encodeURIComponent(hostOf(state.tab?.url)) : ""));
      state.usage = {};
      for (const m of j.matches || []) {
        const id = m.capability || m.promoted_to;
        if (id) state.usage[id] = m;
      }
    } catch { state.usage = {}; }
    renderScripts();
  } catch (e) {
    $("script-list").innerHTML = "";
    $("script-empty").hidden = false;
    $("script-empty").textContent = "读不到能力库：" + e.message;
  }
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
      ? "没有匹配的能力。"
      : state.filterSite
        ? "agent 在这个站点还没有攒下专属能力。去「对话」里让它做点什么，做成了就会出现在这里。"
        : "还没有任何能力。";
    return;
  }
  $("script-empty").hidden = true;
  // Names and plain descriptions only. This list answers "what can it do / what
  // has it done here", not "what does the code say" — the code is the agent's
  // business, and showing it buried the one thing the user came for.
  list.innerHTML = caps.map((c) => {
    const u = state.usage[c.id];
    const used = u ? `用过 ${u.ok_runs || u.runs} 次` : "还没用过";
    const when = u && u.last ? " · " + u.last.replace("T", " ").slice(5, 16) : "";
    return `<div class="item">
      <div class="top">
        <span class="name">${esc(c.title || c.id)}</span>
        <span class="kind">${KIND_LABEL[c.kind] || c.kind || ""}</span>
      </div>
      ${c.description ? `<div class="desc">${esc(c.description.slice(0, 150))}</div>` : ""}
      <div class="ops"><span class="uses">${used}${when}</span></div>
    </div>`;
  }).join("");
}

// --------------------------------------------------------------------------- //
// page tab
// --------------------------------------------------------------------------- //
// --------------------------------------------------------------------------- //
// 页面 tab — the user's own scripts
// --------------------------------------------------------------------------- //
// Separate from the capability library on purpose: this code belongs to the
// user. They paste it, read it, edit it. The agent's capabilities are machine
// facing and live in their own tab.
const PROMPTS = {
  beautify: `请写一段可以直接在浏览器页面执行的原生 JavaScript（不使用任何外部库）。
目标：美化当前页面 —— [描述你想要的效果，例如：正文栏加宽到 90%、隐藏左右侧边栏、字号调大到 17px、行距 1.8]。
要求：只修改 DOM 和样式，代码要能重复执行不报错（先判断元素是否存在）。
页面相关 HTML 结构如下（右键→检查→复制元素）：
[粘贴 HTML 片段]`,
  addbtn: `请写一段可以直接在浏览器页面执行的原生 JavaScript（不使用任何外部库）。
目标：在页面右上角添加一个悬浮按钮，点击后 [描述功能，例如：一键复制页面标题和网址 / 滚动到评论区]。
要求：按钮用 position:fixed 定位，样式美观，重复执行不重复添加按钮。
页面相关 HTML 结构如下：
[粘贴 HTML 片段]`,
  clean: `请写一段可以直接在浏览器页面执行的原生 JavaScript（不使用任何外部库）。
目标：清理当前页面 —— 移除 [广告位 / 弹窗 / 推荐区 / 悬浮客服] 等干扰元素。
要求：用 querySelectorAll 找到元素后 remove()；对动态加载的元素用 MutationObserver 持续清理。
需要清理的元素 HTML 如下：
[粘贴 HTML 片段]`,
  list: `请写一段可以直接在浏览器页面执行的原生 JavaScript（不使用任何外部库）。
目标：从页面提取 [商品 / 文章 / 职位] 列表，每条包含字段：[标题、价格、链接、…]。
要求：
1. 代码末尾必须用 return 返回一个 JSON 数组（纯对象数组，不能包含 DOM 节点）
2. 字段取不到时给 null，文本要 trim()
3. 可以使用 await（比如等待懒加载）
列表项的 HTML 结构如下（右键→检查→复制元素）：
[粘贴一条列表项的 HTML]`,
  table: `请写一段可以直接在浏览器页面执行的原生 JavaScript（不使用任何外部库）。
目标：把页面中的表格提取为 JSON。
要求：
1. 第一行作为字段名，每行数据转成对象
2. 代码末尾必须用 return 返回 JSON 数组
表格的 HTML 结构如下：
[粘贴 <table> 的 HTML，太长可只贴表头和一两行]`,
  paging: `请写一段可以直接在浏览器页面执行的原生 JavaScript（不使用任何外部库）。
目标：翻页批量提取 —— 每页提取 [字段列表]，然后点击「下一页」按钮继续，直到最后一页或最多 [10] 页。
要求：
1. 用 async 写法；每次翻页后 await new Promise(r => setTimeout(r, 1500)) 等待加载
2. 「下一页」按钮选择器：[粘贴按钮的选择器或 HTML]；按钮禁用或消失即停止
3. 所有页的数据合并成一个数组，代码末尾用 return 返回
列表项的 HTML 结构如下：
[粘贴一条列表项的 HTML]`,
};

async function loadUserScripts() {
  const url = state.userFilterSite ? state.tab?.url || "" : "";
  try {
    const data = await api("/user-scripts" + (url ? "?url=" + encodeURIComponent(url) : ""));
    state.userScripts = data.scripts || [];
    state.userTotal = data.total || 0;
    renderUserScripts();
  } catch (e) {
    $("user-list").innerHTML = "";
    $("user-empty").hidden = false;
    $("user-empty").textContent = "读不到脚本：" + e.message;
  }
}

function renderUserScripts() {
  const list = $("user-list");
  if (!state.userScripts.length) {
    list.innerHTML = "";
    $("user-empty").hidden = false;
    $("user-empty").textContent = state.userFilterSite && state.userTotal
      ? "这个站点还没有你的脚本（其它站点有 " + state.userTotal + " 个，点「全部」查看）。"
      : "还没有脚本。点「＋ 新建脚本」，贴一段 JS 就能用。";
    return;
  }
  $("user-empty").hidden = true;
  list.innerHTML = state.userScripts.map((u) => `<div class="item" data-id="${esc(u.id)}">
      <div class="top">
        <span class="name">${esc(u.name)}</span>
        <span class="kind">${esc((u.matches || []).join(",").slice(0, 22))}</span>
      </div>
      ${u.note ? `<div class="desc">${esc(u.note)}</div>` : ""}
      <div class="ops">
        <button class="mini primary run">运行</button>
        <button class="mini ghost edit">编辑</button>
        <button class="mini ghost mark" title="导出成书签，可拖到其它电脑的书签栏">书签</button>
        <label class="switch"><input type="checkbox" class="auto" ${u.autorun ? "checked" : ""}>自动运行</label>
      </div>
    </div>`).join("");

  list.querySelectorAll(".item").forEach((item) => {
    const u = state.userScripts.find((x) => x.id === item.dataset.id);
    item.querySelector(".run").addEventListener("click", () => runUserScript(item, u));
    item.querySelector(".edit").addEventListener("click", () => openUserForm(u));
    item.querySelector(".mark").addEventListener("click", () => exportBookmarklet(u));
    item.querySelector(".auto").addEventListener("change", async (e) => {
      try {
        await api(`/user-script/${encodeURIComponent(u.id)}/autorun`, {
          method: "POST", body: JSON.stringify({ autorun: e.target.checked }),
        });
        await chrome.runtime.sendMessage({ type: "WB_PANEL", action: "sync-autorun" });
        u.autorun = e.target.checked;
        toast(e.target.checked ? "已开启自动运行（刷新页面生效）" : "已关闭（刷新页面还原）");
      } catch (err) {
        e.target.checked = !e.target.checked;
        toast("失败：" + err.message, 2600);
      }
    });
  });
}

async function runUserScript(item, u) {
  const btn = item.querySelector(".run");
  btn.disabled = true;
  btn.textContent = "运行中…";
  try {
    const data = await api(`/user-script/${encodeURIComponent(u.id)}/run`, {
      method: "POST", body: JSON.stringify({ url: state.tab?.url || "" }),
    });
    showUserResult(u.name, data.result);
  } catch (e) {
    showUserResult(u.name, { error: e.message });
  } finally {
    btn.disabled = false;
    btn.textContent = "运行";
  }
}

function showUserResult(name, result) {
  const text = typeof result === "string" ? result : JSON.stringify(result, null, 2);
  $("ur-name").textContent = name;
  $("ur-body").textContent = text ?? "(无返回值)";
  $("u-result").hidden = false;
  $("ur-download").href = URL.createObjectURL(new Blob([text ?? ""], { type: "application/json" }));
  $("ur-download").download = name.replace(/\s+/g, "-") + ".json";
}

async function exportBookmarklet(u) {
  // Opened as a page rather than copied to the clipboard on purpose: Chrome
  // refuses a hand-typed or pasted `javascript:` bookmark, so the only way to
  // install one is to DRAG a real anchor — which means there has to be a page.
  try {
    const data = await api(`/user-script/${encodeURIComponent(u.id)}/bookmarklet`);
    const url = URL.createObjectURL(new Blob([data.html], { type: "text/html" }));
    await chrome.tabs.create({ url });
    toast("已打开书签页：把蓝色按钮拖到书签栏", 3000);
  } catch (e) {
    toast("导出失败：" + e.message, 2600);
  }
}

function openUserForm(u) {
  state.editingUser = u?.id || "";
  $("user-form").hidden = false;
  $("user-list").hidden = true;
  $("user-empty").hidden = true;
  $("u-err").hidden = true;
  $("u-delete").hidden = !u;
  $("u-name").value = u?.name || "";
  $("u-code").value = u?.code || "";
  $("u-autorun").checked = !!u?.autorun;
  const m = u?.matches || [];
  $("u-scope").value = !u ? "site" : m.includes("*") ? "all" : "custom";
  $("u-match").value = m.filter((x) => x !== "*").join(", ");
  $("u-match").hidden = $("u-scope").value !== "custom";
}

function closeUserForm() {
  $("user-form").hidden = true;
  $("user-list").hidden = false;
  state.editingUser = null;
  loadUserScripts();
}

function currentScopeMatches() {
  const scope = $("u-scope").value;
  let host = "", path = "";
  try {
    const u = new URL(state.tab?.url || "");
    host = u.hostname.replace(/^www\./, "");
    path = host + u.pathname;
  } catch (_) {}
  if (scope === "all") return ["*"];
  if (scope === "page") return [path || host || "*"];
  if (scope === "custom") {
    return $("u-match").value.split(",").map((x) => x.trim()).filter(Boolean).length
      ? $("u-match").value.split(",").map((x) => x.trim()).filter(Boolean) : ["*"];
  }
  return [host || "*"];
}

function wirePageTab() {
  $("u-new").addEventListener("click", () => openUserForm(null));
  $("u-cancel").addEventListener("click", closeUserForm);
  $("u-scope").addEventListener("change", () => ($("u-match").hidden = $("u-scope").value !== "custom"));
  $("ur-close").addEventListener("click", () => ($("u-result").hidden = true));
  $("ur-copy").addEventListener("click", async () => {
    await navigator.clipboard.writeText($("ur-body").textContent);
    toast("已复制");
  });
  $("u-site").addEventListener("click", () => {
    state.userFilterSite = true;
    $("u-site").classList.add("active"); $("u-all").classList.remove("active");
    loadUserScripts();
  });
  $("u-all").addEventListener("click", () => {
    state.userFilterSite = false;
    $("u-all").classList.add("active"); $("u-site").classList.remove("active");
    loadUserScripts();
  });

  // prompt templates: the user may well take these to a different AI, which is
  // exactly why they stay — the panel is not the only place scripts get written
  document.querySelectorAll(".prompt-chip").forEach((b) =>
    b.addEventListener("click", async () => {
      await navigator.clipboard.writeText(PROMPTS[b.dataset.p] || "");
      const old = b.textContent;
      b.textContent = "已复制 ✓";
      setTimeout(() => (b.textContent = old), 1200);
    }));

  $("u-ask-agent").addEventListener("click", () => {
    document.querySelector('.tab[data-tab="chat"]').click();
    $("input").value = "帮我给这个页面写一段 JS，做到：（在这里写你想要的效果，" +
      "例如：把正文加宽、隐藏顶部提示、把列表抓成 JSON）。写好后直接在页面上跑给我看，" +
      "并存到我的脚本库。";
    $("input").focus();
  });

  $("u-save").addEventListener("click", async () => {
    const code = $("u-code").value;
    if (!code.trim()) { $("u-err").hidden = false; $("u-err").textContent = "代码不能为空"; return; }
    try {
      await api(`/user-script/${encodeURIComponent(state.editingUser || "new")}`, {
        method: "PUT",
        body: JSON.stringify({
          name: $("u-name").value.trim(),
          code,
          matches: currentScopeMatches(),
          autorun: $("u-autorun").checked,
        }),
      });
      await chrome.runtime.sendMessage({ type: "WB_PANEL", action: "sync-autorun" });
      toast("已保存");
      closeUserForm();
    } catch (e) {
      $("u-err").hidden = false;
      $("u-err").textContent = e.message;
    }
  });

  $("u-delete").addEventListener("click", async () => {
    if (!state.editingUser) return;
    await api(`/user-script/${encodeURIComponent(state.editingUser)}`, { method: "DELETE" }).catch(() => {});
    await chrome.runtime.sendMessage({ type: "WB_PANEL", action: "sync-autorun" });
    toast("已删除");
    closeUserForm();
  });
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
  if (active === "page") loadUserScripts();
  toast("已刷新");
});
chrome.tabs.onActivated.addListener(refreshHeader);
chrome.tabs.onUpdated.addListener((id, info) => { if (info.status === "complete") refreshHeader(); });

$("agent-pick").addEventListener("change", persist);

$("f-site").addEventListener("click", () => {
  state.filterSite = true;
  $("f-site").classList.add("active"); $("f-all").classList.remove("active");
  loadScripts();
});
$("f-all").addEventListener("click", () => {
  state.filterSite = false;
  $("f-all").classList.add("active"); $("f-site").classList.remove("active");
  loadScripts();
});
$("s-search").addEventListener("input", renderScripts);

wirePageTab();

(async () => {
  await restore();          // before loadAgents, which honours the remembered pick
  await refreshHeader();
  await loadAgents();
  if (state.pendingRun) { const id = state.pendingRun; state.pendingRun = null; reattach(id); }
})();
