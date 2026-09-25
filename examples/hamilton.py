"""Hamilton app lottery on an Android emulator: open Lottery, then enter each requested performance.

uv run --env-file .env --extra android python examples/hamilton.py --all --dry-run
uv run --env-file .env --extra android python examples/hamilton.py --performance "October 6, 2026 7:00pm"

The Hamilton lottery is app-only. Install com.hamilton.app on the emulator and sign in there once by
hand; the run types nothing, so no credential reaches the app or a model.

Broadway Direct's lottery terms, which the app links as its Official Rules, say an entry may be
disqualified, and the entrant barred from future lotteries, for "automated or programmed methods of
Lottery entry". Read them before running this without --dry-run.

Each performance is a sequence of narrow stages; a DONE choice only advances a stage when a code check
of the screen passes. Every Submit Your Entry pauses: the run writes <output>/pending.json and waits for
<output>/approve or <output>/reject. --auto-approve skips that pause for performances the user already
chose; --dry-run refuses every submit and goes back to the list, so a whole run can be tested without
entering. Code refuses any control outside the flow, another performance's card, unticking a box, and a
second submit for the same performance.

After the run the app is restarted and the Broadway list is read again from the top, so the reported
result comes from a fresh load, not from the run's own screens.
"""

import argparse
import json
import re
import time
from pathlib import Path

from jev_ultrafast import Agent
from jev_ultrafast.android import TAB, Device, ScreenRecording, contains
from jev_ultrafast.browser import StalePage

PACKAGE = "com.hamilton.app"
COUNTDOWN = re.compile(r"\b\d{2,3}:\d{2}:\d{2}\b")
PERFORMANCE = re.compile(r"PERFORMANCE TIME ([A-Z][a-z]+ \d{1,2}, \d{4} \d{1,2}:\d{2}[ap]m)")
# An entered performance's card replaces its details: "YOU’VE ENTERED! ... lottery for October 7, 7:00pm".
ENTERED = re.compile(r"YOU.VE ENTERED!.*? for ([A-Z][a-z]+ \d{1,2}), (\d{1,2}:\d{2}[ap]m)")
DATE = re.compile(r"[A-Z][a-z]+ \d{1,2}, \d{4}")
TIME = re.compile(r"\d{1,2}:\d{2}[ap]m")
# Controls any stage may use. Everything not allowed below is refused before it executes.
NAVIGATION = {"Back", "Lottery", "No thanks"}
# After this performance's one submit, a confirmation may need closing. Only these names, only then.
DISMISS = {"OK", "Ok", "Okay", "Close", "Done", "Got it", "Dismiss", "Continue"}


NAME_NOTICE = "Winning tickets will be issued to the name above"


def private(nodes):
    """The entrant's name, kept from the model and blurred in recordings.

    On the entry page it sits between the Broadway Details button and a notice about "the name above".
    Either neighbour can be off screen (just after the page opens the notice is below the fold), so
    the name is found from whichever one is visible.
    """
    labeled = [n for n in nodes if n["label"]]
    hidden = set()
    for i, n in enumerate(labeled):
        if n["label"].startswith(NAME_NOTICE) and i:
            hidden.add(labeled[i - 1]["index"])
        if n["label"] == "Broadway Details" and i + 1 < len(labeled) and on_entry(nodes):
            name = labeled[i + 1]
            if not name["clickable"] and not name["label"].startswith(NAME_NOTICE):
                hidden.add(name["index"])
    return hidden


def tap_point(node):
    """The home screen's Lottery shortcut takes taps on its icon and label, not on the padding at the center
    of its bounds (a tap at the center does nothing; one a quarter in from the top left opens the list)."""
    if node["label"] == "Lottery" and node["class"] == "ImageView":
        x1, y1, x2, y2 = node["rect"]
        return x1 + (x2 - x1) // 4, y1 + (y2 - y1) // 4
    return None


def labelled(nodes, label):
    return next((n for n in nodes if n["label"] == label), None)


def overlay(nodes):
    """An in-app message (Braze) is a WebView over the screen; the nodes under it cannot be tapped."""
    return any("inappmessage" in n["resource_id"] for n in nodes)


def on_list(nodes):
    return any(n["selected"] and n["label"].startswith("BROADWAY") and TAB.search(n["label"]) for n in nodes)


def on_entry(nodes):
    marks = any(n["label"] in ("Number of Tickets", "Submit Your Entry") for n in nodes)
    return marks and not on_list(nodes)


def entry_performance(nodes):
    """The performance an entry page is for: its first date label followed by a time label."""
    labels = [n["label"] for n in nodes]
    for date, hour in zip(labels, labels[1:]):
        if DATE.fullmatch(date) and TIME.fullmatch(hour):
            return f"{date} {hour}"
    return None


