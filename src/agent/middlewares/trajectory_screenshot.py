import asyncio
import base64
from pathlib import Path
from typing import Any, Callable

from agentscope.agent import Agent
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

from .common import tool_result_text


TRAJECTORY_SCREENSHOT_TIMEOUT_MS = 30000
CDP_SCREENSHOT_TIMEOUT_SECONDS = 10
CDP_SESSION_DETACH_TIMEOUT_SECONDS = 2


class TrajectoryScreenshotMiddleware(MiddlewareBase):
    """Save trajectory screenshots: initial page first, then after each tool result."""

    def __init__(self, task_idx: int, page_tracker: Any, trajectory_dir: Path) -> None:
        self.task_idx = task_idx
        self.page_tracker = page_tracker
        self.trajectory_dir = trajectory_dir
        self.step = 0
        self.initial_captured = False

    async def on_reply(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]):
        if not self.initial_captured:
            self.initial_captured = True
            # 任务开始立即保存 0.png，记录进入目标网站后的初始页面状态。
            await self._capture("initial page")
        async for event in next_handler(**input_kwargs):
            yield event

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler: Callable[..., Any]):
        async for item in next_handler(**input_kwargs):
            yield item
            # AgentScope 2.x 的 on_acting 产出 ToolChunk / ToolResponse，
            # ToolResponse 是一个工具调用完成后的最终结果。
            if isinstance(item, ToolResponse):
                # MCP 的结果通常带有 "- Page URL:"。据此同步本地 Page 并激活同一页签。
                await self.page_tracker.sync_from_tool_result(tool_result_text(item))
                # 每次工具执行完成后截图，trajectory 表示“动作之后”的页面状态。
                await self._capture("after tool")

    async def _capture(self, reason: str) -> None:
        screenshot_path = self.trajectory_dir / f"{self.step}.png"
        current_step = self.step
        self.step += 1
        try:
            self.trajectory_dir.mkdir(parents=True, exist_ok=True)
            await self.page_tracker.activate()
            try:
                await self.page_tracker.page.screenshot(
                    path=str(screenshot_path),
                    # Keep the current viewport readable for the vision judge;
                    # extremely tall full-page images are downscaled by providers.
                    full_page=False,
                    timeout=TRAJECTORY_SCREENSHOT_TIMEOUT_MS,
                )
                print(f"[task-{self.task_idx} TRAJECTORY] saved {screenshot_path.name} ({reason})")
            except Exception as playwright_error:
                try:
                    await self._capture_with_cdp(screenshot_path)
                    print(
                        f"[task-{self.task_idx} TRAJECTORY] saved {screenshot_path.name} "
                        f"({reason}, cdp fallback)"
                    )
                except Exception as cdp_error:
                    raise RuntimeError(
                        "Playwright screenshot and CDP screenshot both failed: "
                        f"playwright={playwright_error.__class__.__name__}; "
                        f"cdp={cdp_error.__class__.__name__}: {cdp_error}"
                    ) from cdp_error
        except Exception as exc:
            print(f"\n[task-{self.task_idx} TRAJECTORY] screenshot {current_step} failed after {reason}: {exc}")

    async def _capture_with_cdp(self, screenshot_path: Path) -> None:
        """Capture the current viewport without waiting for fonts or page settling."""
        page = self.page_tracker.page
        cdp_session = None
        try:
            async with asyncio.timeout(CDP_SCREENSHOT_TIMEOUT_SECONDS):
                cdp_session = await page.context.new_cdp_session(page)
                result = await cdp_session.send(
                    "Page.captureScreenshot",
                    {
                        "format": "png",
                        "fromSurface": True,
                        "captureBeyondViewport": False,
                    },
                )
                image_data = result.get("data") if isinstance(result, dict) else None
                if not image_data:
                    raise RuntimeError("CDP returned no screenshot data")
                screenshot_path.write_bytes(base64.b64decode(image_data))
        finally:
            if cdp_session is not None:
                try:
                    async with asyncio.timeout(CDP_SESSION_DETACH_TIMEOUT_SECONDS):
                        await cdp_session.detach()
                except Exception:
                    # A closed page/session is already unusable; do not let
                    # fallback cleanup delay or terminate the Agent step.
                    pass
