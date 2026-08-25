# web-bridge 交接文档

> 新 session 从这里开始。权威状态见 `ROADMAP.md`，本文是**能立刻上手**的浓缩版。
> 最后更新：2026-08-25（侧栏重构：对话 / 脚本库 / 页面三标签，本地 agent 对话，
> page-beauty 并入能力库；bridge 是 launchd 常驻服务）

## 这是什么

把「用户浏览器里已登录的页面」变成**任何本地 agent 都能调用的能力**。
核心是两个原语：在任意站点的**页面 JS 世界（MAIN world）**执行代码，以及调用
**可发现的能力库**。带 token 认证，暴露为 CLI + MCP + skill。

**状态：功能完整，端到端跑通，已在 4 个 agent 注册，bridge 作为 launchd 服务常驻。** 不是半成品。

## 为什么要这么做（一句话原理）

浏览器扩展是官方允许的 JS 注入通道，**页面 CSP 对扩展豁免**；注入到 MAIN world 后能读
页面全局变量、调页面函数、用登录态 fetch 同源接口。这些是 osascript 之类外部脚本
永远做不到的（它们只能落在 isolated world，看得见 DOM 但看不见页面 JS 内存）。

## 架构

```
agent (claude / codex / openclaw / hermes；dsh 走 skill)
  │ stdio                    │ shell
  ▼                          ▼
mcp_server.py            cli.py
  └────── HTTP + Bearer token ──────▶ server.py (FastAPI, 127.0.0.1:8790)
                                          │ WebSocket + token（只认 chrome-extension:// 来源）
                                          ▼
                                   扩展 service worker
                                          │ chrome.userScripts.execute（主路径，绕 CSP）
                                          ▼
                                   页面 MAIN world
                                     ├─ 能力库 capabilities/*.js
                                     └─ 站点适配器 adapters/*.js
```

**relay.js（ISOLATED）+ page.js（MAIN）+ postMessage 是兜底路径**，仅当
`chrome.userScripts` 不可用时才走（该 API 需要浏览器「允许用户脚本」开关）。

## 文件地图

```
bridge/
  server.py        FastAPI：/exec /capabilities /capability/{id} /adapter /sites /tabs /open /close /reload
  cli.py           wb 命令（自动拉起 server）
  mcp_server.py    stdio MCP，零依赖，14 个工具
  capabilities.py  能力注册表：扫目录、解析元数据头、URL 匹配、参数校验、元数据体检
  journal.py       exec 日志 + 归一化签名计数 + 跑满 3 次自动沉淀成能力
  agents.py        本地 agent 运行器（claude -p / codex exec / dsh），探测 + 流式事件
  run_tests.sh     用一次性 server（独立端口+临时 state）跑回归，不干扰实时服务
  config.py        读 ~/.config/web-bridge/config.json（token/port/sites/blocklist）
  register_mcp.py  把 MCP 注册进 4 个 agent（幂等）
  mock_ext.py / test_mock_ext.py   无浏览器时的假扩展 + 回归测试（19 项）
  panel_harness.py 把扩展侧栏渲染成普通网页以便测试（见「测试与验证」）
  harness_stub.js  harness 用的桩（真文件，避免多层转义把 JS 改坏）
  service.py       launchd 服务安装/卸载/重启/状态/日志（wb service 就是它）
capabilities/      能力库（写一个文件 = 新增能力，无需重载扩展）；auto/ 是自动沉淀出来的
extension/         MV3 扩展；sidepanel/ 是右侧驻留侧栏（对话 / 脚本库 / 页面 三个标签）
```

## 常用命令

