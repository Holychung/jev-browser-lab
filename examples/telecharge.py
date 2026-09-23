"""Live Telecharge Lottery + Rush run: sign in with LinkedIn, open Lottery, enter each --show.

uv run --env-file .env python examples/telecharge.py --show "The Great Gatsby" [--show ...] [--when "8:00PM"]

rush.telecharge.com only frames my.socialtoaster.com, and frames are outside the DOM reader, so the
run starts on the framed page itself. Sign-in is LinkedIn only: the automation Chrome profile must
already hold a LinkedIn session (log in there once by hand). Nothing is typed on this site, so no
credential reaches it or a model.

The task runs as ordered stages. Each stage is one narrow goal; a DONE choice only advances the
stage when an independent page check passes. Every Enter click pauses the run: it writes
<output>/pending.json and waits for <output>/approve or <output>/reject to appear. --auto-approve
skips that pause for an entry the user has already approved; code still refuses any other card.
The final result is read again from a freshly loaded Lottery page, not from the run's own tab.

The viewport defaults to 4800 px tall, so the whole list is one observation instead of several
scrolls. --record captures that full height; render_telecharge.py pans to each chosen element.
"""

import argparse
import json
import re
import time
from pathlib import Path

from browser_harness.helpers import cdp

from jev_ultrafast import Agent
from jev_ultrafast.browser import Browser, StalePage
from jev_ultrafast.recorder import Recorder

URL = "https://my.socialtoaster.com/st/campaign_landing/?key=BROADWAY&source=iframe"
LOTTERY = "https://my.socialtoaster.com/st/lottery_select/?key=BROADWAY&source=iframe"
# Blurred in recordings: the account name in the header and the prefilled contact fields.
BLUR = ["#st_user_info_picture_text", "input#email", "input#phone_number"]
SIGNED_IN = "!!document.querySelector('a[href*=\"/st/campaign_logout/\"]')"
# The requested shows' open lottery cards, optionally narrowed to one performance time. The RESULTS
# tab holds past drawings' cards in another block.
CARDS = """((shows, when) => [...document.querySelectorAll('#lottery_block_events .lottery_show')].filter(c =>
  shows.includes(c.querySelector('.lottery_show_title')?.innerText.trim()) && c.innerText.includes(when)))"""
ENTERED = "(c => !!c.querySelector('.entered')?.checkVisibility())"
TICKETS = "(c => c.querySelector('.lottery_show_tickets_input')?.value)"
PERFORMANCES = (
    "(c => c.map(x => ({show: x.querySelector('.lottery_show_title')?.innerText.trim(), "
    "date: x.querySelector('.lottery_show_date')?.innerText.trim(), "
    f"tickets: {TICKETS}(x), entered: {ENTERED}(x)}})))"
)
# Account pages show personal details; stop before their text can be sent to a model.
PRIVATE = re.compile(r"/st/iframe_account_prefs/")
# Signing out ends the session; the contact form rewrites account details.
REFUSED = re.compile(r"\b(log ?out|my account|contact information)\b", re.IGNORECASE)
SUBMIT = re.compile(r"\b(enter|submit|confirm|register|sign up|buy|pay)\b", re.IGNORECASE)
LINKEDIN = re.compile(r"linkedin", re.IGNORECASE)
LOGIN_POPUP = re.compile(r"linkedin\.com|accounts\.google\.com|/st/popup_login_pub/")
MANUAL_LOGIN = re.compile(r"linkedin\.com/(login|checkpoint|uas)|accounts\.google\.com")


def stages(shows, when, tickets):
    cards = f"{CARDS}({json.dumps(shows)}, {json.dumps(when)})"
    which = "lottery card of " + "; ".join(shows) + (f" (only at {when})" if when else "")
    unit = "ticket" if tickets == 1 else "tickets"
    find = (
        "The list is long and runs in date and time order, so other performances can come first: keep "
        f"choosing SCROLL_DOWN until each {which} is on screen. BLOCKED only at the bottom."
    )
    return [
        (
            "Sign in if the page offers Sign In: click Sign In, then Connect with LinkedIn. Never type "
            "into the Email Address or Password fields. DONE once the Logout link is visible.",
            SIGNED_IN,
        ),
        (
            "Open the Lottery page from the navigation. DONE once the list of lottery drawings is shown.",
            "location.pathname.startsWith('/st/lottery_select/') && !!document.querySelector('.lottery_show')",
        ),
        (
            f"Set the ticket count of each {which} to {tickets} with that card's Fewer tickets or More "
            f"tickets button. Do not click Enter yet. DONE once each {which} shows {tickets} {unit}. " + find,
            f"(c => c.length > 0 && c.filter(x => !{ENTERED}(x)).every(x => {TICKETS}(x) === '{tickets}'))({cards})",
        ),
        (
            f"Click Enter on each {which}, one card at a time. Never enter any other show or performance. "
            f"DONE once each {which} shows Lottery Entered! " + find,
            f"(c => c.length > 0 && c.every({ENTERED}))({cards})",
        ),
    ]


