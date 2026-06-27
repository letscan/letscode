# 事件格式对齐方案

## 三方消息流转

```
ACP Client          ACP Server           CLI (内部事件)            OpenAI API
    |                    |                       |                       |
    |--- prompt -------->|                       |                       |
    |                    |--- spawn CLI -------->|                       |
    |                    |                       |                       |
    |                    |                       | init                  |
    |                    |                       | prompt                |
    |                    |                       |---- messages -------->|
    |                    |                       |                       |
    |                    |  ┌──────── Agent Loop ──────────────────────┐ |
    |                    |  │                     |                    | |
    |                    |  │                     |<--- delta.content ─| |
    |                    |  │  agent_message_chunk|                    | |
    |                    |  │<-------------------|                    | |
    |  agent_msg_chunk   |  │                     |                    | |
    |<-------------------|  │                     |                    | |
    |                    |  │                     |                    | |
    |                    |  │                     |<--- tool_calls ────| |
    |                    |  │  tool_call          |                    | |
    |                    |  │<-------------------|                    | |
    |  tool_call         |  │                     |                    | |
    |<-------------------|  │                     |                    | |
    |                    |  │                     |                    | |
    |                    |  │  tool_update(in_prog)                    | |
    |                    |  │<-------------------|                    | |
    |  tool_update       |  │                     |                    | |
    |<-------------------|  │      [执行工具]     |                    | |
    |                    |  │                     |                    | |
    |                    |  │                     |---- tool result ──>| |
    |                    |  │  tool_update(done)  |                    | |
    |                    |  │<-------------------|                    | |
    |  tool_update       |  │                     |                    | |
    |<-------------------|  │                     |                    | |
    |                    |  │                     |                    | |
    |                    |  │  [Skill展开时]       |                    | |
    |                    |  │  user_msg_chunk     |  ← 仅写日志/发LLM  | |
    |                    |  │                     |---- user msg ─────>| |
    |                    |  └─────────────────────|───────────────────┘ |
    |                    |                       |                       |
    |                    |                       |<--- finish_reason ────|
    |                    |  result               |                       |
    |                    |<---------------------|                       |
    |  PromptResponse    |                       |                       |
    |<-------------------|                       |                       |
```

**关键信息流方向：**
- **ACP → 内部**：`prompt`（用户输入从 ACP client 流入 CLI）
- **OpenAI → 内部**：`agent_message_chunk`、`tool_call`、`result`（LLM 响应数据流入）
- **内部 → OpenAI**：messages 列表（含 prompt、tool result、合成 user message）
- **内部 → ACP**：`SessionUpdate` 事件（UI 展示，不含 `user_message_chunk`）
- **仅内部**：`user_message_chunk`（Skill 展开，仅写日志供 feed 重建，不发 ACP，不发 UI）

内部格式是双向枢纽，既要**接收**来自 ACP 和 OpenAI 的数据，也要**产出**发给 OpenAI 和 ACP 的数据。

## 设计约束

内部事件格式同时与两方双向交互：

1. **OpenAI API**（双向）：
   - **接收** ←：LLM 响应中的 `delta.content`、`delta.tool_calls`、`finish_reason`
   - **产出** →：构造 `messages` 列表（含 prompt、tool result、合成 user message）
   - **重建约束**（`feed.py` → `load_feed`）：事件日志必须能完整重建 messages 列表，用于多轮对话续接（`--feed`）
     - 必须保留：`toolName`、`rawInput`、`rawOutput` 等重建所需字段
     - 必须能区分：assistant text / tool_call / tool_result / user_message 的边界

2. **ACP**（双向）：
   - **接收** ←：`session/prompt` 请求（用户输入）
   - **产出** →：翻译为 `SessionUpdate`（`acp/server.py` → `_translate_event`），用于客户端 UI 展示
     - 需要：content blocks 结构、title/kind/status 等展示字段
     - 不直接存储在内部事件中：title/kind/rawInput/content 等展示字段由 ACP server 从缓存的 `tool_call` 数据推导

**原则：OpenAI 重建优先，ACP 对齐其次。** 格式改动必须确保 `feed.py` 能从事件日志精确重建 messages 列表。在此前提下，尽可能向 ACP 格式靠拢，使翻译层尽可能薄。

## 术语

- **session**：多轮对话的集合体，属于 ACP 层级。一个 session 可包含多次 CLI 调用。
- 本文档描述的事件格式属于 **CLI 层级**，表示单次 `run_agent` 调用的生命周期。不使用 `session/` 前缀。

