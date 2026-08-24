from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from harbor_hf.control import (
    AttemptOutcomePayload,
    AttemptStartedPayload,
    CancellationPayload,
    Clock,
    IdentifierFactory,
    LifecyclePayload,
    ManualInterventionResolutionPayload,
    RetryCategory,
    RunEvent,
    RunProjection,
    ShardRetryPayload,
    SpendRecordedPayload,
    WaveLifecyclePayload,
    new_event,
    ordered_events,
    project_run,
)
from harbor_hf.runs import RunLock

ExecutionStatus = Literal[
    "planned",
    "queued",
    "active",
    "verifying",
    "publishing",
    "complete",
    "invalid",
    "failed_infrastructure",
    "cancelled",
]
ShardStatus = Literal[
    "planned",
    "queued",
    "active",
    "verifying",
    "publishing",
    "retry_wait",
    "complete",
    "invalid",
    "failed_infrastructure",
    "cancelled",
]
TrialStatus = Literal[
    "planned",
    "active",
    "retry_wait",
    "complete",
    "invalid",
    "failed_infrastructure",
    "cancelled",
]
TaskOutcome = Literal[
    "scored",
    "agent_failed",
    "benchmark_failed",
    "infrastructure_exhausted",
]
AttemptStatus = Literal["active", "completed", "failed", "cancelled"]
WaveStatus = Literal[
    "acquiring",
    "provisioning",
    "ready",
    "active",
    "draining",
    "cleaning",
    "closed",
    "cleanup_failed",
]
TerminalStatus = Literal["completed", "partial", "failed", "cancelled"]

_RETRYABLE_CATEGORIES = {
    "lost",
    "transient",
    "quota",
    "rate-limit",
    "ambiguous",
    "evidence",
}
_TERMINAL_STATUSES = {"complete", "invalid", "failed_infrastructure", "cancelled"}
_SCORED_TERMINAL_STATUSES = {"complete", "invalid", "failed_infrastructure"}
_RUN_TERMINAL = {"completed", "partial", "failed", "cancelled"}


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AttemptProjection(FrozenModel):
    attempt_id: str
    trial_id: str
    shard_id: str
    wave_id: str | None
    physical_attempt: int
    status: AttemptStatus
    category: RetryCategory | None = None
    observed_at: datetime
    retry_after_seconds: int | None = None
    estimated_cost_microusd: int = 0
    spend_microusd: int = 0
    message: str | None = None


class TrialProjection(FrozenModel):
    trial_id: str
    shard_id: str
    logical_attempt: int
    status: TrialStatus = "planned"
    attempts: dict[str, AttemptProjection] = Field(default_factory=dict)
    retry_not_before: datetime | None = None
    outcome: TaskOutcome | None = None


class ShardProjection(FrozenModel):
    shard_id: str
    execution_id: str
    status: ShardStatus = "planned"
    trial_ids: list[str]
    observed_status: ShardStatus | None = None


class ExecutionProjection(FrozenModel):
    execution_id: str
    deployment_digest: str
    status: ExecutionStatus = "planned"
    shard_ids: list[str]
    observed_status: ExecutionStatus | None = None


class WaveProjection(FrozenModel):
    wave_id: str
    deployment_digest: str
    provider: str
    shard_ids: list[str]
    estimated_cost_microusd: int
    status: WaveStatus


class ProjectionCounts(FrozenModel):
    planned: int = 0
    active: int = 0
    retrying: int = 0
    complete: int = 0
    invalid: int = 0
    failed: int = 0
    cancelled: int = 0
    physical_retries: int = 0


class TerminalDecision(FrozenModel):
    status: TerminalStatus
    marker: Literal["_SUCCESS", "_PARTIAL", "_FAILED", "_CANCELLED"]
    summary_path: str
    marker_path: str
    reason: str
    counts: ProjectionCounts


class RecoveryProjection(FrozenModel):
    run: RunProjection
    executions: dict[str, ExecutionProjection]
    shards: dict[str, ShardProjection]
    trials: dict[str, TrialProjection]
    attempts: dict[str, AttemptProjection]
    waves: dict[str, WaveProjection]
    spend_microusd: int
    counts: ProjectionCounts
    cancel_requested_at: datetime | None = None
    terminal_decision: TerminalDecision | None = None

    @property
    def status(self) -> str:
        return self.run.status


def durable_cancellation_event(
    lock: RunLock,
    events: list[RunEvent],
    reason: str,
    *,
    clock: Clock = lambda: datetime.now(UTC),
    identifier: IdentifierFactory | None = None,
) -> tuple[RunEvent, bool]:
    for event in ordered_events(events):
        if event.kind == "run.cancel-requested":
            return event, False
    return (
        new_event(
            subject_type="run",
            subject_id=lock.run_id,
            kind="run.cancel-requested",
            producer="cli",
            payload=CancellationPayload(reason=reason),
            clock=clock,
            identifier=identifier or _cancellation_identifier(lock),
        ),
        True,
    )


