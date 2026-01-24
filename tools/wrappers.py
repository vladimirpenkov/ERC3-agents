"""Combo tool implementations - wrappers over erc3.erc3 (dev) API"""

from typing import Any, List, Optional

from erc3 import erc3 as dev

from infra import WIKI_ROOT, INDEX_ROOT
from .dtos import (
    Search_Wiki_With_Page, Resp_Search_Wiki_With_Page, WikiSearchResult,
    List_Wiki_Pages, Resp_List_Wiki_Pages,
    Get_Wiki_Page, Resp_Get_Wiki_Page,
    Get_Wiki_Headers, Resp_Get_Wiki_Headers,
    Get_Wiki_Fragments, Resp_Get_Wiki_Fragments, WikiFragment,
    Delete_Wiki,
    Get_Project, Resp_GetProject, ProjectDetailView, ProjectTeamMember,
    # Pagination wrappers
    Req_ListProjects, Req_ListEmployees, Req_ListCustomers,
    Req_SearchProjects, Req_SearchEmployees, Req_SearchCustomers,
    FilterMost, FilterLeast, FilterSpecific, Criterion,
    Resp_ListProjects, Resp_ListEmployees, Resp_ListCustomers,
    Resp_SearchProjects, Resp_SearchEmployees, Resp_SearchCustomers,
    # Time entries
    Search_TimeEntries, Resp_SearchTimeEntries,
    Update_TimeEntry,
    # Workload
    Get_Employees_Workload, Resp_Get_Employees_Workload, EmployeeWorkload, WorkloadScope,
    # Project leads
    Get_Project_Leads, Resp_Get_Project_Leads,
    # Wiki rename
    Rename_Wiki,
    # Batch employee fetch
    Get_Employees, Resp_Get_Employees,
)
from tools.employee import EmployeeExtInfo, build_employee_ext_info


def list_wiki_pages(wiki_sha: str) -> Resp_List_Wiki_Pages:
    """
    List all available wiki pages.

    Reads from _meta.txt in wiki directory.

    Args:
        wiki_sha: Wiki identifier from whoami.wiki_sha

    Returns:
        Resp_List_Wiki_Pages with pages list or error
    """
    if not wiki_sha:
        return Resp_List_Wiki_Pages(success=False, error="wiki_sha not provided")

    meta_file = WIKI_ROOT / wiki_sha / "_meta.txt"

    if not meta_file.exists():
        return Resp_List_Wiki_Pages(success=False, error=f"_meta.txt not found for wiki {wiki_sha}")

    try:
        pages = meta_file.read_text(encoding="utf-8")
        return Resp_List_Wiki_Pages(success=True, pages=pages)
    except Exception as e:
        return Resp_List_Wiki_Pages(success=False, error=str(e))


def get_wiki_page(wiki_sha: str, request: Get_Wiki_Page) -> Resp_Get_Wiki_Page:
    """
    Get content of a specific wiki page.

    Args:
        wiki_sha: Wiki identifier from whoami.wiki_sha
        request: Request with page_path

    Returns:
        Resp_Get_Wiki_Page with content or error
    """
    if not wiki_sha:
        return Resp_Get_Wiki_Page(success=False, error="wiki_sha not provided")

    page_path = WIKI_ROOT / wiki_sha / request.page_path

    if not page_path.exists():
        return Resp_Get_Wiki_Page(
            success=False,
            error=f"Page not found: {request.page_path}"
        )

    try:
        content = page_path.read_text(encoding="utf-8")
        return Resp_Get_Wiki_Page(success=True, content=content)
    except Exception as e:
        return Resp_Get_Wiki_Page(success=False, error=str(e))


def get_wiki_headers(request: Get_Wiki_Headers) -> Resp_Get_Wiki_Headers:
    """
    Get only headers (##, ###, ####) from a wiki page.

    Args:
        request: Request with wiki_sha and page_path

    Returns:
        Resp_Get_Wiki_Headers with list of headers
    """
    if not request.wiki_sha:
        return Resp_Get_Wiki_Headers(success=False, error="wiki_sha not provided")

    page_path = WIKI_ROOT / request.wiki_sha / request.page_path

    if not page_path.exists():
        return Resp_Get_Wiki_Headers(
            success=False,
            error=f"Page not found: {request.page_path}"
        )

    try:
        content = page_path.read_text(encoding="utf-8")
        headers = []
        for line in content.splitlines():
            stripped = line.strip()
            # Match ##, ###, #### at start of line (not # which is title)
            if stripped.startswith("## ") or stripped.startswith("### ") or stripped.startswith("#### "):
                headers.append(stripped)
        return Resp_Get_Wiki_Headers(success=True, headers=headers)
    except Exception as e:
        return Resp_Get_Wiki_Headers(success=False, error=str(e))


