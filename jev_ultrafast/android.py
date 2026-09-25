"""Observed actions on an Android app: uiautomator2 reads the accessibility tree, adb taps it.

The page this returns has the same shape as the Chrome snapshot, so the loop and the model are
unchanged. Every action points at a node of the observed tree; its tap point is computed here from
that node's bounds, never from model output.
"""

import base64
import hashlib
import io
import json
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .browser import StalePage

BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
TAB = re.compile(r"\bTab \d+ of \d+$")
ROLES = {"Button": "button", "ImageButton": "button", "Switch": "switch", "RadioButton": "radio"}


def read_nodes(xml, package, volatile=None):
    """The app's visible nodes in document order. `volatile` text (a countdown) is removed from labels."""
    # The dumper never writes a DTD; refuse one rather than let entity expansion run.
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise ValueError("Unexpected DTD in the UI hierarchy")
    nodes = []
    for e in ET.fromstring(xml).iter("node"):
        if e.get("package") != package or e.get("visible-to-user") == "false":
            continue
        match = BOUNDS.fullmatch(e.get("bounds", ""))
        if not match:
            continue
        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        lines = []
        for line in (e.get("content-desc") or e.get("text") or "").splitlines():
            line = volatile.sub("", line).strip() if volatile else line.strip()
            if line and line not in lines:
                lines.append(line)
        nodes.append(
            {
                "index": len(nodes),
                "class": e.get("class", "").rsplit(".", 1)[-1],
                "resource_id": e.get("resource-id", ""),
                "label": " ".join(lines),
                "clickable": e.get("clickable") == "true",
                "enabled": e.get("enabled") == "true",
                "checkable": e.get("checkable") == "true",
                "checked": e.get("checked") == "true",
                "selected": e.get("selected") == "true",
                "scrollable": e.get("scrollable") == "true",
                "rect": (x1, y1, x2, y2),
            }
        )
    return nodes


def contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def neighbour_label(node, nodes):
    """Name an unlabeled control by the labeled nodes beside it on the same row."""
    x1, y1, x2, y2 = node["rect"]
    row = [n for n in nodes if n["label"] and n["rect"][1] < y2 and n["rect"][3] > y1 and n is not node]
    left = max((n for n in row if n["rect"][2] <= x1), key=lambda n: n["rect"][2], default=None)
    right = min((n for n in row if n["rect"][0] >= x2), key=lambda n: n["rect"][0], default=None)
    if left and right:
        return f"Unlabeled button between “{left['label']}” and “{right['label']}”"
    if left or right:
        return f"Unlabeled button {'right' if left else 'left'} of “{(left or right)['label']}”"
    return "Unlabeled button"


def tap_point(node):
    x1, y1, x2, y2 = node["rect"]
    w, h = x2 - x1, y2 - y1
    if node["checkable"] and w >= 2 * h:
        # A checkbox row: tap its leading box, so a link inside the row's text is not hit.
        return x1 + round(min(w * 0.09, 100)), y1 + round(min(h / 2, 45))
    return x1 + w // 2, y1 + h // 2


def point(node, tap=None):
    """Where to tap a node: an app-specific point if one is given and lies inside the node, else tap_point."""
    x1, y1, x2, y2 = node["rect"]
    chosen = tap(node) if tap else None
    if chosen and x1 <= chosen[0] < x2 and y1 <= chosen[1] < y2:
        return tuple(chosen)
    return tap_point(node)


