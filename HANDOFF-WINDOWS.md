# Windows 侧流水（给 macOS 侧 review 用）

> 2026-08-26 · Windows 11 家庭中文版 26200 · Python 3.14.0 · Chrome MV3 · Claude Code
> 对应提交 `2cb6f34`（9 文件 +239/-50），基线 `0bd71b8`
>
> 这份是**过程记录**：干了什么、怎么定位的、哪些结论被自己推翻过。
> 只想看结论就读 `INSTALL-WINDOWS.md` 的「已在真机验证」小节，那边是收敛后的版本。

---

## 0. 一句话

按 `INSTALL-WINDOWS.md` 装了一遍，装不上。修完 5 个 Windows 阻塞问题 + 2 个跟平台无关的
bug，测试从 **0/66 → 66/66**。macOS 分支一行没动。

---

## 1. 时间线

### 1.1 装依赖、生成 token —— 一次过

`pip install -r bridge/requirements.txt`、`gen_ext_config.py` 都正常。
`gen_ext_config.py` 是纯 Python 这个决定在 Windows 上是对的，没有任何摩擦。

### 1.2 `service install` —— 撞墙，且是全局性的

```
AttributeError: module 'os' has no attribute 'getuid'
  File "bridge/service.py", line 42, in <module>
    DOMAIN = f"gui/{os.getuid()}"
```

**这不是 service 子命令的问题，是 `cli.py` 顶部 `import service` 的问题**，所以
`wb status` / `wb tabs` / `wb exec` 全都跑不了，测试套件也 import 不进去。
文档里预判的 Windows 差异是「开机自启方式」和「查端口命令」，但真正的第一道墙在模块加载。

→ `service.py:43` 加 `if hasattr(os, "getuid") else ""`。`DOMAIN`/`SERVICE` 只被 launchctl
用到，Windows 上够不着这两个常量。

### 1.3 `netstat` 解码 —— 第二道墙

```
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbb in position 2
TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'
```

`_run()` 用 `text=True` + 默认编码解 `netstat` 输出。中文 Windows 的 netstat 是 cp936，
环境里只要有 `PYTHONUTF8=1` 就炸；`subprocess` 捕获失败后 `p.stdout` 是 `None`，
下一行 `p.stdout + p.stderr` 再炸一次，真正的错因被第二个异常盖住。

→ `service.py:47-53` 显式 `locale.getpreferredencoding(False)` + `errors="replace"`
+ `None` 兜底。对 macOS 只是变宽容。

**顺带回报你们标了 ⚠️ 的那条**：中文版 Windows 的 `netstat -ano` 状态列**仍然是英文
`LISTENING`**，列位置和英文版一致，`_windows_port_pids()` 的解析逻辑不用改。实测输出：

```
  TCP    127.0.0.1:8790    0.0.0.0:0    LISTENING    26672
```

### 1.4 开机自启起不来 —— 最花时间的一个

`service install` 写出了 `.cmd`，但立刻报「服务没起来」。前台 `py -3 bridge/server.py`
完全正常，`.cmd` 就是不行。查了四轮：

| 假设 | 验证方式 | 结果 |
|---|---|---|
| pythonw 下 `print()` 抛异常 | 起一个只 print 的脚本 | ❌ 不抛，`print` 到 `None` 是 no-op |
| pythonw 路径不存在 | `ls pythonw.exe` | ❌ 存在 |
| 端口没让出来 | 带重定向跑 pythonw | 起来了，但日志说「端口已被另一个 web-bridge 占用」——是我自己的前台实例，**这一轮的结论作废** |
| `start` 启动的进程拿不到有效句柄 | `sys.stdout=None` 探针 | ✅ **就是它** |

关键的两个实测事实：

