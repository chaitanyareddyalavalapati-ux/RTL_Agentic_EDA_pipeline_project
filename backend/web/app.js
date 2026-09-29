/* RTL Agentic EDA dashboard */

const STEPS = ['planning', 'rtl', 'testbench', 'simulation', 'synthesis', 'sta', 'physical_design', 'physical_verification'];
const LABELS = {
  planning: 'Planning', rtl: 'RTL generation', testbench: 'Cocotb generation', simulation: 'Simulation',
  synthesis: 'Synthesis', sta: 'OpenSTA', physical_design: 'Physical design', physical_verification: 'Physical verification'
};
const METRIC_LABELS = {
  tests: 'tests', passed: 'passed', failed: 'failed', cell_count: 'cells', area: 'area',
  wns_ns: 'WNS ns', tns_ns: 'TNS ns', floorplan: 'floorplan', placement: 'placement',
  cts: 'CTS', routing: 'routing', drc: 'DRC', lvs: 'LVS'
};
// pending < running < finished. Used to decide whether the snapshot or the event
// stream holds the fresher view of a step after a dropped socket.
const RANK = { pending: 0, running: 1, failed: 2, passed: 2 };
const MAX_RECONNECT = 8;

let events = [], snap = {}, job = null, art = { rtl: '', tb: '', simulation: '', netlistText: '', netlist: '', sta: '', physical: '', physicalImage: '', report: '' };
let ws = null, terminal = false, reconnectAttempts = 0, reconnectTimer = null, ticker = null;
let selected = null;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
const activeTab = () => document.querySelector('.tab.active').dataset.tab;
const setBadge = (cls, text) => { $('status').className = 'badge ' + cls; $('status').textContent = text; };
const setConn = (cls, text) => { $('conn').className = 'conn ' + cls; $('conn').textContent = text; };

// The engine emits tz-aware timestamps; StepResult writes naive UTC. Normalise both.
function parseTs(v) {
  if (!v) return null;
  const d = new Date(/(Z|[+-]\d\d:?\d\d)$/.test(v) ? v : v + 'Z');
  return isNaN(d.getTime()) ? null : d;
}

function fmtDur(ms) {
  if (ms == null || ms < 0) return '';
  if (ms < 1000) return Math.round(ms) + 'ms';
  if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
  return Math.floor(ms / 60000) + 'm ' + String(Math.round((ms % 60000) / 1000)).padStart(2, '0') + 's';
}

function fmtMetric(v) {
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(2);
  return String(v);
}

function blank(name) {
  return { name, status: 'pending', attempts: 0, started: null, ended: null, metrics: {}, output: '', error: null, result: null, repair: null };
}

function stepsFromSnapshot() {
  const m = {};
  STEPS.forEach(n => m[n] = blank(n));
  Object.entries(snap.steps || {}).forEach(([n, v]) => {
    if (!m[n]) return;
    Object.assign(m[n], {
      status: v.status || 'pending', attempts: v.attempts || 0,
      started: parseTs(v.started_at), ended: parseTs(v.ended_at),
      metrics: v.metrics || {}, output: v.output || '', error: v.error || null
    });
  });
  // The engine never records a StepResult for planning, so infer it from the plan.
  if (snap.plan && Object.keys(snap.plan).length) {
    Object.assign(m.planning, { status: 'passed', attempts: m.planning.attempts || 1, result: snap.plan });
  }
  (snap.repair_history || []).forEach(r => { if (m[r.stage]) m[r.stage].repair = r.diagnosis; });
  return m;
}

