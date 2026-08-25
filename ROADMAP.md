# web-bridge — 路线图与状态

把「已登录浏览器页面」变成任何本地 agent 都能调用的能力：通用地在任意站点的
**MAIN world** 里执行 JS / 调用站点适配器，经带认证的本地 bridge，暴露为 CLI + MCP。

前身：`~/cc/chatgpt-bridge` + `~/cc/browser-extension`（保留不动，本项目独立）。

## 架构

```
agent (claude/codex/openclaw/hermes/dsh)
  │  stdio
  ▼
MCP server (bridge/mcp_server.py) ──┐
CLI (bridge/cli.py) ────────────────┤ HTTP + token
                                    ▼
                        bridge server (bridge/server.py, FastAPI, 127.0.0.1:8790)
                                    │  WebSocket + token
                                    ▼
                        extension service worker (background/)
                          │ chrome.runtime           ▲ chrome.runtime
                          ▼                           │
              content/relay.js (ISOLATED world) ──postMessage──┐
                                                               ▼
                                              content/page.js (MAIN world)
                                                = exec 任意 JS + 适配器注册表
                                                   └─ adapters/chatgpt.js 等
```

为什么三段式：MAIN world 能看页面全局/调页面函数/绕过页面 CSP（扩展注入豁免），
但 MAIN world 没有 `chrome.runtime`；ISOLATED 中继有 `chrome.runtime` 但看不见页面
运行时。两者靠 `window.postMessage` + 每页随机 nonce 对接。

## 通用协议

- 客户端 → bridge（HTTP，`Authorization: Bearer <token>`）
  - `GET /health`
  - `POST /exec` `{site?, url?, code, args?, new_tab?, timeout_ms?}` → 在页面 MAIN world 跑 code(args)
  - `POST /adapter/{site}/{method}` `{params}` → 调站点适配器方法
  - `GET /tabs` / `POST /open`
- bridge ↔ SW（WS `/ws/ext?token=`）：`command`/`result`/`progress`/`ping`/`pong`/`hello`
- SW ↔ page：ISOLATED relay 与 MAIN page 之间 postMessage（带 nonce + id）

## 关键约束（来自前身项目实测）

- ChatGPT 完成信号 = `[data-testid="copy-turn-action-button"]` 出现，不是 stop-button 消失
- 隐藏标签会节流 → SW 每次调用前 `chrome.tabs.update{active:true}` 但不抢窗口焦点
- 登录检测用 `/api/auth/session`（cookie/NextAuth），不要用 `/backend-api/me`
- 生成图片 = chatgpt.com/oaiusercontent 大图，`fetch(src)→base64`，按 pathname 去重
- MV3 SW 会被回收：结果走 chrome.runtime 消息带 id 自愈；page 每 ~8s 发 progress 心跳保活
- 扩展重载：改代码用 `wb reload` 自助；**改 manifest 权限必须用户手动重新加载**

## 真机验证结果（2026-08-24，扩展已加载）

已验证可用：
- 扩展连上 bridge（token 认证的 WS）✅
- `tabs` 列标签 ✅ ；`exec` 在 MAIN world 跑 JS（标量/对象都对）✅
- **`adapter chatgpt status` 真实返回 `logged_in:true, account:<用户邮箱>`** ✅
  → 证明「文件注入的适配器」在 CSP 严格站点可用
- **`web-bridge reload` 扩展自重载 + 自动重连** ✅ → 迭代闭环成立

两个重要的机制结论：
1. **CSP 的 `unsafe-eval` 约束扩展注入脚本的运行时 eval**。扩展注入的**文件**不受页面
   CSP 限制，但文件里 `new Function(str)` 求值字符串会被缺 `'unsafe-eval'` 的 `script-src`
   挡住（chatgpt.com 就是）。**解法：`chrome.userScripts.execute` 把代码注入为脚本而非
   求值字符串**，已生效（chatgpt.com 上 exec 正常，并能读到页面 JS 全局）。
   站点适配器（纯文件、不 eval）本来就不受影响。
