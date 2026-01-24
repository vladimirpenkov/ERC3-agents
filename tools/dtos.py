"""Pydantic schemas for local tools"""

from enum import Enum
from typing import Annotated, List, Literal, Optional, Union
from annotated_types import MaxLen
from pydantic import BaseModel, Field
from erc3 import erc3 as dev

from tools.employee import EmployeeExtInfo


# =============================================================================
# Response wrapper - excludes denied_security (watchdog handles security)
# =============================================================================

# Mapping: internal (agent-friendly) -> API (server expects)
OUTCOME_TO_API = {
    'successful': 'ok_answer',
    'ok_not_found': 'ok_not_found',
    'unclear_term_need_clarification': 'none_clarification_needed',
    'action_not_supported': 'none_unsupported',
    'server_error': 'error_internal',
}


class RequestedLink(BaseModel):
    """If the user asks for a link"""
    entity_type: Literal["employee", "customer", "project", "wiki", "location"] = Field(
        ..., description="requested entity type"
    )
    entity_id: str = Field(..., description="The id of used object")


class AgentResponse(BaseModel):
    """Submit final response"""
    tool: Literal["AgentResponse"] = "AgentResponse"
    what_does_the_task_require: str = Field (..., description="Write here the task text")
    message: str = Field(..., description="Verify the task text and question. Include in your answer only requested information without any additions or explanations. Formulate a precise answer that **exactly** matches the question and requested format ('Yes'/'No' for boolean queries, exact numbers for counts, etc.). No unnecessary fluff. No list cuts.")
    outcome: Literal[
        'server_error',                     # Unexpected system failure
        'action_not_supported',             # System doesn't have this capability (e.g., "send email", "book meeting", "export to PDF")
        'unclear_term_need_clarification',  # Request unclear, need clarification, no matching objects
        'ok_not_found',                     # Specific entity not found (e.g., empty answer, project ID doesn't exist, employee not in database)
        'successful',                       # Task completed, answer provided
    ] = Field(
        ...,
        description=(
            "Response outcome. Choose carefully according to the Response Outcomes "

        )
    )
    # did_requester_ask_about_himself: bool  # DISABLED: filter removed requester even when they ARE the answer
    requested_links: Optional[List[RequestedLink]] = Field(
        default=None,
        description="Links as proof of answer (max 100, no duplicates). According to ANSWERING LINKS POLICY"
    )


# =============================================================================
# ERC3 Tool Wrappers - hide 'tool' field from LLM schema
# =============================================================================

# --- GET wrappers (read-only) ---

class Get_Customer(BaseModel):
    """GET customer details by company ID."""
    tool: Literal["Get_Customer"] = "Get_Customer"
    company_id: str = Field(..., description="Customer company ID")


class Get_Employees(BaseModel):
    """Fetch multiple employees by IDs with optional field selection.

    Use instead of calling Get_Employee multiple times.
    Returns: list of employees with requested fields.
    """
    tool: Literal["Get_Employees"] = "Get_Employees"
    employee_ids: List[str] = Field(..., description="List of employee IDs to fetch")
    include_fields: Optional[List[Literal[
        "name", "email", "salary", "notes",
        "location", "department", "skills", "wills", "projects",
    ]]] = Field(default=None, description="Fields to include (id always included). None = all fields.")
    sort_by: Optional[Literal["name", "email", "salary", "location", "department"]] = None
    sort_order: Optional[Literal["asc", "desc"]] = "asc"


class Resp_Get_Employees(BaseModel):
    """Response from Get_Employees."""
    employees: List[EmployeeExtInfo]
    total: int
    errors: Optional[List[str]] = None  # IDs that failed to fetch


class Get_Project(BaseModel):
    """GET project details by ID."""
    tool: Literal["Get_Project"] = "Get_Project"
    project_id: str = Field(..., description="Project ID")


class ProjectTeamMember(BaseModel):
    """Team member in a project."""
    employee: str
    time_slice: float
    role_in_project: str = Field(..., description="Role in THIS PROJECT (Lead/Engineer/etc), NOT employee's department!")


