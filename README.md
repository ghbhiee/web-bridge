# web-bridge

> Turn the pages **already logged in inside your browser** into capabilities any local agent
> can call: run JS in any site's MAIN world, or invoke a discoverable capability library,
> through a token-authenticated local bridge exposed as CLI + MCP. macOS + Chrome (MV3).
> Documentation below is in Chinese.

把「已登录的浏览器页面」变成任何本地 agent 都能调用的能力。通用地在**任意站点的
MAIN world** 里执行 JS 或调用站点适配器，经带 token 认证的本地 bridge，暴露为 CLI + MCP。

前身 `~/cc/chatgpt-bridge` + `~/cc/browser-extension`（保留不动）；本项目把它通用化、
换成 MAIN world 注入、加认证、做成 CLI/MCP 给多 agent 用。

## 为什么有用

浏览器扩展是浏览器官方允许的 JS 注入通道（页面 CSP 对扩展豁免），MAIN world 注入
让脚本能看页面全局、调页面函数、用页面登录态 fetch 同源接口。于是可以：驱动已登录
的 ChatGPT 出图/答题（复用账号，免 API 费用）、抓任意站点登录后才有的数据、把网页
能力封装成 agent 工具——全部不需要各站点的官方 API。

## 结构

```
bridge/
  server.py        FastAPI，token 认证，WS hub，/exec /adapter /tabs /open /reload
  cli.py           web-bridge CLI（自动拉起 server）
  mcp_server.py    stdio MCP（零依赖），暴露 web_exec / web_chatgpt_ask 等工具
  config.py        读 ~/.config/web-bridge/config.json（token/port/sites）
  capabilities.py  能力注册表：扫目录、解析元数据、URL 匹配、参数校验、元数据体检
  results.py       结果缓存：request_id → 结果，可事后领取（内存 + 落盘）
  mock_ext.py      无浏览器时的假扩展，用于测试（拒绝连生产端口）
  test_mock_ext.py headless 测试（19 项）
  popup_harness.py 把扩展弹窗渲染成普通网页以便测试（扩展页面无法被注入）
  service.py       launchd 常驻服务的安装/卸载/重启/状态/日志
capabilities/      能力库（写一个 .js 文件 = 新增一个能力）
extension/         MV3 扩展
  manifest.json    relay(ISOLATED) + page(MAIN) 双内容脚本 + SW
  background/service_worker.js   带 token 连 bridge，路由命令，按需注入脚本/适配器
  content/relay.js  ISOLATED 中继（有 chrome.runtime）
  content/page.js   MAIN 世界代理（exec + 适配器注册表）
  adapters/chatgpt.js  ChatGPT 站点适配器
  config.js        自动生成（token+ws url），由 bridge/gen_ext_config.sh 产出
```

## 安装

1. **加载扩展（一次性，需手动）**：chrome://extensions → 打开右上「开发者模式」→
   「加载已解压的扩展程序」→ 选 `~/cc/web-bridge/extension`。
   （程序化加载被安全策略拦截，故此步需手动；之后 `web-bridge reload` 可自动重载迭代。）
2. **生成扩展侧的 token 文件**（仓库里没有，故意的）：

   ```bash
   bash ~/cc/web-bridge/bridge/gen_ext_config.sh
   ```

   它从 `~/.config/web-bridge/config.json` 读取 token 写出 `extension/config.js`。
   还没有配置文件的话，先建一个：`{"token": "<随便一串长随机串>", "port": 8790}`（chmod 600）。
3. **把 bridge 装成常驻服务**（登录自启、崩溃自愈）：

   ```bash
   python3 ~/cc/web-bridge/bridge/cli.py service install
   ```

   装的是 launchd LaunchAgent（`com.web-bridge.server`），日志在
   `~/Library/Logs/web-bridge.log`；`service status | restart | logs | uninstall` 管理它。
   不装也能用——任何 `wb` 命令会临时拉起 server，只是关机重启后要等下一次调用。
