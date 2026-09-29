"""Record model and tool timing without task content in the shared app log."""

import time
from typing import Any, Callable

from agentscope.agent import Agent
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

from runtime_timeline import elapsed_ms, log_safe_exception, safe_error_kind


class RuntimeTimelineMiddleware(MiddlewareBase):
    """Emit privacy-safe timing events for one task Agent."""

    def __init__(self, logger: Any, progress_callback: Callable[[str], None] | None = None) -> None:
        self.logger = logger
        self.progress_callback = progress_callback

    def _mark_progress(self, phase: str) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(phase)
        except Exception:
            # Monitoring must never change Agent behavior.
            pass

    async def on_reply(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]):
        started_at = time.perf_counter()
        self._mark_progress("agent_execution")
        self.logger.info("agent_run_started")
        try:
            async for event in next_handler(**input_kwargs):
                yield event
        except Exception as exc:
            log_safe_exception(self.logger, "agent_run_finished", exc, started_at)
            raise
        else:
            self.logger.info("agent_run_finished status=success duration_ms=%s", elapsed_ms(started_at))

    async def on_model_call(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]):
        started_at = time.perf_counter()
        self._mark_progress("model_call")
        self.logger.info("model_call_started")
        try:
            result = await next_handler(**input_kwargs)
        except Exception as exc:
            log_safe_exception(self.logger, "model_call_finished", exc, started_at)
            self._mark_progress("agent_execution")
            raise
        self._mark_progress("agent_execution")
        self.logger.info("model_call_finished status=success duration_ms=%s", elapsed_ms(started_at))
        return result

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]):
        started_at = time.perf_counter()
        outcome = "success"
        error_kind = "none"
        self._mark_progress("tool_call")
        self.logger.info("tool_call_started")
        try:
            async for event in next_handler(**input_kwargs):
                if isinstance(event, ToolResponse) and str(getattr(event, "state", "")).lower().endswith("error"):
                    outcome = "error"
                    error_kind = safe_error_kind(event)
                yield event
        except Exception as exc:
            log_safe_exception(self.logger, "tool_call_finished", exc, started_at)
            self._mark_progress("agent_execution")
            raise
        else:
            self._mark_progress("agent_execution")
            self.logger.info(
                "tool_call_finished status=%s error_kind=%s duration_ms=%s",
                outcome,
                error_kind,
                elapsed_ms(started_at),
            )
