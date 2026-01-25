"""
Unified LLM client wrapper for all LLM calls via OpenRouter.

Provides:
- Rate limit retry logic
- Per-model token tracking
- Structured response parsing
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Type, List, Dict, Any

from pydantic import BaseModel, ValidationError
from openai import OpenAI, RateLimitError


# Debug output
DEBUG_LLM = False  # Set to True to see LLM call stats

# Rate limit settings
MAX_RETRIES = 5
RATE_LIMIT_WAIT = 60  # seconds (TPM limit needs ~1 min to reset)

# CLI colors for output
CLI_RED = "\x1B[31m"
CLI_YELLOW = "\x1B[33m"
CLI_CLR = "\x1B[0m"


@dataclass
class TokenUsage:
    """Token usage and cost for a single LLM call."""
    prompt: int = 0
    completion: int = 0
    total: int = 0
    cached_tokens: int = 0
    cost: float = 0.0  # Cost in credits (OpenRouter)
    duration_sec: float = 0.0  # Wall-clock time for LLM call (network + inference)

    def add(self, other: "TokenUsage") -> None:
        """Add usage from another TokenUsage instance."""
        self.prompt += other.prompt
        self.completion += other.completion
        self.total += other.total
        self.cached_tokens += other.cached_tokens
        self.cost += other.cost
        self.duration_sec += other.duration_sec


@dataclass
class LLMResult:
    """Result from an LLM call."""
    success: bool
    parsed: Optional[BaseModel] = None
    usage: Optional[TokenUsage] = None
    model_id: str = ""
    error: Optional[str] = None
    raw_response: Optional[Any] = None


# Task-level usage accumulator (reset per task)
_task_usage: Dict[str, TokenUsage] = {}


def reset_task_usage(model_id: Optional[str] = None) -> None:
    """Reset task-level token usage. Call at task start.

    Args:
        model_id: If provided, initialize with 1 token each for prompt/completion
                  to ensure telemetry is logged even for tasks with no LLM calls
                  (e.g., fast-check denials). Server requires completion_tokens > 0.
    """
    global _task_usage
    if model_id:
        _task_usage = {model_id: TokenUsage(prompt=1, completion=1, total=2)}
    else:
        _task_usage = {}


def get_task_usage() -> Dict[str, TokenUsage]:
    """Get accumulated token usage per model for current task."""
    return _task_usage


def _accumulate_usage(model_id: str, usage: TokenUsage) -> None:
    """Add usage to task-level accumulator."""
    if model_id not in _task_usage:
        _task_usage[model_id] = TokenUsage()
    _task_usage[model_id].add(usage)


def _get_client() -> OpenAI:
    """Get OpenAI client configured for OpenRouter."""
    api_key = os.environ.get("OPENROUTER_API_KEY")

    if not api_key:
        raise ValueError("API key not found: OPENROUTER_API_KEY")

    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        timeout=300.0,
        max_retries=3,
    )


def _make_strict_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Make schema strict for Azure/OpenAI:
    - Add additionalProperties: false to all object types
    - Ensure all properties are in required array
    """
    if isinstance(schema, dict):
        result = {}
        for key, value in schema.items():
            result[key] = _make_strict_schema(value)
        # For objects with properties, ensure strict mode compliance
        if result.get("type") == "object" and "properties" in result:
            result["additionalProperties"] = False
            # Azure requires ALL properties to be in required array
            all_props = list(result["properties"].keys())
            result["required"] = all_props
        return result
    elif isinstance(schema, list):
        return [_make_strict_schema(item) for item in schema]
    else:
        return schema