function stepsFromEvents() {
  const m = {};
  STEPS.forEach(n => m[n] = blank(n));
  events.forEach(e => {
    const s = m[e.step];
    if (!s) return;
    const at = parseTs(e.timestamp);
    if (e.type === 'step_started') {
      Object.assign(s, { status: 'running', attempts: e.attempt || s.attempts || 1, started: at, ended: null });
    } else if (e.type === 'step_completed') {
      Object.assign(s, { status: e.status === 'passed' ? 'passed' : 'failed', attempts: e.attempt || s.attempts || 1, ended: at });
      if (e.metrics) s.metrics = e.metrics;
      if (e.output) s.output = e.output;
      if (e.result) s.result = e.result;
    } else if (e.type === 'step_failed') {
      Object.assign(s, { status: 'failed', attempts: e.attempt || s.attempts || 1, ended: at });
      if (e.error) s.error = e.error;
      if (e.output) s.output = e.output;
    } else if (e.type === 'agent_repair') {
      s.repair = e.diagnosis;
      s.attempts = e.attempt || s.attempts;
    }
  });
  return m;
}

// Merge the two views, preferring whichever is further along. After a dropped
// socket the snapshot is ahead; during a live run the events are.
function buildSteps() {
  const fromSnap = stepsFromSnapshot(), fromEvents = stepsFromEvents();
  const m = {};
  STEPS.forEach(n => {
    const a = fromSnap[n], b = fromEvents[n];
    const base = RANK[b.status] >= RANK[a.status] ? b : a;
    const other = base === b ? a : b;
    m[n] = Object.assign({}, other, base);
    // Keep any richer payload either side happens to hold.
    m[n].metrics = Object.keys(base.metrics || {}).length ? base.metrics : (other.metrics || {});
    m[n].output = base.output || other.output || '';
    m[n].error = base.error || other.error || null;
    m[n].result = base.result || other.result || null;
    m[n].repair = base.repair || other.repair || null;
    m[n].attempts = Math.max(base.attempts || 0, other.attempts || 0);
    m[n].started = base.started || other.started || null;
    m[n].ended = base.ended || other.ended || null;
  });
  return m;
}

function stepDuration(s) {
  if (s.started && s.ended) return fmtDur(s.ended - s.started);
  if (s.status === 'running' && s.started) return fmtDur(Date.now() - s.started);
  return '';
}

function stateText(s) {
  if (s.status === 'running') return 'Running · attempt ' + (s.attempts || 1);
  if (s.status === 'passed') return 'Passed · ' + (s.attempts || 1) + ' attempt(s)';
  if (s.status === 'failed') return 'Failed · attempt ' + (s.attempts || 1);
  return 'Pending';
}

function chipsHtml(metrics, limit) {
  const entries = Object.entries(metrics || {});
  const shown = limit ? entries.slice(0, limit) : entries;
  if (!shown.length) return '';
  return `<div class="chips">${shown.map(([k, v]) =>
    `<span class="chip">${esc(METRIC_LABELS[k] || k)} <b>${esc(fmtMetric(v))}</b></span>`).join('')}</div>`;
}

