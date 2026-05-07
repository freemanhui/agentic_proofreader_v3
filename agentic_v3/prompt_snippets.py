"""Prompt snippet renderers for dynamic V3 soft-rule sections."""

from __future__ import annotations

import os
from typing import Any, Mapping, Sequence


DEFAULT_EMPTY_SOFT_RULES_MESSAGE = "No article-specific soft rules matched for this module."

_DEFAULT_MAX_EVIDENCE_LINES = 3
_DEFAULT_MAX_EVIDENCE_CHARS = 140


def _int_env(name: str, default: int, *, lower: int, upper: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw.isdigit():
        return max(lower, min(int(raw), upper))
    return default


def _max_evidence_lines() -> int:
    return _int_env("AGENTIC_SOFT_RULES_MAX_EVIDENCE_LINES", _DEFAULT_MAX_EVIDENCE_LINES, lower=0, upper=12)


def _max_evidence_chars() -> int:
    return _int_env("AGENTIC_SOFT_RULES_EVIDENCE_CONTEXT_MAX_CHARS", _DEFAULT_MAX_EVIDENCE_CHARS, lower=48, upper=2000)


def _trim(text: str, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def _rule_type(row: Mapping[str, Any]) -> str:
    value = str(row.get("rule_type") or "soft").strip().lower()
    return value if value in {"hard", "soft"} else "soft"


def _evidence_lines(row: Mapping[str, Any]) -> list[str]:
    detail = row.get("evidence_detail")
    if not isinstance(detail, list):
        return []
    lines: list[str] = []
    cap = _max_evidence_chars()
    for item in detail:
        if len(lines) >= _max_evidence_lines():
            break
        if not isinstance(item, Mapping):
            continue
        origin = _trim(str(item.get("origin") or ""), cap)
        accepted = _trim(str(item.get("accepted") or ""), cap)
        context = _trim(str(item.get("context") or ""), cap)
        if origin or accepted:
            lines.append(f"- **{origin}** -> **{accepted}**")
        elif context:
            lines.append(f"- {context}")
    return lines


def _format_rule_block(row: Mapping[str, Any]) -> str:
    category = str(row.get("category") or "SoftRule").strip()
    subcategory = str(row.get("subcategory") or "General").strip()
    try:
        score = float(row.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    rule = str(row.get("rule") or "").strip()
    parts = [
        f"### injected-rule - {_rule_type(row)}",
        f"**{category}** - `{subcategory}` (score {score:.2f})",
        "",
        rule,
    ]
    evidence = _evidence_lines(row)
    if evidence:
        parts.extend(["", "#### Evidence examples", *evidence])
    return "\n".join(parts).strip()


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    suffix = "...(truncated)"
    return text[: max(0, max_chars - len(suffix))].rstrip() + suffix


def render_soft_rules_prompt(
    module_key: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    max_chars: int = 8000,
) -> str:
    """Render matched soft-rule rows into a module-scoped prompt snippet."""
    selected = [row for row in rows if isinstance(row, Mapping)]
    if not selected:
        return DEFAULT_EMPTY_SOFT_RULES_MESSAGE

    selected = sorted(
        selected,
        key=lambda row: float(row.get("score") or 0.0)
        if str(row.get("score") or "").replace(".", "", 1).isdigit()
        else 0.0,
        reverse=True,
    )
    header = [
        "Apply the article-specific rules below only when they are directly relevant to the article and within this module's scope.",
        "- `hard` injected rules are mandatory when the source text clearly matches the rule.",
        "- `soft` injected rules are advisory and should be applied only when strongly supported by the source text.",
        "- Static Section 2 constraints remain in force. If an injected rule conflicts with this module's scope or negative constraints, do not apply it.",
        "- Evidence examples are examples only; do not output a correction unless the article itself contains the issue.",
    ]
    body = "\n\n".join([*header, *[_format_rule_block(row) for row in selected]])
    return _truncate(body, max_chars)
