// meetingsAI — client helpers, right-pane renderers, WebSocket wiring.
// Phase E: right pane restructured into Meeting Pulse, What Changed,
// Action Tracker, Threads, Questions, Potential response, Decisions,
// Risks, Facts, State meta.

// ---------- basic helpers ----------

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function logout() {
  try { await fetch('/api/auth/logout', {method: 'POST', credentials: 'same-origin'}); } catch (_) {}
  window.location.href = '/';
}
document.addEventListener('DOMContentLoaded', () => {
  const lb = document.getElementById('logout-btn');
  if (lb) lb.addEventListener('click', logout);
});

function setText(id, txt) {
  const el = document.getElementById(id);
  if (el) el.textContent = txt;
}

function show(id, yes) {
  const el = document.getElementById(id);
  if (el) el.hidden = !yes;
}

// ---------- provenance modal ----------

const _provCache = {};  // discussion_id -> discussion object

async function loadDiscussionForModal(meetingId, discussionId) {
  if (_provCache[discussionId]) return _provCache[discussionId];
  const r = await fetch(
    `/api/meetings/${encodeURIComponent(meetingId)}/discussions?limit=500`,
    { credentials: 'same-origin' });
  if (!r.ok) return null;
  const body = await r.json();
  for (const d of body.items || []) {
    _provCache[d.id] = d;
  }
  return _provCache[discussionId] || null;
}

function closeProvenanceModal() {
  const b = document.getElementById('prov-modal-backdrop');
  if (b) b.remove();
}

async function openProvenanceModal(meetingId, discussionId, sourceText) {
  closeProvenanceModal();
  const backdrop = document.createElement('div');
  backdrop.className = 'prov-modal-backdrop';
  backdrop.id = 'prov-modal-backdrop';
  backdrop.addEventListener('click', (e) => {
    if (e.target === backdrop) closeProvenanceModal();
  });

  const d = await loadDiscussionForModal(meetingId, discussionId);
  const when = d && d.started_at ? d.started_at : '(unknown time)';
  const text = d && d.text ? d.text : '(discussion not found)';

  backdrop.innerHTML = `
    <div class="prov-modal" role="dialog" aria-modal="true">
      <h3>Discussion #${escapeHtml(discussionId)}</h3>
      <div class="prov-meta">${escapeHtml(when)}</div>
      <div class="prov-source-text">${escapeHtml(text)}</div>
      ${sourceText ? `<div class="prov-meta">Referenced phrase:</div>
      <div class="prov-source-text">${escapeHtml(sourceText)}</div>` : ''}
      <button class="prov-close" id="prov-close">Close</button>
    </div>`;
  document.body.appendChild(backdrop);
  const btn = document.getElementById('prov-close');
  if (btn) btn.addEventListener('click', closeProvenanceModal);
  document.addEventListener('keydown', function escHandler(e) {
    if (e.key === 'Escape') { closeProvenanceModal(); document.removeEventListener('keydown', escHandler); }
  });
}

// Attach the click handler via event delegation.
document.addEventListener('click', (e) => {
  const a = e.target.closest('a.prov');
  if (!a) return;
  e.preventDefault();
  const meetingId = a.dataset.meetingId;
  const discussionId = a.dataset.discussionId;
  const sourceText = a.dataset.sourceText || '';
  if (meetingId && discussionId) openProvenanceModal(meetingId, discussionId, sourceText);
});

function provLink(meetingId, discussionId, sourceText) {
  if (discussionId === null || discussionId === undefined) return '';
  const id = Number(discussionId);
  if (!Number.isFinite(id)) return '';
  const st = sourceText ? ` data-source-text="${escapeHtml(sourceText)}"` : '';
  return ` <a class="prov" data-meeting-id="${escapeHtml(meetingId)}" data-discussion-id="${id}"${st}>discussion #${id}</a>`;
}

// ---------- delta rendering ----------