2. 扩展重载后，**旧标签里残留的 MAIN-world 脚本不会被 executeScript 重注入顶替**
   （新标签/页面刷新后一切正常）。已加「最新者接管」的身份检查 + SW 会话级 injected
   集合，仍未根治；当前 workaround：改完代码 reload 扩展后刷一下目标页面。

## 当前状态（2026-08-24 · 端到端已跑通）

- [x] M0 脚手架
- [x] M1 核心 exec + token 认证 —— headless 7/7 + 真机通过
- [x] M2 CLI —— mock 与真机均通过
- [x] M3 ChatGPT 适配器 —— **真机通过**：`cli.py chatgpt "…"` 返回真实回答；
      `adapter chatgpt status` 返回 logged_in/account
- [x] M4 MCP —— **真机通过**：`web_chatgpt_ask` 经 MCP 协议拿到真实回答；
      已注册进 claude(~/.claude.json) / codex(config.toml) / hermes(config.yaml)，
      幂等、配置文件校验有效（register_mcp.py）
- [x] M5 加固 —— token(HTTP+WS)、**WS origin 校验**（只认 chrome-extension:// 来源，
      挡住知道 token 的网页冒充；真实扩展实测不误伤）、仅 127.0.0.1、命令锁、自重载。
      回归 **10/10**（test_mock_ext.py，含 3 条 WS 安全用例）。
      待补：动态站点注册、收窄 host_permissions
- [x] M6 收尾 —— README、skill(`~/.claude/skills/web-bridge` 软链 7 个 agent)、记忆已写；
      出图真机验证通过（柴犬 PNG）

## 待办（下一轮）

1. ~~openclaw / dsh 的 MCP 注册~~ **已完成**：openclaw 用 `openclaw.json` 的
   `mcp.servers`，已加进 register_mcp.py（现覆盖 claude/codex/hermes/openclaw 四家，
   全部幂等且配置校验有效）。dsh 无 MCP 配置痕迹，改为提供 **skill** 包装：
   `~/.claude/skills/web-bridge`，已软链到 7 个 agent 目录（含 dsh）。
2. **重载后旧标签脚本不刷新**：workaround 是刷新页面。可考虑 SW 启动时主动向所有已知
   标签重注入，或用 registerContentScripts 动态注册。（注：exec 走 userScripts 路径后
   不再受此影响，只有适配器路径受影响。）
3. ~~通用 exec 在严格 CSP 站点不可用~~ **已解决**：改用 `chrome.userScripts.execute`
   （把代码**注入**为脚本而非 eval 字符串），chatgpt.com 上 exec 正常返回，并能读到
   页面 JS 内存里的全局（`__reactRouterContext` / `__REACT_QUERY_CACHE__` 等）。
   注意：`userScripts` 是新权限，**必须在 chrome://extensions 手动点重新加载**才生效，
   `chrome.runtime.reload()` 不重读权限；SW 里做了失败回退到 postMessage 路径。
4. ~~图片生成真机验证~~ **已完成**：`wb chatgpt "…" --images` 成功生成并保存 PNG。
   文件上传（`--file`）仍待验证。
5. 动态站点注册（加站点不必改 manifest，用 chrome.scripting.registerContentScripts）。
6. 收窄 `host_permissions`（现为 `<all_urls>`，可改为按 config.sites 动态申请）。
7. 运维坑：重启 server 前必须 `lsof -ti :8790 | xargs kill -9`——`pkill -f "python3 server.py"`
   匹配不到实际命令行，旧进程占着端口会让新进程静默退出、看起来像"扩展断连"。

## M7 能力系统（2026-08-24 · 已跑通）

把「执行任意 JS」升级为**可发现的能力库**：agent 先问「这个页面能做什么」，再调用或自创。

**设计**：能力 = `capabilities/<id>.js`，文件头一段 JSON 元数据
（id/title/description/kind/match/params），文件体就是 exec 的函数体（`args` 为参数）。
bridge 不执行文件即可解析元数据 → 列表快且安全；运行时把文件体经 userScripts 注入
页面 MAIN world（绕过 CSP）。**加能力 = 写文件，无需重载扩展**。

- `kind`：extract（抽取数据）/ automate（自动化）/ restyle（美化重排）/ inspect（探查）
- `match`：`["*"]` 通用，或 `["chatgpt.com"]` 等站点专属

