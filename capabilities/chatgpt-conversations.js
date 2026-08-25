/* @web-bridge-capability
{
  "id": "chatgpt-conversations",
  "title": "导出 ChatGPT 对话列表",
  "description": "用页面自己的登录态调 ChatGPT 后端接口，列出历史对话（标题、时间、id、链接）。站点专属能力的范例：走页面 API 而非 DOM，稳定且不受界面改版影响。注意项目(Project)内的对话不在默认列表里。",
  "kind": "extract",
  "match": ["chatgpt.com", "chat.openai.com"],
  "params": {
    "limit": {"type": "number", "default": 20, "min": 1, "max": 100, "description": "返回多少条"},
    "search": {"type": "string", "description": "按标题关键词过滤"}
  }
}
*/
const limit = Math.min(args.limit ?? 20, 100);

const r = await fetch(`/backend-api/conversations?offset=0&limit=${limit}&order=updated`, {
  credentials: "include",
  headers: { accept: "application/json" },
});
if (!r.ok) {
  return { ok: false, status: r.status,
           error: r.status === 401 ? "未登录，或该接口需要 Bearer token" : "接口返回非 200" };
}
const j = await r.json();

let items = (j.items || []).map((c) => ({
  id: c.id,
  title: c.title,
  created: c.create_time,
  updated: c.update_time,
  url: `https://chatgpt.com/c/${c.id}`,
}));
if (args.search) {
  const q = String(args.search).toLowerCase();
  items = items.filter((c) => (c.title || "").toLowerCase().includes(q));
}

// A project-scoped tab returns an empty top-level list: the project's chats live
// behind /backend-api/gizmos/<id>/conversations, which needs a Bearer token the
// page keeps in memory (cookies alone give 401). Say so instead of looking broken.
const proj = (location.href.match(/\/g\/(g-p-[a-z0-9]+)/i) || [])[1] || null;
const note = (!items.length && proj)
  ? `当前标签页在项目 ${proj} 内；项目对话不在默认列表中（其接口需 Bearer token，cookie 不足）。切到 chatgpt.com 主界面再跑可看到普通对话。`
  : undefined;

return { ok: true, total: j.total ?? items.length, count: items.length, project: proj, note, conversations: items };
