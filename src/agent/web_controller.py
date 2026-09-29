import json
import uuid
from playwright.sync_api import sync_playwright
from playwright.async_api import async_playwright
import random
import platform
import base64
import json
import re
import os
import sqlite3
import tempfile
import time
import sys
import asyncio
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Callable, List, Dict, Any
from urllib.parse import urlparse, parse_qs
from errors import BrowserLifecycleError, PageNavigationError, classify_infrastructure_error

# ========== Begin Sandbox ==========
# Global sandbox reference
_sandbox_instance = None

def connect_existing_sandbox(cdp_url, access_token):
    """Connect to existing sandbox, set global headers"""
    global _sandbox_instance, _cdp_headers
    _cdp_headers = {"X-Access-Token": access_token}
    print(f"✅ Connected to existing sandbox, CDP: {cdp_url[:60]}...")
    return cdp_url

# Internal headers cache
_cdp_headers = {}

def get_cdp_headers():
    """Get headers needed for CDP connection headers"""
    if _cdp_headers:
        return _cdp_headers
    if _sandbox_instance:
        return {"X-Access-Token": str(_sandbox_instance._envd_access_token)}
    return {}


def resolve_cdp_headers(cdp_url: str) -> dict[str, str]:
    """Return CDP connection headers, deriving the sandbox token from the URL when present."""
    headers = get_cdp_headers()
    if headers:
        return headers

    parsed = urlparse(cdp_url)
    token = parse_qs(parsed.query).get("access_token", [None])[0]
    if token:
        connect_existing_sandbox(cdp_url, token)
        print("🔑 Auto-detected sandbox token from URL")
        return get_cdp_headers()
    return {}
# ========== End Sandbox ==========

browser_window_size_width = 1280
browser_window_size_height = 720

# Browser lifecycle calls need shorter bounds than the total task deadline.
CDP_CONNECT_OPERATION_TIMEOUT_SECONDS = 45
PAGE_CREATE_TIMEOUT_SECONDS = 45
PAGE_CLEANUP_TIMEOUT_SECONDS = 20
PAGE_PREPARE_TIMEOUT_SECONDS = 30
PAGE_ROUTE_TIMEOUT_SECONDS = 25
PAGE_NAVIGATION_TIMEOUT_SECONDS = 240


def install_lifecycle_observers(browser: Any, context: Any, logger: Any) -> dict[str, set[int]]:
    """Observe browser/page closure without changing browser lifecycle behavior."""
    state: dict[str, set[int]] = {"expected_page_closes": set(), "observed_pages": set()}

    def observe_page(page: Any) -> None:
        page_id = id(page)
        if page_id in state["observed_pages"]:
            return
        state["observed_pages"].add(page_id)
        try:
            setattr(page, "_wr048_lifecycle_state", state)
        except Exception:
            pass

        def on_close(*_args: Any) -> None:
            source = (
                "agent_page_cleanup"
                if page_id in state["expected_page_closes"]
                else "external_or_mcp_or_agent"
            )
            state["expected_page_closes"].discard(page_id)
            logger.warning("browser_page_closed source=%s", source)

        try:
            page.on("close", on_close)
        except Exception as exc:
            logger.warning("browser_page_observer_failed error_kind=%s", exc.__class__.__name__)

    try:
        for page in list(context.pages):
            observe_page(page)
        context.on("page", observe_page)
        context.on(
            "close",
            lambda *_args: logger.error(
                "browser_context_closed source=external_or_mcp_or_browser"
            ),
        )
        browser.on(
            "disconnected",
            lambda *_args: logger.error("browser_disconnected source=browser_or_cdp"),
        )
    except Exception as exc:
        logger.warning("browser_lifecycle_observer_failed error_kind=%s", exc.__class__.__name__)
    return state


def _notify_page_phase(phase_callback: Callable[[str], None] | None, phase: str) -> None:
    if phase_callback is None:
        return
    try:
        phase_callback(phase)
    except Exception:
        # Progress reporting must never affect browser control.
        pass


async def _run_page_operation(
    operation: Callable[[], Any],
    timeout_seconds: float,
    phase: str,
    phase_callback: Callable[[str], None] | None = None,
):
    """Run one Playwright lifecycle operation with an independent timeout."""
    _notify_page_phase(phase_callback, phase)
    try:
        async with asyncio.timeout(timeout_seconds):
            return await operation()
    except TimeoutError as exc:
        raise BrowserLifecycleError(
            f"{phase} timed out after {timeout_seconds:g} seconds"
        ) from exc


@dataclass
class PlaywrightSession:
    """Resources owned by one Worker; the remote browser is owned by the sandbox."""

    p: Any
    browser: Any
    context: Any
    page: Any = None


def _live_context_pages(context: Any) -> list[Any]:
    """Return pages that can still be used without closing the browser context."""
    try:
        return [page for page in context.pages if not page.is_closed()]
    except Exception as exc:
        raise BrowserLifecycleError("Browser context is unavailable") from exc


async def ensure_at_least_one_page_async(context: Any) -> Any:
    """Keep a browser context alive even when a previous tab was closed."""
    pages = _live_context_pages(context)
    if pages:
        return pages[0]
    return await _run_page_operation(
        context.new_page,
        PAGE_CREATE_TIMEOUT_SECONDS,
        "page_create",
    )


async def ensure_page_survivor_async(context: Any) -> Any:
    """Create a spare page before an operation may close the last live page."""
    pages = _live_context_pages(context)
    if len(pages) >= 2:
        return pages[0]
    return await _run_page_operation(
        context.new_page,
        PAGE_CREATE_TIMEOUT_SECONDS,
        "page_create",
    )


async def verify_browser_context_health_async(context: Any) -> Any:
    """Probe the SDK connection and a live page before a task is claimed."""
    page = await ensure_at_least_one_page_async(context)
    try:
        await _run_page_operation(
            lambda: page.evaluate("() => document.readyState"),
            PAGE_PREPARE_TIMEOUT_SECONDS,
            "browser_healthcheck",
        )
    except BrowserLifecycleError:
        raise
    except Exception as exc:
        raise BrowserLifecycleError("Browser SDK health check failed") from exc
    return page


