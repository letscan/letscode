# Claude Code → letscode 导入分析报告

**源文件**: `/Users/lichao/.claude/projects/-Users-lichao-Projects-letscode/72769d5b-2132-47b2-8da7-5149249e2afc.jsonl`

## 概览

- 源文件行数(记录数): **1730**
- 转换后 letscode 事件数: **1065**
- 已映射(对话+工具): **1064**
- 已丢弃(噪声/不支持): **666**

## 已映射的 CC 特性

| CC 记录 | letscode 事件 | 数量 |
|---|---|---|
| 真实 user prompt(string content) | `prompt` | 38 |
| compact 续接摘要 | `user_message_chunk` | 1 |
| assistant text block | `agent_message_chunk` | 221 |
| assistant tool_use | `tool_call` | 402 |
| user tool_result | `tool_call_update(completed/failed)` | 402 |

**配对正确性**:`tool_use.id` ↔ `tool_result.tool_use_id`。本次转换遇到 0 个孤儿 tool_result(找不到对应 tool_use,已跳过)。

其中 **16** 个 tool_result 标记为 `is_error`,转换为 `status=failed` 并包成 `<error>...</error>`。

## letscode 暂不支持的 CC 特性(已丢弃)

这些是 CC jsonl 里出现、但 letscode feed 模型尚无对应表示的特性。记录在此作为 letscode feed 格式演进的参考。

### 1. assistant thinking blocks
- 丢弃数量: **47**
- 原因:letscode 的 `agent_thought_chunk` 是**流式显示专用**,不写入 feed、不进入回放历史(见 `events.py` 的注释:thoughts are display-only)。CC 的 thinking 是持久化的一等公民。
- 演进建议:若 letscode 要保留推理历史,需新增持久化 thought 事件类型。

### 2. slash command 痕迹
- 丢弃数量: **8**
- 形式:user message content 以 `<command-name>`/`<local-command-*>` 开头
- 原因:letscode 的 slash command(`/compact`、`/new` 等)在 **ACP 层处理**,直接改写 feed 文件,不产生命令痕迹事件。
- 影响:导入的会话会丢失"用户曾执行过 /clear、/model"等元信息。

### 3. 顶层 type 被整体跳过的记录

| type | 数量 | 说明 |
|---|---|---|
| `ai-title` | 94 | CC 自动生成的会话标题;letscode 标题走 gen-title,单独存储 |
| `mode` | 92 | CC 的 mode 切换记录;letscode 用 preset/sandbox,粒度不同 |
| `permission-mode` | 91 | CC 的权限模式切换;letscode 无对应 |
| `last-prompt` | 90 | CC 记录最后一条 prompt;letscode 无对应 |
| `agent-name` | 84 | CC 的 agent 命名;letscode Agent 是无状态黑盒进程 |
| `attachment` | 64 | CC 的 attachment(skill_listing、auto_mode、plan 等);letscode 用 system prompt 注入,不入 feed |
| `file-history-snapshot` | 58 | CC 的文件历史快照;letscode 无对应 |
| `system` | 36 | CC 的 system 消息(local_command stdout、turn_duration 计时等);letscode 无 system 事件 |
| `queue-operation` | 2 | CC 的队列操作;letscode 无对应 |

### 6. Agent subagent 内部 transcript(未展开)
- Agent 工具调用数: **5**
- 处理方式:**扁平化**。Agent 的 tool_result 已内联子 agent 的最终答案文本,直接作为 `tool_call_update(completed)` 保留。子 agent 内部的 Read/Bash/Grep 等工具调用(在 `<session>/subagents/agent-*.jsonl`)未递归展开。
- 理由:letscode 的 Agent 工具本身就是黑盒 subprocess,扁平化贴合 letscode 语义。如需子 agent 内部细节,可后续扩展递归导入。

## 观察到的格式差异

### tool_result.content 形态
- bare string: **397**
- block list [type:text]: **5**
- letscode 统一为 string(`tool_call_update.rawOutput`)。导入时把 list 形态的 text block 拼接成单个字符串。

### 字段命名约定
- CC: `snake_case`(`tool_use_id`、`is_error`、`parent_uuid`)
- letscode: `camelCase`(`toolCallId`、`rawInput`、`rawOutput`)
- 这反映了两个项目的事件 schema 来源不同(CC 的 schema 服务于自家 UI/SDK,letscode 的 schema 对齐 ACP 规范)。

---
*本报告由 `letscode import-cc` 自动生成。*