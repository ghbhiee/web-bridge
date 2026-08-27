/* @web-bridge-capability {
  "id": "tokencv-mail-send",
  "title": "用 TokenCV 邮箱发一封邮件",
  "description": "在 mail.tokencv.com 上发送邮件：走页面自己的登录态调 /api/send，不碰密码、不点界面。可指定用哪个账号发（服务器上有 hb / zb / admin / hilo1-4 等）。⚠️ 这个能力会真的把邮件发出去，调用前请确认收件人和内容。",
  "kind": "automate",
  "match": ["mail.tokencv.com"],
  "params": {
    "to": {"type": "string", "required": true, "description": "收件人邮箱，如 zb@tokencv.com"},
    "subject": {"type": "string", "required": true, "description": "邮件主题"},
    "body": {"type": "string", "required": true, "description": "正文纯文本；换行会转成 <br>"},
    "from": {"type": "string", "description": "可选。用哪个账号发（用户名，如 hb / zb）。不填就用当前登录的那个"}
  }
} */
// 说明：在 mail.tokencv.com 用当前登录态发一封邮件，返回发件人/收件人/主题以便核对
const A = args || {};
const to = String(A.to || '').trim();
const subject = String(A.subject || '').trim();
const body = String(A.body ?? '');
if (!to || !subject) return { error: '需要 to（收件人）和 subject（主题）' };

const me = await (await fetch('/api/me', { credentials: 'include' })).json();
// The account matters: this mailbox multiplexes several, and sending from the
// wrong one is invisible in the response — it succeeds either way.
if (A.from && me.username !== A.from) {
  if (typeof switchAccount !== 'function') return { error: '页面没有 switchAccount()，无法切账号；去掉 from 参数用当前账号发' };
  const idx = (me.accounts || []).indexOf(A.from);
  if (idx < 0) return { error: `账号 '${A.from}' 不在这个邮箱里。可用：${(me.accounts || []).join(', ')}` };
  await switchAccount(A.from);
  await new Promise(r => setTimeout(r, 600));
}
const now = await (await fetch('/api/me', { credentials: 'include' })).json();

const esc = (s) => s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
const res = await fetch('/api/send', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  credentials: 'include',
  body: JSON.stringify({
    to, subject, text: body,
    html: '<p>' + esc(body).replace(/\n/g, '<br>') + '</p>',
    attachments: [],
  }),
});
const data = await res.json().catch(() => ({}));
if (!res.ok || data.error) return { error: data.error || ('HTTP ' + res.status), from: now.username };
// Say who it actually went out as: "success" alone hides a send from the wrong
// account, which is the one mistake this page makes easy.
return { sent: true, from: now.username, to, subject, chars: body.length };
