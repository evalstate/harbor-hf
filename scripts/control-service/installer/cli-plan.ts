import { cliMain, defaultDependencies, formatPlanOutput, parseOptions } from "./cli.js";
import { planInstall } from "./workflow.js";

const usage =
  "Usage: npm run install:plan -- --space <namespace>/<space> [--bucket <namespace>/<bucket>] --bundle <dir> --plan <file>\n";

await cliMain(async () => {
  const options = parseOptions(process.argv.slice(2), {
    space: { required: true },
    bucket: { required: false },
    bundle: { required: true },
    plan: { required: true },
  });
  if (options === "help") {
    process.stdout.write(usage);
    return;
  }
  const result = await planInstall(
    {
      space: options.space as string,
      ...(options.bucket ? { bucket: options.bucket } : {}),
      bundleDirectory: options.bundle as string,
      planPath: options.plan as string,
    },
    defaultDependencies(),
  );
  process.stdout.write(formatPlanOutput(result.path));
});