**接口**
- `GET /capabilities?url=` → 当前页可用能力（含说明与参数）
- `POST /capability/{id}` → 运行；`PUT /capability/{id}` → agent 创作/更新
- MCP：`web_capabilities` / `web_run_capability` / `web_save_capability`
- CLI：`wb caps` / `wb run <id> --params` / `wb save-cap <id> <file>`
- 扩展弹窗：显示当前标签页可用能力（本站专属 + 通用），点击复制命令

**内置能力（均真机验证）**

| id | kind | 用途 |
|---|---|---|
| inspect-page | inspect | 探路：重复结构候选+推荐选择器、分页按钮、表单、JSON-LD、页面全局 |
| extract-article | extract | 正文 → Markdown（含标题/作者/时间） |
| extract-tables | extract | 所有表格 → 结构化/CSV |
| collect-list | automate | 按选择器翻页/滚动采集列表 |
| reader-mode | restyle | 就地重排为阅读版式（可还原） |

**验证**：`inspect-page` 在 GitHub 真实页面识别出列表候选/分页/页面全局；
`extract-article` 输出正确 Markdown；agent 创作新能力后**立即可发现可运行**。

## M8 动态站点 / 安全加固 / 文件上传（2026-08-24）

- [x] **动态站点注册**：`GET /sites`、`PUT /site/{name}`、`DELETE /site/{name}`，
      CLI `wb sites [--add N --match P --home U]`。内容脚本本就匹配 `<all_urls>`，
      所以加站点**不需要改 manifest、不需要重载扩展**，写进 config.json 即时生效。
- [x] **敏感站点黑名单**（比收窄 host_permissions 更实际：收窄会砍掉"任意站点通用能力"
      这一核心价值）。`config.BLOCKLIST` 默认拦银行/支付/密码管理器/健康门户/chrome://，
      在 bridge 层对 exec / capability / adapter 三条路径统一强制，任何调用方都绕不过。
      实测：`--url https://www.icbc.com.cn/login` 被拒并给出可操作提示。
- [x] **文件上传真机验证**：`wb chatgpt "…" --file X` 模型正确读出文件内容。

### 本轮修掉的四个真 bug（都靠"让程序自报状态"而非推理定位）

1. **丢弃的标签无法注入**：Chrome 省内存会把后台标签 `discarded`，注入报
   "manifest must request permission" 这种误导性错误。`injectable()` 现在同时检查
   生命周期，且只匹配到休眠标签时先 `reload` 复活它。
2. **旧适配器代码不被顶替**：`if (PAGE.adapters.chatgpt) return` 的"已注册"守卫，
   让扩展重载后注入的新适配器被跳过，页面里跑的仍是旧逻辑——修复静默失效一小时。
   已删除该守卫（最新注入必须获胜），并在代码里写明原因。
3. **带附件时首次点击被吞**：附件服务端处理期间点击无效。现在用三个独立信号确认
   已发送（stop 按钮 / 回合数增长 / **输入框清空**），未发出则最多重试两次。
4. **适配器结果回传依赖 relay，而 relay 会消失**（页面跳转后 ISOLATED 半边掉了，
   MAIN 半边还应答 ping 所以不会被重注入）→ 页面里活干完了、调用方却一直超时。
   **适配器改走与 exec 相同的 userScripts 通道**，请求和回复同路，不再依赖 relay。

**诊断方法论**：这四个都不是靠读代码想出来的，而是靠给页面加 `window.__wbAskStage`
阶段标记 + 在注入失败时回报目标标签的 `status/discarded/url`。凡是"卡住"类问题，
先让程序自报状态，再动手改。

## M9 收尾（2026-08-24 · 项目完成）

- [x] **扩展弹窗真机验证**：在 `chrome-extension://<id>/popup/popup.html` 实际渲染并截图确认，
      能力卡片/分类标签/参数/可复制命令全部正常。顺带修掉一个 bug：在扩展页或 chrome://
      打开时，命令示例的 `--url` 会被填成扩展 ID（那里的 hostname 就是扩展 ID）；
      现在只对 http(s) 页面给出 `--url` 建议。