## 事件对照

按执行流程排序：init → prompt → LLM 响应 → tool 请求 → tool 执行 → tool 结果 → 合成消息 → 会话结束。

每个事件展示完整的三方格式：
- **内部事件**：`type` + `timestamp` + `data` 的完整 JSON
- **OpenAI API 对应**：该事件在 OpenAI API 交互中对应的完整请求/响应结构
- **ACP 翻译**：翻译为 ACP `SessionUpdate` 后的完整 JSON

---

### 1. init — 新增

**— 纯内部元数据。** 单次 `run_agent` 调用启动时发射，记录运行环境和配置。

```
agent 启动 → 加载 config/rules/tools/mcp/skills → 事件
```

**内部事件（改后）：**
```json
{
  "type": "init",
  "timestamp": "2026-05-30T01:12:47.173Z",
  "data": {
    "agent": "letscode",
    "version": "0.2.2",
    "model": "deepseek-v4-flash",
    "cwd": "/Users/lichao/Projects/letscode",
    "maxTokens": 131072,
    "maxTurns": 30,
    "preset": "default",
    "sandbox": true,
    "tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "Skill", "Agent", "mcp__playwright__browser"],
    "mcpServers": {"playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}},
    "skills": ["hello", "code-review"],
    "rules": {"allowRead": ["**"], "denyRead": [".ssh/**"], "allowCmd": ["git", "ls"]}
  }
}
```

**OpenAI API 对应：** 无直接对应。这些是 `chat.completions.create` 请求的配置前提（决定用哪个 model、哪些 tools、什么安全策略），但不是 messages 列表的一部分。完整请求示例：
```json
POST /v1/chat/completions
{
  "model": "deepseek-v4-flash",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "hello"}
  ],
  "tools": [
    {"type": "function", "function": {"name": "Bash", "parameters": {...}}},
    {"type": "function", "function": {"name": "Read", "parameters": {...}}}
  ],
  "stream": true,
  "max_tokens": 131072
}
```

**ACP 翻译：** 不翻译为 session update。由 ACP server 内部消费。

**分析：** `feed.py` 从此事件提取 `model`（用于重建 system prompt）。其余字段为调试/审计信息。

---

### 2. prompt — 去掉嵌套

**→ OpenAI。** 用户输入，是唯一从 ACP 方向流入的事件。

```
ACP client → ACP server (session/prompt) → CLI subprocess 参数 → 内部事件 → OpenAI user message
```

**ACP 来源：** ACP server 收到 JSON-RPC 请求后，将 prompt 序列化为 CLI 参数并 spawn 子进程：
```json
{
  "jsonrpc": "2.0",
  "method": "session/prompt",
  "params": {
    "sessionId": "sess_abc123",
    "prompt": [
      {"type": "text", "text": "hello"},
      {"type": "resource_link", "name": "config.json", "uri": "file:///path/to/config.json"}
    ]
  }
}
```
ACP server 将 `params.prompt` 序列化为 JSON 字符串，作为 CLI 最后一个参数传入：
```
python -m letscode --event-stream --prompt-format json '[{"type":"text","text":"hello"}]'
```

**内部事件（当前）：**
```json
{
  "type": "session/prompt",
  "timestamp": "2026-05-30T01:12:47.173Z",
  "data": {
    "agent": "letscode",
    "version": "0.2.2",
    "model": "deepseek-v4-flash",
    "cwd": "/Users/lichao/Projects/letscode",
    "prompt": [{"text": "hello", "type": "text"}]
  }
}
```

**内部事件（改后）：** `data` 直接就是 content blocks 列表，去掉 `{"prompt": ...}` 包装。事件类型已经是 `prompt`，无需再嵌套一层：
```json
{
  "type": "prompt",
  "timestamp": "2026-05-30T01:12:47.173Z",
  "data": [{"text": "hello", "type": "text"}]
}
```

**OpenAI API 对应：** content blocks 提取为 messages 列表中的 user message。文本 block 提取为纯文本：
```json
{"role": "user", "content": "hello"}
```

**ACP 翻译：** 不翻译为 session update。此事件在 ACP 方向上是**输入**而非**输出**——数据从 ACP client 流入，不反向翻译回 ACP。

**验证：**
- OpenAI 重建：`data` 直接就是 content blocks 列表，`feed.py` 遍历 `data` 提取文本 ✓
- ACP：此事件不由 `_translate_event` 处理 ✓

---

### 3. agent_message_chunk — 去掉 content 嵌套

