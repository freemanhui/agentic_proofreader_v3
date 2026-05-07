"""
ProofReader Rewrite — Interactive Gradio Demo (v2)
===================================================
Interactive UI:
  • Top:  text input + "Run" button
  • The ProofReader workflow audits the text and finds suggested changes
  • The final output text (with changes applied) appears in a textbox
  • You can also view the raw JSON suggestions in an accordion

Usage:
    python gradio_new.py
"""

import difflib
import json
import re

import gradio as gr
import pandas as pd

from proofreader_workflow import run_proofreader


# ──────────────────────────────────────────────
# Helper: fuzzy sentence matching
# ──────────────────────────────────────────────
def _fuzzy_find_best_match_sentence(text: str, target: str, threshold: float = 0.85):
    """
    在 text 中模糊搜索与 target 最匹配的句子。
    使用 SequenceMatcher 的 ratio 进行相似度比较。

    Args:
        text: 完整的原始文本
        target: 待匹配的目标句子 (updated_context)
        threshold: 相似度阈值 (默认 0.85)

    Returns:
        (start_pos, end_pos) 或 None（未找到匹配）
    """
    if not target or not target.strip():
        return None

    # 按句子边界分割
    boundary_pattern = re.compile(r'(?<=[.!?])\s+')
    boundaries = [0] + [m.end() for m in boundary_pattern.finditer(text)] + [len(text)]

    best_pos = None
    best_ratio = 0.0
    target_clean = target.lower().strip()

    for i in range(len(boundaries) - 1):
        start, end = boundaries[i], boundaries[i + 1]
        sentence = text[start:end].strip()
        if not sentence:
            continue

        ratio = difflib.SequenceMatcher(None, sentence.lower(), target_clean).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_pos = (start, end)

    if best_pos and best_ratio >= threshold:
        return best_pos
    return None


# ──────────────────────────────────────────────
# Helper: apply a single change to source text
# ──────────────────────────────────────────────
def apply_change(
    text: str,
    original: str,
    corrected: str,
    updated_context: str = "",
) -> str:
    """
    基于接口返回的 updated_context 字段，在文本中定位待修改的句子。
    使用模糊检索（高阈值 0.85），搜索到句子后，
    再将句子里的关键词 original 替换成 corrected。

    如果 updated_context 为空或模糊匹配失败，
    回退到简单的全文首例替换。
    """
    if not original or not corrected:
        return text

    if updated_context:
        # 使用模糊匹配定位句子位置
        pos = _fuzzy_find_best_match_sentence(text, updated_context)
        if pos:
            sent_start, sent_end = pos
            # 在匹配到的句子内进行替换
            sentence = text[sent_start:sent_end]
            pattern = re.escape(original)
            new_sentence = re.sub(pattern, corrected, sentence, count=1)
            if new_sentence != sentence:
                # 将替换后的句子放回原文本
                return text[:sent_start] + new_sentence + text[sent_end:]

    # Fallback: 简单全文首例替换
    pattern = re.escape(original)
    new_text = re.sub(pattern, corrected, text, count=1)
    return new_text