def tickets_shown(nodes):
    labels = [n["label"] for n in nodes]
    if "Number of Tickets" not in labels:
        return None
    value = next((label for label in labels[labels.index("Number of Tickets") + 1 :] if label), "")
    return int(value) if value.isdigit() else None


def submit_enabled(nodes):
    button = labelled(nodes, "Submit Your Entry")
    return button is not None and button["enabled"]


def entry_boxes(nodes):
    return [n for n in nodes if n["checkable"]] if on_entry(nodes) else []


def card_of(nodes, node):
    """The performance of the list card that holds a control."""
    for card in nodes:
        match = card is not node and card["clickable"] and PERFORMANCE.search(card["label"])
        if match and contains(card["rect"], node["rect"]):
            return match.group(1)
    return None


def undated(performance):
    """'October 7, 2026 7:00pm' -> 'October 7 7:00pm', the form an entered card names it by."""
    return re.sub(r", \d{4} ", " ", performance)


def read_cards(nodes):
    """Every card on screen: performance -> the labels inside it. Entered cards are keyed undated."""
    cards = {}
    for card in nodes:
        entered = ENTERED.search(card["label"])
        if entered:
            cards[f"{entered.group(1)} {entered.group(2)}"] = {"enter_now": False, "entered": True,
                                                               "inside": [card["label"]]}
            continue
        match = card["clickable"] and PERFORMANCE.search(card["label"])
        if match:
            inside = [n["label"] for n in nodes if n is not card and n["label"] and contains(card["rect"], n["rect"])]
            cards[match.group(1)] = {"enter_now": "Enter Now" in inside, "entered": False, "inside": inside}
    return cards


OPEN_LIST = (
    "Open the Broadway lottery list: on the home screen CLICK Lottery; on an entry page CLICK Back; on another "
    "lottery tab CLICK BROADWAY. DONE once the BROADWAY tab is shown.",
    on_list,
)


def stages(target, tickets, submitted):
    find = (
        f"Cards run in date and time order, earliest first, and Enter Now is at the bottom of each card. If the "
        f"{target} card is on screen, or continues below the screen, without its Enter Now: SCROLL_DOWN. If only "
        f"performances before {target} are on screen: SCROLL_DOWN. If only performances after it: SCROLL_UP."
    )
    return [
        (
            f"CLICK Enter Now on the card whose PERFORMANCE TIME is {target}, and on no other card. {find} "
            f"DONE once the entry page for {target} is shown.",
            lambda n: on_entry(n) and entry_performance(n) == target,
        ),
        (
            f"Set Number of Tickets to {tickets} with the buttons beside it. Do not click Submit Your Entry. "
            f"DONE once it shows {tickets}.",
            lambda n: tickets_shown(n) == tickets,
        ),
        (
            "Tick both boxes on this entry page: accepting the Terms of Service and official rules, and having "
            "reviewed the profile. Never untick a box. Do not click Submit Your Entry yet. SCROLL_DOWN if a box is "
            "not on screen. DONE once both are ticked.",
            lambda n: len(entry_boxes(n)) == 2 and all(b["checked"] for b in entry_boxes(n)),
        ),
        (
            f"CLICK Submit Your Entry for {target}. DONE once the entry is confirmed.",
            # After the one submit: the button is gone or no longer enabled. The final check is the fresh list.
            lambda n: target in submitted and not overlay(n) and not submit_enabled(n),
        ),
        (
            "Go back to the Broadway lottery list: close any confirmation or CLICK Back. DONE once the list is shown.",
            on_list,
        ),
    ]


STAGE_NAMES = ["Entry page", "Tickets", "Boxes", "Submit", "Back to list"]


