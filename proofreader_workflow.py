"""
ProofReader Rewrite Workflow
=============================
langgraph + MQ 混合架构:

  0. Preprocessing Node: advanced_preprocessing 清洗输入文本
  1. Chunking Node: NewsChunker 递归分块 (paragraph → sentence → token)
  2. MQ Node: queue.Queue 消息队列分发 (max 5 路并发 chunk, 每路 6 agent 并发)
  3. General Agent Node: 最终整合
  4. Duplicate Word Filter Node: 移除重复词条目
  5. Postprocessor Node: 去 noop/noise/重复
  6. Whitelist Filter Node: 白名单过滤

Architecture:
    Input Text → [Preprocessing Node] → [Chunking Node] → [MQ Node] → [General Node] → [DupWordFilter] → [Postprocessor] → [Whitelist Filter] → Output
                        ↓                       ↓              ↓              ↓
              advanced_preprocessing       NewsChunker     queue.Queue      call_llm
              清洗噪声/不可见字符           递归分块        5 Workers x 6 Agents
"""

import os
import json
import re
import time
import queue
import threading
import concurrent.futures
import contextvars
import functools
from pathlib import Path
from typing import TypedDict, Optional, Any, Dict, List

import yaml
import requests
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from langfuse import get_client, observe

from chunking import NewsChunker
from agentic_v3.prompt_snippets import DEFAULT_EMPTY_SOFT_RULES_MESSAGE
from agentic_v3.soft_rules import (
    SOFT_RULE_MODULE_KEYS,
    compile_soft_rule_debug,
    empty_soft_rule_debug,
)
from postprocessor import postprocess_general_result
from proofreader_langfuse import (
    LANGFUSE_ROLE_PROMPT_BY_AGENT,
    LANGFUSE_TASK_PROMPT_BY_AGENT,
    fetch_text_prompt_client_for_generation,
    langfuse_log_system_prompt_max_chars,
)
from tansa_agent import run_tansa_agent
from text_preprocessor import advanced_preprocessing


# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
load_dotenv(Path(__file__).resolve().parent / ".env")

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
CHAT_MODEL = "qwen-flash"
DASHSCOPE_ENDPOINT = "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/text-generation/generation"

# General-agent payload is huge (full text + six merged specialist outputs); DashScope returns HTTP 400
# when over context / platform limits. Override via env if you still see 400 after truncation.
def _general_user_max_chars() -> int:
    raw = os.environ.get("PROOFREADER_GENERAL_USER_MAX_CHARS", "200000").strip()
    try:
        return max(8192, int(raw))
    except ValueError:
        return 200_000


