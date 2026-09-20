"""Thin HTTP client for the TypeSafe evaluation endpoint.

The API key only ever travels as the `Authorization` header. It is never
logged, never put in an error message and never returned to the caller.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from ..errors import JevApiError

LOG = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.typesafe.ai"
EVAL_PATH = "/v1/systemone"
DEFAULT_TIMEOUT = 20.0
MAX_ATTEMPTS = 3
RETRY_STATUS = (429, 503, 529)


@dataclass
class JevResponse:
    answers: Dict[str, Dict[str, Any]]
    model: Optional[str]
    usage: Dict[str, Any]
    latency_ms: int


class JevClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def evaluate(
        self,
        state: Any,
        questions: Dict[str, Dict[str, Any]],
        *,
        model: str = "jev-latest",
    ) -> JevResponse:
        payload = {"state": state, "model": model, "questions": questions}
        url = self._base_url + EVAL_PATH
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        last_error: Optional[str] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.monotonic()
            try:
                response = self._http().post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"network error talking to the Jev API: {type(exc).__name__}"
                LOG.warning("jev request attempt %d failed: %s", attempt, type(exc).__name__)
                self._backoff(attempt)
                continue

            latency_ms = int((time.monotonic() - started) * 1000)

            if response.status_code in RETRY_STATUS:
                last_error = f"Jev API returned HTTP {response.status_code}"
                LOG.warning("jev request attempt %d got HTTP %d", attempt, response.status_code)
                self._backoff(attempt)
                continue

            if response.status_code == 401:
                raise JevApiError(
                    "The TypeSafe API rejected the key (HTTP 401).",
                    code="api_key_invalid",
                    hint="Run /jev-hands:setup and store a valid key.",
                )

            if response.status_code >= 400:
                raise JevApiError(
                    f"The Jev API returned HTTP {response.status_code}.",
                    details={"status": response.status_code},
                )

            body = response.json()
            return JevResponse(
                answers=body.get("answers", {}) or {},
                model=body.get("model"),
                usage=body.get("usage", {}) or {},
                latency_ms=latency_ms,
            )

        raise JevApiError(
            last_error or "The Jev API did not answer after 3 attempts.",
            hint="Retry in a moment; check network access to api.typesafe.ai.",
        )

    @staticmethod
    def _backoff(attempt: int) -> None:
        delay = min(4.0, 0.5 * (2 ** (attempt - 1)))
        time.sleep(delay + random.uniform(0.0, 0.25))