function renderDelta(meetingId, delta) {
  show('delta-block', false);
  const body = document.getElementById('delta-body');
  if (!body) return;
  if (!delta) { body.innerHTML = ''; return; }

  const groups = [];
  const byThread = {};

  const push = (groupKey, tag, text, discussionId, sourceText) => {
    const key = groupKey || '';
    if (!byThread[key]) byThread[key] = { group: groupKey || null, items: [] };
    byThread[key].items.push({ tag, text, discussionId, sourceText });
  };

  for (const f of delta.new_facts || []) {
    push(f.thread_key || null, 'NEW', `${f.key}: ${f.value}`, null, null);
  }
  for (const f of delta.updated_facts || []) {
    push(f.thread_key || null, 'UPDATE', `${f.key}: ${f.value}`, null, null);
  }
  for (const f of delta.superseded_facts || []) {
    push(f.thread_key || null, 'CHANGE', `${f.key} superseded`, null, null);
  }
  for (const t of delta.new_threads || []) {
    push(t.key, 'NEW', `Thread "${t.title}"`, null, null);
  }
  for (const a of delta.new_actions || []) {
    push(a.thread_key || null, 'ACTION', `${a.text}${a.owner ? ' — ' + a.owner : ''}`, null, null);
  }
  for (const ch of delta.status_changes || []) {
    push(ch.thread_key || null, 'CHANGE', `action #${ch.id}: ${ch.from} → ${ch.to}`, null, null);
  }
  for (const d of delta.new_decisions || []) {
    push(d.thread_key || null, 'NEW', `Decision: ${d.text}`, null, null);
  }
  for (const r of delta.new_risks || []) {
    push(r.thread_key || null, 'NEW', `Risk: ${r.text}`, null, null);
  }
  for (const q of delta.new_questions || []) {
    push(q.thread_key || null, 'ACTION', `Question: ${q.text}`, null, null);
  }
  for (const q of delta.resolved_questions || []) {
    push(q.thread_key || null, 'RESOLVED', `Question closed: ${q.text}`, null, null);
  }

  const keys = Object.keys(byThread);
  if (!keys.length) { body.innerHTML = ''; return; }

  // Order: named groups first (alphabetical), then unnamed group.
  keys.sort((a, b) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b)));
  body.innerHTML = keys.map((k) => {
    const g = byThread[k];
    const head = g.group ? `<div class="delta-group-head">${escapeHtml(g.group)} — attention</div>` : '';
    const items = g.items.map((it) => `
      <div class="delta-item">
        <span class="delta-tag ${escapeHtml(it.tag)}">${escapeHtml(it.tag)}</span>
        <span>${escapeHtml(it.text)}</span>
        ${it.discussionId ? provLink(meetingId, it.discussionId, it.sourceText) : ''}
      </div>`).join('');
    return `<div class="delta-group">${head}${items}</div>`;
  }).join('');

  show('delta-block', true);
}
// ---------- pulse ----------

function renderPulse(meeting, state) {
  const modeBadge = document.getElementById('mode-badge');
  const objective = document.getElementById('objective-text');
  const understanding = document.getElementById('pulse-understanding');
  const flags = document.getElementById('pulse-flags');

  if (modeBadge) {
    if (state.mode) {
      modeBadge.textContent = state.mode + (state.mode_inferred ? ' (inferred)' : '');
      modeBadge.classList.add('mode-set');
    } else {
      modeBadge.textContent = 'mode: (pending inference)';
      modeBadge.classList.remove('mode-set');
    }
  }
  if (objective) {
    objective.textContent = meeting && meeting.objective
      ? `Objective: ${meeting.objective}` : '';
  }
  if (understanding) {
    understanding.textContent = state.understanding || '—';
    understanding.classList.toggle('muted', !state.understanding);
  }
  if (flags) {
    const parts = [];
    for (const c of state.contradictions || []) parts.push(`⚠ ${escapeHtml(c)}`);
    for (const m of state.missing_information || []) parts.push(`? ${escapeHtml(m)}`);
    flags.innerHTML = parts.length
      ? parts.map((p) => `<span class="pulse-flag">${p}</span>`).join('')
      : '';
  }
}

// ---------- action tracker ----------

