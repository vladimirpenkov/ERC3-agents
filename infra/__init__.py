"""Infrastructure utilities shared across agents."""

from .core import (
    # Task finalization
    finalize_task,
    # File operations
    safe_file_append,
    write_json_event,
    finalize_json_array,
    # Wiki
    ensure_wiki,
    WIKI_ROOT,
    INDEX_ROOT,
    # Error handling
    TaskTerminated,
    handle_api_error,
    # Utilities
    filter_none,
    make_resolved_key,
)

from .llm import (
    # LLM calling
    llm_call,
    reset_task_usage,
    get_task_usage,
    LLMResult,
    TokenUsage,
)


__all__ = [
    # Core
    "finalize_task",
    "safe_file_append",
    "write_json_event",
    "finalize_json_array",
    "ensure_wiki",
    "WIKI_ROOT",
    "INDEX_ROOT",
    "TaskTerminated",
    "handle_api_error",
    "filter_none",
    "make_resolved_key",
    # LLM
    "llm_call",
    "reset_task_usage",
    "get_task_usage",
    "LLMResult",
    "TokenUsage",
]