**← OpenAI。** LLM 流式响应的文本片段。

```
streaming delta.content → 逐行缓冲 → 事件 + stdout
```

**内部事件（当前）：**
```json
{
  "type": "agent_message_chunk",
  "timestamp": "2026-05-30T01:12:48.197Z",
  "data": {
    "content": {
      "type": "text",
      "text": "Hello! How can I help you with your coding or software engineering tasks today?"
    }
  }
}
```

**OpenAI API 来源：** streaming response 的 `delta.content`：
```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion.chunk",
  "choices": [{
    "index": 0,
    "delta": {"role": "assistant", "content": "Hello! How can I help you with your coding or software engineering tasks today?"},
    "finish_reason": null
  }]
}
```
`feed.py` 累积所有 chunk 的文本，重建为：
```json
{"role": "assistant", "content": "Hello! How can I help you with your coding or software engineering tasks today?"}
```

**ACP 翻译：** `update_agent_message_text("...")` 产出：
```json
{
  "sessionUpdate": "agent_message_chunk",
  "content": {
    "type": "text",
    "text": "Hello! How can I help you with your coding or software engineering tasks today?"
  }
}
```

**问题：** `data` 里多了一层 `{"content": ...}` 包装。ACP 的 `content` 字段直接就是 content block。

**改动：** `data` 去掉 `content` 嵌套，直接就是 content block：
```json
{
  "type": "agent_message_chunk",
  "timestamp": "2026-05-30T01:12:48.197Z",
  "data": {
    "type": "text",
    "text": "Hello! How can I help you with your coding or software engineering tasks today?"
  }
}
```

**验证：**
- OpenAI 重建：`data.text` 可提取文本 ✓（比 `data.content.text` 更直接）
- ACP 翻译：`{"sessionUpdate": type, "content": data}` — 零转换 ✓

---

### 4. tool_call — 精简（去掉 ACP 展示字段）

**← OpenAI。** LLM 请求执行工具。

```
streaming delta.tool_calls[i] → 累积 → 事件 + dispatch
```

**内部事件（当前）：**
```json
{
  "type": "tool_call",
  "timestamp": "2026-05-29T18:22:03.702Z",
  "data": {
    "toolCallId": "call_6d26a80d75e74f2c8c65c7ec",
    "toolName": "Bash",
    "title": "$ ls -la /Users/lichao/Projects/letscode",
    "kind": "other",
    "status": "pending",
    "input": {"command": "ls -la /Users/lichao/Projects/letscode", "description": "List files in current directory"}
  }
}
```

**内部事件（改后）：** `input` → `rawInput`（对齐 ACP 字段名），去掉 `title`、`kind`、`status`——这些是 ACP 展示专用字段，可由 ACP server 从 `toolName` + `rawInput` 推导。`status` 在 `tool_call` 中永远是 `"pending"`（状态变化由 `tool_call_update` 追踪），属于冗余。
```json
{
  "type": "tool_call",
  "timestamp": "2026-05-29T18:22:03.702Z",
  "data": {
    "toolCallId": "call_6d26a80d75e74f2c8c65c7ec",
    "toolName": "Bash",
    "rawInput": {"command": "ls -la /Users/lichao/Projects/letscode", "description": "List files in current directory"}
  }
}
```

**OpenAI API 来源：** streaming response 的 `delta.tool_calls[i]`，累积后为 assistant message 的 `tool_calls`：
```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion.chunk",
  "choices": [{
    "index": 0,
    "delta": {
      "role": "assistant",
      "tool_calls": [{"index": 0, "id": "call_6d26a80d75e74f2c8c65c7ec", "type": "function", "function": {"name": "Bash", "arguments": ""}}]
    },
    "finish_reason": null
  }]
}
```
`feed.py` 从内部事件提取 `toolCallId`/`toolName`/`rawInput`，重建为：
```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [{
    "id": "call_6d26a80d75e74f2c8c65c7ec",
    "type": "function",
    "function": {"name": "Bash", "arguments": "{\"command\": \"ls -la /Users/lichao/Projects/letscode\", \"description\": \"List files in current directory\"}"}
  }]
}
```

**ACP 翻译：** ACP server 从 `toolName` + `rawInput` 自行计算 `title`/`kind`/`status`，`rawInput` 直接透传（Bash 仅取 `command`），产出：
```json
{
  "sessionUpdate": "tool_call",
  "toolCallId": "call_6d26a80d75e74f2c8c65c7ec",
  "title": "Running: List files in current directory",
  "kind": "other",
  "status": "pending",
  "rawInput": "```\nls -la /Users/lichao/Projects/letscode\n```"
}
```

