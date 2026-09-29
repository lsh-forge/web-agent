"""Visual coordinate grounding for controls that cannot be operated through the DOM."""

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from agentscope.message import SystemMsg, TextBlock, UserMsg
from agentscope.model import OpenAIChatModel

try:
    from agentscope.message import Base64Source, DataBlock
except Exception:  # pragma: no cover - compatibility guard
    Base64Source = None
    DataBlock = None

from .browser_runtime import BrowserToolRuntime
from runtime_timeline import elapsed_ms, log_safe_exception


_JSON_OBJECT_PATTERN = re.compile(r"\{\s*\"x\"\s*:\s*.*?\}", re.DOTALL)
_GRID_COLUMNS = 16
_GRID_ROWS = 12


@dataclass(frozen=True)
class ViewportMetrics:
    """CSS viewport dimensions used by Playwright mouse coordinates."""

    width: int
    height: int


class ScreenshotCoordinateTool:
    """Locate one visible UI target and return its Playwright viewport coordinates."""

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
        screenshot_path: Path | None = None
        try:
            screenshot_path, viewport = await self._capture_stable_viewport()
            image_width, image_height = _image_size(screenshot_path)
            grid_screenshot_path = _save_grid_coordinate_image(screenshot_path)
            normalized_point = await self._locate_in_screenshot(
                grid_screenshot_path,
                instruction,
                image_width,
                image_height,
                viewport,
            )
            if normalized_point is None:
                return _empty_coordinates()

            image_x = normalized_point[0] * (image_width - 1) / 1000
            image_y = normalized_point[1] * (image_height - 1) / 1000
            x, y = _image_to_viewport_coordinates(
                image_x,
                image_y,
                image_width,
                image_height,
                viewport.width,
                viewport.height,
            )
            debug_image_path = _save_debug_coordinate_image(screenshot_path, image_x, image_y)
            output = _coordinates_json(x, y)
            self.logger.info(
                "browser_screenshot_coordinate 返回给模型的内容：%s "
                "(screenshot=%sx%s, viewport_css=%sx%s, normalized_point=%s, grid_image=%s, debug_image=%s)",
                output,
                image_width,
                image_height,
                viewport.width,
                viewport.height,
                normalized_point,
                grid_screenshot_path,
                debug_image_path,
            )
            return output
        except Exception as exc:
            # An unavailable coordinate is safer than a plausible but wrong click.
            self.logger.warning("browser_screenshot_coordinate failed: %s", exc)
            return _empty_coordinates()
        finally:
            self.runtime.delete(screenshot_path)

    async def _capture_stable_viewport(self) -> tuple[Path, ViewportMetrics]:
        """Capture only when the CSS viewport did not resize during the screenshot."""
        for attempt in range(2):
            await self.runtime.page_tracker.activate()
            before = await _read_viewport_metrics(self.runtime.page_tracker.page)
            screenshot_path = await self.runtime.screenshot("coordinate-screenshot", full_page=False)
            after = await _read_viewport_metrics(self.runtime.page_tracker.page)
            if before == after:
                return screenshot_path, after
            self.runtime.delete(screenshot_path)
            self.logger.info(
                "browser_screenshot_coordinate retrying after viewport changed: %s -> %s (attempt %s)",
                before,
                after,
                attempt + 1,
            )
        raise RuntimeError("browser viewport changed while capturing the screenshot")

    async def _locate_in_screenshot(
        self,
        screenshot_path: Path,
        instruction: str,
        image_width: int,
        image_height: int,
        viewport: ViewportMetrics,
    ) -> tuple[float, float] | None:
        if DataBlock is None or Base64Source is None:
            raise RuntimeError("当前 AgentScope 版本缺少多模态消息块")

        image_b64 = base64.b64encode(screenshot_path.read_bytes()).decode("ascii")
        messages = [
            SystemMsg(
                name="system",
                content=(
                    "You are a GUI coordinate locator. Locate exactly one visible point for the given instruction. "
                    "Coordinates use a normalized 0-1000 system (0,0 at top-left and 1000,1000 at bottom-right). "
                    "The image includes a reference grid: top labels are normalized X values, left labels are normalized Y values. "
                    "Use grid lines only as a reference. Return the center of the visible actionable control itself, "
                    "not a grid line, grid-cell center, nearby label, or container, unless that is truly the target center. "
                    "First write exactly one short verification sentence in this format: "
                    "Target bounds: X=<left>-<right>, Y=<top>-<bottom>; center=<x>,<y>. "
                    "All values in that sentence use the same normalized 0-1000 system. Use the visible control edges for the bounds. "
                    "The center in that sentence and the JSON must agree, "
                    "and the center must be inside the stated bounds. Do not list alternatives or self-correct. "
                    "Then respond with exactly one JSON object: "
                    '{"x": <number>, "y": <number>}. '
                    'If no target can be located confidently, return {"x": null, "y": null}. '
                    "Do not include Markdown or any other text."
                ),
            ),
            UserMsg(
                name="user",
                content=[
                    TextBlock(
                        text=(
                            f"Reference grid: {_GRID_COLUMNS} columns x {_GRID_ROWS} rows. "
                            "Return the target normalized 0-1000 coordinate directly from the grid labels. Instruction: "
                            f"{instruction}"
                        ),
                    ),
                    DataBlock(
                        source=Base64Source(media_type="image/png", data=image_b64),
                        name="browser-viewport.png",
                    ),
                ],
            ),
        ]
        started_at = time.perf_counter()
        self.timeline_logger.info("vision_model_call_started")
        try:
            response = await asyncio.wait_for(self.model(messages, tools=[]), timeout=self.timeout_seconds)
            raw_response = await _response_text(response)
            point = _parse_normalized_coordinates(raw_response)
        except Exception as exc:
            log_safe_exception(self.timeline_logger, "vision_model_call_finished", exc, started_at)
            raise
        self.timeline_logger.info("vision_model_call_finished status=success duration_ms=%s", elapsed_ms(started_at))
        self.logger.info(
            "browser_screenshot_coordinate VLM result: %s "
            "(screenshot=%sx%s, viewport_css=%sx%s, parsed_normalized_point=%s)",
            raw_response,
            image_width,
            image_height,
            viewport.width,
            viewport.height,
            point,
        )
        return point


