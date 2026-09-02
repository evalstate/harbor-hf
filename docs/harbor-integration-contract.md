# Harbor Compatibility Contract

This document defines the active boundary between Harbor and Harbor-HF.
Historical evidence remains readable at its pinned revisions, but new
preparation, execution, retry, and recovery use this contract.

## Ownership boundary

Harbor owns:

- `JobConfig`, `JobPlan`, `JobLock`, and `TrialLock`;
- benchmark and task-source resolution;
- `AgentConfig` loading and agent execution;
- task environments and verifier execution;
- native sessions, trajectories, metrics, exceptions, and trial results; and
- the trial artifact inventory exposed by public Harbor APIs.

Harbor-HF owns:

- immutable profiles and their composition;
- Run, logical task, and physical attempt identity;
- Hugging Face Jobs and Endpoint lifecycle;
- admission, spend, cancellation, and replacement policy;
- evidence upload, verification, retention, and selection;
- result normalization and publication; and
- the shared TypeScript control authority.

Harbor-HF installs Harbor from a pinned `harbor-framework/harbor` commit and
uses only public APIs. It does not patch Harbor internals or add
benchmark-specific readers.

## Resolved execution input

Before admission, the control service composes the selected benchmark, model,
harness, deployment, and launch policy into one immutable execution contract.

The contract contains:

- the exact Harbor and agent-package revisions;
- a complete Harbor `AgentConfig`;
- the canonical Harbor model route;
- provider-facing model identity;
- inference upstream and API when required;
- context, output, timeout, and price settings;
- worker image, command, hardware, and source provenance;
- task-image safety limits; and
- physical-attempt, evidence, and publication policy.

The model profile is the only source of model identity. The harness profile
contributes a model-independent agent template. The deployment contributes
execution and inference routing. The resolver rejects incompatible
combinations before creating a Job.

## Preparation

A credential-free preparation Job consumes the resolved contract and invokes
Harbor's public planning API. Harbor resolves the benchmark and produces its
normal job lock.

The worker stores:

- one immutable prepared trial for every logical task;
- one prepared job record binding their order; and
- a digest of the reconstructed Harbor `JobLock`.

Each prepared trial includes the exact `TrialLock`, task-source digest,
task-image digest, resources, phase limits, and resolved `AgentConfig`.

Historical locks that lack the current resolved contract remain immutable and
readable. They cannot authorize new work.

## Execution

An execution Job receives exactly one prepared trial. It reconstructs a
one-attempt Harbor `JobConfig`; it does not select tasks or read a mutable
benchmark manifest.

After Harbor exits, the worker accepts the trial only when:

- Harbor wrote a durable native result;
- the result contains no unhandled trial exception for a successful outcome;
- the emitted lock exactly matches the prepared lock;
- agent, model, API, source, image, and verifier identities match;
- required evidence is present and digest-valid; and
- secret scanning succeeds.

Harbor's internal retry count is zero. Harbor-HF owns physical replacement
attempts so every execution has an explicit identity and receipt.

## Direct inference through `AgentConfig`

All inference-backed agents are loaded through Harbor's public
`AgentConfig.import_path`. The resolved `AgentConfig` supplies:

```json
{
  "model_name": "openai/<model-id>:<inference-provider>",
  "env": {
    "OPENAI_API_KEY": "${HF_INFERENCE_TOKEN}",
    "OPENAI_BASE_URL": "<approved-upstream>",
    "HARBOR_HF_MAX_OUTPUT_TOKENS": "<locked-limit>",
    "HARBOR_HF_PROVIDER_TIMEOUT_SECONDS": "<locked-timeout>"
  },
  "extra_allowed_hosts": ["<upstream-host>"]
}
```

Harbor expands the credential reference in the execution Job. The selected
agent configures its native runtime from these values and calls the upstream
directly.

Compatibility is exact:

- the model profile lists supported inference APIs;
- the harness profile lists supported inference APIs;
- the deployment declares one API;
- the provider suffix in the Harbor model route matches the deployment; and
- the upstream host is allowlisted.

An unsupported combination is rejected or skipped in a matrix before Run
creation. Do not translate requests or responses, silently select another
model, change the upstream, or fall back to another harness.

## Harbor agent plugins

Each supported harness has a separate module in `harbor-hf-agents` and an exact
profile revision. Shared support is limited to neutral concerns such as:

- direct environment resolution;
- isolated-user command execution;
- task-process cleanup before verification;
- session discovery and redaction;
- ATIF conversion;
- timeout and output-limit validation; and
- failure classification.

Agent-specific code may generate the native tool's configuration file or
environment aliases. It must preserve the resolved model, API, and upstream.
Package-backed tools use exact package versions; Git-backed tools use full
commits.

The registry validates import paths, revisions, API capabilities, permitted
arguments, session requirements, trace formats, and retry taxonomy. Generic
worker and evidence code must not contain agent-name branches.

## Compatibility export

The worker exports a versioned compatibility bundle from Harbor's native
objects. It preserves:

- Run, logical task, and physical attempt identity;
- Harbor job and trial locks;
- agent and model identity;
- task source and image identity;
- native reward, metrics, timing, and exception data;
- session and trajectory references;
- verifier records;
- worker and package provenance; and
- evidence checksums.

The exporter validates rather than repairs Harbor output. Missing required
fields, malformed sessions, inconsistent paths, non-finite metrics, lock drift,
or identity drift fail closed.

## Evidence contract

Required evidence is profile-driven. A complete attempt commonly contains:

- Harbor lock and result;
- compatibility bundle;
- frozen post-agent workspace and file index;
- native session and ATIF trajectory when required;
- verifier output;
- source, image, and worker provenance;
- worker logs and infrastructure observations;
- secret-scan result;
- checksum manifest; and
- terminal receipt.

Cost is derived from accepted Harbor result data and immutable pricing where
available; unknown usage stays unknown rather than invalidating an otherwise
valid semantic result.

## Retry and failure semantics

Harbor-HF distinguishes:

- semantic model or benchmark outcomes;
- agent setup or runtime outcomes;
- verifier outcomes;
- replacement-eligible infrastructure failures; and
- deterministic shared infrastructure defects.

Only the replacement-eligible infrastructure class can receive another
physical attempt, within the locked limit and Run ceiling. A replacement uses
the same prepared trial. Behavior-affecting changes require a new linked Run.

## Historical reader

Historical evidence is read only by an isolated compatibility path pinned to
the revision that wrote it. Historical records are never rewritten to match
this contract and cannot select a retired writer for new work.

## Verification baseline

Local validation covers:

- import-path and revision validation;
- exact profile composition;
- model provider-suffix and API compatibility;
- direct environment expansion;
- upstream host allowlisting;
- absence of inference secrets from preparation and no-inference Jobs;
- task-user isolation and process cleanup;
- lock and identity drift;
- session redaction and ATIF conversion;
- evidence and checksum failures;
- retry classification; and
- deterministic behavior through every fail-closed branch.

Remote canaries, when separately authorized, must retain complete Harbor
results, workspace evidence, sessions or trajectories, verifier evidence,
provenance, and cleanup observations. A canary does not authorize a full Run,
deployment, publication, or additional remote mutation.
