from __future__ import annotations

from typing import Protocol

from huggingface_hub import CommitOperationAdd
from huggingface_hub.errors import HfHubHTTPError
from pydantic import BaseModel, ConfigDict

from harbor_hf.control import RunEvent, RunSnapshot
from harbor_hf.io import load_experiment_bytes
from harbor_hf.models import PublicationVisibility
from harbor_hf.publication_correction import (
    PublicationCorrection,
    validate_publication_correction,
)
from harbor_hf.recovery import (
    durable_cancellation_event,
    durable_manual_intervention_resolution_event,
    durable_shard_retry_event,
    project_recovery,
    seal_partial_projection,
)
from harbor_hf.result_publisher import PublicationResult
from harbor_hf.results import (
    EvidenceReader,
    EvidenceSource,
    ResultPublication,
    TableName,
    build_result_publication,
    build_result_tables,
)
from harbor_hf.run_finalizer import (
    BucketRunFinalizer,
    ImmutableEvidenceWriter,
    ValidatingEvidenceWriter,
)

_DATASET_INITIALIZATION_PATH = ".harbor-hf-initialized"
_DATASET_INITIALIZATION_PAYLOAD = b"harbor-hf publication Dataset\n"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunEventResult(FrozenModel):
    run_id: str
    event_id: str
    kind: str
    recorded: bool
    dry_run: bool


class VerifiedExecution(FrozenModel):
    execution_id: str
    publication_id: str
    source_prefix: str
    source_checksum: str
    row_counts: dict[TableName, int]


class ArtifactVerificationReport(FrozenModel):
    run_id: str
    artifact_bucket: str
    control_commit: str
    verified: bool = True
    executions: list[VerifiedExecution]


class PublishedExecution(FrozenModel):
    execution_id: str
    publication_id: str
    result_dataset: str
    index_dataset: str
    published: bool
    result_revision: str | None = None
    index_revision: str | None = None


class RunPublicationReport(FrozenModel):
    run_id: str
    control_commit: str
    dry_run: bool
    executions: list[PublishedExecution]


class SealedExecution(FrozenModel):
    execution_id: str
    source_prefix: str
    source_checksum: str | None = None


class RunSealReport(FrozenModel):
    run_id: str
    artifact_bucket: str
    dry_run: bool
    executions: list[SealedExecution]


class RunEventStore(Protocol):
    def load_snapshot(self, run_id: str) -> RunSnapshot: ...

    def ensure_event(self, run_id: str, event: RunEvent) -> bool: ...


class ResultPublisher(Protocol):
    def publish(
        self,
        publication: ResultPublication,
        *,
        result_dataset: str,
        index_dataset: str,
    ) -> PublicationResult: ...


class RefreshingEvidenceReader(EvidenceReader, Protocol):
    def refresh(self) -> None: ...


class DatasetRepositoryApi(Protocol):
    def create_repo(self, repo_id: str, **kwargs: object) -> object: ...

    def repo_info(self, repo_id: str, **kwargs: object) -> object: ...

    def create_commit(
        self, repo_id: str, operations: list[object], **kwargs: object
    ) -> object: ...


class AutomaticRunPublisher:
    """Publish every complete run after terminal evidence is finalized."""

    def __init__(
        self,
        *,
        namespace: str,
        store: RunEventStore,
        reader: RefreshingEvidenceReader,
        publisher: ResultPublisher,
        repositories: DatasetRepositoryApi,
    ) -> None:
        self.namespace = namespace
        self.store = store
        self.reader = reader
        self.publisher = publisher
        self.repositories = repositories

    def publish(self, run_id: str) -> RunPublicationReport:
        snapshot = self.store.load_snapshot(run_id)
        spec = load_experiment_bytes(
            snapshot.request,
            source=f"run {run_id} request",
        )
        if spec.publishing.index_dataset is None:
            raise ValueError("run result publication requires index_dataset")
        assert spec.publishing.index_dataset_visibility is not None
        repositories = (
            (spec.publishing.dataset, spec.publishing.dataset_visibility),
            (
                spec.publishing.index_dataset,
                spec.publishing.index_dataset_visibility,
            ),
        )
        _prepare_dataset_repositories(repositories, self.repositories)
        self.reader.refresh()
        return publish_run_results(
            snapshot,
            namespace=self.namespace,
            reader=self.reader,
            publisher=self.publisher,
            dry_run=False,
        )

    def publish_correction(
        self,
        correction: PublicationCorrection,
    ) -> RunPublicationReport:
        """Publish frozen evidence through an explicit corrected target."""
        snapshot = self.store.load_snapshot(correction.run_id)
        artifact_bucket = validate_publication_correction(
            snapshot, correction, self.namespace
        )
        _prepare_dataset_repositories(
            (
                (correction.result_dataset, correction.result_dataset_visibility),
                (correction.index_dataset, correction.index_dataset_visibility),
            ),
            self.repositories,
        )
        self.reader.refresh()
        return publish_run_results(
            snapshot,
            namespace=self.namespace,
            reader=self.reader,
            publisher=self.publisher,
            dry_run=False,
            destinations=(correction.result_dataset, correction.index_dataset),
            artifact_bucket=artifact_bucket,
        )


