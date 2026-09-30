"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import time
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage, browser_operation, fingerprint


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_native_select_snapshot_does_not_use_all_options_as_its_name():
    from pathlib import Path

    snapshot = Path("jev_ultrafast/snapshot.js").read_text()
    assert "['INPUT','SELECT'].includes(e.tagName) ? ''" in snapshot
    assert "e.tagName==='SELECT' ? [...e.selectedOptions].map(o=>o.label).join(', ')" in snapshot


def test_page_key_skips_hidden_helper_fields_but_keeps_transparent_checkboxes():
    import re
    from pathlib import Path

    snapshot = Path("jev_ultrafast/snapshot.js").read_text()
    safe = re.search(r"const safe = (.+?);\n", snapshot, re.S)[1]
    # display:none (reCAPTCHA's response textarea), visibility:hidden, aria-hidden and inert are out;
    # opacity is not checked, so an opacity:0 native checkbox under a styled box still counts.
    assert "checkVisibility({checkVisibilityCSS:true})" in safe
    assert "checkOpacity" not in safe
    assert """closest('[aria-hidden="true"],[inert]')""" in safe
    assert ".filter(safe)" in snapshot


def step_pattern(direction):
    import re
    from pathlib import Path

    snapshot = Path("jev_ultrafast/snapshot.js").read_text()
    return re.compile(re.search(rf"const {direction} = e => /(.+?)/i\.test\(stepSource\(e\)\);", snapshot)[1], re.I)


@pytest.mark.parametrize(
    ("source", "direction"),
    [
        ("/assets/images/plus.svg", "increase"),
        ("qty-plus", "increase"),
        ("btn increment", "increase"),
        ("/assets/images/minus.svg", "decrease"),
        ("stepper_decrease", "decrease"),
    ],
)
def test_stepper_direction_is_read_from_ids_classes_and_icon_paths(source, direction):
    assert step_pattern(direction).search(source)


@pytest.mark.parametrize("source", ["surplus-note", "btnPlus", "minuscule", "/img/plush.png"])
def test_stepper_direction_ignores_words_that_only_contain_plus_or_minus(source):
    assert not step_pattern("increase").search(source)
    assert not step_pattern("decrease").search(source)


def test_stepper_detection_and_button_names_share_one_test():
    from pathlib import Path

    snapshot = Path("jev_ultrafast/snapshot.js").read_text()
    assert "if (increase(e)) return 'Increase';" in snapshot
    assert "if (decrease(e)) return 'Decrease';" in snapshot
    assert "clickable.has(control) && (increase(control) || decrease(control))" in snapshot
    assert "const editable=!stepper" in snapshot


def test_luckyseat_show_stage_waits_for_performance_controls():
    from examples.luckyseat import stages

    check = stages("Hadestown")[1][1]
    assert "/dash/shows/" in check
    assert "Hadestown" in check
    assert "input[type=checkbox]" in check


def test_luckyseat_submit_stage_requires_independent_confirmation():
    from examples.luckyseat import stages

    check = stages("Hadestown")[-1][1]
    assert check is not None
    assert "successfully submitted" in check
    assert "received" in check
    assert "/dash/results" in check
    assert "Hadestown" in check
    assert "ticket" in check


def test_luckyseat_gate_covers_link_styled_confirmation_controls():
    from types import SimpleNamespace

    from examples.luckyseat import gated

    action = {"id": "e1", "kind": "click", "role": "link", "label": "Confirm & Submit"}
    agent = SimpleNamespace(state={"decision": {"choice": "e1"}, "page": {"actions": [action]}})
    assert gated(agent) == action


def test_luckyseat_submission_evidence_reads_visible_errors_and_invalid_fields():
    from examples.luckyseat import SUBMISSION_EVIDENCE

    assert '[role="dialog"]' in SUBMISSION_EVIDENCE
    assert '[role="alert"]' in SUBMISSION_EVIDENCE
    assert "checkVisibility" in SUBMISSION_EVIDENCE
    assert "checkValidity" in SUBMISSION_EVIDENCE
    assert "mat-snack-bar-container" in SUBMISSION_EVIDENCE


def test_luckyseat_network_log_records_method_path_and_status_only():
    from examples.luckyseat import ARM_NETWORK_LOG

    assert "u.origin + u.pathname" in ARM_NETWORK_LOG
    for leaked in ("body", "text()", "json()", "responseText", "search"):
        assert leaked not in ARM_NETWORK_LOG


def test_luckyseat_submission_network_waits_for_a_slow_submit():
    from types import SimpleNamespace

    from examples.luckyseat import submission_network

    post = {"method": "POST", "url": "https://example.test/api/entries", "status": None}
    get = {"method": "GET", "url": "https://example.test/api/shows", "status": 200}
    logs = [[post], [post, get], [{**post, "status": 201}, get]]
    browser = SimpleNamespace(evaluate=Mock(side_effect=lambda _: logs.pop(0) if len(logs) > 1 else logs[0]))

    result = submission_network(SimpleNamespace(browser=browser), settle=0, timeout=5)

    assert result == {"navigated": False, "complete": True, "requests": [{**post, "status": 201}]}


