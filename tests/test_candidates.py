"""The candidate builder, checked against recorded screens."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

import labels as L
from conftest import build, fixture_text, names

from jev_hands.core import candidates as candidates_mod
from jev_hands.core.models import (
    KIND_CONTAINER,
    KIND_INTERACTIVE,
    KIND_TEXT_LEAF,
    OBSERVE_EMPTY_TREE,
    OBSERVE_FAILED_HINTS,
    OBSERVE_FAILED_REASONS,
    OBSERVE_FAILED_SUMMARIES,
    OBSERVE_HIDDEN_TREE,
)

SEARCH_BAR_ID = "search_layout_area"
SEARCH_BUTTON_ID = "search_button"


def test_search_bar_survives_the_container_rule():
    """Round one lost the whole search bar because one small button inside it
    was clickable. The bar is seven times the button's area, so both stay."""
    screen = build("delivery_home.xml")
    ids = [e.id for e in screen.elements]
    assert SEARCH_BAR_ID in ids
    assert SEARCH_BUTTON_ID in ids

    bar = next(e for e in screen.elements if e.id == SEARCH_BAR_ID)
    button = next(e for e in screen.elements if e.id == SEARCH_BUTTON_ID)
    bar_area = (bar.bounds[2] - bar.bounds[0]) * (bar.bounds[3] - bar.bounds[1])
    button_area = (button.bounds[2] - button.bounds[0]) * (button.bounds[3] - button.bounds[1])
    assert button_area < 0.5 * bar_area


def test_duplicate_names_get_a_short_id_suffix():
    """Two elements called the same thing must be distinguishable without
    resorting to coordinates."""
    screen = build("delivery_home.xml")
    suffixed = [n for n in names(screen) if n.startswith(L.SEARCH + "#")]
    assert f"{L.SEARCH}#{SEARCH_BAR_ID}" in suffixed
    assert f"{L.SEARCH}#{SEARCH_BUTTON_ID}" in suffixed
    assert len(names(screen)) == len(set(names(screen)))


def test_profile_page_surfaces_text_leaves():
    """The profile page has 300-odd nodes and four clickable ones. Without text
    leaves the model sees only the bottom tabs and gives up."""
    screen = build("delivery_profile.xml")
    shown = names(screen)
    assert L.ORDERS in shown
    assert len(screen.elements) > 30
    order = next(e for e in screen.elements if e.name == L.ORDERS)
    assert order.kind == KIND_TEXT_LEAF


def test_a_scrollable_wrapper_does_not_hide_its_text():
    """A ScrollView is a container, not something you tap, so it must not count
    as an interactive ancestor."""
    screen = build("delivery_profile.xml")
    kinds = {e.kind for e in screen.elements}
    assert KIND_TEXT_LEAF in kinds
    assert KIND_CONTAINER in kinds


def test_token_budget_truncates_and_reports():
    screen = build("delivery_food_list.xml", token_budget=600)
    assert screen.truncated is not None
    assert screen.truncated["shown"] < screen.truncated["total"]
    assert screen.truncated["shown"] == len(screen.elements)
    assert [e.index for e in screen.elements] == list(range(len(screen.elements)))
    state = candidates_mod.screen_state(screen)
    assert state["screen_truncated"].startswith("showing ")


def test_the_busiest_recorded_page_fits_the_default_budget():
    """Recorded high-water mark: the delivery list is the biggest real screen in
    the fixtures and still fits 1500 tokens, so the cap only bites on worse."""
    screen = build("delivery_food_list.xml")
    assert screen.truncated is None
    state = candidates_mod.screen_state(screen)
    import json

    rendered = json.dumps(state, ensure_ascii=False)
    assert candidates_mod.estimate_tokens(rendered) < 1500


def test_budget_drops_unnamed_rows_first():
    full = build("delivery_home.xml")
    unnamed_before = sum(1 for e in full.elements if e.name.startswith(candidates_mod.NO_LABEL))
    assert unnamed_before > 0
    tight = build("delivery_home.xml", token_budget=400)
    unnamed_after = sum(1 for e in tight.elements if e.name.startswith(candidates_mod.NO_LABEL))
    assert unnamed_after < unnamed_before


