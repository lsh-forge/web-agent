"""
NavEval Benchmark Evaluator
============================
Refactored from test_dw.py — all hardcoded inputs → CLI args.

Usage:
    python naveval.py --mode both --test-dir ./task_path --save-dir ./output
    python eval_benchmark.py --date xxx --mode both --max-workers 16
    python eval_benchmark.py --date xxx --mode filter
    python eval_benchmark.py --help
"""

import sys
import json
import os
import argparse
import tiktoken
import glob
import multiprocessing as mp
import base64
import re
import time
from openai import OpenAI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.filter.domain_filter import domain_filter
from common.filter.type_filter import type_filter
from common.formatter.request_formatter import requests_formatter
from common.filter.nlp_filter import nlp_filter_requests


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="NavEval: filter & LLM-judge evaluation")
    # Paths
    p.add_argument("--test-dir", default=None,
                   help="Test result dir (default: .../test_result_{date})")
    p.add_argument("--save-dir", default=None,
                   help="Eval output dir (default: .../eval_result_{date}/eval_benchmark)")
    p.add_argument("--config", default=None,
                   help="Optional Agent config.json. Uses judge_* fields first, then api_* fields.")

    # Mode
    p.add_argument("--mode", choices=["filter", "eval", "both"], default="eval",
                   help="filter | eval | both (default: eval)")

    # Parallelism & budget
    p.add_argument("--max-workers", type=int, default=8)
    p.add_argument("--max-tokens-filter", type=int, default=120000,
                   help="Max token budget for filtered requests")

    # LLM
    p.add_argument("--api-key",     default="")
    p.add_argument("--api-base",    default="")
    p.add_argument("--model",       default="")
    p.add_argument("--temperature", type=float, default=0.1)
    p.add_argument("--max-tokens-llm", type=int, default=36384)
    p.add_argument("--max-retries", type=int, default=3)

    args = p.parse_args()

    return args


# ─────────────────────────────────────────────────────────────────────────────
# Global LLM config (populated from CLI)
# ─────────────────────────────────────────────────────────────────────────────

_LLM_CFG = {}


# ─────────────────────────────────────────────────────────────────────────────
# Token counting
# ─────────────────────────────────────────────────────────────────────────────

def count_tokens(text, model="cl100k_base"):
    text_str = text if isinstance(text, str) else str(text)
    encoding = tiktoken.get_encoding(model)
    return len(encoding.encode(text_str))


# ─────────────────────────────────────────────────────────────────────────────
# Request filtering
# ─────────────────────────────────────────────────────────────────────────────

def safe_truncate_requests(requests, max_token):
    tok = count_tokens(requests)
    while tok > max_token and len(requests) > 1:
        requests.pop(0)
        tok = count_tokens(requests)
        print(f"  truncate → {tok} tokens")
    return requests


def filter_requests(filepath, max_token=120000):
    dir_path = os.path.dirname(filepath)
    task_id = os.path.basename(dir_path)

    with open(filepath, 'r', encoding='utf-8') as f:
        raw_data = json.load(f)
    records = raw_data if isinstance(raw_data, list) else raw_data.get("all_requests", [])

    downloads = [record for record in records if record.get("evidence_type") == "download"]
    requests = [record for record in records if record.get("evidence_type") != "download"]
    print(f"[{task_id}] raw={len(records)} events, {count_tokens(records)} tokens")
    print(f"  download events: {len(downloads)}")

    with open(f"{dir_path}/result.json", 'r', encoding='utf-8') as f:
        result_data = json.load(f)
    start_url = result_data.get("website")

    # API requests follow the normal type/domain pipeline. Download events are
    # browser outcomes, not API calls: their URLs may be cross-domain or blob:
    # and must remain available to the judge as independent evidence.
    try:
        filtered = type_filter(requests)
        print(f"  after type_filter: {len(filtered)}")
    except Exception as e:
        print(f"  ERROR type_filter: {e}")
        return {"all_passed": False, "details": [
            {"action": "Setup Process", "passed": False, "reason": str(e)}]}

    try:
        filtered = domain_filter(filtered, start_url)
        print(f"  after domain_filter: {len(filtered)}")
    except Exception as e:
        return f"Error domain_filter: {e}"

    try:
        filtered = requests_formatter(filtered)
        download_evidence = requests_formatter(downloads)
        filtered = sorted(
            [*filtered, *download_evidence],
            key=lambda record: record.get("timestamp", 0),
        )
        print(f"  after formatter: {len(filtered)}")
    except Exception as e:
        return f"Error formatter: {e}"

    tok = count_tokens(filtered)
    print(f"  final tokens: {tok}")
    if tok > max_token:
        print(f"  WARNING: exceeds {max_token}, truncating...")
        filtered = safe_truncate_requests(filtered, max_token)

    with open(f"{dir_path}/filtered_requests.json", 'w', encoding='utf-8') as f:
        json.dump(filtered, f, indent=2, ensure_ascii=False)
    return "OK"


