# 在 Windows 上安装 web-bridge

> 这份文档是写给**在 Windows 机器上干活的 agent**（Claude Code / Codex / 其它）的。
> 用户会把整份内容贴给你，你按顺序执行，遇到不符合预期的地方**停下来报告，不要猜着往下走**。
>
> 项目是在 macOS 上开发的。跨平台的部分（bridge、扩展、能力库、侧栏）是同一套代码；
> 与系统相关的只有两处：**开机自启的方式**和**查端口占用的命令**，代码里已经按平台分支处理。
> 下面标注 ⚠️ 的地方是已知与 macOS 不同的。
>
> **2026-08-26 已在 Windows 11 家庭中文版 26200 / Python 3.14.0 / Chrome MV3 上完整跑过一遍**，
> 结论见文末「已在真机验证」。当时修掉了 5 个会让 Windows 完全跑不起来的问题，
> 剩余差异见「已知的 Windows 差异」表。

## 0. 前置条件

| 需要 | 检查命令 | 说明 |
|---|---|---|
| Python 3.9+ | `py -3 --version` | 装的时候勾选 “Add python.exe to PATH” |
| Chrome 138+ | `chrome://version` | 侧边栏和 userScripts API 需要 |
| Git | `git --version` | 用来克隆仓库 |

本机至少要有一个 agent CLI（`claude` / `codex` / `dsh`），否则侧栏的「对话」标签没有可用后端；
其余功能（脚本库、页面脚本、书签导出）不受影响。

## 1. 克隆并装依赖

```powershell
git clone https://github.com/ghbhiee/web-bridge.git
cd web-bridge
py -3 -m pip install -r bridge/requirements.txt
```

## 2. 生成 token 和扩展配置

```powershell
py -3 bridge\gen_ext_config.py
```

它会：
- 在 `%USERPROFILE%\.config\web-bridge\config.json` 建配置（没有 token 就随机生成一个）
- 写出 `extension\config.js`（扩展用它连 bridge；**这个文件不进 git，每台机器各自生成**）

看到 `写入 ...extension\config.js` 就算成功。

## 3. 启动 bridge

```powershell
py -3 bridge\cli.py service install
```

⚠️ **Windows 的自启方式和 macOS 不同**：macOS 用 launchd，Windows 是往
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` 放一个 `web-bridge.cmd`
（不需要管理员权限，用资源管理器就能删）。装完会立刻起一个后台进程。

验证：

```powershell
py -3 bridge\cli.py service status
py -3 bridge\cli.py status
```

`service status` 应显示「开机启动: 已装」+「服务: ✅ 运行中」；
`status` 这时会显示「扩展连接: ❌」——正常，扩展还没装。

不想要自启就跳过 install，需要时手动跑：`py -3 bridge\server.py`。

## 4. 装 Chrome 扩展（必须手动，一次）

1. 打开 `chrome://extensions/`
2. 右上角打开「开发者模式」
3. 点「加载已解压的扩展程序」，选择仓库里的 **`extension`** 目录
4. 进入这个扩展的「详情」，打开 **「允许用户脚本 / Allow User Scripts」**
   —— 不开这个开关，`chrome.userScripts` 是 undefined，页面注入和自动运行都用不了

再跑一次 `py -3 bridge\cli.py status`，应该变成「扩展连接: ✅」。

点浏览器工具栏的扩展图标 → 右侧出现侧栏，三个标签：**对话 / 页面 / 脚本库**。

## 5. 让本机 agent 可用（可选，但「对话」标签靠它）

```powershell
py -3 bridge\cli.py agents --detect
```

它会在 PATH 里找 `claude` / `codex` / `dsh` 并写进配置。探测逻辑用 `shutil.which`，
**已实测**能找到 `.EXE` / `.CMD` 包装器（`claude.EXE`、`codex.CMD` 都正确识别）；如果没找到，手动编辑
`%USERPROFILE%\.config\web-bridge\config.json` 的 `agents.runners`，把 `path` 写成绝对路径。

⚠️ **安全须知（务必转达用户）**：默认给 agent 加的是跳过确认的参数
（`--dangerously-skip-permissions` 等），意味着**在侧栏对话里说一句话，agent 就能在这台机器上全权执行命令**。
不接受就关掉它：

```powershell
py -3 bridge\cli.py agents --detect --no-full-access
```

（`--no-full-access` 在 `agents` 子命令上，不在 `service install` 上。）

## 6. 把 MCP 注册进本机 agent（可选）

```powershell
py -3 bridge\register_mcp.py
```

让 claude/codex 等在**任何地方**（不只是侧栏）都能用 web-bridge 的工具驱动浏览器。

## 7. 验收

按顺序做完这几步，能全过就算装好了：

1. `py -3 bridge\cli.py status` → 扩展连接 ✅
2. `py -3 bridge\cli.py tabs` → 列出当前打开的标签页
3. 打开任意网页，`py -3 bridge\cli.py exec "return document.title" --url <网址片段>` → 返回标题
4. 侧栏「页面」标签 → ＋新建脚本 → 贴 `document.body.style.background='#eee'; return {ok:1};`
   → 保存 → 运行 → 页面变灰
