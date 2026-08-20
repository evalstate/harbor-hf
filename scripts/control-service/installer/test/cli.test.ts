import { describe, expect, it } from "vitest";
import { formatApplyOutput, formatPlanOutput, parseSavedPlanOptions } from "../cli.js";
import { expectedVariables, type InstallPlan, manifestDigest } from "../model.js";

function plan(): InstallPlan {
  const revision = "a".repeat(40);
  return {
    schema_version: "harbor-hf.install-plan.v1",
    production_ready: false,
    source: {
      revision,
      repository_root: "/repository-placeholder",
    },
    bundle: {
      directory: "/state-placeholder/bundle",
      manifest: [],
      manifest_digest: manifestDigest([]),
    },
    hf_cli_version: "1.23.0",
    targets: {
      namespace: "example",
      space_id: "example/control",
      bucket_id: "example/control-artifacts",
    },
    principal: {
      subject: "stable-subject",
      username: "example-user",
      organizations: [],
    },
    expected_variables: expectedVariables(
      "example",
      "example/control-artifacts",
      null,
      "stable-subject",
      revision,
    ),
    expected_secret_names: ["HF_INFERENCE_TOKEN", "HF_TOKEN"],
    observed_preconditions: {
      namespaceListingsComplete: true,
      space: null,
      bucket: null,
    },
  };
}

describe("installer CLI contract", () => {
  it("locates a saved plan by Space ID with an optional state override", () => {
    expect(parseSavedPlanOptions(["--space", "example/control"])).toEqual({
      space: "example/control",
    });
    expect(
      parseSavedPlanOptions([
        "--space",
        "example/control",
        "--state-dir",
        "/state-placeholder",
      ]),
    ).toEqual({
      space: "example/control",
      stateDirectory: "/state-placeholder",
    });
    for (const obsolete of ["--plan", "--confirm"]) {
      expect(() => parseSavedPlanOptions([obsolete, "<obsolete-value>"])).toThrow(
        "invalid command arguments",
      );
    }
  });

  it("prints a path-free digest-free plan summary and next command", () => {
    const output = formatPlanOutput(plan());
    expect(output).toContain("Space:      example/control");
    expect(output).toContain("Bucket:     example/control-artifacts");
    expect(output).toContain("Write mode: disabled");
    expect(output).toContain("Next: npm run install:apply -- --space example/control");
    expect(output).not.toContain("sha256:");
    expect(output).not.toContain("/state-placeholder");
    expect(output).not.toContain("/repository-placeholder");
    expect(formatPlanOutput(plan(), true)).toContain("same --state-dir");
  });

  it("keeps activation separate after a verified installation", () => {
    const output = formatApplyOutput("example/control", {
      production_ready: false,
      anonymous_live: "passed",
      anonymous_ready: "passed",
      authenticated_system: "skipped",
      source_upload_revision: "passed",
    });
    expect(output).toContain("Installation verified.");
    expect(output).toContain("Write mode: disabled");
    expect(output).toContain("Production ready: no");
    expect(output).toContain("before activation");
  });
});
