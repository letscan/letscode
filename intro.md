# letscode 项目分析

## 概述

letscode 是一个轻量级的 Python AI 编程代理（Agent）框架（v0.1.0），兼容 letscode 的工具调用协议。它通过 OpenAI 兼容的 API 接入大语言模型，提供一个完整的 **LLM 调用 → 工具执行 → 结果反馈** 循环，使模型能够自主完成软件工程任务。

- **语言**: Python 3.11+
- **核心依赖**: `openai>=1.0`
- **包管理**: `uv`（`pyproject.toml` + `uv.lock`）
- **默认模型**: GLM-5-Turbo（通过智谱 API，`https://open.bigmodel.cn/api/coding/paas/v4`）
- **入口**: `letscode` CLI 命令 / `python -m letscode`
- **源码总量**: ~1311 行 Python

## 项目结构

```
letscode/                  # 项目根目录
├── pyproject.toml         # 项目元数据 + 依赖声明 + CLI 入口点
├── uv.lock                # 依赖锁定文件
├── config.json            # 模型配置（api_key, base_url, max_tokens）
├── .gitignore
├── .python-version        # Python 版本（3.13）
└── letscode/              # Python 包
    ├── __init__.py        # 包标识（空）
    ├── __main__.py        # python -m letscode 入口（7 行）
    ├── cli.py             # 命令行参数解析（63 行）
    ├── config.py          # 模型配置加载（73 行）
    ├── agent.py           # 核心 Agent 循环（193 行）
    ├── prompt.py          # 系统提示词构建（167 行）
    └── tools/             # 工具实现
        ├── __init__.py    # 工具注册表与调度（104 行）
        ├── _types.py      # 共享类型定义（11 行）
        ├── bash.py        # Shell 命令执行（105 行）
        ├── read.py        # 文件读取（77 行）
        ├── write.py       # 文件创建/覆盖（53 行）
        ├── edit.py        # 精确字符串替换（99 行）
        ├── glob.py        # 文件名模式匹配（74 行）
        └── grep.py        # 正则内容搜索（285 行）
```

## 核心模块分析

### 1. CLI 入口 (`cli.py`, 63 行)

提供命令行接口，支持以下参数：

| 参数 | 说明 |
|------|------|
| `prompt` | 发送给 Agent 的任务描述（必填位置参数） |
| `--config / -c` | JSON 配置文件路径 |
| `--model / -m` | 模型 ID（覆盖配置文件中的 `default_model`） |
| `--max-turns` | Agent 循环最大轮次（默认无限制） |
| `--workspace / -w` | Agent 工作目录（默认当前目录） |
| `--verbose / -v` | 显示详细工具调用信息 |

执行流程：解析参数 → 切换到指定工作目录 → `load_config()` 加载配置 → `run_agent()` 启动循环 → `finally` 块恢复原始工作目录。

### 2. 配置系统 (`config.py`, 73 行)

配置优先级：**CLI `--model` > 配置文件 `default_model` > 配置文件首个 model**

- `ModelConfig` 数据类：`model`（str）、`api_key`（str|None）、`base_url`（默认 `https://api.openai.com/v1`）、`max_tokens`（默认 16384）
- 支持从 JSON 文件加载多个模型配置，按 `model` 名称精确匹配
- CLI `--model` 覆盖配置文件的 `default_model`，匹配逻辑：先精确匹配 models 列表，未找到则回退到第一个条目
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
  ]
}
```

### 3. Agent 循环 (`agent.py`, 193 行)

这是整个项目的核心引擎，实现了 **ReAct 模式**的 Agent 循环：

```
用户 Prompt → 系统提示词 + 用户消息
    ↓
流式调用 LLM API（携带工具定义）
    ↓
