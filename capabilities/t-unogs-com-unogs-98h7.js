/* @web-bridge-capability
{
  "id": "t-unogs-com-unogs-98h7",
  "title": "按片名在 unogs 上查一部电影/",
  "description": "按片名在 unogs 上查一部电影/剧集在哪些国家的 Netflix 能看（对话里让 claude 写的，存为 Agent Tool）",
  "kind": "extract",
  "match": [
    "unogs.com"
  ],
  "params": {},
  "author": "claude"
}
*/
/* @web-bridge-capability {
  "id": "netflix-title-countries",
  "title": "查电影/剧集在哪些国家的 Netflix 能看",
  "kind": "extract", "match": ["unogs.com"],
  "params": { "query": {...}, "netflixid": {...}, "type": {...}, "detail": {...}, "limit": {...} }
} */
// 说明：按片名在 unogs 上查一部电影/剧集在哪些国家的 Netflix 能看
const A = typeof args === 'string' ? (args.trim().startsWith('{') ? JSON.parse(args) : { query: args }) : (args || {});
const q = String(A.query || A.title || '').trim();
if (!q && !A.netflixid) return { error: '需要 query（片名）或 netflixid' };

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
    cc: r.cc, country: String(r.country || '').trim(),
    since: r.newdate || null, expires: r.expiredate || null,
    audio: r.audio || '', subtitle: r.subtitle || '',
    hd: r.hd === '1', uhd: r.uhd === '1',
  })).sort((a, b) => a.country.localeCompare(b.country));
};

let cands = [];
if (A.netflixid) {
  const d = await get('/api/title/detail?netflixid=' + A.netflixid);
  cands = (Array.isArray(d) ? d : []).map(c => ({ ...c, nfid: c.netflixid }));
} else {
  // 这 17 个参数一个都不能少，否则搜不到结果
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
if (!cands.length) return { query: q, found: 0, titles: [], message: '没搜到这个片名（换英文原名再试）' };

// 标题完全一致的排前面，避免详情落到别的片上
const norm = s => String(s || '').toLowerCase().replace(/[^a-z0-9一-龥]+/g, '');
const exact = cands.filter(c => norm(c.title) === norm(q));
const ranked = exact.concat(cands.filter(c => !exact.includes(c)));
const detailN = Math.max(1, Math.min(Number(A.detail || 1), ranked.length));

const titles = [];
for (const c of ranked.slice(0, detailN)) {
  const list = await countriesOf(c.nfid);
  titles.push({
    title: c.title, year: c.year, type: c.vtype, netflixid: c.nfid, imdbid: c.imdbid,
    rating: c.avgrating, runtime_min: c.runtime ? Math.round(c.runtime / 60) : null,
    synopsis: c.synopsis,
    url: 'https://unogs.com/' + (c.vtype || 'movie') + '/' + c.nfid + '/' + (c.slug || ''),
    country_count: list.length, country_codes: list.map(x => x.cc).join(','), countries: list,
  });
}

return { query: q, found: cands.length, titles,
  other_matches: ranked.slice(detailN).map(c => ({ title: c.title, year: c.year, type: c.vtype, netflixid: c.nfid })) };