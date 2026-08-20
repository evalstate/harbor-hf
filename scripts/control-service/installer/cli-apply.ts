import {
  cliMain,
  defaultDependencies,
  formatApplyOutput,
  parseApplyOptions,
} from "./cli.js";
import {
  findCurrentInstallPlanPath,
  installerStateRoot,
  readBootstrapReceipt,
  withInstallerStateLock,
  writeBootstrapReceipt,
} from "./state.js";
import { applyInstall } from "./workflow.js";

const usage =
  "Usage: npm run install:apply -- --space <namespace>/<space> [--state-dir <dir>] [--replace-credentials]\n";

await cliMain(async () => {
  const options = parseApplyOptions(process.argv.slice(2));
  if (options === "help") {
    process.stdout.write(usage);
    return;
  }
  const stateRoot = installerStateRoot(options.stateDirectory);
  await withInstallerStateLock(options.space, stateRoot, async () => {
    const planPath = await findCurrentInstallPlanPath(options.space, stateRoot);
    if (!planPath) {
      throw new Error("no supported current install plan is available");
    }
    const bootstrapReceipt = await readBootstrapReceipt(planPath);
    const result = await applyInstall(
      {
        planPath,
        ...(bootstrapReceipt ? { bootstrapReceipt } : {}),
        replaceCredentials: options.replaceCredentials,
        persistBootstrapReceipt: async (receipt) =>
          await writeBootstrapReceipt(planPath, receipt),
      },
      defaultDependencies(),
    );
    if (result.status === "credentials_required") {
      await writeBootstrapReceipt(planPath, result.receipt);
    }
    process.stdout.write(
      formatApplyOutput(options.space, result, Boolean(options.stateDirectory)),
    );
  });
});
