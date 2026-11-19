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
    { id: 'you', t: 'About you', c: 'Name, number, time zone', render: you, ready: () => S.ok.you },
    { id: 'claude', t: 'Claude', c: 'The brain', render: claude, ready: () => S.ok.claude },
    { id: 'wsl', t: 'Linux', c: 'Where the bridge runs', render: wsl, ready: () => S.ok.wsl },
    { id: 'bridge', t: 'iMessage bridge', c: 'Build it', render: bridge, ready: () => S.ok.bridge },
    { id: 'mac', t: 'Mac identity', c: 'From your MacBook', render: mac, ready: () => S.ok.mac },
    { id: 'apple', t: 'Bot Apple ID', c: 'Sign in once', render: apple, ready: () => S.ok.apple },
    { id: 'connect', t: 'Connect', c: 'Bots meet the bridge', render: connect, ready: () => S.ok.connect },
    { id: 'extras', t: 'Extras', c: 'Calendar, email, more', render: extras, ready: () => true, optional: true },
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
    return f;
  }

  async function saveFields(box, required = []) {
    const vals = {};
    box.querySelectorAll('.su-field').forEach(f => {
      const inp = f.querySelector('input,select,textarea');
      if (!inp || !f.dataset.key) return;   // a label-only block, like the alias list
      const k = f.dataset.key, v = inp.value.trim();
      const secret = inp.type === 'password';
      if (secret && !v) return;          // blank secret = keep what's saved
      vals[k] = v;
      f.classList.remove('bad'); f.querySelector('.err').classList.add('hidden');
    });
    for (const k of required) if (!vals[k] && !(S.st.secretsSet || {})[k] && !(S.st.env || {})[k]) {
      const f = box.querySelector(`[data-key="${k}"]`);
      f.classList.add('bad'); const e = f.querySelector('.err'); e.textContent = 'This one is needed.'; e.classList.remove('hidden');
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
      box.classList.remove('hidden');
      setTimeout(() => box.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 50);
      const r = await api('setup_run', action, params || {});
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
    return box;
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
        slot.appendChild(info('bad', 'The bridge build needs about 25 GB. Empty the Recycle Bin, uninstall games or apps you don’t use, or move big folders to another drive, then press Check again.'));
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
    const tzGuess = (S.st.env || {}).OWNER_TIMEZONE || S.st.windowsTzIana || 'America/Toronto';
    const form = el('<div class="su-form"></div>');
    form.appendChild(field('OWNER_NAME', 'First name', { ph: 'Sam' }));
    form.appendChild(field('OWNER_PHONE', 'Your iPhone number', { ph: '403 555 0123', hint: 'The number you text from. Anyone else who texts the bots gets treated as a stranger.' }));
    form.appendChild(field('OWNER_EMAIL', 'Your email', { ph: 'you@gmail.com', optional: true }));
    form.appendChild(field('OWNER_CITY', 'City', { ph: 'Toronto', hint: 'For the weather and local news in your morning brief.' }));
    form.appendChild(field('OWNER_TIMEZONE', 'Time zone', { options: TZ.includes(tzGuess) ? TZ : [tzGuess, ...TZ], value: tzGuess,
      hint: S.st.windowsTz ? `Windows says ${E(S.st.windowsTz)}. Make sure this PC's clock is set to your time zone too.` : '' }));
    body.appendChild(form);
    S.ok.you = !!((S.st.env || {}).OWNER_NAME && (S.st.env || {}).OWNER_PHONE);
    STEPS[2].beforeNext = async () => {
      const ok = await saveFields(form, ['OWNER_NAME', 'OWNER_PHONE', 'OWNER_TIMEZONE']);
      S.ok.you = ok;
      if (ok) msg('Saved.');
      return ok;
    };
    S.ok.you = true; // Continue saves and checks the form
    refresh();
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
  const STAGES = [['packages', 'Packages', 'a few minutes'], ['toolchains', 'Go and Rust', '5 to 10 min'], ['source', 'Source code', 'a few minutes'],
    ['build', 'Build', '20 to 40 min'], ['synapse', 'Message server', 'a few minutes'], ['bridge-config', 'Settings', 'under a minute']];
  function bridge(head, body) {
    header(head, 'Step 6', 'Build the iMessage bridge',
      'This builds the piece that lets the bots send and receive iMessages. It runs by itself and takes 30 to 60 minutes. You can leave it and come back, as long as the app stays open.');
    const row = el('<div class="su-stages"></div>');
    const con = consoleBox('Bridge build');
    const go = el('<button class="btn accent">Build it</button>');
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
      go.textContent = S.ok.bridge ? 'Built' : Object.keys(steps).some(k => k.startsWith('bridge:')) ? 'Keep going' : 'Build it';
      go.disabled = S.ok.bridge;
      refresh();
    };
    go.onclick = async () => {
      go.disabled = true;
      for (const [id, t] of STAGES) {
        if (((S.st.steps || {})['bridge:' + id] || {}).status === 'done') continue;
        paint(id);
        con.querySelector('.ttl').textContent = `Bridge build: ${t}`;
        const p = await con.run('bridge_stage', { stage: id });
        S.st = await api('setup_state');
        if (p.code !== 0) { paint(null, id); msg(`${t} failed. The log above says why. Press Keep going to retry from there.`, true); go.disabled = false; return; }
      }
      paint(null);
      msg('The bridge is built.');
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
    const con = consoleBox('Apple sign-in', { input: true });
    const go = el('<button class="btn accent">Sign in now</button>');
    go.onclick = async () => {
      if (!await saveFields(form, ['BOT_APPLE_ID'])) return;
      go.disabled = true;
      if (!body.querySelector('.su-answer')) { const n = info('', 'Answer the questions in the box under the log: the Apple ID, its password, then the 6-digit code Apple sends to your phone.'); n.classList.add('su-answer'); body.insertBefore(n, con); }
      const p = await con.run('apple_login');
      if (p.error) msg(p.error, true);
      S.ok.apple = p.code === 0;
      msg(S.ok.apple ? 'Signed in.' : 'It didn’t sign in. The log says why.', !S.ok.apple);
      S.st = await api('setup_state');
      go.disabled = S.ok.apple;
      refresh();
    };
    if (S.st.loginWait > 0) msg(`You can try signing in again in ${Math.ceil(S.st.loginWait / 60)} minutes.`);
    con.classList.add('hidden');
    body.append(go, con);
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

  /* -------------------------------------------------------- 10 extras -- */
  function extras(head, body) {
    header(head, 'Step 10', 'Extras', 'All optional. Each one makes the bots more useful. Skip any of them and add it later from here.');
    const card = (title, text, fields, test, links) => {
      const c = el(`<div class="su-card su-extra"><h3>${E(title)}</h3><p>${text}</p><div class="row"></div></div>`);
      const row = c.querySelector('.row');
      fields.forEach(f => row.appendChild(field(...f)));
      const con = consoleBox(title, { input: test === 'icloud' });
      con.classList.add('hidden');
      const b = el(`<button class="btn accent">${test === 'icloud' ? 'Sign in' : 'Save and test'}</button>`);
      b.onclick = async () => {
        b.disabled = true;
        if (fields.length && !await saveFields(row)) { b.disabled = false; return; }
        if (test) { con.classList.remove('hidden'); const p = await con.run('test', { what: test }); msg(p.code === 0 ? `${title}: works.` : `${title}: didn't work. The log says why.`, p.code !== 0); }
        b.disabled = false;
      };
      row.appendChild(b);
      (links || []).forEach(([t, u]) => { const a = el(`<button class="btn subtle"><i class="ico">${I.open}</i>${E(t)}</button>`); a.onclick = () => api('setup_open', u); row.appendChild(a); });
      c.appendChild(con);
      return c;
    };
    body.appendChild(card('Calendar', 'Lets Atlas read and add events on your iCloud calendar. Needs an app-specific password for <b>your</b> Apple ID.',
      [['APPLE_ID', 'Your Apple ID'], ['APPLE_APP_PASSWORD', 'App-specific password', { secret: true, ph: 'abcd-efgh-ijkl-mnop' }]], 'calendar',
      [['Make an app password', 'https://account.apple.com/account/manage']]));
    body.appendChild(card('Reminders', 'Lets Atlas make real reminders on your iPhone. Signs in to your iCloud once, with a 2FA code.', [], 'icloud'));
    body.appendChild(card('Gmail', 'Lets Atlas tell you about real emails and watch for ones you’re waiting on.',
      [['GMAIL_ADDRESS', 'Gmail address'], ['GMAIL_APP_PASSWORD', 'App password', { secret: true }]], 'gmail',
      [['Make an app password', 'https://myaccount.google.com/apppasswords']]));
    body.appendChild(card('Other calendars', 'Team schedules, school, anything with a calendar link (.ics). Paste the links, separated by commas.',
      [['ICS_FEEDS', 'Calendar links', { full: true, ph: 'https://...ics, webcal://...' }]], 'feeds'));
    body.appendChild(card('Iris’s email', 'Gives Iris her own email address for emailing people for you. Use a separate Gmail with an app password.',
      [['IRIS_EMAIL_ADDRESS', 'Iris’s Gmail'], ['IRIS_EMAIL_APP_PASSWORD', 'App password', { secret: true }]], 'iris_email'));
    body.appendChild(card('Pictures for Iris', 'A free Pixabay key lets Iris find real photos to send. Memes work without it.',
      [['PIXABAY_KEY', 'Pixabay key', { secret: true }]], null, [['Get a key', 'https://pixabay.com/api/docs/']]));
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

  window.Setup = { open, close };
})();
