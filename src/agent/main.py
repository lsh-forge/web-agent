import argparse
import asyncio
import hashlib
import json
import logging
import multiprocessing as mp
import os
import shutil
import tempfile
import time
import traceback
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from typing import Any, List

import web_controller
from agent import (
    TASK_TIMEOUT_SECONDS,
    close_worker_mcp_client,
    create_worker_mcp_client,
    run_agentscope_task,
)
from runtime_timeline import elapsed_ms, log_safe_exception
from errors import (
    BrowserLifecycleError,
    MCPBrowserUnhealthyError,
    MCPConnectionError,
    PageNavigationError,
    TaskDeadlineExceededError,
    ToolExecutionTimeoutError,
)

try:
    import fcntl
except ImportError:  # Windows 本地调试没有 fcntl，比赛 Linux 环境会走文件锁。
    fcntl = None


APP_LOG_FILENAME = "app.log"
MAX_WORKER_RESTARTS = 2
BROWSER_DISCONNECT_TIMEOUT_SECONDS = 15
MCP_HEALTHCHECK_TIMEOUT_SECONDS = 30
PRE_TASK_SESSION_REBUILD_ATTEMPTS = 2
PRE_TASK_SESSION_REBUILD_BACKOFF_SECONDS = (1, 3)
WORKER_HEARTBEAT_INTERVAL_SECONDS = 5
WORKER_HEARTBEAT_TIMEOUT_SECONDS = 30
SCHEDULER_SNAPSHOT_INTERVAL_SECONDS = 30
WORKER_PHASE_TIMEOUTS_SECONDS = {
    # Covers the small window between task claim and the first lifecycle
    # phase callback. It prevents a Worker from holding a task silently.
    "worker_starting": 180,
    "task_claimed": 120,
    "browser_session_connect": 240,
    "mcp_connect": 420,
    "page_create": 90,
    "page_cleanup": 60,
    "page_close": 60,
    "page_activate": 60,
    "page_prepare": 90,
    "page_route": 60,
    "page_navigation": 240,
    "session_reset": 90,
}


class WorkerTimelineFilter(logging.Filter):
    def __init__(self, worker_id: int) -> None:
        self.worker_id = worker_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.worker_id = self.worker_id
        # Task identifiers are diagnostic metadata only; never include task
        # text, URLs, prompts, tool arguments, or tool results in app.log.
        if not hasattr(record, "task_idx"):
            record.task_idx = "-"
        if not hasattr(record, "task_id"):
            record.task_id = "-"
        return True


def start_app_log_listener(project_root: Path):
    """Write all Worker timing events through one parent-owned app.log handler."""
    log_queue = mp.Queue()
    log_file = project_root / APP_LOG_FILENAME
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)s [Worker %(worker_id)s] "
            "[Task %(task_idx)s/%(task_id)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ),
    )
    listener = QueueListener(log_queue, file_handler, respect_handler_level=True)
    listener.start()
    return log_queue, listener, log_file


