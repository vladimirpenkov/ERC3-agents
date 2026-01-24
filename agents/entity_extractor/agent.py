"""Entity Extractor agent - extracts and resolves entity mentions from task text."""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from infra import llm_call, WIKI_ROOT, make_resolved_key
from infra.agent_log import write_entry
from agents.common import TaskContext, RoleResult
from . import agent_cfg
from .prompts import (
    METADATA_SYSTEM_PROMPT,
    METADATA_USER_TEMPLATE,
    EXTRACT_SYSTEM_PROMPT,
    EXTRACT_USER_TEMPLATE,
    RESOLVE_SYSTEM_PROMPT,
    RESOLVE_USER_TEMPLATE,
)
from tools.employee import build_employee_security_view, build_employee_ext_info


AGENT_NAME = "entity_extractor"

# Fuzzy matching threshold
FUZZY_THRESHOLD = 60

# Exact matches that should NOT be resolved to entities
# (company name, special terms, template placeholders)
UNREPLACEABLES = {
    "bellini",          # Company name - not an external entity
    "bellini coatings", # Company full name
    # Template placeholders (used in path patterns)
    "customer_id",
    "project_id",
    "employee_id",
}

# Lazy cache for fuzzy search (all per-task, reset on task_id change)
# ERC3 generates random employee IDs for each task
_fuzzy_cache: Dict[str, Any] = {
    "employees": [],
    "projects": [],
    "customers": [],
    "_task_id": None,  # current task_id for cache reset
}


# =============================================================================
# Lookups Loading
# =============================================================================

def load_lookups(wiki_sha: Optional[str] = None) -> Dict[str, Any]:
    """
    Load lookups from data/ directory (static reference data).

    Returns dict with keys: skills, wills, departments, locations.
    - skills, wills: Dict[str, str] with {id: description}
    - departments: List[str]
    - locations: List[dict] with {"location": str, "synonyms": List[str]}

    Note: wiki_sha parameter is kept for backwards compatibility but ignored.
    """
    import json

    result: Dict[str, Any] = {
        "skills": {},
        "wills": {},
        "departments": [],
        "locations": [],
    }

    data_dir = Path(__file__).parent.parent.parent / "data"
    if not data_dir.exists():
        return result

    for key in result.keys():
        filepath = data_dir / f"{key}.json"
        if filepath.exists():
            try:
                data = json.loads(filepath.read_text(encoding="utf-8"))
                # skills/wills are dicts, departments/locations are lists
                if key in ("skills", "wills") and isinstance(data, dict):
                    result[key] = data
                elif isinstance(data, list):
                    result[key] = data
            except Exception:
                pass

    return result


def format_lookups_for_prompt(lookups: Dict[str, Any]) -> str:
    """Format lookups for inclusion in Extract prompt."""
    lines = []

    # skills/wills are dicts {id: description}
    if lookups.get("skills"):
        skill_items = [f"{k} ({v})" for k, v in lookups["skills"].items()]
        lines.append(f"Known skills: {', '.join(skill_items)}")
    if lookups.get("wills"):
        will_items = [f"{k} ({v})" for k, v in lookups["wills"].items()]
        lines.append(f"Known wills: {', '.join(will_items)}")
    if lookups.get("departments"):
        lines.append(f"Known departments: {', '.join(lookups['departments'])}")
    if lookups.get("locations"):
        # locations is List[dict] with {"location": str, "synonyms": [...]}
        location_names = [loc["location"] for loc in lookups["locations"] if isinstance(loc, dict)]
        if location_names:
            lines.append(f"Known locations: {', '.join(location_names)}")

    return "\n".join(lines) if lines else ""


# =============================================================================
# Data Models
# =============================================================================

class ExtractedEntitiesList(BaseModel):
    """LLM response for extraction step."""
    entities: List[str] = []  # Text fragments from task
    systems: List[str] = []   # Detected systems: wiki, timeentry, workload


class Candidate(BaseModel):
    """Candidate from database."""
    entity_type: str  # "project", "employee", "customer", "wiki"
    object_id: str  # Database ID
    display_name: str  # Human-readable name
    score: float  # Match score (0-100)
    data: Optional[Dict[str, Any]] = None  # Full data from API


class EntitySelection(BaseModel):
    """Single entity selection."""
    entity: str  # Extracted text
    selected_id: Optional[str] = None  # Candidate id or null


class SelectionsResponse(BaseModel):
    """LLM response for selection step."""
    selections: List[EntitySelection]