def test_decorative_windows_are_the_only_ones_kept_out_of_the_table():
    """The uiautomator2 dump carries the status bar and the gesture bar as
    separate windows. Both are decorative, so the table is the app's alone, and
    with one window left there is nothing for a `window` column to say."""
    raw = fixture_text("u2_delivery_home_with_systemui.xml")
    root = candidates_mod.parse_hierarchy(raw)
    packages = {n.package for n in root.iter_descendants() if n.package}
    assert len(packages) > 1

    screen = build("u2_delivery_home_with_systemui.xml")
    assert screen.app["package"] == "com.example.delivery"
    assert screen.app["windows"] == ["com.example.delivery"]
    adb_screen = build("delivery_home.xml")
    assert len(screen.elements) == len(adb_screen.elements)

    # Dropping the two decorative windows from the dump has to leave the table
    # byte for byte what it already was.
    stripped = ET.fromstring(raw)
    for child in [c for c in stripped if c.get("package") != "com.example.delivery"]:
        stripped.remove(child)
    alone = candidates_mod.build_screen(ET.tostring(stripped, encoding="unicode"))
    assert candidates_mod.screen_state(screen) == candidates_mod.screen_state(alone)

    foreign_ids = {
        n.resource_id.split("/")[-1]
        for n in root.iter_descendants()
        if n.package and n.package != "com.example.delivery" and n.resource_id
    }
    kept_ids = {e.id for e in screen.elements if e.id}
    assert not (foreign_ids & kept_ids)

    rows = candidates_mod.screen_state(screen)["elements"]
    assert not any("window" in row for row in rows), "one window, so no column for it"


def test_an_app_that_hides_its_tree_yields_nothing_and_says_why():
    screen = build("chat_empty_tree.xml")
    assert screen.elements == []
    assert screen.unreadable == OBSERVE_HIDDEN_TREE
    assert screen.note is not None
    lowered = screen.note.lower()
    assert "secure window" in lowered
    assert "screenshot" in lowered
    assert screen.to_json()["unreadable"] == OBSERVE_HIDDEN_TREE


def test_a_dump_with_nothing_in_it_at_all_is_an_empty_tree():
    screen = candidates_mod.build_screen('<hierarchy rotation="0" />')
    assert screen.elements == []
    assert screen.unreadable == OBSERVE_EMPTY_TREE
    assert "screenshot" in (screen.note or "").lower()


def test_a_hidden_window_is_still_a_hidden_tree_behind_the_status_bar():
    """The status bar is nearly always there. A foreground window with nothing
    in it is the thing worth reporting, not the decoration around it."""
    raw = (
        '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
        '<node index="0" text="" resource-id="" class="android.widget.FrameLayout" '
        'package="com.android.systemui" content-desc="" clickable="false" '
        'enabled="true" bounds="[0,0][1080,110]" />'
        '<node index="1" text="" resource-id="" class="" package="com.example.chat" '
        'content-desc="" clickable="false" enabled="false" bounds="[0,0][0,0]" />'
        "</hierarchy>"
    )
    screen = candidates_mod.build_screen(raw)
    assert screen.elements == []
    assert screen.app["package"] == "com.example.chat"
    assert screen.unreadable == OBSERVE_HIDDEN_TREE


def test_a_window_with_content_in_it_is_not_called_hidden():
    screen = build("delivery_home.xml")
    assert screen.unreadable is None
    assert screen.to_json().get("unreadable") is None


def test_every_observe_failed_reason_carries_a_summary_and_a_hint():
    for reason in OBSERVE_FAILED_REASONS:
        assert OBSERVE_FAILED_SUMMARIES[reason]
        assert OBSERVE_FAILED_HINTS[reason]


def test_visible_to_user_false_is_honoured():
    raw = fixture_text("u2_delivery_home_with_systemui.xml")
    hidden = raw.replace('visible-to-user="true"', 'visible-to-user="false"', 1)
    baseline = candidates_mod.build_screen(raw)
    trimmed = candidates_mod.build_screen(hidden)
    assert len(trimmed.elements) <= len(baseline.elements)

    marked = '<node index="0" text="ghost" resource-id="x/ghost" class="android.widget.TextView" package="com.example" content-desc="" clickable="true" enabled="true" visible-to-user="false" bounds="[0,0][100,100]" />'
    wrapped = (
        '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
        '<node index="0" class="android.widget.FrameLayout" package="com.example" '
        'bounds="[0,0][1000,1000]" enabled="true">' + marked + "</node></hierarchy>"
    )
    screen = candidates_mod.build_screen(wrapped)
    assert screen.elements == []


