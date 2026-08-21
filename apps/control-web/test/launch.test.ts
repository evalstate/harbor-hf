import { describe, expect, it } from "vitest";
import {
  doubleReservationMicrousd,
  launchPolicyForBenchmark,
  preferredAlias,
  profileLabel,
  selectDeploymentAlias,
  selectHarnessAlias,
} from "../src/launch";

describe("launch helpers", () => {
  it("keeps the preferred alias when it is approved", () => {
    expect(preferredAlias("gpt-oss-20b", ["control-smoke", "gpt-oss-20b"])).toBe(
      "gpt-oss-20b",
    );
  });

  it("selects OpenCode by agent and reasoning without a silent substitute", () => {
    expect(
      selectHarnessAlias(
        [
          {
            alias: "pi-high",
            spec: { agent: "pi", reasoning_effort: "high" },
          },
          {
            alias: "opencode",
            spec: { agent: "opencode", reasoning_effort: "off" },
          },
        ],
        "opencode",
        "off",
      ),
    ).toBe("opencode");
    expect(() =>
      selectHarnessAlias(
        [{ alias: "pi-high", spec: { agent: "pi", reasoning_effort: "high" } }],
        "opencode",
        "off",
      ),
    ).toThrow(/no approved opencode harness/);
  });

  it("maps Terminal-Bench 2.1 diagnostic to its launch policy", () => {
    expect(launchPolicyForBenchmark("terminal-bench-2-1-diagnostic-1")).toBe(
      "tb21-diagnostic-1",
    );
  });

  it("doubles the estimated reservation for the default ceiling", () => {
    expect(doubleReservationMicrousd(5_200_000)).toBe(10_400_000);
  });

  it("labels Terminal-Bench 2.1 with task count", () => {
    expect(
      profileLabel("benchmark", "terminal-bench-2-1-diagnostic-1", {
        benchmark: "terminal-bench-2-1",
        task_ids: ["a", "b"],
        trial_indices: [1, 1],
      }),
    ).toBe("Terminal-Bench 2.1 · 2 tasks");
  });

  it("selects a providers deployment for the locked model and harness", () => {
    expect(
      selectDeploymentAlias(
        [
          {
            alias: "hf-cpu-smoke",
            spec: { models: ["control-smoke"], harnesses: ["control-smoke"] },
          },
          {
            alias: "tb21-providers",
            spec: {
              models: ["gpt-oss-20b"],
              harnesses: ["opencode"],
              sandbox_template: {
                inference_upstream: "https://router.huggingface.co/v1",
              },
            },
          },
        ],
        "providers",
        "gpt-oss-20b",
        "opencode",
      ),
    ).toBe("tb21-providers");
  });
});
