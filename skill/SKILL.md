---
name: web-bridge
description: >
  用「用户浏览器里已登录的页面」完成任务：在任意站点的页面 JS 世界(MAIN world)执行代码、
  读取登录后才有的数据、调用站点自己的接口。当用户说「抓一下我登录后才能看到的数据」
  「在某网站页面上执行/读取…」「在页面里跑段 JS」「列一下我打开的标签页」「用我的浏览器…」
  时使用。底层是本地带 token 认证的 bridge + Chrome 扩展(MV3)。
  注意分工：**纯 ChatGPT 的活（问答 / 出图 / 让 ChatGPT 读文件）优先用 `chatgpt_image_osascript` skill**
  （osascript 零安装路线，扩展没连也能用）；本 skill 只在需要页面 JS 内存 / MAIN world /
  其它站点时才上，或者 chatgpt_image_osascript skill 明确不可用时兜底。
---

# web-bridge — 驱动已登录的浏览器页面

项目 `~/cc/web-bridge`。CLI：`python3 ~/cc/web-bridge/bridge/cli.py`（下称 `wb`）。
也可作为 MCP 使用（工具名 `web_exec` / `web_tabs` / `web_open` / `web_chatgpt_ask` 等）。

## 先确认可用

```bash
python3 ~/cc/web-bridge/bridge/cli.py status
```

要求「扩展连接 ✅」。若未连接：让用户到 chrome://extensions 加载/重新加载
`~/cc/web-bridge/extension`（首次加载和**新增权限**必须手动，无法脚本化）。

bridge 本身是 launchd 常驻服务（登录自启、崩溃自愈），正常情况下不用管它。
要看/修服务：`wb service status | restart | logs`。**不要手动 kill server 进程**——
真要杀必须 `lsof -ti tcp:8790 -sTCP:LISTEN | xargs kill -9`（不带 `-sTCP:LISTEN`
会把持有连接的 Chrome 也列进去）。

## 侧栏（用户看到的界面）

点扩展图标打开右侧驻留侧栏，三个标签：**对话**（调本机 agent）、
**Page Tools**（用户自己的页面脚本，可运行/自动运行/导出书签）、
**Agent Tools**（你自己的能力库，只读展示给用户看）。

侧栏的每个动作都走下面这些同样的路由，所以你在 CLI/MCP 里做的事和用户在侧栏里做的事
是同一批东西——**用户存的脚本你能发现，你存的脚本用户能在侧栏看到**。

如果用户说「侧栏里…」「侧边栏」「面板上」，指的就是它。

## 本地 agent（侧栏对话标签的后端）

`/agents` 列出本机可用的 claude / codex / dsh；`/agent/ask` 启动一个并流式返回 NDJSON
事件。CLI 是 `wb agents`。**注意**：这些 agent 默认带跳过确认的参数运行，
等于侧栏对话里的一句话可以让 agent 在用户机器上全权执行。

## 用户的页面脚本 ≠ 你的能力库

两个库分开的，别搞混：

| 侧栏标签 | 给谁用 | 工具 |
|---|---|---|
| **Agent Tools**（`capabilities/`） | **给你用**：以后 `web_capabilities` 能发现并调用，带参数声明 | `web_save_capability` |
| **Page Tools**（用户脚本） | **给人用**：用户自己点运行、开自动运行、导出成书签 | `web_page_scripts` / `web_save_page_script` / `web_delete_page_script` |

用户说「存到 Agent Tools / 存进你的能力库 / 做成一个能力」→ 前者；
说「存起来 / 保存这个脚本」→ 后者。分不清就问，别猜。

用户在对话里让你写的页面脚本属于**他的**，存进用户脚本库，不要存进你自己的能力库。
`web_page_scripts` 返回完整代码，别去磁盘上翻文件找。合并/替换掉的旧脚本要
`web_delete_page_script` 删掉，否则它会和新脚本一起自动运行。

**代码第一行写 `// 说明：这个脚本做什么`**——面板会把它存成脚本说明，
用户在列表里就靠这句话认出这是干嘛的（他不看代码）。改进版就写这轮新增了什么。

