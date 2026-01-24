"""Solver agent - main task execution loop with tools."""

import json
import time
from typing import Annotated, Any, Dict, List, Optional, Union

from annotated_types import MaxLen
from pydantic import BaseModel, Field
from erc3 import erc3 as dev, ApiException, TaskInfo, ERC3

from config import AgentConfig
from .prompts import build_system_prompt
from infra import llm_call
from tools.dtos import (
    AgentResponse,
    # GET wrappers
    Get_Customer, Get_Employees, Get_TimeEntry, Get_Project,
    # Time wrappers
    Add_TimeEntry, Get_TimeSummaryByEmployee, Get_TimeSummaryByProject,
    Search_TimeEntries, Update_TimeEntry,
    # UPDATE wrappers
    Update_ProjectTeam, Change_Project_Status, Update_EmployeeInfo, Batch_Update_Employees,
    # Wiki tools
    Search_Wiki_With_Page, List_Wiki_Pages, Get_Wiki_Page, Delete_Wiki, Update_Wiki, Rename_Wiki, Create_Wiki_Pages,
    # Search wrappers (hide pagination from LLM)
    Req_SearchProjects, Req_SearchEmployees, Req_SearchCustomers,
    # Workload
    Get_Employees_Workload,
    # Project leads
    Get_Project_Leads,
    # Current employee
    Get_CurrentEmployee,
)
from tools.wrappers import (
    search_wiki, list_wiki_pages, get_wiki_page, delete_wiki, update_employee_info, batch_update_employees,
    get_project,
    # Search wrappers
    search_projects, search_employees, search_customers,
    search_time_entries, update_time_entry,
    # Workload
    get_employees_workload,
    # Batch employee fetch
    get_employees,
    # Project leads
    get_project_leads,
    # Wiki
    rename_wiki,
    create_wiki_pages,
    # Current employee
    get_current_employee,
)
from infra import write_json_event, finalize_task
from infra.agent_log import write_entry
from agents.common import TaskContext, RoleResult
from . import agent_cfg


# CLI colors
CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_YELLOW = "\x1B[33m"
CLI_BLUE = "\x1B[34m"
CLI_CLR = "\x1B[0m"


def _is_complex_task(task_id: str) -> bool:
    """Check if task is in complex tasks list."""
    complex_cfg = getattr(agent_cfg, 'COMPLEX_TASKS', None)
    return bool(complex_cfg and task_id in complex_cfg.get("task_ids", []))


def _get_effective_model_config(task_id: str) -> Dict[str, Any]:
    """Get model config based on task complexity.

    If task_id is in COMPLEX_TASKS.task_ids, use the complex model.
    Otherwise, use the default model from agent_cfg.
    """
    complex_cfg = getattr(agent_cfg, 'COMPLEX_TASKS', None)
    if complex_cfg and task_id in complex_cfg.get("task_ids", []):
        return complex_cfg["model"]
    return {
        "model_id": agent_cfg.MODEL_ID,
        "temperature": agent_cfg.TEMPERATURE,
        "max_completion_tokens": agent_cfg.MAX_COMPLETION_TOKENS,
        "extra_body": getattr(agent_cfg, 'EXTRA_BODY', {}),
    }


def _get_effective_timeout(task_id: str, default_timeout: float) -> float:
    """Get timeout for task. Complex tasks may have extended timeout."""
    complex_cfg = getattr(agent_cfg, 'COMPLEX_TASKS', None)
    if complex_cfg and task_id in complex_cfg.get("task_ids", []):
        return complex_cfg.get("timeout_sec", default_timeout)
    return default_timeout


