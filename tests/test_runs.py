import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import with_provider_controller
from pydantic import ValidationError

from harbor_hf.control import RunSubmittedPayload, new_event
from harbor_hf.endpoints import deployment_digest
from harbor_hf.models import (
    DeploymentProfile,
    EndpointRef,
    ExperimentSpec,
    MatrixRule,
    RunControllerSpec,
)
from harbor_hf.provider_models import ProviderLimits, ProviderTarget
from harbor_hf.reconciler import (
    AdmissionLimits,
    ReconcileAction,
    ReconcileContext,
    plan_reconciliation,
)
from harbor_hf.runs import (
    RunLock,
    RunPlan,
    RunRecoveryPolicy,
    WaveLock,
    build_run_lock,
    build_run_plan,
    build_wave_lock,
    new_run_id,
    run_json_schemas,
)


def _provider_run_spec(remote_spec: ExperimentSpec) -> ExperimentSpec:
    model = remote_spec.matrix.models[0]
    provider = ProviderTarget(
        id="provider-controller",
        model=model.repo,
        limits=ProviderLimits(max_concurrent_requests=24, max_attempts=2),
    )
    agent = remote_spec.matrix.agents[0].model_copy(
        update={
            "import_path": "harbor_hf_agents.openclaw.agent:OpenClawAgent",
            "parameters": {"openclaw_config": {}},
        }
    )
    return remote_spec.model_copy(
        update={
            "matrix": remote_spec.matrix.model_copy(
                update={"deployments": [provider], "agents": [agent]}
            )
        }
    )


def test_builds_content_addressed_run_plan(remote_spec: ExperimentSpec) -> None:
    plan = build_run_plan(remote_spec)

    assert plan.schema_version == "harbor-hf/run-plan/v1alpha1"
    assert plan.plan_digest.startswith("sha256:")
    assert plan.execution_count == 1
    assert plan.shard_count == 1
    assert plan.trial_count == 1
    assert plan.executions[0].shards[0].trials[0].logical_attempt == 1
    assert plan.executions[0].deployment_digest == deployment_digest(
        remote_spec.matrix.models[0], remote_spec.matrix.deployments[0]
    )


def test_provider_run_plans_690_trials_inside_one_controller_job(
    remote_spec: ExperimentSpec,
) -> None:
    tasks = {f"task-{index:03d}": f"sha256:{index:064x}" for index in range(115)}
    policy = RunControllerSpec(
        planning_trial_seconds=900,
        headroom_factor="1.25",
        wave_reserve_seconds=900,
        controller_reserve_seconds=1800,
        heartbeat_seconds=60,
        stale_after_seconds=600,
        max_attempts=3,
    )
    base = _provider_run_spec(remote_spec)
    assert base.remote is not None
    spec = base.model_copy(
        update={
            "benchmark": base.benchmark.model_copy(
                update={"task_names": ["task-*"], "task_digests": tasks}
            ),
            "execution": base.execution.model_copy(
                update={
                    "attempts": 6,
                    "concurrent_trials": 24,
                    "max_trials_per_shard": 24,
                    "max_shards_per_wave": 4,
                    "timeout_seconds": 16_200,
                    "controller": policy,
                }
            ),
            "remote": base.remote.model_copy(
                update={
                    "job": base.remote.job.model_copy(
                        update={"timeout_seconds": 85_800}
                    )
                }
            ),
        }
    )

    plan = build_run_plan(spec)
    lock = build_run_lock(plan, "provider-690")
    submitted = new_event(
        subject_type="run",
        subject_id=lock.run_id,
        kind="run.submitted",
        producer="cli",
        payload=RunSubmittedPayload(plan_digest=lock.plan_digest),
    )
    actions = plan_reconciliation(
        lock,
        [submitted],
        context=ReconcileContext(
            limits=AdmissionLimits(
                action_limit=64,
                global_active_waves=64,
                deployment_active_waves=64,
                provider_active_waves=64,
                run_active_waves=64,
            )
        ),
    )[1].actions

    assert plan.trial_count == 690
    assert len(plan.initial_waves) == len(lock.initial_waves) == 8
    assert plan.planned_run_duration_seconds == 45_000
    assert all(wave.planned_duration_seconds == 5_400 for wave in plan.initial_waves)
    assert {frozenset(action.shard_ids) for action in actions} == {
        frozenset(wave.shard_ids) for wave in lock.initial_waves
    }
    wave_locks = [build_wave_lock(lock, spec, action) for action in actions]
    assert all(wave.duration_seconds == 4_500 for wave in wave_locks)
    assert all(wave.max_concurrent_shards == 24 for wave in wave_locks)
    assert all(
        wave.max_concurrent_shards == planned.effective_concurrency
        for wave, planned in zip(wave_locks, lock.initial_waves, strict=True)
    )
    retry_trial = next(
        trial
        for run in lock.executions
        for shard in run.shards
        if shard.shard_id in actions[0].shard_ids
        for trial in shard.trials
    )
    retry = actions[0].model_copy(
        update={"kind": "retry-shard", "trial_ids": [retry_trial.trial_id]}
    )
    assert build_wave_lock(lock, spec, retry).duration_seconds == 1_125