```bash
wb() { python3 ~/cc/web-bridge/bridge/cli.py "$@"; }

wb service install                          # 装成常驻服务（开机自启+崩溃自愈），一次即可
wb service status | restart | logs | uninstall
wb status                                   # 必查：扩展连接了吗
wb caps --url <URL>                         # 【推荐入口】这个页面能做什么
wb caps <能力id> [--source]                  # 单个能力：参数表（必填/默认/枚举）+ 源码
wb run <能力id> --url <URL> --params '<JSON>'
wb exec '<JS 函数体>' --site chatgpt         # args 在作用域，可 return/await
wb chatgpt "提示词" [--new] [--images --out DIR] [--file 路径]
wb chatgpt-last [--images --out DIR] [--conversation <会话id>]   # 不发消息，补捞最后一条回答
wb results                                  # 还能补捞的 request_id
wb result <request_id> [--out DIR]          # 按 id 领取上次的结果（连接断了用这个）
wb service restart [--force]                # 有命令在跑时会拒绝重启，--force 才打断
wb log [关键词] [--host 站点] [--code]        # 【写 JS 前先查】这个站以前跑过什么
wb close <URL片段|tabId>                     # 关标签页（自动化收尾）
wb sites --add <名> --match '<URL模式>' --home <URL>   # 加站点，即时生效
wb save-cap <id> <文件>                      # 把一次性脚本沉淀成能力
wb reload                                   # 改了扩展代码后
```

**参数是被校验的**：漏必填 / 拼错名字 / 枚举越界 / 数值越界都会被 bridge 挡下（422），
并回一份照着改就能用的参数表；没填的按声明默认值补齐。存能力时元数据也会体检
（kind 合法、description 非空、params 结构正确），不合格**不写盘**（更新则回滚原文件）。

MCP 工具名：`web_capabilities`（带 `capability` 参数则返回单个能力的参数表+源码）/
`web_run_capability` / `web_save_capability` / `web_exec` / `web_tabs` / `web_open` /
`web_close_tab` / `web_adapter` / `web_chatgpt_ask` / `web_status` / `web_reload_extension`。

## 现有能力

| id | 类型 | 用途 |
|---|---|---|
| `inspect-page` | 探查 | **写抓取脚本前先跑它**：列表结构候选 + 推荐选择器、分页按钮、表单、JSON-LD、页面全局 |
| `extract-article` | 抽取 | 正文 → Markdown |
| `extract-tables` | 抽取 | 表格 → JSON / CSV |
| `collect-list` | 自动化 | 按选择器翻页/滚动采集 |
| `reader-mode` | 美化 | 就地重排阅读版式（`{"restore":true}` 还原） |
| `chatgpt-conversations` | 站点 | chatgpt.com：走页面 API 列历史对话 |
| `perplexity-ask` | 站点 | perplexity.ai：提问并等回答，返回正文 + 来源 |
| `youtube-transcript` | 站点 | youtube：整段文字稿（可带时间戳、可切语言）——驱动页面自带字幕面板，读完自动关回去 |
| `x-posts` | 站点 | x.com：详情页=整条串+回复，主页/个人页=时间线；含互动数、图片、永久链接 |
| `x-post` | 站点 | x.com：**发帖/回复**（详情页上调用即回复那条）。对外发布动作，先跟用户确认文案；`dry_run` 只填不发 |
| `google-search` | 站点 | Google 搜索 → 结构化结果；支持分页、时间过滤、语言。**--url 必须写 www.google.com**（写 google.com 会匹配到 Gmail 标签页） |
| `site-search` | 通用 | 任意站点的站内搜索：找搜索框 → 推断提交地址 → 同源取回结果页解析成列表；解析不了就诚实给出 results_url |

能力文件格式：头部 `/* @web-bridge-capability {JSON} */` + 函数体（`args` 是参数，
可 await，return JSON 安全值）。`kind` = extract/automate/restyle/inspect；
`match` = `["*"]` 通用 或 站点域名。

## ⚠️ 血泪教训（新 session 务必先读）

1. **重启 server 用 `wb service restart`**（装了服务之后就别手动杀了）。
   历史坑：`pkill -f "python3 server.py"` **匹配不到**实际命令行；旧进程占端口会让新进程
   静默退出，表现为「扩展突然断连」，极易误诊为扩展问题。现在 server 启动时会先判断端口
   归属并**明说**（被另一个 web-bridge 占用就干净退出，被别的程序占用就报谁占的）。
   真要手动杀，**必须带 `-sTCP:LISTEN`**：
   ```bash
   lsof -ti tcp:8790 -sTCP:LISTEN | xargs kill -9
   ```
   不带的话 `lsof -ti :8790` 会把**持有连接的 Chrome 进程也列出来**，一 kill -9 就把浏览器
   打了——本文档之前写的就是那条危险命令。

