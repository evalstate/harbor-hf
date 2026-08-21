// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Hint } from "../src/ui";

describe("Hint", () => {
  it("does not attach a native title beside the styled tooltip", () => {
    render(<Hint text="Explanation">Replacement eligible</Hint>);
    expect(screen.getByText("Replacement eligible")).not.toHaveAttribute("title");
    expect(screen.getByRole("tooltip", { hidden: true })).toHaveTextContent(
      "Explanation",
    );
  });
});
