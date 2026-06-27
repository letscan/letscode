# letscode 项目分析

## 概述

letscode 是一个轻量级的 Python AI 编程代理（Agent）框架（v0.1.0），实现了 **ReAct 模式**的 Agent 循环，通过 OpenAI 兼容的 API 接入大语言模型，并提供完整的 **LLM 调用 → 工具执行 → 结果反馈** 循环，使模型能够自主完成软件工程任务。

- **语言**: Python 3.11+
- **核心依赖**: `openai>=1.0`、`mcp>=1.27.0`
- **包管理**: `uv`（`pyproject.toml` + `uv.lock`）
- **构建系统**: Hatchling
- **默认模型**: GLM-5-Turbo（通过智谱 API，`https://open.bigmodel.cn/api/coding/paas/v4`）
- **入口**: `letscode` CLI 命令 / `python -m letscode`
- **源码总量**: ~2100 行 Python

## 项目结构

```
letscode/                          # 项目根目录
├── pyproject.toml                 # 项目元数据 + 依赖声明 + CLI 入口点
├── uv.lock                        # 依赖锁定文件
├── config.json                    # 模型配置（api_key, base_url, max_tokens, mcp_servers）
├── config.example.json            # 配置文件示例（供用户参考）
├── AGENTS.md                      # AI agent 工作区指令
├── README.md                      # （空）
├── intro.md                       # 本文件
├── docs/
│   └── acp-design.md              # ACP（Agent Communication Protocol）设计文档
├── .gitignore
├── .python-version                # Python 3.13
├── .letscode/                     # 运行时目录（.gitignore）
│   └── logs/                      # JSONL 事件日志
├── .claude/
│   ├── settings.local.json        # 客户端本地设置
│   └── skills/
│       └── hello/
│           └── SKILL.md           # 示例技能（问候模板）
└── letscode/                      # Python 包
    ├── __init__.py                # 包标识 + __version__ (2 行)
    ├── __main__.py                # python -m letscode 入口（7 行）
    ├── cli.py                     # 命令行参数解析 + async main（105 行）
    ├── config.py                  # 模型配置加载 + MCP 服务器配置（80 行）
    ├── events.py                  # JSONL 事件流发射器（155 行）
    ├── agent.py                   # 核心 Agent 循环（360 行）
    ├── prompt.py                  # 系统提示词构建（168 行）
    ├── mcp/
    │   ├── __init__.py            # 重新导出 McpManager（3 行）
    │   └── client.py              # MCP 服务器连接管理（155 行）
    └── tools/                     # 工具实现
        ├── __init__.py            # 工具注册表与调度（119 行）
        ├── _types.py              # 共享类型定义（11 行）
        ├── bash.py                # Shell 命令执行（105 行）
        ├── read.py                # 文件读取（77 行）
        ├── write.py               # 文件创建/覆盖（53 行）
        ├── edit.py                # 精确字符串替换（99 行）
        ├── glob.py                # 文件名模式匹配（109 行）
        ├── grep.py                # 正则内容搜索（261 行）
        ├── skill.py               # 技能提示词加载与调用（192 行）
        └── agent.py               # 子代理 schema 定义（58 行）
```

## 核心模块分析

### 1. CLI 入口 (`cli.py`, 105 行)

提供命令行接口，使用 `argparse` 解析参数，并通过 `asyncio.run()` 启动异步主函数：

| 参数 | 说明 |
|------|------|
| `prompt` | 发送给 Agent 的任务描述（必填位置参数） |
| `--config / -c` | JSON 配置文件路径 |
| `--model / -m` | 模型 ID（覆盖配置文件中的 `default_model`） |
| `--max-turns` | Agent 循环最大轮次（默认无限制） |
| `--workspace / -w` | Agent 工作目录（默认当前目录） |
| `--verbose / -v` | 显示详细工具调用信息 |
| `--no-mcp` | 跳过 MCP 服务器连接（供子代理内部使用） |
| `--event-stream` | 以 JSONL 事件流格式输出到 stdout（替代人类可读文本） |