class NextStep(BaseModel):
    """LLM response format for solver agent."""
    previous_step_error_if_exists: str = Field(..., description="Check 'error' of the previous step")
    current_state: str = Field(..., description="what is the current state of your answering?")

    is_task_completed_or_unable_to_accomplish: bool
    # we'll use only the first step, discarding all the rest.
    # MaxLen(10) in schema to be forgiving, but we truncate to 5 after parsing
    plan_remaining_steps_brief: Annotated[List[str], MaxLen(10)] = Field(default_factory=list, description="Think carefully and briefly explain your thoughts on how to accomplish - what steps to execute")
    # now let's continue the cascade and check with LLM if the task is done
    # Routing to one of the tools to execute the first remaining step
    # discriminator='tool' ensures LLM picks the right tool type
    function: Annotated[
        Union[
            AgentResponse,
            # Search wrappers (hide pagination)
            Req_SearchEmployees,
            Req_SearchCustomers,
            Req_SearchProjects,
            # GET wrappers
            Get_Customer,
            Get_Employees,  # Batch fetch (replaces Get_Employee)
            Get_Project,
            Get_TimeEntry,
            # Time wrappers
            Add_TimeEntry,
            Search_TimeEntries,
            Update_TimeEntry,
            Get_TimeSummaryByProject,
            Get_TimeSummaryByEmployee,
            # UPDATE wrappers
            Update_ProjectTeam,
            Change_Project_Status,
            Update_EmployeeInfo,
            Batch_Update_Employees,
            # Wiki tools
            Search_Wiki_With_Page,
            List_Wiki_Pages,
            Get_Wiki_Page,
            Delete_Wiki,
            Update_Wiki,
            Rename_Wiki,
            Create_Wiki_Pages,
            # Workload
            Get_Employees_Workload,
            # Project leads
            Get_Project_Leads,
            # Current employee
            Get_CurrentEmployee,
        ],
        Field(discriminator='tool')
    ]


def run(context: TaskContext) -> RoleResult:
    """
    Solve task using LLM loop with tools.

    Reads from context:
        - task, api, store_api, config, whoami
        - log_file, raw_log_file, task_started
        - solver_task_text, solver_objects (entity-extracted and enriched)
        - security_decision (optional)

    Returns:
        RoleResult with status and execution stats in data
    """
    return _solve_task_impl(
        task=context.task,
        api=context.api,
        store_api=context.store_api,
        core=context.core,
        config=context.config,
        log_file=context.log_file,
        raw_log_file=context.raw_log_file,
        task_started=context.task_started,
        about=context.whoami,
        formatted_task_text=context.solver_task_text,
        resolved_objects=context.solver_objects,
        unresolved_entities=context.solver_unresolved,
    )


