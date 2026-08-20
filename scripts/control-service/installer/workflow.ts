import { cp, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, relative, resolve } from "node:path";
import { canonicalJson } from "./canonical.js";
import type { HfAdapter } from "./hf.js";
import type { HttpAdapter } from "./http.js";
import type { IdentityAdapter } from "./identity.js";
import {
  assertManifestEqual,
  buildBundleManifest,
  expectedVariables,
  INSTALLER_MARKER,
  INSTALLER_VERSION,
  type InstallPlan,
  manifestDigest,
  parseTargetIds,
  type RemoteState,
  readPrivatePlan,
  SECRET_NAMES,
  validateOrigin,
  writePrivatePlan,
} from "./model.js";
import type { SourceAdapter } from "./source.js";

export interface InstallerDependencies {
  hf: HfAdapter;
  identity: IdentityAdapter;
  http: HttpAdapter;
  source: SourceAdapter;
  environment?: NodeJS.ProcessEnv;
  secretInput?: InstallerSecretInput;
}

export interface InstallerSecretInput {
  read(name: "HF_TOKEN" | "HF_INFERENCE_TOKEN"): Promise<string | undefined>;
}

function sortedStrings(values: readonly string[]): string[] {
  return [...values].sort((left, right) => left.localeCompare(right, "en"));
}

function sameStrings(left: readonly string[], right: readonly string[]): boolean {
  return JSON.stringify(sortedStrings(left)) === JSON.stringify(sortedStrings(right));
}

function assertRemoteSafe(
  state: RemoteState,
  expected: {
    spaceId: string;
    bucketId: string;
    variables: Record<string, string | null>;
  },
  options: { requireRunning: boolean; requireAllSecrets: boolean },
): void {
  if (!state.namespaceListingsComplete) {
    throw new Error("namespace listings are incomplete");
  }
  if (!state.space) {
    if (state.bucket) {
      throw new Error("an existing Bucket cannot be adopted without a marked Space");
    }
    return;
  }
  if (
    state.space.id !== expected.spaceId ||
    !state.space.private ||
    state.space.sdk !== "docker" ||
    state.space.requestedHardware !== "cpu-basic" ||
    (state.space.hardware !== null && state.space.hardware !== "cpu-basic") ||
    (options.requireRunning && state.space.hardware !== "cpu-basic") ||
    (expected.variables.HARBOR_HF_PUBLIC_ORIGIN !== null &&
      state.space.origin !== expected.variables.HARBOR_HF_PUBLIC_ORIGIN)
  ) {
    throw new Error("existing Space settings do not match the installer contract");
  }
  if (
    state.space.variables.HARBOR_HF_INSTALLER_MARKER !==
      expected.variables.HARBOR_HF_INSTALLER_MARKER ||
    state.space.variables.HARBOR_HF_INSTALLER_VERSION !==
      expected.variables.HARBOR_HF_INSTALLER_VERSION
  ) {
    throw new Error("existing Space is not installer-marked");
  }
  const expectedKeys = Object.keys(expected.variables).sort();
  const observedKeys = Object.keys(state.space.variables).sort();
  const partialKeys = expectedKeys.filter((key) => key !== "HARBOR_HF_PUBLIC_ORIGIN");
  const missingOnlyUnresolvedOrigin =
    JSON.stringify(partialKeys) === JSON.stringify(observedKeys) &&
    state.space.variables.HARBOR_HF_PUBLIC_ORIGIN === undefined;
  if (
    JSON.stringify(expectedKeys) !== JSON.stringify(observedKeys) &&
    !missingOnlyUnresolvedOrigin
  ) {
    throw new Error("existing Space variables do not match");
  }
  for (const [key, value] of Object.entries(expected.variables)) {
    if (key === "HARBOR_HF_PUBLIC_ORIGIN" && missingOnlyUnresolvedOrigin) continue;
    if (key === "HARBOR_HF_SOURCE_REVISION") {
      if (!/^[a-f0-9]{40}$/.test(state.space.variables[key] ?? "")) {
        throw new Error("existing Space source revision is invalid");
      }
    } else if (value === null || state.space.variables[key] !== value) {
      throw new Error("existing Space variables do not match");
    }
  }
  const unknownSecrets = state.space.secretNames.filter(
    (name) => !SECRET_NAMES.includes(name as (typeof SECRET_NAMES)[number]),
  );
  if (unknownSecrets.length > 0) throw new Error("existing Space has extra secrets");
  if (
    options.requireAllSecrets &&
    !sameStrings(state.space.secretNames, SECRET_NAMES)
  ) {
    throw new Error("Space secret names do not match");
  }
  if (options.requireRunning && state.space.runtimeStage !== "RUNNING") {
    throw new Error("Space runtime is not RUNNING");
  }
  if (state.bucket) {
    if (state.bucket.id !== expected.bucketId || !state.bucket.private) {
      throw new Error("existing Bucket does not match the installer contract");
    }
  }
}

