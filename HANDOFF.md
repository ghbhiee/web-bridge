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
  mcp_server.py    stdio MCP，零依赖，17 个工具
  capabilities.py  能力注册表：扫目录、解析元数据头、URL 匹配、参数校验、元数据体检
  journal.py       exec 日志 + 归一化签名计数 + 跑满 3 次自动沉淀成能力
  agents.py        本地 agent 运行器（claude -p / codex exec / dsh），探测 + 流式事件
  run_tests.sh     用一次性 server（独立端口+临时 state）跑回归，不干扰实时服务
  config.py        读 ~/.config/web-bridge/config.json（token/port/sites/blocklist）
  register_mcp.py  把 MCP 注册进 4 个 agent（幂等）
  mock_ext.py / test_mock_ext.py   无浏览器时的假扩展 + 回归测试（19 项）
  panel_harness.py 把扩展侧栏渲染成普通网页以便测试（见「测试与验证」）
  harness_stub.js  harness 用的桩（真文件，避免多层转义把 JS 改坏）
  service.py       服务安装/卸载/重启/状态/日志；macOS 走 launchd，Windows 走 Startup 目录 .cmd
  gen_ext_config.py  跨平台生成 extension/config.js（原来是 bash，Windows 用不了）
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
| **Page Tools** | **用户自己的脚本**：贴一段 JS 就能在这页跑，可开自动运行；带 6 个提示词模板（复制给任意 AI 用，用户不一定用这里的 agent 写）；也可以一键转到对话让 agent 帮写 |
| **Agent Tools** | **agent 自己的能力**，只读。只给名字 + 说明 + 用过几次，**不显示代码**——这是给用户看「它会做什么、在这个站做过什么」的清单，不是让用户读机器写给机器的代码 |

对话里 agent 的 markdown 由 `renderMarkdown()` 渲染（无依赖，CSP 严格）。
**安全**：agent 的回答里可能夹带它读到的网页内容，所以**先整体 HTML 转义、再放回有限的
几种行内格式**——从页面文本到活markup 没有通路。真机用 `<img onerror>` / `<script>`
payload 验证过：变成可见文本，不执行。

### 先别急着上向量检索：语料小的时候，LLM 就是最好的语义匹配器

全部 14 个能力的一行摘要加起来 **~580 token**。对这个体量，「替模型挑」比「把目录给模型」
更差——它天生跨语言、懂口语，而且已经在环路里。所以 `catalogue()` 在总量低于
`CATALOGUE_BUDGET_CHARS`（2600 字符）时，**把整个库列进简报**，超了才退回排序粗筛。

实测（这两条正是词法打分器答错的）：
- 「把网页上的数据弄成 excel 能用的样子」→ 模型选 `extract-tables` ✅（打分器选了 netflix）
- 「这篇文章排版太乱了想安静地读」→ 模型选 `reader-mode`，并主动解释为什么不是
  `extract-article`（"那个是抽成文本给你读，页面本身不变"）✅

**这才是「向量检索跑不起来」的正确答案**：不是换一个向量库，是这个规模根本不需要向量。
排序逻辑没有白写——它是库长大以后的粗筛器，`CATALOGUE_BUDGET_CHARS` 就是切换点。

### 按意图找工具（`bridge/toolsearch.py`）

`wb find "把网页表格存成 JSON"` / MCP `web_find_tool` / `GET /tools/search?q=&url=`

**三件事一起解决：**

1. **网址不再是过滤器，只是加权**。以前 `capabilities.for_url()` 只按 `match` 筛，
   于是站在 google.com 上问「查电影在哪些国家能看」什么都搜不到——用户的**意图**
   输给了他**碰巧站在哪**。现在 URL 命中只是 ×1.6 的加权，别的站点的工具照样能排上来。
2. **跨语言**。`table` 和 `表格` 是同一个问题：内置同义词表 + CJK 单字/二元切分
   （中文没有空格，按词切会漏）。
3. **排序看战绩**。`relevance × 质量 × 时新` —— 质量用 Laplace 平滑的成功率
   （一次失败不至于判死刑，一次成功也不至于封神）+ 用量对数。
   跑得多又成功的工具往前排，一直失败的往后沉。

**淘汰机制**：`web_rate_tool {id, ok:false, note}`。日志只知道脚本有没有抛异常，
**不知道答案对不对**——只有用它的模型知道。差评计入质量因子，实测 4 次差评把
`extract-tables` 从 7.29 压到 5.47。

