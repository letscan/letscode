"""Session metadata management and persistence."""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Session:
    session_id: str
    cwd: str
    created_at: str
    log_path: str | None = None
    title: str | None = None
    mode: str = "default"
    model: str | None = None
    reasoning_effort: str | None = None


def _sessions_dir(cwd: str) -> Path:
    """Return the sessions directory for a given cwd."""
    d = Path(cwd) / ".letscode" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_path(session_id: str, cwd: str) -> Path:
    return _sessions_dir(cwd) / f"{session_id}.json"


def create_session(cwd: str) -> Session:
    """Create a new session with a UUID4."""
    now = datetime.now(timezone.utc)
    session = Session(
        session_id=uuid.uuid4().hex,
        cwd=cwd,
        created_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    save_session(session)
    return session


def save_session(session: Session) -> None:
    """Write session metadata to JSON file."""
    path = _meta_path(session.session_id, session.cwd)
    data = {
        "session_id": session.session_id,
        "cwd": session.cwd,
        "created_at": session.created_at,
        "title": session.title,
        "log_path": session.log_path,
        "mode": session.mode,
        "model": session.model,
        "reasoning_effort": session.reasoning_effort,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session_meta(session_id: str, cwd: str) -> Session | None:
    """Load session metadata from JSON file."""
    path = _meta_path(session_id, cwd)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Session(
            session_id=data["session_id"],
            cwd=data.get("cwd", cwd),
            created_at=data.get("created_at", ""),
            log_path=data.get("log_path"),
            title=data.get("title"),
            mode=data.get("mode", "default"),
            model=data.get("model"),
            reasoning_effort=data.get("reasoning_effort"),
        )
    except (json.JSONDecodeError, KeyError):
        return None


def list_sessions(
    cwd: str | None = None,
    cursor: str | None = None,
    page_size: int = 25,
) -> tuple[list[Session], str | None]:
    """List sessions sorted by creation time descending.

    Returns (sessions, next_cursor or None).
    """
    if cwd is None:
        return [], None

    sdir = _sessions_dir(cwd)
    if not sdir.exists():
        return [], None

    sessions: list[Session] = []
    for meta_file in sorted(sdir.glob("*.json"), reverse=True):
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            sessions.append(Session(
                session_id=data["session_id"],
                cwd=data.get("cwd", cwd),
                created_at=data.get("created_at", ""),
                log_path=data.get("log_path"),
                title=data.get("title"),
                mode=data.get("mode", "default"),
                model=data.get("model"),
                reasoning_effort=data.get("reasoning_effort"),
            ))
        except (json.JSONDecodeError, KeyError):
            continue

    # Cursor-based pagination: skip sessions until we pass the cursor
    if cursor:
        for i, s in enumerate(sessions):
            if s.session_id == cursor:
                sessions = sessions[i + 1:]
                break
        else:
            sessions = []

    if len(sessions) > page_size:
        return sessions[:page_size], sessions[page_size].session_id
    return sessions, None
