/* Setup walkthrough. Opens as a dark curtain over the Control Center and walks
   through everything, start to finish. Backend: App/setup_flow.py, reached
   through call('setup_*'). Every step can be come back to; progress is saved
   in Shared/setup.json, so closing the app (or a restart for WSL) picks up
   where it stopped. */
(function () {
  'use strict';
  const api = (fn, ...a) => window.call(fn, ...a);
  const E = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const el = (html) => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const I = { check: '', info: '', warn: '', bad: '', copy: '', open: '', mac: '',
    phone: '', claude: '', linux: '', time: '', apple: '', key: '', pc: '', mail: '' };

  const TZ = ['America/St_Johns', 'America/Halifax', 'America/Toronto', 'America/New_York', 'America/Winnipeg', 'America/Chicago',
    'America/Regina', 'America/Edmonton', 'America/Denver', 'America/Phoenix', 'America/Vancouver', 'America/Los_Angeles',
    'America/Anchorage', 'Pacific/Honolulu', 'Europe/London', 'Europe/Dublin', 'Europe/Paris', 'Europe/Berlin', 'Europe/Madrid',
    'Europe/Rome', 'Europe/Amsterdam', 'Europe/Stockholm', 'Europe/Athens', 'Asia/Dubai', 'Asia/Kolkata', 'Asia/Singapore',
    'Asia/Tokyo', 'Asia/Seoul', 'Australia/Perth', 'Australia/Brisbane', 'Australia/Sydney', 'Pacific/Auckland'];

  const S = { st: null, i: 0, root: null, ok: {}, polls: new Set() };

  /* ------------------------------------------------------- the steps -- */
  const STEPS = [
    { id: 'welcome', t: 'Welcome', c: 'What you need', render: welcome, ready: () => true },
    { id: 'pc', t: 'This PC', c: 'Quick checks', render: pc, ready: () => S.ok.pc },
    { id: 'you', t: 'About you', c: 'Name, number, city', render: you, ready: () => S.ok.you },
    { id: 'uses', t: 'Your Atlas', c: 'What you want it for', render: uses, ready: () => S.ok.uses },
    { id: 'tailor', t: 'Make it yours', c: 'Brief, news, goals', render: tailor, ready: () => true },
    { id: 'claude', t: 'Claude', c: 'The brain', render: claude, ready: () => S.ok.claude },
    { id: 'wsl', t: 'Linux', c: 'Where the bridge runs', render: wsl, ready: () => S.ok.wsl },
    { id: 'bridge', t: 'iMessage bridge', c: 'Get it ready', render: bridge, ready: () => S.ok.bridge },
    { id: 'mac', t: 'Mac identity', c: 'From your MacBook', render: mac, ready: () => S.ok.mac },
    { id: 'apple', t: 'Bot Apple ID', c: 'Sign in once', render: apple, ready: () => S.ok.apple },
    { id: 'connect', t: 'Connect', c: 'Bots meet the bridge', render: connect, ready: () => S.ok.connect },
    { id: 'extras', t: 'Add-ons', c: 'Calendar, email, music, more', render: extras, ready: () => true, optional: true },
    { id: 'finish', t: 'Finish', c: 'Say hi', render: finish, ready: () => S.ok.finish },
  ];

  /* ------------------------------------------------------------ shell -- */
  async function open() {
    S.st = await api('setup_state');
    if (!S.st) return;
    if (!S.root) build();
    const steps = S.st.steps || {};
    STEPS.forEach(s => { if (steps[s.id] && steps[s.id].status === 'done') S.ok[s.id] = true; });
    const first = STEPS.findIndex(s => !(steps[s.id] && ['done', 'skipped'].includes(steps[s.id].status)));
    S.i = first < 0 ? STEPS.length - 1 : first;
    S.root.classList.remove('gone');
    requestAnimationFrame(() => S.root.classList.add('on'));
    go(S.i);
  }

  function close() {
    S.root.classList.remove('on');
    setTimeout(() => S.root.classList.add('gone'), 220);
  }

  function build() {
    S.root = el(`<div id="suCurtain" class="gone"><div class="su-win">
      <div class="su-rail">
        <div class="su-brand"><div class="su-mark">A</div><div><b>Set up Atlas</b><span>Atlas and Iris on your iPhone</span></div></div>
        <div class="su-steps"></div>
        <div class="su-railfoot">
          <div class="su-bar"><i style="width:0"></i></div>
          <div class="cap su-prog"></div>
          ${S.st.dry ? '<span class="su-dry">DRY RUN: nothing gets installed</span>' : ''}
          <button class="btn subtle su-later" style="justify-content:flex-start;padding-left:4px"><i class="ico"></i>Finish later</button>
        </div>
      </div>
      <div class="su-main">
        <div class="su-head"></div><div class="su-body"></div>
        <div class="su-foot"><button class="btn su-back">Back</button><span class="msg"></span><span class="spacer"></span>
          <button class="btn su-skip hidden">Skip</button><button class="btn accent su-next">Continue</button></div>
      </div></div></div>`);
    document.body.appendChild(S.root);
    S.root.querySelector('.su-later').onclick = close;
    S.root.querySelector('.su-back').onclick = () => go(S.i - 1);
    S.root.querySelector('.su-next').onclick = next;
    S.root.querySelector('.su-skip').onclick = async () => { await api('setup_mark', STEPS[S.i].id, 'skipped'); stepState(STEPS[S.i].id, 'skipped'); go(S.i + 1); };
  }

  function stepState(id, status) { S.st.steps = S.st.steps || {}; S.st.steps[id] = { status }; }

  function rail() {
    const box = S.root.querySelector('.su-steps');
    box.innerHTML = '';
    const steps = S.st.steps || {};
    STEPS.forEach((s, n) => {
      const st = (steps[s.id] || {}).status;
      const reach = n <= S.i || STEPS.slice(0, n).every(p => p.optional || ['done', 'skipped'].includes((steps[p.id] || {}).status));
      const b = el(`<button class="su-step ${n === S.i ? 'on' : ''} ${st || ''}" ${reach ? '' : 'disabled'}>
        <span class="su-num">${st === 'done' ? I.check : n + 1}</span><span><span class="t">${E(s.t)}</span><span class="c">${E(s.c)}</span></span></button>`);
      b.onclick = () => go(n);
      box.appendChild(b);
    });
    const cur = box.querySelector('.su-step.on');
    if (cur) cur.scrollIntoView({ block: 'nearest' });
    const done = STEPS.filter(s => ['done', 'skipped'].includes((steps[s.id] || {}).status)).length;
    S.root.querySelector('.su-bar i').style.width = `${Math.round(done / STEPS.length * 100)}%`;
    S.root.querySelector('.su-prog').textContent = `Step ${S.i + 1} of ${STEPS.length}`;
  }

  function go(n) {
    if (n < 0 || n >= STEPS.length) return;
    S.polls.forEach(clearInterval); S.polls.clear();
    S.i = n;
    const s = STEPS[n];
    rail();
    const head = S.root.querySelector('.su-head'), body = S.root.querySelector('.su-body');
    head.innerHTML = ''; body.innerHTML = '';
    body.scrollTop = 0;
    S.root.querySelector('.su-back').disabled = n === 0;
    S.root.querySelector('.su-skip').classList.toggle('hidden', !s.optional);
    S.root.querySelector('.su-next').textContent = n === STEPS.length - 1 ? 'Done' : 'Continue';
    msg('');
    s.render(head, body);
    head.classList.remove('su-anim'); body.classList.remove('su-anim');
    void body.offsetWidth;
    head.classList.add('su-anim'); body.classList.add('su-anim');
    refresh();
  }

  function refresh() {
    const s = STEPS[S.i];
    S.root.querySelector('.su-next').disabled = !s.ready();
  }

  async function next() {
    const s = STEPS[S.i];
    if (s.beforeNext && !(await s.beforeNext())) return;
    if (!s.ready()) return;
    await api('setup_mark', s.id, 'done');
    stepState(s.id, 'done');
    S.ok[s.id] = true;
    if (S.i === STEPS.length - 1) { close(); window.toast && toast('Setup is done. Atlas and Iris are live.'); return; }
    go(S.i + 1);
  }

  function msg(t, bad) {
    const m = S.root.querySelector('.su-foot .msg');
    m.textContent = t || '';
    m.style.color = bad ? 'var(--bad)' : '';
  }

  function header(head, eyebrow, title, text) {
    if (/^Step \d+$/.test(eyebrow)) eyebrow = `Step ${S.i + 1}`;  // steps get added; number them by position
    head.innerHTML = `<div class="su-eyebrow">${E(eyebrow)}</div><h1>${E(title)}</h1>${text ? `<p>${text}</p>` : ''}`;
  }

  /* --------------------------------------------------------- pieces -- */
  function info(kind, html, icon) {
    return el(`<div class="su-info ${kind || ''}"><i class="ico">${icon || (kind === 'warn' ? I.warn : kind === 'bad' ? I.bad : kind === 'ok' ? I.check : I.info)}</i><div>${html}</div></div>`);
  }

  function copyBox(text, label) {
    const box = el(`<div class="su-copy"><code></code><button class="btn"><i class="ico">${I.copy}</i>${E(label || 'Copy')}</button></div>`);
    box.querySelector('code').textContent = text;
    box.querySelector('button').onclick = async () => {
      let ok = false;
      try { await navigator.clipboard.writeText(text); ok = true; } catch (e) { ok = await api('setup_copy', text); }
      const b = box.querySelector('button');
      b.innerHTML = `<i class="ico">${I.check}</i>${ok ? 'Copied' : 'Copy failed'}`;
      setTimeout(() => { b.innerHTML = `<i class="ico">${I.copy}</i>${E(label || 'Copy')}`; }, 1600);
    };
    return box;
  }

  function field(key, label, opts = {}) {
    const v = (S.st.env || {})[key] || opts.value || '';
    const set = (S.st.secretsSet || {})[key];
    const f = el(`<div class="su-field ${opts.full ? 'full' : ''}" data-key="${key}">
      <label>${E(label)} ${opts.optional ? '<span class="opt">optional</span>' : ''}</label>
      ${opts.options ? `<select>${opts.options.map(o => `<option ${o === v ? 'selected' : ''}>${E(o)}</option>`).join('')}</select>`
        : `<input class="tb" ${opts.secret ? 'type="password"' : ''} spellcheck="false" placeholder="${E(set ? 'Saved. Type to replace it.' : (opts.ph || ''))}" value="${E(opts.secret ? '' : v)}">`}
      ${opts.hint ? `<div class="hint">${opts.hint}</div>` : ''}<div class="err hidden"></div></div>`);
    clearOnEdit(f);
    return f;
  }

  // An error under a field goes away as soon as the field is changed.
  function clearOnEdit(f) {
    const clear = () => { f.classList.remove('bad'); const e = f.querySelector('.err'); if (e) e.classList.add('hidden'); };
    f.addEventListener('input', clear);
    f.addEventListener('change', clear);
  }

  async function saveFields(box, required = [], extra = {}) {
    const vals = {};
    box.querySelectorAll('.su-field').forEach(f => {
      const inp = f.querySelector('input,select,textarea');
      if (!inp || !f.dataset.key || inp.disabled) return;   // a label-only block, or a field that's switched off
      const k = f.dataset.key, v = inp.value.trim();
      const secret = inp.type === 'password';
      if (secret && !v) return;          // blank secret = keep what's saved
      vals[k] = v;
      f.classList.remove('bad'); const e = f.querySelector('.err'); if (e) e.classList.add('hidden');
    });
    Object.assign(vals, extra);
    for (const k of required) if (!vals[k] && !(S.st.secretsSet || {})[k] && !((S.st.env || {})[k] && !(k in vals))) {
      const f = box.querySelector(`[data-key="${k}"]`);
      f.classList.add('bad'); const e = f.querySelector('.err'); e.textContent = f.dataset.need || 'This one is needed.'; e.classList.remove('hidden');
      return false;
    }
    const r = await api('setup_save', vals);
    if (!r || !r.ok) {
      Object.entries((r && r.errors) || {}).forEach(([k, m]) => {
        const f = box.querySelector(`[data-key="${k}"]`);
        if (!f) { msg(m, true); return; }
        f.classList.add('bad'); const e = f.querySelector('.err'); e.textContent = m; e.classList.remove('hidden');
      });
      return false;
    }
    S.st = await api('setup_state');
    return true;
  }

  function checks(list) {
    const box = el('<div class="su-checks"></div>');
    list.forEach(c => {
      const row = el(`<div class="su-check" data-id="${E(c.id)}"><span class="st ${c.ok === true ? 'ok' : c.ok === false ? 'bad' : 'wait'}">${c.ok === true ? I.check : c.ok === false ? '' : ''}</span>
        <div class="tx"><b>${E(c.label)}</b><span>${E(c.detail || '')}</span></div></div>`);
      if (c.fix) {
        const b = el(`<button class="btn">Fix it</button>`);
        b.onclick = async () => {
          b.disabled = true;
          const r = await api('setup_fix', c.fix);
          msg(r && r.message, !(r && r.ok));
          if (c.onFixed) c.onFixed();
        };
        row.appendChild(b);
      }
      if (c.action) row.appendChild(c.action);
      box.appendChild(row);
    });
    return box;
  }

  /* A console that runs one job and streams its output. */
  function consoleBox(title, opts = {}) {
    const box = el(`<div class="su-con"><div class="bar"><span class="dot"></span><span class="ttl">${E(title)}</span><span class="spacer"></span>
      <button class="btn hidden stop">Stop</button></div><pre></pre>
      ${opts.input ? `<div class="in"><input class="tb" placeholder="Type your answer here and press Enter" spellcheck="false"><button class="btn">Send</button></div>` : ''}</div>`);
    const pre = box.querySelector('pre'), dot = box.querySelector('.dot'), stop = box.querySelector('.stop');
    let job = null, pos = 0, askKind = '';
    const paint = text => {
      if (opts.onText) opts.onText(text);
      text.split(/(\n)/).forEach(part => {
        if (part === '\n') { pre.appendChild(document.createTextNode('\n')); return; }
        if (!part) return;
        // login.sh asks with "ASK: <kind> <question>"; bridge output comes as "LOG: ...".
        const ask = part.match(/^\s*ASK:\s*(\w+)\s*(.*)$/), opt = part.match(/^\s*ASK_OPTION:\s*(.*)$/);
        if (ask) { askKind = ask[1]; part = `? ${ask[2]}`; }
        else if (opt) part = `    ${opt[1]}`;
        else part = part.replace(/^\s*LOG:\s?/, '');
        const sp = document.createElement('span');
        if (ask || opt) { sp.className = 'r'; sp.textContent = part; pre.appendChild(sp); return; }
        sp.className = /^\s*STEP:/.test(part) ? 's' : /^\s*OK:/.test(part) ? 'o' : /^\s*WARN:/.test(part) ? 'w'
          : /^\s*(ERROR:|error|fatal)/i.test(part) ? 'e' : /^\s*RESULT:/.test(part) ? 'r' : '';
        sp.textContent = part;
        pre.appendChild(sp);
      });
      pre.scrollTop = pre.scrollHeight;
    };
    if (opts.input) {
      const inp = box.querySelector('.in input'), send = box.querySelector('.in .btn');
      const go = async () => {
        if (!job) return;
        const v = inp.value;
        inp.value = '';
        paint(`> ${inp.type === 'password' ? '*'.repeat(Math.min(v.length, 12)) : v}\n`);
        await api('setup_input', job, v);
      };
      send.onclick = go;
      inp.onkeydown = e => { if (e.key === 'Enter') go(); };
      inp.disabled = send.disabled = true;
    }
    const lock = on => {
      if (!opts.input) return;
      const inp = box.querySelector('.in input');
      inp.disabled = box.querySelector('.in .btn').disabled = !on;
      if (on) inp.focus();
      else { inp.value = ''; inp.type = 'password'; askKind = ''; }  // never leave a typed password sitting in the box
    };
    box.run = (action, params) => new Promise(async resolve => {
      pre.textContent = ''; pos = 0;
      dot.className = 'dot busy';
      if (!opts.keepHidden) {
        box.classList.remove('hidden');
        setTimeout(() => box.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 50);
      }
      // A job someone else already started (the add-ons page) is followed, not started.
      const r = action && action.follow ? { job: action.follow } : await api('setup_run', action, params || {});
      if (!r || r.error || !r.job) {
        dot.className = 'dot bad';
        paint(`ERROR: ${(r && r.error) || 'It did not start.'}\n`);
        resolve({ code: 1, error: r && r.error });
        return;
      }
      job = r.job;
      lock(true);
      stop.classList.remove('hidden');
      stop.onclick = () => api('setup_cancel', job);
      const t = setInterval(async () => {
        const p = await api('setup_poll', job, pos);
        if (!p) return;
        if (p.text) paint(p.text);
        pos = p.pos;
        if (opts.input) {
          const tail = pre.textContent.split('\n').filter(Boolean).pop() || '';
          box.querySelector('.in input').type = (askKind === 'password' || /password/i.test(tail)) ? 'password' : 'text';
        }
        if (p.done) {
          clearInterval(t); S.polls.delete(t);
          dot.className = 'dot ' + (p.code === 0 ? 'ok' : 'bad');
          stop.classList.add('hidden');
          job = null;
          lock(false);
          resolve(p);
        }
      }, 400);
      S.polls.add(t);
    });
    // Answer the running job's question from code (askBox does this).
    box.send = async (v, secret) => {
      if (!job) return false;
      paint(`> ${secret ? '********' : v}\n`);
      await api('setup_input', job, v);
      return true;
    };
    return box;
  }

  /* A job that asks questions (the Apple sign-in, the iCloud sign-in) shown as
     a real form: what it's doing now, the question as proper fields (password
     box, six boxes for a code, buttons for a choice), and the raw log behind
     "Show details". `answers` fills questions in without asking: a value or a
     function per ASK kind (apple_id, password, code, choice, text). */
  function askBox(title, opts = {}) {
    const box = el(`<div class="su-ask hidden"><div class="now"><span class="spin"></span><span class="tx">Starting...</span></div>
      <div class="q"></div><div class="su-details"><button type="button">Show details</button></div></div>`);
    box.addEventListener('click', e => e.stopPropagation());
    const now = box.querySelector('.now'), q = box.querySelector('.q'), det = box.querySelector('.su-details');
    let buf = '', options = [], lastError = '', running = false;
    const con = consoleBox(title, {
      keepHidden: true,
      onText: text => {
        buf += text;
        const lines = buf.split('\n'); buf = lines.pop();
        lines.forEach(line);
      }
    });
    con.classList.add('hidden');
    det.appendChild(con);
    det.querySelector('button').onclick = () => {
      const show = con.classList.toggle('hidden');
      det.querySelector('button').textContent = show ? 'Show details' : 'Hide details';
    };
    const status = (text, kind) => {
      now.className = 'now ' + (kind || '');
      now.innerHTML = kind ? '' : '<span class="spin"></span>';
      now.appendChild(el(`<span class="tx">${E(text)}</span>`));
    };
    const plain = s => s.replace(/^\(dry run\)\s*/, '');
    const send = async (v, secret) => { q.innerHTML = ''; status(secret ? 'Checking...' : 'Sending...'); await con.send(v, secret); };
    const ask = (kind, text) => {
      const auto = opts.answers && opts.answers[kind];
      const v = typeof auto === 'function' ? auto() : auto;
      if (v) { send(v, kind === 'password'); return; }
      q.innerHTML = '';
      status('Waiting for you');
      if (kind === 'code') {
        q.appendChild(el(`<div><h3>Enter the code</h3><p>${E(opts.codeHint || 'Apple just sent a 6-digit code to your iPhone or the trusted phone number. Type it here.')}</p></div>`));
        const row = el('<div class="su-code"></div>');
        const boxes = [];
        for (let n = 0; n < 6; n++) {
          if (n === 3) row.appendChild(el('<span class="gap"></span>'));
          const b = el('<input inputmode="numeric" maxlength="1" autocomplete="one-time-code">');
          boxes.push(b); row.appendChild(b);
        }
        const value = () => boxes.map(b => b.value).join('');
        const done = () => { if (/^\d{6}$/.test(value())) send(value(), false); };
        boxes.forEach((b, n) => {
          b.addEventListener('input', () => {
            const d = b.value.replace(/\D/g, '');
            if (d.length > 1) {   // a pasted code
              d.slice(0, 6 - n).split('').forEach((c, k) => { boxes[n + k].value = c; });
              boxes[Math.min(n + d.length, 5)].focus();
            } else {
              b.value = d;
              if (d && n < 5) boxes[n + 1].focus();
            }
            done();
          });
          b.addEventListener('keydown', e => { if (e.key === 'Backspace' && !b.value && n > 0) { boxes[n - 1].focus(); boxes[n - 1].value = ''; } });
        });
        q.appendChild(row);
        setTimeout(() => boxes[0].focus(), 30);
        return;
      }
      if (kind === 'choice') {
        q.appendChild(el(`<div><h3>${E(text.replace(/\s*\(answer with the number\)\s*$/, ''))}</h3></div>`));
        const list = el('<div class="opts"></div>');
        options.forEach(o => {
          const m = o.match(/^(\d+)\)\s*(.*)$/);
          const b = el(`<button class="btn">${E(m ? m[2] : o)}</button>`);
          b.onclick = () => send(m ? m[1] : o, false);
          list.appendChild(b);
        });
        options = [];
        q.appendChild(list);
        return;
      }
      const secret = kind === 'password';
      q.appendChild(el(`<div><h3>${E(text)}</h3></div>`));
      const row = el(`<div class="row">${secret ? '<span class="su-pw"><input class="tb" type="password" spellcheck="false"><button type="button" class="eye" title="Show"></button></span>' : '<input class="tb" spellcheck="false">'}<button class="btn accent">Continue</button></div>`);
      const inp = row.querySelector('input');
      const go = () => { if (inp.value.trim()) { const v = inp.value; inp.value = ''; send(v, secret); } };
      row.querySelector('.btn.accent').onclick = go;
      inp.onkeydown = e => { if (e.key === 'Enter') go(); };
      const eye = row.querySelector('.eye');
      if (eye) { eye.textContent = ''; eye.onclick = () => { inp.type = inp.type === 'password' ? 'text' : 'password'; }; }
      q.appendChild(row);
      setTimeout(() => inp.focus(), 30);
    };
    const line = raw => {
      const s = raw.trim();
      if (!s || s.startsWith('> ')) return;
      let m;
      if ((m = s.match(/^ASK_OPTION:\s*(.*)$/))) options.push(m[1]);
      else if ((m = s.match(/^ASK:\s*(\w+)\s*(.*)$/))) ask(m[1], m[2]);
      else if ((m = s.match(/^STEP:\s*(.*)$/))) status(plain(m[1]));
      else if ((m = s.match(/^ERROR:\s*(.*)$/))) { lastError = plain(m[1]); status(lastError, 'bad'); }
      else if ((m = s.match(/^WARN:\s*(.*)$/)) && running) status(plain(m[1]));
    };
    box.follow = jobId => box.run({ follow: jobId });
    box.run = async (action, params) => {
      box.classList.remove('hidden');
      q.innerHTML = ''; options = []; lastError = ''; buf = ''; running = true;
      status('Starting...');
      const p = await con.run(action, params);
      running = false;
      q.innerHTML = '';
      if (p.code === 0) status(opts.doneText || 'Done.', 'ok');
      else status(friendly(lastError || p.error || 'It stopped without saying why. Show details has the log.'), 'bad');
      return p;
    };
    return box;
  }

  // Apple's errors in plain words. Anything not listed is shown as it came.
  function friendly(err) {
    const e = String(err || '');
    if (/-20101|incorrect|wrong password|invalid credentials/i.test(e)) return 'Wrong Apple ID or password. Check them, then try again in 15 minutes.';
    if (/503|throttl|too many|-22405|-22421|rate/i.test(e)) return 'Apple is making this account wait after too many tries. Leave it for a few hours before trying again.';
    if (/-21669|verification code|code.*(wrong|incorrect|invalid)/i.test(e)) return 'That code didn’t work. Wait 15 minutes, then sign in again to get a new one.';
    return e;
  }

  /* ------------------------------------------------------- 1 welcome -- */
  function welcome(head, body) {
    header(head, 'Step 1', 'Let’s set up Atlas',
      'Atlas is your personal assistant and Iris texts the people you know for you. Both live in iMessage on your iPhone and run on this PC. This walks through everything. Most of it is pressing one button and waiting.');
    const g = el('<div class="su-grid"></div>');
    [[I.mac, 'An Intel MacBook you own', 'It doesn’t have to work well, it just has to turn on. Its identity is what lets the bots use iMessage.'],
     [I.apple, 'A spare Apple ID', 'Not your main one. Best is an older one that’s been used on an iPhone. A brand new one can take days before it can send.'],
     [I.phone, 'Your iPhone', 'For the texts, and for the 2FA code when the bot’s Apple ID signs in.'],
     [I.claude, 'A Claude Pro or Max plan', 'Your own. Every reply the bots write uses it.'],
     [I.pc, 'This PC stays on', 'The bots live here. If the PC sleeps, they sleep.'],
     [I.time, 'About an hour', 'Most of it is the bridge building by itself. You can leave and come back.']]
      .forEach(([ic, b, s]) => g.appendChild(el(`<div class="su-card su-need"><i class="ico">${ic}</i><div><b>${E(b)}</b><span>${E(s)}</span></div></div>`)));
    body.appendChild(g);
    body.appendChild(info('warn', '<b>One real risk.</b> This uses your MacBook’s identity to talk to Apple. If Apple ever flags it, that Mac can lose iMessage and FaceTime and the bot’s Apple ID can get blocked. It’s rare when you go slow, and this setup goes slow on purpose.'));
  }

  /* ------------------------------------------------------------ 2 pc -- */
  function pc(head, body) {
    header(head, 'Step 2', 'Check this PC', 'Making sure it can run everything. Anything red has a fix next to it.');
    const slot = el('<div></div>');
    body.appendChild(slot);
    const load = async () => {
      slot.innerHTML = '';
      slot.appendChild(checks([{ id: 'x', label: 'Checking...', ok: null }]));
      const list = await api('setup_check', 'pc') || [];
      list.forEach(c => { if (c.fix) c.onFixed = load; });
      slot.innerHTML = '';
      slot.appendChild(checks(list));
      S.ok.pc = S.st.dry || (list.length && list.filter(c => c.id !== 'sleep').every(c => c.ok));
      if (list.some(c => c.id === 'disk' && !c.ok))
        slot.appendChild(info('bad', `Setup needs about ${S.st.bridgeRelease ? 8 : 25} GB. Empty the Recycle Bin, uninstall games or apps you don’t use, or move big folders to another drive, then press Check again.`));
      if (list.some(c => c.id === 'virt' && !c.ok))
        slot.appendChild(info('bad', 'Virtualization is off. Restart the PC into the BIOS (usually Delete or F2 at startup), turn on <b>Intel VT-x</b> or <b>AMD SVM</b>, save, and open Atlas again.'));
      if (list.some(c => c.id === 'sleep' && !c.ok))
        slot.appendChild(info('warn', 'If the PC sleeps, the bots stop answering. Press <b>Fix it</b> to keep it awake while it’s plugged in. The screen can still turn off.'));
      const again = el('<button class="btn" style="margin-top:12px">Check again</button>');
      again.onclick = load;
      slot.appendChild(again);
      refresh();
    };
    load();
  }

  /* ----------------------------------------------------------- 3 you -- */
  function you(head, body) {
    header(head, 'Step 3', 'About you', 'The bots use this to know who you are. Only your number can give them orders.');
    const env = S.st.env || {};
    const tzGuess = env.OWNER_TIMEZONE || S.st.windowsTzIana || 'America/Toronto';
    const form = el('<div class="su-form"></div>');
    form.appendChild(field('OWNER_NAME', 'First name', { ph: 'Sam' }));

    // How they text: a phone number, or the Apple ID email Messages sends from
    // (an iPad, an iPod, a phone with no plan).
    const how = el(`<div class="su-field" data-how><label>You text from</label>
      <div class="su-seg"><button type="button" data-m="phone">A phone number</button><button type="button" data-m="email">An email</button></div>
      <div class="hint">No phone number on your iPhone or iPad? Pick email and use the Apple ID email that Messages sends from.</div></div>`);
    const phone = field('OWNER_PHONE', 'Your phone number', { ph: '403 555 0123', hint: 'The number you text from. Anyone else who texts the bots gets treated as a stranger.' });
    const mail = field('OWNER_IMESSAGE', 'The email you iMessage from', { ph: 'you@icloud.com', value: (env.OWNER_IMESSAGE || '').split(',')[0],
      hint: 'On the iPhone or iPad: Settings, then Apps, then Messages, then Send and Receive. Use the email ticked under "You can be reached by iMessage at". Anyone else who texts the bots gets treated as a stranger.' });
    mail.dataset.need = 'Put the email you text from.';
    let mode = env.OWNER_IMESSAGE && !env.OWNER_PHONE ? 'email' : 'phone';
    const setMode = m => {
      mode = m;
      how.querySelectorAll('[data-m]').forEach(b => b.classList.toggle('on', b.dataset.m === m));
      phone.classList.toggle('hidden', m !== 'phone'); phone.querySelector('input').disabled = m !== 'phone';
      mail.classList.toggle('hidden', m !== 'email'); mail.querySelector('input').disabled = m !== 'email';
    };
    how.querySelectorAll('[data-m]').forEach(b => b.onclick = () => setMode(b.dataset.m));
    form.append(how, phone, mail);
    setMode(mode);

    form.appendChild(field('OWNER_EMAIL', 'Your email', { ph: 'you@gmail.com', optional: true, hint: 'For your calendar and email, if you connect them later.' }));
    const tz = field('OWNER_TIMEZONE', 'Time zone', { options: TZ.includes(tzGuess) ? TZ : [tzGuess, ...TZ], value: tzGuess,
      hint: S.st.windowsTz ? `Windows says ${E(S.st.windowsTz)}. Make sure this PC's clock is set to your time zone too.` : '' });
    let tzTouched = !!env.OWNER_TIMEZONE;
    tz.querySelector('select').addEventListener('change', () => { tzTouched = true; });
    form.appendChild(cityPicker(c => {
      if (tzTouched || !c.timezone) return;
      const sel = tz.querySelector('select');
      if (![...sel.options].some(o => o.value === c.timezone)) sel.insertBefore(new Option(c.timezone, c.timezone), sel.firstChild);
      sel.value = c.timezone;
    }));
    form.appendChild(tz);
    body.appendChild(form);
    STEPS[2].beforeNext = async () => {
      const city = form.querySelector('[data-key="OWNER_CITY"]');
      if (!city.dataset.picked) {
        city.classList.add('bad'); const e = city.querySelector('.err');
        e.textContent = city.querySelector('input').value.trim() ? 'Pick your city from the list as you type.' : 'This one is needed.';
        e.classList.remove('hidden');
        return false;
      }
      const ok = mode === 'phone'
        ? await saveFields(form, ['OWNER_NAME', 'OWNER_PHONE', 'OWNER_TIMEZONE'], { OWNER_IMESSAGE: '' })
        : await saveFields(form, ['OWNER_NAME', 'OWNER_IMESSAGE', 'OWNER_TIMEZONE'], { OWNER_PHONE: '' });
      S.ok.you = ok;
      if (ok) msg('Saved.');
      return ok;
    };
    S.ok.you = true; // Continue saves and checks the form
    refresh();
  }

  /* City: type a few letters, pick from the list. Picking also gives the
     weather real coordinates and suggests the time zone. */
  function cityPicker(onPick) {
    const env = S.st.env || {};
    const f = el(`<div class="su-field su-city" data-key="OWNER_CITY"><label>City</label>
      <div class="su-combo"><input class="tb" spellcheck="false" autocomplete="off" placeholder="Start typing your city"><div class="su-drop hidden"></div></div>
      <div class="hint">For the weather and local news in your morning brief.</div><div class="err hidden"></div>
      <div class="su-field hidden" data-key="OWNER_LAT"><input><div class="err"></div></div>
      <div class="su-field hidden" data-key="OWNER_LON"><input><div class="err"></div></div></div>`);
    const inp = f.querySelector('.su-combo input'), drop = f.querySelector('.su-drop');
    const lat = f.querySelector('[data-key="OWNER_LAT"] input'), lon = f.querySelector('[data-key="OWNER_LON"] input');
    inp.value = env.OWNER_CITY || '';
    lat.value = env.OWNER_LAT || ''; lon.value = env.OWNER_LON || '';
    if (env.OWNER_CITY) f.dataset.picked = '1';
    clearOnEdit(f);
    let timer = 0, seq = 0, items = [], hi = -1;
    const shut = () => { drop.classList.add('hidden'); hi = -1; };
    const pick = c => {
      inp.value = c.label; lat.value = c.lat ?? ''; lon.value = c.lon ?? '';
      f.dataset.picked = '1'; shut(); onPick && onPick(c);
    };
    const paint = () => {
      drop.innerHTML = '';
      if (!items.length) { shut(); return; }
      items.forEach((c, n) => {
        if (c.error) { drop.appendChild(el(`<div class="su-opt off">${E(c.error)}</div>`)); return; }
        const o = el(`<button type="button" class="su-opt ${n === hi ? 'on' : ''}"><b>${E(c.name)}</b><span>${E([c.region, c.country].filter(Boolean).join(', '))}</span></button>`);
        o.onmousedown = e => { e.preventDefault(); pick(c); };
        drop.appendChild(o);
      });
      drop.classList.remove('hidden');
    };
    inp.addEventListener('input', () => {
      delete f.dataset.picked; lat.value = lon.value = '';
      clearTimeout(timer);
      const q = inp.value.trim(), me = ++seq;
      if (q.length < 2) { items = []; paint(); return; }
      timer = setTimeout(async () => {
        const r = await api('setup_city_search', q);
        if (me !== seq) return;
        items = (r || []).length ? r : [{ error: 'No city by that name. Try the spelling, or a bigger city nearby.' }];
        hi = items[0] && !items[0].error ? 0 : -1;
        paint();
      }, 250);
    });
    inp.addEventListener('keydown', e => {
      const real = items.filter(c => !c.error);
      if (drop.classList.contains('hidden') || !real.length) return;
      if (e.key === 'ArrowDown') { hi = Math.min(hi + 1, real.length - 1); paint(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { hi = Math.max(hi - 1, 0); paint(); e.preventDefault(); }
      else if (e.key === 'Enter' && hi >= 0) { pick(real[hi]); e.preventDefault(); }
      else if (e.key === 'Escape') shut();
    });
    inp.addEventListener('blur', () => setTimeout(shut, 120));
    return f;
  }

  /* ------------------------------------------------------ your atlas -- */
  const USES = [
    ['brief', 'A daily brief', 'Weather, your day and the news you care about, every morning.'],
    ['track', 'Keeping me on track', 'Reminders, deadlines, and following up until it’s actually done.'],
    ['goals', 'Goals and habits', 'Check-ins on what you’re working toward and the habits you’re building.'],
    ['schedule', 'My schedule', 'Your calendar, plans, and “am I free Thursday”.'],
    ['email', 'Email', 'A heads-up on the emails that matter, and help writing replies.'],
    ['remember', 'Remembering stuff', 'Send it links, notes and ideas. Ask for them later.'],
    ['iris', 'Texting people for me', 'Iris texts and emails the people you know, for you.'],
  ];
  const picked = () => ((S.st.env || {}).OWNER_WANTS || '').split(',').filter(Boolean);

  function uses(head, body) {
    header(head, 'Step 4', 'What do you want Atlas for?',
      'Pick everything that fits. It decides what Atlas does on its own and which questions come next. You can change it later.');
    const on = new Set(picked());
    const grid = el('<div class="su-uses"></div>');
    USES.forEach(([id, t, c]) => {
      const b = el(`<button type="button" class="su-use ${on.has(id) ? 'on' : ''}"><b>${E(t)}</b><span>${E(c)}</span></button>`);
      b.onclick = () => {
        on.has(id) ? on.delete(id) : on.add(id);
        b.classList.toggle('on', on.has(id));
        S.ok.uses = on.size > 0; msg(''); refresh();
      };
      grid.appendChild(b);
    });
    body.appendChild(grid);
    S.ok.uses = on.size > 0;
    STEPS[S.i].beforeNext = async () => {
      if (!on.size) { msg('Pick at least one.', true); return false; }
      const order = USES.map(u => u[0]).filter(id => on.has(id));
      const r = await api('setup_save', { OWNER_WANTS: order.join(',') });
      if (!r || !r.ok) { msg('Couldn’t save that.', true); return false; }
      S.st = await api('setup_state');
      return true;
    };
  }

  /* ---------------------------------------------------- make it yours -- */
  const INTERESTS = [
    ['sports', 'Sports', 'teams'], ['tech', 'Tech'], ['ai', 'AI'], ['gaming', 'Gaming', 'Which games?'],
    ['music', 'Music', 'Which artists or genres?'], ['tv', 'Movies and TV', 'Which shows or movies?'],
    ['markets', 'Business and markets', 'Any stocks you follow?'], ['crypto', 'Crypto', 'Which coins?'],
    ['science', 'Science and space'], ['cars', 'Cars'], ['politics', 'Politics'], ['fashion', 'Fashion'],
    ['food', 'Food'], ['fitness', 'Health and fitness'],
  ];
  const BEAT_TEXT = {
    tech: 'the biggest real tech story: major launches, phones and apps they use. Not a niche paper or a funding round.',
    ai: 'new AI models and tools, and what changed this week that they could actually use.',
    gaming: 'big releases, updates and gaming news', music: 'new releases, tours and music news',
    tv: 'new releases, trailers and what everyone is watching', markets: 'the market move that matters today and why',
    crypto: 'the crypto move that matters today and why', science: 'the biggest science or space story',
    cars: 'the biggest car story: launches, reviews, the industry', politics: 'the political story that actually affects them',
    fashion: 'the fashion story people will be talking about', food: 'food news, openings and trends worth knowing',
    fitness: 'health and fitness news backed by real research, not fads',
  };
  // The leagues the Watches page can follow (Atlas/watch.py); the brief can cover any team.
  const LEAGUES = ['NHL', 'NBA', 'WNBA', 'NFL', 'CFL', 'MLB', 'MLS', 'Premier League', 'La Liga', 'Bundesliga', 'Serie A', 'Ligue 1', 'Champions League', 'Formula 1'];
  const TIMES = [['random 09:00-10:00', 'Sometime between 9 and 10am']].concat(
    Array.from({ length: 13 }, (_, k) => { const m = 360 + k * 30, h = Math.floor(m / 60), mm = m % 60;
      return [`${String(h).padStart(2, '0')}:${String(mm).padStart(2, '0')}`, `${h > 12 ? h - 12 : h}:${String(mm).padStart(2, '0')}${h >= 12 ? 'pm' : 'am'}`]; }));

  function tailor(head, body) {
    const wants = new Set(picked());
    const saved = (S.st.tailor || {}).ui || {};
    header(head, 'Step 5', 'Make it yours', 'A few questions so Atlas knows you from the first day instead of the third week. Everything here can change later by just texting Atlas.');
    const ui = { time: saved.time || 'random 09:00-10:00', beats: saved.beats || { local: true, country: true, world: true },
      interests: saved.interests || {}, teams: saved.teams || [], extra: saved.extra || '', goals: saved.goals || [], habits: saved.habits || [] };
    let any = false;

    if (wants.has('brief')) {
      any = true;
      const sec = el(`<div class="su-section"><h3>Your morning brief</h3><p>When should it come?</p></div>`);
      const sel = el(`<select class="tb" style="max-width:280px">${TIMES.map(([v, t]) => `<option value="${v}" ${v === ui.time ? 'selected' : ''}>${E(t)}</option>`).join('')}</select>`);
      sel.onchange = () => { ui.time = sel.value; };
      sec.appendChild(el('<div class="su-field"></div>')).appendChild(sel);
      sec.appendChild(el('<h3>The news in it</h3>'));
      sec.appendChild(el('<p>The usual three, then whatever you’re into. Tap to turn things on or off.</p>'));
      const city = ((S.st.env || {}).OWNER_CITY || 'your city').split(',')[0];
      const base = el('<div class="su-chips"></div>');
      [['local', `Local (${city})`], ['country', 'Your country'], ['world', 'World']].forEach(([id, t]) => {
        const c = el(`<button type="button" class="su-chip ${ui.beats[id] ? 'on' : ''}">${E(t)}</button>`);
        c.onclick = () => { ui.beats[id] = !ui.beats[id]; c.classList.toggle('on', ui.beats[id]); };
        base.appendChild(c);
      });
      sec.appendChild(base);
      sec.appendChild(el('<p style="margin-top:14px">What are you into?</p>'));
      const chips = el('<div class="su-chips"></div>'), subs = el('<div></div>');
      const paintSubs = () => {
        subs.innerHTML = '';
        INTERESTS.forEach(([id, t, ask]) => {
          if (!ui.interests[id] || !ask) return;
          if (ask === 'teams') { subs.appendChild(teamRows(ui)); return; }
          const s = el(`<div class="su-sub"><label>${E(t)}: ${E(ask)} <span class="opt">optional</span></label><input class="tb" spellcheck="false" style="width:100%"></div>`);
          const inp = s.querySelector('input');
          inp.value = typeof ui.interests[id] === 'string' ? ui.interests[id] : '';
          inp.oninput = () => { ui.interests[id] = inp.value || true; };
          subs.appendChild(s);
        });
      };
      INTERESTS.forEach(([id, t]) => {
        const c = el(`<button type="button" class="su-chip ${ui.interests[id] ? 'on' : ''}">${E(t)}</button>`);
        c.onclick = () => { if (ui.interests[id]) delete ui.interests[id]; else ui.interests[id] = true; c.classList.toggle('on', !!ui.interests[id]); paintSubs(); };
        chips.appendChild(c);
      });
      sec.append(chips, subs);
      paintSubs();
      const extra = el('<div class="su-field" style="margin-top:14px;max-width:560px"><label>Anything else? <span class="opt">optional</span></label><input class="tb" spellcheck="false" placeholder="Formula 1 only, no NASCAR. Local concerts."></div>');
      extra.querySelector('input').value = ui.extra;
      extra.querySelector('input').oninput = e => { ui.extra = e.target.value; };
      sec.appendChild(extra);
      body.appendChild(sec);
    }

    if (wants.has('goals')) {
      any = true;
      const sec = el(`<div class="su-section"><h3>What are you working toward?</h3><p>Big or small, in your own words. Atlas checks in on these and calls it out when one stalls.</p></div>`);
      sec.appendChild(listRows(ui.goals, 'Save $2,000 by summer', 5));
      sec.appendChild(el('<h3>Habits you want to build</h3>'));
      sec.appendChild(el('<p>Atlas asks about one in the brief each day and keeps a streak.</p>'));
      sec.appendChild(listRows(ui.habits, 'Gym 4 days a week', 5));
      body.appendChild(sec);
    }

    const later = [wants.has('schedule') && 'your calendar', wants.has('email') && 'your email'].filter(Boolean);
    if (later.length) body.appendChild(info('', `You’ll connect ${later.join(' and ')} near the end, in Add-ons. It’s quick.`));
    if (!any) body.appendChild(info('ok', 'Nothing else to set up for what you picked. Press Continue.'));

    STEPS[S.i].beforeNext = async () => {
      if (!any) return true;
      const city = (S.st.env || {}).OWNER_CITY || 'their city';
      const beats = [];
      if (ui.beats.local) beats.push({ label: 'Local', text: `${city}: roads, weather events, council decisions, transit, crime and safety that made the news, big events.` });
      if (ui.beats.country) beats.push({ label: 'Country', text: 'the one or two national stories everyone will be talking about today, plus anything that changes what things cost.' });
      if (ui.beats.world) beats.push({ label: 'World', text: 'the biggest international story of the day.' });
      const teams = ui.teams.filter(t => (t.team || '').trim());
      INTERESTS.forEach(([id, t]) => {
        const v = ui.interests[id];
        if (!v) return;
        const detail = typeof v === 'string' ? v.trim() : '';
        let text = BEAT_TEXT[id] || `the biggest ${t.toLowerCase()} story`;
        if (id === 'sports') text = teams.length ? `${teams.map(x => `${x.team} (${x.league})`).join(', ')}: last night’s result, trades, injuries and anything big. Plus the biggest sports story of the day.` : 'the biggest sports story of the day.';
        else if (detail) text += `, especially: ${detail}`;
        beats.push({ label: t, text: text.replace(/\.?$/, '.') });
      });
      if (ui.extra.trim()) beats.push({ label: 'Their extra asks', text: ui.extra.trim() });
      const data = {
        brief_time: wants.has('brief') ? ui.time : 'off',
        beats: wants.has('brief') ? beats : [],
        goals: ui.goals.filter(g => g.trim()),
        habits: ui.habits.filter(h => h.trim()).map(h => ({ name: h, gist: h })),
        teams: teams.map(x => ({ league: x.league, team: x.team.trim() })),
        ui,
      };
      const r = await api('setup_tailor', data);
      if (!r || !r.ok) { msg((r && r.message) || 'Couldn’t save that.', true); return false; }
      if (r.warn) { window.toast ? toast(r.warn) : msg(r.warn, true); }
      S.st = await api('setup_state');
      return true;
    };
  }

  // Editable list of short text rows (goals, habits).
  function listRows(arr, ph, max) {
    const box = el('<div class="su-list"></div>');
    const paint = () => {
      box.innerHTML = '';
      const rows = arr.length ? arr : [''];
      if (!arr.length) arr.push('');
      rows.forEach((v, n) => {
        const r = el(`<div class="row"><input class="tb" spellcheck="true" placeholder="${n === 0 ? E(ph) : ''}"><button type="button" class="btn subtle">Remove</button></div>`);
        const inp = r.querySelector('input'); inp.value = v;
        inp.oninput = () => { arr[n] = inp.value; };
        r.querySelector('button').onclick = () => { arr.splice(n, 1); paint(); };
        box.appendChild(r);
      });
      if (arr.length < max) {
        const add = el('<button type="button" class="btn subtle" style="align-self:flex-start">Add another</button>');
        add.onclick = () => { arr.push(''); paint(); box.querySelectorAll('input')[arr.length - 1].focus(); };
        box.appendChild(add);
      }
    };
    paint();
    return box;
  }

  // Sports: which teams, each with its league (they also get final-score alerts).
  function teamRows(ui) {
    const s = el('<div class="su-sub"><label>Sports: your teams. You get the final score after every game, and their news in the brief.</label><div class="su-list"></div></div>');
    const box = s.querySelector('.su-list');
    const paint = () => {
      box.innerHTML = '';
      if (!ui.teams.length) ui.teams.push({ league: 'NHL', team: '' });
      ui.teams.forEach((t, n) => {
        const r = el(`<div class="row"><select class="tb" style="flex:0 0 150px">${LEAGUES.map(l => `<option ${l === t.league ? 'selected' : ''}>${E(l)}</option>`).join('')}</select>
          <input class="tb" spellcheck="false" placeholder="${n === 0 ? 'Team name' : ''}"><button type="button" class="btn subtle">Remove</button></div>`);
        r.querySelector('select').onchange = e => { t.league = e.target.value; };
        const inp = r.querySelector('input'); inp.value = t.team || '';
        inp.oninput = () => { t.team = inp.value; };
        r.querySelector('button').onclick = () => { ui.teams.splice(n, 1); paint(); };
        box.appendChild(r);
      });
      const add = el('<button type="button" class="btn subtle" style="align-self:flex-start">Add a team</button>');
      add.onclick = () => { ui.teams.push({ league: ui.teams[ui.teams.length - 1]?.league || 'NHL', team: '' }); paint(); };
      box.appendChild(add);
    };
    paint();
    return s;
  }

  /* -------------------------------------------------------- 4 claude -- */
  function claude(head, body) {
    header(head, 'Step 4', 'Connect Claude', 'Claude is what thinks and writes every reply. It runs on your own Claude plan.');
    const slot = el('<div></div>'), con = consoleBox('Claude');
    body.appendChild(slot);
    const tokenBox = el('<div class="su-card"></div>');
    const load = async () => {
      const list = await api('setup_check', 'claude') || [];
      const cli = list.find(c => c.id === 'cli'), tok = list.find(c => c.id === 'token');
      if (!cli.ok) {
        const b = el('<button class="btn accent">Install</button>');
        b.onclick = async () => { b.disabled = true; con.classList.remove('hidden'); await con.run('claude_install'); load(); };
        cli.action = b;
      }
      slot.innerHTML = '';
      slot.appendChild(checks(list));
      S.ok.claude = S.st.dry || (cli.ok && tok.ok);
      refresh();
    };
    tokenBox.innerHTML = `<h3>Sign in to Claude</h3><p>Press <b>Sign in</b>. A window opens and your browser asks you to log in to Claude.
      When it’s done, the window shows a long token starting with <code>sk-ant-</code>. Copy it and paste it here.</p>`;
    const row = el('<div class="su-extra" style="margin-top:12px"><div class="row"></div></div>');
    const sign = el('<button class="btn">Sign in</button>');
    sign.onclick = async () => { const r = await api('setup_claude_signin'); msg(r && r.message, !(r && r.ok)); };
    const f = field('CLAUDE_CODE_OAUTH_TOKEN', 'Token', { secret: true, ph: 'sk-ant-oat01-...' });
    const save = el('<button class="btn accent">Save and test</button>');
    save.onclick = async () => {
      save.disabled = true;
      if (await saveFields(row)) {
        con.classList.remove('hidden');
        const p = await con.run('claude_test');
        msg(p.code === 0 ? 'Claude answered. You’re connected.' : 'The test failed. Check the token and try again.', p.code !== 0);
      }
      save.disabled = false;
      load();
    };
    row.querySelector('.row').append(f, sign, save);
    tokenBox.appendChild(row);
    body.appendChild(tokenBox);
    con.classList.add('hidden');
    body.appendChild(con);
    load();
  }

  /* ----------------------------------------------------------- 5 wsl -- */
  function wsl(head, body) {
    header(head, 'Step 5', 'Set up Linux', 'The iMessage bridge runs in Linux inside Windows (WSL). This installs it and keeps it running in the background.');
    const slot = el('<div></div>'), con = consoleBox('Linux');
    con.classList.add('hidden');
    body.append(slot);
    const load = async () => {
      slot.innerHTML = '';
      slot.appendChild(checks([{ id: 'x', label: 'Checking Linux...', ok: null }]));
      const list = await api('setup_check', 'wsl') || [];
      const by = Object.fromEntries(list.map(c => [c.id, c]));
      const btn = (label, fn) => { const b = el(`<button class="btn accent">${label}</button>`); b.onclick = async () => { b.disabled = true; await fn(); load(); }; return b; };
      if (!by.distro.ok) by.distro.action = btn('Install', async () => {
        const r = await api('setup_wsl_install');
        msg(r && r.message, !(r && r.ok));
      });
      else if (!by.user.ok) by.user.action = btn('Create', async () => { con.classList.remove('hidden'); await con.run('wsl_user'); });
      else if (!by.keepalive.ok) by.keepalive.action = btn('Set up', async () => { con.classList.remove('hidden'); await con.run('keepalive'); });
      slot.innerHTML = '';
      slot.appendChild(checks(list));
      if (!by.wsl.ok && by.distro.ok)
        slot.appendChild(info('warn', 'Linux is installed but Windows needs a <b>restart</b> to finish. Restart the PC, then open Atlas again. Setup picks up right here.'));
      if (!by.distro.ok)
        slot.appendChild(info('', 'Windows asks for permission, then a window shows the install. It can take a few minutes. If it says to restart, restart and open Atlas again.'));
      const again = el('<button class="btn" style="margin-top:12px">Check again</button>');
      again.onclick = load;
      slot.appendChild(again);
      S.ok.wsl = S.st.dry || list.every(c => c.ok);
      refresh();
    };
    body.appendChild(con);
    load();
  }

  /* -------------------------------------------------------- 6 bridge -- */
  const BUILD_STAGES = [['packages', 'Packages', 'a few minutes'], ['toolchains', 'Go and Rust', '5 to 10 min'], ['source', 'Source code', 'a few minutes'],
    ['build', 'Build', '20 to 40 min'], ['synapse', 'Message server', 'a few minutes'], ['bridge-config', 'Settings', 'under a minute']];
  const DOWNLOAD_STAGES = [['packages', 'Packages', 'a few minutes'], ['download', 'The bridge', 'a minute'],
    ['synapse', 'Message server', 'a few minutes'], ['bridge-config', 'Settings', 'under a minute']];
  let STAGES = BUILD_STAGES;
  function bridge(head, body) {
    // A kit with a prebuilt bridge downloads it; otherwise it gets compiled here.
    STAGES = S.st.bridgeRelease ? DOWNLOAD_STAGES : BUILD_STAGES;
    header(head, 'Step 6', 'Set up the iMessage bridge', S.st.bridgeRelease
      ? 'This installs the piece that lets the bots send and receive iMessages. It runs by itself and takes about 10 minutes.'
      : 'This builds the piece that lets the bots send and receive iMessages. It runs by itself and takes 30 to 60 minutes. You can leave it and come back, as long as the app stays open.');
    const row = el('<div class="su-stages"></div>');
    const con = consoleBox('Bridge setup');
    const go = el(`<button class="btn accent">${S.st.bridgeRelease ? 'Set it up' : 'Build it'}</button>`);
    body.append(row, el('<div></div>'), con);
    body.insertBefore(go, con);
    const paint = (cur, failed) => {
      const steps = S.st.steps || {};
      row.innerHTML = '';
      STAGES.forEach(([id, t, d]) => {
        const done = (steps['bridge:' + id] || {}).status === 'done';
        row.appendChild(el(`<div class="su-stage ${done ? 'ok' : ''} ${cur === id ? 'on' : ''} ${failed === id ? 'bad' : ''}"><b>${E(t)}</b><span>${E(d)}</span></div>`));
      });
      S.ok.bridge = STAGES.every(([id]) => (steps['bridge:' + id] || {}).status === 'done');
      go.textContent = S.ok.bridge ? 'Done' : Object.keys(steps).some(k => k.startsWith('bridge:')) ? 'Keep going' : (S.st.bridgeRelease ? 'Set it up' : 'Build it');
      go.disabled = S.ok.bridge;
      refresh();
    };
    go.onclick = async () => {
      go.disabled = true;
      for (const [id, t] of STAGES) {
        if (((S.st.steps || {})['bridge:' + id] || {}).status === 'done') continue;
        paint(id);
        con.querySelector('.ttl').textContent = `Bridge: ${t}`;
        const p = await con.run('bridge_stage', { stage: id });
        S.st = await api('setup_state');
        if (p.code !== 0) { paint(null, id); msg(`${t} failed. The log above says why. Press Keep going to retry from there.`, true); go.disabled = false; return; }
      }
      paint(null);
      msg('The bridge is ready.');
    };
    paint(null);
  }

  /* ----------------------------------------------------------- 7 mac -- */
  function mac(head, body) {
    header(head, 'Step 7', 'Get your MacBook’s identity',
      'Apple only lets real Macs use iMessage, so the bridge borrows your MacBook’s identity. This reads it off the Mac. Nothing on the Mac changes.');
    const list = el(`<ol class="su-steps-list">
      <li>On the MacBook, press <b>Command + Space</b>, type <b>Terminal</b> and press Enter.</li>
      <li>Copy the command below, paste it into Terminal, and press Enter.</li>
      <li>It prints one line that starts with <b>{</b>. Copy that whole line and paste it in the box here.</li></ol>`);
    body.appendChild(list);
    body.appendChild(S.st.macCommand ? copyBox(S.st.macCommand, 'Copy command')
      : info('bad', 'The Mac command is missing from this copy of Atlas (Shared\\iMessage\\mac-info.oneline.txt).'));
    body.appendChild(info('', 'Getting the command onto the Mac: the easiest way is to email it to yourself, or AirDrop it, then copy it from there.'));
    const box = el(`<div class="su-field full"><label>What the Mac printed</label>
      <textarea class="tb" rows="4" style="height:auto;padding:8px 11px;font:12px/18px var(--mono);resize:vertical" spellcheck="false" placeholder='{"model":"MacBookPro..."}'></textarea>
      <div class="err hidden"></div></div>`);
    const save = el('<button class="btn accent">Save it</button>'), con = consoleBox('Mac identity');
    con.classList.add('hidden');
    save.onclick = async () => {
      const raw = box.querySelector('textarea').value;
      const err = box.querySelector('.err');
      err.classList.add('hidden');
      if (/apple_silicon/.test(raw)) { err.textContent = 'This Mac is Apple Silicon (M1 or newer). The bridge needs an Intel Mac. Use a different Mac.'; err.classList.remove('hidden'); return; }
      save.disabled = true;
      con.classList.remove('hidden');
      const p = await con.run('mac_key', { json: raw });
      save.disabled = false;
      if (p.error) { err.textContent = p.error; err.classList.remove('hidden'); }
      S.ok.mac = p.code === 0;
      if (S.ok.mac) msg('Mac identity saved.');
      refresh();
    };
    if (S.st.dry) {
      // A made-up Intel Mac, so the test run can get past this step without one.
      box.querySelector('textarea').value = JSON.stringify({ cpu: 'intel', model: 'MacBookPro12,1', serial: 'C02TEST0FAKE',
        mlb: 'C02TESTFAKE0000A1', uuid: '00000000-1111-2222-3333-444444444444', disk_uuid: '55555555-6666-7777-8888-999999999999',
        board_id: 'Mac-E43C1C25D4880AD6', rom: 'a1b2c3d4e5f6', mac: 'a4:83:e7:00:00:01', build: '22G513', version: '13.6.4', darwin: '22.6.0' });
      box.appendChild(el('<div class="hint">Test run: a made-up Mac is filled in. Press Save it.</div>'));
    }
    body.append(box, save, con);
  }

  /* --------------------------------------------------------- 8 apple -- */
  function apple(head, body) {
    header(head, 'Step 8', 'Sign in the bot’s Apple ID', 'The bots text you from their own Apple ID, never yours. This step sets it up and signs it in once.');
    const prep = el(`<div class="su-card"><h3>Before you sign in</h3><ol class="su-steps-list">
      <li>Use a <b>spare Apple ID</b>, not your main one. The best is an older one that’s been on an iPhone before.</li>
      <li>Give each bot its own address on that Apple ID, so each one shows up as its own chat on your phone. On
        <a href="#" data-url="https://account.apple.com">account.apple.com</a>, open <b>Sign-In and Security</b>, then <b>Email and Phone Numbers</b>, and add the two addresses shown below. Apple emails a code to each one to confirm it.</li>
      <li>Don’t sign this Apple ID into Messages on your MacBook. The same identity in two places is what gets accounts flagged.</li></ol></div>`);
    prep.querySelectorAll('[data-url]').forEach(a => a.onclick = e => { e.preventDefault(); api('setup_open', a.dataset.url); });
    body.appendChild(prep);
    const form = el('<div class="su-form"></div>');
    form.appendChild(field('BOT_APPLE_ID', 'The bot’s Apple ID (its email)', { ph: 'myname.bots@gmail.com',
      hint: 'A Gmail works best, because Gmail delivers mail sent to name+anything@gmail.com.' }));
    const alias = el('<div class="su-field"><label>Addresses to add on the Apple ID</label><div class="hint aliases"></div></div>');
    form.appendChild(alias);
    const paintAlias = () => {
      const v = form.querySelector('[data-key="BOT_APPLE_ID"] input').value.trim();
      const [u, d] = v.split('@');
      alias.querySelector('.aliases').innerHTML = u && d ? `<b>${E(u)}+atlas@${E(d)}</b><br><b>${E(u)}+iris@${E(d)}</b>` : 'Type the Apple ID first.';
    };
    form.querySelector('[data-key="BOT_APPLE_ID"] input').oninput = paintAlias;
    paintAlias();
    body.appendChild(form);
    body.appendChild(info('warn', '<b>One try at a time.</b> Apple locks accounts that sign in over and over. If it fails, read what it says before trying again. The button waits 15 minutes between tries.'));
    // The password is typed here and handed to the login when it asks. It's
    // never saved: not in .env, not in the log.
    const pw = el(`<div class="su-field"><label>The bot’s Apple ID password</label>
      <span class="su-pw"><input class="tb" type="password" spellcheck="false" autocomplete="off"><button type="button" class="eye" title="Show"></button></span>
      <div class="hint">Only used for this sign-in. It isn’t saved anywhere.</div><div class="err hidden"></div></div>`);
    const pwIn = pw.querySelector('input'), eye = pw.querySelector('.eye');
    eye.textContent = '';
    eye.onclick = () => { pwIn.type = pwIn.type === 'password' ? 'text' : 'password'; };
    clearOnEdit(pw);
    form.insertBefore(pw, alias);
    const signin = askBox('Apple sign-in', {
      doneText: 'Signed in. The bots can use iMessage now.',
      answers: {
        apple_id: () => form.querySelector('[data-key="BOT_APPLE_ID"] input').value.trim(),
        password: () => { const v = pwIn.value; pwIn.value = ''; return v; },
      },
    });
    const go = el('<button class="btn accent">Sign in</button>');
    go.onclick = async () => {
      if (!pwIn.value) {
        pw.classList.add('bad'); const e = pw.querySelector('.err'); e.textContent = 'Type the password first.'; e.classList.remove('hidden');
        return;
      }
      if (!await saveFields(form, ['BOT_APPLE_ID'])) return;
      go.disabled = true;
      const p = await signin.run('apple_login');
      if (p.error) { msg(p.error, true); pwIn.value = ''; }
      S.ok.apple = p.code === 0;
      S.st = await api('setup_state');
      go.disabled = S.ok.apple;
      if (!S.ok.apple) go.textContent = 'Try again';
      refresh();
    };
    if (S.st.loginWait > 0) msg(`You can try signing in again in ${Math.ceil(S.st.loginWait / 60)} minutes.`);
    body.append(go, signin);
    S.ok.apple = ((S.st.steps || {}).apple_login || {}).status === 'done';
    refresh();
  }

  /* ------------------------------------------------------- 9 connect -- */
  function connect(head, body) {
    header(head, 'Step 9', 'Connect the bots', 'Starts the bridge, hooks Atlas and Iris up to it, and sets it to start by itself whenever you log in.');
    const con = consoleBox('Connecting');
    const go = el('<button class="btn accent">Connect</button>');
    go.onclick = async () => {
      go.disabled = true;
      for (const [action, label] of [['bridge_start', 'Starting the bridge'], ['matrix_setup', 'Connecting the bots'], ['bridge_task', 'Auto-start at login']]) {
        con.querySelector('.ttl').textContent = label;
        const p = await con.run(action);
        if (p.code !== 0) { msg(`${label} failed. The log says why.`, true); go.disabled = false; return; }
      }
      S.ok.connect = true;
      msg('Connected.');
      refresh();
    };
    body.append(go, con);
  }

  /* -------------------------------------------------------- add-ons -- */
  function extras(head, body) {
    header(head, 'Step 12', 'Add-ons', 'Extra things Atlas can do. All optional: connect what you want now, the rest any time from Add-ons in the app.');
    const wants = new Set(picked());
    const push = [wants.has('schedule') && 'your calendar (and Reminders)', wants.has('email') && 'Gmail'].filter(Boolean);
    if (push.length) body.appendChild(info('', `You picked ${wants.has('schedule') && wants.has('email') ? 'your schedule and email' : wants.has('schedule') ? 'your schedule' : 'email'}, so connect ${push.join(' and ')} here. Atlas can’t do that part without it.`));
    const box = el('<div></div>');
    body.appendChild(box);
    if (window.Addons) Addons.render(box, { mode: 'setup' });
    else body.appendChild(info('bad', 'The add-ons part of the app is missing (App\addons.js).'));
  }

  /* -------------------------------------------------------- 11 finish -- */
  function finish(head, body) {
    header(head, 'Step 11', 'Say hi', 'Last step: this starts Atlas and Iris, sets them to start whenever you log in, and Atlas texts you.');
    const con = consoleBox('Finishing');
    const go = el('<button class="btn accent">Start the bots</button>');
    const done = el(`<div class="su-done hidden"><div class="big">${I.check}</div><h2>Check your phone</h2>
      <p>Atlas just texted you. Text it back anything. Text Iris to have her message the people you know.</p>
      <div style="margin-top:18px;display:flex;gap:8px;justify-content:center"><button class="btn got accent">I got the text</button><button class="btn nope">It didn’t come</button></div></div>`);
    go.onclick = async () => {
      go.disabled = true;
      let p = await con.run('crew_task');
      if (p.code === 0) p = await con.run('finish');
      if (p.code !== 0) { msg('Something failed. The log says why.', true); go.disabled = false; return; }
      done.classList.remove('hidden');
    };
    done.querySelector('.got').onclick = () => { S.ok.finish = true; refresh(); next(); };
    done.querySelector('.nope').onclick = () => body.appendChild(info('warn',
      'Give it a minute: the first text from a new Apple ID can be slow. If nothing comes in 10 minutes, open <b>Overview</b> in this app and check the bridge is green, then press Start the bots again.'));
    body.append(go, con, done);
  }

  window.Setup = { open, close, askBox, consoleBox };
})();
