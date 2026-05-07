"""
Tansa Agent — Deterministic Guideline Matcher
==============================================
Loads ``Tansa_guidelines_prod.csv``, searches for each ``Search for`` term
in the input text using **case-insensitive word-boundary** matching, and
returns a JSON report in the standard agent output format.

Output Schema:
    {
      "status": "Changes found" | "No Changes found",
      "category": "tansa",
      "notes_on_changes": [
        {
          "original": "<matched keyword from text>",
          "corrected": "<corresponding Entry from CSV>",
          "updated_context": "<full sentence containing the match>",
          "notes": "<corresponding Information from CSV>"
        }
      ]
    }

This runs *deterministically* (no LLM call) in parallel with the 5 LLM-based
specialist agents in the MQ distributor.
"""

from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TANSA_CSV = os.path.join(BASE_DIR, "Tansa_guidelines_prod.csv")

# ── Compiled sentence-boundary pattern ──────────────────────────────────
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")

# ── Cache for loaded guidelines (module-level) ─────────────────────────
_TANSA_GUIDELINES_CACHE: list[dict[str, str]] | None = None


# ═══════════════════════════════════════════════════════════════════════
# 1.  Load Tansa guidelines from CSV
# ═══════════════════════════════════════════════════════════════════════


def _load_tansa_guidelines() -> list[dict[str, str]]:
    """Load & cache Tansa guidelines.

    Returns a list of dicts with keys ``search_for``, ``entry``, ``information``.
    Filters out rows where ``search_for`` or ``entry`` is empty.
    """
    global _TANSA_GUIDELINES_CACHE
    if _TANSA_GUIDELINES_CACHE is not None:
        return _TANSA_GUIDELINES_CACHE

    csv_path = Path(TANSA_CSV)
    if not csv_path.is_file():
        print(f"  [TansaAgent] WARNING: CSV not found at {TANSA_CSV}")
        _TANSA_GUIDELINES_CACHE = []
        return _TANSA_GUIDELINES_CACHE

    guidelines: list[dict[str, str]] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            search_for = (row.get("Search for") or "").strip()
            entry = (row.get("Entry") or "").strip()
            information = (row.get("Information") or "").strip()
            if search_for and entry:
                guidelines.append({
                    "search_for": search_for,
                    "entry": entry,
                    "information": information,
                })

    # Sort by length (longest first) so a longer term is never shadowed
    # by a shorter prefix match inside the same word.
    guidelines.sort(key=lambda g: len(g["search_for"]), reverse=True)

    print(f"  [TansaAgent] Loaded {len(guidelines)} guidelines from CSV")
    _TANSA_GUIDELINES_CACHE = guidelines
    return guidelines


# ═══════════════════════════════════════════════════════════════════════
# 2.  Sentence extraction helper
# ═══════════════════════════════════════════════════════════════════════


def _extract_sentence(text: str, match_start: int, match_end: int) -> str:
    """Extract the full sentence containing the match range.

    Splits on ``.`` / ``!`` / ``?`` followed by whitespace, then returns
    the sentence that fully contains ``[match_start, match_end)``.
    """
    if not text:
        return ""

    # Find sentence boundaries
    boundaries = [m.end() for m in _SENTENCE_BOUNDARY_RE.finditer(text)]
    boundaries = [0] + boundaries + [len(text)]

    # Find the sentence containing the match
    for i in range(len(boundaries) - 1):
        sent_start = boundaries[i]
        sent_end = boundaries[i + 1]
        if sent_start <= match_start and match_end <= sent_end:
            sentence = text[sent_start:sent_end].strip()
            return sentence

    # Fallback: return surrounding context
    context_start = max(0, match_start - 100)
    context_end = min(len(text), match_end + 100)
    return text[context_start:context_end].strip()


# ═══════════════════════════════════════════════════════════════════════
# 3.  Main Tansa matching logic
# ═══════════════════════════════════════════════════════════════════════


def run_tansa_agent(input_text: str) -> str:
    """Deterministic Tansa guideline matching.

    For each guideline in the CSV:
      1. Case-insensitively find all occurrences of ``search_for`` in text.
      2. For each match, record:
         - ``original``:       the matched keyword (preserving original casing)
         - ``corrected``:      the CSV ``Entry`` value
         - ``updated_context``: the full sentence containing the match
         - ``notes``:          the CSV ``Information`` value

    Returns a JSON string conforming to the standard agent output schema.

    Args:
        input_text: The full news article text to audit.

    Returns:
        JSON string with ``status``, ``category``, ``notes_on_changes``.
    """
    guidelines = _load_tansa_guidelines()
    if not guidelines:
        return json.dumps({
            "status": "No Changes found",
            "category": "tansa",
            "notes_on_changes": [],
        }, ensure_ascii=False)

    # Track matched positions to avoid duplicate reports from overlapping
    # search terms (e.g. "a trained doctor" and "a doctor" matching the
    # same span).
    matched_positions: set[tuple[int, int]] = set()
    notes: list[dict[str, str]] = []

    for guideline in guidelines:
        search_for = guideline["search_for"]
        entry = guideline["entry"]
        information = guideline["information"]

        # Case‑sensitive word-boundary matching
        # Use word boundaries to avoid partial matches (e.g. "ten" matching
        # inside "tenant", "ink" matching inside "Blinken").
        # Matching respects the exact casing in the CSV "Search for" column.
        pattern = re.compile(r"\b" + re.escape(search_for) + r"\b")


        for match in pattern.finditer(input_text):
            span = (match.start(), match.end())

            # Skip if this exact position was already matched by a longer term
            if span in matched_positions:
                continue

            matched_positions.add(span)
            matched_text = input_text[span[0]:span[1]]

            # Extract the full sentence
            sentence = _extract_sentence(input_text, span[0], span[1])

            notes.append({
                "original": matched_text,
                "corrected": entry,
                "updated_context": sentence or matched_text,
                "notes": information,
            })

    if notes:
        print(f"  [TansaAgent] Found {len(notes)} Tansa guideline matches")
        return json.dumps({
            "status": "Changes found",
            "category": "tansa",
            "notes_on_changes": notes,
        }, ensure_ascii=False)
    else:
        return json.dumps({
            "status": "No Changes found",
            "category": "tansa",
            "notes_on_changes": [],
        }, ensure_ascii=False)


# ────────────────────────────────────────────────────────────────────────
# CLI test
# ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        text = sys.argv[1]
    else:
        text = (
            "The SCMP reported that US secretary of state Antony Blinken "
            "met with ASEAN leaders in a high-tech conference. "
            "The event was co-sponsored by NATO and the BBC. "
            "Discussions focused on regional security and economic cooperation."
        )

    result = run_tansa_agent(text)
    print(result)