async def cleanup_context_after_task_async(
    context: Any,
    keep_page: Any = None,
    logger: Any = None,
    phase_callback: Callable[[str], None] | None = None,
) -> Any:
    """Clean the context after a task while preserving one live page.

    A cleanup failure is allowed to mark the Worker session unhealthy so the
    next task can rebuild local clients before claiming work. It must never
    close the final live page.
    """
    pages = _live_context_pages(context)
    if keep_page is None or keep_page not in pages:
        keep_page = pages[0] if pages else await ensure_at_least_one_page_async(context)
        pages = _live_context_pages(context)

    # Remove extra pages first. The selected page is the survivor and remains
    # available for the next task even when it is the only live page.
    for page in list(pages):
        if page != keep_page and not page.is_closed():
            await _close_page_async(
                page,
                logger=logger,
                phase_callback=phase_callback,
            )

    # The survivor may still carry the previous task's route handler. Detach
    # it without closing the page so the next task can configure it afresh.
    if not keep_page.is_closed():
        if logger is not None:
            logger.info("browser_page_cleanup_started source=worker_post_task")
        await _run_page_operation(
            lambda: keep_page.unroute_all(behavior="ignoreErrors"),
            PAGE_CLEANUP_TIMEOUT_SECONDS,
            "page_cleanup",
            phase_callback,
        )
        if logger is not None:
            logger.info("browser_page_cleanup_finished status=success source=worker_post_task")
    return keep_page

def extract_between_keywords(text, start_key, end_key):
    pattern = re.compile(f'{re.escape(start_key)}(.*?){re.escape(end_key)}', re.DOTALL)
    matches = pattern.findall(text)
    return [match.strip() for match in matches]

def extract_box_coordinate(response, box_name="start_box"):
    """Extract (x, y) coordinate from both formats:
    - New: start_box='<|box_start|>(x,y)<|box_end|>'
    - Old: start_box='(x,y)'
    """
    # New format with <|box_start|>...<|box_end|>
    m = re.search(rf"{box_name}='<\|box_start\|>\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*<\|box_end\|>'", response)
    if m:
        return int(m.group(1)), int(m.group(2))
    # Old format
    m = re.search(rf"{box_name}='\(\s*(\d+)\s*,\s*(\d+)\s*\)'", response)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None

# Initialize Playwright
def init_playwright_context(url):
    p = sync_playwright().start()
    try:
        headers = resolve_cdp_headers(url)
        browser = p.chromium.connect_over_cdp(url, **({"headers": headers} if headers else {}))
        # Get existing context or create new
        context = browser.contexts[0] if browser.contexts else browser.new_context()
    except Exception as e:
        print("init error", e)
        return None, None, None

    return p, browser, context


async def init_playwright_context_async(url, max_retries=3, raise_on_failure=False):
    """Connect to the sandbox with bounded retries without closing the remote browser."""
    last_error = None
    for attempt in range(1, max_retries + 1):
        # AgentScope 主循环是 async，必须使用 Playwright Async API，不能在事件循环里启动 Sync API。
        p = None
        try:
            async with asyncio.timeout(CDP_CONNECT_OPERATION_TIMEOUT_SECONDS):
                p = await async_playwright().start()
                headers = resolve_cdp_headers(url)
                browser = await p.chromium.connect_over_cdp(
                    url,
                    timeout=30000,
                    **({"headers": headers} if headers else {}),
                )
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
            return p, browser, context
        except Exception as exc:
            last_error = exc
            print(f"init error attempt={attempt}/{max_retries}: {exc.__class__.__name__}: {exc}")
            try:
                # 只断开本地 Playwright 客户端，不调用 browser.close()。
                if p is not None:
                    async with asyncio.timeout(PAGE_CLEANUP_TIMEOUT_SECONDS):
                        await p.stop()
            except Exception:
                pass
            if attempt < max_retries:
                await asyncio.sleep(min(2 * attempt, 5))

    if last_error is not None:
        print(f"init failed after {max_retries} attempts: {last_error.__class__.__name__}: {last_error}")
        if raise_on_failure:
            raise BrowserLifecycleError(
                f"CDP connection failed after {max_retries} attempts: "
                f"{last_error.__class__.__name__}: {last_error}"
            ) from last_error
    return None, None, None


# Open webpage
def open_page(p, browser, context, cdp_url, target_url, max_retries = 3):
    # --- Fix code ---
    # Strip whitespace
    if target_url:
        target_url = target_url.strip()
    # Auto-complete https
    if target_url and not target_url.startswith("http"):
        target_url = "https://" + target_url
    # ----------------

    # print(f"🌐 Opening page: {target_url}", context)
    print(f"🌐 Opening page: {target_url}")
    page = None
    
    retry_count = 0
    while retry_count < max_retries:
        try:
            # Clean up extra pages
            if len(context.pages) >= 3:
                print(f"Too many pages ({len(context.pages)}个)，closing old pages...")
                pages_to_close = context.pages[:-1]
                for old_page in pages_to_close:
                    try:
                        # Force stop page activity first
                        try:
                            old_page.evaluate("() => { window.stop(); }")
                        except:
                            pass
                        
                        # 使用run_before_unload=Falseskip confirmation dialog
                        old_page.close(run_before_unload=False)

                    except Exception as e:
                        print(f"Failed to close: {e}")
                        # Continue closing other pages，不要exit()

        except Exception as e:
            print(f"Error cleaning up pages: {e}")

        try:
            # Try creating new page
            if context and len(context.pages) > 0:
                try:
                    page = context.new_page()
                    break
                except Exception as e:
                    print(f"Failed to create page: {e}")
                    context = None
            
            # 如果context不存在或Failed to create page，Reconnect
            if not context or not page:
                print(f"Reconnecting to browser... (attempt {retry_count + 1}/{max_retries})")
                
                # 只断开本地同步客户端，不关闭远端沙箱浏览器。
                try:
                    p.stop()
                except:
                    pass
                
                # Wait for connection to fully close
                import time
                time.sleep(2)
                
                # Reconnect with a fresh local Playwright client.
                try:
                    p = sync_playwright().start()
                    cdp_h = get_cdp_headers(); browser = p.chromium.connect_over_cdp(cdp_url, **({"headers": cdp_h} if cdp_h else {}))
                    # Wait for connection to stabilize
                    time.sleep(1)
                    
                    # Get or create context
                    if browser.contexts:
                        context = browser.contexts[0]
                    else:
                        context = browser.new_context()
                    
                    page = context.new_page()
                    break
                    
                except Exception as e:
                    print(f"Connection failed: {e}")
                    retry_count += 1
                    if retry_count >= max_retries:
                        raise Exception(f"Cannot connect to browser after {max_retries}  times")
                    continue
                    
        except Exception as e:
            print(f"Error creating page: {e}")
            retry_count += 1
            if retry_count >= max_retries:
                raise
    
    if not page:
        raise Exception("Cannot create page")
    
    # Set viewport size
    try:
        # Get browser window size
        browser_window_size = page.evaluate("""
            () => {
                return {
                    availWidth: window.screen.availWidth,
                    availHeight: window.screen.availHeight,
                    width: window.outerWidth || 1920,
                    height: window.outerHeight || 1080
                }
            }
        """)
        
        print(f"Available screen size: {browser_window_size['availWidth']}x{browser_window_size['availHeight']}")
        print(f"browserwindow size: {browser_window_size['width']}x{browser_window_size['height']}")
        
        global browser_window_size_width
        global browser_window_size_height
        
        browser_window_size_width = browser_window_size['width']
        browser_window_size_height = browser_window_size['height']
        
        page.set_viewport_size({"width": browser_window_size_width, "height": browser_window_size_height})
    except Exception as e:
        print(f"Set viewport sizefailed: {e}，使用default值")
        browser_window_size_width = 1920
        browser_window_size_height = 1080
        page.set_viewport_size({"width": 1920, "height": 1080})
    
    # 为所有page请求添加无缓存头
    def prevent_cache(route):
        try:
            headers = route.request.headers.copy()
            headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            headers['Pragma'] = 'no-cache'
            headers['Expires'] = '0'
            route.continue_(headers=headers)
        except:
            route.continue_()
    
    # 只对主文档应用此规则
    try:
        page.route('**/*', prevent_cache)
    except Exception as e:
        print(f"set路由规则failed: {e}")
    
    # 导航到目标URL
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=150000)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"导航到pagefailed: {e}")
        # attempt使用更宽松的wait条件
        try:
            page.goto(target_url, wait_until="commit", timeout=60000)
        except:
            # 如果还是failed，至少确保page存在
            pass
    
    return page