def test_provider_run_requires_explicit_controller_policy(
    remote_spec: ExperimentSpec,
) -> None:
    with pytest.raises(ValueError, match="require execution.controller"):
        build_run_plan(_provider_run_spec(remote_spec))


def test_controller_policy_requires_exact_decimal_and_bounded_duration(
    remote_spec: ExperimentSpec,
) -> None:
    raw = {
        "planning_trial_seconds": 1,
        "headroom_factor": 1.25,
        "wave_reserve_seconds": 1,
        "controller_reserve_seconds": 600,
        "heartbeat_seconds": 30,
        "stale_after_seconds": 90,
        "max_attempts": 3,
    }
    with pytest.raises(ValueError, match="decimal string"):
        RunControllerSpec.model_validate(raw)

    spec = with_provider_controller(_provider_run_spec(remote_spec))
    assert spec.remote is not None
    spec = spec.model_copy(
        update={"execution": spec.execution.model_copy(update={"timeout_seconds": 1})}
    )
    with pytest.raises(ValueError, match="wave duration exceeds"):
        build_run_plan(spec)


def test_run_accepts_literal_bracketed_task_name(
    remote_spec: ExperimentSpec,
) -> None:
    deprecated = "[DERPRECATED] duplicate-task"
    raw = remote_spec.model_dump(mode="python")
    raw["benchmark"]["task_names"] = [deprecated]
    raw["benchmark"]["task_digests"] = {deprecated: "sha256:" + "6" * 64}

    plan = build_run_plan(ExperimentSpec.model_validate(raw))

    assert plan.trial_count == 1
    assert plan.executions[0].shards[0].trials[0].task_name == deprecated


def test_shards_order_logical_attempts_across_distinct_tasks(
    remote_spec: ExperimentSpec,
) -> None:
    tasks = {f"task-{index}": f"sha256:{index:064x}" for index in range(1, 6)}
    spec = remote_spec.model_copy(
        update={
            "benchmark": remote_spec.benchmark.model_copy(
                update={"task_names": ["task-*"], "task_digests": tasks}
            ),
            "execution": remote_spec.execution.model_copy(
                update={"attempts": 2, "max_trials_per_shard": 3}
            ),
        }
    )

    plan = build_run_plan(spec)

    assert plan.trial_count == 10
    assert plan.shard_count == 4
    assert [len(shard.trials) for shard in plan.executions[0].shards] == [3, 3, 3, 1]
    ordered = [
        (trial.task_name, trial.logical_attempt)
        for shard in plan.executions[0].shards
        for trial in shard.trials
    ]
    assert ordered == [
        (f"task-{index}", attempt) for attempt in (1, 2) for index in range(1, 6)
    ]


def test_plan_digest_ignores_semantically_irrelevant_input_order(
    remote_spec: ExperimentSpec,
) -> None:
    second_model = remote_spec.matrix.models[0].model_copy(
        update={"id": "z-model", "repo": "example/z-model"}
    )
    first = remote_spec.model_copy(
        update={
            "matrix": remote_spec.matrix.model_copy(
                update={"models": [remote_spec.matrix.models[0], second_model]}
            )
        }
    )
    second = first.model_copy(
        update={
            "benchmark": first.benchmark.model_copy(
                update={
                    "task_digests": dict(reversed(first.benchmark.task_digests.items()))
                }
            ),
            "matrix": first.matrix.model_copy(
                update={"models": list(reversed(first.matrix.models))}
            ),
        }
    )

    first_plan = build_run_plan(first)
    second_plan = build_run_plan(second)

    assert first_plan.plan_digest == second_plan.plan_digest
    assert [run.cell_digest for run in first_plan.executions] == [
        run.cell_digest for run in second_plan.executions
    ]
    assert first_plan.manifest_digest != second_plan.manifest_digest


