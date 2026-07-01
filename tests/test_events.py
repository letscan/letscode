"""Tests for LogSubscriber format and feed isolation.

LogSubscriber writes a human-readable, non-JSON debug log. This is a
deliberate design choice: the format must NOT be parseable as jsonl so
that read_events (the feed loader) structurally cannot mistake an internal
log for a replay feed. The replay feed is a separate file produced by
FeedOutputSubscriber (--output).
"""

import json
from pathlib import Path

import pytest

from letscode.events import EventHub, LogSubscriber, set_hub
from letscode.feed_util import read_events


class TestLogSubscriberFormat:
    """The log is human-readable text, not jsonl — feed isolation."""

    def _new_log(self, tmp_path) -> tuple[LogSubscriber, Path]:
        log = LogSubscriber(tmp_path / "logs")
        return log, log.log_path

    def test_file_extension_is_log(self, tmp_path):
        _, path = self._new_log(tmp_path)
        assert path.suffix == ".log"
        assert path.suffix != ".jsonl"

    def test_log_lines_are_not_valid_json(self, tmp_path):
        log, path = self._new_log(tmp_path)
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(log)
        hub.emit_prompt(prompt_blocks=[{"type": "text", "text": "hello"}])

        content = path.read_text().strip()
        for line in content.split("\n"):
            # Each line must FAIL json.loads — this is the feed-isolation
            # contract. If a line ever parses as JSON, a log could be
            # mistaken for a feed.
            with pytest.raises(json.JSONDecodeError):
                json.loads(line)

    def test_read_events_rejects_log_file(self, tmp_path):
        """read_events (the feed loader) must fail on a log file."""
        log, path = self._new_log(tmp_path)
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(log)
        hub.emit_prompt(prompt_blocks=[{"type": "text", "text": "hello"}])

        with pytest.raises(json.JSONDecodeError):
            read_events(str(path))

    def test_format_is_timestamp_level_type_summary(self, tmp_path):
        log, path = self._new_log(tmp_path)
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(log)
        hub.emit_prompt(prompt_blocks=[{"type": "text", "text": "run tests"}])

        line = path.read_text().strip().split("\n")[0]
        # [ISO_TS] INFO event_type: summary
        assert line.startswith("[")
        assert "] INFO " in line
        assert "prompt: run tests" in line


class TestLogSubscriberSummaries:
    """Large tool outputs are summarized (size/lines), not written in full."""

    def _setup(self, tmp_path):
        log = LogSubscriber(tmp_path / "logs")
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(log)
        return log, hub

    def test_large_result_summarized(self, tmp_path):
        log, hub = self._setup(tmp_path)
        big = "LINE\n" * 5000  # ~25KB
        hub.emit_tool_call("t1", "Bash", {"command": "big"})
        hub.emit_tool_update("t1", status="completed", raw_output=big)

        content = log.log_path.read_text()
        # The full 25KB must NOT appear; only a line/byte summary
        assert big not in content
        assert "completed" in content
        assert "bytes" in content

    def test_tool_call_shows_name_and_args(self, tmp_path):
        log, hub = self._setup(tmp_path)
        hub.emit_tool_call("t1", "Read", {"file_path": "x.py"})

        content = log.log_path.read_text()
        assert "Read" in content
        assert "x.py" in content

    def test_agent_message_text_present(self, tmp_path):
        log, hub = self._setup(tmp_path)
        hub.emit_agent_message_chunk("working on it")

        content = log.log_path.read_text()
        assert "working on it" in content

    def test_long_summary_truncated(self, tmp_path):
        log, hub = self._setup(tmp_path)
        long_text = "x" * 1000
        hub.emit_agent_message_chunk(long_text)

        content = log.log_path.read_text()
        # Should be truncated (ellipsis marker), not the full 1000 chars
        assert "…" in content
        assert long_text not in content

    def test_log_debug_writes_debug_level(self, tmp_path):
        log = LogSubscriber(tmp_path / "logs")
        log.log_debug("something happened")

        content = log.log_path.read_text()
        assert "] DEBUG " in content
        assert "something happened" in content