def test_luckyseat_submission_network_reports_requests_still_in_flight():
    from types import SimpleNamespace

    from examples.luckyseat import submission_network

    post = {"method": "POST", "url": "https://example.test/api/entries", "status": None}
    browser = SimpleNamespace(evaluate=Mock(return_value=[post]))

    result = submission_network(SimpleNamespace(browser=browser), settle=0, timeout=0.2)

    assert result == {"navigated": False, "complete": False, "requests": [{**post, "status": "pending"}]}


def test_luckyseat_submission_network_leaves_out_opaque_beacons_and_keeps_failures():
    from types import SimpleNamespace

    from examples.luckyseat import submission_network

    log = [
        {"method": "POST", "url": "https://analytics.example/g/collect", "status": "opaque"},
        {"method": "GET", "url": "https://example.test/api/entries", "status": 500},
        {"method": "POST", "url": "https://example.test/api/entries", "status": 0},
    ]
    browser = SimpleNamespace(evaluate=Mock(return_value=log))

    result = submission_network(SimpleNamespace(browser=browser), settle=0)

    assert result["requests"] == log[1:]


def test_luckyseat_submission_network_reports_a_full_page_load():
    from types import SimpleNamespace

    from examples.luckyseat import submission_network

    browser = SimpleNamespace(evaluate=Mock(return_value=None))
    assert submission_network(SimpleNamespace(browser=browser))["navigated"] is True


def test_luckyseat_results_check_ignores_entries_listed_before_the_submit():
    from examples.luckyseat import new_entries

    old = "Hadestown | New York, NY | Oct 1 at 7:00 PM | 1 ticket(s)"
    new = "Hadestown | New York, NY | Oct 8 at 7:00 PM | 1 ticket(s)"
    assert new_entries([old], [old]) == []
    assert new_entries([old], [old, new]) == [new]
    assert new_entries([], [old]) == [old]


def test_luckyseat_results_entries_waits_for_the_entries_request_and_a_still_list(monkeypatch):
    from examples import luckyseat

    # [rows, text length, requests in flight, entries request returned 2xx]: an empty list read while
    # the entries request is still out must never be taken as "no entries".
    reads = [None, [[], 100, 1, False], [[], 100, 1, False], [["Hadestown | 1 ticket(s)"], 180, 0, True]]
    browser = Mock()
    browser.evaluate.side_effect = lambda _: reads.pop(0) if len(reads) > 1 else reads[0]
    monkeypatch.setattr(luckyseat.time, "sleep", lambda _: None)

    rows = luckyseat.results_entries("Hadestown", open_browser=Mock(return_value=browser), still=0)

    assert rows == ["Hadestown | 1 ticket(s)"]
    assert browser.call.call_args_list[0].args == ("Page.addScriptToEvaluateOnNewDocument",)
    assert browser.call.call_args_list[1].kwargs == {"url": luckyseat.RESULTS_URL}
    browser.close.assert_called_once()


def test_luckyseat_results_entries_never_settles_while_a_request_is_in_flight():
    from examples import luckyseat

    browser = Mock()
    browser.evaluate.return_value = [[], 100, 1, False]
    rows = luckyseat.results_entries("Hadestown", open_browser=Mock(return_value=browser), timeout=0.3, still=0)
    assert rows is None


def test_luckyseat_results_entries_waits_for_the_entries_request_even_on_a_quiet_page(monkeypatch):
    from examples import luckyseat

    # Nothing in flight and nothing changing, but the entries request has not been sent yet (the
    # route's code is still loading): an empty baseline here would make old entries look new.
    quiet = [[], 100, 0, False]
    browser = Mock()
    browser.evaluate.return_value = quiet
    monkeypatch.setattr(luckyseat.time, "sleep", lambda _: None)
    assert luckyseat.results_entries("Hadestown", open_browser=Mock(return_value=browser), timeout=0.2, still=0) is None

    # Once it has returned, an empty list is a real "no entries yet".
    browser.evaluate.return_value = [[], 100, 0, True]
    assert luckyseat.results_entries("Hadestown", open_browser=Mock(return_value=browser), still=0) == []


