/* Ad Designer — vanilla JS, no build step.
 *
 * One `state` object, a handful of render functions, re-render on change. No
 * framework, because every line here has to be readable and changeable live,
 * and a 700-line file you can read beats a build pipeline you have to explain.
 */

const API = '/api';

// Exclusions are ranked worst-first by the API, so this takes the N the
// advertiser should most actively avoid. Showing all fourteen turns a
// recommendation into a catalog dump.
const AVOID_LIMIT = 5;

const state = {
  sessions: [],
  sessionId: null,
  session: null,
  recommendation: null,
  samples: [],
  tab: 'publishers',
  progress: [],   // { phase, label, summary, done }
  eventSource: null,
  sending: false,
  streaming: false,   // a chat turn is mid-flight
  streamText: '',     // reply text received so far
};

// ---------------------------------------------------------------- utilities

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/** Escape first, then apply the tiny subset of markdown the agent may emit.
 *
 * Order matters and is the whole security argument: `esc()` neutralises any
 * markup in the model's output, and only afterwards do we introduce tags of our
 * own choosing. A model that emits `<script>` gets it rendered as text.
 *
 * Bold only. No links, no raw HTML, nothing that could navigate or execute.
 */
function formatReply(text) {
  return esc(text).replace(
    /\*\*([^*\n]+)\*\*/g,
    '<strong class="font-semibold text-gray-900">$1</strong>'
  );
}

const money = (n) => '$' + Number(n ?? 0).toLocaleString('en-US', { maximumFractionDigits: 2 });
const compact = (n) => Number(n ?? 0).toLocaleString('en-US', { notation: 'compact', maximumFractionDigits: 1 });

async function api(path, options = {}) {
  const response = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `${response.status} ${response.statusText}`);
  }
  return response.status === 204 ? null : response.json();
}

// ------------------------------------------------------------------ sidebar

async function loadSessions() {
  state.sessions = await api('/sessions');
  renderSidebar();
}

const STATUS_DOT = {
  collecting: 'bg-muted',
  generating: 'bg-warn animate-pulse',
  ready: 'bg-accent',
  failed: 'bg-bad',
};

function renderSidebar() {
  const list = $('session-list');
  if (!state.sessions.length) {
    list.innerHTML = '<div class="px-3 py-6 text-xs text-muted text-center">No campaigns yet.</div>';
    return;
  }
  list.innerHTML = state.sessions.map((s) => `
    <div class="group flex items-center gap-2.5 rounded-xl px-3 py-2.5 cursor-pointer transition
                ${s.id === state.sessionId ? 'bg-black/[0.07]' : 'hover:bg-black/[0.04]'}"
         data-open="${s.id}">
      <span class="w-1.5 h-1.5 rounded-full shrink-0 ${STATUS_DOT[s.status] || 'bg-muted'}"></span>
      <span class="flex-1 min-w-0 truncate text-[13px]">${esc(s.name)}</span>
      <button data-delete="${s.id}" title="Delete"
              class="opacity-0 group-hover:opacity-100 text-muted hover:text-bad text-xs px-1 transition">✕</button>
    </div>
  `).join('');
}

// ------------------------------------------------------------------ session

function resetView() {
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
  state.sessionId = null;
  state.session = null;
  state.recommendation = null;
  state.progress = [];
  state.streaming = false;
  state.streamText = '';
  closeDrawer();
  $('open-drawer').classList.add('hidden');
  setComposerEnabled(true);
  $('composer-note').classList.add('hidden');
  renderTranscript();
  renderSidebar();
}

async function openSession(id) {
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
  state.sessionId = id;
  state.recommendation = null;
  state.progress = [];
  state.streaming = false;
  state.streamText = '';
  state.session = await api(`/sessions/${id}`);

  $('samples').classList.add('hidden');
  renderSidebar();
  renderTranscript();

  if (state.session.status === 'ready') {
    await loadRecommendation();
    setComposerEnabled(false, 'This brief is complete. Start a new campaign to design another.');
  } else if (state.session.status === 'generating') {
    // Reattaching: the stream replays from the first event, so a refresh
    // mid-generation catches up rather than showing an empty progress list.
    setComposerEnabled(false, 'Designing your campaign…');
    startGeneration();
  } else if (state.session.status === 'failed') {
    setComposerEnabled(false, 'Generation failed.');
    renderTranscript();
  } else {
    setComposerEnabled(true);
  }
}

