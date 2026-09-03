---
title: Simplify Harbor-HF around Harbor
author: Harbor-HF maintainers
date: 2026-09-03
tags: [architecture, profiles, runs, harbor, cleanup]
---

# Simplify Harbor-HF around Harbor

## In short

Harbor-HF is a control plane that runs Harbor benchmarks on Hugging Face
infrastructure. It grew quickly while the maintainers needed benchmark results
for a paper, and it now re-implements most of what Harbor already does. This
plan makes Harbor-HF a thin layer again. Harbor owns the job loop, retries,
resume, the reproducibility lock, and the result files. Harbor-HF owns the
hosted Space with its submission API and form. It also owns credential handling
and Bucket storage, plus a cost ceiling on each trial and the leaderboard.

The plan replaces the five profile kinds with one run record that wraps a Harbor
`JobConfig`. It switches trial execution to Harbor's own `hf-sandbox`
environment. It also deletes about 90,000 lines of Python that no deployment
uses.

**Status.** Proposed. Nothing in this document is implemented. The plan needs
agreement from the maintainers on the open questions at the end before work
starts.

## Problem

A person who wants to submit a run today must first pick a benchmark profile and
a model profile, then a harness profile and a launch policy. The form refuses
the submission unless the launch policy lists that exact combination, and a
deployment profile that also lists the combination must exist. None of these
profiles is something a submitter cares about. They want to name a model and an
agent and pick a benchmark size. After that they expect a score without reading
a profile catalog first.

The profiles exist because Harbor-HF runs its own job loop. The trial worker in
`packages/harbor-hf-agents` writes a one-trial Harbor config with `n_attempts`
and concurrency both set to 1 and with retries disabled. The TypeScript control
service then rebuilds concurrency, retries, budget holds, and resume on top of
HF Jobs. Every rule that Harbor would have applied inside one job now needs a
durable record and a profile field.

The table shows where the code is. Counts are source lines and exclude generated
files and tests.

| Area | Lines | Notes |
| --- | --- | --- |
| `src/harbor_hf` | 40,412 | No Dockerfile copies it. The `harbor-hf` CLI does not import it. |
| `tests/` for `src/harbor_hf` | 49,079 | CI enforces 85% coverage on this code. |
| `packages/control-core` | 11,890 | About 2,600 lines handle retries and the budget bookkeeping behind them. |
| `apps/control-api` and `apps/control-web` | 15,000 | The launch form is about 490 lines. |
| `packages/harbor-hf-agents` | 11,544 | 2,700 lines are an OCI runtime built on proot. |
| `profiles/` | 62 files | Five kinds. A deployment spec has 60 fields. |
| `schemas/` | 23 files | Ten files are referenced by nothing. |

The Python package is the largest item. It is the previous control plane and
three generations of worker. The control Space image copies `apps/` and
`packages/` together with the `profiles/` directory. The trial worker image
copies `packages/harbor-hf-agents`. Neither copies `src/`. The only live file in
that directory is `cli.py`, a 434-line HTTPS client that imports nothing else
from the package.

The live TypeScript code has its own duplication. The run lock stores each
selected profile spec three times. Two copies sit under `profiles` and
`execution`, and a pointer copy sits under `source_profiles`. The profile
compatibility rule is implemented four times, in the profile resolver and the
execution contract on the server, and in the launch helper and a form effect in
the browser. Retries take five separate paths, and a three-level chain of
continuation and repair records exists only for runs created before the last
profile cutover. The publication step writes five Parquet tables that no code in
the repository reads.

The profile churn is visible in the history. Of the 336 commits since August 1,
54 touch `profiles/`. Most of them pin a new worker image digest after an agent
fix, because the Harbor version and the worker revision live in every deployment
profile.

## Existing Harbor coverage

Harbor is the benchmark framework that Harbor-HF wraps. The items below are in
Harbor `origin/main` as of this date. File paths refer to the Harbor repository.

Harbor defines a job as a `JobConfig` in `src/harbor/models/job/config.py`. The
config carries the dataset reference, task include and exclude globs,
`n_attempts`, `n_concurrent_trials`, a `RetryConfig` with typed include and
exclude lists and backoff, and one `AgentConfig` per agent. The CLI reads the
config from a local path or an HTTPS URL, and flags override file values.

Harbor resumes a partial job from disk. Opening a `Job` in an existing job
directory keeps every trial that has a `result.json` and reruns the rest. The
`harbor job resume` command adds `--filter-error-type`, which reruns only trials
whose recorded exception matches. This covers the infrastructure retry case that
Harbor-HF implements in `reconciler.ts` and `service.ts`.

Harbor writes a reproducibility lock. `lock.json` records the Harbor version and
commit together with the task digests. It also holds the resolved agent config
and the environment config for each trial. `result.json` records reward, token
counts, cost, timing, the agent version, and the model name and provider for
each trial. The trajectory lands in `agent/trajectory.json` in ATIF format.

