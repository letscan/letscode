"""Parse JSONL event logs and reconstruct conversation messages."""

from .feed_util import read_events
from .subscribers import MessageSubscriber


def load_feed(path: str) -> tuple[str, list[dict]]:
    """Load a JSONL event log and rebuild the messages list.

    Returns (original_model, messages) where messages excludes the system prompt.
    The caller should prepend a system prompt before using these messages.
    """
    events = read_events(path)
    if not events:
        return "", []

    model = ""
    sub = MessageSubscriber()

    for ev in events:
        type_ = ev.get("type", "")
        data = ev.get("data", {})

        if type_ == "init":
            model = data.get("model", "")

        sub(type_, data)

    sub.flush()
    return model, sub.messages
