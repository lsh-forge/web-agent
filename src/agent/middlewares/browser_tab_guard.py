"""Protect the remote browser from losing its last live tab."""

import asyncio
import json
from typing import Any, Callable

from agentscope.agent import Agent
from agentscope.middleware import MiddlewareBase

from errors import BrowserLifecycleError
from web_controller import ensure_page_survivor_async, ensure_at_least_one_page_async


def _tool_arguments(tool_call: Any) -> dict[str, Any]:
    value = getattr(tool_call, "input", None)
    if value is None:
        value = getattr(tool_call, "arguments", None)
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class BrowserTabGuardMiddleware(MiddlewareBase):
    """Ensure tab-closing actions cannot disconnect the CDP browser."""

    def __init__(self, context: Any) -> None:
        self.context = context

    @staticmethod
    def _tool_name(tool_call: Any) -> str:
        return str(getattr(tool_call, "name", "")).rsplit("__", 1)[-1]

    async def on_acting(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Any],
    ):
        tool_call = input_kwargs.get("tool_call")
        tool_name = self._tool_name(tool_call)
        args = _tool_arguments(tool_call)
        is_close = tool_name == "browser_tabs" and str(args.get("action", "")).lower() == "close"

        if is_close:
            try:
                await ensure_page_survivor_async(self.context)
            except Exception as exc:
                if isinstance(exc, BrowserLifecycleError):
                    raise
                raise BrowserLifecycleError("Unable to preserve a browser tab before closing") from exc

        async for event in next_handler(**input_kwargs):
            yield event

        # Also repair zero-tab states caused by a page-closing script or an
        # MCP implementation that bypasses browser_tabs.
        await ensure_at_least_one_page_async(self.context)