def test_zero_area_and_disabled_nodes_are_dropped():
    xml_text = (
        '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
        '<node index="0" class="android.widget.FrameLayout" package="com.example" '
        'bounds="[0,0][1000,1000]" enabled="true">'
        '<node index="0" text="zero" class="android.widget.Button" package="com.example" '
        'clickable="true" enabled="true" bounds="[10,10][10,10]" />'
        '<node index="1" text="off" class="android.widget.Button" package="com.example" '
        'clickable="true" enabled="false" bounds="[0,0][100,100]" />'
        '<node index="2" text="gone" class="android.widget.Button" package="com.example" '
        'clickable="true" enabled="true" bounds="[2000,2000][2100,2100]" />'
        '<node index="3" text="keep" class="android.widget.Button" package="com.example" '
        'clickable="true" enabled="true" bounds="[0,200][100,300]" />'
        "</node></hierarchy>"
    )
    screen = candidates_mod.build_screen(xml_text)
    assert names(screen) == ["keep"]


def test_names_fall_back_through_desc_text_children_then_no_label():
    xml_text = (
        '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
        '<node index="0" class="android.widget.FrameLayout" package="com.example" '
        'bounds="[0,0][1000,1000]" enabled="true">'
        '<node index="0" content-desc="described" text="ignored" class="android.widget.Button" '
        'package="com.example" clickable="true" enabled="true" bounds="[0,0][100,50]" />'
        '<node index="1" text="plain" class="android.widget.Button" package="com.example" '
        'clickable="true" enabled="true" bounds="[0,60][100,110]" />'
        '<node index="2" class="android.widget.LinearLayout" package="com.example" '
        'clickable="true" enabled="true" bounds="[0,120][100,200]">'
        '<node index="0" text="a" class="android.widget.TextView" package="com.example" bounds="[0,120][50,160]" />'
        '<node index="1" text="a" class="android.widget.TextView" package="com.example" bounds="[50,120][100,160]" />'
        '<node index="2" text="b" class="android.widget.TextView" package="com.example" bounds="[0,160][50,200]" />'
        '<node index="3" text="c" class="android.widget.TextView" package="com.example" bounds="[50,160][80,200]" />'
        '<node index="4" text="d" class="android.widget.TextView" package="com.example" bounds="[80,160][100,200]" />'
        "</node>"
        '<node index="3" class="android.widget.ImageView" package="com.example" '
        'clickable="true" enabled="true" bounds="[0,210][100,260]" />'
        "</node></hierarchy>"
    )
    screen = candidates_mod.build_screen(xml_text)
    labels = names(screen)
    assert labels[0] == "described"
    assert labels[1] == "plain"
    # Deduped and capped at three segments.
    assert labels[2] == "a b c"
    assert labels[3] == candidates_mod.NO_LABEL


def test_a_child_text_segment_contained_in_another_is_dropped():
    """Android rows routinely carry the same label twice, short and long. Both
    wastes the name budget and reads as two things to the model."""
    xml_text = (
        '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
        '<node index="0" class="android.widget.FrameLayout" package="com.example" '
        'bounds="[0,0][1000,1000]" enabled="true">'
        '<node index="0" class="android.widget.LinearLayout" package="com.example" '
        'clickable="true" enabled="true" bounds="[0,0][400,100]">'
        '<node index="0" content-desc="Wifi, connected" class="android.widget.TextView" '
        'package="com.example" bounds="[0,0][200,50]" />'
        '<node index="1" text="Wifi" class="android.widget.TextView" '
        'package="com.example" bounds="[0,50][200,100]" />'
        '<node index="2" text="connected" class="android.widget.TextView" '
        'package="com.example" bounds="[200,50][400,100]" />'
        "</node></node></hierarchy>"
    )
    screen = candidates_mod.build_screen(xml_text)
    assert names(screen) == ["Wifi, connected"]


def test_a_longer_segment_replaces_the_shorter_one_it_contains():
    xml_text = (
        '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
        '<node index="0" class="android.widget.FrameLayout" package="com.example" '
        'bounds="[0,0][1000,1000]" enabled="true">'
        '<node index="0" class="android.widget.LinearLayout" package="com.example" '
        'clickable="true" enabled="true" bounds="[0,0][400,100]">'
        '<node index="0" text="Battery" class="android.widget.TextView" '
        'package="com.example" bounds="[0,0][200,50]" />'
        '<node index="1" text="Battery and charging" class="android.widget.TextView" '
        'package="com.example" bounds="[0,50][400,100]" />'
        "</node></node></hierarchy>"
    )
    screen = candidates_mod.build_screen(xml_text)
    assert names(screen) == ["Battery and charging"]