def build_worker_timeline_logger(worker_id: int, log_queue) -> logging.Logger:
    logger = logging.getLogger(f"app_timeline_worker_{worker_id}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = QueueHandler(log_queue)
    handler.addFilter(WorkerTimelineFilter(worker_id))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def task_timeline_logger(logger: logging.Logger, task_idx: Any, task_id: Any) -> logging.LoggerAdapter:
    """Attach only stable task identifiers to diagnostic events."""
    return logging.LoggerAdapter(
        logger,
        {"task_idx": str(task_idx), "task_id": str(task_id)},
    )


def log_scheduler_snapshot(
    logger: logging.Logger,
    worker_states,
    task_states,
    procs: dict[int, mp.Process],
    completed_tasks,
    total_tasks: int,
) -> None:
    """Record concurrency health without exposing task content."""
    state_values = [dict(state) for state in task_states.values()]
    pending = sum(state.get("status") == "pending" for state in state_values)
    claimed = sum(state.get("status") == "claimed" for state in state_values)
    completed = sum(state.get("status") == "completed" for state in state_values)
    alive = sum(proc.is_alive() for proc in procs.values())
    logger.info(
        "scheduler_snapshot total=%s pending=%s claimed=%s completed=%s "
        "reported_completed=%s alive_workers=%s configured_workers=%s",
        total_tasks,
        pending,
        claimed,
        completed,
        len(completed_tasks),
        alive,
        len(procs),
    )
    now = time.time()
    for worker_id, proc in sorted(procs.items()):
        worker_state = dict(worker_states.get(worker_id, {}))
        current = next(
            (
                state
                for state in state_values
                if state.get("status") == "claimed" and state.get("worker_id") == worker_id
            ),
            {},
        )
        heartbeat_at = float(worker_state.get("heartbeat_at", 0) or 0)
        heartbeat_age_ms = round(max(0.0, now - heartbeat_at) * 1000) if heartbeat_at else -1
        logger.info(
            "worker_snapshot worker_id=%s pid=%s alive=%s exitcode=%s phase=%s "
            "task_idx=%s task_id=%s heartbeat_age_ms=%s restart_count=%s quarantined=%s",
            worker_id,
            proc.pid,
            proc.is_alive(),
            proc.exitcode,
            worker_state.get("phase", "unknown"),
            current.get("task_idx", "-"),
            current.get("task_id", "-"),
            heartbeat_age_ms,
            worker_state.get("restart_count", 0),
            worker_state.get("quarantined", False),
        )


def log_task_artifacts(logger: logging.LoggerAdapter, task_output_dir: Path) -> None:
    """Record artifact presence/counts without reading their contents."""
    try:
        trajectory_dir = task_output_dir / "trajectory"
        visual_dir = task_output_dir / "trajectory_visual"
        logger.info(
            "task_artifacts result=%s capture=%s trajectory_files=%s "
            "trajectory_visual_files=%s",
            (task_output_dir / "result.json").is_file(),
            (task_output_dir / "capture.json").is_file(),
            sum(path.is_file() for path in trajectory_dir.iterdir()) if trajectory_dir.is_dir() else 0,
            sum(path.is_file() for path in visual_dir.iterdir()) if visual_dir.is_dir() else 0,
        )
    except Exception as exc:
        logger.warning("task_artifacts status=unavailable error_type=%s", exc.__class__.__name__)


def update_worker_state(worker_states, worker_id: int, phase: str | None = None) -> None:
    """Update supervisor-visible progress without recording task content."""
    now = time.time()
    state = dict(worker_states.get(worker_id, {}))
    state["heartbeat_at"] = now
    if phase is not None:
        if state.get("phase") != phase:
            state["phase_started_at"] = now
        state["phase"] = phase
        state["progress_at"] = now
    worker_states[worker_id] = state


def quarantine_worker_state(worker_states, worker_id: int, reason: str) -> None:
    """Mark a browser slot unusable for this run; the parent must not restart it."""
    state = dict(worker_states.get(worker_id, {}))
    now = time.time()
    state.update(
        {
            "phase": "quarantined",
            "phase_started_at": now,
            "heartbeat_at": now,
            "progress_at": now,
            "quarantined": True,
            "quarantine_reason": reason,
        },
    )
    worker_states[worker_id] = state


async def worker_heartbeat_loop(worker_id: int, worker_states) -> None:
    """Keep liveness separate from phase progress so a stuck await is detectable."""
    while True:
        state = dict(worker_states.get(worker_id, {}))
        state["heartbeat_at"] = time.time()
        worker_states[worker_id] = state
        await asyncio.sleep(WORKER_HEARTBEAT_INTERVAL_SECONDS)


def get_stale_worker_reason(
    state: dict[str, Any],
    now: float | None = None,
    task_info: dict[str, Any] | None = None,
) -> str | None:
    """Return a reason only for a deadlocked Worker, not a normally long task."""
    effective_state = dict(state or {})
    # The task lease is updated at the same lifecycle boundaries as the
    # Worker state. Prefer it when available so the parent is not dependent
    # on a possibly delayed Manager-dict state refresh.
    if task_info:
        for key in ("phase", "phase_started_at"):
            if task_info.get(key) is not None:
                effective_state[key] = task_info[key]
    if not effective_state or effective_state.get("phase") in {None, "idle", "stopped"}:
        return None
    now = now or time.time()
    heartbeat_at = float(effective_state.get("heartbeat_at", 0) or 0)
    if heartbeat_at and now - heartbeat_at > WORKER_HEARTBEAT_TIMEOUT_SECONDS:
        return f"heartbeat_stale_{int(now - heartbeat_at)}s"

    phase = str(effective_state.get("phase"))
    phase_started_at = float(effective_state.get("phase_started_at", 0) or 0)
    phase_timeout = WORKER_PHASE_TIMEOUTS_SECONDS.get(phase)
    if phase_timeout is not None and phase_started_at and now - phase_started_at > phase_timeout:
        return f"phase_timeout_{phase}_{int(now - phase_started_at)}s"
    return None


def result_requires_session_reset(result: Any) -> bool:
    """Reset browser/MCP only when the session may be unhealthy."""
    if result is None:
        return False
    return result.mcp_connection_failed or result.failure_kind in {
        BrowserLifecycleError.__name__,
        MCPBrowserUnhealthyError.__name__,
        MCPConnectionError.__name__,
        ToolExecutionTimeoutError.__name__,
        TaskDeadlineExceededError.__name__,
    }


def safe_create_directory(base_dir, result_file="result.json"):
    # .lock 文件统一放到 locks/ 子目录，避免和任务目录混在一起。
    parent_dir = os.path.dirname(base_dir)
    lock_dir = os.path.join(parent_dir, "locks")
    os.makedirs(lock_dir, exist_ok=True)
    lock_name = os.path.basename(base_dir) + ".lock"
    lock_file = os.path.join(lock_dir, lock_name)
    try:
        with open(lock_file, "w") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.path.exists(os.path.join(base_dir, result_file)):
                return False
            if os.path.exists(base_dir):
                shutil.rmtree(base_dir)
            os.makedirs(base_dir, exist_ok=True)
            os.makedirs(f"{base_dir}/trajectory", exist_ok=True)
            os.makedirs(f"{base_dir}/trajectory_visual", exist_ok=True)
            return True
    except BlockingIOError:
        return False
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass


def safe_remove_task_dir(task_dir):
    parent_dir = os.path.dirname(task_dir)
    lock_dir = os.path.join(parent_dir, "locks")
    lock_name = os.path.basename(task_dir) + ".lock"
    lock_file = os.path.join(lock_dir, lock_name)
    if os.path.exists(task_dir):
        try:
            shutil.rmtree(task_dir)
        except Exception as e:
            print(f"Error removing task directory {task_dir}: {e}")
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
        except Exception as e:
            print(f"Warning: Failed to remove lock file {lock_file}: {e}")


def safe_write_json(filepath, data, max_retries=3):
    dir_path = os.path.dirname(filepath)
    os.makedirs(dir_path, exist_ok=True)
    tmp_name = None

    for attempt in range(max_retries):
        try:
            # 原子写入，避免多进程下 result/capture 文件半写入。
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=dir_path,
                delete=False,
                suffix=".tmp",
                encoding="utf-8",
            ) as tmp_file:
                json.dump(data, tmp_file, indent=4, ensure_ascii=False)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
                tmp_name = tmp_file.name

            try:
                os.chmod(tmp_name, 0o666)
            except Exception:
                pass

            os.replace(tmp_name, filepath)
            return True
        except Exception as e:
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except Exception:
                    pass
            if attempt < max_retries - 1:
                time.sleep(0.1 * (2 ** attempt))
                continue
            try:
                print(f"Atomic write failed for {filepath}, using direct write. Error: {e}")
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                return True
            except Exception as final_e:
                print(f"Failed to write {filepath} finally: {final_e}")
                return False
    return False