class TestLogSubscriberThought:
    """agent_thought_chunk is logged with a 💭 prefix for human clarity."""

    def _setup(self, tmp_path):
        log = LogSubscriber(tmp_path / "logs")
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(log)
        return log, hub

    def test_thought_logged_with_prefix(self, tmp_path):
        log, hub = self._setup(tmp_path)
        hub.emit_agent_thought_chunk("Let me consider the options")

        content = log.log_path.read_text()
        assert "💭" in content
        assert "Let me consider the options" in content
        assert "agent_thought_chunk" in content

    def test_thought_distinct_from_message(self, tmp_path):
        """A thought line carries the prefix; a message line does not."""
        log, hub = self._setup(tmp_path)
        hub.emit_agent_message_chunk("the answer")
        hub.emit_agent_thought_chunk("reasoning here")

        content = log.log_path.read_text()
        lines = content.strip().split("\n")
        msg_line = next(l for l in lines if "the answer" in l)
        thought_line = next(l for l in lines if "reasoning here" in l)
        assert "💭" not in msg_line
        assert "💭" in thought_line


class TestCliOutputThoughtVerboseOnly:
    """CliOutputSubscriber renders thoughts to stderr only in verbose mode."""

    def _emit(self, verbose: bool, capsys) -> str:
        from letscode.subscribers import CliOutputSubscriber

        sub = CliOutputSubscriber(verbose=verbose)
        sub("agent_thought_chunk", {"type": "text", "text": "pondering deeply"})
        captured = capsys.readouterr()
        return captured

    def test_verbose_emits_thought_to_stderr(self, capsys):
        captured = self._emit(verbose=True, capsys=capsys)
        assert "pondering deeply" in captured.err
        assert "pondering deeply" not in captured.out

    def test_non_verbose_silences_thought(self, capsys):
        captured = self._emit(verbose=False, capsys=capsys)
        assert captured.err == ""
        assert captured.out == ""


class TestConsumeStreamReasoning:
    """consume_stream parses reasoning_content via getattr and routes it."""

    def test_reasoning_routed_to_on_thought_line(self):
        from letscode.stream import consume_stream

        thoughts = []
        client = _FakeClient([_fake_chunk(reasoning="thinking...")])
        consume_stream(
            client, "m", [], 100,
            on_thought_line=lambda t: thoughts.append(t),
        )
        assert thoughts == ["thinking..."]

    def test_reasoning_collected_in_thought_content(self):
        from letscode.stream import consume_stream

        client = _FakeClient([
            _fake_chunk(reasoning="part 1"),
            _fake_chunk(reasoning="part 2"),
        ])
        result = consume_stream(client, "m", [], 100)
        assert result.thought_content == "part 1part 2"

    def test_no_reasoning_yields_empty_thought_content(self):
        from letscode.stream import consume_stream

        client = _FakeClient([_fake_chunk(content="hello")])
        result = consume_stream(client, "m", [], 100)
        assert result.thought_content == ""
        assert result.text_content == "hello"


def _fake_chunk(*, content=None, reasoning=None):
    """Build an object mimicking an OpenAI streaming chunk.

    reasoning_content is set as a plain attribute to emulate the SDK's
    extra="allow" behavior (the field is undeclared but preserved at runtime).
    """
    class _Delta:
        pass

    delta = _Delta()
    delta.content = content
    delta.tool_calls = None
    if reasoning is not None:
        delta.reasoning_content = reasoning

    class _Choice:
        pass

    choice = _Choice()
    choice.delta = delta

    class _Chunk:
        pass

    chunk = _Chunk()
    chunk.choices = [choice]
    chunk.usage = None
    return chunk


def _fake_usage_chunk(usage):
    """Build a final chunk carrying usage with empty choices list."""
    class _Usage:
        def __init__(self, d):
            self.prompt_tokens = d.get("prompt_tokens", 0)
            self.completion_tokens = d.get("completion_tokens", 0)
            self.total_tokens = d.get("total_tokens", 0)

    class _Chunk:
        pass

    chunk = _Chunk()
    chunk.choices = []
    chunk.usage = _Usage(usage)
    return chunk


class _FakeClient:
    """Minimal stand-in for openai.OpenAI with a streaming create()."""

    def __init__(self, chunks):
        # The nested chat.completions.create must read the chunks given here,
        # so stash them on a shared location the generator closes over.
        type(self).chat.completions._chunks = chunks

    class chat:
        class completions:
            _chunks = []

            @classmethod
            def create(cls, **_):
                def _gen():
                    yield from cls._chunks
                return _gen()


