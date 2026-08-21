import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

describe("package-lock registry URLs", () => {
  it("pins tarballs to registry.npmjs.org so Space image builds do not use the Hugging Face npm mirror", () => {
    const lockfile = readFileSync(
      resolve(import.meta.dirname, "../../package-lock.json"),
      "utf8",
    );
    expect(lockfile).not.toContain("npm.registries.huggingface.tech");
    expect(lockfile).toContain("https://registry.npmjs.org/");
  });
});
