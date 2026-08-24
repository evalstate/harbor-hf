"""Root-owned bridge for approved Hugging Face inference credentials."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import select
import shlex
import signal
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from harbor.agents.installed.base import BaseInstalledAgent
    from harbor.environments.base import BaseEnvironment

_HF_JOBS_HOST = re.compile(r"^[a-z0-9-]+\.hf\.jobs$")
_HF_ENDPOINT_HOST = re.compile(
    r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.endpoints\.huggingface\.cloud$"
)
_HF_ROUTER_HOST = "router.huggingface.co"
_LOCAL_API_KEY = "harbor-local-inference-bridge"
_BRIDGE_ACTIVE_ATTRIBUTE = "_harbor_hf_inference_bridge_active"
_BRIDGE_KIND_ATTRIBUTE = "_harbor_hf_inference_bridge_kind"
_JOB_BRIDGE_HANDLE = Path("/run/harbor-hf-inference-bridge.json")
_JOB_BRIDGE_ROUTE = Path("/run/harbor-hf-inference.json")
_JOB_BRIDGE_LOG = Path("/run/harbor-hf-inference-bridge.log")
_JOB_BRIDGE_TOKEN = Path("/run/harbor-hf-inference.token")
_JOB_BRIDGE_STOP_SECONDS = 10


def _upstream_request_path(upstream_path: str, request_path: str) -> str:
    base_path = upstream_path.rstrip("/")
    if base_path.endswith("/v1"):
        base_path = base_path[:-3]
    return f"{base_path}{request_path}"


def _run_hf_inference_bridge() -> None:  # noqa: C901 -- isolated bridge parser
    """Run as root inside the HF Job and inject the inference credential."""
    import http.client
    import json
    import os
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit

    upstream = urlsplit(os.environ["HARBOR_HF_INFERENCE_UPSTREAM"])
    upstream_host = upstream.hostname
    if upstream_host is None:
        raise RuntimeError("Hugging Face inference upstream host is missing")
    try:
        token_file = os.environ["HARBOR_HF_INFERENCE_TOKEN_FILE"]
    except KeyError:
        token_file = None
    if token_file:
        with open(token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
        os.unlink(token_file)
    else:
        token = os.environ["HARBOR_HF_INFERENCE_TOKEN"]
    port = int(os.environ["HARBOR_HF_INFERENCE_LOCAL_PORT"])
    allowed_path = os.environ["HARBOR_HF_INFERENCE_ALLOWED_PATH"]
    allowed_model = os.environ["HARBOR_HF_INFERENCE_ALLOWED_MODEL"]
    max_requests = int(os.environ["HARBOR_HF_INFERENCE_MAX_REQUESTS"])
    max_concurrency = int(os.environ["HARBOR_HF_INFERENCE_MAX_CONCURRENCY"])
    timeout_seconds = int(os.environ["HARBOR_HF_INFERENCE_TIMEOUT_SECONDS"])
    max_output_tokens = int(os.environ["HARBOR_HF_INFERENCE_MAX_OUTPUT_TOKENS"])
    max_response_bytes = min(64 * 1024 * 1024, 1024 * 1024 + max_output_tokens * 1024)
    local_api_key = "harbor-local-inference-bridge"
    counter_lock = threading.Lock()
    request_count = 0
    header_timeout_seconds = 10
    body_timeout_seconds = 30
    socket_timeout_seconds = 30

    class BoundedThreadingHTTPServer(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = max(1, min(max_concurrency, 64))

        def __init__(
            self,
            server_address: tuple[str, int],
            request_handler_class: type[BaseHTTPRequestHandler],
            bind_and_activate: bool = True,
        ) -> None:
            self.handler_slots = threading.BoundedSemaphore(max_concurrency)
            super().__init__(
                server_address,
                request_handler_class,
                bind_and_activate,
            )

        def process_request(
            self,
            request: socket.socket | tuple[bytes, socket.socket],
            client_address: tuple[str, int] | tuple[str, int, int, int],
        ) -> None:
            if not self.handler_slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self.handler_slots.release()
                raise

        def process_request_thread(
            self,
            request: socket.socket | tuple[bytes, socket.socket],
            client_address: tuple[str, int] | tuple[str, int, int, int],
        ) -> None:
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.handler_slots.release()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(header_timeout_seconds)

        def do_POST(self) -> None:  # noqa: C901 -- isolated bridge parser
            nonlocal request_count
            self.close_connection = True
            if self.path != allowed_path:
                self.send_error(404)
                return
            if self.headers.get("Authorization") != f"Bearer {local_api_key}":
                self.send_error(401)
                return
            with counter_lock:
                if request_count >= max_requests:
                    self.send_error(429)
                    return
                request_count += 1
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.send_error(400)
                return
            if length < 0 or length > 64 * 1024 * 1024:
                self.send_error(413)
                return
            self.connection.settimeout(body_timeout_seconds)
            try:
                body = self.rfile.read(length)
            except TimeoutError:
                self.send_error(408)
                return
            if len(body) != length:
                self.send_error(400)
                return
            try:
                request_body = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error(400)
                return
            if not isinstance(request_body, dict):
                self.send_error(400)
                return
            try:
                requested_model = request_body["model"]
            except KeyError:
                requested_model = None
            if requested_model != allowed_model:
                self.send_error(403)
                return
            output_limit_present = False
            for field in (
                "max_tokens",
                "max_completion_tokens",
                "max_output_tokens",
            ):
                try:
                    value = request_body[field]
                except KeyError:
                    continue
                output_limit_present = True
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 1
                    or value > max_output_tokens
                ):
                    self.send_error(403)
                    return
            if not output_limit_present:
                field = (
                    "max_output_tokens"
                    if allowed_path == "/v1/responses"
                    else "max_tokens"
                )
                request_body[field] = max_output_tokens
            body = json.dumps(
                request_body,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            if len(body) > 64 * 1024 * 1024:
                self.send_error(413)
                return
            connection = http.client.HTTPSConnection(
                upstream_host, 443, timeout=timeout_seconds
            )
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": self.headers.get("Content-Type", "application/json"),
                "Accept": self.headers.get("Accept", "application/json"),
            }
            response_started = False
            try:
                connection.request(
                    "POST",
                    _upstream_request_path(upstream.path, self.path),
                    body=body,
                    headers=headers,
                )
                response = connection.getresponse()
                response_body = bytearray()
                while chunk := response.read(64 * 1024):
                    response_body.extend(chunk)
                    if len(response_body) > max_response_bytes:
                        raise OverflowError("inference bridge response limit exceeded")
                self.connection.settimeout(socket_timeout_seconds)
                self.send_response(response.status)
                for name, value in response.getheaders():
                    if name.lower() in {
                        "content-type",
                        "cache-control",
                        "x-request-id",
                        "request-id",
                    }:
                        self.send_header(name, value)
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                response_started = True
                self.wfile.write(response_body)
                self.wfile.flush()
            except OverflowError:
                payload = json.dumps(
                    {"error": "inference bridge response limit exceeded"}
                ).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
            except (OSError, http.client.HTTPException):
                if not response_started and not self.wfile.closed:
                    payload = json.dumps(
                        {"error": "inference bridge transport failed"}
                    ).encode()
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
            finally:
                connection.close()
                self.close_connection = True

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    BoundedThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def _bridge_script() -> str:
    path_helper = inspect.getsource(_upstream_request_path)
    body = inspect.getsource(_run_hf_inference_bridge)
    return path_helper + "\n" + body + "\n_run_hf_inference_bridge()\n"


def _bridge_command() -> str:
    script = _bridge_script()
    return (
        "set -euo pipefail; "
        "if [ -f /tmp/harbor-hf-inference-bridge.pid ]; then "
        "old_pid=$(cat /tmp/harbor-hf-inference-bridge.pid); "
        'kill "$old_pid" 2>/dev/null || true; '
        "rm -f /tmp/harbor-hf-inference-bridge.pid; fi; "
        "rm -f /tmp/harbor-hf-inference-bridge.ready; "
        f"nohup python3 -c {shlex.quote(script)} "
        ">/tmp/harbor-hf-inference-bridge.log 2>&1 & "
        "pid=$!; printf '%s\\n' \"$pid\" "
        ">/tmp/harbor-hf-inference-bridge.pid; "
        "chmod 0444 /tmp/harbor-hf-inference-bridge.pid; "
        "python3 - <<'PY'\n"
        "import os, socket, time\n"
        "port = int(os.environ['HARBOR_HF_INFERENCE_LOCAL_PORT'])\n"
        "deadline = time.monotonic() + 10\n"
        "while True:\n"
        "    try:\n"
        "        with socket.create_connection(('127.0.0.1', port), timeout=0.25):\n"
        "            break\n"
        "    except OSError:\n"
        "        if time.monotonic() >= deadline:\n"
        "            raise SystemExit('HF inference bridge did not become ready')\n"
        "        time.sleep(0.1)\n"
        "PY\n"
    )


def _process_start_time(pid: int) -> int:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        _, separator, fields = raw.rpartition(")")
        if not separator:
            raise ValueError("missing process name terminator")
        return int(fields.split()[19])
    except (OSError, IndexError, ValueError) as error:
        raise RuntimeError(
            "Job inference bridge process cannot be inspected"
        ) from error


def _read_job_bridge_handle() -> tuple[int, int]:
    metadata = _JOB_BRIDGE_HANDLE.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeError("Job inference bridge handle is not root-owned mode 0600")
    descriptor = os.open(_JOB_BRIDGE_HANDLE, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, encoding="utf-8", closefd=False) as handle:
            value = json.load(handle)
    finally:
        os.close(descriptor)
    if not isinstance(value, dict) or set(value) != {
        "pid",
        "schema_version",
        "start_time",
    }:
        raise RuntimeError("Job inference bridge handle is invalid")
    pid = value["pid"]
    start_time = value["start_time"]
    if (
        value["schema_version"] != "v1"
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid < 2
        or isinstance(start_time, bool)
        or not isinstance(start_time, int)
        or start_time < 1
    ):
        raise RuntimeError("Job inference bridge handle is invalid")
    return pid, start_time


def _stop_job_root_bridge() -> None:
    """Remove the route, kill its exact host process, and await process exit."""
    _JOB_BRIDGE_ROUTE.unlink(missing_ok=True)
    try:
        pid, expected_start_time = _read_job_bridge_handle()
    except FileNotFoundError as error:
        raise RuntimeError("Job inference bridge handle disappeared") from error
    try:
        pidfd_open = cast(
            Callable[[int, int], int],
            vars(os)["pidfd_open"],
        )
        pidfd = pidfd_open(pid, 0)
    except ProcessLookupError:
        _JOB_BRIDGE_HANDLE.unlink(missing_ok=True)
        return
    try:
        if _process_start_time(pid) != expected_start_time:
            raise RuntimeError("Job inference bridge PID was reused")
        pidfd_send_signal = cast(
            Callable[[int, int, None, int], None],
            vars(signal)["pidfd_send_signal"],
        )
        pidfd_send_signal(pidfd, signal.SIGKILL, None, 0)
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        if not poller.poll(_JOB_BRIDGE_STOP_SECONDS * 1000):
            raise RuntimeError("Job inference bridge did not terminate")
    finally:
        os.close(pidfd)
    _JOB_BRIDGE_HANDLE.unlink(missing_ok=True)


def _job_bridge_pid() -> int:
    pid, expected_start_time = _read_job_bridge_handle()
    if _process_start_time(pid) != expected_start_time:
        raise RuntimeError("Job inference bridge PID was reused")
    status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines()
    uid_line = next((line for line in status if line.startswith("Uid:")), "")
    if uid_line.split()[1:] != ["0", "0", "0", "0"]:
        raise RuntimeError("Job inference bridge is not running as host root")
    return pid


def is_hf_inference_url(value: str) -> bool:
    """Return whether *value* is an approved Hugging Face inference URL."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or host is None
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    if host == _HF_ROUTER_HOST:
        return parsed.path.rstrip("/") in {"", "/v1"}
    if _HF_JOBS_HOST.fullmatch(host) is not None:
        return "/scopes/" in parsed.path
    return _HF_ENDPOINT_HOST.fullmatch(host) is not None and parsed.path.rstrip(
        "/"
    ) in {"", "/v1"}


