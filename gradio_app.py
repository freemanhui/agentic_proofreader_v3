"""
ProofReader Rewrite — Gradio Demo
===================================
A Gradio-based web UI for the ProofReader Rewrite workflow.

Pipeline:
  0. Text Preprocessor (remove whitespace/noise/invisible chars/duplicate paras)
  1. Recursively chunks input text (paragraph → sentence → token)
  2. Pushes chunks into task queue
  3. Workers (max 5) pop chunks and run all 6 specialist agents
  4. Aggregates per-agent results across all chunks
  5. Feeds to General Agent for final consolidation

Usage:
    python gradio_app.py
"""

import os
import sys
import time
import concurrent.futures

import gradio as gr

# Import the workflow modules
from proofreader_workflow import (
    SYSTEM_PROMPTS,
    call_llm,
    build_agent_prompt,
    build_general_prompt,
    run_proofreader,
    AGENT_KEYS,
)

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
AGENT_LABELS = {
    "country": "🌍 Country Module",
    "greater_china": "🇨🇳 Greater China Module",
    "hyphenation": "🔗 Hyphenation Module",
    "style_grammar": "✍️ Style & Grammar Module",
    "terminology": "📖 Terminology Module",
    "tansa": "📋 Tansa Guideline Module",
}

AGENT_NAMES = {
    "country": "country-task-system-prompt",
    "greater_china": "greater-china-task-system-prompt",
    "hyphenation": "hyphenation-task-system-prompt",
    "style_grammar": "style-grammar-task-system-prompt",
    "terminology": "terminology-task-system-prompt",
}


# ──────────────────────────────────────────────
# Processing logic for Gradio
# ──────────────────────────────────────────────
def process_text(input_text: str, progress=gr.Progress()):
    """
    Process input text through the ProofReader workflow.
    Yields intermediate updates for display in Gradio.
    """
    if not input_text or input_text.strip() == "":
        yield (
            "Please enter some text to audit.",
            *([""] * 6),
            "",
        )
        return

    progress(0, desc="Starting ProofReader workflow...")
    yield (
        f"📝 **Input Text:**\n{input_text}\n\n"
        f"⏳ Preprocessing text (noise/whitespace cleanup) and chunking via MQ...",
        *([""] * 6),
        "",
    )

    # ── Run the full MQ-based workflow ──
    progress(0.05, desc="Text Preprocessing (remove noise/invisible/duplicate chars)...")
    progress(0.1, desc="Recursive chunking (paragraph→sentence→token)...")
    progress(0.2, desc="MQ workers processing chunks (max 5 concurrent)...")

    result = run_proofreader(input_text)

    chunk_count = result.get("_chunk_count", 0)
    successful_chunks = result.get("_successful_chunks", 0)

    progress(0.85, desc="All chunks processed! Running General consolidation agent...")

    # Build display results for each agent
    display_results = []
    for key in AGENT_KEYS:
        result_text = result.get(f"{key}_result", "[No result]")
        display_results.append(result_text)

    # Get general result
    general_result = result.get("general_result", "[No result]")

    # Get duplicate word filter results
    duplicate_words_removed = result.get("duplicate_words_removed", 0)

    # Get postprocessor results
    postprocessed_result = result.get("postprocessed_result", "")
    postprocessor_removed = result.get("postprocessor_removed", 0)

    # Get whitelist results
    whitelist_result = result.get("whitelist_result", "")
    whitelist_removed = result.get("whitelist_removed", 0)

    progress(1.0, desc="Done!")
    yield (
        (
            f"📝 **Input Text:**\n{input_text}\n\n"
            f"✅ **Workflow Complete!**\n\n"
            f"📊 **MQ Processing Stats:** {chunk_count} chunks created, "
            f"{successful_chunks} successfully processed "
            f"(max 5 concurrent via queue.Queue MQ).\n\n"
            f"🔁 **Duplicate Word Filter:** {duplicate_words_removed} items removed "
            f"(corrected had duplicate words like 'the the').\n\n"
            f"🧹 **Postprocessor:** {postprocessor_removed} items removed "
            f"(no-ops, noise, and/or duplicates filtered out).\n\n"
            f"🛡️ **Whitelist Filter:** {whitelist_removed} items removed "
            f"(original matched whitelist keywords).\n\n"
            f"➡️ Results shown below."
        ),
        *display_results,
        whitelist_result if whitelist_result else general_result,
    )


