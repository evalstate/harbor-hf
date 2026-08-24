from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from harbor_hf.control import (
    ActionOutcomePayload,
    ActionReservedPayload,
    AttemptOutcomePayload,
    AttemptStartedPayload,
    CancellationPayload,
    ControlError,
    EventKind,
    EventPayload,
    LifecyclePayload,
    RetryCategory,
    RunEvent,
    RunSubmittedPayload,
    SubjectType,
    TerminalPayload,
    WaveLifecyclePayload,
    new_event,
    ordered_events,
    project_run,
)
from harbor_hf.models import ExperimentSpec
from harbor_hf.reconciler import plan_reconciliation
from harbor_hf.recovery import project_recovery, retry_delay_seconds
from harbor_hf.runs import RunLock, build_run_lock, build_run_plan

NOW = datetime(2026, 7, 14, 1, 2, 3, tzinfo=UTC)


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _lock(remote_spec: ExperimentSpec, *, tasks: int = 1) -> RunLock:
    task_digests = {
        f"task-{index}": f"sha256:{index:064x}" for index in range(1, tasks + 1)
    }
    spec = remote_spec.model_copy(
        update={
            "benchmark": remote_spec.benchmark.model_copy(
                update={"task_names": ["task-*"], "task_digests": task_digests}
            ),
            "execution": remote_spec.execution.model_copy(
                update={"max_trials_per_shard": max(tasks, 1)}
            ),
        }
    )
    return build_run_lock(
        build_run_plan(spec), "run-recovery-mutation", clock=lambda: NOW
    )


def _event(
    lock: RunLock,
    sequence: int,
    subject_type: SubjectType,
    subject_id: str,
    kind: EventKind,
    payload: EventPayload,
) -> RunEvent:
    return new_event(
        subject_type=subject_type,
        subject_id=subject_id,
        kind=kind,
        producer="reconciler",
        payload=payload,
        clock=lambda: NOW + timedelta(seconds=sequence),
        identifier=lambda: f"{sequence:032x}",
    )


def _submitted(lock: RunLock) -> RunEvent:
    return _event(
        lock,
        1,
        "run",
        lock.run_id,
        "run.submitted",
        RunSubmittedPayload(plan_digest=lock.plan_digest),
    )


def _trial_event(
    lock: RunLock, sequence: int, trial_index: int, kind: EventKind
) -> RunEvent:
    shard = lock.executions[0].shards[0]
    return _event(
        lock,
        sequence,
        "trial",
        shard.trials[trial_index].trial_id,
        kind,
        LifecyclePayload(parent_id=shard.shard_id),
    )


def _attempt_started(
    lock: RunLock,
    sequence: int,
    *,
    attempt_id: str = "attempt-one",
    attempt: int = 1,
) -> RunEvent:
    shard = lock.executions[0].shards[0]
    trial = shard.trials[0]
    return _event(
        lock,
        sequence,
        "attempt",
        attempt_id,
        "attempt.started",
        AttemptStartedPayload(
            trial_id=trial.trial_id,
            shard_id=shard.shard_id,
            physical_attempt=attempt,
            wave_id="wave-one",
        ),
    )


def _attempt_outcome(
    lock: RunLock,
    sequence: int,
    *,
    attempt_id: str = "attempt-one",
    kind: EventKind = "attempt.completed",
    attempt: int = 1,
    category: str | None = None,
) -> RunEvent:
    trial = lock.executions[0].shards[0].trials[0]
    return _event(
        lock,
        sequence,
        "attempt",
        attempt_id,
        kind,
        AttemptOutcomePayload(
            trial_id=trial.trial_id,
            physical_attempt=attempt,
            category=cast(RetryCategory | None, category),
        ),
    )


def _wave_event(
    lock: RunLock,
    sequence: int,
    kind: EventKind,
    *,
    provider: str = "provider-one",
) -> RunEvent:
    run = lock.executions[0]
    return _event(
        lock,
        sequence,
        "wave",
        "wave-one",
        kind,
        WaveLifecyclePayload(
            deployment_digest=run.deployment_digest,
            provider=provider,
            shard_ids=[run.shards[0].shard_id],
            estimated_cost_microusd=123,
        ),
    )