// --------------------------------------------------------------- transcript

function renderTranscript() {
  const el = $('transcript');

  if (!state.session) {
    el.innerHTML = `
      <div class="max-w-2xl mx-auto mt-24 text-center rise">
        <div class="font-serif text-5xl leading-[1.12]">Tell us about your business.</div>
        <div class="font-serif text-5xl leading-[1.12] text-accent">We'll design the ads.</div>
        <button id="hero-samples"
          class="lift mt-8 text-xs text-accent rounded-2xl border border-accent/30 px-5 py-2.5
                 hover:bg-accent/[0.06] hover:border-accent/55">
          or start from a sample brief
        </button>
      </div>`;
    const hero = $('hero-samples');
    if (hero) hero.onclick = toggleSamples;
    return;
  }

  const messages = (state.session.chat_history || []).map(renderMessage).join('');

  el.innerHTML = `<div class="max-w-3xl mx-auto space-y-5">
      ${messages}
      ${renderStreaming()}
      ${renderProgress()}
      ${state.session.status === 'failed' ? renderFailure() : ''}
    </div>`;

  const retry = $('retry-btn');
  if (retry) retry.onclick = retryGeneration;

  el.scrollTop = el.scrollHeight;
}

function renderMessage(m) {
  // The advertiser keeps a bubble — it is their input, and the tint marks it as
  // theirs. The agent's reply is centred, unboxed plain text: it is the page
  // speaking, not a second participant in a chat.
  if (m.role === 'user') {
    return `<div class="flex justify-end rise">
        <div class="max-w-[74%] rounded-3xl rounded-br-lg bg-accent/[0.11] border border-accent/20
                    px-5 py-3 text-sm leading-relaxed whitespace-pre-wrap">${esc(m.content)}</div>
      </div>`;
  }
  return `<div class="flex justify-center rise">
      <div class="max-w-[82%] w-fit text-left text-[15px] leading-relaxed text-gray-700
                  whitespace-pre-wrap">${formatReply(m.content)}</div>
    </div>`;
}

function renderStreaming() {
  if (!state.streaming) return '';
  if (!state.streamText) {
    // Nothing has arrived yet. Three breathing pips rather than an empty box.
    return `<div class="flex justify-center py-1">
        <div class="flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full bg-accent/60 pip" style="animation-delay:0ms"></span>
          <span class="w-2 h-2 rounded-full bg-accent/60 pip" style="animation-delay:180ms"></span>
          <span class="w-2 h-2 rounded-full bg-accent/60 pip" style="animation-delay:360ms"></span>
        </div>
      </div>`;
  }
  return `<div class="flex justify-center">
      <div class="max-w-[82%] w-fit text-left text-[15px] leading-relaxed text-gray-700
                  whitespace-pre-wrap caret">${formatReply(state.streamText)}</div>
    </div>`;
}

function renderProgress() {
  if (!state.progress.length) return '';

  const rows = state.progress.map((p) => `
    <div class="flex items-start gap-3.5 rounded-2xl border px-4 py-3 transition
                ${p.done ? 'border-edge bg-ink' : 'working border-accent/30'}">
      <span class="relative shrink-0 mt-0.5 w-5 h-5 grid place-items-center">
        ${p.done
          ? `<svg viewBox="0 0 20 20" class="tick w-5 h-5 text-accent" fill="none" stroke="currentColor"
                  stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
               <circle cx="10" cy="10" r="8.6" class="opacity-25"/><path d="M6 10.4l2.7 2.7L14.2 7.6"/>
             </svg>`
          : `<span class="absolute inset-0 rounded-full bg-accent/35 pip-ring"></span>
             <span class="relative w-3 h-3 rounded-full bg-accent pip"></span>`}
      </span>
      <div class="min-w-0 flex-1">
        <div class="text-sm ${p.done ? 'text-gray-700' : 'text-gray-900 font-medium'}">${esc(p.label)}</div>
        ${p.summary ? `<div class="text-xs text-muted mt-1 leading-relaxed font-light">${esc(p.summary)}</div>` : ''}
      </div>
    </div>`).join('');

  return `<div class="mt-8 rise">
      <div class="text-[11px] uppercase tracking-[0.12em] text-muted mb-3 text-center font-medium">Designing your campaign</div>
      <div class="space-y-2">${rows}</div>
    </div>`;
}