class ProjectDetailView(BaseModel):
    """Project details with team."""
    id: str
    name: str
    description: str | None = None
    customer: str | None = None
    status: str | None = None
    team: List[ProjectTeamMember] = []


class Resp_GetProject(BaseModel):
    """Response from get project (wrapper with message)."""
    project: ProjectDetailView | None = None
    found: bool = False
    message: str | None = None


# --- Time wrappers ---

class Get_TimeEntry(BaseModel):
    """GET time entry details by ID."""
    tool: Literal["Get_TimeEntry"] = "Get_TimeEntry"
    time_entry_id: str = Field(..., description="Time entry ID")


class Add_TimeEntry(BaseModel):
    """ADD (create) a new time entry for an employee."""
    tool: Literal["Add_TimeEntry"] = "Add_TimeEntry"
    employee: str = Field(..., description="Employee ID")
    project: str = Field(..., description="Project ID")
    customer: str = Field(..., description="Company customer ID")
    date: str = Field(..., description="Date in YYYY-MM-DD format")
    hours: float = Field(..., description="Hours worked")
    work_category: Literal["customer_project", "internal", "support", "admin"] = Field(default="customer_project", description="Work category: customer_project (default), internal (internal projects), support, admin")
    notes: str = Field(default="", description="Notes")
    billable: bool = Field(default=True, description="Is this billable?")
    status: str = Field(default="draft", description="Entry status: draft, submitted, approved, invoiced, voided")
    logged_by: str = Field(..., description="Employee ID of who is logging this entry")


class Get_TimeSummaryByEmployee(BaseModel):
    """GET aggregated time summary grouped by employee (read-only report)."""
    tool: Literal["Get_TimeSummaryByEmployee"] = "Get_TimeSummaryByEmployee"
    date_from: str = Field(..., description="Start date YYYY-MM-DD (required)")
    date_to: str = Field(..., description="End date YYYY-MM-DD (required)")
    employees: Optional[List[str]] = Field(default=None, description="Filter by employee IDs")
    projects: Optional[List[str]] = Field(default=None, description="Filter by project IDs")
    customers: Optional[List[str]] = Field(default=None, description="Filter by company customer IDs")
    billable: Optional[Literal["billable", "non_billable"]] = Field(default=None, description="Filter by billable status")


class Get_TimeSummaryByProject(BaseModel):
    """GET aggregated time summary grouped by project (read-only report)."""
    tool: Literal["Get_TimeSummaryByProject"] = "Get_TimeSummaryByProject"
    date_from: str = Field(..., description="Start date YYYY-MM-DD (required)")
    date_to: str = Field(..., description="End date YYYY-MM-DD (required)")
    employees: Optional[List[str]] = Field(default=None, description="Filter by employee IDs")
    projects: Optional[List[str]] = Field(default=None, description="Filter by project IDs")
    customers: Optional[List[str]] = Field(default=None, description="Filter by company customer IDs")
    billable: Optional[Literal["billable", "non_billable"]] = Field(default=None, description="Filter by billable status")


class Search_TimeEntries(BaseModel):
    """Search time entries with filters."""
    tool: Literal["Search_TimeEntries"] = "Search_TimeEntries"
    employee: Optional[str] = Field(default=None, description="Filter by employee ID")
    project: Optional[str] = Field(default=None, description="Filter by project ID")
    customer: Optional[str] = Field(default=None, description="Filter by company customer ID")
    date_from: Optional[str] = Field(default=None, description="Start date YYYY-MM-DD")
    date_to: Optional[str] = Field(default=None, description="End date YYYY-MM-DD")
    work_category: Optional[str] = Field(default=None, description="Filter by work category")
    billable: Optional[Literal["billable", "non_billable"]] = Field(default=None, description="Filter by billable status")
    status: Optional[Literal["draft", "submitted", "approved", "invoiced", "voided"]] = Field(default=None, description="Filter by status")


