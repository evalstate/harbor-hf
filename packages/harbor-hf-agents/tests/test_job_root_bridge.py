from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from harbor_hf_agents.support import job_root_bridge


def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "HF_INFERENCE_TOKEN": "private-inference-token",
        "HF_TOKEN": "persistent-control-token",
        "HARBOR_HF_WORKER_CAPABILITY": "worker-capability",
        "HARBOR_HF_INFERENCE_UPSTREAM": "https://router.huggingface.co/v1",
        "HARBOR_HF_INFERENCE_ALLOWED_MODEL": "example/model",
        "HARBOR_HF_INFERENCE_API": "chat-completions",
        "HARBOR_HF_INFERENCE_MAX_REQUESTS": "8",
        "HARBOR_HF_INFERENCE_MAX_CONCURRENCY": "1",
        "HARBOR_HF_INFERENCE_TIMEOUT_SECONDS": "300",
        "HARBOR_HF_INFERENCE_MAX_OUTPUT_TOKENS": "1024",
    }.items():
        monkeypatch.setenv(key, value)


def test_starts_host_bridge_with_only_required_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token = tmp_path / "token"
    route = tmp_path / "route.json"
    handle = tmp_path / "handle.json"
    log = tmp_path / "bridge.log"
    monkeypatch.setattr(job_root_bridge, "_TOKEN_PATH", token)
    monkeypatch.setattr(job_root_bridge, "_ROUTE_PATH", route)
    monkeypatch.setattr(job_root_bridge, "_HANDLE_PATH", handle)
    monkeypatch.setattr(job_root_bridge, "_LOG_PATH", log)
    monkeypatch.setattr(job_root_bridge.os, "geteuid", lambda: 0)
    monkeypatch.setattr(job_root_bridge, "_process_start_time", lambda _pid: 123)
    monkeypatch.setattr(job_root_bridge, "_wait_ready", lambda _process: None)
    monkeypatch.setattr(job_root_bridge, "_bridge_script", lambda: "bridge-script")
    _settings(monkeypatch)
    calls: list[tuple[list[str], dict[str, str]]] = []

    class FakeProcess:
        pid = 4321

        def __init__(
            self,
            command: list[str],
            *,
            stdin: int,
            stdout: int,
            stderr: int,
            env: dict[str, str],
            start_new_session: bool,
        ) -> None:
            del stdin, stdout, stderr
            assert start_new_session is True
            assert token.read_text() == "private-inference-token"
            assert stat.S_IMODE(token.stat().st_mode) == 0o600
            calls.append((command, env))

        def poll(self) -> int | None:
            return None

        def kill(self) -> None:
            raise AssertionError("healthy bridge must not be killed")

        def wait(self, timeout: int) -> int:
            del timeout
            return 0

    monkeypatch.setattr(job_root_bridge.subprocess, "Popen", FakeProcess)

    job_root_bridge.main()

    command, environment = calls[0]
    assert command[-2:] == ["-c", "bridge-script"]
    assert "HF_INFERENCE_TOKEN" not in environment
    assert "HF_TOKEN" not in environment
    assert "HARBOR_HF_WORKER_CAPABILITY" not in environment
    assert set(environment) == {
        "HOME",
        "LANG",
        "PATH",
        "HARBOR_HF_INFERENCE_TOKEN_FILE",
        "HARBOR_HF_INFERENCE_LOCAL_PORT",
        "HARBOR_HF_INFERENCE_ALLOWED_PATH",
        "HARBOR_HF_INFERENCE_UPSTREAM",
        "HARBOR_HF_INFERENCE_ALLOWED_MODEL",
        "HARBOR_HF_INFERENCE_MAX_REQUESTS",
        "HARBOR_HF_INFERENCE_MAX_CONCURRENCY",
        "HARBOR_HF_INFERENCE_TIMEOUT_SECONDS",
        "HARBOR_HF_INFERENCE_MAX_OUTPUT_TOKENS",
    }
    assert not token.exists()
    assert json.loads(handle.read_text()) == {
        "schema_version": "v1",
        "pid": 4321,
        "start_time": 123,
    }
    assert stat.S_IMODE(handle.stat().st_mode) == 0o600
    assert json.loads(route.read_text()) == {
        "schema_version": "v1",
        "api": "chat-completions",
        "base_url": "http://127.0.0.1:18080/v1",
        "api_key": "harbor-local-inference-bridge",
        "model": "example/model",
    }
    assert stat.S_IMODE(route.stat().st_mode) == 0o644
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_rejects_unknown_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("HARBOR_HF_INFERENCE_API", "unknown")

    with pytest.raises(RuntimeError, match="API is invalid"):
        job_root_bridge.main()
