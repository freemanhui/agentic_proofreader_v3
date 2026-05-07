"""
Postprocessor Module
====================
Cleans and filters the General Agent's JSON / text output.

Faithfully ported from:
    editorial-sub-editing-assistant-backend/app/agentic/nodes/postprocessor.py

Processing order (mirrors the backend ``postprocessor_node``):
  1. Try JSON change-list path (``_try_parse_change_list``)
     a. Strip no-ops       (``_strip_json_noop_changes``)
     b. Deduplicate rows   (``_dedupe_json_change_list``, merging notes)
     c. Convert to notes   (``_change_list_to_notes``)
  2. Fall back to legacy text path (``_process_text_changes``)
     a. Strip ``---`` separators and markdown fences
     b. Split on "Notes on Changes:"
     c. Parse bullet-format change notes
     d. Filter no-ops, noise, and duplicates
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── Noise / no-change phrases (exact copies from backend) ────────────
_NO_CHANGE_PHRASES = ["No changes made", "No applicable changes"]
_NOISE_LOWER = [
    "already correct",
    "no change needed",
    "no change",
    "[none present]",
    "[no instance found]",
    "[no changes found]",
    "not found in text",
    "no instances appear",
    "reiterated",
]


# ─────────────────────────────────────────────────────────────────────
# 1.  JSON change-list path  (changes-only consolidation mode)
# ─────────────────────────────────────────────────────────────────────


def _try_parse_change_list(raw: str) -> list[dict[str, str]] | None:
    """Attempt to parse the consolidation output as a JSON change array.

    Returns the parsed list on success, or None to fall back to legacy parsing.
    (Direct port of backend ``_try_parse_change_list``.)
    """
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    if not text.startswith("["):
        return None

    try:
        data = json.loads(text)
        if isinstance(data, list) and all(isinstance(item, dict) for item in data):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _strip_json_noop_changes(change_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove no-op entries (``strip(original) == strip(corrected)``).

    Does not use case-folding: ``NATO`` → ``Nato`` remains a real edit.
    (Direct port of backend ``_strip_json_noop_changes``.)
    """
    out: list[dict[str, Any]] = []
    for change in change_list:
        if not isinstance(change, dict):
            continue
        o = str(change.get("original", "") or "").strip()
        c = str(change.get("corrected", "") or "").strip()
        if o == c:
            continue
        out.append(change)
    return out


def _merge_change_notes(existing: str, incoming: str) -> str:
    """Merge duplicate-row notes; avoids losing distinct rationale.

    (Direct port of backend ``_merge_change_notes``.)
    """
    a = (existing or "").strip()
    b = (incoming or "").strip()
    if not b:
        return a
    if not a:
        return b
    if a == b:
        return a
    if b.casefold() in a.casefold():
        return a
    if a.casefold() in b.casefold():
        return b
    return f"{a}; {b}"


