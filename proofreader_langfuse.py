"""
Langfuse helpers for the root proofreader workflow (prompt fetching + tracing hooks).

Prompt names match the Langfuse project (text prompts, label: production or PROOFREADER_LANGFUSE_LABEL):

  Task:  proofreader-country-task, proofreader-greater-china-task, proofreader-hyphenation-task,
         proofreader-style-grammar-task, proofreader-terminology-task, proofreader-general-task

  Role:  proofreader-country-role, … (same middle segment as task). If a role fetch succeeds,
         role + task are concatenated for the DashScope system message (local markdown fallback
         stays a single file per agent, unchanged).

  (Tansa validation prompts exist in Langfuse for agentic_v2; this workflow’s chunk Tansa step
   is still rule-based and does not load those prompts here.)

  Generations (`call_llm`) link the Langfuse **task** text prompt via
  ``PROOFREADER_LANGFUSE_LINK_PROMPT_ON_GENERATION`` (default on) so traces show prompt name/version.
  Set ``PROOFREADER_LANGFUSE_LOG_SYSTEM_CHARS`` to tune how much system text appears in generation input
  (`system_prompt_excerpt`; default 12000 chars).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# agent key -> Langfuse text prompt name (task)
LANGFUSE_TASK_PROMPT_BY_AGENT: dict[str, str] = {
    "country": "proofreader-country-task",
    "greater_china": "proofreader-greater-china-task",
    "hyphenation": "proofreader-hyphenation-task",
    "style_grammar": "proofreader-style-grammar-task",
    "terminology": "proofreader-terminology-task",
    "general": "proofreader-general-task",
}

# agent key -> Langfuse text prompt name (role); merged above task when fetch succeeds
LANGFUSE_ROLE_PROMPT_BY_AGENT: dict[str, str] = {
    "country": "proofreader-country-role",
    "greater_china": "proofreader-greater-china-role",
    "hyphenation": "proofreader-hyphenation-role",
    "style_grammar": "proofreader-style-grammar-role",
    "terminology": "proofreader-terminology-role",
    "general": "proofreader-general-role",
}


def _truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def langfuse_prompts_enabled() -> bool:
    if not os.environ.get("LANGFUSE_SECRET_KEY", "").strip():
        return False
    return _truthy("PROOFREADER_USE_LANGFUSE_PROMPTS", True)


def proofreader_prompt_label() -> str:
    return (
        os.environ.get("PROOFREADER_LANGFUSE_LABEL")
        or os.environ.get("AGENTIC_LANGFUSE_LABEL", "production")
    ).strip() or "production"


def _cache_ttl_seconds() -> int | None:
    raw = os.environ.get("PROOFREADER_LANGFUSE_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _merge_role_task_enabled() -> bool:
    return _truthy("PROOFREADER_LANGFUSE_MERGE_ROLE_AND_TASK", True)


def langfuse_generation_prompt_linking_enabled() -> bool:
    """When True, generations pass Langfuse prompt clients so the UI links name + version."""
    if not os.environ.get("LANGFUSE_SECRET_KEY", "").strip():
        return False
    return _truthy("PROOFREADER_LANGFUSE_LINK_PROMPT_ON_GENERATION", True)


def langfuse_log_system_prompt_max_chars() -> int:
    """How much of ``system_prompt`` to include under generation ``input`` in Langfuse Log View."""
    raw = os.environ.get("PROOFREADER_LANGFUSE_LOG_SYSTEM_CHARS", "12000").strip()
    try:
        return max(500, int(raw))
    except ValueError:
        return 12_000


def fetch_text_prompt_client_for_generation(name: str) -> Any | None:
    """
    Return a Langfuse ``TextPromptClient`` for ``update_current_generation(prompt=...)``.

    Uses the same label / TTL as runtime prompt fetching. Cached by the Langfuse SDK.
    """
    if not name.strip():
        return None
    if not langfuse_generation_prompt_linking_enabled():
        return None
    try:
        from langfuse import get_client

        return get_client().get_prompt(**_get_prompt_kw(name))
    except Exception:
        return None


def _get_prompt_kw(name: str) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "name": name,
        "label": proofreader_prompt_label(),
        "type": "text",
    }
    ttl = _cache_ttl_seconds()
    if ttl is not None:
        kw["cache_ttl_seconds"] = ttl
    return kw


def _fetch_compiled_text(client: Any, name: str, compile_variables: dict[str, Any]) -> tuple[str, int | None]:
    prompt = client.get_prompt(**_get_prompt_kw(name))
    body = str(prompt.compile(**compile_variables)).strip()
    version = getattr(prompt, "version", None)
    return body, int(version) if version is not None else None


def load_proofreader_prompt(
    agent_key: str,
    fallback_md_path: Path,
    *,
    compile_variables: dict[str, Any] | None = None,
) -> tuple[str, str, int | None, str]:
    """
    Resolve system prompt text for ``agent_key``.

    Returns:
        (text, source, langfuse_version_or_none, langfuse_prompt_name_or_empty)

    Source is ``"langfuse"`` or ``"local"``.
    """
    lf_task = LANGFUSE_TASK_PROMPT_BY_AGENT.get(agent_key, "")
    if langfuse_prompts_enabled() and lf_task:
        try:
            from langfuse import get_client

            client = get_client()
            vars_ = compile_variables or {}
            task_body, task_ver = _fetch_compiled_text(client, lf_task, vars_)

            if _merge_role_task_enabled():
                lf_role = LANGFUSE_ROLE_PROMPT_BY_AGENT.get(agent_key, "")
                if lf_role:
                    try:
                        role_body, _role_ver = _fetch_compiled_text(client, lf_role, vars_)
                        merged = f"{role_body}\n\n{task_body}".strip()
                        label = f"{lf_role}+{lf_task}"
                        return merged, "langfuse", task_ver, label
                    except Exception:
                        pass

            return task_body, "langfuse", task_ver, lf_task
        except Exception:
            pass

    text = fallback_md_path.read_text(encoding="utf-8").strip()
    return text, "local", None, ""