class Update_TimeEntry(BaseModel):
    """UPDATE an existing time entry (PATCH-style: only specified fields are changed)."""
    tool: Literal["Update_TimeEntry"] = "Update_TimeEntry"
    time_entry_id: str = Field(..., description="Time entry ID to update")
    changed_by: str = Field(..., description="Employee ID who made the change")
    # Optional fields - only specified fields will be updated
    date: Optional[str] = Field(default=None, description="Date in YYYY-MM-DD format")
    hours: Optional[float] = Field(default=None, description="Hours worked")
    work_category: Optional[str] = Field(default=None, description="Work category")
    notes: Optional[str] = Field(default=None, description="Notes")
    billable: Optional[bool] = Field(default=None, description="Is this billable?")
    status: Optional[Literal["draft", "submitted", "approved", "invoiced", "voided"]] = Field(default=None, description="Time entry status")


# --- UPDATE wrappers ---

class Update_ProjectTeam(BaseModel):
    """UPDATE project team members."""
    tool: Literal["Update_ProjectTeam"] = "Update_ProjectTeam"
    project_id: str = Field(..., description="Project ID")
    team: List[dev.Workload] = Field(..., description="List of team members with their workload")
    changed_by: Optional[str] = Field(default=None, description="Employee ID who made the change")


class Change_Project_Status(BaseModel):
    """Change project status (e.g., pause, archive, activate)."""
    tool: Literal["Change_Project_Status"] = "Change_Project_Status"
    project_id: str = Field(..., description="Project ID")
    status: Literal["idea", "exploring", "active", "paused", "archived"] = Field(..., description="New project status")
    changed_by: Optional[str] = Field(default=None, description="Employee ID who made the change")


class Update_EmployeeInfo(BaseModel):
    """UPDATE employee profile. Only specify fields you want to change (PATCH semantics)."""
    tool: Literal["Update_EmployeeInfo"] = "Update_EmployeeInfo"
    employee: str = Field(..., description="Employee ID to update")
    notes: Optional[str] = Field(default=None, description="New notes value. null=keep current, ''=clear")
    salary: Optional[int] = Field(default=None, description="New salary. null=keep current")
    skills: Optional[List[dev.SkillLevel]] = Field(default=None, description="New skills list. null=keep current, []=clear all")
    wills: Optional[List[dev.SkillLevel]] = Field(default=None, description="New wills list. null=keep current, []=clear all")
    location: Optional[str] = Field(default=None, description="New location. null=keep current")
    department: Optional[str] = Field(default=None, description="New department. null=keep current")
    changed_by: Optional[str] = Field(default=None, description="Employee ID who made the change")


class EmployeeUpdate(BaseModel):
    """Single employee update within a batch."""
    employee: str = Field(..., description="Employee ID to update")
    notes: Optional[str] = Field(default=None, description="New notes value")
    salary: Optional[int] = Field(default=None, description="New salary")
    skills: Optional[List[dev.SkillLevel]] = Field(default=None, description="New skills list")
    wills: Optional[List[dev.SkillLevel]] = Field(default=None, description="New wills list")
    location: Optional[str] = Field(default=None, description="New location")
    department: Optional[str] = Field(default=None, description="New department")


class Batch_Update_Employees(BaseModel):
    """UPDATE multiple employees at once. Use when updating 2+ employees."""
    tool: Literal["Batch_Update_Employees"] = "Batch_Update_Employees"
    updates: List[EmployeeUpdate] = Field(..., description="List of employee updates")
    changed_by: str = Field(..., description="Employee ID who made the changes")


# =============================================================================
# Pagination wrappers - hide pagination from LLM
# =============================================================================

class Req_ListProjects(BaseModel):
    """List all projects."""
    tool: Literal["Req_ListProjects"] = "Req_ListProjects"


class Req_ListEmployees(BaseModel):
    """List all employees."""
    tool: Literal["Req_ListEmployees"] = "Req_ListEmployees"


