/* @web-bridge-capability
{
  "id": "x-posts",
  "title": "抓取 X(推特) 帖子",
  "description": "读取当前 X 页面上的帖子：在某条帖子的详情页就是整条串（thread）+ 回复，在主页/个人页/搜索结果页就是时间线。返回作者、时间、正文、互动数、图片和永久链接。X 不登录基本看不到内容，所以这类抓取只能在已登录的浏览器里做。",
  "kind": "extract",
  "match": ["x.com", "twitter.com"],
  "params": {
    "max_posts": {"type": "number", "default": 30, "min": 1, "max": 300, "description": "最多抓多少条"},
    "scroll": {"type": "boolean", "default": true, "description": "自动向下滚动加载更多（关掉则只抓当前可见的）"},
    "max_scrolls": {"type": "number", "default": 12, "min": 1, "description": "最多滚动多少轮"},
    "text_only": {"type": "boolean", "default": false, "description": "只返回正文文本数组，丢掉元数据"}
  }
}
*/
// X renders a virtualised timeline: nodes are recycled as you scroll, so posts
// must be harvested continuously into a keyed map rather than read once at the
// end — otherwise you get whatever happens to be on screen when scrolling stops.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clean = (s) => (s || "").replace(/ /g, " ").replace(/[ \t]+/g, " ").trim();

const maxPosts = args.max_posts ?? 30;
const posts = new Map();

function metric(article, testid) {
  const el = article.querySelector(`[data-testid="${testid}"]`);
  if (!el) return null;
  const label = el.getAttribute("aria-label") || clean(el.textContent);
  const m = label.match(/([\d.,]+\s*[KMkm万]?)/);
  return m ? m[1].trim() : null;
}

function harvest() {
  for (const a of document.querySelectorAll('article[data-testid="tweet"]')) {
    const timeEl = a.querySelector("time");
    const permalink = timeEl?.closest("a")?.href || null;
    const textEl = a.querySelector('[data-testid="tweetText"]');
    const text = clean(textEl?.innerText);
    // the permalink is the only stable identity; without one (ad, placeholder)
    // fall back to author+text so a real post is never dropped as a duplicate
    const nameBlock = clean(a.querySelector('[data-testid="User-Name"]')?.innerText).split("\n");
    const handle = (nameBlock.find((x) => x.startsWith("@")) || "").trim();
    const key = permalink || `${handle}::${text.slice(0, 60)}`;
    if (!text && !a.querySelector('[data-testid="tweetPhoto"]')) continue;
    if (posts.has(key)) continue;
    posts.set(key, {
      author: nameBlock[0] || null,
      handle: handle || null,
      time: timeEl?.getAttribute("datetime") || null,
      text,
      replies: metric(a, "reply"),
      reposts: metric(a, "retweet"),
      likes: metric(a, "like"),
      views: clean(a.querySelector('a[href$="/analytics"]')?.getAttribute("aria-label")) || null,
      images: [...a.querySelectorAll('[data-testid="tweetPhoto"] img')].map((i) => i.src),
      links: [...a.querySelectorAll('a[href^="http"]:not([href*="x.com"]):not([href*="twitter.com"])')]
        .map((l) => l.href),
      permalink,
    });
    if (posts.size >= maxPosts) return true;
  }
  return posts.size >= maxPosts;
}

let done = harvest();
if (args.scroll !== false && !done) {
  const maxScrolls = args.max_scrolls ?? 12;
  let lastHeight = -1, stall = 0;
  for (let i = 0; i < maxScrolls && !done; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await sleep(900);
    done = harvest();
    const h = document.body.scrollHeight;
    stall = h === lastHeight ? stall + 1 : 0;
    lastHeight = h;
    if (stall >= 2) break;                       // nothing new is loading anymore
  }
  window.scrollTo(0, 0);
}

const list = [...posts.values()].slice(0, maxPosts);
return args.text_only
  ? { url: location.href, count: list.length, texts: list.map((p) => p.text) }
  : {
      url: location.href,
      page_title: document.title,
      count: list.length,
      truncated: posts.size >= maxPosts,
      posts: list,
    };
