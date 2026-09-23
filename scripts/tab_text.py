"""uv run --env-file .env python scripts/tab_text.py URL_SUBSTRING [--close] -- print or close matching tabs."""

import sys

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

ensure_daemon()
for target in cdp("Target.getTargets")["targetInfos"]:
    if target["type"] != "page" or sys.argv[1] not in target["url"]:
        continue
    if "--close" in sys.argv:
        cdp("Target.closeTarget", targetId=target["targetId"])
        print("closed", target["targetId"][:8], target["url"])
        continue
    session = cdp("Target.attachToTarget", targetId=target["targetId"], flatten=True)["sessionId"]
    text = cdp("Runtime.evaluate", session_id=session, expression="document.body.innerText.slice(0,600)",
               returnByValue=True)["result"].get("value")
    print("==", target["targetId"][:8], target["url"])
    print(text)