2. **改扩展代码 → `wb reload` 即可；改 manifest 权限 → 必须用户手动到 chrome://extensions
   点重新加载**（`chrome.runtime.reload()` 不重读权限）。程序化安装扩展会被安全策略拦截。

3. **内容脚本不要写「已注册就跳过」的守卫。** 曾因 `if (PAGE.adapters.chatgpt) return`
   导致扩展重载后新代码被跳过，**我改的文件根本没生效，白白排查一小时**。最新注入必须获胜。

4. **Chrome 会 discard 后台标签**，对其注入会报「manifest must request permission」这种
   完全误导的错误。`injectable()` 已检查生命周期，只匹配到休眠标签时先 reload 复活。

5. **卡住类问题不要靠读代码推理。** 四个真 bug 全靠「让程序自报状态」定位：给页面加
   `window.__wbAskStage` 阶段标记、注入失败时回报目标标签的 `status/discarded/url`。
   我前面几轮纯推理全错。

6. **CSP 的 `unsafe-eval` 仍约束扩展注入脚本内的 `new Function(str)`**。所以 exec 走
   `chrome.userScripts.execute`（注入而非求值），不要退回 eval 方案。

7. **别在 userScripts 失败时自动退回 relay 兜底。** 兜底路径用 `new Function`，在
   Trusted Types 站点（youtube.com）必然报「Evaluating a string as JavaScript violates…」——
   于是你看到的是**兜底路径的错**，真正的原因被吞掉了。现在只有 `chrome.userScripts`
   本身不可用（浏览器「允许用户脚本」开关没开）才会兜底，其余错误原样上报。

8. **页面里抛的异常曾经变成 `result: null`。** 失败的能力和「什么都没抓到」的能力长得
   一模一样，这是这条链路上最能骗人的失败形态。现在注入的包装器自己 try/catch，把错误
   当数据回传，SW 再重新抛出——写新能力时能直接看到页面里的真实报错。

9. **富文本编辑器里别补发合成 `input` 事件。** `document.execCommand("insertText")` 本身就会
   派发真实 input 事件，再手动 dispatch 一个会和它抢——X 的编辑器因此把文案插了两遍
   （"文案文案"）。定位办法还是老一套：把每一步的输入框内容打出来看，不要靠推理。

10. **宁可返回 0 条也不要返回"看起来像结果"的垃圾。** site-search 早期版本在
   GitHub/MDN 上把筛选侧栏和顶部导航当成搜索结果返回——结构上它们和结果列表一模一样。
   现在靠三条规则筛掉：不要指回搜索页自身的链接、不要 nav/header/footer 里的链接、
   整组结果必须至少有一行提到查询词；筛没了就返回空 + results_url + 明确原因。

11. **命令锁曾经是全局一把，而且绑在了错误的 event loop 上。** 两个真 bug，是被
   「另一个 agent 会话正在跑 `wb chatgpt`」这件事一起顶出来的：
   - `asyncio.Lock()` 在**模块导入时**创建（Python 3.9 会绑到当时 `get_event_loop()`
     返回的那个 loop），而 uvicorn 跑的是另一个 loop。无争用的快路径不碰这个绑定，
     所以数月无事；第一次两请求重叠就 `got Future attached to a different loop` → 裸 500。
     现在锁在**真正 await 它的那个 loop 里**惰性创建。
   - 全局一把锁意味着一个几分钟的 `chatgpt.ask` 会把**所有**调用（哪怕目标是别的站点）
     排到后面，直到各自超时，而且被堵的人只看到「超时」，不知道被谁堵了。更糟的是
     客户端放弃后，服务端那条命令仍占着锁直到自己 305s 超时。现在**按目标站点/URL 分锁**，
     只读命令（tabs/open/close/reload）完全不排队，排不上的会收到 503 并告诉你
     「目标 X 上正在跑什么、跑了多久」，`/health` 也会列出在跑的命令。

