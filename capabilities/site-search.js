/* @web-bridge-capability
{
  "id": "site-search",
  "title": "站内搜索（通用）",
  "description": "在当前网站上搜一个词并把结果带回来：自动找到页面的搜索框，弄清它提交到哪个地址，然后用同源请求把结果页取回来解析成 {标题, 链接, 摘要} 列表——不跳转、不动用户正在看的页面。搜索框是 JS 即时出结果（下拉联想那种）时会退化为「在页面里真的输入并等结果」。适合在文档站、论坛、电商、内网系统里查东西，尤其是登录后才搜得到的内容。解析不理想时返回的 results_url 可以直接 wb open + collect-list 接着抓。",
  "kind": "automate",
  "match": ["*"],
  "params": {
    "query": {"type": "string", "required": true, "description": "要搜的词"},
    "count": {"type": "number", "default": 20, "min": 1, "max": 100, "description": "最多返回多少条结果"},
    "selector": {"type": "string", "description": "手动指定搜索框选择器（自动找错了才需要）"},
    "mode": {"type": "string", "default": "auto", "enum": ["auto", "fetch", "in-page"], "description": "auto=先试提交地址再退化；fetch=只用同源请求取结果页；in-page=只在页面里输入等结果"},
    "wait_ms": {"type": "number", "default": 6000, "min": 500, "description": "in-page 模式等结果出现的毫秒数"}
  }
}
*/
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
const count = args.count ?? 20;

// --------------------------------------------------------------------------- //
// 1. find the search box
// --------------------------------------------------------------------------- //
const NAME_RE = /^(q|s|k|kw|wd|word|query|search|keyword|keywords|term|text|search_query|searchTerm)$/i;
const HINT_RE = /search|搜索|查找|検索|поиск|buscar/i;

function visible(el) {
  if (!el || el.disabled || el.readOnly) return false;
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width > 40 && r.height > 8 && st.visibility !== "hidden" && st.display !== "none";
}

function findInput(soft) {
  if (args.selector) {
    const el = document.querySelector(args.selector);
    if (!el) throw new Error(`selector 没匹配到元素：${args.selector}`);
    return el;
  }
  // Pierce open shadow roots: web-component search widgets are invisible to a
  // plain querySelectorAll, and the page then looks like it has no search box.
  const deepAll = (sel, root = document, out = []) => {
    out.push(...root.querySelectorAll(sel));
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) deepAll(sel, el.shadowRoot, out);
    }
    return out;
  };
  const all = deepAll('input:not([type=hidden]), [role="searchbox"], [contenteditable="true"]');
  const score = (el) => {
    const type = (el.getAttribute("type") || "").toLowerCase();
    const name = el.getAttribute("name") || "";
    const hay = `${name} ${el.id} ${el.className} ${el.getAttribute("placeholder") || ""} ${el.getAttribute("aria-label") || ""}`;
    let s = 0;
    if (type === "search" || el.getAttribute("role") === "searchbox") s += 5;
    if (NAME_RE.test(name)) s += 4;
    if (HINT_RE.test(hay)) s += 3;
    if (el.closest('form[role="search"], form[action*="search"], form[action*="Search"]')) s += 3;
    if (type && !["search", "text", ""].includes(type)) s -= 6;   // email/password/number…
    if (!visible(el)) s -= 4;                                      // still allow, but last
    return s;
  };
  const best = all.map((el) => [score(el), el]).filter(([s]) => s > 0).sort((a, b) => b[0] - a[0])[0];
  if (!best) {
    // A declared search form beats any visibility heuristic: the input may be
    // collapsed behind an icon (narrow layouts) yet still be the right one.
    const declared = document.querySelector(
      'form[role="search"] input[name], form[action*="search" i] input[name]:not([type=hidden])');
    if (declared) return declared;
  }
  if (!best) {
    if (soft) return null;
    throw new Error("这个页面上找不到搜索框——用 selector 参数手动指一个，或先跑 inspect-page 看看有哪些表单");
  }
  return best[1];
}

