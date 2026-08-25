/* @web-bridge-capability
{
  "id": "reader-mode",
  "title": "阅读模式重排",
  "description": "把当前页面重排成干净的阅读版式：居中单栏、宋体/无衬线可选、加大字号行距、隐藏导航与广告。就地改造，不跳转。再次运行可用 restore 参数还原。",
  "kind": "restyle",
  "match": ["*"],
  "params": {
    "width": {"type": "number", "default": 760, "min": 320, "max": 2000, "description": "正文宽度 px"},
    "font_size": {"type": "number", "default": 19, "min": 10, "max": 48, "description": "字号 px"},
    "theme": {"type": "string", "default": "light", "enum": ["light", "sepia", "dark"], "description": "配色主题"},
    "restore": {"type": "boolean", "default": false, "description": "还原页面原样"}
  }
}
*/
const STYLE_ID = "__wb_reader_style";
const MARK = "__wbReaderOriginal";

if (args.restore) {
  document.getElementById(STYLE_ID)?.remove();
  if (window[MARK]) {
    document.body.innerHTML = window[MARK];
    delete window[MARK];
  }
  return { restored: true };
}

const THEMES = {
  light: { bg: "#ffffff", fg: "#1a1a1a", muted: "#666", link: "#0b66c3" },
  sepia: { bg: "#f4ecd8", fg: "#3b3226", muted: "#7a6a53", link: "#8a5a2b" },
  dark:  { bg: "#16181c", fg: "#e6e6e6", muted: "#9aa0a6", link: "#7cb7ff" },
};
const th = THEMES[args.theme] || THEMES.light;

// find the main content (same heuristic family as extract-article)
const NOISE = /nav|menu|header|footer|sidebar|aside|comment|advert|promo|banner|share|related|cookie|subscribe/i;
const textLen = (el) => (el.innerText || "").replace(/\s+/g, " ").trim().length;

let main =
  document.querySelector("article") ||
  document.querySelector("main") ||
  document.querySelector('[role="main"]');

if (!main || textLen(main) < 200) {
  let best = null, bestScore = 0;
  for (const el of document.querySelectorAll("article, main, section, div")) {
    if (el.children.length > 400) continue;
    const len = textLen(el);
    if (len < 200) continue;
    let s = len;
    if (NOISE.test(`${el.className || ""} ${el.id || ""}`)) s *= 0.2;
    const linkLen = [...el.querySelectorAll("a")].reduce((n, a) => n + textLen(a), 0);
    if (len) s *= 1 - Math.min(0.9, linkLen / len);
    if (s > bestScore) { bestScore = s; best = el; }
  }
  main = best;
}
if (!main) return { ok: false, error: "未找到正文区域" };

// keep one copy of the original DOM so `restore` is lossless within this page load
if (!window[MARK]) window[MARK] = document.body.innerHTML;

const title = document.querySelector("h1")?.innerText?.trim() || document.title;
const html = main.innerHTML;

document.getElementById(STYLE_ID)?.remove();
const style = document.createElement("style");
style.id = STYLE_ID;
style.textContent = `
  html, body { background: ${th.bg} !important; }
  body { margin: 0 !important; padding: 0 !important; }
  #__wb_reader {
    max-width: ${args.width ?? 760}px; margin: 0 auto; padding: 48px 24px 120px;
    font-size: ${args.font_size ?? 19}px; line-height: 1.75; color: ${th.fg};
    font-family: -apple-system, "PingFang SC", "Helvetica Neue", Georgia, serif;
  }
  #__wb_reader h1 { font-size: 1.9em; line-height: 1.25; margin: 0 0 .2em; }
  #__wb_reader h2 { font-size: 1.4em; margin: 1.8em 0 .5em; }
  #__wb_reader h3 { font-size: 1.15em; margin: 1.5em 0 .4em; }
  #__wb_reader p  { margin: 0 0 1.15em; }
  #__wb_reader a  { color: ${th.link}; }
  #__wb_reader img, #__wb_reader video { max-width: 100%; height: auto; display: block; margin: 1.5em auto; border-radius: 6px; }
  #__wb_reader pre { background: rgba(127,127,127,.12); padding: 14px; border-radius: 6px; overflow-x: auto; font-size: .9em; }
  #__wb_reader code { background: rgba(127,127,127,.14); padding: .12em .35em; border-radius: 3px; }
  #__wb_reader blockquote { margin: 1.2em 0; padding-left: 1em; border-left: 3px solid ${th.muted}; color: ${th.muted}; }
  #__wb_reader table { border-collapse: collapse; width: 100%; margin: 1.4em 0; font-size: .92em; }
  #__wb_reader th, #__wb_reader td { border: 1px solid rgba(127,127,127,.35); padding: 8px 10px; text-align: left; }
  #__wb_meta { color: ${th.muted}; font-size: .82em; margin: 0 0 2.2em; }
`;
document.head.appendChild(style);

document.body.innerHTML =
  `<div id="__wb_reader"><h1>${title.replace(/</g, "&lt;")}</h1>` +
  `<div id="__wb_meta">${location.hostname}</div>${html}</div>`;
window.scrollTo(0, 0);

return { ok: true, title, theme: args.theme || "light", chars: (main.innerText || "").length };
