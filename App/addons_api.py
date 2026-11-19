r"""The Add-ons page's backend.

Mix AddonsMixin into App/Atlas.pyw's Api next to SetupMixin:

    from addons_api import AddonsMixin
    class Api(SetupMixin, AddonsMixin): ...

and the page (addons.js) can call every public method here as
pywebview.api.addons_*(...). The registry itself is Shared\addons.py; this
file only turns it into page calls and runs the sign-ins:

  - OAuth (Spotify, Strava): a one-shot server on 127.0.0.1 catches the
    browser's redirect (addons.OAuthFlow); the page polls addons_poll(id).
  - Apple Music: a second pywebview window on music.apple.com; once the owner
    is signed in, MusicKit's developer and user tokens are read off the page,
    saved, and the window closes. The page polls addons_poll(id).
  - Commands that ask questions (Reminders' iCloud login asks for the password
    and the 2FA code) run as setup_flow Jobs, so the page reads them with the
    walkthrough's own setup_poll / setup_input / setup_cancel and answers the
    prompts the same way the setup console does.

Settings are written with SetupMixin's own writer (setup_save validates the
keys it knows, then _write_env keeps .env's comments and order). Dry run
(Atlas.pyw --dry-run) fakes every sign-in; tests that only read still run.
"""

import os
import sys
import threading
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
ROOT = APP.parent
SHARED = ROOT / "Shared"
sys.path.insert(0, str(SHARED))

import setup_flow  # noqa: E402
from setup_flow import DRY, Job, fake  # noqa: E402

if DRY:
    os.environ["ATLAS_SETUP_DRYRUN"] = "1"   # so Shared\addons.py, and every script it runs, use the dry-run .env
import addons  # noqa: E402

MUSIC_URL = "https://music.apple.com/"
MUSIC_JS = """(function () {
  try {
    var mk = window.MusicKit && MusicKit.getInstance && MusicKit.getInstance();
    if (!mk) return {ready: false};
    return {ready: true, dev: mk.developerToken || '', user: mk.musicUserToken || '',
            sf: mk.storefrontId || mk.storefrontCountryCode || '', auth: !!mk.isAuthorized};
  } catch (e) { return {ready: false, err: String(e)}; }
})()"""