function renderActions(meeting, state) {
  const body = document.getElementById('action-body');
  const count = document.getElementById('action-count');
  const items = state.action_items || [];
  if (!items.length) { show('action-block', false); return; }

  const threadTitle = (tid) => {
    const t = (state.threads || []).find((x) => x.id === tid);
    return t ? t.title : null;
  };

  const sorted = [...items].sort((a, b) => {
    if (a.status !== b.status) return a.status === 'open' ? -1 : 1;
    if (a.due && b.due) return a.due < b.due ? -1 : 1;
    return (a.id || 0) - (b.id || 0);
  });

  body.innerHTML = sorted.map((a) => {
    const owner = a.owner ? ` — <b>${escapeHtml(a.owner)}</b>` : '';
    const due = a.due ? ` · due ${escapeHtml(a.due)}` : '';
    const tt = threadTitle(a.thread_id);
    const thread = tt ? ` · <span class="action-thread">${escapeHtml(tt)}</span>` : '';
    return `
      <div class="action-item">
        <span class="action-status-pill action-status-${escapeHtml(a.status)}">${escapeHtml(a.status)}</span>
        <div class="action-body-main">
          <div class="action-text">${escapeHtml(a.text)}</div>
          <div class="action-meta">${owner}${due}${thread}${provLink(state.meeting_id, a.source_discussion_id, a.source_text)}</div>
        </div>
      </div>`;
  }).join('');

  if (count) count.textContent = `(${items.length})`;
  show('action-block', true);
}

// ---------- threads ----------

function renderThreads(state) {
  const body = document.getElementById('thread-body');
  const count = document.getElementById('thread-count');
  const items = state.threads || [];
  if (!items.length) { show('thread-block', false); return; }

  body.innerHTML = items.map((t) => `
    <div class="thread-card">
      <div class="thread-title">${escapeHtml(t.title)}
        <span class="thread-status thread-status-${escapeHtml(t.status)}">${escapeHtml(t.status)}</span>
      </div>
      ${t.summary ? `<div class="thread-summary">${escapeHtml(t.summary)}</div>` : ''}
      <div class="thread-counts">
        <span>facts ${t.fact_count}</span>
        <span>actions ${t.action_count}</span>
        <span>risks ${t.risk_count}</span>
        <span>questions ${t.question_count}</span>
        <span>decisions ${t.decision_count}</span>
      </div>
    </div>`).join('');

  if (count) count.textContent = `(${items.length})`;
  show('thread-block', true);
}

// ---------- questions ----------

function renderQuestions(state, kind) {
  const bodyId = kind === 'now' ? 'q-now-body' : 'q-later-body';
  const countId = kind === 'now' ? 'q-now-count' : 'q-later-count';
  const blockId = kind === 'now' ? 'questions-now-block' : 'questions-later-block';
  const all = (kind === 'now' ? state.questions_now : state.questions_later) || [];
  const items = all.slice(0, 5);

  const body = document.getElementById(bodyId);
  const count = document.getElementById(countId);
  if (!items.length) { show(blockId, false); return; }

  const threadTitle = (tid) => {
    const t = (state.threads || []).find((x) => x.id === tid);
    return t ? t.title : null;
  };

  body.innerHTML = items.map((q) => {
    const tt = threadTitle(q.thread_id);
    const meta = [];
    if (tt) meta.push(`thread: ${escapeHtml(tt)}`);
    if (q.related_action) meta.push(`action: ${escapeHtml(q.related_action)}`);
    if (q.related_risk) meta.push(`risk: ${escapeHtml(q.related_risk)}`);
    const rationale = q.rationale ? `<div class="q-rationale">${escapeHtml(q.rationale)}</div>` : '';
    const metaLine = meta.length ? `<div class="q-rel">${meta.join(' · ')}</div>` : '';
    return `
      <div class="q-item">
        <div class="q-text">${escapeHtml(q.text)}${provLink(state.meeting_id, q.source_discussion_id, q.source_text)}</div>
        ${rationale}
        ${metaLine}
      </div>`;
  }).join('');

  if (count) count.textContent = `(${items.length}${all.length > 5 ? ' of ' + all.length : ''})`;
  show(blockId, true);
}

// ---------- potential response ----------

function renderResponse(state) {
  const body = document.getElementById('response-body');
  if (!state.potential_response) { show('response-block', false); return; }
  body.innerHTML = `<div>${escapeHtml(state.potential_response)}</div>`;
  show('response-block', true);
}

// ---------- decisions ----------

