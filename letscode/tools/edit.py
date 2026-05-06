"""Edit tool — exact string replacement in files."""

from pathlib import Path
from typing import Any

from ._types import check_write_allowed

SCHEMA = {
    "type": "function",
    "function": {
        "name": "Edit",
        "description": (
            "Performs exact string replacements in files.\n\n"
            "Usage:\n"
            "- You must use your `Read` tool at least once in the conversation before editing. "
            "This tool will error if you attempt an edit without reading the file.\n"
            "- When editing text from Read tool output, ensure you preserve the exact indentation "
            "(tabs/spaces) as it appears AFTER the line number prefix. The line number prefix "
            "format is: line number + tab. Everything after that is the actual file content to "
            "match. Never include any part of the line number prefix in the old_string or "
            "new_string.\n"
            "- ALWAYS prefer editing existing files in the codebase. NEVER write new files "
            "unless explicitly required.\n"
            "- Only use emojis if the user explicitly requests it. Avoid adding emojis to files "
            "unless asked.\n"
            "- The edit will FAIL if `old_string` is not unique in the file. Either provide a "
            "larger string with more surrounding context to make it unique or use `replace_all` "
            "to change every instance of `old_string`.\n"
            "- Use `replace_all` for replacing and renaming strings across the file. This "
            "parameter is useful if you want to rename a variable for instance."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to modify",
                },
                "old_string": {
                    "type": "string",
                    "description": "The text to replace",
                },
                "new_string": {
                    "type": "string",
                    "description": "The text to replace it with (must be different from old_string)",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences of old_string (default false)",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
}


def execute(args: dict[str, Any]) -> str:
    file_path = args.get("file_path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    replace_all = args.get("replace_all", False)

    try:
        if err := check_write_allowed(file_path):
            return err

        p = Path(file_path).expanduser().resolve()
        if not p.exists():
            return f"<error>File not found: {file_path}</error>"

        content = p.read_text()

        if old_string not in content:
            return (
                f"<error>old_string not found in {file_path}. "
                "Make sure the string matches exactly, including whitespace and indentation.</error>"
            )

        if not replace_all:
            count = content.count(old_string)
            if count > 1:
                return (
                    f"<error>old_string appears {count} times in {file_path}. "
                    "Either provide a larger string with more surrounding context to make it unique, "
                    "or use replace_all: true to change every instance.</error>"
                )

        if replace_all:
            n = content.count(old_string)
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        p.write_text(new_content)

        old_lines = old_string.count("\n") + 1
        new_lines = new_string.count("\n") + 1
        if replace_all:
            return f"The file {file_path} has been updated. Replaced {n} occurrences ({old_lines} lines -> {new_lines} lines each)."
        return f"The file {file_path} has been updated. ({old_lines} line{'s' if old_lines > 1 else ''} -> {new_lines} line{'s' if new_lines > 1 else ''})"

    except Exception as e:
        return f"<error>{e}</error>"
