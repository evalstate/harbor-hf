import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import {
  chmod,
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, relative, resolve } from "node:path";
import type { AgentWorkbenchRecipeV1 } from "@harbor-hf/contracts";
import { deterministicId, sha256 } from "@harbor-hf/contracts";
import {
  compileAgentWorkbenchRecipe,
  type AgentWorkbenchPreview,
  workbenchRuntimeValues,
} from "@harbor-hf/control-core";

const MAX_LOG_BYTES = 2 * 1024 * 1024;
const MAX_FILES = 1_000;
const MAX_PREVIEW_BYTES = 64 * 1024;

export interface WorkbenchFile {
  file_id: string;
  path: string;
  root: "workspace" | "logs";
  size: number;
  text: boolean;
}

export interface WorkbenchSetupView {
  setup_test_id: string;
  recipe_digest: string;
  revision_id: string;
  status: "queued" | "running" | "passed" | "failed" | "timed-out";
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  exit_code: number | null;
  error: string | null;
  files: WorkbenchFile[];
}

interface SetupState extends WorkbenchSetupView {
  owner: string;
  stdout: string;
  stderr: string;
  directory: string;
  process: ChildProcess | null;
  container_name: string;
  filePaths: Map<string, string>;
}

function appendBounded(current: string, chunk: Buffer): string {
  const next = current + chunk.toString("utf8");
  if (Buffer.byteLength(next) <= MAX_LOG_BYTES) return next;
  const bytes = Buffer.from(next);
  return `[earlier output truncated]\n${bytes.subarray(bytes.length - MAX_LOG_BYTES).toString("utf8")}`;
}

function setupEnvironment(recipe: AgentWorkbenchRecipeV1): Record<string, string> {
  const values: Record<string, string> = {
    workspace_path: "/workspace",
    logs_path: "/logs",
    agent_home: "/agent-home",
    model_name: workbenchRuntimeValues.model_name,
  };
  const environment: Record<string, string> = {};
  for (const binding of recipe.environment) {
    if (
      ["instruction_path", "model_base_url", "model_api_key"].includes(binding.source)
    )
      continue;
    const value =
      binding.source === "literal" ? (binding.value ?? "") : values[binding.source];
    if (value === undefined)
      throw new Error(`workbench binding ${binding.source} is unavailable`);
    environment[binding.name] = value;
  }
  return environment;
}

async function scanRoot(
  state: SetupState,
  rootName: "workspace" | "logs",
  root: string,
): Promise<void> {
  const entries = await readdir(root, { recursive: true, withFileTypes: true });
  for (const entry of entries) {
    if (state.files.length >= MAX_FILES) break;
    const parent = entry.parentPath;
    const absolute = join(parent, entry.name);
    const metadata = await lstat(absolute);
    if (!metadata.isFile() || metadata.isSymbolicLink()) continue;
    const path = relative(root, absolute).replaceAll("\\", "/");
    if (!path || path.startsWith("../") || path.includes("/../")) continue;
    const prefix = rootName === "workspace" ? "workspace" : "logs";
    const fileId = deterministicId("workbench-file", state.setup_test_id, prefix, path);
    let text = false;
    if (metadata.size <= MAX_PREVIEW_BYTES) {
      const bytes = await readFile(absolute);
      text = !bytes.includes(0);
    }
    state.filePaths.set(fileId, absolute);
    state.files.push({
      file_id: fileId,
      path,
      root: rootName,
      size: metadata.size,
      text,
    });
  }
  state.files.sort(
    (left, right) =>
      left.root.localeCompare(right.root) || left.path.localeCompare(right.path),
  );
}

export class WorkbenchRuntime {
  private readonly setupTests = new Map<string, SetupState>();
  private root: string | null = null;

  constructor(
    private readonly mode: "disabled" | "docker",
    private readonly image: string,
  ) {}

  preview(value: unknown): AgentWorkbenchPreview {
    return compileAgentWorkbenchRecipe(value);
  }

  private async workbenchRoot(): Promise<string> {
    this.root ??= await mkdtemp(join(tmpdir(), "harbor-hf-workbench-"));
    return this.root;
  }

