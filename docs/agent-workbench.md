# Agent Workbench

Agent Workbench is an authenticated control-web page for authoring and privately
testing a generic command-line Harbor agent recipe. It is available at
`/workbench`.

The Workbench presents four distinct stages:

1. **Configure** setup and run commands plus typed environment bindings.
2. **Test** installation in a disposable local Docker container or Hugging Face
   Job without a benchmark or model request.
3. **Publish** by matching the exact tested compiler output to an existing
   approved immutable harness profile.
4. **Run** through the normal launcher, where benchmark, model, deployment,
   launch policy, and cost ceiling are selected and separately confirmed.

A successful setup test is ephemeral verification. It does not create, approve,
or publish a harness profile and does not start a Run. The current Publish stage
checks the existing reviewed profile catalog; arbitrary recipe finalization
remains a later generic profile workflow.

The current implementation can continue an exact setup-tested recipe to the
normal Run launcher only when its compiler output exactly matches a reviewed
immutable harness profile and an approved compatible deployment exists. The
handoff sends only the non-secret harness alias. It does not submit the recipe
through a Workbench-specific Run API.

Arbitrary edited recipes remain setup-test-only. Persisting those recipes as
actor-owned private profiles is a later workflow.

## Recipe contract

The authoritative contract is
`packages/contracts/schemas/agent-workbench-v1.schema.json`.

A recipe contains:

- a portable configuration name;
- setup and run shell scripts;
- the OpenAI-compatible inference API expected by the agent:
  `chat-completions` or `responses`;
- a bounded setup timeout;
- typed environment bindings; and
- declared result and optional ATIF trajectory paths beneath `/logs/agent`.

Environment sources are:

| Source | Setup | Run | Meaning |
| --- | --- | --- | --- |
| `literal` | yes | yes | Non-secret recipe text. |
| `workspace_path` | yes | yes | Writable task workspace. |
| `logs_path` | yes | yes | Agent log/output directory. |
| `agent_home` | yes | yes | Managed agent installation and runtime home. |
| `model_name` | yes | yes | Model selected by the immutable Run lock. |
| `instruction_path` | no | yes | File containing the task instruction. |
| `model_base_url` | no | yes | Direct model endpoint from the immutable execution contract. |
| `model_api_key` | no | yes | Runtime model credential injected by the worker. |

Setup compilation rejects references to run-only bindings. Commands receive
instructions by file path; the instruction text is never interpolated into a
command.

Literal values are rejected when their names or values look credential-like.
Reserved process and Harbor-HF environment names are also rejected. The
preview redacts the model API-key binding.

## Generic Harbor agent

The compiler emits a normal Harbor harness profile using:

```text
harbor_hf_agents.command_agent.agent:CommandAgent
```

The command agent:

- uses only Harbor's public installed-agent APIs;
- accepts script or argv commands through a strict Pydantic configuration;
- runs setup and task commands as the isolated unprivileged agent user;
- starts commands with an empty ambient environment and only declared
  bindings;
- stages instructions as a mode-0600 file;
- obtains inference directly from the model endpoint in the immutable execution
  contract;
- collects required declared output files from `/logs/agent`;
- optionally validates, canonicalizes, and ingests a declared ATIF document;
  and
- writes bounded setup and run process logs.

The compiler always requires workspace and verifier evidence. A declared ATIF
path additionally requires trajectory evidence.

Recipe setup failures and client-configuration failures after agent setup
begins are sealed as non-retryable agent outcomes. Typed task environment
failures and typed transient provider failures remain the bounded
replacement-eligible infrastructure cases.

The harness profile omits a fixed model name. During normal Harbor preparation,
the worker injects the model name from the selected immutable model profile.
If a harness explicitly declares a different model, preparation rejects it.

## Disposable setup runners

The control API supports:

```text
POST /api/v1/workbench/preview
POST /api/v1/workbench/setup-tests
GET  /api/v1/workbench/setup-tests
GET  /api/v1/workbench/setup-tests/{setup_test_id}
POST /api/v1/workbench/setup-tests/{setup_test_id}/cancel
GET  /api/v1/workbench/setup-tests/{setup_test_id}/logs
GET  /api/v1/workbench/setup-tests/{setup_test_id}/files/{file_id}
```

## Operator CLI

The Python operator CLI exposes the same actor-scoped Workbench control
surface without adding a Workbench-specific Run path:

```text
harbor-hf workbench preview RECIPE.json
harbor-hf workbench setup start RECIPE.json
harbor-hf workbench setup list
harbor-hf workbench setup status SETUP_TEST_ID
harbor-hf workbench setup wait SETUP_TEST_ID
harbor-hf workbench setup cancel SETUP_TEST_ID
harbor-hf workbench setup logs SETUP_TEST_ID
harbor-hf workbench setup files SETUP_TEST_ID
harbor-hf workbench setup file SETUP_TEST_ID FILE_ID
harbor-hf workbench publication RECIPE.json --setup-test SETUP_TEST_ID
```

`RECIPE.json` may be `-` to read one UTF-8 JSON object from stdin. Setup start
requires a separate confirmation unless `--yes` is supplied, and stdin setup
start always requires `--yes`. Generated idempotency keys are printed only to
stderr so stdout remains machine-readable JSON.

`setup start --wait` and `setup wait` poll actor-scoped setup state and exit
nonzero unless the setup passes. They never cancel a setup test when the local
wait deadline expires. `setup cancel --wait` succeeds only when cancellation
reaches the `cancelled` terminal state.

Logs are JSON by default. `--channel stdout` and `stderr` preserve the selected
text exactly; `combined` inserts a visible `[stderr]` separator when stderr is
present. File previews are JSON by default; `--output` writes a mode-0600 local
file atomically, refuses overwrite without `--force`, and refuses a truncated
preview without `--allow-truncated`.

