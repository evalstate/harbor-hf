import {
  cliMain,
  defaultDependencies,
  formatActivationOutput,
  parseActivationOptions,
} from "./cli.js";
import {
  currentInstallPlanPath,
  installerStateRoot,
  readBootstrapReceipt,
  withInstallerStateLock,
} from "./state.js";
import { activateInstall } from "./workflow.js";

const usage =
  "Usage: npm run install:activate -- --space <namespace>/<space> --to <canary|disabled> --confirm-space <namespace>/<space> [--state-dir <dir>]\n";

await cliMain(async () => {
  const options = parseActivationOptions(process.argv.slice(2));
  if (options === "help") {
    process.stdout.write(usage);
    return;
  }
  const stateRoot = installerStateRoot(options.stateDirectory);
  await withInstallerStateLock(options.space, stateRoot, async () => {
    const planPath = await currentInstallPlanPath(options.space, stateRoot);
    const receipt = await readBootstrapReceipt(planPath);
    const result = await activateInstall(
      {
        planPath,
        ...(receipt ? { bootstrapReceipt: receipt } : {}),
        confirmSpace: options.confirmSpace,
        to: options.to,
      },
      defaultDependencies(),
    );
    process.stdout.write(formatActivationOutput(options.space, result));
  });
});
