import { spawnSync } from "node:child_process";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

function run(command: string, args: string[]): string {
  const result = spawnSync(command, args, { encoding: "utf8" });
  if (result.status !== 0)
    throw new Error(
      `${command} failed: ${result.stderr.trim() || result.stdout.trim()}`,
    );
  return result.stdout.trim();
}

const spaceId = process.argv[2] ?? process.env.HARBOR_HF_SPACE_ID;
if (!spaceId) throw new Error("usage: npm run deploy:space -- <space-id>");
const bundle = await mkdtemp(join(tmpdir(), "harbor-hf-space-"));
run("npm", ["run", "bundle:space", "--", bundle]);
const release = JSON.parse(await readFile(join(bundle, "RELEASE.json"), "utf8")) as {
  source_revision: string;
};
const upload = spawnSync(
  "hf",
  [
    "upload",
    spaceId,
    bundle,
    ".",
    "--type",
    "space",
    "--delete",
    "*",
    "--commit-message",
    `deploy: ${release.source_revision}`,
    "--format",
    "quiet",
  ],
  { encoding: "utf8" },
);
if (upload.status !== 0) {
  const detail = (upload.stderr.trim() || upload.stdout.trim()).replace(
    /https?:\S+/g,
    "<redacted>",
  );
  throw new Error(`hf upload failed: ${detail}`);
}
console.log(JSON.stringify({ source_revision: release.source_revision }));
