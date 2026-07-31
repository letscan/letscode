"""CLI entry point for letscode."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .agent import run_agent
from .agent_card import apply_card, discover_agent_cards, load_agent_card
from .config import load_config, list_models
from .events import (
    EventHub,
    FeedOutputSubscriber,
    LogSubscriber,
    StreamSubscriber,
    set_hub,
)
from .mcp import McpManager
from .mcp.client import set_manager
from .prompt import build_system_prompt
from .rules import load_rules, merge_rules
from .subscribers import CliOutputSubscriber, MessageSubscriber
from .tools import TOOL_DEFINITIONS, EXECUTORS
from .tools.runner import ToolRunner


def _merge_scan_dirs(config_dirs: list[str], cli_dirs: list[str] | None) -> list[str]:
    """Merge config ``add_scan_dirs`` with ``--add-scan-dir`` flags.

    Config dirs first, CLI dirs appended, de-duplicated while preserving order.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for d in [*config_dirs, *(cli_dirs or [])]:
        if d and d not in seen:
            merged.append(d)
            seen.add(d)
    return merged


# camelCase rules-key lookup for --allow parsing and session-config loading.
# (rules.py uses camelCase: allowRead / allowWrite / allowCmd.)
_ALLOW_TYPE_TO_KEY = {
    "read": "allowRead",
    "write": "allowWrite",
    "cmd": "allowCmd",
}


def _parse_allow_flags(flags: list[str]) -> dict[str, list[str]]:
    """Parse repeated ``--allow <type>:<target>`` flags into a rules dict.

    ``<type>`` is one of ``read|write|cmd``. ``<target>`` is the path or
    command string (verbatim; may contain spaces, colons, etc.). The target
    is taken as everything after the FIRST colon, so paths/commands with
    embedded colons are preserved. Unknown types raise ``SystemExit`` so a
    typo fails loudly instead of silently dropping the grant.

    Examples::

        ["write:/etc/hosts"]                         → {"allowWrite": ["/etc/hosts"]}
        ['cmd:npm run dev']                          → {"allowCmd": ["npm run dev"]}
        ["write:/a:b.txt"]                           → {"allowWrite": ["/a:b.txt"]}
    """
    out: dict[str, list[str]] = {}
    for flag in flags:
        if ":" not in flag:
            raise SystemExit(
                f"--allow expects <type>:<target>, got {flag!r}"
            )
        typ, _, target = flag.partition(":")
        typ = typ.strip()
        if typ not in _ALLOW_TYPE_TO_KEY:
            raise SystemExit(
                f"--allow type must be read|write|cmd, got {typ!r}"
            )
        target = target.strip()
        if not target:
            raise SystemExit(
                f"--allow target is empty in {flag!r}"
            )
        key = _ALLOW_TYPE_TO_KEY[typ]
        out.setdefault(key, []).append(target)
    return out