async def _read_viewport_metrics(page: Any) -> ViewportMetrics:
    metrics = await page.evaluate(
        """() => ({ width: window.innerWidth, height: window.innerHeight })""",
    )
    width = int(metrics.get("width", 0))
    height = int(metrics.get("height", 0))
    if width <= 0 or height <= 0:
        raise RuntimeError(f"invalid browser viewport dimensions: {metrics}")
    return ViewportMetrics(width=width, height=height)


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        width, height = image.size
    if width <= 0 or height <= 0:
        raise RuntimeError("invalid screenshot dimensions")
    return width, height


def _save_debug_coordinate_image(screenshot_path: Path, image_x: float, image_y: float) -> Path:
    """Persist a marked copy for local inspection without changing the tool response."""
    project_root = Path(__file__).resolve().parents[3]
    debug_dir = project_root / "temp"
    debug_dir.mkdir(parents=True, exist_ok=True)
    output_path = debug_dir / (
        f"coordinate-{screenshot_path.parent.name}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.png"
    )
    with Image.open(screenshot_path) as source:
        image = _with_reference_grid(source.convert("RGBA"))
        radius = max(8, min(24, min(image.size) // 60))
        draw = ImageDraw.Draw(image)
        bounds = (image_x - radius, image_y - radius, image_x + radius, image_y + radius)
        draw.ellipse(bounds, fill="#ff0000", outline="#ffffff", width=3)
        image.convert("RGB").save(output_path, format="PNG")
    return output_path


def _save_grid_coordinate_image(screenshot_path: Path) -> Path:
    """Persist a lightly annotated copy that gives the VLM stable coordinate anchors."""
    project_root = Path(__file__).resolve().parents[3]
    debug_dir = project_root / "temp"
    debug_dir.mkdir(parents=True, exist_ok=True)
    output_path = debug_dir / (
        f"coordinate-grid-{screenshot_path.parent.name}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.png"
    )

    with Image.open(screenshot_path) as source:
        _with_reference_grid(source.convert("RGBA")).convert("RGB").save(output_path, format="PNG")
    return output_path


def _with_reference_grid(image: Image.Image) -> Image.Image:
    """Return an image with coordinate guides while preserving the original pixel geometry."""
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _grid_font(min(width, height))

    for column in range(_GRID_COLUMNS + 1):
        x = round(column * (width - 1) / _GRID_COLUMNS)
        value = round(column * 1000 / _GRID_COLUMNS)
        draw.line((x, 0, x, height - 1), fill=(0, 153, 255, 125), width=1)
        _draw_grid_label(draw, (x + 3, 3), str(value), font)

    for row in range(_GRID_ROWS + 1):
        y = round(row * (height - 1) / _GRID_ROWS)
        value = round(row * 1000 / _GRID_ROWS)
        draw.line((0, y, width - 1, y), fill=(0, 153, 255, 125), width=1)
        _draw_grid_label(draw, (3, y + 3), str(value), font)

    return Image.alpha_composite(image, overlay)


def _grid_font(min_dimension: int) -> ImageFont.ImageFont:
    size = max(12, min(20, min_dimension // 50))
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - older Pillow fallback
        return ImageFont.load_default()


def _draw_grid_label(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
) -> None:
    left, top, right, bottom = draw.textbbox(position, text, font=font)
    draw.rectangle((left - 2, top - 1, right + 2, bottom + 1), fill=(255, 255, 255, 190))
    draw.text(position, text, fill=(0, 51, 102, 255), font=font)


async def _response_text(response: Any) -> str:
    if hasattr(response, "__aiter__"):
        delta_parts: list[str] = []
        final_text = ""
        async for chunk in response:
            chunk_text = "".join(_text_from_blocks(getattr(chunk, "content", [])))
            if getattr(chunk, "is_last", False):
                final_text = chunk_text
            else:
                delta_parts.append(chunk_text)
        return (final_text or "".join(delta_parts)).strip()
    return "".join(_text_from_blocks(getattr(response, "content", []))).strip()


def _text_from_blocks(blocks: list[Any]) -> list[str]:
    values = []
    for block in blocks:
        if hasattr(block, "text"):
            values.append(block.text)
        elif isinstance(block, str):
            values.append(block)
    return values


def _parse_normalized_coordinates(text: str) -> tuple[float, float] | None:
    """Accept a JSON object only when both coordinates are in the 0-1000 range."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    candidates = [cleaned]
    candidates.extend(match.group(0) for match in _JSON_OBJECT_PATTERN.finditer(cleaned))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or set(data) != {"x", "y"}:
            continue
        x, y = data["x"], data["y"]
        if x is None and y is None:
            return None
        if isinstance(x, bool) or isinstance(y, bool):
            continue
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        if 0 <= x <= 1000 and 0 <= y <= 1000:
            return float(x), float(y)
    return None


def _image_to_viewport_coordinates(
    image_x: float,
    image_y: float,
    image_width: int,
    image_height: int,
    viewport_width: int,
    viewport_height: int,
) -> tuple[int, int]:
    """Map screenshot pixels to Playwright's viewport-relative CSS pixels."""
    x = round(image_x * (viewport_width - 1) / max(image_width - 1, 1))
    y = round(image_y * (viewport_height - 1) / max(image_height - 1, 1))
    return (
        min(max(x, 0), viewport_width - 1),
        min(max(y, 0), viewport_height - 1),
    )


def _coordinates_json(x: int, y: int) -> str:
    return json.dumps({"x": x, "y": y}, ensure_ascii=True, separators=(",", ":"))


def _empty_coordinates() -> str:
    return '{"x":null,"y":null}'