async def _close_page_async(page, logger=None, phase_callback=None):
    """Detach page-level handlers before closing a non-primary tab."""
    if page is None:
        return
    try:
        if page.is_closed():
            return
        lifecycle_state = getattr(page, "_wr048_lifecycle_state", None)
        if isinstance(lifecycle_state, dict):
            lifecycle_state.setdefault("expected_page_closes", set()).add(id(page))
        if logger is not None:
            logger.info("browser_page_cleanup_started source=agent_page_cleanup")
        await _run_page_operation(
            lambda: page.unroute_all(behavior="ignoreErrors"),
            PAGE_CLEANUP_TIMEOUT_SECONDS,
            "page_cleanup",
            phase_callback,
        )
    except BrowserLifecycleError:
        if logger is not None:
            logger.warning("移除旧页签路由超时，会话将重建")
        raise
    except Exception as exc:
        if logger is not None:
            logger.warning("移除旧页签路由失败: %s: %s", exc.__class__.__name__, exc)
    try:
        if not page.is_closed():
            await _run_page_operation(
                lambda: page.close(run_before_unload=False),
                PAGE_CLEANUP_TIMEOUT_SECONDS,
                "page_close",
                phase_callback,
            )
            if logger is not None:
                logger.info(
                    "browser_page_cleanup_finished status=success source=agent_page_cleanup"
                )
    except BrowserLifecycleError:
        if logger is not None:
            logger.warning("关闭旧页签超时，会话将重建")
        raise
    except Exception as exc:
        if logger is not None:
            logger.warning("关闭旧页签失败: %s: %s", exc.__class__.__name__, exc)


