import { describe, expect, it } from "vitest";
import {
  bucketHfUri,
  bucketTreeUrl,
  meanWaldInterval,
  outputsPrefix,
  summarizePublishedResult,
  wilsonInterval,
} from "../src/result-summary.js";

describe("result summary", () => {
  it("matches a known Wilson 95% interval for 80 of 100", () => {
    const interval = wilsonInterval(80, 100);
    expect(interval.low).toBeCloseTo(0.7111, 3);
    expect(interval.high).toBeCloseTo(0.8667, 3);
  });

  it("rejects inverted binomial counts", () => {
    expect(() => wilsonInterval(2, 1)).toThrow(/more successes/);
  });

  it("omits a mean interval until two costs exist", () => {
    expect(meanWaldInterval([4])).toBeNull();
    const interval = meanWaldInterval([10, 20, 30]);
    expect(interval).not.toBeNull();
    expect(interval?.low).toBeCloseTo(8.684, 3);
    expect(interval?.high).toBeCloseTo(31.316, 3);
  });

  it("builds Hub browse URLs from a Bucket id and object prefix", () => {
    expect(outputsPrefix("imports/result-one.json", "publication-one")).toBe("imports");
    expect(bucketTreeUrl("example-org/artifacts", "imports")).toBe(
      "https://huggingface.co/buckets/example-org/artifacts/tree/imports",
    );
    expect(bucketHfUri("example-org/artifacts", "imports")).toBe(
      "hf://buckets/example-org/artifacts/imports",
    );
  });

  it("summarizes pass rate from sealed complete tasks and costs from selected attempts", () => {
    const summary = summarizePublishedResult({
      bucketId: "example-org/artifacts",
      publicationId: "publication-one",
      resultPath: "results/schema=v1/publications/publication-one/receipt.json",
      catalogTaskCount: 2,
      catalogStrictPassCount: null,
      observedCostMicrousd: 56_526,
      tasks: [
        {
          task_id: "task-a",
          terminal_outcome: "complete",
          selected_attempt_id: "attempt-a",
        },
        {
          task_id: "task-b",
          terminal_outcome: "benchmark_timeout",
          selected_attempt_id: "attempt-b",
        },
      ],
      attempts: [
        {
          attempt_id: "attempt-a",
          task_id: "task-a",
          outcome: "complete",
          cost_microusd: 21_000,
          metrics: { reward: 1, input_tokens: 1000, output_tokens: 40 },
        },
        {
          attempt_id: "attempt-b",
          task_id: "task-b",
          outcome: "benchmark_timeout",
          cost_microusd: 34_929,
          metrics: { reward: 0, input_tokens: 191_573, output_tokens: 28_959 },
        },
      ],
    });
    expect(summary.pass_count).toBe(1);
    expect(summary.pass_rate).toBe(0.5);
    expect(summary.pass_rate_ci95).toEqual(wilsonInterval(1, 2));
    expect(summary.input_tokens).toBe(192_573);
    expect(summary.output_tokens).toBe(28_999);
    expect(summary.inference_cost_microusd).toBe(55_929);
    expect(summary.mean_task_cost_microusd).toBe(27_964.5);
    expect(summary.task_cost_ci95).toEqual(meanWaldInterval([21_000, 34_929]));
    expect(summary.observed_cost_microusd).toBe(56_526);
    expect(summary.outputs_prefix).toBe(
      "results/schema=v1/publications/publication-one",
    );
    expect(summary.outputs_url).toContain("/tree/results/schema%3Dv1/publications/");
    expect(summary.tasks).toHaveLength(2);
    expect(summary.tasks[1]?.outcome).toBe("benchmark_timeout");
  });

  it("uses catalog strict passes when no run tasks are projected", () => {
    const summary = summarizePublishedResult({
      bucketId: "example-org/artifacts",
      publicationId: "publication-one",
      resultPath: "imports/result-one.json",
      catalogTaskCount: 89,
      catalogStrictPassCount: 1,
      observedCostMicrousd: null,
      tasks: [],
      attempts: [],
    });
    expect(summary.pass_count).toBe(1);
    expect(summary.pass_rate).toBeCloseTo(1 / 89);
    expect(summary.pass_rate_ci95).toEqual(wilsonInterval(1, 89));
    expect(summary.tasks).toEqual([]);
    expect(summary.outputs_url).toBe(
      "https://huggingface.co/buckets/example-org/artifacts/tree/imports",
    );
  });
});
