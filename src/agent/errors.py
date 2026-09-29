"""Typed errors used to separate recoverable infrastructure failures from task failures."""


class InfrastructureError(RuntimeError):
    """A failure in the browser, MCP transport, or execution infrastructure."""


class BrowserLifecycleError(InfrastructureError):
    """The browser/CDP session or its active page is no longer usable."""


class PageNavigationError(InfrastructureError):
    """The requested page could not load, without proving the browser is unhealthy."""


class MCPConnectionError(InfrastructureError):
    """The MCP process or transport is unavailable."""


class ModelGatewayError(InfrastructureError):
    """A transient model API/network failure that may be retried once."""


class ToolExecutionTimeoutError(InfrastructureError):
    """A browser tool timed out repeatedly in one task."""


class MCPBrowserUnhealthyError(MCPConnectionError):
    """The MCP browser path failed repeatedly and its Worker must be quarantined."""


class TaskDeadlineExceededError(InfrastructureError):
    """The complete Agent execution exceeded its task deadline."""


def classify_infrastructure_error(error: BaseException) -> InfrastructureError | None:
    """Convert known transport/browser failures without hiding unknown errors."""
    if isinstance(error, InfrastructureError):
        return error

    message = str(error).lower()
    if any(
        marker in message
        for marker in (
            "connect_over_cdp",
            "cdp connection failed",
            "browser context",
            "browser session",
            "browser page creation failed",
            "target page, context or browser has been closed",
            "target closed",
            "browser has been closed",
            "无法激活当前任务页签",
        )
    ):
        return BrowserLifecycleError(str(error))

    if any(
        marker in message
        for marker in (
            "err_http_response_code_failure",
            "err_ssl_protocol_error",
            "main resource failed",
            "page navigation timed out",
            "page_navigation timed out",
            "navigation failed after domcontentloaded and commit",
        )
    ):
        return PageNavigationError(str(error))

    if any(
        marker in message
        for marker in (
            "mcp",
            "connection closed",
            "connection reset",
            "broken pipe",
            "not connected",
            "transport closed",
            "session closed",
            "stdio",
            "initializeserver",
        )
    ):
        return MCPConnectionError(str(error))

    if any(
        marker in message
        for marker in (
            "apiconnectionerror",
            "apitimeouterror",
            "request timed out",
            "request timeout",
            "read timeout",
            "connect timeout",
            "rate limit",
            "too many requests",
            "temporarily unavailable",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "status code: 429",
            "status code: 502",
            "status code: 503",
            "status code: 504",
        )
    ):
        return ModelGatewayError(str(error))

    return None
