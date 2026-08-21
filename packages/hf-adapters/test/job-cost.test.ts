import { describe, expect, it } from "vitest";
import { jobHardwareCostMicrousd } from "../src/job-cost.js";

describe("jobHardwareCostMicrousd", () => {
  it("charges elapsed hours at the locked rate", () => {
    expect(
      jobHardwareCostMicrousd(
        {
          startedAt: "2026-08-21T12:00:00.000Z",
          finishedAt: "2026-08-21T13:30:00.000Z",
          status: { stage: "COMPLETED" },
        },
        10_000,
      ),
    ).toBe(15_000);
  });

  it("charges nothing before the Job starts", () => {
    expect(jobHardwareCostMicrousd({ status: { stage: "SCHEDULING" } }, 10_000)).toBe(
      0,
    );
  });
});