执行流程：解析参数 → 切换到指定工作目录 → `load_config()` 加载模型配置和 MCP 服务器配置 → 创建 `EventEmitter`（日志写入 `.letscode/logs/`）→ `McpManager.connect_all()` 初始化 MCP 连接 → `run_agent()` 启动循环 → `finally` 块断开 MCP 连接、关闭事件发射器并恢复原始工作目录。

子代理通过 `--no-mcp` 跳过 MCP 连接，避免重复连接和清理问题。

### 2. 配置系统 (`config.py`, 80 行)

配置优先级：**CLI `--model` > 配置文件 `default_model` > 配置文件首个 model**

- `ModelConfig` 数据类：`model`（str）、`api_key`（str|None）、`base_url`（默认 `https://api.openai.com/v1`）、`max_tokens`（默认 16384）
- `McpServerConfig` 类型别名：`dict[str, any]`，支持 stdio（`{command, args?, env?}`）和 http/sse（`{url, headers?}`）两种配置格式
- `load_config()` 返回 `(ModelConfig, dict[str, McpServerConfig])` 元组
- 支持从 JSON 文件加载多个模型配置，按 `model` 名称精确匹配
- `OPENAI_API_KEY` 和 `OPENAI_BASE_URL` 环境变量始终覆盖文件配置（在模型条目加载之后）
- `max_tokens` 加载时 `min(entry, 131072)` 硬限制
- 未指定模型且无配置文件时抛出 `SystemExit` 错误

配置文件示例（`config.json`）：
```json
{
  "default_model": "glm-5-turbo",
  "models": [
    {
      "model": "glm-5-turbo",
      "api_key": "...",
      "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
      "max_tokens": 200000
    }
  ],
  "mcp_servers": {
    "playwright": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"]
    },
    "exa": {
      "url": "https://mcp.exa.ai/mcp",
      "headers": {}
    }
  }
}
```

### 3. 事件流系统 (`events.py`, 155 行)

**新增模块**。实现 JSONL 格式的事件流发射器，用于记录 Agent 会话的完整执行过程：

- **EventEmitter** 类：
  - 日志写入 `.letscode/logs/` 目录，文件名格式 `{YYYYMMDD_HHMMSS}_{4位hex}.jsonl`
  - 可选 `to_stdout` 模式（`--event-stream` 参数），将事件流输出到 stdout 替代人类可读文本
- **事件类型**：
  | 事件 | 方法 | 说明 |
  |------|------|------|
  | `session/prompt` | `emit_session_prompt()` | 会话开始：版本、模型、CWD、用户 prompt |
  | `agent_message_chunk` | `emit_agent_message_chunk()` | LLM 文本输出片段 |
  | `tool_call` | `emit_tool_call()` | 工具调用发起（含 ACP kind 映射、title 生成） |
  | `tool_call_update` | `emit_tool_update()` | 工具状态变更（`in_progress` → `completed`/`failed`），含耗时、摘要、结果 |
  | `error` | `emit_error()` | 错误事件（含错误码、是否可恢复） |
  | `session/result` | `emit_session_result()` | 会话结束：停止原因、轮次数、工具调用数、总耗时 |
- **ACP 兼容**：`_tool_kind()` 将内置工具映射到 ACP 工具类型（read/edit/execute/search/other）
- **_tool_title()**：为每种工具生成人类可读的标题（如 `"$ ls -la"`、`"Reading src/main.py"`）

### 4. Agent 循环 (`agent.py`, 360 行)

这是整个项目的核心引擎，实现了 **ReAct 模式**的 Agent 循环：

```
用户 Prompt → 系统提示词 + 用户消息
    ↓
流式调用 LLM API（携带工具定义，含内置工具 + MCP 工具）
    ↓
┌─ LLM 返回文本 → 行缓冲实时输出到 stdout（或通过 emitter 发送事件）
│  LLM 返回工具调用 → 执行工具 → 将结果追加到消息历史
│                          ↓
│                     重新调用 LLM（带工具结果）
│                          ↓
│                     循环直到 LLM 不再调用工具
└─────────────────────────────┘
```