def _get_header_level(header: str) -> int:
    """Get markdown header level (2 for ##, 3 for ###, 4 for ####)."""
    if header.startswith("#### "):
        return 4
    elif header.startswith("### "):
        return 3
    elif header.startswith("## "):
        return 2
    return 0


def get_wiki_fragments(request: Get_Wiki_Fragments) -> Resp_Get_Wiki_Fragments:
    """
    Get content of specific sections from a wiki page.

    Rules:
    - If ## is selected: return everything until next ## (including ###, ####)
    - If ### is selected: return everything until next ### or ##
    - If #### is selected: return everything until next ####, ###, or ##

    Args:
        request: Request with wiki_sha, page_path and headers list

    Returns:
        Resp_Get_Wiki_Fragments with content of each requested section
    """
    if not request.wiki_sha:
        return Resp_Get_Wiki_Fragments(success=False, error="wiki_sha not provided")

    page_path = WIKI_ROOT / request.wiki_sha / request.page_path

    if not page_path.exists():
        return Resp_Get_Wiki_Fragments(
            success=False,
            error=f"Page not found: {request.page_path}"
        )

    try:
        content = page_path.read_text(encoding="utf-8")
        lines = content.splitlines()

        # Build index of all headers with their line numbers
        header_positions = []  # [(line_num, header_text, level), ...]
        for i, line in enumerate(lines):
            stripped = line.strip()
            level = _get_header_level(stripped)
            if level > 0:
                header_positions.append((i, stripped, level))

        fragments = []

        for requested_header in request.headers:
            requested_header_stripped = requested_header.strip()
            requested_level = _get_header_level(requested_header_stripped)

            if requested_level == 0:
                # Invalid header format
                fragments.append(WikiFragment(
                    header=requested_header,
                    content=f"[Invalid header format: {requested_header}]"
                ))
                continue

            # Find this header in positions
            start_line = None
            start_idx = None
            for idx, (line_num, header_text, level) in enumerate(header_positions):
                if header_text == requested_header_stripped:
                    start_line = line_num
                    start_idx = idx
                    break

            if start_line is None:
                fragments.append(WikiFragment(
                    header=requested_header,
                    content=f"[Header not found: {requested_header}]"
                ))
                continue

            # Find end: next header of same or higher level (lower number)
            end_line = len(lines)  # default to end of file
            for next_idx in range(start_idx + 1, len(header_positions)):
                _, _, next_level = header_positions[next_idx]
                if next_level <= requested_level:
                    end_line = header_positions[next_idx][0]
                    break

            # Extract content (including the header line itself)
            fragment_lines = lines[start_line:end_line]
            fragment_content = "\n".join(fragment_lines).strip()

            fragments.append(WikiFragment(
                header=requested_header_stripped,
                content=fragment_content
            ))

        return Resp_Get_Wiki_Fragments(success=True, fragments=fragments)

    except Exception as e:
        return Resp_Get_Wiki_Fragments(success=False, error=str(e))


# =============================================================================
# Pagination helper
# =============================================================================

# Error message when server pagination is completely broken (max_limit = -1)
# IMPORTANT: This message must clearly indicate SERVER ERROR so agent uses 'server_error' outcome
SERVER_SYSTEM_ERROR = "SERVER ERROR: The server API is broken and not responding correctly. Use outcome 'server_error' when reporting this."


class ServerSystemError(Exception):
    """Raised when server API is broken (e.g., pagination disabled)."""
    pass


