#!/usr/bin/env python
"""
LLM time statistics per session.

Usage:
    python scripts/llm_time_stats.py <session_dir>
    python scripts/llm_time_stats.py logs/sessions/1220-0532-ssn-42YtqvozgSQr1un3btTjgy

Output:
    1. JSON data (for programmatic use)
    2. Formatted report (for human reading)
"""

import json
import re
import sys
from pathlib import Path
from statistics import median
from collections import defaultdict


def parse_jsonl(filepath: Path) -> list:
    """Parse JSONL file."""
    records = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass
    return records


def parse_agent_logs(session_dir: Path) -> dict:
    """Parse agent JSONL logs for cost and stats."""

    models = defaultdict(lambda: {
        "duration": 0.0,
        "calls": 0,
        "cost": 0.0,
        "tokens": {"prompt": 0, "cached": 0, "completion": 0}
    })
    agents = defaultdict(lambda: {"cost": 0.0, "calls": 0})
    task_costs = {}

    # Find all task directories
    task_dirs = [d for d in session_dir.iterdir() if d.is_dir() and d.name.startswith("t")]

    for task_dir in task_dirs:
        task_id = task_dir.name
        task_costs[task_id] = 0.0

        # Parse agent logs
        for jsonl_file in task_dir.glob("*.jsonl"):
            agent_name = jsonl_file.stem

            # Skip non-agent files
            if agent_name in ("task_result", "resolved_objects"):
                continue

            records = parse_jsonl(jsonl_file)
            for r in records:
                stats = r.get("stats", {})
                cost = stats.get("cost", 0) or 0
                model = stats.get("model", "unknown")
                duration = stats.get("duration_sec", 0) or 0
                tokens_prompt = stats.get("tokens_prompt", 0) or 0
                tokens_cached = stats.get("tokens_cached", 0) or 0
                tokens_completion = stats.get("tokens_completion", 0) or 0

                if cost or duration:
                    models[model]["cost"] += cost
                    models[model]["duration"] += duration
                    models[model]["calls"] += 1
                    models[model]["tokens"]["prompt"] += tokens_prompt
                    models[model]["tokens"]["cached"] += tokens_cached
                    models[model]["tokens"]["completion"] += tokens_completion

                    agents[agent_name]["cost"] += cost
                    agents[agent_name]["calls"] += 1

                    task_costs[task_id] += cost

    return {
        "models": dict(models),
        "agents": dict(agents),
        "task_costs": task_costs,
        "total_cost": sum(models[m]["cost"] for m in models),
    }


def parse_task_results(session_dir: Path) -> dict:
    """Parse task_result.jsonl files for scores."""
    scores = {}

    task_dirs = [d for d in session_dir.iterdir() if d.is_dir() and d.name.startswith("t")]

    for task_dir in task_dirs:
        task_id = task_dir.name
        result_file = task_dir / "task_result.jsonl"

        if result_file.exists():
            records = parse_jsonl(result_file)
            for r in records:
                if "score" in r:
                    scores[task_id] = r["score"]

    return scores


def parse_log_message(message: str) -> dict:
    """Parse log message string like: type='...' body={...} category='telemetry'"""
    result = {}

    # Extract category
    cat_match = re.search(r"category='([^']*)'", message)
    if cat_match:
        result["category"] = cat_match.group(1)

    # Extract body - find body={...} handling nested braces
    body_start = message.find("body={")
    if body_start != -1:
        body_start += 5  # skip "body="
        depth = 0
        body_end = body_start
        for i, c in enumerate(message[body_start:], body_start):
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    body_end = i + 1
                    break

        body_str = message[body_start:body_end]
        try:
            body = {}

            model_match = re.search(r"'model':\s*'([^']*)'", body_str)
            if model_match:
                body["model"] = model_match.group(1)

            duration_match = re.search(r"'duration_sec':\s*([\d.]+)", body_str)
            if duration_match:
                body["duration_sec"] = float(duration_match.group(1))

            prompt_match = re.search(r"'prompt_tokens':\s*(\d+)", body_str)
            if prompt_match:
                body["prompt_tokens"] = int(prompt_match.group(1))

            cached_match = re.search(r"'cached_prompt_tokens':\s*(\d+)", body_str)
            if cached_match:
                body["cached_prompt_tokens"] = int(cached_match.group(1))

            completion_match = re.search(r"'completion_tokens':\s*(\d+)", body_str)
            if completion_match:
                body["completion_tokens"] = int(completion_match.group(1))

            result["body"] = body
        except (ValueError, AttributeError):
            pass

    return result