class TaskMetadata(BaseModel):
    """Task metadata: language, expected format, translation, self-reference."""
    language: str  # "English", "German", "Chinese", "Russian"
    expected_format: Optional[str] = None  # "Yes/No", "DD-MM-YYYY", user's exact wording
    translation: Optional[str] = None  # English translation if not in English
    is_asking_about_self: bool = False  # True if requester asks about themselves (me, my, myself)


# =============================================================================
# Step 0: Extract task metadata
# =============================================================================

def extract_metadata(
    task_text: str,
    model_id: str,
    erc3_api: Any = None,
    task_id: Optional[str] = None,
) -> Optional[TaskMetadata]:
    """Extract language, expected format, and translation from task text."""
    import time
    started = time.perf_counter()
    messages = [
        {"role": "system", "content": METADATA_SYSTEM_PROMPT},
        {"role": "user", "content": METADATA_USER_TEMPLATE.format(task_text=task_text)},
    ]
    result = llm_call(
        model_id=model_id,
        messages=messages,
        response_format=TaskMetadata,
        temperature=0.1,
        max_tokens=500,
        erc3_api=erc3_api,
        task_id=task_id,
        extra_body=agent_cfg.EXTRA_BODY,
    )
    duration = time.perf_counter() - started

    # Log LLM call
    write_entry("entity_extractor", {
        "step": 0,
        "type": "metadata",
        "messages": messages,
        "response": result.parsed.model_dump() if result.parsed else None,
        "error": result.error,
        "stats": {
            "model": model_id,
            "tokens_total": result.usage.total if result.usage else 0,
            "cost": result.usage.cost if result.usage else 0,
            "duration_sec": round(duration, 2),
        }
    })

    if not result.success or not result.parsed:
        return None

    return result.parsed


# =============================================================================
# Step 1: Extract entities from task text
# =============================================================================

def extract_entities(
    task_text: str,
    model_id: str,
    wiki_sha: Optional[str] = None,
    erc3_api: Any = None,
    task_id: Optional[str] = None,
) -> tuple[List[str], List[str]]:
    """Use LLM to extract entity mentions and detect systems from task text.

    Returns:
        Tuple of (entities, systems)
    """
    import time

    # Load lookups and add to prompt
    lookups = load_lookups(wiki_sha)
    lookups_text = format_lookups_for_prompt(lookups)

    system_prompt = EXTRACT_SYSTEM_PROMPT
    if lookups_text:
        system_prompt = f"{system_prompt}\n\n## Reference Data\n{lookups_text}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": EXTRACT_USER_TEMPLATE.format(task_text=task_text)},
    ]
    started = time.perf_counter()
    result = llm_call(
        model_id=model_id,
        messages=messages,
        response_format=ExtractedEntitiesList,
        temperature=agent_cfg.TEMPERATURE,
        max_tokens=1000,
        erc3_api=erc3_api,
        task_id=task_id,
        extra_body=agent_cfg.EXTRA_BODY,
    )
    duration = time.perf_counter() - started

    # Log LLM call
    write_entry("entity_extractor", {
        "step": 1,
        "type": "extract",
        "messages": messages,
        "response": result.parsed.model_dump() if result.parsed else None,
        "error": result.error,
        "stats": {
            "model": model_id,
            "tokens_total": result.usage.total if result.usage else 0,
            "cost": result.usage.cost if result.usage else 0,
            "duration_sec": round(duration, 2),
        }
    })

    if not result.success or not result.parsed:
        return [], []

    # Clean up entities: strip quotes and whitespace
    entities = [e.strip().strip('"\'') for e in result.parsed.entities if e.strip()]
    return entities, result.parsed.systems


# =============================================================================
# Step 2: Search for candidates
# =============================================================================

def _looks_like_id(text: str) -> bool:
    """Check if text looks like a database ID (contains underscores)."""
    return "_" in text


def _search_by_id(api: Any, text: str) -> List[Candidate]:
    """Try to find entity by exact ID."""
    from erc3 import ApiException, erc3 as dev

    candidates = []

    # Try employee
    try:
        resp = api.dispatch(dev.Req_GetEmployee(id=text))
        if resp and resp.employee:
            candidates.append(Candidate(
                entity_type="employee",
                object_id=resp.employee.id,
                display_name=resp.employee.name,
                score=100.0,
                data=resp.employee.model_dump() if hasattr(resp.employee, 'model_dump') else None,
            ))
    except ApiException:
        pass

    # Try project
    try:
        resp = api.dispatch(dev.Req_GetProject(id=text))
        if resp and resp.found and resp.project:
            candidates.append(Candidate(
                entity_type="project",
                object_id=resp.project.id,
                display_name=resp.project.name,
                score=100.0,
                data=resp.project.model_dump() if hasattr(resp.project, 'model_dump') else None,
            ))
    except ApiException:
        pass

    # Try customer
    try:
        resp = api.dispatch(dev.Req_GetCustomer(id=text))
        if resp and resp.found and resp.company:
            candidates.append(Candidate(
                entity_type="customer",
                object_id=resp.company.id,
                display_name=resp.company.name,
                score=100.0,
                data=resp.company.model_dump() if hasattr(resp.company, 'model_dump') else None,
            ))
    except ApiException:
        pass

    return candidates


