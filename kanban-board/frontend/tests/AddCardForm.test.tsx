/**
 * Tests for the AddCardForm component.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import AddCardForm from "../src/components/AddCardForm";

describe("AddCardForm", () => {
  it("should render the collapsed add card button by default", () => {
    render(<AddCardForm loading={false} onSubmit={vi.fn()} />);

    expect(screen.getByTestId("add-card-btn")).toBeInTheDocument();
    expect(screen.queryByTestId("add-card-form")).toBeNull();
  });

  it("should expand the form when the add button is clicked", async () => {
    const user = userEvent.setup();
    render(<AddCardForm loading={false} onSubmit={vi.fn()} />);

    await user.click(screen.getByTestId("add-card-btn"));

    expect(screen.getByTestId("add-card-form")).toBeInTheDocument();
    expect(screen.getByTestId("add-card-title")).toBeInTheDocument();
    expect(screen.getByTestId("add-card-description")).toBeInTheDocument();
  });

  it("should call onSubmit with title and description", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    render(<AddCardForm loading={false} onSubmit={onSubmit} />);

    await user.click(screen.getByTestId("add-card-btn"));
    await user.type(screen.getByTestId("add-card-title"), "New Task");
    await user.type(
      screen.getByTestId("add-card-description"),
      "A description",
    );
    await user.click(screen.getByTestId("add-card-submit"));

    expect(onSubmit).toHaveBeenCalledWith("New Task", "A description");
  });

  it("should collapse the form after submission", async () => {
    const user = userEvent.setup();
    render(<AddCardForm loading={false} onSubmit={vi.fn()} />);

    await user.click(screen.getByTestId("add-card-btn"));
    await user.type(screen.getByTestId("add-card-title"), "New Task");
    await user.click(screen.getByTestId("add-card-submit"));

    // Should return to collapsed state
    expect(screen.getByTestId("add-card-btn")).toBeInTheDocument();
    expect(screen.queryByTestId("add-card-form")).toBeNull();
  });

  it("should collapse the form on cancel", async () => {
    const user = userEvent.setup();
    render(<AddCardForm loading={false} onSubmit={vi.fn()} />);

    await user.click(screen.getByTestId("add-card-btn"));
    expect(screen.getByTestId("add-card-form")).toBeInTheDocument();

    await user.click(screen.getByTestId("add-card-cancel"));

    expect(screen.getByTestId("add-card-btn")).toBeInTheDocument();
    expect(screen.queryByTestId("add-card-form")).toBeNull();
  });

  it("should not submit when title is empty", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    render(<AddCardForm loading={false} onSubmit={onSubmit} />);

    await user.click(screen.getByTestId("add-card-btn"));

    // Submit button should be disabled when title is empty
    const submitBtn = screen.getByTestId("add-card-submit");
    expect(submitBtn).toBeDisabled();

    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("should show loading text when loading is true", async () => {
    const user = userEvent.setup();
    render(<AddCardForm loading={true} onSubmit={vi.fn()} />);

    await user.click(screen.getByTestId("add-card-btn"));
    // Type something so we can see the submit button text
    await user.type(screen.getByTestId("add-card-title"), "Task");

    const submitBtn = screen.getByTestId("add-card-submit");
    expect(submitBtn).toHaveTextContent("Adding\u2026");
    expect(submitBtn).toBeDisabled();
  });
});
