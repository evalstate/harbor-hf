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
  writePrivatePlan,
} from "../model.js";
import {
  activateInstallState,
  currentInstallPlanPath,
  discardInstallState,
  installerStateRoot,
  prepareInstallState,
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
  return {
    schema_version: "harbor-hf.install-plan.v1",
    production_ready: false,
    source: { revision, repository_root: repository },
    bundle: {
      directory: bundle,
      manifest: [],
      manifest_digest: manifestDigest([]),
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

  it("removes an uncommitted plan generation", async () => {
    const directory = await temporaryDirectory();
    const prepared = await prepareInstallState(
      "example/control",
      resolve(directory, "state"),
    );
    await discardInstallState(prepared);
    await expect(lstat(prepared.generationDirectory)).rejects.toThrow();
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
});
