from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from harbor_hf_agents.support import control_client


class _Response:
    def __init__(self, value: dict[str, object]) -> None:
        self._payload = json.dumps(value).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._payload if size < 0 else self._payload[:size]


def test_retries_transient_control_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: list[object] = [
        HTTPError(
            "https://control.invalid/api/v1/runs/run-1",
            503,
            "unavailable",
            {},
            io.BytesIO(b"temporarily unavailable"),
        ),
        _Response({"status": "ok"}),
    ]
    requests: list[Request] = []
    sleeps: list[float] = []

    def urlopen(request: Request, **_kwargs: object) -> object:
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(control_client.time, "sleep", sleeps.append)
    client = control_client.ControlClient(
        control_url="https://control.invalid",
        run_id="run-1",
        capability="capability",
        retry_timeout_seconds=15,
    )
    client._opener = SimpleNamespace(open=urlopen)

    assert client.request_sync(
        "GET",
        "/api/v1/runs/run-1",
        idempotency_key="read-run",
    ) == {"status": "ok"}
    assert sleeps == [1.0]
    assert requests[-1].get_header("X-harbor-hf-worker-capability") == "capability"
    assert requests[-1].get_header("Authorization") is None


def test_retries_until_the_configured_control_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: list[object] = [
        HTTPError(
            "https://control.invalid/api/v1/runs/run-1",
            503,
            "unavailable",
            {},
            io.BytesIO(b"control runtime is initializing"),
        )
        for _ in range(5)
    ]
    responses.append(_Response({"status": "ok"}))
    sleeps: list[float] = []

    def urlopen(_request: Request, **_kwargs: object) -> object:
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(control_client.time, "sleep", sleeps.append)
    client = control_client.ControlClient(
        control_url="https://control.invalid",
        run_id="run-1",
        capability="capability",
        retry_timeout_seconds=31,
    )
    client._opener = SimpleNamespace(open=urlopen)

    assert client.request_sync(
        "POST",
        "/api/v1/runs/run-1/prepared-job",
        idempotency_key="prepare-task",
        body={"phase": "trial"},
    ) == {"status": "ok"}
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]


@pytest.mark.parametrize(
    "location",
    [
        "https://other.invalid/api/v1/runs/run-1",
        "http://control.invalid/api/v1/runs/run-1",
    ],
)
def test_redirect_handler_rejects_cross_origin_and_downgrade_redirects(
    location: str,
) -> None:
    request = Request(
        "https://control.invalid/api/v1/runs/run-1",
        headers={"X-Harbor-HF-Worker-Capability": "capability"},
    )

    redirected = control_client._NoRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {"Location": location},
        location,
    )

    assert redirected is None
    assert request.get_header("X-harbor-hf-worker-capability") == "capability"
    assert request.get_header("Authorization") is None


@pytest.mark.parametrize(
    "url",
    [
        "http://control.invalid",
        "https://control.invalid/path",
        "https://user@control.invalid",
    ],
)
def test_rejects_non_origin_control_urls(url: str) -> None:
    with pytest.raises(control_client.ControlClientError, match="HTTPS origin"):
        control_client.ControlClient(
            control_url=url,
            run_id="run-1",
            capability="capability",
            retry_timeout_seconds=15,
        )


def test_digest_helpers_use_prefixed_sha256(tmp_path: Path) -> None:
    path = tmp_path / "value.txt"
    path.write_bytes(b"value")
    expected = "sha256:cd42404d52ad55ccfa9aca4adc828aa5800ad9d385a0671fbcbf724118320619"

    assert control_client.digest_bytes(b"value") == expected
    assert control_client.digest_file(path) == expected
    assert control_client.digest_json({"value": 1}).startswith("sha256:")