function renderFailure() {
  return `<div class="mt-6 rounded-2xl border border-bad/40 bg-bad/[0.07] px-5 py-4 rise">
      <div class="text-sm font-medium text-bad">Generation failed</div>
      <div class="text-xs text-muted mt-1 break-words">${esc(state.session.error || 'Unknown error')}</div>
      <button id="retry-btn" class="lift mt-3 text-xs rounded-xl border border-edge bg-ink px-4 py-2 hover:bg-black/[0.03]">Try again</button>
    </div>`;
}

// ----------------------------------------------------------------- composer

function setComposerEnabled(enabled, note) {
  $('composer').disabled = !enabled;
  $('send').disabled = !enabled;
  $('toggle-samples').disabled = !enabled;
  $('composer').placeholder = enabled
    ? 'We sell premium dog food for senior dogs…'
    : 'Brief closed.';
  const noteEl = $('composer-note');
  if (note) { noteEl.textContent = note; noteEl.classList.remove('hidden'); }
  else { noteEl.classList.add('hidden'); }
}

async function send() {
  const composer = $('composer');
  const text = composer.value.trim();
  if (!text || state.sending) return;

  composer.value = '';
  composer.style.height = 'auto';
  state.sending = true;

  try {
    if (!state.sessionId) {
      // Optimistic: show the message before the session exists, so clicking a
      // sample does not stare back blankly for two seconds.
      state.session = { name: '…', status: 'collecting', chat_history: [{ role: 'user', content: text }], brief: {} };
      renderTranscript();

      const session = await api('/sessions', {
        method: 'POST',
        body: JSON.stringify({ initial_message: text }),
      });
      state.sessionId = session.id;
      // The server names the session but does not record the message; the
      // streamed turn below does that, so keep the optimistic copy.
      state.session = { ...session, chat_history: [{ role: 'user', content: text }] };
      loadSessions();
    } else {
      state.session.chat_history.push({ role: 'user', content: text });
    }

    state.streaming = true;
    state.streamText = '';
    renderTranscript();

    const result = await streamChatTurn(state.sessionId, text, (chunk) => {
      state.streamText += chunk;
      renderTranscript();
    });

    // Drop the live buffer and commit the reply as a real message, so there is
    // exactly one copy of it on screen at the handover.
    state.streaming = false;
    state.streamText = '';
    state.session.chat_history.push({ role: 'assistant', content: result.reply });
    state.session.brief = result.brief;

    state.sending = false;
    renderTranscript();

    if (state.session.brief && state.session.brief.is_complete) {
      setComposerEnabled(false, 'Brief complete — the conversation is closed for this campaign.');
      startGeneration();
    }
    await loadSessions();
  } catch (error) {
    state.sending = false;
    state.streaming = false;
    state.streamText = '';
    state.session = state.session || { chat_history: [] };
    state.session.chat_history.push({ role: 'assistant', content: `Something went wrong: ${error.message}` });
    renderTranscript();
    await loadSessions();
  }
}

/** POST a turn and read the SSE body as it arrives.
 *
 * `EventSource` only does GET, and the message belongs in a request body, so
 * this reads the stream manually. Same SSE framing the generate endpoint uses.
 */
async function streamChatTurn(sessionId, message, onDelta) {
  const response = await fetch(`${API}/sessions/${sessionId}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `${response.status} ${response.statusText}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let done = null;

  for (;;) {
    const { value, done: finished } = await reader.read();
    if (finished) break;
    buffer += decoder.decode(value, { stream: true });

    let split;
    while ((split = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);

      let event = 'message';
      let data = '';
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      }
      if (!data) continue;

      if (event === 'delta') onDelta(JSON.parse(data).text);
      else if (event === 'done') done = JSON.parse(data);
      else if (event === 'error') throw new Error(JSON.parse(data).message);
    }
  }

  if (!done) throw new Error('Stream ended before the reply was complete.');
  return done;
}

// ---------------------------------------------------------------- streaming