class Req_ListCustomers(BaseModel):
    """List all customers."""
    tool: Literal["Req_ListCustomers"] = "Req_ListCustomers"


class Get_CurrentEmployee(BaseModel):
    """Get request author."""
    tool: Literal["Get_CurrentEmployee"] = "Get_CurrentEmployee"


class Req_SearchProjects(BaseModel):
    """Search projects by name/id substring, optionally filtered by team member, customer, or status."""
    tool: Literal["Req_SearchProjects"] = "Req_SearchProjects"
    name_or_id_substring: Optional[str] = Field(default=None, description="Substring to search in project name or id")
    customer_id: Optional[str] = Field(default=None, description="Filter by customer ID")
    status: Optional[List[Literal["idea", "exploring", "active", "paused", "archived"]]] = Field(
        default=None, description="Filter by project statuses"
    )
    known_participant: Optional[str] = Field(
        default=None,
        description="Employee ID who must be a team member of the project"
    )
    participant_role: Optional[Literal["Lead", "Engineer", "Designer", "QA", "Ops", "Other"]] = Field(
        default=None,
        description="Required role of the participant in the project. **IMPORTANT** use 'Lead' with known_participant to get 'my projects'"
    )
    participant_min_time_slice: Optional[float] = Field(
        default=None,
        description="Minimum time allocation (0.0-1.0) for the participant"
    )


# Level name type for filtering by Bellini scale
SkillLevelName = Literal["Very low", "Basic", "Solid", "Strong", "Exceptional"]


# =============================================================================
# Skill/Will filter criteria (discriminated union by 'mode')
# =============================================================================

class FilterMost(BaseModel):
    """Find employees with HIGHEST level of this skill/will"""
    mode: Literal["MOST"] = "MOST"
    name: str = Field(..., description="Skill or will ID (e.g., 'skill_qms', 'will_travel')")


class FilterLeast(BaseModel):
    """Find employees with LOWEST level of this skill/will"""
    mode: Literal["LEAST"] = "LEAST"
    name: str = Field(..., description="Skill or will ID (e.g., 'skill_qms', 'will_travel')")


class FilterSpecific(BaseModel):
    """Filter employees by SPECIFIC level range (returns all matching)."""
    mode: Literal["SPECIFIC"] = "SPECIFIC"
    name: str = Field(..., description="Skill or will ID (e.g., 'skill_qms', 'will_travel')")
    levels: List[SkillLevelName] = Field(..., description="Level names to include: 'Strong', 'Exceptional', etc.")


# Union type for skill/will criteria
Criterion = Union[FilterMost, FilterLeast, FilterSpecific]


class Req_SearchEmployees(BaseModel):
    """Search employees with filtering and optional sorting.

    Behavior:
    - MOST: returns all tied for highest level
    - LEAST:  returns all tied for lowest level
    - SPECIFIC: Filter by specified levels, returns all matching
    """
    tool: Literal["Req_SearchEmployees"] = "Req_SearchEmployees"
    name_or_id_substring: Optional[str] = Field(default=None, description="Substring to search in employee name or id")
    location: Optional[str] = Field(default=None, description="Filter by location")
    department: Optional[str] = Field(default=None, description="Filter by department")
    skill: Optional[Criterion] = Field(default=None, description="Skill filter criterion")
    will: Optional[Criterion] = Field(default=None, description="Will filter criterion")


class Req_SearchCustomers(BaseModel):
    """Search customers by name/id substring and/or filters."""
    tool: Literal["Req_SearchCustomers"] = "Req_SearchCustomers"
    name_or_id_substring: Optional[str] = Field(default=None, description="Substring to search in customer name or id")
    deal_phase: Optional[List[Literal["idea", "exploring", "active", "paused", "archived"]]] = Field(
        default=None, description="Filter by deal phases"
    )
    account_managers: Optional[List[str]] = Field(default=None, description="Filter by account manager IDs")
    locations: Optional[List[str]] = Field(default=None, description="Filter by locations")


