"""Browser adapter over the Chrome DevTools Protocol.

Phase 2: reuse the whole core and implement the same three methods against a
Chrome the user already has open.
"""

from __future__ import annotations

from ..errors import JevHandsError


class BrowserAdapter:
    name = "browser"

    def __init__(self, *args, **kwargs):  # pragma: no cover - not implemented yet
        raise JevHandsError(
            "The browser adapter is not part of phase 1.",
            code="not_implemented",
            hint="Use the Android server for now.",
        )
