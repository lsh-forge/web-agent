import json
import os
import re
from pathlib import Path
from typing import Any


def msg_text(message: Any) -> str:
    if message is None:
        return ""
    if hasattr(message, "get_text_content"):
        return message.get_text_content() or ""
    return str(message)


def text_key(reply_id: str, block_id: str) -> str:
    return f"{reply_id}:{block_id}"


def pretty_json(value: Any) -> str:
    try:
        if isinstance(value, str):
            value = json.loads(value)
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def format_action(tool_call: Any) -> str:
    return f"{display_tool_name(getattr(tool_call, 'name', ''))}({pretty_json(getattr(tool_call, 'input', ''))})"


def tool_result_text(item: Any) -> str:
    blocks = getattr(item, "content", None)
    if not blocks:
        return ""
    parts = []
    for block in blocks:
        if hasattr(block, "text"):
            parts.append(str(block.text))
            continue
        source = getattr(block, "source", None)
        url = getattr(source, "url", None)
        data = getattr(source, "data", None)
        if url:
            parts.append(str(url))
        elif data:
            parts.append(str(data))
    return "".join(parts)


def display_tool_name(name: str) -> str:
    match = re.match(r"^mcp__[^_]+__(.+)$", name or "")
    return match.group(1) if match else (name or "")


def atomic_json_dump(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)