def durable_manual_intervention_resolution_event(
    lock: RunLock,
    events: list[RunEvent],
    reason: str,
    *,
    cleanup_verified: bool,
    clock: Clock = lambda: datetime.now(UTC),
) -> tuple[RunEvent, bool]:
    """Resume a run after an operator has verified failed cleanup."""
    if not cleanup_verified:
        raise ValueError("manual recovery requires verified endpoint cleanup")
    ordered = ordered_events(events)
    projection = project_recovery(lock, events)
    if projection.run.status != "manual_intervention":
        resolved = next(
            (
                event
                for event in reversed(ordered)
                if event.kind == "run.manual-intervention-resolved"
            ),
            None,
        )
        if resolved is not None:
            return resolved, False
        raise ValueError("run does not require manual intervention")
    required, required_wave_ids = _manual_recovery_requirements(events)
    wave_ids = sorted(
        {
            *required_wave_ids,
            *(
                wave.wave_id
                for wave in projection.waves.values()
                if wave.status == "cleanup_failed"
            ),
        }
    )
    identity = (
        f"{lock.run_id}:{','.join(event.event_id for event in required)}:"
        f"{','.join(wave_ids)}"
    )
    identifier = hashlib.sha256(f"{identity}:resolved".encode()).hexdigest()[:32]
    event_id = f"evt-{identifier}"
    for event in ordered:
        if event.event_id == event_id:
            if event.kind != "run.manual-intervention-resolved":
                raise ValueError("manual recovery event identity conflicts")
            return event, False
    _validate_manual_recovery_waves(projection, wave_ids)
    observed_at = max(
        clock().astimezone(UTC),
        max(event.observed_at for event in ordered) + timedelta(microseconds=1),
    )
    return (
        new_event(
            subject_type="run",
            subject_id=lock.run_id,
            kind="run.manual-intervention-resolved",
            producer="cli",
            payload=ManualInterventionResolutionPayload(
                wave_ids=wave_ids,
                message=reason,
            ),
            clock=lambda: observed_at,
            identifier=lambda: identifier,
        ),
        True,
    )


def _manual_recovery_requirements(
    events: list[RunEvent],
) -> tuple[list[RunEvent], list[str]]:
    resolved_wave_ids = {
        wave_id
        for event in ordered_events(events)
        if event.kind == "run.manual-intervention-resolved"
        for wave_id in cast(ManualInterventionResolutionPayload, event.payload).wave_ids
    }
    required = [
        event
        for event in ordered_events(events)
        if event.kind == "run.manual-intervention-required"
        and cast(LifecyclePayload, event.payload).parent_id not in resolved_wave_ids
    ]
    if not required:
        raise ValueError("manual intervention requirement has not been recorded")
    wave_ids = sorted(
        {
            payload.parent_id
            for event in required
            if (payload := cast(LifecyclePayload, event.payload)).parent_id is not None
        }
    )
    if not wave_ids:
        raise ValueError("manual intervention does not reference recoverable cleanup")
    return required, wave_ids


def _validate_manual_recovery_waves(
    projection: RecoveryProjection, wave_ids: list[str]
) -> None:
    for wave_id in wave_ids:
        wave = projection.waves.get(wave_id)
        if wave is None or wave.status not in {"cleanup_failed", "closed"}:
            raise ValueError(
                "manual intervention does not reference recoverable cleanup"
            )


def _cancellation_identifier(lock: RunLock) -> IdentifierFactory:
    return lambda: hashlib.sha256(f"{lock.run_id}:cancel".encode()).hexdigest()[:32]


def durable_shard_retry_event(
    lock: RunLock,
    events: list[RunEvent],
    shard_id: str,
    reason: str,
    *,
    clock: Clock = lambda: datetime.now(UTC),
) -> tuple[RunEvent, bool]:
    """Create one immediate retry request for the shard's current execution state."""
    projection = project_recovery(lock, events)
    if projection.run.status in _RUN_TERMINAL:
        raise ValueError("a terminal run cannot be retried")
    if projection.run.status in {"cancel_requested", "draining"}:
        raise ValueError("a cancelling run cannot be retried")
    shard = projection.shards.get(shard_id)
    if shard is None:
        raise ValueError(f"unknown run shard: {shard_id}")
    eligible = [
        projection.trials[trial_id]
        for trial_id in shard.trial_ids
        if projection.trials[trial_id].status in {"retry_wait", "failed_infrastructure"}
    ]
    if not eligible:
        raise ValueError("shard has no retryable logical trials")
    generation = ",".join(
        f"{trial.trial_id}:{len(trial.attempts)}" for trial in eligible
    )
    identifier = hashlib.sha256(
        f"{lock.run_id}:{shard_id}:{generation}".encode()
    ).hexdigest()[:32]
    event_id = f"evt-{identifier}"
    for event in ordered_events(events):
        if event.event_id == event_id:
            if event.kind != "run.shard-retry-requested":
                raise ValueError("retry event identity conflicts")
            return event, False
    return (
        new_event(
            subject_type="run",
            subject_id=lock.run_id,
            kind="run.shard-retry-requested",
            producer="cli",
            payload=ShardRetryPayload(
                shard_id=shard_id,
                reason=reason,
                trial_generations={
                    trial.trial_id: len(trial.attempts) for trial in eligible
                },
            ),
            clock=clock,
            identifier=lambda: identifier,
        ),
        True,
    )


