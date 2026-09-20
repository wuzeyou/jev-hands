"""Turn a raw UI hierarchy into the candidate table the model sees.

This is the layer the first two validation rounds hurt the most on, so the rules
below are written out one by one:

1. Interactive nodes come first (clickable / long-clickable / checkable /
   scrollable / editable).
2. A text leaf is kept only when no ancestor is tappable. A scrollable
   container ancestor does not count, otherwise a page wrapped in one ScrollView
   would collapse to its bottom tabs.
3. Zero-area, off-screen, disabled and ``visible-to-user="false"`` nodes are
   dropped.
4. Container rule: when an interactive node has interactive descendants, keep the
   ancestor as well if those descendants cover less than half of its area. The
   whole search bar used to disappear because one small button inside it was
   clickable too.
5. Names come from content-desc, then text, then up to two levels of child text.
6. Two elements with the same name on one screen get a ``#<short-id>`` suffix.
7. Candidates come from every top-level window that is not decorative, whatever
   package drew it. A permission dialog or a vendor chooser is something the
   caller has to be able to tap, and it is routinely drawn by a package other
   than the app in front. See `decorative_windows` for the three kinds that are
   dropped and why.
8. Windows are ranked topmost first: the last one in the dump goes to the top of
   the table, the one furthest back to the bottom. Within a window the nodes
   keep document order. The budget drops from the bottom, so an overlay's
   buttons survive a truncation that eats into the app behind it.
9. Each row carries the package of the window it came from, shortened to its
   last two dotted segments - but only once more than one package draws a
   window, since on the common single-window screen the column would repeat the
   same value on every row for nothing.
10. The table is capped by a token budget, not by a row count.
11. The fingerprint is taken over the full unfiltered tree of every top-level
    window except the decorative ones, plus the sorted set of those windows'
    packages.
12. A dump that produces no rows at all is classified rather than handed over
    blank: `hidden_tree` when the window in front came back with no content in
    it - a secure window, or an app that hides its tree - and `empty_tree`
    otherwise. See `unreadable_reason`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import (
    KIND_CONTAINER,
    KIND_INTERACTIVE,
    KIND_TEXT_LEAF,
    OBSERVE_EMPTY_TREE,
    OBSERVE_FAILED_HINTS,
    OBSERVE_FAILED_SUMMARIES,
    OBSERVE_HIDDEN_TREE,
    Element,
    Screen,
    new_capture_id,
    now,
)

NO_LABEL = "(no label)"
MAX_NAME_CHARS = 40
MAX_NAME_SEGMENTS = 3
CONTAINER_COVERAGE = 0.5
# How much of a window's package goes on a row. Two dotted segments tell
# `android.permissioncontroller` from `example.delivery` at a quarter of the cost
# of the full name.
WINDOW_NAME_SEGMENTS = 2

# The one package that is decorative by name on every Android build.
SYSTEM_UI_PACKAGE = "com.android.systemui"
# Dot-separated segments that mark an input-method package. Matched as whole
# segments, never as substrings, so `com.example.time` is not an IME.
IME_PACKAGE_SEGMENTS = ("inputmethod", "inputmethods", "ime", "latinime", "keyboard")
# A window counts as an edge strip when its short side is at most this much of
# the screen. On the recorded uiautomator2 dump the status bar comes to 4.9%
# and the gesture bar to 2.4%, so there is room to spare either way.
EDGE_STRIP_MAX_FRACTION = 0.1

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_CJK_RANGES = (
    (0x2E80, 0x9FFF),
    (0xA000, 0xA4CF),
    (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF),
    (0xFE30, 0xFE4F),
    (0xFF00, 0xFFEF),
    (0x20000, 0x2FA1F),
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """Rough token count. CJK counts one token per character, everything else
    about four characters per token. Good enough to keep a budget."""
    cjk = 0
    other = 0
    for char in text:
        if _is_cjk(char):
            cjk += 1
        else:
            other += 1
    return cjk + int(math.ceil(other / 4.0))


def _parse_bounds(raw: Optional[str]) -> Optional[Tuple[int, int, int, int]]:
    if not raw:
        return None
    match = _BOUNDS_RE.search(raw)
    if not match:
        return None
    x1, y1, x2, y2 = (int(g) for g in match.groups())
    return (x1, y1, x2, y2)


def _flag(node: "Node", name: str) -> bool:
    return node.attrib.get(name) == "true"


@dataclass
class Node:
    """One node of the parsed hierarchy, with a parent pointer."""

    attrib: Dict[str, str]
    children: List["Node"] = field(default_factory=list)
    parent: Optional["Node"] = None
    depth: int = 0

    # -- convenience accessors -------------------------------------------------
    @property
    def cls(self) -> str:
        return self.attrib.get("class", "") or ""

    @property
    def package(self) -> str:
        return self.attrib.get("package", "") or ""

    @property
    def text(self) -> str:
        return (self.attrib.get("text") or "").strip()

    @property
    def desc(self) -> str:
        return (self.attrib.get("content-desc") or "").strip()

    @property
    def resource_id(self) -> str:
        return (self.attrib.get("resource-id") or "").strip()

    @property
    def bounds(self) -> Optional[Tuple[int, int, int, int]]:
        return _parse_bounds(self.attrib.get("bounds"))

    @property
    def area(self) -> int:
        box = self.bounds
        if not box:
            return 0
        return max(0, box[2] - box[0]) * max(0, box[3] - box[1])

    @property
    def enabled(self) -> bool:
        return self.attrib.get("enabled", "true") != "false"

    @property
    def visible(self) -> Optional[bool]:
        raw = self.attrib.get("visible-to-user")
        if raw is None:
            return None
        return raw == "true"

    @property
    def editable(self) -> bool:
        if self.attrib.get("editable") == "true":
            return True
        lowered = self.cls.lower()
        return lowered.endswith("edittext") or "edittext" in lowered or "contenteditable" in lowered

    @property
    def interactive(self) -> bool:
        return (
            _flag(self, "clickable")
            or _flag(self, "long-clickable")
            or _flag(self, "checkable")
            or _flag(self, "scrollable")
            or self.editable
        )

    @property
    def scrollable(self) -> bool:
        return _flag(self, "scrollable")

    def iter_descendants(self):
        for child in self.children:
            yield child
            yield from child.iter_descendants()


def parse_hierarchy(xml_text: str) -> Node:
    """Parse an adb or uiautomator2 dump into a Node tree."""
    try:
        root_el = ET.fromstring(xml_text)
    except ET.ParseError as exc:  # pragma: no cover - defensive
        raise ValueError(f"UI hierarchy is not valid XML: {exc}") from exc

    def build(element: ET.Element, parent: Optional[Node], depth: int) -> Node:
        node = Node(attrib=dict(element.attrib), parent=parent, depth=depth)
        for child_el in list(element):
            node.children.append(build(child_el, node, depth + 1))
        return node

    return build(root_el, None, 0)


def _is_ime_package(package: str, ime_package: Optional[str]) -> bool:
    if not package:
        return False
    if ime_package and package == ime_package:
        return True
    segments = set(package.split("."))
    return any(segment in segments for segment in IME_PACKAGE_SEGMENTS)


def _is_edge_strip(window: Node, size: Tuple[int, int]) -> bool:
    """A sliver hugging a screen edge with nothing on it to interact with.

    Written geometrically rather than by package name, so that a phone maker's
    own gesture bar is caught without anyone having to know that maker or keep
    a list of them up to date.
    """
    box = window.bounds
    width, height = size
    if not box or not width or not height:
        return False
    x1, y1, x2, y2 = box
    strip_width, strip_height = x2 - x1, y2 - y1
    if strip_width <= 0 or strip_height <= 0:
        return False
    horizontal = (
        strip_width >= width
        and strip_height <= EDGE_STRIP_MAX_FRACTION * height
        and (y1 <= 0 or y2 >= height)
    )
    vertical = (
        strip_height >= height
        and strip_width <= EDGE_STRIP_MAX_FRACTION * width
        and (x1 <= 0 or x2 >= width)
    )
    if not (horizontal or vertical):
        return False
    if window.interactive:
        return False
    return not any(node.interactive for node in window.iter_descendants())


def decorative_windows(root: Node, *, ime_package: Optional[str] = None) -> set:
    """The ids of the top-level windows that say nothing about the screen.

    Exactly three kinds, and nothing else:

    1. the system UI package, ``com.android.systemui``. Its clock ticks and its
       network indicator blinks, several times a second on some phones;
    2. input-method windows. Identified by their package matching the phone's
       current default input method when the adapter could look that up
       cheaply, and otherwise by the package carrying a whole dot-separated
       segment out of `IME_PACKAGE_SEGMENTS`;
    3. edge strips: a window that spans the full width or the full height, sits
       against a screen edge, is at most `EDGE_STRIP_MAX_FRACTION` of the screen
       across its short side, and contains nothing interactive. That is the
       gesture bar, a vendor notch overlay, and nothing a caller can act on.

    Every other window is part of the screen, however small: a system
    permission dialog or a vendor prompt covering a third of the display is the
    thing the caller most needs to know about.
    """
    size = _screen_size(root)
    decorative = set()
    for window in root.children:
        package = window.package
        if (
            package == SYSTEM_UI_PACKAGE
            or _is_ime_package(package, ime_package)
            or _is_edge_strip(window, size)
        ):
            decorative.add(id(window))
    return decorative


def non_decorative_windows(root: Node, *, ime_package: Optional[str] = None) -> List[Node]:
    """The top-level windows that make up the screen, in document order.

    One place decides what a window is worth looking at, and the fingerprint,
    the foreground app and the candidate table all read it from here. When they
    disagreed, the fingerprint saw a dialog the table did not, and the loop
    refused every action on a screen whose buttons it had never listed.
    """
    decorative = decorative_windows(root, ime_package=ime_package)
    return [window for window in root.children if id(window) not in decorative]


def hidden_window(window: Node) -> bool:
    """The shape a window hidden from accessibility leaves in the dump.

    Android keeps secure windows out of the accessibility tree - biometric
    prompts, the system password and PIN entry, payment keyboards - and an app
    is free to switch its own tree off as well. Both come back the same way:
    one root node with no class, no size and no children, where a real window
    would have a layout under it.
    """
    return not window.cls and not window.children and window.area == 0


def unreadable_reason(
    root: Node,
    *,
    package: Optional[str] = None,
    ime_package: Optional[str] = None,
) -> str:
    """Why a dump yielded no candidates: `hidden_tree` or `empty_tree`.

    Only asked when the table came out empty. `hidden_tree` needs a known
    foreground package and a window in front carrying the hidden shape; a
    decorative window alongside it - the status bar is usually still there -
    does not make the screen any more readable, so it does not change the
    answer. Everything else is `empty_tree`: the dump held nothing at all.
    """
    windows = non_decorative_windows(root, ime_package=ime_package) or list(root.children)
    front = [w for w in windows if package and w.package == package] or windows
    if package and front and all(hidden_window(w) for w in front):
        return OBSERVE_HIDDEN_TREE
    return OBSERVE_EMPTY_TREE


def window_packages(windows: Sequence[Node]) -> List[str]:
    """The packages drawing those windows, in document order, without repeats."""
    packages: List[str] = []
    for window in windows:
        if window.package and window.package not in packages:
            packages.append(window.package)
    return packages


def _short_package(package: str) -> str:
    if not package:
        return ""
    return ".".join(package.split(".")[-WINDOW_NAME_SEGMENTS:])


def fingerprint(root: Node, *, ime_package: Optional[str] = None) -> str:
    """sha1 over every non-decorative window, unfiltered, plus their packages.

    Unfiltered matters: a long list scrolled by one row must not look unchanged
    just because the visible slice was truncated the same way.

    Skipping only the decorative windows matters just as much, in both
    directions. Counting the status bar would make a screen differ from itself
    every second, so every action would be refused as `stale_screen`. Counting
    only the front app's own window - which is what this used to do - hides a
    partial overlay drawn by another package, and then the freshness check says
    the screen has not moved while a permission dialog is sitting on top of it.

    The sorted set of the surviving windows' packages goes in as well, so a new
    window is a change even before anything inside it is read.
    """
    windows = non_decorative_windows(root, ime_package=ime_package)

    digest = hashlib.sha1()
    packages = sorted({w.package for w in windows if w.package})
    digest.update(("windows:" + ",".join(packages)).encode("utf-8"))
    digest.update(b"\n")
    for window in windows:
        for node in (window, *window.iter_descendants()):
            digest.update(
                "|".join(
                    (
                        node.cls,
                        node.resource_id,
                        node.text,
                        node.desc,
                        node.attrib.get("bounds", ""),
                    )
                ).encode("utf-8")
            )
            digest.update(b"\n")
    return "sha1:" + digest.hexdigest()


def infer_current_package(root: Node, *, ime_package: Optional[str] = None) -> Optional[str]:
    """Pick the package that owns the biggest non-decorative top level window.

    Skipping the decorative ones keeps a full-height input method or a pulled
    down notification shade from being taken for the app in front. When every
    window is decorative the biggest one is used anyway, so the candidate table
    is never emptied out by this rule alone.
    """

    def largest(windows: List[Node]) -> Optional[str]:
        best: Optional[str] = None
        best_area = -1
        for child in windows:
            if not child.package:
                continue
            if child.area > best_area:
                best_area = child.area
                best = child.package
        return best

    return largest(non_decorative_windows(root, ime_package=ime_package)) or largest(
        root.children
    )


def _screen_size(root: Node) -> Tuple[int, int]:
    """Screen size is the extent of the top level windows.

    Taking the maximum over every node would be wrong: a list item inside a
    scrollable container can legitimately sit below the bottom of the screen,
    and using it would make the off-screen check useless.
    """
    width = height = 0
    for child in root.children:
        box = child.bounds
        if box:
            width = max(width, box[2])
            height = max(height, box[3])
    if width and height:
        return (width, height)
    for node in root.iter_descendants():
        box = node.bounds
        if box:
            width = max(width, box[2])
            height = max(height, box[3])
    return (width, height)


def _name_of(node: Node) -> str:
    """content-desc -> text -> up to two levels of child text -> (no label)."""
    if node.desc:
        return node.desc[:MAX_NAME_CHARS]
    if node.text:
        return node.text[:MAX_NAME_CHARS]

    segments: List[str] = []

    def add(value: str) -> None:
        """Keep the longest wording of each thing.

        Android UIs routinely carry the same label twice, once abbreviated and
        once in full ("WLAN" next to "WLAN, connected"). Repeating both wastes
        the name budget and reads as two separate elements to the model.
        """
        if any(value in kept for kept in segments):
            return
        for position in range(len(segments) - 1, -1, -1):
            if segments[position] in value:
                segments.pop(position)
        segments.append(value)

    def collect(current: Node, level: int) -> None:
        if level > 2 or len(segments) >= MAX_NAME_SEGMENTS:
            return
        for child in current.children:
            for value in (child.desc, child.text):
                if value:
                    add(value)
                    if len(segments) >= MAX_NAME_SEGMENTS:
                        return
            collect(child, level + 1)
            if len(segments) >= MAX_NAME_SEGMENTS:
                return

    collect(node, 1)
    if segments:
        return " ".join(segments)[:MAX_NAME_CHARS]
    return NO_LABEL


def _short_id(node: Node) -> str:
    if node.resource_id:
        tail = node.resource_id.split("/")[-1]
        if tail:
            return tail
    seed = f"{node.cls}|{node.attrib.get('bounds', '')}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:4]


def _kind_of(node: Node) -> str:
    if node.scrollable and not (
        _flag(node, "clickable")
        or _flag(node, "long-clickable")
        or _flag(node, "checkable")
        or node.editable
    ):
        return KIND_CONTAINER
    if node.interactive:
        return KIND_INTERACTIVE
    return KIND_TEXT_LEAF


def _eligible(node: Node, size: Tuple[int, int]) -> bool:
    if not node.enabled:
        return False
    if node.visible is False:
        return False
    box = node.bounds
    if not box:
        return False
    x1, y1, x2, y2 = box
    if x2 - x1 <= 0 or y2 - y1 <= 0:
        return False
    width, height = size
    if width and height:
        if x2 <= 0 or y2 <= 0 or x1 >= width or y1 >= height:
            return False
    return True


def _nearest_interactive_descendants(node: Node, eligible_interactive: set) -> List[Node]:
    """Interactive descendants with no other interactive node in between."""
    found: List[Node] = []

    def walk(current: Node) -> None:
        for child in current.children:
            if id(child) in eligible_interactive:
                found.append(child)
            else:
                walk(child)

    walk(node)
    return found


def _row_text(element: Element, *, with_window: bool = False) -> str:
    """The row exactly as it is serialised into the request state, so the budget
    is measured against what actually costs tokens."""
    return json.dumps(
        element.to_state_row(with_window=with_window), ensure_ascii=False, separators=(",", ":")
    )


def _apply_budget(
    elements: List[Element], budget: int, *, with_window: bool = False
) -> Tuple[List[Element], Optional[Dict[str, int]]]:
    """Drop rows until the table fits the token budget.

    Drop order: unnamed rows, then scrollable containers, then text leaves, then
    whatever is left, always from the end of the table. The table is ordered
    topmost window first, so what goes first is what sits furthest behind.
    """
    total = len(elements)
    kept = list(elements)

    def size() -> int:
        return sum(estimate_tokens(_row_text(e, with_window=with_window)) for e in kept)

    def drop_one() -> bool:
        for predicate in (
            lambda e: e.name == NO_LABEL,
            lambda e: e.kind == KIND_CONTAINER,
            lambda e: e.kind == KIND_TEXT_LEAF,
            lambda e: True,
        ):
            for position in range(len(kept) - 1, -1, -1):
                if predicate(kept[position]):
                    kept.pop(position)
                    return True
        return False

    while budget > 0 and size() > budget and len(kept) > 1:
        if not drop_one():
            break

    if len(kept) == total:
        return kept, None
    for position, element in enumerate(kept):
        element.index = position
    return kept, {"shown": len(kept), "total": total}


def build_elements(
    root: Node,
    *,
    windows: Optional[Sequence[Node]] = None,
    ime_package: Optional[str] = None,
    token_budget: int = 1500,
) -> Tuple[List[Element], Optional[Dict[str, int]], Tuple[int, int], bool]:
    """Run the whole filter chain over every non-decorative window.

    `windows` is the window list the caller already worked out; without it the
    same rule is applied here, so the two can never disagree.

    Returns (elements, truncated, screen size, whether the tree has a scrollable
    container at all). The scrollable flag is taken before the container rule
    runs, because a scrollable wrapper is often dropped from the table itself.
    """
    size = _screen_size(root)
    if windows is None:
        windows = non_decorative_windows(root, ime_package=ime_package)
    with_window = len(window_packages(windows)) > 1

    # Topmost window first: the last one in the dump is the one drawn last, so
    # its rows head the table and outlive a truncation.
    nodes: List[Node] = []
    window_of: Dict[int, Node] = {}
    for window in reversed(list(windows)):
        for node in (window, *window.iter_descendants()):
            nodes.append(node)
            window_of[id(node)] = window
    node_ids = {id(n) for n in nodes}

    eligible = [n for n in nodes if _eligible(n, size)]
    interactive_ids = {id(n) for n in eligible if n.interactive}
    # A pure scrollable container is not something you tap, so it must not hide
    # the text inside it from the candidate table.
    tappable_ids = {id(n) for n in eligible if _kind_of(n) == KIND_INTERACTIVE}
    has_scrollable = any(n.scrollable for n in eligible)

    def has_tappable_ancestor(node: Node) -> bool:
        parent = node.parent
        while parent is not None:
            if id(parent) in tappable_ids and id(parent) in node_ids:
                return True
            parent = parent.parent
        return False

    kept: List[Node] = []
    for node in eligible:
        if id(node) in interactive_ids:
            descendants = _nearest_interactive_descendants(node, interactive_ids)
            if descendants and node.area > 0:
                covered = sum(d.area for d in descendants)
                if covered >= CONTAINER_COVERAGE * node.area:
                    continue
            kept.append(node)
            continue
        # Not interactive: a text leaf may still be worth showing.
        if node.children:
            continue
        if not (node.text or node.desc):
            continue
        if has_tappable_ancestor(node):
            continue
        kept.append(node)

    # Drop duplicates that describe the exact same thing.
    elements: List[Element] = []
    seen_keys = set()
    raw_names: List[str] = []
    raw_nodes: List[Node] = []
    for node in kept:
        key = (_name_of(node), node.resource_id, node.attrib.get("bounds", ""))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        raw_names.append(key[0])
        raw_nodes.append(node)

    duplicated = {name for name in raw_names if raw_names.count(name) > 1}
    used_names: set = set()
    for position, node in enumerate(raw_nodes):
        name = raw_names[position]
        if name in duplicated:
            candidate = f"{name}#{_short_id(node)}"
            if candidate in used_names:
                seed = f"{node.cls}|{node.attrib.get('bounds', '')}"
                candidate = f"{name}#{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:4]}"
            name = candidate
        used_names.add(name)

        box = node.bounds or (0, 0, 0, 0)
        checked: Optional[bool] = None
        if _flag(node, "checkable"):
            checked = _flag(node, "checked")
        elements.append(
            Element(
                index=position,
                name=name,
                id=(node.resource_id.split("/")[-1] if node.resource_id else None),
                type=node.cls.split(".")[-1] if node.cls else "",
                at=((box[0] + box[2]) // 2, (box[1] + box[3]) // 2),
                bounds=box,
                editable=node.editable,
                scrollable=node.scrollable,
                focused=_flag(node, "focused"),
                checked=checked,
                kind=_kind_of(node),
                window=_short_package(window_of[id(node)].package) or None,
            )
        )

    elements, truncated = _apply_budget(elements, token_budget, with_window=with_window)
    return elements, truncated, size, has_scrollable


def build_screen(
    xml_text: str,
    *,
    package: Optional[str] = None,
    activity: Optional[str] = None,
    token_budget: int = 1500,
    scroll: Optional[Dict[str, bool]] = None,
    capture_id: Optional[str] = None,
    ime_package: Optional[str] = None,
) -> Screen:
    """Parse a dump and produce a Screen.

    `ime_package` is the phone's current default input method, when the adapter
    could find it out cheaply. It only sharpens the decorative-window rule.
    """
    root = parse_hierarchy(xml_text)
    windows = non_decorative_windows(root, ime_package=ime_package)
    resolved_package = package or infer_current_package(root, ime_package=ime_package)
    elements, truncated, size, has_scrollable = build_elements(
        root, windows=windows, token_budget=token_budget
    )
    if scroll is None:
        scroll = {"can_down": has_scrollable, "can_up": False}
    else:
        scroll = {
            "can_down": bool(scroll.get("can_down", has_scrollable)) and has_scrollable,
            "can_up": bool(scroll.get("can_up", False)) and has_scrollable,
        }

    note = None
    unreadable = None
    if not elements:
        unreadable = unreadable_reason(
            root, package=resolved_package, ime_package=ime_package
        )
        note = (
            OBSERVE_FAILED_SUMMARIES[unreadable] + " " + OBSERVE_FAILED_HINTS[unreadable]
        )

    return Screen(
        capture_id=capture_id or new_capture_id(),
        captured_at=now(),
        app={
            "package": resolved_package,
            "activity": activity,
            "windows": window_packages(windows),
        },
        elements=elements,
        fingerprint=fingerprint(root, ime_package=ime_package),
        truncated=truncated,
        scroll=scroll,
        size=size if size != (0, 0) else None,
        note=note,
        unreadable=unreadable,
    )


def screen_state(screen: Screen) -> Dict[str, Any]:
    """The compact `screen` object that goes into the Jev request state.

    Rows name their window only when more than one package draws one. On the
    ordinary screen that is every row repeating the same value for nothing.
    """
    with_window = len(screen.app.get("windows") or []) > 1
    state: Dict[str, Any] = {
        "elements": [e.to_state_row(with_window=with_window) for e in screen.elements],
    }
    if screen.truncated:
        state["screen_truncated"] = (
            f"showing {screen.truncated['shown']} of {screen.truncated['total']}"
        )
    state["scroll_available"] = {
        "down": screen.scroll.get("can_down", False),
        "up": screen.scroll.get("can_up", False),
    }
    return state


def editable_indices(elements: Sequence[Element]) -> List[int]:
    return [e.index for e in elements if e.editable]