def page_state(nodes, package, hidden=frozenset(), tap=None):
    """Offer each enabled clickable node as a CLICK target; the rest of the labels are page text.

    `tap(node)` may name a better point inside a node whose tappable area is smaller than its bounds.
    """
    visible = [n for n in nodes if n["index"] not in hidden]
    actions, text = [], []
    height = max((n["rect"][3] for n in nodes), default=0)
    for n in visible:
        if not (n["clickable"] and n["enabled"]) or n["class"] == "EditText":
            if n["label"] and (not text or text[-1] != n["label"]):
                text.append(n["label"])
            continue
        label = n["label"] or neighbour_label(n, visible)
        # A repeated control ("Enter Now" on every card) is named by the card that holds it.
        cards = [c for c in visible if c is not n and c["clickable"] and c["label"] and contains(c["rect"], n["rect"])]
        if cards and n["label"]:
            card = min(cards, key=lambda c: (c["rect"][2] - c["rect"][0]) * (c["rect"][3] - c["rect"][1]))
            label = f"{card['label']} · {label}"
        x1, y1, x2, y2 = n["rect"]
        role = "tab" if TAB.search(n["label"]) else "checkbox" if n["checkable"] else ROLES.get(n["class"], "button")
        action = {
            "id": f"e{len(actions) + 1}",
            "kind": "click",
            "label": label,
            "node": n["index"],
            "role": role,
            "rect": {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1},
            "tap": point(n, tap),
        }
        if n["checkable"]:
            action["checked"] = n["checked"]
        if n["selected"]:
            action["selected"] = True
        actions.append(action)
    scrollers = [n for n in visible if n["scrollable"] and n["class"] != "HorizontalScrollView"]
    below = ""
    if scrollers:
        area = max(scrollers, key=lambda n: (n["rect"][2] - n["rect"][0]) * (n["rect"][3] - n["rect"][1]))["rect"]
        for way in ("down", "up"):
            actions.append({"id": f"scroll_{way}", "kind": "scroll", "label": f"Scroll {way}", "area": area})
        # The tree holds only what is on screen; a node cut by the list's bottom edge shows it continues.
        cut = [n["label"] for n in visible if n["label"] and n["rect"][3] >= area[3] - 2 and n["rect"][1] < area[3]]
        if cut:
            below = "Continues below the screen: " + " | ".join(cut)
    actions.append({"id": "wait", "kind": "wait", "label": "Wait for the screen to update"})
    top = [n["label"] for n in visible if n["label"] and not n["clickable"] and n["rect"][3] < height * 0.1]
    # Where every labeled node sits. Not sent to the model; it makes a scroll of plain text a change.
    layout = [[n["label"][:40], *n["rect"]] for n in visible if n["label"]]
    return {"url": f"android://{package}", "title": top[0] if top else package, "text": "\n".join(text)[:6000],
            "text_below": below[:400], "actions": actions, "layout": layout}


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "layout")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class Device:
    """One app on one Android device or emulator. The app must already be installed."""

    def __init__(self, package, *, serial=None, volatile=None, hide=None, tap=None):
        import uiautomator2  # optional dependency: uv sync --extra android

        self.device = uiautomator2.connect(serial)
        self.serial, self.package, self.volatile, self.hide, self.tap = serial, package, volatile, hide, tap
        self.nodes, self.after_input = [], None
        # scroll id -> the screen where that scroll changed nothing. The tree has no scroll offsets,
        # so an edge of the list is only known once a scroll there had no effect.
        self.edges = {}
        # Called with (time, bounds of the hidden nodes) on every read, so a recording can blur them.
        self.on_read = None
        # Called with (kind, points, started, ended) for every tap and drag, so a recording can show them.
        self.on_input = None
        self.device.app_start(package)  # brings it to the front; does not restart it

    def shell(self, *args):
        prefix = ["adb", "-s", self.serial] if self.serial else ["adb"]
        subprocess.run([*prefix, "shell", *map(str, args)], check=True, capture_output=True, timeout=10)

    def read(self):
        self.nodes = read_nodes(self.device.dump_hierarchy(), self.package, self.volatile)
        hidden = frozenset(self.hide(self.nodes)) if self.hide else frozenset()
        if self.on_read:
            self.on_read(time.perf_counter(), [self.nodes[i]["rect"] for i in sorted(hidden)])
        state = page_state(self.nodes, self.package, hidden, self.tap)
        state["fingerprint"] = fingerprint(state)
        # Offer no scroll already known to do nothing here. The fingerprint above still covers it.
        state["actions"] = [a for a in state["actions"] if self.edges.get(a["id"]) != state["fingerprint"]]
        return state

    def settle(self, still=0.45, cap=3.0):
        """After input, wait until the screen (volatile text aside) has held still for `still` seconds.

        A page transition updates the tree in steps up to about 0.3 s apart, so a shorter hold can
        mistake a pause in the animation for the end of it.
        """
        deadline, last, since = time.monotonic() + cap, None, time.monotonic()
        while time.monotonic() < deadline:
            current = self.read()["fingerprint"]
            if current != last:
                last, since = current, time.monotonic()
            elif time.monotonic() - since >= still:
                return
            time.sleep(0.05)

    def observe(self, screenshot=False):
        scrolled = None
        if self.after_input:
            # Read-only, and after the executed action was logged.
            scrolled, self.after_input = self.after_input.get("scrolled_from"), None
            self.settle()
        state = self.read()
        if scrolled and scrolled[1] == state["fingerprint"]:
            self.edges[scrolled[0]] = state["fingerprint"]
            state["actions"] = [a for a in state["actions"] if a["id"] != scrolled[0]]
        if screenshot:
            buffer = io.BytesIO()
            self.device.screenshot().convert("RGB").save(buffer, format="JPEG", quality=72)
            state["screenshot"] = base64.b64encode(buffer.getvalue()).decode()
        return state

    def fresh(self, page, action=None):
        return self.read()["fingerprint"] == page["fingerprint"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Screen changed since this decision. Observe again.")
        kind = action["kind"]
        if kind == "wait":
            time.sleep(0.3)
        elif kind == "scroll":
            # Three quarters of the list per scroll: less than its height, so every row is on screen once.
            x1, y1, x2, y2 = action["area"]
            x, top, bottom = (x1 + x2) // 2, y1 + (y2 - y1) * 15 // 100, y1 + (y2 - y1) * 90 // 100
            start, end = (bottom, top) if action["id"] == "scroll_down" else (top, bottom)
            self.drag(x, start, end)
        elif kind == "click":
            self.tap_at(action["tap"])
        else:
            raise ValueError(f"{kind} is not supported on Android; nothing was executed.")
        self.after_input = None if kind == "wait" else {**action}
        if kind == "scroll":
            self.after_input["scrolled_from"] = (action["id"], page["fingerprint"])
        return {"executed": action["id"]}

    def drag(self, x, start, end, steps=12):
        """Drag and hold still before lifting, so the list does not fling on after the reading settles.

        A fling keeps moving for about half a second after a swipe while the accessibility tree lags
        behind it, so a settled read could still be followed by a change.
        """
        touch = self.device.touch
        started = time.perf_counter()
        touch.down(x, start)
        for i in range(1, steps + 1):
            touch.move(x, start + (end - start) * i // steps)
            time.sleep(0.015)
        time.sleep(0.15)
        touch.move(x, end)
        touch.up(x, end)
        if self.on_input:
            self.on_input("drag", [[x, start], [x, end]], started, time.perf_counter())

    def tap_at(self, point):
        started = time.perf_counter()
        self.shell("input", "tap", *point)
        if self.on_input:
            self.on_input("tap", [list(point)], started, time.perf_counter())

    def key(self, name):
        """A system key such as BACK. Code-owned, never chosen by the model."""
        self.shell("input", "keyevent", f"KEYCODE_{name}")
        self.after_input = {"kind": "key"}

    def tap_node(self, node):
        """Code-owned tap on a node of the last read, for navigation a script does itself."""
        self.tap_at(point(node, self.tap))
        self.after_input = {"kind": "click"}

    def scroll(self, direction):
        """Code-owned scroll of the main list; returns False when there is nothing to scroll."""
        for attempt in range(3):
            state = self.read()
            action = next((a for a in state["actions"] if a["id"] == f"scroll_{direction}"), None)
            if action is None:
                return False
            try:
                self.act(action, state)
            except StalePage:
                # Raised before any input, so reading again and scrolling once is not a repeated mutation.
                if attempt == 2:
                    raise
                time.sleep(0.2)
                continue
            self.observe()
            return True

    def close(self):
        pass


class ScreenRecording:
    """The device's own screen at real speed, via `screenrecord` segments (each is capped at 180 s).

    Each segment keeps the host time it was started, so marks taken on the host (such as where a
    hidden node was) can be placed on the video.
    """

    def __init__(self, device, folder):
        self.adb = ["adb", "-s", device.serial] if device.serial else ["adb"]
        self.folder = Path(folder)
        self.segments, self.process, self.stopping = [], None, False
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.segments and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)  # screenrecord writes its first frame a moment after it starts

    def loop(self):
        while not self.stopping:
            path = f"/sdcard/jev-screen-{len(self.segments):02d}.mp4"
            command = [*self.adb, "shell", "screenrecord", "--time-limit", "180", "--bit-rate", "12000000", path]
            self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.segments.append({"device_path": path, "started": time.perf_counter()})
            self.process.wait()

    def stop(self):
        """Stop, pull the segments to the folder, and delete them from the device."""
        self.stopping = True
        subprocess.run([*self.adb, "shell", "pkill", "-INT", "screenrecord"], capture_output=True, timeout=10)
        self.thread.join(timeout=15)
        time.sleep(1)  # the last segment is finalized after SIGINT
        for segment in self.segments:
            local = self.folder / Path(segment["device_path"]).name
            subprocess.run([*self.adb, "pull", segment["device_path"], str(local)], capture_output=True, timeout=60)
            subprocess.run([*self.adb, "shell", "rm", "-f", segment["device_path"]], capture_output=True, timeout=10)
            segment["file"] = local.name
        return self.segments
