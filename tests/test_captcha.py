"""Offline contracts for the shared, one-click reCAPTCHA helper."""

from unittest.mock import Mock

from jev_ultrafast.captcha import captcha_status, click_recaptcha_checkbox


def state(*, present=True, solved=False, challenge=False, visible=True):
    return {
        "present": present,
        "solved": solved,
        "token_length": 10 if solved else 0,
        "challenge": challenge,
        "anchor": {"x": 80, "y": 200, "w": 304, "h": 78, "visible": visible} if present else None,
        "viewport": {"width": 1120, "height": 780, "scroll_y": 0},
    }


def test_captcha_status_is_read_only():
    browser = Mock(evaluate=Mock(return_value=state()))
    assert captcha_status(browser)["present"] is True
    browser.call.assert_not_called()


def test_checkbox_is_clicked_once_and_waits_for_token():
    browser = Mock()
    browser.evaluate.side_effect = [state(), state(solved=True)]

    result = click_recaptcha_checkbox(browser, timeout=0.1)

    assert result["outcome"] == "solved"
    mouse = [call for call in browser.call.call_args_list if call.args[0] == "Input.dispatchMouseEvent"]
    assert [call.kwargs["type"] for call in mouse] == ["mousePressed", "mouseReleased"]


def test_visible_image_challenge_is_left_for_a_person():
    browser = Mock(evaluate=Mock(return_value=state(challenge=True)))

    result = click_recaptcha_checkbox(browser)

    assert result["outcome"] == "challenge"
    assert not any(call.args[0] == "Input.dispatchMouseEvent" for call in browser.call.call_args_list)


def test_offscreen_checkbox_is_scrolled_into_view_before_one_click():
    browser = Mock()
    browser.evaluate.side_effect = [state(visible=False), None, state(), state(solved=True)]

    result = click_recaptcha_checkbox(browser, timeout=0.1)

    assert result["outcome"] == "solved"
    assert "scrollIntoView" in browser.evaluate.call_args_list[1].args[0]
    assert any(call.args[0] == "Runtime.evaluate" for call in browser.call.call_args_list)
