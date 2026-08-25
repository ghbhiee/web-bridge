/* @web-bridge-capability
{
  "id": "perplexity-ask",
  "title": "Perplexity 提问",
  "description": "在已登录的 perplexity.ai 上提问并等待回答完成，返回答案正文与引用来源列表。复用用户账号，无需 API key。",
  "kind": "automate",
  "match": ["perplexity.ai"],
  "params": {
    "prompt": {"type": "string", "required": true, "description": "要问的问题"},
    "wait_ms": {"type": "number", "default": 120000, "description": "最长等待毫秒"}
  }
}
*/
if (!args.prompt) return { ok: false, error: "缺少 prompt" };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Date.now();
const deadline = now() + (args.wait_ms ?? 120000);

const ed = document.querySelector("#ask-input") ||
           document.querySelector('[contenteditable="true"]');
if (!ed) return { ok: false, error: "找不到输入框（页面未加载完或未登录）" };

// contenteditable: execCommand insertText is what the editor listens to
ed.focus();
document.execCommand("selectAll", false, null);
document.execCommand("insertText", false, args.prompt);
ed.dispatchEvent(new Event("input", { bubbles: true }));
await sleep(600);

// submit with Enter (Perplexity's submit button has no stable testid)
for (const type of ["keydown", "keypress", "keyup"]) {
  ed.dispatchEvent(new KeyboardEvent(type, {
    key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true,
  }));
}

// wait for navigation to a /search/ URL, then for the answer to stop growing
const startUrl = location.href;
while (now() < deadline && location.href === startUrl && !/\/search\//.test(location.href)) {
  await sleep(400);
}
await sleep(2500);

const answerText = () => {
  const main = document.querySelector("main") || document.body;
  return (main.innerText || "").trim();
};
let prev = "", stable = 0;
while (now() < deadline) {
  await sleep(1500);
  const cur = answerText();
  if (cur === prev && cur.length > 200) {
    if (++stable >= 3) break;      // unchanged for ~4.5s → generation finished
  } else {
    stable = 0; prev = cur;
  }
}

// citations: Perplexity renders source links with external hrefs
const sources = [...new Set(
  [...document.querySelectorAll('main a[href^="http"]')]
    .map((a) => a.href)
    .filter((h) => !/perplexity\.ai/.test(h))
)].slice(0, 30);

return {
  ok: true,
  url: location.href,
  answer: answerText().slice(0, 60000),
  sources,
  source_count: sources.length,
};