function startGeneration() {
  if (state.eventSource) state.eventSource.close();
  state.progress = [];
  renderTranscript();

  const source = new EventSource(`${API}/sessions/${state.sessionId}/generate`);
  state.eventSource = source;

  source.addEventListener('phase_start', (event) => {
    const data = JSON.parse(event.data);
    if (!state.progress.some((p) => p.phase === data.phase)) {
      state.progress.push({ phase: data.phase, label: data.label, summary: '', done: false });
    }
    renderTranscript();
  });

  source.addEventListener('phase_complete', (event) => {
    const data = JSON.parse(event.data);
    const entry = state.progress.find((p) => p.phase === data.phase);
    if (entry) { entry.summary = data.summary; entry.done = true; }
    renderTranscript();
  });

  // Heartbeats are not rendered. They exist so nginx and the browser keep the
  // connection open through a 15-second publisher-scoring call.
  source.addEventListener('heartbeat', () => {});

  source.addEventListener('ready', async () => {
    source.close();
    state.eventSource = null;
    state.session = await api(`/sessions/${state.sessionId}`);
    await loadRecommendation();
    await loadSessions();
    openDrawer();
  });

  source.addEventListener('error', async (event) => {
    source.close();
    state.eventSource = null;
    if (event.data) {
      try { state.session.error = JSON.parse(event.data).message; } catch { /* keep server error */ }
    }
    // A transport-level error still needs the authoritative session status:
    // the work runs server-side and may well have succeeded.
    try { state.session = await api(`/sessions/${state.sessionId}`); } catch { /* offline */ }
    if (state.session.status === 'ready') {
      await loadRecommendation();
      openDrawer();
    } else {
      state.session.status = 'failed';
      renderTranscript();
    }
    await loadSessions();
  });
}

async function retryGeneration() {
  await api(`/sessions/${state.sessionId}/retry`, { method: 'POST' });
  state.session.status = 'collecting';
  state.session.error = null;
  startGeneration();
}

async function loadRecommendation() {
  state.recommendation = await api(`/sessions/${state.sessionId}/recommendation`);
  $('open-drawer').classList.remove('hidden');
  // generation_meta stays in the API response but is not shown: model name,
  // call count and duration are build trivia, not part of the media plan.
  $('drawer-title').textContent = state.recommendation.campaign_name;
  renderDrawer();
}

// ------------------------------------------------------------------- drawer

function openDrawer() {
  if (!state.recommendation) return;
  $('drawer').classList.remove('closed');
  const scrim = $('scrim');
  scrim.classList.remove('hidden');
  requestAnimationFrame(() => { scrim.style.opacity = '1'; });
}

function closeDrawer() {
  $('drawer').classList.add('closed');
  const scrim = $('scrim');
  scrim.style.opacity = '0';
  setTimeout(() => scrim.classList.add('hidden'), 300);
}

const FIT_STYLE = {
  strong: ['bg-accent/15 border-accent/40 text-accent', 'Strong catalog fit'],
  partial: ['bg-warn/15 border-warn/40 text-warn', 'Partial catalog fit'],
  poor: ['bg-bad/15 border-bad/40 text-bad', 'Poor catalog fit'],
};

function renderDrawer() {
  const rec = state.recommendation;
  if (!rec) return;

  const [fitClass, fitLabel] = FIT_STYLE[rec.catalog_fit.verdict] || FIT_STYLE.partial;
  const banner = `
    <div class="rounded-2xl border px-5 py-4 mb-6 ${fitClass}">
      <div class="text-xs font-semibold uppercase tracking-wider">${fitLabel}</div>
      <div class="text-sm mt-1 text-gray-800 leading-relaxed">${esc(rec.catalog_fit.explanation)}</div>
    </div>`;

  const views = {
    publishers: renderPublishers,
    audiences: renderAudiences,
    creatives: renderCreatives,
    config: renderConfig,
  };

  $('drawer-body').innerHTML = banner + views[state.tab]() + renderAssumptions();

  document.querySelectorAll('[data-toggle]').forEach((el) => {
    el.onclick = () => $(el.dataset.toggle).classList.toggle('hidden');
  });
  const copy = $('copy-json');
  if (copy) copy.onclick = copyConfigJson;
}

function scoreBar(score) {
  // Centred at zero: the bar grows right for positive and left for negative, so
  // an "actively wrong" attribute is visually different from a neutral one
  // rather than just shorter.
  const magnitude = Math.min(Math.abs(score), 1) * 50;
  const positive = score >= 0;
  return `
    <div class="relative h-1.5 w-28 rounded-full bg-black/[0.07] shrink-0">
      <div class="absolute top-0 bottom-0 left-1/2 w-px bg-black/25"></div>
      <div class="absolute top-0 bottom-0 rounded-full ${positive ? 'bg-accent' : 'bg-bad'}"
           style="left:${positive ? 50 : 50 - magnitude}%; width:${magnitude}%"></div>
    </div>`;
}