def _search_standard(api: Any, text: str) -> List[Candidate]:
    """Search using standard search_* functions. Order: projects, customers, employees."""
    from tools.wrappers import search_employees, search_projects, list_customers
    from tools.dtos import Req_SearchEmployees, Req_SearchProjects, Req_ListCustomers
    from erc3 import erc3 as dev

    global _fuzzy_cache
    candidates = []
    text_lower = text.lower()

    # Search projects first
    resp = search_projects(api, Req_SearchProjects(name_or_id_substring=text))
    if resp.success and resp.projects:
        for proj in resp.projects[:5]:
            candidates.append(Candidate(
                entity_type="project",
                object_id=proj.id,
                display_name=proj.name,
                score=85.0,
                data=proj.model_dump() if hasattr(proj, 'model_dump') else None,
            ))

    # Load FULL customer data via get_customer() for each, cache for fuzzy reuse
    # CompanyBrief from list_customers doesn't have primary_contact_name
    brief_resp = list_customers(api, Req_ListCustomers())
    full_customers = []

    if brief_resp.success and brief_resp.companies:
        for brief in brief_resp.companies:
            try:
                full_resp = api.dispatch(dev.Req_GetCustomer(id=brief.id))
                if full_resp and full_resp.found and full_resp.company:
                    full_customers.append(full_resp.company)
            except Exception:
                pass

        # Cache full data for fuzzy search (avoid reloading)
        _fuzzy_cache["customers"] = full_customers

        for cust in full_customers:
            # Match by company name (partial)
            if text_lower in cust.name.lower():
                candidates.append(Candidate(
                    entity_type="customer",
                    object_id=cust.id,
                    display_name=cust.name,
                    score=85.0,
                    data=cust.model_dump() if hasattr(cust, 'model_dump') else None,
                ))
            # Match by primary_contact_name (partial)
            contact_name = getattr(cust, 'primary_contact_name', None)
            if contact_name and text_lower in contact_name.lower():
                candidates.append(Candidate(
                    entity_type="customer",
                    object_id=cust.id,
                    display_name=f"{contact_name} ({cust.name})",
                    score=90.0,  # Contact name match
                    data=cust.model_dump() if hasattr(cust, 'model_dump') else None,
                ))

    # Search employees last
    resp = search_employees(api, Req_SearchEmployees(name_or_id_substring=text))
    if resp.success and resp.employees:
        for emp in resp.employees[:5]:  # Limit to 5
            candidates.append(Candidate(
                entity_type="employee",
                object_id=emp.id,
                display_name=emp.name,
                score=85.0,  # Standard search match
                data=emp.model_dump() if hasattr(emp, 'model_dump') else None,
            ))

    return candidates


