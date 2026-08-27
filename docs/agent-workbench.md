# Agent Workbench

Agent Workbench is an authenticated control-web page for authoring and privately
testing a generic command-line Harbor agent recipe. It is available at
`/workbench`.

The Workbench is intentionally low ceremony:

1. Start from the Fast-Agent 0.10.11 recipe or edit arbitrary setup and run
   commands.
2. Bind environment variables to typed runtime values instead of pasting
   credentials.
3. Review the server-generated command and environment preview.
4. Confirm and run setup in a disposable local Docker container.
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

## Local setup runner

The control API supports:

```text
POST /api/v1/workbench/preview
POST /api/v1/workbench/setup-tests
GET  /api/v1/workbench/setup-tests/{setup_test_id}
GET  /api/v1/workbench/setup-tests/{setup_test_id}/logs
GET  /api/v1/workbench/setup-tests/{setup_test_id}/files/{file_id}
```

Setup execution is controlled by:

| Variable | Values | Behavior |
| --- | --- | --- |
| `HARBOR_HF_WORKBENCH_RUNNER` | `disabled`, `docker` | Development defaults to `docker`; production and tests default to `disabled`. |
| `HARBOR_HF_WORKBENCH_IMAGE` | Docker image reference | Defaults to `python:3.12-slim` in the current local implementation. |

The Docker runner:

- uses an unprivileged UID/GID;
- drops all Linux capabilities and sets `no-new-privileges`;
- applies CPU, memory, PID, and timeout limits;
- keeps managed agent-home files outside the browsable workspace;
- bounds retained stdout, stderr, file count, and text previews;
- refuses symlink and special-file previews; and
- scopes in-memory setup state to the authenticated actor.

The runner is a local development facility. Setup-test state and files are
ephemeral and are deleted when the API process stops. Production-shaped setup
testing must use the existing durable control records, reconciler, and reviewed
Hugging Face Job path rather than executing customer commands on the control
host.

## Fast-Agent starter

The starter installs exactly:

```text
fast-agent-mcp==0.10.11
```

Its run command uses typed bindings for model name, loopback base URL,
placeholder API key, instruction file, workspace, managed home, results, and
trajectory output. Fast-Agent is recipe data; neither the Workbench compiler
nor the command-agent plugin branches on its name.

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
