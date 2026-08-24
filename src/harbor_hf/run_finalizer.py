from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal, Protocol, cast

from pydantic import TypeAdapter

from harbor_hf.control import RetryCategory
from harbor_hf.executions import ExecutionLock
from harbor_hf.harbor_native_bundle import (
    HARBOR_NATIVE_BUNDLE_PATH,
    BundleObject,
    HarborNativeBundle,
    load_harbor_native_bundle,
)
from harbor_hf.models import DeploymentProfile, ExperimentSpec
from harbor_hf.provider_models import ProviderTarget
from harbor_hf.publication_envelope import (
    PUBLICATION_ENVELOPE_PATH,
    HarborBundleReference,
    ObjectReference,
    PhysicalAttemptReference,
    ProfileDigests,
    PublicationEnvelope,
    RuntimeIdentity,
    canonical_digest,
    canonical_json_bytes,
    object_reference,
    profile_digest,
)
from harbor_hf.recovery import (
    RecoveryProjection,
    TaskOutcome,
    TerminalDecision,
    TrialStatus,
)
from harbor_hf.results import (
    ArtifactEvidence,
    ArtifactKind,
    AttemptEvidence,
    EvidenceReader,
    ExecutionEvidence,
    ExecutionQuality,
    MetricEvidence,
    ResultEvidence,
    RuntimeKind,
    TrialEvidence,
)
from harbor_hf.runs import RunExecutionLock, RunLock, RunTrialLock
from harbor_hf.wave_worker import AttemptLock

_JSON_OBJECT = TypeAdapter(dict[str, object])
_RETRY_CATEGORY = TypeAdapter(RetryCategory)
_TERMINAL_MARKERS = frozenset({"_SUCCESS", "_FAILED", "_CANCELLED"})


@dataclass(frozen=True)
class _FinalizedExecution:
    manifest_checksum: str
    source_checksum: str


class RunFinalizationError(RuntimeError):
    """Raised when terminal run evidence cannot be finalized safely."""


class ImmutableEvidenceWriter(Protocol):
    def write_immutable(self, *, bucket: str, path: str, content: bytes) -> bool: ...


class ValidatingEvidenceWriter:
    """Validate immutable writes against existing evidence without mutating it."""

    def __init__(self, reader: EvidenceReader, *, bucket: str, prefix: str) -> None:
        self.reader = reader
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self.paths = set(reader.list_files(bucket=bucket, prefix=self.prefix))
        self.staged: dict[str, bytes] = {}

    def write_immutable(self, *, bucket: str, path: str, content: bytes) -> bool:
        root = f"{self.prefix}/"
        if bucket != self.bucket or not path.startswith(root):
            raise RunFinalizationError("immutable write escaped run evidence")
        relative = path.removeprefix(root)
        if not relative:
            raise RunFinalizationError("immutable write has no evidence path")
        staged = self.staged.get(relative)
        if staged is not None and staged != content:
            raise RunFinalizationError(
                f"immutable evidence conflicts during validation: {path}"
            )
        if relative in self.paths:
            existing = self.reader.read_bytes(
                bucket=bucket,
                prefix=self.prefix,
                path=relative,
            )
            if existing != content:
                raise RunFinalizationError(
                    f"immutable evidence conflicts during validation: {path}"
                )
        self.staged[relative] = content
        return relative not in self.paths


class RunFinalizer(Protocol):
    def finalize(
        self,
        lock: RunLock,
        spec: ExperimentSpec,
        projection: RecoveryProjection,
        decision: TerminalDecision,
    ) -> None: ...