function assertPreconditionsEqual(expected: RemoteState, observed: RemoteState): void {
  if (canonicalJson(expected) !== canonicalJson(observed)) {
    throw new Error("remote preconditions drifted after planning");
  }
}

async function pauseManagedTarget(
  plan: InstallPlan,
  dependencies: InstallerDependencies,
): Promise<void> {
  const observed = await dependencies.hf.observe(
    plan.targets.namespace,
    plan.targets.space_id,
    plan.targets.bucket_id,
  );
  const space = observed.space;
  if (
    !space ||
    space.id !== plan.targets.space_id ||
    space.variables.HARBOR_HF_INSTALLER_MARKER !== INSTALLER_MARKER ||
    space.variables.HARBOR_HF_INSTALLER_VERSION !== INSTALLER_VERSION ||
    space.variables.HARBOR_HF_NAMESPACE !== plan.targets.namespace ||
    space.variables.HARBOR_HF_BUCKET_ID !== plan.targets.bucket_id
  ) {
    return;
  }
  await dependencies.hf.pause(plan.targets.space_id);
}

function isInside(root: string, candidate: string): boolean {
  const path = relative(root, candidate);
  return path === "" || (!path.startsWith("..") && !path.startsWith("/"));
}

function assertPrivateOutputPaths(
  repositoryRoot: string,
  bundleDirectory: string,
  planPath: string,
): void {
  const bundle = resolve(bundleDirectory);
  const plan = resolve(planPath);
  if (
    isInside(repositoryRoot, bundle) ||
    isInside(repositoryRoot, plan) ||
    isInside(bundle, plan) ||
    isInside(plan, bundle)
  ) {
    throw new Error("bundle and plan paths must be separate and outside the checkout");
  }
}

export async function planInstall(
  input: {
    space: string;
    bucket?: string;
    bundleDirectory: string;
    planPath: string;
  },
  dependencies: InstallerDependencies,
): Promise<{ path: string; digest: string; plan: InstallPlan }> {
  const ids = parseTargetIds(input.space, input.bucket);
  const sourceBefore = await dependencies.source.inspect();
  assertPrivateOutputPaths(
    sourceBefore.repositoryRoot,
    input.bundleDirectory,
    input.planPath,
  );
  const hfCliVersion = await dependencies.hf.version();
  const principal = await dependencies.identity.resolve();
  await mkdir(resolve(input.bundleDirectory, ".."), { recursive: true });
  await dependencies.source.bundle(input.bundleDirectory);
  const sourceAfter = await dependencies.source.inspect();
  if (canonicalJson(sourceBefore) !== canonicalJson(sourceAfter)) {
    throw new Error("source changed while planning");
  }
  const manifest = await buildBundleManifest(input.bundleDirectory);
  const observed = await dependencies.hf.observe(
    ids.namespace,
    ids.spaceId,
    ids.bucketId,
  );
  const origin = observed.space?.origin ?? null;
  const variables = expectedVariables(
    ids.namespace,
    ids.bucketId,
    origin,
    principal.subject,
    sourceBefore.revision,
  );
  assertRemoteSafe(
    observed,
    { spaceId: ids.spaceId, bucketId: ids.bucketId, variables },
    { requireRunning: false, requireAllSecrets: false },
  );
  const plan: InstallPlan = {
    schema_version: "harbor-hf.install-plan.v1",
    production_ready: false,
    source: {
      revision: sourceBefore.revision,
      repository_root: sourceBefore.repositoryRoot,
    },
    bundle: {
      directory: resolve(input.bundleDirectory),
      manifest,
      manifest_digest: manifestDigest(manifest),
    },
    hf_cli_version: hfCliVersion,
    targets: {
      namespace: ids.namespace,
      space_id: ids.spaceId,
      bucket_id: ids.bucketId,
    },
    principal,
    expected_variables: variables,
    expected_secret_names: [...SECRET_NAMES],
    observed_preconditions: observed,
  };
  await mkdir(dirname(resolve(input.planPath)), { recursive: true, mode: 0o700 });
  return { ...(await writePrivatePlan(input.planPath, plan)), plan };
}

