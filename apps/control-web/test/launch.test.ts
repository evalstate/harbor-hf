import { describe, expect, it } from "vitest";
import {
  doubleReservationMicrousd,
  labeledHarness,
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

  it("labels DeepSeek Harness instead of the dsh alias", () => {
    expect(profileLabel("harness", "dsh", { agent: "dsh" })).toBe("DeepSeek Harness");
    expect(
      profileLabel("harness", "dsh-high-deepseek-v4-flash-0731-together", {
        agent: "dsh",
      }),
    ).toBe("DeepSeek Harness");
    expect(labeledHarness("dsh")).toBe("DeepSeek Harness");
    expect(labeledHarness(null)).toBe("—");
  });

  it("labels FX instead of title-casing the alias", () => {
    expect(profileLabel("harness", "fx", { agent: "fx" })).toBe("FX");
    expect(labeledHarness("fx")).toBe("FX");
  });

  it("labels Terminal-Bench 2.1 by source tasks and trials, not logical attempts", () => {
    expect(
      profileLabel("benchmark", "terminal-bench-2-1-official-5", {
        benchmark: "terminal-bench-2-1",
        task_ids: Array.from({ length: 10 }, (_, index) => `task-${index}`),
        source_task_ids: ["alpha", "beta"],
        trial_indices: [1, 2, 3, 4, 5],
      }),
    ).toBe("Terminal-Bench 2.1 · 2 tasks with 5 trials each");
    expect(
      profileLabel("benchmark", "terminal-bench-2-1-diagnostic-1", {
        benchmark: "terminal-bench-2-1",
        task_ids: ["a", "b"],
        source_task_ids: ["a", "b"],
        trial_indices: [1, 1],
      }),
    ).toBe("Terminal-Bench 2.1 · 2 tasks");
    expect(
      profileLabel("benchmark", "control-smoke", {
        task_ids: ["control-smoke-task"],
      }),
    ).toBe("Control Smoke · 1 task");
  });

  it("treats a providers deployment as providers after the API redacts the router URL", () => {
    expect(
      selectDeploymentAlias(
        [
          {
            alias: "tb21-gpt-oss-20b-opencode-providers",
            spec: {
              models: ["gpt-oss-20b"],
              harnesses: ["opencode"],
              inference_provider: "together",
              sandbox_template: { inference_upstream: "<redacted>" },
            },
          },
        ],
        "providers",
        "gpt-oss-20b",
        "opencode",
      ),
    ).toBe("tb21-gpt-oss-20b-opencode-providers");
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
