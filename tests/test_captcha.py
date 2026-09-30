"""Offline contracts for the shared, one-click reCAPTCHA helper."""

from unittest.mock import Mock

import pytest

from jev_ultrafast.captcha import CAPTCHA_STATUS, captcha_status, click_recaptcha_checkbox


def state(*, present=True, checkbox=True, invisible=False, solved=False, challenge=False, visible=True):
    return {
        "present": present,
        "checkbox": present and checkbox,
        "invisible": invisible,
        "solved": solved,
        "token_length": 10 if solved else 0,
        "challenge": challenge,
        "anchor": {"x": 80, "y": 200, "w": 304, "h": 78, "visible": visible} if present and checkbox else None,
        "viewport": {"width": 1120, "height": 780, "scroll_y": 0},
    }


def mouse(browser):
    return [call for call in browser.call.call_args_list if call.args[0] == "Input.dispatchMouseEvent"]


def test_captcha_status_is_read_only():
    browser = Mock(evaluate=Mock(return_value=state()))
    assert captcha_status(browser)["present"] is True
    browser.call.assert_not_called()


def test_challenge_frames_count_only_when_rendered():
    # reCAPTCHA parks its challenge frame on screen under visibility:hidden, and hCaptcha's checkbox
    # frame title mentions "challenge"; neither is an image challenge.
    assert "checkVisibility({checkOpacity:true,checkVisibilityCSS:true})" in CAPTCHA_STATUS
    assert "bframe" in CAPTCHA_STATUS
    assert "title" not in CAPTCHA_STATUS


def test_checkbox_is_clicked_once_and_waits_for_token():
    browser = Mock()
    browser.evaluate.side_effect = [state(), True, state(solved=True)]

    result = click_recaptcha_checkbox(browser, timeout=0.1)

    assert result["outcome"] == "solved"
    assert [call.kwargs["type"] for call in mouse(browser)] == ["mousePressed", "mouseReleased"]


def test_covered_checkbox_is_not_clicked():
    browser = Mock()
    browser.evaluate.side_effect = [state(), False]

    result = click_recaptcha_checkbox(browser)

    assert result["outcome"] == "covered"
    assert "elementFromPoint" in browser.evaluate.call_args_list[1].args[0]
    assert mouse(browser) == []


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (state(challenge=True), "challenge"),
        (state(checkbox=False, invisible=True), "invisible"),
        (state(checkbox=False), "unavailable"),
        (state(present=False), "absent"),
    ],
)
def test_nothing_is_clicked_without_a_visible_checkbox(status, outcome):
    browser = Mock(evaluate=Mock(return_value=status))

    assert click_recaptcha_checkbox(browser)["outcome"] == outcome
    assert mouse(browser) == []


def test_offscreen_checkbox_is_scrolled_into_view_before_one_click():
    browser = Mock()
    browser.evaluate.side_effect = [state(visible=False), None, state(), True, state(solved=True)]

    result = click_recaptcha_checkbox(browser, timeout=0.1)

    assert result["outcome"] == "solved"
    assert "scrollIntoView" in browser.evaluate.call_args_list[1].args[0]
    assert any(call.args[0] == "Runtime.evaluate" for call in browser.call.call_args_list)
    assert len(mouse(browser)) == 2