# ──────────────────────────────────────────────
# Build Gradio Interface
# ──────────────────────────────────────────────
def build_interface():
    with gr.Blocks(
        title="ProofReader Rewrite Workflow",
    ) as demo:

        gr.Markdown(
            """
            # 📝 ProofReader Rewrite Workflow
            <p style="font-size: 1.1em; color: #666;">
            Enter text below and the system will <strong>preprocess</strong> it
            (remove invisible chars/whitespace/noise/duplicate paragraphs),
            then <strong>recursively chunk</strong> (paragraph → sentence → token),
            queue chunks through a <strong>message queue</strong> for
            <strong>5 concurrent workers</strong>, each running
            <strong>6 specialist agents</strong> in parallel, then consolidate
            all findings via a <strong>General Agent</strong>.
            </p>
            """
        )

        with gr.Row():
            with gr.Column(scale=1):
                input_box = gr.Textbox(
                    label="📥 Input Text",
                    placeholder="Paste or type the text you want to audit here...",
                    lines=8,
                )

                submit_btn = gr.Button(
                    "🚀 Run ProofReader Workflow",
                    variant="primary",
                    size="lg",
                )

                gr.Markdown(
                    """
                    ---
                    **LangGraph + MQ Architecture (7 Nodes):**
                    ```
                    Input ──→ ⓪ Preprocessing Node (TextPreprocessor 清洗)
                                    ↓
                            ① Chunking Node (NewsChunker 递归分块)
                                    ↓
                            ② MQ Distributor Node (queue.Queue)
                               ┌──┬──┬──┬──┬──┐
                               │W1│W2│W3│W4│W5│  5 MQ Workers
                               │ 6│ 6│ 6│ 6│ 6│  6 Agents each (5 LLM + 1 Tansa)
                               └──┴──┴──┴──┴──┘
                                    ↓
                            ③ General Agent Node (最终整合)
                                    ↓
                            ④ Duplicate Word Filter (移除重复词条目)
                                    ↓
                            ⑤ Postprocessor Node (去 noop/noise/重复)
                                    ↓
                            ⑥ Whitelist Filter Node (白名单过滤)
                                    ↓
                              Final Output
                    ```
                    """
                )


            with gr.Column(scale=2):
                status_box = gr.Markdown(
                    value="👋 Enter text and click **Run** to start.",
                )

                # ── General Agent result ──
                gr.Markdown("## 📋 General Agent — Final Result")
                general_output = gr.Markdown(
                    value="*(Will appear here after running)*",
                    elem_classes="general-result",
                )

                with gr.Accordion("🔍 View Individual Agent Results", open=False):
                    with gr.Tabs():
                        with gr.Tab("🌍 Country"):
                            country_output = gr.Markdown(
                                value="", elem_classes="agent-tab"
                            )
                        with gr.Tab("🇨🇳 Greater China"):
                            greater_china_output = gr.Markdown(
                                value="", elem_classes="agent-tab"
                            )
                        with gr.Tab("🔗 Hyphenation"):
                            hyphenation_output = gr.Markdown(
                                value="", elem_classes="agent-tab"
                            )
                        with gr.Tab("✍️ Style & Grammar"):
                            style_grammar_output = gr.Markdown(
                                value="", elem_classes="agent-tab"
                            )
                        with gr.Tab("📖 Terminology"):
                            terminology_output = gr.Markdown(
                                value="", elem_classes="agent-tab"
                            )
                        with gr.Tab("📋 Tansa Guideline"):
                            tansa_output = gr.Markdown(
                                value="", elem_classes="agent-tab"
                            )

        # ── Event Handler ──
        submit_event = submit_btn.click(
            fn=process_text,
            inputs=[input_box],
            outputs=[
                status_box,
                country_output,
                greater_china_output,
                hyphenation_output,
                style_grammar_output,
                terminology_output,
                tansa_output,
                general_output,
            ],
        )

        # Also trigger on Enter key in textbox
        input_box.submit(
            fn=process_text,
            inputs=[input_box],
            outputs=[
                status_box,
                country_output,
                greater_china_output,
                hyphenation_output,
                style_grammar_output,
                terminology_output,
                tansa_output,
                general_output,
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
        server_port=7860,
        share=True,
        css="""
        .general-result {
            border: 2px solid #f59e0b;
            border-radius: 12px;
            padding: 16px;
            background: #fffbeb;
            margin-bottom: 16px;
        }
        .general-result p {
            margin: 0;
        }
        .agent-tab {
            border-left: 4px solid #6366f1;
        }
        """,
    )