Harbor has an HF Jobs environment. `src/harbor/environments/hf_sandbox.py` runs
each trial as a Hugging Face Sandbox backed by HF Jobs, with a `flavor` kwarg
for hardware and a `job_timeout` kwarg. Maintainers from Hugging Face added it
and fixed its shell handling upstream. It replaces the proot runtime in
`job_oci_runtime.py`.

Harbor has built-in agents for pi, hermes, openclaw, codex, opencode, openhands,
mini-swe-agent, kimi-code, qwen-coder, fx and terminus-2. Each agent takes
`model_name` in `provider/model` form and a `version` kwarg that pins the
install. Provider API keys and base URLs resolve from environment variables
through the table in `src/harbor/agents/model_connection.py`.

Harbor exports traces to a Hugging Face Dataset with `--export-push`, and it has
a leaderboard CLI under `harbor hub leaderboard`.

## Remaining wrapper scope

Four things remain for a wrapper to do.

Harbor has no HF Inference Providers routing. The model name convention is
`provider/model`, and there is no `model:provider` suffix and no default base
URL for the Hugging Face router. Harbor-HF currently handles this with the
inference bridge in `hf_inference_bridge.py`.

Harbor has no cost ceiling. It accounts tokens and cost per trial, and a few
agents accept their own budget flags, but nothing stops a job when spend crosses
a limit.

Harbor has no hosted control plane on Hugging Face. Its own remote dispatch runs
on Harbor-hosted infrastructure. A Space that accepts submissions and holds
credentials, and that publishes results afterwards, is Harbor-HF's job.

The `hf-sandbox` environment has gaps. It needs a prebuilt `docker_image` in
`task.toml` and cannot build a task Dockerfile. It ignores user switching and
declares no network policy capability.

## Target design

### Run record

A run is a Harbor `JobConfig` plus a small envelope. The envelope holds the
identity of the run and its submitter, plus the Harbor version and the cost
ceiling.

```json
{
  "run_id": "run-…",
  "created_at": "2026-09-03T12:00:00Z",
  "submitted_by": "<operator>",
  "harbor_version": "0.22.0",
  "cost_ceiling_usd_per_trial": 2.0,
  "harbor_job_config": {
    "datasets": [{ "name": "terminal-bench@2.1", "n_tasks": 89 }],
    "n_attempts": 5,
    "n_concurrent_trials": 8,
    "agents": [
      {
        "name": "pi",
        "model_name": "huggingface/openai/gpt-oss-120b:together",
        "kwargs": { "version": "0.84.2", "reasoning_effort": "high" }
      }
    ],
    "environment": { "type": "hf-sandbox", "kwargs": { "flavor": "cpu-upgrade" } }
  }
}
```

The `harbor_job_config` field is stored as Harbor accepts it. Harbor-HF
validates it by calling Harbor's own config parser and adds nothing to it. The
run record, the Harbor `config.json`, `lock.json`, and `result.json` together
are the reproducibility record. Nothing else needs to be stored for that
purpose.

Reasoning effort lives on the agent config only, because that is where Harbor
puts it. The model profile's `revision` field goes away. For a provider-routed
model the revision is unknown, and for a future Inference Endpoint the endpoint
config records it.

### Profile kinds

The model, harness, deployment, launch policy, and capacity profile kinds are
removed. Two kinds of preset remain, and both are plain `JobConfig` fragments.

A benchmark preset names a dataset, a task filter, `n_attempts`, and a
concurrency limit. The initial presets cover Terminal-Bench 2.1 at increasing
sizes. One task with one trial checks that a setup works. All tasks with one
trial gives a quick score, while all tasks with five trials is the official
measurement. Fixed presets keep runs comparable across submitters.

