from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import cast

import pytest
from conftest import with_provider_controller

from harbor_hf import run_finalizer, run_observer
from harbor_hf.control import (
    ActionOutcomePayload,
    ActionReservedPayload,
    AttemptOutcomePayload,
    RunProjection,
    RunSubmittedPayload,
    new_event,
)
from harbor_hf.evidence import verify_checksums
from harbor_hf.executions import ExecutionLock, build_execution_lock
from harbor_hf.models import ExperimentSpec
from harbor_hf.provider_models import ExplicitProviderRoute, ProviderTarget
from harbor_hf.publication_envelope import canonical_digest
from harbor_hf.reconciler import plan_reconciliation
from harbor_hf.recovery import (
    ExecutionProjection,
    ProjectionCounts,
    RecoveryProjection,
    TerminalDecision,
    TrialProjection,
)
from harbor_hf.run_finalizer import (
    BucketRunFinalizer,
    RunFinalizationError,
    ValidatingEvidenceWriter,
)
from harbor_hf.run_observer import (
    BucketRunObserver,
    RunObservationError,
)
from harbor_hf.runs import (
    RunLock,
    WaveLock,
    build_run_lock,
    build_run_plan,
    build_wave_lock,
)
from harbor_hf.wave_worker import AttemptLock

NOW = datetime(2026, 7, 14, 1, 2, 3, tzinfo=UTC)


def _sha(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _pretty(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


class _Reader:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.listed: list[tuple[str, str]] = []
        self.reads: list[tuple[str, str, str]] = []

    def list_files(self, *, bucket: str, prefix: str) -> list[str]:
        self.listed.append((bucket, prefix))
        return sorted(self.files)

    def read_bytes(self, *, bucket: str, prefix: str, path: str) -> bytes:
        self.reads.append((bucket, prefix, path))
        return self.files[path]


class _Writer:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str, bytes]] = []

    def write_immutable(self, *, bucket: str, path: str, content: bytes) -> bool:
        self.writes.append((bucket, path, content))
        return True


def _run(remote_spec: ExperimentSpec) -> RunLock:
    return build_run_lock(
        build_run_plan(remote_spec),
        "run-terminal-mutation",
        clock=lambda: NOW,
    )


def _execution_lock(remote_spec: ExperimentSpec, lock: RunLock) -> ExecutionLock:
    return build_execution_lock(
        remote_spec, execution_id=lock.executions[0].execution_id, clock=lambda: NOW
    )


def _attempt_lock(lock: RunLock, attempt_id: str, physical_attempt: int) -> bytes:
    run = lock.executions[0]
    shard = run.shards[0]
    trial = shard.trials[0]
    return (
        AttemptLock(
            attempt_id=attempt_id,
            created_at=NOW,
            run_id=lock.run_id,
            wave_id="wave-one",
            execution_id=run.execution_id,
            shard_id=shard.shard_id,
            trial_id=trial.trial_id,
            task_name=trial.task_name,
            task_digest=trial.task_digest,
            logical_attempt=trial.logical_attempt,
            physical_attempt=physical_attempt,
            remote_job_id=f"job-{attempt_id}",
        )
        .model_dump_json()
        .encode()
    )


def _attempt_events(started: str, finished_event: str, finished: str) -> bytes:
    lines = [
        json.dumps({"event": "attempt_started", "at": started}),
        json.dumps({"event": finished_event, "at": finished}),
    ]
    return ("\n".join(lines) + "\n").encode()


def _finalizer_files(remote_spec: ExperimentSpec, lock: RunLock) -> dict[str, bytes]:
    run = lock.executions[0]
    trial = run.shards[0].trials[0]
    base = f"executions/{run.execution_id}"
    trial_prefix = f"{base}/trials/{trial.trial_id}"
    one = f"{trial_prefix}/attempts/attempt-one"
    two = f"{trial_prefix}/attempts/attempt-two"
    verification = _pretty({"trials": [{"rewards": {"reward": 1, "speed": 0.5}}]})
    lock_one = _attempt_lock(lock, "attempt-one", 1)
    lock_two = _attempt_lock(lock, "attempt-two", 2)
    events_one = _attempt_events(
        "2026-07-14T01:03:00+00:00", "attempt_failed", "2026-07-14T01:04:00+00:00"
    )
    events_two = _attempt_events(
        "2026-07-14T01:05:00+00:00",
        "attempt_succeeded",
        "2026-07-14T03:06:00+01:00",
    )
    archive = b"deterministic Harbor archive"
    compatibility = b"{}\n"
    native_lock = b'{"kind":"trial-lock"}\n'
    native_result = b'{"kind":"trial-result"}\n'
    bundle = _pretty(
        {
            "schema_version": "harbor-hf/harbor-native-bundle/v1alpha1",
            "contract_status": "compatibility",
            "harbor_revision": "a" * 40,
            "harbor_version": "test",
            "request_digest": "sha256:" + "b" * 64,
            "compatibility_schema": "harbor-hf/harbor-compatibility/v1alpha3",
            "archive": {
                "path": "artifacts.tar.gz",
                "digest": _sha(archive),
                "size_bytes": len(archive),
                "media_type": "application/gzip",
            },
            "compatibility": {
                "path": "harbor-compatibility.json",
                "digest": _sha(compatibility),
                "size_bytes": len(compatibility),
                "media_type": "application/json",
            },
            "documents": [
                {
                    "kind": "trial_lock",
                    "path": "harbor-jobs/job/trial/lock.json",
                    "digest": _sha(native_lock),
                },
                {
                    "kind": "trial_result",
                    "path": "harbor-jobs/job/trial/result.json",
                    "digest": _sha(native_result),
                },
            ],
        }
    )
    manifest = _pretty(
        {
            "artifacts.tar.gz": _sha(archive),
            "attempt.lock.json": _sha(lock_two),
            "events.jsonl": _sha(events_two),
            "harbor-compatibility.json": _sha(compatibility),
            "harbor-jobs/job/trial/lock.json": _sha(native_lock),
            "harbor-jobs/job/trial/result.json": _sha(native_result),
            "harbor-native-bundle.json": _sha(bundle),
            "verification.json": _sha(verification),
        }
    )
    return {
        "run.lock.json": b"{}",
        f"{base}/execution.lock.json": (
            _execution_lock(remote_spec, lock).model_dump_json().encode()
        ),
        f"{trial_prefix}/_SUCCESS": b"\n",
        f"{trial_prefix}/trial-summary.json": _pretty(
            {
                "trial_id": trial.trial_id,
                "attempt_id": "attempt-two",
                "attempt_checksum": _sha(manifest),
            }
        ),
        f"{one}/attempt.lock.json": lock_one,
        f"{one}/events.jsonl": events_one,
        f"{one}/failure.json": _pretty(
            {"category": "transient", "message": "provider timeout"}
        ),
        f"{one}/_FAILED": b"\n",
        f"{two}/attempt.lock.json": lock_two,
        f"{two}/events.jsonl": events_two,
        f"{two}/artifacts.tar.gz": archive,
        f"{two}/harbor-compatibility.json": compatibility,
        f"{two}/harbor-jobs/job/trial/lock.json": native_lock,
        f"{two}/harbor-jobs/job/trial/result.json": native_result,
        f"{two}/harbor-native-bundle.json": bundle,
        f"{two}/verification.json": verification,
        f"{two}/_SUCCESS": b"\n",
        f"{two}/checksums.json": manifest,
    }


