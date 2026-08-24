from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from huggingface_hub import BucketFile, BucketFolder
from huggingface_hub.errors import HfHubHTTPError

import harbor_hf.run_data_reset as reset_module
from harbor_hf.run_data_reset import (
    SCHEMA_VERSION,
    BucketDeleteError,
    BucketInventoryError,
    ManifestError,
    PrefixConfigurationError,
    ResetConfirmationError,
    ResetInventory,
    ResetVerificationError,
    StaleInventoryError,
    UnknownBucketPathError,
    apply_reset,
    build_inventory,
    classify_key,
    dry_run_manifest,
    list_inventory,
    main,
    read_manifest,
    run_dry_run,
    validate_prefixes,
    write_manifest,
)

_NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


class FakeHfApi:
    """In-memory implementation of the installed Bucket method signatures."""

    def __init__(self, files: dict[str, int], *, expose_xet_hash: bool = True) -> None:
        self.files = dict(files)
        self.contents = {
            key: _default_content(key, size) for key, size in self.files.items()
        }
        self.expose_xet_hash = expose_xet_hash
        self.list_calls = 0
        self.batch_calls: list[list[str]] = []
        self.download_calls: list[list[str]] = []
        self.list_mutations: dict[int, Callable[[], None]] = {}
        self.invalid_listing = False
        self.invalid_batch = False
        self.transient_list_failures = 0
        self.transient_batch_failures = 0
        self.transient_partial_failures = 0
        self.batch_error_status: int | None = None
        self.remove_preserved_after_batch = False
        self.overwrite_preserved_after_batch = False
        self.skip_deletes = False

    def list_bucket_tree(
        self,
        bucket_id: str,
        prefix: str | None = None,
        *,
        recursive: bool | None = None,
        token: str | bool | None = None,
    ) -> Iterable[BucketFile | BucketFolder]:
        assert bucket_id == "example-org/artifact-bucket"
        assert prefix is None
        assert recursive is True
        assert token is None
        self.list_calls += 1
        if self.invalid_listing:
            raise ValueError("invalid listing")
        if self.transient_list_failures:
            self.transient_list_failures -= 1
            request = httpx.Request("GET", "https://huggingface.invalid")
            raise httpx.ConnectError("temporary failure", request=request)
        if self.list_calls in self.list_mutations:
            self.list_mutations[self.list_calls]()
        entries = []
        for key, size in sorted(self.files.items()):
            values: dict[str, object] = {"type": "file", "path": key, "size": size}
            if self.expose_xet_hash:
                values["xet_hash"] = hashlib.sha256(
                    self._content(key, size)
                ).hexdigest()
            entries.append(SimpleNamespace(**values))
        return cast(list[BucketFile | BucketFolder], entries)

    def download_bucket_files(
        self,
        bucket_id: str,
        files: list[tuple[str | BucketFile, str | Path]],
        *,
        raise_on_missing_files: bool = False,
        token: str | bool | None = None,
    ) -> None:
        assert bucket_id == "example-org/artifact-bucket"
        assert raise_on_missing_files is True
        assert token is None
        self.download_calls.append(
            [remote if isinstance(remote, str) else remote.path for remote, _ in files]
        )
        for remote, destination in files:
            key = remote if isinstance(remote, str) else remote.path
            if key not in self.files:
                raise AssertionError(f"missing fake Bucket object: {key}")
            Path(destination).write_bytes(self._content(key, self.files[key]))

    def batch_bucket_files(
        self,
        bucket_id: str,
        *,
        add: list[tuple[str | Path | bytes, str]] | None = None,
        copy: list[tuple[str, str, str, str]] | None = None,
        delete: list[str] | None = None,
        token: str | bool | None = None,
    ) -> None:
        assert bucket_id == "example-org/artifact-bucket"
        assert add is None
        assert copy is None
        assert delete is not None
        assert token is None
        self.batch_calls.append(list(delete))
        self._fail_batch_if_configured(delete)
        if self.skip_deletes:
            return
        for key in delete:
            if key in self.files:
                del self.files[key]
                self.contents.pop(key, None)
        if self.remove_preserved_after_batch:
            del self.files["control/schema=v1/operators/operator.json"]
            self.contents.pop("control/schema=v1/operators/operator.json", None)
        if self.overwrite_preserved_after_batch:
            key = "control/schema=v1/operators/operator.json"
            self.contents[key] = b"x" * self.files[key]

    def _fail_batch_if_configured(self, delete: list[str]) -> None:
        if self.invalid_batch:
            raise ValueError("invalid batch")
        if self.batch_error_status is not None:
            request = httpx.Request("POST", "https://huggingface.invalid")
            response = httpx.Response(self.batch_error_status, request=request)
            raise HfHubHTTPError("batch failed", response=response)
        if self.transient_batch_failures:
            self.transient_batch_failures -= 1
            request = httpx.Request("POST", "https://huggingface.invalid")
            raise httpx.ConnectError("temporary failure", request=request)
        if self.transient_partial_failures:
            self.transient_partial_failures -= 1
            first = delete[0]
            if first in self.files:
                del self.files[first]
            request = httpx.Request("POST", "https://huggingface.invalid")
            raise httpx.ConnectError("temporary failure", request=request)

    def _content(self, key: str, size: int) -> bytes:
        content = self.contents.setdefault(key, _default_content(key, size))
        if len(content) != size:
            return _default_content(key, size)
        return content


