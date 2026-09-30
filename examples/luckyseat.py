"""Live Lucky Seat run: log in, pick New York, open a show (--show), fill its lottery form.

uv run --env-file .env python examples/luckyseat.py

The task runs as ordered stages. Each stage is one narrow goal; a DONE choice only advances the
stage when an independent page check passes. The final stage also reads this show's entries on a
fresh Results page before the submit is approved and again afterwards; only a new entry passes.

Credentials come from LOGIN_EMAIL / LOGIN_PASSWORD and are typed by code, never by a model.
Any click that looks like a submission pauses the run: it writes <output>/pending.json and waits
for <output>/approve or <output>/reject to appear.
"""

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path

from jev_ultrafast import Agent, Browser, captcha_status, click_recaptcha_checkbox
from jev_ultrafast.browser import StalePage
from jev_ultrafast.recorder import Recorder

# Start on the login route: a client-side jump from /home swaps the URL before the form renders.
URL = "https://www.luckyseat.com/account/login"
POPUPS = (
    " Accept the cookie notice if it blocks the page. Close unrelated pop-ups such as a newsletter "
    "sign-up without filling or submitting them."
)
def success_check(show):
    return """(() => {
      const text=(document.body?.innerText||'').replace(/\\s+/g,' ');
      const confirmation=/entries?.{0,80}(?:successfully submitted|received|confirmed)/i.test(text) ||
        /entries?.{0,80}(?:has|have) been submitted/i.test(text) ||
        /(?:successfully submitted|confirmation).{0,80}entries?/i.test(text);
      const listed=location.pathname.startsWith('/dash/results') &&
        text.includes(__SHOW__) && /\\d+\\s+ticket(?:\\(s\\))?/i.test(text);
      return confirmation || listed;
    })()""".replace("__SHOW__", json.dumps(show))
SUBMISSION_EVIDENCE = """(() => {
  const selector='[role="dialog"],[role="alert"],mat-dialog-container,.mat-mdc-dialog-container,'+
    '.modal,.toast,.alert,mat-error,.mat-mdc-form-field-error,.invalid-feedback,'+
    'mat-snack-bar-container,.mat-mdc-snack-bar-container,.mdc-snackbar,[class*="snack-bar"]';
  const visible=e => e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const messages=[...document.querySelectorAll(selector)].filter(visible)
    .map(e => e.innerText.trim()).filter(Boolean).map(s => s.slice(0,500));
  const invalid=[...document.querySelectorAll('input,textarea,select')].filter(e => visible(e) && !e.checkValidity())
    .map(e => ({name:e.labels?.[0]?.innerText.trim() || e.getAttribute('aria-label') || e.name || e.type,
      message:e.validationMessage}));
  return {messages:[...new Set(messages)],invalid};
})()"""


# The page's own fetch/XHR calls from the moment the log is armed: method, origin + path, and status.
# Recording happens in the page, so it neither competes with the Recorder for CDP events nor loses
# the submit request when a burst of later requests overflows the daemon's shared event buffer.
# Bodies and query strings are never read.
ARM_NETWORK_LOG = """(() => {
  const log = window.__jevNetwork ||= {entries: [], installed: false};
  log.entries = [];
  if (log.installed) return true;
  log.installed = true;
  const clean = url => { try { const u = new URL(url, location.href); return u.origin + u.pathname; }
    catch { return String(url).split(/[?#]/)[0]; } };
  const record = (method, url) => {
    const entry = {method: String(method || 'GET').toUpperCase(), url: clean(url), status: null};
    log.entries.push(entry);
    return entry;
  };
  const fetch0 = window.fetch;
  window.fetch = function (input, init) {
    const request = input instanceof Request ? input : null;
    const entry = record(init?.method || request?.method, request ? request.url : input);
    // A no-cors beacon (analytics) resolves opaque with status 0; that is not a failed request.
    return fetch0.apply(this, arguments).then(
      response => { entry.status = response.type === 'opaque' ? 'opaque' : response.status; return response; },
      error => { entry.status = 0; throw error; });
  };
  const open0 = XMLHttpRequest.prototype.open, send0 = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__jevRequest = [method, url];
    return open0.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    if (this.__jevRequest) {
      const entry = record(...this.__jevRequest);
      this.addEventListener('loadend', () => { entry.status = this.status; });
    }
    return send0.apply(this, arguments);
  };
  return true;
})()"""
NETWORK_LOG = "window.__jevNetwork ? window.__jevNetwork.entries : null"


