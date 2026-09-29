# web-agent

> A robust web agent for complex browser tasks, combining structured interaction, visual assistance, context management, and concurrent execution.

[中文版 README](README_zh.md)

## Highlights

- **Structured web interaction first** - Uses page semantics, accessibility snapshots, element references, and DOM inspection for routine browser operations.
- **Visual understanding and localization on demand** - Uses screenshots and grid-assisted coordinate localization when structured page information is insufficient.
- **Long-horizon context management** - Prunes and deduplicates tool output, summarizes history, and recovers when summarization fails.
- **Multi-process concurrency and fault recovery** - Runs independent workers for multiple CDP browser endpoints, with session health checks, bounded retries, and worker recovery.

## Architecture

![web-agent architecture](./web-agent-architecture.png)

Each worker runs a ReAct agent powered by AgentScope. Browser actions are performed through Playwright MCP; visual assistance is invoked only when needed. Context management controls the growth of tool output and interaction history, while the runtime layer coordinates concurrent workers and recovery.

## Technical Report

For the complete technical design, see the [Technical Report](技术报告.pdf).

## Quick Start

### 1. Install dependencies

An isolated Conda environment is recommended for a repeatable setup, but it is not mandatory. The project can also run in an existing environment that provides Python 3.11, Node.js, and the dependencies declared in `environment.yml`.

```bash
conda env create -f environment.yml
conda activate web-agent
```

### 2. Configure model access

Create `config.json` in the project root and keep API keys private. A minimal configuration is:

```json
{
  "api_base": "https://your-api-base/v1",
  "api_key": "YOUR_API_KEY",
  "api_model": "YOUR_VISION_MODEL",
  "text_api_base": "https://your-api-base/v1",
  "text_api_key": "YOUR_API_KEY",
  "text_api_model": "YOUR_TEXT_MODEL"
}
```

### 3. Run an evaluation

```bash
bash scripts/run.sh <task_file> <output_dir> <cdp_url_1> [cdp_url_2 ...]
```

Pass one or more remote browser CDP endpoints. The system starts one independent worker per endpoint, supporting up to eight concurrent workers in the evaluation setting.

## Project Layout

```text
web-agent/
├── web-agent-architecture.png  # System architecture diagram
├── config.json             # Local model configuration (do not commit secrets)
├── environment.yml         # Conda and pip dependencies
├── scripts/run.sh          # Evaluation entry point
└── src/agent/
    ├── agent.py            # Agent loop and tool orchestration
    ├── main.py             # Multi-process task scheduling
    ├── middlewares/        # Context, logging, timeout, and recovery support
    ├── tools/              # Visual and browser helper tools
    └── web_controller.py   # Remote browser control
```

Each task writes its final answer and audit artifacts under the specified output directory, including `result.json`, `trajectory/`, and `capture.json`.

## Results

| Evaluation | Setting | Pass rate |
| --- | --- | --- |
| [WebRetriever dataset](https://huggingface.co/datasets/Mininglamp-2718/WebRetriever) | Protocol 1 test set | **79%** |
| [WebRetriever Challenge](https://mininglamp-ai.github.io/WebRetriever_Challenge/?lang=zh) | Protocol 3 concurrent evaluation | **59%** |

## Acknowledgements and License

This project targets complex web tasks involving webpage understanding, multi-step interaction, visual localization, context management, and concurrent execution. It is built with [AgentScope](https://github.com/agentscope-ai/agentscope) and [Playwright MCP](https://github.com/microsoft/playwright-mcp), and evaluated on the [WebRetriever](https://github.com/Mininglamp-AI/WebRetriever) benchmark and challenge.

See [LICENSE](LICENSE) for license information.
