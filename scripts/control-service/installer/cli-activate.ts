import {
  cliMain,
  defaultDependencies,
  formatActivationOutput,
  parseConfirmationOptions,
} from "./cli.js";
import { locateGitRepositoryRoot } from "./source.js";
import {
  assertInstallerStateOutsideRepository,
  currentInstallPlanPath,
  installerStateRoot,
  readBootstrapReceipt,
  withInstallerStateLock,
} from "./state.js";
import { activateInstall } from "./workflow.js";

const usage =
  "Usage: npm run install:activate -- --space <namespace>/<space> --confirm-space <namespace>/<space> [--state-dir <dir>]\n";

await cliMain(async () => {
  const options = parseConfirmationOptions(process.argv.slice(2));
  if (options === "help") {
    process.stdout.write(usage);
    return;
  }
  const stateRoot = await assertInstallerStateOutsideRepository(
    installerStateRoot(options.stateDirectory),
    await locateGitRepositoryRoot(),
  );
  const dependencies = defaultDependencies();
  await withInstallerStateLock(options.space, stateRoot, async () => {
    const planPath = await currentInstallPlanPath(options.space, stateRoot);
    const receipt = await readBootstrapReceipt(planPath);
    const result = await activateInstall(
      {
        planPath,
        ...(receipt ? { bootstrapReceipt: receipt } : {}),
        confirmSpace: options.confirmSpace,
      },
      dependencies,
    );
    process.stdout.write(formatActivationOutput(options.space, result));
  });
});