// Modern sites often hide the input behind a trigger (GitHub's "Search or jump
// to…", cmd-K palettes): there is no search box in the DOM until you click.
let openedDialog = false;
async function findInputOrOpen() {
  try {
    return findInput();
  } catch (notFound) {
    if (args.selector) throw notFound;
    const trigger = [...document.querySelectorAll('button, [role="button"], summary, [data-testid*="search" i]')]
      .find((b) => visible(b) && HINT_RE.test(`${b.getAttribute("aria-label") || ""} ${b.getAttribute("placeholder") || ""} ${clean(b.textContent)}`));
    if (!trigger) throw notFound;
    trigger.click();
    openedDialog = true;
    await sleep(900);
    return findInput(true);        // may still be null: some palettes ignore a
                                   // synthetic click (GitHub), and that is fine —
                                   // the guessed-endpoint path doesn't need an input
  }
}

let input = null;
try {
  input = await findInputOrOpen();
} catch (e) {
  input = null;                    // remember why, but let the guess path try
  var findError = e;
}
const form = input ? input.closest("form") : null;

// --------------------------------------------------------------------------- //
// 2. work out where the search submits to, WITHOUT submitting
//    (submitting navigates, which would kill this script mid-run)
// --------------------------------------------------------------------------- //
function buildRequest() {
  if (!form || !input) return null;
  const name = input.getAttribute("name");
  if (!name) return null;                        // nothing to send it as
  const method = (form.getAttribute("method") || "get").toLowerCase();
  const action = form.getAttribute("action") || location.pathname + location.search;
  const url = new URL(action, location.href);
  // carry the form's other fields (hidden tokens, scope selectors, …) so the
  // request looks like the one the site itself would have made
  const data = new URLSearchParams();
  for (const el of form.elements) {
    if (!el.name || el.disabled) continue;
    if (el === input) continue;
    if (["submit", "button", "image", "file"].includes((el.type || "").toLowerCase())) continue;
    if (["checkbox", "radio"].includes((el.type || "").toLowerCase()) && !el.checked) continue;
    data.append(el.name, el.value ?? "");
  }
  data.append(name, args.query);
  return { method, url, data };
}

function absolutise(href, base) {
  try { return new URL(href, base).href; } catch { return null; }
}

// --------------------------------------------------------------------------- //
// 3. pull result rows out of a document, without knowing the site
//    (same idea as inspect-page: the results are the biggest group of sibling
//     blocks that each contain a substantial link)
// --------------------------------------------------------------------------- //
function harvest(doc, baseUrl, limit) {
  const groups = new Map();
  for (const a of doc.querySelectorAll("a[href]")) {
    const text = clean(a.textContent);
    const href = a.getAttribute("href") || "";
    if (text.length < 8 || href.startsWith("#") || /^(javascript|mailto|tel):/i.test(href)) continue;
    if (a.closest("nav, header, footer, [role=navigation], [role=banner], [role=contentinfo]")) continue;
    // climb to the row: the ancestor that is a sibling among similar rows
    let node = a;
    for (let i = 0; i < 4 && node.parentElement; i++) {
      node = node.parentElement;
      const parent = node.parentElement;
      if (!parent || parent.children.length < 3) continue;
      const key = parent;
      if (!groups.has(key)) groups.set(key, new Map());
      const rows = groups.get(key);
      if (!rows.has(node)) rows.set(node, a);
    }
  }
  // Does this group of rows mention what we searched for? MDN's top navigation
  // ("HTML: Markup language", "CSS: Styling language") is a perfectly uniform
  // group of link-bearing rows — structurally indistinguishable from results,
  // and it was being returned as if it were the answer. Relevance separates them.
  const words = clean(args.query).toLowerCase().split(/\s+/).filter((w) => w.length >= 3);
  const relevance = (rows) => {
    if (!words.length) return 1;
    let hit = 0;
    for (const node of rows.keys()) {
      const t = clean(node.textContent).toLowerCase();
      if (words.some((w) => t.includes(w))) hit++;
    }
    return hit / rows.size;
  };

  let bestRows = null, bestScore = 0;
  for (const [parent, rows] of groups) {
    if (rows.size < 2) continue;
    const texts = [...rows.keys()].map((n) => clean(n.textContent).length);
    const avg = texts.reduce((x, y) => x + y, 0) / texts.length;
    if (avg < 20) continue;                      // nav bars: many links, little text
    const rel = relevance(rows);
    if (rel === 0) continue;                     // nothing in this group mentions the query
    const score = rows.size * Math.min(avg, 400) * (0.5 + rel);
    if (score > bestScore) { bestScore = score; bestRows = rows; }
  }
  if (!bestRows) return [];
  const out = [];
  const seen = new Set();
  // Links that point back at the search page itself are the result-page's own
  // filters/facets/pagination, not results. On a JS-rendered results page (where
  // the real results aren't in the HTML at all) those are ALL that harvest finds,
  // and returning them looks like a successful search — worse than returning
  // nothing. GitHub's sidebar was exactly this.
  const norm = (p) => (p || "").replace(/\/+$/, "");
  let selfPath = null;
  try { selfPath = norm(new URL(baseUrl).pathname); } catch (_) {}
  const queryWords = clean(args.query).toLowerCase();
  const isFacet = (u) => {
    try {
      const url = new URL(u);
      if (selfPath && norm(url.pathname) === selfPath) return true;       // /search vs /search/
      // a link that carries the query text in its own params is a refinement
      // ("same search, filtered by language/type"), not a result
      for (const v of url.searchParams.values()) {
        if (clean(v).toLowerCase().includes(queryWords)) return true;
      }
    } catch (_) {}
    return false;
  };
  for (const [row, link] of bestRows) {
    const url = absolutise(link.getAttribute("href"), baseUrl);
    if (!url || seen.has(url) || isFacet(url)) continue;
    seen.add(url);
    const title = clean(link.textContent);
    const snippet = clean(row.textContent).replace(title, "").trim().slice(0, 300);
    out.push({ rank: out.length + 1, title, url, snippet: snippet || null });
    if (out.length >= limit) break;
  }
  return out;
}