**上下文成本是硬约束**：只回 top-N（默认 5），每条一行摘要 + 参数名，不回代码、不回全表。
简报里也只放排名前 4 的本页工具。

**索引位置**：`~/.cache/web-bridge/web-bridge-tools/`（Windows 是
`%LOCALAPPDATA%\web-bridge\cache\`）。是从 `capabilities/*.js` 派生、随时可删可重建的数据，
所以放缓存目录，不放配置目录。目录名要唯一——叫 `toolindex` 时 `qmd collection add`
撞上了一个已经存在的、根在**仓库目录**的同名 collection，静默失败，
于是搜索结果里混进了 HANDOFF.md / ROADMAP.md。现在代码里也只认已知的能力 id 兜底。

**qmd 集成：接好了，但实测在这个语料上贡献为零**（qmd 装了就用，`WEB_BRIDGE_QMD=0` 关掉）：
- `qmd search`（BM25，0.13s）：开关它，10 条查询的排序**一模一样**
- `qmd vsearch`（向量）：**能用了，靠 `QMD_FORCE_CPU=1`，不用重编译任何东西**。
  GPU 路径要 node-llama-cpp 现场编译 Metal shader，而这台机器缺 Metal Toolchain
  （`xcrun metal` 直接拒绝），编译失败后**无限等待而不是报错**。强制 CPU 推理绕开整个
  shader 编译，同样的查询 ~5s 出结果（加 `--no-rerank` 省掉第二次模型加载）。
  代码里 `_qmd_env()` 默认设了这个变量。
  **副作用：llm-wiki 的向量检索也一起恢复了**（实测 ob 库 1.3s / 17 条命中）——
  它之前卡的是同一个原因，`kb.py` 里也该加上 `QMD_FORCE_CPU=1`。
  （另一条路是 `xcodebuild -downloadComponent MetalToolchain` 把工具链装回来，
  但那要能连上苹果的资产服务器，而且不是必须的。）
- 历史记录：这台机器上向量曾经完全不可用——
  `xcrun metal` 报 `missing Metal Toolchain`，node-llama-cpp 编译不出 shader，
  于是 `ggml_metal_library_init_from_source: error compiling source` 后无限等待。
  **llm-wiki 自己的 ob 库现在也一样卡**，同一个报错，所以这是机器层面的
  （`xcodebuild -downloadComponent MetalToolchain` 可修，需要能连上苹果的资产服务器）。
  向量路径默认不走，要试设 `WEB_BRIDGE_QMD_VECTOR=1`；所有 qmd 调用都有硬超时。
- 索引隔离：`INDEX_PATH` + `QMD_CONFIG_DIR` 指向 `~/.cache/web-bridge/qmd/`，
  `index.yml` 里显式写 embed/rerank 模型（照抄 llm-wiki 里能用的那套）。
  **之前用全局索引是我的错**：全局索引没有 models 配置，而且里面那个根在仓库目录的
  collection 让 HANDOFF.md 混进了搜索结果。`qmd embed` 在隔离索引上是能跑的（14/14 已嵌入）。

**当前命中率（10 条基准查询）：6/10**。直白问法（表格 / table / 查电影在哪些国家能看 /
视频文字版 / 抓列表翻页）基本都对；**口语化问法仍然不行**——
「把网页数据弄成 excel 能用的样子」→ 命中 netflix，「这篇文章太乱了想安静地读」→ 命中
extract-article 而不是 reader-mode。这是词法打分的天花板，靠加同义词是补不完的，
**要真正解决得让 qmd 的向量索引跑起来**（先修 `qmd embed` / vsearch 的本机环境）。

**一个已经修掉的排序 bug**：战绩分曾经能无中生有——`netflix-title-countries` 因为跑过 5 次成功，
在「看看这页有什么可以抓的」这种几乎不相关的查询上也排第一。现在先算相关性、
把远低于最高分的候选直接剔除（`floor = best * 0.28`），战绩只在**相关性接近的候选之间**做区分。
（试过给相关性加 1.4 次幂让它更主导，实测 6/10 → 6/10，没有收益，就没留这个魔法数字。）

**怎么让 Agent Tools 更容易命中**（实测有效的顺序）：
0. **hint 必须放在所有调用方都看得到的地方**。简报只对**侧栏发起**的 run 生效，
   而实际重复造轮子发生在 dsh / 终端里的 MCP 客户端——那条路没有简报。
   所以 `/exec` 的**返回值**里带 `tools_available`：这个站有专属能力时，
   在 agent 正在手写 JS 的那一刻告诉它。没有专属能力就不带，否则变成噪音。
1. **把工具直接塞进简报**（`agents.available_tools_block`）——不要让 agent 去「想起来问」。
   面板知道 URL，bridge 就能把这个站点已有的能力（名字/参数/说明）写进 system prompt。
   实测：同一个问题，之前 1 次能力调用 + 32 次现写；加了这块之后 **2 次能力调用、0 次现写**，
   而且它连 `web_capabilities` 都没调——工具是被递到手上的，没有「想不起来」的余地。
2. **只列站点专属的**。把 6 个通用能力也堆进去等于噪音，会训练 agent 跳过这一段。
3. **供给不足要单独说**（`agents.adhoc_hint`）：某站现写超过 8 次且一个站点能力都没有，
   简报里直接提醒 agent 做完问用户要不要沉淀。

**三种情况必须分开看**（`wb stats` 分三栏），按「重复度」判而不是按次数：
- **开发调试**：每段脚本都不一样、没有重复 → 这是在**写**一个页面脚本，不是缺工具。
  37 次现写 / 37 段不同脚本就是这个形状。按次数判会把它误报成「该沉淀一个能力」，
  然后简报去催 agent 沉淀一件从没重复过的事。
- **缺工具**：同样的活反复现写（repeats 高）却没有站点能力 → 真的该沉淀
- **没命中**：有工具却没被调用 → 才是命中问题

**自动沉淀有门槛**（`journal.looks_trivial`）：不碰页面/网络的脚本（只 reload、只读
`document.title`）再重复也不沉淀。真事故：我调试时反复刷新页面，
`location.reload();return 1` 被沉淀成了一个叫「🤖 location.reload();return 1」的能力，
还让 example.com 在统计里显示成「有工具没命中」。

**复用率**：`wb stats`（面板 Agent Tools 标签顶部也有）——存下来的工具到底有没有被用上，
还是每次都在现写 JS。这个数字是「能力库有没有价值」的唯一诚实答案；低于 20% 会标红。
以前只能手翻 `exec-log.jsonl` 才知道，等于没人知道。

**标签名就是它们的区别**：`Page Tools` = 给人用的，`Agent Tools` = 给 agent 用的。
两个都叫「脚本库」的时候，用户说「存到脚本库」根本没法区分——真实踩过：用户要把一个
能力存给 agent 复用，agent 只有 `web_save_page_script` 一个出口，存进了用户的页面脚本。
现在对话里的代码块下面有**两个按钮**（存为 Page Tool / 存为 Agent Tool），
简报里也按「给谁用」分流两个工具，分不清就问。

存为 Agent Tool 时面板自己生成能力元数据头（id 必须是 ascii——id 就是文件名，
`capabilities.save()` 会把非 `[A-Za-z0-9_.-]` 全替换成横线，中文标题会变成一串横线）。

**两个库是分开的，这是设计而不是遗漏**：`capabilities/` 是 agent 写给 agent 用的（带参数
声明、kind、给 agent 判断用的描述），`user-scripts.json` 是用户自己的（代码就是全部意义，
用户要读要改）。早期版本把它们合并存储，结果用户的脚本被埋在一堆机器facing 的条目里，
而 agent 的能力发现里混进了一次性的页面小改。

**侧栏不实现任何逻辑**：每个动作都走 CLI/MCP 用的同一批路由（`/agent/ask`、`/capability/{id}`、
`/exec`、`/journal`），所以参数校验、敏感站点黑名单、标签页解析、exec 日志只有一份实现。

### 对话开发脚本 → 存进用户脚本库（这是「对话」标签存在的意义）

用户在对话里说「美化这个页面」，agent 做**两步**（简报里是硬性要求）：
1. `web_exec` 把脚本**真的跑上去**——只探查不动手，用户眼里什么都没发生
2. 把 JS **贴在回答里**（```js 块），用户要看得见写了什么

**第三步是用户的**：面板在代码块下面给一个「保存到我的脚本库」按钮，**一点即存**
（不跳转、不用再填一次表单）。agent **不许自己保存**——用户通常还要接着改几轮，
存不存、什么时候存由他决定。只有用户明确说「保存」时 agent 才用 `web_save_page_script`。

**同一次对话里再存 = 更新那一条**（按钮会变成「更新「脚本名」」），因为"改几轮再存"
就是这个功能的正常用法；每次新建会攒出一堆几乎一样的脚本。想要副本用「另存为新脚本」，
清空对话后指针重置。更新时**只送 code**：名称、匹配范围、autorun 开关是用户在面板里设的，
`user_scripts.save()` 对没送的字段保留原值——否则对话里存一次就把用户开的自动运行关掉了。

**`web_save_page_script` 是补的洞**：拆分两个库之后，MCP 里只有 `web_save_capability`
（agent 自己的能力库），**没有任何工具能写进用户脚本库**——agent 想存也存不了，
所以那次「美化页面」既没注入也没保存。

真机验证过整条链：说一句 → 页面立刻变（背景/字号/style 标签都在）→ 代码在回答里 →
「页面」标签里出现这个脚本 → 刷新自动生效 → 关掉自动运行后刷新完全还原 → 手动运行按钮可用。

**导出书签**：「页面」标签每条脚本有个「书签」按钮 → `GET /user-script/{id}/bookmarklet`
→ **下载一个 .html 文件**。

要点是这个文件**要离开这台机器**：发到没装扩展、没有 bridge、没有 agent 的电脑上双击打开，
把按钮拖进书签栏就能用。所以页面必须完全自包含——样式内联、代码在锚点的 href 里、
不联网、没有 `<script>`、并且要能向一个从没听说过这个项目的人解释清楚自己是什么
（回归测试就是按这几条查的）。

早期版本用 blob URL 直接开标签页，**那个 URL 关掉就没了、发不出去**，等于没解决问题。
两个不能省的细节：必须是**拖**（Chrome 拒绝手输/粘贴 `javascript:` 书签，也没 API 能建）；
存的代码是函数体（可能 `await`、可能顶层 `return`），要包成 async IIFE 才不会一点就语法错。

**状态要说清楚是哪一层在忙**：以前一直显示静态的「运行中…」，用户分不清是 agent 在想、
工具在跑、还是脚本在页面上执行，只能干等。现在按事件切换阶段文案 + 秒表，
结束后**保留**「✅ 完成 · 用时 12s · $0.21」——「它还在跑吗」这个问题要有答案。

**导入/导出**：两个库都能整体或单条导出成 JSON（`/user-scripts/export`、`/capabilities/export`），
再在别的机器导入。**导入默认不覆盖**：id 已存在的会另存为「…（导入）」而不是替换——
导入不该悄悄吃掉已有的东西，让用户自己比对再删。

**工具条可点开**：一行摘要是用来扫的，出问题时要看的是完整调用；截断成一行等于把它藏了。
点一下展开成多行完整 JSON，再点收起。

**代码块默认折叠**（超过 12 行），带「展开全部 (N 行)」和「复制全部」。页面脚本动辄几百行，
全展开会把回答和保存按钮顶出屏幕。

**脚本要有说明**：简报要求 agent 在代码第一行写 `// 说明：…`，面板存成脚本说明；
更新时**追加**而不是覆盖（`· 2026-08-26 加了分页`），于是列表里能看出每轮加了什么。
列表还显示相对时间和作者（哪个 agent 写的）；能力库显示文件修改时间 + 是否自动沉淀。

**中文输入法**：`Enter` 在候选词窗口开着时属于输入法（选词），当成发送会把半截拼音发出去。
`keydown` 里先看 `isComposing` / `keyCode === 229` / `compositionstart-end` 三个信号
（浏览器对前两个的支持不一致，所以都查），`compositionend` 后延一拍再解除。

### 对话必须告诉 agent 它在哪

`agents.panel_brief()`：面板每次提问都带上 `{url, title}`，拼成一段简报注入
（claude 走 `--append-system-prompt`，不进对话记录）。**这不是锦上添花**——没有它的时候，
一次真实的「把当前页面存到 Evernote」是这样跑的：先 `osascript` 问 Chrome 开着什么页面，
再用 PAT 走 Confluence REST API 把页面重新抓一遍，**全程没碰过 web-bridge 一次**，
42 个事件几十次 Bash。简报里写死了：页面在这、用 web-bridge 的 MCP 取、
不许用 osascript 找浏览器、不许绕开这个页面另找接口、动作要收敛。
加上之后同一个任务：ToolSearch → 一次 `web_run_capability extract-article` → 出结果。

### 本地 agent（对话标签的后端）

`bridge/agents.py`。安装服务时自动探测 claude / codex / dsh 并写进 config.json 的 `agents`
块（`wb agents` 查看、`wb agents --detect` 重新探测、`--cwd` 指定工作目录）。

- 三个 CLI 的流式格式不同：claude 是 stream-json、codex 是 JSONL、dsh 是纯文本，
  `parse_line()` 把它们归一成同一串事件（text / tool / done / end）
- **run 会落盘**（`<state>/runs/*.json`，每 5 个事件写一次 + 结束时写一次，保留最近 40 条）。
  以前只在内存里，于是**重启 bridge = 杀掉正在跑的 agent + 抹掉全部历史**，面板拿着一个
  不存在的 run id，用户看到的是「agent 什么都没干」。我自己就这么干掉过用户一个跑了几分钟的
  任务。现在：启动时 `restore_runs()` 读回来，上个进程留下的「还在跑」状态会被标成
  「被 bridge 重启中断」并写进事件流——**说清楚发生了什么，而不是静默**。
- **`wb service restart` 会拒绝打断正在跑的活**（页面命令 + 侧栏 agent 任务都算），
  要打断得显式 `--force`。守卫在 `guard_inflight()` 里，和另一个会话为 chatgpt 结果
  加的那道是同一个。
- **子进程流的行长上限必须调大**：asyncio 默认 64KB/行，而 claude 的 stream-json
  一个事件就是一行，一个大工具结果就会让整个 run 以
  `Separator is not found, and chunk exceed the limit` 挂掉，**已经做完的活全丢**。
  现在 16MB，且真超了也只跳过那一行、不中断 run。
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

**Windows 侧**（2026-08-26 实测通过，细节见 `INSTALL-WINDOWS.md`）：没有 launchd，
改为往 `%APPDATA%\...\Startup\web-bridge.cmd` 放一行
`start "" /min pythonw -u bridge\server.py`，日志 `%LOCALAPPDATA%\web-bridge\server.log`。

- **日志不是由启动器重定向的，是 `server.py` 自己接管的**。这一点看着绕，但它是唯一能同时
  拿到「有日志」和「零控制台窗口」的写法：`start` 拉起的 pythonw 进程 `sys.stdout` 是 `None`
  （第一次 print 就死，且不留痕迹），而 `start` 又**不会把自己的重定向传给子进程**；
  用 `cmd /c` 包一层能拿到句柄，代价是那个 cmd 会跟着服务常驻在任务栏。
  所以 `server.py` 开头发现流是 `None` 就自己重定向到 `service.win_log()`。
- **崩溃自愈 Windows 上没有**（launchd KeepAlive 无等价物），进程挂了要手动起或重新登录。
- `agents.py` 起 agent 子进程时带 `CREATE_NO_WINDOW`，否则每条侧栏消息都会弹一个终端窗口
  （父进程 pythonw 没有控制台，Windows 就给每个控制台子进程新分配一个）。
- 平台判断一律用 `service.IS_WINDOWS`。例外只有 `agents.py`：`service.py` 自己 import 了
  `agents`，所以那边必须在函数内局部 `import service`，否则成环。

## 测试与验证

```bash
./bridge/run_tests.sh                  # 78 项，独立端口 + 临时 state，不碰实时服务
python3 bridge/panel_harness.py        # 生成 .harness/harness.html
```

**别直接跑 `test_mock_ext.py`**：它会连上 8790 抢真扩展的槽位，两边互踢。要单独跑就自己
设好 `WEB_BRIDGE_PORT` / `WEB_BRIDGE_STATE` 指向一次性实例。

**套件不自洽，注意 `sites`**：`WEB_BRIDGE_STATE` 不隔离站点表——它来自 `config.json`。
`exec.roundtrip` / `adapter.roundtrip` / 三个 `hub.*` 用例都要求 `chatgpt` 和 `github`
两个站点已注册，开发机上它们本来就在，所以从没暴露。**在一台全新的机器上（任何平台）
这 5 项会直接红**，看起来像平台 bug。跑之前用 `WEB_BRIDGE_CONFIG` 指一份带这两个站点的
临时配置，模板在 `INSTALL-WINDOWS.md` 的测试小节。

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
