#!/usr/bin/env python3
"""Extract and distill information from wiki using LLM.

Usage:
    python -m infra.extract_wiki --wiki-sha1 <sha1>
    python -m infra.extract_wiki --wiki-sha1 <sha1> --rebuild

Reads wiki files, sends to LLM to extract structured information,
saves results to wiki/companies/<sha1>/extracted/
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Literal, Optional, Union

# Add parent dir to path for imports (when running as CLI)
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from pydantic import BaseModel, Field


# =============================================================================
# Models for structured extraction (Routing Pattern)
# =============================================================================

class WikiRef(BaseModel):
    """Reference to wiki source."""
    page_path: str = Field(..., description="Path to wiki page (e.g., 'rulebook.md', 'offices/vienna.md')")
    current_header: str = Field(..., description="Nearest preceding markdown header")


# === RULE (for security rules) ===
class Rule(BaseModel):
    """Single extracted security rule."""
    wiki_ref: WikiRef
    category: Literal["applies_to_guests", "applies_to_users", "other"] = Field(
        ..., description="Who this rule applies to"
    )
    compact_rule: str = Field(..., description="Compact RFC-style rule text")


class ExtractedRules(BaseModel):
    """Extraction result for security rules."""
    company_name: str
    company_locations: List[str] = Field(..., description="Locations where company operates")
    company_execs: List[str] = Field(..., description="Executive names")
    rules: List[Rule]


# === POLICY (for policies) ===
class Policy(BaseModel):
    """Single extracted policy."""
    wiki_ref: WikiRef
    category: Literal["project_policy", "timeentry_policy", "salary_policy", "general_policy"] = Field(
        ..., description="Policy type"
    )
    compact_policy: str = Field(..., description="Compact RFC-style policy text")


class ExtractedPolicies(BaseModel):
    """Extraction result for policies."""
    company_name: str
    company_locations: List[str] = Field(default_factory=list)
    company_execs: List[str] = Field(default_factory=list)
    policies: List[Policy]


# === INFO (for organizational information) ===
class Info(BaseModel):
    """Single extracted organizational info."""
    wiki_ref: WikiRef
    category: Literal["company_structure", "hierarchy", "role",  "other"] = Field(
        ..., description="Info type"
    )
    compact_info: str = Field(..., description="Compact RFC-style info text")


class ExtractedInfo(BaseModel):
    """Extraction result for organizational info."""
    company_name: str
    company_locations: List[str] = Field(default_factory=list)
    company_execs: List[str] = Field(default_factory=list)
    items: List[Info]


# =============================================================================
# Prompts
# =============================================================================

MODEL_ID = "openai/gpt-5.1"

ACCESS_RULES_PROMPT = """Carefully review the wiki below and identify the most important security/scoping/data access rules. Transform it to the LLM most tailored style. It should be guiderails for the LLM-based agent.


Pay attention to rules that mention:
- AI Agent or Public ChatBot
- Data access permissions
- Who can read/write what
- Role-based restrictions

For each rule, provide:
- wiki_ref: the source page path and nearest header
- category: applies_to_guests (Public ChatBot), applies_to_users (authenticated), or other
- compact_rule: RFC-style compact rule text

Rules must be compact and actionable. They will be used by an AI agent automating company APIs. 
"""

HIERARCHY_PROMPT = """Carefully review the wiki below and identify hierarchy and organizational structure. 
Transform it to the LLM most tailored style. It should be guiderails for the LLM-based agent.
This agent should make the decisions about allowance of the task it gets. 
Do not include low-relevant info that doesn't provide useful information for decision-making.

Pay attention to:
  - Organizational levels and reporting lines
  - Role definitions and responsibilities

For each item provide:
 - wiki_ref: the source page path and nearest header
 - category: hierarchy | role | structure
 - compact_info: RFC-style compact text of hierarchy or role information. This text should be adapted as the most formal and clear for LLM Interpretation.
 """

PROJECT_POLICY_PROMPT = """Carefully review the wiki below and identify project policies. Transform it to the LLM most tailored style. It should be guiderails for the LLM-based agent.