def refusal(nodes, action, target, tickets, submitted):
    """Why a chosen action must not run, or None. Checked by code before anything executes."""
    if action["kind"] == "fill":
        return "Nothing is typed in this run."
    if action["kind"] != "click":
        return None
    node = nodes[action["node"]]
    label = node["label"]
    if label in NAVIGATION or (label in DISMISS and target in submitted):
        return None
    if TAB.search(label):
        return None if label.startswith("BROADWAY") else "Only the BROADWAY lottery is in this run."
    if label == "Enter Now":
        card = card_of(nodes, node)
        if target is None or card != target:
            return f"That Enter Now belongs to {card or 'no card'}, not {target or 'this stage'}."
        return None
    if not on_entry(nodes) or target is None:
        return f"{label or 'That control'!r} is outside this run. Choose another operation."
    shown = entry_performance(nodes)
    if shown and shown != target:
        return f"This entry page is for {shown}, not {target}. CLICK Back."
    if node["checkable"]:
        return "That box is already ticked; never untick it." if node["checked"] else None
    if not label and "Number of Tickets" in action["label"]:
        return f"Number of Tickets already shows {tickets}." if tickets_shown(nodes) == tickets else None
    if label == "Submit Your Entry":
        boxes = entry_boxes(nodes)
        if target in submitted:
            return "This performance was already submitted once and is never submitted again. CLICK Back."
        if shown != target:
            return f"SCROLL_UP until this page shows the performance time {target}, then submit."
        if tickets_shown(nodes) != tickets:
            return f"Number of Tickets must show {tickets} before submitting."
        if len(boxes) != 2 or not all(b["checked"] for b in boxes):
            return "Tick both boxes before submitting."
        return None
    return f"{label or 'That control'!r} is outside this run. Choose another operation."


def chosen(agent):
    decision, page = agent.state["decision"], agent.state["page"]
    return next((a for a in page["actions"] if a["id"] == decision["choice"]), None)


def wait_for_verdict(folder, pending):
    for name in ("approve", "reject"):
        (folder / name).unlink(missing_ok=True)
    (folder / "pending.json").write_text(json.dumps(pending, indent=2))
    print(f"PAUSED before Submit Your Entry for {pending['performance']} -- waiting for {folder}/approve or /reject")
    while True:
        if (folder / "reject").exists():
            return False
        if (folder / "approve").exists():
            (folder / "approve").unlink()
            (folder / "pending.json").unlink(missing_ok=True)
            return True
        time.sleep(0.5)


def dismiss_overlay(device):
    """Close an in-app message with one BACK press. It is never pressed twice for the same message."""
    if not overlay(device.nodes):
        return True
    print("In-app message over the screen; pressing BACK once", flush=True)
    device.key("BACK")
    device.observe()
    return not overlay(device.nodes)


def run(device, plan, names, target, args, folder, submitted, dry_runs, events=None):
    """Run one plan of stages with its own agent. Returns the plan's outcome and its model calls."""
    stage, false_dones, status = 0, 0, "complete"
    agent = Agent(None, plan[0][0], device=device)
    state = agent.state
    after_submit = None

    def mark(kind, **fields):
        if events is not None:
            events.append({"t": round(time.perf_counter(), 3), "kind": kind, "performance": target, **fields})

    def passed():
        device.read()
        return bool(plan[stage][1](device.nodes))

    def advance(reason):
        nonlocal stage, false_dones
        print(f"  STAGE {names[stage]} complete ({reason})", flush=True)
        mark("stage", name=names[stage])
        stage, false_dones = stage + 1, 0
        if stage < len(plan):
            state["goal"], state["status"] = plan[stage][0], "ready"

    def jump(to, text=None):
        nonlocal stage, false_dones
        if to != stage:
            stage, false_dones = to, 0
        state["goal"] = plan[stage][0] + (" NOTE: " + text if text else "")
        state["decision"], state["status"] = None, "ready"

    def note(text):
        """Tell the policy why its last choice was refused, without executing anything."""
        jump(stage, text)

    while stage < len(plan):
        device.read()
        if not dismiss_overlay(device):
            status = "blocked_by_in_app_message"
            break
        if state["status"] == "ready" and passed():
            advance("screen check passed")
            continue
        if state["status"] == "done":
            false_dones += 1
            print(f"  STAGE {names[stage]}: DONE claimed but the screen check failed ({false_dones})", flush=True)
            if false_dones >= 3:
                status = "blocked"
                break
            note("DONE was rejected because this stage is not complete yet. Choose the next operation.")
            continue
        if state["status"] == "blocked":
            chose_blocked = state["decisions"] and state["decisions"][-1]["choice"] == "BLOCKED"
            if not chose_blocked or false_dones >= 3:
                status = "blocked"
                break
            false_dones += 1
            note("BLOCKED was rejected. The control may be off screen: use SCROLL_DOWN or SCROLL_UP.")
            continue
        try:
            agent.command("predict")
            decision = state["decision"]
            action = chosen(agent)
            if device.read()["fingerprint"] != state["page"]["fingerprint"]:
                raise StalePage("Screen changed during the decision")
            reason = action and refusal(device.nodes, action, target, args.tickets, submitted)
            if reason:
                print(f"  REFUSED {action['label'][:80]!r}: {reason}", flush=True)
                mark("refused", reason=reason)
                note(reason)
                continue
            submit = action is not None and action["kind"] == "click"
            submit = submit and device.nodes[action["node"]]["label"] == "Submit Your Entry"
            if submit:
                pending = {
                    "performance": target,
                    "tickets": tickets_shown(device.nodes),
                    "boxes_ticked": [b["checked"] for b in entry_boxes(device.nodes)],
                    "confidence": decision["confidence"],
                }
                if args.dry_run:
                    print(f"  DRY RUN: would submit {json.dumps(pending)}; going back instead", flush=True)
                    mark("dry_run_stop")
                    dry_runs.append(pending)
                    jump(names.index("Back to list"), "This entry was not submitted. Go back to the list.")
                    continue
                if not args.auto_approve and not wait_for_verdict(folder, pending):
                    print(f"  REJECTED by the user: {target} not submitted", flush=True)
                    status = "rejected"
                    jump(names.index("Back to list"), "This entry was not submitted. Go back to the list.")
                    continue
            agent.command("act", {"fingerprint": state["page"]["fingerprint"]})
            if submit:
                submitted.add(target)
                hidden = private(device.nodes)
                after_submit = [n["label"] for n in device.nodes if n["label"] and n["index"] not in hidden]
                print(f"  SUBMITTED {target}; the screen now shows: {after_submit}", flush=True)
                mark("submitted")
            if decision["choice"] not in {"DONE", "BLOCKED"}:
                state["goal"] = plan[stage][0]
        except StalePage:
            state["decision"], state["status"] = None, "ready"
            state["page"] = device.observe()
            continue
        last = state["history"][-1] if state["history"] and decision["choice"] not in {"DONE", "BLOCKED"} else {}
        target_confidence = decision["target_confidence"]
        mark(
            "step",
            operation=decision["operation"],
            label=last.get("action", decision["choice"]),
            confidence=decision["confidence"],
            jev_ms=decision["latency_ms"],
        )
        print(
            f"  {decision['operation']:<11} {last.get('action', decision['choice'])[:70]!r:<74} "
            f"conf={decision['confidence']:.2f} tgt={target_confidence if target_confidence is not None else '-'} "
            f"jev={decision['latency_ms']}ms",
            flush=True,
        )
        if state["status"] == "done" and passed():
            advance("DONE, check passed")
    snapshot = agent.snapshot()
    for d in snapshot["decisions"]:
        request = d.pop("request", {})
        if args.debug_trace:
            d["elements"] = request.get("state", {}).get("elements", [])
    return {
        "status": status,
        "stages_completed": f"{stage}/{len(plan)}",
        "after_submit": after_submit,
        "actions": len(state["history"]),
        "decisions": snapshot["decisions"],
        "history": snapshot["history"],
    }