class AddonsMixin:
    """Mixed into the app's Api. Every public method is page-callable."""

    _addon_flows: dict = {}    # add-on id -> {"kind", "state", "message", "job"?, "flow"?}

    # ----------------------------------------------------------- helpers --

    def _addon_env(self) -> dict:
        return addons.env()

    def _addon_write(self, values: dict) -> None:
        # SetupMixin's writer when it's mixed in (it reloads owner too), else the same one via addons.save
        writer = getattr(self, "_write_env", None)
        writer(values) if writer else addons.save(values)

    def _addon_entry(self, a, e: dict) -> dict:
        st = addons.status(a.id, e)
        fields = []
        for f in a.fields:
            val = e.get(f.key, "")
            fields.append({"key": f.key, "label": f.label, "help": f.help, "link": f.link, "linkLabel": f.link_label,
                           "secret": f.secret, "required": f.required, "placeholder": f.placeholder,
                           "value": "" if f.secret else val, "set": bool(val),
                           "fallback": (e.get(f.fallback, "") if f.fallback and not f.secret else "")})
        flow = self._addon_flows.get(a.id) or {}
        return {"id": a.id, "name": a.name, "desc": a.desc, "needs": a.needs, "group": a.group,
                "defaultOn": a.default_on, "icon": a.icon, "fields": fields, "steps": a.steps,
                "links": [list(x) for x in a.links], "copy": [list(x) for x in a.copy],
                "connect": a.connect, "connectLabel": a.connect_label, "canTest": bool(a.test),
                "enabled": st["enabled"], "status": st["state"], "message": st["message"],
                "tested": st["tested"], "busy": flow.get("state") == "waiting"}

    def _addon(self, addon_id):
        a = addons.BY_ID.get(str(addon_id))
        if not a:
            raise KeyError(f"No add-on called {addon_id}")
        return a

    # -------------------------------------------------------------- list --

    def addons_list(self):
        """Everything the page draws.
        -> {dry, redirect, addons: [entry]}; entry = {id, name, desc, needs, group ('default'|'connect'),
           defaultOn, icon, fields: [{key, label, help, link, linkLabel, secret, required, placeholder,
           value, set, fallback}], steps, links, copy, connect (''|'oauth'|'window'|'job'), connectLabel,
           canTest, enabled, status ('off'|'not_connected'|'connected'|'error'), message, tested, busy}"""
        e = self._addon_env()
        return {"dry": DRY, "redirect": addons.REDIRECT_URI,
                "addons": [self._addon_entry(a, e) for a in addons.ADDONS]}

    def addons_get(self, addon_id):
        a = self._addon(addon_id)
        return self._addon_entry(a, self._addon_env())

    # -------------------------------------------------------------- save --

    def addons_save(self, addon_id, fields):
        """Save an add-on's fields. Blank secrets keep what's saved; blank
        plain fields clear. -> {ok, errors: {key: message}, addon: entry}"""
        a = self._addon(addon_id)
        if not isinstance(fields, dict):
            return {"ok": False, "errors": {"_": "Nothing to save."}, "addon": self.addons_get(a.id)}
        known = {f.key: f for f in a.fields}
        vals, errors = {}, {}
        for k, v in fields.items():
            if k not in known:
                continue
            v = str(v or "").strip()
            if known[k].secret and not v:
                continue
            if "\n" in v or "\r" in v:
                errors[k] = "One line only."
                continue
            vals[k] = v
        errors.update(self._addon_validate(a, vals))
        if errors:
            return {"ok": False, "errors": errors, "addon": self.addons_get(a.id)}
        if not vals:
            return {"ok": True, "errors": {}, "addon": self.addons_get(a.id)}
        saver = getattr(self, "setup_save", None)
        if saver:  # the walkthrough's validation for the keys it knows (emails, app passwords)
            r = saver(vals)
            if not r.get("ok"):
                return {"ok": False, "errors": r.get("errors", {}), "addon": self.addons_get(a.id)}
        else:
            self._addon_write(vals)
        e = self._addon_env()
        if not addons.configured(a, e) and not e.get(a.flag):
            self._addon_write({a.flag: "on"})   # filling it in turns it on
        return {"ok": True, "errors": {}, "addon": self.addons_get(a.id)}

    def _addon_validate(self, a, vals: dict) -> dict:
        err = {}
        v = vals.get("SPOTIFY_CLIENT_ID")
        if v and not (len(v) == 32 and v.isalnum()):
            err["SPOTIFY_CLIENT_ID"] = "That isn't a Client ID. It's 32 letters and numbers, on the app's Settings page."
        v = vals.get("STRAVA_CLIENT_ID")
        if v and not v.isdigit():
            err["STRAVA_CLIENT_ID"] = "Strava's Client ID is just numbers."
        v = vals.get("NOTION_TOKEN")
        if v and not v.startswith(("ntn_", "secret_")):
            err["NOTION_TOKEN"] = "That isn't the integration secret. It starts with ntn_ (older ones with secret_)."
        v = vals.get("WEATHER_UNITS")
        if v and v.lower()[:1] not in ("c", "f"):
            err["WEATHER_UNITS"] = "Put c or f."
        elif v:
            vals["WEATHER_UNITS"] = v.lower()[:1]
        for k in ("WEATHER_COLD_BELOW", "WEATHER_HEAT_ABOVE"):
            if vals.get(k):
                try:
                    float(vals[k])
                except ValueError:
                    err[k] = "A number, like -25."
        v = vals.get("VOICE_NOTES_MODEL")
        if v and v.lower() not in ("tiny", "tiny.en", "base", "base.en", "small", "small.en"):
            err["VOICE_NOTES_MODEL"] = "Use base.en, small.en, base or small."
        elif v:
            vals["VOICE_NOTES_MODEL"] = v.lower()
        v = vals.get("APPLE_MUSIC_COUNTRY")
        if v and not (len(v) == 2 and v.isalpha()):
            err["APPLE_MUSIC_COUNTRY"] = "Two letters, like us or ca."
        elif v:
            vals["APPLE_MUSIC_COUNTRY"] = v.lower()
        v = vals.get("COMMUTE_PLACES")
        if v:
            bad = [p for p in v.split(";") if p.strip() and "=" not in p]
            if bad:
                err["COMMUTE_PLACES"] = "Write each one as name=address, separated by semicolons."
        return err

    def addons_set_enabled(self, addon_id, on):
        """Flip ADDON_<ID>. -> entry"""
        a = self._addon(addon_id)
        self._addon_write({a.flag: "on" if on else "off"})
        return self.addons_get(a.id)

    # -------------------------------------------------------------- test --

    def addons_test(self, addon_id):
        """Run the add-on's test now (blocking, up to ~90 s; pywebview runs
        each call on its own thread). -> {ok, message, lines: [str], addon: entry}"""
        a = self._addon(addon_id)
        if DRY and a.id in ("calendar", "reminders", "gmail", "feeds", "iris_email"):
            # those scripts read Shared\.env directly, not the dry-run copy
            addons.record_test(a.id, True, "(dry run) works")
            return {"ok": True, "message": "(dry run) works", "lines": [], "addon": self.addons_get(a.id)}
        r = addons.run_test(a.id)
        return {**r, "addon": self.addons_get(a.id)}

    # ----------------------------------------------------------- connect --

    def addons_connect(self, addon_id):
        """Start the add-on's sign-in.
        -> {ok, kind: 'oauth', url, message}       browser opened; poll addons_poll(id)
           {ok, kind: 'window', message}           Apple Music window opened; poll addons_poll(id)
           {ok, kind: 'job', job, message}         read it with setup_poll(job, pos), answer with
                                                   setup_input(job, text), stop with setup_cancel(job);
                                                   addons_poll(id) also reports when it's over
           {ok: false, message}                    couldn't start (missing Client ID etc.)"""
        a = self._addon(addon_id)
        try:
            if a.connect == "oauth":
                return self._addon_oauth(a)
            if a.connect == "window":
                return self._addon_music(a)
            if a.connect == "job":
                return self._addon_job(a)
        except Exception as e:  # noqa: BLE001 - shown on the card in plain words
            return {"ok": False, "message": str(e) or type(e).__name__}
        return {"ok": False, "message": f"{a.name} has nothing to connect. Save the fields and test it."}

    def addons_poll(self, addon_id):
        """-> {state: 'idle'|'waiting'|'done'|'error', message, kind, job?, addon: entry}"""
        a = self._addon(addon_id)
        f = self._addon_flows.get(a.id)
        if not f:
            return {"state": "idle", "message": "", "kind": "", "addon": self.addons_get(a.id)}
        if f["kind"] == "oauth" and f.get("flow"):
            fl = f["flow"]
            f["state"], f["message"] = {"waiting": "waiting", "done": "done"}.get(fl.state, "error"), fl.message
            if f["state"] == "done" and not f.get("tested"):
                f["tested"] = True
                addons.run_test(a.id)
        if f["kind"] == "job" and f.get("job"):
            j = setup_flow.SetupMixin._jobs.get(f["job"])
            if j and j.code is not None and f["state"] == "waiting":
                f["state"] = "done" if j.code == 0 else "error"
                f["message"] = f["message"] if j.code == 0 else "It didn't finish. The log says why."
        out = {k: v for k, v in f.items() if k in ("state", "message", "kind", "job")}
        return {**out, "addon": self.addons_get(a.id)}

    def addons_cancel(self, addon_id):
        """Stop a sign-in that's waiting. -> entry"""
        a = self._addon(addon_id)
        f = self._addon_flows.get(a.id) or {}
        if f.get("flow"):
            f["flow"].cancel("Cancelled.")
        if f.get("window"):
            f["cancel"] = True
        if f.get("job") and setup_flow.SetupMixin._jobs.get(f["job"]):
            setup_flow.SetupMixin._jobs[f["job"]].cancel()
        if f.get("state") == "waiting":
            f["state"], f["message"] = "error", "Cancelled."
        return self.addons_get(a.id)

    # ----- OAuth (Spotify, Strava) -----

    def _addon_oauth(self, a):
        miss = [f.label for f in a.fields if f.required and not self._addon_env().get(f.key)]
        if miss:
            return {"ok": False, "message": "Save these first: " + ", ".join(miss) + "."}
        if DRY:
            self._addon_flows[a.id] = {"kind": "oauth", "state": "waiting", "message": "(dry run) waiting"}

            def later():
                time.sleep(2.5)
                self._addon_flows[a.id].update(state="done", message="(dry run) would have opened the browser to sign in")
            threading.Thread(target=later, daemon=True).start()
            return {"ok": True, "kind": "oauth", "url": "", "message": "(dry run) not opening a browser"}
        flow = addons.OAuthFlow(a.id).start()
        if flow.state == "error":
            return {"ok": False, "message": flow.message}
        self._addon_flows[a.id] = {"kind": "oauth", "state": "waiting", "message": flow.message, "flow": flow}
        os.startfile(flow.url)
        return {"ok": True, "kind": "oauth", "url": flow.url,
                "message": "Your browser opened. Approve it there, then come back here."}

    # ----- jobs (Reminders login, voice model download) -----

    def _addon_job(self, a):
        py = addons.python_exe()
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
               "HF_HUB_DISABLE_SYMLINKS_WARNING": "1"}
        if a.id == "reminders":
            label, argv, ok_msg = "Reminders login", [py, str(ROOT / "Atlas" / "icloud.py"), "login"], "Signed in. Reminders work."
            script = [(0.6, "STEP: (dry run) Starting the iCloud login"),
                      (0.6, "ASK: password Apple ID password? (dry run, type anything)"),
                      (0.4, "ASK: code The 6-digit code on your iPhone?"), (0.8, "OK: (dry run) Logged in")]
        elif a.id == "voice_notes":
            label, argv, ok_msg = "Voice model download", [py, str(ROOT / "Atlas" / "voice_notes.py"), "download"], "Voice notes are ready."
            script = fake("STEP: (dry run) Downloading the speech model", "OK: (dry run) Voice notes are ready.")
        else:
            return {"ok": False, "message": f"{a.name} has no sign-in to run."}

        def done(job, a=a, ok_msg=ok_msg):
            if job.code == 0:
                addons.record_test(a.id, True, ok_msg)
            f = self._addon_flows.get(a.id)
            if f and f.get("job") == job.id:
                f["state"] = "done" if job.code == 0 else "error"
                f["message"] = ok_msg if job.code == 0 else "It didn't finish. The log says why."

        job = Job(label, argv=None if DRY else argv, env=env, script=script if DRY else None, on_done=done)
        setup_flow.SetupMixin._jobs[job.id] = job   # where setup_poll / setup_input look
        self._addon_flows[a.id] = {"kind": "job", "state": "waiting", "message": "", "job": job.id}
        return {"ok": True, "kind": "job", "job": job.id, "message": label}

    # ----- Apple Music sign-in window -----

    def _addon_music(self, a):
        if DRY:
            self._addon_flows[a.id] = {"kind": "window", "state": "waiting", "message": "(dry run) waiting"}

            def later():
                time.sleep(2.5)
                self._addon_flows[a.id].update(state="done", message="(dry run) would have opened the Apple Music sign-in window")
            threading.Thread(target=later, daemon=True).start()
            return {"ok": True, "kind": "window", "message": "(dry run) no window"}
        old = self._addon_flows.get(a.id) or {}
        if old.get("state") == "waiting" and old.get("window"):
            try:
                old["window"].show()
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True, "kind": "window", "message": "The Apple Music window is already open."}
        f = {"kind": "window", "state": "waiting", "message": "Sign in to Apple Music in the window that opened.",
             "cancel": False}
        self._addon_flows[a.id] = f
        threading.Thread(target=self.apple_music_signin, args=(f,), daemon=True).start()
        return {"ok": True, "kind": "window", "message": f["message"]}

    def apple_music_signin(self, f: dict, timeout: int = 600) -> None:
        """Open music.apple.com in its own window, wait for the owner to sign
        in, read MusicKit's tokens off the page, save them, close the window.
        The window shares the app's WebView profile, so a second sign-in
        months later is usually one click."""
        import webview
        closed = threading.Event()
        try:
            win = webview.create_window("Sign in to Apple Music", MUSIC_URL, width=1100, height=820,
                                        background_color="#1f1f1f")
            win.events.closed += closed.set
            f["window"] = win
        except Exception as e:  # noqa: BLE001
            f["state"], f["message"] = "error", f"The sign-in window didn't open: {e}"
            return
        f["message"] = "Press Sign In at the top right of the Apple Music window and sign in."
        end = time.time() + timeout
        tokens = {}
        while time.time() < end and not closed.is_set() and not f.get("cancel"):
            time.sleep(2)
            try:
                got = win.evaluate_js(MUSIC_JS) or {}
            except Exception:  # noqa: BLE001 - the page is mid-navigation; try again
                continue
            if isinstance(got, dict) and got.get("user") and got.get("dev"):
                tokens = got
                break
        if not tokens:
            if f.get("cancel"):
                f["state"], f["message"] = "error", "Cancelled."
            elif closed.is_set():
                f["state"], f["message"] = "error", "The window closed before the sign-in finished. Press Sign in to try again."
            else:
                f["state"], f["message"] = "error", "The sign-in timed out. Press Sign in to try again."
            if not closed.is_set():
                try:
                    win.destroy()
                except Exception:  # noqa: BLE001
                    pass
            return
        vals = {"APPLE_MUSIC_DEV_TOKEN": tokens["dev"], "APPLE_MUSIC_USER_TOKEN": tokens["user"], "ADDON_APPLE_MUSIC": "on"}
        sf = str(tokens.get("sf") or "").lower()
        if len(sf) == 2 and sf.isalpha():
            vals["APPLE_MUSIC_STOREFRONT"] = sf
        self._addon_write(vals)
        try:
            win.destroy()
        except Exception:  # noqa: BLE001
            pass
        r = addons.run_test("apple_music")
        f["state"] = "done" if r["ok"] else "error"
        f["message"] = r["message"] if r["ok"] else f"Signed in, but the test failed: {r['message']}"
