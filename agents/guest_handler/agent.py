"""Guest handler — answers public questions about locations, departments, date."""

import json
import time
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from infra import llm_call, write_json_event
from infra.agent_log import write_entry
from agents.common import TaskContext, RoleResult
from . import agent_cfg
from .prompts import build_prompt


class GuestResponse(BaseModel):
    """Response for guest questions."""
    allowed: bool = Field(..., description="True if question is about allowed topic")
    answer: Optional[str] = Field(None, description="Answer if allowed, formatted per question")
    reason: Optional[str] = Field(None, description="Reason if not allowed")


def _load_json(filename: str) -> list:
    """Load JSON file from data/ directory (static reference data)."""
    path = Path(__file__).parent.parent.parent / "data" / filename
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return []


def run(context: TaskContext) -> RoleResult:
    """
    Handle guest (public user) request.

    Reads from context:
        - task.task_text: Question text
        - whoami.today: Current date
        - log_file, core

    Returns:
        RoleResult with GuestResponse in data
    """
    response = _handle_guest_impl(
        task_text=context.task.task_text,
        today=context.whoami.today if context.whoami else "",
        log_file=context.log_file,
        core=context.core,
        task_id=context.task.task_id if context.task else None,
    )

    return RoleResult(
        status="allowed" if response.allowed else "denied",
        data=response.model_dump(),
    )


def _handle_guest_impl(
    task_text: str,
    today: str,
    log_file: str = None,
    core=None,
    task_id: str = None,
) -> GuestResponse:
    """Internal implementation of guest request handling."""
    # Load reference data (static from data/)
    locations = _load_json("locations.json")
    departments = _load_json("departments.json")

    # Build prompt
    system_prompt = build_prompt(locations, departments)

    # Build user message with date prefix (like other agents)
    user_message = f"Today, {today}. Guest asks:\n{task_text}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    # Single LLM call
    step_start = time.perf_counter()
    result = llm_call(
        model_id=agent_cfg.MODEL_ID,
        messages=messages,
        response_format=GuestResponse,
        temperature=agent_cfg.TEMPERATURE,
        max_tokens=agent_cfg.MAX_COMPLETION_TOKENS,
        log_file=log_file,
        erc3_api=core,
        task_id=task_id,
    )
    step_duration = time.perf_counter() - step_start

    # Log to agent_log
    write_entry("guest_handler", {
        "step": 1,
        "type": "llm_call",
        "messages": messages,
        "response": result.parsed.model_dump() if result.parsed else None,
        "error": result.error,
        "stats": {
            "model": agent_cfg.MODEL_ID,
            "tokens_prompt": result.usage.prompt if result.usage else 0,
            "tokens_completion": result.usage.completion if result.usage else 0,
            "tokens_total": result.usage.total if result.usage else 0,
            "tokens_cached": result.usage.cached_tokens if result.usage else 0,
            "cost": result.usage.cost if result.usage else 0,
            "duration_sec": round(step_duration, 2),
        }
    })

    if result.error or not result.parsed:
        response = GuestResponse(
            allowed=False,
            reason=f"Error processing request: {result.error or 'empty response'}",
        )
    else:
        response = result.parsed

    # Log step
    if log_file:
        write_json_event(log_file, {
            "role": "guest_handler",
            "type": "step",
            "step_num": 1,
            "task_text": task_text,
            "allowed": response.allowed,
            "answer": response.answer,
            "reason": response.reason,
            "tokens": result.usage.total if result.usage else 0,
        })

    return response