- [x] **`new_chat` 实现**（此前是**空操作**，参数被静默忽略，导致每次提问都堆在同一个
      会话里、上下文互相污染）。现在 SW 在注入适配器前先把标签导航到站点 home。
      实测：`--new` 后 URL 变为新的 `/c/<id>`、回合数从 0 开始。
- [x] **relay 路径定位与加固**：exec 与 adapter 现在都走 userScripts；relay 仅作兜底
      （userScripts 需要浏览器"允许用户脚本"开关，新配置默认关闭）。兜底路径**改为快速失败**
      ——先等 relay 的 ack，1.5s 内没有确认就立刻报错，不再让调用方干等到超时。

### 最终状态

全部里程碑完成。回归 10/10；真机六项验收全通过（动态站点 exec、抽取能力、探查能力、
站点适配器、黑名单拦截、站点列表）。

## M9 可靠性与能力扩展（2026-08-24 · 第二轮）

**参数校验**（`capabilities.validate_params`）。`params` 元数据从「说明」变成契约：
类型强制（"800"→800、"true"→true）、必填、enum、min/max、默认值在 bridge 侧补齐；
未知参数用编辑距离给「是否想写 X」。错误码 422，正文就是一份可照着改的参数表。
CLI/MCP 都透传。同时 `save` 增加 `lint()`：kind 合法、description 非空、match 是
非空数组、params 结构正确、有函数体——**不合格不写盘**，更新失败回滚原文件
（LLM 写能力最典型的失败就是「看起来合理但字段乱编」）。

**弹窗可以直接运行能力**。按参数元数据生成表单（数字/布尔/枚举下拉/JSON 文本），
运行走的就是 `/capability/{id}`（校验、黑名单、标签页解析一条路），结果就地展示，
可复制/下载。下载用 blob 锚点而不是 `chrome.downloads`——避免新增权限（新增权限
必须用户手动重载扩展）。
验证：`bridge/popup_harness.py` 把真实 popup 渲染成普通网页（扩展页面无法被注入），
实测表单→参数、默认值补齐、枚举、JSON 字段、结果区都对。

**错误不再说谎**（三个真 bug，都是"看起来像另一回事"）：
1. 页面里抛异常 → 之前 `result: null`，与「没抓到数据」无法区分。注入包装器现在
   自己 try/catch，把 `{__wbError, __wbStack}` 当数据回传，SW 再抛出。
2. userScripts 失败自动退回 relay → relay 用 `new Function`，在 Trusted Types 站点
   （youtube.com）报「Evaluating a string as JavaScript violates…」，把真因盖掉。
   现在只有 `chrome.userScripts` 不可用才兜底。
3. 打不开的 URL → 「Frame with ID 0 is showing error page」，读起来像权限问题。
   现在翻译成「目标页面没有加载成功」，并且**把自己开的那个错误标签页关掉**
   （`ourTabs` 记着哪些标签页是 web-bridge 开的，重试时也认得出来）。

**hub 单槽位修复**：第二个 WS 连上会顶掉前一个（测试里就是这样撞见的）。现在被顶掉的
socket 会被显式关闭（对方随即重连），而仍在说话的 socket 会被重新认领——以前这会表现
为「扩展突然断连」。回归测试从 10 项加到 19 项（新增：默认值/未知参数/必填/枚举强制、
元数据体检、黑名单三路径、能力详情、socket 顶替后恢复）。

**新增 `/close`**（+ `wb close` + `web_close_tab`）：自动化开的标签页要能自己收拾。
必须给明确目标（url 片段或 tab id），没有「关全部」。

**新增站点能力**（都在真实页面验证过）：
- `youtube-transcript`：timedtext 接口现在返回 200 空 body，`youtubei/v1/get_transcript`
  报 Precondition failed（缺签名头），所以改成**驱动页面自带的字幕面板**再读 DOM，
  读完关回去。实测 572 段英文字幕、可切「Chinese (China)」、带时间戳模式正常。
- `x-posts`：X 的时间线是虚拟列表（节点会回收），所以边滚边按永久链接入 Map 收割，
  不能滚完再读。实测：个人页滚 6 轮拿到 25 条不重复、详情页拿到整条串。

## M10 常驻服务（2026-08-24）

