from __future__ import annotations

import json
import os
from typing import Annotated, NoReturn
from uuid import uuid4

import httpx
import typer

from harbor_hf.workbench_cli import TransientControlError, register_workbench_commands

app = typer.Typer(
    no_args_is_help=True,
    help="Operate Harbor-HF through the TypeScript control service.",
)
run_app = typer.Typer(no_args_is_help=True, help="Submit and inspect runs.")
app.add_typer(run_app, name="run")
capacity_app = typer.Typer(help="Inspect and set the shared namespace Job cap.")
app.add_typer(capacity_app, name="capacity")


def _base_url() -> str:
    value = os.environ.get("HARBOR_HF_CONTROL_URL", "").rstrip("/")
    if not value:
        raise typer.BadParameter(
            "set HARBOR_HF_CONTROL_URL to the private control Space URL"
        )
    if not value.startswith("https://") and not value.startswith("http://127.0.0.1"):
        raise typer.BadParameter("the control URL must use HTTPS")
    return value


def _headers(*, idempotency_key: str | None = None) -> dict[str, str]:
    token = os.environ.get("HARBOR_HF_CONTROL_BEARER_TOKEN", "").strip()
    if not token:
        raise typer.BadParameter(
            "set HARBOR_HF_CONTROL_BEARER_TOKEN to an explicitly approved, "
            "purpose-scoped control credential"
        )
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.echo(json.dumps({"error": message}, sort_keys=True), err=True)
    raise typer.Exit(code)


def _request(
    method: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
    idempotency_key: str | None = None,
    timeout_seconds: float = 30.0,
    transient: bool = False,
) -> object:
    try:
        response = httpx.request(
            method,
            f"{_base_url()}{path}",
            headers=_headers(idempotency_key=idempotency_key),
            json=payload,
            timeout=timeout_seconds,
            follow_redirects=False,
        )
    except httpx.HTTPError as error:
        if transient:
            raise TransientControlError from None
        _fail(f"control API request failed: {type(error).__name__}")
    except ValueError as error:
        _fail(f"control API request failed: {type(error).__name__}")
    if transient and response.status_code in {429, 500, 502, 503, 504}:
        raise TransientControlError
    if response.status_code >= 400:
        try:
            body = response.json()
            message = body["error"]["message"]
        except (KeyError, TypeError, ValueError):
            message = "request rejected"
        _fail(f"control API returned {response.status_code}: {message}")
    if response.status_code == 204:
        return {}
    return response.json()


def _echo(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True))


register_workbench_commands(app, request=_request, echo=_echo, fail=_fail)


@capacity_app.callback(invoke_without_command=True)
def capacity(ctx: typer.Context) -> None:
    """Show the shared namespace Job cap."""
    if ctx.invoked_subcommand is not None:
        return
    _echo(_request("GET", "/api/v1/capacity"))


@capacity_app.command("set")
def capacity_set(
    max_jobs: Annotated[int, typer.Option("--max-jobs", min=1, max=1024)],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm the namespace Job cap change."),
    ] = False,
) -> None:
    """Replace the promoted namespace Job cap and start pacing."""
    if not yes:
        typer.confirm(
            f"Set the namespace Job cap to {max_jobs}? "
            "Existing runs keep their locked per-run Job and worker limits.",
            abort=True,
        )
    key = str(uuid4())
    typer.echo(json.dumps({"idempotency_key": key}), err=True)
    _echo(
        _request(
            "POST",
            "/api/v1/capacity",
            payload={
                "max_active_jobs": max_jobs,
                "confirmed": True,
            },
            idempotency_key=key,
        )
    )


@app.command("status")
def status() -> None:
    """Show control-service readiness and write mode."""
    _echo(_request("GET", "/api/v1/system"))


@run_app.command("list")
def run_list() -> None:
    """List runs from the control service."""
    _echo(_request("GET", "/api/v1/runs"))


@run_app.command("status")
def run_status(run_id: Annotated[str, typer.Argument()]) -> None:
    """Show one run."""
    _echo(_request("GET", f"/api/v1/runs/{run_id}"))


@run_app.command("continue-historical")
def run_continue_historical(
    run_id: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option("--reason")],
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Confirm the immutable execution attachment.",
        ),
    ] = False,
) -> None:
    """Attach the reviewed current execution to one paused historical run."""
    if not yes:
        typer.confirm(
            f"Attach the current reviewed execution to historical run {run_id}?",
            abort=True,
        )
    key = idempotency_key or str(uuid4())
    if not idempotency_key:
        typer.echo(json.dumps({"idempotency_key": key}), err=True)
    _echo(
        _request(
            "POST",
            f"/api/v1/runs/{run_id}/continuation",
            payload={"reason": reason, "confirmed": True},
            idempotency_key=key,
        )
    )


@run_app.command("repair-continuation")
def run_repair_continuation(
    run_id: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option("--reason")],
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Confirm the immutable continuation worker repair.",
        ),
    ] = False,
) -> None:
    """Attach the reviewed worker image and revision to a historical continuation."""
    if not yes:
        typer.confirm(
            f"Attach the reviewed worker repair to historical run {run_id}?",
            abort=True,
        )
    key = idempotency_key or str(uuid4())
    if not idempotency_key:
        typer.echo(json.dumps({"idempotency_key": key}), err=True)
    _echo(
        _request(
            "POST",
            f"/api/v1/runs/{run_id}/continuation-repair",
            payload={"reason": reason, "confirmed": True},
            idempotency_key=key,
        )
    )


