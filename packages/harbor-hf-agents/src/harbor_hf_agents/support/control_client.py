"""Capability-scoped client helpers for the Harbor-HF Run API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_DELAY_SECONDS = 30.0
_MAX_CONTROL_RESPONSE_BYTES = 16 * 1024 * 1024


class ControlClientError(RuntimeError):
    """Base error for control-service request failures."""


class ControlClientResponseError(ControlClientError):
    """Raised when the control service returns an invalid response."""


class ControlClientTransientError(ControlClientError):
    """Raised when bounded retries exhaust a transient remote failure."""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects so worker capabilities never leave the control origin."""

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class ControlClient:
    """Read and write one capability-scoped Run through the control API."""

    def __init__(
        self,
        *,
        control_url: str,
        run_id: str,
        capability: str,
        timeout_seconds: int = 30,
        retry_timeout_seconds: int,
    ) -> None:
        origin = _https_origin(control_url)
        if isinstance(retry_timeout_seconds, bool) or retry_timeout_seconds <= 0:
            raise ControlClientError("control retry timeout must be a positive integer")
        self.origin = origin
        self.run_id = run_id
        self.prefix = f"/api/v1/runs/{quote(run_id, safe='')}"
        self._capability = capability
        self._timeout_seconds = timeout_seconds
        self._retry_timeout_seconds = retry_timeout_seconds
        self._opener = build_opener(_NoRedirectHandler())

    @classmethod
    def from_environment(cls) -> ControlClient:
        """Build a client from the trusted worker process environment."""
        return cls(
            control_url=_required_environment("HARBOR_HF_CONTROL_URL"),
            run_id=_required_environment("HARBOR_HF_RUN_ID"),
            capability=_required_environment("HARBOR_HF_WORKER_CAPABILITY"),
            retry_timeout_seconds=_required_positive_integer_environment(
                "HARBOR_HF_CONTROL_RETRY_TIMEOUT_SECONDS"
            ),
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        idempotency_key: str,
        body: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Send a JSON request with a bounded transient retry budget."""
        return await asyncio.to_thread(
            self.request_sync,
            method,
            path,
            idempotency_key,
            body,
            timeout_seconds,
        )

    def request_sync(
        self,
        method: str,
        path: str,
        idempotency_key: str,
        body: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Send a blocking JSON request with bounded transient retries."""
        payload = canonical_json_bytes(body) if body is not None else None
        headers = {
            "X-Harbor-HF-Worker-Capability": self._capability,
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        retry_seconds_remaining = float(self._retry_timeout_seconds)
        retry_count = 0
        while True:
            request = Request(
                f"{self.origin}{path}",
                data=payload,
                headers=headers,
                method=method,
            )
            try:
                with self._opener.open(
                    request,
                    timeout=timeout_seconds or self._timeout_seconds,
                ) as response:
                    value = json.loads(_read_bounded(response))
                if not isinstance(value, dict):
                    raise ControlClientResponseError(
                        "control API response must be a JSON object"
                    )
                return value
            except HTTPError as error:
                detail_bytes = error.read(4097)
                detail = detail_bytes[:4096].decode("utf-8", errors="replace")
                if len(detail_bytes) > 4096:
                    detail += " [truncated]"
                retry_delay = _retry_delay(retry_count, retry_seconds_remaining)
                if error.code in _RETRYABLE_HTTP_CODES and retry_delay is not None:
                    time.sleep(retry_delay)
                    retry_seconds_remaining -= retry_delay
                    retry_count += 1
                    continue
                error_type = (
                    ControlClientTransientError
                    if error.code in _RETRYABLE_HTTP_CODES
                    else ControlClientError
                )
                raise error_type(
                    f"control API returned HTTP {error.code}: {detail}"
                ) from error
            except (TimeoutError, URLError) as error:
                retry_delay = _retry_delay(retry_count, retry_seconds_remaining)
                if retry_delay is not None:
                    time.sleep(retry_delay)
                    retry_seconds_remaining -= retry_delay
                    retry_count += 1
                    continue
                raise ControlClientTransientError(
                    "control API request failed"
                ) from error


def canonical_json_bytes(value: object) -> bytes:
    """Encode a value with the canonical JSON form used by Run records."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def digest_bytes(value: bytes) -> str:
    """Return the portable prefixed SHA-256 digest for bytes."""
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def digest_json(value: object) -> str:
    """Return the canonical SHA-256 digest for a JSON value."""
    return digest_bytes(canonical_json_bytes(value))


def digest_file(path: Path) -> str:
    """Return the portable prefixed SHA-256 digest for one file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def run_lock_profile(lock: dict[str, Any], kind: str) -> dict[str, Any]:
    """Return one validated profile specification from a Run lock."""
    profiles = lock["profiles"]
    if not isinstance(profiles, list):
        raise RuntimeError("run lock profiles must be a list")
    matches = [
        item for item in profiles if isinstance(item, dict) and item["kind"] == kind
    ]
    if len(matches) != 1 or not isinstance(matches[0]["spec"], dict):
        raise RuntimeError(f"run lock must contain one {kind} profile")
    return matches[0]["spec"]


def _read_bounded(response: Any) -> bytes:  # noqa: ANN401 -- urllib response protocol
    payload = response.read(_MAX_CONTROL_RESPONSE_BYTES + 1)
    if len(payload) > _MAX_CONTROL_RESPONSE_BYTES:
        raise ControlClientResponseError("control API response exceeds the size limit")
    return payload


def _required_environment(name: str) -> str:
    try:
        value = os.environ[name]
    except KeyError as error:
        raise ControlClientError(
            f"required control worker setting {name} is missing"
        ) from error
    if not value:
        raise ControlClientError(f"required control worker setting {name} is empty")
    return value


def _required_positive_integer_environment(name: str) -> int:
    """Return a required positive integer worker setting."""
    value = _required_environment(name)
    try:
        parsed = int(value)
    except ValueError as error:
        raise ControlClientError(
            f"required control worker setting {name} must be an integer"
        ) from error
    if parsed <= 0:
        raise ControlClientError(
            f"required control worker setting {name} must be positive"
        )
    return parsed


def _retry_delay(retry_count: int, seconds_remaining: float) -> float | None:
    """Return the next capped retry delay within the locked Job timeout."""
    delay = min(2.0 ** min(retry_count, 5), _MAX_RETRY_DELAY_SECONDS)
    return delay if delay <= seconds_remaining else None


def _https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ControlClientError("control URL must be an HTTPS origin")
    return f"{parsed.scheme}://{parsed.netloc}"