# ─────────────────────────────────────────────────────────────────────────────
# LLM judge
# ─────────────────────────────────────────────────────────────────────────────

EVAL_SYSTEM_PROMPT = """\
## Role

You are a benchmark validator.
Your job is to decide whether a browser-automation agent successfully completed a task by examining:

1. **Task description** – defines the goal and the key actions required. Every requirement, keyword, constraint, or objective in the task is treated as a criterion.
2. **Network requests** – simplified logs revealing user operations such as searches, filters, sorts, form submissions, item selections, report retrieval, etc.
3. **Final screenshot** – shows the final state of the page and is used to verify that the result matches the task requirements.
4. **URL trajectory** – the full ordered list of URLs visited during navigation, reflecting the steps taken to reach the final state.

You must use **the combination of these three evidence channels** to evaluate whether each key requirement in the task was fulfilled.
However, **if a single evidence source alone is already sufficient to confirm or reject a requirement**, you may rely on that source.

## Evaluation Principles

### 1. Break the task into atomic requirements
Examples include: filters, ranges, keywords, sort criteria, extremum criteria (cheapest, highest-rated, newest, etc.), selecting a specific item, opening a report, completing a submission, or reaching a particular informational view.

Each requirement becomes one action item in the output.

### 2. Evidence interpretation

#### Network requests
Requests reveal operational steps such as:
- applied filters
- applied sort orders
- search queries
- backend apis
- item detail requests (ASINs, product IDs, listing IDs)
- "submit", "search", "apply", or other key transitions

Requests are often the clearest indicator of whether a requirement was explicitly performed.

#### URL trajectory
URL history is used to determine the **sequence of operations**, including:
- when filtering occurred
- when an item was opened
- when a page transition corresponds to a specific step in the task
- whether the sequence of URLs aligns with the required workflow

In some tasks, URL sequence contains the **expected operational milestones** implied by the task.

#### Final screenshot
Used to verify the final state:
- correct result set displayed
- correct item or report visible
- correct filter values reflected visually
- correct summary or confirmation shown
- correct price, date, rating, or other constraints satisfied

Screenshot = state verification; requests/URLs = operation verification.

### 3. Strict rules for numeric / filter / sort requirements

If the task defines a numeric range or exact condition (price, year, beds, dates, etc.), the requirement must match **exactly**:

- `$1500-$2500` requires that exact interval
- "exactly 2 beds" cannot be satisfied by "2+ beds"
- "newest", "cheapest", "highest-rated", etc. require a sort or otherwise unambiguous extremum selection

Failure to match these conditions exactly -> requirement fails.

### 4. Success, failure, confidence

You must output:
- Whether all requirements were satisfied
- A confidence score
- Per-requirement judgments

**Success (all_passed = true)**
All key requirements are supported by evidence.

**Failure (all_passed = false)**
At least one requirement is violated or cannot be confidently verified.

**Confidence scoring:**
- Clear success: **0.75-1.0**
- Clear failure: **0.10-0.39**
- Evidence insufficient to confirm a requirement:
  failure with **0.40-0.50**, and explanation why evidence does not conclusively satisfy the requirement.

## Output Format (Strict)

{
    "all_passed": True/False,
    "score": <float number between 0-1>
    "reasoning": <top level reasoning why the result is success or failure>
    "details": [
        {
            "action": <key action to validate>,
            "passed": True/False,
            "reason": <detailed reason>
        }
    ]
}

Return only the JSON object, no commentary."""


def build_eval_messages(task, url_trajectory, network_requests, base64_image):
    user_text = (
        f"## User Input Data\n"
        f"- task: {task}\n"
        f"- url trajectory: {url_trajectory}\n"
        f"- browser evidence (network requests and download events): {network_requests}\n"
        f"- last screenshot: last screenshot is appended in messages with image_url_type"
    )
    return [
        {"role": "system", "content": EVAL_SYSTEM_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}}
        ]}
    ]