async def open_page_async(
    p,
    browser,
    context,
    cdp_url,
    target_url,
    max_retries=3,
    before_goto=None,
    logger=None,
    phase_callback: Callable[[str], None] | None = None,
    lifecycle_state: dict[str, set[int]] | None = None,
    prepared_page: Any = None,
):
    """Reuse one Worker browser session and keep one active task page."""
    if target_url:
        target_url = target_url.strip()
    if target_url and not target_url.startswith("http"):
        target_url = "https://" + target_url

    print(f"🌐 Opening page: {target_url}")
    if context is None:
        raise BrowserLifecycleError("Browser context is unavailable")

    if prepared_page is not None:
        try:
            if prepared_page.is_closed() or prepared_page not in _live_context_pages(context):
                raise BrowserLifecycleError("Prepared browser page is unavailable")
            page = prepared_page
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            raise BrowserLifecycleError("Prepared browser page is unavailable") from exc
    else:
        # Backward-compatible fallback for callers that do not run Worker
        # preflight. The main Worker path always supplies prepared_page.
        try:
            page = await _run_page_operation(
                context.new_page,
                PAGE_CREATE_TIMEOUT_SECONDS,
                "page_create",
                phase_callback,
            )
            if lifecycle_state is not None:
                try:
                    setattr(page, "_wr048_lifecycle_state", lifecycle_state)
                except Exception:
                    pass
        except Exception as exc:
            if isinstance(exc, BrowserLifecycleError):
                raise
            raise BrowserLifecycleError("browser page creation failed") from exc

        try:
            # 先确保新主页签存在，再清理弹窗和上一题遗留页签，避免误关最后一个页签。
            for other_page in list(context.pages):
                if other_page != page and not other_page.is_closed():
                    await _close_page_async(
                        other_page,
                        logger=logger,
                        phase_callback=phase_callback,
                    )
        except BrowserLifecycleError:
            raise
        except Exception as e:
            raise BrowserLifecycleError("清理旧页签失败") from e

    try:
        await _run_page_operation(
            page.bring_to_front,
            PAGE_PREPARE_TIMEOUT_SECONDS,
            "page_activate",
            phase_callback,
        )
    except Exception as exc:
        if isinstance(exc, BrowserLifecycleError):
            raise
        raise BrowserLifecycleError("无法激活当前任务页签") from exc

    if before_goto is not None:
        try:
            await _run_page_operation(
                lambda: before_goto(page),
                PAGE_PREPARE_TIMEOUT_SECONDS,
                "page_prepare",
                phase_callback,
            )
        except BrowserLifecycleError:
            raise
        except Exception as e:
            print(f"导航前浏览器准备失败: {e}")

    try:
        browser_window_size = await _run_page_operation(
            lambda: page.evaluate("""
                () => {
                    return {
                        availWidth: window.screen.availWidth,
                        availHeight: window.screen.availHeight,
                        width: window.outerWidth || 1920,
                        height: window.outerHeight || 1080
                    }
                }
            """),
            PAGE_PREPARE_TIMEOUT_SECONDS,
            "page_prepare",
            phase_callback,
        )

        print(f"Available screen size: {browser_window_size['availWidth']}x{browser_window_size['availHeight']}")
        print(f"browserwindow size: {browser_window_size['width']}x{browser_window_size['height']}")

        global browser_window_size_width
        global browser_window_size_height

        browser_window_size_width = browser_window_size['width']
        browser_window_size_height = browser_window_size['height']

        await _run_page_operation(
            lambda: page.set_viewport_size(
                {"width": browser_window_size_width, "height": browser_window_size_height}
            ),
            PAGE_PREPARE_TIMEOUT_SECONDS,
            "page_prepare",
            phase_callback,
        )
    except BrowserLifecycleError:
        raise
    except Exception as e:
        print(f"Set viewport sizefailed: {e}，使用default值")
        browser_window_size_width = 1920
        browser_window_size_height = 1080
        await _run_page_operation(
            lambda: page.set_viewport_size({"width": 1920, "height": 1080}),
            PAGE_PREPARE_TIMEOUT_SECONDS,
            "page_prepare",
            phase_callback,
        )

    async def prevent_cache(route):
        try:
            headers = route.request.headers.copy()
            headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            headers['Pragma'] = 'no-cache'
            headers['Expires'] = '0'
            await route.continue_(headers=headers)
        except Exception as exc:
            # 页面关闭时不要再次 continue，避免 TargetClosedError 形成清理竞态。
            if "closed" not in str(exc).lower():
                try:
                    await route.continue_()
                except Exception:
                    pass

    try:
        await _run_page_operation(
            lambda: page.route("**/*", prevent_cache),
            PAGE_ROUTE_TIMEOUT_SECONDS,
            "page_route",
            phase_callback,
        )
    except BrowserLifecycleError as first_route_error:
        # A timed-out route registration may have been accepted by the browser.
        # Check the current page before retrying, and never clean up a possibly
        # active route here because cleanup can block the same browser session.
        try:
            if page.is_closed() or page not in _live_context_pages(context):
                raise BrowserLifecycleError("Current page is unavailable after page_route timeout")
            await _run_page_operation(
                lambda: page.evaluate("() => document.readyState"),
                PAGE_PREPARE_TIMEOUT_SECONDS,
                "page_route_healthcheck",
                phase_callback,
            )
        except Exception as health_error:
            raise BrowserLifecycleError(
                "page_route timed out and browser health check failed"
            ) from health_error

        try:
            await _run_page_operation(
                lambda: page.route("**/*", prevent_cache),
                PAGE_ROUTE_TIMEOUT_SECONDS,
                "page_route",
                phase_callback,
            )
        except BrowserLifecycleError as second_route_error:
            if logger is not None:
                logger.warning(
                    "page_route status=degraded reason=timeout_after_retry"
                )
            else:
                print("page_route timed out twice; continue with degraded route status")
    except Exception as e:
        print(f"set路由规则failed: {e}")

    _notify_page_phase(phase_callback, "page_navigation")
    try:
        async with asyncio.timeout(PAGE_NAVIGATION_TIMEOUT_SECONDS):
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=150000)
                await page.wait_for_timeout(3000)
            except Exception as first_error:
                print(f"导航到pagefailed: {first_error}")
                try:
                    await page.goto(target_url, wait_until="commit", timeout=60000)
                except Exception as fallback_error:
                    classified = classify_infrastructure_error(fallback_error)
                    if isinstance(classified, BrowserLifecycleError):
                        raise BrowserLifecycleError(
                            "navigation failed because the browser page or context became unavailable: "
                            f"{fallback_error.__class__.__name__}: {fallback_error}"
                        ) from fallback_error
                    raise PageNavigationError(
                        "navigation failed after domcontentloaded and commit: "
                        f"{fallback_error.__class__.__name__}: {fallback_error}"
                    ) from fallback_error
    except TimeoutError as exc:
        raise PageNavigationError(
            f"page_navigation timed out after {PAGE_NAVIGATION_TIMEOUT_SECONDS:g} seconds"
        ) from exc

    _notify_page_phase(phase_callback, "page_ready")
    return page

# screenshotsave
def save_screenshot(page, savePath, timeout_ms=5000, max_retries=3):
    """
    savescreenshot，带retry机制
    """
    # 确保directory存在 (只需execute一 times)
    try:
        os.makedirs(os.path.dirname(savePath), exist_ok=True)
    except Exception as e:
        print(f"###创建directoryfailed: {str(e)}")
        return False

    for i in range(max_retries):
        try:
            # attemptscreenshot
            page.screenshot(path=savePath, full_page=False, timeout=timeout_ms)
            print(f"###screenshotsave至: {savePath}")
            return True
        except Exception as e:
            print(f"###screenshotsavefailed (attempt {i+1}/{max_retries}): {str(e)}")
            if i < max_retries - 1:
                time.sleep(1)  # failed后稍作wait再retry
    
    return False

# CDP移动
def cdp_mouse_move(client, end_x, end_y, steps=20):
    """
    使用CDP协议模拟Playwright的page.mouse.move多步移动功能
    
    params:
    client - CDP会话
    end_x, end_y - 目标坐标
    steps - 移动的步数
    """
    import time
    
    # 首先get鼠标current位置（如果无法get，可以假设一个起始位置）
    try:
        # 注意：CDP没有直接get鼠标位置的方法，这里通过JavaScriptget
        position = client.send("Runtime.evaluate", {
            "expression": "({x: window.mouseX || 0, y: window.mouseY || 0})",
            "returnByValue": True
        })
        start_x = position["result"]["value"]["x"]
        start_y = position["result"]["value"]["y"]
    except:
        # 如果无法get，假设起始位置为(0,0)
        start_x, start_y = 0, 0
    
    # 计算移动增量
    delta_x = (end_x - start_x) / steps
    delta_y = (end_y - start_y) / steps
    
    # execute多步移动
    for step in range(1, steps + 1):
        current_x = start_x + delta_x * step
        current_y = start_y + delta_y * step
        
        # 发送鼠标移动事件
        client.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", 
            "x": current_x, 
            "y": current_y
        })
        
        # 可选：添加小延迟使移动更自然
        time.sleep(0.01)  # 10毫秒延迟
    
    # 更新全局鼠标位置（可选，方便下 times调用）
    client.send("Runtime.evaluate", {
        "expression": f"window.mouseX = {end_x}; window.mouseY = {end_y};"
    })

# CDP点击
def mouse_up_and_down(page, client, x, y, time_wait=500):
    # client.send("Input.dispatchMouseEvent", {
    #     "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1
    # })
    # page.wait_for_timeout(time_wait)  # 保持按下
    # client.send("Input.dispatchMouseEvent", {
    #     "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1
    # })
    page.mouse.click(x, y)

# 点击
def click(page, client, x, y, time_wait=500):
    cdp_mouse_move(client, x, y, steps=20)
    #page.wait_for_timeout(500)

    # executeCDP点击操作
    mouse_up_and_down(page, client, x, y, time_wait)
    page.wait_for_timeout(3000)

    return page, client

