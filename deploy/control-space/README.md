---
title: Harbor-HF Control
sdk: docker
app_port: 7860
hf_oauth: true
hf_oauth_expiration_minutes: 720
suggested_hardware: cpu-upgrade
---

# Harbor-HF control

This private Docker Space runs the Harbor-HF control API and web application.
The release is generated from an exact reviewed Harbor-HF source revision.

The Space reads and creates immutable objects in the canonical private Bucket
through the Hugging Face API. Operators configure two persistent secrets:
`HF_TOKEN` for control operations and `HF_INFERENCE_TOKEN` for reviewed
benchmark workers. Deployment-specific resource identifiers and OAuth bootstrap
subjects remain private Space variables.

Preparation and execution Jobs use signed capabilities bound to one immutable
Run lock, launch action, task set, operation set, and expiration. The control
process retains Job lifecycle authority and never forwards `HF_TOKEN` or a
writable Bucket mount. A reviewed inference-required Job receives only
`HF_INFERENCE_TOKEN`, which its root-owned bridge isolates from the benchmark
agent.
