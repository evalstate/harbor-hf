import { HfCli } from "./hf.js";
import { BoundedHttpAdapter } from "./http.js";
import { StableIdentityAdapter } from "./identity.js";
import { BoundedJsonProcess } from "./process.js";
import { GitSourceAdapter } from "./source.js";
import type { InstallerDependencies } from "./workflow.js";

export function defaultDependencies(): InstallerDependencies {
  const hf = new HfCli(new BoundedJsonProcess());
  const http = new BoundedHttpAdapter();
  return {
    hf,
    http,
    identity: new StableIdentityAdapter(hf, http),
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

export async function cliMain(action: () => Promise<void>): Promise<void> {
  try {
    await action();
  } catch (error) {
    const message = error instanceof Error ? error.message : "installer failed";
    process.stderr.write(`error: ${message}\n`);
    process.exitCode = 1;
  }
}
