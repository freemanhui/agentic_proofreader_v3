"""
News Chunking Module for ProofReader Rewrite
=============================================
Recursive text chunking — faithfully adapted from inn-12/srcs/offline/chunking.py.

Chunking strategy (三层递归分块):
  1. 段落级: 按双换行符分割文本为段落
  2. 句子级: 按中英文标点分割段落为句子
  3. Token 级: 按 Token 大小将句子合并/切分为 Chunk

支持句子重叠 (overlap) 以确保上下文不丢失。

Usage:
    from chunking import NewsChunker

    chunker = NewsChunker(
        min_chunk_size=200,
        target_chunk_size=256,
        max_chunk_size=350,
        chunk_overlap=50,
    )
    chunks = chunker.chunk_text("Long text to be chunked...")
    # chunks 是一个 list of str
"""

import re
import hashlib
from typing import List, Tuple

# ──────────────────────────────────────────────
# Constants (same as inn-12/srcs/offline/chunking.py)
# ──────────────────────────────────────────────
TOKEN_ESTIMATE_CHARS = 3.5

DEFAULT_MIN_CHUNK_SIZE = 200
DEFAULT_TARGET_CHUNK_SIZE = 256
DEFAULT_MAX_CHUNK_SIZE = 350
DEFAULT_CHUNK_OVERLAP = 50


# ══════════════════════════════════════════════
# Token 估算
# ══════════════════════════════════════════════

def count_tokens(text: str) -> int:
    """估算文本 token 数量（字符数 / 3.5）"""
    if not text:
        return 0
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return 0
    return max(1, int(len(text) / TOKEN_ESTIMATE_CHARS))


# ══════════════════════════════════════════════
# 句子分割
# ══════════════════════════════════════════════

def split_into_sentences(text: str) -> List[str]:
    """
    将文本分割成句子（支持中英文标点）

    中文: 。！？；：
    英文: .!?;:
    """
    if not text:
        return []
    sentences = re.split(r'(?<=[。！？；：.!?:])\s+', text)
    return [s.strip() for s in sentences if s.strip()]


# ══════════════════════════════════════════════
# Token 级切分（对大句子按空格切分）
# ══════════════════════════════════════════════

def split_text_by_tokens(
    text: str,
    max_tokens: int,
    overlap_tokens: int = 0,
) -> List[Tuple[str, int, int]]:
    """
    按 token 数量将文本切分成重叠的块（按空格分词）。

    Returns:
        [(chunk_text, start_token, end_token), ...]
    """
    tokens = text.split(' ')
    total = len(tokens)

    if total <= max_tokens:
        return [(text, 0, total)]

    chunks = []
    stride = max_tokens - overlap_tokens

    for start in range(0, total, stride):
        end = min(start + max_tokens, total)
        chunk_text = ' '.join(tokens[start:end])
        chunks.append((chunk_text, start, end))
        if end >= total:
            break

    return chunks


# ══════════════════════════════════════════════
# 句子 → Chunk 合并（句子窗口 + Token 边界）
# ══════════════════════════════════════════════

def merge_sentences_into_chunks(
    sentences: List[str],
    target_chunk_size: int,
    overlap_sentences: int = 1,
    min_chunk_size: int = 200,
    max_chunk_size: int = 350,
) -> List[str]:
    """
    将句子合并成 token 限制的 chunks（带重叠）。

    策略 (同 inn-12/srcs/offline/chunking.py):
      1. 累积句子直到超过 max_chunk_size
      2. 如果累积后超出 max_chunk_size，保存当前 chunk
      3. 重叠: 保留最后 N 个句子到下一个 chunk
      4. 单个句子超出 max_chunk_size → 按 token 拆分

    Returns:
        List of chunk text strings (最终纯文本列表).
    """
    if not sentences:
        return []

    chunks: List[str] = []
    current_sentences: List[str] = []
    current_tokens = 0

    for sentence in sentences:
        sentence_tokens = count_tokens(sentence)

        # ── 单个句子超出最大限制 → 按 token 拆分 ──
        if sentence_tokens > max_chunk_size:
            if current_sentences:
                chunks.append(' '.join(current_sentences))
                current_sentences = []
                current_tokens = 0

            sub_chunks = split_text_by_tokens(
                sentence,
                max_chunk_size,
                overlap_tokens=int(target_chunk_size * 0.2),
            )
            for sub_text, _start_tok, _end_tok in sub_chunks:
                chunks.append(sub_text)
            continue

        # ── 估算加入新句子后的 token 数 ──
        if current_sentences:
            new_tokens = current_tokens + sentence_tokens + 1  # +1 for space
        else:
            new_tokens = sentence_tokens

        # ── 超出最大大小 → 保存当前 chunk，处理重叠 ──
        if current_sentences and new_tokens > max_chunk_size:
            chunks.append(' '.join(current_sentences))

            # 重叠处理
            if overlap_sentences > 0 and len(current_sentences) > overlap_sentences:
                overlap_start = len(current_sentences) - overlap_sentences
                current_sentences = current_sentences[overlap_start:]
                current_tokens = sum(count_tokens(s) for s in current_sentences)
                current_tokens = max(0, current_tokens + len(current_sentences) - 1)
            else:
                current_sentences = []
                current_tokens = 0

            if current_sentences:
                new_tokens = current_tokens + sentence_tokens + 1
            else:
                new_tokens = sentence_tokens

        # ── 添加新句子 ──
        current_sentences.append(sentence)
        current_tokens = new_tokens

    # ── 保存最后一个 chunk ──
    if current_sentences:
        chunks.append(' '.join(current_sentences))

    return chunks


