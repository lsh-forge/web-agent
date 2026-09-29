import asyncio
from contextlib import suppress
from typing import Any, Callable

from agentscope.agent import Agent
from agentscope.middleware import MiddlewareBase
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse

from errors import MCPBrowserUnhealthyError, ToolExecutionTimeoutError


def _tool_response_text(value: Any) -> str:
    parts: list[str] = []
    for block in getattr(value, "content", []) or []:
        text = getattr(block, "text", None)
        parts.append(str(text if text is not None else block))
    return "\n".join(parts)


def _is_mcp_initialize_timeout(value: Any) -> bool:
    text = str(value).lower()
    if isinstance(value, ToolResponse):
        text = _tool_response_text(value).lower()
    return "initializeserver" in text and "timeout" in text


class ToolTimeoutMiddleware(MiddlewareBase):
    """Apply a timeout to local and MCP tool execution."""

    def __init__(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds
        self.consecutive_timeouts = 0
        self.consecutive_mcp_initialize_timeouts = 0

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]):
        queue: asyncio.Queue[Any] = asyncio.Queue()
        mcp_timeout_in_call = False

        async def consume() -> None:
            try:
                async for item in next_handler(**input_kwargs):
                    await queue.put(("item", item))
            except Exception as exc:
                await queue.put(("error", exc))
            finally:
                await queue.put(("done", None))

        task = asyncio.create_task(consume())
        try:
            while True:
                kind, value = await asyncio.wait_for(queue.get(), timeout=self.timeout_seconds)
                if kind == "item":
                    if _is_mcp_initialize_timeout(value):
                        mcp_timeout_in_call = True
                    yield value
                elif kind == "error":
                    if _is_mcp_initialize_timeout(value):
                        mcp_timeout_in_call = True
                        yield ToolResponse(
                            id=getattr(input_kwargs.get("tool_call"), "id", ""),
                            content=[
                                TextBlock(
                                    text=(
                                        "The browser MCP initialization timed out. "
                                        "Try another action once; repeated failures will end this task."
                                    ),
                                ),
                            ],
                            state=ToolResultState.ERROR,
                        )
                    else:
                        raise value
                else:
                    break
        except asyncio.TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.consecutive_timeouts += 1
            if self.consecutive_timeouts >= 3:
                raise ToolExecutionTimeoutError(
                    f"browser tool timed out {self.consecutive_timeouts} consecutive times"
                )
            # The first two timeouts remain step-level errors so the Agent can
            # choose another action. The third timeout terminates this task.
            tool_call = input_kwargs.get("tool_call")
            yield ToolResponse(
                id=getattr(tool_call, "id", ""),
                content=[
                    TextBlock(
                        text=(
                            f"Tool call timed out after {self.timeout_seconds} seconds. "
                            "Try a different browser action or continue from the evidence already collected."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
            )
        else:
            self.consecutive_timeouts = 0
            if mcp_timeout_in_call:
                self.consecutive_mcp_initialize_timeouts += 1
                if self.consecutive_mcp_initialize_timeouts >= 3:
                    raise MCPBrowserUnhealthyError(
                        "MCP browser initializeServer timed out three consecutive times"
                    )
            else:
                self.consecutive_mcp_initialize_timeouts = 0