def to_top(device):
    for _ in range(20):
        before = device.read()["fingerprint"]
        if not device.scroll("up") or device.read()["fingerprint"] == before:
            return


def scan(device):
    """Read every Broadway card from the top of the list to the bottom. Scrolls only."""
    to_top(device)
    cards = {}
    for _ in range(30):
        before = device.read()["fingerprint"]
        for performance, card in read_cards(device.nodes).items():
            # A card's button can hide under the pinned tabs, so one sighting of Enter Now is kept.
            if not cards.get(performance, {}).get("enter_now"):
                cards[performance] = card
        if not device.scroll("down") or device.read()["fingerprint"] == before:
            break
    to_top(device)
    return cards


def verify(device, performances):
    """Restart the app, open the Broadway list by code, and read each performance's card again."""
    device.device.app_stop(PACKAGE)
    device.device.app_start(PACKAGE)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not any(n["label"] == "Lottery" and n["clickable"] for n in device.nodes):
        time.sleep(0.5)
        device.read()
    # The home screen shows before the account has loaded, and a tap then is lost: wait for it to hold still.
    device.settle(still=1.5, cap=8)
    device.read()
    if overlay(device.nodes) and not dismiss_overlay(device):
        return None
    lottery = next((n for n in device.nodes if n["label"] == "Lottery" and n["clickable"]), None)
    if lottery is None:
        return None
    device.tap_node(lottery)
    device.observe()
    if not on_list(device.nodes):
        return None
    cards = scan(device)
    return {p: cards.get(p) or cards.get(undated(p)) for p in performances}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--performance", action="append", default=[], help="e.g. 'October 6, 2026 7:00pm'")
    parser.add_argument("--all", action="store_true", help="Every Broadway performance that shows Enter Now")
    parser.add_argument("--tickets", type=int, choices=(1, 2), default=2)
    parser.add_argument("--output", default="artifacts/hamilton/latest")
    parser.add_argument("--serial", help="adb serial of the device, if more than one is connected")
    parser.add_argument("--dry-run", action="store_true", help="Stop before every submit and go back to the list")
    parser.add_argument("--auto-approve", action="store_true", help="Submit the requested entries without pausing")
    parser.add_argument("--debug-trace", action="store_true", help="Also save each decision's element table")
    parser.add_argument("--record", action="store_true", help="Record the screen from the first entry to the last")
    parser.add_argument(
        "--verify-only", action="store_true", help="Only restart the app and re-read the --output run's targets"
    )
    args = parser.parse_args()
    if not args.all and not args.performance and not args.verify_only:
        parser.error("give --performance at least once, or --all")
    folder = Path(args.output).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    device = Device(PACKAGE, serial=args.serial, volatile=COUNTDOWN, hide=private, tap=tap_point)
    if args.verify_only:
        # A later, independent read of an earlier run. Its own check is kept as it was.
        path = folder / "state.json"
        trace = json.loads(path.read_text())
        rescan = verify(device, trace["summary"]["targets"])
        trace["summary"]["verified_after_restart_rescan"] = rescan
        path.write_text(json.dumps(trace, indent=2, default=str))
        print("VERIFIED after restart:", json.dumps(rescan, indent=2))
        return
    started = time.perf_counter()
    submitted, dry_runs, results = set(), [], {}

    opened = run(device, [OPEN_LIST], ["Open list"], None, args, folder, submitted, dry_runs)
    results["open_list"] = opened
    targets = []
    if opened["status"] == "complete":
        cards = scan(device)
        listed = [p for p, card in cards.items() if card["enter_now"]]
        print("Open for entry:", listed, flush=True)
        entered = [p for p, card in cards.items() if card["entered"]]
        if entered:
            print("Already entered:", entered, flush=True)
        closed = {p: card["inside"] for p, card in cards.items() if not card["enter_now"] and not card["entered"]}
        if closed:
            print("Listed without Enter Now:", closed, flush=True)
        targets = listed if args.all else args.performance
        missing = [p for p in targets if p not in listed]
        if missing:
            print(f"STOPPED: not open for entry: {missing}", flush=True)
            targets = []
    events, hidden, inputs, recording = [], [], [], None
    if args.record and targets:
        # Where the entrant's name was on every read, so the renderer can blur it.
        device.on_read = lambda t, rects: hidden.append([round(t, 3), rects])
        # Every tap and drag, so the renderer can show where input went.
        device.on_input = lambda kind, points, started, ended: inputs.append(
            {"kind": kind, "points": points, "started": round(started, 3), "ended": round(ended, 3)}
        )
        recording = ScreenRecording(device, folder / "screen")
        recording.start()
    entries_started = time.perf_counter()
    for target in targets:
        print(f"== {target}", flush=True)
        plan = stages(target, args.tickets, submitted)
        results[target] = run(device, plan, STAGE_NAMES, target, args, folder, submitted, dry_runs, events)
        if results[target]["status"] not in {"complete", "rejected"}:
            print(f"STOPPED at {target}: {results[target]['status']}", flush=True)
            break
    entries_ms = round((time.perf_counter() - entries_started) * 1000)
    if recording:
        segments = recording.stop()
        device.on_read = device.on_input = None
        (folder / "recording.json").write_text(
            json.dumps(
                {
                    "entries_started": entries_started,
                    "segments": segments,
                    "hidden": hidden,
                    "inputs": inputs,
                    "events": events,
                }
            )
        )
        print(f"recording: {len(segments)} segment(s) in {folder / 'screen'}", flush=True)
    elapsed = round((time.perf_counter() - started) * 1000)
    verified = verify(device, targets) if targets else None
    print("VERIFIED after restart:", json.dumps(verified, indent=2), flush=True)
    decisions = [d for r in results.values() for d in r["decisions"]]
    summary = {
        "targets": targets,
        "submitted": sorted(submitted),
        "dry_run": args.dry_run,
        "dry_run_stops": dry_runs,
        "auto_approve": args.auto_approve,
        "tickets": args.tickets,
        "statuses": {k: r["status"] for k, r in results.items()},
        "after_submit": {k: r["after_submit"] for k, r in results.items() if r["after_submit"]},
        "verified_after_restart": verified,
        "actions": sum(r["actions"] for r in results.values()),
        "jev_calls": len(decisions),
        "cost_usd": round(sum(d["usage"].get("cost", 0) for d in decisions), 6),
        "entries_ms": entries_ms,
        "elapsed_ms_including_scans": elapsed,
    }
    trace = {"results": results, "events": events, "summary": summary}
    (folder / "state.json").write_text(json.dumps(trace, indent=2, default=str))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