def call_llm(messages):
    cfg = _LLM_CFG
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["api_base"])

    for attempt in range(cfg["max_retries"]):
        try:
            stream = client.chat.completions.create(
                model=cfg["model"],
                messages=messages,
                temperature=cfg["temperature"],
                max_tokens=cfg["max_tokens_llm"],
                stream=True,
            )
            usage_dict, ans_str, reason_str = {}, "", ""

            for chunk in stream:
                if not chunk.choices:
                    if chunk.usage:
                        usage_dict = chunk.usage.to_dict()
                    continue
                delta = chunk.choices[0].delta
                if getattr(delta, 'reasoning_content', None):
                    reason_str += delta.reasoning_content
                elif delta.content:
                    ans_str += delta.content

            if not reason_str:
                m = re.search(r'<thinking>(.*?)</thinking>', ans_str, re.DOTALL)
                if m:
                    reason_str = m.group(1)

            return ans_str, usage_dict, reason_str

        except Exception:
            import traceback
            traceback.print_exc()
            delay = 2 ** (attempt + 1)
            if attempt < cfg["max_retries"] - 1:
                print(f"  retry {attempt+1}/{cfg['max_retries']} in {delay}s ...")
                time.sleep(delay)
            else:
                raise


def eval_task(dir_path, save_base_dir):
    task_id = os.path.basename(os.path.normpath(dir_path))
    eval_result_path = f"{save_base_dir}/{task_id}.json"
    if os.path.exists(eval_result_path):
        return

    with open(f"{dir_path}/filtered_requests.json", 'r', encoding='utf-8') as f:
        requests_data = json.load(f)
    with open(f"{dir_path}/result.json", 'r', encoding='utf-8') as f:
        result_data = json.load(f)

    task = result_data.get("task")
    if "### key points" in task:
        task = task.split("### key points")[0]
    url_trajectory = list(dict.fromkeys(result_data.get("urls")))

    image_list = sorted(os.listdir(f'{dir_path}/trajectory'), key=lambda x: int(x.split('.')[0]))
    last_img = f"{dir_path}/trajectory/{image_list[-1]}"
    with open(last_img, "rb") as f:
        screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")

    messages = build_eval_messages(task, url_trajectory, requests_data, screenshot_b64)
    ans_str, usage_dict, reason_str = call_llm(messages)
    print(f"[{task_id}] Reasoning: {reason_str[:200]}...")
    print(f"[{task_id}] Ans: {ans_str[:200]}...")

    result = parse_judge_result(ans_str)
    with open(eval_result_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"[{task_id}] saved → {eval_result_path}")


