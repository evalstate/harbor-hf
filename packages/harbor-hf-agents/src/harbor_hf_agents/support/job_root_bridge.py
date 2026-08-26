"""Start the root-owned inference bridge before the trusted trial worker."""

from __future__ import annotations

import json
import logging
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

from harbor_hf_agents.support.hf_inference_bridge import (
    _bridge_script,
    _process_start_time,
    _stop_job_root_bridge,
)

_LOGGER = logging.getLogger(__name__)
_LOCAL_API_KEY = "harbor-local-inference-bridge"
_LOCAL_PORT = 18080
_ROUTE_PATHS = {
    "chat-completions": "/v1/chat/completions",
    "responses": "/v1/responses",
}
_BRIDGE_SETTING_NAMES = (
    "HARBOR_HF_INFERENCE_UPSTREAM",
    "HARBOR_HF_INFERENCE_ALLOWED_MODEL",
    "HARBOR_HF_INFERENCE_MAX_REQUESTS",
    "HARBOR_HF_INFERENCE_MAX_CONCURRENCY",
    "HARBOR_HF_INFERENCE_TIMEOUT_SECONDS",
    "HARBOR_HF_INFERENCE_MAX_OUTPUT_TOKENS",
)
_TOKEN_PATH = Path("/run/harbor-hf-inference.token")
_ROUTE_PATH = Path("/run/harbor-hf-inference.json")
_HANDLE_PATH = Path("/run/harbor-hf-inference-bridge.json")
_LOG_PATH = Path("/run/harbor-hf-inference-bridge.log")
_USAGE_PATH = Path("/run/harbor-hf-inference-usage.json")
_USAGE_TEMP_PATH = Path("/run/harbor-hf-inference-usage.json.tmp")


def _required(name: str) -> str:
    try:
        value = os.environ[name]
    except KeyError as error:
        raise RuntimeError(f"required Job bridge setting {name} is missing") from error
    if not value:
        raise RuntimeError(f"required Job bridge setting {name} is empty")
    return value


def _required_positive_int(name: str) -> int:
    """Return a required positive integer bridge setting."""
    raw = _required(name)
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"required Job bridge setting {name} is invalid") from error
    if value <= 0:
        raise RuntimeError(f"required Job bridge setting {name} is invalid")
    return value


def _bridge_environment(token_path: Path, allowed_path: str) -> dict[str, str]:
    environment = {
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HARBOR_HF_INFERENCE_TOKEN_FILE": str(token_path),
        "HARBOR_HF_INFERENCE_LOCAL_PORT": str(_LOCAL_PORT),
        "HARBOR_HF_INFERENCE_ALLOWED_PATH": allowed_path,
        "HARBOR_HF_INFERENCE_USAGE_FILE": str(_USAGE_PATH),
    }
    environment.update({name: _required(name) for name in _BRIDGE_SETTING_NAMES})
    return environment


def _write_bytes(path: Path, payload: bytes, mode: int) -> None:
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_NOFOLLOW | os.O_TRUNC | os.O_WRONLY,
        mode,
    )
    try:
        os.write(descriptor, payload)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: object, mode: int) -> None:
    _write_bytes(
        path,
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        mode,
    )


def _wait_ready(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("HF inference bridge exited before readiness")
        try:
            with socket.create_connection(
                ("127.0.0.1", _LOCAL_PORT),
                timeout=0.25,
            ):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("HF inference bridge did not become ready")


def _validate_runtime_paths() -> None:
    for path in (
        _TOKEN_PATH,
        _ROUTE_PATH,
        _HANDLE_PATH,
        _LOG_PATH,
        _USAGE_PATH,
        _USAGE_TEMP_PATH,
    ):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"Job bridge path is a symbolic link: {path}")


def _stop_previous_bridge() -> None:
    if _HANDLE_PATH.exists():
        _stop_job_root_bridge()
    _ROUTE_PATH.unlink(missing_ok=True)
    _TOKEN_PATH.unlink(missing_ok=True)
    _USAGE_PATH.unlink(missing_ok=True)
    _USAGE_TEMP_PATH.unlink(missing_ok=True)


def main() -> None:
    """Start the bridge and publish only its non-secret loopback settings."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if os.geteuid() != 0:
        raise RuntimeError("Job inference bridge bootstrap must run as host root")
    api = _required("HARBOR_HF_INFERENCE_API")
    try:
        allowed_path = _ROUTE_PATHS[api]
    except KeyError as error:
        raise RuntimeError("Job bridge API is invalid") from error
    _validate_runtime_paths()
    _stop_previous_bridge()
    _write_bytes(_TOKEN_PATH, _required("HF_INFERENCE_TOKEN").encode(), 0o600)
    log_descriptor = os.open(
        _LOG_PATH,
        os.O_CREAT | os.O_NOFOLLOW | os.O_TRUNC | os.O_WRONLY,
        0o600,
    )
    _LOGGER.info("Starting root-owned inference bridge")
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", _bridge_script()],
            stdin=subprocess.DEVNULL,
            stdout=log_descriptor,
            stderr=subprocess.STDOUT,
            env=_bridge_environment(_TOKEN_PATH, allowed_path),
            start_new_session=True,
        )
        _write_json(
            _HANDLE_PATH,
            {
                "schema_version": "v1",
                "pid": process.pid,
                "start_time": _process_start_time(process.pid),
            },
            0o600,
        )
        _wait_ready(process)
        _write_json(
            _ROUTE_PATH,
            {
                "schema_version": "v1",
                "api": api,
                "base_url": f"http://127.0.0.1:{_LOCAL_PORT}/v1",
                "api_key": _LOCAL_API_KEY,
                "model": _required("HARBOR_HF_INFERENCE_ALLOWED_MODEL"),
                "max_output_tokens": _required_positive_int(
                    "HARBOR_HF_INFERENCE_MAX_OUTPUT_TOKENS"
                ),
            },
            0o644,
        )
    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        _HANDLE_PATH.unlink(missing_ok=True)
        _ROUTE_PATH.unlink(missing_ok=True)
        raise
    finally:
        os.close(log_descriptor)
        _TOKEN_PATH.unlink(missing_ok=True)
    _LOGGER.info("Published local inference route")


if __name__ == "__main__":
    main()