def _search_fuzzy(api: Any, text: str, task_id: str = None) -> List[Candidate]:
    """Fuzzy search across cached objects using rapidfuzz. Order: projects, customers, employees.

    Each cache is loaded lazily - only when we're about to search in it.
    If 100% match found in projects, customers/employees won't be loaded.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return []  # rapidfuzz not installed

    global _fuzzy_cache
    from tools.wrappers import paginate_all
    from erc3 import erc3 as dev

    # Reset caches on task change (but preserve customers if already loaded by _search_standard)
    if task_id and _fuzzy_cache.get("_task_id") != task_id:
        _fuzzy_cache["projects"] = []
        # Don't reset customers - _search_standard loads full data that we want to keep
        # Only reset if explicitly empty (not loaded yet)
        if not _fuzzy_cache.get("customers"):
            _fuzzy_cache["customers"] = []
        _fuzzy_cache["employees"] = []
        _fuzzy_cache["_task_id"] = task_id

    candidates = []
    text_norm = _normalize_for_fuzzy(text)

    def search_in_cache(entity_type: str, cache_key: str, loader) -> bool:
        """Search in cache, load if needed. Returns True if 100% match found."""
        # Lazy load if empty
        if not _fuzzy_cache.get(cache_key):
            try:
                _fuzzy_cache[cache_key] = loader()
            except Exception:
                _fuzzy_cache[cache_key] = []

        # Search
        for item in _fuzzy_cache.get(cache_key, []):
            try:
                item_norm = _normalize_for_fuzzy(item.name)
                score = fuzz.token_set_ratio(text_norm, item_norm)
                if score >= FUZZY_THRESHOLD:
                    candidates.append(Candidate(
                        entity_type=entity_type,
                        object_id=item.id,
                        display_name=item.name,
                        score=score,
                        data=item.model_dump() if hasattr(item, 'model_dump') else None,
                    ))
                    if score == 100.0:
                        return True  # 100% match, stop searching
            except Exception:
                pass
        return False

    # Search order: projects -> customers -> employees (stop on 100% match)
    if search_in_cache("project", "projects",
                       lambda: paginate_all(api, dev.Req_SearchProjects, 'projects', include_archived=True)):
        return candidates

    # Customers: use full data loader (CompanyBrief doesn't have primary_contact_name)
    def load_full_customers():
        """Load full customer data including contacts."""
        from tools.wrappers import list_customers
        from tools.dtos import Req_ListCustomers
        brief_resp = list_customers(api, Req_ListCustomers())
        if not brief_resp.success:
            return []
        full_customers = []
        for brief in brief_resp.companies:
            try:
                full_resp = api.dispatch(dev.Req_GetCustomer(id=brief.id))
                if full_resp and full_resp.found and full_resp.company:
                    full_customers.append(full_resp.company)
            except Exception:
                pass
        return full_customers

    if search_in_cache("customer", "customers", load_full_customers):
        return candidates

    search_in_cache("employee", "employees",
                    lambda: paginate_all(api, dev.Req_SearchEmployees, 'employees'))

    # Search customer contacts by primary_contact_name
    for cust in _fuzzy_cache.get("customers", []):
        try:
            contact_name = getattr(cust, 'primary_contact_name', None)
            if not contact_name:
                continue
            contact_norm = _normalize_for_fuzzy(contact_name)
            score = fuzz.token_set_ratio(text_norm, contact_norm)
            if score >= FUZZY_THRESHOLD:
                candidates.append(Candidate(
                    entity_type="customer",
                    object_id=cust.id,
                    display_name=f"{contact_name} ({cust.name})",
                    score=score,
                    data=cust.model_dump() if hasattr(cust, 'model_dump') else None,
                ))
                if score == 100.0:
                    break  # 100% match found
        except Exception:
            pass

    return candidates


def _search_wiki(wiki_sha: str, text: str) -> List[Candidate]:
    """Search wiki: direct path match or fuzzy search existing pages."""
    text_lower = text.lower()

    # Direct path match: if text looks like a wiki path (contains .md)
    # Create candidate directly - works for both existing and new files
    # No fuzzy matching needed for explicit paths
    if ".md" in text_lower:
        # Path already contains .md - use as-is (handles .md.bak, .md.old, etc.)
        wiki_path = text.strip()

        # Extract display name from path
        display_name = Path(wiki_path).stem.replace("_", " ")

        return [Candidate(
            entity_type="wiki",
            object_id=wiki_path,
            display_name=display_name,
            score=100.0,  # Direct path reference
        )]

    # Fuzzy search existing wiki files (requires rapidfuzz)
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return []

    if not wiki_sha:
        return []

    wiki_dir = WIKI_ROOT / wiki_sha
    if not wiki_dir.exists():
        return []

    candidates = []

    # Walk through wiki files
    for md_file in wiki_dir.rglob("*.md"):
        # Get relative path as page name
        rel_path = md_file.relative_to(wiki_dir)
        page_name = rel_path.stem  # Without .md extension

        # Fuzzy match
        score = fuzz.token_set_ratio(text_lower, page_name.lower().replace("_", " "))
        if score >= FUZZY_THRESHOLD:
            candidates.append(Candidate(
                entity_type="wiki",
                object_id=str(rel_path),
                display_name=page_name.replace("_", " "),
                score=score,
            ))

    return candidates


def _normalize_for_fuzzy(text: str) -> str:
    """Normalize text for fuzzy matching: replace -_. with spaces, lowercase."""
    return text.replace("-", " ").replace("_", " ").replace(".", " ").lower()


def _search_lookups(wiki_sha: Optional[str], text: str, task_text: str = "") -> List[Candidate]:
    """Search in lookups (departments, skills, wills, locations)."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return []

    lookups = load_lookups(wiki_sha)
    candidates = []
    text_norm = _normalize_for_fuzzy(text)
    task_lower = task_text.lower()

    def match_value(
        entity_type: str,
        value: str,
        object_id: str,
        display_name: str,
        data: Optional[Dict[str, Any]] = None,
    ):
        """Helper to match a single value and append candidate if match found."""
        value_norm = _normalize_for_fuzzy(value)
        # Try exact match first (normalized)
        if text_norm == value_norm:
            candidates.append(Candidate(
                entity_type=entity_type,
                object_id=object_id,
                display_name=display_name,
                score=100.0,
                data=data,
            ))
            return True
        # Fuzzy match (skip if value shorter than text)
        if len(value) >= len(text):
            score = fuzz.token_set_ratio(text_norm, value_norm)
            if score >= FUZZY_THRESHOLD:
                candidates.append(Candidate(
                    entity_type=entity_type,
                    object_id=object_id,
                    display_name=display_name,
                    score=score,
                    data=data,
                ))
                return True
        return False

    # Search departments (list)
    for dept in lookups.get("departments", []):
        match_value("department", dept, dept, dept)

    # Search skills (dict: id -> description)
    if "skill" in task_lower:
        for skill_id, description in lookups.get("skills", {}).items():
            skill_data = {"id": skill_id, "description": description}
            # Match by ID
            match_value("skill", skill_id, skill_id, description, skill_data)
            # Match by description
            match_value("skill", description, skill_id, description, skill_data)

    # Search wills (dict: id -> description)
    if any(w in task_lower for w in ["will", "willing", "eager", "interest", "travel"]):
        for will_id, description in lookups.get("wills", {}).items():
            will_data = {"id": will_id, "description": description}
            # Match by ID
            match_value("will", will_id, will_id, description, will_data)
            # Match by description
            match_value("will", description, will_id, description, will_data)

    # Search locations with synonyms
    for loc_obj in lookups.get("locations", []):
        if not isinstance(loc_obj, dict):
            continue
        location = loc_obj.get("location", "")
        synonyms = loc_obj.get("synonyms", [])
        if not location:
            continue
        # Location data includes canonical name and synonyms
        loc_data = {"location": location, "synonyms": synonyms}
        # Match against location name
        match_value("location", location, location, location, loc_data)
        # Match against synonyms (return location as object_id, same data)
        for synonym in synonyms:
            match_value("location", synonym, location, location, loc_data)

    return candidates