1. `start "" /min pythonw.exe x.py` 启动的进程 **`sys.stdout` 和 `sys.stderr` 都是 `None`**
2. **`start` 不会把自己的重定向传给子进程** —— `start "" pythonw ... > log 2>&1` 里，
   子进程的 `sys.stdout` 依然是 `None`，log 文件是空的

所以 server 在 `main()` 里第一次 print 时就没了，而且**什么都不留**，
`service install` 只能说「服务没起来」，用户拿不到任何线索。

**第一版修法（已废弃）**：套一层 `cmd /c` 拿到真句柄。能用，但那个 `cmd.exe` 会
**跟着服务常驻在任务栏**——用户后来正是因为看见这个窗口才追问的。

**最终修法**：让 `server.py` 自己接管。`server.py:47,53` 发现流是 `None` 就重定向到
`service.win_log()`。这样启动器保持一行 `start`（`service.py:256`），
**既有日志又零控制台窗口**。日志路径顺手收敛成一处定义：`_win_log()` → 公开的
`win_log()`（`service.py:223`），`server.py` 复用，不再各写一份。

验证：杀掉进程 → 只跑 Startup 里的 `.cmd` → 服务起来、扩展连上、日志有内容、
带窗口的 cmd 进程数 **1 → 0**。

### 1.5 每条侧栏消息弹一个终端窗口

用户报的现象。`agents.py` 用 `create_subprocess_exec` 起 `claude.EXE`，没带
`CREATE_NO_WINDOW`。agent CLI 都是控制台程序，父进程 pythonw 又没有控制台，
于是 Windows 给**每个**子进程新分配一个控制台窗口。

→ `agents.py:416-426`。

验证时先怀疑过是这个 flag 弄坏了中文 prompt（agent 收到的是 `??????:????????????`），
做了对照实验：pythonw 父进程 + 带/不带 flag 各起一次 `claude.EXE` 传中文 argv，
**两边都完好**。是我的 PowerShell 测试脚本传中文字面量时降级成了 `?`，
换 curl 从 Bash 发同样的请求，中文端到端正常。**这条弯路记在这里，免得你们重走。**

### 1.6 `config.json` 读写编码不一致 —— 静默损坏

审自己的 diff 时发现的，不是被现象逼出来的：

- `gen_ext_config.py:31,44` **显式 utf-8** 读写 `config.json`
- `config.py:26,80,85` 和 `agents.py:186,191` 用**系统默认编码**读写同一个文件（行号为修复后）

同一个文件两套编码。macOS 上两者都是 utf-8 所以永远不会暴露。

写了个复现确认后果是**静默损坏而不是抛异常**：

```
原值      : 日本語ページ
默认编码读 : （乱码）   一致? False
utf-8 读  : 日本語ページ  一致? True
```

