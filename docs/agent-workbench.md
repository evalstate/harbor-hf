# Agent Workbench

Agent Workbench is an authenticated control-web page for authoring and privately
testing a generic command-line Harbor agent recipe. It is available at
`/workbench`.

The Workbench is intentionally low ceremony:

1. Start from the Fast-Agent 0.10.11 or FX 0.0.6 recipe, or edit arbitrary
   setup and run commands.
2. Bind environment variables to typed runtime values instead of pasting
   credentials.
3. Review the server-generated command and environment preview.
4. Confirm and run setup in a disposable local Docker container or Hugging Face
   Job.
5. Inspect bounded logs and files created beneath the setup workspace or logs
   directory.

The current implementation verifies setup only. It does not yet persist a
customer recipe as a durable private harness profile or bind it to a benchmark
Run. The UI states this limitation and does not silently navigate to an
unrelated Run launcher.

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
| `model_base_url` | no | yes | Root-owned loopback inference bridge URL. |
| `model_api_key` | no | yes | Non-secret placeholder accepted by the bridge. |

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
- obtains inference only through the root-owned Job bridge;
- collects required declared output files from `/logs/agent`;
- optionally validates, canonicalizes, and ingests a declared ATIF document;
  and
- writes bounded setup and run process logs.

The compiler derives evidence requirements from capabilities rather than the
agent name. Route bindings require provider-usage evidence. A declared ATIF
path requires trajectory evidence.

The harness profile omits a fixed model name. During normal Harbor preparation,
the worker injects the model name from the selected immutable model profile.
If a harness explicitly declares a different model, preparation rejects it.

## Disposable setup runners

The control API supports:

```text
POST /api/v1/workbench/preview
POST /api/v1/workbench/setup-tests
GET  /api/v1/workbench/setup-tests/{setup_test_id}
POST /api/v1/workbench/setup-tests/{setup_test_id}/cancel
GET  /api/v1/workbench/setup-tests/{setup_test_id}/logs
GET  /api/v1/workbench/setup-tests/{setup_test_id}/files/{file_id}
```

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
control with an inline active-setup panel. The panel polls status and bounded
stdout/stderr once per second, follows new output, and provides a separately
confirmed Cancel action. Cancellation stops the configured disposable
environment, retains the available logs and files, and records the terminal
setup state as `cancelled` rather than a generic failure.

Setup-test API state, reconstructed logs, and file previews remain intentionally
ephemeral and actor-scoped. A graceful control-service shutdown cancels active
setup environments; an abrupt failure can leave an HF Job running only until
its bounded remote timeout. The remote Job is the lifecycle object, but the
Workbench does not turn setup verification into durable benchmark state.

## Fast-Agent starter

The starter installs exactly:

```text
fast-agent-mcp==0.10.11
```

Its run command uses typed bindings for model name, loopback base URL,
placeholder API key, instruction file, workspace, managed home, results, and
trajectory output. Fast-Agent is recipe data; neither the Workbench compiler
nor the command-agent plugin branches on its name.

## FX starter

The FX starter installs the pinned `v0.0.6` Linux release beneath the managed
agent home using Python's standard library, then verifies `fx --version`. Its
run command uses `fx ask --yolo --json`, the locked Chat Completions route, the
task instruction file, and a declared `/logs/agent/fx-results.json` output. FX
is recipe data and uses the same generic command-agent path as Fast-Agent.

FX v0.0.6 selects a process model with `FX_MODEL` and reads a Vercel AI Gateway
credential from `AI_GATEWAY_API_KEY`. The Workbench binds those names to the
locked model and the non-secret model-key placeholder; users do not paste an
API key into the recipe. FX v0.0.6 does not expose a documented custom Gateway
base-URL override, so this starter currently verifies setup only. Benchmark
handoff remains disabled until the inference bridge has reviewed FX v0.0.6
route compatibility.

## Benchmark handoff requirements

Benchmark continuation is deliberately disabled until all of the following are
implemented:

1. Finalize the exact setup-tested compiler output as an immutable, unpromoted,
   actor-owned harness profile in the canonical private Bucket.
2. Extend normal Run submission and profile resolution to accept that exact
   harness profile digest.
3. Force unpromoted customer profiles to diagnostic launch policies and
   diagnostic-only publication.
4. Select deployment compatibility through generic harness capabilities.
5. Use a reviewed, digest-pinned worker image and revision that contain the
   command-agent plugin.
6. Prove the compiled nested agent configuration survives pinned Harbor
   preparation unchanged.

There must not be a Workbench-specific Run endpoint, worker, reconciler,
resource, or benchmark/model/harness-name branch. Once implemented, the
Workbench should finalize the passed setup test and open the existing Run
launcher with only the non-secret immutable harness-profile handle.