bridge 从「哪个 CLI 调用碰巧把它拉起来的」变成 launchd 托管的常驻服务
（`bridge/service.py` + `wb service install|status|restart|logs|uninstall`）。

- plist：`~/Library/LaunchAgents/com.web-bridge.server.plist`，Label `com.web-bridge.server`，
  `RunAtLoad` + `KeepAlive={Crashed, SuccessfulExit:false}` + `ThrottleInterval 10`，
  日志 `~/Library/Logs/web-bridge.log`。解释器选 `/usr/bin/python3`（稳定系统路径，
  不绑 Xcode.app 内部路径），且是**真的 import 过 fastapi/uvicorn/websockets** 才选中。
- **端口归属唯一化**（这条链路最老的失败模式）：install 先杀掉端口上的旧监听进程；
  server 启动时判断端口归属——被另一个健康 web-bridge 占用则打印说明并 exit 0
  （干净退出 → launchd 不重启，避免死循环），被别的程序占用则报错 exit 1（10s 后重试）。
  CLI 与 MCP 检测到服务已装就不再自己 spawn，改 `launchctl kickstart`。
- **修掉一个危险命令**：`lsof -ti :8790` 会连**持有连接的 Chrome 进程**一起列出来，
  `service.free_port()` 初版会把浏览器 kill -9 掉。改成 `lsof -ti tcp:PORT -sTCP:LISTEN`，
  HANDOFF 里那条同样危险的教训命令也一并改了。
- 日志在 install/restart 时按 5MB 轮转（launchd 在进程启动时打开日志，这是安全时机）。

实测：kill -9 后 **1 秒**自愈、扩展 0.5 秒自动重连；第二个 server 实例正确让位（exit 0，
不影响在跑的服务）；19 项回归全过；`wb status` 会注明当前是「常驻服务」还是「临时进程」。

## M11 三个新能力（2026-08-24）

**`google-search`（站点）**：导航会杀掉注入脚本，所以走**同源 fetch + DOMParser** 解析
结果页——保留用户的登录态、地区和个性化，但不动任何标签页内容。支持 page / recent
(day|week|month|year) / lang。结果定位用结构规则（「`<a>` 里的 `<h3>`」）而不是 class 名
（Google 的 class 全是混淆的且天天变）；摘要取 `div[data-sncf]`，比「块文本减标题」干净
（后者会带上重复的面包屑和 "Read more"）。相关搜索限定在 `#botstuff` 内，否则会把
Images/Videos/News 这些结果类型 tab 当成相关搜索。
**坑**：`--url google.com` 会匹配到 mail.google.com 标签页（子串匹配），必须写 www.google.com。

**`x-post`（站点）**：发帖/回复。在主页/个人页=发新帖，在帖子详情页=回复那条，
所以「回复」就是 `--url <帖子链接>`。发送确认看输入框是否清空 + 提示条里的 `/status/` 链接
（提示条消失得快，要和轮询同一轮抓）。`dry_run` 只填不发。
**真机验证**：发帖 + 回复各一条，并用 `x-posts` 读回确认。
**修掉的坑**：`execCommand("insertText")` 之后**不能**再手动 dispatch 一个 input 事件——
和编辑器自己的原生事件抢，导致文案被插两遍。

**`site-search`（通用）**：任意站点的站内搜索。流程是
找搜索框（含**穿透 open shadow root**，合成 shadow 表单验证过）→ 读它所在 form 的 action/method
和其它字段 → 同源 fetch 结果页 → 用「最大的、含实质链接的兄弟块组」解析成列表。
没有可用 form 时依次试常见搜索路径（`/search?q=`、`/?s=` …）。都不行且找得到输入框时，
退化为在页面里真的输入并观察新出现的链接（in-page 模式）。
识别「精确命中直接跳转」（Wikipedia 那种）并返回 direct_hit 而不是硬解析。
**质量三规则**（都是被真实站点打脸后加的）：不要指回搜索页自身的链接（GitHub 筛选侧栏）、
不要 nav/header/footer 里的链接、整组结果至少要有一行提到查询词（MDN 顶部导航）。
解析不出来就返回 0 条 + results_url + 原因，让调用方 `wb open` 后接 collect-list。
**真机验证**：npm ✅ 3 条真结果、Wikipedia ✅ 3 条 + 直达命中识别、
GitHub / MDN ✅ 正确识别为「JS 渲染、HTML 里没有结果」并给出搜索地址。