def _inventory(files: dict[str, int]) -> ResetInventory:
    entries = [
        SimpleNamespace(
            type="file",
            path=key,
            size=size,
            xet_hash=hashlib.sha256(_default_content(key, size)).hexdigest(),
        )
        for key, size in files.items()
    ]
    return build_inventory(entries)


def _default_content(key: str, size: int) -> bytes:
    seed = hashlib.sha256(key.encode()).digest()
    return (seed * ((size + len(seed) - 1) // len(seed)))[:size]


def _dry_manifest(tmp_path: Path, files: dict[str, int]) -> Path:
    path = tmp_path / "dry-run.json"
    write_manifest(path, dry_run_manifest(_inventory(files), created_at=_NOW))
    return path


def _clock() -> datetime:
    return _NOW


def _no_sleep(_seconds: float) -> None:
    return None


def test_prefix_sets_are_disjoint_and_reject_overlaps() -> None:
    validate_prefixes(("preserved/",), ("deleted/",))

    with pytest.raises(PrefixConfigurationError, match="overlap"):
        validate_prefixes(("control/",), ("control/runs/",))
    with pytest.raises(PrefixConfigurationError, match="overlap"):
        validate_prefixes(("results/", "results/schema=v1/"), ())
    with pytest.raises(PrefixConfigurationError, match="canonical"):
        validate_prefixes(("absolute",), ("deleted/",))


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("control/schema=v1/profiles/objects/model/profile.json", "preserve"),
        ("control/schema=v1/operators/operator.json", "preserve"),
        ("control/schema=v1/auth/operator-acl.json", "preserve"),
        ("control/schema=v1/migrations/migration.json", "preserve"),
        ("benchmark-bundles/sha256/abc/bundle.json", "preserve"),
        ("control/schema=v1/campaigns/campaign/request.json", "delete"),
        ("control/schema=v1/runs/run/request.json", "delete"),
        ("evidence/schema=v1/campaigns/campaign/object", "delete"),
        ("evidence/schema=v1/runs/run/object", "delete"),
        ("campaigns/campaign/runs/run/result.json", "delete"),
        ("runs/run/result.json", "delete"),
        ("sandbox-results/schema=v1/campaign/action/result.json", "delete"),
        ("serving-profiles/profile/points/1/1/evidence.json", "preserve"),
        ("results/schema=v1/leaderboard/snapshots/value.sqlite", "delete"),
        ("imports/schema=v1/migration=one/manifest.json", "delete"),
        ("unreviewed/object.json", "unknown"),
    ],
)
def test_classifies_preserved_old_and_new_run_prefixes(
    key: str,
    expected: str,
) -> None:
    classification, _prefix = classify_key(key)
    assert classification == expected


def test_build_inventory_rejects_duplicate_and_invalid_entries() -> None:
    duplicate = [
        SimpleNamespace(type="file", path="runs/run/a", size=1),
        SimpleNamespace(type="file", path="runs/run/a", size=1),
    ]
    with pytest.raises(BucketInventoryError, match="duplicate"):
        build_inventory(duplicate)

    with pytest.raises(BucketInventoryError, match="metadata"):
        build_inventory([SimpleNamespace(type="file", path="/runs/run/a", size=1)])


def test_build_inventory_ignores_directories_and_rejects_unknown_entry_types() -> None:
    inventory = build_inventory(
        [SimpleNamespace(type="directory", path="runs", uploaded_at=None)]
    )
    assert inventory.preserved == ()
    assert inventory.deleted == ()

    with pytest.raises(BucketInventoryError, match="entry type"):
        build_inventory([SimpleNamespace(type="symlink", path="runs/run")])


