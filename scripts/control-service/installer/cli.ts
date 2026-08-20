import { HfCli } from "./hf.js";
import { BoundedHttpAdapter } from "./http.js";
import { StableIdentityAdapter } from "./identity.js";
import type { InstallPlan } from "./model.js";
import { BoundedJsonProcess } from "./process.js";
import { TtyInstallerSecretInput } from "./secret-input.js";
import { GitSourceAdapter } from "./source.js";
import type { InstallerDependencies, VerificationResult } from "./workflow.js";

export function defaultDependencies(): InstallerDependencies {
  const hf = new HfCli(new BoundedJsonProcess());
  const http = new BoundedHttpAdapter();
  return {
    hf,
    http,
    identity: new StableIdentityAdapter(hf, http),
    secretInput: new TtyInstallerSecretInput(),
    source: new GitSourceAdapter(),
  };
}

export function parseOptions(
  args: readonly string[],
  specification: Record<string, { required: boolean }>,
): Record<string, string> | "help" {
  if (args.includes("--help")) return "help";
  const output: Record<string, string> = {};
  for (let index = 0; index < args.length; index += 2) {
    const key = args[index];
    const value = args[index + 1];
    if (!key?.startsWith("--") || !value || value.startsWith("--")) {
      throw new Error("invalid command arguments");
    }
    const name = key.slice(2);
    if (!(name in specification) || name in output) {
      throw new Error("invalid command arguments");
    }
    output[name] = value;
  }
  for (const [name, option] of Object.entries(specification)) {
    if (option.required && !output[name]) {
      throw new Error(`missing --${name}`);
    }
  }
  return output;
}

export function parseSavedPlanOptions(
  args: readonly string[],
): { space: string; stateDirectory?: string } | "help" {
  const options = parseOptions(args, {
    space: { required: true },
    "state-dir": { required: false },
  });
  if (options === "help") return options;
  return {
    space: options.space as string,
    ...(options["state-dir"] ? { stateDirectory: options["state-dir"] as string } : {}),
  };
}

export function formatPlanOutput(
  plan: InstallPlan,
  customStateDirectory = false,
): string {
  const observed = plan.observed_preconditions;
  const action = !observed.space
    ? "create Space and Bucket"
    : observed.bucket
      ? "update installer-managed Space"
      : "update Space and create Bucket";
  return [
    `Space:      ${plan.targets.space_id}`,
    `Bucket:     ${plan.targets.bucket_id}`,
    "Access:     protected",
    "Hardware:   cpu-basic",
    "Write mode: disabled",
    `Action:     ${action}`,
    "",
    "Plan saved privately.",
    customStateDirectory
      ? "Next: rerun install:apply with this Space and the same --state-dir."
      : `Next: npm run install:apply -- --space ${plan.targets.space_id}`,
    "",
  ].join("\n");
}

export function formatApplyOutput(spaceId: string, result: VerificationResult): string {
  return [
    "Installation verified.",
    `Space: ${spaceId}`,
    `Anonymous health: ${result.anonymous_live}`,
    `Authenticated system: ${result.authenticated_system}`,
    `Source upload: ${result.source_upload_revision}`,
    "Write mode: disabled",
    "Production ready: no",
    "",
    "Review access and credential scopes before activation.",
    "",
  ].join("\n");
}

export async function cliMain(action: () => Promise<void>): Promise<void> {
  try {
    await action();
  } catch (error) {
    const message = error instanceof Error ? error.message : "installer failed";
    process.stderr.write(`error: ${message}\n`);
    process.exitCode = 1;
  }
}