function attributeRows(scores) {
  return scores.map((a) => `
    <div class="flex items-start gap-3 py-1.5">
      <div class="w-36 shrink-0 text-[11px] text-muted font-mono pt-0.5">${esc(a.attribute)}</div>
      ${scoreBar(a.score)}
      <div class="w-11 shrink-0 text-[11px] font-mono ${a.score >= 0 ? 'text-accent' : 'text-bad'} text-right pt-0.5">
        ${a.score >= 0 ? '+' : ''}${a.score.toFixed(2)}
      </div>
      <div class="flex-1 text-xs text-gray-600 leading-relaxed">${esc(a.reason)}</div>
    </div>`).join('');
}

function renderPublishers() {
  const rec = state.recommendation;
  const spendById = {};
  rec.campaign_config.allocation.forEach((a) => { spendById[a.publisher_id] = a; });

  const card = (s, index) => {
    const spend = spendById[s.publisher_id];
    const bodyId = `pub-${s.publisher_id}`;
    return `
      <div class="rounded-2xl border border-edge bg-white mb-2.5 overflow-hidden lift">
        <div class="px-4 py-3 cursor-pointer hover:bg-black/[0.03]" data-toggle="${bodyId}">
          <div class="flex items-center gap-3">
            <div class="w-6 text-xs text-muted font-mono">${index + 1}</div>
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2">
                <span class="font-medium text-sm">${esc(s.publisher.name)}</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded bg-black/[0.04] text-muted font-mono">${esc(s.publisher.category)}</span>
                ${s.override_reason ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-warn/20 text-warn">overridden</span>' : ''}
              </div>
              <div class="text-xs text-gray-600 mt-1 leading-relaxed">${esc(s.reason)}</div>
            </div>
            <div class="text-right shrink-0">
              <div class="text-sm font-mono ${s.composite_score >= 0 ? 'text-accent' : 'text-bad'}">${s.composite_score.toFixed(2)}</div>
              ${spend ? `<div class="text-[11px] text-muted mt-0.5">${money(spend.daily_budget_usd)}/day</div>` : ''}
            </div>
          </div>
        </div>
        <div id="${bodyId}" class="hidden border-t border-edge px-4 py-3 bg-black/[0.02]">
          ${attributeRows(s.attribute_scores)}
          ${s.override_reason ? `<div class="mt-3 text-xs text-warn/90 border-l-2 border-warn/50 pl-3 leading-relaxed">
              Threshold override: ${esc(s.override_reason)}</div>` : ''}
          <div class="mt-3 grid grid-cols-4 gap-3 text-[11px] text-muted border-t border-edge pt-3">
            <div><div class="text-gray-700 font-mono">${money(s.publisher.cpm_usd)}</div>CPM</div>
            <div><div class="text-gray-700 font-mono">${compact(s.publisher.monthly_impressions)}</div>monthly impressions</div>
            <div><div class="text-gray-700 font-mono">${money(s.publisher.avg_order_value_usd)}</div>publisher AOV</div>
            <div><div class="text-gray-700 font-mono">${esc(s.publisher.audience.age_skew)}</div>${esc(s.publisher.audience.income_tier)} income</div>
          </div>
          <div class="mt-2 text-[11px] text-muted italic">${esc(s.publisher.notes)}</div>
        </div>
      </div>`;
  };

  return `
    <div class="text-[11px] uppercase tracking-[0.12em] text-muted mb-2">Where to run</div>
    ${rec.publisher_plan.recommended.map(card).join('')}

    <div class="mt-7 flex items-center justify-between">
      <div class="text-[11px] uppercase tracking-[0.12em] text-muted">Where not to run</div>
      <button data-toggle="avoid-list" class="text-[11px] text-accent hover:underline">show / hide</button>
    </div>
    <div class="text-[11px] text-muted mt-1 mb-2">The placements worth actively staying away from.</div>
    <div id="avoid-list" class="hidden mt-2 opacity-90">
      ${rec.publisher_plan.excluded.slice(0, AVOID_LIMIT).map(card).join('')}
    </div>`;
}