function renderDecisions(state) {
  const body = document.getElementById('decisions-body');
  const count = document.getElementById('decision-count');
  const items = state.decisions || [];
  if (!items.length) { show('decisions-block', false); return; }
  body.innerHTML = items.map((d) => `
    <div class="q-item">
      <div class="q-text">${escapeHtml(d.text)}${provLink(state.meeting_id, d.source_discussion_id, d.source_text)}</div>
      <div class="q-meta">${escapeHtml(d.status)}</div>
    </div>`).join('');
  if (count) count.textContent = `(${items.length})`;
  show('decisions-block', true);
}

// ---------- risks ----------

function renderRisks(state) {
  const body = document.getElementById('risks-body');
  const count = document.getElementById('risk-count');
  const items = state.risks || [];
  if (!items.length) { show('risks-block', false); return; }
  body.innerHTML = items.map((r) => `
    <div class="q-item">
      <div class="q-text">${escapeHtml(r.text)}${provLink(state.meeting_id, r.source_discussion_id, r.source_text)}</div>
      <div class="q-meta">${escapeHtml(r.kind)}${r.severity ? ' · ' + escapeHtml(r.severity) : ''}</div>
    </div>`).join('');
  if (count) count.textContent = `(${items.length})`;
  show('risks-block', true);
}

// ---------- facts ----------

function renderFacts(state, showIncidental) {
  const body = document.getElementById('facts-body');
  const count = document.getElementById('fact-count');
  const note = document.getElementById('incidental-note');
  const items = state.facts || [];
  const inc = (state.incidental_counts || {}).facts || 0;

  if (!items.length && !inc) { show('facts-block', false); return; }

  body.innerHTML = items.map((f) => {
    const relClass = f.meeting_relevance
      ? `relevance-${escapeHtml(f.meeting_relevance)}` : 'relevance-null';
    const rel = f.meeting_relevance || 'relevant';
    const conf = (f.confidence !== null && f.confidence !== undefined)
      ? `<span class="conf">${Math.round(f.confidence * 100)}%</span>` : '';
    const status = f.status ? `<span class="pill pill-${escapeHtml(f.status)}">${escapeHtml(f.status)}</span>` : '';
    const relPill = `<span class="relevance-pill ${relClass}">${escapeHtml(rel)}</span>`;
    return `
      <div class="q-item">
        <div class="q-text"><b>${escapeHtml(f.key)}</b> ${status} ${relPill} ${conf}</div>
        <div class="q-meta">${escapeHtml(f.value)}${provLink(state.meeting_id, f.source_discussion_id, f.source_text)}</div>
      </div>`;
  }).join('');

  if (count) count.textContent = `(${items.length})`;
  if (note) {
    note.textContent = (!showIncidental && inc) ? `+${inc} incidental hidden` : '';
  }
  show('facts-block', true);
}

// ---------- state meta ----------

function renderStateMeta(state) {
  const el = document.getElementById('state-meta-body');
  if (!el) return;
  const run = state.last_analysis_run_id ? `run #${state.last_analysis_run_id}` : 'no runs yet';
  const snap = state.last_snapshot_at ? `snapshot ${state.last_snapshot_at}` : '';
  el.textContent = [run, snap].filter(Boolean).join(' · ');
}

// ---------- orchestrator + state ----------

let _incidentalFlag = false;
let _currentMeeting = null;
let _currentState = null;

function renderRightPane(state, meeting) {
  _currentMeeting = meeting;
  _currentState = state;
  renderPulse(meeting, state);
  renderContextGaps(state);
  renderDelta(state.meeting_id, state.delta);
  renderActions(meeting, state);
  renderThreads(state);
  renderQuestions(state, 'now');
  renderQuestions(state, 'later');
  renderResponse(state);
  renderDecisions(state);
  renderRisks(state);
  renderFacts(state, _incidentalFlag);
  renderStateMeta(state);
}

async function refreshState() {
  const pane = document.getElementById('state-pane');
  if (!pane) return;
  const mid = pane.dataset.meetingId;
  if (!mid) return;
  try {
    const inc = _incidentalFlag ? '?include_incidental=true' : '';
    const r = await fetch(`/api/meetings/${encodeURIComponent(mid)}/state${inc}`,
                          { credentials: 'same-origin' });
    if (r.status === 401) { window.location.href = '/'; return; }
    if (!r.ok) return;
    const state = await r.json();
    let meeting = _currentMeeting;
    try {
      const mr = await fetch(`/api/meetings/${encodeURIComponent(mid)}`,
                             { credentials: 'same-origin' });
      if (mr.ok) meeting = await mr.json();
    } catch (_) {}
    renderRightPane(state, meeting);
  } catch (_) {}
}

