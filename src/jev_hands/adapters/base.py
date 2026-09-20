"""The contract every platform adapter implements.

Three methods is the whole surface: read the screen, check whether a previous
reading is still current, and carry out one action.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol, runtime_checkable

from ..core.models import Screen


@runtime_checkable
class Adapter(Protocol):
    name: str

    def observe(
        self,
        *,
        token_budget: int = 1500,
        scroll: Optional[Dict[str, bool]] = None,
        screenshot: bool = False,
    ) -> Screen:
        """Read the current screen into a Screen."""

    def fresh(self, capture_id: str) -> bool:
        """True when the given observation still matches what is on screen."""

    def execute(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Carry out one action.

        ``action`` carries ``type`` plus whatever that type needs (``x``/``y``
        for a tap, ``text`` for typing). Returns a small record of what happened;
        raises a JevHandsError subclass when it cannot tell.
        """