12. **测试不要跑在用户的实时服务上。** mock 扩展会抢 hub 的扩展槽位，而真扩展被挤掉后
   会重连、又把 mock 挤掉——两边互踢，测试随机超时，用户的浏览器也被搅。现在
   `bridge/run_tests.sh` 起一个独立端口 + 临时 state 目录的一次性 server 来跑。
   **现在有护栏**：mock 连接带 `client=mock`，bridge 在生产端口(8790)上直接拒绝它，
   `test_mock_ext.py` / `mock_ext.py` 自己也会拒绝对 8790 启动。日志里那个
   `hello {'mock': True}` 就是这么来的——不是残留进程，是有人直接跑了 test_mock_ext.py。

13. **结果不能只存在于那条 HTTP 连接上。** 三次 `wb chatgpt --images` 出图，页面三张全画好了，
   只因为连接断了就全丢，额度白花。现在每个驱动页面的调用都带 `request_id`：结果同时存内存和
   `~/.config/web-bridge/results/<id>.json`，`GET /result/{id}`（`wb result <id>`）可以事后领取，
   **重启也还在**；同一个 id 再 POST 不会重跑（重试绝不能把提示词再发一遍、再花一次钱）。
   连接断了 CLI/MCP 会自己去补捞，补不到再退到 `wb chatgpt-last` 直接从页面读。

14. **重启服务会 SIGTERM 掉正在跑的命令。** 用户看到的「服务半夜自己崩了」，实测是
   `launchctl kickstart -k`（就是 `wb service restart`）——`launchctl print` 里
   `last terminating signal = Terminated: 15`，err.log 里**没有**对应时刻的 traceback，
   也没有 crash report。改代码后随手重启是常态，盲目重启才是问题：现在
   `service restart/install/uninstall` 发现有命令在跑会**拒绝并列出在跑什么**，要打断得加 `--force`。

15. **别拿旧日志当新证据。** err.log 是追加的，里面那串 `attached to a different loop` 全是
   23:32 之前旧进程留下的（栈里的行号在当前文件里已经不存在了）。现在 `/health` 带
   `build`：版本、pid、启动时间、源码 sha、以及 `stale`（磁盘代码比进程新）——
   `wb status` 会直接告诉你「你排查的是旧代码」。

16. **不要抢别的工具钉住的标签页。** `--new` 曾经把 `chatgpt-osascript` 正在用的 chatgpt 标签
   直接导航去新会话，两边开始读对方的会话。现在扩展**记住自己的标签**（按站点存在
   `chrome.storage.session`），并在挑标签时探测占用标记（`window.__cgo` /
   `document.body.dataset.cgoOwned`）跳过别人的页；自己占的页也会打上 `window.__webBridgeOwned`。
   全被占用又有 `home` 就另开一个。

17. **hub 只有一个扩展槽位**：第二个 WS 连上会顶掉前一个；顶掉的一方现在会被显式关闭
   （于是它重连），仍在说话的 socket 会被重新认领。以前这会表现为「扩展突然断连」，
   又是一次误诊。

## 安全设计

- bridge 只监听 127.0.0.1；每个 HTTP 路由要 `Authorization: Bearer <token>`
- 扩展 WS 也要 token，且**只接受 `chrome-extension://` 来源**（挡住知道 token 的网页冒充）
- **敏感站点黑名单**（`config.BLOCKLIST`）：银行/支付/密码管理器/健康门户/chrome:// 一律拒绝，
  在 bridge 层对 exec / capability / adapter 三条路径统一强制
- **刻意没有收窄 `host_permissions`**：收窄会砍掉「任意站点通用能力」这一核心价值，
  黑名单是更合适的取舍。改主意的话改 config 即可。

## 侧栏（点扩展图标打开，右侧驻留）

`extension/sidepanel/`，三个标签：