def test_plan_digest_includes_hosted_judge(remote_spec: ExperimentSpec) -> None:
    raw = remote_spec.model_dump(mode="python")
    raw["benchmark"]["judge"] = {
        "api_url": "https://router.huggingface.co/v1/chat/completions",
        "model": "judge/model-one",
    }
    first = ExperimentSpec.model_validate(raw)
    raw["benchmark"]["judge"]["model"] = "judge/model-two"
    second = ExperimentSpec.model_validate(raw)

    assert build_run_plan(first).plan_digest != build_run_plan(second).plan_digest


def test_matrix_rules_filter_run_cells(remote_spec: ExperimentSpec) -> None:
    second_model = remote_spec.matrix.models[0].model_copy(update={"id": "other-model"})
    spec = remote_spec.model_copy(
        update={
            "matrix": remote_spec.matrix.model_copy(
                update={
                    "models": [remote_spec.matrix.models[0], second_model],
                    "include": [MatrixRule(models=["other-model"])],
                    "exclude": [],
                }
            )
        }
    )

    plan = build_run_plan(spec)

    assert [run.model for run in plan.executions] == ["other-model"]


@pytest.mark.parametrize(
    ("invalid_dimension", "error"),
    [
        (
            "model",
            "Inference Provider target model must match the selected model profile",
        ),
        ("agent", "require one of: hermes, openclaw, openclaw-codex, pi"),
    ],
)
def test_run_plan_validates_every_resolved_provider_matrix_cell(
    remote_spec: ExperimentSpec,
    invalid_dimension: str,
    error: str,
) -> None:
    first_model = remote_spec.matrix.models[0]
    second_model = first_model.model_copy(
        update={"id": "second-model", "repo": "example/second-model"}
    )
    first_provider = ProviderTarget(id="first-provider", model=first_model.repo)
    second_provider = ProviderTarget(
        id="second-provider",
        model=(first_model.repo if invalid_dimension == "model" else second_model.repo),
    )
    first_agent = remote_spec.matrix.agents[0].model_copy(
        update={
            "import_path": "harbor_hf_agents.openclaw.agent:OpenClawAgent",
            "parameters": {"openclaw_config": {}},
        }
    )
    second_agent = first_agent.model_copy(
        update={
            "id": "second-agent",
            "name": "terminus" if invalid_dimension == "agent" else "openclaw",
        }
    )
    spec = remote_spec.model_copy(
        update={
            "matrix": remote_spec.matrix.model_copy(
                update={
                    "models": [first_model, second_model],
                    "deployments": [first_provider, second_provider],
                    "agents": [first_agent, second_agent],
                    "include": [
                        MatrixRule(
                            models=[first_model.id],
                            deployments=[first_provider.id],
                            agents=[first_agent.id],
                        ),
                        MatrixRule(
                            models=[second_model.id],
                            deployments=[second_provider.id],
                            agents=[second_agent.id],
                        ),
                    ],
                }
            )
        }
    )

    with pytest.raises(ValueError, match=error):
        build_run_plan(spec)


def test_run_plan_allows_agent_aliases_for_one_effective_deployment(
    remote_spec: ExperimentSpec,
) -> None:
    model = remote_spec.matrix.models[0]
    provider = ProviderTarget(id="provider-one", model=model.repo)
    first_agent = remote_spec.matrix.agents[0].model_copy(
        update={
            "import_path": "harbor_hf_agents.openclaw.agent:OpenClawAgent",
            "parameters": {"openclaw_config": {}},
        }
    )
    second_agent = first_agent.model_copy(update={"id": "agent-two"})
    spec = remote_spec.model_copy(
        update={
            "matrix": remote_spec.matrix.model_copy(
                update={
                    "deployments": [provider],
                    "agents": [first_agent, second_agent],
                }
            )
        }
    )

    spec = with_provider_controller(spec)
    plan = build_run_plan(spec)
    run = build_run_lock(plan, "run-agent-aliases")
    action = _wave_action(run)
    wave = build_wave_lock(run, spec, action)

    assert plan.execution_count == 2
    assert len({run.deployment_digest for run in plan.executions}) == 1
    assert len(action.shard_ids) == 2
    assert len(wave.executions) == 2
    assert wave.provider_target == provider