def setup_logger(log_file: Path, worker_id: int):
    """Create one shared log for a single task run."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger_name = f"task_log_{log_file.stem}"
    logger = logging.getLogger(logger_name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - [Worker %(worker_id)s] [PID %(process)d] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    class WorkerFilter(logging.Filter):
        def __init__(self, worker_id):
            self.worker_id = worker_id

        def filter(self, record):
            record.worker_id = self.worker_id
            return True

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(WorkerFilter(worker_id))
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(WorkerFilter(worker_id))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False
    return logger


def close_logger(logger: logging.Logger) -> None:
    """Release task log handlers after each task to avoid accumulating files."""
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)


def get_args():
    parser = argparse.ArgumentParser(description="WebRetriever Competition - Agent Evaluation Runner")

    # 1. 输入输出
    parser.add_argument("--input", type=str, required=True, help="任务 JSON 文件路径")
    parser.add_argument("--output", type=str, required=True, help="结果输出目录（轨迹 + 日志）")

    # 2. 浏览器连接
    parser.add_argument("--cdp_url", type=str, nargs="+", required=True, help="浏览器 CDP URL 列表（数量决定 worker 数）")

    # 3. 模型配置：api_* 用于视觉工具；text_api_* 用于主任务 Agent。
    # 保留旧参数，供单模型本地调试时覆盖文本模型。
    parser.add_argument("--model", type=str, default=None, help="文本任务模型名称（兼容旧参数）")
    parser.add_argument("--api_base", type=str, default=None, help="文本任务 API base URL（兼容旧参数）")
    parser.add_argument("--api_key", type=str, default=None, help="文本任务 API key（兼容旧参数）")
    parser.add_argument("--text_api_model", type=str, default=None, help="文本任务模型名称")
    parser.add_argument("--text_api_base", type=str, default=None, help="文本任务 OpenAI 兼容 API base URL")
    parser.add_argument("--text_api_key", type=str, default=None, help="文本任务 OpenAI 兼容 API key")
    parser.add_argument("--temperature", type=float, default=None, help="采样温度")
    parser.add_argument("--top_p", type=float, default=None, help="Top-p 采样")
    parser.add_argument("--max_tokens", type=int, default=None, help="最大生成 token 数")

    args = parser.parse_args()
    args.config = load_config(args)
    return args


def load_config(args) -> dict[str, Any]:
    config_path = Path(__file__).resolve().parents[2] / "config.json"
    config = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as file:
            config = json.load(file)

    # 命令行显式传入时覆盖 config.json，便于本地临时测试。
    # 没有 text_api_* 的旧配置继续保持单模型行为；双模型配置下，旧参数仅覆盖文本 Agent。
    has_text_model_config = any(
        config.get(key)
        for key in ("text_api_model", "text_api_base", "text_api_key")
    )
    if args.model:
        config["text_api_model"] = args.model
        if not has_text_model_config:
            config["api_model"] = args.model
    if args.api_base:
        config["text_api_base"] = args.api_base
        if not has_text_model_config:
            config["api_base"] = args.api_base
    if args.api_key:
        config["text_api_key"] = args.api_key
        if not has_text_model_config:
            config["api_key"] = args.api_key
    if args.text_api_model:
        config["text_api_model"] = args.text_api_model
    if args.text_api_base:
        config["text_api_base"] = args.text_api_base
    if args.text_api_key:
        config["text_api_key"] = args.text_api_key
    if args.temperature is not None:
        config["temperature"] = args.temperature
    if args.top_p is not None:
        config["top_p"] = args.top_p
    if args.max_tokens is not None:
        config["max_tokens"] = args.max_tokens

    if not config.get("api_base"):
        raise ValueError("必须在 config.json 或命令行中配置 api_base")
    if not config.get("api_model"):
        raise ValueError("必须在 config.json 或命令行中配置 api_model")
    return config


def normalize_website(website: Any) -> str:
    value = str(website or "").strip()
    if value and not value.startswith("http"):
        value = "https://" + value
    return value


async def prepare_browser_window(page: Any, logger: logging.Logger) -> None:
    try:
        await page.bring_to_front()
        cdp = await page.context.new_cdp_session(page)
        window = await cdp.send("Browser.getWindowForTarget")
        await cdp.send(
            "Browser.setWindowBounds",
            {"windowId": window["windowId"], "bounds": {"windowState": "maximized"}},
        )
        await asyncio.sleep(0.5)
    except Exception as exc:
        logger.warning("浏览器窗口最大化失败: %s", exc)


async def run_one_async(
    worker_id: int,
    json_item_i: int,
    json_item: dict[str, Any],
    base_dir: Path,
    log_file: Path,
    cdp_url: str,
    config: dict[str, Any],
    logger: logging.Logger,
    timeline_logger: logging.Logger,
    mcp_client: Any | None,
    browser_session: web_controller.PlaywrightSession,
    request_collector: web_controller.RequestCollector,
    deadline_at: float | None = None,
    progress_callback=None,
    prepared_page: Any = None,
):
    task_id = json_item.get("task_id", str(json_item.get("task_index", json_item.get("task_idx"))))
    task_idx = json_item.get("task_index", json_item.get("task_idx"))
    task = json_item["task"]
    website = normalize_website(json_item.get("website", ""))
    started_at = time.perf_counter()
    timeline_logger.info("task_execution_started")

    try:
        if browser_session is None or browser_session.browser is None:
            raise RuntimeError("Worker browser session is unavailable")

        # Worker 级复用 Context；每题只清空本题的请求缓冲区。
        request_collector.clear()
        page = await web_controller.open_page_async(
            browser_session.p,
            browser_session.browser,
            browser_session.context,
            cdp_url,
            website,
            before_goto=lambda task_page: prepare_browser_window(task_page, logger),
            logger=logger,
            phase_callback=progress_callback,
            lifecycle_state=getattr(browser_session, "lifecycle_state", None),
            prepared_page=prepared_page,
        )
        browser_session.page = page
        logger.info("页面打开成功: %s", page.url)
        timeline_logger.info("browser_page_ready")

        if mcp_client is None:
            if progress_callback is not None:
                progress_callback("mcp_connect")
            mcp_client = await create_worker_mcp_client(worker_id, cdp_url, timeline_logger)

        if progress_callback is not None:
            progress_callback("agent_execution")
        result = await run_agentscope_task(
            worker_id=worker_id,
            task_idx=task_idx,
            task_id=task_id,
            task=task,
            website=website,
            page=page,
            cdp_url=cdp_url,
            mcp_client=mcp_client,
            task_output_dir=base_dir,
            log_file=log_file,
            config=config,
            logger=logger,
            timeline_logger=timeline_logger,
            deadline_at=deadline_at,
            progress_callback=progress_callback,
        )

        timeline_logger.info(
            "task_execution_finished status=%s duration_ms=%s",
            result.status,
            elapsed_ms(started_at),
        )
        return result, mcp_client, None
    except Exception as exc:
        logger.error("任务初始化或执行失败: %s", exc, exc_info=True)
        log_safe_exception(timeline_logger, "task_execution_finished", exc, started_at)
        write_bootstrap_failure_result(base_dir, json_item, website, str(exc))
        return None, mcp_client, exc
    finally:
        save_result_path = base_dir / "capture.json"
        try:
            request_count, _ = await request_collector.save_results_async(str(save_result_path))
            logger.info(
                "保存了 %s 条浏览器请求记录到 %s",
                request_count,
                save_result_path,
            )
        except Exception as exc:
            logger.error("保存 capture.json 失败: %s", exc)
            safe_write_json(
                str(save_result_path),
                {
                    "capture_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "total_requests": 0,
                    "all_requests": [],
                },
            )
        # Playwright 客户端由 Worker 统一关闭，不能在任务结束时断开。


def write_bootstrap_failure_result(
    task_output_dir: Path,
    json_item: dict[str, Any],
    website: str,
    error: str,
) -> None:
    """Keep the official result.json shape even when AgentScope never starts."""
    task_output_dir.mkdir(parents=True, exist_ok=True)
    (task_output_dir / "trajectory").mkdir(exist_ok=True)
    (task_output_dir / "trajectory_visual").mkdir(exist_ok=True)
    task_idx = json_item.get("task_index", json_item.get("task_idx"))
    task_id = json_item.get("task_id", str(task_idx))
    message = f"Task initialization failed: {error}"
    safe_write_json(
        str(task_output_dir / "result.json"),
        {
            "task_idx": task_idx,
            "task_id": task_id,
            "task": json_item.get("task", ""),
            "website": website,
            "status": "FAIL",
            "reference_length": 100,
            "predict_length": 0,
            "agent_answer": message,
            "final_result_response": message,
            "actions": [],
            "thoughts": [],
            "history_resps": [],
            "urls": [],
        },
    )
    # 即使 Agent 尚未启动，也保持官方要求的 capture.json 输出结构。
    safe_write_json(
        str(task_output_dir / "capture.json"),
        {
            "capture_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_requests": 0,
            "all_requests": [],
        },
    )


def ensure_failure_artifacts(
    task_output_dir: Path,
    json_item: dict[str, Any],
    website: str,
    error: str,
) -> None:
    """Complete the official artifact set without deleting a partial failure trace."""
    task_output_dir.mkdir(parents=True, exist_ok=True)
    (task_output_dir / "trajectory").mkdir(exist_ok=True)
    (task_output_dir / "trajectory_visual").mkdir(exist_ok=True)
    if not (task_output_dir / "result.json").exists():
        write_bootstrap_failure_result(task_output_dir, json_item, website, error)
    if not (task_output_dir / "capture.json").exists():
        safe_write_json(
            str(task_output_dir / "capture.json"),
            {"capture_time": time.strftime("%Y-%m-%d %H:%M:%S"), "total_requests": 0, "all_requests": []},
        )


def get_task_output_dir(base_dir: Path, json_item: dict[str, Any]) -> Path:
    task_idx = json_item.get("task_index", json_item.get("task_idx"))
    task_id = json_item.get("task_id", str(task_idx))
    return base_dir / f"{task_idx}_{task_id}"


def has_success_output(output_dir: Path) -> bool:
    result_path = output_dir / "result.json"
    if not result_path.exists():
        return False
    try:
        with result_path.open("r", encoding="utf-8") as result_file:
            return json.load(result_file).get("status") == "SUCCESS"
    except (OSError, ValueError, TypeError):
        return False


def mark_task_completed(
    task_index: int,
    worker_id: int | str,
    processing_tasks,
    inflight_tasks,
    completed_tasks,
    task_states=None,
    state_lock=None,
) -> None:
    if task_states is not None:
        def complete_state() -> None:
            state = dict(task_states.get(task_index, {}))
            state.update({"status": "completed", "completed_by": worker_id, "completed_at": time.time()})
            task_states[task_index] = state

        if state_lock is None:
            complete_state()
        else:
            with state_lock:
                complete_state()
    processing_tasks.pop(task_index, None)
    inflight_tasks.pop(task_index, None)
    completed_tasks[task_index] = worker_id


def claim_next_task(
    task_states,
    state_lock,
    worker_id: int,
    task_deadlines,
    processing_tasks,
    inflight_tasks,
) -> int | None:
    """Atomically reserve one task without holding the lock during execution.

    The task registry is the source of truth. A single manager-dict assignment
    changes a task from pending to claimed before any browser/model work starts;
    the parent can therefore recover a claimed task even if the Worker exits
    between the claim and the later bookkeeping writes.
    """
    now = time.time()
    deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    with state_lock:
        pending_indices = sorted(
            (
                int(task_index)
                for task_index, state in task_states.items()
                if dict(state).get("status") == "pending"
            ),
        )
        if not pending_indices:
            return None

        task_index = pending_indices[0]
        pending_state = dict(task_states.get(task_index, {}))
        claim = {
            "status": "claimed",
            "worker_id": worker_id,
            "json_item_i": task_index,
            "attempt": 1,
            "started_at": now,
            "phase": "task_claimed",
            "phase_started_at": now,
            "heartbeat_at": now,
            "task_idx": pending_state.get("task_idx", "-"),
            "task_id": pending_state.get("task_id", "-"),
        }
        # This assignment is the atomic scheduler decision. No browser or
        # network operation is performed while the short scheduler lock is held.
        task_states[task_index] = claim
        task_deadlines[task_index] = deadline
        processing_tasks[task_index] = worker_id
        inflight_tasks[task_index] = claim
        return task_index


def has_pending_task(task_states, state_lock) -> bool:
    """Check scheduler state without touching browser or network resources."""
    with state_lock:
        return any(dict(state).get("status") == "pending" for state in task_states.values())


async def reset_worker_session(
    mcp_client,
    browser_session,
    request_collector,
    timeline_logger,
):
    """Drop both clients after an infrastructure error; neither may be reused."""
    await close_worker_mcp_client(mcp_client, timeline_logger)
    await disconnect_worker_browser_session(browser_session, timeline_logger)
    request_collector = web_controller.RequestCollector()
    return None, None, request_collector


def finalize_unclaimed_task(
    json_item_i: int,
    worker_id: int | str,
    scribe_json_items: list[dict[str, Any]],
    base_dir: Path,
    processing_tasks,
    inflight_tasks,
    completed_tasks,
    task_states,
    state_lock,
    error: str,
) -> None:
    json_item = scribe_json_items[json_item_i]
    output_dir = get_task_output_dir(base_dir, json_item)
    safe_remove_task_dir(str(output_dir))
    safe_create_directory(str(output_dir), result_file="result.json")
    write_bootstrap_failure_result(
        output_dir,
        json_item,
        normalize_website(json_item.get("website", "")),
        error,
    )
    mark_task_completed(
        json_item_i,
        worker_id,
        processing_tasks,
        inflight_tasks,
        completed_tasks,
        task_states,
        state_lock,
    )


async def connect_worker_browser_session(worker_id, cdp_url, timeline_logger):
    """Create one browser SDK session for the whole Worker."""
    timeline_logger.info("browser_connection_started")
    try:
        p, browser, context = await web_controller.init_playwright_context_async(
            cdp_url,
            raise_on_failure=True,
        )
    except Exception as exc:
        log_safe_exception(timeline_logger, "browser_connection_finished", exc)
        raise
    if browser is None or context is None:
        error = BrowserLifecycleError("Worker browser session initialization failed")
        log_safe_exception(timeline_logger, "browser_connection_finished", error)
        raise error
    timeline_logger.info("browser_connection_finished status=success")
    session = web_controller.PlaywrightSession(p=p, browser=browser, context=context)
    session.lifecycle_state = web_controller.install_lifecycle_observers(
        browser,
        context,
        timeline_logger,
    )
    return session


async def verify_worker_runtime_health(browser_session, mcp_client, timeline_logger) -> None:
    """Require SDK liveness and one real MCP browser operation before claiming work."""
    try:
        page = await web_controller.verify_browser_context_health_async(browser_session.context)
    except Exception as exc:
        log_safe_exception(timeline_logger, "browser_sdk_healthcheck", exc)
        raise BrowserLifecycleError("Browser SDK health check failed") from exc
    browser_session.page = page
    timeline_logger.info("browser_sdk_healthcheck status=success")

    try:
        browser_tabs = await mcp_client.get_tool("browser_tabs")
        async with asyncio.timeout(MCP_HEALTHCHECK_TIMEOUT_SECONDS):
            probe = await browser_tabs.call(action="list")
    except Exception as exc:
        log_safe_exception(timeline_logger, "mcp_browser_healthcheck", exc)
        raise MCPConnectionError("MCP browser health check failed") from exc

    state = str(getattr(probe, "state", "")).lower()
    if state.endswith("error"):
        raise MCPConnectionError("MCP browser health check returned an error")
    timeline_logger.info("mcp_browser_healthcheck status=success")


async def disconnect_worker_browser_session(browser_session, timeline_logger):
    """Disconnect only the local Playwright client; never close the sandbox browser."""
    if browser_session is None or browser_session.p is None:
        return
    try:
        async with asyncio.timeout(BROWSER_DISCONNECT_TIMEOUT_SECONDS):
            await browser_session.p.stop()
        timeline_logger.info("browser_connection_closed status=success")
    except Exception as exc:
        log_safe_exception(timeline_logger, "browser_connection_closed", exc)


def run_worker(
    worker_id: int,
    task_states,
    log_dir: str,
    cdp_urls: List[str],
    scribe_json_items: List[dict[str, Any]],
    base_dir: str,
    config: dict[str, Any],
    total_pending: int,
    processing_tasks,
    completed_tasks,
    inflight_tasks,
    task_deadlines,
    state_lock,
    app_log_queue,
    worker_states,
):
    # Windows multiprocessing 使用 spawn，worker 函数必须放在模块顶层才能被子进程导入。
    asyncio.run(
        run_worker_async(
            worker_id,
            task_states,
            log_dir,
            cdp_urls,
            scribe_json_items,
            base_dir,
            config,
            total_pending,
            processing_tasks,
            completed_tasks,
            inflight_tasks,
            task_deadlines,
            state_lock,
            app_log_queue,
            worker_states,
        ),
    )


async def run_worker_async(
    worker_id: int,
    task_states,
    log_dir: str,
    cdp_urls: List[str],
    scribe_json_items: List[dict[str, Any]],
    base_dir: str,
    config: dict[str, Any],
    total_pending: int,
    processing_tasks,
    completed_tasks,
    inflight_tasks,
    task_deadlines,
    state_lock,
    app_log_queue,
    worker_states,
):
    """Process atomically claimed tasks with recoverable Worker-owned resources."""
    base_dir_path = Path(base_dir)
    cdp_url = cdp_urls[worker_id]
    print(f"[W{worker_id}] 使用浏览器: {cdp_url[:80]}...")
    timeline_logger = build_worker_timeline_logger(worker_id, app_log_queue)
    timeline_logger.info("worker_started")
    mcp_client = None
    browser_session = None
    request_collector = web_controller.RequestCollector()
    last_logger: logging.Logger | None = None
    heartbeat_task = asyncio.create_task(worker_heartbeat_loop(worker_id, worker_states))
    update_worker_state(worker_states, worker_id, "idle")

    async def ensure_worker_connections() -> None:
        """Prepare a healthy browser session before claiming work."""
        nonlocal browser_session, mcp_client, request_collector
        last_error = None
        for attempt in range(1, PRE_TASK_SESSION_REBUILD_ATTEMPTS + 1):
            try:
                if browser_session is None:
                    update_worker_state(worker_states, worker_id, "browser_session_connect")
                    browser_session = await connect_worker_browser_session(
                        worker_id,
                        cdp_url,
                        timeline_logger,
                    )
                    request_collector = web_controller.RequestCollector()
                    request_collector.attach_context_async(browser_session.context)
                if mcp_client is None:
                    update_worker_state(worker_states, worker_id, "mcp_connect")
                    mcp_client = await create_worker_mcp_client(
                        worker_id,
                        cdp_url,
                        timeline_logger,
                    )
                await verify_worker_runtime_health(browser_session, mcp_client, timeline_logger)

                timeline_logger.info("worker_preflight_ready status=success")
                return
            except Exception as exc:
                last_error = exc
                log_safe_exception(timeline_logger, "worker_preflight_failed", exc)
                if attempt >= PRE_TASK_SESSION_REBUILD_ATTEMPTS:
                    raise

                # No task has been claimed. Drop only local clients and retry
                # preflight; the remote browser is never closed here.
                update_worker_state(worker_states, worker_id, "session_reset")
                mcp_client, browser_session, request_collector = await reset_worker_session(
                    mcp_client,
                    browser_session,
                    request_collector,
                    timeline_logger,
                )
                await asyncio.sleep(PRE_TASK_SESSION_REBUILD_BACKOFF_SECONDS[attempt - 1])
        if last_error is not None:
            raise last_error

    try:
        while len(completed_tasks) < total_pending:
            if not has_pending_task(task_states, state_lock):
                await asyncio.sleep(0.2)
                continue
            # A task is only claimed after the Worker has a usable browser and
            # MCP connection. Connection failures here affect no claimed task.
            await ensure_worker_connections()
            json_item_i = claim_next_task(
                task_states,
                state_lock,
                worker_id,
                task_deadlines,
                processing_tasks,
                inflight_tasks,
            )
            if json_item_i is None:
                await asyncio.sleep(0.2)
                continue
            task_id = None
            json_item = None
            logger = None
            task_timeline = None
            task_output_dir = None
            task_claimed = False
            report_phase = None
            try:
                json_item = scribe_json_items[json_item_i]
                task_claimed = True
                task_id = json_item.get("task_id", str(json_item.get("task_index", json_item.get("task_idx"))))
                task_idx = json_item.get("task_index", json_item.get("task_idx"))
                task = json_item["task"]
                task_timeline = task_timeline_logger(timeline_logger, task_idx, task_id)
                task_started_at = time.perf_counter()
                task_timeline.info("task_claimed")

                def report_phase(phase: str) -> None:
                    """Publish lifecycle progress to both supervisor views."""
                    update_worker_state(worker_states, worker_id, phase)
                    now = time.time()
                    with state_lock:
                        info = dict(inflight_tasks.get(json_item_i, {}))
                        if not info:
                            return
                        if info.get("phase") != phase:
                            info["phase_started_at"] = now
                        info["phase"] = phase
                        info["heartbeat_at"] = now
                        inflight_tasks[json_item_i] = info
                        task_state = dict(task_states.get(json_item_i, {}))
                        if task_state.get("status") == "claimed":
                            task_state.update(
                                {
                                    "phase": phase,
                                    "phase_started_at": info["phase_started_at"],
                                    "heartbeat_at": now,
                                },
                            )
                            task_states[json_item_i] = task_state

                report_phase("task_claimed")

                # 同一 worker 会顺序处理多个网站任务，日志必须按任务而不是 worker 分文件。
                started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
                task_log_file = Path(log_dir) / f"task_{task_idx}_{task_id}_{started_at}.log"
                logger = setup_logger(task_log_file, worker_id)
                last_logger = logger
                logger.info("任务日志: %s", task_log_file)
                task_timeline.info("task_received")

                logger.info("开始处理任务 %s/%s：%s...", json_item_i, task_id, task[:50])

                task_output_dir = get_task_output_dir(base_dir_path, json_item)
                if not safe_create_directory(str(task_output_dir), result_file="result.json"):
                    if has_success_output(task_output_dir):
                        logger.info("任务已有成功结果，确认完成 %s/%s", json_item_i, task_id)
                        mark_task_completed(
                            json_item_i,
                            worker_id,
                            processing_tasks,
                            inflight_tasks,
                            completed_tasks,
                            task_states,
                            state_lock,
                        )
                        continue
                    raise RuntimeError("task output directory is locked or cannot be prepared")

                if browser_session is None or mcp_client is None:
                    raise BrowserLifecycleError("Worker preflight connections are unavailable")

                remaining_seconds = max(0.1, float(task_deadlines[json_item_i]) - time.monotonic())

                try:
                    async with asyncio.timeout(remaining_seconds):
                        result, mcp_client, run_error = await run_one_async(
                            worker_id=worker_id,
                            json_item_i=json_item_i,
                            json_item=json_item,
                            base_dir=task_output_dir,
                            log_file=task_log_file,
                            cdp_url=cdp_url,
                            config=config,
                            logger=logger,
                            timeline_logger=task_timeline or timeline_logger,
                            mcp_client=mcp_client,
                            browser_session=browser_session,
                            request_collector=request_collector,
                            deadline_at=float(task_deadlines[json_item_i]),
                            progress_callback=report_phase,
                            prepared_page=browser_session.page,
                        )
                except TimeoutError as timeout_error:
                    raise TaskDeadlineExceededError(
                        f"task exceeded the {TASK_TIMEOUT_SECONDS} second deadline"
                    ) from timeout_error

                if run_error is not None and result is None:
                    logger.error(
                        "任务未成功完成，将保留失败结果: %s",
                        run_error.__class__.__name__,
                    )

                if result is not None and result.retryable:
                    logger.error(
                        "基础设施异常导致当前任务最终失败，不重新入队: %s",
                        result.failure_kind or "InfrastructureError",
                    )

                quarantine_current_worker = (
                    result is not None
                    and result.failure_kind == MCPBrowserUnhealthyError.__name__
                )

                # A bootstrap failure can return result=None before the Agent
                # is created. Reset the broken session for the next task, but
                # never replay the task that has already been claimed.
                # A navigation/network failure ends only this claimed task.
                # The next task's preflight will decide whether the existing
                # Worker session is still healthy; do not reset it eagerly.
                should_reset_session = (
                    result is None
                    and not isinstance(run_error, PageNavigationError)
                ) or result_requires_session_reset(result)
                if should_reset_session:
                    logger.warning("检测到基础设施异常，将在下一题前重建浏览器和 MCP 会话")
                    report_phase("session_reset")
                    mcp_client, browser_session, request_collector = await reset_worker_session(
                        mcp_client,
                        browser_session,
                        request_collector,
                        task_timeline,
                    )

                # 只有成功返回或达到最终失败状态后，当前任务才标记完成。
                mark_task_completed(
                    json_item_i,
                    worker_id,
                    processing_tasks,
                    inflight_tasks,
                    completed_tasks,
                    task_states,
                    state_lock,
                )
                task_deadlines.pop(json_item_i, None)
                task_timeline.info(
                    "task_finished status=%s failure_kind=%s duration_ms=%s",
                    result.status if result is not None else "FAILURE",
                    (result.failure_kind if result is not None else run_error.__class__.__name__ if run_error else "unknown"),
                    elapsed_ms(task_started_at),
                )
                log_task_artifacts(task_timeline, task_output_dir)
                if quarantine_current_worker:
                    timeline_logger.warning(
                        "worker_quarantined reason=%s; remaining tasks stay pending",
                        result.failure_kind or MCPBrowserUnhealthyError.__name__,
                    )
                    quarantine_worker_state(
                        worker_states,
                        worker_id,
                        result.failure_kind or MCPBrowserUnhealthyError.__name__,
                    )
                else:
                    update_worker_state(worker_states, worker_id, "idle")
                logger.info("###总任务数/当前任务：%s/%s 已完成!!!", len(scribe_json_items), json_item_i + 1)
                if quarantine_current_worker:
                    break
            except Exception as e:
                if logger is not None:
                    logger.error("处理任务 %s 时发生异常: %s", json_item_i, str(e), exc_info=True)
                else:
                    print(f"处理任务 {json_item_i} 时发生异常: {e}")
                log_safe_exception(timeline_logger, "worker_task_loop", e)
                if isinstance(e, TaskDeadlineExceededError):
                    report_phase("session_reset")
                    mcp_client, browser_session, request_collector = await reset_worker_session(
                        mcp_client,
                        browser_session,
                        request_collector,
                        task_timeline or timeline_logger,
                    )
                if json_item is not None:
                    if task_output_dir is None:
                        task_output_dir = get_task_output_dir(base_dir_path, json_item)
                        safe_create_directory(str(task_output_dir), result_file="result.json")
                    ensure_failure_artifacts(
                        task_output_dir,
                        json_item,
                        normalize_website(json_item.get("website", "")),
                        str(e),
                    )
                    mark_task_completed(
                        json_item_i,
                        worker_id,
                        processing_tasks,
                        inflight_tasks,
                        completed_tasks,
                        task_states,
                        state_lock,
                    )
                    (task_timeline or timeline_logger).info(
                        "task_finished status=FAILURE failure_kind=%s duration_ms=%s",
                        e.__class__.__name__,
                        elapsed_ms(task_started_at) if task_timeline is not None else -1,
                    )
                    if task_timeline is not None:
                        log_task_artifacts(task_timeline, task_output_dir)
                    task_deadlines.pop(json_item_i, None)
                    if isinstance(e, MCPBrowserUnhealthyError):
                        timeline_logger.warning(
                            "worker_quarantined reason=%s; remaining tasks stay pending",
                            e.__class__.__name__,
                        )
                        quarantine_worker_state(
                            worker_states,
                            worker_id,
                            e.__class__.__name__,
                        )
                    else:
                        update_worker_state(worker_states, worker_id, "idle")
                    if isinstance(e, MCPBrowserUnhealthyError):
                        break
            finally:
                if task_claimed and browser_session is not None:
                    cleanup_logger = task_timeline or timeline_logger
                    try:
                        update_worker_state(worker_states, worker_id, "page_cleanup")
                        browser_session.page = await web_controller.cleanup_context_after_task_async(
                            browser_session.context,
                            keep_page=browser_session.page,
                            logger=cleanup_logger,
                            phase_callback=report_phase if callable(report_phase) else None,
                        )
                        timeline_logger.info("worker_post_task_cleanup status=success")
                        update_worker_state(worker_states, worker_id, "idle")
                    except Exception as cleanup_error:
                        log_safe_exception(
                            cleanup_logger,
                            "worker_post_task_cleanup_failed",
                            cleanup_error,
                        )
                        update_worker_state(worker_states, worker_id, "session_reset")
                        try:
                            mcp_client, browser_session, request_collector = await reset_worker_session(
                                mcp_client,
                                browser_session,
                                request_collector,
                                cleanup_logger,
                            )
                        except Exception as reset_error:
                            log_safe_exception(
                                timeline_logger,
                                "worker_post_task_session_reset_failed",
                                reset_error,
                            )
                if logger is not None:
                    close_logger(logger)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        update_worker_state(worker_states, worker_id, "stopped")
        await close_worker_mcp_client(mcp_client, timeline_logger)
        await disconnect_worker_browser_session(browser_session, timeline_logger)
        timeline_logger.info("worker_finished")


def load_task_items(input_path: str) -> List[dict[str, Any]]:
    """Load the official JSON array format and Java runner-compatible JSONL."""
    task_path = Path(input_path)
    text = task_path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []

    if text.startswith("["):
        items = json.loads(text)
        if not isinstance(items, list):
            raise ValueError("任务 JSON 顶层必须是数组")
        return [_normalize_task_item(item, index) for index, item in enumerate(items)]

    items = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        items.append(_normalize_task_item(item, line_number - 1, line_number))
    return items


def _normalize_task_item(
    item: Any,
    fallback_index: int,
    line_number: int | None = None,
) -> dict[str, Any]:
    if not isinstance(item, dict):
        location = f"JSONL 第 {line_number} 行" if line_number is not None else "JSON 数组元素"
        raise ValueError(f"任务 {location} 必须是对象")

    # 官方字段优先；同时兼容 Java runner 支持的 id/question/url 简写任务格式。
    normalized = dict(item)
    normalized.setdefault("task_index", normalized.get("task_idx", fallback_index))
    normalized.setdefault("task", normalized.get("question", ""))
    normalized.setdefault("website", normalized.get("url", ""))
    if not normalized.get("task_id"):
        normalized["task_id"] = normalized.get("id") or _stable_task_id(
            str(normalized["task"]),
            str(normalized["website"]),
        )
    return normalized


def _stable_task_id(task: str, website: str) -> str:
    # 与 Java runner 的 stableTaskId 保持一致：SHA-256 前 4 个字节，便于断点续跑。
    return hashlib.sha256(f"{website}\n{task}".encode("utf-8")).hexdigest()[:8]


if __name__ == "__main__":
    args = get_args()

    BASE_DIR = Path(args.output)
    LOG_DIR = BASE_DIR / "logs"
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    CDP_URLS = args.cdp_url
    WORKERS = len(CDP_URLS)

    print(f"加载任务: {args.input}")
    scribe_json_items = load_task_items(args.input)
    print(f"共 {len(scribe_json_items)} 条任务")

    # 构建已成功任务映射，支持断点续跑。
    success_task_map = {}
    if BASE_DIR.is_dir():
        for fname in os.listdir(BASE_DIR):
            rp = BASE_DIR / fname / "result.json"
            if rp.exists():
                try:
                    with rp.open("r", encoding="utf-8") as rf:
                        rj = json.load(rf)
                    if rj.get("status") == "SUCCESS":
                        tidx = fname.split("_")[0]
                        success_task_map[tidx] = True
                except Exception:
                    pass
        if success_task_map:
            print(f"已成功任务数: {len(success_task_map)}")

    N = len(scribe_json_items)
    indices = []
    skip_cnt = 0
    for i in range(N):
        json_item = scribe_json_items[i]
        task_id = json_item.get("task_id", str(json_item.get("task_index", json_item.get("task_idx"))))
        task_idx = json_item.get("task_index", json_item.get("task_idx"))

        if str(task_idx) in success_task_map:
            skip_cnt += 1
            continue
        idx_task_id = f"{task_idx}_{task_id}"
        task_dir = BASE_DIR / idx_task_id
        result_json_path = task_dir / "result.json"
        if result_json_path.exists():
            with result_json_path.open("r", encoding="utf-8") as f:
                result_json_item = json.load(f)
            actions = result_json_item.get("actions", [])
            if len(actions) > 0 and result_json_item.get("status") == "SUCCESS":
                skip_cnt += 1
                continue
            safe_remove_task_dir(str(task_dir))
        else:
            safe_remove_task_dir(str(task_dir))
        indices.append(i)

    print(f"总任务数: {N}, 跳过: {skip_cnt}, 待处理: {len(indices)}")
    if not indices:
        print("所有任务已完成！")
        raise SystemExit(0)
    print(f"从任务索引 {indices[0]} 开始测试")

    manager = mp.Manager()
    # The registry is the single scheduler source of truth. Workers claim a
    # pending entry under state_lock, so task assignment and claim bookkeeping
    # cannot get out of sync if a process exits at the claim boundary.
    task_states = manager.dict(
        {
            task_index: {
                "status": "pending",
                "task_idx": scribe_json_items[task_index].get(
                    "task_index",
                    scribe_json_items[task_index].get("task_idx"),
                ),
                "task_id": scribe_json_items[task_index].get(
                    "task_id",
                    str(
                        scribe_json_items[task_index].get(
                            "task_index",
                            scribe_json_items[task_index].get("task_idx"),
                        ),
                    ),
                ),
            }
            for task_index in indices
        },
    )
    processing_tasks = manager.dict()
    completed_tasks = manager.dict()
    inflight_tasks = manager.dict()
    task_deadlines = manager.dict()
    worker_states = manager.dict()
    state_lock = manager.Lock()
    app_log_queue, app_log_listener, app_log_file = start_app_log_listener(Path(__file__).resolve().parents[2])
    parent_timeline_logger = build_worker_timeline_logger("parent", app_log_queue)
    print(f"应用运行日志: {app_log_file}")

    print(f"启动 {WORKERS} 个进程")
    for wid in range(WORKERS):
        print(f"  Worker {wid}: 共享任务状态表")
    procs: dict[int, mp.Process] = {}
    worker_restart_counts = {wid: 0 for wid in range(WORKERS)}
    parent_timeline_logger.info(
        "run_started total_tasks=%s pending_tasks=%s configured_workers=%s",
        len(scribe_json_items),
        len(indices),
        WORKERS,
    )

    def start_worker(wid: int) -> None:
        # Do not let a restarted process inherit the previous process' stale
        # phase/heartbeat while it is still being spawned.
        now = time.time()
        worker_states[wid] = {
            "phase": "worker_starting",
            "phase_started_at": now,
            "heartbeat_at": now,
            "progress_at": now,
            "restart_count": worker_restart_counts[wid],
        }
        proc = mp.Process(
            target=run_worker,
            args=(
                wid,
                task_states,
                str(LOG_DIR),
                CDP_URLS,
                scribe_json_items,
                str(BASE_DIR),
                args.config,
                len(indices),
                processing_tasks,
                completed_tasks,
                inflight_tasks,
                task_deadlines,
                state_lock,
                app_log_queue,
                worker_states,
            ),
        )
        proc.start()
        procs[wid] = proc
        print(f"Worker {wid} 已启动")

    for wid in range(WORKERS):
        start_worker(wid)

    print("\n开始监控进程状态...")
    last_scheduler_snapshot = 0.0
    abnormal_worker_exits = 0
    try:
        while len(completed_tasks) < len(indices):
            time.sleep(5)
            now_monotonic = time.monotonic()
            if now_monotonic - last_scheduler_snapshot >= SCHEDULER_SNAPSHOT_INTERVAL_SECONDS:
                log_scheduler_snapshot(
                    parent_timeline_logger,
                    worker_states,
                    task_states,
                    procs,
                    completed_tasks,
                    len(indices),
                )
                last_scheduler_snapshot = now_monotonic
            alive_count = sum(p.is_alive() for p in procs.values())
            print(
                f"[状态] 活跃: {alive_count}, 完成: {len(completed_tasks)}/{len(indices)}, "
                f"处理中: {len(processing_tasks)}"
            )

            for wid, proc in list(procs.items()):
                if proc.is_alive():
                    task_info = next(
                        (
                            dict(info)
                            for info in inflight_tasks.values()
                            if info.get("worker_id") == wid
                        ),
                        None,
                    )
                    stale_reason = get_stale_worker_reason(
                        dict(worker_states.get(wid, {})),
                        task_info=task_info,
                    )
                    if stale_reason is not None:
                        state = dict(worker_states.get(wid, {}))
                        parent_timeline_logger.warning(
                            "worker_stale_detected phase=%s reason=%s; terminating",
                            state.get("phase", "unknown"),
                            stale_reason,
                        )
                        print(f"Worker {wid} 无响应（{stale_reason}），终止并回收任务")
                        proc.terminate()
                        proc.join(timeout=5)
                        if proc.is_alive() and hasattr(proc, "kill"):
                            proc.kill()
                            proc.join(timeout=1)
                        # 下一轮按既有 Worker 退出恢复流程回收 inflight_tasks。
                        continue
                if proc.is_alive() or proc.exitcode is None:
                    continue

                proc.join(timeout=0.1)
                worker_state = dict(worker_states.get(wid, {}))
                worker_exit_kind = (
                    "quarantined"
                    if worker_state.get("quarantined")
                    else "normal"
                    if proc.exitcode == 0
                    else "abnormal"
                )
                if worker_exit_kind == "abnormal":
                    abnormal_worker_exits += 1
                parent_timeline_logger.log(
                    logging.WARNING if worker_exit_kind == "abnormal" else logging.INFO,
                    "worker_process_exit worker_id=%s pid=%s exitcode=%s kind=%s "
                    "last_phase=%s restart_count=%s",
                    wid,
                    proc.pid,
                    proc.exitcode,
                    worker_exit_kind,
                    worker_state.get("phase", "unknown"),
                    worker_state.get("restart_count", 0),
                )
                recovered_indices = set()
                with state_lock:
                    for task_index, info in list(inflight_tasks.items()):
                        if info.get("worker_id") != wid:
                            continue
                        recovered_indices.add(int(task_index))
                        processing_tasks.pop(task_index, None)
                        inflight_tasks.pop(task_index, None)

                    # The task registry also covers the tiny window in which
                    # a Worker may exit after the atomic claim but before the
                    # auxiliary inflight map is updated.
                    for task_index, state in list(task_states.items()):
                        state = dict(state)
                        if (
                            state.get("worker_id") == wid
                            and state.get("status") in {"claimed", "completed"}
                            and int(task_index) not in completed_tasks
                        ):
                            recovered_indices.add(int(task_index))
                            processing_tasks.pop(task_index, None)
                            inflight_tasks.pop(task_index, None)

                for task_index in recovered_indices:
                    item = scribe_json_items[task_index]
                    output_dir = get_task_output_dir(BASE_DIR, item)
                    if has_success_output(output_dir):
                        # A hard exit can occur after result.json is written but
                        # before capture.json is flushed. Complete the required
                        # artifact set without replaying the task.
                        ensure_failure_artifacts(
                            output_dir,
                            item,
                            normalize_website(item.get("website", "")),
                            "Worker exited after writing the task result",
                        )
                        mark_task_completed(
                            task_index,
                            f"recovered-worker-{wid}",
                            processing_tasks,
                            inflight_tasks,
                            completed_tasks,
                            task_states,
                            state_lock,
                        )
                        task_state = dict(task_states.get(task_index, {}))
                        recovered_task_logger = task_timeline_logger(
                            parent_timeline_logger,
                            task_state.get("task_idx", item.get("task_idx", "-")),
                            task_state.get("task_id", item.get("task_id", "-")),
                        )
                        recovered_task_logger.info(
                            "task_recovered status=SUCCESS recovery=worker_exit artifact_repair=true"
                        )
                        log_task_artifacts(recovered_task_logger, output_dir)
                    else:
                        # The task was already claimed by the crashed Worker.
                        # Recover only its final artifacts; never replay it.
                        finalize_unclaimed_task(
                            task_index,
                            f"recovered-worker-{wid}",
                            scribe_json_items,
                            BASE_DIR,
                            processing_tasks,
                            inflight_tasks,
                            completed_tasks,
                            task_states,
                            state_lock,
                            "Worker exited before the task completed",
                        )
                        recovered_task_logger = task_timeline_logger(
                            parent_timeline_logger,
                            item.get("task_index", item.get("task_idx", "-")),
                            item.get("task_id", "-"),
                        )
                        recovered_task_logger.warning(
                            "task_recovered status=FAILURE recovery=worker_exit artifact_repair=true"
                        )
                        log_task_artifacts(recovered_task_logger, output_dir)

                if len(completed_tasks) >= len(indices):
                    continue
                if dict(worker_states.get(wid, {})).get("quarantined"):
                    print(f"Worker {wid} 已隔离，不再重启对应浏览器槽位")
                    continue
                if worker_restart_counts[wid] < MAX_WORKER_RESTARTS:
                    worker_restart_counts[wid] += 1
                    print(
                        f"Worker {wid} 异常退出，重启第 {worker_restart_counts[wid]}/"
                        f"{MAX_WORKER_RESTARTS} 次"
                    )
                    start_worker(wid)
                else:
                    print(f"Worker {wid} 已达到重启上限，后续任务将生成失败产物")

            if not any(p.is_alive() for p in procs.values()) and len(completed_tasks) < len(indices):
                # 所有 Worker 都不可用时，主进程兜底完成剩余任务，保证官方输出可识别。
                for task_index in indices:
                    if task_index not in completed_tasks:
                        finalize_unclaimed_task(
                            task_index,
                            "parent",
                            scribe_json_items,
                            BASE_DIR,
                            processing_tasks,
                            inflight_tasks,
                            completed_tasks,
                            task_states,
                            state_lock,
                            "No Worker remained available to execute the task",
                        )
                        item = scribe_json_items[task_index]
                        failed_task_logger = task_timeline_logger(
                            parent_timeline_logger,
                            item.get("task_index", item.get("task_idx", "-")),
                            item.get("task_id", "-"),
                        )
                        failed_task_logger.error(
                            "task_finished status=FAILURE failure_kind=NoWorkerAvailable"
                        )
                        log_task_artifacts(failed_task_logger, get_task_output_dir(BASE_DIR, item))
                break
    except KeyboardInterrupt:
        print("\n中断，终止所有进程...")
        for proc in procs.values():
            proc.terminate()
        for proc in procs.values():
            proc.join(timeout=5)
    finally:
        # Stop Workers that may still be waiting in connection preflight after
        # the shared task count has reached completion.
        for proc in procs.values():
            if proc.is_alive():
                proc.terminate()
        for proc in procs.values():
            proc.join(timeout=5)
        log_scheduler_snapshot(
            parent_timeline_logger,
            worker_states,
            task_states,
            procs,
            completed_tasks,
            len(indices),
        )
        parent_timeline_logger.info(
            "run_finished total_tasks=%s completed_tasks=%s abnormal_worker_exits=%s",
            len(indices),
            len(completed_tasks),
            abnormal_worker_exits,
        )
        parent_timeline_logger.info("parent_finished")
        parent_timeline_logger.handlers.clear()
        app_log_listener.stop()
