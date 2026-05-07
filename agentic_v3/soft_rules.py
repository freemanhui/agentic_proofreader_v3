"""Deterministic soft-rule loading, filtering, routing, and rendering for V3."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import requests

from agentic_v3.prompt_snippets import render_soft_rules_prompt


logger = logging.getLogger(__name__)

SOFT_RULES_FETCH_TIMEOUT_S = 30
SOFT_RULE_MODULE_KEYS = ("country", "greater_china", "hyphenation", "style_grammar", "terminology")

_DEFAULT_DATA = Path(__file__).resolve().parent / "data" / "soft_rules_sample.json"
_LEGACY_DATA = Path(__file__).resolve().parents[1] / "agentic" / "data" / "soft_rules_sample.json"
_MIN_EVIDENCE_SUBSTRING_LEN = 10

_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "as",
        "by", "with", "from", "into", "through", "over", "after", "before", "between",
        "under", "above", "below", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could", "should",
        "may", "might", "must", "not", "no", "if", "then", "than", "that", "this",
        "these", "those", "it", "its", "they", "them", "their", "we", "our", "you",
        "your", "use", "using", "used", "avoid", "retain", "preserve", "when", "where",
        "while", "unless", "only", "such", "all", "each", "any", "some", "more", "most",
    }
)

_ROUTING_EXACT: dict[tuple[str, str], tuple[str, ...]] = {
    ("Grammar", "Plurals"): ("style_grammar",),
    ("Grammar", "Verb_Form"): ("style_grammar",),
    ("HouseStyle", "Compound_Spelling_Preferences"): ("hyphenation",),
    ("HouseStyle", "InstitutionalNames"): ("country", "terminology"),
    ("HouseStyle", "Time_Format"): ("style_grammar",),
    ("ProperNounCapitalization", "AcronymUsage"): ("country",),
    ("ProperNounCapitalization", "Event_Names"): ("country",),
    ("ProperNounCapitalization", "HyphenatedCompoundNouns"): ("hyphenation",),
    ("ProperNounCapitalization", "Job_Titles"): ("country",),
    ("ProperNounCapitalization", "OfficialTitles"): ("country",),
    ("ProperNounCapitalization", "ProperNounCapitalization"): ("country",),
    ("ProperNounCapitalization", "Proper_Nouns"): ("country",),
    ("Punctuation", "Commas"): ("style_grammar",),
    ("Punctuation", "Compound_Adjective_Hyphenation"): ("hyphenation",),
    ("Punctuation", "Dashes"): ("style_grammar",),
    ("Punctuation", "Quotation_Marks"): ("style_grammar",),
    ("PunctuationFormat", "ApostropheUsage"): ("style_grammar",),
    ("PunctuationFormat", "CompoundHyphenation"): ("hyphenation",),
    ("PunctuationFormat", "PunctuationFormat"): ("style_grammar",),
    ("Spelling", "Accented_Characters"): ("style_grammar",),
    ("Spelling", "British_Spelling_Variants"): ("style_grammar",),
    ("Spelling", "Typos"): ("style_grammar",),
    ("WordChoice", "Homophones"): ("style_grammar",),
    ("WordChoice", "Redundant_Words"): ("style_grammar",),
    ("WordChoice", "Synonyms"): ("style_grammar",),
}

_ROUTING_CATEGORY_DEFAULT: dict[str, tuple[str, ...]] = {
    "GreaterChina": ("greater_china",),
    "Grammar": ("style_grammar",),
    "HouseStyle": ("style_grammar",),
    "NumberFormat": ("style_grammar",),
    "ProperNounCapitalization": ("country",),
    "Punctuation": ("style_grammar",),
    "RedundancyRemoval": ("style_grammar",),
    "Spelling": ("style_grammar",),
    "WordChoice": ("style_grammar",),
}


def resolve_soft_rules_path() -> Optional[Path]:
    env = os.environ.get("AGENTIC_SOFT_RULES_PATH", "").strip()
    if env:
        path = Path(env).expanduser()
        return path if path.is_file() else None
    if _DEFAULT_DATA.is_file():
        return _DEFAULT_DATA
    return _LEGACY_DATA if _LEGACY_DATA.is_file() else None


def _parse_categories(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("soft rules JSON must be a list at top level")
    return [item for item in raw if isinstance(item, dict)]


def load_soft_rules_categories(path: Path) -> list[dict[str, Any]]:
    return _parse_categories(json.loads(path.read_text(encoding="utf-8")))


def load_soft_rules_categories_from_url(url: str) -> list[dict[str, Any]]:
    response = requests.get(url, timeout=SOFT_RULES_FETCH_TIMEOUT_S)
    response.raise_for_status()
    return _parse_categories(json.loads(response.text))


def _normalize_rule_type(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in {"hard", "soft"} else "soft"


def _evidence_strings_and_details(evidence: Any) -> tuple[list[str], list[dict[str, str]]]:
    details: list[dict[str, str]] = []
    if not isinstance(evidence, list):
        return [], []
    for item in evidence:
        if isinstance(item, str):
            text = item.strip()
            if text:
                details.append({"origin": "", "accepted": "", "context": text})
        elif isinstance(item, Mapping):
            details.append(
                {
                    "origin": str(item.get("origin") or "").strip(),
                    "accepted": str(item.get("accepted") or "").strip(),
                    "context": str(item.get("context") or "").strip(),
                }
            )

    strings: list[str] = []
    for detail in details:
        for value in (
            detail.get("context", ""),
            f"{detail.get('origin', '')} {detail.get('accepted', '')}".strip(),
            detail.get("origin", ""),
            detail.get("accepted", ""),
        ):
            if value:
                strings.append(value.lower())

    deduped: list[str] = []
    seen: set[str] = set()
    for value in strings:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped, details


def _flatten_rows(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for block in categories:
        category = str(block.get("category") or "").strip()
        subcategories = block.get("subcategories")
        if not category or not isinstance(subcategories, list):
            continue
        for sub in subcategories:
            if not isinstance(sub, Mapping):
                continue
            evidence, evidence_detail = _evidence_strings_and_details(sub.get("evidence"))
            try:
                score = float(sub.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            rows.append(
                {
                    "category": category,
                    "subcategory": str(sub.get("subcategory") or "").strip(),
                    "rule": str(sub.get("rule") or "").strip(),
                    "score": score,
                    "rule_type": _normalize_rule_type(sub.get("rule_type")),
                    "evidence": evidence,
                    "evidence_detail": evidence_detail,
                }
            )
    return rows


def _split_camel(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [part.lower() for part in spaced.split() if part]


def _tokens_from_text(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z0-9%]+", text.lower())
    return [token for token in raw if (len(token) >= 3 or token == "%") and token not in _STOPWORDS]


def _signals_for_row(row: Mapping[str, Any]) -> tuple[set[str], list[str]]:
    category = str(row.get("category") or "")
    subcategory = str(row.get("subcategory") or "")
    rule = str(row.get("rule") or "")
    tokens: list[str] = []
    tokens.extend(_split_camel(subcategory))
    tokens.extend(_tokens_from_text(subcategory))
    tokens.extend(_split_camel(category))
    tokens.extend(_tokens_from_text(category))
    tokens.extend(_tokens_from_text(rule))
    return {token for token in tokens if len(token) >= 3 or token == "%"}, list(row.get("evidence") or [])


def _prefilter_hit(
    article_norm: str,
    article_tokens: set[str],
    row_signals: set[str],
    evidence_norms: Sequence[str],
) -> bool:
    for evidence in evidence_norms:
        if len(evidence) >= _MIN_EVIDENCE_SUBSTRING_LEN and evidence in article_norm:
            return True
    if not row_signals:
        return False
    hits = sum(1 for token in row_signals if token in article_tokens)
    need = 1 if any(len(token) >= 6 for token in row_signals) else 2
    return hits >= need


def prefilter_rows(article_text: str, rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    article_norm = article_text.lower()
    article_tokens = set(_tokens_from_text(article_norm))
    kept: list[Mapping[str, Any]] = []
    for row in rows:
        signals, evidence = _signals_for_row(row)
        if _prefilter_hit(article_norm, article_tokens, signals, evidence):
            kept.append(row)
    return kept


def route_row_to_modules(row: Mapping[str, Any]) -> tuple[str, ...]:
    category = str(row.get("category") or "")
    subcategory = str(row.get("subcategory") or "")
    return _ROUTING_EXACT.get((category, subcategory)) or _ROUTING_CATEGORY_DEFAULT.get(category) or ("style_grammar",)


def _filtered_rows(article_text: str, path: Optional[Path] = None) -> list[Mapping[str, Any]]:
    url = os.environ.get("AGENTIC_SOFT_RULES_URL", "").strip()
    try:
        if url:
            categories = load_soft_rules_categories_from_url(url)
        else:
            resolved = path if path is not None else resolve_soft_rules_path()
            if resolved is None or not resolved.is_file():
                return []
            categories = load_soft_rules_categories(resolved)
    except (OSError, ValueError, json.JSONDecodeError, requests.RequestException) as exc:
        logger.warning("Failed to load soft rules: %s", exc)
        return []
    return prefilter_rows(article_text, _flatten_rows(categories))


def _rule_id(row: Mapping[str, Any]) -> str:
    raw = f"{row.get('category', '')}\0{row.get('subcategory', '')}\0{row.get('rule', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _empty_module_map(value: Any) -> dict[str, Any]:
    return {key: value() if callable(value) else value for key in SOFT_RULE_MODULE_KEYS}


def rows_by_module(article_text: str, path: Optional[Path] = None) -> dict[str, list[Mapping[str, Any]]]:
    buckets: dict[str, list[Mapping[str, Any]]] = _empty_module_map(list)
    for row in _filtered_rows(article_text, path):
        for module_key in route_row_to_modules(row):
            if module_key in buckets:
                buckets[module_key].append(row)
    return buckets


def _trace_for_buckets(buckets: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = _empty_module_map(list)
    for module_key, rows in buckets.items():
        if module_key not in out:
            continue
        for row in rows:
            out[module_key].append(
                {
                    "source": "soft_rule",
                    "rule_type": _normalize_rule_type(row.get("rule_type")),
                    "rule_family": str(row.get("category") or ""),
                    "subcategory": str(row.get("subcategory") or ""),
                    "instruction": str(row.get("rule") or ""),
                    "rule_id": _rule_id(row),
                    "evidence_pairs": [
                        {
                            "origin": str(item.get("origin") or ""),
                            "accepted": str(item.get("accepted") or ""),
                        }
                        for item in row.get("evidence_detail", [])
                        if isinstance(item, Mapping) and (item.get("origin") or item.get("accepted"))
                    ],
                }
            )
    return out


def compile_soft_rules_by_module(article_text: str, path: Optional[Path] = None) -> dict[str, str]:
    buckets = rows_by_module(article_text, path)
    return {
        key: render_soft_rules_prompt(key, buckets.get(key, []))
        for key in SOFT_RULE_MODULE_KEYS
    }


def compile_injected_rule_trace_by_module(
    article_text: str,
    path: Optional[Path] = None,
) -> dict[str, list[dict[str, Any]]]:
    return _trace_for_buckets(rows_by_module(article_text, path))


def empty_soft_rule_debug(enabled: bool, skipped_reason: str = "") -> dict[str, Any]:
    return {
        "enabled": enabled,
        "skipped": not enabled,
        "skipped_reason": skipped_reason,
        "summary": {
            "rule_counts": {key: 0 for key in SOFT_RULE_MODULE_KEYS},
            "byte_counts": {key: 0 for key in SOFT_RULE_MODULE_KEYS},
            "source_url": os.environ.get("AGENTIC_SOFT_RULES_URL", "").strip(),
            "source_path": os.environ.get("AGENTIC_SOFT_RULES_PATH", "").strip(),
        },
        "injected_rule_trace_by_module": {key: [] for key in SOFT_RULE_MODULE_KEYS},
        "rendered_prompt_by_module": {key: "" for key in SOFT_RULE_MODULE_KEYS},
    }


def compile_soft_rule_debug(article_text: str, *, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return empty_soft_rule_debug(False, "request_disabled")

    buckets = rows_by_module(article_text)
    rendered = {
        key: render_soft_rules_prompt(key, buckets.get(key, []))
        for key in SOFT_RULE_MODULE_KEYS
    }
    trace = _trace_for_buckets(buckets)
    return {
        "enabled": True,
        "skipped": False,
        "skipped_reason": "",
        "summary": {
            "rule_counts": {key: len(trace.get(key) or []) for key in SOFT_RULE_MODULE_KEYS},
            "byte_counts": {key: len((rendered.get(key) or "").encode("utf-8")) for key in SOFT_RULE_MODULE_KEYS},
            "source_url": os.environ.get("AGENTIC_SOFT_RULES_URL", "").strip(),
            "source_path": os.environ.get("AGENTIC_SOFT_RULES_PATH", "").strip(),
        },
        "injected_rule_trace_by_module": trace,
        "rendered_prompt_by_module": rendered,
    }