def project_recovery(lock: RunLock, events: list[RunEvent]) -> RecoveryProjection:
    run = project_run(lock, events)
    executions, shards, trials = _initial_projections(lock)
    attempts: dict[str, AttemptProjection] = {}
    waves: dict[str, WaveProjection] = {}
    spend = 0
    for event in ordered_events(events):
        _apply_run_recovery_event(event, waves)
        spend += _apply_recovery_event(
            event, executions, shards, trials, attempts, waves
        )
    if run.status not in _RUN_TERMINAL and any(
        wave.status == "cleanup_failed" for wave in waves.values()
    ):
        run = run.model_copy(update={"status": "manual_intervention"})
    trials = _derive_trials(lock, trials, attempts)
    trials = _apply_retry_requests(lock, events, trials)
    shards = _derive_shards(shards, trials)
    executions = _derive_executions(executions, shards)
    counts = _counts(trials)
    if run.status == "queued" and (attempts or waves):
        run = run.model_copy(update={"status": "active"})
    terminal = _terminal_decision(lock, run, executions, trials, waves, counts)
    cancel_requested_at = next(
        (
            event.observed_at
            for event in ordered_events(events)
            if event.kind == "run.cancel-requested"
        ),
        None,
    )
    return RecoveryProjection(
        run=run,
        executions=executions,
        shards=shards,
        trials=trials,
        attempts=attempts,
        waves=waves,
        spend_microusd=spend,
        counts=counts,
        cancel_requested_at=cancel_requested_at,
        terminal_decision=terminal,
    )


def seal_partial_projection(projection: RecoveryProjection) -> RecoveryProjection:
    """Convert drained retry failures into typed terminal scoring outcomes."""
    decision = projection.terminal_decision
    if projection.run.status != "partial" or decision is None:
        raise ValueError("only a recorded partial run can be sealed")
    if decision.status != "partial":
        raise ValueError("only a recorded partial run can be sealed")
    if any(wave.status != "closed" for wave in projection.waves.values()):
        raise ValueError("partial run cleanup is not complete")

    trials: dict[str, TrialProjection] = {}
    for trial_id, trial in projection.trials.items():
        if trial.status in _SCORED_TERMINAL_STATUSES:
            trials[trial_id] = trial
            continue
        trials[trial_id] = _seal_retry_wait_trial(trial)

    shards = _derive_shards(projection.shards, trials)
    executions = _derive_executions(projection.executions, shards)
    if any(run.status != "complete" for run in executions.values()):
        raise ValueError("a sealed run requires at least one scored trial")
    return projection.model_copy(
        update={
            "executions": executions,
            "shards": shards,
            "trials": trials,
            "counts": _counts(trials),
        }
    )


def _seal_retry_wait_trial(trial: TrialProjection) -> TrialProjection:
    if trial.status != "retry_wait" or not trial.attempts:
        raise ValueError(f"partial trial cannot be sealed: {trial.trial_id}")
    latest = max(
        trial.attempts.values(),
        key=lambda attempt: attempt.physical_attempt,
    )
    if latest.status != "failed" or latest.category is None:
        raise ValueError(f"partial trial has no failed attempt: {trial.trial_id}")
    status: TrialStatus = (
        "invalid"
        if latest.category in {"agent", "benchmark"}
        else "failed_infrastructure"
    )
    outcome: TaskOutcome = (
        "agent_failed"
        if latest.category == "agent"
        else (
            "benchmark_failed"
            if latest.category == "benchmark"
            else "infrastructure_exhausted"
        )
    )
    return trial.model_copy(
        update={
            "status": status,
            "retry_not_before": None,
            "outcome": outcome,
        }
    )