## 自动运行脚本

能力元数据里 `autorun: true` 的脚本，会被扩展注册成 userScripts，**页面加载时自动运行**
（原 page-beauty 的「页面增强」）。开关：`POST /capability/{id}/autorun`。
`extract` 类不允许 autorun（抽取是按需的）。关掉后需要刷新页面才还原。

## 先按「我要干什么」搜工具（第一步，别跳过）

**动手写 JS 之前先看一眼库里有什么。** 侧栏发起的对话，简报里已经把**全部工具**列好了；
其它入口（dsh、终端）用：

```bash
wb find                              # MCP: web_find_tool {}  → 列出全部（库小，直接读完自己挑）
wb find "把网页表格存成 JSON"          # MCP: web_find_tool {query}  → 库大到装不下时用，按文字相关性召回
```

**挑哪个是你的判断，桥不替你排。** 库小的时候它把全部工具的一行摘要原样给你；
库大到装不下才按文字相关性召回一批候选。返回的每条都带着**事实**——
用过几次、成功几次、被标记过几次不好用、是不是这个页面专属——
但这些只是数据，怎么权衡由你定，因为**只有你知道这次的任务是什么**。

桥曾经替你排过：把相关性乘上成功率、时新度、本页加权，给你一个"答案"。
实测它比你差——「这篇文章太乱了想安静地读」它排 extract-article 而不是 reader-mode，
「把网页数据弄成 excel」它排 netflix 而不是 extract-tables，而模型看一眼平铺列表全都对。
所以那套乘数删掉了。

**工具跑了但结果不对**（没报错却答非所问），`web_rate_tool {id, ok:false, note}` 标一下。
标记会作为事实出现在以后的检索结果里，但**不会自动把它压下去**——
下一个 agent 看到"被标记 3 次不好用"自己会做决定。日志只知道有没有抛异常，
不知道答案对不对，只有你知道。

关键：**不受当前网址限制**。站在 google.com 上要查"电影在哪些国家能看"，
unogs 专用的那个工具照样在目录里、照样该用。`url` 只是给当前页面的工具标上 ★，不是过滤。

## 看这个页面有什么（按网址列）



```bash
wb caps --url <页面URL>        # 列出该页可用能力(通用 + 本站专属),含说明与参数
wb caps <能力id>               # 单个能力：完整参数表(必填/默认值/可选值) + 源码位置
wb run <能力id> --url <URL> --params '<JSON>'
```

参数会在 bridge 侧校验：漏必填、拼错参数名、枚举/范围越界都会被挡下并**直接告诉你
正确的参数表**（拼错还会提示最接近的名字），不会静默跑出空结果。没填的参数按声明的
默认值补齐。

MCP 用户用 `web_capabilities` / `web_run_capability`。内置能力：

| id | 类型 | 用途 |
|---|---|---|
| `inspect-page` | 探查 | **写抓取脚本前先跑它**：给出重复结构候选+推荐选择器、分页按钮、表单、JSON-LD、页面全局 |
| `extract-article` | 抽取 | 正文 → Markdown（标题/作者/时间齐全） |
| `extract-tables` | 抽取 | 所有表格 → 结构化 JSON / CSV |
| `collect-list` | 自动化 | 按选择器翻页或滚动采集列表 |
| `reader-mode` | 美化 | 就地重排为阅读版式（`{"restore":true}` 还原） |
| `chatgpt-conversations` | 抽取 | 仅 chatgpt.com：走页面 API 列出历史对话 |
| `perplexity-ask` | 自动化 | 仅 perplexity.ai：提问并等回答，返回正文 + 来源 |
| `youtube-transcript` | 抽取 | 仅 youtube：整段文字稿（可带时间戳、可切语言），适合喂模型总结视频 |
| `x-posts` | 抽取 | 仅 x.com：帖子详情页=整条串+回复，主页/个人页=时间线；含互动数与永久链接 |
| `x-post` | 自动化 | 仅 x.com：**发帖/回复**（在某条帖子详情页调用=回复它）。**对外发布，发之前必须跟用户确认文案**；`dry_run:true` 只填不发 |
| `google-search` | 抽取 | Google 搜索 → 结构化结果（标题/链接/摘要/域名），支持 page/recent/lang。`--url` 写 **www.google.com**，写 google.com 会匹配到 Gmail 标签页 |
| `site-search` | 自动化 | **任意站点站内搜索**：自动找搜索框→推断提交地址→同源取回结果解析。解析不出来会给 `results_url`，再 `wb open` + `collect-list` 接着抓 |