def _projection(lock: RunLock, status: str) -> RecoveryProjection:
    run = lock.executions[0]
    trial = run.shards[0].trials[0]
    return RecoveryProjection(
        run=RunProjection(
            run_id=lock.run_id,
            plan_digest=lock.plan_digest,
            status="completed",
            event_count=1,
            last_observed_at=NOW,
            actions={},
        ),
        executions={
            run.execution_id: ExecutionProjection.model_validate(
                {
                    "execution_id": run.execution_id,
                    "deployment_digest": run.deployment_digest,
                    "status": status,
                    "shard_ids": [run.shards[0].shard_id],
                }
            )
        },
        shards={},
        trials={
            trial.trial_id: TrialProjection(
                trial_id=trial.trial_id,
                shard_id=run.shards[0].shard_id,
                logical_attempt=trial.logical_attempt,
                status="complete",
                outcome="scored",
            )
        },
        attempts={},
        waves={},
        spend_microusd=0,
        counts=ProjectionCounts(complete=1),
    )


def _decision(lock: RunLock, status: str) -> TerminalDecision:
    return TerminalDecision.model_validate(
        {
            "status": status,
            "marker": "_SUCCESS" if status == "completed" else "_FAILED",
            "summary_path": f"{lock.artifact_prefix}/run-summary.json",
            "marker_path": f"{lock.artifact_prefix}/_TERMINAL",
            "reason": "terminal",
            "counts": ProjectionCounts(complete=1).model_dump(mode="python"),
        }
    )