5. 侧栏「对话」标签 → 选一个 agent → 说「把这个页面标题变红」→ 页面应立刻变化，
   回答里带代码块，下面有「保存到我的脚本库」按钮

跑测试（可选）：

```powershell
py -3 bridge\test_mock_ext.py
```

⚠️ macOS 上是 `bridge/run_tests.sh` 起一个独立端口的一次性实例来跑；Windows 上没有对应的 .cmd。
**直接跑 test_mock_ext.py 会连上 8790 抢真扩展的槽位，两边互踢**。要跑就先设环境变量：

```powershell
$env:WEB_BRIDGE_PORT=8795
$env:WEB_BRIDGE_STATE="$env:TEMP\wb-test"
$env:WEB_BRIDGE_CONFIG="$env:TEMP\wb-test-cfg.json"   # ← 见下
py -3 bridge\server.py    # 另开一个窗口
py -3 bridge\test_mock_ext.py
```

⚠️ **`WEB_BRIDGE_STATE` 不隔离 `sites`**：站点表来自 `config.json`，而套件里
`exec.roundtrip` / `adapter.roundtrip` / 三个 `hub.*` 用例都要求 `chatgpt` 和 `github`
两个站点已注册。macOS 开发机上它们本来就在，所以从没暴露；**全新装的机器（任何平台）
会直接红 5 项**，看起来像平台 bug，其实是套件不自洽。用 `WEB_BRIDGE_CONFIG` 指一份
临时配置即可：

```json
{
  "host": "127.0.0.1", "port": 8795, "token": "test-token-for-suite",
  "sites": {
    "chatgpt": {"match": ["chatgpt.com"], "home": "https://chatgpt.com/", "adapter": "chatgpt"},
    "github":  {"match": ["github.com"],  "home": "https://github.com/"}
  }
}
```

## 如果你要改代码（给动手做兼容的 agent）

**先读这一节再动手**，跨平台分支已经写好了一部分，重写会和 macOS 侧冲突。

已经存在的平台分支，**请在原处扩展，不要另起炉灶**：

| 文件 | 已有的分支 | 说明 |
|---|---|---|
| `bridge/service.py` | `IS_WINDOWS` / `IS_MAC` 常量 | `cmd_install` / `cmd_uninstall` / `cmd_restart` 开头就分流到 `windows_*()`；`installed()`、`cmd_status()`、`port_owner_pids()` 也已分平台 |
| `bridge/service.py` | `_windows_port_pids()` | 用 `netstat -ano` 解析监听端口的 pid（替代 lsof）。**中文版 Windows 的 netstat 输出列位置是否一致，需要你实测** |
| `bridge/gen_ext_config.py` | 纯 Python | 替代 `gen_ext_config.sh`，两个平台通用，不要再写 .ps1 |
| `bridge/cli.py` / `bridge/mcp_server.py` | `service.installed() and not service.IS_WINDOWS` | 避免在 Windows 上调 `launchctl` |

