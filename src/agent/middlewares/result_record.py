from pathlib import Path
import re
from typing import Any, Callable

from agentscope.agent import Agent
from agentscope.event import (
    AgentEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
)
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

from .common import atomic_json_dump, format_action, text_key, tool_result_text


REFERENCE_LENGTH = 100
PAGE_URL_PATTERN = re.compile(r"(?m)^- Page URL:\s*(\S+)\s*$")


class ResultRecordMiddleware(MiddlewareBase):
    """Collect and write result.json fields required by the template."""

    def __init__(
        self,
        task_idx: int,
        task_id: str,
        task: str,
        website: str,
        page_tracker: Any,
        task_output_dir: Path,
    ) -> None:
        self.task_idx = task_idx
        self.task_id = task_id
        self.task = task
        self.website = website
        self.page_tracker = page_tracker
        self.result_path = task_output_dir / "result.json"
        self.actions: list[str] = []
        self.thoughts: list[str] = []
        self.history_resps: list[str] = []
        self.urls: list[str] = []
        self.agent_answer = ""
        self.final_result_response = ""
        self.thinking_buffers: dict[str, list[str]] = {}
        self.text_buffers: dict[str, list[str]] = {}

    async def on_model_call(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]):
        # 与官方示例的 agent.urls.append(page.url) 对齐：每轮模型决策前记录当时页面地址。
        # 不依赖 snapshot 是否被调用，因此导航、点击、输入后的页面状态都能完整回放。
        current_url = str(getattr(self.page_tracker.page, "url", "") or "")
        if current_url:
            self.urls.append(current_url)
        return await next_handler(**input_kwargs)

    async def on_reasoning(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]):
        async for event in next_handler(**input_kwargs):
            self._record_reasoning_event(event)
            yield event

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]):
        tool_call = input_kwargs.get("tool_call")
        if tool_call is not None:
            # actions 对齐示例项目：记录工具名和输入参数，作为预测动作序列。
            self.actions.append(format_action(tool_call))
        async for event in next_handler(**input_kwargs):
            if isinstance(event, ToolResponse):
                # MCP returns the page URL after the action. Record it immediately so
                # the trajectory includes the state transition even when no later model
                # call is needed or a single-page app updates the visible state via XHR.
                self._record_page_url_from_tool(event)
            yield event

    def _record_page_url_from_tool(self, event: ToolResponse) -> None:
        page_urls = PAGE_URL_PATTERN.findall(tool_result_text(event))
        if page_urls:
            self.urls.append(page_urls[-1])

    def _record_reasoning_event(self, event: AgentEvent) -> None:
        if isinstance(event, ThinkingBlockDeltaEvent):
            self._append(self.thinking_buffers, text_key(event.reply_id, event.block_id), event.delta)
            return
        if isinstance(event, ThinkingBlockEndEvent):
            thinking = self._take(self.thinking_buffers, text_key(event.reply_id, event.block_id)).strip()
            if thinking:
                # thoughts 只保存模型显式输出的思考块；模型不输出则保持为空。
                self.thoughts.append(thinking)
            return
        if isinstance(event, TextBlockDeltaEvent):
            self._append(self.text_buffers, text_key(event.reply_id, event.block_id), event.delta)
            return
        if isinstance(event, TextBlockEndEvent):
            output = self._take(self.text_buffers, text_key(event.reply_id, event.block_id)).strip()
            if output:
                # 最后一段模型文本同时作为 history_resps、final_result_response 和 agent_answer。
                self.history_resps.append(output)
                self.final_result_response = output
                self.agent_answer = output

    def write_success(self) -> dict[str, Any]:
        return self._write("SUCCESS")

    def write_failed(self, error: str) -> dict[str, Any]:
        self.final_result_response = self.final_result_response or error
        self.agent_answer = self.agent_answer or error
        return self._write("FAIL")

    def _write(self, status: str) -> dict[str, Any]:
        # 字段名称和数量严格对齐官方示例项目，不额外添加 finished_at 等自定义字段。
        item = {
            "task_idx": self.task_idx,
            "task_id": self.task_id,
            "task": self.task,
            "website": self.website,
            "status": status,
            "reference_length": REFERENCE_LENGTH,
            "predict_length": len(self.actions),
            "agent_answer": self.agent_answer,
            "final_result_response": self.final_result_response,
            "actions": list(self.actions),
            "thoughts": list(self.thoughts),
            "history_resps": list(self.history_resps),
            "urls": list(self.urls),
        }
        self.result_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_dump(self.result_path, item)
        return item

    def _append(self, buffers: dict[str, list[str]], key: str, value: str) -> None:
        buffers.setdefault(key, []).append(value or "")

    def _take(self, buffers: dict[str, list[str]], key: str) -> str:
        return "".join(buffers.pop(key, []))