def test_run_plan_rejects_duplicate_effective_deployment_aliases(
    remote_spec: ExperimentSpec,
) -> None:
    deployment = remote_spec.matrix.deployments[0]
    assert isinstance(deployment, DeploymentProfile)
    endpoint = deployment.endpoint
    assert endpoint is not None
    alias = deployment.model_copy(
        update={
            "id": "deployment-alias",
            "endpoint": EndpointRef(
                namespace=endpoint.namespace,
                name="alternate-endpoint",
                served_model_name=endpoint.served_model_name,
            ),
        }
    )
    assert deployment_digest(remote_spec.matrix.models[0], alias) == deployment_digest(
        remote_spec.matrix.models[0], deployment
    )
    spec = remote_spec.model_copy(
        update={
            "matrix": remote_spec.matrix.model_copy(
                update={"deployments": [deployment, alias]}
            )
        }
    )

    with pytest.raises(
        ValueError,
        match="deployment digest must resolve to one model and deployment profile pair",
    ):
        build_run_plan(spec)


def test_run_lock_has_stable_scoped_ids(remote_spec: ExperimentSpec) -> None:
    plan = build_run_plan(remote_spec)
    first = build_run_lock(
        plan,
        "run-one",
        clock=lambda: datetime(2026, 7, 14, tzinfo=UTC),
    )
    second = build_run_lock(
        plan,
        "run-one",
        clock=lambda: datetime(2026, 7, 15, tzinfo=UTC),
    )

    assert first.executions[0].execution_id == second.executions[0].execution_id
    assert (
        first.executions[0].shards[0].shard_id
        == second.executions[0].shards[0].shard_id
    )
    assert (
        first.executions[0].shards[0].trials[0].trial_id
        == second.executions[0].shards[0].trials[0].trial_id
    )
    assert second.created_at - first.created_at == timedelta(days=1)
    assert first.artifact_prefix == "runs/run-one"


def test_repeated_plan_submission_gets_distinct_execution_ids(
    remote_spec: ExperimentSpec,
) -> None:
    plan = build_run_plan(remote_spec)

    first = build_run_lock(plan, "run-one")
    second = build_run_lock(plan, "run-two")

    assert first.executions[0].execution_id != second.executions[0].execution_id
    assert (
        first.executions[0].shards[0].trials[0].trial_id
        != second.executions[0].shards[0].trials[0].trial_id
    )


def test_run_id_must_be_one_safe_path_component(
    remote_spec: ExperimentSpec,
) -> None:
    with pytest.raises(ValueError, match="run ID must be one safe path"):
        build_run_lock(build_run_plan(remote_spec), "../unsafe")


def test_run_plan_rejects_inconsistent_counts(
    remote_spec: ExperimentSpec,
) -> None:
    value = build_run_plan(remote_spec).model_dump(mode="json")
    value["trial_count"] = 2

    with pytest.raises(ValidationError, match="counts do not match"):
        RunPlan.model_validate(value)


def test_run_plan_requires_resolved_tasks(remote_spec: ExperimentSpec) -> None:
    spec = remote_spec.model_copy(
        update={
            "benchmark": remote_spec.benchmark.model_copy(
                update={"task_names": ["*"], "task_digests": {}}
            )
        }
    )

    with pytest.raises(ValueError, match="requires resolved task digests"):
        build_run_plan(spec)


def test_exports_run_json_schemas() -> None:
    schemas = run_json_schemas()

    assert set(schemas) == {"run_plan", "run_lock", "wave_lock"}
    assert schemas["run_plan"]["title"] == "RunPlan"
    assert schemas["run_lock"]["title"] == "RunLock"
    assert schemas["wave_lock"]["title"] == "WaveLock"


