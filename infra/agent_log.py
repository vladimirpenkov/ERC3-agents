"""Per-task agent logging.

Each task gets its own folder with agent-specific log files.
Call set_task_dir() at task start to set the logging directory.
"""

from pathlib import Path
from typing import Dict, Any, Optional
import json


_task_dir: Optional[Path] = None


def set_task_dir(task_dir: Path):
    """Set current task directory for agent logs.

    Call at the start of each task.
    """
    global _task_dir
    _task_dir = task_dir


def write_entry(agent_name: str, entry: Dict[str, Any]):
    """Write entry to task's agent file.

    Writes to {task_dir}/{agent_name}.jsonl
    """
    if not _task_dir:
        return

    file_path = _task_dir / f"{agent_name}.jsonl"
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    with file_path.open("a", encoding="utf-8") as f:
        f.write(line)
