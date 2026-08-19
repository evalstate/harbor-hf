import { cliMain, defaultDependencies, parseOptions } from "./cli.js";
import { verifyInstall } from "./workflow.js";

const usage = "Usage: npm run install:verify -- --plan <file>\n";

await cliMain(async () => {
  const options = parseOptions(process.argv.slice(2), {
    plan: { required: true },
  });
  if (options === "help") {
    process.stdout.write(usage);
    return;
  }
  const result = await verifyInstall(options.plan as string, defaultDependencies());
  process.stdout.write(`${JSON.stringify(result)}\n`);
});
