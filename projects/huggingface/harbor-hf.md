---
schema_version: v1
slug: huggingface/harbor-hf
repository: https://github.com/huggingface/harbor-hf
default_branch: main
---

# Harbor-HF

## Current authorization

Status: approved
Approved at: 2026-08-17T06:48:55Z
Amended at: 2026-08-23T09:01:00Z
Inference-token amendment approved at: 2026-08-17T15:37:46Z
Sandbox-lifecycle amendment approved at: 2026-08-17T18:39:15Z
Finalization amendment approved at: 2026-08-18T00:25:01Z
Terminal-Bench 2.1 amendment approved at: 2026-08-18T10:40:26Z
Terminal-Bench 2.1 USD 300 campaign-ceiling amendment approved at: 2026-08-18T17:20:36Z
Canonical-Bucket amendment approved at: 2026-08-20T13:53:14Z
Canonical-Space replacement amendment approved at: 2026-08-20T14:01:49Z
Terminal-Bench 2.1 single-trial diagnostic amendment approved at: 2026-08-20T19:21:55Z
Production-writes amendment approved at: 2026-08-21T10:31:00Z
Leaderboard-snapshot amendment approved at: 2026-08-21T20:06:00Z
Harness-integration amendment approved at: 2026-08-21T23:01:07Z
Terminal-Bench 2.1 clean-rerun amendment approved at: 2026-08-22T07:33:41Z
Public-leaderboard amendment approved at: 2026-08-22T12:09:50Z
Diagnostic-recovery amendment approved at: 2026-08-23T04:30:39+08:00
Infrastructure-retry amendment approved at: 2026-08-22T21:19:00Z
Harness 89-task diagnostic amendment approved at: 2026-08-22T23:31:00Z
Harbor-from-source amendment approved at: 2026-08-23T07:20:00Z
FX harness amendment approved at: 2026-08-23T07:40:00Z
Harness full-run repair amendment approved at: 2026-08-23T08:21:00Z
Sandbox-parallelism amendment approved at: 2026-08-23T09:01:00Z

### Scope

