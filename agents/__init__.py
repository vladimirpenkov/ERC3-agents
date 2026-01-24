"""
Agents package.

Contains all agent implementations:
- guest_handler: Guest (public) request handling
- watchdog: Security policy checks
- entity_extractor: Entity extraction and resolution
- solver: Main task execution loop

Common models:
- TaskContext: Global context passed between agents
- AgentRun: Metrics for agent execution
- SecurityDecision: Security decision from watchdog
- RoleResult: Result from agent execution
"""

from agents import guest_handler
from agents import watchdog
from agents import entity_extractor
from agents import solver

# Re-export common models for convenience
from agents.common import TaskContext, AgentRun, SecurityDecision, RoleResult, run_agent

__all__ = [
    # Agent modules
    "guest_handler", "watchdog", "entity_extractor", "solver",
    # Common models and helpers
    "TaskContext", "AgentRun", "SecurityDecision", "RoleResult", "run_agent",
]