def _solve_task_impl(
    task: TaskInfo,
    api: ERC3,
    store_api: ERC3,
    core: Any,  # ERC3 instance for log_llm
    config: AgentConfig,
    log_file: str,
    raw_log_file: str,
    task_started: float,
    about: Any,  # WhoAmI result
    formatted_task_text: Optional[str] = None,
    resolved_objects: Optional[Dict[str, Any]] = None,
    unresolved_entities: Optional[List[str]] = None,
) -> RoleResult:
    """Internal implementation of task solving."""
    # === MAIN AGENT PIPELINE ===
    # System prompt with filled placeholders from data files
    system_prompt = build_system_prompt()

    # Build user message: Today + Task + Objects
    task_text = formatted_task_text or task.task_text
    today = getattr(about, 'today', None) or ""

    if resolved_objects:
        objects_json = json.dumps(resolved_objects, indent=2, ensure_ascii=False)
        user_message = f"Today: {today}\n\nTask: {task_text}\n\n## OBJECTS RESOLVED from the task:\n{objects_json}"
    else:
        user_message = f"Today: {today}\n\nTask: {task_text}"

    # Add hint about unresolved entities if any
    if unresolved_entities:
        unresolved_list = "\n".join(f"- {term}" for term in unresolved_entities)
        user_message += f"""

## [SYSTEM HINT] UNRESOLVED TERMS
The entity extractor could not find database matches for the following terms:
{unresolved_list}

*Instruction:* Review these terms.
- If an unresolved term is a **Specific Name/ID** of the entity required for the task (e.g., a Customer for comparison), treat it as a missing entity -> `unclear_term_need_clarification`.
- If it is a **Pattern**, **General Term** or descriptor - ignore this warning and proceed."""

    log = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    # Log initial context - EVERYTHING that r2d2 receives
    write_json_event(log_file, {
        "role": "r2d2",
        "type": "context_start",
        "system_prompt": system_prompt,
        "user_message": user_message,
    })

    # Get model config (may use stronger model for complex tasks)
    model_config = _get_effective_model_config(task.spec_id)
    task_timeout = _get_effective_timeout(task.spec_id, config.task_timeout_sec)

    # let's limit number of reasoning steps by 10
    for i in range(10):
        # Check timeout
        if time.perf_counter() - task_started > task_timeout:
            print(f"TIMEOUT: task exceeded {task_timeout}s limit")
            return finalize_task(api, task, config, "timeout", task_started)
        step = f"step_{i + 1}"
        print(f"{CLI_GREEN}Next {step}...{CLI_CLR} ", end="")

        started = time.perf_counter()

        # Call LLM via unified wrapper
        llm_result = llm_call(
            model_id=model_config["model_id"],
            messages=log,
            response_format=NextStep,
            temperature=model_config["temperature"],
            max_tokens=model_config["max_completion_tokens"],
            log_file=log_file,
            raw_log_file=raw_log_file,
            task_id=task.task_id,
            erc3_api=core,
            extra_body=model_config.get("extra_body"),
        )

        step_duration = time.perf_counter() - started

        # Check if call failed
        if not llm_result.success:
            if "Rate limit" in (llm_result.error or ""):
                print(f"{CLI_RED}RATE_LIMIT_EXHAUSTED{CLI_CLR}: {llm_result.error}")
                return finalize_task(api, task, config, "rate_limit_exhausted", task_started)
            print(f"{CLI_RED}LLM_ERROR{CLI_CLR}: {llm_result.error}")
            write_json_event(log_file, {
                "role": "r2d2",
                "type": "step",
                "step_num": i + 1,
                "error": {"type": "llm_error", "error": llm_result.error},
            })
            continue

        # Track step tokens for logging
        step_tokens = {
            "prompt": llm_result.usage.prompt if llm_result.usage else 0,
            "completion": llm_result.usage.completion if llm_result.usage else 0,
            "total": llm_result.usage.total if llm_result.usage else 0,
        }

        # Log LLM call to agent log
        write_entry("solver", {
            "step": i + 1,
            "type": "llm_call",
            "messages": log,
            "response": llm_result.parsed.model_dump() if llm_result.parsed else None,
            "stats": {
                "model": model_config["model_id"],
                "tokens_prompt": step_tokens["prompt"],
                "tokens_completion": step_tokens["completion"],
                "tokens_total": step_tokens["total"],
                "tokens_cached": llm_result.usage.cached_tokens if llm_result.usage else 0,
                "cost": llm_result.usage.cost if llm_result.usage else 0,
                "duration_sec": round(step_duration, 2),
            }
        })

        elapsed_sec = time.perf_counter() - task_started

        # Get parsed result (already validated as non-None by llm_result.success check above)
        job: NextStep = llm_result.parsed  # type: ignore[assignment]

        # Truncate plan to 5 steps (schema allows 10 to be forgiving, but we only use first step anyway)
        if len(job.plan_remaining_steps_brief) > 5:
            job.plan_remaining_steps_brief = job.plan_remaining_steps_brief[:5]

        # Get first step or fallback to current_state if plan is empty (task completed)
        first_step = job.plan_remaining_steps_brief[0] if job.plan_remaining_steps_brief else job.current_state

        # print next step for debugging (exclude None values)
        tool_name = job.function.__class__.__name__
        tool_params_dict = {k: v for k, v in job.function.model_dump().items() if v is not None and k != "tool"}
        tool_params_str = " ".join(f"{k}={v!r}" for k, v in tool_params_dict.items())
        print(first_step, f"\n  tool='{tool_name}' {tool_params_str}")

        # Let's add tool request to conversation history as if OpenAI asked for it.
        log.append({
            "role": "assistant",
            "content": first_step,
            "tool_calls": [{
                "type": "function",
                "id": step,
                "function": {
                    "name": job.function.__class__.__name__,
                    "arguments": job.function.model_dump_json(),
                }}]
        })

        # now execute the tool by dispatching command to our handler
        step_error = None
        result_data = None
        txt = ""
        links: List[dev.AgentLink] = []

        try:
            result = None  # Will be set by wrapper functions, None for safe_dispatch

            # Handle wiki tools locally (not via API)
            if isinstance(job.function, Search_Wiki_With_Page):
                result = search_wiki(about.wiki_sha1, job.function)
            elif isinstance(job.function, List_Wiki_Pages):
                result = list_wiki_pages(about.wiki_sha1)
            elif isinstance(job.function, Get_Wiki_Page):
                result = get_wiki_page(about.wiki_sha1, job.function)
            elif isinstance(job.function, Delete_Wiki):
                result = delete_wiki(store_api, job.function.file, job.function.user_id)
            elif isinstance(job.function, Update_Wiki):
                result = store_api.dispatch(dev.Req_UpdateWiki(
                    file=job.function.file,
                    content=job.function.content,
                    changed_by=job.function.changed_by,
                ))
            elif isinstance(job.function, Rename_Wiki):
                result = rename_wiki(store_api, job.function)
            elif isinstance(job.function, Create_Wiki_Pages):
                result = create_wiki_pages(store_api, job.function)
            # Search wrappers
            elif isinstance(job.function, Req_SearchProjects):
                result = search_projects(store_api, job.function)
            elif isinstance(job.function, Req_SearchEmployees):
                result = search_employees(store_api, job.function)
            elif isinstance(job.function, Req_SearchCustomers):
                result = search_customers(store_api, job.function)
            # Smart wrappers
            elif isinstance(job.function, Get_Project):
                result = get_project(store_api, job.function)
            # GET wrappers -> dispatch to dev.Req_*
            elif isinstance(job.function, Get_Customer):
                result = store_api.dispatch(dev.Req_GetCustomer(id=job.function.company_id))
            elif isinstance(job.function, Get_Employees):
                # Batch fetch employees with optional field selection
                result = get_employees(
                    api=store_api,
                    request=job.function,
                    store_api=store_api,
                    log_file=log_file,
                    core=api,
                    task=task,
                )
            elif isinstance(job.function, Get_TimeEntry):
                result = store_api.dispatch(dev.Req_GetTimeEntry(id=job.function.time_entry_id))
            # Time wrappers -> dispatch to dev.Req_*
            elif isinstance(job.function, Add_TimeEntry):
                result = store_api.dispatch(dev.Req_LogTimeEntry(
                    employee=job.function.employee,
                    customer=job.function.customer,
                    project=job.function.project,
                    date=job.function.date,
                    hours=job.function.hours,
                    work_category=job.function.work_category,
                    notes=job.function.notes,
                    billable=job.function.billable,
                    status=job.function.status,
                    logged_by=job.function.logged_by,
                ))
            elif isinstance(job.function, Search_TimeEntries):
                result = search_time_entries(store_api, job.function)
            elif isinstance(job.function, Update_TimeEntry):
                result = update_time_entry(store_api, job.function)
            elif isinstance(job.function, Get_TimeSummaryByEmployee):
                result = store_api.dispatch(dev.Req_TimeSummaryByEmployee(
                    date_from=job.function.date_from,
                    date_to=job.function.date_to,
                    employees=job.function.employees or [],
                    customers=job.function.customers or [],
                    projects=job.function.projects or [],
                    billable=job.function.billable or "",
                ))
            elif isinstance(job.function, Get_TimeSummaryByProject):
                result = store_api.dispatch(dev.Req_TimeSummaryByProject(
                    date_from=job.function.date_from,
                    date_to=job.function.date_to,
                    employees=job.function.employees or [],
                    customers=job.function.customers or [],
                    projects=job.function.projects or [],
                    billable=job.function.billable or "",
                ))
            # UPDATE wrappers -> dispatch to dev.Req_*
            elif isinstance(job.function, Update_ProjectTeam):
                result = store_api.dispatch(dev.Req_UpdateProjectTeam(
                    id=job.function.project_id,
                    team=job.function.team,
                    changed_by=job.function.changed_by,
                ))
            elif isinstance(job.function, Change_Project_Status):
                result = store_api.dispatch(dev.Req_UpdateProjectStatus(
                    id=job.function.project_id,
                    status=job.function.status,
                    changed_by=job.function.changed_by,
                ))
            elif isinstance(job.function, Update_EmployeeInfo):
                # PATCH-style update: wrapper fetches current data and merges
                result = update_employee_info(store_api, job.function)
            elif isinstance(job.function, Batch_Update_Employees):
                result = batch_update_employees(store_api, job.function)
            elif isinstance(job.function, Get_Employees_Workload):
                result = get_employees_workload(store_api, job.function)
            elif isinstance(job.function, Get_Project_Leads):
                result = get_project_leads(store_api)
            elif isinstance(job.function, Get_CurrentEmployee):
                result = get_current_employee(store_api, about)
            elif isinstance(job.function, AgentResponse):
                # Map agent-friendly outcome to API outcome before dispatch
                from tools.dtos import OUTCOME_TO_API
                api_outcome = OUTCOME_TO_API.get(job.function.outcome, job.function.outcome)
                # Use agent's links directly (no auto-collection), deduplicate
                # Clear links if outcome is ok_not_found (nothing to link to)
                seen_links = set()
                if job.function.outcome != 'ok_not_found' and job.function.requested_links:
                    for lnk in job.function.requested_links:
                        link_key = (lnk.entity_type, lnk.entity_id)
                        if link_key not in seen_links:
                            seen_links.add(link_key)
                            links.append(dev.AgentLink(kind=lnk.entity_type, id=lnk.entity_id))
                result = store_api.dispatch(dev.Req_ProvideAgentResponse(
                    message=job.function.message,
                    outcome=api_outcome,
                    links=links,
                ))
            else:
                # Fallback: unknown tool type - should not happen
                txt = f'ERROR: Unknown tool type: {type(job.function).__name__}'
                print(f"{CLI_RED}ERR: {txt}{CLI_CLR}")
                step_error = {"type": "unknown_tool", "error": txt}

            # Process result for non-safe_dispatch branches
            if result is not None:
                if isinstance(result, dict):
                    # Plain dict result (e.g., from delete_wiki)
                    txt = json.dumps(result)
                    result_data = result
                    # Use INFO: prefix for business logic responses (not server errors)
                    # Agent should handle these as informational, not as errors
                    if result.get("success") is False and result.get("needs_clarification"):
                        print(f"{CLI_YELLOW}INFO{CLI_CLR}: {txt}")
                        txt = "INFO: " + txt
                    else:
                        print(f"OUT: {txt}")
                        txt = "DONE: " + txt
                else:
                    # Pydantic model result
                    txt = result.model_dump_json(exclude_none=True, exclude_unset=True)
                    result_data = json.loads(txt)
                    print(f"OUT: {txt}")
                    txt = "DONE: " + txt
        except ApiException as e:
            txt = "ERROR: " + e.detail
            print(f"{CLI_RED}ERR: {e.api_error.error}{CLI_CLR}")
            step_error = {"type": "api_error", "error": e.api_error.error}
        except Exception as e:
            txt = f'ERROR: {{"error": "{str(e)}"}}'
            print(f"{CLI_RED}ERR: {str(e)}{CLI_CLR}")
            step_error = {"type": "exception", "error": str(e)}

        # Log tool execution to agent log
        write_entry("solver", {
            "step": i + 1,
            "type": "tool_call",
            "tool": job.function.__class__.__name__,
            "args": json.loads(job.function.model_dump_json()),
            "result": result_data,
            "error": step_error,
        })

        # Log step event
        write_json_event(log_file, {
            "role": "r2d2",
            "type": "step",
            "step_num": i + 1,
            "prev_err": job.previous_step_error_if_exists,
            "current_state": job.current_state,
            "plan": job.plan_remaining_steps_brief,
            "task_completed": job.is_task_completed_or_unable_to_accomplish,
            "function": job.function.__class__.__name__,
            "args": json.loads(job.function.model_dump_json()),
            "tool_response": txt,
            "result": result_data,
            "error": step_error,
            "elapsed_sec": round(elapsed_sec, 1),
            "step_duration_sec": round(step_duration, 1),
            "tokens": step_tokens,
        })

        # Check if agent finished work (AgentResponse)
        if isinstance(job.function, AgentResponse):
            print(f"{CLI_BLUE}agent {job.function.outcome}{CLI_CLR}. Summary:\n{job.function.message}")
            # Use actual links sent to API (matches what was dispatched)
            agent_links = [(lnk.kind, lnk.id) for lnk in links]
            return finalize_task(api, task, config, "completed", task_started, log_file, agent_links)

        # Add results back to the conversation history
        log.append({"role": "tool", "content": txt, "tool_call_id": step})

    # If we reach here, all steps were exhausted without completion
    return finalize_task(api, task, config, "max_steps_exceeded", task_started)
