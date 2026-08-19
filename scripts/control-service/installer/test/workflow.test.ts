import {
  access,
  mkdir,
  mkdtemp,
  readFile,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import type { HfAdapter } from "../hf.js";
import type { HttpAdapter } from "../http.js";
import type { IdentityAdapter } from "../identity.js";
import { expectedVariables, type Principal, type RemoteState } from "../model.js";
import type { SourceAdapter } from "../source.js";
import {
  applyInstall,
  type InstallerDependencies,
  planInstall,
  verifyInstall,
} from "../workflow.js";

const REVISION = "a".repeat(40);
const OLD_REVISION = "c".repeat(40);
const UPLOAD_SHA = "b".repeat(40);
const ORIGIN = "https://placeholder-control.hf.space";
const temporaryDirectories: string[] = [];

async function temporaryDirectory(): Promise<string> {
  const directory = await mkdtemp(resolve(tmpdir(), "installer-workflow-test-"));
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

function parseEnvironmentFile(content: string): Record<string, string> {
  return Object.fromEntries(
    content
      .trim()
      .split("\n")
      .filter(Boolean)
      .map((line) => {
        const separator = line.indexOf("=");
        return [line.slice(0, separator), line.slice(separator + 1)];
      }),
  );
}

class FakeSource implements SourceAdapter {
  constructor(
    readonly repositoryRoot: string,
    readonly revision = REVISION,
  ) {}

  async inspect() {
    return { repositoryRoot: this.repositoryRoot, revision: this.revision };
  }

  async bundle(directory: string): Promise<void> {
    await rm(directory, { recursive: true, force: true });
    await mkdir(resolve(directory, "nested"), { recursive: true });
    await writeFile(resolve(directory, "Dockerfile"), "FROM scratch\n");
    await writeFile(resolve(directory, "nested", "release.txt"), this.revision);
  }
}

class FakeIdentity implements IdentityAdapter {
  readonly principal: Principal = {
    subject: "stable-subject",
    username: "example-user",
    organizations: ["example-org"],
  };

  async resolve(): Promise<Principal> {
    return structuredClone(this.principal);
  }
}

class FakeHttp implements HttpAdapter {
  readonly requests: { path: string; bearer?: string }[] = [];
  readyStatus = "ready";
  systemIntegrityError: string | null = null;

  async getJson(
    url: URL,
    options: { bearer?: string; timeoutMs: number; maxBytes: number },
  ) {
    this.requests.push({
      path: url.pathname,
      ...(options.bearer ? { bearer: options.bearer } : {}),
    });
    if (url.pathname === "/health/live") {
      return { status: 200, body: { status: "live" } };
    }
    if (url.pathname === "/health/ready") {
      return {
        status: this.readyStatus === "ready" ? 200 : 503,
        body: { status: this.readyStatus },
      };
    }
    if (url.pathname === "/api/v1/system") {
      return {
        status: 200,
        body: {
          source_revision: REVISION,
          write_mode: "disabled",
          projection: {
            ready: this.systemIntegrityError === null,
            integrity_error: this.systemIntegrityError,
          },
          resource_contract: {
            spaces: 1,
            buckets: 1,
            operator_secrets: 2,
          },
        },
      };
    }
    return { status: 404, body: { status: "missing" } };
  }
}

class FakeHf implements HfAdapter {
  state: RemoteState = {
    namespaceListingsComplete: true,
    space: null,
    bucket: null,
  };
  readonly calls: string[] = [];
  readonly temporaryPaths: string[] = [];
  readonly uploadedBundleDirectories: string[] = [];
  failCreateBucket = false;
  failCreateSpaceResponse = false;
  failCreateWithUnmarkedRace = false;
  failSetVariablesAfterUpload = false;
  failUpload = false;
  failObserve = false;

  async version(): Promise<string> {
    return "1.23.0";
  }

  async whoamiUsername(): Promise<string> {
    throw new Error("not used");
  }

  async authToken(): Promise<string> {
    throw new Error("not used");
  }

  async observe(
    _namespace: string,
    _spaceId: string,
    _bucketId: string,
  ): Promise<RemoteState> {
    if (this.failObserve) throw new Error("listing failed");
    this.calls.push("observe");
    return structuredClone(this.state);
  }

  async createSpace(
    spaceId: string,
    variablesFile: string,
    secretsFile: string,
  ): Promise<void> {
    this.calls.push("createSpace");
    this.temporaryPaths.push(variablesFile, secretsFile);
    expect((await stat(variablesFile)).mode & 0o777).toBe(0o600);
    expect((await stat(secretsFile)).mode & 0o777).toBe(0o600);
    const variables = parseEnvironmentFile(await readFile(variablesFile, "utf8"));
    const secrets = parseEnvironmentFile(await readFile(secretsFile, "utf8"));
    expect(Object.keys(secrets).sort()).toEqual(["HF_INFERENCE_TOKEN", "HF_TOKEN"]);
    if (this.failCreateWithUnmarkedRace) {
      this.state.space = {
        id: spaceId,
        private: true,
        sdk: "docker",
        origin: ORIGIN,
        sha: UPLOAD_SHA,
        runtimeStage: "RUNNING",
        hardware: "cpu-basic",
        requestedHardware: "cpu-basic",
        variables: {},
        secretNames: [],
      };
      throw new Error("target appeared concurrently");
    }
    this.state.space = {
      id: spaceId,
      private: true,
      sdk: "docker",
      origin: ORIGIN,
      sha: null,
      runtimeStage: "BUILDING",
      hardware: null,
      requestedHardware: "cpu-basic",
      variables,
      secretNames: Object.keys(secrets).sort(),
    };
    if (this.failCreateSpaceResponse) throw new Error("lost create response");
  }

  async createBucket(bucketId: string): Promise<void> {
    this.calls.push("createBucket");
    if (this.failCreateBucket) throw new Error("provider detail");
    this.state.bucket = { id: bucketId, private: true };
  }

  async setVariables(_spaceId: string, variablesFile: string): Promise<void> {
    this.calls.push("setVariables");
    this.temporaryPaths.push(variablesFile);
    if (!this.state.space) throw new Error("missing Space");
    if (this.failSetVariablesAfterUpload && this.calls.includes("uploadMirror")) {
      throw new Error("provider detail");
    }
    this.state.space.variables = parseEnvironmentFile(
      await readFile(variablesFile, "utf8"),
    );
  }

  async setSecrets(_spaceId: string, secretsFile: string): Promise<void> {
    this.calls.push("setSecrets");
    this.temporaryPaths.push(secretsFile);
    if (!this.state.space) throw new Error("missing Space");
    const values = parseEnvironmentFile(await readFile(secretsFile, "utf8"));
    this.state.space.secretNames = [
      ...new Set([...this.state.space.secretNames, ...Object.keys(values)]),
    ].sort();
  }

  async setProtected(): Promise<void> {
    this.calls.push("setProtected");
    if (!this.state.space) throw new Error("missing Space");
    this.state.space.private = true;
  }

  async uploadMirror(
    _spaceId: string,
    _bundleDirectory: string,
    _revision: string,
  ): Promise<string> {
    this.calls.push("uploadMirror");
    this.uploadedBundleDirectories.push(_bundleDirectory);
    if (this.failUpload) throw new Error("upload failed");
    if (!this.state.space) throw new Error("missing Space");
    this.state.space.sha = UPLOAD_SHA;
    return UPLOAD_SHA;
  }

  async wait(): Promise<void> {
    this.calls.push("wait");
    if (!this.state.space) throw new Error("missing Space");
    this.state.space.runtimeStage = "RUNNING";
    this.state.space.hardware = "cpu-basic";
  }

  async pause(): Promise<void> {
    this.calls.push("pause");
    if (this.state.space) this.state.space.runtimeStage = "PAUSED";
  }

  async restart(): Promise<void> {
    this.calls.push("restart");
    if (!this.state.space) throw new Error("missing Space");
    this.state.space.runtimeStage = "BUILDING";
  }
}

async function setup(existingRevision?: string) {
  const directory = await temporaryDirectory();
  const repository = resolve(directory, "repository");
  const bundle = resolve(directory, "private", "bundle");
  const planPath = resolve(directory, "private", "plan.json");
  const hf = new FakeHf();
  const source = new FakeSource(repository);
  const identity = new FakeIdentity();
  const http = new FakeHttp();
  if (existingRevision) {
    hf.state = installedState(existingRevision, identity.principal);
  }
  const dependencies: InstallerDependencies = {
    hf,
    source,
    identity,
    http,
    environment: {
      HARBOR_HF_INSTALL_CONTROL_SECRET: "control-placeholder",
      HARBOR_HF_INSTALL_INFERENCE_SECRET: "inference-placeholder",
    },
  };
  const planned = await planInstall(
    {
      space: "example/control",
      bundleDirectory: bundle,
      planPath,
    },
    dependencies,
  );
  return {
    directory,
    bundle,
    planPath,
    hf,
    source,
    identity,
    http,
    dependencies,
    planned,
  };
}

function installedState(revision: string, principal: Principal): RemoteState {
  const variables = expectedVariables(
    "example",
    "example/control-artifacts",
    ORIGIN,
    principal.subject,
    revision,
  ) as Record<string, string>;
  return {
    namespaceListingsComplete: true,
    space: {
      id: "example/control",
      private: true,
      sdk: "docker",
      origin: ORIGIN,
      sha: UPLOAD_SHA,
      runtimeStage: "RUNNING",
      hardware: "cpu-basic",
      requestedHardware: "cpu-basic",
      variables,
      secretNames: ["HF_INFERENCE_TOKEN", "HF_TOKEN"],
    },
    bucket: { id: "example/control-artifacts", private: true },
  };
}

describe("installer workflows", () => {
  it("plans and applies a fresh protected disabled-write installation", async () => {
    const setupResult = await setup();
    const result = await applyInstall(
      {
        planPath: setupResult.planPath,
        confirmation: setupResult.planned.digest,
      },
      setupResult.dependencies,
    );
    expect(result).toEqual({
      production_ready: false,
      anonymous_live: "passed",
      anonymous_ready: "passed",
      authenticated_system: "skipped",
      source_upload_revision: "passed",
    });
    expect(setupResult.hf.calls).toContain("createSpace");
    expect(setupResult.hf.calls).toContain("createBucket");
    expect(setupResult.hf.calls).toContain("uploadMirror");
    expect(setupResult.hf.uploadedBundleDirectories[0]).not.toBe(setupResult.bundle);
    expect(setupResult.hf.state.space?.variables.HARBOR_HF_WRITE_MODE).toBe("disabled");
    expect(setupResult.hf.state.space?.variables.HARBOR_HF_PUBLIC_ORIGIN).toBe(ORIGIN);
    const planBytes = await readFile(setupResult.planPath, "utf8");
    expect(planBytes).not.toContain("control-placeholder");
    expect(planBytes).not.toContain("inference-placeholder");
    for (const path of setupResult.hf.temporaryPaths) {
      await expect(access(path)).rejects.toThrow();
    }
  });

  it("reasserts an existing installation without replacing secrets", async () => {
    const setupResult = await setup(REVISION);
    setupResult.hf.calls.length = 0;
    await applyInstall(
      {
        planPath: setupResult.planPath,
        confirmation: setupResult.planned.digest,
      },
      setupResult.dependencies,
    );
    expect(setupResult.hf.calls).toContain("pause");
    expect(setupResult.hf.calls).toContain("setProtected");
    expect(setupResult.hf.calls).toContain("setVariables");
    expect(setupResult.hf.calls).not.toContain("setSecrets");
    expect(setupResult.hf.calls).toContain("uploadMirror");
    expect(setupResult.hf.calls).toContain("restart");
    expect(setupResult.hf.calls).toContain("wait");
  });

  it("updates only the release variable and exact mirror for a marked target", async () => {
    const setupResult = await setup(OLD_REVISION);
    setupResult.hf.calls.length = 0;
    await applyInstall(
      {
        planPath: setupResult.planPath,
        confirmation: setupResult.planned.digest,
      },
      setupResult.dependencies,
    );
    expect(setupResult.hf.calls).toContain("setVariables");
    expect(setupResult.hf.calls).toContain("uploadMirror");
    expect(setupResult.hf.calls).not.toContain("setSecrets");
    expect(setupResult.hf.state.space?.variables.HARBOR_HF_SOURCE_REVISION).toBe(
      REVISION,
    );
  });

  it("uploads before changing the revision so an interrupted update stays retryable", async () => {
    const setupResult = await setup(OLD_REVISION);
    setupResult.hf.failUpload = true;
    await expect(
      applyInstall(
        {
          planPath: setupResult.planPath,
          confirmation: setupResult.planned.digest,
        },
        setupResult.dependencies,
      ),
    ).rejects.toThrow("after remote mutation began");
    expect(setupResult.hf.state.space?.variables.HARBOR_HF_SOURCE_REVISION).toBe(
      OLD_REVISION,
    );
    expect(setupResult.hf.calls).toContain("uploadMirror");
    expect(setupResult.hf.calls).not.toContain("setVariables");
    expect(setupResult.hf.calls).toContain("pause");
  });

  it("replans and resumes after a lost fresh Space create response", async () => {
    const setupResult = await setup();
    setupResult.hf.failCreateSpaceResponse = true;
    await expect(
      applyInstall(
        {
          planPath: setupResult.planPath,
          confirmation: setupResult.planned.digest,
        },
        setupResult.dependencies,
      ),
    ).rejects.toThrow("after remote mutation began");
    expect(setupResult.hf.state.space?.runtimeStage).toBe("PAUSED");

    setupResult.hf.failCreateSpaceResponse = false;
    setupResult.dependencies.environment = {};
    const recovery = await planInstall(
      {
        space: "example/control",
        bundleDirectory: resolve(setupResult.directory, "recovery", "bundle"),
        planPath: resolve(setupResult.directory, "recovery", "plan.json"),
      },
      setupResult.dependencies,
    );
    await expect(
      applyInstall(
        {
          planPath: recovery.path,
          confirmation: recovery.digest,
        },
        setupResult.dependencies,
      ),
    ).resolves.toMatchObject({ production_ready: false });
  });

  it("does not pause an unmarked Space that appears during create", async () => {
    const setupResult = await setup();
    setupResult.hf.failCreateWithUnmarkedRace = true;
    await expect(
      applyInstall(
        {
          planPath: setupResult.planPath,
          confirmation: setupResult.planned.digest,
        },
        setupResult.dependencies,
      ),
    ).rejects.toThrow("after remote mutation began");
    expect(setupResult.hf.calls).not.toContain("pause");
    expect(setupResult.hf.state.space?.variables).toEqual({});
  });

  it("replans and resumes after upload succeeds but variable update fails", async () => {
    const setupResult = await setup(OLD_REVISION);
    setupResult.hf.failSetVariablesAfterUpload = true;
    await expect(
      applyInstall(
        {
          planPath: setupResult.planPath,
          confirmation: setupResult.planned.digest,
        },
        setupResult.dependencies,
      ),
    ).rejects.toThrow("after remote mutation began");
    expect(setupResult.hf.state.space?.sha).toBe(UPLOAD_SHA);
    expect(setupResult.hf.state.space?.variables.HARBOR_HF_SOURCE_REVISION).toBe(
      OLD_REVISION,
    );
    expect(setupResult.hf.state.space?.runtimeStage).toBe("PAUSED");

    setupResult.hf.failSetVariablesAfterUpload = false;
    const recovery = await planInstall(
      {
        space: "example/control",
        bundleDirectory: resolve(setupResult.directory, "recovery", "bundle"),
        planPath: resolve(setupResult.directory, "recovery", "plan.json"),
      },
      setupResult.dependencies,
    );
    await expect(
      applyInstall(
        {
          planPath: recovery.path,
          confirmation: recovery.digest,
        },
        setupResult.dependencies,
      ),
    ).resolves.toMatchObject({ source_upload_revision: "passed" });
  });

  it("rejects unmarked targets, list failures, and post-plan drift", async () => {
    const directory = await temporaryDirectory();
    const repository = resolve(directory, "repository");
    const privateDirectory = resolve(directory, "private");
    const dependencies: InstallerDependencies = {
      hf: new FakeHf(),
      source: new FakeSource(repository),
      identity: new FakeIdentity(),
      http: new FakeHttp(),
    };
    const hf = dependencies.hf as FakeHf;
    hf.state = installedState(
      REVISION,
      (dependencies.identity as FakeIdentity).principal,
    );
    if (!hf.state.space) throw new Error("test state is missing");
    hf.state.space.variables.HARBOR_HF_INSTALLER_MARKER = "different";
    await expect(
      planInstall(
        {
          space: "example/control",
          bundleDirectory: resolve(privateDirectory, "mismatch-bundle"),
          planPath: resolve(privateDirectory, "mismatch-plan.json"),
        },
        dependencies,
      ),
    ).rejects.toThrow("installer-marked");

    hf.failObserve = true;
    await expect(
      planInstall(
        {
          space: "example/control",
          bundleDirectory: resolve(privateDirectory, "failure-bundle"),
          planPath: resolve(privateDirectory, "failure-plan.json"),
        },
        dependencies,
      ),
    ).rejects.toThrow("listing failed");

    const drift = await setup();
    drift.hf.state.bucket = {
      id: "example/control-artifacts",
      private: true,
    };
    await expect(
      applyInstall(
        { planPath: drift.planPath, confirmation: drift.planned.digest },
        drift.dependencies,
      ),
    ).rejects.toThrow("drifted");
  });

  it("requires private plan and bundle paths outside the checkout", async () => {
    const directory = await temporaryDirectory();
    const repository = resolve(directory, "repository");
    await expect(
      planInstall(
        {
          space: "example/control",
          bundleDirectory: resolve(repository, "bundle"),
          planPath: resolve(repository, "plan.json"),
        },
        {
          hf: new FakeHf(),
          source: new FakeSource(repository),
          identity: new FakeIdentity(),
          http: new FakeHttp(),
        },
      ),
    ).rejects.toThrow("outside the checkout");
  });

  it("redacts post-mutation failure, pauses, and removes temp files", async () => {
    const setupResult = await setup();
    setupResult.hf.failCreateBucket = true;
    const controlSecret = setupResult.dependencies.environment
      ?.HARBOR_HF_INSTALL_CONTROL_SECRET as string;
    let message = "";
    try {
      await applyInstall(
        {
          planPath: setupResult.planPath,
          confirmation: setupResult.planned.digest,
        },
        setupResult.dependencies,
      );
    } catch (error) {
      message = error instanceof Error ? error.message : "";
    }
    expect(message).toContain("after remote mutation began");
    expect(message).not.toContain(controlSecret);
    expect(message).not.toContain("provider detail");
    expect(setupResult.hf.calls).toContain("pause");
    for (const path of setupResult.hf.temporaryPaths) {
      await expect(access(path)).rejects.toThrow();
    }
  });

  it("requires valid distinct secret sources only when names are missing", async () => {
    const setupResult = await setup();
    setupResult.dependencies.environment = {
      HARBOR_HF_INSTALL_CONTROL_SECRET: "same-placeholder",
      HARBOR_HF_INSTALL_INFERENCE_SECRET: "same-placeholder",
    };
    await expect(
      applyInstall(
        {
          planPath: setupResult.planPath,
          confirmation: setupResult.planned.digest,
        },
        setupResult.dependencies,
      ),
    ).rejects.toThrow("distinct");

    const existing = await setup(REVISION);
    existing.dependencies.environment = {};
    await expect(
      applyInstall(
        {
          planPath: existing.planPath,
          confirmation: existing.planned.digest,
        },
        existing.dependencies,
      ),
    ).resolves.toMatchObject({ production_ready: false });
  });

  it("verifies authenticated system state only with an explicit bearer", async () => {
    const setupResult = await setup(REVISION);
    setupResult.dependencies.environment = {
      HARBOR_HF_INSTALL_VERIFY_BEARER: "verify-placeholder",
    };
    const result = await verifyInstall(setupResult.planPath, setupResult.dependencies);
    expect(result.authenticated_system).toBe("passed");
    expect(setupResult.http.requests).toContainEqual({
      path: "/api/v1/system",
      bearer: "verify-placeholder",
    });
  });

  it("fails closed on unhealthy anonymous or authenticated projections", async () => {
    const anonymous = await setup(REVISION);
    anonymous.http.readyStatus = "rebuilding";
    await expect(
      verifyInstall(anonymous.planPath, anonymous.dependencies),
    ).rejects.toThrow("readiness");

    const authenticated = await setup(REVISION);
    authenticated.dependencies.environment = {
      HARBOR_HF_INSTALL_VERIFY_BEARER: "verify-placeholder",
    };
    authenticated.http.systemIntegrityError = "integrity-placeholder";
    await expect(
      verifyInstall(authenticated.planPath, authenticated.dependencies),
    ).rejects.toThrow("system verification");
  });

  it("rejects changed bundle content and confirmation digests", async () => {
    const setupResult = await setup();
    await expect(
      applyInstall(
        {
          planPath: setupResult.planPath,
          confirmation: `sha256:${"0".repeat(64)}`,
        },
        setupResult.dependencies,
      ),
    ).rejects.toThrow("confirmation");
    await writeFile(resolve(setupResult.bundle, "Dockerfile"), "changed\n");
    await expect(
      applyInstall(
        {
          planPath: setupResult.planPath,
          confirmation: setupResult.planned.digest,
        },
        setupResult.dependencies,
      ),
    ).rejects.toThrow("bundle");
  });
});
