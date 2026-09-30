"""Small, observable helpers for a standard reCAPTCHA checkbox.

The helper clicks the checkbox at most once. It never attempts to recognize or answer an image
challenge; callers can pause for a person when the returned outcome is ``challenge``.
"""

import json
import time

# Frames are identified by their Google path, not their title: hCaptcha's checkbox frame is titled
# "...security challenge". A challenge counts only when rendered. reCAPTCHA keeps its hidden
# challenge frame on screen under visibility:hidden, so geometry alone reports a false challenge.
CAPTCHA_STATUS = """(() => {
  const rect=e=>{if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,
    visible:r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth};};
  const shown=e=>rect(e).visible && e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const widgets=[...document.querySelectorAll('.g-recaptcha,[data-sitekey]')];
  const frames=[...document.querySelectorAll('iframe[src*="/recaptcha/"]')];
  const anchors=frames.filter(f=>/\\/recaptcha\\/(?:api2|enterprise)\\/anchor/.test(f.src));
  const anchor=anchors.find(f=>!/[?&]size=invisible/.test(f.src));
  const challenge=frames.find(f=>/\\/recaptcha\\/(?:api2|enterprise)\\/bframe/.test(f.src) && shown(f));
  const responses=[...document.querySelectorAll('textarea[name="g-recaptcha-response"]')];
  const tokenLength=Math.max(0,...responses.map(e=>e.value.trim().length));
  return {present:widgets.length>0||frames.length>0,checkbox:!!anchor,invisible:!anchor&&anchors.length>0,
    solved:tokenLength>0,token_length:tokenLength,challenge:!!challenge,anchor:rect(anchor),
    viewport:{width:innerWidth,height:innerHeight,scroll_y:scrollY}};
})()"""

ANCHOR = (
    "[...document.querySelectorAll('iframe[src*=\"/recaptcha/\"]')]"
    ".find(f=>/\\/recaptcha\\/(?:api2|enterprise)\\/anchor/.test(f.src) && !/[?&]size=invisible/.test(f.src))"
)
# The point must land on the checkbox frame itself. A consent banner or sticky footer on top of it
# would otherwise receive the click, an unintended mutation outside any approval gate.
HIT_TEST = "(point => { const f=" + ANCHOR + "; return !!f && document.elementFromPoint(...point)===f; })(%s)"


def captcha_status(browser):
    """Return observable reCAPTCHA state without interacting with the page."""
    return browser.evaluate(CAPTCHA_STATUS)


def click_recaptcha_checkbox(browser, timeout=5.0):
    """Click one visible, uncovered reCAPTCHA checkbox once and report the resulting state.

    Outcomes are ``absent``, ``solved``, ``challenge``, ``invisible`` (the token is issued on
    submit, so there is nothing to click), ``unavailable`` (another vendor's widget, or no checkbox
    in view), ``covered``, or ``pending``. A visible image challenge is deliberately not solved here.
    """
    browser.call("Page.bringToFront")
    status = captcha_status(browser)
    if not status["present"]:
        return {**status, "outcome": "absent"}
    if status["solved"]:
        return {**status, "outcome": "solved"}
    if status["challenge"]:
        return {**status, "outcome": "challenge"}
    if not status["checkbox"]:
        return {**status, "outcome": "invisible" if status["invisible"] else "unavailable"}

    if not status["anchor"]["visible"]:
        browser.evaluate(ANCHOR + "?.scrollIntoView({block:'center'})")
        browser.call(
            "Runtime.evaluate",
            expression="""new Promise(resolve => {
              const start=performance.now(), initial=scrollY; let last=initial, stable=0, changed=false;
              const tick=()=>{const current=scrollY, elapsed=performance.now()-start;
                changed ||= current!==initial; stable=current===last?stable+1:0; last=current;
                if((changed&&stable>=3)||(!changed&&elapsed>250)||elapsed>1000) resolve();
                else requestAnimationFrame(tick);}; requestAnimationFrame(tick);
            })""",
            awaitPromise=True,
            returnByValue=True,
        )
        status = captcha_status(browser)
        if not status["anchor"] or not status["anchor"]["visible"]:
            return {**status, "outcome": "unavailable"}

    anchor = status["anchor"]
    # Google's checkbox sits near the leading edge of its standard anchor iframe.
    x, y = anchor["x"] + min(28, anchor["w"] / 2), anchor["y"] + anchor["h"] / 2
    if browser.evaluate(HIT_TEST % json.dumps([x, y])) is not True:
        return {**status, "outcome": "covered"}
    for event in ("mousePressed", "mouseReleased"):
        browser.call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = captcha_status(browser)
        if status["solved"]:
            return {**status, "outcome": "solved"}
        if status["challenge"]:
            return {**status, "outcome": "challenge"}
        time.sleep(0.1)
    return {**status, "outcome": "pending"}
