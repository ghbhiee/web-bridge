/* @web-bridge-capability {
  "id": "netflix-title-countries",
  "title": "查电影/剧集在哪些国家的 Netflix 能看",
  "description": "查一部电影/剧集在哪些国家的 Netflix 能看，返回国家码、上架/下架日期、音轨与字幕语言、HD/UHD。三种用法：①**已经在 unogs.com/title/<id> 详情页上就不用传参数**，自动读当前页面这部片；②传 query（片名）搜索；③传 netflixid 精确查询。需要浏览器有 unogs.com 页面（该站用 cookie 里的 authtoken 做 API 认证）。",
  "kind": "extract",
  "match": ["unogs.com"],
  "params": {
    "query": {"type": "string", "description": "可选。片名，英文原名命中率最高，例如 Inception / Breaking Bad。在 /title/<id> 详情页上可以不传"},
    "netflixid": {"type": "string", "description": "可选。已知 Netflix ID 时直接用它查，忽略 query。在 /title/<id> 页面上会自动从 URL 读出来"},
    "type": {"type": "string", "description": "可选。movie 或 series，用来过滤搜索结果"},
    "detail": {"type": "number", "description": "可选，默认 1。对前 N 个匹配结果拉取国家列表"},
    "limit": {"type": "number", "description": "可选，默认 10。搜索候选数量上限"},
    "start_year": {"type": "string", "description": "可选。起始年份，用于区分同名作品"},
    "end_year": {"type": "string", "description": "可选。结束年份"}
  }
} */
// 说明：按片名在 unogs 上查一部电影/剧集在哪些国家的 Netflix 能看
const A = typeof args === 'string' ? (args.trim().startsWith('{') ? JSON.parse(args) : { query: args }) : (args || {});
const q = String(A.query || A.title || '').trim();
// On a title page the id is already in the URL. Without this, asking "which
// countries is this on?" while looking at the answer's own page had to go
// through a name search -- a different enough shape that an agent reads the
// page text by hand instead, which is exactly what happened on 2026-08-27.
const fromUrl = (location.pathname.match(/\/title\/(\d+)/) || [])[1];
// An explicit query wins over the page you happen to be on: asking for
// Inception while sitting on the Dark Knight page must search, not answer
// about the current page.
const nfidArg = A.netflixid || (q ? '' : (fromUrl || ''));
if (!q && !nfidArg) return { error: '需要 query（片名）或 netflixid，或在 unogs.com/title/<id> 页面上运行' };

const tok = (document.cookie.match(/authtoken=([^;]+)/) || [])[1];
if (!tok) return { error: '当前页面没有 unogs 的 authtoken cookie，请先打开 https://unogs.com/' };
// unogs 的 API 同时校验 Bearer token 和自定义 REFERRER 头，缺一个就返回 fail:unogskey
const H = { 'Accept': 'application/json', 'REFERRER': 'http://unogs.com', 'Authorization': 'Bearer ' + tok };
const get = async (u) => {
  const r = await fetch(u, { headers: H, credentials: 'include' });
  const t = await r.text();
  try { return JSON.parse(t); } catch (e) { return { status: 'bad response: ' + t.slice(0, 200) }; }
};

const countriesOf = async (nfid) => {
  const rows = await get('/api/title/countries?netflixid=' + nfid);
  if (!Array.isArray(rows)) return [];
  return rows.map(r => ({
    cc: r.cc,
    country: String(r.country || '').trim(),
    since: r.newdate || null,
    expires: r.expiredate || null,
    audio: r.audio || '',
    subtitle: r.subtitle || '',
    hd: r.hd === '1',
    uhd: r.uhd === '1',
  })).sort((a, b) => a.country.localeCompare(b.country));
};

let cands = [];
if (nfidArg) {
  const d = await get('/api/title/detail?netflixid=' + nfidArg);
  cands = (Array.isArray(d) ? d : []).map(c => ({ ...c, nfid: c.netflixid }));
} else {
  const p = new URLSearchParams({
    limit: String(A.limit || 10), offset: '0', query: q,
    countrylist: '', country_andorunique: '',
    start_year: A.start_year || '', end_year: A.end_year || '',
    start_rating: '', end_rating: '', genrelist: '',
    type: A.type || '', audio: '', subtitle: '', audiosubtitle_andor: '',
    person: '', personid: '', filterby: '', orderby: 'Relevance',
  });
  const s = await get('/api/search?' + p);
  if (s.status) return { error: s.status };
  cands = s.results || [];
}
if (!cands.length) return { query: q, found: 0, titles: [], message: '没搜到这个片名（Netflix 全球目录里没有，或换英文原名再试）' };

// 标题完全一致的排前面，避免搜 Inception 时详情落到别的片上
const norm = s => String(s || '').toLowerCase().replace(/[^a-z0-9一-龥]+/g, '');
const exact = cands.filter(c => norm(c.title) === norm(q));
const ranked = exact.concat(cands.filter(c => !exact.includes(c)));
const detailN = Math.max(1, Math.min(Number(A.detail || 1), ranked.length));

const titles = [];
for (const c of ranked.slice(0, detailN)) {
  const list = await countriesOf(c.nfid);
  titles.push({
    title: c.title,
    year: c.year,
    type: c.vtype,
    netflixid: c.nfid,
    imdbid: c.imdbid,
    rating: c.avgrating,
    runtime_min: c.runtime ? Math.round(c.runtime / 60) : null,
    synopsis: c.synopsis,
    url: 'https://unogs.com/' + (c.vtype || 'movie') + '/' + c.nfid + '/' + (c.slug || ''),
    country_count: list.length,
    country_codes: list.map(x => x.cc).join(','),
    countries: list,
  });
}

return {
  query: q,
  found: cands.length,
  titles,
  other_matches: ranked.slice(detailN).map(c => ({ title: c.title, year: c.year, type: c.vtype, netflixid: c.nfid })),
};
