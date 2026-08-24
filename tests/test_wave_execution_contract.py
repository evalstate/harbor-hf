from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from conftest import write_fake_compatibility_bundle

from harbor_hf.control import RunSubmittedPayload, new_event
from harbor_hf.models import EndpointRef, ExperimentSpec, SourcePin
from harbor_hf.process import CommandRunner
from harbor_hf.reconciler import plan_reconciliation
from harbor_hf.runs import (
    RunLock,
    WaveLock,
    build_run_lock,
    build_run_plan,
    build_wave_lock,
)
from harbor_hf.wave_worker import run_wave_worker


def _endpoint_snapshot(state: str, ready: int) -> dict[str, object]:
    return {
        "model": {
            "repository": "nvidia/Qwen3.6-35B-A3B-NVFP4",
            "revision": "0123456789abcdef0123456789abcdef01234567",
            "image": {
                "custom": {
                    "url": "ghcr.io/example/vllm@sha256:" + "0" * 64,
                }
            },
            "args": [
                "--model",
                "/repository",
                "--max-model-len",
                "65536",
                "--kv-cache-dtype",
                "fp8",
            ],
            "env": {"VLLM_USE_FLASHINFER_MOE_FP4": "1"},
            "secrets": {"HF_TOKEN": "configured"},
        },
        "provider": {"vendor": "aws", "region": "us-east-1"},
        "compute": {
            "instanceType": "nvidia-rtx-pro-6000",
            "instanceSize": "x1",
            "scaling": {"minReplica": 0, "maxReplica": 1},
        },
        "status": {
            "state": state,
            "readyReplica": ready,
            "targetReplica": 1,
            "url": "https://endpoint.example",
        },
        "healthRoute": "/ready",
    }