能力总数 12，lint 全过，回归 19/19。

## M12 自进化：exec 日志 → 自动沉淀（2026-08-24）

能力库以前只能靠人手写：agent 写完一次性脚本就丢，`save_capability` 全靠自觉，没有任何
机制保证它发生。现在这条路自己会走。

**`bridge/journal.py`**：`/exec` 和 `/capability/{id}` 每次调用都追加到
`~/.config/web-bridge/exec-log.jsonl`（600，超 5MB 轮转），同时维护 `exec-index.json` 计数器。
**只记代码/URL/耗时/参数/成败，结果只记形状（类型/键名/条数）**——日志是账本，不是用户
页面数据的副本。

**归一化签名**是这套东西能不能转起来的关键：agent 几乎不会逐字节重复同一段脚本。
做法是**先把字符串字面量摘成内容 hash**，再剥注释、压空白、去掉标点旁的空格。
先保护字符串有两个硬理由（都实测踩过）：`"https://…"` 里的 `//` 会被注释正则吃掉后半行；
压空白会让 `"div a"` 和 `"diva"` 这两个不同选择器撞成同一段脚本。

**自动沉淀**：同一站点上成功跑满 `promote_after`（默认 3）次 → 写
`capabilities/auto/auto-<host>-<sig6>.js`，元数据机器生成（标题取脚本第一行注释，
参数由上次 args 推断，带 `auto:true` 和 runs/first_seen），立刻可被发现、可运行、可被
`save_capability` 用同名 id 覆盖成人写版本。

**三个出口**（agent 不需要事先知道日志存在也能撞上这条路）：
1. exec 返回值里的 `journal.hint`——第 2 次预告、第 3 次通知沉淀结果
2. `GET /capabilities?url=` 附带 `prior_scripts` + `prior_hint`：**在"找现成能力"那一刻**
   就把这个站踩熟的路摆出来，这是最关键的一个出口
3. 主动查：`wb log [关键词] [--host] [--code]` / MCP `web_journal` / 直接 grep jsonl

回归 19 → **23 项**（新增：重复计数、第三次自动沉淀、沉淀后可被发现、排版变化不算新脚本；
测试会自清理，不污染真实日志）。MCP 工具 11 → 12。

## M13 并发与锁（2026-08-24）

M12 收尾时撞上「exec 全部超时、扩展却显示已连接」，查出来**两个先前就潜伏的真 bug**
（诱因是本机另一个 agent 会话正在跑 `wb chatgpt`，adapter 命令一占 5 分钟）：

1. **锁绑错 event loop**：`asyncio.Lock()` 在模块导入时创建，Python 3.9 会把它绑到当时
   `get_event_loop()` 的 loop，而 uvicorn 跑的是另一个。无争用的快路径不碰这个绑定，
   所以一直没事；第一次并发就 `got Future attached to a different loop` → 裸 500。
   改为在真正 await 它的 loop 里惰性创建。
2. **全局一把锁**：docstring 写的是 "one command at a time per tab"，实现却是全局单锁。
   一个 `chatgpt.ask` 把所有调用（含其它站点、含只读的 tabs）排到后面直到超时；客户端
   放弃后服务端仍占着锁到 305s。改为**按 site/url 分锁**：只读命令（tabs/open/close/reload）
   不加锁，同目标排队超过 `queue_wait_ms`（默认 20s，exec/capability 可传）返回 503 并说明
   「目标 X 上正在跑什么、多久了」，`/health` 增加 `inflight` 列表。

**测试隔离**：mock 扩展会抢 hub 的扩展槽位，真扩展被挤掉会重连再把 mock 挤掉——互踢导致
随机超时，还搅乱用户浏览器。新增 `bridge/run_tests.sh`：独立端口 + 临时 state 目录起一次性
server 跑测试。回归 23 → **27 项**（新增：只读命令不排队、不同目标并发、同目标忙时报 503、
/health 报告在跑的命令）。

