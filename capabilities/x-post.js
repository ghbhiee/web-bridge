/* @web-bridge-capability
{
  "id": "x-post",
  "title": "在 X(推特) 发帖 / 回复",
  "description": "用已登录的账号发一条帖子。在主页/个人页 = 发新帖；在某条帖子的详情页 = 回复那条帖子（所以回复就是先 --url 指向那条帖子再调用）。发送成功会尽量返回新帖子的永久链接。⚠️ 这是**对外发布**动作，内容会公开可见——调用前先跟用户确认文案；只想试试就用 dry_run 只填不发。",
  "kind": "automate",
  "match": ["x.com", "twitter.com"],
  "params": {
    "text": {"type": "string", "required": true, "description": "帖子正文（换行会保留）"},
    "dry_run": {"type": "boolean", "default": false, "description": "只把文字填进输入框、不点发送，用来确认文案和按钮状态"},
    "wait_ms": {"type": "number", "default": 15000, "min": 2000, "description": "等待发送完成的毫秒数"}
  }
}
*/
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
const waitMs = args.wait_ms ?? 15000;

async function until(fn, ms, step = 200) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    const v = fn();
    if (v) return v;
    await sleep(step);
  }
  return null;
}

const isReply = /\/status\/\d+/.test(location.pathname);

// The composer is lazily mounted on a post page — clicking the reply field
// mounts the real editor, so nudge it before looking for the editor itself.
let box = document.querySelector('[data-testid^="tweetTextarea_0"][contenteditable="true"]');
if (!box) {
  const placeholder = document.querySelector('[data-testid="tweetTextarea_0_label"], [data-testid="tweetTextarea_0"]');
  if (placeholder) { placeholder.click(); await sleep(600); }
  box = await until(() => document.querySelector('[data-testid^="tweetTextarea_0"][contenteditable="true"]'), 4000);
}
if (!box) {
  throw new Error("页面上没有发帖输入框——确认这个标签页是已登录的 x.com（主页/个人页发新帖，帖子详情页发回复）");
}

// X's editor is a rich-text component: assigning textContent doesn't register.
// execCommand("insertText") goes through the browser's own editing pipeline, so
// the editor's state updates and the Post button un-disables.
//
// But that insert RACES the editor's own async state update and sometimes gets
// applied twice — the composer ends up holding "文案文案". It is not
// deterministic (same call, empty composer: correct, then doubled, then
// correct), so there is no ordering trick that makes it safe. Since this
// capability PUBLISHES, a wrong composer means wrong text in public: write,
// then read back and verify, and retry from empty until it matches.
const setText = async (text) => {
  box.focus();
  await sleep(150);
  // Select the composer's own contents, not the document's: execCommand
  // ("selectAll") targets the whole document and leaves the editor's internal
  // selection where it was, so insertText appends instead of replacing.
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(box);
  sel.removeAllRanges();
  sel.addRange(range);
  document.execCommand("insertText", false, text);
  // No synthetic input event: execCommand already fires a real one.
  await sleep(700);
  return clean(box.textContent);
};

const wanted = clean(args.text);
let got = await setText(args.text);
for (let attempt = 0; attempt < 3 && got !== wanted; attempt++) {
  // clear explicitly, then write into a known-empty composer
  box.focus();
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(box);
  sel.removeAllRanges();
  sel.addRange(range);
  document.execCommand("delete", false, null);
  await sleep(300);
  got = await setText(args.text);
}
if (got !== wanted) {
  throw new Error(`输入框内容和要发的文案对不上（编辑器竞态没收敛）：想发 ${JSON.stringify(wanted)}，框里是 ${JSON.stringify(got)}。没有发送。`);
}

const typed = clean(box.textContent);
if (!typed) throw new Error("文字没能写进输入框（X 换了编辑器实现？）");

const button = () => document.querySelector('[data-testid="tweetButtonInline"], [data-testid="tweetButton"]');
const enabled = () => {
  const b = button();
  return b && b.getAttribute("aria-disabled") !== "true" && !b.disabled ? b : null;
};

if (args.dry_run) {
  return {
    dry_run: true,
    kind: isReply ? "reply" : "post",
    url: location.href,
    text_in_box: typed,
    button_enabled: !!enabled(),
    note: "没有发送。把 dry_run 去掉才会真的发出去",
  };
}

const btn = await until(enabled, 5000);
if (!btn) throw new Error("发送按钮一直是禁用状态（超字数？只有空白字符？账号受限？）");
btn.click();

// Sent = the composer goes empty again. The toast carries the new post's link,
// but it is short-lived, so grab it in the same poll rather than afterwards.
let permalink = null;
const sent = await until(() => {
  const link = document.querySelector('[data-testid="toast"] a[href*="/status/"]');
  if (link) permalink = link.href;
  const b = document.querySelector('[data-testid^="tweetTextarea_0"][contenteditable="true"]');
  return (b && !clean(b.textContent)) || !!link;
}, waitMs);

if (!sent) {
  throw new Error(`点了发送但 ${waitMs}ms 内没看到发出去的迹象——去页面上确认一下，别重复发`);
}
await sleep(400);
if (!permalink) {
  const link = document.querySelector('[data-testid="toast"] a[href*="/status/"]');
  if (link) permalink = link.href;
}

return {
  ok: true,
  kind: isReply ? "reply" : "post",
  text: args.text,
  permalink,                                   // null just means the toast was gone
  posted_from: location.href,
  note: permalink ? undefined : "没抓到永久链接（提示条消失得快），但帖子已发出",
};