function render() {
  const m = buildSteps();
  let done = 0, failed = 0, running = false;

  $('steps').innerHTML = STEPS.map(n => {
    const s = m[n];
    if (s.status === 'passed') done++;
    if (s.status === 'failed') failed++;
    if (s.status === 'running') running = true;
    const cls = s.status === 'passed' ? 'pass' : s.status === 'failed' ? 'fail' : s.status === 'running' ? 'run' : '';
    const dur = stepDuration(s);
    return `<div class="step ${cls}${selected === n ? ' sel' : ''}" data-step="${n}" tabindex="0" role="button" aria-pressed="${selected === n}">
      <div class="step-top"><b>${LABELS[n]}</b>${s.attempts > 1 ? `<span class="retry">&times;${s.attempts}</span>` : ''}</div>
      <div class="state">${esc(stateText(s))}${dur ? ` <span class="dur">${dur}</span>` : ''}</div>
      ${chipsHtml(s.metrics, 3)}
    </div>`;
  }).join('');

  $('progress').textContent = `${done} / ${STEPS.length}`;
  $('bar').style.width = Math.round(done / STEPS.length * 100) + '%';
  $('bar').className = 'bar-fill' + (failed ? ' fail' : running ? ' run' : '');
  $('elapsed').textContent = overallElapsed();

  const feed = events.slice(-60).reverse();
  $('eventCount').textContent = events.length ? events.length + ' events' : '';
  $('agent').innerHTML = feed.map(e => {
    const sub = e.diagnosis?.diagnosis || e.error || e.step || e.status || '';
    const tone = (e.type === 'step_failed' || e.type === 'pipeline_failed') ? 'bad' : e.type === 'agent_repair' ? 'warn' : '';
    const at = parseTs(e.timestamp);
    return `<div class="event ${tone}">
      <b>${esc(String(e.type || 'event').replaceAll('_', ' '))}</b>
      <small>${esc(sub)}${at ? ` · ${at.toLocaleTimeString()}` : ''}</small>
    </div>`;
  }).join('') || '<span class="mini">No activity yet.</span>';

  const filter = $('logFilter').value.trim().toLowerCase();
  let text = events.filter(e => e.output || e.error || e.traceback)
    .map(e => `[${e.step || 'pipeline'}] ${e.output || e.error || ''}${e.traceback ? '\n' + e.traceback : ''}`)
    .join('\n\n');
  if (filter) text = text.split('\n').filter(l => l.toLowerCase().includes(filter)).join('\n');
  $('logs').textContent = text || 'No tool output yet.';
  if ($('autoscroll').checked) $('logs').scrollTop = $('logs').scrollHeight;

  renderDetail(m);
  bindSteps();
  syncTicker(running);
}

function overallElapsed() {
  const start = parseTs(events.find(e => e.type === 'pipeline_started')?.timestamp);
  if (!start) return '';
  const endEvent = events.find(e => e.type === 'pipeline_completed' || e.type === 'pipeline_failed');
  const end = parseTs(endEvent?.timestamp) || (terminal ? null : new Date());
  return end ? fmtDur(end - start) : '';
}

function renderDetail(m) {
  const d = $('detail');
  if (!selected || !m[selected]) { d.hidden = true; d.innerHTML = ''; return; }
  const s = m[selected];
  const rows = [
    ['Status', s.status],
    ['Attempts', s.attempts || 0],
    ['Started', s.started ? s.started.toLocaleTimeString() : '—'],
    ['Duration', stepDuration(s) || '—']
  ];
  d.hidden = false;
  d.innerHTML = `
    <div class="detail-head"><b>${LABELS[selected]}</b><button class="ghost" id="closeDetail">Close</button></div>
    <div class="kv">${rows.map(([k, v]) => `<div><span>${k}</span><b>${esc(v)}</b></div>`).join('')}</div>
    ${chipsHtml(s.metrics)}
    ${s.result ? `<h3>Plan</h3><pre class="sub">${esc(JSON.stringify(s.result, null, 2))}</pre>` : ''}
    ${s.repair ? `<h3>Agent diagnosis</h3><pre class="sub">${esc(JSON.stringify(s.repair, null, 2))}</pre>` : ''}
    ${s.error ? `<h3>Error</h3><pre class="sub bad">${esc(s.error)}</pre>` : ''}
    ${s.output ? `<h3>Output</h3><pre class="sub">${esc(s.output)}</pre>` : ''}`;
  $('closeDetail').onclick = () => { selected = null; render(); };
}

function bindSteps() {
  document.querySelectorAll('.step').forEach(el => {
    const pick = () => { selected = selected === el.dataset.step ? null : el.dataset.step; render(); };
    el.onclick = pick;
    el.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); pick(); } };
  });
}

// Re-render once a second only while something is actually running, to keep the
// live duration counters moving.
function syncTicker(running) {
  if (running && !ticker) ticker = setInterval(render, 1000);
  if (!running && ticker) { clearInterval(ticker); ticker = null; }
}

