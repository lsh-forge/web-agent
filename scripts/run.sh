#!/bin/bash
# ============================================================
# 评测入口脚本
# 用法: bash run.sh <task_file> <output_dir> <cdp_url1> [cdp_url2] ...
#
# 模型 API 配置请在 config.json 中设置，main.py 会自动读取。
# ============================================================
set -e

# 安装系统依赖（opencv 需要 libopenblas）
apt-get install -y libopenblas-base > /dev/null 2>&1 || true

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ====== 参数校验 ======
if [ $# -lt 3 ]; then
    echo "❌ 参数不足"
    echo "用法: bash run.sh <task_file> <output_dir> <cdp_url1> [cdp_url2] ..."
    exit 1
fi

TASK_FILE="$1"
OUTPUT_DIR="$2"
shift 2
CDP_URLS=("$@")

if [ ! -f "$TASK_FILE" ]; then
    echo "❌ 任务文件不存在: $TASK_FILE"
    exit 1
fi

echo "📋 任务文件: $TASK_FILE"
echo "📁 输出目录: $OUTPUT_DIR"
echo "🔗 CDP URLs (${#CDP_URLS[@]}):"
for u in "${CDP_URLS[@]}"; do
    echo "  ${u:0:80}..."
done

# ====== 运行 ======
cd "$PROJECT_DIR/src/agent"
python3 main.py \
    --input "$TASK_FILE" \
    --output "$OUTPUT_DIR" \
    --cdp_url "${CDP_URLS[@]}"

echo "✅ 评测完成"