STAGE_NAMES = ["Sign in", "Lottery", "Ticket count", "Enter"]


def chosen(agent):
    decision, page = agent.state["decision"], agent.state["page"]
    return next((a for a in page["actions"] if a["id"] == decision["choice"]), None)


def control(action):
    """The control's own name, without the card text prefixed to repeated labels."""
    return action["label"].rsplit(" · ", 1)[-1]


def submits(action):
    return action is not None and action["kind"] == "click" and bool(SUBMIT.search(control(action)))


def card_of(agent, action):
    """Read the lottery card that holds an observed control: its show, time, tickets and state."""
    return agent.browser.evaluate(f"""(e => {{
      const c=e?.closest('.lottery_show');
      return c && {{show: c.querySelector('.lottery_show_title')?.innerText.trim(),
        date: c.querySelector('.lottery_show_date')?.innerText.trim(), tickets: {TICKETS}(c),
        entered: {ENTERED}(c)}};
    }})(window.__jevFast?.nodes.get({int(action["node"])}))""")


def refusal(agent, action, shows, when, tickets, submitted):
    """Why a chosen action must not run, or None. Checked by code before anything executes."""
    if action["kind"] == "fill":
        return "Nothing is typed on this site. Sign in only through Connect with LinkedIn."
    if action["kind"] != "click":
        return None
    if REFUSED.search(control(action)):
        return f"{control(action)!r} is off limits in this run. Choose another operation."
    if submits(action):
        if action["node"] in submitted:
            return "That Enter was already clicked once and is never clicked again. Choose WAIT or DONE."
        card = card_of(agent, action)
        if not card or card["show"] not in shows or when not in (card["date"] or ""):
            wanted = "; ".join(shows)
            return f"That Enter belongs to {card['show'] if card else 'no lottery card'}, not {wanted}."
        if card["tickets"] != str(tickets):
            return f"That {card['show']} card shows {card['tickets']} tickets, not {tickets}. Adjust it first."
    return None


def wait_for_popup(opened_before, mark, appear=5, limit=30):
    """After Connect with LinkedIn, let its window finish. Returns a login URL if it needs a human.

    The window opens only after an AJAX nonce check, so first wait for it to appear.
    """

    def popups():
        return [
            t
            for t in cdp("Target.getTargets")["targetInfos"]
            if t["type"] == "page" and t["targetId"] not in opened_before and LOGIN_POPUP.search(t["url"])
        ]

    deadline = time.monotonic() + appear
    while time.monotonic() < deadline and not popups():
        time.sleep(0.2)
    if not popups():
        return None
    mark("popup", phase="open")
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if not (open_now := popups()):
            mark("popup", phase="closed")
            return None
        time.sleep(0.25)
    return next((t["url"] for t in open_now if MANUAL_LOGIN.search(t["url"])), open_now[0]["url"])


def verify(shows, when, height):
    """Load the Lottery page in a fresh tab, so the server, not the run's own tab, says what was entered."""
    browser = Browser(LOTTERY, (1120, height))
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not browser.evaluate("!!document.querySelector('.lottery_show')"):
            time.sleep(0.25)
        return browser.evaluate(f"{PERFORMANCES}({CARDS}({json.dumps(shows)}, {json.dumps(when)}))")
    finally:
        browser.close()