- Add the project-authorization skill and this repository-indexed project file through the normal contribution workflow.
- Finish deployment and hard cutover of the hosted TypeScript control service described by the approved control-service plan.
- Install the retained purpose-scoped service credential as the control Space's `HF_TOKEN` control secret.
- Run the hosted no-inference recovery and cutover canaries, plus only bounded paid canaries required by the approved plan.
- Promote the verified historical migration and enable production writes only after every required gate passes.
- Audit legacy consumers and unique objects before proposing any resource retirement.
- Make the existing control Space publicly reachable only after adding and verifying application-layer protection for operator, browser, and worker routes.
- Admit workers with short-lived, signed, campaign-scoped control capabilities.
- Install a separate, regularly rotated, inference-only Hugging Face credential in the existing control Space and pass it only to reviewed benchmark workers as `HF_INFERENCE_TOKEN`.
- Extend signed worker capabilities to exact Hugging Face Sandbox lifecycle operations performed by the control Space for an immutable campaign task.
- Prepare and run the requested Terminal-Bench 3 campaign for the locked model in low adaptive-thinking mode after the bounded paid canary and launch-review gates pass.
- Prepare and run Terminal-Bench 2.1 at revision `d49e28f1e4ddd13d289e85a5f312a66750951932` with `deepseek-ai/DeepSeek-V4-Flash-0731` at revision `7872f01b1d1fe23eabc4c98b48bffcef5a386062`, the reviewed Pi 0.84.2 worker, and high reasoning. The current approved campaign is a single-trial diagnostic run with one trial for each of the 89 tasks. The official five-trial protocol is outside the current run and needs a separate later decision.
- Migrate the remaining active ShellBench result catalog, verify parity, replace the legacy results viewer, and perform the hard cutover without deleting legacy resources.
- Create one private canonical `<artifact-bucket>` in the selected namespace because no existing canonical Bucket is available, then deploy the exact reviewed control-service revision to the existing canonical `<control-space>`.
- Create one private canonical replacement `<control-space>` in the selected namespace because the previous Space no longer exists, then deploy the exact reviewed control-service revision with writes disabled.
- Enable production writes on the hosted control Space so operators can submit any promoted-profile campaign, not only the built-in control-smoke canary.
- Restart the failed no-inference control-smoke as an infrastructure replacement after protected public ingress.
- Add a leaderboard snapshot in the existing canonical `<artifact-bucket>`: a configuration digest, mechanical eligibility, and a derived SQLite file of the rows shown on the board. Keep one Space and one Bucket.
- Make the official leaderboard the Space default route and allow anonymous `GET /api/v1/leaderboard`. The current operator dashboard moves to `/overview` behind a "Run benchmark" button and Hugging Face login.
- Integrate the requested dashboard harnesses as Harbor agent plugins behind the existing campaign path: OpenCode, Qwen Code, mini-swe-agent, Pi, Kimi Code, Hermes, Codex, OpenHands, OpenClaw, and Claude Code. Prove each with one Terminal-Bench 2.1 two-task canary. Reject a harness that needs a native API the locked Hugging Face router route cannot preserve.
- Fix zero-token selection, fail-closed task exhaustion, campaign completion, publication commit safety, cooperative pause and resume, and append-only publication supersession. After the reviewed implementation is merged and deployed, run one fresh full 89-task Terminal-Bench 2.1 single-trial diagnostic campaign with worker concurrency eight to validate the rolling scheduler and produce a clean replacement publication.
- Finish the active diagnostic campaign. Fix and deploy terminal Job reservation settlement, recover only unresolved tasks through isolated one-task Jobs, publish the complete result, and append the required supersession record.
- Treat Harbor environment-setup failures as infrastructure, retry transient evidence-upload HTTP 500 responses, and keep an execution Job running after one task fails to upload evidence. Deploy that reviewed revision, then retry only eligible infrastructure failures on the existing gpt-oss OpenCode Terminal-Bench 2.1 single-trial campaign. Add a run-page control and CLI `--all-eligible` that call the existing per-task infrastructure retry path.
- After the gpt-oss OpenCode 89-task single-trial diagnostic exists, run the same Terminal-Bench 2.1 one-trial diagnostic for the other Chat Completions harnesses that already have a two-task canary: Qwen Code, mini-swe-agent, Pi, Kimi Code, Hermes, OpenHands, and OpenClaw. Use `openai/gpt-oss-20b` on Together, reasoning off, publication role diagnostic, and the existing promoted profiles. Do not add a campaign for OpenCode. Reject Codex and Claude Code on this route because they need a native API the locked Chat Completions router cannot preserve.
- Install Harbor from a pinned `harbor-framework/harbor` git commit instead of a PyPI release so new campaigns can evaluate harnesses as they land upstream. Remove the Harbor 0.21.0 empty-metrics sitecustomize workaround after that pin includes PR 2681. Deploy the reviewed revision. Existing campaign locks keep their Harbor pin.
- Add FX as a Harbor agent plugin and promoted harness plus gpt-oss Together deployment so it appears in the launch list. Deploy the reviewed revision. Do not launch a campaign.
- Finish one successful full Terminal-Bench 2.1 single-trial diagnostic for each existing gpt-oss Chat Completions 89-task run by inspecting that run and its Jobs, fixing the shared defects those Jobs expose, deploying the reviewed revision, and retrying only eligible infrastructure failures or unresolved tasks on those same campaigns. The existing FX 89-task row may be finished. Do not add a second 89-task campaign for a harness that already has one. Do not launch Codex or Claude Code.
- Make the namespace Sandbox cap an operator setting with default 16, then set the live service to 128 so the existing 89-task diagnostics can start more Sandboxes at once. Campaign ceilings stay unchanged. Existing campaign locks keep their per-run `max_sandboxes` and worker concurrency.

### Limits

