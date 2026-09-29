"""Privacy-safe runtime timeline helpers for the shared application log."""

import logging
import time
import traceback


def safe_error_kind(value: object) -> str:
    """Map an error/result to a small category without logging its contents."""
    text = str(value).lower()
    if "initializeserver" in text and "timeout" in text:
        return "mcp_initialize_timeout"
    if "browserbackend.calltool" in text and "timeout" in text:
        return "mcp_tool_timeout"
    if "connection closed" in text or "transport closed" in text or "session closed" in text:
        return "mcp_transport_closed"
    if "browser disconnected" in text:
        return "browser_disconnected"
    if "targetclosed" in text or "browser has been closed" in text:
        return "browser_target_closed"
    if "no open pages" in text:
        return "no_open_pages"
    if "page.goto" in text or "net::err_" in text:
        return "page_navigation_error"
    if "locator.click" in text and "timeout" in text:
        return "element_action_timeout"
    if "ref " in text and "not found" in text:
        return "stale_page_reference"
    if "timeout" in text:
        return "timeout"
    if "connection" in text or "transport" in text or "mcp" in text:
        return "connection_error"
    if "referenceerror" in text or "syntaxerror" in text or "typeerror" in text:
        return "script_error"
    return "tool_error"
def elapsed_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)


def log_safe_exception(
    logger: logging.Logger,
    event: str,
    error: BaseException,
    started_at: float | None = None,
) -> None:
    """Log type and code stack only; exception text can contain task data."""
    duration = f" duration_ms={elapsed_ms(started_at)}" if started_at is not None else ""
    stack = "".join(traceback.format_tb(error.__traceback__)).strip()
    logger.error(
        "%s status=error error_type=%s error_kind=%s%s\n%s",
        event,
        error.__class__.__name__,
        safe_error_kind(error),
        duration,
        stack or "<stack unavailable>",
    )
