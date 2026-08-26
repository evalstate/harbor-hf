from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol, cast

import httpx
from huggingface_hub import BucketFile, BucketFolder, HfApi
from huggingface_hub.errors import HfHubHTTPError

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = "harbor-hf/run-data-reset/v2"
TARGETED_SCHEMA_VERSION = "harbor-hf/targeted-run-data-reset/v1"
DEFAULT_DRY_RUN_MANIFEST = Path("run-data-reset-dry-run.json")
DEFAULT_VERIFICATION_MANIFEST = Path("run-data-reset-verification.json")
DELETE_BATCH_SIZE = 100
MAX_REMOTE_ATTEMPTS = 3
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_XET_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_IDENTITY_PATTERN = re.compile(r"^(?:sha256|xet):[0-9a-f]{64}$")
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
TARGET_RUN_ROOTS = (
    "control/schema=v1/runs/",
    "evidence/schema=v1/runs/",
)

# These are the only durable Bucket trees that survive a Run-data reset.
PRESERVE_PREFIXES = (
    "benchmark-bundles/sha256/",
    "control/schema=v1/auth/",
    "control/schema=v1/migrations/",
    "control/schema=v1/operators/",
    "control/schema=v1/profiles/",
    "serving-profiles/",
)

# Each entry names one complete Run-derived path generation. Keep this list
# broad enough to cover retired writers, but never use a Bucket-root prefix.
DELETE_PREFIXES = (
    "action-leases/",
    "bucket-evidence-leases/",
    "campaign-reservations/",
    "campaigns/",
    "claims/",
    "compositions/",
    "control/schema=v1/campaigns/",
    "control/schema=v1/runs/",
    "coordination/",
    "cutovers/",
    "data/",
    "endpoint-leases/",
    "evidence/schema=v1/campaigns/",
    "evidence/schema=v1/runs/",
    "execution-reservations/",
    "executions/",
    "imports/",
    "job-inputs/",
    "projections/",
    "publications/",
    "reassessments/",
    "results/schema=v1/",
    "run-reservations/",
    "runs/",
    "sandbox-results/schema=v1/",
    "wave-worker-leases/",
)

type Classification = Literal["preserve", "delete", "unknown"]
type Sleeper = Callable[[float], None]
type Clock = Callable[[], datetime]


class RunDataResetError(RuntimeError):
    """Base exception for a rejected or failed Run-data reset."""


class PrefixConfigurationError(RunDataResetError):
    """Raised when preserve and delete prefixes are not safely disjoint."""


class BucketInventoryError(RunDataResetError):
    """Raised when the complete Bucket inventory cannot be trusted."""


class UnknownBucketPathError(RunDataResetError):
    """Raised when an object is outside every reviewed prefix."""


class ResetConfirmationError(RunDataResetError):
    """Raised when irreversible reset confirmation is incomplete."""


class StaleInventoryError(RunDataResetError):
    """Raised when Bucket contents changed after the reviewed dry run."""


class BucketDeleteError(RunDataResetError):
    """Raised when bounded deletion cannot finish safely."""


class ResetVerificationError(RunDataResetError):
    """Raised when post-delete Bucket verification fails."""


class ManifestError(RunDataResetError):
    """Raised when a local reset manifest is malformed or unavailable."""


class BucketResetApi(Protocol):
    """Installed huggingface_hub methods used by the reset tool."""

    def list_bucket_tree(
        self,
        bucket_id: str,
        prefix: str | None = None,
        *,
        recursive: bool | None = None,
        token: str | bool | None = None,
    ) -> Iterable[BucketFile | BucketFolder]: ...

    def batch_bucket_files(
        self,
        bucket_id: str,
        *,
        add: list[tuple[str | Path | bytes, str]] | None = None,
        copy: list[tuple[str, str, str, str]] | None = None,
        delete: list[str] | None = None,
        token: str | bool | None = None,
    ) -> None: ...

    def download_bucket_files(
        self,
        bucket_id: str,
        files: list[tuple[str | BucketFile, str | Path]],
        *,
        raise_on_missing_files: bool = False,
        token: str | bool | None = None,
    ) -> None: ...


@dataclass(frozen=True, order=True)
class BucketObject:
    """One Bucket object and its content identity when it must be preserved."""

    key: str
    size: int
    content_identity: str | None = None


@dataclass(frozen=True)
class PrefixTotal:
    """Counts and bytes classified under one reviewed prefix."""

    prefix: str
    classification: Literal["preserve", "delete"]
    count: int
    bytes: int


