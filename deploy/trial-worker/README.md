# Trial worker image

Prepared execution Jobs must use this reviewed worker image, never the
benchmark task image. The worker uses `skopeo` and `umoci` to verify and unpack
the locked task image in a root-owned workspace. It maps the unpacked rootfs to
a dedicated host UID/GID, strips setuid, setgid, and file capabilities, and
rejects special files and images that exceed the deployment's byte or entry
limits. Before extraction, it verifies compressed blob sizes and digests,
streams every gzip, tar, or Zstandard layer to bound expanded bytes and entries,
and reserves Job-local filesystem space for extraction and cleanup. It then
uses PRoot only to present that rootfs and emulate the task image user.
`setpriv` launches every task command as real UID/GID 60000 with no supplementary
groups, capabilities, or privilege escalation. The task remains that dedicated
unprivileged host UID even when it sees container UID 0.
The image builds PRoot 5.4 from its checksummed upstream source because Debian
Bookworm's PRoot 5.1 cannot translate the `statx` calls used by Ubuntu 24.04
package tools. Worker preflight rejects PRoot versions older than 5.3.

The host kernel boundary is Unix UID separation, empty supplementary groups,
an empty capability bounding set, and `no_new_privs`. PRoot is not treated as a
security boundary. The task sees host `/proc` metadata so normal tools work,
but preflight proves that its UID cannot read the root worker environment or a
root-owned probe file. Host `/run`, `/tmp`, worker files, capabilities, and
token files are not bound into the task rootfs.

The image contains pinned Python 3.12, the pinned Harbor commit, and the local
`harbor-hf-agents` package. Preparation and execution profiles can therefore
invoke the installed workers directly:

```text
python -m harbor_hf_agents.support.control_prepare_worker
python -m harbor_hf_agents.support.control_trial_job_worker
```

The root bootstrap similarly runs
`python -m harbor_hf_agents.support.job_root_bridge`; it does not download
worker code. The custom agent stops that trusted root bridge and kills all UID
60000 processes before Harbor starts the verifier. The verifier intentionally
shares the task rootfs so Terminal-Bench can score the modified filesystem.
A distinct verifier image remains unsupported.

Build and inspect the image locally:

```bash
docker build -f deploy/trial-worker/Dockerfile -t <trial-worker-image> .
docker image inspect <trial-worker-image> --format '{{json .RepoDigests}}'
```

The `Publish trial worker` workflow publishes only the selected commit's
`linux/amd64` image. It does not move a mutable `latest` tag. Record the
resulting registry digest in every deployment profile before deployment.

Before updating a deployment profile, review the installed Python, Harbor,
`git`, `proot`, `setpriv`, `skopeo`, `umoci`, and `zstd` versions from the build log.
Test the real-UID preflight, PRoot execution, process cleanup, transfer freeze,
image limits, and root bridge shutdown on the target HF Job hardware. Publish
the reviewed image and pin its registry digest. Until that pin is approved,
execution preflight fails as replacement-eligible infrastructure.