┌─ LLM 返回文本 → 行缓冲实时输出到 stdout
│  LLM 返回工具调用 → 执行工具 → 将结果追加到消息历史
│                          ↓
│                     重新调用 LLM（带工具结果）
│                          ↓
│                     循环直到 LLM 不再调用工具
└─────────────────────────────┘
```

关键实现细节：

- **流式输出** (`_stream_response`): 使用 OpenAI streaming API，文本按行缓冲输出（`line_buf` 累积 token 直到遇到 `\n`），避免逐 token 闪烁
- **工具调用拼接**: streaming 模式下工具调用是分片到达的，通过 `tc_accum` 字典按 `index` 拼接 `id`、`name`、`arguments`
- **结果截断**: 工具返回结果超过 50000 字符时截断，附加截断提示（含原始字符数）
- **摘要生成** (`_result_summary`): 为每个工具结果生成一行摘要，针对不同工具类型（Bash/Read/Write/Edit/Glob/Grep）定制摘要格式；verbose 模式下输出到 stderr
- **并行工具**: 支持在单次响应中调用多个工具（OpenAI function calling 原生支持），所有工具结果作为独立 `tool` 角色消息追加
- **错误处理**: API 异常捕获并输出到 stderr，触发循环终止
- **OpenAI 客户端**: `api_key` 为 None 时使用 `"dummy"` 占位

### 4. 系统提示词 (`prompt.py`, 167 行)

系统提示词由 8 个段落拼接而成（静态 7 + 动态 1）：

| 段落 | 函数 | 内容 |
|------|------|------|
| Intro | `_intro_section()` | Agent 身份定义（软件工程助手）与安全边界（安全测试 vs 恶意用途） |
| System | `_system_section()` | 工具执行权限模式、标签处理、上下文压缩说明、hooks 机制 |
| Doing tasks | `_doing_tasks_section()` | 软件工程任务指导原则（代码优先、安全优先、最小改动） |
| Actions | `_actions_section()` | 风险操作确认机制（删除、推送等需用户确认），列举具体场景 |
| Using tools | `_using_tools_section()` | 工具选择优先级（专用工具 > Bash），并行工具调用规则 |
| Tone & style | `_tone_and_style_section()` | 输出风格要求（简洁、Markdown、代码引用 `file_path:line_number`） |
| Output efficiency | `_output_efficiency_section()` | 输出效率原则（直奔主题、减少废话、短句优先） |
| Environment | `_env_section()` | 动态生成：CWD、Git 状态、平台、Shell、OS 版本、模型 ID |

动态环境信息在运行时通过 `subprocess`（git）、`platform`、`os` 等模块获取：
- `_is_git_repo()`: 调用 `git rev-parse --is-inside-work-tree`（5 秒超时）
- `_get_shell_name()`: 解析 `$SHELL` 环境变量识别 zsh/bash
- `_get_os_version()`: 通过 `platform.system()` + `platform.release()` 获取

提示词来源：从 letscode 的 `src/constants/prompts.ts` 提取（external-user 分支）。

### 5. 工具系统 (`tools/`)

提供 6 个工具，每个工具模块包含 `SCHEMA`（OpenAI function calling schema 定义）和 `execute(args)`（执行函数，接收 `dict[str, Any]`，返回 `str`）。

#### 共享类型 (`tools/_types.py`, 11 行)

- `ToolExecutor`: 类型别名，`callable`（`(dict[str, Any]) -> str`）
- `get_cwd()`: 返回 `os.getcwd()`，供需要工作目录的工具使用

#### 工具注册与调度 (`tools/__init__.py`, 104 行)

- `TOOL_DEFINITIONS`: 6 个工具的 schema 列表（按 Bash、Read、Write、Edit、Glob、Grep 顺序），传入 LLM API
- `_EXECUTORS`: 工具名 → 执行函数的映射表
- `_call_summary()`: 为每个工具调用生成一行摘要（显示在 verbose 模式）
- `execute_tool(name, arguments)`: JSON 解析参数（支持宽松模式） → 查找执行器 → 调用执行 → 返回 `(result, call_summary)` 元组

#### Bash (`tools/bash.py`, 105 行)

- 通过 `subprocess.run([shell, "-c", command])` 执行命令，使用 `$SHELL` 环境变量（默认 `/bin/bash`）
- 默认超时 120 秒，通过 `timeout` 参数指定（毫秒转秒，最大 600 秒）
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

#### Glob (`tools/glob.py`, 74 行)

- 基于 `pathlib.Path.glob()` 的文件模式匹配
- 结果按修改时间倒序排列（`st_mtime` 降序）
- 最大返回 `MAX_RESULTS = 1000` 条结果，超出时截断并提示
- 返回相对路径（`f.relative_to(base)`），超出 base 范围时回退绝对路径

#### Grep (`tools/grep.py`, 285 行 — 最大的工具模块)

- 优先使用系统 `rg`（ripgrep，通过 `shutil.which("rg")` 检测），不可用时回退到 Python 实现
- 支持三种输出模式：`files_with_matches`（默认）、`content`、`count`
- 支持 glob 过滤、文件类型过滤（`_TYPE_MAP` 映射 12 种语言扩展名：js/ts/py/rust/go/java/c/cpp/rb/swift/kt）
- 支持上下文行（`-A`/`-B`/`-C`）、大小写不敏感（`-i`）、多行模式（`multiline`，`.` 匹配换行）
- 分页支持 `head_limit`（默认 250）和 `offset`
- `_format_output()`: 统一输出格式化，自动添加汇总头（`Found N files`）和截断提示
- ripgrep 后端 (`_search_rg`): 构建 rg 命令行参数，30 秒超时
- Python 后端 (`_search_python`): 使用 `os.walk` 遍历 + `re.compile` 匹配，跳过隐藏目录（`d.startswith(".")`），content 模式下用 `>` 标记匹配行

## 数据流总结

```
用户命令行
  │
  ▼