def _positive_limit(value: str | None, name: str, maximum: int) -> int:
    if value is None:
        raise RuntimeError(f"HF inference bridging requires {name}")
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(f"HF inference bridge {name} is invalid") from error
    if parsed < 1 or parsed > maximum:
        raise RuntimeError(f"HF inference bridge {name} is out of range")
    return parsed


async def prepare_hf_inference_bridge(
    agent: BaseInstalledAgent,
    environment: BaseEnvironment,
    env: dict[str, str],
    *,
    base_url_key: str,
    api_key_key: str,
    inference_token: str | None,
    api: Literal["chat-completions", "responses"],
    allowed_model: str,
    max_requests: str | None,
    max_concurrency: str | None,
    timeout_seconds: str | None,
    max_output_tokens: str | None,
    local_port: int = 18080,
) -> bool:
    """Replace an approved HF URL with a root-owned loopback bridge."""
    try:
        base_url = env[base_url_key]
    except KeyError:
        base_url = ""
    if not is_hf_inference_url(base_url):
        return False
    if not inference_token:
        raise RuntimeError("HF inference bridging requires an inference credential")
    if not allowed_model:
        raise RuntimeError("HF inference bridging requires a locked model")
    request_limit = _positive_limit(max_requests, "max requests", 4096)
    concurrency_limit = _positive_limit(max_concurrency, "max concurrency", 64)
    timeout_limit = _positive_limit(timeout_seconds, "timeout", 3600)
    output_limit = _positive_limit(max_output_tokens, "max output tokens", 1048576)
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    upstream = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    allowed_path = (
        "/v1/chat/completions" if api == "chat-completions" else "/v1/responses"
    )
    await agent.exec_as_root(
        environment,
        command=_bridge_command(),
        env={
            "HARBOR_HF_INFERENCE_UPSTREAM": upstream,
            "HARBOR_HF_INFERENCE_TOKEN": inference_token,
            "HARBOR_HF_INFERENCE_LOCAL_PORT": str(local_port),
            "HARBOR_HF_INFERENCE_ALLOWED_PATH": allowed_path,
            "HARBOR_HF_INFERENCE_ALLOWED_MODEL": allowed_model,
            "HARBOR_HF_INFERENCE_MAX_REQUESTS": str(request_limit),
            "HARBOR_HF_INFERENCE_MAX_CONCURRENCY": str(concurrency_limit),
            "HARBOR_HF_INFERENCE_TIMEOUT_SECONDS": str(timeout_limit),
            "HARBOR_HF_INFERENCE_MAX_OUTPUT_TOKENS": str(output_limit),
        },
    )
    env[base_url_key] = f"http://127.0.0.1:{local_port}/v1"
    env[api_key_key] = _LOCAL_API_KEY
    mark_hf_inference_bridge_active(agent, kind="environment")
    try:
        await verify_hf_inference_isolation(agent, environment)
    except BaseException:
        with suppress(Exception):
            await stop_hf_inference_bridge(agent, environment)
        raise
    return True


