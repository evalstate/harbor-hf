from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
import yaml
from huggingface_hub import CommitOperationAdd

from harbor_hf.control import (
    AttemptOutcomePayload,
    AttemptStartedPayload,
    LifecyclePayload,
    ManualInterventionResolutionPayload,
    RunEvent,
    RunSnapshot,
    RunSubmittedPayload,
    WaveLifecyclePayload,
    new_event,
)
from harbor_hf.io import ManifestError
from harbor_hf.models import ExperimentSpec
from harbor_hf.operations import (
    AutomaticRunPublisher,
    cancel_run,
    publish_run_results,
    resume_run,
    retry_run_shard,
    verify_run_artifacts,
)
from harbor_hf.publication_correction import (
    PublicationCorrection,
    publication_correction_digest,
)
from harbor_hf.reconciler import plan_reconciliation
from harbor_hf.recovery import (
    durable_manual_intervention_resolution_event,
    project_recovery,
)
from harbor_hf.result_publisher import PublicationResult
from harbor_hf.results import ResultPublication, ResultPublicationError
from harbor_hf.runs import build_run_lock, build_run_plan


class MemoryStore:
    def __init__(self, snapshot: RunSnapshot) -> None:
        self.snapshot = snapshot

    def load_snapshot(self, run_id: str) -> RunSnapshot:
        assert run_id == self.snapshot.lock.run_id
        return self.snapshot

    def ensure_event(self, run_id: str, event: RunEvent) -> bool:
        assert run_id == self.snapshot.lock.run_id
        if event in self.snapshot.events:
            return False
        self.snapshot.events.append(event)
        return True


class MemoryEvidence:
    def __init__(
        self,
        prefix: str,
        files: dict[str, bytes],
        *,
        interactions: list[object] | None = None,
    ) -> None:
        self.prefix = prefix
        self.files = files
        self.interactions = interactions
        self.refresh_calls = 0

    def refresh(self) -> None:
        self.refresh_calls += 1
        if self.interactions is not None:
            self.interactions.append("refresh")

    def list_files(self, *, bucket: str, prefix: str) -> list[str]:
        assert bucket == "example/benchmark-runs"
        assert prefix == self.prefix
        return list(reversed(self.files))

    def read_bytes(self, *, bucket: str, prefix: str, path: str) -> bytes:
        assert bucket == "example/benchmark-runs"
        assert prefix == self.prefix
        return self.files[path]


class FakePublisher:
    def __init__(self, *, interactions: list[object] | None = None) -> None:
        self.publications: list[ResultPublication] = []
        self.interactions = interactions

    def publish(
        self,
        publication: ResultPublication,
        *,
        result_dataset: str,
        index_dataset: str,
    ) -> PublicationResult:
        assert result_dataset == "example/shellbench-results"
        assert index_dataset == "example/benchmark-run-index"
        if self.interactions is not None:
            self.interactions.append(
                ("publish", result_dataset, index_dataset, publication)
            )
        self.publications.append(publication)
        return PublicationResult(
            publication_id=publication.tables.publication_id,
            result_dataset=result_dataset,
            result_revision="a" * 40,
            index_dataset=index_dataset,
            index_revision="b" * 40,
        )


class MemoryRepositories:
    def __init__(
        self,
        interactions: list[object],
        *,
        existing: dict[str, bool] | None = None,
    ) -> None:
        self.interactions = interactions
        self.private = dict(existing or {})
        self.sha = {repository: "1" * 40 for repository in self.private}
        self.commits: list[tuple[str, list[object], dict[str, object]]] = []

    def create_repo(self, repo_id: str, **kwargs: object) -> object:
        self.interactions.append(("create_repo", repo_id, kwargs))
        requested_private = kwargs.get("private")
        assert isinstance(requested_private, bool)
        self.private.setdefault(repo_id, requested_private)
        return object()

    def repo_info(self, repo_id: str, **kwargs: object) -> object:
        self.interactions.append(("repo_info", repo_id, kwargs))
        return SimpleNamespace(
            private=self.private[repo_id],
            sha=self.sha.get(repo_id),
        )

    def create_commit(
        self, repo_id: str, operations: list[object], **kwargs: object
    ) -> object:
        self.interactions.append(("create_commit", repo_id, kwargs))
        self.commits.append((repo_id, operations, kwargs))
        self.sha[repo_id] = "2" * 40
        return SimpleNamespace(oid=self.sha[repo_id])


def _snapshot(spec: ExperimentSpec) -> RunSnapshot:
    lock = build_run_lock(build_run_plan(spec), "run-one")
    submitted = new_event(
        subject_type="run",
        subject_id=lock.run_id,
        kind="run.submitted",
        producer="cli",
        payload=RunSubmittedPayload(plan_digest=lock.plan_digest),
        clock=lambda: lock.created_at - timedelta(seconds=3),
        identifier=lambda: "1" * 32,
    )
    request = yaml.safe_dump(spec.model_dump(mode="json", exclude_none=True)).encode()
    return RunSnapshot(
        lock=lock,
        events=[submitted],
        request=request,
        control_commit="c" * 40,
    )


