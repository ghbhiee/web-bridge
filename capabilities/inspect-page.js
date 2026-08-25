/* @web-bridge-capability
{
  "id": "inspect-page",
  "title": "探查页面结构",
  "description": "先探路再动手：报告页面有哪些可用的抓取入口——重复出现的列表结构（含推荐选择器与样例）、表格、表单、分页/加载更多按钮、结构化数据(JSON-LD)、以及页面暴露的 JS 全局。写 collect-list/自定义脚本前先跑这个。",
  "kind": "inspect",
  "match": ["*"],
  "params": {
    "max_candidates": {"type": "number", "default": 8, "min": 1, "max": 50, "description": "返回多少个列表候选"}
  }
}
*/
const txt = (el) => (el.innerText || "").replace(/\s+/g, " ").trim();

// ---- repeated-structure detection: the core of "what can I scrape here" ----
// Group elements by a structural signature; a signature seen many times with
// meaningful text is almost always the page's list item.
const groups = new Map();
for (const el of document.querySelectorAll("li, article, tr, div, section, a")) {
  const t = txt(el);
  if (t.length < 20 || t.length > 2000) continue;
  if (el.children.length > 60) continue;
  const cls = (typeof el.className === "string" ? el.className : "").trim().split(/\s+/).slice(0, 2).join(".");
  const sig = `${el.tagName.toLowerCase()}${cls ? "." + cls : ""}|${el.children.length}`;
  if (!groups.has(sig)) groups.set(sig, []);
  groups.get(sig).push(el);
}

const candidates = [];
for (const [sig, els] of groups) {
  if (els.length < 3) continue;
  const sel = sig.split("|")[0];
  // only keep signatures whose selector actually resolves to about this many nodes
  let hits = 0;
  try { hits = document.querySelectorAll(sel).length; } catch (_) { continue; }
  if (hits < 3) continue;
  const sample = els[0];
  const subFields = {};
  for (const tag of ["a", "h1", "h2", "h3", "h4", "img", "time", "span", "p"]) {
    const f = sample.querySelector(tag);
    if (f && (txt(f) || f.getAttribute("href") || f.getAttribute("src"))) {
      subFields[tag] = tag === "a" ? "a@href" : tag === "img" ? "img@src" : tag;
    }
  }
  candidates.push({
    selector: sel,
    count: hits,
    avg_text_len: Math.round(els.reduce((n, e) => n + txt(e).length, 0) / els.length),
    sample_text: txt(sample).slice(0, 160),
    suggested_fields: subFields,
  });
}
candidates.sort((a, b) => b.count * Math.min(b.avg_text_len, 300) - a.count * Math.min(a.avg_text_len, 300));

// ---- pagination / load-more ------------------------------------------------
const NEXT_RE = /next|下一页|下一頁|more|加载更多|載入更多|show more|载入更多|›|»/i;
const nextButtons = [...document.querySelectorAll('a, button, [role="button"]')]
  .filter((el) => NEXT_RE.test(txt(el)) || NEXT_RE.test(el.getAttribute("aria-label") || "") || el.rel === "next")
  .slice(0, 5)
  .map((el) => ({
    text: txt(el).slice(0, 40) || el.getAttribute("aria-label"),
    selector: el.id ? `#${el.id}` :
      (typeof el.className === "string" && el.className.trim()
        ? `${el.tagName.toLowerCase()}.${el.className.trim().split(/\s+/)[0]}`
        : el.tagName.toLowerCase()),
  }));

// ---- forms -----------------------------------------------------------------
const forms = [...document.forms].slice(0, 5).map((f) => ({
  action: f.getAttribute("action"),
  method: f.method,
  fields: [...f.elements]
    .filter((e) => e.name || e.id)
    .slice(0, 15)
    .map((e) => ({ name: e.name || e.id, type: e.type, placeholder: e.placeholder || undefined })),
}));

// ---- structured data -------------------------------------------------------
const jsonld = [];
for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
  try {
    const d = JSON.parse(s.textContent);
    jsonld.push({ type: d["@type"] || (Array.isArray(d) ? "array" : "?"), keys: Object.keys(d).slice(0, 12) });
  } catch (_) {}
}

// ---- page-world globals (only reachable because we run in MAIN world) ------
const globals = Object.keys(window).filter((k) => /^(__|_[A-Z]|\$|app|store|state|APP|Vue|React|ng)/.test(k)).slice(0, 20);

return {
  url: location.href,
  title: document.title,
  list_candidates: candidates.slice(0, args.max_candidates ?? 8),
  tables: document.querySelectorAll("table").length,
  pagination: nextButtons,
  forms,
  json_ld: jsonld,
  page_globals: globals,
  hint: "用 list_candidates[].selector 作为 collect-list 的 item 参数，suggested_fields 作为 fields 起点",
};
