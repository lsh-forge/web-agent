import json
import re
from collections import Counter
from typing import Any, Callable

from agentscope.agent import Agent
from agentscope.message import TextBlock
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

from .common import display_tool_name, tool_result_text


_RESULT_SECTION_PATTERN = re.compile(
    r"(?s)(?P<prefix>### Result\s*\n)(?P<body>.*?)(?=\n### [^\n]+|\Z)",
)


class BrowserEvaluateContextMiddleware(MiddlewareBase):
    """Compact oversized ``browser_evaluate`` results before model delivery.

    ``browser_evaluate`` can return a large JSON list when a page-level DOM
    container is selected by mistake. Such output is often mostly duplicated
    ancestor text, which both hides item-specific fields and destabilizes the
    following model turn. Small results remain byte-for-byte unchanged.
    """

    def __init__(
        self,
        max_utf8_bytes: int = 24000,
        max_string_utf8_bytes: int = 480,
    ) -> None:
        self.max_utf8_bytes = max_utf8_bytes
        self.max_string_utf8_bytes = max_string_utf8_bytes

    async def on_acting(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Any],
    ):
        tool_call = input_kwargs.get("tool_call")
        tool_name = display_tool_name(getattr(tool_call, "name", ""))
        async for item in next_handler(**input_kwargs):
            if tool_name != "browser_evaluate" or not isinstance(item, ToolResponse):
                yield item
                continue

            raw_text = tool_result_text(item)
            if len(raw_text.encode("utf-8")) <= self.max_utf8_bytes:
                yield item
                continue

            compact_text = self._compact(raw_text)
            if compact_text == raw_text:
                yield item
                continue

            metadata = dict(item.metadata)
            metadata["browser_evaluate_context_compacted"] = True
            metadata["browser_evaluate_raw_utf8_bytes"] = len(raw_text.encode("utf-8"))
            metadata["browser_evaluate_context_utf8_bytes"] = len(compact_text.encode("utf-8"))
            print(
                "[browser-evaluate-context] compacted browser_evaluate from %d to %d UTF-8 bytes"
                % (
                    metadata["browser_evaluate_raw_utf8_bytes"],
                    metadata["browser_evaluate_context_utf8_bytes"],
                ),
            )
            yield ToolResponse(
                id=item.id,
                state=item.state,
                metadata=metadata,
                content=[TextBlock(text=compact_text)],
            )

    def _compact(self, raw_text: str) -> str:
        raw_bytes = len(raw_text.encode("utf-8"))
        match = _RESULT_SECTION_PATTERN.search(raw_text)
        if match is None:
            return self._fallback_compact(raw_text, raw_bytes)

        try:
            value = json.loads(match.group("body").strip())
        except json.JSONDecodeError:
            return self._fallback_compact(raw_text, raw_bytes)

        compact_value, notes = self._compact_json(value)
        header_lines = [
            "### browser_evaluate context [COMPACTED]",
            f"- Raw tool-result UTF-8 bytes: {raw_bytes}",
            f"- Model context budget: {self.max_utf8_bytes} UTF-8 bytes",
            "- Large repeated or long JSON values were compacted; retained fields are the model input.",
        ]
        if notes:
            header_lines.extend(f"- {note}" for note in notes)

        compact_result = json.dumps(compact_value, ensure_ascii=False, indent=2)
        suffix = raw_text[match.end() :]
        result = "\n".join(header_lines) + "\n### Result\n" + compact_result + suffix
        if len(result.encode("utf-8")) <= self.max_utf8_bytes:
            return result

        return self._fit_json_result(header_lines, compact_value, suffix, raw_bytes)

    def _compact_json(self, value: Any) -> tuple[Any, list[str]]:
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            return self._compact_value(value), []

        repeated_values = Counter(
            (key, item[key])
            for item in value
            for key in item
            if isinstance(item[key], str)
            and len(item[key].encode("utf-8")) > self.max_string_utf8_bytes
        )
        notes: list[str] = []
        compact_items: list[dict[str, Any]] = []
        repeated_count = 0
        for item in value:
            compact_item: dict[str, Any] = {}
            for key, item_value in item.items():
                if (
                    isinstance(item_value, str)
                    and repeated_values[(key, item_value)] > 1
                    and len(item_value.encode("utf-8")) > self.max_string_utf8_bytes
                ):
                    compact_item[key] = (
                        "<omitted: identical long value repeated in "
                        f"{repeated_values[(key, item_value)]} list items>"
                    )
                    repeated_count += 1
                else:
                    compact_item[key] = self._compact_value(item_value)
            compact_items.append(compact_item)

        if repeated_count:
            notes.append(
                f"Omitted {repeated_count} repeated long field values across {len(value)} JSON items."
            )
        return compact_items, notes

    def _compact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._shorten_text(value, self.max_string_utf8_bytes)
        if isinstance(value, list):
            return [self._compact_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self._compact_value(item) for key, item in value.items()}
        return value

    def _fit_json_result(
        self,
        header_lines: list[str],
        compact_value: Any,
        suffix: str,
        raw_bytes: int,
    ) -> str:
        available = self.max_utf8_bytes - len(("\n".join(header_lines) + "\n### Result\n" + suffix).encode("utf-8"))
        if isinstance(compact_value, list):
            retained: list[Any] = []
            omitted = 0
            for index, item in enumerate(compact_value):
                candidate = retained + [item]
                if len(json.dumps(candidate, ensure_ascii=False, indent=2).encode("utf-8")) > available:
                    omitted = len(compact_value) - index
                    break
                retained.append(item)
            if omitted:
                retained.append(
                    {
                        "_context_omitted_items": omitted,
                        "_reason": "JSON list exceeded the model context budget after per-field compaction.",
                    },
                )
            compact_value = retained
        else:
            compact_value = self._shorten_text(
                json.dumps(compact_value, ensure_ascii=False),
                max(256, available),
            )

        result = "\n".join(header_lines) + "\n### Result\n" + json.dumps(
            compact_value,
            ensure_ascii=False,
            indent=2,
        ) + suffix
        if len(result.encode("utf-8")) <= self.max_utf8_bytes:
            return result
        return self._fallback_compact(result, raw_bytes)

    def _fallback_compact(self, raw_text: str, raw_bytes: int) -> str:
        marker = (
            "### browser_evaluate context [COMPACTED]\n"
            f"- Raw tool-result UTF-8 bytes: {raw_bytes}\n"
            f"- Model context budget: {self.max_utf8_bytes} UTF-8 bytes\n"
            "- The tool result was not a JSON list; its beginning and ending are retained.\n"
        )
        omitted_marker = "\n... <omitted middle of oversized tool result> ...\n"
        budget = max(
            0,
            self.max_utf8_bytes - len(marker.encode("utf-8")) - len(omitted_marker.encode("utf-8")),
        )
        head_budget = budget * 2 // 3
        head = self._shorten_text(raw_text, head_budget)
        remaining = max(0, budget - len(head.encode("utf-8")))
        tail = self._tail_text(raw_text, remaining)
        if tail and tail != head:
            return marker + head + omitted_marker + tail
        return marker + head

    @staticmethod
    def _shorten_text(text: str, max_utf8_bytes: int) -> str:
        if len(text.encode("utf-8")) <= max_utf8_bytes:
            return text
        suffix = "... <truncated>"
        kept: list[str] = []
        used = len(suffix.encode("utf-8"))
        for character in text:
            char_bytes = len(character.encode("utf-8"))
            if used + char_bytes > max_utf8_bytes:
                break
            kept.append(character)
            used += char_bytes
        return "".join(kept) + suffix

    @staticmethod
    def _tail_text(text: str, max_utf8_bytes: int) -> str:
        if len(text.encode("utf-8")) <= max_utf8_bytes:
            return text
        kept: list[str] = []
        used = 0
        for character in reversed(text):
            char_bytes = len(character.encode("utf-8"))
            if used + char_bytes > max_utf8_bytes:
                break
            kept.append(character)
            used += char_bytes
        return "".join(reversed(kept))
