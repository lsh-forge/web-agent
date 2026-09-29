from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from agentscope.agent import Agent
from agentscope.event import (
    AgentEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.middleware import MiddlewareBase

from .common import display_tool_name, msg_text, pretty_json, text_key, tool_result_text


class WorkerLogMiddleware(MiddlewareBase):
    """Append model and tool details to the current task log."""

    def __init__(self, worker_index: int, log_file: Path) -> None:
        self.worker_index = worker_index
        self.log_file = log_file
        self.round = 0
        self.initialized = False
        self.user_prompt_logged = False
        self.system_prompt_logged = False
        self.text_buffers: dict[str, list[str]] = {}
        self.thinking_buffers: dict[str, list[str]] = {}
        self.tool_calls: dict[str, dict[str, str]] = {}
        self.tool_result_buffers: dict[str, list[str]] = {}
        self.logged_compaction_summaries: set[str] = set()

    async def on_system_prompt(self, agent: Agent, current_prompt: str) -> str:
        self._init_log_file()
        if not self.system_prompt_logged:
            self.system_prompt_logged = True
            self._append_section("系统提示词", current_prompt)
        return current_prompt

    async def on_reply(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]):
        self._init_log_file()
        self._log_user_prompt(input_kwargs.get("inputs"))
        try:
            async for event in next_handler(**input_kwargs):
                yield event
        except Exception as exc:
            self._record_error(exc)
            raise

    async def on_model_call(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]):
        self.round += 1
        self._init_log_file()
        self._append_line("\n" + "=" * 28 + f" 第 {self.round} 轮模型调用 " + "=" * 28 + "\n")
        self._log_compaction_summaries(input_kwargs.get("messages") or [])
        try:
            return await next_handler(**input_kwargs)
        except Exception as exc:
            self._record_error(exc)
            raise

    async def on_reasoning(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]):
        try:
            async for event in next_handler(**input_kwargs):
                self._record_reasoning_event(event)
                yield event
        except Exception as exc:
            self._record_error(exc)
            raise

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]):
        tool_call = input_kwargs.get("tool_call")
        self._record_tool_use(tool_call)
        try:
            async for event in next_handler(**input_kwargs):
                self._record_tool_result_item(tool_call, event)
                yield event
        except Exception as exc:
            self._record_error(exc)
            raise

    async def on_compress_context(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]) -> None:
        # 这里每轮都会进入压缩检查，不能直接记为“已触发压缩”。
        # 真正产生摘要后，会在下一次模型调用的 messages 中由 _log_compaction_summaries 记录。
        await next_handler(**input_kwargs)

    def record_runtime_info(self, title: str, body: str) -> None:
        """Write task setup facts that are useful when diagnosing a run."""
        self._init_log_file()
        self._append_section(title, body)

    def _log_user_prompt(self, inputs: Any) -> None:
        if self.user_prompt_logged:
            return
        self.user_prompt_logged = True
        messages = inputs if isinstance(inputs, list) else [inputs]
        text = "\n\n".join(msg_text(msg) for msg in messages if msg is not None)
        self._append_section("用户提示词", text)

    def _log_compaction_summaries(self, messages: list[Any]) -> None:
        for message in messages:
            text = msg_text(message)
            if (
                "__compaction_summary__" in str(getattr(message, "name", ""))
                or "Here is a summary" in text
                or "<summary>" in text
            ):
                key = getattr(message, "id", text)
                if key not in self.logged_compaction_summaries:
                    self.logged_compaction_summaries.add(key)
                    self._append_section("上下文压缩，压缩摘要", text)

    def _record_reasoning_event(self, event: AgentEvent) -> None:
        if isinstance(event, ThinkingBlockDeltaEvent):
            self._append(self.thinking_buffers, text_key(event.reply_id, event.block_id), event.delta)
            return
        if isinstance(event, ThinkingBlockEndEvent):
            thinking = self._take(self.thinking_buffers, text_key(event.reply_id, event.block_id))
            if thinking.strip():
                self._append_section("模型思考", thinking)
            return
        if isinstance(event, TextBlockDeltaEvent):
            self._append(self.text_buffers, text_key(event.reply_id, event.block_id), event.delta)
            return
        if isinstance(event, TextBlockEndEvent):
            output = self._take(self.text_buffers, text_key(event.reply_id, event.block_id))
            if output.strip():
                self._append_section("模型输出", output)

    def _record_tool_use(self, tool_call: Any) -> None:
        if tool_call is None:
            return
        tool_id = getattr(tool_call, "id", "")
        tool_name = display_tool_name(getattr(tool_call, "name", ""))
        tool_input = getattr(tool_call, "input", "")
        self.tool_calls[tool_id] = {"name": tool_name, "input": tool_input}
        self._append_section(
            "工具调用",
            "名称: " + tool_name + "\nID: " + tool_id + "\n参数:\n" + pretty_json(tool_input),
        )

    def _record_tool_result_event(self, event: AgentEvent) -> None:
        if isinstance(event, ToolResultTextDeltaEvent):
            self._append(self.tool_result_buffers, event.tool_call_id, event.delta)
            return
        if isinstance(event, ToolResultDataDeltaEvent):
            self._append(self.tool_result_buffers, event.tool_call_id, str(getattr(event, "data", "") or getattr(event, "url", "")))
            return
        if isinstance(event, ToolResultEndEvent):
            result = self._take(self.tool_result_buffers, event.tool_call_id)
            tool_use = self.tool_calls.pop(event.tool_call_id, {})
            tool_name = tool_use.get("name", getattr(event, "tool_call_name", ""))
            self._append_section(
                "工具结果",
                "名称: " + str(tool_name)
                + "\nID: " + event.tool_call_id
                + "\n状态: " + str(event.state)
                + "\n结果:\n" + (result.strip() or "<null>"),
            )

    def _record_tool_result_item(self, tool_call: Any, item: Any) -> None:
        tool_call_id = getattr(tool_call, "id", "")
        if not tool_call_id:
            return
        item_type = item.__class__.__name__
        text = tool_result_text(item)
        if item_type != "ToolResponse" and text:
            self._append(self.tool_result_buffers, tool_call_id, text)
            return

        if item_type != "ToolResponse":
            return

        result = text or self._take(self.tool_result_buffers, tool_call_id)
        tool_use = self.tool_calls.pop(tool_call_id, {})
        tool_name = tool_use.get("name", display_tool_name(getattr(tool_call, "name", "")))
        state = getattr(item, "state", "")
        self._append_section(
            "工具结果",
            "名称: " + str(tool_name)
            + "\nID: " + tool_call_id
            + "\n状态: " + str(state)
            + "\n结果:\n" + (result.strip() or "<null>"),
        )

    def _append(self, buffers: dict[str, list[str]], key: str, value: str) -> None:
        buffers.setdefault(key, []).append(value or "")

    def _take(self, buffers: dict[str, list[str]], key: str) -> str:
        return "".join(buffers.pop(key, []))

    def _append_section(self, title: str, body: str) -> None:
        self._append_line(f"\n--- {title} | {datetime.now().astimezone().isoformat()} ---\n{(body or '<null>').strip()}\n")

    def _append_line(self, text: str) -> None:
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as file:
                file.write(text)
        except Exception:
            # 日志失败不能影响 Agent 主流程。
            pass

    def _init_log_file(self) -> None:
        if self.initialized:
            return
        self.initialized = True
        self._append_line(
            "\n"
            + "#" * 90
            + f"\nWorker: {self.worker_index}"
            + f"\nStartedAt: {datetime.now().astimezone().isoformat()}"
            + f"\nLogFile: {self.log_file.resolve()}"
            + "\n"
            + "#" * 90
            + "\n"
        )

    def _record_error(self, error: BaseException) -> None:
        self._append_section("异常", f"{error.__class__.__name__}: {error}")
