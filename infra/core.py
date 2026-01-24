"""Common utilities shared across agents"""

import json
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple


# =============================================================================
# File operations with retry
# =============================================================================

def safe_file_append(
    file_path: Path | str,
    content: str,
    max_attempts: int = 3,
    delay: float = 0.1
) -> bool:
    """
    Safely append content to file with retry on concurrent access errors.

    Args:
        file_path: Path to file
        content: Content to append
        max_attempts: Maximum number of retry attempts
        delay: Delay in seconds between retries

    Returns:
        True if successful, False otherwise
    """
    file_path = Path(file_path)

    for attempt in range(max_attempts):
        try:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(content)
            return True
        except (IOError, OSError) as e:
            if attempt < max_attempts - 1:
                time.sleep(delay)
            else:
                print(f"Warning: Could not append to {file_path} after {max_attempts} attempts: {e}")
                return False
    return False


def write_json_event(log_file: str | None, event: dict) -> None:
    """
    Write a JSON event to session log.

    Each event is written as a single line with automatic timestamp.
    Format: comma-separated JSON objects (finalized to array at end).

    Args:
        log_file: Path to log file (None = no-op)
        event: Event dict to write
    """
    if log_file:
        from datetime import datetime
        event["_ts"] = datetime.now().isoformat()
        safe_file_append(log_file, json.dumps(event, ensure_ascii=False) + ",\n")


def finalize_json_array(log_file: str | None) -> None:
    """
    Convert session log from comma-separated to valid JSON array.

    Transforms file content from:
        {"event": 1},
        {"event": 2},
    To:
        [{"event": 1},{"event": 2}]

    Args:
        log_file: Path to log file (None = no-op)
    """
    if not log_file:
        return

    file_path = Path(log_file)
    if not file_path.exists():
        return

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Remove trailing comma and whitespace, wrap in brackets
        content = content.rstrip(',\n \t')
        content = '[' + content + ']'

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        print(f"Warning: Could not finalize {log_file}: {e}")


# =============================================================================
# Wiki operations
# =============================================================================

# Wiki directories (relative to agent root)
WIKI_ROOT = Path(__file__).parent.parent / "wiki" / "companies"
INDEX_ROOT = WIKI_ROOT / "indexes"


def ensure_wiki(api: Any, wiki_sha: str) -> bool:
    """
    Ensure wiki is downloaded and indexed.

    Checks if wiki exists locally. If not:
    1. Downloads all wiki files from API
    2. Creates _meta.txt with list of files
    3. Indexes them with txtai

    Args:
        api: ERC3 dev API client (from core.get_erc_dev_client())
        wiki_sha: Wiki identifier

    Returns:
        True if wiki is ready, False on error
    """
    if not wiki_sha:
        return False

    wiki_dir = WIKI_ROOT / wiki_sha
    index_dir = INDEX_ROOT / wiki_sha
    meta_file = wiki_dir / "_meta.txt"

    # Check if already indexed
    if index_dir.exists():
        return True

    # Check if downloaded but not indexed
    if wiki_dir.exists():
        try:
            from .wiki_rag import index_wiki
            count = index_wiki(wiki_sha)
            print(f"[wiki] Indexed {count} chunks for {wiki_sha}")
            return True
        except Exception as e:
            print(f"[wiki] Index error: {e}")
            return False

    # Need to download wiki
    print(f"[wiki] Downloading wiki {wiki_sha}...")
    try:
        # Get list of wiki files
        wiki_list = api.list_wiki()

        if wiki_list.sha1 != wiki_sha:
            print(f"[wiki] Warning: API returned sha1={wiki_list.sha1}, expected {wiki_sha}")

        # Create directory
        wiki_dir.mkdir(parents=True, exist_ok=True)

        # Download each file
        md_files = []
        for file_path in wiki_list.paths:
            try:
                content = api.load_wiki(file_path)
                local_path = wiki_dir / file_path
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_text(content.content, encoding="utf-8")
                if file_path.endswith(".md"):
                    md_files.append(file_path)
            except Exception as e:
                print(f"[wiki] Error downloading {file_path}: {e}")

        # Create _meta.txt with list of md files
        meta_file.write_text("\n".join(sorted(md_files)), encoding="utf-8")

        print(f"[wiki] Downloaded {len(wiki_list.paths)} files")

        # Index the wiki
        from .wiki_rag import index_wiki
        count = index_wiki(wiki_sha)
        print(f"[wiki] Indexed {count} chunks")

        return True

    except Exception as e:
        print(f"[wiki] Download error: {e}")
        return False


# =============================================================================
# Task termination on server errors
# =============================================================================

class TaskTerminated(Exception):
    """Raised when task is terminated due to server error.

    The error has already been logged and response sent to API.
    Caller should finalize the task without additional actions.
    """
    def __init__(self, method: str, status: int, error: str):
        self.method = method
        self.status = status
        self.error = error
        super().__init__(f"Task terminated: {method} returned {status}: {error}")


