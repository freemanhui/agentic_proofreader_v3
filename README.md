# 📝 ProofReader Rewrite

> **AI-Powered Editorial Proofreading System** — LangGraph + MQ hybrid architecture with optional **article-specific soft rules**

An intelligent proofreading system that audits news articles, identifies editorial issues, and suggests corrections. It combines **6 specialist agents** (5 LLM-based + 1 deterministic Tansa guideline matcher) in a **LangGraph** pipeline with **message queue (MQ) parallelism** for large-text processing. **Soft rules** are opt-in: when enabled, a dedicated node compiles rules once from the cleaned full article and injects **per-module** snippets into the five LLM specialist prompts on every chunk (Tansa and the General consolidation agent do not receive them).

---

## Table of Contents

- [Architecture](#architecture)
- [Soft rules (opt-in)](#soft-rules-opt-in)
- [Pipeline nodes](#pipeline-nodes)
  - [0. Preprocessing](#0-preprocessing-node)
  - [1. Soft rules](#1-soft-rules-node)
  - [2. Chunking](#2-chunking-node)
  - [3. MQ distributor](#3-mq-distributor-node)
  - [4. General agent](#4-general-agent-node)
  - [5. Duplicate word filter](#5-duplicate-word-filter-node)
  - [6. Postprocessor](#6-postprocessor-node)
  - [7. Whitelist filter](#7-whitelist-filter-node)
- [6 specialist agents](#6-specialist-agents)
- [Project structure](#project-structure)
- [Setup & installation](#setup--installation)
- [Running the services](#running-the-services)
  - [Gradio (v1 — inspect)](#gradio-app-v1--inspect-mode)
  - [Gradio (v2 — interactive)](#gradio-app-v2--interactive-mode)
  - [Portal API (FastAPI)](#portal-api-fastapi)
- [Output format](#output-format)
- [Configuration](#configuration)
  - [Environment variables](#environment-variables)
  - [Soft rules sources](#soft-rules-sources)
  - [Whitelist](#whitelist)
  - [Tansa guidelines](#tansa-guidelines)
  - [System prompts](#system-prompts)
- [FAQ](#faq)

---

## Architecture

Graph (root workflow in `proofreader_workflow.py`):

`preprocessing → soft_rules → chunking → mq_distributor → general_agent → duplicate_word_filter → postprocessor → whitelist_filter`

```
                        ┌─────────────────────────────────────────┐
                        │              Input Text                 │
                        └────────────────────┬────────────────────┘
                                             ▼
                    ┌──────────────────────────────────────────────┐
         Node 0     │          Preprocessing Node                  │
                    │  TextPreprocessor: noise / invisible / dup   │
                    └────────────────────────┬─────────────────────┘
                                             ▼
                    ┌──────────────────────────────────────────────┐
         Node 1     │            Soft Rules Node                   │
                    │  Load, prefilter, route, render, trace       │
                    │  (full cleaned article → per-module payload)   │
                    └────────────────────────┬─────────────────────┘
                                             ▼
                    ┌──────────────────────────────────────────────┐
         Node 2     │             Chunking Node                   │
                    │  NewsChunker: paragraph → sentence → token   │
                    └────────────────────────┬─────────────────────┘
                                             ▼
         ┌──────────────────────────────────────────────────────────┐
         │                 MQ Distributor Node (Node 3)              │
         │              queue.Queue, max 5 concurrent              │
         │    W1 … W5 → 6 agents each: 5 LLM + Tansa                 │
         │    (5 LLM modules receive per-chunk soft-rule snippets)   │
         └──────────────────────────────┬───────────────────────────┘
                                        ▼
                    ┌──────────────────────────────────────────────┐
         Node 4     │           General Agent Node                 │
                    │      Consolidates all agents → JSON array     │
                    └────────────────────────┬─────────────────────┘
                                             ▼
                    ┌──────────────────────────────────────────────┐
         Node 5     │        Duplicate Word Filter Node            │
                    └────────────────────────┬─────────────────────┘
                                             ▼
                    ┌──────────────────────────────────────────────┐
         Node 6     │            Postprocessor Node                │
                    └────────────────────────┬─────────────────────┘
                                             ▼
                    ┌──────────────────────────────────────────────┐
         Node 7     │          Whitelist Filter Node               │
                    └────────────────────────┬─────────────────────┘
                                             ▼
                    ┌──────────────────────────────────────────────┐
                    │            Final output (JSON)                │
                    └──────────────────────────────────────────────┘
```

**Design choices:** Behaviour matches the soft-rules integration plan; the per-task Markdown plan stays **local-only** (`agentic_v3/soft_rules_integration_plan.md` is not tracked—copy it separately if you want it beside this repo).

- Exactly **one** new LangGraph node (`soft_rules`) after preprocessing; chunking and downstream behaviour stay the same when soft rules are off.
- **Compile once** per article after preprocessing; **inject per module** for each chunk worker (no repeated source fetch/filter work per chunk).
- **Default:** soft rules **disabled** unless `run_proofreader(..., enable_soft_rules=True)` or the portal sends `enable_soft_rules: true`.
- **Prompt injection:** if a prompt contains `{{soft_rules}}`, it is substituted; otherwise an **Article-Specific Soft Rules** section is inserted before the **Output Requirements** heading when possible.
- **Observability:** the API returns structured `soft_rules_debug` (trace + rendered snippets by module); **raw source JSON is not** injected into LLM prompts—only rendered snippets.

---

## Soft rules (opt-in)

| Topic | Detail |
|--------|--------|
| **Modules receiving snippets** | `country`, `greater_china`, `hyphenation`, `style_grammar`, `terminology` |
| **Excluded from injection** | Tansa (deterministic), General consolidation agent |
| **Runtime package** | `agentic_v3/soft_rules.py`, `agentic_v3/prompt_snippets.py` |
| **Portal** | Toggle sends `enable_soft_rules`; response includes `soft_rules_debug` |

Two outputs from the soft-rule pipeline:

- **`rendered_prompt_by_module`** — text injected into that module’s system prompt.
- **`injected_rule_trace_by_module`** — structured rows for the UI / API (categories, hard/soft, evidence); not pasted into model prompts as raw JSON.

Optional caps (see [Configuration](#configuration)):

- `AGENTIC_SOFT_RULES_MAX_EVIDENCE_LINES`
- `AGENTIC_SOFT_RULES_EVIDENCE_CONTEXT_MAX_CHARS`

---

## Pipeline nodes

### 0. Preprocessing node
**Source:** `text_preprocessor.py`

Cleans input text before processing:

- Removes screen reader / system noise artifacts
- Strips **invisible characters** (zero-width spaces, word joiners, BOM: `\u200b-\u200f`, `\u2060-\u206f`, `\ufeff`)
- Compresses excessive newlines (3+ → 2)
- **Deduplicates** identical paragraphs

### 1. Soft rules node
**Source:** `proofreader_workflow.py` (`soft_rules_node`) and `agentic_v3/soft_rules.py`

When enabled, loads soft-rule JSON (URL or local path via env), prefilters to article-relevant rows, routes rows to modules, renders per-module instruction snippets, and builds trace data for the portal. When disabled, passes through empty payloads with a skip reason.

### 2. Chunking node
**Source:** `chunking.py` | **Class:** `NewsChunker`

Recursive three-level chunking:

| Level | Method | Description |
|-------|--------|-------------|
| **Paragraph** | `split_paragraphs()` | Split on double newlines |
| **Sentence** | `split_into_sentences()` | Split on Chinese/English punctuation (`。！？；：.!?;:`) |
| **Token** | `merge_sentences_into_chunks()` | Merge sentences up to `max_chunk_size` (~350 tokens), with sentence overlap |

**Default sizes:** `min=200`, `target=256`, `max=350`, `overlap=50` tokens.

### 3. MQ distributor node
**Source:** `proofreader_workflow.py`

- Uses `queue.Queue` as a **message queue**
- **Max 5 concurrent workers** (configurable via `MAX_CONCURRENT_CHUNKS`)
- Each worker runs **all 6 agents** in parallel on its chunk
- The five LLM specialists receive **injected soft-rule sections** when enabled; Tansa does not
- Aggregates per-agent results across all chunks after completion

### 4. General agent node
**Source:** `proofreader_workflow.py` | **Prompt:** `general-task-system-prompt.md`

The **final consolidation** step:

1. Takes all agent outputs as input
2. Applies a **hierarchy of truth**: `Tansa (Level 1) > Lock List (Level 2) > Specialists (Level 3)`
3. Enforces editorial rules (parentheses policy, typography, house style, anti-overcorrection)
4. Outputs a **deduplicated JSON array** of changes

Does **not** receive the dynamic soft-rule block (per design).

### 5. Duplicate word filter node
Removes entries where the `corrected` text contains duplicate words (e.g., "the the", "a a").

### 6. Postprocessor node
**Source:** `postprocessor.py` | **Function:** `postprocess_general_result()`

- **JSON path** (preferred): Parse JSON array → strip no-ops → deduplicate (merging notes)
- **Legacy text path** (fallback): Parse markdown "Notes on Changes:" format → filter noise/no-ops/duplicates

### 7. Whitelist filter node
**Source:** `whitelist.yaml`

Removes any change whose `original` field matches a whitelisted term (**exact** or **regex** via `re.fullmatch()`). Runs last and logs match diagnostics.

---

## 6 specialist agents

| Agent | Prompt file | Description |
|-------|-------------|-------------|
| 🌍 **Country** | `country-task-system-prompt.md` | Country-specific editorial guidelines |
| 🇨🇳 **Greater China** | `greater-china-task-system-prompt.md` | Greater China region terminology |
| 🔗 **Hyphenation** | `hyphenation-task-system-prompt.md` | Prefix/hyphenation rules |
| ✍️ **Style & grammar** | `style-grammar-task-system-prompt.md` | General style and grammar |
| 📖 **Terminology** | `terminology-task-system-prompt.md` | Specialised terminology |
| 📋 **Tansa** *(deterministic)* | `tansa_agent.py` | Rule-based matcher over **5,673** rows in `Tansa_guidelines_prod.csv` (no LLM) |

The five LLM specialists use **Qwen-Flash** via DashScope. **Soft rules** apply only to those five, not to Tansa.

---

## Project structure

Layout for the **root** workflow and `portal_api.py`:

```
agentic_proofreader_v3/
├── .env.example
├── .gitignore
├── README.md
├── requirements.txt
│
├── agentic_v3/                    # Soft rules (imported by proofreader_workflow)
│   ├── __init__.py
│   ├── soft_rules.py
│   └── prompt_snippets.py
│
├── proofreader_workflow.py        # LangGraph + MQ (8 nodes including soft_rules)
├── proofreader_langfuse.py        # Langfuse prompt fetch / logging helpers
├── chunking.py
├── text_preprocessor.py
├── postprocessor.py
├── tansa_agent.py
├── portal_api.py                  # FastAPI + embedded portal UI
├── gradio_app.py                  # Gradio v1 (inspect)
├── gradio_new.py                  # Gradio v2 (interactive)
├── Tansa_guidelines_prod.csv
├── whitelist.yaml
│
├── general-task-system-prompt.md
├── country-task-system-prompt.md
├── greater-china-task-system-prompt.md
├── hyphenation-task-system-prompt.md
├── style-grammar-task-system-prompt.md
├── terminology-task-system-prompt.md
│
└── conftest.py                    # Pytest path bootstrap (optional local tests)
```

---

## Setup & installation

### Prerequisites

- Python 3.11+
- `DASHSCOPE_API_KEY` for Qwen-Flash

### Install

```bash
cd agentic_proofreader_v3
pip install -r requirements.txt
```

### Environment

```bash
cp .env.example .env
```

Edit `.env` with your keys and optional soft-rule settings. `.env` is git-ignored.

---

## Running the services

### Gradio app (v1 — inspect mode)

```bash
cd agentic_proofreader_v3
python3 gradio_app.py
```

- **Local:** http://localhost:7860

### Gradio app (v2 — interactive mode)

```bash
cd agentic_proofreader_v3
python3 gradio_new.py
```

- **Local:** http://localhost:7861

### Portal API (FastAPI)

```bash
cd agentic_proofreader_v3
python3 portal_api.py
# or
uvicorn portal_api:app --host 0.0.0.0 --port 8001 --reload
```

- **Portal:** http://localhost:8001
- **API:** `POST /api/proofread` with JSON body:

```json
{
  "text": "Your article…",
  "enable_soft_rules": false
}
```

Response includes proofreading results plus **`soft_rules_debug`** (summary, per-module traces, rendered snippet lengths) when soft rules are enabled or the structure is still returned for the UI.

---

## Output format

The workflow produces a **JSON array** of changes:

```json
[
  {
    "original": "colour",
    "corrected": "colour",
    "category": "Spelling and Language Variety",
    "note": "British English spelling is correct; no change needed.",
    "updated_context": "The article uses British English colour throughout."
  }
]
```

| Field | Description |
|-------|-------------|
| `original` | Text matched in the source |
| `corrected` | Suggested replacement |
| `category` | Editorial category |
| `note` | Rationale |
| `updated_context` | Sentence-level context (e.g. for fuzzy matching in Gradio v2) |

---

## Configuration

### Environment variables

| Variable | Purpose |
|----------|---------|
| `DASHSCOPE_API_KEY` | Required for LLM calls (Qwen-Flash) |
| `PROOFREADER_GENERAL_USER_MAX_CHARS` | Optional cap on general-agent user payload size (default `200000`) |
| `AGENTIC_SOFT_RULES_URL` | Optional HTTP(S) URL for soft-rule JSON |
| `AGENTIC_SOFT_RULES_PATH` | Optional filesystem path for soft-rule JSON |
| `AGENTIC_SOFT_RULES_MAX_EVIDENCE_LINES` | Caps evidence lines in rendered snippets (integer) |
| `AGENTIC_SOFT_RULES_EVIDENCE_CONTEXT_MAX_CHARS` | Caps evidence character width per line |

Langfuse-related keys follow the standard Langfuse SDK env names if you use hosted prompts (`proofreader_langfuse.py`).

### Soft rules sources

Provide **either** `AGENTIC_SOFT_RULES_URL` **or** `AGENTIC_SOFT_RULES_PATH` when using soft rules. The loader understands the same JSON shape as the legacy v2 utilities.

### Whitelist

**File:** `whitelist.yaml`

- **`whitelist`:** exact case-sensitive strings
- **`whitelist_regex`:** patterns matched with `re.fullmatch()`

### Tansa guidelines

**File:** `Tansa_guidelines_prod.csv` — columns include search term, replacement, and notes. Loaded with longest-match-first, case-sensitive word-boundary matching.

### System prompts

Six markdown files at the repo root configure the five specialists plus prompts managed through `proofreader_langfuse.py` where applicable.

---

## FAQ

**Q: What LLM is used?**  
A: Qwen-Flash via DashScope International (`dashscope-intl.aliyuncs.com`).

**Q: What changed with soft rules?**  
A: One new node after preprocessing compiles rules from the **full cleaned article**, then MQ workers attach **module-specific** snippets to the five LLM prompts only. Defaults stay the same when soft rules are off.

**Q: Why two Gradio apps?**  
A: v1 exposes per-agent output for debugging; v2 adds accept/reject with fuzzy sentence matching.

**Q: How does MQ parallelism work?**  
A: Chunks go through a `queue.Queue`; up to five workers process chunks concurrently, each running all six agents in parallel on its chunk.

**Q: What is a postprocessor “no-op”?**  
A: An entry where `original == corrected`; these are stripped from the final list.