An agent preset names a Harbor agent and a pinned version. Harbor's `harbor
agent schema` command lists each agent's accepted kwargs, so the form can read
that list instead of keeping its own table.

### Submission form

The form asks for five values. A benchmark preset and an agent preset come from
the two catalogs. The submitter types a model id and a reasoning effort, and
sets a cost ceiling per trial. The runtime is fixed to Inference Providers for
the first version. Submitter-supplied API keys for providers such as OpenAI and
Anthropic come next, because those models are the most requested. Inference
Endpoints and running an agent from a fork commit are recorded as later work.

### Execution

The Space runs `harbor run` with the `hf-sandbox` environment. Harbor then
launches one HF Job per trial and owns concurrency, retries, resume and the
result files. Each task still runs as its own Job, and the Space remains the
process that notices failures and reruns them.

Harbor's `jobs_dir` lives on Space persistent storage so a Space restart resumes
the job in place. The orchestrator must not run inside an HF Job, because Jobs
stop after 24 hours and a full Terminal-Bench run with five trials per task can
take longer.

The cost ceiling is a Harbor job plugin in Harbor-HF. Harbor calls
`BaseJobPlugin` hooks on job start and end and trial hooks on each trial event,
which is enough to read `cost_usd` from finished trials and cancel the job when
the ceiling is crossed.

### Results and leaderboard

After each trial finishes, the Space copies the Harbor job directory to the
Bucket unchanged. The leaderboard reads `result.json` for pass rate and token
counts as well as cost and timing. The Parquet tables and the result catalog are
removed, along with the publication receipt and the supersession chain.

Historical runs in the Bucket stay as a read-only archive under their existing
paths. They are not migrated to the new record shape.

### Command-line client

The `harbor-hf` CLI stays. It is a thin HTTPS client for the control API and
keeps that role. Two commands fit the new shape well. `harbor-hf submit --config
job.yaml` sends a Harbor `JobConfig` file to the Space as it is, and `harbor-hf
run status` reads the projection. The package loses the 66 dead modules that
share its directory and the dependencies they pulled in, such as `pyarrow` and
`zstandard`.

## Upstream contributions

Several parts of the current wrapper belong in Harbor. Hugging Face maintainers
already own the `hf-sandbox` path there, so these do not conflict with Harbor's
direction.

The first is Inference Providers routing. Adding a default base URL for the
`huggingface` provider in `model_connection.py` and passing the `:provider`
suffix through is a small change. Once it lands, most of the 13 agent wrappers
in `packages/harbor-hf-agents` lose their reason to exist, because they mainly
redirect the agent to the local bridge.

The second is the set of local `hf-sandbox` fixes that are not yet upstream. Six
fix branches exist in a maintainer's Harbor checkout. They cover startup
readiness, working directory creation, mounts and private task environments. The
prebuilt image requirement and user switching are the two remaining gaps worth a
Harbor issue each.

The third is trajectory export. The pi, hermes, openclaw and dsh agents in this
repository each convert native sessions to ATIF by hand. Those converters belong
in the upstream agents, and local Harbor branches already exist for pi and
openclaw.

The fourth is a per-trial cost ceiling as a `JobConfig` field. Until Harbor
accepts it, the job plugin described above does the work here.

## Deletion plan

Work proceeds in four phases. Each phase leaves the repository deployable.

The first phase has no runtime risk and can happen now. It removes every module
in `src/harbor_hf` except `cli.py`, the Python tests for those modules, the 85%
coverage gate in `.github/workflows/ci.yml`, the ten unreferenced files in
`schemas/`, the empty `space/` and `apps/results-web` directories,
`scripts/build_space_release.py`, and the one-shot migration scripts. It also
rewrites `docs/architecture.md` and retires `docs/run-spec.md`, both of which
describe the deleted Python design. The untracked `mutants/` directories, about
774 MB, go as well.

The second phase changes the data model. It introduces the run record and the
two preset kinds in place of the five profile kinds, and the four compatibility
implementations go with them. The launch form shrinks to the fields listed
above. This is the phase that fixes the submission page.

The third phase changes execution. The Space starts calling `harbor run` with
`hf-sandbox`. The proot runtime and both workers in `packages/harbor-hf-agents`
are deleted, together with the inference bridge and the retry and continuation
machinery in `control-core`. The open questions below must be answered before
this phase.

The fourth phase changes results. The Bucket receives Harbor job directories and
the leaderboard reads `result.json` from them, so the Parquet publication path
is deleted.

## Open questions

Every Terminal-Bench 2.1 task must have a prebuilt `docker_image`, because
`hf-sandbox` cannot build one. This needs a check against the task registry
before the third phase.

No task may need user switching or a network policy inside the sandbox. The
proot runtime enforced both, and `hf-sandbox` does neither.

The current design keeps the inference token out of the agent process through a
root-owned bridge. With `hf-sandbox` the key is in the agent environment, and
Harbor scrubs it from logs. The maintainers need to decide whether that is
acceptable for the first version, at least for keys the submitter supplies.

The Harbor checkout used while writing this plan was 16 days behind
`origin/main`. It misses the hosted job commands, `harbor agent schema` and the
shell fix for `hf-sandbox`. Implementation work must start from a current
checkout.

## Related documents

This plan supersedes the design in [architecture](architecture.md) and the [run
specification](run-spec.md). It also supersedes the [reusable harness profiles
plan](2026-08-28-reusable-harness-profiles-plan.md). The [control service
specification](CONTROL_SERVICE.md) remains the reference for the Space runtime,
authentication, the Bucket store, and the projection until the second phase
updates its profile and run sections. The [task result retry
plan](2026-09-01-task-result-retry-plan.md) describes the retry mechanism that
the third phase removes.
