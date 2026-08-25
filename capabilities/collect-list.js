/* @web-bridge-capability
{
  "id": "collect-list",
  "title": "翻页采集列表",
  "description": "自动化采集：按选择器抓取列表项，可自动点「下一页」或滚动加载，直到达到数量上限或没有更多。适合抓搜索结果、商品列表、时间线。",
  "kind": "automate",
  "match": ["*"],
  "params": {
    "item": {"type": "string", "required": true, "description": "列表项选择器，如 '.product-card'（先跑 inspect-page 拿推荐选择器）"},
    "fields": {"type": "object", "description": "字段映射 {名称: 选择器}，相对列表项；值取文本，选择器以 @attr 结尾则取属性，如 {\"链接\": \"a@href\"}"},
    "next": {"type": "string", "description": "「下一页」按钮选择器；不填则用滚动加载"},
    "max_items": {"type": "number", "default": 200, "min": 1, "description": "最多采集条数"},
    "max_pages": {"type": "number", "default": 10, "min": 1, "description": "最多翻多少页/滚动多少轮"},
    "wait_ms": {"type": "number", "default": 1200, "description": "每轮之后等待毫秒"}
  }
}
*/
if (!args.item) return { ok: false, error: "缺少参数 item（列表项选择器）" };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const wait = args.wait_ms ?? 1200;
const maxItems = args.max_items ?? 200;
const maxPages = args.max_pages ?? 10;

function readItem(el) {
  if (!args.fields || !Object.keys(args.fields).length) {
    return { text: (el.innerText || "").trim().replace(/\s+/g, " ").slice(0, 400) };
  }
  const row = {};
  for (const [name, sel] of Object.entries(args.fields)) {
    let s = sel, attr = null;
    const at = sel.lastIndexOf("@");
    if (at > 0) { s = sel.slice(0, at); attr = sel.slice(at + 1); }
    const target = s ? el.querySelector(s) : el;
    if (!target) { row[name] = null; continue; }
    row[name] = attr
      ? target.getAttribute(attr) || (attr === "href" || attr === "src" ? target[attr] : null)
      : (target.innerText || target.textContent || "").trim().replace(/\s+/g, " ");
  }
  return row;
}

const seen = new Set();
const items = [];
let pages = 0;
let stalled = 0;

const harvest = () => {
  for (const el of document.querySelectorAll(args.item)) {
    if (items.length >= maxItems) break;
    // dedupe on the item's own text+href signature (DOM nodes get recycled by
    // virtualised lists, so identity is not reliable)
    const sig = ((el.innerText || "").slice(0, 120) + "|" +
                 (el.querySelector("a")?.getAttribute("href") || "")).trim();
    if (seen.has(sig)) continue;
    seen.add(sig);
    items.push(readItem(el));
  }
};

harvest();

while (items.length < maxItems && pages < maxPages) {
  const before = items.length;
  if (args.next) {
    const btn = document.querySelector(args.next);
    if (!btn || btn.disabled || btn.getAttribute("aria-disabled") === "true") break;
    btn.click();
  } else {
    window.scrollTo(0, document.body.scrollHeight);
  }
  pages++;
  await sleep(wait);
  harvest();
  if (items.length === before) {
    if (++stalled >= 2) break;  // two rounds with nothing new = done
  } else {
    stalled = 0;
  }
}

return {
  ok: true,
  count: items.length,
  pages_advanced: pages,
  truncated: items.length >= maxItems,
  items,
};
