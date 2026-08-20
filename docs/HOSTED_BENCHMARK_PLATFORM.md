# Hosted benchmark platform

## TL;DR

Harbor-HF should make reproducible benchmark runs easy to launch and share on Hugging Face. It should also make them easy to inspect. A user chooses a benchmark and model, then selects a harness and runtime policy. The platform locks every input and runs the work remotely. It separates infrastructure failures from model results, keeps sensitive evidence private, and publishes results that others can verify.

## Purpose

The hosted platform gives model and harness developers a common place to prove that their work performs well. It should support trusted comparisons without requiring each team to build its own runner, storage format, results site, or recovery system.

A dedicated Hugging Face organization hosts the product-facing application and public result resources. Harbor-HF remains the infrastructure layer around Harbor. Harbor continues to own benchmark resolution and trial execution. It also owns harness behavior, verification, and native trial records.

## Product experience

A user starts from a small set of clear choices:

- benchmark and task set.
- model and inference configuration.
- harness and reasoning policy.
- runtime and cost ceiling.

The service resolves those choices to immutable versions before launch. The user sees what will run, the expected cost, the enforced maximum cost, and the work that cancellation will stop. Active runs expose progress and operational failures without exposing private infrastructure details.

Completed runs provide scores and task outcomes. They also show cost and runtime, along with provenance and supported traces. Each result links to the exact benchmark and model. It also identifies the harness, configuration, and evidence digest used to produce the result.

The first public product should use fixed, reviewed benchmarks and harnesses. Broader user inputs can be added after their isolation and reproducibility rules are equally strong.

## Platform boundaries

Harbor-HF has four main responsibilities.

### Control

The control service accepts a launch request and resolves approved profiles. It locks the campaign, admits cost, and records every action. It also handles cancellation and recovery. Publication and audit history use the same durable control path.

The durable record is immutable object storage. Local databases and web sessions are disposable views that can be rebuilt or recreated.

### Execution

Harbor prepares one exact job definition from the selected benchmark and harness. Remote workers execute that locked job on Hugging Face infrastructure. Logical trials keep separate identities and evidence. They also keep separate outcomes and retry histories even when physical scheduling groups work for efficiency.

The execution layer remains independent of product names. A new Harbor-supported benchmark or compatible model should need configuration only. A new harness should only need a Harbor agent package and profile.

### Evidence

Every trial keeps enough private evidence to explain and reproduce its result. This includes the Harbor lock and result, plus the session and trace. The selected profile can also require logs, workspace output, timing, usage, and infrastructure receipts.

Evidence is content-addressed and verified before publication. Public results contain only approved normalized fields and approved trace data. Raw sessions, workspace files, and private operational evidence remain private.

### Publication

Publication starts from one verified canonical result. Harbor-HF can then produce its own result tables, public comparison views, ATIF traces, and Harbor Hub submissions from the same immutable source.

Trial workers never publish directly to shared public destinations. This keeps scoring and visibility decisions separate from benchmark execution. Publication remains a separate control action.

## Comparable and trustworthy results

A score is useful only when readers can tell what happened. The platform must preserve the difference between model behavior and platform failure.

Infrastructure outcomes use a typed failure model. Each failure records its area and cause. It also records scope and retry eligibility. Transient task-local failures may receive a bounded replacement. Deterministic shared defects stop affected work. Model refusals and verifier outcomes remain part of the model result. Benchmark timeouts and other semantic outcomes do too.

Published comparisons use the same task selection and attempt rules. Scoring and failure treatment also stay consistent. Hidden runtime details remain explicitly unknown instead of being guessed. Changed inputs produce a different result identity.

Integrity checks operate on the exact session and trace that belong to the locked task. Public evidence can show reproducibility and infrastructure checks. Sensitive detection policy and private evidence do not need to become public artifacts.

## Harness and benchmark support

Harnesses are first-class profiles with pinned package versions, entry points, supported models, reasoning controls, session requirements, and trace formats. Pi and Terminus 2 should use the same generic Harbor job path. Other compatible harnesses should follow the same contract.

ATIF is the portable trace format for result exchange. A harness that already writes valid ATIF should keep that output. A shared converter may handle a supported native session format when needed. Benchmark-specific converters do not belong in the control or worker core.

Fixed public benchmarks provide the comparable catalog. Support for private or user-supplied benchmarks can come later through content-addressed bundles with the same task locking and evidence rules.

## Secret handling and security

The current platform keeps its control credential outside remote workers. The inference credential stays in a root-owned bridge, while the benchmark agent receives a loopback route and a non-secret placeholder key. Workers reject persistent platform credentials in their environment. Browser responses and public results omit raw evidence and credential-bearing fields.

These controls protect credentials that the benchmark cannot read. The current hosted path does not yet provide complete handling for arbitrary user secrets. It retains complete trial evidence, but its direct leak check covers only the worker capability. General user-managed compute and model credentials must remain unsupported until that gap is closed.

Future user-secret support must keep each secret with one declared consumer. A launch credential stays in the control service when only the control service needs it. A model credential stays in a trusted bridge when the benchmark agent does not need it.

Known values must be scanned in paths and file bytes before evidence upload. The scan also covers logs and sessions, along with traces and results. Workspace output receives the same check. Public candidates also need a generic credential-pattern scan. A finding quarantines and invalidates the attempt before publication. Canonical evidence is not rewritten to disguise a leak.

No scanner can guarantee safety after a secret becomes visible to an agent because the agent can transform or encode it. Process isolation is the primary control. Scanning is the final fail-closed check.

## Long-term roadmap

### Reliable execution

Make failure classification and bounded recovery consistent across every benchmark and harness. Cancellation and cleanup must follow the same rules. Cost reconciliation must also remain consistent. Operational faults must never silently become model scores.

### Broader catalog

Grow the supported catalog through reviewed profiles and packages. Keep the shared control and worker paths free of benchmark or harness branches.

### Self-service runs

Let users launch from approved choices with a clear cost estimate, enforced ceiling, progress view, and cancellation. Add user-funded execution only after short-lived credential custody, isolation, leak prevention, and spend attribution are complete.

### Portable evidence

Standardize native sessions and ATIF traces. Use the same provenance and result records across Harbor-HF, Harbor Hub, and other compatible tools.

### Public results

Build a useful catalog of comparable runs with stable links and filters. Include task details, cost, runtime, and trace inspection. Keep raw private evidence behind the control boundary.

### Scale

Scale physical execution without changing logical trial identity or result semantics. Scheduling and batching should become more efficient. Storage should improve while the user-facing contract stays stable.

## Non-goals

Harbor-HF is not a general remote-compute service. It does not need benchmark-specific worker scripts, arbitrary unreviewed images, public raw evidence, or a separate control system for each harness.

The platform should not claim comparability when inputs or runtime details are unknown. It should not hide infrastructure failures inside a score. It should not give benchmark agents credentials they do not need.

## End state

A model or harness developer can select a supported benchmark, review the exact configuration and maximum cost, launch the run, follow its progress, and receive a verified result with portable traces. Another person can inspect the public record and understand what ran, what failed, how the score was computed, and which evidence supports it.

Adding a normal benchmark, model, or harness does not require a new Harbor-HF execution path. Security and evidence rules apply in the same way to every run. Public results remain useful without exposing credentials, private workspaces, or internal infrastructure.

## Related documents

- [Control service](CONTROL_SERVICE.md)
- [Control service plan](2026-08-16-harbor-hf-control-service-plan.md)
- [General Harbor job path](2026-08-18-general-harbor-job-path-plan.md)
- [Harbor compatibility contract](harbor-integration-contract.md)
- [Result publication](result-publication.md)