def test_recovery_terminal_decision_matrix_has_complete_canonical_structures(
    remote_spec: ExperimentSpec,
) -> None:
    two = _lock(remote_spec, tasks=2)
    cases = [
        (_lock(remote_spec), []),
        (_lock(remote_spec), ["trial.complete"]),
        (_lock(remote_spec), ["trial.invalid"]),
        (
            _lock(remote_spec),
            ["run.cancel-requested", "trial.cancelled"],
        ),
        (two, ["trial.complete", "trial.invalid"]),
    ]
    corpus: list[object] = []
    for lock, kinds in cases:
        events = [_submitted(lock)]
        trial_index = 0
        for sequence, kind in enumerate(kinds, 2):
            if kind == "run.cancel-requested":
                events.append(
                    _event(
                        lock,
                        sequence,
                        "run",
                        lock.run_id,
                        "run.cancel-requested",
                        CancellationPayload(reason="operator"),
                    )
                )
            else:
                events.append(
                    _trial_event(lock, sequence, trial_index, cast(EventKind, kind))
                )
                trial_index += 1
        projection, plan = plan_reconciliation(lock, events, now=NOW)
        corpus.append(
            {
                "projection": projection.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
            }
        )

    assert _hash(corpus) == (
        "ff527263cd99e64aba98416aee026438db3ccd8cc10fa27503612058960ac68b"
    )