排查手段：`config.debug_ws: true` 打开 WS 收发跟踪（代码常驻，默认关）。这次就是靠它看到
「服务端根本没发出 command，只有 ping」才定位到卡在锁上，而不是扩展的问题。

## M14 结果不再丢 + 不抢标签页（2026-08-25）

起因是 `ISSUES-2026-08-24.md`：连跑三张出图，**页面三张全画好了，CLI 一张也没拿到**。

**真凶不是崩溃，是重启。** M13 的锁修复是对的，全仓再扫一遍也**没有**第二处 import 期构造
的 asyncio 对象。err.log 里那串 `attached to a different loop` 全是 23:32 之前旧进程留下的
（栈里的行号在当前文件已不存在）；23:39:30 与 23:47:21 两次重启**之后 err.log 一个字节都没写**，
也没有 crash report，而 `launchctl print` 记的是 `last terminating signal = Terminated: 15`
——SIGTERM，即 `launchctl kickstart -k`（`wb service restart`）。launchd 的 `runs = 12` 与
日志里 12 条启动横幅一一对应，时间点也和当时正在改代码的文件 mtime 吻合。

于是这一轮修的是**「长命令怎么活下来 / 死了怎么捞回来」**：

1. **结果缓存**（`bridge/results.py`）：每个驱动页面的调用带 `request_id`，结果同时存内存和
   `~/.config/web-bridge/results/<id>.json`（600），`GET /result/{id}`、`GET /results`
   事后领取，**重启后仍在**；同一个 id 再 POST 会附到正在跑的那次上、绝不重跑
   （重试若把提示词再发一遍就是再花一次额度）。CLI/MCP 在连接断掉时自动补捞。
2. **`wb chatgpt-last`**：不发消息，直接把当前会话（或 `--conversation <id>`）的最后一条
   回答连图片取回来。会话树**从 `current_node` 顺 `parent` 走**（按 create_time 排会把问答
   颠倒），图片在 `role:"tool"` 的 `image_asset_pointer` 里、按 asset id 去重（同一张图会在
   tool 和 assistant 两处各出现一次），签名下载链接同源 cookie 鉴权**必须在页面里 fetch**，
   `/backend-api/conversation` 只读一次、429 指数退避。
3. **重启不再盲目**：`service restart|install|uninstall` 发现有命令在跑会拒绝并列出在跑什么，
   要打断加 `--force`；退出时日志写明打断了谁。
4. **`/health.build`**：版本 / pid / 启动时间 / 源码 sha / `stale`（磁盘代码比进程新），
   `wb status` 直接提示「你排查的是旧代码」——这次就是被这个坑绕了一大圈。
5. **mock 不再顶掉真扩展**：日志里那个 `hello {'mock': True}` 是有人直接跑了
   `test_mock_ext.py`（默认连 8790），10 秒内和真扩展互顶约 40 次，正是「WS 反复掉线」的来源。
   现在 mock 连接带 `client=mock`，生产端口直接拒绝，两个 mock 脚本自己也拒绝对 8790 启动。
6. **不抢别人的标签页**：扩展按站点记住自己的标签（`chrome.storage.session`），挑标签时探测
   `window.__cgo` / `dataset.cgoOwned` 跳过他人占用的页，自己占的页打 `window.__webBridgeOwned`，
   全被占且有 `home` 就另开一个。`--new` 的导航只会落在自己的/无主的标签上。

回归 27 → **34 项**（新增：/health 报告 build、结果可事后领取、同 id 不重跑、未知 id 404
指向 chatgpt-last、失败也记录、/results 列表、mock 连接带标识）。

## M14 侧栏重构 + 本地 agent 对话 + page-beauty 并入（2026-08-25）

把 `~/Downloads/page-beauty`（独立的 MV3 扩展：保存 AI 写的 JS、按站点注入、
enhance 自动运行 / extract 手动出 JSON）并进来，同时把扩展从 popup 改成**右侧驻留侧栏**。

**没有照搬 page-beauty 的实现**，只并入它的两个概念：
- 「enhance = 页面加载时自动运行」→ 能力元数据的 `autorun` 字段
- 「extract = return JSON」→ 本来就是 `kind: extract` 的语义

