/* Add-ons: one card per optional capability, in the setup walkthrough and on
   its own app page. Backend: App/addons_api.py (AddonsMixin), reached through
   window.call('addons_*'). Sign-ins that ask questions (the Reminders login)
   run as setup jobs and are read with setup_poll / setup_input / setup_cancel.

   Use: Addons.render(container, { mode: 'setup' | 'page' })
   'page' opens the cards that still need something; 'setup' starts with every
   card closed. render() returns { refresh }, and Addons.refresh() redraws
   every rendered list (call it when the page is shown again). */
(function () {
  'use strict';
  const api = (fn, ...a) => window.call(fn, ...a);
  const E = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const el = html => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const I = { check: '\uE73E', bad: '\uE783', info: '\uE946', open: '\uE8A7', copy: '\uE8C8', down: '\uE70D', plug: '\uE71B' };
  const PILL = { connected: ['ok', 'Connected'], not_connected: ['idle', 'Not connected'], error: ['bad', 'Needs a look'], off: ['off', 'Off'] };
  const GROUPS = [['default', 'On by default', 'These work without any setup. Turn off what you don\u2019t want.'],
                  ['connect', 'Connect', 'Each one needs a key or a sign-in. Open a card for the steps.']];
  const lists = new Set();

  /* ------------------------------------------------------------ list -- */
  function render(container, opts = {}) {
    const S = { mode: opts.mode === 'setup' ? 'setup' : 'page', open: new Set(), root: el(`<div class="ao ao-${opts.mode === 'setup' ? 'setup' : 'page'}"></div>`) };
    container.innerHTML = '';
    container.appendChild(S.root);
    S.root.appendChild(el('<div class="ao-loading"><span class="ring"></span><span>Loading add-ons</span></div>'));
    const handle = { refresh: () => load(S) };
    lists.add(S);
    load(S, true);
    return handle;
  }

  async function load(S, first) {
    if (!document.body.contains(S.root)) { lists.delete(S); return; }
    const data = await api('addons_list');
    if (!data || !data.addons) {
      S.root.innerHTML = '';
      S.root.appendChild(note('bad', 'The add-ons list didn\u2019t load. Close the app and open it again.'));
      return;
    }
    if (first && S.mode === 'page') data.addons.forEach(a => { if (a.enabled && (a.status === 'not_connected' || a.status === 'error')) S.open.add(a.id); });
    S.root.innerHTML = '';
    if (data.dry) S.root.appendChild(note('warn', 'Dry run: sign-ins are faked and nothing connects for real.'));
    GROUPS.forEach(([g, title, sub]) => {
      const items = data.addons.filter(a => a.group === g);
      if (!items.length) return;
      const sec = el(`<div class="ao-group"><div class="ao-gtitle">${E(title)}</div><div class="ao-gsub">${E(sub)}</div><div class="ao-cards"></div></div>`);
      items.forEach(a => sec.querySelector('.ao-cards').appendChild(card(S, a)));
      S.root.appendChild(sec);
    });
  }

  function note(kind, text) {
    return el(`<div class="ao-note ${kind}"><i class="ico">${kind === 'bad' ? I.bad : I.info}</i><span>${E(text)}</span></div>`);
  }

  /* ------------------------------------------------------------ card -- */
  function card(S, a) {
    const c = el(`<div class="ao-card" data-id="${E(a.id)}">
      <div class="ao-head" tabindex="0">
        <span class="ao-tile"><i class="ico">${a.icon || I.plug}</i></span>
        <div class="ao-id"><div class="ao-name">${E(a.name)}</div><div class="ao-desc">${E(a.desc)}</div></div>
        <span class="ao-pill"></span>
        <label class="toggle ao-switch" title="On or off"><input type="checkbox"><span class="track"></span></label>
        <i class="ico ao-chev">${I.down}</i>
      </div>
      <div class="ao-body">
        <div class="ao-needs"><b>What it needs:</b> ${E(a.needs)}</div>
        <div class="ao-state"></div>
      </div></div>`);
    c._a = a;
    const body = c.querySelector('.ao-body');
    if (a.steps && a.steps.length) {
      body.appendChild(el(`<ol class="ao-steps">${a.steps.map(s => `<li>${E(s)}</li>`).join('')}</ol>`));
    }
    (a.copy || []).forEach(([label, text]) => body.appendChild(copyRow(label, text)));
    if (a.fields.length) {
      const grid = el('<div class="ao-fields"></div>');
      a.fields.forEach(f => grid.appendChild(fieldBox(f)));
      body.appendChild(grid);
    }
    const acts = el('<div class="ao-actions"></div>');
    const res = el('<div class="ao-result hidden"><span class="ic"></span><div class="tx"></div><button class="btn subtle more hidden">Details</button></div>');
    const lines = el('<pre class="ao-lines hidden"></pre>');
    // Sign-ins that ask questions (Reminders' iCloud login) use the setup
    // walkthrough's real form when it's loaded: fields and code boxes, not a terminal.
    const ask = window.Setup && window.Setup.askBox ? window.Setup.askBox(a.name, { doneText: 'Signed in.' }) : null;
    const con = ask ? { run: (job) => ask.run({ follow: job }), el: ask } : jobConsole();
    let save = null, connect = null, test = null, stop = null;
    if (a.fields.length) { save = el('<button class="btn">Save</button>'); acts.appendChild(save); }
    if (a.connect) { connect = el(`<button class="btn accent">${E(a.connectLabel || 'Connect')}</button>`); acts.appendChild(connect); }
    if (a.canTest) { test = el(`<button class="btn ${a.connect ? '' : 'accent'}">Test</button>`); acts.appendChild(test); }
    stop = el('<button class="btn subtle hidden">Cancel</button>');
    acts.appendChild(stop);
    (a.links || []).forEach(([t, u]) => acts.appendChild(linkBtn(t, u)));
    body.appendChild(acts);
    body.appendChild(res);
    body.appendChild(lines);
    body.appendChild(con.el || con);

    const head = c.querySelector('.ao-head');
    const sw = c.querySelector('.ao-switch input');
    const setOpen = on => { c.classList.toggle('open', on); on ? S.open.add(a.id) : S.open.delete(a.id); };
    setOpen(S.open.has(a.id));
    head.onclick = e => { if (!e.target.closest('.ao-switch')) setOpen(!c.classList.contains('open')); };
    head.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen(!c.classList.contains('open')); } };
    c.querySelector('.ao-switch').onclick = e => e.stopPropagation();
    sw.onchange = async () => {
      sw.disabled = true;
      const r = await api('addons_set_enabled', a.id, sw.checked);
      sw.disabled = false;
      if (r) paint(c, r);
    };

    const busy = on => [save, connect, test].forEach(b => { if (b) b.disabled = on; });
    const show = (kind, text, detail) => {
      res.classList.remove('hidden', 'ok', 'bad', 'wait');
      res.classList.add(kind);
      res.querySelector('.ic').innerHTML = kind === 'wait' ? '<span class="ring"></span>' : `<i class="ico">${kind === 'ok' ? I.check : I.bad}</i>`;
      res.querySelector('.tx').textContent = text || '';
      const more = res.querySelector('.more');
      lines.classList.add('hidden');
      if (detail && detail.length) {
        lines.textContent = detail.join('\n');
        more.classList.remove('hidden');
        more.textContent = 'Details';
        more.onclick = () => { const h = lines.classList.toggle('hidden'); more.textContent = h ? 'Details' : 'Hide details'; };
      } else more.classList.add('hidden');
    };

    const doSave = async () => {
      const r = await saveFields(c);
      if (r === null) return true;          // nothing changed
      if (!r.ok) { show('bad', 'Fix the field marked in red.'); return false; }
      paint(c, r.addon);
      return true;
    };

    const doTest = async () => {
      show('wait', `Testing ${a.name}...`);
      const r = await api('addons_test', a.id);
      if (!r) { show('bad', 'The test didn\u2019t run.'); return; }
      show(r.ok ? 'ok' : 'bad', r.message, r.lines);
      paint(c, r.addon);
    };

    if (save) save.onclick = async () => {
      busy(true);
      const changed = dirty(c);
      if (await doSave()) {
        if (changed && test && !a.connect) await doTest();
        else if (changed) show('ok', 'Saved.');
        else show('ok', 'Nothing changed.');
      }
      busy(false);
    };
    if (test) test.onclick = async () => { busy(true); if (await doSave()) await doTest(); busy(false); };
    if (connect) connect.onclick = async () => {
      busy(true);
      if (!await doSave()) { busy(false); return; }
      const r = await api('addons_connect', a.id);
      if (!r || !r.ok) { show('bad', (r && r.message) || 'It didn\u2019t start.'); busy(false); return; }
      stop.classList.remove('hidden');
      stop.onclick = () => api('addons_cancel', a.id);
      if (r.kind === 'job') {
        res.classList.add('hidden');
        const p = await con.run(r.job, r.message);  // askBox ignores the title
        const after = await api('addons_poll', a.id);
        if (after && after.addon) paint(c, after.addon);
        show(p.code === 0 ? 'ok' : 'bad', (after && after.message) || (p.code === 0 ? 'Done.' : 'It didn\u2019t finish. The log above says why.'));
      } else {
        show('wait', r.message);
        while (document.body.contains(c)) {
          await sleep(1500);
          const p = await api('addons_poll', a.id);
          if (!p) continue;
          if (p.state === 'waiting') { if (p.message) res.querySelector('.tx').textContent = p.message; continue; }
          show(p.state === 'done' ? 'ok' : 'bad', p.message);
          if (p.addon) paint(c, p.addon);
          break;
        }
      }
      stop.classList.add('hidden');
      busy(false);
    };

    paint(c, a);
    return c;
  }

  /* Header pill, switch and the plain-words status line, from an entry. */
  function paint(c, a) {
    c._a = a;
    const [cls, word] = PILL[a.status] || PILL.not_connected;
    const pill = c.querySelector('.ao-pill');
    pill.className = 'ao-pill ' + cls;
    pill.innerHTML = `<span class="dot ${cls === 'idle' ? '' : cls}"></span>${E(word)}`;
    c.querySelector('.ao-switch input').checked = !!a.enabled;
    c.classList.toggle('off', a.status === 'off');
    const st = c.querySelector('.ao-state');
    st.textContent = a.message || '';
    st.className = 'ao-state ' + (a.status === 'error' ? 'bad' : a.status === 'connected' ? 'ok' : '');
    (a.fields || []).forEach(f => {
      const box = c.querySelector(`.ao-field[data-key="${f.key}"]`);
      if (!box) return;
      const inp = box.querySelector('input');
      if (f.secret) { inp.value = ''; inp.placeholder = f.set ? 'Saved. Type to replace it.' : (f.placeholder || ''); }
      inp.dataset.orig = f.secret ? '' : (f.value || '');
    });
  }

  /* ---------------------------------------------------------- fields -- */
  function fieldBox(f) {
    const ph = f.secret ? (f.set ? 'Saved. Type to replace it.' : f.placeholder) : (f.fallback ? `Blank uses ${f.fallback}` : f.placeholder);
    const box = el(`<div class="ao-field" data-key="${E(f.key)}">
      <label>${E(f.label)}${f.required ? '' : ' <span class="opt">optional</span>'}</label>
      <input class="tb" ${f.secret ? 'type="password"' : ''} spellcheck="false" autocomplete="off" placeholder="${E(ph || '')}" value="${E(f.secret ? '' : f.value)}">
      ${f.help ? `<div class="hint">${E(f.help)}</div>` : ''}<div class="err hidden"></div></div>`);
    box.querySelector('input').dataset.orig = f.secret ? '' : (f.value || '');
    box.querySelector('input').oninput = () => { box.classList.remove('bad'); box.querySelector('.err').classList.add('hidden'); };
    if (f.link) {
      const b = linkBtn(f.linkLabel || 'Where to get it', f.link);
      b.classList.add('ao-flink');
      box.insertBefore(b, box.querySelector('.err'));
    }
    return box;
  }

  function dirty(c) {
    return [...c.querySelectorAll('.ao-field input')].some(i => i.value.trim() !== (i.dataset.orig || '').trim() && !(i.type === 'password' && !i.value.trim()));
  }

  /* Save the card's fields. null = nothing to save; else {ok, errors, addon}. */
  async function saveFields(c) {
    if (!dirty(c)) return null;
    const vals = {};
    c.querySelectorAll('.ao-field').forEach(box => {
      const i = box.querySelector('input');
      if (i.type === 'password' && !i.value.trim()) return;
      vals[box.dataset.key] = i.value.trim();
    });
    const r = await api('addons_save', c._a.id, vals);
    if (!r) return { ok: false, errors: {} };
    Object.entries(r.errors || {}).forEach(([k, m]) => {
      const box = c.querySelector(`.ao-field[data-key="${k}"]`);
      if (!box) return;
      box.classList.add('bad');
      const e = box.querySelector('.err');
      e.textContent = m; e.classList.remove('hidden');
    });
    return r;
  }

  function linkBtn(label, url) {
    const b = el(`<button class="btn subtle ao-link"><i class="ico">${I.open}</i><span>${E(label)}</span></button>`);
    b.onclick = () => api('setup_open', url);
    return b;
  }

  function copyRow(label, text) {
    const row = el(`<div class="ao-copy"><span class="lb">${E(label)}</span><code></code><button class="btn"><i class="ico">${I.copy}</i><span>Copy</span></button></div>`);
    row.querySelector('code').textContent = text;
    const b = row.querySelector('button');
    b.onclick = async () => {
      let ok = false;
      try { await navigator.clipboard.writeText(text); ok = true; } catch (e) { ok = await api('setup_copy', text); }
      b.querySelector('span').textContent = ok ? 'Copied' : 'Copy failed';
      setTimeout(() => { b.querySelector('span').textContent = 'Copy'; }, 1600);
    };
    return row;
  }

  /* ---------------------------------------------------- job console -- */
  /* Streams a setup_flow Job (setup_poll) and answers its questions
     (setup_input). "ASK: <kind> <question>" lines and prompts mentioning a
     password switch the answer box to hidden typing. */
  function jobConsole() {
    const box = el(`<div class="ao-con hidden"><div class="bar"><span class="dot"></span><span class="ttl"></span></div><pre></pre>
      <div class="in"><input class="tb" placeholder="Type the answer here and press Enter" spellcheck="false" autocomplete="off"><button class="btn">Send</button></div></div>`);
    const pre = box.querySelector('pre'), dot = box.querySelector('.dot');
    const inp = box.querySelector('.in input'), send = box.querySelector('.in .btn');
    let job = null, pos = 0, askKind = '';
    const paint = text => {
      text.split(/(\n)/).forEach(part => {
        if (part === '\n') { pre.appendChild(document.createTextNode('\n')); return; }
        if (!part) return;
        const ask = part.match(/^\s*ASK:\s*(\w+)\s*(.*)$/);
        if (ask) { askKind = ask[1]; part = `? ${ask[2]}`; }
        const sp = document.createElement('span');
        sp.className = ask ? 'r' : /^\s*STEP:/.test(part) ? 's' : /^\s*OK:/.test(part) ? 'o' : /^\s*WARN:/.test(part) ? 'w'
          : /^\s*(ERROR:|error|fatal|Wrong)/i.test(part) ? 'e' : '';
        sp.textContent = part.replace(/^\s*(STEP|OK|WARN|ERROR):\s?/, '');
        pre.appendChild(sp);
      });
      pre.scrollTop = pre.scrollHeight;
    };
    const lock = on => {
      inp.disabled = send.disabled = !on;
      if (on) inp.focus(); else { inp.value = ''; inp.type = 'password'; askKind = ''; }
    };
    const go = async () => {
      if (!job) return;
      const v = inp.value;
      inp.value = '';
      paint(`> ${inp.type === 'password' ? '*'.repeat(Math.min(v.length, 12)) : v}\n`);
      await api('setup_input', job, v);
    };
    send.onclick = go;
    inp.onkeydown = e => { if (e.key === 'Enter') go(); };
    lock(false);
    box.run = (jobId, title) => new Promise(resolve => {
      job = jobId; pos = 0; pre.textContent = '';
      box.querySelector('.ttl').textContent = title || '';
      dot.className = 'dot busy';
      box.classList.remove('hidden');
      lock(true);
      const t = setInterval(async () => {
        if (!document.body.contains(box)) { clearInterval(t); api('setup_cancel', jobId); resolve({ code: 130 }); return; }
        const p = await api('setup_poll', jobId, pos);
        if (!p) return;
        if (p.text) paint(p.text);
        pos = p.pos;
        const tail = pre.textContent.split('\n').filter(Boolean).pop() || '';
        inp.type = (askKind === 'password' || /password/i.test(tail)) ? 'password' : 'text';
        if (p.done) {
          clearInterval(t);
          dot.className = 'dot ' + (p.code === 0 ? 'ok' : 'bad');
          job = null;
          lock(false);
          resolve(p);
        }
      }, 400);
    });
    return box;
  }

  window.Addons = {
    render,
    refresh: () => lists.forEach(S => load(S)),
  };
})();