# ══════════════════════════════════════════════
# 段落分割
# ══════════════════════════════════════════════

def split_paragraphs(text: str) -> List[str]:
    """
    按段落分割文本（同 inn-12/srcs/offline/chunking.py TextChunker._split_by_paragraph）。

    策略:
      1. 优先按双换行符分割
      2. 如果结果太少，按单换行符分割
    """
    if not text:
        return []

    text = re.sub(r'\n{2,}', '\n\n', text)
    text = text.strip()

    paragraphs = re.split(r'\n\n+', text)

    if len(paragraphs) < 2:
        paragraphs = re.split(r'\n', text)

    return [p.strip() for p in paragraphs if p.strip()]


# ══════════════════════════════════════════════
# NewsChunker — 主入口
# ══════════════════════════════════════════════

class NewsChunker:
    """
    递归文档分块器（忠实参考 inn-12/srcs/offline/chunking.py TextChunker）。

    分块流程:
      text → split_paragraphs() → 每段落 → split_into_sentences()
      → merge_sentences_into_chunks() → 最终 chunks
    """

    def __init__(
        self,
        min_chunk_size: int = DEFAULT_MIN_CHUNK_SIZE,
        target_chunk_size: int = DEFAULT_TARGET_CHUNK_SIZE,
        max_chunk_size: int = DEFAULT_MAX_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ):
        """
        Args:
            min_chunk_size: 最小 chunk 大小 (tokens)
            target_chunk_size: 目标 chunk 大小 (tokens)
            max_chunk_size: 最大 chunk 大小 (tokens)
            chunk_overlap: 重叠 token 数
        """
        self.min_chunk_size = min_chunk_size
        self.target_chunk_size = target_chunk_size
        self.max_chunk_size = max_chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk_text(self, text: str) -> List[str]:
        """
        递归分块主入口。

        流程:
          1. 按段落分割
          2. 每段落按句子分割
          3. 句子合并/切分为 token 限制的 chunks
          4. 所有段落的 chunks 合并返回

        Args:
            text: 输入文本

        Returns:
            List of chunk text strings
        """
        if not text or not text.strip():
            return []

        text = text.strip()

        # 计算 overlap_sentences 数
        overlap_ratio = self.chunk_overlap / max(self.target_chunk_size, 1)
        overlap_sentences = max(1, int(overlap_ratio * 5))

        # 1. 按段落分割
        paragraphs = split_paragraphs(text)

        all_chunks: List[str] = []

        for para in paragraphs:
            # 2. 段落按句子分割
            sentences = split_into_sentences(para)

            if not sentences:
                continue

            # 3. 句子合并为 chunks
            para_chunks = merge_sentences_into_chunks(
                sentences,
                self.target_chunk_size,
                overlap_sentences=overlap_sentences,
                min_chunk_size=self.min_chunk_size,
                max_chunk_size=self.max_chunk_size,
            )

            all_chunks.extend(para_chunks)

        # 如果分块后数量太多，递归合并小 chunk
        # （保证每个 chunk 至少达到 min_chunk_size）
        all_chunks = self._merge_small_chunks(all_chunks)

        return all_chunks

    def _merge_small_chunks(self, chunks: List[str]) -> List[str]:
        """
        合并太小的 chunks 到前一个 chunk。
        确保每个 chunk 达到 min_chunk_size（除了最后一个）。
        """
        if len(chunks) <= 1:
            return chunks

        merged = []
        buffer = ""

        for chunk in chunks:
            if not buffer:
                buffer = chunk
                continue

            if count_tokens(buffer) < self.min_chunk_size:
                buffer = buffer + " " + chunk
            else:
                merged.append(buffer)
                buffer = chunk

        if buffer:
            merged.append(buffer)

        return merged

    def __repr__(self) -> str:
        return (
            f"NewsChunker(min={self.min_chunk_size}, "
            f"target={self.target_chunk_size}, "
            f"max={self.max_chunk_size}, "
            f"overlap={self.chunk_overlap})"
        )


# ──────────────────────────────────────────────
# Quick test
# ──────────────────────────────────────────────
if __name__ == "__main__":
    sample = """
    The SCMP reported that US secretary of state Antony Blinken met with ASEAN leaders in a high-tech conference.

    The event was co-sponsored by NATO and the BBC. Discussions focused on regional security and economic cooperation.

    Several agreements were signed during the two-day summit. Analysts say this marks a new era of multilateral engagement in the Asia-Pacific region.

    Meanwhile, trade tensions between major economies continue to evolve. The conference also addressed climate change initiatives and digital transformation strategies.
    """.strip()

    chunker = NewsChunker()
    chunks = chunker.chunk_text(sample)

    print(f"Input: {len(sample)} chars, ~{count_tokens(sample)} tokens")
    print(f"Chunks: {len(chunks)}\n")
    for i, c in enumerate(chunks):
        print(f"  Chunk {i+1}: ~{count_tokens(c):>3d} tokens | {c[:80]}...")