def parse_judge_result(raw_text):
    """Extract the judge's JSON object from common OpenAI-compatible outputs."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    if text.endswith("```"):
        text = text[:-3].strip()

    # Some reasoning models omit the opening brace, and occasionally the
    # opening quote of the first key too: all_passed": true, ...
    if re.match(r'^"?all_passed"?\s*:', text):
        if text.startswith('"all_passed"'):
            text = "{" + text
        else:
            text = '{"' + text
        if not text.endswith("}"):
            text += "}"

    start = text.find("{")
    if start < 0:
        raise ValueError("裁判模型未返回 JSON 对象")

    # raw_decode accepts a valid object followed by commentary, but avoids
    # accepting arbitrary text before the JSON object.
    result, _ = json.JSONDecoder().raw_decode(text[start:])
    if not is_complete_judge_result(result):
        raise ValueError("裁判模型返回的 JSON 顶层不是对象")
    return result


def is_complete_judge_result(result):
    """Reject a nested detail object accidentally parsed as the whole verdict."""
    return isinstance(result, dict) and {"all_passed", "score", "reasoning", "details"}.issubset(result)


# ─────────────────────────────────────────────────────────────────────────────
# Multiprocessing workers
# ─────────────────────────────────────────────────────────────────────────────

def _worker_eval(wid, q_in, q_out, worker_config):
    global _LLM_CFG
    # Windows multiprocessing uses spawn, so workers do not inherit the
    # parent process's module globals. Pass judge configuration explicitly.
    save_base_dir = worker_config["save_base_dir"]
    _LLM_CFG = worker_config["llm_config"]
    print(f"[Worker-{wid}] start", flush=True)
    while True:
        try:
            task_dir = q_in.get_nowait()
        except Exception:
            break
        try:
            eval_task(task_dir, save_base_dir)
            q_out.put(("ok", task_dir))
        except Exception as e:
            q_out.put(("error", f"{task_dir}: {e}"))
    print(f"[Worker-{wid}] done", flush=True)


def _worker_filter(wid, q_in, q_out, max_token):
    print(f"[Worker-{wid}] start", flush=True)
    while True:
        try:
            filepath = q_in.get_nowait()
        except Exception:
            break
        try:
            filter_requests(filepath, max_token)
            q_out.put(("ok", filepath))
        except Exception as e:
            q_out.put(("error", f"{filepath}: {e}"))
    print(f"[Worker-{wid}] done", flush=True)


def _run_pool(fn, items, n_workers, extra_arg):
    q_in, q_out = mp.Queue(), mp.Queue()
    for item in items:
        q_in.put(item)

    procs = []
    for i in range(n_workers):
        p = mp.Process(target=fn, args=(i, q_in, q_out, extra_arg))
        p.start()
        procs.append(p)
    for p in procs:
        p.join()

    results = []
    while not q_out.empty():
        results.append(q_out.get())
    ok = sum(1 for s, _ in results if s == "ok")
    err = sum(1 for s, _ in results if s == "error")
    print(f"Done: {ok} ok, {err} errors")
    for s, msg in results:
        if s == "error":
            print(f"  ERROR: {msg}")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Batch entry points
# ─────────────────────────────────────────────────────────────────────────────

def run_filter(test_dir, max_workers, max_token):
    captures = glob.glob(os.path.join(test_dir, "*/capture.json"))
    todo, skip = [], 0
    for cap in captures:
        d = os.path.dirname(cap)
        if os.path.exists(os.path.join(d, "filtered_requests.json")):
            skip += 1
            continue
        result_f = os.path.join(d, "result.json")
        with open(result_f, 'r', encoding='utf-8') as f:
            r = json.load(f)
        if r.get("status") not in ["SUCCESS", "FAIL_CALL_USER", "FAIL_SCROLLDOWN", ""]:
            continue
        todo.append(cap)
    print(f"Filter: skip={skip}, todo={len(todo)}")
    return _run_pool(_worker_filter, todo, max_workers, max_token)


def run_eval(test_dir, save_dir, max_workers, llm_config):
    captures = glob.glob(os.path.join(test_dir, "*/capture.json"))
    todo, skip = [], 0
    for cap in captures:
        d = os.path.dirname(cap)
        task_id = os.path.basename(os.path.dirname(cap))
        filtered_path = os.path.join(d, "filtered_requests.json")
        if not os.path.exists(filtered_path):
            # filtered_requests.json is judge input, not an optional cache.
            # Agent reruns recreate task directories and can delete it, so
            # rebuild from the durable capture.json before judging.
            print(f"Eval: rebuild missing filtered requests for {task_id}")
            filter_requests(cap)
        if not os.path.exists(filtered_path):
            print(f"Eval: skip {task_id}; failed to build filtered requests")
            continue
        eval_path = os.path.join(save_dir, f"{task_id}.json")
        if os.path.exists(eval_path):
            try:
                with open(eval_path, "r", encoding="utf-8") as f:
                    if is_complete_judge_result(json.load(f)):
                        skip += 1
                        continue
                print(f"Eval: re-run incomplete result {task_id}")
            except Exception:
                print(f"Eval: re-run unreadable result {task_id}")
        todo.append(d)
    print(f"Eval: skip={skip}, todo={len(todo)}")
    return _run_pool(
        _worker_eval,
        todo,
        max_workers,
        {"save_base_dir": save_dir, "llm_config": llm_config},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    global _LLM_CFG
    args = parse_args()

    # Local integration: reuse the agent connection by default, while allowing
    # a stronger or separate judge through judge_api_base/judge_api_key/judge_api_model.
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            config = json.load(f)
        args.api_key = args.api_key or config.get("judge_api_key") or config.get("api_key", "")
        args.api_base = args.api_base or config.get("judge_api_base") or config.get("api_base", "")
        args.model = args.model or config.get("judge_api_model") or config.get("api_model", "")

    if not args.test_dir or not args.save_dir:
        raise ValueError("--test-dir 和 --save-dir 为必填参数")
    if args.mode in ("eval", "both") and (not args.api_base or not args.model):
        raise ValueError("评测需要 api_base 和 model：传命令行参数或使用 --config")

    _LLM_CFG = {
        "api_key":        args.api_key,
        "api_base":       args.api_base,
        "model":          args.model,
        "temperature":    args.temperature,
        "max_tokens_llm": args.max_tokens_llm,
        "max_retries":    args.max_retries,
    }

    os.makedirs(args.save_dir, exist_ok=True)

    print(f"Test dir:    {args.test_dir}")
    print(f"Save dir:    {args.save_dir}")
    print(f"Mode:        {args.mode}")
    print(f"Workers:     {args.max_workers}")
    print(f"Model:       {args.model}")
    print()

    if args.mode in ("filter", "both"):
        print("=" * 60)
        print("Phase 1: Filter requests")
        print("=" * 60)
        run_filter(args.test_dir, args.max_workers, args.max_tokens_filter)
        print()

    if args.mode in ("eval", "both"):
        print("=" * 60)
        print("Phase 2: LLM evaluation")
        print("=" * 60)
        run_eval(args.test_dir, args.save_dir, args.max_workers, _LLM_CFG)


if __name__ == "__main__":
    main()
