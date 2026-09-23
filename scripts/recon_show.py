"""uv run --env-file .env python scripts/recon_show.py SHOW_URL -- log in by code, open a show, print its table.

No model calls. Credentials are read from LOGIN_EMAIL / LOGIN_PASSWORD and never printed.
"""

import json
import os
import sys
import time

from jev_ultrafast.browser import Browser
from jev_ultrafast.model import action_space

browser = Browser("https://www.luckyseat.com/account/login?afterLoginUrl=" + sys.argv[1].split(".com", 1)[1])
try:
    time.sleep(2)
    fill = """((email, password) => {
      const set=(sel, v)=>{const e=document.querySelector(sel); e.focus(); e.value=v;
        e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}));};
      set('input[type=email]', email); set('input[type=password]', password);
      [...document.querySelectorAll('button,input[type=submit]')].find(b=>/log in/i.test(b.innerText||b.value)).click();
      return 'submitted';
    })"""
    args = json.dumps(os.environ["LOGIN_EMAIL"]), json.dumps(os.environ["LOGIN_PASSWORD"])
    print(browser.evaluate(f"{fill}({args[0]}, {args[1]})"))
    for second in range(8):
        time.sleep(1)
        url = browser.evaluate("location.href")
        print(second + 1, "s", url)
    for scroll in range(4):
        page = browser.observe(screenshot=False)
        print(f"=== scroll y={page['scroll']} ===")
        for e in action_space(page["actions"])[0]:
            extra = {k: e[k] for k in ("checked", "selected", "expanded") if k in e}
            print(f"[{e['index']}] {e['role']:<9} {e['label'][:90]!r} {e.get('value', '')!r} {extra or ''}")
        browser.call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=560)
        time.sleep(0.8)
finally:
    browser.close()
