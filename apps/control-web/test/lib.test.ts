import { describe, expect, it } from "vitest";
import {
  estimateLaunchReservationMicrousd,
  formatPercent,
  formatPercentInterval,
  formatTokens,
  runNameClass,
} from "../src/lib";

describe("launch reservation estimate", () => {
  it("counts one execution reservation per worker Job", () => {
    expect(
      estimateLaunchReservationMicrousd(
        445,
        {
          preparation: "required",
          worker_max_tasks_per_job: 445,
        },
        {
          reservation_microusd: 5_100_000,
          preparation_reservation_microusd: 100_000,
          max_preparation_attempts: 2,
        },
      ),
    ).toBe(5_300_000);
  });

  it("counts each bounded execution batch", () => {
    expect(
      estimateLaunchReservationMicrousd(
        10,
        { worker_max_tasks_per_job: 4 },
        { reservation_microusd: 1_000_000 },
      ),
    ).toBe(3_000_000);
  });
});

describe("result formatting", () => {
  it("renders percents and token counts for the Results page", () => {
    expect(formatPercent(0.5)).toBe("50.0%");
    expect(formatPercentInterval({ low: 0.095, high: 0.905 })).toBe("9.5%–90.5%");
    expect(formatTokens(191_573).replace(/\D/g, "")).toBe("191573");
    expect(formatTokens(null)).toBe("—");
  });

  it("wraps complete run names instead of forcing a wide column", () => {
    expect(runNameClass).toContain("break-all");
    expect(runNameClass).toContain("min-w-0");
    expect(runNameClass).not.toContain("min-w-[20rem]");
    expect(runNameClass).not.toContain("min-w-[22rem]");
  });
});
