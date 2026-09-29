"""Small, observable helpers for a standard reCAPTCHA checkbox.

The helper clicks the checkbox at most once. It never attempts to recognize or answer an image
challenge; callers can pause for a person when the returned outcome is ``challenge``.
"""

import time

CAPTCHA_STATUS = """(() => {
  const rect=e=>{if(!e)return null;const r=e.getBoundingClientRect();return {
    x:r.x,y:r.y,w:r.width,h:r.height,visible:r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight};};
  const widgets=[...document.querySelectorAll('.g-recaptcha,[data-sitekey]')];
  const frames=[...document.querySelectorAll('iframe[src*=recaptcha],iframe[title*=reCAPTCHA]')];
  const responses=[...document.querySelectorAll('textarea[name="g-recaptcha-response"]')];
  const anchor=frames.find(f=>!/challenge/i.test(f.title||'') && /anchor|recaptcha/i.test(f.src||f.title||''));
  const challenge=frames.find(f=>/challenge/i.test(f.title||'') && rect(f)?.visible);
  const tokenLength=Math.max(0,...responses.map(e=>e.value.trim().length));
  return {present:widgets.length>0||frames.length>0,solved:tokenLength>0,token_length:tokenLength,
    challenge:!!challenge,anchor:rect(anchor),viewport:{width:innerWidth,height:innerHeight,scroll_y:scrollY}};
})()"""


def captcha_status(browser):
    """Return observable reCAPTCHA state without interacting with the page."""
    return browser.evaluate(CAPTCHA_STATUS)


def click_recaptcha_checkbox(browser, timeout=5.0):
    """Click one visible reCAPTCHA checkbox once and report the resulting state.

    Outcomes are ``absent``, ``solved``, ``challenge``, ``pending``, or ``unavailable``. A visible
    image challenge is deliberately not solved here.
    """
    browser.call("Page.bringToFront")
    status = captcha_status(browser)
    if not status["present"]:
        return {**status, "outcome": "absent"}
    if status["solved"]:
        return {**status, "outcome": "solved"}
    if status["challenge"]:
        return {**status, "outcome": "challenge"}
    if not status["anchor"]:
        return {**status, "outcome": "unavailable"}

    if not status["anchor"]["visible"]:
        browser.evaluate(
            "document.querySelector('iframe[title*=reCAPTCHA],iframe[src*=recaptcha]')"
            "?.scrollIntoView({block:'center'})"
        )
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