def arm_submission_network(agent):
    """Start a clean, submit-only request log in the page; it never reads bodies or query strings."""
    agent.browser.evaluate(ARM_NETWORK_LOG)


def submission_network(agent, settle=1.0, timeout=10.0):
    """Wait for the requests that followed the approved click, then keep non-GET and failed ones.

    A request still in flight at the timeout is reported as pending rather than dropped. A full page
    load discards the in-page log, and that is reported too.
    """
    started, entries = time.monotonic(), []
    while True:
        try:
            current = agent.browser.evaluate(NETWORK_LOG)
        except StalePage:
            current = None
        if current is None:
            return {"navigated": True, "complete": False, "requests": entries}
        entries = current
        elapsed = time.monotonic() - started
        in_flight = any(e["status"] is None for e in entries)
        if (elapsed >= settle and not in_flight) or elapsed >= timeout:
            break
        time.sleep(0.1)

    def kept(entry):
        status = entry["status"]
        if status == "opaque":
            return False
        return entry["method"] != "GET" or status is None or status == 0 or status >= 400

    requests = [{**e, "status": "pending" if e["status"] is None else e["status"]} for e in entries if kept(e)]
    return {"navigated": False, "complete": not in_flight, "requests": requests}


RESULTS_URL = "https://www.luckyseat.com/dash/results"
# One Results entry is the smallest element that names the show and holds exactly one ticket count,
# e.g. "Hadestown | New York, NY | Oct 1 at 7:00 PM | 1 ticket(s)". The count's digit sits in its own
# span, so counts are read from rendered ancestor text, not single text nodes. Lines after the count
# (drawing date, status) are left out so a drawing that finishes between two reads does not look like
# a new entry.
RESULT_ROWS = """(show => {
  if (!location.pathname.startsWith('/dash/results') || document.readyState !== 'complete') return null;
  const count=/\\d+\\s+ticket(?:\\(s\\))?/i, target=show.toLowerCase(), rows=new Set();
  const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  let node;
  while ((node=walker.nextNode())) {
    if (!/ticket/i.test(node.textContent)) continue;
    for (let a=node.parentElement; a && a!==document.body; a=a.parentElement) {
      const text=a.innerText||'', counts=(text.match(/\\d+\\s+ticket/gi)||[]).length;
      if (counts>1) break;
      if (counts===1 && text.toLowerCase().includes(target)) { rows.add(a); break; }
    }
  }
  return [...rows].map(row => {
    const lines=row.innerText.split('\\n').map(s=>s.trim()).filter(Boolean);
    return lines.slice(0, lines.findIndex(line=>count.test(line))+1).join(' | ');
  });
})"""


def results_entries(show, open_browser=Browser, timeout=15.0, still=1.5):
    """Read this show's entries from a fresh Results tab: an independent view of the account.

    Returns None when the Results list never settles (signed out, still loading). The list counts as
    settled once its entries and the page text have held still for `still` seconds with no request in
    flight: an empty list read before the entries request returns would make old entries look new.
    """
    browser = open_browser("about:blank")
    try:
        # Log the page's requests from its first script on, so the entries request cannot be missed.
        browser.call("Page.addScriptToEvaluateOnNewDocument", source=ARM_NETWORK_LOG)
        browser.call("Page.navigate", url=RESULTS_URL)
        read = f"""(() => [({RESULT_ROWS})({json.dumps(show)}), (document.body?.innerText||'').length,
          (window.__jevNetwork?.entries||[]).filter(e => e.status===null).length])()"""
        last, since, deadline = None, None, time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                current = browser.evaluate(read)
            except StalePage:
                current = None
            if current is None or current[0] is None or current[2] or current != last:
                last, since = current, time.monotonic()
            elif time.monotonic() - since >= still:
                return current[0]
            time.sleep(0.25)
        return None
    finally:
        browser.close()


def new_entries(before, after):
    """Entries on the Results page after the submit that were not there before it."""
    return list((Counter(after) - Counter(before)).elements())


