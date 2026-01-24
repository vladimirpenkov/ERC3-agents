"""Solver agent configuration."""

# LLM settings (default model)
MODEL_ID = "x-ai/grok-4-fast"
TEMPERATURE = 0.1
MAX_COMPLETION_TOKENS = 10000
EXTRA_BODY = {"reasoning": {"enabled": False, "max_tokens": 0}}

# Complex tasks → stronger model
# If task_id is in task_ids list, use this model instead of default
COMPLEX_TASKS = {
    # to achieve ~95% result, uncomment these ids
    # "task_ids": ["t013", "t015", "t050", "t064", "t071",  "t086", "t016", "t017", "t032", "t035", "t047", "t056", "t068", "t074", "t075", "t076", "t077", "t081", "t094", "t095", "t101"],  # Task IDs that need stronger model
    "task_ids": [],
    "model": {
        "model_id": "x-ai/grok-4.1-fast",
        "temperature": 0.1,
        "max_completion_tokens": 10000,
        "extra_body": {"reasoning": {"enabled": True}},
    }
}