def test_luckyseat_final_stage_needs_a_new_results_entry_not_confirmation_text():
    from examples.luckyseat import final_stage_passed

    found = Mock(return_value=True)
    missing = Mock(return_value=False)
    # Confirmation text alone is not enough; the fresh Results read decides.
    assert final_stage_passed(True, False, None, missing) is False
    # A submit whose page shows no recognised text still passes on a DONE claim if Results has it.
    assert final_stage_passed(False, True, None, found) is True
    # An earlier read that found a new entry (right after the approved click) stands.
    never = Mock()
    assert final_stage_passed(False, False, {"new": ["Hadestown | Oct 8 | 1 ticket(s)"]}, never) is True
    never.assert_not_called()
    # Without a trigger there is no read.
    assert final_stage_passed(False, False, {"new": []}, never) is False
    never.assert_not_called()


def test_luckyseat_results_entries_is_unknown_when_the_list_never_loads():
    from examples import luckyseat

    browser = Mock()
    browser.evaluate.return_value = None
    assert luckyseat.results_entries("Hadestown", open_browser=Mock(return_value=browser), timeout=0.3) is None
    browser.close.assert_called_once()


@pytest.mark.parametrize(
    ("status", "blocks"),
    [
        ({"checkbox": True, "solved": False}, True),
        ({"checkbox": True, "solved": True}, False),
        # Invisible reCAPTCHA issues its token when the gated button is pressed.
        ({"checkbox": False, "invisible": True, "solved": False}, False),
        # Turnstile keeps its token in cf-turnstile-response, never g-recaptcha-response.
        ({"checkbox": False, "invisible": False, "solved": False}, False),
    ],
)
def test_luckyseat_captcha_gate_waits_only_for_a_visible_checkbox(status, blocks):
    from examples.luckyseat import captcha_blocks_approval

    assert captcha_blocks_approval(status) is blocks


def test_luckyseat_keeps_clicks_inside_the_physical_viewport():
    from inspect import getsource

    from examples.luckyseat import main

    source = getsource(main)
    assert "viewport=(1120, 780)" in source
    assert "viewport=(1120, 3200)" not in source


def test_observation_waits_for_scroll_to_settle(monkeypatch):
    from jev_ultrafast import browser as browser_module

    browser = browser_module.Browser.__new__(browser_module.Browser)
    browser.session = "test-session"
    browser.after_input = {"kind": "scroll"}
    browser.call = Mock(return_value={})
    expected = {"url": "https://example.test", "marker": "stable"}
    monkeypatch.setattr(browser_module, "browser_operation", Mock(return_value=expected))

    assert browser.observe(screenshot=False) == expected
    expressions = [call.kwargs.get("expression", "") for call in browser.call.call_args_list]
    assert any(
        "changed && stable>=3" in expression and "!changed && elapsed>250" in expression
        for expression in expressions
    )


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target"}


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Find a book", [])


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })

    def post(_url, _key, body):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] is False
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(p, "Search with free cancellation", [])
    assert d["choice"] == "e3"


def test_quoted_task_text_still_uses_the_llm(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    assert model.field_text(context)[0] == "Zurich"
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        model.field_text({"goal": 'Enter "Zurich"'})


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return a


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2  # The first call rejects before any browser input.
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    runner.state["browser"].act.assert_called_once()


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import jev_ultrafast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book")
    operation.assert_not_called()


def test_fill_notifies_framework_controlled_fields(monkeypatch):
    import jev_ultrafast.browser as browser

    cdp = Mock(return_value={"result": {"value": {"x": 10, "y": 20}}})
    monkeypatch.setattr(browser, "cdp", cdp)
    browser_operation({
        "operation": "act",
        "session": "test",
        "action": {"id": "e1", "kind": "fill", "node": 1},
        "text": "1",
    })

    evaluations = [call for call in cdp.call_args_list if call.args[0] == "Runtime.evaluate"]
    assert len(evaluations) == 2
    committed = evaluations[-1].kwargs["expression"]
    assert "new Event('input'" in committed
    assert "new Event('change'" in committed


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


@pytest.mark.parametrize("changed", ["Departure", "Where from?", "Where to?", "year"])
def test_flight_verification_rejects_wrong_trip(changed):
    from examples.flights import verify

    actual = {
        "url": "https://www.google.com/travel/flights/search?tfs=example",
        "text": "Track prices from Zürich to London departing 2026-09-20",
        "actions": [
            {"label": k, "value": v}
            for k, v in [
                ("Change ticket type. One way", "One way"),
                ("Where from?", "Zürich"),
                ("Where to?", "London"),
                ("Departure", "Sun, Sep 20"),
                ("Nonstop flight on Sunday, September 20. Select flight", ""),
            ]
        ],
    }
    assert verify(actual)["passed"]
    if changed == "year":
        actual["text"] = actual["text"].replace("2026", "2027")
    else:
        next(a for a in actual["actions"] if a["label"] == changed)["value"] = "wrong"
    assert not verify(actual)["passed"]


@pytest.mark.parametrize(
    "content", ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}']
)
def test_text_helper_rejects_invalid_values(monkeypatch, content):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text({"goal": "Find a flight"})


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()