  async startSetup(
    value: unknown,
    owner: string,
    idempotencyKey: string,
  ): Promise<WorkbenchSetupView> {
    if (this.mode !== "docker") throw new Error("local setup testing is not enabled");
    const preview = this.preview(value);
    const setupTestId = deterministicId(
      "setup-test",
      owner,
      preview.recipe_digest,
      sha256(idempotencyKey),
    );
    const existing = this.setupTests.get(setupTestId);
    if (existing) return this.view(existing);
    const root = await this.workbenchRoot();
    const directory = resolve(root, setupTestId);
    const workspace = join(directory, "workspace");
    const logs = join(directory, "logs");
    const agentHome = join(directory, "agent-home");
    const recipeDirectory = join(directory, "recipe");
    await Promise.all([
      mkdir(workspace, { recursive: true }),
      mkdir(logs, { recursive: true }),
      mkdir(agentHome, { recursive: true }),
      mkdir(recipeDirectory, { recursive: true }),
    ]);
    await Promise.all([
      chmod(workspace, 0o777),
      chmod(logs, 0o777),
      chmod(agentHome, 0o777),
    ]);
    const setupScript = join(recipeDirectory, "setup.sh");
    await writeFile(
      setupScript,
      `#!/bin/sh\nset -eu\n${preview.recipe.setup_command}\n`,
      { mode: 0o700 },
    );
    const createdAt = new Date().toISOString();
    const containerName = `hhf-${setupTestId.slice(-20)}`;
    const state: SetupState = {
      setup_test_id: setupTestId,
      recipe_digest: preview.recipe_digest,
      revision_id: preview.revision_id,
      status: "queued",
      created_at: createdAt,
      started_at: null,
      completed_at: null,
      exit_code: null,
      error: null,
      files: [],
      owner,
      stdout: "",
      stderr: "",
      directory,
      process: null,
      container_name: containerName,
      filePaths: new Map(),
    };
    this.setupTests.set(setupTestId, state);
    const args = [
      "run",
      "--rm",
      "--name",
      containerName,
      "--network",
      "bridge",
      "--cpus",
      "2",
      "--memory",
      "4g",
      "--pids-limit",
      "1024",
      "--cap-drop",
      "ALL",
      "--security-opt",
      "no-new-privileges",
      "--user",
      "1000:1000",
      "--tmpfs",
      "/tmp:rw,noexec,nosuid,size=512m",
      "--volume",
      `${workspace}:/workspace:rw`,
      "--volume",
      `${logs}:/logs:rw`,
      "--volume",
      `${agentHome}:/agent-home:rw`,
      "--volume",
      `${setupScript}:/recipe/setup.sh:ro`,
      "--workdir",
      "/workspace",
      "--env",
      "HOME=/agent-home",
      "--env",
      "PATH=/agent-home/venv/bin:/usr/local/bin:/usr/bin:/bin",
    ];
    for (const [name, value] of Object.entries(setupEnvironment(preview.recipe)))
      args.push("--env", `${name}=${value}`);
    args.push(this.image, "/bin/sh", "/recipe/setup.sh");
    const child = spawn("docker", args, {
      stdio: ["ignore", "pipe", "pipe"],
      env: {
        HOME: process.env.HOME ?? "/tmp",
        PATH: process.env.PATH ?? "/usr/bin:/bin",
      },
    });
    state.process = child;
    state.status = "running";
    state.started_at = new Date().toISOString();
    child.stdout.on("data", (chunk: Buffer) => {
      state.stdout = appendBounded(state.stdout, chunk);
    });
    child.stderr.on("data", (chunk: Buffer) => {
      state.stderr = appendBounded(state.stderr, chunk);
    });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      spawnSync("docker", ["kill", containerName], { stdio: "ignore" });
    }, preview.recipe.setup_timeout_seconds * 1000);
    child.once("error", (error) => {
      clearTimeout(timer);
      state.status = "failed";
      state.error = error.message;
      state.completed_at = new Date().toISOString();
      state.process = null;
    });
    child.once("close", (code) => {
      clearTimeout(timer);
      state.exit_code = code;
      state.process = null;
      void Promise.all([
        scanRoot(state, "workspace", workspace),
        scanRoot(state, "logs", logs),
      ])
        .then(() => {
          state.status = timedOut ? "timed-out" : code === 0 ? "passed" : "failed";
          state.completed_at = new Date().toISOString();
        })
        .catch((error: unknown) => {
          state.error =
            error instanceof Error ? error.message : "could not inventory setup files";
          state.status = "failed";
          state.completed_at = new Date().toISOString();
        });
    });
    return this.view(state);
  }

  getSetup(setupTestId: string, owner: string): WorkbenchSetupView | null {
    const state = this.setupTests.get(setupTestId);
    if (!state || state.owner !== owner) return null;
    return this.view(state);
  }

  logs(setupTestId: string, owner: string): { stdout: string; stderr: string } | null {
    const state = this.setupTests.get(setupTestId);
    if (!state || state.owner !== owner) return null;
    return { stdout: state.stdout, stderr: state.stderr };
  }

  async file(
    setupTestId: string,
    fileId: string,
    owner: string,
  ): Promise<{ content: string; truncated: boolean } | null> {
    const state = this.setupTests.get(setupTestId);
    const path = state?.filePaths.get(fileId);
    if (!state || state.owner !== owner || !path) return null;
    const bytes = await readFile(path);
    const selected = bytes.subarray(0, MAX_PREVIEW_BYTES);
    return {
      content: selected.toString("utf8"),
      truncated: bytes.length > selected.length,
    };
  }

  private view(state: SetupState): WorkbenchSetupView {
    return {
      setup_test_id: state.setup_test_id,
      recipe_digest: state.recipe_digest,
      revision_id: state.revision_id,
      status: state.status,
      created_at: state.created_at,
      started_at: state.started_at,
      completed_at: state.completed_at,
      exit_code: state.exit_code,
      error: state.error,
      files: [...state.files],
    };
  }

  async close(): Promise<void> {
    for (const state of this.setupTests.values()) {
      if (state.process) {
        spawnSync("docker", ["kill", state.container_name], { stdio: "ignore" });
        state.process.kill();
      }
    }
    if (this.root) await rm(this.root, { recursive: true, force: true });
    this.root = null;
  }
}