def parse_sess_serv(sess_serv_path: Path) -> dict:
    """Parse sess_serv.json and extract LLM stats."""

    tasks = {}  # spec_id -> stats
    models = defaultdict(lambda: {
        "duration": 0.0,
        "calls": 0,
        "tokens": {"prompt": 0, "cached": 0, "completion": 0}
    })
    current_task = None

    with open(sess_serv_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    for event in data:
        event_type = event.get("type")

        if event_type == "task_start":
            current_task = event.get("spec_id")
            if current_task and current_task not in tasks:
                tasks[current_task] = {
                    "duration": 0.0,
                    "calls": 0,
                    "tokens": {"prompt": 0, "cached": 0, "completion": 0}
                }

        elif event_type == "task_log":
            log = event.get("log", {})
            message = log.get("message", "")

            parsed = parse_log_message(message)

            if parsed.get("category") == "telemetry":
                body = parsed.get("body", {})

                duration = body.get("duration_sec")
                if duration is None:
                    continue

                prompt_tokens = body.get("prompt_tokens", 0)
                cached_tokens = body.get("cached_prompt_tokens", 0)
                completion_tokens = body.get("completion_tokens", 0)
                model = body.get("model", "unknown")

                # Add to current task
                if current_task and current_task in tasks:
                    tasks[current_task]["duration"] += duration
                    tasks[current_task]["calls"] += 1
                    tasks[current_task]["tokens"]["prompt"] += prompt_tokens
                    tasks[current_task]["tokens"]["cached"] += cached_tokens
                    tasks[current_task]["tokens"]["completion"] += completion_tokens

                # Add to model stats
                models[model]["duration"] += duration
                models[model]["calls"] += 1
                models[model]["tokens"]["prompt"] += prompt_tokens
                models[model]["tokens"]["cached"] += cached_tokens
                models[model]["tokens"]["completion"] += completion_tokens

    # Calculate session summary
    if tasks:
        durations = [t["duration"] for t in tasks.values()]
        calls = [t["calls"] for t in tasks.values()]

        summary = {
            "total_duration": round(sum(durations), 2),
            "total_calls": sum(calls),
            "total_tokens": {
                "prompt": sum(t["tokens"]["prompt"] for t in tasks.values()),
                "cached": sum(t["tokens"]["cached"] for t in tasks.values()),
                "completion": sum(t["tokens"]["completion"] for t in tasks.values())
            },
            "task_count": len(tasks),
            "avg_duration_per_task": round(sum(durations) / len(durations), 2),
            "median_duration_per_task": round(median(durations), 2),
            "avg_calls_per_task": round(sum(calls) / len(calls), 2)
        }
    else:
        summary = {
            "total_duration": 0,
            "total_calls": 0,
            "total_tokens": {"prompt": 0, "cached": 0, "completion": 0},
            "task_count": 0,
            "avg_duration_per_task": 0,
            "median_duration_per_task": 0,
            "avg_calls_per_task": 0
        }

    # Round task durations
    for t in tasks.values():
        t["duration"] = round(t["duration"], 2)

    # Round model durations
    for m in models.values():
        m["duration"] = round(m["duration"], 2)

    return {
        "tasks": tasks,
        "session": summary,
        "models": dict(models)
    }


def fmt_num(n: int) -> str:
    """Format number with thousand separators."""
    return f"{n:,}".replace(",", " ")


def fmt_k(n: int) -> str:
    """Format number in thousands (K)."""
    return f"{n / 1000:.1f}K"


def print_report(stats: dict, session_name: str, agent_stats: dict = None, scores: dict = None):
    """Print formatted report."""
    s = stats["session"]
    models = stats["models"]
    tasks = stats["tasks"]

    print(f"\n{'='*50}")
    print(f"REPORT: {session_name}")
    print(f"{'='*50}")
    print()

    # Score and cost summary
    if scores:
        total_score = sum(scores.values())
        scored_count = len(scores)
        avg_score = total_score / scored_count if scored_count else 0
        cost_str = f"  Cost: ${agent_stats['total_cost']:.4f}" if agent_stats and agent_stats.get("total_cost") else ""
        print(f"Score: {avg_score:.1%}{cost_str}")

    # Session summary (compact)
    print(f"Duration: {s['total_duration']:.0f}s  Calls: {s['total_calls']}  Tasks: {s['task_count']}")
    print(f"Avg/task: {s['avg_duration_per_task']:.1f}s  Median: {s['median_duration_per_task']:.1f}s  Calls/task: {s['avg_calls_per_task']:.1f}")

    # Models table with cost
    if agent_stats:
        print(f"\n{'Model':<20} {'Cost':>10} {'Prompt':>8} {'Compl':>8}")
        print("-" * 48)
        for model in sorted(agent_stats["models"].keys()):
            m = agent_stats["models"][model]
            short_name = model.split("/")[-1] if "/" in model else model
            cost_str = f"${m['cost']:.4f}"
            print(f"{short_name:<20} {cost_str:>10} {fmt_k(m['tokens']['prompt']):>8} {fmt_k(m['tokens']['completion']):>8}")
    else:
        # Fallback to old format
        print(f"\n{'Model':<18} {'Time':>8} {'Calls':>6} {'Prompt':>8} {'Cached':>8} {'Compl':>8}")
        print("-" * 60)
        for model, m in sorted(models.items()):
            short_name = model.split("/")[-1] if "/" in model else model
            print(f"{short_name:<18} {m['duration']:>8.1f} {m['calls']:>6} {fmt_k(m['tokens']['prompt']):>8} {fmt_k(m['tokens']['cached']):>8} {fmt_k(m['tokens']['completion']):>8}")

    # Top 3 longest tasks
    sorted_tasks = sorted(tasks.items(), key=lambda x: x[1]["duration"], reverse=True)
    print(f"\nTop 3 longest tasks:")
    print(f"{'Task':<8} {'Time':>8} {'Calls':>6}")
    print("-" * 24)
    for tid, t in sorted_tasks[:3]:
        print(f"{tid:<8} {t['duration']:>8.1f} {t['calls']:>6}")



def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/llm_time_stats.py <session_dir>")
        print("Example: python scripts/llm_time_stats.py logs/sessions/1220-0532-ssn-42YtqvozgSQr1un3btTjgy")
        sys.exit(1)

    session_dir = Path(sys.argv[1])

    if not session_dir.exists():
        print(f"Error: {session_dir} not found")
        sys.exit(1)

    # Parse agent logs for cost (new)
    agent_stats = parse_agent_logs(session_dir)

    # Parse task results for scores (new)
    scores = parse_task_results(session_dir)

    # Parse sess_serv.json for duration/tokens (original)
    sess_serv_path = session_dir / "sess_serv.json"
    if sess_serv_path.exists():
        stats = parse_sess_serv(sess_serv_path)
    else:
        # Fallback: minimal stats from agent logs
        stats = {
            "session": {
                "total_duration": sum(m["duration"] for m in agent_stats["models"].values()),
                "total_calls": sum(m["calls"] for m in agent_stats["models"].values()),
                "total_tokens": {"prompt": 0, "cached": 0, "completion": 0},
                "task_count": len(agent_stats["task_costs"]),
                "avg_duration_per_task": 0,
                "median_duration_per_task": 0,
                "avg_calls_per_task": 0,
            },
            "models": {},
            "tasks": {},
        }

    # Extract session name from path
    session_name = session_dir.name

    # Formatted report
    print_report(stats, session_name, agent_stats, scores)


if __name__ == "__main__":
    main()
