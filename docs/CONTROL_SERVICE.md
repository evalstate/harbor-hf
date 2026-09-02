# Harbor-HF Control Service

The Harbor-HF control service is the single shared authority for hosted Harbor
Runs on Hugging Face infrastructure. It runs in one application-protected
Docker Space, stores immutable records in one private Bucket, and uses two
persistent Space secrets: `HF_TOKEN` for control operations and
`HF_INFERENCE_TOKEN` for inference-backed execution Jobs.

This specification describes the current Harbor-first direct-inference design.

## Runtime inventory

The steady-state service has exactly two persistent resources:

1. one protected control Space; and
2. one private `<artifact-bucket>` Bucket.

The Space contains one Node.js process running:

- Fastify API routes;
- the background reconciler;
- a disposable SQLite projection;
- Server-Sent Events;
- the compiled React application; and
- health, readiness, and operational views.

The Bucket contains immutable control records, profiles, prepared Harbor locks,
evidence chunks and manifests, attempt receipts, normalized results,
publication receipts, and catalogs. SQLite is never authoritative. Rebuilding
it from Bucket objects must produce the same state and next action.

Do not create a per-Run repository, Bucket, Space, Dataset, schedule, lease
store, result service, or backup service. A new persistent resource requires an
explicit access or failure-domain reason and operator approval.

## Process and trust model

The control process is the only shared writer of Run decisions. API mutations
record immutable intent before returning. The reconciler reserves one
deterministic action, performs the external side effect, and records an
immutable receipt before advancing.

Python remains in pinned preparation and execution Jobs. Those workers call
Harbor and return scoped evidence, but they do not become another control
service.

Credential boundaries are:

- `HF_TOKEN` remains in the control Space and is used for Bucket and HF
  lifecycle operations.
- `HF_INFERENCE_TOKEN` may be attached only to an execution Job whose resolved
  deployment contains `inference_upstream`.
- Preparation Jobs receive neither persistent credential.
- Every worker receives a short-lived signed capability scoped to its Run,
  action, task set, operations, and expiration.
- No Job receives a writable mount of the canonical Bucket.

For direct inference, the execution contract places
`${HF_INFERENCE_TOKEN}` in Harbor `AgentConfig.env` alongside the locked
upstream URL. Harbor expands that value for the selected agent. This means the
agent is an intended secret consumer; arbitrary user-supplied agent code and
unreviewed recipes cannot use this path.

## Source layout

The control-service implementation is split by responsibility:

```text
apps/
  control-api/       Fastify process and composition root
  control-web/       React application
packages/
  contracts/         JSON Schema, generated TypeScript, API contracts
  control-core/      domain records, projection, reconciler, policy
  bucket-store/      immutable Bucket adapter
  hf-adapters/       Hugging Face lifecycle adapter
  harbor-hf-agents/  pinned Harbor workers and agent plugins
deploy/
  control-space/     Docker Space image
  trial-worker/      reviewed HF Job image
profiles/            checked-in immutable profile sources
```

Domain code does not import Fastify, React, the filesystem, Hugging Face
clients, or wall-clock implementations. Untrusted HF responses are validated
at adapter boundaries.

## Run preparation

A Run submission names promoted profile aliases for:

- benchmark;
- model;
- harness;
- deployment; and
- launch policy.

The service resolves each alias to an immutable profile record and composes one
execution contract before cost reservation or Job admission. It rejects:

- unknown or non-promoted profiles;
- imported historical profiles;
- incompatible model, harness, deployment, or API combinations;
- malformed Harbor model routes;
- mutable image or source references;
- unsupported worker commands or hardware;
- missing prices or runtime limits; and
- any combination outside the caller's authorization.

A preparation Job receives the resolved contract without persistent secrets.
It installs the pinned Harbor and agent-package revisions, creates a normal
Harbor `JobConfig`, and asks Harbor's public `JobPlan` API to resolve the
benchmark. It writes:

- one `prepared.trial` record per logical task;
- one `prepared.job` record binding the ordered trials; and
- the SHA-256 digest of the reconstructed Harbor `JobLock`.

Each prepared trial binds the exact Harbor `TrialLock`, task source digest,
task-image digest, resource request, phase limits, agent configuration, and
worker provenance. Execution and recovery never resolve the benchmark source
again.

## Profile composition

### Benchmark profile

The benchmark profile identifies the source, revision, task selection, and
source-integrity policy. Harbor remains the only component that interprets the
benchmark format.

### Model profile

The model profile supplies:

- canonical Harbor model name, such as
  `openai/<model-id>:<inference-provider>`;
- model revision when observable;
- supported inference APIs;
- context and output limits; and
- model behavior settings.

### Harness profile

The harness profile supplies:

- Harbor agent `import_path`;
- exact agent revision;
- model-independent agent keyword arguments;
- supported inference APIs;
- required evidence types; and
- session or trajectory policy.

Adding a harness must require only a Harbor agent plugin and profile. Control,
API, schema, and infrastructure code must not branch on the harness name.

### Deployment profile

The deployment profile supplies:

- `route`;
- digest-pinned worker image and reviewed commands;
- hardware and phase timeouts;
- supported model and harness profile names;
- prices and admission settings;
- direct `inference_upstream`;
- `inference_api`;
- `inference_timeout_seconds`;
- `inference_max_output_tokens`; and
- endpoint configuration when the route uses a managed Endpoint.

The deployment profile does not provide a second model identity. The resolver
derives the provider-facing model from the canonical Harbor model route and
verifies that its suffix matches `inference_provider`.

### Resolved inference contract

When `inference_upstream` is present, composition produces:

- Harbor provider `openai`;
- the selected HF inference provider;
- the exact upstream URL;
- the canonical Harbor agent model;
- the provider-facing model;
- either `chat-completions` or `responses`;
- timeout, context, and output-token limits;
- immutable token prices; and
- the upstream hostname in `extra_allowed_hosts`.

The final `AgentConfig` includes:

```json
{
  "model_name": "openai/<model-id>:<inference-provider>",
  "env": {
    "OPENAI_API_KEY": "${HF_INFERENCE_TOKEN}",
    "OPENAI_BASE_URL": "https://router.huggingface.co/v1",
    "HARBOR_HF_MAX_OUTPUT_TOKENS": "<locked-positive-integer>",
    "HARBOR_HF_PROVIDER_TIMEOUT_SECONDS": "<locked-positive-integer>"
  },
  "extra_allowed_hosts": ["router.huggingface.co"]
}
```

Agent plugins may derive runtime-specific environment names or configuration
files from these values. They may not select another model, upstream, API, or
credential. The service does not translate one inference API into another.

## Agent Workbench

The authenticated Agent Workbench compiles and tests generic command-agent
recipes through the versioned recipe schema and server-authoritative preview
compiler. Its setup state is actor-scoped and ephemeral.

Local development defaults to a disposable Docker runner. Hosted setups use a
reviewed setup-only Job. An edited recipe cannot launch benchmark work. It may
continue to the Run launcher only when its exact compiled form matches a
reviewed immutable harness profile and compatible deployment.

The detailed Workbench document is maintained separately in
[`agent-workbench.md`](agent-workbench.md).

## Durable contract authority

Versioned JSON Schema under `packages/contracts/schemas/` is authoritative for
Bucket records. TypeScript types and browser clients are generated from the
schema. Portable contracts must not have handwritten duplicate definitions.

Immutable records include:

- actor and request identity;
- Run specification and lock;
- profile records and promotions;
- preparation and prepared-trial records;
- admission, action intent, dispatch, observation, and receipt records;
- evidence manifests and selected attempt receipts;
- endpoint ownership and cleanup observations;
- retry, cancellation, and seal decisions;
- normalized results and publication receipts; and
- audit and migration records.

