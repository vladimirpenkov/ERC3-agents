"""Prompts for Entity Extractor agent."""

# =============================================================================
# Step 0: Extract task metadata (language, format, translation)
# =============================================================================

METADATA_SYSTEM_PROMPT = """You analyze user questions for a corporate chatbot.

Your task: detect language, expected response format, translate if needed, and detect self-reference.

## Rules
1. Identify the language of the question
2. If user specifies an expected format - extract it EXACTLY as written by user
3. If question is NOT in English - provide English translation
4. If question IS in English - translation should be null
5. Detect if requester is asking about themselves:
   - TRUE if contains: me, my, myself, mine, I, we, our, us, ourselves
   - TRUE for actions on behalf of requester: "for me", "assign to me", "create for myself"
   - FALSE if asking about other people/entities only

## Examples

Input: "Ich suche einen seriösen Anbieter mit einem Standort vor Ort in Dubai. Sind Sie dort tätig? Nein/Ja"
→ language: "German", expected_format: "Nein/Ja", translation: "...", is_asking_about_self: false

Input: "What is my salary?"
→ language: "English", expected_format: null, translation: null, is_asking_about_self: true

Input: "What is the salary of John Doe?"
→ language: "English", expected_format: null, translation: null, is_asking_about_self: false

Input: "List my projects"
→ language: "English", expected_format: null, translation: null, is_asking_about_self: true

Input: "We are starting to create customer wiki pages"
→ language: "English", expected_format: null, translation: null, is_asking_about_self: true

Input: "List all active projects"
→ language: "English", expected_format: null, translation: null, is_asking_about_self: false

Return ONLY JSON. No explanations.
"""

METADATA_USER_TEMPLATE = """Analyze this question:

"{task_text}"
"""


# =============================================================================
# Step 1: Extract entity mentions
# =============================================================================

EXTRACT_SYSTEM_PROMPT = """You are an Entity Extractor for the corporate internal chatbot.

Your task:
1. Extract text fragments that may identify entities (employees, projects, customers)
2. Detect which systems are involved in the task

## Input

- `task_text`: user request in natural language

## Output

JSON object with entities and systems arrays:
```json
{
  "entities": ["fragment1", "fragment2", ...],
  "systems": ["wiki", "timeentry", ...]
}
```

## ENTITIES: What to extract

- Person names (full or partial): "Federica", "Barbieri Simone", "Nicolas"
- IDs: "BwFV_151", "proj_siberia_structural_cuprum"
- Project names or fragments: "freezer-room floor system trial", "R&D low-VOC project"
- Word groups written with the capitals: "Central Steel Production"
- Company/customer names: "NordicGold", "Adriatic Marine Services"
- name-like word groups near the words "project", "customer"
- Personal skills, sometimes mentioned as skill_* or will_*

## ENTITIES: What NOT to extract

- common words alone: "project", "customer", "employee", "department"
- Countries and cities alone
- entity states, company workflow terms, and software category names
- special structured text organizing elements like "placeholder", "pattern", "<SOMETHING_ID>" 

## SYSTEMS: Detect these based on keywords

| System | Keywords/Indicators |
|--------|---------------------|
| wiki | wiki, article, page, document, policy, rulebook, create page, rename page |
| timeentry | time entry, hours, logged, tracking, timesheet, work hours, log time |
| workload | workload, capacity, FTE, availability, how busy, utilization |

## Rules

- Extract entities as found in text — do not normalize
- Systems are detected by keywords, not extracted literally

## Examples

"Who is customer for freezer-room floor system trial for NorNickel Storage Group"
→ {"entities": ["freezer-room floor system", "NorNickel Storage Group"], "systems": []}

"Check employees BwFV_151, BwFV_152, BwFV_153"
→ {"entities": ["BwFV_151", "BwFV_152", "BwFV_153"], "systems": []}

"What is the department of Relja"
→ {"entities": ["Relja"], "systems": []}

"How many hours did John log last week?"
→ {"entities": ["John"], "systems": ["timeentry"]}

"Create a wiki page for new policy"
→ {"entities": [], "systems": ["wiki"]}

"What is the workload of employees in Karakas?"
→ {"entities": ["Karakas"], "systems": ["workload"]}

"Show time entries for project Apollo"
→ {"entities": ["Apollo"], "systems": ["timeentry"]}

Return ONLY JSON. No explanations.
"""

EXTRACT_USER_TEMPLATE = """Extract all entity mentions from this task:

"{task_text}"
"""


# Step 3: Select best candidates (LLM decides, code does replacement)
RESOLVE_SYSTEM_PROMPT = """You are an Entity Resolver for a corporate task processing system.

Your task: for each extracted entity, select the best matching candidate from the database.

## Input
- Original task text (for context)
- For each extracted entity: list of candidates with type, id, name, score

## Rules
1. For each entity, select the best matching candidate considering context
2. If no good match exists, set selected_id to null
3. Consider context: "customer" as a word in question ≠ customer entity
4. Only select if the entity text actually refers to the candidate

## Output
Return JSON with selections array. Each selection has:
- entity: the extracted text
- selected_id: the candidate id to use, or null if no match

Return ONLY JSON. No explanations.
"""

RESOLVE_USER_TEMPLATE = """Original task: "{task_text}"

Entities and candidates:
{entities_with_candidates}

Return selections as JSON."""