# 双击
def doubleclick(page, client, x, y, time_wait=500):
    # execute点击操作
    cdp_mouse_move(client, x, y, steps=20)
 
    time_wait = random.randint(time_wait // 2, time_wait)
    mouse_up_and_down(page, client, x, y, time_wait)
    
    time_wait = random.randint(time_wait // 2, time_wait)
    mouse_up_and_down(page, client, x, y, time_wait)

    page.wait_for_timeout(3000)
    
    return page, client

# 按压快捷键
def hotkey(page, client, hot_key_value, time_wait=500):
    # 映射常见键名到 Playwright 格式
    key_map = {
        "ctrl": "Control", "control": "Control",
        "alt": "Alt", "shift": "Shift",
        "meta": "Meta", "cmd": "Meta", "command": "Meta",
        "enter": "Enter", "return": "Enter",
        "tab": "Tab", "escape": "Escape", "esc": "Escape",
        "backspace": "Backspace", "delete": "Delete",
        "space": " ", "arrowup": "ArrowUp", "arrowdown": "ArrowDown",
        "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
        "home": "Home", "end": "End",
        "pageup": "PageUp", "pagedown": "PageDown",
    }

    # CDP keyCode 映射（用于 CDP Input.dispatchKeyEvent）
    cdp_key_codes = {
        "Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8, "Delete": 46,
        " ": 32, "ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39,
        "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34,
    }

    # 支持 "ctrl+a", "ctrl+shift+t", "ctrl a", "enter" 等格式
    if "+" in hot_key_value:
        keys = [k.strip() for k in hot_key_value.split("+")]
    else:
        keys = hot_key_value.strip().split()
    mapped = [key_map.get(k.lower(), k) for k in keys]

    # 单键且有 CDP keyCode → 用 CDP 直接发送（更可靠）
    if len(mapped) == 1 and mapped[0] in cdp_key_codes:
        key_name = mapped[0]
        key_code = cdp_key_codes[key_name]
        client.send("Input.dispatchKeyEvent", {
            "type": "keyDown",
            "windowsVirtualKeyCode": key_code,
            "code": key_name,
            "key": key_name,
        })
        client.send("Input.dispatchKeyEvent", {
            "type": "keyUp",
            "windowsVirtualKeyCode": key_code,
            "code": key_name,
            "key": key_name,
        })
    else:
        # 组合键用 Playwright keyboard.press
        combo = "+".join(mapped)
        page.keyboard.press(combo)

    page.wait_for_timeout(time_wait)
    return page, client

# 键入
def click_type(page, client, x, y, content, time_wait=500):
    # 1. 使用CDPexecute点击
    if x != -1 and y != -1:
        page, client = click(page, client, x, y, time_wait) 
        page.wait_for_timeout(random.randint(100, 200))
    
    system = platform.system()
    
    if system == "Darwin":  # macOS
        # 在 macOS 上使用 Command+A (Meta+A)
        page.keyboard.press("Meta+a")
    else:  # Windows 和 Linux/Ubuntu
        # 在 Windows 和 Linux 上使用 Control+A
        page.keyboard.press("Control+a")
    page.keyboard.press('Backspace')
    page.wait_for_timeout(2500)

    for char in content:
        page.keyboard.type(char, delay=random.randint(10, 50))

    # execute完后暂停2秒
    page.wait_for_timeout(2000)

    return page, client

# 滑动
def scroll(page, client, delta_x, delta_y):
    viewport_size = page.viewport_size
    center_x = viewport_size["width"] / 2
    center_y = viewport_size["height"] / 2
    client.send("Input.dispatchMouseEvent", {
        "type": "mouseWheel",
        "x": center_x,
        "y": center_y,
        "deltaX": delta_x, # 正值scroll down，负值scroll up
        "deltaY": delta_y  # 正值scroll down，负值scroll up
    })
    return page, client

# 滑动(固定区域)
def scroll_menu(page, client, x, y, delta_x, delta_y):
    print("x,y,delta_y:", x, y, delta_y)
    cdp_mouse_move(client, x, y, steps=20)
    client.send("Input.dispatchMouseEvent", {
        "type": "mouseWheel",
        "x": x,
        "y": y,
        "deltaX": delta_x,
        "deltaY": delta_y  # 正值scroll down，负值scroll up
    })
    return page, client

# 拉拽
def drag(page, client, x1, y1, x2, y2, steps=20):
    """
    page - Playwrightpage对象
    client - CDP会话
    x1, y1 - 起始坐标
    x2, y2 - 目标坐标
    steps - drag过程的步数
    """
    import time
    
    # 1. move mouse到起始位置
    cdp_mouse_move(client, x1, y1, steps=steps)
    page.wait_for_timeout(100)
    
    # 2. press mouse左键
    client.send("Input.dispatchMouseEvent", {
        "type": "mousePressed",
        "x": x1,
        "y": y1,
        "button": "left",
        "clickCount": 1
    })
    page.wait_for_timeout(100)
    
    # 3. 分步移动到目标位置
    delta_x = (x2 - x1) / steps
    delta_y = (y2 - y1) / steps
    
    for step in range(1, steps + 1):
        current_x = x1 + delta_x * step
        current_y = y1 + delta_y * step
        
        client.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved",
            "x": current_x,
            "y": current_y,
            "button": "left",  # drag时保持按下status
            "buttons": 1       # 表示左键被按下
        })
        
        # 添加小延迟使drag更自然
        time.sleep(0.01)  # 10毫秒延迟
    
    # 4. 在目标位置释放鼠标
    client.send("Input.dispatchMouseEvent", {
        "type": "mouseReleased",
        "x": x2,
        "y": y2,
        "button": "left",
        "clickCount": 1
    })
    
    page.wait_for_timeout(2000)  # waitdrag效果complete
    return page, client

# wait
def wait(page, client, time):
    page.wait_for_timeout(time)  # waitdrag效果complete
    return page, client

