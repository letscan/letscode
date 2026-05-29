"""ACP server: SDK-based implementation using agent-client-protocol."""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import acp.helpers as h
from acp import PROTOCOL_VERSION, Client, run_agent
from acp.agent.connection import AgentSideConnection
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    ConfigOptionUpdate,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpCapabilities,
    ModelInfo,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    SessionCapabilities,
    SessionConfigSelectOption,
    SessionConfigOptionSelect,
    SessionInfo,
    SessionMode,
    SessionModeState,
    SessionModelState,
    ToolCallLocation,
)

from .. import __version__
from .session import Session, create_session, list_sessions, load_session_meta, save_session

logger = logging.getLogger("letscode-acp")

_DEFAULT_MAX_TURNS = 30


def _get_modes() -> list[dict]:
    from ..sandbox import list_presets
    return list_presets()


class LetscodeAgent:
    """ACP agent implementation backed by the letscode CLI subprocess."""

    def __init__(self, config_path: str | None = None):
        self.config_path = config_path
        self._conn: AgentSideConnection | None = None
        self.sessions: dict[str, Session] = {}
        self._agent_proc: asyncio.subprocess.Process | None = None
        self._current_session_id: str | None = None
        self._cancelled = False
        self._models: list[dict] = []
        self._default_model: str | None = None
        self._load_models()

    def _load_models(self) -> None:
        from ..config import list_models
        try:
            self._models, self._default_model = list_models(self.config_path)
            logger.info("Loaded %d models, default=%s", len(self._models), self._default_model)
        except Exception as e:
            logger.warning("Failed to load models: %s", e)

    def on_connect(self, conn: Client) -> None:
        self._conn = conn
        logger.info("Client connected")

    def _build_config_options(self, session: Session) -> list[SessionConfigOptionSelect]:
        options: list[SessionConfigOptionSelect] = [
            SessionConfigOptionSelect(
                id="mode",
                name="Session Mode",
                category="mode",
                type="select",
                current_value=session.mode,
                options=[
                    SessionConfigSelectOption(value=m["id"], name=m["name"], description=m["description"])
                    for m in _get_modes()
                ],
            ),
        ]
        if self._models:
            options.append(
                SessionConfigOptionSelect(
                    id="model",
                    name="Model",
                    category="model",
                    type="select",
                    current_value=session.model or self._default_model or "",
                    options=[
                        SessionConfigSelectOption(value=m["model"], name=m["model"])
                        for m in self._models
                    ],
                )
            )
        return options

    def _build_modes(self, session: Session) -> SessionModeState:
        return SessionModeState(
            current_mode_id=session.mode,
            available_modes=[
                SessionMode(id=m["id"], name=m["name"], description=m["description"])
                for m in _get_modes()
            ],
        )

    def _build_models_state(self, session: Session) -> SessionModelState | None:
        if not self._models:
            return None
        return SessionModelState(
            current_model_id=session.model or self._default_model or "",
            available_models=[
                ModelInfo(model_id=m["model"], name=m["model"])
                for m in self._models
            ],
        )

    # ── Agent protocol methods ──

    async def initialize(self, protocol_version: int, **kwargs: Any) -> InitializeResponse:
        logger.info("initialize(protocol_version=%d)", protocol_version)
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                prompt_capabilities=PromptCapabilities(embedded_context=False, image=True),
                mcp_capabilities=McpCapabilities(http=True, sse=True),
                load_session=True,
                session_capabilities=SessionCapabilities(close={}, list={}),
            ),
            agent_info=Implementation(name="letscode-acp", version=__version__, title="letscode"),
            auth_methods=[],
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> NewSessionResponse:
        logger.info("new_session(cwd=%s)", cwd)
        session = create_session(cwd)
        session.model = self._default_model
        save_session(session)
        self.sessions[session.session_id] = session

        return NewSessionResponse(
            session_id=session.session_id,
            config_options=self._build_config_options(session),
            modes=self._build_modes(session),
            models=self._build_models_state(session),
        )

    async def prompt(
        self,
        prompt: list,
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> PromptResponse:
        session = self.sessions.get(session_id)
        if session is None:
            return PromptResponse(stop_reason="end_turn")

        from pydantic import BaseModel
        serialized_blocks = [
            b.model_dump(mode="json", exclude_none=True) if isinstance(b, BaseModel) else b
            for b in prompt
        ]

        logger.info("prompt(session=%s, %d blocks)", session_id[:12], len(prompt))

        if session.title is None:
            title = next((b.get("text", "") for b in serialized_blocks if b.get("type") == "text"), "")
            session.title = title[:120] if title else None

        cmd = [sys.executable, "-m", "letscode", "--event-stream", "--no-mcp"]
        if self.config_path:
            cmd.extend(["--config", self.config_path])
        if session.model:
            cmd.extend(["--model", session.model])
        if session.mode != "default":
            cmd.extend(["--preset", session.mode])
        cmd.extend(["--max-turns", str(_DEFAULT_MAX_TURNS)])
        cmd.append("--prompt-format")
        cmd.append("json")

        from .session import _sessions_dir
        log_path = _sessions_dir(session.cwd) / f"{session_id}.jsonl"

        cmd.extend(["--feed", str(log_path), "--append"])
        cmd.append(json.dumps(serialized_blocks, ensure_ascii=False))

        session.log_path = str(log_path)
        save_session(session)

        logger.info("Spawning letscode for session %s: %s", session_id[:12], " ".join(cmd))
        self._cancelled = False
        self._current_session_id = session_id

        cwd = session.cwd if os.path.isdir(session.cwd) else os.getcwd()

        exit_code: int | None = None
        try:
            self._agent_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=cwd,
            )
            logger.info("Subprocess PID=%d started", self._agent_proc.pid)

            stop_reason = "end_turn"
            error_msg: str | None = None
            pending_tool_inputs: dict[str, dict] = {}

            async for line in self._agent_proc.stdout:
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    continue

                if event.get("type") == "session/result":
                    stop_reason = event.get("data", {}).get("stopReason", "end_turn")
                    continue
                if event.get("type") == "session/prompt":
                    continue
                if event.get("type") == "error":
                    error_msg = event.get("data", {}).get("message", "unknown error")
                    continue

                update = _translate_event(event, pending_tool_inputs)
                if update is not None and self._conn is not None:
                    if isinstance(update, list):
                        for u in update:
                            await self._conn.session_update(session_id=session_id, update=u)
                    else:
                        await self._conn.session_update(session_id=session_id, update=update)

            await self._agent_proc.wait()
            exit_code = self._agent_proc.returncode
            logger.info("Subprocess exited rc=%d, stop_reason=%s", exit_code, stop_reason)

            if self._cancelled:
                stop_reason = "cancelled"

        except RequestError:
            raise
        except Exception:
            logger.exception("Agent subprocess error")
            raise RequestError.internal_error({"details": "Agent subprocess failed"})
        finally:
            self._agent_proc = None
            self._current_session_id = None

        if error_msg:
            raise RequestError.internal_error({"details": error_msg})

        if exit_code:
            raise RequestError.internal_error({"details": f"Agent exited with code {exit_code}"})

        return PromptResponse(stop_reason=stop_reason)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        if self._agent_proc is not None and session_id == self._current_session_id:
            self._cancelled = True
            logger.info("Cancelling session %s", session_id[:12])
            try:
                self._agent_proc.kill()
            except ProcessLookupError:
                pass

    async def load_session(self, cwd: str, session_id: str, **kwargs: Any) -> LoadSessionResponse | None:
        logger.info("load_session(session=%s, cwd=%s)", session_id[:12], cwd)
        session = load_session_meta(session_id, cwd)
        if session is None:
            return None

        if not session.log_path or not Path(session.log_path).exists():
            return None

        logger.info("Loading session %s from %s", session_id[:12], session.log_path)

        pending_tool_inputs: dict[str, dict] = {}
        with open(session.log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                update = _translate_event(event, pending_tool_inputs)
                if update is not None and self._conn is not None:
                    if isinstance(update, list):
                        for u in update:
                            await self._conn.session_update(session_id=session_id, update=u)
                    else:
                        await self._conn.session_update(session_id=session_id, update=update)

        self.sessions[session_id] = session

        return LoadSessionResponse(
            config_options=self._build_config_options(session),
            modes=self._build_modes(session),
            models=self._build_models_state(session),
        )

    async def list_sessions(self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any) -> ListSessionsResponse:
        logger.debug("list_sessions(cwd=%s, cursor=%s)", cwd, cursor)
        sessions, next_cursor = list_sessions(cwd, cursor)
        return ListSessionsResponse(
            sessions=[
                SessionInfo(
                    session_id=s.session_id,
                    cwd=s.cwd,
                    updated_at=s.created_at,
                    title=s.title,
                )
                for s in sessions
            ],
            next_cursor=next_cursor,
        )

    async def set_session_mode(self, mode_id: str, session_id: str, **kwargs: Any):
        logger.info("set_session_mode(session=%s, mode=%s)", session_id[:12], mode_id)
        session = self.sessions.get(session_id)
        if session is None:
            return None
        session.mode = mode_id
        save_session(session)
        if self._conn is not None:
            await self._conn.session_update(
                session_id=session_id,
                update=h.update_current_mode(mode_id),
            )
        return {}

    async def set_config_option(self, config_id: str, session_id: str, value: str | bool, **kwargs: Any):
        logger.info("set_config_option(session=%s, %s=%s)", session_id[:12], config_id, value)
        session = self.sessions.get(session_id)
        if session is None:
            return None

        if config_id == "mode":
            session.mode = str(value)
        elif config_id == "model":
            session.model = str(value)
        else:
            return None

        save_session(session)
        if self._conn is not None:
            await self._conn.session_update(
                session_id=session_id,
                update=ConfigOptionUpdate(
                    session_update="config_option_update",
                    config_options=self._build_config_options(session),
                ),
            )
        return {}

    async def close_session(self, session_id: str, **kwargs: Any):
        logger.info("close_session(session=%s)", session_id[:12])
        if self._agent_proc and session_id == self._current_session_id:
            try:
                self._agent_proc.kill()
            except ProcessLookupError:
                pass
        self.sessions.pop(session_id, None)
        return {}


# ── Event translation ──

def _translate_event(event: dict, pending_tool_inputs: dict[str, dict]) -> Any:
    """Translate a letscode JSONL event to an ACP SessionUpdate object."""
    type_ = event.get("type", "")
    data = event.get("data", {})

    if type_ == "agent_message_chunk":
        content = data.get("content", {})
        if content.get("type") == "text" and content.get("text"):
            return h.update_agent_message_text(content["text"] + "\n")
        return None

    if type_ == "session/prompt":
        prompt_blocks = data.get("prompt", [])
        if not prompt_blocks:
            return None
        updates = _blocks_to_user_messages(prompt_blocks)
        return updates if updates else None

    if type_ == "tool_call":
        tc_id = data.get("toolCallId", "")
        inp = data.get("input", {})
        pending_tool_inputs[tc_id] = inp
        title = data.get("title", "")

        if "command" in inp:
            desc = inp.get("description", title)
            title = f"正在执行: {desc}"

        locations = None
        file_path = inp.get("file_path", "")
        if file_path:
            locations = [ToolCallLocation(path=os.path.abspath(file_path))]

        raw_input: Any = inp
        if "command" in inp:
            raw_input = f"```\n{inp['command']}\n```"

        return h.start_tool_call(
            tool_call_id=tc_id,
            title=title,
            kind=data.get("kind", "other"),
            status=data.get("status", "pending"),
            raw_input=raw_input,
            locations=locations,
        )

    if type_ == "tool_call_update":
        tc_id = data.get("toolCallId", "")
        status = data.get("status", "")
        inp = pending_tool_inputs.get(tc_id, {})

        title = None
        if inp and "command" in inp:
            desc = inp.get("description", "")
            if status == "completed":
                title = f"已执行: {desc}"
            elif status == "failed":
                title = f"执行失败: {desc}"

        content = None
        if status in ("completed", "failed"):
            content = _build_completed_content(tc_id, data, pending_tool_inputs)
            if content is None and "content" in data:
                content = [h.tool_content(h.text_block(data["content"]))]
            pending_tool_inputs.pop(tc_id, None)
        elif "content" in data:
            content = [h.tool_content(h.text_block(data["content"]))]

        return h.update_tool_call(
            tool_call_id=tc_id,
            title=title,
            status=status,
            content=content,
        )

    return None


def _build_completed_content(
    tc_id: str, data: dict, pending_tool_inputs: dict[str, dict],
) -> list | None:
    """Build ACP content for tool completions: diff, resource_link, or text."""
    inp = pending_tool_inputs.get(tc_id)
    if not inp:
        return None

    tool_name = data.get("toolName", "")
    file_path = inp.get("file_path", "")

    if tool_name == "Read" and file_path:
        abs_path = os.path.abspath(file_path)
        return [h.tool_content(h.resource_link_block(
            name=os.path.basename(file_path),
            uri=f"file://{abs_path}",
        ))]

    if tool_name == "Edit" and file_path:
        abs_path = os.path.abspath(file_path)
        return [h.tool_diff_content(
            path=abs_path,
            old_text=inp.get("old_string", ""),
            new_text=inp.get("new_string", ""),
        )]

    if tool_name == "Write" and file_path:
        abs_path = os.path.abspath(file_path)
        old_text: str | None = None
        try:
            old_text = open(file_path, "r", encoding="utf-8").read()
        except (FileNotFoundError, OSError):
            pass
        return [h.tool_diff_content(
            path=abs_path,
            new_text=inp.get("content", ""),
            old_text=old_text,
        )]

    if tool_name == "Bash":
        result_text = data.get("result", "")
        if not result_text and "result_file" in data:
            try:
                result_text = Path(data["result_file"]).read_text(encoding="utf-8")
            except (FileNotFoundError, OSError):
                result_text = data.get("result_summary", "")
        if result_text:
            return [h.tool_content(h.text_block(f"```\n{result_text}\n```"))]

    return None


def _blocks_to_user_messages(blocks: list[dict]) -> list[Any] | None:
    """Convert serialized content blocks to a list of UserMessageChunk."""

    def _to_update(b: dict):
        t = b.get("type")
        if t == "text":
            return h.update_user_message(h.text_block(b.get("text", "")))
        if t == "resource_link":
            return h.update_user_message(h.resource_link_block(
                name=b.get("name", ""),
                uri=b.get("uri", ""),
            ))
        if t == "resource":
            res = b.get("resource", {})
            text = res.get("text", res.get("uri", ""))
            return h.update_user_message(h.text_block(text))
        if t == "image":
            mime = b.get("mime_type")
            if not mime:
                return None
            return h.update_user_message(h.image_block(b.get("data", ""), mime_type=mime, uri=b.get("uri")))
        if "text" in b:
            return h.update_user_message(h.text_block(b["text"]))
        return None

    updates = [_to_update(b) for b in blocks]
    updates = [u for u in updates if u is not None]
    return updates if updates else None