def stages(show):
    """(goal, independent check) per stage. A check is JS returning true once the stage is complete."""
    on_show = (
        f"location.pathname.startsWith('/dash/shows/') && document.body.innerText.includes({json.dumps(show)}) "
        "&& document.querySelectorAll('input[type=checkbox]').length > 0"
    )
    return [
        (
            "Log in to Lucky Seat. Choose TYPE_TEXT on the Email and Password fields; their values are "
            "filled automatically. Then click Log in. DONE once the login form is gone." + POPUPS,
            "!location.pathname.startsWith('/account/login') && !document.querySelector('input[type=password]')",
        ),
        (
            f"Set the city to New York and open the {show} event. If it is not listed, change the category "
            f"filter. DONE once the {show} lottery page is open." + POPUPS,
            on_show,
        ),
        (
            f"On this {show} lottery page, click the Select All button under Select your performance, so "
            "that every performance checkbox is checked. Scroll to find it if needed. Do not touch the "
            "ticket count or Submit Entry yet. DONE once every performance checkbox is checked." + POPUPS,
            "(b => b.length > 0 && b.every(x => x.checked))([...document.querySelectorAll('input[type=checkbox]')])",
        ),
        (
            "Set Select number of tickets to 1. Leave the performance checkboxes as they are and do not "
            "click Submit Entry yet. DONE once the ticket count shows 1." + POPUPS,
            "[...document.querySelectorAll('input[type=number]')].some(i => i.value === '1')",
        ),
        (
            "Click Submit Entry. DONE once the page confirms the lottery entry." + POPUPS,
            success_check(show),
        ),
    ]


STAGES = stages("Hadestown")
STAGE_NAMES = ["Log in", "New York → Hadestown", "Select all performances", "1 ticket", "Submit entry"]
# Blurred in recordings: the typed email, the password's length, and the account name in the header.
BLUR = ["input[type=email]", "input[type=password]", "header .menu-item-has-children > a"]
# Account pages show personal details; stop before their text can be sent to a model.
PRIVATE = re.compile(r"/account/(?!login)")
SUBMIT = re.compile(r"\b(enter|submit|confirm|register|sign up|buy|pay)\b", re.IGNORECASE)


def gated(agent):
    """A click that may submit something, outside the login form, needs a human decision."""
    decision, page = agent.state["decision"], agent.state["page"]
    action = next((a for a in page["actions"] if a["id"] == decision["choice"]), None)
    if not action or action["kind"] != "click" or not SUBMIT.search(action["label"]):
        return None
    if any(a.get("input_type") == "password" for a in page["actions"]):
        return None
    return action


def premature_login(agent):
    """Clicking a login form's button while its password field is empty wastes a login attempt."""
    decision, page = agent.state["decision"], agent.state["page"]
    action = next((a for a in page["actions"] if a["id"] == decision["choice"]), None)
    if not action or action["kind"] != "click" or action.get("role") != "button":
        return False
    return any(a.get("input_type") == "password" and not a.get("value") for a in page["actions"])


CAPTCHA_NOTES = {
    "challenge": "CAPTCHA image challenge is visible and needs manual completion",
    "covered": "CAPTCHA checkbox is covered by another element; it was not clicked",
    "pending": "CAPTCHA checkbox was clicked once but issued no token; complete it by hand",
    "unavailable": "CAPTCHA checkbox is not in view; complete it by hand",
}


def captcha_blocks_approval(status):
    """Only a visible reCAPTCHA checkbox must hold a token before the click.

    Invisible reCAPTCHA issues its token when the gated button is pressed, and other vendors keep
    theirs in their own field, so requiring a g-recaptcha-response token first could never be met.
    For those the person reading pending.json decides.
    """
    return status["checkbox"] and not status["solved"]