- Deploy an exact merged source revision with writes disabled first.
- Use `cpu-upgrade` at USD 0.03 per active hour for the always-on control service.
- Keep total project spend within USD 300. This includes campaign, recovery, provider and endpoint costs plus the control service.
- For the next Terminal-Bench 2.1 production campaign, use the later explicitly approved USD 300 hard campaign ceiling. This campaign-specific amendment supersedes the preceding cumulative limit for that campaign only. Preserve and report all earlier spend separately.
- Do not create another persistent Space, Bucket, repository, Dataset, schedule, credential beyond the approved inference credential, lease store, status store, backup store, or result store.
- The 2026-08-20 amendment permits exactly one new private canonical `<artifact-bucket>` in the selected namespace. It does not permit another Space, Bucket, repository, Dataset, schedule, credential, or result store.
- The later 2026-08-20 replacement amendment permits exactly one new private canonical `<control-space>` in the selected namespace. It does not permit an additional Space or any other persistent resource.
- Do not rerun valid logical tasks or use inference during migration and publication recovery. The 2026-08-22 amendment permits one separate fresh 89-task diagnostic campaign after the validity fixes deploy; it does not reopen or retry the old campaign.
- Keep credential values, private resource identifiers, operator paths, and private topology out of Git and browsers. Do not expose credentials in logs or evidence; the approved inference credential may appear only in the trusted worker or root-owned inference bridge environment.
- Do not delete or retire a legacy resource without its completed private audit and a separate explicit approval for that resource.
- Anonymous callers may reach static application assets, login initiation, OAuth return handling, health checks, and `GET /api/v1/leaderboard`. That leaderboard response is the official snapshot only: ranked rows and Pareto flags, no `sqlite_key`, no diagnostic catalogs, and no campaign internals. Campaigns, results, system, events, Jobs, profiles, audit, and all mutations remain deny-by-default.
- Add bounded request-body and anonymous request-rate controls before changing Space visibility. If hosted denial, capability, or abuse-control verification fails, restore private visibility, disable writes, and stop.
- Keep exactly two operator-managed Space secrets: the control credential `HF_TOKEN` and the inference-only `HF_INFERENCE_TOKEN`.
- Workers must never receive `HF_TOKEN`. They may receive only `HF_INFERENCE_TOKEN`, whose permissions are limited to serverless and Endpoint inference calls.
- Pin each worker image and command, enforce the locked model, route, token, request, concurrency, timeout, and cost limits in the worker bridge, and rotate the inference credential regularly. Revoke the prior credential only after every Job using it is terminal.
- Bind every Sandbox operation to the immutable campaign lock, launch action, task, expiration, approved image, hardware, paths, transfer limits, timeouts, and budget. Record fenced lifecycle receipts and do not expose a general Hugging Face API proxy.
- Keep `HF_TOKEN` in the control Space. Never pass it to a worker or Sandbox. The control Space may derive and use a per-Sandbox credential only inside its trusted process while handling an authorized lifecycle operation.
- Keep the first Terminal-Bench canary below USD 5. Treat the full campaign as substantial paid compute: measure throughput and cost first, preserve durable partial evidence, prove pause and resume, and obtain explicit approval for the exact trial count, concurrency, hardware, and hard cost ceiling before launch.
- For the approved Terminal-Bench 2.1 campaign, use one bounded representative canary and then continue without another conversational prompt only when the hosted control plane admits the measured worst-case cost for 89 tasks and five trials under the existing USD 300 total project limit. Count setup, canaries, retries, recovery, and cleanup. Allow only infrastructure replacements; never rerun a terminal semantic outcome.
- Production writes admit any promoted-profile campaign through the existing control path. They do not raise the spend ceiling, add persistent resources, or authorize rerunning a terminal semantic outcome.
- The configuration digest hashes benchmark identity, model identity, harness identity, trial count, reasoning effort, inference provider, and Harbor version from the campaign lock. It excludes worker revision, Job IDs, and cost.
- Only `publication_role=final`, quality `clean`, fully scored campaigns enter the leaderboard snapshot. Diagnostic, cancelled, mixed, and policy-failed catalogs stay private candidate material.
- Store each snapshot as an immutable SQLite object under the existing results prefix. Do not create another Bucket, Dataset, Space, or result service. Anonymous `GET /api/v1/leaderboard` is allowed and rate-limited separately from other anonymous API traffic. Result detail and publication click-through stay authenticated.
- The harness-integration series uses `terminal-bench-2-1-canary`, `openai/gpt-oss-20b` on Together, reasoning off, and publication role diagnostic. Hard ceiling USD 80 for the whole series, including retries. This does not authorize the 89-task diagnostic or the official five-trial protocol.
- Keep real observed cost for the active diagnostic campaign at or below USD 100 during this recovery. Preserve its locked worker, model, benchmark, provider, hardware, task inputs, timeouts, concurrency, trial count, and attempt limit. Use no new persistent resource or credential.
- The 2026-08-22 infrastructure-retry amendment does not raise any campaign ceiling. Retries stay inside the locked ceiling of that existing campaign. Do not reopen `complete`, agent, verifier, policy, refusal, semantic, cancelled, or benchmark-timeout outcomes. Do not rerun a scored miss.
- The 2026-08-22 harness 89-task diagnostic amendment authorizes seven new campaigns. Each campaign uses the same hard ceiling as the existing gpt-oss OpenCode 89-task run: USD 10.60 (`10600000` micro-USD), which is twice the diagnostic reservation. Combined hard cap for those seven campaigns is USD 74.20, including infrastructure retries. This does not reopen the OpenCode 89-task campaign, does not authorize Codex or Claude Code, and does not authorize the official five-trial protocol.
- The 2026-08-23 Harbor-from-source amendment pins an exact Harbor git commit. It does not float on a branch, add a persistent resource or credential, relaunch a campaign, or raise any spend ceiling. `harbor_version` stays the version that commit reports so preparation admission still matches.
- The 2026-08-23 FX harness amendment does not authorize a canary, 89-task diagnostic, official five-trial run, new persistent resource, or credential. It only adds the harness to the existing campaign path and deploys the reviewed revision.
- The later 2026-08-23 harness full-run repair amendment does not raise any campaign ceiling and does not add a persistent resource or credential. Retries stay inside each existing campaign's locked ceiling. The seven-campaign combined cap remains USD 74.20. The existing FX 89-task row stays inside its locked ceiling. Do not reopen sealed semantic, agent, verifier, policy, refusal, cancelled, or benchmark-timeout outcomes. Do not launch a second 89-task campaign for a harness that already has one.
- The 2026-08-23 Sandbox-parallelism amendment raises only the shared namespace Sandbox cap from 16 to 128. It does not raise a campaign ceiling, add a persistent resource, or change a locked campaign. Sandbox hardware cost still counts against each campaign's existing ceiling.