# ──────────────────────────────────────────────
# Core workflow runner
# ──────────────────────────────────────────────
def run_workflow(input_text: str, progress=gr.Progress()):
    """
    Run the ProofReader workflow, parse results,
    yield (input_text, summary_md, table_df, changes_json, output_text).
    """
    if not input_text or input_text.strip() == "":
        yield input_text, "Please enter some text to audit.", pd.DataFrame(), "[]", ""
        return

    progress(0, desc="Starting ProofReader workflow...")
    progress(0.1, desc="Preprocessing, chunking, and MQ agents...")

    result = run_proofreader(input_text)

    progress(0.7, desc="Consolidating agent results...")

    # Final output cascade: whitelist → postprocessed → general
    final_result = (
        result.get("whitelist_result")
        or result.get("postprocessed_result")
        or result.get("general_result")
        or ""
    )

    chunk_count = result.get("_chunk_count", 0)
    successful_chunks = result.get("_successful_chunks", 0)

    # Parse JSON array
    changes = []
    if final_result:
        try:
            parsed = json.loads(final_result)
            if isinstance(parsed, list):
                changes = parsed
        except (json.JSONDecodeError, ValueError):
            pass

    progress(0.9, desc="Building results...")

    # Build a pandas DataFrame for the changes table
    df_rows = []
    for i, ch in enumerate(changes):
        df_rows.append({
            "#": i + 1,
            "original": ch.get("original", ""),
            "corrected": ch.get("corrected", ""),
            "category": ch.get("category", ""),
            "note": ch.get("note", ""),
        })

    df = pd.DataFrame(df_rows) if df_rows else pd.DataFrame()

    if not changes:
        summary = (
            f"**✅ Workflow Complete** — {chunk_count} chunks, "
            f"{successful_chunks} processed.\n\n"
            "No editorial changes suggested."
        )
    else:
        summary = (
            f"**✅ Workflow Complete** — {chunk_count} chunks, "
            f"{successful_chunks} processed.\n\n"
            f"**{len(changes)} change(s)** suggested. Click **Accept All** to "
            f"apply all changes to the output text."
        )

    changes_json = json.dumps(changes, ensure_ascii=False)

    # Also build the "accept all" text
    accepted_text = input_text
    for ch in changes:
        original = ch.get("original", "")
        corrected = ch.get("corrected", "")
        updated_context = ch.get("updated_context", "")
        if original and corrected:
            accepted_text = apply_change(
                accepted_text, original, corrected, updated_context
            )

    progress(1.0, desc="Done!")
    yield input_text, summary, df, changes_json, accepted_text


# ──────────────────────────────────────────────
# Accept a single change by index (1‑based)
# ──────────────────────────────────────────────
def on_accept(change_idx: int, text: str, changes_str: str) -> str:
    if not text:
        return text

    try:
        changes = json.loads(changes_str) if changes_str else []
    except (json.JSONDecodeError, ValueError):
        changes = []

    idx = int(change_idx) - 1 if change_idx else -1
    if idx < 0 or idx >= len(changes):
        return text

    ch = changes[idx]
    original = ch.get("original", "")
    corrected = ch.get("corrected", "")
    updated_context = ch.get("updated_context", "")

    if not original or not corrected:
        return text

    new_text = apply_change(text, original, corrected, updated_context)

    if new_text != text:
        gr.Info(f"✅ Applied: \"{original}\" → \"{corrected}\"")
    else:
        gr.Warning(f"⚠️ \"{original}\" not found (already changed?)")

    return new_text


# ──────────────────────────────────────────────
# Accept all changes
# ──────────────────────────────────────────────
def on_accept_all(text: str, changes_str: str) -> str:
    if not text:
        return text

    try:
        changes = json.loads(changes_str) if changes_str else []
    except (json.JSONDecodeError, ValueError):
        changes = []

    applied = 0
    for ch in changes:
        original = ch.get("original", "")
        corrected = ch.get("corrected", "")
        updated_context = ch.get("updated_context", "")
        if not original or not corrected:
            continue
        new_text = apply_change(text, original, corrected, updated_context)
        if new_text != text:
            text = new_text
            applied += 1

    if applied > 0:
        gr.Info(f"✅ Applied {applied} change(s)!")
    return text


