"""ACP server: SDK-based implementation using agent-client-protocol."""

import asyncio
import json
import logging
import os
import sys
import time
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
    Usage,
    UsageUpdate,
)

from .. import __version__
from ..feed_util import split_turns, write_events
from .commands import SlashCommandRegistry, create_builtin_registry, parse_slash_command, register_skills
from .session import Session, create_session, list_sessions, load_session_meta, save_session

logger = logging.getLogger("letscode-acp")

_DEFAULT_MAX_TURNS = 30


def _human_tokens(n: int) -> str:
    """Format a token count compactly, e.g. 2700 -> '2.7k'."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _human_duration(seconds: float) -> str:
    """Format an elapsed duration, e.g. 76s -> '1m16s', 45s -> '45s'."""
    s = int(round(seconds))
    if s >= 60:
        m, s = divmod(s, 60)
        return f"{m}m{s}s"
    return f"{s}s"


def _format_stat_quote(big_turn: int, tokens: int, elapsed: float) -> str:
    """Build the per-turn stat summary as a markdown blockquote line.

    Example: '> Turn 3 | 2.7k tokens | 1m16s'
    """
    return f"\n> Turn {big_turn} | {_human_tokens(tokens)} tokens | {_human_duration(elapsed)}\n"


def _make_replay_stat_quote(data: dict, prev_tokens: int, prev_turn: int) -> str | None:
    """Build a stat footer from a replayed ``result`` event's data.

    Mirrors :func:`_format_stat_quote` but takes the turn's usage + duration
    from the logged event. ``tokens`` is the delta vs the previous turn's
    cumulative ``prompt_tokens`` (matching the live path), floored at 0.
    """
    usage = data.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens", 0)
    delta = max(prompt_tokens - prev_tokens, 0)
    duration_ms = data.get("duration_ms") or 0
    elapsed = duration_ms / 1000.0
    # A result event may lack usage (e.g. error turns); skip those.
    if not prompt_tokens and not duration_ms:
        return None
    return _format_stat_quote(prev_turn + 1, delta, elapsed)


def _get_modes() -> list[dict]:
    from ..sandbox import list_presets
    return list_presets()


class LetscodeAgent:
    """ACP agent implementation backed by the letscode CLI subprocess."""

    def __init__(self, config_path: str | None = None, *, show_stat: bool = False):
        self.config_path = config_path
        self.show_stat = show_stat
        self._conn: AgentSideConnection | None = None
        self.sessions: dict[str, Session] = {}
        self._agent_proc: asyncio.subprocess.Process | None = None
        self._current_session_id: str | None = None
        self._cancelled = False
        self._models: list[dict] = []
        self._default_model: str | None = None
        self._session_commands: dict[str, SlashCommandRegistry] = {}
        # Per-session cumulative prompt_tokens (last big turn) for stat deltas.
        self._session_prompt_tokens: dict[str, int] = {}
        # Per-session big-turn counter (each prompt() call = one big turn).
        self._session_big_turn: dict[str, int] = {}
        # Per-session context window size (captured from the init event).
        self._session_context_window: dict[str, int] = {}
        # Per-session in-flight title-generation tasks (so we don't double-fire).
        self._session_title_task: dict[str, asyncio.Task] = {}
        self._load_models()

    def _load_models(self) -> None:
        from ..config import list_models
        try:
            self._models, self._default_model = list_models(self.config_path)
            logger.info("Loaded %d models, default=%s", len(self._models), self._default_model)
        except Exception as e:
            logger.warning("Failed to load models: %s", e)

    def _model_context_window(self, model_id: str | None) -> int | None:
        """Look up a model's context_window from the loaded config entries."""
        target = model_id or self._default_model
        if not target:
            return None
        for m in self._models:
            if m.get("model") == target:
                cw = m.get("context_window")
                return cw if isinstance(cw, int) and cw > 0 else None
        return None

    def _build_commands(self, cwd: str) -> SlashCommandRegistry:
        """Create a per-session command registry with builtins + cwd-specific skills."""
        registry = create_builtin_registry()
        try:
            register_skills(registry, cwd)
        except Exception:
            logger.debug("Skill discovery failed for cwd=%s", cwd, exc_info=True)
        return registry

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

    async def _deferred_send_commands(self, session_id: str) -> None:
        """Send available_commands_update after a delay so the response is sent first."""
        await asyncio.sleep(0.2)
        registry = self._session_commands.get(session_id)
        if registry is None or self._conn is None:
            return
        try:
            await self._conn.session_update(
                session_id=session_id,
                update=registry.to_acp_update(),
            )
        except Exception:
            logger.debug("Failed to send commands update for session %s", session_id[:12], exc_info=True)

    async def _deferred_send_usage(self, session_id: str) -> None:
        """Send an initial usage_update so the UI gauge shows on session start.

        On new sessions used=0; on loaded sessions it reflects the last turn's
        cumulative prompt_tokens (the current context fill), recovered from the
        session log by load_session.
        """
        await asyncio.sleep(0.2)
        size = self._session_context_window.get(session_id)
        if not size or self._conn is None:
            return
        used = self._session_prompt_tokens.get(session_id, 0)
        try:
            await self._conn.session_update(
                session_id=session_id,
                update=UsageUpdate(session_update="usage_update", used=used, size=size),
            )
        except Exception:
            logger.debug("Failed to send initial usage for session %s", session_id[:12], exc_info=True)

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
        self._session_commands[session.session_id] = self._build_commands(cwd)
        # Seed context window from config so the initial usage_update can fire
        # before any letscode subprocess runs.
        cw = self._model_context_window(session.model)
        if cw:
            self._session_context_window[session.session_id] = cw
        asyncio.create_task(self._deferred_send_commands(session.session_id))
        # usage_update is emitted via set_config_option(model), which the
        # client always sends right after new_session — no need to send here.

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

        # ── Slash command handling ──
        registry = self._session_commands.get(session_id)
        cmd_name, cmd_args = parse_slash_command(serialized_blocks)
        if cmd_name == "rename":
            # Echo the command first so the UI shows it immediately, even while
            # the no-arg form awaits the LLM for title generation.
            if self._conn is not None:
                await self._conn.session_update(
                    session_id=session_id,
                    update=h.update_user_message(
                        h.text_block(f"/rename{(' ' + cmd_args) if cmd_args else ''}")
                    ),
                )
            # /rename is dispatched here (not via the registry): it needs the
            # connection to push a session_info_update, and the no-arg form runs
            # async gen-title. Returns immediately like other slash commands.
            await self._handle_rename(session_id, cmd_args)
            return PromptResponse(stop_reason="end_turn")

        if cmd_name is not None and registry is not None:
            cmd = registry.get(cmd_name)
            if cmd is not None and cmd.handler is not None:
                # Built-in command: dispatch and return
                from ..config import load_config
                config, _ = load_config(self.config_path, session.model)
                result = registry.dispatch(cmd_name, session, cmd_args, config=config)
                if self._conn is not None:
                    await self._conn.session_update(
                        session_id=session_id,
                        update=h.update_user_message(h.text_block(f"/{cmd_name}")),
                    )
                    await self._conn.session_update(
                        session_id=session_id,
                        update=h.update_agent_message_text(result.message + "\n"),
                    )
                return PromptResponse(stop_reason="end_turn")

            if cmd is not None and cmd.is_skill:
                # Skill command: expand template and run as agent prompt
                from ..tools.skill import execute as skill_execute
                expanded = skill_execute({"skill": cmd_name, "args": cmd_args})
                if self._conn is not None:
                    await self._conn.session_update(
                        session_id=session_id,
                        update=h.update_user_message(h.text_block(f"/{cmd_name}")),
                    )
                serialized_blocks = [{"type": "text", "text": expanded}]

        was_first_prompt = session.title is None
        if session.title is None:
            title = next((b.get("text", "") for b in serialized_blocks if b.get("type") == "text"), "")
            session.title = title[:120] if title else None

        # Kick off title generation early (before the agent subprocess starts),
        # in parallel with the agent run. Title uses only user prompts — no need
        # to wait for the agent's reply.
        if was_first_prompt and not self._session_title_task.get(session_id):
            current_text = next(
                (b.get("text", "") for b in serialized_blocks if b.get("type") == "text"), ""
            )
            self._session_title_task[session_id] = asyncio.create_task(
                self._generate_and_set_title(session_id, current_text)
            )

        # Spill inline image blocks to local files so they travel to the
        # subprocess as short path refs (image_ref) instead of huge base64
        # blobs in argv (which would blow past ARG_MAX for real screenshots).
        # The CLI reads them back into inline image_url parts when building
        # the OpenAI message, so vision models see the image unchanged.
        cli_blocks = _spill_image_blocks(serialized_blocks, session.cwd)

        cmd = [sys.executable, "-m", "letscode", "--event-stream", "--no-mcp"]
        if self.config_path:
            cmd.extend(["--config", self.config_path])
        if session.model:
            cmd.extend(["--model", session.model])
        if session.mode != "default":
            cmd.extend(["--preset", session.mode])
        cmd.extend(["--max-turns", str(_DEFAULT_MAX_TURNS)])

        from .session import _sessions_dir
        log_path = _sessions_dir(session.cwd) / f"{session_id}.jsonl"

        cmd.extend(["--feed", str(log_path), "--append"])

        # Translate blocks to --text/--image argv tokens, preserving order.
        for b in cli_blocks:
            t = b.get("type")
            if t == "text" and b.get("text") is not None:
                cmd.extend(["--text", b["text"]])
            elif t == "image_ref" and b.get("path"):
                cmd.extend(["--image", b["path"]])

        session.log_path = str(log_path)
        save_session(session)

        logger.info("Spawning letscode for session %s: %s", session_id[:12], " ".join(cmd))
        self._cancelled = False
        self._current_session_id = session_id

        cwd = session.cwd if os.path.isdir(session.cwd) else os.getcwd()

        exit_code: int | None = None
        start_time = time.monotonic()
        context_window = self._session_context_window.get(session_id)
        turn_prompt_tokens = 0
        try:
            self._agent_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            logger.info("Subprocess PID=%d started", self._agent_proc.pid)

            stop_reason = "end_turn"
            usage: Usage | None = None
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

                if event.get("type") == "init":
                    cw = event.get("data", {}).get("contextWindow")
                    if isinstance(cw, int) and cw > 0:
                        context_window = cw
                        self._session_context_window[session_id] = cw
                    continue
                if event.get("type") in ("session/prompt", "prompt"):
                    continue
                if event.get("type") in ("result", "session/result"):
                    result_data = event.get("data", {})
                    stop_reason = result_data.get("stopReason", "end_turn")
                    if result_data.get("usage"):
                        usage_data = result_data["usage"]
                        turn_prompt_tokens = usage_data.get("prompt_tokens", 0)
                        usage = Usage(
                            input_tokens=usage_data.get("prompt_tokens", 0),
                            output_tokens=usage_data.get("completion_tokens", 0),
                            total_tokens=usage_data.get("total_tokens", 0),
                            cached_read_tokens=usage_data.get("cache_read_tokens") or None,
                            cached_write_tokens=usage_data.get("cache_write_tokens") or None,
                            thought_tokens=usage_data.get("reasoning_tokens") or None,
                        )
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
        except Exception as exc:
            import traceback
            tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
            logger.exception("Agent subprocess error")
            raise RequestError.internal_error({"details": f"Agent subprocess error: {exc}\n{''.join(tb)}"}) from exc
        finally:
            if self._agent_proc is not None:
                try:
                    self._agent_proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await self._agent_proc.wait()
                except Exception:
                    pass
            self._agent_proc = None
            self._current_session_id = None

        # A user-initiated cancel SIGKILLs the subprocess (exit -9); that's the
        # expected outcome, not an error. Skip the error/exit-code checks so the
        # prompt() returns cleanly with stop_reason="cancelled".
        if self._cancelled:
            return PromptResponse(stop_reason="cancelled")

        if error_msg:
            raise RequestError.internal_error({"details": error_msg})

        if exit_code:
            raise RequestError.internal_error({"details": f"Agent exited with code {exit_code}"})

        elapsed = time.monotonic() - start_time

        # Context-window usage update (drives a fill gauge in the client UI).
        if context_window and self._conn is not None and turn_prompt_tokens:
            try:
                await self._conn.session_update(
                    session_id=session_id,
                    update=UsageUpdate(
                        session_update="usage_update",
                        used=turn_prompt_tokens, size=context_window,
                    ),
                )
            except Exception:
                logger.warning("Failed to emit usage update", exc_info=True)

        # Per-turn stat quote appended as an agent message (markdown blockquote).
        if self.show_stat and self._conn is not None:
            prev = self._session_prompt_tokens.get(session_id, 0)
            delta = max(turn_prompt_tokens - prev, 0)
            self._session_prompt_tokens[session_id] = turn_prompt_tokens
            big_turn = self._session_big_turn.get(session_id, 0) + 1
            self._session_big_turn[session_id] = big_turn
            quote = _format_stat_quote(big_turn, delta, elapsed)
            try:
                await self._conn.session_update(
                    session_id=session_id,
                    update=h.update_agent_message_text(quote),
                )
            except Exception:
                logger.warning("Failed to emit stat quote", exc_info=True)

        return PromptResponse(stop_reason=stop_reason, usage=usage)

    async def _handle_rename(self, session_id: str, args: str | None) -> None:
        """Handle /rename: set the title directly, or generate it when no arg.

        Pushes a session_info_update so the client's UI updates immediately.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return
        title = (args or "").strip()
        if title:
            # Direct rename: use the given title.
            self._apply_title(session_id, title)
        else:
            # No arg: generate a title from the user prompts in the session.
            from ..feed_util import extract_user_prompts, read_events
            prompts: list[str] = []
            if session.log_path and Path(session.log_path).exists():
                prompts = extract_user_prompts(read_events(session.log_path))
            from .titles import generate_title
            gen = await generate_title(
                prompts,
                model_id=session.model, config_path=self.config_path,
            )
            if gen:
                self._apply_title(session_id, gen)

    def _apply_title(self, session_id: str, title: str) -> None:
        """Set + persist the title, and push a session_info_update to the client."""
        session = self.sessions.get(session_id)
        if session is None:
            return
        session.title = title
        save_session(session)
        logger.info("Renamed session %s: %s", session_id[:12], title)
        if self._conn is not None:
            try:
                from acp.schema import SessionInfoUpdate
                asyncio.create_task(self._conn.session_update(
                    session_id=session_id,
                    update=SessionInfoUpdate(
                        session_update="session_info_update", title=title,
                    ),
                ))
            except Exception:
                logger.debug("Failed to push title update to client", exc_info=True)

    async def _generate_and_set_title(
        self, session_id: str, current_prompt: str,
    ) -> None:
        """Async title generation from user prompts. Fire-and-forget.

        Uses the current prompt plus any prior user prompts from the session
        log. No agent replies — the questions a user asks are the best signal
        for what the session is about.
        """
        from .titles import generate_title
        from ..feed_util import extract_user_prompts, read_events
        session = self.sessions.get(session_id)
        if session is None:
            return
        # Gather prior user prompts from the log (the current prompt hasn't
        # been written yet at auto-trigger time).
        prior_prompts: list[str] = []
        if session.log_path and Path(session.log_path).exists():
            prior_prompts = extract_user_prompts(read_events(session.log_path))
        all_prompts = prior_prompts + ([current_prompt] if current_prompt.strip() else [])
        title = await generate_title(
            all_prompts,
            model_id=session.model, config_path=self.config_path,
        )
        self._session_title_task.pop(session_id, None)
        if title:
            self._apply_title(session_id, title)

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
        events = await asyncio.to_thread(_read_log_events, session.log_path)

        # Summarize old turns for the UI if the session is long. The summary is
        # written to the log as a session/summary event (UI-only; the agent's
        # --feed replay skips it, so the agent still sees full history).
        from .summaries import (
            SUMMARY_TURN_THRESHOLD, SUMMARY_EVENT_TYPE,
            find_summary_event, summarize_old_turns,
        )
        summary_event = find_summary_event(events)
        if summary_event is None:
            turns = split_turns(events)
            if len(turns) > SUMMARY_TURN_THRESHOLD:
                summary_event = await summarize_old_turns(
                    events, SUMMARY_TURN_THRESHOLD,
                    model_id=session.model, config_path=self.config_path,
                )
                if summary_event is not None:
                    # Persist: insert the summary event before the last
                    # THRESHOLD turns so future loads reuse it.
                    keep_events = [ev for t in turns[-SUMMARY_TURN_THRESHOLD:] for ev in t]
                    early_events = [ev for t in turns[:-SUMMARY_TURN_THRESHOLD] for ev in t]
                    new_events = early_events + [summary_event] + keep_events
                    await asyncio.to_thread(
                        lambda: write_events(session.log_path, new_events),  # type: ignore[arg-type]
                    )
                    events = new_events

        # Decide which events to send to the UI. If a summary exists, emit it
        # as a thought block and only stream the events after it (the kept
        # recent turns). Otherwise send everything.
        if summary_event is not None:
            ui_events = [summary_event] + _events_after_summary(events, summary_event)
        else:
            ui_events = events

        for event in ui_events:
            # On a result event, emit the per-turn stat footer (same format as
            # the live path) so resumed sessions show the same "> Turn N | …"
            # quotes as a live session.
            if self.show_stat and event.get("type") in ("result", "session/result"):
                data = event.get("data", {})
                quote = _make_replay_stat_quote(
                    data,
                    self._session_prompt_tokens.get(session_id, 0),
                    self._session_big_turn.get(session_id, 0),
                )
                if quote is not None and self._conn is not None:
                    try:
                        await self._conn.session_update(
                            session_id=session_id,
                            update=h.update_agent_message_text(quote),
                        )
                    except Exception:
                        logger.debug("Failed to emit replay stat quote", exc_info=True)
                    # Advance the running counters so subsequent turns' deltas
                    # are correct.
                    usage = data.get("usage", {})
                    pt = usage.get("prompt_tokens", 0)
                    if pt:
                        self._session_prompt_tokens[session_id] = pt
                    self._session_big_turn[session_id] = \
                        self._session_big_turn.get(session_id, 0) + 1
                continue  # result events are never translated to UI updates

            update = _translate_event(event, pending_tool_inputs)
            if update is not None and self._conn is not None:
                if isinstance(update, list):
                    for u in update:
                        await self._conn.session_update(session_id=session_id, update=u)
                else:
                    await self._conn.session_update(session_id=session_id, update=update)

        self.sessions[session_id] = session
        self._session_commands[session_id] = self._build_commands(cwd)
        cw = self._model_context_window(session.model)
        if cw:
            self._session_context_window[session_id] = cw
        # Recover the last turn's cumulative prompt_tokens so the initial
        # usage_update reflects the current context fill (not 0).
        last_tokens = _last_prompt_tokens(events)
        if last_tokens:
            self._session_prompt_tokens[session_id] = last_tokens
        asyncio.create_task(self._deferred_send_commands(session_id))
        # usage_update is emitted via set_config_option(model), which the
        # client always sends right after load_session — no need to send here.

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
            # Context window may differ per model; refresh the cached size.
            cw = self._model_context_window(session.model)
            if cw:
                self._session_context_window[session_id] = cw
            # The client sends set_config_option(model) right after every
            # new/load session, so this is the single place to emit the
            # initial usage_update — covers both new (used=0) and loaded
            # (used=<last turn>) sessions.
            asyncio.create_task(self._deferred_send_usage(session_id))
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
        self._session_commands.pop(session_id, None)
        return {}


def _spill_image_blocks(blocks: list[dict], cwd: str) -> list[dict]:
    """Replace inline ``image`` blocks with on-disk ``image_ref`` blocks.

    The base64 payload is written to ``<cwd>/.letscode/images/<hash>.<ext>``
    (content-addressed, idempotent) and the block becomes a short path
    reference. Non-image blocks pass through unchanged. Images that fail to
    spill (bad payload) are dropped with a text note so the prompt isn't
    silently corrupted.
    """
    from ..image_store import spill_image, default_images_dir

    images_dir = Path(cwd) / ".letscode" / "images" if cwd else default_images_dir()
    out: list[dict] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "image":
            data = b.get("data")
            mime = b.get("mime_type") or b.get("mimeType") or "image/png"
            if not data:
                out.append({"type": "text", "text": "[image unavailable]"})
                continue
            try:
                path = spill_image(data, mime, images_dir=images_dir)
            except (ValueError, OSError):
                out.append({"type": "text", "text": "[image unavailable]"})
                continue
            out.append({"type": "image_ref", "path": str(path), "mime_type": mime})
        else:
            out.append(b)
    return out


def _events_after_summary(events: list[dict], summary_event: dict) -> list[dict]:
    """Return the events following ``summary_event`` in the log.

    On load with a summary, only those (the kept recent turns) are streamed to
    the UI — the early turns stay on disk for the agent's full replay.
    """
    try:
        idx = events.index(summary_event)
    except ValueError:
        return events
    return events[idx + 1:]


def _resolve_image_for_ui(block: dict):
    """Turn an image/image_ref block into an ACP image UserMessageChunk.

    Inline ``image`` blocks pass their base64 straight through. ``image_ref``
    blocks are read from disk and re-encoded to base64 so the ACP UI renders
    the image on session load. Returns ``None`` if the image can't be resolved.
    """
    t = block.get("type")
    if t == "image":
        mime = block.get("mime_type") or block.get("mimeType")
        if mime and block.get("data"):
            return h.image_block(block["data"], mime_type=mime, uri=block.get("uri"))
        return None
    if t == "image_ref":
        path = block.get("path")
        if not path:
            return None
        try:
            from ..image_store import read_as_data_url
            url = read_as_data_url(Path(path), block.get("mime_type"))
        except OSError:
            return None
        # data URL -> pull the base64 payload back out for the ACP block
        header, _, b64 = url.partition(",")
        mime = header.split(";")[0].split(":", 1)[-1] if ":" in header else "image/png"
        return h.image_block(b64, mime_type=mime, uri=path)
    return None


def _read_log_events(log_path: str) -> list[dict]:
    """Read and parse events from a JSONL log file (blocking I/O)."""
    events: list[dict] = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return events


def _last_prompt_tokens(events: list[dict]) -> int | None:
    """Extract the last result event's cumulative prompt_tokens from events.

    This represents the current context fill after the last completed turn,
    used to seed the usage gauge when resuming a session.
    """
    for event in reversed(events):
        if event.get("type") in ("result", "session/result"):
            usage = event.get("data", {}).get("usage")
            if usage:
                tokens = usage.get("prompt_tokens")
                if isinstance(tokens, int) and tokens > 0:
                    return tokens
            return None
    return None


# ── Event translation ──

# ACP tool kind mapping
_TOOL_KINDS: dict[str, str] = {
    "Read": "read",
    "Write": "edit",
    "Edit": "edit",
    "Bash": "other",
    "Glob": "search",
    "Grep": "search",
    "Skill": "other",
    "Agent": "other",
}


def _tool_kind(name: str) -> str:
    if name.startswith("mcp__"):
        return "other"
    return _TOOL_KINDS.get(name, "other")


def _tool_call_title(name: str, inp: dict) -> str:
    if name == "Read":
        return f"Reading {inp.get('file_path', '')}"
    if name == "Write":
        return f"Writing {inp.get('file_path', '')}"
    if name == "Edit":
        return f"Editing {inp.get('file_path', '')}"
    if name == "Bash":
        desc = inp.get("description", inp.get("command", ""))
        return f"Running: {desc}"
    if name == "Glob":
        return f"Searching files: {inp.get('pattern', '')}"
    if name == "Grep":
        return f"Searching: {inp.get('pattern', '')}"
    if name == "Skill":
        return f"Running skill: {inp.get('skill', '')}"
    if name == "Agent":
        return f"Sub-agent: {inp.get('prompt', '')[:50]}"
    if name.startswith("mcp__"):
        parts = name[5:].split("__", 1)
        return parts[1] if len(parts) == 2 else name
    return name


def _tool_result_title(name: str, inp: dict, status: str) -> str | None:
    """Build title for tool_call_update from cached toolName + rawInput."""
    if name == "Bash":
        desc = inp.get("description", "")
        if status == "completed":
            return f"Ran: {desc}"
        elif status == "failed":
            return f"Failed: {desc}"
    return None


def _translate_event(event: dict, pending_tool_inputs: dict[str, dict]) -> Any:
    """Translate a letscode JSONL event to an ACP SessionUpdate object."""
    type_ = event.get("type", "")
    data = event.get("data", {})

    if type_ == "init":
        return None

    if type_ == "agent_message_chunk":
        # Each chunk is one line of stream output with its "\n" stripped; a
        # blank line is the empty string "". Append "\n" unconditionally and
        # forward — dropping the empty string would lose markdown paragraph
        # breaks.
        text = data.get("text", "")
        return h.update_agent_message_text(text + "\n")

    if type_ == "agent_thought_chunk":
        # Reasoning/thinking output (e.g. GLM reasoning_content). Same
        # blank-line handling as agent_message_chunk above.
        text = data.get("text", "")
        return h.update_agent_thought_text(text + "\n")

    if type_ in ("session/prompt", "prompt"):
        # User input event. Current "prompt" events carry the block list
        # directly in data; legacy "session/prompt" events wrap it as
        # data.prompt. Emit each block as a user_message_chunk so resumed
        # sessions show the same input as the live path (text + images).
        if type_ == "prompt":
            blocks = data if isinstance(data, list) else []
        else:  # session/prompt (legacy)
            blocks = data.get("prompt", []) if isinstance(data, dict) else []
        updates: list = []
        text_parts: list[str] = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and b.get("text"):
                text_parts.append(b["text"])
            elif t in ("image", "image_ref"):
                # Flush any accumulated text first, preserving block order.
                if text_parts:
                    updates.append(h.update_user_message(h.text_block("".join(text_parts))))
                    text_parts = []
                resolved = _resolve_image_for_ui(b)
                if resolved is not None:
                    updates.append(h.update_user_message(resolved))
        if text_parts:
            updates.append(h.update_user_message(h.text_block("".join(text_parts))))
        return updates if updates else None

    if type_ == "tool_call":
        tc_id = data.get("toolCallId", "")
        name = data.get("toolName", "")
        inp = data.get("rawInput", data.get("input", {}))
        pending_tool_inputs[tc_id] = {"input": inp, "name": name}

        title = data.get("title", "")
        if not title or "rawInput" in data:
            title = _tool_call_title(name, inp)

        locations = None
        file_path = inp.get("file_path", "") if isinstance(inp, dict) else ""
        if file_path:
            locations = [ToolCallLocation(path=os.path.abspath(file_path))]

        raw_input: Any = inp
        if isinstance(inp, dict) and "command" in inp:
            raw_input = f"```\n{inp['command']}\n```"

        kind = data.get("kind", "")
        if not kind or "rawInput" in data:
            kind = _tool_kind(name)
        if not kind:
            kind = "other"

        return h.start_tool_call(
            tool_call_id=tc_id,
            title=title,
            kind=kind,
            status="pending",
            raw_input=raw_input,
            locations=locations,
        )

    if type_ == "tool_call_update":
        tc_id = data.get("toolCallId", "")
        status = data.get("status", "")
        cached = pending_tool_inputs.get(tc_id, {})
        inp = cached.get("input", {})
        name = cached.get("name", "")

        title = None
        if name:
            title = _tool_result_title(name, inp, status)

        content = None
        if status in ("completed", "failed"):
            content = _build_completed_content(name, inp, data)
            if content is None and "content" in data:
                content = _content_to_tool_blocks(data["content"])
            pending_tool_inputs.pop(tc_id, None)
        elif "content" in data:
            content = _content_to_tool_blocks(data["content"])

        return h.update_tool_call(
            tool_call_id=tc_id,
            title=title,
            status=status,
            content=content,
        )

    if type_ in ("user_message", "user_message_chunk"):
        # Injected user content: skill expansion prompts, compact summaries.
        # Translate so they render as a user turn in the ACP client.
        text = data.get("text", "")
        if text:
            return h.update_user_message(h.text_block(text))
        return None

    if type_ == "result":
        # Not translated to session update; handled by prompt() method
        return None

    if type_ == "session/result":
        # Legacy format
        return None

    if type_ == "session/summary":
        # A collapsed summary of early turns, shown as a thought block in the UI.
        text = data.get("text", "")
        n = data.get("summarized_turns", 0)
        header = f"[已折叠 {n} 轮早期对话]" if n else "[历史摘要]"
        body = f"{header}\n{text}" if text else header
        return h.update_agent_thought(h.text_block(body))

    return None


def _content_to_tool_blocks(content) -> list | None:
    """Convert tool_call_update content to ACP tool content blocks."""
    if isinstance(content, str):
        return [h.tool_content(h.text_block(content))]
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                texts.append(item.get("text", str(item)))
        return [h.tool_content(h.text_block("\n".join(texts)))] if texts else None
    return None


def _build_completed_content(
    tool_name: str, inp: dict, data: dict,
) -> list | None:
    """Build ACP content for tool completions: diff, resource_link, or text."""
    if not tool_name or not inp:
        return None

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
        result_text = data.get("rawOutput", data.get("result", ""))
        # Legacy: old logs externalized results to result_file
        if not result_text and "result_file" in data:
            try:
                result_text = Path(data["result_file"]).read_text(encoding="utf-8")
            except (FileNotFoundError, OSError):
                result_text = data.get("result_summary", "")
        if result_text:
            return [h.tool_content(h.text_block(f"```\n{result_text}\n```"))]

    return None