关键实现细节：

- **流式输出** (`_stream_response`): 使用 OpenAI streaming API，文本按行缓冲输出（`line_buf` 累积 token 直到遇到 `\n`），避免逐 token 闪烁；当 emitter 存在时，文本通过 `emit_agent_message_chunk()` 发送而非直接写 stdout
- **事件集成**: 整个循环中通过 `EventEmitter` 发射完整的事件流——session 开始、每个工具调用的 pending/in_progress/completed 状态、错误和 session 结束（含 `_stop_reason()` 判断停止原因：`end_turn` / `max_turn_requests` / `error`）
- **工具调用拼接**: streaming 模式下工具调用是分片到达的，通过 `tc_accum` 字典按 `index` 拼接 `id`、`name`、`arguments`
- **子代理** (`_run_subagent`): 将 `Agent` 工具调用通过 `subprocess` 启动 `letscode` 子进程来执行，传入 `--no-mcp` 和 `--max-turns 30`，设置 300 秒超时
- **Read-before-edit 强制**: 维护 `_read_files` 集合追踪已读文件，Edit 工具调用时检查文件是否已被读取，未读取则拒绝执行
- **结果截断**: 工具返回结果超过 50000 字符时截断，附加截断提示（含原始字符数）
- **摘要生成** (`_result_summary`): 为每个工具结果生成一行摘要，针对不同工具类型（Bash/Read/Write/Edit/Glob/Grep/Skill/Agent/MCP）定制摘要格式；verbose 模式下输出到 stderr
- **并行工具**: 支持在单次响应中调用多个工具（OpenAI function calling 原生支持），所有工具结果作为独立 `tool` 角色消息追加
- **工具合并**: 内置工具定义与 MCP 工具定义合并后统一传给 LLM，调度时根据名称前缀区分（`mcp__` 前缀走 MCP 通道）
- **Skill 内联注入**: Skill 工具调用成功后，工具结果作为短确认消息（`"Launching skill: {name}"`），实际技能内容作为 `user` 消息注入对话上下文
- **错误处理**: API 异常捕获并输出到 stderr + 通过 emitter 发射 error 事件，触发循环终止

### 5. 系统提示词 (`prompt.py`, 168 行)

系统提示词由 8 个段落拼接而成（静态 7 + 动态 1）：

| 段落 | 函数 | 内容 |
|------|------|------|
| Intro | `_intro_section()` | Agent 身份定义（软件工程助手）与安全边界（安全测试 vs 恶意用途） |
| System | `_system_section()` | 工具执行权限模式、标签处理、上下文压缩说明、hooks 机制 |
| Doing tasks | `_doing_tasks_section()` | 软件工程任务指导原则（代码优先、安全优先、最小改动） |
| Actions | `_actions_section()` | 风险操作确认机制（删除、推送等需用户确认），列举具体场景 |
| Using tools | `_using_tools_section()` | 工具选择优先级（专用工具 > Bash）、并行工具调用规则、Skill 说明 |
| Tone & style | `_tone_and_style_section()` | 输出风格要求（简洁、Markdown、代码引用 `file_path:line_number`） |
| Output efficiency | `_output_efficiency_section()` | 输出效率原则（直奔主题、减少废话、短句优先） |
| Environment | `_env_section()` | 动态生成：CWD、Git 状态、平台、Shell、OS 版本、模型 ID |

动态环境信息在运行时通过 `subprocess`（git）、`platform`、`os` 等模块获取：
- `_is_git_repo()`: 调用 `git rev-parse --is-inside-work-tree`（5 秒超时）
- `_get_shell_name()`: 解析 `$SHELL` 环境变量识别 zsh/bash
- `_get_os_version()`: 通过 `platform.system()` + `platform.release()` 获取