def wait_for_verdict(folder, action, agent):
    for name in ("approve", "reject"):
        (folder / name).unlink(missing_ok=True)
    decision = agent.state["decision"]
    pending = {
        "label": action["label"],
        "url": agent.state["page"]["url"],
        "confidence": decision["confidence"],
        "target_confidence": decision["target_confidence"],
        "card": card_of(agent, action),
    }
    (folder / "pending.json").write_text(json.dumps(pending, indent=2))
    print(f"PAUSED before clicking {action['label']!r} -- waiting for {folder}/approve or /reject", flush=True)
    while True:
        if (folder / "reject").exists():
            return False
        if (folder / "approve").exists():
            (folder / "approve").unlink()
            (folder / "pending.json").unlink(missing_ok=True)
            return True
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--show", action="append", required=True, help="Show title exactly as listed on the Lottery page; repeatable"
    )
    parser.add_argument("--when", default="", help="Only the performances whose date text includes this")
    parser.add_argument("--tickets", type=int, choices=(1, 2), default=2)
    parser.add_argument(
        "--height", type=int, default=4800, help="Viewport height; tall enough to show the whole lottery list"
    )
    parser.add_argument("--output", default="artifacts/telecharge/latest")
    parser.add_argument("--auto-approve", action="store_true", help="Click Enter on the matching card without pausing")
    parser.add_argument("--record", action="store_true", help="Record a redacted screencast for render_telecharge.py")
    parser.add_argument(
        "--debug-trace", action="store_true", help="Also save each decision's element table (visible labels)"
    )
    args = parser.parse_args()
    plan = stages(args.show, args.when, args.tickets)
    folder = Path(args.output).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stage, false_dones = 0, 0
    submitted = set()
    agent = Agent(URL, plan[stage][0], viewport=(1120, args.height))
    state = agent.state
    recorder = Recorder(agent.browser, folder, BLUR, size=(1120, args.height)) if args.record else None

    def mark(kind, **fields):
        if recorder:
            recorder.mark(kind, **fields)

    started = time.perf_counter()
    cards = f"{CARDS}({json.dumps(args.show)}, {json.dumps(args.when)})"

    def passed():
        try:
            return agent.browser.evaluate(f"!!({plan[stage][1]})") is True
        except StalePage:
            return False

    def advance(reason):
        nonlocal stage, false_dones
        print(f"STAGE {stage + 1}/{len(plan)} {STAGE_NAMES[stage]} complete ({reason})", flush=True)
        mark("stage", index=stage, name=STAGE_NAMES[stage])
        stage, false_dones = stage + 1, 0
        if stage < len(plan):
            state["goal"] = plan[stage][0]
            state["status"] = "ready"

    def note(text):
        """Tell the policy why its last choice was refused, without executing anything."""
        state["goal"] = plan[stage][0] + " NOTE: " + text
        state["decision"] = None
        state["status"] = "ready"

    try:
        while stage < len(plan):
            # A stage the page already satisfies (signed in, 2 tickets by default) costs no model call.
            if state["status"] == "ready" and passed():
                advance("page check passed")
                listed = stage == 2 and agent.browser.evaluate(
                    f"{cards}.map(c => c.querySelector('.lottery_show_title').innerText.trim())"
                )
                missing = [s for s in args.show if s not in listed] if stage == 2 else []
                if missing:
                    titles = agent.browser.evaluate(
                        "[...new Set([...document.querySelectorAll('.lottery_show_title')].map(t=>t.innerText.trim()))]"
                    )
                    print(
                        f"STOPPED: no lottery for {missing}{' at ' + args.when if args.when else ''}. Listed: {titles}",
                        flush=True,
                    )
                    state["status"] = "show_not_listed"
                    break
                continue
            if state["status"] == "done":
                false_dones += 1
                print(f"STAGE {stage + 1}: DONE claimed but the page check failed ({false_dones})", flush=True)
                mark("refused", reason="DONE rejected by page check")
                if false_dones >= 3:
                    state["status"] = "blocked"
                    break
                note("DONE was rejected because this stage is not complete yet. Choose the next operation.")
                continue
            if state["status"] == "blocked":
                chose_blocked = state["decisions"] and state["decisions"][-1]["choice"] == "BLOCKED"
                if not chose_blocked or false_dones >= 3:
                    break
                false_dones += 1
                print(f"STAGE {stage + 1}: BLOCKED rejected ({false_dones})", flush=True)
                mark("refused", reason="BLOCKED rejected: controls may be off screen")
                note("BLOCKED was rejected. The needed control may be off screen: use SCROLL_DOWN or SCROLL_UP.")
                continue
            try:
                agent.command("predict")
                decision = state["decision"]
                action = chosen(agent)
                if action and "rect" in action:
                    # Where the chosen element is, so the rendered video can pan to it before it acts.
                    mark(
                        "target",
                        rect=action["rect"],
                        operation=decision["operation"],
                        label=action["label"],
                        confidence=decision["confidence"],
                    )
                reason = action and refusal(agent, action, args.show, args.when, args.tickets, submitted)
                if reason:
                    print(f"REFUSED {action['label']!r}: {reason}", flush=True)
                    mark("refused", reason=reason)
                    note(reason)
                    continue
                if submits(action) and args.auto_approve:
                    mark("gate", label=action["label"], approved="in advance")
                elif submits(action):
                    mark("gate", label=action["label"], confidence=decision["confidence"])
                    if recorder:
                        recorder.pause()
                    before = time.perf_counter()
                    approved = wait_for_verdict(folder, action, agent)
                    paused = time.perf_counter() - before
                    started += paused
                    state["started_at"] += paused
                    if not approved:
                        state["status"] = "rejected"
                        break
                    if recorder:
                        recorder.resume()
                opened = {t["targetId"] for t in cdp("Target.getTargets")["targetInfos"]}
                # A StalePage from act is raised before any input, so the Enter stays clickable.
                agent.command("act", {"fingerprint": state["page"]["fingerprint"]})
                mark("acted")
                if submits(action):
                    submitted.add(action["node"])
                    # The entry is an AJAX post: confirm this card flips to entered, or stop.
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline and not (card_of(agent, action) or {}).get("entered"):
                        time.sleep(0.25)
                    card = card_of(agent, action)
                    print(f"ENTRY {'confirmed' if card and card['entered'] else 'NOT confirmed'}: {card}", flush=True)
                    mark("entry", card=card)
                    if not (card and card["entered"]):
                        state["status"] = "entry_not_confirmed"
                        break
                    state["page"] = state["browser"].observe(screenshot=False)
                if stage == 0 and action and action["kind"] == "click" and LINKEDIN.search(control(action)):
                    stuck = wait_for_popup(opened, mark)
                    if stuck:
                        state["status"] = "needs_manual_login"
                        print(
                            f"STOPPED: the LinkedIn window needs a human at {stuck.split('?')[0]}. Sign in "
                            "there by hand, then run again.",
                            flush=True,
                        )
                        break
                    # The closing window refreshes this page; let that navigation finish before observing.
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not passed():
                        time.sleep(0.25)
                    if not passed():
                        # The session cookie is set either way; a reload shows it (navigation, not input).
                        print("LinkedIn window closed without a refresh; reloading the page", flush=True)
                        mark("reload")
                        state["browser"].call("Page.reload")
                        while time.monotonic() < deadline + 10 and not passed():
                            time.sleep(0.25)
                    state["page"] = state["browser"].observe(screenshot=False)
                if decision["choice"] not in {"DONE", "BLOCKED"}:
                    state["goal"] = plan[stage][0]
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
                f"{last.get('action', decision['choice'])[:70]!r:<74} conf={decision['confidence']:.2f} "
                f"tgt={decision['target_confidence'] if decision['target_confidence'] is not None else '-'} "
                f"jev={decision['latency_ms']}ms",
                flush=True,
            )
            mark(
                "step",
                operation=decision["operation"],
                label=last.get("action", decision["choice"]),
                confidence=decision["confidence"],
                target_confidence=decision["target_confidence"],
                jev_ms=decision["latency_ms"],
            )
            if state["status"] == "done" and passed():
                advance("DONE, check passed")
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
        elapsed = round((time.perf_counter() - started) * 1000)
        if recorder:
            snapshot["recording"] = recorder.stop()
            print("recording:", {k: v for k, v in snapshot["recording"].items() if k != "events"})
        agent.close()
        # Independent of every check above: what a fresh load of the Lottery page shows.
        verified = verify(args.show, args.when, args.height) if state["history"] else None
        print("VERIFIED after reload:", verified, flush=True)
        summary = {
            "status": state["status"],
            "stages_completed": f"{stage}/{len(plan)}",
            "final_url": state["page"]["url"],
            "shows": args.show,
            "when": args.when,
            "tickets": args.tickets,
            "auto_approve": args.auto_approve,
            "viewport": [1120, args.height],
            "verified_after_reload": verified,
            "actions": len(state["history"]),
            "jev_calls": len(snapshot["decisions"]),
            "text_calls": len(snapshot["text_calls"]),
            "cost_usd": round(cost, 6),
            "elapsed_ms_excluding_pauses": elapsed,
        }
        snapshot["page"] = {k: state["page"].get(k) for k in ("url", "title")}
        (folder / "state.json").write_text(json.dumps({**snapshot, "summary": summary}, indent=2, default=str))
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