def _prepare_dataset_repositories(
    repositories: tuple[
        tuple[str, PublicationVisibility], tuple[str, PublicationVisibility]
    ],
    api: DatasetRepositoryApi,
) -> None:
    for repository, visibility in repositories:
        api.create_repo(
            repository,
            repo_type="dataset",
            private=visibility == "private",
            exist_ok=True,
        )
    repository_info = [
        api.repo_info(repository, repo_type="dataset")
        for repository, _visibility in repositories
    ]
    for (repository, visibility), info in zip(
        repositories, repository_info, strict=True
    ):
        observed_private = getattr(info, "private", None)
        expected_private = visibility == "private"
        if observed_private is not expected_private:
            observed_visibility = "private" if observed_private is True else "public"
            raise ValueError(
                f"Dataset repository {repository} is {observed_visibility}; "
                f"manifest requires {visibility}"
            )
    for (repository, _visibility), info in zip(
        repositories, repository_info, strict=True
    ):
        if _commit_identity(info) is None:
            _initialize_dataset_repository(repository, api)


def _initialize_dataset_repository(repository: str, api: DatasetRepositoryApi) -> None:
    initialization_error: HfHubHTTPError | None = None
    try:
        api.create_commit(
            repository,
            [
                CommitOperationAdd(
                    path_in_repo=_DATASET_INITIALIZATION_PATH,
                    path_or_fileobj=_DATASET_INITIALIZATION_PAYLOAD,
                )
            ],
            commit_message="chore: initialize publication Dataset",
            repo_type="dataset",
            revision="main",
        )
    except HfHubHTTPError as error:
        initialization_error = error
    info = api.repo_info(repository, repo_type="dataset", revision="main")
    if _commit_identity(info) is not None:
        return
    if initialization_error is not None:
        raise initialization_error
    raise ValueError(f"Dataset repository {repository} has no commit identity")


def _commit_identity(info: object) -> str | None:
    revision = getattr(info, "sha", None)
    return revision if isinstance(revision, str) and revision else None


def cancel_run(
    store: RunEventStore,
    run_id: str,
    *,
    reason: str,
    dry_run: bool,
) -> RunEventResult:
    snapshot = store.load_snapshot(run_id)
    event, created = durable_cancellation_event(snapshot.lock, snapshot.events, reason)
    recorded = False if dry_run or not created else store.ensure_event(run_id, event)
    return RunEventResult(
        run_id=run_id,
        event_id=event.event_id,
        kind=event.kind,
        recorded=recorded,
        dry_run=dry_run,
    )


def retry_run_shard(
    store: RunEventStore,
    run_id: str,
    *,
    shard_id: str,
    reason: str,
    dry_run: bool,
) -> RunEventResult:
    snapshot = store.load_snapshot(run_id)
    event, created = durable_shard_retry_event(
        snapshot.lock, snapshot.events, shard_id, reason
    )
    recorded = False if dry_run or not created else store.ensure_event(run_id, event)
    return RunEventResult(
        run_id=run_id,
        event_id=event.event_id,
        kind=event.kind,
        recorded=recorded,
        dry_run=dry_run,
    )


def resume_run(
    store: RunEventStore,
    run_id: str,
    *,
    reason: str,
    cleanup_verified: bool,
    dry_run: bool,
) -> RunEventResult:
    snapshot = store.load_snapshot(run_id)
    event, created = durable_manual_intervention_resolution_event(
        snapshot.lock,
        snapshot.events,
        reason,
        cleanup_verified=cleanup_verified,
    )
    recorded = False if dry_run or not created else store.ensure_event(run_id, event)
    return RunEventResult(
        run_id=run_id,
        event_id=event.event_id,
        kind=event.kind,
        recorded=recorded,
        dry_run=dry_run,
    )