提示词来源：参考主流 Agent 框架的系统提示词结构，适配 letscode。

### 6. MCP 集成 (`mcp/client.py`, 155 行)

实现 Model Context Protocol（MCP）客户端，允许 Agent 连接外部 MCP 服务器获取额外工具：

- **McpConnection**: 单个服务器连接，支持三种传输协议：
  - `stdio`：通过子进程的 stdin/stdout 通信（`{command, args?, env?}` 配置）
  - `sse`：Server-Sent Events（URL 含 `/sse` 或 `type: "sse"` 配置时使用）
  - `streamable-http`：可流式 HTTP（默认用于现代服务器，`{url, headers?}` 配置）
- **McpManager**: 管理所有 MCP 连接的集合类
  - `connect_all(servers)`: 批量连接配置中定义的服务器列表
  - `disconnect_all()`: 批量断开所有连接
  - `get_tool_definitions()`: 收集所有服务器的工具定义，统一添加 `mcp__{server}__{tool}` 前缀防止命名冲突
  - `resolve_tool(prefixed_name)`: 将 `mcp__{server}__{tool}` 解析为 `(McpConnection, original_tool_name)`
  - `call_tool(name, arguments)`: 根据前缀解析目标服务器和工具名，转发调用
  - `get_tool_count()`: 返回所有 MCP 工具的总数
- MCP 工具与内置工具在 Agent 循环中无缝合并，LLM 通过统一接口调用

### 7. 工具系统 (`tools/`)

提供 7 个内置工具 + 1 个子代理工具，每个工具模块包含 `SCHEMA`（OpenAI function calling schema 定义）和 `execute(args)`（执行函数，接收 `dict[str, Any]`，返回 `str`）。

#### 共享类型 (`tools/_types.py`, 11 行)

- `ToolExecutor`: 类型别名，`callable`（`(dict[str, Any]) -> str`）
- `get_cwd()`: 返回 `os.getcwd()`，供需要工作目录的工具使用

#### 工具注册与调度 (`tools/__init__.py`, 119 行)

- `TOOL_DEFINITIONS`: 7 个工具的 schema 列表（不含 Agent，Agent schema 在 `agent.py` 中动态合并以避免循环导入）
- `_EXECUTORS`: 工具名 → 执行函数的映射表
- `_call_summary()`: 为每个工具调用生成一行摘要（显示在 verbose 模式）
- `execute_tool(name, arguments)`: JSON 解析参数（支持宽松模式 `strict=False`） → 查找执行器 → 调用执行 → 返回 `(result, call_summary)` 元组

#### Bash (`tools/bash.py`, 105 行)

- 通过 `subprocess.run([shell, "-c", command])` 执行命令，使用 `$SHELL` 环境变量（默认 `/bin/bash`）
- 默认超时 120 秒，通过 `timeout` 参数指定（毫秒转秒，最大 1800 秒）
- 捕获 stdout 和 stderr，非零退出码在输出末尾附加 `[Exit code: N]`
- 超时返回 `<error>Command timed out after {timeout}s</error>`，其他异常同样 `<error>` 包裹
- 无输出时返回 `(no output)`

#### Read (`tools/read.py`, 77 行)

- 支持 `offset`（起始行号，1-based）和 `limit`（行数）参数进行分页读取
- 输出格式：右对齐 6 位行号 + tab + 内容（`f"{i:>6}\t{content}"`），模拟 `cat -n`
- 对目录返回错误提示（建议使用 Bash ls），不存在文件、非普通文件均有明确错误提示

#### Write (`tools/write.py`, 53 行)

- 创建新文件或完全覆盖已有文件
- 自动创建父目录（`p.parent.mkdir(parents=True, exist_ok=True)`）
- 返回信息区分"创建"和"更新"，均附带行数统计：`File created successfully at: {path} (N lines)` / `The file {path} has been updated. (N lines)`

#### Edit (`tools/edit.py`, 99 行)