class TestUsageCapture:
    """consume_stream captures usage from the final empty-choices chunk."""

    def test_usage_captured_in_stream_result(self):
        from letscode.stream import consume_stream

        client = _FakeClient([
            _fake_chunk(content="hello"),
            _fake_usage_chunk({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
        ])
        result = consume_stream(client, "m", [], 100)
        assert result.usage is not None
        assert result.usage["prompt_tokens"] == 10
        assert result.usage["completion_tokens"] == 5
        assert result.usage["total_tokens"] == 15

    def test_no_usage_chunk_yields_none(self):
        from letscode.stream import consume_stream

        client = _FakeClient([_fake_chunk(content="hello")])
        result = consume_stream(client, "m", [], 100)
        assert result.usage is None


class TestEventHubUsageAccumulation:
    """EventHub accumulates usage across turns and surfaces it in emit_result."""

    def test_record_usage_accumulates(self):
        from letscode.events import EventHub, set_hub

        hub = EventHub()
        set_hub(hub)
        hub.record_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        hub.record_usage({"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28})
        assert hub._usage == {"prompt_tokens": 30, "completion_tokens": 13, "total_tokens": 43}

    def test_emit_result_includes_usage_when_nonzero(self, tmp_path):
        from letscode.events import EventHub, set_hub

        hub = EventHub()
        set_hub(hub)
        captured = []
        hub.subscribe(lambda t, d: captured.append((t, d)))
        hub.record_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        hub.emit_result("end_turn")

        result_event = next((t, d) for t, d in captured if t == "result")
        assert result_event[1]["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_emit_result_omits_usage_when_zero(self, tmp_path):
        from letscode.events import EventHub, set_hub

        hub = EventHub()
        set_hub(hub)
        captured = []
        hub.subscribe(lambda t, d: captured.append((t, d)))
        hub.emit_result("end_turn")

        result_event = next((t, d) for t, d in captured if t == "result")
        assert "usage" not in result_event[1]


class TestCliOutputResultUsage:
    """CliOutputSubscriber prints a token summary on the result event."""

    def test_result_with_usage_prints_to_stderr(self, capsys):
        from letscode.subscribers import CliOutputSubscriber

        sub = CliOutputSubscriber(verbose=False)
        sub("result", {"stopReason": "end_turn", "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})
        captured = capsys.readouterr()
        assert "100" in captured.err
        assert "150" in captured.err

    def test_result_without_usage_silent(self, capsys):
        from letscode.subscribers import CliOutputSubscriber

        sub = CliOutputSubscriber(verbose=False)
        sub("result", {"stopReason": "end_turn"})
        captured = capsys.readouterr()
        assert captured.err == ""


class TestStreamRetry:
    """consume_stream retries transient errors and propagates non-retryable ones."""

    @staticmethod
    def _make_response():
        """Build a minimal stand-in for httpx.Response that the OpenAI SDK
        exception constructors accept (they access response.request)."""
        class _Req:
            url = "https://example.com/chat"
            headers = {}

        class _Resp:
            status_code = 429
            headers = {}
            request = _Req()

            def json(self):
                return {}

            def text(self):
                return ""

        return _Resp()

    def test_retry_on_rate_limit_then_success(self, monkeypatch):
        from letscode import stream as stream_mod
        from letscode.stream import consume_stream
        from openai import RateLimitError

        # Avoid real sleeping during the backoff
        monkeypatch.setattr(stream_mod.time, "sleep", lambda _: None)

        calls = {"n": 0}

        class _Client:
            class chat:
                class completions:
                    @staticmethod
                    def create(**_):
                        def _gen():
                            calls["n"] += 1
                            if calls["n"] == 1:
                                raise RateLimitError(
                                    message="rate limited",
                                    response=self._make_response(),
                                    body=None,
                                )
                            yield _fake_chunk(content="recovered")
                        return _gen()

        result = consume_stream(_Client(), "m", [], 100, max_retries=2)
        assert calls["n"] == 2
        assert result.text_content == "recovered"

    def test_non_retryable_propagates_immediately(self, monkeypatch):
        from letscode import stream as stream_mod
        from letscode.stream import consume_stream
        from openai import BadRequestError

        monkeypatch.setattr(stream_mod.time, "sleep", lambda _: None)

        calls = {"n": 0}

        class _Client:
            class chat:
                class completions:
                    @staticmethod
                    def create(**_):
                        calls["n"] += 1
                        def _gen():
                            raise BadRequestError(
                                message="bad request",
                                response=self._make_response(),
                                body=None,
                            )
                        return _gen()

        with pytest.raises(BadRequestError):
            consume_stream(_Client(), "m", [], 100, max_retries=3)
        assert calls["n"] == 1  # no retries

    def test_retry_exhausted_raises_last_error(self, monkeypatch):
        from letscode import stream as stream_mod
        from letscode.stream import consume_stream
        from openai import RateLimitError

        monkeypatch.setattr(stream_mod.time, "sleep", lambda _: None)

        calls = {"n": 0}

        class _Client:
            class chat:
                class completions:
                    @staticmethod
                    def create(**_):
                        def _gen():
                            calls["n"] += 1
                            raise RateLimitError(
                                message="rate limited",
                                response=self._make_response(),
                                body=None,
                            )
                        return _gen()

        with pytest.raises(RateLimitError):
            consume_stream(_Client(), "m", [], 100, max_retries=2)
        # 1 initial + 2 retries = 3 total attempts
        assert calls["n"] == 3


class TestContextWindowConfig:
    """ModelConfig carries a context_window; config entries can set it."""

    def test_context_window_field_default_none(self):
        from letscode.config import ModelConfig

        cfg = ModelConfig(model="m")
        assert cfg.context_window is None

    def test_context_window_loaded_from_config(self, tmp_path):
        from letscode.config import load_config

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "default_model": "m1",
            "providers": {
                "p": {
                    "base_url": "http://x",
                    "api_key": "k",
                    "models": [{"model": "m1", "context_window": 200000}],
                }
            },
        }))
        cfg, _ = load_config(str(config_file), "m1")
        assert cfg.context_window == 200000

    def test_context_window_absent_yields_none(self, tmp_path):
        from letscode.config import load_config

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "default_model": "m1",
            "providers": {
                "p": {"base_url": "http://x", "api_key": "k", "models": [{"model": "m1"}]},
            },
        }))
        cfg, _ = load_config(str(config_file), "m1")
        assert cfg.context_window is None


class TestEmitInitContextWindow:
    """emit_init surfaces contextWindow in the init event when provided."""

    def test_init_carries_context_window(self):
        from letscode.events import EventHub, set_hub

        hub = EventHub()
        set_hub(hub)
        captured = []
        hub.subscribe(lambda t, d: captured.append((t, d)))
        hub.emit_init(model="m", cwd=".", max_tokens=100, max_turns=3,
                      preset="default", sandbox=True, tools=[], context_window=200000)
        init = next(d for t, d in captured if t == "init")
        assert init["contextWindow"] == 200000

    def test_init_omits_context_window_when_none(self):
        from letscode.events import EventHub, set_hub

        hub = EventHub()
        set_hub(hub)
        captured = []
        hub.subscribe(lambda t, d: captured.append((t, d)))
        hub.emit_init(model="m", cwd=".", max_tokens=100, max_turns=3,
                      preset="default", sandbox=True, tools=[])
        init = next(d for t, d in captured if t == "init")
        assert "contextWindow" not in init


class TestStatFormatting:
    """The show-stat quote and token/duration helpers format correctly."""

    def test_human_tokens(self):
        from letscode.acp.server import _human_tokens

        assert _human_tokens(0) == "0"
        assert _human_tokens(450) == "450"
        assert _human_tokens(2700) == "2.7k"
        assert _human_tokens(100000) == "100.0k"

    def test_human_duration(self):
        from letscode.acp.server import _human_duration

        assert _human_duration(8.3) == "8s"
        assert _human_duration(76.4) == "1m16s"
        assert _human_duration(122.0) == "2m2s"

    def test_format_stat_quote(self):
        from letscode.acp.server import _format_stat_quote

        quote = _format_stat_quote(3, 2700, 76.4)
        assert quote.strip() == "> Turn 3 | 2.7k tokens | 1m16s"


class TestModelContextWindowLookup:
    """LetscodeAgent resolves context_window from loaded config entries."""

    def test_lookup_returns_configured_value(self, tmp_path):
        from letscode.acp.server import LetscodeAgent

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "default_model": "m1",
            "providers": {
                "p": {
                    "base_url": "http://x", "api_key": "k",
                    "models": [
                        {"model": "m1", "context_window": 200000},
                        {"model": "m2", "context_window": 32768},
                    ],
                }
            },
        }))
        agent = LetscodeAgent(str(config_file))
        assert agent._model_context_window("m1") == 200000
        assert agent._model_context_window("m2") == 32768

    def test_lookup_returns_none_when_unset(self, tmp_path):
        from letscode.acp.server import LetscodeAgent

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "default_model": "m1",
            "providers": {
                "p": {"base_url": "http://x", "api_key": "k", "models": [{"model": "m1"}]},
            },
        }))
        agent = LetscodeAgent(str(config_file))
        assert agent._model_context_window("m1") is None

    def test_lookup_falls_back_to_default_model(self, tmp_path):
        from letscode.acp.server import LetscodeAgent

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "default_model": "m1",
            "providers": {
                "p": {
                    "base_url": "http://x", "api_key": "k",
                    "models": [{"model": "m1", "context_window": 131072}],
                }
            },
        }))
        agent = LetscodeAgent(str(config_file))
        # No model_id passed -> uses default_model
        assert agent._model_context_window(None) == 131072