def _legacy_publication_snapshot(spec: ExperimentSpec) -> RunSnapshot:
    snapshot = _snapshot(spec)
    request = yaml.safe_load(snapshot.request)
    del request["publishing"]["dataset_visibility"]
    del request["publishing"]["index_dataset_visibility"]
    return RunSnapshot(
        lock=snapshot.lock,
        events=snapshot.events,
        request=yaml.safe_dump(request).encode(),
        control_commit=snapshot.control_commit,
    )


def _publication_correction(
    snapshot: RunSnapshot,
    *,
    result_visibility: str = "private",
    index_visibility: str = "private",
) -> PublicationCorrection:
    return PublicationCorrection.model_validate(
        {
            "run_id": snapshot.lock.run_id,
            "source_manifest_digest": snapshot.lock.manifest_digest,
            "source_plan_digest": snapshot.lock.plan_digest,
            "result_dataset": "example/shellbench-results",
            "result_dataset_visibility": result_visibility,
            "index_dataset": "example/benchmark-run-index",
            "index_dataset_visibility": index_visibility,
        }
    )


def _retry_snapshot(spec: ExperimentSpec) -> RunSnapshot:
    snapshot = _snapshot(spec)
    shard = snapshot.lock.executions[0].shards[0]
    trial = shard.trials[0]
    started = new_event(
        subject_type="attempt",
        subject_id="attempt-one",
        kind="attempt.started",
        producer="wave-controller",
        payload=AttemptStartedPayload(
            trial_id=trial.trial_id,
            shard_id=shard.shard_id,
            physical_attempt=1,
        ),
        clock=lambda: snapshot.lock.created_at - timedelta(seconds=2),
        identifier=lambda: "2" * 32,
    )
    failed = new_event(
        subject_type="attempt",
        subject_id="attempt-one",
        kind="attempt.failed",
        producer="wave-controller",
        payload=AttemptOutcomePayload(
            trial_id=trial.trial_id,
            physical_attempt=1,
            category="transient",
        ),
        clock=lambda: snapshot.lock.created_at - timedelta(seconds=1),
        identifier=lambda: "3" * 32,
    )
    snapshot.events.extend([started, failed])
    return snapshot