def test_retry_delay_matrix_pins_backoff_jitter_and_bounds(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _lock(remote_spec)
    values = [
        retry_delay_seconds(
            lock,
            cast(RetryCategory, category),
            attempt,
            f"attempt-{category}-{attempt}",
            retry_after,
        )
        for category in ["lost", "transient", "quota", "rate-limit", "ambiguous"]
        for attempt in [1, 2, 3, 31]
        for retry_after in [None, 1, 59, 999]
    ]

    assert values == [
        35,
        35,
        59,
        999,
        72,
        72,
        72,
        999,
        110,
        110,
        110,
        999,
        1800,
        1800,
        1800,
        1800,
        35,
        35,
        59,
        999,
        55,
        55,
        59,
        999,
        123,
        123,
        123,
        999,
        1800,
        1800,
        1800,
        1800,
        54,
        54,
        59,
        999,
        121,
        121,
        121,
        999,
        290,
        290,
        290,
        999,
        1800,
        1800,
        1800,
        1800,
        65,
        65,
        65,
        999,
        126,
        126,
        126,
        999,
        192,
        192,
        192,
        999,
        1800,
        1800,
        1800,
        1800,
        24,
        24,
        59,
        999,
        50,
        50,
        59,
        999,
        108,
        108,
        108,
        999,
        1800,
        1800,
        1800,
        1800,
    ]


def _identity_history(lock: RunLock, case: str) -> list[RunEvent]:
    run = lock.executions[0]
    trial = run.shards[0].trials[0]
    events = [_submitted(lock)]
    if case == "unknown-execution":
        events.append(
            _event(
                lock,
                2,
                "execution",
                "missing",
                "execution.active",
                LifecyclePayload(),
            )
        )
    elif case == "unknown-shard":
        events.append(
            _event(lock, 2, "shard", "missing", "shard.active", LifecyclePayload())
        )
    elif case == "unknown-trial":
        events.append(
            _event(lock, 2, "trial", "missing", "trial.complete", LifecyclePayload())
        )
    elif case == "bad-start":
        events.append(
            _attempt_started(lock, 2).model_copy(
                update={
                    "payload": AttemptStartedPayload(
                        trial_id=trial.trial_id,
                        shard_id="missing",
                        physical_attempt=1,
                        wave_id="wave-one",
                    )
                }
            )
        )
    elif case == "duplicate-start":
        events.extend([_attempt_started(lock, 2), _attempt_started(lock, 3)])
    elif case == "duplicate-attempt":
        events.extend(
            [
                _attempt_started(lock, 2),
                _attempt_started(lock, 3, attempt_id="attempt-two"),
            ]
        )
    return events


def _outcome_wave_history(lock: RunLock, case: str) -> list[RunEvent]:
    run = lock.executions[0]
    events = [_submitted(lock)]
    if case == "outcome-no-start":
        events.append(_attempt_outcome(lock, 2))
    elif case == "multiple-outcomes":
        events.extend(
            [
                _attempt_started(lock, 2),
                _attempt_outcome(lock, 3),
                _attempt_outcome(lock, 4),
            ]
        )
    elif case == "outcome-mismatch":
        events.extend(
            [
                _attempt_started(lock, 2),
                _attempt_outcome(lock, 3, attempt=2),
            ]
        )
    elif case == "completed-category":
        events.extend(
            [
                _attempt_started(lock, 2),
                _attempt_outcome(lock, 3, category="transient"),
            ]
        )
    elif case == "failed-no-category":
        events.extend(
            [
                _attempt_started(lock, 2),
                _attempt_outcome(lock, 3, kind="attempt.failed"),
            ]
        )
    elif case == "wave-unknown":
        events.append(
            _wave_event(lock, 2, "wave.acquiring").model_copy(
                update={
                    "payload": WaveLifecyclePayload(
                        deployment_digest=run.deployment_digest,
                        provider="provider-one",
                        shard_ids=["missing"],
                        estimated_cost_microusd=123,
                    )
                }
            )
        )
    elif case == "wave-identity":
        events.extend(
            [
                _wave_event(lock, 2, "wave.acquiring"),
                _wave_event(lock, 3, "wave.provisioning", provider="provider-two"),
            ]
        )
    else:
        events.extend(
            [
                _wave_event(lock, 2, "wave.acquiring"),
                _wave_event(lock, 3, "wave.active"),
            ]
        )
    return events


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("unknown-execution", "event references unknown execution: missing"),
        ("unknown-shard", "event references unknown shard: missing"),
        ("unknown-trial", "event references unknown trial: missing"),
        ("bad-start", "attempt references an unknown trial or shard"),
        ("duplicate-start", "attempt started more than once: attempt-one"),
        ("duplicate-attempt", "trial has duplicate physical attempt numbers"),
        ("outcome-no-start", "attempt outcome has no start: attempt-one"),
        ("multiple-outcomes", "attempt has multiple outcomes: attempt-one"),
        ("outcome-mismatch", "attempt outcome identity does not match its start"),
        ("completed-category", "completed attempt cannot have a failure category"),
        ("failed-no-category", "failed attempt requires a failure category"),
        ("wave-unknown", "wave references unknown shards: missing"),
        ("wave-identity", "wave lifecycle identity changed"),
        ("wave-transition", "invalid wave transition: acquiring -> active"),
    ],
)
def test_recovery_history_rejection_matrix_has_exact_errors(
    remote_spec: ExperimentSpec, case: str, message: str
) -> None:
    lock = _lock(remote_spec)
    identity_cases = {
        "unknown-execution",
        "unknown-shard",
        "unknown-trial",
        "bad-start",
        "duplicate-start",
        "duplicate-attempt",
    }
    events = (
        _identity_history(lock, case)
        if case in identity_cases
        else _outcome_wave_history(lock, case)
    )

    with pytest.raises(ValueError) as captured:
        project_recovery(lock, events)

    assert str(captured.value) == message


