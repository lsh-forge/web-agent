import asyncio
import base64
import time
from pathlib import Path
from typing import Any

from agentscope.message import SystemMsg, TextBlock, UserMsg
from agentscope.model import OpenAIChatModel

try:
    from agentscope.message import Base64Source, DataBlock
except Exception:  # pragma: no cover - compatibility guard
    Base64Source = None
    DataBlock = None

from .browser_runtime import BrowserToolRuntime
from runtime_timeline import elapsed_ms, log_safe_exception


class ScreenshotVlmTool:
    """Visual analysis of the currently visible browser viewport."""

    def __init__(
        self,
        runtime: BrowserToolRuntime,
        model: OpenAIChatModel,
        logger: Any,
        timeline_logger: Any,
        timeout_seconds: int,
    ) -> None:
        self.runtime = runtime
        self.model = model
        self.logger = logger
        self.timeline_logger = timeline_logger
        self.timeout_seconds = timeout_seconds

    async def __call__(self, instruction: str) -> str:
        screenshot_path = None
        try:
            page_position = await self._page_position()
            screenshot_path = await self.runtime.screenshot("vlm-screenshot", full_page=False)
            result = await self._call_vlm(screenshot_path, instruction, page_position)
            output = f"页面位置：{page_position}\n以下是网页截图VLM分析结果：{result}"
            self.logger.info("browser_screenshot_vlm 返回给模型的内容：\n%s", output)
            return output
        except Exception as exc:
            output = "网页截图及VLM分析异常：" + str(exc)
            self.logger.warning("browser_screenshot_vlm 返回给模型的异常内容：\n%s", output)
            return output
        finally:
            self.runtime.delete(screenshot_path)

    async def _page_position(self) -> str:
        """Describe the document scroll position represented by the viewport screenshot."""
        try:
            await self.runtime.page_tracker.activate()
            metrics = await self.runtime.page_tracker.page.evaluate(
                """() => {
                    const root = document.documentElement;
                    const body = document.body;
                    const viewportHeight = Math.max(window.innerHeight || 0, root?.clientHeight || 0);
                    const documentHeight = Math.max(root?.scrollHeight || 0, body?.scrollHeight || 0);
                    const maxScroll = Math.max(documentHeight - viewportHeight, 0);
                    const rawScrollTop = window.scrollY ?? root?.scrollTop ?? body?.scrollTop ?? 0;
                    const scrollTop = Math.min(Math.max(rawScrollTop, 0), maxScroll);
                    return { scrollTop, maxScroll, viewportHeight };
                }""",
            )
            scroll_top = round(float(metrics.get("scrollTop", 0)))
            max_scroll = round(float(metrics.get("maxScroll", 0)))
            viewport_height = round(float(metrics.get("viewportHeight", 0)))
            progress = round(scroll_top * 100 / max_scroll) if max_scroll else 0
            if max_scroll <= 1:
                boundary = "页面不可滚动"
            elif scroll_top <= 1:
                boundary = "位于页面顶部"
            elif max_scroll - scroll_top <= 1:
                boundary = "已到页面底部"
            else:
                boundary = "未到页面边界"
            return (
                f"距顶部 {scroll_top}px / 可滚动 {max_scroll}px（{progress}%）；"
                f"可视区高度 {viewport_height}px；{boundary}"
            )
        except Exception as exc:
            self.logger.warning("读取 VLM 截图页面位置失败: %s", exc)
            return "当前页面滚动位置不可用"

    async def _call_vlm(self, screenshot_path: Path, instruction: str, page_position: str) -> str:
        if DataBlock is None or Base64Source is None:
            raise RuntimeError("当前 AgentScope 版本缺少多模态消息块")

        # 用 OpenAI 兼容多模态消息把截图和用户指令一起发给视觉模型。
        image_b64 = base64.b64encode(screenshot_path.read_bytes()).decode("ascii")
        messages = [
            SystemMsg(
                name="system",
                content=(
                    "你是网页截图视觉分析工具。\n"
                    "只根据提供的截图和指令回答，不要编造截图中看不到的信息。\n"
                    "可识别可见文字、视觉状态、图表、布局和图标；对精确数值仅在清晰可读时给出。\n"
                    "回答使用中文，尽量简洁，必要时按条目整理。"
                ),
            ),
            UserMsg(
                name="user",
                content=[
                    TextBlock(
                        text=(
                            f"这是一张当前可视窗口截图。页面位置：{page_position}。"
                            "请先据此判断可确认的信息范围，再执行指令：" + instruction
                        ),
                    ),
                    DataBlock(
                        source=Base64Source(
                            media_type="image/png",
                            data=image_b64,
                        ),
                        name="browser-viewport.png",
                    ),
                ],
            ),
        ]
        started_at = time.perf_counter()
        self.timeline_logger.info("vision_model_call_started")
        try:
            response = await asyncio.wait_for(self.model(messages, tools=[]), timeout=self.timeout_seconds)
            if hasattr(response, "__aiter__"):
                # AgentScope 流会先产生增量块，部分模型随后还会给一个 is_last=True 的完整块。
                # 不能把两者直接拼接，否则完整回答会在工具结果中重复一次。
                delta_parts = []
                final_text = ""
                async for chunk in response:
                    chunk_text = "".join(_text_from_blocks(getattr(chunk, "content", [])))
                    if getattr(chunk, "is_last", False):
                        final_text = chunk_text
                    else:
                        delta_parts.append(chunk_text)
                text = (final_text or "".join(delta_parts)).strip()
            else:
                text = "".join(_text_from_blocks(getattr(response, "content", []))).strip()
        except Exception as exc:
            log_safe_exception(self.timeline_logger, "vision_model_call_finished", exc, started_at)
            raise
        self.timeline_logger.info("vision_model_call_finished status=success duration_ms=%s", elapsed_ms(started_at))
        return text or "VLM 未返回有效文本"
def _text_from_blocks(blocks: list[Any]) -> list[str]:
    values = []
    for block in blocks:
        if hasattr(block, "text"):
            values.append(block.text)
        elif isinstance(block, str):
            values.append(block)
    return values