Records use canonical JSON, stable IDs, explicit schema versions, UTC
timestamps, and SHA-256 digests. Writing different bytes to an existing key is
an integrity failure.

## HTTP API

The API uses `/api/v1`. Exact request and response shapes come from generated
OpenAPI contracts. The major route groups are:

- system health, readiness, status, and events;
- profiles, promotions, and compatibility;
- Runs, preparation, retries, cancellation, and sealing;
- Jobs, Endpoints, capacity, and audit;
- worker preparation, evidence upload, and attempt completion;
- Workbench recipe preview and setup;
- private results and publications; and
- the approved public leaderboard snapshot.

Mutations require:

- an authenticated actor;
- authorization for the operation;
- CSRF protection for browser sessions;
- a request confirmation where the action can spend or mutate remote state;
- an idempotency key; and
- immutable intent before any external call.

API errors are typed and must not expose credentials, private object keys,
private resource identifiers, or raw upstream response bodies.

## Authentication and authorization

Browser users authenticate with Hugging Face OAuth. The callback validates
state, nonce, redirect target, issuer, audience, and token timing. Browser
mutations require same-origin checks and CSRF tokens.

Automation uses a purpose-scoped bearer accepted by the control service.
Workers use signed capabilities, not the operator bearer. Capability validation
checks signature, issuer, audience, Run, action, task set, operation set,
expiration, and current durable action state.

Anonymous access is limited to static assets, login and callback handling,
health surfaces intended for hosting, and the approved public leaderboard
snapshot. Forwarded client-address headers do not establish identity.

The service redacts `HF_TOKEN`, `HF_INFERENCE_TOKEN`, OAuth tokens, worker
capabilities, cookies, authorization headers, private routes, and configured
credential patterns from logs and browser responses.

## Projection and replay

SQLite accelerates queries but can be deleted at any time. Startup:

1. acquires the single-writer service lock;
2. validates the immutable-store migration and operator ACL;
3. lists records from stable prefixes;
4. validates schemas, digests, references, and ordering;
5. rejects conflicting immutable bytes;
6. rebuilds projections in one transaction; and
7. starts reconciliation only after replay succeeds.

Listing order, duplication, and pagination must not change the projection.
Incomplete action sequences remain visible and recoverable.

## Admission and reconciliation

Admission uses immutable capacity profiles and Run ceilings. It keeps separate
limits for:

- active HF Jobs;
- Job starts over time;
- deployment or endpoint ownership;
- per-Run concurrency;
- estimated and observed spend; and
- cleanup work.

The reconciler always prefers safety and cleanup over new billable work. A
typical progression is:

1. prepare the Run;
2. validate the prepared Harbor lock;
3. reserve an execution action;
4. launch or adopt the deterministic HF Job;
5. observe Job state and worker receipts;
6. accept or reject the evidence manifest;
7. seal the logical task or admit an eligible replacement;
8. pause and verify any owned Endpoint;
9. normalize results; and
10. publish approved outputs.

An ambiguous external response never authorizes a duplicate side effect.
Reconciliation first observes the deterministic remote identity. Cancellation
stops new admission but does not skip evidence finalization or Endpoint cleanup.

## Trial execution and evidence

An execution Job receives exactly one prepared trial. The reviewed worker:

1. validates its capability and task assignment;
2. verifies and unpacks the locked task image;
3. launches the task as a dedicated unprivileged host UID;
4. loads the selected agent through Harbor's public API;
5. supplies the direct inference environment when required;
6. waits for agent descendants to stop;
7. freezes the post-agent workspace;
8. runs Harbor verification against that frozen state;
9. compares Harbor's emitted lock with the prepared lock;
10. uploads content-addressed evidence; and
11. posts the attempt receipt.

Required evidence is profile-driven. Typical evidence includes the Harbor lock
and result, workspace archive and index, native session, ATIF trajectory,
verifier output, worker logs, image and source provenance, evidence checksums,
and infrastructure observations.

