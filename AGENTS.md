# Repository Instructions

## Public repository privacy

- Treat operator identity and private deployment topology as confidential.
- Do not publish real account names, organization names, emails, usernames,
  home paths, Space or Bucket names, Endpoint IDs, credential display names,
  private URLs, or local aliases.
- Apply this rule to source, tests, fixtures, generated files, logs,
  screenshots, commit messages, branch names, issues, pull requests, review
  comments, and release notes.
- Use placeholders such as `<namespace>`, `<control-space>`,
  `<artifact-bucket>`, `<endpoint-id>`, and `<service-token>`.
- Public availability elsewhere does not grant permission to repeat an
  identifier here. Publishing any operator-specific value requires explicit
  approval for that exact value and destination.
- Before every public commit, push, issue, pull request, review comment, or
  release, run `uv run python scripts/check_public_privacy.py .` and inspect
  the complete diff and public metadata.
- If private information is published accidentally, stop, report exactly what
  was exposed and where, remove it from the current version, and ask before
  rewriting public history or rotating credentials.

## Harbor-first run architecture

- Harbor owns benchmark resolution, task environments, agents, verification,
  trajectories, locks, and native trial results. Harbor-HF owns profile
  composition, Hugging Face lifecycle, Run and physical-attempt identity,
  admission, immutable evidence, cleanup, and publication.
- Use the pinned Harbor revision in a credential-free preparation Job and
  store its exact `JobLock` and prepared trials. Execution, retry, and recovery
  must reuse those records and must not resolve the benchmark again.
- Keep core control and worker code independent of benchmark, model, and
  harness names. These names are immutable data, not dispatch branches.
- A new benchmark or compatible model must need profile changes only. A new
  harness belongs in a Harbor agent plugin behind the common agent interface
  and must not add core API, schema, infrastructure, or worker branches.
- If support requires a missing behavior, add a general capability at the
  Harbor, agent, model, or Hugging Face adapter boundary, or reject the
  combination as unsupported.
- Use only public Harbor APIs. Never monkeypatch Harbor internals.

## Direct inference contract

- For inference-backed Jobs, compose one immutable Harbor `AgentConfig` from
  model, harness, and deployment profiles.
- The model profile is the only model-identity source. The deployment provides
  the HF inference upstream and native API; the harness provides its import
  path and capabilities.
- Require exact compatibility among the Harbor model provider suffix, the
  deployment provider, the model API list, and the harness API list.
- Supply the upstream URL, `${HF_INFERENCE_TOKEN}`, timeout, and output-token
  limit through `AgentConfig.env`, and add the upstream hostname to
  `extra_allowed_hosts`.
- The selected reviewed agent calls the upstream directly. Do not add an
  intermediate transport layer, request or response translation, alternate
  model binding, or fallback route.
- `HF_TOKEN` is the fine-grained control credential and must never leave the
  control Space. `HF_INFERENCE_TOKEN` may enter only an execution Job whose
  immutable deployment resolves an inference upstream.
- Preparation and no-inference Jobs receive no inference credential. Jobs
  never receive a writable canonical Bucket mount.
- Direct inference makes the reviewed agent an intended credential consumer.
  Arbitrary user-authored recipes remain setup-only unless their exact
  compiled form is promoted as a reviewed immutable harness.

## Durable control and evidence

- The steady-state inventory is one protected control Space and one private
  `<artifact-bucket>` Bucket.
- Do not create a per-Run repository, Bucket, Space, Dataset, schedule, lease
  store, status store, backup store, or result service.
- The TypeScript control service is the only shared Run authority. Python
  workers may upload assigned evidence and attempt receipts but must not become
  a second reconciler or shared control path.
- Versioned JSON Schema is authoritative for durable Bucket records. Generate
  TypeScript types and the browser API client; do not maintain handwritten
  copies of portable contracts.
- SQLite is disposable. Replaying immutable Bucket records must reconstruct
  the same projection and next action.
- Workers use short-lived signed capabilities scoped to a Run, launch action,
  task set, operation set, and expiration.
- Required evidence is profile-driven and includes the Harbor result and
  prepared lock plus required workspace, session, trajectory, verifier, and
  provenance records.
- Retry only typed replacement-eligible infrastructure failures. Model
  refusals, benchmark timeouts, agent outcomes, verifier outcomes, and valid
  zero scores are semantic and terminal.
- Endpoint cleanup has priority over new work. A Run cannot complete while an
  owned Endpoint lacks a durable paused, zero-ready-replica observation.

## Development

- Before implementation or external mutation, read
  `.agents/skills/project-authorization/SKILL.md` and verify the request against
  the repository-indexed project file.
- Before planning, launching, monitoring, recovering, verifying, or publishing
  a Harbor-HF Run, read `.agents/skills/harbor-hf/SKILL.md`.
- Use Python 3.12+, uv, Pydantic, Typer, Ruff, ty, and pytest for Python.
- Use Node.js `>=22.12.0`, npm workspaces, the root lockfile, Fastify, React,
  Vite, Tailwind CSS, shadcn/ui, strict TypeScript, Biome, Vitest, and
  Playwright for the control service and web application.
- Keep domain planning separate from Hugging Face, Harbor, filesystem, clock,
  and process-state adapters.
- Validate untrusted Hugging Face data at adapter boundaries and avoid `Any`.
- Never load models or run inference locally.
- Remote integration tests require explicit authorization and must leave every
  managed Endpoint paused.
- Never pass a locally configured personal or broad account credential to a
  Job, task environment, model service, or other remote runtime.
- Never write credential values to manifests, logs, tests, locks, evidence, or
  repository content.
- Add tests for every behavior change and preserve at least 85% Python
  coverage where that gate applies.
- Run relevant Python checks:

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run ty check
  uv run pytest --cov=src/harbor_hf --cov-fail-under=85
  ```

- Run the root npm format, lint, type, test, build, generated-file, dependency,
  and browser checks for TypeScript or web changes.
- Run `uv run slophammer-py check . --baseline` after project-structure or CI
  changes and `uv run slophammer-py dry .` for behavior changes.
- Mutation testing is intentionally unsupported. Do not add mutation tooling,
  workflows, release gates, or configuration.
- Use Conventional Commits and follow the repository's Slophammer standards.
- Do not modify `docs/agent-workbench.md` unless the task explicitly assigns
  that document.
