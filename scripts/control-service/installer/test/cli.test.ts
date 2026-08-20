import { describe, expect, it } from "vitest";
import { formatPlanOutput, parseApplyOptions } from "../cli.js";

describe("installer CLI contract", () => {
  it("applies a saved plan without exposing its digest", () => {
    expect(parseApplyOptions(["--plan", "<plan-file>"])).toEqual({
      plan: "<plan-file>",
    });
    expect(() =>
      parseApplyOptions([
        "--plan",
        "<plan-file>",
        "--confirm",
        `sha256:${"0".repeat(64)}`,
      ]),
    ).toThrow("invalid command arguments");
  });

  it("prints only the private plan path after planning", () => {
    const output = formatPlanOutput("<plan-file>");
    expect(output).toBe("plan: <plan-file>\n");
    expect(output).not.toContain("sha256:");
    expect(output).not.toContain("confirm");
  });
});