function renderAudiences() {
  const rec = state.recommendation;

  const card = (s, selected) => {
    const bodyId = `persona-${s.persona_id}`;
    return `
      <div class="rounded-2xl border ${selected ? 'border-edge' : 'border-edge/60'} bg-white mb-2.5 overflow-hidden lift">
        <div class="px-4 py-3 cursor-pointer hover:bg-black/[0.03]" data-toggle="${bodyId}">
          <div class="flex items-start gap-3">
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="font-medium text-sm">${esc(s.persona.name)}</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded bg-black/[0.04] text-muted font-mono">${esc(s.persona.age_range)} · ${esc(s.persona.gender_skew)}</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded bg-black/[0.04] text-muted font-mono">${money(s.persona.typical_aov_usd)} AOV</span>
                ${s.override_reason ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-warn/20 text-warn">overridden</span>' : ''}
              </div>
              <div class="text-xs text-gray-600 mt-1 leading-relaxed">${esc(s.reason)}</div>
            </div>
            <div class="text-sm font-mono shrink-0 ${s.composite_score >= 0 ? 'text-accent' : 'text-bad'}">${s.composite_score.toFixed(2)}</div>
          </div>
        </div>
        <div id="${bodyId}" class="hidden border-t border-edge px-4 py-3 bg-black/[0.02]">
          <div class="text-xs text-gray-600 mb-3 leading-relaxed">${esc(s.persona.description)}</div>
          ${attributeRows(s.attribute_scores)}
          ${s.override_reason ? `<div class="mt-3 text-xs text-warn/90 border-l-2 border-warn/50 pl-3 leading-relaxed">
              Selection override: ${esc(s.override_reason)}</div>` : ''}
          <div class="mt-3 border-t border-edge pt-3 text-[11px] space-y-1">
            <div><span class="text-muted">responds to:</span> ${esc(s.persona.messaging_preferences.join(', '))}</div>
            <div><span class="text-muted">turned off by:</span> ${esc(s.persona.disinterested_in.join(', '))}</div>
          </div>
        </div>
      </div>`;
  };

  return `
    <div class="text-[11px] uppercase tracking-[0.12em] text-muted mb-2">Who to speak to</div>
    ${rec.audience_plan.selected.map((s) => card(s, true)).join('')}
    <div class="mt-7 flex items-center justify-between">
      <div class="text-[11px] uppercase tracking-[0.12em] text-muted">Who not to chase</div>
      <button data-toggle="not-selected-list" class="text-[11px] text-accent hover:underline">show / hide</button>
    </div>
    <div id="not-selected-list" class="hidden mt-2 opacity-90">
      ${rec.audience_plan.not_selected.slice(0, AVOID_LIMIT).map((s) => card(s, false)).join('')}
    </div>`;
}

const ANGLE_LABEL = {
  benefit_led: 'Benefit',
  social_proof: 'Social proof',
  offer_led: 'Offer',
};

function renderCreatives() {
  const rec = state.recommendation;
  if (!rec.ad_sets.length) return '<div class="text-sm text-muted">No ad sets were generated.</div>';

  return rec.ad_sets.map((set) => `
    <div class="mb-6">
      <div class="flex items-baseline gap-2 mb-2">
        <div class="text-sm font-medium">${esc(set.persona_name)}</div>
        <div class="text-[11px] text-muted font-mono">${esc(set.persona_id)} · ${set.creatives.length} variants</div>
      </div>
      <div class="grid grid-cols-1 gap-2">
        ${set.creatives.map((c) => `
          <div class="rounded-2xl border border-edge bg-white px-5 py-4 lift">
            <div class="flex items-center justify-between gap-3 mb-2">
              <span class="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded bg-accent/15 text-accent">${ANGLE_LABEL[c.angle] || esc(c.angle)}</span>
              <span class="text-[10px] text-muted font-mono">${c.headline.length}/60 · ${c.body.length}/160</span>
            </div>
            <div class="text-sm font-semibold leading-snug">${esc(c.headline)}</div>
            <div class="text-sm text-gray-700 mt-1 leading-relaxed">${esc(c.body)}</div>
            <div class="text-[11px] text-muted mt-2.5 border-t border-edge pt-2 leading-relaxed">${esc(c.persona_fit_note)}</div>
          </div>`).join('')}
      </div>
    </div>`).join('');
}

