/* @web-bridge-capability
{
  "id": "extract-article",
  "title": "提取正文",
  "description": "抽取页面主要正文（标题、作者、发布时间、正文纯文本/Markdown），自动剥离导航、广告、评论。适合把文章、博客、新闻喂给模型。",
  "kind": "extract",
  "match": ["*"],
  "params": {
    "markdown": {"type": "boolean", "default": true, "description": "正文转 Markdown（否则纯文本）"},
    "max_chars": {"type": "number", "default": 200000, "min": 100, "description": "正文长度上限"}
  }
}
*/
const meta = (sel, attr = "content") => {
  const el = document.querySelector(sel);
  return el ? (el.getAttribute(attr) || "").trim() : "";
};

// --- pick the main content node ------------------------------------------
// Prefer explicit semantics; otherwise score candidates by text density, which
// beats "longest node" on pages where a wrapper contains nav + article.
const NOISE = /nav|menu|header|footer|sidebar|aside|comment|advert|promo|banner|share|related|cookie|subscribe|paywall/i;

function textLen(el) {
  return (el.innerText || "").replace(/\s+/g, " ").trim().length;
}

function score(el) {
  const len = textLen(el);
  if (len < 200) return -1;
  const cls = `${el.className || ""} ${el.id || ""}`;
  let s = len;
  if (NOISE.test(cls)) s *= 0.2;
  // penalise link-heavy blocks (nav / lists of links)
  const linkLen = [...el.querySelectorAll("a")].reduce((n, a) => n + textLen(a), 0);
  if (len) s *= 1 - Math.min(0.9, linkLen / len);
  const p = el.querySelectorAll("p").length;
  s *= 1 + Math.min(1, p / 10);
  return s;
}

let main =
  document.querySelector("article") ||
  document.querySelector("main") ||
  document.querySelector('[role="main"]');

if (!main || textLen(main) < 200) {
  let best = null, bestScore = 0;
  const nodes = document.querySelectorAll("article, main, section, div");
  for (const el of nodes) {
    if (el.children.length > 400) continue; // huge wrappers
    const s = score(el);
    if (s > bestScore) { bestScore = s; best = el; }
  }
  main = best || document.body;
}

// --- serialize -----------------------------------------------------------
function toMarkdown(root) {
  const parts = [];
  const walk = (node) => {
    for (const el of node.children) {
      const tag = el.tagName;
      const t = (el.innerText || "").trim();
      if (!t && tag !== "IMG") continue;
      if (NOISE.test(`${el.className || ""} ${el.id || ""}`)) continue;
      if (/^H[1-6]$/.test(tag)) {
        parts.push("#".repeat(+tag[1]) + " " + t);
      } else if (tag === "P") {
        parts.push(t);
      } else if (tag === "LI") {
        parts.push("- " + t);
      } else if (tag === "PRE") {
        parts.push("```\n" + t + "\n```");
      } else if (tag === "BLOCKQUOTE") {
        parts.push(t.split("\n").map((l) => "> " + l).join("\n"));
      } else if (tag === "IMG") {
        if (el.src && el.naturalWidth > 100) parts.push(`![${el.alt || ""}](${el.src})`);
      } else if (el.children.length) {
        walk(el);
      } else {
        parts.push(t);
      }
    }
  };
  walk(root);
  return parts.join("\n\n").replace(/\n{3,}/g, "\n\n");
}

const body = args.markdown === false
  ? (main.innerText || "").trim()
  : toMarkdown(main);

return {
  title: meta('meta[property="og:title"]') || document.title,
  author: meta('meta[name="author"]') || meta('meta[property="article:author"]') || null,
  published: meta('meta[property="article:published_time"]') || meta('meta[name="date"]') ||
             (document.querySelector("time")?.getAttribute("datetime") || null),
  description: meta('meta[name="description"]') || meta('meta[property="og:description"]') || null,
  site: meta('meta[property="og:site_name"]') || location.hostname,
  url: location.href,
  lang: document.documentElement.lang || null,
  word_count: (main.innerText || "").trim().split(/\s+/).length,
  content: body.slice(0, args.max_chars ?? 200000),
};