def _evidence(snapshot: RunSnapshot) -> MemoryEvidence:
    run = snapshot.lock.executions[0]
    trial = run.shards[0].trials[0]
    created = snapshot.lock.created_at
    summary = {
        "schema_version": "harbor-hf/result-evidence/v1",
        "sanitized": True,
        "execution": {
            "execution_id": run.execution_id,
            "run_id": snapshot.lock.run_id,
            "experiment": "experiment",
            "evaluation_id": snapshot.lock.evaluation_id,
            "publication_role": snapshot.lock.publication_role,
            "component_kind": snapshot.lock.component_kind,
            "benchmark": "shellbench",
            "benchmark_revision": "sha256:" + "1" * 64,
            "result_kind": "ordinary",
            "outcome": "complete",
            "quality": "clean",
            "created_at": created.isoformat(),
            "completed_at": (created + timedelta(minutes=1)).isoformat(),
            "model_id": "model-one",
            "model_repo": "org/model",
            "model_revision": "a" * 40,
            "deployment_id": "deployment-one",
            "provider": "huggingface",
            "region": "aws-us-east-1",
            "hardware": "cpu-basic",
            "accelerator_count": 0,
            "agent_id": "agent-one",
            "agent_name": "agent",
            "agent_revision": "1.0.0",
        },
        "trials": [
            {
                "trial_id": trial.trial_id,
                "task_name": trial.task_name,
                "task_digest": trial.task_digest,
                "logical_attempt": trial.logical_attempt,
                "selected_attempt_id": "attempt-one",
                "outcome": "scored",
            }
        ],
        "attempts": [
            {
                "attempt_id": "attempt-one",
                "trial_id": trial.trial_id,
                "physical_attempt": 1,
                "runtime_kind": "endpoint",
                "status": "succeeded",
                "failure_category": None,
                "started_at": created.isoformat(),
                "completed_at": (created + timedelta(minutes=1)).isoformat(),
                "retry_reason": None,
                "remote_job_id": "job-one",
            }
        ],
        "metrics": [
            {
                "owner_type": "trial",
                "owner_id": trial.trial_id,
                "name": "reward",
                "value": 0.0,
                "unit": "score",
                "aggregation": None,
            }
        ],
        "artifacts": [],
    }
    files = {
        "execution.lock.json": json.dumps(
            {
                "execution_id": run.execution_id,
                "evaluation_id": snapshot.lock.evaluation_id,
                "publication_role": snapshot.lock.publication_role,
                "component_kind": snapshot.lock.component_kind,
                "attempts": 1,
                "model": {
                    "id": "model-one",
                    "repo": "org/model",
                    "revision": "a" * 40,
                    "weights": {"format": "safetensors"},
                },
                "deployment": {
                    "id": "deployment-one",
                    "provider": "hf-inference-endpoints",
                    "hardware": "cpu-basic",
                    "accelerator_count": 1,
                    "region": "aws-us-east-1",
                    "engine": {"name": "test", "image": "test:latest"},
                },
                "agent": {
                    "id": "agent-one",
                    "name": "agent",
                    "revision": "1.0.0",
                    "revision_kind": "package",
                },
                "benchmark_task_digests": {
                    trial.task_name: trial.task_digest,
                },
            }
        ).encode(),
        "execution-summary.json": json.dumps(summary).encode(),
    }
    attempt_prefix = f"trials/{trial.trial_id}/attempts/attempt-one"
    manifest_path = f"{attempt_prefix}/harbor-native-bundle.json"
    archive_path = f"{attempt_prefix}/artifacts.tar.gz"
    files[manifest_path] = b"native bundle manifest"
    files[archive_path] = b"native bundle archive"
    prefix = f"{snapshot.lock.artifact_prefix}/executions/{run.execution_id}"
    execution_lock = files["execution.lock.json"]
    files["publication-envelope.v1.json"] = json.dumps(
        {
            "schema_version": "harbor-hf/publication-envelope/v1",
            "execution_id": run.execution_id,
            "run_id": snapshot.lock.run_id,
            "created_at": created.isoformat(),
            "completed_at": (created + timedelta(minutes=1)).isoformat(),
            "evidence_bucket": "example/benchmark-runs",
            "evidence_prefix": prefix,
            "execution_lock": {
                "path": "execution.lock.json",
                "digest": f"sha256:{hashlib.sha256(execution_lock).hexdigest()}",
                "size_bytes": len(execution_lock),
            },
            "profiles": {
                "experiment": "sha256:" + "1" * 64,
                "model": "sha256:" + "2" * 64,
                "deployment": "sha256:" + "3" * 64,
                "agent": "sha256:" + "4" * 64,
            },
            "runtime": {
                "kind": "endpoint",
                "provider": "huggingface",
                "region": "aws-us-east-1",
                "hardware": "cpu-basic",
                "accelerator_count": 0,
            },
            "sanitizer_version": "harbor-hf/public-results/v1",
            "projection_version": "harbor-hf/results-projection/v1",
            "cleanup_outcome": "verified",
            "attempts": [
                {
                    "attempt_id": "attempt-one",
                    "trial_id": trial.trial_id,
                    "physical_attempt": 1,
                    "status": "succeeded",
                    "failure_category": None,
                    "started_at": created.isoformat(),
                    "completed_at": (created + timedelta(minutes=1)).isoformat(),
                    "retry_reason": None,
                    "remote_job_id": "job-one",
                    "bundle_status": "verified",
                    "harbor_bundle": {
                        "manifest": {
                            "path": manifest_path,
                            "digest": "sha256:"
                            + hashlib.sha256(files[manifest_path]).hexdigest(),
                            "size_bytes": len(files[manifest_path]),
                        },
                        "archive": {
                            "path": archive_path,
                            "digest": "sha256:"
                            + hashlib.sha256(files[archive_path]).hexdigest(),
                            "size_bytes": len(files[archive_path]),
                        },
                        "harbor_revision": "a" * 40,
                        "harbor_version": "0.1.0",
                        "compatibility_schema": (
                            "harbor-hf/harbor-compatibility/v1alpha3"
                        ),
                        "request_digest": "sha256:" + "5" * 64,
                        "document_count": 2,
                    },
                }
            ],
        }
    ).encode()
    checksums = {
        path: f"sha256:{hashlib.sha256(content).hexdigest()}"
        for path, content in files.items()
    }
    files["checksums.json"] = json.dumps(checksums).encode()
    files["_SUCCESS"] = b""
    return MemoryEvidence(prefix, files)


def test_cancel_is_durable_idempotent_and_supports_dry_run(
    remote_spec: ExperimentSpec,
) -> None:
    store = MemoryStore(_snapshot(remote_spec))

    first = cancel_run(store, "run-one", reason="stop", dry_run=False)
    repeated = cancel_run(store, "run-one", reason="different", dry_run=False)
    dry_store = MemoryStore(_snapshot(remote_spec))
    dry = cancel_run(dry_store, "run-one", reason="stop", dry_run=True)

    assert first.recorded
    assert not repeated.recorded
    assert repeated.event_id == first.event_id
    assert not dry.recorded
    assert len(dry_store.snapshot.events) == 1


def test_retry_makes_backoff_ready_and_is_idempotent(
    remote_spec: ExperimentSpec,
) -> None:
    store = MemoryStore(_retry_snapshot(remote_spec))
    shard_id = store.snapshot.lock.executions[0].shards[0].shard_id

    first = retry_run_shard(
        store,
        "run-one",
        shard_id=shard_id,
        reason="retry now",
        dry_run=False,
    )
    repeated = retry_run_shard(
        store,
        "run-one",
        shard_id=shard_id,
        reason="retry again",
        dry_run=False,
    )
    _projection, plan = plan_reconciliation(store.snapshot.lock, store.snapshot.events)

    assert first.recorded
    assert not repeated.recorded
    assert repeated.event_id == first.event_id
    assert [action.kind for action in plan.actions] == ["retry-shard"]


def test_retry_rejects_nonretryable_shard(remote_spec: ExperimentSpec) -> None:
    store = MemoryStore(_snapshot(remote_spec))
    shard_id = store.snapshot.lock.executions[0].shards[0].shard_id

    with pytest.raises(ValueError, match="no retryable"):
        retry_run_shard(
            store,
            "run-one",
            shard_id=shard_id,
            reason="retry",
            dry_run=False,
        )