class EndpointRunner:
    def __init__(self) -> None:
        self.descriptions = [
            _endpoint_snapshot("paused", 0),
            _endpoint_snapshot("running", 1),
            _endpoint_snapshot("paused", 0),
        ]
        self.calls: list[tuple[list[str], float | None]] = []

    def run_json(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        normalized = list(command)
        self.calls.append((normalized, timeout_seconds))
        if normalized[2] == "describe":
            return self.descriptions.pop(0)
        return _endpoint_snapshot(
            "running" if normalized[2] == "resume" else "paused", 0
        )

    def run_text(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        raise AssertionError((command, timeout_seconds))


class HarborStream:
    def __init__(self, task_digest: str) -> None:
        self.task_digest = task_digest
        self.calls: list[tuple[list[str], Path, dict[str, str], int]] = []

    def __call__(
        self,
        command: list[str],
        log_path: Path,
        *,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> int:
        if "--output" in command and "--request-digest" in command:
            write_fake_compatibility_bundle(command, log_path)
            return 0
        self.calls.append((command, log_path, environment, timeout_seconds))
        config = json.loads(
            Path(command[command.index("--config") + 1]).read_text(encoding="utf-8")
        )
        task_name = config["datasets"][0]["task_names"][0]
        jobs_dir = Path(config["jobs_dir"])
        result_root = jobs_dir / "job-contract" / "trial-contract"
        result_root.mkdir(parents=True)
        (result_root / "result.json").write_text(
            json.dumps(
                {
                    "task_name": task_name,
                    "agent_info": {
                        "name": "openclaw",
                        "version": "2026.7.2",
                        "model_info": {
                            "provider": "openai",
                            "name": "/repository",
                        },
                    },
                    "verifier_result": {"rewards": {"reward": 0.75, "contract": 1.0}},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (result_root / "lock.json").write_text(
            json.dumps({"task": {"digest": self.task_digest}}, sort_keys=True),
            encoding="utf-8",
        )
        workspace = result_root / "artifacts" / "workspace" / "app" / "output"
        workspace.mkdir(parents=True)
        (workspace / "answer.txt").write_text("answer\n", encoding="utf-8")
        sessions = result_root / "agent" / "openclaw-sessions"
        sessions.mkdir(parents=True)
        (sessions / "session.jsonl").write_text('{"role":"assistant"}\n')
        (result_root / "agent" / "trajectory.json").write_text("{}\n")
        verifier = result_root / "verifier"
        verifier.mkdir()
        (verifier / "scorecard.json").write_text('{"passed":true}\n')
        (verifier / "reward.txt").write_text("1\n")
        (verifier / "test-stdout.txt").write_text("ok\n")
        (verifier / "test-stderr.txt").write_text("")
        log_path.write_text("wave completed with contract-token\n", encoding="utf-8")
        return 0


def _prepare_source(
    source: SourcePin, destination: Path, runner: CommandRunner
) -> None:
    del source, runner
    destination.mkdir(parents=True)
    (destination / "uv.lock").write_text("locked\n", encoding="utf-8")


def _launch_watchdog(lock: WaveLock, endpoint: EndpointRef, token: str) -> str:
    assert lock.run_id == "run-contract"
    assert endpoint.name == "qwen-endpoint"
    assert token == "contract-token"
    return "watchdog-contract"


def _wave_inputs(
    remote_spec: ExperimentSpec, root: Path
) -> tuple[ExperimentSpec, RunLock, WaveLock, Path, Path, Path]:
    spec = remote_spec.model_copy(
        update={
            "execution": remote_spec.execution.model_copy(
                update={
                    "attempts": 1,
                    "concurrent_trials": 1,
                    "max_trials_per_shard": 1,
                }
            )
        }
    )
    run = build_run_lock(
        build_run_plan(spec),
        "run-contract",
        clock=lambda: datetime(2026, 7, 14, 0, 0, tzinfo=UTC),
    )
    submitted = new_event(
        subject_type="run",
        subject_id=run.run_id,
        kind="run.submitted",
        producer="cli",
        payload=RunSubmittedPayload(plan_digest=run.plan_digest),
    )
    action = plan_reconciliation(run, [submitted])[1].actions[0]
    wave = build_wave_lock(run, spec, action)
    manifest = root / "manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(spec.model_dump(mode="json", exclude_none=True)),
        encoding="utf-8",
    )
    run_path = root / "run.lock.json"
    run_path.write_text(run.model_dump_json(), encoding="utf-8")
    wave_path = root / "wave.lock.json"
    wave_path.write_text(wave.model_dump_json(), encoding="utf-8")
    return spec, run, wave, manifest, run_path, wave_path


def _events(path: Path) -> list[dict[str, object]]:
    return [
        {key: value for key, value in json.loads(line).items() if key != "at"}
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _expected_checksums(root: Path) -> dict[str, str]:
    excluded = {"checksums.json", "_SUCCESS", "_FAILED", "_CANCELLED"}
    return {
        str(path.relative_to(root)): _digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and str(path.relative_to(root)) not in excluded
    }


def test_wave_execution_publishes_complete_linked_evidence_contract(
    remote_spec: ExperimentSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, run, wave, manifest, run_path, wave_path = _wave_inputs(remote_spec, tmp_path)
    monkeypatch.setenv("HF_TOKEN", "contract-token")
    monkeypatch.setattr(
        "harbor_hf.worker.probe_runtime",
        lambda url, token, route, *_deadline: {
            "contract_url": url,
            "contract_token": token,
            "contract_route": route,
        },
    )
    runner = EndpointRunner()
    task_name, task_digest = next(iter(spec.benchmark.task_digests.items()))
    stream = HarborStream(task_digest)
    output = tmp_path / "published"

    destination = run_wave_worker(
        manifest,
        run_path,
        wave_path,
        output,
        runner=runner,
        stream_runner=stream,
        source_preparer=_prepare_source,
        watchdog_launcher=_launch_watchdog,
        identifier=lambda: "1234567890abcdef1234567890abcdef",
        clock=lambda: datetime(2026, 7, 14, 1, 2, 3, tzinfo=UTC),
        monotonic=lambda: 1000.0,
    )

    execution = wave.executions[0]
    shard = execution.shards[0].shard
    trial = shard.trials[0]
    attempt_id = "exec-1234567890abcdef1234567890abcdef"
    run_root = output / run.artifact_prefix
    execution_root = output / execution.artifact_prefix
    wave_root = output / wave.artifact_prefix
    shard_root = output / execution.shards[0].artifact_prefix
    trial_root = execution_root / "trials" / trial.trial_id
    attempt_root = trial_root / "attempts" / attempt_id

    assert destination == wave_root
    expected_files = {
        "run.lock.json",
        "run.lock.json.sha256",
        "source.lock.json",
        "source.lock.json.sha256",
        f"executions/{execution.configuration.execution_id}/execution.lock.json",
        f"executions/{execution.configuration.execution_id}/execution.lock.json.sha256",
        f"executions/{execution.configuration.execution_id}/shards/{shard.shard_id}/_SUCCESS",
        f"executions/{execution.configuration.execution_id}/shards/{shard.shard_id}/checksums.json",
        f"executions/{execution.configuration.execution_id}/shards/{shard.shard_id}/events.jsonl",
        f"executions/{execution.configuration.execution_id}/shards/{shard.shard_id}/shard-summary.json",
        f"executions/{execution.configuration.execution_id}/shards/{shard.shard_id}/shard.lock.json",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/_SUCCESS",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/checksums.json",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/events.jsonl",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/trial-summary.json",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/trial.lock.json",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/_SUCCESS",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/artifacts.tar.gz",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/checksums.json",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/events.jsonl",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/attempt.lock.json",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/harbor-compatibility.json",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/harbor-native-bundle.json",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/harbor-export.log",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/harbor-job.json",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/harbor-jobs/job-contract/trial-contract/lock.json",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/harbor-jobs/job-contract/trial-contract/result.json",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/harbor.log",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/harbor-request.json",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/manifest.yaml",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/private-artifacts.json",
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/verification.json",
        f"waves/{wave.wave_id}/_SUCCESS",
        f"waves/{wave.wave_id}/checksums.json",
        f"waves/{wave.wave_id}/endpoint.final.json",
        f"waves/{wave.wave_id}/endpoint.snapshot.json",
        f"waves/{wave.wave_id}/events.jsonl",
        f"waves/{wave.wave_id}/runtime-environment.json",
        f"waves/{wave.wave_id}/wave-summary.json",
        f"waves/{wave.wave_id}/wave.lock.json",
    }
    native = (
        f"executions/{execution.configuration.execution_id}/trials/{trial.trial_id}/attempts/"
        f"{attempt_id}/harbor-jobs/job-contract/trial-contract"
    )
    expected_files.update(
        {
            f"{native}/agent/openclaw-sessions/session.jsonl",
            f"{native}/agent/trajectory.json",
            f"{native}/evidence/manifest.json",
            f"{native}/evidence/workspace-files.jsonl",
            f"{native}/evidence/workspace.tar.zst",
            f"{native}/verifier/reward.txt",
            f"{native}/verifier/scorecard.json",
            f"{native}/verifier/test-stderr.txt",
            f"{native}/verifier/test-stdout.txt",
        }
    )
    assert {
        str(path.relative_to(run_root))
        for path in run_root.rglob("*")
        if path.is_file()
    } == expected_files

    assert json.loads((attempt_root / "attempt.lock.json").read_text()) == {
        "schema_version": "harbor-hf/attempt-lock/v1alpha1",
        "attempt_id": attempt_id,
        "created_at": "2026-07-14T01:02:03Z",
        "run_id": run.run_id,
        "wave_id": wave.wave_id,
        "execution_id": execution.configuration.execution_id,
        "shard_id": shard.shard_id,
        "trial_id": trial.trial_id,
        "task_name": task_name,
        "task_digest": task_digest,
        "logical_attempt": 1,
        "physical_attempt": 1,
        "remote_job_id": "test-wave-job",
    }
    assert _events(attempt_root / "events.jsonl") == [
        {"event": "attempt_started", "attempt_id": attempt_id},
        {"event": "harbor_started"},
        {"event": "harbor_finished", "exit_code": 0},
        {"event": "trial_evidence_validated", "trial_id": trial.trial_id},
        {"event": "attempt_succeeded"},
        {"event": "secrets_redacted", "files": ["harbor.log"]},
    ]
    private_artifacts = json.loads(
        (attempt_root / "private-artifacts.json").read_text(encoding="utf-8")
    )
    assert private_artifacts["schema_version"] == "harbor-hf/private-artifacts/v1"
    assert private_artifacts["attempt_id"] == attempt_id
    assert private_artifacts["trial_id"] == trial.trial_id
    assert private_artifacts["requirements"] == [
        {
            "name": "openclaw_session_jsonl",
            "paths": [
                "harbor-jobs/job-contract/trial-contract/agent/"
                "openclaw-sessions/session.jsonl"
            ],
            "required": False,
            "satisfied": True,
        },
        {
            "name": "trial_evidence_complete",
            "paths": ["harbor-jobs/job-contract/trial-contract/evidence/manifest.json"],
            "required": True,
            "satisfied": True,
        },
    ]
    assert all(
        entry["classification"] == "private" for entry in private_artifacts["entries"]
    )
    assert _events(trial_root / "events.jsonl") == [{"event": "trial_succeeded"}]
    assert _events(shard_root / "events.jsonl") == [
        {"event": "shard_started", "shard_id": shard.shard_id},
        {"event": "trial_completed", "trial_id": trial.trial_id},
        {"event": "shard_succeeded"},
    ]
    assert _events(wave_root / "events.jsonl") == [
        {"event": "wave_started", "wave_id": wave.wave_id},
        {"event": "endpoint_baseline_validated"},
        {
            "event": "endpoint_lease_acquired",
            "watchdog_job_id": "watchdog-contract",
        },
        {"event": "cleanup_watchdog_started", "job_id": "watchdog-contract"},
        {"event": "endpoint_resume_requested"},
        {"event": "endpoint_ready", "state": "running"},
        {"event": "runtime_probed"},
        {"event": "endpoint_pause_requested"},
        {
            "event": "endpoint_paused",
            "state": "paused",
            "ready_replicas": 0,
            "target_replicas": 1,
        },
        {"event": "wave_succeeded"},
    ]

    execution_summary = json.loads((trial_root / "trial-summary.json").read_text())
    assert execution_summary == {
        "trial_id": trial.trial_id,
        "attempt_id": attempt_id,
        "attempt_checksum": _digest(attempt_root / "checksums.json"),
    }
    shard_summary = json.loads((shard_root / "shard-summary.json").read_text())
    assert shard_summary == {
        "run_id": run.run_id,
        "execution_id": execution.configuration.execution_id,
        "shard_id": shard.shard_id,
        "trial_checksums": {trial.trial_id: _digest(trial_root / "checksums.json")},
    }
    wave_summary = json.loads((wave_root / "wave-summary.json").read_text())
    assert wave_summary == {
        "wave_id": wave.wave_id,
        "run_id": run.run_id,
        "shard_checksums": {shard.shard_id: _digest(shard_root / "checksums.json")},
        "endpoint_cleanup_verified": True,
    }

    for evidence_root in (attempt_root, trial_root, shard_root, wave_root):
        assert json.loads((evidence_root / "checksums.json").read_text()) == (
            _expected_checksums(evidence_root)
        )
    assert (run_root / "run.lock.json.sha256").read_text() == (
        _digest(run_root / "run.lock.json") + "\n"
    )
    assert (execution_root / "execution.lock.json.sha256").read_text() == (
        _digest(execution_root / "execution.lock.json") + "\n"
    )

    assert len(stream.calls) == 1
    command, log_path, environment, timeout_seconds = stream.calls[0]
    config = json.loads((attempt_root / "harbor-job.json").read_text(encoding="utf-8"))
    assert config["datasets"][0]["task_names"] == [task_name]
    assert Path(config["jobs_dir"]).name == "harbor-jobs"
    assert log_path.name == "harbor.log"
    assert environment == {
        "HF_TOKEN": "contract-token",
        "OPENAI_API_KEY": "contract-token",
        "OPENAI_BASE_URL": "https://endpoint.example/v1",
    }
    assert timeout_seconds == wave.duration_seconds
    assert [call[0][2] for call in runner.calls] == [
        "describe",
        "resume",
        "describe",
        "pause",
        "describe",
    ]
    assert (attempt_root / "harbor.log").read_text() == (
        "wave completed with [REDACTED]\n"
    )
    assert all(
        b"contract-token" not in path.read_bytes()
        for path in run_root.rglob("*")
        if path.is_file()
    )