def mark_hf_inference_bridge_active(
    agent: BaseInstalledAgent,
    *,
    kind: Literal["environment", "job"],
) -> None:
    """Record that this agent owns a root bridge requiring cleanup."""
    setattr(agent, _BRIDGE_ACTIVE_ATTRIBUTE, True)
    setattr(agent, _BRIDGE_KIND_ATTRIBUTE, kind)


def hf_inference_bridge_is_active(agent: BaseInstalledAgent) -> bool:
    """Return whether the agent currently owns a root inference bridge."""
    return getattr(agent, _BRIDGE_ACTIVE_ATTRIBUTE, False) is True


async def verify_hf_inference_isolation(
    agent: BaseInstalledAgent,
    environment: BaseEnvironment,
    *,
    bridge_pid: int | None = None,
) -> None:
    """Verify the task UID cannot inspect the root-owned bridge."""
    if bridge_pid is None:
        command = (
            "set -euo pipefail; "
            "test ! -e /run/harbor-hf-inference.token; "
            "test ! -e /tmp/harbor-hf-inference-bridge.pid"
        )
    else:
        command = (
            "set -euo pipefail; "
            "test ! -e /run/harbor-hf-inference.token; "
            "test ! -e /run/harbor-hf-inference.json; "
            f"! cat /proc/{bridge_pid}/environ >/dev/null 2>&1; "
            "test \"$(awk '/^NoNewPrivs:/ {print $2}' /proc/self/status)\" = 1; "
            "for field in CapInh CapPrm CapEff CapBnd CapAmb; do "
            'test "$(awk -v name="$field:" \'$1 == name {print $2}\' '
            '/proc/self/status)" = 0000000000000000; '
            "done"
        )
    await agent.exec_as_agent(
        environment,
        command=command,
    )