def test_resume_requires_verified_cleanup_and_records_resolution(
    remote_spec: ExperimentSpec,
) -> None:
    snapshot = _snapshot(remote_spec)
    shard = snapshot.lock.executions[0].shards[0]
    snapshot.events.extend(
        [
            new_event(
                subject_type="wave",
                subject_id="wave-one",
                kind="wave.cleanup-failed",
                producer="watchdog",
                payload=WaveLifecyclePayload(
                    deployment_digest=snapshot.lock.executions[0].deployment_digest,
                    provider="hf-inference-endpoints",
                    shard_ids=[shard.shard_id],
                ),
                clock=lambda: snapshot.lock.created_at - timedelta(seconds=1),
                identifier=lambda: "3" * 32,
            ),
            new_event(
                subject_type="run",
                subject_id=snapshot.lock.run_id,
                kind="run.manual-intervention-required",
                producer="reconciler",
                payload=LifecyclePayload(
                    parent_id="wave-one", message="cleanup failed"
                ),
                clock=lambda: snapshot.lock.created_at,
                identifier=lambda: "4" * 32,
            ),
        ]
    )
    store = MemoryStore(snapshot)

    with pytest.raises(ValueError, match="requires verified endpoint cleanup"):
        resume_run(
            store,
            "run-one",
            reason="not checked",
            cleanup_verified=False,
            dry_run=False,
        )
    result = resume_run(
        store,
        "run-one",
        reason="verified paused",
        cleanup_verified=True,
        dry_run=False,
    )
    repeated = resume_run(
        store,
        "run-one",
        reason="already verified",
        cleanup_verified=True,
        dry_run=False,
    )

    assert result.recorded
    assert not repeated.recorded
    assert repeated.event_id == result.event_id
    assert result.kind == "run.manual-intervention-resolved"
    projection = project_recovery(snapshot.lock, snapshot.events)
    assert projection.status == "active"
    assert projection.waves["wave-one"].status == "closed"


def test_resume_acknowledges_every_failed_cleanup_wave(
    remote_spec: ExperimentSpec,
) -> None:
    snapshot = _snapshot(remote_spec)
    shard = snapshot.lock.executions[0].shards[0]
    wave_payload = WaveLifecyclePayload(
        deployment_digest=snapshot.lock.executions[0].deployment_digest,
        provider="hf-inference-endpoints",
        shard_ids=[shard.shard_id],
    )
    for index, wave_id in enumerate(["wave-one", "wave-two"], start=1):
        snapshot.events.extend(
            [
                new_event(
                    subject_type="wave",
                    subject_id=wave_id,
                    kind="wave.cleanup-failed",
                    producer="watchdog",
                    payload=wave_payload,
                    clock=lambda index=index: (
                        snapshot.lock.created_at + timedelta(seconds=index * 2)
                    ),
                    identifier=lambda index=index: f"{index * 2:032x}",
                ),
                new_event(
                    subject_type="run",
                    subject_id=snapshot.lock.run_id,
                    kind="run.manual-intervention-required",
                    producer="reconciler",
                    payload=LifecyclePayload(parent_id=wave_id),
                    clock=lambda index=index: (
                        snapshot.lock.created_at + timedelta(seconds=index * 2 + 1)
                    ),
                    identifier=lambda index=index: f"{index * 2 + 1:032x}",
                ),
            ]
        )
    store = MemoryStore(snapshot)

    result = resume_run(
        store,
        "run-one",
        reason="all endpoints verified paused",
        cleanup_verified=True,
        dry_run=False,
    )

    resolution = next(
        event for event in snapshot.events if event.event_id == result.event_id
    )
    assert isinstance(resolution.payload, ManualInterventionResolutionPayload)
    assert resolution.payload.wave_ids == ["wave-one", "wave-two"]
    projection = project_recovery(snapshot.lock, snapshot.events)
    assert projection.status == "active"
    assert {
        projection.waves[wave_id].status for wave_id in resolution.payload.wave_ids
    } == {"closed"}


def test_unpaired_cleanup_failure_keeps_run_in_manual_intervention(
    remote_spec: ExperimentSpec,
) -> None:
    snapshot = _snapshot(remote_spec)
    shard = snapshot.lock.executions[0].shards[0]
    snapshot.events.append(
        new_event(
            subject_type="wave",
            subject_id="wave-unpaired",
            kind="wave.cleanup-failed",
            producer="watchdog",
            payload=WaveLifecyclePayload(
                deployment_digest=snapshot.lock.executions[0].deployment_digest,
                provider="hf-inference-endpoints",
                shard_ids=[shard.shard_id],
            ),
            clock=lambda: snapshot.lock.created_at,
            identifier=lambda: "5" * 32,
        )
    )

    assert (
        project_recovery(snapshot.lock, snapshot.events).status == "manual_intervention"
    )
    with pytest.raises(ValueError, match="requirement has not been recorded"):
        resume_run(
            MemoryStore(snapshot),
            "run-one",
            reason="verified",
            cleanup_verified=True,
            dry_run=False,
        )