**改动约定**（为了 pull 回 macOS 时能直接合）：
1. **不要删或改写 macOS 分支**，只加 Windows 分支；共用逻辑保持共用
2. 平台判断统一用 `service.IS_WINDOWS`，不要各文件自己 `sys.platform.startswith("win")`
3. 路径一律 `pathlib.Path`，不要拼 `/` 或 `\`
4. 新增依赖前先问——目前 bridge 只依赖 `fastapi` / `uvicorn` / `websockets`，保持这样
5. `extension/` 下的代码是浏览器里跑的，**与操作系统无关**，正常情况下不需要改。
   如果你发现必须改，那多半是别的问题，先说明原因
6. 改完在**两个地方**记一笔：`INSTALL-WINDOWS.md` 的差异表、`HANDOFF.md` 的对应小节

**测试**：`py -3 bridge\test_mock_ext.py`，但必须先设 `WEB_BRIDGE_PORT` / `WEB_BRIDGE_STATE`
指向一次性实例（见上文），否则会抢真扩展的连接。macOS 侧目前 66 项全过，
**你的改动不应该让任何一项变红**；如果某项在 Windows 上天然不适用，
不要删掉它，用条件跳过并说明原因。

**提交**：正常 commit + push 到 `main` 即可。写清楚改了什么、在哪个 Windows 版本上验证过、
哪些还没验证。macOS 侧会 review 后再 pull。

## 已知的 Windows 差异

| 位置 | macOS | Windows | 状态 |
|---|---|---|---|
| 开机自启 | launchd plist | Startup 目录 `.cmd`（`start "" /min pythonw server.py`） | ✅ 已实测：杀掉进程后单跑该 .cmd 能起来，**不留任何控制台窗口** |
| 查端口占用 | `lsof` | `netstat -ano` | ✅ 已实测：中文版 Windows 的 netstat 状态列仍是英文 `LISTENING`，列位置与英文版一致，解析正确 |
| 崩溃自愈 | launchd KeepAlive 自动重启 | **没有**——进程挂了要手动起或重新登录 | ⚠️ **仍然缺失**，这是功能缺失不是 bug |
| 文件权限 | `chmod 600` 保护 token | chmod 在 Windows 上基本无效 | ⚠️ 未改动，token 文件靠 NTFS 用户目录权限保护 |
| 日志 | `~/Library/Logs/web-bridge.log` | `%LOCALAPPDATA%\web-bridge\server.log` | ✅ 已解决，见下 |
| 控制台窗口 | 不存在这个问题 | pythonw 下 `sys.stdout` 是 `None`；子进程会被分配新控制台 | ✅ 已解决，见下 |

## 已在真机验证（2026-08-26, Windows 11 家庭中文版 26200 / Python 3.14.0）

`py -3 bridge	est_mock_ext.py` → **66/66 全过**（配好上面那份临时 config 之后）。
干净的 `origin/main` 在同一台机器上是 **0/66**——连 import 都过不去，见下面第 1 条。

修掉的问题，按严重程度：

1. **`service.py` 模块级 `os.getuid()`** —— Windows 上没有这个函数，`import service` 直接崩，
   于是 `cli.py` 的**每一个**子命令、以及整个测试套件都跑不了。改成
   `if hasattr(os, "getuid") else ""`；`DOMAIN`/`SERVICE` 只被 launchctl 用，Windows 上够不着。
2. **开机自启起不来** —— `start "" /min pythonw server.py` 启动的进程**没有控制台**，
   `sys.stdout`/`sys.stderr` 都是 `None`，server 在第一次 `print()` 时死掉，而且不留任何痕迹，
   `service install` 只会说「服务没起来」。另外实测确认：**`start` 不会把自己的重定向传给子进程**，
   所以 `start "" pythonw ... > log` 也救不了。解法是让 `server.py` 自己接管——发现流是 `None`
   就重定向到 `service.win_log()`。这样启动器保持一行 `start`，**既有日志又零控制台窗口**
   （用 cmd /c 包一层也能work，但那个 cmd 会跟着服务常驻在任务栏）。
3. **agent 子进程弹终端窗口** —— `agents.py` 起 `claude.EXE` 时没带 `CREATE_NO_WINDOW`。
   agent CLI 都是控制台程序，父进程 pythonw 又没有控制台，于是 Windows 给每个子进程新分配一个
   控制台窗口：**在侧栏每说一句话就弹一次**。已加（仅 Windows 分支）。
4. **`config.json` 读写编码不一致** —— `gen_ext_config.py` 显式用 utf-8 写，
   而 `config.py` / `agents.py` 用系统默认编码读（中文 Windows 上是 cp936）。
   同一个文件两套编码，macOS 上两者都是 utf-8 所以从没暴露。后果是**静默损坏**：
   非 ASCII 值（站点 `home`、agent 的 `cwd` 在中文用户目录下）读出来是乱码，不报错。
   已给 5 处 `read_text()`/`write_text()` 补上 `encoding="utf-8"`。
5. **`service._run()` 解码** —— `text=True` 用默认编码解 `netstat` 输出；环境里只要有
   `PYTHONUTF8=1` 就 `UnicodeDecodeError`，`p.stdout` 变 `None`，接着 `None + str` 崩。
   已改成显式 locale 编码 + `errors="replace"` + `None` 兜底（对 macOS 只是变宽容，行为不变）。

另外顺手修的两个**与平台无关**的 bug（macOS 上同样存在）：

- `register_mcp.py` 删除旧 TOML 块的正则 `[^\[]*` 会在 `args = [` 处提前停下，
  往 `~/.codex/config.toml` 里残留一行 `["...mcp_server.py"]`——而它是个合法的 TOML 表头，
  等于污染配置，**每重新注册一次多留一行**。改成匹配到下一个行首表头。
- `capabilities/x-post.js` 发帖前的文案写入会**偶发重复**（"文案文案"）。原注释归因于
  「多余的合成 input 事件」，实测推翻：不发任何合成事件也能复现，且同样的调用一次对一次错——
  是 Draft.js 内部状态异步更新的竞态。既然这个能力是**对外发布**的，改成写完读回来自检、
  不一致就清空重来（最多 3 次），3 次仍不符就抛错**并且不发送**。压测 6 次全部一次命中。

**没验证到的**：侧栏「页面」「对话」两个标签的 UI 操作（需要人手点）、崩溃自愈（Windows 侧本来就没有）。

**回报给 macOS 侧的注意事项**：所有新增的平台分支都用 `service.IS_WINDOWS`，
没有新写 `sys.platform`（全仓库现在只有 `service.py:36-37` 那一处定义）。
唯一的例外是 `agents.py`——它必须用**函数内局部 import**拿到 `service`，
因为 `service.py` 自己就 import 了 `agents`，模块级反向 import 会成环。