async def stop_hf_inference_bridge(
    agent: BaseInstalledAgent,
    environment: BaseEnvironment,
) -> None:
    """Stop the trial-local bridge and remove its process handle."""
    try:
        kind = getattr(agent, _BRIDGE_KIND_ATTRIBUTE, None)
        if kind == "job":
            await asyncio.to_thread(_stop_job_root_bridge)
            try:
                if _JOB_BRIDGE_LOG.is_file():
                    await environment.upload_file(
                        _JOB_BRIDGE_LOG,
                        "/logs/agent/hf-inference-bridge.log",
                    )
            finally:
                _JOB_BRIDGE_LOG.unlink(missing_ok=True)
                _JOB_BRIDGE_TOKEN.unlink(missing_ok=True)
            return
        await agent.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                "if [ -f /tmp/harbor-hf-inference-bridge.pid ]; then "
                "pid=$(cat /tmp/harbor-hf-inference-bridge.pid); "
                'kill "$pid" 2>/dev/null || true; '
                "rm -f /tmp/harbor-hf-inference-bridge.pid; fi; "
                "if [ -f /tmp/harbor-hf-inference-bridge.log ]; then "
                "install -m 0640 -o root -g harbor-agent "
                "/tmp/harbor-hf-inference-bridge.log "
                "/logs/agent/hf-inference-bridge.log; fi"
            ),
        )
    finally:
        setattr(agent, _BRIDGE_ACTIVE_ATTRIBUTE, False)
        setattr(agent, _BRIDGE_KIND_ATTRIBUTE, None)
