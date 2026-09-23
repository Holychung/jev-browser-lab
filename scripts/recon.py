"""uv run --env-file .env python scripts/recon.py URL -- print the element table Jev would see. No model calls."""

import json
import sys

from jev_ultrafast.browser import Browser
from jev_ultrafast.model import action_space

browser = Browser(sys.argv[1])
try:
    page = browser.observe(screenshot=False)
    elements, targets, controls = action_space(page["actions"])
    print(page["url"], "|", page["title"])
    for e in elements:
        ops = "/".join(e["operations"])
        print(f"[{e['index']}] {e['role']:<10} {e['label'][:70]!r:<74} {ops} {e.get('value', '')!r}")
    print("controls:", list(controls))
    print("omitted:", page.get("omitted_actions"))
    print("--- page text (first 1500 chars) ---")
    print(page["text"][:1500])
    frames = browser.evaluate(
        "JSON.stringify({iframes:[...document.querySelectorAll('iframe')].map(f=>f.src),"
        "shadowHosts:[...document.querySelectorAll('*')].filter(e=>e.shadowRoot).map(e=>e.tagName)})"
    )
    print("--- frames/shadow ---")
    print(json.dumps(json.loads(frames), indent=2))
finally:
    if "--keep-open" not in sys.argv:
        browser.close()
