"""Read tool — file reading with line numbers."""

from pathlib import Path
from typing import Any

SCHEMA = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": (
            "Reads a file from the local filesystem. You can access any file directly "
            "by using this tool.\n"
            "Assume this tool is able to read all files on the machine. If the User "
            "provides a path to a file assume that path is valid. It is okay to read a "
            "file that does not exist; an error will be returned.\n\n"
            "Usage:\n"
            "- The file_path parameter must be an absolute path, not a relative path\n"
            "- By default, it reads up to 2000 lines starting from the beginning of the file\n"
            "- You can optionally specify a line offset and limit (especially handy for long "
            "files), but it's recommended to read the whole file by not providing these parameters\n"
            "- Results are returned using cat -n format, with line numbers starting at 1\n"
            "- This tool can only read files, not directories. To read a directory, use an ls "
            "command via the Bash tool.\n"
            "- If you read a file that exists but has empty contents you will receive a system "
            "reminder warning in place of file contents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to read",
                },
                "offset": {
                    "type": "integer",
                    "description": "The line number to start reading from. Only provide if the file is too large to read at once",
                },
                "limit": {
                    "type": "integer",
                    "description": "The number of lines to read. Only provide if the file is too large to read at once.",
                },
            },
            "required": ["file_path"],
        },
    },
}


def execute(args: dict[str, Any], *, validate_path=None, **_) -> str:
    file_path = args.get("file_path", "")
    offset = args.get("offset")
    limit = args.get("limit")

    try:
        if validate_path:
            if err := validate_path("read", file_path):
                return err

        p = Path(file_path).expanduser().resolve()
        if p.is_dir():
            return f"<error>{file_path} is a directory, not a file. Use ls via Bash to list directory contents.</error>"
        if not p.exists():
            return f"<error>File not found: {file_path}</error>"
        if not p.is_file():
            return f"<error>Not a regular file: {file_path}</error>"

        with open(p) as f:
            lines = f.readlines()

        start = (offset - 1) if offset else 0
        end = start + limit if limit else len(lines)
        selected = lines[start:end]

        numbered = []
        for i, line in enumerate(selected, start=(start + 1)):
            content = line.rstrip("\n")
            numbered.append(f"{i:>6}\t{content}")

        return "\n".join(numbered)
    except Exception as e:
        return f"<error>{e}</error>"
