"""Offline contracts for the Android backend and the Hamilton example, on captured app screens. No paid APIs."""

import re
from pathlib import Path
from unittest.mock import Mock

import pytest

from examples import hamilton
from jev_ultrafast.android import Device, fingerprint, page_state, point, read_nodes, tap_point
from jev_ultrafast.browser import StalePage

SCREENS = Path(__file__).with_name("fixtures") / "hamilton"
PACKAGE = "com.hamilton.app"


def screen(name, **replace):
    xml = (SCREENS / f"{name}.xml").read_text()
    for old, new in replace.items():
        xml = xml.replace(old, new)
    return read_nodes(xml, PACKAGE, hamilton.COUNTDOWN)


def state(nodes, hide=hamilton.private):
    s = page_state(nodes, PACKAGE, frozenset(hide(nodes)))
    s["fingerprint"] = fingerprint(s)
    return s


def action(s, label):
    return next(a for a in s["actions"] if a["label"].endswith(label))


def test_only_the_app_is_read_and_a_countdown_does_not_change_the_screen():
    nodes = screen("list-card")
    assert all("Battery" not in n["label"] for n in nodes)
    assert not any(re.search(r"\d{3}:\d{2}:\d{2}", n["label"]) for n in nodes)
    xml = (SCREENS / "list-card.xml").read_text()
    ticked = re.sub(r"\b1(\d{2}):(\d{2}):(\d{2})\b", r"0\1:\2:\3", xml)
    assert ticked != xml
    assert state(read_nodes(ticked, PACKAGE, hamilton.COUNTDOWN))["fingerprint"] == state(nodes)["fingerprint"]


def test_scrolled_plain_text_changes_the_screen():
    nodes = screen("entry-top")
    moved = screen("entry-top", **{"[105,1391][975,1431]": "[105,1291][975,1331]"})
    assert next(n for n in moved if n["label"] == "PERFORMANCE TIME")["rect"][1] == 1291
    assert state(moved)["text"] == state(nodes)["text"]
    assert state(moved)["fingerprint"] != state(nodes)["fingerprint"]


def test_repeated_enter_now_is_named_by_its_card():
    s = state(screen("list-card"))
    enters = [a for a in s["actions"] if a["label"].endswith(" · Enter Now")]
    assert [hamilton.PERFORMANCE.search(a["label"]).group(1) for a in enters] == [
        "October 6, 2026 7:00pm",
        "October 7, 2026 7:00pm",
    ]


def test_checkbox_rows_are_tapped_on_their_box_not_their_links():
    s = state(screen("entry-bottom"))
    boxes = [a for a in s["actions"] if a.get("role") == "checkbox"]
    assert len(boxes) == 2
    for box in boxes:
        x, y = box["tap"]
        assert box["rect"]["x"] <= x <= box["rect"]["x"] + 120
        assert box["rect"]["y"] <= y <= box["rect"]["y"] + 50


def test_unlabeled_ticket_button_is_named_by_its_row():
    s = state(screen("entry-bottom"))
    assert any(a["label"] == "Unlabeled button between “Number of Tickets” and “2”" for a in s["actions"])


@pytest.mark.parametrize("name", ["entry-top", "entry-bottom"])
def test_entrant_name_is_kept_from_the_model(name):
    # entry-top: the name sits on the bottom edge and the notice below it is off screen.
    nodes = screen(name)
    assert "Test Entrant" in [n["label"] for n in nodes]
    s = state(nodes)
    assert "Test Entrant" not in s["text"] and "Test Entrant" not in str(s["actions"])
    assert "Test Entrant" not in s["text_below"]


def test_list_page_hides_nothing():
    assert hamilton.private(screen("list-card")) == set()


def test_disabled_submit_is_text_not_a_target():
    s = state(screen("entry-bottom"))
    assert "Submit Your Entry" in s["text"]
    assert not any(a["label"] == "Submit Your Entry" for a in s["actions"])


def test_a_dtd_is_refused_before_parsing():
    with pytest.raises(ValueError, match="DTD"):
        read_nodes('<!DOCTYPE x [<!ENTITY a "b">]><hierarchy/>', PACKAGE)


def phone(name):
    """A Device with no phone behind it: every read returns a captured screen."""
    d = Device.__new__(Device)
    d.package, d.volatile, d.hide, d.tap = PACKAGE, hamilton.COUNTDOWN, hamilton.private, hamilton.tap_point
    d.after_input, d.edges, d.on_read, d.on_input = None, {}, None, None
    d.device = Mock(dump_hierarchy=Mock(return_value=(SCREENS / f"{name}.xml").read_text()))
    d.shell = Mock()
    return d