`config.load()` 的 `except Exception: data = {}` 意味着**如果碰巧解出非法 JSON，
token 会直接消失**，服务照常启动但扩展永远连不上。现实触发条件不难：
站点 `home` 带中文、或者 agent 的 `cwd` 落在 `C:\Users\<中文用户名>\` 下。

→ 5 处补 `encoding="utf-8"`，`config.py:21-25` 写了注释说明为什么必须显式。

### 1.7 两个跟平台无关的 bug（macOS 上同样存在）

**`register_mcp.py` 的 TOML 正则**

```python
r'\[mcp_servers\."?web-bridge"?\][^\[]*'
```

`[^\[]*` 会在 `args = [` 的那个 `[` 处提前停下，把 `["...mcp_server.py"]` 这行留在
`~/.codex/config.toml` 里。而它是个**合法的 TOML 表头**——等于污染配置，
**每重新注册一次多留一行**。本机已经长出来的那行清掉了（清理前备份在 `%TEMP%`）。

→ `register_mcp.py:70` 改成匹配到下一个行首表头。
同文件所有 `read_text`/`write_text` 也补了 utf-8（读 `~/.claude.json` 时报的
`'gbk' codec can't decode` 就是这里）。

**`capabilities/x-post.js` 会重复输入文案**

原注释把重复归因于「多余的合成 input 事件和 execCommand 抢跑」。**实测推翻**：

| 场景 | 结果 |
|---|---|
| 手搓 `selectAll` + `insertText`，不发任何合成事件 | 重复 |
| 改用 `Range.selectNodeContents` | 正常 |
| 清空输入框 → 跑一次 | 正常 |
| 再清空 → 跑一次（**完全相同的调用**） | **重复** |
| 不清空 → 跑一次 | 正常 |

同样的调用、同样的起始状态，一次对一次错——**是竞态**（Draft.js 内部状态异步更新），
不是选区方法、也不是事件抢跑。中途我一度以为是 `selectAll`，也写进注释了，
被第 4 组数据推翻。

既然是竞态就没有安全的时序写法，而这个能力是**对外发布**的：输入框错了就是公开发错。
→ `x-post.js:53` 改成写完读回来自检，不一致就清空重来（最多 3 次），
3 次仍不符就抛错**并且不发送**。压测 6 次（交替空/非空起始状态）全部一次命中。
原有的 `dry_run` 参数保留，全程没有真的发帖。

---

## 2. 测试：0/66 → 66/66

按 `INSTALL-WINDOWS.md` 的方式跑（独立端口 + 临时 state）。过程中的两个发现：

**干净的 `origin/main` 在这台机器上是 0/66** —— 不是红几项，是连 import 都过不去
（就是 1.2 那个 `getuid`）。所以「macOS 66 项全过」和 Windows 之间原本没有可比基线。

**那 5 项失败不是平台 bug，是套件不自洽。** 做法是先 stash 掉自己全部改动、
只打 `getuid` 一个最小补丁跑基线：

| 代码状态 | 结果 |
|---|---|
| 干净 `origin/main` | 0/66（import 失败） |
| `origin/main` + 仅 getuid 修复 | 61/66 |
| `origin/main` + 我的全部改动 | 61/66，**失败项完全相同** |
| 同上 + `WEB_BRIDGE_CONFIG` 指定站点 | **66/66** |

先确认零回归，再查失败原因：`exec` 返回 `404 未知站点 'chatgpt'`。

`WEB_BRIDGE_STATE` **不隔离 `sites`** —— 站点表来自 `config.json`。
`exec.roundtrip` / `adapter.roundtrip` / 三个 `hub.*` 要求 `chatgpt` 和 `github` 已注册，
开发机上它们本来就在，所以从没暴露。**任何平台的新机器都会红这 5 项**，
而且报错长得像端口或连接问题，很容易误判成平台适配没做好。

绕法（`WEB_BRIDGE_CONFIG` 指一份临时配置）已写进 `INSTALL-WINDOWS.md` 和 `HANDOFF.md`。
**我没有改测试代码** —— 要不要让套件自己准备这两个站点，是你们的设计决定。

---

## 3. 遵守约定的情况

对照 `INSTALL-WINDOWS.md`「改动约定」逐条：

| # | 约定 | 情况 |
|---|---|---|
| 1 | 不删改 macOS 分支 | ✅ launchd/plist/lsof 那侧一行没动，新增全在 `windows_*` 和 `IS_WINDOWS` 分支里 |
| 2 | 平台判断统一 `service.IS_WINDOWS` | ⚠️ **一开始违反了**，在 `agents.py`/`server.py` 各写了 `sys.platform.startswith("win")`；读到这条后改正。现在全仓库 `sys.platform` **只出现在 `service.py:36-37`** |
| 3 | 路径用 `pathlib` | ✅ |
| 4 | 不加新依赖 | ✅ 只用到 stdlib（`locale`、`subprocess`、`io`） |
| 5 | 不动 `extension/` | ✅ 一个字节没改。改的 `capabilities/x-post.js` 是能力库，且那是**跟 OS 无关的真 bug**，见 1.7 |
| 6 | 两处文档各记一笔 | ✅ `INSTALL-WINDOWS.md` 差异表 + 验证小节，`HANDOFF.md` 常驻服务 + 测试两节 |

### 一处绕不过去的例外，请你们定夺

**`agents.py:423` 必须在函数内局部 `import service`。**

`service.py:34` 自己就 `import agents`，而 `IS_WINDOWS` 定义在 `service.py:36`——
也就是在那行 import **之后**。所以：

- 模块级 `import service` → 循环导入
- `from service import IS_WINDOWS` → 直接 `ImportError`（此时属性还不存在）

局部 import 能工作（调用时 service 早已加载完），但确实别扭。
更彻底的解法是把 `IS_WINDOWS`/`IS_MAC` 挪到 `config.py`（最底层模块），
再由 `service.py` 转出一行 `IS_WINDOWS = config.IS_WINDOWS`——
这样 `service.IS_WINDOWS` 这个公开名字和你们的约定都不破，`cli.py`/`mcp_server.py`
也不用改。但那属于动共享结构，**我没自作主张**。

`server.py:47` 的 `import service` 是模块级的，没有环（没有任何模块 import `server`）。
不过这给 `server` 新增了一条对 `service` 的依赖，麻烦确认一下你们能接受。

---

## 4. review 建议：先看这 5 处

只有这些是两边都会走到的共享代码：

| 文件 | macOS 上验什么 | 预期 |
|---|---|---|
| `service.py:47-53` `_run()` | 喂的是 `lsof` / `launchctl` | 行为不变，只是变宽容 |
| `config.py`、`agents.py` | 补的 `encoding="utf-8"` | Mac 默认就是 utf-8，应无变化 |
| `register_mcp.py:70` | **Mac 上同样有这个 bug** | 重新注册一次，看 `~/.codex/config.toml` 不再长垃圾；已长出来的旧垃圾要手动删一次 |
| `capabilities/x-post.js` | 浏览器代码，与 OS 无关 | `dry_run` 连跑几次，文案一次命中不重复 |
| `server.py:47-63` | 新增的 17 行整个包在 `if service.IS_WINDOWS` 里 | Mac 上不执行；但 `import service` 这条依赖两边都有，确认没引入循环导入 |

---

## 5. 没验证到的

- **侧栏「页面」「对话」两个标签的 UI 操作** —— 需要人手点，我做不了。
  验收清单第 4、5 项因此是空的。
- **崩溃自愈** —— Windows 侧本来就没有（launchd KeepAlive 无等价物），不是回归。
- **`agents --detect` 的 `--no-full-access` 挡位在侧栏不可用** —— 不是 bug，
  是 headless `-p` 模式下没有审批 UI，agent 只能干说「需要授权」然后卡死。
  你们代码里的注释已经写明了这点。本机最终按用户明确要求切到了全权模式。

---

## 6. 一个已定位但**没修**的 bug，留给你们决定

**`extension/background/service_worker.js:118` `waitComplete()` 只认
`status === "complete"`，30 秒后 reject。**

页面卡在 `readyState: "interactive"` 是极常见的情况——某个广告/统计子资源一直挂着，
DOM 早已完整可用。国内网络访问境外站尤其容易出现。

实测：某个 macrotrends 标签页上 `run extract-tables` → `❌ 页面加载超时`，
但同一个标签页 `exec` 完全正常：

```json
{ "ready": "interactive", "tables": 2 }
```

后果：这类页面上**每一个 capability 都必然失败，而 `exec` 却好好的**。
用户看到的现象是「能力库时灵时不灵」，很难联想到是加载状态判定。

没动手的原因有两个：这是**行为语义**的改动（建议超时后降级为
`readyState !== 'loading'` 就放行，而不是直接 reject），且落在
`extension/` 下——按约定 #5 我应该先说明原因再改。要修的话我可以另开一个提交。
