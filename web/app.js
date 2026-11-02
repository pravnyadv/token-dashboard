// app.js — router, state, fetch helpers

export const $  = (sel, root=document) => root.querySelector(sel);
export const $$ = (sel, root=document) => Array.from(root.querySelectorAll(sel));

const COMPACT = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
export const fmt = {
  int:   n => (n ?? 0).toLocaleString(),
  compact: n => COMPACT.format(n ?? 0),
  usd:   n => n == null ? '—' : '$' + Number(n).toFixed(2),
  usd4:  n => n == null ? '—' : '$' + Number(n).toFixed(4),
  pct:   n => n == null ? '—' : (n * 100).toFixed(0) + '%',
  short: (s, n=80) => s == null ? '' : (s.length > n ? s.slice(0, n - 1) + '…' : s),
  htmlSafe: s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  modelClass: m => {
    const s = (m || '').toLowerCase();
    if (s.includes('fable') || s.includes('mythos')) return 'fable';
    if (s.includes('opus'))   return 'opus';
    if (s.includes('sonnet')) return 'sonnet';
    if (s.includes('haiku'))  return 'haiku';
    return '';
  },
  modelShort: m => (m || '').replace('claude-', ''),
  ts: t => (t || '').slice(0, 16).replace('T', ' '),
};

// Provider filter (claude | codex | all). Only these endpoints honour it; every other tab is Claude-only.
export const PROVIDERS = [['claude', 'Claude'], ['codex', 'Codex'], ['all', 'All']];
const PROVIDER_API = ['/api/overview', '/api/daily', '/api/by-model', '/api/sessions', '/api/projects', '/api/repos'];
const PROVIDER_ROUTES = ['/overview', '/sessions', '/projects'];

function readProvider() {
  try { const v = localStorage.getItem('td.provider'); if (PROVIDERS.some(p => p[0] === v)) return v; }
  catch (e) { /* storage unavailable */ }
  return 'claude';
}

function withProvider(path) {
  const [base, q] = path.split('?');
  if (state.provider === 'claude' || !PROVIDER_API.includes(base)) return path;
  return base + '?' + (q ? q + '&' : '') + 'provider=' + state.provider;
}