def handle_api_error(
    e: Exception,
    method: str,
    store_api: Any,
    log_file: str | None,
    core: Any = None,
    task: Any = None,
) -> None:
    """
    Handle API error - log, send telemetry, send response, raise TaskTerminated.

    For 404 errors (not found) - does nothing, returns normally.
    For other errors (5xx, 4xx except 404) - logs error, sends telemetry,
    sends error_internal response to API, and raises TaskTerminated.

    Args:
        e: Exception from API call (ApiException or other)
        method: API method name for logging
        store_api: ERC3 dev API client for sending response
        log_file: Path to log file
        core: ERC3 core instance (optional, for task completion and telemetry)
        task: TaskInfo (optional, for task completion and telemetry)

    Raises:
        TaskTerminated: If error is not 404 (server error)
    """
    from erc3 import ApiException, erc3 as dev

    # CLI colors
    CLI_RED = "\x1B[31m"
    CLI_YELLOW = "\x1B[33m"
    CLI_CLR = "\x1B[0m"

    # Extract status and error message
    if isinstance(e, ApiException):
        status = getattr(e.api_error, 'status', 500) if hasattr(e, 'api_error') else 500
        error_msg = str(e.api_error.error) if hasattr(e, 'api_error') else str(e)
    else:
        status = 500
        error_msg = str(e)

    # 404 = not found, this is normal
    if status == 404:
        return

    # Log error
    write_json_event(log_file, {
        "role": "system",
        "type": "api_error",
        "status": status,
        "method": method,
        "error": error_msg,
    })

    # NOTE: Telemetry (token logging) is NOT sent here.
    # Caller must call _finalize_task() which handles all telemetry centrally.

    # Prepare response
    outcome = "error_internal"
    message = f"Server error in {method}: {error_msg}"

    # Print response being sent
    print(f"{CLI_RED}[server_error]{CLI_CLR} Sending response: outcome={outcome}, message={message}")

    # Send response
    try:
        store_api.dispatch(dev.Req_ProvideAgentResponse(
            outcome=outcome,
            message=message,
            links=[],
        ))
    except Exception as send_err:
        print(f"{CLI_RED}[server_error]{CLI_CLR} Failed to send response: {send_err}")

    # Complete task and check result (if core and task provided)
    score = None
    eval_logs = None
    if core and task:
        try:
            result = core.complete_task(task)
            if result.eval:
                score = result.eval.score
                eval_logs = result.eval.logs
                if score == 0:
                    print(f"{CLI_RED}[server_error]{CLI_CLR} Score: 0 | Error: {eval_logs}")
                else:
                    print(f"{CLI_YELLOW}[server_error]{CLI_CLR} Score: {score}")
        except Exception as complete_err:
            print(f"{CLI_RED}[server_error]{CLI_CLR} Failed to complete task: {complete_err}")

    # Log task end
    write_json_event(log_file, {
        "role": "system",
        "type": "task_end",
        "reason": "server_error",
        "status": status,
        "method": method,
        "error": error_msg,
        "score": score,
        "eval_logs": eval_logs,
    })

    # Raise to signal task termination (with score info)
    exc = TaskTerminated(method, status, error_msg)
    exc.score = score
    exc.eval_logs = eval_logs
    exc.task_completed = core is not None and task is not None
    raise exc


# =============================================================================
# Utilities
# =============================================================================

def filter_none(d):
    """
    Recursively filter out None values from a dict or list.

    Args:
        d: Dict or list to filter

    Returns:
        New dict/list without None values
    """
    if isinstance(d, dict):
        return {
            k: filter_none(v)
            for k, v in d.items()
            if v is not None
        }
    elif isinstance(d, list):
        return [filter_none(item) for item in d]
    else:
        return d


def make_resolved_key(entity_type: str, entity_id: str) -> str:
    """Create resolved_objects key: 'employee:abc123'"""
    return f"{entity_type}:{entity_id}"


# =============================================================================
# Task finalization
# =============================================================================

def finalize_task(
    api: Any,
    task: Any,
    config: Any,
    status: str,
    task_started: float,
    log_file: Optional[str] = None,
    links: Optional[List[Tuple[str, str]]] = None,
):
    """
    Finalize task execution: log LLM usage per model and return stats.

    This function is called when a task completes (success, timeout, error).
    It logs usage to ERC3 for each model used during the task.

    Args:
        api: ERC3 core client
        task: TaskInfo object
        config: AgentConfig
        status: Completion status (completed, timeout, error, etc.)
        task_started: Task start timestamp
        log_file: Path to log file (optional)
        links: List of (type, id) tuples for entity links (optional)

    Returns:
        RoleResult with status and execution stats in data
    """
    from agents.common import RoleResult
    from .llm import get_task_usage

    # Log links to file
    if log_file and links:
        write_json_event(log_file, {
            "role": "system",
            "type": "task_links",
            "links": [{"type": t, "id": i} for t, i in links],
        })
        for link_type, link_id in links:
            print(f"  - link {link_type}: {link_id}")

    task_duration = time.perf_counter() - task_started

    # Get per-model usage from llm_client
    task_usage = get_task_usage()

    # Calculate total tokens across all models
    total_tokens = {
        "prompt": sum(u.prompt for u in task_usage.values()),
        "completion": sum(u.completion for u in task_usage.values()),
        "total": sum(u.total for u in task_usage.values()),
    }

    # If no LLM calls were made, send fake telemetry
    # Server requires at least one log_llm call per task
    if total_tokens["total"] <= 2:  # Only init tokens (1+1)
        model_id = "dummy"
        api.log_llm(
            task_id=task.task_id,
            completion="solved without LLM",
            model=model_id,
            duration_sec=task_duration,
            prompt_tokens=1,
            completion_tokens=1,
            cached_prompt_tokens=0,
        )

    return RoleResult(
        status=status,
        data={
            "duration_sec": round(task_duration, 1),
            "tokens": total_tokens,
            "tokens_per_model": {m: {"prompt": u.prompt, "completion": u.completion, "total": u.total}
                                for m, u in task_usage.items()},
        }
    )