@dataclass(frozen=True)
class ResetInventory:
    """Complete classification of one recursive Bucket listing."""

    preserved: tuple[BucketObject, ...]
    deleted: tuple[BucketObject, ...]
    unknown: tuple[BucketObject, ...]
    prefix_totals: tuple[PrefixTotal, ...]

    @property
    def preserve_bytes(self) -> int:
        """Return the total bytes that must survive."""
        return sum(item.size for item in self.preserved)

    @property
    def delete_bytes(self) -> int:
        """Return the total bytes selected for deletion."""
        return sum(item.size for item in self.deleted)

    @property
    def delete_key_digest(self) -> str:
        """Digest the complete sorted delete-key list without object contents."""
        return _key_digest(self.deleted)

    @property
    def preserve_identity_digest(self) -> str:
        """Digest every preserved key, size, and content identity."""
        return _preserve_identity_digest(self.preserved)

    @property
    def unknown_key_digest(self) -> str:
        """Digest unknown keys without exposing them in a manifest."""
        return _key_digest(self.unknown)


def validate_prefixes(
    preserve_prefixes: Sequence[str],
    delete_prefixes: Sequence[str],
) -> None:
    """Reject malformed, duplicate, nested, or cross-class prefixes.

    Args:
        preserve_prefixes: Exact Bucket prefixes that must survive.
        delete_prefixes: Exact Bucket prefixes eligible for deletion.

    Raises:
        PrefixConfigurationError: If any prefix is unsafe or overlaps another.
    """
    labeled = [
        *[("preserve", prefix) for prefix in preserve_prefixes],
        *[("delete", prefix) for prefix in delete_prefixes],
    ]
    for classification, prefix in labeled:
        if (
            not prefix
            or prefix.startswith("/")
            or not prefix.endswith("/")
            or "//" in prefix
            or any(part in {".", ".."} for part in prefix.split("/"))
        ):
            raise PrefixConfigurationError(
                f"{classification} prefix is not a canonical relative directory"
            )
    for index, (left_classification, left) in enumerate(labeled):
        for right_classification, right in labeled[index + 1 :]:
            if left == right or left.startswith(right) or right.startswith(left):
                raise PrefixConfigurationError(
                    "reviewed prefixes overlap: "
                    f"{left_classification} and {right_classification}"
                )


def classify_key(
    key: str,
    *,
    preserve_prefixes: Sequence[str] = PRESERVE_PREFIXES,
    delete_prefixes: Sequence[str] = DELETE_PREFIXES,
) -> tuple[Classification, str | None]:
    """Classify one Bucket key against the exact reviewed prefix sets."""
    matches = [
        *[
            ("preserve", prefix)
            for prefix in preserve_prefixes
            if key.startswith(prefix)
        ],
        *[("delete", prefix) for prefix in delete_prefixes if key.startswith(prefix)],
    ]
    if len(matches) > 1:
        raise PrefixConfigurationError("a Bucket key matches overlapping prefixes")
    if not matches:
        return "unknown", None
    classification, prefix = matches[0]
    return cast(Classification, classification), prefix


def build_inventory(
    entries: Iterable[object],
    *,
    preserve_prefixes: Sequence[str] = PRESERVE_PREFIXES,
    delete_prefixes: Sequence[str] = DELETE_PREFIXES,
) -> ResetInventory:
    """Build a deterministic, complete classification from Hub entries.

    Directory entries are structural and are ignored. Every file must expose a
    canonical path and non-negative byte size.
    """
    validate_prefixes(preserve_prefixes, delete_prefixes)
    preserved: list[BucketObject] = []
    deleted: list[BucketObject] = []
    unknown: list[BucketObject] = []
    totals: dict[str, list[int]] = {
        prefix: [0, 0] for prefix in (*preserve_prefixes, *delete_prefixes)
    }
    observed_keys: set[str] = set()
    for entry in entries:
        item = _bucket_object(entry)
        if item is None:
            continue
        if item.key in observed_keys:
            raise BucketInventoryError("Bucket listing returned a duplicate file")
        observed_keys.add(item.key)
        classification, prefix = classify_key(
            item.key,
            preserve_prefixes=preserve_prefixes,
            delete_prefixes=delete_prefixes,
        )
        if classification == "preserve":
            preserved.append(item)
        elif classification == "delete":
            deleted.append(replace(item, content_identity=None))
        else:
            unknown.append(replace(item, content_identity=None))
        if prefix is not None:
            totals[prefix][0] += 1
            totals[prefix][1] += item.size
    preserve_set = set(preserve_prefixes)
    prefix_totals = tuple(
        PrefixTotal(
            prefix=prefix,
            classification="preserve" if prefix in preserve_set else "delete",
            count=totals[prefix][0],
            bytes=totals[prefix][1],
        )
        for prefix in sorted(totals)
    )
    return ResetInventory(
        preserved=tuple(sorted(preserved)),
        deleted=tuple(sorted(deleted)),
        unknown=tuple(sorted(unknown)),
        prefix_totals=prefix_totals,
    )