function renderConfig() {
  const config = state.recommendation.campaign_config;
  const bid = config.bid_strategy;
  const bidValue = bid.target_cpa_usd ?? bid.max_cpc_usd ?? bid.cpm_bid_usd;

  const kv = (label, value) => `
    <div class="flex justify-between gap-4 py-1.5 border-b border-edge/50 text-xs">
      <span class="text-muted">${esc(label)}</span>
      <span class="font-mono text-right">${esc(value)}</span>
    </div>`;

  const overBreakEven = config.allocation.filter(
    (a) => a.est_cpa_usd !== null && a.est_cpa_usd > config.measurement.break_even_cpa_usd
  ).length;

  return `
    <div class="grid grid-cols-2 gap-x-6 mb-6">
      <div>
        <div class="text-[11px] uppercase tracking-wider text-muted mb-2">Objective &amp; bidding</div>
        ${kv('objective', config.objective)}
        ${kv('pricing model', config.pricing_model)}
        ${kv('bid strategy', bid.type)}
        ${kv('bid value', money(bidValue))}
        ${kv('break-even CPA', money(config.measurement.break_even_cpa_usd))}
        ${kv('primary KPI', config.measurement.primary_kpi)}
        ${kv('attribution window', config.measurement.attribution_window_days + ' days')}
      </div>
      <div>
        <div class="text-[11px] uppercase tracking-wider text-muted mb-2">Budget &amp; flight</div>
        ${kv('daily budget', money(config.budget.daily_usd))}
        ${kv('suggested total', money(config.budget.suggested_total_usd))}
        ${kv('flight', `${config.flight.start_date} → ${config.flight.end_date}`)}
        ${kv('pacing', config.budget.pacing)}
        ${kv('frequency cap', `${config.frequency_cap.impressions}/${config.frequency_cap.per}`)}
        ${kv('geos', config.targeting.geos.join(', '))}
        ${kv('audience', `${config.targeting.age_range}, ${config.targeting.gender_skew}`)}
      </div>
    </div>

    <div class="rounded-xl border border-edge bg-white px-4 py-3 mb-6">
      <div class="text-[11px] uppercase tracking-wider text-muted mb-1.5">Bid rationale</div>
      <div class="text-xs text-gray-700 leading-relaxed">${esc(bid.rationale)}</div>
    </div>

    <div class="text-[11px] uppercase tracking-wider text-muted mb-2">Budget allocation</div>
    <div class="rounded-2xl border border-edge overflow-hidden mb-2">
      <table class="w-full text-xs">
        <thead class="bg-black/[0.04] text-muted">
          <tr>
            <th class="text-left font-normal px-3 py-2">Publisher</th>
            <th class="text-right font-normal px-3 py-2">Daily</th>
            <th class="text-right font-normal px-3 py-2">Share</th>
            <th class="text-right font-normal px-3 py-2">CPM</th>
            <th class="text-right font-normal px-3 py-2">Impr.</th>
            <th class="text-right font-normal px-3 py-2">Conv.</th>
            <th class="text-right font-normal px-3 py-2">Est. CPA</th>
          </tr>
        </thead>
        <tbody>
          ${config.allocation.map((a) => `
            <tr class="border-t border-edge">
              <td class="px-3 py-2">
                <div>${esc(a.publisher_name)}</div>
                <div class="text-[11px] text-muted mt-0.5 leading-relaxed">${esc(a.rationale)}</div>
              </td>
              <td class="px-3 py-2 text-right font-mono">${money(a.daily_budget_usd)}</td>
              <td class="px-3 py-2 text-right font-mono">${a.share_pct.toFixed(1)}%</td>
              <td class="px-3 py-2 text-right font-mono">${money(a.cpm_usd)}</td>
              <td class="px-3 py-2 text-right font-mono">${compact(a.est_impressions)}</td>
              <td class="px-3 py-2 text-right font-mono">${a.est_conversions.toFixed(1)}</td>
              <td class="px-3 py-2 text-right font-mono ${a.est_cpa_usd !== null && a.est_cpa_usd > config.measurement.break_even_cpa_usd ? 'text-bad' : 'text-accent'}">
                ${a.est_cpa_usd === null ? '—' : money(a.est_cpa_usd)}
              </td>
            </tr>`).join('')}
          <tr class="border-t border-edge bg-black/[0.03] font-medium">
            <td class="px-3 py-2">Total</td>
            <td class="px-3 py-2 text-right font-mono">${money(config.allocation.reduce((sum, a) => sum + a.daily_budget_usd, 0))}</td>
            <td class="px-3 py-2 text-right font-mono">${config.allocation.reduce((sum, a) => sum + a.share_pct, 0).toFixed(1)}%</td>
            <td colspan="4"></td>
          </tr>
        </tbody>
      </table>
    </div>
    ${overBreakEven ? `<div class="text-[11px] text-bad mb-6">
        ${overBreakEven} line${overBreakEven > 1 ? 's' : ''} project a CPA above break-even at the assumed CTR and CVR.
      </div>` : '<div class="mb-6"></div>'}

    <div class="rounded-xl border border-edge bg-white px-4 py-3 mb-6">
      <div class="text-[11px] uppercase tracking-wider text-muted mb-1.5">Brand safety</div>
      <div class="text-xs text-gray-700 leading-relaxed">${esc(config.brand_safety.notes)}</div>
      <div class="text-[11px] text-muted mt-2 font-mono break-words">
        excluded: ${esc(config.brand_safety.excluded_publisher_ids.join(', ') || 'none')}
      </div>
    </div>

    <div class="flex items-center justify-between mb-2">
      <div class="text-[11px] uppercase tracking-wider text-muted">Raw config</div>
      <button id="copy-json" class="text-[11px] text-accent hover:underline">copy JSON</button>
    </div>
    <pre class="rounded-2xl border border-edge bg-gray-100 p-4 text-[11px] overflow-x-auto max-h-80 leading-relaxed">${esc(JSON.stringify(config, null, 2))}</pre>`;
}