def test_the_settings_rows_no_longer_repeat_their_own_label():
    screen = build("settings_home.xml")
    for element in screen.elements:
        words = element.name.split()
        assert len(words) == len(set(words)) or len(words) < 2, element.name


def test_names_are_capped_at_forty_characters():
    long_text = "x" * 200
    xml_text = (
        '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
        '<node index="0" class="android.widget.FrameLayout" package="com.example" '
        'bounds="[0,0][1000,1000]" enabled="true">'
        f'<node index="0" text="{long_text}" class="android.widget.Button" package="com.example" '
        'clickable="true" enabled="true" bounds="[0,0][100,50]" />'
        "</node></hierarchy>"
    )
    screen = candidates_mod.build_screen(xml_text)
    assert len(screen.elements[0].name) == 40


def test_container_rule_drops_an_ancestor_its_children_fill():
    xml_text = (
        '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
        '<node index="0" class="android.widget.FrameLayout" package="com.example" '
        'bounds="[0,0][1000,1000]" enabled="true">'
        '<node index="0" content-desc="wrapper" class="android.widget.LinearLayout" '
        'package="com.example" clickable="true" enabled="true" bounds="[0,0][100,100]">'
        '<node index="0" content-desc="inner" class="android.widget.Button" package="com.example" '
        'clickable="true" enabled="true" bounds="[0,0][100,90]" />'
        "</node></node></hierarchy>"
    )
    screen = candidates_mod.build_screen(xml_text)
    assert names(screen) == ["inner"]


def test_fingerprint_covers_nodes_the_table_never_shows():
    raw = fixture_text("delivery_home.xml")
    root = candidates_mod.parse_hierarchy(raw)
    before = candidates_mod.fingerprint(root)
    changed = raw.replace('bounds="[0,0][1216,2640]"', 'bounds="[0,0][1216,2641]"', 1)
    after = candidates_mod.fingerprint(candidates_mod.parse_hierarchy(changed))
    assert before != after
    assert before.startswith("sha1:")


def test_cjk_costs_about_one_token_per_character():
    assert candidates_mod.estimate_tokens(L.SEARCH) == 2
    assert candidates_mod.estimate_tokens("abcd") == 1


def test_editable_elements_are_marked_and_listed():
    screen = build("settings_home.xml")
    editable = candidates_mod.editable_indices(screen.elements)
    assert editable
    for index in editable:
        assert screen.element_by_index(index).editable


def test_scroll_hint_is_off_when_nothing_scrolls():
    screen = build("popup_app_clone_chooser.xml")
    assert screen.scroll == {"can_down": False, "can_up": False}


def test_scroll_hint_respects_the_caller_history():
    screen = build("settings_home.xml", scroll={"can_down": False, "can_up": True})
    assert screen.scroll["can_down"] is False
    assert screen.scroll["can_up"] is True


@pytest.mark.parametrize(
    "name",
    [
        "delivery_home.xml",
        "delivery_search.xml",
        "delivery_food_list.xml",
        "settings_home.xml",
        "delivery_profile.xml",
        "popup_app_clone_chooser.xml",
        "popup_captcha_text.xml",
        "u2_delivery_home_with_systemui.xml",
        "u2_delivery_search_result.xml",
    ],
)
def test_every_fixture_parses_into_a_usable_screen(name):
    screen = build(name)
    assert screen.fingerprint.startswith("sha1:")
    assert [e.index for e in screen.elements] == list(range(len(screen.elements)))
    for element in screen.elements:
        assert element.kind in (KIND_INTERACTIVE, KIND_TEXT_LEAF, KIND_CONTAINER)
        assert element.name


def test_fingerprint_ignores_other_packages_windows():
    """A status bar whose clock shows seconds must not make a screen differ
    from itself: every action would be refused as stale_screen."""
    xml_text = (
        '<hierarchy rotation="0">'
        '<node class="android.widget.FrameLayout" package="com.example.app" '
        'bounds="[0,0][100,200]">'
        '<node class="android.widget.Button" text="Go" package="com.example.app" '
        'clickable="true" enabled="true" bounds="[0,0][100,90]" />'
        "</node>"
        '<node class="android.widget.FrameLayout" package="com.android.systemui" '
        'bounds="[0,0][100,20]">'
        '<node class="android.widget.TextView" text="19:06:16" '
        'package="com.android.systemui" bounds="[0,0][40,20]" />'
        "</node></hierarchy>"
    )
    screen = candidates_mod.build_screen(xml_text)
    ticked = candidates_mod.build_screen(xml_text.replace("19:06:16", "19:06:17"))
    assert screen.app["package"] == "com.example.app"
    assert screen.fingerprint == ticked.fingerprint

    moved = candidates_mod.build_screen(xml_text.replace('text="Go"', 'text="Stop"'))
    assert screen.fingerprint != moved.fingerprint, "the app's own change must still count"


