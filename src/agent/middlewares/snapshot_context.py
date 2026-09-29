from typing import Any, Callable

from agentscope.agent import Agent
from agentscope.message import TextBlock
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

from .common import display_tool_name, tool_result_text


_PRIMARY_CONTENT_ROLES = ("main", "article", "tabpanel", "dialog", "form", "table")
_SEMANTIC_ROLES = (
    "heading",
    "paragraph",
    "button",
    "link",
    "textbox",
    "combobox",
    "checkbox",
    "radio",
    "tab",
    "menuitem",
    "option",
    "row",
    "cell",
    "listitem",
    "alert",
    "status",
)


class SnapshotContextMiddleware(MiddlewareBase):
    """Reduce oversized accessibility snapshots before they enter model context."""

    def __init__(self, max_utf8_bytes: int = 48000) -> None:
        self.max_utf8_bytes = max_utf8_bytes

    async def on_acting(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Any],
    ):
        tool_call = input_kwargs.get("tool_call")
        tool_name = display_tool_name(getattr(tool_call, "name", ""))
        async for item in next_handler(**input_kwargs):
            if tool_name != "browser_snapshot" or not isinstance(item, ToolResponse):
                yield item
                continue

            raw_text = tool_result_text(item)
            compact_text = self._compact(raw_text)
            if compact_text == raw_text:
                yield item
                continue

            metadata = dict(item.metadata)
            metadata["snapshot_context_compacted"] = True
            metadata["snapshot_raw_utf8_bytes"] = len(raw_text.encode("utf-8"))
            metadata["snapshot_context_utf8_bytes"] = len(compact_text.encode("utf-8"))
            print(
                "[snapshot-context] compacted browser_snapshot from %d to %d UTF-8 bytes"
                % (metadata["snapshot_raw_utf8_bytes"], metadata["snapshot_context_utf8_bytes"]),
            )
            yield ToolResponse(
                id=item.id,
                state=item.state,
                metadata=metadata,
                content=[TextBlock(text=compact_text)],
            )

    def _compact(self, raw_text: str) -> str:
        if len(raw_text.encode("utf-8")) <= self.max_utf8_bytes:
            return raw_text

        raw_bytes = len(raw_text.encode("utf-8"))
        marker = (
            "### Snapshot context [TRUNCATED]\n"
            f"- Raw snapshot UTF-8 bytes: {raw_bytes}\n"
            f"- Model context budget: {self.max_utf8_bytes} UTF-8 bytes\n"
            "- The following is a semantic accessibility outline of the original snapshot.\n"
        )
        lines = raw_text.splitlines()
        snapshot_start = next(
            (index for index, line in enumerate(lines) if line.strip() == "### Snapshot"),
            0,
        )
        metadata = list(range(min(snapshot_start + 8, len(lines))))
        (
            primary_headings,
            primary_controls,
            primary_details,
            tab_controls,
            fallback_headings,
            fallback_controls,
        ) = _semantic_snapshot_lines(
            lines,
            snapshot_start,
        )

        compact_lines: list[str] = []
        for label, indices in (
            ("Page metadata", metadata),
            ("Primary content headings", primary_headings),
            ("Tab navigation", tab_controls),
            ("Primary content controls", primary_controls),
            ("Primary content details", primary_details),
            ("Other section headings", fallback_headings),
            ("Other interactive controls", fallback_controls),
        ):
            _append_outline_group(compact_lines, label, indices, lines)

        body_budget = max(0, self.max_utf8_bytes - len(marker.encode("utf-8")))
        return marker + _fit_utf8_budget(compact_lines, body_budget)


def _semantic_snapshot_lines(
    lines: list[str],
    snapshot_start: int,
) -> tuple[list[int], list[int], list[int], list[int], list[int], list[int]]:
    primary_headings: set[int] = set()
    primary_controls: set[int] = set()
    primary_details: set[int] = set()
    tab_controls: set[int] = set()
    global_headings: set[int] = set()
    global_controls: set[int] = set()
    primary_indents: list[int] = []

    for index in range(snapshot_start, len(lines)):
        line = lines[index]
        indent = len(line) - len(line.lstrip())
        while primary_indents and indent <= primary_indents[-1]:
            primary_indents.pop()
        lowered = line.casefold()
        if _has_role(lowered, _PRIMARY_CONTENT_ROLES):
            primary_indents.append(indent)

        if not _is_semantic_node(lowered):
            continue
        is_heading = _has_role(lowered, ("heading",))
        is_control = _is_interactive_node(lowered)
        if is_heading:
            global_headings.add(index)
        if is_control:
            global_controls.add(index)
        if _has_role(lowered, ("tab",)):
            tab_controls.add(index)
        if primary_indents:
            if is_heading:
                primary_headings.add(index)
            elif is_control:
                primary_controls.add(index)
            else:
                primary_details.add(index)

    # Some sites expose no main/article landmark. Headings and controls are a
    # stable, language-agnostic fallback for those pages.
    if not (primary_headings or primary_controls or primary_details):
        primary_headings.update(global_headings)
        primary_controls.update(global_controls)
    return (
        sorted(primary_headings),
        sorted(primary_controls),
        sorted(primary_details),
        sorted(tab_controls),
        sorted(global_headings - primary_headings),
        sorted(global_controls - primary_controls),
    )


def _is_semantic_node(lowered_line: str) -> bool:
    return _has_role(lowered_line, _SEMANTIC_ROLES)


def _is_interactive_node(lowered_line: str) -> bool:
    return _has_role(
        lowered_line,
        ("button", "link", "textbox", "combobox", "checkbox", "radio", "tab", "menuitem", "option"),
    )


def _has_role(line: str, roles: tuple[str, ...]) -> bool:
    node = line.lstrip().removeprefix("- ").split(None, 1)
    return bool(node) and node[0] in roles


def _include_window(selected: set[int], index: int, length: int, *, before: int, after: int) -> None:
    selected.update(range(max(0, index - before), min(length, index + after + 1)))


def _append_outline_group(output: list[str], label: str, indices: list[int], lines: list[str]) -> None:
    unique_indices = [index for index in indices if lines[index] not in output]
    if not unique_indices:
        return
    output.append(f"### {label}")
    previous_index: int | None = None
    for index in unique_indices:
        if previous_index is not None and index > previous_index + 1:
            output.append("... <omitted unrelated snapshot content> ...")
        output.append(lines[index])
        previous_index = index


def _fit_utf8_budget(lines: list[str], max_utf8_bytes: int) -> str:
    result: list[str] = []
    used = 0
    for line in lines:
        encoded_size = len((line + "\n").encode("utf-8"))
        if used + encoded_size > max_utf8_bytes:
            break
        result.append(line)
        used += encoded_size
    return "\n".join(result)