export async function api(path, opts) {
  const r = await fetch(withProvider(path), opts);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

export const state = { plan: 'api', pricing: null, provider: readProvider() };

const ROUTES = {
  '/overview': () => import('/web/routes/overview.js'),
  '/prompts':  () => import('/web/routes/prompts.js'),
  '/sessions': () => import('/web/routes/sessions.js'),
  '/recent':  () => import('/web/routes/recent.js'),
  '/agents':   () => import('/web/routes/agents.js'),
  '/activity': () => import('/web/routes/activity.js'),
  '/compare':  () => import('/web/routes/compare.js'),
  '/projects': () => import('/web/routes/projects.js'),
  '/skills':   () => import('/web/routes/skills.js'),
  '/costs':    () => import('/web/routes/costs.js'),
  '/cache':    () => import('/web/routes/cache.js'),
  '/compactions': () => import('/web/routes/compactions.js'),
  '/ledger':   () => import('/web/routes/ledger.js'),
  '/tips':     () => import('/web/routes/tips.js'),
  '/settings': () => import('/web/routes/settings.js'),
};

function buildTopbar() {
  const wrap = document.createElement('header');
  wrap.className = 'topbar';
  wrap.innerHTML = `
    <div class="brand">Token Dashboard</div>
    <nav>
      ${Object.keys(ROUTES).map(p => `<a href="#${p}" data-route="${p}">${p.slice(1)}</a>`).join('')}
    </nav>
    <label class="provider-pick muted" title="Which CLI's usage the Overview, Sessions and Projects tabs show. All other tabs are Claude-only.">
      provider
      <select id="provider-select" aria-label="Provider">
        ${PROVIDERS.map(([k, label]) => `<option value="${k}" ${k === state.provider ? 'selected' : ''}>${label}</option>`).join('')}
      </select>
    </label>
    <div class="topbar-pills">
    <span id="compare-slot"></span>
    <span class="pill" id="plan-pill">api</span>
    <span class="pill muted" title="Cmd/Ctrl+B blurs sensitive text">⌘B blur</span>
    </div>
  `;
  document.body.prepend(wrap);
  wrap.querySelector('#provider-select').addEventListener('change', e => {
    state.provider = e.target.value;
    try { localStorage.setItem('td.provider', state.provider); } catch (err) { /* ignore */ }
    render();
  });
  import('/web/whatif.js').then(m => m.mountCompareSelector(wrap.querySelector('#compare-slot'))).catch(() => {});
}

function setActiveTab(routeKey) {
  $$('header.topbar nav a').forEach(a => a.classList.toggle('active', a.dataset.route === routeKey));
}

async function render() {
  const hash = location.hash.replace(/^#/, '') || '/overview';
  const path = hash.split('?')[0];
  let key = path;
  if (path.startsWith('/sessions/')) key = '/sessions';
  if (path.startsWith('/costs/')) key = '/costs';
  setActiveTab(key);
  const loader = ROUTES[key] || ROUTES['/overview'];
  const mod = await loader();
  $('#app').innerHTML = '';
  try {
    await mod.default($('#app'));
    if (state.provider !== 'claude' && !PROVIDER_ROUTES.includes(key)) {
      $('#app').insertAdjacentHTML('afterbegin',
        `<div class="card provider-note" role="note">The provider filter is set to <b>${fmt.htmlSafe(state.provider)}</b>,
         but this tab shows <b>Claude usage only</b>: it relies on Claude's prompt-cache model, which Codex does not share.
         The filter applies to Overview, Sessions and Projects.</div>`);
    }
  } catch (e) {
    $('#app').innerHTML = `<div class="card"><h2>Error</h2><pre>${fmt.htmlSafe(String(e.stack || e))}</pre></div>`;
  }
}

async function firstRun() {
  if (localStorage.getItem('td.plan-set')) return;
  const plans = Object.entries(state.pricing.plans);
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <h2>Welcome — pick your plan</h2>
      <p>This sets how costs are displayed. Change it later in Settings.</p>
      <select id="firstplan" style="width:100%">
        ${plans.map(([k,v]) => `<option value="${k}">${v.label}${v.monthly ? ` — $${v.monthly}/mo` : ''}</option>`).join('')}
      </select>
      <div class="actions">
        <div class="spacer"></div>
        <button class="primary" id="firstsave">Continue</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  await new Promise(res => $('#firstsave', overlay).addEventListener('click', async () => {
    const plan = $('#firstplan', overlay).value;
    await fetch('/api/plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plan }) });
    localStorage.setItem('td.plan-set', '1');
    overlay.remove();
    res();
  }));
  state.plan = (await api('/api/plan')).plan;
}

function showScanProgress(st) {
  let el = document.getElementById('scan-banner');
  if (!st || !st.running) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement('div');
    el.id = 'scan-banner';
    el.style.cssText = 'position:fixed;bottom:12px;right:12px;z-index:50;padding:8px 14px;' +
      'background:#1b2330;color:#cdd6e0;border:1px solid #2c3a4d;border-radius:6px;font-size:13px';
    document.body.appendChild(el);
  }
  el.textContent = `Indexing ${st.files_done}/${st.files_total} files…`;
}

function offerRefresh() {
  if (document.getElementById('refresh-banner')) return;
  const el = document.createElement('button');
  el.id = 'refresh-banner';
  el.textContent = 'New data. Click to refresh';
  el.style.cssText = 'position:fixed;bottom:12px;right:12px;z-index:50;padding:8px 14px;cursor:pointer;' +
    'background:#1b2330;color:#cdd6e0;border:1px solid #2c3a4d;border-radius:6px;font-size:13px';
  el.onclick = () => { el.remove(); render(); };
  document.body.appendChild(el);
}

async function boot() {
  buildTopbar();
  const planResp = await api('/api/plan');
  state.plan = planResp.plan;
  state.pricing = planResp.pricing;
  $('#plan-pill').textContent = state.plan;

  await firstRun();

  window.addEventListener('hashchange', render);
  await render();

  // Privacy blur (Cmd+B / Ctrl+B)
  window.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'b') {
      e.preventDefault();
      document.body.classList.toggle('privacy-on');
    }
  });

  api('/api/scan/status').then(showScanProgress).catch(() => {});
  let openScanPending = true;
  api('/api/scan').catch(() => {}); // rescan on open; the SSE stream refreshes the page once it finishes

  // SSE diff stream
  try {
    const es = new EventSource('/api/stream');
    es.onmessage = ev => {
      try {
        const evt = JSON.parse(ev.data);
        if (evt.type === 'scan_progress') showScanProgress(evt);
        // Re-render once for the scan started on open. Later scans (every 30 s) only offer a refresh.
        if (evt.type === 'scan_progress' && !evt.running && openScanPending) { openScanPending = false; render(); }
        else if (evt.type === 'scan') offerRefresh();
      } catch {}
    };
  } catch {}
}

boot();