def search_candidates(
    entities: List[str],
    api: Any,
    wiki_sha: Optional[str] = None,
    task_text: str = "",
    task_id: str = None,
) -> Dict[str, List[Candidate]]:
    """Search for candidates for each extracted entity."""
    result: Dict[str, List[Candidate]] = {}

    for text in entities:
        # Skip unreplaceable terms (company name, etc.)
        if text.lower() in UNREPLACEABLES:
            result[text] = []
            continue

        candidates: List[Candidate] = []

        # Level 0: Wiki search FIRST (if text looks like wiki reference)
        # Direct .md paths get 100% score and skip other searches
        if "wiki" in text.lower() or ".md" in text.lower():
            wiki_candidates = _search_wiki(wiki_sha, text)
            if wiki_candidates and any(c.score == 100.0 for c in wiki_candidates):
                # Direct wiki path found, skip other searches
                result[text] = wiki_candidates
                continue
            candidates.extend(wiki_candidates)

        # Level 1: ID search
        if _looks_like_id(text):
            candidates.extend(_search_by_id(api, text))

        # Level 2: Standard search (if no ID matches)
        if not candidates:
            candidates.extend(_search_standard(api, text))

        # Level 3: Lookups search (departments, skills, wills, locations)
        # Note: lookups are from data/ dir, don't depend on wiki_sha
        if not any(c.score == 100.0 for c in candidates):
            candidates.extend(_search_lookups(wiki_sha, text, task_text))

        # Early exit if 100% match found
        if any(c.score == 100.0 for c in candidates):
            pass  # Skip fuzzy search
        elif not candidates:
            # Level 4: Fuzzy search (if still nothing)
            fuzzy_results = _search_fuzzy(api, text, task_id)
            candidates.extend(fuzzy_results)

        # Sort by score, deduplicate
        seen = set()
        unique_candidates = []
        for c in sorted(candidates, key=lambda x: x.score, reverse=True):
            key = (c.entity_type, c.object_id)
            if key not in seen:
                seen.add(key)
                unique_candidates.append(c)

        # If 100% match exists, keep only 100% matches (no ambiguity)
        if unique_candidates and unique_candidates[0].score == 100.0:
            unique_candidates = [c for c in unique_candidates if c.score == 100.0]

        result[text] = unique_candidates[:10]  # Limit to top 10

    return result