Pay attention to:
  - Project access and modification rules
  - Project workflow elements
  - Team membership requirements

For each item provide:
 - wiki_ref: the source page path and nearest header
 - category: project_policy
 - compact_policy: RFC-style compact text of project policy. This text should be adapted as the most formal and clear for LLM interpretation.
"""

TIMEENTRY_POLICY_PROMPT = """Carefully review the wiki below and identify time entry policies. Transform it to the LLM most tailored style. It should be guiderails for the LLM-based agent.

Pay attention to:
  - Time tracking rules and requirements
  - Approval workflows

For each item provide:
 - wiki_ref: the source page path and nearest header
 - category: timeentry_policy
 - compact_policy: RFC-style compact text of time entry policy. This text should be adapted as the most formal and clear for LLM interpretation.
"""

SALARY_POLICY_PROMPT = """Carefully review the wiki below and identify every salary and compensation related information. 
Transform it to the LLM most tailored short style. It should be guiderails for the LLM-based agent.

Pay attention to:
  - Compensation access rules
  - Salary data visibility

For each item provide:
 - wiki_ref: the source page path and nearest header
 - category: salary_policy
 - compact_policy: RFC-style compact text of salary policy. This compact text should be adapted as the most formal and clear for LLM interpretation.
"""

# =============================================================================
# Category ordering (for sorting within each extraction)
# =============================================================================

CATEGORY_ORDER = {
    # Info categories
    "company_structure": 0,
    "hierarchy": 1,
    "role": 2,
    # Policy categories
    "project_policy": 0,
    "timeentry_policy": 1,
    "salary_policy": 2,
    # Rule categories
    "applies_to_guests": 0,
    "applies_to_users": 1,
    "other": 99,  # "other" always last
}


# =============================================================================
# Extraction Configuration
# =============================================================================

EXTRACTIONS = {
    "hierarchy": {
        "prompt": HIERARCHY_PROMPT,
        "parse_files": ["hierarchy.md", "merger.md"],
        "response_model": ExtractedInfo,
        "items_field": "items",
        "lines": [
            "# Hierarchy for {company_name}",
            "# Locations: {locations}",
            "",
        ],
        "item_format": "[{category}] {compact_info}",
        "compact_field": "compact_info",
    },
    "salary_policy": {
        "prompt": SALARY_POLICY_PROMPT,
        "parse_files": ["rulebook.md", "culture.md", "merger.md"],
        "response_model": ExtractedPolicies,
        "items_field": "policies",
        "lines": [
            "# Salary Policies for {company_name}",
            "",
        ],
        "item_format": "[{category}] {compact_policy}",
        "compact_field": "compact_policy",
    },
    "project_policy": {
        "prompt": PROJECT_POLICY_PROMPT,
        "parse_files": ["rulebook.md", "hierarchy.md", "merger.md"],
        "response_model": ExtractedPolicies,
        "items_field": "policies",
        "lines": [
            "# Project Policies for {company_name}",
            "",
        ],
        "item_format": "[{category}] {compact_policy}",
        "compact_field": "compact_policy",
    },
    "timeentry_policy": {
        "prompt": TIMEENTRY_POLICY_PROMPT,
        "parse_files": ["rulebook.md", "hierarchy.md", "merger.md"],
        "response_model": ExtractedPolicies,
        "items_field": "policies",
        "lines": [
            "# Time Entry Policies for {company_name}",
            "",
        ],
        "item_format": "[{category}] {compact_policy}",
        "compact_field": "compact_policy",
    },
    "rules": {
        "prompt": ACCESS_RULES_PROMPT,
        "parse_files": ["rulebook.md", "merger.md"],
        "response_model": ExtractedRules,
        "items_field": "rules",
        "lines": [
            "# Other Rules for {company_name}",
            "",
        ],
        "item_format": "[{category}] {compact_rule}",
        "compact_field": "compact_rule",
    },

}


# =============================================================================
# Checksum utilities
# =============================================================================

def compute_checksum(file_path: Path) -> str:
    """Compute SHA256 checksum of file content."""
    return hashlib.sha256(file_path.read_bytes()).hexdigest()


def load_checksums(output_dir: Path) -> Dict[str, str]:
    """Load checksums.json from output directory."""
    checksums_file = output_dir / "checksums.json"
    if checksums_file.exists():
        return json.loads(checksums_file.read_text(encoding="utf-8"))
    return {}


def save_checksums(output_dir: Path, checksums: Dict[str, str]) -> None:
    """Save checksums.json to output directory."""
    checksums_file = output_dir / "checksums.json"
    checksums_file.write_text(json.dumps(checksums, indent=2, sort_keys=True), encoding="utf-8")


def get_file_basename(extraction_name: str, source_file: str) -> str:
    """Generate base filename: category_sourcefile (without .md extension).

    Examples:
        get_file_basename("salary_policy", "rulebook.md") -> "salary_policy_rulebook"
        get_file_basename("hierarchy", "hierarchy.md") -> "hierarchy_hierarchy"
    """
    source_name = Path(source_file).stem  # rulebook.md -> rulebook
    return f"{extraction_name}_{source_name}"


# =============================================================================
# Wiki loading
# =============================================================================

def load_wiki_file(wiki_dir: Path, file_path: str) -> Optional[tuple[str, str]]:
    """Load a single markdown file from wiki directory.

    Args:
        wiki_dir: Path to wiki directory
        file_path: Relative path to file (e.g., 'rulebook.md')

    Returns:
        Tuple of (path, content) or None if file doesn't exist
    """
    full_path = wiki_dir / file_path
    if full_path.exists():
        return (file_path, full_path.read_text(encoding="utf-8"))
    return None


def load_wiki_files(wiki_dir: Path, parse_files: Optional[List[str]] = None) -> List[tuple[str, str]]:
    """Load markdown files from wiki directory.

    Args:
        wiki_dir: Path to wiki directory
        parse_files: Optional list of filenames to include. If None, loads all.

    Returns:
        List of (path, content) tuples
    """
    files = []
    meta_file = wiki_dir / "_meta.txt"

    if meta_file.exists():
        # Use _meta.txt for file list
        paths = meta_file.read_text().strip().split("\n")
        for path in paths:
            # Filter by parse_files if specified
            if parse_files and path not in parse_files:
                continue
            file_path = wiki_dir / path
            if file_path.exists():
                files.append((path, file_path.read_text(encoding="utf-8")))
    else:
        # Fallback: glob for .md files
        for md_file in wiki_dir.glob("**/*.md"):
            rel_path = str(md_file.relative_to(wiki_dir))
            # Filter by parse_files if specified
            if parse_files and rel_path not in parse_files:
                continue
            files.append((rel_path, md_file.read_text(encoding="utf-8")))

    return files


# =============================================================================
# Extraction functions
# =============================================================================

def get_extraction_path(wiki_sha: str, extraction_name: str, extension: str = "txt") -> Path:
    """Get path to extraction file.

    Args:
        wiki_sha: Wiki SHA1 identifier
        extraction_name: Name from EXTRACTIONS dict
        extension: File extension (txt or json)

    Returns:
        Path to the extraction file
    """
    wiki_dir = Path(__file__).parent.parent / "wiki" / "companies" / wiki_sha
    return wiki_dir / "extracted" / f"{extraction_name}.{extension}"


def read_extraction(wiki_sha: str, extraction_name: str) -> str:
    """Read extracted content from txt files (new per-file format).

    Reads all {extraction_name}_{source}.txt files and concatenates them.

    Args:
        wiki_sha: Wiki SHA1 identifier
        extraction_name: Name from EXTRACTIONS dict

    Returns:
        Concatenated content of all extraction txt files, or empty string if not found
    """
    if extraction_name not in EXTRACTIONS:
        return ""

    config = EXTRACTIONS[extraction_name]
    wiki_dir = Path(__file__).parent.parent / "wiki" / "companies" / wiki_sha
    output_dir = wiki_dir / "extracted"

    if not output_dir.exists():
        return ""

    contents = []
    for source_file in config["parse_files"]:
        basename = get_file_basename(extraction_name, source_file)
        txt_file = output_dir / f"{basename}.txt"
        if txt_file.exists():
            contents.append(txt_file.read_text(encoding="utf-8"))

    return "\n\n".join(contents)


def _extract_one_file(
    extraction_name: str,
    source_file: str,
    wiki_dir: Path,
    output_dir: Path,
    config: dict,
    checksums: Dict[str, str],
    rebuild: bool = False
) -> tuple[int, bool]:
    """Extract information from a single wiki file for one extraction type.

    Args:
        extraction_name: Key from EXTRACTIONS dict
        source_file: Source wiki file (e.g., 'rulebook.md')
        wiki_dir: Path to wiki directory
        output_dir: Path to extracted directory
        config: EXTRACTIONS config for this extraction type
        checksums: Current checksums dict (will be modified)
        rebuild: If True, re-extract even if cached

    Returns:
        Tuple of (items_count, was_processed)
    """
    from infra import llm_call

    basename = get_file_basename(extraction_name, source_file)
    json_file = output_dir / f"{basename}.json"
    txt_file = output_dir / f"{basename}.txt"
    source_path = wiki_dir / source_file

    # Check if source file exists
    if not source_path.exists():
        print(f"  [{extraction_name}] {source_file}: not found, skipping")
        return (0, False)

    # Compute current checksum of SOURCE file (key = source filename, not basename)
    current_checksum = compute_checksum(source_path)
    cached_checksum = checksums.get(source_file)

    # Check cache: skip if both conditions met
    if cached_checksum == current_checksum and json_file.exists() and not rebuild:
        print(f"  [{extraction_name}] {source_file}: cached (checksum match)")
        return (0, False)

    # Determine reason for processing
    if not json_file.exists():
        reason = "output missing"
    elif cached_checksum != current_checksum:
        reason = "source changed"
    else:
        reason = "rebuild requested"

    print(f"  [{extraction_name}] {source_file}: processing ({reason})...")

    # Load wiki file content
    wiki_file = load_wiki_file(wiki_dir, source_file)
    if not wiki_file:
        print(f"  [{extraction_name}] {source_file}: failed to load")
        return (0, False)

    path, content = wiki_file

    # Build prompt with wiki content
    prompt = config["prompt"] + "\n\n"
    prompt += f"---- start of {path} ----\n\n{content}\n\n---- end of {path} ----\n\n"

    # Call LLM
    result = llm_call(
        model_id=MODEL_ID,
        messages=[{"role": "system", "content": prompt}],
        response_format=config["response_model"],
        temperature=0.1,
        max_tokens=20000,
    )

    if not result.success or not result.parsed:
        print(f"  [{extraction_name}] {source_file}: LLM failed: {result.error}")
        return (0, False)

    extracted = result.parsed

    # Save to JSON
    json_file.write_text(extracted.model_dump_json(indent=2), encoding="utf-8")

    # Build human-readable text version
    items = getattr(extracted, config["items_field"])
    lines = []

    # Header lines with formatting
    for line_template in config["lines"]:
        line = line_template.format(
            company_name=extracted.company_name,
            locations=", ".join(extracted.company_locations) if extracted.company_locations else "",
            execs=", ".join(extracted.company_execs) if extracted.company_execs else "",
        )
        lines.append(line)

    # Item lines
    for item in items:
        compact_value = getattr(item, config["compact_field"])
        item_line = f"[{item.category}] {compact_value}"
        lines.append(item_line)
        lines.append(f"  source: {item.wiki_ref.page_path} > {item.wiki_ref.current_header}")
        lines.append("")

    txt_file.write_text("\n".join(lines), encoding="utf-8")

    # Update checksum (key = source filename)
    checksums[source_file] = current_checksum

    print(f"  [{extraction_name}] {source_file}: extracted {len(items)} items")
    return (len(items), True)


def _extract_one(
    wiki_sha: str,
    extraction_name: str,
    wiki_dir: Path,
    checksums: Dict[str, str],
    rebuild: bool = False
) -> int:
    """Extract one type of information from wiki, processing each file individually.

    Args:
        wiki_sha: Wiki SHA1 identifier
        extraction_name: Key from EXTRACTIONS dict
        wiki_dir: Path to wiki directory
        checksums: Checksums dict (will be modified)
        rebuild: If True, re-extract even if cached

    Returns:
        Number of items extracted (only newly processed)
    """
    config = EXTRACTIONS[extraction_name]
    output_dir = wiki_dir / "extracted"
    output_dir.mkdir(exist_ok=True)

    total_items = 0
    for source_file in config["parse_files"]:
        items_count, was_processed = _extract_one_file(
            extraction_name=extraction_name,
            source_file=source_file,
            wiki_dir=wiki_dir,
            output_dir=output_dir,
            config=config,
            checksums=checksums,
            rebuild=rebuild,
        )
        if was_processed:
            total_items += items_count

    return total_items


def extract_all(wiki_sha: str, rebuild: bool = False) -> dict:
    """Extract all information types from wiki.

    Args:
        wiki_sha: Wiki SHA1 identifier
        rebuild: If True, re-extract even if cached

    Returns:
        Dict with extraction counts: {extraction_name: item_count}
    """
    wiki_dir = Path(__file__).parent.parent / "wiki" / "companies" / wiki_sha

    if not wiki_dir.exists():
        raise ValueError(f"Wiki directory not found: {wiki_dir}")

    print(f"Extracting from wiki: {wiki_sha}")

    output_dir = wiki_dir / "extracted"
    output_dir.mkdir(exist_ok=True)

    # Load existing checksums
    checksums = load_checksums(output_dir)

    results = {}
    for name in EXTRACTIONS:
        try:
            count = _extract_one(wiki_sha, name, wiki_dir, checksums, rebuild)
            results[name] = count
        except Exception as e:
            print(f"  [{name}] error: {e}")
            results[name] = -1

    # Save updated checksums
    save_checksums(output_dir, checksums)

    # Join all extractions into single files (txt and json)
    join_extractions(wiki_sha)
    join_extractions_json(wiki_sha)

    return results


def join_extractions(wiki_sha: str) -> None:
    """Join all extracted JSON files into a single joined.txt.

    Rules:
    1. Process in EXTRACTIONS dict order
    2. For each extraction, process source files in parse_files order
    3. Sort items by category order within each file
    4. Skip duplicates (same wiki_ref already added)
    5. Add 2 blank lines after each extraction section
    6. Save duplicates to doubles.txt for review

    File naming: {extraction_name}_{source_stem}.json
    Example: salary_policy_rulebook.json, salary_policy_culture.json

    Args:
        wiki_sha: Wiki SHA1 identifier
    """
    wiki_dir = Path(__file__).parent.parent / "wiki" / "companies" / wiki_sha
    output_dir = wiki_dir / "extracted"

    if not output_dir.exists():
        return

    joined_lines = []
    doubles_lines = []
    seen_refs: set[tuple[str, str]] = set()  # (page_path, current_header)

    for extraction_name, config in EXTRACTIONS.items():
        section_items = []

        # Collect items from all source files for this extraction
        for source_file in config["parse_files"]:
            basename = get_file_basename(extraction_name, source_file)
            json_file = output_dir / f"{basename}.json"

            if not json_file.exists():
                continue

            # Load JSON
            data = json.loads(json_file.read_text(encoding="utf-8"))
            items_field = config["items_field"]
            items = data.get(items_field, [])
            section_items.extend(items)

        if not section_items:
            continue

        # Sort by category order
        section_items.sort(key=lambda x: CATEGORY_ORDER.get(x.get("category", ""), 99))

        # Process items
        section_lines = []
        for item in section_items:
            wiki_ref = item.get("wiki_ref", {})
            ref_key = (wiki_ref.get("page_path", ""), wiki_ref.get("current_header", ""))

            # Check for duplicate
            if ref_key in seen_refs:
                # Log duplicate
                compact_field = config["compact_field"]
                compact_value = item.get(compact_field, "")
                doubles_lines.append(f"DUPLICATE from [{extraction_name}]:")
                doubles_lines.append(f"  wiki_ref: {ref_key[0]} > {ref_key[1]}")
                doubles_lines.append(f"  [{item.get('category', '')}] {compact_value[:100]}...")
                doubles_lines.append("")
                continue

            seen_refs.add(ref_key)

            # Format item
            compact_field = config["compact_field"]
            compact_value = item.get(compact_field, "")
            category = item.get("category", "")

            section_lines.append(f"[{category}] {compact_value}")
            section_lines.append(f"  source: {ref_key[0]} > {ref_key[1]}")
            section_lines.append("")

        if section_lines:
            joined_lines.extend(section_lines)
            joined_lines.append("")  # 2 blank lines between sections
            joined_lines.append("")

    # Save joined.txt
    joined_file = output_dir / "joined.txt"
    joined_file.write_text("\n".join(joined_lines), encoding="utf-8")
    print(f"  [joined] saved {len(seen_refs)} items to {joined_file}")

    # Save doubles.txt if any
    if doubles_lines:
        doubles_file = output_dir / "doubles.txt"
        doubles_file.write_text("\n".join(doubles_lines), encoding="utf-8")
        print(f"  [doubles] saved {len([l for l in doubles_lines if l.startswith('DUPLICATE')])} duplicates to {doubles_file}")


def join_extractions_json(wiki_sha: str) -> None:
    """Join all extracted JSON files into a single joined.json.

    Same logic as join_extractions() but outputs JSON instead of text.
    Each rule is an object with category, text, and source fields.

    Args:
        wiki_sha: Wiki SHA1 identifier
    """
    wiki_dir = Path(__file__).parent.parent / "wiki" / "companies" / wiki_sha
    output_dir = wiki_dir / "extracted"

    if not output_dir.exists():
        return

    rules = []
    seen_refs: set[tuple[str, str]] = set()

    for extraction_name, config in EXTRACTIONS.items():
        section_items = []

        for source_file in config["parse_files"]:
            basename = get_file_basename(extraction_name, source_file)
            json_file = output_dir / f"{basename}.json"

            if not json_file.exists():
                continue

            data = json.loads(json_file.read_text(encoding="utf-8"))
            items_field = config["items_field"]
            items = data.get(items_field, [])
            section_items.extend(items)

        if not section_items:
            continue

        # Sort by category order
        section_items.sort(key=lambda x: CATEGORY_ORDER.get(x.get("category", ""), 99))

        for item in section_items:
            wiki_ref = item.get("wiki_ref", {})
            ref_key = (wiki_ref.get("page_path", ""), wiki_ref.get("current_header", ""))

            if ref_key in seen_refs:
                continue
            seen_refs.add(ref_key)

            compact_field = config["compact_field"]
            rules.append({
                "category": item.get("category"),
                "text": item.get(compact_field),
                "source": {
                    "file": wiki_ref.get("page_path"),
                    "section": wiki_ref.get("current_header"),
                }
            })

    joined_file = output_dir / "joined.json"
    joined_file.write_text(json.dumps(rules, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  [joined.json] saved {len(rules)} items to {joined_file}")


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract information from wiki using LLM")
    parser.add_argument("--wiki-sha1", required=True, help="Wiki SHA1 (directory under wiki/companies/)")
    parser.add_argument("--rebuild", action="store_true", help="Re-extract even if cached")

    args = parser.parse_args()

    try:
        results = extract_all(args.wiki_sha1, rebuild=args.rebuild)
        print(f"\nExtraction complete:")
        for name, count in results.items():
            status = f"{count} items" if count >= 0 else "error"
            print(f"  {name}: {status}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