def _truncate_middle(text: str, max_chars: int) -> tuple[str, bool]:
    """If text exceeds max_chars, keep start + end with an omission marker."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False

    overhead = 96  # room for omission line (+ small slack for digit growth)
    if max_chars <= overhead + 200:
        return text, False
    budget = max_chars - overhead
    head = budget // 2
    tail = budget - head
    omitted = len(text) - head - tail
    if omitted <= 0:
        return text, False

    banner_tmpl = "\n\n[... omitted {:d} characters ...]\n\n"

    def _fit(h: int, t: int) -> tuple[int, str, int]:
        om = len(text) - h - t
        if om <= 0:
            return 0, "", 0
        b = banner_tmpl.format(om)
        return h, b, t

    banner = ""
    for _ in range(len(text)):  # always terminates as we shrink
        h2, banner, t2 = _fit(head, tail)
        if not banner:
            break
        if h2 + len(banner) + t2 <= max_chars:
            return text[:h2] + banner + text[-t2:], True
        if head >= tail > 80:
            tail -= 1
        elif head > 80:
            head -= 1
        else:
            break
    return text, False


def _dashscope_error_detail(resp: requests.Response, limit: int = 1500) -> str:
    """Extract API error text for logs (DashScope JSON or raw body)."""
    try:
        body = resp.json()
        if isinstance(body, dict):
            parts = []
            if body.get("request_id"):
                parts.append(f"request_id={body['request_id']}")
            if body.get("code"):
                parts.append(f"code={body['code']}")
            msg = body.get("message")
            if msg:
                parts.append(str(msg))
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                parts.append(str(err["message"]))
            if parts:
                return " | ".join(parts)[:limit]
    except Exception:
        pass
    return (resp.text or "")[:limit]

# ── Whitelist config (extended with regex support) ──
from dataclasses import dataclass, field


@dataclass
class WhitelistConfig:
    """白名单配置，同时支持精确匹配和正则匹配。"""
    exact_set: set[str] = field(default_factory=set)
    regex_patterns: list[re.Pattern] = field(default_factory=list)

    def is_whitelisted(self, original: str) -> bool:
        """检查 original 是否命中白名单规则。

        规则顺序：
          1. 精确匹配（O(1) set lookup）
          2. 正则匹配（依次尝试 fullmatch）
        """
        # 1. 精确匹配
        if original in self.exact_set:
            return True

        # 2. 正则匹配
        for pattern in self.regex_patterns:
            if pattern.fullmatch(original):
                return True

        return False

    def __bool__(self) -> bool:
        return bool(self.exact_set) or bool(self.regex_patterns)

AGENT_KEYS = ["country", "greater_china", "hyphenation", "style_grammar", "terminology", "tansa"]
SOFT_RULE_AGENT_KEYS = list(SOFT_RULE_MODULE_KEYS)
SOFT_RULES_PLACEHOLDER = "{{soft_rules}}"
SOFT_RULE_SECTION_TITLE = "## Article-Specific Soft Rules (Dynamic)"
SOFT_RULE_INSERT_ANCHORS = (
    "\n## 3 Output Requirements",
    "\n## 4 Output Requirements",
    "\n## Output Requirements",
)

# ── MQ 配置 ──
MAX_CONCURRENT_CHUNKS = 5  # 最大 5 路并发处理 chunk
MAX_AGENTS_PER_CHUNK = 6   # 每个 chunk 跑 6 个 agent (5 LLM + 1 Tansa)



# ──────────────────────────────────────────────
# Load system prompts (Langfuse text prompts, then local markdown fallback)
# ──────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


_AGENT_PROMPT_FILES: Dict[str, str] = {
    "country": "country-task-system-prompt.md",
    "greater_china": "greater-china-task-system-prompt.md",
    "hyphenation": "hyphenation-task-system-prompt.md",
    "style_grammar": "style-grammar-task-system-prompt.md",
    "terminology": "terminology-task-system-prompt.md",
    "general": "general-task-system-prompt.md",
}


def _build_system_prompts() -> Dict[str, str]:
    from proofreader_langfuse import load_proofreader_prompt

    out: Dict[str, str] = {}
    for agent_key, filename in _AGENT_PROMPT_FILES.items():
        path = Path(BASE_DIR) / filename
        text, source, version, lf_name = load_proofreader_prompt(agent_key, path)
        vlog = f" v{version}" if version is not None else ""
        origin = f"{source}"
        if lf_name:
            origin = f"{source} [{lf_name}]"
        print(f"[ProofReader] system prompt '{agent_key}': {origin}{vlog}")
        out[agent_key] = text
    return out


SYSTEM_PROMPTS = _build_system_prompts()


# ──────────────────────────────────────────────
# Graph State Schema
# ──────────────────────────────────────────────
class ProofreaderState(TypedDict):
    input_text: str
    user_prompt: str
    soft_rules_enabled: bool
    soft_rules_by_module: Dict[str, str]
    injected_rule_trace_by_module: Dict[str, List[Dict[str, Any]]]
    soft_rules_debug: Dict[str, Any]
    chunks: List[str]                      # 递归分块结果
    chunk_count: int                       # chunk 总数
    successful_chunks: int                 # 成功处理的 chunk 数
    country_result: Optional[str]          # 聚合后的 Country 结果
    greater_china_result: Optional[str]     # 聚合后的 Greater China 结果
    hyphenation_result: Optional[str]      # 聚合后的 Hyphenation 结果
    style_grammar_result: Optional[str]    # 聚合后的 Style & Grammar 结果
    terminology_result: Optional[str]      # 聚合后的 Terminology 结果
    tansa_result: Optional[str]            # 聚合后的 Tansa 结果

    general_result: Optional[str]          # General Agent 最终结果
    duplicate_words_removed: int           # 重复词过滤器移除的条目数
    duplicate_word_filtered_result: Optional[str]  # 重复词过滤后的结果
    postprocessor_removed: int             # Postprocessor 移除的条目数
    postprocessed_result: Optional[str]    # Postprocessor 过滤后的结果
    whitelist_removed: int                 # 白名单移除的条目数
    whitelist_result: Optional[str]        # 白名单过滤后的最终结果


# ──────────────────────────────────────────────
# LLM API call
# ──────────────────────────────────────────────
@observe(as_type="generation")
def call_llm(
    system_prompt: str,
    user_text: str,
    max_retries: int = 3,
    *,
    trace_metadata: Optional[Dict[str, Any]] = None,
    langfuse_linked_task_prompt_name: Optional[str] = None,
) -> str:
    """Call DashScope LLM with retries. Returns error string on failure."""
    api_key = DASHSCOPE_API_KEY or os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        return "[ERROR: missing DASHSCOPE_API_KEY]"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-DashScope-DataInspection": '{"input":"disable", "output":"disable"}',
    }

    payload = {
        "model": CHAT_MODEL,
        "input": {"messages": messages},
        "parameters": {"result_format": "message"},
    }

    system_log_n = langfuse_log_system_prompt_max_chars()
    linked_prompt = None
    if langfuse_linked_task_prompt_name:
        linked_prompt = fetch_text_prompt_client_for_generation(langfuse_linked_task_prompt_name)

    try:
        merged_meta = {
            "workflow": "proofreader_rewrite",
            **(trace_metadata or {}),
        }
        if langfuse_linked_task_prompt_name:
            merged_meta["langfuse.task_prompt_name"] = langfuse_linked_task_prompt_name
        kw_up: Dict[str, Any] = {
            "model": CHAT_MODEL,
            "input": {
                "system_preview": system_prompt[:500],
                "system_prompt_excerpt": system_prompt[:system_log_n],
                "system_preview_truncated": len(system_prompt) > system_log_n,
                "user_preview": user_text[:800],
                "system_chars": len(system_prompt),
                "user_chars": len(user_text),
                "user_suffix_preview": user_text[-400:] if len(user_text) > 400 else user_text,
            },
            "metadata": merged_meta,
        }
        if linked_prompt is not None:
            kw_up["prompt"] = linked_prompt
        get_client().update_current_generation(**kw_up)
    except Exception:
        pass

    for attempt in range(max_retries):
        try:
            resp = requests.post(
                DASHSCOPE_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=120,
            )

            if resp.status_code != 200:
                error_detail = _dashscope_error_detail(resp)
                if attempt < max_retries - 1:
                    snippet = _dashscope_error_detail(resp, 800)
                    print(
                        f"  LLM call failed (attempt {attempt+1}/{max_retries}): "
                        f"HTTP {resp.status_code}. {snippet} Retrying..."
                    )
                    time.sleep(2)
                    continue
                return f"[ERROR: HTTP {resp.status_code}] {error_detail}"

            result = resp.json()
            content = result["output"]["choices"][0]["message"]["content"]
            try:
                get_client().update_current_generation(output=content[:2000])
            except Exception:
                pass
            return content

        except Exception as e:
            if attempt < max_retries - 1:
                print(f"  LLM call failed (attempt {attempt+1}/{max_retries}): {e}. Retrying...")
                time.sleep(2)
                continue
            return f"[ERROR: LLM call failed after {max_retries} attempts: {e}]"


# ──────────────────────────────────────────────
# Prompt Builders
# ──────────────────────────────────────────────
def _inject_soft_rules_into_system_prompt(system_prompt: str, module_key: str, soft_rules: str) -> str:
    """Inject a rendered module-specific soft-rule snippet into the module prompt."""
    rules = (soft_rules or "").strip()
    if not rules or rules == DEFAULT_EMPTY_SOFT_RULES_MESSAGE:
        if SOFT_RULES_PLACEHOLDER in system_prompt:
            return system_prompt.replace(SOFT_RULES_PLACEHOLDER, DEFAULT_EMPTY_SOFT_RULES_MESSAGE)
        return system_prompt

    if SOFT_RULES_PLACEHOLDER in system_prompt:
        return system_prompt.replace(SOFT_RULES_PLACEHOLDER, rules)

    section = f"\n\n{SOFT_RULE_SECTION_TITLE}\n{rules}\n"
    for anchor in SOFT_RULE_INSERT_ANCHORS:
        pos = system_prompt.find(anchor)
        if pos != -1:
            return system_prompt[:pos].rstrip() + section + system_prompt[pos:].lstrip("\n")

    return system_prompt.rstrip() + section


def build_agent_prompt(system_prompt: str, input_text: str) -> str:
    return (
        f"Please review the following text according to your editorial guidelines.\n"
        f"Conduct a thorough audit and report all findings.\n\n"
        f"--- TEXT TO AUDIT ---\n"
        f"{input_text}\n"
        f"--- END OF TEXT ---"
    )


def build_general_prompt(
    system_prompt: str,
    input_text: str,
    country: str,
    greater_china: str,
    hyphenation: str,
    style_grammar: str,
    terminology: str,
    tansa: str,
) -> str:
    return (
        f"You are the Final Consolidation Agent. Below is the original text "
        f"followed by the findings from 6 specialist editorial modules.\n\n"
        f"--- ORIGINAL TEXT ---\n"
        f"{input_text}\n"
        f"--- END OF ORIGINAL TEXT ---\n\n"
        f"--- SPECIALIST FINDINGS ---\n\n"
        f"=== Tansa Guideline Module Findings ===\n"
        f"{tansa}\n\n"
        f"=== Country Module Findings ===\n"
        f"{country}\n\n"
        f"=== Greater China Module Findings ===\n"
        f"{greater_china}\n\n"
        f"=== Hyphenation Module Findings ===\n"
        f"{hyphenation}\n\n"
        f"=== Style & Grammar Module Findings ===\n"
        f"{style_grammar}\n\n"
        f"=== Terminology Module Findings ===\n"
        f"{terminology}\n\n"
        f"--- END OF SPECIALIST FINDINGS ---\n\n"
        f"Please merge these findings into a single JSON array following the "
        f"editorial constitution (hierarchy: Tansa Guideline > Candidate Lock "
        f"List > Specialist Modules). Return ONLY a valid JSON array."
    )



# ══════════════════════════════════════════════
# LangGraph Node 0: Text Preprocessing
# ══════════════════════════════════════════════

def preprocessing_node(state: ProofreaderState) -> Dict[str, Any]:
    """
    文本预处理 Node。
    在输入文本进入 Chunking 之前，先经过 advanced_preprocessing 清洗：
      - 去除系统噪声指令（Screen Reader 等）
      - 深度清洗不可见字符（零宽空格、BOM 等）
      - 压缩过度堆积的空白符和换行
      - 按段落去重

    预处理后的文本写回 state.input_text，供后续 Node 使用。
    """
    print("\n[LangGraph] Preprocessing Node: cleaning input text...")
    original_text = state.get("input_text", "")
    cleaned_text = advanced_preprocessing(original_text)

    if cleaned_text != original_text:
        removed_chars = len(original_text) - len(cleaned_text)
        print(f"  → Removed {removed_chars} chars of noise/whitespace/duplicates")
    else:
        print(f"  → No changes needed (text already clean)")

    return {
        "input_text": cleaned_text,
    }


# ══════════════════════════════════════════════
# LangGraph Node 1: Soft Rules
# ══════════════════════════════════════════════

def soft_rules_node(state: ProofreaderState) -> Dict[str, Any]:
    """
    Soft Rules Node.
    Compiles article-specific soft rules once from the preprocessed full article.
    """
    enabled = bool(state.get("soft_rules_enabled", False))
    print(f"[LangGraph] Soft Rules Node: {'enabled' if enabled else 'disabled'}")

    soft_debug = compile_soft_rule_debug(state.get("input_text", ""), enabled=enabled)
    summary = soft_debug.get("summary", {})
    rule_counts = summary.get("rule_counts", {})
    total_rules = sum(int(v or 0) for v in rule_counts.values())

    if enabled:
        print(f"  [SoftRules] Injecting {total_rules} matched rules across modules")
    else:
        print("  [SoftRules] Skipped by request")

    return {
        "soft_rules_by_module": soft_debug.get("rendered_prompt_by_module", {}),
        "injected_rule_trace_by_module": soft_debug.get("injected_rule_trace_by_module", {}),
        "soft_rules_debug": soft_debug,
    }


# ══════════════════════════════════════════════
# LangGraph Node 2: Chunking
# ══════════════════════════════════════════════

def chunking_node(state: ProofreaderState) -> Dict[str, Any]:
    """
    递归分块 Node。
    将输入文本按 段落→句子→Token 三层递归分块，存入 state.chunks。
    """
    print("\n[LangGraph] Chunking Node: recursive chunking...")
    chunker = NewsChunker()
    chunks = chunker.chunk_text(state["input_text"])

    if not chunks:
        chunks = [state["input_text"]]  # fallback

    print(f"  → {len(chunks)} chunks generated")
    return {
        "chunks": chunks,
        "chunk_count": len(chunks),
    }


# ══════════════════════════════════════════════
# Post-Processor: 清洗 agent 输出中的无效变更
# ══════════════════════════════════════════════

def _post_process_agent_result(agent_key: str, raw_output: str) -> str:
    """
    后处理单个 agent 的输出。

    规则：
      1. 尝试解析 agent 输出为 JSON
      2. 如果 JSON 包含 notes_on_changes 数组：
         - 删除其中 original == corrected 的条目（无实际变更）
         - 如果过滤后数组为空 → 返回 {"category": key, "status": "No Changes found"}
         - 否则返回过滤后的完整 JSON
      3. 如果 JSON 不含预期的 category/status/notes_on_changes 字段
         → 返回 {"category": key, "status": "No Changes found"}
      4. 如果无法解析为 JSON → 返回 {"category": key, "status": "No Changes found"}

    Args:
        agent_key:  agent 名称（如 "hyphenation", "country"）
        raw_output: agent 原始输出文本

    Returns:
        处理后的输出文本（JSON 字符串）
    """
    # 如果已经是错误标记，直接返回
    if raw_output.startswith("[ERROR"):
        return raw_output

    try:
        data = json.loads(raw_output)
    except (json.JSONDecodeError, ValueError):
        # 不是合法 JSON → 视为无变更
        return json.dumps({"category": agent_key, "status": "No Changes found"}, ensure_ascii=False)

    # 检查是否包含预期的字段
    if not isinstance(data, dict):
        return json.dumps({"category": agent_key, "status": "No Changes found"}, ensure_ascii=False)

    has_expected_fields = (
        "category" in data and
        "status" in data and
        "notes_on_changes" in data
    )

    if not has_expected_fields:
        return json.dumps({"category": agent_key, "status": "No Changes found"}, ensure_ascii=False)

    notes = data.get("notes_on_changes", [])

    if not isinstance(notes, list):
        return json.dumps({"category": agent_key, "status": "No Changes found"}, ensure_ascii=False)

    # 过滤：删除 original == corrected 的条目
    filtered = [
        item for item in notes
        if item.get("original") != item.get("corrected")
    ]

    if not filtered:
        # 过滤后无有效变更
        return json.dumps({"category": agent_key, "status": "No Changes found"}, ensure_ascii=False)

    # 有有效变更，返回过滤后的完整结果
    data["notes_on_changes"] = filtered
    return json.dumps(data, ensure_ascii=False)


# Agent + Post-Processor 映射（模块名 + _post_processor）
AGENT_POST_PROCESSORS = {
    key: _post_process_agent_result
    for key in AGENT_KEYS
}


# ══════════════════════════════════════════════
# MQ 内部: queue.Queue + ThreadPoolExecutor
# ══════════════════════════════════════════════

class _ChunkTask:
    """单个 chunk 处理任务"""
    def __init__(self, chunk_index: int, chunk_text: str):
        self.chunk_index = chunk_index
        self.chunk_text = chunk_text


class _ChunkResult:
    """单个 chunk 的处理结果"""
    def __init__(self, chunk_index: int, success: bool, results: Dict[str, str]):
        self.chunk_index = chunk_index
        self.success = success
        self.results = results


def _run_agents(
    chunk_text: str,
    *,
    soft_rules_by_module: Optional[Dict[str, str]] = None,
    injected_rule_trace_by_module: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    soft_rules_enabled: bool = False,
) -> Dict[str, str]:
    """
    对单个 chunk 并行跑 6 个 agent（5 个 LLM agent + 1 个 Tansa 确定性 agent）。
    每个 agent 输出会经过对应的 post-processor 清洗。

    Tansa agent 是确定性规则匹配（无 LLM 调用），搜索 Tansa_guidelines_prod.csv
    中的 Search for 字段在原文内的匹配，返回 JSON 格式的结果。

    失败时对应 agent 结果以 [ERROR 开头。
    """
    raw_results: Dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_AGENTS_PER_CHUNK) as executor:
        future_to_key = {}
        for key in AGENT_KEYS:
            ctx = contextvars.copy_context()
            if key == "tansa":
                # Tansa agent: 确定性匹配，不调用 LLM
                future_to_key[executor.submit(ctx.run, run_tansa_agent, chunk_text)] = key
            else:
                module_soft_rules = (soft_rules_by_module or {}).get(key, "") if soft_rules_enabled else ""
                system_prompt = _inject_soft_rules_into_system_prompt(
                    SYSTEM_PROMPTS[key],
                    key,
                    module_soft_rules,
                )
                chunk_fn = functools.partial(
                    call_llm,
                    system_prompt,
                    build_agent_prompt(system_prompt, chunk_text),
                    trace_metadata={
                        "agent": key,
                        "proofreader.phase": "per_chunk_parallel",
                        "langfuse.role_prompt_name": LANGFUSE_ROLE_PROMPT_BY_AGENT.get(key, ""),
                        "langfuse.task_prompt_name": LANGFUSE_TASK_PROMPT_BY_AGENT.get(key, ""),
                        "soft_rules.enabled": soft_rules_enabled,
                        "soft_rules.chars": len(module_soft_rules),
                        "soft_rules.rule_count": len((injected_rule_trace_by_module or {}).get(key, [])),
                    },
                    langfuse_linked_task_prompt_name=LANGFUSE_TASK_PROMPT_BY_AGENT.get(key),
                )
                future_to_key[executor.submit(ctx.run, chunk_fn)] = key

        for future in concurrent.futures.as_completed(future_to_key):
            key = future_to_key[future]
            try:
                raw_results[key] = future.result()
            except Exception as e:
                raw_results[key] = f"[ERROR: {e}]"

    # 每个 agent 输出经过对应的 post-processor 清洗
    results: Dict[str, str] = {}
    for key in AGENT_KEYS:
        raw = raw_results.get(key, "")
        post_fn = AGENT_POST_PROCESSORS.get(key, _post_process_agent_result)
        results[key] = post_fn(key, raw)

    return results



def _worker(
    task_q: queue.Queue,
    result_q: queue.Queue,
    stop_event: threading.Event,
    langfuse_ctx: contextvars.Context,
    soft_rules_by_module: Optional[Dict[str, str]] = None,
    injected_rule_trace_by_module: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    soft_rules_enabled: bool = False,
):
    """MQ Worker: 取 chunk → 跑6个agent (5 LLM + 1 Tansa) → 放结果"""
    while not stop_event.is_set():
        try:
            task = task_q.get(timeout=1)
        except queue.Empty:
            continue
        if task is None:
            task_q.task_done()
            break

        agent_results = langfuse_ctx.run(
            _run_agents,
            task.chunk_text,
            soft_rules_by_module=soft_rules_by_module,
            injected_rule_trace_by_module=injected_rule_trace_by_module,
            soft_rules_enabled=soft_rules_enabled,
        )

        has_error = any(v.startswith("[ERROR") for v in agent_results.values())

        if has_error:
            failed = [k for k, v in agent_results.items() if v.startswith("[ERROR")]
            print(f"  [MQ] ✗ Chunk {task.chunk_index + 1} skipped: {failed}")
            result_q.put(_ChunkResult(task.chunk_index, False, agent_results))
        else:
            print(f"  [MQ] ✓ Chunk {task.chunk_index + 1} OK")
            result_q.put(_ChunkResult(task.chunk_index, True, agent_results))

        task_q.task_done()


def _mq_process_chunks(
    chunks: List[str],
    *,
    soft_rules_by_module: Optional[Dict[str, str]] = None,
    injected_rule_trace_by_module: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    soft_rules_enabled: bool = False,
) -> Dict[str, Any]:
    """
    使用 queue.Queue 并发处理 chunks。
    - 最多 MAX_CONCURRENT_CHUNKS 个 Worker 同时运行
    - 每个 Worker 内 5 个 agent 并发
    - 失败 chunk 直接 skip

    Returns:
        dict with aggregated agent results + stats
    """
    task_q: queue.Queue = queue.Queue()
    result_q: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    for idx, text in enumerate(chunks):
        task_q.put(_ChunkTask(idx, text))

    workers = []
    for _ in range(MAX_CONCURRENT_CHUNKS):
        # Separate Context instance per MQ thread: Python 3.11+ forbids concurrent
        # context.run(...) on the *same* Context object ("already entered").
        thread_langfuse_ctx = contextvars.copy_context()
        w = threading.Thread(
            target=_worker,
            args=(task_q, result_q, stop_event, thread_langfuse_ctx),
            kwargs={
                "soft_rules_by_module": soft_rules_by_module,
                "injected_rule_trace_by_module": injected_rule_trace_by_module,
                "soft_rules_enabled": soft_rules_enabled,
            },
            daemon=True,
        )
        w.start()
        workers.append(w)

    task_q.join()
    stop_event.set()
    for w in workers:
        w.join(timeout=2)

    # 收集结果
    total: Dict[int, _ChunkResult] = {}
    while not result_q.empty():
        try:
            r = result_q.get_nowait()
            total[r.chunk_index] = r
        except queue.Empty:
            break

    # 按 agent 聚合
    agg: Dict[str, List[str]] = {k: [] for k in AGENT_KEYS}
    success_count = 0

    for idx in sorted(total.keys()):
        cr = total[idx]
        if cr.success:
            success_count += 1
            for k in AGENT_KEYS:
                agg[k].append(cr.results.get(k, ""))

    # 合并
    def merge(lst, default):
        if not lst:
            return default
        return "\n\n".join(f"--- Chunk Finding ---\n{r}" for r in lst)

    merged = {k: merge(agg[k], f"[No findings from {k} module]") for k in AGENT_KEYS}

    return {
        "successful_chunks": success_count,
        "tansa_result": merged["tansa"],
        "country_result": merged["country"],
        "greater_china_result": merged["greater_china"],
        "hyphenation_result": merged["hyphenation"],
        "style_grammar_result": merged["style_grammar"],
        "terminology_result": merged["terminology"],
    }



# ══════════════════════════════════════════════
# LangGraph Node 2: MQ Distributor
# ══════════════════════════════════════════════

def mq_distributor_node(state: ProofreaderState) -> Dict[str, Any]:
    """
    MQ 分发 Node。
    将 state.chunks 通过 queue.Queue 并发处理，
    每个 chunk 跑 5 个 agent，结果聚合后存入 state。
    """
    chunks = state.get("chunks", [])
    if not chunks:
        chunks = [state["input_text"]]

    print(f"\n[LangGraph] MQ Distributor Node: {len(chunks)} chunks → {MAX_CONCURRENT_CHUNKS} MQ workers")
    result = _mq_process_chunks(
        chunks,
        soft_rules_by_module=state.get("soft_rules_by_module", {}),
        injected_rule_trace_by_module=state.get("injected_rule_trace_by_module", {}),
        soft_rules_enabled=bool(state.get("soft_rules_enabled", False)),
    )
    print(f"  → {result['successful_chunks']}/{len(chunks)} chunks OK\n")
    return result


# ══════════════════════════════════════════════
# LangGraph Node 3: General Agent
# ══════════════════════════════════════════════

def general_agent_node(state: ProofreaderState) -> Dict[str, Any]:
    """
    General Agent Node。
    将 6 个 agent（5 LLM + 1 Tansa）的聚合结果合并后，做最终整合。
    """
    print("[LangGraph] General Agent Node: consolidating 6 agents (5 LLM + 1 Tansa)...")
    user_prompt = build_general_prompt(
        SYSTEM_PROMPTS["general"],
        state["input_text"],
        state.get("country_result", ""),
        state.get("greater_china_result", ""),
        state.get("hyphenation_result", ""),
        state.get("style_grammar_result", ""),
        state.get("terminology_result", ""),
        state.get("tansa_result", ""),
    )
    max_u = _general_user_max_chars()
    user_prompt, did_truncate = _truncate_middle(user_prompt, max_u)
    if did_truncate:
        print(
            "  → General-agent user payload exceeded "
            f"PROOFREADER_GENERAL_USER_MAX_CHARS={max_u}: applied middle truncation"
        )

    result = call_llm(
        SYSTEM_PROMPTS["general"],
        user_prompt,
        trace_metadata={
            "agent": "general",
            "proofreader.phase": "consolidation",
            "langfuse.role_prompt_name": LANGFUSE_ROLE_PROMPT_BY_AGENT.get("general", ""),
            "langfuse.task_prompt_name": LANGFUSE_TASK_PROMPT_BY_AGENT.get("general", ""),
        },
        langfuse_linked_task_prompt_name=LANGFUSE_TASK_PROMPT_BY_AGENT.get("general"),
    )
    return {"general_result": result}



# ══════════════════════════════════════════════
# 白名单配置 & 加载（支持精确匹配 + 正则表达式）
# ══════════════════════════════════════════════

WHITELIST_PATH = os.path.join(BASE_DIR, "whitelist.yaml")

# 全局缓存白名单配置
_WHITELIST_CACHE_CONFIG: Optional[WhitelistConfig] = None

def load_whitelist() -> WhitelistConfig:
    """
    从 whitelist.yaml 加载白名单配置（精确关键词 + 正则表达式）。

    配置格式（YAML）:
        whitelist:           # 精确匹配列表（区分大小写）
          - "keyword1"
          - "keyword2"
        whitelist_regex:     # 正则表达式列表（通过 re.fullmatch 匹配）
          - "^[0-9]+$"       # 纯数字
          - "(?i)^chinese$"  # 大小写不敏感匹配

    Returns:
        WhitelistConfig 对象，包含精确匹配 set 和编译后的正则列表
    """
    global _WHITELIST_CACHE_CONFIG
    if _WHITELIST_CACHE_CONFIG is not None:
        return _WHITELIST_CACHE_CONFIG

    try:
        with open(WHITELIST_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # 加载精确匹配列表
        exact_list: list[str] = data.get("whitelist", []) if data else []
        exact_set = set(item.strip() for item in exact_list if item and item.strip())

        # 加载正则表达式列表
        regex_raw: list[str] = data.get("whitelist_regex", []) if data else []
        regex_patterns: list[re.Pattern] = []
        bad_patterns: list[str] = []
        for pattern_str in regex_raw:
            if not pattern_str or not pattern_str.strip():
                continue
            try:
                regex_patterns.append(re.compile(pattern_str.strip()))
            except re.error as e:
                bad_patterns.append(f"'{pattern_str}' ({e})")

        config = WhitelistConfig(exact_set=exact_set, regex_patterns=regex_patterns)
        _WHITELIST_CACHE_CONFIG = config

        print(f"  [Whitelist] Loaded {len(exact_set)} exact keywords + {len(regex_patterns)} regex patterns")
        if bad_patterns:
            print(f"  [Whitelist] WARNING: {len(bad_patterns)} invalid regex pattern(s) skipped: {bad_patterns}")
        return config

    except Exception as e:
        print(f"  [Whitelist] Failed to load whitelist: {e}. Using empty whitelist.")
        _WHITELIST_CACHE_CONFIG = WhitelistConfig(exact_set=set(), regex_patterns=[])
        return _WHITELIST_CACHE_CONFIG


# ══════════════════════════════════════════════
# LangGraph Node 4: Duplicate Word Filter
# ══════════════════════════════════════════════

def _has_duplicate_words(text: str) -> bool:
    """
    检查文本中是否包含连续重复的单词（如 "the the", "in in", "of of" 等）。

    规则：
      1. 将文本按空白分割为单词列表
      2. 遍历检查是否存在相邻的相同单词（忽略大小写）
      3. 如果存在 → 返回 True

    Args:
        text: 要检查的文本

    Returns:
        True 如果包含连续重复单词，否则 False
    """
    if not text:
        return False
    words = text.split()
    for i in range(len(words) - 1):
        if words[i].lower() == words[i + 1].lower():
            return True
    return False


def duplicate_word_filter_node(state: ProofreaderState) -> Dict[str, Any]:
    """
    重复词过滤 Node。
    解析 general_agent 输出的 JSON 数组，删除其中 corrected 字段包含
    连续重复单词（如 "the the", "in in", "of of" 等）的条目。

    Returns:
        更新 state，增加 duplicate_word_filtered_result 和 duplicate_words_removed 字段
    """
    print("[LangGraph] Duplicate Word Filter Node: checking for duplicate words...")

    general_raw = state.get("general_result", "")
    if not general_raw or general_raw.startswith("[ERROR"):
        return {
            "duplicate_word_filtered_result": general_raw,
            "duplicate_words_removed": 0,
        }

    try:
        data = json.loads(general_raw)
        if not isinstance(data, list):
            return {
                "duplicate_word_filtered_result": general_raw,
                "duplicate_words_removed": 0,
            }
    except (json.JSONDecodeError, ValueError):
        return {
            "duplicate_word_filtered_result": general_raw,
            "duplicate_words_removed": 0,
        }

    original_count = len(data)

    # 过滤：删除 corrected 包含连续重复单词的条目
    filtered = [
        item for item in data
        if not (isinstance(item, dict) and _has_duplicate_words(item.get("corrected", "")))
    ]

    removed = original_count - len(filtered)

    # 如果有移除，打印详情
    if removed > 0:
        removed_items = [
            item for item in data
            if isinstance(item, dict) and _has_duplicate_words(item.get("corrected", ""))
        ]
        for item in removed_items:
            print(f"  [DuplicateWord] Removed: corrected='{item.get('corrected', '')}' "
                  f"(original='{item.get('original', '')}')")

    result_json = json.dumps(filtered, ensure_ascii=False)

    print(f"  [DuplicateWord] Removed {removed} items (corrected has duplicate words). "
          f"{len(filtered)} items remaining.")

    return {
        "duplicate_word_filtered_result": result_json,
        "duplicate_words_removed": removed,
    }


# ══════════════════════════════════════════════
# LangGraph Node 5: Postprocessor
# ══════════════════════════════════════════════

def postprocessor_node(state: ProofreaderState) -> Dict[str, Any]:
    """
    Postprocessor Node。
    Parses the duplicate-word-filtered JSON output, removes no-ops, noise,
    and duplicates, then returns the cleaned result.

    Placed between duplicate_word_filter and whitelist_filter in the workflow.
    Logic ported from:
        editorial-sub-editing-assistant-backend/app/agentic/nodes/postprocessor.py
    """
    print("[LangGraph] Postprocessor Node: cleaning general agent output...")

    input_raw = state.get("duplicate_word_filtered_result", "") or state.get("general_result", "")

    result = postprocess_general_result(input_raw, request_context=state.get("input_text", ""))

    removed = result.get("postprocessor_removed", 0)
    output_raw = result.get("postprocessed_result", input_raw)

    if removed > 0:
        print(f"  [Postprocessor] Removed {removed} entries "
              f"({result.get('postprocessor_input_count', 0)} → "
              f"{result.get('postprocessor_output_count', 0)}): "
              f"no-ops, noise, and/or duplicates filtered out")
    else:
        print(f"  [Postprocessor] No entries removed "
              f"({result.get('postprocessor_input_count', 0)} items checked)")

    return {
        "postprocessed_result": output_raw,
        "postprocessor_removed": removed,
    }


# ══════════════════════════════════════════════
# LangGraph Node 6: Whitelist Filter
# ══════════════════════════════════════════════

def whitelist_filter_node(state: ProofreaderState) -> Dict[str, Any]:
    """
    白名单过滤 Node。
    解析 postprocessor_node 输出的 JSON 数组，删除其中 original 与白名单关键词
    完全匹配（区分大小写）的条目。

    Note: 输入来自 postprocessed_result（而非 duplicate_word_filtered_result 或 general_result），
    因为该节点在 postprocessor 之后执行。

    Returns:
        更新 state，增加 whitelist_result 和 whitelist_removed 字段
    """
    print("[LangGraph] Whitelist Filter Node: filtering after postprocessor...")

    # 读取已经过 postprocessor 处理的结果
    general_raw = state.get("postprocessed_result", "") or state.get("duplicate_word_filtered_result", "") or state.get("general_result", "")
    if not general_raw or general_raw.startswith("[ERROR"):
        return {
            "whitelist_result": general_raw,
            "whitelist_removed": 0,
        }

    whitelist_config = load_whitelist()
    if not whitelist_config:
        # 白名单为空，无需过滤
        return {
            "whitelist_result": general_raw,
            "whitelist_removed": 0,
        }

    try:
        # 尝试解析 JSON 数组
        data = json.loads(general_raw)
        if not isinstance(data, list):
            # 不是数组，直接返回
            return {
                "whitelist_result": general_raw,
                "whitelist_removed": 0,
            }
    except (json.JSONDecodeError, ValueError):
        # 无法解析 JSON，直接返回
        return {
            "whitelist_result": general_raw,
            "whitelist_removed": 0,
        }

    original_count = len(data)

    # 过滤：使用 WhitelistConfig 同时检查精确匹配 + 正则匹配
    removed_items_detail: list[str] = []
    filtered: list[dict] = []
    for item in data:
        if isinstance(item, dict):
            original = item.get("original", "")
            if whitelist_config.is_whitelisted(original):
                removed_items_detail.append(original)
                continue  # 命中白名单，删除
        filtered.append(item)

    removed = original_count - len(filtered)
    result_json = json.dumps(filtered, ensure_ascii=False)

    # 打印详细的过滤信息
    if removed > 0:
        # 分类统计：精确匹配 vs 正则匹配
        exact_matched = [w for w in removed_items_detail if w in whitelist_config.exact_set]
        regex_matched = [w for w in removed_items_detail if w not in whitelist_config.exact_set]
        parts = []
        if exact_matched:
            parts.append(f"{len(exact_matched)} exact matches: {exact_matched[:10]}{'...' if len(exact_matched) > 10 else ''}")
        if regex_matched:
            parts.append(f"{len(regex_matched)} regex matches: {regex_matched[:10]}{'...' if len(regex_matched) > 10 else ''}")
        print(f"  [Whitelist] Removed {removed} items (original in whitelist). "
              f"{len(filtered)} items remaining. Details: {'; '.join(parts)}")
    else:
        print(f"  [Whitelist] No items removed ({len(filtered)} items checked).")

    return {
        "whitelist_result": result_json,
        "whitelist_removed": removed,
    }


# ══════════════════════════════════════════════
# Build LangGraph
# ══════════════════════════════════════════════

def build_proofreader_graph() -> StateGraph:
    """构建 langgraph 工作流图。"""
    workflow = StateGraph(ProofreaderState)

    workflow.add_node("preprocessing", preprocessing_node)
    workflow.add_node("soft_rules", soft_rules_node)
    workflow.add_node("chunking", chunking_node)
    workflow.add_node("mq_distributor", mq_distributor_node)
    workflow.add_node("general_agent", general_agent_node)
    workflow.add_node("duplicate_word_filter", duplicate_word_filter_node)
    workflow.add_node("postprocessor", postprocessor_node)
    workflow.add_node("whitelist_filter", whitelist_filter_node)

    workflow.set_entry_point("preprocessing")
    workflow.add_edge("preprocessing", "soft_rules")
    workflow.add_edge("soft_rules", "chunking")
    workflow.add_edge("chunking", "mq_distributor")
    workflow.add_edge("mq_distributor", "general_agent")
    workflow.add_edge("general_agent", "duplicate_word_filter")
    workflow.add_edge("duplicate_word_filter", "postprocessor")
    workflow.add_edge("postprocessor", "whitelist_filter")
    workflow.set_finish_point("whitelist_filter")

    return workflow

proofreader_graph = build_proofreader_graph().compile()


# ──────────────────────────────────────────────
# 兼容入口: run_proofreader (直接调用 langgraph)
# ──────────────────────────────────────────────
@observe(name="proofreader_workflow")
def run_proofreader(text: str, enable_soft_rules: bool = False) -> Dict[str, Any]:
    """
    运行 ProofReader 工作流 (langgraph 编排)。

    Flow:
      0. Preprocessing Node: advanced_preprocessing 清洗输入文本
      1. Chunking Node: NewsChunker 递归分块
      2. MQ Distributor Node: queue.Queue 并发处理 (max 5 chunks, 各 6 agents)
      3. General Agent Node: 最终整合

    Args:
        text: 输入文本

    Returns:
        Dict with all results + chunking metadata
    """
    initial_state: ProofreaderState = {
        "input_text": text,
        "user_prompt": "",
        "soft_rules_enabled": bool(enable_soft_rules),
        "soft_rules_by_module": {},
        "injected_rule_trace_by_module": {},
        "soft_rules_debug": empty_soft_rule_debug(bool(enable_soft_rules)),
        "chunks": [],
        "chunk_count": 0,
        "successful_chunks": 0,
        "country_result": None,
        "greater_china_result": None,
        "hyphenation_result": None,
        "style_grammar_result": None,
        "terminology_result": None,
        "tansa_result": None,
        "general_result": None,
        "duplicate_words_removed": 0,
        "duplicate_word_filtered_result": None,
        "postprocessor_removed": 0,
        "postprocessed_result": None,
        "whitelist_removed": 0,
        "whitelist_result": None,
    }

    try:
        result = proofreader_graph.invoke(initial_state)
        return {
            "country_result": result.get("country_result", ""),
            "greater_china_result": result.get("greater_china_result", ""),
            "hyphenation_result": result.get("hyphenation_result", ""),
            "style_grammar_result": result.get("style_grammar_result", ""),
            "terminology_result": result.get("terminology_result", ""),
            "tansa_result": result.get("tansa_result", ""),
            "soft_rules_debug": result.get("soft_rules_debug", empty_soft_rule_debug(False)),
            "soft_rules_by_module": result.get("soft_rules_by_module", {}),
            "injected_rule_trace_by_module": result.get("injected_rule_trace_by_module", {}),
            "general_result": result.get("general_result", ""),
            "duplicate_word_filtered_result": result.get("duplicate_word_filtered_result", ""),
            "duplicate_words_removed": result.get("duplicate_words_removed", 0),
            "postprocessed_result": result.get("postprocessed_result", ""),
            "postprocessor_removed": result.get("postprocessor_removed", 0),
            "whitelist_result": result.get("whitelist_result", ""),
            "whitelist_removed": result.get("whitelist_removed", 0),
            "_chunks": result.get("chunks", []),
            "_chunk_count": result.get("chunk_count", 0),
            "_successful_chunks": result.get("successful_chunks", 0),
        }
    finally:
        try:
            get_client().flush()
        except Exception:
            pass



# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        input_text = sys.argv[1]
    else:
        input_text = (
            "The SCMP reported that US secretary of state Antony Blinken "
            "met with ASEAN leaders in a high-tech conference. "
            "The event was co-sponsored by NATO and the BBC. "
            "Discussions focused on regional security and economic cooperation. "
            "Several agreements were signed during the two-day summit. "
            "Analysts say this marks a new era of multilateral engagement "
            "in the Asia-Pacific region."
        )

    print("=" * 60)
    print("ProofReader Workflow (LangGraph + MQ)")
    print("=" * 60)
    print(f"\nInput text:\n{input_text}\n")

    result = run_proofreader(input_text)

    print("\n" + "=" * 60)
    print("Chunking Info:")
    print("-" * 40)
    print(f"  Total chunks: {result.get('_chunk_count', 'N/A')}")
    print(f"  Successful: {result.get('_successful_chunks', 'N/A')}")
    for i, chunk in enumerate(result.get('_chunks', [])):
        from chunking import count_tokens
        print(f"  Chunk {i+1}: {len(chunk)} chars, ~{count_tokens(chunk)} tokens")

    for key in AGENT_KEYS:
        print(f"\n{'=' * 60}")
        print(f"{key.title()} Agent Result:")
        print("-" * 40)
        print(result.get(f"{key}_result", "[No result]"))

    print(f"\n{'=' * 60}")
    print("General Agent (Final Consolidation) Result:")
    print("-" * 40)
    print(result.get("general_result", "[No result]"))

    print(f"\n{'=' * 60}")
    print("Workflow Complete!")
    print("=" * 60)
