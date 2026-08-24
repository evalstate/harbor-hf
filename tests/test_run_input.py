import json
from pathlib import Path

import pytest

from harbor_hf.models import ExperimentSpec
from harbor_hf.run_input import validate_run_input, write_run_input
from harbor_hf.runs import build_run_lock, build_run_plan


def _input(tmp_path: Path, remote_manifest: Path, remote_spec: ExperimentSpec) -> Path:
    lock = build_run_lock(build_run_plan(remote_spec), "run-input")
    root = tmp_path / "run-input"
    write_run_input(
        root,
        request=remote_manifest.read_bytes(),
        lock=lock,
    )
    return root


def test_run_input_is_exact_content_addressed_and_reproducible(
    tmp_path: Path,
    remote_manifest: Path,
    remote_spec: ExperimentSpec,
) -> None:
    root = _input(tmp_path, remote_manifest, remote_spec)

    validated = validate_run_input(root)

    assert validated.lock.run_id == "run-input"
    assert validated.manifest.plan_digest == validated.lock.plan_digest
    assert set(validated.manifest.files) == {
        "run.lock.json",
        "manifest.yaml",
        "source.lock.json",
    }
    assert validated.manifest.input_digest.startswith("sha256:")
    assert set(path.name for path in root.iterdir()) == {
        "run.lock.json",
        "input-manifest.json",
        "manifest.yaml",
        "source.lock.json",
    }


def test_run_input_rejects_extra_files_symlinks_and_changed_bytes(
    tmp_path: Path,
    remote_manifest: Path,
    remote_spec: ExperimentSpec,
) -> None:
    root = _input(tmp_path, remote_manifest, remote_spec)
    (root / "extra.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly four files"):
        validate_run_input(root)
    (root / "extra.txt").unlink()

    manifest = root / "manifest.yaml"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="does not match its digest"):
        validate_run_input(root)

    manifest.write_bytes(remote_manifest.read_bytes())
    link = root / "extra-link"
    link.symlink_to(root / "manifest.yaml")
    with pytest.raises(ValueError, match="symlinks"):
        validate_run_input(root)


def test_run_input_rejects_a_mismatched_identity(
    tmp_path: Path,
    remote_manifest: Path,
    remote_spec: ExperimentSpec,
) -> None:
    root = _input(tmp_path, remote_manifest, remote_spec)
    path = root / "input-manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["run_id"] = "other-run"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="identity does not match"):
        validate_run_input(root)