| 标签 | 干什么 |
|---|---|
| **对话** | 调用**本机的 agent**（claude / codex / dsh）。`＋页面内容`把当前页正文读进上下文；agent 回答里的 ```js 代码块会给出「存成脚本 / 在本页运行」两个按钮——对话生成的脚本由此进入能力库 |
| **脚本库** | 原 page-beauty 的功能并入这里。列当前站点/全部能力，填参数运行、编辑、删除、**自动运行开关**（页面加载时注入）、**声明参数**（名字/类型/必填/默认值/说明，存进能力元数据后由 bridge 校验） |
| **页面** | bridge/扩展/服务状态、快捷动作（提取正文/探查/表格/阅读模式）、**这个站以前跑过什么**（点一条即可拿它的代码去存成脚本）、标签页列表 |

对话里 agent 的 markdown 由 `renderMarkdown()` 渲染（无依赖，CSP 严格）。
**安全**：agent 的回答里可能夹带它读到的网页内容，所以**先整体 HTML 转义、再放回有限的
几种行内格式**——从页面文本到活markup 没有通路。真机用 `<img onerror>` / `<script>`
payload 验证过：变成可见文本，不执行。

**侧栏不实现任何逻辑**：每个动作都走 CLI/MCP 用的同一批路由（`/agent/ask`、`/capability/{id}`、
`/exec`、`/journal`），所以参数校验、敏感站点黑名单、标签页解析、exec 日志只有一份实现。

### 本地 agent（对话标签的后端）

`bridge/agents.py`。安装服务时自动探测 claude / codex / dsh 并写进 config.json 的 `agents`
块（`wb agents` 查看、`wb agents --detect` 重新探测、`--cwd` 指定工作目录）。

- 三个 CLI 的流式格式不同：claude 是 stream-json、codex 是 JSONL、dsh 是纯文本，
  `parse_line()` 把它们归一成同一串事件（text / tool / done / end）
- `/agent/ask` 返回 **NDJSON 流**（不是等跑完再返回，这类任务动辄几分钟）；
  run 存在服务端，侧栏**关掉再打开会自动接回正在跑的任务**（存 run id → 重开时
  `/agent/run/{id}`，没跑完就 `?follow=true` 继续跟）。真机验证过：起一个真 claude 任务、
  3 秒后断开、重新接上拿到完整答案——和 chatgpt 那条「活干完了结果却丢了」是同一类问题
- **权限**：探测时默认加上跳过确认的参数（claude `--dangerously-skip-permissions`、
  codex `--dangerously-bypass-approvals-and-sandbox`）——非交互跑不能停在确认提示上。
  这等于**网页里的一句话可以驱动你机器上的 agent 全权干活**，是用户明确选择的取舍；
  想收紧就 `wb service install --no-full-access` 或改 config.json 的 args。

### 自动运行脚本（原 page-beauty 的 enhance 开关）

能力元数据新增 `autorun: true`，SW 启动时从 `/capabilities/autorun` 拉取并用
`chrome.userScripts.register` 注册，页面加载时自动跑。改动会通过 WS 的 `notify`
立刻让 SW 重新注册（不用等下次启动）。`extract` 类能力禁止 autorun——抽取是按需的。

坑：`match` 里写的是宽松写法（`*`、裸域名），但 userScripts.register 只认真正的
match pattern，**一个坏 pattern 会让整批注册失败**，所以 `_match_patterns()` 统一转换，
注册也做了逐个回退。

坑二（真机回归才发现）：**自动运行必须拿到和手动运行一样的参数**。早期版本注入的是
裸 `{}`，于是同一个脚本按「运行」按钮时 `args.color` 有值、页面加载自动跑时是 undefined，
静默走进兜底分支。现在 `/capabilities/autorun` 返回已填好默认值的 `args`。
连带：**有必填参数的能力不允许开自动运行**——页面加载时没人传，开了也只会失败。

## 自进化：exec 日志 → 自动沉淀成能力

**问题**：能力库能不能长大，以前完全取决于 agent 记不记得调 `save_capability`——没有任何
机制保证那一步发生，所以 12 个能力全是人手写的，没有一个是跑着跑着自己长出来的。

**现在**：每次 `/exec` 和 `/capability/{id}` 都记进 `~/.config/web-bridge/exec-log.jsonl`
（JSONL，直接 grep），同一段脚本按**归一化签名**计数；同一个站上**成功跑满 3 次**
（`config.promote_after`）就自动写进 `capabilities/auto/<auto-host-sig>.js`，
立刻可被所有 agent 发现和调用。

三个出口，任选其一都能找到这条路，不需要 agent 事先知道日志存在：
- **exec 返回值里带 hint**：第 2 次提醒「再跑一次就会自动沉淀」，第 3 次告诉它沉淀成了什么
- **发现入口自带历史**：`GET /capabilities?url=` 会带 `prior_scripts`（这个站以前跑过什么，
  按使用次数排序）+ `prior_hint`——agent 在「找现成能力」那一刻就看到踩熟的路
- **主动查**：`wb log <关键词> --host x.com --code` / MCP `web_journal` / 直接 grep 那个 jsonl

**记什么不记什么**：记代码、URL、耗时、成功与否、参数；**结果只记形状**
（类型/键名/条数），不记页面内容——日志是「跑过什么」的账本，不是用户页面数据的副本。
文件 chmod 600，超 5MB 轮转。

**签名归一化**（决定了这套东西能不能转起来）：先把字符串字面量摘出来换成内容 hash，
再剥注释、压空白、去掉标点旁边的空格。理由：agent 几乎不会逐字节重复同一段脚本
（换个注释、重排缩进就变），不归一化就永远攒不满 3 次；而字符串必须先保护起来，
否则 `"https://…"` 里的 `//` 会被当成注释把后半行吃掉，`"div a"` 和 `"diva"` 也会撞成同一段。

