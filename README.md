<p align="center">
  <img alt="harbor-hf" src="assets/harbor-hf-logo.svg" width="440">
</p>

`harbor-hf` is a Harbor control plane for running benchmark campaigns on Hugging Face infrastructure. It submits pinned work to HF Jobs, tracks retries and Endpoints, preserves evidence in an HF Bucket, and publishes queryable results without running the benchmark on your machine.

A hosted installation uses two persistent resources: one publicly reachable, application-protected control Space and one private Bucket. The Space serves the API and web console while its single control process reconciles immutable records stored in the Bucket.

Installer usage: `npm run install:plan -- --help`,
`npm run install:apply -- --help`, `npm run install:verify -- --help`, and
`npm run install:activate -- --help`.

After authenticated verification, activate only the built-in control-smoke
canary with an exact repeated target confirmation. Activation also requires an
empty durable campaign projection and an owner-only receipt that attests the
exact provider upload SHA. Installations completed by an older installer must
rerun `install:apply` once to record that attestation:

```bash
export HARBOR_HF_INSTALL_VERIFY_BEARER="$HARBOR_HF_CONTROL_BEARER_TOKEN"
npm run install:activate -- \
  --space '<namespace>/<control-space>' \
  --to canary \
  --confirm-space '<namespace>/<control-space>'
```

Emergency disablement does not require a healthy control API:

```bash
npm run install:activate -- \
  --space '<namespace>/<control-space>' \
  --to disabled \
  --confirm-space '<namespace>/<control-space>'
```

Production `enabled` promotion remains unavailable until durable canary
evidence and separately approved paid always-on hardware are proven.

## Install the CLI

The CLI requires Python 3.12 or newer. Install it with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install harbor-hf
```

Create a dedicated [fine-grained Hugging Face User Access Token](https://huggingface.co/docs/hub/security-tokens)
for the CLI, have its identity approved as an operator or reader by the control
service, and point the CLI at your control Space. Harbor-HF uses the token only
to verify its Hugging Face identity through `whoami-v2`; it does not require
repository, inference, Endpoint, Job, billing, or write permissions. Leave
those optional permissions disabled unless the token has a separately approved
purpose.

```bash
export HARBOR_HF_CONTROL_URL=https://<control-space>.hf.space
read -rsp 'Control bearer token: ' HARBOR_HF_CONTROL_BEARER_TOKEN
export HARBOR_HF_CONTROL_BEARER_TOKEN
printf '\n'
harbor-hf status
```

The CLI deliberately does not read the active `hf auth login` credential or
`HF_TOKEN`. Do not substitute a broad `read` or `write` token, print the token,
or store it in the repository. The CLI sends the explicit bearer token only to
the configured HTTPS control API and does not access the Bucket directly. A
valid token does not grant control access unless its Hugging Face identity is
also present in the service access list.

## Launch a campaign

A campaign selects promoted benchmark, model, harness, deployment, and launch-policy profiles. The control service resolves those aliases into an immutable campaign lock before creating physical work.

```bash
harbor-hf campaign submit \
  --benchmark <benchmark-profile> \
  --model <model-profile> \
  --harness <harness-profile> \
  --deployment <deployment-profile> \
  --launch-policy <launch-policy-profile> \
  --ceiling-microusd 5000000 \
  --yes
```

`5000000` micro-USD is a $5 campaign ceiling. Use an idempotency key when a caller may repeat the same request:

```bash
harbor-hf campaign submit \
  --benchmark <benchmark-profile> \
  --model <model-profile> \
  --harness <harness-profile> \
  --ceiling-microusd 5000000 \
  --idempotency-key <stable-request-key> \
  --yes
```

Repeating that command with the same actor and key adopts the existing campaign. It does not create a second logical run.

## Monitor work and results

```bash
harbor-hf campaign list
harbor-hf campaign status <campaign-id>
harbor-hf jobs
harbor-hf endpoints
harbor-hf results
harbor-hf audit
```

The same information is available in the Space's web console. The browser uses same-origin API requests and never receives the Bucket credential.

## Repair infrastructure failures

Terminal benchmark outcomes stay sealed. Only a task recorded as an eligible infrastructure failure can receive a bounded replacement:

```bash
harbor-hf campaign retry-infrastructure <campaign-id> \
  --task <task-id> \
  --reason "transient infrastructure failure" \
  --yes
```

Cancellation also preserves existing evidence:

```bash
harbor-hf campaign cancel <campaign-id> --yes
```

Publication is independent of execution. A publication retry rebuilds deterministic result objects from sealed task receipts and does not rerun model work.

## Safety model

- Campaign locks contain exact profile identities, task IDs, and input digests.
- Worker receipts identify the durable action that authorized the attempt.
- Mutations require an authenticated operator, explicit confirmation, and an idempotency key.
- Browser mutations also require same-origin requests and a CSRF token.
- The Bucket is append-only at the application boundary. A local SQLite database is only a disposable projection rebuilt from Bucket records.
- A terminal logical task cannot run again. Infrastructure repair creates a new physical attempt only for the failed task.
- Endpoint cleanup is complete only after a pause record reports zero ready replicas.
- Result catalogs retain outcome, quality, role, task counts, metric units, and source digests.

The [control service specification](docs/CONTROL_SERVICE.md) defines the durable record protocol, authentication boundary, recovery behavior, and deployment contract.

## Development

Clone the repository and install both locked environments:

```bash
git clone https://github.com/huggingface/harbor-hf.git
cd harbor-hf
uv sync --all-groups
npm ci
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the local quality gates.

## License

[Apache-2.0](LICENSE)