# Response types for pagination wrappers
class Resp_ListProjects(BaseModel):
    """Response with all projects."""
    success: bool = True
    projects: List[dev.ProjectBrief] = []
    error_message: str | None = None


class Resp_ListEmployees(BaseModel):
    """Response with all employees."""
    success: bool = True
    employees: List[dev.EmployeeBrief] = []
    error_message: str | None = None


class Resp_ListCustomers(BaseModel):
    """Response with all customers."""
    success: bool = True
    companies: List[dev.CompanyBrief] = []
    error_message: str | None = None


class Resp_SearchProjects(BaseModel):
    """Response from project search."""
    success: bool = True
    projects: List[dev.ProjectBrief] = []
    error_message: str | None = None


class Resp_SearchEmployees(BaseModel):
    """Response from employee search.

    employees type depends on filter mode:
    - MOST/LEAST: EmployeeBrief (level already known from search)
    - SPECIFIC: EmployeeView with ONLY filtered skills/wills (not all 21)
    - No filter: EmployeeBrief
    """
    success: bool = True
    employees: List[Union[dev.EmployeeBrief, dev.EmployeeView]] = []
    error_message: str | None = None


class Resp_SearchCustomers(BaseModel):
    """Response from customer search."""
    success: bool = True
    companies: List[dev.CompanyBrief] = []
    error_message: str | None = None


class Resp_SearchTimeEntries(BaseModel):
    """Response from time entries search with automatic pagination."""
    success: bool = True
    time_entries: List[dev.TimeEntryWithID] = []
    error_message: str | None = None


# =============================================================================
# Wiki tools
# =============================================================================

class Search_Wiki_With_Page(BaseModel):
    """Search the company wiki for relevant information."""
    tool: Literal["Search_Wiki_With_Page"] = "Search_Wiki_With_Page"
    query: str | Annotated[List[str], MaxLen(3)] = Field(..., description="Search query or list of queries (max 3) in English")
    top_k: int = Field(default=10, ge=1, le=20, description="Number of results to return")


class List_Wiki_Pages(BaseModel):
    """List all available wiki pages."""
    tool: Literal["List_Wiki_Pages"] = "List_Wiki_Pages"


class Get_Wiki_Page(BaseModel):
    """Get content of a specific wiki page."""
    tool: Literal["Get_Wiki_Page"] = "Get_Wiki_Page"
    page_path: str = Field(..., description="Path to wiki page (e.g., 'offices/vienna.md', 'rulebook.md')")


class Delete_Wiki(BaseModel):
    """Delete a wiki article by path."""
    tool: Literal["Delete_Wiki"] = "Delete_Wiki"
    file: str = Field(..., description="Path to wiki file to delete, e.g. 'page_path.md'")
    user_id: str = Field(..., description="ID of user performing deletion (from user.id)")


class Update_Wiki(BaseModel):
    """Create or update a wiki page. Do not forget to include new file to the result links."""
    tool: Literal["Update_Wiki"] = "Update_Wiki"
    file: str = Field(..., description="Path to wiki file, e.g. 'policies/new_policy.md'")
    content: str = Field(..., description="Full content of the wiki page (markdown)")
    changed_by: str = Field(..., description="Employee ID who made the change")


class Rename_Wiki(BaseModel):
    """Rename/move a wiki page to a new path.

    No direct rename API exists. This tool:
    1. Creates new file with the content
    2. Zeros out the old file
    . Do not forget to include new file to the result links.
    """
    tool: Literal["Rename_Wiki"] = "Rename_Wiki"
    old_path: str = Field(..., description="Current path, e.g. 'policies/old_name.md'")
    new_path: str = Field(..., description="New path, exactly the name that the Requester asks for'")
    changed_by: str = Field(..., description="Employee ID who made the change")


class WikiPageSpec(BaseModel):
    """Specification for a single wiki page to create."""
    file: str = Field(..., description="Path to wiki file, e.g. 'policies/new_policy.md'")
    content: str = Field(..., description="Full content of the wiki page (markdown)")