async function fetchJson(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

function show(k) {
  const image = $('artifactImage');
  const text = $('artifact');
  const stats = $('artifactStats');
  renderArtifactSummary();
  const labels = { rtl: 'RTL source · SystemVerilog', tb: 'Cocotb testbench · Python', simulation: 'Simulation results · Icarus / Cocotb', netlistText: 'Mapped netlist · Sky130 standard cells', netlist: 'Netlist visualization · Yosys', sta: 'OpenSTA timing results · setup and hold', physical: 'Physical design parameters · OpenROAD / Sky130', physicalImage: 'Physical layout · OpenROAD DEF visualization', report: 'Verified pipeline report' };
  $('artifactLabel').textContent = labels[k] || 'Artifact';
  stats.hidden = k !== 'sta';
  if (k === 'sta') renderTimingStats();
  if (k === 'netlist' || k === 'physicalImage') {
    text.hidden = true;
    const src = art[k] || '';
    image.hidden = !src;
    image.alt = k === 'netlist' ? 'Sky130 mapped netlist graph' : 'Physical design layout image';
    image.src = src;
    image.onerror = () => {
      if (image.getAttribute('src') !== src) return;
      image.hidden = true;
      image.removeAttribute('src');
      text.hidden = false;
      text.textContent = k === 'physicalImage'
        ? 'Physical layout image could not be loaded. Check the physical-design output and artifact endpoint.'
        : 'Netlist image could not be loaded.';
    };
    if (!src) {
      image.removeAttribute('src');
      text.hidden = false;
      text.textContent = k === 'physicalImage' ? 'Physical layout image will appear after OpenROAD produces a DEF.' : 'Netlist image is not available yet.';
    }
    return;
  }
  image.hidden = true;
  text.hidden = false;
  text.textContent = art[k] || 'Waiting for artifact...';
}

function renderTimingStats() {
  const metrics = buildSteps().sta.metrics || {};
  const metric = (key, unit = 'ns') => metrics[key] == null ? 'N/A' : `${fmtMetric(metrics[key])} ${unit}`;
  $('artifactStats').innerHTML = [
    ['Setup slack', metric('setup_slack_ns'), 'setup_slack_ns'],
    ['Hold slack', metric('hold_slack_ns'), 'hold_slack_ns'],
    ['WNS', metric('wns_ns'), 'wns_ns'],
    ['TNS', metric('tns_ns'), 'tns_ns'],
    ['Setup paths', metrics.setup_paths ?? 'N/A', 'setup_paths'],
    ['Hold paths', metrics.hold_paths ?? 'N/A', 'hold_paths']
  ].map(([label, value]) => `<div class="timing-stat"><span>${esc(label)}</span><b>${esc(String(value))}</b></div>`).join('');
}

function artifactUrl(path) {
  return `/api/design/${job}/artifact?path=${encodeURIComponent(path)}`;
}

function renderArtifactSummary() {
  const steps = buildSteps();
  const s = steps.synthesis || {}, sta = steps.sta || {}, pd = steps.physical_design || {}, pv = steps.physical_verification || {};
  const value = (step, key, fallback = '—') => step.metrics?.[key] ?? fallback;
  $('artifactSummary').innerHTML = [
    ['Simulation', steps.simulation?.status === 'passed' ? 'PASS' : (steps.simulation?.status || 'Pending'), steps.simulation?.status === 'passed' ? 'ok' : 'warn'],
    ['Mapped cells', value(s, 'cell_count', 'Sky130 HD'), s.status === 'passed' ? 'ok' : 'warn'],
    ['STA WNS', value(sta, 'wns_ns'), sta.status === 'passed' ? 'ok' : 'warn'],
    ['Routing', value(pd, 'routing', pd.status === 'passed' ? 'PASS' : 'Pending'), pd.metrics?.routing === 'pass' ? 'ok' : 'warn'],
    ['GDSII stream-out', value(pd, 'gds_stream_out', 'Pending'), pd.metrics?.gds_stream_out === 'pass' ? 'ok' : 'warn'],
    ['DRC', value(pv, 'drc', pv.status === 'passed' ? 'PASS' : 'Pending'), pv.metrics?.drc === 'pass' ? 'ok' : 'warn'],
    ['LVS', value(pv, 'lvs', pv.status === 'passed' ? 'PASS' : 'Not run'), pv.metrics?.lvs === 'pass' ? 'ok' : 'warn']
  ].map(([label, val, tone]) => `<div class="artifact-stat ${tone}"><span>${esc(label)}</span><b>${esc(String(val))}</b></div>`).join('');
}

async function load(path, key) {
  if (!job) return false;
  try {
    const r = await fetch(`/api/design/${job}/artifact?path=${encodeURIComponent(path)}`);
    if (!r.ok) return false;
    art[key] = await r.text();
    show(activeTab());
    return true;
  } catch { return false; }
}

async function loadArtifactsFromSnapshot(d) {
  const simulation = d.steps?.simulation;
  if (simulation) {
    art.simulation = simulation.output || simulation.error || '';
    if (!art.simulation && d.artifacts?.['results.xml']) {
      await load('results.xml', 'simulation');
    }
    if (!art.simulation) {
      art.simulation = simulation.status === 'running'
        ? 'Simulation is still running...'
        : 'Simulation did not produce output.';
    }
  }
  const staStep = d.steps?.sta;
  if (staStep) art.sta = staStep.output || staStep.error || 'OpenSTA result not available.';
  if (d.artifacts?.['rtl/design.sv']) await load('rtl/design.sv', 'rtl');
  if (d.artifacts?.['testbench/test_design.py']) await load('testbench/test_design.py', 'tb');
  if (d.artifacts?.['synthesized.v']) await load('synthesized.v', 'netlistText');
  const netlistImage = d.artifacts?.['synthesized.png'] ? 'synthesized.png' : 'synthesized.svg';
  if (d.artifacts?.[netlistImage]) art.netlist = artifactUrl(netlistImage);
  const staPath = d.artifacts?.['sta_mapped.log'] || d.artifacts?.['sta.log'];
  if (staPath && !art.sta) await load(d.artifacts?.['sta_mapped.log'] ? 'sta_mapped.log' : 'sta.log', 'sta');
  const physicalPath = d.artifacts?.['physical_design.rpt']
    ? 'physical_design.rpt'
    : d.artifacts?.['physical_design.log']
      ? 'physical_design.log'
      : d.artifacts?.['config.json'] ? 'config.json' : 'verify.sh';
  if (d.artifacts?.[physicalPath]) await load(physicalPath, 'physical');
  if (d.artifacts?.['drc.log']) await load('drc.log', 'physicalVerification');
  if (d.artifacts?.['lvs.log']) await load('lvs.log', 'physicalVerification');
  const imagePath = ['physical_layout.svg', 'physical_layout.png'].find(p => d.artifacts?.[p])
    || Object.keys(d.artifacts || {}).find(p => /physical|openroad|layout/i.test(p) && /\.(png|jpg|jpeg|svg)$/i.test(p));
  if (imagePath) art.physicalImage = artifactUrl(imagePath);
  if (d.report && Object.keys(d.report).length) art.report = JSON.stringify(d.report, null, 2);
  renderArtifactSummary();
}

function applyStatus(status) {
  if (!status) return;
  const terminalStatus = status === 'completed' || status === 'failed' || status === 'needs_clarification';
  setBadge(
    status === 'completed' ? 'completed' : status === 'failed' ? 'failed' : status === 'needs_clarification' ? 'warn' : 'running',
    status === 'needs_clarification' ? 'NEEDS CLARIFICATION' : status.toUpperCase(),
  );
  if (terminalStatus) $('run').disabled = false;
}

async function loadJob(id) {
  const d = await fetchJson(`/api/design/${id}`);
  if (!d) { setBadge('failed', 'LOAD FAILED'); return; }
  job = id; snap = d; selected = null; art = { rtl: '', tb: '', simulation: '', netlistText: '', netlist: '', sta: '', physical: '', physicalImage: '', report: '' };
  $('prompt').value = d.prompt || '';
  $('job').textContent = 'Job ' + id;
  applyStatus(d.status);
  await loadArtifactsFromSnapshot(d);
  reconnectAttempts = 0;
  connectWebSocket(id);
  show(activeTab());
}

function connectWebSocket(id) {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (ws) { try { ws.onclose = null; ws.onmessage = null; ws.close(); } catch { } }
  terminal = false;
  // The server replays the job's whole event history on every connect, so start clean.
  events = [];
  setConn('wait', 'Connecting');
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const sock = new WebSocket(`${proto}://${location.host}/ws/${id}`);
  ws = sock;

  sock.onopen = () => { reconnectAttempts = 0; setConn('live', 'Live'); };

  sock.onmessage = async m => {
    let e;
    try { e = JSON.parse(m.data); } catch { return; }

    if (e.type === 'job_not_found') { terminal = true; setConn('idle', 'Not found'); setBadge('failed', 'JOB NOT FOUND'); return; }

    if (e.type === 'state_snapshot') {
      snap = e;
      applyStatus(e.status);
      await loadArtifactsFromSnapshot(e);
      // Artifact loading may finish after the selected tab was rendered.
      // Repaint the board so completed simulation/STA/netlist content replaces
      // the previous waiting state immediately.
      show(activeTab());
      render();
      return;
    }

    events.push(e);
    render();

    if (e.type === 'pipeline_needs_clarification') {
      terminal = true;
      setConn('idle', 'Waiting for details');
      applyStatus('needs_clarification');
      const missing = (e.missing || []).join(', ');
      art.report = e.report ? JSON.stringify(e.report, null, 2) : `Missing mandatory specification: ${missing}`;
      show(activeTab());
      $('run').disabled = false;
      loadHistory();
      return;
    }

    // Keep the artifact board live with the same tool output shown in the log.
    // These stages can finish before the final pipeline snapshot is persisted.
    if (e.step === 'simulation' && (e.output || e.error)) {
      art.simulation = e.output || e.error;
      if (activeTab() === 'simulation') show('simulation');
    }
    if (e.step === 'sta' && (e.output || e.error)) {
      art.sta = e.output || e.error;
      if (activeTab() === 'sta') show('sta');
    }

    if (e.type === 'step_completed' && e.status === 'passed') {
      if (e.step === 'rtl') await load('rtl/design.sv', 'rtl');
      if (e.step === 'testbench') await load('testbench/test_design.py', 'tb');
      if (e.step === 'synthesis') {
        await load('synthesized.v', 'netlistText');
        art.netlist = artifactUrl('synthesized.png');
        show(activeTab());
      }
      if (e.step === 'physical_design') {
        art.physicalImage = artifactUrl('physical_layout.svg');
        if (activeTab() === 'physicalImage') show('physicalImage');
      }
    }

    if (e.type === 'pipeline_completed' || e.type === 'pipeline_failed') {
      terminal = true;
      setConn('idle', 'Finished');
      setBadge(e.type === 'pipeline_completed' ? 'completed' : 'failed', e.type === 'pipeline_completed' ? 'COMPLETED' : 'FAILED');
      const latest = await fetchJson(`/api/design/${job}`);
      if (latest) { snap = latest; await loadArtifactsFromSnapshot(latest); }
      await load('rtl/design.sv', 'rtl');
      await load('testbench/test_design.py', 'tb');
      if (e.report) art.report = JSON.stringify(e.report, null, 2);
      show(activeTab());
      $('run').disabled = false;
      render();
      loadHistory();
    }
  };

  // uvicorn --reload closes sockets with 1012 on every restart; without this the
  // dashboard would sit frozen on stale state with no indication.
  sock.onclose = () => {
    if (sock !== ws || terminal) return;
    scheduleReconnect(id);
  };
  sock.onerror = () => { };
}

function scheduleReconnect(id) {
  if (reconnectAttempts >= MAX_RECONNECT) {
    setConn('down', 'Disconnected');
    $('run').disabled = false;
    return;
  }
  const delay = Math.min(500 * 2 ** reconnectAttempts, 8000);
  reconnectAttempts++;
  setConn('wait', `Reconnecting ${reconnectAttempts}/${MAX_RECONNECT}`);
  reconnectTimer = setTimeout(() => connectWebSocket(id), delay);
}

async function refreshPipeline() {
  const btn = $('refreshPipeline');
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Refreshing...';
  try {
    if (job) {
      const d = await fetchJson(`/api/design/${job}`);
      if (d) {
        snap = d;
        applyStatus(d.status);
        await loadArtifactsFromSnapshot(d);
        if (d.status === 'completed' || d.status === 'failed' || d.status === 'needs_clarification') terminal = true;
        render();
      } else {
        setConn('down', 'Unreachable');
      }
      if (!terminal && (!ws || ws.readyState > 1)) { reconnectAttempts = 0; connectWebSocket(job); }
    } else {
      render();
    }
    await loadHistory();
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

async function loadHistory() {
  const d = await fetchJson('/api/designs');
  if (!d) { $('history').innerHTML = '<span class="mini">Cannot reach the server.</span>'; return; }
  $('history').innerHTML = (d.jobs || []).map(x => {
    const cls = x.status === 'completed' ? 'ok' : x.status === 'failed' ? 'bad' : 'warn';
    return `<button class="history-btn${x.job_id === job ? ' sel' : ''}" data-id="${esc(x.job_id)}">
      <span class="dot ${cls}"></span>
      <b>${esc((x.status || '').toUpperCase())}</b>
      <span class="mini">${esc(new Date(x.updated_at).toLocaleString())}</span>
      <span>${esc((x.prompt || '').slice(0, 80))}</span>
    </button>`;
  }).join('') || '<span class="mini">No saved runs yet.</span>';
  document.querySelectorAll('.history-btn').forEach(b => b.onclick = () => loadJob(b.dataset.id));
}

async function runPipeline() {
  if ($('run').disabled) return;
  $('run').disabled = true;
  events = []; snap = {}; selected = null; art = { rtl: '', tb: '', simulation: '', netlistText: '', netlist: '', sta: '', physical: '', physicalImage: '', report: '' };
  setBadge('running', 'RUNNING');
  render();
  let d;
  try {
    const r = await fetch('/api/design', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: $('prompt').value })
    });
    d = await r.json().catch(() => ({}));
    if (!r.ok) { setBadge('failed', d.detail || 'FAILED'); $('run').disabled = false; return; }
  } catch {
    setBadge('failed', 'SERVER UNREACHABLE');
    setConn('down', 'Unreachable');
    $('run').disabled = false;
    return;
  }
  job = d.job_id;
  $('job').textContent = 'Job ' + job;
  reconnectAttempts = 0;
  connectWebSocket(job);
  loadHistory();
}

$('run').onclick = runPipeline;
$('refreshPipeline').onclick = refreshPipeline;
$('refreshJobs').onclick = loadHistory;
$('logFilter').oninput = render;
$('autoscroll').onchange = render;

$('prompt').onkeydown = e => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); runPipeline(); } };

$('copyArtifact').onclick = async () => {
  const btn = $('copyArtifact');
  if (['netlist', 'physicalImage'].includes(activeTab())) return;
  const text = art[activeTab()] || '';
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = 'Copied';
  } catch {
    btn.textContent = 'Copy failed';
  }
  setTimeout(() => btn.textContent = 'Copy', 1200);
};

document.querySelectorAll('.tab').forEach(b => b.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  show(b.dataset.tab);
});

// Recover the stream after sleep/offline without needing a manual refresh.
window.addEventListener('online', () => { if (job && !terminal && (!ws || ws.readyState > 1)) { reconnectAttempts = 0; connectWebSocket(job); } });
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && job && !terminal && (!ws || ws.readyState > 1)) { reconnectAttempts = 0; connectWebSocket(job); }
});

render();
show('rtl');
loadHistory();
