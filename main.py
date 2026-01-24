import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
# Load .env from repo root
load_dotenv(Path(__file__).parent / ".env")

# Force UTF-8 encoding for stdout/stderr on Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from infra import (
    finalize_task, write_json_event,
    TaskTerminated, ensure_wiki, reset_task_usage,
)
from infra.agent_log import set_task_dir, write_entry
from config import AgentConfig, default_config
from erc3 import ERC3, erc3 as dev, ApiException
from tools.wrappers import paginate_all

# Agents
from agents import entity_extractor, watchdog, guest_handler, solver
from agents.common import TaskContext, RoleResult, run_agent
# Agent configs for metrics
from agents.entity_extractor import agent_cfg as entity_cfg
from agents.watchdog import agent_cfg as watchdog_cfg
from agents.guest_handler import agent_cfg as guest_cfg
from agents.solver import agent_cfg as solver_cfg


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Run SGR Store Agent session")
    parser.add_argument(
        "task_code",
        nargs="?",
        type=str,
        help="Task code to run (e.g., t086). Overrides config.task_codes"
    )
    return parser.parse_args()

def get_git_commit() -> str:
    """Get current git commit hash (short form)"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""

def log_event(message: str):
    """Print timestamped event to stdout"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts}: {message}")

# Parse arguments and apply task_code override
args = parse_args()
config = default_config
if args.task_code:
    config.task_codes = [args.task_code]

core = ERC3()

# Track session start time
session_start_time = time.perf_counter()

# Start session with metadata
res = core.start_session(
    benchmark=config.benchmark,
    workspace=config.workspace,
    name=f"{config.session_name}",
    architecture=config.architecture,
    flags=config.session_flags,
)

log_event(f"Session started: {res.session_id}")

# Create session folder immediately (write logs directly, no copy at end)
session_dir_name = datetime.now(timezone.utc).strftime("%m%d-%H%M") + "-" + res.session_id
SESSION_DIR = Path("logs/sessions") / session_dir_name
SESSION_DIR.mkdir(parents=True, exist_ok=True)

# All log paths point to session folder
LOG_FILE = str(SESSION_DIR / "sess.json")
with open(LOG_FILE, "w", encoding="utf-8") as f:
    pass

# Raw LLM response log (for debugging parse issues)
RAW_LOG_FILE = str(SESSION_DIR / "raw_responses.log")
with open(RAW_LOG_FILE, "w", encoding="utf-8") as f:
    pass

# Write session_start event
write_json_event(LOG_FILE, {
    "type": "session_start",
    "session_id": res.session_id,
    "url": f"https://erc.timetoact-group.at/sessions/{res.session_id}",
    "commit": get_git_commit(),
})

# Log config event (for session analysis)
write_json_event(LOG_FILE, {
    "type": "config",
    **config.model_dump(),
})

status = core.session_status(res.session_id)

# Track scores and task_ids for session summary
task_scores = []
task_ids = []