# parse动作类型
def parse_action_type(action_str):
    action_str = action_str.strip()
    if action_str.startswith('click('):
        return 'LeftClick'
    elif action_str.startswith('doubleclick(') or action_str.startswith('left_double('):
        return 'DoubleClick'
    elif action_str.startswith('right_single('):
        return 'RightClick'
    elif action_str.startswith('hover('):
        return 'Hover'
    elif action_str.startswith('select('):
        return 'Select'
    elif action_str.startswith('drag('):
        return 'Drag'
    elif action_str.startswith('hotkey(') or action_str.startswith('hotkey '):
        return 'Hotkey'
    elif action_str.startswith('type('):
        return 'Type'
    elif action_str.startswith('stop('):
        return 'Stop'
    elif action_str.startswith('scroll'):
        # 新的动作空间
        if "scroll(" in action_str: 
            if "up" in action_str:
                return 'ScrollUp'
            elif "down" in action_str:
                return 'ScrollDown'
            elif "left" in action_str:
                return 'ScrollLeft'
            elif "right" in action_str:
                return 'ScrollRight'
            else:
                print("scroll parse error !")
                exit()
        else:
            print("scroll parse error !")
            exit()
    elif action_str == 'finish()' or action_str == 'finished()':
        return 'Finish'
    elif action_str == 'wait()':
        return 'Wait'
    elif action_str == 'call_user()':
        return 'CallUser'
    else:
        return 'Unknown'
           
# parse动作
def parse_action(action_str):
    response = action_str
    # print(response)
    try:
        if "type(content" in response:
            type_value = extract_between_keywords(response,"content='", "')")
            if len(type_value) == 0:
                type_value = extract_between_keywords(response, 'content="', '")')
            if len(type_value) == 0:
                print("type parse不出内容！")
                exit()

            action = {"name": "Type", "value": type_value[0]}

        elif "click(start_box=" in response:
            coord = extract_box_coordinate(response, "start_box")
            if coord:
                cx, cy = coord
                action = {"name": "LeftClick", "coordinate": [cx, cy]}
            else:
                action = {"name": "Unknown"}

        elif "left_double(start_box=" in response or "doubleclick(start_box=" in response:
            coord = extract_box_coordinate(response, "start_box")
            if coord:
                cx, cy = coord
                action = {"name": "DoubleClick", "coordinate": [cx, cy]}
            else:
                action = {"name": "Unknown"}

        elif "right_single(start_box=" in response:
            coord = extract_box_coordinate(response, "start_box")
            if coord:
                cx, cy = coord
                action = {"name": "RightClick", "coordinate": [cx, cy]}
            else:
                action = {"name": "Unknown"}

        elif "hover(start_box=" in response:
            coord = extract_box_coordinate(response, "start_box")
            if coord:
                cx, cy = coord
                action = {"name": "Hover", "coordinate": [cx, cy]}
            else:
                action = {"name": "Unknown"}

        elif "scroll(" in response:
            # 提取 direction
            dir_match = re.search(r"direction='(\w+)'", response)
            direction = dir_match.group(1) if dir_match else None

            # 提取坐标（兼容 <|box_start|> 和 (x,y) 两种格式）
            coord = extract_box_coordinate(response, "start_box")

            if direction:
                direction_map = {"up": "ScrollUp", "down": "ScrollDown", "left": "ScrollLeft", "right": "ScrollRight"}
                action = {"name": direction_map.get(direction, "ScrollDown")}
            elif "up" in response:
                action = {"name": "ScrollUp"}
            elif "down" in response:
                action = {"name": "ScrollDown"}
            elif "left" in response:
                action = {"name": "ScrollLeft"}
            elif "right" in response:
                action = {"name": "ScrollRight"}
            else:
                print("scroll parse error !")
                exit()

            if coord:
                action["coordinate"] = [coord[0], coord[1]]

        elif "hotkey" in response:
            # Support both key='...' and key="..."
            hotkey_value = extract_between_keywords(response, "key='", "')")
            if not hotkey_value:
                hotkey_value = extract_between_keywords(response, "key=\"", "\")")
            if not hotkey_value:
                hotkey_value = extract_between_keywords(response, "\"", "\"")
            if hotkey_value:
                hotkey_value = hotkey_value[0].replace(".", "")
            else:
                hotkey_value = ""
            print(f"hotkey_value:{hotkey_value}")
            action = {"name": "HotKey", "value": hotkey_value}

        elif "drag" in response:
            start = extract_box_coordinate(response, "start_box")
            end = extract_box_coordinate(response, "end_box")
            if start and end:
                x1, y1 = start
                x2, y2 = end
                action = {"name": "Drag", "bbox": [x1, y1, x2, y2]}
            else:
                # Fall back to old format
                try:
                    click_point = extract_between_keywords(response, "start_box='(", ")'")[0]
                    c1, c2 = click_point[0].split("),(")
                    x1, y1 = c1.split(",")
                    x2, y2 = c2.split(",")
                    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
                    action = {"name": "Drag", "bbox": [x1, y1, x2, y2]}
                except:
                    action = {"name": "Unknown"}

        elif "finished()" in response or "finish()" in response:
            action = {"name": "Finish"}

        elif "call_user()" in response:
            action = {"name": "CallUser"}

        elif "wait()" in response:
            action = {"name": "Wait"}

        else:
            action = {"name": "Unknown"}

    except:
        print("parse error")
        action = {"name": "Unknown"}

    return action

# execute动作
def excute_action(page, client, action):
    name = action["name"]

    # 记录动作execute前的所有page
    old_pages = set(page.context.pages)

    viewport_size = page.viewport_size
    page_height = viewport_size["height"]
    page_width = viewport_size["width"]

    if name == "Type":
        content = action["value"]
        cx,cy = -1,-1
        page, client = click_type(page, client, cx, cy, content, time_wait=1000)

    elif name == "LeftClick":
        cx,cy = action["coordinate"]
        page, client = click(page, client, cx, cy, time_wait=1000)

    elif name == "DoubleClick":
        cx,cy = action["coordinate"]
        page, client = doubleclick(page, client, cx, cy, time_wait=1000)

    elif name == "Hover":
        cx,cy = action["coordinate"]
        cdp_mouse_move(client, cx, cy, steps=20)

    elif name == "RightClick":
        cx, cy = action["coordinate"]
        cdp_mouse_move(client, cx, cy, steps=20)
        page.mouse.click(cx, cy, button="right")
        page.wait_for_timeout(3000)

    elif name == "HotKey":
        hot_key_value = action["value"]
        try:
            page, client = hotkey(page, client, hot_key_value)
        except Exception as e:
            print(f"⚠️ hotkey 执行失败 (key='{hot_key_value}'): {e}")

    elif "Scroll" in name:
        if name in ["ScrollUp", "ScrollDown", "ScrollLeft", "ScrollRight"]:
            if name == "ScrollUp":
                delta_x = 0
                delta_y = -(page_height - 100)
            elif name == "ScrollDown":
                delta_x = 0
                delta_y = page_height - 100
            elif name == "ScrollLeft":
                delta_x = -(page_width - 100)
                delta_y = 0
            elif name == "ScrollRight":
                delta_x = page_width - 100
                delta_y = 0
            
            if "coordinate" in action:
                x, y = action["coordinate"]
                page, client = scroll_menu(page, client, x, y, delta_x, delta_y)
            else:
                page, client = scroll(page, client, delta_x, delta_y)

        else:
            print(f"parse error: {name}")
            exit()

    elif name == "Drag":
        x1, y1, x2, y2 = action["bbox"]
        page, client = drag(page, client, x1, y1, x2, y2, steps=20)

    elif name == "Wait":
        page, client = wait(page, client, time=3000)
   
    # 统一检测page跳转并清理旧page
    # 计算新增的page
    current_pages = set(page.context.pages)
    new_pages = current_pages - old_pages
    print("len new pages:", len(list(new_pages)))

    final_page = page
    final_client = client

    # 如果有new page，返回它；否则返回原page
    if new_pages:
        new_page = list(new_pages)[0]
        new_page.set_viewport_size({"width": browser_window_size_width, "height": browser_window_size_height})
        new_client = page.context.new_cdp_session(new_page)
        print(f"检测到new page，currentlyclose {len(old_pages)} 个旧page...")
        
        # closed_pages = old_pages
        # 最后一页保留，防止new page打不开导致browserclose
        closed_pages = list(old_pages)[:-1]
        
        for p in closed_pages: 
            try:
                # run_before_unload=False 强制close，忽略"是否离开"弹窗
                p.close(run_before_unload=False)
            except Exception as e:
                print(f"closing old pagesfailed: {e}")

        final_page = new_page
        final_client = new_client

    wait_for_rendering_complete(final_page)

    return final_page, final_client

