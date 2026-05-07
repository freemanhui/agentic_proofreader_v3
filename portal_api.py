"""
Agentic Proofreader v3 PoC — Portal API
=================================
FastAPI backend that wraps run_proofreader() and serves an interactive portal.

Routes:
  GET  /            →  Serve the portal HTML
  POST /api/proofread  →  Submit text, get structured change results

Usage:
    uvicorn portal_api:app --host 0.0.0.0 --port 8001 --reload
Or:
    python portal_api.py
"""

import json
import os
import sys
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# Add parent dir for proofreader imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proofreader_workflow import run_proofreader, AGENT_KEYS

app = FastAPI(title="Agentic Proofreader v3 PoC")

# ──────────────────────────────────────────────
# HTML Template (embedded for simplicity)
# ──────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agentic Proofreader v3 PoC</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f5f7fa; color: #1a1a2e; line-height: 1.6;
    padding: 24px; min-height: 100vh;
  }
  .container { max-width: 1200px; margin: 0 auto; }

  /* Header */
  header {
    text-align: center; padding: 20px 0 30px;
    border-bottom: 1px solid #e0e0e0; margin-bottom: 30px;
  }
  header h1 { font-size: 1.8em; color: #1a1a2e; }
  header p { color: #666; margin-top: 4px; font-size: 0.95em; }

  /* Input Section */
  .input-section { background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 24px; }
  .input-section textarea {
    width: 100%; min-height: 180px; padding: 16px; font-size: 0.95em;
    border: 1px solid #d0d5dd; border-radius: 8px; resize: vertical;
    font-family: inherit; line-height: 1.7; transition: border-color .2s;
  }
  .input-section textarea:focus { outline: none; border-color: #6366f1; box-shadow: 0 0 0 3px rgba(99,102,241,0.1); }
  .input-actions { display: flex; gap: 12px; margin-top: 16px; align-items: center; }
  .toggle-row { display: inline-flex; align-items: center; gap: 8px; color: #344054; font-size: 0.9em; cursor: pointer; user-select: none; }
  .toggle-row input { position: absolute; opacity: 0; pointer-events: none; }
  .toggle-ui { width: 38px; height: 22px; border-radius: 999px; background: #d0d5dd; position: relative; transition: background .2s; flex-shrink: 0; }
  .toggle-ui::after { content: ''; position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%; background: #fff; box-shadow: 0 1px 2px rgba(16,24,40,0.25); transition: transform .2s; }
  .toggle-row input:checked + .toggle-ui { background: #4f46e5; }
  .toggle-row input:checked + .toggle-ui::after { transform: translateX(16px); }
  .btn {
    display: inline-flex; align-items: center; gap: 8px; padding: 10px 24px;
    border: none; border-radius: 8px; font-size: 0.95em; font-weight: 600;
    cursor: pointer; transition: all .2s; text-decoration: none;
  }
  .btn-primary { background: #6366f1; color: #fff; }
  .btn-primary:hover { background: #4f46e5; transform: translateY(-1px); }
  .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
  .btn-secondary { background: #e8e8ef; color: #333; }
  .btn-secondary:hover { background: #d0d0dd; }
  .btn-accept { background: #10b981; color: #fff; }
  .btn-accept:hover { background: #059669; }
  .btn-reject { background: #ef4444; color: #fff; }
  .btn-reject:hover { background: #dc2626; }
  .btn-sm { padding: 6px 14px; font-size: 0.85em; }
  .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid rgba(255,255,255,0.3); border-top-color: #fff; border-radius: 50%; animation: spin .6s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Stats bar */
  .stats-bar { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
  .stat-card {
    background: #fff; border-radius: 10px; padding: 16px 20px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06); flex: 1; min-width: 140px;
  }
  .stat-card .num { font-size: 1.6em; font-weight: 700; }
  .stat-card .label { font-size: 0.85em; color: #666; margin-top: 2px; }

  /* Original Text */
  .text-panel { background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 24px; }
  .text-panel h3 { font-size: 1em; color: #666; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
  .text-content { font-size: 1em; line-height: 1.8; white-space: pre-wrap; word-wrap: break-word; }
  .change-highlight { background: #fef3c7; padding: 1px 0; border-radius: 2px; cursor: pointer; border-bottom: 2px solid #f59e0b; transition: background .2s; }
  .change-highlight:hover { background: #fde68a; }
  .change-highlight.accepted { background: #d1fae5; border-bottom-color: #10b981; }
  .change-highlight.rejected { background: #fce7f3; border-bottom-color: #ef4444; text-decoration: line-through; opacity: 0.5; }
  .tooltip { display: inline-block; font-size: 0.75em; color: #666; margin-left: 4px; }

  /* Changes List */
  .changes-section { margin-top: 0; }
  .change-card {
    background: #fff; border-radius: 10px; padding: 16px 20px;
    border-left: 4px solid #f59e0b; margin-bottom: 10px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06); transition: all .2s;
    display: flex; align-items: flex-start; gap: 16px;
  }
  .change-card.accepted { border-left-color: #10b981; background: #f0fdf4; }
  .change-card.rejected { border-left-color: #ef4444; background: #fef2f2; opacity: 0.7; }
  .change-card .change-info { flex: 1; min-width: 0; }
  .change-card .category { font-size: 0.8em; color: #6366f1; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
  .change-card .change-text { margin: 6px 0 4px; display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
  .change-card .original { color: #dc2626; text-decoration: line-through; }
  .change-card .arrow { color: #999; font-size: 0.9em; }
  .change-card .corrected { color: #059669; font-weight: 600; }
  .change-card .note { font-size: 0.85em; color: #666; margin-top: 2px; }
  .change-card .actions { display: flex; gap: 6px; flex-shrink: 0; align-items: center; }

  /* Agent Accordion */
  .accordion { margin-top: 24px; }
  .accordion details { background: #fff; border-radius: 10px; margin-bottom: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); overflow: hidden; }
  .accordion summary { padding: 14px 20px; cursor: pointer; font-weight: 600; font-size: 0.95em; background: #fafafa; }
  .accordion summary:hover { background: #f0f0f5; }
  .accordion .agent-content { padding: 16px 20px; font-size: 0.85em; white-space: pre-wrap; max-height: 300px; overflow-y: auto; background: #f9fafb; border-top: 1px solid #eef0f5; }
  .accordion .agent-content pre { white-space: pre-wrap; word-break: break-word; font-family: 'SF Mono', Consolas, monospace; font-size: 0.9em; }
  .soft-rules-section { margin-bottom: 24px; display: none; }
  .soft-rules-summary { padding: 12px 16px; background: #fff; border: 1px solid #e4e7ec; border-radius: 10px; margin-bottom: 12px; font-size: 0.9em; color: #475467; box-shadow: 0 1px 4px rgba(0,0,0,0.04); }
  .soft-rules-section details { background: #fff; border-radius: 10px; margin-bottom: 8px; border: 1px solid #e4e7ec; overflow: hidden; }
  .soft-rules-section summary { padding: 12px 16px; cursor: pointer; font-weight: 600; background: #fafafa; }
  .soft-rules-module { padding: 12px 16px 16px; }
  .rule-card { border: 1px solid #eaecf0; border-radius: 8px; padding: 12px; margin-bottom: 10px; background: #fcfcfd; }
  .rule-card .rule-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; font-size: 0.82em; color: #667085; }
  .rule-type-hard, .rule-type-soft { display: inline-flex; align-items: center; padding: 2px 7px; border-radius: 999px; font-size: 0.78em; font-weight: 700; text-transform: uppercase; }
  .rule-type-hard { background: #fef3c7; color: #92400e; }
  .rule-type-soft { background: #e0f2fe; color: #075985; }
  .rule-instruction { color: #1d2939; margin-bottom: 6px; }
  .rule-evidence { font-size: 0.84em; color: #667085; }
  .rendered-snippet { margin-top: 10px; }
  .rendered-snippet summary { background: #f9fafb; padding: 8px 0; font-size: 0.88em; color: #475467; }
  .rendered-snippet pre { white-space: pre-wrap; word-break: break-word; font-size: 0.82em; background: #f9fafb; border-radius: 8px; padding: 10px; color: #344054; }

  .status-msg { padding: 16px 20px; border-radius: 10px; margin-bottom: 20px; font-weight: 500; }
  .status-loading { background: #eef2ff; color: #4338ca; }
  .status-error { background: #fef2f2; color: #991b1b; }
  .status-success { background: #f0fdf4; color: #166534; }

  /* Accepted changes summary */
  .accepted-summary { background: #fff; border-radius: 12px; padding: 20px 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 24px; display: none; }
  .accepted-summary h3 { font-size: 1em; color: #059669; margin-bottom: 12px; }
  .accepted-summary .final-text { font-size: 1em; line-height: 1.8; white-space: pre-wrap; word-wrap: break-word; padding: 16px; background: #f7faf8; border-radius: 8px; border: 1px solid #d1f0db; }

  @media (max-width: 768px) {
    body { padding: 12px; }
    .change-card { flex-direction: column; }
    .change-card .actions { align-self: flex-end; }
  }
</style>
</head>
<body>
<div class="container" id="app">
  <header>
    <h1>📝 Agentic Proofreader v3 PoC</h1>
    <p>Editorial Portal — Review, accept, or reject AI-suggested changes</p>
  </header>

  <!-- Input Section -->
  <div class="input-section">
    <textarea id="inputText" placeholder="Paste your news article here..."></textarea>
    <div class="input-actions">
      <button class="btn btn-primary" id="submitBtn" onclick="submitProofread()">
        <span>🚀</span> Run ProofReader
      </button>
      <label class="toggle-row" title="Inject article-specific soft rules into specialist module prompts">
        <input type="checkbox" id="softRulesToggle">
        <span class="toggle-ui"></span>
        <span>Soft rules</span>
      </label>
      <span id="statusText" style="color:#666;font-size:0.9em;"></span>
    </div>
  </div>

  <!-- Status Message -->
  <div id="statusMsg" class="status-msg" style="display:none;"></div>

  <!-- Stats -->
  <div id="statsBar" class="stats-bar" style="display:none;">
    <div class="stat-card"><div class="num" id="statTotal">0</div><div class="label">Total Changes</div></div>
    <div class="stat-card"><div class="num" id="statAccepted">0</div><div class="label">Accepted</div></div>
    <div class="stat-card"><div class="num" id="statRejected">0</div><div class="label">Rejected</div></div>
    <div class="stat-card"><div class="num" id="statPending">0</div><div class="label">Pending</div></div>
  </div>

  <!-- Soft Rules -->
  <div id="softRulesSection" class="soft-rules-section">
    <h3 style="margin-bottom:12px;">Article-Specific Soft Rules</h3>
    <div id="softRulesDetails"></div>
  </div>

  <!-- Accepted Final Text -->
  <div id="acceptedSummary" class="accepted-summary">
    <h3>✅ Accepted Changes Applied</h3>
    <div id="acceptedFinalText" class="final-text"></div>
  </div>

  <!-- Original Text with Highlights -->
  <div id="originalTextPanel" class="text-panel" style="display:none;">
    <h3>📄 Original Text with Change Annotations</h3>
    <div id="originalTextContent" class="text-content"></div>
  </div>

  <!-- Changes List -->
  <div id="changesSection" class="changes-section" style="display:none;">
    <h3 style="margin-bottom:14px;">✏️ Suggested Changes</h3>
    <div id="changesList"></div>
  </div>

  <!-- Agent Accordion -->
  <div id="agentAccordion" class="accordion" style="display:none;">
    <h3 style="margin-bottom:12px;margin-top:24px;">🧠 Agent Raw Outputs</h3>
    <div id="agentDetails"></div>
  </div>
</div>

<script>
// ── State ──
let changes = [];
let originalText = '';
let acceptedMap = {}; // index -> true

// ── Submit ──
async function submitProofread() {
  const text = document.getElementById('inputText').value.trim();
  if (!text) { showStatus('Please enter some text.', 'error'); return; }
  const enableSoftRules = document.getElementById('softRulesToggle').checked;

  const btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Processing...';
  showStatus('⏳ Chunking text and running MQ workers (max 5 concurrent)... This may take a moment.', 'loading');

  try {
    const res = await fetch('/api/proofread', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, enable_soft_rules: enableSoftRules }),
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    renderResults(data);
    showStatus(`✅ Workflow complete! ${data.chunk_count} chunks, ${data.total_changes} changes found.`, 'success');
  } catch (err) {
    showStatus('❌ Error: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span>🚀</span> Run ProofReader';
  }
}

// ── Render ──
function renderResults(data) {
  originalText = data.original_text;
  changes = data.changes || [];
  acceptedMap = {};

  // Stats
  document.getElementById('statTotal').textContent = changes.length;
  document.getElementById('statAccepted').textContent = '0';
  document.getElementById('statRejected').textContent = '0';
  document.getElementById('statPending').textContent = changes.length;
  document.getElementById('statsBar').style.display = 'flex';

  // Original Text
  document.getElementById('originalTextPanel').style.display = 'block';
  renderHighlightedText();

  // Changes
  document.getElementById('changesSection').style.display = 'block';
  renderChangesList();

  // Agent accordion
  document.getElementById('agentAccordion').style.display = 'block';
  renderSoftRules(data);
  renderAgentOutputs(data);

  // Hide accepted summary
  document.getElementById('acceptedSummary').style.display = 'none';
  document.getElementById('acceptedFinalText').textContent = '';

  // Scroll to results
  document.getElementById('originalTextPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderSoftRules(data) {
  const section = document.getElementById('softRulesSection');
  const container = document.getElementById('softRulesDetails');
  const debug = data.soft_rules_debug || {};
  const summary = debug.summary || {};
  const trace = debug.injected_rule_trace_by_module || {};
  const rendered = debug.rendered_prompt_by_module || {};
  const labels = {
    'country': 'Country',
    'greater_china': 'Greater China',
    'hyphenation': 'Hyphenation',
    'style_grammar': 'Style & Grammar',
    'terminology': 'Terminology',
  };
  const keys = ['country', 'greater_china', 'hyphenation', 'style_grammar', 'terminology'];
  const ruleCounts = summary.rule_counts || {};
  const totalRules = keys.reduce((sum, key) => sum + Number(ruleCounts[key] || 0), 0);
  const source = summary.source_url || summary.source_path || 'default source';
  const enabledText = debug.enabled ? 'enabled' : 'disabled';

  let html = `<div class="soft-rules-summary">
    Soft rules ${enabledText}. ${totalRules} injected rule${totalRules === 1 ? '' : 's'} matched. Source: ${escapeHtml(source)}
  </div>`;

  for (const key of keys) {
    const rules = Array.isArray(trace[key]) ? trace[key] : [];
    const snippet = rendered[key] || '';
    html += `<details ${rules.length ? 'open' : ''}>
      <summary>${escapeHtml(labels[key])} (${rules.length})</summary>
      <div class="soft-rules-module">`;

    if (!rules.length) {
      html += `<div class="rule-evidence">No matched rules.</div>`;
    }

    for (const rule of rules) {
      const type = (rule.rule_type || 'soft').toLowerCase() === 'hard' ? 'hard' : 'soft';
      const evidence = Array.isArray(rule.evidence_pairs) ? rule.evidence_pairs : [];
      html += `<div class="rule-card">
        <div class="rule-meta">
          <span class="rule-type-${type}">${escapeHtml(type)}</span>
          <span>${escapeHtml(rule.rule_family || 'SoftRule')}</span>
          <span>${escapeHtml(rule.subcategory || 'General')}</span>
        </div>
        <div class="rule-instruction">${escapeHtml(rule.instruction || '')}</div>`;
      if (evidence.length) {
        html += `<div class="rule-evidence">${evidence.map(pair => {
          const origin = escapeHtml(pair.origin || '');
          const accepted = escapeHtml(pair.accepted || '');
          return `${origin} → ${accepted}`;
        }).join('<br>')}</div>`;
      }
      html += `</div>`;
    }

    if (snippet) {
      html += `<details class="rendered-snippet">
        <summary>Rendered prompt snippet</summary>
        <pre>${escapeHtml(snippet)}</pre>
      </details>`;
    }

    html += `</div></details>`;
  }

  container.innerHTML = html;
  section.style.display = 'block';
}

function renderHighlightedText() {
  const container = document.getElementById('originalTextContent');
  if (!changes.length) {
    container.textContent = originalText;
    return;
  }

  // Sort changes by position in original text (first occurrence of original text)
  const sorted = changes.map((c, i) => ({ ...c, _idx: i }))
    .sort((a, b) => originalText.indexOf(a.original) - originalText.indexOf(b.original)
                     || (b.original.length - a.original.length));

  // Build highlighted HTML
  let html = '';
  let lastEnd = 0;
  for (const change of sorted) {
    const pos = originalText.indexOf(change.original, lastEnd);
    if (pos === -1) continue;

    // Text before this change
    html += escapeHtml(originalText.slice(lastEnd, pos));

    // The change span
    const status = acceptedMap[change._idx] === true ? 'accepted'
                 : acceptedMap[change._idx] === false ? 'rejected' : '';
    html += `<span class="change-highlight ${status}" title="Click to accept/reject" onclick="toggleChange(${change._idx})">`;
    html += escapeHtml(change.original);
    html += `</span>`;

    lastEnd = pos + change.original.length;
  }
  html += escapeHtml(originalText.slice(lastEnd));

  container.innerHTML = html;
}

function renderChangesList() {
  const container = document.getElementById('changesList');
  let html = '';
  for (let i = 0; i < changes.length; i++) {
    const c = changes[i];
    const status = acceptedMap[i] === true ? 'accepted' : acceptedMap[i] === false ? 'rejected' : '';
    const cat = c.category || 'General';
    html += `
      <div class="change-card ${status}" data-idx="${i}">
        <div class="change-info">
          <div class="category">${escapeHtml(cat)}</div>
          <div class="change-text">
            <span class="original">${escapeHtml(c.original)}</span>
            <span class="arrow">→</span>
            <span class="corrected">${escapeHtml(c.corrected)}</span>
          </div>
          ${c.note ? `<div class="note">💡 ${escapeHtml(c.note)}</div>` : ''}
        </div>
        <div class="actions">
          ${acceptedMap[i] !== true ? `<button class="btn btn-accept btn-sm" onclick="acceptChange(${i})">✓ Accept</button>` : ''}
          ${acceptedMap[i] !== false ? `<button class="btn btn-reject btn-sm" onclick="rejectChange(${i})">✗ Reject</button>` : ''}
          ${acceptedMap[i] === true ? `<span style="color:#059669;font-weight:600;">✓ Accepted</span>` : ''}
          ${acceptedMap[i] === false ? `<span style="color:#dc2626;font-weight:600;">✗ Rejected</span>` : ''}
        </div>
      </div>`;
  }
  container.innerHTML = html;
}

function renderAgentOutputs(data) {
  const container = document.getElementById('agentDetails');
  const agentLabels = {
    'country': '🌍 Country Module',
    'greater_china': '🇨🇳 Greater China Module',
    'hyphenation': '🔗 Hyphenation Module',
    'style_grammar': '✍️ Style & Grammar Module',
    'terminology': '📖 Terminology Module',
  };
  let html = '';

  // Stats row
  html += `<div style="padding:12px 20px;background:#f9fafb;border-radius:8px;margin-bottom:12px;font-size:0.9em;color:#555;">
    📊 <strong>MQ Stats:</strong> ${data.chunk_count} chunks created, ${data.successful_chunks} successfully processed
    | 🔁 ${data.duplicate_words_removed} duplicate word items removed
    | 🧹 ${data.postprocessor_removed} postprocessor items removed
    | 🛡️ ${data.whitelist_removed} whitelist items removed
  </div>`;

  for (const key of ['country', 'greater_china', 'hyphenation', 'style_grammar', 'terminology']) {
    const label = agentLabels[key] || key;
    const content = data[key + '_result'] || '[No result]';
    const isJson = content.trim().startsWith('{') || content.trim().startsWith('[');
    html += `
      <details>
        <summary>${label}</summary>
        <div class="agent-content"><pre>${isJson ? syntaxHighlightJson(content) : escapeHtml(content)}</pre></div>
      </details>`;
  }
  // General agent
  html += `
    <details>
      <summary>📋 General Agent — Final Result</summary>
      <div class="agent-content"><pre>${escapeHtml(data.general_result || '[No result]')}</pre></div>
    </details>`;
  html += `
    <details>
      <summary>✅ Whitelist Result (Final Output)</summary>
      <div class="agent-content"><pre>${escapeHtml(data.whitelist_result || '[No result]')}</pre></div>
    </details>`;

  container.innerHTML = html;
}

// ── Actions ──
function acceptChange(idx) {
  if (acceptedMap[idx] === true) return;
  acceptedMap[idx] = true;
  updateAfterAction();
}

function rejectChange(idx) {
  if (acceptedMap[idx] === false) return;
  acceptedMap[idx] = false;
  updateAfterAction();
}

function toggleChange(idx) {
  if (acceptedMap[idx] === true) {
    // Reset to pending
    delete acceptedMap[idx];
  } else if (acceptedMap[idx] === false) {
    delete acceptedMap[idx];
  } else {
    acceptedMap[idx] = true;
  }
  updateAfterAction();
}

function updateAfterAction() {
  // Update stats
  const accepted = Object.entries(acceptedMap).filter(([_, v]) => v === true).length;
  const rejected = Object.entries(acceptedMap).filter(([_, v]) => v === false).length;
  const pending = changes.length - accepted - rejected;
  document.getElementById('statAccepted').textContent = accepted;
  document.getElementById('statRejected').textContent = rejected;
  document.getElementById('statPending').textContent = pending;

  // Re-render
  renderHighlightedText();
  renderChangesList();

  // Build final text
  if (accepted > 0) {
    let finalText = originalText;
    // Apply accepted changes in reverse order of position to preserve indices
    const acceptedChanges = changes
      .map((c, i) => ({ ...c, idx: i }))
      .filter((c, i) => acceptedMap[i] === true)
      .sort((a, b) => originalText.indexOf(b.original) - originalText.indexOf(a.original));

    for (const change of acceptedChanges) {
      const pos = finalText.indexOf(change.original);
      if (pos !== -1) {
        finalText = finalText.slice(0, pos) + change.corrected + finalText.slice(pos + change.original.length);
      }
    }

    document.getElementById('acceptedFinalText').textContent = finalText;
    document.getElementById('acceptedSummary').style.display = 'block';
  } else {
    document.getElementById('acceptedSummary').style.display = 'none';
  }
}

// ── Helpers ──
function showStatus(msg, type) {
  const el = document.getElementById('statusMsg');
  el.textContent = msg;
  el.className = 'status-msg status-' + type;
  el.style.display = 'block';
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&').replace(/</g, '<').replace(/>/g, '>')
    .replace(/"/g, '"').replace(/'/g, '&#039;');
}

function syntaxHighlightJson(str) {
  try {
    const parsed = JSON.parse(str);
    return syntaxHighlight(JSON.stringify(parsed, null, 2));
  } catch {
    return escapeHtml(str);
  }
}

function syntaxHighlight(json) {
  json = escapeHtml(json);
  return json.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
    function (match) {
      let cls = 'number';
      if (match.startsWith('"') && match.endsWith(':')) { cls = 'key'; }
      else if (/true|false/.test(match)) { cls = 'boolean'; }
      else if (/null/.test(match)) { cls = 'null'; }
      else if (match.startsWith('"')) { cls = 'string'; }
      return '<span style="color:' + ({
        'key': '#881391', 'string': '#0451a5', 'number': '#098658',
        'boolean': '#0000ff', 'null': '#808080'
      }[cls] || '#000') + '">' + match + '</span>';
    }
  );
}
</script>
</body>
</html>"""


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_portal():
    return HTMLResponse(HTML_TEMPLATE)


@app.post("/api/proofread")
async def api_proofread(request: Request):
    body = await request.json()
    text = body.get("text", "").strip()
    enable_soft_rules = bool(body.get("enable_soft_rules", False))
    if not text:
        return JSONResponse({"detail": "No text provided"}, status_code=400)

    # Run the full workflow
    result = run_proofreader(text, enable_soft_rules=enable_soft_rules)

    # Parse the whitelist result (final JSON array of changes)
    whitelist_raw = result.get("whitelist_result", "")
    changes = _parse_changes(whitelist_raw)

    # Ensure each change has a unique index key
    for i, change in enumerate(changes):
        change.setdefault("category", "General")
        change.setdefault("note", "")

    return {
        "original_text": text,
        "changes": changes,
        "total_changes": len(changes),
        "chunk_count": result.get("_chunk_count", 0),
        "successful_chunks": result.get("_successful_chunks", 0),
        "duplicate_words_removed": result.get("duplicate_words_removed", 0),
        "postprocessor_removed": result.get("postprocessor_removed", 0),
        "whitelist_removed": result.get("whitelist_removed", 0),
        "soft_rules_debug": result.get("soft_rules_debug", {}),
        # Raw outputs for agent accordion
        "country_result": result.get("country_result", ""),
        "greater_china_result": result.get("greater_china_result", ""),
        "hyphenation_result": result.get("hyphenation_result", ""),
        "style_grammar_result": result.get("style_grammar_result", ""),
        "terminology_result": result.get("terminology_result", ""),
        "general_result": result.get("general_result", ""),
        "whitelist_result": whitelist_raw,
    }


def _parse_changes(raw: str) -> list:
    """Parse the whitelist_result (JSON array of changes)."""
    if not raw or raw.startswith("[ERROR"):
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return []


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("portal_api:app", host="0.0.0.0", port=8001, reload=True)