async function writePrivateEnvironmentFile(
  directory: string,
  name: string,
  values: Record<string, string>,
): Promise<string> {
  for (const [key, value] of Object.entries(values)) {
    if (
      !/^[A-Z][A-Z0-9_]*$/.test(key) ||
      value.includes("\n") ||
      value.includes("\r")
    ) {
      throw new Error("environment file value is unsafe");
    }
  }
  const path = resolve(directory, name);
  await writeFile(
    path,
    `${Object.entries(values)
      .sort(([left], [right]) => left.localeCompare(right, "en"))
      .map(([key, value]) => `${key}=${value}`)
      .join("\n")}\n`,
    { encoding: "utf8", mode: 0o600, flag: "wx" },
  );
  return path;
}

function concreteVariables(plan: InstallPlan, origin: string): Record<string, string> {
  const output: Record<string, string> = {};
  for (const [key, value] of Object.entries(plan.expected_variables)) {
    if (key === "HARBOR_HF_PUBLIC_ORIGIN") {
      output[key] = validateOrigin(origin);
    } else if (value === null) {
      throw new Error("install plan contains an unresolved variable");
    } else {
      output[key] = value;
    }
  }
  return output;
}

function initialVariables(plan: InstallPlan): Record<string, string> {
  const output: Record<string, string> = {};
  for (const [key, value] of Object.entries(plan.expected_variables)) {
    if (value !== null) output[key] = value;
  }
  return output;
}

async function secretValues(
  environment: NodeJS.ProcessEnv,
  missingNames: readonly string[],
  input?: InstallerSecretInput,
): Promise<Record<string, string>> {
  const sourceNames = {
    HF_TOKEN: "HARBOR_HF_INSTALL_CONTROL_SECRET",
    HF_INFERENCE_TOKEN: "HARBOR_HF_INSTALL_INFERENCE_SECRET",
  } as const;
  const values: Record<string, string> = {};
  for (const name of missingNames) {
    if (name !== "HF_TOKEN" && name !== "HF_INFERENCE_TOKEN") {
      throw new Error("unexpected secret name");
    }
    const value = environment[sourceNames[name]] ?? (await input?.read(name));
    if (!value || value.length < 8 || value.includes("\n") || value.includes("\r")) {
      throw new Error("required installer secret is missing or invalid");
    }
    values[name] = value;
  }
  if (
    values.HF_TOKEN &&
    values.HF_INFERENCE_TOKEN &&
    values.HF_TOKEN === values.HF_INFERENCE_TOKEN
  ) {
    throw new Error("installer secrets must be distinct");
  }
  return values;
}

