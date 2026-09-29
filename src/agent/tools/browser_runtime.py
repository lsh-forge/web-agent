import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
import re


_PAGE_URL_PATTERN = re.compile(r"(?m)^- Page URL:\s*(\S+)\s*$")
_CURRENT_TAB_URL_PATTERN = re.compile(
    r"(?m)^\s*-\s*\d+:\s*\(current\)\s*\[[^\]]*\]\((\S+?)\)\s*$",
)


class BrowserPageTracker:
    """Keep custom screenshot tools on the same tab currently selected by Playwright MCP."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.context = page.context

    async def sync_from_tool_result(self, result: str) -> bool:
        """Activate the local page that Playwright MCP identifies as current."""
        matches = _PAGE_URL_PATTERN.findall(result or "")
        if not matches:
            # browser_tabs reports the selected tab as markdown instead of a
            # Page URL field, so support that explicit current-tab form too.
            matches = _CURRENT_TAB_URL_PATTERN.findall(result or "")
        if not matches:
            return False

        target_url = matches[-1]
        for candidate in self.context.pages:
            if not candidate.is_closed() and candidate.url == target_url:
                self.page = candidate
                await self.activate()
                return True
        return False

    async def activate(self) -> None:
        """Make the tracked tab the visible Chrome tab before a local screenshot is captured."""
        if not self.page.is_closed():
            await self.page.bring_to_front()


class BrowserToolRuntime:
    """Shared browser runtime used by custom visual tools and screenshots."""

    def __init__(self, page_tracker: BrowserPageTracker, task_output_dir: Path) -> None:
        self.page_tracker = page_tracker
        # 视觉工具的截图仅用于本次工具调用，不能出现在比赛要求的任务结果目录中。
        self.temp_dir = Path(tempfile.gettempdir()) / "wr-048-agent" / task_output_dir.name

    async def screenshot(self, prefix: str, full_page: bool) -> Path:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        # 临时截图只给视觉工具使用，调用结束后由工具负责删除。
        screenshot_path = self.temp_dir / f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.png"
        # 截图前激活 MCP 最近操作的页签，保证视觉工具与浏览器工具观察同一页面。
        await self.page_tracker.activate()
        await self.page_tracker.page.screenshot(
            path=str(screenshot_path),
            full_page=full_page,
            timeout=50000,
        )
        return screenshot_path

    @staticmethod
    def delete(path: Path | None) -> None:
        if path is None:
            return
        try:
            # 识别完成即删除临时图，并在目录为空时顺手清理目录。
            path.unlink(missing_ok=True)
            path.parent.rmdir()
        except Exception:
            # 删除失败不影响工具返回，下一次运行仍会使用唯一文件名。
            pass