# waitpage渲染
def wait_for_rendering_complete(page):
    try:
        # waitpage load和网络空闲
        page.wait_for_load_state("load", timeout=30000)
        #page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=10000)

        # 简单checkpagestatus，如果这个调用success，page很可能是稳定的
        is_ready = page.evaluate("() => document.readyState")
        if is_ready != "complete":
            print(f"Warning: document.readyState is {is_ready}, not 'complete'")

        # 然后再attemptexecute更复杂的评估
        page.evaluate("""() => {
            return new Promise(resolve => {
                let lastHeight = document.body.scrollHeight;
                let checkCount = 0;
                const interval = setInterval(() => {
                    const currentHeight = document.body.scrollHeight;
                    if (currentHeight === lastHeight || checkCount > 10) {
                        clearInterval(interval);
                        resolve();
                    } else {
                        lastHeight = currentHeight;
                        checkCount++;
                    }
                }, 100);
            });
        }""")

        # 确保有实际内容
        page.wait_for_function("""() => {
            const content = document.body.innerText.trim();
            return content.length > 0;
        }""", timeout=5000)

    except Exception as e:
        print(f"Warning: wait_for_rendering_complete encountered an error: {e}")
        # 如果发生error，再 timesattemptwaitpage load
        try:
            page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass  # 忽略二 timeswait的error

# getvimcparse元素info
def get_vimc_elements(page):
    # ② 提取 highlight 元素
    print("🛠️ execute highlight_dom_sync 方法...")
    # page.wait_for_timeout(1000)
    # result_handle = page.evaluate_handle("""() => window.__highlight_dom_sync__?.()""")
    # page.wait_for_timeout(10000)
    page.evaluate("""() => {
    window.__highlight_completed__ = false;
    Promise.resolve(window.__highlight_dom_sync__?.())
      .then(result => {
        window.__highlight_result__ = result;
        window.__highlight_completed__ = true;
      });
    }""")
    page.wait_for_timeout(2000)
    try:
        page.wait_for_function("""() => window.__highlight_completed__ === true""", timeout=30000)
        result_handle = page.evaluate_handle("""() => window.__highlight_result__""")
        element_handles = result_handle.get_properties()
        print(f"✨ Highlight 元素数量: {len(element_handles)}")

        highlighted_elements = []
        for idx, handle in element_handles.items():
            try:
                obj = handle.json_value()
                if "attributes" in obj:
                    if "id" in obj["attributes"]:
                        obj["attributes"]["attributes_id"] = obj["attributes"]["id"]
                        del obj["attributes"]["id"]
                highlighted_elements.append(obj)
            except Exception as e:
                print(f"⚠️ 提取第 {idx} 个元素failed：{e}")

        page.evaluate("""() => window.__clear_highlight_dom_sync__?.()""")
        page.wait_for_timeout(1000)
    except Exception as e:
        return []
    return highlighted_elements

def get_url(scribe_path):
    #scribe_path_raw = scribe_path.replace("document_details_parse_", "document_details_").replace("_addthink_fix_aug", "").replace("_addthink_aug", "").replace("_aug_list.json",".json")
    scribe_path_raw = scribe_path.replace("document_details_parse_", "document_details_").replace("_addthink_fix", "").replace("_addthink", "")
    print(scribe_path_raw)
    scribe_items = json.load(open(scribe_path_raw))
    title = scribe_items["title"]
    action_list = scribe_items["actions"]
    #print(action_list[0]["description"])
    if "Navigate to" not in action_list[0]["description"]:
        return None, title
    matchs = extract_between_keywords(action_list[0]["description"], "(", ")")
    if len(matchs) == 0:
        matchs = extract_between_keywords(action_list[0]["description"], "<", ">")
    url = matchs[0].split("?")[0]
    print(url, title)    
    return url, title

def check_url_accessible(cdp_url, url, timeout=5000):
    """check URL 是否可访问（sandbox版）"""
    with sync_playwright() as p:
        headers = get_cdp_headers()
        browser = p.chromium.connect_over_cdp(cdp_url, **({"headers": headers} if headers else {}))
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        
        try:
            # response = page.goto(url, timeout=timeout, wait_until='domcontentloaded')
            response = page.goto(url, timeout=timeout, wait_until='commit')
            # check响应status码
            if response and response.status < 400:
                return True, response.status
            else:
                return False, response.status if response else None

        except Exception as e:
            return False, str(e)