@run_app.command("repair-continuation-successor")
def run_repair_continuation_successor(
    run_id: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option("--reason")],
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Confirm the immutable successor continuation worker repair.",
        ),
    ] = False,
) -> None:
    """Attach one reviewed successor to a historical continuation repair."""
    if not yes:
        typer.confirm(
            f"Attach the reviewed successor worker repair to historical run {run_id}?",
            abort=True,
        )
    key = idempotency_key or str(uuid4())
    if not idempotency_key:
        typer.echo(json.dumps({"idempotency_key": key}), err=True)
    _echo(
        _request(
            "POST",
            f"/api/v1/runs/{run_id}/continuation-repair-successor",
            payload={"reason": reason, "confirmed": True},
            idempotency_key=key,
        )
    )


@run_app.command("submit")
def run_submit(
    benchmark: Annotated[str, typer.Option("--benchmark")],
    model: Annotated[str, typer.Option("--model")],
    harness: Annotated[str, typer.Option("--harness")],
    ceiling_microusd: Annotated[int, typer.Option("--ceiling-microusd", min=0)],
    launch_policy: Annotated[str, typer.Option("--launch-policy")],
    deployment: Annotated[str | None, typer.Option("--deployment")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    start_paused: Annotated[bool, typer.Option("--start-paused")] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm the resolved launch and cost ceiling."),
    ] = False,
) -> None:
    """Submit profile references and return the durable run ID."""
    if not yes:
        typer.confirm(
            f"Launch {benchmark} with {model} through {harness}, "
            f"with a ceiling of {ceiling_microusd} micro-USD?",
            abort=True,
        )
    key = idempotency_key or str(uuid4())
    if not idempotency_key:
        typer.echo(json.dumps({"idempotency_key": key}), err=True)
    payload: dict[str, object] = {
        "benchmark": benchmark,
        "model": model,
        "harness": harness,
        "deployment": deployment,
        "launch_policy": launch_policy,
        "ceiling_microusd": ceiling_microusd,
        "confirmed": True,
    }
    if start_paused:
        payload["start_paused"] = True
    _echo(_request("POST", "/api/v1/runs", payload=payload, idempotency_key=key))


def _run_action(
    run_id: str,
    action: str,
    *,
    task_id: str | None = None,
    reason: str | None = None,
    task_limit: int | None = None,
    publication_id: str | None = None,
    yes: bool,
) -> None:
    if not yes:
        prompt = (
            f"Cancel run {run_id}?"
            if action == "cancel"
            else f"Apply {action} to {run_id}?"
        )
        typer.confirm(prompt, abort=True)
    key = str(uuid4())
    typer.echo(json.dumps({"idempotency_key": key}), err=True)
    payload: dict[str, object] = {
        "action": action,
        "task_id": task_id,
        "reason": reason,
        "confirmed": True,
    }
    if task_limit is not None:
        payload["task_limit"] = task_limit
    if publication_id is not None:
        payload["publication_id"] = publication_id
    _echo(
        _request(
            "POST",
            f"/api/v1/runs/{run_id}/actions",
            payload=payload,
            idempotency_key=key,
        )
    )


@run_app.command("cancel")
def run_cancel(
    run_id: Annotated[str, typer.Argument()],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Cancel a run's open logical tasks without deleting evidence."""
    _run_action(run_id, "cancel", reason=reason, yes=yes)


@run_app.command("pause")
def run_pause(
    run_id: Annotated[str, typer.Argument()],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Stop new task admission at the next durable task boundary."""
    _run_action(run_id, "pause", reason=reason, yes=yes)


@run_app.command("resume")
def run_resume(
    run_id: Annotated[str, typer.Argument()],
    task_limit: Annotated[int | None, typer.Option("--task-limit", min=1)] = None,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Resume unresolved tasks without repeating completed tasks."""
    _run_action(
        run_id,
        "resume",
        task_limit=task_limit,
        reason=reason,
        yes=yes,
    )


@run_app.command("supersede")
def run_supersede(
    run_id: Annotated[str, typer.Argument()],
    publication_id: Annotated[str, typer.Option("--publication")],
    reason: Annotated[str, typer.Option("--reason")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Mark an older publication superseded after this run publishes."""
    _run_action(
        run_id,
        "supersede",
        publication_id=publication_id,
        reason=reason,
        yes=yes,
    )


@run_app.command("retry-infrastructure")
def run_retry_infrastructure(
    run_id: Annotated[str, typer.Argument()],
    task_id: Annotated[str | None, typer.Option("--task")] = None,
    all_eligible: Annotated[bool, typer.Option("--all-eligible")] = False,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Request a bounded infrastructure-only replacement."""
    if all_eligible == bool(task_id):
        _fail("provide exactly one of --task or --all-eligible")
    _run_action(
        run_id,
        "retry_infrastructure",
        task_id=None if all_eligible else task_id,
        reason=reason,
        yes=yes,
    )


@run_app.command("pause-endpoint")
def run_pause_endpoint(
    run_id: Annotated[str, typer.Argument()],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Pause the run's one active managed endpoint."""
    _run_action(
        run_id,
        "pause_endpoint",
        reason=reason,
        yes=yes,
    )


@app.command("jobs")
def jobs() -> None:
    """List projected HF Job actions."""
    _echo(_request("GET", "/api/v1/jobs"))


@app.command("endpoints")
def endpoints() -> None:
    """List managed endpoint state and cleanup status."""
    _echo(_request("GET", "/api/v1/endpoints"))


@app.command("profiles")
def profiles() -> None:
    """List resolved immutable profiles."""
    _echo(_request("GET", "/api/v1/profiles"))


@app.command("results")
def results() -> None:
    """List immutable result publications."""
    _echo(_request("GET", "/api/v1/results"))


@app.command("audit")
def audit() -> None:
    """List recent immutable control records."""
    _echo(_request("GET", "/api/v1/audit"))


if __name__ == "__main__":
    app()