export interface VerificationResult {
  production_ready: false;
  anonymous_live: "passed";
  anonymous_ready: "passed";
  authenticated_system: "passed" | "skipped";
  source_upload_revision: "passed" | "platform_observed";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactStatus(body: unknown, expected: string): boolean {
  return isRecord(body) && body.status === expected && Object.keys(body).length === 1;
}

function assertSystem(body: unknown, sourceRevision: string): void {
  if (!isRecord(body)) throw new Error("system response is invalid");
  const projection = body.projection;
  const contract = body.resource_contract;
  if (
    body.source_revision !== sourceRevision ||
    body.write_mode !== "disabled" ||
    !isRecord(projection) ||
    projection.ready !== true ||
    projection.integrity_error !== null ||
    !isRecord(contract) ||
    contract.spaces !== 1 ||
    contract.buckets !== 1 ||
    contract.operator_secrets !== 2
  ) {
    throw new Error("authenticated system verification failed");
  }
}

async function verifyPlan(
  plan: InstallPlan,
  dependencies: InstallerDependencies,
  expectedUploadSha?: string,
): Promise<VerificationResult> {
  const observed = await dependencies.hf.observe(
    plan.targets.namespace,
    plan.targets.space_id,
    plan.targets.bucket_id,
  );
  const expectedForRemote = observed.space
    ? concreteVariables(plan, observed.space.origin)
    : plan.expected_variables;
  assertRemoteSafe(
    observed,
    {
      spaceId: plan.targets.space_id,
      bucketId: plan.targets.bucket_id,
      variables: expectedForRemote,
    },
    { requireRunning: true, requireAllSecrets: true },
  );
  if (!observed.space || !observed.bucket) {
    throw new Error("installed resources are missing");
  }
  const variables = concreteVariables(plan, observed.space.origin);
  for (const [key, value] of Object.entries(variables)) {
    if (observed.space.variables[key] !== value) {
      throw new Error("managed Space variable verification failed");
    }
  }
  let sourceUploadRevision: VerificationResult["source_upload_revision"] =
    "platform_observed";
  if (expectedUploadSha) {
    if (observed.space.sha !== expectedUploadSha) {
      throw new Error("Space upload revision does not match");
    }
    sourceUploadRevision = "passed";
  } else if (
    observed.space.sha !== null &&
    !/^[a-f0-9]{40}$/.test(observed.space.sha)
  ) {
    throw new Error("Space upload revision is invalid");
  }

  const origin = validateOrigin(observed.space.origin);
  const live = await dependencies.http.getJson(new URL("/health/live", origin), {
    timeoutMs: 10_000,
    maxBytes: 64 * 1024,
  });
  if (live.status !== 200 || !exactStatus(live.body, "live")) {
    throw new Error("anonymous liveness verification failed");
  }
  const ready = await dependencies.http.getJson(new URL("/health/ready", origin), {
    timeoutMs: 10_000,
    maxBytes: 64 * 1024,
  });
  if (ready.status !== 200 || !exactStatus(ready.body, "ready")) {
    throw new Error("anonymous readiness verification failed");
  }

  const bearer = (dependencies.environment ?? process.env)
    .HARBOR_HF_INSTALL_VERIFY_BEARER;
  let authenticatedSystem: VerificationResult["authenticated_system"] = "skipped";
  if (bearer) {
    const system = await dependencies.http.getJson(new URL("/api/v1/system", origin), {
      bearer,
      timeoutMs: 10_000,
      maxBytes: 256 * 1024,
    });
    if (system.status !== 200) {
      throw new Error("authenticated system verification failed");
    }
    assertSystem(system.body, plan.source.revision);
    authenticatedSystem = "passed";
  }
  return {
    production_ready: false,
    anonymous_live: "passed",
    anonymous_ready: "passed",
    authenticated_system: authenticatedSystem,
    source_upload_revision: sourceUploadRevision,
  };
}

export async function verifyInstall(
  planPath: string,
  dependencies: InstallerDependencies,
): Promise<VerificationResult> {
  const { plan } = await readPrivatePlan(planPath);
  const version = await dependencies.hf.version();
  if (version !== plan.hf_cli_version) throw new Error("hf CLI version changed");
  return await verifyPlan(plan, dependencies);
}

export async function applyInstall(
  input: { planPath: string },
  dependencies: InstallerDependencies,
): Promise<VerificationResult> {
  const loaded = await readPrivatePlan(input.planPath);
  const plan = loaded.plan;
  const version = await dependencies.hf.version();
  if (version !== plan.hf_cli_version) throw new Error("hf CLI version changed");
  const source = await dependencies.source.inspect();
  if (
    source.repositoryRoot !== plan.source.repository_root ||
    source.revision !== plan.source.revision
  ) {
    throw new Error("source does not match the install plan");
  }
  assertManifestEqual(
    plan.bundle.manifest,
    await buildBundleManifest(plan.bundle.directory),
  );
  const principal = await dependencies.identity.resolve();
  if (canonicalJson(principal) !== canonicalJson(plan.principal)) {
    throw new Error("authenticated principal changed");
  }
  const observed = await dependencies.hf.observe(
    plan.targets.namespace,
    plan.targets.space_id,
    plan.targets.bucket_id,
  );
  assertPreconditionsEqual(plan.observed_preconditions, observed);
  assertRemoteSafe(
    observed,
    {
      spaceId: plan.targets.space_id,
      bucketId: plan.targets.bucket_id,
      variables: plan.expected_variables,
    },
    { requireRunning: false, requireAllSecrets: false },
  );

  const environment = dependencies.environment ?? process.env;
  const missingSecrets = SECRET_NAMES.filter(
    (name) => !observed.space?.secretNames.includes(name),
  );
  const secrets = await secretValues(
    environment,
    missingSecrets,
    dependencies.secretInput,
  );
  const tempDirectory = await mkdtemp(resolve(tmpdir(), "harbor-hf-install-"));
  const stagedBundle = resolve(tempDirectory, "bundle");
  await cp(plan.bundle.directory, stagedBundle, {
    recursive: true,
    dereference: false,
    errorOnExist: true,
    force: false,
  });
  assertManifestEqual(plan.bundle.manifest, await buildBundleManifest(stagedBundle));
  let remoteMutationStarted = false;
  let uploadSha: string | undefined;
  try {
    let variablesFile = await writePrivateEnvironmentFile(
      tempDirectory,
      "variables.env",
      initialVariables(plan),
    );
    const secretsFile =
      missingSecrets.length > 0
        ? await writePrivateEnvironmentFile(tempDirectory, "secrets.env", secrets)
        : null;

    if (!observed.space) {
      if (!secretsFile || missingSecrets.length !== SECRET_NAMES.length) {
        throw new Error("fresh Space creation requires both secrets");
      }
      remoteMutationStarted = true;
      await dependencies.hf.createSpace(
        plan.targets.space_id,
        variablesFile,
        secretsFile,
      );
      const created = await dependencies.hf.observe(
        plan.targets.namespace,
        plan.targets.space_id,
        plan.targets.bucket_id,
      );
      if (!created.space) throw new Error("created Space metadata is unavailable");
      const resolvedVariables = concreteVariables(plan, created.space.origin);
      await rm(variablesFile, { force: true });
      variablesFile = await writePrivateEnvironmentFile(
        tempDirectory,
        "variables-resolved.env",
        resolvedVariables,
      );
      await dependencies.hf.setVariables(plan.targets.space_id, variablesFile);
      const configured = await dependencies.hf.observe(
        plan.targets.namespace,
        plan.targets.space_id,
        plan.targets.bucket_id,
      );
      assertRemoteSafe(
        configured,
        {
          spaceId: plan.targets.space_id,
          bucketId: plan.targets.bucket_id,
          variables: resolvedVariables,
        },
        { requireRunning: false, requireAllSecrets: true },
      );
    } else {
      remoteMutationStarted = true;
      await dependencies.hf.pause(plan.targets.space_id);
      await dependencies.hf.setProtected(plan.targets.space_id);
      if (secretsFile) {
        await dependencies.hf.setSecrets(plan.targets.space_id, secretsFile);
      }
    }
    if (!observed.bucket) {
      remoteMutationStarted = true;
      await dependencies.hf.createBucket(plan.targets.bucket_id);
    }

    remoteMutationStarted = true;
    uploadSha = await dependencies.hf.uploadMirror(
      plan.targets.space_id,
      stagedBundle,
      plan.source.revision,
    );
    await dependencies.hf.setVariables(plan.targets.space_id, variablesFile);
    await dependencies.hf.restart(plan.targets.space_id);
    await dependencies.hf.wait(plan.targets.space_id);
    return await verifyPlan(plan, dependencies, uploadSha);
  } catch {
    if (remoteMutationStarted) {
      try {
        await pauseManagedTarget(plan, dependencies);
      } catch {
        // Best effort only. Never replace the fixed redacted failure.
      }
      throw new Error(
        "installation failed after remote mutation began; the Space was paused when possible; inspect remote diagnostics",
      );
    }
    throw new Error("installation failed before remote mutation began");
  } finally {
    await rm(tempDirectory, { recursive: true, force: true });
  }
}
