import { chmod, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { fastAgentWorkbenchStarter } from "@harbor-hf/control-core";
import { WorkbenchRuntime } from "../src/workbench.js";

describe.sequential("local Workbench runner", () => {
  const originalPath = process.env.PATH;

  afterEach(() => {
    process.env.PATH = originalPath;
  });

  it("does not report completion before file inventory is ready", async () => {
    const bin = await mkdtemp(join(tmpdir(), "harbor-hf-fake-docker-"));
    const docker = join(bin, "docker");
    await writeFile(
      docker,
      `#!/bin/sh
workspace=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--volume" ]; then
    shift
    case "$1" in
      *:/workspace:rw) workspace="\${1%:/workspace:rw}" ;;
    esac
  fi
  shift
done
mkdir -p "$workspace/generated"
i=0
while [ "$i" -lt 1000 ]; do
  printf 'file %s\\n' "$i" > "$workspace/generated/file-$i.txt"
  i=$((i + 1))
done
printf 'fake setup complete\\n'
`,
      { mode: 0o700 },
    );
    await chmod(docker, 0o700);
    process.env.PATH = `${bin}:${originalPath ?? ""}`;

    const runtime = new WorkbenchRuntime("docker", "unused:test-image");
    try {
      const started = await runtime.startSetup(
        fastAgentWorkbenchStarter,
        "test-operator",
        "inventory-ready",
      );
      let current = started;
      while (["queued", "running"].includes(current.status)) {
        await new Promise((resolve) => setTimeout(resolve, 1));
        current = runtime.getSetup(
          started.setup_test_id,
          "test-operator",
        ) as typeof current;
      }

      expect(current.status).toBe("passed");
      expect(current.files).toHaveLength(1000);
      expect(current.files[0]?.path).toMatch(/^generated\/file-\d+\.txt$/);
    } finally {
      await runtime.close();
    }
  });
});
