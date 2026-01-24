"""Watchdog agent configuration."""

# LLM settings
MODEL_ID = "x-ai/grok-4-fast"  # Security checks need reliable model
TEMPERATURE = 0.0  # Deterministic for security decisions
MAX_COMPLETION_TOKENS = 3000  # Security decisions are concise
EXTRA_BODY = {"reasoning": {"enabled": False, "max_tokens": 0}}
