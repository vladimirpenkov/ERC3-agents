"""
Extended employee information from API.

Sources:
- API: Req_GetEmployee (id, name, email, salary, notes, location, department, skills, wills)
- API: Req_SearchProjects + Req_GetProject (projects)
"""

from typing import List, Optional

from pydantic import BaseModel
from infra import TaskTerminated

from erc3 import erc3 as dev

# Operational departments (production, logistics, quality)
OPERATIONAL_DEPARTMENTS = {
    "Production – Italy",
    "Production – Serbia",
    "Logistics & Supply Chain",
    "Quality & HSE",
}


class ProjectBrief(BaseModel):
    """Brief project info from SearchProjects (no extra API calls)."""
    id: str
    name: str
    customer: str       # Customer ID
    status: str         # idea, exploring, active, paused, archived


class EmployeeExtInfo(BaseModel):
    """Employee information from API + projects.

    All fields except id are Optional to support partial field selection
    via include_fields parameter in Get_Employees.
    """

    # FROM: Req_GetEmployee API
    id: str  # Always required
    name: Optional[str] = None
    email: Optional[str] = None
    salary: Optional[int] = None
    notes: Optional[str] = None
    location: Optional[str] = None
    department: Optional[str] = None
    skills: Optional[List[dict]] = None
    wills: Optional[List[dict]] = None

    # FROM: SearchProjects API (fetched only if requested)
    projects: Optional[List[ProjectBrief]] = None


# =============================================================================
# Security View — for access control checks
# =============================================================================

class TeamMember(BaseModel):
    """Project team member."""
    employee: str            # employee_id
    role_in_project: str     # Lead, Engineer, Designer, QA, Ops, Other (NOT department!)
    time_slice: float        # 0.0 - 1.0


class ProjectSecurityView(BaseModel):
    """Project from access control perspective."""
    id: str
    status: str              # idea, exploring, active, paused, archived
    role_in_project: str     # current employee's role in project (NOT department!)
    team: List[TeamMember]


class EmployeeSecurityView(BaseModel):
    """Employee for access control checks.

    Compact structure with information needed for
    determining access rights to resources.
    """
    id: str
    name: str
    location: str
    department: str
    is_executive: bool          # True if department == "Corporate Leadership"
    is_operational: bool        # True if department in OPERATIONAL_DEPARTMENTS
    projects: List[ProjectSecurityView] = []


def _get_employee_projects(
    api,
    employee_id: str,
    store_api=None,
    log_file: str = None,
    core=None,
    task=None,
) -> List[ProjectBrief]:
    """Get all projects where employee participates (including archived).

    Uses only SearchProjects - no extra GetProject calls per project.

    Args:
        api: ERC3 API client
        employee_id: Employee ID
        store_api: ERC3 DevClient for sending responses on server error
        log_file: Optional log file
        core: ERC3 core instance (for task completion on error)
        task: TaskInfo (for task completion on error)

    Returns:
        List of project briefs (id, name, customer, status)

    Raises:
        TaskTerminated: If server error occurs
    """
    from tools.wrappers import paginate_all
    from infra import handle_api_error

    try:
        projects = paginate_all(
            api,
            dev.Req_SearchProjects,
            'projects',
            include_archived=True,
            team=dev.ProjectTeamFilter(employee_id=employee_id)
        )
    except TaskTerminated:
        raise  # Re-raise immediately, task already completed
    except Exception as e:
        if store_api:
            handle_api_error(e, "SearchProjects", store_api, log_file, core, task)
        # 404 or no store_api - return empty
        return []

    # Convert SearchProjects results to ProjectBrief (no extra API calls)
    result = []
    for proj in projects:
        result.append(ProjectBrief(
            id=proj.id,
            name=proj.name,
            customer=proj.customer or "",
            status=proj.status or "",
        ))

    return result


