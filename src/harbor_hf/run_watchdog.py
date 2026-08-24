from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from harbor_hf.control import RunSnapshot, RunStore
from harbor_hf.controller_status import (
    ControllerAttemptReservation,
    ControllerClaim,
    ControllerRecoveryDecision,
    ControllerStateStore,
    ControllerStatus,
    ProviderCapacityClaim,
)
from harbor_hf.io import load_experiment_bytes
from harbor_hf.models import RunControllerSpec
from harbor_hf.recovery import project_recovery
from harbor_hf.runs import RunLock
from harbor_hf.submission import (
    ControllerJobsApi,
    RunControllerSubmission,
    TextRunner,
    launch_reserved_run_controller,
)

_TERMINAL_JOB_STAGES = {"COMPLETED", "CANCELED", "CANCELLED", "ERROR", "DELETED"}


class RunWatchdogError(RuntimeError):
    """Raised when controller recovery evidence is unsafe or incomplete."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ControllerJob(FrozenModel):
    job_id: str
    stage: str
    attempt: int = Field(ge=1)

    @property
    def terminal(self) -> bool:
        return self.stage.upper() in _TERMINAL_JOB_STAGES


class WatchdogDecision(FrozenModel):
    action: Literal["none", "recover", "operator"]
    reason: str
    replacement_attempt: int | None = Field(default=None, ge=2)


class WatchdogResult(FrozenModel):
    run_id: str
    decision: WatchdogDecision
    submission: RunControllerSubmission | None = None


class SnapshotRunStore(RunStore, Protocol):
    def load_snapshot(self, run_id: str) -> RunSnapshot: ...


def plan_controller_watchdog(
    lock: RunLock,
    status: ControllerStatus | None,
    claim: ControllerClaim | None,
    jobs: list[ControllerJob],
    *,
    run_terminal: bool,
    now: datetime,
) -> WatchdogDecision:
    policy = lock.controller_policy
    if policy is None:
        return WatchdogDecision(action="none", reason="run uses no controller")
    if run_terminal or (status is not None and status.state == "completed"):
        return WatchdogDecision(action="none", reason="run is terminal")
    active = [job for job in jobs if not job.terminal]
    if active:
        return WatchdogDecision(action="none", reason="controller Job is active")
    if claim is not None and claim.expires_at > now.astimezone(UTC):
        return WatchdogDecision(action="none", reason="controller claim is still valid")
    return _status_recovery_decision(policy, status)


def _status_recovery_decision(
    policy: RunControllerSpec,
    status: ControllerStatus | None,
) -> WatchdogDecision:
    if status is None:
        return WatchdogDecision(
            action="operator", reason="controller has no durable status"
        )
    if status.state in {
        "paused-capacity",
        "paused-policy",
        "failed-deterministic",
    }:
        return WatchdogDecision(
            action="operator",
            reason=f"controller state requires approval: {status.state}",
        )
    if status.attempt >= policy.max_attempts:
        return WatchdogDecision(
            action="operator", reason="controller attempt limit is exhausted"
        )
    retryable_states = {
        "starting",
        "running",
        "waiting-retry",
        "finalizing",
        "failed-infrastructure",
    }
    if status.state not in retryable_states:
        return WatchdogDecision(
            action="operator",
            reason="controller outcome is not retryable infrastructure",
        )
    return WatchdogDecision(
        action="recover",
        reason="prior controller is terminal or absent and its claim is stale",
        replacement_attempt=status.attempt + 1,
    )


def run_watchdog(
    run_id: str,
    *,
    store: SnapshotRunStore,
    state_store: ControllerStateStore,
    jobs_api: ControllerJobsApi,
    runner: TextRunner,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    dry_run: bool = False,
) -> WatchdogResult:
    snapshot = store.load_snapshot(run_id)
    spec = load_experiment_bytes(snapshot.request, source="run request")
    projection = project_recovery(snapshot.lock, snapshot.events)
    status = state_store.read_status(run_id)
    claim = state_store.read_claim(run_id)
    jobs = _controller_jobs(
        jobs_api, snapshot.lock, spec.remote.job.namespace if spec.remote else ""
    )
    if not dry_run:
        _release_abandoned_provider_capacity(
            snapshot.lock,
            state_store,
            jobs,
        )
    decision = plan_controller_watchdog(
        snapshot.lock,
        status,
        claim,
        jobs,
        run_terminal=projection.status
        in {"completed", "partial", "failed", "cancelled"},
        now=clock(),
    )
    if decision.action != "recover" or dry_run:
        return WatchdogResult(run_id=run_id, decision=decision)
    if status is None or decision.replacement_attempt is None:
        raise RunWatchdogError("watchdog recovery has no prior controller status")
    prior = state_store.read_attempt(run_id, status.attempt)
    if prior is None:
        raise RunWatchdogError("watchdog recovery has no immutable launch contract")
    replacement_attempt = decision.replacement_attempt
    existing_recovery = state_store.read_recovery(run_id, replacement_attempt)
    existing_replacement = state_store.read_attempt(run_id, replacement_attempt)
    recorded_at = (
        existing_recovery.decided_at
        if existing_recovery is not None
        else (
            existing_replacement.reserved_at
            if existing_replacement is not None
            else clock().astimezone(UTC)
        )
    )
    replacement = ControllerAttemptReservation(
        **prior.model_dump(
            mode="python",
            exclude={"attempt", "reserved_at"},
        ),
        attempt=replacement_attempt,
        reserved_at=recorded_at,
    )
    recovery = ControllerRecoveryDecision(
        run_id=run_id,
        plan_digest=snapshot.lock.plan_digest,
        prior_job_id=status.job_id,
        prior_attempt=status.attempt,
        replacement_attempt=replacement_attempt,
        checkpoint_revision=(
            existing_recovery.checkpoint_revision
            if existing_recovery is not None
            else snapshot.control_commit
        ),
        category="lost",
        decided_at=recorded_at,
    )
    if existing_recovery is not None and existing_recovery != recovery:
        raise RunWatchdogError(
            "watchdog recovery decision conflicts with the run state"
        )
    if existing_replacement is not None and existing_replacement != replacement:
        raise RunWatchdogError(
            "watchdog replacement attempt conflicts with the launch contract"
        )
    state_store.write_recovery(recovery)
    state_store.reserve_attempt(replacement)
    submission = launch_reserved_run_controller(
        snapshot.lock,
        spec,
        replacement,
        runner=runner,
        jobs_api=jobs_api,
        state_store=state_store,
        clock=clock,
    )
    return WatchdogResult(
        run_id=run_id,
        decision=decision,
        submission=submission,
    )


def _release_abandoned_provider_capacity(
    lock: RunLock,
    state_store: ControllerStateStore,
    jobs: list[ControllerJob],
) -> None:
    active_job_ids = {job.job_id for job in jobs if not job.terminal}
    providers = sorted(
        {run.provider for run in lock.executions if run.provider is not None}
    )
    for provider in providers:
        claim = state_store.read_provider_capacity(provider)
        if not _capacity_is_abandoned(claim, lock.run_id, active_job_ids):
            continue
        assert claim is not None
        state_store.release_provider_capacity(claim)


def _capacity_is_abandoned(
    claim: ProviderCapacityClaim | None,
    run_id: str,
    active_job_ids: set[str],
) -> bool:
    return (
        claim is not None
        and claim.run_id == run_id
        and claim.job_id not in active_job_ids
    )


def _controller_jobs(
    api: ControllerJobsApi,
    lock: RunLock,
    namespace: str,
) -> list[ControllerJob]:
    if not namespace:
        raise RunWatchdogError("run watchdog requires remote settings")
    labels = {
        "harbor-hf-role": "run-controller",
        "harbor-hf-run": lock.run_id,
        "harbor-hf-plan": lock.plan_digest.removeprefix("sha256:")[:16],
    }
    resources: Iterable[object] = api.list_jobs(labels=labels, namespace=namespace)
    jobs: list[ControllerJob] = []
    seen_attempts: set[int] = set()
    for resource in resources:
        job = _controller_job(resource, labels)
        if job.attempt in seen_attempts:
            raise RunWatchdogError(
                "multiple controller Jobs have one physical attempt identity"
            )
        seen_attempts.add(job.attempt)
        jobs.append(job)
    return sorted(jobs, key=lambda job: job.attempt)


def _controller_job(value: object, expected: Mapping[str, str]) -> ControllerJob:
    identifier = getattr(value, "id", None)
    labels = getattr(value, "labels", None)
    status = getattr(value, "status", None)
    stage_value = getattr(status, "stage", status)
    stage = getattr(stage_value, "value", stage_value)
    if not isinstance(identifier, str) or not identifier:
        raise RunWatchdogError("controller Job has no ID")
    if not isinstance(labels, Mapping) or any(
        labels.get(key) != item for key, item in expected.items()
    ):
        raise RunWatchdogError("controller Job labels do not match the run")
    attempt_value = labels.get("harbor-hf-controller-attempt")
    if not isinstance(attempt_value, str) or not attempt_value.isdigit():
        raise RunWatchdogError("controller Job has no valid attempt label")
    if not isinstance(stage, str) or not stage:
        raise RunWatchdogError("controller Job has no stage")
    return ControllerJob(job_id=identifier, stage=stage, attempt=int(attempt_value))