def _bucket_object(entry: object) -> BucketObject | None:
    entry_type = getattr(entry, "type", None)
    if entry_type == "directory":
        return None
    if entry_type != "file":
        raise BucketInventoryError("Bucket listing returned an unknown entry type")
    key = getattr(entry, "path", None)
    size = getattr(entry, "size", None)
    if (
        not isinstance(key, str)
        or not key
        or key.startswith("/")
        or "\n" in key
        or "\r" in key
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
    ):
        raise BucketInventoryError("Bucket listing returned invalid file metadata")
    xet_hash = getattr(entry, "xet_hash", None)
    if xet_hash is not None and (
        not isinstance(xet_hash, str) or _XET_HASH_PATTERN.fullmatch(xet_hash) is None
    ):
        raise BucketInventoryError("Bucket listing returned an invalid Xet hash")
    content_identity = f"xet:{xet_hash.lower()}" if xet_hash is not None else None
    return BucketObject(key, size, content_identity)


def dry_run_manifest(
    inventory: ResetInventory,
    *,
    created_at: datetime,
) -> dict[str, object]:
    """Create the secret-free manifest reviewed before an apply."""
    identity_counts = _preserve_identity_counts(inventory.preserved)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run",
        "created_at": _utc_timestamp(created_at),
        "preserve_count": len(inventory.preserved),
        "preserve_bytes": inventory.preserve_bytes,
        "delete_count": len(inventory.deleted),
        "delete_bytes": inventory.delete_bytes,
        "prefix_histogram": _prefix_histogram(inventory.prefix_totals),
        "delete_key_digest": inventory.delete_key_digest,
        "preserve_identity_digest": inventory.preserve_identity_digest,
        "preserve_xet_count": identity_counts["xet"],
        "preserve_sha256_count": identity_counts["sha256"],
        "unknown_count": len(inventory.unknown),
        "unknown_key_digest": inventory.unknown_key_digest,
    }


def run_dry_run(
    *,
    api: BucketResetApi,
    bucket_id: str,
    manifest_path: Path,
    clock: Clock = lambda: datetime.now(UTC),
    sleep: Sleeper = time.sleep,
) -> dict[str, object]:
    """List and classify the Bucket without mutating it.

    The manifest is written even when unknown paths are found, then the command
    fails so an unknown object can never be silently accepted.
    """
    inventory = list_inventory(api, bucket_id, sleep=sleep)
    manifest = dry_run_manifest(inventory, created_at=clock())
    write_manifest(manifest_path, manifest)
    LOGGER.info(
        "dry run classified preserve_count=%d delete_count=%d unknown_count=%d",
        len(inventory.preserved),
        len(inventory.deleted),
        len(inventory.unknown),
    )
    if inventory.unknown:
        raise UnknownBucketPathError(
            "dry run found Bucket paths outside the reviewed prefix sets"
        )
    return manifest


def target_run_prefixes(run_ids: Sequence[str]) -> tuple[str, ...]:
    """Return exact current-schema Bucket prefixes for selected Runs."""
    normalized = tuple(sorted(run_ids))
    if not normalized:
        raise PrefixConfigurationError("targeted reset requires at least one Run")
    if len(set(normalized)) != len(normalized):
        raise PrefixConfigurationError("targeted reset Run IDs must be unique")
    if any(
        len(run_id) < 2 or len(run_id) > 160 or _ID_PATTERN.fullmatch(run_id) is None
        for run_id in normalized
    ):
        raise PrefixConfigurationError("targeted reset Run ID is invalid")
    prefixes = tuple(
        f"{root}{run_id}/" for root in TARGET_RUN_ROOTS for run_id in normalized
    )
    validate_prefixes((), prefixes)
    return prefixes


def list_targeted_inventory(
    api: BucketResetApi,
    bucket_id: str,
    run_ids: Sequence[str],
    *,
    sleep: Sleeper = time.sleep,
) -> ResetInventory:
    """List only the exact control and evidence trees for selected Runs."""
    prefixes = target_run_prefixes(run_ids)
    entries: list[object] = []
    for prefix in prefixes:
        for attempt in range(1, MAX_REMOTE_ATTEMPTS + 1):
            try:
                entries.extend(
                    api.list_bucket_tree(
                        bucket_id,
                        prefix=prefix,
                        recursive=True,
                    )
                )
                break
            except ValueError as error:
                raise BucketInventoryError(
                    "targeted Bucket identifier or listing request is invalid"
                ) from error
            except (HfHubHTTPError, httpx.TransportError) as error:
                if not _is_transient(error) or attempt == MAX_REMOTE_ATTEMPTS:
                    raise BucketInventoryError(
                        "complete targeted Bucket listing failed"
                    ) from error
                LOGGER.warning(
                    "transient targeted Bucket listing failure; retrying attempt=%d",
                    attempt + 1,
                )
                sleep(_retry_delay(attempt))
    inventory = build_inventory(
        entries,
        preserve_prefixes=(),
        delete_prefixes=prefixes,
    )
    _require_known_inventory(inventory)
    return inventory