class Create_Wiki_Pages(BaseModel):
    """Create multiple wiki pages at once. Include all new files in result links."""
    tool: Literal["Create_Wiki_Pages"] = "Create_Wiki_Pages"
    pages: List[WikiPageSpec] = Field(..., description="List of pages to create")
    changed_by: str = Field(..., description="Employee ID who made the change")


class WikiSearchResult(BaseModel):
    """Single wiki search result. Includes page path and matched content."""
    score: float
    page_file_name: str = Field(..., description="Path to wiki page, e.g. 'policies/access.md'")
    section_title: str
    text: str


class Resp_Search_Wiki_With_Page(BaseModel):
    """Response from wiki search."""
    success: bool
    results: List[WikiSearchResult] = []
    error: str = ""


class Resp_List_Wiki_Pages(BaseModel):
    """Response from list wiki pages."""
    success: bool
    pages: str = ""  # List of pages as text
    error: str = ""


class Resp_Get_Wiki_Page(BaseModel):
    """Response from get wiki page."""
    success: bool
    content: str = ""
    error: str = ""


class Get_Wiki_Headers(BaseModel):
    """Get only headers (##, ###, ####) from a wiki page."""
    wiki_sha: str = Field(..., description="Wiki identifier from whoami.wiki_sha1")
    page_path: str = Field(..., description="Path to wiki page (e.g., 'rulebook.md')")


class Resp_Get_Wiki_Headers(BaseModel):
    """Response with wiki page headers."""
    success: bool
    headers: List[str] = Field(default_factory=list, description="List of headers with their ##, ###, #### markers")
    error: str = ""


class Get_Wiki_Fragments(BaseModel):
    """Get content of specific sections from a wiki page."""
    wiki_sha: str = Field(..., description="Wiki identifier from whoami.wiki_sha1")
    page_path: str = Field(..., description="Path to wiki page")
    headers: List[str] = Field(..., description="Headers to extract (e.g., ['## Access Rules', '### Executive'])")


class WikiFragment(BaseModel):
    """Single wiki section fragment."""
    header: str
    content: str


class Resp_Get_Wiki_Fragments(BaseModel):
    """Response with wiki page fragments."""
    success: bool
    fragments: List[WikiFragment] = Field(default_factory=list)
    error: str = ""


# =============================================================================
# Employee workload tool
# =============================================================================

class WorkloadScope(str, Enum):
    """Scope for workload calculation."""
    active_only = "active_only"           # Only active projects (for "busiest", "current activity")
    total_allocation = "total_allocation"  # All except archived (for "workload", "time slices", "total")


class EmployeeWorkload(BaseModel):
    """Single employee workload result."""
    employee_id: str
    total_fte: float


class Get_Employees_Workload(BaseModel):
    """Calculates the projected FTE workload based on Project Registry.

    Args:
        employee_ids: List of employee IDs.
        workload_scope: Determines which project statuses to include:
            - total_allocation (default): All projects except archived.
              Use for: "workload", "time slices", "total", "least/most busy".
            - active_only: if user asks 'who is busiest' or refers to current activity (not workload).
    """
    tool: Literal["Get_Employees_Workload"] = "Get_Employees_Workload"
    employee_ids: List[str] = Field(..., description="List of employee IDs")
    workload_scope: WorkloadScope = Field(
        default=WorkloadScope.total_allocation,
        description="Scope: total_allocation (all except archived) or active_only"
    )


class Resp_Get_Employees_Workload(BaseModel):
    """Response with workloads for all requested employees."""
    workloads: List[EmployeeWorkload]


# =============================================================================
# Project leads tool
# =============================================================================

class Get_Project_Leads(BaseModel):
    """Get all unique project leads across all projects."""
    tool: Literal["Get_Project_Leads"] = "Get_Project_Leads"


class Resp_Get_Project_Leads(BaseModel):
    """Response with list of unique lead employee IDs."""
    leads: List[str] = Field(default_factory=list, description="List of unique employee IDs who are project leads")