# ──────────────────────────────────────────────
# Gradio UI
# ──────────────────────────────────────────────
def build_interface():
    with gr.Blocks(
        title="ProofReader Rewrite — Interactive",
    ) as demo:

        gr.Markdown(
            """
            # 📝 ProofReader Rewrite — Interactive
            <p style="font-size: 1.1em; color: #666;">
            Paste text below, click <strong>Run</strong>, review the suggested
            changes, then <strong>Accept All</strong> to get the final output.
            </p>
            """
        )

        # ── Hidden state ──
        current_text = gr.State("")
        current_changes = gr.State("[]")   # full JSON array of all changes

        # ══════════════════════════════════════
        # INPUT
        # ══════════════════════════════════════
        input_box = gr.Textbox(
            label="📥 Input Text",
            placeholder="Paste or type the text you want to audit here...",
            lines=8, max_lines=20,
        )

        run_btn = gr.Button(
            "🚀 Run ProofReader Workflow",
            variant="primary", size="lg",
        )

        # ══════════════════════════════════════
        # SUMMARY + TABLE
        # ══════════════════════════════════════
        summary_box = gr.Markdown(
            value="👋 Enter text and click **Run** to start."
        )

        changes_table = gr.Dataframe(
            label="🔍 Suggested Changes",
            interactive=False,
            wrap=True,
            column_widths=["5%", "25%", "25%", "20%", "25%"],
        )

        # ══════════════════════════════════════
        # ACCEPT CONTROLS
        # ══════════════════════════════════════
        with gr.Row():
            accept_idx = gr.Number(
                label="Accept change #",
                value=1, minimum=1, maximum=999,
                step=1, precision=0, scale=1,
            )
            accept_btn = gr.Button(
                "✓ Accept Selected", variant="primary", scale=2,
            )
            accept_all_btn = gr.Button(
                "✅ Accept All", variant="secondary", scale=2,
            )
            reset_btn = gr.Button(
                "🔄 Reset", variant="stop", scale=1,
            )

        # ══════════════════════════════════════
        # OUTPUT TEXTBOX (the main result)
        # ══════════════════════════════════════
        output_text = gr.Textbox(
            label="📄 Output Text (after accepting changes)",
            lines=12, max_lines=30,
            interactive=True,
        )

        # ══════════════════════════════════════
        # RAW JSON (accordion, for debugging)
        # ══════════════════════════════════════
        with gr.Accordion("🔧 View Raw Suggestions (JSON)", open=False):
            raw_json = gr.JSON(label="Raw suggestions JSON")

        # ══════════════════════════════════════
        # Event: Run Workflow
        # ══════════════════════════════════════
        def on_run(text):
            for text_out, summary, df, changes_str, final_text in run_workflow(text):
                # Parse for raw JSON view
                try:
                    raw = json.loads(changes_str) if changes_str else []
                except (json.JSONDecodeError, ValueError):
                    raw = []
                yield text_out, summary, df, changes_str, final_text, raw

        run_btn.click(
            fn=on_run,
            inputs=[input_box],
            outputs=[
                current_text, summary_box, changes_table,
                current_changes, output_text, raw_json,
            ],
        )

        input_box.submit(
            fn=on_run,
            inputs=[input_box],
            outputs=[
                current_text, summary_box, changes_table,
                current_changes, output_text, raw_json,
            ],
        )

        # ══════════════════════════════════════
        # Event: Accept Selected
        # ══════════════════════════════════════
        accept_btn.click(
            fn=on_accept,
            inputs=[accept_idx, current_text, current_changes],
            outputs=[current_text],
        ).then(
            fn=lambda t: t,
            inputs=[current_text],
            outputs=[output_text],
        )

        # ══════════════════════════════════════
        # Event: Accept All
        # ══════════════════════════════════════
        accept_all_btn.click(
            fn=on_accept_all,
            inputs=[current_text, current_changes],
            outputs=[current_text],
        ).then(
            fn=lambda t: t,
            inputs=[current_text],
            outputs=[output_text],
        )

        # ══════════════════════════════════════
        # Event: Reset
        # ══════════════════════════════════════
        reset_btn.click(
            fn=lambda: (
                "", "👋 Enter text and click **Run** to start.",
                pd.DataFrame(), "[]", "", [],
            ),
            outputs=[
                current_text, summary_box, changes_table,
                current_changes, output_text, raw_json,
            ],
        )

    return demo


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    demo = build_interface()
    demo.launch(
        server_name="0.0.0.0",
        server_port=7861,
        share=True,
    )