def test_fingerprint_separates_two_packages_showing_nothing_of_each_other():
    """Two apps whose trees happen to hash the same still differ: the sorted
    set of window packages goes into the digest."""
    bare = '<hierarchy rotation="0"><node class="X" package="%s" bounds="[0,0][10,10]" /></hierarchy>'
    one = candidates_mod.parse_hierarchy(bare % "com.one")
    two = candidates_mod.parse_hierarchy(bare % "com.two")
    assert candidates_mod.fingerprint(one) != candidates_mod.fingerprint(two)


def window(package: str, bounds: str, *, inner: str = "") -> str:
    return (
        f'<node class="android.widget.FrameLayout" package="{package}" '
        f'bounds="{bounds}">{inner}</node>'
    )


APP_WINDOW = window(
    "com.example.app",
    "[0,0][1000,2000]",
    inner=(
        '<node class="android.widget.Button" text="Go" package="com.example.app" '
        'clickable="true" enabled="true" bounds="[0,100][1000,300]" />'
    ),
)
GESTURE_BAR = window(
    "com.vendor.gesturebar",
    "[0,1960][1000,2000]",
    inner='<node class="android.view.View" package="com.vendor.gesturebar" bounds="[0,1960][1000,2000]" />',
)
PERMISSION_DIALOG = window(
    "com.android.permissioncontroller",
    "[100,700][900,1300]",
    inner=(
        '<node class="android.widget.Button" text="Allow" '
        'package="com.android.permissioncontroller" clickable="true" enabled="true" '
        'bounds="[500,1200][880,1280]" />'
    ),
)
CHOOSER_DIALOG = window(
    "com.vendor.chooser",
    "[100,700][900,1300]",
    inner=(
        '<node class="android.widget.Button" text="Open once" '
        'package="com.vendor.chooser" clickable="true" enabled="true" '
        'bounds="[140,1180][500,1260]" />'
        '<node class="android.widget.Button" text="Always open" '
        'package="com.vendor.chooser" clickable="true" enabled="true" '
        'bounds="[520,1180][880,1260]" />'
    ),
)
KEYBOARD = window(
    "com.example.inputmethod.pinyin",
    "[0,1200][1000,1960]",
    inner=(
        '<node class="android.widget.Button" text="A" package="com.example.inputmethod.pinyin" '
        'clickable="true" enabled="true" bounds="[0,1300][100,1400]" />'
    ),
)


def tree(*windows: str) -> str:
    return '<hierarchy rotation="0">' + "".join(windows) + "</hierarchy>"


def test_a_foreign_dialog_over_the_app_changes_the_fingerprint():
    """The bug this replaces: a permission dialog drawn by another package,
    covering only part of the screen, left the fingerprint untouched, so the
    freshness check said nothing had moved and the tap went to the app
    underneath."""
    plain = candidates_mod.build_screen(tree(APP_WINDOW))
    covered = candidates_mod.build_screen(tree(APP_WINDOW, PERMISSION_DIALOG))
    assert plain.fingerprint != covered.fingerprint
    # The app window is still the biggest one, so it is still the app in front;
    # the dialog's button joins the table rather than replacing it.
    assert covered.app["package"] == "com.example.app"
    assert "Allow" in names(covered)


def test_decorative_windows_are_the_only_ones_skipped():
    root = candidates_mod.parse_hierarchy(
        tree(APP_WINDOW, GESTURE_BAR, KEYBOARD, PERMISSION_DIALOG)
    )
    decorative = candidates_mod.decorative_windows(root)
    skipped = {w.package for w in root.children if id(w) in decorative}
    assert skipped == {"com.vendor.gesturebar", "com.example.inputmethod.pinyin"}