def test_classification_rejects_runtime_prefix_overlap() -> None:
    with pytest.raises(PrefixConfigurationError, match="overlapping"):
        classify_key(
            "control/runs/run.json",
            preserve_prefixes=("control/",),
            delete_prefixes=("control/runs/",),
        )


def test_listing_retries_transient_failure_and_wraps_invalid_requests() -> None:
    api = FakeHfApi({"runs/run/request.json": 1})
    api.transient_list_failures = 1

    inventory = list_inventory(
        api,
        "example-org/artifact-bucket",
        sleep=_no_sleep,
    )
    assert len(inventory.deleted) == 1
    assert api.list_calls == 2

    api.invalid_listing = True
    with pytest.raises(BucketInventoryError, match="identifier"):
        list_inventory(
            api,
            "example-org/artifact-bucket",
            sleep=_no_sleep,
        )


def test_unknown_path_writes_manifest_then_aborts_without_mutation(
    tmp_path: Path,
) -> None:
    api = FakeHfApi(
        {
            "control/schema=v1/profiles/objects/model/profile.json": 3,
            "unknown/private-object": 5,
        }
    )
    manifest_path = tmp_path / "dry-run.json"

    with pytest.raises(UnknownBucketPathError):
        run_dry_run(
            api=api,
            bucket_id="example-org/artifact-bucket",
            manifest_path=manifest_path,
            clock=_clock,
            sleep=_no_sleep,
        )

    manifest = manifest_path.read_text(encoding="utf-8")
    assert '"unknown_count": 1' in manifest
    assert "example-org" not in manifest
    assert api.batch_calls == []


def test_dry_run_is_non_mutating_and_secret_free(tmp_path: Path) -> None:
    files = {
        "benchmark-bundles/sha256/abc/bundle.json": 7,
        "control/schema=v1/runs/run/request.json": 11,
    }
    api = FakeHfApi(files)
    manifest_path = tmp_path / "dry-run.json"

    manifest = run_dry_run(
        api=api,
        bucket_id="example-org/artifact-bucket",
        manifest_path=manifest_path,
        clock=_clock,
        sleep=_no_sleep,
    )

    assert api.files == files
    assert api.batch_calls == []
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["preserve_count"] == 1
    assert manifest["delete_count"] == 1
    assert manifest["preserve_bytes"] == 7
    assert manifest["delete_bytes"] == 11
    assert manifest["preserve_xet_count"] == 1
    assert manifest["preserve_sha256_count"] == 0
    assert manifest["unknown_count"] == 0
    histogram = cast(dict[str, object], manifest["prefix_histogram"])
    assert "control/schema=v1/runs/" in histogram
    assert "example-org" not in manifest_path.read_text(encoding="utf-8")


def test_apply_requires_confirmation_and_matching_digest(tmp_path: Path) -> None:
    api = FakeHfApi({"runs/run/request.json": 1})
    expected = _inventory(api.files).delete_key_digest

    with pytest.raises(ResetConfirmationError, match="--yes"):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=False,
            expected_delete_digest=expected,
            verification_manifest_path=tmp_path / "verification.json",
            sleep=_no_sleep,
        )
    assert api.list_calls == 0

    with pytest.raises(StaleInventoryError, match="digest"):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest="sha256:" + "0" * 64,
            dry_run_manifest_path=_dry_manifest(tmp_path, api.files),
            verification_manifest_path=tmp_path / "verification.json",
            sleep=_no_sleep,
        )
    assert api.batch_calls == []

    with pytest.raises(ResetConfirmationError, match="digest"):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest="invalid",
            verification_manifest_path=tmp_path / "verification.json",
            sleep=_no_sleep,
        )
    with pytest.raises(ResetConfirmationError, match="batch"):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=expected,
            verification_manifest_path=tmp_path / "verification.json",
            batch_size=0,
            sleep=_no_sleep,
        )
    with pytest.raises(ResetConfirmationError, match="dry-run-manifest"):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=expected,
            verification_manifest_path=tmp_path / "verification.json",
            sleep=_no_sleep,
        )