自动沉淀出来的能力标题带 🤖、元数据里 `auto:true`，描述里写明是机器生成的——
确认有用就用 `save_capability` 覆盖同名 id，换成像样的标题、描述和参数说明。

## 常驻服务（已安装）

bridge 由 launchd 托管：`~/Library/LaunchAgents/com.web-bridge.server.plist`
（Label `com.web-bridge.server`，跑 `/usr/bin/python3 bridge/server.py`）。
**登录即起，被 kill 掉约 1 秒自愈**（实测），日志 `~/Library/Logs/web-bridge.log`。

- `KeepAlive = {Crashed, SuccessfulExit:false}`：崩溃/非零退出才重启。**干净退出不重启**
  是故意的——server 发现端口已被另一个健康的 web-bridge 占用时会 exit 0 主动让位，
  若这也重启就成了死循环。
- 端口归属只有一个主人：`wb service install` 会先干掉端口上的旧监听进程再交给 launchd；
  CLI 和 MCP 发现装了服务后不再自己 spawn，改用 `launchctl kickstart`。
- 日志超过 5MB 在 install/restart 时轮转一份 `.1`（launchd 在进程启动时打开日志文件，
  这是唯一安全的轮转时机）。
- 卸载：`wb service uninstall`（回到「wb 命令按需临时拉起」的旧行为）。

## 测试与验证

```bash
./bridge/run_tests.sh                  # 45 项，独立端口 + 临时 state，不碰实时服务
python3 bridge/panel_harness.py        # 生成 .harness/harness.html
```

**别直接跑 `test_mock_ext.py`**：它会连上 8790 抢真扩展的槽位，两边互踢。要单独跑就自己
设好 `WEB_BRIDGE_PORT` / `WEB_BRIDGE_STATE` 指向一次性实例。

桩代码在 `bridge/harness_stub.js`（真文件，不是 Python 字符串）。**这一点是踩了两次才改的**：
JS 里的 `\n` 先被三引号字符串吃掉一次、又被 `re.sub` 的替换展开吃掉一次，两次都生成出
语法错误的 harness。改成读文件后没有任何转义层，只替换 `__FIXTURE__` / `__TAB_URL__`。

**侧栏为什么要 harness**：Chrome 不允许任何扩展注入别的扩展的页面，所以侧栏是唯一
不能被 web-bridge 自己驱动验证的部分。`popup_harness.py` 用**真实的** popup.html/popup.js，
只桩掉三处扩展专属接缝（config.js 导入、chrome.* 、api()），于是渲染 / 参数表单 /
readForm 都能在普通浏览器里跑，运行结果回显参数、调用记录挂在 `window.__calls` 上。
`~/cc/.claude/launch.json` 里有个 `wbharness` 配置（静态服务 8791）配合它用。

## 可能的下一步

- 更多站点能力（每个常用站点一个能力，比通用 exec 稳）——youtube/x 这两个是范例
- 兜底 relay 路径（relay.js + page.js）现在只在「允许用户脚本」开关没开时才会用到，
  可以考虑彻底删掉，换成一条明确的报错
- 能力可以带自检（run 前先确认页面是预期的那种页面），失败时给出更准的原因
- 弹窗目前一次只跑一个能力、结果不持久；要做「结果历史」得先想清楚存哪