def test_an_input_method_named_by_the_phone_is_decorative_too():
    """A keyboard whose package name gives nothing away is still recognised,
    as long as the adapter could read the phone's default input method."""
    odd_keyboard = window(
        "com.vendor.typing",
        "[0,1200][1000,1960]",
        inner=(
            '<node class="android.widget.Button" text="A" package="com.vendor.typing" '
            'clickable="true" enabled="true" bounds="[0,1300][100,1400]" />'
        ),
    )
    root = candidates_mod.parse_hierarchy(tree(APP_WINDOW, odd_keyboard))
    assert candidates_mod.decorative_windows(root) == set()
    named = candidates_mod.decorative_windows(root, ime_package="com.vendor.typing")
    assert len(named) == 1
    assert candidates_mod.fingerprint(root) != candidates_mod.fingerprint(
        root, ime_package="com.vendor.typing"
    )


def test_the_recorded_status_bar_and_gesture_bar_do_not_move_the_fingerprint():
    """The round-3 uiautomator2 dump carries three windows: the app, the system
    UI status bar and the vendor gesture bar. A ticking clock in either must
    leave the digest alone, or every action is refused as stale_screen."""
    raw = fixture_text("u2_delivery_home_with_systemui.xml")
    root = candidates_mod.parse_hierarchy(raw)
    assert len({child.package for child in root.children}) == 3

    decorative = candidates_mod.decorative_windows(root)
    kept = [child for child in root.children if id(child) not in decorative]
    assert [c.package for c in kept] == [candidates_mod.infer_current_package(root)], (
        "only the app's own window survives; the status bar is named, the "
        "gesture bar is caught as an edge strip with nothing on it"
    )

    clock = next(
        n.text
        for n in root.iter_descendants()
        if n.package == "com.android.systemui" and ":" in n.text
    )
    ticked = candidates_mod.build_screen(raw.replace(clock, "23:59", 1))
    assert candidates_mod.build_screen(raw).fingerprint == ticked.fingerprint


def test_a_dialog_from_another_package_is_tappable_and_ranks_first():
    """The bug this closes: the chooser was drawn by another package, the table
    was filtered down to the app in front, and its two buttons were invisible to
    the model. They now head the table, because the dialog is the window drawn
    last."""
    plain = candidates_mod.build_screen(tree(APP_WINDOW))
    covered = candidates_mod.build_screen(tree(APP_WINDOW, CHOOSER_DIALOG))

    assert covered.app["package"] == "com.example.app", "the app is still the biggest window"
    assert covered.app["windows"] == ["com.example.app", "com.vendor.chooser"]
    assert plain.app["windows"] == ["com.example.app"]
    assert plain.fingerprint != covered.fingerprint

    assert names(covered)[:2] == ["Open once", "Always open"]
    assert "Go" in names(covered), "the app underneath is still listed, just lower"

    rows = candidates_mod.screen_state(covered)["elements"]
    assert [row["window"] for row in rows[:2]] == ["vendor.chooser", "vendor.chooser"]
    assert rows[-1]["window"] == "example.app"


def test_the_budget_eats_the_app_before_the_dialog_on_top_of_it():
    """Ranking exists for truncation: whatever is dropped comes off the bottom,
    and the bottom is the window furthest back."""
    wide_app = window(
        "com.example.app",
        "[0,0][1000,2000]",
        inner="".join(
            f'<node class="android.widget.Button" text="row {n}" package="com.example.app" '
            f'clickable="true" enabled="true" bounds="[0,{n * 20}][1000,{n * 20 + 18}]" />'
            for n in range(40)
        ),
    )
    screen = candidates_mod.build_screen(tree(wide_app, CHOOSER_DIALOG), token_budget=120)
    assert screen.truncated is not None
    assert screen.truncated["shown"] < screen.truncated["total"]
    assert names(screen)[:2] == ["Open once", "Always open"]


def test_the_app_clone_chooser_stays_in_the_table_under_the_app_it_covers():
    """A recorded vendor chooser: the phone reports the app being launched as
    the foreground package while the sheet belongs to the phone maker's own
    package. Filtering the table down to the reported package emptied it out."""
    raw = fixture_text("popup_app_clone_chooser.xml")
    sheet_package = candidates_mod.parse_hierarchy(raw).children[0].package
    assert sheet_package and sheet_package != "com.example.delivery"

    screen = candidates_mod.build_screen(raw, package="com.example.delivery")
    assert screen.app["package"] == "com.example.delivery"
    assert screen.app["windows"] == [sheet_package]
    kept = {e.id for e in screen.elements if e.id}
    assert {"main", "clone"} <= kept, "both chooser entries have to be tappable"
