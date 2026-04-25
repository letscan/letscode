"""Write tool — file creation and overwrite."""

from pathlib import Path
from typing import Any

SCHEMA = {
    "type": "function",
    "function": {
        "name": "Write",
        "description": (
            "Writes a file to the local filesystem.\n\n"
            "Usage:\n"
            "- This tool will overwrite the existing file if there is one at the provided path.\n"
            "- Prefer the Edit tool for modifying existing files — it only sends the diff. "
            "Only use this tool to create new files or for complete rewrites.\n"
            "- NEVER create documentation files (*.md) or README files unless explicitly "
            "requested by the User.\n"
            "- Only use emojis if the user explicitly requests it. Avoid writing emojis to "
            "files unless asked."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to write (must be absolute, not relative)",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file",
                },
            },
            "required": ["file_path", "content"],
        },
    },
}


def execute(args: dict[str, Any]) -> str:
    file_path = args.get("file_path", "")
    content = args.get("content", "")

    try:
        p = Path(file_path).expanduser()
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        n_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        if existed:
            return f"The file {file_path} has been updated. ({n_lines} lines)"
        return f"File created successfully at: {file_path} ({n_lines} lines)"
    except Exception as e:
        return f"<error>{e}</error>"
