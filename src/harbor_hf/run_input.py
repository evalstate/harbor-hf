from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harbor_hf.benchmark_source import (
    BenchmarkSourceLock,
    load_source_lock,
    resolved_experiment,
    source_lock_bytes,
)
from harbor_hf.io import load_experiment
from harbor_hf.models import ExperimentSpec
from harbor_hf.runs import RunLock, build_run_lock, build_run_plan

_INPUT_FILES = ("manifest.yaml", "run.lock.json", "source.lock.json")


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InputFile(FrozenModel):
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RunInputManifest(FrozenModel):
    schema_version: Literal["harbor-hf/run-input/v1alpha1"] = (
        "harbor-hf/run-input/v1alpha1"
    )
    run_id: str
    plan_digest: str
    files: dict[str, InputFile]
    input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def file_set_is_exact(self) -> RunInputManifest:
        if tuple(sorted(self.files)) != _INPUT_FILES:
            raise ValueError("run input manifest has an unexpected file set")
        expected = run_input_digest(self.files)
        if self.input_digest != expected:
            raise ValueError("run input digest does not match its files")
        return self


class ValidatedRunInput(FrozenModel):
    requested_spec: ExperimentSpec
    spec: ExperimentSpec
    source_lock: BenchmarkSourceLock
    lock: RunLock
    manifest: RunInputManifest


def write_run_input(
    destination: Path,
    *,
    request: bytes,
    lock: RunLock,
) -> RunInputManifest:
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError("run input destination must be a real directory")
        if any(destination.iterdir()):
            raise ValueError("run input destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / "manifest.yaml"
    source_lock_path = destination / "source.lock.json"
    lock_path = destination / "run.lock.json"
    manifest_path.write_bytes(request)
    source_lock_path.write_bytes(source_lock_bytes(lock.source_lock))
    lock_path.write_text(
        json.dumps(lock.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files = {
        path.name: _file_record(path)
        for path in sorted(
            (lock_path, manifest_path, source_lock_path), key=lambda value: value.name
        )
    }
    input_manifest = RunInputManifest(
        run_id=lock.run_id,
        plan_digest=lock.plan_digest,
        files=files,
        input_digest=run_input_digest(files),
    )
    (destination / "input-manifest.json").write_text(
        json.dumps(input_manifest.model_dump(mode="json"), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return input_manifest


def validate_run_input(root: Path) -> ValidatedRunInput:
    _validate_run_input_paths(root)
    input_manifest = RunInputManifest.model_validate_json(
        (root / "input-manifest.json").read_text(encoding="utf-8")
    )
    for name, expected in input_manifest.files.items():
        if _file_record(root / name) != expected:
            raise ValueError(f"run input file does not match its digest: {name}")
    requested_spec = load_experiment(root / "manifest.yaml")
    source_lock = load_source_lock(root / "source.lock.json")
    spec = resolved_experiment(requested_spec, source_lock)
    lock = RunLock.model_validate_json(
        (root / "run.lock.json").read_text(encoding="utf-8")
    )
    if source_lock != lock.source_lock:
        raise ValueError("run input source lock does not match its run")
    expected_lock = build_run_lock(
        build_run_plan(
            requested_spec,
            source_lock=source_lock,
            recovery_policy=lock.recovery_policy,
        ),
        lock.run_id,
        clock=lambda: lock.created_at,
    )
    if lock != expected_lock:
        raise ValueError("run input lock is not reproducible from its manifest")
    if (
        input_manifest.run_id != lock.run_id
        or input_manifest.plan_digest != lock.plan_digest
    ):
        raise ValueError("run input identity does not match its lock")
    return ValidatedRunInput(
        requested_spec=requested_spec,
        spec=spec,
        source_lock=source_lock,
        lock=lock,
        manifest=input_manifest,
    )


def _validate_run_input_paths(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("run input root must be a real directory")
    entries = sorted(root.iterdir(), key=lambda path: path.name)
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ValueError("run input cannot contain symlinks or directories")
    if [path.name for path in entries] != [
        "input-manifest.json",
        "manifest.yaml",
        "run.lock.json",
        "source.lock.json",
    ]:
        raise ValueError("run input must contain exactly four files")


def run_input_json_schema() -> dict[str, object]:
    return RunInputManifest.model_json_schema()


def run_input_digest(files: dict[str, InputFile]) -> str:
    payload = json.dumps(
        {name: files[name].model_dump(mode="json") for name in sorted(files)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _file_record(path: Path) -> InputFile:
    content = path.read_bytes()
    return InputFile(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
