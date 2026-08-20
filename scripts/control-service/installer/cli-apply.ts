import { cliMain, defaultDependencies, parseApplyOptions } from "./cli.js";
import { applyInstall } from "./workflow.js";

const usage = "Usage: npm run install:apply -- --plan <file>\n";

await cliMain(async () => {
  const options = parseApplyOptions(process.argv.slice(2));
  if (options === "help") {
    process.stdout.write(usage);
    return;
  }
  const result = await applyInstall(
    {
      planPath: options.plan,
    },
    defaultDependencies(),
  );
  process.stdout.write(`${JSON.stringify(result)}\n`);
});
