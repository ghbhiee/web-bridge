/* @web-bridge-capability
{
  "id": "google-search",
  "title": "Google 搜索",
  "description": "在 Google 上搜一个词，返回结构化结果（标题、链接、摘要、域名）。用页面自己的登录态和地区设置发同源请求再解析，不跳转、不动用户当前那个标签页的内容。要求有一个 www.google.com 的标签页（没有会自动开一个）——调用时 --url 写 www.google.com，别写 google.com（那会匹配到 Gmail 标签页）。",
  "kind": "extract",
  "match": ["www.google.com", "google.com/search"],
  "params": {
    "query": {"type": "string", "required": true, "description": "搜索词，支持 site: filetype: 等运算符"},
    "count": {"type": "number", "default": 10, "min": 1, "max": 30, "description": "要多少条结果"},
    "page": {"type": "number", "default": 1, "min": 1, "description": "第几页（每页按 count 算）"},
    "lang": {"type": "string", "description": "结果语言，如 en / zh-CN；不填用账号默认"},
    "recent": {"type": "string", "enum": ["day", "week", "month", "year"], "description": "只要多久以内的结果"}
  }
}
*/
// Navigation would kill this script mid-run, so the search goes out as a
// same-origin fetch and the HTML is parsed with DOMParser. Same cookies, same
// region, same personalisation as the user's own browser — just no page change.
const count = args.count ?? 10;
const page = args.page ?? 1;
const qs = new URLSearchParams({ q: args.query, num: String(count) });
if (page > 1) qs.set("start", String((page - 1) * count));
if (args.lang) qs.set("hl", args.lang);
if (args.recent) qs.set("tbs", { day: "qdr:d", week: "qdr:w", month: "qdr:m", year: "qdr:y" }[args.recent]);

const resp = await fetch("/search?" + qs.toString(), { credentials: "include" });
const html = await resp.text();
if (/\/sorry\/|unusual traffic/i.test(resp.url + html.slice(0, 2000))) {
  throw new Error("Google 要求人机验证（/sorry/）——在浏览器里手动搜一次过掉验证再重试");
}
if (/consent\.google\.com/i.test(resp.url)) {
  throw new Error("Google 拦在同意条款页——在浏览器里打开 google.com 点一次同意再重试");
}
const doc = new DOMParser().parseFromString(html, "text/html");

const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
const results = [];
const seen = new Set();

// A result is "an <h3> inside a link". Google's class names are obfuscated and
// churn constantly; this structural rule has outlived all of them.
for (const h3 of doc.querySelectorAll("#search h3, #rso h3")) {
  const a = h3.closest("a[href]");
  if (!a) continue;
  let url = a.getAttribute("href") || "";
  if (url.startsWith("/url?")) url = new URLSearchParams(url.slice(5)).get("q") || url;
  if (!/^https?:\/\//.test(url)) continue;
  if (seen.has(url)) continue;
  seen.add(url);

  // snippet: `data-sncf` is Google's own marker for the description block. It
  // survives the class-name churn, and unlike "block text minus the title" it
  // doesn't drag in the breadcrumb (which the markup repeats) or "Read more".
  const block = h3.closest("div[data-snc]") || h3.closest("div[data-hveid]") || h3.parentElement?.parentElement;
  let snippet = clean(
    block?.querySelector("div[data-sncf]")?.textContent ||
    block?.querySelector('div[style*="line-clamp"]')?.textContent || "");
  if (!snippet && block) {                       // last resort: strip what we know
    snippet = clean(block.textContent)
      .replace(clean(h3.textContent), "")
      .replace(/^Web results\s*/i, "")
      .replace(/\s*Read more$/i, "")
      .trim();
    try {
      const host = new URL(url).hostname;
      snippet = snippet.replace(new RegExp("(" + host.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "[^ ]*\\s*)+", "gi"), "").trim();
    } catch (_) {}
  }

  results.push({
    rank: results.length + 1 + (page - 1) * count,
    title: clean(h3.textContent),
    url,
    domain: (() => { try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return null; } })(),
    snippet: snippet.slice(0, 400) || null,
  });
  if (results.length >= count) break;
}

// related searches live in #botstuff. Scoping to it matters: querying the whole
// document also picks up the result-type tabs (Images / Videos / News / AI Mode),
// and the pagination links, which are not related searches at all.
const related = [...doc.querySelectorAll('#botstuff a[href^="/search?"]')]
  .map((e) => clean(e.textContent))
  .filter((t) => t && t.length > 2 && t.length < 60 && !/^\d+$/.test(t) &&
                 !/^(next|previous|上一页|下一页|更多)$/i.test(t))
  .slice(0, 8);

if (!results.length) {
  throw new Error("没解析出结果——可能 Google 换了页面结构，或这次返回的是验证页；" +
                  "先在浏览器里手动搜一次确认能出结果");
}

return {
  query: args.query,
  results_url: "https://www.google.com/search?" + qs.toString(),
  count: results.length,
  results,
  related_searches: related.length ? [...new Set(related)] : undefined,
};
