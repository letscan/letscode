"""Render a ConvertReport to a markdown analysis of CC-vs-letscode features.

The report documents what the importer mapped and — more interestingly — what
it had to drop, so the letscode feed format can grow informed by real CC data.
"""

from .cc import ConvertReport


def render_report_md(report: ConvertReport) -> str:
    """Render the converter's findings as a markdown document."""
    lines: list[str] = []
    lines.append("# Claude Code → letscode 导入分析报告")
    lines.append("")
    lines.append(f"**源文件**: `{report.cc_path}`")
    lines.append("")

    # ---- Overview ----
    lines.append("## 概览")
    lines.append("")
    lines.append(f"- 源文件行数(记录数): **{report.total_lines}**")
    lines.append(f"- 转换后 letscode 事件数: **{report.converted_events}**")
    kept = (report.user_prompts + report.compact_continuations
            + report.agent_text_blocks + report.tool_use_blocks + report.tool_results)
    lines.append(f"- 已映射(对话+工具): **{kept}**")
    skipped = sum(report.skipped_types.values()) + report.skipped_user_markers \
        + report.thinking_blocks_dropped + report.image_blocks_dropped \
        + report.sidechain_entries
    lines.append(f"- 已丢弃(噪声/不支持): **{skipped}**")
    lines.append("")

    # ---- Mapped features ----
    lines.append("## 已映射的 CC 特性")
    lines.append("")
    lines.append("| CC 记录 | letscode 事件 | 数量 |")
    lines.append("|---|---|---|")
    lines.append(f"| 真实 user prompt(string content) | `prompt` | {report.user_prompts} |")
    lines.append(f"| compact 续接摘要 | `user_message_chunk` | {report.compact_continuations} |")
    lines.append(f"| assistant text block | `agent_message_chunk` | {report.agent_text_blocks} |")
    lines.append(f"| assistant tool_use | `tool_call` | {report.tool_use_blocks} |")
    lines.append(f"| user tool_result | `tool_call_update(completed/failed)` | {report.tool_results} |")
    lines.append("")
    lines.append("**配对正确性**:`tool_use.id` ↔ `tool_result.tool_use_id`。"
                 f"本次转换遇到 {report.orphan_tool_results} 个孤儿 tool_result"
                 "(找不到对应 tool_use,已跳过)。")
    lines.append("")
    if report.is_error_results:
        lines.append(f"其中 **{report.is_error_results}** 个 tool_result 标记为 `is_error`,"
                     "转换为 `status=failed` 并包成 `<error>...</error>`。")
        lines.append("")

    # ---- Skipped features (the interesting part) ----
    lines.append("## letscode 暂不支持的 CC 特性(已丢弃)")
    lines.append("")
    lines.append("这些是 CC jsonl 里出现、但 letscode feed 模型尚无对应表示的特性。"
                 "记录在此作为 letscode feed 格式演进的参考。")
    lines.append("")

    lines.append("### 1. assistant thinking blocks")
    lines.append(f"- 丢弃数量: **{report.thinking_blocks_dropped}**")
    lines.append("- 原因:letscode 的 `agent_thought_chunk` 是**流式显示专用**,不写入 feed、"
                 "不进入回放历史(见 `events.py` 的注释:thoughts are display-only)。"
                 "CC 的 thinking 是持久化的一等公民。")
    lines.append("- 演进建议:若 letscode 要保留推理历史,需新增持久化 thought 事件类型。")
    lines.append("")

    lines.append("### 2. slash command 痕迹")
    lines.append(f"- 丢弃数量: **{report.skipped_user_markers}**")
    lines.append("- 形式:user message content 以 `<command-name>`/`<local-command-*>` 开头")
    lines.append("- 原因:letscode 的 slash command(`/compact`、`/new` 等)在 **ACP 层处理**,"
                 "直接改写 feed 文件,不产生命令痕迹事件。")
    lines.append("- 影响:导入的会话会丢失\"用户曾执行过 /clear、/model\"等元信息。")
    lines.append("")

    if report.skipped_types:
        lines.append("### 3. 顶层 type 被整体跳过的记录")
        lines.append("")
        lines.append("| type | 数量 | 说明 |")
        lines.append("|---|---|---|")
        type_notes = {
            "system": "CC 的 system 消息(local_command stdout、turn_duration 计时等);letscode 无 system 事件",
            "attachment": "CC 的 attachment(skill_listing、auto_mode、plan 等);letscode 用 system prompt 注入,不入 feed",
            "mode": "CC 的 mode 切换记录;letscode 用 preset/sandbox,粒度不同",
            "permission-mode": "CC 的权限模式切换;letscode 无对应",
            "ai-title": "CC 自动生成的会话标题;letscode 标题走 gen-title,单独存储",
            "last-prompt": "CC 记录最后一条 prompt;letscode 无对应",
            "agent-name": "CC 的 agent 命名;letscode Agent 是无状态黑盒进程",
            "file-history-snapshot": "CC 的文件历史快照;letscode 无对应",
            "queue-operation": "CC 的队列操作;letscode 无对应",
        }
        for t, n in sorted(report.skipped_types.items(), key=lambda x: -x[1]):
            note = type_notes.get(t, "letscode 无对应事件类型")
            lines.append(f"| `{t}` | {n} | {note} |")
        lines.append("")

    if report.image_blocks_dropped:
        lines.append("### 4. 图片/文档 block")
        lines.append(f"- 丢弃数量: **{report.image_blocks_dropped}**")
        lines.append("- 原因:CC 的 image/document block 携带 base64 内联数据;"
                     "letscode 的图片走 `image_ref`(路径引用)协议。导入未做转换。")
        lines.append("")

    if report.sidechain_entries:
        lines.append("### 5. isSidechain 记录")
        lines.append(f"- 丢弃数量: **{report.sidechain_entries}**")
        lines.append("- 说明:主会话文件通常不含 sidechain 记录(子 agent transcript 单独存"
                     "`<session>/subagents/agent-*.jsonl`)。此处计数若非 0,说明主文件混入了"
                     "子 agent 内容,已被跳过。")
        lines.append("")

    # ---- Subagent handling ----
    lines.append("### 6. Agent subagent 内部 transcript(未展开)")
    lines.append(f"- Agent 工具调用数: **{report.agent_tool_calls}**")
    lines.append("- 处理方式:**扁平化**。Agent 的 tool_result 已内联子 agent 的最终答案文本,"
                 "直接作为 `tool_call_update(completed)` 保留。子 agent 内部的 Read/Bash/Grep "
                 "等工具调用(在 `<session>/subagents/agent-*.jsonl`)未递归展开。")
    lines.append("- 理由:letscode 的 Agent 工具本身就是黑盒 subprocess,扁平化贴合 letscode 语义。"
                 "如需子 agent 内部细节,可后续扩展递归导入。")
    lines.append("")

    # ---- Format differences observed ----
    lines.append("## 观察到的格式差异")
    lines.append("")
    lines.append("### tool_result.content 形态")
    lines.append(f"- bare string: **{report.tool_result_string_content}**")
    lines.append(f"- block list [type:text]: **{report.tool_result_list_content}**")
    lines.append("- letscode 统一为 string(`tool_call_update.rawOutput`)。"
                 "导入时把 list 形态的 text block 拼接成单个字符串。")
    lines.append("")
    lines.append("### 字段命名约定")
    lines.append("- CC: `snake_case`(`tool_use_id`、`is_error`、`parent_uuid`)")
    lines.append("- letscode: `camelCase`(`toolCallId`、`rawInput`、`rawOutput`)")
    lines.append("- 这反映了两个项目的事件 schema 来源不同(CC 的 schema 服务于自家 UI/SDK,"
                 "letscode 的 schema 对齐 ACP 规范)。")
    lines.append("")

    if report.skipped_user_subtypes or report.skipped_assistant_subtypes:
        lines.append("### 其它未识别的 content block 类型")
        lines.append("")
        for label, counter in (("user", report.skipped_user_subtypes),
                               ("assistant", report.skipped_assistant_subtypes)):
            if counter:
                items = ", ".join(f"`{k}`×{v}" for k, v in counter.items())
                lines.append(f"- {label}: {items}")
        lines.append("")

    lines.append("---")
    lines.append("*本报告由 `letscode import-cc` 自动生成。*")
    return "\n".join(lines)