def _convert_oneof_to_anyof(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert oneOf to anyOf for OpenAI compatibility.

    OpenAI structured outputs reject 'oneOf' but accept 'anyOf'.
    Also removes 'discriminator' which triggers oneOf in Pydantic.

    Recursively processes the entire schema.
    """
    if isinstance(schema, dict):
        result = {}
        for key, value in schema.items():
            # Skip discriminator (OpenAI doesn't need it)
            if key == "discriminator":
                continue
            # Convert oneOf to anyOf
            if key == "oneOf":
                result["anyOf"] = _convert_oneof_to_anyof(value)
            else:
                result[key] = _convert_oneof_to_anyof(value)
        return result
    elif isinstance(schema, list):
        return [_convert_oneof_to_anyof(item) for item in schema]
    else:
        return schema


def _is_openai_model(model_id: str) -> bool:
    """Check if model is OpenAI (requires anyOf instead of oneOf).

    Handles both formats:
    - Native: "gpt-4o", "o1-preview"
    - OpenRouter: "openai/gpt-4o", "openai/o1-preview"
    """
    model_lower = model_id.lower()
    # Remove provider prefix if present
    if "/" in model_lower:
        provider, model_name = model_lower.split("/", 1)
        if provider == "openai":
            return True
        # Other providers (x-ai, anthropic, google) - not OpenAI
        return False
    # Native format without prefix
    return model_lower.startswith("gpt-") or model_lower.startswith("o1")


def llm_call(
    model_id: str,
    messages: List[Dict[str, Any]],
    response_format: Type[BaseModel],
    temperature: float = 1.0,
    max_tokens: int = 8000,
    log_file: Optional[str] = None,
    raw_log_file: Optional[str] = None,
    task_id: Optional[str] = None,
    erc3_api: Optional[Any] = None,
    extra_body: Optional[Dict[str, Any]] = None,
) -> LLMResult:  # type: ignore[type-arg]
    """
    Unified LLM call wrapper.

    Handles rate limit retries and accumulates token usage per model.

    Args:
        model_id: Model ID (e.g., "x-ai/grok-4-fast", "openai/gpt-4o")
        messages: Chat messages in OpenAI format
        response_format: Pydantic model class for structured output
        temperature: Sampling temperature (default 1.0)
        max_tokens: Maximum completion tokens
        log_file: Optional log file for rate limit events
        raw_log_file: Optional log file for raw API responses (before parsing)
        task_id: Optional task ID for raw log context
        erc3_api: Optional ERC3 client for logging completions to server
        extra_body: Optional extra body params (e.g., {"reasoning": {"enabled": False}})

    Returns:
        LLMResult with parsed response or error
    """
    client = _get_client()

    # Default extra_body for OpenRouter: usage include + no fallbacks
    default_extra_body = {
        "usage": {"include": True},
        "provider": {"allow_fallbacks": False},
    }

    # Merge: defaults <- call-specific extra_body
    merged_extra = {**default_extra_body}
    if extra_body:
        merged_extra = {**merged_extra, **extra_body}
    call_params = {"extra_body": merged_extra}

    # Retry loop for rate limit errors
    for attempt in range(MAX_RETRIES):
        raw_content: Optional[str] = None
        started = time.perf_counter()
        try:
            # Generate and process schema
            schema = response_format.model_json_schema()
            schema = _make_strict_schema(schema)
            # Convert oneOf to anyOf for OpenAI models (they reject oneOf)
            if _is_openai_model(model_id):
                schema = _convert_oneof_to_anyof(schema)

            # Use create() instead of parse() to get raw content
            completion = client.chat.completions.create(
                model=model_id,
                messages=messages,  # type: ignore[arg-type]
                response_format={"type": "json_schema", "json_schema": {
                    "name": response_format.__name__,
                    "schema": schema,
                    "strict": True,
                }},
                temperature=temperature,
                max_completion_tokens=max_tokens,
                **call_params,  # Endpoint + call-specific params
            )

            # Log raw response BEFORE any processing
            if raw_log_file:
                from datetime import datetime
                from infra import safe_file_append
                raw_entry = {
                    "task_id": task_id,
                    "model_id": model_id,
                    "attempt": attempt + 1,
                    "time": datetime.now().isoformat(),
                    "raw_response": completion.model_dump() if hasattr(completion, 'model_dump') else str(completion),
                }
                safe_file_append(raw_log_file, json.dumps(raw_entry, ensure_ascii=False, default=str) + "\n")

            # Get raw content and measure duration
            raw_content = completion.choices[0].message.content
            duration_sec = time.perf_counter() - started

            # Extract usage and cost
            usage = TokenUsage()
            cached_prompt_tokens = 0
            if completion.usage:
                # Get cached tokens if available
                if hasattr(completion.usage, 'prompt_tokens_details') and completion.usage.prompt_tokens_details:
                    cached_prompt_tokens = getattr(completion.usage.prompt_tokens_details, 'cached_tokens', 0) or 0

                # Get cost from OpenRouter extended usage
                # OpenRouter returns cost in the usage object when usage.include=true
                usage_dict = completion.usage.model_dump() if hasattr(completion.usage, 'model_dump') else {}
                cost = usage_dict.get('cost', 0.0) or 0.0

                usage = TokenUsage(
                    prompt=completion.usage.prompt_tokens,
                    completion=completion.usage.completion_tokens,
                    total=completion.usage.total_tokens,
                    cached_tokens=cached_prompt_tokens,
                    cost=cost,
                    duration_sec=duration_sec,
                )

            # Accumulate for task-level tracking
            _accumulate_usage(model_id, usage)

            # DEBUG: timing after each LLM call
            usage_dict = completion.usage.model_dump() if completion.usage and hasattr(completion.usage, 'model_dump') else {}
            # Get reasoning tokens from completion_tokens_details (OpenRouter/xAI format)
            completion_details = usage_dict.get('completion_tokens_details') or {}
            reasoning_tokens = completion_details.get('reasoning_tokens', 0) or 0
            reasoning_str = f", reason={reasoning_tokens}" if reasoning_tokens else ""
            if DEBUG_LLM:
                print(f"DEBUG LLM: {model_id} | wall={duration_sec:.2f}s{reasoning_str} | {usage.total}tok")

            # Log completion to sess_compl.json (for diagnostics)
            if log_file:
                from datetime import datetime
                from pathlib import Path
                from infra import safe_file_append
                compl_log = Path(log_file).parent / "sess_compl.json"
                compl_entry = {
                    "ts": datetime.now().isoformat(),
                    "model": model_id,
                    "duration_sec": round(duration_sec, 2),
                    "usage_prompt": usage.prompt,
                    "usage_completion": usage.completion,
                    "usage_total": usage.total,
                    "cached_tokens": usage.cached_tokens,
                    "cost": usage.cost,
                    "completion": raw_content,
                }
                safe_file_append(str(compl_log), json.dumps(compl_entry, ensure_ascii=False) + "\n")

            # Send to ERC3 server if api provided
            if erc3_api and task_id:
                try:
                    erc3_api.log_llm(
                        task_id=task_id,
                        completion=raw_content or "",
                        model=model_id,
                        duration_sec=duration_sec,
                        prompt_tokens=usage.prompt,
                        completion_tokens=usage.completion,
                        cached_prompt_tokens=cached_prompt_tokens,
                    )
                except Exception as e:
                    print(f"{CLI_YELLOW}WARN: log_llm failed: {e}{CLI_CLR}")

            if raw_content is None or raw_content.strip() == "":
                print(f"{CLI_YELLOW}WARN: Empty response from LLM{CLI_CLR}")
                if log_file:
                    from infra import write_json_event
                    write_json_event(log_file, {
                        "type": "parse_error",
                        "model_id": model_id,
                        "error_type": "EmptyResponse",
                        "error": "LLM returned empty content",
                        "raw_content": raw_content or "",
                    })
                return LLMResult(
                    success=False,
                    model_id=model_id,
                    usage=usage,
                    error="Empty response from LLM",
                    raw_response=completion,
                )

            # Parse JSON manually
            # Some models wrap JSON in markdown code blocks, XML tags, or append extra data
            content_to_parse = raw_content.strip()

            # Strip markdown code blocks if present (grok-4.1 does this)
            # Matches: ```json\n{...}\n``` or ```\n{...}\n```
            md_match = re.match(r'^```(?:json)?\s*\n(.*?)\n```$', content_to_parse, re.DOTALL)
            if md_match:
                content_to_parse = md_match.group(1).strip()
                print(f"{CLI_YELLOW}WARN: Stripped markdown code block from response{CLI_CLR}")

            # Strip XML wrapper tags if present (model hallucination)
            xml_match = re.match(r'^<[^>]+>\s*(.*?)\s*</[^>]+>$', content_to_parse, re.DOTALL)
            if xml_match:
                content_to_parse = xml_match.group(1).strip()
                print(f"{CLI_YELLOW}WARN: Stripped XML wrapper from response{CLI_CLR}")

            # Fix invalid \$ escape (model hallucinates JSON Schema $ref syntax)
            if '\\$' in content_to_parse:
                content_to_parse = content_to_parse.replace('\\$', '$')
                print(f"{CLI_YELLOW}WARN: Fixed invalid \\$ escape{CLI_CLR}")

            try:
                data = json.loads(content_to_parse)
            except json.JSONDecodeError as first_error:
                data = None  # Will be set if any fallback succeeds

                # Fallback 1: Try parsing just the first line (model may have appended schema)
                first_line = content_to_parse.split('\n')[0]
                try:
                    data = json.loads(first_line)
                    print(f"{CLI_YELLOW}WARN: Parsed first line only (extra data after){CLI_CLR}")
                except json.JSONDecodeError:
                    pass

                # Fallback 2: Strip leading lone brace (model outputs "{\n{..}", "{{..}", "{ {..}")
                if data is None:
                    brace_match = re.match(r'^\{\s*(\{.*)$', content_to_parse, re.DOTALL)
                    if brace_match:
                        try:
                            data = json.loads(brace_match.group(1))
                            print(f"{CLI_YELLOW}WARN: Stripped leading brace from response{CLI_CLR}")
                        except json.JSONDecodeError:
                            pass

                # Fallback 3: Multiple concatenated JSON objects (model outputs {...}{...}{...})
                # Model does chain-of-thought, last object is the final answer
                if data is None and "Extra data" in str(first_error):
                    try:
                        decoder = json.JSONDecoder()
                        objects = []
                        pos = 0
                        while pos < len(content_to_parse):
                            # Skip whitespace
                            while pos < len(content_to_parse) and content_to_parse[pos] in ' \t\n\r':
                                pos += 1
                            if pos >= len(content_to_parse):
                                break
                            obj, end_pos = decoder.raw_decode(content_to_parse, pos)
                            objects.append(obj)
                            pos = end_pos
                        if objects:
                            data = objects[-1]  # Take LAST object (final answer after self-correction)
                            print(f"{CLI_YELLOW}WARN: Model output {len(objects)} JSON objects, using last one{CLI_CLR}")
                    except json.JSONDecodeError:
                        pass

                # Fallback 4: Unterminated string - try to close it
                if data is None and "Unterminated string" in str(first_error):
                    # Try adding closing quote, brace(s) at the end
                    for suffix in ['"}\n}', '"}', '"]}', '"\n}']:
                        try:
                            data = json.loads(content_to_parse + suffix)
                            print(f"{CLI_YELLOW}WARN: Fixed unterminated string with suffix '{suffix}'{CLI_CLR}")
                            break
                        except json.JSONDecodeError:
                            pass

                # All fallbacks failed
                if data is None:
                    print(f"\n{CLI_RED}JSON_ERROR{CLI_CLR}: {first_error}")
                    print(f"Raw content length: {len(raw_content)} chars")
                    print(f"Raw content:\n{raw_content}")

                    if log_file:
                        from infra import write_json_event
                        write_json_event(log_file, {
                            "type": "parse_error",
                            "model_id": model_id,
                            "error_type": "JSONDecodeError",
                            "error": str(first_error),
                            "raw_content": raw_content,
                        })

                    return LLMResult(
                        success=False,
                        model_id=model_id,
                        usage=usage,
                        error=f"JSON decode error: {first_error}",
                    )

            # Validate with Pydantic
            try:
                parsed = response_format.model_validate(data)
            except ValidationError as e:
                print(f"\n{CLI_RED}VALIDATION_ERROR{CLI_CLR}: {e}")
                print(f"Raw content:\n{raw_content}")

                if log_file:
                    from infra import write_json_event
                    write_json_event(log_file, {
                        "type": "parse_error",
                        "model_id": model_id,
                        "error_type": "ValidationError",
                        "error": str(e),
                        "raw_content": raw_content,
                    })

                return LLMResult(
                    success=False,
                    model_id=model_id,
                    usage=usage,
                    error=f"Validation error: {e}",
                )

            return LLMResult(
                success=True,
                parsed=parsed,
                usage=usage,
                model_id=model_id,
                raw_response=completion,
            )

        except RateLimitError as e:
            duration_sec = time.perf_counter() - started
            if DEBUG_LLM:
                print(f"DEBUG LLM: {model_id} | wall={duration_sec:.2f}s | RATE_LIMIT")
            print(f"\n{CLI_YELLOW}RATE_LIMIT{CLI_CLR}: waiting {RATE_LIMIT_WAIT}s (attempt {attempt+1}/{MAX_RETRIES})")

            if log_file:
                from infra import write_json_event
                write_json_event(log_file, {
                    "type": "rate_limit",
                    "model_id": model_id,
                    "attempt": attempt + 1,
                    "max_retries": MAX_RETRIES,
                    "wait_sec": RATE_LIMIT_WAIT,
                })

            time.sleep(RATE_LIMIT_WAIT)

        except Exception as e:
            # Catch any other errors
            duration_sec = time.perf_counter() - started
            error_type = type(e).__name__
            error_str = str(e)
            if DEBUG_LLM:
                print(f"DEBUG LLM: {model_id} | wall={duration_sec:.2f}s | ERROR: {error_type}")
            print(f"\n{CLI_RED}LLM_ERROR ({error_type}){CLI_CLR}: {error_str}")

            if log_file:
                from infra import write_json_event
                write_json_event(log_file, {
                    "type": "llm_error",
                    "model_id": model_id,
                    "error_type": error_type,
                    "error": error_str,
                    "raw_content": raw_content,
                })

            return LLMResult(
                success=False,
                model_id=model_id,
                error=f"{error_type}: {error_str}",
            )

    # All retries exhausted
    print(f"{CLI_RED}RATE_LIMIT_EXHAUSTED{CLI_CLR}: Max retries ({MAX_RETRIES}) exceeded")
    return LLMResult(
        success=False,
        model_id=model_id,
        error=f"Rate limit exceeded after {MAX_RETRIES} retries",
    )