def test_executor_rejects_a_changed_screen_before_any_input():
    d = phone("list-card")
    s = d.read()
    d.device.dump_hierarchy.return_value = (SCREENS / "entry-top.xml").read_text()
    with pytest.raises(StalePage):
        d.act(action(s, "Enter Now"), s)
    d.shell.assert_not_called()


def test_an_app_tap_point_is_used_only_inside_its_node():
    nodes = [dict(n) for n in screen("list-card")]
    back = next(n for n in nodes if n["label"] == "Back")
    inside = (back["rect"][0] + 5, back["rect"][1] + 5)
    assert point(back, lambda n: (0, 0)) == tap_point(back)
    assert point(back, lambda n: inside) == inside
    lottery = {**back, "label": "Lottery", "class": "ImageView", "rect": (63, 242, 257, 441)}
    assert hamilton.tap_point(lottery) == (111, 291)


def test_a_scroll_that_changed_nothing_is_not_offered_again():
    d = phone("list-card")
    d.settle = Mock()
    s = d.read()
    up = next(a for a in s["actions"] if a["id"] == "scroll_up")
    d.act(up, s)
    after = d.observe()
    assert d.device.touch.up.called
    assert [a["id"] for a in after["actions"] if a["kind"] == "scroll"] == ["scroll_down"]
    assert "scroll_up" not in [a["id"] for a in d.read()["actions"]]
    assert d.read()["fingerprint"] == s["fingerprint"]


def refuse(nodes, label, target="October 6, 2026 7:00pm", submitted=()):
    s = state(nodes)
    return hamilton.refusal(nodes, action(s, label), target, 2, set(submitted))


def test_only_the_requested_card_can_be_entered():
    nodes = screen("list-card")
    assert refuse(nodes, "Enter Now") is None
    assert refuse(nodes, "Enter Now", target=None) is not None
    enters = [a for a in state(nodes)["actions"] if a["label"].endswith(" · Enter Now")]
    other = hamilton.refusal(nodes, enters[1], "October 6, 2026 7:00pm", 2, set())
    assert other == "That Enter Now belongs to October 7, 2026 7:00pm, not October 6, 2026 7:00pm."


@pytest.mark.parametrize("label", ["BIRMINGHAM Tab 2 of 4", "100", "12:00pm"])
def test_controls_outside_the_flow_are_refused(label):
    assert refuse(screen("list-card"), label) is not None


@pytest.mark.parametrize("label", ["Update your Profile", "Broadway Official Rules", "Broadway Details"])
def test_entry_page_links_are_refused(label):
    assert refuse(screen("entry-bottom"), label) is not None


def test_a_confirmation_can_be_closed_only_after_this_performance_was_submitted():
    nodes = screen("list-card", **{'content-desc="Back"': 'content-desc="OK"'})
    assert refuse(nodes, "OK") is not None
    assert refuse(nodes, "OK", submitted={"October 6, 2026 7:00pm"}) is None
    assert refuse(nodes, "OK", submitted={"October 7, 2026 7:00pm"}) is not None


def test_a_ticked_box_is_never_unticked():
    nodes = screen("entry-bottom")
    assert refuse(nodes, "before entering the Lottery.") is None
    ticked = screen("entry-bottom", **{'checkable="true" checked="false"': 'checkable="true" checked="true"'})
    assert "never untick" in refuse(ticked, "before entering the Lottery.")


def ready_to_submit():
    return screen(
        "entry-bottom",
        **{
            'checkable="true" checked="false"': 'checkable="true" checked="true"',
            'content-desc="Submit Your Entry" checkable="false" checked="false" clickable="false" enabled="false"': (
                'content-desc="Submit Your Entry" checkable="false" checked="false" clickable="true" enabled="true"'
            ),
        },
    )


def test_submit_needs_the_right_page_both_boxes_and_no_earlier_submit():
    nodes = ready_to_submit()
    assert refuse(nodes, "Submit Your Entry") is None
    assert "not October 7" in refuse(nodes, "Submit Your Entry", target="October 7, 2026 7:00pm")
    assert "never submitted again" in refuse(nodes, "Submit Your Entry", submitted={"October 6, 2026 7:00pm"})
    one_box = screen(
        "entry-bottom",
        **{
            'content-desc="Submit Your Entry" checkable="false" checked="false" clickable="false" enabled="false"': (
                'content-desc="Submit Your Entry" checkable="false" checked="false" clickable="true" enabled="true"'
            ),
        },
    )
    assert "Tick both boxes" in refuse(one_box, "Submit Your Entry")


