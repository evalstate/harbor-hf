import {
  chmod,
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  rm,
  stat,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  expectedVariables,
  type InstallPlan,
  manifestDigest,
  readPrivatePlan,
  writePrivatePlan,
} from "../model.js";
import {
  activateInstallState,
  carryBootstrapReceipt,
  currentInstallPlanPath,
  discardInstallState,
  findCurrentInstallPlanPath,
  installerStateRoot,
  prepareInstallState,
  preserveBootstrapReceipt,
  readBootstrapReceipt,
  withInstallerStateLock,
  writeBootstrapReceipt,
} from "../state.js";

const temporaryDirectories: string[] = [];

async function temporaryDirectory(): Promise<string> {
  const directory = await mkdtemp(resolve(tmpdir(), "installer-state-test-"));
  temporaryDirectories.push(directory);
  return directory;
}

afterEach(async () => {
  await Promise.all(
    temporaryDirectories
      .splice(0)
      .map((path) => rm(path, { recursive: true, force: true })),
  );
});

function plan(repository: string, bundle: string): InstallPlan {
  const revision = "a".repeat(40);
  const installId = "f".repeat(64);
  const bundleDigest = manifestDigest([]);
  return {
    schema_version: "harbor-hf.install-plan.v2",
    install_id: installId,
    production_ready: false,
    source: { revision, repository_root: repository },
    bundle: {
      directory: bundle,
      manifest: [],
      manifest_digest: bundleDigest,
    },
    hf_cli_version: "1.23.0",
    targets: {
      namespace: "example",
      space_id: "example/control",
      bucket_id: "example/control-artifacts",
    },
    principal: {
      subject: "stable-subject",
      username: "example-user",
      organizations: [],
    },
    expected_variables: expectedVariables(
      "example",
      "example/control-artifacts",
      null,
      "stable-subject",
      revision,
      {
        installId,
        manifestDigest: bundleDigest,
        phase: "installed",
      },
    ),
    expected_secret_names: ["HF_INFERENCE_TOKEN", "HF_TOKEN"],
    observed_preconditions: {
      namespaceListingsComplete: true,
      space: null,
      bucket: null,
    },
  };
}

async function savedState() {
  const directory = await temporaryDirectory();
  const root = resolve(directory, "state");
  const prepared = await prepareInstallState("example/control", root);
  await mkdir(prepared.bundleDirectory);
  await writePrivatePlan(
    prepared.planPath,
    plan(resolve(directory, "repository"), prepared.bundleDirectory),
  );
  await activateInstallState(prepared, "example/control");
  return { directory, root, prepared };
}