class TestLastPromptTokens:
    """_last_prompt_tokens recovers the last turn's context fill from a log."""

    def test_returns_last_result_prompt_tokens(self):
        from letscode.acp.server import _last_prompt_tokens

        events = [
            {"type": "prompt", "data": []},
            {"type": "result", "data": {"usage": {"prompt_tokens": 4500}}},
            {"type": "prompt", "data": []},
            {"type": "result", "data": {"usage": {"prompt_tokens": 8000}}},
        ]
        assert _last_prompt_tokens(events) == 8000

    def test_returns_none_when_no_result(self):
        from letscode.acp.server import _last_prompt_tokens

        assert _last_prompt_tokens([{"type": "prompt", "data": []}]) is None

    def test_returns_none_when_result_has_no_usage(self):
        from letscode.acp.server import _last_prompt_tokens

        events = [{"type": "result", "data": {"stopReason": "end_turn"}}]
        assert _last_prompt_tokens(events) is None

    def test_handles_legacy_session_result_type(self):
        from letscode.acp.server import _last_prompt_tokens

        events = [
            {"type": "session/result", "data": {"usage": {"prompt_tokens": 3000}}},
        ]
        assert _last_prompt_tokens(events) == 3000


class TestReplayStatQuote:
    """Session load replay emits the same per-turn stat footers as live."""

    def test_builds_quote_from_result_event(self):
        from letscode.acp.server import _make_replay_stat_quote
        data = {"usage": {"prompt_tokens": 5000}, "duration_ms": 3000}
        q = _make_replay_stat_quote(data, prev_tokens=2000, prev_turn=1)
        assert q is not None
        assert "Turn 2" in q
        assert "3.0k" in q     # delta = 5000 - 2000 = 3000 → "3.0k"
        assert "3s" in q       # 3000ms = 3s

    def test_delta_floored_at_zero(self):
        from letscode.acp.server import _make_replay_stat_quote
        data = {"usage": {"prompt_tokens": 100}, "duration_ms": 1000}
        q = _make_replay_stat_quote(data, prev_tokens=500, prev_turn=0)
        assert q is not None
        assert "0 tokens" in q   # max(100 - 500, 0) = 0

    def test_returns_none_when_no_usage_or_duration(self):
        from letscode.acp.server import _make_replay_stat_quote
        assert _make_replay_stat_quote({}, prev_tokens=0, prev_turn=0) is None

    def test_turn_number_increments(self):
        from letscode.acp.server import _make_replay_stat_quote
        data = {"usage": {"prompt_tokens": 1000}, "duration_ms": 500}
        q = _make_replay_stat_quote(data, prev_tokens=0, prev_turn=4)
        assert "Turn 5" in q
