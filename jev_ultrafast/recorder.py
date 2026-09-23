"""Continuous CDP screencast of an owned tab, with a wall-clock event timeline for rendering.

Frames capture the whole page; only the blur selectors are redacted. Treat the screencast folder as
sensitive and delete it once the video is rendered (render_luckyseat.py does this by default).
"""

import base64
import json
import threading
import time
from pathlib import Path

from browser_harness.helpers import drain_events

# Blur rules are applied by the page's own renderer, so every captured frame is already redacted.
BLUR_SCRIPT = """(css => {
  const add = () => {
    if (document.getElementById('jev-privacy')) return;
    const style = document.createElement('style');
    style.id = 'jev-privacy';
    style.textContent = css;
    (document.head || document.documentElement).appendChild(style);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add);
  else add();
})(%s)"""


class Recorder:
    def __init__(self, browser, folder, blur_selectors=(), size=(1120, 780)):
        self.browser = browser
        self.frames = Path(folder) / "screencast"
        self.frames.mkdir(parents=True, exist_ok=False)
        if blur_selectors:
            css = json.dumps(", ".join(blur_selectors) + " { filter: blur(14px) !important; }")
            browser.call("Page.addScriptToEvaluateOnNewDocument", source=BLUR_SCRIPT % css)
            browser.evaluate(BLUR_SCRIPT % css)
        self.events, self.pauses, self.errors = [], [], []
        self.paused_at = None
        self.stopped = threading.Event()
        self.epoch = time.time()
        first = browser.call("Page.captureScreenshot", format="jpeg", quality=85)["data"]
        (self.frames / "000000.jpg").write_bytes(base64.b64decode(first))
        width, height = size
        browser.call(
            "Page.startScreencast", format="jpeg", quality=85, maxWidth=width, maxHeight=height, everyNthFrame=1
        )
        self.worker = threading.Thread(target=self._capture, daemon=True)
        self.worker.start()

    def _capture(self):
        try:
            while not self.stopped.is_set():
                for event in drain_events():
                    if event["method"] != "Page.screencastFrame" or event.get("session_id") != self.browser.session:
                        continue
                    params = event["params"]
                    ms = max(1, round((params["metadata"]["timestamp"] - self.epoch) * 1000))
                    (self.frames / f"{ms:07d}.jpg").write_bytes(base64.b64decode(params["data"]))
                    # What each frame shows (device size, scroll offset), for frames that change shape.
                    with (self.frames / "metadata.jsonl").open("a") as log:
                        log.write(json.dumps({"ms": ms, **params["metadata"]}) + "\n")
                    self.browser.call("Page.screencastFrameAck", sessionId=params["sessionId"])
                self.stopped.wait(0.015)
        except Exception as e:
            self.errors.append(str(e))

    def now(self):
        return round((time.time() - self.epoch) * 1000)

    def mark(self, kind, **fields):
        self.events.append({"t": self.now(), "kind": kind, **fields})

    def pause(self):
        self.paused_at = self.now()

    def resume(self):
        self.pauses.append([self.paused_at, self.now()])
        self.paused_at = None

    def stop(self):
        time.sleep(0.1)  # Let the last frame arrive.
        self.stopped.set()
        self.worker.join(timeout=3)
        try:
            self.browser.call("Page.stopScreencast")
        except RuntimeError as e:
            self.errors.append(str(e))
        return {
            "events": self.events,
            "pauses": self.pauses,
            "open_pause": self.paused_at,
            "stopped_ms": self.now(),
            "frames": len(list(self.frames.glob("*.jpg"))),
            "errors": self.errors,
        }
