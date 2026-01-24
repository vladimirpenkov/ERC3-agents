# ERC32 Agent

Agent for [ERC3 (Enterprise Reasoning Challenge 3)](https://www.timetoact-group.at/events/enterprise-rag-challenge-part-3) — HR/PM benchmark tasks: employees, projects, wiki, security policies.

## Approach

**SGR-based Agentic Workflows with Named Entity Recognition and Non-reasoning Models**

- **SGR (Schema-Guided Reasoning)** — LLM outputs constrained to valid tool calls via Pydantic schemas and OpenAI structured outputs
- **Named Entity Recognition** — entity_extractor agent resolves mentions like "John's manager" or "Project Alpha" to concrete IDs before task execution
- **Non-reasoning models** — optimized for speed and cost using smaller models (gpt-4o-mini, gemini-flash) with structured outputs instead of chain-of-thought
- **Multi-agent pipeline** — separation of concerns: entity resolution → security check → task execution

## Results

**Target:** 90% accuracy, ≤10 sec avg per LLM call, ≤$1 per 103 tasks.

| Mode                   | Accuracy  | Cost (OpenRouter)   |
|------------------------|-----------|---------------------|
| Default                | up to 88% | $0.24 per 103 tasks |
| With partial reasoning | up to 95% | $0.40 per 103 tasks |


**About the +7% boost:**

The list of "complex" tasks in `agents/solver/agent_cfg.py` is derived from historical statistics across multiple sessions — knowledge that cannot be obtained from a single run. This demonstrates the *potential* of the approach rather than a fair single-run benchmark result of non-reasoning cheap model.

The "stronger" model is the same grok-4.1-fast with `"extra_body": {"reasoning": {"enabled": True}}`. It runs slower but provides significant accuracy gains on tasks that require deeper reasoning.

## Requirements

- Python 3.11+
- API keys:
  - `ERC3_API_KEY` — competition platform access
  - `OPENROUTER_API_KEY` — LLM provider

## Installation

```bash
cp .env.example .env       # then fill in your API keys
pip install -r requirements.txt
./scripts/setup.sh         # downloads embedding model, indexes wiki
```

## Usage

Activate your Python environment, then:

```bash
./run_session.sh  # Full session (all tasks)
```

## Configuration

Project settings in `config.py`:

| Parameter | Description                                                         |
|-----------|---------------------------------------------------------------------|
| `task_codes` | Run only specific tasks by spec_id (empty = all tasks)              |
| `task_name_filter` | Run tasks containing substring in text                              |
| `data_dump` | Download API data before each task for debugging (can take a while) |
| `policy_rulebook` | Security rules file in `data/`                                      |

## Agents

Each agent is a module in `agents/<name>/`:

```
agents/<name>/
├── __init__.py      # Exports run(context) function
├── agent.py         # Main logic
├── agent_cfg.py     # LLM settings (MODEL_ID, TEMPERATURE, etc.)
└── prompts.py       # System prompts
```

| Agent              | Purpose                            |
|--------------------|------------------------------------|
| `entity_extractor` | Resolves entity mentions to IDs    |
| `watchdog`         | Checks security policy             |
| `solver`           | Executes the task                  |
| `guest_handler`    | Handles public (non-employee) requests |

## Project Structure

```
.
├── main.py              # Entry point
├── config.py            # AgentConfig (data only)
├── agents/              # Agent modules
│   ├── common.py        # TaskContext, run_agent()
│   ├── entity_extractor/# Resolves {employee:id}, {project:id}
│   ├── watchdog/        # Security policy check
│   ├── solver/          # Task execution
│   └── guest_handler/   # Public user handling
├── infra/               # Infrastructure (LLM, file I/O, wiki RAG)
├── tools/               # API wrappers, DTOs
├── data/                # Static data (departments, locations, skills)
├── scripts/             # Utilities (setup.sh, stats)
├── logs/                # Session logs (generated)
└── wiki/                # Company wiki data and indexes
```

**Pipeline:** `entity_extractor → watchdog → solver`

## Resources

- Competition: https://www.timetoact-group.at/events/enterprise-rag-challenge-part-3
- Platform: https://erc.timetoact-group.at/

## License

[MIT](LICENSE)