# 监听请求类
class RequestCollector:
    """Collect API traffic and browser download outcomes for one task context."""
    def __init__(self):
        # Keep complete records on disk while a task is running. A long-lived
        # page can emit a large amount of XHR/Fetch traffic, so retaining every
        # record in a Python list would grow with task duration and Worker count.
        self._spool_path = None
        self._spool_connection = None
        self._request_count = 0
        self._download_count = 0
        self._attached_page_ids = set()
        self._download_tasks = set()
        self.clear()
    
    def clear(self):
        """Start a fresh on-disk request spool for the next task."""
        self._cancel_download_tasks()
        self._close_spool(delete=True)
        fd, spool_path = tempfile.mkstemp(prefix="wr048_capture_", suffix=".sqlite3")
        os.close(fd)
        self._spool_path = spool_path
        self._spool_connection = sqlite3.connect(spool_path)
        self._spool_connection.execute(
            "CREATE TABLE request_records ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp REAL NOT NULL, "
            "record_json TEXT NOT NULL"
            ")"
        )
        self._spool_connection.commit()
        self._request_count = 0
        self._download_count = 0

    def _cancel_download_tasks(self):
        for task in tuple(self._download_tasks):
            if not task.done():
                task.cancel()
        self._download_tasks.clear()

    def _close_spool(self, delete=False):
        connection = self._spool_connection
        self._spool_connection = None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        spool_path = self._spool_path
        self._spool_path = None
        if delete and spool_path:
            try:
                os.unlink(spool_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _append_record(self, record):
        """Persist one complete record and return its internal row id."""
        if self._spool_connection is None:
            self.clear()
        record_json = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        cursor = self._spool_connection.execute(
            "INSERT INTO request_records (timestamp, record_json) VALUES (?, ?)",
            (float(record.get("timestamp", 0)), record_json),
        )
        # Commit each event so the collector never accumulates a large SQLite
        # transaction in memory and already-collected data survives later errors.
        self._spool_connection.commit()
        self._request_count += 1
        return cursor.lastrowid

    def _update_record(self, sequence, record):
        if self._spool_connection is None:
            return
        record_json = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        self._spool_connection.execute(
            "UPDATE request_records SET record_json = ? WHERE sequence = ?",
            (record_json, sequence),
        )
        self._spool_connection.commit()

    def attach_context_async(self, context):
        """Capture XHR/Fetch from existing and future tabs in one task context."""
        if context is None:
            return
        for page in context.pages:
            self.attach_page_async(page)
        context.on("page", self.attach_page_async)

    def attach_page_async(self, page):
        """Attach once because a page can be seen through both context and tracker flows."""
        if page is None or id(page) in self._attached_page_ids:
            return
        self._attached_page_ids.add(id(page))
        page.on("request", self.handle_request_async)
        page.on("download", self.handle_download)

    def handle_download(self, download):
        """Track every browser download, including server and blob downloads."""
        task = asyncio.create_task(self._record_download_async(download))
        self._download_tasks.add(task)
        task.add_done_callback(self._download_tasks.discard)

    async def _record_download_async(self, download):
        page_url = None
        try:
            page_url = download.page.url
        except Exception:
            pass

        record = {
            "evidence_type": "download",
            "timestamp": time.time(),
            "url": download.url,
            "page_url": page_url,
            "filename": download.suggested_filename,
            "status": "pending",
            "failure": None,
        }
        sequence = self._append_record(record)
        self._download_count += 1
        try:
            failure = await download.failure()
            record["failure"] = failure
            record["status"] = "completed" if failure is None else "failed"
        except Exception as exc:
            record["status"] = "unknown"
            record["failure"] = f"{exc.__class__.__name__}: {exc}"
        self._update_record(sequence, record)
    
    def handle_request(self, request):
        """process每个请求"""
        req_data = {
            "evidence_type": "request",
            "timestamp": time.time(),
            "url": request.url,
            "page_url": self._get_request_page_url(request),
            "method": request.method,
            "headers": dict(request.headers),
            "resource_type": request.resource_type,
            "post_data": None,
            "post_text": None,
            "json_data": None
        }
        
        if request.resource_type not in ["xhr", "fetch"]:
            return
        
        raw = request.post_data_buffer
        if raw:
            req_data["raw_post_data"] = raw.hex()
            try:
                text = raw.decode("utf-8")
                req_data["post_data"] = text
                try:
                    req_data["json_data"] = json.loads(text)
                except:
                    pass
                if req_data["post_data"] is None:
                    return
            except UnicodeDecodeError:
                pass
        
        self._append_record(req_data)

    async def handle_request_async(self, request):
        """process每个请求（Playwright Async API 版本）"""
        if request.resource_type not in ["xhr", "fetch"]:
            return

        req_data = {
            "evidence_type": "request",
            "timestamp": time.time(),
            "url": request.url,
            "page_url": self._get_request_page_url(request),
            "method": request.method,
            "headers": dict(request.headers),
            "resource_type": request.resource_type,
            "post_data": None,
            "post_text": None,
            "json_data": None,
        }

        # 不同 Playwright 版本里 post_data_buffer 可能是 bytes 属性，也可能是可 await 的方法。
        raw = request.post_data_buffer
        if callable(raw):
            raw = await raw()
        if raw:
            req_data["raw_post_data"] = raw.hex()
            try:
                text = raw.decode("utf-8")
                req_data["post_data"] = text
                try:
                    req_data["json_data"] = json.loads(text)
                except Exception:
                    pass
                if req_data["post_data"] is None:
                    return
            except UnicodeDecodeError:
                pass

        self._append_record(req_data)

    async def finalize_downloads(self, timeout=10):
        """Wait briefly for downloads already initiated by the browser to settle."""
        pending = tuple(self._download_tasks)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    @staticmethod
    def _get_request_page_url(request):
        """Return the document URL that initiated an XHR/Fetch request, if available."""
        try:
            page_url = request.frame.page.url
            return page_url or None
        except Exception:
            # Requests can outlive a detached frame during navigation. The request
            # itself remains useful, so page context is best-effort only.
            return None
    
    def save_results(self, save_result_path="capture.json"):
        """Stream the complete on-disk spool into the official JSON shape."""
        request_count = self._request_count
        download_count = self._download_count
        connection = self._spool_connection
        if connection is None:
            self.clear()
            connection = self._spool_connection

        try:
            with open(save_result_path, "w", encoding="utf-8") as f:
                f.write("{\n")
                f.write('    "capture_time": ')
                json.dump(time.strftime("%Y-%m-%d %H:%M:%S"), f, ensure_ascii=False)
                f.write(",\n")
                f.write(f'    "total_requests": {request_count},\n')
                f.write('    "all_requests": [')
                first = True
                cursor = connection.execute(
                    "SELECT record_json FROM request_records "
                    "ORDER BY timestamp ASC, sequence ASC"
                )
                for (record_json,) in cursor:
                    if first:
                        f.write("\n")
                        first = False
                    else:
                        f.write(",\n")
                    f.write("        ")
                    f.write(record_json)
                if not first:
                    f.write("\n    ")
                f.write("]\n}\n")
        finally:
            self._cancel_download_tasks()
            self._close_spool(delete=True)

        print("result已save到 capture.json")
        return request_count, download_count

    async def save_results_async(self, save_result_path="capture.json"):
        await self.finalize_downloads()
        return self.save_results(save_result_path)