def seal_partial_run_executions(
    snapshot: RunSnapshot,
    *,
    namespace: str,
    reader: EvidenceReader,
    writer: ImmutableEvidenceWriter | None,
    dry_run: bool,
) -> RunSealReport:
    spec = load_experiment_bytes(
        snapshot.request,
        source=f"run {snapshot.lock.run_id} request",
    )
    if spec.remote is None or spec.remote.job.namespace != namespace:
        raise ValueError("run request does not match the control namespace")
    projection = seal_partial_projection(
        project_recovery(snapshot.lock, snapshot.events)
    )
    if dry_run:
        evidence_writer: ImmutableEvidenceWriter = ValidatingEvidenceWriter(
            reader,
            bucket=spec.artifacts.bucket,
            prefix=snapshot.lock.artifact_prefix,
        )
    elif writer is None:
        raise ValueError("evidence writer is required outside dry-run")
    else:
        evidence_writer = writer
    checksums = BucketRunFinalizer(reader, evidence_writer).seal_executions(
        snapshot.lock,
        spec,
        projection,
    )
    return RunSealReport(
        run_id=snapshot.lock.run_id,
        artifact_bucket=spec.artifacts.bucket,
        dry_run=dry_run,
        executions=[
            SealedExecution(
                execution_id=run.execution_id,
                source_prefix=f"{snapshot.lock.artifact_prefix}/executions/{run.execution_id}",
                source_checksum=checksums[run.execution_id],
            )
            for run in snapshot.lock.executions
        ],
    )


def verify_run_artifacts(
    snapshot: RunSnapshot,
    *,
    namespace: str,
    reader: EvidenceReader,
) -> ArtifactVerificationReport:
    report, _publications, _destinations = _prepare_publications(
        snapshot, namespace=namespace, reader=reader
    )
    return report


def publish_run_results(
    snapshot: RunSnapshot,
    *,
    namespace: str,
    reader: EvidenceReader,
    publisher: ResultPublisher | None,
    dry_run: bool,
    destinations: tuple[str, str] | None = None,
    artifact_bucket: str | None = None,
) -> RunPublicationReport:
    verification, publications, destinations = _prepare_publications(
        snapshot,
        namespace=namespace,
        reader=reader,
        destinations=destinations,
        artifact_bucket=artifact_bucket,
    )
    result_dataset, index_dataset = destinations
    published: list[PublishedExecution] = []
    for verified, publication in zip(
        verification.executions, publications, strict=True
    ):
        receipt = None
        if not dry_run:
            if publisher is None:
                raise ValueError("result publisher is required outside dry-run")
            receipt = publisher.publish(
                publication,
                result_dataset=result_dataset,
                index_dataset=index_dataset,
            )
        published.append(
            PublishedExecution(
                execution_id=verified.execution_id,
                publication_id=verified.publication_id,
                result_dataset=result_dataset,
                index_dataset=index_dataset,
                published=receipt is not None,
                result_revision=(receipt.result_revision if receipt else None),
                index_revision=(receipt.index_revision if receipt else None),
            )
        )
    return RunPublicationReport(
        run_id=snapshot.lock.run_id,
        control_commit=snapshot.control_commit,
        dry_run=dry_run,
        executions=published,
    )


def _prepare_publications(
    snapshot: RunSnapshot,
    *,
    namespace: str,
    reader: EvidenceReader,
    destinations: tuple[str, str] | None = None,
    artifact_bucket: str | None = None,
) -> tuple[
    ArtifactVerificationReport,
    list[ResultPublication],
    tuple[str, str],
]:
    if (destinations is None) != (artifact_bucket is None):
        raise ValueError(
            "publication destinations and artifact bucket must be overridden together"
        )
    if destinations is None:
        spec = load_experiment_bytes(
            snapshot.request,
            source=f"run {snapshot.lock.run_id} request",
        )
        if spec.remote is None or spec.remote.job.namespace != namespace:
            raise ValueError("run request does not match the control namespace")
        index_dataset = spec.publishing.index_dataset
        if index_dataset is None:
            raise ValueError("run result publication requires index_dataset")
        destinations = (spec.publishing.dataset, index_dataset)
        artifact_bucket = spec.artifacts.bucket
    assert artifact_bucket is not None
    verified: list[VerifiedExecution] = []
    publications: list[ResultPublication] = []
    for run in snapshot.lock.executions:
        source = EvidenceSource(
            bucket=artifact_bucket,
            prefix=f"{snapshot.lock.artifact_prefix}/executions/{run.execution_id}",
        )
        tables = build_result_tables(
            reader,
            source,
            control_commit=snapshot.control_commit,
        )
        observed = tables.executions[0]
        if (
            observed.execution_id != run.execution_id
            or observed.run_id != snapshot.lock.run_id
        ):
            raise ValueError("run evidence does not match the run lock")
        verified.append(
            VerifiedExecution(
                execution_id=run.execution_id,
                publication_id=tables.publication_id,
                source_prefix=source.prefix,
                source_checksum=observed.source_checksum,
                row_counts={
                    "executions": len(tables.executions),
                    "trials": len(tables.trials),
                    "attempts": len(tables.attempts),
                    "metrics": len(tables.metrics),
                    "artifacts": len(tables.artifacts),
                },
            )
        )
        publications.append(build_result_publication(tables))
    report = ArtifactVerificationReport(
        run_id=snapshot.lock.run_id,
        artifact_bucket=artifact_bucket,
        control_commit=snapshot.control_commit,
        executions=verified,
    )
    return report, publications, destinations