def test_new_cleanup_failure_requires_a_new_manual_requirement(
    remote_spec: ExperimentSpec,
) -> None:
    snapshot = _snapshot(remote_spec)
    shard = snapshot.lock.executions[0].shards[0]
    wave_payload = WaveLifecyclePayload(
        deployment_digest=snapshot.lock.executions[0].deployment_digest,
        provider="hf-inference-endpoints",
        shard_ids=[shard.shard_id],
    )
    snapshot.events.extend(
        [
            new_event(
                subject_type="wave",
                subject_id="wave-one",
                kind="wave.cleanup-failed",
                producer="watchdog",
                payload=wave_payload,
                clock=lambda: snapshot.lock.created_at,
                identifier=lambda: "6" * 32,
            ),
            new_event(
                subject_type="run",
                subject_id=snapshot.lock.run_id,
                kind="run.manual-intervention-required",
                producer="reconciler",
                payload=LifecyclePayload(parent_id="wave-one"),
                clock=lambda: snapshot.lock.created_at + timedelta(seconds=1),
                identifier=lambda: "7" * 32,
            ),
        ]
    )
    store = MemoryStore(snapshot)
    resume_run(
        store,
        "run-one",
        reason="verified first wave",
        cleanup_verified=True,
        dry_run=False,
    )
    snapshot.events.append(
        new_event(
            subject_type="wave",
            subject_id="wave-two",
            kind="wave.cleanup-failed",
            producer="watchdog",
            payload=wave_payload,
            clock=lambda: snapshot.lock.created_at + timedelta(seconds=2),
            identifier=lambda: "8" * 32,
        )
    )

    assert (
        project_recovery(snapshot.lock, snapshot.events).status == "manual_intervention"
    )
    with pytest.raises(ValueError, match="requirement has not been recorded"):
        resume_run(
            store,
            "run-one",
            reason="verified second wave",
            cleanup_verified=True,
            dry_run=False,
        )


def test_resume_accepts_cleanup_wave_already_closed(
    remote_spec: ExperimentSpec,
) -> None:
    snapshot = _snapshot(remote_spec)
    shard = snapshot.lock.executions[0].shards[0]
    wave_payload = WaveLifecyclePayload(
        deployment_digest=snapshot.lock.executions[0].deployment_digest,
        provider="hf-inference-endpoints",
        shard_ids=[shard.shard_id],
    )
    snapshot.events.extend(
        [
            new_event(
                subject_type="wave",
                subject_id="wave-one",
                kind="wave.cleanup-failed",
                producer="watchdog",
                payload=wave_payload,
                clock=lambda: snapshot.lock.created_at - timedelta(seconds=2),
                identifier=lambda: "2" * 32,
            ),
            new_event(
                subject_type="run",
                subject_id=snapshot.lock.run_id,
                kind="run.manual-intervention-required",
                producer="reconciler",
                payload=LifecyclePayload(parent_id="wave-one"),
                clock=lambda: snapshot.lock.created_at - timedelta(seconds=1),
                identifier=lambda: "3" * 32,
            ),
            new_event(
                subject_type="wave",
                subject_id="wave-one",
                kind="wave.closed",
                producer="watchdog",
                payload=wave_payload,
                clock=lambda: snapshot.lock.created_at,
                identifier=lambda: "4" * 32,
            ),
        ]
    )
    store = MemoryStore(snapshot)

    result = resume_run(
        store,
        "run-one",
        reason="verified paused",
        cleanup_verified=True,
        dry_run=False,
    )

    assert result.recorded
    assert project_recovery(snapshot.lock, snapshot.events).waves[
        "wave-one"
    ].status == ("closed")


def test_resume_validates_wave_and_orders_after_existing_events(
    remote_spec: ExperimentSpec,
) -> None:
    snapshot = _snapshot(remote_spec)
    required = new_event(
        subject_type="run",
        subject_id=snapshot.lock.run_id,
        kind="run.manual-intervention-required",
        producer="reconciler",
        payload=LifecyclePayload(parent_id="missing-wave"),
        clock=lambda: snapshot.lock.created_at,
        identifier=lambda: "2" * 32,
    )

    with pytest.raises(ValueError, match="does not reference recoverable cleanup"):
        durable_manual_intervention_resolution_event(
            snapshot.lock,
            [*snapshot.events, required],
            "verified",
            cleanup_verified=True,
            clock=lambda: snapshot.lock.created_at - timedelta(days=1),
        )

    shard = snapshot.lock.executions[0].shards[0]
    cleanup_failed = new_event(
        subject_type="wave",
        subject_id="wave-one",
        kind="wave.cleanup-failed",
        producer="watchdog",
        payload=WaveLifecyclePayload(
            deployment_digest=snapshot.lock.executions[0].deployment_digest,
            provider="hf-inference-endpoints",
            shard_ids=[shard.shard_id],
        ),
        clock=lambda: snapshot.lock.created_at,
        identifier=lambda: "3" * 32,
    )
    required = required.model_copy(
        update={
            "payload": LifecyclePayload(parent_id="wave-one"),
            "observed_at": snapshot.lock.created_at + timedelta(seconds=1),
        }
    )
    event, created = durable_manual_intervention_resolution_event(
        snapshot.lock,
        [*snapshot.events, cleanup_failed, required],
        "verified",
        cleanup_verified=True,
        clock=lambda: snapshot.lock.created_at - timedelta(days=1),
    )

    assert created
    assert event.observed_at == required.observed_at + timedelta(microseconds=1)