### Remaining gates

No project-scope amendment remains pending. Operational gates still apply:

- Do not retire the legacy results viewer or stores until catalog parity is verified. No deletion is authorized.
- Keep each substantial paid campaign behind its measured launch review and exact enforced cost ceiling.
- Keep the harness-integration canary series inside the USD 80 hard ceiling. Reject a harness that needs a native API the locked router route cannot preserve.
- Keep the seven gpt-oss 89-task harness diagnostics inside USD 10.60 each and USD 74.20 combined.
- Finish those existing 89-task rows, plus the existing OpenCode and FX 89-task rows, without a second campaign for the same harness.

## Approval history

### 2026-08-17

- Approved the current scope and limits before the remaining project work starts.
- Directed the project to keep one authorization file indexed by canonical repository slug and to record approvals here.
- At 2026-08-17T09:13:49Z, approved protected public ingress for the existing control Space so workers can use short-lived capabilities without receiving a persistent Hugging Face credential.
- At 2026-08-17T13:30:03Z, requested an additional decision on capability-scoped inference and sandbox lifecycle operations plus the remaining legacy result-catalog migration.
- At 2026-08-17T15:37:46Z, approved replacing the proposed inference gateway with a separate inference-only credential passed to reviewed workers and rotated regularly. The broader control credential remains confined to the control Space. Sandbox lifecycle operations and remaining result-catalog migration remained pending.
- At 2026-08-17T18:39:15Z, authorized finalizing the project, including capability-scoped Sandbox lifecycle operations and the requested Terminal-Bench 3 low-thinking campaign. The full paid campaign remains subject to the mandatory measured-cost launch approval. Remaining result-catalog migration was still pending.

### 2026-08-18

- At 2026-08-18T00:25:01Z, approved all remaining project work needed for autonomous finalization, including result-catalog migration and viewer replacement. This did not authorize deleting legacy resources or bypassing the measured substantial paid-compute gate.
- At 2026-08-18T10:40:26Z, directed the project to run DeepSeek V4 Flash on Terminal-Bench 2.1 autonomously while separate web UI work proceeds. The campaign uses the existing enforced total project limit and does not authorize a new credential, persistent store, or unreviewed runtime.
- At 2026-08-18T17:20:36Z, set a USD 300 hard ceiling for the next Terminal-Bench 2.1 production campaign. This later campaign-specific limit supersedes the earlier cumulative USD 300 limit for that campaign only; earlier spend remains part of the reported project cost.

### 2026-08-20

- At 2026-08-20T13:53:14Z, approved creating one private canonical `<artifact-bucket>` in the selected namespace and connecting the existing canonical `<control-space>` to it. No additional persistent resource or paid campaign was approved.
- At 2026-08-20T14:01:49Z, approved creating one private canonical replacement `<control-space>` in the selected namespace because the previous Space no longer exists, then deploying the reviewed control service with writes disabled. No additional persistent resource or paid campaign was approved.
- At 2026-08-20T19:21:55Z, replaced the current five-trial campaign request with a single-trial diagnostic run of all 89 Terminal-Bench 2.1 tasks. The exact benchmark, model, revisions, Pi version, high reasoning, provider route, hardware class, authorization boundaries, and USD 300 hard ceiling remain unchanged. The result must be labeled diagnostic and must not be used as an official five-trial result.

### 2026-08-21

- At 2026-08-21T10:31:00Z, approved enabling production writes on the hosted control Space and launching campaigns beyond the built-in control-smoke canary. This does not authorize a new persistent resource, credential, or bypass of the measured substantial paid-compute gate. Existing cost, inventory, credential, and semantic-outcome limits remain.
- At 2026-08-21T20:06:00Z, approved a derived leaderboard SQLite snapshot in the canonical `<artifact-bucket>`. The configuration digest includes trial count, reasoning, provider, and Harbor version. Only final, clean, fully scored campaigns appear. No second persistent resource and no anonymous leaderboard API in this amendment.

