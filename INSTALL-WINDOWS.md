# 在 Windows 上安装 web-bridge

> 这份文档是写给**在 Windows 机器上干活的 agent**（Claude Code / Codex / 其它）的。
> 用户会把整份内容贴给你，你按顺序执行，遇到不符合预期的地方**停下来报告，不要猜着往下走**。
>
> 项目是在 macOS 上开发的。跨平台的部分（bridge、扩展、能力库、侧栏）是同一套代码；
> 与系统相关的只有两处：**开机自启的方式**和**查端口占用的命令**，代码里已经按平台分支处理。
> 下面标注 ⚠️ 的地方是已知与 macOS 不同、**没有在真 Windows 上验证过**的，请实际跑一遍并回报结果。

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

它会在 PATH 里找 `claude` / `codex` / `dsh` 并写进配置。⚠️ 探测逻辑用 `shutil.which`，
在 Windows 上应该能找到 `.cmd` / `.exe` 包装器，但**没实测过**；如果没找到，手动编辑
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
$env:WEB_BRIDGE_PORT=8795; $env:WEB_BRIDGE_STATE="$env:TEMP\wb-test"
py -3 bridge\server.py    # 另开一个窗口
py -3 bridge\test_mock_ext.py
```

## 已知的 Windows 差异（都还没在真机上验证）

| 位置 | macOS | Windows | 风险 |
|---|---|---|---|
| 开机自启 | launchd plist | Startup 目录 `.cmd` | 中：路径含空格已加引号，但未实测 |
| 查端口占用 | `lsof` | `netstat -ano` | 中：解析的是中文/英文 netstat 输出，列位置假定为标准格式 |
| 崩溃自愈 | launchd KeepAlive 自动重启 | **没有**——进程挂了要手动起或重新登录 | 高：这是功能缺失，不是 bug |
| 文件权限 | `chmod 600` 保护 token | chmod 在 Windows 上基本无效 | 中：token 文件靠 NTFS 用户目录权限保护 |
| 日志 | `~/Library/Logs/web-bridge.log` | 服务由 .cmd 启动，**日志没有重定向** | 中：排查问题时改成前台跑 `py -3 bridge\server.py` 看输出 |

**请把实际结果回报给用户**：哪几步一次过、哪几步报错、报的什么错。
这几处分支是照着文档写的，没有真 Windows 环境验证过。