# =============================================================================
# Step 3: Select candidates (LLM decides) + Replace (code does)
# =============================================================================

def _find_candidate_by_id(
    candidates: Dict[str, List[Candidate]],
    selected_id: str
) -> Optional[Candidate]:
    """Find candidate by ID across all entity candidates."""
    for entity_candidates in candidates.values():
        for c in entity_candidates:
            if c.object_id == selected_id:
                return c
    return None


def resolve_entities(
    task_text: str,
    entities: List[str],
    candidates: Dict[str, List[Candidate]],
    model_id: str,
    erc3_api: Any = None,
    task_id: Optional[str] = None,
) -> tuple[str, List[str]]:
    """Select best candidates and replace in text.

    Strategy:
    1. Auto-resolve: entities with single 100% match → substitute directly
    2. LLM-resolve: entities with multiple candidates or <100% → ask LLM
    """
    formatted_text = task_text
    used_object_ids = set()
    llm_rejected: List[str] = []

    # Separate: auto-resolve (single 100%) vs needs-llm (ambiguous)
    auto_resolved: Dict[str, Candidate] = {}
    needs_llm: Dict[str, List[Candidate]] = {}

    for entity in entities:
        entity_candidates = candidates.get(entity, [])
        if not entity_candidates:
            continue

        # Check for single 100% match
        perfect = [c for c in entity_candidates if c.score == 100.0]
        if len(perfect) == 1:
            auto_resolved[entity] = perfect[0]
        else:
            needs_llm[entity] = entity_candidates

    # Step 1: Apply auto-resolved substitutions
    for entity, candidate in auto_resolved.items():
        if candidate.object_id in used_object_ids:
            continue
        used_object_ids.add(candidate.object_id)
        tag = f"{{{make_resolved_key(candidate.entity_type, candidate.object_id)}}}"
        formatted_text = formatted_text.replace(entity, tag, 1)

    # Step 2: If nothing needs LLM, we're done
    if not needs_llm:
        return formatted_text, llm_rejected

    # Step 3: Format remaining entities for LLM
    entities_with_candidates = []
    for entity, entity_candidates in needs_llm.items():
        candidate_strs = []
        for c in entity_candidates:
            candidate_strs.append(
                f"  [{c.entity_type}] id:'{c.object_id}' name:'{c.display_name}' score:{c.score:.0f}%"
            )
        entities_with_candidates.append(
            f'"{entity}":\n' + "\n".join(candidate_strs)
        )

    prompt_text = "\n\n".join(entities_with_candidates)

    # Step 4: Ask LLM to select candidates
    import time
    messages = [
        {"role": "system", "content": RESOLVE_SYSTEM_PROMPT},
        {"role": "user", "content": RESOLVE_USER_TEMPLATE.format(
            task_text=formatted_text,  # Use already-substituted text for context
            entities_with_candidates=prompt_text,
        )},
    ]
    started = time.perf_counter()
    result = llm_call(
        model_id=model_id,
        messages=messages,
        response_format=SelectionsResponse,
        temperature=0.1,
        max_tokens=1000,
        erc3_api=erc3_api,
        task_id=task_id,
        extra_body=agent_cfg.EXTRA_BODY,
    )
    duration = time.perf_counter() - started

    # Log LLM call
    write_entry("entity_extractor", {
        "step": 3,
        "type": "resolve",
        "messages": messages,
        "response": result.parsed.model_dump() if result.parsed else None,
        "error": result.error,
        "stats": {
            "model": model_id,
            "tokens_total": result.usage.total if result.usage else 0,
            "cost": result.usage.cost if result.usage else 0,
            "duration_sec": round(duration, 2),
        }
    })

    if not result.success or not result.parsed:
        return formatted_text, list(needs_llm.keys())  # All needs_llm are unresolved

    # Collect entities where LLM returned null
    llm_rejected = [
        selection.entity
        for selection in result.parsed.selections
        if not selection.selected_id
    ]

    # Step 5: Apply LLM selections
    for selection in result.parsed.selections:
        if not selection.selected_id:
            continue

        candidate = _find_candidate_by_id(candidates, selection.selected_id)
        if not candidate:
            continue

        if candidate.object_id in used_object_ids:
            continue
        used_object_ids.add(candidate.object_id)

        tag = f"{{{make_resolved_key(candidate.entity_type, candidate.object_id)}}}"
        formatted_text = formatted_text.replace(selection.entity, tag, 1)

    return formatted_text, llm_rejected