The control service verifies manifest structure, every chunk digest, declared
media types, parent references, Run and task identity, selected attempt,
required evidence kinds, and secret-scan status before accepting the receipt.

## Completion, retry, and publication

A logical task becomes terminal when one accepted physical attempt produces:

- a valid semantic result;
- a terminal benchmark or agent outcome;
- a cancellation; or
- an infrastructure-exhausted result permitted by the launch policy.

Only typed replacement-eligible infrastructure failures can receive another
physical attempt, and only within the immutable physical-attempt limit and Run
ceiling. Model refusals, benchmark timeouts, verifier failures, and valid zero
scores are semantic outcomes and must not be retried as infrastructure.

A Run completes only when:

- every logical task is sealed;
- no action remains pending;
- all Job observations and receipts are consistent;
- every owned Endpoint is observed paused with zero ready replicas;
- accepted spend is within the ceiling;
- normalized results and publication receipts are durable; and
- a clean replay produces the same projection.

Publication is a separate deterministic action. A publication failure does not
reopen benchmark work. Public outputs contain only approved normalized fields
and traces; raw workspaces, sessions, credentials, capabilities, and private
topology remain private.

## Container build and deployment

The control Space uses a pinned multi-stage Docker build:

1. install locked npm workspaces;
2. generate and verify contracts;
3. type-check and build API and web assets;
4. copy production dependencies and required schemas into the runtime stage;
5. run as a non-root user on port `7860`.

Secret values are available only at runtime and are never mounted into build
steps. Deployment records the source commit, lockfile digest, base image
digests, and resulting Space revision.

The public repository is source of truth. Operators deploy an exact reviewed
revision; they do not edit the Space repository by hand. Production must use
hardware that remains available while Jobs or Endpoint cleanup may require
reconciliation. Exact price and monthly ceiling require review before a
hardware change.

## Observability and testing

Every API request has a request ID and every side effect has a deterministic
action ID. Structured logs redact secrets and private routing data. Operator
views expose service revision, projection status, replay cursor, reconciler
heartbeat, last successful Bucket write, dependency health, capacity, owned
resources, and limiting reasons.

Tests cover:

- generated schema and API synchronization;
- deterministic profile composition;
- model, provider suffix, and API compatibility;
- direct inference environment expansion and host allowlisting;
- absence of inference credentials from preparation and no-inference Jobs;
- immutable replay under shuffled and duplicated listings;
- process interruption around every external action;
- ambiguous Job creation and deterministic adoption;
- worker capability scope and expiration;
- evidence digest and secret-scan failures;
- retry classification and attempt limits;
- cancellation and Endpoint cleanup; and
- browser authentication, CSRF, and private-data rejection.

Use Biome, TypeScript, Vitest, fast-check, Testing Library, MSW, Playwright,
Ruff, ty, pytest, dependency checks, container builds, and the public privacy
scan as applicable. Mutation testing is intentionally unsupported.

## Boundaries

The control service does not:

- modify or monkeypatch Harbor;
- resolve benchmark formats outside Harbor;
- run benchmark agents in the Space;
- load or serve models;
- translate between inference APIs;
- infer unobserved provider hardware or model revision;
- keep durable state only in SQLite;
- expose the Bucket to browsers;
- create active-active control replicas;
- add benchmark-, model-, or harness-name branches to core code; or
- rewrite historical evidence.

## References

- [Architecture](architecture.md)
- [Harbor compatibility contract](harbor-integration-contract.md)
- [Harbor agent architecture](provider-agent-architecture.md)
- [Hosted operations cookbook](harbor-cookbook.md)
- [Trial evidence bundle](trial-evidence-bundle.md)
- [Hugging Face Docker Spaces](https://huggingface.co/docs/hub/spaces-sdks-docker)
- [Hugging Face Spaces OAuth](https://huggingface.co/docs/hub/spaces-oauth)