def _apply_retry_requests(
    lock: RunLock,
    events: list[RunEvent],
    trials: dict[str, TrialProjection],
) -> dict[str, TrialProjection]:
    requested_at: dict[tuple[str, int], datetime] = {}
    ordered = ordered_events(events)
    legacy_generations = (
        _legacy_retry_generations(lock, ordered)
        if any(
            event.kind == "run.shard-retry-requested"
            and not cast(ShardRetryPayload, event.payload).trial_generations
            for event in ordered
        )
        else {}
    )
    for event in ordered:
        if event.kind != "run.shard-retry-requested":
            continue
        payload = cast(ShardRetryPayload, event.payload)
        generations = (
            _validated_retry_generations(lock, payload, trials)
            if payload.trial_generations
            else legacy_generations[event.event_id]
        )
        for trial_id, generation in generations.items():
            key = (trial_id, generation)
            requested_at[key] = max(
                requested_at.get(key, event.observed_at), event.observed_at
            )
    recovered: dict[str, TrialProjection] = {}
    for trial_id, trial in trials.items():
        generations = [
            generation
            for requested_trial_id, generation in requested_at
            if requested_trial_id == trial_id
        ]
        if not generations:
            recovered[trial_id] = trial
            continue
        generation = max(generations)
        current_generation = len(trial.attempts)
        if generation > current_generation:
            raise ValueError("retry request generation exceeds physical attempt state")
        cleared = trial.model_copy(
            update={"status": "planned", "retry_not_before": None, "outcome": None}
        )
        derived = _derive_trial(lock, cleared, list(trial.attempts.values()))
        if generation == current_generation and derived.status == "retry_wait":
            derived = derived.model_copy(
                update={"retry_not_before": requested_at[(trial_id, generation)]}
            )
        recovered[trial_id] = derived
    return recovered


def _validated_retry_generations(
    lock: RunLock,
    payload: ShardRetryPayload,
    trials: dict[str, TrialProjection],
) -> dict[str, int]:
    shard_ids = {
        shard.shard_id: {trial.trial_id for trial in shard.trials}
        for run in lock.executions
        for shard in run.shards
    }
    allowed = shard_ids.get(payload.shard_id)
    if allowed is None:
        raise ValueError("retry request references an unknown shard")
    if any(
        trial_id not in allowed
        or generation < 1
        or generation > len(trials[trial_id].attempts)
        for trial_id, generation in payload.trial_generations.items()
    ):
        raise ValueError(
            "retry request generations do not match the requested shard state"
        )
    return payload.trial_generations


def _legacy_retry_generations(
    lock: RunLock, events: list[RunEvent]
) -> dict[str, dict[str, int]]:
    """Recover generation bindings for legacy retry events in one forward pass."""
    executions, shards, trials = _initial_projections(lock)
    attempts: dict[str, AttemptProjection] = {}
    attempt_ids_by_trial: dict[str, list[str]] = {trial_id: [] for trial_id in trials}
    waves: dict[str, WaveProjection] = {}
    generations: dict[str, dict[str, int]] = {}
    for event in events:
        if event.kind == "run.shard-retry-requested":
            payload = cast(ShardRetryPayload, event.payload)
            shard = shards.get(payload.shard_id)
            if shard is None:
                raise ValueError("retry request references an unknown shard")
            event_generations: dict[str, int] = {}
            for trial_id in shard.trial_ids:
                current = _derive_trial(
                    lock,
                    trials[trial_id],
                    [
                        attempts[attempt_id]
                        for attempt_id in attempt_ids_by_trial[trial_id]
                    ],
                )
                if current.status == "retry_wait":
                    event_generations[trial_id] = len(current.attempts)
            generations[event.event_id] = event_generations
        _apply_run_recovery_event(event, waves)
        _apply_recovery_event(event, executions, shards, trials, attempts, waves)
        if event.kind == "attempt.started":
            payload = cast(AttemptStartedPayload, event.payload)
            attempt_ids_by_trial[payload.trial_id].append(event.subject_id)
    return generations


def _apply_recovery_event(
    event: RunEvent,
    executions: dict[str, ExecutionProjection],
    shards: dict[str, ShardProjection],
    trials: dict[str, TrialProjection],
    attempts: dict[str, AttemptProjection],
    waves: dict[str, WaveProjection],
) -> int:
    if event.kind.startswith("execution."):
        _record_execution_status(event, executions)
    elif event.kind.startswith("shard."):
        _record_shard_status(event, shards)
    elif event.kind.startswith("trial."):
        _record_trial_status(event, trials)
    elif event.kind == "attempt.started":
        _start_attempt(event, trials, attempts)
    elif event.kind.startswith("attempt."):
        return _finish_attempt(event, attempts)
    elif event.kind.startswith("wave."):
        _record_wave_status(event, shards, waves)
    elif event.kind == "spend.recorded":
        return cast(SpendRecordedPayload, event.payload).amount_microusd
    return 0