The `publication` command mirrors the browser's exact publication-state check:
the current recipe must match the passed setup digest and revision, the
compiler-produced harness profile must exactly equal an approved immutable
harness profile, and an approved compatible model and deployment must exist.
It reports `test-required`, `unpublished`, `published-no-deployment`, or
`published`. It does not create or approve profiles and does not submit a Run.
Use `harbor-hf run submit` for the separately confirmed benchmark, model,
deployment, launch policy, and cost ceiling.

Setup execution is controlled by:

| Variable | Values | Behavior |
| --- | --- | --- |
| `HARBOR_HF_WORKBENCH_RUNNER` | `disabled`, `docker`, `hf-jobs` | Development defaults to `docker`; production and tests default to `disabled`. Selecting `hf-jobs` requires `HF_TOKEN`. |
| `HARBOR_HF_WORKBENCH_IMAGE` | Docker image reference | Defaults to `python:3.12-slim`; hosted installations should configure a reviewed immutable image reference. |

The Docker runner:

- uses an unprivileged UID/GID;
- drops all Linux capabilities and sets `no-new-privileges`;
- applies CPU, memory, PID, and timeout limits;
- keeps managed agent-home files outside the browsable workspace;
- bounds retained stdout, stderr, file count, and text previews;
- refuses symlink and special-file previews; and
- scopes in-memory setup state to the authenticated actor.

The Hugging Face Jobs runner is a thin disposable adapter. It:

- calls the Hugging Face Jobs API directly rather than creating a Harbor Run,
  profile, lock, action intent, or reconciler record;
- checks the configured namespace active-Job limit before launching;
- launches one `cpu-basic` Job with one attempt and the recipe's setup timeout
  plus a short finalization allowance;
- labels the Job with opaque setup, actor, recipe, and revision digests so the
  Workbench can recover the actor's recent setup tests after a service restart;
- supplies no Job secrets, volumes, worker capability, inference credential, or
  model route;
- uses the control credential only in the control-service-to-Hub request;
- runs the customer command with a constructed environment containing only
  declared setup bindings plus managed `HOME` and `PATH`;
- frames bounded stdout, stderr, file metadata, text previews, and the final
  exit result through the private Job log stream; and
- observes and cancels the exact Job directly through the Jobs API.

After submission, the web application replaces the one-time confirmation
control with one inline live-output panel. The panel polls status and bounded
stdout/stderr once per second, follows new output, and provides a separately
confirmed Cancel action. Once setup is terminal, the live panel is replaced by
the final stdout/stderr and created-file views; the same logs are not displayed
twice. Cancellation stops the configured disposable environment, retains the
available logs and files, and records the terminal setup state as `cancelled`
rather than a generic failure.

Setup-test API state, reconstructed logs, and file previews remain intentionally
ephemeral and actor-scoped. A graceful control-service shutdown cancels active
setup environments; an abrupt failure can leave an HF Job running only until
its bounded remote timeout. The remote Job is the lifecycle object, but the
Workbench does not turn setup verification into durable benchmark state.

## Fast-Agent starter

The starter bootstraps a version-pinned command-agent toolchain from immutable
recipe data:

```text
uv 0.12.5
CPython 3.12.14
fast-agent-mcp==0.10.11
```

The setup command downloads the pinned Linux `uv` archive, verifies its exact
SHA-256 before execution, installs the selected managed Python version beneath
the agent home, and installs the selected Fast-Agent package version into that
managed environment. It does not depend on an ambient task-image Python, venv,
or pip. Download diagnostics remain beneath the non-browsable managed agent
home.

Its run command uses typed bindings for model name, direct model base URL,
runtime `OPENAI_API_KEY`, instruction file, workspace, managed home, results,
and trajectory output. Fast-Agent and its toolchain remain recipe data; neither
the Workbench compiler nor the command-agent plugin branches on their names.

The checked-in `fast-agent-0-10-11-command` harness profile is derived from the
same compiler output returned by the Workbench preview. After setup passes, the
Workbench compares the complete canonical harness spec rather than the recipe
name. A different command, binding, output, timeout, or version produces a
different spec and cannot use that reviewed alias.

## Benchmark handoff

The narrow reviewed-profile flow is:

1. Setup passes for the current recipe digest and revision.
2. The browser loads approved profiles from the normal profile catalog.
3. It compares the complete canonical compiler-produced harness spec with
   reviewed harness profiles.
4. It requires an approved deployment that names that exact harness alias and
   at least one approved model.
5. It opens the existing Run launcher with only that harness alias.
6. The user selects the benchmark, model, deployment route, diagnostic launch
   policy, and cost ceiling, then submits through `POST /api/v1/runs`.

The browser reports publication as one of these explicit states:

- checking the approved profile catalog;
- profile catalog unavailable;
- setup-tested recipe is unpublished;
- exact recipe is published but has no compatible deployment; or
- exact recipe is published and ready for the normal Run launcher.

The launcher selects harnesses by exact immutable alias. Reasoning is displayed
from the selected harness profile rather than being used to guess a harness by
agent name.

The Fast-Agent profile is ready for this flow, but a hosted canary also requires
a reviewed, digest-pinned deployment whose worker image contains the generic
command-agent plugin. The repository must record the real published image
digest and exact worker revision; a placeholder or mutable image reference is
not accepted.

Arbitrary customer recipes still require:

1. actor-owned private profile finalization;
2. owner-scoped profile resolution;
3. forced diagnostic-only policy and trial ceilings; and
4. generic capability-based deployment matching.

There is no Workbench-specific Run endpoint, worker, reconciler, resource, or
benchmark/model/harness-name branch.