def targeted_dry_run_manifest(
    inventory: ResetInventory,
    run_ids: Sequence[str],
    *,
    created_at: datetime,
) -> dict[str, object]:
    """Create a secret-free manifest for selected Run trees."""
    return {
        "schema_version": TARGETED_SCHEMA_VERSION,
        "mode": "dry-run",
        "created_at": _utc_timestamp(created_at),
        "target_run_count": len(tuple(run_ids)),
        "target_run_digest": _target_run_digest(run_ids),
        "target_prefix_digest": _target_prefix_digest(run_ids),
        "delete_count": len(inventory.deleted),
        "delete_bytes": inventory.delete_bytes,
        "delete_key_digest": inventory.delete_key_digest,
        "unknown_count": len(inventory.unknown),
        "unknown_key_digest": inventory.unknown_key_digest,
    }


def run_targeted_dry_run(
    *,
    api: BucketResetApi,
    bucket_id: str,
    run_ids: Sequence[str],
    manifest_path: Path,
    clock: Clock = lambda: datetime.now(UTC),
    sleep: Sleeper = time.sleep,
) -> dict[str, object]:
    """Inventory selected Run trees without mutating the Bucket."""
    inventory = list_targeted_inventory(api, bucket_id, run_ids, sleep=sleep)
    manifest = targeted_dry_run_manifest(
        inventory,
        run_ids,
        created_at=clock(),
    )
    write_manifest(manifest_path, manifest)
    LOGGER.info(
        "targeted dry run classified target_run_count=%d delete_count=%d",
        len(tuple(run_ids)),
        len(inventory.deleted),
    )
    return manifest


def apply_targeted_reset(
    *,
    api: BucketResetApi,
    bucket_id: str,
    run_ids: Sequence[str],
    confirmed: bool,
    expected_delete_digest: str | None,
    verification_manifest_path: Path,
    dry_run_manifest_path: Path | None = None,
    batch_size: int = DELETE_BATCH_SIZE,
    clock: Clock = lambda: datetime.now(UTC),
    sleep: Sleeper = time.sleep,
) -> dict[str, object]:
    """Delete only the reviewed current-schema trees for selected Runs."""
    expected_digest, manifest_path = _validate_apply_confirmation(
        confirmed,
        expected_delete_digest,
        dry_run_manifest_path,
        batch_size,
    )
    target_run_prefixes(run_ids)
    preflight = list_targeted_inventory(api, bucket_id, run_ids, sleep=sleep)
    if preflight.delete_key_digest != expected_digest:
        raise StaleInventoryError(
            "targeted delete digest differs from the reviewed dry run"
        )
    expected_manifest = read_manifest(manifest_path)
    _compare_targeted_dry_run_manifest(expected_manifest, preflight, run_ids)

    immediate = list_targeted_inventory(api, bucket_id, run_ids, sleep=sleep)
    if immediate.deleted != preflight.deleted:
        raise StaleInventoryError(
            "targeted Bucket inventory changed immediately before mutation"
        )
    for batch in _batches(immediate.deleted, batch_size):
        _delete_targeted_batch(
            api=api,
            bucket_id=bucket_id,
            run_ids=run_ids,
            batch=batch,
            preflight=preflight,
            sleep=sleep,
        )

    verified = list_targeted_inventory(api, bucket_id, run_ids, sleep=sleep)
    if verified.deleted:
        raise ResetVerificationError(
            "post-delete verification found selected Run objects"
        )
    manifest = targeted_verification_manifest(
        preflight,
        verified,
        run_ids,
        created_at=clock(),
    )
    write_manifest(verification_manifest_path, manifest)
    LOGGER.info(
        "targeted reset verified deleted_count=%d target_run_count=%d",
        len(preflight.deleted),
        len(tuple(run_ids)),
    )
    return manifest


def targeted_verification_manifest(
    preflight: ResetInventory,
    verified: ResetInventory,
    run_ids: Sequence[str],
    *,
    created_at: datetime,
) -> dict[str, object]:
    """Create local verification evidence for selected Run deletion."""
    return {
        "schema_version": TARGETED_SCHEMA_VERSION,
        "mode": "verification",
        "created_at": _utc_timestamp(created_at),
        "status": "verified",
        "target_run_count": len(tuple(run_ids)),
        "target_run_digest": _target_run_digest(run_ids),
        "target_prefix_digest": _target_prefix_digest(run_ids),
        "deleted_count": len(preflight.deleted),
        "deleted_bytes": preflight.delete_bytes,
        "preflight_delete_key_digest": preflight.delete_key_digest,
        "remaining_delete_count": len(verified.deleted),
        "unknown_count": len(verified.unknown),
    }