def build_employee_ext_info(
    api,
    employee_id: str,
    include_projects: bool = True,
    store_api=None,
    log_file: str = None,
    core=None,
    task=None,
) -> EmployeeExtInfo:
    """
    Build employee info from API.

    Args:
        api: ERC3 API client
        employee_id: Employee ID
        include_projects: Whether to fetch projects (default True)
        store_api: ERC3 DevClient for sending responses on server error
        log_file: Optional log file
        core: ERC3 core instance (for task completion on error)
        task: TaskInfo (for task completion on error)

    Returns:
        EmployeeExtInfo

    Raises:
        TaskTerminated: If server error occurs (response already sent)
    """
    from infra import handle_api_error

    # 1. Get employee from API
    try:
        resp = api.dispatch(dev.Req_GetEmployee(id=employee_id))
        emp = resp.employee
    except TaskTerminated:
        raise
    except Exception as e:
        if store_api:
            handle_api_error(e, "GetEmployee", store_api, log_file, core, task)
        # 404 - use fallback stub
        emp = type('Employee', (), {
            'id': employee_id,
            'name': '<employee not found>',
            'email': '',
            'salary': 0,
            'notes': '',
            'location': '',
            'department': '',
            'skills': [],
            'wills': [],
        })()

    # 2. Get projects (optional)
    projects = []
    if include_projects:
        try:
            projects = _get_employee_projects(api, employee_id, store_api, log_file, core, task)
        except TaskTerminated:
            raise
        except Exception as e:
            if store_api:
                handle_api_error(e, "GetEmployeeProjects", store_api, log_file, core, task)
            projects = []

    return EmployeeExtInfo(
        id=emp.id,
        name=emp.name,
        email=emp.email,
        salary=emp.salary,
        notes=emp.notes,
        location=emp.location,
        department=emp.department,
        skills=[{"skill_will_id": s.name, "level": s.level} for s in emp.skills] if emp.skills else [],
        wills=[{"skill_will_id": w.name, "level": w.level} for w in emp.wills] if emp.wills else [],
        projects=projects,
    )


def build_employee_security_view(
    api,
    employee_id: str,
    store_api=None,
    log_file: str = None,
    core=None,
    task=None,
) -> EmployeeSecurityView:
    """
    Build security-focused employee view for access control decisions.

    Args:
        api: ERC3 API client
        employee_id: Employee ID (e.g., "elena_vogel")
        store_api: ERC3 DevClient for sending responses on server error
        log_file: Optional log file
        core: ERC3 core instance (for task completion on error)
        task: TaskInfo (for task completion on error)

    Returns:
        EmployeeSecurityView with access control data

    Raises:
        TaskTerminated: If server error occurs (response already sent)
    """
    from tools.wrappers import paginate_all
    from infra import handle_api_error

    # 1. Get employee from API
    try:
        resp = api.dispatch(dev.Req_GetEmployee(id=employee_id))
        emp = resp.employee
    except TaskTerminated:
        raise
    except Exception as e:
        if store_api:
            handle_api_error(e, "GetEmployee", store_api, log_file, core, task)
        # 404 or no store_api - use fallback stub
        emp = type('Employee', (), {
            'id': employee_id,
            'name': '<employee not found>',
            'location': '',
            'department': '',
        })()

    # 2. Determine is_executive
    is_executive = emp.department == "Corporate Leadership"

    # 3. Get projects where employee participates
    projects_security = []
    try:
        projects = paginate_all(
            api,
            dev.Req_SearchProjects,
            'projects',
            include_archived=True,
            team=dev.ProjectTeamFilter(employee_id=employee_id)
        )
    except TaskTerminated:
        raise
    except Exception as e:
        if store_api:
            handle_api_error(e, "SearchProjects", store_api, log_file, core, task)
        projects = []

    # 4. For each project, get full details (team)
    for proj in projects:
        try:
            full = api.dispatch(dev.Req_GetProject(id=proj.id))
            if not full.project:
                continue

            # Find this employee's role in team
            my_role = ""
            team_members = []
            for member in full.project.team or []:
                team_members.append(TeamMember(
                    employee=member.employee,
                    role_in_project=member.role or "",
                    time_slice=member.time_slice or 0.0,
                ))
                if member.employee == employee_id:
                    my_role = member.role or ""

            projects_security.append(ProjectSecurityView(
                id=full.project.id,
                status=full.project.status or "",
                role_in_project=my_role,
                team=team_members,
            ))
        except TaskTerminated:
            raise
        except Exception as e:
            if store_api:
                handle_api_error(e, "GetProject", store_api, log_file, core, task)
            # 404 - skip this project
            continue

    return EmployeeSecurityView(
        id=emp.id,
        name=emp.name,
        location=emp.location,
        department=emp.department,
        is_executive=is_executive,
        is_operational=emp.department in OPERATIONAL_DEPARTMENTS,
        projects=projects_security,
    )