**没有合适的能力时**：先查「这个站以前跑过什么」，再决定要不要自己写：

```bash
wb log --host example.com --code      # 别人/以前的自己在这个站跑过的脚本，按使用次数排
wb log 表格 --code                     # 按关键词搜（也可以直接 grep 下面那个日志文件）
```

MCP 用 `web_journal`。有能用的就照着改，没有再 `inspect-page` 摸结构 → `wb exec` 写脚本。

### 干成了就存下来——这一步是你的，桥不会替你做

**没有自动沉淀。** 桥只记录发生了什么，不判断什么值得留。原因是它判断不了：
它试过按「同一段脚本成功 3 次」来存，结果存下的是发邮件流程里*不变*的那半
（`await sendEmail()` + 看提示），而带着收件人、主题、正文的那半每次都不同、
永远攒不够次数——**任务里会重复的是样板，有价值的是变化的部分**。
这类判断你做得比任何规则都好，因为**你刚用过那个答案，你知道哪段才是答案**。

所以：**一件事做成了、而且以后还会再做，就存成能力**：

```bash
wb save-cap <id> <文件>          # MCP: web_save_capability
```

存的时候必须做到四件事，否则存了也没用（甚至有害）：

1. **名字说人话**。`用 TokenCV 邮箱发一封邮件` ✅；`🤖 const items=document.query…` ❌
   ——工具列表里没人认得出第二种，包括下一个你。
2. **描述写「什么时候该用它」**，不是「它是怎么写出来的」。别写「对话里让 claude 写的」。
3. **把会变的部分声明成参数**，别写死在代码里。收件人、片名、选择器、条数——
   凡是下次可能不一样的，都是参数。这是自动沉淀最做不到的一步。
4. **会对外发出去/改掉/删掉东西的能力**（发信、发帖、提交、删除），
   描述里必须**明写它会真的执行**，并且参数必须显式——绝不要存一个
   「无参数、调用即把当前草稿发出去」的能力。

exec 的返回值在这段脚本成功跑过两次以上时会提醒你一句，但**提醒只是提醒**，
存不存、怎么存、参数是哪几个，由你判断。

写一次性脚本时第一行写句 `// 这段在干嘛`——后来的人（和 `wb log`）靠它认路。

日志在 `~/.config/web-bridge/exec-log.jsonl`（JSONL，可直接 grep；只记代码和结果形状，
不记页面内容）。

能力文件格式（`~/cc/web-bridge/capabilities/<id>.js`）：

```js
/* @web-bridge-capability
{"id":"my-cap","title":"标题","description":"说明","kind":"extract",
 "match":["*"],"params":{"n":{"type":"number","default":10}}}
*/
// 函数体：args 是参数对象，可 await，return JSON 安全的值
return {n: args.n};
```

`kind` 取 extract / automate / restyle / inspect；`match` 用 `["*"]` 表示通用，
或写站点域名表示专属。

## 底层能力