def _load_session_allow_config(path: Path) -> dict[str, list[str]]:
    """Load a session-level allow-always config written by the ACP server.

    Format: ``{"allowRead": [...], "allowWrite": [...], "allowCmd": [...]}``
    (camelCase, matching rules.py keys). Unknown keys are ignored; missing
    file or parse errors return ``{}`` (a missing grant should never break
    the run — the worst case is the user re-approves via popup).
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k in ("allowRead", "allowWrite", "allowCmd"):
        v = data.get(k)
        if isinstance(v, list):
            out[k] = [str(x) for x in v]
    return out


def _emit_termination_notice(reason: str, *, event_stream: bool, hub) -> None:
    """Emit a "run ended early" notice per the CLI exit-code contract (School A).

    The CONTENT is identical regardless of mode (the caller/LLM must learn the
    run ended early and why); only the CARRIER differs, and each mode's carrier
    must stay valid for its consumers:

    - ``--event-stream`` mode: the stdout stream is strict JSONL consumed by
      the ACP server line-by-line (json.loads per line). So the notice goes
      ONLY as typed JSONL events (``error`` with ``code="interrupted"`` + a
      terminal ``result``) — NEVER as a bare text line, which would corrupt
      the stream.
    - text / sub-agent mode: a one-line human-readable notice on **stdout**
      so a capturing parent (gs / the Agent tool / DevHard) gets *something*
      to feed back — never an empty pipe.

    ``hub`` is the EventHub (available in _async_main); None in the main()
    last-resort path, where the hub is already closed and only text output is
    possible (and event_stream there means the JSONL stream already ended, so
    a bare line would still be invalid — we emit nothing extra in that case).
    """
    if event_stream:
        # JSONL stream only. A bare text line would break json.loads consumers.
        if hub is not None:
            hub.emit_error(reason, code="interrupted", recoverable=False)
            hub.on_session_end("interrupted")
    else:
        print(f"Agent terminated early: {reason}", file=sys.stdout)
        print()  # final newline


async def _async_main(args):
    """Main entry: single event loop for MCP connections + agent loop."""
    original_cwd = os.getcwd()

    if args.workspace:
        os.chdir(os.path.expanduser(args.workspace))

    try:
        config, mcp_servers = load_config(args.config, args.model)

        # Effective extra scan dirs: config add_scan_dirs + --add-scan-dir flags,
        # merged (config first, CLI appended, de-duplicated). Set on the skill
        # module's runtime state so the Skill tool (called with no args via the
        # ToolRunner protocol) resolves skills from these roots at exec time.
        scan_dirs = _merge_scan_dirs(config.add_scan_dirs, args.add_scan_dirs)
        from .tools.skill import set_scan_dirs
        set_scan_dirs(scan_dirs)

        # AgentCard: merge card with (config, mcp_servers) once, up front.
        # CLI overrides below run after apply_card, so CLI > card naturally
        # (e.g. --no-mcp zeros out the card-filtered mcp_servers).
        card = load_agent_card(args.agent, scan_dirs=scan_dirs) if args.agent else None
        overrides = apply_card(config, mcp_servers, card)
        mcp_servers = overrides.mcp_servers

        # Card preset overrides config.json's preset (safe/default/risk); the
        # CLI --preset flag still wins over both. Applied to ModelConfig here
        # so the merge_rules(preset, ...) call below sees the card's intent.
        if overrides.preset is not None:
            config.preset = overrides.preset

        # CLI overrides for security settings
        if args.no_sandbox:
            config.sandbox = False
        if args.preset:
            config.preset = args.preset
        config.verbose = args.verbose

        # Reasoning effort: when the model declares effort_options, resolve the
        # effective tier (--effort flag, else the first listed option = default)
        # and merge it into extra_body as the reasoning_effort field. Providers
        # that don't recognize reasoning_effort simply ignore it.
        if config.effort_options:
            tier = args.effort
            if tier is None:
                tier = config.effort_options[0]
            elif tier not in config.effort_options:
                raise SystemExit(
                    f"Effort {tier!r} not in model's effort_options "
                    f"{config.effort_options}."
                )
            config.extra_body = {**(config.extra_body or {}),
                                 "reasoning_effort": tier}

        # Sub-agents skip MCP to avoid duplicate connections and cleanup issues
        if args.no_mcp:
            mcp_servers = {}

        # Build prompt_blocks from --text/--image flags + optional positional.
        prompt_blocks = _build_prompt_blocks(args)

        # Validate image paths up front: a typo silently drops the image from
        # the message (the LLM then answers "I don't see any image"), which is
        # hard to debug. Fail loudly instead.
        for b in prompt_blocks:
            if isinstance(b, dict) and b.get("type") == "image_ref":
                if not Path(b["path"]).exists():
                    raise SystemExit(f"Image not found: {b['path']}")

        # --feed X --append is sugar for --feed X --output X --event-stream
        if args.append and args.feed and not args.output:
            args.output = args.feed
            args.event_stream = True

        # Initialize EventHub
        hub = EventHub()
        set_hub(hub)

        # Enable upstream externalization: large tool results are persisted to
        # .letscode/cache/ and replaced with a preview+path reference before
        # any subscriber sees them. This keeps every downstream consumer
        # (Stream/Feed/Message/Log) from receiving oversized payloads.
        cache_dir = Path(os.getcwd()) / ".letscode" / "cache"
        hub.enable_externalization(cache_dir)

        # LogSubscriber: always-on 1:1 raw event log
        log_dir = Path(os.getcwd()) / ".letscode" / "logs"
        log_sub = LogSubscriber(log_dir)
        hub.subscribe(log_sub)

        # MessageSubscriber: always-on, builds messages list.
        # log_stem drives large-result persistence: prefer the --output feed
        # path (so persisted refs align with the replayable file), fall back
        # to the internal log path.
        if args.output and args.event_stream:
            msg_log_stem = Path(args.output)
        else:
            msg_log_stem = log_sub.log_path
        msg_sub = MessageSubscriber(log_stem=msg_log_stem)
        hub.subscribe(msg_sub)

        # Real-time output: StreamSubscriber (event-stream) or CliOutputSubscriber
        if args.event_stream:
            hub.subscribe(StreamSubscriber())
        else:
            hub.subscribe(CliOutputSubscriber(verbose=args.verbose))

        # FeedOutputSubscriber: --output writes consolidated agent output
        if args.output:
            if args.event_stream:
                feed_mode = "json"
            elif args.verbose:
                feed_mode = "verbose"
            else:
                feed_mode = "text"
            hub.subscribe(FeedOutputSubscriber(
                path=args.output, mode=feed_mode,
            ))

        # Initialize MCP
        mcp = McpManager()
        set_manager(mcp)

        try:
            if mcp_servers:
                await mcp.connect_all(mcp_servers, quiet=args.event_stream)

            # Build security rules (card rules already merged into rules_raw).
            # Layering (lowest → highest precedence): preset → config/card
            # rules_raw → session-level allow-always (.letscode/config.<sid>.json)
            # → CLI --allow. Higher layers are more specific allow patterns,
            # which the most-specific-wins engine promotes over broader denies.
            rules_raw = dict(overrides.rules_raw or {})

            # Session-level allow-always: written by the ACP server when the
            # user picks "Approve for session". Bound to the session id, which
            # is the --feed file name's stem (no extra flag needed). Pure CLI
            # runs that pass --feed also pick this up; runs without --feed have
            # no session id and skip it (one-shot, no in-session inheritance).
            if args.feed:
                sid = Path(args.feed).stem
                sess_cfg_path = (
                    Path(os.getcwd()) / ".letscode" / f"config.{sid}.json"
                )
                sess_rules = _load_session_allow_config(sess_cfg_path)
                if sess_rules:
                    for k, v in sess_rules.items():
                        rules_raw.setdefault(k, [])
                        rules_raw[k] = [*rules_raw[k], *v]

            # CLI --allow <type>:<target> flags (repeatable). Parsed into the
            # matching camelCase rules key. Highest precedence (most specific
            # allow pattern), so they override broad denies via the escape hatch.
            for k, v in _parse_allow_flags(args.allow or []).items():
                rules_raw.setdefault(k, [])
                rules_raw[k] = [*rules_raw[k], *v]

            user_rules = load_rules(rules_raw)
            rules = merge_rules(config.preset, user_rules)

            # AgentCard tool whitelist: filter built-in tools (MCP tools are
            # filtered in ToolRunner.definitions by the same allowlist).
            if overrides.tool_allowlist is None:
                tool_defs, execs = TOOL_DEFINITIONS, EXECUTORS
            else:
                allow = overrides.tool_allowlist
                tool_defs = [
                    d for d in TOOL_DEFINITIONS
                    if d.get("function", {}).get("name") in allow
                ]
                execs = {k: v for k, v in EXECUTORS.items() if k in allow}

            # Create ToolRunner
            tool_runner = ToolRunner(
                definitions=tool_defs,
                executors=execs,
                mcp=mcp,
                rules=rules,
                preset=config.preset,
                sandbox=config.sandbox,
                agent_config={
                    "config_path": args.config,
                    "preset": config.preset,
                    "sandbox": config.sandbox,
                    "verbose": args.verbose,
                    "scan_dirs": scan_dirs,
                    "state_file": args.state,
                },
                tool_allowlist=overrides.tool_allowlist,
                skill_allowlist=overrides.skill_allowlist,
            )

            # Build system_prompt (feed scenario uses feed model).
            #   - AgentCard body: render {{ env }} / {{ skills }} /
            #     {{ default_system_prompt }} variables against the card.
            #   - No card: assemble the built-in prompt directly (no template
            #     layer — unchanged behavior).
            model = config.model
            if args.feed:
                from .feed import load_feed
                feed_model, _ = load_feed(args.feed)
                model = feed_model or model
            if overrides.system_prompt is not None:
                from .prompt_renderer import render_card_template
                system_prompt = render_card_template(
                    overrides.system_prompt,
                    model_id=model,
                    skill_allowlist=overrides.skill_allowlist,
                    scan_dirs=scan_dirs,
                )
            else:
                system_prompt = build_system_prompt(model, scan_dirs=scan_dirs)

            # Vision proxy: if the active model can't see images but a
            # vision_model is configured, route each image through it and splice
            # the text descriptions back into the prompt. No-op for vision
            # models or text-only prompts.
            if not config.vision:
                from .config import load_vision_model_id
                vision_model_id = load_vision_model_id(args.config)
                if vision_model_id:
                    from .vision_proxy import rewrite_prompt_for_text_model
                    prompt_blocks = await rewrite_prompt_for_text_model(
                        prompt_blocks, vision_model_id, args.config,
                    )

            try:
                rc = await run_agent(
                    prompt_blocks=prompt_blocks,
                    system_prompt=system_prompt,
                    config=config,
                    max_turns=args.max_turns,
                    feed_path=args.feed,
                    tool_runner=tool_runner,
                    msg_sub=msg_sub,
                    on_agent_start=overrides.on_agent_start,
                    on_agent_end=overrides.on_agent_end,
                    state_file=args.state,
                    config_path=args.config,
                )
            except asyncio.CancelledError:
                # The run was cancelled mid-flight. Two indistinguishable sources
                # land here as the same exception: a genuine user Ctrl-C, and an
                # internal cancellation propagated up from a poisoned MCP
                # session / streaming call (e.g. a remote MCP 429 that the
                # transport's anyio TaskGroup turned into a cancel).
                #
                # CLI exit-code contract (School A, matching Claude Code): we do
                # NOT treat this as a process-level failure (exit stays 0) so a
                # parent process (gs / the Agent tool / DevHard) doesn't see a
                # spurious non-zero exit. But — crucially — we must NOT leave
                # stdout empty either: the contract is "exit 0 AND emit a
                # descriptive message so the caller/LLM knows the run ended
                # early and why." Empty stdout + exit 0 is the worst-of-both
                # (caller can neither treat it as success nor as a caught
                # failure). So we surface a one-line termination notice.
                print("\nInterrupted, shutting down…", file=sys.stderr)
                _emit_termination_notice(
                    "run was cancelled before completion (interrupted)",
                    event_stream=args.event_stream, hub=hub,
                )
                return 0
            if not args.event_stream:
                print()  # final newline
            return rc
        finally:
            # Tear down. On Ctrl-C, asyncio cancels the task and runs this block,
            # but mcp.disconnect_all() awaits MCP child shutdown and can hang for
            # tens of seconds — which is why users had to press Ctrl-C twice.
            # Guard it so an interrupt shuts down promptly; orphaned MCP children
            # are reaped by the OS. hub.close() just closes file handles (fast).
            try:
                await asyncio.wait_for(mcp.disconnect_all(), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
            hub.close()
            set_manager(None)
            set_hub(None)
    finally:
        os.chdir(original_cwd)


def _build_prompt_blocks(args) -> list[dict]:
    """Assemble ordered prompt content blocks from CLI input.

    When ``--text``/``--image`` are present, they are laid out in the exact
    order they appear on the command line (scanned from ``sys.argv`` — argparse's
    ``append`` lists lose that interleaving), and a positional argument, if any,
    is appended as a trailing text block. With no flags, the positional
    argument alone becomes the single text block (the common ``letscode "..."``
    path, unchanged).

    ``--image`` paths are stored verbatim (resolved to absolute) as
    ``image_ref`` blocks; the file is read lazily when the OpenAI message is
    built (see ``subscribers._prompt_message``).
    """
    has_flags = bool(args.text or args.image)
    if not has_flags:
        # Common path: a single text prompt.
        return [{"type": "text", "text": args.prompt or ""}]

    blocks: list[dict] = []
    i = 0
    argv = sys.argv[1:]
    while i < len(argv):
        tok = argv[i]
        if tok == "--text" and i + 1 < len(argv):
            blocks.append({"type": "text", "text": argv[i + 1]})
            i += 2
        elif tok == "--image" and i + 1 < len(argv):
            blocks.append({
                "type": "image_ref",
                "path": str(Path(argv[i + 1]).resolve()),
            })
            i += 2
        else:
            i += 1

    # Positional argument is always the trailing text block.
    if args.prompt:
        blocks.append({"type": "text", "text": args.prompt})
    return blocks


def _main_import_cc(argv: list[str]) -> int:
    """`letscode import-cc <cc.jsonl> [--out path] [--report path]`.

    Convert a Claude Code session transcript into a letscode session feed.
    Default output is `.letscode/sessions/<cc-stem>.jsonl`; the analysis
    report prints to stdout unless --report gives a file path.
    """
    import uuid
    from .importers.cc import convert_cc_session
    from .importers.report import render_report_md

    p = argparse.ArgumentParser(
        prog="letscode import-cc",
        description="Convert a Claude Code session jsonl to a letscode session feed.",
    )
    p.add_argument("cc_session", help="Path to the Claude Code <session>.jsonl file")
    p.add_argument("--out", help="Output letscode feed path (default: .letscode/sessions/<stem>.jsonl)")
    p.add_argument("--report", help="Write the CC-vs-letscode analysis markdown to this path (default: stdout)")
    args = p.parse_args(argv)

    cc_path = Path(args.cc_session).resolve()
    if not cc_path.is_file():
        print(f"Error: {cc_path} not found", file=sys.stderr)
        return 1

    out_path = args.out
    if not out_path:
        sessions_dir = Path.cwd() / ".letscode" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        # Use a short uuid suffix so repeated imports of the same CC file don't collide.
        out_path = str(sessions_dir / f"{cc_path.stem[:8]}_{uuid.uuid4().hex[:4]}.jsonl")

    report = convert_cc_session(str(cc_path), out_path)
    md = render_report_md(report)

    if args.report:
        Path(args.report).write_text(md, encoding="utf-8")
        print(f"Report written to {args.report}", file=sys.stderr)
    else:
        print(md)

    print(f"Converted {report.total_lines} records -> {report.converted_events} events", file=sys.stderr)
    print(f"Feed written to {out_path}", file=sys.stderr)
    return 0


def main():
    # Subcommand dispatch: `letscode import-cc ...` runs the Claude Code
    # session importer and exits before the agent argparse runs.
    if len(sys.argv) >= 2 and sys.argv[1] == "import-cc":
        return _main_import_cc(sys.argv[2:])

    parser = argparse.ArgumentParser(
        prog="letscode",
        description="Lightweight Python AI agent harness",
    )
    parser.add_argument(
        "prompt",
        help="The task prompt to send to the agent",
        nargs="?",
        default=None,
    )
    parser.add_argument(
        "--models",
        help="List available models and exit",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--list-agents",
        help="List available agent cards (from agents/) and exit",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--as",
        dest="agent",
        help="Run as the named agent card (from agents/<Name>.md). The card's "
             "frontmatter overrides tools/skills/mcp_servers/rules and its body "
             "becomes the system prompt.",
        default=None,
    )
    parser.add_argument(
        "--config", "-c",
        help="Path to config file (JSON)",
        default=None,
    )
    parser.add_argument(
        "--model", "-m",
        help="Model ID to use (overrides default_model in config)",
        default=None,
    )
    parser.add_argument(
        "--max-turns",
        help="Maximum number of agent loop turns (default: unlimited)",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--workspace", "-w",
        help="Working directory for the agent (default: current directory)",
        default=None,
    )
    parser.add_argument(
        "--verbose", "-v",
        help="Show detailed tool call info",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--no-mcp",
        help="Skip MCP server connections (used internally for sub-agents)",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--event-stream",
        help="Output JSONL event stream to stdout instead of human-readable text",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--feed",
        help="Load conversation history from a JSONL log file for multi-turn",
        default=None,
    )
    parser.add_argument(
        "--output",
        help="Write consolidated agent output to a file. Format depends on mode: "
             "text (default), verbose (with -v), or JSONL feed (with --event-stream)",
        default=None,
    )
    parser.add_argument(
        "--append",
        help="Sugar: --feed X --append expands to --feed X --output X --event-stream",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--no-sandbox", "-ns",
        help="Disable sandbox entirely",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--preset", "-p",
        help="Sandbox preset: safe (read-only), default (workspace writable), risk (full R/W)",
        choices=["safe", "default", "risk"],
        default=None,
    )
    parser.add_argument(
        "--allow",
        help="Pre-approve a permission denial. Format: <type>:<target>, "
             "where <type> is read|write|cmd and <target> is the exact path "
             "or command. Repeatable. More specific than any deny rule, so a "
             "--allow grant overrides a broad deny (the escape hatch). Used "
             "internally by the ACP server to resume after a permission popup.",
        action="append",
        default=None,
    )
    parser.add_argument(
        "--effort",
        help="Reasoning effort tier (model must declare effort_options in config). "
             "Defaults to the first listed option when not specified.",
        default=None,
    )
    parser.add_argument(
        "--add-scan-dir",
        dest="add_scan_dirs",
        help="Extra directory to scan for skills (<dir>/skills) and agent "
             "cards (<dir>/agents). Repeatable. Appended to config add_scan_dirs.",
        action="append",
        default=None,
    )
    parser.add_argument(
        "--state",
        help="Path to a shared state JSON file for multi-agent orchestration. "
             "Passed to hooks (via $LETSCODE_STATE env var) and forwarded to "
             "spawned sub-agents. The file is created if it doesn't exist.",
        default=None,
    )
    parser.add_argument(
        "--text",
        help="Prompt text block (repeatable; combined with --image in the "
             "order given on the command line)",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--image",
        help="Path to an image file to include as an image block "
             "(repeatable; interleaves with --text in command-line order)",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--version", "-V",
        help="Show version and exit",
        action="store_true",
        default=False,
    )
    args = parser.parse_args()

    if args.version:
        from . import __version__
        print(f"letscode {__version__}")
        return

    if args.models:
        models, default_model = list_models(args.config)
        for m in models:
            marker = " (default)" if m["model"] == default_model else ""
            print(f"{m['model']}{marker}")
        return

    if args.list_agents:
        from .config import load_scan_dirs
        scan_dirs = _merge_scan_dirs(load_scan_dirs(args.config), args.add_scan_dirs)
        cards = discover_agent_cards(scan_dirs=scan_dirs)
        if not cards:
            print("(no agent cards found; create agents/<Name>.md)")
            return
        # Lazy import to avoid YAML parse cost on the --models path
        from .agent_card import _parse_card
        for stem_lower, path in sorted(cards.items()):
            is_builtin = "builtin_agents" in str(path)
            tag = " (built-in)" if is_builtin else ""
            try:
                card = _parse_card(path.read_text(encoding="utf-8"))
                label = card.name or getattr(path, "stem", path.name[:-3])
                desc = f" — {card.description}" if card.description else ""
            except Exception:
                label, desc = getattr(path, "stem", path.name), " (parse error)"
            print(f"{label}{tag}{desc}")
        return

    if not args.prompt and not args.text and not args.image:
        parser.error("prompt is required: provide a positional argument, --text, or --image")

    try:
        rc = asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        # A genuine user Ctrl-C (asyncio.run turns SIGINT into KeyboardInterrupt
        # when it lands outside the task). The user initiated this, so the
        # notice is kept brief — but still emitted (School A: never empty
        # stdout) so a capturing parent gets a non-empty pipe. Exit 0: an
        # interrupt is not a process-level failure.
        _emit_termination_notice(
            "interrupted by user (Ctrl-C)",
            event_stream=args.event_stream, hub=None,
        )
        return
    except asyncio.CancelledError:
        # A cancellation that ESCAPED _async_main's own handler — rare (it
        # catches in-run cancels), but possible if one is raised inside the
        # finally teardown (e.g. MCP disconnect). Distinct from KeyboardInterrupt:
        # this is an internal cancellation, not a user action. Same School-A
        # contract (exit 0 + notice), but labeled as internal so the cause
        # isn't misattributed to the user.
        _emit_termination_notice(
            "run was cancelled internally before completion",
            event_stream=args.event_stream, hub=None,
        )
        return
    if rc:
        raise SystemExit(rc)