window.refreshState = refreshState;
window.renderRightPane = renderRightPane;

// ---------- incidental toggle ----------

document.addEventListener('DOMContentLoaded', () => {
  const cb = document.getElementById('show-incidental');
  if (cb) {
    cb.addEventListener('change', () => {
      _incidentalFlag = cb.checked;
      if (_currentState) renderFacts(_currentState, _incidentalFlag);
      refreshState();
    });
  }
});

// ---------- websocket ----------

// ---------- websocket with state machine + reconnect ----------

let _ws = null;
let _wsState = {
  status: 'connecting',
  lastMessageAt: 0,
  lastHeartbeatAt: 0,
  attempts: 0,
  nextRetryAt: null,
};

const WS_BACKOFF_MS = [1000, 2000, 4000, 8000, 16000, 30000];

function setWsBadge(status) {
  const b = document.getElementById('ws-badge');
  if (!b) return;
  const labels = {
    connecting:   'WS: connecting',
    connected:    'WS: on',
    reconnecting: 'WS: reconnecting',
    disconnected: 'WS: off',
  };
  b.textContent = labels[status] || 'WS: off';
  b.classList.toggle('badge-on', status === 'connected');
  b.classList.toggle('badge-off', status === 'disconnected' || status === 'reconnecting');
}

async function refreshAll(meetingId) {
  const tiGen = ++_tiGeneration;
  const [m, t, d, s, tp] = await Promise.allSettled([
    fetch(`/api/meetings/${encodeURIComponent(meetingId)}`, {credentials: 'same-origin'}).then(r => r.ok ? r.json() : null),
    fetch(`/api/meetings/${encodeURIComponent(meetingId)}/transcript?limit=500`, {credentials: 'same-origin'}).then(r => r.ok ? r.json() : null),
    fetch(`/api/meetings/${encodeURIComponent(meetingId)}/discussions?limit=500`, {credentials: 'same-origin'}).then(r => r.ok ? r.json() : null),
    fetch(`/api/meetings/${encodeURIComponent(meetingId)}/state`, {credentials: 'same-origin'}).then(r => r.ok ? r.json() : null),
    fetch(`/api/meetings/${encodeURIComponent(meetingId)}/topics?limit=50`, {credentials: 'same-origin'}).then(r => r.ok ? r.json() : null),
  ]);
  if (typeof window.rebuildLeftPane === 'function') {
    window.rebuildLeftPane(m.value, t.value, d.value);
  }
  if (typeof window.renderRightPane === 'function' && s.value) {
    window.renderRightPane(s.value, m.value);
  }
  if (tiGen === _tiGeneration) {
    _renderTopicIntelligence(meetingId, tp.value, tiGen);
  }
}