for task in status.tasks:
    # Skip tasks not in task_codes (if filter is set)
    # Just continue without API calls - faster for local testing
    if config.task_codes and task.spec_id not in config.task_codes:
        continue

    # Skip tasks not matching task_name_filter (if filter is set)
    if config.task_name_filter and config.task_name_filter.lower() not in task.task_text.lower():
        continue

    # start the task
    print()  # Empty line before new task
    log_event(f"Task started: {task.spec_id}. {task.task_text}")
    core.start_task(task)
    task_ids.append(task.task_id)

    # Create task folder for all task-specific logs
    TASK_DIR = SESSION_DIR / task.spec_id
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    set_task_dir(TASK_DIR)

    # Initialize variables for post-task processing
    task_context = None

    # === GET API CLIENT AND WHOAMI ===
    api = core.get_erc_dev_client(task)
    store_api = api

    # Data dump: download API data before task processing
    if config.data_dump:
        from infra.data_dump import dump_task_data
        dump_task_data(store_api, TASK_DIR / "api_data", verbose=False)

    try:
        whoami = api.who_am_i()
        wiki_sha = whoami.wiki_sha1 if whoami else None
    except ApiException as e:
        log_event(f"ERROR: who_am_i() failed: {e}")
        print(f"[{task.spec_id}] who_am_i() ApiException: {e}")
        # Send error_internal response to API
        try:
            store_api.dispatch(dev.Req_ProvideAgentResponse(
                outcome="error_internal",
                message=f"System error: who_am_i() failed",
                links=[],
            ))
        except Exception:
            pass  # Response send failed, still need to finalize
        # Init fake telemetry (1 token) before finalize
        reset_task_usage("dummy")
        finalize_task(core, task, config, "server_error", time.perf_counter(), LOG_FILE)
        # Complete task in ERC3
        try:
            core.complete_task(task)
        except Exception:
            pass
        continue

    # === ORCHESTRATION: Guest Handler / Employee Flow ===
    task_started = time.perf_counter()
    task_stats = RoleResult(
        status="error",
        data={
            "duration_sec": 0,
            "tokens": {"prompt": 1, "completion": 1, "total": 2},
            "tokens_per_model": {},
        }
    )

    # Ensure wiki is downloaded and indexed
    if whoami and whoami.wiki_sha1:
        ensure_wiki(store_api, whoami.wiki_sha1)

    # Write task_start event (system event, before any agent actions)
    write_json_event(LOG_FILE, {
        "role": "system",
        "type": "task_start",
        "task_id": task.task_id,
        "spec_id": task.spec_id,
        "task_text": task.task_text,
        "who_am_i": whoami.model_dump() if whoami else None,
    })

    # Reset task-level token tracking (init with 1 token for telemetry)
    reset_task_usage("dummy")

    try:
        # === GUEST HANDLER (is_public=true) ===
        if whoami and whoami.is_public:
            log_event("Guest detected -> handling guest request")
            guest_context = TaskContext(
                task=task,
                whoami=whoami,
                log_file=LOG_FILE,
                core=core,
            )
            guest_role_result = run_agent(
                "guest_handler",
                guest_handler.run,
                guest_context,
                model_id=guest_cfg.MODEL_ID,
            )
            guest_result = guest_role_result.data  # Dict with allowed/answer/reason

            if guest_result.get("allowed") and guest_result.get("answer"):
                # Guest question allowed - send answer
                log_event(f"Guest ALLOWED: {guest_result['answer'][:50]}...")
                try:
                    store_api.dispatch(dev.Req_ProvideAgentResponse(
                        outcome="ok_answer",
                        message=guest_result["answer"],
                        links=[],
                    ))
                except Exception as e:
                    log_event(f"Failed to submit guest response: {e}")
                task_stats = finalize_task(core, task, config, "guest_answered", task_started, LOG_FILE)
            else:
                # Guest question denied - send refusal
                reason = guest_result.get("reason") or "This information is not available for public users."
                log_event(f"Guest DENIED: {reason}")
                try:
                    store_api.dispatch(dev.Req_ProvideAgentResponse(
                        outcome="denied_security",
                        message=reason,
                        links=[],
                    ))
                except Exception as e:
                    log_event(f"Failed to submit guest denial: {e}")
                task_stats = finalize_task(core, task, config, "guest_denied", task_started, LOG_FILE)

        # === EMPLOYEE FLOW (is_public=false) ===
        else:
            # Create TaskContext for all agents
            task_context = TaskContext(
                indent=2,  # Agent-level indent
                task=task,
                api=api,
                store_api=store_api,
                core=core,
                config=config,
                whoami=whoami,
                log_file=LOG_FILE,
            )

            # === ENTITY EXTRACTION ===
            entity_result = run_agent(
                "entity_extractor",
                entity_extractor.run,
                task_context,
                model_id=entity_cfg.MODEL_ID,
            )

            # Check entity extraction result
            if entity_result.status == "error":
                error_msg = entity_result.data.get("error", "Entity extraction failed") if entity_result.data else "Entity extraction failed"
                log_event(f"Entity extraction ERROR: {error_msg}")
                task_stats = finalize_task(core, task, config, "entity_extraction_error", task_started, LOG_FILE)
                continue  # Skip to next task

            # Write resolved_objects.jsonl (both security and solver objects)
            with open(TASK_DIR / "resolved_objects.jsonl", "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "security_objects", "data": task_context.security_objects}, ensure_ascii=False) + "\n")
                f.write(json.dumps({"type": "solver_objects", "data": task_context.solver_objects}, ensure_ascii=False) + "\n")

            # Security check
            security_result = run_agent(
                "watchdog",
                watchdog.run,
                task_context,
                model_id=watchdog_cfg.MODEL_ID,
            )


            # Handle security decisions
            if security_result.status == "deny":
                reason = security_result.data.get('decision', {}).get('reason', 'Security check failed')
                log_event(f"Security DENIED: {reason}")
                try:
                    store_api.dispatch(dev.Req_ProvideAgentResponse(
                        outcome="denied_security",
                        message=f"Security check failed: {reason}",
                        links=[],
                    ))
                except Exception as e:
                    log_event(f"Failed to submit refusal: {e}")
                task_stats = finalize_task(core, task, config, "security_denied", task_started, LOG_FILE)
    
            else:
                # === SOLVE TASK (allow or concerns) ===
                # entity_extractor already prepared solver_task_text/solver_objects:
                # - ExtInfo enrichment done
                # - Author removed from solver context (if not asking about self)
                log_event(f"Solver task text: {task_context.solver_task_text}")
                log_event(f"Security {security_result.status.upper()} -> solving task")

                # Set remaining context for solver
                task_context.raw_log_file = RAW_LOG_FILE
                task_context.task_started = task_started
                task_context.security_decision = security_result.data.get("decision") if security_result.data else None

                task_stats = run_agent(
                    "solver",
                    solver.run,
                    task_context,
                    model_id=solver_cfg.MODEL_ID,
                )

    except TaskTerminated as e:
        # Server error - response already sent
        log_event(f"Server error: {e}")
        task_stats = finalize_task(core, task, config, "server_error", task_started, LOG_FILE)
    except Exception as e:
        log_event(f"Unexpected error: {e}")
        print(e)

    # CLI colors
    CLI_RED = "\x1B[31m"
    CLI_CLR = "\x1B[0m"

    # Always complete task (even if response was already sent)
    try:
        result = core.complete_task(task)
        if result.eval:
            score = result.eval.score
            task_scores.append(score)
            log_event(f"Task completed: {task.spec_id} | Score: {score}")
            # Create status file (empty "success" or "fail")
            status_file = "success" if score > 0 else "fail"
            (TASK_DIR / status_file).touch()
            # Print eval logs if score is 0
            if score == 0 and result.eval.logs:
                print(f"{CLI_RED}[eval]{CLI_CLR} Score: 0 | Error: {result.eval.logs}")


            # Write task_end event with aggregated stats
            write_json_event(LOG_FILE, {
                "type": "task_end",
                "task_id": task.task_id,
                "spec_id": task.spec_id,
                "status": task_stats.status,
                "score": score,
                "eval_logs": result.eval.logs,
                "duration_sec": task_stats.data.get("duration_sec") if task_stats.data else None,
                "tokens": task_stats.data.get("tokens") if task_stats.data else None,
                "tokens_per_model": task_stats.data.get("tokens_per_model") if task_stats.data else None,
            })
            # Write task_result to agent_log
            write_entry("task_result", {
                "score": score,
                "eval_logs": result.eval.logs,
            })
        else:
            # Production mode: no eval available
            log_event(f"Task completed: {task.spec_id} | (no eval)")
            write_json_event(LOG_FILE, {
                "type": "task_end",
                "task_id": task.task_id,
                "spec_id": task.spec_id,
                "status": task_stats.status,
                "duration_sec": task_stats.data.get("duration_sec") if task_stats.data else None,
                "tokens": task_stats.data.get("tokens") if task_stats.data else None,
                "tokens_per_model": task_stats.data.get("tokens_per_model") if task_stats.data else None,
            })
    except Exception as e:
        log_event(f"Failed to complete task {task.spec_id}: {e}")
        print(f"{CLI_RED}[complete_task]{CLI_CLR} Error: {e}")

# Calculate total time
total_time_sec = time.perf_counter() - session_start_time

# Calculate session score
session_score = sum(task_scores) / len(task_scores) if task_scores else 0.0

# Write session_end event (before submit so it's in the log)
write_json_event(LOG_FILE, {
    "type": "session_end",
    "session_id": res.session_id,
    "total_time_sec": round(total_time_sec, 1),
    "task_scores": task_scores,
    "session_score": round(session_score, 3),
})

try:
    core.submit_session(res.session_id)
except Exception as e:
    log_event(f"Session submit failed: {e}, forcing...")
    core.submit_session(res.session_id, force=True)

if task_scores:
    log_event(f"Session completed: {res.session_id} | Score: {session_score:.1%}")
else:
    log_event(f"Session completed: {res.session_id} | (no eval scores)")

# === Write structured session summary to separate file ===
# Format: session start/end at root level, tasks at indent 1, task details at indent 2, logs at indent 3
STRUCTURED_LOG_FILE = Path(LOG_FILE).parent / "sess_serv.json"

structured_log = []

# Session start (no indent)
structured_log.append({
    "type": "session_start",
    "session_id": res.session_id,
    "benchmark": config.benchmark,
})

# Fetch and add task details
for task_id in task_ids:
    try:
        detail = core.task_detail(task_id)

        # Task start (indent 1)
        structured_log.append({
            "type": "task_start",
            "spec_id": detail.spec,
            "text": detail.text,
        })

        # Task details (indent 2) - exclude task_id, session_id, benchmark
        task_detail = {
            "type": "task_detail",
            "spec": detail.spec,
            "text": detail.text,
            "status": detail.status,
            "score": detail.score,
            "error_message": detail.error_message,
        }
        structured_log.append(task_detail)

        # Logs (indent 3)
        if detail.logs:
            for log_entry in detail.logs:
                structured_log.append({
                    "type": "task_log",
                    "log": log_entry if isinstance(log_entry, dict) else {"message": str(log_entry)},
                })

        # Task end (indent 1)
        structured_log.append({
            "type": "task_end",
            "spec_id": detail.spec,
            "score": detail.score,
        })
    except Exception as e:
        structured_log.append({
            "type": "task_error",
            "task_id": task_id,
            "error": str(e),
        })

# Session end (no indent)
structured_log.append({
    "type": "session_end",
    "session_id": res.session_id,
    "session_score": round(session_score, 3),
    "total_time_sec": round(total_time_sec, 1),
})

# Write with custom indentation based on type
def get_indent(entry_type: str) -> int:
    if entry_type in ("session_start", "session_end"):
        return 0
    elif entry_type in ("task_start", "task_end", "task_error"):
        return 1
    elif entry_type == "task_detail":
        return 2
    elif entry_type == "task_log":
        return 3
    return 0

with open(STRUCTURED_LOG_FILE, "w", encoding="utf-8") as f:
    f.write("[\n")
    for i, entry in enumerate(structured_log):
        indent = "  " * get_indent(entry.get("type", ""))
        json_str = json.dumps(entry, ensure_ascii=False)
        comma = "," if i < len(structured_log) - 1 else ""
        f.write(f"{indent}{json_str}{comma}\n")
    f.write("]\n")

# === Finalize sess.json (convert to valid JSON array) ===
from infra import finalize_json_array
finalize_json_array(LOG_FILE)

# === Create formatted copies ===
sess_file = Path(LOG_FILE)
sess_fmt_file = sess_file.parent / "sess_fmt.json"
if sess_file.exists():
    with open(sess_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    with open(sess_fmt_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

serv_fmt_file = STRUCTURED_LOG_FILE.parent / "sess_serv_fmt.json"
if STRUCTURED_LOG_FILE.exists():
    with open(STRUCTURED_LOG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    with open(serv_fmt_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# Convert JSONL to JSON array (sess_compl.json)
sess_compl_file = Path(LOG_FILE).parent / "sess_compl.json"
if sess_compl_file.exists():
    completions = []
    for line in sess_compl_file.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            completions.append(json.loads(line))
    sess_compl_file.write_text(
        json.dumps(completions, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

