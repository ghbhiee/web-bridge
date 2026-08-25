/* @web-bridge-capability
{
  "id": "youtube-transcript",
  "title": "提取 YouTube 字幕",
  "description": "拿到当前 YouTube 视频的完整文字稿（标题、频道、时长 + 逐句字幕）。自动打开页面自带的「显示字幕」面板并读取，读完自动关回去；自动字幕和上传字幕都能取。适合把视频喂给模型做总结/翻译/摘要。",
  "kind": "extract",
  "match": ["youtube.com/watch", "youtu.be", "youtube.com/shorts"],
  "params": {
    "lang": {"type": "string", "description": "首选字幕语言，写语言名的一部分即可（如 中文 / English / auto-generated）；不填用默认那条"},
    "timestamps": {"type": "boolean", "default": false, "description": "返回带时间戳的分段（否则合并成整段文本）"},
    "max_chars": {"type": "number", "default": 200000, "min": 100, "description": "文字稿长度上限"},
    "wait_ms": {"type": "number", "default": 8000, "min": 1000, "description": "等待字幕面板加载的毫秒数"}
  }
}
*/
// Why the UI and not the API: youtube.com/api/timedtext now answers 200 with an
// empty body for a plain fetch, and /youtubei/v1/get_transcript answers
// "Precondition check failed" without the signed session headers. The transcript
// panel, however, is rendered by YouTube's own client with the user's session —
// driving it is both simpler and far less likely to rot.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const wait = args.wait_ms ?? 8000;

const segNodes = () => [...document.querySelectorAll("ytd-transcript-segment-renderer")];
const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

function findButton(re) {
  return [...document.querySelectorAll("button, tp-yt-paper-button, yt-button-shape button")]
    .find((b) => re.test((b.getAttribute("aria-label") || "") + " " + (b.textContent || "")));
}

async function until(fn, ms, step = 250) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    const v = fn();
    if (v && (!Array.isArray(v) || v.length)) return v;
    await sleep(step);
  }
  return null;
}

// 1. open the transcript panel (unless it is already showing)
let openedByUs = false;
if (!segNodes().length) {
  const open = findButton(/show transcript|显示(字幕|文字记录|文稿)|транскрип|transcripción|transcription/i);
  if (!open) {
    throw new Error("这个视频没有「显示字幕」按钮——通常表示它没有字幕，或字幕被上传者关闭了");
  }
  open.click();
  openedByUs = true;
  if (!(await until(segNodes, wait))) {
    throw new Error(`点开了字幕面板但 ${wait}ms 内没有加载出内容——网络慢的话把 wait_ms 调大`);
  }
}

// 2. optional language switch, through the panel's own language menu
let langNote = null;
if (args.lang) {
  const want = String(args.lang).toLowerCase();
  const trigger = document.querySelector(
    "ytd-transcript-footer-renderer #label, ytd-transcript-footer-renderer yt-dropdown-menu, ytd-transcript-footer-renderer tp-yt-paper-button");
  if (trigger) {
    trigger.click();
    const items = await until(
      () => [...document.querySelectorAll("tp-yt-paper-item, ytd-menu-service-item-renderer")]
        .filter((i) => clean(i.textContent)), 3000);
    const hit = (items || []).find((i) => clean(i.textContent).toLowerCase().includes(want));
    if (hit) {
      hit.click();
      await sleep(1200);
      await until(segNodes, wait);
    } else {
      langNote = `没有找到语言「${args.lang}」，用的是默认字幕；可选：` +
        (items || []).map((i) => clean(i.textContent)).join(" / ");
      document.body.click();                    // close the menu again
    }
  } else {
    langNote = "这个视频只有一种字幕，lang 参数被忽略";
  }
}

// 3. read the segments
const segments = segNodes().map((n) => ({
  t: clean(n.querySelector(".segment-timestamp, [class*=timestamp]")?.textContent),
  text: clean(n.querySelector(".segment-text, yt-formatted-string.segment-text")?.textContent),
})).filter((s) => s.text);

if (!segments.length) throw new Error("字幕面板是空的（视频可能刚发布，字幕还在生成）");

// #label is the dropdown TRIGGER (the selected language); the containing menu
// node's text is the whole option list, which is not what we want here
const currentLang = clean(document.querySelector(
  "ytd-transcript-footer-renderer #label, ytd-transcript-footer-renderer #trigger")?.textContent) || null;
const availableLangs = [...document.querySelectorAll(
  "ytd-transcript-footer-renderer tp-yt-paper-item")].map((i) => clean(i.textContent)).filter(Boolean);

// 4. put the page back the way we found it
if (openedByUs) {
  const close = findButton(/close transcript|关闭(字幕|文字记录|文稿)/i);
  if (close) close.click();
}

const meta = window.ytInitialPlayerResponse?.videoDetails || {};
const maxChars = args.max_chars ?? 200000;
const full = segments.map((s) => s.text).join(" ").slice(0, maxChars);

return {
  video_id: meta.videoId || new URL(location.href).searchParams.get("v"),
  title: meta.title || clean(document.querySelector("h1.ytd-watch-metadata")?.textContent),
  channel: meta.author || clean(document.querySelector("#owner #channel-name")?.textContent),
  duration_sec: Number(meta.lengthSeconds) || null,
  url: location.href.split("&")[0],
  language: currentLang,
  available_languages: availableLangs.length ? availableLangs : undefined,
  segment_count: segments.length,
  chars: full.length,
  note: langNote,
  transcript: args.timestamps ? segments : full,
};
