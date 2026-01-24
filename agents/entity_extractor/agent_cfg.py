"""Entity extractor agent configuration."""

# LLM settings
MODEL_ID = "x-ai/grok-4-fast"  # Entity extraction
TEMPERATURE = 0.1  # Some creativity for entity matching
MAX_COMPLETION_TOKENS = 2000
EXTRA_BODY = {"reasoning": {"enabled": False, "max_tokens": 0}}