- **精确字符串替换**，非正则
- 默认要求 `old_string` 在文件中唯一（出现 > 1 次时报错提示扩大上下文或使用 `replace_all`）
- `old_string` 不存在时报错，提示检查精确匹配
- `replace_all=true` 时替换所有匹配项，返回替换次数和行数变化
- 返回信息包含行数变化：`({old_lines} lines -> {new_lines} lines)` 或 `Replaced N occurrences`
- 用于局部修改，比 Write 更精确（只传 diff 而非整个文件）
- **Read-before-edit**: 与 Agent 层面配合，调用前必须先 Read 目标文件

#### Glob (`tools/glob.py`, 109 行)

- 优先使用系统 `rg --files --glob`（ripgrep），不可用时回退到 `pathlib.Path.glob()`
- 结果按修改时间倒序排列（`st_mtime` 降序）
- 最大返回 `MAX_RESULTS = 1000` 条结果，超出时截断并提示
- 返回相对路径（`f.relative_to(base)`），超出 base 范围时回退绝对路径

#### Grep (`tools/grep.py`, 261 行)

- 优先使用系统 `rg`（ripgrep，通过 `shutil.which("rg")` 检测），不可用时回退到系统 `grep`
- 支持三种输出模式：`files_with_matches`（默认）、`content`、`count`
- 支持 glob 过滤、文件类型过滤（`_TYPE_MAP` 映射 12 种语言扩展名：js/ts/py/rust/go/java/c/cpp/rb/swift/kt）
- 支持上下文行（`-A`/`-B`/`-C`）、大小写不敏感（`-i`）、多行模式（`multiline`，`.` 匹配换行）
- 分页支持 `head_limit`（默认 250）和 `offset`
- `_format_output()`: 统一输出格式化，自动添加汇总头（`Found N files`）和截断提示
- ripgrep 后端 (`_search_rg`): 构建 rg 命令行参数，30 秒超时
- grep 后端 (`_search_grep`): 使用系统 `grep -E` 命令，30 秒超时；不支持多行模式时返回错误提示

#### Skill (`tools/skill.py`, 192 行)

- 加载 `.claude/skills/` 目录下的 `SKILL.md` 技能文件
- 支持 YAML frontmatter 解析（`---` 分隔的元数据块），提取 `description`、`name` 等字段
- 模板变量 `$ARGUMENTS` 和 `${ARGUMENTS}` 替换：将用户传入的参数替换到技能提示词中
- 支持递归搜索项目级（`.claude/skills/`）、父目录级（向上查到 `.git`）和用户级（`~/.claude/skills/`）的技能路径（`.claude/skills/` 为跨客户端通用约定）
- 技能本质上是预定义的提示词模板，加载后注入到 Agent 的对话上下文中
- `get_skill_list()`: 返回可用技能的 `{name, description}` 列表

#### Agent (`tools/agent.py`, 58 行)

- 定义子代理的 schema（参数：`description`、`prompt`、`subagent_type`）
- `execute()` 为存根函数，实际执行逻辑在 `agent.py` 的 `_run_subagent()` 中
- 支持两种子代理类型：
  - `general-purpose`：全功能代理，可搜索代码和执行多步任务
  - `Explore`：只读代理，快速探索代码库（Bash、Read、Glob、Grep）
- 允许 Agent 将子任务委托给独立的 Agent 实例执行，实现任务分解

## 数据流总结