def _apply_run_recovery_event(
    event: RunEvent, waves: dict[str, WaveProjection]
) -> None:
    if event.kind != "run.manual-intervention-resolved":
        return
    payload = cast(ManualInterventionResolutionPayload, event.payload)
    for wave_id in payload.wave_ids:
        wave = waves.get(wave_id)
        if wave is None or wave.status not in {"cleanup_failed", "closed"}:
            raise ValueError("manual recovery does not reference a failed cleanup wave")
        if wave.status == "cleanup_failed":
            waves[wave.wave_id] = wave.model_copy(update={"status": "closed"})


def retry_delay_seconds(
    lock: RunLock,
    category: RetryCategory,
    physical_attempt: int,
    attempt_id: str,
    retry_after_seconds: int | None = None,
) -> int:
    if category not in _RETRYABLE_CATEGORIES:
        raise ValueError(f"retry category is terminal: {category}")
    policy = lock.recovery_policy
    exponent = min(physical_attempt - 1, 30)
    multiplier = 2 if category in {"quota", "rate-limit"} else 1
    raw = policy.retry_base_seconds * (2**exponent) * multiplier
    digest = hashlib.sha256(
        f"{attempt_id}:{category}:{physical_attempt}".encode()
    ).digest()
    jittered = raw * (75 + digest[0] * 50 // 255) // 100
    requested = retry_after_seconds or 0
    return min(policy.retry_max_seconds, max(1, jittered, requested))


def retry_is_ready(trial: TrialProjection, now: datetime) -> bool:
    return (
        trial.status == "retry_wait"
        and trial.retry_not_before is not None
        and now.astimezone(UTC) >= trial.retry_not_before
    )


def _initial_projections(
    lock: RunLock,
) -> tuple[
    dict[str, ExecutionProjection],
    dict[str, ShardProjection],
    dict[str, TrialProjection],
]:
    executions: dict[str, ExecutionProjection] = {}
    shards: dict[str, ShardProjection] = {}
    trials: dict[str, TrialProjection] = {}
    for run in lock.executions:
        executions[run.execution_id] = ExecutionProjection(
            execution_id=run.execution_id,
            deployment_digest=run.deployment_digest,
            shard_ids=[shard.shard_id for shard in run.shards],
        )
        for shard in run.shards:
            shards[shard.shard_id] = ShardProjection(
                shard_id=shard.shard_id,
                execution_id=run.execution_id,
                trial_ids=[trial.trial_id for trial in shard.trials],
            )
            for trial in shard.trials:
                trials[trial.trial_id] = TrialProjection(
                    trial_id=trial.trial_id,
                    shard_id=shard.shard_id,
                    logical_attempt=trial.logical_attempt,
                )
    return executions, shards, trials


def _record_execution_status(
    event: RunEvent, executions: dict[str, ExecutionProjection]
) -> None:
    execution = executions.get(event.subject_id)
    if execution is None:
        raise ValueError(f"event references unknown execution: {event.subject_id}")
    status = cast(
        ExecutionStatus, event.kind.removeprefix("execution.").replace("-", "_")
    )
    executions[event.subject_id] = execution.model_copy(
        update={"observed_status": status}
    )


def _record_shard_status(event: RunEvent, shards: dict[str, ShardProjection]) -> None:
    shard = shards.get(event.subject_id)
    if shard is None:
        raise ValueError(f"event references unknown shard: {event.subject_id}")
    status = cast(ShardStatus, event.kind.removeprefix("shard.").replace("-", "_"))
    shards[event.subject_id] = shard.model_copy(update={"observed_status": status})


def _record_trial_status(event: RunEvent, trials: dict[str, TrialProjection]) -> None:
    trial = trials.get(event.subject_id)
    if trial is None:
        raise ValueError(f"event references unknown trial: {event.subject_id}")
    status = cast(TrialStatus, event.kind.removeprefix("trial.").replace("-", "_"))
    trials[event.subject_id] = trial.model_copy(update={"status": status})


def _start_attempt(
    event: RunEvent,
    trials: dict[str, TrialProjection],
    attempts: dict[str, AttemptProjection],
) -> None:
    payload = cast(AttemptStartedPayload, event.payload)
    trial = trials.get(payload.trial_id)
    if trial is None or trial.shard_id != payload.shard_id:
        raise ValueError("attempt references an unknown trial or shard")
    if event.subject_id in attempts:
        raise ValueError(f"attempt started more than once: {event.subject_id}")
    physical_attempt_numbers = {
        attempt.physical_attempt
        for attempt in attempts.values()
        if attempt.trial_id == payload.trial_id
    }
    if payload.physical_attempt in physical_attempt_numbers:
        raise ValueError("trial has duplicate physical attempt numbers")
    attempts[event.subject_id] = AttemptProjection(
        attempt_id=event.subject_id,
        status="active",
        observed_at=event.observed_at,
        **payload.model_dump(mode="python"),
    )


def _finish_attempt(event: RunEvent, attempts: dict[str, AttemptProjection]) -> int:
    attempt = attempts.get(event.subject_id)
    if attempt is None:
        raise ValueError(f"attempt outcome has no start: {event.subject_id}")
    if attempt.status != "active":
        if _is_reconciler_lost_sentinel(event):
            return 0
        raise ValueError(f"attempt has multiple outcomes: {event.subject_id}")
    payload = cast(AttemptOutcomePayload, event.payload)
    if (
        payload.trial_id != attempt.trial_id
        or payload.physical_attempt != attempt.physical_attempt
    ):
        raise ValueError("attempt outcome identity does not match its start")
    if event.kind == "attempt.completed" and payload.category is not None:
        raise ValueError("completed attempt cannot have a failure category")
    if event.kind == "attempt.failed" and payload.category is None:
        raise ValueError("failed attempt requires a failure category")
    status = cast(AttemptStatus, event.kind.removeprefix("attempt."))
    attempts[event.subject_id] = attempt.model_copy(
        update={
            "status": status,
            "category": payload.category,
            "observed_at": event.observed_at,
            "retry_after_seconds": payload.retry_after_seconds,
            "spend_microusd": payload.spend_microusd,
            "message": payload.message,
        }
    )
    return payload.spend_microusd


def _is_reconciler_lost_sentinel(event: RunEvent) -> bool:
    if event.producer != "reconciler" or event.kind != "attempt.failed":
        return False
    payload = cast(AttemptOutcomePayload, event.payload)
    return (
        payload.category == "lost"
        and payload.message is not None
        and payload.message.startswith("HF Job ")
        and payload.message.endswith(" without terminal attempt evidence")
    )


def _record_wave_status(
    event: RunEvent,
    shards: dict[str, ShardProjection],
    waves: dict[str, WaveProjection],
) -> None:
    payload = cast(WaveLifecyclePayload, event.payload)
    unknown = set(payload.shard_ids) - shards.keys()
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"wave references unknown shards: {names}")
    status = cast(WaveStatus, event.kind.removeprefix("wave.").replace("-", "_"))
    previous = waves.get(event.subject_id)
    identity = (
        payload.deployment_digest,
        payload.provider,
        payload.shard_ids,
        payload.estimated_cost_microusd,
    )
    if previous is not None:
        observed = (
            previous.deployment_digest,
            previous.provider,
            previous.shard_ids,
            previous.estimated_cost_microusd,
        )
        if observed != identity:
            raise ValueError("wave lifecycle identity changed")
        stale_reconciler_transition = event.producer == "reconciler" and (
            (previous.status == "closed" and status in {"draining", "cleaning"})
            or (previous.status == "cleanup_failed" and status == "draining")
        )
        if stale_reconciler_transition:
            return
        _validate_wave_transition(previous.status, status)
    waves[event.subject_id] = WaveProjection(
        wave_id=event.subject_id,
        status=status,
        **payload.model_dump(mode="python"),
    )


