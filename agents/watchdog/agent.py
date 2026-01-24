"""
Watchdog - security agent.

Pipeline:
1. Fast check (regex) — immediate deny for obvious violations
2. Policy check (LLM) — apply policies to enriched task context

Returns: allow / deny decision.
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Literal, Optional, Union, Any

from pydantic import BaseModel, Field

from erc3 import erc3 as dev

from agents.common import TaskContext, RoleResult, SecurityDecision
from tools.dtos import Search_TimeEntries
from tools.wrappers import search_time_entries
from . import agent_cfg
from .prompts import build_prompt, build_user_message
from infra import llm_call, write_json_event, filter_none
from infra.agent_log import write_entry


AGENT_NAME = "watchdog"
MAX_POLICY_STEPS = 3  # Max steps for policy check


# CLI colors
CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_YELLOW = "\x1B[33m"
CLI_BLUE = "\x1B[34m"
CLI_CLR = "\x1B[0m"


# =============================================================================
# Watchdog-specific tool: check if requester is Lead of a project
# =============================================================================

class Check_Requester_Is_Lead(BaseModel):
    """Check if requester is Lead of the specified project.

    Use when access rule requires "requester has role=Lead in project.team".
    """
    tool: Literal["Check_Requester_Is_Lead"] = "Check_Requester_Is_Lead"
    requester_id: str = Field(..., description="Employee ID of the requester")
    project_id: str = Field(..., description="Project ID to check")


class PolicyStep(BaseModel):
    """Policy check step — make security decision."""
    # Reasoning
    situation_understanding: str = Field(..., max_length=1000, description="Current understanding of the situation - briefly")
    related_rules: List[str] = Field(default_factory=list, description="Which rules govern this query?")
    data_to_check: List[str] = Field(default_factory=list, description="must you check anything more?")

    # Action: either make a decision or use a tool
    action: Union[
        Check_Requester_Is_Lead,
        Search_TimeEntries,
        SecurityDecision,
    ] = Field(..., description="Use tool or make security decision")


def run(context: TaskContext) -> RoleResult:
    """
    Security policy check (LLM-based).

    Reads from context:
        - security_task_text: Task text with author prefix and {type:id} tags
        - security_objects: Dict of resolved entities with SecurityView data

    Returns:
        RoleResult with status "allow", "deny", or "concerns"
    """
    return _check_policies_v2(context, agent_cfg.MODEL_ID)


def _check_requester_is_lead(api: Any, requester_id: str, project_id: str) -> bool:
    """Check if requester is Lead of the specified project."""
    try:
        resp = api.dispatch(dev.Req_GetProject(id=project_id))
        if resp.project:
            for member in resp.project.team or []:
                if member.employee == requester_id and member.role == "Lead":
                    return True
    except Exception:
        pass
    return False


def _dispatch_tool(action: Any, context: TaskContext) -> str:
    """Dispatch tool call and return result as string."""
    try:
        if isinstance(action, Check_Requester_Is_Lead):
            is_lead = _check_requester_is_lead(
                api=context.api,
                requester_id=action.requester_id,
                project_id=action.project_id,
            )
            return f"DONE: {is_lead}"
        elif isinstance(action, Search_TimeEntries):
            result = search_time_entries(context.api, action)
            return result.model_dump_json()
        else:
            return f"ERROR: Unknown tool: {action.__class__.__name__}"
    except Exception as e:
        return f"ERROR: {str(e)}"


def _check_policies_v2(
    context: TaskContext,
    model_id: str,
    extra_body: Optional[Dict[str, Any]] = None,
) -> RoleResult:
    """Check policies using security context (security_task_text + security_objects).

    Args:
        context: TaskContext with security_task_text and security_objects
        model_id: LLM model ID for policy check
        extra_body: Optional extra params for LLM call (e.g., reasoning config)
    """
    wiki_sha = context.whoami.wiki_sha1 if context.whoami else None

    # Build task_summary — only task_text and resolved_objects (for prompt)
    # User info is embedded in task_text: "Requester {employee:id} (department) asks: ..."
    task_summary = {
        "task_text": context.security_task_text,
        "resolved_objects": context.security_objects,
    }

    # Load security rules from data/ (static)
    rulebook_file = context.config.policy_rulebook if context.config else "security_rules.txt"
    rulebook_is_json = rulebook_file.endswith(".json")
    rules_path = Path(__file__).parent.parent.parent / "data" / rulebook_file
    rulebook_content = rules_path.read_text(encoding="utf-8") if rules_path.exists() else ""

    # Filter None values for cleaner output
    task_summary = filter_none(task_summary)

    # Build messages optimized for prompt caching
    system_prompt = build_prompt(rulebook=rulebook_content, is_json=rulebook_is_json)
    user_message = build_user_message(
        parsed_task_json=json.dumps(task_summary, indent=2, ensure_ascii=False),
        task_text=context.task.task_text,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    # Log task summary for debugging
    write_json_event(context.log_file, {
        "role": "system",
        "type": "step_debug",
        "task_summary": task_summary,
    })

    print(f"  {CLI_BLUE}[policy]{CLI_CLR} Checking policies...")

    # Merge agent's extra_body with any passed extra_body
    effective_extra_body = {**agent_cfg.EXTRA_BODY, **(extra_body or {})}

    start_time = time.perf_counter()
    for i in range(MAX_POLICY_STEPS):
        step_start = time.perf_counter()
        result = llm_call(
            model_id=model_id,
            messages=messages,
            response_format=PolicyStep,
            temperature=agent_cfg.TEMPERATURE,
            max_tokens=agent_cfg.MAX_COMPLETION_TOKENS,
            log_file=context.log_file,
            task_id=context.task.task_id if context.task else None,
            erc3_api=context.core,
            extra_body=effective_extra_body,
        )
        step_duration = time.perf_counter() - step_start

        # Log LLM call to agent log
        write_entry("watchdog", {
            "step": i + 1,
            "type": "llm_call",
            "messages": messages,
            "response": result.parsed.model_dump() if result.parsed else None,
            "error": result.error,
            "stats": {
                "model": model_id,
                "tokens_prompt": result.usage.prompt if result.usage else 0,
                "tokens_completion": result.usage.completion if result.usage else 0,
                "tokens_total": result.usage.total if result.usage else 0,
                "tokens_cached": result.usage.cached_tokens if result.usage else 0,
                "cost": result.usage.cost if result.usage else 0,
                "duration_sec": round(step_duration, 2),
            }
        })

        if result.error:
            decision = SecurityDecision(
                reason=f"Policy check failed: {result.error}"[:500],
                decision="deny",
            )
            return RoleResult(status="deny", data={"decision": decision.model_dump()})

        policy_step = result.parsed
        if not policy_step:
            decision = SecurityDecision(
                reason="Policy check returned empty result",
                decision="deny",
            )
            return RoleResult(status="deny", data={"decision": decision.model_dump()})

        # Handle decision (action is SecurityDecision)
        if isinstance(policy_step.action, SecurityDecision):
            decision_data = policy_step.action
            status_map = {"allow": "allow", "deny": "deny", "concerns": "concerns"}
            status = status_map.get(decision_data.decision, "deny")

            # Log decision
            write_json_event(context.log_file, {
                "role": AGENT_NAME,
                "type": "policy_decision",
                "decision": decision_data.model_dump(),
            })

            # Print decision with elapsed time
            elapsed = time.perf_counter() - start_time
            color = CLI_GREEN if status == "allow" else CLI_RED if status == "deny" else CLI_YELLOW
            print(f"  {color}[{status.upper()}]{CLI_CLR} [takes {elapsed:.1f}s] {decision_data.reason}")

            return RoleResult(
                status=status,
                data={
                    "decision": decision_data.model_dump(),
                    "task_context": task_summary,
                }
            )

        # Handle tool calls - dispatch and add result
        action = policy_step.action
        tool_name = action.__class__.__name__

        # Log tool call for debugging
        write_json_event(context.log_file, {
            "role": AGENT_NAME,
            "type": "tool_call",
            "tool": tool_name,
            "args": action.model_dump() if hasattr(action, 'model_dump') else str(action),
            "step": i + 1,
        })

        tool_result = _dispatch_tool(action, context)

        # Add assistant message and tool result
        messages.append({"role": "assistant", "content": json.dumps(policy_step.model_dump())})
        messages.append({"role": "user", "content": f"Tool result:\n{tool_result}"})
        continue

    # Max steps exceeded
    decision = SecurityDecision(
        reason="Policy check exceeded maximum steps",
        decision="deny",
    )
    return RoleResult(status="deny", data={"decision": decision.model_dump()})
