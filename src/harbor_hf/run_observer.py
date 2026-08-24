from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Protocol, cast

from pydantic import TypeAdapter, ValidationError

from harbor_hf.control import (
    AttemptOutcomePayload,
    AttemptStartedPayload,
    EventKind,
    EventPayload,
    RetryCategory,
    RunEvent,
    SubjectType,
    WaveLifecyclePayload,
    new_event,
)
from harbor_hf.models import ExperimentSpec
from harbor_hf.results import EvidenceReader
from harbor_hf.runs import (
    RunLock,
    WaveLock,
    estimated_partial_wave_cost,
)
from harbor_hf.wave_worker import AttemptLock

_JSON_OBJECT = TypeAdapter(dict[str, object])
_RETRY_CATEGORY = TypeAdapter(RetryCategory)
_TERMINAL_MARKERS = frozenset({"_SUCCESS", "_FAILED", "_CANCELLED"})
_OBSERVATION_FILES = frozenset(
    {
        "_FAILED",
        "checksums.json",
        "events.jsonl",
        "attempt.lock.json",
        "failure.json",
        "verification.json",
        "wave-summary.json",
        "wave.lock.json",
    }
)


class RunObservationError(RuntimeError):
    """Raised when terminal Bucket evidence cannot be projected safely."""


class RunObserver(Protocol):
    def observe(self, lock: RunLock, spec: ExperimentSpec) -> list[RunEvent]: ...


