import { O_NOFOLLOW, O_RDONLY } from "node:constants";
import { createHash, randomUUID } from "node:crypto";
import { chmod, lstat, mkdir, mkdtemp, open, rename, rm, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, relative, resolve } from "node:path";
import { canonicalJson } from "./canonical.js";
import { parseTargetIds, readPrivatePlan } from "./model.js";

const STATE_SCHEMA = "harbor-hf.install-state.v1";
const POINTER_BYTES_LIMIT = 16 * 1024;

interface StatePointer {
  schema_version: typeof STATE_SCHEMA;
  space_id: string;
  generation: string;
}

export interface PreparedInstallState {
  stateRoot: string;
  targetDirectory: string;
  generationDirectory: string;
  bundleDirectory: string;
  planPath: string;
}

function targetKey(spaceId: string): string {
  return createHash("sha256").update(spaceId).digest("hex");
}

function currentUid(): number | undefined {
  return process.getuid?.();
}

function assertOwned(info: { uid: number }, label: string): void {
  const uid = currentUid();
  if (uid !== undefined && info.uid !== uid) {
    throw new Error(`${label} is not owned by the current user`);
  }
}

async function ensurePrivateDirectory(path: string): Promise<void> {
  await mkdir(path, { recursive: true, mode: 0o700 });
  const info = await lstat(path);
  if (info.isSymbolicLink() || !info.isDirectory()) {
    throw new Error("installer state path must be a non-symlink directory");
  }
  assertOwned(info, "installer state directory");
  await chmod(path, 0o700);
  const finished = await stat(path);
  if (!finished.isDirectory() || (finished.mode & 0o777) !== 0o700) {
    throw new Error("installer state directory must be owner-only");
  }
}

async function requirePrivateDirectory(path: string): Promise<void> {
  const info = await lstat(path);
  if (info.isSymbolicLink() || !info.isDirectory() || (info.mode & 0o777) !== 0o700) {
    throw new Error("installer state directory must be owner-only");
  }
  assertOwned(info, "installer state directory");
}

export function installerStateRoot(
  override: string | undefined,
  environment: NodeJS.ProcessEnv = process.env,
  home: string = homedir(),
): string {
  if (override) {
    if (!isAbsolute(override)) {
      throw new Error("installer state override must be an absolute path");
    }
    return resolve(override);
  }
  const xdgStateHome = environment.XDG_STATE_HOME;
  if (xdgStateHome) {
    if (!isAbsolute(xdgStateHome)) {
      throw new Error("XDG_STATE_HOME must be an absolute path");
    }
    return resolve(xdgStateHome, "harbor-hf", "install");
  }
  if (!isAbsolute(home)) throw new Error("user home directory must be absolute");
  return resolve(home, ".local", "state", "harbor-hf", "install");
}

function targetDirectory(stateRoot: string, spaceId: string): string {
  return resolve(stateRoot, targetKey(spaceId));
}

export async function prepareInstallState(
  spaceId: string,
  stateRootInput: string,
): Promise<PreparedInstallState> {
  parseTargetIds(spaceId);
  const stateRoot = resolve(stateRootInput);
  await ensurePrivateDirectory(stateRoot);
  const target = targetDirectory(stateRoot, spaceId);
  await ensurePrivateDirectory(target);
  const generation = await mkdtemp(resolve(target, "plan-"));
  await chmod(generation, 0o700);
  return {
    stateRoot,
    targetDirectory: target,
    generationDirectory: generation,
    bundleDirectory: resolve(generation, "bundle"),
    planPath: resolve(generation, "plan.json"),
  };
}