def _validate_wave_transition(previous: WaveStatus, current: WaveStatus) -> None:
    allowed: dict[WaveStatus, set[WaveStatus]] = {
        "acquiring": {"provisioning", "draining", "cleaning", "cleanup_failed"},
        "provisioning": {"ready", "draining", "cleaning", "cleanup_failed"},
        "ready": {"active", "draining", "cleaning", "cleanup_failed"},
        "active": {"draining", "cleaning", "cleanup_failed"},
        "draining": {"cleaning", "closed", "cleanup_failed"},
        "cleaning": {"closed", "cleanup_failed"},
        "cleanup_failed": {"cleaning", "closed"},
        "closed": set(),
    }
    if current != previous and current not in allowed[previous]:
        raise ValueError(f"invalid wave transition: {previous} -> {current}")


def _derive_trials(
    lock: RunLock,
    trials: dict[str, TrialProjection],
    attempts: dict[str, AttemptProjection],
) -> dict[str, TrialProjection]:
    by_trial: dict[str, list[AttemptProjection]] = {key: [] for key in trials}
    for attempt in attempts.values():
        by_trial[attempt.trial_id].append(attempt)
    return {
        trial_id: _derive_trial(lock, trial, by_trial[trial_id])
        for trial_id, trial in trials.items()
    }


