import asyncio
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agentscope.agent import Agent, ContextConfig, InjectionConfig, ModelConfig, ReActConfig
from agentscope.credential import OpenAICredential
from agentscope.mcp import MCPClient, StdioMCPConfig
from agentscope.message import UserMsg
from agentscope.model import OpenAIChatModel
from agentscope.permission import PermissionMode
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, ToolChoice, Toolkit

import web_controller
from middlewares import (
    BrowserEvaluateContextMiddleware,
    BrowserTabGuardMiddleware,
    ContextCompressionRecoveryMiddleware,
    ResultRecordMiddleware,
    RuntimeTimelineMiddleware,
    SnapshotContextMiddleware,
    ToolTimeoutMiddleware,
    TrajectoryScreenshotMiddleware,
    WorkerLogMiddleware,
)
from prompts import SYSTEM_PROMPT, build_user_prompt
from runtime_timeline import elapsed_ms, log_safe_exception
from tools import BrowserPageTracker, BrowserToolRuntime, ScreenshotCoordinateTool, ScreenshotVlmTool
from errors import (
    MCPConnectionError,
    TaskDeadlineExceededError,
    classify_infrastructure_error,
)


ENABLED_BROWSER_TOOLS = {
    # 只暴露 Web 任务真正需要的 Playwright MCP 工具，避免模型被文件系统、截图等无关工具干扰。
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_select_option",
    "browser_fill_form",
    "browser_find",
    "browser_hover",
    "browser_wait_for",
    "browser_tabs",
    "browser_evaluate",
    "browser_run_code_unsafe",
}

MCP_CONNECT_TIMEOUT_SECONDS = 180
MCP_CONNECT_MAX_ATTEMPTS = 2
MCP_CONNECT_RETRY_DELAY_SECONDS = 3
MCP_CLOSE_TIMEOUT_SECONDS = 15
MODEL_CALL_TIMEOUT_SECONDS = 180
TASK_TIMEOUT_SECONDS = 60 * 60
PLAYWRIGHT_MCP_PACKAGE = os.getenv("PLAYWRIGHT_MCP_PACKAGE", "@playwright/mcp@0.0.79")


# Browser snapshots can be very large. A single concise field gives the model
# a small, deterministic target during context compression instead of asking it
# to produce five unbounded prose fields.
WEB_SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary"],
    "properties": {
        "summary": {
            "type": "string",
            "maxLength": 6000,
            "description": (
                "A concise, self-contained continuation note under 6000 characters: "
                "task goal, current page state, evidence found, and the next browser action."
            ),
        },
    },
}
WEB_COMPRESSION_PROMPT = (
    "<system-hint>Compress prior browser work into the required JSON field. "
    "Do not reproduce page snapshots, tool output, or long lists verbatim. "
    "Keep the summary below 6000 characters and make it actionable.</system-hint>"
)
WEB_SUMMARY_TEMPLATE = (
    "<system-info>Here is a summary of your previous work\n"
    "{summary}\n"
    "</system-info>"
)