class BucketRunFinalizer:
    """Build terminal run and run records from canonical Bucket evidence."""

    def __init__(
        self,
        reader: EvidenceReader,
        writer: ImmutableEvidenceWriter,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self._staged: dict[tuple[str, str, str], bytes] = {}

    def finalize(
        self,
        lock: RunLock,
        spec: ExperimentSpec,
        projection: RecoveryProjection,
        decision: TerminalDecision,
    ) -> None:
        self._staged.clear()
        paths = self.reader.list_files(
            bucket=spec.artifacts.bucket,
            prefix=lock.artifact_prefix,
        )
        execution_checksums: dict[str, str] = {}
        for execution in lock.executions:
            if projection.executions[execution.execution_id].status != "complete":
                continue
            finalized = self._finalize_execution(
                lock, spec, execution, paths, projection
            )
            execution_checksums[execution.execution_id] = finalized.manifest_checksum
        if decision.status == "completed" and len(execution_checksums) != len(
            lock.executions
        ):
            raise RunFinalizationError(
                "completed run does not have complete execution evidence"
            )
        summary = _json_bytes(
            {
                "schema_version": "harbor-hf/run-summary/v1alpha1",
                "run_id": lock.run_id,
                "status": decision.status,
                "reason": decision.reason,
                "counts": decision.counts.model_dump(mode="json"),
                "execution_checksums": dict(sorted(execution_checksums.items())),
            }
        )
        self.writer.write_immutable(
            bucket=spec.artifacts.bucket,
            path=decision.summary_path,
            content=summary,
        )
        self.writer.write_immutable(
            bucket=spec.artifacts.bucket,
            path=decision.marker_path,
            content=b"\n",
        )

    def seal_executions(
        self,
        lock: RunLock,
        spec: ExperimentSpec,
        projection: RecoveryProjection,
    ) -> dict[str, str]:
        """Finalize publishable executions without rewriting run terminal evidence."""
        self._staged.clear()
        if any(run.status != "complete" for run in projection.executions.values()):
            raise RunFinalizationError("sealed projection has an incomplete run")
        paths = self.reader.list_files(
            bucket=spec.artifacts.bucket,
            prefix=lock.artifact_prefix,
        )
        return {
            execution.execution_id: self._finalize_execution(
                lock, spec, execution, paths, projection
            ).source_checksum
            for execution in lock.executions
        }

    def _finalize_execution(
        self,
        lock: RunLock,
        spec: ExperimentSpec,
        execution: RunExecutionLock,
        paths: list[str],
        projection: RecoveryProjection,
    ) -> _FinalizedExecution:
        prefix = f"executions/{execution.execution_id}"
        execution_paths = _under(paths, prefix)
        execution_lock_bytes = self._read(spec, lock, f"{prefix}/execution.lock.json")
        configuration = ExecutionLock.model_validate_json(execution_lock_bytes)
        _validate_execution_identity(lock, execution, configuration)
        trials: list[TrialEvidence] = []
        attempts: list[AttemptEvidence] = []
        metrics: list[MetricEvidence] = []
        physical_attempts: list[PhysicalAttemptReference] = []
        verification_trials: list[object] = []
        completion_times: list[datetime] = []
        runtime_kind = (
            "provider"
            if isinstance(configuration.deployment, ProviderTarget)
            else "endpoint"
        )
        for shard in execution.shards:
            for trial in shard.trials:
                records = self._trial_records(
                    spec,
                    lock,
                    prefix,
                    execution_paths,
                    trial,
                    runtime_kind,
                    projection.trials[trial.trial_id].status,
                    projection.trials[trial.trial_id].outcome,
                )
                trials.append(records.trial)
                attempts.extend(records.attempts)
                physical_attempts.extend(records.physical_attempts)
                metrics.extend(records.metrics)
                verification_trials.extend(records.verification_trials)
                completion_times.append(records.completed_at)
        verification = _json_bytes(
            {
                "trial_count": len(verification_trials),
                "trials": verification_trials,
            }
        )
        verification_path = f"{prefix}/verification.json"
        self.writer.write_immutable(
            bucket=spec.artifacts.bucket,
            path=f"{lock.artifact_prefix}/{verification_path}",
            content=verification,
        )
        completed_at = max(completion_times)
        artifacts = [
            _artifact(
                execution.execution_id,
                "execution_lock",
                "execution.lock.json",
                execution_lock_bytes,
            ),
            _artifact(
                execution.execution_id,
                "verification",
                "verification.json",
                verification,
            ),
        ]
        quality: ExecutionQuality = (
            "degraded"
            if any(trial.outcome != "scored" for trial in trials)
            else "clean"
        )
        execution_evidence = _execution_evidence(
            lock, configuration, completed_at, quality=quality
        )
        evidence = ResultEvidence(
            sanitized=True,
            execution=execution_evidence,
            trials=trials,
            attempts=attempts,
            metrics=metrics,
            artifacts=artifacts,
        )
        summary = _json_bytes(evidence.model_dump(mode="json"))
        summary_path = f"{prefix}/execution-summary.json"
        self.writer.write_immutable(
            bucket=spec.artifacts.bucket,
            path=f"{lock.artifact_prefix}/{summary_path}",
            content=summary,
        )
        envelope = PublicationEnvelope(
            execution_id=execution.execution_id,
            run_id=lock.run_id,
            created_at=configuration.created_at,
            completed_at=completed_at,
            evidence_bucket=spec.artifacts.bucket,
            evidence_prefix=f"{lock.artifact_prefix}/{prefix}",
            execution_lock=object_reference(
                "execution.lock.json", execution_lock_bytes
            ),
            profiles=ProfileDigests(
                experiment=configuration.spec_digest,
                model=profile_digest(configuration.model),
                deployment=profile_digest(configuration.deployment),
                agent=profile_digest(configuration.agent),
            ),
            runtime=RuntimeIdentity(
                kind=runtime_kind,
                provider=execution_evidence.provider,
                region=execution_evidence.region,
                hardware=execution_evidence.hardware,
                accelerator_count=execution_evidence.accelerator_count,
            ),
            cleanup_outcome=(
                "not_applicable" if runtime_kind == "provider" else "verified"
            ),
            attempts=physical_attempts,
        )
        envelope_bytes = canonical_json_bytes(envelope.model_dump(mode="json"))
        self.writer.write_immutable(
            bucket=spec.artifacts.bucket,
            path=(f"{lock.artifact_prefix}/{prefix}/{PUBLICATION_ENVELOPE_PATH}"),
            content=envelope_bytes,
        )
        additions = {
            "verification.json": verification,
            "execution-summary.json": summary,
            PUBLICATION_ENVELOPE_PATH: envelope_bytes,
        }
        checksums = self._aggregate_checksums(
            spec,
            lock,
            prefix,
            execution_paths,
            additions,
        )
        checksum_bytes = _json_bytes(checksums)
        self.writer.write_immutable(
            bucket=spec.artifacts.bucket,
            path=f"{lock.artifact_prefix}/{prefix}/checksums.json",
            content=checksum_bytes,
        )
        self.writer.write_immutable(
            bucket=spec.artifacts.bucket,
            path=f"{lock.artifact_prefix}/{prefix}/_SUCCESS",
            content=b"\n",
        )
        return _FinalizedExecution(
            manifest_checksum=_sha256(checksum_bytes),
            source_checksum=canonical_digest(checksums),
        )

    def _trial_records(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_prefix: str,
        execution_paths: list[str],
        trial: RunTrialLock,
        runtime_kind: RuntimeKind,
        status: TrialStatus,
        outcome: TaskOutcome | None,
    ) -> _TrialRecords:
        prefix = f"{execution_prefix}/trials/{trial.trial_id}"
        relative_prefix = prefix.removeprefix(f"{execution_prefix}/")
        if status != "complete":
            return self._failed_trial_records(
                spec,
                run,
                execution_prefix,
                execution_paths,
                trial,
                runtime_kind,
                status,
                outcome,
            )
        _require_scored_outcome(outcome, trial.trial_id)
        selected_id = self._complete_trial_selection(
            spec,
            run,
            execution_prefix,
            execution_paths,
            trial,
            runtime_kind,
        )
        attempt_paths = sorted(
            path
            for path in execution_paths
            if path.startswith(f"{relative_prefix}/attempts/")
            and path.endswith("/attempt.lock.json")
        )
        records: list[AttemptEvidence] = []
        physical_attempts: list[PhysicalAttemptReference] = []
        selected: tuple[dict[str, object], datetime] | None = None
        for relative in attempt_paths:
            attempt_prefix = str(PurePosixPath(relative).parent)
            absolute_prefix = f"{execution_prefix}/{attempt_prefix}"
            record = self._attempt_record(
                spec,
                run,
                execution_paths,
                attempt_prefix,
                absolute_prefix,
                runtime_kind,
            )
            records.append(record.evidence)
            physical_attempts.append(record.physical_attempt)
            if record.evidence.attempt_id != selected_id:
                continue
            if record.evidence.status != "succeeded":
                raise RunFinalizationError("trial selected attempt is not successful")
            selected = (
                _JSON_OBJECT.validate_json(
                    self._read(spec, run, f"{absolute_prefix}/verification.json")
                ),
                record.evidence.completed_at,
            )
        if selected is None:
            raise RunFinalizationError("trial selected attempt is missing")
        verifier = _selected_verifier(selected[0])
        return _TrialRecords(
            trial=TrialEvidence(
                trial_id=trial.trial_id,
                task_name=trial.task_name,
                task_digest=trial.task_digest,
                logical_attempt=trial.logical_attempt,
                selected_attempt_id=selected_id,
                outcome="scored",
            ),
            attempts=records,
            physical_attempts=physical_attempts,
            metrics=_reward_metrics(trial.trial_id, verifier),
            verification_trials=[dict(verifier)],
            completed_at=selected[1],
        )

    def _complete_trial_selection(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_prefix: str,
        execution_paths: list[str],
        trial: RunTrialLock,
        runtime_kind: RuntimeKind,
    ) -> str:
        relative_prefix = f"trials/{trial.trial_id}"
        summary_path = f"{relative_prefix}/trial-summary.json"
        marker_path = f"{relative_prefix}/_SUCCESS"
        has_summary = summary_path in execution_paths
        has_marker = marker_path in execution_paths
        if has_marker and not has_summary:
            raise RunFinalizationError(
                f"complete trial has a success marker but no summary: {trial.trial_id}"
            )
        if has_summary:
            summary = _JSON_OBJECT.validate_json(
                self._read(spec, run, f"{execution_prefix}/{summary_path}")
            )
            selected_id = summary.get("attempt_id")
            if not isinstance(selected_id, str):
                raise RunFinalizationError("trial summary has no selected attempt")
            if not has_marker:
                self._finish_interrupted_trial(
                    spec,
                    run,
                    execution_prefix,
                    execution_paths,
                    trial,
                    runtime_kind,
                    selected_id,
                    summary,
                )
            return selected_id

        successful = self._successful_attempt_ids(
            spec, run, execution_prefix, execution_paths, trial, runtime_kind
        )
        if len(successful) != 1:
            raise RunFinalizationError(
                "interrupted trial finalization has an ambiguous successful attempt"
            )
        selected_id = successful[0]
        attempt_checksum = self._attempt_checksum(
            spec, run, execution_prefix, trial.trial_id, selected_id
        )
        summary = _json_bytes(
            {
                "trial_id": trial.trial_id,
                "attempt_id": selected_id,
                "attempt_checksum": attempt_checksum,
            }
        )
        self._stage_trial_envelope(
            spec,
            run,
            execution_prefix,
            execution_paths,
            trial,
            selected_id,
            attempt_checksum,
            summary=summary,
        )
        return selected_id

    def _finish_interrupted_trial(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_prefix: str,
        execution_paths: list[str],
        trial: RunTrialLock,
        runtime_kind: RuntimeKind,
        selected_id: str,
        summary: dict[str, object],
    ) -> None:
        successful = self._successful_attempt_ids(
            spec, run, execution_prefix, execution_paths, trial, runtime_kind
        )
        if successful != [selected_id]:
            raise RunFinalizationError(
                "interrupted trial finalization has an ambiguous successful attempt"
            )
        attempt_checksum = self._attempt_checksum(
            spec, run, execution_prefix, trial.trial_id, selected_id
        )
        if (
            summary.get("trial_id") != trial.trial_id
            or summary.get("attempt_checksum") != attempt_checksum
        ):
            raise RunFinalizationError(
                "interrupted trial summary does not match its attempt"
            )
        self._stage_trial_envelope(
            spec,
            run,
            execution_prefix,
            execution_paths,
            trial,
            selected_id,
            attempt_checksum,
            summary=None,
        )

    def _attempt_checksum(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_prefix: str,
        trial_id: str,
        attempt_id: str,
    ) -> str:
        path = (
            f"{execution_prefix}/trials/{trial_id}/attempts/{attempt_id}/checksums.json"
        )
        return _sha256(self._read(spec, run, path))

    def _stage_trial_envelope(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_prefix: str,
        execution_paths: list[str],
        trial: RunTrialLock,
        selected_id: str,
        attempt_checksum: str,
        *,
        summary: bytes | None,
    ) -> None:
        prefix = f"trials/{trial.trial_id}"
        lock_path = f"{prefix}/trial.lock.json"
        summary_path = f"{prefix}/trial-summary.json"
        checksums_path = f"{prefix}/checksums.json"
        marker_path = f"{prefix}/_SUCCESS"
        lock_content = _json_bytes(trial.model_dump(mode="json"))
        lock_missing = self._trial_lock_missing(
            spec,
            run,
            execution_prefix,
            execution_paths,
            lock_path,
            checksums_path,
            lock_content,
        )

        recovery_path = f"{prefix}/trial-finalization-recovery.json"
        recovery = _json_bytes(
            {
                "schema_version": "harbor-hf/trial-finalization-recovery/v1alpha1",
                "reason": "interrupted_trial_finalization",
                "trial_id": trial.trial_id,
                "selected_attempt_id": selected_id,
                "selected_attempt_checksum": attempt_checksum,
            }
        )
        if checksums_path in execution_paths:
            self._complete_marker_only_trial(
                spec,
                run,
                execution_prefix,
                execution_paths,
                prefix,
                checksums_path,
                lock_missing=lock_missing,
                summary_missing=summary is not None,
            )
        else:
            additions = [(recovery_path, recovery)]
            if lock_missing:
                additions.append((lock_path, lock_content))
            if summary is not None:
                additions.append((summary_path, summary))
            for path, content in additions:
                self._stage_trial_file(
                    spec, run, execution_prefix, execution_paths, path, content
                )
            trial_paths = _under(execution_paths, prefix)
            checksums = self._aggregate_checksums(
                spec,
                run,
                f"{execution_prefix}/{prefix}",
                trial_paths,
                {},
            )
            self._stage_trial_file(
                spec,
                run,
                execution_prefix,
                execution_paths,
                checksums_path,
                _json_bytes(checksums),
            )
        self._stage_trial_file(
            spec, run, execution_prefix, execution_paths, marker_path, b"\n"
        )

    def _trial_lock_missing(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_prefix: str,
        execution_paths: list[str],
        lock_path: str,
        checksums_path: str,
        lock_content: bytes,
    ) -> bool:
        if lock_path not in execution_paths:
            if checksums_path in execution_paths:
                raise RunFinalizationError(
                    "interrupted trial checksum manifest has no trial lock"
                )
            return True
        if self._read(spec, run, f"{execution_prefix}/{lock_path}") != lock_content:
            raise RunFinalizationError("interrupted trial lock does not match its run")
        return False

    def _complete_marker_only_trial(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_prefix: str,
        execution_paths: list[str],
        prefix: str,
        checksums_path: str,
        *,
        lock_missing: bool,
        summary_missing: bool,
    ) -> None:
        if lock_missing or summary_missing:
            raise RunFinalizationError(
                "interrupted trial checksum manifest omits finalization evidence"
            )
        self._validate_trial_checksums(
            spec, run, execution_prefix, execution_paths, prefix, checksums_path
        )

    def _validate_trial_checksums(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_prefix: str,
        execution_paths: list[str],
        prefix: str,
        checksums_path: str,
    ) -> None:
        observed = self._read(spec, run, f"{execution_prefix}/{checksums_path}")
        expected = self._aggregate_checksums(
            spec,
            run,
            f"{execution_prefix}/{prefix}",
            _under(execution_paths, prefix),
            {},
        )
        if _json_bytes(expected) != observed:
            raise RunFinalizationError(
                "interrupted trial checksum manifest is not canonical"
            )

    def _stage_trial_file(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_prefix: str,
        execution_paths: list[str],
        path: str,
        content: bytes,
    ) -> None:
        self._stage_immutable(spec, run, f"{execution_prefix}/{path}", content)
        if path not in execution_paths:
            execution_paths.append(path)

    def _successful_attempt_ids(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_prefix: str,
        execution_paths: list[str],
        trial: RunTrialLock,
        runtime_kind: RuntimeKind,
    ) -> list[str]:
        relative_prefix = f"trials/{trial.trial_id}"
        lock_paths = sorted(
            path
            for path in execution_paths
            if path.startswith(f"{relative_prefix}/attempts/")
            and path.endswith("/attempt.lock.json")
        )
        execution_id = execution_prefix.removeprefix("executions/")
        shard_ids = [
            shard.shard_id
            for execution in run.executions
            if execution.execution_id == execution_id
            for shard in execution.shards
            if any(candidate.trial_id == trial.trial_id for candidate in shard.trials)
        ]
        if len(shard_ids) != 1:
            raise RunFinalizationError(
                "successful attempt trial has no unique locked shard"
            )
        successful: list[str] = []
        for path in lock_paths:
            attempt_prefix = str(PurePosixPath(path).parent)
            marker = _marker(execution_paths, attempt_prefix)
            absolute_prefix = f"{execution_prefix}/{attempt_prefix}"
            self._attempt_record(
                spec,
                run,
                execution_paths,
                attempt_prefix,
                absolute_prefix,
                runtime_kind,
            )
            attempt = AttemptLock.model_validate_json(
                self._read(spec, run, f"{execution_prefix}/{path}")
            )
            observed = (
                attempt.run_id,
                attempt.execution_id,
                attempt.shard_id,
                attempt.trial_id,
                attempt.task_name,
                attempt.task_digest,
                attempt.logical_attempt,
                attempt.attempt_id,
            )
            expected = (
                run.run_id,
                execution_id,
                shard_ids[0],
                trial.trial_id,
                trial.task_name,
                trial.task_digest,
                trial.logical_attempt,
                PurePosixPath(attempt_prefix).name,
            )
            if observed != expected:
                raise RunFinalizationError(
                    "successful attempt identity does not match its trial"
                )
            if marker == "_SUCCESS":
                successful.append(attempt.attempt_id)
        return successful

    def _stage_immutable(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        path: str,
        content: bytes,
    ) -> None:
        full_path = f"{run.artifact_prefix}/{path}"
        key = (spec.artifacts.bucket, run.artifact_prefix, path)
        previous = self._staged.get(key)
        if previous is not None and previous != content:
            raise RunFinalizationError(
                f"immutable evidence conflicts during finalization: {full_path}"
            )
        self.writer.write_immutable(
            bucket=spec.artifacts.bucket,
            path=full_path,
            content=content,
        )
        self._staged[key] = content

    def _failed_trial_records(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_prefix: str,
        execution_paths: list[str],
        trial: RunTrialLock,
        runtime_kind: RuntimeKind,
        status: TrialStatus,
        outcome: TaskOutcome | None,
    ) -> _TrialRecords:
        if status not in {"invalid", "failed_infrastructure"}:
            raise RunFinalizationError(
                f"scored run contains a nonterminal trial: {trial.trial_id}"
            )
        if outcome not in {
            "agent_failed",
            "benchmark_failed",
            "infrastructure_exhausted",
        }:
            raise RunFinalizationError(
                f"failed trial has no terminal task outcome: {trial.trial_id}"
            )
        relative_prefix = f"trials/{trial.trial_id}"
        attempt_paths = sorted(
            path
            for path in execution_paths
            if path.startswith(f"{relative_prefix}/attempts/")
            and path.endswith("/attempt.lock.json")
        )
        if not attempt_paths:
            raise RunFinalizationError(
                f"failed trial has no execution evidence: {trial.trial_id}"
            )
        records: list[AttemptEvidence] = []
        physical_attempts: list[PhysicalAttemptReference] = []
        for relative in attempt_paths:
            attempt_prefix = str(PurePosixPath(relative).parent)
            absolute_prefix = f"{execution_prefix}/{attempt_prefix}"
            record = self._attempt_record(
                spec,
                run,
                execution_paths,
                attempt_prefix,
                absolute_prefix,
                runtime_kind,
            )
            records.append(record.evidence)
            physical_attempts.append(record.physical_attempt)
        selected = max(records, key=lambda record: record.physical_attempt)
        if selected.status != "failed":
            raise RunFinalizationError(
                "failed trial selected attempt is not a recorded failure"
            )
        verifier = {
            "task_name": trial.task_name,
            "outcome": "failed",
            "rewards": {"reward": 0.0},
        }
        return _TrialRecords(
            trial=TrialEvidence(
                trial_id=trial.trial_id,
                task_name=trial.task_name,
                task_digest=trial.task_digest,
                logical_attempt=trial.logical_attempt,
                selected_attempt_id=selected.attempt_id,
                outcome=outcome,
            ),
            attempts=records,
            physical_attempts=physical_attempts,
            metrics=[
                MetricEvidence(
                    owner_type="trial",
                    owner_id=trial.trial_id,
                    name="reward",
                    value=0.0,
                    unit="score",
                )
            ],
            verification_trials=[verifier],
            completed_at=selected.completed_at,
        )

    def _attempt_record(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_paths: list[str],
        attempt_prefix: str,
        absolute_prefix: str,
        runtime_kind: RuntimeKind,
    ) -> _AttemptRecord:
        attempt = AttemptLock.model_validate_json(
            self._read(spec, run, f"{absolute_prefix}/attempt.lock.json")
        )
        marker = _marker(execution_paths, attempt_prefix)
        raw_events = _json_lines(
            self._read(spec, run, f"{absolute_prefix}/events.jsonl")
        )
        started = _event_time(raw_events, "attempt_started")
        finished = _event_time(
            raw_events,
            "attempt_succeeded",
            "attempt_failed",
            "attempt_cancelled",
        )
        status = cast(
            Literal["succeeded", "failed", "cancelled"],
            {
                "_SUCCESS": "succeeded",
                "_FAILED": "failed",
                "_CANCELLED": "cancelled",
            }[marker],
        )
        failure_category = self._attempt_failure_category(
            spec,
            run,
            execution_paths,
            attempt_prefix,
            absolute_prefix,
            marker,
        )
        evidence = AttemptEvidence(
            attempt_id=attempt.attempt_id,
            trial_id=attempt.trial_id,
            physical_attempt=attempt.physical_attempt,
            runtime_kind=runtime_kind,
            status=status,
            failure_category=failure_category,
            started_at=started,
            completed_at=finished,
            retry_reason=(
                "infrastructure_retry" if attempt.physical_attempt > 1 else None
            ),
            remote_job_id=attempt.remote_job_id,
        )
        bundle = self._attempt_bundle_reference(
            spec,
            run,
            execution_paths,
            attempt_prefix,
            absolute_prefix,
        )
        if evidence.status == "succeeded" and bundle is None:
            raise RunFinalizationError(
                "successful attempt has no verified Harbor native bundle"
            )
        return _AttemptRecord(
            evidence=evidence,
            physical_attempt=PhysicalAttemptReference(
                attempt_id=evidence.attempt_id,
                trial_id=evidence.trial_id,
                physical_attempt=evidence.physical_attempt,
                status=evidence.status,
                failure_category=evidence.failure_category,
                started_at=evidence.started_at,
                completed_at=evidence.completed_at,
                retry_reason=evidence.retry_reason,
                remote_job_id=evidence.remote_job_id,
                bundle_status="verified" if bundle is not None else "not_available",
                harbor_bundle=bundle,
            ),
        )

    def _attempt_failure_category(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_paths: list[str],
        attempt_prefix: str,
        absolute_prefix: str,
        marker: str,
    ) -> RetryCategory | None:
        if marker != "_FAILED":
            return None
        relative = f"{attempt_prefix}/failure.json"
        if relative not in execution_paths:
            raise RunFinalizationError("failed attempt has no typed failure evidence")
        value = _JSON_OBJECT.validate_json(
            self._read(spec, run, f"{absolute_prefix}/failure.json")
        )
        try:
            return _RETRY_CATEGORY.validate_python(value.get("category"))
        except ValueError as error:
            raise RunFinalizationError(
                "failed attempt has an invalid failure category"
            ) from error

    def _attempt_bundle_reference(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_paths: list[str],
        attempt_prefix: str,
        absolute_prefix: str,
    ) -> HarborBundleReference | None:
        relative_manifest = f"{attempt_prefix}/{HARBOR_NATIVE_BUNDLE_PATH}"
        if relative_manifest not in execution_paths:
            return None
        manifest_bytes = self._read(
            spec,
            run,
            f"{absolute_prefix}/{HARBOR_NATIVE_BUNDLE_PATH}",
        )
        try:
            manifest = load_harbor_native_bundle(manifest_bytes)
        except Exception as error:
            raise RunFinalizationError(
                "execution Harbor native bundle is invalid"
            ) from error
        _validate_native_bundle_paths(manifest)
        self._verify_bundle_documents(
            spec, run, execution_paths, attempt_prefix, absolute_prefix, manifest
        )
        archive = self._verified_bundle_object(
            spec,
            run,
            execution_paths,
            attempt_prefix,
            absolute_prefix,
            manifest.archive,
            "archive",
        )
        self._verified_bundle_object(
            spec,
            run,
            execution_paths,
            attempt_prefix,
            absolute_prefix,
            manifest.compatibility,
            "compatibility export",
        )
        return HarborBundleReference(
            manifest=object_reference(relative_manifest, manifest_bytes),
            archive=archive,
            harbor_revision=manifest.harbor_revision,
            harbor_version=manifest.harbor_version,
            compatibility_schema=manifest.compatibility_schema,
            request_digest=manifest.request_digest,
            document_count=len(manifest.documents),
        )

    def _verified_bundle_object(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_paths: list[str],
        attempt_prefix: str,
        absolute_prefix: str,
        reference: BundleObject,
        label: str,
    ) -> ObjectReference:
        relative = f"{attempt_prefix}/{reference.path}"
        if relative not in execution_paths:
            raise RunFinalizationError(f"execution Harbor {label} is missing")
        content = self._read(spec, run, f"{absolute_prefix}/{reference.path}")
        observed = object_reference(relative, content)
        if (
            observed.digest != reference.digest
            or observed.size_bytes != reference.size_bytes
        ):
            raise RunFinalizationError(
                f"execution Harbor {label} conflicts with its bundle manifest"
            )
        return observed

    def _verify_bundle_documents(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_paths: list[str],
        attempt_prefix: str,
        absolute_prefix: str,
        manifest: HarborNativeBundle,
    ) -> None:
        for document in manifest.documents:
            relative = f"{attempt_prefix}/{document.path}"
            if relative not in execution_paths:
                raise RunFinalizationError(
                    "execution Harbor native document is missing"
                )
            content = self._read(spec, run, f"{absolute_prefix}/{document.path}")
            if _sha256(content) != document.digest:
                raise RunFinalizationError(
                    "execution Harbor native document conflicts with its bundle"
                )

    def _aggregate_checksums(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_prefix: str,
        execution_paths: list[str],
        additions: dict[str, bytes],
    ) -> dict[str, str]:
        covered = self._child_checksums(spec, run, execution_prefix, execution_paths)
        expected = set(execution_paths) | set(additions)
        expected.discard("checksums.json")
        expected.difference_update(_TERMINAL_MARKERS)
        for path in sorted(expected - covered.keys()):
            content = additions.get(path)
            if content is None:
                content = self._read(spec, run, f"{execution_prefix}/{path}")
            covered[path] = _sha256(content)
        if set(covered) != expected:
            raise RunFinalizationError(
                "execution checksums do not cover exact evidence"
            )
        for path, digest in sorted(covered.items()):
            content = additions.get(path)
            if content is None:
                content = self._read(spec, run, f"{execution_prefix}/{path}")
            if _sha256(content) != digest:
                raise RunFinalizationError(
                    f"child checksum does not match evidence: {path}"
                )
        return dict(sorted(covered.items()))

    def _child_checksums(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        execution_prefix: str,
        execution_paths: list[str],
    ) -> dict[str, str]:
        covered: dict[str, str] = {}
        for path in sorted(execution_paths):
            if not path.endswith("/checksums.json"):
                continue
            manifest = json.loads(self._read(spec, run, f"{execution_prefix}/{path}"))
            if not isinstance(manifest, dict):
                raise RunFinalizationError("child checksum manifest is invalid")
            parent = str(PurePosixPath(path).parent)
            for relative, digest in manifest.items():
                if not isinstance(relative, str) or not isinstance(digest, str):
                    raise RunFinalizationError("child checksum manifest is invalid")
                key = str(PurePosixPath(parent, relative))
                previous = covered.setdefault(key, digest)
                if previous != digest:
                    raise RunFinalizationError("child checksums conflict")
        return covered

    def _read(self, spec: ExperimentSpec, run: RunLock, path: str) -> bytes:
        staged = self._staged.get((spec.artifacts.bucket, run.artifact_prefix, path))
        if staged is not None:
            return staged
        return self.reader.read_bytes(
            bucket=spec.artifacts.bucket,
            prefix=run.artifact_prefix,
            path=path,
        )


@dataclass(frozen=True)
class _AttemptRecord:
    evidence: AttemptEvidence
    physical_attempt: PhysicalAttemptReference


@dataclass(frozen=True)
class _TrialRecords:
    trial: TrialEvidence
    attempts: list[AttemptEvidence]
    physical_attempts: list[PhysicalAttemptReference]
    metrics: list[MetricEvidence]
    verification_trials: list[object]
    completed_at: datetime


def _require_scored_outcome(outcome: TaskOutcome | None, trial_id: str) -> None:
    if outcome != "scored":
        raise RunFinalizationError(f"complete trial has no scored outcome: {trial_id}")


def _validate_native_bundle_paths(manifest: HarborNativeBundle) -> None:
    if (
        manifest.archive.path != "artifacts.tar.gz"
        or manifest.compatibility.path != "harbor-compatibility.json"
    ):
        raise RunFinalizationError(
            "execution Harbor native bundle uses noncanonical paths"
        )


def _execution_evidence(
    run: RunLock,
    lock: ExecutionLock,
    completed_at: datetime,
    *,
    quality: ExecutionQuality,
) -> ExecutionEvidence:
    deployment = lock.deployment
    if isinstance(deployment, DeploymentProfile):
        provider = deployment.provider
        region = deployment.region
        hardware = deployment.hardware
        accelerators = deployment.accelerator_count
    else:
        provider = deployment.service
        region = "not_reported"
        hardware = "not_reported"
        accelerators = 0
    agent_revision = (
        cast(str, lock.agent.reported_version)
        if lock.agent.revision_kind == "harbor-source"
        else lock.agent.revision
    )
    return ExecutionEvidence(
        execution_id=lock.execution_id,
        run_id=run.run_id,
        experiment=lock.experiment,
        evaluation_id=lock.evaluation_id,
        publication_role=lock.publication_role,
        component_kind=lock.component_kind,
        benchmark=lock.benchmark_dataset,
        benchmark_revision=lock.benchmark_dataset_digest,
        quality=quality,
        created_at=lock.created_at,
        completed_at=completed_at,
        model_id=lock.model.id,
        model_repo=lock.model.repo,
        model_revision=(
            lock.model.revision
            if isinstance(deployment, DeploymentProfile)
            else "not_observed"
        ),
        deployment_id=deployment.id,
        provider=provider,
        region=region,
        hardware=hardware,
        accelerator_count=accelerators,
        agent_id=lock.agent.id,
        agent_name=lock.agent.name,
        agent_revision=agent_revision,
    )


def _validate_execution_identity(
    run: RunLock,
    expected: RunExecutionLock,
    observed: ExecutionLock,
) -> None:
    identity = (
        observed.execution_id,
        observed.model.id,
        observed.deployment.id,
        observed.agent.id,
    )
    locked = (
        expected.execution_id,
        expected.model,
        expected.deployment,
        expected.agent,
    )
    classification = (
        observed.evaluation_id,
        observed.publication_role,
        observed.component_kind,
    )
    run_classification = (
        run.evaluation_id,
        run.publication_role,
        run.component_kind,
    )
    if (
        identity != locked
        or classification != run_classification
        or observed.created_at != run.created_at
    ):
        raise RunFinalizationError("execution evidence does not match run lock")


def _under(paths: list[str], prefix: str) -> list[str]:
    root = f"{prefix}/"
    return sorted(path.removeprefix(root) for path in paths if path.startswith(root))


def _marker(paths: list[str], prefix: str) -> str:
    markers = {
        PurePosixPath(path).name
        for path in paths
        if str(PurePosixPath(path).parent) == prefix
        and PurePosixPath(path).name in _TERMINAL_MARKERS
    }
    if len(markers) != 1:
        raise RunFinalizationError("attempt has no exclusive terminal marker")
    return markers.pop()


def _json_lines(value: bytes) -> list[dict[str, object]]:
    try:
        return [
            _JSON_OBJECT.validate_json(line)
            for line in value.splitlines()
            if line.strip()
        ]
    except Exception as error:
        raise RunFinalizationError("attempt event log is invalid") from error


def _event_time(records: list[dict[str, object]], *names: str) -> datetime:
    for record in records:
        if record.get("event") not in names:
            continue
        value = record.get("at")
        if not isinstance(value, str):
            break
        observed = datetime.fromisoformat(value)
        if observed.tzinfo is None:
            break
        return observed.astimezone(UTC)
    raise RunFinalizationError("attempt event log omits a required timestamp")


def _artifact(
    execution_id: str,
    kind: ArtifactKind,
    path: str,
    content: bytes,
) -> ArtifactEvidence:
    return ArtifactEvidence(
        owner_type="execution",
        owner_id=execution_id,
        kind=kind,
        path=path,
        sha256=_sha256(content),
        media_type="application/json",
        size_bytes=len(content),
    )


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _reward_metrics(
    trial_id: str, verifier: Mapping[object, object]
) -> list[MetricEvidence]:
    rewards = verifier.get("rewards")
    if not isinstance(rewards, Mapping):
        raise RunFinalizationError("trial verification has no rewards")
    metrics: list[MetricEvidence] = []
    for name, value in sorted(rewards.items(), key=lambda item: str(item[0])):
        if (
            not isinstance(name, str)
            or not isinstance(value, int | float)
            or isinstance(value, bool)
        ):
            raise RunFinalizationError("trial reward evidence is invalid")
        metrics.append(
            MetricEvidence(
                owner_type="trial",
                owner_id=trial_id,
                name=name,
                value=float(value),
                unit="score",
            )
        )
    return metrics


def _selected_verifier(value: Mapping[str, object]) -> Mapping[object, object]:
    trials = value.get("trials")
    if not isinstance(trials, list) or len(trials) != 1:
        raise RunFinalizationError("trial verification evidence is invalid")
    verifier = trials[0]
    if not isinstance(verifier, Mapping):
        raise RunFinalizationError("trial verification record is invalid")
    return cast(Mapping[object, object], verifier)