def apply_reset(
    *,
    api: BucketResetApi,
    bucket_id: str,
    confirmed: bool,
    expected_delete_digest: str | None,
    verification_manifest_path: Path,
    dry_run_manifest_path: Path | None = None,
    batch_size: int = DELETE_BATCH_SIZE,
    clock: Clock = lambda: datetime.now(UTC),
    sleep: Sleeper = time.sleep,
) -> dict[str, object]:
    """Delete exactly the reviewed Run-derived inventory and verify the Bucket.

    Args:
        api: Installed Hugging Face Hub API or a compatible test fake.
        bucket_id: Canonical Bucket identifier, never stored in manifests.
        confirmed: Explicit irreversible-operation confirmation.
        expected_delete_digest: Digest copied from a fresh dry run.
        verification_manifest_path: Local destination for post-delete evidence.
        dry_run_manifest_path: Reviewed manifest that binds preserved content
            identities and the delete inventory.
        batch_size: Maximum file count passed to one delete request.
        clock: UTC clock used only for local manifest timestamps.
        sleep: Delay function used between bounded transient retries.

    Raises:
        RunDataResetError: If confirmation, inventory, deletion, or verification
            does not satisfy the fail-closed contract.
    """
    expected_digest, manifest_path = _validate_apply_confirmation(
        confirmed,
        expected_delete_digest,
        dry_run_manifest_path,
        batch_size,
    )
    preflight = list_inventory(api, bucket_id, sleep=sleep)
    _require_known_inventory(preflight)
    if preflight.delete_key_digest != expected_digest:
        raise StaleInventoryError("delete digest differs from the reviewed dry run")
    expected_manifest = read_manifest(manifest_path)
    _compare_dry_run_manifest(expected_manifest, preflight)

    # Re-list immediately before the first mutation. Both object sets and sizes
    # must still match the preflight inventory exactly.
    immediate = list_inventory(api, bucket_id, sleep=sleep)
    _require_exact_inventory(preflight, immediate)
    for batch in _batches(immediate.deleted, batch_size):
        _delete_batch(
            api=api,
            bucket_id=bucket_id,
            batch=batch,
            preflight=preflight,
            sleep=sleep,
        )

    verified = list_inventory(api, bucket_id, sleep=sleep)
    _verify_post_delete(preflight, verified)
    manifest = verification_manifest(preflight, verified, created_at=clock())
    write_manifest(verification_manifest_path, manifest)
    LOGGER.info(
        "reset verified deleted_count=%d preserved_count=%d",
        len(preflight.deleted),
        len(preflight.preserved),
    )
    return manifest


def verification_manifest(
    preflight: ResetInventory,
    verified: ResetInventory,
    *,
    created_at: datetime,
) -> dict[str, object]:
    """Create local evidence for the completed post-delete verification."""
    identity_counts = _preserve_identity_counts(verified.preserved)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "verification",
        "created_at": _utc_timestamp(created_at),
        "status": "verified",
        "deleted_count": len(preflight.deleted),
        "deleted_bytes": preflight.delete_bytes,
        "preflight_delete_key_digest": preflight.delete_key_digest,
        "preserve_count": len(verified.preserved),
        "preserve_bytes": verified.preserve_bytes,
        "preserve_identity_digest": verified.preserve_identity_digest,
        "preserve_xet_count": identity_counts["xet"],
        "preserve_sha256_count": identity_counts["sha256"],
        "remaining_delete_count": len(verified.deleted),
        "unknown_count": len(verified.unknown),
        "prefix_histogram": _prefix_histogram(verified.prefix_totals),
    }


def list_inventory(
    api: BucketResetApi,
    bucket_id: str,
    *,
    sleep: Sleeper = time.sleep,
) -> ResetInventory:
    """Recursively list the whole Bucket with bounded transient retries."""
    for attempt in range(1, MAX_REMOTE_ATTEMPTS + 1):
        try:
            entries = list(api.list_bucket_tree(bucket_id, recursive=True))
            inventory = build_inventory(entries)
            return _resolve_preserved_content_identities(api, bucket_id, inventory)
        except ValueError as error:
            raise BucketInventoryError(
                "Bucket identifier or listing request is invalid"
            ) from error
        except OSError as error:
            raise BucketInventoryError(
                "preserved Bucket object hashing failed"
            ) from error
        except (HfHubHTTPError, httpx.TransportError) as error:
            if not _is_transient(error) or attempt == MAX_REMOTE_ATTEMPTS:
                raise BucketInventoryError("complete Bucket listing failed") from error
            LOGGER.warning(
                "transient Bucket listing failure; retrying attempt=%d",
                attempt + 1,
            )
            sleep(_retry_delay(attempt))
    raise AssertionError("bounded Bucket listing loop did not terminate")


