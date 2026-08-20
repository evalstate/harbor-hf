import { cliMain, defaultDependencies, formatPlanOutput, parseOptions } from "./cli.js";
import {
  activateInstallState,
  discardInstallState,
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
  const prepared = await prepareInstallState(
    space,
    installerStateRoot(options["state-dir"]),
  );
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
    await activateInstallState(prepared, space);
    activated = true;
    process.stdout.write(formatPlanOutput(result.plan, Boolean(options["state-dir"])));
  } finally {
    if (!activated) await discardInstallState(prepared);
  }
});