def test_verifies_and_publishes_run_evidence(
    remote_spec: ExperimentSpec,
) -> None:
    snapshot = _snapshot(remote_spec)
    evidence = _evidence(snapshot)

    verified = verify_run_artifacts(snapshot, namespace="example-org", reader=evidence)
    dry_run = publish_run_results(
        snapshot,
        namespace="example-org",
        reader=evidence,
        publisher=None,
        dry_run=True,
    )
    publisher = FakePublisher()
    published = publish_run_results(
        snapshot,
        namespace="example-org",
        reader=evidence,
        publisher=publisher,
        dry_run=False,
    )

    assert verified.verified
    assert verified.executions[0].row_counts == {
        "executions": 1,
        "trials": 1,
        "attempts": 1,
        "metrics": 1,
        "artifacts": 0,
    }
    assert not dry_run.executions[0].published
    assert published.executions[0].published
    assert published.executions[0].result_revision == "a" * 40
    assert len(publisher.publications) == 1


def test_verification_rejects_tampered_bucket_evidence(
    remote_spec: ExperimentSpec,
) -> None:
    snapshot = _snapshot(remote_spec)
    evidence = _evidence(snapshot)
    evidence.files["execution.lock.json"] = b"tampered"

    with pytest.raises(ResultPublicationError, match="checksum mismatch"):
        verify_run_artifacts(snapshot, namespace="example-org", reader=evidence)


def test_automatic_publisher_initializes_new_empty_public_repositories(
    remote_spec: ExperimentSpec,
) -> None:
    interactions: list[object] = []
    snapshot = _snapshot(remote_spec)
    source = _evidence(snapshot)
    reader = MemoryEvidence(
        source.prefix,
        source.files,
        interactions=interactions,
    )
    publisher = FakePublisher(interactions=interactions)
    repositories = MemoryRepositories(interactions)

    report = AutomaticRunPublisher(
        namespace="example-org",
        store=MemoryStore(snapshot),
        reader=reader,
        publisher=publisher,
        repositories=repositories,
    ).publish(snapshot.lock.run_id)

    assert report.run_id == snapshot.lock.run_id
    assert report.control_commit == snapshot.control_commit
    assert report.dry_run is False
    assert len(report.executions) == 1
    assert report.executions[0].model_dump(mode="json") == {
        "execution_id": snapshot.lock.executions[0].execution_id,
        "publication_id": publisher.publications[0].tables.publication_id,
        "result_dataset": "example/shellbench-results",
        "index_dataset": "example/benchmark-run-index",
        "published": True,
        "result_revision": "a" * 40,
        "index_revision": "b" * 40,
    }
    commit_kwargs = {
        "commit_message": "chore: initialize publication Dataset",
        "repo_type": "dataset",
        "revision": "main",
    }
    assert interactions[:9] == [
        (
            "create_repo",
            "example/shellbench-results",
            {"repo_type": "dataset", "private": False, "exist_ok": True},
        ),
        (
            "create_repo",
            "example/benchmark-run-index",
            {"repo_type": "dataset", "private": False, "exist_ok": True},
        ),
        (
            "repo_info",
            "example/shellbench-results",
            {"repo_type": "dataset"},
        ),
        (
            "repo_info",
            "example/benchmark-run-index",
            {"repo_type": "dataset"},
        ),
        ("create_commit", "example/shellbench-results", commit_kwargs),
        (
            "repo_info",
            "example/shellbench-results",
            {"repo_type": "dataset", "revision": "main"},
        ),
        ("create_commit", "example/benchmark-run-index", commit_kwargs),
        (
            "repo_info",
            "example/benchmark-run-index",
            {"repo_type": "dataset", "revision": "main"},
        ),
        "refresh",
    ]
    assert interactions[9] == (
        "publish",
        "example/shellbench-results",
        "example/benchmark-run-index",
        publisher.publications[0],
    )
    assert [
        repository for repository, _operations, _kwargs in repositories.commits
    ] == [
        "example/shellbench-results",
        "example/benchmark-run-index",
    ]
    for _repository, operations, kwargs in repositories.commits:
        assert kwargs == commit_kwargs
        assert "parent_commit" not in kwargs
        assert len(operations) == 1
        operation = operations[0]
        assert isinstance(operation, CommitOperationAdd)
        assert operation.path_in_repo == ".harbor-hf-initialized"
        assert operation.path_or_fileobj == b"harbor-hf publication Dataset\n"
    assert reader.refresh_calls == 1


def test_automatic_publisher_initializes_new_empty_private_repositories(
    remote_spec: ExperimentSpec,
) -> None:
    interactions: list[object] = []
    private_spec = remote_spec.model_copy(
        update={
            "publishing": remote_spec.publishing.model_copy(
                update={
                    "dataset_visibility": "private",
                    "index_dataset_visibility": "private",
                }
            )
        }
    )
    snapshot = _snapshot(private_spec)
    source = _evidence(snapshot)
    reader = MemoryEvidence(source.prefix, source.files, interactions=interactions)
    publisher = FakePublisher(interactions=interactions)
    repositories = MemoryRepositories(interactions)

    report = AutomaticRunPublisher(
        namespace="example-org",
        store=MemoryStore(snapshot),
        reader=reader,
        publisher=publisher,
        repositories=repositories,
    ).publish(snapshot.lock.run_id)

    assert report.executions[0].published
    assert repositories.private == {
        "example/shellbench-results": True,
        "example/benchmark-run-index": True,
    }
    assert interactions[0] == (
        "create_repo",
        "example/shellbench-results",
        {"repo_type": "dataset", "private": True, "exist_ok": True},
    )
    assert interactions[1] == (
        "create_repo",
        "example/benchmark-run-index",
        {"repo_type": "dataset", "private": True, "exist_ok": True},
    )
    assert reader.refresh_calls == 1