def test_apply_deletes_in_bounded_batches_and_verifies(tmp_path: Path) -> None:
    files = {
        "control/schema=v1/operators/operator.json": 9,
        **{f"runs/run/object-{index}.json": index + 1 for index in range(5)},
    }
    api = FakeHfApi(files)
    preflight = _inventory(files)
    verification_path = tmp_path / "verification.json"

    manifest = apply_reset(
        api=api,
        bucket_id="example-org/artifact-bucket",
        confirmed=True,
        expected_delete_digest=preflight.delete_key_digest,
        dry_run_manifest_path=_dry_manifest(tmp_path, files),
        verification_manifest_path=verification_path,
        batch_size=2,
        clock=_clock,
        sleep=_no_sleep,
    )

    assert [len(batch) for batch in api.batch_calls] == [2, 2, 1]
    assert api.files == {"control/schema=v1/operators/operator.json": 9}
    assert manifest["status"] == "verified"
    assert manifest["deleted_count"] == 5
    assert manifest["remaining_delete_count"] == 0
    assert verification_path.is_file()


def test_transient_partial_delete_retries_only_remaining_keys(
    tmp_path: Path,
) -> None:
    files = {
        "runs/run/one.json": 1,
        "runs/run/two.json": 2,
    }
    api = FakeHfApi(files)
    api.transient_partial_failures = 1

    apply_reset(
        api=api,
        bucket_id="example-org/artifact-bucket",
        confirmed=True,
        expected_delete_digest=_inventory(files).delete_key_digest,
        dry_run_manifest_path=_dry_manifest(tmp_path, files),
        verification_manifest_path=tmp_path / "verification.json",
        clock=_clock,
        sleep=_no_sleep,
    )

    assert api.batch_calls == [
        ["runs/run/one.json", "runs/run/two.json"],
        ["runs/run/two.json"],
    ]
    assert api.files == {}


@pytest.mark.parametrize("failure", ["invalid", "forbidden", "transient"])
def test_delete_failures_raise_specific_error(
    tmp_path: Path,
    failure: str,
) -> None:
    files = {"runs/run/request.json": 1}
    api = FakeHfApi(files)
    if failure == "invalid":
        api.invalid_batch = True
    elif failure == "forbidden":
        api.batch_error_status = 403
    else:
        api.transient_batch_failures = 3

    with pytest.raises(BucketDeleteError):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=_inventory(files).delete_key_digest,
            dry_run_manifest_path=_dry_manifest(tmp_path, files),
            verification_manifest_path=tmp_path / "verification.json",
            sleep=_no_sleep,
        )

    assert api.files == files


def test_apply_rerun_is_idempotent(tmp_path: Path) -> None:
    files = {"control/schema=v1/runs/run/request.json": 3}
    api = FakeHfApi(files)
    first = _inventory(files)
    apply_reset(
        api=api,
        bucket_id="example-org/artifact-bucket",
        confirmed=True,
        expected_delete_digest=first.delete_key_digest,
        dry_run_manifest_path=_dry_manifest(tmp_path, files),
        verification_manifest_path=tmp_path / "verification-one.json",
        clock=_clock,
        sleep=_no_sleep,
    )
    first_batch_count = len(api.batch_calls)
    empty = _inventory({})

    second = apply_reset(
        api=api,
        bucket_id="example-org/artifact-bucket",
        confirmed=True,
        expected_delete_digest=empty.delete_key_digest,
        dry_run_manifest_path=_dry_manifest(tmp_path, {}),
        verification_manifest_path=tmp_path / "verification-two.json",
        clock=_clock,
        sleep=_no_sleep,
    )

    assert len(api.batch_calls) == first_batch_count
    assert second["deleted_count"] == 0
    assert second["status"] == "verified"


def test_post_delete_verification_detects_preserved_object_loss(
    tmp_path: Path,
) -> None:
    files = {
        "control/schema=v1/operators/operator.json": 5,
        "runs/run/request.json": 2,
    }
    api = FakeHfApi(files)
    api.remove_preserved_after_batch = True
    verification_path = tmp_path / "verification.json"

    with pytest.raises(ResetVerificationError, match="preserved"):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=_inventory(files).delete_key_digest,
            dry_run_manifest_path=_dry_manifest(tmp_path, files),
            verification_manifest_path=verification_path,
            sleep=_no_sleep,
        )

    assert not verification_path.exists()


