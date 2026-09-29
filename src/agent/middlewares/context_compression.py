from typing import Any, Callable

from agentscope.agent import Agent
from agentscope.middleware import MiddlewareBase


class ContextCompressionRecoveryMiddleware(MiddlewareBase):
    """Keep a task alive when the model cannot produce a compression summary."""

    def __init__(self, task: str, summary_template: str) -> None:
        self.task = task
        self.summary_template = summary_template
        self.recovery_count = 0

    async def on_compress_context(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Any],
    ) -> None:
        try:
            await next_handler(**input_kwargs)
            return
        except Exception as exc:
            # Context compression is an optimization. A malformed summary must
            # not turn an otherwise recoverable browser task into FAILED.
            self.recovery_count += 1
            config = input_kwargs.get("context_config") or agent.context_config
            try:
                prepared = await agent._prepare_model_input()
                _, reserved = await agent._split_context_for_compression(
                    config.reserve_ratio * agent.model.context_size,
                    prepared.get("tools", []),
                )
            except Exception:
                # Keep the latest complete message as a last resort. This is
                # still preferable to dropping the task because compression
                # itself failed.
                reserved = agent.state.context[-1:]

            agent.state.summary = self.summary_template.format(task=self.task)
            agent.state.context = reserved
            print(
                "[context-compression] recovery %d: %s; retained %d recent message(s)"
                % (self.recovery_count, exc.__class__.__name__, len(reserved)),
            )