function connectWs(meetingId) {
  if (_ws) { try { _ws.close(); } catch (_) {} _ws = null; }
  _wsState.status = _wsState.attempts === 0 ? 'connecting' : 'reconnecting';
  setWsBadge(_wsState.status);
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${location.host}/ws/meetings/${encodeURIComponent(meetingId)}`;
  let ws;
  try { ws = new WebSocket(url); } catch (_) { scheduleReconnect(meetingId); return; }
  _ws = ws;

  ws.addEventListener('open', async () => {
    _wsState.status = 'connected';
    _wsState.attempts = 0;
    _wsState.nextRetryAt = null;
    setWsBadge('connected');
    await refreshAll(meetingId);
  });

  ws.addEventListener('message', (ev) => {
    _wsState.lastMessageAt = Date.now();
    let msg; try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (!msg || !msg.type) return;
    if (msg.type === 'heartbeat') { _wsState.lastHeartbeatAt = Date.now(); return; }
    if (msg.type === 'hello') return;
    if (msg.type === 'segment') onSegmentMessage(msg);
    else if (msg.type === 'discussion') onDiscussionMessage(msg);
    else if (msg.type === 'state_update') onStateUpdateMessage(msg);
    else if (msg.type === 'finalization') onFinalizationMessage(msg);
    else if (msg.type === 'topic_intelligence') onTopicIntelligenceMessage(msg);
  });

  ws.addEventListener('close', () => { scheduleReconnect(meetingId); });
  ws.addEventListener('error', () => { /* close fires next */ });
}

function scheduleReconnect(meetingId) {
  if (_wsState.attempts >= WS_BACKOFF_MS.length) {
    _wsState.status = 'disconnected';
    setWsBadge('disconnected');
    return;
  }
  const delay = WS_BACKOFF_MS[Math.min(_wsState.attempts, WS_BACKOFF_MS.length - 1)];
  _wsState.attempts += 1;
  _wsState.status = 'reconnecting';
  _wsState.nextRetryAt = Date.now() + delay;
  setWsBadge('reconnecting');
  setTimeout(() => connectWs(meetingId), delay);
}
function onSegmentMessage(msg) {
  const el = document.getElementById('transcript');
  if (!el) return;
  if (el.querySelector(`[data-segment-id="${CSS.escape(msg.segment_id)}"]`)) return;
  if (typeof window.appendSegment === 'function') {
    window.appendSegment(msg);
    const c = document.getElementById('t-count');
    if (c) {
      const n = parseInt((c.textContent.match(/\d+/) || ['0'])[0], 10) + 1;
      c.textContent = `(${n})`;
    }
  }
}

function onDiscussionMessage(msg) {
  const el = document.getElementById('discussions');
  if (!el) return;
  const st = document.getElementById('d-status');
  if (st) st.remove();
  if (el.querySelector(`[data-discussion-id="${msg.discussion_id}"]`)) return;
  if (typeof window.appendDiscussion === 'function') {
    window.appendDiscussion({
      id: msg.discussion_id, meeting_id: msg.meeting_id,
      started_at: msg.started_at, ended_at: msg.ended_at,
      text: msg.text, segment_count: msg.segment_count,
    });
    const c = document.getElementById('d-count');
    if (c) {
      const n = parseInt((c.textContent.match(/\d+/) || ['0'])[0], 10) + 1;
      c.textContent = `(${n})`;
    }
  }
}

function onStateUpdateMessage(msg) {
  refreshState();
}

function onFinalizationMessage(msg) {
  const statusEl = document.getElementById('finalize-status');
  const finalizeBtn = document.getElementById('btn-finalize');
  const stopBtn = document.getElementById('btn-stop');

  if (stopBtn) {
    stopBtn.disabled = false;
    stopBtn.style.opacity = '';
    stopBtn.style.cursor = '';
  }
  if (statusEl) {
    statusEl.hidden = false;
    if (msg.status === 'ok') {
      statusEl.textContent = 'Finalization complete.';
      statusEl.classList.remove('error');
    } else {
      statusEl.textContent = 'Finalization failed: ' + (msg.error || 'unknown error');
      statusEl.classList.add('error');
    }
  }
  if (finalizeBtn) finalizeBtn.hidden = (msg.status === 'ok');
  refreshState();
}
// ---------- exposed WS state (read by livenessTick in meeting.html) ----------
window.getWsState = function () {
  return {
    status: _wsState.status,
    lastMessageAt: _wsState.lastMessageAt,
    lastHeartbeatAt: _wsState.lastHeartbeatAt,
    attempts: _wsState.attempts,
  };
};

// ---------- context review card (Phase G6) ----------

function crGapHtml(g) {
  const urgency = (g.urgency || "low").toLowerCase();
  const kind = (g.gap_kind || "").replace(/_/g, " ");
  const rationale = g.rationale
    ? `<div class="cr-rationale">${escapeHtml(g.rationale)}</div>`
    : "";
  const prov = provLink(g.meeting_id, g.source_discussion_id, g.source_text);
  return `
    <div class="cr-gap">
      <div class="cr-head">
        <span class="urgency-pill urgency-${escapeHtml(urgency)}">${escapeHtml(g.urgency)}</span>
        <span class="cr-kind">${escapeHtml(kind)}</span>
      </div>
      <div class="cr-text">${escapeHtml(g.text)}${prov}</div>
      ${rationale}
    </div>`;
}

function renderContextGaps(state) {
  const block = document.getElementById("context-review-block");
  const body = document.getElementById("context-review-body");
  const count = document.getElementById("context-review-count");
  const showAll = document.getElementById("context-review-show-all");
  if (!block || !body) return;

  const gaps = state.context_gaps || [];
  const total = state.context_gaps_total || 0;

  if (!gaps.length) { block.hidden = true; return; }

  body.innerHTML = gaps.map(crGapHtml).join("");
  if (count) count.textContent = `(${total} open)`;
  if (showAll) {
    showAll.hidden = total <= gaps.length;
    showAll.textContent = `Show all ${total}`;
  }
  block.hidden = false;
}

async function showAllContextGaps(meetingId) {
  const r = await fetch(`/api/meetings/${encodeURIComponent(meetingId)}/context_gaps?limit=200`,
                        { credentials: "same-origin" });
  if (!r.ok) return;
  const body = await r.json();
  const backdrop = document.createElement("div");
  backdrop.className = "cr-modal-backdrop";
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) backdrop.remove();
  });
  const rows = (body.items || []).map(crGapHtml).join("") || "<p class='muted'>None.</p>";
  backdrop.innerHTML = `
    <div class="cr-modal" role="dialog" aria-modal="true">
      <h3>Context Review — all gaps (${body.total || 0})</h3>
      ${rows}
      <button class="prov-close" id="cr-modal-close">Close</button>
    </div>`;
  document.body.appendChild(backdrop);
  document.getElementById("cr-modal-close").addEventListener("click", () => backdrop.remove());
}

// ---------- topic intelligence card (Stage A) ----------

var TI_DETAIL_CAP = 10;
var _tiGeneration = 0;
var _tiTimer = null;

function onTopicIntelligenceMessage(msg) {
  if (_tiTimer) clearTimeout(_tiTimer);
  _tiTimer = setTimeout(function () {
    _tiTimer = null;
    const pane = document.getElementById("state-pane");
    const mid = pane && pane.dataset.meetingId;
    if (mid) refreshTopicIntelligence(mid);
  }, 250);
}

async function refreshTopicIntelligence(meetingId) {
  const gen = ++_tiGeneration;
  const listRes = await fetch(
    `/api/meetings/${encodeURIComponent(meetingId)}/topics?limit=50`,
    { credentials: "same-origin" }
  );
  if (gen !== _tiGeneration) return;
  if (!listRes.ok) return;
  const list = await listRes.json();
  if (gen !== _tiGeneration) return;
  _renderTopicIntelligence(meetingId, list, gen);
}

var _TI_STATUS_WHITELIST = { pending: "pending", done: "done", failed: "failed" };
var _TI_FAILURE_LABELS = {
  provider_error: "provider error",
  schema_error: "schema error",
  missing_segment: "missing segment",
  unknown: "unknown",
};
var _TI_GROUNDING_LABELS = {
  model_only: "Model-only \u2014 unverified",
};

function _statusLabel(status) {
  return _TI_STATUS_WHITELIST[status] || "status unknown";
}

function _failureLabel(kind) {
  return _TI_FAILURE_LABELS[kind] || "unavailable";
}

function _groundingLabel(status) {
  return _TI_GROUNDING_LABELS[status] || "Unverified";
}

function _relevanceClass(relevance) {
  const r = String(relevance || "").toLowerCase();
  if (r === "high") return "ti-rel-high";
  if (r === "low") return "ti-rel-low";
  return "ti-rel-medium";
}

function _safeHref(rawUrl) {
  if (typeof rawUrl !== "string" || !rawUrl) return null;
  var u;
  try {
    u = new URL(rawUrl);
  } catch (_) {
    return null;
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return null;
  if (u.username || u.password) return null;
  return u.href;
}

function _topicHeadHtml(topic) {
  const name = escapeHtml(topic.name);
  const rel = escapeHtml(topic.relevance);
  const relClass = _relevanceClass(topic.relevance);
  const typeLabel = escapeHtml(String(topic.topic_type || "").replace(/_/g, " "));
  const statusLabel = _statusLabel(topic.research_status);
  const statusClass = _TI_STATUS_WHITELIST[topic.research_status]
    ? "ti-status-" + topic.research_status
    : "ti-status-unknown";
  return `
    <div class="ti-head">
      <span class="ti-name">${name}</span>
      <span class="ti-badge ${relClass}">${rel}</span>
      <span class="ti-badge ti-type">${typeLabel}</span>
      <span class="ti-badge ${statusClass}">${escapeHtml(statusLabel)}</span>
    </div>`;
}

function _sourceRowHtml(s) {
  const titleRaw = s.title || s.url || "(source)";
  const safeHref = _safeHref(s.url);
  const claimLabel = "Model-only claim \u2014 unverified";
  const label = escapeHtml(titleRaw);
  if (safeHref) {
    return `<li><a class="prov" href="${escapeHtml(safeHref)}" target="_blank" rel="noopener noreferrer">${label}</a> <span class="ti-source-claim">${escapeHtml(claimLabel)}</span></li>`;
  }
  const note = s.url ? " (unsupported link)" : "";
  return `<li>${label}${escapeHtml(note)} <span class="ti-source-claim">${escapeHtml(claimLabel)}</span></li>`;
}

function _topicCardHtml(topic, detail, generationMatches) {
  const head = _topicHeadHtml(topic);
  const status = topic.research_status;

  if (status === "pending") {
    return `<div class="ti-topic">${head}<div class="ti-body muted small">Research pending\u2026</div></div>`;
  }

  if (status === "failed") {
    const kind = detail && detail.latest_run ? detail.latest_run.failure_kind : null;
    return `<div class="ti-topic">${head}
      <div class="ti-body muted small">Research failed (${escapeHtml(_failureLabel(kind))}).</div>
    </div>`;
  }

  if (!detail) {
    return `<div class="ti-topic">${head}
      <div class="ti-body muted small">Brief not yet loaded.</div>
    </div>`;
  }

  const run = detail.latest_run || null;
  const brief = detail.brief || null;
  const sources = detail.sources || [];

  const groundingLabel = escapeHtml(_groundingLabel(run ? run.grounding_status : null));

  const provenance = `<div class="ti-provenance ti-model-only">${groundingLabel}</div>`;

  const summary = brief && brief.summary
    ? `<div class="ti-summary">${escapeHtml(brief.summary)}</div>`
    : `<div class="ti-summary muted small">No brief available.</div>`;

  const sourcesHtml = sources.length
    ? `<ul class="ti-sources">${sources.map(_sourceRowHtml).join("")}</ul>`
    : `<div class="ti-sources muted small">No sources claimed.</div>`;

  const mermaid = brief && brief.conceptual_diagram_mermaid
    ? `<div class="ti-diagram muted small">Conceptual diagram available (rendering in a future release).</div>`
    : "";

  return `<div class="ti-topic">${head}${provenance}${summary}${sourcesHtml}${mermaid}</div>`;
}

function _renderTopicIntelligence(meetingId, list, gen) {
  const block = document.getElementById("topic-intelligence-block");
  const body = document.getElementById("topic-intelligence-body");
  const count = document.getElementById("topic-intelligence-count");
  if (!block || !body) return;
  if (gen !== _tiGeneration) return;

  const items = (list && list.items) || [];
  if (!items.length) {
    block.hidden = true;
    body.innerHTML = "";
    if (count) count.textContent = "";
    return;
  }

  if (count) count.textContent = `(${items.length})`;
  block.hidden = false;

  body.innerHTML = items.map(function (t) { return _topicCardHtml(t, null, true); }).join("");

  const detailTargets = items
    .filter(function (t) { return t.research_status !== "pending"; })
    .slice(0, TI_DETAIL_CAP);

  if (!detailTargets.length) return;

  Promise.allSettled(
    detailTargets.map(function (t) {
      return fetch(
        `/api/meetings/${encodeURIComponent(meetingId)}/topics/${encodeURIComponent(t.id)}`,
        { credentials: "same-origin" }
      )
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (detail) { return { id: t.id, detail: detail }; });
    })
  ).then(function (results) {
    if (gen !== _tiGeneration) return;
    const byId = {};
    for (var i = 0; i < results.length; i++) {
      var r = results[i];
      if (r.status === "fulfilled" && r.value && r.value.detail) {
        byId[r.value.id] = r.value.detail;
      }
    }
    body.innerHTML = items
      .map(function (t) { return _topicCardHtml(t, byId[t.id] || null, true); })
      .join("");
  });
}


document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("context-review-show-all");
  if (btn) {
    btn.addEventListener("click", () => {
      const pane = document.getElementById("state-pane");
      const mid = pane && pane.dataset.meetingId;
      if (mid) showAllContextGaps(mid);
    });
  }
});