def test_run_recovery_policy_is_content_addressed_and_stable(
    remote_spec: ExperimentSpec,
) -> None:
    tasks = {f"task-{index}": f"sha256:{index:064x}" for index in range(1, 6)}
    second_model = remote_spec.matrix.models[0].model_copy(
        update={"id": "model-two", "repo": "example/model-two"}
    )
    spec = remote_spec.model_copy(
        update={
            "benchmark": remote_spec.benchmark.model_copy(
                update={"task_names": ["task-*"], "task_digests": tasks}
            ),
            "matrix": remote_spec.matrix.model_copy(
                update={"models": [remote_spec.matrix.models[0], second_model]}
            ),
            "execution": remote_spec.execution.model_copy(
                update={"attempts": 2, "max_trials_per_shard": 3}
            ),
        }
    )
    policy = RunRecoveryPolicy(
        max_active_waves=3,
        max_physical_attempts_per_trial=4,
        retry_base_seconds=17,
        retry_max_seconds=99,
        cancellation_grace_seconds=23,
        spend_cap_microusd=123_456,
    )
    default_plan = build_run_plan(spec)
    plan = build_run_plan(spec, recovery_policy=policy)
    lock = build_run_lock(
        plan, "run-policy", clock=lambda: datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert plan.plan_digest != default_plan.plan_digest
    assert lock.recovery_policy == policy
    encoded = json.dumps(
        [plan.model_dump(mode="json"), lock.model_dump(mode="json")],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == (
        "cd68f24c14a84ab6d6348b6643412f7ce0083f889016c60b31cb0577aa2f6f53"
    )


def test_wave_lock_reuses_the_run_recovery_policy(
    remote_spec: ExperimentSpec,
) -> None:
    policy = RunRecoveryPolicy(
        max_physical_attempts_per_trial=5,
        retry_base_seconds=7,
    )
    run = build_run_lock(
        build_run_plan(remote_spec, recovery_policy=policy),
        "run-policy-wave",
    )

    wave = build_wave_lock(run, remote_spec, _wave_action(run))

    assert wave.run_id == run.run_id
    assert run.recovery_policy == policy


def test_run_recovery_policy_rejects_unbounded_base_delay() -> None:
    with pytest.raises(ValidationError, match="retry base seconds must not exceed"):
        RunRecoveryPolicy(retry_base_seconds=61, retry_max_seconds=60)


def test_wave_lock_is_deterministic_and_bounded(remote_spec: ExperimentSpec) -> None:
    tasks = {
        "task-one": "sha256:" + "3" * 64,
        "task-two": "sha256:" + "4" * 64,
    }
    spec = remote_spec.model_copy(
        update={
            "benchmark": remote_spec.benchmark.model_copy(
                update={"task_names": ["task-*"], "task_digests": tasks}
            ),
            "execution": remote_spec.execution.model_copy(
                update={"max_trials_per_shard": 1, "concurrent_trials": 2}
            ),
        }
    )
    run = build_run_lock(build_run_plan(spec), "run-one")
    action = _wave_action(run)

    first = build_wave_lock(run, spec, action)
    second = build_wave_lock(run, spec, action)

    assert first == second
    assert first.wave_id == f"wave-{action.action_key}"
    assert first.action_id == action.action_id
    assert first.action_key == action.action_key
    assert first.run_id == run.run_id
    assert first.created_at == run.created_at
    assert first.manifest_digest == run.manifest_digest
    assert first.plan_digest == run.plan_digest
    assert first.deployment_digest == action.deployment_digest
    deployment = spec.matrix.deployments[0]
    assert isinstance(deployment, DeploymentProfile)
    assert first.endpoint == deployment.endpoint
    assert first.artifact_bucket == spec.artifacts.bucket
    assert first.artifact_prefix == (f"runs/{run.run_id}/waves/{first.wave_id}")
    assert first.shard_ids == action.shard_ids
    assert first.max_shards == run.max_shards_per_wave
    assert first.max_concurrent_shards == 2
    assert first.duration_seconds == spec.execution.timeout_seconds
    assert sum(len(execution.shards) for execution in first.executions) == 2
    assert first.remote == spec.remote
    locked_execution = first.executions[0]
    planned_execution = run.executions[0]
    assert locked_execution.artifact_prefix == (
        f"runs/{run.run_id}/executions/{planned_execution.execution_id}"
    )
    assert locked_execution.configuration.execution_id == planned_execution.execution_id
    assert locked_execution.configuration.model.id == planned_execution.model
    assert locked_execution.configuration.deployment.id == planned_execution.deployment
    assert locked_execution.configuration.agent.id == planned_execution.agent
    assert sorted(shard.shard.shard_id for shard in locked_execution.shards) == sorted(
        action.shard_ids
    )
    assert all(
        shard.execution_id == planned_execution.execution_id
        for shard in locked_execution.shards
    )
    assert all(
        shard.artifact_prefix
        == (
            f"runs/{run.run_id}/executions/{planned_execution.execution_id}/"
            f"shards/{shard.shard.shard_id}"
        )
        for shard in locked_execution.shards
    )
    assert WaveLock.model_config["frozen"] is True


def test_wave_lock_rejects_tampered_or_unbound_actions(
    remote_spec: ExperimentSpec,
) -> None:
    run = build_run_lock(build_run_plan(remote_spec), "run-one")
    action = _wave_action(run)
    tampered = action.model_copy(update={"action_id": "act-" + "0" * 24})

    with pytest.raises(ValueError, match="identity does not match"):
        build_wave_lock(run, remote_spec, tampered)

    deployment = remote_spec.matrix.deployments[0].model_copy(update={"endpoint": None})
    unbound = remote_spec.model_copy(
        update={
            "matrix": remote_spec.matrix.model_copy(
                update={"deployments": [deployment]}
            )
        }
    )
    unbound_run = build_run_lock(build_run_plan(unbound), "run-unbound")
    with pytest.raises(ValueError, match="pre-existing endpoint binding"):
        build_wave_lock(unbound_run, unbound, _wave_action(unbound_run))


def test_wave_lock_enforces_shard_bound(remote_spec: ExperimentSpec) -> None:
    lock = build_run_lock(build_run_plan(remote_spec), "run-one")
    execution = lock.executions[0]
    shard_id = execution.shards[0].shard_id
    action_key = "0" * 24
    oversized = ReconcileAction(
        action_id=f"act-{action_key}",
        action_key=action_key,
        kind="submit-wave",
        run_id=lock.run_id,
        deployment_digest=execution.deployment_digest,
        shard_ids=[
            f"{shard_id}-{index}" for index in range(lock.max_shards_per_wave + 1)
        ],
    )

    with pytest.raises(ValueError, match="exceeds the run shard bound"):
        build_wave_lock(lock, remote_spec, oversized)


def test_retry_wave_locks_only_trials_admitted_by_its_action(
    remote_spec: ExperimentSpec,
) -> None:
    run = build_run_lock(build_run_plan(remote_spec), "run-one")
    action = _wave_action(run)
    trial_id = run.executions[0].shards[0].trials[0].trial_id

    retry = action.model_copy(
        update={
            "kind": "retry-shard",
            "trial_ids": [trial_id],
            "estimated_cost_microusd": 1,
        }
    )
    lock = build_wave_lock(run, remote_spec, retry)
    assert lock.action_kind == "retry-shard"
    assert lock.trial_ids == [trial_id]
    assert lock.estimated_cost_microusd == (
        run.executions[0].estimated_wave_cost_microusd or 0
    )

    with pytest.raises(ValueError, match="must admit at least one trial"):
        build_wave_lock(
            run,
            remote_spec,
            action.model_copy(update={"kind": "retry-shard", "trial_ids": []}),
        )
    with pytest.raises(ValueError, match="trial IDs must be unique"):
        build_wave_lock(
            run,
            remote_spec,
            retry.model_copy(update={"trial_ids": [trial_id, trial_id]}),
        )
    with pytest.raises(ValueError, match="outside its shards"):
        build_wave_lock(
            run,
            remote_spec,
            retry.model_copy(update={"trial_ids": ["trial-" + "f" * 24]}),
        )
    with pytest.raises(ValueError, match="cannot admit individual trials"):
        build_wave_lock(
            run,
            remote_spec,
            action.model_copy(update={"trial_ids": [trial_id]}),
        )


def test_new_run_id_uses_utc_plan_identity_and_bounded_nonce(
    remote_spec: ExperimentSpec,
) -> None:
    plan = build_run_plan(remote_spec)
    run_id = new_run_id(
        plan,
        clock=lambda: datetime(2026, 7, 14, 9, 8, 7, tzinfo=UTC),
        identifier=lambda: "0123456789abcdef",
    )

    assert run_id == (
        f"20260714T090807Z-{plan.plan_digest.removeprefix('sha256:')[:10]}-0123456789"
    )


def _wave_action(lock: RunLock) -> ReconcileAction:
    from harbor_hf.control import RunSubmittedPayload, new_event

    event = new_event(
        subject_type="run",
        subject_id=lock.run_id,
        kind="run.submitted",
        producer="cli",
        payload=RunSubmittedPayload(plan_digest=lock.plan_digest),
    )
    return plan_reconciliation(lock, [event])[1].actions[0]