class BucketRunObserver:
    """Derive compact, deterministic control events from terminal Bucket units."""

    def __init__(self, reader: EvidenceReader) -> None:
        self.reader = reader

    def refresh(self) -> None:
        refresh = getattr(self.reader, "refresh", None)
        if callable(refresh):
            refresh()

    def observe(self, lock: RunLock, spec: ExperimentSpec) -> list[RunEvent]:
        paths = self.reader.list_files(
            bucket=spec.artifacts.bucket,
            prefix=lock.artifact_prefix,
        )
        prefetch = getattr(self.reader, "prefetch_files", None)
        if callable(prefetch):
            prefetch(
                bucket=spec.artifacts.bucket,
                prefix=lock.artifact_prefix,
                paths=[
                    path
                    for path in paths
                    if PurePosixPath(path).name in _OBSERVATION_FILES
                ],
            )
        events: list[RunEvent] = []
        for path in _wave_lock_paths(paths):
            wave_prefix = str(PurePosixPath(path).parent)
            marker = _terminal_marker(paths, wave_prefix)
            if marker is None:
                continue
            wave = self._load_wave_lock(spec, lock, paths, path)
            if wave.run_id != lock.run_id:
                raise RunObservationError("wave evidence belongs to another run")
            self._verify_critical_unit(
                spec,
                lock,
                paths,
                wave_prefix,
                {"wave.lock.json", "events.jsonl", "wave-summary.json"},
            )
            raw_wave_events = _json_lines(
                self._read(spec, lock, f"{wave_prefix}/events.jsonl")
            )
            events.extend(_wave_events(lock, wave, marker, raw_wave_events))
        events.extend(self._attempt_events(spec, lock, paths))
        return sorted(events, key=lambda event: (event.observed_at, event.event_id))

    def _load_wave_lock(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        paths: list[str],
        path: str,
    ) -> WaveLock:
        content = self._read(spec, run, path)
        try:
            return WaveLock.model_validate_json(content)
        except ValidationError as original:
            raw = _JSON_OBJECT.validate_json(content)
            if raw.get("action_kind") != "retry-shard" or raw.get("trial_ids"):
                raise RunObservationError("wave lock evidence is invalid") from original
            wave_id = raw.get("wave_id")
            if not isinstance(wave_id, str):
                raise RunObservationError("wave lock evidence is invalid") from original
            trial_ids = self._legacy_retry_trial_ids(spec, run, paths, wave_id)
            if not trial_ids:
                raise RunObservationError(
                    "legacy retry wave has no matching execution evidence"
                ) from original
            raw["trial_ids"] = trial_ids
            try:
                return WaveLock.model_validate(raw)
            except ValidationError as error:
                raise RunObservationError("wave lock evidence is invalid") from error

    def _legacy_retry_trial_ids(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        paths: list[str],
        wave_id: str,
    ) -> list[str]:
        trial_ids: set[str] = set()
        for attempt_path in _attempt_lock_paths(paths):
            attempt = AttemptLock.model_validate_json(
                self._read(spec, run, attempt_path)
            )
            if attempt.wave_id == wave_id:
                trial_ids.add(attempt.trial_id)
        return sorted(trial_ids)

    def _attempt_events(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        paths: list[str],
    ) -> list[RunEvent]:
        events: list[RunEvent] = []
        for path in _attempt_lock_paths(paths):
            prefix = str(PurePosixPath(path).parent)
            marker = _terminal_marker(paths, prefix)
            if marker is None:
                continue
            critical = {"attempt.lock.json", "events.jsonl"}
            if marker == "_SUCCESS":
                critical.add("verification.json")
            elif marker == "_FAILED" and f"{prefix}/failure.json" in paths:
                critical.add("failure.json")
            self._verify_critical_unit(spec, run, paths, prefix, critical)
            attempt = AttemptLock.model_validate_json(self._read(spec, run, path))
            _validate_attempt_identity(run, attempt, path)
            raw_events = _json_lines(self._read(spec, run, f"{prefix}/events.jsonl"))
            failure_message, failure_category = self._failure_details(
                spec, run, paths, prefix, marker
            )
            events.extend(
                _attempt_control_events(
                    run,
                    attempt,
                    marker,
                    raw_events,
                    failure_message,
                    failure_category,
                )
            )
        return events

    def _failure_details(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        paths: list[str],
        prefix: str,
        marker: str,
    ) -> tuple[str | None, RetryCategory | None]:
        if marker != "_FAILED":
            return None, None
        details_path = (
            f"{prefix}/failure.json"
            if f"{prefix}/failure.json" in paths
            else f"{prefix}/_FAILED"
        )
        value = _JSON_OBJECT.validate_json(self._read(spec, run, details_path))
        message = value.get("message")
        error_type = value.get("error_type")
        raw_category = value.get("category")
        if raw_category is None:
            category = _legacy_failure_category(error_type, message)
        else:
            try:
                category = _RETRY_CATEGORY.validate_python(raw_category)
            except ValidationError as error:
                raise RunObservationError(
                    "attempt failure evidence has an invalid retry category"
                ) from error
        return message if isinstance(message, str) else None, category

    def _verify_critical_unit(
        self,
        spec: ExperimentSpec,
        run: RunLock,
        paths: list[str],
        prefix: str,
        critical: set[str],
    ) -> None:
        manifest_path = f"{prefix}/checksums.json"
        if manifest_path not in paths:
            raise RunObservationError("terminal evidence has no checksum manifest")
        try:
            manifest = cast(
                dict[str, str],
                json.loads(self._read(spec, run, manifest_path)),
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RunObservationError(
                "terminal checksum manifest is invalid"
            ) from error
        if not all(
            isinstance(path, str)
            and isinstance(digest, str)
            and digest.startswith("sha256:")
            for path, digest in manifest.items()
        ):
            raise RunObservationError("terminal checksum manifest is invalid")
        if not critical.issubset(manifest):
            raise RunObservationError("terminal checksum manifest is incomplete")
        for relative in sorted(critical):
            content = self._read(spec, run, f"{prefix}/{relative}")
            observed = f"sha256:{hashlib.sha256(content).hexdigest()}"
            if manifest[relative] != observed:
                raise RunObservationError(
                    f"terminal evidence checksum mismatch: {prefix}/{relative}"
                )

    def _read(self, spec: ExperimentSpec, run: RunLock, path: str) -> bytes:
        return self.reader.read_bytes(
            bucket=spec.artifacts.bucket,
            prefix=run.artifact_prefix,
            path=path,
        )


def _wave_lock_paths(paths: list[str]) -> list[str]:
    return sorted(
        path
        for path in paths
        if len(PurePosixPath(path).parts) == 3
        and PurePosixPath(path).parts[0] == "waves"
        and PurePosixPath(path).name == "wave.lock.json"
    )


def _attempt_lock_paths(paths: list[str]) -> list[str]:
    return sorted(
        path
        for path in paths
        if PurePosixPath(path).name == "attempt.lock.json"
        and "attempts" in PurePosixPath(path).parts
    )


def _terminal_marker(paths: list[str], prefix: str) -> str | None:
    markers = {
        PurePosixPath(path).name
        for path in paths
        if str(PurePosixPath(path).parent) == prefix
        and PurePosixPath(path).name in _TERMINAL_MARKERS
    }
    if not markers:
        return None
    if len(markers) != 1:
        raise RunObservationError("terminal evidence has conflicting markers")
    return markers.pop()


def _validate_attempt_identity(lock: RunLock, attempt: AttemptLock, path: str) -> None:
    for execution in lock.executions:
        if execution.execution_id != attempt.execution_id:
            continue
        for shard in execution.shards:
            if shard.shard_id != attempt.shard_id:
                continue
            for trial in shard.trials:
                if trial.trial_id != attempt.trial_id:
                    continue
                expected_path = (
                    f"executions/{execution.execution_id}/trials/{trial.trial_id}/"
                    f"attempts/{attempt.attempt_id}/attempt.lock.json"
                )
                observed = (
                    attempt.run_id,
                    attempt.task_name,
                    attempt.task_digest,
                    attempt.logical_attempt,
                    path,
                )
                expected = (
                    lock.run_id,
                    trial.task_name,
                    trial.task_digest,
                    trial.logical_attempt,
                    expected_path,
                )
                if observed == expected:
                    return
    raise RunObservationError("execution evidence does not match run lock")


def _json_lines(value: bytes) -> list[dict[str, object]]:
    try:
        records = [
            _JSON_OBJECT.validate_json(line)
            for line in value.splitlines()
            if line.strip()
        ]
    except ValidationError as error:
        raise RunObservationError("lifecycle event log is invalid") from error
    if not records:
        raise RunObservationError("lifecycle event log is empty")
    return records


def _event_time(records: list[dict[str, object]], *names: str) -> datetime:
    for record in records:
        if record.get("event") not in names:
            continue
        value = record.get("at")
        if not isinstance(value, str):
            break
        try:
            observed = datetime.fromisoformat(value)
        except ValueError as error:
            raise RunObservationError("lifecycle event timestamp is invalid") from error
        if observed.tzinfo is None:
            raise RunObservationError("lifecycle event timestamp has no timezone")
        return observed.astimezone(UTC)
    raise RunObservationError(
        "lifecycle event log omits required events: " + ", ".join(names)
    )


def _wave_events(
    run: RunLock,
    wave: WaveLock,
    marker: str,
    records: list[dict[str, object]],
) -> list[RunEvent]:
    provider = (
        next(
            run.provider
            for run in run.executions
            if run.deployment_digest == wave.deployment_digest
        )
        or "hf-inference-endpoints"
    )
    estimated_cost = wave.estimated_cost_microusd
    if wave.action_kind == "retry-shard":
        estimated_cost = estimated_partial_wave_cost(
            run,
            wave.deployment_digest,
            estimated_cost,
            len(wave.trial_ids),
        )
        assert estimated_cost is not None
    payload = WaveLifecyclePayload(
        deployment_digest=wave.deployment_digest,
        provider=provider,
        shard_ids=wave.shard_ids,
        estimated_cost_microusd=estimated_cost,
    )
    active = _event_time(records, "wave_started")
    finished = _event_time(records, "wave_succeeded", "wave_failed")
    events = [
        _event(
            run,
            subject_type="wave",
            subject_id=wave.wave_id,
            kind="wave.active",
            payload=payload,
            observed_at=active,
            identity=f"{wave.wave_id}:active",
        )
    ]
    cleanup_incomplete = any(
        record.get("event") in {"endpoint_cleanup_failed", "endpoint_cleanup_skipped"}
        for record in records
    )
    if cleanup_incomplete:
        events.append(
            _event(
                run,
                subject_type="wave",
                subject_id=wave.wave_id,
                kind="wave.cleanup-failed",
                payload=payload,
                observed_at=finished,
                identity=f"{wave.wave_id}:cleanup-failed",
            )
        )
        return events
    cleaning = _optional_event_time(records, "endpoint_pause_requested") or finished
    closed = max(cleaning, finished) + timedelta(microseconds=1)
    events.extend(
        [
            _event(
                run,
                subject_type="wave",
                subject_id=wave.wave_id,
                kind="wave.cleaning",
                payload=payload,
                observed_at=cleaning,
                identity=f"{wave.wave_id}:cleaning",
            ),
            _event(
                run,
                subject_type="wave",
                subject_id=wave.wave_id,
                kind="wave.closed",
                payload=payload,
                observed_at=closed,
                identity=f"{wave.wave_id}:closed:{marker}",
            ),
        ]
    )
    return events


def _attempt_control_events(
    run: RunLock,
    attempt: AttemptLock,
    marker: str,
    records: list[dict[str, object]],
    message: str | None,
    failure_category: RetryCategory | None,
) -> list[RunEvent]:
    started = _event_time(records, "attempt_started")
    finished = _event_time(
        records,
        "attempt_succeeded",
        "attempt_failed",
        "attempt_cancelled",
    )
    start = _event(
        run,
        subject_type="attempt",
        subject_id=attempt.attempt_id,
        kind="attempt.started",
        payload=AttemptStartedPayload(
            trial_id=attempt.trial_id,
            shard_id=attempt.shard_id,
            physical_attempt=attempt.physical_attempt,
            wave_id=attempt.wave_id,
        ),
        observed_at=started,
        identity=f"{attempt.attempt_id}:started",
    )
    if marker == "_SUCCESS":
        kind = "attempt.completed"
        category: RetryCategory | None = None
    elif marker == "_CANCELLED":
        kind = "attempt.cancelled"
        category = None
    else:
        kind = "attempt.failed"
        if failure_category is None:
            raise RunObservationError("attempt failure evidence has no retry category")
        category = failure_category
    outcome = _event(
        run,
        subject_type="attempt",
        subject_id=attempt.attempt_id,
        kind=kind,
        payload=AttemptOutcomePayload(
            trial_id=attempt.trial_id,
            physical_attempt=attempt.physical_attempt,
            category=category,
            message=message,
        ),
        observed_at=finished,
        identity=f"{attempt.attempt_id}:{kind}",
    )
    return [start, outcome]


def _legacy_failure_category(error_type: object, message: object) -> RetryCategory:
    value = f"{error_type or ''} {message or ''}".lower()
    if any(item in value for item in ("authentication", "unauthorized", "forbidden")):
        return "authentication"
    if "ratelimit" in value or "rate_limit" in value or "status=429" in value:
        return "rate-limit"
    if "quota" in value:
        return "quota"
    if any(item in value for item in ("timeout", "connection", "status=5")):
        return "transient"
    if any(item in value for item in ("configuration", "badrequest", "notfound")):
        return "configuration"
    return "benchmark"


def _optional_event_time(
    records: list[dict[str, object]], name: str
) -> datetime | None:
    try:
        return _event_time(records, name)
    except RunObservationError:
        return None


def _event(
    run: RunLock,
    *,
    subject_type: SubjectType,
    subject_id: str,
    kind: EventKind,
    payload: EventPayload,
    observed_at: datetime,
    identity: str,
) -> RunEvent:
    identifier = hashlib.sha256(f"{run.run_id}:{identity}".encode()).hexdigest()[:32]
    return new_event(
        subject_type=subject_type,
        subject_id=subject_id,
        kind=kind,
        producer="wave-controller",
        payload=payload,
        clock=lambda: observed_at,
        identifier=lambda: identifier,
    )