def paginate_all(
    api: Any,
    request_class: type,
    items_field: str,
    **request_kwargs
) -> list:
    """
    Fetch all items with automatic pagination and limit discovery.

    Automatically finds the maximum working page limit via binary search.
    Handles "page limit exceeded" errors by reducing limit.

    Args:
        api: ERC3 API client
        request_class: Request class (e.g., dev.Req_ListProjects)
        items_field: Name of list field in response ('projects', 'employees', etc.)
        **request_kwargs: Other request params (e.g., query for search)

    Returns:
        Complete list of all items

    Raises:
        ServerSystemError: If server pagination is completely broken
    """
    from erc3 import ApiException

    all_items = []
    offset = 0
    working_limit = 0  # Last known working limit
    current_limit = 2  # Start small
    limit_locked = False  # True when exact limit is found (no more exploration)

    while True:
        try:
            request = request_class(offset=offset, limit=current_limit, **request_kwargs)
            response = api.dispatch(request)
            items = getattr(response, items_field) or []
            all_items.extend(items)

            # Success — remember working limit
            working_limit = current_limit

            # If got less than requested — no more data
            if len(items) < current_limit:
                break

            offset += len(items)

            # Only try to increase limit if we haven't locked it yet
            if not limit_locked:
                current_limit = current_limit * 2

        except ApiException as e:
            error_msg = str(e.api_error.error) if hasattr(e, 'api_error') else str(e)
            if "page limit exceeded" in error_msg.lower():
                # If even limit=1 fails — server is broken
                if current_limit == 1:
                    raise ServerSystemError(SERVER_SYSTEM_ERROR)

                if working_limit > 0:
                    # We hit the limit — lock it, no more exploration
                    limit_locked = True
                    # Binary search between working_limit and current_limit
                    current_limit = working_limit + (current_limit - working_limit) // 2
                    if current_limit <= working_limit:
                        # Binary search converged
                        current_limit = working_limit
                else:
                    # Haven't found working limit yet — halve
                    current_limit = max(1, current_limit // 2)
            else:
                raise  # Different error — propagate

    return all_items


# =============================================================================
# Pagination wrappers - hide pagination from LLM
# =============================================================================

def list_projects(api: Any, request: Req_ListProjects) -> Resp_ListProjects:
    """List all projects with automatic pagination."""
    try:
        projects = paginate_all(api, dev.Req_ListProjects, 'projects')
        return Resp_ListProjects(success=True, projects=projects)
    except ServerSystemError as e:
        return Resp_ListProjects(success=False, error_message=str(e))


def list_employees(api: Any, request: Req_ListEmployees) -> Resp_ListEmployees:
    """List all employees with automatic pagination."""
    try:
        employees = paginate_all(api, dev.Req_ListEmployees, 'employees')
        return Resp_ListEmployees(success=True, employees=employees)
    except ServerSystemError as e:
        return Resp_ListEmployees(success=False, error_message=str(e))


def list_customers(api: Any, request: Req_ListCustomers) -> Resp_ListCustomers:
    """List all customers with automatic pagination."""
    try:
        companies = paginate_all(api, dev.Req_ListCustomers, 'companies')
        return Resp_ListCustomers(success=True, companies=companies)
    except ServerSystemError as e:
        return Resp_ListCustomers(success=False, error_message=str(e))


def search_projects(api: Any, request: Req_SearchProjects) -> Resp_SearchProjects:
    """Search projects with automatic pagination.

    NOTE: Server API only searches in 'name' field, not in 'id'.
    This wrapper fetches all projects (with team filter if specified)
    and searches query substring in both 'id' and 'name' locally.
    Always includes archived projects (include_archived=True).
    """
    try:
        kwargs = {"include_archived": True}

        # Add customer_id filter if specified
        if request.customer_id:
            kwargs['customer_id'] = request.customer_id

        # Add status filter if specified
        if request.status:
            kwargs['status'] = request.status

        # Build team filter if any participant constraints specified
        if request.known_participant or request.participant_role or request.participant_min_time_slice:
            kwargs['team'] = dev.ProjectTeamFilter(
                employee_id=request.known_participant,
                role=request.participant_role,
                min_time_slice=request.participant_min_time_slice or 0.0
            )

        # Fetch all projects (with team filter, but without query - we filter locally)
        all_projects = paginate_all(api, dev.Req_SearchProjects, 'projects', **kwargs)

        # Filter locally: search substring in id and name (case-insensitive)
        query_lower = request.name_or_id_substring.lower() if request.name_or_id_substring else ""
        if query_lower:
            filtered = [
                p for p in all_projects
                if query_lower in p.id.lower() or query_lower in p.name.lower()
            ]
        else:
            filtered = all_projects

        return Resp_SearchProjects(success=True, projects=filtered)
    except ServerSystemError as e:
        return Resp_SearchProjects(success=False, error_message=str(e))


# Bellini skill/will level scale (hardcoded)
SKILL_LEVEL_SCALE = {
    "Very low": [1, 2],
    "Basic": [3, 4],
    "Solid": [5, 6],
    "Strong": [7, 8],
    "Exceptional": [9, 10],
}


def _levels_to_range(names: Optional[List[str]]) -> tuple:
    """Convert level names to (min_level, max_level) range. Returns (1, 10) if None."""
    if not names:
        return (1, 10)
    all_levels = []
    for name in names:
        if name in SKILL_LEVEL_SCALE:
            all_levels.extend(SKILL_LEVEL_SCALE[name])
    if not all_levels:
        return (1, 10)  # fallback to all levels
    return (min(all_levels), max(all_levels))


def _get_criterion_mode(c: Optional[Criterion]) -> str:
    """Extract mode from criterion. Returns 'NONE' if no criterion."""
    if c is None:
        return "NONE"
    return c.mode


def search_employees(api: Any, request: Req_SearchEmployees) -> Resp_SearchEmployees:
    """Search employees with filtering and optional sorting.

    Modes:
    - MOST: Iterative search 10→1, returns all tied for best level
    - LEAST: Iterative search 1→10, returns all tied for worst level
    - SPECIFIC: Filter by specified levels, returns all matching
    - NONE: No skill/will filter, just base filters
    """
    try:
        # 1. Build base filters
        base_kwargs = {}
        if request.name_or_id_substring:
            base_kwargs['query'] = request.name_or_id_substring
        if request.location:
            base_kwargs['location'] = request.location
        if request.department:
            base_kwargs['department'] = request.department

        # 2. Determine primary mode (skill takes precedence)
        skill_mode = _get_criterion_mode(request.skill)
        will_mode = _get_criterion_mode(request.will)

        # Primary criterion for iteration (skill first, then will)
        if skill_mode in ("MOST", "LEAST"):
            primary_mode = skill_mode
            is_skill_primary = True
        elif will_mode in ("MOST", "LEAST"):
            primary_mode = will_mode
            is_skill_primary = False
        else:
            primary_mode = "NONE"
            is_skill_primary = True  # doesn't matter

        # 3. Get level ranges for SPECIFIC filters
        def get_filter_range(c: Optional[Criterion]) -> tuple:
            if c is None:
                return (1, 10)
            if isinstance(c, FilterSpecific):
                return _levels_to_range(c.levels)
            return (1, 10)

        skill_range = get_filter_range(request.skill)
        will_range = get_filter_range(request.will)

        # --- STRATEGY A: Iterative search (MOST or LEAST) ---
        if primary_mode in ("MOST", "LEAST"):
            # Determine iteration range
            if is_skill_primary:
                start, end = 1, 10
            else:
                start, end = 1, 10

            # MOST: 10→1, LEAST: 1→10
            levels_to_check = range(10, 0, -1) if primary_mode == "MOST" else range(1, 11)

            found_brief = []

            for level in levels_to_check:
                kwargs = base_kwargs.copy()

                # Build SKILL filter
                if request.skill:
                    if is_skill_primary:
                        # Primary: fixed level
                        kwargs['skills'] = [dev.SkillFilter(
                            name=request.skill.name, min_level=level, max_level=level
                        )]
                    else:
                        # Secondary: use its range (CERTAIN) or wide (MOST/LEAST)
                        min_l, max_l = skill_range
                        kwargs['skills'] = [dev.SkillFilter(
                            name=request.skill.name, min_level=min_l, max_level=max_l
                        )]

                # Build WILL filter
                if request.will:
                    if not is_skill_primary:
                        # Primary: fixed level
                        kwargs['wills'] = [dev.SkillFilter(
                            name=request.will.name, min_level=level, max_level=level
                        )]
                    else:
                        # Secondary: use its range (CERTAIN) or wide (MOST/LEAST)
                        min_l, max_l = will_range
                        kwargs['wills'] = [dev.SkillFilter(
                            name=request.will.name, min_level=min_l, max_level=max_l
                        )]

                # Query API
                batch = paginate_all(api, dev.Req_SearchEmployees, 'employees', **kwargs)

                if batch:
                    found_brief.extend(batch)
                    # Stop at first found level (all ties for best/worst)
                    break

            # MOST/LEAST: return EmployeeBrief (no need for full profile)
            return Resp_SearchEmployees(success=True, employees=found_brief)

        # --- STRATEGY B: Filter only (SPECIFIC or NONE) ---
        else:
            kwargs = base_kwargs.copy()

            if request.skill:
                min_l, max_l = skill_range
                kwargs['skills'] = [dev.SkillFilter(
                    name=request.skill.name, min_level=min_l, max_level=max_l
                )]

            if request.will:
                min_l, max_l = will_range
                kwargs['wills'] = [dev.SkillFilter(
                    name=request.will.name, min_level=min_l, max_level=max_l
                )]

            employees = paginate_all(api, dev.Req_SearchEmployees, 'employees', **kwargs)

            # Only SPECIFIC mode needs full profile (to show filtered skill/will levels)
            is_specific = skill_mode == "SPECIFIC" or will_mode == "SPECIFIC"
            if is_specific:
                # Collect filter names
                filter_skills = {request.skill.name} if request.skill else set()
                filter_wills = {request.will.name} if request.will else set()

                found_filtered = []
                for brief in employees:
                    try:
                        resp = api.dispatch(dev.Req_GetEmployee(id=brief.id))
                        if resp and resp.employee:
                            emp = resp.employee
                            # Filter skills/wills to only those in filter
                            filtered_skills = [s for s in emp.skills if s.name in filter_skills]
                            filtered_wills = [w for w in emp.wills if w.name in filter_wills]
                            # Create new EmployeeView with filtered lists
                            filtered_emp = dev.EmployeeView(
                                id=emp.id,
                                name=emp.name,
                                email=emp.email,
                                salary=emp.salary,
                                notes=emp.notes,
                                location=emp.location,
                                department=emp.department,
                                skills=filtered_skills,
                                wills=filtered_wills,
                            )
                            found_filtered.append(filtered_emp)
                    except Exception:
                        pass
                return Resp_SearchEmployees(success=True, employees=found_filtered)

            # No SPECIFIC filter: return EmployeeBrief
            return Resp_SearchEmployees(success=True, employees=employees)

    except ServerSystemError as e:
        return Resp_SearchEmployees(success=False, error_message=str(e))


def search_customers(api: Any, request: Req_SearchCustomers) -> Resp_SearchCustomers:
    """Search customers with automatic pagination."""
    try:
        import re
        kwargs = {}

        # Remove "project" from query (case-insensitive)
        if request.name_or_id_substring:
            query = re.sub(r'\bproject\b', '', request.name_or_id_substring, flags=re.IGNORECASE).strip()
            query = ' '.join(query.split())  # normalize whitespace
            if query:
                kwargs['query'] = query

        if request.deal_phase:
            kwargs['deal_phase'] = request.deal_phase
        if request.account_managers:
            kwargs['account_managers'] = request.account_managers
        if request.locations:
            kwargs['locations'] = request.locations

        companies = paginate_all(api, dev.Req_SearchCustomers, 'companies', **kwargs)
        return Resp_SearchCustomers(success=True, companies=companies)
    except ServerSystemError as e:
        return Resp_SearchCustomers(success=False, error_message=str(e))


def search_time_entries(api: Any, request: Search_TimeEntries) -> Resp_SearchTimeEntries:
    """Search time entries with automatic pagination."""
    try:
        kwargs = {}
        if request.employee:
            kwargs['employee'] = request.employee
        if request.customer:
            kwargs['customer'] = request.customer
        if request.project:
            kwargs['project'] = request.project
        if request.date_from:
            kwargs['date_from'] = request.date_from
        if request.date_to:
            kwargs['date_to'] = request.date_to
        if request.work_category:
            kwargs['work_category'] = request.work_category
        if request.billable:
            kwargs['billable'] = request.billable
        if request.status:
            kwargs['status'] = request.status

        time_entries = paginate_all(api, dev.Req_SearchTimeEntries, 'entries', **kwargs)
        return Resp_SearchTimeEntries(success=True, time_entries=time_entries)
    except ServerSystemError as e:
        return Resp_SearchTimeEntries(success=False, error_message=str(e))


def _convert_project(project: Any) -> ProjectDetailView:
    """Convert API ProjectDetail to our ProjectDetailView with role_in_project."""
    team = []
    for member in project.team or []:
        team.append(ProjectTeamMember(
            employee=member.employee,
            time_slice=member.time_slice or 0.0,
            role_in_project=member.role or "",
        ))
    return ProjectDetailView(
        id=project.id,
        name=project.name,
        description=project.description,
        customer=project.customer,
        status=project.status,
        team=team,
    )


def get_project(api: Any, request: Get_Project) -> Resp_GetProject:
    """
    Get project by ID.

    Args:
        api: ERC3 API client
        request: Get_Project with project_id

    Returns:
        Resp_GetProject with project details or error message
    """
    try:
        result = api.dispatch(dev.Req_GetProject(id=request.project_id))
        if result and result.project:
            return Resp_GetProject(project=_convert_project(result.project), found=True)
    except Exception:
        pass
    return Resp_GetProject(found=False, message="Project not found")


# =============================================================================
# Batch employee fetch
# =============================================================================

def get_employees(
    api: Any,
    request: Get_Employees,
    store_api: Any = None,
    log_file: str = None,
    core: Any = None,
    task: Any = None,
) -> Resp_Get_Employees:
    """
    Fetch multiple employees by IDs with optional field selection.

    Uses build_employee_ext_info() for each employee. Filters fields if
    include_fields is specified. Sorts by requested field.

    Args:
        api: ERC3 API client
        request: Get_Employees with employee_ids, include_fields, sort_by, sort_order
        store_api: ERC3 DevClient for error handling
        log_file: Optional log file
        core: ERC3 core instance
        task: TaskInfo for error handling

    Returns:
        Resp_Get_Employees with list of employees
    """
    from infra import TaskTerminated

    employees = []
    errors = []

    # Determine if we need to fetch projects
    include_projects = (
        request.include_fields is None or "projects" in request.include_fields
    )

    for emp_id in request.employee_ids:
        try:
            emp_info = build_employee_ext_info(
                api=api,
                employee_id=emp_id,
                include_projects=include_projects,
                store_api=store_api,
                log_file=log_file,
                core=core,
                task=task,
            )
            employees.append(emp_info)
        except TaskTerminated:
            raise  # Server error - propagate
        except Exception as e:
            errors.append(f"{emp_id}: {str(e)}")

    # Filter fields if specified
    if request.include_fields is not None:
        fields_to_keep = set(request.include_fields) | {"id", "notes"}  # id and notes always included
        filtered = []
        for emp in employees:
            data = emp.model_dump()
            filtered_data = {k: v for k, v in data.items() if k in fields_to_keep}
            filtered.append(EmployeeExtInfo(**filtered_data))
        employees = filtered

    # Sort if requested
    if request.sort_by and employees:
        reverse = request.sort_order == "desc"
        employees.sort(
            key=lambda e: getattr(e, request.sort_by, "") or "",
            reverse=reverse
        )

    return Resp_Get_Employees(
        employees=employees,
        total=len(employees),
        errors=errors if errors else None,
    )


# =============================================================================
# Employee update wrapper
# =============================================================================

def update_employee_info(api: Any, request: Any) -> Any:
    """
    PATCH-style employee update: fetch current data, merge with changes.

    Convention:
    - null = keep current value
    - value (including empty string/list) = set new value

    Args:
        api: ERC3 dev API client
        request: Update_EmployeeInfo from dtos.py

    Returns:
        Resp_UpdateEmployeeInfo from API
    """
    from erc3 import erc3 as dev

    # 1. Fetch current employee data
    current = api.dispatch(dev.Req_GetEmployee(id=request.employee))
    emp = current.employee

    # 2. Merge helper: null = keep current, any value = use new
    def merge(new_val, current_val):
        return current_val if new_val is None else new_val

    # 3. Build full request with merged values
    return api.dispatch(dev.Req_UpdateEmployeeInfo(
        employee=request.employee,
        notes=merge(request.notes, emp.notes),
        salary=merge(request.salary, emp.salary),
        skills=merge(request.skills, list(emp.skills) if emp.skills else []),
        wills=merge(request.wills, list(emp.wills) if emp.wills else []),
        location=merge(request.location, emp.location),
        department=merge(request.department, emp.department),
        changed_by=request.changed_by,
    ))


def batch_update_employees(api: Any, request: Any) -> dict:
    """
    Update multiple employees at once.

    Args:
        api: ERC3 dev API client
        request: Batch_Update_Employees from dtos.py

    Returns:
        Dict with results for each employee
    """
    from .dtos import Update_EmployeeInfo

    results = []
    for upd in request.updates:
        # Build single update request
        single_request = Update_EmployeeInfo(
            employee=upd.employee,
            notes=upd.notes,
            salary=upd.salary,
            skills=upd.skills,
            wills=upd.wills,
            location=upd.location,
            department=upd.department,
            changed_by=request.changed_by,
        )
        try:
            update_employee_info(api, single_request)
            results.append({"employee": upd.employee, "success": True})
        except Exception as e:
            results.append({"employee": upd.employee, "success": False, "error": str(e)})

    return {"updated": len([r for r in results if r["success"]]), "results": results}


def update_time_entry(api: Any, request: Update_TimeEntry) -> Any:
    """
    PATCH-style time entry update: fetch current data, merge with changes.

    Convention:
    - None = keep current value
    - value = set new value

    Args:
        api: ERC3 dev API client
        request: Update_TimeEntry from dtos.py

    Returns:
        Result from Req_UpdateTimeEntry
    """
    # 1. Fetch current time entry
    current = api.dispatch(dev.Req_GetTimeEntry(id=request.time_entry_id))
    te = current.entry

    # 2. Merge helper: None = keep current, any value = use new
    def merge(new_val, current_val):
        return current_val if new_val is None else new_val

    # 3. Build full request with merged values
    return api.dispatch(dev.Req_UpdateTimeEntry(
        id=request.time_entry_id,
        date=merge(request.date, te.date),
        hours=merge(request.hours, te.hours),
        work_category=merge(request.work_category, te.work_category),
        notes=merge(request.notes, te.notes),
        billable=merge(request.billable, te.billable),
        status=merge(request.status, te.status),
        changed_by=request.changed_by,
    ))


def delete_wiki(api: Any, file: str, changed_by: str) -> dict:
    """Delete wiki article by setting content to empty string.

    If path has no '/' (root-level name like 'marketing.md'), validates:
    1. File exists in wiki
    2. File name is unique (no ambiguity)

    Returns recoverable error (success=False, needs_clarification=True) if ambiguous.
    """
    from erc3 import ApiException

    # If path is root-level (no '/'), validate uniqueness
    if '/' not in file:
        try:
            wiki_list = api.dispatch(dev.Req_ListWiki())
            paths = wiki_list.paths or []

            # Find all paths ending with this filename
            matches = [p for p in paths if p == file or p.endswith('/' + file)]

            if len(matches) == 0:
                return {
                    "success": False,
                    "needs_clarification": True,
                    "error": f"File '{file}' not found in wiki. Available files: {paths[:10]}"
                }
            elif len(matches) > 1:
                return {
                    "success": False,
                    "needs_clarification": True,
                    "error": f"Ambiguous: '{file}' found in multiple locations: {matches}. Specify full path."
                }
            else:
                # Exactly one match - use the full path
                file = matches[0]
        except ApiException as e:
            return {"success": False, "error": f"Failed to list wiki: {e.api_error}"}

    try:
        api.dispatch(dev.Req_UpdateWiki(
            file=file,
            content="",
            changed_by=changed_by,
        ))
        return {"success": True, "deleted": file}
    except ApiException as e:
        return {"success": False, "error": str(e.api_error)}


def search_wiki(wiki_sha: str, request: Search_Wiki_With_Page) -> Resp_Search_Wiki_With_Page:
    """
    Search company wiki using local txtai index.

    Supports single query or list of queries (max 3).
    Results are merged, deduplicated, sorted by score, and top_k returned.

    Args:
        wiki_sha: Wiki identifier (from api.who_am_i().wiki_sha)
        request: Search request with query (str or list) and top_k

    Returns:
        Resp_Search_Wiki_With_Page with results or error
    """
    if not wiki_sha:
        return Resp_Search_Wiki_With_Page(success=False, error="wiki_sha not available")

    try:
        from infra.wiki_rag import search as wiki_search

        # Normalize query to list
        queries = request.query if isinstance(request.query, list) else [request.query]

        # Collect results from all queries
        all_results = {}  # key: (file_path, section_title, text) -> WikiSearchResult
        for q in queries:
            raw_results = wiki_search(wiki_sha, q, request.top_k)
            for r in raw_results:
                key = (r.get("file_path", ""), r.get("section_title", ""), r.get("text", ""))
                score = r.get("score", 0.0)
                # Keep highest score for duplicates
                if key not in all_results or all_results[key].score < score:
                    all_results[key] = WikiSearchResult(
                        score=score,
                        page_file_name=r.get("file_path", ""),
                        section_title=r.get("section_title", ""),
                        text=r.get("text", ""),
                    )

        # Sort by score descending and take top_k
        results = sorted(all_results.values(), key=lambda x: x.score, reverse=True)[:request.top_k]
        return Resp_Search_Wiki_With_Page(success=True, results=results)
    except Exception as e:
        return Resp_Search_Wiki_With_Page(success=False, error=str(e))


def get_employees_workload(api: Any, request: Get_Employees_Workload) -> Resp_Get_Employees_Workload:
    """
    Get workload (FTE) for one or more employees.

    Source: Project Registry (NOT time tracking).
    Algorithm per employee:
    1. Search for projects where employee is a team member (filtered by scope)
    2. For each project, get full details and find employee's time_slice
    3. Sum all time_slice values

    Args:
        api: ERC3 API client
        request: Request with list of employee_ids and workload_scope

    Returns:
        Resp_Get_Employees_Workload with workload for each employee
    """
    # Determine status filter based on scope
    if request.workload_scope == WorkloadScope.active_only:
        status_filter = ["active"]
    else:  # total_allocation - all statuses
        status_filter = ["active", "exploring", "paused", "idea", "archived"]
    results = []

    for emp_id in request.employee_ids:
        # Search current projects with employee in team
        projects = paginate_all(
            api, dev.Req_SearchProjects, 'projects',
            status=status_filter,
            team=dev.ProjectTeamFilter(employee_id=emp_id)
        )

        # For each project, get details and find time_slice
        total_fte = 0.0
        for proj in projects:
            try:
                resp = api.dispatch(dev.Req_GetProject(id=proj.id))
                if resp.project:
                    for wl in resp.project.team:
                        if wl.employee == emp_id:
                            total_fte += wl.time_slice
                            break
            except Exception:
                pass  # Skip projects that fail to load

        results.append(EmployeeWorkload(employee_id=emp_id, total_fte=round(total_fte, 2)))

    return Resp_Get_Employees_Workload(workloads=results)


def get_project_leads(api: Any) -> Resp_Get_Project_Leads:
    """
    Get all unique project leads across all projects.

    Algorithm:
    1. List all projects
    2. For each project, find team member with role='Lead'
    3. Collect unique employee IDs

    Args:
        api: ERC3 API client

    Returns:
        Resp_Get_Project_Leads with list of unique lead employee IDs
    """
    # 1. Get all projects
    projects = paginate_all(api, dev.Req_ListProjects, 'projects')

    # 2. Collect unique leads
    leads = set()
    for proj in projects:
        try:
            resp = api.dispatch(dev.Req_GetProject(id=proj.id))
            if resp.project:
                for wl in resp.project.team:
                    if wl.role == "Lead":
                        leads.add(wl.employee)
        except Exception:
            pass  # Skip projects that fail to load

    return Resp_Get_Project_Leads(leads=sorted(leads))


def rename_wiki(api: Any, request: Rename_Wiki) -> dict:
    """
    Rename/move a wiki page.

    No direct rename API exists. This function:
    1. Loads content from old path
    2. Creates new file with that content
    3. Zeros out the old file

    Args:
        api: ERC3 API client
        request: Rename_Wiki with old_path, new_path, changed_by

    Returns:
        dict with success status and message
    """
    # 1. Load old file content
    try:
        old_content_resp = api.dispatch(dev.Req_LoadWiki(file=request.old_path))
        content = old_content_resp.content
    except Exception as e:
        return {"success": False, "error": f"Failed to load {request.old_path}: {str(e)}"}

    # 2. Create new file with content
    try:
        api.dispatch(dev.Req_UpdateWiki(
            file=request.new_path,
            content=content,
            changed_by=request.changed_by,
        ))
    except Exception as e:
        return {"success": False, "error": f"Failed to create {request.new_path}: {str(e)}"}

    # 3. Zero out old file
    try:
        api.dispatch(dev.Req_UpdateWiki(
            file=request.old_path,
            content="",
            changed_by=request.changed_by,
        ))
    except Exception as e:
        return {
            "success": False,
            "error": f"Created {request.new_path} but failed to zero out {request.old_path}: {str(e)}",
            "partial": True,
        }

    return {"success": True, "message": f"Renamed {request.old_path} -> {request.new_path}"}


def create_wiki_pages(api: Any, request) -> dict:
    """
    Create multiple wiki pages at once.

    Args:
        api: ERC3 API client
        request: Create_Wiki_Pages with pages list and changed_by

    Returns:
        dict with success status, created files list, and failed files list
    """
    created = []
    failed = []

    for page in request.pages:
        try:
            api.dispatch(dev.Req_UpdateWiki(
                file=page.file,
                content=page.content,
                changed_by=request.changed_by,
            ))
            created.append(page.file)
        except Exception as e:
            failed.append({"file": page.file, "error": str(e)})

    return {
        "success": len(failed) == 0,
        "created": created,
        "failed": failed,
    }


def get_current_employee(api: Any, whoami: Any) -> dev.EmployeeBrief:
    """
    Get EmployeeBrief for the current employee (user who submitted the request).

    Args:
        api: ERC3 API client
        whoami: Resp_WhoAmI with current_user field

    Returns:
        EmployeeBrief for the current employee
    """
    resp = api.dispatch(dev.Req_GetEmployee(id=whoami.current_user))
    emp = resp.employee
    return dev.EmployeeBrief(
        id=emp.id,
        name=emp.name,
        email=emp.email,
        salary=emp.salary,
        location=emp.location,
        department=emp.department,
    )