**分析：**
- 内部格式只保留 OpenAI 重建必需字段：`toolCallId`、`toolName`、`rawInput`
- `title`：从 `toolName` + `rawInput` 推导（如 Bash → `"Running: " + rawInput.description`，Read → `"Reading " + file_path`）
- `kind`：从 `toolName` 查表（如 Read→"read", Edit→"edit", Bash→"other"）
- `status`：`tool_call` 恒为 `"pending"`，无需存储
- `rawInput`：Bash 仅保留 `command`（不含 `description`），其他工具原样透传
- ACP 翻译时 `rawInput` 字段名直接对齐，无需重命名

---

### 5. tool_call_update (in_progress) — 精简（去掉 toolName）

**— 纯内部状态。** 工具开始执行。

```
dispatch 开始 → 状态通知 → 实际执行
```

**内部事件（当前）：**
```json
{
  "type": "tool_call_update",
  "timestamp": "2026-05-29T18:22:03.702Z",
  "data": {
    "toolCallId": "call_6d26a80d75e74f2c8c65c7ec",
    "status": "in_progress",
    "toolName": "Bash"
  }
}
```

**内部事件（改后）：** 去掉 `toolName`——ACP 不需要，`feed.py` 不消费 in_progress 事件。ACP server 如需关联工具名，可从之前缓存的 `tool_call` 事件中取（`pending_tool_inputs` 已维护此映射）。
```json
{
  "type": "tool_call_update",
  "timestamp": "2026-05-29T18:22:03.702Z",
  "data": {
    "toolCallId": "call_6d26a80d75e74f2c8c65c7ec",
    "status": "in_progress"
  }
}
```

**OpenAI API 对应：** 无。OpenAI API 没有 in_progress 概念。

**ACP 翻译：** `update_tool_call(...)` 产出：
```json
{
  "sessionUpdate": "tool_call_update",
  "toolCallId": "call_6d26a80d75e74f2c8c65c7ec",
  "status": "in_progress"
}
```

**分析：** `feed.py` 不消费 in_progress 事件。ACP server 翻译时只需 `toolCallId` + `status`。

---

### 6. tool_call_update (completed/failed) — 精简（去掉 toolName/duration_ms）

**→ OpenAI。** 工具执行结果，注入 messages 列表作为 tool result 发送给下一轮 API 调用。

```
工具执行完成 → _process_tool_result 规范化 → 事件 + messages.append
```

**内部事件（当前）：**
```json
{
  "type": "tool_call_update",
  "timestamp": "2026-05-29T18:22:03.741Z",
  "data": {
    "toolCallId": "call_6d26a80d75e74f2c8c65c7ec",
    "status": "completed",
    "toolName": "Bash",
    "content": [{"type": "content", "content": {"type": "text", "text": "19 lines"}}],
    "result": "total 408\ndrwxr-xr-x  18 lichao  staff     576 ...",
    "duration_ms": 38
  }
}
```

**内部事件（改后）：** `result` → `rawOutput`（与 `rawInput` 命名对称）。只保留 `toolCallId` + `status` + `rawOutput`。去掉 `toolName`（从 `tool_call` 缓存取）、`duration_ms`（无消费者）、`content`（ACP 展示专用，由 server 从 `toolName` + `rawOutput` + 缓存的 `rawInput` 推导）：
```json
{
  "type": "tool_call_update",
  "timestamp": "2026-05-29T18:22:03.741Z",
  "data": {
    "toolCallId": "call_6d26a80d75e74f2c8c65c7ec",
    "status": "completed",
    "rawOutput": "total 408\ndrwxr-xr-x  18 lichao  staff     576 ..."
  }
}
```

**OpenAI API 对应：** tool result message，注入 messages 列表：
```json
{
  "role": "tool",
  "tool_call_id": "call_6d26a80d75e74f2c8c65c7ec",
  "content": "total 408\ndrwxr-xr-x  18 lichao  staff     576 ..."
}
```

**ACP 翻译：** ACP server 从缓存取 `toolName` + `rawInput`，从 `rawOutput` 取原始输出，由 `_build_completed_content` 构建展示内容（resource_link、diff block、code block 等）：
```json
{
  "sessionUpdate": "tool_call_update",
  "toolCallId": "call_6d26a80d75e74f2c8c65c7ec",
  "title": "Ran: List files in current directory",
  "status": "completed",
  "content": [{"type": "content", "content": {"type": "text", "text": "```total 408\ndrwxr-xr-x  18 lichao  staff     576 ...\n```"}}]
}
```

