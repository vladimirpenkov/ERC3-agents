"""
Common utilities for agents.

Provides:
- TaskContext: Global context passed between agents
- AgentRun: Unified metrics for agent execution
- SecurityDecision: Security decision from watchdog
- RoleResult: Result from agent execution
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from erc3 import TaskInfo


# Valid entity types for entities_to_change
EntityType = Literal["employee", "customer", "project", "wiki", "timeentry"]



def get_indent(context: "TaskContext") -> str:
    """Get indent string for console output."""
    return " " * context.indent


class AgentRun(BaseModel):
    """Metrics for a single agent execution.

    Same data that goes to telemetry.
    """
    agent: str  # Agent name (watchdog, entity_extractor, solver, etc.)
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    duration_sec: Optional[float] = None
    model_id: Optional[str] = None
    tokens_prompt: int = 0
    tokens_completion: int = 0
    tokens_total: int = 0
    tokens_cached: int = 0
    cost: float = 0.0  # Cost in credits (OpenRouter)
    steps: int = 0
    status: Optional[str] = None  # "success", "error", "timeout"
    error: Optional[str] = None


class SecurityDecision(BaseModel):
    """Security decision from watchdog.

    Four options for nuanced responses:
    - allow: Request doesn't break any rules, permitted
    - concerns: Allowed but with noted concerns
    - deny: Request breaks any rule, blocked
    - reconsider: Need more information

    entities_to_change specifies which entities can be modified.
    None or [] means read-only access.
    """
    reason: str = Field(..., max_length=500, description="Decision explanation")
    decision: Literal["allow", "concerns", "deny", "reconsider"]
    entities_to_change: Optional[List[EntityType]] = Field(
        default=None,
        description="Which entity types the task requires modifying. Examples: ['employee'] for salary update, ['wiki'] for page creation, ['timeentry'] for logging hours. None or [] if task is read-only (query, search, report)."
    )


class TaskContext(BaseModel):
    """Global context passed between agents.

    Structure:
    - INFRASTRUCTURE: Static config (set once at task start)
    - SHARED: Process data (filled by agents during execution)
    - AGENTS: Metrics per agent

    Data flow:
    1. entity_extractor → fills security_* and solver_* contexts
    2. watchdog → reads security_*, writes security_decision
    3. solver → reads solver_*, executes task
    """

    class Config:
        arbitrary_types_allowed = True

    # =========================================================================
    # INFRASTRUCTURE — set once at task start
    # =========================================================================
    indent: int = 0  # Console indent level (0=main, 2=agent)
    task: Optional[TaskInfo] = None
    api: Any = None  # ERC3 core client
    store_api: Any = None  # ERC3 DevClient for task operations
    core: Any = None  # ERC3 core instance for task completion
    config: Any = None  # AgentConfig
    whoami: Optional[Any] = None  # WhoAmI response (wiki_sha1, current_user, etc.)
    session_id: Optional[str] = None
    task_dir: Optional[Any] = None  # Path to task folder for per-task logs
    log_file: Optional[str] = None
    raw_log_file: Optional[str] = None  # Path to raw LLM response log (JSON Lines)
    task_started: Optional[float] = None  # Task start timestamp for timeout

    # =========================================================================
    # SHARED — process data, filled by agents
    # =========================================================================

    # Task metadata (from entity_extractor step 0)
    task_language: Optional[str] = None  # "English", "German", "Chinese", etc.
    task_expected_format: Optional[str] = None  # User's exact format requirement
    task_text_national: Optional[str] = None  # Original text before translation
    is_asking_about_self: bool = False  # True if requester asks about themselves

    # Security context (filled by entity_extractor, used by watchdog)
    # - task_text includes author prefix: "Requester {employee:id} asks: ..."
    # - objects use EmployeeSecurityView (limited fields for security check)
    security_task_text: Optional[str] = None
    security_objects: Dict[str, Dict[str, Any]] = {}

    # Solver context (filled by entity_extractor, used by solver)
    # - task_text: without author if not asking about self, original otherwise
    # - objects use EmployeeExtInfo (enriched data for task solving)
    # - unresolved_entities: terms that couldn't be matched to any known entity
    solver_task_text: Optional[str] = None
    solver_objects: Dict[str, Dict[str, Any]] = {}
    solver_unresolved: List[str] = []

    # Results
    security_decision: Optional[SecurityDecision] = None

    # Detected entities and systems (from entity_extractor, used by tiny_solver)
    detected_entities: List[str] = []  # ["employee", "project", "customer"]
    detected_systems: List[str] = []   # ["wiki", "timeentry", "workload"]

    # Debug info: accumulated step-by-step solution trace for debug agent
    debug_info_lines: List[str] = []

    # Solver messages: full conversation history for debug agent continuation
    solver_messages: List[Dict[str, Any]] = []

    # =========================================================================
    # AGENTS — metrics per agent
    # =========================================================================
    agents: Dict[str, AgentRun] = {}



class RoleResult(BaseModel):
    """Result from agent execution."""

    status: str  # "done", "error", "need_input", "blocked", "allow", "deny", "concerns"
    data: Any = None
    next_role: Optional[str] = None


def run_agent(
    agent_name: str,
    agent_func,
    context: TaskContext,
    model_id: Optional[str] = None,
    **kwargs
) -> Any:
    """Run agent function and record metrics in context.agents.

    Wraps agent execution with timing and token tracking.

    Args:
        agent_name: Name for metrics (e.g., "entity_extractor")
        agent_func: The run() function to call
        context: TaskContext to pass to agent
        model_id: Optional model ID to record (from agent_cfg.MODEL_ID)
        **kwargs: Additional arguments for agent_func

    Returns:
        Whatever agent_func returns

    Usage:
        from agents.common import run_agent
        from agents import entity_extractor
        from agents.entity_extractor import agent_cfg

        result = run_agent(
            "entity_extractor",
            entity_extractor.run,
            context,
            model_id=agent_cfg.MODEL_ID
        )
    """
    import time
    from infra.llm import get_task_usage

    # Snapshot usage before agent runs
    usage_before = get_task_usage()
    totals_before = {
        "prompt": sum(u.prompt for u in usage_before.values()),
        "completion": sum(u.completion for u in usage_before.values()),
        "total": sum(u.total for u in usage_before.values()),
        "cached": sum(u.cached_tokens for u in usage_before.values()),
        "cost": sum(u.cost for u in usage_before.values()),
        "duration": sum(u.duration_sec for u in usage_before.values()),
    }

    started_at = time.perf_counter()
    status = "success"
    error_msg = None

    try:
        result = agent_func(context, **kwargs)
        return result
    except Exception as e:
        status = "error"
        error_msg = str(e)
        raise
    finally:
        ended_at = time.perf_counter()

        # Compute deltas from accumulated usage
        usage_after = get_task_usage()
        totals_after = {
            "prompt": sum(u.prompt for u in usage_after.values()),
            "completion": sum(u.completion for u in usage_after.values()),
            "total": sum(u.total for u in usage_after.values()),
            "cached": sum(u.cached_tokens for u in usage_after.values()),
            "cost": sum(u.cost for u in usage_after.values()),
            "duration": sum(u.duration_sec for u in usage_after.values()),
        }

        tokens_prompt = totals_after["prompt"] - totals_before["prompt"]
        tokens_completion = totals_after["completion"] - totals_before["completion"]
        tokens_total = totals_after["total"] - totals_before["total"]
        tokens_cached = totals_after["cached"] - totals_before["cached"]
        cost_delta = totals_after["cost"] - totals_before["cost"]
        duration_delta = totals_after["duration"] - totals_before["duration"]

        # Record metrics
        context.agents[agent_name] = AgentRun(
            agent=agent_name,
            started_at=started_at,
            ended_at=ended_at,
            duration_sec=duration_delta,
            model_id=model_id,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            tokens_total=tokens_total,
            tokens_cached=tokens_cached,
            cost=cost_delta,
            status=status,
            error=error_msg,
        )

        # Debug output with proper indent
        cost_str = f"${cost_delta:.4f}" if cost_delta > 0 else "-"
        cached_str = f" (cached:{tokens_cached})" if tokens_cached > 0 else ""
        indent_str = " " * context.indent
        print(f"{indent_str}\x1B[90m[{agent_name}] {duration_delta:.1f}s | {tokens_total}tok{cached_str} | {cost_str}\x1B[0m")
