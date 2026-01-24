"""
Dump API data for a task.

Main function: dump_task_data(api, output_dir) - for use from main.py.
"""

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict

from erc3 import erc3 as dev

from tools.wrappers import paginate_all


def _load_employees(api, log: Callable[[str], None]) -> list:
    """Load all employees with full details."""
    t0 = time.perf_counter()
    log("Loading employees...")
    summaries = paginate_all(api, dev.Req_ListEmployees, 'employees')
    t1 = time.perf_counter()
    log(f"  {len(summaries)} found ({t1-t0:.1f}s), fetching details...")

    employees = []
    for emp in summaries:
        try:
            result = api.dispatch(dev.Req_GetEmployee(id=emp.id))
            if result.employee:
                employees.append(result.employee.model_dump())
        except Exception as e:
            log(f"  ERROR {emp.id}: {e}")

    t2 = time.perf_counter()
    log(f"  {len(employees)} loaded ({t2-t1:.1f}s)")
    return employees


def _load_projects(api, log: Callable[[str], None]) -> list:
    """Load all projects with full details."""
    t0 = time.perf_counter()
    log("Loading projects...")
    summaries = paginate_all(api, dev.Req_SearchProjects, 'projects', query='', include_archived=True)
    t1 = time.perf_counter()
    log(f"  {len(summaries)} found ({t1-t0:.1f}s), fetching details...")

    projects = []
    for proj in summaries:
        try:
            result = api.dispatch(dev.Req_GetProject(id=proj.id))
            if result.project:
                projects.append(result.project.model_dump())
        except Exception as e:
            log(f"  ERROR {proj.id}: {e}")

    t2 = time.perf_counter()
    log(f"  {len(projects)} loaded ({t2-t1:.1f}s)")
    return projects


def _load_customers(api, log: Callable[[str], None]) -> list:
    """Load all customers with full details."""
    t0 = time.perf_counter()
    log("Loading customers...")
    summaries = paginate_all(api, dev.Req_ListCustomers, 'companies')
    t1 = time.perf_counter()
    log(f"  {len(summaries)} found ({t1-t0:.1f}s), fetching details...")

    customers = []
    for cust in summaries:
        try:
            result = api.dispatch(dev.Req_GetCustomer(id=cust.id))
            if result.company:
                customers.append(result.company.model_dump())
        except Exception as e:
            log(f"  ERROR {cust.id}: {e}")

    t2 = time.perf_counter()
    log(f"  {len(customers)} loaded ({t2-t1:.1f}s)")
    return customers


def _load_time_entries(api, log: Callable[[str], None]) -> list:
    """Load all time entries (date filter 2020-2030)."""
    t0 = time.perf_counter()
    log("Loading time entries...")

    try:
        entries = paginate_all(
            api, dev.Req_SearchTimeEntries, 'entries',
            date_from="2020-01-01", date_to="2030-12-31"
        )
        all_entries = [e.model_dump() for e in entries]
    except Exception as ex:
        log(f"  ERROR: {ex}")
        all_entries = []

    t1 = time.perf_counter()
    log(f"  {len(all_entries)} loaded ({t1-t0:.1f}s)")
    return all_entries


def dump_task_data(
    api,
    output_dir: Path,
    verbose: bool = False
) -> Dict[str, Any]:
    """
    Load all API data for current task and save to output_dir.

    Args:
        api: ERC3 API client (from core.get_erc_dev_client)
        output_dir: Directory to save JSON files
        verbose: Print progress to stdout

    Returns:
        Dict with counts: {employees, projects, customers, time_entries, elapsed}
    """
    t0 = time.perf_counter()

    def log(msg: str):
        if verbose:
            print(msg)

    output_dir.mkdir(parents=True, exist_ok=True)

    # Load all entities
    employees = _load_employees(api, log)
    projects = _load_projects(api, log)
    customers = _load_customers(api, log)
    time_entries = _load_time_entries(api, log)

    # Save to files
    (output_dir / "employees.json").write_text(
        json.dumps(employees, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "projects.json").write_text(
        json.dumps(projects, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "customers.json").write_text(
        json.dumps(customers, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "time_entries.json").write_text(
        json.dumps(time_entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    elapsed = time.perf_counter() - t0
    log(f"Saved to {output_dir} (total: {elapsed:.1f}s)")

    return {
        "employees": len(employees),
        "projects": len(projects),
        "customers": len(customers),
        "time_entries": len(time_entries),
        "elapsed": elapsed,
    }