```bash
wb tabs [关键词]                       # 列出/筛选打开的标签页
wb open <url>                          # 打开或聚焦标签页
wb exec '<JS 函数体>' --site chatgpt    # 在页面 MAIN world 执行(可 return/await)
wb exec '<JS>' --url example.com       # 按 URL 定位标签页
wb chatgpt "提示词" [--new] [--images --out DIR] [--file 路径]
wb chatgpt-last [--images --out DIR] [--conversation <会话id>]   # 不发消息，补捞已生成的结果
wb result <request_id> [--wait N]      # 传输断了之后领回那次的结果（服务端缓存 1 小时）
wb results                             # 列出可领取的结果

**结果丢了不用重跑**（2026-08-25 起）：页面把活干完但连接断掉时，CLI 会自动用 `request_id`
去服务端缓存领结果，领不到再退到 `chatgpt-last` 从页面补捞。手动补捞就用上面两条命令——
重发提示词等于再花一次额度，永远先补捞。

> **先看一眼分工**：单纯要 ChatGPT 干活（问答/出图/读文件）请改用 `chatgpt_image_osascript` skill
> （`~/.claude/skills/chatgpt_image_osascript/scripts/cgo.py`，只用 osascript，不依赖本扩展和 bridge 服务）。
> `wb chatgpt` 留给「已经在用 web-bridge 做别的事、顺手问一句」或 chatgpt_image_osascript skill 不可用时。
wb adapter <站点> <方法> --params '<JSON>'
wb log [关键词] [--host 站点] [--code]  # 查以前在这个站跑过什么（写 JS 前先看）
wb agents [--detect]                   # 侧栏对话能调用的本地 agent
wb close <URL片段|tabId>                # 关掉标签页（自动化开完要收拾干净）
wb sites [--add 名 --match 模式 --home URL]  # 查看/注册站点，即时生效
wb reload                              # 扩展从磁盘重载(改代码后)
```

`exec` 的 `code` 是**函数体**：`args` 在作用域内，可 `return`、可 `await`。
它跑在页面自己的 JS 世界，因此能读页面全局变量、调页面函数、用用户登录态 fetch
同源接口——这是普通脚本(如 osascript)做不到的。

```bash
# 例：用页面登录态调站点接口
wb exec 'return await fetch("/api/me",{credentials:"include"}).then(r=>r.json())' --site chatgpt
# 例：读页面 JS 内存里的状态
wb exec 'return Object.keys(window).filter(k=>k.startsWith("__"))' --site chatgpt
```

## 边界与坑

- **只操作用户已登录的页面**，不涉及账号密码；发消息/下单等外发动作前必须先向用户确认。
- 站点适配器（`extension/adapters/*.js`，文件注入）在任何站点可用；通用 `exec` 依赖
  `chrome.userScripts`（已启用），若某站点仍报 CSP `unsafe-eval` 则改用适配器。
- `wb service restart` 在有命令正在跑时会**拒绝重启**并告诉你在跑什么，要打断得加 `--force`
  （以前一次重启就能把一个正在出图的长请求打断，结果全丢）。`wb status` 会显示服务是否在跑旧代码。
- 挑标签页时会跳过被别的工具占用的页面（页面里有 `window.__cgo` 视为 chatgpt_image_osascript skill 占用），
  自己用过的标签会记住并复用，`--new` 只会导航自己的或无主的标签。
- **改了扩展代码**：`wb reload` 即可；**改了 manifest 权限**：必须用户手动到
  chrome://extensions 点重新加载。
- 扩展重载后，**已打开的旧标签页需刷新一次**才会用上新内容脚本（新标签页不受影响）。
- 报 503「目标 X 上已有命令在跑」= 有别的调用（可能是**另一个 agent 会话**）正在驱动同一个
  站点，不同目标之间不互相排队；等它跑完或换个目标即可。`wb status` / `/health` 能看到在跑什么。
- 页面里抛的异常会原样报回来（不再变成 `null`）；报「目标页面没有加载成功」就是那个 URL
  在浏览器里打不开，不是权限问题——bridge 会把自己开的那个错误标签页自动关掉。
- ChatGPT 出图/上传文件是 Plus 功能；`wb chatgpt` 会自动校验是否真实登录（非访客会话）。

## 加新站点

1. `~/.config/web-bridge/config.json` 的 `sites` 加 `{match, home, adapter?}`
2. 需要高级动作时写 `extension/adapters/<name>.js`，用
   `window.__webBridge.registerAdapter(name, {method(params, ctx){...}})` 自注册
3. `wb reload`