4. `web-bridge status` 应显示扩展已连接。

## 用法

```bash
web-bridge status
web-bridge caps --url <URL>          # 这个页面能做什么（推荐入口）
web-bridge caps <能力id>              # 单个能力的参数表 + 源码
web-bridge run <能力id> --url <URL> --params '<JSON>'
web-bridge tabs [filter]
web-bridge open <url>
web-bridge close <URL片段|tabId>
# 在页面 MAIN world 执行 JS（函数体，args 在作用域，可 return/await）：
web-bridge exec 'return document.title' --url example.com
web-bridge exec 'return await fetch("/api/me").then(r=>r.json())' --site chatgpt
web-bridge chatgpt "画一只赛博朋克猫" --images --out ~/Desktop
web-bridge chatgpt-last --images --out ~/Desktop   # 不发消息，补捞当前会话最后一条回答
web-bridge result <request_id>                     # 连接断了？按 id 把结果领回来
web-bridge results                                 # 有哪些还能领
web-bridge reload      # 让扩展从磁盘重载
```

### 结果不会因为连接断了就丢

驱动页面的调用都带一个 `request_id`：结果存在服务端（内存 + `~/.config/web-bridge/results/`），
连接断了、服务重启了，都可以用 `wb result <id>` 领回来；同一个 id 再发一次不会重跑
（重试不会把提示词再发一遍、再花一次额度）。CLI 和 MCP 在连接断掉时会自动补捞，
补不到就退到 `wb chatgpt-last`——直接从页面把答案和图片读回来（走会话树，图片在
`role:"tool"` 消息里，签名下载链接必须在页面里 fetch）。

`wb service restart` 在有命令正在跑时会拒绝执行（`--force` 才打断）——重启本身就是
「结果全丢」最常见的原因。

### 自进化

每次 exec / 能力调用都记进 `~/.config/web-bridge/exec-log.jsonl`（可 grep）。同一段脚本
（按归一化签名，忽略注释和排版差异）在同一站点成功跑满 3 次，就自动写进
`capabilities/auto/` 成为正式能力，所有 agent 立刻可发现。`web-bridge log` 查历史，
`GET /capabilities?url=` 也会附带「这个站以前跑过什么」。

### 能力库

能力 = `capabilities/<id>.js`（文件头一段 JSON 元数据 + 函数体）。写一个文件就多一个
能力，不用重载扩展，所有 agent 和扩展弹窗立刻能发现。参数在 bridge 侧按元数据校验
（必填/类型/枚举/范围），传错会直接回一份可照着改的参数表。

内置（12 个）——通用：`inspect-page`（先探路）、`extract-article`、`extract-tables`、
`collect-list`、`site-search`（任意站点站内搜索）、`reader-mode`；站点专属：`google-search`、
`youtube-transcript`、`x-posts`、`x-post`（发帖/回复）、`chatgpt-conversations`、`perplexity-ask`。

MCP：把 `python3 ~/cc/web-bridge/bridge/mcp_server.py` 注册为 stdio MCP server，
即得 `web_exec` / `web_tabs` / `web_open` / `web_chatgpt_ask` 等工具。

## 加新站点

在 `~/.config/web-bridge/config.json` 的 `sites` 加一项 `{match, home, adapter?}`；
需要高级动作时在 `extension/adapters/<name>.js` 写一个适配器（`window.__webBridge
.registerAdapter(name, methods)`），然后 `web-bridge reload`。

## 安全

- bridge 仅监听 127.0.0.1；每个 HTTP 路由要 `Authorization: Bearer <token>`；
  扩展 WS 也要 `?token=`。token 在 `~/.config/web-bridge/config.json`（chmod 600）。
- 扩展 `<all_urls>` + MAIN world eval 能力较大，靠 bridge 的 token 限制只有本机授权
  调用方能驱动。