### 2026-08-22

- At 2026-08-21T23:01:07Z, approved integrating the requested dashboard harnesses as Harbor agent plugins and proving each with one Terminal-Bench 2.1 two-task canary. Requested harnesses: OpenCode, Qwen Code, mini-swe-agent, Pi, Kimi Code, Hermes, Codex, OpenHands, OpenClaw, Claude Code. Use the existing `terminal-bench-2-1-canary` task pair, `openai/gpt-oss-20b` on Together through Inference Providers, reasoning off, publication role diagnostic. Keep one Space and one Bucket. Do not add a credential. Reject a harness that needs a native API the locked HF router route cannot preserve. Hard ceiling USD 80 for the whole canary series, including retries. This does not authorize the 89-task diagnostic or the official five-trial protocol.
- At 2026-08-22T07:33:41Z, approved merging the Sandbox admission work, implementing and merging the valid-result and pause-resume fixes, deploying the reviewed control service, and running one new full 89-task Terminal-Bench 2.1 single-trial diagnostic campaign from scratch with worker concurrency eight. The existing USD 300 hard campaign ceiling applies only after the updated launch review and control admission gates pass. The old campaign and publication remain immutable; append-only supersession may occur only after the new publication validates. No new persistent resource, credential, model promotion, or official five-trial claim is authorized.
- At 2026-08-22T12:09:50Z, approved making the official leaderboard the Space default route and allowing anonymous `GET /api/v1/leaderboard`. The operator dashboard moves to `/overview` behind a "Run benchmark" button and login. Campaigns, results, system, events, and mutations stay authenticated. Result click-through requires login. No new Space, Bucket, Dataset, or credential.
- At 2026-08-22T21:19:00Z, approved classifying Harbor environment-setup failures as infrastructure, retrying evidence-upload HTTP 500 responses, keeping an execution Job running after one upload failure, adding a run-page and CLI batch of existing infrastructure retries, deploying the reviewed revision, and retrying only eligible infrastructure tasks on the existing gpt-oss OpenCode 89-task campaign. Spend stays inside that campaign's locked ceiling. Sealed semantic, agent, verifier, policy, refusal, cancelled, and timeout outcomes stay sealed.
- At 2026-08-22T23:31:00Z, approved one new 89-task Terminal-Bench 2.1 single-trial diagnostic for each remaining Chat Completions harness that already has a two-task canary: Qwen Code, mini-swe-agent, Pi, Kimi Code, Hermes, OpenHands, and OpenClaw. Same model, provider, reasoning, publication role, and USD 10.60 campaign ceiling as the existing gpt-oss OpenCode 89-task run. Combined cap USD 74.20. OpenCode is not relaunched. Codex and Claude Code stay rejected on this route.

### 2026-08-23

- At 2026-08-23T04:30:39+08:00, approved all work needed to finish the active diagnostic campaign without a workflow. This includes fixing, testing, reviewing, committing, pushing, merging, and deploying terminal Job reservation settlement; using the fixed control revision for the campaign; recovering unresolved tasks through isolated one-task Jobs; publishing the complete result; and appending its supersession record. Keep real observed recovery cost at or below USD 100 and preserve the locked execution contract.
- At 2026-08-23T07:20:00Z, approved installing Harbor from a pinned `harbor-framework/harbor` git commit instead of PyPI, removing the empty-metrics sitecustomize workaround when that pin includes PR 2681, and deploying the reviewed revision. Existing campaign locks stay on their locked Harbor pin. No new persistent resource, credential, or campaign launch.
- At 2026-08-23T07:40:00Z, approved adding FX to the available harness list as a Harbor agent plugin with a gpt-oss Together deployment, then committing and deploying the reviewed revision. No campaign launch, persistent resource, or credential.
- At 2026-08-23T08:21:00Z, approved inspecting each existing gpt-oss 89-task diagnostic, fixing the defects those Jobs expose, deploying the reviewed revision, and retrying eligible infrastructure failures or unresolved tasks on those same campaigns so each of those harnesses can finish one full run. The already-started FX 89-task row may be finished. No second campaign for a harness that already has an 89-task row. No Codex or Claude Code. No ceiling increase.
- At 2026-08-23T09:01:00Z, approved making the namespace Sandbox cap configurable with default 16 and setting the live service to 128 so the existing 89-task diagnostics can evaluate faster. Campaign ceilings, inventory, and locked per-run Sandbox and worker limits stay unchanged.
