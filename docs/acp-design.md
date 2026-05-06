# letscode Event Stream 设计

> 参考 [ACP (Agent Client Protocol)](https://agentclientprotocol.com/protocol/overview) 的消息结构，设计 letscode 的 JSONL 事件流格式。

## 1. 目标

letscode CLI 始终在后台输出 JSONL 事件日志文件，`--event-stream` 参数控制是否同时将事件流输出到 stdout（替代人类可读文本）。

**不是双向协议**。letscode 仍然是 `letscode "prompt"` 的一次性调用，只是输出通道变为结构化 JSON 事件。

## 2. 使用方式

```bash
# 传统模式 — stdout 输出人类可读文本，后台写 JSONL log
letscode "帮我检查代码"

# event-stream 模式 — stdout 输出 JSONL 事件流，后台也写 JSONL log
letscode --event-stream "帮我检查代码"
```

两种模式的行为差异只在 stdout：
- **默认**：stdout 输出文本（现有行为）
- **`--event-stream`**：stdout 输出 newline-delimited JSON 事件

**后台 JSONL log 始终写入**，无论是否指定 `--event-stream`。

## 3. 日志文件位置

```
.letscode/logs/{YYYYMMDD}_{HHMMSS}_{short_uuid}.jsonl
```

示例：`.letscode/logs/20260427_103000_a1b2.jsonl`

由工作目录下的 `.letscode/logs/` 目录自动创建。

## 4. 事件格式

每个事件是一行 JSON，通用结构：

```json
{
  "type": "<event_type>",
  "timestamp": "2026-04-27T10:30:00.123Z",
  "data": { ... }
}
```

- `type` — 事件类型
- `timestamp` — ISO 8601，毫秒精度
- `data` — 事件载荷，结构因 type 而异

## 5. 事件类型

### 5.1 `session/prompt` — 会话开始 + 用户输入

对应 ACP 的 `session/prompt` 请求。每次 CLI 调用发送一次，包含元信息和用户 prompt。

```json
{
  "type": "session/prompt",
  "timestamp": "2026-04-27T10:30:00.000Z",
  "data": {
    "agent": "letscode",
    "version": "0.1.0",
    "model": "glm-5-turbo",
    "cwd": "/home/user/project",
    "prompt": [
      {
        "type": "text",
        "text": "帮我检查代码质量"
      }
    ]
  }
}
```

`data.prompt` 使用 ACP 的 `ContentBlock[]` 结构，与 ACP `session/prompt.params.prompt` 格式一致。当前只有 text 类型，未来可扩展 image、resource 等。

### 5.2 `agent_message_chunk` — LLM 文本输出

对应 ACP 的 `SessionUpdate.agent_message_chunk`。LLM 的流式文本输出，逐块推送。

```json
{
  "type": "agent_message_chunk",
  "timestamp": "2026-04-27T10:30:01.234Z",
  "data": {
    "content": {
      "type": "text",
      "text": "我来帮你检查代码质量..."
    }
  }
}
```

### 5.3 `tool_call` — 工具调用创建

对应 ACP 的 `SessionUpdate.tool_call`。LLM 请求工具调用时发送，状态为 `pending`。

```json
{
  "type": "tool_call",
  "timestamp": "2026-04-27T10:30:02.100Z",
  "data": {
    "toolCallId": "call_001",
    "title": "Reading main.py",
    "kind": "read",
    "status": "pending",
    "input": {
      "file_path": "/home/user/project/main.py"
    }
  }
}
```

字段：
- `toolCallId` — 来自 LLM 返回的 tool_call id
- `title` — 人类可读描述
- `kind` — ACP tool kind（见第 6 节映射表）
- `status` — `pending` | `in_progress` | `completed` | `failed`
- `input` — 工具输入参数

### 5.4 `tool_call_update` — 工具状态变更

对应 ACP 的 `SessionUpdate.tool_call_update`。

开始执行（`in_progress`）：

```json
{
  "type": "tool_call_update",
  "timestamp": "2026-04-27T10:30:02.150Z",
  "data": {
    "toolCallId": "call_001",
    "status": "in_progress"
  }
}
```

执行完成（`completed`）：

```json
{
  "type": "tool_call_update",
  "timestamp": "2026-04-27T10:30:02.300Z",
  "data": {
    "toolCallId": "call_001",
    "status": "completed",
    "content": [
      {
        "type": "content",
        "content": {
          "type": "text",
          "text": "42 lines"
        }
      }
    ],
    "result": "full tool output text...",
    "duration_ms": 150
  }
}
```

- `content` — 人类可读摘要（ACP 结构）
- `result` — 完整工具输出（letscode 扩展，用于会话回放）
- `duration_ms` — 执行耗时

### 5.5 `session/result` — 会话结束

对应 ACP 的 `session/prompt` 响应（StopReason）。

```json
{
  "type": "session/result",
  "timestamp": "2026-04-27T10:30:15.000Z",
  "data": {
    "stopReason": "end_turn",
    "turns": 3,
    "toolCalls": 5,
    "duration_ms": 15000
  }
}
```

**StopReason**：

| 值 | 含义 |
|---|---|
| `end_turn` | LLM 正常结束，不再请求工具 |
| `max_turn_requests` | 达到 max_turns 限制 |
| `error` | 运行出错 |

### 5.6 `error` — 错误

运行期间的非致命错误。

```json
{
  "type": "error",
  "timestamp": "2026-04-27T10:30:05.000Z",
  "data": {
    "message": "API rate limit exceeded",
    "code": "rate_limit",
    "recoverable": true
  }
}
```

致命错误后紧跟 `session/result`（`stopReason: "error"`）。

## 6. 工具 → Tool Kind 映射

| letscode 工具 | kind | title 模板 |
|---|---|---|
| `Read` | `read` | `"Reading {file_path}"` |
| `Write` | `edit` | `"Writing {file_path}"` |
| `Edit` | `edit` | `"Editing {file_path}"` |
| `Bash` | `execute` | `"$ {command}"`（首行） |
| `Glob` | `search` | `"Searching files: {pattern}"` |
| `Grep` | `search` | `"Searching: {pattern}"` |
| `Skill` | `other` | `"Running skill: {skill}"` |
| `Agent` | `other` | `"Sub-agent: {prompt[:50]}"` |
| `mcp__*` | `other` | `"{server}/{tool}"` |

## 7. Skill 工具处理

Skill 工具在 letscode 内部会将结果拆分为 `tool` 消息 + `user` 消息注入对话，但在事件流中**不产生额外的 user_message 事件**。这与 Codex CLI 的 ACP 实现一致——Skill 内容通过 `tool_call_update` 的完整 `result` 字段承载，回放时由 `--feed` 逻辑负责重建对话结构。

## 8. 与 ACP 的对应关系

| letscode 事件 | ACP 对应 | 说明 |
|---|---|---|
| `session/prompt` | `session/prompt` 请求 | 元信息 + prompt 合并为一个事件 |
| `agent_message_chunk` | `SessionUpdate.agent_message_chunk` | 结构相同 |
| `tool_call` | `SessionUpdate.tool_call` | 结构相同 |
| `tool_call_update` | `SessionUpdate.tool_call_update` | 额外增加 `result`、`duration_ms` |
| `session/result` | `session/prompt` 响应 (StopReason) | ACP 用请求响应，letscode 用事件 |
| `error` | JSON-RPC error response | letscode 用事件而非响应 |

## 9. 完整示例

```bash
$ letscode --event-stream "检查 src/ 下的代码质量"
```

```jsonl
{"type":"session/prompt","timestamp":"2026-04-27T10:30:00.000Z","data":{"agent":"letscode","version":"0.1.0","model":"glm-5-turbo","cwd":"/home/user/project","prompt":[{"type":"text","text":"检查 src/ 下的代码质量"}]}}
{"type":"agent_message_chunk","timestamp":"2026-04-27T10:30:01.100Z","data":{"content":{"type":"text","text":"我来检查 src/ 目录下的代码质量。"}}}
{"type":"tool_call","timestamp":"2026-04-27T10:30:01.500Z","data":{"toolCallId":"call_abc123","title":"Searching files: src/**/*.py","kind":"search","status":"pending","input":{"pattern":"src/**/*.py"}}}
{"type":"tool_call_update","timestamp":"2026-04-27T10:30:01.520Z","data":{"toolCallId":"call_abc123","status":"in_progress"}}
{"type":"tool_call_update","timestamp":"2026-04-27T10:30:01.600Z","data":{"toolCallId":"call_abc123","status":"completed","content":[{"type":"content","content":{"type":"text","text":"12 files"}}],"result":"src/main.py\nsrc/utils.py\n...","duration_ms":80}}
{"type":"tool_call","timestamp":"2026-04-27T10:30:02.100Z","data":{"toolCallId":"call_def456","title":"Reading src/main.py","kind":"read","status":"pending","input":{"file_path":"src/main.py"}}}
{"type":"tool_call_update","timestamp":"2026-04-27T10:30:02.120Z","data":{"toolCallId":"call_def456","status":"in_progress"}}
{"type":"tool_call_update","timestamp":"2026-04-27T10:30:02.200Z","data":{"toolCallId":"call_def456","status":"completed","content":[{"type":"content","content":{"type":"text","text":"85 lines"}}],"result":"1: import os\n2: ...","duration_ms":80}}
{"type":"agent_message_chunk","timestamp":"2026-04-27T10:30:05.300Z","data":{"content":{"type":"text","text":"代码质量整体良好，有几点建议：\n1. src/main.py 第42行缺少错误处理\n2. 建议在 config 模块添加类型注解"}}}
{"type":"session/result","timestamp":"2026-04-27T10:30:05.500Z","data":{"stopReason":"end_turn","turns":2,"toolCalls":2,"duration_ms":5500}}
```