def test_finalize_recovers_unique_success_missing_trial_projection(
    remote_spec: ExperimentSpec,
    tmp_path: Path,
) -> None:
    lock = _run(remote_spec)
    run = lock.executions[0]
    trial = run.shards[0].trials[0]
    files = _finalizer_files(remote_spec, lock)
    trial_prefix = f"executions/{run.execution_id}/trials/{trial.trial_id}"
    del files[f"{trial_prefix}/trial-summary.json"]
    del files[f"{trial_prefix}/_SUCCESS"]
    writer = _Writer()

    BucketRunFinalizer(_Reader(files), writer).finalize(
        lock,
        remote_spec,
        _projection(lock, "complete"),
        _decision(lock, "completed"),
    )

    base = f"{lock.artifact_prefix}/{trial_prefix}"
    writes = {path: content for _bucket, path, content in writer.writes}
    recovery_path = f"{base}/trial-finalization-recovery.json"
    summary_path = f"{base}/trial-summary.json"
    marker_path = f"{base}/_SUCCESS"
    lock_path = f"{base}/trial.lock.json"
    checksums_path = f"{base}/checksums.json"
    assert list(writes)[:5] == [
        recovery_path,
        lock_path,
        summary_path,
        checksums_path,
        marker_path,
    ]
    selected_checksums = files[f"{trial_prefix}/attempts/attempt-two/checksums.json"]
    assert json.loads(writes[recovery_path]) == {
        "reason": "interrupted_trial_finalization",
        "schema_version": "harbor-hf/trial-finalization-recovery/v1alpha1",
        "selected_attempt_checksum": _sha(selected_checksums),
        "selected_attempt_id": "attempt-two",
        "trial_id": trial.trial_id,
    }
    assert json.loads(writes[summary_path]) == {
        "attempt_checksum": _sha(selected_checksums),
        "attempt_id": "attempt-two",
        "trial_id": trial.trial_id,
    }
    assert json.loads(writes[lock_path]) == trial.model_dump(mode="json")
    assert writes[marker_path] == b"\n"
    trial_checksums = json.loads(writes[checksums_path])
    assert trial_checksums["trial-finalization-recovery.json"] == _sha(
        writes[recovery_path]
    )
    assert trial_checksums["trial.lock.json"] == _sha(writes[lock_path])
    assert trial_checksums["trial-summary.json"] == _sha(writes[summary_path])

    trial_root = tmp_path / "recovered-trial"
    source_root = f"{trial_prefix}/"
    for path, content in files.items():
        if path.startswith(source_root):
            destination = trial_root / path.removeprefix(source_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    for path, content in writes.items():
        if path.startswith(f"{base}/"):
            destination = trial_root / path.removeprefix(f"{base}/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    verify_checksums(trial_root)

    execution_checksums = json.loads(
        writes[f"{lock.artifact_prefix}/executions/{run.execution_id}/checksums.json"]
    )
    assert execution_checksums[
        f"trials/{trial.trial_id}/trial-finalization-recovery.json"
    ] == _sha(writes[recovery_path])
    assert execution_checksums[f"trials/{trial.trial_id}/trial-summary.json"] == _sha(
        writes[summary_path]
    )


def test_finalize_completes_marker_after_checksum_publication(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    run = lock.executions[0]
    trial = run.shards[0].trials[0]
    files = _finalizer_files(remote_spec, lock)
    trial_prefix = f"executions/{run.execution_id}/trials/{trial.trial_id}"
    del files[f"{trial_prefix}/_SUCCESS"]
    files[f"{trial_prefix}/trial.lock.json"] = _pretty(trial.model_dump(mode="json"))
    trial_root = f"{trial_prefix}/"
    files[f"{trial_prefix}/checksums.json"] = _pretty(
        {
            path.removeprefix(trial_root): _sha(content)
            for path, content in sorted(files.items())
            if path.startswith(trial_root)
        }
    )
    writer = _Writer()

    BucketRunFinalizer(_Reader(files), writer).finalize(
        lock,
        remote_spec,
        _projection(lock, "complete"),
        _decision(lock, "completed"),
    )

    paths = [path for _bucket, path, _content in writer.writes]
    assert paths[0] == f"{lock.artifact_prefix}/{trial_prefix}/_SUCCESS"
    assert not any("trial-finalization-recovery.json" in path for path in paths)


def test_finalize_rejects_success_marker_without_summary(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    run = lock.executions[0]
    trial = run.shards[0].trials[0]
    files = _finalizer_files(remote_spec, lock)
    trial_prefix = f"executions/{run.execution_id}/trials/{trial.trial_id}"
    del files[f"{trial_prefix}/trial-summary.json"]
    writer = _Writer()

    with pytest.raises(RunFinalizationError, match="marker but no summary"):
        BucketRunFinalizer(_Reader(files), writer).finalize(
            lock,
            remote_spec,
            _projection(lock, "complete"),
            _decision(lock, "completed"),
        )

    assert writer.writes == []


def test_finalize_rejects_ambiguous_success_missing_trial_projection(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    run = lock.executions[0]
    trial = run.shards[0].trials[0]
    files = _finalizer_files(remote_spec, lock)
    trial_prefix = f"executions/{run.execution_id}/trials/{trial.trial_id}"
    del files[f"{trial_prefix}/trial-summary.json"]
    del files[f"{trial_prefix}/_SUCCESS"]
    second = f"{trial_prefix}/attempts/attempt-two"
    third = f"{trial_prefix}/attempts/attempt-three"
    for path, content in list(files.items()):
        if path.startswith(f"{second}/"):
            files[path.replace(second, third)] = content
    third_lock = _attempt_lock(lock, "attempt-three", 3)
    files[f"{third}/attempt.lock.json"] = third_lock
    third_checksums = json.loads(files[f"{third}/checksums.json"])
    third_checksums["attempt.lock.json"] = _sha(third_lock)
    files[f"{third}/checksums.json"] = _pretty(third_checksums)

    with pytest.raises(RunFinalizationError, match="ambiguous successful"):
        BucketRunFinalizer(_Reader(files), _Writer()).finalize(
            lock,
            remote_spec,
            _projection(lock, "complete"),
            _decision(lock, "completed"),
        )


def test_finalize_validates_execution_before_recovering_trial_projection(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    run = lock.executions[0]
    trial = run.shards[0].trials[0]
    files = _finalizer_files(remote_spec, lock)
    trial_prefix = f"executions/{run.execution_id}/trials/{trial.trial_id}"
    del files[f"{trial_prefix}/trial-summary.json"]
    del files[f"{trial_prefix}/_SUCCESS"]
    del files[f"{trial_prefix}/attempts/attempt-two/harbor-native-bundle.json"]
    writer = _Writer()

    with pytest.raises(RunFinalizationError, match="no verified Harbor native bundle"):
        BucketRunFinalizer(_Reader(files), writer).finalize(
            lock,
            remote_spec,
            _projection(lock, "complete"),
            _decision(lock, "completed"),
        )

    assert writer.writes == []


def test_finalize_writes_exact_execution_evidence(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    run = lock.executions[0]
    trial = run.shards[0].trials[0]
    files = _finalizer_files(remote_spec, lock)
    reader = _Reader(files)
    writer = _Writer()

    BucketRunFinalizer(reader, writer).finalize(
        lock,
        remote_spec,
        _projection(lock, "complete"),
        _decision(lock, "completed"),
    )

    bucket = remote_spec.artifacts.bucket
    assert reader.listed == [(bucket, lock.artifact_prefix)]
    assert {write[0] for write in writer.writes} == {bucket}
    base = f"{lock.artifact_prefix}/executions/{run.execution_id}"
    assert [write[1] for write in writer.writes] == [
        f"{base}/verification.json",
        f"{base}/execution-summary.json",
        f"{base}/publication-envelope.v1.json",
        f"{base}/checksums.json",
        f"{base}/_SUCCESS",
        f"{lock.artifact_prefix}/run-summary.json",
        f"{lock.artifact_prefix}/_TERMINAL",
    ]
    contents = {write[1]: write[2] for write in writer.writes}
    verification = contents[f"{base}/verification.json"]
    assert verification == _pretty(
        {
            "trial_count": 1,
            "trials": [{"rewards": {"reward": 1, "speed": 0.5}}],
        }
    )
    summary = json.loads(contents[f"{base}/execution-summary.json"])
    assert summary["sanitized"] is True
    assert summary["execution"]["execution_id"] == run.execution_id
    assert summary["execution"]["run_id"] == lock.run_id
    assert summary["execution"]["provider"] == "hf-inference-endpoints"
    assert summary["execution"]["quality"] == "clean"
    assert summary["execution"]["completed_at"] == "2026-07-14T02:06:00Z"
    assert summary["trials"] == [
        {
            "trial_id": trial.trial_id,
            "task_name": trial.task_name,
            "task_digest": trial.task_digest,
            "logical_attempt": 1,
            "selected_attempt_id": "attempt-two",
            "outcome": "scored",
        }
    ]
    assert [
        (
            attempt["attempt_id"],
            attempt["status"],
            attempt["failure_category"],
            attempt["retry_reason"],
            attempt["started_at"],
            attempt["completed_at"],
            attempt["runtime_kind"],
            attempt["physical_attempt"],
            attempt["remote_job_id"],
        )
        for attempt in summary["attempts"]
    ] == [
        (
            "attempt-one",
            "failed",
            "transient",
            None,
            "2026-07-14T01:03:00Z",
            "2026-07-14T01:04:00Z",
            "endpoint",
            1,
            "job-attempt-one",
        ),
        (
            "attempt-two",
            "succeeded",
            None,
            "infrastructure_retry",
            "2026-07-14T01:05:00Z",
            "2026-07-14T02:06:00Z",
            "endpoint",
            2,
            "job-attempt-two",
        ),
    ]
    envelope_bytes = contents[f"{base}/publication-envelope.v1.json"]
    envelope = json.loads(envelope_bytes)
    assert envelope["schema_version"] == "harbor-hf/publication-envelope/v1"
    assert envelope["execution_id"] == run.execution_id
    assert envelope["run_id"] == lock.run_id
    assert envelope["runtime"]["kind"] == "endpoint"
    assert envelope["cleanup_outcome"] == "verified"
    assert [record["attempt_id"] for record in envelope["attempts"]] == [
        "attempt-one",
        "attempt-two",
    ]
    assert envelope["attempts"][0]["harbor_bundle"] is None
    assert envelope["attempts"][1]["harbor_bundle"]["document_count"] == 2
    assert [record["remote_job_id"] for record in envelope["attempts"]] == [
        "job-attempt-one",
        "job-attempt-two",
    ]
    assert "task_name" not in envelope_bytes.decode()
    assert "rewards" not in envelope_bytes.decode()
    assert [
        (metric["owner_id"], metric["name"], metric["value"], metric["unit"])
        for metric in summary["metrics"]
    ] == [
        (trial.trial_id, "reward", 1.0, "score"),
        (trial.trial_id, "speed", 0.5, "score"),
    ]
    execution_lock_bytes = files[f"executions/{run.execution_id}/execution.lock.json"]
    assert [
        (
            artifact["kind"],
            artifact["path"],
            artifact["sha256"],
            artifact["size_bytes"],
        )
        for artifact in summary["artifacts"]
    ] == [
        (
            "execution_lock",
            "execution.lock.json",
            _sha(execution_lock_bytes),
            len(execution_lock_bytes),
        ),
        (
            "verification",
            "verification.json",
            _sha(verification),
            len(verification),
        ),
    ]
    prefix = f"executions/{run.execution_id}/"
    expected_checksums = {
        path.removeprefix(prefix): _sha(content)
        for path, content in files.items()
        if path.startswith(prefix)
    }
    expected_checksums["verification.json"] = _sha(verification)
    expected_checksums["execution-summary.json"] = _sha(
        contents[f"{base}/execution-summary.json"]
    )
    expected_checksums["publication-envelope.v1.json"] = _sha(envelope_bytes)
    checksum_bytes = contents[f"{base}/checksums.json"]
    assert checksum_bytes == _pretty(dict(sorted(expected_checksums.items())))
    assert contents[f"{base}/_SUCCESS"] == b"\n"
    assert contents[f"{lock.artifact_prefix}/_TERMINAL"] == b"\n"
    assert contents[f"{lock.artifact_prefix}/run-summary.json"] == _pretty(
        {
            "schema_version": "harbor-hf/run-summary/v1alpha1",
            "run_id": lock.run_id,
            "status": "completed",
            "reason": "terminal",
            "counts": {
                "planned": 0,
                "active": 0,
                "retrying": 0,
                "complete": 1,
                "invalid": 0,
                "failed": 0,
                "cancelled": 0,
                "physical_retries": 0,
            },
            "execution_checksums": {run.execution_id: _sha(checksum_bytes)},
        }
    )


def test_seal_executions_writes_evidence_without_rewriting_run_summary(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    run = lock.executions[0]
    writer = _Writer()

    checksums = BucketRunFinalizer(
        _Reader(_finalizer_files(remote_spec, lock)), writer
    ).seal_executions(lock, remote_spec, _projection(lock, "complete"))

    base = f"{lock.artifact_prefix}/executions/{run.execution_id}"
    assert [path for _bucket, path, _content in writer.writes] == [
        f"{base}/verification.json",
        f"{base}/execution-summary.json",
        f"{base}/publication-envelope.v1.json",
        f"{base}/checksums.json",
        f"{base}/_SUCCESS",
    ]
    checksum_bytes = next(
        content
        for _bucket, path, content in writer.writes
        if path == f"{base}/checksums.json"
    )
    assert checksums == {run.execution_id: canonical_digest(json.loads(checksum_bytes))}


def test_validating_writer_accepts_identical_writes_without_mutating() -> None:
    reader = _Reader({"existing.json": b"{}\n"})
    writer = ValidatingEvidenceWriter(
        reader,
        bucket="namespace/bucket",
        prefix="runs/example",
    )

    assert not writer.write_immutable(
        bucket="namespace/bucket",
        path="runs/example/existing.json",
        content=b"{}\n",
    )
    assert writer.write_immutable(
        bucket="namespace/bucket",
        path="runs/example/new.json",
        content=b"{}\n",
    )
    assert reader.files == {"existing.json": b"{}\n"}


def test_validating_writer_rejects_immutable_conflict() -> None:
    writer = ValidatingEvidenceWriter(
        _Reader({"existing.json": b"{}\n"}),
        bucket="namespace/bucket",
        prefix="runs/example",
    )

    with pytest.raises(RunFinalizationError, match="conflicts"):
        writer.write_immutable(
            bucket="namespace/bucket",
            path="runs/example/existing.json",
            content=b"different\n",
        )


def test_failed_trial_becomes_zero_score_terminal_evidence(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    run = lock.executions[0]
    trial = run.shards[0].trials[0]
    base = f"executions/{run.execution_id}"
    trial_prefix = f"{base}/trials/{trial.trial_id}"
    files = _finalizer_files(remote_spec, lock)
    for path in list(files):
        if path.startswith(f"{trial_prefix}/attempts/attempt-two/"):
            del files[path]
    del files[f"{trial_prefix}/_SUCCESS"]
    del files[f"{trial_prefix}/trial-summary.json"]
    finalizer = BucketRunFinalizer(_Reader(files), _Writer())

    records = finalizer._failed_trial_records(
        remote_spec,
        lock,
        base,
        run_finalizer._under(sorted(files), base),
        trial,
        "endpoint",
        "invalid",
        "benchmark_failed",
    )

    assert records.trial.outcome == "benchmark_failed"
    assert records.trial.selected_attempt_id == "attempt-one"
    assert records.attempts[0].status == "failed"
    assert records.attempts[0].failure_category == "transient"
    assert [(metric.name, metric.value) for metric in records.metrics] == [
        ("reward", 0.0)
    ]


def test_finalize_preserves_cancelled_attempt_status_and_timestamp(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    run = lock.executions[0]
    trial = run.shards[0].trials[0]
    prefix = (
        f"executions/{run.execution_id}/trials/{trial.trial_id}/attempts/attempt-one"
    )
    files = _finalizer_files(remote_spec, lock)
    del files[f"{prefix}/_FAILED"]
    files[f"{prefix}/_CANCELLED"] = b"\n"
    files[f"{prefix}/events.jsonl"] = _attempt_events(
        "2026-07-14T01:03:00+00:00",
        "attempt_cancelled",
        "2026-07-14T01:04:00+00:00",
    )
    writer = _Writer()

    BucketRunFinalizer(_Reader(files), writer).finalize(
        lock,
        remote_spec,
        _projection(lock, "complete"),
        _decision(lock, "completed"),
    )

    summary_path = (
        f"{lock.artifact_prefix}/executions/{run.execution_id}/execution-summary.json"
    )
    summary_bytes = next(
        content for _, path, content in writer.writes if path == summary_path
    )
    summary = json.loads(summary_bytes)
    cancelled = next(
        attempt
        for attempt in summary["attempts"]
        if attempt["attempt_id"] == "attempt-one"
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["completed_at"] == "2026-07-14T01:04:00Z"


def test_finalize_rejects_success_without_native_bundle(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    run = lock.executions[0]
    trial = run.shards[0].trials[0]
    attempt = (
        f"executions/{run.execution_id}/trials/{trial.trial_id}/attempts/attempt-two"
    )
    files = _finalizer_files(remote_spec, lock)
    del files[f"{attempt}/harbor-native-bundle.json"]
    checksums = json.loads(files[f"{attempt}/checksums.json"])
    del checksums["harbor-native-bundle.json"]
    files[f"{attempt}/checksums.json"] = _pretty(checksums)
    writer = _Writer()

    with pytest.raises(RunFinalizationError, match="no verified Harbor native bundle"):
        BucketRunFinalizer(_Reader(files), writer).finalize(
            lock,
            remote_spec,
            _projection(lock, "complete"),
            _decision(lock, "completed"),
        )

    assert not any(path.endswith("/_SUCCESS") for _, path, _ in writer.writes)


def test_finalize_skips_incomplete_runs_and_guards_completed_runs(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    files = _finalizer_files(remote_spec, lock)

    with pytest.raises(RunFinalizationError) as captured:
        BucketRunFinalizer(_Reader(files), _Writer()).finalize(
            lock,
            remote_spec,
            _projection(lock, "active"),
            _decision(lock, "completed"),
        )
    assert str(captured.value) == (
        "completed run does not have complete execution evidence"
    )

    writer = _Writer()
    BucketRunFinalizer(_Reader(files), writer).finalize(
        lock,
        remote_spec,
        _projection(lock, "active"),
        _decision(lock, "failed"),
    )
    assert [write[1] for write in writer.writes] == [
        f"{lock.artifact_prefix}/run-summary.json",
        f"{lock.artifact_prefix}/_TERMINAL",
    ]
    summary = json.loads(writer.writes[0][2])
    assert summary["status"] == "failed"
    assert summary["execution_checksums"] == {}


def _mutate(files: dict[str, bytes], lock: RunLock, case: str) -> dict[str, bytes]:
    run = lock.executions[0]
    trial = run.shards[0].trials[0]
    base = f"executions/{run.execution_id}"
    trial_prefix = f"{base}/trials/{trial.trial_id}"
    two = f"{trial_prefix}/attempts/attempt-two"
    tampered_run = json.loads(files[f"{base}/execution.lock.json"])
    tampered_run["execution_id"] = "run-imposter"
    tampered_classification = json.loads(files[f"{base}/execution.lock.json"])
    tampered_classification["publication_role"] = "diagnostic"
    tampered_classification["component_kind"] = None
    removals: dict[str, list[str]] = {
        "missing-trial-marker": [f"{trial_prefix}/_SUCCESS"],
        "selected-failed": [f"{two}/_SUCCESS"],
        "no-marker": [f"{two}/_SUCCESS"],
    }
    replacements: dict[str, dict[str, bytes]] = {
        "no-selected-attempt": {
            f"{trial_prefix}/trial-summary.json": _pretty({"attempt_id": 7})
        },
        "selected-missing": {
            f"{trial_prefix}/trial-summary.json": _pretty(
                {"attempt_id": "attempt-three"}
            )
        },
        "selected-failed": {
            f"{two}/_FAILED": b"\n",
            f"{two}/failure.json": _pretty(
                {"category": "benchmark", "message": "verification failed"}
            ),
        },
        "conflicting-markers": {f"{two}/_FAILED": b"\n"},
        "invalid-events": {f"{two}/events.jsonl": b"not-json\n"},
        "missing-finish-event": {
            f"{two}/events.jsonl": (
                json.dumps(
                    {"event": "attempt_started", "at": "2026-07-14T01:05:00+00:00"}
                ).encode()
                + b"\n"
            )
        },
        "naive-timestamp": {
            f"{two}/events.jsonl": _attempt_events(
                "2026-07-14T01:05:00",
                "attempt_succeeded",
                "2026-07-14T01:06:00+00:00",
            )
        },
        "non-string-timestamp": {
            f"{two}/events.jsonl": (
                json.dumps({"event": "attempt_started", "at": 5}).encode() + b"\n"
            )
        },
        "invalid-verification": {
            f"{two}/verification.json": _pretty({"trials": [{}, {}]})
        },
        "non-mapping-verifier": {
            f"{two}/verification.json": _pretty({"trials": ["bad"]})
        },
        "no-rewards": {
            f"{two}/verification.json": _pretty({"trials": [{"rewards": 3}]})
        },
        "bool-reward": {
            f"{two}/verification.json": _pretty(
                {"trials": [{"rewards": {"reward": True}}]}
            )
        },
        "manifest-not-object": {f"{two}/checksums.json": b"[]"},
        "manifest-bad-value": {f"{two}/checksums.json": _pretty({"events.jsonl": 5})},
        "manifest-conflict": {
            f"{trial_prefix}/checksums.json": _pretty(
                {"attempts/attempt-two/events.jsonl": "sha256:different"}
            )
        },
        "manifest-extra-entry": {
            f"{two}/checksums.json": _pretty({"phantom.json": "sha256:" + "0" * 64})
        },
        "identity-mismatch": {
            f"{base}/execution.lock.json": json.dumps(tampered_run).encode()
        },
        "classification-mismatch": {
            f"{base}/execution.lock.json": json.dumps(tampered_classification).encode()
        },
    }
    mutated = dict(files)
    for path in removals.get(case, []):
        del mutated[path]
    mutated.update(replacements.get(case, {}))
    return mutated


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("no-selected-attempt", "trial summary has no selected attempt"),
        ("selected-missing", "trial selected attempt is missing"),
        ("selected-failed", "trial selected attempt is not successful"),
        ("conflicting-markers", "attempt has no exclusive terminal marker"),
        ("no-marker", "attempt has no exclusive terminal marker"),
        ("invalid-events", "attempt event log is invalid"),
        (
            "missing-finish-event",
            "attempt event log omits a required timestamp",
        ),
        ("naive-timestamp", "attempt event log omits a required timestamp"),
        ("non-string-timestamp", "attempt event log omits a required timestamp"),
        ("invalid-verification", "trial verification evidence is invalid"),
        ("non-mapping-verifier", "trial verification record is invalid"),
        ("no-rewards", "trial verification has no rewards"),
        ("bool-reward", "trial reward evidence is invalid"),
        ("manifest-not-object", "child checksum manifest is invalid"),
        ("manifest-bad-value", "child checksum manifest is invalid"),
        ("manifest-conflict", "child checksums conflict"),
        ("manifest-extra-entry", "execution checksums do not cover exact evidence"),
        ("identity-mismatch", "execution evidence does not match run lock"),
        ("classification-mismatch", "execution evidence does not match run lock"),
    ],
)
def test_finalize_rejection_matrix_has_exact_errors(
    remote_spec: ExperimentSpec, case: str, message: str
) -> None:
    lock = _run(remote_spec)
    trial = lock.executions[0].shards[0].trials[0]
    files = _mutate(_finalizer_files(remote_spec, lock), lock, case)

    with pytest.raises(RunFinalizationError) as captured:
        BucketRunFinalizer(_Reader(files), _Writer()).finalize(
            lock,
            remote_spec,
            _projection(lock, "complete"),
            _decision(lock, "completed"),
        )

    assert str(captured.value) == message.format(trial=trial.trial_id)


def _wave(lock: RunLock, spec: ExperimentSpec) -> WaveLock:
    event = new_event(
        subject_type="run",
        subject_id=lock.run_id,
        kind="run.submitted",
        producer="cli",
        payload=RunSubmittedPayload(plan_digest=lock.plan_digest),
        clock=lambda: NOW,
    )
    action = plan_reconciliation(lock, [event], now=NOW)[1].actions[0]
    return build_wave_lock(lock, spec, action)


def _wave_events_lines(*records: dict[str, object]) -> bytes:
    return ("\n".join(json.dumps(record) for record in records) + "\n").encode()


def _observer_files(lock: RunLock, wave: WaveLock) -> dict[str, bytes]:
    trial = lock.executions[0].shards[0].trials[0]
    wave_prefix = f"waves/{wave.wave_id}"
    wave_events = _wave_events_lines(
        {"event": "wave_started", "at": "2026-07-14T01:10:00+00:00"},
        {"event": "endpoint_pause_requested", "at": "2026-07-14T01:20:00+00:00"},
        {"event": "wave_succeeded", "at": "2026-07-14T01:19:00+00:00"},
    )
    wave_lock_bytes = wave.model_dump_json().encode()
    attempt_prefix = (
        f"executions/{lock.executions[0].execution_id}/trials/{trial.trial_id}/attempts"
    )
    success = f"{attempt_prefix}/attempt-one"
    failed = f"{attempt_prefix}/attempt-two"
    lock_one = _attempt_lock_for_wave(lock, wave, trial.trial_id, "attempt-one")
    lock_two = _attempt_lock_for_wave(lock, wave, trial.trial_id, "attempt-two")
    events_one = _attempt_events(
        "2026-07-14T01:11:00+00:00",
        "attempt_succeeded",
        "2026-07-14T01:12:00+00:00",
    )
    events_two = _attempt_events(
        "2026-07-14T01:13:00+00:00",
        "attempt_failed",
        "2026-07-14T01:14:00+00:00",
    )
    verification = b"{}\n"
    failed_marker = _pretty({"category": "transient", "message": "provider exploded"})
    return {
        f"{wave_prefix}/wave.lock.json": wave_lock_bytes,
        f"{wave_prefix}/events.jsonl": wave_events,
        f"{wave_prefix}/wave-summary.json": b"{}\n",
        f"{wave_prefix}/_SUCCESS": b"\n",
        f"{wave_prefix}/checksums.json": _pretty(
            {
                "wave.lock.json": _sha(wave_lock_bytes),
                "events.jsonl": _sha(wave_events),
                "wave-summary.json": _sha(b"{}\n"),
            }
        ),
        f"{success}/attempt.lock.json": lock_one,
        f"{success}/events.jsonl": events_one,
        f"{success}/verification.json": verification,
        f"{success}/_SUCCESS": b"\n",
        f"{success}/checksums.json": _pretty(
            {
                "attempt.lock.json": _sha(lock_one),
                "events.jsonl": _sha(events_one),
                "verification.json": _sha(verification),
            }
        ),
        f"{failed}/attempt.lock.json": lock_two,
        f"{failed}/events.jsonl": events_two,
        f"{failed}/_FAILED": failed_marker,
        f"{failed}/checksums.json": _pretty(
            {
                "attempt.lock.json": _sha(lock_two),
                "events.jsonl": _sha(events_two),
            }
        ),
    }


def _attempt_lock_for_wave(
    lock: RunLock, wave: WaveLock, trial_id: str, attempt_id: str
) -> bytes:
    run = lock.executions[0]
    shard = run.shards[0]
    trial = shard.trials[0]
    return (
        AttemptLock(
            attempt_id=attempt_id,
            created_at=NOW,
            run_id=lock.run_id,
            wave_id=wave.wave_id,
            execution_id=run.execution_id,
            shard_id=shard.shard_id,
            trial_id=trial_id,
            task_name=trial.task_name,
            task_digest=trial.task_digest,
            logical_attempt=1,
            physical_attempt=1,
        )
        .model_dump_json()
        .encode()
    )


def test_observe_projects_exact_wave_and_attempt_events(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    wave = _wave(lock, remote_spec).model_copy(
        update={"estimated_cost_microusd": 123_456}
    )
    trial = lock.executions[0].shards[0].trials[0]
    shard = lock.executions[0].shards[0]
    files = _observer_files(lock, wave)
    reader = _Reader(files)

    events = BucketRunObserver(reader).observe(lock, remote_spec)

    assert reader.listed == [(remote_spec.artifacts.bucket, lock.artifact_prefix)]
    assert [(event.kind, event.subject_id, event.observed_at) for event in events] == [
        ("wave.active", wave.wave_id, datetime(2026, 7, 14, 1, 10, tzinfo=UTC)),
        (
            "attempt.started",
            "attempt-one",
            datetime(2026, 7, 14, 1, 11, tzinfo=UTC),
        ),
        (
            "attempt.completed",
            "attempt-one",
            datetime(2026, 7, 14, 1, 12, tzinfo=UTC),
        ),
        (
            "attempt.started",
            "attempt-two",
            datetime(2026, 7, 14, 1, 13, tzinfo=UTC),
        ),
        (
            "attempt.failed",
            "attempt-two",
            datetime(2026, 7, 14, 1, 14, tzinfo=UTC),
        ),
        ("wave.cleaning", wave.wave_id, datetime(2026, 7, 14, 1, 20, tzinfo=UTC)),
        (
            "wave.closed",
            wave.wave_id,
            datetime(2026, 7, 14, 1, 20, 0, 1, tzinfo=UTC),
        ),
    ]
    active = events[0]
    assert active.subject_type == "wave"
    assert active.producer == "wave-controller"
    expected_id = hashlib.sha256(
        f"{lock.run_id}:{wave.wave_id}:active".encode()
    ).hexdigest()[:32]
    assert active.event_id == f"evt-{expected_id}"
    assert active.payload.model_dump(mode="json") == {
        "deployment_digest": wave.deployment_digest,
        "provider": "hf-inference-endpoints",
        "shard_ids": [shard.shard_id],
        "estimated_cost_microusd": 123_456,
    }
    closed_id = hashlib.sha256(
        f"{lock.run_id}:{wave.wave_id}:closed:_SUCCESS".encode()
    ).hexdigest()[:32]
    closed = next(event for event in events if event.kind == "wave.closed")
    assert closed.event_id == f"evt-{closed_id}"
    assert events[1].payload.model_dump(mode="json") == {
        "trial_id": trial.trial_id,
        "shard_id": shard.shard_id,
        "physical_attempt": 1,
        "wave_id": wave.wave_id,
        "estimated_cost_microusd": 0,
    }
    assert events[2].payload.model_dump(mode="json") == {
        "trial_id": trial.trial_id,
        "physical_attempt": 1,
        "category": None,
        "spend_microusd": 0,
        "retry_after_seconds": None,
        "message": None,
    }
    assert events[4].payload.model_dump(mode="json") == {
        "trial_id": trial.trial_id,
        "physical_attempt": 1,
        "category": "transient",
        "spend_microusd": 0,
        "retry_after_seconds": None,
        "message": "provider exploded",
    }


def test_observer_migrates_legacy_retry_wave_trial_ids_from_attempt_locks(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    wave = _wave(lock, remote_spec)
    files = _observer_files(lock, wave)
    wave_prefix = f"waves/{wave.wave_id}"
    raw = wave.model_dump(mode="json")
    raw["action_kind"] = "retry-shard"
    raw["trial_ids"] = []
    legacy = _pretty(raw)
    files[f"{wave_prefix}/wave.lock.json"] = legacy
    checksums = json.loads(files[f"{wave_prefix}/checksums.json"])
    checksums["wave.lock.json"] = _sha(legacy)
    files[f"{wave_prefix}/checksums.json"] = _pretty(checksums)

    events = BucketRunObserver(_Reader(files)).observe(lock, remote_spec)

    observed = [event for event in events if event.kind == "attempt.completed"]
    assert len(observed) == 1
    payload = observed[0].payload
    assert isinstance(payload, AttemptOutcomePayload)
    assert payload.trial_id == lock.executions[0].shards[0].trials[0].trial_id


def test_observe_skips_non_terminal_units_and_handles_cleanup_failures(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    wave = _wave(lock, remote_spec)
    files = _observer_files(lock, wave)
    wave_prefix = f"waves/{wave.wave_id}"

    pending = dict(files)
    del pending[f"{wave_prefix}/_SUCCESS"]
    pending_events = BucketRunObserver(_Reader(pending)).observe(lock, remote_spec)
    assert not any(event.subject_type == "wave" for event in pending_events)
    assert [event.kind for event in pending_events] == [
        "attempt.started",
        "attempt.completed",
        "attempt.started",
        "attempt.failed",
    ]

    cancelled = dict(files)
    success = (
        f"executions/{lock.executions[0].execution_id}/trials/"
        f"{lock.executions[0].shards[0].trials[0].trial_id}/attempts/attempt-one"
    )
    del cancelled[f"{success}/_SUCCESS"]
    cancelled[f"{success}/_CANCELLED"] = b"\n"
    cancelled_events = _attempt_events(
        "2026-07-14T01:03:00+00:00",
        "attempt_cancelled",
        "2026-07-14T01:04:00+00:00",
    )
    cancelled[f"{success}/events.jsonl"] = cancelled_events
    cancelled_checksums = json.loads(cancelled[f"{success}/checksums.json"])
    cancelled_checksums["events.jsonl"] = _sha(cancelled_events)
    cancelled[f"{success}/checksums.json"] = _pretty(cancelled_checksums)
    events = BucketRunObserver(_Reader(cancelled)).observe(lock, remote_spec)
    assert [event.kind for event in events if event.subject_id == "attempt-one"] == [
        "attempt.started",
        "attempt.cancelled",
    ]

    no_marker = dict(files)
    del no_marker[f"{success}/_SUCCESS"]
    events = BucketRunObserver(_Reader(no_marker)).observe(lock, remote_spec)
    assert "attempt-one" not in {event.subject_id for event in events}

    cleanup = dict(files)
    cleanup[f"{wave_prefix}/events.jsonl"] = _wave_events_lines(
        {"event": "wave_started", "at": "2026-07-14T01:10:00+00:00"},
        {"event": "wave_failed", "at": "2026-07-14T01:19:00+00:00"},
        {"event": "endpoint_cleanup_failed", "at": "2026-07-14T01:21:00+00:00"},
    )
    cleanup[f"{wave_prefix}/checksums.json"] = _pretty(
        {
            "wave.lock.json": _sha(files[f"{wave_prefix}/wave.lock.json"]),
            "events.jsonl": _sha(cleanup[f"{wave_prefix}/events.jsonl"]),
            "wave-summary.json": _sha(b"{}\n"),
        }
    )
    events = BucketRunObserver(_Reader(cleanup)).observe(lock, remote_spec)
    wave_kinds = [event.kind for event in events if event.subject_type == "wave"]
    assert wave_kinds == ["wave.active", "wave.cleanup-failed"]
    cleanup_event = next(
        event for event in events if event.kind == "wave.cleanup-failed"
    )
    assert cleanup_event.observed_at == datetime(2026, 7, 14, 1, 19, tzinfo=UTC)

    skipped = dict(files)
    skipped[f"{wave_prefix}/events.jsonl"] = _wave_events_lines(
        {"event": "wave_started", "at": "2026-07-14T01:10:00+00:00"},
        {"event": "endpoint_cleanup_skipped", "at": "2026-07-14T01:18:00+00:00"},
        {"event": "wave_failed", "at": "2026-07-14T01:19:00+00:00"},
    )
    skipped[f"{wave_prefix}/checksums.json"] = _pretty(
        {
            "wave.lock.json": _sha(files[f"{wave_prefix}/wave.lock.json"]),
            "events.jsonl": _sha(skipped[f"{wave_prefix}/events.jsonl"]),
            "wave-summary.json": _sha(b"{}\n"),
        }
    )
    events = BucketRunObserver(_Reader(skipped)).observe(lock, remote_spec)
    assert [event.kind for event in events if event.subject_type == "wave"] == [
        "wave.active",
        "wave.cleanup-failed",
    ]

    no_pause = dict(files)
    no_pause[f"{wave_prefix}/events.jsonl"] = _wave_events_lines(
        {"event": "wave_started", "at": "2026-07-14T01:10:00+00:00"},
        {"event": "wave_succeeded", "at": "2026-07-14T01:19:00+00:00"},
    )
    no_pause[f"{wave_prefix}/checksums.json"] = _pretty(
        {
            "wave.lock.json": _sha(files[f"{wave_prefix}/wave.lock.json"]),
            "events.jsonl": _sha(no_pause[f"{wave_prefix}/events.jsonl"]),
            "wave-summary.json": _sha(b"{}\n"),
        }
    )
    events = BucketRunObserver(_Reader(no_pause)).observe(lock, remote_spec)
    cleaning = next(event for event in events if event.kind == "wave.cleaning")
    closed = next(event for event in events if event.kind == "wave.closed")
    assert cleaning.observed_at == datetime(2026, 7, 14, 1, 19, tzinfo=UTC)
    assert closed.observed_at == datetime(2026, 7, 14, 1, 19, 0, 1, tzinfo=UTC)


def test_observe_recovers_terminal_execution_without_parent_wave_marker(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    submitted = new_event(
        subject_type="run",
        subject_id=lock.run_id,
        kind="run.submitted",
        producer="cli",
        payload=RunSubmittedPayload(plan_digest=lock.plan_digest),
        clock=lambda: NOW,
    )
    action = plan_reconciliation(lock, [submitted], now=NOW)[1].actions[0]
    wave = build_wave_lock(lock, remote_spec, action)
    files = _observer_files(lock, wave)
    files = {
        path: content
        for path, content in files.items()
        if not path.startswith("waves/") and "attempt-two" not in path
    }

    events = BucketRunObserver(_Reader(files)).observe(lock, remote_spec)

    assert [event.kind for event in events] == [
        "attempt.started",
        "attempt.completed",
    ]
    assert not any(event.kind.startswith("wave.") for event in events)
    reserved = new_event(
        subject_type="run",
        subject_id=lock.run_id,
        kind="action.reserved",
        producer="reconciler",
        payload=ActionReservedPayload(
            action_id=action.action_id,
            action_key=action.action_key,
            action_kind=action.kind,
            target_ids=action.target_ids,
        ),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    succeeded = new_event(
        subject_type="run",
        subject_id=lock.run_id,
        kind="action.succeeded",
        producer="reconciler",
        payload=ActionOutcomePayload(
            action_id=action.action_id,
            remote_id="0123456789abcdef01234567",
        ),
        clock=lambda: NOW + timedelta(seconds=2),
    )
    projection, recovery = plan_reconciliation(
        lock,
        [submitted, reserved, succeeded, *events],
        now=datetime(2026, 7, 14, 1, 30, tzinfo=UTC),
    )
    assert projection.attempts["attempt-one"].status == "completed"
    assert projection.waves == {}
    assert recovery.terminal_decision is None
    assert not any(
        action.kind in {"submit-wave", "retry-shard"} for action in recovery.actions
    )


def test_orphan_terminal_attempt_identity_still_fails_closed(
    remote_spec: ExperimentSpec,
) -> None:
    lock = _run(remote_spec)
    wave = _wave(lock, remote_spec)
    files = _observer_files(lock, wave)
    attempt_path = next(
        path for path in files if path.endswith("/attempt-one/attempt.lock.json")
    )
    attempt_prefix = str(PurePosixPath(attempt_path).parent)
    foreign = json.loads(files[attempt_path])
    foreign["run_id"] = "run-foreign"
    foreign_bytes = json.dumps(foreign).encode()
    files[attempt_path] = foreign_bytes
    files[f"{attempt_prefix}/checksums.json"] = _pretty(
        {
            "attempt.lock.json": _sha(foreign_bytes),
            "events.jsonl": _sha(files[f"{attempt_prefix}/events.jsonl"]),
            "verification.json": _sha(files[f"{attempt_prefix}/verification.json"]),
        }
    )
    files = {
        path: content
        for path, content in files.items()
        if not path.startswith("waves/") and "attempt-two" not in path
    }

    with pytest.raises(
        RunObservationError,
        match="^execution evidence does not match run lock$",
    ):
        BucketRunObserver(_Reader(files)).observe(lock, remote_spec)


def test_provider_wave_without_pause_events_projects_cleaning_before_closed(
    remote_spec: ExperimentSpec,
) -> None:
    model = remote_spec.matrix.models[0]
    provider = ProviderTarget(
        id="provider-one",
        model=model.repo,
        routing=ExplicitProviderRoute(provider="groq"),
    )
    agent = remote_spec.matrix.agents[0].model_copy(
        update={
            "import_path": "harbor_hf_agents.openclaw.agent:OpenClawAgent",
            "parameters": {"openclaw_config": {}},
        }
    )
    spec = remote_spec.model_copy(
        update={
            "matrix": remote_spec.matrix.model_copy(
                update={"deployments": [provider], "agents": [agent]}
            )
        }
    )
    spec = with_provider_controller(spec)
    lock = _run(spec)
    submitted = new_event(
        subject_type="run",
        subject_id=lock.run_id,
        kind="run.submitted",
        producer="cli",
        payload=RunSubmittedPayload(plan_digest=lock.plan_digest),
        clock=lambda: NOW,
    )
    action = plan_reconciliation(lock, [submitted], now=NOW)[1].actions[0]
    wave = build_wave_lock(lock, spec, action)
    files = _observer_files(lock, wave)
    wave_prefix = f"waves/{wave.wave_id}"
    events_body = _wave_events_lines(
        {"event": "wave_started", "at": "2026-07-14T01:10:00+00:00"},
        {"event": "wave_succeeded", "at": "2026-07-14T01:19:00+00:00"},
    )
    files[f"{wave_prefix}/events.jsonl"] = events_body
    files[f"{wave_prefix}/checksums.json"] = _pretty(
        {
            "wave.lock.json": _sha(files[f"{wave_prefix}/wave.lock.json"]),
            "events.jsonl": _sha(events_body),
            "wave-summary.json": _sha(b"{}\n"),
        }
    )
    files = {
        path: content for path, content in files.items() if "/attempts/" not in path
    }

    observed = BucketRunObserver(_Reader(files)).observe(lock, spec)
    wave_events = [event for event in observed if event.subject_type == "wave"]

    assert [event.kind for event in wave_events] == [
        "wave.active",
        "wave.cleaning",
        "wave.closed",
    ]
    assert wave_events[1].observed_at < wave_events[2].observed_at
    projection, _plan = plan_reconciliation(
        lock, [submitted, *wave_events], now=wave_events[-1].observed_at
    )
    assert projection.waves[wave.wave_id].status == "closed"


def _mutate_observer(
    files: dict[str, bytes], wave: WaveLock, case: str
) -> dict[str, bytes]:
    wave_prefix = f"waves/{wave.wave_id}"
    execution = next(
        path for path in files if path.endswith("/attempt-one/attempt.lock.json")
    )
    success = str(PurePosixPath(execution).parent)
    foreign = json.loads(files[f"{wave_prefix}/wave.lock.json"])
    foreign["run_id"] = "run-foreign"
    wave_event_bodies: dict[str, bytes] = {
        "invalid-events": b'["not", "an", "object"]\n',
        "invalid-timestamp": _wave_events_lines(
            {"event": "wave_started", "at": "not-a-time"}
        ),
        "naive-timestamp": _wave_events_lines(
            {"event": "wave_started", "at": "2026-07-14T01:10:00"}
        ),
        "missing-wave-start": _wave_events_lines(
            {"event": "wave_succeeded", "at": "2026-07-14T01:19:00+00:00"}
        ),
    }
    replacements: dict[str, dict[str, bytes]] = {
        "foreign-run": {f"{wave_prefix}/wave.lock.json": json.dumps(foreign).encode()},
        "manifest-not-json": {f"{wave_prefix}/checksums.json": b"not-json"},
        "manifest-bad-digest": {
            f"{wave_prefix}/checksums.json": _pretty(
                {
                    "wave.lock.json": "md5:nope",
                    "events.jsonl": "md5:nope",
                    "wave-summary.json": "md5:nope",
                }
            )
        },
        "manifest-incomplete": {
            f"{wave_prefix}/checksums.json": _pretty(
                {"wave.lock.json": _sha(files[f"{wave_prefix}/wave.lock.json"])}
            )
        },
        "checksum-mismatch": {f"{wave_prefix}/wave-summary.json": b"{ }\n"},
        "conflicting-markers": {f"{wave_prefix}/_FAILED": b"\n"},
        "empty-events": {
            f"{success}/events.jsonl": b"\n",
            f"{success}/checksums.json": _pretty(
                {
                    "attempt.lock.json": _sha(files[f"{success}/attempt.lock.json"]),
                    "events.jsonl": _sha(b"\n"),
                    "verification.json": _sha(b"{}\n"),
                }
            ),
        },
    }
    for name, body in wave_event_bodies.items():
        replacements[name] = {
            f"{wave_prefix}/events.jsonl": body,
            f"{wave_prefix}/checksums.json": _pretty(
                {
                    "wave.lock.json": _sha(files[f"{wave_prefix}/wave.lock.json"]),
                    "events.jsonl": _sha(body),
                    "wave-summary.json": _sha(b"{}\n"),
                }
            ),
        }
    mutated = dict(files)
    if case == "missing-manifest":
        del mutated[f"{wave_prefix}/checksums.json"]
    mutated.update(replacements.get(case, {}))
    return mutated


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("foreign-run", "wave evidence belongs to another run"),
        ("missing-manifest", "terminal evidence has no checksum manifest"),
        ("manifest-not-json", "terminal checksum manifest is invalid"),
        ("manifest-bad-digest", "terminal checksum manifest is invalid"),
        ("manifest-incomplete", "terminal checksum manifest is incomplete"),
        (
            "checksum-mismatch",
            "terminal evidence checksum mismatch: waves/{wave_id}/wave-summary.json",
        ),
        ("conflicting-markers", "terminal evidence has conflicting markers"),
        ("empty-events", "lifecycle event log is empty"),
        ("invalid-events", "lifecycle event log is invalid"),
        ("invalid-timestamp", "lifecycle event timestamp is invalid"),
        ("naive-timestamp", "lifecycle event timestamp has no timezone"),
        (
            "missing-wave-start",
            "lifecycle event log omits required events: wave_started",
        ),
    ],
)
def test_observe_rejection_matrix_has_exact_errors(
    remote_spec: ExperimentSpec, case: str, message: str
) -> None:
    lock = _run(remote_spec)
    wave = _wave(lock, remote_spec)
    files = _mutate_observer(_observer_files(lock, wave), wave, case)

    with pytest.raises(RunObservationError) as captured:
        BucketRunObserver(_Reader(files)).observe(lock, remote_spec)

    assert str(captured.value) == message.format(wave_id=wave.wave_id)


def test_path_and_marker_helpers_filter_exactly() -> None:
    paths = [
        "waves/w2/wave.lock.json",
        "waves/w1/wave.lock.json",
        "waves/w1/nested/wave.lock.json",
        "executions/w1/wave.lock.json",
        "waves/w1/other.json",
        "executions/r/trials/t/attempts/e/attempt.lock.json",
        "executions/r/trials/t/attempt.lock.json",
        "executions/r/trials/t/attempts/e/_SUCCESS",
    ]
    assert run_observer._wave_lock_paths(paths) == [
        "waves/w1/wave.lock.json",
        "waves/w2/wave.lock.json",
    ]
    assert run_observer._attempt_lock_paths(paths) == [
        "executions/r/trials/t/attempts/e/attempt.lock.json"
    ]
    assert run_observer._terminal_marker(paths, "waves/w1") is None
    assert (
        run_observer._terminal_marker(paths, "executions/r/trials/t/attempts/e")
        == "_SUCCESS"
    )
    assert run_finalizer._under(
        [
            "executions/r/b.json",
            "executions/r/a.json",
            "executions/other/c.json",
            "executions/rx.json",
        ],
        "executions/r",
    ) == ["a.json", "b.json"]
    assert (
        run_finalizer._marker(
            ["unit/_FAILED", "unit/data.json", "other/_SUCCESS"], "unit"
        )
        == "_FAILED"
    )
    with pytest.raises(RunFinalizationError) as captured:
        run_finalizer._marker(["unit/data.json"], "unit")
    assert str(captured.value) == "attempt has no exclusive terminal marker"


def test_serialization_helpers_are_byte_exact() -> None:
    assert run_finalizer._json_bytes({"b": 1, "a": [2]}) == (
        b'{\n  "a": [\n    2\n  ],\n  "b": 1\n}\n'
    )
    assert run_finalizer._sha256(b"harbor") == (
        "sha256:" + hashlib.sha256(b"harbor").hexdigest()
    )
    artifact = run_finalizer._artifact(
        "execution-one", "execution_lock", "execution.lock.json", b"abc"
    )
    assert artifact.model_dump(mode="json") == {
        "owner_type": "execution",
        "owner_id": "execution-one",
        "kind": "execution_lock",
        "path": "execution.lock.json",
        "sha256": _sha(b"abc"),
        "media_type": "application/json",
        "size_bytes": 3,
    }


def test_event_time_helpers_convert_and_reject_exactly() -> None:
    records: list[dict[str, object]] = [
        {"event": "other", "at": "2026-07-14T09:00:00+00:00"},
        {"event": "wave_started", "at": "2026-07-14T09:00:00+05:00"},
    ]
    observed = run_observer._event_time(records, "wave_started")
    assert observed == datetime(2026, 7, 14, 4, 0, tzinfo=UTC)
    assert observed.tzinfo == UTC
    assert run_observer._optional_event_time(records, "missing") is None
    assert run_observer._optional_event_time(records, "wave_started") == datetime(
        2026, 7, 14, 4, 0, tzinfo=UTC
    )
    with pytest.raises(RunObservationError) as captured:
        run_observer._event_time(records, "wave_failed", "wave_succeeded")
    assert str(captured.value) == (
        "lifecycle event log omits required events: wave_failed, wave_succeeded"
    )
    with pytest.raises(RunObservationError) as captured:
        run_observer._event_time([{"event": "wave_started"}], "wave_started")
    assert str(captured.value) == (
        "lifecycle event log omits required events: wave_started"
    )

    finalizer_records = cast(
        list[dict[str, object]],
        [{"event": "attempt_started", "at": "2026-07-14T09:00:00+05:00"}],
    )
    assert run_finalizer._event_time(finalizer_records, "attempt_started") == datetime(
        2026, 7, 14, 4, 0, tzinfo=UTC
    )
    assert run_finalizer._json_lines(b' \n{"a": 1}\n\n{"b": 2}') == [
        {"a": 1},
        {"b": 2},
    ]
    assert run_observer._json_lines(b'{"a": 1}\n \n{"b": 2}\n') == [
        {"a": 1},
        {"b": 2},
    ]