def wait_for_verdict(folder, action, agent):
    for name in ("approve", "reject"):
        (folder / name).unlink(missing_ok=True)
    decision, page = agent.state["decision"], agent.state["page"]
    def form_state():
        return agent.browser.evaluate("""(() => {
          const boxes=[...document.querySelectorAll('input[type=checkbox]')];
          return {checked: boxes.filter(b=>b.checked).length, total: boxes.length,
            checked_labels: boxes.filter(b=>b.checked).map(b=>b.labels?.[0]?.innerText.trim()),
            numbers: [...document.querySelectorAll('input[type=number]')].map(i=>i.value)};
        })()""")

    pending = {
        "label": action["label"],
        "url": page["url"],
        "confidence": decision["confidence"],
        "target_confidence": decision["target_confidence"],
        # Whole-form state, including controls scrolled out of view. No page text: see main().
        "form": form_state(),
        "captcha": captcha_status(agent.browser),
    }
    agent.browser.call("Page.bringToFront")
    if captcha_blocks_approval(pending["captcha"]):
        pending["captcha"] = click_recaptcha_checkbox(agent.browser)
    (folder / "pending.json").write_text(json.dumps(pending, indent=2))
    if pending["captcha"].get("outcome") in CAPTCHA_NOTES:
        print(CAPTCHA_NOTES[pending["captcha"]["outcome"]], flush=True)
    print(f"PAUSED before clicking {action['label']!r} -- waiting for {folder}/approve or /reject", flush=True)
    while True:
        if (folder / "reject").exists():
            return False
        if (folder / "approve").exists():
            captcha = captcha_status(agent.browser)
            if captcha_blocks_approval(captcha):
                (folder / "approve").unlink()
                pending.update(form=form_state(), captcha=captcha)
                (folder / "pending.json").write_text(json.dumps(pending, indent=2))
                print(f"REFUSED: solve the visible CAPTCHA before approving {action['label']}", flush=True)
                continue
            (folder / "approve").unlink()
            (folder / "pending.json").unlink(missing_ok=True)
            return True
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/luckyseat/latest")
    parser.add_argument("--record", action="store_true", help="Record a redacted screencast for render_luckyseat.py")
    parser.add_argument("--show", default="Hadestown", help="Event title exactly as listed on Lucky Seat")
    parser.add_argument(
        "--debug-trace", action="store_true", help="Also save each decision's element table (visible labels)"
    )
    args = parser.parse_args()
    global STAGES, STAGE_NAMES
    STAGES = stages(args.show)
    STAGE_NAMES = [*STAGE_NAMES[:1], f"New York → {args.show}", *STAGE_NAMES[2:]]
    folder = Path(args.output).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stage, false_dones = 0, 0
    # Keep click targets inside Chrome's physical compositor viewport. An oversized emulated
    # viewport can expose a button to the snapshot while native mouse events below the real window
    # silently miss it; ordinary scrolling keeps observation and execution geometry aligned.
    agent = Agent(URL, STAGES[stage][0], viewport=(1120, 780), background=False)
    state = agent.state
    recorder = Recorder(agent.browser, folder, BLUR) if args.record else None

    def mark(kind, **fields):
        if recorder:
            recorder.mark(kind, **fields)

    started = time.perf_counter()
    # This show's Results entries, read in a fresh tab before the first final-stage click is approved.
    # Earlier entries for the same show are already listed, so only a difference proves this submit.
    baseline, verification, submissions = None, None, []

    def verified_entry():
        nonlocal started, verification
        if baseline is None:
            print("RESULTS CHECK: no Results baseline was read before the submit", flush=True)
            return False
        # The read is verification, not agent work: keep it out of the elapsed time like a pause.
        before = time.perf_counter()
        after = results_entries(args.show)
        reading = time.perf_counter() - before
        started += reading
        state["started_at"] += reading
        new = new_entries(baseline, after) if after is not None else []
        verification = {"baseline": len(baseline), "after": None if after is None else len(after), "new": new}
        print(f"RESULTS CHECK: {verification}", flush=True)
        return bool(new)

    def passed():
        check = STAGES[stage][1]
        try:
            ok = check is not None and agent.browser.evaluate(f"!!({check})") is True
        except StalePage:
            return False
        # The final page check only says the page looks finished; a fresh Results read decides.
        return ok and (stage < len(STAGES) - 1 or verified_entry())

    def advance(reason):
        nonlocal stage, false_dones
        print(f"STAGE {stage + 1}/{len(STAGES)} complete ({reason})", flush=True)
        mark("stage", index=stage, name=STAGE_NAMES[stage])
        stage, false_dones = stage + 1, 0
        if stage < len(STAGES):
            state["goal"] = STAGES[stage][0]
            state["status"] = "ready"

    def note(text):
        """Tell the policy why its last choice was refused, without executing anything."""
        state["goal"] = STAGES[stage][0] + " NOTE: " + text
        state["decision"] = None
        state["status"] = "ready"

    try:
        while stage < len(STAGES):
            if state["status"] == "done":
                if STAGES[stage][1] is None or passed():
                    advance("DONE, check passed")
                    continue
                false_dones += 1
                print(f"STAGE {stage + 1}: DONE claimed but the page check failed ({false_dones})", flush=True)
                mark("refused", reason="DONE rejected by page check")
                if false_dones >= 3:
                    state["status"] = "blocked"
                    break
                note("DONE was rejected because this stage is not complete yet. Choose the next operation.")
            if state["status"] == "blocked":
                chose_blocked = state["decisions"] and state["decisions"][-1]["choice"] == "BLOCKED"
                if not chose_blocked or false_dones >= 3:
                    break
                false_dones += 1
                print(f"STAGE {stage + 1}: BLOCKED rejected ({false_dones})", flush=True)
                mark("refused", reason="BLOCKED rejected: controls may be off screen")
                note(
                    "BLOCKED was rejected. The needed control may be off screen: use SCROLL_UP to reach "
                    "filters at the top of the page, or SCROLL_DOWN, then continue."
                )
                continue
            try:
                agent.command("predict")
                decision = state["decision"]
                if premature_login(agent):
                    print("REFUSED: login button clicked while the Password field is empty", flush=True)
                    mark("refused", reason="Login clicked with an empty password")
                    note("The Password field is still empty. Choose TYPE_TEXT on the Password field first.")
                    continue
                action, final = gated(agent), stage == len(STAGES) - 1
                if action:
                    mark("gate", label=action["label"], confidence=decision["confidence"])
                    if recorder:
                        recorder.pause()
                    before = time.perf_counter()
                    if final and baseline is None:
                        baseline = results_entries(args.show)
                        print(f"RESULTS BASELINE: {baseline if baseline is None else len(baseline)}", flush=True)
                    approved = wait_for_verdict(folder, action, agent)
                    paused = time.perf_counter() - before
                    started += paused
                    state["started_at"] += paused
                    if not approved:
                        state["status"] = "rejected"
                        break
                    if recorder:
                        recorder.resume()
                    if final:
                        arm_submission_network(agent)
                agent.command("act", {"fingerprint": state["page"]["fingerprint"]})
                if action and final:
                    network = submission_network(agent)
                    shown = agent.browser.evaluate(SUBMISSION_EVIDENCE)
                    submissions.append({"label": action["label"], "page": shown, "network": network})
                    (folder / "submission.json").write_text(json.dumps(submissions, indent=2))
                    # Page messages stay in the file: they can name the account holder.
                    requests = ", ".join(f"{r['method']} {r['url']} {r['status']}" for r in network["requests"])
                    print(
                        f"SUBMISSION EVIDENCE ({action['label']}): requests=[{requests}] "
                        f"complete={network['complete']} navigated={network['navigated']} "
                        f"messages={len(shown['messages'])} invalid={len(shown['invalid'])}",
                        flush=True,
                    )
                if decision["choice"] not in {"DONE", "BLOCKED"}:
                    state["goal"] = STAGES[stage][0]
                if PRIVATE.search(state["page"]["url"]):
                    state["status"] = "stopped_private_page"
                    print(f"STOPPED: reached private page {state['page']['url']}", flush=True)
                    break
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=False)
                continue
            last = state["history"][-1] if state["history"] and decision["choice"] not in {"DONE", "BLOCKED"} else {}
            print(
                f"{round((time.perf_counter() - started) * 1000):>6} ms  {decision['operation']:<11} "
                f"{last.get('action', decision['choice'])[:60]!r:<64} conf={decision['confidence']:.2f} "
                f"tgt={decision['target_confidence'] if decision['target_confidence'] is not None else '-'} "
                f"jev={decision['latency_ms']}ms text={last.get('text')!r}",
                flush=True,
            )
            mark(
                "step",
                operation=decision["operation"],
                label=last.get("action", decision["choice"]),
                confidence=decision["confidence"],
                target_confidence=decision["target_confidence"],
                jev_ms=decision["latency_ms"],
                text=last.get("text"),
            )
            if state["status"] == "ready" and passed():
                advance("page check passed")
    finally:
        snapshot = agent.snapshot()
        # Traces stay on disk: keep no page text (it can show the account holder's details).
        # Element tables carry visible labels, so they are kept only with --debug-trace.
        for d in snapshot["decisions"]:
            request = d.pop("request", {})
            if args.debug_trace:
                d["elements"] = request.get("state", {}).get("elements", [])
        if not args.debug_trace:
            snapshot.pop("elements", None)
        cost = sum(d["usage"].get("cost", 0) for d in snapshot["decisions"])
        cost += sum(t["usage"].get("cost", 0) for t in snapshot["text_calls"])
        summary = {
            "status": state["status"],
            "show": args.show,
            "stages_completed": f"{stage}/{len(STAGES)}",
            "final_url": state["page"]["url"],
            "actions": len(state["history"]),
            "jev_calls": len(snapshot["decisions"]),
            "text_calls": len(snapshot["text_calls"]),
            "cost_usd": round(cost, 6),
            "elapsed_ms_excluding_pauses": round((time.perf_counter() - started) * 1000),
            "results_baseline": None if baseline is None else len(baseline),
            "results_check": verification,
        }
        snapshot["page"] = {k: state["page"].get(k) for k in ("url", "title")}
        if recorder:
            snapshot["recording"] = recorder.stop()
            print("recording:", {k: v for k, v in snapshot["recording"].items() if k != "events"})
        (folder / "state.json").write_text(json.dumps({**snapshot, "summary": summary}, indent=2, default=str))
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
