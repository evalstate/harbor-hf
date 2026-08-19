import { cliMain, defaultDependencies, parseOptions } from "./cli.js";
import { applyInstall } from "./workflow.js";

const usage =
  "Usage: npm run install:apply -- --plan <file> --confirm <sha256:digest>\n";

await cliMain(async () => {
  const options = parseOptions(process.argv.slice(2), {
    plan: { required: true },
    confirm: { required: true },
  });
  if (options === "help") {
    process.stdout.write(usage);
    return;
  }
  const result = await applyInstall(
    {
      planPath: options.plan as string,
      confirmation: options.confirm as string,
    },
    defaultDependencies(),
  );
  process.stdout.write(`${JSON.stringify(result)}\n`);
});