def _derive_trial(
    lock: RunLock,
    trial: TrialProjection,
    attempts: list[AttemptProjection],
) -> TrialProjection:
    ordered = sorted(attempts, key=lambda value: value.physical_attempt)
    physical_attempts = [attempt.physical_attempt for attempt in ordered]
    if physical_attempts != list(range(1, len(ordered) + 1)):
        raise ValueError("physical attempt numbers must be contiguous")
    completed = [
        index for index, attempt in enumerate(ordered) if attempt.status == "completed"
    ]
    if completed and completed[-1] != len(ordered) - 1:
        raise ValueError("a completed logical trial was physically re-executed")
    attempt_map = {value.attempt_id: value for value in ordered}
    if trial.status in _TERMINAL_STATUSES:
        return trial.model_copy(
            update={
                "attempts": attempt_map,
                "outcome": _task_outcome(trial.status, ordered),
            }
        )
    if not ordered:
        return trial
    if any(execution.status == "completed" for execution in ordered):
        return trial.model_copy(
            update={
                "status": "complete",
                "attempts": attempt_map,
                "outcome": "scored",
            }
        )
    latest = ordered[-1]
    if latest.status == "active":
        status: TrialStatus = "active"
        retry_at = None
    elif latest.status == "cancelled":
        status = "cancelled"
        retry_at = None
    else:
        status, retry_at = _failed_trial_state(lock, latest, len(ordered))
    return trial.model_copy(
        update={
            "status": status,
            "attempts": attempt_map,
            "retry_not_before": retry_at,
            "outcome": _task_outcome(status, ordered),
        }
    )


def _task_outcome(
    status: TrialStatus, attempts: list[AttemptProjection]
) -> TaskOutcome | None:
    if status == "complete":
        return "scored"
    if status not in {"invalid", "failed_infrastructure"} or not attempts:
        return None
    latest = attempts[-1]
    if latest.status != "failed":
        return None
    if latest.category == "agent":
        return "agent_failed"
    if latest.category == "benchmark":
        return "benchmark_failed"
    return "infrastructure_exhausted"


def _failed_trial_state(
    lock: RunLock, execution: AttemptProjection, attempt_count: int
) -> tuple[TrialStatus, datetime | None]:
    category = execution.category
    if category in {"agent", "benchmark"}:
        return "invalid", None
    if category not in _RETRYABLE_CATEGORIES:
        return "failed_infrastructure", None
    if attempt_count >= lock.recovery_policy.max_physical_attempts_per_trial:
        return (
            "invalid" if category in {"agent", "benchmark"} else "failed_infrastructure"
        ), None
    delay = retry_delay_seconds(
        lock,
        category,
        execution.physical_attempt,
        execution.attempt_id,
        execution.retry_after_seconds,
    )
    return "retry_wait", execution.observed_at + timedelta(seconds=delay)


def _derive_shards(
    shards: dict[str, ShardProjection], trials: dict[str, TrialProjection]
) -> dict[str, ShardProjection]:
    return {
        shard_id: shard.model_copy(update={"status": _aggregate_shard(shard, trials)})
        for shard_id, shard in shards.items()
    }


def _aggregate_shard(
    shard: ShardProjection, trials: dict[str, TrialProjection]
) -> ShardStatus:
    statuses = [trials[trial_id].status for trial_id in shard.trial_ids]
    _validate_observed_terminal(shard.observed_status, statuses, "shard")
    scored = _scored_terminal_status(statuses)
    if scored is not None:
        return cast(ShardStatus, scored)
    if all(status in _TERMINAL_STATUSES for status in statuses):
        return "cancelled"
    if any(status == "active" for status in statuses):
        return "active"
    if any(status == "retry_wait" for status in statuses):
        return "retry_wait"
    return shard.observed_status or "planned"


def _derive_executions(
    executions: dict[str, ExecutionProjection], shards: dict[str, ShardProjection]
) -> dict[str, ExecutionProjection]:
    return {
        execution_id: run.model_copy(
            update={"status": _aggregate_execution(run, shards)}
        )
        for execution_id, run in executions.items()
    }


def _aggregate_execution(
    run: ExecutionProjection, shards: dict[str, ShardProjection]
) -> ExecutionStatus:
    statuses = [shards[shard_id].status for shard_id in run.shard_ids]
    _validate_observed_terminal(run.observed_status, statuses, "execution")
    scored = _scored_terminal_status(statuses)
    if scored is not None:
        return cast(ExecutionStatus, scored)
    if all(status in _TERMINAL_STATUSES for status in statuses):
        return "cancelled"
    if any(status in {"active", "retry_wait"} for status in statuses):
        return "active"
    return run.observed_status or "planned"


def _scored_terminal_status(
    statuses: list[TrialStatus] | list[ShardStatus],
) -> Literal["complete", "invalid", "failed_infrastructure"] | None:
    if not all(status in _SCORED_TERMINAL_STATUSES for status in statuses):
        return None
    if any(status == "complete" for status in statuses):
        return "complete"
    if any(status == "failed_infrastructure" for status in statuses):
        return "failed_infrastructure"
    return "invalid"