```
用户命令行
  │
  ▼
cli.py::main() ──argparse──▶ config.py::load_config() ──▶ (ModelConfig, mcp_servers)
  │                                         │
  ├── os.chdir(workspace)                    ├── config.json (模型列表, API 密钥, MCP 服务器)
  │                                         └── OPENAI_API_KEY / OPENAI_BASE_URL 环境变量
  ├── EventEmitter(.letscode/logs/, --event-stream)
  │
  ▼
McpManager.connect_all(mcp_servers) ──▶ 连接 MCP 服务器 (stdio/HTTP/SSE)
  │
  ▼
agent.py::run_agent()
  │
  ├── prompt.py::build_system_prompt(model) ──▶ 8 段式系统提示词
  │
  ├── emitter.emit_session_prompt()  ──▶ 记录会话开始
  │
  ├── 合并内置工具 + MCP 工具定义
  │
  ├── messages = [system + user]
  │
  ├── while True:
  │   ├── _stream_response() ──▶ OpenAI streaming API
  │   │   ├── 文本 → stdout（行缓冲）或 emitter.emit_agent_message_chunk()
  │   │   └── 工具调用 → tc_accum 拼接
  │   │
  │   ├── 无工具调用 → break
  │   │
  │   └── 每个工具调用:
  │       ├── emitter.emit_tool_call() ──▶ pending 状态
  │       ├── emitter.emit_tool_update() ──▶ in_progress 状态
  │       ├── Agent → subprocess 启动 letscode 子代理（--no-mcp, 300s 超时）
  │       ├── mcp__* → McpManager.resolve_tool() + call_tool() (await)
  │       ├── Skill → tool 消息确认 + user 消息注入技能内容
  │       ├── Edit → read-before-edit 检查
  │       └── 其他 → tools/__init__.py::execute_tool()
  │           ├── JSON 解析参数（strict=False 回退）
  │           ├── _EXECUTORS[name](args)
  │           └── 结果 → messages（截断至 50000 字符）
  │       │
  │       ├── emitter.emit_tool_update() ──▶ completed/failed 状态（含耗时、结果）
  │       └── _result_summary() ──▶ verbose 模式摘要
  │
  └── emitter.emit_session_result()  ──▶ 记录会话结束（停止原因、轮次、耗时）
      │
      ▼
  McpManager.disconnect_all() + emitter.close()
```

## 设计特点

1. **OpenAI 兼容协议**: 通过 `base_url` 可接入任何兼容 OpenAI Chat Completions API 的服务（智谱、本地模型等）
2. **流式交互**: 文本实时按行缓冲输出，避免逐 token 闪烁，用户体验接近实时对话
3. **MCP 扩展协议**: 通过 Model Context Protocol 连接外部工具服务器（stdio/HTTP/SSE 三种传输方式），工具自动发现和前缀隔离（`mcp__{server}__{tool}`），可无限扩展工具集
4. **工具专用化**: 每个工具只做一件事，schema 精确定义参数和描述，便于 LLM 理解和正确调用
5. **安全提示**: 系统提示词强调安全边界（OWASP top 10、输入验证、权限控制），Agent 对破坏性操作需用户确认
6. **容错设计**: 工具结果截断（50000 字符）、命令超时控制（120 秒）、异常统一包裹为 `<error>` 标签
7. **回退机制**: Grep 工具在无 ripgrep 时回退到系统 `grep`；Glob 工具回退到 `pathlib.glob()`
8. **Read-before-edit 强制**: Edit 工具在 Agent 层面强制要求先 Read 目标文件，避免盲目编辑
9. **子代理委托**: 支持将复杂任务拆分给独立的 `letscode` 子进程执行（`--no-mcp` 隔离），实现任务分解
10. **技能系统**: 基于 Markdown 的技能模板，支持 YAML 元数据和 `$ARGUMENTS` 变量扩展，支持项目级/父目录级/用户级三层搜索
11. **JSONL 事件流**: 新增 `EventEmitter` 模块，完整记录会话的 prompt、消息片段、工具调用全生命周期（pending → in_progress → completed/failed）、错误和结果；支持 `--event-stream` 模式将事件流输出到 stdout，便于 IDE/前端集成；日志持久化到 `.letscode/logs/` 目录，包含 ACP 兼容的工具类型映射
12. **简洁实现**: 核心逻辑约 2100 行 Python，核心依赖仅 `openai` 和 `mcp`
13. **Prompt 工程借鉴**: 系统提示词参考主流 Agent 框架，保留其成熟的 Agent 行为指导
