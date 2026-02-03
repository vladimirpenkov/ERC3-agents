"""Configuration for ERC3 Agent"""

from typing import List
from pydantic import BaseModel


class AgentConfig(BaseModel):
    """Configuration for the ERC3 agent."""

    # Session parameters (for core.start_session)
    benchmark: str = "erc3-prod"
    workspace: str = "default"
    session_name: str = "you name it"
    architecture: str = "Architecture: SGR-based Agentic Workflows with Named Entity Recognition and Non-reasoning Models"
    session_flags: List[str] = ["compete_speed", "compete_accuracy", "compete_budget"]
    # compete_accuracy compete_budget compete_speed compete_local

    # Task filter: if empty, run all tasks; otherwise run only these spec_ids
    task_codes: List[str] = []

    # Task name filter: if set, run only tasks containing this substring in task text (case-insensitive)
    # Example:  "wiki" matches all tasks with "wiki" in text
    task_name_filter: str = ""

    task_timeout_sec: int = 300

    # Data dump: download API data before each task (can take a while). logs/sessions/<sess_id>/<task_spec>/api_data
    data_dump: bool = False

    # Policy rulebook file in data/ (e.g., "security_rules.txt" or "security_rules.json")
    policy_rulebook: str = "security_rules.txt"


# Default configuration instance
default_config = AgentConfig()
