import { api, fmt, $ } from '/web/app.js';

const esc = fmt.htmlSafe;
const VERDICT = {
  improved:  ['good', 'moved the right way'],
  no_change: ['muted', 'no clear change'],
  worse:     ['bad', 'moved the wrong way'],
};

const val = (v, unit) => v == null ? '—'
  : unit === 'usd' ? fmt.usd(v) : unit === 'ratio' ? fmt.pct(v) : (+v).toFixed(2);

function verdictCell(e) {
  if (e.status !== 'checked') return `<span class="muted" title="Needs 7 full days after the day it was applied">pending · judged from ${esc(e.due_on)}</span>`;
  const [cls, text] = VERDICT[e.verdict] || ['muted', esc(e.verdict)];
  return `<span class="${cls}">${text}</span> <span class="muted" style="font-size:11px">indicative</span>`;
}

function rowHtml(e, metrics) {
  const m = metrics[e.metric] || { label: e.metric, direction: 'lower', unit: 'count' };
  const thin = n => n != null && n < 7 ? ' class="unpriced" title="Fewer than 7 days had data: a thin sample"' : '';
  return `<tr>
    <td class="mono">${esc(e.applied_at.slice(0, 10))}</td>
    <td><span class="badge">${esc(e.kind)}</span></td>
    <td>${esc(m.label)} <span class="muted" style="font-size:11px">(${m.direction} is better)</span></td>
    <td>${esc(e.description)}</td>
    <td class="num">${val(e.baseline, m.unit)}</td>
    <td class="num">${val(e.after, m.unit)}</td>
    <td class="num"><span${thin(e.days_before)}>${e.days_before ?? '—'}</span> / <span${e.days_after == null ? '' : thin(e.days_after)}>${e.days_after ?? '—'}</span></td>
    <td>${verdictCell(e)}</td></tr>`;
}

export default async function (root) {
  const d = await api('/api/ledger');
  const metrics = Object.fromEntries(d.metrics.map(m => [m.name, m]));
  root.innerHTML = `
    <div class="card">
      <h2>Fix ledger</h2>
      <p class="muted" style="margin:0 0 12px">Make one change at a time to cut token cost: a rule in CLAUDE.md, a model choice, a habit.
        Record it here with its metric. The baseline is the average of the ${d.window_days} days before today; once ${d.window_days} full days have passed
        the same metric is averaged again and compared. Claude usage only. <b>Verdicts are indicative, not proof:</b>
        one user, one week, and other things changed too. A move under ${Math.round(d.threshold * 100)}% counts as no change.</p>
      <form id="ledger-form" class="flex" style="gap:8px;flex-wrap:wrap;margin-bottom:6px">
        <select id="lg-kind" aria-label="Kind">${d.kinds.map(k => `<option value="${esc(k)}">${esc(k)}</option>`).join('')}</select>
        <select id="lg-metric" aria-label="Metric">${d.metrics.map(m => `<option value="${esc(m.name)}">${esc(m.label)} (${m.direction} is better)</option>`).join('')}</select>
        <input id="lg-desc" type="text" maxlength="500" required placeholder="What did you change?" aria-label="Description" style="flex:1;min-width:240px">
        <button class="primary" type="submit">Record change</button>
        <button type="button" id="lg-check" title="Judge every entry whose 7 days are over">Check now</button>
      </form>
      <div id="lg-msg" class="muted" role="status" style="min-height:18px;margin-bottom:8px"></div>
      ${d.entries.length ? `<table>
        <thead><tr><th>applied</th><th>kind</th><th>metric</th><th>change</th><th class="num">baseline</th><th class="num">after</th>
          <th class="num" title="days that had data: before / after">days of data</th><th>verdict</th></tr></thead>
        <tbody>${d.entries.map(e => rowHtml(e, metrics)).join('')}</tbody></table>`
        : '<p class="muted">No changes recorded yet.</p>'}
    </div>`;

  const msg = (t, cls = 'muted') => { const el = $('#lg-msg', root); el.className = cls; el.textContent = t; };
  const post = (path, body) => fetch(path, { method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Token-Dashboard': '1' }, body: JSON.stringify(body) });
  const rerender = () => window.dispatchEvent(new HashChangeEvent('hashchange'));

  $('#ledger-form', root).addEventListener('submit', async ev => {
    ev.preventDefault();
    const r = await post('/api/ledger', { kind: $('#lg-kind', root).value, metric: $('#lg-metric', root).value,
                                          description: $('#lg-desc', root).value });
    if (!r.ok) return msg((await r.json().catch(() => ({}))).error || `failed (${r.status})`, 'bad');
    rerender();
  });
  $('#lg-check', root).addEventListener('click', async () => {
    const r = await post('/api/ledger/check', {});
    if (!r.ok) return msg(`check failed (${r.status})`, 'bad');
    const n = (await r.json()).checked.length;
    n ? rerender() : msg('Nothing is due yet: an entry is judged once 7 full days have passed since it was applied.');
  });
}