def test_post_delete_verification_rejects_same_size_preserved_overwrite(
    tmp_path: Path,
) -> None:
    key = "control/schema=v1/operators/operator.json"
    files = {key: 5, "runs/run/request.json": 2}
    api = FakeHfApi(files)
    api.overwrite_preserved_after_batch = True

    with pytest.raises(ResetVerificationError, match="preserved"):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=_inventory(files).delete_key_digest,
            dry_run_manifest_path=_dry_manifest(tmp_path, files),
            verification_manifest_path=tmp_path / "verification.json",
            sleep=_no_sleep,
        )

    assert api.files == {key: 5}
    assert api.batch_calls == [["runs/run/request.json"]]


def test_post_delete_verification_rejects_remaining_and_unknown_paths(
    tmp_path: Path,
) -> None:
    files = {"runs/run/request.json": 2}
    remaining = FakeHfApi(files)
    remaining.skip_deletes = True
    with pytest.raises(ResetVerificationError, match="remaining"):
        apply_reset(
            api=remaining,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=_inventory(files).delete_key_digest,
            dry_run_manifest_path=_dry_manifest(tmp_path, files),
            verification_manifest_path=tmp_path / "remaining.json",
            sleep=_no_sleep,
        )

    unknown = FakeHfApi(files)
    unknown.list_mutations[3] = lambda: unknown.files.__setitem__("unknown/path", 1)
    with pytest.raises(ResetVerificationError, match="unknown"):
        apply_reset(
            api=unknown,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=_inventory(files).delete_key_digest,
            dry_run_manifest_path=_dry_manifest(tmp_path, files),
            verification_manifest_path=tmp_path / "unknown.json",
            sleep=_no_sleep,
        )


def test_apply_aborts_on_unknown_path(tmp_path: Path) -> None:
    files = {
        "runs/run/request.json": 2,
        "other/object.json": 4,
    }
    api = FakeHfApi(files)

    with pytest.raises(UnknownBucketPathError):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=_inventory(files).delete_key_digest,
            dry_run_manifest_path=_dry_manifest(tmp_path, files),
            verification_manifest_path=tmp_path / "verification.json",
            sleep=_no_sleep,
        )
    assert api.batch_calls == []


def test_apply_aborts_if_immediate_relist_changes_count(tmp_path: Path) -> None:
    files = {"runs/run/request.json": 2}
    api = FakeHfApi(files)
    api.list_mutations[2] = lambda: api.files.__setitem__(
        "control/schema=v1/runs/canary/request.json",
        3,
    )

    with pytest.raises(StaleInventoryError, match="immediately"):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=_inventory(files).delete_key_digest,
            dry_run_manifest_path=_dry_manifest(tmp_path, files),
            verification_manifest_path=tmp_path / "verification.json",
            sleep=_no_sleep,
        )
    assert api.batch_calls == []


def test_apply_aborts_if_inventory_changes_during_partial_delete(
    tmp_path: Path,
) -> None:
    files = {
        "runs/run/one.json": 1,
        "runs/run/two.json": 2,
    }
    api = FakeHfApi(files)
    api.transient_partial_failures = 1
    api.list_mutations[3] = lambda: api.files.__setitem__("runs/run/new.json", 3)

    with pytest.raises(StaleInventoryError, match="during deletion"):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=_inventory(files).delete_key_digest,
            dry_run_manifest_path=_dry_manifest(tmp_path, files),
            verification_manifest_path=tmp_path / "verification.json",
            sleep=_no_sleep,
        )


def test_supplied_dry_run_manifest_checks_preserved_inventory(
    tmp_path: Path,
) -> None:
    files = {
        "control/schema=v1/profiles/objects/model/profile.json": 5,
        "runs/run/request.json": 2,
    }
    original = _inventory(files)
    manifest_path = tmp_path / "dry-run.json"
    write_manifest(
        manifest_path,
        dry_run_manifest(original, created_at=_NOW),
    )
    api = FakeHfApi(
        {
            "control/schema=v1/profiles/objects/model/profile.json": 6,
            "runs/run/request.json": 2,
        }
    )

    with pytest.raises(StaleInventoryError, match="manifest"):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=original.delete_key_digest,
            dry_run_manifest_path=manifest_path,
            verification_manifest_path=tmp_path / "verification.json",
            sleep=_no_sleep,
        )
    assert api.batch_calls == []


def test_preserved_identity_digest_covers_content_and_size() -> None:
    first = _inventory({"control/schema=v1/operators/operator.json": 1})
    second = _inventory({"control/schema=v1/operators/operator.json": 2})

    assert first.preserved[0].key == "control/schema=v1/operators/operator.json"
    assert first.preserved[0].content_identity is not None
    assert first.preserved[0].content_identity.startswith("xet:")
    assert first.preserve_identity_digest != second.preserve_identity_digest