cli.py::main() ──argparse──▶ config.py::load_config() ──▶ ModelConfig
  │
  ├── os.chdir(workspace)  ──▶ 切换工作目录
  │
  ▼
agent.py::run_agent()
  │
  ├── prompt.py::build_system_prompt(model) ──▶ 系统提示词
  │
  ├── messages = [system + user]
  │
  ├── while True:
  │   ├── _stream_response() ──▶ OpenAI streaming API
  │   │   ├── 文本 → stdout（行缓冲）
  │   │   └── 工具调用 → tc_accum 拼接
  │   │
  │   ├── 无工具调用 → break
  │   │
  │   └── 每个工具调用:
  │       └── tools/__init__.py::execute_tool()
  │           ├── JSON 解析参数
  │           ├── _EXECUTORS[name](args)
  │           └── 结果 → messages（截断至 50000 字符）
  │
  └── os.chdir(original_cwd)  ──▶ 恢复工作目录
```

## 设计特点

1. **OpenAI 兼容协议**: 通过 `base_url` 可接入任何兼容 OpenAI Chat Completions API 的服务（智谱、本地模型等）
2. **流式交互**: 文本实时按行缓冲输出，避免逐 token 闪烁，用户体验接近实时对话
3. **工具专用化**: 每个工具只做一件事，schema 精确定义参数和描述，便于 LLM 理解和正确调用
4. **安全提示**: 系统提示词强调安全边界（OWASP top 10、输入验证、权限控制），Agent 对破坏性操作需用户确认
5. **容错设计**: 工具结果截断（50000 字符）、命令超时控制（120 秒）、异常统一包裹为 `<error>` 标签
6. **回退机制**: Grep 工具在无 ripgrep 时自动回退到纯 Python 实现（`os.walk` + `re`）
7. **简洁实现**: 总计 ~1311 行 Python，零外部依赖（仅 `openai`），核心逻辑清晰易读
8. **Prompt 工程借鉴**: 系统提示词从 letscode 官方 TypeScript 实现提取，保留其成熟的 Agent 行为指导
