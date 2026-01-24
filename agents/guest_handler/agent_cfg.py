"""Guest handler agent configuration."""

# LLM settings
MODEL_ID = "x-ai/grok-4-fast"
TEMPERATURE = 0.0  # Deterministic for public responses
MAX_COMPLETION_TOKENS = 1000  # Short public responses
EXTRA_BODY = {"reasoning": {"enabled": False, "max_tokens": 0}}

