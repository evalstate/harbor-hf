import { describe, expect, it } from "vitest";
import { profile } from "@harbor-hf/test-fixtures";
import { ProfileResolver } from "../src/profiles.js";

describe("ProfileResolver", () => {
  it("keeps the deployed profile when a promotion is a stale digest of the same name", () => {
    const current = profile("deployment", "tb21-providers", {
      route: "hf_job",
      worker_revision: "current-revision",
    });
    const stale = profile("deployment", "tb21-providers", {
      route: "hf_job",
      worker_revision: "stale-revision",
    });
    const resolver = new ProfileResolver([current]);
    resolver.replacePromotedProfiles([{ ...stale, alias: "tb21-providers" }]);
    expect(resolver.get("deployment", "tb21-providers").profile_id).toBe(
      current.profile_id,
    );
    expect(resolver.get("deployment", "tb21-providers").profile.spec).toMatchObject({
      worker_revision: "current-revision",
    });
  });

  it("keeps a promotion that remaps a checked-in name to a different profile", () => {
    const builtIn = profile("model", "control-smoke", {
      model_id: "built-in",
      revision: "a",
    });
    const remapped = profile("model", "durable-model", {
      model_id: "durable",
      revision: "b",
    });
    const resolver = new ProfileResolver([builtIn]);
    resolver.replacePromotedProfiles([{ ...remapped, alias: "control-smoke" }]);
    expect(resolver.get("model", "control-smoke").profile_id).toBe(remapped.profile_id);
  });

  it("keeps a promotion that only exists as an extra alias", () => {
    const builtIn = profile("harness", "opencode", { agent: "opencode" });
    const extra = profile("harness", "opencode-canary", { agent: "opencode" });
    const resolver = new ProfileResolver([builtIn]);
    resolver.replacePromotedProfiles([{ ...extra, alias: "opencode-canary" }]);
    expect(resolver.get("harness", "opencode").profile_id).toBe(builtIn.profile_id);
    expect(resolver.get("harness", "opencode-canary").profile_id).toBe(
      extra.profile_id,
    );
  });

  it("resolves distinct command recipes by alias without an agent-name branch", () => {
    const first = profile("harness", "first-command-recipe", {
      agent: "command-agent",
      revision: "sha256:first",
    });
    const second = profile("harness", "second-command-recipe", {
      agent: "command-agent",
      revision: "sha256:second",
    });
    const resolver = new ProfileResolver([first, second]);

    expect(resolver.get("harness", "first-command-recipe").profile.spec.revision).toBe(
      "sha256:first",
    );
    expect(resolver.get("harness", "second-command-recipe").profile.spec.revision).toBe(
      "sha256:second",
    );
  });
});