describe("private installer state", () => {
  it("uses XDG state with a private home fallback", () => {
    expect(
      installerStateRoot(undefined, { XDG_STATE_HOME: "/state-placeholder" }, "/home"),
    ).toBe("/state-placeholder/harbor-hf/install");
    expect(installerStateRoot(undefined, {}, "/home-placeholder")).toBe(
      "/home-placeholder/.local/state/harbor-hf/install",
    );
    expect(installerStateRoot("/override-placeholder", {}, "/home")).toBe(
      "/override-placeholder",
    );
    expect(() => installerStateRoot("relative-override", {}, "/home")).toThrow(
      "absolute",
    );
    expect(() =>
      installerStateRoot(undefined, { XDG_STATE_HOME: "relative" }, "/home"),
    ).toThrow("absolute");
  });

  it("stores and resolves a target-bound owner-only current plan", async () => {
    const { root, prepared } = await savedState();
    await expect(currentInstallPlanPath("example/control", root)).resolves.toBe(
      prepared.planPath,
    );
    expect(basename(prepared.targetDirectory)).toMatch(/^[a-f0-9]{64}$/);
    for (const directory of [
      root,
      prepared.targetDirectory,
      prepared.generationDirectory,
    ]) {
      expect((await stat(directory)).mode & 0o777).toBe(0o700);
    }
    expect(
      (await stat(resolve(prepared.targetDirectory, "current.json"))).mode & 0o777,
    ).toBe(0o600);
    expect(
      await readFile(resolve(prepared.targetDirectory, "current.json"), "utf8"),
    ).toContain('"space_id":"example/control"');
  });

  it("allows planning to replace an unsupported legacy local plan", async () => {
    const { root, prepared } = await savedState();
    const legacy = JSON.parse(await readFile(prepared.planPath, "utf8")) as Record<
      string,
      unknown
    >;
    legacy.schema_version = "harbor-hf.install-plan.v1";
    await writeFile(prepared.planPath, `${JSON.stringify(legacy)}\n`);

    await expect(
      findCurrentInstallPlanPath("example/control", root),
    ).resolves.toBeUndefined();
    await expect(currentInstallPlanPath("example/control", root)).rejects.toThrow(
      "unsupported install plan",
    );

    await writeBootstrapReceipt(prepared.planPath, {
      schema_version: "harbor-hf.install-bootstrap-receipt.v1",
      install_id: "f".repeat(64),
      plan_digest: `sha256:${"d".repeat(64)}`,
      space_id: "example/control",
      bucket_id: "example/control-artifacts",
      source_revision: "a".repeat(40),
      manifest_digest: `sha256:${"b".repeat(64)}`,
    });
    await expect(findCurrentInstallPlanPath("example/control", root)).rejects.toThrow(
      "manual recovery",
    );
  });

  it("rejects target confusion and insecure pointer state", async () => {
    const { root, prepared } = await savedState();
    await expect(currentInstallPlanPath("example/other", root)).rejects.toThrow();

    const pointer = resolve(prepared.targetDirectory, "current.json");
    await chmod(pointer, 0o644);
    await expect(currentInstallPlanPath("example/control", root)).rejects.toThrow(
      "owner-only",
    );

    await rm(pointer);
    const target = resolve(prepared.targetDirectory, "pointer-target");
    await writeFile(target, "{}\n", { mode: 0o600 });
    await symlink(target, pointer);
    await expect(currentInstallPlanPath("example/control", root)).rejects.toThrow(
      "owner-only",
    );
  });

  it("does not hide a receipt when the selected plan file is missing", async () => {
    const { root, prepared } = await savedState();
    await writeBootstrapReceipt(prepared.planPath, {
      schema_version: "harbor-hf.install-bootstrap-receipt.v1",
      install_id: "f".repeat(64),
      plan_digest: `sha256:${"d".repeat(64)}`,
      space_id: "example/control",
      bucket_id: "example/control-artifacts",
      source_revision: "a".repeat(40),
      manifest_digest: `sha256:${"b".repeat(64)}`,
    });
    await rm(prepared.planPath);

    await expect(findCurrentInstallPlanPath("example/control", root)).rejects.toThrow(
      "manual recovery",
    );
  });

  it("does not hide proof in an older plan generation", async () => {
    const { directory, root, prepared } = await savedState();
    await writeBootstrapReceipt(prepared.planPath, {
      schema_version: "harbor-hf.install-bootstrap-receipt.v1",
      install_id: "f".repeat(64),
      plan_digest: `sha256:${"d".repeat(64)}`,
      space_id: "example/control",
      bucket_id: "example/control-artifacts",
      source_revision: "a".repeat(40),
      manifest_digest: `sha256:${"b".repeat(64)}`,
    });
    const next = await prepareInstallState("example/control", root);
    await mkdir(next.bundleDirectory);
    await writePrivatePlan(
      next.planPath,
      plan(resolve(directory, "repository"), next.bundleDirectory),
    );
    await activateInstallState(next, "example/control");

    await expect(findCurrentInstallPlanPath("example/control", root)).rejects.toThrow(
      "outside the current installer generation",
    );
  });

  it("removes an uncommitted plan generation", async () => {
    const directory = await temporaryDirectory();
    const prepared = await prepareInstallState(
      "example/control",
      resolve(directory, "state"),
    );
    await discardInstallState(prepared);
    await expect(lstat(prepared.generationDirectory)).rejects.toThrow();
  });

  it("serializes plan and apply operations for one target", async () => {
    const directory = await temporaryDirectory();
    const root = resolve(directory, "state");
    let release: (() => void) | undefined;
    let entered: (() => void) | undefined;
    const enteredPromise = new Promise<void>((resolvePromise) => {
      entered = resolvePromise;
    });
    const releasePromise = new Promise<void>((resolvePromise) => {
      release = resolvePromise;
    });
    const first = withInstallerStateLock("example/control", root, async () => {
      entered?.();
      await releasePromise;
    });
    await enteredPromise;

    await expect(
      withInstallerStateLock("example/control", root, async () => undefined),
    ).rejects.toThrow("another installer");
    release?.();
    await first;
    await expect(
      withInstallerStateLock("example/control", root, async () => "released"),
    ).resolves.toBe("released");
  });

  it("refuses to activate a prepared state object under another target", async () => {
    const directory = await temporaryDirectory();
    const prepared = await prepareInstallState(
      "example/control",
      resolve(directory, "state"),
    );
    await mkdir(prepared.bundleDirectory);
    await writePrivatePlan(
      prepared.planPath,
      plan(resolve(directory, "repository"), prepared.bundleDirectory),
    );
    await expect(
      activateInstallState(
        {
          ...prepared,
          targetDirectory: resolve(directory, "different-target"),
        },
        "example/control",
      ),
    ).rejects.toThrow("does not match");
  });

  it("stores an idempotent owner-only bootstrap receipt beside the plan", async () => {
    const { prepared } = await savedState();
    const receipt = {
      schema_version: "harbor-hf.install-bootstrap-receipt.v1" as const,
      install_id: "f".repeat(64),
      plan_digest: `sha256:${"d".repeat(64)}`,
      space_id: "example/control",
      bucket_id: "example/control-artifacts",
      source_revision: "a".repeat(40),
      manifest_digest: `sha256:${"b".repeat(64)}`,
    };
    await writeBootstrapReceipt(prepared.planPath, receipt);
    await writeBootstrapReceipt(prepared.planPath, receipt);
    await expect(readBootstrapReceipt(prepared.planPath)).resolves.toEqual(receipt);
    const path = resolve(prepared.generationDirectory, "bootstrap-receipt.json");
    expect((await stat(path)).mode & 0o777).toBe(0o600);
    await chmod(path, 0o644);
    await expect(readBootstrapReceipt(prepared.planPath)).rejects.toThrow("owner-only");
  });

  it("atomically accepts concurrent writes of the same bootstrap receipt", async () => {
    const { prepared } = await savedState();
    const receipt = {
      schema_version: "harbor-hf.install-bootstrap-receipt.v1" as const,
      install_id: "f".repeat(64),
      plan_digest: `sha256:${"d".repeat(64)}`,
      space_id: "example/control",
      bucket_id: "example/control-artifacts",
      source_revision: "a".repeat(40),
      manifest_digest: `sha256:${"b".repeat(64)}`,
    };
    await expect(
      Promise.all([
        writeBootstrapReceipt(prepared.planPath, receipt),
        writeBootstrapReceipt(prepared.planPath, receipt),
      ]),
    ).resolves.toEqual([undefined, undefined]);
    await expect(readBootstrapReceipt(prepared.planPath)).resolves.toEqual(receipt);
  });

  it("carries exact Bucket proof into a replacement plan generation", async () => {
    const { directory, root, prepared } = await savedState();
    const previous = await readPrivatePlan(prepared.planPath);
    await writeBootstrapReceipt(prepared.planPath, {
      schema_version: "harbor-hf.install-bootstrap-receipt.v1",
      install_id: previous.plan.install_id,
      plan_digest: previous.digest,
      space_id: previous.plan.targets.space_id,
      bucket_id: previous.plan.targets.bucket_id,
      source_revision: previous.plan.source.revision,
      manifest_digest: previous.plan.bundle.manifest_digest,
    });

    const next = await prepareInstallState("example/control", root);
    await mkdir(next.bundleDirectory);
    const nextWritten = await writePrivatePlan(
      next.planPath,
      plan(resolve(directory, "repository"), next.bundleDirectory),
    );
    await expect(
      carryBootstrapReceipt(
        prepared.planPath,
        next.planPath,
        (await readPrivatePlan(next.planPath)).plan,
        nextWritten.digest,
      ),
    ).resolves.toBe(true);
    await expect(readBootstrapReceipt(next.planPath)).resolves.toMatchObject({
      plan_digest: nextWritten.digest,
      install_id: previous.plan.install_id,
    });
  });

  it("does not discard Bucket proof when the remote Bucket disappears", async () => {
    const { directory, root, prepared } = await savedState();
    const previous = await readPrivatePlan(prepared.planPath);
    await writeBootstrapReceipt(prepared.planPath, {
      schema_version: "harbor-hf.install-bootstrap-receipt.v1",
      install_id: previous.plan.install_id,
      plan_digest: previous.digest,
      space_id: previous.plan.targets.space_id,
      bucket_id: previous.plan.targets.bucket_id,
      source_revision: previous.plan.source.revision,
      manifest_digest: previous.plan.bundle.manifest_digest,
    });
    const next = await prepareInstallState("example/control", root);
    await mkdir(next.bundleDirectory);
    const nextWritten = await writePrivatePlan(
      next.planPath,
      plan(resolve(directory, "repository"), next.bundleDirectory),
    );

    await expect(
      preserveBootstrapReceipt(
        prepared.planPath,
        next.planPath,
        (await readPrivatePlan(next.planPath)).plan,
        nextWritten.digest,
        {
          spacePresent: true,
          bucketPresent: false,
          phase: "credentials_required",
        },
      ),
    ).rejects.toThrow("proven resource is missing");
    await expect(readBootstrapReceipt(next.planPath)).resolves.toBeUndefined();
  });

  it("establishes proof in every exact installed plan generation", async () => {
    const { directory, root, prepared } = await savedState();
    const next = await prepareInstallState("example/control", root);
    await mkdir(next.bundleDirectory);
    const nextPlan = plan(resolve(directory, "repository"), next.bundleDirectory);
    nextPlan.expected_variables.HARBOR_HF_PUBLIC_ORIGIN =
      "https://example-control.hf.space";
    nextPlan.observed_preconditions = {
      namespaceListingsComplete: true,
      space: {
        id: "example/control",
        private: true,
        sdk: "docker",
        origin: "https://example-control.hf.space",
        sha: "a".repeat(40),
        runtimeStage: "RUNNING",
        hardware: "cpu-basic",
        requestedHardware: "cpu-basic",
        variables: Object.fromEntries(
          Object.entries(nextPlan.expected_variables).filter(
            (entry): entry is [string, string] => entry[1] !== null,
          ),
        ),
        secretNames: ["HF_INFERENCE_TOKEN", "HF_TOKEN"],
      },
      bucket: {
        id: "example/control-artifacts",
        private: true,
      },
    };
    const nextWritten = await writePrivatePlan(next.planPath, nextPlan);

    await preserveBootstrapReceipt(
      prepared.planPath,
      next.planPath,
      (await readPrivatePlan(next.planPath)).plan,
      nextWritten.digest,
      {
        spacePresent: true,
        bucketPresent: true,
        phase: "installed",
      },
    );
    await expect(readBootstrapReceipt(next.planPath)).resolves.toMatchObject({
      plan_digest: nextWritten.digest,
      install_id: nextPlan.install_id,
      source_revision: nextPlan.source.revision,
      manifest_digest: nextPlan.bundle.manifest_digest,
    });
  });
});