它原来的脚本存 `chrome.storage.local`、自带一套 match 匹配和悬浮球；这些都没要——
脚本统一存服务端 `capabilities/`，复用已有的元数据、参数校验、lint、自动沉淀。
好处是**对话里生成的脚本、CLI 存的脚本、自动沉淀的脚本、侧栏存的脚本是同一批东西**。

**侧栏三标签**（`extension/sidepanel/`）：对话 / 脚本库 / 页面。侧栏零业务逻辑，
全部走 CLI 和 MCP 用的同一批 HTTP 路由。

**本地 agent 对话**（`bridge/agents.py` + `/agents` `/agent/ask` `/agent/run/{id}`）：
安装服务时探测 claude/codex/dsh 写进 config；三种流式格式归一成一串事件；
`/agent/ask` 走 NDJSON 流，run 存服务端可重新接上。对话里 agent 回答的 js 代码块
带「存成脚本 / 在本页运行」，这是**对话 → 能力库**的入口。

**修的两个真 bug**：
- `[hidden]` 被 CSS 的 `display:flex` 盖过——侧栏里每个隐藏块（上下文条、编辑表单、
  结果卡）都渲染成了空盒子。加一条 `[hidden]{display:none!important}` 一次性根治。
- `re.sub` 会展开替换串里的转义：harness 生成器里 JS 字符串的 `\n` 变成了真换行，
  生成的脚本直接语法错误。改用 lambda 替换。

回归 34 → **40 项**（新增：agent 名册、未知 agent 拒绝、空 prompt 拒绝、autorun 存/开/列、
裸域名转 match pattern、extract 拒绝 autorun）。

## M15 侧栏收尾（2026-08-25，/loop 若干轮）

M14 之后用 /loop 自定节奏做的收尾，每轮一个可验证的点：

**对话跨关闭恢复**。侧栏一关就被销毁（切窗口就会），正在进行的对话随之消失——而那是 agent
已经花了时间和额度产出的东西。对话记录 / 选中的 agent / session id 存 `chrome.storage.local`；
恢复的历史里那些失效按钮直接剥掉，不留半可用的东西。

**参数声明**（功能缺口，不是打磨）。面板此前只能存 `params: {}`，等于从 UI 根本触达不到
bridge 的参数校验。表单现在能编辑参数声明（名字/类型/必填/默认值/说明）并带出已有脚本的参数。

**对话 markdown 渲染**。agent 回答本是 markdown，之前当纯文本显示。渲染器无依赖（面板 CSP 严格），
**先整体 HTML 转义再放回有限几种行内格式**——agent 的回答里可能夹带它刚读到的网页内容，
这条路必须堵死。用 `<img onerror>` / `<script>` 载荷实测：渲染成可见文本，不执行。

**agent 长任务断线重连**。run id 跟对话一起存；重开面板时问 bridge：跑完了直接渲染，
还在跑就 `?follow=true` 跟到结束。真机验证：起真 claude 任务、3 秒后断开、重新接上拿到完整答案。
和 chatgpt 那条「活干完了结果却丢了」是同一类问题。

**脚本库搜索**（id / 名称 / 说明 / 匹配范围）。

**harness 桩改成真文件** `bridge/harness_stub.js`。JS 里的 `\n` 被两道转义层各吃掉一次
（三引号字符串一道、`re.sub` 替换展开一道），生成出语法错误的 harness——**同一个坑踩了两次**，
因为第一次只修了出错那行、没动"把 JS 塞进 Python 字符串"这个形状。现在没有任何转义层。

**tool 事件改成人话**：`⚙ Read {"file":"x"}` → `⚙ 在页面执行 JS：return document.title;`，
完整调用留在 hover。

**autorun 参数默认值**（真机回归才发现，harness 测不出来）：自动运行注入的是裸 `{}`，
于是同一个脚本按「运行」时 `args.color` 有值、页面加载自动跑时 undefined，静默走兜底分支。
现在 `/capabilities/autorun` 返回填好默认值的 args。连带：**有必填参数的能力不允许开自动运行**——
页面加载时没人传，开了也只会失败。

回归 40 → **45 项**。