class WebAgentOpenAIChatModel(OpenAIChatModel):
    """Use automatic choice for framework-owned structured-output tools."""

    async def _call_api_with_structured_output(
        self,
        model_name: str,
        messages: list[Any],
        structured_model: Any,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> Any:
        # Context compression supplies a synthetic schema tool. Let compatible
        # providers select it automatically instead of forcing a named tool.
        return await super()._call_api_with_structured_output(
            model_name=model_name,
            messages=messages,
            structured_model=structured_model,
            tool_choice=ToolChoice(mode="auto"),
            **kwargs,
        )
LOCAL_RECOVERY_SUMMARY_TEMPLATE = (
    "<system-info>Here is a summary of your previous work\n"
    "Task: {task}\n"
    "The model-generated context summary was unavailable. Continue from the retained recent "
    "browser tool calls and results below. Re-check the current page before repeating an action.\n"
    "</system-info>"
)

@dataclass
class AgentRunResult:
    status: str
    result_item: dict[str, Any]
    mcp_connection_failed: bool = False
    retryable: bool = False
    failure_kind: str | None = None


async def create_worker_mcp_client(
    worker_id: int,
    cdp_url: str,
    logger: Any,
) -> MCPClient:
    """Start one Playwright MCP process for a worker, with bounded recovery."""
    last_error: Exception | None = None
    for attempt in range(1, MCP_CONNECT_MAX_ATTEMPTS + 1):
        mcp_client = _build_playwright_mcp(worker_id, cdp_url)
        logger.info("mcp_connection_started attempt=%s", attempt)
        started_at = asyncio.get_running_loop().time()
        try:
            # Keep connect/close in the same Worker task; wait_for creates a child
            # task and can violate AnyIO cancel-scope ownership during cleanup.
            async with asyncio.timeout(MCP_CONNECT_TIMEOUT_SECONDS):
                await mcp_client.connect()
            await mcp_client.list_raw_tools()
            logger.info("mcp_connection_finished status=success attempt=%s", attempt)
            return mcp_client
        except Exception as exc:
            last_error = exc
            log_safe_exception(logger, "mcp_connection_finished", exc, started_at)
            try:
                async with asyncio.timeout(MCP_CLOSE_TIMEOUT_SECONDS):
                    await mcp_client.close()
            except Exception as close_error:
                log_safe_exception(logger, "mcp_failed_client_close", close_error)
            if attempt < MCP_CONNECT_MAX_ATTEMPTS:
                delay = MCP_CONNECT_RETRY_DELAY_SECONDS * attempt
                logger.warning(
                    "mcp_connection_retry attempt=%s max_attempts=%s delay_seconds=%s",
                    attempt,
                    MCP_CONNECT_MAX_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)

    raise MCPConnectionError(
        f"Playwright MCP failed to connect after {MCP_CONNECT_MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error


async def close_worker_mcp_client(mcp_client: MCPClient | None, logger: Any) -> None:
    if mcp_client is None:
        return
    try:
        async with asyncio.timeout(MCP_CLOSE_TIMEOUT_SECONDS):
            await mcp_client.close()
        logger.info("mcp_connection_closed status=success")
    except Exception as exc:
        log_safe_exception(logger, "mcp_connection_closed", exc)


def is_mcp_connection_error(exc: Exception) -> bool:
    return isinstance(classify_infrastructure_error(exc), MCPConnectionError)


async def run_agentscope_task(
    *,
    worker_id: int,
    task_idx: int,
    task_id: str,
    task: str,
    website: str,
    page: Any,
    cdp_url: str,
    mcp_client: MCPClient,
    task_output_dir: Path,
    log_file: Path,
    config: dict[str, Any],
    logger: Any,
    timeline_logger: Any,
    deadline_at: float | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> AgentRunResult:
    """Run one WebRetriever task with AgentScope ReAct + Playwright MCP."""

    # ReAct 只使用文本模型；截图工具单独使用 config.json 中的视觉模型。
    task_model = _build_text_model(config)
    vision_model = _build_vision_model(config)
    # 自定义 VLM 工具共用当前任务的 Page；临时截图放在系统临时目录，不污染比赛输出。
    page_tracker = BrowserPageTracker(page)
    runtime = BrowserToolRuntime(page_tracker, task_output_dir)
    # result.json / 任务日志 / trajectory 截图分别独立为 Middleware，职责不要混在主循环里。
    result_recorder = ResultRecordMiddleware(task_idx, task_id, task, website, page_tracker, task_output_dir)
    worker_log = WorkerLogMiddleware(worker_id, log_file)
    trajectory = TrajectoryScreenshotMiddleware(task_idx, page_tracker, task_output_dir / "trajectory")
    browser_tab_guard = BrowserTabGuardMiddleware(page.context)
    try:
        # VLM 始终观察当前窗口。
        vlm_tool = ScreenshotVlmTool(runtime, vision_model, logger, timeline_logger, 180)
        coordinate_tool = ScreenshotCoordinateTool(runtime, vision_model, logger, timeline_logger, 180)

        async def browser_screenshot_vlm(instruction: str) -> str:
            """Capture the current browser viewport and ask a VLM to analyze it.

            Args:
                instruction: Instruction sent to the VLM for screenshot analysis.
            """
            return await vlm_tool(instruction)

        async def browser_screenshot_coordinate(instruction: str) -> str:
            """Capture the current browser viewport and locate a visible target coordinate.

            Args:
                instruction: Instruction describing the visible UI target to locate.
            """
            return await coordinate_tool(instruction)

        toolkit = Toolkit(
            mcps=[mcp_client],
            tools=[
                FunctionTool(
                    browser_screenshot_vlm,
                    name="browser_screenshot_vlm",
                    description=(
                        "Use when DOM tools cannot resolve visible text or when the task needs visual reasoning about layout, "
                        "charts, images, icons, colors, or selected state. It analyzes the current visible window. "
                        "State a focused instruction and use only facts visible in the returned analysis."
                    ),
                    is_read_only=True,
                    is_concurrency_safe=False,
                ),
                FunctionTool(
                    browser_screenshot_coordinate,
                    name="browser_screenshot_coordinate",
                    description=(
                        "Use when DOM tools cannot provide a usable element reference but the target is visible. "
                        "It analyzes the current visible window and returns target coordinates relative to that window "
                        "as {\"x\": number, \"y\": number}, or {\"x\": null, \"y\": null} if not confident."
                    ),
                    is_read_only=True,
                    is_concurrency_safe=False,
                ),
            ],
        )
        custom_tool_names = ("browser_screenshot_vlm", "browser_screenshot_coordinate")
        registered_custom_tools = [
            tool_name
            for tool_name in custom_tool_names
            if await toolkit.get_tool(tool_name) is not None
        ]
        worker_log.record_runtime_info(
            "自定义工具注册",
            "Toolkit 可用工具: " + ", ".join(registered_custom_tools),
        )

        # 每个任务使用独立临时 session，避免读到上一次任务的历史会话或记忆。
        state = AgentState()
        state.session_id = f"web-agent-session-{task_idx}-{uuid.uuid4().hex[:8]}"
        # 本地比赛任务不需要每一步弹权限确认，直接允许工具执行。
        state.permission_context.mode = PermissionMode.BYPASS

        agent = Agent(
            name=f"web-agent-{task_idx}",
            system_prompt=SYSTEM_PROMPT,
            model=task_model,
            toolkit=toolkit,
            state=state,
            middlewares=[
                result_recorder,
                RuntimeTimelineMiddleware(timeline_logger, progress_callback),
                worker_log,
                trajectory,
                # 放在日志中间件内层：WorkerLog 记录的正是模型上下文中实际保留的快照。
                SnapshotContextMiddleware(),
                # 大型 browser_evaluate JSON 常包含重复的祖先节点文本；日志记录压缩后的真实模型输入。
                BrowserEvaluateContextMiddleware(),
                browser_tab_guard,
                ToolTimeoutMiddleware(180),
                ContextCompressionRecoveryMiddleware(task, LOCAL_RECOVERY_SUMMARY_TEMPLATE),
            ],
            # 每个任务最多 100 步
            react_config=ReActConfig(max_iters=100),
            # 对话摘要压缩：Web 任务的 snapshot/VLM 结果较长，提前压缩历史前缀。
            context_config=ContextConfig(
                trigger_ratio=0.75,
                reserve_ratio=0.1,
                compression_prompt=WEB_COMPRESSION_PROMPT,
                summary_template=WEB_SUMMARY_TEMPLATE,
                summary_schema=WEB_SUMMARY_SCHEMA,
                # 长页面快照的可读部分足够用于决策，限制单次工具结果可避免少数页面
                # 在两三轮内占满上下文，触发高风险的压缩请求。
                tool_result_limit=16000,
            ),
            injection_config=InjectionConfig(
                # 关闭框架运行态注入，避免给模型暴露无关 session/memory/workspace 信息。
                inject_runtime_state=False,
                timezone="Asia/Shanghai",
            ),
            # 不让框架内部重试模型请求，失败交给本轮任务记录，便于定位真实问题。
            model_config=ModelConfig(max_retries=0),
        )

        # 用户提示词只保留网站和任务，避免 id/name/content 等框架字段污染模型输入。
        prompt = build_user_prompt(website, task)
        try:
            task_started_at = asyncio.get_running_loop().time()
            remaining_seconds = TASK_TIMEOUT_SECONDS
            if deadline_at is not None:
                remaining_seconds = max(0.1, deadline_at - task_started_at)
            async with asyncio.timeout(remaining_seconds):
                reply_message = await agent.reply(UserMsg(name="user", content=prompt))
            finish_reason = getattr(reply_message, "finished_reason", None)
            timeline_logger.info(
                "agent_reply_returned finish_reason=%s iterations=%s",
                str(finish_reason or "unknown"),
                getattr(agent.state, "cur_iter", "unknown"),
            )
            result_item = result_recorder.write_success()
            timeline_logger.info("task_result_written status=success duration_ms=%s", elapsed_ms(task_started_at))
            return AgentRunResult(status="SUCCESS", result_item=result_item)
        except Exception as exc:
            failure = (
                TaskDeadlineExceededError(
                    f"task exceeded the {TASK_TIMEOUT_SECONDS} second deadline"
                )
                if isinstance(exc, TimeoutError)
                else classify_infrastructure_error(exc)
            )
            recorded_error = failure or exc
            timeline_logger.warning(
                "agent_terminal_failure failure_kind=%s iterations=%s",
                failure.__class__.__name__ if failure is not None else exc.__class__.__name__,
                getattr(agent.state, "cur_iter", "unknown"),
            )
            result_item = result_recorder.write_failed(str(recorded_error))
            log_safe_exception(timeline_logger, "task_result_written", recorded_error, task_started_at)
            return AgentRunResult(
                status="FAILED",
                result_item=result_item,
                mcp_connection_failed=isinstance(failure, MCPConnectionError),
                retryable=failure is not None and not isinstance(failure, TaskDeadlineExceededError),
                failure_kind=failure.__class__.__name__ if failure is not None else None,
            )
    finally:
        # MCP 由 Worker 持有，跨其顺序执行的任务复用，Worker 退出时统一关闭。
        pass


def _build_text_model(config: dict[str, Any]) -> WebAgentOpenAIChatModel:
    """Build the text-only model used by the ReAct task agent.

    Existing single-model configurations remain valid by falling back to api_*.
    """
    return _build_model(
        config,
        base_key="text_api_base",
        credential_key="text_api_key",
        model_key="text_api_model",
    )


def _build_vision_model(config: dict[str, Any]) -> WebAgentOpenAIChatModel:
    """Build the multimodal model used only by the custom screenshot tools."""
    return _build_model(
        config,
        base_key="api_base",
        credential_key="api_key",
        model_key="api_model",
    )


def _build_model(
    config: dict[str, Any],
    *,
    base_key: str,
    credential_key: str,
    model_key: str,
) -> WebAgentOpenAIChatModel:
    api_key = (
        config.get(credential_key)
        or config.get("api_key")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("VLM_API_KEY")
        or "EMPTY"
    )
    return WebAgentOpenAIChatModel(
        credential=OpenAICredential(
            api_key=api_key,
            base_url=config.get(base_key) or config.get("api_base"),
        ),
        model=config.get(model_key) or config.get("api_model", "qwen3.7-plus"),
        parameters=OpenAIChatModel.Parameters(
            temperature=float(config.get("temperature", 0.2)),
            top_p=float(config.get("top_p", 1.0)),
            max_tokens=int(config.get("max_tokens", 8192)),
            # Web 自动化每轮通常只需要一个动作，关闭并行工具调用能减少 Pending tool call 问题。
            parallel_tool_calls=False,
        ),
        stream=True,
        max_retries=1,
        context_size=int(config.get("context_size", 128000)),
        client_kwargs={"timeout": MODEL_CALL_TIMEOUT_SECONDS},
    )


def _build_playwright_mcp(worker_id: int, cdp_url: str) -> MCPClient:
    # 一个 Worker 只维护一个 MCP 进程；运行文件不能污染评测输出目录。
    project_root = Path(__file__).resolve().parents[2]
    log_dir = project_root / ".playwright-mcp" / f"worker-{worker_id}"
    log_dir.mkdir(parents=True, exist_ok=True)
    npm_cache_dir = project_root / ".playwright-mcp" / "npm-cache"
    npm_cache_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PLAYWRIGHT_MCP_OUTPUT_DIR"] = str(log_dir)
    # Do not depend on the user's global npm cache: it can be locked by another
    # npm process or blocked by local permissions, preventing MCP initialization.
    env["npm_config_cache"] = str(npm_cache_dir)
    env["npm_config_update_notifier"] = "false"
    env["npm_config_prefer_offline"] = "true"
    # npmjs.org can be unreachable from some local networks. Keep an explicit
    # override for managed environments while using a reachable default locally.
    env["npm_config_registry"] = (
        env.get("PLAYWRIGHT_MCP_NPM_REGISTRY")
        or env.get("npm_config_registry")
        or "https://registry.npmmirror.com"
    )
    command = _npx_command()
    args = [
        "-y",
        PLAYWRIGHT_MCP_PACKAGE,
        "--cdp-endpoint",
        cdp_url,
        "--output-dir",
        str(log_dir),
    ]
    for name, value in web_controller.resolve_cdp_headers(cdp_url).items():
        args.extend(["--cdp-header", f"{name}: {value}"])
    return MCPClient(
        name=f"playwright_worker_{worker_id}",
        # stateful MCP 进程持有同一个 CDP 浏览器会话，避免每次工具调用都重新连接浏览器。
        is_stateful=True,
        # MCP 的工作目录同样放在隐藏运行目录，避免自动创建 outputs/ 到任务结果目录。
        mcp_config=StdioMCPConfig(command=command, args=args, env=env, cwd=str(log_dir)),
        # 白名单只保留浏览器导航、点击、输入、页面快照和少量 JS 能力。
        enable_tools=sorted(ENABLED_BROWSER_TOOLS),
        # 使用我们自己的 VLM 截图工具，禁用 MCP 自带截图以减少工具选择干扰。
        disable_tools=["browser_take_screenshot"],
        execution_timeout=180,
    )


def _npx_command() -> str:
    if sys.platform.startswith("win"):
        return shutil.which("npx.cmd") or shutil.which("npx") or "npx.cmd"
    return shutil.which("npx") or "npx"