def test_blur_covers_the_name_between_every_two_reads():
    from scripts.render_hamilton import PAD_PIXELS, PAD_SECONDS, blur_windows

    low, high = [63, 1122, 296, 1174], [63, 500, 296, 552]
    hidden = [[10.0, []], [10.5, [low]], [11.0, [high]], [12.0, []], [13.0, []]]
    windows = blur_windows(hidden, 10.0, 2400)
    assert len(windows) == 3
    # While the name moves between two reads, the band covers both places it was read at.
    start, end, top, bottom = windows[1]
    assert top == high[1] - PAD_PIXELS and bottom == low[3] + PAD_PIXELS
    assert start == 0.5 - PAD_SECONDS and end == 1.0 + PAD_SECONDS
    # From the read before it appeared to the read after it left, no moment is uncovered.
    assert windows[0][0] == 0.0 and windows[-1][1] == 2.0 + PAD_SECONDS


def test_end_card_reports_the_run_from_its_summary():
    from scripts.render_hamilton import results

    open_card = {"enter_now": True, "entered": False, "inside": ["Enter Now"]}
    entered_card = {"enter_now": False, "entered": True, "inside": ["YOU’VE ENTERED!"]}
    shows = ["October 6, 2026 7:00pm", "October 7, 2026 7:00pm"]
    base = {"targets": shows, "entries_ms": 18_400, "jev_calls": 14, "cost_usd": 0.00137}
    dry = {**base, "dry_run": True, "dry_run_stops": [{}, {}], "submitted": [],
           "verified_after_restart": {shows[0]: open_card, shows[1]: open_card}}
    headline, detail, rows = results(dry)
    assert (headline, "dry run" in detail) == ("2 / 2", True)
    assert rows == [("Real time", "18.4 s"), ("Jev decisions", "14"), ("Model cost", "US$0.0014"),
                    ("Restarted app, still open", "2 / 2")]
    real = {**base, "dry_run": False, "dry_run_stops": [], "submitted": [shows[0]],
            "verified_after_restart": {shows[0]: None, shows[1]: open_card},
            "verified_after_restart_rescan": {shows[0]: entered_card, shows[1]: open_card}}
    headline, detail, rows = results(real)
    assert (headline, detail, rows[-1]) == ("1 / 2", "entries submitted", ("Restarted app, marked entered", "1 / 2"))


def test_the_middle_cut_keeps_the_first_and_last_performance():
    from scripts.render_hamilton import middle_cut

    shows = ["A", "B", "C", "D"]
    events = [{"t": t, "performance": p} for p, t in [("A", 1), ("A", 4), ("B", 6), ("B", 9), ("C", 12), ("C", 15),
                                                      ("D", 17), ("D", 20)]]
    assert middle_cut(events, shows) == (4.3, 15, 2)
    # With inputs, the cut runs on to the caption of the last performance's first input.
    cut = middle_cut(events, shows, [{"started": 14.0}, {"started": 16.2}, {"started": 18.0}])
    assert cut[0] == 4.3 and abs(cut[1] - 15.9) < 1e-9 and cut[2] == 2
    assert middle_cut(events, shows, [{"started": 15.2}]) == (4.3, 15, 2)
    assert middle_cut(events, shows[:2]) is None


def test_an_entered_card_is_read_by_its_undated_performance():
    card = {"index": 0, "class": "View", "resource_id": "", "clickable": False, "enabled": True, "checkable": False,
            "checked": False, "selected": False, "scrollable": False, "rect": (0, 352, 1080, 900),
            "label": "YOU’VE ENTERED! You have successfully entered the lottery for October 7, 7:00pm"}
    cards = hamilton.read_cards([card])
    assert cards == {"October 7 7:00pm": {"enter_now": False, "entered": True, "inside": [card["label"]]}}
    assert hamilton.undated("October 7, 2026 7:00pm") == "October 7 7:00pm"


def test_screen_checks_read_the_entry_page():
    top, bottom = screen("entry-top"), ready_to_submit()
    assert hamilton.on_entry(top) and not hamilton.on_list(top)
    assert hamilton.entry_performance(top) == hamilton.entry_performance(bottom) == "October 6, 2026 7:00pm"
    assert hamilton.tickets_shown(top) == 2
    assert [b["checked"] for b in hamilton.entry_boxes(bottom)] == [True, True]
    assert hamilton.on_list(screen("list-card"))