def test_apply_rejects_same_size_preserved_xet_overwrite(tmp_path: Path) -> None:
    key = "control/schema=v1/operators/operator.json"
    files = {key: 4, "runs/run/request.json": 2}
    api = FakeHfApi(files)
    manifest_path = _dry_manifest(tmp_path, files)
    api.contents[key] = b"xxxx"

    with pytest.raises(StaleInventoryError, match="manifest"):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=_inventory(files).delete_key_digest,
            dry_run_manifest_path=manifest_path,
            verification_manifest_path=tmp_path / "verification.json",
            sleep=_no_sleep,
        )
    assert api.batch_calls == []


def test_sha256_fallback_rejects_same_size_preserved_overwrite(
    tmp_path: Path,
) -> None:
    key = "control/schema=v1/operators/operator.json"
    files = {key: 4, "runs/run/request.json": 2}
    api = FakeHfApi(files, expose_xet_hash=False)
    manifest_path = tmp_path / "dry-run.json"
    manifest = run_dry_run(
        api=api,
        bucket_id="example-org/artifact-bucket",
        manifest_path=manifest_path,
        clock=_clock,
        sleep=_no_sleep,
    )
    assert manifest["preserve_xet_count"] == 0
    assert manifest["preserve_sha256_count"] == 1
    assert api.download_calls == [[key]]
    api.contents[key] = b"xxxx"

    with pytest.raises(StaleInventoryError, match="manifest"):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=_inventory(files).delete_key_digest,
            dry_run_manifest_path=manifest_path,
            verification_manifest_path=tmp_path / "verification.json",
            sleep=_no_sleep,
        )
    assert api.batch_calls == []


def test_manifest_io_and_timestamp_validation(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("[", encoding="utf-8")
    with pytest.raises(ManifestError, match="read"):
        read_manifest(malformed)

    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("file", encoding="utf-8")
    with pytest.raises(ManifestError, match="write"):
        write_manifest(parent_file / "manifest.json", {})

    with pytest.raises(ManifestError, match="UTC"):
        dry_run_manifest(_inventory({}), created_at=datetime(2026, 8, 24))


def test_supplied_manifest_rejects_schema_field_changes(tmp_path: Path) -> None:
    files = {"runs/run/request.json": 2}
    manifest = dry_run_manifest(_inventory(files), created_at=_NOW)
    del manifest["unknown_key_digest"]
    manifest_path = tmp_path / "dry-run.json"
    write_manifest(manifest_path, manifest)
    api = FakeHfApi(files)

    with pytest.raises(ManifestError, match="fields"):
        apply_reset(
            api=api,
            bucket_id="example-org/artifact-bucket",
            confirmed=True,
            expected_delete_digest=_inventory(files).delete_key_digest,
            dry_run_manifest_path=manifest_path,
            verification_manifest_path=tmp_path / "verification.json",
            sleep=_no_sleep,
        )


def test_cli_defaults_to_dry_run_then_applies_reviewed_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = FakeHfApi(
        {
            "control/schema=v1/operators/operator.json": 3,
            "runs/run/request.json": 2,
        }
    )
    monkeypatch.setattr(reset_module, "HfApi", lambda: api)
    dry_path = tmp_path / "dry-run.json"
    verification_path = tmp_path / "verification.json"

    assert (
        main(
            [
                "--bucket",
                "example-org/artifact-bucket",
                "--manifest",
                str(dry_path),
            ]
        )
        == 0
    )
    digest = cast(str, read_manifest(dry_path)["delete_key_digest"])
    assert (
        main(
            [
                "--bucket",
                "example-org/artifact-bucket",
                "--apply",
                "--yes",
                "--expected-delete-digest",
                digest,
                "--dry-run-manifest",
                str(dry_path),
                "--verification-manifest",
                str(verification_path),
            ]
        )
        == 0
    )

    assert verification_path.is_file()
    assert api.files == {"control/schema=v1/operators/operator.json": 3}


def test_cli_rejects_apply_options_without_apply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = FakeHfApi({})
    monkeypatch.setattr(reset_module, "HfApi", lambda: api)

    assert (
        main(
            [
                "--bucket",
                "example-org/artifact-bucket",
                "--yes",
                "--manifest",
                str(tmp_path / "dry-run.json"),
            ]
        )
        == 1
    )
    assert api.list_calls == 0
