---
title: Add task-attempt checkpoint and resume
author: Harbor-HF maintainers
date: 2026-09-01
tags: [control, checkpoints, recovery, workers]
---

# Add task-attempt checkpoint and resume

## In short

An infrastructure failure can currently force an unfinished benchmark task to
start over. The planned change will save the task after each complete model
response or tool action, so another Job can continue from that point.

The resumed Job will keep the same task attempt and its original token, cost,
time and deadline limits. Harbor-HF will resume only from checkpoint files that
were uploaded to the existing private Bucket and verified.

**Status.** Approved implementation plan. The feature is not implemented yet.
This document records the selected design and its verification gates. The
current work is documentation-only.

Harbor-HF currently preserves completed task receipts, but an interrupted
physical Job can lose all unfinished agent work. Replacement then starts the
task from the beginning. This can waste valid model responses, tool actions,
time and provider cost after an infrastructure failure.

The selected design adds application-level checkpoint and resume for each
logical task attempt. A physical Job parks at the last complete agent boundary.
A later Job restores that checkpoint and continues the same logical attempt
without repeating completed work or consuming another benchmark trial.

The [control service specification](CONTROL_SERVICE.md#cooperative-pause-and-resume)
defines the durable state transitions. [Architecture](architecture.md#task-attempt-checkpoints)
defines logical attempts, physical executions and checkpoint storage. The
[Harbor compatibility contract](harbor-integration-contract.md#execution-input)
defines the worker and agent boundary.

## Goal

Make infrastructure interruption recoverable without discarding completed agent
work. Keep every resumed Job under the original task, benchmark and budget
contracts. Preserve enough immutable evidence to audit the complete result.

## Safe checkpoint boundary

Create an application checkpoint after each completed model response or tool
action. Save all state needed to continue the attempt:

- task workspace changes
- the full agent conversation
- completed tool results and pending actions
- logs and trajectory data
- used and remaining tokens
- observed cost
- elapsed time and remaining deadline
- exact benchmark, model, harness, worker and task revisions

A checkpoint must describe one complete application boundary. Do not save a
partial model response, a request that is still streaming or a live process. If
a Job stops during a provider response, resume from the last committed
pre-request or post-response boundary.

Application checkpoints replace neither Harbor results nor task receipts. A
checkpoint keeps unfinished work recoverable. A valid receipt remains the final
selection unit for a completed logical task.

## Durable parking

Store checkpoint bytes in the existing private artifact Bucket. Store an
immutable manifest that binds:

- the logical attempt and physical Job
- the checkpoint format version
- every checkpoint object and checksum
- the complete provenance and cumulative budget state
- the worker generation that wrote the checkpoint

Mark an attempt `parked` only after the worker uploads the checkpoint and
manifest, reads them back, verifies every checksum, and the control service
writes an immutable record that points to that exact manifest. An upload or
verification failure must not create a parked or resumable state. Keep the
attempt running when that is safe. Otherwise, record an infrastructure failure.

Use deterministic identities for checkpoint, park and resume records. A repeated
request must adopt matching bytes and state. It must reject a conflicting
manifest or object.

## Resume

A resume creates a new physical Job for the same logical task attempt. Before
agent work starts, the Job must:

1. Fetch the exact immutable checkpoint manifest.
2. Verify each object and checksum.
3. Verify that the worker supports the checkpoint format.
4. Verify the task, benchmark, model, harness and worker provenance.
5. Restore the workspace, conversation, tool state, logs and trajectory.
6. Restore the cumulative budget and deadline state.

The resumed Job continues after the saved boundary. It does not repeat completed
agent work. It does not consume another benchmark trial. It does not reset any
token, cost, elapsed-time or deadline limit.

Reject a missing, corrupt, conflicting or incompatible checkpoint before work
starts. Do not fall back to a fresh task execution under the same logical
attempt. An operator can choose a separate bounded replacement attempt only
through the existing control policy.

## Worker repair handoff

A reviewed worker repair may resume a parked attempt only when the new worker
explicitly supports and validates the checkpoint format. The control record must
retain:

- every physical Job
- every worker generation
- every repair generation
- the exact checkpoint used for each handoff
- cumulative token use, cost and elapsed time
- the outcome and evidence for each physical execution

Keep valid completed-task receipts. After a repair, schedule only unresolved
tasks. If the same deterministic shared failure repeats, pause the affected
fleet before another retry.

## Publication and audit

A logical result can span several physical Jobs. Final publication must retain
the complete Job, worker, checkpoint, usage, cost and repair history. It must
still select exactly one valid receipt for each logical task. Failed, parked and
resumed physical executions remain immutable provenance and are not collapsed
or deleted.

Publication must prove that cumulative limits were continuous across every
resume. It must also prove that the final selected receipt came from the locked
task, model, harness, benchmark and worker chain.

## Implementation plan

### Portable checkpoint contracts

Update `packages/contracts` with versioned checkpoint manifest, park, resume and
worker-compatibility records. Update generated JSON Schema, TypeScript types and
OpenAPI output. Keep benchmark, model and harness names as data. Do not add a
second control API or storage service.

The contracts must bind checkpoint content, logical attempt, physical Job,
worker and repair generations, provenance, checksums and cumulative budgets.
They must reject unknown required fields, invalid digests and conflicting
identities.

### Control-service state transitions

Update `packages/control-core` so a running logical attempt can become parked
only after a verified checkpoint record exists. Add idempotent park and resume
actions. A resume must continue the same logical attempt and create a new
physical execution record.

Projection replay must derive the same state from duplicated or shuffled Bucket
listings. It must reject conflicting immutable bytes. Scheduling must keep valid
completed receipts and select only unresolved tasks after a pause or repair.

Update `apps/control-api` with the matching protected control routes and safe
read models. Keep the TypeScript service as the only shared control authority.
Do not add a Python reconciler or a direct browser-to-Bucket path.

### Worker checkpoint and restore support

Update the reviewed trial worker in `packages/harbor-hf-agents` so it can export
and restore application state at completed model-response and tool-action
boundaries. Keep checkpoint handling generic. Control and worker code must not
branch on benchmark, model or harness names.

The worker must package workspace changes, conversation, tool state, logs,
trajectory, provenance and cumulative budgets. It must upload through the
existing short-lived capability, verify the returned immutable objects, and
report the exact manifest to the control service.

Restore must validate the manifest and worker format before task work starts.
It must reconstruct the same prepared Harbor trial and continue from the saved
agent boundary. Do not use process-memory, virtual-machine or whole-container
snapshots.

### Cumulative budget

Carry token use, provider cost, elapsed time and the absolute deadline through
every checkpoint and resume. Admission must use the cumulative values before it
starts a replacement Job. A parked attempt must not gain more budget because a
new physical Job starts.

All physical Job costs remain observed costs for the run. Reservations remain
temporary exposure and are reconciled through the existing budget rules.

### Complete evidence

Extend private evidence and normalized internal records with physical Job,
worker, repair, checkpoint and handoff history. Keep existing completed task
receipts unchanged. Final publication must prove one valid selected receipt per
logical task and retain all physical provenance behind it.

Do not publish private checkpoint contents, full conversations, task workspaces
or operator-specific infrastructure. Public result rows may include only the
approved normalized provenance fields.

### Local failure checks

Add state-machine, contract and worker tests for:

- a checkpoint after several completed agent steps
- interruption during checkpoint upload
- missing or corrupt checkpoint bytes
- duplicate park and resume requests
- conflicting immutable manifests
- unsupported worker checkpoint formats
- a repaired worker handoff
- interruption during a provider response
- cumulative token, cost, elapsed-time and deadline continuity
- preserved completed task receipts
- scheduling only unresolved tasks
- a repeated shared defect that pauses the affected fleet
- final publication with all physical provenance

Inject process exits at every external action boundary. Replay Bucket records in
different orders and with duplicates. The selected result, next action and
budget state must remain the same.

### Remote pause and resume canary

Use the existing control Space and private artifact Bucket. Start one bounded
remote task and let it complete several agent steps. Request parking, verify the
checkpoint and terminal physical Job state, then resume in a new physical Job.

Prove that the resumed Job does not repeat completed model responses or tool
actions. Prove that cumulative budgets continue, the final receipt is valid,
and all physical Jobs and checkpoint handoffs remain visible. Also run bounded
failure canaries for an interrupted upload, corrupt object, duplicate resume and
incompatible worker format.

Paid canaries require the normal spending authorization and launch checks. A
successful local test does not authorize remote work.

## Required checks

Run all checks required by the final changed-file set. At minimum, run:

```sh
uv run python scripts/check_public_privacy.py .
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest --cov=src/harbor_hf --cov-fail-under=85
npm run format:check
npm run lint
npm run typecheck
npm test
npm run build
npm run check:generated
npm audit --audit-level=low
npx -y @simpledoc/simpledoc check
uv run slophammer-py dry .
uv run pip-audit
uv run slophammer-py check .
git diff --check
```

Run browser and remote integration checks when the changed surface requires
them. Run Pi Reviewer against the approved comparison base until no P0 or P1
finding remains.

## Rollout

Land the contracts and deterministic control transitions before enabling worker
parking. Publish a reviewed worker only after local checks pass. Keep parking
disabled for profiles whose worker does not declare checkpoint compatibility.

Start with one no-inference lifecycle test. Then run one bounded remote task
that parks after several complete agent steps and resumes in a new Job. Expand
only after the resume canary proves no repeated work, continuous budgets, valid
receipts and complete provenance.

The current whole-task replacement path remains available only for a separate
logical attempt under existing bounded retry policy. Do not silently restart a
parked logical attempt from the beginning.

## Failure handling

- Keep an attempt running when checkpoint creation fails and safe execution can
  continue.
- Record an infrastructure failure when safe continuation is not possible.
- Never label unverified checkpoint data as parked or resumable.
- Reject corrupt or incompatible state before agent work starts.
- Return to the last complete application boundary after interruption during a
  provider response.
- Pause the affected fleet when the same deterministic defect repeats.
- Keep all failed and partial physical execution records.
- Stop rollout if a resumed Job repeats completed work, resets a cumulative
  limit, loses provenance or changes benchmark trial count.

## Boundaries

This design uses the existing control Space and private artifact Bucket. It does
not add a repository, Space, Bucket, Dataset, Endpoint, service, credential,
model server or persistent resource.

It does not change benchmark tasks, models, providers, harnesses, prices,
context limits, output limits, verifier behavior, scoring, evidence selection or
publication meaning. It does not move shared control out of the TypeScript
service or patch Harbor internals.

This documentation task does not implement code, launch Jobs, spend money,
publish a worker, deploy, merge or release. Those actions require their normal
review, validation and authorization.
