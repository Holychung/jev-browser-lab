"""Live Lucky Seat run: log in, pick New York, open Hadestown, enter its lottery.

uv run --env-file .env python examples/luckyseat.py

The task runs as ordered stages. Each stage is one narrow goal; a DONE choice only advances the
stage when an independent page check passes.

Credentials come from LOGIN_EMAIL / LOGIN_PASSWORD and are typed by code, never by a model.
Any click that looks like a submission pauses the run: it writes <output>/pending.json and waits
for <output>/approve or <output>/reject to appear.
"""

import argparse
import json
import re
import time
from pathlib import Path

from jev_ultrafast import Agent
from jev_ultrafast.browser import StalePage

# Start on the login route: a client-side jump from /home swaps the URL before the form renders.
URL = "https://www.luckyseat.com/account/login"
POPUPS = (
    " Accept the cookie notice if it blocks the page. Close unrelated pop-ups such as a newsletter "
    "sign-up without filling or submitting them."
)
# (goal, independent check). A check is JS returning true once the stage is really complete.
STAGES = [
    (
        "Log in to Lucky Seat. Choose TYPE_TEXT on the Email and Password fields; their values are "
        "filled automatically. Then click Log in. DONE once the login form is gone." + POPUPS,
        "!location.pathname.startsWith('/account/login') && !document.querySelector('input[type=password]')",
    ),
    (
        "Set the city to New York and open the Hadestown event. DONE once the Hadestown lottery page "
        "is open." + POPUPS,
        "location.pathname.includes('/shows/hadestown')",
    ),
    (
        "On this Hadestown lottery page, click the Select All button under Select your performance, "
        "so that every performance checkbox is checked. Scroll to find it if needed. Do not touch the "
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
        None,
    ),
]
# Account pages show personal details; stop before their text can be sent to a model.
PRIVATE = re.compile(r"/account/(?!login)")
SUBMIT = re.compile(r"\b(enter|submit|confirm|register|sign up|buy|pay)\b", re.IGNORECASE)


def gated(agent):
    """A click that may submit something, outside the login form, needs a human decision."""
    decision, page = agent.state["decision"], agent.state["page"]
    action = next((a for a in page["actions"] if a["id"] == decision["choice"]), None)
    if not action or action["kind"] != "click" or action.get("role") != "button" or not SUBMIT.search(action["label"]):
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


def wait_for_verdict(folder, action, agent):
    for name in ("approve", "reject"):
        (folder / name).unlink(missing_ok=True)
    decision, page = agent.state["decision"], agent.state["page"]
    pending = {
        "label": action["label"],
        "url": page["url"],
        "confidence": decision["confidence"],
        "target_confidence": decision["target_confidence"],
        "page_text": page["text"][:3000],
        # Whole-form state, including controls scrolled out of view.
        "form": agent.browser.evaluate("""(() => {
          const boxes=[...document.querySelectorAll('input[type=checkbox]')];
          return {checked: boxes.filter(b=>b.checked).length, total: boxes.length,
            checked_labels: boxes.filter(b=>b.checked).map(b=>b.labels?.[0]?.innerText.trim()),
            numbers: [...document.querySelectorAll('input[type=number]')].map(i=>i.value)};
        })()"""),
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
    parser.add_argument("--output", default="artifacts/luckyseat/latest")
    args = parser.parse_args()
    folder = Path(args.output).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stage, false_dones = 0, 0
    agent = Agent(URL, STAGES[stage][0])
    state = agent.state
    started = time.perf_counter()

    def passed():
        check = STAGES[stage][1]
        try:
            return check is not None and agent.browser.evaluate(f"!!({check})") is True
        except StalePage:
            return False

    def advance(reason):
        nonlocal stage, false_dones
        print(f"STAGE {stage + 1}/{len(STAGES)} complete ({reason})", flush=True)
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
                if false_dones >= 3:
                    state["status"] = "blocked"
                    break
                note("DONE was rejected because this stage is not complete yet. Choose the next operation.")
            if state["status"] == "blocked":
                break
            try:
                agent.command("predict")
                decision = state["decision"]
                if premature_login(agent):
                    print("REFUSED: login button clicked while the Password field is empty", flush=True)
                    note("The Password field is still empty. Choose TYPE_TEXT on the Password field first.")
                    continue
                action = gated(agent)
                paused = 0.0
                if action:
                    before = time.perf_counter()
                    if not wait_for_verdict(folder, action, agent):
                        state["status"] = "rejected"
                        break
                    paused = time.perf_counter() - before
                    started += paused
                    state["started_at"] += paused
                agent.command("act", {"fingerprint": state["page"]["fingerprint"]})
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
            if state["status"] == "ready" and passed():
                advance("page check passed")
    finally:
        snapshot = agent.snapshot()
        for d in snapshot["decisions"]:
            d.pop("request", None)
        cost = sum(d["usage"].get("cost", 0) for d in snapshot["decisions"])
        cost += sum(t["usage"].get("cost", 0) for t in snapshot["text_calls"])
        summary = {
            "status": state["status"],
            "stages_completed": f"{stage}/{len(STAGES)}",
            "final_url": state["page"]["url"],
            "actions": len(state["history"]),
            "jev_calls": len(snapshot["decisions"]),
            "text_calls": len(snapshot["text_calls"]),
            "cost_usd": round(cost, 6),
            "elapsed_ms_excluding_pauses": round((time.perf_counter() - started) * 1000),
        }
        snapshot["page"].pop("screenshot", None)
        (folder / "state.json").write_text(json.dumps({**snapshot, "summary": summary}, indent=2, default=str))
        print(json.dumps(summary, indent=2))
        if not PRIVATE.search(state["page"]["url"]):
            print("final page text:\n" + state["page"]["text"][:1500])


if __name__ == "__main__":
    main()
