/**
 * Tests for the CardComponent.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import CardComponent from "../src/components/CardComponent";
import type { Card } from "../src/types/board";

function makeCard(overrides: Partial<Card> = {}): Card {
  return {
    id: "1",
    column_id: "10",
    title: "Test Card",
    description: null,
    position: 1024,
    assignee_id: null,
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("CardComponent", () => {
  it("should render the card title", () => {
    render(<CardComponent card={makeCard({ title: "My Task" })} />);

    expect(screen.getByText("My Task")).toBeInTheDocument();
  });

  it("should render a description preview when description is provided", () => {
    render(
      <CardComponent
        card={makeCard({ description: "This is a short description." })}
      />,
    );

    expect(screen.getByText("This is a short description.")).toBeInTheDocument();
  });

  it("should truncate long descriptions", () => {
    const longDesc = "A".repeat(120);
    render(<CardComponent card={makeCard({ description: longDesc })} />);

    // Should show truncated text with ellipsis (80 chars + ellipsis)
    const descEl = screen.getByText(/^A+\u2026$/);
    expect(descEl).toBeInTheDocument();
    // The displayed text should be shorter than the original
    expect(descEl.textContent!.length).toBeLessThan(longDesc.length);
  });

  it("should not render description when it is null", () => {
    const { container } = render(
      <CardComponent card={makeCard({ description: null })} />,
    );

    const descEl = container.querySelector(".text-xs.text-gray-500");
    expect(descEl).toBeNull();
  });

  it("should render an assignee avatar when assignee_id is present", () => {
    render(<CardComponent card={makeCard({ assignee_id: "42" })} />);

    expect(screen.getByTestId("card-1-avatar")).toBeInTheDocument();
  });

  it("should not render an assignee avatar when assignee_id is null", () => {
    render(<CardComponent card={makeCard({ assignee_id: null })} />);

    expect(screen.queryByTestId("card-1-avatar")).toBeNull();
  });

  it("should render with the correct test id", () => {
    render(<CardComponent card={makeCard({ id: "99" })} />);

    expect(screen.getByTestId("card-99")).toBeInTheDocument();
  });
});