def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    """Atomically write a local manifest without logging its filesystem path."""
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as error:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise ManifestError("could not write the local reset manifest") from error


def read_manifest(path: Path) -> dict[str, object]:
    """Read and minimally validate a local dry-run manifest."""
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError("could not read the local dry-run manifest") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ManifestError("dry-run manifest must be a JSON object")
    return cast(dict[str, object], value)


def _validate_apply_confirmation(
    confirmed: bool,
    expected_delete_digest: str | None,
    dry_run_manifest_path: Path | None,
    batch_size: int,
) -> tuple[str, Path]:
    if not confirmed:
        raise ResetConfirmationError("apply requires --yes")
    if (
        expected_delete_digest is None
        or _DIGEST_PATTERN.fullmatch(expected_delete_digest) is None
    ):
        raise ResetConfirmationError("apply requires a valid --expected-delete-digest")
    if batch_size < 1:
        raise ResetConfirmationError("delete batch size must be positive")
    if dry_run_manifest_path is None:
        raise ResetConfirmationError("apply requires --dry-run-manifest")
    return expected_delete_digest, dry_run_manifest_path


def _compare_dry_run_manifest(
    manifest: dict[str, object],
    inventory: ResetInventory,
) -> None:
    required = {
        "schema_version",
        "mode",
        "created_at",
        "preserve_count",
        "preserve_bytes",
        "delete_count",
        "delete_bytes",
        "prefix_histogram",
        "delete_key_digest",
        "preserve_identity_digest",
        "preserve_xet_count",
        "preserve_sha256_count",
        "unknown_count",
        "unknown_key_digest",
    }
    if set(manifest) != required:
        raise ManifestError("dry-run manifest fields do not match the reset schema")
    if (
        manifest["schema_version"] != SCHEMA_VERSION
        or manifest["mode"] != "dry-run"
        or manifest["unknown_count"] != 0
    ):
        raise StaleInventoryError("dry-run manifest is not an applicable clean plan")
    expected = dry_run_manifest(
        inventory,
        created_at=_parse_utc_timestamp(manifest["created_at"]),
    )
    if manifest != expected:
        raise StaleInventoryError("Bucket inventory differs from the dry-run manifest")


