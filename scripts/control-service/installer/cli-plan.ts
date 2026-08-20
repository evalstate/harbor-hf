import { cliMain, defaultDependencies, formatPlanOutput, parseOptions } from "./cli.js";
import {
  activateInstallState,
  carryBootstrapReceipt,
  discardInstallState,
  findCurrentInstallPlanPath,
  installerStateRoot,
  prepareInstallState,
} from "./state.js";
import { planInstall } from "./workflow.js";

const usage =
  "Usage: npm run install:plan -- --space <namespace>/<space> [--bucket <namespace>/<bucket>] [--state-dir <dir>]\n";

await cliMain(async () => {
  const options = parseOptions(process.argv.slice(2), {
    space: { required: true },
    bucket: { required: false },
    "state-dir": { required: false },
  });
  if (options === "help") {
    process.stdout.write(usage);
    return;
  }
  const space = options.space as string;
  const stateRoot = installerStateRoot(options["state-dir"]);
  const previousPlanPath = await findCurrentInstallPlanPath(space, stateRoot);
  const prepared = await prepareInstallState(space, stateRoot);
  let activated = false;
  try {
    const result = await planInstall(
      {
        space,
        ...(options.bucket ? { bucket: options.bucket } : {}),
        bundleDirectory: prepared.bundleDirectory,
        planPath: prepared.planPath,
      },
      defaultDependencies(),
    );
    const observedPhase =
      result.plan.observed_preconditions.space?.variables.HARBOR_HF_INSTALL_PHASE;
    if (
      result.plan.observed_preconditions.bucket &&
      (observedPhase === "credentials_required" || observedPhase === "source_staged")
    ) {
      if (
        !previousPlanPath ||
        !(await carryBootstrapReceipt(
          previousPlanPath,
          result.path,
          result.plan,
          result.digest,
        ))
      ) {
        throw new Error(
          "bootstrap receipt is unavailable; the original private installer state is required",
        );
      }
    }
    await activateInstallState(prepared, space);
    activated = true;
    process.stdout.write(formatPlanOutput(result.plan, Boolean(options["state-dir"])));
  } finally {
    if (!activated) await discardInstallState(prepared);
  }
});
