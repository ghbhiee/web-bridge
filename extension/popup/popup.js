// popup.js — browse the capabilities that apply to the current tab, and RUN one.
//
// Reads the catalog from the local bridge (same token as the service worker), so
// capabilities added on disk appear here immediately — no extension reload. A
// run posts to /capability/{id} with this tab's URL, i.e. it takes exactly the
// same path as `wb run` (validation, blocklist, tab resolution all included) —
// the popup is a front-end for the bridge, never a second implementation.

import { BRIDGE_WS, BRIDGE_TOKEN } from "../config.js";

const BASE = BRIDGE_WS.replace(/^ws:/, "http:").replace(/\/ws\/ext$/, "");

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const KIND_LABEL = {
  extract: "抽取数据",
  automate: "自动化",
  restyle: "美化重排",
  inspect: "探查",
  other: "其它",
};

async function api(path, opts = {}) {
  const r = await fetch(BASE + path, {
    ...opts,
    headers: {
      Authorization: "Bearer " + BRIDGE_TOKEN,
      ...(opts.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.error || "HTTP " + r.status);
  return data;
}

function shellQuote(s) {
  return /[^\w@%+=:,./-]/.test(s) ? "'" + s.replace(/'/g, `'\\''`) + "'" : s;
}

// --------------------------------------------------------------------------- //
// parameter form — built from the capability's declared param specs
// --------------------------------------------------------------------------- //
function fieldHtml(name, spec) {
  spec = spec || {};
  const t = (spec.type || "string").toLowerCase();
  const def = spec.default;
  const req = spec.required ? '<span class="req">*</span>' : "";
  const title = esc(spec.description || name);
  let input;
  if (t === "boolean") {
    input = `<input type="checkbox" data-name="${esc(name)}" data-type="boolean" ${def ? "checked" : ""}>`;
  } else if (Array.isArray(spec.enum) && spec.enum.length) {
    input = `<select data-name="${esc(name)}" data-type="${t}">` +
      spec.enum.map((v) => `<option ${v === def ? "selected" : ""}>${esc(v)}</option>`).join("") +
      `</select>`;
  } else if (t === "number") {
    input = `<input type="number" data-name="${esc(name)}" data-type="number"` +
      (spec.min !== undefined ? ` min="${esc(spec.min)}"` : "") +
      (spec.max !== undefined ? ` max="${esc(spec.max)}"` : "") +
      ` value="${def === undefined ? "" : esc(def)}" placeholder="${title}">`;
  } else {
    // object / array / string all typed as JSON-ish text; the bridge coerces
    const ph = t === "object" || t === "array" ? `JSON，如 {"名称": "选择器"}` : title;
    input = `<input type="text" data-name="${esc(name)}" data-type="${t}"` +
      ` value="${def === undefined || def === null ? "" : esc(typeof def === "string" ? def : JSON.stringify(def))}"` +
      ` placeholder="${esc(ph)}">`;
  }
  return `<div class="field" title="${title}"><label>${esc(name)}${req}</label>${input}</div>`;
}

function readForm(li) {
  const params = {};
  li.querySelectorAll("[data-name]").forEach((el) => {
    const name = el.dataset.name;
    const t = el.dataset.type;
    if (t === "boolean") {
      params[name] = el.checked;
      return;
    }
    const raw = el.value.trim();
    if (raw === "") return;                       // omitted → bridge applies the default
    if (t === "number") params[name] = Number(raw);
    else if (t === "object" || t === "array") {
      try { params[name] = JSON.parse(raw); }
      catch { throw new Error(`参数 ${name} 不是合法 JSON`); }
    } else params[name] = raw;
  });
  return params;
}

// --------------------------------------------------------------------------- //
// rendering
// --------------------------------------------------------------------------- //
function render(caps, url, host) {
  const body = $("body");
  if (!caps.length) {
    body.innerHTML = '<div class="empty">该页面暂无可用能力</div>';
    return;
  }
  const universal = caps.filter((c) => (c.match || []).includes("*"));
  const siteSpecific = caps.filter((c) => !(c.match || []).includes("*"));

  const item = (c) => {
    const specs = c.params || {};
    const names = Object.keys(specs);
    const example = `wb run ${c.id}` + (host ? ` --url ${shellQuote(host)}` : "") +
      (names.length ? ` --params '{"${names[0]}": …}'` : "");
    return `<li data-id="${esc(c.id)}">
      <div class="row head">
        <span class="title">${esc(c.title || c.id)}</span>
        <span class="kind">${KIND_LABEL[c.kind] || c.kind || ""}</span>
        <span class="id">${esc(c.id)}</span>
      </div>
      ${c.description ? `<div class="desc">${esc(c.description)}</div>` : ""}
      <div class="form">
        ${names.map((n) => fieldHtml(n, specs[n])).join("") || '<div class="hint">这个能力不需要参数</div>'}
        <div class="actions">
          <button class="run">在本页运行</button>
          <button class="ghost copy">复制命令</button>
          <span class="hint status"></span>
        </div>
        <div class="out"></div>
        <div class="cmd" hidden data-cmd="${esc(example)}"></div>
      </div>
    </li>`;
  };

  body.innerHTML =
    (siteSpecific.length ? `<div class="group">本站专属</div><ul>${siteSpecific.map(item).join("")}</ul>` : "") +
    (universal.length ? `<div class="group">通用能力</div><ul>${universal.map(item).join("")}</ul>` : "");

  body.querySelectorAll("li").forEach((li) => wire(li, url));
}

function wire(li, url) {
  const out = li.querySelector(".out");
  const status = li.querySelector(".status");
  const runBtn = li.querySelector(".run");

  li.querySelector(".head").addEventListener("click", () => {
    li.classList.toggle("open");
    if (li.classList.contains("open")) li.querySelector("input,select")?.focus();
  });

  li.querySelector(".copy").addEventListener("click", async () => {
    // copy the CLI form of exactly what the form says, so a run can be repeated
    // from a shell or handed to an agent
    let params = {};
    try { params = readForm(li); } catch (_) {}
    const cmd = `wb run ${li.dataset.id} --url ${shellQuote(url)}` +
      (Object.keys(params).length ? ` --params ${shellQuote(JSON.stringify(params))}` : "");
    await navigator.clipboard.writeText(cmd);
    status.textContent = "命令已复制 ✓";
    setTimeout(() => (status.textContent = ""), 1500);
  });

  runBtn.addEventListener("click", async () => {
    let params;
    try {
      params = readForm(li);
    } catch (e) {
      out.className = "out show err";
      out.textContent = e.message;
      return;
    }
    runBtn.disabled = true;
    status.textContent = "运行中…";
    out.className = "out show";
    out.textContent = "…";
    try {
      const data = await api("/capability/" + encodeURIComponent(li.dataset.id), {
        method: "POST",
        body: JSON.stringify({ params, url, timeout_ms: 120000 }),
      });
      const result = data.result;
      const text = typeof result === "string" ? result : JSON.stringify(result, null, 2);
      out.className = "out show";
      out.textContent = text === undefined ? "(无返回值)" : text;
      status.innerHTML = "";
      addResultActions(status, out, text || "", li.dataset.id);
    } catch (e) {
      out.className = "out show err";
      out.textContent = e.message;
      status.textContent = "";
    } finally {
      runBtn.disabled = false;
    }
  });
}

function addResultActions(status, out, text, capId) {
  const copy = document.createElement("a");
  copy.textContent = "复制结果";
  copy.style.cursor = "pointer";
  copy.addEventListener("click", async () => {
    await navigator.clipboard.writeText(text);
    copy.textContent = "已复制 ✓";
    setTimeout(() => (copy.textContent = "复制结果"), 1200);
  });
  // A plain download anchor, deliberately: chrome.downloads would mean adding a
  // manifest permission, and a permission change can only be picked up by a
  // manual reload in chrome://extensions — not worth it for a save button.
  const dl = document.createElement("a");
  dl.textContent = "下载";
  dl.style.cssText = "cursor:pointer;margin-left:8px";
  dl.download = `${capId}.json`;
  dl.href = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  status.append(copy, dl);
}

// --------------------------------------------------------------------------- //
(async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = tab?.url || "";
  $("url").textContent = url || "(无标签页)";

  try {
    const health = await api("/health");
    $("dot").classList.toggle("on", !!health.extension_connected);
    $("dot").title = health.extension_connected ? "扩展已连接 bridge" : "扩展未连接";
  } catch (e) {
    $("dot").title = "bridge 未运行";
    $("body").innerHTML =
      '<div class="empty">本地 bridge 未运行<br><br>' +
      '<code>python3 ~/cc/web-bridge/bridge/cli.py status</code></div>';
    return;
  }

  // Only suggest --url with a real web host: on chrome-extension:// / chrome://
  // the "hostname" is the extension id, which produced a nonsense example.
  const host = (() => {
    try {
      const u = new URL(url);
      return /^https?:$/.test(u.protocol) ? u.hostname : "";
    } catch { return ""; }
  })();

  try {
    const data = await api("/capabilities?url=" + encodeURIComponent(url));
    render(data.capabilities || [], url, host);
    $("count").textContent = `${data.count} 个能力可用`;
  } catch (e) {
    $("body").innerHTML = `<div class="empty">读取能力失败：${esc(e.message)}</div>`;
  }
})();

$("reload").addEventListener("click", async (e) => {
  e.preventDefault();
  $("reload").textContent = "重载中…";
  chrome.runtime.reload();
});