def test_control_action_projection_and_rejections_use_complete_values(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _lock(remote_spec)
    submitted = _submitted(lock)
    events = [submitted]
    for index, outcome in enumerate(
        ["action.succeeded", "action.failed", "action.ambiguous"], 2
    ):
        action_id = f"action-{index}"
        events.extend(
            [
                _event(
                    lock,
                    index,
                    "run",
                    lock.run_id,
                    "action.reserved",
                    ActionReservedPayload(
                        action_id=action_id,
                        action_key=f"key-{index}",
                        action_kind="submit-wave",
                        target_ids=[f"target-{index}"],
                    ),
                ),
                _event(
                    lock,
                    index + 10,
                    "run",
                    lock.run_id,
                    cast(EventKind, outcome),
                    ActionOutcomePayload(
                        action_id=action_id,
                        message=f"message-{index}",
                        remote_id=f"remote-{index}",
                    ),
                ),
            ]
        )

    projection = project_run(lock, list(reversed(events)))

    assert _hash(projection.model_dump(mode="json")) == (
        "27b3238e8f6be4f70acf204c1c32921b80a41cafb4d79f867c9992df153a0d5e"
    )

    conflicting = submitted.model_copy(update={"subject_id": "wrong"})
    with pytest.raises(ControlError) as captured:
        project_run(lock, [conflicting])
    assert str(captured.value) == ("run submission event does not match its lock")


def test_execution_and_shard_event_projection_matrix_is_canonical(
    remote_spec: ExperimentSpec,
) -> None:
    corpus: list[object] = []
    for index, kind in enumerate(
        [
            "execution.queued",
            "execution.active",
            "execution.verifying",
            "execution.publishing",
            "shard.queued",
            "shard.active",
            "shard.verifying",
            "shard.publishing",
        ],
        2,
    ):
        lock = _lock(remote_spec)
        subject_type: SubjectType = (
            "execution" if kind.startswith("execution.") else "shard"
        )
        subject_id = (
            lock.executions[0].execution_id
            if subject_type == "execution"
            else lock.executions[0].shards[0].shard_id
        )
        event = _event(
            lock,
            index,
            subject_type,
            subject_id,
            cast(EventKind, kind),
            LifecyclePayload(),
        )
        corpus.append(
            project_recovery(lock, [_submitted(lock), event]).model_dump(mode="json")
        )

    assert _hash(corpus) == (
        "31df8baf0f8ea6fe647d18d9a5482d2628aa3a8019d0bdf6cb1d5358d8c4a4ad"
    )


@pytest.mark.parametrize(
    ("subject", "kind", "message"),
    [
        (
            "execution",
            "execution.complete",
            "execution completed with non-complete children",
        ),
        ("shard", "shard.complete", "shard completed with non-complete children"),
        (
            "execution",
            "execution.failed-infrastructure",
            "execution became terminal before its children",
        ),
        (
            "shard",
            "shard.failed-infrastructure",
            "shard became terminal before its children",
        ),
    ],
)
def test_observed_terminal_parent_rejection_matrix_has_exact_errors(
    remote_spec: ExperimentSpec, subject: str, kind: EventKind, message: str
) -> None:
    lock = _lock(remote_spec)
    subject_id = (
        lock.executions[0].execution_id
        if subject == "execution"
        else lock.executions[0].shards[0].shard_id
    )
    sequence = 3 if kind.endswith(".complete") else 2
    event = _event(
        lock,
        sequence,
        cast(SubjectType, subject),
        subject_id,
        kind,
        LifecyclePayload(),
    )

    events = [_submitted(lock)]
    if kind.endswith(".complete"):
        events.append(_trial_event(lock, 2, 0, "trial.invalid"))
    events.append(event)

    with pytest.raises(ValueError) as captured:
        project_recovery(lock, events)

    assert str(captured.value) == message


def test_control_terminal_and_subject_boundary_matrix_is_complete(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _lock(remote_spec)
    submitted = _submitted(lock)
    terminal_projections = []
    for index, kind in enumerate(
        [
            "run.completed",
            "run.partial",
            "run.failed",
            "run.cancelled",
        ],
        2,
    ):
        terminal = _event(
            lock,
            index,
            "run",
            lock.run_id,
            cast(EventKind, kind),
            TerminalPayload(message="terminal"),
        )
        terminal_projections.append(
            project_run(lock, [submitted, terminal]).model_dump(mode="json")
        )
        late = _event(
            lock,
            index + 10,
            "run",
            lock.run_id,
            "run.draining",
            LifecyclePayload(),
        )
        with pytest.raises(ControlError) as captured:
            project_run(lock, [submitted, terminal, late])
        assert str(captured.value) == "run has events after a terminal transition"

    assert _hash(terminal_projections) == (
        "f7719fc4084b4c6bb8a9a6ef9585e1bd6295a52f771ad8b7077beb1902bb5fb3"
    )

    wrong_type = _event(
        lock,
        2,
        "run",
        lock.run_id,
        "run.draining",
        LifecyclePayload(),
    ).model_copy(update={"subject_type": "execution"})
    wrong_id = wrong_type.model_copy(
        update={"subject_type": "run", "subject_id": "wrong"}
    )
    assert project_run(lock, [submitted, wrong_type]).status == "queued"
    duplicate = submitted.model_copy(
        update={"event_id": "evt-" + "2" * 32, "observed_at": NOW + timedelta(1)}
    )
    for event, message in [
        (wrong_id, "run event has the wrong subject"),
        (duplicate, "run has multiple submission events"),
    ]:
        with pytest.raises(ControlError) as captured:
            project_run(lock, [submitted, event])
        assert str(captured.value) == message


def test_action_projection_rejection_matrix_has_exact_errors(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _lock(remote_spec)
    submitted = _submitted(lock)
    reserved = _event(
        lock,
        2,
        "run",
        lock.run_id,
        "action.reserved",
        ActionReservedPayload(
            action_id="action-one",
            action_key="key-one",
            action_kind="submit-wave",
            target_ids=["shard-one"],
        ),
    )
    outcome = _event(
        lock,
        3,
        "run",
        lock.run_id,
        "action.succeeded",
        ActionOutcomePayload(action_id="action-one", remote_id="remote-one"),
    )
    cases = [
        (
            [
                submitted,
                reserved,
                reserved.model_copy(update={"event_id": "evt-duplicate"}),
            ],
            "action was reserved more than once",
        ),
        (
            [submitted, outcome],
            "action outcome has no reservation",
        ),
        (
            [
                submitted,
                reserved,
                outcome,
                outcome.model_copy(
                    update={
                        "event_id": "evt-second-outcome",
                        "kind": "action.failed",
                    }
                ),
            ],
            "action has multiple outcomes",
        ),
    ]

    for events, message in cases:
        with pytest.raises(ControlError) as captured:
            project_run(lock, events)
        assert str(captured.value) == message


def test_event_ordering_deduplication_and_conflicts_are_complete(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _lock(remote_spec)
    first = _submitted(lock).model_copy(
        update={"event_id": "evt-b", "observed_at": NOW}
    )
    second = _event(
        lock,
        2,
        "run",
        lock.run_id,
        "run.draining",
        LifecyclePayload(),
    ).model_copy(update={"event_id": "evt-a", "observed_at": NOW})

    ordered = ordered_events([first, second, first, second])
    assert [event.event_id for event in ordered] == ["evt-a", "evt-b"]
    assert ordered == [second, first]

    conflicting = first.model_copy(update={"producer": "different-producer"})
    with pytest.raises(ControlError) as captured:
        ordered_events([first, conflicting])
    assert str(captured.value) == "event ID has conflicting records"


def test_non_run_events_do_not_hide_later_run_transitions(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _lock(remote_spec)
    ignored = _event(
        lock,
        2,
        "execution",
        lock.executions[0].execution_id,
        "execution.active",
        LifecyclePayload(),
    )
    draining = _event(
        lock,
        3,
        "run",
        lock.run_id,
        "run.draining",
        LifecyclePayload(),
    )
    projection = project_run(lock, [_submitted(lock), ignored, draining])

    assert projection.status == "draining"
    assert projection.event_count == 3
    assert projection.last_observed_at == NOW + timedelta(seconds=3)


def test_repeated_cancellation_preserves_draining_and_manual_states(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _lock(remote_spec)
    submitted = _submitted(lock)
    cancel = _event(
        lock,
        4,
        "run",
        lock.run_id,
        "run.cancel-requested",
        CancellationPayload(reason="operator"),
    )
    states = []
    for kind in [None, "run.draining", "run.manual-intervention-required"]:
        events = [submitted]
        if kind is not None:
            events.append(
                _event(
                    lock,
                    2,
                    "run",
                    lock.run_id,
                    cast(EventKind, kind),
                    LifecyclePayload(),
                )
            )
        events.extend(
            [cancel, cancel.model_copy(update={"event_id": "evt-cancel-two"})]
        )
        states.append(project_run(lock, events).model_dump(mode="json"))

    assert _hash(states) == (
        "bf39a85e885a53fb6764f55abb06bb1739bb31d6c164059e4df2fcc3e292c1d4"
    )
