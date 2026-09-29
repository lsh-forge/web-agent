#!/bin/bash
# WebRetriever NavEval integration.
# Usage: bash scripts/run_naveval.sh <agent_output_dir> <eval_output_dir> [filter|eval|both] [max_workers]
set -euo pipefail
export PYTHONIOENCODING=UTF-8

if [ $# -lt 2 ]; then
    echo "Usage: bash scripts/run_naveval.sh <agent_output_dir> <eval_output_dir> [filter|eval|both] [max_workers]"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_DIR="$1"
SAVE_DIR="$2"
MODE="${3:-both}"
MAX_WORKERS="${4:-8}"

if [ ! -d "$TEST_DIR" ]; then
    echo "Agent output directory does not exist: $TEST_DIR"
    exit 1
fi

# config.json can use judge_api_base/judge_api_key/judge_api_model to select a
# separate judge model. When omitted, its api_* values are reused.
python3 "$PROJECT_DIR/src/eval/naveval.py" \
    --config "$PROJECT_DIR/config.json" \
    --mode "$MODE" \
    --max-workers "$MAX_WORKERS" \
    --test-dir "$TEST_DIR" \
    --save-dir "$SAVE_DIR"
