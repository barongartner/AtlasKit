/* Watches page: everything Atlas keeps an eye on (email, prices, webpages,
   YouTube, sports). Backend: App/watches_api.py (WatchesMixin), reached
   through window.pywebview.api.watches_*. Styles: watches.css.

   window.Watches.render(container)  draws the page into container
   window.Watches.addTeams(list)     [{league, team, start?}] -> {ok, added, errors}
                                     (for the setup walkthrough) */
(function () {
  'use strict';

  const E = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const el = html => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };

  async function api(fn, ...args) {
    const a = window.pywebview && window.pywebview.api;
    if (!a || typeof a[fn] !== 'function') return { ok: false, error: 'The app backend is not connected.' };
    try { return (await a[fn](...args)) || { ok: false, error: 'No answer from the app.' }; }
    catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
  }
  const say = (text, error) => { if (typeof window.toast === 'function') window.toast(text, error); else console.log(text); };
  const confirmBox = (title, text, primary) =>
    typeof window.ask === 'function' ? window.ask(title, text, primary) : Promise.resolve(window.confirm(`${title}\n\n${text}`));

  const IC = { mail: '\uE715', price: '\uE9D2', web: '\uE774', video: '\uE714', team: '\uE7FC', add: '\uE710', refresh: '\uE72C',
    check: '\uE895', pause: '\uE769', play: '\uE768', del: '\uE74D', warn: '\uE7BA', eye: '\uE7B3' };
  const TYPES = [
    { key: 'email', name: 'Email', group: 'Email', icon: IC.mail },
    { key: 'price', name: 'Price', group: 'Prices', icon: IC.price },
    { key: 'web', name: 'Webpage', group: 'Webpages', icon: IC.web },
    { key: 'youtube', name: 'YouTube', group: 'YouTube', icon: IC.video },
    { key: 'team', name: 'Sports', group: 'Sports', icon: IC.team },
  ];
  const DEFAULT_LEAGUES = [
    { key: 'nhl', name: 'NHL' }, { key: 'nba', name: 'NBA' }, { key: 'nfl', name: 'NFL' }, { key: 'mlb', name: 'MLB' },
    { key: 'mls', name: 'MLS' }, { key: 'epl', name: 'Premier League' }, { key: 'f1', name: 'Formula 1', individual: true },
  ];

  // Form fields per type. kind: text (default), select, seg, toggle.
  const FIELDS = {
    email: [
      { k: 'field', label: 'Match on', kind: 'seg', options: [['from', 'Sender'], ['subject', 'Subject']], value: 'from' },
      { k: 'match', label: 'Sender name or address', ph: 'someone@example.com',
        hint: 'A full address beats a first name. Only mail that lands after you add it counts.' },
    ],
    price: [
      { k: 'symbol', label: 'Symbol', ph: 'AAPL, SHOP.TO, BTC', hint: 'Stocks come from Yahoo Finance, crypto from CoinGecko.' },
      { k: 'kind', label: 'Kind', kind: 'select', options: [['auto', 'Work it out'], ['stock', 'Stock'], ['crypto', 'Crypto']] },
      { k: 'condition', label: 'Tell me when it', kind: 'select',
        options: [['above', 'Goes above'], ['below', 'Goes below'], ['up', 'Rises in a day by (%)'], ['down', 'Falls in a day by (%)'], ['move', 'Moves either way in a day by (%)']] },
      { k: 'value', label: 'Price', ph: '250', hint: 'Fires once per crossing, not on every check.' },
      { k: 'currency', label: 'Currency for crypto', ph: 'usd', opt: true },
    ],
    web: [
      { k: 'url', label: 'Page address', ph: 'https://example.com/product', full: true },
      { k: 'mode', label: 'Tell me when', kind: 'select', options: [['change', 'The page changes'], ['appears', 'Some text shows up'], ['disappears', 'Some text goes away']] },
      { k: 'text', label: 'Text to look for', ph: 'In stock' },
      { k: 'selector', label: 'Only this part of the page', ph: '.price or #stock', opt: true, full: true,
        hint: 'A CSS selector: tags, #id, .class and [attr=value] work. Leave it blank for the whole page.' },
    ],
    youtube: [
      { k: 'channel', label: 'Channel', ph: '@handle, a channel link, or a channel id', full: true, hint: 'Fires on every new upload.' },
    ],
    team: [
      { k: 'league', label: 'League', kind: 'select', options: [] },
      { k: 'team', label: 'Team', ph: 'Maple Leafs' },
      { k: 'start', label: 'Also tell me when games start', kind: 'toggle', full: true, hint: 'Final scores always come through.' },
    ],
  };

  const S = { root: null, data: null, form: null, busy: new Set(), timer: 0, loading: false };

  /* ------------------------------------------------------------ render -- */
  function render(container) {
    if (!container) return;
    S.root = container;
    container.innerHTML = '';
    container.appendChild(el(`<div class="wx">
      <div class="wx-bar">
        <div class="wx-count" data-f="count"></div>
        <div class="spacer"></div>
        <button class="btn" data-act="refresh"><i class="ico">${IC.refresh}</i><span>Refresh</span></button>
        <button class="btn accent" data-act="add"><i class="ico">${IC.add}</i><span>Add watch</span></button>
      </div>
      <div data-f="form"></div>
      <div data-f="list"><div class="wx-loading"><span class="ring"></span><span>Loading watches...</span></div></div>
    </div>`));
    container.onclick = onClick;
    if (S.form) drawForm();
    load();
    clearInterval(S.timer);
    S.timer = setInterval(() => {
      if (!S.root || !document.contains(S.root)) { clearInterval(S.timer); return; }
      if (S.root.offsetParent !== null && !S.busy.size) load(true);
    }, 30000);
  }

  const part = k => S.root && S.root.querySelector(`[data-f="${k}"]`);

  async function load(quiet) {
    if (S.loading) return;
    S.loading = true;
    const res = await api('watches_list');
    S.loading = false;
    if (!res.ok) {
      if (!quiet) part('list').innerHTML = `<div class="infobar bad"><i class="ico">${IC.warn}</i><div class="ib-body"><span class="ib-title">Can't load watches</span>${E(res.error)}</div></div>`;
      return;
    }
    S.data = res;
    drawList();
  }

  function drawList() {
    const list = part('list');
    if (!list || !S.data) return;
    const all = S.data.watches || [];
    const active = all.filter(w => w.status === 'active').length;
    part('count').innerHTML = all.length
      ? `<b>${active}</b> active${all.length > active ? `, ${all.length - active} paused or done` : ''}`
      : '';
    if (!all.length) {
      list.innerHTML = `<div class="card wx-empty"><i class="ico">${IC.eye}</i><b>Nothing watched yet</b>
        <p>Add a watch here, or text Atlas something like "tell me when AAPL goes over 250" or "let me know the Leafs score after every game".</p></div>`;
      return;
    }
    const order = { active: 0, paused: 1, done: 2 };
    list.innerHTML = TYPES.map(t => {
      const rows = all.filter(w => w.type === t.key).sort((a, b) => (order[a.status] - order[b.status]) || (a.created < b.created ? 1 : -1));
      if (!rows.length) return '';
      return `<div class="wx-group"><div class="wx-ghead"><i class="ico">${t.icon}</i><span>${E(t.group)}</span><span class="cap">${rows.length}</span></div>
        <div class="card wx-card">${rows.map(rowHTML).join('')}</div></div>`;
    }).join('');
  }

  function chip(w) {
    if (w.status === 'paused') return '<span class="status warn"><span class="dot"></span>Paused</span>';
    if (w.status === 'done') return '<span class="status">Done</span>';
    if (w.last_error && (w.error_count || 0) > 0) return '<span class="status bad"><span class="dot"></span>Failing</span>';
    return '<span class="status ok"><span class="dot"></span>Active</span>';
  }

  function rowHTML(w) {
    const busy = S.busy.has(w.id);
    const meta = [];
    if (w.last_value) meta.push(`<span><b>Now</b>${E(w.last_value)}</span>`);
    meta.push(`<span><b>Last fired</b>${E(w.last_fired_when || 'never')}</span>`);
    if (w.last_checked_when) meta.push(`<span><b>Checked</b>${E(w.last_checked_when)}</span>`);
    if (w.status === 'active' && w.type !== 'email' && w.next_check_when) meta.push(`<span><b>Next check</b>${E(w.next_check_when)}</span>`);
    const toggle = w.status === 'active'
      ? `<button class="btn subtle icon" data-act="pause" title="Pause"><i class="ico">${IC.pause}</i></button>`
      : `<button class="btn subtle icon" data-act="resume" title="${w.status === 'done' ? 'Watch again' : 'Resume'}"><i class="ico">${IC.play}</i></button>`;
    return `<div class="wx-row${w.status !== 'active' ? ' off' : ''}${busy ? ' busy' : ''}" data-id="${E(w.id)}">
      <div class="wx-main">
        <div class="wx-top"><span class="wx-label" title="${E(w.id)}">${E(w.label)}</span>${chip(w)}</div>
        <div class="wx-sum">${E(w.summary)}</div>
        <div class="wx-meta">${meta.join('')}</div>
        ${w.last_note && w.last_fired ? `<div class="wx-note">${E(w.last_note)}</div>` : ''}
        ${w.last_error ? `<div class="wx-err"><i class="ico">${IC.warn}</i><span>${E(w.last_error)}</span></div>` : ''}
      </div>
      <div class="wx-acts">
        <button class="btn subtle" data-act="check"${busy ? ' disabled' : ''}>${busy ? '<span class="ring"></span>' : `<i class="ico">${IC.check}</i>`}<span>Check now</span></button>
        ${toggle}
        <button class="btn subtle icon danger-text" data-act="delete" title="Delete"><i class="ico">${IC.del}</i></button>
      </div>
    </div>`;
  }

  /* ------------------------------------------------------------ events -- */
  async function onClick(e) {
    const b = e.target.closest('[data-act]');
    if (!b || !S.root.contains(b) || b.disabled) return;
    const act = b.dataset.act;
    const row = b.closest('.wx-row');
    const id = row && row.dataset.id;
    const w = id && S.data && (S.data.watches || []).find(x => x.id === id);
    if (act === 'refresh') return load();
    if (act === 'add') { S.form = S.form || { type: 'email', values: {} }; drawForm(); return; }
    if (act === 'cancel') { S.form = null; part('form').innerHTML = ''; return; }
    if (act === 'type') { saveValues(); S.form.type = b.dataset.v; drawForm(); return; }
    if (act === 'segpick') { pickSeg(b); return; }
    if (act === 'save') return save();
    if (!w) return;
    if (act === 'check') return checkNow(w);
    if (act === 'pause' || act === 'resume') {
      const res = await api('watches_set_status', w.id, act === 'pause' ? 'paused' : 'active');
      say(res.ok ? res.message : res.error, !res.ok);
      return load(true);
    }
    if (act === 'delete') {
      if (!await confirmBox('Delete this watch?', `"${w.label}" stops for good. Pause it instead if you might want it back.`, 'Delete')) return;
      const res = await api('watches_remove', w.id);
      say(res.ok ? res.message : res.error, !res.ok);
      return load(true);
    }
  }

  async function checkNow(w) {
    S.busy.add(w.id);
    drawList();
    const res = await api('watches_check', w.id);
    S.busy.delete(w.id);
    if (res.watch && S.data) {
      const i = S.data.watches.findIndex(x => x.id === w.id);
      if (i >= 0) S.data.watches[i] = Object.assign({}, S.data.watches[i], res.watch);
    }
    say(res.ok ? res.message : (res.error || res.message), !res.ok);
    await load(true);
  }

  /* -------------------------------------------------------------- form -- */
  function leagues() {
    const l = (S.data && S.data.leagues) || [];
    return l.length ? l : DEFAULT_LEAGUES;
  }

  function fieldHTML(f, v) {
    const id = `wx-${f.k}`;
    const opt = f.opt ? '<span class="opt">optional</span>' : '';
    const hint = f.hint ? `<div class="hint" data-hint="${f.k}">${E(f.hint)}</div>` : '';
    let input;
    if (f.kind === 'select') {
      const options = f.k === 'league' ? leagues().map(x => [x.key, x.name]) : f.options;
      const cur = v != null ? v : options[0][0];
      input = `<select id="${id}" data-k="${f.k}">${options.map(([val, txt]) => `<option value="${E(val)}"${val === cur ? ' selected' : ''}>${E(txt)}</option>`).join('')}</select>`;
    } else if (f.kind === 'seg') {
      const cur = v || f.value;
      input = `<div class="seg" data-k="${f.k}" data-v="${E(cur)}">${f.options.map(([val, txt]) =>
        `<button type="button" data-act="segpick" data-v="${E(val)}" class="${val === cur ? 'on' : ''}">${E(txt)}</button>`).join('')}</div>`;
    } else if (f.kind === 'toggle') {
      return `<div class="wx-field${f.full ? ' full' : ''}"><label class="toggle"><input type="checkbox" data-k="${f.k}"${v ? ' checked' : ''}><span class="track"></span><span>${E(f.label)}</span></label>${hint}</div>`;
    } else {
      input = `<input class="tb" id="${id}" data-k="${f.k}" placeholder="${E(f.ph || '')}" spellcheck="false" value="${E(v || '')}">`;
    }
    return `<div class="wx-field${f.full ? ' full' : ''}" data-field="${f.k}"><label class="wx-l" for="${id}" data-label="${f.k}">${E(f.label)}${opt}</label>${input}${hint}</div>`;
  }

  function drawForm() {
    const slot = part('form');
    if (!slot || !S.form) return;
    const { type, values } = S.form;
    const fields = FIELDS[type].concat([{ k: 'label', label: 'Name it', ph: 'What Atlas calls it', opt: true, full: true }]);
    slot.innerHTML = `<div class="card wx-form">
      <div class="wx-ftitle">New watch</div>
      <div class="seg wx-types">${TYPES.map(t => `<button type="button" data-act="type" data-v="${t.key}" class="${t.key === type ? 'on' : ''}"><i class="ico">${t.icon}</i>${E(t.name)}</button>`).join('')}</div>
      <div class="wx-fields">${fields.map(f => fieldHTML(f, values[f.k])).join('')}</div>
      <div class="wx-ffoot"><div class="wx-fmsg" data-f="fmsg"></div><div class="spacer"></div>
        <button class="btn" data-act="cancel">Cancel</button>
        <button class="btn accent" data-act="save"><span>Add watch</span></button></div>
    </div>`;
    slot.querySelectorAll('select, input').forEach(x => x.addEventListener('change', () => { saveValues(); relabel(); }));
    slot.querySelectorAll('input.tb').forEach(x => x.addEventListener('keydown', e => { if (e.key === 'Enter') save(); }));
    relabel();
    const first = slot.querySelector('.wx-fields .tb');
    if (first) first.focus();
  }

  function pickSeg(b) {
    const seg = b.closest('.seg');
    seg.dataset.v = b.dataset.v;
    seg.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
    saveValues();
    relabel();
  }

  function readValues() {
    const out = {};
    const slot = part('form');
    if (!slot) return out;
    slot.querySelectorAll('[data-k]').forEach(x => {
      const k = x.dataset.k;
      if (x.classList.contains('seg')) out[k] = x.dataset.v;
      else if (x.type === 'checkbox') out[k] = x.checked;
      else out[k] = x.value.trim();
    });
    return out;
  }

  function saveValues() { if (S.form) S.form.values = Object.assign(S.form.values, readValues()); }

  // Labels and visibility that depend on other fields.
  function relabel() {
    const slot = part('form');
    if (!slot || !S.form) return;
    const v = readValues();
    const setLabel = (k, text, ph) => {
      const l = slot.querySelector(`[data-label="${k}"]`);
      if (l) l.firstChild.textContent = text;
      const i = slot.querySelector(`input[data-k="${k}"]`);
      if (i && ph != null) i.placeholder = ph;
    };
    const show = (k, on) => { const f = slot.querySelector(`[data-field="${k}"]`); if (f) f.classList.toggle('hidden', !on); };
    if (S.form.type === 'email') {
      if (v.field === 'subject') setLabel('match', 'Words in the subject', 'order shipped');
      else setLabel('match', 'Sender name or address', 'someone@example.com');
    }
    if (S.form.type === 'price') {
      const pct = ['up', 'down', 'move'].includes(v.condition);
      setLabel('value', pct ? 'Percent' : 'Price', pct ? '5' : '250');
      show('currency', v.kind !== 'stock');
    }
    if (S.form.type === 'web') show('text', v.mode !== 'change');
    if (S.form.type === 'team') {
      const lg = leagues().find(x => x.key === v.league);
      const solo = !!(lg && lg.individual);
      setLabel('team', solo ? 'Driver' : 'Team', solo ? 'Leave blank for just the winner' : 'Maple Leafs');
      const l = slot.querySelector('[data-label="team"]');
      if (l) {
        let o = l.querySelector('.opt');
        if (solo && !o) { o = document.createElement('span'); o.className = 'opt'; o.textContent = 'optional'; l.appendChild(o); }
        if (!solo && o) o.remove();
      }
    }
  }

  async function save() {
    if (!S.form) return;
    saveValues();
    const slot = part('form');
    const btn = slot.querySelector('[data-act="save"]');
    const msg = part('fmsg');
    if (btn.disabled) return;
    const type = S.form.type;
    const v = Object.assign({}, S.form.values);
    const params = {};
    FIELDS[type].concat([{ k: 'label' }]).forEach(f => {
      const field = slot.querySelector(`[data-field="${f.k}"]`);
      if (field && field.classList.contains('hidden')) return;
      if (v[f.k] !== undefined && v[f.k] !== '') params[f.k] = v[f.k];
    });
    btn.disabled = true;
    btn.innerHTML = '<span class="ring"></span><span>Checking...</span>';
    msg.className = 'wx-fmsg';
    msg.textContent = type === 'email' ? '' : 'Looking it up and taking a first reading...';
    const res = await api('watches_add', type, params);
    btn.disabled = false;
    btn.innerHTML = '<span>Add watch</span>';
    if (!res.ok) {
      msg.className = 'wx-fmsg bad';
      msg.textContent = `Not set: ${res.error}`;
      return;
    }
    say(res.message);
    S.form = null;
    slot.innerHTML = '';
    await load(true);
  }

  /* ----------------------------------------------------- setup helper -- */
  async function addTeams(list) {
    const res = await api('watches_add_teams', Array.isArray(list) ? list : []);
    if (!res.added && !res.errors) return { ok: false, added: [], errors: [{ error: res.error || 'Could not add the teams.' }] };
    if (S.root && document.contains(S.root)) load(true);
    return res;
  }

  window.Watches = { render, addTeams };
  window.watches_add_teams = addTeams;
})();