def _dedupe_json_change_list(change_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First occurrence wins; later duplicates merge ``note`` onto the kept row.

    (Direct port of backend ``_dedupe_json_change_list``.)
    """
    order: list[tuple[str, str, str]] = []
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for ch in change_list:
        if not isinstance(ch, dict):
            continue
        cat = str(ch.get("category", "General") or "General").strip() or "General"
        orig_raw = str(ch.get("original", "") or "")
        corr_raw = str(ch.get("corrected", "") or "")
        o = orig_raw.strip()
        c = corr_raw.strip()
        if not o:
            continue
        key = (cat, o, c)
        if key not in buckets:
            buckets[key] = dict(ch)
            order.append(key)
        else:
            buckets[key]["note"] = _merge_change_notes(
                str(buckets[key].get("note", "") or ""),
                str(ch.get("note", "") or ""),
            )
    return [buckets[k] for k in order]


def _change_list_to_notes(changes: list[dict[str, str]]) -> dict[str, Any]:
    """Convert the JSON change array into the categorised notes format.

    (Direct port of backend ``_change_list_to_notes``.)
    """
    categories: dict[str, list[dict[str, str]]] = {}
    for change in changes:
        cat = change.get("category", "General")
        key = f"**{cat}:**"
        if key not in categories:
            categories[key] = []
        categories[key].append({
            "original": change.get("original", ""),
            "corrected": change.get("corrected", ""),
            "note": change.get("note", ""),
        })
    return categories


# ─────────────────────────────────────────────────────────────────────
# 2.  Legacy text path  ("Corrected Text + Notes on Changes")
# ─────────────────────────────────────────────────────────────────────


def _process_text_changes(
    request_context: str,
    corrected_text: str,
    changes_raw: str,
) -> dict[str, Any]:
    """Parse, filter, and format editorial change notes.

    (Direct port of backend ``_process_text_changes``.)
    """
    lines = changes_raw.strip().split("\n")
    parsed_categories: list[dict[str, Any]] = []
    current_category: dict[str, Any] | None = None
    current_item: dict[str, Any] | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("**") and stripped.endswith("**"):
            if current_category:
                parsed_categories.append(current_category)
            current_category = {"title": stripped, "items": []}
            current_item = None

        elif stripped.startswith("• ") and "→" in stripped:
            try:
                before_part, after_part = stripped.split("→", 1)
                before_term = before_part.replace("•", "", 1).strip()
                after_term = after_part.strip()
                current_item = {"before": before_term, "after": after_term, "note": ""}
                if current_category:
                    current_category["items"].append(current_item)
            except ValueError:
                continue

        elif stripped.startswith("• Note:"):
            if current_item:
                current_item["note"] = stripped[len("• Note:") :].strip()

    if current_category:
        parsed_categories.append(current_category)

    # Filter: remove no-ops, noise, and duplicates
    filtered: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for category in parsed_categories:
        kept: list[dict[str, Any]] = []
        for item in category["items"]:
            before_clean = item["before"].strip("*").strip()
            after_clean = item["after"].strip("*").strip()
            if before_clean == after_clean:
                continue
            combined_lower = (item["before"] + item["after"] + item.get("note", "")).lower()
            if any(p in combined_lower for p in _NOISE_LOWER):
                continue
            if any(p in item["before"] for p in _NO_CHANGE_PHRASES) or any(
                p in item["after"] for p in _NO_CHANGE_PHRASES
            ):
                continue
            dedup_key = f"{before_clean}|{after_clean}"
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            if (before_clean not in request_context) and (
                after_clean not in corrected_text
            ):
                continue
            kept.append(item)
        if kept:
            filtered.append({"title": category["title"], "items": kept})

    result_notes: dict[str, list[dict[str, str]]] = {}
    for cat in filtered:
        result_notes[cat["title"]] = [
            {"original": it["before"], "corrected": it["after"], "note": it["note"]}
            for it in cat["items"]
        ]
    return result_notes


# ─────────────────────────────────────────────────────────────────────
# 3.  Main postprocessing entry point
# ─────────────────────────────────────────────────────────────────────


def postprocess_general_result(general_raw: str, request_context: str = "") -> dict:
    """
    Postprocess the General Agent's output.

    Attempts JSON change-list path first; falls back to legacy text parsing.

    Args:
        general_raw: Raw output from the General Agent (JSON array or legacy text).
        request_context: Original input text (used for context validation in legacy path).

    Returns:
        dict with keys:
            - postprocessed_result (str):   Cleaned JSON array string (JSON path)
                                             or legacy notes dict repr (legacy path).
            - final_changes (dict):          Categorised change notes.
            - parse_path (str):              "json_change_list" or "legacy_text".
            - postprocessor_removed (int):   Number of entries removed.
            - postprocessor_input_count (int):  Entries before filtering.
            - postprocessor_output_count (int): Entries after filtering.
            - postprocessor_notes (dict):    Same as final_changes (for convenience).
    """
    if not general_raw or general_raw.startswith("[ERROR"):
        return {
            "postprocessed_result": general_raw,
            "final_changes": {},
            "postprocessor_notes": {},
            "parse_path": "error",
            "postprocessor_removed": 0,
            "postprocessor_input_count": 0,
            "postprocessor_output_count": 0,
        }

    # ── Step 1: Try JSON change-list path ────────────────────────────
    change_list = _try_parse_change_list(general_raw)
    parse_path = "json_change_list" if change_list is not None else "legacy_text"

    if change_list is not None:
        input_count = len(change_list)

        # Strip no-ops
        change_list = _strip_json_noop_changes(change_list)

        # Deduplicate
        change_list = _dedupe_json_change_list(change_list)

        output_count = len(change_list)
        removed = input_count - output_count

        final_changes = _change_list_to_notes(change_list)
        result_json = json.dumps(change_list, ensure_ascii=False)

        if removed > 0:
            print(f"  [Postprocessor] Removed {removed} entries "
                  f"({input_count} → {output_count}): "
                  f"no-ops and/or duplicates filtered out (JSON path)")

        return {
            "postprocessed_result": result_json,
            "final_changes": final_changes,
            "postprocessor_notes": final_changes,
            "parse_path": parse_path,
            "postprocessor_removed": removed,
            "postprocessor_input_count": input_count,
            "postprocessor_output_count": output_count,
        }

    # ── Step 2: Legacy text path ──────────────────────────────────────
    print("  [Postprocessor] Falling back to legacy text parsing")
    raw = general_raw
    raw = re.sub(r"(?<!-)---(?!-)", "", raw)
    raw = re.sub(r"---$", "", raw)
    match = re.search(r"```json(.*?)```", raw, re.DOTALL)
    if match:
        raw = match.group(1).strip()

    parts = raw.split("Notes on Changes:", 1)
    corrected_text = parts[0]
    changes_raw = parts[1] if len(parts) > 1 else ""

    if corrected_text.endswith("**"):
        corrected_text = corrected_text[:-2]
    if changes_raw.startswith("**"):
        changes_raw = changes_raw[2:]
    corrected_text = corrected_text.replace("**Corrected Text:**\n", "").strip()

    final_changes = _process_text_changes(
        request_context or corrected_text,
        corrected_text,
        changes_raw,
    )

    input_count = sum(len(items) for items in final_changes.values())
    output_count = input_count  # For legacy path, counting after parsing
    removed = 0

    return {
        "postprocessed_result": str(final_changes),
        "final_changes": final_changes,
        "postprocessor_notes": final_changes,
        "parse_path": parse_path,
        "postprocessor_removed": removed,
        "postprocessor_input_count": input_count,
        "postprocessor_output_count": output_count,
    }