def _compare_targeted_dry_run_manifest(
    manifest: dict[str, object],
    inventory: ResetInventory,
    run_ids: Sequence[str],
) -> None:
    required = {
        "schema_version",
        "mode",
        "created_at",
        "target_run_count",
        "target_run_digest",
        "target_prefix_digest",
        "delete_count",
        "delete_bytes",
        "delete_key_digest",
        "unknown_count",
        "unknown_key_digest",
    }
    if set(manifest) != required:
        raise ManifestError(
            "targeted dry-run manifest fields do not match the reset schema"
        )
    if (
        manifest["schema_version"] != TARGETED_SCHEMA_VERSION
        or manifest["mode"] != "dry-run"
        or manifest["unknown_count"] != 0
    ):
        raise StaleInventoryError(
            "targeted dry-run manifest is not an applicable clean plan"
        )
    expected = targeted_dry_run_manifest(
        inventory,
        run_ids,
        created_at=_parse_utc_timestamp(manifest["created_at"]),
    )
    if manifest != expected:
        raise StaleInventoryError(
            "targeted Bucket inventory differs from the dry-run manifest"
        )


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ManifestError("dry-run manifest timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ManifestError("dry-run manifest timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ManifestError("dry-run manifest timestamp must use UTC")
    return parsed


def _require_known_inventory(inventory: ResetInventory) -> None:
    if inventory.unknown:
        raise UnknownBucketPathError(
            "Bucket contains paths outside the reviewed prefix sets"
        )


def _require_exact_inventory(
    expected: ResetInventory,
    observed: ResetInventory,
) -> None:
    _require_known_inventory(observed)
    if expected.preserved != observed.preserved or expected.deleted != observed.deleted:
        raise StaleInventoryError("Bucket changed immediately before mutation")


def _delete_batch(
    *,
    api: BucketResetApi,
    bucket_id: str,
    batch: tuple[BucketObject, ...],
    preflight: ResetInventory,
    sleep: Sleeper,
) -> None:
    remaining = batch
    for attempt in range(1, MAX_REMOTE_ATTEMPTS + 1):
        if not remaining:
            return
        try:
            api.batch_bucket_files(
                bucket_id,
                delete=[item.key for item in remaining],
            )
            return
        except ValueError as error:
            raise BucketDeleteError("a delete batch request was invalid") from error
        except (HfHubHTTPError, httpx.TransportError) as error:
            observed = list_inventory(api, bucket_id, sleep=sleep)
            _require_safe_partial_inventory(preflight, observed)
            observed_keys = {item.key for item in observed.deleted}
            remaining = tuple(item for item in batch if item.key in observed_keys)
            if not remaining:
                return
            if not _is_transient(error) or attempt == MAX_REMOTE_ATTEMPTS:
                raise BucketDeleteError(
                    "a bounded delete batch did not complete"
                ) from error
            LOGGER.warning(
                "transient partial delete failure; retrying remaining_count=%d "
                "attempt=%d",
                len(remaining),
                attempt + 1,
            )
            sleep(_retry_delay(attempt))


def _delete_targeted_batch(
    *,
    api: BucketResetApi,
    bucket_id: str,
    run_ids: Sequence[str],
    batch: tuple[BucketObject, ...],
    preflight: ResetInventory,
    sleep: Sleeper,
) -> None:
    remaining = batch
    for attempt in range(1, MAX_REMOTE_ATTEMPTS + 1):
        if not remaining:
            return
        try:
            api.batch_bucket_files(
                bucket_id,
                delete=[item.key for item in remaining],
            )
            return
        except ValueError as error:
            raise BucketDeleteError(
                "a targeted delete batch request was invalid"
            ) from error
        except (HfHubHTTPError, httpx.TransportError) as error:
            observed = list_targeted_inventory(
                api,
                bucket_id,
                run_ids,
                sleep=sleep,
            )
            expected_objects = set(preflight.deleted)
            if any(item not in expected_objects for item in observed.deleted):
                raise StaleInventoryError(
                    "targeted inventory changed during deletion"
                ) from error
            observed_keys = {item.key for item in observed.deleted}
            remaining = tuple(item for item in batch if item.key in observed_keys)
            if not remaining:
                return
            if not _is_transient(error) or attempt == MAX_REMOTE_ATTEMPTS:
                raise BucketDeleteError(
                    "a bounded targeted delete batch did not complete"
                ) from error
            LOGGER.warning(
                "transient targeted partial delete failure; "
                "retrying remaining_count=%d attempt=%d",
                len(remaining),
                attempt + 1,
            )
            sleep(_retry_delay(attempt))


def _require_safe_partial_inventory(
    preflight: ResetInventory,
    observed: ResetInventory,
) -> None:
    _require_known_inventory(observed)
    if observed.preserved != preflight.preserved:
        raise StaleInventoryError("preserved Bucket inventory changed during deletion")
    expected_delete_objects = set(preflight.deleted)
    if any(item not in expected_delete_objects for item in observed.deleted):
        raise StaleInventoryError("delete-eligible inventory changed during deletion")


def _verify_post_delete(
    preflight: ResetInventory,
    verified: ResetInventory,
) -> None:
    if verified.unknown:
        raise ResetVerificationError(
            "post-delete verification found unknown Bucket paths"
        )
    if verified.deleted:
        raise ResetVerificationError(
            "post-delete verification found remaining delete-prefix objects"
        )
    if verified.preserved != preflight.preserved:
        raise ResetVerificationError(
            "post-delete verification found changed preserved objects"
        )


def _batches(
    items: tuple[BucketObject, ...],
    batch_size: int,
) -> Iterator[tuple[BucketObject, ...]]:
    for offset in range(0, len(items), batch_size):
        yield items[offset : offset + batch_size]


def _retry_delay(attempt: int) -> float:
    return float(2 ** (attempt - 1))


def _key_digest(items: Sequence[BucketObject]) -> str:
    keys = [item.key for item in items]
    payload = json.dumps(keys, ensure_ascii=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _target_run_digest(run_ids: Sequence[str]) -> str:
    normalized = tuple(sorted(run_ids))
    target_run_prefixes(normalized)
    payload = json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _target_prefix_digest(run_ids: Sequence[str]) -> str:
    prefixes = target_run_prefixes(run_ids)
    payload = json.dumps(
        prefixes,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _preserve_identity_digest(items: Sequence[BucketObject]) -> str:
    triples: list[list[str | int]] = []
    for item in items:
        identity = item.content_identity
        if identity is None or _CONTENT_IDENTITY_PATTERN.fullmatch(identity) is None:
            raise BucketInventoryError(
                "preserved Bucket object has no verified content identity"
            )
        triples.append([item.key, item.size, identity])
    payload = json.dumps(triples, ensure_ascii=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _preserve_identity_counts(items: Sequence[BucketObject]) -> dict[str, int]:
    counts = {"xet": 0, "sha256": 0}
    for item in items:
        identity = item.content_identity
        if identity is None or _CONTENT_IDENTITY_PATTERN.fullmatch(identity) is None:
            raise BucketInventoryError(
                "preserved Bucket object has no verified content identity"
            )
        counts[identity.split(":", 1)[0]] += 1
    return counts


def _resolve_preserved_content_identities(
    api: BucketResetApi,
    bucket_id: str,
    inventory: ResetInventory,
) -> ResetInventory:
    missing = [item for item in inventory.preserved if item.content_identity is None]
    if not missing:
        return inventory
    with TemporaryDirectory(prefix="harbor-hf-reset-") as directory:
        root = Path(directory)
        destinations = [root / str(index) for index in range(len(missing))]
        api.download_bucket_files(
            bucket_id,
            [
                (item.key, destination)
                for item, destination in zip(missing, destinations, strict=True)
            ],
            raise_on_missing_files=True,
        )
        identities: dict[str, str] = {}
        for item, destination in zip(missing, destinations, strict=True):
            content = destination.read_bytes()
            if len(content) != item.size:
                raise BucketInventoryError(
                    "downloaded preserved Bucket object size does not match listing"
                )
            identities[item.key] = f"sha256:{hashlib.sha256(content).hexdigest()}"
    return replace(
        inventory,
        preserved=tuple(
            replace(item, content_identity=identities[item.key])
            if item.content_identity is None
            else item
            for item in inventory.preserved
        ),
    )


def _prefix_histogram(totals: Sequence[PrefixTotal]) -> dict[str, object]:
    return {
        item.prefix: {
            "classification": item.classification,
            "count": item.count,
            "bytes": item.bytes,
        }
        for item in totals
    }


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ManifestError("manifest clock must return a UTC timestamp")
    return value.isoformat().replace("+00:00", "Z")


def _is_transient(error: HfHubHTTPError | httpx.TransportError) -> bool:
    if isinstance(error, httpx.TransportError):
        return True
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    return isinstance(status_code, int) and status_code in _TRANSIENT_STATUS_CODES


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run or apply the exact-prefix, one-time Harbor-HF Run-data reset."
        )
    )
    parser.add_argument(
        "--bucket", required=True, metavar="<namespace>/<artifact-bucket>"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_DRY_RUN_MANIFEST,
        help="local dry-run manifest path",
    )
    parser.add_argument(
        "--run-id",
        action="append",
        default=[],
        help="delete only this current-schema Run; repeat for multiple Runs",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--expected-delete-digest")
    parser.add_argument("--dry-run-manifest", type=Path)
    parser.add_argument(
        "--verification-manifest",
        type=Path,
        default=DEFAULT_VERIFICATION_MANIFEST,
        help="local post-delete verification manifest path",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone operator CLI without exposing remote identifiers."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    arguments = _parser().parse_args(argv)
    bucket_id = cast(str, arguments.bucket)
    apply = cast(bool, arguments.apply)
    run_ids = cast(list[str], arguments.run_id)
    try:
        api = cast(BucketResetApi, HfApi())
        if apply:
            if run_ids:
                apply_targeted_reset(
                    api=api,
                    bucket_id=bucket_id,
                    run_ids=run_ids,
                    confirmed=cast(bool, arguments.yes),
                    expected_delete_digest=cast(
                        str | None, arguments.expected_delete_digest
                    ),
                    dry_run_manifest_path=cast(Path | None, arguments.dry_run_manifest),
                    verification_manifest_path=cast(
                        Path, arguments.verification_manifest
                    ),
                )
            else:
                apply_reset(
                    api=api,
                    bucket_id=bucket_id,
                    confirmed=cast(bool, arguments.yes),
                    expected_delete_digest=cast(
                        str | None, arguments.expected_delete_digest
                    ),
                    dry_run_manifest_path=cast(Path | None, arguments.dry_run_manifest),
                    verification_manifest_path=cast(
                        Path, arguments.verification_manifest
                    ),
                )
        else:
            if (
                cast(bool, arguments.yes)
                or cast(str | None, arguments.expected_delete_digest) is not None
                or cast(Path | None, arguments.dry_run_manifest) is not None
            ):
                raise ResetConfirmationError(
                    "apply-only confirmation options require --apply"
                )
            if run_ids:
                run_targeted_dry_run(
                    api=api,
                    bucket_id=bucket_id,
                    run_ids=run_ids,
                    manifest_path=cast(Path, arguments.manifest),
                )
            else:
                run_dry_run(
                    api=api,
                    bucket_id=bucket_id,
                    manifest_path=cast(Path, arguments.manifest),
                )
    except RunDataResetError as error:
        LOGGER.error("Run-data reset aborted: %s", error)
        return 1
    return 0


validate_prefixes(PRESERVE_PREFIXES, DELETE_PREFIXES)