def test_automatic_publisher_adopts_initialized_public_repositories(
    remote_spec: ExperimentSpec,
) -> None:
    interactions: list[object] = []
    snapshot = _snapshot(remote_spec)
    source = _evidence(snapshot)
    reader = MemoryEvidence(
        source.prefix,
        source.files,
        interactions=interactions,
    )
    publisher = FakePublisher(interactions=interactions)
    repositories = MemoryRepositories(
        interactions,
        existing={
            "example/shellbench-results": False,
            "example/benchmark-run-index": False,
        },
    )

    report = AutomaticRunPublisher(
        namespace="example-org",
        store=MemoryStore(snapshot),
        reader=reader,
        publisher=publisher,
        repositories=repositories,
    ).publish(snapshot.lock.run_id)

    assert report.executions[0].published
    assert repositories.private == {
        "example/shellbench-results": False,
        "example/benchmark-run-index": False,
    }
    assert reader.refresh_calls == 1
    assert len(publisher.publications) == 1
    assert repositories.commits == []


@pytest.mark.parametrize(
    "private_repository",
    ["example/shellbench-results", "example/benchmark-run-index"],
)
def test_automatic_publisher_rejects_visibility_mismatch_before_evidence(
    remote_spec: ExperimentSpec,
    private_repository: str,
) -> None:
    interactions: list[object] = []
    snapshot = _snapshot(remote_spec)
    source = _evidence(snapshot)
    reader = MemoryEvidence(
        source.prefix,
        source.files,
        interactions=interactions,
    )
    publisher = FakePublisher(interactions=interactions)
    repositories = MemoryRepositories(
        interactions,
        existing={
            "example/shellbench-results": (
                private_repository == "example/shellbench-results"
            ),
            "example/benchmark-run-index": (
                private_repository == "example/benchmark-run-index"
            ),
        },
    )

    with pytest.raises(
        ValueError,
        match=(
            f"^Dataset repository {private_repository} is private; "
            "manifest requires public$"
        ),
    ):
        AutomaticRunPublisher(
            namespace="example-org",
            store=MemoryStore(snapshot),
            reader=reader,
            publisher=publisher,
            repositories=repositories,
        ).publish(snapshot.lock.run_id)

    assert interactions == [
        (
            "create_repo",
            "example/shellbench-results",
            {"repo_type": "dataset", "private": False, "exist_ok": True},
        ),
        (
            "create_repo",
            "example/benchmark-run-index",
            {"repo_type": "dataset", "private": False, "exist_ok": True},
        ),
        (
            "repo_info",
            "example/shellbench-results",
            {"repo_type": "dataset"},
        ),
        (
            "repo_info",
            "example/benchmark-run-index",
            {"repo_type": "dataset"},
        ),
    ]
    assert reader.refresh_calls == 0
    assert publisher.publications == []


@pytest.mark.parametrize(
    "public_repository",
    ["example/shellbench-results", "example/benchmark-run-index"],
)
def test_automatic_publisher_rejects_public_repository_when_private_required(
    remote_spec: ExperimentSpec,
    public_repository: str,
) -> None:
    interactions: list[object] = []
    private_spec = remote_spec.model_copy(
        update={
            "publishing": remote_spec.publishing.model_copy(
                update={
                    "dataset_visibility": "private",
                    "index_dataset_visibility": "private",
                }
            )
        }
    )
    snapshot = _snapshot(private_spec)
    source = _evidence(snapshot)
    reader = MemoryEvidence(source.prefix, source.files, interactions=interactions)
    publisher = FakePublisher(interactions=interactions)
    repositories = MemoryRepositories(
        interactions,
        existing={
            "example/shellbench-results": (
                public_repository != "example/shellbench-results"
            ),
            "example/benchmark-run-index": (
                public_repository != "example/benchmark-run-index"
            ),
        },
    )

    with pytest.raises(
        ValueError,
        match=(
            f"^Dataset repository {public_repository} is public; "
            "manifest requires private$"
        ),
    ):
        AutomaticRunPublisher(
            namespace="example-org",
            store=MemoryStore(snapshot),
            reader=reader,
            publisher=publisher,
            repositories=repositories,
        ).publish(snapshot.lock.run_id)

    assert reader.refresh_calls == 0
    assert publisher.publications == []