**`status: "failed"` 变体：** 结构相同，区别仅在 `feed.py` 的重建逻辑——result 会被包裹在 `<error>` 标签中：
```python
pending_tools[tid]["result"] = f"<error>{result_text}</error>"
```

**实现影响：** `_build_completed_content` 需改为从缓存取 `toolName`（原来从 `data.toolName` 读取）和从 `data.rawOutput` 取原始输出（原来从 `data.content` 取摘要）。`feed.py` 中的 `_resolve_result` 改为读 `rawOutput` 字段。

---

### 7. user_message → user_message_chunk — 去掉 content 嵌套 + 改名 + 不发 ACP

**→ OpenAI（仅内部 + OpenAI，不发给 ACP）。** Skill 工具展开后合成的 user message，紧跟 tool result 之后注入 messages 列表。仅写日志供 `feed.py` 重建，不暴露给 ACP client。

```
Skill 工具完成 → _process_tool_result 拆分 → tool result + 合成 user message → 仅写日志/发 LLM
```

**内部事件（当前）：**
```json
{
  "type": "user_message",
  "timestamp": "2026-05-30T01:12:49.000Z",
  "data": {
    "content": {
      "type": "text",
      "text": "[Skill: hello]\nDescription: A simple greeting skill\n\nSay hello..."
    }
  }
}
```

**OpenAI API 对应：** 合成的 user message，注入 messages 列表发送给 API：
```json
{"role": "user", "content": "[Skill: hello]\nDescription: A simple greeting skill\n\nSay hello..."}
```

**ACP 翻译：** **不翻译。** Skill 展开的详细内容是 LLM 上下文，不是用户可见的 UI 消息。ACP client 不应看到原始 skill prompt。

**问题：**
1. `data` 里多了一层 `{"content": ...}` 包装
2. 事件名 `user_message` → 改为 `user_message_chunk`（与 ACP `sessionUpdate` 值对齐）
3. 当前 `_translate_event` 会将其翻译为 ACP `UserMessageChunk`，不应该暴露

**改动：**
- 事件名：`user_message` → `user_message_chunk`
- `data` 去掉 `content` 嵌套
- `_translate_event` 中不再翻译此事件（返回 `None`）

```json
{
  "type": "user_message_chunk",
  "timestamp": "2026-05-30T01:12:49.000Z",
  "data": {
    "type": "text",
    "text": "[Skill: hello]\nDescription: A simple greeting skill\n\nSay hello..."
  }
}
```

**验证：**
- OpenAI 重建：`data.text` 可提取文本，`feed.py` 正常消费 ✓
- ACP：`_translate_event` 跳过此事件，ACP client 不收到 skill 原始内容 ✓

---

### 8. result — 不变（原 session/result）

**← OpenAI。** 运行结束，LLM 不再请求工具。

```
finish_reason → 事件 → ACP PromptResponse
```

**内部事件：**
```json
{
  "type": "result",
  "timestamp": "2026-05-29T18:22:06.742Z",
  "data": {
    "stopReason": "end_turn",
    "turns": 2,
    "toolCalls": 1,
    "duration_ms": 4480
  }
}
```

**OpenAI API 来源：** streaming response 最终 chunk 的 `finish_reason`：
```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion.chunk",
  "choices": [{
    "index": 0,
    "delta": {},
    "finish_reason": "stop"
  }]
}
```
映射：`stop` → `end_turn`。

**ACP 翻译：** 不翻译为 session update。由 ACP server 构造 `PromptResponse`：
```json
{"stopReason": "end_turn"}
```

**分析：** `feed.py` 不消费此事件。`turns`/`toolCalls`/`duration_ms` 为内部统计。

**仅改名**（`session/result` → `result`），格式不变。

---

### 9. error — 不变

**— 纯内部状态。** API 调用异常或子进程错误，可在执行流程任意阶段发生。

**内部事件：**
```json
{
  "type": "error",
  "timestamp": "2026-05-29T18:22:04.000Z",
  "data": {
    "message": "Connection error: API unreachable",
    "code": "api_error",
    "recoverable": false
  }
}
```

**OpenAI API 对应：** 无。

**ACP 翻译：** 不翻译为 session update。由 ACP server 构造 `RequestError`。

**分析：** `feed.py` 不消费此事件。

**不改。**

---

## ACP 翻译规则