# =============================================================================
# Step 4: Build resolved_objects from formatted text
# =============================================================================

def build_resolved_objects(
    formatted_text: str,
    candidates: Dict[str, List[Candidate]]
) -> Dict[str, Dict[str, Any]]:
    """Build resolved_objects dict from formatted text and candidates.

    Scans formatted_text for {type:id} tags, finds matching candidates,
    returns dict for TaskContext.

    Args:
        formatted_text: Text with {type:id} tags
        candidates: Dict of entity text -> list of candidates

    Returns:
        Dict with key "type:id", value {"type", "id", "data"}
    """
    result: Dict[str, Dict[str, Any]] = {}

    # Find all {type:id} tags in text
    pattern = r'\{(\w+):([^}]+)\}'
    for match in re.finditer(pattern, formatted_text):
        entity_type, object_id = match.groups()
        key = make_resolved_key(entity_type, object_id)

        # Find candidate with this id
        found = False
        for text, cands in candidates.items():
            for c in cands:
                if c.entity_type == entity_type and c.object_id == object_id:
                    result[key] = {
                        "type": entity_type,
                        "id": object_id,
                        "data": c.data or {}
                    }
                    found = True
                    break
            if found:
                break

    return result


# =============================================================================
# Main entry point
# =============================================================================

def run(context: TaskContext) -> RoleResult:
    """Run entity extraction, update context in place.

    Reads from context:
        - task.task_text: Original task text
        - whoami.wiki_sha1: Wiki identifier for lookups
        - api, core, store_api, log_file

    Updates context:
        - task_language, task_expected_format, task_text_national, is_asking_about_self
        - security_task_text, security_objects (for watchdog - with author, SecurityView)
        - solver_task_text, solver_objects (for solver - ExtInfo, no author if not asking about self)

    Returns:
        RoleResult with status "done" or "error"
    """
    task_text = context.task.task_text
    original_text = task_text  # Save for logging
    api = context.api  # Erc3Client for API dispatch
    erc3_core = context.core  # ERC3 instance for log_llm
    task_id = context.task.task_id if context.task else None
    spec_id = context.task.spec_id if context.task else "unknown"
    wiki_sha = context.whoami.wiki_sha1 if context.whoami else None

    # Get model from config
    model_id = agent_cfg.MODEL_ID

    try:
        # Step 0: Extract task metadata (language, format, translation, self-reference)
        metadata = extract_metadata(task_text, model_id, erc3_api=erc3_core, task_id=task_id)
        if metadata:
            context.task_language = metadata.language
            context.task_expected_format = metadata.expected_format
            context.is_asking_about_self = metadata.is_asking_about_self

            # If not English and translation provided — swap texts
            if metadata.language != "English" and metadata.translation:
                context.task_text_national = task_text  # Save original
                task_text = metadata.translation  # Work with English

        # Step 1: Extract entities and detect systems (with lookups from wiki)
        extracted, detected_systems = extract_entities(task_text, model_id, wiki_sha, erc3_api=erc3_core, task_id=task_id)

        # Store detected systems in context
        context.detected_systems = detected_systems

        # Step 2: Search candidates (local buffer)
        candidates: Dict[str, List[Candidate]] = {}
        if extracted:
            candidates = search_candidates(extracted, api, wiki_sha, task_text, task_id)

        # Step 3: Resolve entities (LLM) — select + substitute tags
        formatted_text = task_text
        llm_rejected: List[str] = []
        if extracted and any(len(cands) > 0 for cands in candidates.values()):
            formatted_text, llm_rejected = resolve_entities(task_text, extracted, candidates, model_id, erc3_api=erc3_core, task_id=task_id)

        # Trace unresolved entities (red output)
        # 1. Entities with no candidates (didn't go to LLM-2)
        # 2. Entities where LLM-2 returned null
        no_candidates = [e for e in extracted if not candidates.get(e)] if extracted else []
        unresolved = no_candidates + llm_rejected
        for entity in unresolved:
            print(f"\033[91m  [UNRESOLVED] \"{entity}\"\033[0m", flush=True)

        # Step 4: Build resolved_objects from formatted_text (entities from text)
        text_objects = build_resolved_objects(formatted_text, candidates)

        # Step 5: Add author prefix and build final resolved_objects (author first, no duplicates)
        whoami = context.whoami
        today = getattr(whoami, 'today', None) or ""
        prefix = ""
        author_key: Optional[str] = None
        author_entry: Optional[Dict[str, Any]] = None

        if whoami and not getattr(whoami, 'is_public', True):
            # Employee — use current_user field for ID
            author_id = getattr(whoami, 'current_user', None)
            if author_id:
                author_tag = f"{{employee:{author_id}}}"
                prefix = f"Today, {today}. Requester {author_tag} asks:\n"
                author_key = make_resolved_key("employee", author_id)
                # Get employee data via API
                author_data = {}
                try:
                    from erc3 import erc3 as dev
                    resp = api.dispatch(dev.Req_GetEmployee(id=author_id))
                    if resp and resp.employee:
                        author_data = resp.employee.model_dump() if hasattr(resp.employee, 'model_dump') else {}
                except Exception:
                    pass
                author_entry = {
                    "type": "employee",
                    "id": author_id,
                    "data": author_data
                }
            else:
                prefix = f"Today, {today}. Employee asks:\n"
        else:
            # Guest
            prefix = f"Today, {today}. Guest asks:\n"

        # =====================================================================
        # Build DUAL CONTEXT: security_* and solver_*
        # =====================================================================
        from erc3 import erc3 as dev
        import copy

        # --- SECURITY CONTEXT ---
        # Includes author, uses SecurityView for employees
        security_objects: Dict[str, Dict[str, Any]] = {}
        if author_key and author_entry:
            security_objects[author_key] = copy.deepcopy(author_entry)
        security_objects.update(copy.deepcopy(text_objects))

        # Enrich security_objects with SecurityView
        for key, obj in security_objects.items():
            if obj.get("type") == "employee":
                try:
                    security_view = build_employee_security_view(
                        api=api,
                        employee_id=obj["id"],
                        store_api=context.store_api,
                        log_file=context.log_file,
                        core=erc3_core,
                        task=context.task,
                    )
                    obj["data"] = security_view.model_dump()
                except Exception as e:
                    print(f"  [entity_extractor] build_security_view({obj['id']}) failed: {e}")
            elif obj.get("type") == "project":
                try:
                    resp = api.dispatch(dev.Req_GetProject(id=obj["id"]))
                    if resp and resp.found and resp.project:
                        obj["data"] = resp.project.model_dump()
                except Exception as e:
                    print(f"  [entity_extractor] GetProject({obj['id']}) failed: {e}")

        context.security_task_text = prefix + formatted_text
        context.security_objects = security_objects

        # --- SOLVER CONTEXT ---
        # If asking about self: same as security (with author)
        # Otherwise: no author in text/objects, uses ExtInfo for employees
        is_asking_self = context.is_asking_about_self

        if is_asking_self:
            # Same as security - author is relevant
            solver_objects = copy.deepcopy(security_objects)
            context.solver_task_text = prefix + formatted_text
        else:
            # No author - they're just asking about others
            solver_objects = copy.deepcopy(text_objects)
            context.solver_task_text = formatted_text

        # Enrich solver_objects with ExtInfo (richer data for solving)
        for key, obj in solver_objects.items():
            if obj.get("type") == "employee":
                try:
                    ext_info = build_employee_ext_info(
                        api=api,
                        employee_id=obj["id"],
                        store_api=context.store_api,
                        log_file=context.log_file,
                        core=erc3_core,
                        task=context.task,
                    )
                    obj["data"] = ext_info.model_dump()
                except Exception as e:
                    print(f"  [entity_extractor] build_ext_info({obj['id']}) failed: {e}")
            elif obj.get("type") == "project":
                # Project data same as security
                try:
                    resp = api.dispatch(dev.Req_GetProject(id=obj["id"]))
                    if resp and resp.found and resp.project:
                        obj["data"] = resp.project.model_dump()
                except Exception as e:
                    print(f"  [entity_extractor] GetProject({obj['id']}) failed: {e}")

        context.solver_objects = solver_objects
        context.solver_unresolved = unresolved

        # Derive detected_entities from resolved objects
        detected_entity_types = set()
        for obj in solver_objects.values():
            obj_type = obj.get("type")
            if obj_type:
                detected_entity_types.add(obj_type)
        context.detected_entities = list(detected_entity_types)

        # Console output: final task formulation
        print(f"  {context.security_task_text}", flush=True)
        if detected_systems:
            print(f"  [systems: {', '.join(detected_systems)}]", flush=True)

        return RoleResult(status="done")

    except Exception as e:
        return RoleResult(status="error", data={"error": str(e)})