async function writePointer(path: string, pointer: StatePointer): Promise<void> {
  const temporaryPath = resolve(
    dirname(path),
    `.current-${randomUUID().replaceAll("-", "")}.json`,
  );
  const bytes = `${canonicalJson(pointer)}\n`;
  const handle = await open(temporaryPath, "wx", 0o600);
  try {
    await handle.writeFile(bytes, "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }
  try {
    await rename(temporaryPath, path);
  } finally {
    await rm(temporaryPath, { force: true });
  }
}

export async function activateInstallState(
  prepared: PreparedInstallState,
  spaceId: string,
): Promise<void> {
  const expectedTarget = targetDirectory(resolve(prepared.stateRoot), spaceId);
  if (
    prepared.targetDirectory !== expectedTarget ||
    dirname(prepared.generationDirectory) !== expectedTarget ||
    prepared.bundleDirectory !== resolve(prepared.generationDirectory, "bundle") ||
    prepared.planPath !== resolve(prepared.generationDirectory, "plan.json")
  ) {
    throw new Error("prepared installer state does not match its target");
  }
  const loaded = await readPrivatePlan(prepared.planPath);
  if (loaded.plan.targets.space_id !== spaceId) {
    throw new Error("saved plan target does not match installer state");
  }
  const generation = basename(prepared.generationDirectory);
  if (!/^plan-[A-Za-z0-9]{6}$/.test(generation)) {
    throw new Error("installer plan generation is invalid");
  }
  await writePointer(resolve(prepared.targetDirectory, "current.json"), {
    schema_version: STATE_SCHEMA,
    space_id: spaceId,
    generation,
  });
}

export async function discardInstallState(
  prepared: PreparedInstallState,
): Promise<void> {
  await rm(prepared.generationDirectory, { recursive: true, force: true });
}

function parsePointer(value: unknown): StatePointer {
  if (
    typeof value !== "object" ||
    value === null ||
    Array.isArray(value) ||
    Object.keys(value).sort().join(",") !== "generation,schema_version,space_id"
  ) {
    throw new Error("installer state pointer is invalid");
  }
  const record = value as Record<string, unknown>;
  if (
    record.schema_version !== STATE_SCHEMA ||
    typeof record.space_id !== "string" ||
    typeof record.generation !== "string" ||
    !/^plan-[A-Za-z0-9]{6}$/.test(record.generation)
  ) {
    throw new Error("installer state pointer is invalid");
  }
  parseTargetIds(record.space_id);
  return {
    schema_version: STATE_SCHEMA,
    space_id: record.space_id,
    generation: record.generation,
  };
}

async function readPointer(path: string): Promise<StatePointer> {
  const info = await lstat(path);
  if (
    info.isSymbolicLink() ||
    !info.isFile() ||
    (info.mode & 0o777) !== 0o600 ||
    info.size > POINTER_BYTES_LIMIT
  ) {
    throw new Error("installer state pointer must be owner-only");
  }
  assertOwned(info, "installer state pointer");
  const handle = await open(path, O_RDONLY | O_NOFOLLOW);
  let bytes: string;
  try {
    const opened = await handle.stat();
    if (
      !opened.isFile() ||
      opened.uid !== info.uid ||
      opened.dev !== info.dev ||
      opened.ino !== info.ino ||
      opened.size !== info.size ||
      (opened.mode & 0o777) !== 0o600
    ) {
      throw new Error("installer state pointer changed while opening");
    }
    bytes = await handle.readFile("utf8");
  } finally {
    await handle.close();
  }
  let value: unknown;
  try {
    value = JSON.parse(bytes);
  } catch {
    throw new Error("installer state pointer is not valid JSON");
  }
  return parsePointer(value);
}

export async function currentInstallPlanPath(
  spaceId: string,
  stateRootInput: string,
): Promise<string> {
  parseTargetIds(spaceId);
  const stateRoot = resolve(stateRootInput);
  await requirePrivateDirectory(stateRoot);
  const target = targetDirectory(stateRoot, spaceId);
  await requirePrivateDirectory(target);
  const pointer = await readPointer(resolve(target, "current.json"));
  if (pointer.space_id !== spaceId) {
    throw new Error("installer state target does not match the requested Space");
  }
  const generation = resolve(target, pointer.generation);
  const pathFromTarget = relative(target, generation);
  if (pathFromTarget !== pointer.generation) {
    throw new Error("installer state generation escapes its target");
  }
  await requirePrivateDirectory(generation);
  const planPath = resolve(generation, "plan.json");
  const loaded = await readPrivatePlan(planPath);
  if (loaded.plan.targets.space_id !== spaceId) {
    throw new Error("saved plan target does not match the requested Space");
  }
  return planPath;
}