def test_automatic_publisher_rejects_missing_index_without_side_effects(
    remote_spec: ExperimentSpec,
) -> None:
    interactions: list[object] = []
    spec = remote_spec.model_copy(
        update={
            "publishing": remote_spec.publishing.model_copy(
                update={"index_dataset": None, "index_dataset_visibility": None}
            )
        }
    )
    snapshot = _snapshot(spec)
    source = _evidence(snapshot)
    reader = MemoryEvidence(
        source.prefix,
        source.files,
        interactions=interactions,
    )

    with pytest.raises(ValueError) as captured:
        AutomaticRunPublisher(
            namespace="example-org",
            store=MemoryStore(snapshot),
            reader=reader,
            publisher=FakePublisher(interactions=interactions),
            repositories=MemoryRepositories(interactions),
        ).publish(snapshot.lock.run_id)

    assert str(captured.value) == "run result publication requires index_dataset"
    assert interactions == []
    assert reader.refresh_calls == 0


def test_automatic_publisher_correction_publishes_legacy_evidence_privately(
    remote_spec: ExperimentSpec,
) -> None:
    interactions: list[object] = []
    snapshot = _legacy_publication_snapshot(remote_spec)
    source = _evidence(snapshot)
    reader = MemoryEvidence(source.prefix, source.files, interactions=interactions)
    publisher = FakePublisher(interactions=interactions)
    repositories = MemoryRepositories(interactions)
    correction = _publication_correction(snapshot)

    report = AutomaticRunPublisher(
        namespace="example-org",
        store=MemoryStore(snapshot),
        reader=reader,
        publisher=publisher,
        repositories=repositories,
    ).publish_correction(correction)

    assert report.executions[0].published
    assert repositories.private == {
        "example/shellbench-results": True,
        "example/benchmark-run-index": True,
    }
    assert reader.refresh_calls == 1
    assert len(publisher.publications) == 1
    assert publication_correction_digest(correction).startswith("sha256:")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "source_manifest_digest",
            "sha256:" + "0" * 64,
            "source manifest digest does not match",
        ),
        (
            "source_plan_digest",
            "sha256:" + "0" * 64,
            "source plan digest does not match",
        ),
    ],
)
def test_automatic_publisher_correction_rejects_source_mismatch_before_writes(
    remote_spec: ExperimentSpec,
    field: str,
    value: str,
    message: str,
) -> None:
    interactions: list[object] = []
    snapshot = _legacy_publication_snapshot(remote_spec)
    source = _evidence(snapshot)
    correction = _publication_correction(snapshot).model_copy(update={field: value})

    with pytest.raises(ValueError, match=message):
        AutomaticRunPublisher(
            namespace="example-org",
            store=MemoryStore(snapshot),
            reader=MemoryEvidence(source.prefix, source.files),
            publisher=FakePublisher(),
            repositories=MemoryRepositories(interactions),
        ).publish_correction(correction)

    assert interactions == []


def test_automatic_publisher_correction_rejects_current_request(
    remote_spec: ExperimentSpec,
) -> None:
    interactions: list[object] = []
    snapshot = _snapshot(remote_spec)
    source = _evidence(snapshot)

    with pytest.raises(ValueError, match="only for requests without visibility"):
        AutomaticRunPublisher(
            namespace="example-org",
            store=MemoryStore(snapshot),
            reader=MemoryEvidence(source.prefix, source.files),
            publisher=FakePublisher(),
            repositories=MemoryRepositories(interactions),
        ).publish_correction(_publication_correction(snapshot))

    assert interactions == []


def test_automatic_publisher_correction_rejects_visibility_mismatch_before_evidence(
    remote_spec: ExperimentSpec,
) -> None:
    interactions: list[object] = []
    snapshot = _legacy_publication_snapshot(remote_spec)
    source = _evidence(snapshot)
    reader = MemoryEvidence(source.prefix, source.files, interactions=interactions)
    correction = _publication_correction(snapshot)
    repositories = MemoryRepositories(
        interactions,
        existing={
            "example/shellbench-results": False,
            "example/benchmark-run-index": True,
        },
    )

    with pytest.raises(
        ValueError,
        match=(
            "Dataset repository example/shellbench-results is public; "
            "manifest requires private"
        ),
    ):
        AutomaticRunPublisher(
            namespace="example-org",
            store=MemoryStore(snapshot),
            reader=reader,
            publisher=FakePublisher(interactions=interactions),
            repositories=repositories,
        ).publish_correction(correction)

    assert reader.refresh_calls == 0
    assert not any(
        isinstance(interaction, tuple) and interaction[0] == "publish"
        for interaction in interactions
    )


def test_automatic_publisher_reports_run_identity_for_invalid_request(
    remote_spec: ExperimentSpec,
) -> None:
    interactions: list[object] = []
    snapshot = _snapshot(remote_spec)
    snapshot = RunSnapshot(
        lock=snapshot.lock,
        events=snapshot.events,
        request=b"not yaml: [",
        control_commit=snapshot.control_commit,
    )
    source = _evidence(snapshot)
    reader = MemoryEvidence(
        source.prefix,
        source.files,
        interactions=interactions,
    )

    with pytest.raises(ManifestError) as captured:
        AutomaticRunPublisher(
            namespace="example-org",
            store=MemoryStore(snapshot),
            reader=reader,
            publisher=FakePublisher(interactions=interactions),
            repositories=MemoryRepositories(interactions),
        ).publish(snapshot.lock.run_id)

    assert str(captured.value).startswith(
        "cannot read run run-one request: while parsing a flow node"
    )
    assert interactions == []
    assert reader.refresh_calls == 0
