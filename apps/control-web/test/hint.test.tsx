// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Hint } from "../src/ui";

describe("Hint", () => {
  it("does not attach a native title beside the styled tooltip", () => {
    render(<Hint text="Explanation">Replacement eligible</Hint>);
    expect(screen.getByText("Replacement eligible")).not.toHaveAttribute("title");
    const tooltip = screen.getByRole("tooltip", { hidden: true });
    expect(tooltip).toHaveTextContent("Explanation");
    expect(tooltip).toHaveClass("fixed", "invisible");
    expect(tooltip.parentElement).toBe(document.body);
  });
});