function renderAssumptions() {
  const a = state.recommendation.generation_meta.assumptions;
  return `
    <div class="mt-8 pt-4 border-t border-edge text-[11px] text-muted leading-relaxed">
      <div class="uppercase tracking-wider mb-1.5">Assumptions</div>
      <div>${esc(a.rate_card_note)}</div>
      <div class="mt-1">
        Click-through rate assumed at ${(a.assumed_ctr * 100).toFixed(2)}%, conversion rate at
        ${(a.assumed_cvr * 100).toFixed(1)}%, gross margin at ${a.gross_margin_pct}%. The advertiser
        supplied none of these; a production system would read them from delivery history.
      </div>
    </div>`;
}

async function copyConfigJson() {
  await navigator.clipboard.writeText(JSON.stringify(state.recommendation.campaign_config, null, 2));
  const button = $('copy-json');
  button.textContent = 'copied';
  setTimeout(() => { button.textContent = 'copy JSON'; }, 1500);
}

// ------------------------------------------------------------------ samples

async function loadSamples() {
  state.samples = await api('/sample-briefs');
  $('samples').innerHTML = state.samples.map((s) => `
    <button data-sample="${s.number}"
      class="w-full text-left text-xs rounded-xl px-3.5 py-2.5 hover:bg-black/[0.04] transition leading-relaxed font-light">
      <span class="text-muted font-mono mr-2">${s.number}</span>${esc(s.text)}
    </button>`).join('');
}

function toggleSamples() {
  $('samples').classList.toggle('hidden');
}

// -------------------------------------------------------------------- wiring

document.addEventListener('click', async (event) => {
  const sample = event.target.closest('[data-sample]');
  if (sample) {
    const entry = state.samples.find((s) => String(s.number) === sample.dataset.sample);
    $('samples').classList.add('hidden');
    if (state.sessionId) resetView();
    $('composer').value = entry.text;
    await send();
    return;
  }

  const del = event.target.closest('[data-delete]');
  if (del) {
    event.stopPropagation();
    const id = del.dataset.delete;
    await api(`/sessions/${id}`, { method: 'DELETE' });
    if (id === state.sessionId) resetView();
    await loadSessions();
    return;
  }

  const open = event.target.closest('[data-open]');
  if (open) { await openSession(open.dataset.open); return; }

  const tab = event.target.closest('.tab');
  if (tab) {
    state.tab = tab.dataset.tab;
    document.querySelectorAll('.tab').forEach((t) => {
      const active = t.dataset.tab === state.tab;
      t.className = `tab px-3 py-2 rounded-t-md border-b-2 ${active ? 'border-accent text-gray-900' : 'border-transparent text-muted hover:text-gray-900'}`;
    });
    renderDrawer();
  }
});

$('send').onclick = send;
$('new-session').onclick = resetView;
$('toggle-samples').onclick = toggleSamples;
$('open-drawer').onclick = openDrawer;
$('close-drawer').onclick = closeDrawer;
$('scrim').onclick = closeDrawer;

$('composer').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); }
});
$('composer').addEventListener('input', (event) => {
  event.target.style.height = 'auto';
  event.target.style.height = Math.min(event.target.scrollHeight, 160) + 'px';
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeDrawer();
});

(async function init() {
  renderTranscript();
  await Promise.all([loadSessions(), loadSamples()]);
})();