// --------------------------------------------------------------------------- //
// 4a. fetch mode — ask the site's own search endpoint and parse the answer
// --------------------------------------------------------------------------- //
async function viaFetch() {
  const req = buildRequest();
  if (!req) return null;
  let resp, resultsUrl;
  if (req.method === "post") {
    resultsUrl = req.url.href;
    resp = await fetch(resultsUrl, {
      method: "POST", credentials: "include",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: req.data.toString(),
    });
  } else {
    req.url.search = req.data.toString();
    resultsUrl = req.url.href;
    resp = await fetch(resultsUrl, { credentials: "include" });
  }
  if (!resp.ok) return { results_url: resultsUrl, status: resp.status, results: [] };
  // Many sites jump straight to the matching page when the query is an exact
  // title ("I'm feeling lucky" behaviour — Wikipedia does it). Parsing that page
  // as a result list produces nonsense rows, so notice the redirect instead.
  const finalUrl = resp.url || resultsUrl;
  const redirected = finalUrl.replace(/[?#].*$/, "") !== resultsUrl.replace(/[?#].*$/, "");
  const type = resp.headers.get("content-type") || "";
  const body = await resp.text();
  if (/json/i.test(type)) {
    // a JSON search API: hand it back whole rather than guessing its shape
    let data = null;
    try { data = JSON.parse(body); } catch (_) {}
    return { results_url: resultsUrl, json: data, results: [],
             note: "站点搜索返回的是 JSON，已原样带回（json 字段），结构因站而异" };
  }
  const doc = new DOMParser().parseFromString(body, "text/html");
  const results = harvest(doc, finalUrl, count);
  if (redirected && results.length < 3) {
    return {
      results_url: resultsUrl,
      results: [],
      direct_hit: { url: finalUrl, title: clean(doc.querySelector("title")?.textContent) },
      note: "站点把这个查询直接跳到了具体页面（精确命中），没有结果列表。" +
            "想看正文就对 direct_hit.url 跑 extract-article",
    };
  }
  return { results_url: finalUrl, results };
}

// --------------------------------------------------------------------------- //
// 4a-bis. no usable form? try the conventional search endpoints.
//    Sites whose box is a JS palette (GitHub, cmd-K docs sites) have no form to
//    read, but almost all of them still answer a plain GET on one of these.
// --------------------------------------------------------------------------- //
const GUESSES = ["/search?q=", "/search?query=", "/search/?q=", "/?s=", "/search?keyword="];

async function viaGuess() {
  let answered = null;                            // an endpoint that exists but whose
                                                  // results we couldn't parse (JS-rendered)
  for (const pattern of GUESSES) {
    const url = location.origin + pattern + encodeURIComponent(args.query);
    let resp;
    try {
      resp = await fetch(url, { credentials: "include" });
    } catch (_) { continue; }
    if (!resp.ok) continue;
    if (!/html/i.test(resp.headers.get("content-type") || "")) continue;
    const finalUrl = resp.url || url;
    const doc = new DOMParser().parseFromString(await resp.text(), "text/html");
    const results = harvest(doc, finalUrl, count);
    if (results.length >= 3) return { results_url: finalUrl, results, guessed: pattern };
    if (!answered) answered = { results_url: finalUrl, results: [], guessed: pattern, unparsed: true };
  }
  return answered;                                // knowing WHERE the search lives is
                                                  // still useful even unparsed
}

// --------------------------------------------------------------------------- //
// 4b. in-page mode — type it and watch what appears (SPA / instant search)
// --------------------------------------------------------------------------- //
async function viaPage() {
  if (!input) throw (typeof findError !== "undefined" ? findError :
    new Error("页面上没有可用的搜索框"));
  const before = new Set([...document.querySelectorAll("a[href]")].map((a) => a.href));
  input.focus();
  if (input.isContentEditable) {
    document.execCommand("selectAll", false, null);
    document.execCommand("insertText", false, args.query);
  } else {
    // React/Vue listen to the value setter, so poking .value directly is ignored
    const proto = Object.getPrototypeOf(input);
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    setter ? setter.call(input, args.query) : (input.value = args.query);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  for (const type of ["keydown", "keyup"]) {
    input.dispatchEvent(new KeyboardEvent(type, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
  }
  const deadline = Date.now() + (args.wait_ms ?? 6000);
  let fresh = [];
  while (Date.now() < deadline) {
    await sleep(400);
    fresh = [...document.querySelectorAll("a[href]")].filter(
      (a) => !before.has(a.href) && clean(a.textContent).length >= 8);
    if (fresh.length >= 3) break;
  }
  const seen = new Set();
  const results = [];
  for (const a of fresh) {
    if (seen.has(a.href)) continue;
    seen.add(a.href);
    const row = a.closest("li, article, tr, [role=option], div") || a;
    const title = clean(a.textContent);
    results.push({ rank: results.length + 1, title, url: a.href,
                   snippet: clean(row.textContent).replace(title, "").trim().slice(0, 300) || null });
    if (results.length >= count) break;
  }
  return { results_url: location.href, results, in_page: true };
}

// --------------------------------------------------------------------------- //
const describe = {
  input: !input ? null : (args.selector || input.getAttribute("name") || input.getAttribute("aria-label") ||
         input.getAttribute("placeholder") || input.id || input.tagName.toLowerCase()),
  has_form: !!form,
  opened_dialog: openedDialog || undefined,
};

let out = null;
if (args.mode !== "in-page") {
  out = await viaFetch();
  if (!out || (!out.results.length && !out.direct_hit && !out.json)) {
    out = (await viaGuess()) || out;
  }
}
// Typing into the page is the last resort — and only possible if we actually
// found an input. A guessed endpoint that exists but rendered its results in JS
// is a better answer than an exception.
if (input && (!out || (!out.results.length && !out.direct_hit)) && args.mode !== "fetch") {
  const page = await viaPage();
  // keep the fetch answer if it at least found the endpoint and returned JSON
  if (page.results.length || !out || (!out.results.length && !out.json)) {
    out = { ...page, fetch_results_url: out ? out.results_url : undefined };
  }
}
if (!out) {
  throw (typeof findError !== "undefined" && findError) || new Error(
    "推断不出这个站的搜索地址：搜索框没有 name/表单，常见搜索路径也没命中。" +
    "用 selector 指定输入框，或先跑 inspect-page 看看这个页面有哪些表单");
}

if (openedDialog && !out.in_page) {
  // we only opened the palette to read the input's shape; put it away again
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", keyCode: 27, bubbles: true }));
}

return {
  query: args.query,
  site: location.hostname,
  searched_with: describe,
  mode: out.in_page ? "in-page" : "fetch",
  results_url: out.results_url,
  guessed_endpoint: out.guessed,
  count: out.results.length,
  results: out.results,
  direct_hit: out.direct_hit,
  json: out.json,
  note: out.note || (out.results.length ? undefined :
        (out.unparsed
          ? "找到了这个站的搜索地址，但结果是 JS 渲染的、HTML 里没有内容。" +
            "用 wb open " + out.results_url + " 打开它，再跑 inspect-page / collect-list"
          : "没解析出结果——用 wb open " + out.results_url + " 打开结果页，再跑 inspect-page / collect-list")),
};
