import {
  cliMain,
  defaultDependencies,
  formatApplyOutput,
  parseSavedPlanOptions,
} from "./cli.js";
import { currentInstallPlanPath, installerStateRoot } from "./state.js";
import { applyInstall } from "./workflow.js";

const usage =
  "Usage: npm run install:apply -- --space <namespace>/<space> [--state-dir <dir>]\n";

await cliMain(async () => {
  const options = parseSavedPlanOptions(process.argv.slice(2));
  if (options === "help") {
    process.stdout.write(usage);
    return;
  }
  const planPath = await currentInstallPlanPath(
    options.space,
    installerStateRoot(options.stateDirectory),
  );
  const result = await applyInstall(
    {
      planPath,
    },
    defaultDependencies(),
  );
  process.stdout.write(formatApplyOutput(options.space, result));
});
