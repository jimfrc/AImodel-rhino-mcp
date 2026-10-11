# GOLEM Rhino Web Agent

用自然语言驱动 **Rhino 8** 建模的 Web 对话端。

后端把 [GOLEM-3DMCP](https://github.com/TheKingHippopotamus/GOLEM-3DMCP-Rhino-) 当 MCP server 拉起来，
**动态**读取它暴露的 89 个工具 schema 交给大模型，模型的 `tool_call` 再转成 MCP 调用；
模型能看到视口截图，形成「读 → 建 → 看 → 改」的闭环。

## 架构

```
浏览器 ──WebSocket──> webapp (FastAPI, 127.0.0.1:8849)
                          │
                          │ MCP stdio（自动拉起子进程）
                          v
              python -m golem_3dmcp start     ← 89 个工具 schema 从这里动态获取
                          │
                          │ TCP 127.0.0.1:9876（4 字节长度前缀 + JSON）
                          v
               Rhino 8 插件（start_in_rhino.py）
```

## 快速开始

### 1. 装环境

```bat
setup.cmd
```

它会建 `.venv`、按 `requirements.txt` 装依赖、并从 `.env.example` 生成 `.env`。

### 2. 填 API Key

打开 `.env`，填入 `DEEPSEEK_API_KEY`。默认模型是 `deepseek-v4-pro`
（89 个工具对 function calling 能力要求较高，`deepseek-flash` 容易漏调用工具）。

### 3. 在 Rhino 里启动插件

Rhino 8 → 命令行输入 `ScriptEditor` → 打开并运行：

```
F:\...\golem-mcp\start_in_rhino.py
```

成功会打印：

```
GOLEM-3DMCP: listening on 127.0.0.1:9876, 135 handlers registered
```

也可以用「工具 → 选项 → RhinoScript → 启动脚本」让它随 Rhino 自启。

### 4. 启动网页版

```bat
start_web.cmd
```

浏览器打开 **http://127.0.0.1:8849**。顶部状态灯变绿即表示 Rhino 桥已连通。

## 页面能力

- 顶部状态灯：Rhino 连接状态、工具数量、文档单位与对象数（4 秒轮询，带 15 秒缓存）
- 对话流 + 工具调用轨迹（长返回可折叠）
- 视口截图直接内嵌显示，点击可放大
- 「撤销一步」（GOLEM 没有 undo 工具，走 Rhino 的 `_Undo` 命令）
- 「新会话」清空上下文

## 配置项

全部写在 `.env`（优先级：真实环境变量 > 本目录 `.env`）。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | — | 必填（也接受 `OPENAI_API_KEY`） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | 任何 OpenAI 兼容端点 |
| `RHINO_WEB_MODEL` | `deepseek-v4-pro` | 模型名 |
| `RHINO_WEB_MAX_TURNS` | `25` | 单轮最多工具回合 |
| `RHINO_WEB_MAX_TOKENS` | `8192` | 单次回复上限 |
| `RHINO_WEB_VISION` | `1` | `0` = 不把截图喂给模型（省钱，闭环降级为单向生成） |
| `RHINO_WEB_IMAGE_MAX_SIDE` | `1024` | 上传前把截图最长边缩到多少像素 |
| `RHINO_WEB_RPC_TIMEOUT` | `180` | 单次工具调用超时（秒） |
| `RHINO_WEB_DENY_TOOLS` | 空 | 工具黑名单，逗号分隔，例 `execute_python,run_rhino_command` |
| `RHINO_WEB_PORT` | `8849` | 网页端口 |
| `GOLEM_RHINO_PORT` | `9876` | Rhino 插件端口（两侧必须一致） |

## 注意：单位不是米

GOLEM 全程使用 **Rhino 当前文档单位**，不做米/毫米换算。
系统提示词已强制要求模型先调 `get_document_info` 确认单位，但你自己下指令时也要留意：
文档是 `Millimeters` 时「半径 100」就是 100mm。

## 目录结构

```
golem-mcp/
├── start_in_rhino.py        Rhino 侧启动：停旧实例 → 重载模块 → 监听 9876
├── start_web.cmd            Web 侧启动
├── setup.cmd                一键装环境
├── requirements.txt         依赖
├── golem_3dmcp/             vendored 的 GOLEM 服务端（含修复，见下节）
├── rhino_plugin/            vendored 的 Rhino 端插件（含修复，见下节）
├── webapp/
│   ├── config.py            .env 加载与全部配置项
│   ├── golem_client.py      MCP 客户端：拉子进程、调用工具、截图压缩、链路自愈
│   ├── agent.py             DeepSeek function calling 闭环 + 视觉回传
│   ├── app.py               FastAPI：/、/api/status、/api/undo、/ws
│   └── static/index.html    单页对话 UI
├── THIRD_PARTY_LICENSES.txt 第三方版权声明与改动说明
└── LICENSE                  本项目 MIT
```

## 第三方代码与两处修复

`golem_3dmcp/` 与 `rhino_plugin/` 来自 [GOLEM-3DMCP](https://github.com/TheKingHippopotamus/GOLEM-3DMCP-Rhino-)（MIT），
**已直接 vendor 进本仓库**，并修掉了两个会导致不可用的缺陷：

| 文件 | 问题 | 修复 |
| --- | --- | --- |
| `golem_3dmcp/connection.py` | 只把 `BrokenPipe / ConnectionReset` 当可重试故障，`ConnectionAbortedError(10053)`、`ConnectionRefusedError(10061)` 以及读端 EOF 都不在其列，坏 socket 被反复复用，**后续每次调用永久失败** | 重连判定放宽为内建 `ConnectionError`（上述全部都是它的子类） |
| `rhino_plugin/server.py` | `_accept_loop` 同步执行 `handle_client`，一个长连接（如本项目的常驻 MCP 子进程）就把 accept 循环卡死，`listen(5)` 的 backlog 塞满后 **9876 拒绝一切新连接** | 每个客户端一个线程；`listen` backlog 5 → 16 |

另外为兼容上游 pip 安装包与目录名的差异，插件目录被重命名为 `rhino_plugin`（上游为 `_rhino_plugin`），
因为插件内部使用 `from rhino_plugin.xxx import ...` 绝对导入。
上游自带的 `install-rhino` 在 Windows 上不可用（复制出的目录名与硬编码的 macOS 路径都错），
本项目改用 `start_in_rhino.py` 自行部署。

## 关于上游更新

**本仓库不跟踪上游更新。** GOLEM 相关代码是 vendor 进来的快照，
不会通过 `pip install -U golem-3dmcp` 更新，也不打算跟随上游变化做同步。
`requirements.txt` 里刻意不包含 `golem-3dmcp`，避免出现「pip 版」与「仓库内副本」两套代码互相干扰。

这样做是刻意的取舍：本项目需要的是**稳定可复现**的工具集，
而不是上游的最新特性；上游的第三方依赖版本是变动的，跟随会带来不必要的回归风险。

如果将来确实需要同步上游某个版本，建议按下面的顺序做（大约 15 分钟）：

1. 装一份新版到临时目录，不要装进本项目 venv：
   ```bat
   python -m pip download golem-3dmcp --no-deps -d %TEMP%\golem_new
   ```
2. 解包后与仓库内的 `golem_3dmcp/` 做差异对比：
   ```bat
   git --no-pager diff --no-index --stat %TEMP%\golem_new\golem_3dmcp golem_3dmcp
   ```
3. 把上游改动合并回来后，**重新确认上面那两处修复还在**（否则会重新踩 10053 与端口堵死）：
   ```bat
   findstr /c:"except ConnectionError as exc" golem_3dmcp\connection.py
   findstr /c:"golem-client" rhino_plugin\server.py
   ```
4. 同步更新 `THIRD_PARTY_LICENSES.txt` 里的改动说明，然后按版本号打一个 commit。

## 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| 页面状态灯红，提示 9876 未监听 | Rhino 里的插件没启动。ScriptEditor 运行 `start_in_rhino.py` |
| `[WinError 10053] 主机中的软件中止了一个已建立的连接` | 插件被重启过（`start_in_rhino.py` 会停掉旧实例），客户端手里是死连接。**已修复**：web 端会自动重建 MCP 会话并重试一次 |
| `[WinError 10061] 目标计算机积极拒绝` | 9876 没有监听，或插件 backlog 被塞满（旧版插件的缺陷，已修复） |
| 工具调用一直 TIMEOUT | Rhino 可能卡在模态对话框上；插件对 UI 线程派发设了 30 秒超时 |
| `golem doctor` 报 `UnicodeEncodeError: 'gbk' codec` | 中文 Windows 控制台打不出 `✓`。先 `set PYTHONIOENCODING=utf-8` 再跑 |
| 建模尺寸差 1000 倍 | 忘了单位约定。GOLEM 用**文档单位**，不换算（见上文「注意：单位不是米」） |
| 想限制任意代码执行 | `.env` 里设 `RHINO_WEB_DENY_TOOLS=execute_python,run_rhino_command,execute_rhinoscript` |
| 和别的 Rhino MCP 抢端口 | 本项目用 9876；GOLEM 的 Grasshopper 子服务默认 9877（代码里不会自动启动） |

## License

本项目采用 **MIT**，见 [LICENSE](LICENSE)。

`golem_3dmcp/` 与 `rhino_plugin/` 目录包含来自 GOLEM-3DMCP 的第三方代码（MIT，
Copyright (c) 2026 KingHippo），版权声明与本仓库的改动说明见
[THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt)。