def _validate_observed_terminal(
    observed: ExecutionStatus | ShardStatus | None,
    child_statuses: list[TrialStatus] | list[ShardStatus],
    subject: str,
) -> None:
    if observed not in _TERMINAL_STATUSES:
        return
    if not all(status in _TERMINAL_STATUSES for status in child_statuses):
        raise ValueError(f"{subject} became terminal before its children")
    if observed == "complete" and (
        not all(status in _SCORED_TERMINAL_STATUSES for status in child_statuses)
        or not any(status == "complete" for status in child_statuses)
    ):
        raise ValueError(f"{subject} completed with non-complete children")


def _counts(trials: dict[str, TrialProjection]) -> ProjectionCounts:
    values = list(trials.values())
    attempts = sum(len(trial.attempts) for trial in values)
    return ProjectionCounts(
        planned=sum(trial.status == "planned" for trial in values),
        active=sum(trial.status == "active" for trial in values),
        retrying=sum(trial.status == "retry_wait" for trial in values),
        complete=sum(trial.status == "complete" for trial in values),
        invalid=sum(trial.status == "invalid" for trial in values),
        failed=sum(trial.status == "failed_infrastructure" for trial in values),
        cancelled=sum(trial.status == "cancelled" for trial in values),
        physical_retries=max(
            0, attempts - sum(bool(trial.attempts) for trial in values)
        ),
    )


def _terminal_decision(
    lock: RunLock,
    run: RunProjection,
    executions: dict[str, ExecutionProjection],
    trials: dict[str, TrialProjection],
    waves: dict[str, WaveProjection],
    counts: ProjectionCounts,
) -> TerminalDecision | None:
    if run.status in _RUN_TERMINAL:
        status = cast(TerminalStatus, run.status)
        return _decision(lock, status, _terminal_counts(run, counts), "recorded")
    if not _cleanup_is_complete(run, waves):
        return None
    decision_counts = _terminal_counts(run, counts)
    cancelling = run.status in {"cancel_requested", "draining"}
    if not all(
        trial.status in _TERMINAL_STATUSES
        or (cancelling and trial.status in {"planned", "retry_wait"})
        for trial in trials.values()
    ):
        return None
    run_statuses = [run.status for run in executions.values()]
    if run_statuses and all(status == "complete" for status in run_statuses):
        return _decision(
            lock,
            "completed",
            decision_counts,
            "all logical trials reached a scored terminal outcome",
        )
    if cancelling and decision_counts.complete:
        return _decision(
            lock,
            "partial",
            decision_counts,
            "cancellation preserved some scored trial outcomes",
        )
    if any(status == "complete" for status in run_statuses):
        return _decision(
            lock, "partial", decision_counts, "some executions reached a scored outcome"
        )
    if cancelling or decision_counts.cancelled:
        return _decision(
            lock,
            "cancelled",
            decision_counts,
            "cancellation drained and cleaned",
        )
    return _decision(
        lock, "failed", decision_counts, "no valid logical trial completed"
    )


def _terminal_counts(run: RunProjection, counts: ProjectionCounts) -> ProjectionCounts:
    if run.status not in {
        "cancel_requested",
        "draining",
        "cancelled",
        "partial",
    }:
        return counts
    return counts.model_copy(
        update={
            "planned": 0,
            "retrying": 0,
            "cancelled": counts.cancelled + counts.planned + counts.retrying,
        }
    )


def _cleanup_is_complete(run: RunProjection, waves: dict[str, WaveProjection]) -> bool:
    for action in run.actions.values():
        wave = waves.get(f"wave-{action.action_key}")
        if (
            action.action_kind in {"submit-wave", "retry-shard"}
            and action.status != "failed"
            and (wave is None or wave.status != "closed")
        ):
            return False
    return all(wave.status == "closed" for wave in waves.values())


def _decision(
    lock: RunLock,
    status: TerminalStatus,
    counts: ProjectionCounts,
    reason: str,
) -> TerminalDecision:
    markers = {
        "completed": "_SUCCESS",
        "partial": "_PARTIAL",
        "failed": "_FAILED",
        "cancelled": "_CANCELLED",
    }
    marker = cast(
        Literal["_SUCCESS", "_PARTIAL", "_FAILED", "_CANCELLED"], markers[status]
    )
    return TerminalDecision(
        status=status,
        marker=marker,
        summary_path=f"{lock.artifact_prefix}/run-summary.json",
        marker_path=f"{lock.artifact_prefix}/{marker}",
        reason=reason,
        counts=counts,
    )