以下字段由 ACP server 在 `_translate_event` 中从内部事件数据推导，不存储在 JSONL 中。

### kind 映射

| toolName | kind |
|----------|------|
| Read | `read` |
| Write | `edit` |
| Edit | `edit` |
| Glob | `search` |
| Grep | `search` |
| Bash | `other` |
| Skill | `other` |
| Agent | `other` |
| mcp\_\* | `other` |

### title 规则

| toolName | tool_call title | tool_call_update title |
|----------|----------------|----------------------|
| Read | `Reading {file_path}` | `Read {file_path}` / `Failed to read {file_path}` |
| Write | `Writing {file_path}` | `Wrote {file_path}` / `Failed to write {file_path}` |
| Edit | `Editing {file_path}` | `Edited {file_path}` / `Failed to edit {file_path}` |
| Bash | `Running: {rawInput.description}` | `Ran: {rawInput.description}` / `Failed: {rawInput.description}` |
| Glob | `Searching files: {pattern}` | `Found {n} files` / `No files found` |
| Grep | `Searching: {pattern}` | `{result_summary}` |
| Skill | `Running skill: {skill}` | `Completed skill: {skill}` / `Failed skill: {skill}` |
| Agent | `Sub-agent: {prompt[:50]}` | `Sub-agent completed` / `Sub-agent failed` |
| mcp\_\* | `{server}.{method}` | `{status}` |

Bash 的 title 取自 `rawInput.description`（LLM 生成的一句话描述），而非 `rawInput.command`（命令本身过长且不利于 UI 展示）。

### rawInput 规则

| toolName | rawInput |
|----------|----------|
| Bash | `` ```{rawInput.command}``` ``（仅 command，不含 description） |
| 其他 | `rawInput` 原样（dict） |

`.description` 是给 title 用的，ACP 翻译时 Bash 的 `rawInput` 只保留 `command`。

### locations 规则

| toolName | locations |
|----------|-----------|
| 含 `file_path` 的工具 | `[ToolCallLocation(path=abs_path)]` |
| 其他 | 无 |

### content 规则（tool_call_update completed）

所有数据来源于缓存的 `toolName` + `rawInput`（来自 `tool_call`）和 `rawOutput`（来自 `tool_call_update`）。

| toolName | content 类型 | 来源 |
|----------|-------------|------|
| Read | `resource_link` | `rawInput.file_path` → `{name: basename, uri: file://abs_path}` |
| Edit | `diff` | `rawInput.file_path` + `rawInput.old_string`/`rawInput.new_string` |
| Write | `diff` | `rawInput.file_path` + `rawInput.content` + 文件当前内容 |
| Bash | `text`（code block） | `` ```{rawOutput}``` `` |
| 其他 | `text` | `rawOutput` 原文 |

---

## 总结

| # | 事件 | 方向 | OpenAI 重建必需字段 | ACP 对齐改动 | 理由 |
|---|---|---|---|---|---|
| 1 | `init` | — | `model` | 新增 | 会话配置独立，原 `session/prompt` 中的元数据拆出 |
| 2 | `prompt` | → OpenAI | `data`（content blocks 列表） | 去掉嵌套，`session/prompt` → `prompt` | `data` 直接即 content blocks 列表 |
| 3 | `agent_message_chunk` | ← OpenAI | `text` | `data` 去掉 `content` 嵌套 | `data` 直接即 content block，两边更直接 |
| 4 | `tool_call` | ← OpenAI | `toolCallId`, `toolName`, `rawInput` | `input`→`rawInput`，去掉 `title`/`kind`/`status` | `rawInput` 对齐 ACP 字段名 |
| 5 | `tool_call_update` (in_progress) | — | — | 去掉 `toolName` | `feed.py` 不消费，ACP 从缓存取 |
| 6 | `tool_call_update` (completed/failed) | → OpenAI | `toolCallId`, `rawOutput` | `result`→`rawOutput`，去掉 `toolName`/`duration_ms`/`content` | `rawInput`/`rawOutput` 命名对称 |
| 7 | `user_message` → `user_message_chunk` | → OpenAI | `text` | 改名 + 去掉 `content` 嵌套 + 不发 ACP | 仅内部 + OpenAI，ACP 不翻译 |
| 8 | `result` | ← OpenAI | — | 不变（`session/result` → `result`） | 内部消费 |
| 9 | `error` | — | — | 不变 | 内部消费 |

受影响文件：`events.py`（emit）、`acp/server.py`（_translate_event）、`feed.py`、`feed_util.py`
